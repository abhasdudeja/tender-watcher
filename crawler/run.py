"""Entry point:  python -m crawler.run [--source SLUG] [--parallel N] [--limit-orgs N] [--org NAME] ...

Loads sources.yaml and crawls every enabled source into data/<slug>/ (Parquet, one shard per
organisation; checkpointed after every organisation and on Ctrl-C). Sources are independent
portals, so they are crawled in parallel (one thread each, each polite at its own pace); then
docs/data/ (site index, status) is rebuilt - also after Ctrl-C or a crash. Exit code 1 if any
source reported problems, 2 if another crawl holds the data directory.

  --index-only      rebuild the site index from data/ without crawling (read-only on the data; alias --build-only)
  --no-index        crawl only, don't touch docs/data (CI matrix jobs; a publish job indexes)
  --show-progress   print the live progress.json of a crawl running elsewhere

Old data/<slug>.jsonl files are migrated to Parquet automatically on first use - but never while
a crawl may be writing them (see crawler/migrate.py).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

import yaml

from . import gepnic, index, nprocure, store
from .progress import format_line

ROOT = Path(__file__).resolve().parent.parent
CRAWLERS = {"gepnic": gepnic.crawl_source, "nprocure": nprocure.crawl_source}
PROGRESS_FILE = "progress.json"
STATUS_FILE = "_status.json"      # last crawl status of a source, kept next to its data

log = logging.getLogger("tenderwatch")


# ------------------------------------------------------------------ progress / status files
def make_progress_sink(site_dir: Path):
    """Sink for Progress snapshots: keeps every source's latest snapshot in site_dir/progress.json."""
    state: dict = {}
    lock = threading.Lock()              # one writer per source thread
    path = site_dir / PROGRESS_FILE

    def sink(snap: dict) -> None:
        with lock:
            state[snap["slug"]] = snap
            doc = {"updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "sources": state}
            site_dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(path)

    return sink


def show_progress(site_dir: Path) -> int:
    path = site_dir / PROGRESS_FILE
    if not path.exists():
        print("no crawl in progress (no progress.json)")
        return 0
    doc = json.loads(path.read_text(encoding="utf-8"))
    print(f"progress as of {doc['updated']}")
    for snap in doc["sources"].values():
        print(format_line(snap))
    return 0


def write_source_status(data_dir: Path, slug: str, status: dict) -> None:
    d = data_dir / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / STATUS_FILE).write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")


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
            (site_dir / PROGRESS_FILE).unlink(missing_ok=True)   # transient: must not be committed
            store.release_crawl_lock(data_dir)
    if stop.is_set():
        return 130                                                 # interrupted (Ctrl-C), progress saved
    return 0 if all(r.get("ok") for r in results) and len(results) == len(selected) else 1


if __name__ == "__main__":
    sys.exit(main())
