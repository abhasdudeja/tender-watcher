"""tools/local_crawl.ps1 crawls the sources that sources.yaml marks enabled: false; the two lists must not drift apart."""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEAD = {"mizoram"}        # the old NIC notice board: disabled because it is gone (the Mizoram API source replaced it), not local-only


def test_the_local_crawl_script_covers_every_disabled_source():
    script = (ROOT / "tools" / "local_crawl.ps1").read_text(encoding="utf-8")
    m = re.search(r"\[string\[\]\]\$Slugs = @\(([^)]*)\)", script)
    assert m
    in_script = set(re.findall(r'"([\w-]+)"', m.group(1)))
    cfg = yaml.safe_load((ROOT / "sources.yaml").read_text(encoding="utf-8"))["sources"]
    disabled = {s["slug"] for s in cfg if not s.get("enabled", True)} - DEAD
    assert in_script == disabled
