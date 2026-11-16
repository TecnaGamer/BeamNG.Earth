#!/usr/bin/env python3
"""The 3D map's world labels (server/data/world_labels.json.gz) from Natural Earth 1:10m (public domain,
naturalearthdata.com; the GeoJSON of github.com/nvkelso/natural-earth-vector v5.1.2), for the views too far out for
the per-cell Overture place names: continents, oceans and seas, countries, states and provinces, the populated places,
mountain ranges and deserts. Natural Earth gives each one the web-map zoom its label starts at (min_zoom / MIN_LABEL)
and stops at (max_label): the map shows more as it zooms in, as map apps do (the user, 2026-11-07: "As you zoom in
more and more info appears").

  python3 tools/build_world_labels.py NE_GEOJSON_DIR [OUT]

Rows: [kind, name, lat, lon, min zoom, max zoom (99: none), rank (lower first), extra]
  kind  continent | water | country | state | city | land
  extra water: its class (ocean, sea, bay, ...); state: its postal code (US, CA, AU, MX, BR, IN: shown far out);
        city: "capital" | "admin" | ""; land: its class (Range/mtn, Desert, ...)
Polygons are labelled at a point well inside their largest part (the grid point farthest from its edges)."""
import gzip
import json
import math
import os
import sys

import numpy as np

ABBREV_COUNTRIES = {"USA", "CAN", "AUS", "MEX", "BRA", "IND"}   # their states' postal codes far out (map apps' style)
LAND_CLASSES = {"Range/mtn": 0, "Desert": 1, "Plateau": 2, "Plain": 3, "Basin": 4, "Peninsula": 5, "Pen/cape": 5,
                "Delta": 6, "Tundra": 7}


def load(d, name):
    with open(os.path.join(d, name + ".geojson"), encoding="utf-8") as fh:
        return json.load(fh)["features"]


def rings(geom):
    """[(outer ring (n, 2) lon/lat, [holes])] of a Polygon / MultiPolygon."""
    t, c = geom["type"], geom["coordinates"]
    polys = [c] if t == "Polygon" else c if t == "MultiPolygon" else []
    return [(np.asarray(p[0], np.float64), [np.asarray(h, np.float64) for h in p[1:]]) for p in polys if p]


def area(r):
    x, y = r[:, 0], r[:, 1]
    return 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


def inside(px, py, r):
    """Even-odd point in polygon, vectorised over points."""
    x, y = r[:, 0], r[:, 1]
    x2, y2 = np.roll(x, -1), np.roll(y, -1)
    res = np.zeros(px.shape, bool)
    for i in range(len(x)):
        c = ((y[i] > py) != (y2[i] > py)) & (px < (x2[i] - x[i]) * (py - y[i]) / ((y2[i] - y[i]) or 1e-12) + x[i])
        res ^= c
    return res


def edge_dist(px, py, rs):
    """Distance (deg, x scaled by cos lat) from points to the nearest edge of rings rs."""
    best = np.full(px.shape, np.inf)
    k = math.cos(math.radians(float(np.mean(py)))) if len(py) else 1.0
    for r in rs:
        a, b = r[:-1], r[1:]
        ax, ay, bx, by = a[:, 0] * k, a[:, 1], b[:, 0] * k, b[:, 1]
        dx, dy = bx - ax, by - ay
        l2 = np.maximum(dx * dx + dy * dy, 1e-18)
        for j in range(0, len(ax), 2000):
            sl = slice(j, j + 2000)
            t = np.clip(((px[:, None] * k - ax[None, sl]) * dx[None, sl] + (py[:, None] - ay[None, sl]) * dy[None, sl])
                        / l2[None, sl], 0, 1)
            qx, qy = ax[None, sl] + t * dx[None, sl], ay[None, sl] + t * dy[None, sl]
            d = np.min(np.hypot(px[:, None] * k - qx, py[:, None] - qy), axis=1)
            best = np.minimum(best, d)
    return best


def label_point(geom):
    """A point well inside the polygon's largest part (lat, lon), or None."""
    rs = rings(geom)
    if not rs:
        return None
    outer, holes = max(rs, key=lambda p: area(p[0]))
    if outer[:, 0].max() - outer[:, 0].min() > 300:     # (across the antimeridian: its centroid as is)
        return float(np.mean(outer[:, 1])), float(np.mean(outer[:, 0]))
    if len(outer) > 3000:                               # (fine coasts: every n-th point is plenty for a label)
        outer = outer[:: len(outer) // 3000 + 1]
    x0, y0 = outer.min(axis=0)
    x1, y1 = outer.max(axis=0)
    n = 40
    gx, gy = np.meshgrid(np.linspace(x0, x1, n), np.linspace(y0, y1, n))
    px, py = gx.ravel(), gy.ravel()
    ok = inside(px, py, outer)
    for h in holes:
        ok &= ~inside(px, py, h)
    if not ok.any():
        return float(np.mean(outer[:, 1])), float(np.mean(outer[:, 0]))
    px, py = px[ok], py[ok]
    d = edge_dist(px, py, [outer] + holes)
    i = int(np.argmax(d))
    return float(py[i]), float(px[i])


def num(v, default):
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def build(d):
    rows = []
    for f in load(d, "ne_10m_geography_regions_polys"):
        p = f["properties"]
        cla, name = p.get("FEATURECLA"), p.get("NAME_EN") or p.get("NAME")
        if not name or not f.get("geometry"):
            continue
        if cla == "Continent":
            kind, extra, rank = "continent", "", 0
        elif cla in LAND_CLASSES:
            kind, extra, rank = "land", cla, 60 + LAND_CLASSES[cla]
        else:
            continue
        pt = label_point(f["geometry"])
        if pt:
            rows.append([kind, name, pt[0], pt[1], num(p.get("MIN_LABEL"), 2 if kind == "continent" else 5),
                         num(p.get("MAX_LABEL"), 99), rank + num(p.get("SCALERANK"), 5), extra])
    for f in load(d, "ne_10m_geography_marine_polys"):
        p = f["properties"]
        name = p.get("name") or p.get("name_en")       # ("North Atlantic Ocean", as map apps)
        if not name or not f.get("geometry"):
            continue
        pt = label_point(f["geometry"])
        if pt:
            rows.append(["water", name, pt[0], pt[1], num(p.get("min_label"), 4), num(p.get("max_label"), 99),
                         10 + num(p.get("scalerank"), 5), p.get("featurecla") or ""])
    for f in load(d, "ne_10m_admin_0_countries"):
        p = f["properties"]
        # (the shortest of its plain names: "United States", not "United States of America"; no "Dem. Rep.")
        names = [n for n in (p.get("NAME_EN"), p.get("NAME_LONG"), p.get("NAME")) if n]
        name = min([n for n in names if "." not in n] or names, key=len) if names else None
        if not name or p.get("LABEL_Y") is None:
            continue
        rows.append(["country", name, num(p["LABEL_Y"], 0), num(p["LABEL_X"], 0), num(p.get("MIN_LABEL"), 3),
                     num(p.get("MAX_LABEL"), 99), 20 + num(p.get("LABELRANK"), 5), ""])
    for f in load(d, "ne_10m_admin_1_states_provinces"):
        p = f["properties"]
        name = p.get("name_en") or p.get("name")
        if not name or p.get("latitude") is None:
            continue
        postal = (p.get("postal") or "") if p.get("adm0_a3") in ABBREV_COUNTRIES else ""
        rows.append(["state", name, num(p["latitude"], 0), num(p["longitude"], 0), num(p.get("min_label"), 6),
                     num(p.get("max_label"), 99), 30 + num(p.get("labelrank"), 5), postal])
    for f in load(d, "ne_10m_populated_places_simple"):
        p = f["properties"]
        name = p.get("name") or p.get("nameascii")
        if not name:
            continue
        cla = p.get("featurecla") or ""
        extra = "capital" if cla.startswith("Admin-0 capital") else "admin" if cla.startswith("Admin-1 capital") else ""
        pop = num(p.get("pop_max"), 0)
        rows.append(["city", name, num(p["latitude"], 0), num(p["longitude"], 0), num(p.get("min_zoom"), 8), 99,
                     40 - math.log10(max(pop, 1)), extra])
    for r in rows:
        r[2], r[3] = round(r[2], 4), round(r[3], 4)
        r[4], r[5], r[6] = round(r[4], 1), round(r[5], 1), round(r[6], 2)
    rows.sort(key=lambda r: (r[0], r[6], r[1]))
    return rows


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(__file__), "..", "server", "data",
                                                             "world_labels.json.gz")
    rows = build(sys.argv[1])
    data = {"v": 1, "source": "Natural Earth 1:10m v5.1.2 (public domain)", "rows": rows}
    with gzip.open(out, "wt", encoding="utf-8", compresslevel=9) as fh:
        json.dump(data, fh, separators=(",", ":"), ensure_ascii=False)
    kinds = {}
    for r in rows:
        kinds[r[0]] = kinds.get(r[0], 0) + 1
    print(f"{out}: {len(rows)} labels {kinds}, {os.path.getsize(out) / 1e3:.0f} KB")


if __name__ == "__main__":
    main()
