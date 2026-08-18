# Lausanne Rental Watcher

Scrapes Lausanne-region rental listings on a schedule and sends them to Telegram:

- **instant loud push** for every new listing matching the search — currently
  **3.5 pces, in 1006 or 1007, 70–95 m², ≤ 2800 CHF, not on the ground floor**
- **daily silent digest** of everything new in the last 24 h

No LLM is involved at runtime — it is plain Python on a cron, so it costs nothing
per run and cannot drift or hallucinate.

## Sources

| Source | What it brings | How |
|---|---|---|
| `flatfox` | SMG network (~54 % of its listings carry an `smg_id`) | public JSON API, bbox pins + detail |
| `immobilier_ch` | Romandie régies (Bernard Nicod, Marmillod, …) | HTML cards — ships `data-latlng`, no geocoding needed; NPA and floor need `enrich` |
| `acheter_louer` | syndicates **Homegate** inventory | ld+json `ItemList` for ids, ld+json detail per listing |
| `petitesannonces` | private landlords, absent from the portals | HTML table rows |
| `comparis` | aggregator over Homegate/ImmoScout24; **states the floor outright** | Next.js payload on the results page — one request, newest 10 only |

**Not scraped, deliberately.** `homegate`, `immoscout24`, `anibis`/`tutti` and
`newhome` sit behind DataDome/Cloudflare walls that need a real browser on a
residential IP — unreachable from a CI runner (re-verified 2026-08-18: all still
403). Homegate and ImmoScout24 inventory still reaches us indirectly through
`acheter_louer`, `flatfox` and `comparis`; Anibis/tutti and newhome are an
accepted gap.

**`comparis` is capped at one request per run**, and that is a property of the
site, not a choice. Its WAF refuses `?page=N`, every other query parameter, the
commune and property-type path variants, and the `/details/show/` pages; only a
bare hit on the results URL returns. The payload is ordered newest-first, so ten
listings every 30 minutes still comfortably outpaces what Lausanne produces —
but comparis can never seed history, and reports ~2500 active listings we will
never page through. It is there for new listings and for the floor.

The four portals overlap heavily — `acheter_louer` ∩ `immobilier_ch` share 72 %
of their addresses, `flatfox` ∩ `acheter_louer` 53 %. Measured by addresses that
would be lost entirely if a source were dropped: immobilier.ch 35 %, flatfox
18 %, acheter-louer 6 %, petitesannonces 2 %. petitesannonces is worth keeping
despite that 2 %: private landlords are absent everywhere else, and it has
produced a disproportionate share of actual matches.

Adding a source = adding one file in `watcher/scrapers/` that exposes
`fetch(seen: set[str]) -> Iterator[Listing]`, then listing its name in `config.yaml`.

## How matching works

`geo.py` resolves each listing to coordinates (portal-supplied where available,
else Nominatim, cached permanently), rejects anything more than
`haversine_cutoff_km` away without spending a routing call, and asks a
pedestrian-profile OSRM server for the real walking duration to the station.

> The main OSRM demo server only routes by car and silently returned car times —
> this uses `routing.openstreetmap.de/routed-foot`, which has a real foot profile.

All criteria live in `config.yaml` under `push_criteria`, and the Telegram
messages label themselves from it, so the two cannot drift apart. Two of them
reject a listing whose value is simply *unknown* — `zip_codes` and `rooms`,
which define the search rather than bound it. The rest only reject a figure the
portal actually published, because "prix sur demande" is common and should not
cost you a flat. `max_walk_minutes` is still implemented and just needs
uncommenting.

### Postal code and floor

Neither is uniformly published, so both are pieced together per source:

| Source | Postal code | Floor |
|---|---|---|
| `flatfox` | in the API response | `floor` field (`0` = rez), else parsed from the description |
| `acheter_louer` | in the ld+json | `<td>Etage</td>` row, else the description — scoped to `div.content`, since the related-listing thumbnails on the same page advertise *other* flats' floors |
| `immobilier_ch` | **absent from search cards** — fetched from the detail page by `enrich` | URL slug (~19 % of cards, free), else the detail page's `og:title` |
| `petitesannonces` | in the search row | ad description, fetched by `enrich` |
| `comparis` | in the payload | stated outright (`3. Etage`, `EG`) — every listing, no extra request |

`enrich` costs one request per listing, so it runs **only for listings that
already pass every other criterion** (`geo.could_match`) — in practice a
handful per run, not one per listing. Floor parsing lives in `watcher/floors.py`
and handles both languages, since comparis labels floors in German (`EG`,
`Untergeschoss`) even on French listings; run `python -m watcher.floors` to
check its regexes against real portal strings.

Rows stored before a field was collected keep their gap, and `is_match` is
written once at insert, so both go stale when the criteria change:

```bash
python -m watcher.main backfill-details   # fetch missing postal codes / floors
python -m watcher.main rematch            # re-apply the criteria to every row
```

`backfill-details` visits **only rows that already pass every other criterion**,
through the same `geo.could_match` gate a scan uses before spending a request —
a few dozen rows rather than the couple of thousand with a gap, since the floor
is never consulted for anything else. So **re-run both after widening the
criteria**: rows that were not candidates before were never enriched, and cannot
match until they are.

Where the floor cannot be determined the listing **still pushes**, labelled
`étage inconnu`: only a confirmed rez-de-chaussée is filtered out, since a
missed flat costs more than one extra ping.

## Why "new to us" is not "newly posted"

immobilier.ch paginates non-deterministically: two consecutive full passes
return ~930 listings each but only ~615 in common, so every run "discovers"
hundreds of listings that have been on the market for months. Treating those as
new would have meant ~80 spurious pushes per run.

Two safeguards, in order:

1. **Publication dates.** flatfox, acheter-louer and petitesannonces all expose
   one, so a listing only earns a push if it was posted within
   `max_listing_age_days`. (immobilier.ch exposes none — its "New" badge sits on
   83 % of cards, so it is worthless as a signal.)
2. **A per-source alert cap.** Whatever happens upstream, one source can never
   send more than `max_alerts_per_source_run` pushes in a run. Anything beyond
   the cap is logged and still appears in the recap — bounded, never lost.

The first scan is silent, and stays silent until a **complete** pass finishes:
a run killed by a timeout leaves the seed flag unset, so the next run does not
mistake the un-scraped remainder for fresh listings.

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
