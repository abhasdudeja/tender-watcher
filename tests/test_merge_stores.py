"""tools/merge_stores.py: how two crawls' copies of a source are merged (the git plumbing is not exercised here)."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("merge_stores", ROOT / "tools" / "merge_stores.py")
ms = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ms)


def rec(i, seen="2026-10-17T05:00:00Z", **kw):
    return {"id": i, "last_seen": seen, "status": "active", "active": True, **kw}


def test_newer_last_seen_wins_either_side():
    old, new = rec("a", "2026-10-15T00:00:00Z", title="old"), rec("a", "2026-10-17T00:00:00Z", title="new")
    assert ms.pick_record(old, new)[0]["title"] == "new"
    assert ms.pick_record(new, old)[0]["title"] == "new"


def test_tie_goes_to_the_side_that_closed_it_then_later_details_then_ours():
    open_, closed = rec("a", title="open"), rec("a", title="closed", status="closed", active=False,
                                                inactive_since="2026-10-17T05:41:16Z")
    assert ms.pick_record(open_, closed)[0]["title"] == "closed"
    assert ms.pick_record(closed, open_)[0]["title"] == "closed"
    a, b = rec("a", title="a", details_fetched_at="2026-10-16T00:00:00Z"), rec("a", title="b", details_fetched_at="2026-10-17T00:00:00Z")
    assert ms.pick_record(a, b)[0]["title"] == "b"
    assert ms.pick_record(rec("a", title="ours"), rec("a", title="theirs"))[0]["title"] == "ours"


def test_winner_with_older_details_is_counted():
    ours = rec("a", "2026-10-17T00:00:00Z", details_fetched_at="2026-10-01T00:00:00Z")
    theirs = rec("a", "2026-10-16T00:00:00Z", details_fetched_at="2026-10-16T00:00:00Z")
    assert ms.pick_record(ours, theirs) == (ours, True)


def test_union_keeps_every_tender_and_counts_sources():
    counts = {}
    merged = ms.merge_stores([rec("1"), rec("2", "2026-10-15T00:00:00Z", title="x")],
                             [rec("2", "2026-10-17T00:00:00Z", title="y"), rec("3")], counts)
    by_id = {r["id"]: r for r in merged}
    assert sorted(by_id) == ["1", "2", "3"] and by_id["2"]["title"] == "y"
    assert counts == {"only_ours": 1, "only_theirs": 1, "both_theirs": 1}


def test_status_file_of_the_later_crawl():
    a = json.dumps({"finished": "2026-10-17T05:00:00Z", "active": 1}).encode()
    b = json.dumps({"finished": "2026-10-17T06:00:00Z", "active": 2}).encode()
    assert ms.pick_status(a, b) == b and ms.pick_status(b, a) == b
    assert ms.pick_status(a, b"not json") == a
