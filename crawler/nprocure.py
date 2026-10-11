"""Stub for nProcure portals (the Gujarat state portal, tender.nprocure.com).

NOT IMPLEMENTED. nProcure is a private platform ((n)Code Solutions). Its public tender
listing needs no login or CAPTCHA and the site has no robots rules (/robots.txt returns the
app's HTML), but the page's JavaScript encrypts the listing request, and permission or an API
from the portal's operator has not been obtained. Decide that before writing the parser.

What is known (see README "Roadmap"): the portal serves ~165 "clients" (departments, boards,
municipal corporations, Nagarpalikas) with a tender count each in the Client dropdown.
Choosing a client only offers "Go to subdomain" (e.g. smctender.nprocure.com); the state
portal's own table already lists ALL ~4,058 tenders, each row naming its department
("NAGARPALIKA-Gondal Nagar Seva Sadan", "DGVCL-City Division - Navsari").

CONFIRMED against the live portal (2026-10-12), plain HTTP, no browser:
* Detail: `POST /view-nit-home` form fields `_csrf=token&tenderid=<id>` returns the full
  "View NIT" page (~36 KB: organisation, department, notice no, category, estimated cost,
  bid dates, remarks ...). No cookie, Referer or encryption is needed. Documents are
  /view-nit-document and corrigenda /view-nit-amendment (same form).
* Listing: `POST /beforeLoginTenderTableList`, Content-Type application/json, answered with
  DataTables JSON {iTotalRecords, recordsFiltered, tenderStatus, data:[{"1": notice no,
  "2": HTML fragment (department, "Tender Id :N", "Name Of Work :", "Estimated Contract
  Value :", "Last Date & Time For Submission : dd-mm-yyyy HH:MM:SS"), "3": documents link}]}.
  iDisplayLength 150 works (~300 KB, ~0.7 s per page; 4,058 rows = 28 pages) and
  iDisplayStart pages without overlap. The request body is encrypted client-side:
      plain  = JSON {"reqData": [{"name","value"}...DataTables params...], "_csrf": "",
                     "idList": "0", "id": "Tenders In Progress"}
      number = random 5-char alphanumeric string
      key    = PBKDF2-HMAC-SHA1(number, salt=16 random bytes, 1000 iterations, 16 bytes)
      body   = {"jsonData": base64(AES-128-CBC/PKCS7(plain, key, iv=16 random bytes)),
                "iv": hex, "salt": hex, "key": base64(number)}
  The key travels in the same body, so this is obfuscation, not access control.
* Gotchas: the JSON can contain a stray non-UTF-8 byte (0xAE) despite charset=UTF-8 -
  decode with errors="replace"; the same tender id can appear twice in a page - dedupe;
  estimated value can be 0.00 (= not stated).

To implement, mirror crawler/gepnic.py:

    def crawl_source(src, store, limit_orgs=None, only_orgs=None, dump_dir=None,
                     checkpoint=None, progress=None, no_details=False,
                     force_refresh=False, stop=None) -> status dict

(checkpoint(org_name) saves that organisation's shard; progress(snapshot) feeds the progress
counter; stop is a threading.Event for a clean Ctrl-C. See gepnic.crawl_source.)

and register "nprocure" in crawler/run.py:CRAWLERS. Records must use the same
keys as gepnic (id, source, ref, title, org, chain, pub, close, open, ...).
"""
from __future__ import annotations

from typing import Dict, Iterable, Optional


def crawl_source(
    src: Dict,
    store: Dict[str, Dict],
    limit_orgs: Optional[int] = None,
    only_orgs: Optional[Iterable[str]] = None,
    dump_dir: Optional[str] = None,
    fetch_details: Optional[bool] = None,
    checkpoint=None,
    **_other,   # no_details, progress, force_refresh, stop ... passed by run.py; see gepnic.crawl_source
) -> Dict:
    raise NotImplementedError(
        "nprocure parser not implemented; see crawler/nprocure.py docstring"
    )
