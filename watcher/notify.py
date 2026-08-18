"""Telegram notifications: the daily digest of matching listings."""
from __future__ import annotations

import html
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from . import floors

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 4096
LOCAL_TZ = ZoneInfo("Europe/Zurich")


def _creds() -> tuple[str, str]:
    # Stripped, and the chat id tolerates the `chat_id=` prefix that
    # `get-chat-id` prints: both are pasted through the GitHub secrets UI,
    # which shows nothing back, and either mistake surfaces only as Telegram's
    # unhelpful "chat not found" hours later.
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if chat_id.startswith("chat_id="):
        chat_id = chat_id[len("chat_id="):].strip()
    if not token or not chat_id:
        raise RuntimeError(
            "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables."
        )
    return token, chat_id


def send(text: str, silent: bool = False) -> None:
    """Send an HTML-formatted message, splitting if over Telegram's limit."""
    token, chat_id = _creds()
    for chunk in _split(text):
        resp = requests.post(
            API.format(token=token, method="sendMessage"),
            json={
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "disable_notification": silent,
            },
            timeout=20,
        )
        if not resp.ok:
            log.error("Telegram send failed: %s %s", resp.status_code, resp.text)
            resp.raise_for_status()


def _split(text: str) -> list[str]:
    """Split past Telegram's limit on listing boundaries, not mid-listing.

    Entries are separated by a blank line, so breaking there keeps an address
    with its price and its walk time. A single entry can never approach 4096
    characters on its own, but the line-level fallback is kept for the case
    where one somehow does.
    """
    if len(text) <= MAX_LEN:
        return [text]
    chunks, current = [], ""
    for block in text.split("\n\n"):
        if len(current) + len(block) + 2 > MAX_LEN:
            if current:
                chunks.append(current)
            current = ""
            if len(block) > MAX_LEN:
                for line in block.split("\n"):
                    if len(current) + len(line) + 1 > MAX_LEN:
                        chunks.append(current)
                        current = line
                    else:
                        current = f"{current}\n{line}" if current else line
                continue
        current = f"{current}\n\n{block}" if current else block
    if current:
        chunks.append(current)
    return chunks


def _get(row, key, default=None):
    """Read a field from a sqlite Row or a Listing, tolerating absent columns."""
    try:
        return row[key] if hasattr(row, "keys") else getattr(row, key, default)
    except (IndexError, KeyError):
        return default


def _fmt_date(row) -> str:
    """The publication date, or the cue that the portal stated none.

    Shown on every line rather than only on the dated ones: an undated listing
    is in the digest because we *saw* it today, which is a weaker claim than
    "published today", and the reader has to be able to tell the two apart.
    """
    published = _get(row, "published")
    if not published:
        return "📅 date de publication inconnue — vue aujourd'hui"
    if hasattr(published, "strftime"):
        return f"📅 publiée le {published:%d.%m}"
    # sqlite hands back the ISO string it was given: "2026-08-18T13:55:03[...]"
    date = str(published)[:10]
    parts = date.split("-")
    return (f"📅 publiée le {parts[2]}.{parts[1]}" if len(parts) == 3
            else f"📅 publiée le {date}")


def _fmt_listing(row, index: int | None = None) -> str:
    """One listing as a numbered Telegram entry.

    The link sits on the address rather than on the portal's title, because the
    titles are advertising copy ("Appartement à louer: 3.5 pièces, Lausanne -
    Vaud") and the address is what tells two listings apart at a glance. The
    title follows underneath only when it adds something the address does not.
    """
    get = row.__getitem__ if hasattr(row, "keys") else lambda k: getattr(row, k)

    address = (get("address") or "").strip()
    city = (get("city") or "").strip()
    zip_code = (get("zip_code") or "").strip()
    heading = address or (get("title") or "").strip() or "Annonce"
    where = " ".join(x for x in (zip_code, city) if x)

    facts = []
    if get("price"):
        facts.append(f"{get('price')} CHF")
    if get("rooms"):
        rooms = get("rooms")
        facts.append(f"{rooms:g} {'pce' if rooms <= 1 else 'pces'}")
    if get("surface"):
        facts.append(f"{get('surface')} m²")
    # Always shown, including when unknown: "étage inconnu" is the cue to open
    # the ad and check, since a ground floor is only filtered out when the
    # portal actually said so.
    facts.append(floors.describe(_get(row, "floor")))
    if get("walk_minutes") is not None:
        approx = "~" if _get(row, "walk_estimated") else ""
        facts.append(f"🚶 {approx}{get('walk_minutes'):.0f} min de la gare")

    num = f"{index}. " if index is not None else ""
    lines = [
        f"{num}<b><a href=\"{html.escape(get('url'))}\">{html.escape(heading)}</a></b>"
        + (f" — {html.escape(where)}" if where else ""),
        "   " + " · ".join(facts),
        f"   {_fmt_date(row)} · {html.escape(str(_get(row, 'source') or ''))}",
    ]
    return "\n".join(lines)


def criteria_label(config: dict | None) -> str:
    """One-line summary of the search criteria, for the message header.

    Built from the config rather than written out, because the previous
    hardcoded "moins de 20 min de la gare" outlived the criterion it described.
    """
    crit = (config or {}).get("criteria", {})
    parts = []
    if crit.get("rooms"):
        parts.append(" / ".join(f"{float(r):g}" for r in crit["rooms"]) + " pces")
    if crit.get("zip_codes"):
        parts.append(" ou ".join(str(z) for z in crit["zip_codes"]))
    if crit.get("min_surface") or crit.get("max_surface"):
        lo, hi = crit.get("min_surface"), crit.get("max_surface")
        parts.append(f"{lo}-{hi} m²" if lo and hi else f"{lo or hi} m²")
    if crit.get("max_price"):
        parts.append(f"≤ {crit['max_price']} CHF")
    if crit.get("exclude_ground_floor"):
        parts.append("sans rez")
    if crit.get("max_walk_minutes"):
        parts.append(f"≤ {crit['max_walk_minutes']} min à pied")
    return " · ".join(parts) or "critères de recherche"


def send_digest(rows, failed_sources: list[str] | None = None,
                config: dict | None = None, scanned: int | None = None) -> None:
    """The one message a day. Sent every day, empty or not.

    Every matching listing is in it — no sampling, no "… et N autres". Telegram
    caps a message at 4096 characters, and `send` splits past that, so a busy day
    arrives as two messages rather than as a truncated one: a flat you are never
    shown is a flat you cannot rent.

    Loud when it has something, silent when it does not. It goes out either way,
    because a day with no message would be indistinguishable from a watcher that
    has stopped running.
    """
    label = criteria_label(config)
    today = datetime.now(LOCAL_TZ)
    header = f"🏠 <b>Récap immo Lausanne</b> — {_fmt_day(today)}\n"

    if rows:
        plural = "s" if len(rows) > 1 else ""
        count = (f"<b>{len(rows)} annonce{plural}</b> "
                 f"correspond{'ent' if plural else ''} à tes critères aujourd'hui.")
        body = "\n\n".join(_fmt_listing(r, i)
                            for i, r in enumerate(rows, start=1))
    else:
        if scanned:
            plural = "s" if scanned > 1 else ""
            count = (f"<b>Aucune des {scanned} nouvelle{plural} "
                     f"annonce{plural} d'aujourd'hui ne correspond à tes "
                     f"critères.</b>")
        else:
            count = "<b>Aucune nouvelle annonce aujourd'hui.</b>"
        body = "Rien à visiter — le récap revient demain à 19 h."

    footer = f"\n\n<i>Critères : {html.escape(label)}</i>"
    if failed_sources:
        footer += ("\n⚠️ Sources en erreur : "
                   + ", ".join(html.escape(s) for s in failed_sources))
    send(f"{header}{count}\n\n{body}{footer}", silent=not rows)


DAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
MONTHS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet",
          "août", "septembre", "octobre", "novembre", "décembre")


def _fmt_day(when) -> str:
    """"mercredi 19 août" — %A/%B would follow the runner's locale, which in CI
    is C, and would print "Wednesday 19 August" in an otherwise French message."""
    return f"{DAYS[when.weekday()]} {when.day} {MONTHS[when.month - 1]}"


def get_chat_id() -> None:
    """Helper: print chat ids of recent messages sent to the bot."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN first.")
    resp = requests.get(API.format(token=token, method="getUpdates"), timeout=20)
    resp.raise_for_status()
    updates = resp.json().get("result", [])
    if not updates:
        print("No messages yet — send any message to your bot in Telegram, then rerun.")
        return
    for u in updates:
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat", {})
        if chat:
            print(f"chat_id={chat.get('id')}  ({chat.get('type')}, "
                  f"{chat.get('first_name') or chat.get('title', '')})")
