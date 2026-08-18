"""acheter-louer.ch — syndicates Homegate inventory (which blocks us directly).

Each commune results page embeds a ld+json ItemList holding *every* listing URL
(not just the ~80 rendered cards), so one request per commune yields the full id
set; detail pages are then fetched only for ids we have never seen.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Iterator

from bs4 import BeautifulSoup

from .. import floors
from ..models import Listing
from .base import get, session

log = logging.getLogger(__name__)

SOURCE = "acheter_louer"
BASE = "https://www.acheter-louer.ch"
RESULTS_URL = (BASE + "/immobilier-louer/{commune}-immobilier-location-"
               "maison-appartement-villa-chalet-appartement-terrain.html")
COMMUNES = ["lausanne", "renens", "prilly", "pully", "ecublens",
            "chavannes-pres-renens", "crissier", "epalinges",
            "le-mont-sur-lausanne", "jouxtens-mezery"]
# URL path segment right before the commune tells us the object type.
WANTED_TYPES = {"appartement", "maison", "villa", "studio", "duplex", "attique"}
# Cap detail fetches per run so a first/seed run cannot hang forever.
MAX_DETAILS = 400


def fetch(seen: set[str] = frozenset()) -> Iterator[Listing]:
    sess = session()
    urls: dict[str, str] = {}
    for commune in COMMUNES:
        try:
            html = get(sess, RESULTS_URL.format(commune=commune)).text
        except Exception as e:
            log.warning("acheter-louer %s failed: %s", commune, e)
            continue
        for url in _listing_urls(html):
            m = re.search(r"-(\d+)\.html$", url)
            if m:
                urls.setdefault(m.group(1), url)
    log.info("acheter-louer: %d listing urls across communes", len(urls))

    todo = [(sid, u) for sid, u in urls.items() if sid not in seen]
    if len(todo) > MAX_DETAILS:
        log.info("acheter-louer: capping detail fetches at %d (of %d); "
                 "the rest are picked up on the next run",
                 MAX_DETAILS, len(todo))
        todo = todo[:MAX_DETAILS]

    for sid, url in todo:
        try:
            listing = _detail(sess, sid, url)
        except Exception as e:
            log.warning("acheter-louer detail %s failed: %s", sid, e)
            continue
        if listing:
            yield listing


def _listing_urls(html: str) -> list[str]:
    """Pull every listing URL out of the page's ld+json.

    The results ItemList sits nested under `mainEntity` of a SearchResultsPage,
    so walk the whole document rather than matching on the top-level @type.
    """
    out = []
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>',
                         html, re.S):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        for item in _walk_item_lists(data):
            ref = item.get("item")
            ref = ref.get("@id", "") if isinstance(ref, dict) else ""
            url = ref.split("#")[0]
            if "/location-immobilier/" in url and _type_of(url) in WANTED_TYPES:
                out.append(url)
    return out


def _walk_item_lists(node) -> list[dict]:
    """Yield every entry of every itemListElement found anywhere in the tree."""
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "itemListElement" and isinstance(value, list):
                found.extend(v for v in value if isinstance(v, dict))
            else:
                found.extend(_walk_item_lists(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_walk_item_lists(value))
    return found


def _type_of(url: str) -> str:
    """Object type is the path segment preceding the commune slug."""
    parts = url.rstrip("/").split("/")
    return parts[-3].lower() if len(parts) >= 3 else ""


def enrich(sess, listing) -> None:
    """Fill the floor on rows stored before it was collected.

    `_detail` already sets it from the same page, so during a scan this is a
    no-op; it exists for `backfill-details`.
    """
    if listing.floor is not None:
        return
    soup = BeautifulSoup(get(sess, listing.url).text, "lxml")
    listing.floor = _floor(soup)


def published_for(sess, listing):
    """Publication date via the same detail page the scraper already parses."""
    try:
        parsed = _detail(sess, listing.source_id, listing.url)
    except Exception as e:
        log.warning("acheter-louer date fetch failed for %s: %s", listing.source_id, e)
        return None
    return parsed.published if parsed else None


def _parse_dt(value):
    """acheter-louer stamps dates as "2026-03-01 13:55:03.0"."""
    if not value:
        return None
    try:
        return datetime.strptime(value.split(".")[0].strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _detail(sess, sid: str, url: str) -> Listing | None:
    html = get(sess, url).text
    about = None
    posted = None
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>',
                         html, re.S):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if data.get("@type") == "RealEstateListing" and data.get("about"):
            about = data["about"][0]
            posted = _parse_dt(data.get("datePosted"))
            break
    if not about:
        return None

    addr = about.get("address") or {}
    rooms = (about.get("numberOfRooms") or {}).get("value")
    surface = (about.get("floorSize") or {}).get("value")
    soup = BeautifulSoup(html, "lxml")
    price = None
    for el in soup.select(".price"):
        m = re.search(r"([\d'’]{3,})", el.get_text(" ", strip=True))
        if m:
            price = int(re.sub(r"[^\d]", "", m.group(1)))
            break

    return Listing(
        source=SOURCE,
        source_id=sid,
        url=url,
        title=about.get("name") or "",
        price=price,
        rooms=float(rooms) if rooms else None,
        surface=int(surface) if surface else None,
        address=addr.get("streetAddress") or "",
        zip_code=str(addr.get("postalCode") or ""),
        city=addr.get("addressLocality") or "",
        floor=_floor(soup),
        published=posted,
    )


FLOOR_LABEL = re.compile(r"[ée]tage", re.I)
# Only the listing's own description. The page also renders thumbnails of
# related listings (div.vign-desc), and reading the floor off the whole document
# picks up a *neighbouring* flat's étage — verified on listing 1897734, whose
# vignettes advertise a "4ème étage" that is not this flat.
DESCRIPTION_SEL = "div.content > p.all-lh.all-text"


def _floor(soup) -> "int | None":
    """Floor from the "Données techniques" table, else from the description.

    The table ships it as its own row — `<td>Etage</td><td>4</td>`, or "Rez" —
    which is worth preferring over the prose, but plenty of listings omit the
    row and only mention the floor in the description text.
    """
    for cell in soup.find_all("td"):
        if not FLOOR_LABEL.search(cell.get_text(" ", strip=True)):
            continue
        value = cell.find_next_sibling("td")
        if value is None:
            continue
        floor = _floor_value(value.get_text(" ", strip=True))
        if floor is not None:
            return floor
    description = soup.select_one(DESCRIPTION_SEL)
    return floors.from_text(description.get_text(" ", strip=True)) if description else None


def _floor_value(text: str) -> "int | None":
    """Parse the table cell's value, which is a figure rather than a sentence.

    It holds a bare number ("4"), so `floors.from_text` — which wants the word
    "étage" next to the digit — cannot read it. Words like "Rez" still go
    through the text parser.
    """
    m = re.fullmatch(r"(-?\d{1,2})(?:\s*(?:er|ème|eme|e))?", text.strip())
    return int(m.group(1)) if m else floors.from_text(text)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ls = list(fetch())
    print(len(ls), "listings")
    for l in ls[:8]:
        print(l.uid, l.price, l.rooms, l.surface, "|", l.address, l.zip_code, l.city)
