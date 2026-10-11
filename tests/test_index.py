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
