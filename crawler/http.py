"""Polite HTTP session for GePNIC portals.

GePNIC (Tapestry) ties every "DirectLink" to a server-side session, so all
requests for one portal must share a cookie jar. Links also go stale after
a few minutes of inactivity; we detect the "Stale Session" page and let the
caller restart and re-navigate.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Optional
from urllib import robotparser
from urllib.parse import urlsplit

import requests

log = logging.getLogger(__name__)

# NIC portals sit behind a WAF that silently stalls (ReadTimeout) requests with
# non-browser User-Agents, so we send a browser UA. We stay identifiable via the
# standard `From` header, which a portal admin can see in their logs.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
CONTACT = "tenderwatch crawler, 1 req/s; see repo README"

STALE_MARKERS = ("Stale Session", "session has timed out", "Stale Link")


class StaleSession(Exception):
    """The portal discarded our session; links must be regenerated."""


class RobotsDisallowed(Exception):
    """robots.txt forbids this URL and the source has not set ignore_robots."""


class PortalSession:
    def __init__(
        self,
        base_url: str,
        delay_seconds: float = 1.0,
        timeout: float = 60.0,
        dump_dir: Optional[str] = None,
        max_retries: int = 3,
        ignore_robots: bool = False,
    ):
        self.base_url = base_url
        self.delay = delay_seconds
        self.timeout = timeout
        self.max_retries = max_retries
        self.dump_dir = Path(dump_dir) if dump_dir else None
        self._last_request = 0.0
        self._n = 0
        self.requests_made = 0
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-IN,en;q=0.8",
                "Accept-Encoding": "gzip, deflate",
                "Connection": "keep-alive",
                "From": CONTACT,
            }
        )
        if self.dump_dir:
            self.dump_dir.mkdir(parents=True, exist_ok=True)
        self._robots: Optional[robotparser.RobotFileParser] = None
        if not ignore_robots:
            self._robots = self._load_robots()

    def _load_robots(self) -> Optional[robotparser.RobotFileParser]:
        parts = urlsplit(self.base_url)
        url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        rp = robotparser.RobotFileParser()
        try:
            r = self.session.get(url, timeout=self.timeout)
            if r.status_code >= 400:
                return None  # no robots.txt -> everything allowed
            rp.parse(r.text.splitlines())
            return rp
        except requests.RequestException:
            return None

    def allowed(self, url: str) -> bool:
        if self._robots is None:
            return True
        return self._robots.can_fetch("*", url) and self._robots.can_fetch("tenderwatch", url)

    # ---- basics ---------------------------------------------------------
    def _throttle(self) -> None:
        wait = self.delay - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def restart(self) -> None:
        """Open a fresh server-side session."""
        self.session.cookies.clear()
        # Plain home URL: ?service=restart is slower on some portals and the
        # home page issues a fresh JSESSIONID just the same.
        self.get(self.base_url, _allow_stale=True, label="home")

    def get(self, url: str, _allow_stale: bool = False, label: str = "") -> str:
        if not self.allowed(url):
            raise RobotsDisallowed(url)
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                r = self.session.get(url, timeout=self.timeout)
                self.requests_made += 1
                r.raise_for_status()
                html = r.text
                self._dump(html, label)
                if not _allow_stale and is_stale(html):
                    raise StaleSession(url)
                return html
            except StaleSession:
                raise
            except requests.RequestException as e:  # network / 5xx
                last_exc = e
                backoff = min(60, 5 * attempt)
                log.warning("GET failed (%s), attempt %d/%d, sleeping %ss: %s",
                            e.__class__.__name__, attempt, self.max_retries, backoff, url[:120])
                time.sleep(backoff)
        assert last_exc is not None
        raise last_exc

    def _dump(self, html: str, label: str) -> None:
        if not self.dump_dir:
            return
        self._n += 1
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)[:60] or "page"
        (self.dump_dir / f"{self._n:05d}_{safe}.html").write_text(html, encoding="utf-8")


def is_stale(html: str) -> bool:
    head = html[:4000]
    return any(m in head for m in STALE_MARKERS)
