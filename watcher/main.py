"""CLI entry point.

Commands:
    scan         scrape all sources, store new listings, push matches
    recap        send the daily Telegram digest of the last 24 h
    get-chat-id  print chat ids seen by the bot (setup helper)
    test-alert   send a fake match alert to verify Telegram wiring
"""
from __future__ import annotations

import logging
import sys

import yaml
from pathlib import Path

from . import db, geo, notify
from .models import Listing
from .scrapers import get_scraper

log = logging.getLogger("watcher")

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def scan() -> None:
    config = load_config()
    conn = db.connect()
    seed_mode = not db.is_seeded(conn)
    if seed_mode:
        log.info("Not yet seeded: this pass stays silent (no notifications).")

    failed: list[str] = []
    total_new = 0
    for source in config["sources"]:
        try:
            fetch = get_scraper(source)
        except ModuleNotFoundError:
            log.warning("No scraper module for %r, skipping.", source)
            continue
        seen_ids = {uid.split(":", 1)[1] for uid in db.known_uids(conn, source)}
        # Scrapers stream their results so that everything fetched before an
        # interruption (job timeout, network drop) is already persisted.
        source_new = 0
        try:
            for l in fetch(seen_ids):
                if l.source_id in seen_ids:
                    continue
                seen_ids.add(l.source_id)
                cross_dupe = db.has_fingerprint(conn, l.fingerprint)
                geo.resolve_listing(conn, l, config)
                is_match = geo.matches_criteria(l, config)
                db.insert(conn, l, is_match)
                source_new += 1
                total_new += 1
                if is_match and not seed_mode and not cross_dupe:
                    try:
                        notify.send_match_alert(l)
                    except Exception:
                        log.exception("Match alert failed for %s", l.uid)
        except Exception:
            log.exception("Source %s failed after %d new listings.",
                          source, source_new)
            failed.append(source)
            continue
        log.info("%s: %d new listings", source, source_new)

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
    notify.send_recap(unique)
    log.info("Recap sent: %d listings (%d after dedup).", len(rows), len(unique))


def test_alert() -> None:
    fake = Listing(
        source="test", source_id="0", url="https://example.com/annonce-test",
        title="TEST — Appartement 3.5 pièces, Av. de la Gare",
        price=1850, rooms=3.5, surface=78,
        address="Avenue de la Gare 1", zip_code="1003", city="Lausanne",
        walk_minutes=4.0,
    )
    notify.send_match_alert(fake)
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
        "get-chat-id": notify.get_chat_id,
        "test-alert": test_alert,
    }
    if cmd not in commands:
        print(__doc__)
        sys.exit(1)
    commands[cmd]()


if __name__ == "__main__":
    main()
