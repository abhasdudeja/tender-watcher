"""NIC GePNIC portals (etenders.kerala.gov.in, eprocure.gov.in, most state portals).

Flow: Tenders by Organisation -> each org's tender list -> each new
tender's detail page. Every link carries a session-bound `sp=` token, so details
are fetched right after the page that lists them, and a Stale Session page makes us
restart the session and re-walk that organisation.

The portal returns ALL of an organisation's tenders in a single response (paging is
client-side only), so there is normally no Next link; _find_next is a fallback in case
a portal version paginates server-side.

Parsers are pure functions over HTML so they can be tested against saved fixtures.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .http import PortalSession, StaleSession  # noqa: F401  (StaleSession re-exported)
from .progress import Progress

log = logging.getLogger("tenderwatch.gepnic")

MAX_RESTARTS = 5
MAX_PAGES_PER_ORG = 3000
DETAIL_CHUNK = 50          # checkpoint after this many detail pages
CHANGE_KEYS = ("close", "open", "title", "ref", "pub")   # listing fields whose change means "re-fetch details"
REFRESH_SLACK_HOURS = 1.0  # tolerance so a cron that fires a few minutes late doesn't skip yesterday's tenders
IST = timezone(timedelta(hours=5, minutes=30))   # portal dates are IST wall-clock
# Keys filled in from the detail page; wiped before a refresh so a field the portal
# dropped doesn't linger from an older fetch.
DETAIL_FIELDS = ("category", "product_category", "value", "emd", "location", "pincode", "bid_end",
                 "contract_type", "form_of_contract", "tender_type", "period", "authority",
                 "authority_address", "description", "documents", "details")

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_DATE_RE = re.compile(
    r"(\d{1,2})-([A-Za-z]{3})[a-z]*-(\d{4})(?:\s+(\d{1,2}):(\d{2})(?::\d{2})?\s*([AaPp][Mm])?)?")
_MONEY_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


# ---------------------------------------------------------------- parsing helpers
def parse_date(text: Optional[str]) -> Optional[str]:
    """'17-Oct-2026 06:55 PM' -> '2026-10-17T18:55'; date only -> '2026-10-17'."""
    m = _DATE_RE.search(text or "")
    if not m:
        return None
    day, mon, year, hh, mm, ampm = m.groups()
    month = _MONTHS.get(mon.lower())
    if not month:
        return None
    try:
        datetime(int(year), month, int(day))
    except ValueError:
        return None
    out = f"{int(year):04d}-{month:02d}-{int(day):02d}"
    if hh is None:
        return out
    h = int(hh)
    if ampm:
        h = h % 12 + (12 if ampm.lower() == "pm" else 0)
    return f"{out}T{h:02d}:{mm}"


def parse_money(text: Optional[str]) -> Optional[float]:
    """'Rs. 12,34,567.50' -> 1234567.5 ; '' / 'NA' -> None."""
    m = _MONEY_RE.search(text or "")
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _text(el) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)) if el is not None else ""


def parse_org_list(html: str, base_url: str) -> List[Dict]:
    """Rows of 'Tenders by Organisation': [{name, count, url}]. Orgs without a link are skipped."""
    soup = BeautifulSoup(html, "lxml")
    orgs = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 3:
            continue
        a = tds[-1].find("a", href=True)
        if not a:
            continue
        digits = re.sub(r"\D", "", _text(a))
        name = _text(tds[-2])
        if not digits or not name:
            continue
        orgs.append({"name": name, "count": int(digits), "url": urljoin(base_url, a["href"])})
    return orgs


def _find_next(soup: BeautifulSoup, base_url: str) -> Optional[str]:
    a = soup.find("a", id="loadNext", href=True) or soup.find("a", title="Next", href=True)
    return urljoin(base_url, a["href"]) if a else None


def parse_tender_list(html: str, base_url: str) -> Dict:
    """One page of an organisation's tenders -> {"rows": [...], "next_url": str|None}.

    Header-driven: columns are located by their header text, so extra/reordered
    columns between portal versions do not break it.
    """
    soup = BeautifulSoup(html, "lxml")
    rows: List[Dict] = []
    for table in soup.find_all("table"):
        col: Dict[str, int] = {}
        for tr in table.find_all("tr"):
            if tr.find_parent("table") is not table:
                continue
            cells = tr.find_all(["td", "th"], recursive=False)
            # header row: <tr class="list_header"> (real portal) or cells with class list_header (older markup)
            is_head = "list_header" in (tr.get("class") or []) or any(
                "list_header" in (c.get("class") or []) or c.name == "th" for c in cells)
            if is_head:
                if col:
                    continue
                for i, h in enumerate(_text(c).lower() for c in cells):
                    if "publish" in h:
                        col["pub"] = i
                    elif "closing" in h:
                        col["close"] = i
                    elif "opening" in h:
                        col["open"] = i
                    elif "title" in h or "tender id" in h:
                        col["title"] = i
                    elif "organisation" in h or "chain" in h:
                        col["chain"] = i
                continue
            if not col or "title" not in col or len(cells) <= col["title"]:
                continue
            tcell = cells[col["title"]]
            a = tcell.find("a")
            title = _text(a) if a else ""
            if title.startswith("[") and title.endswith("]"):  # real portal wraps the title in [ ]
                title = title[1:-1].strip()
            rest = tcell.get_text(" ", strip=True)
            if a:
                rest = rest.replace(_text(a), "", 1)
            brackets = re.findall(r"\[([^\]]*)\]", rest)
            if not brackets:
                continue
            tid = brackets[-1].strip()
            ref = brackets[-2].strip() if len(brackets) > 1 else ""
            if not tid:
                continue

            def cell(key):
                i = col.get(key)
                return _text(cells[i]) if i is not None and i < len(cells) else ""

            rows.append({
                "id": tid,
                "ref": ref,
                "title": title or _text(tcell),
                "pub": parse_date(cell("pub")),
                "close": parse_date(cell("close")),
                "open": parse_date(cell("open")),
                "chain": cell("chain"),
                "detail_url": urljoin(base_url, a["href"]) if a and a.get("href") else None,
            })
        if rows:
            break
    return {"rows": rows, "next_url": _find_next(soup, base_url)}


# caption prefix (lower-case) -> record field. The first matching caption wins.
# id / ref / title / chain / pub / open come from the listing and are not taken from the detail page.
DETAIL_MAP = {
    "tender category": "category",
    "product category": "product_category",
    "tender value": "value",
    "emd amount": "emd",
    "location": "location",
    "pincode": "pincode",
    "pin code": "pincode",
    "bid submission end date": "bid_end",
    "contract type": "contract_type",
    "form of contract": "form_of_contract",
    "period of work": "period",
    "tender type": "tender_type",
    "work description": "description",
    "name": "authority",              # inside the 'Tender Inviting Authority' block
    "address": "authority_address",
}
DOC_EXT = (".pdf", ".zip", ".doc", ".docx", ".xls", ".xlsx", ".rar")


def parse_tender_detail(html: str) -> Dict:
    """Detail page -> typed fields plus `details`, every caption/value pair raw."""
    soup = BeautifulSoup(html, "lxml")
    details: Dict[str, str] = {}
    # Strategy 1: <td class="td_caption">Label</td><td class="td_field">Value</td>
    for cap in soup.find_all("td", class_=lambda c: c and "caption" in c.lower()):
        val = cap.find_next_sibling("td")
        key = _text(cap).rstrip(":").strip()
        if key and val is not None and key not in details:
            details[key] = _text(val)
    # Strategy 2: plain two-column tables, for markup variants without the caption classes
    if not details:
        for tr in soup.find_all("tr"):
            tds = tr.find_all("td", recursive=False)
            if len(tds) == 2:
                key = _text(tds[0]).rstrip(":").strip()
                if key and len(key) < 80 and key not in details:
                    details[key] = _text(tds[1])

    out: Dict = {}
    for key, val in details.items():
        low = key.lower()
        for needle, field in DETAIL_MAP.items():
            if low.startswith(needle):
                out.setdefault(field, val)
                break
    for f in ("value", "emd"):
        if f in out:
            out[f] = parse_money(out[f])
    if out.get("value") == 0:
        out["value"] = None               # 0.00 means "not stated"
    if "bid_end" in out:
        out["bid_end"] = parse_date(out["bid_end"])
    docs = sorted({_text(a) for a in soup.find_all("a", href=True) if _text(a).lower().endswith(DOC_EXT)})
    if docs:
        out["documents"] = docs
    out["details"] = details
    return {k: v for k, v in out.items() if v not in (None, "")}


def looks_like_captcha_wall(html: str) -> bool:
    low = html.lower()
    return "captcha" in low and "td_caption" not in low and "tender id" not in low


# ---------------------------------------------------------------- crawl
def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def crawl_source(src: Dict, store: Dict[str, Dict], limit_orgs: Optional[int] = None,
                 only_orgs: Optional[List[str]] = None, no_details: bool = False,
                 dump_dir: Optional[str] = None, checkpoint=None, progress=None,
                 force_refresh: bool = False, stop=None) -> Dict:
    """Crawl one GePNIC portal into `store` (id -> record). Returns a status dict.

    checkpoint(org_name): called after each org's listing, every DETAIL_CHUNK detail pages and after
    each org, with the org in flight so the store can rewrite just that org's shard; the caller
    does a full save afterwards (deactivations touch many orgs).
    progress(snapshot): optional sink for the per-source progress counter (see crawler/progress.py).
    stop: optional threading.Event; when set the crawl winds down after its current request
    (the run is then flagged not-ok, so nothing is deactivated).

    Refresh policy (force_refresh ignores all of it):
      * a tender's detail page is fetched when it is new, when its LISTING changed since we last
        saw it (closing/opening date, title, ref, publish date - flagged needs_details so an
        interrupted run still picks it up), or as a safety net when the details are older than
        src["detail_max_age_days"] (default 14, 0 = never). Otherwise it is left alone;
      * an organisation is re-listed at most every src["refresh_after_hours"] (default 48), and is
        skipped outright (no listing request) when its listing is fresh, the portal's tender count
        equals our stored active count, and no tender needs details. A count mismatch forces a
        re-list even inside the window.
    Retention: nothing is ever deleted. A tender missing from a complete, error-free full
    crawl gets active=False, inactive_since, and status "closed" (its closing date had
    passed) or "removed" (it vanished before its deadline). If it reappears it is reactivated.
    Records missing from a complete, error-free full crawl are marked inactive;
    partial runs (limit_orgs / only_orgs) never deactivate.
    """
    base = src["base_url"].rstrip("/")
    slug = src["slug"]
    sess = PortalSession(base, delay_seconds=float(src.get("delay_seconds", 1.0)),
                         dump_dir=dump_dir, ignore_robots=bool(src.get("ignore_robots", False)),
                         extra_ca_certs=src.get("extra_ca_certs"))
    want_details = bool(src.get("fetch_details")) and not no_details
    max_details = int(src.get("max_detail_fetches", 500))
    org_list_url = f"{base}?page=FrontEndTendersByOrganisation&service=page"
    refresh_hours = float(src.get("refresh_after_hours", 48))     # how often an org is re-listed
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=refresh_hours - REFRESH_SLACK_HOURS)
              ).strftime("%Y-%m-%dT%H:%M:%SZ")
    max_age_days = float(src.get("detail_max_age_days", 14))      # safety net only; 0 disables
    detail_cutoff = ((datetime.now(timezone.utc) - timedelta(days=max_age_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
                     if max_age_days > 0 else "")

    status = {"slug": slug, "name": src.get("name", slug), "ok": True, "started": _now(),
              "orgs": 0, "orgs_skipped": 0, "orgs_failed": 0, "listed": 0, "new": 0, "changed": 0,
              "details_failed": 0, "details_fetched": 0, "errors": []}
    seen = set()
    rows_parsed = 0
    prog: Optional[Progress] = None

    def counters() -> Dict:
        return dict(listed=len(seen), new=status["new"], details_fetched=status["details_fetched"],
                    errors=len(status["errors"]))

    def error(msg: str) -> None:
        """An organisation-level failure: the listing is incomplete, so the run is not ok and nothing is deactivated."""
        log.error(msg)
        status["ok"] = False
        status["orgs_failed"] += 1
        if len(status["errors"]) < 20:
            status["errors"].append(msg)

    def want_details_off() -> None:
        nonlocal want_details
        want_details = False

    def detail_failed(msg: str) -> None:
        """One detail page failed: counted and reported, but the listing is intact so the run stays ok."""
        log.warning(msg)
        status["details_failed"] += 1
        if len(status["errors"]) < 20:
            status["errors"].append(msg)

    def load_orgs() -> List[Dict]:
        return parse_org_list(sess.get(org_list_url, label="orgs"), base)

    def upsert(row: Dict, org: str) -> Dict:
        now = _now()
        rec = store.get(row["id"])
        if rec is None:
            rec = {"id": row["id"], "source": slug, "first_seen": now}
            store[row["id"]] = rec
            status["new"] += 1
        elif "details_fetched_at" in rec and any(
                row.get(k) and rec.get(k) and row[k] != rec[k] for k in CHANGE_KEYS):
            rec["needs_details"] = True      # e.g. closing date extended / corrigendum: refresh its details
            status["changed"] += 1
        for k in ("ref", "title", "chain", "pub", "close", "open"):
            if row.get(k):
                rec[k] = row[k]
        rec.update(org=org, last_seen=now, active=True, status="active")
        rec.pop("inactive_since", None)   # reappeared after having dropped off
        return rec

    def details_stale(rec: Dict) -> bool:
        fetched = rec.get("details_fetched_at", "")
        return bool(force_refresh or not fetched or rec.get("needs_details")
                    or (detail_cutoff and fetched < detail_cutoff))

    def fresh_org_ids(name: str, count: int):
        """Ids of an org's stored tenders if it can be skipped this run, else None."""
        if force_refresh:
            return None
        recs = [r for r in store.values() if r.get("org") == name and r.get("active")]
        if not recs or len(recs) != count:          # new org, or portal count != our count
            return None
        if max(r.get("last_seen", "") for r in recs) < cutoff:
            return None                             # listing older than the refresh window
        if want_details and any(details_stale(r) for r in recs):
            return None
        return [r["id"] for r in recs]

    def mark_inactive(rec: Dict) -> None:
        now_ist = datetime.now(IST).strftime("%Y-%m-%dT%H:%M")
        rec["active"] = False
        rec["last_seen_active"] = rec.get("last_seen")
        rec["inactive_since"] = _now()
        rec["status"] = "closed" if rec.get("close") and rec["close"] <= now_ist else "removed"

    def walk_org(name: str, url: str) -> None:
        """Phase A: list the org (normally one big response) and checkpoint it.
        Phase B: fetch details in chunks, checkpointing after each chunk, so a crash,
        Ctrl-C or the per-run detail cap never loses more than DETAIL_CHUNK pages."""
        nonlocal rows_parsed
        visited = set()
        pending: List[Dict] = []
        pages = 0
        while url and url not in visited and pages < MAX_PAGES_PER_ORG:
            visited.add(url)
            pages += 1
            page = parse_tender_list(sess.get(url, label="list"), base)
            if not page["rows"] and pages == 1:
                log.warning("%s: first page parsed 0 rows", name)
            for row in page["rows"]:
                rows_parsed += 1
                seen.add(row["id"])
                rec = upsert(row, name)
                if want_details and row.get("detail_url") and details_stale(rec):
                    pending.append((rec, row["detail_url"]))
            url = page["next_url"]
        if checkpoint:
            checkpoint(name)
        if prog:
            prog.update(phase="details", org_frac=0.0, force=True, **counters())
        if not pending:
            return
        # never-detailed tenders first, then changed/oldest ones; newest published first within each
        pending.sort(key=lambda p: p[0].get("pub") or "", reverse=True)
        pending.sort(key=lambda p: p[0].get("details_fetched_at", ""))
        todo = pending[: max(0, max_details - status["details_fetched"])]
        log.info("%s: %d need details, fetching %d this run", name, len(pending), len(todo))
        for n, (rec, durl) in enumerate(todo, 1):
            if stop is not None and stop.is_set():
                return
            try:
                html = sess.get(durl, label="detail")
                if looks_like_captcha_wall(html):
                    detail_failed("detail pages appear CAPTCHA-gated; skipping details for the rest of this run")
                    want_details_off()
                    return
                fetched = parse_tender_detail(html)
                if not fetched.get("details"):
                    detail_failed(f"detail {rec['id']}: no caption/value pairs parsed (markup changed?)")
                    continue
                for k in DETAIL_FIELDS:
                    rec.pop(k, None)
                rec.update(fetched)
                rec.pop("needs_details", None)
                rec["details_fetched_at"] = _now()
                status["details_fetched"] += 1
            except StaleSession:
                raise  # restart + re-list; already-fetched tenders are skipped next time
            except Exception as e:  # noqa: BLE001
                detail_failed(f"detail {rec['id']}: {e.__class__.__name__}: {e}"[:200])
            if checkpoint and n % DETAIL_CHUNK == 0:
                checkpoint(name)
            if prog and (n % 10 == 0 or n == len(todo)):
                prog.update(org_frac=n / len(todo), **counters())

    try:
        orgs = load_orgs()
    except Exception as e:  # noqa: BLE001
        error(f"organisation list: {e}")
        status["finished"] = _now()
        status["requests"] = getattr(sess, "requests_made", 0)
        return status
    if not orgs:
        error("organisation list parsed empty")
    full_run = not limit_orgs and not only_orgs
    if only_orgs:
        wanted = {o.lower() for o in only_orgs}
        orgs = [o for o in orgs if o["name"].lower() in wanted]
    if limit_orgs:
        orgs = orgs[:limit_orgs]

    prog = Progress(slug, status["name"], len(orgs), sum(o["count"] for o in orgs), sink=progress)
    for i, org in enumerate(orgs, 1):
        if stop is not None and stop.is_set():
            error("interrupted before all organisations were crawled")
            break
        name, url = org["name"], org["url"]
        before = rows_parsed
        prog.start_org(name, org["count"])
        skip_ids = fresh_org_ids(name, org["count"])
        if skip_ids is not None:
            log.info("%s: unchanged (%d tenders, fresh within %gh) - skipped", name, len(skip_ids), refresh_hours)
            seen.update(skip_ids)          # still on the portal, so don't deactivate them below
            status["orgs"] += 1
            status["orgs_skipped"] += 1
            prog.update(**counters())
            prog.finish_org(skipped=True)
            continue
        log.info("[%d/%d] %s (portal says %d)", i, len(orgs), name, org["count"])
        for attempt in range(MAX_RESTARTS + 1):
            try:
                walk_org(name, url)
                break
            except StaleSession:
                if attempt == MAX_RESTARTS:
                    error(f"{name}: session kept going stale")
                    break
                log.info("%s: stale session, restarting (%d)", name, attempt + 1)
                try:
                    sess.restart()
                    fresh = {o["name"]: o["url"] for o in load_orgs()}
                    if name not in fresh:
                        error(f"{name}: missing from organisation list after restart")
                        break
                    url = fresh[name]
                except Exception as e:  # noqa: BLE001
                    error(f"{name}: restart failed: {e}")
                    break
            except Exception as e:  # noqa: BLE001
                error(f"{name}: {e}")
                break
        got = rows_parsed - before
        if got == 0 and org["count"] > 0 and status["ok"]:
            error(f"{name}: portal lists {org['count']} tenders but 0 rows parsed (markup changed?)")
        elif got < org["count"]:
            log.warning("%s: got %d of %d tenders", name, got, org["count"])
        status["orgs"] += 1
        if checkpoint:
            checkpoint(name)
        prog.update(**counters())
        prog.finish_org()

    status["listed"] = len(seen)
    status["active"] = sum(1 for r in store.values() if r.get("active"))
    if full_run and status["ok"] and orgs:
        for tid, rec in store.items():
            if tid not in seen and rec.get("active"):
                mark_inactive(rec)
    status["finished"] = _now()
    status["requests"] = getattr(sess, "requests_made", 0)
    prog.update(**counters())
    prog.finish()
    return status
