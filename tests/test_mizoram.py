"""Mizoram e-Tender notice board: parser on a trimmed real API answer, and crawl flows against a fake API (no network)."""
import json
from datetime import datetime
from pathlib import Path
from unittest import mock

import pytest

from crawler import engine, mizoram as M, run
from crawler.http import PortalSession

FIX = Path(__file__).parent / "fixtures" / "mizoram"
OPEN = (FIX / "api_open.json").read_text(encoding="utf-8")          # the real answer of 15 Oct 2026: two open notices
HOME = "https://tender.mizoram.gov.in/home"
API = "https://tender.mizoram.gov.in/api/public/tenders"
SRC = {"slug": "mizoram-etender", "name": "Mizoram e-Tender", "type": "mizoram", "base_url": HOME,
       "fetch_details": False, "delay_seconds": 0}
NOW = datetime(2026, 10, 15, 13, 30, tzinfo=M.IST)                  # the fixture was taken on this day
POLICE = "Police"


def page(rows, last_page=1, total=None):
    return json.dumps({"tenders": {"current_page": 1, "last_page": last_page, "total": len(rows) if total is None else total, "data": rows}})


ROWS = json.loads(OPEN)["tenders"]["data"]


# ---------------------------------------------------------------- parsing
def test_dates_come_from_the_api_in_24_hour_form():
    assert M.parse_dt("2026-11-11 12:00:00") == "2026-11-11T12:00"
    assert M.parse_dt("2026-10-12 00:00:00") == "2026-10-12"            # a midnight issue date is just a date
    assert M.parse_dt("2026-10-12") == "2026-10-12" and M.parse_dt("2026-02-31 10:00:00") is None
    assert M.parse_dt("soon") is None and M.parse_dt(None) is None


def test_the_real_answer_is_read_into_notice_rows():
    last, rows = M.parse_tenders(OPEN)
    assert last == 1 and len(rows) == 2
    first = rows[0]
    assert first["id"] == "994" and first["ref"] == "No.1-SE of 2026-2027" and first["chain"] == POLICE
    assert first["title"] == "Security/ Training Equipment for Mizoram Police" and first["tender_type"] == "TENDER NOTICE"
    assert first["pub"] == "2026-10-12" and first["open"] == "2026-11-11T13:00" and first["close"] == "2026-11-11T12:00"
    assert first["documents"] == ["tender-no-1-se-of-2026-2027.pdf"]


def test_a_notice_without_a_description_uses_its_subject_as_the_title():
    row = dict(ROWS[0], detail=None, subject="Auction of old vehicles")
    assert M.parse_tenders(page([row]))[1][0]["title"] == "Auction of old vehicles" and M.parse_tenders(page([row]))[1][0]["tender_type"] is None


def test_something_that_is_not_the_notices_list_is_rejected():
    for bad in ("<!doctype html><html><body><div id=q-app></div></body></html>", json.dumps({"message": "Server Error"}),
                json.dumps({"tenders": {"data": "x"}})):
        with pytest.raises(ValueError):
            M.parse_tenders(bad)


# ---------------------------------------------------------------- crawl flows
class FakeApi:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, _sess, url, _allow_stale=False, label=""):
        assert url.startswith(API + "?status=open&order=latest&page=")
        self.calls.append(url)
        n = int(url.rsplit("=", 1)[1])
        return self.pages[n - 1]


def crawl(fake, store, **kw):
    with mock.patch.object(PortalSession, "__init__", lambda self, *a, **k: None), \
            mock.patch.object(PortalSession, "get", side_effect=fake.get, autospec=True), \
            mock.patch.object(M, "_now", return_value=kw.pop("now", NOW)):
        PortalSession.requests_made = 0
        return M.crawl_source(kw.pop("src", SRC), store, **kw)


def test_open_notices_are_stored_without_detail_requests():
    fake, store = FakeApi([OPEN]), {}
    st = crawl(fake, store)
    assert fake.calls == [API + "?status=open&order=latest&page=1"]
    assert st["ok"] is True and st["listed"] == 2 and st["new"] == 2 and st["orgs"] == 1
    rec = store["994"]
    assert rec["org"] == POLICE and rec["close"] == "2026-11-11T12:00" and rec["active"] is True and rec["status"] == "active"
    assert rec["documents"] and "details_fetched_at" not in rec
    assert st["details_fetched"] == 0 and st["details_failed"] == 0


def test_every_page_is_read_up_to_the_last_page():
    second = [dict(ROWS[0], id=995, tender_number="No.2")]
    fake, store = FakeApi([page(ROWS, last_page=2, total=3), page(second, last_page=2, total=3)]), {}
    st = crawl(fake, store)
    assert [c.rsplit("=", 1)[1] for c in fake.calls] == ["1", "2"]
    assert st["listed"] == 3 and set(store) == {"994", "993", "995"}


def test_a_notice_that_shifted_onto_the_next_page_is_stored_once():
    fake, store = FakeApi([page(ROWS, last_page=2), page(ROWS[:1], last_page=2)]), {}
    st = crawl(fake, store)
    assert st["listed"] == 2 and len(store) == 2


def test_a_board_with_no_open_notice_is_a_valid_answer_that_closes_what_was_open():
    store = {}
    crawl(FakeApi([OPEN]), store)
    st = crawl(FakeApi([page([], total=0)]), store)                       # "no notices": total 0
    assert st["ok"] is True and st["orgs"] == 0 and st["errors"] == []
    assert all(r["active"] is False and r["inactive_since"] and r["status"] in ("closed", "removed") for r in store.values())


def test_a_notice_that_expires_is_dropped_from_the_open_ones_and_marked_closed():
    store = {}
    crawl(FakeApi([OPEN]), store)
    later = datetime(2026, 11, 20, 9, 0, tzinfo=M.IST)                    # after both deadlines
    st = crawl(FakeApi([OPEN]), store, now=later)
    assert st["ok"] is True and st["orgs"] == 0 and st["errors"] == []
    assert store["994"]["active"] is False and store["994"]["last_seen_active"]


def test_an_answer_that_is_not_the_api_fails_the_run_and_deactivates_nothing():
    store = {"1": {"id": "1", "source": "mizoram-etender", "org": POLICE, "active": True, "close": "2099-01-01T10:00"}}
    st = crawl(FakeApi(["<!doctype html><html><body><div id=q-app></div></body></html>"]), store)    # the empty app shell
    assert st["ok"] is False and any("unexpected answer" in e for e in st["errors"])
    assert store["1"]["active"] is True


def test_an_unchanged_department_is_skipped_on_the_next_run():
    store = {}
    crawl(FakeApi([OPEN]), store)
    st = crawl(FakeApi([OPEN]), store)
    assert st["orgs_skipped"] == 1 and st["new"] == 0


def test_an_extended_deadline_is_picked_up_as_a_change():
    store = {}
    crawl(FakeApi([OPEN]), store)
    for r in store.values():
        r["last_seen"] = "2020-01-01T00:00:00Z"
    moved = [dict(ROWS[0], last_date_of_submission="2026-11-20 12:00:00"), ROWS[1]]
    st = crawl(FakeApi([page(moved)]), store)
    assert store["994"]["close"] == "2026-11-20T12:00" and st["ok"] is True


def test_the_runner_dispatches_mizoram_sources_to_this_crawler():
    assert run.CRAWLERS["mizoram"] is M.crawl_source


def test_fetch_detail_is_never_expected():
    with pytest.raises(engine.DetailFailed):
        M.MizoramPortal(mock.Mock(), HOME).fetch_detail({}, {})
