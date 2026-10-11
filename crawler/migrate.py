"""Migrate stored data to the current format (currently: data/<slug>.jsonl -> Parquet shards).

    python -m crawler.migrate --dry-run            # verify what would happen, change nothing
    python -m crawler.migrate                       # migrate every source that still has a .jsonl
    python -m crawler.migrate --source kerala

Safety (see crawler/store.py): refuses while a crawl is active (lock file, or the legacy file was
modified within --min-idle seconds); every record is re-read from the new files and compared with
the original before anything is replaced; the old file is moved to data/_legacy/, never deleted.
Exit codes: 0 ok / nothing to do, 1 verification or other failure, 2 refused because a crawl is active.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import yaml

from . import store

ROOT = Path(__file__).resolve().parent.parent


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    ap.add_argument("--config", default=str(ROOT / "sources.yaml"))
    ap.add_argument("--source", action="append", default=[], help="only this slug (repeatable)")
    ap.add_argument("--dry-run", action="store_true", help="verify the migration but change nothing")
    ap.add_argument("--min-idle", type=float, default=store.DEFAULT_MIN_IDLE_SECONDS,
                    help="refuse if the legacy file changed within this many seconds (default %(default)s)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    data_dir = Path(args.data_dir)
    slugs = [s["slug"] for s in (yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}).get("sources", [])]
    slugs += [p.stem for p in data_dir.glob("*.jsonl") if p.stem not in slugs]   # data from sources no longer configured
    slugs = [s for s in slugs if not args.source or s in args.source]

    todo = [s for s in slugs if (data_dir / f"{s}.jsonl").exists() and not (data_dir / s / store.META_FILE).exists()]
    if not todo:
        print("nothing to migrate: no un-migrated data/<slug>.jsonl files")
        return 0
    rc = 0
    for slug in todo:
        try:
            info = store.migrate_jsonl(data_dir, slug, dry_run=args.dry_run, min_idle_seconds=args.min_idle)
        except store.CrawlActive as e:
            print(f"REFUSED {slug}: {e}")
            return 2
        except store.StoreError as e:
            print(f"FAILED  {slug}: {e}")
            rc = 1
            continue
        ratio = info["jsonl_bytes"] / max(info["parquet_bytes"], 1)
        print(f"{'DRY-RUN ' if args.dry_run else 'MIGRATED'} {slug}: {info['records']:,} records -> {info['shards']} shards, "
              f"{info['jsonl_bytes'] / 1e6:.1f} MB jsonl -> {info['parquet_bytes'] / 1e6:.1f} MB parquet ({ratio:.1f}x smaller), "
              f"verified record-for-record" + ("" if args.dry_run else f"; legacy kept at {info['legacy_moved_to']}"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
