"""CLI entry point.

Commands:
    scan         scrape all sources, store new listings, push matches
    recap        send the daily Telegram digest of the last 24 h
    preview      print what would be sent, without sending (add hours, e.g. 24)
    backfill-dates    fetch publication dates for rows stored without one
    backfill-details  fetch postal code / floor for rows stored without them
    rematch      recompute is_match for stored rows after editing the criteria
    get-chat-id  print chat ids seen by the bot (setup helper)
    test-alert   send a fake match alert to verify Telegram wiring
"""
from __future__ import annotations

import html
import importlib
import logging
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _is_recent(listing, max_age_days: int) -> bool:
    """Whether a listing is new enough to deserve an instant push.

    Portals paginate non-deterministically, so "first time we saw it" is not
    the same as "newly posted" — immobilier.ch alone re-shuffles roughly a
    third of its results between runs. Where the portal publishes a date we
    trust it; where it does not, the per-run alert cap is the backstop.
    """
    if listing.published is None:
        return True
    published = listing.published
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - published
    return age <= timedelta(days=max_age_days)


def scan() -> None:
    config = load_config()
    conn = db.connect()
    seed_mode = not db.is_seeded(conn)
    if seed_mode:
        log.info("Not yet seeded: this pass stays silent (no notifications).")

    alerting = config.get("alerting", {})
    max_age_days = alerting.get("max_listing_age_days", 3)
    max_alerts = alerting.get("max_alerts_per_source_run", 15)

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
        alerts_sent = 0
        suppressed = 0
        out_of_region = 0
        batch: list = []

        def flush() -> int:
            """Resolve, persist and alert on one batch. Returns how many."""
            nonlocal alerts_sent, suppressed
            if not batch:
                return 0
            geo.resolve_walk_times(conn, batch, config)
            for listing in batch:
                # Before the fingerprint is read: it is derived from zip_code,
                # which enrichment is what supplies on some sources.
                if enrich is not None and geo.could_match(listing, config):
                    try:
                        enrich(sess, listing)
                    except Exception as e:
                        log.warning("enrich failed for %s: %s", listing.uid, e)
                cross_dupe = db.has_fingerprint(conn, listing.fingerprint)
                is_match = geo.matches_criteria(listing, config)
                db.insert(conn, listing, is_match)
                if not (is_match and not seed_mode and not cross_dupe):
                    continue
                if not _is_recent(listing, max_age_days):
                    continue           # old listing we simply had not indexed
                if alerts_sent >= max_alerts:
                    suppressed += 1    # backfill flood — bounded, see recap
                    continue
                try:
                    notify.send_match_alert(listing, config)
                    alerts_sent += 1
                except Exception:
                    log.exception("Match alert failed for %s", listing.uid)
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
        if suppressed:
            log.warning("%s: %d matching listings beyond the %d-alert cap were "
                        "not pushed (they are still in the recap)",
                        source, suppressed, max_alerts)
        log.info("%s: %d new listings, %d alerts sent%s", source, source_new,
                 alerts_sent,
                 f", {out_of_region} outside the region" if out_of_region else "")

    # Reaching here means a full pass finished, so later runs may alert. A run
    # killed mid-pass never gets here and stays in seed mode next time.
    if seed_mode:
        db.set_meta(conn, "seeded", "1")
        log.info("Seed pass complete — future runs will send alerts.")
    log.info("Scan done: %d new listings. Failed sources: %s",
             total_new, failed or "none")


def recap() -> None:
    config = load_config()
    conn = db.connect()
    rows = db.new_since(conn, hours=24)
    # collapse cross-portal duplicates: keep first occurrence per fingerprint
    seen_fp: set[str] = set()
    unique = []
    for r in rows:
        if r["fingerprint"] in seen_fp:
            continue
        seen_fp.add(r["fingerprint"])
        unique.append(r)
    notify.send_recap(unique, config=config)
    log.info("Recap sent: %d listings (%d after dedup).", len(rows), len(unique))


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
    """
    config = load_config()
    conn = db.connect()
    before = conn.execute("SELECT COUNT(*) FROM listings WHERE is_match = 1").fetchone()[0]

    changed = 0
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
        is_match = int(geo.matches_criteria(listing, config))
        if is_match != row["is_match"]:
            conn.execute("UPDATE listings SET is_match = ? WHERE uid = ?",
                         (is_match, row["uid"]))
            changed += 1
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM listings WHERE is_match = 1").fetchone()[0]
    log.info("Rematch: %d matching rows before, %d after (%d rows changed).",
             before, after, changed)


def preview() -> None:
    """Render what would be sent to Telegram, to the terminal. Sends nothing.

    Usage: `python -m watcher.main preview [hours]` (default 24).
    """
    hours = float(sys.argv[2]) if len(sys.argv) > 2 else 24.0
    conn = db.connect()
    config = load_config()

    captured: list[tuple[bool, str]] = []
    original_send = notify.send
    notify.send = lambda text, silent=False: captured.append((silent, text))
    try:
        rows = db.published_since(conn, hours)
        notify.send_recap(rows, config=config)
        for row in [r for r in rows if r["is_match"]][:3]:
            notify.send_match_alert(row, config)
    finally:
        notify.send = original_send

    print(f"\nListings published in the last {hours:g} h: {len(rows)}"
          f" ({sum(1 for r in rows if r['is_match'])} matching)\n")
    for i, (silent, text) in enumerate(captured):
        kind = "DAILY RECAP (silent)" if i == 0 else "PUSH ALERT (buzzes phone)"
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
    fake = Listing(
        source="test", source_id="0", url="https://example.com/annonce-test",
        title="TEST — Appartement 3.5 pièces, Av. de la Gare",
        price=2450, rooms=3.5, surface=82,
        address="Avenue de la Harpe 1", zip_code="1007", city="Lausanne",
        floor=3, walk_minutes=12.0,
    )
    notify.send_match_alert(fake, load_config())
    print("Test alert sent.")


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
