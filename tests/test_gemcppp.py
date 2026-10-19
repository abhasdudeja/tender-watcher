"""GeM CPPP lists (central and state): parsers on trimmed real pages, and crawl flows against a fake site (no network)."""
from pathlib import Path
from unittest import mock

import pytest

from crawler import engine, gemcppp as G, run
from crawler.http import PortalSession, Throttled

FIX = Path(__file__).parent / "fixtures" / "gemcppp"
read = lambda name: (FIX / name).read_text(encoding="utf-8")  # noqa: E731
BASE = "https://gem.gov.in/cppp"
SRC = {"slug": "gemcppp", "name": "GeM CPPP", "type": "gemcppp", "base_url": BASE, "delay_seconds": 0}
RAIL = "Ministry of Railways"
HEAD = ["Bid Submission Closing Date", "Tender Opening Date", "e-Published Date", "Title/Ref.No./Tender Id",
        "Organisation Name", "Corrigendum", "GeM Availability Report Id", "Download"]


def page_html(rows, total):
    """A list page like the site's: the entry count and a table with the real column headings."""
    body = "".join(
        "<tr>" + "".join(f"<td>{c}</td>" for c in (r.get("close", "06-November-2026 02:30:00 PM"), "14-October-2026 06:10:44 PM",
                                                      "14-October-2026 06:10:44 PM", f"{r.get('title', 'Works')} /REF-{r['id']}/{r['id']}",
                                                      r.get("org", RAIL), "N/A", "", "N/A")) + "</tr>" for r in rows)
    head = "".join(f"<th>{h}</th>" for h in HEAD)
    return f'<html><body><div>Showing 1 to {len(rows)} of {total:,} entries</div><table class="table"><tr>{head}</tr>{body}</table></body></html>'


def pool(n, org=RAIL):
    return [{"id": str(92000000 + i), "org": org} for i in range(n)]


# ---------------------------------------------------------------- parsers (real trimmed pages)
def test_dates_are_12_hour_with_month_names():
    assert G.parse_dt("06-November-2026 02:30:00 PM") == "2026-11-06T14:30"
    assert G.parse_dt("15-October-2026 10:00:00 AM") == "2026-10-15T10:00" and G.parse_dt("12-June-2026 12:15:00 AM") == "2026-06-12T00:15"
    assert G.parse_dt("soon") is None and G.parse_dt(None) is None


def test_the_combined_cell_splits_into_title_reference_and_id():
    assert G.split_cell("Provision of points at Siwan./E-Tender-L-36-26-27/92749798") == ("Provision of points at Siwan.", "E-Tender-L-36-26-27", "92749798")
    assert G.split_cell("B6-2 /ROC176/26/EMLY/B6-2/2026_TDB_879402_1")[2] == "2026_TDB_879402_1"
    assert G.split_cell("REF-9/55") == ("", "REF-9", "55")
    title, ref, tid = G.split_cell("a cell with no usable id at the end/ref/a sentence, not an id")
    assert tid.startswith("x") and G.split_cell("a cell with no usable id at the end/ref/a sentence, not an id")[2] == tid      # stable


def test_a_real_tender_id_is_the_record_id_and_a_repeating_value_gets_a_hash_id():
    assert G.row_id("92749798", "REF-1", "Org A", "2026-10-14T18:10") == "92749798"                  # railway tender number
    assert G.row_id("2026_PRD_5080689_1", "x", "Org A", None) == "2026_PRD_5080689_1"                 # GePNIC id
    assert G.row_id("348101", "GMRC/1", "Gujarat Metro", None) == "348101"                              # 6 digits: kept (matches other portals)
    a = G.row_id("NIL", "2026-27", "Org A", "2026-10-14T18:10")
    b = G.row_id("NIL", "2026-27", "Org B", "2026-10-14T18:10")
    assert a.startswith("g") and b.startswith("g") and a != b                                          # different tenders, different ids
    assert a == G.row_id("NIL", "2026-27", "Org A", "2026-10-14T18:10")                                  # stable between crawls
    assert G.row_id("258", "ET-16", "AIIMS", "2026-09-01") != G.row_id("258", "ET-17", "AIIMS", "2026-09-01")
    assert G.row_id("2026_BL_32790", "005", "Org", None).startswith("g")                                # not a full GePNIC id


def test_tenders_that_share_a_placeholder_id_are_all_kept():
    rows = [{"id": "NIL", "org": "Org A", "title": "First"}, {"id": "NIL", "org": "Org A", "title": "Second"},
            {"id": "NIL", "org": "Org B", "title": "Third"}]
    fake, store = FakeGem(rows), {}
    st = crawl(fake, store)
    assert st["listed"] == 3 and len(store) == 3


def test_the_central_page_is_read_into_rows():
    total, rows = G.parse_page(read("central_p1.html"))
    assert total == 23779 and len(rows) == 10
    first = rows[0]
    assert first["id"] == "92749798" and first["ref"] == "E-Tender-L-36-26-27" and first["department"] == RAIL
    assert first["title"] == "Provision of Pre-cooling cum Battery charging Points at Siwan & Mau stations."
    assert first["pub"] == "2026-10-14T18:10" and first["open"] == "2026-10-14T18:10" and first["close"] == "2026-11-06T14:30"
    assert first["corrigendum"] is None and first["gem_report"] is None
    assert first["link"] == "https://www.ireps.gov.in/ireps/works/pdfdocs/112026/92749798/viewNitPdf_5584942.pdf"   # kept whole: a notice PDF
    assert {r["department"] for r in rows} == {RAIL} and len({r["id"] for r in rows}) == 10


def test_the_last_central_page_has_other_organisations():
    total, rows = G.parse_page(read("central_last.html"))
    assert len(rows) == 9 and {r["department"] for r in rows} == {"NTPC Limited", "Directorate of Purchase and Stores, DAE"}


def test_the_state_page_carries_gepnic_ids_and_no_report_columns():
    total, rows = G.parse_page(read("state_p1.html"))
    assert total == 4918 and len(rows) == 10
    assert rows[0]["id"] == "2026_PRD_5080689_1" and rows[0]["department"] == "PANCHAYAT AND RURAL DEVELOPMENT"
    assert all(r["id"].startswith("2026_") for r in rows)
    assert rows[0]["link"] == "https://tenders.wb.gov.in/nicgep/app"                       # GePNIC detail address cut back to the portal
    assert all(r["link"].endswith("/nicgep/app") for r in rows)


def test_a_page_without_the_table_has_no_rows():
    assert G.parse_page("<html><body>Showing 1 to 10 of 5 entries</body></html>") == (5, [])
    assert G.parse_page("<html></html>") == (None, [])


# ---------------------------------------------------------------- crawl flows against a fake site
class FakeGem:
    def __init__(self, rows, total=None):
        self.rows, self.total, self.calls, self.fail_on = rows, total if total is not None else len(rows), [], {}

    def get(self, _sess, url, _allow_stale=False, label=""):
        self.calls.append(url)
        n = 1 if url == BASE else int(url.rsplit("/", 1)[1])
        if self.fail_on.get(n):
            self.fail_on[n] -= 1
            raise Throttled("HTTP 429")
        return page_html(self.rows[(n - 1) * G.PAGE_SIZE:n * G.PAGE_SIZE], self.total)


def crawl(fake, store, **kw):
    with mock.patch.object(PortalSession, "__init__", lambda self, *a, **k: None), \
            mock.patch.object(PortalSession, "get", side_effect=fake.get, autospec=True):
        PortalSession.requests_made = 0
        return G.crawl_source(kw.pop("src", SRC), store, **kw)


def test_every_page_is_read_and_the_tenders_stored_without_detail_requests():
    fake, store = FakeGem(pool(25)), {}
    st = crawl(fake, store)
    assert fake.calls == [BASE, BASE + "/2", BASE + "/3"]
    assert st["ok"] is True and st["listed"] == 25 and st["new"] == 25 and st["orgs"] == 1 and st["details_fetched"] == 0
    rec = store["92000003"]
    assert rec["org"] == RAIL and rec["close"] == "2026-11-06T14:30" and rec["active"] is True and rec["status"] == "active"
    assert "details_fetched_at" not in rec


def test_max_pages_limits_the_pass():
    fake, store = FakeGem(pool(60)), {}
    st = crawl(fake, store, src=dict(SRC, max_pages=2))
    assert len(fake.calls) == 2 and st["listed"] == 20 and st["ok"] is True


def test_a_row_listed_on_two_pages_is_stored_once():
    rows = pool(25)
    rows[10] = dict(rows[9])                                                   # the list shifted: a tender shows twice
    fake, store = FakeGem(rows), {}
    st = crawl(fake, store)
    assert st["ok"] is True and len(store) == 24


def test_a_pass_stopped_by_throttling_resumes_where_it_stopped():
    sess = mock.Mock(requests_made=0)
    fake = FakeGem(pool(35))
    fake.fail_on = {3: 1}
    portal = G.GemCpppPortal(sess, BASE)
    sess.get.side_effect = lambda url, label="": fake.get(sess, url, label=label)
    with pytest.raises(Throttled):
        portal.list_orgs()
    assert fake.calls == [BASE, BASE + "/2", BASE + "/3"]
    orgs = portal.list_orgs()                                                  # the engine calls it again after its pause
    assert fake.calls[3:] == [BASE + "/3", BASE + "/4"]                         # pages 1 and 2 were not read again
    assert orgs == [{"name": RAIL, "count": 35, "url": BASE}]


def test_a_new_pass_starts_from_the_first_page_again():
    sess = mock.Mock(requests_made=0)
    fake = FakeGem(pool(15))
    sess.get.side_effect = lambda url, label="": fake.get(sess, url, label=label)
    portal = G.GemCpppPortal(sess, BASE)
    portal.list_orgs()
    portal.list_orgs()
    assert fake.calls == [BASE, BASE + "/2", BASE, BASE + "/2"]


def test_a_list_far_shorter_than_announced_fails_the_run():
    st = crawl(FakeGem(pool(20), total=500), {})
    assert st["ok"] is False and any("returned only" in e or "shorter" in e or "only" in e for e in st["errors"])


def test_a_page_without_a_count_fails_the_run_and_deactivates_nothing():
    store = {"1": {"id": "1", "source": "gemcppp", "org": RAIL, "active": True, "close": "2099-01-01T10:00"}}

    class Broken(FakeGem):
        def get(self, _sess, url, _allow_stale=False, label=""):
            return "<html>maintenance</html>"

    st = crawl(Broken([]), store)
    assert st["ok"] is False and any("entry count" in e for e in st["errors"]) and store["1"]["active"] is True


def test_a_tender_that_leaves_the_list_is_kept_and_marked_after_a_full_pass():
    store = {}
    crawl(FakeGem(pool(15)), store)
    st = crawl(FakeGem(pool(15)[:10]), store)                                  # five tenders closed
    assert st["ok"] is True and sum(1 for r in store.values() if not r["active"]) == 5


def test_a_changed_deadline_is_picked_up():
    rows, store = pool(10), {}
    crawl(FakeGem(rows), store)
    for r in store.values():
        r["last_seen"] = "2020-01-01T00:00:00Z"
    moved = [dict(rows[0], close="20-November-2026 05:00:00 PM")] + rows[1:]
    st = crawl(FakeGem(moved), store)
    assert store["92000000"]["close"] == "2026-11-20T17:00" and st["changed"] == 0 and st["ok"] is True


def test_the_runner_dispatches_gemcppp_sources_to_this_crawler():
    assert run.CRAWLERS["gemcppp"] is G.crawl_source
    with pytest.raises(engine.DetailFailed):
        G.GemCpppPortal(mock.Mock(), BASE).fetch_detail({}, {})


def test_a_tender_link_is_kept_unless_it_is_a_session_bound_gepnic_address():
    assert G.portal_link("https://www.ireps.gov.in/ireps/supply/pdfdocs/102026/1/viewNitPdf_5.pdf").endswith("viewNitPdf_5.pdf")
    assert G.portal_link("http://www.ntpctender.com/") == "http://www.ntpctender.com/"
    assert G.portal_link("https://x.gov.in/nicgep/app?page=FrontEndTenderDetailsExternal&service=page&tnid=77") == "https://x.gov.in/nicgep/app"
    assert G.portal_link("https://x.gov.in/eprocure/app?TNID=1&a=2") == "https://x.gov.in/eprocure/app"
    for bad in (None, "", "javascript:alert(1)", "ftp://x/y", "/relative/path", "https://x.in/" + "a" * 300):
        assert G.portal_link(bad) is None
