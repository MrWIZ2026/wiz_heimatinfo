import os
import re
import json
import time
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


STATE_FILE = "state.json"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

TZ = ZoneInfo(os.environ.get("TZ", "Europe/Berlin"))
INCLUDE_PAST = os.environ.get("EXIST_POSTS", "0").strip().lower() in {"1", "true", "yes", "on"}

HEIMAT_EMBED_URL = os.environ.get("HEIMAT_EMBED_URL", "").strip()
if not HEIMAT_EMBED_URL:
    # Fallback, funktioniert für Witzenhausen (c Parameter)
    HEIMAT_EMBED_URL = "https://www.heimat-info.de/embeddings/events/v1/?c=a8169a1a-b21f-4922-98de-1bce6480c8f6"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WitzenhausenEventsBot/1.0; +https://github.com/)",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"posted_event_ids": []}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def fetch(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=45)
    r.raise_for_status()
    return r.text


def telegram_send_message(text_html: str) -> None:
    api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text_html,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    r = requests.post(api, json=payload, timeout=30)
    r.raise_for_status()


def escape_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def extract_event_detail_urls_from_embed(embed_html: str, embed_url: str) -> set[str]:
    soup = BeautifulSoup(embed_html, "html.parser")
    urls: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href:
            continue

        abs_url = urljoin(embed_url, href)

        # Gesucht: Heimat Info Event Detail URLs
        if abs_url.startswith("https://www.heimat-info.de/veranstaltungen/"):
            # Es gibt Links mit gleichem Ziel, wir normalisieren leicht
            urls.add(abs_url.rstrip("/"))

    return urls


def parse_heimat_event_detail_page(html: str, url: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True).replace("\xa0", " ")
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

    # Titel: meistens direkt nach "Zurück"
    title = None
    for i, ln in enumerate(lines):
        if ln.lower() == "zurück" and i + 1 < len(lines):
            title = lines[i + 1].strip()
            break
    if not title:
        return None

    # Zeitfenster: 07.02.2026 08:30-07.02.2026 11:30
    start_dt = None
    end_dt = None

    for ln in lines:
        m = re.search(
            r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}):(\d{2})\s*-\s*(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}):(\d{2})",
            ln,
        )
        if m:
            start_dt = datetime(
                int(m.group(3)), int(m.group(2)), int(m.group(1)),
                int(m.group(4)), int(m.group(5)),
                tzinfo=TZ
            )
            end_dt = datetime(
                int(m.group(8)), int(m.group(7)), int(m.group(6)),
                int(m.group(9)), int(m.group(10)),
                tzinfo=TZ
            )
            break

    if not start_dt:
        return None

    # Location: meistens direkt nach der Zeile mit dem Zeitraum
    location = None
    try:
        idx = next(i for i, ln in enumerate(lines) if re.search(r"\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}-\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}", ln))
        if idx + 1 < len(lines):
            cand = lines[idx + 1].strip()
            if cand:
                location = cand
    except StopIteration:
        pass

    event_id = url.rstrip("/").split("/")[-1]

    return {
        "id": event_id,
        "title": title,
        "location": location,
        "url": url,
        "start_dt": start_dt,
        "end_dt": end_dt,
    }


def build_message(evt: dict) -> str:
    title = escape_html(evt["title"])
    loc = escape_html(evt["location"]) if evt.get("location") else None

    sd: datetime = evt["start_dt"]
    ed: datetime = evt["end_dt"]

    when = f"{sd.strftime('%d.%m.%Y %H:%M')} bis {ed.strftime('%H:%M')} Uhr"
    if sd.date() != ed.date():
        when = f"{sd.strftime('%d.%m.%Y %H:%M')} bis {ed.strftime('%d.%m.%Y %H:%M')} Uhr"

    parts = [f"<b>{title}</b>", when]
    if loc:
        parts.append(loc)
    parts.append(f'<a href="{evt["url"]}">Details</a>')
    return "\n".join(parts)


def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

    now = datetime.now(TZ)
    today = now.date()

    state = load_state()
    posted = set(state.get("posted_event_ids", []))

    embed_html = fetch(HEIMAT_EMBED_URL)
    detail_urls = sorted(extract_event_detail_urls_from_embed(embed_html, HEIMAT_EMBED_URL))

    print(f"Embed URL: {HEIMAT_EMBED_URL}")
    print(f"Discovered detail urls: {len(detail_urls)}")

    events = []
    for u in detail_urls:
        try:
            html = fetch(u)
            evt = parse_heimat_event_detail_page(html, u)
            if evt:
                events.append(evt)
        except Exception as e:
            print(f"Failed {u}: {e}")
        time.sleep(0.5)

    events.sort(key=lambda e: e["start_dt"])

    if INCLUDE_PAST:
        to_post = [e for e in events if e["id"] not in posted]
    else:
        to_post = [e for e in events if e["id"] not in posted and e["start_dt"].date() >= today]

    print(f"Parsed events: {len(events)}")
    print(f"To post: {len(to_post)} INCLUDE_PAST={INCLUDE_PAST}")

    for evt in to_post:
        telegram_send_message(build_message(evt))
        posted.add(evt["id"])
        state["posted_event_ids"] = sorted(posted)
        save_state(state)
        time.sleep(1.0)

    print("Done.")


if __name__ == "__main__":
    main()
