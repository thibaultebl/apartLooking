"""petitesannonces.ch — rubrique 270124 (Immobilier > Location > Vaud)."""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Iterator

from bs4 import BeautifulSoup

from ..models import Listing
from .base import REGION_ZIPS, get, session

log = logging.getLogger(__name__)

SOURCE = "petitesannonces"
BASE = "https://www.petitesannonces.ch"
RUBRIQUE = "270124"  # Immobilier > Location > Vaud
TYPES = {"Appartement", "Maison / Villa", "Studio", "Chambre"}
MAX_PAGES = 10  # sorted by date desc; 20/page


def fetch(seen: set[str] = frozenset()) -> Iterator[Listing]:
    sess = session()
    emitted: set[str] = set()
    for page in range(1, MAX_PAGES + 1):
        try:
            # The server declares utf-8 correctly; do not override it.
            html = get(sess, f"{BASE}/r/{RUBRIQUE}/", params={"p": page}).text
        except Exception as e:
            log.warning("petitesannonces p%d failed: %s", page, e)
            break
        rows = _parse_page(html)
        if not rows:
            break
        for l in rows:
            if l.source_id not in emitted:
                emitted.add(l.source_id)
                yield l


def _parse_page(html: str) -> list[Listing]:
    soup = BeautifulSoup(html, "lxml")
    out = []
    for row in soup.select("div.ele"):
        link = row.select_one(".elha a[href^='/a/']")
        if not link:
            continue
        obj_type = _text(row.select_one(".elht"))
        if obj_type not in TYPES:
            continue
        addr_full = _text(link)  # "1003 Lausanne, Rue Saint-Pierre 2"
        m = re.match(r"(\d{4})\s+([^,]+)(?:,\s*(.*))?", addr_full)
        if not m or m.group(1) not in REGION_ZIPS:
            continue
        zip_code, city, street = m.group(1), m.group(2).strip(), (m.group(3) or "").strip()
        sid = link["href"].strip("/").split("/")[-1]
        price_txt = _text(row.select_one(".elsp"))
        price_m = re.search(r"[\d']+", price_txt)
        rooms_txt = _text(row.select_one(".elhr"))
        surface_txt = _text(row.select_one(".elhs"))
        out.append(Listing(
            source=SOURCE,
            source_id=sid,
            url=f"{BASE}/a/{sid}",
            title=f"{obj_type} — {addr_full}",
            price=int(price_m.group(0).replace("'", "")) if price_m else None,
            rooms=float(rooms_txt) if re.fullmatch(r"\d+(\.\d)?", rooms_txt) else None,
            surface=int(surface_txt) if surface_txt.isdigit() else None,
            address=street,
            zip_code=zip_code,
            city=city,
            published=_parse_date(_text(row.select_one(".elss"))),
        ))
    return out


def _parse_date(value: str):
    """Dates come as "11.08" with no year; assume the most recent such date."""
    m = re.fullmatch(r"(\d{2})\.(\d{2})", value.strip())
    if not m:
        return None
    day, month = int(m.group(1)), int(m.group(2))
    today = datetime.now()
    for year in (today.year, today.year - 1):
        try:
            dt = datetime(year, month, day)
        except ValueError:
            return None
        if dt <= today:
            return dt
    return None


def _text(el) -> str:
    return el.get_text(" ", strip=True) if el else ""


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ls = list(fetch())
    print(len(ls), "listings")
    for l in ls[:8]:
        print(l.uid, l.price, l.rooms, l.address, l.zip_code, l.city)
