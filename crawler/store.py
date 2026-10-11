"""Tender storage: Parquet shards, one file per organisation, one directory per source.

    data/<slug>/_meta.json                 {"format": 1, ...}
    data/<slug>/<org-slug>-<hash>.parquet  every tender of one organisation, sorted by id
    data/_legacy/<slug>.jsonl              the pre-Parquet file, kept after a verified migration

The in-memory view is a plain dict (id -> record) so the crawlers don't care about the format.
Round-tripping is lossless: known fields get typed columns; anything unexpected (new fields,
a value of an unexpected type) goes into the `extra` JSON column and comes back unchanged.
`None` values are the one normalisation: they are dropped (the crawler never stores them).

Formats are versioned (FORMAT_VERSION, `_meta.json`, and the parquet key-value metadata). Older
layouts are migrated forward on open, verified record-for-record before anything is replaced,
and the old files are moved aside, never deleted. See crawler/migrate.py for the CLI.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import pyarrow as pa
import pyarrow.parquet as pq

log = logging.getLogger("tenderwatch.store")

FORMAT_VERSION = 1
META_FILE = "_meta.json"
LEGACY_DIR = "_legacy"
LOCK_FILE = ".crawl.lock"
# A legacy file touched this recently may still be getting written by a running crawl.
# CI sets TENDERWATCH_MIGRATE_MIN_IDLE=0: a fresh checkout has fresh mtimes but no crawl running.
DEFAULT_MIN_IDLE_SECONDS = float(os.environ.get("TENDERWATCH_MIGRATE_MIN_IDLE", 300))

STR_COLS = ("id", "source", "ref", "title", "org", "chain", "pub", "close", "open", "first_seen",
            "last_seen", "status", "inactive_since", "details_fetched_at", "category",
            "product_category", "location", "pincode", "bid_end", "contract_type", "period",
            "authority", "authority_address", "description", "form_of_contract", "tender_type",
            "last_seen_active")
FLOAT_COLS = ("value", "emd")
BOOL_COLS = ("active", "needs_details")
LIST_COLS = ("documents",)
JSON_COLS = ("details",)          # dict -> JSON text
SCHEMA = pa.schema(
    [(c, pa.string()) for c in STR_COLS]
    + [(c, pa.float64()) for c in FLOAT_COLS]
    + [(c, pa.bool_()) for c in BOOL_COLS]
    + [(c, pa.list_(pa.string())) for c in LIST_COLS]
    + [(c, pa.string()) for c in JSON_COLS]
    + [("extra", pa.string())],
    metadata={b"tenderwatch_format": str(FORMAT_VERSION).encode()},
)


class StoreError(RuntimeError):
    pass


class CrawlActive(StoreError):
    """Refusing to touch the data because a crawl may be writing it."""


# ------------------------------------------------------------------ record <-> row
def _is_str_list(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def encode_record(rec: Dict) -> Dict:
    """Record dict -> row dict for SCHEMA (unknown / mistyped fields go to `extra`)."""
    row: Dict = {}
    extra: Dict = {}
    for k, v in rec.items():
        if v is None:
            continue
        if k in STR_COLS and isinstance(v, str):
            row[k] = v
        elif k in FLOAT_COLS and isinstance(v, (int, float)) and not isinstance(v, bool):
            row[k] = float(v)
        elif k in BOOL_COLS and isinstance(v, bool):
            row[k] = v
        elif k in LIST_COLS and _is_str_list(v):
            row[k] = list(v)
        elif k in JSON_COLS and isinstance(v, dict):
            row[k] = json.dumps(v, ensure_ascii=False, sort_keys=True)
        else:
            extra[k] = v
    if extra:
        row["extra"] = json.dumps(extra, ensure_ascii=False, sort_keys=True)
    return row


def decode_row(row: Dict) -> Dict:
    rec: Dict = {}
    for k, v in row.items():
        if v is None or k == "extra":
            continue
        rec[k] = json.loads(v) if k in JSON_COLS else v
    if row.get("extra"):
        rec.update(json.loads(row["extra"]))
    return rec


def normalise(rec: Dict) -> Dict:
    """What a record looks like after a round trip (None values dropped, ints in value/emd as floats)."""
    out = {}
    for k, v in rec.items():
        if v is None:
            continue
        if k in FLOAT_COLS and isinstance(v, (int, float)) and not isinstance(v, bool):
            v = float(v)
        out[k] = v
    return out


# ------------------------------------------------------------------ shards
def shard_name(org: Optional[str]) -> str:
    """Filesystem-safe, collision-proof shard file stem for an organisation name."""
    if not org:
        return "_unassigned"
    stem = re.sub(r"[^a-z0-9]+", "-", org.lower()).strip("-")[:60] or "org"
    return f"{stem}-{hashlib.sha1(org.encode('utf-8')).hexdigest()[:6]}"


def _serialise(records: List[Dict]) -> bytes:
    rows = [encode_record(r) for r in sorted(records, key=lambda r: r["id"])]
    table = pa.Table.from_pylist(rows, schema=SCHEMA)
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="zstd")
    return buf.getvalue()


def _write_if_changed(path: Path, data: bytes) -> bool:
    if path.exists() and path.read_bytes() == data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return True


def _read_shard(path: Path) -> List[Dict]:
    return [decode_row(r) for r in pq.read_table(path).to_pylist()]


class SourceStore(dict):
    """id -> record for one source, persisted as per-organisation Parquet shards."""

    def __init__(self, directory: Path, records: Iterable[Dict] = ()):
        super().__init__()
        self.directory = Path(directory)
        for r in records:
            self[r["id"]] = r

    # ---- loading ----
    @classmethod
    def load_dir(cls, directory: Path) -> "SourceStore":
        st = cls(directory)
        newest: Dict[str, str] = {}
        for path in sorted(Path(directory).glob("*.parquet")):
            for rec in _read_shard(path):
                old = st.get(rec["id"])
                # a tender that moved org can sit in two shards until the next full save: keep the newer
                if old is None or rec.get("last_seen", "") >= old.get("last_seen", ""):
                    st[rec["id"]] = rec
        return st

    # ---- saving ----
    def _groups(self, only_shard: Optional[str] = None) -> Dict[str, List[Dict]]:
        groups: Dict[str, List[Dict]] = {}
        for rec in self.values():
            sh = shard_name(rec.get("org"))
            if only_shard is None or sh == only_shard:
                groups.setdefault(sh, []).append(rec)
        return groups

    def save(self, org: Optional[str] = None) -> int:
        """Write shards whose content changed. `org` limits the work to that organisation's shard
        (cheap checkpoint); with no argument every shard is checked and emptied ones are removed.
        Returns the number of files written."""
        self.directory.mkdir(parents=True, exist_ok=True)
        written = 0
        if org is not None:
            sh = shard_name(org)
            recs = self._groups(sh).get(sh)
            path = self.directory / f"{sh}.parquet"
            if recs:
                written += _write_if_changed(path, _serialise(recs))
            elif path.exists():
                path.unlink()
        else:
            groups = self._groups()
            for sh, recs in groups.items():
                written += _write_if_changed(self.directory / f"{sh}.parquet", _serialise(recs))
            for path in self.directory.glob("*.parquet"):
                if path.stem not in groups:      # all its tenders live in other shards now
                    path.unlink()
        self._write_meta()
        return written

    def _write_meta(self) -> None:
        path = self.directory / META_FILE
        if path.exists():
            return
        _atomic_json(path, {"format": FORMAT_VERSION, "created": _now()})


# ------------------------------------------------------------------ helpers
def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def read_jsonl(path: Path) -> Dict[str, Dict]:
    store: Dict[str, Dict] = {}
    with Path(path).open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rec = json.loads(line)
                store[rec["id"]] = rec
    return store


def _sha1(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------ crawl lock
def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == 259          # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_crawl_lock(data_dir: Path) -> Path:
    """Mark the data dir as being written by a crawl. Raises CrawlActive if another one holds it."""
    path = Path(data_dir) / LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            pid = int(json.loads(path.read_text(encoding="utf-8")).get("pid", 0))
        except (ValueError, OSError):
            pid = 0
        if _pid_alive(pid) and pid != os.getpid():
            raise CrawlActive(f"another crawl (pid {pid}) holds {path}")
    _atomic_json(path, {"pid": os.getpid(), "started": _now()})
    return path


def release_crawl_lock(data_dir: Path) -> None:
    (Path(data_dir) / LOCK_FILE).unlink(missing_ok=True)


def assert_no_crawl_active(data_dir: Path, watch: Iterable[Path] = (),
                           min_idle_seconds: float = DEFAULT_MIN_IDLE_SECONDS) -> None:
    """Refuse (CrawlActive) if a crawl holds the lock, or any `watch`ed file changed recently.

    The mtime check covers crawls from before the lock existed: they save at least every minute
    or so, so a file idle for `min_idle_seconds` means nothing is writing it."""
    lock = Path(data_dir) / LOCK_FILE
    if lock.exists():
        try:
            pid = int(json.loads(lock.read_text(encoding="utf-8")).get("pid", 0))
        except (ValueError, OSError):
            pid = 0
        if _pid_alive(pid) and pid != os.getpid():     # our own lock (a crawl migrating its own data) is fine
            raise CrawlActive(f"a crawl is running (pid {pid}, {lock})")
    now = time.time()
    for p in watch:
        p = Path(p)
        if p.exists() and now - p.stat().st_mtime < min_idle_seconds:
            raise CrawlActive(
                f"{p} was modified {int(now - p.stat().st_mtime)}s ago - a crawl may still be writing it. "
                f"Wait for it to finish (or stop it) and retry.")


# ------------------------------------------------------------------ migration
def compare_stores(expected: Dict[str, Dict], actual: Dict[str, Dict]) -> List[str]:
    """Differences between two id->record maps after normalisation (empty list = identical)."""
    problems = []
    if len(expected) != len(actual):
        problems.append(f"record count {len(expected)} != {len(actual)}")
    for tid, rec in expected.items():
        other = actual.get(tid)
        if other is None:
            problems.append(f"missing {tid}")
        elif normalise(rec) != normalise(other):
            diff = sorted(k for k in set(normalise(rec)) | set(normalise(other))
                          if normalise(rec).get(k) != normalise(other).get(k))
            problems.append(f"{tid}: fields differ {diff}")
        if len(problems) >= 20:
            break
    problems += [f"unexpected {tid}" for tid in list(actual)[:5] if tid not in expected]
    return problems


def migrate_jsonl(data_dir: Path, slug: str, dry_run: bool = False,
                  min_idle_seconds: float = DEFAULT_MIN_IDLE_SECONDS) -> Dict:
    """v0 (data/<slug>.jsonl) -> v1 (Parquet shards). Verified before anything is replaced.

    1. refuse while a crawl may be writing the JSONL
    2. write shards into data/<slug>.migrating/ and re-read them
    3. compare every record with the JSONL; any difference aborts (nothing else touched)
    4. swap the directory into place, move the JSONL to data/_legacy/ (never deleted)
    """
    data_dir = Path(data_dir)
    legacy = data_dir / f"{slug}.jsonl"
    final = data_dir / slug
    tmp = data_dir / f"{slug}.migrating"
    assert_no_crawl_active(data_dir, [legacy], min_idle_seconds)
    records = read_jsonl(legacy)
    if tmp.exists():
        shutil.rmtree(tmp)
    try:
        st = SourceStore(tmp, records.values())
        st.save()
        _atomic_json(tmp / META_FILE, {
            "format": FORMAT_VERSION, "created": _now(), "migrated_from": "jsonl",
            "records": len(records), "legacy_sha1": _sha1(legacy)})
        problems = compare_stores(records, SourceStore.load_dir(tmp))
        if problems:
            raise StoreError(f"{slug}: migration verification FAILED, nothing changed: " + "; ".join(problems))
        info = {"slug": slug, "records": len(records), "shards": len(list(tmp.glob('*.parquet'))),
                "parquet_bytes": sum(p.stat().st_size for p in tmp.glob("*.parquet")),
                "jsonl_bytes": legacy.stat().st_size, "verified": True, "dry_run": dry_run}
        if dry_run:
            return info
        if final.exists():                       # leftover of an interrupted earlier attempt
            aside = data_dir / LEGACY_DIR / f"{slug}.incomplete-{int(time.time())}"
            aside.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(final), str(aside))
        tmp.replace(final)
        dest = data_dir / LEGACY_DIR / f"{slug}.jsonl"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest = dest.with_name(f"{slug}.{int(time.time())}.jsonl")
        shutil.move(str(legacy), str(dest))
        info["legacy_moved_to"] = str(dest)
        log.info("%s: migrated %d records to %d parquet shards (legacy kept at %s)",
                 slug, info["records"], info["shards"], dest)
        return info
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)


def migrate_if_needed(data_dir: Path, slug: str, min_idle_seconds: float = DEFAULT_MIN_IDLE_SECONDS) -> Optional[Dict]:
    """Bring `slug` up to FORMAT_VERSION. Returns migration info, or None if nothing to do.

    Future formats add a step here: read meta["format"], run the matching step, bump the meta."""
    data_dir = Path(data_dir)
    meta_path = data_dir / slug / META_FILE
    legacy = data_dir / f"{slug}.jsonl"
    if meta_path.exists():
        fmt = json.loads(meta_path.read_text(encoding="utf-8")).get("format", 0)
        if fmt > FORMAT_VERSION:
            raise StoreError(f"{slug}: data format {fmt} is newer than this code understands ({FORMAT_VERSION})")
        if legacy.exists():
            log.warning("%s: ignoring stale %s (data already migrated; the stale file is safe to move or delete)", slug, legacy)
        return None
    if legacy.exists():
        return migrate_jsonl(data_dir, slug, min_idle_seconds=min_idle_seconds)
    return None


def open_source(data_dir: Path, slug: str, auto_migrate: bool = True) -> SourceStore:
    """Load (migrating legacy data first if needed) the store for one source."""
    data_dir = Path(data_dir)
    if auto_migrate:
        migrate_if_needed(data_dir, slug)
    d = data_dir / slug
    if d.exists() and (d / META_FILE).exists():
        return SourceStore.load_dir(d)
    legacy = data_dir / f"{slug}.jsonl"
    if legacy.exists():            # un-migrated and migration not requested: read-only view of the old file
        return SourceStore(d, read_jsonl(legacy).values())
    return SourceStore(d)
