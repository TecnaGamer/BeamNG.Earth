"""Land boundary of Norway (Kartverket CC BY 4.0, 2024, simplified).

The packaged outline includes mainland and islands. No bounding-box approximation
is used to decide if a foliage point belongs to Norway. Arrays are supported.
"""
import functools
import json
from pathlib import Path

import numpy as np


@functools.lru_cache(maxsize=1)
def _polygons():
    raw = json.loads(Path(__file__).with_name("norway_outline.json").read_text(encoding="utf-8"))
    out = []
    for poly in raw["polygons"]:
        rings = []
        for ring in poly:
            pts = np.asarray(ring, dtype=np.float64)
            if len(pts) < 4:
                continue
            rings.append((pts, (pts[:, 0].min(), pts[:, 1].min(), pts[:, 0].max(), pts[:, 1].max())))
        if rings:
            out.append(rings)
    return out


def _within_ring(x, y, ring):
    """Ray casting; skip all edges that cannot cross any query latitude."""
    pts, bbox = ring
    out = np.zeros(len(x), dtype=bool)
    if not len(x):
        return out
    ylo, yhi = y.min(), y.max()
    prev = pts[-1]
    for point in pts:
        x0, y0 = prev
        x1, y1 = point
        prev = point
        if y0 == y1 or max(y0, y1) < ylo or min(y0, y1) > yhi:
            continue
        cross = ((y0 > y) != (y1 > y)) & (x < x0 + (y - y0) * (x1 - x0) / (y1 - y0))
        out ^= cross
    return out


def contains(lat, lon):
    """Return whether each latitude/longitude is on Norwegian land."""
    yy, xx = np.broadcast_arrays(np.asarray(lat, dtype=np.float64), np.asarray(lon, dtype=np.float64))
    shape = yy.shape
    y, x = yy.ravel(), xx.ravel()
    result = np.zeros(len(x), dtype=bool)
    possible = (y >= 57.0) & (y <= 82.0) & (x >= -10.0) & (x <= 36.0)
    if not possible.any():
        return result.reshape(shape)
    for rings in _polygons():
        _pts, (xmin, ymin, xmax, ymax) = rings[0]
        sel = possible & ~result & (x >= xmin) & (x <= xmax) & (y >= ymin) & (y <= ymax)
        if not sel.any():
            continue
        index = np.flatnonzero(sel)
        inside = _within_ring(x[index], y[index], rings[0])
        for hole in rings[1:]:
            inside &= ~_within_ring(x[index], y[index], hole)
        result[index] = inside
    return result.reshape(shape)


def intersects_bbox(bounds):
    """Cheap conservative extent test for deciding whether to request SR16 WMS."""
    west, south, east, north = bounds
    if east < -10.0 or west > 36.0 or north < 57.0 or south > 82.0:
        return False
    for rings in _polygons():
        _pts, (xmin, ymin, xmax, ymax) = rings[0]
        if west <= xmax and east >= xmin and south <= ymax and north >= ymin:
            return True
    return False


def fully_inside_bbox(bounds):
    """Conservative gate for tile-wide road width changes near borders."""
    west, south, east, north = bounds
    return bool(np.all(contains(np.array([south, south, north, north, (south+north)/2]),
                                np.array([west, east, west, east, (west+east)/2]))))
