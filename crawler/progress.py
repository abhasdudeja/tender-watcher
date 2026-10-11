"""Per-source progress counter for a crawl.

Progress is measured in tenders, not organisations (one org holds ~70% of Kerala), as
tenders in finished orgs + the done fraction of the org in flight, against the sum of the
counts the portal lists. ETA = remaining tenders / the average rate so far, where the rate
only counts tenders actually worked on (orgs skipped as fresh are "done" instantly and would
inflate it).

A Progress logs a one-line summary (at most every LOG_EVERY seconds, always on org change)
and hands a snapshot dict to `sink`, which run.py writes to docs/data/progress.json.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Callable, Dict, Optional

log = logging.getLogger("tenderwatch.progress")
LOG_EVERY = 30.0


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_duration(sec: Optional[float]) -> str:
    if sec is None:
        return "?"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s" if m else f"{s}s"


def format_line(s: Dict) -> str:
    """One human-readable line from a snapshot."""
    state = "done" if s.get("state") == "finished" else s.get("phase", "running")
    org = f" | {s['current_org'][:32]}" if s.get("current_org") and s.get("state") != "finished" else ""
    return (f"[{s['slug']}] {s['pct']:.1f}%  {s['tenders_done']:,}/{s['tenders_total']:,} tenders"
            f"  orgs {s['orgs_done']}/{s['orgs_total']}  details {s['details_fetched']:,}"
            f"  errors {s['errors']}  elapsed {fmt_duration(s['elapsed_s'])}"
            f"  ETA {fmt_duration(s['eta_s']) if state != 'done' else '-'}  ({state}){org}")


class Progress:
    def __init__(self, slug: str, name: str, orgs_total: int, tenders_total: int,
                 sink: Optional[Callable[[Dict], None]] = None):
        self.slug, self.name = slug, name
        self.orgs_total, self.tenders_total = orgs_total, tenders_total
        self.sink = sink
        self.started = _now()
        self._t0 = time.monotonic()
        self._last_log = 0.0
        self.orgs_done = 0
        self._done_tenders = 0      # tenders in finished orgs (including skipped ones)
        self._skipped_tenders = 0   # of which skipped as fresh, i.e. cost no time
        self._org_count = 0
        self._org_frac = 0.0
        self.fields: Dict = {"current_org": None, "phase": "starting", "listed": 0, "new": 0,
                             "details_fetched": 0, "errors": 0}
        self.state = "running"

    # ---- updates ----
    def start_org(self, name: str, count: int) -> None:
        self._org_count, self._org_frac = count, 0.0
        self.update(current_org=name, phase="listing", force=True)

    def finish_org(self, skipped: bool = False) -> None:
        self.orgs_done += 1
        self._done_tenders += self._org_count
        if skipped:
            self._skipped_tenders += self._org_count
        self._org_count, self._org_frac = 0, 0.0
        self.update(phase="between orgs", force=True)

    def finish(self) -> None:
        self.state = "finished"
        self._done_tenders = self.tenders_total
        self._org_count, self._org_frac = 0, 0.0
        self.update(phase="finished", force=True)

    def update(self, org_frac: Optional[float] = None, force: bool = False, **fields) -> None:
        if org_frac is not None:
            self._org_frac = max(0.0, min(1.0, org_frac))
        self.fields.update(fields)
        snap = self.snapshot()
        if self.sink:
            try:
                self.sink(snap)
            except Exception:  # noqa: BLE001  progress must never break a crawl
                log.debug("progress sink failed", exc_info=True)
        if force or time.monotonic() - self._last_log >= LOG_EVERY:
            self._last_log = time.monotonic()
            log.info(format_line(snap))

    # ---- snapshot ----
    def snapshot(self) -> Dict:
        done = self._done_tenders + self._org_frac * self._org_count
        total = max(self.tenders_total, 1)
        elapsed = time.monotonic() - self._t0
        worked = done - self._skipped_tenders
        rate = worked / elapsed if elapsed > 0 and worked > 0 else 0.0
        remaining = max(0.0, self.tenders_total - done)
        eta = remaining / rate if rate > 0 and self.state != "finished" else (0 if self.state == "finished" else None)
        return {
            "slug": self.slug, "name": self.name, "state": self.state,
            "started": self.started, "updated": _now(), "elapsed_s": round(elapsed),
            "orgs_done": self.orgs_done, "orgs_total": self.orgs_total,
            "tenders_done": int(done), "tenders_total": self.tenders_total,
            "pct": round(100.0 * done / total, 1) if self.state != "finished" else 100.0,
            "rate_per_min": round(rate * 60, 1),
            "eta_s": None if eta is None else round(eta),
            **self.fields,
        }
