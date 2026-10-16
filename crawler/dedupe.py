"""Duplicates across sources: the same tender listed by two portals.

Some sources are aggregators of other portals (GeM's CPPP pages list the tenders the central and the state
portals publish). Such a source is marked `prefer_others: true` in sources.yaml: when one of its tenders is also
held by another source, the other source's record is the one to show and the aggregator's copy is a duplicate. The
stored data is never changed; the site index (crawler/index.py) simply leaves duplicates out, and
`python -m crawler.run --dedupe-report` prints what overlaps.

A tender of a `prefer_others` source is a duplicate of another source's *active* tender when
  * its id equals that tender's id. A GePNIC id such as 2026_PRD_5080689_1 is unique across portals, so for those
    the id is the only rule: an id that is not held elsewhere is a different tender, however alike the titles are.
    Any other id (a railway tender number, say) must also agree on the closing day; and for those only
  * the references are equal (alphanumerics only, at least 6 characters, with letters in them) and the closing day is
    the same, or
  * the titles are equal (first 90 alphanumeric characters, at least 30 of them) and the closing day is the same.
Matching on a reference or a title alone would flag different tenders that reuse a common reference ("NIT-1"), and
the aggregator's reference is only an approximation (a reference containing "/" is cut short), so GePNIC rows never
use it.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Dict, List, Optional, Tuple

_GEPNIC_ID = re.compile(r"^\d{4}_[A-Za-z0-9]+_\d+_\d+$")
MIN_REF = 6          # alphanumeric characters of a reference worth matching on
MIN_TITLE = 30
TITLE_KEY = 90


def norm(text) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def day(stamp) -> str:
    return (stamp or "")[:10]


def _distinctive(ref: str) -> bool:
    """A reference worth matching on: long enough and with letters (not just a year like '2026 27')."""
    return len(ref) >= MIN_REF and len(re.findall(r"[a-z]", ref)) >= 3


Lookup = Tuple[Dict[str, Tuple[str, str]], Dict[Tuple[str, str], str], Dict[Tuple[str, str], str]]


def build_lookup(stores: Dict[str, Dict[str, Dict]], sources: List[Dict]) -> Lookup:
    """Keys of every active tender of the sources that do not defer to others."""
    by_id: Dict[str, Tuple[str, str]] = {}
    by_ref: Dict[Tuple[str, str], str] = {}
    by_title: Dict[Tuple[str, str], str] = {}
    for src in sources:
        if src.get("prefer_others"):
            continue
        slug = src["slug"]
        for rec in (stores.get(slug) or {}).values():
            if not rec.get("active"):
                continue
            tag, closes = f"{slug}:{rec['id']}", day(rec.get("close"))
            by_id.setdefault(str(rec["id"]), (tag, closes))
            ref, title = norm(rec.get("ref")), norm(rec.get("title"))
            if _distinctive(ref) and closes:
                by_ref.setdefault((ref, closes), tag)
            if len(title) >= MIN_TITLE and closes:
                by_title.setdefault((title[:TITLE_KEY], closes), tag)
    return by_id, by_ref, by_title


def match(rec: Dict, lookup: Lookup) -> Optional[Tuple[str, str]]:
    """(rule, "<slug>:<id>" of the other source's tender) if `rec` is also held elsewhere, else None."""
    by_id, by_ref, by_title = lookup
    closes = day(rec.get("close"))
    rid = str(rec["id"])
    gepnic = bool(_GEPNIC_ID.match(rid))
    hit = by_id.get(rid)
    if hit and (gepnic or not closes or not hit[1] or hit[1] == closes):
        return "id", hit[0]
    if gepnic:
        return None          # a GePNIC id is unique across portals: one that is not held elsewhere is a different tender
    ref, title = norm(rec.get("ref")), norm(rec.get("title"))
    if _distinctive(ref) and closes and (ref, closes) in by_ref:
        return "ref", by_ref[(ref, closes)]
    if len(title) >= MIN_TITLE and closes and (title[:TITLE_KEY], closes) in by_title:
        return "title", by_title[(title[:TITLE_KEY], closes)]
    return None


def find_duplicates(stores: Dict[str, Dict[str, Dict]], sources: List[Dict]) -> Dict[str, Dict[str, Tuple[str, str]]]:
    """For every `prefer_others` source: {tender id: (rule, other "<slug>:<id>")} of its duplicate tenders."""
    deferring = [s for s in sources if s.get("prefer_others")]
    if not deferring:
        return {}
    lookup = build_lookup(stores, sources)
    out: Dict[str, Dict[str, Tuple[str, str]]] = {}
    for src in deferring:
        slug = src["slug"]
        found = {}
        for tid, rec in (stores.get(slug) or {}).items():
            if rec.get("active"):
                m = match(rec, lookup)
                if m:
                    found[tid] = m
        out[slug] = found
    return out


def report(stores: Dict[str, Dict[str, Dict]], sources: List[Dict], examples: int = 3) -> List[str]:
    """Human-readable overlap of every `prefer_others` source with the rest."""
    dups = find_duplicates(stores, sources)
    if not dups:
        return ["no source is marked prefer_others: true, nothing to compare"]
    lines = []
    for slug, found in dups.items():
        active = sum(1 for r in (stores.get(slug) or {}).values() if r.get("active"))
        rules = Counter(rule for rule, _ in found.values())
        others = Counter(other.split(":", 1)[0] for _, other in found.values())
        lines.append(f"{slug}: {active} active, {len(found)} duplicates ({', '.join(f'{n} by {r}' for r, n in rules.most_common()) or 'none'}), "
                     f"{active - len(found)} only here")
        if others:
            lines.append("   duplicates of: " + ", ".join(f"{s} {n}" for s, n in others.most_common(8)))
        recs = stores.get(slug) or {}
        for tid, (rule, other) in list(found.items())[:examples]:
            lines.append(f"   e.g. {tid} ({rule}) = {other}: {str(recs[tid].get('title'))[:70]}")
    return lines
