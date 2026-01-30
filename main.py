import os
import re
import json
import time
from datetime import datetime, date
from urllib.parse import urlsplit, urlunsplit
from dateutil.relativedelta import relativedelta

import requests
from bs4 import BeautifulSoup


BASE_LIST_URL = "https://www.witzenhausen.eu/veranstaltungen/"
STATE_FILE = "state.json"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WitzenhausenEventsBot/1.0; +https://github.com/)",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}

MONTH_MAP = {
    "jan": 1, "januar": 1,
    "feb": 2, "februar": 2,
    "mär": 3, "mrz": 3, "märz": 3, "maerz": 3,
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


def add_query_param(base_url: str, key: str, value: str) -> str:
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}{key}={value}"


def fetch(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def strip_query(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def is_event_detail_url(url: str) -> bool:
    p = urlsplit(url).path.strip("/").split("/")
    if len(p) < 2:
        return False
    if p[0] != "veranstaltungen":
        return False
    if p[1] in ("kategorie", "tag", "seite"):
        return False
    return True


def extract_event_urls_from_list_page(html: str) -> set[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls = set()

    candidates = []

    for a in soup.find_all("a"):
        href = (a.get("href") or "").strip()
        if href:
            candidates.append(href)
        dh = (a.get("data-href") or "").strip()
        if dh:
            candidates.append(dh)
        du = (a.get("data-url") or "").strip()
        if du:
            candidates.append(du)

    for href in candidates:
        if href.startswith("/"):
            href = "https://www.witzenhausen.eu" + href

        href = strip_query(href).rstrip("/") + "/"

        if href.rstrip("/") == BASE_LIST_URL.rstrip("/"):
            continue

        if not href.startswith("https://www.witzenhausen.eu/veranstaltungen/"):
            continue

        if not is_event_detail_url(href):
            continue

        urls.add(href)

    return urls


def parse_german_date(d: str) -> date | None:
    d = d.replace("..", ".").strip()
    m = re.search(r"\b(\d{1,2})\.\s*([A-Za-zÄÖÜäöü]{3,9})\.?\s*(\d{4})\b", d)
    if not m:
        return None
    day = int(m.group(1))
    mon_raw = m.group(2).strip().lower()
    mon = MONTH_MAP.get(mon_raw)
    if not mon:
        return None
    year = int(m.group(3))
    return date(year, mon, day)


def extract_start_time(text: str) -> str | None:
    # Fälle wie "08:30 bis 11:30 Uhr" oder "18.00 – 20.00 Uhr"
    m = re.search(r"\b(\d{1,2})[.:](\d{2})\s*(?:uhr)?\s*(?:bis|–|-)\s*(\d{1,2})[.:](\d{2})\s*uhr\b", text, re.IGNORECASE)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    m = re.search(r"\b(\d{1,2})[.:](\d{2})\s*uhr\b", text, re.IGNORECASE)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return None
