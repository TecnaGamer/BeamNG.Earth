#!/usr/bin/env python3
"""build server/data/au_states.json.gz, the Australian state and territory outlines climate.au_state() reads.

    python tools/build_au_states.py [--src ne_10m_admin_1_states_provinces.geojson] [--tol 0.002] [--min-area 0.00003]

Source: Natural Earth v5.1.2 admin_1 (1:10m states and provinces), public domain (naturalearthdata.com; GeoJSON from
github.com/nvkelso/natural-earth-vector). Kept: the features of Australia, each ring Douglas-Peucker simplified to --tol
degrees (0.002 = about 200 m; --tol 0.02 is about 2 km) and snapped to 0.001 degrees, the polygons smaller than --min-area square degrees dropped
(islands are still reached by au_state()'s offshore tolerance). The file is {"v": 1, "states": {code: [polygon, ...]}},
a polygon [ring, ...] (the first the outer ring, the others holes), a ring the [lon, lat] of its first point in 1/1000
degrees followed by the deltas to each next one (flat, closed implicitly).
"""
import argparse
import gzip
import json
import os
import urllib.request

import numpy as np

URL = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/v5.1.2/geojson/ne_10m_admin_1_states_provinces.geojson"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server", "data", "au_states.json.gz")
Q = 1000    # snap to 1/1000 degree (about 110 m)


def douglas_peucker(p, tol):
    """The indices of the points of the open polyline p (n x 2, scaled to equal units) kept at tolerance tol."""
    n = len(p)
    keep = np.zeros(n, bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        d = p[b] - p[a]
        seg = p[a + 1:b] - p[a]
        L = float(np.hypot(*d))
        dist = np.hypot(seg[:, 0], seg[:, 1]) if L == 0 else np.abs(d[0] * seg[:, 1] - d[1] * seg[:, 0]) / L
        k = int(np.argmax(dist))
        if dist[k] > tol:
            keep[a + 1 + k] = True
            stack += [(a, a + 1 + k), (a + 1 + k, b)]
    return np.nonzero(keep)[0]


def simplify_ring(ring, tol):
    """The closed ring (n x 2 lon / lat) simplified: split at its farthest point from the start, each half reduced."""
    p = np.asarray(ring, np.float64)[:, :2]
    if np.allclose(p[0], p[-1]):
        p = p[:-1]
    s = p * np.array([np.cos(np.radians(25.0)), 1.0])       # (Australia's latitudes: close enough)
    k = int(np.argmax(np.hypot(*(s - s[0]).T)))
    a = douglas_peucker(s[:k + 1], tol)
    b = douglas_peucker(np.vstack([s[k:], s[:1]]), tol)
    idx = np.concatenate([a[:-1], k + b[:-1]])
    q = np.round(p[idx] * Q).astype(np.int64)
    keep = np.r_[True, np.any(np.diff(q, axis=0) != 0, axis=1)]
    return q[keep]


def area(q):
    x, y = q[:, 0].astype(np.float64), q[:, 1].astype(np.float64)
    return 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))) / Q / Q


def encode(q):
    return [int(q[0, 0]), int(q[0, 1])] + [int(v) for v in np.diff(q, axis=0).ravel()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.path.join(os.environ.get("TEMP", "."), "ne_10m_admin_1_states_provinces.geojson"))
    ap.add_argument("--tol", type=float, default=0.002)
    ap.add_argument("--min-area", type=float, default=0.00003)
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args()
    if not os.path.exists(a.src):
        print("downloading", URL)
        urllib.request.urlretrieve(URL, a.src)
    with open(a.src, encoding="utf-8") as f:
        feats = json.load(f)["features"]
    states = {}
    for ft in feats:
        pr = ft["properties"]
        if pr.get("iso_a2") != "AU" and pr.get("admin") != "Australia":
            continue
        code = str(pr.get("iso_3166_2") or "").split("-")[-1].upper()      # AU-QLD (the postal field is two letters)
        if not code.isalpha():
            code = {"Jervis Bay Territory": "JBT", "Macquarie Island": "TAS", "Ashmore and Cartier Islands": "AC",
                    "Coral Sea Islands": "CS"}.get(pr["name"], pr["name"])
        g = ft["geometry"]
        polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
        for poly in polys:
            rings = [simplify_ring(r, a.tol) for r in poly]
            if len(rings[0]) < 4 or area(rings[0]) < a.min_area:
                continue
            states.setdefault(code, []).append([encode(r) for r in rings if len(r) >= 4 and area(r) >= a.min_area])
    # Natural Earth's coast at Coolangatta lies ~2 km inland of the real one (QLD ends at Greenmount, Point Danger sits at
    # -28.178 / 153.532), leaving Coolangatta in the sea and nearer NSW: a hand patch gives QLD the triangle from there to
    # the real Point Danger (-28.1667 / 153.5547), a border close to Boundary Street
    tri = np.array([[153.5065, -28.1493], [153.5547, -28.1667], [153.5320, -28.1779]])
    states.setdefault("QLD", []).append([encode(np.round(tri * Q).astype(np.int64))])
    raw = json.dumps({"v": 1, "states": states}, separators=(",", ":")).encode()
    with gzip.GzipFile(a.out, "wb", 9, mtime=0) as f:
        f.write(raw)
    print({k: len(v) for k, v in states.items()}, os.path.getsize(a.out), "bytes")


if __name__ == "__main__":
    main()
