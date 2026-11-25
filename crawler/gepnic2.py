"""NIC GePNIC portals on NIC's new front end ("GePNIC 2X Home UI", JSON API under /nggepnic_home_service).

From November 2026 several state GePNIC portals (Meghalaya, Nagaland, Manipur, Arunachal Pradesh, Andaman & Nicobar,
DNH & DD) replaced the old /nicgep/app?page=FrontEnd... pages with a single-page app. Its full tender lists
(Active Tenders, Tenders by Organisation) need a CAPTCHA. This crawler does not touch those. It reads the portal's
public organisation-wise page instead: the one departments link to from their own websites as
#/FrontEndLatestActiveTendersOrgwise?org=<organisation>, which needs no CAPTCHA, login or cookie.
The owner chose this route on 25-11-2026. Calls, all JSON POSTs, as the page makes them:

    POST <api>/activesearch/get-org-list                     {}
         -> {"dropdownList": [{"id", "stringValue": organisation name}]}   (organisations with live tenders)
    POST <api>/home/front-end-latest-active-tenders-orgwise  {"org": name, "limit": 50, "offset": N, "isCountRequest": bool}
         -> {"frontEndLatestActiveTendersList": [...], "tendersCount": total (count requests only)}
            (an empty body means no tenders; the server accepts only some page sizes: 10 and 50 work, 25/100 do not)
    POST <api>/home/tender-info-details                      {"workItemId": id}
         -> {"tenderDetails": {...}, "tenderOtherInfo": {...}}   one tender's detail

(<api> = <scheme>://<host>/nggepnic_home_service.) Tender IDs are the usual GePNIC ones (2026_MeECL_2092_1), so
records keep the ids and organisation names of the old `gepnic` crawler and carry on where it stopped.
The detail JSON also names the officers who signed the documents: that is not read or stored.
The crawl loop is crawler/engine.py.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Dict, Iterator, List, Optional
from urllib.parse import urlsplit

from . import engine
from .gepnic import parse_date, parse_money
from .http import PortalSession

log = logging.getLogger("tenderwatch.gepnic2")

API = "/nggepnic_home_service"
ORG_LIST = "/activesearch/get-org-list"
ORG_TENDERS = "/home/front-end-latest-active-tenders-orgwise"
DETAIL = "/home/tender-info-details"
PAGE = 50                       # the server rejects most sizes ("Your search criteria is not valid"); 50 is accepted
MAX_PAGES_PER_ORG = 400
JSON_HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}

# the same detail fields as crawler/gepnic.py, so records look alike whichever front end a portal had
DETAIL_FIELDS = ("category", "product_category", "value", "emd", "location", "pincode", "bid_end",
                 "contract_type", "form_of_contract", "tender_type", "period", "authority",
                 "authority_address", "description", "documents", "details")

_norm = lambda t: re.sub(r"\s+", " ", str(t) if t is not None else "").strip()  # noqa: E731


def api_root(base_url: str) -> str:
    """'https://meghalayatenders.gov.in/nicgep/app' -> 'https://meghalayatenders.gov.in/nggepnic_home_service'."""
    p = urlsplit(base_url)
    return f"{p.scheme}://{p.netloc}{API}"


def _load(text: str):
    return json.loads(text) if text and text.strip() else {}


def parse_org_list(text: str) -> List[Dict[str, str]]:
    """get-org-list -> [{"name": tidied name, "query": the name exactly as the portal sent it}].

    The org-wise call matches the name exactly, runs of spaces included (Andaman's
    "Chief Engineer<22 spaces>Andaman Public Works Department (APWD)" finds nothing once tidied), so the
    raw string is what gets sent; the tidied one is the organisation's name in our records, as before.
    Blank and duplicate names are dropped and the portal's order is kept."""
    orgs: List[Dict[str, str]] = []
    seen = set()
    for o in (_load(text) or {}).get("dropdownList") or []:
        raw = o.get("stringValue")
        name = _norm(raw)
        if name and name not in seen:
            seen.add(name)
            orgs.append({"name": name, "query": str(raw)})
    return orgs


def parse_org_tenders(text: str) -> Dict:
    """One page of an organisation's tenders -> {"rows": [...], "total": int | None}."""
    j = _load(text) or {}
    rows = []
    for t in j.get("frontEndLatestActiveTendersList") or []:
        tid = _norm(t.get("tenderId"))
        if not tid:
            continue
        rows.append({
            "id": tid, "ref": _norm(t.get("tenderRefNo")), "title": _norm(t.get("tenderTitle")),
            "chain": _norm(t.get("orgDept")),
            "pub": parse_date(t.get("publishDate")), "close": parse_date(t.get("closingDate")),
            "open": parse_date(t.get("openingDate")),
            "detail_ref": t.get("workItemId") or None,
        })
    total = j.get("tendersCount")
    return {"rows": rows, "total": int(total) if isinstance(total, (int, float)) or str(total or "").isdigit() else None}


def parse_detail(text: str) -> Dict:
    """tender-info-details -> typed fields plus `details` (label -> text). {} if it is not a tender."""
    j = _load(text)
    if not isinstance(j, dict) or not isinstance(j.get("tenderDetails"), dict) or not j["tenderDetails"].get("tenderId"):
        return {}
    td, other = j["tenderDetails"], j.get("tenderOtherInfo") or {}
    basic = other.get("tenderBasicDetails") or {}
    work = other.get("tenderWorkItemDetails") or {}
    dates = other.get("tenderCriticalDates") or {}
    fee = other.get("tenderFeeDetails") or {}
    emd = other.get("emdfeeDetails") or {}
    auth = other.get("tenderInvitingAuthority") or {}
    details: Dict[str, str] = {}

    def put(label: str, value) -> None:
        v = _norm(value)
        if v and v.lower() not in ("none", "null", "na", "nil"):
            details[label] = v

    put("Tender ID", td.get("tenderId"))
    put("Tender Reference Number", td.get("tenderRefNo"))
    put("Organisation Chain", td.get("orgChain"))
    put("Tender Type", basic.get("tenderType"))
    put("Tender Category", basic.get("tenderCategory"))
    put("Form of Contract", basic.get("formOfContract") or td.get("formOfContract"))
    put("No. of Covers", basic.get("noOfCovers"))
    put("Payment Mode", basic.get("paymentMode"))
    put("Tender Status", td.get("tenderStatus"))
    put("Tender Stage", td.get("tenderStage"))
    put("Tender Value in ₹", work.get("tenderValue"))
    put("Product Category", work.get("productCategory"))
    put("Sub category", work.get("productSubCategory"))
    put("Contract Type", work.get("contractType"))
    put("Bid Validity(Days)", work.get("bidValidity"))
    put("Period Of Work(Days)", work.get("periodOfWork"))
    put("Location", work.get("location"))
    put("Pincode", work.get("pincode"))
    put("Pre Bid Meeting Place", work.get("prebidMeetingPlace"))
    put("Bid Opening Place", work.get("bidOpeningPlace"))
    put("Tender Class", work.get("tenderClass"))
    put("Tender Fee in ₹", fee.get("tenderFee"))
    put("Processing Fee in ₹", fee.get("processingFee"))
    put("Fee Payable To", fee.get("feePayableTo"))
    put("Fee Payable At", fee.get("feePayableAt"))
    put("EMD Amount in ₹", emd.get("emdAmount"))
    put("EMD Fee Type", emd.get("emdFeeType"))
    put("EMD Percentage", emd.get("emdPercentage"))
    put("EMD Payable To", emd.get("emdPayableTo"))
    put("EMD Payable At", emd.get("emdPayableAt"))
    for label, key in (("Published Date", "publishDate"), ("Document Download / Sale Start Date", "docSaleStartDate"),
                       ("Document Download / Sale End Date", "docSaleEndDate"),
                       ("Clarification Start Date", "clarificationStartDate"), ("Clarification End Date", "clarificationEndDate"),
                       ("Bid Submission Start Date", "bidSubStartDate"), ("Bid Submission End Date", "bidSubEndDate"),
                       ("Bid Opening Date", "bidOpeningDate"), ("Financial Bid Opening Date", "finBidOpeningDate"),
                       ("Pre Bid Meeting Date", "preBidMeetingDate")):
        put(label, dates.get(key))
    put("Work Description", work.get("workDescription"))
    put("Pre Qualification Details", work.get("preQualDetails"))
    put("Remarks", work.get("remarks"))

    value = parse_money(work.get("tenderValue")) if str(work.get("showTenderValue", "true")).lower() != "false" else None
    out: Dict = {
        "category": _norm(basic.get("tenderCategory")),
        "product_category": _norm(work.get("productCategory")),
        "value": value or None,                                  # 0 means "not stated"
        "emd": parse_money(emd.get("emdAmount")),
        "location": _norm(work.get("location")),
        "pincode": _norm(work.get("pincode")),
        "bid_end": parse_date(dates.get("bidSubEndDate")),
        "contract_type": _norm(work.get("contractType")),
        "form_of_contract": _norm(basic.get("formOfContract") or td.get("formOfContract")),
        "tender_type": _norm(basic.get("tenderType")),
        "period": _norm(work.get("periodOfWork")),
        "authority": _norm(auth.get("invitingOfficer")),
        "authority_address": _norm(auth.get("invitingOfficerAddress")),
        "description": _norm(work.get("workDescription")),
    }
    docs = sorted({_norm(d.get("documentName")) for key in ("nitDocuments", "workItemDocuments", "preBidMeetingDocuments")
                   for d in other.get(key) or [] if isinstance(d, dict) and _norm(d.get("documentName"))})
    if docs:
        out["documents"] = docs
    out["details"] = details
    return {k: v for k, v in out.items() if v not in (None, "")}


# ---------------------------------------------------------------- crawl
class Gepnic2Portal:
    """Adapter that lets crawler.engine drive a new-front-end GePNIC portal (see the interface in engine.py)."""

    parallel_details = True       # independent POSTs, no session state; the shared session still caps the request rate

    def __init__(self, sess: PortalSession, base_url: str):
        self.sess, self.base = sess, base_url
        self.api = api_root(base_url)

    def _post(self, path: str, body: Dict, label: str) -> str:
        return self.sess.post(self.api + path, data=json.dumps(body), headers=JSON_HEADERS, label=label)

    def list_orgs(self) -> List[Dict]:
        """Every organisation the portal lists, with its tender count. The list only names organisations
        with live tenders, so one whose tender list comes back empty is kept with count 0 and flagged:
        walk_org then fails it, which keeps the run from closing that organisation's tenders on a bad answer."""
        orgs = []
        for o in parse_org_list(self._post(ORG_LIST, {}, "orgs")):
            page = parse_org_tenders(self._post(ORG_TENDERS, self._page_body(o["query"], 0, True), "list"))
            count = page["total"] if page["total"] is not None else len(page["rows"])
            orgs.append(dict(o, count=count, first_page=page["rows"], empty=not page["rows"]))
        return orgs

    @staticmethod
    def _page_body(org: str, offset: int, count: bool) -> Dict:
        return {"org": org, "limit": PAGE, "offset": offset, "isCountRequest": count}

    def walk_org(self, org: Dict) -> Iterator[Dict]:
        query = org.get("query", org["name"])
        if org.get("empty"):
            raise RuntimeError("listed by the portal, but its tender list came back empty "
                               "(name mismatch or a portal fault); its stored tenders are kept")
        rows = org.get("first_page")
        if rows is None:
            rows = parse_org_tenders(self._post(ORG_TENDERS, self._page_body(query, 0, False), "list"))["rows"]
        seen, offset, pages = set(), 0, 1
        while True:
            fresh = [r for r in rows if r["id"] not in seen]
            for r in fresh:
                seen.add(r["id"])
                yield r
            offset += PAGE
            if len(rows) < PAGE or not fresh or offset >= org["count"] or pages >= MAX_PAGES_PER_ORG:
                break
            rows = parse_org_tenders(self._post(ORG_TENDERS, self._page_body(query, offset, False), "list"))["rows"]
            pages += 1

    def fetch_detail(self, org: Dict, row: Dict) -> Dict:
        if not row.get("detail_ref"):
            raise engine.DetailFailed(f"detail {row['id']}: the row has no workItemId")
        text = self._post(DETAIL, {"workItemId": row["detail_ref"]}, "detail")
        try:
            fetched = parse_detail(text)
        except ValueError as e:
            raise engine.DetailFailed(f"detail {row['id']}: not JSON ({e})") from e
        if not fetched.get("details"):
            raise engine.DetailFailed(f"detail {row['id']}: no tender in the response (API changed?)")
        return fetched

    def restart_org(self, org: Dict) -> Optional[Dict]:
        org.pop("first_page", None)
        return org        # no per-organisation session state; engine.Restart is never raised

    def requests_made(self) -> int:
        return getattr(self.sess, "requests_made", 0)


def crawl_source(src: Dict, store: Dict[str, Dict], limit_orgs: Optional[int] = None,
                 only_orgs: Optional[List[str]] = None, no_details: bool = False,
                 dump_dir: Optional[str] = None, checkpoint=None, progress=None,
                 force_refresh: bool = False, stop=None, **_other) -> Dict:
    """Crawl one new-front-end GePNIC portal into `store`. Policy: see crawler.engine.crawl."""
    base = src["base_url"].rstrip("/")
    sess = PortalSession(base, delay_seconds=float(src.get("delay_seconds", 1.0)), dump_dir=dump_dir,
                         ignore_robots=bool(src.get("ignore_robots", False)), extra_ca_certs=src.get("extra_ca_certs"))
    return engine.crawl(src, store, Gepnic2Portal(sess, base), limit_orgs=limit_orgs, only_orgs=only_orgs,
                        no_details=no_details, checkpoint=checkpoint, progress=progress,
                        force_refresh=force_refresh, stop=stop, detail_fields=DETAIL_FIELDS)
