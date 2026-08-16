"""Telegram notifications: instant match alerts and the daily recap digest."""
from __future__ import annotations

import html
import logging
import os

import requests

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 4096


def _creds() -> tuple[str, str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
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
    if len(text) <= MAX_LEN:
        return [text]
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > MAX_LEN:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


def _get(row, key, default=None):
    """Read a field from a sqlite Row or a Listing, tolerating absent columns."""
    try:
        return row[key] if hasattr(row, "keys") else getattr(row, key, default)
    except (IndexError, KeyError):
        return default


def _fmt_listing(row, link: bool = True) -> str:
    """Format one listing row (sqlite Row or Listing-like) as an HTML line."""
    get = row.__getitem__ if hasattr(row, "keys") else lambda k: getattr(row, k)
    title = html.escape(get("title") or "(sans titre)")
    parts = []
    if get("price"):
        parts.append(f"{get('price')} CHF")
    if get("rooms"):
        rooms = get("rooms")
        parts.append(f"{rooms:g} {'pce' if rooms <= 1 else 'pces'}")
    if get("surface"):
        parts.append(f"{get('surface')} m²")
    if get("walk_minutes") is not None:
        approx = "~" if _get(row, "walk_estimated") else ""
        parts.append(f"🚶 {approx}{get('walk_minutes'):.0f} min")
    loc = get("address") or get("city") or "📍 adresse inconnue"
    meta = " · ".join(parts)
    line = f"<a href=\"{html.escape(get('url'))}\">{title}</a>"
    detail = " — ".join(x for x in (meta, html.escape(loc)) if x)
    return f"• {line}\n   {detail}" if detail else f"• {line}"


def send_match_alert(listing) -> None:
    """Loud push for a listing matching the criteria."""
    text = (
        "🚨🚨 <b>MATCH — nouvelle annonce à moins de 20 min de la gare !</b> 🚨🚨\n\n"
        + _fmt_listing(listing)
    )
    send(text, silent=False)


# A digest must stay readable on a phone. Matches are the point of the whole
# thing so they get a generous allowance; the rest is sampled per source.
MAX_MATCHES = 40
MAX_PER_SOURCE = 8


def _section(title: str, items: list, limit: int) -> str:
    shown = items[:limit]
    lines = [_fmt_listing(r) for r in shown]
    if len(items) > limit:
        lines.append(f"   <i>… et {len(items) - limit} autre(s)</i>")
    return f"{title}\n" + "\n".join(lines)


def send_recap(rows, failed_sources: list[str] | None = None) -> None:
    """Daily digest of all new listings; matches pinned on top, silent send."""
    if not rows:
        body = "Aucune nouvelle annonce ces dernières 24 h."
    else:
        matches = [r for r in rows if r["is_match"]]
        others = [r for r in rows if not r["is_match"]]
        sections = []
        if matches:
            sections.append(_section(
                f"🚨 <b>Matches (≤ 20 min à pied de la gare)</b> — {len(matches)}",
                matches, MAX_MATCHES))
        if others:
            by_source: dict[str, list] = {}
            for r in others:
                by_source.setdefault(r["source"], []).append(r)
            for source, items in sorted(by_source.items()):
                sections.append(_section(
                    f"<b>{html.escape(source)}</b> ({len(items)})",
                    items, MAX_PER_SOURCE))
        body = "\n\n".join(sections)

    header = f"🏠 <b>Récap immo Lausanne</b> — {len(rows)} nouvelle(s) annonce(s)\n\n"
    footer = ""
    if failed_sources:
        footer = "\n\n⚠️ Sources en erreur : " + ", ".join(failed_sources)
    send(header + body + footer, silent=True)


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
