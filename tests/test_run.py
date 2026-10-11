import json
import os
import threading
import time
from pathlib import Path

import yaml

from crawler import run, store as S


def make_env(tmp_path, slugs=("a", "b")):
    cfg = tmp_path / "sources.yaml"
    cfg.write_text(yaml.safe_dump({"sources": [
        {"slug": s, "name": s.upper(), "type": "fake", "base_url": "https://x/" + s, "enabled": True} for s in slugs]}))
    return ["--config", str(cfg), "--data-dir", str(tmp_path / "data"), "--site-data-dir", str(tmp_path / "site")]


def fake_crawler(calls=None, barrier=None, running=None):
    def crawl(src, st, checkpoint=None, progress=None, stop=None, **kw):
        slug = src["slug"]
        if running is not None:
            with running["lock"]:
                running["now"] += 1
                running["max"] = max(running["max"], running["now"])
        if calls is not None:
            calls.append(slug)
        try:
            if barrier is not None:
                barrier.wait(timeout=10)                     # only returns if the other source runs at the same time
            time.sleep(0.05)
            st[f"{slug}1"] = {"id": f"{slug}1", "source": slug, "org": "Org " + slug, "title": "t", "active": True,
                              "status": "active", "close": "2999-01-01T00:00"}
            if checkpoint:
                checkpoint("Org " + slug)
            return {"slug": slug, "name": slug.upper(), "ok": True, "listed": 1, "errors": []}
        except threading.BrokenBarrierError:
            return {"slug": slug, "name": slug, "ok": False, "errors": ["not parallel"]}
        finally:
            if running is not None:
                with running["lock"]:
                    running["now"] -= 1
    return crawl


def test_sources_are_crawled_in_parallel_and_everything_is_written(tmp_path, monkeypatch):
    monkeypatch.setitem(run.CRAWLERS, "fake", fake_crawler(barrier=threading.Barrier(2)))
    assert run.main(make_env(tmp_path)) == 0               # a sequential run would break the barrier -> rc 1
    for s in "ab":
        assert list((tmp_path / "data" / s).glob("*.parquet")), s
        assert json.loads((tmp_path / "data" / s / run.STATUS_FILE).read_text())["ok"] is True
    manifest = json.loads((tmp_path / "site" / "index.json").read_text())
    assert [x["slug"] for x in manifest["sources"]] == ["a", "b"] and all(x["active"] == 1 for x in manifest["sources"])
    status = json.loads((tmp_path / "site" / "status.json").read_text())
    assert [x["slug"] for x in status["sources"]] == ["a", "b"]
    assert not (tmp_path / "site" / run.PROGRESS_FILE).exists()     # transient files are cleaned up
    assert not (tmp_path / "data" / S.LOCK_FILE).exists()


def test_parallel_one_runs_sources_one_at_a_time(tmp_path, monkeypatch):
    running = {"lock": threading.Lock(), "now": 0, "max": 0}
    monkeypatch.setitem(run.CRAWLERS, "fake", fake_crawler(running=running))
    assert run.main(make_env(tmp_path, ("a", "b", "c")) + ["--parallel", "1"]) == 0
    assert running["max"] == 1
    running.update(now=0, max=0)
    assert run.main(make_env(tmp_path, ("a", "b", "c"))) == 0
    assert running["max"] >= 2                                       # default: all sources overlap


def test_a_live_lock_refuses_the_crawl_without_touching_anything(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setitem(run.CRAWLERS, "fake", fake_crawler(calls))
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / S.LOCK_FILE).write_text(json.dumps({"pid": os.getppid()}))   # parent process: alive
    assert run.main(make_env(tmp_path)) == 2
    assert calls == [] and not (tmp_path / "site").exists()
    assert (tmp_path / "data" / S.LOCK_FILE).exists()                # the other crawl's lock is left alone


def test_no_index_flag_leaves_the_site_alone(tmp_path, monkeypatch):
    monkeypatch.setitem(run.CRAWLERS, "fake", fake_crawler())
    assert run.main(make_env(tmp_path) + ["--no-index"]) == 0
    assert list((tmp_path / "data" / "a").glob("*.parquet")) and not (tmp_path / "site" / "index.json").exists()
    assert run.main(["--index-only"] + make_env(tmp_path)) == 0      # the separate publish step builds it
    assert (tmp_path / "site" / "index.json").exists() and (tmp_path / "site" / "status.json").exists()


def _legacy(tmp_path, age):
    (tmp_path / "data").mkdir(exist_ok=True)
    p = tmp_path / "data" / "a.jsonl"
    p.write_text(json.dumps({"id": "old1", "source": "a", "org": "Org a", "active": True, "title": "legacy"}) + "\n")
    os.utime(p, (time.time() - age, time.time() - age))
    return p


def test_old_jsonl_is_migrated_automatically_and_kept_in_legacy(tmp_path, monkeypatch):
    p = _legacy(tmp_path, age=3600)
    monkeypatch.setitem(run.CRAWLERS, "fake", fake_crawler())
    assert run.main(make_env(tmp_path, ("a",))) == 0
    assert not p.exists() and (tmp_path / "data" / "_legacy" / "a.jsonl").exists()
    st = S.open_source(tmp_path / "data", "a")
    assert set(st) == {"old1", "a1"}                                 # continuity: old data + the new crawl


def test_recently_written_jsonl_is_never_migrated_by_a_crawl(tmp_path, monkeypatch):
    p = _legacy(tmp_path, age=5)                                     # an old-style crawl might still be writing it
    calls = []
    monkeypatch.setitem(run.CRAWLERS, "fake", fake_crawler(calls))
    assert run.main(make_env(tmp_path, ("a",))) == 2
    assert p.exists() and not (tmp_path / "data" / "a").exists() and calls == []
    assert not (tmp_path / "site" / "index.json").exists()           # and no index from a partial picture
    assert not (tmp_path / "data" / S.LOCK_FILE).exists()


def test_index_only_reads_unmigrated_data_without_changing_it(tmp_path):
    p = _legacy(tmp_path, age=5)
    assert run.main(["--index-only"] + make_env(tmp_path, ("a",))) == 0
    assert p.exists() and not (tmp_path / "data" / "a").exists()     # read-only: nothing migrated, nothing written to data/
    m = json.loads((tmp_path / "site" / "index.json").read_text())
    assert m["sources"][0]["active"] == 1


def test_disabled_source_runs_only_when_named_and_original_flag_names_still_work(tmp_path, monkeypatch):
    cfg = tmp_path / "s.yaml"
    cfg.write_text(yaml.safe_dump({"sources": [
        {"slug": "on", "name": "On", "type": "fake", "base_url": "https://x/on", "enabled": True},
        {"slug": "off", "name": "Off", "type": "fake", "base_url": "https://x/off", "enabled": False}]}))
    base = ["--sources", str(cfg), "--data-dir", str(tmp_path / "data"), "--site-data-dir", str(tmp_path / "site")]
    calls = []
    monkeypatch.setitem(run.CRAWLERS, "fake", fake_crawler(calls))
    assert run.main(base) == 0 and calls == ["on"]                     # disabled source skipped by default
    calls.clear()
    assert run.main(base + ["--source", "off"]) == 0 and calls == ["off"]   # ...but runs when named explicitly
    assert run.main(["--build-only"] + base) == 0                       # original flag name for --index-only


def test_unimplemented_source_type_reports_cleanly_and_does_not_crash_the_run(tmp_path, monkeypatch):
    def not_ready(src, st, **kw):
        raise NotImplementedError("parser not written yet")

    monkeypatch.setitem(run.CRAWLERS, "fake", not_ready)
    assert run.main(make_env(tmp_path, ("a",))) == 1
    status = json.loads((tmp_path / "data" / "a" / run.STATUS_FILE).read_text())
    assert status["ok"] is False and "not written yet" in status["errors"][0]
    assert (tmp_path / "site" / "index.json").exists()           # the rest of the pipeline still ran
