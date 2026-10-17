"""docs/analytics.js: the GoatCounter site code is well formed, and app.js only reports fixed event names."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ANALYTICS = (ROOT / "docs" / "analytics.js").read_text(encoding="utf-8")
APP = (ROOT / "docs" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")


def test_site_code_is_empty_or_a_valid_goatcounter_code():
    code = re.search(r'var SITE = "([^"]*)"', ANALYTICS).group(1)
    assert code == "" or re.fullmatch(r"[a-z0-9][a-z0-9-]{1,30}", code)


def test_analytics_loads_before_app_and_the_privacy_note_exists():
    assert HTML.index("analytics.js") < HTML.index("app.js")
    assert 'id="privacyNote"' in HTML


def test_events_never_carry_typed_text():
    calls = re.findall(r"track\(([^)]*)\)", APP)
    assert calls                                                    # the hooks are still there
    allowed = ('"search"', '"view/" + v', '"map/state/" + k', '"map/metric/" + metric', '"map/style/" + style',
               '"tab/" + t', '"tab/" + to', '"source/" + els.source.value', '"share/tender"', '"share/open"')
    for c in calls:
        if c == "name":                                             # the const track = (name) => ... definition
            continue
        assert c in allowed, c
    # the search box value is read only to decide whether a search happened, never passed to track()
    assert not re.search(r"track\([^)]*(els\.q|\.value\.trim)", APP)


def test_a_shared_tender_link_carries_no_personal_data():
    # ?t=<source>:<id>&o=<organisation>&ref=share : the tender and a fixed marker, nothing about the sender
    m = re.search(r"function tenderLink\(t\) \{(.*?)\n  \}", APP, re.S)
    assert m
    keys = set(re.findall(r'(?:set\("|new URLSearchParams\(\{ )(\w+)', m.group(1)))
    assert keys == {"o", "ref"} and '"?t=" + encodeURIComponent(t.s) + ":" + encodeURIComponent(t.id)' in m.group(1)
    assert 'q.set("ref", "share")' in m.group(1)
