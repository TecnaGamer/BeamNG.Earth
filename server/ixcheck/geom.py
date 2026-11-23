"""ixcheck's own small geometry over the artifacts' lines: dnp for the maths, so a check's verdict is the same on every
platform. The tolerances are fixed here (the checks read no pipeline code)."""
import numpy as np

import dnp

JOIN_M = 2.5    # m: ends this close meet (the join law's tolerance)
SAME_S_M = 1.0  # m of arc: a relation at an end is at the end (gradeshapes folds a joint found from both its parts' ends)
END_M = 2.0     # m of arc: a relation this near either member's end is an end's (an end on the other's line, within JOIN_M of it),
#                 not a crossing of the two lines
CROSS_M = 0.5   # m: a recomputed crossing's two points, and their agreement with q


def dist(p, q):
    """The distance of two points."""
    d = np.asarray(p, np.float64) - np.asarray(q, np.float64)
    return float(dnp.hypot(d[0], d[1]))


def arclen(line):
    """Cumulative arc length (m,) along line (k, 2), from 0."""
    line = np.asarray(line, np.float64)
    if len(line) < 2:
        return np.zeros(len(line))
    d = np.diff(line, axis=0)
    return np.concatenate([np.zeros(1), np.cumsum(dnp.hypot(d[:, 0], d[:, 1]))])


def at_s(line, s):
    """The point of line at arc length s (clamped to its ends)."""
    line = np.asarray(line, np.float64)
    if not len(line):
        return np.zeros(2)
    a = arclen(line)
    if len(line) == 1 or a[-1] <= 0.0:
        return line[0].copy()
    s = min(max(float(s), 0.0), float(a[-1]))
    k = min(max(int(np.searchsorted(a, s, side="right")) - 1, 0), len(line) - 2)
    den = float(a[k + 1] - a[k])
    if den <= 0.0:
        return line[k].copy()
    return line[k] + (line[k + 1] - line[k]) * ((s - float(a[k])) / den)


def seg_dist(p, a, b):
    """The distance of point p to segment [a, b]."""
    p, a, b = (np.asarray(v, np.float64) for v in (p, a, b))
    ab = b - a
    dd = float(dnp.hypot(ab[0], ab[1]))
    if dd <= 0.0:
        return dist(p, a)
    t = min(max(((p[0] - a[0]) * ab[0] + (p[1] - a[1]) * ab[1]) / (dd * dd), 0.0), 1.0)
    return dist(p, a + ab * t)


def line_dist(p, line):
    """The distance of point p to each segment of line ((k - 1,) array)."""
    line = np.asarray(line, np.float64)
    if len(line) < 2:
        return np.zeros(0)
    a, b = line[:-1], line[1:]
    ab = b - a
    dd = dnp.hypot(ab[:, 0], ab[:, 1])
    safe = np.where(dd > 0.0, dd, 1.0)
    ap = np.asarray(p, np.float64)[None, :] - a
    t = np.clip(np.where(dd > 0.0, (ap[:, 0] * ab[:, 0] + ap[:, 1] * ab[:, 1]) / (safe * safe), 0.0), 0.0, 1.0)
    d = p[None, :] - (a + ab * t[:, None])
    return dnp.hypot(d[:, 0], d[:, 1])


def crossings(a, b):
    """Where two polylines meet: [(s on a, s on b, q)], sorted by the arcs. Parallel segments never cross here."""
    A, B = np.asarray(a, np.float64), np.asarray(b, np.float64)
    if len(A) < 2 or len(B) < 2:
        return []
    sa, sb = arclen(A), arclen(B)
    out = []
    for i in range(len(A) - 1):
        p, r = A[i], A[i + 1] - A[i]
        for j in range(len(B) - 1):
            q, s = B[j], B[j + 1] - B[j]
            den = float(r[0] * s[1] - r[1] * s[0])
            if abs(den) <= 1e-12:
                continue
            dq = q - p
            t = float(dq[0] * s[1] - dq[1] * s[0]) / den
            u = float(dq[0] * r[1] - dq[1] * r[0]) / den
            if -1e-9 <= t <= 1.0 + 1e-9 and -1e-9 <= u <= 1.0 + 1e-9:
                t, u = min(max(t, 0.0), 1.0), min(max(u, 0.0), 1.0)
                out.append((float(sa[i] + t * (sa[i + 1] - sa[i])), float(sb[j] + u * (sb[j + 1] - sb[j])),
                            p + r * t))
    return sorted(out, key=lambda x: (x[0], x[1]))


def crossing_tol(la, sa, lb, sb):
    """How far apart the two points of a relation (at sa on line la, at sb on lb) and its q may be: CROSS_M where the lines
    cross in the middle of both; JOIN_M where one is at its end (the end's own point is within a join of the other's line)."""
    ends = min(sa, float(arclen(la)[-1]) - sa, sb, float(arclen(lb)[-1]) - sb)
    return JOIN_M if ends < END_M else CROSS_M
