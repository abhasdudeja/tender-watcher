"""JSONL store: one file per source, one JSON object per line, keyed by tender id.

The JSONL files under data/ are the source of truth and are committed to git.
They are human-readable and diff cleanly (sorted by id, one tender per line).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict


def load(path: Path) -> Dict[str, Dict]:
    store: Dict[str, Dict] = {}
    if not path.exists():
        return store
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            store[rec["id"]] = rec
    return store


def save(path: Path, store: Dict[str, Dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for tid in sorted(store):
            f.write(json.dumps(store[tid], ensure_ascii=False, sort_keys=True))
            f.write("\n")
    tmp.replace(path)
