"""Unit tests for travel provider integration, validation, and offer normalization."""
from datetime import datetime
import json
import os
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

from jarvis_hermes.travel import (
    DuffelProvider,
    Offer,
    Segment,
    SerpApiProvider,
    Slice,
    TravelError,
    TravelManager,
    TravelRequest,
)


class TravelRequestValidationTests(unittest.TestCase):
    def test_missing_origin_raises_error(self):
        with self.assertRaises(TravelError) as ctx:
            TravelRequest.from_params({"destination": "JFK", "departure_date": "2026-11-01", "passengers": 1})
        self.assertEqual(ctx.exception.code, "MISSING_PARAMETER")
        self.assertIn("origine", ctx.exception.message.lower())

    def test_missing_destination_raises_error(self):
        with self.assertRaises(TravelError) as ctx:
            TravelRequest.from_params({"origin": "MXP", "departure_date": "2026-11-01", "passengers": 1})
        self.assertEqual(ctx.exception.code, "MISSING_PARAMETER")
        self.assertIn("destinazione", ctx.exception.message.lower())

    def test_missing_date_or_year_raises_error(self):
        with self.assertRaises(TravelError) as ctx:
            TravelRequest.from_params({"origin": "MXP", "destination": "JFK", "passengers": 1})
        self.assertEqual(ctx.exception.code, "MISSING_PARAMETER")

        with self.assertRaises(TravelError) as ctx:
            TravelRequest.from_params({"origin": "MXP", "destination": "JFK", "departure_date": "11-05", "passengers": 1})
        self.assertEqual(ctx.exception.code, "INVALID_DATE_FORMAT")
        self.assertIn("anno esplicito", ctx.exception.message)

    def test_missing_passengers_raises_error(self):
        with self.assertRaises(TravelError) as ctx:
            TravelRequest.from_params({"origin": "MXP", "destination": "JFK", "departure_date": "2026-11-01"})
        self.assertEqual(ctx.exception.code, "MISSING_PARAMETER")
        self.assertIn("passeggeri", ctx.exception.message.lower())

    def test_invalid_passenger_count_raises_error(self):
        for bad_pax in [0, -1, "abc"]:
            with self.assertRaises(TravelError) as ctx:
                TravelRequest.from_params({
                    "origin": "MXP",
                    "destination": "JFK",
                    "departure_date": "2026-11-01",
                    "passengers": bad_pax,
                })
            self.assertEqual(ctx.exception.code, "INVALID_PASSENGER_COUNT")

    def test_valid_parameters_produce_travel_request(self):
        req = TravelRequest.from_params({
            "origin": "mxp",
            "destination": "jfk",
            "departure_date": "2026-11-01",
            "return_date": "2026-11-15",
            "passengers": 2,
            "cabin_class": "economy",
        })
        self.assertEqual(req.origin, "MXP")
        self.assertEqual(req.destination, "JFK")
        self.assertEqual(req.departure_date, "2026-11-01")
        self.assertEqual(req.return_date, "2026-11-15")
        self.assertEqual(req.passengers, 2)
        self.assertEqual(req.cabin_class, "economy")


class DuffelNormalizationTests(unittest.TestCase):
    def setUp(self):
        # Sample realistic Duffel API v2 response payload
        self.sample_duffel_data = {
            "data": {
                "offers": [
                    {
                        "id": "off_0000A123",
                        "total_amount": "845.50",
                        "total_currency": "EUR",
                        "slices": [
                            {
                                "origin_type": "airport",
                                "origin": {"iata_code": "MXP"},
                                "destination_type": "airport",
                                "destination": {"iata_code": "JFK"},
                                "duration": "PT9H15M",
                                "segments": [
                                    {
                                        "origin": {"iata_code": "MXP"},
                                        "destination": {"iata_code": "JFK"},
                                        "departing_at": "2026-11-01T10:30:00",
                                        "arriving_at": "2026-11-01T13:45:00",
                                        "marketing_carrier": {"name": "Emirates", "iata_code": "EK"},
                                        "operating_carrier": {"name": "Emirates"},
                                        "marketing_carrier_flight_number": "205",
                                        "duration": "PT9H15M",
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        }

    def test_normalize_duffel_offer(self):
        offers, discarded = DuffelProvider.normalize_response(self.sample_duffel_data, passengers_count=2)
        self.assertEqual(discarded, 0)
        self.assertEqual(len(offers), 1)
        offer = offers[0]
        self.assertEqual(offer.offer_id, "off_0000A123")
        self.assertEqual(offer.provider, "duffel")
        self.assertEqual(offer.total_amount, 845.50)
        self.assertEqual(offer.currency, "EUR")
        self.assertEqual(offer.price_per_passenger, 422.75)
        self.assertEqual(offer.passengers_count, 2)
        self.assertTrue(offer.observed_at)

        # Check slices and segments
        self.assertEqual(len(offer.slices), 1)
        sl = offer.slices[0]
        self.assertEqual(sl.origin, "MXP")
        self.assertEqual(sl.destination, "JFK")
        self.assertEqual(sl.duration, "PT9H15M")
        self.assertEqual(sl.stops_count, 0)
        self.assertEqual(len(sl.segments), 1)
        seg = sl.segments[0]
        self.assertEqual(seg.origin_airport, "MXP")
        self.assertEqual(seg.destination_airport, "JFK")
        self.assertEqual(seg.flight_number, "EK205")
        self.assertEqual(seg.marketing_carrier, "Emirates")

        # Unknown fields explicitly marked
        self.assertIn("cabin_baggage_weight", offer.unknown_fields)
        self.assertIn(
            "sconosciuto", offer.unknown_fields["cabin_baggage_weight"].lower()
        )
        self.assertIn("connection_risk_score", offer.unknown_fields)


class SerpApiNormalizationTests(unittest.TestCase):
    def setUp(self):
        # Risposta realistica SerpApi google_flights (prezzi interi, minuti)
        self.sample_serpapi_data = {
            "search_parameters": {"currency": "EUR"},
            "best_flights": [
                {
                    "flights": [
                        {
                            "departure_airport": {"id": "MXP", "time": "2026-11-01 10:10"},
                            "arrival_airport": {"id": "JFK", "time": "2026-11-01 13:45"},
                            "airline": "Emirates",
                            "flight_number": "EK 205",
                            "duration": 555,
                        }
                    ],
                    "layovers": [],
                    "total_duration": 555,
                    "price": 845,
                    "type": "One way",
                    "booking_token": "tok_abc",
                }
            ],
            "other_flights": [],
        }

    def test_normalize_serpapi_offer(self):
        offers, discarded = SerpApiProvider.normalize_response(
            self.sample_serpapi_data, passengers_count=1, currency="EUR"
        )
        self.assertEqual(discarded, 0)
        self.assertEqual(len(offers), 1)
        offer = offers[0]
        self.assertTrue(offer.offer_id.startswith("serpapi_"))
        self.assertEqual(offer.provider, "serpapi")
        self.assertEqual(offer.total_amount, 845.00)
        self.assertEqual(offer.currency, "EUR")
        self.assertEqual(offer.price_per_passenger, 845.00)
        self.assertEqual(offer.provider_token, "tok_abc")

        self.assertEqual(len(offer.slices), 1)
        sl = offer.slices[0]
        self.assertEqual(sl.origin, "MXP")
        self.assertEqual(sl.destination, "JFK")
        self.assertEqual(sl.duration, "PT9H15M")
        self.assertEqual(sl.stops_count, 0)
        seg = sl.segments[0]
        self.assertEqual(seg.flight_number, "EK205")
        self.assertEqual(seg.departing_at, "2026-11-01T10:10")
        self.assertEqual(seg.marketing_carrier, "Emirates")

        self.assertIn("booking_reliability", offer.unknown_fields)
        self.assertIn("price_precision", offer.unknown_fields)

    def test_serpapi_ids_deterministic_and_deduped(self):
        data = {
            "search_parameters": {"currency": "EUR"},
            "best_flights": self.sample_serpapi_data["best_flights"],
            "other_flights": self.sample_serpapi_data["best_flights"],
        }
        offers, discarded = SerpApiProvider.normalize_response(data, passengers_count=1, currency="EUR")
        # stesso itinerario in entrambe le liste -> deduplicato
        self.assertEqual(len(offers), 1)
        self.assertEqual(discarded, 0)

    def test_serpapi_currency_undeterminable_raises(self):
        with self.assertRaises(TravelError) as ctx:
            SerpApiProvider.normalize_response({"best_flights": []}, passengers_count=1)
        self.assertEqual(ctx.exception.code, "PROVIDER_INVALID_RESPONSE")


class TravelManagerAndCredentialsTests(unittest.TestCase):
    def test_fails_closed_without_credentials(self):
        with patch.dict(os.environ, {}, clear=True):
            mgr = TravelManager()
            with self.assertRaises(TravelError) as ctx:
                mgr.search_flights({
                    "origin": "MXP",
                    "destination": "JFK",
                    "departure_date": "2026-11-01",
                    "passengers": 1,
                })
            self.assertEqual(ctx.exception.code, "PROVIDER_NOT_CONFIGURED")
            self.assertIn("SERPAPI_API_KEY", ctx.exception.message)
            self.assertIn("DUFFEL_ACCESS_TOKEN", ctx.exception.message)

    def test_selects_duffel_when_token_available(self):
        with patch.dict(os.environ, {"DUFFEL_ACCESS_TOKEN": "duffel_test_xyz"}):
            mgr = TravelManager()
            mock_response = {
                "data": {
                    "offers": [
                        {
                            "id": "off_test1",
                            "total_amount": "500.00",
                            "total_currency": "EUR",
                            "slices": [
                                {
                                    "origin": {"iata_code": "MXP"},
                                    "destination": {"iata_code": "JFK"},
                                    "duration": "PT9H15M",
                                    "segments": [
                                        {
                                            "origin": {"iata_code": "MXP"},
                                            "destination": {"iata_code": "JFK"},
                                            "departing_at": "2026-11-01T10:30:00",
                                            "arriving_at": "2026-11-01T13:45:00",
                                            "marketing_carrier": {"name": "Neos", "iata_code": "NO"},
                                            "operating_carrier": {"name": "Neos"},
                                            "marketing_carrier_flight_number": "101",
                                            "duration": "PT9H15M",
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                }
            }
            with patch("urllib.request.urlopen") as mock_url:
                mock_resp = MagicMock()
                mock_resp.read.return_value = json.dumps(mock_response).encode("utf-8")
                mock_url.return_value.__enter__.return_value = mock_resp

                res = mgr.search_flights({
                    "origin": "MXP",
                    "destination": "JFK",
                    "departure_date": "2026-11-01",
                    "passengers": 1,
                })

                self.assertEqual(res["provider"], "duffel")
                self.assertEqual(res["offers_count"], 1)
                self.assertEqual(res["discarded_count"], 0)
                self.assertEqual(res["offers"][0]["total_amount"], 500.00)

    def test_selects_serpapi_when_forced(self):
        env = {"TRAVEL_PROVIDER": "serpapi", "SERPAPI_API_KEY": "key123"}
        with patch.dict(os.environ, env):
            mgr = TravelManager()
            search_response = {
                "search_parameters": {"currency": "EUR"},
                "best_flights": [
                    {
                        "flights": [
                            {
                                "departure_airport": {"id": "FCO", "time": "2026-11-10 07:00"},
                                "arrival_airport": {"id": "HND", "time": "2026-11-10 09:00"},
                                "airline": "Air France",
                                "flight_number": "AF 1105",
                                "duration": 120,
                            }
                        ],
                        "total_duration": 120,
                        "price": 600,
                        "type": "One way",
                    }
                ],
            }
            with patch("urllib.request.urlopen") as mock_url:
                resp = MagicMock()
                resp.read.return_value = json.dumps(search_response).encode("utf-8")
                mock_url.return_value.__enter__.return_value = resp

                res = mgr.search_flights({
                    "origin": "FCO",
                    "destination": "HND",
                    "departure_date": "2026-11-10",
                    "passengers": 2,
                })

                self.assertEqual(res["provider"], "serpapi")
                self.assertEqual(res["offers_count"], 1)
                self.assertEqual(res["discarded_count"], 0)
                self.assertEqual(res["offers"][0]["price_per_passenger"], 300.00)

    def test_invalid_travel_provider_rejected(self):
        with patch.dict(os.environ, {"TRAVEL_PROVIDER": "kiwi"}):
            mgr = TravelManager()
            with self.assertRaises(TravelError) as ctx:
                mgr.search_flights({
                    "origin": "MXP", "destination": "JFK",
                    "departure_date": "2026-11-01", "passengers": 1,
                })
            self.assertEqual(ctx.exception.code, "INVALID_CONFIGURATION")


class FailClosedNormalizationTests(unittest.TestCase):
    """Offerte malformate scartate, mai inventate; conteggio scarti visibile."""

    def test_malformed_offers_discarded_with_count(self):
        data = {
            "data": {
                "offers": [
                    {"id": "bad1", "total_amount": "0.00", "total_currency": "EUR", "slices": []},
                    {"id": "bad2", "total_amount": "500.00", "total_currency": "EURO", "slices": []},
                    {"id": "bad3", "total_amount": "abc", "total_currency": "EUR", "slices": []},
                ]
            }
        }
        offers, discarded = DuffelProvider.normalize_response(data, passengers_count=1)
        self.assertEqual(offers, [])
        self.assertEqual(discarded, 3)

    def test_mixed_valid_and_invalid_counts_discarded(self):
        data = {
            "search_parameters": {"currency": "EUR"},
            "best_flights": [
                {
                    "flights": [
                        {
                            "departure_airport": {"id": "FCO", "time": "2026-11-10 07:00"},
                            "arrival_airport": {"id": "CDG", "time": "2026-11-10 09:00"},
                            "airline": "Air France",
                            "flight_number": "AF 1105",
                            "duration": 120,
                        }
                    ],
                    "total_duration": 120,
                    "price": 600,
                    "type": "One way",
                },
                {"flights": [], "price": -10, "type": "One way"},
            ],
        }
        offers, discarded = SerpApiProvider.normalize_response(data, passengers_count=1, currency="EUR")
        self.assertEqual(len(offers), 1)
        self.assertEqual(discarded, 1)


class ValidationStrictnessTests(unittest.TestCase):
    def test_same_origin_destination_rejected(self):
        with self.assertRaises(TravelError) as ctx:
            TravelRequest.from_params({
                "origin": "MXP", "destination": "mxp",
                "departure_date": "2026-11-01", "passengers": 1,
            })
        self.assertEqual(ctx.exception.code, "INVALID_ROUTE")

    def test_invalid_iata_rejected(self):
        for bad in ["MX", "MXP1", "Milano"]:
            with self.assertRaises(TravelError) as ctx:
                TravelRequest.from_params({
                    "origin": bad, "destination": "JFK",
                    "departure_date": "2026-11-01", "passengers": 1,
                })
            self.assertEqual(ctx.exception.code, "INVALID_AIRPORT_CODE")

    def test_nonexistent_date_rejected(self):
        with self.assertRaises(TravelError) as ctx:
            TravelRequest.from_params({
                "origin": "MXP", "destination": "JFK",
                "departure_date": "2026-02-30", "passengers": 1,
            })
        self.assertEqual(ctx.exception.code, "INVALID_DATE_FORMAT")

    def test_compact_date_format_rejected(self):
        with self.assertRaises(TravelError) as ctx:
            TravelRequest.from_params({
                "origin": "MXP", "destination": "JFK",
                "departure_date": "20261101", "passengers": 1,
            })
        self.assertEqual(ctx.exception.code, "INVALID_DATE_FORMAT")

    def test_bool_passengers_rejected(self):
        with self.assertRaises(TravelError) as ctx:
            TravelRequest.from_params({
                "origin": "MXP", "destination": "JFK",
                "departure_date": "2026-11-01", "passengers": True,
            })
        self.assertEqual(ctx.exception.code, "INVALID_PASSENGER_COUNT")

    def test_unknown_cabin_rejected(self):
        with self.assertRaises(TravelError) as ctx:
            TravelRequest.from_params({
                "origin": "MXP", "destination": "JFK",
                "departure_date": "2026-11-01", "passengers": 1,
                "cabin_class": "luxury",
            })
        self.assertEqual(ctx.exception.code, "INVALID_CABIN_CLASS")

    def test_float_passengers_rejected(self):
        with self.assertRaises(TravelError) as ctx:
            TravelRequest.from_params({
                "origin": "MXP", "destination": "JFK",
                "departure_date": "2026-11-01", "passengers": 2.5,
            })
        self.assertEqual(ctx.exception.code, "INVALID_PASSENGER_COUNT")

    def test_cache_db_permissions(self):
        import stat
        import tempfile
        from jarvis_hermes.travel import TravelCacheAndBudget
        db_file = os.path.join(tempfile.mkdtemp(), "test_perm.sqlite3")
        TravelCacheAndBudget(db_path=db_file)
        mode = stat.S_IMODE(os.stat(db_file).st_mode)
        self.assertEqual(mode, 0o600)


class ErrorClassificationTests(unittest.TestCase):
    def test_no_credential_leak_in_http_errors(self):
        import io
        with patch("urllib.request.urlopen") as mock_url:
            err_body = json.dumps({"errors": [{"message": "Invalid API key"}]}).encode("utf-8")
            mock_url.side_effect = urllib.error.HTTPError(
                "https://api.duffel.com/air/offer_requests?return_offers=true&secret=x",
                401, "Unauthorized", {}, io.BytesIO(err_body),
            )
            with patch.dict(os.environ, {"DUFFEL_ACCESS_TOKEN": "sekret-token"}):
                mgr = TravelManager()
                with self.assertRaises(TravelError) as ctx:
                    mgr.search_flights({
                        "origin": "MXP", "destination": "JFK",
                        "departure_date": "2026-11-01", "passengers": 1,
                    })
        self.assertEqual(ctx.exception.code, "PROVIDER_AUTH_ERROR")
        self.assertFalse(ctx.exception.retryable)
        self.assertNotIn("sekret-token", ctx.exception.message)
        self.assertNotIn("secret=x", ctx.exception.message)

    def test_429_is_retryable(self):
        import io
        with patch("urllib.request.urlopen") as mock_url:
            err_body = json.dumps({"errors": [{"message": "Rate limited"}]}).encode("utf-8")
            mock_url.side_effect = urllib.error.HTTPError(
                "https://example.com", 429, "Too Many Requests", {}, io.BytesIO(err_body),
            )
            with patch.dict(os.environ, {"DUFFEL_ACCESS_TOKEN": "tok"}):
                mgr = TravelManager()
                with self.assertRaises(TravelError) as ctx:
                    mgr.search_flights({
                        "origin": "MXP", "destination": "JFK",
                        "departure_date": "2026-11-01", "passengers": 1,
                    })
        self.assertTrue(ctx.exception.retryable)

    def test_401_is_auth_error_not_retryable(self):
        import io
        with patch("urllib.request.urlopen") as mock_url:
            err_body = json.dumps({"error": "Invalid API key."}).encode("utf-8")
            mock_url.side_effect = urllib.error.HTTPError(
                "https://serpapi.com/search", 401, "Unauthorized", {}, io.BytesIO(err_body),
            )
            with patch.dict(os.environ, {"SERPAPI_API_KEY": "bad-key", "TRAVEL_PROVIDER": "serpapi"}):
                mgr = TravelManager()
                with self.assertRaises(TravelError) as ctx:
                    mgr.search_flights({
                        "origin": "MXP", "destination": "JFK",
                        "departure_date": "2026-11-01", "passengers": 1,
                    })
        self.assertEqual(ctx.exception.code, "PROVIDER_AUTH_ERROR")
        self.assertFalse(ctx.exception.retryable)
        self.assertNotIn("bad-key", ctx.exception.message)


if __name__ == "__main__":
    unittest.main()


class _ComboStubProvider:
    """Finto provider: 1 offerta da 100+10€/giorno di scostamento, traccia le chiamate."""

    name = "stub"
    environment = "production"

    def __init__(self):
        self.calls: list[tuple[str, str | None]] = []

    def search_offers(self, request):
        from jarvis_hermes.travel import Offer
        self.calls.append((request.departure_date, request.return_date))
        dep_day = int(request.departure_date[-2:])
        amount = float(100 + 10 * (dep_day - 10))
        return ([Offer(
            offer_id=f"stub-{request.departure_date}",
            provider="stub",
            observed_at="2026-01-01T00:00:00+00:00",
            total_amount=amount,
            currency="EUR",
            price_per_passenger=amount,
            passengers_count=1,
            slices=[],
        )], 0)


class FlexibleSearchTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from jarvis_hermes.travel import TravelCacheAndBudget
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = TravelCacheAndBudget(
            db_path=os.path.join(self.tmp.name, "t.sqlite3"),
            daily_limit=1000, monthly_limit=1000, ttl_seconds=3600,
        )

    def _params(self, **kw):
        base = {
            "origin": "FCO", "destination": "DPS",
            "departure_date": "2026-10-10", "return_date": "2026-10-20",
            "passengers": 1, "departure_window": 1, "stay_extra": 1,
            "max_searches": 100,
        }
        base.update(kw)
        return base

    def test_valid_combinations_count_and_order(self):
        from jarvis_hermes.travel import FlexibleTravelRequest
        flex = FlexibleTravelRequest.from_params(self._params())
        combos = flex.valid_combinations()
        # 3 partenze x 2 ritorni, tutte valide (ritorno >= partenza)
        self.assertEqual(len(combos), 6)
        self.assertEqual(combos[0], ("2026-10-09", "2026-10-20"))

    def test_plan_limited_by_max_searches_declares_partial(self):
        from jarvis_hermes.travel import search_flexible_flights
        prov = _ComboStubProvider()
        res = search_flexible_flights(self._params(max_searches=2), provider=prov, cache=self.cache)
        self.assertEqual(len(prov.calls), 2)
        self.assertTrue(res["coverage"]["partial"])
        self.assertIn("max_searches=2", res["coverage"]["partial_reason"])
        self.assertEqual(res["coverage"]["valid_combinations"], 6)
        self.assertEqual(res["offers_count"], 2)
        # La coppia base (distanza 0) deve essere la prima ad essere pianificata ed eseguita
        self.assertEqual(prov.calls[0], ("2026-10-10", "2026-10-20"))

    def test_plan_prioritizes_requested_dates_over_chronological(self):
        from jarvis_hermes.travel import search_flexible_flights
        prov = _ComboStubProvider()
        # Con departure_window=5 e stay_extra=3 ci sono 44 combinazioni.
        # Con max_searches=1, senza ordinamento per distanza verrebbe cercato 2026-10-05 anziché la base.
        res = search_flexible_flights(
            self._params(departure_window=5, stay_extra=3, max_searches=1),
            provider=prov,
            cache=self.cache,
        )
        self.assertEqual(len(prov.calls), 1)
        self.assertEqual(prov.calls[0], ("2026-10-10", "2026-10-20"))

    def test_transient_error_mid_run_keeps_collected_offers(self):
        from jarvis_hermes.travel import TravelError, search_flexible_flights

        class _TransientProvider(_ComboStubProvider):
            def search_offers(self, request):
                if len(self.calls) >= 1:
                    raise TravelError("PROVIDER_UNAVAILABLE", "timeout di rete", retryable=True)
                return super().search_offers(request)

        prov = _TransientProvider()
        res = search_flexible_flights(self._params(max_searches=3), provider=prov, cache=self.cache)
        self.assertEqual(res["coverage"]["searched_live"], 1)
        self.assertTrue(res["coverage"]["partial"])
        self.assertIn("PROVIDER_UNAVAILABLE", res["coverage"]["partial_reason"])
        self.assertEqual(res["offers_count"], 1)

    def test_second_run_is_full_cache_hit_no_new_calls(self):
        from jarvis_hermes.travel import search_flexible_flights
        prov = _ComboStubProvider()
        res1 = search_flexible_flights(self._params(max_searches=6), provider=prov, cache=self.cache)
        self.assertEqual(res1["coverage"]["searched_live"], 6)
        prov.calls.clear()
        res2 = search_flexible_flights(self._params(max_searches=6), provider=prov, cache=self.cache)
        self.assertEqual(prov.calls, [])
        self.assertEqual(res2["coverage"]["from_cache"], 6)
        self.assertEqual(res2["offers_count"], res1["offers_count"])

    def test_expired_cache_recounts_as_live(self):
        import time
        from jarvis_hermes.travel import TravelCacheAndBudget, search_flexible_flights
        short = TravelCacheAndBudget(
            db_path=os.path.join(self.tmp.name, "short.sqlite3"),
            daily_limit=1000, monthly_limit=1000, ttl_seconds=1,
        )
        prov = _ComboStubProvider()
        search_flexible_flights(self._params(max_searches=1), provider=prov, cache=short)
        time.sleep(1.2)
        res = search_flexible_flights(self._params(max_searches=1), provider=prov, cache=short)
        self.assertEqual(res["coverage"]["cache_expired"], 1)
        self.assertEqual(res["coverage"]["searched_live"], 1)

    def test_budget_exhaustion_gives_partial_coverage_not_error(self):
        from jarvis_hermes.travel import TravelCacheAndBudget, search_flexible_flights
        tight = TravelCacheAndBudget(
            db_path=os.path.join(self.tmp.name, "tight.sqlite3"),
            daily_limit=2, monthly_limit=1000, ttl_seconds=3600,
        )
        prov = _ComboStubProvider()
        res = search_flexible_flights(self._params(max_searches=6), provider=prov, cache=tight)
        self.assertEqual(res["coverage"]["searched_live"], 2)
        self.assertTrue(res["coverage"]["partial"])
        self.assertIn("Budget", res["coverage"]["partial_reason"])
        self.assertEqual(res["offers_count"], 2)

    def test_provider_quota_exhaustion_gives_partial_coverage(self):
        from jarvis_hermes.travel import TravelError, search_flexible_flights

        class _QuotaProvider(_ComboStubProvider):
            def search_offers(self, request):
                if len(self.calls) >= 1:
                    raise TravelError("PROVIDER_QUOTA_EXHAUSTED", "quota finita", retryable=False)
                return super().search_offers(request)

        prov = _QuotaProvider()
        res = search_flexible_flights(self._params(max_searches=6), provider=prov, cache=self.cache)
        self.assertEqual(res["coverage"]["searched_live"], 1)
        self.assertTrue(res["coverage"]["partial"])
        self.assertIn("Quota provider", res["coverage"]["partial_reason"])

    def test_stay_extra_without_return_rejected(self):
        from jarvis_hermes.travel import FlexibleTravelRequest, TravelError
        with self.assertRaises(TravelError) as ctx:
            FlexibleTravelRequest.from_params({
                "origin": "FCO", "destination": "DPS",
                "departure_date": "2026-10-10", "passengers": 1,
                "stay_extra": 2,
            })
        self.assertEqual(ctx.exception.code, "INVALID_FLEXIBLE_PARAMS")

    def test_one_way_expands_departures_only(self):
        from jarvis_hermes.travel import FlexibleTravelRequest
        flex = FlexibleTravelRequest.from_params({
            "origin": "FCO", "destination": "DPS",
            "departure_date": "2026-10-10", "passengers": 1,
            "departure_window": 2,
        })
        self.assertEqual(len(flex.valid_combinations()), 5)


class SerpApiFallbackKeyTests(unittest.TestCase):
    def _quota_error(self):
        import io
        body = json.dumps({"error": "You have run out of searches for this month."}).encode()
        return urllib.error.HTTPError("https://serpapi.com/search", 429, "Too Many Requests", {}, io.BytesIO(body))

    def _ok_response(self, price=500):
        resp = MagicMock()
        resp.read.return_value = json.dumps({
            "search_parameters": {"currency": "EUR"},
            "best_flights": [{
                "flights": [{
                    "departure_airport": {"id": "FCO", "time": "2026-11-10 07:00"},
                    "arrival_airport": {"id": "DPS", "time": "2026-11-11 09:00"},
                    "airline": "Test Air", "flight_number": "TA 1", "duration": 600,
                }],
                "total_duration": 600, "price": price, "type": "Round trip",
            }],
        }).encode("utf-8")
        ctx = MagicMock()
        ctx.__enter__.return_value = resp
        return ctx

    def test_fallback_key_used_on_quota_exhausted(self):
        with patch.dict(os.environ, {"SERPAPI_API_KEY": "key-one", "SERPAPI_API_KEY2": "key-two", "TRAVEL_PROVIDER": "serpapi"}):
            mgr = TravelManager()
            with patch("urllib.request.urlopen") as mock_url:
                mock_url.side_effect = [self._quota_error(), self._ok_response()]
                res = mgr.search_flights({
                    "origin": "FCO", "destination": "DPS",
                    "departure_date": "2026-11-10", "return_date": "2026-11-20", "passengers": 1,
                })
            self.assertEqual(mock_url.call_count, 2)
            self.assertEqual(res["offers_count"], 1)
            # Le chiavi non devono comparire da nessuna parte
            dumped = json.dumps(res)
            self.assertNotIn("key-one", dumped)
            self.assertNotIn("key-two", dumped)

    def test_no_fallback_without_second_key(self):
        with patch.dict(os.environ, {"SERPAPI_API_KEY": "key-one", "TRAVEL_PROVIDER": "serpapi"}):
            mgr = TravelManager()
            with patch("urllib.request.urlopen") as mock_url:
                mock_url.side_effect = self._quota_error()
                with self.assertRaises(TravelError) as ctx:
                    mgr.search_flights({
                        "origin": "FCO", "destination": "DPS",
                        "departure_date": "2026-11-10", "passengers": 1,
                    })
            self.assertEqual(ctx.exception.code, "PROVIDER_QUOTA_EXHAUSTED")
            self.assertEqual(mock_url.call_count, 1)

    def test_same_key_twice_no_retry(self):
        env = {"SERPAPI_API_KEY": "same", "SERPAPI_API_KEY2": "same", "TRAVEL_PROVIDER": "serpapi"}
        with patch.dict(os.environ, env):
            prov = SerpApiProvider()
            self.assertEqual(prov.api_key_fallback, "")

    def test_only_key2_promoted_to_primary(self):
        with patch.dict(os.environ, {"SERPAPI_API_KEY": "", "SERPAPI_API_KEY2": "only-second", "TRAVEL_PROVIDER": "serpapi"}):
            mgr = TravelManager()
            with patch("urllib.request.urlopen") as mock_url:
                mock_url.return_value = self._ok_response()
                res = mgr.search_flights({
                    "origin": "FCO", "destination": "DPS",
                    "departure_date": "2026-11-10", "passengers": 1,
                })
                self.assertEqual(res["offers_count"], 1)
                self.assertEqual(mock_url.call_count, 1)

    def test_fallback_on_quota_in_json_200(self):
        quota_body = MagicMock()
        quota_body.read.return_value = json.dumps({
            "error": "You have run out of searches for this month.",
            "search_parameters": {"currency": "EUR"},
        }).encode("utf-8")
        quota_ctx = MagicMock()
        quota_ctx.__enter__.return_value = quota_body
        with patch.dict(os.environ, {"SERPAPI_API_KEY": "key-one", "SERPAPI_API_KEY2": "key-two", "TRAVEL_PROVIDER": "serpapi"}):
            mgr = TravelManager()
            with patch("urllib.request.urlopen") as mock_url:
                mock_url.side_effect = [quota_ctx, self._ok_response()]
                res = mgr.search_flights({
                    "origin": "FCO", "destination": "DPS",
                    "departure_date": "2026-11-10", "passengers": 1,
                })
            self.assertEqual(mock_url.call_count, 2)
            self.assertEqual(res["offers_count"], 1)
