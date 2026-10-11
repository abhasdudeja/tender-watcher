"""End-to-end crawl against a fake GePNIC portal (no network).

Simulates: org list -> org page 1 -> Next -> page 2 -> detail pages, including one
stale-session hit on the first attempt to make sure the restart path works.
"""
from pathlib import Path
from unittest import mock

from crawler import gepnic
from crawler.http import PortalSession

FIX = Path(__file__).parent / "fixtures"
BASE = "https://example.test/nicgep/app"

ORG_LIST = (FIX / "gepnic_org_list.html").read_text()
LIST_P1 = (FIX / "gepnic_tender_list.html").read_text()
LIST_P2 = LIST_P1.replace("2026_LSGD_812345_1", "2026_LSGD_999999_1").replace(
    "2026_KSRTC_812400_1", "2026_KSRTC_999998_1").replace('id="loadNext"', 'id="nope"').replace('title="Next"', "")
DETAIL = (FIX / "gepnic_tender_detail.html").read_text()
STALE = "<html><head><title>Stale Session</title></head><body>Your session has timed out.</body></html>"


class FakePortal:
    def __init__(self):
        self.calls = []
        self.stale_once = True

    def get(self, _sess, url, _allow_stale=False, label=""):
        self.calls.append(url)
        if "service=restart" in url:
            return "<html>home</html>"
        if "FrontEndTendersByOrganisation&service=page" in url:
            return ORG_LIST
        if "sp=Snextpage" in url:
            return LIST_P2
        if "sp=Sdetail" in url:
            return DETAIL
        if "service=direct" in url:
            if self.stale_once:
                self.stale_once = False
                raise gepnic.StaleSession(url)
            return LIST_P1
        raise AssertionError("unexpected url " + url)


def test_full_flow():
    fake = FakePortal()
    src = {"slug": "t", "base_url": BASE, "fetch_details": True, "max_detail_fetches": 10, "delay_seconds": 0}
    store = {}
    with mock.patch.object(PortalSession, "__init__", lambda self, *a, **k: None), \
         mock.patch.object(PortalSession, "get", side_effect=fake.get, autospec=True), \
         mock.patch.object(PortalSession, "restart", lambda self: None):
        PortalSession.requests_made = 0
        st = gepnic.crawl_source(src, store, limit_orgs=1)
    assert st["orgs"] == 1
    # page1 (2 rows) + page2 (2 rows) for the single org, after one stale retry
    assert st["listed"] == 4, st
    assert st["new"] == 4
    assert st["details_fetched"] == 4
    rec = store["2026_KSRTC_812400_1"]
    assert rec["org"] == "Agency for Development of Aquaculture Kerala"
    assert rec["category"] == "Goods" and rec["value"] == 35000000.0
    assert rec["active"] is True and rec["details_fetched_at"]
    assert st["ok"] is True


def test_deactivation():
    fake = FakePortal()
    fake.stale_once = False
    src = {"slug": "t", "base_url": BASE, "fetch_details": False, "delay_seconds": 0}
    store = {"OLD_1": {"id": "OLD_1", "source": "t", "active": True, "last_seen": "x"}}
    with mock.patch.object(PortalSession, "__init__", lambda self, *a, **k: None), \
         mock.patch.object(PortalSession, "get", side_effect=fake.get, autospec=True), \
         mock.patch.object(PortalSession, "restart", lambda self: None):
        PortalSession.requests_made = 0
        gepnic.crawl_source(src, store)  # full run (no limit) -> may deactivate
    assert store["OLD_1"]["active"] is False
