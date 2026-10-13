"""EPSV2 (Bihar) crawler: parsers on trimmed real responses, the guest-token handling, and crawl flows
against a fake portal (no network)."""
import base64
import json
import time
from pathlib import Path
from unittest import mock

import pytest
from curl_cffi.requests.exceptions import RequestException

from crawler import engine, epsv2 as E
from crawler.http import PortalSession

FIX = Path(__file__).parent / "fixtures" / "epsv2"
PAGE_URL = "https://eproc2.bihar.gov.in/EPSV2Web/openarea/tenderListingPage.action"
API = "https://eproc2.bihar.gov.in/EPSV2Web"
LIST = (FIX / "list.json").read_text(encoding="utf-8")
DEPTS = (FIX / "depts.json").read_text(encoding="utf-8")
MASTERS = (FIX / "masters.json").read_text(encoding="utf-8")
PREVIEW = (FIX / "preview_140352.json").read_text(encoding="utf-8")
SRC = {"slug": "bihar", "name": "Bihar", "type": "epsv2", "base_url": PAGE_URL, "fetch_details": True,
       "max_detail_fetches": 500, "delay_seconds": 0}
URBAN, MINES = "Urban Development and Housing Department", "MINES & Geology Department"


def make_token(expires_in=900.0):
    b64 = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")  # noqa: E731
    return "Bearer " + ".".join([b64({"alg": "HS256"}), b64({"exp": int(time.time() + expires_in), "iss": "SYSTEM"}), "sig"])


def page_html(token):
    return (f'<html><body><script>var orgId = \'538\';</script>'
            f'<input type="hidden" name="Authorization" id="Authorization" value="{token}"/></body></html>')


# ---------------------------------------------------------------- parsers (real trimmed responses)
def test_dates_are_shown_as_ist_wall_clock():
    assert E.ms_to_ist(1791883800000) == "2026-10-13T15:00"          # the "End Date" the site shows for tender 140157
    assert E.ms_to_ist("1790053200000") == "2026-09-22T10:30"
    assert E.ms_to_ist(None) is None and E.ms_to_ist("soon") is None


def test_token_and_org_id_come_from_the_page():
    tok = make_token()
    html = page_html(tok)
    assert E.token_from_page(html) == tok and E.org_id_from_page(html) == 538
    assert E.token_from_page("<html></html>") is None and E.org_id_from_page("<html></html>") is None
    assert abs(E.token_expiry(tok) - (time.time() + 900)) < 5
    assert E.token_expiry("garbage") is None


def test_parse_tender_list_maps_the_public_id_names_and_ist_dates():
    depts, masters = E.parse_departments(DEPTS), E.parse_masters(MASTERS)
    assert depts[800] == URBAN and masters["types"][101] == "Open Tender" and masters["cats"][1557] == "Civil"
    rows = E.parse_tender_list(LIST, depts, masters)
    assert len(rows) == 5
    first = rows[0]
    assert first["id"] == "140157" and first["detail_ref"] == "140352"          # site id vs internal id
    assert first["ref"] == "02/2026-27 Group-1" and first["chain"] == URBAN
    assert first["close"] == "2026-10-13T15:00" and first["pub"] and first["open"]
    assert first["tender_type"] == "Open Tender" and first["category"]
    assert rows[1]["title"] == "Construction of drain in ward 4, Nagar Panchayat"     # line breaks / runs of spaces collapsed
    assert {r["chain"] for r in rows} == {URBAN, MINES}


def test_an_unknown_department_id_still_gets_a_name():
    rows = E.parse_tender_list(LIST, {}, E.parse_masters(MASTERS))
    assert rows[0]["chain"].startswith("Department ")


def test_duplicate_tenders_in_the_list_are_kept_once():
    items = json.loads(LIST)
    rows = E.parse_tender_list(json.dumps(items + [items[0]]), E.parse_departments(DEPTS), E.parse_masters(MASTERS))
    assert len(rows) == 5


def test_parse_preview_extracts_value_emd_dates_and_documents_but_no_people():
    got = E.parse_preview(PREVIEW)
    assert got["value"] == 11047900.0 and got["emd"] == 221000.0
    d = got["details"]
    assert d["Tender ID"] == "140157" and d["Tender Type"] == "Open Tender" and d["Procurement Category"] == "ITMRATE"
    assert d["Bid Submission Due Date"] == "2026-10-13T15:00" and d["Bid Open Date"]
    assert d["EMD"] == "221000" and d["Tender Fee"] == "10000" and d["Tender Processing Fee"] == "5900"
    assert d["Pre-Bid Discussion Type"] == "OFFLINE" and d["Offer Validity (days)"] == "180"
    assert got["documents"] and "mrf 01.pdf" in got["documents"]
    assert got["authority"] == "Executive Officer"                       # the designation, not "Test Officer"
    assert "Test Officer" not in json.dumps(got)


def test_parse_preview_without_a_tender_is_empty():
    assert E.parse_preview(json.dumps({"message": "nope"})) == {}
    assert E.parse_preview("[]") == {}
    with pytest.raises(ValueError):
        E.parse_preview("<html>Internal error</html>")


# ---------------------------------------------------------------- crawl flows
class FakeBihar:
    """Listing page (token) + the four JSON calls, speaking the real protocol."""

    def __init__(self, preview=PREVIEW, token_ttl=900.0, list_text=LIST):
        self.preview, self.token_ttl, self.list_text = preview, token_ttl, list_text
        self.page_calls, self.detail_calls, self.list_calls, self.auth_seen = 0, [], 0, []
        self.valid = None

    def get(self, _sess, url, _allow_stale=False, label=""):
        assert url == PAGE_URL, url
        self.page_calls += 1
        self.valid = make_token(self.token_ttl)
        return page_html(self.valid)

    def post(self, _sess, url, data=None, headers=None, label=""):
        assert url.startswith(API), url
        path = url[len(API):]
        self.auth_seen.append(headers["Authorization"])
        if headers["Authorization"] != self.valid:
            raise RequestException("HTTP 500")                       # what an expired or invalid token gets
        if path.startswith("/rest/organization/loadAllDeptWithParent"):
            assert path.endswith("orgId=538")
            return DEPTS
        if path == "/rest/master/showRfqCategoryRfqTypeProcCatBidPartList":
            return MASTERS
        if path == "/rest/openarea/getTenderList":
            self.list_calls += 1
            assert json.loads(data) == {"orgId": 538}
            return self.list_text
        if path.startswith("/rest/tender/previewTenderByTenderId?tenderId="):
            self.detail_calls.append(path.rsplit("=", 1)[1])
            return self.preview
        raise AssertionError(url)


def crawl(fake, store, **kw):
    with mock.patch.object(PortalSession, "__init__", lambda self, *a, **k: None), \
         mock.patch.object(PortalSession, "get", side_effect=fake.get, autospec=True), \
         mock.patch.object(PortalSession, "post", side_effect=fake.post, autospec=True):
        PortalSession.requests_made = 0
        return E.crawl_source(kw.pop("src", SRC), store, **kw)


def test_crawl_lists_every_department_from_one_call_and_fetches_details():
    fake, store = FakeBihar(), {}
    st = crawl(fake, store)
    assert st["ok"] is True and st["orgs"] == 2 and st["listed"] == 5 and st["new"] == 5 and st["details_fetched"] == 5
    assert fake.list_calls == 1 and sorted(fake.detail_calls) == sorted(str(t["currenttenderid"]) for t in json.loads(LIST))
    rec = store["140157"]
    assert rec["org"] == URBAN and rec["chain"] == URBAN and rec["ref"] == "02/2026-27 Group-1"
    assert rec["close"] == "2026-10-13T15:00" and rec["value"] == 11047900.0 and rec["emd"] == 221000.0
    assert rec["tender_type"] == "Open Tender" and rec["active"] is True and rec["status"] == "active" and rec["details_fetched_at"]
    assert {r["org"] for r in store.values()} == {URBAN, MINES}


def test_a_fresh_unchanged_department_is_skipped_without_detail_requests():
    fake, store = FakeBihar(), {}
    crawl(fake, store)
    fake2 = FakeBihar()
    st = crawl(fake2, store)
    assert st["orgs_skipped"] == 2 and fake2.detail_calls == [] and fake2.list_calls == 1


def test_changed_closing_date_refetches_just_that_tender():
    fake, store = FakeBihar(), {}
    crawl(fake, store)
    for r in store.values():
        r["last_seen"] = "2020-01-01T00:00:00Z"
    items = json.loads(LIST)
    items[1]["currentbidEndDate"] += 86_400_000                          # deadline extended by a day
    fake2 = FakeBihar(list_text=json.dumps(items))
    st = crawl(fake2, store)
    assert st["changed"] == 1 and st["details_fetched"] == 1 and fake2.detail_calls == [str(items[1]["currenttenderid"])]


def test_a_tender_that_leaves_the_list_is_kept_and_marked_after_a_full_run():
    store = {"OLD": {"id": "OLD", "source": "bihar", "org": URBAN, "active": True, "close": "2020-01-01T10:00",
                     "last_seen": "2020-01-01T00:00:00Z"}}
    st = crawl(FakeBihar(), store, no_details=True)
    assert st["ok"] is True and store["OLD"]["active"] is False and store["OLD"]["status"] == "closed"


def test_an_expired_token_is_replaced_before_it_is_used():
    fake, store = FakeBihar(token_ttl=100.0), {}                         # always inside the last 150 s: renew every call
    st = crawl(fake, store, only_orgs=[URBAN])
    assert st["ok"] is True and st["details_fetched"] == 3
    assert fake.page_calls > 3                                           # the page (and so the token) was fetched again and again


def test_a_http_500_from_a_stale_token_is_retried_once_with_a_new_one():
    class StaleOnce(FakeBihar):
        def __init__(self):
            super().__init__()
            self.failed = False

        def post(self, _sess, url, data=None, headers=None, label=""):
            if "previewTenderByTenderId" in url and not self.failed:
                self.failed = True
                self.valid = "Bearer rotated-on-the-server"             # the server dropped our token
                raise RequestException("HTTP 500")
            return super().post(_sess, url, data, headers, label)

    fake, store = StaleOnce(), {}
    st = crawl(fake, store, only_orgs=[MINES])
    assert st["ok"] is True and st["details_fetched"] == 2 and st["details_failed"] == 0


def test_a_response_that_is_not_a_tender_is_a_failed_detail_not_a_fetch():
    fake, store = FakeBihar(preview=json.dumps({"error": "x"})), {}
    st = crawl(fake, store, only_orgs=[MINES])
    assert st["details_fetched"] == 0 and st["details_failed"] == 2 and st["ok"] is True
    assert any("no tender in the response" in e for e in st["errors"])
    assert all("details_fetched_at" not in r for r in store.values())


def test_missing_token_fails_the_run_with_a_clear_message():
    class NoToken(FakeBihar):
        def get(self, _sess, url, _allow_stale=False, label=""):
            return "<html>maintenance</html>"

    st = crawl(NoToken(), {})
    assert st["ok"] is False and any("guest token" in e for e in st["errors"])


def test_two_workers_fetch_the_same_details():
    fake, store = FakeBihar(), {}
    st = crawl(fake, store, src=dict(SRC, detail_workers=2))
    assert st["ok"] is True and st["details_fetched"] == 5 and all(r["value"] == 11047900.0 for r in store.values())


def test_the_runner_dispatches_epsv2_sources_to_this_crawler():
    from crawler import run

    assert run.CRAWLERS["epsv2"] is E.crawl_source
    assert issubclass(engine.DetailFailed, Exception)
