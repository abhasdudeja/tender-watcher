"""Polite HTTP session for one portal: one cookie jar, rate limited, robots.txt aware.

GePNIC links carry session-bound `sp=` tokens, so a PortalSession is created per
source and `restart()` throws the cookies away when the portal reports a stale session.
"""
from __future__ import annotations

import email.utils
import hashlib
import logging
import math
import os
import re
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import certifi
from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException

# NIC's WAF stalls clients whose TLS fingerprint is not a browser's: plain
# requests/curl hang until ReadTimeout whatever the User-Agent. curl_cffi
# impersonates Chrome's TLS/HTTP2 handshake, which the portal accepts.
# We stay identifiable via the standard `From` header (override with TENDERWATCH_CONTACT).
IMPERSONATE = "chrome"
CONTACT = os.environ.get("TENDERWATCH_CONTACT", "tenderwatch crawler, 1 req/s; see repo README")
STALE_MARKERS = ("stale session", "session has timed out", "stale link")
TIMEOUT = 40
# The portal sends a whole organisation's tenders in ONE response (LSGD ~10 MB, ~1 min),
# so listing pages get a much longer total-time budget than detail pages.
LIST_TIMEOUT = 300
RETRIES = 3

log = logging.getLogger("tenderwatch.http")


def ca_bundle(extra: Optional[str]) -> Optional[str]:
    """Path of a CA bundle = certifi's roots + one extra PEM (a missing intermediate), or None.

    Verification stays ON: some portals serve an incomplete chain, and the fix is to supply the
    public intermediate (see certs/README.md), never to turn verification off."""
    if not extra:
        return None
    extra_pem = Path(extra).read_bytes()
    tag = hashlib.sha1(extra_pem).hexdigest()[:10]
    out = Path(tempfile.gettempdir()) / f"tenderwatch-ca-{tag}.pem"
    if not out.exists():
        out.write_bytes(Path(certifi.where()).read_bytes() + b"\n" + extra_pem)
    return str(out)


class StaleSession(Exception):
    """The portal says our session (and every sp= link in it) has expired."""


class Throttled(Exception):
    """The portal is pushing back (HTTP 429/503, timeouts, dropped connections) even after the short
    retries in PortalSession. The crawl engine reacts by cooling down (probing 1, 2, 4 ... up to 15 min)
    and retrying. `retry_after` is the portal's own Retry-After (seconds) when it sent one.
    A DNS failure, a 404 or a 500 is NOT throttling and stays an ordinary error."""

    def __init__(self, message: str = "", retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


class HostNotFound(RequestException):
    """The host name does not resolve (curl 6) even after the retries: the portal lists a site that no
    longer exists (nProcure still lists clients whose subdomain was removed). Not throttling, so no
    cool-down; the crawl engine skips that organisation and keeps what it has stored for it."""


class _ThrottleSignal(RequestException):
    """HTTP 429 / 503 seen by _request (internal: lets the retry loop classify the failure).
    Carries what the portal told us: status, rate-limit headers, Retry-After seconds, start of the body."""
    status = 429
    headers: Dict[str, str] = {}
    retry_after: Optional[float] = None
    body = ""


def _parse_retry_after(value) -> Optional[float]:
    """Retry-After is either delta-seconds or an HTTP date."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass
    try:
        when = email.utils.parsedate_to_datetime(str(value))
        return max(0.0, (when - datetime.now(when.tzinfo)).total_seconds())
    except (TypeError, ValueError):
        return None


def _rate_headers(headers) -> Dict[str, str]:
    """The response headers that say anything about rate limits (plus Server, for context)."""
    out = {}
    try:
        for k, v in headers.items():
            low = str(k).lower()
            if "ratelimit" in low or "rate-limit" in low or "retry" in low or low in ("server", "x-powered-by"):
                out[low] = str(v)[:120]
    except AttributeError:
        pass
    return out


def _throttle_signal(r) -> "_ThrottleSignal":
    e = _ThrottleSignal(f"HTTP {r.status_code}")
    e.status = r.status_code
    e.headers = _rate_headers(r.headers)
    e.retry_after = _parse_retry_after(r.headers.get("Retry-After"))
    try:
        e.body = r.content[:200].decode("utf-8", errors="replace").strip()
    except (AttributeError, TypeError):
        e.body = ""
    return e


def _looks_like_throttling(exc: Exception) -> bool:
    """Did the server answer 429/503, go silent after we connected, or hang up on us?

    curl 28 is a timeout; "Connection timed out" is the connect phase (a dead or unroutable host, not
    throttling) while "Operation timed out ... bytes received" means we connected and it stopped answering.
    curl 52 (empty reply) and 56 (receive failure) are the server dropping the connection."""
    if isinstance(exc, _ThrottleSignal):
        return True
    code = getattr(exc, "code", None)
    if code in (52, 56):
        return True
    if code == 28 or type(exc).__name__ in ("Timeout", "ReadTimeout"):
        return "Connection timed out" not in str(exc) and type(exc).__name__ != "ConnectTimeout"
    return False


class RobotsDisallowed(Exception):
    pass


# Adaptive pacing: a 429/503 means we are going too fast, so the minimum start-to-start gap becomes
# 1.5x what we have actually been doing (never below `delay`), at most once a minute, up to RATE_BACKOFF_MAX;
# after RELAX_AFTER seconds without one it relaxes back towards `delay`, step by step.
SLOWDOWN = 1.5
SLOWDOWN_EVERY = 60.0
RELAX_AFTER = 600.0
RATE_BACKOFF_MAX = 12.0
MIN_USEFUL_GAP = 0.5      # below this, "slowing down" is pointless (fast fakes in tests, very fast portals)


class PortalSession:
    requests_made = 0

    def __init__(self, base_url: str, delay_seconds: float = 1.0, dump_dir: Optional[str] = None,
                 ignore_robots: bool = False, extra_ca_certs: Optional[str] = None):
        self.base_url = base_url
        self.delay = max(0.0, float(delay_seconds))
        self.dump_dir = Path(dump_dir) if dump_dir else None
        self.ignore_robots = ignore_robots
        self.requests_made = 0
        self._dump_n = 0
        self._last = 0.0
        self._interval = self.delay        # current minimum start-to-start gap (>= delay, grows after a 429)
        self._ema_gap = None               # recent average start-to-start gap, i.e. our real request pace
        self._slowed_at = -math.inf
        self._last_throttle = -math.inf
        self._requests_at_throttle = 0
        self.on_throttle = None            # optional callback(event dict) for every 429/503 (engine telemetry)
        self._gate = threading.Lock()      # serialises request *starts*, so workers share one rate cap
        self._count = threading.Lock()
        self._robots: Dict[str, RobotFileParser] = {}     # per host
        self.verify = ca_bundle(extra_ca_certs) or True
        self._http = self._new_http()
        if self.dump_dir:
            self.dump_dir.mkdir(parents=True, exist_ok=True)

    def _new_http(self) -> "requests.Session":
        s = requests.Session(impersonate=IMPERSONATE, verify=self.verify)
        s.headers["Accept-Language"] = "en-IN,en;q=0.9"
        s.headers["From"] = CONTACT
        return s

    def restart(self) -> None:
        """Drop cookies and open the portal home to get a fresh server session."""
        self._http.close()
        self._http = self._new_http()
        self.get(self.base_url, _allow_stale=True, label="restart")

    # ---- robots.txt ----
    def _allowed(self, url: str) -> bool:
        if self.ignore_robots:
            return True
        parts = urlsplit(url)
        rp = self._robots.get(parts.netloc)
        if rp is None:
            rp = RobotFileParser()
            try:
                r = self._http.get(f"{parts.scheme}://{parts.netloc}/robots.txt", timeout=TIMEOUT)
                if r.status_code == 200:
                    rp.parse(r.content.decode("utf-8", errors="replace").splitlines())
                else:
                    rp.parse([])  # no robots.txt -> everything allowed
            except RequestException:
                rp.parse([])
            self._robots[parts.netloc] = rp
        return rp.can_fetch("*", url) and rp.can_fetch("tenderwatch", url)

    def _throttle(self) -> None:
        """Start-to-start spacing of at least `delay` seconds across ALL threads: with several workers the
        requests overlap in flight (that is the speed-up) but never start faster than 1/delay per second."""
        with self._gate:
            now = time.monotonic()
            if self._interval > self.delay and now - max(self._last_throttle, self._slowed_at) >= RELAX_AFTER:
                self._interval = max(self.delay, self._interval / SLOWDOWN)
                self._slowed_at = now
                log.info("no rate limiting for %d min: request spacing relaxed to %.1fs", RELAX_AFTER // 60, self._interval)
            wait = self._last + max(self.delay, self._interval) - now
            if wait > 0:
                time.sleep(wait)
            start = time.monotonic()
            if self._last > 0 and start - self._last < 30:          # (longer gaps are retry back-offs, not our pace)
                gap = start - self._last
                self._ema_gap = gap if self._ema_gap is None else 0.8 * self._ema_gap + 0.2 * gap
            self._last = start

    def _note_throttle(self, sig: "_ThrottleSignal") -> None:
        """A 429/503 came back: record what the portal said, how long/how many requests since the previous
        one, and slow down (see SLOWDOWN)."""
        with self._gate:
            now = time.monotonic()
            first = math.isinf(self._last_throttle)
            event = {"kind": str(sig.status), "since_prev_s": None if first else round(now - self._last_throttle, 1),
                     "requests_since_prev": self.requests_made - self._requests_at_throttle,
                     "pace_gap_s": None if self._ema_gap is None else round(self._ema_gap, 2),
                     "headers": sig.headers, "retry_after": sig.retry_after, "body": sig.body}
            self._last_throttle, self._requests_at_throttle = now, self.requests_made
            target = min(RATE_BACKOFF_MAX, max(self.delay, self._ema_gap or 0.0) * SLOWDOWN)
            if target >= MIN_USEFUL_GAP and target > self._interval and now - self._slowed_at >= SLOWDOWN_EVERY:
                self._interval, self._slowed_at = target, now
            event["spacing_s"] = round(max(self.delay, self._interval), 1)
        log.warning("rate limit (HTTP %s): %s requests, %s s since the previous one; headers %s; body %r; "
                    "spacing requests >= %.1fs", sig.status, event["requests_since_prev"], event["since_prev_s"],
                    sig.headers or "none", sig.body[:80], event["spacing_s"])
        if self.on_throttle:
            try:
                self.on_throttle(event)
            except Exception:  # noqa: BLE001  telemetry must never break a crawl
                log.debug("on_throttle failed", exc_info=True)

    # ---- fetch ----
    def get(self, url: str, _allow_stale: bool = False, label: str = "") -> str:
        return self._request("GET", url, _allow_stale=_allow_stale, label=label)

    def post(self, url: str, data=None, headers: Optional[Dict[str, str]] = None, label: str = "") -> str:
        """POST a form (dict) or a raw body (str, e.g. JSON); returns the decoded response text."""
        return self._request("POST", url, data=data, headers=headers, label=label)

    def _request(self, method: str, url: str, data=None, headers: Optional[Dict[str, str]] = None,
                 _allow_stale: bool = False, label: str = "") -> str:
        if not self._allowed(url):
            raise RobotsDisallowed(url)
        last_err: Exception = RuntimeError("no attempt made")
        throttled = unresolved = False
        for attempt in range(RETRIES):
            self._throttle()
            t0 = time.monotonic()
            try:
                kw = {"timeout": LIST_TIMEOUT if label == "list" else TIMEOUT}
                if method == "POST":
                    kw.update(data=data, headers=headers)
                r = self._http.request(method, url, **kw)
                with self._count:
                    self.requests_made += 1
                log.debug("%s %s [%s] %.2fs", method, label or url[:80], r.status_code, time.monotonic() - t0)
                if r.status_code in (429, 503):
                    raise _throttle_signal(r)
                if r.status_code >= 500:
                    raise RequestException(f"HTTP {r.status_code}")
                r.raise_for_status()
                # decode ourselves: some portals send a stray non-UTF-8 byte despite charset=UTF-8,
                # which must not make the whole page (or JSON listing) unreadable
                html = r.content.decode(_charset(r.headers.get("Content-Type", "")), errors="replace")
                break
            except RequestException as e:
                last_err = e
                throttled = _looks_like_throttling(e)
                unresolved = getattr(e, "code", None) == 6
                if isinstance(e, _ThrottleSignal):
                    self._note_throttle(e)
                log.warning("%s %s failed (attempt %d/%d): %s", method, label or url[:80], attempt + 1, RETRIES, e)
                if attempt < RETRIES - 1:                   # no pointless wait after the last attempt
                    time.sleep(min(60, 5 * (attempt + 1)))
        else:
            if throttled:
                raise Throttled(str(last_err), getattr(last_err, "retry_after", None)) from last_err
            if unresolved:
                raise HostNotFound(str(last_err)) from last_err
            raise last_err
        self._dump(html, label)
        if not _allow_stale and is_stale(html):
            raise StaleSession(url)
        return html

    def _dump(self, html: str, label: str) -> None:
        if not self.dump_dir:
            return
        self._dump_n += 1
        safe = re.sub(r"[^A-Za-z0-9]+", "_", label or "page")[:40]
        ext = "json" if html.lstrip()[:1] in "{[" else "html"
        (self.dump_dir / f"{self._dump_n:03d}_{safe}.{ext}").write_text(html, encoding="utf-8")


def _charset(content_type: str) -> str:
    """The charset named in a Content-Type header, defaulting to UTF-8 (and to UTF-8 if unknown)."""
    m = re.search(r"charset=([\w\-]+)", content_type, re.I)
    name = m.group(1) if m else "utf-8"
    try:
        "".encode(name)
    except LookupError:
        return "utf-8"
    return name


def is_stale(html: str) -> bool:
    """The portal's "Stale Session" / "Stale Link" page (checked in the head of the page only)."""
    head = html[:4000].lower()
    return any(m in head for m in STALE_MARKERS)
