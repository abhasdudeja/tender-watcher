"""Karnataka Public Procurement Portal (KPPP, kppp.karnataka.gov.in), through the portal's own JSON API.

The Centre for e-Governance, Karnataka and KPPP asked for this API to be used (the page's tender search is a
single-page app that calls it). It needs no login and no cookie. Used here as the page uses it:

    POST <api>/search-eproc-tenders?page=N&size=100&order-by-tender-publish=true            goods
    POST <api>/works/search-eproc-tenders?...                                               works
    POST <api>/services/search-eproc-tenders?...                                            services
         body: {"category", "status": "PUBLISHED", "tenderType": OPEN | RESERVED | RESTRICTED, ...null filters}
         -> a JSON list of rows; the total number of matches is in the X-Total-Count response header
    GET  <api>/<nitId>/goods-tender-full-view   (works-tender-full-view, service-tender-full-view)
         -> one tender's detail: estimated value (tenderSchedule.ecv), EMD and fee, dates, document names

(<api> = /supplier-registration-service/v1/api/portal-service.) A department plays the role of a GePNIC
*organisation*; its office is the second half of `chain`. The row id used here is "G|W|S" + the internal nitId
(unique per category); the tender number shown on the site is kept as `ref`.

Records use the same keys as the other crawlers. The detail JSON also holds the officer's name, office and mobile
numbers and the publishing user: none of that is read or stored. The crawl loop is crawler/engine.py.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Dict, Iterator, List, Optional

from . import engine
from .http import PortalSession
from .nprocure import parse_dt

log = logging.getLogger("tenderwatch.kppp")

API = "/supplier-registration-service/v1/api/portal-service"
CATEGORIES = {                     # category -> (search path, detail path)
    "GOODS": ("/search-eproc-tenders", "/{id}/goods-tender-full-view"),
    "WORKS": ("/works/search-eproc-tenders", "/{id}/works-tender-full-view"),
    "SERVICES": ("/services/search-eproc-tenders", "/{id}/service-tender-full-view"),
}
LETTER = {"GOODS": "G", "WORKS": "W", "SERVICES": "S"}
TENDER_TYPES = ("OPEN", "RESERVED", "RESTRICTED")
PAGE = 100
MAX_SHORTFALL = 0.05            # tenders that close or publish while we page shift the list a little
MAX_DOC = 200                   # some "document names" are whole paragraphs of eligibility text
JSON_HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}

CHANGE_KEYS = ("close", "title", "ref", "pub", "value")
LISTING_FIELDS = ("ref", "title", "chain", "pub", "close", "category", "tender_type", "value")
DETAIL_FIELDS = ("value", "emd", "documents", "details")

_norm = lambda t: re.sub(r"\s+", " ", str(t) if t is not None else "").strip()  # noqa: E731


def _amount(v) -> Optional[float]:
    try:
        x = float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return x if x > 0 else None


def parse_search(text: str) -> List[Dict]:
    """One search response -> listing rows (id is category letter + nitId)."""
    rows = []
    for t in json.loads(text):
        cat, nit = t.get("category"), t.get("nitId")
        if cat not in LETTER or nit is None:
            continue
        dept, office = _norm(t.get("deptName")), _norm(t.get("locationName"))
        rows.append({
            "id": f"{LETTER[cat]}{nit}", "ref": _norm(t.get("tenderNumber")), "title": _norm(t.get("title")),
            "department": dept or "Unknown department", "chain": f"{dept}||{office}" if dept and office else (dept or office),
            "pub": parse_dt(t.get("publishedDate")), "close": parse_dt(t.get("tenderClosureDate")),
            "category": _norm(t.get("categoryText")) or cat.capitalize(),
            "tender_type": _norm(t.get("workCategoryName") or t.get("invitingStrategyText")) or None,
            "value": _amount(t.get("ecv")),
            "detail_ref": f"{cat}:{nit}",
        })
    return rows


def parse_full_view(text: str) -> Dict:
    """A full-view JSON -> value, emd, documents and `details`. {} if it is not a tender."""
    j = json.loads(text)
    if not isinstance(j, dict) or not isinstance(j.get("tenderSchedule"), dict):
        return {}
    sched, notice = j["tenderSchedule"], j.get("noticeInvitingTenderDTO") or {}
    details: Dict[str, str] = {}

    def put(label: str, value) -> None:
        v = _norm(value)
        if v and v.lower() not in ("none", "null"):
            details[label] = v

    put("Tender Fee (INR)", _amount(notice.get("tenderFee")) and str(_amount(notice.get("tenderFee"))).rstrip("0").rstrip("."))
    put("Evaluation Type", notice.get("evaluationTypeText"))
    put("Bid Type", notice.get("bidValueTypeText"))
    put("Bid Validity (days)", notice.get("bidValidityPeriod"))
    put("Number of Calls", notice.get("noOfCalls"))
    put("Procuring Entity", sched.get("procEntityTypeText"))
    put("Inviting Strategy", notice.get("invitingStrategyText"))
    for label, key in (("Query Closing", "tenderQueryClose"), ("Bid Receipt Closing", "tenderReceiptClose"),
                       ("Technical Bid Opening", "technicalBidOpen"), ("Pre-Bid Meeting", "preBidMeetingDate")):
        put(label, parse_dt(notice.get(key)))
    out: Dict = {"value": _amount(sched.get("ecv")), "emd": _amount(notice.get("emd"))}
    docs = sorted({_norm(d.get("documentName"))[:MAX_DOC] for d in j.get("tenderCriterionDocumentList") or [] if _norm(d.get("documentName"))})
    if docs:
        out["documents"] = docs
    out["details"] = details
    return {k: v for k, v in out.items() if v not in (None, "", {})} or {"details": details}


# ---------------------------------------------------------------- crawl
class KpppPortal:
    """Adapter that lets crawler.engine drive KPPP (see the interface in engine.py)."""

    throttle_scope = "source"
    parallel_details = True       # independent GETs, no session; the shared session still caps the request rate

    def __init__(self, sess: PortalSession, base_url: str):
        self.sess, self.base = sess, base_url.rstrip("/")
        self.api = self.base + API
        self._by_org: Dict[str, List[Dict]] = {}

    def _search(self, category: str, tender_type: str, page: int) -> List[Dict]:
        body = {"tenderNumber": None, "category": category, "status": "PUBLISHED", "deptId": None, "publishedFromDate": None,
                "publishedToDate": None, "tenderType": tender_type, "title": None, "ecv": None, "location": None,
                "tenderClosureFromDate": None, "tenderClosureToDate": None}
        if category == "WORKS":
            body["workCategoryId"] = None
        url = f"{self.api}{CATEGORIES[category][0]}?page={page}&size={PAGE}&order-by-tender-publish=true"
        return parse_search(self.sess.post(url, data=json.dumps(body), headers=JSON_HEADERS, label="list"))

    def list_orgs(self) -> List[Dict]:
        rows: Dict[str, Dict] = {}
        expected = 0
        for category in CATEGORIES:
            for tender_type in TENDER_TYPES:
                page, got_here, total = 0, 0, None
                while total is None or got_here < total:
                    got = self._search(category, tender_type, page)
                    if total is None:
                        total = int(self.sess.last_headers.get("x-total-count") or 0)
                        expected += total
                    if not got:
                        break
                    for r in got:
                        rows.setdefault(r["id"], r)
                    got_here += len(got)
                    page += 1
        if expected and len(rows) < expected * (1 - MAX_SHORTFALL) - 5:
            raise RuntimeError(f"the paged list returned only {len(rows)} of {expected} tenders")
        self._by_org = {}
        for r in rows.values():
            self._by_org.setdefault(r["department"], []).append(r)
        return [{"name": name, "count": len(rs), "url": self.base} for name, rs in sorted(self._by_org.items())]

    def walk_org(self, org: Dict) -> Iterator[Dict]:
        yield from self._by_org.get(org["name"], [])

    def fetch_detail(self, org: Dict, row: Dict) -> Dict:
        category, nit = (row.get("detail_ref") or ":").split(":")
        if category not in CATEGORIES or not nit:
            raise engine.DetailFailed(f"detail {row['id']}: the row has no detail reference")
        text = self.sess.get(self.api + CATEGORIES[category][1].format(id=nit), label="detail")
        try:
            fetched = parse_full_view(text)
        except ValueError as e:
            raise engine.DetailFailed(f"detail {row['id']}: not JSON ({e})") from e
        if not fetched:
            raise engine.DetailFailed(f"detail {row['id']}: no tender in the response (API changed?)")
        return fetched

    def restart_org(self, org: Dict) -> Optional[Dict]:
        return org        # no per-organisation session state; engine.Restart is never raised

    def requests_made(self) -> int:
        return getattr(self.sess, "requests_made", 0)


def crawl_source(src: Dict, store: Dict[str, Dict], limit_orgs: Optional[int] = None,
                 only_orgs: Optional[List[str]] = None, no_details: bool = False,
                 dump_dir: Optional[str] = None, checkpoint=None, progress=None,
                 force_refresh: bool = False, stop=None, **_other) -> Dict:
    """Crawl KPPP (all departments, goods + works + services) into `store`. Policy: see crawler.engine.crawl."""
    base = src["base_url"]
    sess = PortalSession(base, delay_seconds=float(src.get("delay_seconds", 1.0)), dump_dir=dump_dir,
                         ignore_robots=bool(src.get("ignore_robots", False)), extra_ca_certs=src.get("extra_ca_certs"))
    return engine.crawl(src, store, KpppPortal(sess, base), limit_orgs=limit_orgs, only_orgs=only_orgs,
                        no_details=no_details, checkpoint=checkpoint, progress=progress,
                        force_refresh=force_refresh, stop=stop, change_keys=CHANGE_KEYS,
                        listing_fields=LISTING_FIELDS, detail_fields=DETAIL_FIELDS)
