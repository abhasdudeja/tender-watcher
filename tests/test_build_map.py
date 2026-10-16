"""tools/build_map.py (boundary GeoJSON -> compact SVG paths) and the committed docs/map/india-states.json."""
import importlib.util
import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("build_map", ROOT / "tools" / "build_map.py")
bm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bm)


def _square(x0, y0, size, n=40):
    """A closed ring (first point repeated at the end) going round a square with n points per side."""
    pts = []
    for i in range(n):
        pts.append((x0 + size * i / n, y0))
    for i in range(n):
        pts.append((x0 + size, y0 + size * i / n))
    for i in range(n):
        pts.append((x0 + size - size * i / n, y0 + size))
    for i in range(n):
        pts.append((x0, y0 + size - size * i / n))
    pts.append(pts[0])
    return pts


def test_a_closed_ring_survives_simplification_with_its_corners():
    ring = _square(0, 0, 10)
    out = bm.dp_ring(ring, 0.1)
    assert 4 <= len(out) <= 6                                      # collinear points dropped, the four corners kept
    assert {(round(x), round(y)) for x, y in out} >= {(0, 0), (10, 0), (10, 10), (0, 10)}
    assert bm.area(out) == pytest.approx(100, rel=0.02)


def test_a_plain_polyline_keeps_its_end_points_and_a_bend():
    out = bm.dp([(0, 0), (1, 0.001), (2, 0), (3, 5), (4, 0)], 0.1)
    assert out[0] == (0, 0) and out[-1] == (4, 0) and (3, 5) in out and (1, 0.001) not in out


def test_projection_is_monotonic_and_north_is_up():
    x_w, _ = bm.project(70, 23)
    x_e, _ = bm.project(90, 23)
    _, y_s = bm.project(80, 10)
    _, y_n = bm.project(80, 30)
    assert x_w < x_e and y_n > y_s                                  # screen y is flipped later


def _feature(iso, name, coords, kind="Polygon"):
    """coords: a polygon (list of rings) for "Polygon", a list of polygons for "MultiPolygon"."""
    return {"type": "Feature", "properties": {"shapeISO": iso, "shapeName": name}, "geometry": {"type": kind, "coordinates": coords}}


def _lonlat_square(lon, lat, size, n=40):
    """One polygon (a list with its single outer ring) of lon/lat points."""
    return [[(x, y) for x, y in _square(lon, lat, size, n)]]


def test_build_produces_paths_markers_and_a_fitted_view_box():
    gj = {"type": "FeatureCollection", "features": [
        _feature("IN-KL", "Kerala", _lonlat_square(75, 9, 3)),
        _feature("IN-DL", "Delhi", _lonlat_square(77, 28.4, 0.4)),
        _feature("IN-LD", "Lakshadweep", [_lonlat_square(72.2, 10, 0.3), _lonlat_square(72.9, 11, 0.01)], kind="MultiPolygon"),
    ]}
    res = bm.build(gj)
    assert res["w"] == 560.0 and res["h"] > 100
    assert set(res["units"]) == {"IN-KL", "IN-DL", "IN-LD"}
    assert res["units"]["IN-DL"]["marker"] is True and res["units"]["IN-KL"]["marker"] is False
    for u in res["units"].values():
        assert re.fullmatch(r"(M-?\d+\.\d -?\d+\.\dl[-\d. ]+z)+", u["d"])
        assert 0 <= u["cx"] <= res["w"] and 0 <= u["cy"] <= res["h"]
    assert res["units"]["IN-LD"]["d"].count("M") == 2              # both islands are kept for an island unit


def test_relative_path_data_adds_back_up_to_the_rounded_absolute_points():
    res = bm.build({"type": "FeatureCollection", "features": [_feature("IN-KL", "Kerala", _lonlat_square(75, 9, 3, n=10))]})
    d = res["units"]["IN-KL"]["d"]
    m = re.match(r"M(-?\d+\.\d) (-?\d+\.\d)l(.*)z$", d)
    x, y = float(m.group(1)), float(m.group(2))
    nums = [float(v) for v in m.group(3).split()]
    pts = [(x, y)]
    for i in range(0, len(nums), 2):
        x, y = round(x + nums[i], 1), round(y + nums[i + 1], 1)
        pts.append((x, y))
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    assert max(xs) - min(xs) > 10 and max(ys) - min(ys) > 10        # a real shape came back, not a collapsed one
    assert all(0 <= px <= 560 for px in xs)


def test_the_committed_map_has_every_state_and_is_small():
    path = ROOT / "docs" / "map" / "india-states.json"
    res = json.loads(path.read_text(encoding="utf-8"))
    assert len(res["units"]) == 36 and path.stat().st_size < 120_000
    assert {"IN-LA", "IN-TG", "IN-DH", "IN-DL", "IN-CH"} <= set(res["units"])
    assert all(u["d"].startswith("M") and u["d"].endswith("z") for u in res["units"].values())
    assert {iso for iso, u in res["units"].items() if u["marker"]} == set(bm.MARKER_ISO)


def test_every_source_state_in_sources_yaml_exists_on_the_map():
    res = json.loads((ROOT / "docs" / "map" / "india-states.json").read_text(encoding="utf-8"))
    cfg = yaml.safe_load((ROOT / "sources.yaml").read_text(encoding="utf-8"))["sources"]
    states = {s["slug"]: s.get("state") for s in cfg}
    assert all(states.values()), [k for k, v in states.items() if not v]        # every source says what it covers
    assert all(v in ("central", "multi") or v in res["units"] for v in states.values()), \
        [k for k, v in states.items() if v not in ("central", "multi") and v not in res["units"]]
