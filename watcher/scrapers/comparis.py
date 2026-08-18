"""comparis.ch — aggregator; the only source that publishes a floor directly.

Reached through the Next.js payload embedded in the results page, which carries
the full listing objects (address, rooms, surface, floor, price, date and
coordinates) with no detail fetch needed.

**This source is deliberately limited to a single request per run.** Its WAF
answers 403 to essentially everything except a bare first hit on the results
path: `?page=N` is refused, so are all other query parameters, so are the
commune and property-type path variants, and so are the /details/show/ pages.
Only the unparameterised results URL returns. Measured behaviour, not a guess.

That still works, because the payload is ordered newest-first and the watcher
runs every 30 minutes: ten listings per run is far more headroom than Lausanne
produces. What it costs is depth — comparis reports ~2500 active listings in
Lausanne and we only ever see the newest ten, so this source will never seed
history the way the others do. It earns its place on new listings only.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Iterator

from .. import floors
from ..models import Listing
from .base import get, session

log = logging.getLogger(__name__)

SOURCE = "comparis"
BASE = "https://www.comparis.ch"
# Communes are path segments, but only this one answers; the others 403. The
# region filter in geo.py discards anything that turns out to be too far.
RESULTS_URL = f"{BASE}/immobilien/marktplatz/lausanne/mieten"
DETAIL_URL = f"{BASE}/immobilien/marktplatz/details/show/{{ad_id}}"
NEXT_DATA = re.compile(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
# DealType 10 is renting; the path already selects it, this guards against a
# payload change silently letting sales in.
DEAL_TYPE_RENT = 10
# The results mix homes with commercial space and parking (a 130 CHF
# "Tiefgarage", a 267 m² "Gewerbeobjekt"). Filtering is by exclusion rather than
# a whitelist on purpose: an unrecognised *residential* type would cost a flat,
# while an unrecognised commercial one only adds a row to the digest that no
# criterion can match. Rejections are logged so a new type is visible.
NON_RESIDENTIAL = re.compile(
    r"gewerbe|b[üu]ro|garage|parkplatz|einstellhalle|lager|depot"
    r"|laden|verkaufs|praxis|atelier|gastro|hotel|industrie",
    re.I,
)


def fetch(seen: set[str] = frozenset()) -> Iterator[Listing]:
    sess = session()
    try:
        html = get(sess, RESULTS_URL).text
    except Exception as e:
        log.warning("comparis results page failed: %s", e)
        return

    items = _result_items(html)
    if not items:
        log.warning("comparis: no listings in the payload — markup may have changed")
        return
    log.info("comparis: %d listings in the newest-first page", len(items))

    skipped: list[str] = []
    for item in items:
        kind = str(item.get("PropertyTypeText") or "")
        if NON_RESIDENTIAL.search(kind):
            skipped.append(kind)
            continue
        listing = _to_listing(item)
        if listing is not None:
            yield listing
    if skipped:
        log.info("comparis: skipped %d non-residential (%s)",
                 len(skipped), ", ".join(sorted(set(skipped))))


def _result_items(html: str) -> list[dict]:
    """The hydrated listing objects out of the Next.js payload."""
    match = NEXT_DATA.search(html)
    if not match:
        return []
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []
    result = (data.get("props", {}).get("pageProps", {})
                  .get("initialResultData") or {})
    return [i for i in result.get("resultItems") or [] if isinstance(i, dict)]


def _to_listing(item: dict) -> "Listing | None":
    ad_id = item.get("AdId")
    if ad_id is None or item.get("DealType") != DEAL_TYPE_RENT:
        return None

    street, zip_code, city = _address(item.get("Address"))
    rooms, surface, floor = _essentials(item.get("EssentialInformation"))
    rooms = _reconcile_rooms(rooms, item.get("Title"))
    if floor is None:
        floor = floors.from_text(f"{item.get('Title') or ''} "
                                 f"{item.get('Remarks') or ''}")
    coord = item.get("Coordinate") or {}

    return Listing(
        source=SOURCE,
        source_id=str(ad_id),
        url=DETAIL_URL.format(ad_id=ad_id),
        title=item.get("Title") or "",
        price=_price(item),
        rooms=rooms,
        surface=surface,
        address=street,
        zip_code=zip_code,
        city=city,
        lat=coord.get("Latitude"),
        lon=coord.get("Longitude"),
        floor=floor,
        published=_parse_dt(item.get("Date")),
    )


def _address(address) -> tuple[str, str, str]:
    """["Av. de Sévelin 6", "1004 Lausanne"] -> street, zip, city.

    The first line is sometimes just the town again ("Lausanne", "1004
    Lausanne") when the agency withholds the street; that is not an address, so
    it is dropped rather than stored as one.
    """
    lines = [str(x).strip() for x in (address or []) if str(x).strip()]
    if not lines:
        return "", "", ""
    zip_code = city = ""
    for line in lines:
        m = re.match(r"^(\d{4})\s+(.+)$", line)
        if m:
            zip_code, city = m.group(1), m.group(2).strip()
            break
    street = ""
    for line in lines:
        if not re.match(r"^\d{4}\s", line) and line != city:
            street = line
            break
    return street, zip_code, city


def _essentials(essentials) -> tuple["float | None", "int | None", "int | None"]:
    """["2.5 Zimmer", "75 m²", "1. Etage"] -> rooms, surface, floor.

    The three facts are positional only by convention — plenty of listings
    carry just one or two of them ("267 m²", "EG") — so each is matched by
    shape rather than by index.
    """
    rooms = surface = floor = None
    for raw in essentials or []:
        text = str(raw)
        if rooms is None:
            m = re.search(r"(\d+(?:[.,]\d)?)\s*(?:Zimmer|pi[eè]ces?|Zi\b)", text, re.I)
            if m:
                rooms = float(m.group(1).replace(",", "."))
                continue
        if surface is None:
            m = re.search(r"(\d+(?:[.,]\d+)?)\s*m[²2]", text, re.I)
            if m:
                surface = int(float(m.group(1).replace(",", ".")))
                continue
        if floor is None:
            floor = floors.from_text(text)
    return rooms, surface, floor


PIECES_IN_TITLE = re.compile(r"(\d+(?:[.,]\d)?)\s*(?:-\s*)?pi[eè]ces?", re.I)


def _reconcile_rooms(rooms: "float | None", title) -> "float | None":
    """Prefer a room count the ad states in pièces over the Zimmer field.

    The two disagree on real listings — one ad titled "Appartement 3,5 pièces"
    is filed as "2.5 Zimmer". Romandie quotes flats in pièces and the criteria
    are written in pièces, so the ad's own wording wins; deferring to the
    normalised German count would silently drop the flat from an exact match.
    """
    m = PIECES_IN_TITLE.search(str(title or ""))
    if not m:
        return rooms
    stated = float(m.group(1).replace(",", "."))
    if rooms is not None and abs(stated - rooms) > 1e-6:
        log.info("comparis: title says %g pièces but the listing is filed as "
                 "%g Zimmer — trusting the title", stated, rooms)
    return stated


def _price(item: dict) -> "int | None":
    """Monthly rent, or None when the agency wrote "auf Anfrage".

    PriceValue is 0 rather than null for price-on-request listings, and 0 would
    sail through a max_price criterion, so it is treated as unknown.
    """
    value = item.get("PriceValue")
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    m = re.search(r"[\d'’]{3,}", str(item.get("Price") or ""))
    return int(re.sub(r"[^\d]", "", m.group(0))) if m else None


def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:19])
    except ValueError:
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ls = list(fetch())
    print(len(ls), "listings")
    for l in ls:
        print(f"  {l.source_id}  {str(l.price):>5} CHF  {str(l.rooms):>4} pces  "
              f"{str(l.surface):>4} m²  floor={str(l.floor):>4}  "
              f"{l.zip_code} {l.city} — {l.address[:30]}")
    print(f"with a floor: {sum(1 for l in ls if l.floor is not None)}/{len(ls)}")
