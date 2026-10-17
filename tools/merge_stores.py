"""Resolve the merge conflicts that two crawls of the same sources leave in `data/` and `docs/data/`.

When the Actions crawl and a local crawl both update a source, `git merge origin/master` conflicts on every Parquet
shard (binary) and on the generated site index. Run this in the middle of that merge:

    git merge origin/master            # stops with conflicts
    python -I -X utf8 tools/merge_stores.py
    git commit                         # the script stages everything it resolved; it never commits

What it does:
  * data/<slug>/*.parquet  merged tender by tender (see pick_record): a tender on one side only is kept; on both sides
    the record with the newer last_seen wins. A tie goes to the side that closed the tender later, then to the one
    with the later details_fetched_at, then to "ours" (the branch you are on).
  * data/<slug>/_status.json  the side whose crawl finished later; other files in data/: "ours".
  * docs/data/**  taken from "ours" and then rebuilt from the merged stores (python -m crawler.run --index-only),
    so the site index always describes the merged data. --no-index skips the rebuild (and leaves docs/data
    conflicts for you).
Nothing is deleted from a store: a tender never disappears in a merge. Refuses to run outside a merge or while a
crawl holds data/.crawl.lock. Printed at the end: how many records came from each side, and how many times the
winning record had older fetched details than the losing one (a sign to re-run the affected source).
"""
from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pyarrow.parquet as pq  # noqa: E402

from crawler import store  # noqa: E402


def pick_record(ours: Dict, theirs: Dict) -> Tuple[Dict, bool]:
    """The record to keep when both sides hold the tender -> (winner, winner has older fetched details)."""
    a_seen, b_seen = ours.get("last_seen") or "", theirs.get("last_seen") or ""
    if a_seen != b_seen:
        win, lose = (theirs, ours) if b_seen > a_seen else (ours, theirs)
    else:
        key = lambda r: (r.get("inactive_since") or "", r.get("details_fetched_at") or "")  # noqa: E731
        win, lose = (theirs, ours) if key(theirs) > key(ours) else (ours, theirs)
    return win, (lose.get("details_fetched_at") or "") > (win.get("details_fetched_at") or "")


def merge_stores(ours: List[Dict], theirs: List[Dict], counts: Optional[Dict[str, int]] = None) -> List[Dict]:
    """Union of two lists of tender records by id (see pick_record)."""
    counts = counts if counts is not None else {}

    def bump(k: str) -> None:
        counts[k] = counts.get(k, 0) + 1

    a, b = {r["id"]: r for r in ours}, {r["id"]: r for r in theirs}
    merged: Dict[str, Dict] = {}
    for i in a.keys() | b.keys():
        if i not in b:
            merged[i] = a[i]; bump("only_ours")
        elif i not in a:
            merged[i] = b[i]; bump("only_theirs")
        else:
            merged[i], stale = pick_record(a[i], b[i])
            bump("both_ours" if merged[i] is a[i] else "both_theirs")
            if stale:
                bump("older_details")
    return list(merged.values())


def pick_status(ours: bytes, theirs: bytes) -> bytes:
    """_status.json of the crawl that finished later (ours when either does not parse)."""
    try:
        a, b = json.loads(ours), json.loads(theirs)
        return theirs if (b.get("finished") or b.get("started") or "") > (a.get("finished") or a.get("started") or "") else ours
    except ValueError:
        return ours


def _git(*args: str, binary: bool = False):
    """git in the repo, retried a few times (Windows briefly locks the index and files)."""
    for n in range(10):
        r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=not binary)
        if r.returncode == 0:
            return r.stdout
        time.sleep(1.0)
    raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr}")


def _stage_blob(stage: int, path: str) -> bytes:
    return _git("show", f":{stage}:{path}", binary=True)


def _records(raw: bytes) -> List[Dict]:
    return [store.decode_row(r) for r in pq.read_table(io.BytesIO(raw)).to_pylist()]


def _write(path: Path, data: bytes) -> None:
    for n in range(8):                                   # write beside it, then replace (as the store does)
        try:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
            return
        except OSError:
            if n == 7:
                raise
            time.sleep(1.0)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--no-index", action="store_true", help="don't rebuild docs/data/ (its conflicts stay)")
    args = ap.parse_args(argv)

    if not (ROOT / ".git" / "MERGE_HEAD").exists():
        print("ABORT: no merge in progress (run `git merge origin/master` first)")
        return 2
    if (ROOT / "data" / store.LOCK_FILE).exists():
        print("ABORT: data/.crawl.lock exists: a crawl is running (or died; remove the lock once its process is gone)")
        return 2

    conflicted = sorted({line.split("\t")[1] for line in _git("ls-files", "-u").splitlines()})
    counts: Dict[str, int] = {}
    shards = others = 0
    for path in conflicted:
        if not path.startswith("data/"):
            continue
        if path.endswith(".parquet"):
            ours, theirs = _records(_stage_blob(2, path)), _records(_stage_blob(3, path))
            _write(ROOT / path, store._serialise(merge_stores(ours, theirs, counts)))
            shards += 1
        else:
            ours, theirs = _stage_blob(2, path), _stage_blob(3, path)
            _write(ROOT / path, pick_status(ours, theirs) if path.endswith("_status.json") else ours)
            others += 1
        _git("add", "--", path)
    print(f"data/: {shards} shards merged, {others} other files; records {dict(sorted(counts.items()))}")

    left = [p for p in conflicted if p.startswith("docs/data/")]
    if left and not args.no_index:
        for p in left:
            _write(ROOT / p, _stage_blob(2, p))
        r = subprocess.run([sys.executable, "-X", "utf8", "-m", "crawler.run", "--index-only"], cwd=ROOT)
        if r.returncode:
            print("ABORT: the index rebuild failed; docs/data/ still has the 'ours' copies, nothing staged there")
            return r.returncode
        _git("add", "-A", "--", "docs/data")
        print(f"docs/data/: {len(left)} conflicted files replaced by the rebuilt index")
    elif left:
        print(f"docs/data/: {len(left)} conflicts left for you (--no-index)")

    rest = [p for p in conflicted if not p.startswith(("data/", "docs/data/"))]
    if rest:
        print("Still conflicted (not crawl output, resolve by hand):", *rest, sep="\n  ")
        return 1
    print("Done. Check `git status`, then `git commit`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
