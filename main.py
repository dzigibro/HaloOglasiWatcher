#!/usr/bin/env python
import ssl, smtplib, requests, yaml
from pathlib import Path
from email.message import EmailMessage
from bs4 import BeautifulSoup

CFG = Path(__file__).with_name("config.yaml")
DB  = Path(__file__).with_name("seen.yaml")

def cfg():
    return yaml.safe_load(CFG.read_text())

def load_seen():
    if DB.exists():
        return set(yaml.safe_load(DB.read_text()) or [])
    return set()

def save_seen(seen):
    DB.write_text(yaml.safe_dump(sorted(seen)))





def fetch(url: str):
    """Return unique listing URLs from a Halo Oglasi search page (mid-2025 markup)."""
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (HaloWatch)"})
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    links = {
        "https://www.halooglasi.com" + a["href"].split("?")[0]
        for a in soup.select("h3 a[href^='/nekretnine/prodaja-stanova/']")
    }

    print(f"[DEBUG] extracted {len(links)} links")
    return links







def alert(cfg, links):
    msg = EmailMessage()
    msg["Subject"] = f"[HaloWatch] {len(links)} new listing(s)"
    msg["From"]    = cfg["email"]["username"]
    msg["To"]      = cfg["email"]["to"]
    msg.set_content("\n".join(links))

    with smtplib.SMTP(cfg["email"]["smtp_server"], cfg["email"]["smtp_port"]) as s:
        s.starttls(context=ssl.create_default_context())
        s.login(cfg["email"]["username"], cfg["email"]["password"])
        s.send_message(msg)

def main():
    c = cfg()
    seen = load_seen()
    new  = [link for link in fetch(c["location_url"]) if link not in seen]
    if new:
        alert(c, new)
        save_seen(seen.union(new))
        print("Done")

if __name__ == "__main__":
    main()
