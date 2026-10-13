"""Entry point:  python -m crawler.run [--source SLUG] [--parallel N] [--limit-orgs N] [--org NAME] ...

Loads sources.yaml and crawls every enabled source into data/<slug>/ (Parquet, one shard per
organisation; checkpointed after every organisation and on Ctrl-C). Sources are independent
portals, so they are crawled in parallel (one thread each, each polite at its own pace); then
docs/data/ (site index, status) is rebuilt - also after Ctrl-C or a crash. Exit code 1 if any
source reported problems, 2 if another crawl holds the data directory.

  --index-only      rebuild the site index from data/ without crawling (read-only on the data; alias --build-only)
  --no-index        crawl only, don't touch docs/data (CI matrix jobs; a publish job indexes)
  --show-progress   print the live per-source progress (docs/data/progress/<slug>.json) of a crawl running
                    elsewhere, flagging entries whose process is gone or stuck

Old data/<slug>.jsonl files are migrated to Parquet automatically on first use - but never while
a crawl may be writing them (see crawler/migrate.py).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from . import epsv2, gepnic, index, mizoram, nprocure, store
from .progress import format_line

ROOT = Path(__file__).resolve().parent.parent
CRAWLERS = {"gepnic": gepnic.crawl_source, "nprocure": nprocure.crawl_source, "epsv2": epsv2.crawl_source,
            "mizoram": mizoram.crawl_source}
PROGRESS_FILE = "progress.json"     # the old single-file layout (still read, never written)
STALE_AFTER_SECONDS = 600           # a running source with no update for this long is flagged
STATUS_FILE = "_status.json"      # last crawl status of a source, kept next to its data
THROTTLE_FILE = "_throttle.json"  # what each portal said when it throttled us (last few runs; not published)

log = logging.getLogger("tenderwatch")


# ------------------------------------------------------------------ progress / status files
def progress_dir(site_dir: Path) -> Path:
    return site_dir / "progress"


def _write_json_atomic(path: Path, doc: dict) -> None:
    """tmp file + replace, retried a few times: on Windows the swap can fail for a moment while a reader has the file open."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    for attempt in range(5):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.02 * (attempt + 1))


def make_progress_sink(site_dir: Path):
    """Sink for Progress snapshots: one file per source, site_dir/progress/<slug>.json, stamped with this
    process's pid. Separate files mean a source's updates never rewrite another's, a stale file is
    attributable to one source and one process, and several crawls can share a site folder."""
    locks: dict = {}
    pid = os.getpid()

    def sink(snap: dict) -> None:
        lock = locks.setdefault(snap["slug"], threading.Lock())
        with lock:
            _write_json_atomic(progress_dir(site_dir) / f"{snap['slug']}.json", dict(snap, pid=pid))

    return sink


def _read_json_retry(path: Path, tries: int = 4):
    """Read a JSON file that may be mid-swap (Windows briefly refuses reads during a replace); None if gone."""
    for attempt in range(tries):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            if attempt == tries - 1:
                raise
            time.sleep(0.03 * (attempt + 1))


def read_progress(site_dir: Path) -> list:
    """Every source's latest snapshot: the per-source files, plus the old single progress.json if a crawl
    started by an older version is still writing it. Each snapshot gets a "legacy" flag when it came from there."""
    snaps = []
    pdir = progress_dir(site_dir)
    for f in sorted(pdir.glob("*.json")) if pdir.exists() else []:
        doc = _read_json_retry(f)
        if doc:
            snaps.append(doc)
    old = _read_json_retry(site_dir / PROGRESS_FILE)
    if old:
        have = {s["slug"] for s in snaps}
        snaps += [dict(s, legacy=True) for s in old.get("sources", {}).values() if s["slug"] not in have]
    return snaps


def staleness(snap: dict, now: Optional[datetime] = None) -> str:
    """"" for a live (or finished) source, else why the snapshot should not be trusted."""
    if snap.get("state") == "finished":
        return ""
    pid = snap.get("pid")
    if pid and not store.pid_alive(int(pid)):
        return f"STALE: process {pid} is gone (killed or crashed)"
    try:
        age = ((now or datetime.now(timezone.utc)) - datetime.strptime(snap["updated"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).total_seconds()
    except (KeyError, ValueError):
        return ""
    if age > STALE_AFTER_SECONDS:
        return f"STALE: no update for {int(age // 60)} min (stuck?)"
    return ""


def show_progress(site_dir: Path) -> int:
    snaps = read_progress(site_dir)
    if not snaps:
        print("no crawl in progress (no progress files)")
        return 0
    newest = max(s.get("updated", "") for s in snaps)
    print(f"progress as of {newest}")
    for snap in snaps:
        why = staleness(snap)
        print(format_line(snap) + (f"  [{why}]" if why else ""))
    return 0


def clear_progress(site_dir: Path, slugs=None, only_dead: bool = False) -> None:
    """Remove progress files: those of `slugs` (or all), and with only_dead just those whose process is gone."""
    pdir = progress_dir(site_dir)
    for f in list(pdir.glob("*.json")) if pdir.exists() else []:
        if slugs is not None and f.stem not in slugs:
            continue
        if only_dead:
            try:
                doc = _read_json_retry(f) or {}
            except (OSError, ValueError):
                doc = {}
            if doc.get("pid") and store.pid_alive(int(doc["pid"])) and int(doc["pid"]) != os.getpid():
                continue
        f.unlink(missing_ok=True)
    for tmp in list(pdir.glob("*.tmp")) if pdir.exists() else []:
        tmp.unlink(missing_ok=True)
    (site_dir / PROGRESS_FILE).unlink(missing_ok=True)    # the old single file, if one was left behind


def write_source_status(data_dir: Path, slug: str, status: dict) -> None:
    d = data_dir / slug
    d.mkdir(parents=True, exist_ok=True)
    status = dict(status)
    events = status.pop("throttle_events", None)       # detailed telemetry stays out of the published status
    (d / STATUS_FILE).write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")
    if events:
        p, runs = d / THROTTLE_FILE, []
        try:
            runs = json.loads(p.read_text(encoding="utf-8")).get("runs", [])
        except (OSError, ValueError, AttributeError):
            pass
        runs.append({"started": status.get("started"), "recoveries_s": status.get("throttle_recoveries", []),
                     "events": events})
        p.write_text(json.dumps({"runs": runs[-3:]}, ensure_ascii=False, indent=1), encoding="utf-8")


def statuses_from_disk(data_dir: Path, sources: List[dict]) -> List[dict]:
    out = []
    for src in sources:
        p = data_dir / src["slug"] / STATUS_FILE
        if p.exists():
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except ValueError:
                log.warning("%s: unreadable %s", src["slug"], p)
    return out


def rebuild_index(stores: Dict[str, dict], sources: List[dict], data_dir: Path, site_dir: Path) -> dict:
    """Rebuild docs/data/ (index manifest + chunks, status.json) from the stores and per-source status files."""
    idx = index.build_index(stores, sources, site_dir)
    statuses = statuses_from_disk(data_dir, sources)
    if statuses:
        index.write_status(statuses, site_dir)
    n = sum(s["active"] for s in idx["sources"])
    log.info("index: %d active (+%d closed/removed) tenders in %d chunk files -> %s", n,
             sum(s["inactive"] for s in idx["sources"]), sum(len(s["chunks"]) for s in idx["sources"]), site_dir)
    return idx


# ------------------------------------------------------------------ crawling
def _crawl_one(src: dict, st: store.SourceStore, args, sink, stop: threading.Event, data_dir: Path) -> dict:
    """Runs in its own thread: crawl one source, checkpointing its store; never raises."""
    slug = src["slug"]
    fn = CRAWLERS[src["type"]]
    checkpoint = lambda org=None: st.save(org)  # noqa: E731  (per-org shard while crawling, full save with no arg)
    kwargs = dict(limit_orgs=args.limit_orgs, only_orgs=args.org or None, no_details=args.no_details,
                  dump_dir=args.dump_html, checkpoint=checkpoint, progress=sink,
                  force_refresh=args.force_refresh, stop=stop)
    try:
        result = fn(src, st, **kwargs)
    except NotImplementedError as e:
        log.error("%s: %s", slug, e)
        result = {"slug": slug, "name": src.get("name", slug), "ok": False, "errors": [str(e)]}
    except Exception as e:  # noqa: BLE001
        log.exception("%s: crawler crashed", slug)
        result = {"slug": slug, "name": src.get("name", slug), "ok": False, "errors": [f"crashed: {e}"]}
    try:
        checkpoint()                                   # full save: also persists deactivations across orgs
        write_source_status(data_dir, slug, result)
    except Exception as e:  # noqa: BLE001
        log.exception("%s: final save failed", slug)
        result.setdefault("errors", []).append(f"save failed: {e}")
        result["ok"] = False
    log.info("%s: %s", slug, {k: v for k, v in result.items() if k != "errors"})
    for err in result.get("errors", [])[:5]:
        log.error("%s: %s", slug, err)
    return result


def _run_parallel(jobs: List[tuple], workers: int, stop: threading.Event) -> List[dict]:
    """Run (src, store) jobs on up to `workers` daemon threads. Ctrl-C sets `stop` so every crawl
    winds down cleanly (finishing its current request) and saves."""
    results: List[dict] = []
    lock = threading.Lock()
    queue = list(jobs)

    def worker():
        while not stop.is_set():
            with lock:
                if not queue:
                    return
                job = queue.pop(0)
            res = job()
            with lock:
                results.append(res)

    threads = [threading.Thread(target=worker, name=f"crawl-{i}", daemon=True) for i in range(max(1, workers))]
    for t in threads:
        t.start()
    try:
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(timeout=0.5)
    except KeyboardInterrupt:
        log.warning("interrupted: letting each crawl finish its current request and save...")
        stop.set()
        for t in threads:
            t.join(timeout=120)
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", "--sources", dest="config", default=str(ROOT / "sources.yaml"),
                    help="sources file (default sources.yaml)")
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    ap.add_argument("--site-data-dir", default=str(ROOT / "docs" / "data"))
    ap.add_argument("--source", action="append", default=[], help="only this slug (repeatable)")
    ap.add_argument("--parallel", type=int, default=0, metavar="N",
                    help="crawl up to N sources at once (default: all selected). Each portal is still "
                         "hit at its own delay_seconds pace; this only overlaps different portals")
    ap.add_argument("--limit-orgs", type=int, default=None, help="only the first N organisations")
    ap.add_argument("--org", action="append", default=[], help="only this organisation name (repeatable)")
    ap.add_argument("--no-details", action="store_true", help="skip tender detail pages")
    ap.add_argument("--dump-html", default=None, metavar="DIR", help="save every fetched page here")
    ap.add_argument("--force-refresh", action="store_true",
                    help="ignore the freshness rules: re-list every org and re-fetch every detail page")
    ap.add_argument("--show-progress", action="store_true",
                    help="print the progress of a crawl running in another terminal, then exit")
    ap.add_argument("--index-only", "--build-only", dest="index_only", action="store_true",
                    help="don't crawl; just rebuild docs/data/ from data/")
    ap.add_argument("--no-index", action="store_true", help="crawl only; don't rebuild docs/data/")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    site_dir = Path(args.site_data_dir)
    if args.show_progress:
        return show_progress(site_dir)

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    sources = cfg.get("sources", [])
    data_dir = Path(args.data_dir)

    if args.index_only:       # read-only on the data: un-migrated JSONL is read as is, never migrated here
        stores = {s["slug"]: store.open_source(data_dir, s["slug"], auto_migrate=False) for s in sources}
        rebuild_index(stores, sources, data_dir, site_dir)
        return 0

    selected = []
    for src in sources:
        if args.source and src["slug"] not in args.source:
            continue
        if not src.get("enabled", True) and not args.source:     # a disabled source runs only when asked for by name
            log.info("skipping disabled source %s", src["slug"])
            continue
        if src.get("type") not in CRAWLERS:
            log.warning("%s: no crawler for type %r, skipping", src["slug"], src.get("type"))
            continue
        selected.append(src)

    try:
        store.acquire_crawl_lock(data_dir)
    except store.CrawlActive as e:
        log.error("%s", e)
        return 2
    clear_progress(site_dir, only_dead=True)        # leftovers of a crawl that was killed
    sink = make_progress_sink(site_dir)
    stop = threading.Event()
    results: List[dict] = []
    stores: Dict[str, dict] = {}
    try:
        for src in sources:   # crawled sources migrate on open; the rest are read as they are, for the index
            stores[src["slug"]] = store.open_source(data_dir, src["slug"], auto_migrate=src in selected)
        jobs = [(lambda s=src: _crawl_one(s, stores[s["slug"]], args, sink, stop, data_dir)) for src in selected]
        workers = args.parallel if args.parallel > 0 else len(jobs)
        log.info("crawling %d source(s) with %d in parallel: %s", len(jobs), max(1, workers),
                 ", ".join(s["slug"] for s in selected))
        results = _run_parallel(jobs, workers, stop)
    except store.CrawlActive as e:
        log.error("%s", e)
        return 2
    finally:   # always leave the site with an up-to-date index, even after a crash / Ctrl-C
        try:
            if not args.no_index and len(stores) == len(sources):   # never index from a partial set of stores
                rebuild_index(stores, sources, data_dir, site_dir)
        finally:
            clear_progress(site_dir, slugs={s["slug"] for s in selected})   # transient: must not be committed
            store.release_crawl_lock(data_dir)
    if stop.is_set():
        return 130                                                 # interrupted (Ctrl-C), progress saved
    return 0 if all(r.get("ok") for r in results) and len(results) == len(selected) else 1


if __name__ == "__main__":
    sys.exit(main())
