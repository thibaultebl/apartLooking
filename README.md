# Lausanne Rental Watcher

Scrapes Lausanne-region rental listings on a schedule and sends **one Telegram
message a day, at 19:00 Swiss time**: the listings published that day that match
the search — currently **3.5 pces, in 1006 or 1007, 70–95 m², ≤ 2800 CHF, not on
the ground floor, ≤ 25 min on foot from Lausanne station**.

Nothing else is ever sent. A listing that fails a criterion is stored (it is what
makes dedup and the caches work) but never reported, and the scans that run
through the day are silent.

The digest goes out **every day, empty or not** — a day with no message would be
indistinguishable from a watcher that has stopped running, so a quiet market says
so out loud ("Aucune des 47 nouvelles annonces d'aujourd'hui ne correspond"). It
buzzes when it has a flat and arrives silently when it does not.

It also never truncates: every match is listed, numbered, with the address as the
link. Past Telegram's 4096-character limit it splits into a second message on a
listing boundary rather than dropping the tail — a flat you are never shown is a
flat you cannot rent.

No LLM is involved at runtime — it is plain Python on a cron, so it costs nothing
per run and cannot drift or hallucinate.

## Sources

| Source | What it brings | How |
|---|---|---|
| `flatfox` | SMG network (~54 % of its listings carry an `smg_id`) | public JSON API, bbox pins + detail |
| `immobilier_ch` | Romandie régies (Bernard Nicod, Marmillod, …) | HTML cards — ships `data-latlng`, no geocoding needed; NPA and floor need `enrich` |
| `acheter_louer` | syndicates **Homegate** inventory | ld+json `ItemList` for ids, ld+json detail per listing |
| `petitesannonces` | private landlords, absent from the portals | HTML table rows |
| ~~`comparis`~~ | aggregator over Homegate/ImmoScout24; states the floor outright | **disabled** — 403 from the CI runner on every run since it was added |

**Not scraped, deliberately.** `homegate`, `immoscout24`, `anibis`/`tutti` and
`newhome` sit behind DataDome/Cloudflare walls that need a real browser on a
residential IP — unreachable from a CI runner (re-verified 2026-08-18: all still
403). Homegate and ImmoScout24 inventory still reaches us indirectly through
`acheter_louer` and `flatfox`; Anibis/tutti and newhome are an accepted gap.

**`comparis` joined them on 2026-08-19.** It works from a residential IP and its
code is unchanged, but its WAF has answered 403 to the GitHub runner on every run
since the source was added — it has never returned a listing in production. It is
commented out in `config.yaml` rather than deleted; uncomment it to bring it back.
Until 2026-08-19 that failure was invisible: the scraper caught its own 403 and
returned nothing, so the source stamped itself healthy. A source that cannot make
its one request now raises, which is what puts it in the digest'"'"'s
`⚠️ Sources en erreur` line.

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
Where the street does not geocode — "Avenue FC de la Harpe" for Frédéric-César,
which the classifieds sources are full of — it falls back to the **postcode
centroid** and flags the resulting time as approximate (`~14 min`), because with
`max_walk_minutes` in force, no coordinates would mean a real flat silently
rejected for a spelling.

> The main OSRM demo server only routes by car and silently returned car times —
> this uses `routing.openstreetmap.de/routed-foot`, which has a real foot profile.

All criteria live in `config.yaml` under `criteria`, and the digest labels itself
from it, so the two cannot drift apart. Two of them reject a listing whose value
is simply *unknown* — `zip_codes` and `rooms`, which define the search rather
than bound it. Most of the rest only reject a figure the portal actually
published, because "prix sur demande" is common and should not cost you a flat.

`max_walk_minutes` is the third kind: it is *computed*, not published, so
"unknown" means the address never resolved, and it rejects. Two things keep that
from costing a flat — the centroid fallback above, and `could_match` (the gate
that decides whether a listing is worth a detail-page fetch) letting an unknown
walk time through, since the address that would produce it is exactly what that
fetch goes and gets.

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
written once at insert, so both go stale when the criteria change.

**Editing `config.yaml` is enough for `is_match`**: a scan fingerprints the
`criteria` block and re-applies it to every stored row when it differs from the
last run, so the deployed database follows the config without anyone remembering
to do anything. This matters because in CI the database lives on the `state`
branch, where nobody ever runs a maintenance command by hand.

The postal code and floor still need fetching by hand after **widening** the
criteria, since rows that were not candidates before were never enriched:

```bash
python -m watcher.main backfill-details   # fetch missing postal codes / floors
python -m watcher.main rematch            # (a scan does this on its own)
```

`backfill-details` visits **only rows that already pass every other criterion**,
through the same `geo.could_match` gate a scan uses before spending a request —
a few dozen rows rather than the couple of thousand with a gap, since the floor
is never consulted for anything else. So **re-run both after widening the
criteria**: rows that were not candidates before were never enriched, and cannot
match until they are.

`rematch` also geocodes and routes any candidate row that has no walk time yet,
through the same gate. A criterion cannot be applied to a value that was never
computed, and failing those rows instead would quietly delete flats whose street
did not geocode the day they were stored.

Where the floor cannot be determined the listing **still appears**, labelled
`étage inconnu`: only a confirmed rez-de-chaussée is filtered out, since a
missed flat costs more than one extra line to read.

## What "published today" means

The digest is keyed on the portal's **publication date**, never on when we first
indexed a listing. The two are not the same: immobilier.ch paginates
non-deterministically — two consecutive full passes return ~930 listings each but
only ~615 in common — so every run "discovers" hundreds of listings that have
been on the market for months.

- **`digest.window_hours` is 26, not 24.** The window ends when the job runs, and
  GitHub crons run late by minutes; an exact 24 h window would open a hole of
  exactly that delay, every time a run is delayed more than the previous one. The
  overlap costs a repeated line in two consecutive digests. A hole costs a flat.
- **Undated listings fall back to `first_seen`**, labelled `date inconnue` in the
  message. Every source publishes a date somewhere — immobilier.ch only on the
  detail page, which `enrich` already fetches for candidates — but some ads
  simply carry none. Since only *matches* reach the digest, and matches are rare,
  the worst case is an occasional flat that was already on the market; the
  alternative is never reporting a real match whose date the portal withheld.
- **No digest until the database is seeded.** A first pass holds an arbitrary
  slice of the market rather than a day's news, and every undated row in it would
  read as "seen today". The seed flag is set only when a **complete** pass
  finishes, so a run killed by a timeout does not let the un-scraped remainder
  through next time.

## Setup

1. **Telegram bot** — message [@BotFather](https://t.me/BotFather), `/newbot`, copy the token.
2. Send your new bot any message, then:
   ```bash
   TELEGRAM_BOT_TOKEN=... python -m watcher.main get-chat-id
   ```
3. **GitHub repo** — make it **public**. Actions minutes are unlimited on public
   repos; a private repo gets 2,000 min/month, and scanning every 30 min is
   ~1,020 runs/month, which does not fit. Nothing secret lives in the repo, and
   `schedule` / `workflow_dispatch` are the only triggers, so no fork-originated
   workflow can ever read the secrets.
4. Settings → Secrets and variables → Actions, add `TELEGRAM_BOT_TOKEN` and
   `TELEGRAM_CHAT_ID`. Then Settings → Actions → General → Workflow permissions
   must not be read-only, or `scan.yml` cannot push the `state` branch.
5. Push. `scan.yml` runs every 30 min (07:00–23:00 CEST) and sends nothing;
   `recap.yml` sends the digest daily at **19:00 Swiss time**. Trigger either
   manually from the Actions tab to verify — a manual recap always sends,
   bypassing the hour check.

Only `recap.yml` gets the Telegram secrets: `scan` has no notification path left,
and a job that cannot reach Telegram cannot leak the token either.

The scan cadence is about coverage, not latency — `comparis` only ever returns
its newest 10 listings per request, so a slower scan loses listings on a busy
day.

### Why the recap has two crons

GitHub schedules in UTC and does not observe DST, so 19:00 local is 17:00 UTC in
summer and 18:00 UTC in winter. `recap.yml` registers **both**, and
`_scheduled_for_now` in `watcher/main.py` lets through only the one that is
really 19:00 today — keyed on `github.event.schedule` (which cron fired) rather
than on the clock, since a scheduled run is often delayed into the next hour.
Collapsing these into a single cron would make the digest drift by an hour twice
a year; dropping one would silence it for half the year.

### Scheduled workflows get disabled after 60 days

GitHub disables cron workflows in a repo with no activity for 60 days. The
`state` branch pushes do not count — they are made by `github-actions[bot]`.
GitHub emails you first, and one click in the Actions tab re-enables it; any
commit you push yourself resets the clock.

## Local use

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m watcher.main test-alert   # verify Telegram wiring
.venv/bin/python -m watcher.main scan
.venv/bin/python -m watcher.main recap
.venv/bin/python -m watcher.main preview 720  # render a month, send nothing
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

Portals change markup. A source that throws is logged and skipped rather than
failing the run — the other sources keep working. Every source that completes a
pass stamps `last_ok:<source>` in the `meta` table, and the daily digest lists any
source with no success in 24 h under `⚠️ Sources en erreur`, so a scraper that
has quietly died cannot masquerade as a quiet market — which matters more now
that most days the digest is legitimately empty. Run that scraper standalone
(above) to see what changed.
