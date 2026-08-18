"""CLI entry point.

Commands:
    scan         scrape all sources and store new listings (sends nothing)
    recap        send the daily Telegram digest of the day's matching listings
    preview      print what would be sent, without sending (add hours, e.g. 26)
    backfill-dates    fetch publication dates for rows stored without one
    backfill-details  fetch postal code / floor for rows stored without them
    rematch      recompute is_match for stored rows after editing the criteria
    get-chat-id  print chat ids seen by the bot (setup helper)
    test-alert   send a one-listing digest to verify Telegram wiring
"""
from __future__ import annotations

import hashlib
import html
import importlib
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from . import db, geo, notify
from .models import Listing
from .scrapers import get_scraper
from .scrapers.base import session as base_session

log = logging.getLogger("watcher")

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"

# Listings are resolved and written in batches: big enough for one matrix
# request to do the routing work, small enough that a crash loses little.
BATCH_SIZE = 100

# Seconds between requests in a bulk backfill (see backfill_details).
BACKFILL_INTERVAL = 0.4

# When the daily digest goes out, in Swiss local time.
RECAP_HOUR_LOCAL = 19
# Window the digest covers when config.yaml says nothing.
DEFAULT_WINDOW_HOURS = 26
LOCAL_TZ = ZoneInfo("Europe/Zurich")


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _criteria_hash(config: dict) -> str:
    """Fingerprint of the search, so a scan can tell the criteria have changed."""
    crit = sorted((k, repr(v)) for k, v in config.get("criteria", {}).items())
    return hashlib.sha1(repr(crit).encode()).hexdigest()


def scan() -> None:
    config = load_config()
    conn = db.connect()

    # `is_match` is written once, at insert, so every stored row keeps the
    # verdict of whatever criteria were in force that day. Editing config.yaml
    # would otherwise leave the digest pinning listings that no longer match and
    # hiding ones that now do, until someone remembered to run `rematch` by hand
    # — and in CI nobody ever does, because the database lives on a branch.
    current = _criteria_hash(config)
    if db.get_meta(conn, "criteria_hash") != current:
        log.info("Criteria changed since the last scan — re-applying them.")
        rematch()
        db.set_meta(conn, "criteria_hash", current)

    seed_mode = not db.is_seeded(conn)
    if seed_mode:
        log.info("Not yet seeded: no digest goes out until this pass completes.")

    failed: list[str] = []
    total_new = 0
    sess = base_session()
    for source in config["sources"]:
        try:
            fetch = get_scraper(source)
        except ModuleNotFoundError:
            log.warning("No scraper module for %r, skipping.", source)
            continue
        # Some portals withhold on their search cards what a criterion needs
        # (immobilier.ch publishes no postal code at all). Those modules expose
        # `enrich` to fetch it from the detail page.
        module = importlib.import_module(f".scrapers.{source}", package=__package__)
        enrich = getattr(module, "enrich", None)
        seen_ids = {uid.split(":", 1)[1] for uid in db.known_uids(conn, source)}
        # Scrapers stream their results so that everything fetched before an
        # interruption (job timeout, network drop) is already persisted.
        source_new = 0
        matched = 0
        out_of_region = 0
        batch: list = []

        def flush() -> int:
            """Resolve, match and persist one batch. Returns how many."""
            nonlocal matched
            if not batch:
                return 0
            geo.resolve_walk_times(conn, batch, config)
            for listing in batch:
                if enrich is not None and geo.could_match(listing, config):
                    try:
                        enrich(sess, listing)
                    except Exception as e:
                        log.warning("enrich failed for %s: %s", listing.uid, e)
                    # Enrichment is what supplies the address on immobilier.ch,
                    # so a listing that had no coordinates — and therefore no
                    # walk time — may be routable now. Without this it would
                    # fail the walk criterion for want of an address it has.
                    if listing.walk_minutes is None:
                        geo.resolve_coords(conn, listing)
                        geo.resolve_walk_times(conn, [listing], config)
                is_match = geo.matches_criteria(listing, config)
                matched += is_match
                db.insert(conn, listing, is_match)
            n = len(batch)
            batch.clear()
            return n

        try:
            for l in fetch(seen_ids):
                if l.source_id in seen_ids:
                    continue
                seen_ids.add(l.source_id)
                geo.resolve_coords(conn, l)
                if geo.out_of_region(l, config):
                    out_of_region += 1
                    continue
                batch.append(l)
                if len(batch) >= BATCH_SIZE:
                    source_new += flush()
            source_new += flush()
        except Exception:
            log.exception("Source %s failed after %d new listings.",
                          source, source_new)
            source_new += flush()   # keep what this source did produce
            failed.append(source)
            total_new += source_new
            continue
        total_new += source_new
        # Only on the success path: `recap` reads these marks to tell a quiet
        # market apart from a scraper that has silently stopped working.
        db.set_meta(conn, f"last_ok:{source}",
                    datetime.now(timezone.utc).isoformat())
        log.info("%s: %d new listings, %d matching%s", source, source_new,
                 matched,
                 f", {out_of_region} outside the region" if out_of_region else "")

    # Reaching here means a full pass finished, so the digest may start going
    # out. A run killed mid-pass never gets here and stays in seed mode next
    # time, so a partly-filled database cannot be mistaken for a day's news.
    if seed_mode:
        db.set_meta(conn, "seeded", "1")
        log.info("Seed pass complete — the digest starts from the next one.")
    log.info("Scan done: %d new listings. Failed sources: %s",
             total_new, failed or "none")


def _scheduled_for_now() -> bool:
    """Whether this run is the one that lands on RECAP_HOUR_LOCAL Swiss time.

    GitHub crons are UTC and do not follow DST, so recap.yml registers both
    candidate hours (17:00 and 18:00 UTC) and exactly one of them is 19:00
    locally on any given day. The decision keys on which cron fired — passed
    in as RECAP_CRON, from github.event.schedule — rather than on the clock,
    because a scheduled run is routinely delayed by several minutes and can
    land in the following hour, which a wall-clock test would read as "not
    19:00" and silently drop the digest.

    An empty RECAP_CRON means a manual dispatch (or a local run), which always
    sends.
    """
    cron = os.environ.get("RECAP_CRON", "")
    if not cron:
        return True
    offset_hours = int(datetime.now(LOCAL_TZ).utcoffset().total_seconds() // 3600)
    return cron.split()[1] == str(RECAP_HOUR_LOCAL - offset_hours)


def _stale_sources(conn, config: dict, hours: int = 24) -> list[str]:
    """Sources that have not completed a scan pass within the window.

    Keyed on last success rather than on the last run's failures, because scan
    and recap are separate processes: recap cannot see the `failed` list scan
    built. It also catches a source that has been quietly dead for days rather
    than only one that broke in the final run before the digest.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    stale = []
    for source in config["sources"]:
        last_ok = db.get_meta(conn, f"last_ok:{source}")
        if last_ok is None or datetime.fromisoformat(last_ok) < cutoff:
            stale.append(source)
    return stale


def _window_hours(config: dict) -> float:
    return config.get("digest", {}).get("window_hours", DEFAULT_WINDOW_HOURS)


def _digest_rows(conn, config: dict) -> list:
    """The day's matching listings, cross-portal duplicates collapsed.

    Shared by `recap` and `preview` so that what the preview prints is what the
    digest sends, down to the ordering.
    """
    rows = db.matches_since(conn, _window_hours(config))
    seen_fp: set[str] = set()
    unique = []
    for r in rows:
        if r["fingerprint"] in seen_fp:
            continue          # same flat, listed on a second portal
        seen_fp.add(r["fingerprint"])
        unique.append(r)
    return unique


def recap() -> None:
    if not _scheduled_for_now():
        log.info("Cron %r is not %02d:00 Europe/Zurich today — no digest.",
                 os.environ.get("RECAP_CRON", ""), RECAP_HOUR_LOCAL)
        return
    config = load_config()
    conn = db.connect()
    # A database still being seeded holds an arbitrary slice of the market
    # rather than a day's news, and the undated rows in it would all read as
    # "seen today". Nothing goes out until one full pass has completed.
    if not db.is_seeded(conn):
        log.info("Database not seeded yet — no digest.")
        return
    rows = _digest_rows(conn, config)
    stale = _stale_sources(conn, config)
    notify.send_digest(rows, failed_sources=stale, config=config,
                       scanned=db.count_since(conn, _window_hours(config)))
    log.info("Digest sent: %d matching listings over %g h. Stale sources: %s",
             len(rows), _window_hours(config), stale or "none")


def backfill_dates() -> None:
    """Fetch publication dates for rows stored before dates were extracted.

    Usage: `python -m watcher.main backfill-dates [per_source]` (default 300).

    Works newest-id-first: portal ids are sequential, so the recent listings —
    the only ones a digest window can contain — are reached first, and the run
    can be stopped at any point without losing what it already wrote.
    """
    per_source = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    conn = db.connect()
    sess = base_session()

    for source in load_config()["sources"]:
        module = importlib.import_module(f".scrapers.{source}", package=__package__)
        if not hasattr(module, "published_for"):
            continue
        rows = conn.execute(
            """SELECT uid, source_id, url FROM listings
               WHERE source = ? AND published IS NULL
               ORDER BY CAST(source_id AS INTEGER) DESC LIMIT ?""",
            (source, per_source),
        ).fetchall()
        if not rows:
            log.info("%s: nothing to backfill", source)
            continue

        filled = 0
        for row in rows:
            stub = Listing(source=source, source_id=row["source_id"], url=row["url"])
            published = module.published_for(sess, stub)
            if published is None:
                continue
            conn.execute("UPDATE listings SET published = ? WHERE uid = ?",
                         (published.isoformat(), row["uid"]))
            conn.commit()
            filled += 1
        log.info("%s: dated %d of %d rows", source, filled, len(rows))


def backfill_details() -> None:
    """Re-run each source's `enrich` over candidate rows stored before it existed.

    Usage: `python -m watcher.main backfill-details [per_source]` (default 500).

    Rows scraped under older criteria — or before enrichment existed at all —
    never had their postal code or floor fetched, so no criterion that depends
    on either can be applied to them retroactively. This fills them in.

    Only rows that already pass every *other* criterion are visited, through the
    same `geo.could_match` gate a scan applies before it spends a request. The
    floor is never consulted for anything else, so fetching it for the rest
    would be pure waste — and that scoping is what keeps this pass small enough
    to finish: a few dozen rows out of a few thousand.

    It is also why no "already attempted" marker is needed. Plenty of rows never
    yield a floor (immobilier.ch frequently just does not state one) and stay
    NULL for good, so they qualify again on every run — harmless over a few
    dozen rows, but it is why this must not be widened to the whole table
    without one. Re-run it after widening the criteria, then `rematch`.

    Newest-id-first and committed per row, so it can be stopped at any point
    without losing what it already wrote.
    """
    config = load_config()
    per_source = int(sys.argv[2]) if len(sys.argv) > 2 else 500
    conn = db.connect()
    sess = base_session()

    for source in config["sources"]:
        module = importlib.import_module(f".scrapers.{source}", package=__package__)
        enrich = getattr(module, "enrich", None)
        if enrich is None:
            continue
        # Deliberately uncapped in SQL: `could_match` does the narrowing, and it
        # needs the criteria columns to do it. `per_source` then caps the
        # requests actually spent rather than the rows considered — capping here
        # instead would hide older candidates behind newer non-candidates.
        rows = conn.execute(
            """SELECT uid, source_id, url, title, price, rooms, surface,
                      address, zip_code, city, lat, lon, floor,
                      walk_minutes, walk_estimated, published
                 FROM listings
                WHERE source = ? AND (zip_code = '' OR zip_code IS NULL
                                      OR floor IS NULL)
                ORDER BY CAST(source_id AS INTEGER) DESC""",
            (source,),
        ).fetchall()

        filled_zip = filled_floor = failed = visited = 0
        for row in rows:
            listing = Listing(
                source=source, source_id=row["source_id"], url=row["url"],
                title=row["title"] or "", price=row["price"], rooms=row["rooms"],
                surface=row["surface"], address=row["address"] or "",
                zip_code=row["zip_code"] or "", city=row["city"] or "",
                lat=row["lat"], lon=row["lon"], floor=row["floor"],
                walk_minutes=row["walk_minutes"],
                walk_estimated=bool(row["walk_estimated"]),
            )
            # Checked before the request is spent, not after.
            if not geo.could_match(listing, config):
                continue
            if visited >= per_source:
                log.warning("%s: stopping at the %d-request cap; re-run to "
                            "continue", source, per_source)
                break
            visited += 1
            # A bulk pass hits one portal repeatedly, which a normal scan never
            # does. Stay slow enough not to get the runner's IP blocked — losing
            # the source would cost far more than the wait.
            time.sleep(BACKFILL_INTERVAL)
            try:
                enrich(sess, listing)
            except Exception as e:
                failed += 1
                log.warning("enrich failed for %s: %s", row["uid"], e)
                continue
            if listing.zip_code == (row["zip_code"] or "") and \
                    listing.floor == row["floor"]:
                continue
            conn.execute(
                """UPDATE listings SET zip_code = ?, floor = ?, address = ?,
                          published = COALESCE(published, ?)
                     WHERE uid = ?""",
                (listing.zip_code, listing.floor, listing.address,
                 listing.published.isoformat() if listing.published else None,
                 row["uid"]),
            )
            conn.commit()
            filled_zip += bool(listing.zip_code) and not row["zip_code"]
            filled_floor += listing.floor is not None and row["floor"] is None
        log.info("%s: %d candidates fetched out of %d incomplete rows — %d "
                 "gained a postal code, %d a floor, %d failed",
                 source, visited, len(rows), filled_zip, filled_floor, failed)

    log.info("Backfill done. Re-run `rematch` to apply the criteria to them.")


def rematch() -> None:
    """Recompute is_match for every stored row against the current criteria.

    Usage: `python -m watcher.main rematch`

    `is_match` is written once, when a listing is first seen, so rows keep the
    verdict of whatever criteria were in force that day. After editing
    config.yaml — or after `backfill-details` supplies a missing postal code —
    the stored verdicts are stale and the digest pins the wrong listings.

    Rows that pass every other criterion but have no walk time are geocoded and
    routed here rather than being failed on the spot: a criterion cannot be
    applied to a value that was never computed, and rejecting those rows would
    quietly delete flats whose street simply did not geocode when they were
    first stored. Only candidates are resolved — the same `could_match` gate
    `backfill-details` uses — so this stays a handful of rows, not the table.
    """
    config = load_config()
    conn = db.connect()
    before = conn.execute("SELECT COUNT(*) FROM listings WHERE is_match = 1").fetchone()[0]

    changed = resolved = 0
    for row in conn.execute("SELECT * FROM listings").fetchall():
        listing = Listing(
            source=row["source"], source_id=row["source_id"], url=row["url"],
            title=row["title"] or "", price=row["price"], rooms=row["rooms"],
            surface=row["surface"], address=row["address"] or "",
            zip_code=row["zip_code"] or "", city=row["city"] or "",
            lat=row["lat"], lon=row["lon"], floor=row["floor"],
            walk_minutes=row["walk_minutes"],
            walk_estimated=bool(row["walk_estimated"]),
        )
        if listing.walk_minutes is None and geo.could_match(listing, config):
            geo.resolve_coords(conn, listing)
            geo.resolve_walk_times(conn, [listing], config)
            if listing.walk_minutes is not None:
                resolved += 1
                conn.execute(
                    """UPDATE listings SET lat = ?, lon = ?, walk_minutes = ?,
                              walk_estimated = ? WHERE uid = ?""",
                    (listing.lat, listing.lon, listing.walk_minutes,
                     int(listing.walk_estimated), row["uid"]))
        is_match = int(geo.matches_criteria(listing, config))
        if is_match != row["is_match"]:
            conn.execute("UPDATE listings SET is_match = ? WHERE uid = ?",
                         (is_match, row["uid"]))
            changed += 1
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM listings WHERE is_match = 1").fetchone()[0]
    log.info("Rematch: %d matching rows before, %d after (%d rows changed, "
             "%d candidates newly geocoded).", before, after, changed, resolved)


def preview() -> None:
    """Render what would be sent to Telegram, to the terminal. Sends nothing.

    Usage: `python -m watcher.main preview [hours]` (default: the configured
    digest window). Widen it — `preview 168` — to see a week when the day is
    empty, which it usually is.
    """
    conn = db.connect()
    config = load_config()
    if len(sys.argv) > 2:
        config.setdefault("digest", {})["window_hours"] = float(sys.argv[2])
    hours = _window_hours(config)

    captured: list[tuple[bool, str]] = []
    original_send = notify.send
    notify.send = lambda text, silent=False: captured.append((silent, text))
    try:
        rows = _digest_rows(conn, config)
        notify.send_digest(rows, failed_sources=_stale_sources(conn, config),
                           config=config,
                           scanned=db.count_since(conn, hours))
    finally:
        notify.send = original_send

    print(f"\nMatching listings published in the last {hours:g} h: {len(rows)}\n")
    for silent, text in captured:
        kind = "DAILY DIGEST (silent)" if silent else "DAILY DIGEST (buzzes phone)"
        print(f"┌─ {kind} " + "─" * max(0, 56 - len(kind)))
        for line in _to_terminal(text).split("\n"):
            print(f"│ {line}")
        print("└" + "─" * 62 + "\n")


def _to_terminal(text: str) -> str:
    """Flatten Telegram HTML into something readable in a terminal."""
    text = re.sub(r'<a href="([^"]+)">(.*?)</a>', r"\2\n     → \1", text)
    text = re.sub(r"</?[bi]>", "", text)
    return html.unescape(text)


def test_alert() -> None:
    """Send a one-listing digest, to verify the Telegram wiring.

    Goes through `send_digest` rather than a bespoke message so that what this
    proves is the path the daily digest actually takes.
    """
    fake = Listing(
        source="test", source_id="0", url="https://example.com/annonce-test",
        title="TEST — Appartement 3.5 pièces, Av. de la Gare",
        price=2450, rooms=3.5, surface=82,
        address="Avenue de la Harpe 1", zip_code="1007", city="Lausanne",
        floor=3, walk_minutes=12.0, published=datetime.now(timezone.utc),
    )
    notify.send_digest([fake], config=load_config())
    print("Test digest sent.")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    commands = {
        "scan": scan,
        "recap": recap,
        "preview": preview,
        "backfill-dates": backfill_dates,
        "backfill-details": backfill_details,
        "rematch": rematch,
        "get-chat-id": notify.get_chat_id,
        "test-alert": test_alert,
    }
    if cmd not in commands:
        print(__doc__)
        sys.exit(1)
    commands[cmd]()


if __name__ == "__main__":
    main()
