"""Start places on a road (the level select's "Random location", 2026-10-20: "a random option that places you in a
random part of the world"; the default places list, some of which started the player in a building or on a hill).

    road_spot(md, lat, lon, radius_m)   -> (lat, lon, class) on the nearest public road (on the ground: no bridge or
                                           tunnel) within radius_m, or None
    pick(md, rng)                       -> (lat, lon, name): a road near a random town (Natural Earth's 1:10m
                                           populated places, the world map's place names: maplayers.js), or None

Both read the roads layer of the map cells around the point (md.cell, kind "demand": downloaded when not at hand), so
they run in a worker thread.
"""
import json
import logging
import math
import os
import sys

import numpy as np
import dmath
import dnp

import mapcells
import roads as roadsmod

log = logging.getLogger("earthd.randomplace")

ROOT = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOWNS_JS = os.path.join(ROOT, "mod", "beamng_earth", "ui", "ui-vue", "mods", "beamngEarth", "maplayers.js")

ROAD_CLASSES = ("trunk", "primary", "secondary", "tertiary", "unclassified", "residential")
OFFSET_M = 4000.0           # a random start is up to this far from its town's centre ...
SEARCH_M = 1500.0           # ... on the nearest road within this
END_CLEAR_M = 25.0          # m from a road piece's ends (its junctions) where a start is not put, when it is that long
TRIES = 10                  # towns tried before giving up (no road near: a remote point, the sea)
M_LAT = 111320.0

_towns = None


def towns():
    """[(lat, lon, name)] of the populated places, read once (an empty list when the file is missing)."""
    global _towns
    if _towns is None:
        try:
            with open(TOWNS_JS, encoding="utf-8") as fh:
                text = fh.read()
            data = json.loads(text[text.index("export default ") + len("export default "):].rstrip().rstrip(";"))
            _towns = [(float(p[2]), float(p[1]), " ".join(str(p[0]).split())) for p in data.get("places", [])]
        except (OSError, ValueError, KeyError, IndexError) as e:
            log.warning("random place: no towns (%s: %s)", type(e).__name__, e)
            _towns = []
    return _towns


def _seg_nearest(xy, p):
    """(distance, index of the segment, t along it) of the point p nearest on polyline xy (k, 2)."""
    a, b = xy[:-1], xy[1:]
    ab = b - a
    L2 = np.maximum((ab * ab).sum(1), 1e-12)
    t = np.clip(((p - a) * ab).sum(1) / L2, 0.0, 1.0)
    d = dnp.hypot(*(a + t[:, None] * ab - p).T)
    k = int(d.argmin())
    return float(d[k]), k, float(t[k])


def road_spot(md, lat, lon, radius_m=SEARCH_M, classes=ROAD_CLASSES):
    """(lat, lon, class) of the nearest point within radius_m on a public road on the ground, kept END_CLEAR_M off the
    ends of its piece (a junction's middle is no place to start), or None."""
    k = dmath.cos(math.radians(lat))
    dlat, dlon = radius_m / M_LAT, radius_m / (M_LAT * max(k, 0.05))
    best = None
    for key in mapcells.cells_for_bbox(lat - dlat, lon - dlon, lat + dlat, lon + dlon):
        f = md.cell(key, layers=("roads",), kind="demand").layers.get("roads")
        if f is None or not len(f):
            continue
        cls = f.attrs.get("class")
        for i in range(len(f)):
            c = str(cls[i]) if cls is not None else ""
            if c not in classes:
                continue
            for ring in roadsmod.ground_parts(f, i):
                ring = np.asarray(ring, np.float64)
                if len(ring) < 2:
                    continue
                xy = np.stack([(ring[:, 0] - lon) * M_LAT * k, (ring[:, 1] - lat) * M_LAT], axis=1)
                s = np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(xy, axis=0).T))]
                d, j, t = _seg_nearest(xy, np.zeros(2))
                if d > radius_m or (best is not None and d >= best[0]):
                    continue
                at = s[j] + t * (s[j + 1] - s[j])
                if s[-1] > 2 * END_CLEAR_M:
                    at = min(max(at, END_CLEAR_M), s[-1] - END_CLEAR_M)
                q = np.array([np.interp(at, s, xy[:, 0]), np.interp(at, s, xy[:, 1])])
                best = (d, float(lat + q[1] / M_LAT), float(lon + q[0] / (M_LAT * k)), c)
    return best[1:] if best else None


def pick(md, rng=None):
    """(lat, lon, name) on a road near a random town, or None after TRIES towns."""
    rng = rng or np.random.default_rng()
    ts = towns()
    if not ts:
        return None
    for _ in range(TRIES):
        lat, lon, name = ts[int(rng.integers(len(ts)))]
        r = OFFSET_M * math.sqrt(float(rng.random()))           # (uniform over the disc)
        a = 2 * math.pi * float(rng.random())
        la = lat + r * dmath.sin(a) / M_LAT
        lo = lon + r * dmath.cos(a) / (M_LAT * max(dmath.cos(math.radians(lat)), 0.05))
        try:
            spot = road_spot(md, la, lo)
        except Exception as e:          # a cell that will not load (offline): another town
            log.debug("random place near %s: %s: %s", name, type(e).__name__, e)
            continue
        if spot is not None:
            return spot[0], spot[1], f"Near {name}"
    return None
