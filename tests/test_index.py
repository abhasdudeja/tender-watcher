import json

from crawler import index


def _stores():
    return {"t": {
        "A": {"id": "A", "source": "t", "title": "live", "org": "O", "active": True, "status": "active", "close": "2999-01-01T10:00"},
        "B": {"id": "B", "source": "t", "title": "gone ok", "org": "O", "active": False, "status": "closed",
              "inactive_since": "2026-10-01T00:00:00Z", "close": "2026-09-30T10:00"},
        "C": {"id": "C", "source": "t", "title": "cancelled", "org": "O", "active": False, "status": "removed"},
        "D": {"id": "D", "source": "t", "title": "legacy inactive, no status", "org": "O", "active": False},
    }}


SRC = [{"slug": "t", "name": "T", "type": "gepnic", "base_url": "https://x"}]


def _load(out, manifest, kind):
    rows = []
    for src in manifest["sources"]:
        for c in src["chunks"]:
            if c["kind"] == kind:
                rows += json.loads((out / c["file"]).read_text(encoding="utf-8"))["tenders"]
    return {r["id"]: r for r in rows}


def test_live_and_closed_tenders_go_to_separate_chunks(tmp_path):
    m = index.build_index(_stores(), SRC, tmp_path)
    live, closed = _load(tmp_path, m, "live"), _load(tmp_path, m, "closed")
    assert set(live) == {"A"} and set(closed) == {"B", "C", "D"}        # nothing dropped, nothing duplicated
    assert "st" not in live["A"]
    assert closed["B"]["st"] == "closed" and closed["B"]["i"] == "2026-10-01T00:00:00Z"
    assert closed["C"]["st"] == "removed" and closed["D"]["st"] == "removed"   # unknown inactive -> removed
    src = m["sources"][0]
    assert (src["active"], src["inactive"], src["total_seen"]) == (1, 3, 4)
    assert {c["kind"] for c in src["chunks"]} == {"live", "closed"}
    assert json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))["format"] == index.FORMAT


def test_manifest_is_small_and_lists_chunk_files_that_exist(tmp_path):
    m = index.build_index(_stores(), SRC, tmp_path)
    assert all((tmp_path / c["file"]).exists() for s in m["sources"] for c in s["chunks"])
    assert "tenders" not in m                                            # rows live only in chunks


def test_chunks_respect_the_row_limit_and_keep_orgs_together(tmp_path, monkeypatch):
    monkeypatch.setattr(index, "CHUNK_ROWS", 5)
    recs = {}
    for org, n in (("Big", 12), ("Mid", 4), ("Small", 2), ("Tiny", 1)):
        for i in range(n):
            recs[f"{org}{i}"] = {"id": f"{org}{i}", "source": "t", "org": org, "active": True, "close": f"2030-01-{i + 1:02d}"}
    m = index.build_index({"t": recs}, SRC, tmp_path)
    chunks = m["sources"][0]["chunks"]
    assert all(c["rows"] <= 5 for c in chunks) and sum(c["rows"] for c in chunks) == 19
    assert set(_load(tmp_path, m, "live")) == set(recs)
    # Mid(4) is not split across chunks, and neither is Small+Tiny packed apart from each other needlessly
    where = {}
    for c in chunks:
        for r in json.loads((tmp_path / c["file"]).read_text())["tenders"]:
            where.setdefault(r["o"], set()).add(c["file"])
    assert len(where["Mid"]) == 1 and len(where["Small"]) == 1


def test_stale_chunk_files_are_removed_and_output_is_deterministic(tmp_path, monkeypatch):
    monkeypatch.setattr(index, "CHUNK_ROWS", 2)
    big = {f"X{i}": {"id": f"X{i}", "source": "t", "org": "O", "active": True} for i in range(6)}
    index.build_index({"t": big}, SRC, tmp_path)
    assert len(list((tmp_path / "index").glob("t-live-*.json"))) == 3
    small = {k: v for k, v in list(big.items())[:2]}
    m = index.build_index({"t": small}, SRC, tmp_path)
    assert [p.name for p in (tmp_path / "index").glob("*.json")] == ["t-live-0.json"]   # old chunks gone
    first = (tmp_path / "index" / "t-live-0.json").read_text()
    index.build_index({"t": small}, SRC, tmp_path)
    assert (tmp_path / "index" / "t-live-0.json").read_text() == first
    assert m["sources"][0]["active"] == 2


def test_source_without_data_gets_an_empty_entry(tmp_path):
    m = index.build_index({}, SRC, tmp_path)
    assert m["sources"][0]["chunks"] == [] and m["sources"][0]["active"] == 0


# ---------------------------------------------------------------- format 3: orgs.json, presets, stats
from datetime import datetime, timezone  # noqa: E402

NOW = datetime(2026, 10, 13, 6, 0, tzinfo=timezone.utc)          # 11:30 IST on 13 Oct 2026


def _rec(i, org="O", **kw):
    rec = {"id": i, "source": "t", "title": i, "org": org, "active": True, "status": "active",
           "first_seen": "2026-08-01T00:00:00Z", "pub": "2026-08-01", "close": "2026-12-31T10:00"}
    rec.update(kw)
    return rec


def _load_file(out, rel):
    return json.loads((out / rel).read_text(encoding="utf-8"))


def test_orgs_json_lists_every_org_with_counts_files_and_categories(tmp_path, monkeypatch):
    monkeypatch.setattr(index, "CHUNK_ROWS", 3)
    recs = {r["id"]: r for r in [_rec("a1", "Alpha", category="Works"), _rec("a2", "Alpha", category="Works"),
                                 _rec("b1", "Beta", category="Goods"), _rec("b2", "Beta"),
                                 _rec("g1", "Beta", active=False, status="closed", inactive_since="2026-10-01T00:00:00Z")]}
    m = index.build_index({"t": recs}, SRC, tmp_path, now=NOW)
    doc = _load_file(tmp_path, m["orgs"])
    by_org = {o["o"]: o for o in doc["orgs"]}
    assert doc["format"] == index.FORMAT and set(by_org) == {"Alpha", "Beta"}
    assert (by_org["Alpha"]["n"], by_org["Alpha"]["c"], by_org["Beta"]["n"], by_org["Beta"]["c"]) == (2, 0, 2, 1)
    assert all(o["s"] == "t" for o in doc["orgs"])
    assert doc["categories"] == [{"k": "Works", "n": 2}, {"k": "Goods", "n": 1}]
    # every listed file exists and really holds that org's rows
    for o in doc["orgs"]:
        rows = [r for f in o["files"] for r in _load_file(tmp_path, f)["tenders"] if r.get("o") == o["o"]]
        assert len(rows) == o["n"] + o["c"] and o["files"]


def test_new_preset_is_what_was_published_in_the_last_week(tmp_path, monkeypatch):
    monkeypatch.setattr(index, "NEW_DAYS", 7)
    monkeypatch.setattr(index, "CLOSING_DAYS", 7)
    monkeypatch.setattr(index, "CLOSED_DAYS", 14)
    recs = {r["id"]: r for r in [
        _rec("old", pub="2026-09-01"), _rec("edge", pub="2026-10-06"), _rec("today", pub="2026-10-13T09:00"),
        _rec("closed_new", pub="2026-10-12", active=False, status="closed", inactive_since="2026-10-12T00:00:00Z")]}
    m = index.build_index({"t": recs}, SRC, tmp_path, now=NOW)
    ids = [r["id"] for r in _load_file(tmp_path, m["presets"]["new"]["file"])["tenders"]]
    assert ids == ["today", "edge"]                                        # newest first; old and closed rows are not "new"
    assert m["presets"]["new"] == {"file": "presets/new.json", "rows": 2, "days": 7}
    assert m["sources"][0]["stats"]["new"] == 2


def test_a_source_crawled_for_the_first_time_this_week_has_no_first_seen_signal(tmp_path):
    fresh = {r["id"]: r for r in [_rec("x", pub="", first_seen="2026-10-12T20:00:00Z"), _rec("y", pub="", first_seen="2026-10-12T20:00:00Z")]}
    for r in fresh.values():
        r.pop("pub")
    m = index.build_index({"t": fresh}, SRC, tmp_path, now=NOW)
    assert _load_file(tmp_path, m["presets"]["new"]["file"])["tenders"] == []
    established = dict(fresh)
    established["z"] = {k: v for k, v in _rec("z", first_seen="2026-10-12T20:00:00Z").items() if k != "pub"}
    established["w"] = {k: v for k, v in _rec("w", first_seen="2026-07-01T00:00:00Z").items() if k != "pub"}
    m = index.build_index({"t": established}, SRC, tmp_path, now=NOW)
    assert {r["id"] for r in _load_file(tmp_path, m["presets"]["new"]["file"])["tenders"]} == {"x", "y", "z"}   # w was seen long ago


def test_closing_preset_is_the_next_seven_days_soonest_first(tmp_path, monkeypatch):
    monkeypatch.setattr(index, "NEW_DAYS", 7)
    monkeypatch.setattr(index, "CLOSING_DAYS", 7)
    monkeypatch.setattr(index, "CLOSED_DAYS", 14)
    recs = {r["id"]: r for r in [
        _rec("past", close="2026-10-13T10:00"), _rec("soon", close="2026-10-13T18:00"), _rec("week", close="2026-10-20T11:00"),
        _rec("later", close="2026-10-21T10:00"), _rec("none", close="")]}
    recs["none"].pop("close")
    m = index.build_index({"t": recs}, SRC, tmp_path, now=NOW)
    assert [r["id"] for r in _load_file(tmp_path, m["presets"]["closing"]["file"])["tenders"]] == ["soon", "week"]


def test_closed_recent_preset_is_what_dropped_off_in_the_last_two_weeks(tmp_path, monkeypatch):
    monkeypatch.setattr(index, "NEW_DAYS", 7)
    monkeypatch.setattr(index, "CLOSING_DAYS", 7)
    monkeypatch.setattr(index, "CLOSED_DAYS", 14)
    recs = {r["id"]: r for r in [
        _rec("r1", active=False, status="closed", inactive_since="2026-10-12T00:00:00Z"),
        _rec("r2", active=False, status="removed", inactive_since="2026-10-01T00:00:00Z"),
        _rec("old", active=False, status="closed", inactive_since="2026-08-01T00:00:00Z"),
        _rec("unknown", active=False, status="closed")]}
    m = index.build_index({"t": recs}, SRC, tmp_path, now=NOW)
    rows = _load_file(tmp_path, m["presets"]["closed_recent"]["file"])["tenders"]
    assert [r["id"] for r in rows] == ["r1", "r2"] and rows[0]["st"] == "closed" and rows[1]["st"] == "removed"


def test_presets_are_capped_and_say_so(tmp_path, monkeypatch):
    monkeypatch.setattr(index, "PRESET_MAX", 3)
    monkeypatch.setattr(index, "NEW_DAYS", 7)
    recs = {f"n{i}": _rec(f"n{i}", pub=f"2026-10-{8 + i % 5:02d}") for i in range(10)}
    m = index.build_index({"t": recs}, SRC, tmp_path, now=NOW)
    doc = _load_file(tmp_path, m["presets"]["new"]["file"])
    assert len(doc["tenders"]) == 3 and doc["truncated"] == 10 and m["presets"]["new"]["truncated"] == 10
    assert all(r["p"] >= "2026-10-11" for r in doc["tenders"])            # the newest ones are the ones kept
    assert m["sources"][0]["stats"]["new"] == 10                          # the per-source stats are the full count, not the file's


def test_stale_preset_and_chunk_files_are_removed_and_format3_is_deterministic(tmp_path):
    (tmp_path / "presets").mkdir()
    (tmp_path / "presets" / "leftover.json").write_text("{}")
    m1 = index.build_index(_stores(), SRC, tmp_path, now=NOW)
    first = {p.name: p.read_bytes() for p in sorted(tmp_path.rglob("*.json"))}
    m2 = index.build_index(_stores(), SRC, tmp_path, now=NOW)
    assert not (tmp_path / "presets" / "leftover.json").exists() and m1 == m2
    assert first == {p.name: p.read_bytes() for p in sorted(tmp_path.rglob("*.json"))}


def test_a_source_without_data_still_has_stats_and_the_presets_exist(tmp_path):
    m = index.build_index({}, SRC, tmp_path, now=NOW)
    assert m["sources"][0]["stats"] == {"new": 0, "closing": 0, "closed_recent": 0, "closing48": 0, "orgs": 0}
    assert all((tmp_path / p["file"]).exists() for p in m["presets"].values()) and (tmp_path / "orgs.json").exists()


def test_write_status_merges_by_default_and_can_replace(tmp_path):
    index.write_status([{"slug": "a", "ok": True}, {"slug": "gone", "ok": False}], tmp_path)
    index.write_status([{"slug": "a", "ok": False}], tmp_path)                      # merge: "gone" survives, "a" is updated
    st = {s["slug"]: s for s in json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))["sources"]}
    assert set(st) == {"a", "gone"} and st["a"]["ok"] is False
    index.write_status([{"slug": "a", "ok": True}], tmp_path, replace=True)         # replace: only what is passed
    st = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))["sources"]
    assert [s["slug"] for s in st] == ["a"]


def test_manifest_carries_the_state_and_the_map_stats(tmp_path):
    recs = {r["id"]: r for r in [
        _rec("a", "Alpha", close="2026-10-14T10:00"),                       # within 48 h of NOW (13 Oct 11:30 IST)
        _rec("b", "Alpha", close="2026-10-15T10:00"),                       # 46.5 h away: inside
        _rec("c", "Beta", close="2026-10-15T14:00"),                        # 50.5 h away: outside
        _rec("d", "Beta", close="2026-10-12T10:00"),                        # already past
        _rec("g", "Gamma", active=False, status="closed", close="2026-10-14T10:00", inactive_since="2026-10-12T00:00:00Z"),
    ]}
    sources = [dict(SRC[0], state="IN-KL"), {"slug": "u", "name": "U", "type": "gepnic", "base_url": "https://y"}]
    m = index.build_index({"t": recs}, sources, tmp_path, now=NOW)
    t, u = m["sources"]
    assert t["state"] == "IN-KL" and "state" not in u                         # only sources that declare one
    assert t["stats"]["closing48"] == 2                                      # closed tenders and past deadlines do not count
    assert t["stats"]["orgs"] == 2                                           # Alpha and Beta have active tenders; Gamma only a closed one
    assert u["stats"]["closing48"] == 0 and u["stats"]["orgs"] == 0


def test_manifest_counts_listed_tenders_that_have_their_details(tmp_path):
    stores = {"t": {
        "A": {"id": "A", "source": "t", "title": "with", "org": "O", "active": True, "details_fetched_at": "2026-10-01T00:00:00Z"},
        "B": {"id": "B", "source": "t", "title": "without", "org": "O", "active": True, "needs_details": True},
        "C": {"id": "C", "source": "t", "title": "gone", "org": "O", "active": False, "details_fetched_at": "2026-10-01T00:00:00Z"},
    }}
    src = dict(SRC[0], fetch_details=True)
    m = index.build_index(stores, [src], tmp_path)
    assert (m["sources"][0]["active"], m["sources"][0]["with_details"]) == (2, 1)      # closed tenders are not counted
    m = index.build_index(stores, SRC, tmp_path)                                       # a listing-only source has no such number
    assert "with_details" not in m["sources"][0]
    listing_only = {"t": {"A": {"id": "A", "source": "t", "title": "x", "org": "O", "active": True}}}
    m = index.build_index(listing_only, [src], tmp_path)                               # fetch_details on, but no detail page ever stored
    assert "with_details" not in m["sources"][0]


def test_a_tenders_own_link_goes_into_its_row_as_u(tmp_path):
    stores = {"t": {"A": {"id": "A", "source": "t", "title": "x", "org": "O", "active": True, "link": "https://p.gov.in/n.pdf"},
                    "B": {"id": "B", "source": "t", "title": "y", "org": "O", "active": True}}}
    m = index.build_index(stores, SRC, tmp_path)
    live = _load(tmp_path, m, "live")
    assert live["A"]["u"] == "https://p.gov.in/n.pdf" and "u" not in live["B"]
