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


def looks_like_location(line: str) -> bool:
    s = (line or "").strip()
    if len(s) < 4:
        return False

    low = s.lower()

    if low in {"details", "zurück"}:
        return False

    if s.startswith(","):
        return False

    if re.fullmatch(DATE_RE, s):
        return False

    if re.search(rf"{DATE_RE}\s+\d{{1,2}}:\d{{2}}", s):
        return False

    if re.search(r"\b\d{1,2}:\d{2}\b", s) and "uhr" in low:
        return False

    if re.search(r"\b\d{5}\b", s):
        left = re.split(r"\b\d{5}\b", s, maxsplit=1)[0].strip()
        if not re.search(r"[A-Za-zÄÖÜäöü]", left):
            return False

    if re.search(r"\b\d{5}\b", s):
        return True

    if any(k in low for k in ["straße", "str.", "platz", "weg", "gasse", "markt", "allee", "ring", "hof", "damm"]):
        return True

    if "," in s and re.search(r"[A-Za-zÄÖÜäöü]", s):
        return True

    return False


def location_score(line: str) -> int:
    s = (line or "").strip()
    low = s.lower()
    score = 0

    if s.startswith(","):
        score -= 100

    if re.match(r"^[A-Za-zÄÖÜäöü]", s):
        score += 10

    if re.search(r"\b\d{5}\b", s):
        score += 5

    if any(k in low for k in ["straße", "str.", "platz", "weg", "gasse", "allee", "markt"]):
        score += 5

    if low.strip() == "witzenhausen":
        score -= 5

    return score


def parse_heimat_event_detail_page(html: str, url: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True).replace("\xa0", " ")
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    full = "\n".join(lines)

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

    m = re.search(
        r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{1,2}):(\d{2})\s*[-–]\s*(\d{2})\.(\d{2})\.(\d{4})\s+(\d{1,2}):(\d{2})",
        full,
    )
    if m:
        start_dt = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)),
                            int(m.group(4)), int(m.group(5)), tzinfo=TZ)
        end_dt = datetime(int(m.group(8)), int(m.group(7)), int(m.group(6)),
                          int(m.group(9)), int(m.group(10)), tzinfo=TZ)
    else:
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
        else:
            m = re.search(
                r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{1,2}):(\d{2})\s*Uhr",
                full,
                re.IGNORECASE,
            )
            if m:
                start_dt = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)),
                                    int(m.group(4)), int(m.group(5)), tzinfo=TZ)
                end_dt = None
            else:
                m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", full)
                if m:
                    start_dt = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)),
                                        0, 0, tzinfo=TZ)
                    end_dt = None

    if not start_dt:
        if DEBUG:
            print(f"[DEBUG] No date/time for {url}")
            print(f"[DEBUG] First lines: {lines[:30]}")
        return None

    candidates = []
    date_line_idx = None
    for i, ln in enumerate(lines):
        if re.search(DATE_RE, ln):
            date_line_idx = i
            break

    if date_line_idx is not None:
        for j in range(date_line_idx + 1, min(date_line_idx + 40, len(lines))):
            cand = lines[j]
            if looks_like_location(cand):
                candidates.append(cand)

    if not candidates:
        for cand in lines:
            if looks_like_location(cand):
                candidates.append(cand)

    location = None
    if candidates:
        candidates.sort(key=location_score, reverse=True)
        location = candidates[0]

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
