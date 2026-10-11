"""Polite HTTP session for one portal: one cookie jar, rate limited, robots.txt aware.

GePNIC links carry session-bound `sp=` tokens, so a PortalSession is created per
source and `restart()` throws the cookies away when the portal reports a stale session.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
import time
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
    retries in PortalSession. The crawl engine reacts by cooling down (default 15 min) and retrying.
    A DNS failure, a 404 or a 500 is NOT throttling and stays an ordinary error."""


class HostNotFound(RequestException):
    """The host name does not resolve (curl 6) even after the retries: the portal lists a site that no
    longer exists (nProcure still lists clients whose subdomain was removed). Not throttling, so no
    cool-down; the crawl engine skips that organisation and keeps what it has stored for it."""


class _ThrottleSignal(RequestException):
    """HTTP 429 / 503 seen by _request (internal: lets the retry loop classify the failure)."""


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
        wait = self._last + self.delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

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
                self.requests_made += 1
                log.debug("%s %s [%s] %.2fs", method, label or url[:80], r.status_code, time.monotonic() - t0)
                if r.status_code in (429, 503):
                    raise _ThrottleSignal(f"HTTP {r.status_code}")
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
                log.warning("%s %s failed (attempt %d/%d): %s", method, label or url[:80], attempt + 1, RETRIES, e)
                if attempt < RETRIES - 1:                   # no pointless wait after the last attempt
                    time.sleep(min(60, 5 * (attempt + 1)))
        else:
            if throttled:
                raise Throttled(str(last_err)) from last_err
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
