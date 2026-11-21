"""Bundled US state outlines for the highway signs' styles (sign_states.geojson: the Census Bureau's TIGERweb
states, public domain, simplified by tools/build_sign_states.py)."""
from functools import lru_cache
import json
from pathlib import Path
import numpy as np

DATA = Path(__file__).with_name("sign_states.geojson")

class _Polygons(list):
    """Bounds shared by all lookups, including the many separate paint strokes."""
    def __init__(self, rows):
        super().__init__(rows)
        self.lower = np.array([row[1] for row in rows])
        self.upper = np.array([row[2] for row in rows])


@lru_cache(maxsize=1)
def _polygons():
    result = []
    data = json.loads(DATA.read_text(encoding='utf-8'))
    for feature in data['features']:
        state = feature['properties']['STUSAB']
        g = feature['geometry']
        polygons = [g['coordinates']] if g['type'] == 'Polygon' else g['coordinates']
        for polygon in polygons:
            rings = [np.asarray(r, np.float64) for r in polygon]
            result.append((state, rings[0].min(0), rings[0].max(0), rings))
    return _Polygons(result)


@lru_cache(maxsize=1)
def _index():
    """(earthfast.PolygonIndex of _polygons(), their states as a U2 array)."""
    import fast
    fast.require(385, "PolygonIndex")
    polygons = _polygons()
    return (fast._ef.PolygonIndex(np.ascontiguousarray(polygons.lower, np.float64).reshape(-1, 2),
                                  np.ascontiguousarray(polygons.upper, np.float64).reshape(-1, 2),
                                  [[np.ascontiguousarray(r, np.float64) for r in row[3]] for row in polygons]),
            np.array([row[0] for row in polygons], dtype='U2'))


def states(lat, lon):
    """State abbreviation, or '' outside the 50 states / DC; holes and islands respected: each point's first polygon
    (in _polygons' order) whose box and rings hold it, by the even-odd count of all its rings' edges (earthfast
    roadnet::regions::PolygonIndex)."""
    lat, lon = np.broadcast_arrays(lat, lon)
    x, y = lon.ravel(), lat.ravel()
    found = np.full(len(x), '', dtype='U2')
    if not len(x):
        return found.reshape(lat.shape)
    index, names = _index()
    got = index.lookup(np.ascontiguousarray(x, np.float64), np.ascontiguousarray(y, np.float64))
    hit = got >= 0
    if hit.any():
        found[hit] = names[got[hit]]
    return found.reshape(lat.shape)
