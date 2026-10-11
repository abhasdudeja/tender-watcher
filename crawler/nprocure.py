"""Stub for nProcure portals (the Gujarat state portal, tender.nprocure.com).

NOT IMPLEMENTED. nProcure is a private platform ((n)Code Solutions). Its public tender
listing needs no login or CAPTCHA and the site has no robots rules (/robots.txt returns the
app's HTML), but the page's JavaScript encrypts the listing request, and permission or an API
from the portal's operator has not been obtained. Decide that before writing the parser.

What is known (see README "Roadmap"): the portal serves ~165 "clients" (departments, boards,
municipal corporations, Nagarpalikas) with a tender count each in the Client dropdown;
listing = POST /beforeLoginTenderTableList (DataTables, 10/50/100/150 rows per page);
details = POST /view-nit-home (tender id), documents /view-nit-document, corrigenda
/view-nit-amendment. TLS: the server omits its intermediate certificate; use the source's
`extra_ca_certs` (certs/README.md).

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
