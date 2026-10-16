"""GeM's CPPP tender lists (gem.gov.in/cppp and gem.gov.in/cppp_state).

GeM republishes the tenders that the central procurement portal (CPPP) and the state portals publish, as plain
server-rendered tables, newest published first, 10 per page (the central list has about 24,000 live tenders, mostly
Indian Railways; the state list about 5,000, nearly all of them tenders of the NIC state portals that are also crawled
here). Public pages: no login, no CAPTCHA, robots.txt allows them.

    GET /cppp            page 1                    "Showing 1 to 10 of 23,779 entries" + the table
    GET /cppp/<n>        page n (also /cppp_state/<n>)

Columns: bid closing date, opening date, e-published date, one "Title/Ref.No./Tender Id" cell, organisation,
corrigendum, (central only) GeM availability report id and download. There is no value, no EMD and no detail page, so
records are listing-only. The id is the last "/" part of the combined cell (an IREPS tender number, or a GePNIC tender id
such as 2026_PRD_5080689_1); the reference is the part before it and the title the rest (a reference or a title that
itself contains "/" splits imperfectly, which only affects the displayed split, not the id).

A whole pass is one request per page (about 2,400 for the central list), so it takes about 40 minutes at one request a
second; the pass is resumable after a throttling pause. Both sources are `prefer_others: true`: their tenders that other
sources also hold are left out of the site index (crawler/dedupe.py). The crawl loop is crawler/engine.py.
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
from datetime import datetime
from typing import Dict, Iterator, List, Optional, Tuple

from bs4 import BeautifulSoup

from . import engine
from .http import PortalSession

log = logging.getLogger("tenderwatch.gemcppp")

PAGE_SIZE = 10
MAX_SHORTFALL = 0.05            # tenders that close while we page shift the list a little (a few are skipped)
LOG_EVERY = 100                 # pages

CHANGE_KEYS = ("close", "open", "title", "ref", "corrigendum")
LISTING_FIELDS = ("ref", "title", "chain", "pub", "close", "open", "corrigendum", "gem_report")

# column heading (lower case) -> what it is
COLUMNS = {"bid submission closing date": "close", "tender opening date": "open", "e-published date": "pub",
           "title/ref.no./tender id": "cell", "organisation name": "org", "corrigendum": "corrigendum",
           "gem availability report id": "gem_report", "download": "download"}

_norm = lambda t: re.sub(r"\s+", " ", t or "").strip()  # noqa: E731


def parse_dt(text: Optional[str]) -> Optional[str]:
    """'06-November-2026 02:30:00 PM' -> '2026-11-06T14:30'; else None."""
    try:
        return datetime.strptime(_norm(text), "%d-%B-%Y %I:%M:%S %p").strftime("%Y-%m-%dT%H:%M")
    except ValueError:
        return None


def split_cell(cell: str) -> Tuple[str, str, str]:
    """'Title /Ref.No./TenderId' -> (title, ref, id): the id is the last part, the reference the one before it."""
    cell = _norm(cell)
    parts = [p.strip() for p in cell.rsplit("/", 2)]
    if len(parts) == 3:
        title, ref, tid = parts
    elif len(parts) == 2:
        title, ref, tid = "", parts[0], parts[1]
    else:
        title, ref, tid = cell, "", ""
    if not re.fullmatch(r"[A-Za-z0-9_\-.]+", tid or ""):            # no usable id: a stable one from the whole cell
        tid = "x" + hashlib.sha1(cell.encode("utf-8")).hexdigest()[:12]
    return title, ref, tid


def parse_page(html: str) -> Tuple[Optional[int], List[Dict]]:
    """One list page -> (the total number of entries the site says it has, rows)."""
    soup = BeautifulSoup(html, "lxml")
    text = _norm(re.sub(r"<[^>]+>", " ", html))
    m = re.search(r"Showing \d+ to \d+ of ([\d,]+) entries", text)
    total = int(m.group(1).replace(",", "")) if m else None
    table = soup.find("table", class_="table")
    rows: List[Dict] = []
    if table is None:
        return total, rows
    trs = table.find_all("tr")
    head = [COLUMNS.get(_norm(c.get_text(" ")).lower(), "") for c in trs[0].find_all(["th", "td"])] if trs else []
    if "cell" not in head or "org" not in head:
        return total, rows
    for tr in trs[1:]:
        cells = [_norm(c.get_text(" ")) for c in tr.find_all("td")]
        if len(cells) < len(head) - 1:
            continue
        d = dict(zip(head, cells))
        title, ref, tid = split_cell(d["cell"])
        org = d.get("org") or "Unspecified organisation"
        corr = d.get("corrigendum")
        rows.append({
            "id": tid, "ref": ref, "title": title or ref, "department": org, "chain": org,
            "pub": parse_dt(d.get("pub")), "open": parse_dt(d.get("open")), "close": parse_dt(d.get("close")),
            "corrigendum": None if not corr or corr.upper() == "N/A" else corr,
            "gem_report": d.get("gem_report") or None,
        })
    return total, rows


class GemCpppPortal:
    """Adapter that lets crawler.engine drive a GeM CPPP list (see the interface in engine.py). List only."""

    throttle_scope = "source"
    parallel_details = False

    def __init__(self, sess: PortalSession, base_url: str, max_pages: Optional[int] = None):
        self.sess, self.base, self.max_pages = sess, base_url.rstrip("/"), max_pages
        self._rows: Dict[str, Dict] = {}
        self._next, self._pages, self._total = 1, None, None
        self._resume = False                  # a pass that stopped on an error is continued, not restarted
        self._by_org: Dict[str, List[Dict]] = {}

    def _url(self, page: int) -> str:
        return self.base if page == 1 else f"{self.base}/{page}"

    def list_orgs(self) -> List[Dict]:
        if not self._resume:
            self._rows, self._next, self._pages, self._total = {}, 1, None, None
        try:
            while self._pages is None or self._next <= self._pages:
                total, rows = parse_page(self.sess.get(self._url(self._next), label="list"))
                if self._pages is None:
                    if not total:
                        raise RuntimeError("the list page has no entry count or table (markup changed?)")
                    self._total = total
                    self._pages = math.ceil(total / PAGE_SIZE)
                    if self.max_pages:
                        self._pages = min(self._pages, self.max_pages)
                    log.info("%d entries on %d pages (reading %d)", total, math.ceil(total / PAGE_SIZE), self._pages)
                if not rows:
                    log.warning("page %d has no rows: the list is shorter than announced", self._next)
                    break
                for r in rows:
                    self._rows.setdefault(r["id"], r)
                if self._next % LOG_EVERY == 0:
                    log.info("page %d of %d: %d tenders so far", self._next, self._pages, len(self._rows))
                self._next += 1
        except BaseException:
            self._resume = True
            raise
        self._resume = False
        expected = min(self._total, self._pages * PAGE_SIZE)
        if len(self._rows) < expected * (1 - MAX_SHORTFALL) - 5:
            raise RuntimeError(f"the paged list returned only {len(self._rows)} of {expected} tenders")
        self._by_org = {}
        for r in self._rows.values():
            self._by_org.setdefault(r["department"], []).append(r)
        return [{"name": name, "count": len(rs), "url": self.base} for name, rs in sorted(self._by_org.items())]

    def walk_org(self, org: Dict) -> Iterator[Dict]:
        for row in self._by_org.get(org["name"], []):
            yield dict(row, detail_ref=None)

    def fetch_detail(self, org: Dict, row: Dict) -> Dict:
        raise engine.DetailFailed("the GeM CPPP lists have no detail pages")

    def restart_org(self, org: Dict) -> Optional[Dict]:
        return org

    def requests_made(self) -> int:
        return getattr(self.sess, "requests_made", 0)


def crawl_source(src: Dict, store: Dict[str, Dict], limit_orgs: Optional[int] = None,
                 only_orgs: Optional[List[str]] = None, no_details: bool = False,
                 dump_dir: Optional[str] = None, checkpoint=None, progress=None,
                 force_refresh: bool = False, stop=None, **_other) -> Dict:
    """Crawl one GeM CPPP list (every organisation, every page) into `store`. Policy: see crawler.engine.crawl."""
    base = src["base_url"]
    sess = PortalSession(base, delay_seconds=float(src.get("delay_seconds", 1.0)), dump_dir=dump_dir,
                         ignore_robots=bool(src.get("ignore_robots", False)), extra_ca_certs=src.get("extra_ca_certs"))
    portal = GemCpppPortal(sess, base, max_pages=src.get("max_pages"))
    return engine.crawl(src, store, portal, limit_orgs=limit_orgs, only_orgs=only_orgs, no_details=True,
                        checkpoint=checkpoint, progress=progress, force_refresh=force_refresh, stop=stop,
                        change_keys=CHANGE_KEYS, listing_fields=LISTING_FIELDS, detail_fields=())
