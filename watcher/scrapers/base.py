"""Shared HTTP helpers for scrapers."""
from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)

# A partial header set gets 403'd by the Cloudflare-fronted portals; this full
# browser fingerprint passes.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "fr-CH,fr;q=0.9,en;q=0.8",
    "sec-ch-ua": '"Chromium";v="126", "Not(A:Brand";v="24", "Google Chrome";v="126"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
}

# Lausanne + adjacent communes (NPA) used to scope searches where the portal
# API takes zip codes rather than a region id.
REGION_ZIPS = [
    "1000", "1003", "1004", "1005", "1006", "1007", "1010", "1012", "1018",
    "1020",  # Renens
    "1008",  # Prilly / Jouxtens
    "1009",  # Pully
    "1022",  # Chavannes-près-Renens
    "1023",  # Crissier
    "1024",  # Ecublens
    "1066",  # Epalinges
    "1052",  # Le Mont-sur-Lausanne
]


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def get(sess: requests.Session, url: str, **kwargs) -> requests.Response:
    kwargs.setdefault("timeout", 25)
    resp = sess.get(url, **kwargs)
    resp.raise_for_status()
    return resp
