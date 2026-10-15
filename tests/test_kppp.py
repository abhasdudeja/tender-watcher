"""KPPP (Karnataka): parsers on trimmed real API responses, and crawl flows against a fake API (no network)."""
import json
from pathlib import Path
from unittest import mock

import pytest

from crawler import engine, kppp as K, run
from crawler.http import PortalSession

FIX = Path(__file__).parent / "fixtures" / "kppp"
BASE = "https://kppp.karnataka.gov.in"
API = BASE + K.API
read = lambda name: (FIX / name).read_text(encoding="utf-8")  # noqa: E731
SRC = {"slug": "karnataka", "name": "Karnataka", "type": "kppp", "base_url": BASE, "fetch_details": True,
       "max_detail_fetches": 500, "delay_seconds": 0}
KSCFL = "Karnataka State Co-Operative Federation LTD"


# ---------------------------------------------------------------- parsers (real trimmed responses)
def test_search_rows_use_a_category_letter_plus_the_internal_id():
    rows = K.parse_search(read("search_goods.json"))
    assert len(rows) == 5
    first = rows[0]
    assert first["id"] == "G63215" and first["ref"] == "KSCFL/2026-27/IND0027" and first["title"] == "PRINTING OF 2027 Calendars"
    assert first["department"] == KSCFL and first["chain"] == KSCFL + "||KSCFL Managing Director office Bengaluru"
    assert first["pub"] == "2026-10-15T16:52" and first["close"] == "2026-10-31T17:00" and first["category"] == "Goods"
    assert first["detail_ref"] == "GOODS:63215" and first["value"] is None          # the list has no estimated value
    assert K.parse_search(read("search_works.json"))[0]["id"].startswith("W")
    assert K.parse_search(read("search_services.json"))[0]["id"].startswith("S")


def test_works_rows_carry_their_work_category():
    first = K.parse_search(read("search_works.json"))[0]
    assert first["tender_type"] == "Other Works" and first["department"] == "Karnataka Forest Department"


def test_the_full_view_gives_value_emd_fees_and_documents_but_no_personal_details():
    d = K.parse_full_view(read("detail_goods.json"))
    assert d["value"] == 490000.0 and d["emd"] == 12500.0
    assert d["details"]["Tender Fee (INR)"] == "500" and d["details"]["Bid Validity (days)"] == "90"
    assert d["details"]["Bid Receipt Closing"] == "2026-10-31T17:00" and d["details"]["Technical Bid Opening"] == "2026-11-02T11:00"
    assert d["documents"] == ["Annual Turnover Certificate", "Company Registration Certificate"]
    text = json.dumps(d)
    assert "REDACTED" not in text and "contact" not in text.lower() and "mobile" not in text.lower()
    for cat, value, emd in (("works", 508853.0, 12721.0), ("services", 9697.0, 500.0)):
        x = K.parse_full_view(read(f"detail_{cat}.json"))
        assert x["value"] == value and x["emd"] == emd and x["documents"]


def test_a_document_name_that_is_a_paragraph_is_cut():
    j = json.loads(read("detail_goods.json"))
    j["tenderCriterionDocumentList"] = [{"documentName": "x" * 500}, {"documentName": "Short name"}]
    d = K.parse_full_view(json.dumps(j))
    assert d["documents"] == ["Short name", "x" * K.MAX_DOC]


def test_something_that_is_not_a_tender_is_empty():
    assert K.parse_full_view(json.dumps({"title": "Not Found", "status": 404})) == {}
    assert K.parse_full_view("[]") == {}


# ---------------------------------------------------------------- crawl flows against a fake API
class FakeKppp:
    """Search endpoints with paging and X-Total-Count, and the three full-view endpoints."""

    def __init__(self, per_type=None, detail_status=None):
        self.rows = {"GOODS": json.loads(read("search_goods.json")), "WORKS": json.loads(read("search_works.json")),
                     "SERVICES": json.loads(read("search_services.json"))}
        self.per_type = per_type or {}          # (category, type) -> rows; default: the fixture rows under OPEN only
        self.searches, self.details, self.detail_status = [], [], detail_status

    def rows_for(self, cat, ttype):
        return self.per_type.get((cat, ttype), self.rows[cat] if ttype == "OPEN" else [])

    def post(self, sess, url, data=None, headers=None, label=""):
        path, _, qs = url.partition("?")
        cat = next(c for c, (p, _d) in K.CATEGORIES.items() if path == API + p)
        body = json.loads(data)
        assert body["category"] == cat and body["status"] == "PUBLISHED" and headers["Content-Type"] == "application/json"
        q = dict(p.split("=") for p in qs.split("&"))
        assert q["order-by-tender-publish"] == "true"
        page, size = int(q["page"]), int(q["size"])
        rows = self.rows_for(cat, body["tenderType"])
        self.searches.append((cat, body["tenderType"], page, size))
        sess.last_headers = {"x-total-count": str(len(rows))}
        return json.dumps(rows[page * size:(page + 1) * size])

    def get(self, sess, url, _allow_stale=False, label=""):
        for cat, (_s, detail) in K.CATEGORIES.items():
            tail = detail.format(id="")
            if url.startswith(API + "/") and url.endswith(tail.lstrip("/")):
                self.details.append(url)
                if self.detail_status:
                    return json.dumps({"title": "Not Found", "status": self.detail_status})
                return read(f"detail_{cat.lower()}.json")
        raise AssertionError(url)


def crawl(fake, store, **kw):
    with mock.patch.object(PortalSession, "__init__", lambda self, *a, **k: None), \
            mock.patch.object(PortalSession, "get", side_effect=fake.get, autospec=True), \
            mock.patch.object(PortalSession, "post", side_effect=fake.post, autospec=True):
        PortalSession.requests_made = 0
        return K.crawl_source(kw.pop("src", SRC), store, **kw)


def test_crawl_lists_every_category_and_fetches_details():
    fake, store = FakeKppp(), {}
    st = crawl(fake, store)
    assert st["ok"] is True and st["listed"] == 15 and st["new"] == 15 and st["details_fetched"] == 15
    assert st["orgs"] == len({r["org"] for r in store.values()})
    rec = store["G63215"]
    assert rec["org"] == KSCFL and rec["ref"] == "KSCFL/2026-27/IND0027" and rec["close"] == "2026-10-31T17:00"
    assert rec["value"] == 490000.0 and rec["emd"] == 12500.0 and rec["category"] == "Goods"
    assert rec["active"] is True and rec["status"] == "active" and rec["details_fetched_at"] and rec["documents"]
    assert {c for c, *_ in fake.searches} == {"GOODS", "WORKS", "SERVICES"}
    assert {t for _, t, *_ in fake.searches} == set(K.TENDER_TYPES)          # open, reserved and restricted are all asked for


def test_reserved_and_restricted_tenders_are_included():
    works = json.loads(read("search_works.json"))
    reserved = [dict(works[0], nitId=9990001, tenderNumber="RES/1", invitingStrategy="RESERVED", invitingStrategyText="Reserved")]
    fake = FakeKppp(per_type={("WORKS", "RESERVED"): reserved})
    store = {}
    st = crawl(fake, store, no_details=True)
    assert "W9990001" in store and st["listed"] == 15 + 1


def test_the_list_is_paged_until_the_total_is_reached():
    fake, store = FakeKppp(), {}
    with mock.patch.object(K, "PAGE", 2):
        st = crawl(fake, store, no_details=True)
    assert st["ok"] is True and st["listed"] == 15
    goods = [(p, s) for c, t, p, s in fake.searches if c == "GOODS" and t == "OPEN"]
    assert goods == [(0, 2), (1, 2), (2, 2)]


def test_a_fresh_unchanged_department_is_skipped_without_detail_requests():
    store = {}
    crawl(FakeKppp(), store)
    fake2 = FakeKppp()
    st = crawl(fake2, store)
    assert st["orgs_skipped"] == st["orgs"] and fake2.details == []


def test_a_changed_closing_date_refetches_just_that_tender():
    store = {}
    crawl(FakeKppp(), store)
    for r in store.values():
        r["last_seen"] = "2020-01-01T00:00:00Z"
    fake2 = FakeKppp()
    fake2.rows["GOODS"] = [dict(r) for r in fake2.rows["GOODS"]]
    fake2.rows["GOODS"][1]["tenderClosureDate"] = "30-11-2026 17:00:00"            # deadline extended
    st = crawl(fake2, store)
    assert st["changed"] == 1 and st["details_fetched"] == 1 and len(fake2.details) == 1


def test_a_tender_that_leaves_the_list_is_kept_and_marked_after_a_full_run():
    store = {"G1": {"id": "G1", "source": "karnataka", "org": KSCFL, "active": True, "close": "2020-01-01T10:00",
                    "last_seen": "2020-01-01T00:00:00Z"}}
    st = crawl(FakeKppp(), store, no_details=True)
    assert st["ok"] is True and store["G1"]["active"] is False and store["G1"]["status"] == "closed"


def test_a_detail_answer_that_is_not_a_tender_is_a_failed_detail_not_a_fetch():
    fake, store = FakeKppp(detail_status=404), {}
    st = crawl(fake, store, only_orgs=[KSCFL])
    assert st["details_fetched"] == 0 and st["details_failed"] >= 1 and st["ok"] is True
    assert any("no tender in the response" in e for e in st["errors"])
    assert all("details_fetched_at" not in r for r in store.values())


def test_a_list_that_comes_back_far_short_fails_the_run():
    class Short(FakeKppp):
        def post(self, sess, url, data=None, headers=None, label=""):
            out = super().post(sess, url, data, headers, label)
            sess.last_headers = {"x-total-count": "900"}                              # claims far more than it returns
            return out

    st = crawl(Short(), {}, no_details=True)
    assert st["ok"] is False and any("returned only" in e for e in st["errors"])


def test_two_workers_fetch_the_same_details():
    store = {}
    st = crawl(FakeKppp(), store, src=dict(SRC, detail_workers=2))
    assert st["ok"] is True and st["details_fetched"] == 15 and all(r.get("value") for r in store.values())


def test_the_runner_dispatches_kppp_sources_to_this_crawler():
    assert run.CRAWLERS["kppp"] is K.crawl_source
    assert engine.DetailFailed
