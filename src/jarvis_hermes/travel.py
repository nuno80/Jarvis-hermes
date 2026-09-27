"""Travel provider integration and flight offer normalization.

Implements requirements from J13, D12, E1, E2 and ADR 0008.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from typing import Any, Protocol
import urllib.error
import urllib.parse
import urllib.request


class TravelError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass
class Segment:
    origin_airport: str
    destination_airport: str
    departing_at: str
    arriving_at: str
    marketing_carrier: str
    operating_carrier: str | None = None
    flight_number: str | None = None
    duration: str | None = None


@dataclass
class Slice:
    origin: str
    destination: str
    departure_date: str
    duration: str | None
    stops_count: int
    segments: list[Segment] = field(default_factory=list)


@dataclass
class Offer:
    offer_id: str
    provider: str
    observed_at: str
    total_amount: float
    currency: str
    price_per_passenger: float
    passengers_count: int
    slices: list[Slice]
    unknown_fields: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "offer_id": self.offer_id,
            "provider": self.provider,
            "observed_at": self.observed_at,
            "total_amount": round(self.total_amount, 2),
            "currency": self.currency,
            "price_per_passenger": round(self.price_per_passenger, 2),
            "passengers_count": self.passengers_count,
            "slices": [
                {
                    "origin": s.origin,
                    "destination": s.destination,
                    "departure_date": s.departure_date,
                    "duration": s.duration,
                    "stops_count": s.stops_count,
                    "segments": [
                        {
                            "origin_airport": seg.origin_airport,
                            "destination_airport": seg.destination_airport,
                            "departing_at": seg.departing_at,
                            "arriving_at": seg.arriving_at,
                            "marketing_carrier": seg.marketing_carrier,
                            "operating_carrier": seg.operating_carrier,
                            "flight_number": seg.flight_number,
                            "duration": seg.duration,
                        }
                        for seg in s.segments
                    ],
                }
                for s in self.slices
            ],
            "unknown_fields": self.unknown_fields,
        }


@dataclass
class TravelRequest:
    origin: str
    destination: str
    departure_date: str
    passengers: int
    return_date: str | None = None
    cabin_class: str = "economy"

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> "TravelRequest":
        origin = params.get("origin")
        if not origin or not str(origin).strip():
            raise TravelError(
                "MISSING_PARAMETER",
                "Aeroporto di origine richiesto (es. MXP, FCO, BRU). Non può essere dedotto tacitamente.",
            )

        destination = params.get("destination")
        if not destination or not str(destination).strip():
            raise TravelError("MISSING_PARAMETER", "Destinazione richiesta (es. DPS, HND, JFK).")

        departure_date = params.get("departure_date")
        if not departure_date or not str(departure_date).strip():
            raise TravelError(
                "MISSING_PARAMETER",
                "Data di partenza con anno richiesta nel formato YYYY-MM-DD. L'anno non può essere dedotto tacitamente.",
            )

        # Validate date format (YYYY-MM-DD)
        parts = str(departure_date).strip().split("-")
        if len(parts) != 3 or len(parts[0]) != 4:
            raise TravelError(
                "INVALID_DATE_FORMAT",
                f"Formato data non valido '{departure_date}'. Richiesto YYYY-MM-DD con anno esplicito a 4 cifre.",
            )

        passengers_raw = params.get("passengers")
        if passengers_raw is None:
            raise TravelError(
                "MISSING_PARAMETER",
                "Numero di passeggeri richiesto. Non può essere dedotto tacitamente.",
            )
        try:
            passengers = int(passengers_raw)
            if passengers <= 0:
                raise ValueError()
        except (ValueError, TypeError):
            raise TravelError(
                "INVALID_PASSENGER_COUNT",
                f"Numero passeggeri non valido '{passengers_raw}'. Richiesto intero positivo maggiore di 0.",
            )

        return_date = params.get("return_date")
        if return_date:
            rparts = str(return_date).strip().split("-")
            if len(rparts) != 3 or len(rparts[0]) != 4:
                raise TravelError(
                    "INVALID_DATE_FORMAT",
                    f"Formato data ritorno non valido '{return_date}'. Richiesto YYYY-MM-DD con anno esplicito a 4 cifre.",
                )

        return cls(
            origin=str(origin).strip().upper(),
            destination=str(destination).strip().upper(),
            departure_date=str(departure_date).strip(),
            passengers=passengers,
            return_date=str(return_date).strip() if return_date else None,
            cabin_class=str(params.get("cabin_class", "economy")).strip().lower(),
        )


class TravelProvider(Protocol):
    @property
    def name(self) -> str:
        ...

    def search_offers(self, request: TravelRequest) -> list[Offer]:
        ...


class DuffelProvider:
    """Duffel Flights API v2 provider."""

    API_BASE = "https://api.duffel.com/air"

    def __init__(self, access_token: str | None = None):
        self.token = access_token or os.environ.get("DUFFEL_ACCESS_TOKEN", "").strip()

    @property
    def name(self) -> str:
        return "duffel"

    def search_offers(self, request: TravelRequest) -> list[Offer]:
        if not self.token:
            raise TravelError(
                "CREDENTIALS_REQUIRED",
                "Nessun token Duffel configurato. Configura la variabile d'ambiente DUFFEL_ACCESS_TOKEN o inserisci le credenziali.",
            )

        slices_payload = [
            {
                "origin": request.origin,
                "destination": request.destination,
                "departure_date": request.departure_date,
            }
        ]
        if request.return_date:
            slices_payload.append(
                {
                    "origin": request.destination,
                    "destination": request.origin,
                    "departure_date": request.return_date,
                }
            )

        body = {
            "data": {
                "slices": slices_payload,
                "passengers": [{"type": "adult"} for _ in range(request.passengers)],
                "cabin_class": request.cabin_class,
            }
        }

        req = urllib.request.Request(
            f"{self.API_BASE}/offer_requests?return_offers=true",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Duffel-Version": "v2",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "Jarvis-Hermes/1.0",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw_data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            try:
                err_json = json.loads(err_body)
                msg = err_json.get("errors", [{}])[0].get("message", exc.reason)
            except Exception:
                msg = exc.reason
            raise TravelError("PROVIDER_API_ERROR", f"Duffel API error ({exc.code}): {msg}") from exc
        except Exception as exc:
            raise TravelError("PROVIDER_UNAVAILABLE", f"Impossibile contattare Duffel API: {exc}", retryable=True) from exc

        return self.normalize_response(raw_data, request.passengers)

    @classmethod
    def normalize_response(cls, data: dict[str, Any], passengers_count: int) -> list[Offer]:
        observed_at = datetime.now(timezone.utc).isoformat()
        payload = data.get("data", {})
        offers_raw = payload.get("offers", [])
        if not offers_raw and "total_amount" in payload:
            offers_raw = [payload]

        normalized: list[Offer] = []
        for raw in offers_raw:
            try:
                total_amount = float(raw.get("total_amount", 0.0))
            except (ValueError, TypeError):
                total_amount = 0.0
            currency = raw.get("total_currency", "EUR")
            price_per_pax = total_amount / max(1, passengers_count)

            slices_list: list[Slice] = []
            for s_raw in raw.get("slices", []):
                origin = s_raw.get("origin", {}).get("iata_code") or s_raw.get("origin_type") or "UNKNOWN"
                destination = s_raw.get("destination", {}).get("iata_code") or s_raw.get("destination_type") or "UNKNOWN"
                duration = s_raw.get("duration")

                segments_raw = s_raw.get("segments", [])
                segments_list: list[Segment] = []
                for seg_raw in segments_raw:
                    seg_origin = seg_raw.get("origin", {}).get("iata_code", "")
                    seg_dest = seg_raw.get("destination", {}).get("iata_code", "")
                    dep_at = seg_raw.get("departing_at", "")
                    arr_at = seg_raw.get("arriving_at", "")
                    carrier = seg_raw.get("marketing_carrier", {}).get("name") or seg_raw.get("marketing_carrier", {}).get("iata_code", "")
                    oper_carrier = seg_raw.get("operating_carrier", {}).get("name")
                    fl_num = seg_raw.get("marketing_carrier_flight_number")
                    seg_dur = seg_raw.get("duration")

                    segments_list.append(
                        Segment(
                            origin_airport=seg_origin,
                            destination_airport=seg_dest,
                            departing_at=dep_at,
                            arriving_at=arr_at,
                            marketing_carrier=carrier,
                            operating_carrier=oper_carrier,
                            flight_number=fl_num,
                            duration=seg_dur,
                        )
                    )

                stops = max(0, len(segments_list) - 1)
                dep_date = (
                    segments_list[0].departing_at[:10]
                    if segments_list and len(segments_list[0].departing_at) >= 10
                    else ""
                )
                slices_list.append(
                    Slice(
                        origin=origin,
                        destination=destination,
                        departure_date=dep_date,
                        duration=duration,
                        stops_count=stops,
                        segments=segments_list,
                    )
                )

            unknown_fields = {
                "cabin_baggage_weight": "unknown (not specified in Duffel offer response)",
                "connection_risk_score": "unknown (no deterministic risk model without live transfer gates)",
                "co2_emissions": "unknown (optional carbon offset not returned)",
            }

            normalized.append(
                Offer(
                    offer_id=str(raw.get("id", "")),
                    provider="duffel",
                    observed_at=observed_at,
                    total_amount=total_amount,
                    currency=currency,
                    price_per_passenger=price_per_pax,
                    passengers_count=passengers_count,
                    slices=slices_list,
                    unknown_fields=unknown_fields,
                )
            )

        return normalized


class AmadeusProvider:
    """Amadeus for Developers Flight Offers Search v2 provider."""

    AUTH_URL = "https://test.api.amadeus.com/v1/security/oauth2/token"
    SEARCH_URL = "https://test.api.amadeus.com/v2/shopping/flight-offers"

    def __init__(self, client_id: str | None = None, client_secret: str | None = None):
        self.client_id = client_id or os.environ.get("AMADEUS_CLIENT_ID", "").strip()
        self.client_secret = client_secret or os.environ.get("AMADEUS_CLIENT_SECRET", "").strip()

    @property
    def name(self) -> str:
        return "amadeus"

    def _get_token(self) -> str:
        if not self.client_id or not self.client_secret:
            raise TravelError(
                "CREDENTIALS_REQUIRED",
                "Credenziali Amadeus non configurate. Richieste AMADEUS_CLIENT_ID e AMADEUS_CLIENT_SECRET.",
            )

        data = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            self.AUTH_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                return payload.get("access_token", "")
        except Exception as exc:
            raise TravelError("PROVIDER_AUTH_ERROR", f"Autenticazione Amadeus fallita: {exc}") from exc

    def search_offers(self, request: TravelRequest) -> list[Offer]:
        token = self._get_token()

        params = {
            "originLocationCode": request.origin,
            "destinationLocationCode": request.destination,
            "departureDate": request.departure_date,
            "adults": str(request.passengers),
            "currencyCode": "EUR",
            "max": "10",
        }
        if request.return_date:
            params["returnDate"] = request.return_date

        url = f"{self.SEARCH_URL}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "Jarvis-Hermes/1.0",
            },
            method="GET",
        )

        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw_data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise TravelError("PROVIDER_API_ERROR", f"Amadeus API error ({exc.code}): {exc.reason}") from exc
        except Exception as exc:
            raise TravelError("PROVIDER_UNAVAILABLE", f"Impossibile contattare Amadeus API: {exc}", retryable=True) from exc

        return self.normalize_response(raw_data, request.passengers)

    @classmethod
    def normalize_response(cls, data: dict[str, Any], passengers_count: int) -> list[Offer]:
        observed_at = datetime.now(timezone.utc).isoformat()
        offers_raw = data.get("data", [])
        normalized: list[Offer] = []

        for raw in offers_raw:
            try:
                total_amount = float(raw.get("price", {}).get("total", 0.0))
            except (ValueError, TypeError):
                total_amount = 0.0
            currency = raw.get("price", {}).get("currency", "EUR")
            price_per_pax = total_amount / max(1, passengers_count)

            slices_list: list[Slice] = []
            for itin in raw.get("itineraries", []):
                duration = itin.get("duration")
                segments_raw = itin.get("segments", [])
                segments_list: list[Segment] = []

                for seg_raw in segments_raw:
                    dep = seg_raw.get("departure", {})
                    arr = seg_raw.get("arrival", {})
                    carrier = seg_raw.get("carrierCode", "")
                    oper_carrier = seg_raw.get("operating", {}).get("carrierCode")
                    fl_num = f"{carrier}{seg_raw.get('number', '')}"
                    seg_dur = seg_raw.get("duration")

                    segments_list.append(
                        Segment(
                            origin_airport=dep.get("iataCode", ""),
                            destination_airport=arr.get("iataCode", ""),
                            departing_at=dep.get("at", ""),
                            arriving_at=arr.get("at", ""),
                            marketing_carrier=carrier,
                            operating_carrier=oper_carrier,
                            flight_number=fl_num,
                            duration=seg_dur,
                        )
                    )

                origin = segments_list[0].origin_airport if segments_list else "UNKNOWN"
                destination = segments_list[-1].destination_airport if segments_list else "UNKNOWN"
                dep_date = (
                    segments_list[0].departing_at[:10]
                    if segments_list and len(segments_list[0].departing_at) >= 10
                    else ""
                )
                stops = max(0, len(segments_list) - 1)

                slices_list.append(
                    Slice(
                        origin=origin,
                        destination=destination,
                        departure_date=dep_date,
                        duration=duration,
                        stops_count=stops,
                        segments=segments_list,
                    )
                )

            unknown_fields = {
                "cabin_baggage_weight": "unknown (not included in standard Amadeus GET response)",
                "connection_risk_score": "unknown (historical connection reliability not modeled)",
                "seat_availability_detail": "unknown (requires SeatMap Display API call)",
            }

            normalized.append(
                Offer(
                    offer_id=str(raw.get("id", "")),
                    provider="amadeus",
                    observed_at=observed_at,
                    total_amount=total_amount,
                    currency=currency,
                    price_per_passenger=price_per_pax,
                    passengers_count=passengers_count,
                    slices=slices_list,
                    unknown_fields=unknown_fields,
                )
            )

        return normalized


class TravelManager:
    """Orchestrates travel providers, ensuring credentials presence and clean errors."""

    def __init__(self, provider: TravelProvider | None = None):
        self._provider = provider

    def _resolve_provider(self) -> TravelProvider:
        if self._provider is not None:
            return self._provider

        # Check Duffel token first (preferred provider per ADR 0008)
        if os.environ.get("DUFFEL_ACCESS_TOKEN", "").strip():
            return DuffelProvider()

        # Check Amadeus
        if os.environ.get("AMADEUS_CLIENT_ID", "").strip() and os.environ.get("AMADEUS_CLIENT_SECRET", "").strip():
            return AmadeusProvider()

        # Fail closed: never return fake results
        raise TravelError(
            "PROVIDER_NOT_CONFIGURED",
            "Nessun provider viaggi configurato. Per eseguire ricerche reali imposta DUFFEL_ACCESS_TOKEN (consigliato per Duffel) oppure AMADEUS_CLIENT_ID e AMADEUS_CLIENT_SECRET (per Amadeus). Non verranno generati dati fittizi.",
        )

    def search_flights(self, params: dict[str, Any]) -> dict[str, Any]:
        """Validate input parameters and search for flight offers."""
        req = TravelRequest.from_params(params)
        provider = self._resolve_provider()
        offers = provider.search_offers(req)

        return {
            "query": {
                "origin": req.origin,
                "destination": req.destination,
                "departure_date": req.departure_date,
                "return_date": req.return_date,
                "passengers": req.passengers,
                "cabin_class": req.cabin_class,
            },
            "provider": provider.name,
            "offers_count": len(offers),
            "offers": [o.to_dict() for o in offers],
        }
