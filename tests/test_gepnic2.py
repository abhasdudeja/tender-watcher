"""GePNIC on NIC's new front end: parsers on real API responses, and crawl flows against a fake API (no network)."""
import json
from pathlib import Path
from unittest import mock

from crawler import gepnic2 as G, run
from crawler.http import PortalSession

FIX = Path(__file__).parent / "fixtures" / "gepnic2"
BASE = "https://meghalayatenders.gov.in/nicgep/app"
API = "https://meghalayatenders.gov.in/nggepnic_home_service"
read = lambda name: (FIX / name).read_text(encoding="utf-8")  # noqa: E731
SRC = {"slug": "meghalaya", "name": "Meghalaya", "type": "gepnic2", "base_url": BASE, "fetch_details": True,
       "max_detail_fetches": 500, "delay_seconds": 0}
SWD = "Social Welfare Department-ADB"
MBDA = "MEGHALAYA BASIN DEVELOPMENT AUTHORITY - WORLD BANK"
MBDA_RAW = "MEGHALAYA BASIN DEVELOPMENT AUTHORITY  - WORLD BANK"


# ---------------------------------------------------------------- parsers (real responses)
def test_the_api_lives_beside_the_old_app_path():
    assert G.api_root(BASE) == API
    assert G.api_root("https://manipurtenders.gov.in/nicgep/app/") == "https://manipurtenders.gov.in/nggepnic_home_service"


def test_org_list_keeps_the_raw_name_to_query_and_a_tidied_one_to_store():
    orgs = G.parse_org_list(read("org_list.json"))
    names = [o["name"] for o in orgs]
    assert len(orgs) == 8 and SWD in names and "Ministry of PWD,Meghalaya" in names
    mbda = next(o for o in orgs if o["name"] == MBDA)                            # the portal sends a double space
    assert mbda["query"] == "MEGHALAYA BASIN DEVELOPMENT AUTHORITY  - WORLD BANK"
    assert G.parse_org_list("") == [] and G.parse_org_list('{"dropdownList": null}') == []


def test_org_tenders_rows_use_the_gepnic_tender_id():
    page = G.parse_org_tenders(read("tender_list.json"))
    assert page["total"] == 4 and len(page["rows"]) == 4
    r = page["rows"][0]
    assert r["id"] == "2026_SWADB_2095_1" and r["ref"] == "MECDM/ADB/Eggs/PKG50/545/2026"
    assert r["title"].startswith("Procurement of Eggs in Garo Hills Region")
    assert r["chain"] == "Social Welfare Department-ADB||Meghalaya Early Childhood Development Mission (MECDM)"
    assert r["pub"] == "2026-11-12T15:00" and r["close"] == "2026-12-03T15:00" and r["open"] == "2026-12-03T15:00"
    assert r["detail_ref"] == "bsXp0dFmgcz8Ep2QfSe6Sw"


def test_an_empty_body_means_no_tenders():
    assert G.parse_org_tenders("") == {"rows": [], "total": None}


def test_detail_gives_typed_fields_and_no_signer_names():
    d = G.parse_detail(read("tender_detail.json"))
    assert d["value"] == 86186277.0 and d["emd"] == 870000.0
    assert d["category"] == "Goods" and d["product_category"] == "Miscellaneous" and d["tender_type"] == "Open Tender"
    assert d["form_of_contract"] == "Item Rate" and d["period"] == "365" and d["location"] == "Meghalaya"
    assert d["pincode"] == "793001" and d["bid_end"] == "2026-12-03T15:00" and d["authority"] == "CEO MECDM"
    assert d["documents"] == ["Annexure1to5Pkg050.xls", "BOQ_2267.xls", "FBPkg050.pdf", "TBPkg050.pdf", "Tendernotice_1.pdf"]
    assert d["details"]["Tender ID"] == "2026_SWADB_2095_1" and d["details"]["Bid Submission End Date"] == "03-Dec-2026 03:00 PM"
    assert "Remarks" not in d["details"]                                        # "NA" is dropped
    assert "REDACTED" not in json.dumps(d) and "signed" not in json.dumps(d).lower()


def test_a_hidden_or_zero_value_is_not_stated():
    j = json.loads(read("tender_detail.json"))
    j["tenderOtherInfo"]["tenderWorkItemDetails"]["showTenderValue"] = "false"
    assert "value" not in G.parse_detail(json.dumps(j))
    j["tenderOtherInfo"]["tenderWorkItemDetails"].update(showTenderValue="true", tenderValue="0.00")
    assert "value" not in G.parse_detail(json.dumps(j))


def test_something_that_is_not_a_tender_is_empty():
    assert G.parse_detail("") == {}
    assert G.parse_detail(json.dumps({"error": "Validation Failed"})) == {}
    assert G.parse_detail(json.dumps({"tenderDetails": {"tenderId": None}})) == {}


# ---------------------------------------------------------------- crawl flows against a fake API
class FakeApi:
    """get-org-list, the org-wise list (paged by limit/offset, total only on count requests; the org name must match
    exactly, spaces included, as on the real portal) and the detail call. Every listed organisation has tenders."""

    def __init__(self, extra_rows=0):
        self.orgs = json.loads(read("org_list.json"))
        listing = json.loads(read("tender_list.json"))["frontEndLatestActiveTendersList"]
        extra = [dict(listing[0], tenderId=f"2026_SWADB_9{i:03d}_1", workItemId=f"w{i}") for i in range(extra_rows)]
        self.by_org = {SWD: listing + extra}
        for i, o in enumerate(self.orgs["dropdownList"]):
            if o["stringValue"] != SWD:
                self.by_org[o["stringValue"]] = [dict(listing[1], tenderId=f"2026_ORG{i}_1_1", workItemId=f"o{i}",
                                                      orgDept=o["stringValue"])]
        self.lists, self.details = [], []

    def post(self, sess, url, data=None, headers=None, label=""):
        assert headers["Content-Type"] == "application/json"
        body = json.loads(data)
        if url == API + G.ORG_LIST:
            return json.dumps(self.orgs)
        if url == API + G.ORG_TENDERS:
            assert body["limit"] == G.PAGE                       # the server only accepts some sizes
            rows = self.by_org.get(body["org"], [])
            self.lists.append((body["org"], body["offset"], body["isCountRequest"]))
            if not rows:
                return ""                                        # what the portal answers for an organisation with none
            out = {"frontEndLatestActiveTendersList": rows[body["offset"]:body["offset"] + body["limit"]]}
            if body["isCountRequest"]:
                out["tendersCount"] = len(rows)
            return json.dumps(out)
        if url == API + G.DETAIL:
            self.details.append(body["workItemId"])
            return read("tender_detail.json")
        raise AssertionError(url)


def crawl(fake, store, **kw):
    with mock.patch.object(PortalSession, "__init__", lambda self, *a, **k: None), \
            mock.patch.object(PortalSession, "post", side_effect=fake.post, autospec=True):
        PortalSession.requests_made = 0
        return G.crawl_source(kw.pop("src", SRC), store, **kw)


def test_crawl_lists_organisations_with_tenders_and_fetches_details():
    fake, store = FakeApi(), {}
    st = crawl(fake, store)
    assert st["ok"] is True and st["orgs"] == 8 and st["listed"] == 4 + 7 and st["new"] == 11 and st["details_fetched"] == 11
    rec = store["2026_SWADB_2095_1"]
    assert rec["org"] == SWD and rec["close"] == "2026-12-03T15:00" and rec["value"] == 86186277.0
    assert rec["active"] is True and rec["details_fetched_at"] and rec["documents"]
    assert [x for x in fake.lists if x[0] == SWD] == [(SWD, 0, True)]          # the count request's page is reused


def test_a_long_list_is_paged_by_offset():
    fake, store = FakeApi(extra_rows=G.PAGE + 7), {}
    st = crawl(fake, store, no_details=True)
    assert st["ok"] is True and st["listed"] == 4 + G.PAGE + 7 + 7
    assert [x for x in fake.lists if x[0] == SWD] == [(SWD, 0, True), (SWD, G.PAGE, False)]


def test_a_fresh_unchanged_organisation_is_skipped_without_detail_requests():
    store = {}
    crawl(FakeApi(), store)
    fake2 = FakeApi()
    st = crawl(fake2, store)
    assert st["orgs_skipped"] == st["orgs"] and fake2.details == []


def test_a_tender_that_leaves_the_list_is_kept_and_marked_after_a_full_run():
    store = {"2020_X_1_1": {"id": "2020_X_1_1", "source": "meghalaya", "org": SWD, "active": True,
                            "close": "2020-01-01T10:00", "last_seen": "2020-01-01T00:00:00Z"}}
    st = crawl(FakeApi(), store, no_details=True)
    assert st["ok"] is True and store["2020_X_1_1"]["active"] is False and store["2020_X_1_1"]["status"] == "closed"


def test_an_organisation_name_is_sent_exactly_as_listed_and_stored_tidied():
    fake, store = FakeApi(), {}
    st = crawl(fake, store, no_details=True)
    assert st["ok"] is True
    assert (MBDA_RAW, 0, True) in fake.lists and not any(org == MBDA for org, *_ in fake.lists)
    assert [r["org"] for r in store.values() if r["chain"] == MBDA] == [MBDA]


def test_a_listed_organisation_whose_list_comes_back_empty_fails_and_keeps_its_tenders():
    store = {"2026_X_1_1": {"id": "2026_X_1_1", "source": "meghalaya", "org": MBDA, "active": True,
                            "close": "2099-01-01T10:00", "last_seen": "2020-01-01T00:00:00Z"}}
    fake = FakeApi()
    del fake.by_org[MBDA_RAW]                                     # e.g. the portal changed how it spells the name
    st = crawl(fake, store, no_details=True)
    assert st["ok"] is False and st["orgs_failed"] == 1 and any("came back empty" in e for e in st["errors"])
    assert store["2026_X_1_1"]["active"] is True and st["deactivated"] == 0


def test_an_empty_organisation_list_fails_the_run():
    fake = FakeApi()
    fake.orgs = {"dropdownList": []}
    st = crawl(fake, {})
    assert st["ok"] is False


def test_the_runner_dispatches_gepnic2_sources_to_this_crawler():
    assert run.CRAWLERS["gepnic2"] is G.crawl_source
