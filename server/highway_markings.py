"""Offline, state-specific highway travel-lane paint. Ramp guidance has a separate style.

Boundary data: U.S. Census Bureau TIGERweb Generalized ACS 2024, States 500K.
The selected states use a 4.5 m stripe, a 12 m blank interval and 0.45 m width.
"""
import json
from functools import lru_cache
from pathlib import Path

import numpy as np

DATA = Path(__file__).with_name('highway_marking_states.geojson')
DASH_M, GAP_M, LANE_W = 4.5, 12., .45


@lru_cache(maxsize=1)
def _polygons():
    result = []
    for feature in json.loads(DATA.read_text(encoding='utf-8'))['features']:
        geometry = feature['geometry']
        polygons = [geometry['coordinates']] if geometry['type'] == 'Polygon' else geometry['coordinates']
        for rings in polygons:
            rings = [np.asarray(r, np.float64) for r in rings]
            outer = rings[0]
            result.append((outer.min(0), outer.max(0), rings))
    return result


def dash_length(lat, lon):
    """Local stripe length in metres; zero outside the five requested state polygons."""
    lat, lon = np.broadcast_arrays(lat, lon)
    shape = lat.shape
    x, y = lon.ravel(), lat.ravel()
    found = np.zeros(len(x), np.float64)
    for lo, hi, rings in _polygons():
        use = np.flatnonzero((found == 0.) & (x >= lo[0]) & (x <= hi[0]) & (y >= lo[1]) & (y <= hi[1]))
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
            crossings = ((a[:, 1] > py) != (b[:, 1] > py)) & (px < intercept)
            inside ^= crossings.sum(1) % 2 != 0
        found[use[inside]] = DASH_M
    return found.reshape(shape)


def selected(lat, lon):
    """Boolean mask inside CA, ID, MI, MN or WI, including polygon holes and islands."""
    return dash_length(lat, lon) > 0.


def apply(chunks, frame):
    """Apply the local pattern along each line; leave ramp dots and all other paint alone."""
    out = []
    for chunk in chunks:
        if not chunk.get('highway_lane') or len(chunk['n']) < 2:
            out.append(chunk)
            continue
        p = np.asarray(chunk['n'], np.float64)
        arc = np.r_[0., np.cumsum(np.linalg.norm(np.diff(p[:, :2], axis=0), axis=1))]
        # Probe at short intervals so one map feature can cross a state border correctly.
        at = np.linspace(0., arc[-1], max(2, int(np.ceil(arc[-1]/4.))+1))
        xy = np.c_[np.interp(at, arc, p[:, 0]), np.interp(at, arc, p[:, 1])]
        la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.)
        flags = dash_length(la, lo)
        cuts = [0.]
        for k in np.flatnonzero(flags[1:] != flags[:-1]):
            a, b = at[k:k+2]
            for _ in range(8):
                mid = (a+b)/2
                q = [np.interp(mid, arc, p[:, d]) for d in (0, 1)]
                la, lo, _ = frame.enu_to_geodetic(*q, 0.)
                if float(dash_length(la, lo)) == float(flags[k]):
                    a = mid
                else:
                    b = mid
            cuts.append((a+b)/2)
        cuts.append(float(arc[-1]))
        for a, b in zip(cuts[:-1], cuts[1:]):
            samples = np.r_[a, arc[(arc > a) & (arc < b)], b]
            copy = dict(chunk, n=np.column_stack([np.interp(samples, arc, p[:, d]) for d in range(3)]).tolist())
            if chunk.get('cross'):
                cross = np.asarray(chunk['cross'])
                copy['cross'] = np.c_[np.interp(samples, arc, cross[:, 0]), np.interp(samples, arc, cross[:, 1])].tolist()
            copy['uv_start'] = chunk.get('uv_start', 0.)+a
            mid = (a+b)/2
            la, lo, _ = frame.enu_to_geodetic(np.interp(mid, arc, p[:, 0]), np.interp(mid, arc, p[:, 1]), 0.)
            dash = float(dash_length(la, lo))
            if dash:
                copy.update(dash=dash, gap=GAP_M)
                copy['w'] = LANE_W
            out.append(copy)
    return out
