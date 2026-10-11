# TenderWatch

Crawls government procurement portals on a schedule, stores every tender as plain
JSONL in git, and serves a fast client-side search as a static site. No database,
no server: GitHub Actions runs the crawler, GitHub Pages hosts the site.

```
sources.yaml          which portals to crawl (base URL + parser type)
crawler/              Python crawler  (python -m crawler.run)
data/<slug>.jsonl     every tender ever seen, one JSON per line  <- source of truth
docs/                 the static site (GitHub Pages serves this folder)
docs/data/index.json  compact index of ACTIVE tenders the site loads
docs/data/status.json last crawl status per source (shown at the bottom of the site)
tests/                parser tests against saved HTML fixtures
```

## Run it locally first

```bash
pip install -r requirements.txt
python -m crawler.run --limit-orgs 3            # smoke test: 3 organisations, with details
python -m crawler.run --org "ANERT" --org "Kerala State Road Transport Corporation"
python -m crawler.run                            # full crawl (Kerala: ~7k tenders, ~2h with details)
python -m crawler.run --no-details               # listing only (~15 min)
python -m http.server -d docs 8000               # open http://localhost:8000
```

If an org parses to 0 rows, re-run with `--dump-html debug/` and look at the saved
page: GePNIC markup varies slightly between portal versions, and `parse_tender_list`
in `crawler/gepnic.py` is header-driven so it is usually a one-line fix.

Tests: `python -m pytest tests` (or run `tests/test_gepnic.py` functions directly).

## Deploy (zero servers)

1. Push this folder to a new GitHub repo.
2. Settings → Pages → Source: **Deploy from a branch**, branch `main`, folder `/docs`.
3. Settings → Actions → General → Workflow permissions: **Read and write**.
4. Actions → "Crawl tenders" → Run workflow (tick *no_details* for the first run so it
   finishes in minutes). The workflow commits `data/` and `docs/data/`; Pages redeploys.
5. It then runs every 6 hours by itself. Change the cron in `.github/workflows/crawl.yml`.

**Check after the first Actions run:** gov.in portals sometimes block non-Indian IPs.
If the log shows timeouts or "organisation list parsed empty", run the crawler on a
machine in India (your laptop, a ₹300/month Indian VPS) and have *it* push to the repo;
the site stays on Pages. Nothing else changes.

## Adding a source

GePNIC (NIC) portals — eprocure.gov.in and most state eTenders sites — need only a
new block in `sources.yaml`:

```yaml
  - slug: tamilnadu
    name: Tamil Nadu eTenders
    type: gepnic
    base_url: https://tntenders.gov.in/nicgep/app
    enabled: true
    fetch_details: true
    max_detail_fetches: 1500
    delay_seconds: 1.0
```

Other platforms need a parser module exposing `crawl_source(src, store, ...)` (see
`crawler/nprocure.py` for the contract) and a line in `crawler/run.py:CRAWLERS`.

### How the GePNIC crawl works

Tenders by Organisation → each org's tender list (follows *Next*) → each new tender's
detail page. All links are tied to a server session (`sp=` tokens) and go stale in
minutes, so one `requests.Session` is used per source and a *Stale Session* page
triggers a restart. The CAPTCHA only guards the filter form, which is never used.

Because deep links expire, the site cannot link straight to a tender. **Open portal**
opens the portal home and **Copy Tender ID** puts the ID on the clipboard; the portal's
*Search with ID* box takes it.

## Record fields

Listing (always): `id`, `source`, `ref`, `title`, `org`, `chain`, `pub`, `close`, `open`,
`first_seen`, `last_seen`, `active`.
Detail (if `fetch_details`): `category`, `product_category`, `value`, `emd`, `location`,
`pincode`, `bid_end`, `contract_type`, `period`, `authority`, `authority_address`,
`description`, `documents`, plus `details` — every caption/value pair on the page, raw.

A tender missing from a healthy crawl is marked `active: false`, kept in the JSONL,
and dropped from the site index. `--limit-orgs` / `--org` runs never deactivate.

## Roadmap

- **Surat Municipal Corporation (nProcure)** — `smctender.nprocure.com`. Public
  government notices on a vendor-run platform. Its robots.txt blocks generic crawlers,
  so the cloud build environment could not fetch pages to write the parser. To do it:
  `python -m crawler.capture https://smctender.nprocure.com/ tests/fixtures/nprocure --follow 10`
  from your own machine, then implement `crawler/nprocure.py` against those files and
  set `enabled: true` (the block already has `ignore_robots: true`).
- CPPP (`eprocure.gov.in/eprocure/app`) and Gujarat/other state GePNIC portals: config only.
- Corrigenda: GePNIC has a *Latest Corrigendums* list; attach to tenders by ID.
- Alerts: a saved search → email/Telegram on new matches (another scheduled workflow).
- If total active tenders pass ~30k, split `index.json` per source and load lazily.

## Being a good citizen

One request per second per portal, one session, no CAPTCHA solving, no logins, no
document downloads. Everything collected is already public on the portal's front page.
The crawler reads `robots.txt` and refuses disallowed URLs unless a source explicitly
sets `ignore_robots: true`.
