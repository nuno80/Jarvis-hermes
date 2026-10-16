"""Integrazione provider di viaggio, normalizzazione offerte e ricerca flessibile.

Conforme ai requisiti J13, J14, D12, E1, E2 e ADR 0008 (come superata da ADR 0016).
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import logging
import os
import re
import socket
import sqlite3
import time
from pathlib import Path
from typing import Any, Protocol
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)


# =============================================================================
# BLOCCO 1 - COSTANTI DI CONFIGURAZIONE E GATEWAY HUBS
# =============================================================================
IATA_PATTERN = re.compile(r"^[A-Z]{3}$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

CABIN_CLASSES: tuple[str, ...] = ("economy", "premium_economy", "business", "first")

SERPAPI_TRAVEL_CLASS: dict[str, str] = {
    "economy": "1",
    "premium_economy": "2",
    "business": "3",
    "first": "4",
}

MAX_PASSENGERS = 9
MAX_OFFERS = 10
SEARCH_TIMEOUT_S = 30
SERPAPI_SEARCH_URL = "https://serpapi.com/search"

SERPAPI_DEFAULT_CURRENCY = "EUR"
SERPAPI_DEFAULT_GL = "it"
SERPAPI_DEFAULT_HL = "en"

QUOTA_EXHAUSTED_MARKERS = ("run out of searches", "out of searches")

CACHE_SCHEMA_VERSION = "v1"


# =============================================================================
# BLOCCO 2 - ERRORI
# =============================================================================
class TravelError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}


# =============================================================================
# BLOCCO 3 - MODELLO DATI
# =============================================================================
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
class Layover:
    airport: str
    duration: str | None
    duration_minutes: int | None
    overnight: bool | None
    airport_change: bool = False


@dataclass
class Slice:
    origin: str
    destination: str
    departure_date: str
    duration: str | None
    stops_count: int
    segments: list[Segment] = field(default_factory=list)
    layovers: list[Layover] = field(default_factory=list)
    has_airport_change: bool = False
    has_overnight_layover: bool = False
    is_homogenous_carrier: bool = True
    has_tight_connection: bool = False


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
    total_amount_raw: str = ""
    environment: str = "production"
    provider_token: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "offer_id": self.offer_id,
            "provider": self.provider,
            "environment": self.environment,
            "observed_at": self.observed_at,
            "total_amount": round(self.total_amount, 2),
            "total_amount_raw": self.total_amount_raw,
            "currency": self.currency,
            "price_per_passenger": round(self.price_per_passenger, 2),
            "passengers_count": self.passengers_count,
            "provider_token": self.provider_token,
            "slices": [
                {
                    "origin": s.origin,
                    "destination": s.destination,
                    "departure_date": s.departure_date,
                    "duration": s.duration,
                    "stops_count": s.stops_count,
                    "has_airport_change": s.has_airport_change,
                    "has_overnight_layover": s.has_overnight_layover,
                    "is_homogenous_carrier": s.is_homogenous_carrier,
                    "has_tight_connection": s.has_tight_connection,
                    "layovers": [
                        {
                            "airport": l.airport,
                            "duration": l.duration,
                            "duration_minutes": l.duration_minutes,
                            "overnight": l.overnight,
                            "airport_change": l.airport_change,
                        }
                        for l in s.layovers
                    ],
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


# =============================================================================
# BLOCCO 4 - VALIDAZIONE RIGIDA DELL'INPUT (Conforme E1 / ADR-0008)
# =============================================================================
def _parse_iso_date(value: Any, label: str) -> date:
    text = str(value).strip()
    if not DATE_PATTERN.match(text):
        raise TravelError(
            "INVALID_DATE_FORMAT",
            f"Formato {label} non valido '{text}'. Richiesto YYYY-MM-DD con anno esplicito a 4 cifre.",
        )
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise TravelError(
            "INVALID_DATE_FORMAT",
            f"La {label} '{text}' non è una data di calendario valida.",
        ) from None


def _require_iata(value: Any, label: str) -> str:
    code = str(value).strip().upper()
    if not IATA_PATTERN.match(code):
        raise TravelError(
            "INVALID_AIRPORT_CODE",
            f"{label} '{value}' non valida: richiesto un codice IATA di 3 lettere (es. MXP, FCO, JFK).",
        )
    return code


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
        origin_raw = params.get("origin")
        if not origin_raw or not str(origin_raw).strip():
            raise TravelError(
                "MISSING_PARAMETER",
                "Aeroporto di origine richiesto (es. MXP, FCO). Non può essere dedotto tacitamente.",
            )

        destination_raw = params.get("destination")
        if not destination_raw or not str(destination_raw).strip():
            raise TravelError("MISSING_PARAMETER", "Destinazione richiesta (es. DPS, JFK).")

        departure_raw = params.get("departure_date")
        if not departure_raw or not str(departure_raw).strip():
            raise TravelError(
                "MISSING_PARAMETER",
                "Data di partenza con anno richiesta nel formato YYYY-MM-DD.",
            )

        passengers_raw = params.get("passengers")
        if passengers_raw is None:
            raise TravelError(
                "MISSING_PARAMETER",
                "Numero di passeggeri richiesto. Non può essere dedotto tacitamente.",
            )

        origin = _require_iata(origin_raw, "Origine")
        destination = _require_iata(destination_raw, "Destinazione")
        if origin == destination:
            raise TravelError(
                "INVALID_ROUTE",
                f"Origine e destinazione coincidono ('{origin}').",
            )

        departure = _parse_iso_date(departure_raw, "data di partenza")

        return_date: str | None = None
        return_raw = params.get("return_date")
        if return_raw and str(return_raw).strip():
            ret = _parse_iso_date(return_raw, "data di ritorno")
            if ret < departure:
                raise TravelError(
                    "INVALID_DATE_RANGE",
                    f"La data di ritorno '{ret.isoformat()}' precede la partenza '{departure.isoformat()}'.",
                )
            return_date = ret.isoformat()

        try:
            if isinstance(passengers_raw, bool) or isinstance(passengers_raw, float) or (isinstance(passengers_raw, str) and "." in passengers_raw):
                raise ValueError()
            passengers = int(passengers_raw)
            if passengers <= 0:
                raise ValueError()
        except (ValueError, TypeError):
            raise TravelError(
                "INVALID_PASSENGER_COUNT",
                f"Numero passeggeri non valido '{passengers_raw}'. Richiesto intero positivo maggiore di 0.",
            ) from None
        if passengers > MAX_PASSENGERS:
            raise TravelError(
                "INVALID_PASSENGER_COUNT",
                f"Massimo {MAX_PASSENGERS} passeggeri per ricerca (richiesti {passengers}).",
            )

        cabin = str(params.get("cabin_class") or "economy").strip().lower()
        if cabin not in CABIN_CLASSES:
            raise TravelError(
                "INVALID_CABIN_CLASS",
                f"Classe cabina '{cabin}' non valida. Valori ammessi: {', '.join(CABIN_CLASSES)}.",
            )

        return cls(
            origin=origin,
            destination=destination,
            departure_date=departure.isoformat(),
            passengers=passengers,
            return_date=return_date,
            cabin_class=cabin,
        )


# =============================================================================
# BLOCCO 5 - UTILITÀ CONDIVISE (HTTP, DENARO, TEMPI E LAYOVER)
# =============================================================================
def _extract_error_message(body: str, fallback: str) -> str:
    try:
        parsed = json.loads(body)
    except ValueError:
        return fallback
    if isinstance(parsed, dict):
        top = parsed.get("error")
        if isinstance(top, str) and top.strip():
            return top.strip()[:300]
        errors = parsed.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            first = errors[0]
            msg = first.get("message") or first.get("detail") or first.get("title")
            if isinstance(msg, str) and msg.strip():
                return msg.strip()[:300]
    return fallback


def _request_json(req: urllib.request.Request, timeout: int, label: str) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        msg = _extract_error_message(body, str(exc.reason))
        if exc.code in (401, 403):
            raise TravelError(
                "PROVIDER_AUTH_ERROR",
                f"{label}: credenziali non valide o non abilitate ({exc.code}): {msg}",
            ) from None
        if exc.code == 429 and any(m in msg.lower() for m in QUOTA_EXHAUSTED_MARKERS):
            raise TravelError(
                "PROVIDER_QUOTA_EXHAUSTED",
                f"{label}: quota di ricerche esaurita ({msg}).",
            ) from None
        raise TravelError(
            "PROVIDER_API_ERROR",
            f"{label} API error ({exc.code}): {msg}",
            retryable=(exc.code == 429 or exc.code >= 500),
        ) from None
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
        reason = getattr(exc, "reason", exc)
        raise TravelError(
            "PROVIDER_UNAVAILABLE",
            f"Impossibile contattare {label} API: {reason}",
            retryable=True,
        ) from None

    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise TravelError(
            "PROVIDER_INVALID_RESPONSE",
            f"{label} ha restituito una risposta non JSON valida.",
        ) from None

    if not isinstance(parsed, dict):
        raise TravelError(
            "PROVIDER_INVALID_RESPONSE",
            f"{label} ha restituito una struttura inattesa (atteso un oggetto JSON).",
        )
    return parsed


def _parse_amount(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount <= 0:
        return None
    return amount


def _valid_currency(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    code = value.strip().upper()
    return code if re.fullmatch(r"[A-Z]{3}", code) else None


def _minutes_to_iso_duration(minutes: Any) -> str | None:
    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 0:
        return None
    hours, mins = divmod(minutes, 60)
    parts = ""
    if hours:
        parts += f"{hours}H"
    if mins or not hours:
        parts += f"{mins}M"
    return f"PT{parts}"


def _compute_layovers(segments: list[Segment]) -> tuple[list[Layover], bool, bool, bool]:
    if len(segments) <= 1:
        return [], False, False, False

    layovers: list[Layover] = []
    has_airport_change = False
    has_overnight_layover = False
    has_tight_connection = False

    for i in range(len(segments) - 1):
        curr_seg = segments[i]
        next_seg = segments[i + 1]

        arr_time_str = curr_seg.arriving_at
        dep_time_str = next_seg.departing_at

        airport_change = curr_seg.destination_airport != next_seg.origin_airport
        if airport_change:
            has_airport_change = True

        # ponytail: unknown stays None (no PT0M/zero invention); overnight = date change only
        overnight: bool | None = None
        duration_minutes: int | None = None

        if len(arr_time_str) >= 16 and len(dep_time_str) >= 16:
            try:
                dt_arr = datetime.fromisoformat(arr_time_str)
                dt_dep = datetime.fromisoformat(dep_time_str)
                diff = dt_dep - dt_arr
                minutes = int(diff.total_seconds() // 60)
                if minutes < 0:
                    pass
                else:
                    duration_minutes = minutes
                    if 0 < minutes < 60:
                        has_tight_connection = True
                    overnight = dt_arr.date() != dt_dep.date()
                    if overnight:
                        has_overnight_layover = True
            except ValueError:
                pass

        layovers.append(
            Layover(
                airport=curr_seg.destination_airport,
                duration=_minutes_to_iso_duration(duration_minutes) if duration_minutes else None,
                duration_minutes=duration_minutes,
                overnight=overnight,
                airport_change=airport_change,
            )
        )

    return layovers, has_airport_change, has_overnight_layover, has_tight_connection


def _build_slice(
    segments: list[Segment],
    duration: str | None,
    fallback_origin: str = "",
    fallback_destination: str = "",
) -> Slice | None:
    if not segments:
        return None

    origin = segments[0].origin_airport or fallback_origin
    destination = segments[-1].destination_airport or fallback_destination
    if not origin or not destination:
        return None

    first_departure = segments[0].departing_at
    departure_date = first_departure[:10] if len(first_departure) >= 10 else ""

    layovers, airport_change, overnight_layover, tight_conn = _compute_layovers(segments)
    first_carrier = segments[0].marketing_carrier if segments else ""
    is_homogenous = bool(first_carrier) and all(s.marketing_carrier == first_carrier for s in segments)

    return Slice(
        origin=origin,
        destination=destination,
        departure_date=departure_date,
        duration=duration,
        stops_count=max(0, len(segments) - 1),
        segments=segments,
        layovers=layovers,
        has_airport_change=airport_change,
        has_overnight_layover=overnight_layover,
        is_homogenous_carrier=is_homogenous,
        has_tight_connection=tight_conn,
    )


def _finalize_offer(
    *,
    offer_id: Any,
    provider: str,
    observed_at: str,
    amount: Decimal,
    currency: str,
    passengers_count: int,
    slices: list[Slice],
    unknown_fields: dict[str, str],
    environment: str = "production",
    provider_token: str = "",
) -> Offer:
    per_pax = (amount / max(1, passengers_count)).quantize(Decimal("0.01"))
    return Offer(
        offer_id=str(offer_id or ""),
        provider=provider,
        observed_at=observed_at,
        total_amount=float(amount),
        currency=currency,
        price_per_passenger=float(per_pax),
        passengers_count=passengers_count,
        slices=slices,
        unknown_fields=unknown_fields,
        total_amount_raw=str(amount),
        environment=environment,
        provider_token=provider_token,
    )


# =============================================================================
# BLOCCO 6 - INTERFACCIA PROVIDER
# =============================================================================
class TravelProvider(Protocol):
    @property
    def name(self) -> str:
        ...

    @property
    def environment(self) -> str:
        ...

    def cache_fingerprint(self) -> str:
        ...

    def search_offers(self, request: TravelRequest) -> tuple[list[Offer], int]:
        ...


def _make_cache_key(*parts: str) -> str:
    return "|".join([CACHE_SCHEMA_VERSION, *[str(p) for p in parts]])


# =============================================================================
# BLOCCO 7 - PROVIDER SERPAPI
# =============================================================================
class SerpApiProvider:
    environment = "production"

    def __init__(
        self,
        api_key: str | None = None,
        currency: str | None = None,
        gl: str | None = None,
        hl: str | None = None,
        deep_search: bool = False,
    ):
        key1 = api_key or os.environ.get("SERPAPI_API_KEY", "").strip()
        key2 = "" if api_key else os.environ.get("SERPAPI_API_KEY2", "").strip()
        if not key1 and key2:
            self.api_key = key2
            self.api_key_fallback = ""
        else:
            self.api_key = key1
            self.api_key_fallback = key2 if key2 != key1 else ""
        self._primary_exhausted = False

        cur = _valid_currency(currency or os.environ.get("SERPAPI_CURRENCY") or SERPAPI_DEFAULT_CURRENCY)
        if cur is None:
            raise TravelError(
                "INVALID_CONFIGURATION",
                "Valuta SerpApi non valida: richiesto un codice ISO 4217 di 3 lettere (es. EUR).",
            )
        self.currency = cur
        self.gl = (gl or SERPAPI_DEFAULT_GL).strip().lower()
        self.hl = (hl or SERPAPI_DEFAULT_HL).strip().lower()
        self.deep_search = deep_search

    @property
    def name(self) -> str:
        return "serpapi"

    def cache_fingerprint(self) -> str:
        return f"cur={self.currency}:gl={self.gl}:hl={self.hl}:deep={int(self.deep_search)}"

    def _execute_serpapi_query(self, params: dict[str, str]) -> dict[str, Any]:
        if not self.api_key:
            raise TravelError(
                "CREDENTIALS_REQUIRED",
                "Nessuna chiave SerpApi configurata. Imposta la variabile d'ambiente SERPAPI_API_KEY.",
            )

        def _fetch(key: str) -> dict[str, Any]:
            query_params = dict(params)
            query_params["api_key"] = key
            http_req = urllib.request.Request(
                f"{SERPAPI_SEARCH_URL}?{urllib.parse.urlencode(query_params)}",
                headers={"Accept": "application/json", "User-Agent": "Jarvis-Hermes/1.0"},
                method="GET",
            )
            raw_data = _request_json(http_req, SEARCH_TIMEOUT_S, "SerpApi")
            self._raise_if_error_payload(raw_data)
            return raw_data

        active_key = self.api_key_fallback if (self._primary_exhausted and self.api_key_fallback) else self.api_key
        try:
            return _fetch(active_key)
        except TravelError as exc:
            if exc.code != "PROVIDER_QUOTA_EXHAUSTED" or active_key != self.api_key or not self.api_key_fallback:
                raise
            self._primary_exhausted = True
            logger.warning("SerpApi: quota chiave primaria esaurita, passo alla chiave secondaria")
            return _fetch(self.api_key_fallback)

    def search_offers(self, request: TravelRequest) -> tuple[list[Offer], int]:
        params: dict[str, str] = {
            "engine": "google_flights",
            "departure_id": request.origin,
            "arrival_id": request.destination,
            "outbound_date": request.departure_date,
            "type": "1" if request.return_date else "2",
            "adults": str(request.passengers),
            "travel_class": SERPAPI_TRAVEL_CLASS[request.cabin_class],
            "currency": self.currency,
            "gl": self.gl,
            "hl": self.hl,
            "sort_by": "2",
        }
        if request.return_date:
            params["return_date"] = request.return_date
        if self.deep_search:
            params["deep_search"] = "true"

        raw_data = self._execute_serpapi_query(params)
        return self.normalize_response(raw_data, request.passengers, self.currency, request.return_date)

    @staticmethod
    def _raise_if_error_payload(data: dict[str, Any]) -> None:
        error = data.get("error")
        status = (data.get("search_metadata") or {}).get("status")
        if not error and status != "Error":
            return

        message = str(error)[:300] if error else "ricerca terminata con stato Error"
        if error and "hasn't returned any results" in message.lower():
            return

        if error and any(m in message.lower() for m in QUOTA_EXHAUSTED_MARKERS):
            raise TravelError(
                "PROVIDER_QUOTA_EXHAUSTED",
                f"SerpApi: quota di ricerche esaurita ({message}).",
            ) from None

        raise TravelError(
            "PROVIDER_API_ERROR",
            f"SerpApi: {message}",
            retryable=(status == "Error"),
        )

    @classmethod
    def normalize_response(
        cls,
        data: dict[str, Any],
        passengers_count: int,
        currency: str | None = None,
        return_date: str | None = None,
    ) -> tuple[list[Offer], int]:
        observed_at = datetime.now(timezone.utc).isoformat()
        resolved_currency = _valid_currency(currency) or _valid_currency(
            (data.get("search_parameters") or {}).get("currency")
        )
        if resolved_currency is None:
            raise TravelError(
                "PROVIDER_INVALID_RESPONSE",
                "Valuta dei prezzi SerpApi non determinabile: offerte ambigue, nessuna restituita.",
            )

        groups = list(data.get("best_flights") or []) + list(data.get("other_flights") or [])
        normalized: list[Offer] = []
        discarded = 0
        seen_ids: set[str] = set()

        for raw in groups:
            if not isinstance(raw, dict):
                discarded += 1
                continue
            amount = _parse_amount(raw.get("price"))
            if amount is None:
                discarded += 1
                continue

            segments: list[Segment] = []
            for seg_raw in raw.get("flights") or []:
                dep = seg_raw.get("departure_airport") or {}
                arr = seg_raw.get("arrival_airport") or {}
                number = str(seg_raw.get("flight_number") or "").replace(" ", "") or None
                carrier = str(seg_raw.get("airline") or "")

                segments.append(
                    Segment(
                        origin_airport=str(dep.get("id") or ""),
                        destination_airport=str(arr.get("id") or ""),
                        departing_at=str(dep.get("time") or "").replace(" ", "T"),
                        arriving_at=str(arr.get("time") or "").replace(" ", "T"),
                        marketing_carrier=carrier,
                        operating_carrier=seg_raw.get("plane_and_crew_by"),
                        flight_number=number,
                        duration=_minutes_to_iso_duration(seg_raw.get("duration")),
                    )
                )

            built = _build_slice(segments, _minutes_to_iso_duration(raw.get("total_duration")))
            if built is None:
                discarded += 1
                continue

            fingerprint = "|".join(
                f"{s.marketing_carrier}|{s.flight_number}@{s.origin_airport}->{s.destination_airport}@{s.departing_at}"
                for s in segments
            ) + f"|{amount}|{resolved_currency}"
            offer_id = "serpapi_" + hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:16]
            if offer_id in seen_ids:
                continue
            seen_ids.add(offer_id)

            unknown_fields = {
                "booking_reliability": "prezzo estratto da Google Flights: indicativo, non prenotabile",
                "price_precision": "intero in unità di valuta (totale per tutti i passeggeri)",
            }
            # Live 2026-10-16: SerpApi per A/R restituisce solo i segmenti di andata
            # col prezzo totale A/R (ritorno solo nel departure_token). Niente dati inventati.
            if return_date:
                unknown_fields["return_leg"] = (
                    "A/R: segmenti del solo volo di andata; il prezzo copre andata+ritorno, "
                    "dettagli ritorno nel provider_token (departure_token SerpApi)."
                )

            normalized.append(
                _finalize_offer(
                    offer_id=offer_id,
                    provider="serpapi",
                    observed_at=observed_at,
                    amount=amount,
                    currency=resolved_currency,
                    passengers_count=passengers_count,
                    slices=[built],
                    unknown_fields=unknown_fields,
                    provider_token=str(raw.get("departure_token") or raw.get("booking_token") or ""),
                )
            )

        return normalized, discarded


# =============================================================================
# BLOCCO 8 - PROVIDER DUFFEL
# =============================================================================
class DuffelProvider:
    API_BASE = "https://api.duffel.com/air"

    def __init__(self, access_token: str | None = None):
        self.token = access_token or os.environ.get("DUFFEL_ACCESS_TOKEN", "").strip()

    @property
    def name(self) -> str:
        return "duffel"

    def cache_fingerprint(self) -> str:
        return f"env={self.environment}"

    @property
    def environment(self) -> str:
        return "test" if self.token.startswith("duffel_test_") else "production"

    def search_offers(self, request: TravelRequest) -> tuple[list[Offer], int]:
        if not self.token:
            raise TravelError("CREDENTIALS_REQUIRED", "Nessun token Duffel configurato.")

        slices_payload = [{"origin": request.origin, "destination": request.destination, "departure_date": request.departure_date}]
        if request.return_date:
            slices_payload.append({"origin": request.destination, "destination": request.origin, "departure_date": request.return_date})

        body = {
            "data": {
                "slices": slices_payload,
                "passengers": [{"type": "adult"} for _ in range(request.passengers)],
                "cabin_class": request.cabin_class,
            }
        }

        http_req = urllib.request.Request(
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

        raw_data = _request_json(http_req, SEARCH_TIMEOUT_S, "Duffel")
        return self.normalize_response(raw_data, request.passengers, self.environment)

    @classmethod
    def normalize_response(
        cls,
        data: dict[str, Any],
        passengers_count: int,
        environment: str = "production",
    ) -> tuple[list[Offer], int]:
        observed_at = datetime.now(timezone.utc).isoformat()
        payload = data.get("data", {})
        offers_raw = payload.get("offers", [])
        if not offers_raw and "total_amount" in payload:
            offers_raw = [payload]

        normalized: list[Offer] = []
        discarded = 0
        for raw in offers_raw:
            offer_id = raw.get("id", "")
            amount = _parse_amount(raw.get("total_amount"))
            currency = _valid_currency(raw.get("total_currency"))
            if amount is None or currency is None:
                discarded += 1
                continue

            slices_list: list[Slice] = []
            valid = True
            for s_raw in raw.get("slices", []):
                segments: list[Segment] = []
                for seg_raw in s_raw.get("segments", []):
                    marketing = seg_raw.get("marketing_carrier") or {}
                    operating = seg_raw.get("operating_carrier") or {}
                    number = seg_raw.get("marketing_carrier_flight_number")
                    flight_number = f"{marketing.get('iata_code', '')}{number}" if number else None

                    segments.append(
                        Segment(
                            origin_airport=(seg_raw.get("origin") or {}).get("iata_code", ""),
                            destination_airport=(seg_raw.get("destination") or {}).get("iata_code", ""),
                            departing_at=seg_raw.get("departing_at", ""),
                            arriving_at=seg_raw.get("arriving_at", ""),
                            marketing_carrier=marketing.get("name") or marketing.get("iata_code", ""),
                            operating_carrier=operating.get("name"),
                            flight_number=flight_number,
                            duration=seg_raw.get("duration"),
                        )
                    )

                built = _build_slice(
                    segments,
                    s_raw.get("duration"),
                    fallback_origin=(s_raw.get("origin") or {}).get("iata_code", ""),
                    fallback_destination=(s_raw.get("destination") or {}).get("iata_code", ""),
                )
                if built is None:
                    valid = False
                    break
                slices_list.append(built)

            if not valid or not slices_list:
                discarded += 1
                continue

            normalized.append(
                _finalize_offer(
                    offer_id=offer_id,
                    provider="duffel",
                    observed_at=observed_at,
                    amount=amount,
                    currency=currency,
                    passengers_count=passengers_count,
                    slices=slices_list,
                    unknown_fields={
                        "cabin_baggage_weight": "sconosciuto (non specificato nella risposta Duffel)",
                        "connection_risk_score": "sconosciuto (nessun modello di rischio deterministico senza dati live dei gate)",
                        "co2_emissions": "sconosciuto (compensazione carbonio opzionale non restituita)",
                    },
                    environment=environment,
                )
            )

        return normalized, discarded


# =============================================================================
# BLOCCO 9 - ORCHESTRATORE
# =============================================================================
class TravelManager:
    def __init__(self, provider: TravelProvider | None = None):
        self._provider = provider

    def resolve_provider(self) -> TravelProvider:
        return self._resolve_provider()

    def _resolve_provider(self) -> TravelProvider:
        if self._provider is not None:
            return self._provider
        forced = os.environ.get("TRAVEL_PROVIDER", "").strip().lower()
        if forced:
            if forced == "serpapi":
                return SerpApiProvider()
            if forced == "duffel":
                return DuffelProvider()
            raise TravelError("INVALID_CONFIGURATION", f"TRAVEL_PROVIDER '{forced}' non valido.")

        if os.environ.get("DUFFEL_ACCESS_TOKEN", "").strip():
            return DuffelProvider()
        if os.environ.get("SERPAPI_API_KEY", "").strip() or os.environ.get("SERPAPI_API_KEY2", "").strip():
            return SerpApiProvider()

        raise TravelError(
            "PROVIDER_NOT_CONFIGURED",
            "Nessun provider viaggi configurato. Imposta SERPAPI_API_KEY (ricerca e confronto prezzi) oppure DUFFEL_ACCESS_TOKEN (ricerca e prenotazione). Non verranno generati dati fittizi.",
        )

    def search_flights(self, params: dict[str, Any]) -> dict[str, Any]:
        request = TravelRequest.from_params(params)
        provider = self._resolve_provider()
        offers, discarded = provider.search_offers(request)
        offers = sorted(offers, key=lambda o: o.total_amount)[:MAX_OFFERS]

        return {
            "query": {
                "origin": request.origin,
                "destination": request.destination,
                "departure_date": request.departure_date,
                "return_date": request.return_date,
                "passengers": request.passengers,
                "cabin_class": request.cabin_class,
            },
            "provider": provider.name,
            "environment": getattr(provider, "environment", "production"),
            "offers_count": len(offers),
            "discarded_count": discarded,
            "offers": [o.to_dict() for o in offers],
        }


# =============================================================================
# BLOCCO 10 - RICERCA FLESSIBILE CON CONTEGGIO COMBO CORRETTO (E2)
# =============================================================================
DEFAULT_DEPARTURE_WINDOW_DAYS = 5
DEFAULT_STAY_EXTRA_DAYS = 3
DEFAULT_MAX_SEARCHES = 20
HARD_MAX_SEARCHES = 100

TRAVEL_DB_FILENAME = "travel.sqlite3"
DEFAULT_TRAVEL_DAILY_LIMIT = 50
DEFAULT_TRAVEL_MONTHLY_LIMIT = 250
DEFAULT_TRAVEL_CACHE_TTL_SECONDS = 3600


@dataclass
class FlexibleTravelRequest:
    origin: str
    destination: str
    departure_date: str
    passengers: int
    return_date: str | None = None
    cabin_class: str = "economy"
    departure_window: int = DEFAULT_DEPARTURE_WINDOW_DAYS
    stay_extra: int = DEFAULT_STAY_EXTRA_DAYS
    max_searches: int = DEFAULT_MAX_SEARCHES

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> "FlexibleTravelRequest":
        base = TravelRequest.from_params(params)

        def _non_negative_int(raw: Any, name: str, default: int, max_value: int | None = None) -> int:
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                return default
            if isinstance(raw, bool):
                raise TravelError("INVALID_FLEXIBLE_PARAMS", f"'{name}' non valido.")
            # ponytail: float/str-float rifiutati (2.7 != 2, niente troncamenti silenziosi)
            if isinstance(raw, float) or (isinstance(raw, str) and re.search(r"[.eE]", raw.strip())):
                raise TravelError("INVALID_FLEXIBLE_PARAMS", f"'{name}' non valido.")
            try:
                val = int(raw)
                if val < 0 or (max_value is not None and val > max_value):
                    raise ValueError()
                return val
            except (ValueError, TypeError):
                raise TravelError("INVALID_FLEXIBLE_PARAMS", f"'{name}' non valido.") from None

        window = _non_negative_int(params.get("departure_window"), "departure_window", DEFAULT_DEPARTURE_WINDOW_DAYS, max_value=30)
        stay = _non_negative_int(params.get("stay_extra"), "stay_extra", DEFAULT_STAY_EXTRA_DAYS, max_value=30)
        max_searches = _non_negative_int(params.get("max_searches"), "max_searches", DEFAULT_MAX_SEARCHES)
        if max_searches < 1 or max_searches > HARD_MAX_SEARCHES:
            raise TravelError("INVALID_FLEXIBLE_PARAMS", "Parametro max_searches non valido.")
        if base.return_date is None and stay != 0 and params.get("stay_extra") is not None:
            raise TravelError(
                "INVALID_FLEXIBLE_PARAMS",
                "'stay_extra' richiede 'return_date': senza ritorno non c'è soggiorno da allungare.",
            )

        return cls(
            origin=base.origin,
            destination=base.destination,
            departure_date=base.departure_date,
            passengers=base.passengers,
            return_date=base.return_date,
            cabin_class=base.cabin_class,
            departure_window=window,
            stay_extra=stay,
            max_searches=max_searches,
        )

    def valid_combinations(self) -> list[tuple[str, str | None]]:
        """Elenca le coppie (andata, ritorno) in ordine rigorosamente cronologico."""
        base_dep = date.fromisoformat(self.departure_date)
        departures = [
            (base_dep + timedelta(days=delta)).isoformat()
            for delta in range(-self.departure_window, self.departure_window + 1)
        ]

        if self.return_date is None:
            return [(d, None) for d in departures]

        base_ret = date.fromisoformat(self.return_date)
        returns = [
            (base_ret + timedelta(days=extra)).isoformat() for extra in range(0, self.stay_extra + 1)
        ]

        combos = [(d, r) for d in departures for r in returns if r >= d]
        if not combos:
            raise TravelError(
                "INVALID_DATE_RANGE",
                "Nessuna combinazione andata/ritorno valida con questi parametri.",
            )
        return combos


class TravelCacheAndBudget:
    def __init__(
        self,
        db_path: Path | str | None = None,
        daily_limit: int | None = None,
        monthly_limit: int | None = None,
        ttl_seconds: int | None = None,
    ):
        if db_path is None:
            state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
            state_dir = state_home / "jarvis-hermes"
            state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            db_path = state_dir / TRAVEL_DB_FILENAME
        self.db_path = Path(db_path)
        # ponytail: argomento esplicito > env > default; env non valido = TravelError
        def _limit(raw: Any, env_name: str, default: int) -> int:
            v = raw if raw is not None else os.environ.get(env_name)
            if v is None or (isinstance(v, str) and not v.strip()):
                return default
            try:
                n = int(v)  # type: ignore[arg-type]
            except (ValueError, TypeError):
                raise TravelError("INVALID_CONFIGURATION", f"'{env_name}' non valido: '{v}'.") from None
            if n < 0:
                raise TravelError("INVALID_CONFIGURATION", f"'{env_name}' non valido: negativo.")
            return n

        self.daily_limit = _limit(daily_limit, "JARVIS_TRAVEL_DAILY_LIMIT", DEFAULT_TRAVEL_DAILY_LIMIT)
        self.monthly_limit = _limit(monthly_limit, "JARVIS_TRAVEL_MONTHLY_LIMIT", DEFAULT_TRAVEL_MONTHLY_LIMIT)
        self.ttl_seconds = _limit(ttl_seconds, "JARVIS_TRAVEL_CACHE_TTL_SECONDS", DEFAULT_TRAVEL_CACHE_TTL_SECONDS)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._get_conn()) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS travel_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT NOT NULL,
                    day_utc TEXT NOT NULL,
                    month_utc TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS travel_cache (
                    cache_key TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    query TEXT NOT NULL,
                    results_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    retrieved_at_iso TEXT NOT NULL
                )
            """)
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(self.db_path) + suffix).chmod(0o600)
            except OSError:
                pass

    def get_stats(self, provider: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        with closing(self._get_conn()) as conn:
            row_day = conn.execute(
                "SELECT COUNT(*) as cnt FROM travel_usage WHERE provider = ? AND day_utc = ?",
                (provider, now.strftime("%Y-%m-%d")),
            ).fetchone()
            row_month = conn.execute(
                "SELECT COUNT(*) as cnt FROM travel_usage WHERE provider = ? AND month_utc = ?",
                (provider, now.strftime("%Y-%m")),
            ).fetchone()
            return {
                "used_today": row_day["cnt"] if row_day else 0,
                "used_month": row_month["cnt"] if row_month else 0,
                "daily_limit": self.daily_limit,
                "monthly_limit": self.monthly_limit,
            }

    def ensure_budget(self, provider: str, needed: int = 1) -> None:
        stats = self.get_stats(provider)
        if stats["used_today"] + needed > self.daily_limit or stats["used_month"] + needed > self.monthly_limit:
            raise TravelError("SEARCH_QUOTA_EXHAUSTED", "Budget ricerche viaggi esaurito.")

    def record_usage(self, provider: str, count: int = 1) -> None:
        now = datetime.now(timezone.utc)
        rows = [(provider, now.strftime("%Y-%m-%d"), now.strftime("%Y-%m"), now.isoformat())] * count
        with closing(self._get_conn()) as conn:
            conn.executemany(
                "INSERT INTO travel_usage (provider, day_utc, month_utc, retrieved_at) VALUES (?, ?, ?, ?)",
                rows,
            )

    def lookup(self, cache_key: str) -> tuple[str, dict[str, Any] | None]:
        if self.ttl_seconds <= 0:
            return ("miss", None)
        with closing(self._get_conn()) as conn:
            row = conn.execute("SELECT results_json, created_at FROM travel_cache WHERE cache_key = ?", (cache_key,)).fetchone()
        if row is None:
            return ("miss", None)
        try:
            payload = json.loads(row["results_json"])
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            self.invalidate(cache_key)
            return ("miss", None)
        if time.time() - float(row["created_at"]) <= self.ttl_seconds:
            return ("hit", payload)
        return ("stale", None)

    def invalidate(self, cache_key: str) -> None:
        with closing(self._get_conn()) as conn:
            conn.execute("DELETE FROM travel_cache WHERE cache_key = ?", (cache_key,))

    def prune_stale(self) -> int:
        with closing(self._get_conn()) as conn:
            cur = conn.execute(
                "DELETE FROM travel_cache WHERE created_at < ?",
                (time.time() - self.ttl_seconds,),
            )
            return cur.rowcount if cur.rowcount is not None else 0

    def store(self, cache_key: str, provider: str, query: str, payload: dict[str, Any]) -> None:
        if self.ttl_seconds <= 0:
            return
        with closing(self._get_conn()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO travel_cache (cache_key, provider, query, results_json, created_at, retrieved_at_iso) VALUES (?, ?, ?, ?, ?, ?)",
                (cache_key, provider, query, json.dumps(payload), time.time(), datetime.now(timezone.utc).isoformat()),
            )


def search_flexible_flights(
    params: dict[str, Any],
    provider: TravelProvider | None = None,
    cache: TravelCacheAndBudget | None = None,
) -> dict[str, Any]:
    flex = FlexibleTravelRequest.from_params(params)
    prov = TravelManager(provider=provider).resolve_provider()
    store = cache or TravelCacheAndBudget()

    combos = flex.valid_combinations()
    base_dep = date.fromisoformat(flex.departure_date)
    base_ret = date.fromisoformat(flex.return_date) if flex.return_date else None

    def _distance(c: tuple[str, str | None]) -> tuple[int, str]:
        d = abs((date.fromisoformat(c[0]) - base_dep).days)
        r = abs((date.fromisoformat(c[1]) - base_ret).days) if c[1] and base_ret else 0
        return (d + r, c[0])

    planned = sorted(combos, key=_distance)[: flex.max_searches]
    partial = len(planned) < len(combos)
    partial_reason = (
        f"Piano limitato a max_searches={flex.max_searches}: {len(planned)} su {len(combos)} combinazioni valide."
        if partial
        else None
    )

    offers_all: list[dict[str, Any]] = []
    discarded = 0
    searched_live = cache_hits = cache_expired = searches_used = 0
    budget_exhausted = False
    pending_exc: TravelError | None = None

    prov_fingerprint = prov.cache_fingerprint() if hasattr(prov, "cache_fingerprint") else ""

    for dep, ret in planned:
        cache_key = _make_cache_key(prov.name, flex.origin, flex.destination, dep, ret or "", str(flex.passengers), flex.cabin_class, prov_fingerprint)
        status, payload = store.lookup(cache_key)
        if status == "hit" and payload is not None:
            cache_hits += 1
            discarded += int(payload.get("discarded", 0))
            combo_offers = payload.get("offers", [])
            for o in combo_offers:
                o["from_cache"] = True
        else:
            if status == "stale":
                cache_expired += 1
            if budget_exhausted:
                continue  # senza budget si servono solo le cache hit
            try:
                store.ensure_budget(prov.name, 1)
            except TravelError as exc:
                budget_exhausted = True
                pending_exc = pending_exc or exc
                partial = True
                partial_reason = "Budget viaggi esaurito."
                continue
            single = TravelRequest(
                origin=flex.origin,
                destination=flex.destination,
                departure_date=dep,
                passengers=flex.passengers,
                return_date=ret,
                cabin_class=flex.cabin_class,
            )
            try:
                offers, disc = prov.search_offers(single)
            except TravelError as exc:
                if exc.code == "PROVIDER_QUOTA_EXHAUSTED":
                    partial = True
                    partial_reason = "Quota provider esaurita."
                    pending_exc = pending_exc or exc
                    budget_exhausted = True  # evita ulteriori addebiti inutili
                    continue
                if (searched_live > 0 or cache_hits > 0) and (exc.retryable or exc.code == "PROVIDER_API_ERROR"):
                    partial = True
                    partial_reason = f"Interruzione per errore provider ({exc.code})."
                    break
                raise
            store.store(
                cache_key,
                prov.name,
                json.dumps({"origin": flex.origin, "destination": flex.destination, "departure_date": dep, "return_date": ret}),
                {"offers": [o.to_dict() for o in offers], "discarded": disc},
            )
            store.record_usage(prov.name, 1)
            searched_live += 1
            searches_used += 1
            discarded += disc
            combo_offers = [o.to_dict() for o in offers]
            for o in combo_offers:
                o["from_cache"] = False

        for offer_dict in combo_offers:
            offer_dict["query_dates"] = {"departure_date": dep, "return_date": ret}
        offers_all.extend(combo_offers)

    offers_all = sorted(offers_all, key=lambda o: o["total_amount"])[:MAX_OFFERS]

    # ponytail: quota/budget esauriti prima di qualsiasi risultato = errore, non "successo" vuoto
    if not offers_all and searched_live == 0 and cache_hits == 0 and pending_exc is not None:
        raise pending_exc
    store.prune_stale()

    return {
        "query": {
            "origin": flex.origin,
            "destination": flex.destination,
            "departure_date": flex.departure_date,
            "return_date": flex.return_date,
            "passengers": flex.passengers,
            "cabin_class": flex.cabin_class,
            "departure_window": flex.departure_window,
            "stay_extra": flex.stay_extra,
            "max_searches": flex.max_searches,
        },
        "provider": prov.name,
        "environment": getattr(prov, "environment", "production"),
        "coverage": {
            "valid_combinations": len(combos),
            "planned": len(planned),
            "searched_live": searched_live,
            "from_cache": cache_hits,
            "cache_expired": cache_expired,
            "searches_used": searches_used,
            "partial": partial,
            "partial_reason": partial_reason,
            "cache_ttl_seconds": store.ttl_seconds,
        },
        "budget": store.get_stats(prov.name),
        "offers_count": len(offers_all),
        "discarded_count": discarded,
        "offers": offers_all,
    }
