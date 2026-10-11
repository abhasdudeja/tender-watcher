"""NIC GePNIC portals (etenders.kerala.gov.in, eprocure.gov.in, most state portals).

Flow: Tenders by Organisation -> each org's tender list -> each new
tender's detail page. Every link carries a session-bound `sp=` token, so details
are fetched right after the page that lists them, and a Stale Session page makes us
restart the session and re-walk that organisation.

The portal returns ALL of an organisation's tenders in a single response (paging is
client-side only), so there is normally no Next link; _find_next is a fallback in case
a portal version paginates server-side.

Parsers are pure functions over HTML so they can be tested against saved fixtures. The crawl
loop itself (refresh policy, retention, checkpoints, progress) lives in crawler/engine.py and is
shared with the other portal types; this module only provides the GePNIC adapter for it.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Dict, Iterator, List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from . import engine
from .engine import now as _now  # noqa: F401  (re-exported: tests and callers use gepnic._now)
from .http import PortalSession, StaleSession  # noqa: F401  (StaleSession re-exported)

log = logging.getLogger("tenderwatch.gepnic")

MAX_PAGES_PER_ORG = 3000
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
class GepnicPortal:
    """Adapter that lets crawler.engine drive a GePNIC portal (see the interface in engine.py)."""

    def __init__(self, sess: PortalSession, base: str):
        self.sess, self.base = sess, base
        self.org_list_url = f"{base}?page=FrontEndTendersByOrganisation&service=page"

    def list_orgs(self) -> List[Dict]:
        return parse_org_list(self.sess.get(self.org_list_url, label="orgs"), self.base)

    def walk_org(self, org: Dict) -> Iterator[Dict]:
        url, visited, pages = org["url"], set(), 0
        while url and url not in visited and pages < MAX_PAGES_PER_ORG:
            visited.add(url)
            pages += 1
            try:
                page = parse_tender_list(self.sess.get(url, label="list"), self.base)
            except StaleSession as e:
                raise engine.Restart(str(e)) from e
            for row in page["rows"]:
                row["detail_ref"] = row.pop("detail_url", None)
                yield row
            url = page["next_url"]

    def fetch_detail(self, org: Dict, row: Dict) -> Dict:
        try:
            html = self.sess.get(row["detail_ref"], label="detail")
        except StaleSession as e:
            raise engine.Restart(str(e)) from e
        if looks_like_captcha_wall(html):
            raise engine.DetailsBlocked("detail pages appear CAPTCHA-gated; skipping details for the rest of this run")
        fetched = parse_tender_detail(html)
        if not fetched.get("details"):
            raise engine.DetailFailed(f"detail {row['id']}: no caption/value pairs parsed (markup changed?)")
        return fetched

    def restart_org(self, org: Dict) -> Optional[Dict]:
        self.sess.restart()
        fresh = {o["name"]: o for o in self.list_orgs()}
        return fresh.get(org["name"])

    def requests_made(self) -> int:
        return getattr(self.sess, "requests_made", 0)


def crawl_source(src: Dict, store: Dict[str, Dict], limit_orgs: Optional[int] = None,
                 only_orgs: Optional[List[str]] = None, no_details: bool = False,
                 dump_dir: Optional[str] = None, checkpoint=None, progress=None,
                 force_refresh: bool = False, stop=None) -> Dict:
    """Crawl one GePNIC portal into `store` (id -> record). Returns a status dict.

    The policy (refresh rules, retention, checkpoints, progress, stop) is documented on
    crawler.engine.crawl; this only builds the session and the GePNIC adapter.
    """
    base = src["base_url"].rstrip("/")
    sess = PortalSession(base, delay_seconds=float(src.get("delay_seconds", 1.0)),
                         dump_dir=dump_dir, ignore_robots=bool(src.get("ignore_robots", False)),
                         extra_ca_certs=src.get("extra_ca_certs"))
    return engine.crawl(src, store, GepnicPortal(sess, base), limit_orgs=limit_orgs, only_orgs=only_orgs,
                        no_details=no_details, checkpoint=checkpoint, progress=progress,
                        force_refresh=force_refresh, stop=stop, detail_fields=DETAIL_FIELDS)
