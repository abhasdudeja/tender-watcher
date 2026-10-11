"""Save pages from a portal to disk so a parser can be written against real HTML.

    python -m crawler.capture https://smctender.nprocure.com/ tests/fixtures/nprocure
    python -m crawler.capture "https://etenders.kerala.gov.in/nicgep/app?page=FrontEndTendersByOrganisation&service=page" tests/fixtures/kerala --follow 3

Fetches the URL, then (with --follow N) up to N same-host links from it, in one
cookie session. Writes NNN_<label>.html plus _manifest.txt mapping files to URLs.
Ignores robots.txt deliberately (you are capturing a handful of public pages by
hand); do not use it to bulk-crawl.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .http import PortalSession


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("out_dir")
    ap.add_argument("--follow", type=int, default=0, help="also fetch up to N same-host links")
    ap.add_argument("--match", default="", help="only follow links whose href/text contains this")
    args = ap.parse_args(argv)

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    sess = PortalSession(args.url, delay_seconds=1.5, dump_dir=str(out), ignore_robots=True)
    host = urlsplit(args.url).netloc

    html = sess.get(args.url, _allow_stale=True, label="start")
    manifest = [f"001_start.html\t{args.url}"]
    print(f"saved start page ({len(html)} bytes)")

    if args.follow:
        soup = BeautifulSoup(html, "lxml")
        n = 0
        for a in soup.find_all("a", href=True):
            href = urljoin(args.url, a["href"])
            text = a.get_text(" ", strip=True)
            if urlsplit(href).netloc != host or href.startswith("javascript:"):
                continue
            if args.match and args.match.lower() not in (href + text).lower():
                continue
            n += 1
            label = re.sub(r"[^A-Za-z0-9]+", "_", text)[:40] or "link"
            try:
                sess.get(href, _allow_stale=True, label=label)
                manifest.append(f"{n + 1:03d}_{label}.html\t{href}")
                print(f"saved {label!r} <- {href[:100]}")
            except Exception as e:  # noqa: BLE001
                print(f"FAILED {href[:100]}: {e}")
            if n >= args.follow:
                break
    (out / "_manifest.txt").write_text("\n".join(manifest) + "\n", encoding="utf-8")
    print(f"\nwrote {len(manifest)} pages + _manifest.txt to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
