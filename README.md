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
docs/data/index.json              manifest: per source its counts and chunk files
docs/data/index/<slug>-live-N.json    active tenders, loaded lazily by the site
docs/data/index/<slug>-closed-N.json  closed / removed tenders, loaded only on "Show closed"
docs/data/status.json             last crawl status per source (shown at the bottom of the site)
tests/                            parser, storage, migration, index and crawl-flow tests
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

Other platforms need a parser module exposing `crawl_source(src, store, ...)` (see
`crawler/nprocure.py` for the contract) and a line in `crawler/run.py:CRAWLERS`.

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

`docs/data/index.json` is a small manifest. The page loads it first (source list, counts), then
fetches only the chunk files the current view needs — the selected source's, or every source's in
the background for *All sources* (with a loading percentage) — and the **closed** chunks only once
*Show closed* is switched on. Chunks hold ≤ 20,000 rows, organisations packed together, so adding a
tender only changes its own organisation's chunk. Search is still entirely client-side.

## Record fields

Listing (always): `id`, `source`, `ref`, `title`, `org`, `chain`, `pub`, `close`, `open`,
`first_seen`, `last_seen`, `active`, `status`, `inactive_since` / `last_seen_active` (once inactive),
`needs_details` (listing changed), `details_fetched_at`.
Detail (if `fetch_details`): `category`, `product_category`, `value`, `emd`, `location`, `pincode`,
`bid_end`, `contract_type`, `form_of_contract`, `tender_type`, `period`, `authority`,
`authority_address`, `description`, `documents`, plus `details` — every caption/value pair on the
page, raw. (`value` is absent when the portal says `NA`/`0`.)

## Roadmap

- **Gujarat state portal (nProcure)** — `tender.nprocure.com`, source `gujarat` (enabled, parser not
  written yet, so it reports "not implemented"). One portal for ~165 *clients* (departments, boards,
  municipal corporations, all Nagarpalikas) with ~4,000 active tenders and a per-client count up
  front, which maps onto the per-organisation shards and skip rules. Each client has its own subdomain
  (listed in the state home page's dropdown; e.g. Surat = `smctender.nprocure.com`), and that client
  page is the authoritative per-client listing (its total equals the dropdown count; the state-level
  table is ~35 tenders short), so the crawl goes client by client.
  Findings so far: no robots rules (`/robots.txt` returns the app's HTML); the public listing needs
  no login or CAPTCHA (that is only on sign-up); the server omits its TLS intermediate (handled by
  `extra_ca_certs`, see `certs/README.md`); the listing is `POST /beforeLoginTenderTableList`
  (DataTables, up to 150 rows per page) whose request body the page's JavaScript encrypts; tender
  details are `POST /view-nit-home`. The open question is how to produce that request (re-implement
  the encryption vs drive a headless browser) and whether to ask the operator for permission first.
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
