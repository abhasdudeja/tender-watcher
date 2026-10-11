"""Stub for nProcure portals (e.g. smctender.nprocure.com).

NOT IMPLEMENTED. smctender.nprocure.com publishes a robots.txt that disallows
automated access, and nProcure is a private platform ((n)Code Solutions).
Obtain permission or an API from the municipal corporation / nProcure before
writing a parser here.

To implement, mirror crawler/gepnic.py:

    def crawl_source(src, store, limit_orgs=None, only_orgs=None,
                     dump_dir=None, fetch_details=None) -> status dict

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
) -> Dict:
    raise NotImplementedError(
        "nprocure parser not implemented; see crawler/nprocure.py docstring"
    )
