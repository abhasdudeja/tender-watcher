"""Build the compact search index the static site loads - lazily, in pieces.

docs/data/index.json                  manifest: per source its counts, state (for the map), stats and chunk files, plus where the presets and orgs are
docs/data/orgs.json                   every organisation with its active / closed counts and the chunk files holding its rows,
                                      and the category counts - enough to draw all filters without loading a single tender
docs/data/presets/new.json            tenders published in the last NEW_DAYS days                 {"tenders": [...]}
docs/data/presets/closing.json        tenders whose deadline is within the next CLOSING_DAYS days
docs/data/presets/closed-recent.json  tenders that dropped off the portal within the last CLOSED_DAYS days
docs/data/index/<slug>-live-<n>.json  active tenders   {"tenders": [...]}, <= CHUNK_ROWS rows each
docs/data/index/<slug>-closed-<n>.json  tenders no longer on the portal (closed / removed)
docs/data/status.json                 last crawl status per source (shown at the bottom of the site)

The page loads the manifest and orgs.json first (counts, source / organisation / category filters), paints a
preset (small), and fetches row chunks only when a view needs them: an organisation's files, a source's live
chunks, or every live chunk for "All tenders" / a search. Short keys keep the files small for phones; closed
rows carry `st` ("closed" | "removed") and `i` (when they dropped off). Raw `details` dicts stay in the
Parquet data only. A preset holds at most PRESET_MAX rows, the most relevant first (newest / soonest / latest
closed); when it was cut, `truncated` holds the full number of matching tenders. The per-source `stats` are
always the full counts, so a page that narrows to one source loads that source's chunks and applies the
preset's rule itself (new: published within `days`; closing: deadline within `days`; closed_recent: dropped off
within `days`).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from . import dedupe

CHUNK_ROWS = 3000     # rows per chunk file (~0.3-0.6 MB raw): a source / organisation view loads only its own chunks
FORMAT = 3
IST = timezone(timedelta(hours=5, minutes=30))   # portal dates are IST wall-clock
NEW_DAYS = 2           # "new" preset: published within this many days (~4-5k tenders are published a day across the sources)
CLOSING_DAYS = 3       # "closing soon" preset: deadline within this many days
CLOSED_DAYS = 7        # "recently closed" preset: dropped off the portal within this many days
CLOSING_HOURS = 48     # stats.closing48: deadline within this many hours (shown on the map)
FRESH_SOURCE_DAYS = 7  # a source first crawled more recently than this has no meaningful "first seen" signal
PRESET_MAX = 2000      # rows kept per preset file (the most relevant first): a first paint of ~150 KB gzipped

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
    "link": "u",    # the tender's own address on its portal (GeM lists: a notice PDF or the portal's application address)
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


def _presets(rows_by_src: Dict[str, List[Dict]], now: datetime) -> Dict[str, List[Dict]]:
    """The three small landing views, from the already-shortened rows (short keys)."""
    now_ist = now.astimezone(IST)
    new_cut = (now_ist - timedelta(days=NEW_DAYS)).strftime("%Y-%m-%d")
    tomorrow = (now_ist + timedelta(days=1)).strftime("%Y-%m-%d")
    now_str = now_ist.strftime("%Y-%m-%dT%H:%M")
    closing_end = (now_ist + timedelta(days=CLOSING_DAYS)).strftime("%Y-%m-%dT%H:%M")
    now_utc = now.astimezone(timezone.utc)
    closed_cut = (now_utc - timedelta(days=CLOSED_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh_cut = (now_utc - timedelta(days=FRESH_SOURCE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    seen_cut = (now_utc - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out: Dict[str, List[Dict]] = {"new": [], "closing": [], "closed_recent": []}
    for rows in rows_by_src.values():
        # a source crawled for the first time this week has every tender "first seen" just now: no signal there
        established = min((r.get("f") or "9999" for r in rows), default="9999") < fresh_cut
        for r in rows:
            if "st" in r:
                if r.get("i") and r["i"] >= closed_cut:
                    out["closed_recent"].append(r)
                continue
            pub = (r.get("p") or "")[:10]
            if pub:
                if new_cut <= pub <= tomorrow:
                    out["new"].append(r)
            elif established and (r.get("f") or "") >= seen_cut:
                out["new"].append(r)
            if r.get("d") and now_str <= r["d"] <= closing_end:
                out["closing"].append(r)
    out["new"].sort(key=lambda r: (r.get("p") or r.get("f") or "", r["id"]), reverse=True)
    out["closing"].sort(key=lambda r: (r["d"], r["id"]))
    out["closed_recent"].sort(key=lambda r: (r.get("i") or "", r["id"]), reverse=True)
    return out


def build_index(stores: Dict[str, Dict[str, Dict]], sources: List[Dict], out_dir: Path,
                now: Optional[datetime] = None) -> Dict:
    """Write the manifest, orgs.json, the presets and the chunk files. Returns the manifest."""
    now = now or datetime.now(timezone.utc)
    out_dir = Path(out_dir)
    chunk_dir, preset_dir = out_dir / "index", out_dir / "presets"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    preset_dir.mkdir(parents=True, exist_ok=True)
    written = set()
    src_meta = []
    rows_by_src: Dict[str, List[Dict]] = {}
    org_files: Dict[tuple, set] = {}
    org_counts: Dict[tuple, List[int]] = {}
    categories: Dict[str, int] = {}
    # tenders an aggregator source (prefer_others) shares with another source: the other source's copy is shown, this one is left out
    dups = dedupe.find_duplicates(stores, sources)

    def dump(rel: str, obj) -> None:
        (out_dir / rel).write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        written.add(rel)

    for src in sources:
        slug = src["slug"]
        store = stores.get(slug, {})
        skip = dups.get(slug, {})
        live = [_row(r) for r in store.values() if r.get("active") and r["id"] not in skip]
        gone = [_row(r) for r in store.values() if not r.get("active") and r["id"] not in skip]
        rows_by_src[slug] = live + gone
        with_details = (sum(1 for r in store.values() if r.get("active") and r["id"] not in skip and r.get("details_fetched_at"))
                        if src.get("fetch_details") else 0)            # 0 for a source read from its listing only
        chunk_meta = []
        for kind, rows in (("live", live), ("closed", gone)):
            for n, chunk in enumerate(_chunks(rows)):
                name = f"index/{slug}-{kind}-{n}.json"
                dump(name, {"tenders": chunk})
                chunk_meta.append({"file": name, "kind": kind, "rows": len(chunk)})
                for o in {r.get("o") or "" for r in chunk}:
                    org_files.setdefault((slug, o), set()).add(name)
            for r in rows:
                counts = org_counts.setdefault((slug, r.get("o") or ""), [0, 0])
                counts[0 if kind == "live" else 1] += 1
                if kind == "live" and r.get("k"):
                    categories[r["k"]] = categories.get(r["k"], 0) + 1
        src_meta.append({
            "slug": slug, "name": src.get("name", slug), "type": src.get("type"), "base_url": src["base_url"],
            "enabled": bool(src.get("enabled", True)), "active": len(live), "inactive": len(gone),
            "total_seen": len(store), "chunks": chunk_meta,
            **({"duplicates": len(skip)} if skip else {}),               # also held by another source, so not listed here
            **({"with_details": with_details} if with_details else {}),    # listed tenders whose detail page is stored (portals with detail pages)
            **({"state": src["state"]} if src.get("state") else {}),     # ISO 3166-2 code, or "central"; the map reads it
        })

    presets = _presets(rows_by_src, now)
    preset_meta = {}
    for key, fname, days in (("new", "new", NEW_DAYS), ("closing", "closing", CLOSING_DAYS),
                             ("closed_recent", "closed-recent", CLOSED_DAYS)):
        rows = presets[key]
        kept = rows[:PRESET_MAX]
        rel = f"presets/{fname}.json"
        extra = {"truncated": len(rows)} if len(rows) > len(kept) else {}
        dump(rel, {"tenders": kept, **extra})
        preset_meta[key] = {"file": rel, "rows": len(kept), "days": days, **extra}
        per_source: Dict[str, int] = {}
        for r in rows:                                   # full counts, not just the rows that fit in the file
            per_source[r.get("s")] = per_source.get(r.get("s"), 0) + 1
        for sm in src_meta:
            sm.setdefault("stats", {})[key] = per_source.get(sm["slug"], 0)

    now_ist = now.astimezone(IST)
    now_str = now_ist.strftime("%Y-%m-%dT%H:%M")
    end_str = (now_ist + timedelta(hours=CLOSING_HOURS)).strftime("%Y-%m-%dT%H:%M")
    for sm in src_meta:                                  # full per-source numbers for the map's tooltip
        sm["stats"]["closing48"] = sum(1 for r in rows_by_src[sm["slug"]] if "st" not in r and r.get("d") and now_str <= r["d"] <= end_str)
        sm["stats"]["orgs"] = sum(1 for (slug, _), (live_n, _c) in org_counts.items() if slug == sm["slug"] and live_n > 0)

    orgs = [{"s": slug, "o": org, "n": org_counts[(slug, org)][0], "c": org_counts[(slug, org)][1],
             "files": sorted(org_files.get((slug, org), ()))} for slug, org in sorted(org_counts)]
    dump("orgs.json", {"format": FORMAT, "orgs": orgs,
                       "categories": [{"k": k, "n": n} for k, n in sorted(categories.items(), key=lambda kv: (-kv[1], kv[0]))]})

    for d, prefix in ((chunk_dir, "index"), (preset_dir, "presets")):   # derived data: drop files of tenders/sources that no longer exist
        for old in d.glob("*.json"):
            if f"{prefix}/{old.name}" not in written:
                old.unlink()
    manifest = {"format": FORMAT, "generated": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "sources": src_meta, "orgs": "orgs.json", "presets": preset_meta}
    (out_dir / "index.json").write_text(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return manifest


def write_status(statuses: List[Dict], out_dir: Path, replace: bool = False) -> None:
    """Write status.json. By default new statuses are merged into the existing file; replace=True writes
    exactly `statuses` (a source whose data is gone must not keep showing its last failure)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "status.json"
    existing: Dict[str, Dict] = {}
    if path.exists() and not replace:
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
