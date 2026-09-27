"""Unit tests for travel provider integration, validation, and offer normalization."""
from datetime import datetime
import json
import os
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

from jarvis_hermes.travel import (
    AmadeusProvider,
    DuffelProvider,
    Offer,
    Segment,
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
        offers = DuffelProvider.normalize_response(self.sample_duffel_data, passengers_count=2)
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
        self.assertEqual(seg.flight_number, "205")
        self.assertEqual(seg.marketing_carrier, "Emirates")

        # Unknown fields explicitly marked
        self.assertIn("cabin_baggage_weight", offer.unknown_fields)
        self.assertIn("unknown", offer.unknown_fields["cabin_baggage_weight"].lower())
        self.assertIn("connection_risk_score", offer.unknown_fields)


class AmadeusNormalizationTests(unittest.TestCase):
    def setUp(self):
        # Sample realistic Amadeus Flight Offers Search response
        self.sample_amadeus_data = {
            "data": [
                {
                    "id": "1",
                    "price": {"total": "720.00", "currency": "EUR"},
                    "itineraries": [
                        {
                            "duration": "PT11H30M",
                            "segments": [
                                {
                                    "departure": {"iataCode": "MXP", "at": "2026-11-01T07:00:00"},
                                    "arrival": {"iataCode": "CDG", "at": "2026-11-01T08:30:00"},
                                    "carrierCode": "AF",
                                    "number": "1234",
                                    "duration": "PT1H30M",
                                },
                                {
                                    "departure": {"iataCode": "CDG", "at": "2026-11-01T10:30:00"},
                                    "arrival": {"iataCode": "JFK", "at": "2026-11-01T13:30:00"},
                                    "carrierCode": "AF",
                                    "number": "022",
                                    "duration": "PT8H00M",
                                },
                            ],
                        }
                    ],
                }
            ]
        }

    def test_normalize_amadeus_offer(self):
        offers = AmadeusProvider.normalize_response(self.sample_amadeus_data, passengers_count=1)
        self.assertEqual(len(offers), 1)
        offer = offers[0]
        self.assertEqual(offer.offer_id, "1")
        self.assertEqual(offer.provider, "amadeus")
        self.assertEqual(offer.total_amount, 720.00)
        self.assertEqual(offer.currency, "EUR")
        self.assertEqual(offer.price_per_passenger, 720.00)

        # Multi-segment slice (1 stop)
        self.assertEqual(len(offer.slices), 1)
        sl = offer.slices[0]
        self.assertEqual(sl.origin, "MXP")
        self.assertEqual(sl.destination, "JFK")
        self.assertEqual(sl.stops_count, 1)
        self.assertEqual(len(sl.segments), 2)
        self.assertEqual(sl.segments[0].flight_number, "AF1234")
        self.assertEqual(sl.segments[1].flight_number, "AF022")

        # Unknown fields explicitly marked
        self.assertIn("cabin_baggage_weight", offer.unknown_fields)
        self.assertIn("seat_availability_detail", offer.unknown_fields)


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
            self.assertIn("DUFFEL_ACCESS_TOKEN", ctx.exception.message)
            self.assertIn("AMADEUS_CLIENT_ID", ctx.exception.message)

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
                            "slices": [],
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
                self.assertEqual(res["offers"][0]["total_amount"], 500.00)

    def test_selects_amadeus_when_credentials_available(self):
        env = {
            "DUFFEL_ACCESS_TOKEN": "",
            "AMADEUS_CLIENT_ID": "amadeus_id",
            "AMADEUS_CLIENT_SECRET": "amadeus_secret",
        }
        with patch.dict(os.environ, env):
            mgr = TravelManager()
            token_response = {"access_token": "mocked_jwt_token"}
            search_response = {
                "data": [
                    {
                        "id": "amadeus_offer_1",
                        "price": {"total": "600.00", "currency": "EUR"},
                        "itineraries": [],
                    }
                ]
            }

            with patch("urllib.request.urlopen") as mock_url:
                resp1 = MagicMock()
                resp1.read.return_value = json.dumps(token_response).encode("utf-8")
                resp2 = MagicMock()
                resp2.read.return_value = json.dumps(search_response).encode("utf-8")
                mock_url.return_value.__enter__.side_effect = [resp1, resp2]

                res = mgr.search_flights({
                    "origin": "FCO",
                    "destination": "HND",
                    "departure_date": "2026-10-10",
                    "passengers": 2,
                })

                self.assertEqual(res["provider"], "amadeus")
                self.assertEqual(res["offers_count"], 1)
                self.assertEqual(res["offers"][0]["price_per_passenger"], 300.00)


if __name__ == "__main__":
    unittest.main()
