from pathlib import Path

from crawler import gepnic

FIX = Path(__file__).parent / "fixtures"
BASE = "https://etenders.kerala.gov.in/nicgep/app"


def test_org_list():
    orgs = gepnic.parse_org_list((FIX / "gepnic_org_list.html").read_text(), BASE)
    assert [o["name"] for o in orgs] == [
        "Agency for Development of Aquaculture Kerala", "ANERT", "Local Self Government Department"]
    assert orgs[2]["count"] == 4957
    assert orgs[0]["url"].startswith(BASE + "?component=")


def test_tender_list():
    page = gepnic.parse_tender_list((FIX / "gepnic_tender_list.html").read_text(), BASE)
    rows = page["rows"]
    assert len(rows) == 2
    r = rows[1]
    assert r["id"] == "2026_KSRTC_812400_1"
    assert r["ref"] == "KSRTC/EV/2026/07"
    assert r["title"] == "Supply of 2 nos electric buses"
    assert r["pub"] == "2026-10-08T10:30"
    assert r["close"] == "2026-10-20T15:00"
    assert r["open"] == "2026-10-21T11:00"
    assert r["chain"].startswith("Kerala State Road Transport")
    assert page["next_url"] and "Snextpage" in page["next_url"]


def test_tender_detail():
    d = gepnic.parse_tender_detail((FIX / "gepnic_tender_detail.html").read_text())
    assert d["category"] == "Goods"
    assert d["product_category"] == "Vehicles/Vehicle Spares"
    assert d["value"] == 35000000.0
    assert d["emd"] == 350000.0
    assert d["location"] == "Thiruvananthapuram"
    assert d["pincode"] == "695001"
    assert d["bid_end"] == "2026-10-20T15:00"
    assert d["authority"] == "Chief Mechanical Engineer"
    assert d["period"] == "180"
    assert d["documents"] == ["BOQ_123.xls", "Tenderdocument.pdf"]
    assert "Work Description" in d["details"]


def test_dates():
    assert gepnic.parse_date("17-Oct-2026 06:55 PM") == "2026-10-17T18:55"
    assert gepnic.parse_date("17-Oct-2026") == "2026-10-17"
    assert gepnic.parse_date("") is None
    assert gepnic.parse_money("Rs. 12,34,567.50") == 1234567.5


# ---- fixtures captured from the live Kerala portal (trimmed) ----
def test_real_tender_list():
    """Real markup: <tr class="list_header">, title wrapped in [ ], no Next link
    (the portal returns a whole organisation in one response)."""
    page = gepnic.parse_tender_list((FIX / "gepnic_real_tender_list.html").read_text(encoding="utf-8"), BASE)
    rows = page["rows"]
    assert len(rows) == 3
    r = rows[0]
    assert r["id"] == "2026_ADAK_867976_3"
    assert r["ref"] == "ADAK/RE.NZ/127/2026"
    assert r["title"] == "Second Retender for the Supply Of Medium Protein Fish Feed"
    assert r["pub"] == "2026-10-07T12:00"
    assert r["close"] == "2026-10-15T14:00"
    assert r["open"] == "2026-10-16T15:00"
    assert r["chain"] == "Agency for Development of Aquaculture Kerala||Regional Office North Zone Eranholi"
    assert r["detail_url"].startswith(BASE + "?component=")
    assert page["next_url"] is None


def test_real_tender_detail():
    d = gepnic.parse_tender_detail((FIX / "gepnic_real_tender_detail.html").read_text(encoding="utf-8"))
    assert d["category"] == "Goods"
    assert d["product_category"] == "Consumables"
    assert "value" not in d              # 'Tender Value in ₹' is 'NA' on this tender
    assert d["emd"] == 44800.0
    assert d["location"] == "ADAK, North Region"
    assert d["pincode"] == "670107"
    assert d["bid_end"] == "2026-10-15T14:00"
    assert d["contract_type"] == "Tender"            # 'Contract Type'
    assert d["form_of_contract"] == "Item Rate"      # 'Form Of Contract' is a separate caption
    assert d["tender_type"] == "Open Tender"
    assert d["period"] == "180"
    assert d["authority"] == "REGIONAL EXECUTIVE"
    assert d["documents"] == ["Tendernotice_1.pdf"]


# ---- robustness ported from the original implementation ----
def test_detail_fallback_for_pages_without_caption_classes():
    html = "<html><body><table><tr><td>Tender Category</td><td>Works</td></tr><tr><td>Location</td><td>Kochi</td></tr></table></body></html>"
    d = gepnic.parse_tender_detail(html)
    assert d["category"] == "Works" and d["location"] == "Kochi" and d["details"]["Location"] == "Kochi"


def test_detail_page_with_no_pairs_has_empty_details():
    assert gepnic.parse_tender_detail("<html><body><p>nothing</p></body></html>").get("details") in (None, {})


def test_captcha_wall_and_stale_detection():
    from crawler.http import is_stale

    assert gepnic.looks_like_captcha_wall("<html><body>Enter the CAPTCHA to continue</body></html>")
    assert not gepnic.looks_like_captcha_wall((FIX / "gepnic_tender_detail.html").read_text())
    assert is_stale("<html><head><title>Stale Session</title></head></html>")
    assert is_stale("<html><body>Stale Link</body></html>")
    assert not is_stale("<html><body>" + "x" * 5000 + "Stale Session in a far-away tender description</body></html>")
