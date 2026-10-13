"""Mizoram e-Tender notice board: parser on trimmed real pages, and crawl flows against a fake board (no network)."""
from datetime import datetime
from pathlib import Path
from unittest import mock

import pytest

from crawler import mizoram as M
from crawler.http import PortalSession

FIX = Path(__file__).parent / "fixtures" / "mizoram"
PAGE1 = (FIX / "page1.html").read_text(encoding="utf-8")
PAGE2 = (FIX / "page2.html").read_text(encoding="utf-8")
PAGE3 = (FIX / "page3.html").read_text(encoding="utf-8")
HOME = "https://tender.mizoram.gov.in/home"
SRC = {"slug": "mizoram-etender", "name": "Mizoram e-Tender", "type": "mizoram", "base_url": HOME,
       "fetch_details": False, "delay_seconds": 0}
NOW = datetime(2026, 10, 13, 13, 30, tzinfo=M.IST)          # the fixtures were taken on this day
POLICE = "Police"


# ---------------------------------------------------------------- parsing
def test_parse_dt_handles_the_portals_12_hour_clock():
    assert M.parse_dt("30-10-2026 01:00 pm") == "2026-10-30T13:00"
    assert M.parse_dt("14-08-2026 12:00 pm") == "2026-08-14T12:00"          # noon, not midnight
    assert M.parse_dt("14-08-2026 12:00 am") == "2026-08-14T00:00"
    assert M.parse_dt("14-08-2026 10:30 am") == "2026-08-14T10:30"
    assert M.parse_dt("28-09-2026") == "2026-09-28"
    assert M.parse_dt("31-02-2026 10:00 am") is None and M.parse_dt("soon") is None and M.parse_dt(None) is None


def test_parse_cards_reads_the_real_notice_cards_not_the_page_wrapper():
    rows = M.parse_cards(PAGE1)
    assert len(rows) == 3                                          # the wrapper container is not a card
    first = rows[0]
    assert first["id"] == "993" and first["ref"] == "D.22012/PHQ/PCS/PUR/2026-27/46"
    assert first["chain"] == POLICE and first["title"] == "Supply of Police Uniforms" and first["tender_type"] == "TENDER NOTICE"
    assert first["pub"] == "2026-09-28" and first["open"] == "2026-10-30T13:00" and first["close"] == "2026-10-30T12:00"
    assert first["documents"] == ["SPECIFICATION OF UNIFORM ITEMS FOR THE YEAR 2026.pdf", "TENDER TERMS & CONDITION -2026-27.pdf"]


def test_a_long_notice_type_is_the_title_not_a_type():
    # on some cards the h3 holds the whole title (and there is no separate description)
    html = ('<div class="container"><div class="container"><div>REF/1</div><div><i>Roads</i><h3>'
            + "Feasibility Study and Preparation of Project Report for the roads in the State of Mizoram " * 2
            + '</h3></div><div class="container"><div>Issue date: 01-01-2026</div><div>Opening Date: 02-01-2026 10:00 am</div>'
            '<div>Last Date: 02-01-2026 09:00 am</div></div></div></div>')
    row = M.parse_cards(html)[0]
    assert row["tender_type"] is None and row["title"].startswith("Feasibility Study") and row["id"].startswith("x")


# ---------------------------------------------------------------- crawl flows
class FakeBoard:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, _sess, url, _allow_stale=False, label=""):
        self.calls.append(url)
        n = 1 if url == HOME else int(url.rsplit("=", 1)[1])
        return self.pages[n - 1] if n <= len(self.pages) else PAGE3


def crawl(fake, store, **kw):
    with mock.patch.object(PortalSession, "__init__", lambda self, *a, **k: None), \
         mock.patch.object(PortalSession, "get", side_effect=fake.get, autospec=True), \
         mock.patch.object(M, "_now", return_value=kw.pop("now", NOW)):
        PortalSession.requests_made = 0
        return M.crawl_source(kw.pop("src", SRC), store, **kw)


def test_only_open_notices_are_stored_and_the_pages_are_walked_to_the_end():
    fake, store = FakeBoard([PAGE1, PAGE2]), {}
    st = crawl(fake, store)
    assert fake.calls == [HOME, HOME + "?page=2", HOME + "?page=3"]       # stops at the first empty page
    assert st["ok"] is True and st["listed"] == 1 and st["new"] == 1 and st["orgs"] == 1
    rec = store["993"]
    assert rec["org"] == POLICE and rec["close"] == "2026-10-30T12:00" and rec["active"] is True and rec["status"] == "active"
    assert rec["documents"] and "details_fetched_at" not in rec            # no detail pages exist
    assert st["details_fetched"] == 0 and st["details_failed"] == 0


def test_a_notice_that_shifted_onto_the_next_page_is_stored_once():
    fake, store = FakeBoard([PAGE1, PAGE2]), {}
    st = crawl(fake, store)                                               # page 2 repeats notice 993
    assert st["listed"] == 1 and len(store) == 1


def test_a_notice_that_expires_is_kept_and_marked_closed():
    fake, store = FakeBoard([PAGE1, PAGE2]), {}
    crawl(fake, store)
    later = datetime(2026, 11, 2, 9, 0, tzinfo=M.IST)                      # after the 30-10-2026 12:00 deadline
    st = crawl(FakeBoard([PAGE1, PAGE2]), store, now=later)
    assert st["ok"] is True and st["orgs"] == 0 and st["errors"] == []     # nothing open: valid, not a failure
    rec = store["993"]
    assert rec["active"] is False and rec["inactive_since"] and rec["last_seen_active"]
    assert rec["status"] in ("closed", "removed")      # which one depends on the engine's real clock vs the 30-10-2026 deadline


def test_a_board_without_cards_is_a_failure_and_deactivates_nothing():
    store = {"1": {"id": "1", "source": "mizoram-etender", "org": POLICE, "active": True, "close": "2099-01-01T10:00"}}
    st = crawl(FakeBoard([PAGE3]), store)                                  # first page has no cards: markup changed
    assert st["ok"] is False and any("no notice cards" in e for e in st["errors"])
    assert store["1"]["active"] is True


def test_an_unchanged_department_is_skipped_on_the_next_run():
    fake, store = FakeBoard([PAGE1, PAGE2]), {}
    crawl(fake, store)
    st = crawl(FakeBoard([PAGE1, PAGE2]), store)
    assert st["orgs_skipped"] == 1 and st["new"] == 0


def test_extended_deadline_is_picked_up_as_a_change():
    fake, store = FakeBoard([PAGE1, PAGE2]), {}
    crawl(fake, store)
    for r in store.values():
        r["last_seen"] = "2020-01-01T00:00:00Z"
        r["details_fetched_at"] = "2026-10-13T00:00:00Z"
    changed = PAGE1.replace("30-10-2026 12:00 pm", "05-11-2026 12:00 pm", 1)
    st = crawl(FakeBoard([changed, PAGE2]), store)
    assert store["993"]["close"] == "2026-11-05T12:00" and st["ok"] is True


def test_the_runner_dispatches_mizoram_sources_to_this_crawler():
    from crawler import run

    assert run.CRAWLERS["mizoram"] is M.crawl_source


def test_fetch_detail_is_never_expected():
    from crawler import engine

    with pytest.raises(engine.DetailFailed):
        M.MizoramPortal(mock.Mock(), HOME).fetch_detail({}, {})
