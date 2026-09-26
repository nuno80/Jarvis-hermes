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


if __name__ == "__main__":
    unittest.main()

