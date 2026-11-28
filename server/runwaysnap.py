"""The mapped runway lines snapped onto the OurAirports runways they are the same runway as (airportgrade grades and marks the
OurAirports ones; OpenStreetMap's lines are the paving's and the taxiways' reference): Brisbane's 19L is drawn 4 m to the side of
OurAirports' (0.5 degrees of heading, 4 m across at its end, 75 m along: a pale strip showed beside and beyond the graded runway), and its
taxiways end on the mapped line, not on the runway's centreline.

    set_provider(fn)        # fn(lat0, lon0, lat1, lon1) -> [(le lat, le lon, he lat, he lon, width m)] of the runways in the box
    snap(items, rws) -> (lines, ways)

items: {"runway": [(key, lonlat (n, 2))], "stopway": [...], "way": [...]} the mapped lines (lon, lat); rws: the OurAirports runways.
A runway or stopway line whose heading is within SNAP_HEADING_DEG of a runway's and whose lateral offset from its centreline is under
SNAP_LATERAL_M (the nearest such runway) is that runway's: a runway line is laid on the centreline (a straight line, the lateral offset
taken out, the position along it kept) at the runway's width and kept only between the runway's ends; a stopway line (within
SNAP_STOPWAY_M of an end of it) is laid on the centreline beyond that end, its length kept, at the runway's width. A way's end (a
taxiway's) that is within SNAP_END_M of a mapped runway line is moved by the same lateral offset (the line's there), so it ends on the
runway's centreline, the way's vertices within SNAP_BLEND_M of it moving less and less. lines: {key: (lonlat (2, 2) or None for a line
with nothing left, width m)} of the runway and stopway lines moved; ways: {key: lonlat (n, 2)} of the ways moved. Pure.
"""
import math

import numpy as np

import dmath
import dnp
import geodesy

SNAP_HEADING_DEG = 5.0          # a mapped line within this of a runway's heading ...
SNAP_LATERAL_M = 80.0           # ... and this near its centreline (the median of the line's points) is that runway
SNAP_STOPWAY_M = 250.0          # a stopway's near end this near (along) a runway's end belongs to that end
SNAP_END_M = 3.0                # a way's end this near a mapped runway line ended on it
SNAP_BLEND_M = 60.0             # the way's vertices this far back from its end move by the end's offset, less and less
_SIN = dmath.sin(math.radians(SNAP_HEADING_DEG))
_TOKEN = [0]
_PROVIDER = [None]


def set_provider(fn):
    """Take the OurAirports runways from fn(lat0, lon0, lat1, lon1) (None: none, nothing is snapped)."""
    _PROVIDER[0] = fn
    _TOKEN[0] += 1


def token():
    """A number that changes whenever the provider does (what snapped results are kept by)."""
    return _TOKEN[0]


def runways_in(lat0, lon0, lat1, lon1):
    """The provider's runways in the box ([] without one)."""
    fn = _PROVIDER[0]
    return [] if fn is None else list(fn(lat0, lon0, lat1, lon1))


class _Rw:
    """An OurAirports runway in its own local frame: s along it from its first end, t to its left."""
    __slots__ = ("frame", "u", "left", "length", "width")

    def __init__(self, le_lat, le_lon, he_lat, he_lon, width):
        self.frame = geodesy.EnuFrame(float(le_lat), float(le_lon), 0.0)
        e, n, _ = self.frame.geodetic_to_enu(float(he_lat), float(he_lon), 0.0)
        e, n = float(e), float(n)
        self.length = float(dnp.hypot(e, n))
        self.u = np.array([e, n]) / max(self.length, 1e-9)
        self.left = np.array([-self.u[1], self.u[0]])
        self.width = float(width)

    def local(self, lonlat):
        e, n, _ = self.frame.geodetic_to_enu(np.asarray(lonlat[:, 1], np.float64), np.asarray(lonlat[:, 0], np.float64), 0.0)
        return np.stack([np.asarray(e), np.asarray(n)], 1)

    def st(self, xy):
        return xy[:, 0] * self.u[0] + xy[:, 1] * self.u[1], xy[:, 0] * self.left[0] + xy[:, 1] * self.left[1]

    def lonlat(self, s, t):
        xy = np.asarray(s, np.float64)[:, None] * self.u + np.asarray(t, np.float64)[:, None] * self.left
        la, lo, _ = self.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
        return np.stack([np.asarray(lo), np.asarray(la)], 1)


def _along(rw, ln):
    """(s, t) of a mapped line's points when it runs along the runway rw (within SNAP_HEADING_DEG, the median |t| under SNAP_LATERAL_M), else None."""
    xy = rw.local(ln)
    d = xy[-1] - xy[0]
    n = float(dnp.hypot(d[0], d[1]))
    if n < 1e-6:
        return None
    d = d / n
    if abs(float(d[0] * rw.u[1] - d[1] * rw.u[0])) > _SIN:
        return None
    s, t = rw.st(xy)
    if float(np.median(np.abs(t))) >= SNAP_LATERAL_M:
        return None
    return s, t


def snap(items, rws):
    """(lines, ways): see the module docstring."""
    runways = [_Rw(*r) for r in rws if r[4] > 0.0]
    lines, ways = {}, {}
    if not runways:
        return lines, ways
    kept = []                                   # (the matched mapped runway and stopway lines: lonlat, the runway's, s, t)
    for kind in ("runway", "stopway"):
        for key, ln in items.get(kind, ()):
            ln = np.asarray(ln, np.float64)
            if len(ln) < 2:
                continue
            best = None
            for rw in runways:
                got = _along(rw, ln)
                if got is None:
                    continue
                s, t = got
                if kind == "runway":
                    if float(s.max()) <= 0.0 or float(s.min()) >= rw.length:
                        continue
                else:                           # (its near end's distance from the runway's end it is by)
                    near = float(s.min()) - rw.length if float(s.mean()) >= rw.length / 2.0 else float(s.max())
                    if abs(near) > SNAP_STOPWAY_M:
                        continue
                lat = float(np.median(np.abs(t)))
                if best is None or lat < best[0]:
                    best = (lat, rw, s, t)
            if best is None:
                continue
            _lat, rw, s, t = best
            kept.append((ln, rw, s, t))
            if kind == "runway":
                lo, hi = max(float(s.min()), 0.0), min(float(s.max()), rw.length)
                lines[key] = (rw.lonlat([lo, hi], [0.0, 0.0]) if hi - lo > 1e-6 else None, rw.width)
            else:
                length = float(s.max() - s.min())
                if float(s.mean()) >= rw.length / 2.0:
                    a, b = rw.length, rw.length + length
                else:
                    a, b = -length, 0.0
                lines[key] = (rw.lonlat([a, b], [0.0, 0.0]), rw.width)
    for key, ln in items.get("way", ()):
        ln = np.asarray(ln, np.float64)
        if len(ln) < 2 or not kept:
            continue
        out = ln.copy()
        moved = False
        for end in (0, -1):
            off = _end_offset(ln[end], kept)
            if off is None:
                continue
            moved = True
            out = _blend(out, ln, end, off)
        if moved:
            ways[key] = out
    return lines, ways


def _end_offset(p, kept):
    """The offset (lon, lat) that takes the point p (lon, lat), a way's end on a mapped runway line of `kept`, onto the runway's centreline
    (the lateral offset of the line there), None where p is on none."""
    best = None
    for ln, rw, s, t in kept:
        xy = rw.local(ln)
        q = rw.local(np.asarray(p, np.float64)[None])[0]
        a, b = xy[:-1], xy[1:]
        ab = b - a
        k = np.clip(dnp.vecdot(q - a, ab) / np.maximum(dnp.vecdot(ab, ab), 1e-12), 0.0, 1.0)
        pr = a + k[:, None] * ab
        d = dnp.hypot(*(pr - q).T)
        j = int(np.argmin(d))
        if float(d[j]) > SNAP_END_M:
            continue
        tq = float(t[j] + k[j] * (t[j + 1] - t[j]))
        if best is None or float(d[j]) < best[0]:
            moved = q - rw.left * tq
            la0, lo0, _ = rw.frame.enu_to_geodetic(np.array([q[0]]), np.array([q[1]]), np.zeros(1))
            la1, lo1, _ = rw.frame.enu_to_geodetic(np.array([moved[0]]), np.array([moved[1]]), np.zeros(1))
            best = (float(d[j]), np.array([float(lo1[0]) - float(lo0[0]), float(la1[0]) - float(la0[0])]), rw)
    return None if best is None else (best[1], best[2])


def _blend(out, ln, end, off):
    """`out` with the end `end` (0 or -1) of the way ln moved by the offset off = (lonlat offset, runway), the vertices up to SNAP_BLEND_M
    back from it by less and less (linearly)."""
    d, rw = off
    xy = rw.local(ln)
    seg = dnp.hypot(*np.diff(xy, axis=0).T)
    cum = np.r_[0.0, np.cumsum(seg)]
    back = cum if end == 0 else cum[-1] - cum
    w = np.clip(1.0 - back / SNAP_BLEND_M, 0.0, 1.0)
    return out + w[:, None] * d[None]
