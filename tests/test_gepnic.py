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
