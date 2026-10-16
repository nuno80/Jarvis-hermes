import os
import sys
import json
import urllib.request
import urllib.error

try:
    from dotenv import load_dotenv
except ImportError:
    print("Errore: la libreria 'python-dotenv' non è installata. Usa 'pip install python-dotenv' per usarla.")
    sys.exit(1)

def do_research(query):
    # Carica esplicitamente il file .env dalla root di ZeroClaw
    env_path = os.path.expanduser('~/.zeroclaw/.env')
    load_dotenv(dotenv_path=env_path)
    
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        print(f"Errore: GEMINI_API_KEY o GOOGLE_API_KEY mancanti nel file {env_path}")
        return

    # Endpoint Gemini Pro con Grounding Nativo
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-pro:generateContent?key={api_key}"
    
    payload = {
        "contents": [{
            "parts": [{"text": f"Agisci come un analista OSINT esperto. Effettua una ricerca profonda e iper-aggiornata sul web per: '{query}'.\n\nREGOLE DI OUTPUT:\n- Restituisci cause, conseguenze, ultime notizie e fonti.\n- Scrivi in markdown puro e formatta bene i dati.\n- REGOLA CRITICA: Sii iper-denso e sintetico. Trattieni tutte le nozioni ma costringile in massimo 1000/1200 parole. Non superare questa lunghezza o farai collassare la memoria del micro-agente che ti rilegge."}]
        }],
        "tools": [{"googleSearch": {}}] # Grounding
    }
    
    req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json'})
    
    try:
        print(f"📡 *Inoltro la query a Gemini Pro (Google Grounding)... Attendi...*\n")
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode('utf-8'))
            
            candidate = data.get('candidates', [{}])[0]
            content = candidate.get('content', {}).get('parts', [{}])[0].get('text', 'Nessun risultato testuale trovato.')
            
            print("=== 📝 REPORT DI DEEP RESEARCH ===\n")
            print(content)
            print("\n==================================")
            
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode('utf-8')
        print(f"Errore API Gemini: {e.code} - {err_msg}")
    except Exception as e:
        print(f"Errore critico nello script di ricerca: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python deep_research.py \"tuo argomento\"")
    else:
        query = " ".join(sys.argv[1:])
        do_research(query)
