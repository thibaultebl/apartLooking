"""SQLite persistence: listings, dedup, geocode cache."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .models import Listing

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "listings.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    uid          TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    url          TEXT NOT NULL,
    title        TEXT,
    price        INTEGER,
    rooms        REAL,
    surface      INTEGER,
    address      TEXT,
    zip_code     TEXT,
    city         TEXT,
    lat          REAL,
    lon          REAL,
    floor        INTEGER,            -- 0 = rez-de-chaussée; NULL = unknown
    walk_minutes REAL,
    walk_estimated INTEGER DEFAULT 0,
    published    TEXT,
    fingerprint  TEXT,
    is_match     INTEGER DEFAULT 0,
    first_seen   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_listings_first_seen ON listings(first_seen);
CREATE INDEX IF NOT EXISTS idx_listings_fingerprint ON listings(fingerprint);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS geocode_cache (
    query TEXT PRIMARY KEY,
    lat   REAL,
    lon   REAL
);

CREATE TABLE IF NOT EXISTS walk_cache (
    key          TEXT PRIMARY KEY,   -- "lat,lon" on a ~100 m grid (3 decimals)
    walk_minutes REAL
);
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was first created."""
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(listings)")}
    for column, ddl in (("walk_estimated", "INTEGER DEFAULT 0"),
                        ("published", "TEXT"),
                        ("floor", "INTEGER")):
        if column not in existing:
            conn.execute(f"ALTER TABLE listings ADD COLUMN {column} {ddl}")
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                 (key, value))
    conn.commit()


def is_seeded(conn: sqlite3.Connection) -> bool:
    """True once a full scan pass has completed at least once.

    Emptiness alone is not a safe signal: a first run cut short by a job
    timeout leaves a partly-filled database, and treating that as "seeded"
    would fire an alert for every listing the interrupted run had not reached.
    """
    return get_meta(conn, "seeded") == "1"


def known_uids(conn: sqlite3.Connection, source: str) -> set[str]:
    rows = conn.execute("SELECT uid FROM listings WHERE source = ?", (source,))
    return {r["uid"] for r in rows}


def has_fingerprint(conn: sqlite3.Connection, fp: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM listings WHERE fingerprint = ? LIMIT 1", (fp,)
    ).fetchone() is not None


def insert(conn: sqlite3.Connection, l: Listing, is_match: bool) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO listings
           (uid, source, source_id, url, title, price, rooms, surface,
            address, zip_code, city, lat, lon, floor, walk_minutes,
            walk_estimated, published, fingerprint, is_match, first_seen)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (l.uid, l.source, l.source_id, l.url, l.title, l.price, l.rooms,
         l.surface, l.address, l.zip_code, l.city, l.lat, l.lon, l.floor,
         l.walk_minutes, int(l.walk_estimated),
         l.published.isoformat() if l.published else None,
         l.fingerprint, int(is_match),
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def published_since(conn: sqlite3.Connection, hours: float = 24.0) -> list[sqlite3.Row]:
    """Listings the portal published within the window.

    Used by `preview` to show a representative day: `new_since` keys on when we
    first indexed a listing, which during a seed means everything at once.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    return conn.execute(
        "SELECT * FROM listings WHERE published >= ? "
        "ORDER BY is_match DESC, source, price",
        (cutoff[:19],),
    ).fetchall()


def new_since(conn: sqlite3.Connection, hours: float = 24.0) -> list[sqlite3.Row]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    return conn.execute(
        "SELECT * FROM listings WHERE first_seen >= ? ORDER BY is_match DESC, source, price",
        (cutoff,),
    ).fetchall()


# --- geocode / walk caches -------------------------------------------------

def cached_geocode(conn: sqlite3.Connection, query: str) -> Optional[tuple]:
    row = conn.execute(
        "SELECT lat, lon FROM geocode_cache WHERE query = ?", (query,)
    ).fetchone()
    if row is None:
        return None
    return (row["lat"], row["lon"])  # (None, None) means "known unresolvable"


def store_geocode(conn: sqlite3.Connection, query: str,
                  lat: Optional[float], lon: Optional[float]) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO geocode_cache (query, lat, lon) VALUES (?,?,?)",
        (query, lat, lon),
    )
    conn.commit()


def cached_walk(conn: sqlite3.Connection, key: str) -> Optional[float]:
    row = conn.execute(
        "SELECT walk_minutes FROM walk_cache WHERE key = ?", (key,)
    ).fetchone()
    return row["walk_minutes"] if row else None


def store_walk(conn: sqlite3.Connection, key: str, minutes: float) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO walk_cache (key, walk_minutes) VALUES (?,?)",
        (key, minutes),
    )
    conn.commit()
