"""Throttling: what counts as it (http.py), and the cool-down / retry / give-up policy (engine.py)."""
import threading
from types import SimpleNamespace

import pytest
from curl_cffi.requests import exceptions as E

from crawler import engine, http
from crawler.http import HostNotFound, PortalSession, Throttled

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


@pytest.mark.parametrize("failure", [503, 429, E.Timeout(OPERATION_TIMEOUT, 28), E.Timeout("x", 52), E.Timeout("x", 56),
                                     E.SSLError("Failed to perform, curl: (35) Recv failure: Connection was reset.", 35)])
def test_signs_of_throttling_raise_throttled_after_the_short_retries(failure, monkeypatch):
    sess, calls, sleeps = session([failure], monkeypatch)
    with pytest.raises(Throttled):
        sess.get("https://x.test/p")
    assert len(calls) == http.RETRIES
    assert sleeps == [5, 10]                       # backs off between attempts, but not after the last one


@pytest.mark.parametrize("failure", [E.DNSError(DNS, 6), E.Timeout(CONNECT_TIMEOUT, 28), E.ConnectTimeout(CONNECT_TIMEOUT, 28), 500, 404,
                                     E.SSLError("Failed to perform, curl: (35) OpenSSL SSL_connect: SSL_ERROR_SYSCALL in connection to x:443", 35)])
def test_dead_hosts_and_ordinary_errors_are_not_throttling(failure, monkeypatch):
    sess, calls, _ = session([failure], monkeypatch)
    with pytest.raises(Exception) as ei:
        sess.get("https://x.test/p")
    assert not isinstance(ei.value, Throttled)     # no 15-minute cool-down for a dead subdomain or a plain 404/500
    assert isinstance(ei.value, E.RequestException)


def test_a_host_that_does_not_resolve_raises_host_not_found(monkeypatch):
    sess, calls, _ = session([E.DNSError(DNS, 6)], monkeypatch)
    with pytest.raises(http.HostNotFound) as ei:
        sess.get("https://dead.example/p")
    assert len(calls) == http.RETRIES and isinstance(ei.value, E.RequestException)   # a flaky resolver gets its retries


def test_a_throttled_response_that_recovers_within_the_short_retries_is_just_a_success(monkeypatch):
    sess, calls, sleeps = session([503, 200], monkeypatch)
    assert sess.get("https://x.test/p") == "ok" and len(calls) == 2 and sleeps == [5]


# ---------------------------------------------------------------- engine: cool down, retry, give up
def row(i):
    return {"id": f"T{i}", "ref": f"R{i}", "title": f"Tender {i}", "chain": "A", "close": "2999-01-01T00:00",
            "detail_ref": f"u{i}"}


class FakePortal:
    def __init__(self, orgs=None, rows=2, throttle=None, always=None, scope="source", dead=()):
        self.orgs = orgs or [{"name": "A", "count": rows}]
        self.rows = rows
        self.throttle = dict(throttle or {})        # operation -> how many calls fail with Throttled first
        self.always = set(always or ())             # operations that are throttled forever
        self.dead = set(dead)                       # organisations whose host does not exist
        self.calls = []
        self.throttle_scope = scope

    def _maybe(self, op):
        self.calls.append(op)
        if op.startswith("walk:") and op.split(":")[1] in self.dead:
            raise HostNotFound("curl: (6) Could not resolve host")
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
    monkeypatch.setattr(engine, "_sleep", lambda seconds, stop=None, **_: rec.append(seconds))
    return rec


def crawl(portal, store=None, src=None, **kw):
    return engine.crawl(src or SRC, {} if store is None else store, portal, **kw)


def schedule(cooldown=900.0, probe=60.0, patience=3600.0):
    """The waits a portal that never recovers sees: probe, 2x, 4x ... capped at the cool-down, until the
    total waited reaches the patience (max_cooldowns x cooldown)."""
    waits, n = [], 0
    while sum(waits) < patience:
        waits.append(min(cooldown, probe * 2 ** n))
        n += 1
    return waits


def test_the_first_wait_is_a_one_minute_probe_and_the_crawl_recovers(sleeps):
    portal = FakePortal(throttle={"orgs": 1})
    st = crawl(portal)
    assert sleeps == [60.0]
    assert st["ok"] is True and st["throttled"] == 1 and st["gave_up"] is False and st["listed"] == 2
    assert st["throttle_recoveries"] == [60]                               # how long the portal really needed


def test_waits_double_while_the_portal_is_still_throttling_up_to_the_cooldown_cap(sleeps):
    crawl(FakePortal(throttle={"orgs": 6}))
    assert sleeps == [60.0, 120.0, 240.0, 480.0, 900.0, 900.0]            # 15 min is the cap, not the first wait


def test_the_probe_and_the_cap_are_configurable_per_source(sleeps):
    crawl(FakePortal(throttle={"orgs": 3}), src=dict(SRC, throttle_cooldown_minutes=2, throttle_probe_minutes=0.5))
    assert sleeps == [30.0, 60.0, 120.0]
    sleeps.clear()
    crawl(FakePortal(throttle={"orgs": 2}), src=dict(SRC, throttle_cooldown_minutes=2, throttle_probe_minutes=2))
    assert sleeps == [120.0, 120.0]                                        # probe == cap: the old flat cool-down


def test_the_portals_retry_after_is_obeyed_instead_of_the_schedule(sleeps):
    class Told(FakePortal):
        def list_orgs(self):
            if not self.calls:
                self.calls.append("orgs")
                raise Throttled("HTTP 429", retry_after=7)
            return super().list_orgs()

    st = crawl(Told())
    assert sleeps == [7.0] and st["ok"] is True
    assert st["throttle_events"][0]["kind"] == "wait" and st["throttle_events"][0]["retry_after"] == 7


def test_events_from_the_session_are_recorded(sleeps):
    class Sess:
        on_throttle = None

    class Talkative(FakePortal):
        sess = Sess()

        def fetch_detail(self, org, row):
            self.sess.on_throttle({"kind": "429", "headers": {"retry-after": "5"}})
            return super().fetch_detail(org, row)

    st = crawl(Talkative())
    assert [e["kind"] for e in st["throttle_events"]].count("429") == 2 and st["throttle_events"][0]["headers"]


def test_a_listing_throttled_midway_is_retried_from_scratch_without_double_counting(sleeps):
    portal = FakePortal(rows=3, throttle={"walk:A:mid": 1})
    store = {}
    st = crawl(portal, store)
    assert sleeps == [60.0] and set(store) == {"T0", "T1", "T2"}
    assert st["listed"] == 3 and st["new"] == 3 and st["ok"] is True
    assert portal.calls.count("walk:A:start") == 2                       # the walk really was restarted


def test_a_throttled_detail_is_retried_and_then_fetched(sleeps):
    st_store = {}
    st = crawl(FakePortal(throttle={"detail": 1}), st_store)
    assert sleeps == [60.0] and st["details_fetched"] == 2 and st["details_failed"] == 0
    assert all(r.get("category") == "Works" for r in st_store.values())


def test_a_portal_that_keeps_throttling_makes_the_source_give_up_and_keeps_its_data(sleeps):
    store = {"KEEP": {"id": "KEEP", "source": "t", "org": "A", "active": True}}
    portal = FakePortal(orgs=[{"name": "A", "count": 2}, {"name": "B", "count": 2}], always={"walk:A:start"})
    st = crawl(portal, store)
    assert sleeps == schedule()                                         # about an hour of waiting in all, then it stops asking
    assert st["ok"] is False and st["gave_up"] is True and st["throttled"] == len(schedule())
    assert any("keeps throttling" in e and "giving up" in e for e in st["errors"])
    assert not any(c.startswith("walk:B") for c in portal.calls)        # B was never attempted: the source stopped
    assert store["KEEP"]["active"] is True                              # and nothing is deactivated


def test_max_cooldowns_sets_the_total_patience(sleeps):
    crawl(FakePortal(always={"walk:A:start"}), src=dict(SRC, throttle_max_cooldowns=1))
    assert sleeps == schedule(patience=900.0)


def test_when_the_organisation_list_itself_stays_throttled_the_source_fails_cleanly(sleeps):
    st = crawl(FakePortal(always={"orgs"}))
    assert st["ok"] is False and sleeps == schedule()
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


def test_a_client_whose_host_does_not_exist_is_skipped_without_failing_the_run(sleeps):
    gone = {"id": "OLD", "source": "t", "org": "B", "active": True}          # stored earlier, B's host has since vanished
    store = {"OLD": gone, "ELSEWHERE": {"id": "ELSEWHERE", "source": "t", "org": "C", "active": True}}
    portal = FakePortal(orgs=[{"name": "A", "count": 2}, {"name": "B", "count": 5}], dead={"B"}, scope="org")
    st = crawl(portal, store)
    assert st["ok"] is True and st["orgs_unreachable"] == 1 and st["orgs_failed"] == 0 and st["errors"] == []
    assert sleeps == []                                                      # no cool-down for a dead host
    assert gone["active"] is True                                            # we cannot tell it was removed: kept
    assert store["ELSEWHERE"]["active"] is False                             # but the healthy run still retires other leftovers
    assert set(store) >= {"T0", "T1"}                                        # and A was crawled normally


def test_a_stop_request_during_a_cooldown_ends_the_wait_without_retrying(monkeypatch):
    stop = threading.Event()
    waits = []
    monkeypatch.setattr(engine, "_sleep", lambda seconds, stop=None, **_: (waits.append(seconds), stop.set()))
    portal = FakePortal(throttle={"orgs": 1})
    st = crawl(portal, stop=stop)
    assert waits == [60.0] and portal.calls == ["orgs"]                  # no second attempt after the stop
    assert st["ok"] is False


def test_sleep_wakes_early_when_stopped():
    import time

    stop = threading.Event()
    threading.Timer(0.2, stop.set).start()
    t = time.monotonic()
    engine._sleep(30, stop)                                               # would be 30 s without the stop
    assert time.monotonic() - t < 3


def test_sleep_calls_its_heartbeat_while_waiting():
    ticks = []
    engine._sleep(1.3, tick=lambda: ticks.append(1), every=0.4)
    assert 2 <= len(ticks) <= 4                                          # roughly every 0.4 s over 1.3 s


def test_a_cooldown_keeps_writing_progress_so_it_is_not_reported_stale(monkeypatch):
    from datetime import datetime, timezone
    from crawler.run import staleness

    stamps = []

    def sleep(seconds, stop=None, tick=None, every=30.0):
        for _ in range(3):
            tick()                                                       # what _sleep does every `every` seconds

    monkeypatch.setattr(engine, "_sleep", sleep)
    crawl(FakePortal(throttle={"detail": 1}), progress=lambda snap: stamps.append(snap))
    waiting = [s for s in stamps if s["phase"].startswith("cooling down until")]
    assert len(waiting) >= 4                                             # the initial one + three heartbeats
    assert staleness(waiting[-1], now=datetime.now(timezone.utc)) == ""


def test_progress_shows_the_cooldown_while_waiting(monkeypatch):
    seen = []
    monkeypatch.setattr(engine, "_sleep", lambda seconds, stop=None, **_: None)
    st = crawl(FakePortal(throttle={"detail": 1}), progress=lambda snap: seen.append(snap["phase"]))
    assert any(p.startswith("cooling down until") for p in seen)
    assert seen[-1] == "finished" and st["ok"] is True                    # and the phase is restored afterwards


# ---------------------------------------------------------------- transport: what the portal said, and slowing down
def response(status, headers=None, body=b"Too Many Requests"):
    h = {"Content-Type": "text/html", **(headers or {})}

    def raise_for_status():
        if status >= 400:
            raise E.HTTPError(f"HTTP {status}")

    return SimpleNamespace(status_code=status, headers=h, content=body, raise_for_status=raise_for_status)


def session_with(replies, monkeypatch, **kw):
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    sess = PortalSession("https://x.test", delay_seconds=kw.pop("delay", 0), ignore_robots=True, **kw)
    seen = iter(replies)
    sess._http = SimpleNamespace(request=lambda m, u, **k: next(seen), close=lambda: None)
    return sess


def test_the_throttle_response_headers_body_and_retry_after_are_captured(monkeypatch):
    hdr = {"Retry-After": "42", "X-RateLimit-Remaining": "0", "Server": "nginx", "Set-Cookie": "secret=1"}
    sess = session_with([response(429, hdr)] * 3, monkeypatch)
    events = []
    sess.on_throttle = events.append
    with pytest.raises(Throttled) as ei:
        sess.get("https://x.test/p")
    assert ei.value.retry_after == 42.0
    assert len(events) == 3 and events[0]["kind"] == "429" and events[0]["body"] == "Too Many Requests"
    assert events[0]["headers"] == {"retry-after": "42", "x-ratelimit-remaining": "0", "server": "nginx"}   # no cookies
    assert events[1]["since_prev_s"] is not None and events[0]["since_prev_s"] is None


def test_retry_after_accepts_an_http_date():
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    soon = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=90), usegmt=True)
    assert 80 < http._parse_retry_after(soon) <= 90 and http._parse_retry_after("12") == 12.0
    assert http._parse_retry_after("soon") is None and http._parse_retry_after(None) is None


def test_a_429_slows_the_request_pace_at_most_once_a_minute_and_it_relaxes_after_a_quiet_spell(monkeypatch):
    sess = session_with([response(429)], monkeypatch, delay=1.0)
    sess._ema_gap = 3.5                                     # we have really been doing one request per 3.5 s
    sig = http._throttle_signal(response(429))
    sess._note_throttle(sig)
    assert sess._interval == pytest.approx(5.25)            # 1.5x our real pace, not 1.5x the 1 s setting
    sess._note_throttle(sig)
    assert sess._interval == pytest.approx(5.25)            # a burst of 429s does not compound the slow-down
    sess._slowed_at -= 61
    sess._ema_gap = 5.25                                    # the pace has really slowed to the new spacing
    sess._note_throttle(sig)
    assert sess._interval == pytest.approx(7.875)
    sess._interval = 12.0
    sess._slowed_at -= 61
    sess._note_throttle(sig)
    assert sess._interval == http.RATE_BACKOFF_MAX          # and never beyond the cap

    sess._last_throttle -= http.RELAX_AFTER + 1
    sess._slowed_at -= http.RELAX_AFTER + 1
    sess._throttle()
    assert sess._interval == pytest.approx(8.0)             # one quiet spell relaxes one step (12 / 1.5)
    sess._interval = 1.2
    sess._slowed_at -= http.RELAX_AFTER + 1
    sess._throttle()
    assert sess._interval == 1.0                            # and never below the configured delay


def test_a_pace_that_is_already_fast_is_not_slowed_down(monkeypatch):
    sess = session_with([response(503)], monkeypatch, delay=0)
    sess._ema_gap = 0.01
    sess._note_throttle(http._throttle_signal(response(503)))
    assert sess._interval == 0                              # slowing a 100/s fake to 150/s-minus is pointless


def test_throttle_telemetry_goes_to_its_own_file_and_not_the_published_status(tmp_path):
    import json
    from crawler import run

    for n in (1, 2, 3, 4):
        run.write_source_status(tmp_path, "t", {"slug": "t", "started": f"run{n}", "throttled": 1,
                                                "throttle_recoveries": [60], "throttle_events": [{"kind": "429"}]})
    status = json.loads((tmp_path / "t" / "_status.json").read_text())
    assert "throttle_events" not in status and status["throttle_recoveries"] == [60]
    runs = json.loads((tmp_path / "t" / "_throttle.json").read_text())["runs"]
    assert [r["started"] for r in runs] == ["run2", "run3", "run4"]       # the last three runs are kept
    assert runs[-1]["events"] == [{"kind": "429"}] and runs[-1]["recoveries_s"] == [60]
