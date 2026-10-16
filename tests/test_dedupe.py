"""Duplicates between an aggregator source (prefer_others) and the other sources, and their exclusion from the index."""
import json
from datetime import datetime, timezone

from crawler import dedupe, index

SOURCES = [{"slug": "kerala", "name": "Kerala", "type": "gepnic", "base_url": "https://k", "state": "IN-KL"},
           {"slug": "jk", "name": "J&K", "type": "gepnic", "base_url": "https://j", "state": "IN-JK"},
           {"slug": "gemcppp-state", "name": "GeM state", "type": "gemcppp", "base_url": "https://g", "state": "multi", "prefer_others": True}]
TITLE = "Supply of ready to use potholes repair material for all weather resilience"


def rec(i, **kw):
    base = {"id": i, "source": "x", "org": "Org", "active": True, "status": "active", "close": "2026-10-29T14:00", "ref": "", "title": ""}
    return {**base, **kw}


def stores(own=None, kerala=None, jk=None):
    return {"kerala": {r["id"]: r for r in (kerala or [])}, "jk": {r["id"]: r for r in (jk or [])},
            "gemcppp-state": {r["id"]: r for r in (own or [])}}


def found(own, **others):
    return dedupe.find_duplicates(stores(own=own, **others), SOURCES)["gemcppp-state"]


def test_a_gepnic_id_held_by_another_source_is_a_duplicate_even_with_another_closing_date():
    f = found([rec("2026_TDB_879402_1", close="2026-10-30T10:00")], kerala=[rec("2026_TDB_879402_1")])
    assert f == {"2026_TDB_879402_1": ("id", "kerala:2026_TDB_879402_1")}


def test_a_numeric_id_must_also_agree_on_the_closing_day():
    same = found([rec("92749798")], jk=[rec("92749798")])
    other_day = found([rec("92749798")], jk=[rec("92749798", close="2026-12-01T10:00")])
    assert "92749798" in same and other_day == {}


def test_the_same_reference_and_closing_day_is_a_duplicate_a_different_day_is_not():
    own = [rec("a1", ref="NIT/12/2026-27")]
    assert found(own, kerala=[rec("k1", ref="nit 12 2026 27")]) == {"a1": ("ref", "kerala:k1")}
    assert found(own, kerala=[rec("k1", ref="NIT/12/2026-27", close="2026-11-30T10:00")]) == {}


def test_a_gepnic_id_that_is_not_held_elsewhere_is_a_different_tender_whatever_its_title_says():
    own = [rec("2026_A_5_1", ref="NIT/12/2026-27", title=TITLE + " rapid maintenance")]
    other = [rec("2026_B_9_1", ref="NIT/12/2026-27", title=TITLE + " rapid maintenance")]
    assert found(own, kerala=other) == {}


def test_a_reference_that_is_only_a_year_is_not_distinctive():
    assert found([rec("a1", ref="2026-27")], kerala=[rec("k1", ref="2026-27")]) == {}
    assert found([rec("a1", ref="2026-2027")], kerala=[rec("k1", ref="2026-2027")]) == {}


def test_a_short_common_reference_alone_is_not_enough():
    assert found([rec("a1", ref="NIT-1")], kerala=[rec("k1", ref="NIT-1")]) == {}


def test_the_same_title_and_closing_day_is_a_duplicate():
    own = [rec("a1", title=TITLE + " rapid maintenance")]
    assert found(own, jk=[rec("j1", title=TITLE.upper() + " Rapid  Maintenance")]) == {"a1": ("title", "jk:j1")}
    assert found([rec("a1", title="Short title")], jk=[rec("j1", title="Short title")]) == {}


def test_only_active_tenders_of_other_sources_count_and_the_aggregator_never_matches_itself():
    inactive = rec("2026_X_1_1", active=False, status="closed")
    assert found([rec("2026_X_1_1")], kerala=[inactive]) == {}
    twin = [rec("2026_Y_2_1"), rec("2026_Y_2_1b", ref="whatever-ref", title=TITLE + " one")]
    assert found(twin) == {}


def test_no_aggregator_means_nothing_to_find():
    assert dedupe.find_duplicates(stores(), [s for s in SOURCES if not s.get("prefer_others")]) == {}


def test_the_report_counts_what_overlaps():
    own = [rec("2026_A_1_1"), rec("2026_A_2_1"), rec("2026_A_3_1")]
    lines = dedupe.report(stores(own=own, kerala=[rec("2026_A_1_1"), rec("2026_A_2_1")]), SOURCES)
    assert lines[0].startswith("gemcppp-state: 3 active, 2 duplicates (2 by id), 1 only here")
    assert "duplicates of: kerala 2" in lines[1]


def test_the_index_leaves_duplicates_out_and_says_how_many():
    own = [rec("2026_A_1_1", title="dup"), rec("2026_A_9_1", title="only here")]
    st = stores(own=own, kerala=[rec("2026_A_1_1", title="dup")])
    out = index.build_index(st, SOURCES, __import__("pathlib").Path(__import__("tempfile").mkdtemp()),
                            now=datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc))
    by = {s["slug"]: s for s in out["sources"]}
    assert by["gemcppp-state"]["active"] == 1 and by["gemcppp-state"]["duplicates"] == 1 and by["gemcppp-state"]["total_seen"] == 2
    assert by["kerala"]["active"] == 1 and "duplicates" not in by["kerala"]
