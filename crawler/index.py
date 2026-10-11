"""Build the compact search index the static site loads - lazily, in pieces.

docs/data/index.json                  manifest: per source its counts and its chunk files
docs/data/index/<slug>-live-<n>.json  active tenders   {"tenders": [...]}, <= CHUNK_ROWS rows each
docs/data/index/<slug>-closed-<n>.json  tenders no longer on the portal (closed / removed)
docs/data/status.json                 last crawl status per source (shown at the bottom of the site)

The page loads the manifest first (counts, source list), then fetches only the chunks it needs:
the selected source's live chunks (or every source's, in the background, for "All sources") and the
closed chunks only once "Show closed" is switched on. Short keys keep the files small for phones;
closed rows carry `st` ("closed" | "removed") and `i` (when they dropped off). Raw `details` dicts
stay in the Parquet data only.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

CHUNK_ROWS = 20000     # rows per chunk file: ~4-6 MB raw, ~1 MB gzipped on GitHub Pages
FORMAT = 2

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
    "inactive_since": "i",
}


def _row(rec: Dict) -> Dict:
    row = {}
    for k, short in INDEX_FIELDS.items():
        v = rec.get(k)
        if v not in (None, "", [], {}):
            row[short] = v
    if not rec.get("active"):
        row["st"] = rec.get("status") if rec.get("status") in ("closed", "removed") else "removed"
    return row


def _chunks(rows: List[Dict]) -> List[List[Dict]]:
    """Pack rows into chunks of <= CHUNK_ROWS keeping each organisation together where possible
    (stable files: a new tender only changes its own org's chunk). An org bigger than a chunk is split."""
    by_org: Dict[str, List[Dict]] = {}
    for r in rows:
        by_org.setdefault(r.get("o") or "", []).append(r)
    chunks: List[List[Dict]] = []
    cur: List[Dict] = []
    for org in sorted(by_org):
        grp = sorted(by_org[org], key=lambda r: (r.get("d") or "9999", r["id"]))
        if len(cur) + len(grp) > CHUNK_ROWS and cur:
            chunks.append(cur)
            cur = []
        while len(grp) > CHUNK_ROWS:
            chunks.append(grp[:CHUNK_ROWS])
            grp = grp[CHUNK_ROWS:]
        cur.extend(grp)
    if cur:
        chunks.append(cur)
    return chunks


def build_index(stores: Dict[str, Dict[str, Dict]], sources: List[Dict], out_dir: Path) -> Dict:
    """Write the manifest + chunk files. Returns the manifest."""
    out_dir = Path(out_dir)
    chunk_dir = out_dir / "index"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    written = set()
    src_meta = []
    for src in sources:
        slug = src["slug"]
        store = stores.get(slug, {})
        live = [_row(r) for r in store.values() if r.get("active")]
        gone = [_row(r) for r in store.values() if not r.get("active")]
        chunk_meta = []
        for kind, rows in (("live", live), ("closed", gone)):
            for n, chunk in enumerate(_chunks(rows)):
                name = f"index/{slug}-{kind}-{n}.json"
                (out_dir / name).write_text(
                    json.dumps({"tenders": chunk}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
                written.add(name)
                chunk_meta.append({"file": name, "kind": kind, "rows": len(chunk)})
        src_meta.append({
            "slug": slug, "name": src.get("name", slug), "type": src.get("type"), "base_url": src["base_url"],
            "enabled": bool(src.get("enabled", True)), "active": len(live), "inactive": len(gone),
            "total_seen": len(store), "chunks": chunk_meta,
        })
    for old in chunk_dir.glob("*.json"):          # derived data: drop chunks of tenders/sources that no longer exist
        if f"index/{old.name}" not in written:
            old.unlink()
    manifest = {"format": FORMAT, "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "sources": src_meta}
    (out_dir / "index.json").write_text(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return manifest


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
