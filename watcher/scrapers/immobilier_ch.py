"""immobilier.ch — server-rendered HTML cards (incl. lat/lng), paginated."""
from __future__ import annotations

import logging
import re
from typing import Iterator

from bs4 import BeautifulSoup

from ..models import Listing
from .base import get, session

log = logging.getLogger(__name__)

SOURCE = "immobilier_ch"
BASE = "https://www.immobilier.ch"
# (property type, commune slug) combinations to scan
SEARCHES = [
    ("appartement", c) for c in
    ["lausanne", "renens-vd", "prilly", "pully", "ecublens-vd",
     "chavannes-pres-renens", "crissier", "epalinges", "le-mont-sur-lausanne"]
] + [("maison", "lausanne")]
MAX_PAGES = 30


def fetch(seen: set[str] = frozenset()) -> Iterator[Listing]:
    sess = session()
    emitted: set[str] = set()
    for prop_type, commune in SEARCHES:
        for page in range(1, MAX_PAGES + 1):
            url = f"{BASE}/fr/louer/{prop_type}/vaud/{commune}/page-{page}"
            try:
                html = get(sess, url).text
            except Exception as e:
                log.warning("immobilier.ch %s p%d failed: %s", commune, page, e)
                break
            items = _parse_page(html)
            for l in items:
                if l.source_id not in emitted:
                    emitted.add(l.source_id)
                    yield l
            if len(items) < 20:  # last page
                break


def _parse_page(html: str) -> list[Listing]:
    soup = BeautifulSoup(html, "lxml")
    out = []
    for card in soup.select("div.filter-item[data-id]"):
        sid = card["data-id"]
        link = card.select_one("a[href*='/fr/louer/']")
        if not link:
            continue
        price = _parse_price(_text(card.select_one("strong.title")))
        obj_type = _text(card.select_one("p.object-type"))
        rooms_m = re.search(r"(\d+(?:[.,]\d)?)\s*pi", obj_type)
        loc = _text(card.select_one("div.filter-item-content > p:not(.object-type)"))
        city, _, address = loc.partition(",")
        surface_m = re.search(r"(\d+)\s*m", _text(card.select_one("span.space")))
        lat = lon = None
        if card.get("data-latlng"):
            try:
                lat, lon = (float(x) for x in card["data-latlng"].split(","))
            except ValueError:
                pass
        out.append(Listing(
            source=SOURCE,
            source_id=sid,
            url=BASE + link["href"],
            title=obj_type or _text(link),
            price=price,
            rooms=float(rooms_m.group(1).replace(",", ".")) if rooms_m else None,
            surface=int(surface_m.group(1)) if surface_m else None,
            address=address.strip(),
            city=city.strip(),
            lat=lat,
            lon=lon,
        ))
    return out


def _text(el) -> str:
    return el.get_text(" ", strip=True) if el else ""


def _parse_price(s: str):
    m = re.search(r"CHF\s*([\d'’’]+)", s)
    return int(re.sub(r"[^\d]", "", m.group(1))) if m else None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ls = list(fetch())
    print(len(ls), "listings")
    for l in ls[:8]:
        print(l.uid, l.price, l.rooms, l.surface, l.address, l.city, l.lat)
