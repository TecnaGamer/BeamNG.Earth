"""The world server's Rust kernels (native/earthfast, built by tools/build_native.py) with their Python fallbacks.

The road tiles' hot loops were Python over tiny numpy arrays (a 2026-10-15 profile: the islands' curb ray march alone
was ~20 % of a road tile, 0.9 s of ~4 s); the compiled module does them in one call. Without it (no binary for this
platform) every caller keeps its Python code or uses the vectorised numpy fallbacks here: fast.AVAILABLE says which.
"""
import math
import numpy as np

try:
    import earthfast as _ef
except ImportError:         # no binary for this platform / Python: the Python code
    _ef = None

AVAILABLE = _ef is not None

_PAIR_BATCH = 2_000_000     # (segment, point) pairs per numpy batch in grade_points / contains_points
_BANDS = 512


def segments(lines_hw):
    """(segs (m, 4) ax ay bx by, hw (m,)) of [(polyline (k, 2), half width)]."""
    valid = []
    total = 0
    for pts, hw in lines_hw:
        p = np.asarray(pts, dtype=np.float64)[:, :2]
        n = len(p) - 1
        if n < 1:
            continue
        valid.append((p, n, float(hw)))
        total += n
    if not total:
        return np.zeros((0, 4), dtype=np.float64), np.zeros(0, dtype=np.float64)
    segs = np.empty((total, 4), dtype=np.float64)
    hws = np.empty(total, dtype=np.float64)
    pos = 0
    for p, n, hw in valid:
        nxt = pos + n
        segs[pos:nxt, :2] = p[:-1]
        segs[pos:nxt, 2:] = p[1:]
        hws[pos:nxt] = hw
        pos = nxt
    return segs, hws


def points_inside(poly, pts):
    """Even-odd point-in-polygon for pts (n, 2) against poly (k, 2) -> bool (n,)."""
    poly = np.ascontiguousarray(np.asarray(poly, dtype=np.float64)[:, :2])
    pts = np.ascontiguousarray(np.asarray(pts, dtype=np.float64)[:, :2])
    if _ef is not None:
        return np.asarray(_ef.points_inside(poly, pts))
    n = len(pts)
    if len(poly) < 3 or n == 0:
        return np.zeros(n, dtype=bool)
    px, py = poly[:, 0], poly[:, 1]
    xj, yj = np.roll(px, 1), np.roll(py, 1)
    dy = np.where(yj - py == 0.0, 1e-12, yj - py)
    dx = xj - px
    x, y = pts[:, 0], pts[:, 1]
    out = np.empty(n, dtype=bool)
    step = max(1, (1 << 20) // max(1, len(poly)))
    for i in range(0, n, step):
        xs = x[i:i + step, None]
        ys = y[i:i + step, None]
        cross = ((py[None, :] > ys) != (yj[None, :] > ys)) & (
            xs < dx[None, :] * (ys - py[None, :]) / dy[None, :] + px[None, :]
        )
        out[i:i + step] = (np.count_nonzero(cross, axis=1) & 1).astype(bool)
    return out


def point_inside(poly, x, y):
    """Single point (x, y) in polygon poly (k, >=2), even-odd."""
    poly = np.asarray(poly, dtype=np.float64)
    if _ef is not None and poly.ndim == 2:
        return _ef.point_inside(poly, float(x), float(y))
    if len(poly) == 0:
        return False
    px, py = poly[:, 0], poly[:, 1]
    xj, yj = np.roll(px, 1), np.roll(py, 1)
    dy = np.where(yj - py == 0.0, 1e-12, yj - py)
    cross = ((py > y) != (yj > y)) & (x < (xj - px) * (y - py) / dy + px)
    return bool(np.count_nonzero(cross) & 1)


def polyline_dist(line, pts):
    """Distance from each point in pts (n, 2) to the nearest segment of line (k, 2) -> (n,)."""
    line = np.ascontiguousarray(np.asarray(line, dtype=np.float64)[:, :2])
    pts = np.ascontiguousarray(np.asarray(pts, dtype=np.float64)[:, :2])
    if _ef is not None:
        return np.asarray(_ef.polyline_dist(line, pts))
    n = len(pts)
    if n == 0:
        return np.zeros(0, dtype=np.float64)
    if len(line) < 2:
        return np.full(n, np.inf, dtype=np.float64)
    a, b = line[:-1], line[1:]
    dx, dy = b[:, 0] - a[:, 0], b[:, 1] - a[:, 1]
    l2 = np.maximum(dx * dx + dy * dy, 1e-12)
    ax, ay = a[:, 0], a[:, 1]
    out = np.empty(n, dtype=np.float64)
    step = max(1, (1 << 20) // max(1, len(a)))
    for i in range(0, n, step):
        px = pts[i:i + step, 0, None]
        py = pts[i:i + step, 1, None]
        t = np.clip(((px - ax[None, :]) * dx[None, :] + (py - ay[None, :]) * dy[None, :]) / l2[None, :], 0.0, 1.0)
        d = np.hypot(ax[None, :] + dx[None, :] * t - px, ay[None, :] + dy[None, :] * t - py)
        out[i:i + step] = d.min(axis=1)
    return out


def seg_dist_min(line, px, py=None):
    """Minimum distance from point (px, py) (or p=(px, py)) to polyline line (k, >=2)."""
    if py is None:
        px, py = float(px[0]), float(px[1])
    else:
        px, py = float(px), float(py)
    line = np.asarray(line, dtype=np.float64)
    if _ef is not None and line.ndim == 2:
        return _ef.seg_dist_min(line, px, py)
    if len(line) < 2:
        return float("inf")
    a, b = line[:-1, :2], line[1:, :2]
    dx, dy = b[:, 0] - a[:, 0], b[:, 1] - a[:, 1]
    l2 = np.maximum(dx * dx + dy * dy, 1e-12)
    t = np.clip(((px - a[:, 0]) * dx + (py - a[:, 1]) * dy) / l2, 0.0, 1.0)
    return float(np.min(np.hypot(a[:, 0] + dx * t - px, a[:, 1] + dy * t - py)))


def island_ring_rays(poly, lines_hw, c, rays, step, rmax, step_r=None, rmax_r=None):
    """(rays, 2) island curb ring from centre c along `rays` directions in steps of `step` up to `rmax`.
    Accepts either (poly, lines_hw, c, rays, step, rmax) or (poly, segs, hw, cx, cy, rays, step, rmax)."""
    poly2 = np.ascontiguousarray(np.asarray(poly, dtype=np.float64)[:, :2])
    if step_r is not None and rmax_r is not None:
        segs = np.ascontiguousarray(lines_hw, dtype=np.float64)
        hw = np.ascontiguousarray(c, dtype=np.float64)
        cx, cy = float(rays), float(step)
        rays = int(rmax)
        step = float(step_r)
        rmax = float(rmax_r)
    else:
        segs, hw = segments(lines_hw)
        cx, cy = float(c[0]), float(c[1])
        rays = int(rays)
        step = float(step)
        rmax = float(rmax)
    if _ef is not None:
        return _ef.island_ring_rays(poly2, segs, hw, cx, cy, rays, step, rmax)
    if rays <= 0:
        return np.zeros((0, 2), dtype=np.float64)
    ang = 2.0 * math.pi * np.arange(rays, dtype=np.float64) / rays
    ux, uy = np.cos(ang), np.sin(ang)
    if step <= 0.0:
        return np.column_stack([np.full(rays, cx), np.full(rays, cy)])
    # Build the exact sequence of step radii produced by `r = 0.0; while r < rmax: ... r += step`
    r_list = [0.0]
    r = 0.0
    while r < rmax:
        r += step
        r_list.append(r)
    r_arr = np.asarray(r_list, dtype=np.float64)
    s_cnt = len(r_arr) - 1
    if s_cnt <= 0:
        return np.column_stack([np.full(rays, cx), np.full(rays, cy)])
    r_test = r_arr[:-1] + step
    qx = (cx + ux[:, None] * r_test[None, :]).ravel()
    qy = (cy + uy[:, None] * r_test[None, :]).ravel()
    pts = np.column_stack([qx, qy])
    ins = points_inside(poly2, pts)
    if len(segs):
        ax, ay, bx, by = segs[:, 0], segs[:, 1], segs[:, 2], segs[:, 3]
        dx, dy = bx - ax, by - ay
        l2 = np.maximum(dx * dx + dy * dy, 1e-12)
        near = np.zeros(len(pts), dtype=bool)
        chunk = max(1, (1 << 20) // len(segs))
        for i in range(0, len(pts), chunk):
            px = qx[i:i + chunk, None]
            py = qy[i:i + chunk, None]
            t = np.clip(((px - ax[None, :]) * dx[None, :] + (py - ay[None, :]) * dy[None, :]) / l2[None, :], 0.0, 1.0)
            d = np.hypot(ax[None, :] + dx[None, :] * t - px, ay[None, :] + dy[None, :] * t - py)
            near[i:i + chunk] = np.any(d <= hw[None, :], axis=1)
        bad = (~ins | near).reshape(rays, s_cnt)
    else:
        bad = (~ins).reshape(rays, s_cnt)
    # First step along each ray that exits the polygon or hits a road segment's half-width
    padded = np.empty((rays, s_cnt + 1), dtype=bool)
    padded[:, :s_cnt] = bad
    padded[:, s_cnt] = True
    stop_idx = np.argmax(padded, axis=1)
    r_final = r_arr[stop_idx]
    return np.column_stack([cx + ux * r_final, cy + uy * r_final])


def clear_of(segs, half, owner, px, py, own, margin):
    """Whether (px, py) is farther than half + margin from every segment not owned by `own`."""
    segs = np.ascontiguousarray(segs, dtype=np.float64)
    half = np.ascontiguousarray(half, dtype=np.float64)
    owner = np.ascontiguousarray(owner, dtype=np.int64)
    px, py, own, margin = float(px), float(py), int(own), float(margin)
    if _ef is not None:
        return _ef.clear_of(segs, half, owner, px, py, own, margin)
    if len(segs) == 0:
        return True
    m = owner != own
    if not np.any(m):
        return True
    s = segs[m]
    ax, ay = s[:, 0], s[:, 1]
    dx, dy = s[:, 2] - ax, s[:, 3] - ay
    l2 = np.maximum(dx * dx + dy * dy, 1e-12)
    t = np.clip(((px - ax) * dx + (py - ay) * dy) / l2, 0.0, 1.0)
    d = np.hypot(ax + dx * t - px, ay + dy * t - py)
    return bool(np.all(d >= half[m] + margin))


class Others:
    """furniture's (fid, half width, polyline) roads as segment arrays once, for clear() at every candidate."""

    def __init__(self, others):
        self.others = others
        ids = {}
        valid = []
        total = 0
        for fid, h, line in others:
            p = np.asarray(line, dtype=np.float64)[:, :2]
            n = len(p) - 1
            if n < 1:
                continue
            k = ids.setdefault(fid, len(ids))
            valid.append((p, n, float(h), k))
            total += n
        self.ids = ids
        if not total:
            self.segs = np.zeros((0, 4), dtype=np.float64)
            self.half = np.zeros(0, dtype=np.float64)
            self.owner = np.zeros(0, dtype=np.int64)
            self._dx = self._dy = self._l2 = np.zeros(0, dtype=np.float64)
            return
        segs = np.empty((total, 4), dtype=np.float64)
        half = np.empty(total, dtype=np.float64)
        owner = np.empty(total, dtype=np.int64)
        pos = 0
        for p, n, h, k in valid:
            nxt = pos + n
            segs[pos:nxt, :2] = p[:-1]
            segs[pos:nxt, 2:] = p[1:]
            half[pos:nxt] = h
            owner[pos:nxt] = k
            pos = nxt
        self.segs = segs
        self.half = half
        self.owner = owner
        dx = segs[:, 2] - segs[:, 0]
        dy = segs[:, 3] - segs[:, 1]
        self._dx = dx
        self._dy = dy
        self._l2 = np.maximum(dx * dx + dy * dy, 1e-12)

    def clear(self, p, own, margin):
        """Fast segment distance check (native kernel when available, vectorised numpy fallback otherwise)."""
        px, py = float(p[0]), float(p[1])
        own_id = self.ids.get(own, -1)
        margin = float(margin)
        if _ef is not None:
            return _ef.clear_of(self.segs, self.half, self.owner, px, py, own_id, margin)
        if len(self.segs) == 0:
            return True
        m = self.owner != own_id
        if not np.any(m):
            return True
        s = self.segs[m]
        dx, dy, l2 = self._dx[m], self._dy[m], self._l2[m]
        ax, ay = s[:, 0], s[:, 1]
        t = np.clip(((px - ax) * dx + (py - ay) * dy) / l2, 0.0, 1.0)
        d = np.hypot(ax + dx * t - px, ay + dy * t - py)
        return bool(np.all(d >= self.half[m] + margin))


def inside_blockers(pts, segs, starts, hw):
    """Which points (n, 2) lie within half width hw[j] of a segment of blocker j.
    segs (m, 4) ax ay bx by, starts (B + 1,) int64, hw (B,) float64."""
    pts = np.ascontiguousarray(np.asarray(pts, dtype=np.float64)[:, :2])
    segs = np.ascontiguousarray(segs, dtype=np.float64)
    starts = np.ascontiguousarray(starts, dtype=np.int64)
    hw = np.ascontiguousarray(hw, dtype=np.float64)
    if _ef is not None:
        return np.asarray(_ef.inside_blockers(pts, segs, starts, hw))
    n = len(pts)
    out = np.zeros(n, dtype=bool)
    if n == 0 or len(segs) == 0 or len(hw) == 0:
        return out
    counts = np.diff(starts)
    valid_b = counts > 0
    if not np.any(valid_b):
        return out
    seg_hw = np.repeat(hw[valid_b], counts[valid_b])
    if len(seg_hw) != len(segs):
        # Fallback if starts has non-monotonic or partial slices
        seg_parts, hw_parts = [], []
        for b in range(len(hw)):
            s0, s1 = int(starts[b]), int(starts[b + 1])
            if s1 > s0:
                seg_parts.append(segs[s0:s1])
                hw_parts.append(np.full(s1 - s0, hw[b], dtype=np.float64))
        if not seg_parts:
            return out
        segs = np.vstack(seg_parts)
        seg_hw = np.concatenate(hw_parts)
    x0, y0 = float(pts[:, 0].min()), float(pts[:, 1].min())
    x1, y1 = float(pts[:, 0].max()), float(pts[:, 1].max())
    ax, ay, bx, by = segs[:, 0], segs[:, 1], segs[:, 2], segs[:, 3]
    s_lox = np.minimum(ax, bx) - seg_hw
    s_hix = np.maximum(ax, bx) + seg_hw
    s_loy = np.minimum(ay, by) - seg_hw
    s_hiy = np.maximum(ay, by) + seg_hw
    keep_s = (s_hix >= x0) & (s_lox <= x1) & (s_hiy >= y0) & (s_loy <= y1)
    if not np.any(keep_s):
        return out
    ax, ay, bx, by = ax[keep_s], ay[keep_s], bx[keep_s], by[keep_s]
    seg_hw = seg_hw[keep_s]
    dx, dy = bx - ax, by - ay
    l2 = np.maximum(dx * dx + dy * dy, 1e-12)
    bx0, by0 = float(s_lox[keep_s].min()), float(s_loy[keep_s].min())
    bx1, by1 = float(s_hix[keep_s].max()), float(s_hiy[keep_s].max())
    cand = np.nonzero((pts[:, 0] >= bx0) & (pts[:, 0] <= bx1) & (pts[:, 1] >= by0) & (pts[:, 1] <= by1))[0]
    if len(cand) == 0:
        return out
    m = len(ax)
    step = max(1, (1 << 20) // m)
    for i in range(0, len(cand), step):
        idx = cand[i:i + step]
        px = pts[idx, 0, None]
        py = pts[idx, 1, None]
        t = np.clip(((px - ax[None, :]) * dx[None, :] + (py - ay[None, :]) * dy[None, :]) / l2[None, :], 0.0, 1.0)
        d = np.hypot(px - (ax[None, :] + t * dx[None, :]), py - (ay[None, :] + t * dy[None, :]))
        out[idx] = np.any(d < seg_hw[None, :], axis=1)
    return out


def _wall_rays_np(p, d, keys, first, counts, seg_of, segs, bucket, reach):
    """Vectorised numpy ray-wall intersection against Walls bucket arrays."""
    p = np.asarray(p, dtype=np.float64).reshape(-1, 2)
    d = np.asarray(d, dtype=np.float64).reshape(-1, 2)
    out = np.full(len(p), np.nan, dtype=np.float64)
    if not len(p) or not len(keys):
        return out
    q = p + d * reach
    bx0 = np.floor(np.minimum(p[:, 0], q[:, 0]) / bucket).astype(np.int64)
    bx1 = np.floor(np.maximum(p[:, 0], q[:, 0]) / bucket).astype(np.int64)
    by0 = np.floor(np.minimum(p[:, 1], q[:, 1]) / bucket).astype(np.int64)
    by1 = np.floor(np.maximum(p[:, 1], q[:, 1]) / bucket).astype(np.int64)
    nx, ny = bx1 - bx0 + 1, by1 - by0 + 1
    cnt = nx * ny
    total_b = int(cnt.sum())
    if total_b <= 0:
        return out
    ray = np.repeat(np.arange(len(p), dtype=np.int64), cnt)
    k = np.arange(total_b, dtype=np.int64) - np.repeat(np.cumsum(cnt) - cnt, cnt)
    nx_rep = np.repeat(nx, cnt)
    bx = np.repeat(bx0, cnt) + k % nx_rep
    by = np.repeat(by0, cnt) + k // nx_rep
    key = (bx + (1 << 30)) * (1 << 31) + (by + (1 << 30))
    i = np.minimum(np.searchsorted(keys, key), len(keys) - 1)
    found = keys[i] == key
    if not np.any(found):
        return out
    ray, i = ray[found], i[found]
    c = counts[i]
    total_s = int(c.sum())
    if total_s <= 0:
        return out
    ray = np.repeat(ray, c)
    j = np.arange(total_s, dtype=np.int64) - np.repeat(np.cumsum(c) - c, c) + np.repeat(first[i], c)
    s = segs[seg_of[j]]
    pr, dr = p[ray], d[ray]
    ax, ay = s[:, 0], s[:, 1]
    ex, ey = s[:, 2] - ax, s[:, 3] - ay
    den = dr[:, 0] * ey - dr[:, 1] * ex
    ok = np.abs(den) > 1e-12
    if not np.any(ok):
        return out
    ray, pr, dr, ax, ay, ex, ey, den = ray[ok], pr[ok], dr[ok], ax[ok], ay[ok], ex[ok], ey[ok], den[ok]
    wx, wy = ax - pr[:, 0], ay - pr[:, 1]
    inv_den = 1.0 / den
    t = (wx * ey - wy * ex) * inv_den
    u = (wx * dr[:, 1] - wy * dr[:, 0]) * inv_den
    hit = (t > 0.0) & (t <= reach) & (u >= 0.0) & (u <= 1.0)
    if not np.any(hit):
        return out
    best = np.full(len(p), np.inf, dtype=np.float64)
    np.minimum.at(best, ray[hit], t[hit])
    fin = np.isfinite(best)
    out[fin] = best[fin]
    return out


def measure_row(line, keys, first=None, counts=None, seg_of=None, segs=None, bucket=16.0, step=6.0,
                end_skip=10.0, reach=25.0, min_samples=4, walled=0.6, agree=0.3):
    """Right-of-way (m) of a street (line (k, >=2)) between building walls, or None.
    Accepts either (line, walls) or (line, keys, first, counts, seg_of, segs, bucket, step, ...)."""
    if first is None and hasattr(keys, "keys") and hasattr(keys, "segs"):
        walls = keys
        rs = walls.__dict__.get("_rs")
        if rs is None:
            rs = walls._rs = tuple(np.ascontiguousarray(a, t) for a, t in
                                   ((walls.keys, np.int64), (walls.first, np.int64), (walls.counts, np.int64),
                                    (walls.seg_of, np.int64), (walls.segs, np.float64)))
        keys, first, counts, seg_of, segs = rs
    else:
        keys = np.ascontiguousarray(keys, dtype=np.int64)
        first = np.ascontiguousarray(first, dtype=np.int64)
        counts = np.ascontiguousarray(counts, dtype=np.int64)
        seg_of = np.ascontiguousarray(seg_of, dtype=np.int64)
        segs = np.ascontiguousarray(segs, dtype=np.float64)
    xy = np.ascontiguousarray(np.asarray(line, dtype=np.float64)[:, :2])
    if len(xy) < 2 or len(keys) == 0:
        return None
    if _ef is not None:
        return _ef.measure_row(xy, keys, first, counts, seg_of, segs, float(bucket), float(step),
                               float(end_skip), float(reach), int(min_samples), float(walled), float(agree))
    dxy = np.diff(xy, axis=0)
    seg = np.hypot(dxy[:, 0], dxy[:, 1])
    s = np.empty(len(xy), dtype=np.float64)
    s[0] = 0.0
    np.cumsum(seg, out=s[1:])
    if s[-1] < 2.0 * end_skip + step:
        return None
    dd = np.arange(end_skip, s[-1] - end_skip + 1e-9, step, dtype=np.float64)
    k = np.clip(np.searchsorted(s, dd), 1, len(s) - 1)
    t = xy[k] - xy[k - 1]
    L = np.hypot(t[:, 0], t[:, 1])
    good = L >= 1e-6
    dd, t, L = dd[good], t[good], L[good]
    n = len(dd)
    if n == 0:
        return None
    inv_L = 1.0 / L
    nrm = np.column_stack([-t[:, 1] * inv_L, t[:, 0] * inv_L])
    c = np.column_stack([np.interp(dd, s, xy[:, 0]), np.interp(dd, s, xy[:, 1])])
    hits = _wall_rays_np(np.vstack([c, c]), np.vstack([nrm, -nrm]), keys, first, counts, seg_of, segs,
                         float(bucket), float(reach))
    w = hits[:n] + hits[n:]
    w = w[~np.isnan(w)]
    if len(w) < min_samples or len(w) / n < walled:
        return None
    med = float(np.median(w))
    q1, q3 = np.percentile(w, [25.0, 75.0])
    if (q3 - q1) / med > agree:
        return None
    return med


def grade_points(px, py, base, a, b=None, za=None, zb=None, h=None, lift=0.0, slope_run=2.0,
                 t_min=5.0, t_max=12.0, blend_r=4.0, bucket=32.0):
    """Grade base heights at (px, py) under road segments a-b (heights za-zb, core half width h) or plans list."""
    px = np.ascontiguousarray(px, dtype=np.float64)
    py = np.ascontiguousarray(py, dtype=np.float64)
    base = np.ascontiguousarray(base, dtype=np.float64)
    n = len(px)
    if n == 0:
        return base.copy()
    x0, y0, x1, y1 = float(px.min()), float(py.min()), float(px.max()), float(py.max())
    if za is None:
        # Called as grade_points(px, py, base, plans, lift=0.0)
        plans = a
        if b is not None:
            lift = float(b)
        valid = []
        total = 0
        for p in plans:
            if p.bbox[2] < x0 or p.bbox[0] > x1 or p.bbox[3] < y0 or p.bbox[1] > y1 or len(p.xy) < 2:
                continue
            m = len(p.xy) - 1
            valid.append((p, m))
            total += m
        if not total:
            return base.copy()
        A = np.empty((total, 2), dtype=np.float64)
        B = np.empty((total, 2), dtype=np.float64)
        ZA = np.empty(total, dtype=np.float64)
        ZB = np.empty(total, dtype=np.float64)
        H = np.empty(total, dtype=np.float64)
        pos = 0
        for p, m in valid:
            nxt = pos + m
            A[pos:nxt] = p.xy[:-1, :2]
            B[pos:nxt] = p.xy[1:, :2]
            ZA[pos:nxt] = p.z[:-1]
            ZB[pos:nxt] = p.z[1:]
            H[pos:nxt] = p.half
            pos = nxt
    else:
        A = np.ascontiguousarray(np.asarray(a, dtype=np.float64)[:, :2])
        B = np.ascontiguousarray(np.asarray(b, dtype=np.float64)[:, :2])
        ZA = np.ascontiguousarray(za, dtype=np.float64)
        ZB = np.ascontiguousarray(zb, dtype=np.float64)
        H = np.ascontiguousarray(h, dtype=np.float64)
    if len(H) == 0:
        return base.copy()
    if _ef is not None:
        return _ef.grade_points(px, py, base, A, B, ZA, ZB, H, float(lift), float(slope_run),
                                float(t_min), float(t_max), float(blend_r), float(bucket))
    out = base.copy()
    bx = np.floor((px - x0) / bucket).astype(np.int64)
    by = np.floor((py - y0) / bucket).astype(np.int64)
    nbx = int(bx.max()) + 1
    key = by * nbx + bx
    order = np.argsort(key, kind="stable")
    sk = key[order]
    reach = H + t_max
    lo_x = np.minimum(A[:, 0], B[:, 0]) - reach
    hi_x = np.maximum(A[:, 0], B[:, 0]) + reach
    lo_y = np.minimum(A[:, 1], B[:, 1]) - reach
    hi_y = np.maximum(A[:, 1], B[:, 1]) + reach
    near = ~((hi_x < x0) | (lo_x > x1) | (hi_y < y0) | (lo_y > y1))
    if not np.any(near):
        return out
    A, B, ZA, ZB, H = A[near], B[near], ZA[near], ZB[near], H[near]
    lo_x, hi_x, lo_y, hi_y = lo_x[near], hi_x[near], lo_y[near], hi_y[near]
    cx0 = np.maximum(0, np.floor((lo_x - x0) / bucket)).astype(np.int64)
    cx1 = np.minimum(nbx - 1, np.floor((hi_x - x0) / bucket)).astype(np.int64)
    cy0 = np.maximum(0, np.floor((lo_y - y0) / bucket)).astype(np.int64)
    cy1 = np.floor((hi_y - y0) / bucket).astype(np.int64)
    nrow = np.maximum(cy1 - cy0 + 1, 0)
    total_rows = int(nrow.sum())
    if total_rows <= 0:
        return out
    seg = np.repeat(np.arange(len(A), dtype=np.int64), nrow)
    row = np.repeat(cy0, nrow) + (np.arange(total_rows, dtype=np.int64) - np.repeat(np.cumsum(nrow) - nrow, nrow))
    i0 = np.searchsorted(sk, row * nbx + cx0[seg], "left")
    i1 = np.searchsorted(sk, row * nbx + cx1[seg], "right")
    cnt = np.maximum(i1 - i0, 0)
    keep = cnt > 0
    if not np.any(keep):
        return out
    seg, i0, cnt = seg[keep], i0[keep], cnt[keep]
    # Precompute per-segment deltas and lengths once instead of per (segment, point) pair
    ABx = B[:, 0] - A[:, 0]
    ABy = B[:, 1] - A[:, 1]
    L2 = ABx * ABx + ABy * ABy
    long_ = L2 > 1e-12
    safe_L2 = np.where(long_, L2, 1.0)
    DZ = ZB - ZA
    inv_blend_r = 1.0 / blend_r
    num = np.zeros(n, dtype=np.float64)
    den = np.zeros(n, dtype=np.float64)
    wmax = np.zeros(n, dtype=np.float64)
    ends = np.cumsum(cnt)
    start = 0
    while start < len(seg):
        stop = int(np.searchsorted(ends, (ends[start - 1] if start else 0) + _PAIR_BATCH, "right"))
        stop = max(stop, start + 1)
        c = cnt[start:stop]
        s_ = np.repeat(seg[start:stop], c)
        idx = order[np.repeat(i0[start:stop], c) + (np.arange(int(c.sum()), dtype=np.int64) - np.repeat(np.cumsum(c) - c, c))]
        abx, aby = ABx[s_], ABy[s_]
        qx, qy = px[idx] - A[s_, 0], py[idx] - A[s_, 1]
        t = np.where(long_[s_], np.clip((qx * abx + qy * aby) / safe_L2[s_], 0.0, 1.0), 0.0)
        d = np.hypot(qx - t * abx, qy - t * aby)
        target = ZA[s_] + DZ[s_] * t
        T = np.clip(slope_run * np.abs(target - base[idx]), t_min, t_max)
        u = np.clip((d - H[s_]) / T, 0.0, 1.0)
        m = u < 1.0
        if np.any(m):
            um = u[m]
            w = 1.0 - um * um * (3.0 - 2.0 * um)
            pos_w = w > 0.0
            if np.any(pos_w):
                if not np.all(pos_w):
                    w = w[pos_w]
                    i = idx[m][pos_w]
                    dm = d[m][pos_w]
                    tm = target[m][pos_w]
                else:
                    i = idx[m]
                    dm = d[m]
                    tm = target[m]
                r = dm * inv_blend_r
                wb = w / (1.0 + r * r)
                num += np.bincount(i, wb * tm, n)
                den += np.bincount(i, wb, n)
                np.maximum.at(wmax, i, w)
        start = stop
    g = den > 0
    out[g] = base[g] + wmax[g] * (num[g] / den[g] - base[g] + lift)
    return out


def contains_points(ring, pts):
    """Which of pts (M, 2) lie inside the polygon ring (N, 2) (even-odd crossings of a ray toward +x)."""
    ring = np.ascontiguousarray(np.asarray(ring, dtype=np.float64).reshape(-1, 2))
    pts = np.ascontiguousarray(np.asarray(pts, dtype=np.float64).reshape(-1, 2))
    if _ef is not None and hasattr(_ef, "contains_points"):
        return np.asarray(_ef.contains_points(ring, pts))
    out = np.zeros(len(pts), dtype=bool)
    if len(ring) < 3 or len(pts) == 0:
        return out
    x0, y0 = ring[:, 0], ring[:, 1]
    x1, y1 = np.concatenate((x0[1:], x0[:1])), np.concatenate((y0[1:], y0[:1]))
    keep = y0 != y1
    x0, y0, x1, y1 = x0[keep], y0[keep], x1[keep], y1[keep]
    if len(x0) == 0:
        return out
    lo = np.minimum(y0, y1)
    hi = np.maximum(y0, y1)
    slope = (x1 - x0) / (y1 - y0)
    bx0, bx1 = float(ring[:, 0].min()), float(ring[:, 0].max())
    by0, by1 = float(ring[:, 1].min()), float(ring[:, 1].max())
    cand = np.nonzero((pts[:, 0] >= bx0) & (pts[:, 0] <= bx1) & (pts[:, 1] >= by0) & (pts[:, 1] <= by1))[0]
    if len(cand) == 0:
        return out
    nb = int(min(_BANDS, max(1, len(x0) // 4)))
    h = (by1 - by0) / nb or 1.0
    band = np.minimum(((pts[cand, 1] - by0) / h).astype(np.int64), nb - 1)
    e0 = np.clip(((lo - by0) / h).astype(np.int64), 0, nb - 1)
    e1 = np.clip(((hi - by0) / h).astype(np.int64), 0, nb - 1)
    order = np.argsort(band, kind="stable")
    cuts = np.searchsorted(band[order], np.arange(nb + 1))
    for b in range(nb):
        if cuts[b] == cuts[b + 1]:
            continue
        idx = cand[order[cuts[b]:cuts[b + 1]]]
        es = np.nonzero((e0 <= b) & (e1 >= b))[0]
        if len(es) == 0:
            continue
        ex0, ey0, elo, ehi, esl = x0[es], y0[es], lo[es], hi[es], slope[es]
        step = max(1, (1 << 22) // len(es))
        for i in range(0, len(idx), step):
            j = idx[i:i + step]
            px = pts[j, 0, None]
            py = pts[j, 1, None]
            span = (py >= elo) & (py < ehi)
            xc = ex0 + (py - ey0) * esl
            out[j] = (np.count_nonzero(span & (xc > px), axis=1) & 1).astype(bool)
    return out


def shared_walls(coords, offs, cents, rads, order, tol=0.6):
    """Per ring, the number of other rings with at least two vertices within tol of one of its edges."""
    coords = np.ascontiguousarray(coords, dtype=np.float64)
    offs = np.ascontiguousarray(offs, dtype=np.int64)
    cents = np.ascontiguousarray(cents, dtype=np.float64)
    rads = np.ascontiguousarray(rads, dtype=np.float64)
    order = np.ascontiguousarray(order, dtype=np.int64)
    tol = float(tol)
    if _ef is not None and hasattr(_ef, "shared_walls"):
        return np.asarray(_ef.shared_walls(coords, offs, cents, rads, order, tol), dtype=np.int64)
    n = len(rads)
    cnt = np.zeros(n, dtype=np.int64)
    if n < 2:
        return cnt
    xs = cents[order, 0]
    for i in order:
        i = int(i)
        lo = int(np.searchsorted(xs, cents[i, 0] - rads[i] - 30.0))
        hi = int(np.searchsorted(xs, cents[i, 0] + rads[i] + 30.0))
        if hi <= lo:
            continue
        ia, ib = int(offs[i]), int(offs[i + 1])
        ri = coords[ia:ib]
        b = np.concatenate((ri[1:], ri[:1]), axis=0)
        d = b - ri
        l2 = np.maximum((d * d).sum(axis=1), 1e-9)
        for j in order[lo:hi]:
            j = int(j)
            if j == i or math.hypot(cents[j, 0] - cents[i, 0], cents[j, 1] - cents[i, 1]) > rads[i] + rads[j] + tol:
                continue
            rj = coords[int(offs[j]):int(offs[j + 1])]
            t = np.clip(((rj[:, None, :] - ri[None, :, :]) * d[None, :, :]).sum(axis=2) / l2[None, :], 0.0, 1.0)
            q = ri[None, :, :] + t[..., None] * d[None, :, :]
            diff = rj[:, None, :] - q
            dist = np.hypot(diff[..., 0], diff[..., 1])
            if np.count_nonzero(np.any(dist < tol, axis=1)) >= 2:
                cnt[i] += 1
    return cnt


def tangents(v, uv, nrm, idx):
    """Per-vertex tangents (xyz, w) by Lengyel's method."""
    v = np.ascontiguousarray(v, dtype=np.float64)
    uv = np.ascontiguousarray(uv, dtype=np.float64)
    nrm = np.ascontiguousarray(nrm, dtype=np.float64)
    idx = np.ascontiguousarray(idx, dtype=np.int64)
    if _ef is not None and hasattr(_ef, "tangents"):
        return _ef.tangents(v, uv, nrm, idx)
    i0, i1, i2 = idx[:, 0], idx[:, 1], idx[:, 2]
    e1, e2 = v[i1] - v[i0], v[i2] - v[i0]
    d1, d2 = uv[i1] - uv[i0], uv[i2] - uv[i0]
    den = d1[:, 0] * d2[:, 1] - d2[:, 0] * d1[:, 1]
    r = np.zeros_like(den)
    np.divide(1.0, den, out=r, where=np.abs(den) > 0)
    sdir = (e1 * d2[:, 1:2] - e2 * d1[:, 1:2]) * r[:, None]
    tdir = (e2 * d1[:, 0:1] - e1 * d2[:, 0:1]) * r[:, None]
    flat_idx = np.concatenate([i0, i1, i2])
    n = len(v)
    t1 = np.stack([np.bincount(flat_idx, np.tile(sdir[:, c], 3), n) for c in range(3)], axis=1)
    t2 = np.stack([np.bincount(flat_idx, np.tile(tdir[:, c], 3), n) for c in range(3)], axis=1)
    dot_nt1 = (nrm[:, 0] * t1[:, 0] + nrm[:, 1] * t1[:, 1] + nrm[:, 2] * t1[:, 2])[:, None]
    t = t1 - nrm * dot_nt1
    tl = np.sqrt(t[:, 0] * t[:, 0] + t[:, 1] * t[:, 1] + t[:, 2] * t[:, 2])
    for axis in (0, 1):
        bad = tl < 1e-12
        if not np.any(bad):
            break
        e = np.zeros(3, dtype=np.float64)
        e[axis] = 1.0
        t[bad] = e - nrm[bad] * nrm[bad, axis:axis + 1]
        tl[bad] = np.sqrt(t[bad, 0] * t[bad, 0] + t[bad, 1] * t[bad, 1] + t[bad, 2] * t[bad, 2])
    t /= np.where(tl > 0.0, tl, 1.0)[:, None]
    cr0 = nrm[:, 1] * t[:, 2] - nrm[:, 2] * t[:, 1]
    cr1 = nrm[:, 2] * t[:, 0] - nrm[:, 0] * t[:, 2]
    cr2 = nrm[:, 0] * t[:, 1] - nrm[:, 1] * t[:, 0]
    s = cr0 * t2[:, 0] + cr1 * t2[:, 1] + cr2 * t2[:, 2]
    out = np.empty((n, 4), dtype=np.float64)
    out[:, :3] = t
    out[:, 3] = np.where(s < 0.0, -1.0, 1.0)
    return out


def densify(pts, spacing):
    """Nodes added so no two are more than `spacing` m apart in xy."""
    pts = np.ascontiguousarray(pts, dtype=np.float64)
    spacing = float(spacing)
    if _ef is not None and pts.ndim == 2 and pts.shape[1] >= 2 and len(pts):
        return _ef.densify(pts, spacing)
    if len(pts) < 2:
        return pts.copy()
    d = np.diff(pts[:, :2], axis=0)
    n = np.ceil(np.hypot(d[:, 0], d[:, 1]) / spacing).astype(np.int64)
    cnt = np.maximum(1, n)
    if np.all(cnt == 1):
        return pts.copy()
    total = int(cnt.sum())
    seg_idx = np.repeat(np.arange(len(cnt), dtype=np.int64), cnt)
    ends = np.cumsum(cnt)
    k = np.arange(total, dtype=np.int64) - np.repeat(ends - cnt, cnt) + 1
    denom = np.maximum(1, n[seg_idx]).astype(np.float64)
    f = (k / denom)[:, None]
    a = pts[seg_idx]
    b = pts[seg_idx + 1]
    out = np.empty((1 + total, pts.shape[1]), dtype=np.float64)
    out[0] = pts[0]
    out[1:] = a + (b - a) * f
    out[ends] = pts[1:]
    return out


def resample(xy, step):
    """Points every `step` m along xy (k, 2), the last point kept, and their arc lengths."""
    xy = np.ascontiguousarray(np.asarray(xy, dtype=np.float64)[:, :2])
    step = float(step)
    if _ef is not None and len(xy):
        return _ef.resample(xy, step)
    if len(xy) < 2:
        return xy[:1].copy(), np.zeros(1, dtype=np.float64)
    d = np.diff(xy, axis=0)
    seg = np.hypot(d[:, 0], d[:, 1])
    s = np.empty(len(xy), dtype=np.float64)
    s[0] = 0.0
    np.cumsum(seg, out=s[1:])
    if s[-1] <= 0.0:
        return xy[:1].copy(), np.zeros(1, dtype=np.float64)
    t = np.r_[np.arange(0.0, s[-1], step, dtype=np.float64), s[-1]]
    return np.column_stack([np.interp(t, s, xy[:, 0]), np.interp(t, s, xy[:, 1])]), t


def smooth_profile(z, step, sigma):
    """Gaussian smoothing of a profile sampled every `step` m (reflected at the ends)."""
    z = np.ascontiguousarray(z, dtype=np.float64)
    step, sigma = float(step), float(sigma)
    if _ef is not None:
        return _ef.smooth_profile(z, step, sigma)
    if len(z) < 3 or sigma <= 0.0:
        return z.copy()
    r = int(math.ceil(3.0 * sigma / step))
    k = np.exp(-0.5 * (np.arange(-r, r + 1, dtype=np.float64) * step / sigma) ** 2)
    k /= k.sum()
    pad = np.pad(z, r, mode="reflect") if len(z) > r else np.pad(z, r, mode="edge")
    return np.convolve(pad, k, mode="valid")


def dual_side(a, b, gap_min=4.0, gap_max=45.0, cos_par=0.9063077870366499):
    """(share of a's ~10 m samples with b beside it, mean side of b) for polylines a (k, 2), b (m, 2)."""
    a = np.ascontiguousarray(np.asarray(a, dtype=np.float64)[:, :2])
    b = np.ascontiguousarray(np.asarray(b, dtype=np.float64)[:, :2])
    gap_min, gap_max, cos_par = float(gap_min), float(gap_max), float(cos_par)
    if _ef is not None:
        return _ef.dual_side(a, b, gap_min, gap_max, cos_par)
    if len(a) < 2 or len(b) < 2:
        return 0.0, 0.0
    seg = np.diff(a, axis=0)
    L = np.hypot(seg[:, 0], seg[:, 1])
    total = float(L.sum())
    if total < 1.0:
        return 0.0, 0.0
    s = np.empty(len(a), dtype=np.float64)
    s[0] = 0.0
    np.cumsum(L, out=s[1:])
    n = max(2, int(total // 10.0) + 1)
    t = np.linspace(0.0, total, n)
    q = np.column_stack([np.interp(t, s, a[:, 0]), np.interp(t, s, a[:, 1])])
    k = np.clip(np.searchsorted(s, t, side="right") - 1, 0, len(seg) - 1)
    ta = seg[k] / np.maximum(L[k], 1e-9)[:, None]
    bs = np.diff(b, axis=0)
    bl2 = np.maximum(bs[:, 0] * bs[:, 0] + bs[:, 1] * bs[:, 1], 1e-12)
    b0 = b[:-1]
    f = np.clip(
        ((q[:, None, 0] - b0[None, :, 0]) * bs[None, :, 0] + (q[:, None, 1] - b0[None, :, 1]) * bs[None, :, 1])
        / bl2[None, :],
        0.0,
        1.0,
    )
    nx = b0[None, :, 0] + bs[None, :, 0] * f
    ny = b0[None, :, 1] + bs[None, :, 1] * f
    d = np.hypot(nx - q[:, None, 0], ny - q[:, None, 1])
    j = np.argmin(d, axis=1)
    rows = np.arange(n)
    dist = d[rows, j]
    tb = bs[j] / np.sqrt(bl2[j])[:, None]
    par = np.abs(ta[:, 0] * tb[:, 0] + ta[:, 1] * tb[:, 1]) >= cos_par
    ok = (dist >= gap_min) & (dist <= gap_max) & par
    if not np.any(ok):
        return 0.0, 0.0
    vx = nx[rows[ok], j[ok]] - q[ok, 0]
    vy = ny[rows[ok], j[ok]] - q[ok, 1]
    side = np.sign(ta[ok, 0] * vy - ta[ok, 1] * vx)
    return float(ok.mean()), float(side.mean())


def deck_crossings(pts, starts):
    """(i, j, arc on i, arc on j) where polylines i < j cross away from their endpoints."""
    pts = np.ascontiguousarray(np.asarray(pts, dtype=np.float64)[:, :2])
    starts = np.ascontiguousarray(starts, dtype=np.int64)
    if _ef is not None:
        return _ef.deck_crossings(pts, starts)
    nl = max(0, len(starts) - 1)
    lines = [pts[int(starts[i]):int(starts[i + 1])] for i in range(nl)]
    boxes = [(ln.min(axis=0), ln.max(axis=0)) if len(ln) >= 2 else None for ln in lines]
    arcs = []
    for ln in lines:
        if len(ln) >= 2:
            d = np.diff(ln, axis=0)
            s = np.empty(len(ln), dtype=np.float64)
            s[0] = 0.0
            np.cumsum(np.hypot(d[:, 0], d[:, 1]), out=s[1:])
            arcs.append(s)
        else:
            arcs.append(None)
    out = []
    for i in range(nl):
        if boxes[i] is None:
            continue
        alo, ahi = boxes[i]
        A = lines[i]
        P, r = A[:-1], A[1:] - A[:-1]
        si_end = float(arcs[i][-1])
        for j in range(i + 1, nl):
            if boxes[j] is None:
                continue
            blo, bhi = boxes[j]
            if ahi[0] < blo[0] or bhi[0] < alo[0] or ahi[1] < blo[1] or bhi[1] < alo[1]:
                continue
            B = lines[j]
            sj_end = float(arcs[j][-1])
            for q in range(len(B) - 1):
                a, d = B[q], B[q + 1] - B[q]
                den = r[:, 0] * d[1] - r[:, 1] * d[0]
                ok = np.abs(den) > 1e-12
                if not np.any(ok):
                    continue
                ap = a - P
                sden = np.where(ok, den, 1.0)
                t = np.where(ok, (ap[:, 0] * d[1] - ap[:, 1] * d[0]) / sden, -1.0)
                u = np.where(ok, (ap[:, 0] * r[:, 1] - ap[:, 1] * r[:, 0]) / sden, -1.0)
                for k in np.nonzero(ok & (t > 1e-6) & (t < 1.0 - 1e-6) & (u > 1e-6) & (u < 1.0 - 1e-6))[0]:
                    k = int(k)
                    si = float(arcs[i][k] + t[k] * (arcs[i][k + 1] - arcs[i][k]))
                    sj = float(arcs[j][q] + u[k] * (arcs[j][q + 1] - arcs[j][q]))
                    if min(si, si_end - si, sj, sj_end - sj) > 1.0:
                        out.append((i, j, si, sj))
    return out


def loop_crossed(poly, pts, margin=2.0):
    """Whether road pts (k, >=2) runs through the inside of loop poly (n, 2) farther than `margin` from its ring."""
    poly = np.ascontiguousarray(np.asarray(poly, dtype=np.float64)[:, :2])
    pts = np.ascontiguousarray(np.asarray(pts, dtype=np.float64)[:, :2])
    margin = float(margin)
    if _ef is not None and hasattr(_ef, "loop_crossed"):
        return _ef.loop_crossed(poly, pts, margin)
    if len(poly) == 0 or len(pts) < 2:
        return False
    lo, hi = poly.min(axis=0) - 1.0, poly.max(axis=0) + 1.0
    if (pts.max(axis=0) < lo).any() or (pts.min(axis=0) > hi).any():
        return False
    d = np.diff(pts, axis=0)
    counts = np.maximum(1, (np.hypot(d[:, 0], d[:, 1]) / 2.0).astype(np.int64))
    total = int(counts.sum())
    seg_idx = np.repeat(np.arange(len(counts), dtype=np.int64), counts)
    s = np.arange(total, dtype=np.float64) - np.repeat(np.cumsum(counts) - counts, counts)
    t = ((s + 0.5) / counts[seg_idx])[:, None]
    samples = pts[seg_idx] + d[seg_idx] * t
    ins = points_inside(poly, samples)
    if not np.any(ins):
        return False
    ring = np.vstack([poly, poly[:1]])
    return bool(np.any(polyline_dist(ring, samples[ins]) > margin))


def any_within(pts, line, h):
    """Whether any point in pts (m, 2) lies closer than h to polyline line (k, >=2)."""
    pts = np.ascontiguousarray(np.asarray(pts, dtype=np.float64)[:, :2])
    line = np.ascontiguousarray(np.asarray(line, dtype=np.float64)[:, :2])
    h = float(h)
    if _ef is not None and hasattr(_ef, "any_within"):
        return _ef.any_within(pts, line, h)
    if len(pts) == 0 or len(line) < 2:
        return False
    return bool(np.any(polyline_dist(line, pts) < h))

