"""Flatfox — public JSON API (pins by bounding box, then details per listing)."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Iterator

from .. import floors
from ..models import Listing
from .base import get, session

log = logging.getLogger(__name__)

SOURCE = "flatfox"
BASE = "https://flatfox.ch"
# Bounding box: Lausanne + adjacent communes (Renens..Epalinges/Pully)
BBOX = {"west": 6.55, "east": 6.72, "south": 46.49, "north": 46.575}
CATEGORIES = {"APARTMENT", "HOUSE"}
# The pin endpoint silently truncates at 200 results, so any cell that comes
# back full is split into quadrants until every cell is under the cap.
PIN_CAP = 200
MAX_DEPTH = 5


def _pins(sess, bbox: dict, depth: int = 0) -> list[dict]:
    found = get(
        sess,
        f"{BASE}/api/v1/pin/",
        params={**bbox, "offer_type": "RENT",
                "object_category": "APARTMENT,HOUSE"},
    ).json()
    if len(found) < PIN_CAP or depth >= MAX_DEPTH:
        if len(found) >= PIN_CAP:
            log.warning("flatfox: cell still full at max depth, may be truncated")
        return found

    mid_lat = (bbox["south"] + bbox["north"]) / 2
    mid_lon = (bbox["west"] + bbox["east"]) / 2
    quadrants = [
        {"south": bbox["south"], "north": mid_lat, "west": bbox["west"], "east": mid_lon},
        {"south": bbox["south"], "north": mid_lat, "west": mid_lon, "east": bbox["east"]},
        {"south": mid_lat, "north": bbox["north"], "west": bbox["west"], "east": mid_lon},
        {"south": mid_lat, "north": bbox["north"], "west": mid_lon, "east": bbox["east"]},
    ]
    merged: dict[int, dict] = {}
    for q in quadrants:
        for pin in _pins(sess, q, depth + 1):
            merged[pin["pk"]] = pin
    return list(merged.values())


def fetch(seen: set[str] = frozenset()) -> Iterator[Listing]:
    sess = session()
    pins = _pins(sess, BBOX)
    log.info("flatfox: %d pins in bbox", len(pins))

    for pin in pins:
        pk = str(pin["pk"])
        if pk in seen:
            continue
        try:
            d = get(sess, f"{BASE}/api/v1/public-listing/{pk}/").json()
        except Exception as e:
            log.warning("flatfox detail %s failed: %s", pk, e)
            continue
        if d.get("object_category") not in CATEGORIES or d.get("status") != "act":
            continue
        rooms = d.get("number_of_rooms")
        yield Listing(
            source=SOURCE,
            source_id=pk,
            url=BASE + d["url"],
            title=d.get("public_title") or d.get("short_title") or "",
            price=d.get("price_display"),
            rooms=float(rooms) if rooms else None,
            surface=d.get("surface_living"),
            address=d.get("street") or "",
            zip_code=str(d.get("zipcode") or ""),
            city=d.get("city") or "",
            lat=d.get("latitude"),
            lon=d.get("longitude"),
            floor=_floor(d),
            published=_parse_dt(d.get("published") or d.get("created")),
        )


def _floor(d: dict) -> "int | None":
    """Floor from the structured field, falling back to the listing text.

    The field is the truth when present — one sampled listing carries floor=4
    while its description mentions a "1er étage" elsewhere in the building — but
    it is null for roughly a quarter of listings, and the description names the
    floor for most of those.
    """
    if d.get("floor") is not None:
        return d["floor"]
    return floors.from_text(f"{d.get('public_title') or ''} "
                            f"{d.get('description') or ''}")


def enrich(sess, listing) -> None:
    """Fill the floor on rows stored before it was collected.

    `fetch` already sets the floor from the same endpoint, so during a scan this
    is a no-op and costs nothing; it exists for `backfill-details`.
    """
    if listing.floor is not None:
        return
    d = get(sess, f"{BASE}/api/v1/public-listing/{listing.source_id}/").json()
    listing.floor = _floor(d)


def published_for(sess, listing):
    """Publication date from the public listing endpoint."""
    try:
        d = get(sess, f"{BASE}/api/v1/public-listing/{listing.source_id}/").json()
    except Exception as e:
        log.warning("flatfox date fetch failed for %s: %s", listing.source_id, e)
        return None
    return _parse_dt(d.get("published") or d.get("created"))


def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ls = list(fetch())
    print(len(ls), "listings")
    for l in ls[:8]:
        print(l.uid, l.price, l.rooms, l.address, l.zip_code, l.city)
