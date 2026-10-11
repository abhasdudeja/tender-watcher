"""nProcure ("eProc-Suite") portals: the Gujarat state portal, tender.nprocure.com, and its clients.

The portal serves ~165 *clients* (departments, boards, municipal corporations, all Nagarpalikas).
The state home page's Client dropdown lists every client with its tender count and its own
subdomain (class "subDomainName_https://<host>:443"), e.g. Surat -> smctender.nprocure.com. Each
client host serves the same app and the same public tender table with no login, and its listing
total equals the dropdown count - so a client plays the role of a GePNIC *organisation*:

    state home page -> client list (name, count, host)
    per client      -> listing  (POST <host>/beforeLoginTenderTableList, 150 rows per page)
    per tender      -> detail   (POST <host>/view-nit-home  _csrf=token&tenderid=<id>)

The state portal's own table is NOT used: it is ~35 rows short of the clients' sum.

The listing request body is encrypted by the page's JavaScript (AES-128-CBC, PBKDF2 key); the key
travels in the same body, so this is obfuscation, not access control. encrypt_payload reproduces it
exactly (verified against the portal's own CryptoJS code and the live server). Detail pages are plain
form POSTs. All hosts share the *.nprocure.com certificate whose intermediate the server omits - set
`extra_ca_certs: certs/sectigo-ov-r36.pem` on the source (see certs/README.md).

Records use the same keys as the GePNIC crawler. `org` is the client, `chain` is
"Client||Department". The crawl loop (refresh policy, retention, checkpoints, progress) is
crawler/engine.py, shared with every other portal type.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import string
from datetime import datetime
from typing import Dict, Iterator, List, Optional
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from . import engine
from .gepnic import parse_money
from .http import PortalSession

log = logging.getLogger("tenderwatch.nprocure")

LISTING_PATH = "/beforeLoginTenderTableList"
DETAIL_PATH = "/view-nit-home"
PAGE_SIZE = 150                 # the table offers 10/50/100/150
MAX_PAGES_PER_CLIENT = 400      # 60,000 tenders: a runaway guard, not a limit anyone should hit
JSON_HEADERS = {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"}

# listing fields whose change means "re-fetch this tender's detail page" (the listing has no
# publish or opening date; it does show the estimated value)
CHANGE_KEYS = ("close", "title", "ref", "value")
LISTING_FIELDS = ("ref", "title", "chain", "close", "value", "doc_count")
# filled from the detail page; wiped before a refresh so a field the portal dropped doesn't linger
DETAIL_FIELDS = ("category", "product_category", "value", "emd", "location", "bid_end", "pub",
                 "tender_type", "form_of_contract", "period", "authority", "authority_address",
                 "description", "documents", "details")

# detail-page label (lower-case prefix) -> record field; the first matching label wins
DETAIL_MAP = {
    "location": "location",
    "tender type": "tender_type",
    "description of material": "description",
    "form of contract": "form_of_contract",
    "product category": "product_category",
    "tender category": "category",
    "estimated cost value": "value",
    "period of completion": "period",
    "bid document download start date": "pub",
    "bid submission closing date": "bid_end",
    "officer inviting bids": "authority",
    "address": "authority_address",
}
# detail tables that are not tender information and must never be stored: a bidder's digital
# signature certificate (names a person) lives under this heading
SKIP_HEADINGS = ("certificate",)


# ---------------------------------------------------------------- request encryption
def _aes_cbc(key: bytes, iv: bytes):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    return Cipher(algorithms.AES(key), modes.CBC(iv))


def _derive_key(number: str, salt: bytes) -> bytes:
    """CryptoJS.PBKDF2(passphrase, salt, {keySize: 4 words, iterations: 1000}); SHA-1 is its default hash."""
    return hashlib.pbkdf2_hmac("sha1", number.encode("utf-8"), salt, 1000, 16)


def encrypt_payload(plain: str, number: Optional[str] = None, salt: Optional[bytes] = None,
                    iv: Optional[bytes] = None) -> Dict[str, str]:
    """The portal's request envelope: {"jsonData", "iv", "salt", "key"}.

    `number` is the short random string the page makes up; the PBKDF2 passphrase is the string itself
    and the `key` field carries it Base64-encoded (the page's encrypt() Base64-decodes it again).
    """
    from cryptography.hazmat.primitives import padding

    number = number or "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(5))
    salt = salt or os.urandom(16)
    iv = iv or os.urandom(16)
    padder = padding.PKCS7(128).padder()
    data = padder.update(plain.encode("utf-8")) + padder.finalize()
    enc = _aes_cbc(_derive_key(number, salt), iv).encryptor()
    return {"jsonData": base64.b64encode(enc.update(data) + enc.finalize()).decode("ascii"),
            "iv": iv.hex(), "salt": salt.hex(),
            "key": base64.b64encode(number.encode("utf-8")).decode("ascii")}


def decrypt_payload(body: Dict[str, str]) -> str:
    """Inverse of encrypt_payload (for tests and debugging what the page sends)."""
    from cryptography.hazmat.primitives import padding

    number = base64.b64decode(body["key"]).decode("utf-8")
    dec = _aes_cbc(_derive_key(number, bytes.fromhex(body["salt"])), bytes.fromhex(body["iv"])).decryptor()
    padded = dec.update(base64.b64decode(body["jsonData"])) + dec.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")


def ao_data(start: int, length: int, echo: int = 1) -> List[Dict]:
    """The legacy DataTables server-side parameters the page sends for its 3-column tender table."""
    return [{"name": "sEcho", "value": echo}, {"name": "iColumns", "value": 3}, {"name": "sColumns", "value": ",,"},
            {"name": "iDisplayStart", "value": start}, {"name": "iDisplayLength", "value": length},
            {"name": "mDataProp_0", "value": "1"}, {"name": "bSortable_0", "value": True},
            {"name": "mDataProp_1", "value": "2"}, {"name": "bSortable_1", "value": True},
            {"name": "mDataProp_2", "value": "3"}, {"name": "bSortable_2", "value": False},
            {"name": "iSortCol_0", "value": 0}, {"name": "sSortDir_0", "value": "asc"},
            {"name": "iSortingCols", "value": 1}]


def build_listing_body(start: int = 0, length: int = PAGE_SIZE, **crypto) -> str:
    """JSON text to POST to LISTING_PATH for rows [start, start+length) of a client's active tenders."""
    plain = json.dumps({"reqData": ao_data(start, length), "_csrf": "", "idList": "0",
                        "id": "Tenders In Progress"}, separators=(",", ":"))
    return json.dumps(encrypt_payload(plain, **crypto), separators=(",", ":"))


# ---------------------------------------------------------------- parsing
_norm = lambda t: re.sub(r"\s+", " ", t or "").strip()  # noqa: E731


def parse_dt(text: Optional[str]) -> Optional[str]:
    """'21-10-2026 18:00:00' or '21-10-2026 18:00' -> '2026-10-21T18:00'; a bare date -> '2026-10-21'."""
    m = re.search(r"(\d{1,2})-(\d{1,2})-(\d{4})(?:\s+(\d{1,2}):(\d{2})(?::\d{2})?)?", text or "")
    if not m:
        return None
    d, mo, y, hh, mm = m.groups()
    try:
        datetime(int(y), int(mo), int(d))
    except ValueError:
        return None
    out = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    return out if hh is None else f"{out}T{int(hh):02d}:{mm}"


def parse_client_list(html: str) -> List[Dict]:
    """The state home page's Client dropdown -> [{id, name, count, host}] (every client, even with 0 tenders)."""
    clients = []
    for opt in BeautifulSoup(html, "lxml").find_all("option"):
        cls = " ".join(opt.get("class") or [])
        m = re.search(r"subDomainName_(https?://[^\s\"':]+)", cls)
        if not m:
            continue
        text = _norm(opt.get_text(" "))
        c = re.match(r"^(.*?)\s*\((\d+)\)$", text)
        clients.append({"id": opt.get("value", ""), "name": c.group(1) if c else text,
                        "count": int(c.group(2)) if c else 0, "host": m.group(1)})
    return clients


def parse_listing(text: str, client: str = "") -> Dict:
    """The listing JSON -> {"total": int, "rows": [...]}. Rows are deduplicated by tender id.

    Row: id, ref, title, chain ("Client||Department"), close, value (None if 0.00), doc_count.
    """
    j = json.loads(text)
    rows, seen = [], set()
    for item in j.get("data", []):
        frag = BeautifulSoup(item.get("2", ""), "lxml")
        tid_el = frag.find("input", attrs={"name": "tenderid"})
        m = re.search(r"Tender Id\s*:\s*(\d+)", frag.get_text(" "))
        tid = (tid_el.get("value") if tid_el else None) or (m.group(1) if m else None)
        if not tid or tid in seen:
            continue
        seen.add(tid)
        dept_span = frag.find("span", style=re.compile("f44336", re.I))
        dept = _norm(dept_span.find(string=True, recursive=False) or "") if dept_span else ""
        title = ""
        strong = frag.find("strong", string=re.compile(r"Name Of Work", re.I))
        if strong is not None and strong.parent is not None:
            title = _norm(strong.parent.get_text(" ").replace(strong.get_text(), "", 1))
        body = frag.get_text(" ")
        val = re.search(r"Estimated Contract Value\s*:\s*([\d,]+(?:\.\d+)?)", body)
        due = re.search(r"Last Date\s*&\s*Time For Submission\s*:\s*(\d{1,2}-\d{1,2}-\d{4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)", body)
        docs = re.search(r"Total No\s*:\s*(\d+)", BeautifulSoup(item.get("3", ""), "lxml").get_text(" "))
        value = parse_money(val.group(1)) if val else None
        rows.append({
            "id": tid, "ref": _norm(item.get("1", "")), "title": title,
            "chain": f"{client}||{dept}" if client and dept else (client or dept),
            "close": parse_dt(due.group(1)) if due else None,
            "value": value if value else None,           # 0.00 means "not stated"
            "doc_count": int(docs.group(1)) if docs else None,
        })
    total = int(j.get("iTotalRecords") or j.get("recordsFiltered") or 0)
    return {"total": total, "rows": rows}


def parse_tender_detail(html: str) -> Dict:
    """A "View NIT" page -> typed fields plus `details` (every label/value pair, raw)."""
    soup = BeautifulSoup(html, "lxml")
    root = soup.find("main") or soup.body or soup
    details: Dict[str, str] = {}
    documents: List[str] = []
    head = root.find(["h1", "h2", "h3", "h4", "h5"], string=re.compile(r"Tender ID", re.I)) \
        or next((h for h in root.find_all(["h1", "h2", "h3", "h4", "h5"]) if re.search(r"Tender ID", h.get_text())), None)
    if head is not None:
        tid = re.search(r"Tender ID\s*-?\s*(\d+)", head.get_text(" "))
        if tid:
            details["Tender ID"] = tid.group(1)
    for tb in root.find_all("table"):
        h = tb.find_previous(["h1", "h2", "h3", "h4", "h5"])
        heading = _norm(h.get_text(" ")) if h is not None else ""
        if any(w in heading.lower() for w in SKIP_HEADINGS):
            continue
        header = [_norm(c.get_text(" ")) for c in (tb.find("tr").find_all(["td", "th"], recursive=False) if tb.find("tr") else [])]
        if "Document Name" in header and "tender documents" in heading.lower():
            col = header.index("Document Name")
            for tr in tb.find_all("tr")[1:]:
                cells = tr.find_all(["td", "th"], recursive=False)
                if len(cells) > col and _norm(cells[col].get_text(" ")):
                    documents.append(_norm(cells[col].get_text(" ")))
            continue
        for tr in tb.find_all("tr"):
            cells = tr.find_all(["td", "th"], recursive=False)
            if len(cells) == 2:
                key, val = _norm(cells[0].get_text(" ")).rstrip(":"), _norm(cells[1].get_text(" "))
                if key and key not in details:
                    details[key] = val

    out: Dict = {}
    for key, val in details.items():
        low = key.lower()
        if "payable" in low:
            continue
        if low.startswith("bid security/emd/proposal security inr"):
            out.setdefault("emd", val)
            continue
        for needle, field in DETAIL_MAP.items():
            if low.startswith(needle):
                out.setdefault(field, val)
                break
    for f in ("value", "emd"):
        if f in out:
            out[f] = parse_money(out[f])
    if not out.get("value"):
        out.pop("value", None)                              # 0.00 / missing = not stated
    for f in ("pub", "bid_end"):
        if f in out:
            out[f] = parse_dt(out[f])
    if out.get("category"):
        out["category"] = out["category"].title()           # WORKS -> Works, like the GePNIC categories
    if documents:
        out["documents"] = sorted(set(documents))
    out["details"] = details
    return {k: v for k, v in out.items() if v not in (None, "", {})} if details else {}


# ---------------------------------------------------------------- crawl
class NprocurePortal:
    """Adapter that lets crawler.engine drive an nProcure state portal (see the interface in engine.py)."""

    # Each client is its own host, so one that stays throttled (or hung) is skipped after its cool-downs
    # instead of ending the whole source; three in a row do end it (see engine.MAX_ORGS_GIVEN_UP_IN_A_ROW).
    throttle_scope = "org"

    def __init__(self, sess: PortalSession, base: str):
        self.sess, self.base = sess, base.rstrip("/")

    def list_orgs(self) -> List[Dict]:
        clients = parse_client_list(self.sess.get(self.base + "/", label="clients"))
        return [{"name": c["name"], "count": c["count"], "id": c["id"], "host": c["host"], "url": c["host"]}
                for c in clients if c["count"] > 0]

    def walk_org(self, org: Dict) -> Iterator[Dict]:
        host, start, seen = org["host"], 0, set()
        for _ in range(MAX_PAGES_PER_CLIENT):
            text = self.sess.post(host + LISTING_PATH, data=build_listing_body(start, PAGE_SIZE),
                                  headers=JSON_HEADERS, label="list")
            page = parse_listing(text, org["name"])
            if not page["rows"]:
                break
            for row in page["rows"]:
                if row["id"] not in seen:
                    seen.add(row["id"])
                    row["detail_ref"] = row["id"]
                    yield row
            start += PAGE_SIZE
            if start >= page["total"]:
                break
        if page["total"] != org["count"]:
            log.warning("%s: listing says %d tenders, the client list said %d", org["name"], page["total"], org["count"])

    def fetch_detail(self, org: Dict, row: Dict) -> Dict:
        html = self.sess.post(org["host"] + DETAIL_PATH, data={"_csrf": "token", "tenderid": row["detail_ref"]},
                              label="detail")
        fetched = parse_tender_detail(html)
        if not fetched.get("details"):
            raise engine.DetailFailed(f"detail {row['id']}: no label/value pairs parsed (markup changed?)")
        return fetched

    def restart_org(self, org: Dict) -> Optional[Dict]:
        return org        # no server-side session state to lose; engine.Restart is never raised

    def requests_made(self) -> int:
        return getattr(self.sess, "requests_made", 0)


def crawl_source(src: Dict, store: Dict[str, Dict], limit_orgs: Optional[int] = None,
                 only_orgs: Optional[List[str]] = None, no_details: bool = False,
                 dump_dir: Optional[str] = None, checkpoint=None, progress=None,
                 force_refresh: bool = False, stop=None, **_other) -> Dict:
    """Crawl one nProcure state portal (all its clients) into `store`. Policy: see crawler.engine.crawl."""
    base = src["base_url"].rstrip("/")
    sess = PortalSession(base, delay_seconds=float(src.get("delay_seconds", 2.0)),
                         dump_dir=dump_dir, ignore_robots=bool(src.get("ignore_robots", False)),
                         extra_ca_certs=src.get("extra_ca_certs"))
    return engine.crawl(src, store, NprocurePortal(sess, base), limit_orgs=limit_orgs, only_orgs=only_orgs,
                        no_details=no_details, checkpoint=checkpoint, progress=progress,
                        force_refresh=force_refresh, stop=stop, change_keys=CHANGE_KEYS,
                        listing_fields=LISTING_FIELDS, detail_fields=DETAIL_FIELDS)
