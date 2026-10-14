# TenderWatch

Crawls government procurement portals on a schedule, stores every tender it has ever seen as
Parquet in git, and serves a fast client-side search as a static site. No database, no server:
GitHub Actions runs the crawlers (one job per portal, in parallel), GitHub Pages hosts the site.

```
sources.yaml                      which portals to crawl (base URL + parser type)
crawler/                          Python crawler  (python -m crawler.run)
data/<slug>/<org>-<hash>.parquet  every tender ever seen, one shard per organisation  <- source of truth
data/<slug>/_meta.json            storage format version of that source
data/<slug>/_status.json          outcome of that source's last crawl
data/_legacy/<slug>.jsonl         pre-Parquet data, kept after a verified migration
docs/                             the static site (GitHub Pages serves this folder)
docs/data/index.json              manifest: per source its counts, state, stats and chunk files (+ orgs.json and presets/, see "The site")
docs/data/index/<slug>-live-N.json    active tenders, loaded lazily by the site
docs/data/index/<slug>-closed-N.json  closed / removed tenders, loaded only on "Show closed"
docs/data/status.json             last crawl status per source (shown at the bottom of the site)
docs/map/india-states.json        state boundaries for the map panel (see "The map")
tools/build_map.py                regenerates that file from a boundary GeoJSON
tests/                            parser, storage, migration, index, map-build and crawl-flow tests
```

## Run it locally first

```bash
pip install -r requirements.txt
python -m crawler.run --limit-orgs 3            # smoke test: 3 organisations, with details
python -m crawler.run --org "ANERT" --org "Kerala State Road Transport Corporation"
python -m crawler.run                            # all enabled sources, in parallel (Kerala alone: ~7k tenders, ~2h with details)
python -m crawler.run --no-details               # listing only (~6 min for Kerala)
python -m crawler.run --parallel 2               # at most 2 sources at once (default: all)
python -m crawler.run --force-refresh            # ignore the freshness rules below
python -m crawler.run --show-progress            # live per-source progress of a crawl running in another terminal
python -m crawler.run --index-only               # rebuild docs/data/ from data/, no crawling
python -m http.server -d docs 8000               # open http://localhost:8000
```

If an org parses to 0 rows, re-run with `--dump-html debug/` and look at the saved page: GePNIC
markup varies slightly between portal versions, and `parse_tender_list` in `crawler/gepnic.py`
is header-driven so it is usually a one-line fix.

Tests: `python -m pytest tests`.

## Deploy (zero servers)

1. Push this folder to a new GitHub repo.
2. Settings → Pages → Source: **Deploy from a branch**, branch `main`, folder `/docs`.
3. Settings → Actions → General → Workflow permissions: **Read and write**.
4. Actions → "Crawl tenders" → Run workflow (tick *no_details* for the first run so it finishes
   in minutes). Each source crawls in its own job; a final *publish* job rebuilds the index and
   commits `data/` and `docs/data/` once; Pages redeploys.
5. It then runs once a day (06:47 IST) by itself. Change the cron in `.github/workflows/crawl.yml`.

**Check after the first Actions run:** portals sometimes block non-Indian IPs. If the log shows
timeouts or "organisation list parsed empty", run the crawler on a machine in India (your laptop,
a small Indian VPS) and have *it* push to the repo; the site stays on Pages. Nothing else changes.

## Adding a source

GePNIC (NIC) portals — eprocure.gov.in and most state eTenders sites — need only a new block in
`sources.yaml`:

```yaml
  - slug: tamilnadu
    name: Tamil Nadu eTenders
    type: gepnic
    base_url: https://tntenders.gov.in/nicgep/app
    enabled: true
    fetch_details: true
    max_detail_fetches: 8000
    refresh_after_hours: 48
    detail_max_age_days: 14
    delay_seconds: 1.0
```

Other platforms need a small adapter for the shared crawl loop (`crawler/engine.py` documents the
interface: list organisations, walk one, fetch a detail) plus a `crawl_source` and a line in
`crawler/run.py:CRAWLERS`. `crawler/gepnic.py`, `crawler/nprocure.py` and `crawler/epsv2.py` are the examples; the
refresh policy, retention, checkpoints and progress all come from the engine, identically for every
portal type.

### How the GePNIC crawl works

Tenders by Organisation → each org's tender list → each tender's detail page. The portal sends a
whole organisation's tenders in **one response** (paging is client-side only; Local Self
Government Department's ~5,000 tenders are a ~10 MB, ~2 minute response), so listing requests get a
long timeout. All links are tied to a server session (`sp=` tokens); they stay valid for minutes,
and a *Stale Session* page triggers a restart and a re-list of that org. The CAPTCHA only guards the
filter form, which is never used.

The portal's WAF stalls clients whose **TLS fingerprint** is not a browser's (plain `requests`
and curl time out whatever the User-Agent), so HTTP goes through `curl_cffi` impersonating Chrome.

Each organisation is crawled in two phases: **listing** (one request, saved immediately), then
**details** (one request per tender, saved every 50 pages and after each org), so an interruption
loses little and a re-run carries on where it stopped.

Because deep links expire, the site cannot link straight to a tender. **Open portal** opens the
portal home and **Copy Tender ID** puts the ID on the clipboard; the portal's *Search with ID*
box takes it.

### How the nProcure crawl works (Gujarat state portal)

`tender.nprocure.com` is one portal for ~165 *clients* (departments, boards, municipal corporations,
all Nagarpalikas), ~4,000 active tenders. A client plays the role of a GePNIC organisation:

1. **Client list** — the state home page's dropdown carries, per client, its id, its tender count and
   its own subdomain (e.g. Surat → `smctender.nprocure.com`); clients with 0 tenders are skipped.
2. **Listing** — per client, on its own host: `POST /beforeLoginTenderTableList`, 150 rows per page.
   That page's total equals the dropdown count (the state-level table is ~35 tenders short, so it is
   not used). The request body is encrypted by the portal's own JavaScript with a key it sends in the
   same body (obfuscation, not access control); `crawler/nprocure.py` reproduces it exactly and a test
   pins it to a vector the portal's own CryptoJS decrypts and re-encrypts byte for byte.
3. **Detail** — `POST /view-nit-home` (`_csrf=token&tenderid=<id>`): a plain form, no session needed.

Records match the GePNIC ones (`org` = client, `chain` = `Client||Department`, `value`, `emd`,
`category`, `pub`, `bid_end`, `documents`, …). The detail page also contains a bidder's digital-signature
certificate (a person's name); that table is never stored. The `*.nprocure.com` certificate's
intermediate is missing from the server's chain, so the source sets `extra_ca_certs` (`certs/README.md`).
Cost: ~88 requests for all listings plus one per tender (~4,100 at the 2 s delay ≈ 2.3 h the first time,
then only new/changed tenders). Needs the `cryptography` package for AES.

### How the Bihar crawl works (EPSV2 portal)

`eproc2.bihar.gov.in` runs Beltron/mjunction's "eProc 2.0" (EPSV2). Its open-area tender list is an
AngularJS page; the tenders come from the portal's own JSON API, called with the anonymous guest token that
the page embeds in itself (a ~15 minute JWT, no login). `crawler/epsv2.py` makes the same calls the page does:

1. **Page** — `GET /EPSV2Web/openarea/tenderListingPage.action`: the guest token and the portal's org id.
   The token is renewed automatically when it has under 2.5 minutes left.
2. **Listing** — one `POST /rest/openarea/getTenderList` returns *every* live tender (no paging; ~800 tenders,
   1.3 MB), plus the department and tender-type/category name tables. A **department** plays the role of a GePNIC
   organisation. The id shown on the site is `currentOrgTenderId`; the detail call needs the internal id.
3. **Detail** — `POST /rest/tender/previewTenderByTenderId`: estimated value, EMD, fees, bid/opening/pre-bid
   dates, attachment names (~200 KB of JSON per tender, of which only the useful fields are kept).

Officers' names, contact details and the digital-signature strings in the detail JSON are never stored (the
`authority` field holds the issuing authority's designation). The server omits its certificate's intermediate,
so the source sets `extra_ca_certs: certs/sectigo-ov-r36.pem` (the same one Gujarat uses). Not crawled (yet):
cancelled tenders, the corrigendum list and past tenders; a changed deadline in the listing already triggers a
detail refresh.

### How the Mizoram crawl works (state e-Tender notice board)

NIC's Mizoram portal (`mizoramtenders.gov.in`) puts a CAPTCHA in front of every tender list, so it is
disabled in `sources.yaml` and cannot be crawled. The state also runs its own e-Tender notice board,
`tender.mizoram.gov.in` (source `mizoram-etender`, type `mizoram`): a paged list (`/home?page=N`, 15 cards a page,
~51 pages, expired notices included). Each card has a reference, department, notice type, description,
attachment names and three dates (issue, opening, last date); there is no detail page and no value or EMD.

`crawler/mizoram.py` reads every page (~1 minute), keeps only notices whose last date is still ahead, and groups
them by department (a department plays the role of an organisation). Notices that expire stay in the store, marked
closed, like on every other source. At the moment only a handful of notices are open at any time. A board with no open
notice is a valid result (`may_be_empty` in the adapter), whereas a first page without any card (markup changed) fails
the run and deactivates nothing.

### Parallel crawling

Sources are independent portals, so they run concurrently: one thread each locally, one matrix
job each in CI. Every portal is still hit at its own `delay_seconds` pace (1 request per second
by default); parallelism only overlaps *different* portals, so wall time is the slowest portal,
not the sum. Ctrl-C lets each crawl finish its current request and save.

## Storage

`data/<slug>/` holds one **Parquet** file per organisation (zstd, rows sorted by id), so a source
is split into many modest files and an unchanged organisation is never rewritten (a daily run only
touches shards whose content changed, which keeps git history small). Known fields have typed
columns; `details` is JSON text; anything unexpected goes in an `extra` JSON column, so a round
trip is lossless. `None` values are dropped.

Plain JSONL was human-readable and diffed line by line; Parquet is not. To look at data:

```python
import pyarrow.parquet as pq
pq.read_table("data/kerala/anert-a37c7b.parquet").to_pandas()      # or: .to_pylist()
```

### Migrations (formats never strand your data)

Storage is versioned (`data/<slug>/_meta.json`, `FORMAT_VERSION` in `crawler/store.py`). Older
layouts are migrated forward — currently `data/<slug>.jsonl` → Parquet shards:

```bash
python -m crawler.migrate --dry-run     # verify what would happen, change nothing
python -m crawler.migrate               # migrate every source that still has a .jsonl
```

Also happens automatically the first time a crawl or the index builder opens an old source. The
guarantees: **it never runs while a crawl is active** (a lock file `data/.crawl.lock`, or a legacy
file modified in the last 5 minutes → refused, exit code 2); every record is re-read from the new
files and compared with the original **before anything is replaced** (any difference aborts and
changes nothing); the old file is **moved to `data/_legacy/`, never deleted**. Future formats add a
step in `store.migrate_if_needed`; data written by a *newer* format than the code knows is refused.

## Refresh policy

- **Detail pages** are fetched for a tender when it is **new**, when its **listing changed** since
  we last saw it (closing date, opening date, title, ref, publish date — flagged `needs_details`, so
  an interrupted run still picks it up), or, as a safety net, when its details are older than
  `detail_max_age_days` (default 14, `0` = never). They are *not* re-fetched on a timer.
- An **organisation is re-listed** at most every `refresh_after_hours` (default 48) and is **skipped
  with no request at all** when its listing is fresh, the portal's tender count equals our stored
  active count, and no tender needs details. A count mismatch forces a re-list even inside the window.
- `--force-refresh` ignores all of it.

### Parallel detail fetching

The first crawl of a big source is dominated by detail pages (nProcure answers each one in about 3 s, so
one at a time is ~17 tenders a minute). `detail_workers: N` in a source of `sources.yaml` fetches N detail
pages at once (1 = off, the default; at most 8; every portal type supports it). Safety:

* The session's `delay_seconds` still caps the **start rate** across all workers (1.0 = at most one new
  request a second), so workers only overlap the waiting; they never make the portal see a faster request rate.
* All workers share one cool-down: the first throttled worker stops the others from starting new requests,
  the source waits once (see below), then only the pages that were not fetched are retried.
* Results are applied in order on the main thread, in rounds of 50 pages that are checkpointed as before.

Start with 2 or 3 and watch for `cooling down` in `--show-progress`; more workers than the portal tolerates
just means more cool-downs.

### Throttling: slow down, back off, probe, retry

If a portal pushes back, the crawler adapts instead of hammering it or failing the run. Three layers:

1. **Slow down.** On every HTTP 429/503 the minimum gap between request starts becomes 1.5x the pace we
   were really keeping (never below `delay_seconds`, at most once a minute, capped at 12 s), and it relaxes
   one step after every 10 quiet minutes. The point is to stay under the portal's limit so it never
   escalates to a hard block.
2. **Back off and probe.** After the short retries in `PortalSession` (3 attempts, 5 s then 10 s apart),
   these count as **throttling**: HTTP 429/503, a timeout after we connected ("Operation timed out ... 0 bytes
   received", the symptom Kerala showed) and a dropped connection. The source then waits **1 minute** and
   retries the same operation (the organisation list, one organisation's listing, or one detail page).
   Still throttled: **2, 4, 8, then 15 minutes** (the cap). If the portal sends `Retry-After` that is
   obeyed instead. After about an hour of waiting in total (`throttle_max_cooldowns` x the cap) the source
   gives up for this run, **keeps everything saved so far** (nothing is deactivated) and the next run resumes.
3. **Learn.** Every 429/503 is recorded with the rate-limit headers and body the portal sent, how many
   requests and seconds passed since the previous one, and the spacing in force; every wait and every
   recovery ("answers again after 3 min") too. They go to `data/<slug>/_throttle.json` (last 3 runs, not
   published); the status keeps `throttle_recoveries` (seconds the portal really needed), `throttled` (waits)
   and `gave_up`. Use them to tune `delay_seconds` and the cap.

Other sources keep crawling meanwhile, and a source in a wait shows as `cooling down until HH:MM (throttled)`
in `--show-progress` (the wait writes a progress heartbeat every 30 s, so it is never flagged `STALE`).

A **host that does not exist** (DNS failure; nProcure still lists clients whose subdomain was removed) is
not a crawl failure: that organisation is skipped with a warning, counted in the status as `orgs_unreachable`,
the run stays `ok`, and the tenders already stored for it stay active (we cannot tell they were removed).
Connection refused or timed out while connecting, a 404 or a 500 are ordinary errors with **no** cool-down.
On nProcure each client is its own host, so a client that stays throttled
is skipped after its cool-downs and the crawl continues; three in a row end the source for this run.
Tune per source in `sources.yaml`: `throttle_probe_minutes` (first wait, default 1), `throttle_cooldown_minutes`
(longest wait, default 15; set both equal for a flat cool-down) and `throttle_max_cooldowns` (default 4;
total patience = this x the longest wait).

### Nothing is ever deleted

A tender missing from a complete, error-free full crawl stays in the data with `active: false`,
`inactive_since`, `last_seen_active` and a `status`:

| status | meaning |
|---|---|
| `active` | on the portal's current listing |
| `closed` | dropped from the listing on/after its closing date (normal expiry) |
| `removed` | dropped *before* its closing date (cancelled, scrapped or moved) |

If it reappears it is reactivated. `--limit-orgs` / `--org` runs and runs with any organisation-level
error never deactivate anything. A failed *detail* page does not count as an error for this purpose:
the listing is intact. The site hides closed tenders by default; **Show closed** loads and shows them.

## The site: lazy loading

The first paint needs about 200 KB (gzipped) instead of every tender. The crawler's last step
(`crawler/index.py`, also `python -m crawler.run --index-only`) writes, under `docs/data/`:

| file | what it is |
|---|---|
| `index.json` | manifest: per source its counts, `stats` (full counts of new / closing soon / recently closed) and chunk files; where the presets and `orgs.json` are |
| `orgs.json` | every organisation (~1,200) with its active / closed counts and the chunk files holding its rows, and the category counts: all filters can be drawn without any tender data |
| `presets/new.json`, `closing.json`, `closed-recent.json` | the landing views, at most 2,000 rows each, most relevant first: published in the last 2 days, deadline in the next 3 days, dropped off the portal in the last 7 days (`truncated` holds the full count when a file was cut) |
| `index/<slug>-live-<n>.json`, `-closed-<n>.json` | the tenders, ≤ 3,000 rows per chunk, organisations kept together so a new tender only changes its own organisation's chunk |
| `status.json` | last crawl status per source |

The page loads the manifest and `orgs.json`, paints a preset, and fetches chunks only when a view needs
them: an organisation's files, a source's live chunks, or every live chunk for *All tenders* or a search
(with a loading percentage). Closed chunks load only when *Show closed* is on. Search is client-side over
the rows that are loaded. The windows and caps are constants at the top of `crawler/index.py`.

## The map

The site has a map of India: each state or UT is coloured by its active tenders (or new tenders, or those
closing within 48 hours); hovering shows active, new (2 days), closing within 48 hours, the number of
organisations and when it was last crawled; clicking opens that state's tender list. Small places (Delhi,
Chandigarh, Goa, Puducherry, Dadra & Nagar Haveli and Daman & Diu, Lakshadweep) get round markers so they can
be clicked, and a tile-grid style is the phone, keyboard and screen-reader alternative.

* **Which source is which state** comes from the `state:` key of each block in `sources.yaml` (an ISO 3166-2 code
  such as `IN-KL`, or `central` for the all-India source). The index carries it, so the page hard-codes no slugs;
  a new state source only needs its `state:`. Several sources may share one state (their numbers are summed).
  States without a source are drawn as "not on TenderWatch yet"; Chhattisgarh is hatched (its portal needs a CAPTCHA).
* **Numbers** are in `docs/data/index.json` (`sources[].stats`: `new`, `closing`, `closing48`, `orgs`; `closing48` is
  the deadline within 48 hours, `orgs` the organisations with an active tender).
* **Boundaries** are `docs/map/india-states.json`: simplified SVG paths, 36 units, about 72 KB (25 KB gzipped),
  fetched only when the map is shown. They come from **geoBoundaries India ADM1** (data from DataMeet / the Election
  Commission of India, **CC BY 2.5 India**; the page shows the attribution). Some state lines may predate the latest
  delimitation. Regenerate with the stdlib-only tool, from a downloaded copy of the "simplified" GeoJSON:

  ```bash
  python -I -X utf8 tools/build_map.py geoBoundaries-IND-ADM1_simplified.geojson docs/map/india-states.json
  ```

  India requires the official depiction of its external boundary: check the north and north-east of the map
  (J&K, Ladakh, Arunachal Pradesh) against the official map whenever the boundary file is replaced.

## Record fields

Listing (always): `id`, `source`, `ref`, `title`, `org`, `chain`, `pub`, `close`, `open`,
`first_seen`, `last_seen`, `active`, `status`, `inactive_since` / `last_seen_active` (once inactive),
`needs_details` (listing changed), `details_fetched_at`.
Detail (if `fetch_details`): `category`, `product_category`, `value`, `emd`, `location`, `pincode`,
`bid_end`, `contract_type`, `form_of_contract`, `tender_type`, `period`, `authority`,
`authority_address`, `description`, `documents`, plus `details` — every caption/value pair on the
page, raw. (`value` is absent when the portal says `NA`/`0`.)

## Roadmap

- **nProcure (Gujarat)**: the first full crawl of the `gujarat` source is still to be run (a single
  small client was crawled live and verified; see "How the nProcure crawl works"). Open items:
  whether to ask the operator for permission, and corrigenda (the listing shows a corrigendum link per
  tender but its format is not captured yet, so for now a revised closing date or estimate, or the
  14-day safety net, triggers the detail refresh). Other states on nProcure would be config only.
- CPPP (`eprocure.gov.in/eprocure/app`) and other state GePNIC portals: config only. Before adding
  many, read each portal's organisation-list page (one request, shows tender counts up front) to size
  the work: at ~1.2 s per detail page, lakhs of tenders only stay practical with change-driven detail
  fetching and per-portal parallelism, both of which are in place.
- Very large sources: an organisation file over ~50k rows should be split further (e.g. by publish year).
- Corrigenda: GePNIC has a *Latest Corrigendums* list; attach to tenders by ID.
- Alerts: a saved search → email/Telegram on new matches (another scheduled workflow).

## Being a good citizen

One request per second per portal, one session, no CAPTCHA solving, no logins, no document
downloads. Everything collected is already public on the portal's front page. The crawler reads
`robots.txt` and refuses disallowed URLs unless a source explicitly sets `ignore_robots: true`, and
identifies itself with a `From` header (override with `TENDERWATCH_CONTACT`).
