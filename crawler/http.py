"""Polite HTTP session for one portal: one cookie jar, rate limited, robots.txt aware.

GePNIC links carry session-bound `sp=` tokens, so a PortalSession is created per
source and `restart()` throws the cookies away when the portal reports a stale session.
"""
from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

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


class StaleSession(Exception):
    """The portal says our session (and every sp= link in it) has expired."""


class RobotsDisallowed(Exception):
    pass


class PortalSession:
    requests_made = 0

    def __init__(self, base_url: str, delay_seconds: float = 1.0, dump_dir: Optional[str] = None,
                 ignore_robots: bool = False):
        self.base_url = base_url
        self.delay = max(0.0, float(delay_seconds))
        self.dump_dir = Path(dump_dir) if dump_dir else None
        self.ignore_robots = ignore_robots
        self.requests_made = 0
        self._dump_n = 0
        self._last = 0.0
        self._robots: Optional[RobotFileParser] = None
        self._http = self._new_http()
        if self.dump_dir:
            self.dump_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _new_http() -> "requests.Session":
        s = requests.Session(impersonate=IMPERSONATE)
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
        if self._robots is None:
            parts = urlsplit(url)
            rp = RobotFileParser()
            try:
                r = self._http.get(f"{parts.scheme}://{parts.netloc}/robots.txt", timeout=TIMEOUT)
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())
                else:
                    rp.parse([])  # no robots.txt -> everything allowed
            except RequestException:
                rp.parse([])
            self._robots = rp
        return self._robots.can_fetch("*", url) and self._robots.can_fetch("tenderwatch", url)

    def _throttle(self) -> None:
        wait = self._last + self.delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    # ---- fetch ----
    def get(self, url: str, _allow_stale: bool = False, label: str = "") -> str:
        if not self._allowed(url):
            raise RobotsDisallowed(url)
        last_err: Exception = RuntimeError("no attempt made")
        for attempt in range(RETRIES):
            self._throttle()
            t0 = time.monotonic()
            try:
                r = self._http.get(url, timeout=LIST_TIMEOUT if label == "list" else TIMEOUT)
                self.requests_made += 1
                log.debug("GET %s [%s] %.2fs", label or url[:80], r.status_code, time.monotonic() - t0)
                if r.status_code >= 500:
                    raise RequestException(f"HTTP {r.status_code}")
                r.raise_for_status()
                if "charset" not in r.headers.get("Content-Type", "").lower():
                    r.encoding = "utf-8"
                html = r.text
                break
            except RequestException as e:
                last_err = e
                log.warning("GET %s failed (attempt %d/%d): %s", label or url[:80], attempt + 1, RETRIES, e)
                time.sleep(min(60, 5 * (attempt + 1)))
        else:
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
        (self.dump_dir / f"{self._dump_n:03d}_{safe}.html").write_text(html, encoding="utf-8")


def is_stale(html: str) -> bool:
    """The portal's "Stale Session" / "Stale Link" page (checked in the head of the page only)."""
    head = html[:4000].lower()
    return any(m in head for m in STALE_MARKERS)
