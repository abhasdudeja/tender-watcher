"""Tender Management System portals (Telangana, Andhra Pradesh): parsers on trimmed real responses and crawl flows
against a fake portal (no network)."""
import base64
import json
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs

import pytest

from crawler import engine, run, tms as T
from crawler.http import PortalSession

FIX = Path(__file__).parent / "fixtures" / "tms"
TS_BASE, AP_BASE = "https://tender.telangana.gov.in", "https://tender.apeprocurement.gov.in"
read = lambda name: (FIX / name).read_text(encoding="utf-8")  # noqa: E731
LOGIN, LOGIN_TOKEN = read("login.html"), "11111111-2222-3333-4444-555555555555"
SINGARENI, CAD = "THE SINGARENI COLLIERIES COMPANY LIMITED", "I & CAD - Telangana"
SRC = {"slug": "telangana", "name": "Telangana", "type": "tms", "base_url": TS_BASE, "fetch_details": True,
       "max_detail_fetches": 500, "delay_seconds": 0}
AP_SRC = dict(SRC, slug="andhrapradesh", name="Andhra Pradesh", base_url=AP_BASE)


def ts_parts():
    page = read("telangana_list_page.html")
    return T.parse_departments(page), T.parse_columns(page)


# ---------------------------------------------------------------- parsers (real trimmed responses)
def test_dates_are_the_portals_wall_clock_in_12_hour_form():
    assert T.parse_dt("10/10/2026 06:04 PM") == "2026-10-10T18:04"
    assert T.parse_dt("15/10/2026 12:30 AM") == "2026-10-15T00:30" and T.parse_dt("15/10/2026 12:30 PM") == "2026-10-15T12:30"
    assert T.parse_dt("19/11/2025") == "2025-11-19" and T.parse_dt("31/02/2026 01:00 PM") is None and T.parse_dt("") is None


def test_amounts_are_plain_numbers_only():
    assert T._amount("71970000000.00") == 71970000000.0 and T._amount("₹ 1,200") == 1200.0
    assert T._amount("₹ 0") is None and T._amount("") is None and T._amount("0.50 % of Quoted value") is None


def test_token_comes_from_the_login_page():
    assert T.token_from_page(LOGIN) == LOGIN_TOKEN and T.token_from_page("<html></html>") is None


def test_the_columns_are_found_from_the_table_headings():
    assert T.parse_columns(read("telangana_list_page.html")) == \
        ["department", "id", "ref", "category", "title", "value", "pub", "open", "close", "action"]
    assert T.parse_columns(read("andhra_list_page.html")) == \
        ["department", "id", "ref", "category", "title", "value", "open", "close", "action"]      # no published date
    assert T.parse_columns("<html></html>") == T.DEFAULT_COLUMNS


def test_the_form_keeps_data_fields_not_buttons():
    form = T.parse_form(read("telangana_list_page.html"))
    assert form["CSRFToken"] and form["hdnType"] == "current" and "hdnIndentID" in form and "hdnProcurementID" in form
    assert "ctl00$VTMSBodyContent$Clear" not in form


def test_departments_come_from_the_select_with_spaces_collapsed():
    depts = T.parse_departments(read("telangana_list_page.html"))
    assert SINGARENI in depts and CAD in depts and "Select Department" not in depts


def test_a_row_is_split_into_department_and_circle_by_the_longest_known_department():
    depts = [CAD, "I", SINGARENI]
    assert T.split_department("I  &  CAD - Telangana-SE, Irrigation Circle", depts) == (CAD, "SE, Irrigation Circle")
    assert T.split_department(SINGARENI + "-Corp-MP", depts) == (SINGARENI, "Corp-MP")
    assert T.split_department("Unknown Body-Circle 9", depts) == ("Unknown Body-Circle 9", "")


def test_the_list_response_may_be_json_or_base64_json():
    body = json.dumps({"iTotalDisplayRecords": "1", "aaData": []})
    assert T.decode_body(body) == T.decode_body(base64.b64encode(body.encode()).decode())
    with pytest.raises(ValueError):
        T.decode_body("<html>maintenance</html>")


def test_telangana_rows_and_the_portals_own_test_tenders():
    depts, cols = ts_parts()
    total, rows = T.parse_list(read("telangana_list.json"), depts, cols)
    assert total == 2058 and len(rows) == 4 and "742204" not in {r["id"] for r in rows}      # Eprocadmin's "test" is dropped
    first = rows[0]
    assert first["id"] == "741460" and first["department"] == SINGARENI and first["chain"] == SINGARENI + "||Corp-MP"
    assert first["category"] == "Services" and first["value"] == 71970000000.0
    assert first["pub"] == "2026-10-10T18:04" and first["open"] == "2026-10-15T17:00" and first["close"] == "2026-10-19T17:00"
    assert first["detail_ref"] == "674461,104,674451"
    assert {r["department"] for r in rows} == {SINGARENI, CAD}


def test_andhra_pradesh_rows_come_from_base64_without_a_published_date():
    page = read("andhra_list_page.html")
    total, rows = T.parse_list(read("andhra_list.json"), T.parse_departments(page), T.parse_columns(page))
    assert total > 0 and rows
    first = rows[0]
    assert first["id"] == "865169" and first["department"] == "APGENCO" and first["chain"] == "APGENCO||Dr NTTPS"
    assert first["value"] is None and first["pub"] is None                    # "₹ 0": no value given; no published column
    assert first["open"] == "2025-11-19T18:00" and first["close"] == "2026-10-22T10:30" and first["detail_ref"] == "774634,102,773314"


def test_the_telangana_detail_page_gives_fees_documents_and_no_personal_details():
    d = T.parse_detail(read("telangana_detail.html"))
    assert d["authority"] == SINGARENI + "-Corp-MP"
    assert d["details"]["Transaction Fee (INR)"] == "29500" and d["details"]["Bid Validity Period (in Days)"] == "180"
    assert "Letter of Bid (LOB)" in d["documents"] and "emd" not in d              # bid security is "N/A"
    text = json.dumps(d)
    assert "@" not in text and "0000000000" not in text and not any("officer" in k.lower() or "address" in k.lower() for k in d["details"])


def test_the_andhra_detail_page_keeps_a_percentage_emd_as_text():
    d = T.parse_detail(read("andhra_detail.html"))
    assert d["authority"] == "APGENCO-Dr NTTPS" and d["details"]["Tender Type"] == "OPEN - NCB"
    assert "emd" not in d and any("%" in v for k, v in d["details"].items() if "Bid Security" in k)
    assert "@" not in json.dumps(d)


def _tender_page(*rows):
    head = "<tr><td>Department Name</td><td>D</td><td>Circle/Division</td><td>C</td></tr>"
    return "<html><body><table>" + head + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows) + "</table></body></html>"


def test_bid_security_is_read_from_either_layout():
    ts = T.parse_detail(_tender_page(["Bid Security (INR)", "Bid Security In Favour Of", "Mode of Payment", "EMD Applicable"],
                                     ["Rs. 5513.00", "SAOOANDMKTPSVVISTAGES", "Online Payment,BG", ""]))
    assert ts["emd"] == 5513.0 and ts["details"]["Bid Security (INR)"] == "Rs. 5513.00"            # Telangana: the next row
    split = ("<html><body><table><tr><td>Department Name</td><td>D</td><td>Circle/Division</td><td>C</td></tr></table>"
             "<table><thead><tr><th>Bid Security (INR)</th><th>Bid Security In Favour Of</th></tr></thead>"
             "<tbody><tr><td>Rs. 30412461.00</td><td>SE</td></tr></tbody></table></body></html>")
    assert T.parse_detail(split)["emd"] == 30412461.0                  # header and amount in different table sections
    ap = T.parse_detail(_tender_page(["Bid Security (INR)", "Rs.", "25700000.00", "Bid Security In Favour Of", "NREDCAP Ltd"]))
    assert ap["emd"] == 25700000.0 and ap["details"]["Bid Security (INR)"] == "25700000.00"         # Andhra Pradesh: the same row
    none = T.parse_detail(_tender_page(["Bid Security (INR)", "Bid Security In Favour Of", "Mode of Payment", "EMD Applicable"],
                                       ["N/A", "", "", ""]))
    assert "emd" not in none and "Bid Security (INR)" not in none["details"]


def test_numbered_checklist_rows_and_paragraphs_are_not_detail_fields():
    d = T.parse_detail(_tender_page(["10", "Processing fee of Rs. 3200 through Current Bank Account No. 024305010245, IFSC ICIC0000243"],
                                    ["Bid Validity Period (in Days)", "90"], ["x" * 150, "a paragraph, not a field"],
                                    ["Remarks", "y" * 900]))
    assert "10" not in d["details"] and not any("Bank Account" in v for v in d["details"].values())
    assert d["details"]["Bid Validity Period (in Days)"] == "90" and len(d["details"]["Remarks"]) == T.MAX_VALUE
    assert not any(len(k) > T.MAX_LABEL for k in d["details"])


def test_a_page_that_is_not_a_tender_is_empty():
    assert T.parse_detail("<html><body>Your session has been automatically logged out</body></html>") == {}


# ---------------------------------------------------------------- crawl flows against a fake portal
class FakeTms:
    """Log-in page, list page, paged JSON list and detail pages, with the checks the real portal makes."""

    def __init__(self, prefix="telangana", total=None, encode=False):
        self.prefix, self.total, self.encode = prefix, total, encode
        self.list_page, self.detail = read(f"{prefix}_list_page.html"), read(f"{prefix}_detail.html")
        self.rows = T.decode_body(read(f"{prefix}_list.json"))["aaData"]
        self.logins, self.queries, self.detail_posts, self.timeouts_left = 0, [], [], 0

    def get(self, _sess, url, _allow_stale=False, label=""):
        path, _, qs = url.partition("?")
        if path.endswith("/login.html"):
            self.logins += 1
            return LOGIN
        if path.endswith("/TenderDetailsHomeJson.html"):
            q = {k: v[0] for k, v in parse_qs(qs, keep_blank_values=True).items()}
            assert "CSRFToken" not in q and q["hdnType"] == "current" and q["sEcho"]
            self.queries.append(q)
            start, length = int(q["iDisplayStart"]), int(q["iDisplayLength"])
            body = json.dumps({"iTotalRecords": -1, "iTotalDisplayRecords": str(self.total or len(self.rows)),
                               "aaData": self.rows[start:start + length]})
            return base64.b64encode(body.encode()).decode() if self.encode else body
        raise AssertionError(url)

    def post(self, _sess, url, data=None, headers=None, label=""):
        path = url.split("?")[0]
        if path.endswith("/TenderDetailsHome.html"):
            assert data["CSRFToken"] == LOGIN_TOKEN and data["hdnType"] == "current"
            return self.list_page
        if path.endswith("/ViewTender.html"):
            self.detail_posts.append(dict(data))
            if self.timeouts_left:
                self.timeouts_left -= 1
                return "<html><body>Your session has been automatically logged out due to inactivity.</body></html>"
            return self.detail
        raise AssertionError(url)


def crawl(fake, store, src=SRC, **kw):
    with mock.patch.object(PortalSession, "__init__", lambda self, *a, **k: None), \
            mock.patch.object(PortalSession, "get", side_effect=fake.get, autospec=True), \
            mock.patch.object(PortalSession, "post", side_effect=fake.post, autospec=True):
        PortalSession.requests_made = 0
        return T.crawl_source(src, store, **kw)


def test_crawl_lists_every_department_and_fetches_details():
    fake, store = FakeTms(), {}
    st = crawl(fake, store)
    assert st["ok"] is True and st["orgs"] == 2 and st["listed"] == 4 and st["new"] == 4 and st["details_fetched"] == 4
    rec = store["741460"]
    assert rec["org"] == SINGARENI and rec["chain"] == SINGARENI + "||Corp-MP" and rec["category"] == "Services"
    assert rec["value"] == 71970000000.0 and rec["close"] == "2026-10-19T17:00" and rec["pub"] == "2026-10-10T18:04"
    assert rec["authority"] == SINGARENI + "-Corp-MP" and rec["details"]["Transaction Fee (INR)"] == "29500"
    assert rec["active"] is True and rec["status"] == "active" and rec["details_fetched_at"]
    assert "742204" not in store                                              # the administrator's test tender
    assert "@" not in json.dumps(store)


def test_each_detail_request_posts_the_form_with_the_rows_three_numbers():
    fake = FakeTms()
    crawl(fake, {}, only_orgs=[CAD])
    assert len(fake.detail_posts) == 1
    p = fake.detail_posts[0]
    assert (p["hdnIndentID"], p["hdnTenderCategory"], p["hdnProcurementID"]) == ("671949", "101", "671939")
    assert p["CSRFToken"] and p["hdnPreviousPage"] == "TenderDetailsHome.html" and p["popUPRequestParameter"]


def test_the_list_is_paged_with_the_grids_own_parameters():
    fake = FakeTms()
    with mock.patch.object(T, "PAGE", 2):
        st = crawl(fake, {}, no_details=True)
    assert st["ok"] is True and st["listed"] == 4
    assert [q["iDisplayStart"] for q in fake.queries] == ["0", "2", "4"] and {q["iDisplayLength"] for q in fake.queries} == {"2"}
    q = fake.queries[0]
    assert q["iColumns"] == "10" and q["iSortCol_0"] == "1" and q["nDepartmentID"] == "0" and q["hdnType"] == "current"


def test_andhra_pradesh_is_crawled_through_base64_and_nine_columns():
    fake, store = FakeTms("andhra", encode=True), {}
    st = crawl(fake, store, src=AP_SRC)
    assert st["ok"] is True and st["listed"] == len(fake.rows) and st["details_fetched"] == len(fake.rows)
    assert fake.queries[0]["iColumns"] == "9"
    rec = store["865169"]
    assert rec["org"] == "APGENCO" and rec["close"] == "2026-10-22T10:30" and "pub" not in rec and rec.get("value") is None


def test_a_fresh_unchanged_department_is_skipped_without_detail_requests():
    fake, store = FakeTms(), {}
    crawl(fake, store)
    fake2 = FakeTms()
    st = crawl(fake2, store)
    assert st["orgs_skipped"] == 2 and fake2.detail_posts == []


def test_a_changed_closing_date_refetches_just_that_tender():
    fake, store = FakeTms(), {}
    crawl(fake, store)
    for r in store.values():
        r["last_seen"] = "2020-01-01T00:00:00Z"
    fake2 = FakeTms()
    fake2.rows = [list(r) for r in fake2.rows]
    fake2.rows[2][8] = "30/10/2026 03:00 PM"                                   # tender 740206: deadline extended
    st = crawl(fake2, store)
    assert st["changed"] == 1 and st["details_fetched"] == 1 and fake2.detail_posts[0]["hdnProcurementID"] == "673197"


def test_a_tender_that_leaves_the_list_is_kept_and_marked_after_a_full_run():
    store = {"OLD": {"id": "OLD", "source": "telangana", "org": SINGARENI, "active": True, "close": "2020-01-01T10:00",
                     "last_seen": "2020-01-01T00:00:00Z"}}
    st = crawl(FakeTms(), store, no_details=True)
    assert st["ok"] is True and store["OLD"]["active"] is False and store["OLD"]["status"] == "closed"


def test_an_ended_session_logs_in_again_once_and_the_detail_is_still_fetched():
    fake, store = FakeTms(), {}
    fake.timeouts_left = 1
    st = crawl(fake, store, only_orgs=[CAD])
    assert st["ok"] is True and st["details_fetched"] == 1 and st["details_failed"] == 0
    assert fake.logins == 2 and len(fake.detail_posts) == 2


def test_a_response_that_is_not_a_tender_is_a_failed_detail_not_a_fetch():
    fake, store = FakeTms(), {}
    fake.detail = "<html><body>maintenance</body></html>"
    st = crawl(fake, store, only_orgs=[CAD])
    assert st["details_fetched"] == 0 and st["details_failed"] == 1 and st["ok"] is True
    assert any("no tender in the response" in e for e in st["errors"])
    assert all("details_fetched_at" not in r for r in store.values())


def test_a_list_that_comes_back_far_short_fails_the_run():
    st = crawl(FakeTms(total=500), {}, no_details=True)
    assert st["ok"] is False and any("returned only" in e for e in st["errors"])


def test_a_missing_token_fails_the_run_with_a_clear_message():
    class NoToken(FakeTms):
        def get(self, _sess, url, _allow_stale=False, label=""):
            return "<html>maintenance</html>"

    st = crawl(NoToken(), {})
    assert st["ok"] is False and any("CSRF token" in e for e in st["errors"])


def test_the_runner_dispatches_tms_sources_to_this_crawler():
    assert run.CRAWLERS["tms"] is T.crawl_source
    assert engine.DetailFailed  # the adapter raises the engine's own exception for a bad detail page
