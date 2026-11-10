"""New York's rooftop water tanks found on an orthophoto (tools/build_nyc_tanks.py; numpy and PIL only).

The NYS ITS 2024 orthoimagery of the city (orthos.its.ny.gov, the "newyork" regional source; winter leaf-off, long
shadows, near true-ortho: the city's photogrammetric footprints sit on the roofs to ~0.04 m of lean per m of height)
shows a cedar tank's top as a tan disc 2.5-6 m across (often in the dark square of its steel platform), a metal-topped
one as a bright grey cone. Two channels, then the roofs:

  tan     a cedar-colour likelihood (chromaticity r, g near TAN_RG, brightness 105-200) and a disc-minus-ring matched
          filter over radii (tan_candidates); each candidate's rim then fitted (ring_best: the radial edges' coverage of
          the circle and their strength against the edges inside / outside it) and the features scored by a logistic
          model (LR_*: fitted on hand marks of 11 test areas, 2026-11-10)
  metal   a gradient-direction circular Hough transform (hough) for bright grey discs with a near-perfect rim (METAL)
  roofs   a tank counts on a building's roof only (the city's footprints, leaned by the photo's relief displacement:
          estimate_lean), of MIN_H or more (a strong rim needs no height: the footprints' heights are wrong at times)

Hand-checked 2026-11-10 on 15 areas of 250 m, 104 tanks (the builder's docstring): precision 0.95, recall 0.73; missed
are mostly grey or dark tops in their platform's shadow. All sizes are in pixels at MPP (0.1 m)."""
import math

import numpy as np

MPP = 0.1
TAN_RG, TAN_SRG = (0.383, 0.328), (0.016, 0.009)     # cedar tops' chromaticity r, g and spread
TAN_L = (105.0, 200.0)
TAN_RADII = tuple(range(12, 32, 2))
RING_RADII = (12, 14, 16, 18, 20, 22, 25, 28, 32)
PRE = dict(ts=0.45, el=1.6, fill0=0.5, fill1=1.3, to=0.35)      # the loose pre-filter before the logistic score
RING_TAN_MAX = 0.2         # the tan share of the ring round an accepted disc at most (a tan deck or box goes on)
LR_KEYS = ("ts", "to", "bk", "bcov", "el", "fill", "td", "li")
LR_MU = (0.5760, 0.1706, 1.1000, 0.7513, 1.2003, 0.9189, 0.6954, 156.53)
LR_SD = (0.1003, 0.0911, 0.6570, 0.1612, 0.1294, 0.1158, 0.0830, 15.760)
LR_W = (1.2469, -0.9712, 1.2800, 0.7895, 0.4479, -0.3307, 0.7599, 0.4328)
LR_B = -1.5585
LR_T = 0.8
METAL = dict(rb=15.0, li=150.0, cov=0.85, k=2.6, dl=35.0, r0=13, r1=28)
STRONG = dict(bk=2.5, bcov=0.95)
MIN_H = 12.0                # m: the roof's height (a tank on a lower roof needs a STRONG rim)
TANK_TOP_M = 5.0            # m: a tank's top over its roof (its own extra lean)
ROOF_TOL = 15.0             # px: a tank this near a roof's outline is still on it
NMS = 28                    # px: two detections closer than this are one


# --- image basics -------------------------------------------------------------------------------------------------
def box_blur(a, r):
    if r <= 0:
        return a
    k = 2 * r + 1
    p = np.pad(a, ((r + 1, r), (0, 0)), mode="edge")
    c = np.cumsum(p, axis=0, dtype=np.float64)
    a = ((c[k:] - c[:-k]) / k).astype(np.float32)
    p = np.pad(a, ((0, 0), (r + 1, r)), mode="edge")
    c = np.cumsum(p, axis=1, dtype=np.float64)
    return ((c[:, k:] - c[:, :-k]) / k).astype(np.float32)


def gblur(a, sigma):
    """~Gaussian blur (three box blurs)."""
    r = max(int(round(math.sqrt(12 * sigma * sigma / 3 + 1) / 2 - 0.5)), 0) if sigma > 0 else 0
    for _ in range(3):
        a = box_blur(a, r)
    return a


def sobel(L):
    p = np.pad(L, 1, mode="edge")
    gx = (p[:-2, 2:] + 2 * p[1:-1, 2:] + p[2:, 2:]) - (p[:-2, :-2] + 2 * p[1:-1, :-2] + p[2:, :-2])
    gy = (p[2:, :-2] + 2 * p[2:, 1:-1] + p[2:, 2:]) - (p[:-2, :-2] + 2 * p[:-2, 1:-1] + p[:-2, 2:])
    return gx / 8.0, gy / 8.0


def maxfilt(a, r):
    out = a.copy()
    for d in range(1, r + 1):
        out[d:] = np.maximum(out[d:], a[:-d])
        out[:-d] = np.maximum(out[:-d], a[d:])
    b = out.copy()
    for d in range(1, r + 1):
        out[:, d:] = np.maximum(out[:, d:], b[:, :-d])
        out[:, :-d] = np.maximum(out[:, :-d], b[:, d:])
    return out


class Img:
    """An RGB picture (H, W, 3) at MPP with its luminance and gradients."""

    def __init__(self, rgb):
        self.rgb = np.asarray(rgb, np.float32)
        self.L = self.rgb @ np.array([0.299, 0.587, 0.114], np.float32)
        self.gx, self.gy = sobel(gblur(self.L, 1.2))
        self.mag = np.hypot(self.gx, self.gy)
        self.H, self.W = self.L.shape


# --- the rim of a disc ----------------------------------------------------------------------------------------------
def ring_stats(im, cx, cy, r, nsec=36):
    """cov: the share of nsec sectors with a radial edge within r +- 2 px; er / ein / eout: the radial edge strength at
    r / 0.75 r / 1.3 r; li, lo: mean luminance inside 0.75 r / in the ring 1.15-1.5 r; rb: inside R - B."""
    R = int(math.ceil(1.6 * r)) + 3
    x0, x1, y0, y1 = max(cx - R, 0), min(cx + R + 1, im.W), max(cy - R, 0), min(cy + R + 1, im.H)
    if x1 - x0 < 3 or y1 - y0 < 3:
        return dict(cov=0.0, li=0.0, lo=0.0, rb=0.0, er=0.0, ein=1.0, eout=1.0)
    yy, xx = np.mgrid[y0:y1, x0:x1]
    dx, dy = xx - cx, yy - cy
    d = np.hypot(dx, dy)
    gx, gy, mag = im.gx[y0:y1, x0:x1], im.gy[y0:y1, x0:x1], im.mag[y0:y1, x0:x1]
    with np.errstate(invalid="ignore", divide="ignore"):
        cosr = np.nan_to_num(np.abs(gx * dx + gy * dy) / (mag * d))
    band = (np.abs(d - r) <= 2.0) & (mag > 5.0) & (cosr > 0.85)
    ang = ((np.arctan2(dy, dx) + math.pi) / (2 * math.pi) * nsec).astype(int) % nsec
    cov = len(np.unique(ang[band])) / nsec
    inside, ring = d <= 0.75 * r, (d >= 1.15 * r) & (d <= 1.5 * r)
    L, rgb = im.L[y0:y1, x0:x1], im.rgb[y0:y1, x0:x1]
    em = mag * cosr

    def at(rr):
        b = np.abs(d - rr) <= 1.0
        return float(em[b].mean()) if b.any() else 0.0
    return dict(cov=cov, li=float(L[inside].mean()) if inside.any() else 0.0,
                lo=float(L[ring].mean()) if ring.any() else 0.0,
                rb=float((rgb[..., 0] - rgb[..., 2])[inside].mean()) if inside.any() else 0.0,
                er=at(r), ein=at(0.75 * r), eout=at(1.3 * r))


def rim_roundness(im, cx, cy, r, nsec=24):
    """How round the rim round (cx, cy) is: per sector the distance (0.6-1.5 r) of its strongest radial edge, their
    spread (p80 - p20) over their median. A disc ~0.1, a square ~0.25 (its sides 1 to 1.41 apart); 9.0: no rim."""
    R = int(math.ceil(1.5 * r)) + 2
    x0, x1, y0, y1 = max(cx - R, 0), min(cx + R + 1, im.W), max(cy - R, 0), min(cy + R + 1, im.H)
    yy, xx = np.mgrid[y0:y1, x0:x1]
    dx, dy = xx - cx, yy - cy
    d = np.hypot(dx, dy)
    with np.errstate(invalid="ignore", divide="ignore"):
        e = np.nan_to_num(np.abs(im.gx[y0:y1, x0:x1] * dx + im.gy[y0:y1, x0:x1] * dy) / d)
    ok = (d >= 0.6 * r) & (d <= 1.5 * r)
    sec = ((np.arctan2(dy, dx) + math.pi) / (2 * math.pi) * nsec).astype(int) % nsec
    rad = []
    for s in range(nsec):
        m = ok & (sec == s)
        if m.any() and e[m].max() > 5.0:
            rad.append(float(d[m][np.argmax(e[m])]))
    if len(rad) < nsec // 2:
        return 9.0
    p20, p50, p80 = np.percentile(rad, [20, 50, 80])
    return float((p80 - p20) / max(p50, 1e-6))


def ring_quality(s):
    return s["er"] / (0.5 * (s["ein"] + s["eout"]) + 2.0)


def ring_best(im, x, y, r):
    """The best rim near (x, y): (quality x coverage, coverage, radius px)."""
    best = (0.0, 0.0, float(r))
    for rr in RING_RADII:
        if rr < 0.8 * r:
            continue
        for dx, dy in ((0, 0), (3, 0), (-3, 0), (0, 3), (0, -3)):
            s = ring_stats(im, x + dx, y + dy, rr)
            k = ring_quality(s) * s["cov"]
            if k > best[0]:
                best = (k, s["cov"], float(rr))
    return best


# --- the tan channel ------------------------------------------------------------------------------------------------
def tan_map(rgb):
    rgb = np.asarray(rgb, np.float32)
    s = rgb.sum(2) + 1.0
    r, g, L = rgb[..., 0] / s, rgb[..., 1] / s, s / 3.0
    t = np.exp(-0.5 * (((r - TAN_RG[0]) / TAN_SRG[0]) ** 2 + ((g - TAN_RG[1]) / TAN_SRG[1]) ** 2))
    t *= np.clip((L - (TAN_L[0] - 20)) / 20, 0, 1) * np.clip(((TAN_L[1] + 20) - L) / 20, 0, 1)
    return gblur(t.astype(np.float32), 0.8)


def _kernel(shape, r_in, r0=None, r1=None):
    H, W = shape
    yy, xx = np.fft.fftfreq(H) * H, np.fft.fftfreq(W) * W
    d = np.hypot(yy[:, None], xx[None, :])
    k = (d <= r_in) if r0 is None else ((d >= r0) & (d <= r1))
    k = k.astype(np.float32)
    return np.fft.rfft2(k / k.sum())


def blob_shape(T, x, y, r, t=0.35):
    """The tan region round (x, y): its area over pi r^2 and its elongation."""
    H, W = T.shape
    R = int(1.6 * r) + 2
    x0, x1, y0, y1 = max(x - R, 0), min(x + R + 1, W), max(y - R, 0), min(y + R + 1, H)
    yy, xx = np.nonzero(T[y0:y1, x0:x1] > t)
    xx, yy = xx + x0 - x, yy + y0 - y
    near = np.hypot(xx, yy) <= 1.1 * r
    if near.sum() < 10:
        return 0.0, 9.0
    a = near.sum() / (math.pi * r * r)
    c = np.cov(np.vstack([xx[near] - xx[near].mean(), yy[near] - yy[near].mean()]))
    ev = np.linalg.eigvalsh(c)
    return float(a), float(math.sqrt(max(ev[1], 1e-6) / max(ev[0], 1e-6)))


def tan_candidates(rgb, t=0.18, nms=14):
    """[{x, y, r, ts, td, to, fill, el}]: the tan discs (disc mean minus 0.7 ring mean of the tan map, best radius)."""
    T = tan_map(rgb)
    F = np.fft.rfft2(T)
    best = np.full(T.shape, -1.0, np.float32)
    br, bd, bo = (np.zeros(T.shape, np.float32) for _ in range(3))
    for r in TAN_RADII:
        dm = np.fft.irfft2(F * _kernel(T.shape, 0.85 * r), s=T.shape).astype(np.float32)
        om = np.fft.irfft2(F * _kernel(T.shape, 0, 1.15 * r, 1.45 * r), s=T.shape).astype(np.float32)
        sc = dm - 0.7 * om
        u = sc > best
        best[u], br[u], bd[u], bo[u] = sc[u], r, dm[u], om[u]
    ys, xs = np.nonzero((best >= maxfilt(best, nms)) & (best > t))
    out = []
    for x, y in zip(xs.tolist(), ys.tolist()):
        r = float(br[y, x])
        fill, el = blob_shape(T, x, y, r)
        out.append(dict(x=x, y=y, r=r, ts=float(best[y, x]), td=float(bd[y, x]), to=float(bo[y, x]), fill=fill,
                        el=el))
    return out


def lr_p(f):
    z = sum(w * (f[k] - m) / s for k, m, s, w in zip(LR_KEYS, LR_MU, LR_SD, LR_W)) + LR_B
    return 1.0 / (1.0 + math.exp(-z))


# --- the metal channel ----------------------------------------------------------------------------------------------
def hough(im, r0=13, r1=32, edge_t=6.0):
    """Per pixel the best circle score over radii r0..r1 px (edge votes along the gradient, either sign, over 2 pi r)
    and its radius."""
    H, W = im.H, im.W
    ys, xs = np.nonzero(im.mag > edge_t)
    m = im.mag[ys, xs]
    ux, uy = im.gx[ys, xs] / m, im.gy[ys, xs] / m
    w = np.minimum(m, 4 * edge_t) / (4 * edge_t)
    best = np.zeros((H, W), np.float32)
    bestr = np.zeros((H, W), np.float32)
    for r in range(r0, r1 + 1):
        acc = np.zeros(H * W, np.float64)
        for s in (1.0, -1.0):
            cx = np.rint(xs + s * r * ux).astype(np.int64)
            cy = np.rint(ys + s * r * uy).astype(np.int64)
            ok = (cx >= 0) & (cx < W) & (cy >= 0) & (cy < H)
            acc += np.bincount(cy[ok] * W + cx[ok], weights=w[ok], minlength=H * W)
        sc = gblur(acc.reshape(H, W).astype(np.float32), 1.0) * 9.0 / (2 * math.pi * r)
        u = sc > best
        best[u], bestr[u] = sc[u], r
    return best, bestr


def metal_candidates(im, t=0.22, nms=12):
    sc, rad = hough(im)
    ys, xs = np.nonzero((sc >= maxfilt(sc, nms)) & (sc > t))
    out = []
    for x, y in zip(xs.tolist(), ys.tolist()):
        r = float(rad[y, x])
        if not METAL["r0"] <= r <= METAL["r1"]:
            continue
        s = ring_stats(im, x, y, r)
        k = ring_quality(s) * s["cov"]
        if abs(s["rb"]) <= METAL["rb"] and s["li"] >= METAL["li"] and s["cov"] >= METAL["cov"] and k >= METAL["k"] \
                and s["li"] - s["lo"] >= METAL["dl"]:
            out.append(dict(x=x, y=y, r=r, k=k))
    return out


# --- the roofs ------------------------------------------------------------------------------------------------------
def point_in_ring(x, y, ring):
    xs, ys = ring[:, 0], ring[:, 1]
    j = np.roll(np.arange(len(ring)), 1)
    c = ((ys > y) != (ys[j] > y)) & (x < (xs[j] - xs) * (y - ys) / (ys[j] - ys + 1e-12) + xs)
    return bool(c.sum() % 2)


def dist_to_ring(x, y, ring):
    a, b = ring, np.roll(ring, -1, axis=0)
    d = b - a
    t = np.clip(((x - a[:, 0]) * d[:, 0] + (y - a[:, 1]) * d[:, 1]) / ((d ** 2).sum(1) + 1e-12), 0, 1)
    return float(np.hypot(a[:, 0] + t * d[:, 0] - x, a[:, 1] + t * d[:, 1] - y).min())


def boundary_samples(rings, step=5.0):
    pts, nrm = [], []
    for r in rings:
        for a, b in zip(r, np.roll(r, -1, axis=0)):
            d = b - a
            L = math.hypot(*d)
            if L < 1e-6:
                continue
            k = max(int(L // step), 1)
            for t in (np.arange(k) + 0.5) / k:
                pts.append(a + t * d)
                nrm.append(np.array([-d[1], d[0]]) / L)
    return pts, nrm


def estimate_lean(im, blds, vmax=6.0, step=0.2, min_h=12.0):
    """The photo's lean v (px of image shift per m of height): the shift of every roof outline (blds: [(rings px,
    height m)]) by h * v that puts them best on the image's edges. (0, 0) without tall buildings."""
    P, N, Hh = [], [], []
    for rings, h in blds:
        if h < min_h:
            continue
        p, n = boundary_samples(rings)
        P += p
        N += n
        Hh += [h] * len(p)
    if len(P) < 50:
        return np.zeros(2)
    P, N, Hh = np.asarray(P), np.asarray(N), np.asarray(Hh)
    vs = np.arange(-vmax, vmax + 1e-9, step)
    best, bv = -1.0, np.zeros(2)
    for vy in vs:
        for vx in vs:
            q = P + Hh[:, None] * np.array([vx, vy])
            xi, yi = np.rint(q[:, 0]).astype(int), np.rint(q[:, 1]).astype(int)
            ok = (xi >= 1) & (xi < im.W - 1) & (yi >= 1) & (yi < im.H - 1)
            if not ok.any():
                continue
            g = np.abs(im.gx[yi[ok], xi[ok]] * N[ok, 0] + im.gy[yi[ok], xi[ok]] * N[ok, 1]).mean()
            if g > best:
                best, bv = g, np.array([vx, vy])
    return bv


class Roofs:
    """The footprints in the picture's pixels: [(rings [(k, 2) px], height m)], leaned by v (px per m)."""

    def __init__(self, blds, v):
        self.blds = blds
        self.v = np.asarray(v, np.float64)
        self.bb = [(np.min([r.min(0) for r in rs], 0), np.max([r.max(0) for r in rs], 0)) for rs, _h in blds]

    def find(self, x, y, tol=ROOF_TOL):
        """(index, height m) of the highest roof that holds the point (or is within tol), or None."""
        best = None
        for i, (rs, h) in enumerate(self.blds):
            q = np.array([x, y]) - (h + TANK_TOP_M) * self.v
            lo, hi = self.bb[i]
            if q[0] < lo[0] - tol or q[0] > hi[0] + tol or q[1] < lo[1] - tol or q[1] > hi[1] + tol:
                continue
            d = 0.0 if any(point_in_ring(q[0], q[1], r) for r in rs) else min(dist_to_ring(q[0], q[1], r) for r in rs)
            if d <= tol and (best is None or (d, -h) < (best[2], -best[1])):
                best = (i, h, d)
        return best and best[:2]


# --- all together ---------------------------------------------------------------------------------------------------
def detect(rgb, blds, lean=None):
    """Tanks on the picture rgb (H, W, 3 at MPP) over buildings blds [(rings px, height m)]: [{x, y, r, kind, p, h,
    bld}] (x, y: the tank top on the picture, px; r px; h: the roof's height; bld: the index in blds)."""
    im = Img(rgb)
    v = estimate_lean(im, blds) if lean is None else np.asarray(lean, np.float64)
    roofs = Roofs(blds, v)
    out = []
    for c in tan_candidates(rgb):
        if not (c["ts"] >= PRE["ts"] and c["el"] <= PRE["el"] and PRE["fill0"] <= c["fill"] <= PRE["fill1"]
                and c["to"] <= PRE["to"]):
            continue
        bk, bcov, br = ring_best(im, c["x"], c["y"], c["r"])
        s = ring_stats(im, c["x"], c["y"], c["r"])
        p = lr_p(dict(c, bk=bk, bcov=bcov, li=s["li"]))
        if p < LR_T or c["to"] > RING_TAN_MAX:
            continue
        f = roofs.find(c["x"], c["y"])
        if f is None or (f[1] < MIN_H and not (bk >= STRONG["bk"] and bcov >= STRONG["bcov"])):
            continue
        out.append(dict(x=c["x"], y=c["y"], r=max(c["r"], br), kind="tan", p=p, bld=f[0], h=f[1]))
    for c in metal_candidates(im):
        f = roofs.find(c["x"], c["y"])
        if f is None or f[1] < MIN_H:
            continue
        out.append(dict(x=c["x"], y=c["y"], r=c["r"], kind="metal", p=min(c["k"] / 4.0, 1.0), bld=f[0], h=f[1]))
    out.sort(key=lambda d: (d["kind"] != "tan", -d["p"]))
    keep = []
    for d in out:
        if all(math.hypot(d["x"] - o["x"], d["y"] - o["y"]) > NMS for o in keep):
            keep.append(d)
    return keep, v


def ground_point(d, v):
    """The tank's place on the map (px): its top on the photo less its lean (roof height + its own)."""
    return d["x"] - (d["h"] + TANK_TOP_M) * v[0], d["y"] - (d["h"] + TANK_TOP_M) * v[1]
