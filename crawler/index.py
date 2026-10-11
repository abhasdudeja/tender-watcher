"""Build the compact search index the static site loads.

docs/data/index.json  -> {"generated", "sources": [...], "tenders": [...]}
docs/data/status.json -> last crawl status per source

Only active tenders go into the index, with short keys to keep the file small
enough for phones. Raw `details` dicts stay in the JSONL only.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

INDEX_FIELDS = {
    # record key -> index key
    "id": "id",
    "source": "s",
    "ref": "r",
    "title": "t",
    "org": "o",
    "chain": "c",
    "pub": "p",
    "close": "d",   # deadline
    "open": "b",    # bid opening
    "category": "k",
    "product_category": "pc",
    "value": "v",
    "emd": "e",
    "location": "l",
    "pincode": "z",
    "authority": "a",
    "first_seen": "f",
}


def build_index(stores: Dict[str, Dict[str, Dict]], sources: List[Dict], out_dir: Path) -> Dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    tenders: List[Dict] = []
    src_meta = []
    for src in sources:
        slug = src["slug"]
        store = stores.get(slug, {})
        n_active = 0
        for rec in store.values():
            if not rec.get("active"):
                continue
            n_active += 1
            row = {}
            for k, short in INDEX_FIELDS.items():
                v = rec.get(k)
                if v not in (None, "", [], {}):
                    row[short] = v
            tenders.append(row)
        src_meta.append(
            {
                "slug": slug,
                "name": src.get("name", slug),
                "type": src.get("type"),
                "base_url": src["base_url"],
                "enabled": bool(src.get("enabled", True)),
                "active": n_active,
                "total_seen": len(store),
            }
        )
    tenders.sort(key=lambda r: (r.get("d") or "9999", r.get("id")))
    index = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": src_meta,
        "tenders": tenders,
    }
    (out_dir / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    return index


def write_status(statuses: List[Dict], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "status.json"
    existing: Dict[str, Dict] = {}
    if path.exists():
        try:
            for s in json.loads(path.read_text(encoding="utf-8")).get("sources", []):
                existing[s["slug"]] = s
        except (ValueError, KeyError):
            pass
    for s in statuses:
        existing[s["slug"]] = s
    doc = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": sorted(existing.values(), key=lambda s: s["slug"]),
    }
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
