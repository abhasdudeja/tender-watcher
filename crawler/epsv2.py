"""EPSV2 portals (Beltron / mjunction "eProc 2.0"): the Bihar state portal, eproc2.bihar.gov.in.

Unlike GePNIC (server-rendered HTML) the open-area tender list is an AngularJS page whose data comes
from the portal's own JSON API, called by the page's JavaScript with the anonymous guest token that is
embedded in the page itself (hidden input #Authorization, a ~15 minute JWT; no login). Used here exactly
as the page uses it:

    GET  <base>/EPSV2Web/openarea/tenderListingPage.action   -> token + the portal's top org id
    POST /rest/organization/loadAllDeptWithParent?orgId=<org> -> department id -> name
    POST /rest/master/showRfqCategoryRfqTypeProcCatBidPartList -> tender type / procurement category names
    POST /rest/openarea/getTenderList  {"orgId": <org>}       -> EVERY live tender in one response
    POST /rest/tender/previewTenderByTenderId?tenderId=<id>   -> one tender's detail (value, fees, dates...)

A department plays the role of a GePNIC *organisation*: the one list call is made in list_orgs and
walk_org serves each department's rows from it. The tender id shown on the site (and used here) is
`currentOrgTenderId`; the detail call wants the internal `currenttenderid`, kept as `detail_ref`.
The server omits its certificate's intermediate: set `extra_ca_certs: certs/sectigo-ov-r36.pem`.

Records use the same keys as the other crawlers. Officers' names, contact details and the digital
certificates in the detail JSON are never stored. The crawl loop is crawler/engine.py.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterator, List, Optional
from urllib.parse import urlsplit

from . import engine
from .http import HostNotFound, PortalSession, RequestException, Throttled

log = logging.getLogger("tenderwatch.epsv2")

CONTEXT = "/EPSV2Web"
IST = timezone(timedelta(hours=5, minutes=30))
TOKEN_MIN_LEFT = 150.0          # fetch a new guest token when less than this many seconds of it are left
JSON_HEADERS = {"Content-Type": "application/json;charset=utf-8", "Auth-Token": "X-Requested-With"}

CHANGE_KEYS = ("close", "open", "title", "ref")
LISTING_FIELDS = ("ref", "title", "chain", "pub", "close", "open", "category", "tender_type")
DETAIL_FIELDS = ("value", "emd", "authority", "documents", "details")

_norm = lambda t: re.sub(r"\s+", " ", t or "").strip()  # noqa: E731


# ---------------------------------------------------------------- small parsers
def token_from_page(html: str) -> Optional[str]:
    m = re.search(r'id="Authorization"[^>]*?value="([^"]+)"', html) or re.search(r'name="Authorization"[^>]*?value="([^"]+)"', html)
    return m.group(1) if m else None


def org_id_from_page(html: str) -> Optional[int]:
    m = re.search(r"var\s+orgId\s*=\s*'?(\d+)'?", html)
    return int(m.group(1)) if m else None


def token_expiry(token: str) -> Optional[float]:
    """Unix time at which the JWT in "Bearer <jwt>" expires, or None if it cannot be read."""
    try:
        seg = token.split(" ")[-1].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))
        return float(claims["exp"])
    except Exception:  # noqa: BLE001
        return None


def ms_to_ist(ms) -> Optional[str]:
    """Epoch milliseconds -> IST wall clock 'YYYY-MM-DDTHH:MM' (how the portal shows its dates)."""
    try:
        return datetime.fromtimestamp(int(ms) / 1000, IST).strftime("%Y-%m-%dT%H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _amount(value) -> Optional[float]:
    try:
        v = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def parse_departments(text: str) -> Dict[int, str]:
    return {int(d["organizationId"]): _norm(d.get("organizationName")) for d in json.loads(text)
            if d.get("organizationId") is not None and d.get("organizationName")}


def parse_masters(text: str) -> Dict[str, Dict[int, str]]:
    j = json.loads(text)
    return {"types": {int(x["rfqtypeId"]): _norm(x["description"]) for x in j.get("rfqTypeList", [])},
            "cats": {int(x["proccatId"]): _norm(x["description"]).capitalize() for x in j.get("procCatList", [])}}


def parse_tender_list(text: str, depts: Dict[int, str], masters: Dict[str, Dict[int, str]]) -> List[Dict]:
    """getTenderList JSON -> listing rows (deduplicated by the public tender id)."""
    rows, seen = [], set()
    for t in json.loads(text):
        public, internal = t.get("currentOrgTenderId"), t.get("currenttenderid")
        if not public or not internal or str(public) in seen:
            continue
        seen.add(str(public))
        dept = depts.get(t.get("currentdeptid")) or f"Department {t.get('currentdeptid')}"
        rows.append({
            "id": str(public), "detail_ref": str(internal),
            "ref": _norm(t.get("currenttenderrefno")), "title": _norm(t.get("currentdescription")), "chain": dept,
            "pub": ms_to_ist(t.get("currentTenderPublishDate")), "close": ms_to_ist(t.get("currentbidEndDate")),
            "open": ms_to_ist(t.get("currentbidOpenDate")),
            "category": masters["cats"].get(t.get("currentproccatid")),
            "tender_type": masters["types"].get(t.get("currenttendertypeid")),
        })
    return rows


def parse_preview(text: str) -> Dict:
    """previewTenderByTenderId JSON -> typed fields plus `details` (label -> value). {} if it is not a tender."""
    j = json.loads(text)
    if not isinstance(j, dict) or not j.get("tenderid"):
        return {}
    fields: Dict[str, str] = {}
    payments: Dict[str, str] = {}
    documents: List[str] = []
    for tpl in j.get("templates") or []:
        kind = tpl.get("shortName")
        vals = {f.get("code"): f.get("value") for f in tpl.get("templateFieldList") or []}
        labels = {f.get("code"): f.get("longName") for f in tpl.get("templateFieldList") or []}
        if kind == "rfqdate":
            for code, v in vals.items():
                if v not in (None, "") and ms_to_ist(v):
                    fields[_norm(labels[code])] = ms_to_ist(v)
        elif kind == "prebiddiscussion":
            for code, v in vals.items():
                if v in (None, ""):
                    continue
                fields[_norm(labels[code])] = ms_to_ist(v) if code.endswith("_date") else _norm(str(v))
        elif kind == "rfqpayment":
            if vals.get("payment_type") and _amount(vals.get("amount")):
                payments.setdefault(_norm(vals["payment_type"]), str(_amount(vals["amount"])).rstrip("0").rstrip("."))
        elif kind == "rfqattachment":
            name = _norm(vals.get("file_name") or vals.get("label"))
            if name:
                documents.append(name)
    pmap = j.get("tenderPreviewMap") or {}
    auth = j.get("tenderIssuingAuthority") or {}
    details: Dict[str, str] = {"Tender ID": str(j.get("orgtenderid") or "")}
    if pmap.get("tenderType"):
        details["Tender Type"] = _norm(pmap["tenderType"])
    if pmap.get("procCat"):
        details["Procurement Category"] = _norm(pmap["procCat"])
    details.update(fields)
    for name, amount in payments.items():
        details[name] = amount
    if j.get("offerValidity"):
        details["Offer Validity (days)"] = str(j["offerValidity"])
    out: Dict = {"value": _amount(j.get("pacamt")), "emd": _amount(payments.get("EMD"))}
    # a designation or the department, never an officer's name
    out["authority"] = _norm(auth.get("tenderIssuingAuthorityDesignation") or auth.get("organizationName")) or None
    if documents:
        out["documents"] = sorted(set(documents))
    out["details"] = {k: v for k, v in details.items() if v}
    return {k: v for k, v in out.items() if v not in (None, "", {})}


# ---------------------------------------------------------------- crawl
class Epsv2Portal:
    """Adapter that lets crawler.engine drive an EPSV2 portal (see the interface in engine.py)."""

    throttle_scope = "source"
    # detail calls are independent POSTs; the shared PortalSession still caps the start rate at 1/delay_seconds
    parallel_details = True

    def __init__(self, sess: PortalSession, page_url: str):
        parts = urlsplit(page_url)
        self.sess, self.page_url = sess, page_url
        self.api = f"{parts.scheme}://{parts.netloc}{CONTEXT}"
        self._token: Optional[str] = None
        self._expires = 0.0
        self._org_id: Optional[int] = None
        self._lock = threading.Lock()
        self._by_org: Dict[str, List[Dict]] = {}

    # ---- guest token
    def _fresh_token(self, force: bool = False) -> str:
        with self._lock:
            if force or self._token is None or time.time() > self._expires - TOKEN_MIN_LEFT:
                html = self.sess.get(self.page_url, label="page")
                token = token_from_page(html)
                if not token:
                    raise RuntimeError("no guest token in the listing page (markup changed?)")
                self._token, self._org_id = token, org_id_from_page(html) or self._org_id
                self._expires = token_expiry(token) or (time.time() + 600)
            return self._token

    def _post(self, path: str, body: Optional[Dict] = None, label: str = "", force_token: bool = False) -> str:
        headers = dict(JSON_HEADERS, Authorization=self._fresh_token(force_token))
        return self.sess.post(self.api + path, data=json.dumps(body) if body is not None else None,
                              headers=headers, label=label)

    # ---- adapter interface
    def list_orgs(self) -> List[Dict]:
        self._fresh_token(force=True)
        if self._org_id is None:
            raise RuntimeError("the listing page does not name the portal's organisation id (markup changed?)")
        depts = parse_departments(self._post(f"/rest/organization/loadAllDeptWithParent?orgId={self._org_id}", label="departments"))
        masters = parse_masters(self._post("/rest/master/showRfqCategoryRfqTypeProcCatBidPartList", label="masters"))
        rows = parse_tender_list(self._post("/rest/openarea/getTenderList", {"orgId": self._org_id}, label="list"), depts, masters)
        self._by_org = {}
        for row in rows:
            self._by_org.setdefault(row["chain"], []).append(row)
        return [{"name": name, "count": len(rs), "url": self.page_url} for name, rs in sorted(self._by_org.items())]

    def walk_org(self, org: Dict) -> Iterator[Dict]:
        yield from self._by_org.get(org["name"], [])

    def fetch_detail(self, org: Dict, row: Dict) -> Dict:
        path = f"/rest/tender/previewTenderByTenderId?tenderId={row['detail_ref']}"
        try:
            text = self._post(path, label="detail")
        except (Throttled, HostNotFound):
            raise
        except RequestException:
            text = self._post(path, label="detail", force_token=True)     # an expired token answers HTTP 500: new token, once
        try:
            fetched = parse_preview(text)
        except ValueError as e:
            raise engine.DetailFailed(f"detail {row['id']}: not JSON ({e})") from e
        if not fetched.get("details"):
            raise engine.DetailFailed(f"detail {row['id']}: no tender in the response (markup changed?)")
        return fetched

    def restart_org(self, org: Dict) -> Optional[Dict]:
        return org        # no per-organisation session state; engine.Restart is never raised

    def requests_made(self) -> int:
        return getattr(self.sess, "requests_made", 0)


def crawl_source(src: Dict, store: Dict[str, Dict], limit_orgs: Optional[int] = None,
                 only_orgs: Optional[List[str]] = None, no_details: bool = False,
                 dump_dir: Optional[str] = None, checkpoint=None, progress=None,
                 force_refresh: bool = False, stop=None, **_other) -> Dict:
    """Crawl one EPSV2 portal (all its departments) into `store`. Policy: see crawler.engine.crawl."""
    page_url = src["base_url"]
    sess = PortalSession(page_url, delay_seconds=float(src.get("delay_seconds", 1.0)),
                         dump_dir=dump_dir, ignore_robots=bool(src.get("ignore_robots", False)),
                         extra_ca_certs=src.get("extra_ca_certs"))
    return engine.crawl(src, store, Epsv2Portal(sess, page_url), limit_orgs=limit_orgs, only_orgs=only_orgs,
                        no_details=no_details, checkpoint=checkpoint, progress=progress,
                        force_refresh=force_refresh, stop=stop, change_keys=CHANGE_KEYS,
                        listing_fields=LISTING_FIELDS, detail_fields=DETAIL_FIELDS)
