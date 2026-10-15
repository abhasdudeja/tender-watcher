"""The Mizoram e-Tender notice board, tender.mizoram.gov.in (a Laravel site run by the state, not NIC's GePNIC).

It is a notice board, not a tender system: each notice has a number, the office, a type ("TENDER NOTICE"), a
description, PDF attachments and three dates (issue, opening, last date of submission). There is no detail page and
no value or EMD. The site is a single-page app (the page itself is an empty shell); its data comes from the site's
own public JSON API, which is what is read here:

    GET /api/public/tenders?status=open&order=latest&page=N      {"tenders": {"data": [...], "total", "last_page", ...}}

15 notices per page, only the open ones. A board with no open notice answers `total: 0`: that is a valid answer
(`may_be_empty`), not a failure; whatever was open before is then kept and marked closed like any tender that leaves
a portal. (Until October 2026 the board served the notices as server-rendered HTML cards; the page now carries none.)

A department (the notice's office) plays the role of a GePNIC organisation. The notice id is the API's own. The crawl
loop is crawler/engine.py.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlsplit

from . import engine
from .http import PortalSession

log = logging.getLogger("tenderwatch.mizoram")

IST = timezone(timedelta(hours=5, minutes=30))
MAX_PAGES = 200                  # runaway guard (15 notices a page)

CHANGE_KEYS = ("close", "open", "title", "ref")
LISTING_FIELDS = ("ref", "title", "chain", "pub", "close", "open", "tender_type", "documents")
DETAIL_FIELDS: tuple = ()

_norm = lambda t: re.sub(r"\s+", " ", str(t if t is not None else "").replace("\xa0", " ")).strip()  # noqa: E731


def _now() -> datetime:
    return datetime.now(IST)


def parse_dt(text: Optional[str]) -> Optional[str]:
    """'2026-11-11 12:00:00' -> '2026-11-11T12:00'; a midnight value ('2026-10-12 00:00:00') or a bare date -> '2026-10-12'."""
    m = re.match(r"\s*(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?", text or "")
    if not m:
        return None
    y, mo, d, hh, mm = m.groups()
    try:
        datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0))
    except ValueError:
        return None
    if hh is None or (hh, mm) == ("00", "00"):
        return f"{y}-{mo}-{d}"
    return f"{y}-{mo}-{d}T{hh}:{mm}"


def parse_tenders(text: str) -> Tuple[int, List[Dict]]:
    """One API page -> (last page number, notice rows). ValueError if the answer is not the notices list."""
    try:
        j = json.loads(text)
    except ValueError as e:
        raise ValueError(f"not JSON ({e})") from e
    tenders = j.get("tenders") if isinstance(j, dict) else None
    if not isinstance(tenders, dict) or not isinstance(tenders.get("data"), list):
        raise ValueError("no tenders list in the answer")
    rows = []
    for t in tenders["data"]:
        if not isinstance(t, dict) or t.get("id") is None or t.get("is_open") is False:
            continue
        office = (t.get("office") or {}).get("name")
        subject, detail = _norm(t.get("subject")), _norm(t.get("detail"))
        docs = sorted({_norm(f"{m.get('ori_filename') or ''}.{m.get('file_ext')}" if m.get("file_ext") else m.get("ori_filename"))
                       for m in t.get("media") or [] if m.get("ori_filename")})
        rows.append({
            "id": str(t["id"]), "ref": _norm(t.get("tender_number")), "title": detail or subject,
            "chain": _norm(office) or "Unspecified department",
            "pub": parse_dt(t.get("issue_date")), "open": parse_dt(t.get("opening_date")),
            "close": parse_dt(t.get("last_date_of_submission")),
            "tender_type": subject if detail and 0 < len(subject) <= 60 else None,
            "documents": docs or None,
        })
    return int(tenders.get("last_page") or 1), rows


class MizoramPortal:
    """Adapter that lets crawler.engine drive the notice board (see the interface in engine.py)."""

    throttle_scope = "source"
    may_be_empty = True          # a board with no open notice is a valid answer, not a failure

    def __init__(self, sess: PortalSession, base_url: str):
        parts = urlsplit(base_url)
        self.sess = sess
        self.api = f"{parts.scheme}://{parts.netloc}/api/public"
        self.home = f"{parts.scheme}://{parts.netloc}/home"
        self._by_org: Dict[str, List[Dict]] = {}

    def list_orgs(self) -> List[Dict]:
        now = _now().strftime("%Y-%m-%dT%H:%M")
        seen, live, pages, last = set(), [], 0, 1
        page = 1
        while page <= min(last, MAX_PAGES):
            text = self.sess.get(f"{self.api}/tenders?status=open&order=latest&page={page}", label="list")
            try:
                last, rows = parse_tenders(text)
            except ValueError as e:
                raise RuntimeError(f"the notices API gave an unexpected answer: {e} (site changed?)") from e
            pages += 1
            for row in rows:
                if row["id"] in seen:
                    continue                                   # a notice that shifted to the next page while we read
                seen.add(row["id"])
                if not row["close"] or row["close"] >= now[:len(row["close"])]:   # no parsable last date: keep it rather than hide it
                    live.append(row)
            page += 1
        log.info("%d open notices on %d page(s), %d still open", len(seen), pages, len(live))
        self._by_org = {}
        for row in live:
            self._by_org.setdefault(row["chain"], []).append(row)
        return [{"name": name, "count": len(rs), "url": self.home} for name, rs in sorted(self._by_org.items())]

    def walk_org(self, org: Dict) -> Iterator[Dict]:
        for row in self._by_org.get(org["name"], []):
            yield dict(row, detail_ref=None)                   # nothing more to fetch: the notice is the whole record

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
