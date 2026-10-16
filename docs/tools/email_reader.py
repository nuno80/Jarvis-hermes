import argparse
import imaplib
import email
from email.header import decode_header
import json
import os
import sys
from datetime import datetime, timedelta
import re

try:
    from dotenv import load_dotenv
    env_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '.env'))
    if os.path.exists(env_path):
        load_dotenv(env_path)
    else:
        load_dotenv()
except ImportError:
    pass

EMAIL_MEM_DIR = os.path.join(os.path.expanduser("~"), ".zeroclaw", "workspace", "memory", "emails")

def decode_str(s):
    if not s:
        return ""
    try:
        if isinstance(s, bytes):
            return s.decode("utf-8", errors="ignore")
        decoded_list = decode_header(s)
        final_str = ""
        for decoded_bytes, charset in decoded_list:
            if isinstance(decoded_bytes, bytes):
                final_str += decoded_bytes.decode(charset if charset else "utf-8", errors="ignore")
            else:
                final_str += str(decoded_bytes)
        return final_str
    except:
        return str(s)

def sanitize_filename(s):
    s = re.sub(r'[^\w\s\-]', '', s)
    s = re.sub(r'[\s]+', '-', s)
    return s[:50]

def save_emails_to_md(emails, provider, match_filter=None):
    """
    Salva le email in file .md nella directory memory/emails.
    
    Args:
        emails: Lista di dizionari email
        provider: 'gmail' o 'yahoo'
        match_filter: Se impostato, usa ricerca avanzata (vedi sotto)
    
    Returns:
        Lista di path dei file salvati
    """
    os.makedirs(EMAIL_MEM_DIR, exist_ok=True)
    saved_files = []
    
    # Se c'è un filtro, applica ricerca avanzata
    if match_filter:
        match_terms = [match_filter.lower()]
        match_no_space = match_filter.replace(" ", "").lower()
        if match_no_space not in match_terms:
            match_terms.append(match_no_space)
        words = match_filter.lower().split()
        for w in words:
            if w not in match_terms:
                match_terms.append(w)
        
        def email_matches(e):
            text = (e['From'] + " " + e['Subject']).lower()
            return any(term in text for term in match_terms)
        
        emails = [e for e in emails if email_matches(e)]
    
    for e in emails:
        safe_subject = sanitize_filename(e['Subject'])
        # Usa timestamp con millisecondi per evitare duplicati
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        filename = f"{timestamp}_{safe_subject}.md"
        filepath = os.path.join(EMAIL_MEM_DIR, filename)

        md_content = f"""# Email: {e['Subject']}

- **Da:** {e['From']}
- **Data:** {e['Date']}
- **Provider:** {provider}
- **Salvato il:** {datetime.now().isoformat()}

---

{e['Body']}
"""
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(md_content)
        saved_files.append(filepath)
    return saved_files

def cleanup_old_emails(days=7):
    if not os.path.exists(EMAIL_MEM_DIR):
        return []
    cutoff = datetime.now() - timedelta(days=days)
    deleted = []
    for f in os.listdir(EMAIL_MEM_DIR):
        if not f.endswith('.md'):
            continue
        filepath = os.path.join(EMAIL_MEM_DIR, f)
        mtime = datetime.fromtimestamp(os.path.getmtime(filepath))
        if mtime < cutoff:
            os.remove(filepath)
            deleted.append(f)
    return deleted

def fetch_emails(provider, folder="INBOX", limit=5, unread=False):
    if provider == "gmail":
        host = "imap.gmail.com"
        user = os.environ.get("GMAIL_USER")
        password = os.environ.get("GMAIL_PASS")
    elif provider == "yahoo":
        host = "imap.mail.yahoo.com"
        user = os.environ.get("YAHOO_USER")
        password = os.environ.get("YAHOO_PASS")
    else:
        raise ValueError("Provider must be 'gmail' or 'yahoo'")

    if not user or not password:
        raise ValueError(f"Credenziali per {provider} non trovate in .env (Richiesti: {provider.upper()}_USER e {provider.upper()}_PASS)")

    mail = imaplib.IMAP4_SSL(host)
    mail.login(user, password)

    status, messages = mail.select('"' + folder + '"')
    if status != 'OK':
        raise ValueError(f"Cartella IMAP '{folder}' non trovata o inaccessibile.")

    search_str = "UNSEEN" if unread else "ALL"
    status, messages = mail.search(None, search_str)

    if status != "OK":
        return []

    mail_ids = messages[0].split()
    if not mail_ids:
        return []

    mail_ids = mail_ids[-limit:]
    results = []

    for num in reversed(mail_ids):
        res, msg_data = mail.fetch(num, "(RFC822)")
        for response_part in msg_data:
            if isinstance(response_part, tuple):
                msg = email.message_from_bytes(response_part[1])

                subject = decode_str(msg.get("Subject"))
                from_ = decode_str(msg.get("From"))
                date_ = msg.get("Date")

                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        content_type = part.get_content_type()
                        content_disposition = str(part.get("Content-Disposition"))

                        if content_type == "text/plain" and "attachment" not in content_disposition:
                            try:
                                body = part.get_payload(decode=True).decode(errors="ignore")
                                break
                            except:
                                pass
                else:
                    try:
                        body = msg.get_payload(decode=True).decode(errors="ignore")
                    except:
                        pass

                results.append({"From": from_, "Date": str(date_), "Subject": subject, "Body": body})

    mail.logout()
    return results

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=["gmail", "yahoo"], help="Specifica il provider (gmail o yahoo)")
    parser.add_argument("--folder", default="INBOX", help="Cartella IMAP da spulciare")
    parser.add_argument("--limit", type=int, default=5, help="Numero max di messaggi da tornare")
    parser.add_argument("--unread", action="store_true", help="Cerca solo le mail NON lette")
    parser.add_argument("--save", action="store_true", help="Salva le email in file .md in memory/emails/")
    # Filtro per cercare email per mittente o oggetto (es. --match "maria coppeta")
    parser.add_argument("--match", type=str, default=None, help="Filtra per mittente o oggetto (case-insensitive)")
    parser.add_argument("--cleanup", action="store_true", help="Elimina email più vecchie di --days")
    parser.add_argument("--days", type=int, default=7, help="Giorni per cleanup automatico")
    args = parser.parse_args()

    if args.cleanup:
        deleted = cleanup_old_emails(args.days)
        print(f"Eliminati {len(deleted)} file email vecchi.")
        for f in deleted:
            print(f"  - {f}")
        sys.exit(0)

    if not args.provider:
        print("Errore: --provider è richiesto (usa gmail o yahoo)", file=sys.stderr)
        sys.exit(1)

    try:
        emails = fetch_emails(args.provider, args.folder, args.limit, args.unread)
        
        if not emails:
            print("Nessuna email trovata in questa cartella.")
            sys.exit(0)
        
        # Se c'è un filtro --match, applicalo con ricerca avanzata
        # Nota: quando si usa --match, aumenta il limite a 50 per cercare più email
        if args.match:
            # Costruisci varianti di ricerca: "maria coppeta" -> ["maria coppeta", "mariacoppeta", "maria", "coppeta"]
            match_terms = [args.match.lower()]
            # Rimuovi spazi: "maria coppeta" -> "mariacoppeta"
            match_no_space = args.match.replace(" ", "").lower()
            if match_no_space not in match_terms:
                match_terms.append(match_no_space)
            # Estrai singole parole
            words = args.match.lower().split()
            for w in words:
                if w not in match_terms:
                    match_terms.append(w)
            
            # Se l'utente non ha specificato un limite personalizzato, usa 50 per ricerche con --match
            if args.limit <= 5:  # era il default originale
                emails = fetch_emails(args.provider, args.folder, 50, args.unread)
            
            # Cerca in From e Subject
            def email_matches(e):
                text = (e['From'] + " " + e['Subject']).lower()
                return any(term in text for term in match_terms)
            
            emails = [e for e in emails if email_matches(e)]
            if not emails:
                print(f"Nessuna email che contiene '{args.match}'.")
                sys.exit(0)
        
        if args.save:
            saved = save_emails_to_md(emails, args.provider, args.match)
            print(f"Salvate {len(saved)} email in file .md:")
            for f in saved:
                print(f"  - {os.path.basename(f)}")
        else:
            print(f"Ho trovato {len(emails)} email:\n")
            for i, e in enumerate(emails, 1):
                print(f"### EMAIL {i}")
                print(f"**Da:** {e['From']}")
                print(f"**Data:** {e['Date']}")
                print(f"**Oggetto:** {e['Subject']}\n")
                body = e['Body'][:1500] + ("\n[...] (usa --save per corpo completo)" if len(e['Body']) > 1500 else "")
                print(f"{body}\n-----------------------------------\n")
    except Exception as ex:
        print(f"Errore durante la connessione/lettura: {ex}", file=sys.stderr)