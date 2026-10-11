"""Several detail pages in flight at once (src["detail_workers"]): speed-up, one shared cool-down, safe rate cap."""
import threading
import time
from types import SimpleNamespace

import pytest
from curl_cffi.requests import exceptions as E

from crawler import engine, http
from crawler.http import PortalSession, Throttled

from test_throttle import FakePortal, SRC, crawl, sleeps  # noqa: F401  (sleeps is a fixture)


class ParallelPortal(FakePortal):
    parallel_details = True

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.lock = threading.Lock()
        self.active = self.max_active = 0

    def fetch_detail(self, org, row):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.05)                                   # a slow portal: this is what workers overlap
            with self.lock:
                self._maybe("detail")
            return {"details": {"k": row["id"]}, "category": "Works"}
        finally:
            with self.lock:
                self.active -= 1


def src(workers):
    return dict(SRC, detail_workers=workers, max_detail_fetches=500)


@pytest.fixture(autouse=True)
def small_chunks(monkeypatch):
    monkeypatch.setattr(engine, "DETAIL_CHUNK", 5)             # so 12 tenders span several rounds


def test_workers_fetch_details_at_the_same_time_and_every_page_lands_on_its_own_tender(sleeps):
    portal, store = ParallelPortal(rows=12), {}
    st = crawl(portal, store, src=src(3))
    assert portal.max_active == 3                               # really concurrent
    assert st["ok"] is True and st["details_fetched"] == 12 and st["details_failed"] == 0
    assert all(r["details"] == {"k": r["id"]} for r in store.values())     # results not mixed up between tenders
    assert sleeps == []


def test_one_worker_stays_sequential(sleeps):
    portal = ParallelPortal(rows=6)
    crawl(portal, {}, src=src(1))
    assert portal.max_active == 1


def test_portals_that_cannot_do_it_ignore_detail_workers(sleeps):
    portal = FakePortal(rows=4)                                 # no parallel_details attribute
    st = crawl(portal, {}, src=src(4))
    assert st["details_fetched"] == 4


def test_a_throttled_worker_stops_the_others_and_the_source_cools_down_once(sleeps):
    portal, store = ParallelPortal(rows=12, throttle={"detail": 2}), {}
    st = crawl(portal, store, src=src(3))
    assert sleeps == [900.0] and st["throttled"] == 1           # not one cool-down per worker
    assert st["details_fetched"] == 12 and st["gave_up"] is False
    assert all(r.get("details") for r in store.values())        # the throttled pages were retried afterwards


def test_a_portal_that_keeps_throttling_makes_the_source_give_up_after_the_usual_cooldowns(sleeps):
    portal, store = ParallelPortal(rows=12, always={"detail"}), {}
    st = crawl(portal, store, src=src(3))
    assert sleeps == [900.0] * engine.DEFAULT_MAX_COOLDOWNS
    assert st["gave_up"] is True and st["ok"] is False
    assert len(store) == 12                                     # the listing is kept; only details are missing


def test_a_failed_detail_does_not_stop_the_others(sleeps):
    class Flaky(ParallelPortal):
        def fetch_detail(self, org, row):
            if row["id"] == "T3":
                raise engine.DetailFailed("detail T3: no label/value pairs")
            return super().fetch_detail(org, row)

    st = crawl(Flaky(rows=8), {}, src=src(4))
    assert st["details_fetched"] == 7 and st["details_failed"] == 1 and st["ok"] is True


def test_a_stop_request_ends_the_parallel_fetch_quickly(sleeps):
    stop = threading.Event()
    portal = ParallelPortal(rows=12)
    original = portal.fetch_detail

    def fetch(org, row):
        stop.set()
        return original(org, row)

    portal.fetch_detail = fetch
    st = crawl(portal, {}, src=src(3), stop=stop)
    assert st["details_fetched"] < 12                           # remaining pages are left for the next run


# ---------------------------------------------------------------- transport: one rate cap for all workers
def test_request_starts_stay_at_least_delay_apart_across_threads():
    starts = []
    sess = PortalSession("https://x.test", delay_seconds=0.05, ignore_robots=True)

    def request(method, url, **kw):
        starts.append(time.monotonic())
        time.sleep(0.15)                                       # slower than the delay: requests overlap in flight
        return SimpleNamespace(status_code=200, headers={"Content-Type": "text/html"}, content=b"ok",
                               raise_for_status=lambda: None)

    sess._http = SimpleNamespace(request=request, close=lambda: None)
    threads = [threading.Thread(target=lambda: [sess.get("https://x.test/p") for _ in range(3)]) for _ in range(4)]
    t0 = time.monotonic()
    [t.start() for t in threads]
    [t.join() for t in threads]
    elapsed = time.monotonic() - t0
    gaps = [b - a for a, b in zip(sorted(starts), sorted(starts)[1:])]
    assert len(starts) == 12 and sess.requests_made == 12
    assert min(gaps) >= 0.045                                  # never faster than 1/delay
    assert elapsed < 12 * 0.15 * 0.6                           # but overlapped, not serialised (12 x 0.15 s = 1.8 s)
