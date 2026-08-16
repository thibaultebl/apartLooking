"""Geocoding (Nominatim) and walking time to Lausanne station (OSRM)."""
from __future__ import annotations

import logging
import math
import time
from typing import Optional

import requests

from . import db
from .models import Listing

log = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# Free public routers, tried in order. Both are shared community instances that
# rate-limit and occasionally go away, so neither is trusted on its own.
# (The main OSRM demo server is deliberately absent: it only routes by car and
# silently returns driving times for a /foot/ request.)
OSRM_URL = ("https://routing.openstreetmap.de/routed-foot/route/v1/foot/"
            "{lon1},{lat1};{lon2},{lat2}")
VALHALLA_URL = "https://valhalla1.openstreetmap.de/route"
USER_AGENT = "lausanne-rental-watcher/1.0 (personal apartment search)"

# Walking assumptions for the last-resort estimate.
WALK_KMH = 4.8
DETOUR_FACTOR = 1.35  # street network vs straight line, Lausanne is not a grid

_last_nominatim_call = 0.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def geocode(conn, query: str) -> Optional[tuple[float, float]]:
    """Geocode an address string, permanently cached. Respects 1 req/s."""
    global _last_nominatim_call
    cached = db.cached_geocode(conn, query)
    if cached is not None:
        return None if cached[0] is None else cached

    wait = 1.1 - (time.time() - _last_nominatim_call)
    if wait > 0:
        time.sleep(wait)
    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={"q": query, "format": "json", "limit": 1, "countrycodes": "ch"},
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        _last_nominatim_call = time.time()
        resp.raise_for_status()
        results = resp.json()
    except Exception as e:
        log.warning("geocode failed for %r: %s", query, e)
        return None  # transient failure: do NOT cache

    if not results:
        db.store_geocode(conn, query, None, None)  # cache "unresolvable"
        return None
    lat, lon = float(results[0]["lat"]), float(results[0]["lon"])
    db.store_geocode(conn, query, lat, lon)
    return (lat, lon)


def _route_osrm(lat, lon, station_lat, station_lon) -> Optional[float]:
    resp = requests.get(
        OSRM_URL.format(lat1=lat, lon1=lon, lat2=station_lat, lon2=station_lon),
        params={"overview": "false"},
        headers={"User-Agent": USER_AGENT},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "Ok" or not data.get("routes"):
        return None
    return data["routes"][0]["duration"] / 60.0


def _route_valhalla(lat, lon, station_lat, station_lon) -> Optional[float]:
    resp = requests.post(
        VALHALLA_URL,
        json={"locations": [{"lat": lat, "lon": lon},
                            {"lat": station_lat, "lon": station_lon}],
              "costing": "pedestrian", "units": "kilometers"},
        headers={"User-Agent": USER_AGENT},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()["trip"]["summary"]["time"] / 60.0


ROUTERS = (("osrm", _route_osrm), ("valhalla", _route_valhalla))

# These are donated community servers; hammering them gets the caller's IP
# null-routed (which is exactly what happened to us during development).
ROUTER_MIN_INTERVAL = 1.0
_last_route_call = 0.0


def _throttle() -> None:
    global _last_route_call
    wait = ROUTER_MIN_INTERVAL - (time.time() - _last_route_call)
    if wait > 0:
        time.sleep(wait)
    _last_route_call = time.time()


def walk_minutes_to(conn, lat: float, lon: float, station_lat: float,
                    station_lon: float) -> tuple[Optional[float], bool]:
    """Walking minutes to the station. Returns (minutes, is_estimate).

    Tries each public router in turn. If they are all unreachable we fall back
    to a distance estimate rather than returning nothing: an unknown walk time
    would silently fail the criterion and cost the user a flat, which is a far
    worse outcome than an occasional imprecise alert.
    """
    key = f"{lat:.5f},{lon:.5f}"
    cached = db.cached_walk(conn, key)
    if cached is not None:
        return cached, False

    for name, router in ROUTERS:
        try:
            _throttle()
            minutes = router(lat, lon, station_lat, station_lon)
        except Exception as e:
            log.warning("router %s failed for %s: %s", name, key, e)
            continue
        if minutes is not None:
            db.store_walk(conn, key, minutes)
            return minutes, False

    dist = haversine_km(lat, lon, station_lat, station_lon)
    estimate = dist * DETOUR_FACTOR / WALK_KMH * 60
    log.warning("all routers unavailable for %s — estimating %.1f min", key, estimate)
    return estimate, True  # deliberately not cached: retry properly next run


def resolve_listing(conn, l: Listing, config: dict) -> None:
    """Fill l.lat/lon (if missing) and l.walk_minutes. Best-effort."""
    station = config["station"]
    cutoff_km = config.get("haversine_cutoff_km", 2.2)

    if l.lat is None or l.lon is None:
        query = l.location_str
        if not query:
            return
        coords = geocode(conn, query)
        if coords is None:
            return
        l.lat, l.lon = coords

    dist = haversine_km(l.lat, l.lon, station["lat"], station["lon"])
    if dist > cutoff_km:
        # Too far to ever be a 20-min walk: estimate instead of routing.
        l.walk_minutes = round(dist * DETOUR_FACTOR / WALK_KMH * 60, 1)
        l.walk_estimated = True
        return
    minutes, estimated = walk_minutes_to(
        conn, l.lat, l.lon, station["lat"], station["lon"])
    if minutes is not None:
        l.walk_minutes = round(minutes, 1)
        l.walk_estimated = estimated


# When the walk time is only an estimate, accept this much overshoot before
# rejecting — an extra ping is cheap, a missed flat is not.
ESTIMATE_TOLERANCE = 1.25


def matches_criteria(l: Listing, config: dict) -> bool:
    crit = config.get("push_criteria", {})
    max_walk = crit.get("max_walk_minutes")
    if max_walk is not None:
        if l.walk_minutes is None:
            return False
        limit = max_walk * ESTIMATE_TOLERANCE if l.walk_estimated else max_walk
        if l.walk_minutes > limit:
            return False
    max_price = crit.get("max_price")
    if max_price is not None and (l.price is None or l.price > max_price):
        return False
    min_rooms = crit.get("min_rooms")
    if min_rooms is not None and (l.rooms is None or l.rooms < min_rooms):
        return False
    return True
