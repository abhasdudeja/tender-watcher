"""Per-source progress files: separate files, writer pid, staleness flags, retrying reader/writer, cleanup."""
import json
import os
import threading
from pathlib import Path

import yaml

from crawler import run, store as S
from crawler.progress import Progress

DEAD_PID = 2 ** 22 + 12345          # no such process


def snap(slug, state="running", pid=None, updated=None, pct_done=0):
    p = Progress(slug, slug.upper(), orgs_total=10, tenders_total=100)
    s = p.snapshot()
    s.update(state=state)
    if pid is not None:
        s["pid"] = pid
    if updated:
        s["updated"] = updated
    return s


def test_each_source_gets_its_own_file_stamped_with_the_writers_pid(tmp_path):
    sink = run.make_progress_sink(tmp_path)
    pa, pb = Progress("kerala", "K", 5, 50, sink=sink), Progress("gujarat", "G", 5, 50, sink=sink)
    pb.start_org("x", 10)
    before = (tmp_path / "progress" / "gujarat.json").read_text(encoding="utf-8")
    for i in range(20):                                   # a busy source...
        pa.start_org(f"org {i}", 10)
        pa.update(force=True, listed=i)
    after = (tmp_path / "progress" / "gujarat.json").read_text(encoding="utf-8")
    assert before == after                                # ...never rewrites another source's file
    assert sorted(f.name for f in (tmp_path / "progress").glob("*.json")) == ["gujarat.json", "kerala.json"]
    assert json.loads(after)["pid"] == os.getpid()
    assert not (tmp_path / "progress.json").exists()      # the old single file is not written any more


def test_concurrent_writers_and_a_fast_reader_never_see_a_torn_file(tmp_path):
    sink = run.make_progress_sink(tmp_path)
    stop, bad = threading.Event(), []

    def writer(slug):
        p = Progress(slug, slug, 50, 5000, sink=sink)
        for i in range(150):
            p.start_org(f"o{i}", 100)
            p.update(org_frac=0.5, force=True)

    def reader():
        while not stop.is_set():
            try:
                run.read_progress(tmp_path)
            except Exception as e:                        # noqa: BLE001
                bad.append(repr(e))

    ths = [threading.Thread(target=writer, args=(s,)) for s in ("a", "b", "c", "d")]
    rd = threading.Thread(target=reader)
    rd.start()
    [t.start() for t in ths]
    [t.join() for t in ths]
    stop.set()
    rd.join()
    assert bad == []
    assert sorted(s["slug"] for s in run.read_progress(tmp_path)) == ["a", "b", "c", "d"]


def test_staleness_rules():
    assert run.staleness(snap("x", state="finished", pid=DEAD_PID)) == ""          # a finished source is never stale
    assert "process" in run.staleness(snap("x", pid=DEAD_PID)) and "gone" in run.staleness(snap("x", pid=DEAD_PID))
    assert run.staleness(snap("x", pid=os.getpid())) == ""                          # live process, fresh update
    old = snap("x", pid=os.getpid(), updated="2020-01-01T00:00:00Z")
    assert "no update" in run.staleness(old)                                         # alive but silent: stuck?
    assert run.staleness(snap("x")) == ""                                            # no pid recorded: judged by age only


def test_show_progress_flags_stale_entries_and_still_reads_the_old_single_file(tmp_path, capsys):
    pdir = tmp_path / "progress"
    pdir.mkdir()
    (pdir / "kerala.json").write_text(json.dumps(snap("kerala", pid=DEAD_PID)))
    (pdir / "gujarat.json").write_text(json.dumps(snap("gujarat", pid=os.getpid())))
    old = snap("maharashtra")                              # a crawl from before this change, still writing progress.json
    (tmp_path / "progress.json").write_text(json.dumps({"updated": old["updated"], "sources": {"maharashtra": old}}))
    assert run.show_progress(tmp_path) == 0
    out = capsys.readouterr().out
    lines = {l.split("]")[0].lstrip("["): l for l in out.splitlines() if l.startswith("[")}
    assert "STALE" in lines["kerala"] and "gone" in lines["kerala"]
    assert "STALE" not in lines["gujarat"] and "maharashtra" in lines


def test_show_progress_with_nothing_running(tmp_path, capsys):
    assert run.show_progress(tmp_path) == 0
    assert "no crawl in progress" in capsys.readouterr().out


def test_reader_and_writer_retry_through_transient_windows_file_errors(tmp_path, monkeypatch):
    f = tmp_path / "x.json"
    f.write_text('{"a": 1}')
    real_read = Path.read_text
    fails = {"n": 2}

    def flaky_read(self, *a, **k):
        if self.name == "x.json" and fails["n"] > 0:
            fails["n"] -= 1
            raise PermissionError("being replaced")
        return real_read(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", flaky_read)
    assert run._read_json_retry(f) == {"a": 1} and fails["n"] == 0                    # succeeded on the third try
    assert run._read_json_retry(tmp_path / "missing.json") is None                     # a missing file is just "nothing yet"

    real_replace = Path.replace
    rfails = {"n": 2}

    def flaky_replace(self, target):
        if rfails["n"] > 0:
            rfails["n"] -= 1
            raise PermissionError("reader has it open")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", flaky_replace)
    run._write_json_atomic(tmp_path / "y.json", {"b": 2})
    assert json.loads((tmp_path / "y.json").read_text()) == {"b": 2} and rfails["n"] == 0


def test_clear_progress_removes_leftovers_of_dead_crawls_but_not_live_ones(tmp_path):
    pdir = tmp_path / "progress"
    pdir.mkdir()
    (pdir / "dead.json").write_text(json.dumps(snap("dead", pid=DEAD_PID)))
    (pdir / "nopid.json").write_text(json.dumps(snap("nopid")))
    (pdir / "live.json").write_text(json.dumps(snap("live", pid=os.getppid())))        # another running process
    (pdir / "half.tmp").write_text("{")
    (tmp_path / "progress.json").write_text("{}")
    run.clear_progress(tmp_path, only_dead=True)
    assert sorted(f.name for f in pdir.glob("*")) == ["live.json"]
    assert not (tmp_path / "progress.json").exists()
    run.clear_progress(tmp_path, slugs={"live"})
    assert list(pdir.glob("*.json")) == []


def test_a_crawl_cleans_up_old_leftovers_and_its_own_progress_files(tmp_path, monkeypatch):
    cfg = tmp_path / "s.yaml"
    cfg.write_text(yaml.safe_dump({"sources": [{"slug": "a", "name": "A", "type": "fake", "base_url": "https://x/a"}]}))
    pdir = tmp_path / "site" / "progress"
    pdir.mkdir(parents=True)
    (pdir / "ghost.json").write_text(json.dumps(snap("ghost", pid=DEAD_PID)))          # left by a killed crawl

    def crawl(src, st, progress=None, **kw):
        progress(snap("a", pid=os.getpid()))                                           # the crawl reports progress
        assert (pdir / "a.json").exists() and not (pdir / "ghost.json").exists()       # ghost already cleaned at start
        return {"slug": "a", "name": "A", "ok": True, "errors": []}

    monkeypatch.setitem(run.CRAWLERS, "fake", crawl)
    assert run.main(["--config", str(cfg), "--data-dir", str(tmp_path / "data"), "--site-data-dir", str(tmp_path / "site")]) == 0
    assert list(pdir.glob("*")) == []                                                  # and removes its own at the end
