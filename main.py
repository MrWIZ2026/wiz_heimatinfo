import os
import re
import json
import time
from datetime import datetime, date
from dateutil.relativedelta import relativedelta

import requests
from bs4 import BeautifulSoup


BASE_LIST_URL = "https://www.witzenhausen.eu/veranstaltungen/?iframe"
STATE_FILE = "state.json"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WitzenhausenEventsBot/1.0; +https://github.com/)",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}

MONTH_MAP = {
    "jan": 1, "feb": 2, "mär": 3, "mrz": 3, "apr": 4, "mai": 5, "jun": 6,
    "jul": 7, "juli": 7, "aug": 8, "sep": 9, "sept": 9, "okt": 10,
    "nov": 11, "dez": 12,
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
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def extract_event_urls_from_list_page(html: str) -> set[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href:
            continue

        if href.startswith("/"):
            href = "https://www.witzenhausen.eu" + href

        if "witzenhausen.eu/veranstaltungen/" not in href:
            continue

        # Root list page ausschließen
        if href.rstrip("/") in [
            "https://www.witzenhausen.eu/veranstaltungen",
            "https://www.witzenhausen.eu/veranstaltungen?iframe",
            "https://www.witzenhausen.eu/veranstaltungen/?iframe",
        ]:
            continue

        # Manche Links sind Kategorie Filter oder sowas
        if "?" in href:
            continue

        urls.add(href.rstrip("/") + "/")

    return urls


def parse_german_date(d: str) -> date | None:
    # Beispiele: "23. Jan. 2026" oder "10. Aug.. 2024"
    d = d.replace("..", ".").strip()
    m = re.search(r"\b(\d{1,2})\.\s*([A-Za-zÄÖÜäöü]{3,4})\.?\s*(\d{4})\b", d)
    if not m:
        return None
    day = int(m.group(1))
    mon_raw = m.group(2).strip().lower()
    mon = MONTH_MAP.get(mon_raw)
    if not mon:
        return None
    year = int(m.group(3))
    return date(year, mon, day)


def parse_event_page(html: str, url: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")

    title_tag = soup.find("h1") or soup.find("h2")
    title = title_tag.get_text(strip=True) if title_tag else None
    if not title:
        return None

    text = soup.get_text("\n", strip=True).replace("\xa0", " ")
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

    # Datum finden
    date_candidates = []
    for ln in lines:
        dt = parse_german_date(ln)
        if dt:
            date_candidates.append(dt)

    if not date_candidates:
        return None

    # Meist steht das Event Datum zweimal auf der Seite, wir nehmen das spätere Auftreten nicht zwingend
    # sondern das kleinste Datum als "Start"
    event_date = min(date_candidates)

    # Uhrzeit finden
    tm_match = re.search(r"\b(\d{1,2}:\d{2})\s*Uhr\b", text)
    event_time = tm_match.group(1) if tm_match else None

    # Ort finden, bevorzugt nahe der Uhrzeit
    location = None
    if event_time:
        needle = f"{event_time} Uhr"
        try:
            idx = next(i for i, ln in enumerate(lines) if needle in ln)
            for j in range(idx + 1, min(idx + 8, len(lines))):
                cand = lines[j]
                if re.search(r"\b\d{5}\b", cand) or "Witzenhausen" in cand:
                    location = cand
                    break
        except StopIteration:
            pass

    # Fallback: erste Zeile mit PLZ oder Witzenhausen
    if not location:
        for ln in lines:
            if re.search(r"\b\d{5}\b", ln) and ("Witzenhausen" in ln or "," in ln):
                location = ln
                break

    # Event ID aus URL
    event_id = url.rstrip("/").split("/")[-1]

    # Start datetime
    if event_time:
        hour, minute = event_time.split(":")
        start_dt = datetime(event_date.year, event_date.month, event_date.day, int(hour), int(minute))
    else:
        start_dt = datetime(event_date.year, event_date.month, event_date.day, 0, 0)

    return {
        "id": event_id,
        "title": title,
        "date": event_date.isoformat(),
        "time": event_time,
        "location": location,
        "url": url,
        "start_dt": start_dt.isoformat(),
    }


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


def build_message(evt: dict) -> str:
    d = evt["date"]
    t = evt["time"]
    loc = evt["location"]

    parts = [f"<b>{escape_html(evt['title'])}</b>"]
    if t:
        parts.append(f"{d} {t} Uhr")
    else:
        parts.append(f"{d}")

    if loc:
        parts.append(escape_html(loc))

    parts.append(f'<a href="{evt["url"]}">Details</a>')
    return "\n".join(parts)


def escape_html(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
    )


def discover_event_urls(months_ahead: int = 12) -> set[str]:
    urls = set()

    today = date.today()
    for m in range(months_ahead):
        month_date = today + relativedelta(months=m)
        month_param = month_date.replace(day=1).isoformat()
        url = f"{BASE_LIST_URL}&tribe-bar-date={month_param}"

        try:
            html = fetch(url)
            urls |= extract_event_urls_from_list_page(html)
        except Exception:
            # Wenn ein Monat fehlschlägt, weiter mit dem nächsten
            continue

        time.sleep(1)

    # Zusätzlich noch die Basis Seite ohne Param
    try:
        html = fetch(BASE_LIST_URL)
        urls |= extract_event_urls_from_list_page(html)
    except Exception:
        pass

    return urls


def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

    state = load_state()
    posted = set(state.get("posted_event_ids", []))

    event_urls = discover_event_urls(months_ahead=12)

    events = []
    for url in sorted(event_urls):
        try:
            html = fetch(url)
            evt = parse_event_page(html, url)
            if not evt:
                continue
            events.append(evt)
        except Exception:
            continue
        time.sleep(1)

    # sortieren
    events.sort(key=lambda e: e["start_dt"])

    # nur neue, nur ab heute
    today_iso = date.today().isoformat()
    to_post = [e for e in events if e["id"] not in posted and e["date"] >= today_iso]

    for evt in to_post:
        msg = build_message(evt)
        telegram_send_message(msg)
        posted.add(evt["id"])
        state["posted_event_ids"] = sorted(posted)

        save_state(state)
        time.sleep(2)

    print(f"Found {len(events)} events, posted {len(to_post)} new.")


if __name__ == "__main__":
    main()
