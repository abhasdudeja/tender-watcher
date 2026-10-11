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


def test_progress_counter():
    from crawler.progress import format_line

    fake = FakePortal()
    src = {"slug": "t", "name": "T", "base_url": BASE, "fetch_details": True, "max_detail_fetches": 10, "delay_seconds": 0}
    snaps = []
    with mock.patch.object(PortalSession, "__init__", lambda self, *a, **k: None), \
         mock.patch.object(PortalSession, "get", side_effect=fake.get, autospec=True), \
         mock.patch.object(PortalSession, "restart", lambda self: None):
        PortalSession.requests_made = 0
        gepnic.crawl_source(src, {}, limit_orgs=1, progress=snaps.append)
    assert snaps, "progress sink never called"
    pcts = [s["pct"] for s in snaps]
    assert pcts == sorted(pcts), "progress must never go backwards"
    last = snaps[-1]
    assert last["state"] == "finished" and last["pct"] == 100.0 and last["eta_s"] == 0
    assert last["orgs_done"] == last["orgs_total"] == 1
    assert last["tenders_total"] == 6          # the portal's own count for the first org
    assert last["listed"] == 4 and last["details_fetched"] == 4
    assert any(s["phase"] == "details" for s in snaps)
    assert "100.0%" in format_line(last) and "(done)" in format_line(last)


def test_progress_snapshot_math():
    from crawler.progress import Progress

    p = Progress("s", "S", orgs_total=2, tenders_total=100)
    p.start_org("A", 80)
    p.update(org_frac=0.5, force=True)
    assert p.snapshot()["tenders_done"] == 40 and p.snapshot()["pct"] == 40.0
    p.finish_org()
    p.start_org("B", 20)
    assert p.snapshot()["tenders_done"] == 80 and p.snapshot()["orgs_done"] == 1


# ---------------------------------------------------------------- refresh window & retention
OLD = "2020-01-01T00:00:00Z"
ORG1 = "Agency for Development of Aquaculture Kerala"   # portal says 6 tenders in the fixture


def _now_z():
    return gepnic._now()


def _recs(n, org=ORG1, last_seen=None, fetched=None):
    out = {}
    for i in range(n):
        r = {"id": f"S_{i}", "source": "t", "org": org, "active": True, "status": "active",
             "last_seen": last_seen or _now_z(), "title": "x"}
        if fetched is not False:
            r["details_fetched_at"] = fetched or _now_z()
        out[r["id"]] = r
    return out


def _crawl(store, **kw):
    fake = FakePortal()
    fake.stale_once = False
    src = {"slug": "t", "name": "T", "base_url": BASE, "fetch_details": True,
           "max_detail_fetches": 50, "delay_seconds": 0}
    with mock.patch.object(PortalSession, "__init__", lambda self, *a, **k: None), \
         mock.patch.object(PortalSession, "get", side_effect=fake.get, autospec=True), \
         mock.patch.object(PortalSession, "restart", lambda self: None):
        PortalSession.requests_made = 0
        st = gepnic.crawl_source(src, store, **kw)
    return st, fake.calls


def _listing_calls(calls):
    return [c for c in calls if "service=direct" in c]


def test_fresh_org_with_same_count_is_skipped():
    store = _recs(6)                          # portal count (6) == stored active count, all fresh
    st, calls = _crawl(store, limit_orgs=1)
    assert st["orgs_skipped"] == 1 and _listing_calls(calls) == []
    assert st["details_fetched"] == 0 and st["ok"] is True


def test_count_mismatch_forces_relist_even_if_fresh():
    store = _recs(5)                          # portal says 6, we hold 5 -> something changed
    st, calls = _crawl(store, limit_orgs=1)
    assert st["orgs_skipped"] == 0 and _listing_calls(calls)


def test_old_listing_forces_relist():
    st, calls = _crawl(_recs(6, last_seen=OLD), limit_orgs=1)
    assert st["orgs_skipped"] == 0 and _listing_calls(calls)


def test_details_older_than_24h_are_refreshed_and_fresh_ones_skipped():
    store = _recs(6, last_seen=OLD, fetched=OLD)
    store["2026_KSRTC_812400_1"] = {"id": "2026_KSRTC_812400_1", "source": "t", "org": ORG1, "active": True,
                                    "last_seen": OLD, "details_fetched_at": _now_z(), "category": "KEEP"}
    st, calls = _crawl(store, limit_orgs=1)
    # listing returns LIST_P1 + LIST_P2 = 4 ids; KSRTC_812400 was fetched just now -> not refetched
    assert st["details_fetched"] == 3
    assert store["2026_KSRTC_812400_1"]["category"] == "KEEP"
    assert store["2026_LSGD_812345_1"]["details_fetched_at"] >= store["2026_KSRTC_812400_1"]["details_fetched_at"]


def test_force_refresh_ignores_the_window():
    st, calls = _crawl(_recs(6), limit_orgs=1, force_refresh=True)
    assert st["orgs_skipped"] == 0 and _listing_calls(calls)


def test_missing_tenders_are_kept_and_marked_closed_or_removed():
    store = {
        "GONE_PAST": {"id": "GONE_PAST", "source": "t", "active": True, "close": "2020-01-01T10:00"},
        "GONE_EARLY": {"id": "GONE_EARLY", "source": "t", "active": True, "close": "2999-01-01T10:00"},
        "GONE_NODATE": {"id": "GONE_NODATE", "source": "t", "active": True},
    }
    _crawl(store)                              # full run: no limit_orgs / only_orgs
    assert store["GONE_PAST"]["active"] is False and store["GONE_PAST"]["status"] == "closed"
    assert store["GONE_EARLY"]["active"] is False and store["GONE_EARLY"]["status"] == "removed"
    assert store["GONE_NODATE"]["status"] == "removed"
    assert all(store[k]["inactive_since"] for k in ("GONE_PAST", "GONE_EARLY", "GONE_NODATE"))
    assert len(store) >= 3                     # nothing deleted


def test_skipped_org_is_not_deactivated_in_a_full_run():
    store = _recs(6)                           # fresh + same count -> ORG1 is skipped, not listed
    st, _ = _crawl(store)                      # full run, so the deactivation pass runs
    assert st["orgs_skipped"] == 1 and st["ok"] is True
    assert all(store[f"S_{i}"]["active"] and store[f"S_{i}"]["status"] == "active" for i in range(6))


def test_reappearing_tender_is_reactivated():
    store = {"2026_KSRTC_812400_1": {"id": "2026_KSRTC_812400_1", "source": "t", "org": ORG1, "active": False,
                                     "status": "closed", "inactive_since": OLD, "last_seen": OLD}}
    _crawl(store, limit_orgs=1)                # 0 active stored != 6 on the portal -> re-listed
    rec = store["2026_KSRTC_812400_1"]
    assert rec["active"] is True and rec["status"] == "active" and "inactive_since" not in rec


def test_progress_eta_ignores_skipped_orgs():
    from crawler.progress import Progress

    p = Progress("s", "S", orgs_total=2, tenders_total=1000)
    p.start_org("A", 900)
    p.finish_org(skipped=True)                 # instantly "done": must not make the rate look huge
    p.start_org("B", 100)
    snap = p.snapshot()
    assert snap["tenders_done"] == 900 and snap["rate_per_min"] == 0.0 and snap["eta_s"] is None


# ---------------------------------------------------------------- change-driven detail refresh
from datetime import datetime, timedelta, timezone  # noqa: E402


def _ago(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _recs_from_listing(fetched, **over):
    """Stored records identical to what the fake portal lists for ORG1 (4 tenders over 2 pages)."""
    rows = gepnic.parse_tender_list(LIST_P1, BASE)["rows"] + gepnic.parse_tender_list(LIST_P2, BASE)["rows"]
    out = {}
    for r in rows:
        rec = {k: r[k] for k in ("id", "ref", "title", "chain", "pub", "close", "open")}
        rec.update(source="t", org=ORG1, active=True, status="active", last_seen=OLD,
                   details_fetched_at=fetched, category="KEEP")
        rec.update(over)
        out[rec["id"]] = rec
    return out


def test_unchanged_tenders_are_not_refetched_just_because_48h_passed():
    store = _recs_from_listing(fetched=_ago(days=3))          # details 3 days old, listing old -> org is re-listed
    st, calls = _crawl(store, limit_orgs=1)
    assert st["orgs_skipped"] == 0 and _listing_calls(calls)  # re-listed (stale listing / count mismatch)...
    assert st["details_fetched"] == 0 and st["changed"] == 0  # ...but nothing changed, so no detail requests
    assert all(r["category"] == "KEEP" for r in store.values())


def test_changed_closing_date_triggers_a_detail_refetch():
    store = _recs_from_listing(fetched=_ago(days=3))
    victim = "2026_KSRTC_812400_1"
    store[victim]["close"] = "2026-10-01T10:00"               # portal now says 2026-10-20T15:00 (extended)
    st, _ = _crawl(store, limit_orgs=1)
    assert st["changed"] == 1 and st["details_fetched"] == 1
    assert store[victim]["category"] == "Goods" and "needs_details" not in store[victim]
    assert store[victim]["close"] == "2026-10-20T15:00"
    assert store["2026_LSGD_812345_1"]["category"] == "KEEP"   # untouched neighbour


def test_changed_flag_survives_when_the_detail_cap_stops_the_fetch():
    store = _recs_from_listing(fetched=_ago(days=3))
    store["2026_KSRTC_812400_1"]["title"] = "older title"
    fake = FakePortal()
    fake.stale_once = False
    src = {"slug": "t", "name": "T", "base_url": BASE, "fetch_details": True, "max_detail_fetches": 0, "delay_seconds": 0}
    with mock.patch.object(PortalSession, "__init__", lambda self, *a, **k: None), \
         mock.patch.object(PortalSession, "get", side_effect=fake.get, autospec=True), \
         mock.patch.object(PortalSession, "restart", lambda self: None):
        PortalSession.requests_made = 0
        gepnic.crawl_source(src, store, limit_orgs=1)
    assert store["2026_KSRTC_812400_1"]["needs_details"] is True   # still queued for the next run
    assert store["2026_KSRTC_812400_1"]["title"] == "Supply of 2 nos electric buses"


def test_very_old_details_are_refreshed_as_a_safety_net_unless_disabled():
    store = _recs_from_listing(fetched=_ago(days=30))
    st, _ = _crawl(store, limit_orgs=1)
    assert st["details_fetched"] == 4                          # all older than detail_max_age_days (14)
    store = _recs_from_listing(fetched=_ago(days=30))
    fake = FakePortal()
    fake.stale_once = False
    src = {"slug": "t", "name": "T", "base_url": BASE, "fetch_details": True, "max_detail_fetches": 50,
           "delay_seconds": 0, "detail_max_age_days": 0}
    with mock.patch.object(PortalSession, "__init__", lambda self, *a, **k: None), \
         mock.patch.object(PortalSession, "get", side_effect=fake.get, autospec=True), \
         mock.patch.object(PortalSession, "restart", lambda self: None):
        PortalSession.requests_made = 0
        st = gepnic.crawl_source(src, store, limit_orgs=1)
    assert st["details_fetched"] == 0                          # 0 disables the safety net


# ---------------------------------------------------------------- detail-page failures (ported from the original design)
class DetailVariantPortal(FakePortal):
    def __init__(self, detail_html):
        super().__init__()
        self.stale_once = False
        self.detail_html = detail_html

    def get(self, _sess, url, _allow_stale=False, label=""):
        if "sp=Sdetail" in url and "sp=Snextpage" not in url:
            self.calls.append(url)
            return self.detail_html
        return super().get(_sess, url, _allow_stale, label)


def _crawl_with(fake, store, **kw):
    src = {"slug": "t", "name": "T", "base_url": BASE, "fetch_details": True, "max_detail_fetches": 50, "delay_seconds": 0}
    with mock.patch.object(PortalSession, "__init__", lambda self, *a, **k: None), \
         mock.patch.object(PortalSession, "get", side_effect=fake.get, autospec=True), \
         mock.patch.object(PortalSession, "restart", lambda self: None):
        PortalSession.requests_made = 0
        return gepnic.crawl_source(src, store, **kw)


def test_empty_detail_page_is_a_failure_not_a_fetch_and_does_not_fail_the_run():
    store = {}
    st = _crawl_with(DetailVariantPortal("<html><body><p>nothing here</p></body></html>"), store, limit_orgs=1)
    assert st["details_fetched"] == 0 and st["details_failed"] == 4
    assert st["ok"] is True                                  # the listing is intact: only details failed
    assert all("details_fetched_at" not in r for r in store.values())   # so they are retried next run
    assert any("no caption/value pairs" in e for e in st["errors"])


def test_captcha_gated_detail_pages_switch_details_off_for_the_run():
    fake = DetailVariantPortal("<html><body>Please enter the CAPTCHA</body></html>")
    store = {}
    st = _crawl_with(fake, store, limit_orgs=1)
    assert st["details_fetched"] == 0 and st["details_failed"] == 1 and st["ok"] is True
    assert len([c for c in fake.calls if "sp=Sdetail" in c]) == 1       # gave up after the first wall
    assert any("CAPTCHA" in e for e in st["errors"])


def test_deactivated_tender_keeps_the_last_time_it_was_listed():
    store = {"GONE": {"id": "GONE", "source": "t", "active": True, "last_seen": "2026-10-01T00:00:00Z"}}
    _crawl(store)
    assert store["GONE"]["active"] is False and store["GONE"]["last_seen_active"] == "2026-10-01T00:00:00Z"


def test_stop_flag_winds_the_crawl_down_without_deactivating_anything():
    import threading

    stop = threading.Event()
    stop.set()
    store = {"KEEP": {"id": "KEEP", "source": "t", "active": True}}
    st, calls = _crawl(store, stop=stop)
    assert st["ok"] is False and store["KEEP"]["active"] is True and _listing_calls(calls) == []
