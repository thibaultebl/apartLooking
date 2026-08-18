"""immobilier.ch — server-rendered HTML cards (incl. lat/lng), paginated."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from html import unescape
from typing import Iterator

from bs4 import BeautifulSoup

from .. import floors
from ..models import Listing
from .base import REGION_ZIPS, get, session

log = logging.getLogger(__name__)

SOURCE = "immobilier_ch"
BASE = "https://www.immobilier.ch"
# (property type, commune slug) combinations to scan
# Only slugs this portal actually recognises. An unknown slug does NOT 404 —
# it silently falls back to canton-wide results, which is how listings in
# Avenches and the Vallée de Joux once ended up in a Lausanne search. Verify
# any addition returns on-target cities before adding it here.
SEARCHES = [
    ("appartement", c) for c in
    ["lausanne", "renens-vd", "prilly", "pully", "ecublens-vd",
     "chavannes-pres-renens", "crissier", "epalinges"]
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
        zip_code, city, address = _parse_location(loc)
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
            address=address,
            zip_code=zip_code,
            city=city,
            lat=lat,
            lon=lon,
            # The slug names the floor on roughly a fifth of listings, for free.
            # `enrich` fetches the rest from the detail page, for candidates only.
            floor=floors.from_text(link["href"]),
        ))
    return out


def _parse_location(loc: str) -> tuple[str, str, str]:
    """Split the card's location line into (zip, city, address).

    It comes in three shapes, all seen on one page of results:

        "1007 Lausanne, Contactez l'agence pour l'adresse"   NPA, no address
        "Lausanne, Chemin des Sauges 30"                     address, no NPA
        "Lausanne, Rte de Berne 73, 1010 Lausanne"           NPA in the tail

    Only about one card in ten carries an NPA at all, so this is a cheap partial
    win rather than a substitute for `enrich`. Matching is restricted to known
    regional postcodes so that a street number can never be read as one.
    """
    city, _, address = loc.partition(",")
    zip_code = ""
    for candidate in re.findall(r"\b(\d{4})\b", loc):
        if candidate in REGION_ZIPS:
            zip_code = candidate
            break
    # Shape 1 leaves the NPA glued to the city ("1007 Lausanne"); strip it.
    city = re.sub(r"^\s*\d{4}\s+", "", city).strip()
    return zip_code, city, address.strip()


def _fetch_detail(sess, url: str) -> tuple[dict, str]:
    """The listing's ld+json plus the page's og:title.

    Everything this portal is asked for lives in <head> — the <body> is rendered
    client-side and arrives empty, so there is nothing to gain from a browser.
    """
    html = get(sess, url).text
    data: dict = {}
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>',
                         html, re.S):
        try:
            parsed = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if parsed.get("@type") == "RealEstateListing":
            data = parsed
            break
    og = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]*)"', html)
    return data, unescape(og.group(1)) if og else ""


def _posted(data: dict) -> "datetime | None":
    raw = data.get("datePosted")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw[:19])
    except ValueError:
        return None


def published_for(sess, listing) -> "datetime | None":
    """Publication date, which this portal only exposes on the detail page."""
    try:
        data, _ = _fetch_detail(sess, listing.url)
    except Exception as e:
        log.warning("immobilier.ch date fetch failed for %s: %s", listing.source_id, e)
        return None
    return _posted(data)


def enrich(sess, listing) -> None:
    """Fill in what the search card withholds: postal code, address, floor, date.

    This portal ships no NPA on its cards, so without this step every one of its
    listings fails a postal-code criterion and the whole source goes dark. One
    request settles that, the floor, and the publication date — which this
    source otherwise has none of at all.

    Called for candidates only (see geo.could_match), so it costs a handful of
    requests per run rather than one per listing.
    """
    data, og_title = _fetch_detail(sess, listing.url)
    about = data.get("about") or {}
    if isinstance(about, list):
        about = about[0] if about else {}
    address = about.get("address") or {}

    if not listing.zip_code:
        listing.zip_code = str(address.get("postalCode") or "")
    if not listing.street:
        listing.address = address.get("streetAddress") or listing.address
    if listing.floor is None:
        # og:title reads "Appartement 4 pièces au 2ème étage - 1003 Lausanne";
        # `about.name` is the same headline without the locality.
        listing.floor = floors.from_text(f"{og_title} {about.get('name') or ''}")
    if listing.published is None:
        listing.published = _posted(data)


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
        print(l.uid, l.price, l.rooms, l.surface, "|", l.address,
              l.zip_code, l.city, "| floor", l.floor)
    with_floor = sum(1 for l in ls if l.floor is not None)
    with_zip = sum(1 for l in ls if l.zip_code)
    print(f"from the search cards alone: {with_zip} have an NPA, "
          f"{with_floor} a floor (enrich fetches the rest, for candidates)")
