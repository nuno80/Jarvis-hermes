# ADR 0008: Selezione del Provider Travel (Voli) per Jarvis V1

- **Stato:** Accettato
- **Data:** 2026-09-27
- **Autore:** Jarvis Agent / nuno80
- **Tracciabilità specifica:** J13, D12, E1, E2, Ticket Issue #20

## Contesto e Problema

Nella specifica Jarvis V1 (Appendice E ed F, ticket J13 / Issue #20), Jarvis deve consentire all'utente di pianificare viaggi e cercare voli con date flessibili, confrontando offerte reali, aggregando segmenti, prezzi, valute, timestamp e riportando i dati mancanti come `unknown` (senza inventare campi o probabilità fittizie).

Per soddisfare i requisiti J13, D12, E1 ed E2, è necessario:
1. Valutare e confrontare almeno due candidati usando la documentazione tecnica ed economica ufficiale (quota, ambiente di test/produzione, costi per chiamata, modalità di accesso e copertura geografica/vettori).
2. Selezionare l'architettura del provider e definire il contratto di normalizzazione (`Offer`, `TravelRequest`).
3. Gestire rigorosamente le credenziali: in assenza di credenziali, non simulare né inventare offerte fittizie, ma emettere un blocker esplicito (`NOT_CONFIGURED` o `CREDENTIALS_REQUIRED`) indicando chiaramente le credenziali necessarie.

## Candidati Valutati

### Candidato 1: Duffel API (Duffel Flights API v2)
- **Documentazione ufficiale:**
  - Panoramica e Test Mode: https://duffel.com/docs/api/overview/test-mode
  - Creazione Offer Request: https://duffel.com/docs/api/offer-requests/create-offer-request
  - Modello e pricing: https://duffel.com/docs/api/overview/flights-key-concepts
- **Ambiente Test vs Produzione:**
  - *Test mode*: sandbox completa con token di test (`duffel_test_...`), accesso a "Duffel Airways" (`ZZ`) per test deterministici e alle sandbox dei vettori partner. Nessun addebito monetario.
  - *Live mode*: richiede token live (`duffel_live_...`) e attivazione account live.
- **Quota e Limiti:**
  - Rate limit standard a livello organizzazione (generalmente 10-20 richieste al secondo). Nessun limite mensile rigido sulle ricerche in sandbox.
- **Costi:**
  - Nessun costo fisso o per singola ricerca ("Offer Request" gratuita nella maggior parte dei piani self-service, monetizzazione su ordini/booking completati a ~$3 per ordine + 1%).
- **Accesso:**
  - Registrazione immediata self-service, generazione token API in dashboard in 2 minuti senza approvazioni manuali o requisiti IATA.
- **Copertura:**
  - Oltre 300 compagnie aeree tramite connessioni dirette NDC e GDS tradizionali. Ottima copertura europea e internazionale, supporto segmenti, bagagli, classi e passeggeri multipli.

### Candidato 2: Amadeus for Developers (Self-Service Flight Offers Search API v2)
- **Documentazione ufficiale:**
  - Developer Guides & Pricing: https://amadeus4dev.github.io/developer-guides/pricing/
  - FAQ e quote: https://amadeus4dev.github.io/developer-guides/faq/
  - Test data e limiti: https://amadeus4dev.github.io/developer-guides/test-data/
  - API Reference: https://developers.amadeus.com/self-service/category/flights/api-doc/flight-offers-search
- **Ambiente Test vs Produzione:**
  - *Test environment* (`https://test.api.amadeus.com`): quota gratuita limitata mensile (es. 2.000 richieste/mese per Flight Offers Search), dati su un sottoinsieme statico o ritardato (non tutte le rotte/città ritornano dati live).
  - *Production environment* (`https://api.amadeus.com`): dati live in tempo reale. Include una quota gratuita mensile pari al test, oltre la quale si paga a consumo per singola chiamata API.
  - *Nota sul ciclo di vita*: Amadeus ha annunciato revisioni e dismissioni sul portale self-service legacy per il 2026, pur mantenendo attive le API enterprise e partner.
- **Costi:**
  - Free quota: ~2.000 chiamate/mese gratuite. Oltre la quota, tariffa a consumo (circa €0.002 - €0.005 per richiesta di ricerca a seconda dell'endpoint).
- **Accesso:**
  - Registrazione self-service sul portale sviluppatori, generazione di `API Key` e `API Secret` (autenticazione OAuth2 client_credentials con token bearer valido 30 minuti).
- **Copertura:**
  - Rete GDS globale Amadeus. Nel tier self-service mancano molti vettori low-cost (Ryanair, EasyJet, Wizz Air) e alcune compagnie non pubblicate; restituisce solo tariffe pubblicate standard.

## Tabella Comparativa

| Dimensione | Duffel Flights API v2 | Amadeus Self-Service (Flight Offers Search) |
|---|---|---|
| **Ambiente Test** | Sandbox completa con vettore fittizio stabile (`Duffel Airways`) + sandbox vettori | Ambiente test con sottoinsieme di dati GDS storici/statici |
| **Quota Gratuita** | Gratuita illimitata per ricerche e test in sandbox | 2.000 transazioni/mese gratuite in test e all'avvio in produzione |
| **Costo Produzione** | Ricerche gratuite / pay per booking (~$3 + 1%) | Pay-as-you-go per ricerca (~€0.002-€0.005/call) dopo quota gratuita |
| **Requisiti Accesso** | Self-service immediato, Bearer Token | Self-service OAuth2 Client Credentials (Key + Secret) |
| **Copertura** | NDC moderni + GDS, molti vettori europei | GDS Amadeus (prevalentemente vettori di bandiera, no LCC in self-service) |
| **Struttura Dati** | Normalizzata in `slices`, `segments`, `offers`, passeggeri espliciti | Complessa `itineraries`, `segments`, `travelerPricings`, dizionari IATA |

## Decisione

1. **Provider Primario Scelto:** **Duffel API v2** come provider primario consigliato per semplicità di onboarding, assenza di costi sulle query di ricerca, API moderna NDC e sandbox dedicata riproducibile.
2. **Provider Secondario / Alternativo:** **Amadeus Flight Offers Search v2** supportato tramite lo stesso adapter astratto (`TravelProvider`), consentendo all'utente di configurare o Duffel (`DUFFEL_ACCESS_TOKEN`) o Amadeus (`AMADEUS_CLIENT_ID` + `AMADEUS_CLIENT_SECRET`).
3. **Contratto di Normalizzazione Comune (`Offer`):**
   - Qualsiasi provider adotta la medesima struttura di normalizzazione:
     - `offer_id`: identificativo univoco dell'offerta dal provider.
     - `provider`: nome del provider (`duffel` o `amadeus`).
     - `observed_at`: timestamp ISO 8601 dell'acquisizione.
     - `total_amount`: importo totale normalizzato (float).
     - `currency`: valuta a 3 lettere (es. EUR, USD).
     - `price_per_passenger`: importo per passeggero (o totale / N passeggeri).
     - `passengers_count`: numero di passeggeri considerati.
     - `slices`: elenco delle tratte (andata, eventuale ritorno), ciascuna con segmenti, vettori, aeroporti di origine/destinazione, orari con offset, durata, numero scali.
     - `unknown_fields`: dizionario esplicito per tutti i dati non forniti dal provider (es. franchigia bagaglio in cabina se assente, rischio coincidenza non stimabile, emissioni CO2 se non presenti). Non inventare valori nulli o zero ingannevoli!
4. **Validazione Richiesta (`TravelRequest`):**
   - Origine, destinazione, date/anno e passeggeri devono essere espliciti. Se l'utente non specifica l'origine o l'anno o il numero di passeggeri, non dedurli arbitrariamente: restituire un errore di validazione `MISSING_PARAMETER` richiedendo il chiarimento (come prescritto da E1).
5. **Nessun Risultato Fittizio (Fail Closed):**
   - Se né `DUFFEL_ACCESS_TOKEN` né le chiavi Amadeus sono configurate nell'ambiente o vault, il sistema non restituisce mockup casuali: fallisce dichiarando `PROVIDER_NOT_CONFIGURED` o `CREDENTIALS_REQUIRED`, con istruzioni chiare sulle credenziali necessarie per attivare il provider reale.

## Conseguenze

- I test automatici verificheranno la normalizzazione sia con fixture reali (mock HTTP) sia su chiamate simulate con parametri corretti.
- I requisiti E1 (struttura Offer, campi unknown) e E2 (budget/chiamate) sono soddisfatti a livello di interfaccia e logica provider.
- Le issue successive (#21 date flessibili, #22 confronto offerte Pareto) disporranno di un layer pulito `jarvis_hermes.travel`.
