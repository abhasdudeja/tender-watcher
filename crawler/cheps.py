"""Chhattisgarh Government Integrated e-Procurement Portal (CHEPS, eproc.cgstate.gov.in/CHEPS).

The home page has a "LOAD LIVE DATA" button; the portal's owners (CHiPS) pointed the project to it. The button runs
loadLiveData(), which submits the page's own `loadForm`:

    GET  /CHEPS/security/getSignInAction.do              -> the home page: loadForm with its OWASP_CSRFTOKEN
    POST /CHEPS/security/getSignInAction.do              loadAlldata=Y + that token (the button's own form)
         -> the same page with a table of EVERY live tender, grouped by department (about 570 tenders, ~2 MB)

No login and no CAPTCHA is involved. Each row (`javascript:viewRfq(<tender no>, <part>)`) shows the tender number,
the bid submission start and due dates, the description (the portal cuts it at about 50 characters), the probable
amount of contract ("NA" when not stated) and whether a corrigendum was issued. Opening a tender is a separate
page that is not part of what the owners pointed to, so this crawler reads the list only (no detail pages).

A department plays the role of a GePNIC *organisation*. Records use the same keys as the other crawlers; the
tender number is both `id` and `ref`. The crawl loop is crawler/engine.py.
"""
from __future__ import annotations

import logging
import re
from typing import Dict, Iterator, List, Optional

from bs4 import BeautifulSoup

from . import engine
from .http import PortalSession

log = logging.getLogger("tenderwatch.cheps")

HOME = "/CHEPS/security/getSignInAction.do"
LISTING_FIELDS = ("ref", "title", "chain", "pub", "close", "value", "corrigendum")
CHANGE_KEYS = ("close", "title", "pub", "value", "corrigendum")

_norm = lambda t: re.sub(r"\s+", " ", t or "").strip()  # noqa: E731


def parse_dt(text: Optional[str]) -> Optional[str]:
    """'2026-10-12 8:00:00 PM' -> '2026-10-12T20:00'; else None."""
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})(?::\d{2})?\s*([AP]M)?", text or "", re.I)
    if not m:
        return None
    y, mo, d, hh, mm, ap = m.groups()
    hour = int(hh) % 12 + (12 if (ap or "").upper() == "PM" else 0) if ap else int(hh)
    if not (0 <= hour < 24 and 0 <= int(mm) < 60 and 1 <= int(mo) <= 12 and 1 <= int(d) <= 31):
        return None
    return f"{y}-{mo}-{d}T{hour:02d}:{mm}"


def _amount(text: str) -> Optional[float]:
    try:
        v = float(_norm(text).replace(",", ""))
    except ValueError:
        return None            # "NA"
    return v if v > 0 else None


def parse_form(home: str) -> Dict[str, str]:
    """The fields of the home page's `loadForm` (the LOAD LIVE DATA button's form)."""
    soup = BeautifulSoup(home, "lxml")
    form = soup.find("form", id="loadForm") or soup.find("form", attrs={"name": "loadForm"})
    return {i["name"]: i.get("value") or "" for i in (form.find_all("input") if form else []) if i.get("name")}


def parse_live(html: str) -> List[Dict]:
    """The live-data page -> listing rows. A department heading is a one-cell row; its tenders follow it."""
    soup = BeautifulSoup(html, "lxml")
    rows, seen = [], set()
    for tr in soup.find_all("tr", attrs={"data-href": re.compile(r"viewRfq")}):
        cells = tr.find_all("td", recursive=False)
        view = re.search(r"viewRfq\(\s*(\d+)\s*,\s*(\d+)\s*\)", tr.get("data-href", ""))
        if len(cells) < 7 or not view:
            continue
        number = _norm(cells[1].get_text())
        if not number or number in seen:
            continue
        dept = None
        for prev in tr.find_previous_siblings("tr"):          # the nearest heading row above it
            if not prev.get("data-href") and len(prev.find_all("td", recursive=False)) == 1:
                dept = _norm(prev.get_text(" "))
                break
        seen.add(number)
        tip = cells[4].find("span", class_="tooltiptext")               # the cell repeats its text in a tooltip: take it once
        title = _norm((tip or cells[4]).get_text(" "))
        rows.append({
            "id": number, "ref": number, "title": title, "department": dept or "Unknown department",
            "chain": dept or "Unknown department", "pub": parse_dt(cells[2].get_text()), "close": parse_dt(cells[3].get_text()),
            "value": _amount(cells[5].get_text()), "corrigendum": "Yes" if _norm(cells[6].get_text()).upper().startswith("Y") else "No",
            "detail_ref": f"{view.group(1)},{view.group(2)}",
        })
    return rows


# ---------------------------------------------------------------- crawl
class ChepsPortal:
    """Adapter that lets crawler.engine drive CHEPS (see the interface in engine.py). List only: no detail pages."""

    throttle_scope = "source"
    parallel_details = False

    def __init__(self, sess: PortalSession, base_url: str):
        self.sess, self.base = sess, base_url.rstrip("/")
        self._by_org: Dict[str, List[Dict]] = {}

    def list_orgs(self) -> List[Dict]:
        home = self.sess.get(self.base + HOME, label="home")
        form = parse_form(home)
        if not form.get("OWASP_CSRFTOKEN"):
            raise RuntimeError("the home page has no LOAD LIVE DATA form or security token (markup changed?)")
        form["loadAlldata"] = "Y"                                     # what the button's loadLiveData() submits
        rows = parse_live(self.sess.post(self.base + HOME, data=form, label="list"))
        if not rows:
            raise RuntimeError("LOAD LIVE DATA returned no tenders (markup changed?)")
        self._by_org = {}
        for r in rows:
            self._by_org.setdefault(r["department"], []).append(r)
        return [{"name": name, "count": len(rs), "url": self.base} for name, rs in sorted(self._by_org.items())]

    def walk_org(self, org: Dict) -> Iterator[Dict]:
        yield from self._by_org.get(org["name"], [])

    def fetch_detail(self, org: Dict, row: Dict) -> Dict:
        raise engine.DetailFailed("CHEPS detail pages are not read")

    def restart_org(self, org: Dict) -> Optional[Dict]:
        return org

    def requests_made(self) -> int:
        return getattr(self.sess, "requests_made", 0)


def crawl_source(src: Dict, store: Dict[str, Dict], limit_orgs: Optional[int] = None,
                 only_orgs: Optional[List[str]] = None, no_details: bool = False,
                 dump_dir: Optional[str] = None, checkpoint=None, progress=None,
                 force_refresh: bool = False, stop=None, **_other) -> Dict:
    """Crawl CHEPS (every department's live tenders, from the LOAD LIVE DATA page) into `store`."""
    base = src["base_url"]
    sess = PortalSession(base, delay_seconds=float(src.get("delay_seconds", 1.0)), dump_dir=dump_dir,
                         ignore_robots=bool(src.get("ignore_robots", False)), extra_ca_certs=src.get("extra_ca_certs"))
    return engine.crawl(src, store, ChepsPortal(sess, base), limit_orgs=limit_orgs, only_orgs=only_orgs,
                        no_details=True, checkpoint=checkpoint, progress=progress,
                        force_refresh=force_refresh, stop=stop, change_keys=CHANGE_KEYS,
                        listing_fields=LISTING_FIELDS, detail_fields=())
