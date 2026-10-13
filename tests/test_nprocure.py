"""nProcure (Gujarat state portal) crawler: crypto, parsers on real trimmed pages, and crawl flows
against a fake portal (no network)."""
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from crawler import engine, nprocure as N
from crawler.http import PortalSession

FIX = Path(__file__).parent / "fixtures" / "nprocure"
STATE = "https://tender.nprocure.com"
HOME = (FIX / "state_home_clients.html").read_text(encoding="utf-8")
LISTING = (FIX / "listing_surat.json").read_text(encoding="utf-8")
DETAIL = (FIX / "detail_353672.html").read_text(encoding="utf-8")
# On real tenders the listing's estimated value and the detail page's agree exactly (checked on 5 live tenders), so the fake
# portal serves a detail page whose value matches the 500000.00 its listing rows carry.
DETAIL_FAKE = DETAIL.replace("23171681.20", "500000.00")


# ---------------------------------------------------------------- crypto
# Known-answer vector: encrypted by crawler.nprocure and then decrypted - and re-encrypted byte for byte -
# by the portal's own CryptoJS code (AesUtil) in a browser on 2026-10-12.
VECTOR_PLAIN = ('{"reqData":[{"name":"sEcho","value":1}],"_csrf":"","idList":"0",'
                '"id":"Tenders In Progress","note":"₹ ünïcode"}')
VECTOR_BODY = {
    "jsonData": "H8RyX2wul6Xk+XZ/T2r9zTrNQJZ5msE+bzARWMpeSnoWI12EDfuty4wqlrYWXvH1qiLeX2zmNGIykLR1epCkXMyFssOPtbVLrHUx4KSOUW1601bhpvipflTmXjVDLLO79jJNdTFi0lFQ0/xosI59kgHSx8GlALpTBTaZyBw/I0I=",
    "iv": "101112131415161718191a1b1c1d1e1f", "salt": "000102030405060708090a0b0c0d0e0f", "key": "QWIzeDk=",
}


def test_encryption_matches_the_portals_own_javascript_byte_for_byte():
    body = N.encrypt_payload(VECTOR_PLAIN, number="Ab3x9", salt=bytes(range(16)), iv=bytes(range(16, 32)))
    assert body == VECTOR_BODY
    assert N.decrypt_payload(VECTOR_BODY) == VECTOR_PLAIN


def test_encryption_is_randomised_but_always_decryptable():
    a, b = N.encrypt_payload("hello"), N.encrypt_payload("hello")
    assert a["jsonData"] != b["jsonData"] and a["salt"] != b["salt"] and a["iv"] != b["iv"]
    assert N.decrypt_payload(a) == N.decrypt_payload(b) == "hello"
    assert len(bytes.fromhex(a["iv"])) == len(bytes.fromhex(a["salt"])) == 16


def test_listing_request_body_carries_paging_and_the_page_parameters():
    envelope = json.loads(N.build_listing_body(300, 150))
    assert set(envelope) == {"jsonData", "iv", "salt", "key"}
    plain = json.loads(N.decrypt_payload(envelope))
    params = {p["name"]: p["value"] for p in plain["reqData"]}
    assert params["iDisplayStart"] == 300 and params["iDisplayLength"] == 150 and params["iColumns"] == 3
    assert plain["id"] == "Tenders In Progress" and plain["idList"] == "0" and plain["_csrf"] == ""


# ---------------------------------------------------------------- parsers (real trimmed pages)
def test_parse_dt():
    assert N.parse_dt("21-10-2026 18:00:00") == "2026-10-21T18:00"
    assert N.parse_dt("08-10-2026 18:00") == "2026-10-08T18:00"
    assert N.parse_dt("5-1-2027") == "2027-01-05"
    assert N.parse_dt("N/A") is None and N.parse_dt("31-02-2026 10:00") is None and N.parse_dt(None) is None


def test_client_list_has_name_count_id_and_the_clients_own_host():
    clients = {c["name"]: c for c in N.parse_client_list(HOME)}
    assert len(clients) == 5                                         # the '--Select--' placeholder is not a client
    surat = clients["Surat Municipal Corporation"]
    assert (surat["count"], surat["host"], surat["id"]) == (79, "https://smctender.nprocure.com", "6")
    assert clients["Rajkot Municipal Corporation"]["host"] == "https://rmctender.nprocure.com"
    assert clients["Ahmedabad Municipal Transport Service"]["count"] == 0     # listed, but nothing to crawl


def test_listing_rows_dedupe_value_zero_and_chain():
    page = N.parse_listing(LISTING, "Surat Municipal Corporation")
    assert page["total"] == 79 and len(page["rows"]) == 4          # 5 raw entries, one is a duplicate id
    r = page["rows"][0]
    assert r["id"] == "354196" and r["ref"] == "ACE & I/C CE/BRIDGE/05/2026-27(Work No.01)"
    assert r["title"].startswith("Maintenance of wearing coat") and r["title"].endswith("(4th Attempt)")
    assert r["chain"] == "Surat Municipal Corporation||SMC-Surat Mahanagar Seva Sadan"
    assert r["close"] == "2026-10-21T18:00" and r["value"] == 7213552.1 and r["doc_count"] == 3
    assert page["rows"][3]["value"] is None                         # a blank "Estimated Contract Value :" = not stated


def test_listing_survives_a_stray_non_utf8_byte_once_decoded_leniently():
    raw = LISTING.replace("Maintenance of wearing coat", "Maintenance® of wearing coat").encode("utf-8")
    raw = raw.replace("®".encode("utf-8"), b"\xae")             # what the portal really sends: a lone 0xAE
    text = raw.decode("utf-8", errors="replace")                    # PortalSession decodes like this
    page = N.parse_listing(text, "Surat Municipal Corporation")
    assert len(page["rows"]) == 4 and "�" in page["rows"][0]["title"]


def test_detail_fields_and_no_bidder_certificate_data():
    d = N.parse_tender_detail(DETAIL)
    assert d["location"] == "Surat" and d["tender_type"] == "Open" and d["form_of_contract"] == "Works"
    assert d["category"] == "Works" and d["product_category"] == "Pipe Laying Work"
    assert d["value"] == 23171681.2 and d["emd"] == 231800.0
    assert d["pub"] == "2026-10-08T18:00" and d["bid_end"] == "2026-10-21T18:00"
    assert d["authority"] == "Deputy Municipal Commissioner (D)" and d["authority_address"].startswith("Drainage Department")
    assert d["period"] == "As per tender Document Attached" and d["description"].startswith("Providing and laying RCC")
    assert len(d["documents"]) == 3 and d["documents"] == sorted(d["documents"])
    raw = d["details"]
    assert raw["Tender ID"] == "353672" and raw["Organization Name"] == "Surat Municipal Corporation"
    assert raw["Sub Department"] == "Surat Mahanagar Seva Sadan" and raw["Bid validity"] == "120 Days"
    assert not any("signer" in (k + v).lower() or "thumbprint" in k.lower() or "subject" in k.lower() for k, v in raw.items())


def test_detail_page_without_label_value_pairs_parses_to_nothing():
    assert N.parse_tender_detail("<html><body><main><p>Tender not found</p></main></body></html>") == {}


# ---------------------------------------------------------------- transport: POST + tolerant decoding
def test_post_decodes_a_response_with_a_stray_byte_instead_of_failing():
    sess = PortalSession("https://x.test", delay_seconds=0, ignore_robots=True)
    seen = {}

    def fake_request(method, url, **kw):
        seen.update(method=method, url=url, **kw)
        return SimpleNamespace(status_code=200, headers={"Content-Type": "text/html;charset=UTF-8"},
                               content=b'{"data":[{"1":"caf\xae"}]}', raise_for_status=lambda: None)

    sess._http = SimpleNamespace(request=fake_request, close=lambda: None)
    text = sess.post("https://x.test/p", data='{"a":1}', headers={"Content-Type": "application/json"})
    assert json.loads(text)["data"][0]["1"].startswith("caf") and "�" in text
    assert seen["method"] == "POST" and seen["data"] == '{"a":1}' and seen["headers"]["Content-Type"] == "application/json"
    assert sess.requests_made == 1


# ---------------------------------------------------------------- crawl flow against a fake portal
def item(tid, title="Some work", dept="SMC-Surat Mahanagar Seva Sadan", ecv="500000.00", due="21-10-2026 18:00:00"):
    form = ("<form action='/view-nit-home' method='POST' target='_blank'><input type='hidden' name='_csrf' value='token'>"
            "<input type='hidden' name='tenderid' id='tenderid_{t}' value='{t}'/>\n"
            "<a href='#' id='tenderInProgress' class='edit1'>{inner}</a></form>")
    frag = ("<html><body><span style=color:#f44336; >" + dept
            + form.format(t=tid, inner=f"Tender Id :{tid}") + "</span><span style=color:#FF9933; >"
            + form.format(t=tid, inner=f"<strong style='color: maroon;'>Name Of Work :</strong>{title}") + "</span>\n"
            + f"<p style=color:#FF9933;display:block; class='mb-2'> Estimated Contract Value : {ecv}\n"
            + f"<p style=color:#000000; class='mt-2 mb-0'> Last Date & Time For Submission : {due}</body></html>")
    return {"1": f"NIT/{tid}", "2": frag, "3": f"<form><a href='#'>Total No:2 </a></form>"}


class FakeNprocure:
    """State home page + per-client listing (paged) + detail pages, speaking the real protocol."""

    def __init__(self, rows_by_host=None, detail_html=DETAIL_FAKE):
        self.rows_by_host = rows_by_host or {}
        self.detail_html = detail_html
        self.listing_calls, self.detail_calls, self.home_calls = [], [], 0

    def get(self, _sess, url, _allow_stale=False, label=""):
        assert url == STATE + "/", url
        self.home_calls += 1
        return HOME

    def post(self, _sess, url, data=None, headers=None, label=""):
        host, path = re.match(r"(https://[^/]+)(/.*)", url).groups()
        if path == N.LISTING_PATH:
            plain = json.loads(N.decrypt_payload(json.loads(data)))
            p = {x["name"]: x["value"] for x in plain["reqData"]}
            start, length = p["iDisplayStart"], p["iDisplayLength"]
            self.listing_calls.append((host, start, length))
            rows = self.rows_by_host.get(host, [])
            return json.dumps({"iTotalRecords": len(rows), "recordsFiltered": len(rows),
                               "data": [item(**r) for r in rows[start:start + length]]})
        if path == N.DETAIL_PATH:
            assert data == {"_csrf": "token", "tenderid": data["tenderid"]}
            self.detail_calls.append((host, data["tenderid"]))
            return self.detail_html
        raise AssertionError(url)


RAJKOT, SURAT = "https://rmctender.nprocure.com", "https://smctender.nprocure.com"
SRC = {"slug": "gujarat", "name": "Gujarat", "base_url": STATE, "fetch_details": True,
       "max_detail_fetches": 500, "delay_seconds": 0}


def crawl(fake, store, **kw):
    with mock.patch.object(PortalSession, "__init__", lambda self, *a, **k: None), \
         mock.patch.object(PortalSession, "get", side_effect=fake.get, autospec=True), \
         mock.patch.object(PortalSession, "post", side_effect=fake.post, autospec=True):
        PortalSession.requests_made = 0
        return N.crawl_source(kw.pop("src", SRC), store, **kw)


def rows(n, base=900000):
    return [{"tid": str(base + i), "title": f"Work {i}"} for i in range(n)]


def test_crawl_pages_the_client_listing_and_fetches_details(monkeypatch):
    monkeypatch.setattr(N, "PAGE_SIZE", 10)
    fake, store = FakeNprocure({RAJKOT: rows(27)}), {}
    st = crawl(fake, store, only_orgs=["Rajkot Municipal Corporation"])
    assert fake.listing_calls == [(RAJKOT, 0, 10), (RAJKOT, 10, 10), (RAJKOT, 20, 10)]    # 27 rows -> 3 pages, own host
    assert st["ok"] is True and st["listed"] == 27 and st["new"] == 27 and st["details_fetched"] == 27
    assert len(fake.detail_calls) == 27 and {h for h, _ in fake.detail_calls} == {RAJKOT}
    rec = store["900000"]
    assert rec["org"] == "Rajkot Municipal Corporation" and rec["chain"] == "Rajkot Municipal Corporation||SMC-Surat Mahanagar Seva Sadan"
    assert rec["ref"] == "NIT/900000" and rec["title"] == "Work 0" and rec["close"] == "2026-10-21T18:00"
    assert rec["value"] == 500000.0 and rec["emd"] == 231800.0 and rec["category"] == "Works"
    assert rec["pub"] == "2026-10-08T18:00" and rec["active"] is True and rec["status"] == "active" and rec["details_fetched_at"]


def test_a_stale_client_list_count_is_not_mistaken_for_a_markup_change():
    # the dropdown still says the client has tenders, but its own listing says total 0: that is the truth
    fake, store = FakeNprocure({RAJKOT: []}), {}
    st = crawl(fake, store, only_orgs=["Rajkot Municipal Corporation"])
    assert st["ok"] is True and st["errors"] == [] and st["listed"] == 0 and fake.listing_calls


def test_clients_with_zero_tenders_are_never_requested():
    fake = FakeNprocure({SURAT: rows(2), RAJKOT: rows(1, 800000), "https://amctender.nprocure.com": rows(1, 700000),
                         "https://autender.nprocure.com": rows(1, 600000)})
    crawl(fake, {}, no_details=True)
    assert {h for h, _, _ in fake.listing_calls} == {SURAT, RAJKOT, "https://amctender.nprocure.com", "https://autender.nprocure.com"}
    assert "https://amtstender.nprocure.com" not in {h for h, _, _ in fake.listing_calls}     # AMTS has (0)


def test_duplicate_ids_in_the_listing_are_stored_once():
    r = rows(3)
    fake = FakeNprocure({SURAT: r + [r[0]]})
    store = {}
    st = crawl(fake, store, only_orgs=["Surat Municipal Corporation"], no_details=True)
    assert len(store) == 3 and st["listed"] == 3


def test_a_fresh_unchanged_client_is_skipped_without_any_listing_request(monkeypatch):
    fake, store = FakeNprocure({RAJKOT: rows(27)}), {}
    crawl(fake, store, only_orgs=["Rajkot Municipal Corporation"])
    # the client list says Rajkot has 27 tenders and we now hold 27 active, fresh ones
    fake2 = FakeNprocure({RAJKOT: rows(27)})
    st = crawl(fake2, store, only_orgs=["Rajkot Municipal Corporation"])
    assert st["orgs_skipped"] == 1 and fake2.listing_calls == [] and fake2.detail_calls == []
    assert fake2.home_calls == 1                                    # only the client list was fetched


def test_changed_closing_date_refetches_just_that_tender():
    fake, store = FakeNprocure({SURAT: rows(3)}), {}
    crawl(fake, store, only_orgs=["Surat Municipal Corporation"])
    for r in store.values():
        r["last_seen"] = "2020-01-01T00:00:00Z"                     # listing is old -> re-listed next run
    before = {k: r["details_fetched_at"] for k, r in store.items()}
    changed = rows(3)
    changed[1]["due"] = "30-10-2026 18:00:00"                      # closing date extended
    fake2 = FakeNprocure({SURAT: changed})
    st = crawl(fake2, store, only_orgs=["Surat Municipal Corporation"])
    assert st["changed"] == 1 and st["details_fetched"] == 1
    assert fake2.detail_calls == [(SURAT, "900001")]
    assert store["900000"]["details_fetched_at"] == before["900000"] and store["900002"]["details_fetched_at"] == before["900002"]
    assert store["900001"]["bid_end"] == "2026-10-21T18:00" and "needs_details" not in store["900001"]


def test_empty_detail_page_is_a_failure_not_a_fetch():
    fake = FakeNprocure({SURAT: rows(2)}, detail_html="<html><body><main>nothing</main></body></html>")
    store = {}
    st = crawl(fake, store, only_orgs=["Surat Municipal Corporation"])
    assert st["details_fetched"] == 0 and st["details_failed"] == 2 and st["ok"] is True
    assert all("details_fetched_at" not in r for r in store.values())
    assert any("no label/value pairs" in e for e in st["errors"])


def test_tender_that_leaves_the_portal_is_kept_and_marked_after_a_full_run():
    store = {"OLD1": {"id": "OLD1", "source": "gujarat", "org": "Rajkot Municipal Corporation", "active": True,
                      "close": "2020-01-01T10:00", "last_seen": "2020-01-01T00:00:00Z"}}
    fake = FakeNprocure({SURAT: rows(1), RAJKOT: rows(1, 800000), "https://amctender.nprocure.com": rows(1, 700000),
                         "https://autender.nprocure.com": rows(1, 600000)})
    st = crawl(fake, store, no_details=True)                        # a full run: no only_orgs / limit_orgs
    assert st["ok"] is True
    assert store["OLD1"]["active"] is False and store["OLD1"]["status"] == "closed" and "OLD1" in store    # kept, not deleted
    assert st["deactivated"] == 1 and st["active"] == sum(1 for r in store.values() if r["active"])    # status counts after retention
    assert store["OLD1"]["last_seen_active"] == "2020-01-01T00:00:00Z"


def test_a_failing_client_does_not_stop_the_others_or_deactivate_anything():
    class Flaky(FakeNprocure):
        def post(self, _sess, url, data=None, headers=None, label=""):
            if url.startswith(RAJKOT) and url.endswith(N.LISTING_PATH):
                raise RuntimeError("HTTP 500")
            return super().post(_sess, url, data, headers, label)

    store = {"KEEP": {"id": "KEEP", "source": "gujarat", "org": "Rajkot Municipal Corporation", "active": True}}
    fake = Flaky({SURAT: rows(2), "https://amctender.nprocure.com": rows(1, 700000), "https://autender.nprocure.com": rows(1, 600000)})
    st = crawl(fake, store, no_details=True)
    assert st["ok"] is False and st["orgs_failed"] == 1 and any("Rajkot" in e for e in st["errors"])
    assert "900000" in store                                        # the other clients were still crawled
    assert store["KEEP"]["active"] is True                          # an incomplete run never deactivates


def test_adapter_never_asks_for_a_restart():
    portal = N.NprocurePortal(SimpleNamespace(requests_made=7), STATE)
    org = {"name": "x", "host": "h"}
    assert portal.restart_org(org) is org and portal.requests_made() == 7
    assert issubclass(engine.Restart, Exception)


def test_client_names_that_contain_parentheses_keep_them_and_split_off_only_the_count():
    html = ('<select><option value="63" class="subDomainName_https://gmrctender.nprocure.com:443">'
            'Gujarat Metro Rail Corporation (GMRC) Ltd. (14)</option>'
            '<option value="7" class="subDomainName_https://x.nprocure.com:443">Plain Client</option></select>')
    got = {c["name"]: c for c in N.parse_client_list(html)}
    assert got["Gujarat Metro Rail Corporation (GMRC) Ltd."]["count"] == 14
    assert got["Gujarat Metro Rail Corporation (GMRC) Ltd."]["host"] == "https://gmrctender.nprocure.com"
    assert got["Plain Client"]["count"] == 0                       # no "(n)" suffix -> nothing to crawl


def test_the_runner_dispatches_nprocure_sources_to_this_crawler():
    from crawler import run

    assert run.CRAWLERS["nprocure"] is N.crawl_source
