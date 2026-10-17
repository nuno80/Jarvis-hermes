import re
import unittest
from datetime import datetime, timezone

from jarvis_hermes.travel import (
    Offer,
    Segment,
    Slice,
    compare_flight_offers,
)


def _make_dummy_offer(
    offer_id: str,
    price: float,
    duration_minutes: int,
    stops: int = 0,
    has_airport_change: bool = False,
    has_tight_connection: bool = False,
    has_overnight_layover: bool = False,
    unknown_fields: dict[str, str] | None = None,
    observed_at: str = "2026-10-15T10:00:00Z",
    provider: str = "serpapi",
) -> dict:
    hours, mins = divmod(duration_minutes, 60)
    iso_dur = f"PT{hours}H{mins}M" if hours else f"PT{mins}M"
    s = Slice(
        origin="MXP",
        destination="JFK",
        departure_date="2026-11-15",
        duration=iso_dur,
        stops_count=stops,
        has_airport_change=has_airport_change,
        has_tight_connection=has_tight_connection,
        has_overnight_layover=has_overnight_layover,
        segments=[
            Segment(
                origin_airport="MXP",
                destination_airport="JFK",
                departing_at="2026-11-15T08:00:00Z",
                arriving_at="2026-11-15T18:00:00Z",
                marketing_carrier="AZ",
                duration=iso_dur,
            )
        ],
    )
    offer = Offer(
        offer_id=offer_id,
        provider=provider,
        observed_at=observed_at,
        total_amount=price,
        currency="EUR",
        price_per_passenger=price,
        passengers_count=1,
        slices=[s],
        unknown_fields=unknown_fields or {},
    )
    return offer.to_dict()


class ParetoComparisonTests(unittest.TestCase):
    def test_import_and_signature(self):
        offers = [
            _make_dummy_offer("off_cheap", 300.0, 600, stops=1),
            _make_dummy_offer("off_fast", 600.0, 300, stops=0),
            _make_dummy_offer("off_mid", 400.0, 360, stops=0),
        ]
        res = compare_flight_offers(offers)
        self.assertIn("alternatives", res)
        self.assertIn("pareto_frontier", res)

    def test_pareto_frontier_and_three_distinct_alternatives_with_known_optimum(self):
        # Fixture con optimum noto:
        # A: 250 EUR, 500 min, 1 stop (cheapest)
        # B: 600 EUR, 300 min, 0 stops (fastest)
        # C: 320 EUR, 330 min, 0 stops (best compromise: di poco più caro di A, ma quasi veloce come B senza scali)
        # D: 500 EUR, 600 min, 2 stops (dominata da A e da C: prezzo peggiore e durata peggiore)
        offers = [
            _make_dummy_offer("A", 250.0, 500, stops=1),
            _make_dummy_offer("B", 600.0, 300, stops=0),
            _make_dummy_offer("C", 320.0, 330, stops=0),
            _make_dummy_offer("D", 500.0, 600, stops=2),
        ]
        res = compare_flight_offers(offers)

        # D deve essere esclusa dalla frontiera di Pareto
        frontier_ids = [o["offer_id"] for o in res["pareto_frontier"]]
        self.assertIn("A", frontier_ids)
        self.assertIn("B", frontier_ids)
        self.assertIn("C", frontier_ids)
        self.assertNotIn("D", frontier_ids)

        alts = res["alternatives"]
        self.assertEqual(alts["cheapest"]["offer_id"], "A")
        self.assertEqual(alts["fastest"]["offer_id"], "B")
        self.assertEqual(alts["best_compromise"]["offer_id"], "C")

        # Verifica differenze numeriche calcolate rispetto al cheapest (baseline)
        diff_fastest = alts["fastest"]["diff_vs_cheapest"]
        self.assertEqual(diff_fastest["price_delta"], 350.0)  # 600 - 250
        self.assertEqual(diff_fastest["duration_minutes_delta"], -200)  # 300 - 500

        diff_compromise = alts["best_compromise"]["diff_vs_cheapest"]
        self.assertEqual(diff_compromise["price_delta"], 70.0)  # 320 - 250
        self.assertEqual(diff_compromise["duration_minutes_delta"], -170)  # 330 - 500

    def test_hard_filters_before_pareto(self):
        # Filtro hard: max_budget, max_stops, max_duration_minutes, allow_airport_change
        offers = [
            _make_dummy_offer("O_OK", 300.0, 400, stops=1),
            _make_dummy_offer("O_OVER_BUDGET", 800.0, 300, stops=0),
            _make_dummy_offer("O_TOO_MANY_STOPS", 250.0, 500, stops=3),
            _make_dummy_offer("O_TOO_LONG", 280.0, 900, stops=1),
            _make_dummy_offer("O_AIRPORT_CHANGE", 200.0, 350, stops=1, has_airport_change=True),
        ]
        constraints = {
            "max_budget": 500.0,
            "max_stops": 2,
            "max_duration_minutes": 600,
            "allow_airport_change": False,
        }
        res = compare_flight_offers(offers, constraints=constraints)
        frontier_ids = [o["offer_id"] for o in res["pareto_frontier"]]
        self.assertEqual(frontier_ids, ["O_OK"])
        self.assertEqual(res["discarded_by_constraints_count"], 4)

    def test_unknown_baggage_and_taxes_are_not_counted_as_zero(self):
        # Requisito J15 / E1: bagagli/tasse ignoti (unknown_fields) non devono essere conteggiati
        # come zero (es. non assumerli gratis), ma devono essere esplicitati come avvertenze / unverified
        # e non possono rendere l'offerta artificialmente vantaggiosa rispetto a un'offerta con bagaglio incluso.
        o_unknown = _make_dummy_offer(
            "O_UNKNOWN",
            300.0,
            400,
            unknown_fields={"checked_baggage": "unknown", "taxes_included": "unknown"},
        )
        o_known = _make_dummy_offer("O_KNOWN", 320.0, 400, unknown_fields={})
        res = compare_flight_offers([o_unknown, o_known])

        # Entrambe le offerte sono presenti, ma o_unknown deve avere il flag/warning di incompletezza dati
        alt_unknown = [
            a for a in res["alternatives"].values() if a["offer_id"] == "O_UNKNOWN"
        ]
        self.assertTrue(len(alt_unknown) > 0)
        self.assertTrue(alt_unknown[0]["pricing_incomplete"])
        self.assertIn("checked_baggage", alt_unknown[0]["unverified_fields"])
        self.assertIn("taxes_included", alt_unknown[0]["unverified_fields"])
        self.assertIn(
            "tasse o bagagli non verificati: potrebbero esserci costi aggiuntivi",
            alt_unknown[0]["warning"],
        )

    def test_verifiable_explanation_text_generation(self):
        # Requisito J15 / E1: le motivazioni devono essere verificabili dai dati (nessuna statistica inventata)
        offers = [
            _make_dummy_offer("O_CHEAP", 250.0, 500, stops=1),
            _make_dummy_offer("O_FAST", 550.0, 300, stops=0),
            _make_dummy_offer("O_MID", 310.0, 330, stops=0),
        ]
        res = compare_flight_offers(offers)

        # Verifica presenza della spiegazione testuale strutturata
        self.assertIn("summary_explanation", res)
        explanation = res["summary_explanation"]
        self.assertIn("O_CHEAP", explanation)
        self.assertIn("O_FAST", explanation)
        self.assertIn("O_MID", explanation)

        # La spiegazione deve menzionare i delta numerici precisi
        # Es. Fastest costa +300.0 EUR ma fa risparmiare 200 minuti (3h 20m)
        self.assertIn("+300.0", explanation)
        self.assertIn("200 minuti", explanation)

    def test_seller_links_and_freshness_metadata_explicit(self):
        # Requisito J15 / E1: collegamento al venditore e freschezza espliciti;
        # self-transfer e indicatori di rischio descritti senza probabilità inventate.
        observed_ts = "2026-10-15T12:30:00Z"
        o = _make_dummy_offer(
            "O_SELLER",
            400.0,
            350,
            stops=1,
            has_tight_connection=True,
            observed_at=observed_ts,
            provider="serpapi",
        )
        res = compare_flight_offers([o])
        alt = res["alternatives"]["cheapest"]

        self.assertEqual(alt["provider"], "serpapi")
        self.assertEqual(alt["observed_at"], observed_ts)
        self.assertIn("coincidenza stretta", alt["risk_factors"])
        # Nessun numero di probabilità inventata nel report
        for rf in alt["risk_factors"]:
            self.assertFalse(re.search(r"\b\d+%\b", rf))

    def test_fewer_than_three_distinct_offers_no_duplicates(self):
        # Quando ci sono meno di tre offerte (es. 1 sola offerta o tutte coincidenti),
        # non duplicare le alternative!
        single_offer = [_make_dummy_offer("ONLY_ONE", 350.0, 420, stops=0)]
        res = compare_flight_offers(single_offer)

        alts = res["alternatives"]
        self.assertEqual(len(alts), 1)
        self.assertIn("cheapest", alts)
        self.assertNotIn("fastest", alts)
        self.assertNotIn("best_compromise", alts)
        self.assertEqual(alts["cheapest"]["offer_id"], "ONLY_ONE")

        # Due offerte distinte: cheapest e fastest coincidono in parte o sono solo 2
        two_offers = [
            _make_dummy_offer("O1", 300.0, 500, stops=1),
            _make_dummy_offer("O2", 500.0, 300, stops=0),
        ]
        res2 = compare_flight_offers(two_offers)
        alts2 = res2["alternatives"]
        self.assertEqual(len(alts2), 2)
        self.assertIn("cheapest", alts2)
        self.assertIn("fastest", alts2)
        self.assertNotIn("best_compromise", alts2)
        self.assertEqual(alts2["cheapest"]["offer_id"], "O1")
        self.assertEqual(alts2["fastest"]["offer_id"], "O2")


if __name__ == "__main__":
    unittest.main()
