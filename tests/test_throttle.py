"""Throttling: what counts as it (http.py), and the cool-down / retry / give-up policy (engine.py)."""
import threading
from types import SimpleNamespace

import pytest
from curl_cffi.requests import exceptions as E

from crawler import engine, http
from crawler.http import PortalSession, Throttled

OPERATION_TIMEOUT = "Failed to perform, curl: (28) Operation timed out after 40007 milliseconds with 0 bytes received."
CONNECT_TIMEOUT = "Failed to perform, curl: (28) Connection timed out after 5001 milliseconds"
DNS = "Failed to perform, curl: (6) Could not resolve host: dead.example"


# ---------------------------------------------------------------- transport: what is throttling?
def session(responses, monkeypatch):
    """A PortalSession whose HTTP layer plays `responses` (status codes or exceptions), one per attempt."""
    sleeps, calls = [], []
    monkeypatch.setattr(http.time, "sleep", lambda s: sleeps.append(s))
    sess = PortalSession("https://x.test", delay_seconds=0, ignore_robots=True)

    def request(method, url, **kw):
        calls.append(url)
        item = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(item, Exception):
            raise item

        def raise_for_status():
            if item >= 400:
                raise E.HTTPError(f"HTTP {item}")

        return SimpleNamespace(status_code=item, headers={"Content-Type": "text/html"}, content=b"ok",
                               raise_for_status=raise_for_status)

    sess._http = SimpleNamespace(request=request, close=lambda: None)
    return sess, calls, sleeps


@pytest.mark.parametrize("failure", [503, 429, E.Timeout(OPERATION_TIMEOUT, 28), E.Timeout("x", 52), E.Timeout("x", 56)])
def test_signs_of_throttling_raise_throttled_after_the_short_retries(failure, monkeypatch):
    sess, calls, sleeps = session([failure], monkeypatch)
    with pytest.raises(Throttled):
        sess.get("https://x.test/p")
    assert len(calls) == http.RETRIES
    assert sleeps == [5, 10]                       # backs off between attempts, but not after the last one


@pytest.mark.parametrize("failure", [E.DNSError(DNS, 6), E.Timeout(CONNECT_TIMEOUT, 28), E.ConnectTimeout(CONNECT_TIMEOUT, 28), 500, 404])
def test_dead_hosts_and_ordinary_errors_are_not_throttling(failure, monkeypatch):
    sess, calls, _ = session([failure], monkeypatch)
    with pytest.raises(Exception) as ei:
        sess.get("https://x.test/p")
    assert not isinstance(ei.value, Throttled)     # no 15-minute cool-down for a dead subdomain or a plain 404/500
    assert isinstance(ei.value, E.RequestException)


def test_a_throttled_response_that_recovers_within_the_short_retries_is_just_a_success(monkeypatch):
    sess, calls, sleeps = session([503, 200], monkeypatch)
    assert sess.get("https://x.test/p") == "ok" and len(calls) == 2 and sleeps == [5]


# ---------------------------------------------------------------- engine: cool down, retry, give up
def row(i):
    return {"id": f"T{i}", "ref": f"R{i}", "title": f"Tender {i}", "chain": "A", "close": "2999-01-01T00:00",
            "detail_ref": f"u{i}"}


class FakePortal:
    def __init__(self, orgs=None, rows=2, throttle=None, always=None, scope="source"):
        self.orgs = orgs or [{"name": "A", "count": rows}]
        self.rows = rows
        self.throttle = dict(throttle or {})        # operation -> how many calls fail with Throttled first
        self.always = set(always or ())             # operations that are throttled forever
        self.calls = []
        self.throttle_scope = scope

    def _maybe(self, op):
        self.calls.append(op)
        if op in self.always:
            raise Throttled("HTTP 503")
        if self.throttle.get(op, 0) > 0:
            self.throttle[op] -= 1
            raise Throttled("HTTP 503")

    def list_orgs(self):
        self._maybe("orgs")
        return self.orgs

    def walk_org(self, org):
        self._maybe(f"walk:{org['name']}:start")
        for i in range(org["count"]):
            if i == 1:
                self._maybe(f"walk:{org['name']}:mid")        # throttled part-way through the listing
            yield row(i if org["name"] == "A" else 100 + i)

    def fetch_detail(self, org, row):
        self._maybe("detail")
        return {"details": {"k": "v"}, "category": "Works"}

    def restart_org(self, org):
        return org

    def requests_made(self):
        return 0


SRC = {"slug": "t", "name": "T", "fetch_details": True, "max_detail_fetches": 50}


@pytest.fixture
def sleeps(monkeypatch):
    rec = []
    monkeypatch.setattr(engine, "_sleep", lambda seconds, stop=None: rec.append(seconds))
    return rec


def crawl(portal, store=None, src=None, **kw):
    return engine.crawl(src or SRC, {} if store is None else store, portal, **kw)


def test_the_default_cooldown_is_15_minutes_and_the_crawl_recovers(sleeps):
    portal = FakePortal(throttle={"orgs": 1})
    st = crawl(portal)
    assert sleeps == [900.0]                       # 15 minutes
    assert st["ok"] is True and st["throttled"] == 1 and st["gave_up"] is False and st["listed"] == 2


def test_the_cooldown_is_configurable_per_source(sleeps):
    crawl(FakePortal(throttle={"orgs": 2}), src=dict(SRC, throttle_cooldown_minutes=2))
    assert sleeps == [120.0, 120.0]


def test_a_listing_throttled_midway_is_retried_from_scratch_without_double_counting(sleeps):
    portal = FakePortal(rows=3, throttle={"walk:A:mid": 1})
    store = {}
    st = crawl(portal, store)
    assert sleeps == [900.0] and set(store) == {"T0", "T1", "T2"}
    assert st["listed"] == 3 and st["new"] == 3 and st["ok"] is True
    assert portal.calls.count("walk:A:start") == 2                       # the walk really was restarted


def test_a_throttled_detail_is_retried_and_then_fetched(sleeps):
    st_store = {}
    st = crawl(FakePortal(throttle={"detail": 1}), st_store)
    assert sleeps == [900.0] and st["details_fetched"] == 2 and st["details_failed"] == 0
    assert all(r.get("category") == "Works" for r in st_store.values())


def test_a_portal_that_keeps_throttling_makes_the_source_give_up_and_keeps_its_data(sleeps):
    store = {"KEEP": {"id": "KEEP", "source": "t", "org": "A", "active": True}}
    portal = FakePortal(orgs=[{"name": "A", "count": 2}, {"name": "B", "count": 2}], always={"walk:A:start"})
    st = crawl(portal, store)
    assert sleeps == [900.0] * engine.DEFAULT_MAX_COOLDOWNS             # 4 cool-downs = 1 hour, then it stops asking
    assert st["ok"] is False and st["gave_up"] is True and st["throttled"] == 4
    assert any("keeps throttling" in e and "giving up" in e for e in st["errors"])
    assert not any(c.startswith("walk:B") for c in portal.calls)        # B was never attempted: the source stopped
    assert store["KEEP"]["active"] is True                              # and nothing is deactivated


def test_max_cooldowns_is_configurable(sleeps):
    crawl(FakePortal(always={"walk:A:start"}), src=dict(SRC, throttle_max_cooldowns=1))
    assert sleeps == [900.0]


def test_when_the_organisation_list_itself_stays_throttled_the_source_fails_cleanly(sleeps):
    st = crawl(FakePortal(always={"orgs"}))
    assert st["ok"] is False and len(sleeps) == engine.DEFAULT_MAX_COOLDOWNS
    assert any(e.startswith("organisation list") for e in st["errors"])


def test_per_host_portals_skip_a_throttled_client_but_stop_after_three_in_a_row(sleeps):
    orgs = [{"name": n, "count": 1} for n in "ABCDEF"]
    # A is fine, B gives up after its cool-downs, C is fine (so the streak is broken), then D, E, F are fine
    portal = FakePortal(orgs=orgs, always={"walk:B:start"}, scope="org")
    st = crawl(portal)
    assert st["orgs"] == 6 and st["orgs_failed"] == 1 and st["gave_up"] is False
    assert all(any(c == f"walk:{n}:start" for c in portal.calls) for n in "ACDEF")     # the rest were crawled

    portal = FakePortal(orgs=orgs, always={"walk:B:start", "walk:C:start", "walk:D:start"}, scope="org")
    st = crawl(portal)
    assert st["gave_up"] is True and st["orgs_failed"] == 3                              # B, C, D in a row -> abort
    assert not any(c.startswith(("walk:E", "walk:F")) for c in portal.calls)


def test_a_stop_request_during_a_cooldown_ends_the_wait_without_retrying(monkeypatch):
    stop = threading.Event()
    waits = []
    monkeypatch.setattr(engine, "_sleep", lambda seconds, stop=None: (waits.append(seconds), stop.set()))
    portal = FakePortal(throttle={"orgs": 1})
    st = crawl(portal, stop=stop)
    assert waits == [900.0] and portal.calls == ["orgs"]                  # no second attempt after the stop
    assert st["ok"] is False


def test_sleep_wakes_early_when_stopped():
    import time

    stop = threading.Event()
    threading.Timer(0.2, stop.set).start()
    t = time.monotonic()
    engine._sleep(30, stop)                                               # would be 30 s without the stop
    assert time.monotonic() - t < 3


def test_progress_shows_the_cooldown_while_waiting(monkeypatch):
    seen = []
    monkeypatch.setattr(engine, "_sleep", lambda seconds, stop=None: None)
    st = crawl(FakePortal(throttle={"detail": 1}), progress=lambda snap: seen.append(snap["phase"]))
    assert any(p.startswith("cooling down until") for p in seen)
    assert seen[-1] == "finished" and st["ok"] is True                    # and the phase is restored afterwards
