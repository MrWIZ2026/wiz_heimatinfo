import os
import re
import json
import time
from datetime import datetime, date
from urllib.parse import urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from dateutil.relativedelta import relativedelta


BASE_LIST_URL = "https://www.witzenhausen.eu/veranstaltungen/"
STATE_FILE = "state.json"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

TZ = ZoneInfo(os.environ.get("TZ", "Europe/Berlin"))
INCLUDE_PAST = os.environ.get("EXIST_POSTS", "0").strip().lower() in {"1", "true", "yes", "on"}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WitzenhausenEventsBot/1.0; +https://github.com/)",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}

MONTH_MAP = {
    "jan": 1, "januar": 1,
    "feb": 2, "februar": 2,
    "mär": 3, "mrz": 3, "märz": 3,
    "apr": 4, "april": 4,
    "mai": 5,
    "jun": 6, "juni": 6,
    "jul": 7, "juli": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "okt": 10, "oktober": 10,
    "nov": 11, "november": 11,
    "dez": 12, "dezember": 12,
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


def canonical_event_url(href: str) -> str | None:
    abs_url = urljoin(BASE_LIST_URL, href.strip())
    parts = urlsplit(abs_url)

    if "witzenhausen.eu" not in parts.netloc:
        return None

    path = parts.path

    if path in ("/veranstaltungen", "/veranstaltungen/"):
        return None

    if not path.startswith("/veranstaltungen/"):
        return None

    clean = urlunsplit((parts.scheme, parts.netloc, path.rstrip("/") + "/", "", ""))
    return clean


def extract_event_urls_from_list_page(html: str) -> set[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls: set[str] = set()

    for a in soup.find_all("a", href=True):
        u = canonical_event_url(a["href"])
        if u:
            urls.add(u)

    return urls


def parse_date_from_text(text: str) -> date | None:
    t = text.replace("..", ".").replace("\xa0", " ").strip()

    m = re.search(r"\b(\d{1,2})\.\s*([A-Za-zÄÖÜäöü]{3,10})\.?\s*(\d{4})\b", t)
    if m:
        day = int(m.group(1))
        mon_raw = m.group(2).strip().lower()
        mon = MONTH_MAP.get(mon_raw)
        if not mon:
            return None
        year = int(m.group(3))
        return date(year, mon, day)

    m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", t)
    if m:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))

    return None


def parse_time_from_text(text: str) -> tuple[int, int] | None:
    t = text.replace("\xa0", " ").strip()
    m = re.search(r"\b(\d{1,2})[:.](\d{2})\s*Uhr\b", t)
    if m:
        return int(m.group(1)), int(m.group(2))

    m = re.search(r"\b(\d{1,2})[:.](\d{2})\b", t)
    if m:
        return int(m.group(1)), int(m.group(2))

    return None


def parse_event_page(html: str, url: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")

    title_tag = soup.find("h1") or soup.find("h2")
    title = title_tag.get_text(strip=True) if title_tag else None
    if not title:
        return None

    text = soup.get_text("\n", strip=True).replace("\xa0", " ")
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

    event_date: date | None = None
    for ln in lines[:200]:
        d = parse_date_from_text(ln)
        if d:
            event_date = d
            break
    if not event_date:
        return None

    event_time: tuple[int, int] | None = None
    for ln in lines[:300]:
        tm = parse_time_from_text(ln)
        if tm:
            event_time = tm
            break

    location = None
    for ln in lines[:400]:
        if re.search(r"\b\d{5}\b", ln) and ("Witzenhausen" in ln or "," in ln):
            location = ln
            break
    if not location:
        for ln in lines[:400]:
            if "Witzenhausen" in ln and len(ln) > 8:
                location = ln
                break

    slug = url.rstrip("/").split("/")[-1]

    if event_time:
        hour, minute = event_time
    else:
        hour, minute = 0, 0

    start_dt = datetime(event_date.year, event_date.month, event_date.day, hour, minute, tzinfo=TZ)

    return {
        "id": slug,
        "title": title,
        "location": location,
        "url": url,
        "start_dt": start_dt,
        "date": event_date,
        "time": f"{hour:02d}:{minute:02d}" if event_time else None,
    }


def escape_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_message(evt: dict) -> str:
    title = escape_html(evt["title"])
    sd: datetime = evt["start_dt"]
    loc = escape_html(evt["location"]) if evt.get("location") else None

    when = sd.strftime("%d.%m.%Y %H:%M Uhr")

    parts = [f"<b>{title}</b>", when]
    if loc:
        parts.append(loc)
    parts.append(f'<a href="{evt["url"]}">Details</a>')
    return "\n".join(parts)


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


def discover_event_urls(months_ahead: int = 12) -> set[str]:
    urls: set[str] = set()
    today = date.today()

    for m in range(months_ahead):
        month_date = (today + relativedelta(months=m)).replace(day=1)
        month_param = month_date.isoformat()
        list_url = f"{BASE_LIST_URL}?eventDisplay=list&tribe-bar-date={month_param}"

        try:
            html = fetch(list_url)
            month_urls = extract_event_urls_from_list_page(html)
            urls |= month_urls
            print(f"List {month_param} found {len(month_urls)} urls")
        except Exception as e:
            print(f"List {month_param} failed: {e}")

        time.sleep(1)

    return urls


def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

    now = datetime.now(TZ)
    today = now.date()

    state = load_state()
    posted = set(state.get("posted_event_ids", []))

    event_urls = sorted(discover_event_urls(months_ahead=12))
    print(f"Discovered total event urls: {len(event_urls)}")

    events: list[dict] = []
    for u in event_urls:
        try:
            html = fetch(u)
            evt = parse_event_page(html, u)
            if evt:
                events.append(evt)
        except Exception as e:
            print(f"Parse failed {u}: {e}")
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
        time.sleep(1)

    print("Done.")


if __name__ == "__main__":
    main()
