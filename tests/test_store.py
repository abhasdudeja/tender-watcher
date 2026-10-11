import json
import os
import time
from pathlib import Path

import pytest

from crawler import store as S

OLD = time.time() - 3600


def rec(i, org="Org A", **kw):
    r = {"id": f"T_{i}", "source": "k", "ref": f"R/{i}", "title": f"Tender {i} ₹", "org": org,
         "chain": f"{org}||Sub", "pub": "2026-10-01T10:00", "close": "2026-10-20T15:00", "open": "2026-10-21T11:00",
         "first_seen": "2026-10-01T00:00:00Z", "last_seen": "2026-10-02T00:00:00Z", "active": True, "status": "active"}
    r.update(kw)
    return r


FULL = dict(category="Goods", product_category="Vehicles", value=35000000.0, emd=350000.0, location="TVM",
            pincode="695001", bid_end="2026-10-20T15:00", contract_type="Supply", period="180",
            authority="CME", authority_address="Addr", description="desc", documents=["a.pdf", "b.xls"],
            details={"Tender ID": "T_1", "Tender Value in ₹": "3,50,00,000"}, details_fetched_at="2026-10-02T01:00:00Z",
            needs_details=True)


def write_jsonl(path: Path, records, age=3600):
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in records) + "\n", encoding="utf-8")
    os.utime(path, (time.time() - age, time.time() - age))


# ---------------------------------------------------------------- round trip
def test_round_trip_is_lossless_including_unknown_and_mistyped_fields(tmp_path):
    odd = rec(3, value="NA", brand_new_field={"x": [1, 2]}, documents=[])          # str in a float col, unknown key, empty list
    st = S.SourceStore(tmp_path / "k", [rec(1, **FULL), rec(2), odd, rec(4, org="Org B"), rec(5, org=None)])
    st.save()
    back = S.SourceStore.load_dir(tmp_path / "k")
    assert S.compare_stores(dict(st), dict(back)) == []
    assert back["T_3"]["value"] == "NA" and back["T_3"]["brand_new_field"] == {"x": [1, 2]}
    assert back["T_3"]["documents"] == []                    # empty list stays distinct from "missing"
    assert "documents" not in back["T_2"]
    assert back["T_1"]["details"]["Tender Value in ₹"] == "3,50,00,000"
    assert back["T_1"]["value"] == 35000000.0 and back["T_1"]["needs_details"] is True


def test_none_values_are_dropped_and_ints_become_floats(tmp_path):
    st = S.SourceStore(tmp_path / "k", [rec(1, category=None, value=5)])
    st.save()
    back = S.SourceStore.load_dir(tmp_path / "k")["T_1"]
    assert "category" not in back and back["value"] == 5.0 and isinstance(back["value"], float)


def test_one_shard_per_org_with_safe_unique_names(tmp_path):
    names = [S.shard_name(o) for o in ("A/B", "A-B", "a b", None, "", "Local Self Government Department", "x" * 500)]
    assert len(set(names[:3])) == 3                          # same slug stem, different hash
    assert names[3] == names[4] == "_unassigned"
    assert all(len(n) <= 70 and all(c.isalnum() or c in "-_" for c in n) for n in names)
    st = S.SourceStore(tmp_path / "k", [rec(1, org="A"), rec(2, org="A"), rec(3, org="B")])
    st.save()
    assert sorted(p.stem for p in (tmp_path / "k").glob("*.parquet")) == sorted([S.shard_name("A"), S.shard_name("B")])
    assert json.loads((tmp_path / "k" / "_meta.json").read_text())["format"] == S.FORMAT_VERSION


def test_only_changed_shards_are_rewritten(tmp_path):
    st = S.SourceStore(tmp_path / "k", [rec(1, org="A"), rec(2, org="B")])
    assert st.save() == 2
    assert st.save() == 0                                    # nothing changed -> no files touched
    st["T_1"]["title"] = "edited"
    assert st.save(org="B") == 0                             # cheap per-org checkpoint of an untouched org
    assert st.save(org="A") == 1
    st["T_2"]["org"] = "A"                                   # tender moves org: B's shard empties out
    assert st.save() == 1
    assert [p.stem for p in (tmp_path / "k").glob("*.parquet")] == [S.shard_name("A")]
    assert set(S.SourceStore.load_dir(tmp_path / "k")) == {"T_1", "T_2"}   # nothing lost


def test_duplicate_id_across_shards_keeps_the_newer(tmp_path):
    d = tmp_path / "k"
    S.SourceStore(d, [rec(1, org="A", last_seen="2026-10-01T00:00:00Z", title="old")]).save()
    S.SourceStore(d, [rec(1, org="B", last_seen="2026-10-09T00:00:00Z", title="new")]).save(org="B")
    assert S.SourceStore.load_dir(d)["T_1"]["title"] == "new"


# ---------------------------------------------------------------- migration
def test_migration_verifies_moves_legacy_aside_and_is_idempotent(tmp_path):
    recs = [rec(1, **FULL), rec(2), rec(3, org="Org B", value="NA"), rec(4, org=None)]
    write_jsonl(tmp_path / "k.jsonl", recs)
    info = S.migrate_jsonl(tmp_path, "k")
    assert info["verified"] and info["records"] == 4 and info["shards"] == 3
    assert not (tmp_path / "k.jsonl").exists() and (tmp_path / "_legacy" / "k.jsonl").exists()
    assert not (tmp_path / "k.migrating").exists()
    meta = json.loads((tmp_path / "k" / "_meta.json").read_text())
    assert meta["format"] == S.FORMAT_VERSION and meta["migrated_from"] == "jsonl" and meta["records"] == 4
    assert S.compare_stores({r["id"]: r for r in recs}, S.open_source(tmp_path, "k")) == []
    assert S.migrate_if_needed(tmp_path, "k") is None        # nothing left to do


def test_open_source_migrates_automatically(tmp_path):
    write_jsonl(tmp_path / "k.jsonl", [rec(1), rec(2, org="B")])
    st = S.open_source(tmp_path, "k")
    assert set(st) == {"T_1", "T_2"} and (tmp_path / "k" / "_meta.json").exists()


def test_failed_verification_changes_nothing(tmp_path, monkeypatch):
    write_jsonl(tmp_path / "k.jsonl", [rec(1), rec(2)])
    real = S.SourceStore.load_dir.__func__

    def lossy(cls, d):
        out = real(cls, d)
        out.pop("T_2")                                       # simulate a corrupted write
        return out

    monkeypatch.setattr(S.SourceStore, "load_dir", classmethod(lossy))
    with pytest.raises(S.StoreError, match="FAILED"):
        S.migrate_jsonl(tmp_path, "k")
    assert (tmp_path / "k.jsonl").exists()                   # original untouched
    assert not (tmp_path / "k").exists() and not (tmp_path / "k.migrating").exists()
    assert not (tmp_path / "_legacy").exists()


def test_dry_run_leaves_everything_alone(tmp_path):
    write_jsonl(tmp_path / "k.jsonl", [rec(1), rec(2)])
    info = S.migrate_jsonl(tmp_path, "k", dry_run=True)
    assert info["verified"] and info["dry_run"]
    assert (tmp_path / "k.jsonl").exists() and not (tmp_path / "k").exists()
    assert not (tmp_path / "k.migrating").exists()


# ---------------------------------------------------------------- never while a crawl is active
def test_migration_refuses_a_recently_written_legacy_file(tmp_path):
    write_jsonl(tmp_path / "k.jsonl", [rec(1)], age=5)       # written 5s ago: an old-style crawl may be saving it
    with pytest.raises(S.CrawlActive, match="may still be writing"):
        S.migrate_jsonl(tmp_path, "k")
    with pytest.raises(S.CrawlActive):
        S.open_source(tmp_path, "k")                         # auto-migrate on open is guarded too
    assert (tmp_path / "k.jsonl").exists() and not (tmp_path / "k").exists()


def test_migration_refuses_while_a_live_crawl_holds_the_lock(tmp_path):
    write_jsonl(tmp_path / "k.jsonl", [rec(1)])              # old enough
    (tmp_path / S.LOCK_FILE).write_text(json.dumps({"pid": os.getppid()}))   # parent process is alive
    with pytest.raises(S.CrawlActive, match="crawl is running"):
        S.migrate_jsonl(tmp_path, "k")


def test_stale_lock_from_a_dead_process_is_ignored(tmp_path):
    write_jsonl(tmp_path / "k.jsonl", [rec(1)])
    (tmp_path / S.LOCK_FILE).write_text(json.dumps({"pid": 2 ** 22 + 12345}))
    assert S.migrate_jsonl(tmp_path, "k")["verified"]


def test_crawl_lock_blocks_a_second_crawl_and_releases(tmp_path):
    (tmp_path / S.LOCK_FILE).write_text(json.dumps({"pid": os.getppid()}))
    with pytest.raises(S.CrawlActive):
        S.acquire_crawl_lock(tmp_path)
    (tmp_path / S.LOCK_FILE).unlink()
    S.acquire_crawl_lock(tmp_path)
    S.release_crawl_lock(tmp_path)
    assert not (tmp_path / S.LOCK_FILE).exists()


def test_newer_format_is_refused(tmp_path):
    (tmp_path / "k").mkdir()
    (tmp_path / "k" / "_meta.json").write_text(json.dumps({"format": S.FORMAT_VERSION + 1}))
    with pytest.raises(S.StoreError, match="newer"):
        S.open_source(tmp_path, "k")
