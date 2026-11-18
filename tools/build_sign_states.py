"""build_sign_states.py SRC.geojson: server/sign_states.geojson, the US states' outlines the highway signs' styles
look up (server/signstates.py: shield shapes, post spacing, portrait exit panels), from the U.S. Census Bureau's
TIGERweb Generalized ACS 2024 "States 500K" layer (public domain, a work of the US government) that PR #363 bundled
as highway_marking_states.geojson (6.5 MB).

    python3 tools/build_sign_states.py highway_marking_states.geojson

Every ring simplified (Douglas-Peucker, TOLERANCE_M), islands smaller than MIN_AREA_KM2 left out, coordinates
rounded to 1e-4 degrees (~11 m): a lookup near a border may answer the neighbour within ~TOLERANCE_M, which a sign's
style does not mind. Deterministic (no randomness, features in the source's order). The source query is kept in the
output's "source_url".
"""
import json
import math
import os
import sys

try:        # (first: numpy's AVX-512 kernels held back before anything imports numpy, tools/npyfirst.py)
    import npyfirst  # noqa: E402,F401
except ImportError:     # (loaded by its path into another process, a test: that process's entry point guards)
    pass
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "server", "sign_states.geojson")
TOLERANCE_M = 300.0
MIN_AREA_KM2 = 10.0
DIGITS = 4


def simplify(ring, tol_m):
    """Douglas-Peucker on a closed lon/lat ring (metres by a local equirectangular scale); stays closed."""
    p = np.asarray(ring, np.float64)
    if len(p) < 5:
        return p
    scale = np.array([111320.0 * math.cos(math.radians(float(p[:, 1].mean()))), 110540.0])
    q = p * scale
    keep = np.zeros(len(p), bool)
    keep[0] = keep[-1] = True
    # the farthest point from the first splits the ring in two open chains (the first and last points coincide)
    far = int(np.argmax(np.linalg.norm(q - q[0], axis=1)))
    keep[far] = True
    stack = [(0, far), (far, len(p) - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        ab = q[b] - q[a]
        n = np.linalg.norm(ab)
        seg = q[a + 1:b] - q[a]
        d = np.abs(seg[:, 0] * ab[1] - seg[:, 1] * ab[0]) / n if n > 0 else np.linalg.norm(seg, axis=1)
        k = int(np.argmax(d))
        if d[k] > tol_m:
            m = a + 1 + k
            keep[m] = True
            stack += [(a, m), (m, b)]
    return p[keep]


def area_km2(ring):
    p = np.asarray(ring, np.float64)
    scale = np.array([111.32 * math.cos(math.radians(float(p[:, 1].mean()))), 110.54])
    q = p * scale
    return abs(float(np.sum(q[:-1, 0] * q[1:, 1] - q[1:, 0] * q[:-1, 1]))) / 2


def build(src):
    out = {"type": "FeatureCollection", "source": src.get("source", ""), "source_url": src.get("source_url", ""),
           "license": "Public domain (U.S. Census Bureau TIGERweb); simplified by tools/build_sign_states.py",
           "features": []}
    for f in src["features"]:
        g = f["geometry"]
        polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
        kept = []
        for poly in polys:
            if area_km2(poly[0]) < MIN_AREA_KM2:
                continue
            rings = []
            for i, r in enumerate(poly):
                s = np.round(simplify(r, TOLERANCE_M), DIGITS)
                if len(s) >= 4 and (i == 0 or area_km2(s) >= MIN_AREA_KM2):
                    rings.append(s.tolist())
            if rings:
                kept.append(rings)
        if kept:
            props = {k: f["properties"][k] for k in ("STUSAB", "NAME") if k in f["properties"]}
            out["features"].append({"type": "Feature", "properties": props,
                                    "geometry": {"type": "MultiPolygon", "coordinates": kept}})
    return out


def main():
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    with open(sys.argv[1], encoding="utf-8") as fh:
        src = json.load(fh)
    out = build(src)
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(out, separators=(",", ":")) + "\n")
    print(f"{len(out['features'])} states, {os.path.getsize(OUT) / 1e6:.2f} MB -> {OUT}")


if __name__ == "__main__":
    sys.exit(main())
