"""Portal-independent crawl loop shared by every crawler (GePNIC, nProcure, ...).

A crawler for a portal type is a small *adapter* (duck-typed) that knows how to talk to that
portal; this module owns everything else, so all sources behave identically:

  * the refresh policy (new / listing-changed / very-old tenders get their detail page fetched;
    organisations are re-listed at most every `refresh_after_hours` and skipped with no request
    when nothing changed),
  * retention (nothing is ever deleted: tenders that leave a healthy full crawl are kept and marked
    closed/removed, and reactivated if they return),
  * checkpoints (after each org's listing, every DETAIL_CHUNK detail pages, after each org),
  * the progress counter, cooperative stop, and the status dict.

Adapter interface
    list_orgs()            -> [{"name": str, "count": int, ...adapter fields}]   (orgs with tenders)
    walk_org(org)          -> iterable of listing rows: {"id", "ref", "title", "chain", "pub",
                              "close", "open", ...any listing_fields, "detail_ref": opaque or None}
    fetch_detail(org, row) -> parsed detail dict, non-empty "details"; raise DetailFailed (this
                              tender), DetailsBlocked (stop fetching details for this run) or
                              Restart (session/links went stale)
    restart_org(org)       -> a fresh org dict to retry with, or None if it vanished
    requests_made()        -> int (for the status dict)
Optional adapter attributes: parallel_details (detail_workers is honoured), throttle_scope ("org" for
per-host portals), may_be_empty (an empty organisation list is a valid answer: tenders that are gone
are then marked closed instead of the run failing).
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

from .http import HostNotFound, Throttled
from .progress import Progress

log = logging.getLogger("tenderwatch.engine")

MAX_RESTARTS = 5
DEFAULT_COOLDOWN_MINUTES = 15   # longest single wait when a portal throttles us (src["throttle_cooldown_minutes"])
DEFAULT_PROBE_MINUTES = 1.0     # first wait; doubles on every retry that is still throttled (src["throttle_probe_minutes"])
DEFAULT_MAX_COOLDOWNS = 4       # patience = this many full cool-downs of waiting in total (src["throttle_max_cooldowns"])
MAX_EVENTS = 300                # throttle telemetry kept per run
MAX_DETAIL_WORKERS = 8          # upper bound for src["detail_workers"]
MAX_ORGS_GIVEN_UP_IN_A_ROW = 3  # per-host portals (throttle_scope "org"): abort the source after this many in a row
DETAIL_CHUNK = 50          # checkpoint after this many detail pages
PROGRESS_EVERY = 5         # parallel details: refresh the progress counter after this many pages
REFRESH_SLACK_HOURS = 1.0  # tolerance so a cron that fires a few minutes late doesn't skip yesterday's tenders
IST = timezone(timedelta(hours=5, minutes=30))   # portal dates are IST wall-clock
DEFAULT_CHANGE_KEYS = ("close", "open", "title", "ref", "pub")   # listing fields whose change means "re-fetch details"
DEFAULT_LISTING_FIELDS = ("ref", "title", "chain", "pub", "close", "open")


class _Skip(Exception):
    """A parallel detail fetch that was not attempted (another worker was throttled, or we are stopping)."""


class Restart(Exception):
    """The portal says our session (and every link in it) went stale: restart_org() and retry."""


class DetailFailed(Exception):
    """This tender's detail page could not be used; counted, reported, and retried next run."""


class DetailsBlocked(Exception):
    """Detail pages are walled off (e.g. CAPTCHA): stop fetching details for the rest of this run."""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sleep(seconds: float, stop=None, tick=None, every: float = 30.0) -> None:
    """Sleep `seconds`, waking early (within a second) if `stop` is set; call `tick()` every `every`
    seconds meanwhile (a heartbeat, so a long wait is not mistaken for a hung crawl)."""
    end = time.monotonic() + seconds
    next_tick = time.monotonic() + every
    while True:
        left = end - time.monotonic()
        if left <= 0 or (stop is not None and stop.is_set()):
            return
        time.sleep(min(1.0, left))
        if tick is not None and time.monotonic() >= next_tick:
            next_tick += every
            tick()


def crawl(src: Dict, store: Dict[str, Dict], portal, *, limit_orgs: Optional[int] = None,
          only_orgs: Optional[Iterable[str]] = None, no_details: bool = False, checkpoint=None,
          progress=None, force_refresh: bool = False, stop=None,
          change_keys: Iterable[str] = DEFAULT_CHANGE_KEYS,
          listing_fields: Iterable[str] = DEFAULT_LISTING_FIELDS,
          detail_fields: Iterable[str] = ()) -> Dict:
    """Crawl one source into `store` (id -> record) through `portal`. Returns a status dict.

    checkpoint(org_name): called after each org's listing, every DETAIL_CHUNK detail pages and after
    each org, with the org in flight so the store can rewrite just that org's shard; the caller
    does a full save afterwards (deactivations touch many orgs).
    progress(snapshot): optional sink for the per-source progress counter (see crawler/progress.py).
    stop: optional threading.Event; when set the crawl winds down after its current request
    (the run is then flagged not-ok, so nothing is deactivated).
    detail_fields: record keys filled from a detail page; wiped before a refresh so a field the portal
    dropped doesn't linger from an older fetch.

    Refresh policy (force_refresh ignores all of it):
      * a tender's detail page is fetched when it is new, when its LISTING changed since we last
        saw it (change_keys - flagged needs_details so an interrupted run still picks it up), or
        as a safety net when the details are older than src["detail_max_age_days"] (default 14,
        0 = never). Otherwise it is left alone;
      * an organisation is re-listed at most every src["refresh_after_hours"] (default 48), and is
        skipped outright (no listing request) when its listing is fresh, the portal's tender count
        equals our stored active count, and no tender needs details. A count mismatch forces a
        re-list even inside the window.
    Retention: nothing is ever deleted. A tender missing from a complete, error-free full
    crawl gets active=False, inactive_since, and status "closed" (its closing date had
    passed) or "removed" (it vanished before its deadline). If it reappears it is reactivated.
    Partial runs (limit_orgs / only_orgs) and runs with an organisation-level error never deactivate.

    Parallel details: with src["detail_workers"] > 1 (only for adapters that set `parallel_details = True`)
    several detail pages are fetched at once, DETAIL_CHUNK at a time. The session's start-to-start delay
    still caps the request rate; a throttled worker stops the others and the whole source cools down once.

    Throttling: when the portal keeps answering 429/503/timeouts (http.Throttled), the source backs off
    and retries the same operation: first after src["throttle_probe_minutes"] (default 1), doubling each
    time it is still throttled, up to src["throttle_cooldown_minutes"] (default 15) per wait, or the
    portal's own Retry-After when it sends one. After a total of throttle_max_cooldowns x cooldown (default
    4 x 15 = 60 min) of waiting the source gives up for this run (everything saved so far is kept, the
    next run resumes). Other sources keep crawling meanwhile. How long the portal really needed is logged
    and recorded (status["throttle_recoveries"], throttle_events).
    """
    change_keys, listing_fields, detail_fields = tuple(change_keys), tuple(listing_fields), tuple(detail_fields)
    slug = src["slug"]
    want_details = bool(src.get("fetch_details")) and not no_details
    max_details = int(src.get("max_detail_fetches", 500))
    refresh_hours = float(src.get("refresh_after_hours", 48))     # how often an org is re-listed
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=refresh_hours - REFRESH_SLACK_HOURS)
              ).strftime("%Y-%m-%dT%H:%M:%SZ")
    max_age_days = float(src.get("detail_max_age_days", 14))      # safety net only; 0 disables
    detail_cutoff = ((datetime.now(timezone.utc) - timedelta(days=max_age_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
                     if max_age_days > 0 else "")

    cooldown_s = float(src.get("throttle_cooldown_minutes", DEFAULT_COOLDOWN_MINUTES)) * 60
    probe_s = min(cooldown_s, float(src.get("throttle_probe_minutes", DEFAULT_PROBE_MINUTES)) * 60)
    org_scope = getattr(portal, "throttle_scope", "source") == "org"   # per-host portals: a hung client != the whole source
    gave_up_in_a_row = 0
    may_be_empty = bool(getattr(portal, "may_be_empty", False))   # e.g. a notice board with nothing open: valid, not a parse failure
    max_cooldowns = int(src.get("throttle_max_cooldowns", DEFAULT_MAX_COOLDOWNS))
    patience_s = max_cooldowns * cooldown_s                  # total waiting per operation before giving up
    workers = max(1, min(MAX_DETAIL_WORKERS, int(src.get("detail_workers", 1))))
    if workers > 1 and not getattr(portal, "parallel_details", False):
        log.warning("%s: detail_workers=%d ignored: this portal type fetches details one at a time", slug, workers)
        workers = 1

    status = {"slug": slug, "name": src.get("name", slug), "ok": True, "started": now(),
              "orgs": 0, "orgs_skipped": 0, "orgs_failed": 0, "orgs_unreachable": 0, "listed": 0, "new": 0, "changed": 0,
              "details_failed": 0, "details_fetched": 0, "throttled": 0, "throttle_recoveries": [], "throttle_events": [],
              "gave_up": False, "errors": []}
    seen = set()
    rows_parsed = 0
    prog: Optional[Progress] = None

    def counters() -> Dict:
        return dict(listed=len(seen), new=status["new"], details_fetched=status["details_fetched"],
                    errors=len(status["errors"]))

    def error(msg: str) -> None:
        """An organisation-level failure: the listing is incomplete, so the run is not ok and nothing is deactivated."""
        log.error(msg)
        status["ok"] = False
        status["orgs_failed"] += 1
        if len(status["errors"]) < 20:
            status["errors"].append(msg)

    def stopping() -> bool:
        return stop is not None and stop.is_set()

    def event(**fields) -> None:
        """Throttle telemetry (kept per run in _throttle.json next to the data)."""
        if len(status["throttle_events"]) < MAX_EVENTS:
            status["throttle_events"].append({"t": now(), **fields})

    sess = getattr(portal, "sess", None)
    if sess is not None and hasattr(sess, "on_throttle"):
        sess.on_throttle = lambda ev: event(**ev)         # every 429/503, even those that clear on a retry

    def wait_out(what: str, e: Exception, n: int) -> float:
        """One back-off: the portal's Retry-After if it sent one, else probe_s doubled n times (capped at
        cooldown_s). Logged, shown in the progress, slept (waking early on stop). Returns the seconds waited."""
        retry_after = getattr(e, "retry_after", None)
        wait = max(1.0, min(float(retry_after), 3600.0)) if retry_after else min(cooldown_s, probe_s * 2 ** n)
        status["throttled"] += 1
        until = (datetime.now() + timedelta(seconds=wait)).strftime("%H:%M")
        shown = f"{wait / 60:g} min" if wait >= 60 else f"{wait:g} s"
        log.warning("%s: portal is throttling us (%s) - cooling down %s%s, retrying at %s (attempt %d)",
                    what, e, shown, " (its Retry-After)" if retry_after else "", until, n + 1)
        event(kind="wait", what=what, wait_s=wait, attempt=n + 1, retry_after=retry_after)
        previous = prog.fields.get("phase") if prog else None
        if prog:
            prog.update(phase=f"cooling down until {until} (throttled)", force=True, **counters())
        _sleep(wait, stop, tick=(lambda: prog.update()) if prog else None)   # heartbeat: not "stuck"
        if prog and previous:
            prog.update(phase=previous, force=True)
        return wait

    def recovered(what: str, waited: float, attempts: int) -> None:
        log.info("%s: portal answers again after %g min of waiting (%d attempts)", what, round(waited / 60, 1), attempts)
        status["throttle_recoveries"].append(round(waited))
        event(kind="recovered", what=what, waited_s=round(waited), attempts=attempts)

    def cooled(fn, what: str):
        """Run fn(); if the portal throttles us, back off and retry it until `patience_s` of waiting is used up."""
        waited, n = 0.0, 0
        while True:
            try:
                result = fn()
            except Throttled as e:
                if waited >= patience_s or stopping():
                    raise
                waited += wait_out(what, e, n)
                n += 1
                if stopping():
                    raise
                continue
            if n:
                recovered(what, waited, n)
            return result

    def want_details_off() -> None:
        nonlocal want_details
        want_details = False

    def detail_failed(msg: str) -> None:
        """One detail page failed: counted and reported, but the listing is intact so the run stays ok."""
        log.warning(msg)
        status["details_failed"] += 1
        if len(status["errors"]) < 20:
            status["errors"].append(msg)

    def upsert(row: Dict, org: str) -> Dict:
        stamp = now()
        rec = store.get(row["id"])
        if rec is None:
            rec = {"id": row["id"], "source": slug, "first_seen": stamp}
            store[row["id"]] = rec
            status["new"] += 1
        elif "details_fetched_at" in rec and any(
                row.get(k) and rec.get(k) and row[k] != rec[k] for k in change_keys):
            rec["needs_details"] = True      # e.g. closing date extended / corrigendum: refresh its details
            status["changed"] += 1
        for k in listing_fields:
            if row.get(k) not in (None, ""):
                rec[k] = row[k]
        rec.update(org=org, last_seen=stamp, active=True, status="active")
        rec.pop("inactive_since", None)   # reappeared after having dropped off
        return rec

    def details_stale(rec: Dict) -> bool:
        fetched = rec.get("details_fetched_at", "")
        return bool(force_refresh or not fetched or rec.get("needs_details")
                    or (detail_cutoff and fetched < detail_cutoff))

    def fresh_org_ids(name: str, count: int):
        """Ids of an org's stored tenders if it can be skipped this run, else None."""
        if force_refresh:
            return None
        recs = [r for r in store.values() if r.get("org") == name and r.get("active")]
        if not recs or len(recs) != count:          # new org, or portal count != our count
            return None
        if max(r.get("last_seen", "") for r in recs) < cutoff:
            return None                             # listing older than the refresh window
        if want_details and any(details_stale(r) for r in recs):
            return None
        return [r["id"] for r in recs]

    def mark_inactive(rec: Dict) -> None:
        now_ist = datetime.now(IST).strftime("%Y-%m-%dT%H:%M")
        rec["active"] = False
        rec["last_seen_active"] = rec.get("last_seen")
        rec["inactive_since"] = now()
        rec["status"] = "closed" if rec.get("close") and rec["close"] <= now_ist else "removed"

    def apply_detail(rec: Dict, fetched: Dict) -> None:
        for k in detail_fields:
            rec.pop(k, None)
        rec.update(fetched)
        rec.pop("needs_details", None)
        rec["details_fetched_at"] = now()
        status["details_fetched"] += 1

    def fetch_parallel(org: Dict, name: str, todo: List) -> None:
        """Phase B with `workers` threads, DETAIL_CHUNK pages per round; the store is only touched from
        this thread. If any worker is throttled the others stop starting new requests, the throttled
        pages are collected, the source cools down ONCE, and only those pages are retried."""
        for lo in range(0, len(todo), DETAIL_CHUNK):
            chunk = todo[lo: lo + DETAIL_CHUNK]
            waited, n = 0.0, 0
            while chunk:
                hit = threading.Event()

                def task(item):
                    if hit.is_set() or stopping():
                        raise _Skip()
                    try:
                        return portal.fetch_detail(org, item[1])
                    except (Throttled, Restart):
                        hit.set()                          # the others stop starting requests: stale / throttled
                        raise

                retry, last, applied = [], None, 0
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = [(item, pool.submit(task, item)) for item in chunk]
                    # results are applied in order, on this thread, as each page completes
                    for item, fut in futures:                  # results are applied in order, on this thread
                        rec = item[0]
                        try:
                            apply_detail(rec, fut.result())
                        except _Skip:
                            retry.append(item)
                        except Throttled as e:
                            retry.append(item)
                            last = e
                        except Restart:
                            raise
                        except DetailsBlocked as e:
                            detail_failed(str(e))
                            want_details_off()
                            return
                        except DetailFailed as e:
                            detail_failed(str(e))
                        except Exception as e:  # noqa: BLE001
                            detail_failed(f"detail {rec['id']}: {e.__class__.__name__}: {e}"[:200])
                        applied += 1
                        if prog and applied % PROGRESS_EVERY == 0:
                            prog.update(org_frac=min(1.0, (lo + applied) / len(todo)), **counters())
                if checkpoint:
                    checkpoint(name)
                if prog:
                    prog.update(org_frac=min(1.0, (lo + len(chunk) - len(retry)) / len(todo)), **counters())
                if stopping():
                    return                                  # unfetched pages are picked up by the next run
                if retry and last is None:                  # skipped only because another worker was throttled
                    last = Throttled("throttled")
                if retry:
                    if waited >= patience_s:
                        raise last
                    waited += wait_out(f"{name}: details", last, n)
                    n += 1
                    if stopping():
                        return
                elif n:
                    recovered(f"{name}: details", waited, n)
                chunk = retry

    def walk_org(org: Dict) -> None:
        """Phase A: list the org and checkpoint it.
        Phase B: fetch details in chunks, checkpointing after each chunk, so a crash,
        Ctrl-C or the per-run detail cap never loses more than DETAIL_CHUNK pages."""
        name = org["name"]
        pending: List = []
        listed_here = [0]

        def collect() -> None:
            """List the org. Restartable: a throttled retry starts over and the counters are reset
            (rows already stored are simply upserted again)."""
            nonlocal rows_parsed
            rows_parsed -= listed_here[0]
            listed_here[0] = 0
            pending.clear()
            for row in portal.walk_org(org):
                rows_parsed += 1
                listed_here[0] += 1
                seen.add(row["id"])
                rec = upsert(row, name)
                if want_details and row.get("detail_ref") is not None and details_stale(rec):
                    pending.append((rec, row))

        cooled(collect, name)
        if listed_here[0] == 0:
            log.warning("%s: listing parsed 0 rows", name)
        if checkpoint:
            checkpoint(name)
        if prog:
            prog.update(phase="details", org_frac=0.0, force=True, **counters())
        if not pending:
            return
        # never-detailed tenders first, then changed/oldest ones; newest published first within each
        pending.sort(key=lambda p: p[0].get("pub") or "", reverse=True)
        pending.sort(key=lambda p: p[0].get("details_fetched_at", ""))
        todo = pending[: max(0, max_details - status["details_fetched"])]
        log.info("%s: %d need details, fetching %d this run%s", name, len(pending), len(todo),
                 f" ({workers} workers)" if workers > 1 else "")
        if workers > 1:
            fetch_parallel(org, name, todo)
            return
        for n, (rec, row) in enumerate(todo, 1):
            if stop is not None and stop.is_set():
                return
            try:
                apply_detail(rec, cooled(lambda: portal.fetch_detail(org, row), f"{name}: detail {rec['id']}"))
            except (Restart, Throttled):
                raise  # restart + re-list (already-fetched tenders are skipped next time) / give up on the source
            except DetailsBlocked as e:
                detail_failed(str(e))
                want_details_off()
                return
            except DetailFailed as e:
                detail_failed(str(e))
            except Exception as e:  # noqa: BLE001
                detail_failed(f"detail {rec['id']}: {e.__class__.__name__}: {e}"[:200])
            if checkpoint and n % DETAIL_CHUNK == 0:
                checkpoint(name)
            if prog and (n % 10 == 0 or n == len(todo)):
                prog.update(org_frac=n / len(todo), **counters())

    try:
        orgs = cooled(portal.list_orgs, "organisation list")
    except Exception as e:  # noqa: BLE001
        error(f"organisation list: {e}")
        status["finished"] = now()
        status["requests"] = portal.requests_made()
        return status
    if not orgs and not may_be_empty:
        error("organisation list parsed empty")
    full_run = not limit_orgs and not only_orgs
    if only_orgs:
        wanted = {o.lower() for o in only_orgs}
        orgs = [o for o in orgs if o["name"].lower() in wanted]
    if limit_orgs:
        orgs = orgs[:limit_orgs]

    prog = Progress(slug, status["name"], len(orgs), sum(o["count"] for o in orgs), sink=progress)
    for i, org in enumerate(orgs, 1):
        if stop is not None and stop.is_set():
            error("interrupted before all organisations were crawled")
            break
        name = org["name"]
        before = rows_parsed
        prog.start_org(name, org["count"])
        skip_ids = fresh_org_ids(name, org["count"])
        if skip_ids is not None:
            log.info("%s: unchanged (%d tenders, fresh within %gh) - skipped", name, len(skip_ids), refresh_hours)
            seen.update(skip_ids)          # still on the portal, so don't deactivate them below
            status["orgs"] += 1
            status["orgs_skipped"] += 1
            prog.update(**counters())
            prog.finish_org(skipped=True)
            continue
        log.info("[%d/%d] %s (portal says %d)", i, len(orgs), name, org["count"])
        gave_up = throttled_out = unreachable = False
        for attempt in range(MAX_RESTARTS + 1):
            try:
                walk_org(org)
                break
            except Restart:
                if attempt == MAX_RESTARTS:
                    error(f"{name}: session kept going stale")
                    break
                log.info("%s: stale session, restarting (%d)", name, attempt + 1)
                try:
                    fresh = portal.restart_org(org)
                    if fresh is None:
                        error(f"{name}: missing from organisation list after restart")
                        break
                    org = fresh
                except Exception as e:  # noqa: BLE001
                    error(f"{name}: restart failed: {e}")
                    break
            except Throttled as e:
                throttled_out = True
                gave_up_in_a_row += 1
                gave_up = not org_scope or gave_up_in_a_row >= MAX_ORGS_GIVEN_UP_IN_A_ROW
                error(f"{name}: portal keeps throttling us ({e}) after {patience_s / 60:g} min of waiting - "
                      + (f"giving up on {slug} for this run (saved progress is kept)"
                         if gave_up else "skipping this organisation"))
                status["gave_up"] = status["gave_up"] or gave_up
                break
            except HostNotFound as e:
                # the portal lists a site that no longer exists: not a crawl failure (the run stays ok),
                # and we cannot tell whether its tenders were removed, so what we hold stays active
                unreachable = True
                status["orgs_unreachable"] += 1
                seen.update(r["id"] for r in store.values() if r.get("org") == name and r.get("active"))
                log.warning("%s: host does not exist (%s) - skipping; stored tenders are kept", name, e)
                break
            except Exception as e:  # noqa: BLE001
                error(f"{name}: {e}")
                break
        if not throttled_out:
            gave_up_in_a_row = 0                  # this organisation did not give up: the streak is broken
        got = rows_parsed - before
        if unreachable:
            pass
        elif got == 0 and org["count"] > 0 and status["ok"]:
            error(f"{name}: portal lists {org['count']} tenders but 0 rows parsed (markup changed?)")
        elif got < org["count"]:
            log.warning("%s: got %d of %d tenders", name, got, org["count"])
        status["orgs"] += 1
        if checkpoint:
            checkpoint(name)
        prog.update(**counters())
        prog.finish_org()
        if gave_up:
            break

    status["listed"] = len(seen)
    status["deactivated"] = 0
    if full_run and status["ok"] and (orgs or may_be_empty):
        for tid, rec in store.items():
            if tid not in seen and rec.get("active"):
                mark_inactive(rec)
                status["deactivated"] += 1
    status["active"] = sum(1 for r in store.values() if r.get("active"))      # after retention: what the store really holds
    status["finished"] = now()
    status["requests"] = portal.requests_made()
    prog.update(**counters())
    prog.finish()
    return status
