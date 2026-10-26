"""Longitudinal paint dimensions from the project's ROAD_IMPROVEMENT_NOTES.md (metres).

Dash geometry uses solid paint, so texture duty cycles cannot change the requested
length/gap. Phase is measured on the source line before tile/junction clipping.
"""
from collections import namedtuple

import numpy as np

Profile = namedtuple('Profile', 'travel ramp solid')
DEFAULT = Profile((3.0, 8.0, .35), (1.2, 2.8, .35), .35)
PROFILES = {
    'KY': Profile((3.5, 8.9, .6), None, .6),
    'MI': Profile((4.5, 11.5, .4), (1.5, 6.0, .35), .4),
    'MN': Profile((3.1, 12.25, .3), None, .45),
    'WI': Profile((5.15, 10.25, .31), (1.25, 2.9, .3), .32),
    'CA': Profile((4.6, 10.5, .35), (2.3, 4.9, .25), .25),
}


def profile(state=''):
    return PROFILES.get(state, DEFAULT)


def arc(points):
    return np.r_[0., np.cumsum(np.linalg.norm(np.diff(points[:, :2], axis=0), axis=1))]


def phase(source, point):
    """Distance to a clipped endpoint along its original marking polyline."""
    d = np.diff(source[:, :2], axis=0)
    t = np.clip(((point[:2] - source[:-1, :2]) * d).sum(1) /
                np.maximum((d * d).sum(1), 1e-12), 0., 1.)
    q = source[:-1, :2] + t[:, None] * d
    k = int(np.argmin(((q - point[:2]) ** 2).sum(1)))
    s = arc(source)
    return float(s[k] + t[k] * (s[k + 1] - s[k]))


def paint(chunks):
    """Expand dashed ribbons after clipping; preserve short ramp dashes and deck heights.

    Output is ordinary solid chunks understood by both mesh and DecalRoad renderers.
    Calling twice is harmless (bridge/tunnel output also passes through build_tile).
    """
    out = []
    for chunk in chunks:
        if 'dash' not in chunk:
            out.append(chunk)
            continue
        length, gap = chunk['dash']
        p = np.asarray(chunk['n'], np.float64)
        if len(p) < 2:
            continue
        s = arc(p)
        period = length + gap
        start = -(chunk.get('phase', 0.) % period)
        base = {k: v for k, v in chunk.items() if k not in ('dash', 'phase')}
        while start < s[-1]:
            a, b = max(0., start), min(s[-1], start + length)
            if b - a > 1e-5:
                at = np.r_[a, s[(s > a) & (s < b)], b]
                nodes = np.column_stack([np.interp(at, s, p[:, d]) for d in range(p.shape[1])])
                out.append(dict(base, n=nodes.tolist()))
            start += period
    return out
