"""Parser + crawl routine for NIC GePNIC eProcurement portals.

Pure parsing functions take HTML and return plain dicts, so they can be unit
tested against saved pages. `crawl_source` drives a PortalSession through:

    Tenders by Organisation  ->  per-org tender list (paged)  ->  tender detail

Everything is header-driven and regex-tolerant because GePNIC markup varies a
little between portal versions.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from .http import PortalSession, StaleSession

log = logging.getLogger(__name__)

ORG_LIST_PAGE = "?page=FrontEndTendersByOrganisation&service=page"

TENDER_ID_RE = re.compile(r"\b(\d{4}_[A-Za-z0-9]+_\d+_\d+)\b")
BRACKET_RE = re.compile(r"\[([^\]]+)\]")
DATE_RE = re.compile(r"(\d{1,2}-[A-Za-z]{3}-\d{4})(?:\s+(\d{1,2}:\d{2})\s*([AP]M))?")
MONEY_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def _text(el: Optional[Tag]) -> str:
    if el is None:
        return ""
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()


def parse_date(s: str) -> Optional[str]:
    """'17-Oct-2026 06:55 PM' -> '2026-10-17T18:55'; date-only -> '2026-10-17'."""
    if not s:
        return None
    m = DATE_RE.search(s)
    if not m:
        return None
    d, hm, ampm = m.groups()
    try:
        if hm and ampm:
            dt = datetime.strptime(f"{d} {hm} {ampm}", "%d-%b-%Y %I:%M %p")
            return dt.strftime("%Y-%m-%dT%H:%M")
        return datetime.strptime(d, "%d-%b-%Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


def parse_money(s: str) -> Optional[float]:
    if not s:
        return None
    s = s.replace("₹", "").replace("Rs.", "").replace("INR", "")
    m = MONEY_RE.search(s)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _header_index(headers: List[str]) -> Dict[str, int]:
    """Map semantic column names to indices using keyword matching."""
    idx: Dict[str, int] = {}
    for i, h in enumerate(headers):
        hl = h.lower()
        if "publish" in hl and "pub" not in idx:
            idx["pub"] = i
        elif "clos" in hl and "close" not in idx:
            idx["close"] = i
        elif "open" in hl and "open" not in idx:
            idx["open"] = i
        elif "title" in hl and "title" not in idx:
            idx["title"] = i
        elif ("organisation" in hl or "organization" in hl or "chain" in hl) and "chain" not in idx:
            idx["chain"] = i
    return idx


def _find_list_table(soup: BeautifulSoup) -> Optional[Tuple[Tag, Dict[str, int]]]:
    """Find the table whose header row contains a 'Title' column."""
    best = None
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = tr.find_all(["th", "td"], recursive=False)
            headers = [_text(c) for c in cells]
            if not headers:
                continue
            idx = _header_index(headers)
            if "title" in idx and len(idx) >= 2:
                # Prefer the deepest table that owns this header (avoid wrapper tables)
                best = (table, idx)
        if best and best[0] is table:
            # keep searching nested tables; the innermost one that still
            # yields a match will overwrite `best`
            continue
    return best


# --------------------------------------------------------------------------
# page parsers
# --------------------------------------------------------------------------
def parse_org_list(html: str, base_url: str) -> List[Dict]:
    """Rows of the 'Tenders by Organisation' table.

    Returns [{"name": str, "count": int, "url": str}, ...]
    """
    soup = _soup(html)
    out: List[Dict] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "FrontEndTendersByOrganisation" not in href or "service=direct" not in href:
            continue
        label = _text(a)
        if not label.isdigit():
            continue
        tr = a.find_parent("tr")
        if tr is None:
            continue
        tds = tr.find_all("td", recursive=False)
        name = ""
        if len(tds) >= 2:
            # the org name is the cell before the count cell
            count_td = a.find_parent("td")
            try:
                pos = tds.index(count_td)
                name = _text(tds[pos - 1]) if pos > 0 else _text(tds[0])
            except ValueError:
                name = _text(tds[1])
        if not name:
            continue
        out.append({"name": name, "count": int(label), "url": urljoin(base_url, href)})
    return out


def parse_tender_list(html: str, base_url: str) -> Dict:
    """Parse one page of an organisation's tender list.

    Returns {"rows": [...], "next_url": str|None}
    Each row: {"id", "ref", "title", "pub", "close", "open", "chain", "url"}
    """
    soup = _soup(html)
    found = _find_list_table(soup)
    rows: List[Dict] = []
    if found:
        table, idx = found
        for tr in table.find_all("tr"):
            cells = tr.find_all("td", recursive=False)
            if len(cells) <= max(idx.values()):
                continue
            title_cell = cells[idx["title"]]
            link = title_cell.find("a", href=True)
            if link is None:
                continue  # header or spacer row
            cell_text = _text(title_cell)
            title = _text(link)
            tid_m = TENDER_ID_RE.search(cell_text)
            tid = tid_m.group(1) if tid_m else None
            # reference number: bracketed token that is not the tender id
            ref = ""
            for b in BRACKET_RE.findall(cell_text):
                b = b.strip()
                if tid and b == tid:
                    continue
                if b and b != title:
                    ref = b
                    break
            if not ref:
                rest = cell_text.replace(title, "", 1)
                if tid:
                    rest = rest.replace(tid, "")
                ref = rest.strip(" []")
            if not tid:
                # fall back to a synthetic id from the href so we still record it
                tid = "noid_" + re.sub(r"[^A-Za-z0-9]", "", link["href"])[-24:]
            rows.append(
                {
                    "id": tid,
                    "ref": ref,
                    "title": title,
                    "pub": parse_date(_text(cells[idx["pub"]])) if "pub" in idx else None,
                    "close": parse_date(_text(cells[idx["close"]])) if "close" in idx else None,
                    "open": parse_date(_text(cells[idx["open"]])) if "open" in idx else None,
                    "chain": _text(cells[idx["chain"]]) if "chain" in idx else "",
                    "url": urljoin(base_url, link["href"]),
                }
            )
    return {"rows": rows, "next_url": _find_next(soup, base_url)}


def _find_next(soup: BeautifulSoup, base_url: str) -> Optional[str]:
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "service=direct" not in href:
            continue
        label = (_text(a) or "").lower()
        title = (a.get("title") or "").lower()
        aid = (a.get("id") or "").lower()
        img = a.find("img")
        alt = (img.get("alt") or "").lower() if img else ""
        if (
            label in ("next", "next »", "»", ">", "next >")
            or title == "next"
            or alt == "next"
            or aid in ("loadnext", "linkfwd")
        ):
            return urljoin(base_url, href)
    return None


DETAIL_MAP = {
    "tender category": "category",
    "product category": "product_category",
    "tender value in": "value",
    "tender value": "value",
    "emd amount in": "emd",
    "emd amount": "emd",
    "location": "location",
    "pincode": "pincode",
    "pin code": "pincode",
    "bid submission end date": "bid_end",
    "bid opening date": "open",
    "published date": "pub",
    "contract type": "contract_type",
    "form of contract": "form_of_contract",
    "period of work": "period",
    "tender type": "tender_type",
    "organisation chain": "chain",
    "tender reference number": "ref",
    "tender id": "id",
    "title": "title",
    "work description": "description",
    "name": "authority",  # inside 'Tender Inviting Authority' block
    "address": "authority_address",
}


def parse_tender_detail(html: str) -> Dict:
    """Generic caption/value scrape of a GePNIC tender detail page.

    Returns {"details": {caption: value, ...}, plus mapped keys}.
    """
    soup = _soup(html)
    pairs: Dict[str, str] = {}

    # Strategy 1: GePNIC uses <td class="td_caption">Label</td><td class="td_field">Value</td>
    captions = soup.find_all("td", class_=lambda c: c and "caption" in c.lower())
    for cap in captions:
        val = cap.find_next_sibling("td")
        if val is None:
            continue
        k = _text(cap).rstrip(":").strip()
        v = _text(val)
        if k and k not in pairs:
            pairs[k] = v

    # Strategy 2: plain 2-column tables (fallback when classes are absent)
    if not pairs:
        for tr in soup.find_all("tr"):
            tds = tr.find_all("td", recursive=False)
            if len(tds) == 2:
                k = _text(tds[0]).rstrip(":").strip()
                v = _text(tds[1])
                if k and len(k) < 80 and k not in pairs:
                    pairs[k] = v

    out: Dict = {"details": pairs}
    for k, v in pairs.items():
        kl = k.lower()
        for needle, field in DETAIL_MAP.items():
            if kl.startswith(needle) and field not in out:
                out[field] = v
                break
    for f in ("value", "emd"):
        if f in out:
            out[f] = parse_money(out[f])
    for f in ("bid_end", "open", "pub"):
        if f in out:
            out[f] = parse_date(out[f])
    # tender documents (names only; download needs the session)
    docs = []
    for a in soup.find_all("a", href=True):
        t = _text(a)
        if t.lower().endswith((".pdf", ".zip", ".doc", ".docx", ".xls", ".xlsx", ".rar")):
            docs.append(t)
    if docs:
        out["documents"] = sorted(set(docs))
    return out


def looks_like_captcha_wall(html: str) -> bool:
    low = html.lower()
    return "captcha" in low and "td_caption" not in low and "tender id" not in low


# --------------------------------------------------------------------------
# crawl
# --------------------------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _walk_org(sess: PortalSession, org: Dict, base_url: str, max_pages: int = 2000) -> List[Dict]:
    """Collect every row for one organisation, following Next links."""
    rows: List[Dict] = []
    url = org["url"]
    seen_urls = set()
    for page_no in range(1, max_pages + 1):
        if url in seen_urls:
            break
        seen_urls.add(url)
        html = sess.get(url, label=f"org_{org['name']}_p{page_no}")
        page = parse_tender_list(html, base_url)
        if not page["rows"] and page_no == 1:
            log.warning("No rows parsed for org %r (expected %s). Markup may differ; "
                        "run with --dump-html to inspect.", org["name"], org["count"])
        rows.extend(page["rows"])
        if not page["next_url"] or not page["rows"]:
            break
        url = page["next_url"]
    return rows


def _refresh_orgs(sess: PortalSession, base_url: str) -> List[Dict]:
    sess.restart()
    html = sess.get(base_url + ORG_LIST_PAGE, label="org_list")
    return parse_org_list(html, base_url)


def crawl_source(
    src: Dict,
    store: Dict[str, Dict],
    limit_orgs: Optional[int] = None,
    only_orgs: Optional[Iterable[str]] = None,
    dump_dir: Optional[str] = None,
    fetch_details: Optional[bool] = None,
    checkpoint=None,
) -> Dict:
    """Crawl one GePNIC source, updating `store` (tender_id -> record) in place.

    `checkpoint`, if given, is called with no arguments after every organisation
    so the caller can flush the store to disk; a crash then loses at most one org.
    Returns a status dict for status.json.
    """
    base_url = src["base_url"].rstrip("/")
    slug = src["slug"]
    want_details = src.get("fetch_details", False) if fetch_details is None else fetch_details
    detail_budget = int(src.get("max_detail_fetches", 500))
    sess = PortalSession(
        base_url,
        delay_seconds=float(src.get("delay_seconds", 1.0)),
        dump_dir=dump_dir,
        ignore_robots=bool(src.get("ignore_robots", False)),
    )

    status: Dict = {
        "slug": slug, "name": src.get("name", slug), "started": _now(),
        "ok": False, "orgs": 0, "orgs_failed": 0, "listed": 0, "new": 0,
        "details_fetched": 0, "details_failed": 0, "errors": [],
    }
    run_ts = _now()
    seen_ids: set = set()

    orgs = _refresh_orgs(sess, base_url)
    if only_orgs:
        wanted = {o.lower() for o in only_orgs}
        orgs = [o for o in orgs if o["name"].lower() in wanted]
    if limit_orgs:
        orgs = orgs[:limit_orgs]
    status["orgs"] = len(orgs)
    log.info("[%s] %d organisations, %d tenders listed on portal",
             slug, len(orgs), sum(o["count"] for o in orgs))
    if not orgs:
        status["errors"].append("organisation list parsed empty")

    for org in orgs:
        # ---- listing (with one stale-session retry) ----
        rows: List[Dict] = []
        for attempt in (1, 2):
            try:
                rows = _walk_org(sess, org, base_url)
                break
            except StaleSession:
                log.info("[%s] stale session while listing %r, restarting", slug, org["name"])
                fresh = _refresh_orgs(sess, base_url)
                match = [o for o in fresh if o["name"] == org["name"]]
                if not match:
                    break
                org = match[0]
            except Exception as e:  # noqa: BLE001
                status["orgs_failed"] += 1
                status["errors"].append(f"{org['name']}: {e.__class__.__name__}: {e}"[:200])
                log.exception("[%s] org %r failed", slug, org["name"])
                break
        log.info("[%s] %-60s %4d rows (portal says %d)", slug, org["name"][:60], len(rows), org["count"])
        status["listed"] += len(rows)

        # ---- merge into store ----
        pending_details: List[Dict] = []
        for r in rows:
            tid = r["id"]
            seen_ids.add(tid)
            rec = store.get(tid)
            if rec is None:
                rec = {"id": tid, "source": slug, "first_seen": run_ts}
                store[tid] = rec
                status["new"] += 1
            rec.update(
                {
                    "ref": r["ref"] or rec.get("ref", ""),
                    "title": r["title"] or rec.get("title", ""),
                    "org": org["name"],
                    "chain": r["chain"] or rec.get("chain", ""),
                    "pub": r["pub"] or rec.get("pub"),
                    "close": r["close"] or rec.get("close"),
                    "open": r["open"] or rec.get("open"),
                    "last_seen": run_ts,
                    "active": True,
                }
            )
            if want_details and not rec.get("details_fetched_at") and detail_budget > 0:
                pending_details.append(r)

        # ---- details for new tenders ----
        for r in pending_details:
            if detail_budget <= 0:
                break
            try:
                html = sess.get(r["url"], label=f"detail_{r['id']}")
            except StaleSession:
                # Links from the old listing are dead; skip details for this org this run.
                log.info("[%s] stale session on detail; deferring remaining details for %r",
                         slug, org["name"])
                try:
                    _refresh_orgs(sess, base_url)
                except Exception:  # noqa: BLE001
                    pass
                break
            except Exception as e:  # noqa: BLE001
                status["details_failed"] += 1
                status["errors"].append(f"detail {r['id']}: {e.__class__.__name__}"[:200])
                continue
            detail_budget -= 1
            if looks_like_captcha_wall(html):
                status["errors"].append("detail pages appear CAPTCHA-gated; details disabled this run")
                want_details = False
                break
            d = parse_tender_detail(html)
            if not d["details"]:
                status["details_failed"] += 1
                continue
            rec = store[r["id"]]
            for k, v in d.items():
                if k in ("id",):  # never overwrite the key
                    continue
                if v not in (None, "", [], {}):
                    rec[k] = v
            rec["details_fetched_at"] = run_ts
            status["details_fetched"] += 1

        if checkpoint is not None:
            try:
                checkpoint()
            except Exception:  # noqa: BLE001
                log.exception("[%s] checkpoint save failed", slug)

    # ---- deactivate tenders no longer listed (only if the crawl was broadly healthy) ----
    if orgs and status["orgs_failed"] < max(1, len(orgs) // 4) and not limit_orgs and not only_orgs:
        for tid, rec in store.items():
            if rec.get("source") == slug and tid not in seen_ids and rec.get("active"):
                rec["active"] = False
                rec["last_seen_active"] = rec.get("last_seen")

    status["finished"] = _now()
    status["requests"] = sess.requests_made
    status["active"] = sum(1 for r in store.values() if r.get("source") == slug and r.get("active"))
    status["ok"] = bool(orgs) and status["orgs_failed"] == 0
    status["errors"] = status["errors"][:25]
    return status
