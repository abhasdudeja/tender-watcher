"""The "Tender Management System" eProcurement portals: Telangana (tender.telangana.gov.in) and Andhra Pradesh
(tender.apeprocurement.gov.in). One Java platform (the pages are titled "eProcurement - Tender Management
System - Govt of ..."), neither GePNIC, nProcure nor EPSV2. The public "Live Tenders" list is a server-side DataTables
grid fed by a JSON endpoint; no login and no CAPTCHA. Used here exactly as the page uses it:

    GET  /login.html                       -> session cookie + the CSRFToken hidden field
    POST /TenderDetailsHome.html           hdnType=current + CSRFToken -> the list page: its own CSRFToken, the
                                           department <select>, the table's column headings and the hidden form
                                           every detail view posts
    GET  /TenderDetailsHomeJson.html?...   the form's fields (without CSRFToken, as the page does) + DataTables
                                           (sEcho / iDisplayStart / iDisplayLength): the list rows, 100 per request
                                           (all live tenders are about 2,000). Telangana answers plain JSON,
                                           Andhra Pradesh the same JSON Base64-encoded
    POST /ViewTender.html                  the list form + hdnIndentID / hdnTenderCategory / hdnProcurementID
                                           (the three numbers inside the row's viewBtn(...)) -> the detail page

A department (the portal's own department list) plays the role of a GePNIC *organisation*: every row is read in
list_orgs and walk_org serves each department's rows. The row's "Department Name" cell is "<department>-<circle or
division>"; the circle becomes the second part of `chain`. The columns differ a little by portal (Telangana lists a
published date, Andhra Pradesh does not), so they are found from the table's own headings. The portal
administrator's test tenders (Eprocadmin) are skipped. The page's JavaScript can encrypt the form fields before
posting; the portals accept the plain fields (that call is commented out in the pages), so nothing is encrypted.

Records use the same keys as the other crawlers. Officers' names, addresses, phone numbers and e-mail addresses on
the detail page are never stored; `authority` is the circle / division. The crawl loop is crawler/engine.py.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from datetime import datetime
from typing import Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from . import engine
from .http import PortalSession

log = logging.getLogger("tenderwatch.tms")

PAGE = 100                       # the grid's largest page size
MAX_SHORTFALL = 0.05             # fewer than this fraction missing from the paged list is normal (tenders close meanwhile)
TEST_DEPARTMENTS = ("eprocadmin",)   # the portal administrator's own test tenders

CHANGE_KEYS = ("close", "open", "title", "ref", "pub", "value")
LISTING_FIELDS = ("ref", "title", "chain", "pub", "close", "open", "category", "value")
DETAIL_FIELDS = ("emd", "authority", "documents", "details")

# list-table heading -> what the column is (first match wins)
COLUMN_ROLES = (("department", r"department"), ("id", r"^tender id"), ("ref", r"notice number|enquiry"),
                ("category", r"category"), ("title", r"name of work"), ("value", r"estimated contract value"),
                ("pub", r"publish"), ("open", r"start|opening"), ("close", r"clos"), ("action", r"action"))
DEFAULT_COLUMNS = ["department", "id", "ref", "category", "title", "value", "pub", "open", "close", "action"]   # Telangana

# detail-page labels that are people's contact details, or only repeat what the list already has
_PRIVATE = re.compile(r"officer|authority|address|contact|e-?mail|mobile|phone|fax", re.I)
_REPEATS = {"tender id", "enquiry/ifb/tender notice number", "ifb no / tender notice number", "tender notice number",
            "name of work"}
_EMD_LABEL = re.compile(r"bid security|emd|proposal security", re.I)
_MONEY_CELL = re.compile(r"(?:rs\.?|inr|₹)?\s*\d[\d,]*(?:\.\d+)?", re.I)
MAX_LABEL, MAX_VALUE = 100, 400      # longer "labels" are paragraphs, not fields; a value is kept to this many characters

_norm = lambda t: re.sub(r"\s+", " ", t or "").strip()  # noqa: E731


# ---------------------------------------------------------------- small parsers
def token_from_page(html: str) -> Optional[str]:
    m = (re.search(r'name=["\']CSRFToken["\'][^>]*?value=["\']([^"\']+)', html)
         or re.search(r'value=["\']([^"\']+)["\'][^>]*?name=["\']CSRFToken', html))
    return m.group(1) if m else None


def parse_dt(text: Optional[str]) -> Optional[str]:
    """'10/10/2026 06:04 PM' -> '2026-10-10T18:04'; a bare '10/10/2026' -> '2026-10-10'; else None."""
    m = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})(?:\s+(\d{1,2}):(\d{2})\s*([ap]m)?)?", text or "", re.I)
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


def _amount(text) -> Optional[float]:
    """A plain amount ('71970000000.00', '₹ 1,200'); None for 0, empty or a percentage ('0.50 % of Quoted value')."""
    s = str(text or "")
    if "%" in s:
        return None
    m = re.search(r"\d[\d,]*(?:\.\d+)?", s)
    try:
        v = float(m.group(0).replace(",", "")) if m else 0.0
    except ValueError:
        return None
    return v if v > 0 else None


def parse_departments(html: str) -> List[str]:
    """The department names of the list page's <select id=nDepartmentID> (whitespace collapsed)."""
    sel = BeautifulSoup(html, "lxml").find("select", id="nDepartmentID")
    names = [_norm(o.get_text()) for o in (sel.find_all("option") if sel else []) if o.get("value") not in (None, "", "0")]
    return [n for n in names if n]


def parse_columns(html: str) -> List[str]:
    """What each column of the list table is, from its headings ('department', 'id', ..., 'action')."""
    table = BeautifulSoup(html, "lxml").find("table", id="pagetable13")
    roles = []
    for th in (table.find_all("th") if table else []):
        text = _norm(th.get_text(" ")).lower()
        roles.append(next((role for role, pat in COLUMN_ROLES if re.search(pat, text)), "other"))
    return roles if {"department", "id", "close"} <= set(roles) else list(DEFAULT_COLUMNS)


def parse_form(html: str) -> Dict[str, str]:
    """Name -> value of the data inputs of the list page's hidden form (the base of each request)."""
    form = BeautifulSoup(html, "lxml").find("form", id="tenderDetailsHome")
    skip = {"button", "submit", "reset", "image"}
    return {i["name"]: i.get("value") or "" for i in (form.find_all("input") if form else [])
            if i.get("name") and (i.get("type") or "text").lower() not in skip}


def split_department(cell: str, departments: List[str]) -> Tuple[str, str]:
    """'DEPT NAME-Circle, Division' -> ('DEPT NAME', 'Circle, Division'), by the longest known department that fits."""
    cell = _norm(cell)
    best = ""
    for d in departments:
        if (cell == d or cell.startswith(d + "-")) and len(d) > len(best):
            best = d
    if best:
        return best, cell[len(best) + 1:].strip()
    return cell, ""


def decode_body(text: str) -> Dict:
    """The list response: JSON, or (Andhra Pradesh) the same JSON Base64-encoded."""
    body = (text or "").strip()
    if body.startswith("{"):
        return json.loads(body)
    try:
        return json.loads(base64.b64decode(body, validate=True).decode("utf-8"))
    except (binascii.Error, ValueError) as e:
        raise ValueError(f"the list response is neither JSON nor Base64 JSON ({e})") from e


def parse_list(text: str, departments: List[str], columns: List[str]) -> Tuple[int, List[Dict]]:
    """One DataTables response -> (total rows the portal says match, listing rows). Test tenders are dropped."""
    j = decode_body(text)
    total = int(str(j.get("iTotalDisplayRecords") or 0) or 0)
    rows = []
    for r in j.get("aaData") or []:
        if len(r) < len(columns):
            continue
        cell = dict(zip(columns, r))
        if not _norm(cell.get("id")):
            continue
        dept, circle = split_department(cell.get("department", ""), departments)
        if dept.lower().startswith(TEST_DEPARTMENTS):
            continue
        view = re.search(r"viewBtn\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", cell.get("action") or r[-1] or "")
        rows.append({
            "id": _norm(cell["id"]), "ref": _norm(cell.get("ref")), "title": _norm(cell.get("title")),
            "chain": f"{dept}||{circle}" if circle else dept, "department": dept,
            "category": _norm(cell.get("category")).capitalize() or None, "value": _amount(cell.get("value")),
            "pub": parse_dt(cell.get("pub")), "open": parse_dt(cell.get("open")), "close": parse_dt(cell.get("close")),
            "detail_ref": ",".join(view.groups()) if view else None,
        })
    return total, rows


def parse_detail(html: str) -> Dict:
    """ViewTender.html -> emd, authority (the circle / division), documents and `details` (label -> value).
    {} if the page is not a tender (a timed-out session shows the log-in page instead)."""
    soup = BeautifulSoup(html, "lxml")
    details: Dict[str, str] = {}
    out: Dict = {}
    for tr in soup.find_all("tr"):
        if tr.find("table"):
            continue
        cells = [_norm(c.get_text(" ")) for c in tr.find_all(["td", "th"], recursive=False)]
        if len(cells) == 1 or not any(cells):
            continue
        if cells[0].lower().startswith("bid security (inr)"):
            # Andhra Pradesh: the amount is in this row ("Bid Security (INR) | Rs. | 25700000.00 | ...");
            # Telangana: this is the header row and the amount opens the next one ("Rs. 5513.00 | ...")
            nxt = tr.find_next("tr")                  # (the header may sit in a <thead>, the amount in a <tbody>)
            after = [_norm(c.get_text(" ")) for c in nxt.find_all("td", recursive=False)] if nxt else []
            amount = next((c for c in cells[1:] + after[:1] if _MONEY_CELL.fullmatch(c)), None)
            if amount and _amount(amount):
                out["emd"] = _amount(amount)
                details["Bid Security (INR)"] = amount
            continue
        for label, value in zip(cells[0::2], cells[1::2]):
            label = label.rstrip(":").strip()
            low = label.lower()
            if low.startswith("transaction fee") and _amount(value):
                details["Transaction Fee (INR)"] = str(_amount(value)).rstrip("0").rstrip(".")
            elif (not label or not value or low in _REPEATS or len(label) > MAX_LABEL or label.isdigit()     # numbered checklist rows
                  or (_PRIVATE.search(label) and low != "circle/division")):
                continue
            else:
                details.setdefault(label, value[:MAX_VALUE])
            if _EMD_LABEL.search(label) and "emd" not in out and _amount(value) and re.fullmatch(r"[\s\d,.]+", value):
                out["emd"] = _amount(value)
    if "Circle/Division" not in details and "Department Name" not in details:
        return {}
    if details.get("Circle/Division"):
        out["authority"] = details["Circle/Division"]
    table = soup.find("table", id="VTMSBodyContent_TenderDetailsGridView")
    if table:
        names = [_norm(tr.find_all("td")[1].get_text(" ")) for tr in table.find_all("tr") if len(tr.find_all("td")) > 1]
        docs = sorted({n for n in names if n and n.lower() != "document name"})
        if docs:
            out["documents"] = docs
    out["details"] = details
    return out


# ---------------------------------------------------------------- crawl
class TmsPortal:
    """Adapter that lets crawler.engine drive a Tender Management System portal (see the interface in engine.py)."""

    throttle_scope = "source"
    parallel_details = False      # one shared server session: detail pages go one after the other

    def __init__(self, sess: PortalSession, base_url: str):
        self.sess, self.base = sess, base_url.rstrip("/")
        self._form: Dict[str, str] = {}
        self._columns: List[str] = list(DEFAULT_COLUMNS)
        self._departments: List[str] = []
        self._by_org: Dict[str, List[Dict]] = {}

    # ---- session
    def _open(self) -> None:
        """Log-in page (session + token), then the list page (its token, department list, columns and detail form)."""
        token = token_from_page(self.sess.get(self.base + "/login.html", label="login"))
        if not token:
            raise RuntimeError("no CSRF token on the log-in page (markup changed?)")
        page = self.sess.post(self.base + "/TenderDetailsHome.html", label="list page", data={
            "hdnType": "current", "CSRFToken": token, "hdnPage": "login.html",
            "hdnEncryptNames": "hdnEncryptNames", "hdnEncryptValues": "hdnEncryptValues"})
        self._form, self._departments, self._columns = parse_form(page), parse_departments(page), parse_columns(page)
        if not self._form.get("CSRFToken") or not self._departments:
            raise RuntimeError("the list page has no form or department list (markup changed?)")

    def _page_url(self, start: int) -> str:
        """The data request as the page makes it: its form fields (no CSRFToken) + the grid's own parameters."""
        q = {k: v for k, v in self._form.items() if k != "CSRFToken"}
        for select in ("subDeptId", "ddlDistrict", "ddlMandal", "biddingType", "sProcurementType", "dtBidClosingselect"):
            q.setdefault(select, "")
        q.setdefault("nDepartmentID", "0")
        n = len(self._columns)
        q.update({"sEcho": "1", "iColumns": str(n), "iDisplayStart": str(start), "iDisplayLength": str(PAGE),
                  "iSortCol_0": str(self._columns.index("id")), "sSortDir_0": "asc", "iSortingCols": "1"})   # by tender id: stable while paging
        for i in range(n):
            q[f"mDataProp_{i}"] = str(i)
            q[f"bSortable_{i}"] = "false" if self._columns[i] == "action" else "true"
        return self.base + "/TenderDetailsHomeJson.html?" + urlencode(q)

    # ---- adapter interface
    def list_orgs(self) -> List[Dict]:
        self._open()
        rows: Dict[str, Dict] = {}
        total, start = None, 0
        while total is None or start < total:
            page_total, got = parse_list(self.sess.get(self._page_url(start), label="list"), self._departments, self._columns)
            total = page_total
            if not total:
                break
            for r in got:
                rows.setdefault(r["id"], r)
            start += PAGE
        if total and len(rows) < total * (1 - MAX_SHORTFALL) - 5:      # (test tenders are dropped, so a few are missing)
            raise RuntimeError(f"the paged list returned only {len(rows)} of {total} tenders")
        self._by_org = {}
        for r in rows.values():
            self._by_org.setdefault(r["department"], []).append(r)
        return [{"name": name, "count": len(rs), "url": self.base} for name, rs in sorted(self._by_org.items())]

    def walk_org(self, org: Dict) -> Iterator[Dict]:
        yield from self._by_org.get(org["name"], [])

    def fetch_detail(self, org: Dict, row: Dict) -> Dict:
        if not row.get("detail_ref"):
            raise engine.DetailFailed(f"detail {row['id']}: the row has no view link")
        indent, category, procurement = row["detail_ref"].split(",")
        data = dict(self._form, popUPRequestParameter="popUPRequestParameter", hdnPreviousPage="TenderDetailsHome.html",
                    hdnIndentID=indent, hdnTenderCategory=category, hdnProcurementID=procurement)
        for attempt in (1, 2):
            html = self.sess.post(self.base + "/ViewTender.html", data=data, label="detail")
            fetched = parse_detail(html)
            if fetched.get("details"):
                return fetched
            if attempt == 1 and ("automatically logged out" in html or "SessionTimeOut" in html or 'name="loginForm"' in html):
                log.info("session ended, logging in again")
                self._open()
                data["CSRFToken"] = self._form["CSRFToken"]
                continue
        raise engine.DetailFailed(f"detail {row['id']}: no tender in the response (markup changed?)")

    def restart_org(self, org: Dict) -> Optional[Dict]:
        return org        # no per-organisation session state; engine.Restart is never raised

    def requests_made(self) -> int:
        return getattr(self.sess, "requests_made", 0)


def crawl_source(src: Dict, store: Dict[str, Dict], limit_orgs: Optional[int] = None,
                 only_orgs: Optional[List[str]] = None, no_details: bool = False,
                 dump_dir: Optional[str] = None, checkpoint=None, progress=None,
                 force_refresh: bool = False, stop=None, **_other) -> Dict:
    """Crawl one Tender Management System portal (all its departments) into `store`. Policy: see crawler.engine.crawl."""
    base = src["base_url"]
    sess = PortalSession(base, delay_seconds=float(src.get("delay_seconds", 1.0)), dump_dir=dump_dir,
                         ignore_robots=bool(src.get("ignore_robots", False)), extra_ca_certs=src.get("extra_ca_certs"))
    return engine.crawl(src, store, TmsPortal(sess, base), limit_orgs=limit_orgs, only_orgs=only_orgs,
                        no_details=no_details, checkpoint=checkpoint, progress=progress,
                        force_refresh=force_refresh, stop=stop, change_keys=CHANGE_KEYS,
                        listing_fields=LISTING_FIELDS, detail_fields=DETAIL_FIELDS)
