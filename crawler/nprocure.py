"""Stub for nProcure portals (e.g. smctender.nprocure.com).

NOT IMPLEMENTED. smctender.nprocure.com publishes a robots.txt that disallows
automated access, and nProcure is a private platform ((n)Code Solutions).
Obtain permission or an API from the municipal corporation / nProcure before
writing a parser here.

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
