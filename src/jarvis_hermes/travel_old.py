"""Integrazione provider di viaggio e normalizzazione delle offerte di volo.

Implementa i requisiti J13, D12, E1, E2 e ADR 0008 (come superata da ADR 0016).

PROVIDER SUPPORTATI
-------------------
* SerpApi (engine `google_flights`): ricerca/confronto prezzi, piano gratuito
  da 250 ricerche al mese. I dati sono ESTRATTI da Google Flights: prezzi
  indicativi, NON prenotabili tramite questo modulo.
* Duffel: ricerca + prenotazione reali, richiede l'attivazione di un account
  "live" (token `duffel_live_...`). Mantenuto per un eventuale uso futuro.

(Amadeus Self-Service è stato dismesso dal fornitore il 17 luglio 2026 e il
relativo provider è stato rimosso.)

PRINCIPI DI DESIGN (valgono per tutto il modulo)
------------------------------------------------
1. FAIL CLOSED: se un dato è mancante o non affidabile, non lo si inventa.
   Niente prezzi a 0.0, niente valuta "EUR" dedotta, niente aeroporti
   "UNKNOWN". Un'offerta malformata viene SCARTATA (e loggata), perché
   un'offerta con prezzo sbagliato è peggio di nessuna offerta.
2. NESSUNA DEDUZIONE TACITA: origine, destinazione, data (con anno) e numero
   di passeggeri devono essere forniti esplicitamente dal chiamante.
3. ERRORI TIPIZZATI: ogni errore ha un `code` stabile e un flag `retryable`,
   così un chiamante automatico (agente/LLM) può decidere se riprovare.
4. NESSUNA FUGA DI CREDENZIALI: i messaggi d'errore non includono mai URL,
   header o body della richiesta. ATTENZIONE: SerpApi richiede la chiave API
   NELLA QUERY STRING dell'URL, quindi non loggare mai l'URL completo.
5. TRASPARENZA SUI DATI MANCANTI: ciò che il provider non fornisce, o che
   non è affidabile, finisce in `unknown_fields`, non viene stimato.
"""
from __future__ import annotations

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
# BLOCCO 1 - COSTANTI DI CONFIGURAZIONE
# =============================================================================
# Tenere i "numeri magici" in un unico posto rende il comportamento del modulo
# leggibile e modificabile senza cercare nel codice.

# Codice IATA aeroporto/città: esattamente 3 lettere maiuscole (dopo upper()).
IATA_PATTERN = re.compile(r"^[A-Z]{3}$")

# Formato data rigoroso. `date.fromisoformat` da solo, nelle versioni recenti di
# Python, accetta anche formati come "20260101": qui vogliamo SOLO YYYY-MM-DD.
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Classi di cabina accettate dal modulo (valori interni, stile Duffel).
CABIN_CLASSES: tuple[str, ...] = ("economy", "premium_economy", "business", "first")

# Mappa verso il parametro `travel_class` di SerpApi (1=Economy, 2=Premium
# economy, 3=Business, 4=First, come da documentazione ufficiale).
SERPAPI_TRAVEL_CLASS: dict[str, str] = {
    "economy": "1",
    "premium_economy": "2",
    "business": "3",
    "first": "4",
}

# Limite di adulti per ricerca (vincolo comune delle API di viaggio).
MAX_PASSENGERS = 9

# Numero massimo di offerte restituite, UGUALE per ogni provider, così i
# risultati sono confrontabili e la risposta ha dimensione prevedibile.
MAX_OFFERS = 10

# Timeout di rete (secondi). Con `deep_search` SerpApi può essere più lento.
SEARCH_TIMEOUT_S = 30

# Endpoint SerpApi (unico, il motore si sceglie con il parametro `engine`).
SERPAPI_SEARCH_URL = "https://serpapi.com/search"

# Impostazioni di localizzazione di SerpApi. Sono SCELTE DI PRODOTTO dichiarate
# (non deduzioni sull'utente) e sono sempre inviate esplicitamente:
#   currency: valuta dei prezzi restituiti (il default di SerpApi sarebbe USD);
#   gl: paese di ricerca (Google può mostrare prezzi diversi per paese);
#   hl: lingua (influisce sui nomi di compagnie/aeroporti).
SERPAPI_DEFAULT_CURRENCY = "EUR"
SERPAPI_DEFAULT_GL = "it"
SERPAPI_DEFAULT_HL = "en"

# Frammenti di messaggio con cui un 429 indica ESAURIMENTO DELLA QUOTA
# (non ritentabile) anziché superamento del limite orario (ritentabile).
# SerpApi usa lo stesso codice 429 per entrambi i casi.
QUOTA_EXHAUSTED_MARKERS = ("run out of searches", "out of searches")


def _env_int(key: str, default: int) -> int:
    """Legge un intero da ambiente con fallback robusto se non numerico."""
    raw = os.environ.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default



# =============================================================================
# BLOCCO 2 - ERRORI
# =============================================================================
class TravelError(Exception):
    """Errore di dominio con codice stabile e indicazione di ritentabilità.

    Attributi:
        code:      identificatore stabile leggibile da macchina
                   (es. "MISSING_PARAMETER", "PROVIDER_UNAVAILABLE").
        message:   descrizione leggibile da umano (in italiano).
        retryable: True solo se riprovare PUÒ avere successo (rete, limite
                   orario, 5xx). Errori di input, credenziali o quota
                   esaurita non sono mai ritentabili.
    """

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


# =============================================================================
# BLOCCO 3 - MODELLO DATI
# =============================================================================
@dataclass
class Segment:
    """Un singolo volo (tratta tra due aeroporti, senza scali intermedi)."""

    origin_airport: str
    destination_airport: str
    departing_at: str  # ISO 8601, ora LOCALE dell'aeroporto (senza offset UTC)
    arriving_at: str  # ISO 8601, ora LOCALE dell'aeroporto (senza offset UTC)
    marketing_carrier: str  # compagnia che vende il biglietto (nome o codice)
    operating_carrier: str | None = None  # chi opera fisicamente il volo
    flight_number: str | None = None  # es. "BA301"
    duration: str | None = None  # durata ISO 8601 (es. "PT2H15M")


@dataclass
class Slice:
    """Una direzione di viaggio (andata o ritorno), composta da 1+ segmenti."""

    origin: str
    destination: str
    departure_date: str  # YYYY-MM-DD, ricavata dal primo segmento
    duration: str | None  # durata totale ISO 8601, scali inclusi
    stops_count: int  # numero di scali = segmenti - 1
    segments: list[Segment] = field(default_factory=list)


@dataclass
class Offer:
    """Offerta di volo normalizzata, indipendente dal provider.

    NOTA su `price_per_passenger`: è la MEDIA (totale / passeggeri), non il
    prezzo reale di ciascun passeggero.

    NOTA su `total_amount`: è un float per compatibilità con l'interfaccia
    esistente, ma viene SEMPRE calcolato partendo da `Decimal`. Il valore
    esatto restituito dal provider è conservato in `total_amount_raw`.

    NOTA su `provider_token`: token opaco del provider per passi successivi
    (SerpApi: `departure_token` per ottenere il ritorno, oppure
    `booking_token`). Vuoto se il provider non ne fornisce.
    """

    offer_id: str
    provider: str
    observed_at: str
    total_amount: float
    currency: str
    price_per_passenger: float
    passengers_count: int
    slices: list[Slice]
    unknown_fields: dict[str, str] = field(default_factory=dict)
    # Campi aggiunti (con default, quindi retrocompatibili):
    total_amount_raw: str = ""  # importo esattamente come inviato dal provider
    environment: str = "production"  # "test" per sandbox (Duffel test token)
    provider_token: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serializza l'offerta in un dict JSON-compatibile."""
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
# BLOCCO 4 - VALIDAZIONE DELL'INPUT
# =============================================================================
def _parse_iso_date(value: Any, label: str) -> date:
    """Valida una data YYYY-MM-DD e la restituisce come `date`.

    Due controlli distinti, entrambi necessari:
      1. il FORMATO (regex): impone anno a 4 cifre esplicito, come richiesto
         dai requisiti ("l'anno non può essere dedotto tacitamente");
      2. l'ESISTENZA reale della data (`fromisoformat`): scarta "2026-99-99",
         "2026-02-30" e simili, che la sola regex lascerebbe passare.

    NOTA: non si controlla che la data sia futura. La validazione resta pura
    (niente dipendenza dall'orologio, test stabili nel tempo); il controllo
    definitivo spetta al provider, che rifiuta date non prenotabili.
    """
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
    """Normalizza (strip + maiuscolo) e valida un codice IATA a 3 lettere."""
    code = str(value).strip().upper()
    if not IATA_PATTERN.match(code):
        raise TravelError(
            "INVALID_AIRPORT_CODE",
            f"{label} '{value}' non valida: richiesto un codice IATA di 3 lettere (es. MXP, FCO, JFK).",
        )
    return code


@dataclass
class TravelRequest:
    """Richiesta di ricerca voli, già validata.

    Si costruisce SOLO tramite `from_params`, che garantisce le invarianti:
    IATA validi e distinti, date reali, ritorno non precedente alla partenza,
    passeggeri in [1, MAX_PASSENGERS], cabina nota.
    """

    origin: str
    destination: str
    departure_date: str
    passengers: int
    return_date: str | None = None
    cabin_class: str = "economy"

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> "TravelRequest":
        # --- 4.1 Campi obbligatori: presenza ---------------------------------
        # Nessun default: ogni parametro mancante è un errore esplicito, mai
        # una deduzione silenziosa.
        origin_raw = params.get("origin")
        if not origin_raw or not str(origin_raw).strip():
            raise TravelError(
                "MISSING_PARAMETER",
                "Aeroporto di origine richiesto (es. MXP, FCO, BRU). Non può essere dedotto tacitamente.",
            )

        destination_raw = params.get("destination")
        if not destination_raw or not str(destination_raw).strip():
            raise TravelError("MISSING_PARAMETER", "Destinazione richiesta (es. DPS, HND, JFK).")

        departure_raw = params.get("departure_date")
        if not departure_raw or not str(departure_raw).strip():
            raise TravelError(
                "MISSING_PARAMETER",
                "Data di partenza con anno richiesta nel formato YYYY-MM-DD. L'anno non può essere dedotto tacitamente.",
            )

        passengers_raw = params.get("passengers")
        if passengers_raw is None:
            raise TravelError(
                "MISSING_PARAMETER",
                "Numero di passeggeri richiesto. Non può essere dedotto tacitamente.",
            )

        # --- 4.2 Aeroporti: formato e coerenza -------------------------------
        origin = _require_iata(origin_raw, "Origine")
        destination = _require_iata(destination_raw, "Destinazione")
        if origin == destination:
            # Evita una chiamata API sicuramente inutile (e un errore opaco).
            # Con SerpApi sprecherebbe anche una ricerca della quota mensile.
            raise TravelError(
                "INVALID_ROUTE",
                f"Origine e destinazione coincidono ('{origin}').",
            )

        # --- 4.3 Date: formato, esistenza, ordine -----------------------------
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

        # --- 4.4 Passeggeri: intero positivo entro il limite -----------------
        # `bool` è sottoclasse di `int` in Python: True verrebbe letto come 1.
        # Escludiamo float e bool per non troncare silenziosamente 2.5 a 2.
        try:
            if isinstance(passengers_raw, (bool, float)):
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

        # --- 4.5 Cabina: solo valori noti ------------------------------------
        # Il default "economy" è l'unico default del modulo ed è documentato:
        # è un parametro opzionale, non un dato dedotto al posto dell'utente.
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
# BLOCCO 5 - UTILITÀ CONDIVISE (HTTP, DENARO, TEMPI, SEGMENTI)
# =============================================================================
def _extract_error_message(body: str, fallback: str) -> str:
    """Estrae un messaggio leggibile dal body d'errore di un provider.

    Forme gestite:
      SerpApi: {"error": "Invalid API key. ..."}
      Duffel : {"errors": [{"message": "..."}]}
    Se il body non è JSON o non ha la forma attesa, si usa `fallback`
    (tipicamente la reason HTTP). Il body grezzo NON viene mai incluso per
    intero: il messaggio è troncato a 300 caratteri.
    """
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
    """Esegue una richiesta HTTP e restituisce il JSON, classificando gli errori.

    Classificazione (è il cuore del flag `retryable`):
      * HTTP 401/403            -> PROVIDER_AUTH_ERROR,    retryable=False
        (chiave/token non valido o account non abilitato)
      * HTTP 429 "quota esaurita" -> PROVIDER_QUOTA_EXHAUSTED, retryable=False
        (inutile riprovare: serve attendere il rinnovo mensile o un piano)
      * HTTP 429 (altro) e 5xx  -> PROVIDER_API_ERROR,     retryable=True
        (limite orario o guasto temporaneo del provider)
      * HTTP 4xx (altri)        -> PROVIDER_API_ERROR,     retryable=False
      * errori di rete/timeout  -> PROVIDER_UNAVAILABLE,   retryable=True
      * risposta non JSON       -> PROVIDER_INVALID_RESPONSE, retryable=False

    SICUREZZA: nei messaggi si usa solo `label`, codice HTTP e messaggio del
    provider. Mai l'URL (con SerpApi contiene la chiave API!) né gli header.
    """
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        # NB: HTTPError è sottoclasse di URLError, quindi va catturato PRIMA.
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

        retryable = exc.code == 429 or exc.code >= 500
        raise TravelError(
            "PROVIDER_API_ERROR",
            f"{label} API error ({exc.code}): {msg}",
            retryable=retryable,
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
    """Converte un importo del provider in `Decimal` strettamente positivo.

    Restituisce None (= importo inutilizzabile) per: valore assente, non
    numerico, NaN/Infinity, zero o negativo. Il chiamante scarta l'offerta.
    Si passa da `str(value)` per non importare errori di rappresentazione
    binaria quando il provider invia un float JSON.
    """
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
    """Restituisce il codice valuta ISO 4217 (3 lettere maiuscole) o None.

    Nessun default: senza valuta l'importo è ambiguo (10 EUR != 10 JPY).
    """
    if not isinstance(value, str):
        return None
    code = value.strip().upper()
    return code if re.fullmatch(r"[A-Z]{3}", code) else None


def _minutes_to_iso_duration(minutes: Any) -> str | None:
    """Converte minuti (intero) in durata ISO 8601, es. 135 -> "PT2H15M".

    SerpApi esprime le durate in minuti; Duffel in ISO 8601. Si uniforma
    all'ISO 8601 così le offerte sono omogenee tra provider.
    Restituisce None se il valore non è un intero non negativo.
    """
    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 0:
        return None
    hours, mins = divmod(minutes, 60)
    parts = ""
    if hours:
        parts += f"{hours}H"
    if mins or not hours:
        parts += f"{mins}M"
    return f"PT{parts}"


def _build_slice(
    segments: list[Segment],
    duration: str | None,
    fallback_origin: str = "",
    fallback_destination: str = "",
) -> Slice | None:
    """Costruisce una `Slice` coerente a partire dai suoi segmenti.

    Origine/destinazione/data/scali sono DERIVATI dai segmenti (fonte unica di
    verità, identica per tutti i provider) invece che da campi opzionali
    della risposta. Se non ci sono segmenti, o gli aeroporti non sono
    determinabili, la slice è inutilizzabile e si restituisce None.
    """
    if not segments:
        return None

    origin = segments[0].origin_airport or fallback_origin
    destination = segments[-1].destination_airport or fallback_destination
    if not origin or not destination:
        return None

    # Valida che nessun segmento intermedio abbia aeroporto o orario vuoto
    for s in segments:
        if not s.origin_airport or not s.destination_airport or not s.departing_at or not s.arriving_at:
            return None

    first_departure = segments[0].departing_at
    departure_date = first_departure[:10] if len(first_departure) >= 10 else ""

    return Slice(
        origin=origin,
        destination=destination,
        departure_date=departure_date,
        duration=duration,
        stops_count=max(0, len(segments) - 1),
        segments=segments,
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
    """Assembla l'`Offer` finale (logica comune ai provider).

    Il prezzo medio per passeggero è calcolato in `Decimal` e arrotondato a
    due decimali solo alla fine; `max(1, ...)` evita divisioni per zero
    (impossibile con input validato, ma la funzione resta sicura se riusata).
    """
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
    """Contratto minimo di un provider (tipizzazione strutturale).

    Qualunque oggetto con `name` e `search_offers` è un provider valido:
    questo rende banale iniettare un finto provider nei test.
    Opzionale: un attributo `environment` ("test"/"production"); se assente
    si assume "production".

    `search_offers` restituisce `(offerte, scartate)`: il conteggio delle
    offerte malformate scartate permette al chiamante di distinguere "il
    provider ha restituito zero risultati" da "i risultati erano inutilizzabili"
    (copertura effettiva, J14/AT08).
    """

    @property
    def name(self) -> str:
        ...

    def search_offers(self, request: TravelRequest) -> tuple[list[Offer], int]:
        ...


# =============================================================================
# BLOCCO 7 - PROVIDER SERPAPI (GOOGLE FLIGHTS)
# =============================================================================
class SerpApiProvider:
    """Provider SerpApi, engine `google_flights`.

    COSA È: SerpApi estrae i risultati di Google Flights e li restituisce in
    JSON. Non è un'API ufficiale di Google né un canale di vendita.

    LIMITI NOTI (riportati anche nell'output di ogni offerta):
      * i prezzi sono INDICATIVI e non prenotabili da qui;
      * i prezzi sono INTERI (unità intere di valuta, niente centesimi);
      * per un andata+ritorno Google restituisce prima solo i voli di ANDATA
        con il prezzo complessivo andata+ritorno; il ritorno richiede una
        seconda chiamata con `departure_token` (che consuma +1 ricerca);
      * non è documentato se, con più passeggeri, il prezzo sia a persona o
        totale: viene trattato come TOTALE e segnalato in `unknown_fields`.

    COSTI: ogni ricerca riuscita consuma 1 ricerca della quota mensile
    (piano gratuito: 250/mese). Le ricerche in cache (stessi parametri, entro
    1 ora), quelle in errore e quelle fallite NON vengono conteggiate.
    """

    environment = "production"

    def __init__(
        self,
        api_key: str | None = None,
        currency: str | None = None,
        gl: str | None = None,
        hl: str | None = None,
        deep_search: bool = False,
    ):
        # Priorità: argomento esplicito, poi SERPAPI_API_KEY, poi SERPAPI_API_KEY2.
        # Se la primaria manca ma c'è la secondaria, la secondaria diventa primaria.
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

        # `deep_search` rende i risultati identici a quelli del browser ma è
        # più lento: disattivato di default (come da documentazione).
        self.deep_search = deep_search

    @property
    def name(self) -> str:
        return "serpapi"

    def cache_fingerprint(self) -> str:
        """Parametri di localizzazione ed esecuzione che variano il risultato."""
        return f"cur={self.currency}:gl={self.gl}:hl={self.hl}:deep={int(self.deep_search)}"



    # --- 7.1 Ricerca ---------------------------------------------------------
    def search_offers(self, request: TravelRequest) -> tuple[list[Offer], int]:
        if not self.api_key:
            raise TravelError(
                "CREDENTIALS_REQUIRED",
                "Nessuna chiave SerpApi configurata. Imposta la variabile d'ambiente SERPAPI_API_KEY.",
            )

        params: dict[str, str] = {
            "engine": "google_flights",
            "api_key": self.api_key,
            "departure_id": request.origin,
            "arrival_id": request.destination,
            "outbound_date": request.departure_date,
            # IMPORTANTE: il default di SerpApi è "Round trip" (type=1), che
            # RICHIEDE return_date. Il tipo va quindi impostato SEMPRE in modo
            # esplicito: 1 = andata e ritorno, 2 = solo andata.
            "type": "1" if request.return_date else "2",
            "adults": str(request.passengers),
            "travel_class": SERPAPI_TRAVEL_CLASS[request.cabin_class],
            "currency": self.currency,
            "gl": self.gl,
            "hl": self.hl,
            # Ordina per prezzo: coerente con l'ordinamento del TravelManager.
            "sort_by": "2",
        }
        if request.return_date:
            params["return_date"] = request.return_date
        if self.deep_search:
            params["deep_search"] = "true"

        # L'URL contiene la chiave API: non finisce MAI in log o messaggi.
        def _fetch(key: str) -> dict[str, Any]:
            params["api_key"] = key
            http_req = urllib.request.Request(
                f"{SERPAPI_SEARCH_URL}?{urllib.parse.urlencode(params)}",
                headers={"Accept": "application/json", "User-Agent": "Jarvis-Hermes/1.0"},
                method="GET",
            )
            raw_data = _request_json(http_req, SEARCH_TIMEOUT_S, "SerpApi")
            self._raise_if_error_payload(raw_data)
            return raw_data

        active_key = self.api_key_fallback if (self._primary_exhausted and self.api_key_fallback) else self.api_key
        try:
            raw_data = _fetch(active_key)
        except TravelError as exc:
            # Quota esaurita sulla chiave primaria -> un solo retry con la
            # chiave secondaria (se configurata e diversa). Qualunque altro
            # errore, o il fallimento anche del fallback, passa al chiamante.
            # NB: mai includere le chiavi nei log/messaggi (sono nell'URL).
            if exc.code != "PROVIDER_QUOTA_EXHAUSTED" or active_key != self.api_key or not self.api_key_fallback:
                raise
            self._primary_exhausted = True
            logger.warning("SerpApi: quota chiave primaria esaurita, passo alla chiave secondaria")
            raw_data = _fetch(self.api_key_fallback)
        return self.normalize_response(raw_data, request.passengers, self.currency)

    @staticmethod
    def _raise_if_error_payload(data: dict[str, Any]) -> None:
        """Gestisce gli errori che SerpApi può restituire dentro un JSON 200.

        Dalla documentazione ufficiale:
          * `error` = "Google hasn't returned any results for this query."
            con status `Success` NON è un fallimento: significa "nessun volo
            trovato". Si lascia passare: la normalizzazione darà lista vuota.
          * `search_metadata.status` = "Error" (es. proxy timeout): fallimento
            temporaneo lato SerpApi -> ritentabile.
          * qualunque altro `error`: errore non ritentabile.
        """
        error = data.get("error")
        status = (data.get("search_metadata") or {}).get("status")
        if not error and status != "Error":
            return

        message = str(error)[:300] if error else "ricerca terminata con stato Error"
        if error and "hasn't returned any results" in message.lower():
            return

        # La quota esaurita può arrivare anche dentro un JSON 200: stesso
        # codice del 429-quota così il fallback sulla seconda chiave scatta
        # in entrambi i casi, e il flessibile dichiara copertura parziale.
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

    # --- 7.2 Normalizzazione -------------------------------------------------
    @classmethod
    def normalize_response(
        cls,
        data: dict[str, Any],
        passengers_count: int,
        currency: str | None = None,
    ) -> tuple[list[Offer], int]:
        """Converte la risposta SerpApi in `Offer`, scartando quelle malformate.

        Metodo puro (nessuna rete): testabile con fixture JSON salvate.
        Restituisce `(offerte_valide, offerte_scartate)`.

        Struttura della risposta (documentazione ufficiale):
          * `best_flights` (non sempre presente) e `other_flights`: liste di
            itinerari con la stessa forma. Si leggono entrambe;
          * ogni itinerario ha `flights` (i segmenti), `layovers`,
            `total_duration` (minuti), `price` (INTERO), `type`
            ("Round trip"/"One way") e un token (`departure_token` per
            andata+ritorno, `booking_token` per il resto).

        `currency`: la risposta documentata non riporta la valuta nel singolo
        itinerario, quindi si usa quella RICHIESTA (che inviamo sempre in modo
        esplicito). Se manca anche nel chiamante si prova con
        `search_parameters.currency`; se non è determinabile NON si indovina:
        si solleva un errore, perché ogni prezzo sarebbe ambiguo.
        """
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

            # --- Prezzo: obbligatorio, senza default ---
            amount = _parse_amount(raw.get("price"))
            if amount is None:
                logger.warning("SerpApi: itinerario scartato (prezzo assente o non valido)")
                discarded += 1
                continue

            # --- Segmenti ---
            segments: list[Segment] = []
            for seg_raw in raw.get("flights") or []:
                dep = seg_raw.get("departure_airport") or {}
                arr = seg_raw.get("arrival_airport") or {}

                # SerpApi scrive i numeri volo con uno spazio ("BA 301"):
                # si rimuove per ottenere il formato standard "BA301".
                number = str(seg_raw.get("flight_number") or "").replace(" ", "") or None

                segments.append(
                    Segment(
                        origin_airport=str(dep.get("id") or ""),
                        destination_airport=str(arr.get("id") or ""),
                        # "2026-03-03 10:10" -> "2026-03-03T10:10" (ISO 8601,
                        # ora locale dell'aeroporto, senza offset UTC).
                        departing_at=str(dep.get("time") or "").replace(" ", "T"),
                        arriving_at=str(arr.get("time") or "").replace(" ", "T"),
                        # SerpApi fornisce il NOME della compagnia, non il codice.
                        marketing_carrier=str(seg_raw.get("airline") or ""),
                        # Es. "Horizon Air as Alaska Horizon": testo libero
                        # che indica chi opera davvero il volo.
                        operating_carrier=seg_raw.get("plane_and_crew_by"),
                        flight_number=number,
                        duration=_minutes_to_iso_duration(seg_raw.get("duration")),
                    )
                )

            built = _build_slice(segments, _minutes_to_iso_duration(raw.get("total_duration")))
            if built is None:
                logger.warning("SerpApi: itinerario scartato (segmenti o aeroporti mancanti)")
                discarded += 1
                continue

            # --- Identificatore deterministico ---
            # SerpApi non fornisce un id d'offerta stabile: se ne ricava uno
            # da ciò che identifica davvero l'itinerario (voli, orari, prezzo).
            # Lo stesso volo allo stesso prezzo produce sempre lo stesso id,
            # il che permette anche di eliminare i duplicati.
            fingerprint = "|".join(
                f"{s.flight_number}@{s.departing_at}" for s in segments
            ) + f"|{amount}|{resolved_currency}"
            offer_id = "serpapi_" + hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:16]
            if offer_id in seen_ids:
                continue
            seen_ids.add(offer_id)

            # --- Dati noti, non noti e avvertenze ---
            unknown_fields = {
                "booking_reliability": (
                    "prezzo estratto da Google Flights: indicativo, non garantito dal vettore "
                    "e non prenotabile tramite questo provider"
                ),
                "price_precision": "intero in unità di valuta (nessun centesimo disponibile)",
                "cabin_baggage_weight": "sconosciuto (solo indicazioni testuali generiche, non strutturate)",
                "connection_risk_score": "sconosciuto (nessun modello di rischio per le coincidenze)",
            }
            if passengers_count > 1:
                unknown_fields["price_scope"] = (
                    "non documentato da SerpApi: trattato come TOTALE per tutti i passeggeri; "
                    "verificare con una ricerca di prova"
                )
            if str(raw.get("type", "")).strip().lower() == "round trip":
                unknown_fields["return_slice"] = (
                    "non incluso: il prezzo è quello complessivo andata+ritorno ma i voli di ritorno "
                    "richiedono una seconda chiamata con provider_token (departure_token), +1 ricerca"
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
    """Provider Duffel Flights API v2 (ricerca e prenotazione reali).

    Richiede un token: `duffel_test_...` per la sandbox, `duffel_live_...`
    per dati reali (si ottiene dopo l'onboarding dell'account).
    Vincolo di Duffel: rapporto di 1500 ricerche per prenotazione; oltre,
    0,005$ per ricerca in eccesso.
    """

    API_BASE = "https://api.duffel.com/air"

    def __init__(self, access_token: str | None = None):
        # Priorità: argomento esplicito, poi variabile d'ambiente.
        self.token = access_token or os.environ.get("DUFFEL_ACCESS_TOKEN", "").strip()

    @property
    def name(self) -> str:
        return "duffel"

    def cache_fingerprint(self) -> str:
        return f"env={self.environment}"



    @property
    def environment(self) -> str:
        # I token di test iniziano con "duffel_test_": i dati sono di sandbox
        # e non vanno scambiati per reali. Tutto il resto è considerato live.
        return "test" if self.token.startswith("duffel_test_") else "production"

    # --- 8.1 Ricerca ---------------------------------------------------------
    def search_offers(self, request: TravelRequest) -> tuple[list[Offer], int]:
        if not self.token:
            raise TravelError(
                "CREDENTIALS_REQUIRED",
                "Nessun token Duffel configurato. Configura la variabile d'ambiente DUFFEL_ACCESS_TOKEN o inserisci le credenziali.",
            )

        # Una slice per direzione: andata sempre, ritorno solo se richiesto
        # (con origine e destinazione invertite).
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

        # Solo adulti: scelta esplicita (nessun bambino/neonato supportato).
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

    # --- 8.2 Normalizzazione -------------------------------------------------
    @classmethod
    def normalize_response(
        cls,
        data: dict[str, Any],
        passengers_count: int,
        environment: str = "production",
    ) -> tuple[list[Offer], int]:
        """Converte la risposta Duffel in `Offer`, scartando quelle malformate.

        Metodo puro (nessuna rete): testabile con fixture JSON salvate.
        Restituisce `(offerte_valide, offerte_scartate)`.
        """
        observed_at = datetime.now(timezone.utc).isoformat()
        payload = data.get("data", {})

        # Forma standard: {"data": {"offers": [...]}}.
        # Forma alternativa (singola offerta): {"data": {"total_amount": ...}}.
        offers_raw = payload.get("offers", [])
        if not offers_raw and "total_amount" in payload:
            offers_raw = [payload]

        normalized: list[Offer] = []
        discarded = 0
        for raw in offers_raw:
            if not isinstance(raw, dict):
                discarded += 1
                continue
            offer_id = raw.get("id", "")

            # --- Prezzo e valuta: obbligatori, senza default ---
            amount = _parse_amount(raw.get("total_amount"))
            currency = _valid_currency(raw.get("total_currency"))
            if amount is None or currency is None:
                logger.warning("Duffel: offerta %r scartata (prezzo o valuta non validi)", offer_id)
                discarded += 1
                continue

            # --- Slice e segmenti ---
            slices_list: list[Slice] = []
            valid = True
            for s_raw in raw.get("slices", []):
                segments: list[Segment] = []
                for seg_raw in s_raw.get("segments", []):
                    marketing = seg_raw.get("marketing_carrier") or {}
                    operating = seg_raw.get("operating_carrier") or {}

                    # Numero volo completo = codice compagnia + numero (es. "AZ610").
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

                # Fallback aeroporti: SOLO il codice IATA della slice (mai
                # `origin_type`, che è un tipo e non un codice).
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

            # Un'offerta senza slice utilizzabili non è presentabile.
            if not valid or not slices_list:
                logger.warning("Duffel: offerta %r scartata (itinerario incompleto)", offer_id)
                discarded += 1
                continue

            unknown_fields = {
                "cabin_baggage_weight": "sconosciuto (non specificato nella risposta Duffel)",
                "connection_risk_score": "sconosciuto (nessun modello di rischio deterministico senza dati live dei gate)",
                "co2_emissions": "sconosciuto (compensazione carbonio opzionale non restituita)",
            }
            if environment == "test":
                unknown_fields["data_reliability"] = "dati di sandbox Duffel: prezzi e disponibilità NON reali"

            normalized.append(
                _finalize_offer(
                    offer_id=offer_id,
                    provider="duffel",
                    observed_at=observed_at,
                    amount=amount,
                    currency=currency,
                    passengers_count=passengers_count,
                    slices=slices_list,
                    unknown_fields=unknown_fields,
                    environment=environment,
                )
            )

        return normalized, discarded


# =============================================================================
# BLOCCO 9 - ORCHESTRATORE
# =============================================================================
class TravelManager:
    """Coordina validazione, scelta del provider e formato della risposta.

    Politica sui provider: se ne usa UNO solo, nessun fallback automatico.
    Se il provider scelto fallisce, l'errore (con `retryable`) arriva al
    chiamante, che decide cosa fare. Mescolare silenziosamente fonti diverse
    renderebbe i risultati non riproducibili e non attribuibili.

    Scelta del provider (in ordine):
      1. provider iniettato nel costruttore (test, override manuali);
      2. variabile `TRAVEL_PROVIDER` = "serpapi" | "duffel" (scelta esplicita);
      3. automatica: Duffel se c'è DUFFEL_ACCESS_TOKEN (dati prenotabili),
         altrimenti SerpApi se c'è SERPAPI_API_KEY.
    """

    def __init__(self, provider: TravelProvider | None = None):
        self._provider = provider

    def resolve_provider(self) -> TravelProvider:
        """Sceglie il provider in base alla configurazione dell'ambiente."""
        if self._provider is not None:
            return self._provider

        # 2) Scelta esplicita: evita ambiguità quando entrambe le credenziali
        # sono presenti. Se le credenziali del provider scelto mancano, sarà
        # il provider stesso a sollevare CREDENTIALS_REQUIRED con il messaggio
        # più preciso.
        forced = os.environ.get("TRAVEL_PROVIDER", "").strip().lower()
        if forced:
            if forced == "serpapi":
                return SerpApiProvider()
            if forced == "duffel":
                return DuffelProvider()
            raise TravelError(
                "INVALID_CONFIGURATION",
                f"TRAVEL_PROVIDER '{forced}' non valido. Valori ammessi: serpapi, duffel.",
            )

        # 3) Scelta automatica in base alle credenziali presenti.
        if os.environ.get("DUFFEL_ACCESS_TOKEN", "").strip():
            return DuffelProvider()
        if os.environ.get("SERPAPI_API_KEY", "").strip() or os.environ.get("SERPAPI_API_KEY2", "").strip():
            return SerpApiProvider()

        # Fail closed: mai restituire risultati fittizi.
        raise TravelError(
            "PROVIDER_NOT_CONFIGURED",
            "Nessun provider viaggi configurato. Imposta SERPAPI_API_KEY (ricerca e confronto prezzi) oppure DUFFEL_ACCESS_TOKEN (ricerca e prenotazione). Non verranno generati dati fittizi.",
        )

    _resolve_provider = resolve_provider

    def search_flights(self, params: dict[str, Any]) -> dict[str, Any]:
        """Valida i parametri, interroga il provider e restituisce la risposta.

        Ordine intenzionale: PRIMA la validazione (errori economici, nessuna
        rete e nessuna ricerca consumata), POI la scelta del provider, infine
        la chiamata.

        `discarded_count` = offerte ricevute ma scartate in normalizzazione
        (prezzo/valuta/itinerario non validi). Permette al chiamante di
        distinguere "zero risultati dal provider" da "risultati inutilizzabili"
        (copertura effettiva, J14/AT08).
        """
        request = TravelRequest.from_params(params)
        provider = self.resolve_provider()
        offers, discarded = provider.search_offers(request)

        # Ordinamento per prezzo totale crescente e limite uniforme tra
        # provider. NB: il confronto per importo ha senso solo a parità di
        # valuta; con valute miste l'ordine è indicativo.
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
# BLOCCO 10 - RICERCA FLESSIBILE CON CACHE E BUDGET (J14/D12, Issue 21)
# =============================================================================
# Una "ricerca flessibile" esplora N coppie (andata, ritorno) attorno alle date
# base: partenza in [D-w, D+w], ritorno in [R, R+stay_extra] (solo se R è dato).
# Ogni coppia costa 1 chiamata provider (SerpApi: andata+ritorno in un colpo;
# il dettaglio dei voli di ritorno via departure_token costerebbe +1 ricerca e
# resta fuori da questo conteggio, eventuale Issue 22).
#
# Regole E2 implementate qui:
#  * prima si calcolano combinazioni valide e costo stimato, POI si consuma;
#  * cache SQLite con TTL: hit = nessuna chiamata, nessuna quota consumata;
#  * budget giornaliero/mensile: a esaurimento ci si ferma e si dichiara
#    copertura parziale (n su m), non un errore secco sui dati già raccolti;
#  * quota provider esaurita a metà giro -> copertura parziale, non fallimento.

DEFAULT_DEPARTURE_WINDOW_DAYS = 5
DEFAULT_STAY_EXTRA_DAYS = 3
DEFAULT_MAX_SEARCHES = 20
# Tetto anti-quota anche se il chiamante esagera (SerpApi gratis: 250/mese).
HARD_MAX_SEARCHES = 100

TRAVEL_DB_FILENAME = "travel.sqlite3"
DEFAULT_TRAVEL_DAILY_LIMIT = 50
DEFAULT_TRAVEL_MONTHLY_LIMIT = 250  # allineato al piano gratuito SerpApi
DEFAULT_TRAVEL_CACHE_TTL_SECONDS = 3600  # 1h: oltre, i prezzi sono "vecchi"


@dataclass
class FlexibleTravelRequest:
    """Richiesta di ricerca voli su date flessibili, già validata.

    Si costruisce SOLO tramite `from_params`. La validazione base (IATA,
    date, passeggeri, cabina) è riusata da `TravelRequest`; qui si aggiungono
    solo finestra di partenza, allungamento del soggiorno e tetto ricerche.
    """

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
        # Validazione base identica alla ricerca singola (E1, nessuna
        # deduzione tacita): se fallisce, nessuna chiamata viene consumata.
        base = TravelRequest.from_params(params)

        def _non_negative_int(raw: Any, name: str, default: int) -> int:
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                return default
            if isinstance(raw, (bool, float)):
                raise_value_error = True
            else:
                try:
                    value = int(raw)
                    raise_value_error = value < 0
                except (ValueError, TypeError):
                    raise_value_error = True
            if raise_value_error:
                raise TravelError(
                    "INVALID_FLEXIBLE_PARAMS",
                    f"'{name}' non valido ('{raw}'): richiesto un intero maggiore o uguale a 0.",
                )
            return int(raw)

        window = _non_negative_int(params.get("departure_window"), "departure_window", DEFAULT_DEPARTURE_WINDOW_DAYS)
        stay = _non_negative_int(params.get("stay_extra"), "stay_extra", DEFAULT_STAY_EXTRA_DAYS)
        max_searches = _non_negative_int(params.get("max_searches"), "max_searches", DEFAULT_MAX_SEARCHES)
        if max_searches < 1:
            raise TravelError(
                "INVALID_FLEXIBLE_PARAMS",
                "'max_searches' deve essere almeno 1: con 0 non si cerca nulla.",
            )
        if max_searches > HARD_MAX_SEARCHES:
            raise TravelError(
                "INVALID_FLEXIBLE_PARAMS",
                f"'max_searches'={max_searches} supera il tetto anti-quota di {HARD_MAX_SEARCHES}.",
            )

        if base.return_date is None and stay != 0 and params.get("stay_extra") is not None:
            # stay_extra senza ritorno è un parametro probabilmente sbagliato:
            # fail closed invece di ignorarlo in silenzio.
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

    def valid_combinations(self, today: date | None = None) -> list[tuple[str, str | None]]:
        """Elenca le coppie (andata, ritorno) valide, in ordine di data.

        Partenza in [D-w, D+w]; ritorno in [R, R+stay_extra] con ritorno non
        precedente alla partenza. Date nel passato rispetto a `today` vengono
        escluse (se today è None, non si applica filtro orologio per purezza/test).
        Con solo andata, una coppia per partenza.
        """
        base_dep = date.fromisoformat(self.departure_date)
        departures = [
            (base_dep + timedelta(days=delta)).isoformat()
            for delta in range(-self.departure_window, self.departure_window + 1)
            if today is None or (base_dep + timedelta(days=delta)) >= today
        ]
        if not departures:
            raise TravelError(
                "INVALID_DATE_RANGE",
                "Tutte le date di partenza calcolate cadono nel passato rispetto a oggi.",
            )
        if self.return_date is None:
            return [(d, None) for d in departures]
        base_ret = date.fromisoformat(self.return_date)
        returns = [
            (base_ret + timedelta(days=extra)).isoformat()
            for extra in range(0, self.stay_extra + 1)
            if today is None or (base_ret + timedelta(days=extra)) >= today
        ]
        combos = [(d, r) for d in departures for r in returns if r >= d]
        if not combos:
            raise TravelError(
                "INVALID_DATE_RANGE",
                "Nessuna combinazione andata/ritorno valida con questi parametri (tutte nel passato o ritorno prima dell'andata).",
            )
        return combos


class TravelCacheAndBudget:
    """Cache SQLite delle risposte grezze + contatore ricerche (J14/D12).

    Stesso pattern di `SearchBudgetTracker` in web.py: directory di stato con
    permessi 0o700, file 0o600, WAL. Le chiavi contengono solo parametri di
    ricerca (mai credenziali). Una hit evita chiamata e consumo di quota;
    una entry scaduta (TTL) viene ricontrollata e contata a parte (AT08).
    """

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
        self.daily_limit = (
            daily_limit
            if daily_limit is not None
            else _env_int("JARVIS_TRAVEL_DAILY_LIMIT", DEFAULT_TRAVEL_DAILY_LIMIT)
        )
        self.monthly_limit = (
            monthly_limit
            if monthly_limit is not None
            else _env_int("JARVIS_TRAVEL_MONTHLY_LIMIT", DEFAULT_TRAVEL_MONTHLY_LIMIT)
        )
        self.ttl_seconds = (
            ttl_seconds
            if ttl_seconds is not None
            else _env_int("JARVIS_TRAVEL_CACHE_TTL_SECONDS", DEFAULT_TRAVEL_CACHE_TTL_SECONDS)
        )
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Assicura permessi 0o600 prima o subito dopo la creazione del file.
        try:
            fd = os.open(str(self.db_path), os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            self.db_path.chmod(0o600)
        except OSError:
            pass
        conn = self._get_conn()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS travel_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT NOT NULL,
                    day_utc TEXT NOT NULL,
                    month_utc TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_travel_usage_day ON travel_usage(provider, day_utc)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_travel_usage_month ON travel_usage(provider, month_utc)")
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
        finally:
            conn.close()

    def get_stats(self, provider: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        conn = self._get_conn()
        try:
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
        finally:
            conn.close()

    def ensure_budget(self, provider: str, needed: int = 1) -> None:
        """Verifica il budget PRIMA di consumare (E2). Solleva se insufficiente."""
        stats = self.get_stats(provider)
        if stats["used_today"] + needed > self.daily_limit:
            raise TravelError(
                "SEARCH_QUOTA_EXCEEDED",
                f"Budget giornaliero viaggi esaurito per {provider} ({stats['used_today']}/{self.daily_limit}).",
            )
        if stats["used_month"] + needed > self.monthly_limit:
            raise TravelError(
                "SEARCH_QUOTA_EXCEEDED",
                f"Budget mensile viaggi esaurito per {provider} ({stats['used_month']}/{self.monthly_limit}).",
            )

    def record_usage(self, provider: str, count: int = 1) -> None:
        now = datetime.now(timezone.utc)
        rows = [(provider, now.strftime("%Y-%m-%d"), now.strftime("%Y-%m"), now.isoformat())] * count
        conn = self._get_conn()
        try:
            conn.executemany(
                "INSERT INTO travel_usage (provider, day_utc, month_utc, retrieved_at) VALUES (?, ?, ?, ?)",
                rows,
            )
        finally:
            conn.close()

    def lookup(self, cache_key: str) -> tuple[str, dict[str, Any] | None]:
        """Restituisce ("hit"|"stale"|"miss", payload). Stale = TTL scaduto."""
        if self.ttl_seconds <= 0:
            return ("miss", None)
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT results_json, created_at FROM travel_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return ("miss", None)
        payload = json.loads(row["results_json"])
        if time.time() - float(row["created_at"]) <= self.ttl_seconds:
            return ("hit", payload)
        return ("stale", None)

    def store(self, cache_key: str, provider: str, query: str, payload: dict[str, Any]) -> None:
        if self.ttl_seconds <= 0:
            return
        conn = self._get_conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO travel_cache (cache_key, provider, query, results_json, created_at, retrieved_at_iso) VALUES (?, ?, ?, ?, ?, ?)",
                (cache_key, provider, query, json.dumps(payload), time.time(), datetime.now(timezone.utc).isoformat()),
            )
        finally:
            conn.close()


def search_flexible_flights(
    params: dict[str, Any],
    provider: TravelProvider | None = None,
    cache: TravelCacheAndBudget | None = None,
) -> dict[str, Any]:
    """Cerca voli su date flessibili con cache e budget (J14/D12, AT08/AT09).

    Ordine intenzionale: PRIMA validazione (nessuna rete/quota), POI scelta
    provider, POI piano delle combinazioni; solo alla fine le chiamate, una
    per coppia, con cache-hit che non consuma nulla. A budget o quota
    esauriti ci si ferma e si dichiara copertura parziale: i dati già
    raccolti restano un risultato valido, non un errore.
    """
    flex = FlexibleTravelRequest.from_params(params)
    prov = TravelManager(provider=provider).resolve_provider()
    store = cache or TravelCacheAndBudget()

    # Se Duffel (TTL breve / offerte deperibili), azzera il TTL per non servire offerte scadute
    if prov.name == "duffel" and cache is None:
        store.ttl_seconds = 0

    combos = flex.valid_combinations()
    # Ordina per vicinanza alla data base richiesta (J14): la coppia base
    # (distanza 0) non deve mai essere esclusa dal taglio max_searches.
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

    prov_fingerprint = getattr(prov, "cache_fingerprint", lambda: "")()

    for dep, ret in planned:
        cache_key = "|".join([
            prov.name,
            flex.origin,
            flex.destination,
            dep,
            ret or "",
            str(flex.passengers),
            flex.cabin_class,
            prov_fingerprint,
        ])
        status, payload = store.lookup(cache_key)
        if status == "hit" and payload is not None:
            cache_hits += 1
            discarded += int(payload.get("discarded", 0))
            combo_offers = payload.get("offers", [])
            for o in combo_offers:
                o["from_cache"] = True
        else:
            if status == "stale":
                cache_expired += 1  # AT08: prezzo vecchio distinto da offerta verificata
            try:
                store.ensure_budget(prov.name, 1)
            except TravelError as exc:
                if exc.code == "SEARCH_QUOTA_EXCEEDED":
                    partial = True
                    partial_reason = (
                        f"Budget viaggi esaurito dopo {searched_live} ricerche live "
                        f"({searched_live + cache_hits} su {len(planned)} combinazioni pianificate)."
                    )
                    break
                raise
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
                    partial_reason = (
                        f"Quota provider esaurita dopo {searched_live} ricerche live "
                        f"({searched_live + cache_hits} su {len(planned)} combinazioni pianificate)."
                    )
                    break
                # Se abbiamo già raccolto dati utili e l'errore è transitorio o un errore 4xx del provider
                # su questa singola combinazione (es. rotta non servita o 400), non perdiamo tutto
                if (searched_live > 0 or cache_hits > 0) and (exc.retryable or exc.code == "PROVIDER_API_ERROR"):
                    partial = True
                    partial_reason = (
                        f"Interruzione per errore provider ({exc.code}) dopo {searched_live} ricerche live "
                        f"({searched_live + cache_hits} su {len(planned)} combinazioni pianificate)."
                    )
                    break
                raise
            store.store(
                cache_key,
                prov.name,
                json.dumps({"origin": flex.origin, "destination": flex.destination, "departure_date": dep, "return_date": ret, "passengers": flex.passengers, "cabin_class": flex.cabin_class}),
                {"offers": [o.to_dict() for o in offers], "discarded": disc},
            )
            store.record_usage(prov.name, 1)
            searched_live += 1
            searches_used += 1
            discarded += disc
            combo_offers = [o.to_dict() for o in offers]

        for offer_dict in combo_offers:
            offer_dict["query_dates"] = {"departure_date": dep, "return_date": ret}
        offers_all.extend(combo_offers)

    offers_all = sorted(offers_all, key=lambda o: o["total_amount"])[:MAX_OFFERS]

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
