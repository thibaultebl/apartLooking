"""Listing data model shared by all scrapers."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class Listing:
    source: str                 # scraper name, e.g. "flatfox"
    source_id: str              # portal's own stable id/ref for the listing
    url: str
    title: str = ""
    price: Optional[int] = None         # CHF/month, charges included when known
    rooms: Optional[float] = None
    surface: Optional[int] = None       # m²
    address: str = ""                   # street address if known
    zip_code: str = ""
    city: str = ""
    lat: Optional[float] = None
    lon: Optional[float] = None
    walk_minutes: Optional[float] = None  # filled by geo pipeline
    walk_estimated: bool = False          # True when no router could be reached
    published: Optional[datetime] = None  # portal's publication date, when given

    @property
    def uid(self) -> str:
        """Stable unique id used for dedup within a source."""
        return f"{self.source}:{self.source_id}"

    @property
    def street(self) -> str:
        """The address, or "" when the portal only published a placeholder."""
        addr = self.address.strip()
        if not addr or any(p in addr.lower() for p in ADDRESS_PLACEHOLDERS):
            return ""
        return addr

    @property
    def fingerprint(self) -> str:
        """Cross-portal near-dupe fingerprint: same flat listed on two sites.

        Only a real street address is strong enough evidence to call two
        listings the same flat. Without one we key on the uid so the listing is
        unique to itself — collapsing on (city, price, rooms) would suppress
        genuinely different flats that happen to share those three values.
        """
        street = self.street
        basis = (f"{_normalize(street)}|{self.zip_code}|{self.price}|{self.rooms}"
                 if street else self.uid)
        return hashlib.sha1(basis.encode()).hexdigest()

    @property
    def location_str(self) -> str:
        parts = [p for p in (self.street, f"{self.zip_code} {self.city}".strip()) if p]
        return ", ".join(parts)


# Portals publish these instead of an address when the agency withholds it.
ADDRESS_PLACEHOLDERS = ("contactez", "sur demande", "adresse cachée",
                        "auf anfrage", "on request")


def _normalize(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s).strip()
