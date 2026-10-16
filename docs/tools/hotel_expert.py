#!/home/nuno/.zeroclaw/workspace/tools/venv/bin/python3
import os
import sys
import argparse
import json
import asyncio
import aiohttp
import urllib.parse
from pathlib import Path
from dotenv import load_dotenv

# --- CONFIGURAZIONE AMBIENTE ---
TOOLS_DIR = Path("/home/nuno/.zeroclaw/workspace/tools/")
TRAVEL_DIR = TOOLS_DIR / "travel_info"
TRAVEL_DIR.mkdir(exist_ok=True)
load_dotenv(TOOLS_DIR.parent / ".env")

# --- ROTAZIONE API KEYS ---
env_keys = os.getenv("RAPIDAPI_KEYS")
API_KEYS = [k.strip() for k in env_keys.split(",")] if env_keys else [
    "fb59ed4279mshf335e88b69087b3p1d42e6jsn09657fd604b6",
    "c86101e896mshe92be3f9d3f0889p102582jsne6ef4225cc2f"
]

HOST = "agoda-com.p.rapidapi.com"

async def make_request(session, endpoint, params=None):
    """Helper di Rete: Alterna le chiavi API in caso di 429 o 403"""
    url = f"https://{HOST}{endpoint}"
    for key in API_KEYS:
        headers = {
            "x-rapidapi-key": key,
            "x-rapidapi-host": HOST,
            "Content-Type": "application/json"
        }
        try:
            async with session.get(url, headers=headers, params=params, timeout=25) as resp:
                if resp.status == 200:
                    return await resp.json()
                elif resp.status in [403, 429]:
                    continue 
        except Exception:
            continue
    return {"error": "rate_limit_all_keys"}

async def get_city_id_formatted(session, query):
    """Risolve la città e restituisce l'ID nel formato typeId_id richiesto per la ricerca"""
    data = await make_request(session, "/hotels/auto-complete", {"query": query})
    if data and "error" not in data:
        places = data.get("data", {}).get("places", []) or data.get("places", [])
        if places:
            place = places[0]
            raw_id = place.get("id")
            type_id = place.get("typeId", 1)
            return f"{type_id}_{raw_id}"
    return None

async def main():
    parser = argparse.ArgumentParser(description="Hotel Expert AI - Agoda Engine")
    parser.add_argument("--city", help="Città di ricerca")
    parser.add_argument("--checkin", help="Data checkin (YYYY-MM-DD)")
    parser.add_argument("--checkout", help="Data checkout (YYYY-MM-DD)")
    parser.add_argument("--adults", type=int, default=2)
    parser.add_argument("--rooms", type=int, default=1)
    parser.add_argument("--stars", help="Filtro stelle (es. 4,5)")
    parser.add_argument("--sort", default="Ranking,Desc")
    parser.add_argument("--get_link", help="Label per recuperare il link")
    args = parser.parse_args()

    async with aiohttp.ClientSession() as session:
        LATEST_FILE = TRAVEL_DIR / "latest_hotels.json"

        # --- FASE C: GENERAZIONE LINK ---
        if args.get_link:
            if not LATEST_FILE.exists(): return
            with open(LATEST_FILE, "r") as f: data = json.load(f)
            v = next((x for x in data.get("results", []) if x["label"] == args.get_link), None)
            if v:
                link = v.get("deep_link") or f"https://www.agoda.com/it-it/search?text={urllib.parse.quote(v['name'])}"
                print(json.dumps({"checkout_url": link}))
            return

        # --- FASE A: RICERCA ---
        if not args.city or not args.checkin or not args.checkout: return

        # Step 1: Resolve ID con formattazione specifica
        city_id_formatted = await get_city_id_formatted(session, args.city)
        if not city_id_formatted:
            print(json.dumps({"error": f"Località '{args.city}' non trovata."}))
            return

        # Step 2: Search Overnight
        search_params = {
            "id": city_id_formatted,
            "checkinDate": args.checkin,
            "checkoutDate": args.checkout,
            "adult": args.adults,
            "room": args.rooms,
            "currency": "EUR",
            "language": "it-it"
        }
        
        search_data = await make_request(session, "/hotels/search-overnight", params=search_params)
        
        # Fallback emergenza: se data è null, prova ID numerico semplice
        if not search_data or search_data.get("data") is None:
            search_params["id"] = city_id_formatted.split("_")[-1]
            search_data = await make_request(session, "/hotels/search-overnight", params=search_params)

        if not search_data or search_data.get("data") is None:
            print(json.dumps({"error": "Nessun risultato disponibile per questa località o date."}))
            return

        # Parsing Dati (Gerarchia dinamica citySearch/city)
        data_content = search_data.get("data", {})
        search_node = data_content.get("citySearch") or data_content.get("city") or data_content
        properties = search_node.get("properties", []) if isinstance(search_node, dict) else []

        final_results = []
        for i, prop in enumerate(properties[:5]):
            content = prop.get("content", {})
            info = content.get("informationSummary", {})
            
            # Estrazione Prezzo Ultra-Robusta (Fallback multipli)
            prezzo_display = "N/D"
            try:
                offers = prop.get("pricing", {}).get("offers", [])
                if offers:
                    room_pricing = offers[0].get("roomOffers", [{}])[0].get("room", {}).get("pricing", [{}])
                    p_node = room_pricing[0].get("price", {})
                    # Prova inclusive perNight, poi perRoomPerNight, poi perBook
                    prezzo_display = (p_node.get("perNight", {}).get("inclusive", {}).get("display") or 
                                     p_node.get("perRoomPerNight", {}).get("inclusive", {}).get("display") or
                                     p_node.get("perBook", {}).get("inclusive", {}).get("display") or "N/D")
            except: pass

            final_results.append({
                "label": f"Hotel {i+1}",
                "name": info.get("localeName", info.get("defaultName", "Hotel")),
                "stars": info.get("rating", "N/D"),
                "area": info.get("address", {}).get("area", {}).get("name", "N/D"),
                "score": content.get("reviews", {}).get("cumulative", {}).get("score", "N/D"),
                "price_per_night": prezzo_display,
                "deep_link": f"https://www.agoda.com/it-it/search?hotelId={prop.get('propertyId')}"
            })

        output = {
            "city": args.city,
            "checkin": args.checkin,
            "checkout": args.checkout,
            "results": final_results
        }

        with open(LATEST_FILE, "w") as f: json.dump(output, f, indent=4)
        print(json.dumps(output, indent=2))

if __name__ == "__main__":
    asyncio.run(main())