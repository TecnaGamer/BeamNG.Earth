"""Bundled US state boundaries for sign styles and road-marking dimensions."""
from functools import lru_cache
import json
from pathlib import Path
import numpy as np

DATA = Path(__file__).with_name("highway_marking_states.geojson")

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


def states(lat, lon):
    """State abbreviation, or '' outside the 50 states / DC; holes and islands respected."""
    lat, lon = np.broadcast_arrays(lat, lon)
    x, y = lon.ravel(), lat.ravel()
    found = np.full(len(x), '', dtype='U2')
    if not len(x):
        return found.reshape(lat.shape)
    polygons = _polygons()
    # One vectorized bounds test replaces a Python/numpy lookup for every US
    # island on every stroke. Keep the original polygon order and exact test.
    finite = np.isfinite(x) & np.isfinite(y)
    if not finite.any():
        return found.reshape(lat.shape)
    low = np.array([x[finite].min(), y[finite].min()])
    high = np.array([x[finite].max(), y[finite].max()])
    candidates = np.flatnonzero((polygons.upper >= low).all(1) & (polygons.lower <= high).all(1))
    for index in candidates:
        state, lo, hi, rings = polygons[index]
        use = np.flatnonzero((found == '') & (x >= lo[0]) & (x <= hi[0]) & (y >= lo[1]) & (y <= hi[1]))
        if not len(use):
            continue
        px, py = x[use, None], y[use, None]
        inside = np.zeros(len(use), bool)
        for ring in rings:
            a, b = ring[:-1], ring[1:]
            relevant = (np.maximum(a[:, 1], b[:, 1]) >= py.min()) & (np.minimum(a[:, 1], b[:, 1]) <= py.max())
            a, b = a[relevant], b[relevant]
            dy = b[:, 1]-a[:, 1]
            intercept = a[:, 0]+(py-a[:, 1])*(b[:, 0]-a[:, 0])/np.where(dy == 0., 1., dy)
            inside ^= (((a[:, 1] > py) != (b[:, 1] > py)) & (px < intercept)).sum(1) % 2 != 0
        found[use[inside]] = state
    return found.reshape(lat.shape)

