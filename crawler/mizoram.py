"""The Mizoram e-Tender notice board, tender.mizoram.gov.in (a Laravel site run by the state, not NIC's GePNIC).

It is a paged list of notices - 15 per page, ~50 pages, newest first, expired notices included - not a
tender system: each card has a reference number, the department, a notice type, a description, its
attachments (PDF names) and three dates (issue, opening, last date). There is no detail page and no
value or EMD. Only notices whose last date is still ahead are live; the rest are history and are
never yielded, so (like every other source) the store holds live tenders plus those that just left.

    GET /home?page=N   (1.. until a page has no cards)

A department plays the role of a GePNIC organisation. The notice id is the portal's own (the
`download<id>` modal of the card). robots.txt allows everything. The crawl loop is crawler/engine.py.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterator, List, Optional
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from . import engine
from .http import PortalSession

log = logging.getLogger("tenderwatch.mizoram")

IST = timezone(timedelta(hours=5, minutes=30))
MAX_PAGES = 200                  # runaway guard (the board has ~50)

CHANGE_KEYS = ("close", "open", "title", "ref")
LISTING_FIELDS = ("ref", "title", "chain", "pub", "close", "open", "tender_type", "documents")
DETAIL_FIELDS: tuple = ()

_norm = lambda t: re.sub(r"\s+", " ", (t or "").replace("\xa0", " ")).strip()  # noqa: E731


def _now() -> datetime:
    return datetime.now(IST)


def parse_dt(text: Optional[str]) -> Optional[str]:
    """'30-10-2026 01:00 pm' -> '2026-10-30T13:00'; a bare '28-09-2026' -> '2026-09-28'; else None."""
    m = re.search(r"(\d{1,2})-(\d{1,2})-(\d{4})(?:\s+(\d{1,2}):(\d{2})\s*([ap]m)?)?", text or "", re.I)
    if not m:
        return None
    d, mo, y, hh, mm, ap = m.groups()
    try:
        datetime(int(y), int(mo), int(d))
        if hh is None:
            return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
        hour = int(hh) % 12 + (12 if (ap or "").lower() == "pm" else 0) if ap else int(hh)
        datetime(int(y), int(mo), int(d), hour, int(mm))
    except ValueError:
        return None
    return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}T{hour:02d}:{mm}"


def parse_cards(html: str) -> List[Dict]:
    """One /home page -> notice rows, in page order. Cards are the innermost div.container holding an h3
    and an 'Issue date' (the page wraps all of them in one more container)."""
    soup = BeautifulSoup(html, "lxml")
    cand = [c for c in soup.find_all("div", class_="container") if c.find("h3") and "Issue date" in c.get_text()]
    rows = []
    for card in cand:
        if any(o is not card and o in card.find_all("div", class_="container") for o in cand):
            continue                                        # the page wrapper, not a card
        head = card.find("h3")
        first = card.find("div", recursive=False)
        ref = _norm(first.get_text(" ")) if first is not None else ""
        dept = _norm(card.find("i").get_text(" ")) if card.find("i") else ""
        kind = _norm(head.get_text(" "))
        desc_div = head.parent.find_next_sibling("div") if head.parent is not None else None
        # the description is the plain div after the type; never the attachments or the dates block
        plain = desc_div is not None and not desc_div.find("a") and "container" not in (desc_div.get("class") or [])             and "Issue date" not in desc_div.get_text()
        desc = _norm(desc_div.get_text(" ")) if plain else ""
        modal = card.find("div", class_="modal")
        mid = re.search(r"download(\d+)", (modal.get("id") or "") if modal else "")
        text = _norm(card.get_text(" "))
        dates = {k: (re.search(k + r":\s*(\d{1,2}-\d{1,2}-\d{4}(?:\s+\d{1,2}:\d{2}\s*[ap]m)?)", text, re.I) or [None, None])[1]
                 for k in ("Issue date", "Opening Date", "Last Date")}
        docs = sorted({_norm(a.get_text(" ")) for a in card.find_all("a", href=re.compile("downloadcount")) if _norm(a.get_text(" "))})
        title = desc or kind
        rid = mid.group(1) if mid else "x" + hashlib.sha1(f"{ref}|{dept}|{title}".encode("utf-8")).hexdigest()[:10]
        rows.append({
            "id": rid, "ref": ref, "title": title, "chain": dept or "Unspecified department",
            "pub": parse_dt(dates["Issue date"]), "open": parse_dt(dates["Opening Date"]), "close": parse_dt(dates["Last Date"]),
            "tender_type": kind if desc and len(kind) <= 60 else None,
            "documents": docs or None,
        })
    return rows


class MizoramPortal:
    """Adapter that lets crawler.engine drive the notice board (see the interface in engine.py)."""

    throttle_scope = "source"
    may_be_empty = True          # a board with no live notice is a valid answer, not a parse failure

    def __init__(self, sess: PortalSession, base_url: str):
        parts = urlsplit(base_url)
        self.sess = sess
        self.home = f"{parts.scheme}://{parts.netloc}/home"
        self._by_org: Dict[str, List[Dict]] = {}

    def list_orgs(self) -> List[Dict]:
        now = _now().strftime("%Y-%m-%dT%H:%M")
        seen, live, pages = set(), [], 0
        for page in range(1, MAX_PAGES + 1):
            rows = parse_cards(self.sess.get(self.home if page == 1 else f"{self.home}?page={page}", label="list"))
            if not rows:
                if page == 1:
                    raise RuntimeError("no notice cards on the first page (markup changed?)")
                break
            pages += 1
            for row in rows:
                if row["id"] in seen:
                    continue                                   # a notice that shifted to the next page while we read
                seen.add(row["id"])
                if not row["close"] or row["close"] >= now:    # no parsable last date: keep it rather than hide it
                    live.append(row)
        log.info("%d notices on %d pages, %d still open", len(seen), pages, len(live))
        self._by_org = {}
        for row in live:
            self._by_org.setdefault(row["chain"], []).append(row)
        return [{"name": name, "count": len(rs), "url": self.home} for name, rs in sorted(self._by_org.items())]

    def walk_org(self, org: Dict) -> Iterator[Dict]:
        for row in self._by_org.get(org["name"], []):
            yield dict(row, detail_ref=None)                   # nothing more to fetch: the card is the whole record

    def fetch_detail(self, org: Dict, row: Dict) -> Dict:
        raise engine.DetailFailed("the notice board has no detail pages")

    def restart_org(self, org: Dict) -> Optional[Dict]:
        return org

    def requests_made(self) -> int:
        return getattr(self.sess, "requests_made", 0)


def crawl_source(src: Dict, store: Dict[str, Dict], limit_orgs: Optional[int] = None,
                 only_orgs: Optional[List[str]] = None, no_details: bool = False,
                 dump_dir: Optional[str] = None, checkpoint=None, progress=None,
                 force_refresh: bool = False, stop=None, **_other) -> Dict:
    """Crawl the Mizoram notice board into `store`. Policy: see crawler.engine.crawl."""
    sess = PortalSession(src["base_url"], delay_seconds=float(src.get("delay_seconds", 1.0)),
                         dump_dir=dump_dir, ignore_robots=bool(src.get("ignore_robots", False)),
                         extra_ca_certs=src.get("extra_ca_certs"))
    return engine.crawl(src, store, MizoramPortal(sess, src["base_url"]), limit_orgs=limit_orgs, only_orgs=only_orgs,
                        no_details=True, checkpoint=checkpoint, progress=progress,
                        force_refresh=force_refresh, stop=stop, change_keys=CHANGE_KEYS,
                        listing_fields=LISTING_FIELDS, detail_fields=DETAIL_FIELDS)
