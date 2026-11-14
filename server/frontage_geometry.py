"""Disjoint pavement footprints in a street tile's local frame."""

from collections import defaultdict

import numpy as np

from buildings import _area2, triangulate

MIN_AREA2 = .004
CELL_M = 12.
EPS = 1e-7


def _convex_parts(ring):
    ring = np.asarray(ring, np.float64)[:, :2]
    edges = np.roll(ring, -1, axis=0) - ring
    ring = ring[np.any(np.abs(edges) > EPS, axis=1)]
    area = _area2(ring)
    if len(ring) < 3 or abs(area) <= MIN_AREA2:
        return ()
    if area < 0:
        ring = ring[::-1]
    edges = np.roll(ring, -1, axis=0) - ring
    after = np.roll(edges, -1, axis=0)
    if np.all(edges[:, 0] * after[:, 1] - edges[:, 1] * after[:, 0] >= -EPS):
        return (ring,)
    return ring[triangulate(ring)]


def _split(poly, distances):
    """Partition a convex polygon on a cutting line."""
    left, right = [], []
    for i, b in enumerate(poly):
        a, da, db = poly[i - 1], distances[i - 1], distances[i]
        if min(da, db) < -EPS and max(da, db) > EPS:
            crossing = a + (b - a) * (da / (da - db))
            left.append(crossing)
            right.append(crossing)
        if db >= -EPS:
            left.append(b)
        if db <= EPS:
            right.append(b)
    return np.asarray(left), np.asarray(right)


def _subtract(poly, normals, offsets):
    distances = poly @ normals.T - offsets
    if np.any(distances.max(axis=0) <= EPS):
        return (poly,)
    outside, inside = [], poly
    for j in np.flatnonzero(distances.min(axis=0) < -EPS):
        d = inside @ normals[j] - offsets[j]
        if d.max() <= EPS:
            return (poly,)  # the two shapes only touched or missed each other
        if d.min() < -EPS:
            inside, piece = _split(inside, d)
            if len(piece) >= 3 and _area2(piece) > MIN_AREA2:
                outside.append(piece)
    return outside


def _intersection(poly, normals, offsets):
    distances = poly @ normals.T - offsets
    if np.any(distances.max(axis=0) <= EPS):
        return None
    for j in np.flatnonzero(distances.min(axis=0) < -EPS):
        d = poly @ normals[j] - offsets[j]
        if d.max() <= EPS:
            return None
        if d.min() < -EPS:
            poly, _ = _split(poly, d)
    return poly if len(poly) >= 3 and _area2(poly) > MIN_AREA2 else None


class Coverage:
    """Reserve surveyed surfaces first, then claim any free ground for each new strip. The polygon work in Rust
    (earthfast.FrontageCoverage, VERSION 176: _PyCoverage's steps; its numpy on 3-8 point polygons was about half of a
    French tile's frontage pass); _PyCoverage stays as the reference (tests/test_frontage_native.py)."""

    def __init__(self, bounds):
        import fast
        fast.require(176, "FrontageCoverage")
        x0, y0, x1, y1 = np.asarray(bounds, np.float64).reshape(-1)[:4]
        self._c = fast._ef.FrontageCoverage(float(x0), float(y0), float(x1), float(y1))

    @staticmethod
    def _ring(ring):
        return np.ascontiguousarray(np.asarray(ring, np.float64)[:, :2])

    def reserve(self, ring):
        self._c.reserve(self._ring(ring))

    def overlaps(self, ring):
        """Positive-area intersections with the reserved footprints."""
        return self._c.overlaps(self._ring(ring))

    def overlaps_any(self, ring):
        return self._c.overlaps_any(self._ring(ring))

    def claim(self, ring):
        return self._c.claim(self._ring(ring))


class _PyCoverage:
    """Coverage in numpy (the reference for the Rust one)."""

    def __init__(self, bounds):
        self._cells = defaultdict(list)
        self._shapes = []
        self._lo, self._hi = np.asarray(bounds).reshape(2, 2)

    def _keys(self, lo, hi):
        # Large mapped polygons can run well beyond the region being generated.
        x0, y0 = np.floor(np.maximum(lo, self._lo) / CELL_M).astype(int)
        x1, y1 = np.floor(np.minimum(hi, self._hi) / CELL_M).astype(int)
        return ((x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1))

    def _reserve(self, piece):
        lo, hi = piece.min(axis=0), piece.max(axis=0)
        edges = np.roll(piece, -1, axis=0) - piece
        length = np.linalg.norm(edges, axis=1)
        keep = length > EPS
        normals = np.c_[-edges[keep, 1], edges[keep, 0]] / length[keep, None]
        offsets = (piece[keep] * normals).sum(axis=1)
        idx = len(self._shapes)
        self._shapes.append((*lo, *hi, normals, offsets))
        for key in self._keys(lo, hi):
            self._cells[key].append(idx)

    def reserve(self, ring):
        for piece in _convex_parts(ring):
            self._reserve(piece)

    def _nearby(self, piece):
        lo, hi = piece.min(axis=0), piece.max(axis=0)
        nearby = {idx for key in self._keys(lo, hi) for idx in self._cells.get(key, ())}
        x0, y0 = lo
        x1, y1 = hi
        for idx in sorted(nearby):
            a, b, c, d, normals, offsets = self._shapes[idx]
            if x1 <= a + EPS or y1 <= b + EPS or x0 >= c - EPS or y0 >= d - EPS:
                continue
            yield normals, offsets

    def overlaps(self, ring):
        """Positive-area intersections with the reserved footprints."""
        for piece in _convex_parts(ring):
            for normals, offsets in self._nearby(piece):
                inter = _intersection(piece, normals, offsets)
                if inter is not None:
                    yield inter

    def _free(self, piece):
        parts = [piece]
        for normals, offsets in self._nearby(piece):
            parts = [p for part in parts for p in _subtract(part, normals, offsets)]
            if not parts:
                break
        return parts

    def overlaps_any(self, ring):
        return any(True for _ in self.overlaps(ring))

    def claim(self, ring):
        for piece in _convex_parts(ring):
            for part in self._free(piece):
                self._reserve(part)
                yield part
