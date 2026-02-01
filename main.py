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
DEBUG = os.environ.get("DEBUG", "0").strip().lower() in {"1", "true", "yes", "on"}

HEIMAT_EMBED_URL = os.environ.get("HEIMAT_EMBED_URL", "").strip()
if not HEIMAT_EMBED_URL:
    HEIMAT_EMBED_URL = "https://www.heimat-info.de/embeddings/events/v1/?c=a8169a1a-b21f-4922-98de-1bce6480c8f6"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WitzenhausenEventsBot/1.0; +https://github.com/)",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}

UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
DATE_RE = r"\d{2}\.\d{2}\.\d{4}"


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
        "disable_web_page_preview": True,
    }
    r = requests.post(api, json=payload, timeout=30)
    if DEBUG:
        print(f"[DEBUG] Telegram status {r.status_code}: {r.text[:200]}")
    r.raise_for_status()


def escape_html(s: str) -> str:
    s = s or ""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def extract_event_detail_urls_from_embed(embed_html: str, embed_url: str) -> set[str]:
    soup = BeautifulSoup(embed_html, "html.parser")
    urls: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        abs_url = urljoin(embed_url, href)
        if abs_url.startswith("https://www.heimat-info.de/veranstaltungen/"):
            urls.add(abs_url.rstrip("/"))

    for m in re.finditer(rf"https://www\.heimat-info\.de/veranstaltungen/{UUID_RE}", embed_html):
        urls.add(m.group(0).rstrip("/"))
    for m in re.finditer(rf"/veranstaltungen/{UUID_RE}", embed_html):
        urls.add(("https://www.heimat-info.de" + m.group(0)).rstrip("/"))

    return urls


def is_obviously_not_location(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return True

    low = s.lower()

    if low in {"details", "zurück"}:
        return True

    if re.fullmatch(DATE_RE, s):
        return True

    if re.search(rf"{DATE_RE}\s+\d{{1,2}}:\d{{2}}", s):
        return True

    if re.search(r"\b\d{1,2}:\d{2}\b", s) and "uhr" in low:
        return True

    return False


def find_event_datetime_line_index(lines: list[str], event_date_str: str, event_time_str: str) -> int | None:
    """
    Sucht die Zeile, die zum Event Startdatum und Startzeit gehört.
    Falls Datum und Uhrzeit auf mehrere Zeilen verteilt sind, wird auch das abgefangen.
    """
    # 1. Zeile enthält Datum und Uhrzeit
    for i, ln in enumerate(lines):
        if event_date_str in ln and event_time_str in ln:
            return i

    # 2. Datum in Zeile i, Uhrzeit in Zeile i oder i+1
    date_idxs = [i for i, ln in enumerate(lines) if event_date_str in ln]
    if date_idxs:
        for i in date_idxs:
            if event_time_str in lines[i]:
                return i
            if i + 1 < len(lines) and event_time_str in lines[i + 1]:
                return i
            if i + 2 < len(lines) and event_time_str in lines[i + 2]:
                return i

        # Fallback: erste Zeile mit dem Event Datum
        return date_idxs[0]

    return None


def parse_heimat_event_detail_page(html: str, url: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True).replace("\xa0", " ")
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    full = "\n".join(lines)

    # Titel meist direkt nach "Zurück"
    title = None
    for i, ln in enumerate(lines):
        if ln.lower() == "zurück" and i + 1 < len(lines):
            title = lines[i + 1].strip()
            break

    if not title:
        if DEBUG:
            print(f"[DEBUG] No title for {url} first lines: {lines[:20]}")
        return None

    start_dt = None
    end_dt = None

    # Wir merken uns Startdatum und Startzeit als Strings, um die richtige Zeile zu finden
    event_date_str = None
    event_time_str = None

    # Format A: 06.03.2026 19:00 - 06.03.2026 21:00
    m = re.search(
        r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{1,2}):(\d{2})\s*[-–]\s*(\d{2})\.(\d{2})\.(\d{4})\s+(\d{1,2}):(\d{2})",
        full,
    )
    if m:
        start_dt = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)),
                            int(m.group(4)), int(m.group(5)), tzinfo=TZ)
        end_dt = datetime(int(m.group(8)), int(m.group(7)), int(m.group(6)),
                          int(m.group(9)), int(m.group(10)), tzinfo=TZ)
        event_date_str = f"{int(m.group(1)):02d}.{int(m.group(2)):02d}.{int(m.group(3))}"
        event_time_str = f"{int(m.group(4)):02d}:{int(m.group(5)):02d}"
    else:
        # Format B: 23.01.2026 19:00 - 21:00 Uhr
        m = re.search(
            r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{1,2}):(\d{2})\s*[-–]\s*(\d{1,2}):(\d{2})\s*Uhr",
            full,
            re.IGNORECASE,
        )
        if m:
            start_dt = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)),
                                int(m.group(4)), int(m.group(5)), tzinfo=TZ)
            end_dt = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)),
                              int(m.group(6)), int(m.group(7)), tzinfo=TZ)
            event_date_str = f"{int(m.group(1)):02d}.{int(m.group(2)):02d}.{int(m.group(3))}"
            event_time_str = f"{int(m.group(4)):02d}:{int(m.group(5)):02d}"
        else:
            # Format C: 23.01.2026 19:00 Uhr
            m = re.search(
                r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{1,2}):(\d{2})\s*Uhr",
                full,
                re.IGNORECASE,
            )
            if m:
                start_dt = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)),
                                    int(m.group(4)), int(m.group(5)), tzinfo=TZ)
                end_dt = None
                event_date_str = f"{int(m.group(1)):02d}.{int(m.group(2)):02d}.{int(m.group(3))}"
                event_time_str = f"{int(m.group(4)):02d}:{int(m.group(5)):02d}"
            else:
                # Format D: nur Datum
                m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", full)
                if m:
                    start_dt = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)),
                                        0, 0, tzinfo=TZ)
                    end_dt = None
                    event_date_str = f"{int(m.group(1)):02d}.{int(m.group(2)):02d}.{int(m.group(3))}"
                    event_time_str = "00:00"

    if not start_dt or not event_date_str or not event_time_str:
        if DEBUG:
            print(f"[DEBUG] No date/time for {url}")
            print(f"[DEBUG] First lines: {lines[:40]}")
        return None

    # Ort exakt aus der Zeile direkt unter der Event Datum und Uhrzeit Zeile
    location = None
    dt_idx = find_event_datetime_line_index(lines, event_date_str, event_time_str)

    if dt_idx is not None:
        for j in range(dt_idx + 1, min(dt_idx + 6, len(lines))):
            cand = lines[j].strip()
            if not is_obviously_not_location(cand):
                location = cand
                break

    # Fallback wenn Datum Zeile nicht gefunden wurde, suche nach der ersten Zeile mit dem Event Datum
    if not location:
        for i, ln in enumerate(lines):
            if event_date_str in ln:
                for j in range(i + 1, min(i + 6, len(lines))):
                    cand = lines[j].strip()
                    if not is_obviously_not_location(cand):
                        location = cand
                        break
                break

    if DEBUG and not location:
        print(f"[DEBUG] Location not found for {url}")
        print(f"[DEBUG] event_date_str={event_date_str} event_time_str={event_time_str}")
        print(f"[DEBUG] Lines around datetime:")
        if dt_idx is not None:
            lo = max(0, dt_idx - 3)
            hi = min(len(lines), dt_idx + 10)
            print(lines[lo:hi])
        else:
            print(lines[:50])

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
    ed: datetime | None = evt.get("end_dt")

    if ed:
        if sd.date() == ed.date():
            when = f"{sd.strftime('%d.%m.%Y %H:%M')} bis {ed.strftime('%H:%M')} Uhr"
        else:
            when = f"{sd.strftime('%d.%m.%Y %H:%M')} bis {ed.strftime('%d.%m.%Y %H:%M')} Uhr"
    else:
        when = f"{sd.strftime('%d.%m.%Y %H:%M')} Uhr"

    parts = [f"<b>{title}</b>", when]
    if loc:
        parts.append(loc)
    parts.append(f'<a href="{evt["url"]}">Details</a>')
    return "\n".join(parts)


def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

    today = datetime.now(TZ).date()

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
            else:
                if DEBUG:
                    print(f"[DEBUG] Parsed None for {u}")
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
