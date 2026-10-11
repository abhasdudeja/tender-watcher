"""CLI entry point.

    python -m crawler.run                    # crawl all enabled sources, rebuild index
    python -m crawler.run --source kerala    # one source
    python -m crawler.run --limit-orgs 3     # quick smoke test
    python -m crawler.run --org "ANERT" --org "Kerala Police"
    python -m crawler.run --no-details       # listing only
    python -m crawler.run --dump-html debug/ # save every fetched page for parser debugging
    python -m crawler.run --build-only       # rebuild docs/data/*.json from data/*.jsonl
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Dict

import yaml

from . import gepnic, index, nprocure, store

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SITE_DATA_DIR = ROOT / "docs" / "data"

CRAWLERS = {
    "gepnic": gepnic.crawl_source,
    "nprocure": nprocure.crawl_source,
}


def load_sources(path: Path) -> list:
    with path.open(encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    return doc.get("sources", [])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="tenderwatch crawler")
    ap.add_argument("--sources", default=str(ROOT / "sources.yaml"))
    ap.add_argument("--source", action="append", help="only this slug (repeatable)")
    ap.add_argument("--org", action="append", help="only this organisation name (repeatable)")
    ap.add_argument("--limit-orgs", type=int, help="stop after N organisations (smoke test)")
    ap.add_argument("--no-details", action="store_true", help="skip tender detail pages")
    ap.add_argument("--dump-html", metavar="DIR", help="save every fetched page here")
    ap.add_argument("--build-only", action="store_true", help="rebuild site index only")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("run")

    sources = load_sources(Path(args.sources))
    stores: Dict[str, Dict] = {}
    for src in sources:
        stores[src["slug"]] = store.load(DATA_DIR / f"{src['slug']}.jsonl")

    statuses = []
    exit_code = 0
    if not args.build_only:
        for src in sources:
            if args.source and src["slug"] not in args.source:
                continue
            if not src.get("enabled", True) and not args.source:
                log.info("skipping disabled source %s", src["slug"])
                continue
            crawl = CRAWLERS.get(src.get("type"))
            if crawl is None:
                log.error("unknown source type %r for %s", src.get("type"), src["slug"])
                exit_code = 1
                continue
            log.info("=== crawling %s (%s) ===", src["slug"], src.get("name"))
            jsonl_path = DATA_DIR / f"{src['slug']}.jsonl"
            slug_store = stores[src["slug"]]

            def checkpoint(_p=jsonl_path, _s=slug_store):
                store.save(_p, _s)

            try:
                st = crawl(
                    src,
                    slug_store,
                    limit_orgs=args.limit_orgs,
                    only_orgs=args.org,
                    dump_dir=args.dump_html,
                    fetch_details=False if args.no_details else None,
                    checkpoint=checkpoint,
                )
            except KeyboardInterrupt:
                store.save(jsonl_path, slug_store)
                log.warning("%s: interrupted; progress saved to %s", src["slug"], jsonl_path)
                return 130
            except NotImplementedError as e:
                log.error("%s: %s", src["slug"], e)
                exit_code = 1
                continue
            except Exception as e:  # noqa: BLE001
                log.exception("%s: crawl crashed", src["slug"])
                st = {"slug": src["slug"], "name": src.get("name"), "ok": False,
                      "errors": [f"crash: {e.__class__.__name__}: {e}"[:200]]}
                exit_code = 1
            statuses.append(st)
            store.save(DATA_DIR / f"{src['slug']}.jsonl", stores[src["slug"]])
            log.info("[%s] listed=%s new=%s details=%s active=%s ok=%s",
                     src["slug"], st.get("listed"), st.get("new"), st.get("details_fetched"),
                     st.get("active"), st.get("ok"))
            for err in st.get("errors", [])[:5]:
                log.warning("[%s] %s", src["slug"], err)

    idx = index.build_index(stores, sources, SITE_DATA_DIR)
    if statuses:
        index.write_status(statuses, SITE_DATA_DIR)
    log.info("index: %d active tenders across %d sources -> %s",
             len(idx["tenders"]), len(idx["sources"]), SITE_DATA_DIR / "index.json")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
