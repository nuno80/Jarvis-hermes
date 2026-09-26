import os
import tempfile
import unittest
from pathlib import Path
from jarvis_hermes.vault import Vault, VaultError

class PreferencesTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.vault_path = Path(self.tmpdir.name)
        self.vault = Vault(str(self.vault_path))

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_load_preferences_from_yaml(self):
        pref_file = self.vault_path / "preferences.yaml"
        pref_file.write_text("travel:\n  max_budget: 800\n  preferred_airline: LH\n", encoding="utf-8")
        prefs, version = self.vault.load_preferences()
        self.assertEqual(prefs["travel"]["max_budget"], 800)
        self.assertEqual(prefs["travel"]["preferred_airline"], "LH")
        self.assertTrue(len(version) > 0)

    def test_preferences_update_dynamically(self):
        pref_file = self.vault_path / "preferences.yaml"
        pref_file.write_text("travel:\n  max_budget: 800\n", encoding="utf-8")
        prefs1, v1 = self.vault.load_preferences()
        self.assertEqual(prefs1["travel"]["max_budget"], 800)

        # Update file directly (e.g. user manually edits preferences.yaml)
        pref_file.write_text("travel:\n  max_budget: 950\n", encoding="utf-8")
        prefs2, v2 = self.vault.load_preferences()
        self.assertEqual(prefs2["travel"]["max_budget"], 950)
        self.assertNotEqual(v1, v2)

    def test_preferences_cannot_grant_permissions(self):
        # Preferences cannot grant permissions or modify security policy
        pref_file = self.vault_path / "preferences.yaml"
        pref_file.write_text("permissions:\n  allow_all_commands: true\n  bypass_approval: true\n", encoding="utf-8")
        prefs, _ = self.vault.load_preferences()
        self.assertNotIn("permissions", prefs)

    def test_update_preference_with_conflict_detection(self):
        pref_file = self.vault_path / "preferences.yaml"
        pref_file.write_text("travel:\n  max_budget: 800\n", encoding="utf-8")
        _, v1 = self.vault.load_preferences()

        # Simulate concurrent edit
        pref_file.write_text("travel:\n  max_budget: 850\n", encoding="utf-8")

        # Attempting update with old expected_version must fail with CONFLICT
        with self.assertRaises(VaultError) as ctx:
            self.vault.update_preference("travel.max_budget", 950, expected_version=v1)
        self.assertEqual(ctx.exception.code, "CONFLICT")

        # Successful update with current version
        _, v2 = self.vault.load_preferences()
        res = self.vault.update_preference("travel.max_budget", 950, expected_version=v2)
        self.assertEqual(res["value"], 950)
        self.assertEqual(res["preferences"]["travel"]["max_budget"], 950)

    def test_parse_frontmatter_in_notes_and_detect_ambiguity(self):
        pref_file = self.vault_path / "preferences.yaml"
        pref_file.write_text("travel:\n  max_budget: 800\n", encoding="utf-8")

        note_file = self.vault_path / "Travel.md"
        note_file.write_text(
            "---\n"
            "type: preference\n"
            "domain: travel\n"
            "source: inferred\n"
            "confidence: 0.7\n"
            "updated_at: 2026-09-25T12:00:00Z\n"
            "preferences:\n"
            "  max_budget: 600\n"
            "---\n"
            "# Viaggi\n"
            "Preferisce voli economici attorno ai 600 euro.\n",
            encoding="utf-8"
        )
        profile = self.vault.get_profile()
        self.assertIn("explicit_preferences", profile)
        self.assertEqual(profile["explicit_preferences"]["travel"]["max_budget"], 800)
        self.assertEqual(len(profile["frontmatter_notes"]), 1)
        self.assertEqual(profile["frontmatter_notes"][0]["domain"], "travel")
        self.assertEqual(len(profile["ambiguities"]), 1)
        self.assertEqual(profile["ambiguities"][0]["key"], "max_budget")
        self.assertEqual(profile["ambiguities"][0]["explicit_value"], 800)
        self.assertEqual(profile["ambiguities"][0]["note_value"], 600)


    def test_propose_inferred_memory_and_precedence_of_explicit(self):
        pref_file = self.vault_path / "preferences.yaml"
        pref_file.write_text("travel:\n  max_budget: 950\n", encoding="utf-8")

        # 1. Propose candidate memory from observed interaction
        mem = self.vault.propose_memory(
            domain="travel",
            key="max_budget",
            value=600,
            evidence="Observed search for cheap flights under 600 EUR",
            confidence=0.65
        )
        self.assertEqual(mem["status"], "candidate")
        self.assertTrue(mem["conflicts_with_explicit"])
        self.assertEqual(mem["explicit_value"], 950)
        self.assertEqual(mem["value"], 600)

        # 2. Verify candidate memory did NOT overwrite explicit preference
        prefs, _ = self.vault.load_preferences()
        self.assertEqual(prefs["travel"]["max_budget"], 950)

        # 3. Verify get_profile reflects candidate note, soft status, and surfaces ambiguity
        profile = self.vault.get_profile()
        self.assertEqual(profile["explicit_preferences"]["travel"]["max_budget"], 950)
        self.assertEqual(len(profile["ambiguities"]), 1)
        self.assertEqual(profile["ambiguities"][0]["status"], "candidate")
        self.assertEqual(profile["ambiguities"][0]["explicit_value"], 950)
        self.assertEqual(profile["ambiguities"][0]["note_value"], 600)

    def test_write_note_with_concurrency_conflict(self):
        note_id = "Projects/Plan.md"
        res1 = self.vault.write_note(note_id, "# Initial Plan\n", expected_version=None)
        self.assertEqual(res1["note_id"], note_id)
        v1 = res1["version"]

        # External concurrent update
        res2 = self.vault.write_note(note_id, "# Plan modified concurrently\n", expected_version=v1)
        v2 = res2["version"]

        # Attempt to write with stale v1 must raise CONFLICT
        with self.assertRaises(VaultError) as ctx:
            self.vault.write_note(note_id, "# Attempted overwrite\n", expected_version=v1)
        self.assertEqual(ctx.exception.code, "CONFLICT")

        # Writing with v2 succeeds
        res3 = self.vault.write_note(note_id, "# Up to date\n", expected_version=v2)
        self.assertEqual(res3["note_id"], note_id)

    def test_forget_inferred_memory_and_retention_backup(self):
        # 1. Propose inferred memory note
        mem = self.vault.propose_memory(
            domain="travel",
            key="seat_preference",
            value="window",
            evidence="User picked window seat on flight to Bali",
            confidence=0.8
        )
        note_id = mem["note_id"]
        v1 = mem["version"]

        # 2. Forget key inside candidate memory note
        del_key_res = self.vault.forget_memory(note_id, key="seat_preference", expected_version=v1)
        self.assertEqual(del_key_res["action"], "key_removed")
        self.assertIn("Local backup stored in .jarvis_backups", del_key_res["retention_policy"])
        self.assertTrue((self.vault_path / del_key_res["backup_retained"]).is_file())

        # Verify profile updated
        profile = self.vault.get_profile()
        for fn in profile["frontmatter_notes"]:
            if fn["note_id"] == note_id:
                self.assertNotIn("seat_preference", fn.get("preferences", {}))

        # 3. Full forgetting/removal of note
        v2 = del_key_res["version"]
        del_note_res = self.vault.forget_memory(note_id, expected_version=v2)
        self.assertEqual(del_note_res["action"], "deleted")
        self.assertFalse((self.vault_path / note_id).exists())

        # Backup file exists and declared
        self.assertTrue((self.vault_path / del_note_res["backup_retained"]).is_file())


if __name__ == "__main__":
    unittest.main()


