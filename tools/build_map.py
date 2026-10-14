"""Build docs/map/india-states.json (compact SVG paths of India's states and UTs) from a boundary GeoJSON.

The site's map panel draws these paths. The boundaries are geoBoundaries India ADM1 (CC BY 2.5 India; data from
DataMeet / the Election Commission of India). Download the "simplified" GeoJSON yourself, then run (the tool is
stdlib only; -I keeps the downloaded folder off the import path):

    python -I -X utf8 tools/build_map.py geoBoundaries-IND-ADM1_simplified.geojson docs/map/india-states.json

Units are keyed by their ISO 3166-2 code (IN-KL ...). Coordinates are projected (Lambert conformal conic centred on
India), fitted to a 560-unit-wide viewBox, simplified (Douglas-Peucker) and written as relative SVG path data.
Places too small to click (Chandigarh, Delhi, Dadra & Nagar Haveli and Daman & Diu, Goa, Lakshadweep, Puducherry)
carry "marker": true so the page can draw a clickable round marker for them.
"""
import json
import math
import sys

WIDTH, PAD = 560.0, 14.0
PHI1, PHI2, PHI0, LAM0 = (math.radians(v) for v in (12.5, 28.5, 23.0, 80.5))
TOL = 0.42             # Douglas-Peucker tolerance, in output units
MIN_RING_AREA = 0.9    # rings smaller than this are dropped (except a unit's biggest, and island groups below)
ISLAND_UNITS = {"IN-LD", "IN-AN", "IN-PY", "IN-DH"}
ISLAND_MIN_AREA = 0.05
MARKER_ISO = {"IN-CH", "IN-DL", "IN-DH", "IN-GA", "IN-LD", "IN-PY"}

_N = math.log(math.cos(PHI1) / math.cos(PHI2)) / math.log(math.tan(math.pi / 4 + PHI2 / 2) / math.tan(math.pi / 4 + PHI1 / 2))
_F = math.cos(PHI1) * math.tan(math.pi / 4 + PHI1 / 2) ** _N / _N


def _rho(phi):
    return _F / math.tan(math.pi / 4 + phi / 2) ** _N


_RHO0 = _rho(PHI0)


def project(lon, lat):
    """Lambert conformal conic: (lon, lat) in degrees -> (x, y)."""
    th = _N * (math.radians(lon) - LAM0)
    r = _rho(math.radians(lat))
    return r * math.sin(th), _RHO0 - r * math.cos(th)


def dp(points, tol):
    """Douglas-Peucker for an open polyline (iterative, so long coastlines cannot overflow the stack)."""
    if len(points) < 4:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        (ax, ay), (bx, by) = points[a], points[b]
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy) or 1e-12
        best, idx = 0.0, -1
        for i in range(a + 1, b):
            px, py = points[i]
            d = abs(dy * (px - ax) - dx * (py - ay)) / length
            if d > best:
                best, idx = d, i
        if best > tol and idx > 0:
            keep[idx] = True
            stack.append((a, idx))
            stack.append((idx, b))
    return [p for p, k in zip(points, keep) if k]


def dp_ring(ring, tol):
    """Douglas-Peucker for a closed ring. Its first and last points coincide, which would collapse a plain
    polyline simplification to nothing, so split at the point farthest from the start and simplify both halves."""
    pts = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else list(ring)
    if len(pts) < 4:
        return pts
    far = max(range(1, len(pts)), key=lambda i: (pts[i][0] - pts[0][0]) ** 2 + (pts[i][1] - pts[0][1]) ** 2)
    first = dp(pts[: far + 1], tol)
    second = dp(pts[far:] + [pts[0]], tol)
    return first[:-1] + second[:-1]


def area(pts):
    return abs(sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1]
                   for i in range(len(pts)))) / 2


def centroid(pts):
    a = cx = cy = 0.0
    for i in range(len(pts)):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % len(pts)]
        cr = x0 * y1 - x1 * y0
        a += cr
        cx += (x0 + x1) * cr
        cy += (y0 + y1) * cr
    if abs(a) < 1e-9:
        return pts[0]
    return cx / (3 * a), cy / (3 * a)


def _path(ring):
    """One ring as relative SVG path data with one decimal (rounded absolute points, so errors never accumulate)."""
    px, py = round(ring[0][0], 1), round(ring[0][1], 1)
    out = ["M%.1f %.1f" % (px, py)]
    rel = []
    for x, y in ring[1:]:
        x, y = round(x, 1), round(y, 1)
        rel.append("%.1f %.1f" % (x - px, y - py))
        px, py = x, y
    out.append("l" + " ".join(rel) + "z")
    return "".join(out)


def build(geojson):
    """GeoJSON FeatureCollection (shapeISO / shapeName properties) -> {"w", "h", "units": {iso: {...}}}."""
    raw = {}
    xs, ys = [], []
    for f in geojson["features"]:
        geom = f["geometry"]
        polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
        out = []
        for poly in polys:
            rings = [[project(x, y) for x, y, *_ in ring] for ring in poly]
            for ring in rings:
                xs.extend(p[0] for p in ring)
                ys.extend(p[1] for p in ring)
            out.append(rings)
        raw[f["properties"]["shapeISO"]] = (f["properties"]["shapeName"], out)
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    scale = (WIDTH - 2 * PAD) / (maxx - minx)
    height = (maxy - miny) * scale + 2 * PAD
    units = {}
    for iso, (name, polys) in sorted(raw.items()):
        shapes = []
        for rings in polys:
            simp = [dp_ring([((x - minx) * scale + PAD, (maxy - y) * scale + PAD) for x, y in r], TOL) for r in rings]
            shapes.append((area(simp[0]) if len(simp[0]) >= 3 else 0.0, simp))
        biggest_area, biggest = max(shapes, key=lambda s: s[0])
        floor = ISLAND_MIN_AREA if iso in ISLAND_UNITS else MIN_RING_AREA
        d = []
        for a0, simp in shapes:
            if a0 < floor and a0 != biggest_area:
                continue
            d.extend(_path(r) for r in simp if len(r) >= 3)
        cx, cy = centroid(biggest[0]) if len(biggest[0]) >= 3 else (0.0, 0.0)
        units[iso] = {"name": name, "d": "".join(d), "cx": round(cx, 1), "cy": round(cy, 1), "marker": iso in MARKER_ISO}
    return {"w": round(WIDTH, 1), "h": round(height, 1), "units": units}


def main(argv):
    if len(argv) != 3:
        print(__doc__)
        return 2
    with open(argv[1], encoding="utf-8") as fh:
        result = build(json.load(fh))
    text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    with open(argv[2], "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    print(f"{len(result['units'])} units, viewBox {result['w']} x {result['h']}, {len(text) / 1000:.0f} KB -> {argv[2]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
