"""Chhattisgarh CHEPS: parsers on trimmed real pages, and crawl flows against a fake portal (no network)."""
from pathlib import Path
from unittest import mock

import pytest

from crawler import cheps as C, engine, run
from crawler.http import PortalSession

FIX = Path(__file__).parent / "fixtures" / "cheps"
BASE = "https://eproc.cgstate.gov.in"
HOME, LIVE = (FIX / "home.html").read_text(encoding="utf-8"), (FIX / "live.html").read_text(encoding="utf-8")
SRC = {"slug": "chhattisgarh", "name": "Chhattisgarh", "type": "cheps", "base_url": BASE, "delay_seconds": 0}
BHORAMDEV = "Bhoramdev Sahakari Sugar Production Factory-Kabirdham(BSSPFK)"
CGHB = "Chhattisgarh Housing Board (CGHB)"


def test_dates_are_12_hour_wall_clock_times():
    assert C.parse_dt("2026-10-12 8:00:00 PM") == "2026-10-12T20:00"
    assert C.parse_dt("2026-10-19 12:00:00 PM") == "2026-10-19T12:00" and C.parse_dt("2026-10-19 12:30:00 AM") == "2026-10-19T00:30"
    assert C.parse_dt("soon") is None and C.parse_dt("2026-13-40 1:00:00 PM") is None


def test_the_load_live_data_form_is_read_from_the_home_page():
    form = C.parse_form(HOME)
    assert form["loadAlldata"] == "Y" and form["OWASP_CSRFTOKEN"] and form["pkiEnabledOrg"] == "false"
    assert C.parse_form("<html></html>") == {}


def test_live_rows_are_grouped_under_their_department_heading():
    rows = C.parse_live(LIVE)
    assert len(rows) == 10 and len({r["id"] for r in rows}) == 10
    assert {r["department"] for r in rows} == {BHORAMDEV, CGHB, "Chhattisgarh Infotech Promotion Society (CHiPS)",
                                               "Chhattisgarh Road Development Corporation Limited(CGRDC)"}
    first = rows[0]
    assert first["id"] == "201174" and first["ref"] == "201174" and first["department"] == BHORAMDEV
    assert first["pub"] == "2026-10-12T20:00" and first["close"] == "2026-10-19T12:00" and first["detail_ref"] == "201174,1"
    assert first["value"] is None and first["corrigendum"] == "No"
    assert first["title"] == "Manufacturing Section (Only OEM & Authorized Dealer"          # the tooltip text is not repeated


def test_a_stated_amount_and_a_corrigendum_are_read():
    rows = {r["id"]: r for r in C.parse_live(LIVE)}
    assert rows["200663"]["value"] == 3616000.0 and rows["200663"]["department"] == CGHB
    assert {r["corrigendum"] for r in rows.values()} == {"Yes", "No"}         # the sample has both kinds


class FakeCheps:
    def __init__(self, live=LIVE, home=HOME):
        self.live, self.home, self.posts, self.gets = live, home, [], 0

    def get(self, _sess, url, _allow_stale=False, label=""):
        assert url == BASE + C.HOME
        self.gets += 1
        return self.home

    def post(self, _sess, url, data=None, headers=None, label=""):
        assert url == BASE + C.HOME
        self.posts.append(dict(data))
        return self.live


def crawl(fake, store, **kw):
    with mock.patch.object(PortalSession, "__init__", lambda self, *a, **k: None), \
            mock.patch.object(PortalSession, "get", side_effect=fake.get, autospec=True), \
            mock.patch.object(PortalSession, "post", side_effect=fake.post, autospec=True):
        PortalSession.requests_made = 0
        return C.crawl_source(kw.pop("src", SRC), store, **kw)


def test_crawl_submits_the_load_live_data_form_once_and_stores_every_tender():
    fake, store = FakeCheps(), {}
    st = crawl(fake, store)
    assert st["ok"] is True and st["orgs"] == 4 and st["listed"] == 10 and st["new"] == 10 and st["details_fetched"] == 0
    assert fake.gets == 1 and len(fake.posts) == 1
    assert fake.posts[0]["loadAlldata"] == "Y" and fake.posts[0]["OWASP_CSRFTOKEN"]            # exactly the button's own form
    rec = store["200663"]
    assert rec["org"] == CGHB and rec["value"] == 3616000.0 and rec["close"] == "2026-10-21T17:30" and rec["active"] is True
    assert "details_fetched_at" not in rec


def test_an_unchanged_department_is_skipped_on_the_next_run():
    store = {}
    crawl(FakeCheps(), store)
    st = crawl(FakeCheps(), store)
    assert st["orgs_skipped"] == 4


def test_a_tender_that_leaves_the_list_is_kept_and_marked_after_a_full_run():
    store = {"OLD": {"id": "OLD", "source": "chhattisgarh", "org": CGHB, "active": True, "close": "2020-01-01T10:00",
                     "last_seen": "2020-01-01T00:00:00Z"}}
    st = crawl(FakeCheps(), store)
    assert st["ok"] is True and store["OLD"]["active"] is False and store["OLD"]["status"] == "closed"


def test_a_home_page_without_the_form_fails_the_run_clearly():
    st = crawl(FakeCheps(home="<html>maintenance</html>"), {})
    assert st["ok"] is False and any("LOAD LIVE DATA form" in e for e in st["errors"])


def test_an_empty_result_is_an_error_not_a_reason_to_close_everything():
    store = {"A": {"id": "A", "source": "chhattisgarh", "org": CGHB, "active": True, "close": "2999-01-01T10:00",
                   "last_seen": "2020-01-01T00:00:00Z"}}
    st = crawl(FakeCheps(live="<html><body>nothing</body></html>"), store)
    assert st["ok"] is False and any("no tenders" in e for e in st["errors"]) and store["A"]["active"] is True


def test_the_runner_dispatches_cheps_sources_to_this_crawler():
    assert run.CRAWLERS["cheps"] is C.crawl_source
    assert engine.DetailFailed
