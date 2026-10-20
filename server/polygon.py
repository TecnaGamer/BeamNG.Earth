"""Point-in-polygon for numpy arrays (even-odd rule), in place of matplotlib.path.Path.contains_points:
the server used matplotlib for nothing else, and the players' install (install.py: server/requirements.txt)
did not have it, so the server did not start from the release zip (2026-10-15).

    Path(ring).contains_points(pts) -> bool array    ring: (N, 2) vertices, open or closed; pts: (M, 2)
"""
import numpy as np

try:
    import earthfast as _ef
except ImportError:
    _ef = None

CHUNK = 1 << 22         # point x edge pairs per numpy step (memory bound)
BANDS = 512


def contains_points(ring, pts):
    """Which of pts (M, 2) lie inside the polygon ring (N, 2) (even-odd crossings of a ray toward +x)."""
    ring = np.asarray(ring, np.float64).reshape(-1, 2)
    pts = np.asarray(pts, np.float64).reshape(-1, 2)
    if _ef is not None and hasattr(_ef, "contains_points"):     # the Rust loop: the same rule and bands
        return np.asarray(_ef.contains_points(np.ascontiguousarray(ring), np.ascontiguousarray(pts)))
    out = np.zeros(len(pts), bool)
    if len(ring) < 3 or len(pts) == 0:
        return out
    x0, y0 = ring[:, 0], ring[:, 1]
    x1, y1 = np.roll(x0, -1), np.roll(y0, -1)
    keep = y0 != y1                         # horizontal edges never cross the ray
    x0, y0, x1, y1 = x0[keep], y0[keep], x1[keep], y1[keep]
    if len(x0) == 0:
        return out
    lo = np.minimum(y0, y1)
    hi = np.maximum(y0, y1)
    slope = (x1 - x0) / (y1 - y0)
    # only the points inside the ring's box can be inside it
    bx0, bx1 = ring[:, 0].min(), ring[:, 0].max()
    by0, by1 = ring[:, 1].min(), ring[:, 1].max()
    cand = np.nonzero((pts[:, 0] >= bx0) & (pts[:, 0] <= bx1) & (pts[:, 1] >= by0) & (pts[:, 1] <= by1))[0]
    if len(cand) == 0:
        return out
    # horizontal bands: a point meets only the edges spanning its band
    nb = int(min(BANDS, max(1, len(x0) // 4)))
    h = (by1 - by0) / nb or 1.0
    band = np.minimum(((pts[cand, 1] - by0) / h).astype(np.int64), nb - 1)
    e0 = np.clip(((lo - by0) / h).astype(np.int64), 0, nb - 1)
    e1 = np.clip(((hi - by0) / h).astype(np.int64), 0, nb - 1)
    order = np.argsort(band, kind="stable")
    cuts = np.searchsorted(band[order], np.arange(nb + 1))
    for b in range(nb):
        idx = cand[order[cuts[b]:cuts[b + 1]]]
        if len(idx) == 0:
            continue
        es = np.nonzero((e0 <= b) & (e1 >= b))[0]
        if len(es) == 0:
            continue
        ex0, ey0, elo, ehi, esl = x0[es], y0[es], lo[es], hi[es], slope[es]
        step = max(1, CHUNK // len(es))
        for i in range(0, len(idx), step):
            j = idx[i:i + step]
            px = pts[j, 0][:, None]
            py = pts[j, 1][:, None]
            # an edge counts when the point's y is in [lo, hi) and the edge crosses y right of the point
            span = (py >= elo) & (py < ehi)
            xc = ex0 + (py - ey0) * esl
            out[j] = (np.count_nonzero(span & (xc > px), axis=1) & 1).astype(bool)
    return out


class Path:
    """The part of matplotlib.path.Path the server uses."""

    def __init__(self, vertices):
        self.vertices = np.asarray(vertices, np.float64).reshape(-1, 2)

    def contains_points(self, pts):
        return contains_points(self.vertices, pts)


def roof_cells(outer, holes=()):
    """Convex cells covering a polygon with holes, using an even-odd sweep.

    Each band lies between consecutive vertex heights. Boundary edges are
    linear within a band, so paired crossings form exact trapezoids. This
    preserves courtyards without a new triangulation dependency or bridges
    across the holes. Input rings must be simple and non-intersecting.
    """
    rings = [np.asarray(r, np.float64)[:, :2] for r in (outer, *holes) if len(r) >= 3]
    if not rings:
        return []
    a = np.concatenate(rings)
    b = np.concatenate([np.roll(r, -1, axis=0) for r in rings])
    ys = np.unique(a[:, 1])
    out = []
    for low, high in zip(ys[:-1], ys[1:]):
        if high - low <= 1e-9:
            continue
        mid = (low + high) / 2
        active = (np.minimum(a[:, 1], b[:, 1]) < mid) & (np.maximum(a[:, 1], b[:, 1]) > mid)
        p, q = a[active], b[active]
        slope = (q[:, 0] - p[:, 0]) / (q[:, 1] - p[:, 1])
        order = np.argsort(p[:, 0] + (mid - p[:, 1]) * slope, kind="stable")
        x0 = p[:, 0] + (low - p[:, 1]) * slope
        x1 = p[:, 0] + (high - p[:, 1]) * slope
        for left, right in zip(order[::2], order[1::2]):
            cell = np.array([(x0[left], low), (x0[right], low),
                             (x1[right], high), (x1[left], high)])
            keep = np.linalg.norm(cell - np.roll(cell, 1, axis=0), axis=1) > 1e-9
            if keep.sum() >= 3:
                out.append(cell[keep])
    return out


def subtract_cells(outer, cutters=(), holes=()):
    """Convex cells of outer minus the union of cutters and holes.

    Unlike the even-odd roof sweep, overlapping cutters must remain empty.
    Split at segment intersections too, so the boundary order stays constant
    inside each band. This also handles parts crossing a parent's outline.
    """
    rings = [np.asarray(r, np.float64)[:, :2] for r in (outer, *holes, *cutters) if len(r) >= 3]
    if not rings:
        return []
    a = np.concatenate(rings)
    b = np.concatenate([np.roll(r, -1, axis=0) for r in rings])
    group = np.repeat(np.arange(len(rings)), [len(r) for r in rings])
    d = b - a
    ys = list(a[:, 1])
    # A vectorised row at a time avoids an unbounded N x N allocation.
    for i in range(len(a) - 1):
        other = np.arange(i + 1, len(a))
        q, e = a[other] - a[i], d[other]
        den = d[i, 0] * e[:, 1] - d[i, 1] * e[:, 0]
        safe = np.where(np.abs(den) > 1e-12, den, 1.0)
        t = (q[:, 0] * e[:, 1] - q[:, 1] * e[:, 0]) / safe
        u = (q[:, 0] * d[i, 1] - q[:, 1] * d[i, 0]) / safe
        hit = (np.abs(den) > 1e-12) & (t > 1e-9) & (t < 1 - 1e-9) & (u > 1e-9) & (u < 1 - 1e-9)
        ys.extend((a[i, 1] + t[hit] * d[i, 1]).tolist())
    ys = np.unique(ys)
    out = []
    for low, high in zip(ys[:-1], ys[1:]):
        if high - low <= 1e-8:
            continue
        mid = (low + high) / 2
        active = np.flatnonzero((np.minimum(a[:, 1], b[:, 1]) < mid) &
                                (np.maximum(a[:, 1], b[:, 1]) > mid))
        slope = d[active, 0] / d[active, 1]
        order = np.argsort(a[active, 0] + (mid - a[active, 1]) * slope, kind='stable')
        x0 = a[active, 0] + (low - a[active, 1]) * slope
        x1 = a[active, 0] + (high - a[active, 1]) * slope
        inside = np.zeros(len(rings), bool)
        for k, left in enumerate(order[:-1]):
            inside[group[active[left]]] ^= True
            if not inside[0] or inside[1:].any():
                continue
            right = order[k + 1]
            cell = np.array([(x0[left], low), (x0[right], low),
                             (x1[right], high), (x1[left], high)])
            area = abs(np.sum(cell[:, 0] * np.roll(cell[:, 1], -1) -
                              np.roll(cell[:, 0], -1) * cell[:, 1])) / 2
            keep = np.linalg.norm(cell - np.roll(cell, 1, axis=0), axis=1) > 1e-8
            if area > 1e-7 and keep.sum() >= 3:
                out.append(cell[keep])
    return out
