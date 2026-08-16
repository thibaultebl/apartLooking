# Lausanne Rental Watcher

Scrapes Lausanne-region rental listings on a schedule and sends them to Telegram:

- **instant loud push** for every new listing within **20 minutes' walk of Lausanne station**
- **daily silent digest** of everything new in the last 24 h

No LLM is involved at runtime — it is plain Python on a cron, so it costs nothing
per run and cannot drift or hallucinate.

## Sources

| Source | What it brings | How |
|---|---|---|
| `flatfox` | SMG network (~54 % of its listings carry an `smg_id`) | public JSON API, bbox pins + detail |
| `immobilier_ch` | Romandie régies (Bernard Nicod, Marmillod, …) | HTML cards — ships `data-latlng`, no geocoding needed |
| `acheter_louer` | syndicates **Homegate** inventory | ld+json `ItemList` for ids, ld+json detail per listing |
| `petitesannonces` | private landlords, absent from the portals | HTML table rows |

**Not scraped, deliberately.** `homegate`, `immoscout24`, `anibis` and `newhome`
sit behind DataDome/Cloudflare walls that need a real browser on a residential
IP — unreachable from a CI runner (verified: their web *and* mobile API endpoints
all return 403). Homegate and ImmoScout24 inventory still reaches us indirectly
through `acheter_louer` and `flatfox`; Anibis and newhome are an accepted gap.

Adding a source = adding one file in `watcher/scrapers/` that exposes
`fetch(seen: set[str]) -> Iterator[Listing]`, then listing its name in `config.yaml`.

## How matching works

`geo.py` resolves each listing to coordinates (portal-supplied where available,
else Nominatim, cached permanently), rejects anything more than
`haversine_cutoff_km` away without spending a routing call, and asks a
pedestrian-profile OSRM server for the real walking duration to the station.

> The main OSRM demo server only routes by car and silently returned car times —
> this uses `routing.openstreetmap.de/routed-foot`, which has a real foot profile.

Criteria live in `config.yaml`; `max_price` and `min_rooms` are already wired up
and just need uncommenting.

## Setup

1. **Telegram bot** — message [@BotFather](https://t.me/BotFather), `/newbot`, copy the token.
2. Send your new bot any message, then:
   ```bash
   TELEGRAM_BOT_TOKEN=... python -m watcher.main get-chat-id
   ```
3. **GitHub repo** → Settings → Secrets and variables → Actions, add
   `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
4. Push. `scan.yml` runs every 30 min (07:00–23:00 CEST), `recap.yml` daily at 08:00 CEST.
   Trigger either manually from the Actions tab to verify.

The first run seeds the database **silently** — no notification flood.

## Local use

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m watcher.main test-alert   # verify Telegram wiring
.venv/bin/python -m watcher.main scan
.venv/bin/python -m watcher.main recap
```

Run a single scraper standalone to debug it:
```bash
.venv/bin/python -m watcher.scrapers.flatfox
```

## State

`data/listings.db` (SQLite) holds listings, the dedup index, and the geocode /
walk-time caches. In CI it is force-pushed as a single commit to an orphan
`state` branch by `scripts/state.sh`, so it survives between runs without
bloating the code history.

Scrapers **stream** their results and each listing is written as it arrives, so a
job timeout or a dropped connection never discards the work already done.

## When a scraper breaks

Portals change markup. A source that starts returning 0 listings is logged and
reported in the recap footer rather than failing the run — the other sources keep
working. Run that scraper standalone (above) to see what changed.
