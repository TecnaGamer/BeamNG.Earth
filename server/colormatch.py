"""Colour-seam removal in the USGS imagery: the "seam feather" (docs/research/2026-10-10-imagery-colormatch.md
§3 method, §7 integration plan; --colormatch off|s2).

USGS ImageryOnly is a mosaic of NAIP flights from different years and seasons, flown per state, so
state lines and other flight boundaries are sharp colour steps (Monument Valley on the AZ/UT line:
dE 17.6). A smooth colour transfer cannot remove a sharp step, so the feather finds the steps first:
long, straight-ish steps of the ratio USGS / Sentinel-2 that S2 does not have (natural edges are in
both images, survey seams only in USGS), measures each one's local step and moves each side half way,
faded out with the distance to the seam (SIGMA_F_M). Away from seams the gain is exactly 1: nothing is
pulled toward S2's palette (EOX S2 is far more saturated than NAIP; S2 is only the reference).

All maths in linear light, per channel, on the z14 pixel grid ("cells", 4 x 4 z16 px, ~7.6 m at 37 N:
EOX S2 cloudless at its native zoom, pixel-aligned with the USGS z14 tiles). Two stages per USGS z12
tile ("region", 1024² cells, ~7.8 km at 37 N), each a pure function of source tiles, so a place comes
out the same whatever order its regions were computed in, from whichever anchor:
  1. seams (detect_region, <cache>/colormatch/v1/usgs/12/x/y.seams.npz): the region's z14 USGS and
     S2 tiles with a HALO-cell ring of neighbouring tiles (detection needs >= 1 km around the core).
     Reliability weights (deep shadow / water, snow or cloud in one source only, IRLS against a 30 m
     robust blur: cast shadows differ between NAIP and S2), an oriented step filter on a 15 m grid
     (36 angles, 5 segments of 240 m that must agree, S2 gate), NMS, hysteresis and a component filter
     (>= 1.5 km, consistent step vector: a flight seam has one gain ratio along its length, crop-change
     field edges do not; USGS carries the step on the 300 m cut blurs: a step in S2 only, e.g. at the
     edge of the per-tile 2016 fallback, is not imported). Kept per seam cell of the core: the normal,
     the local step (cut blur of the ratio at SIGMA_S_M, sampled 4 cells either side) and the
     sub-pixel line, fitted on the z16 USGS profile across the seam (z16 tiles fetched only along
     the seam). No seam: a ".seams.none" marker.
  2. field (feather_field, <cache>/colormatch/v1/usgs/12/x/y.npz): from the seams of the region and
     its 8 neighbours (the fade reaches FADE_CUT <= one region), on a 30 m grid (COARSE cells, one z12
     pixel): the half-steps scattered on each side, extended along the sides with the cut blur (300 m;
     1500 m where the short one has no support), faded with a Gaussian of the distance to the seam
     (SIGMA_F_M, tapered to 0 at FADE_CUT), clamped to +-log 2. Stored: that log gain over the core
     (+PADC samples, float16), and the band within BAND samples of a seam: per cell its nearest seam
     cell, per seam cell its line and both sides' gains. No seam within reach: a ".none" marker.
Per source tile (z12..z16, Field.apply, called from imagery.Imagery._mosaic): the gain bilinear from
the 30 m field; within the band each pixel takes its side's gain from its signed distance to the
sub-pixel line (2 z16 px ramp); at z15/z16 the strip |d| <= 1.5 z16 px on the line (the mosaic's own
blend row and JPEG ringing, which no gain fixes) is inpainted along the normal from d = +-2.5 px,
with the neighbouring tiles as margin where the strip comes near the edge. A tile with no seam within
reach is returned unchanged (the common case, ~0 ms); pixels with gain 1 keep their exact bytes.
z <= 11 source tiles span several regions and stay raw (a documented follow-up).

Order with --deshadow (§6): raw -> de-shadow -> colour gain -> tone LUT. The seam analysis runs on
the raw imagery (de-shadow's model needs the source radiometry; cast shadows are outliers of the
robust ratio anyway, and a gain from sunlit cells is right for the relit tile), and the gain is
applied to the relit tile (and the inpaint margin is taken from the relit neighbours); imagery
keeps the corrected tiles in a second LRU keyed by the input (raw or relit), the raw tiles stay
untouched.

Regions are computed by one background thread, nearest the anchor first (queued at an anchor within
PREP_M, and by prefetch / render for the tiles they need, within REQUEST_M). Until a region is ready
its tiles render raw; render() reports the regions it lacked (pending=) so the server can rebuild
those textures once they are ready (earthd: the listener marks the far tiles stale). Tiles of regions
beyond REQUEST_M render raw without being reported (nothing is queued for them). A region's imagery
(z14 / z16 USGS and the S2 references) is fetched as netfetch kind "background", as the de-shadow
fits are: dropped while the host's bucket is kept for demand (a cold start), and the region is tried
again after RETRY_S with what was fetched so far cached. The inpaint margin of a tile being rendered
is demand (render's own fetch). The calls earthd
makes on its event loop (prepare, request, status) never decode a field (stat calls only); tile
workers read fields from disk without holding the lock.
"""
import heapq
import json
import logging
import math
import os
import threading
import time
from collections import OrderedDict

import numpy as np

import mercator

import fast
import pacer
import staging
from PIL import Image

import cache as cachemod
from deshadow import SRGB2LIN, down, lonlat_of_px, luma, to_srgb

log = logging.getLogger("earthd.colormatch")

VERSION = 1                         # bump: seams and fields are recomputed (the cache path holds it)
MODES = ("off", "s2")
SOURCE = "usgs"
REFS = ("eox2023", "eox2016")       # the reference, per z14 tile: S2 cloudless 2023, else 2016
TILE = 256
REGION_Z = 12                       # one seam set and field per z12 tile
GRID_Z = 14                         # analysis grid = the z14 pixel grid (S2's native resolution)
HALO = TILE                         # cells of neighbouring z14 tiles around a region for the detection
COARSE = 4                          # cells per field sample (30 m: one z12 pixel)
PADC = 2                            # field samples stored around a region's core (tile bilinear margin)
BAND = 2                            # field samples around a seam whose pixels take their side's gain
SIDE = 3.5                          # ... sampled this many field samples from the line (outside the cut)
MAX_Z = 16
INPAINT_Z = 15                      # the strip on the line is inpainted at z15 and z16
EARTH_CIRC = 40075016.686
EPS = 0.004                         # linear-light floor (log of deep shadow / water)
NOOP = 0.002                        # |log gain| below this everywhere in a tile: unchanged
# The feather (§3, §7: "s2" = sigma_s 300 m, sigma_f 1500 m, split 0.5, clamp +-log 2)
SIGMA_S_M = 300.0                   # local step: cut blur of the ratio
SIGMA_F_M = 1500.0                  # fade of the half-steps with the distance to the seam
FADE_CUTS = 4.0                     # the fade is tapered to 0 at FADE_CUTS sigma_f (at most one region)
SPLIT = 0.5                         # each side moves half way
CLAMP = math.log(2.0)
# Detection
DET_F = 2                           # detection grid: 2 cells (15 m)
SEG_M, NSEG, DEPTH_M = 240.0, 5, 45.0   # tested line: 5 segments of 240 m, 45 m bands each side
N_ANG = 36                          # 5 deg
THR, HYST_LOW = 0.12, 0.35          # hysteresis 0.12 / 0.042
GATE = 1.5                          # S2 gate: |D_s2| - 0.03 < GATE |D|
MIN_SHARE = 0.5                     # share of a component's step USGS carries (~1; a step in S2 only ~0)
AGREE = 0.75                        # segments that must agree
MIN_LEN_M = 1500.0
MAX_INCONS = 0.3                    # step vector consistency along a component
DET_TAU = 0.25                      # IRLS (log units)
STEP_CELLS = 4                      # local step sampled this many cells either side of a seam cell
# Sub-pixel line (z16 px)
LINE_F = 4                          # z16 px per cell
LINE_ALONG, LINE_ACROSS, LINE_R = 24, 12, 16
RAMP = 1.0                          # z16 px: side ramp half-width on the line
STRIP, REACH = 1.5, 2.5             # z16 px: inpainted strip, and where its samples come from
MARGIN = 6                          # px of neighbouring tiles for the inpaint samples
# Scheduling (as deshadow)
PREP_M = 8000.0                     # regions queued at an anchor
REQUEST_M = 24000.0                 # regions further than this from the anchor stay raw
RETRY_S = 60.0
PATIENT_S, PATIENT_STEP_S = 180.0, 1.0   # a region's dropped background fetches: wait, retry (_bg_tiles)
MEM_REGIONS = 24                    # decoded fields and seam sets kept in memory
EMPTY = "empty"                     # a field with no seam within reach (every tile unchanged)
PENDING = "pending"

_LUT16 = to_srgb(np.arange(65536) / 65535.0)     # linear (16 bit) -> sRGB


# --- filters -----------------------------------------------------------------------------------
def _rf_pass(X, cut_prev, a):
    """In place recursive filter along axis 1 of X [rows, cols, ch], forward and backward;
    cut_prev[r, c] True = no link between c - 1 and c."""
    fast.require(1, "rf_pass")
    Y = X if X.dtype == np.float32 and X.flags.c_contiguous and X.flags.writeable else np.ascontiguousarray(X, np.float32)
    pacer.pace()
    fast._ef.rf_pass(Y, np.ascontiguousarray(cut_prev, bool), float(a))     # (Rust)
    pacer.pace()
    if Y is not X:
        X[...] = Y


def dt_blur(X, sigma, cut=None, iters=3):
    """Edge-aware blur (domain transform, recursive filter) of X [h, w, ch] with spatial sigma in
    cells; cut: bool [h, w] cells that block propagation (every link touching one is cut)."""
    X = X.astype(np.float32).copy()
    h, w = X.shape[:2]
    if cut is None:
        cut = np.zeros((h, w), bool)
    cut_h = np.zeros((h, w), bool)
    cut_h[:, 1:] = cut[:, 1:] | cut[:, :-1]
    cut_v = np.zeros((w, h), bool)
    cut_v[:, 1:] = cut.T[:, 1:] | cut.T[:, :-1]
    for i in range(iters):
        pacer.pace()
        s = sigma * math.sqrt(3) * 2 ** (iters - i - 1) / math.sqrt(4 ** iters - 1)
        a = math.exp(-math.sqrt(2) / s)
        _rf_pass(X, cut_h, a)
        Xt = np.ascontiguousarray(X.transpose(1, 0, 2))
        _rf_pass(Xt, cut_v, a)
        X = np.ascontiguousarray(Xt.transpose(1, 0, 2))
    return X


def dilate(m, r=1):
    """8-neighbour dilation, r steps."""
    out = m.copy()
    for _ in range(r):
        g = out.copy()
        g[1:] |= out[:-1]
        g[:-1] |= out[1:]
        g[:, 1:] |= out[:, :-1]
        g[:, :-1] |= out[:, 1:]
        g[1:, 1:] |= out[:-1, :-1]
        g[:-1, :-1] |= out[1:, 1:]
        g[1:, :-1] |= out[:-1, 1:]
        g[:-1, 1:] |= out[1:, :-1]
        out = g
    return out


def fill(C, mask, iters=64):
    """Cells under mask take the mean of their unmasked 4-neighbours, repeatedly (the cut cells'
    gain: only used inside the band, where pixels take their side's gain anyway)."""
    C = C.copy()
    m = mask.copy()
    for _ in range(iters):
        pacer.pace()
        if not m.any():
            break
        acc = np.zeros_like(C)
        cnt = np.zeros(C.shape[:2], np.float32)
        for sl_dst, sl_src in (((slice(1, None),), (slice(None, -1),)), ((slice(None, -1),), (slice(1, None),)),
                               ((slice(None), slice(1, None)), (slice(None), slice(None, -1))),
                               ((slice(None), slice(None, -1)), (slice(None), slice(1, None)))):
            ok = ~m[sl_src]
            acc[sl_dst] += C[sl_src] * ok[..., None]
            cnt[sl_dst] += ok
        upd = m & (cnt > 0)
        C[upd] = acc[upd] / cnt[upd][:, None]
        m &= ~upd
    return C


# --- weights and the ratio ---------------------------------------------------------------------
def base_weights(U, S, valid=None, rows=256):
    """Reliability of a cell for the colour ratio before the robust reweighting: deep shadow and
    water (min luminance < 0.006) and snow / cloud in one source only (bright and grey) get 0.02,
    cells without data in either source 0. In chunks of `rows` rows (pacer: short busy slices)."""
    def grey_bright(x, lum):
        sat = (x.max(-1) - x.min(-1)) / np.maximum(x.max(-1), 1e-4)
        return (lum > 0.45) & (sat < 0.15)
    w = np.empty(U.shape[:2], np.float32)
    for r0 in range(0, U.shape[0], rows):
        pacer.pace()
        u, s_ = U[r0:r0 + rows], S[r0:r0 + rows]
        lu, ls = luma(u), luma(s_)
        wc = np.clip((np.minimum(lu, ls) - 0.006) / 0.02, 0.02, 1).astype(np.float32)
        wc[grey_bright(u, lu) ^ grey_bright(s_, ls)] = 0.02
        w[r0:r0 + rows] = wc
    if valid is not None:
        w *= valid
    return w


def smooth_ratio(U, S, w0, sigma, cut=None, robust=2, tau=0.25, usgs=False):
    """(Lsm, w): log ratio USGS / S2 of edge-aware weighted blurs, with IRLS outlier rejection
    (residuals against the blur, tau in log units). usgs: also the log of the weighted USGS blur
    alone, (Lsm, w, Lu) (the weight blurred as one more channel: the others are unchanged)."""
    L = np.log(U + EPS) - np.log(S + EPS)
    w = w0.copy()
    for it in range(robust + 1):
        pacer.pace()
        last = usgs and it == robust
        X = np.concatenate([U * w[..., None], S * w[..., None]] + ([w[..., None]] if last else []), -1)
        B = dt_blur(X, sigma, cut)
        Lsm = np.log(B[..., :3] + EPS * 0.25) - np.log(B[..., 3:6] + EPS * 0.25)
        if it < robust:
            r = np.sqrt(((L - Lsm) ** 2).mean(-1))
            w = w0 * np.exp(-0.5 * (r / tau) ** 2)
    if usgs:
        return Lsm, w, np.log(B[..., :3] / np.maximum(B[..., 6:], 1e-6) + EPS)
    return Lsm, w


# --- seam detection ----------------------------------------------------------------------------
def _rot(a, deg):
    return np.asarray(Image.fromarray(np.ascontiguousarray(a, np.float32), "F").rotate(deg, Image.BILINEAR, expand=True))


def _unrot(a, deg, shape):
    b = _rot(a, -deg)
    h, w = shape
    H, W = b.shape
    y0, x0 = (H - h) // 2, (W - w) // 2
    return b[y0:y0 + h, x0:x0 + w]


def normals(ang):
    """Unit normal (nx, ny) in image coordinates of the step filter rotated by ang (rad)."""
    return -np.sin(ang), np.cos(ang)


def _seam_pass(arrs, half, depth, offs, nseg, gate):
    """detect_seams' step test for one rotation: arrs = rotated [w, w*L_0 .. w*L_5] (float32 [H, W]); returns
    (score [H, W] float32, [Dm_0, Dm_1, Dm_2]) (Rust: earthfast.seam_pass)."""
    full = 0.3 * depth * (2 * half + 1)
    fast.require(1, "seam_pass")
    score, Dm = fast._ef.seam_pass(np.ascontiguousarray(np.stack(arrs), np.float32), half, depth,
                                   [int(o) for o in offs], full, gate, AGREE, max(2, nseg - 2))
    return score, list(Dm)

def _seams_gpu(w, Lw, n_ang, half, depth, offs, nseg, gate):
    """detect_seams' angle loop on the GPU (earthfast.seam_detect_gpu: the same rotations, sums, medians and gate in
    float32; the CPU path's sums are float64, so a few borderline cells differ), or None: no GPU (or BNGE_GPU=0),
    or it failed (logged once; the CPU path then)."""
    global _GPU_FAILED
    if _GPU_FAILED or not fast.gpu():
        return None
    try:
        src = np.ascontiguousarray(np.concatenate([w[None], np.moveaxis(Lw, -1, 0)]), np.float32)
        return fast._ef.seam_detect_gpu(src, n_ang, half, depth, [int(o) for o in offs],
                                        0.3 * depth * (2 * half + 1), gate, AGREE, max(2, nseg - 2))
    except Exception as e:
        _GPU_FAILED = True
        log.warning("colormatch: GPU seam detection failed (%s); the CPU from now on", e)
        return None


_GPU_FAILED = False


def detect_seams(Lu, Ls, w, seg=16, nseg=NSEG, depth=3, n_ang=N_ANG, thr=THR, gate=GATE, min_len=100):
    """Long, consistent steps of the log ratio that S2 does not explain. Lu, Ls: log linear USGS /
    S2 [h, w, 3] on the detection grid; w: cell weights. The step is measured on nseg consecutive
    segments of seg cells along each candidate line (depth cells each side, a 1-cell gap); score =
    |median step| if >= AGREE of the segments agree (cos > 0.7, >= half the magnitude) and S2 has no
    comparable step there. Returns (seam mask after NMS, hysteresis and the component filter, normal
    angle [rad], step vector D [h, w, 3] = side(+n) - side(-n), score, component stats)."""
    L = np.concatenate([Lu, Ls], -1).astype(np.float32)
    h, wd = L.shape[:2]
    best = np.zeros((h, wd), np.float32)
    ang = np.zeros((h, wd), np.float32)
    Dbest = np.zeros((h, wd, 3), np.float32)
    half = seg // 2
    offs = [int(round((k - (nseg - 1) / 2) * seg)) for k in range(nseg)]
    Lw = L * w[..., None]
    gpu = _seams_gpu(w, Lw, n_ang, half, depth, offs, nseg, gate)
    if gpu is not None:                 # every angle at once on the GPU (~0.2 s, the CPU path ~13 s a region)
        best, ang, Dbest = gpu
    for k in range(n_ang if gpu is None else 0):
        pacer.pace()
        deg = 180.0 * k / n_ang
        arrs = [_rot(w, deg)]
        for c in range(6):
            pacer.pace()
            arrs.append(_rot(Lw[..., c], deg))
        pacer.pace()
        score_r, Dm = _seam_pass(arrs, half, depth, offs, nseg, gate)
        pacer.pace()
        score_o = _unrot(score_r, deg, (h, wd))
        m = score_o > best
        if m.any():
            best[m] = score_o[m]
            ang[m] = math.radians(deg)
            for c in range(3):
                Dbest[..., c][m] = _unrot(Dm[c], deg, (h, wd))[m]
    nx, ny = normals(ang)
    yy, xx = np.mgrid[0:h, 0:wd]
    keep = np.ones((h, wd), bool)
    for sgn in (1, -1):                 # NMS along the normal
        pacer.pace()
        y2 = np.clip(np.rint(yy + sgn * ny).astype(int), 0, h - 1)
        x2 = np.clip(np.rint(xx + sgn * nx).astype(int), 0, wd - 1)
        keep &= best >= best[y2, x2]
    nms = keep & (best > thr * HYST_LOW)
    pacer.pace()
    seam = _hysteresis(nms, nms & (best > thr))
    pacer.pace()
    seam, stats = _drop_small(seam, min_len, best, thr, Dbest, MAX_INCONS)
    return seam, ang, Dbest, best, stats


def _hysteresis(weak, strong):
    lab = strong.copy()
    for _ in range(400):
        pacer.pace()
        g = dilate(lab) & weak
        if (g == lab).all():
            break
        lab = g
    return lab


def _components(mask):
    """(ys, xs, root per cell) of the 8-connected components of mask (union-find)."""
    ys, xs = np.nonzero(mask)
    idx = {(y, x): i for i, (y, x) in enumerate(zip(ys.tolist(), xs.tolist()))}
    parent = list(range(len(ys)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for (y, x), i in idx.items():
        pacer.pace()
        for dy, dx in ((0, 1), (1, -1), (1, 0), (1, 1)):
            j = idx.get((y + dy, x + dx))
            if j is not None:
                a, b = find(i), find(j)
                if a != b:
                    parent[max(a, b)] = min(a, b)
    return ys, xs, np.array([find(i) for i in range(len(ys))], np.int64)


def _drop_small(mask, min_cells, score, min_score, D, max_incons):
    """Keep the components that span >= min_cells (bounding-box extent), have a mean score >=
    min_score and a consistent step vector along their length (median |D - median D| / |median D|
    <= max_incons: a flight seam has the same gain ratio everywhere, crop-change field edges do
    not). Returns (mask, [stats of the kept components])."""
    ys, xs, roots = _components(mask)
    out = np.zeros_like(mask)
    stats = []
    if len(ys) == 0:
        return out, stats
    u, inv = np.unique(roots, return_inverse=True)
    keep = np.zeros(len(u), bool)
    for k in range(len(u)):
        pacer.pace()
        m = inv == k
        ext = max(ys[m].max() - ys[m].min(), xs[m].max() - xs[m].min()) + 1
        if ext < min_cells:
            continue
        ms = float(score[ys[m], xs[m]].mean())
        Dc = D[ys[m], xs[m]]
        md = np.median(Dc, 0)
        inc = float(np.median(np.linalg.norm(Dc - md, axis=1)) / max(float(np.linalg.norm(md)), 1e-6))
        if ms >= min_score and inc <= max_incons:
            keep[k] = True
            stats.append(dict(cells=int(m.sum()), extent=int(ext), score=round(ms, 3), incons=round(inc, 2),
                              D=[round(float(v), 3) for v in md]))
    sel = keep[inv]
    out[ys[sel], xs[sel]] = True
    return out, stats


# --- sub-pixel line ----------------------------------------------------------------------------
def seam_lines(ys, xs, ang, D, fine, F=LINE_F, along=LINE_ALONG, across=LINE_ACROSS, R=LINE_R, fit=None):
    """Sub-cell line of each seam cell (ys, xs window cells, ang the normal angle, D the step):
    the offset (cells, along the normal from the cell centre) of the step in the full-resolution
    USGS profile across the seam (fine(py, px) -> log linear [..., 3] at z16 pixel index coordinates
    of the window, NaN without data): change point, then the centroid of the derivative (re-centred
    twice: texture pulls a fixed window), then a trimmed mean of the neighbours' points within R
    cells, projected on this cell's normal. fit: bool per cell, the cells whose own profile is
    fitted (others only take part as neighbours: NaN).
    Returns the offsets (NaN where no neighbour had data)."""
    n_c = len(ys)
    nx, ny = normals(ang)
    cy, cx = F * ys + (F - 1) / 2, F * xs + (F - 1) / 2
    best_off = np.full(n_c, np.nan)
    sel = np.arange(n_c) if fit is None else np.nonzero(fit)[0]
    t = np.arange(-along, along + 1, 2.0)
    k = np.arange(-across, across + 1, 1.0)
    nk = len(k)
    for i0 in range(0, len(sel), 4096):
        s = sel[i0:i0 + 4096]
        py = cy[s, None, None] + k[None, :, None] * ny[s, None, None] + t[None, None, :] * nx[s, None, None]
        px = cx[s, None, None] + k[None, :, None] * nx[s, None, None] - t[None, None, :] * ny[s, None, None]
        P = fine(py, px).mean(2)                            # [cells, k, 3] profile across the seam
        good = np.isfinite(P).all((1, 2))
        d = D[s]
        dn = d / np.maximum(np.linalg.norm(d, axis=-1, keepdims=True), 1e-6)
        proj = np.where(good[:, None], (np.nan_to_num(P) * dn[:, None, :]).sum(-1), 0.0)
        c = np.concatenate([np.zeros((len(s), 1)), np.cumsum(proj, 1)], 1)
        bs, bo = np.full(len(s), -1e9), np.zeros(len(s))
        for split in range(4, nk - 3):                  # the step between k[split - 1] and k[split]
            ma = c[:, split] / split
            mb = (c[:, nk] - c[:, split]) / (nk - split)
            sc = (mb - ma) * math.sqrt(split * (nk - split) / nk)
            better = sc > bs
            bs[better] = sc[better]
            bo[better] = k[split] - 0.5
        g = np.diff(proj, axis=1)                       # at k[j] + 0.5
        kc = k[:-1] + 0.5
        for win in (3.0, 2.5, 2.5):
            gw = np.where(np.abs(kc[None, :] - bo[:, None]) <= win, np.maximum(g, 0), 0)
            sw = gw.sum(1)
            bo = np.where(sw > 1e-6, (gw * kc[None, :]).sum(1) / np.maximum(sw, 1e-9), bo)
        best_off[s] = np.where(good, bo, np.nan)
    # neighbours' points (within R cells) projected on this cell's normal, trimmed mean (the per-cell
    # fits are bimodal by +-0.5 px with the cell centre's phase; a median picks one mode)
    y0, x0 = int(ys.min()) - R, int(xs.min()) - R
    H, W = int(ys.max()) - y0 + R + 1, int(xs.max()) - x0 + R + 1
    gy = np.full((H, W), np.nan)
    gx = np.full((H, W), np.nan)
    gy[ys - y0, xs - x0] = cy + best_off * ny
    gx[ys - y0, xs - x0] = cx + best_off * nx
    off = np.full(n_c, np.nan)
    for i0 in range(0, n_c, 2048):
        s = slice(i0, i0 + 2048)
        yy, xx = ys[s] - y0, xs[s] - x0
        nb = np.stack([(gy[yy + dy, xx + dx] - cy[s]) * ny[s] + (gx[yy + dy, xx + dx] - cx[s]) * nx[s]
                       for dy in range(-R, R + 1) for dx in range(-R, R + 1)], 1)
        have = np.isfinite(nb).any(1)
        nbh = nb[have]
        lo, hi = np.nanpercentile(nbh, 25, 1), np.nanpercentile(nbh, 75, 1)
        keep = (nbh >= lo[:, None]) & (nbh <= hi[:, None])
        o = np.full(len(yy), np.nan)
        o[have] = np.nansum(np.where(keep, nbh, 0), 1) / np.maximum(keep.sum(1), 1)
        off[s] = o
    return off / F


# --- stage 1: a region's seams ------------------------------------------------------------------
def detect_region(U, S, valid, cell_m, core, fine=None):
    """Seams of a window's core. U, S: linear USGS / S2 [h, w, 3] float32 on the cell grid; valid:
    bool [h, w], both sources have data; core: (y0, y1, x0, x1) window cells whose seam cells are
    kept; fine: the z16 sampler of seam_lines (None: lines through the cell centres).
    Returns dict(y, x (window cells), ang, D, step, ok, off, stats); no seam: y is empty."""
    pacer.pace()
    w0 = base_weights(U, S, valid)
    pacer.pace()
    _, w = smooth_ratio(U, S, w0, 4.0, None, robust=1, tau=DET_TAU)
    f = DET_F
    dc = cell_m * f
    sm, an, Dd, _, stats = detect_seams(np.log(down(U, f) + EPS), np.log(down(S, f) + EPS), down(w, f),
                                        seg=max(4, int(round(SEG_M / dc))), depth=max(2, int(round(DEPTH_M / dc))),
                                        min_len=int(round(MIN_LEN_M / dc)))
    out = dict(y=np.zeros(0, np.int64), x=np.zeros(0, np.int64), ang=np.zeros(0, np.float32),
               D=np.zeros((0, 3), np.float32), step=np.zeros((0, 3), np.float32), ok=np.zeros(0, bool),
               off=np.zeros(0, np.float32), stats=stats)
    if not sm.any():
        return out
    rep = lambda a: np.repeat(np.repeat(a, f, 0), f, 1)     # noqa: E731
    h, wd = U.shape[:2]
    seam = np.zeros((h, wd), bool)
    seam[:sm.shape[0] * f, :sm.shape[1] * f] = rep(sm)
    ang_c = np.zeros((h, wd), np.float32)
    ang_c[:sm.shape[0] * f, :sm.shape[1] * f] = rep(an)
    D_c = np.zeros((h, wd, 3), np.float32)
    D_c[:sm.shape[0] * f, :sm.shape[1] * f] = rep(Dd)
    cut = dilate(seam, 1)
    Lc, _, Lu = smooth_ratio(U, S, w0, SIGMA_S_M / cell_m, cut, usgs=True)
    ys, xs = np.nonzero(seam)
    a = ang_c[ys, xs]
    nx, ny = normals(a)
    P = lambda sg: (np.clip(np.rint(ys + sg * STEP_CELLS * ny).astype(int), 0, h - 1),    # noqa: E731
                    np.clip(np.rint(xs + sg * STEP_CELLS * nx).astype(int), 0, wd - 1))
    (ya, xa), (yb, xb) = P(1), P(-1)
    ok = ~cut[ya, xa] & ~cut[yb, xb] & (valid[ya, xa] & valid[yb, xb])
    step = (Lc[ya, xa] - Lc[yb, xb]).astype(np.float32)     # local step across the seam
    # The USGS share of each component's step (its medians over the cells): a step in S2 only (an
    # EOX compositing seam, the edge of the per-tile 2016 fallback) has the same ratio step and
    # passes the S2 gate when |D_s2| ~ |D|, but USGS has none of it (share ~0; a survey seam ~1).
    # Measured on the 300 m cut blurs: on the detector's 45 m bands texture decides, and the gate
    # picks the offsets where USGS' texture mimics the step. Dropped components leave a cut behind
    # (their cells were cut above): nothing reaches across it, nothing is scattered from it.
    ustep = Lu[ya, xa] - Lu[yb, xb]
    lab_d = np.full(sm.shape, -1, np.int64)
    dys, dxs, roots = _components(sm)
    lab_d[dys, dxs] = np.unique(roots, return_inverse=True)[1]      # stats order (_drop_small)
    lab = rep(lab_d)[ys, xs]
    keep = np.ones(len(ys), bool)
    stats_kept = []
    for k, st in enumerate(stats):
        m = (lab == k) & ok
        if m.any():
            md = np.median(step[m], 0)
            share = float(np.median(ustep[m], 0) @ md) / max(float(md @ md), 1e-12)
            st = dict(st, share=round(share, 2))
            if share < MIN_SHARE:
                keep &= lab != k
                continue
        stats_kept.append(st)
    out["stats"] = stats_kept
    if not keep.all():
        ys, xs, a, ok, step = ys[keep], xs[keep], a[keep], ok[keep], step[keep]
        if not len(ys):
            return out
    y0, y1, x0, x1 = core
    near = (ys >= y0 - LINE_R) & (ys < y1 + LINE_R) & (xs >= x0 - LINE_R) & (xs < x1 + LINE_R)
    if fine is not None:
        off = seam_lines(ys, xs, a, D_c[ys, xs], fine, fit=near)
    else:
        off = np.full(len(ys), np.nan)
    kc = (ys >= y0) & (ys < y1) & (xs >= x0) & (xs < x1)
    out.update(y=ys[kc].astype(np.int64), x=xs[kc].astype(np.int64), ang=a[kc].astype(np.float32),
               D=D_c[ys[kc], xs[kc]], step=step[kc], ok=ok[kc], off=off[kc].astype(np.float32))
    return out


# --- stage 2: the feather field -----------------------------------------------------------------
def fade_cut(cell_m, region_cells):
    """Distance (m) at which the fade reaches 0: FADE_CUTS sigma_f, at most one region (so the 3 x 3
    regions around a field hold every seam that reaches its core)."""
    return min(FADE_CUTS * SIGMA_F_M, region_cells * cell_m)


def seam_distance(seam):
    """Euclidean distance (samples) from every sample of the grid to the nearest seam sample (exact
    brute force over the seam samples, in chunks)."""
    h, w = seam.shape
    ys, xs = np.nonzero(seam)
    best = np.full((h, w), np.inf, np.float32)
    if len(ys) == 0:
        return best
    Y = np.arange(h, dtype=np.float32)[:, None, None]
    X = np.arange(w, dtype=np.float32)[None, :, None]
    for i in range(0, len(ys), 128):
        dy2 = (Y - ys[None, None, i:i + 128].astype(np.float32)) ** 2      # [h, 1, k]
        dx2 = (X - xs[None, None, i:i + 128].astype(np.float32)) ** 2      # [1, w, k]
        best = np.minimum(best, (dy2 + dx2).min(-1))
    return np.sqrt(best)


def feather_field(seams, origin, shape_c, cell_m, cut_m):
    """The feather's log gain on a window of the 30 m grid. seams: dict y, x (global cells), ang,
    step, ok of every seam cell that can reach the window; origin: global cell (y, x) of the window's
    sample (0, 0) (multiples of COARSE); shape_c: samples (h, w); cut_m: the fade's end (fade_cut).
    Returns (C [h, w, 3] float32 with the cut samples filled, seam samples)."""
    hc, wc = shape_c
    C = np.zeros((hc, wc, 3), np.float32)
    if len(seams["y"]) == 0:
        return C, np.zeros((hc, wc), bool)
    sy = (seams["y"] - origin[0]) // COARSE
    sx = (seams["x"] - origin[1]) // COARSE
    inside = (sy >= 0) & (sy < hc) & (sx >= 0) & (sx < wc)
    seam_c = np.zeros((hc, wc), bool)
    seam_c[sy[inside], sx[inside]] = True
    cut = dilate(seam_c, 1)
    # each side's half-step, scattered SIDE samples from the seam (outside the cut)
    nx, ny = normals(seams["ang"])
    cyy = (seams["y"] - origin[0] + 0.5) / COARSE
    cxx = (seams["x"] - origin[1] + 0.5) / COARSE
    X = np.zeros((hc, wc, 4), np.float32)
    for sg, part in ((1, SPLIT), (-1, -(1 - SPLIT))):
        py = np.floor(cyy + sg * SIDE * ny).astype(np.int64)
        px = np.floor(cxx + sg * SIDE * nx).astype(np.int64)
        m = seams["ok"] & (py >= 0) & (py < hc) & (px >= 0) & (px < wc)
        m[m] &= ~cut[py[m], px[m]]
        v = np.concatenate([seams["step"][m] * part, np.ones((int(m.sum()), 1), np.float32)], 1)
        np.add.at(X, (py[m], px[m]), v)
    cs = cell_m * COARSE
    Es = dt_blur(X, SIGMA_S_M / cs, cut)
    El = dt_blur(X, SIGMA_F_M / cs, cut)
    a = np.clip(Es[..., 3:] / 1e-3, 0, 1)
    E = a * Es[..., :3] / np.maximum(Es[..., 3:], 1e-9) + (1 - a) * El[..., :3] / np.maximum(El[..., 3:], 1e-9)
    dist = seam_distance(seam_c) * cs                         # m
    fade = np.exp(-0.5 * (dist / SIGMA_F_M) ** 2)
    t = np.clip((cut_m - dist) / (0.25 * cut_m), 0, 1)        # smooth taper to 0 at cut_m
    fade *= t * t * (3 - 2 * t)
    C = np.clip(-E * fade[..., None], -CLAMP, CLAMP).astype(np.float32)
    C[fade <= 0] = 0.0
    return fill(C, cut), seam_c


def build_field(seams, origin, shape_c, core_c, n_cells, cell_m, cut_m):
    """A region's Field from the seams around it. seams: dict y, x (global cells), ang, step, ok,
    off of every seam cell in reach (the region and its 8 neighbours); origin, shape_c: the window
    of feather_field; core_c: window sample (y, x) of the region's core corner; n_cells: the
    region's side in cells. Every array is indexed in a fixed order (seam cells sorted by global
    cell), so the result is a pure function of the seams."""
    order = np.lexsort((seams["x"], seams["y"]))
    seams = {k: v[order] for k, v in seams.items() if k != "stats"}
    C, seam_c = feather_field(seams, origin, shape_c, cell_m, cut_m)
    nco = n_cells // COARSE
    sl = (slice(core_c[0] - PADC, core_c[0] + nco + PADC), slice(core_c[1] - PADC, core_c[1] + nco + PADC))
    Cr = C[sl]
    band_c = dilate(seam_c, BAND)[sl]
    if not band_c.any():
        return Field(n_cells, Cr)
    pb = PADC * COARSE
    # band cells, relative to (core corner - pb)
    sy, sx = np.nonzero(band_c)
    oy, ox = np.mgrid[0:COARSE, 0:COARSE]
    by = (COARSE * sy[:, None, None] + oy[None]).ravel()
    bx = (COARSE * sx[:, None, None] + ox[None]).ravel()
    gy0 = origin[0] + core_c[0] * COARSE - pb               # global cell of band-map (0, 0)
    gx0 = origin[1] + core_c[1] * COARSE - pb
    R = (BAND + 1) * COARSE + 1
    side = n_cells + 2 * pb
    ry, rx = seams["y"] - gy0 + R, seams["x"] - gx0 + R
    m = (ry >= 0) & (ry < side + 2 * R) & (rx >= 0) & (rx < side + 2 * R)
    grid = np.full((side + 2 * R, side + 2 * R), -1, np.int64)
    grid[ry[m], rx[m]] = np.nonzero(m)[0]
    best = np.full(len(by), np.inf)
    sid = np.full(len(by), -1, np.int64)
    for dy in range(-R, R + 1):         # nearest seam cell (fixed scan order: deterministic ties)
        for dx in range(-R, R + 1):
            cand = grid[by + R + dy, bx + R + dx]
            d2 = float(dy * dy + dx * dx)
            better = (cand >= 0) & (d2 < best)
            best[better] = d2
            sid[better] = cand[better]
    has = sid >= 0
    by, bx, sid = by[has], bx[has], sid[has]
    used, inv = np.unique(sid, return_inverse=True)
    nx, ny = normals(seams["ang"][used])
    off = np.nan_to_num(seams["off"][used].astype(np.float64))
    qy = seams["y"][used] - (gy0 + pb) + 0.5 + off * ny     # line point, cells from the core corner
    qx = seams["x"][used] - (gx0 + pb) + 0.5 + off * nx
    # both sides' gains: the field SIDE samples from the line, outside the cut
    wy = (qy + (gy0 + pb - origin[0])) / COARSE
    wx = (qx + (gx0 + pb - origin[1])) / COARSE

    def at(sg):
        iy = np.clip(np.floor(wy + sg * SIDE * ny).astype(np.int64), 0, shape_c[0] - 1)
        ix = np.clip(np.floor(wx + sg * SIDE * nx).astype(np.int64), 0, shape_c[1] - 1)
        return C[iy, ix]
    smap = np.full((side, side), -1, np.int32)
    smap[by, bx] = inv
    line = np.stack([qy, qx, ny, nx], 1).astype(np.float32)
    return Field(n_cells, Cr, smap, line, at(1), at(-1))


# --- per tile ------------------------------------------------------------------------------------
def upsample(g, f):
    """Bilinear x f (a power of two) of a field slice padded by one sample, [n + 2, n + 2, c] ->
    [f n, f n, c], sample i's centre at output (i + 0.5) f; phase slicing (a BLAS matmul at this size
    costs several ms)."""
    if f == 1:
        return g[1:-1, 1:-1]
    n = g.shape[0] - 2
    o1 = np.empty((f * n, g.shape[1], g.shape[2]), np.float32)
    for p in range(f):
        c = (p + 0.5) / f + 0.5
        c0 = int(c)
        fr = c - c0
        o1[p::f] = (1 - fr) * g[c0:c0 + n] + fr * g[c0 + 1:c0 + 1 + n]
    o = np.empty((f * n, f * n, g.shape[2]), np.float32)
    for p in range(f):
        c = (p + 0.5) / f + 0.5
        c0 = int(c)
        fr = c - c0
        o[:, p::f] = (1 - fr) * o1[:, c0:c0 + n] + fr * o1[:, c0 + 1:c0 + 1 + n]
    return o


def bilinear(img, y, x):
    """img [h, w, c] at pixel index coordinates (y, x) (pixel centres at integers), clamped."""
    h, w = img.shape[:2]
    y = np.clip(y, 0, h - 1.001)
    x = np.clip(x, 0, w - 1.001)
    y0 = np.floor(y).astype(np.int64)
    x0 = np.floor(x).astype(np.int64)
    fy = (y - y0)[..., None]
    fx = (x - x0)[..., None]
    return ((img[y0, x0] * (1 - fx) + img[y0, x0 + 1] * fx) * (1 - fy)
            + (img[y0 + 1, x0] * (1 - fx) + img[y0 + 1, x0 + 1] * fx) * fy)


class Field:
    """A region's correction: the 30 m log gain over its core (+PADC samples) and the band (cells
    within BAND samples of a seam: nearest seam cell, its sub-pixel line and both sides' gains).
    Coordinates: cells relative to the core's corner (rx N, ry N) of a region of N cells."""

    def __init__(self, n_cells, C, sid=None, line=None, cA=None, cB=None, meta=None):
        self.n = n_cells
        self.C = np.asarray(C, np.float16).astype(np.float32)      # as stored: the same bytes either way
        self.gain = np.exp(self.C)
        self.noop = np.abs(self.C).max(-1) < NOOP
        self.pb = PADC * COARSE                     # band map margin (cells)
        self.sid = sid
        self.has_band = sid is not None and line is not None and len(line) > 0
        if self.has_band:
            self.line = np.asarray(line, np.float32)    # qy, qx (cells, core corner at 0), ny, nx
            self.cA = np.asarray(cA, np.float32)
            self.cB = np.asarray(cB, np.float32)
            self.gA, self.gB = np.exp(self.cA), np.exp(self.cB)
        self.meta = meta or {}

    def arrays(self):
        out = dict(C=self.C.astype(np.float16))
        if self.has_band:
            out.update(sid=self.sid, line=self.line, cA=self.cA, cB=self.cB)
        return out

    def apply(self, rgb, z, x, y, region_z, margin=None):
        """The tile (z, x, y) of this region corrected (uint8 [256, 256, 3]), or None if every pixel
        keeps its value. margin() -> the raw tile with MARGIN px of its neighbours, for inpaint
        samples beyond the edge (called only when the strip comes that near)."""
        f = 2 ** (z - GRID_Z + 2)                               # px per field sample (one z12 px)
        n = TILE // f                                           # field samples across the tile
        k = 2 ** (z - region_z)                                 # tiles of zoom z per region side
        s0y = (y % k) * n + PADC - 1
        s0x = (x % k) * n + PADC - 1
        g = self.gain[s0y:s0y + n + 2, s0x:s0x + n + 2]
        F = 2.0 ** (z - GRID_Z)                                 # px per cell (0.25 at z12)
        nc = int(round(TILE / F))                               # cells across the tile
        c0y = int(round((y % k) * nc)) + self.pb
        c0x = int(round((x % k) * nc)) + self.pb
        band = None
        if self.has_band:
            sc = self.sid[c0y:c0y + nc, c0x:c0x + nc]
            by, bx = np.nonzero(sc >= 0)
            if len(by):
                band = (by, bx, sc[by, bx])
        if band is None and self.noop[s0y:s0y + n + 2, s0x:s0x + n + 2].all():
            return None
        G = upsample(g, f)
        if band is not None:
            G = G.copy() if f == 1 else G
            strip = self._band(G, band, F, c0y - self.pb, c0x - self.pb, z)
        else:
            strip = None
        x_lin = SRGB2LIN[rgb] * G
        if strip is not None:
            self._inpaint(x_lin, rgb, strip, F, margin)
        out = _LUT16[np.minimum(x_lin * 65535 + 0.5, 65535).astype(np.uint16)]
        same = (G == 1).all(-1)
        if strip is not None:
            same[strip[0], strip[1]] = False
        out[same] = rgb[same]                   # gain 1: the exact source value
        return out

    def _band(self, G, band, F, cy0, cx0, z):
        """Band pixels take their side's gain (in place in G). Returns the strip pixels to inpaint
        (py, px, d16, seam id) at z >= INPAINT_Z, else None."""
        by, bx, s = band
        L = self.line
        if F >= 1:                                              # z14..16: F x F px per band cell
            Fi = int(F)
            oy, ox = np.mgrid[0:Fi, 0:Fi]
            py = (Fi * by[:, None, None] + oy[None]).ravel()
            px = (Fi * bx[:, None, None] + ox[None]).ravel()
            s = np.repeat(s, Fi * Fi)
            d16 = (((py + 0.5) / F + cy0 - L[s, 0]) * L[s, 2] + ((px + 0.5) / F + cx0 - L[s, 1]) * L[s, 3]) * LINE_F
            a = np.clip(0.5 + d16 / (2 * RAMP), 0, 1)[:, None]
            G[py, px] = np.exp(a * self.cA[s] + (1 - a) * self.cB[s])
            if z >= INPAINT_Z:
                st = np.abs(d16) <= STRIP
                if st.any():
                    return py[st], px[st], d16[st], s[st]
            return None
        # z12..13: a pixel covers B x B cells; its gain is the mean over them (band cells their side's)
        B = int(round(1 / F))
        d16 = ((by + 0.5 + cy0 - L[s, 0]) * L[s, 2] + (bx + 0.5 + cx0 - L[s, 1]) * L[s, 3]) * LINE_F
        a = np.clip(0.5 + d16 / (2 * RAMP), 0, 1)[:, None]
        gc = np.exp(a * self.cA[s] + (1 - a) * self.cB[s])
        py, px = by // B, bx // B
        acc = np.zeros(G.shape, np.float32)
        cnt = np.zeros(G.shape[:2], np.float32)
        np.add.at(acc, (py, px), gc)
        np.add.at(cnt, (py, px), 1)
        m = cnt > 0
        G[m] = (acc[m] + (B * B - cnt[m])[:, None] * G[m]) / (B * B)
        return None

    def _inpaint(self, x_lin, rgb, strip, F, margin):
        """The strip on the line (the mosaic's blend row, JPEG ringing) replaced along the normal by
        the corrected pixels at d = +-REACH z16 px: raw samples x their side's gain."""
        py, px, d16, s = strip
        L = self.line
        ny, nx = L[s, 2], L[s, 3]
        r = F / LINE_F                                          # px per z16 px
        ya, xa = py + (REACH - d16) * r * ny, px + (REACH - d16) * r * nx
        yb, xb = py - (REACH + d16) * r * ny, px - (REACH + d16) * r * nx
        lo = min(ya.min(), yb.min(), xa.min(), xb.min())
        hi = max(ya.max(), yb.max(), xa.max(), xb.max())
        if lo < 0 or hi > min(rgb.shape[:2]) - 1.001:
            src = margin() if margin is not None else None
            if src is None:
                src = np.pad(rgb, ((MARGIN, MARGIN), (MARGIN, MARGIN), (0, 0)), mode="edge")
            src, o = SRGB2LIN[src], MARGIN
        else:
            src, o = SRGB2LIN[rgb], 0
        pa = bilinear(src, ya + o, xa + o) * self.gA[s]
        pb = bilinear(src, yb + o, xb + o) * self.gB[s]
        t = ((d16 + REACH) / (2 * REACH))[:, None]
        x_lin[py, px] = (1 - t) * pb + t * pa


# --- regions (I/O, background worker) ---------------------------------------------------------
class TileSampler:
    """fine() of seam_lines over source tiles: log linear values at z16 pixel index coordinates of a
    window (origin: the window's global z16 pixel), the tiles fetched as the samples need them."""

    def __init__(self, fetch, origin, z):
        self.fetch, self.oy, self.ox, self.z = fetch, origin[0], origin[1], z
        self.tiles = {}                 # (tx, ty) -> uint8 tile or None

    def __call__(self, py, px):
        gy = np.asarray(py, np.float64) + self.oy
        gx = np.asarray(px, np.float64) + self.ox
        y0 = np.floor(gy).astype(np.int64)
        x0 = np.floor(gx).astype(np.int64)
        fy = (gy - y0).astype(np.float32)[..., None]
        fx = (gx - x0).astype(np.float32)[..., None]
        n = 2 ** self.z
        taps = [(y0, x0), (y0, x0 + 1), (y0 + 1, x0), (y0 + 1, x0 + 1)]
        need = set()
        for yy, xx in taps:
            ids = np.unique(np.clip(yy >> 8, 0, n - 1) * n + ((xx >> 8) % n))
            need.update((int(i % n), int(i // n)) for i in ids)
        new = sorted(k for k in need if k not in self.tiles)
        if new:
            self.tiles.update(self.fetch(new))
        keys = sorted(self.tiles, key=lambda k: k[1] * n + k[0])
        ids = np.array([k[1] * n + k[0] for k in keys], np.int64)
        stack = np.zeros((len(keys) + 1, TILE, TILE, 3), np.uint8)     # the last one: no data
        have = np.zeros(len(keys) + 1, bool)
        for i, k in enumerate(keys):
            if self.tiles[k] is not None:
                stack[i] = self.tiles[k]
                have[i] = True
        vals = []
        for yy, xx in taps:
            key = np.clip(yy >> 8, 0, n - 1) * n + ((xx >> 8) % n)
            j = np.minimum(np.searchsorted(ids, key), len(keys) - 1)
            j = np.where(ids[j] == key, j, len(keys))
            v = np.log(SRGB2LIN[stack[j, np.clip(yy, 0, None) & 255, xx & 255]] + EPS)
            v[~have[j]] = np.nan
            vals.append(v)
        return (vals[0] * (1 - fx) + vals[1] * fx) * (1 - fy) + (vals[2] * (1 - fx) + vals[3] * fx) * fy


def _seams_empty(stats=()):
    return dict(y=np.zeros(0, np.int64), x=np.zeros(0, np.int64), ang=np.zeros(0, np.float32),
                D=np.zeros((0, 3), np.float32), step=np.zeros((0, 3), np.float32), ok=np.zeros(0, bool),
                off=np.zeros(0, np.float32), stats=list(stats))


def _atomic_write(path, write):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{threading.get_ident()}.tmp"
    with open(tmp, "wb") as f:
        write(f)
    staging.replace_cached(tmp, path)


class ColorMatch:
    """Seams and fields of the regions (one background thread) and the per-tile correction for one
    Imagery (bind()). region_z: the region zoom (12; tests use smaller regions)."""
    disk = cachemod.NONE    # the server's cache.Cache (last use per file, the size cap); set by earthd
    PENDING = PENDING
    refs = REFS                         # the reference layers (imagery credits them)
    source, version = SOURCE, VERSION   # (the finished pyramid's settings: imagery.Imagery._fin_sig, with meta)

    def __init__(self, cache_dir, mode="s2", region_z=REGION_Z, mem_regions=MEM_REGIONS):
        if mode not in MODES[1:]:
            raise ValueError(f"colormatch mode {mode!r}: choose from {', '.join(MODES[1:])}")
        if not 10 <= region_z <= GRID_Z:
            raise ValueError("region zoom out of range")
        self.mode = mode
        self.rz = region_z
        self.n = TILE << (GRID_Z - region_z)            # cells per region side
        self.dir = os.path.join(cache_dir, "colormatch", f"v{VERSION}", SOURCE, str(region_z))
        self.meta = dict(version=VERSION, source=SOURCE, refs=",".join(REFS), region_z=region_z, mode=mode)
        self.mem = mem_regions
        self.imagery = None
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._fields = OrderedDict()        # rk -> Field or EMPTY (decoded LRU)
        self._seams = OrderedDict()         # rk -> seams dict (decoded LRU)
        self._events = {}                   # rk -> Event, set when its field is done (or failed)
        self._failed = {}                   # rk -> monotonic time of the failure
        self._queue = []                    # heap of (distance m, seq, rk)
        self._seq = 0
        self._center = None
        self._listeners = []
        self._logged = set()
        self._closed = False
        self.stats = {}
        self._thread = threading.Thread(target=self._worker, name="colormatch", daemon=True)
        self._thread.start()

    def bind(self, imagery):
        self.imagery = imagery

    def add_listener(self, fn):
        """fn(rk, changed) from the worker thread once a region's field is ready (changed: it is
        not EMPTY, so textures built raw over it differ from what they would be now)."""
        self._listeners.append(fn)

    def _count(self, what, n=1):
        with self._lock:
            self.stats[what] = self.stats.get(what, 0) + n

    def summary(self):
        with self._lock:
            seams = sum(1 for v in self._fields.values() if v is not EMPTY)
            return (f"colormatch {self.mode}: regions {seams} with seams / {len(self._fields)} ready, "
                    f"{len(self._queue)} queued; tiles {dict(sorted(self.stats.items()))}")

    # --- geometry ------------------------------------------------------------------------------
    def region_of(self, z, x, y):
        return x >> (z - self.rz), y >> (z - self.rz)

    def _center_of(self, rk):
        lon, lat = lonlat_of_px((rk[0] + 0.5) * TILE, (rk[1] + 0.5) * TILE, self.rz)
        return float(lat), float(lon)

    def cell_m(self, rk):
        """Cell size (m) of the region's grid at its centre."""
        return EARTH_CIRC * math.cos(math.radians(self._center_of(rk)[0])) / (TILE * 2 ** GRID_Z)

    def _distance(self, rk):
        if self._center is None:
            return 0.0
        lat, lon = self._center_of(rk)
        la0, lo0 = self._center
        dlon = (lon - lo0 + 180.0) % 360.0 - 180.0
        return math.hypot((lat - la0) * 111320.0, dlon * 111320.0 * math.cos(math.radians(la0)))

    # --- files ---------------------------------------------------------------------------------
    def _paths(self, rk, kind):
        base = os.path.join(self.dir, str(rk[0]), str(rk[1]) + ("" if kind == "field" else ".seams"))
        return base + ".npz", base + ".none"

    def _read_meta(self, raw):
        try:
            m = json.loads(raw)
        except ValueError:
            return None
        return m if isinstance(m, dict) and {k: m.get(k) for k in self.meta} == self.meta else None

    def _load(self, rk, kind):
        """(seams / field of rk (EMPTY for a field marker), meta), or None if absent or stale
        (another VERSION, reference chain, region zoom or mode)."""
        npz, none = self._paths(rk, kind)
        if os.path.exists(none):
            try:
                with open(none, "rb") as f:
                    m = self._read_meta(f.read())
            except OSError:
                m = None
            if m is not None:
                self.disk.used(none)
                return (EMPTY, m) if kind == "field" else (_seams_empty(m.get("stats", ())), m)
        if not os.path.exists(npz):
            return None
        try:
            with np.load(npz) as f:
                m = self._read_meta(str(f["meta"]))
                if m is None:
                    return None
                a = {k: f[k] for k in f.files if k != "meta"}
        except (OSError, ValueError, KeyError):
            return None
        self.disk.used(npz)
        if kind == "field":
            return Field(self.n, a["C"], a.get("sid"), a.get("line"), a.get("cA"), a.get("cB"), m), m
        s = _seams_empty(m.get("stats", ()))
        s.update({k: a[k] for k in s if k in a})
        return s, m

    def _store(self, rk, kind, value, info):
        npz, none = self._paths(rk, kind)
        m = dict(self.meta, **info)
        if (value is EMPTY) if kind == "field" else len(value["y"]) == 0:
            _atomic_write(none, lambda f: f.write(json.dumps(m).encode()))
            self.disk.wrote(none)
            return
        arrays = value.arrays() if kind == "field" else {k: v for k, v in value.items() if k != "stats"}
        _atomic_write(npz, lambda f: np.savez_compressed(f, meta=np.array(json.dumps(m)), **arrays))
        self.disk.wrote(npz)

    @staticmethod
    def _lru_put(od, k, v, size):
        od[k] = v
        od.move_to_end(k)
        while len(od) > size:
            od.popitem(last=False)

    # --- requests ------------------------------------------------------------------------------
    # prepare, request and status run on earthd's event loop: memory, the queue and stat calls only
    # (a field is decoded by field(), on a tile worker, outside the lock)
    def _on_disk(self, rk):
        """True (a field file of rk), False (its no-seam marker) or None: stat calls only."""
        npz, none = self._paths(rk, "field")
        return True if os.path.exists(npz) else False if os.path.exists(none) else None

    def _peek(self, rk):
        """Whether rk is in memory, queued or on disk (then a tile worker reads it when needed)."""
        with self._lock:
            if rk in self._fields or rk in self._events:
                return True
        return self._on_disk(rk) is not None

    def _enqueue(self, rk):
        """Queue the field of rk (once). Caller holds the lock."""
        if rk in self._events or rk in self._fields:
            return
        f = self._failed.get(rk)
        if f is not None and time.monotonic() - f < RETRY_S:
            return
        self._failed.pop(rk, None)
        self._events[rk] = threading.Event()
        self._seq += 1
        heapq.heappush(self._queue, (self._distance(rk), self._seq, rk))
        self._cv.notify()

    def _wanted(self, rk):
        return self._center is None or self._distance(rk) <= REQUEST_M

    def prepare(self, lat, lon, radius=PREP_M):
        """Anchor set: queue the fields of the regions within radius, nearest first."""
        n = 2 ** self.rz
        with self._lock:
            self._center = (lat, lon)
            self._queue = sorted((self._distance(rk), s, rk) for _, s, rk in self._queue)
        if abs(lat) >= mercator.MAX_LAT:        # no Web-Mercator imagery to match (the poles)
            return
        x = (lon / 360.0 + 0.5) * n * TILE
        y = (0.5 - math.asinh(math.tan(math.radians(lat))) / (2 * math.pi)) * n * TILE
        rpx = radius / (EARTH_CIRC * math.cos(math.radians(lat)) / (TILE * n))
        for ry in range(max(0, int((y - rpx) // TILE)), min(n - 1, int((y + rpx) // TILE)) + 1):
            for rx in mercator.columns(x, rpx, TILE, n):
                rk = (rx % n, ry)
                if 0 <= ry < n and self._distance(rk) <= radius + 6000 and not self._peek(rk):
                    with self._lock:
                        self._enqueue(rk)

    def handles(self, key):
        """The source tile (source, z, x, y) is one this corrects (USGS, region zoom to MAX_Z)."""
        src, z, _, _ = key
        return getattr(src, "name", src) == SOURCE and self.rz <= z <= MAX_Z

    def request(self, key):
        """A source tile will be needed (prefetch, on the event loop): queue its region's field
        unless it is known (never waits, never decodes)."""
        if not self.handles(key):
            return
        rk = self.region_of(*key[1:])
        if not self._peek(rk):
            with self._lock:
                if self._wanted(rk):
                    self._enqueue(rk)

    def field(self, rk, enqueue=True):
        """The region's Field, EMPTY, or None if not ready (then queued, within REQUEST_M). Reads
        it from disk if needed, without holding the lock (tile workers)."""
        with self._lock:
            v = self._fields.get(rk)
            if v is not None:
                self._fields.move_to_end(rk)
                return v
            busy = rk in self._events       # queued or being computed: not on disk yet
        if not busy:
            got = self._load(rk, "field")
            if got is not None:
                with self._lock:
                    self._lru_put(self._fields, rk, got[0], self.mem)
                    self._log_region(rk, got[0], got[1], cached=True)
                return got[0]
        if enqueue:
            with self._lock:
                if self._wanted(rk):
                    self._enqueue(rk)
        return None

    def status(self, rk):
        """None (not ready), False (ready, nothing to correct) or True (ready with a correction).
        On the event loop: from memory, else the files on disk (stat calls only). A region that failed
        (tried again later) is not ready: a file it has is one the renders could not use (another mode
        or reference chain), else it would not have been computed (earthd re-rendered a shown tile in a
        loop, or dropped it from the region's waiting list)."""
        with self._lock:
            v = self._fields.get(rk)
            if v is not None:
                return v is not EMPTY
            if rk in self._events or rk in self._failed:
                return None
        return self._on_disk(rk)

    def wait(self, rk, timeout):
        """The field of rk, queued if needed, waiting up to timeout s (tests, tools)."""
        v = self.field(rk)
        if v is not None:
            return v
        with self._lock:
            ev = self._events.get(rk)
        if ev is not None:
            ev.wait(timeout)
        return self.field(rk, enqueue=False)

    # --- worker --------------------------------------------------------------------------------
    def _requeue_failed(self):
        """Queue again the regions that failed more than RETRY_S ago and are still wanted. Caller holds
        the lock. Without this a region dropped while the player stood still (the rate limiter keeps
        a cold start's tokens for demand) was never asked for again (in-game 2026-10-11: no region
        ready 7 min after a start at the AZ/UT line)."""
        now = time.monotonic()
        for rk, t in list(self._failed.items()):
            if now - t >= RETRY_S and self._wanted(rk):
                self._enqueue(rk)

    def _worker(self):
        pacer.enable(pacer.DUTY)     # a background analysis: paced (pacer.py)
        while True:
            with self._lock:
                while not self._queue and not self._closed:
                    if self._failed:
                        self._cv.wait(timeout=RETRY_S / 4)
                        self._requeue_failed()
                    else:
                        self._cv.wait()
                if self._closed:
                    return
                _, _, rk = heapq.heappop(self._queue)
            pacer.idle()                # the wait above was not busy time
            try:
                t0 = time.perf_counter()
                F, info = self._compute_field(rk)
                info["seconds"] = round(time.perf_counter() - t0, 2)
                self._store(rk, "field", F, info)
            except Exception as e:      # network trouble: tried again later
                if self._closed:
                    return
                # deferred: the imagery host's bucket is kept for demand (a cold start): not a fault
                (log.debug if getattr(e, "deferred", False) else log.warning)(
                    "colormatch region %s: %s: %s", rk, type(e).__name__, e)
                with self._lock:
                    self._failed[rk] = time.monotonic()
                    self._events.pop(rk).set()
                continue
            with self._lock:
                self._lru_put(self._fields, rk, F, self.mem)
                self._events.pop(rk).set()
                self._log_region(rk, F, dict(self.meta, **info), cached=False)
            for fn in list(self._listeners):
                try:
                    fn(rk, F is not EMPTY)
                except Exception:
                    log.exception("colormatch listener")

    def _log_region(self, rk, F, m, cached):
        """Once per region: seams in reach (count, length, step %), time, cache hit."""
        if rk in self._logged:
            return
        self._logged.add(rk)
        how = "cached" if cached else f"{m.get('seconds', 0):.1f} s, {m.get('computed', 0)} seam sets computed"
        if F is EMPTY:
            log.info("colormatch region %s: no seam within reach (%s)", rk, how)
        else:
            log.info("colormatch region %s: %d seam(s) in reach, %.1f km, step %s %% (R/G/B); %s", rk,
                     m.get("seams", 0), m.get("km", 0.0), m.get("step_pct"), how)

    def _neighbours(self, rk):
        """The 3 x 3 regions around rk in a fixed order: (key or None beyond the poles, dx, dy)."""
        n = 2 ** self.rz
        return [(((rk[0] + dx) % n, rk[1] + dy) if 0 <= rk[1] + dy < n else None, dx, dy)
                for dy in (-1, 0, 1) for dx in (-1, 0, 1)]

    def _bg_tiles(self, im, keys):
        """im.tiles(keys) as netfetch kind "background", patiently: when the limiter drops a request
        (the host's bucket is kept for demand), wait and go on with what is still missing (fetched
        tiles are cached) for up to PATIENT_S, on this region thread only. Dropping the whole region
        instead fetched a handful of its ~70 tiles per minute (in-game 2026-10-11)."""
        keys = list(keys)
        deadline = time.monotonic() + PATIENT_S
        wait = PATIENT_STEP_S
        while True:
            try:
                return im.tiles(keys, kind="background")
            except Exception as e:
                if not getattr(e, "deferred", False) or self._closed or time.monotonic() + wait > deadline:
                    raise
            time.sleep(wait)
            wait = min(wait * 1.5, 10.0)

    def _compute_field(self, rk):
        """(Field or EMPTY, info) of rk from the seams of its 3 x 3 regions (loaded or computed)."""
        parts, computed = [], 0
        for nk, dx, dy in self._neighbours(rk):
            if nk is None:
                continue
            s, new = self._seams_of(nk)
            computed += new
            if len(s["y"]):
                s = dict(s, x=s["x"] - nk[0] * self.n + (rk[0] + dx) * self.n)    # unwrapped at the antimeridian
                parts.append(s)
        info = dict(computed=computed)
        if not parts:
            return EMPTY, info
        seams = {k: np.concatenate([p[k] for p in parts]) for k in parts[0] if k != "stats"}
        nc = self.n // COARSE
        cm = self.cell_m(rk)
        F = build_field(seams, ((rk[1] - 1) * self.n, (rk[0] - 1) * self.n), (3 * nc, 3 * nc), (nc, nc),
                        self.n, cm, fade_cut(cm, self.n))
        if not F.has_band and F.noop.all():
            return EMPTY, info
        info.update(seams=sum(len(p["stats"]) for p in parts), km=round(len(seams["y"]) / DET_F * cm / 1000, 2),
                    step_pct=[round(100 * (math.exp(abs(float(v))) - 1)) for v in np.median(seams["D"], 0)])
        return F, info

    def _seams_of(self, rk):
        """(seams of rk, 1 if computed now else 0): memory, disk, or detect_region on its window."""
        with self._lock:
            s = self._seams.get(rk)
        if s is not None:
            return s, 0
        got = self._load(rk, "seams")
        new = 0
        if got is None:
            t0 = time.perf_counter()
            s = self._detect(rk)
            self._store(rk, "seams", s, dict(stats=s["stats"], seconds=round(time.perf_counter() - t0, 2)))
            log.debug("colormatch seams %s: %d cells in %.1f s", rk, len(s["y"]), time.perf_counter() - t0)
            new = 1
        else:
            s = got[0]
        with self._lock:
            self._lru_put(self._seams, rk, s, 4 * self.mem)
        return s, new

    def _detect(self, rk):
        """Fetch the region's window (z14 USGS and S2 with a HALO ring) and detect its seams."""
        from imagery import SOURCES
        im = self.imagery
        usgs = SOURCES[SOURCE]
        k = self.n // TILE                  # z14 tiles per region side
        h = HALO // TILE
        n14 = 2 ** GRID_Z
        cols = [rk[0] * k + i for i in range(-h, k + h)]
        rows = [rk[1] * k + j for j in range(-h, k + h)]
        W = len(cols) * TILE
        keys = {(i, j): (usgs, GRID_Z, c % n14, r) for j, r in enumerate(rows) for i, c in enumerate(cols)
                if 0 <= r < n14}
        got = self._bg_tiles(im, keys.values())     # never the demand budget (netfetch)
        have = {ij for ij, key in keys.items() if got[key] is not None and not got[key].uniform}
        if not have & {(i, j) for i in range(h, h + k) for j in range(h, h + k)}:
            return _seams_empty()           # no USGS in the core (outside its coverage)
        ref, todo = {}, sorted(have)
        for name in REFS:                   # S2 2023, per tile 2016 where 2023 has nothing
            rkeys = {ij: (SOURCES[name], GRID_Z, keys[ij][2], keys[ij][3]) for ij in todo}
            rgot = self._bg_tiles(im, rkeys.values())
            for ij in todo:
                t = rgot[rkeys[ij]]
                if t is not None and not t.uniform:
                    ref[ij] = t.rgb
            todo = [ij for ij in todo if ij not in ref]
        U = np.zeros((W, W, 3), np.float32)
        S = np.zeros((W, W, 3), np.float32)
        valid = np.zeros((W, W), bool)
        for (i, j), rgb in ref.items():
            pacer.pace()
            sl = (slice(j * TILE, (j + 1) * TILE), slice(i * TILE, (i + 1) * TILE))
            U[sl] = SRGB2LIN[got[keys[(i, j)]].rgb]
            S[sl] = SRGB2LIN[rgb]
            valid[sl] = True
        if not valid.any():
            return _seams_empty()

        def fetch16(tiles):
            g = self._bg_tiles(im, [(usgs, 16, tx, ty) for tx, ty in tiles])
            return {(tx, ty): None if (t := g[(usgs, 16, tx, ty)]) is None or t.uniform else t.rgb for tx, ty in tiles}
        s = detect_region(U, S, valid, self.cell_m(rk), (h * TILE, (h + k) * TILE, h * TILE, (h + k) * TILE),
                          TileSampler(fetch16, (rows[0] * TILE * LINE_F, cols[0] * TILE * LINE_F), 16))
        s["y"] = s["y"] + rows[0] * TILE                        # global cells
        s["x"] = s["x"] + cols[0] * TILE
        return s

    # --- tiles ---------------------------------------------------------------------------------
    def poll(self, rk):
        """A region apply() left a tile raw for (PENDING, or "far"): "ready" (its field is here: the tile is to be
        corrected again), "wait" (wanted, queued) or "far" (beyond REQUEST_M of the anchor)."""
        if self.field(rk) is not None:
            return "ready"
        with self._lock:
            return "wait" if self._wanted(rk) else "far"

    def apply(self, key, rgb, imagery):
        """(corrected RGB or None if unchanged, True) for a decoded source tile's RGB (raw, or relit by --deshadow),
        (None, "far") when its region lies beyond REQUEST_M of the anchor (raw while it does), (None or the RGB,
        False) when a neighbour of the inpaint margin could not be fetched (not to be kept), or (PENDING, region) if
        its region is not ready yet."""
        src, z, x, y = key
        if not self.handles(key):
            return None, True
        rk = self.region_of(z, x, y)
        F = self.field(rk)
        if F is None:
            with self._lock:
                wanted = self._wanted(rk)
            if not wanted:                  # beyond REQUEST_M of the anchor: raw, nothing to wait for
                self._count("far")
                return None, "far"
            self._count("pending")
            return PENDING, rk
        if F is EMPTY:
            self._count("empty")
            return None, True
        failed = []

        def margin():
            n = 2 ** z
            nb = {(dx, dy): (src, z, (x + dx) % n, y + dy) for dy in (-1, 0, 1) for dx in (-1, 0, 1)
                  if 0 <= y + dy < n and (dx, dy) != (0, 0)}
            try:
                got = imagery.tiles(nb.values())
            except Exception as e:      # ImageryError: edge-replicated this time, and not cached
                failed.append(e)
                return None
            big = np.pad(rgb, ((MARGIN, MARGIN), (MARGIN, MARGIN), (0, 0)), mode="edge")
            cut = {-1: (slice(0, MARGIN), slice(TILE - MARGIN, TILE)), 0: (slice(MARGIN, MARGIN + TILE), slice(0, TILE)),
                   1: (slice(MARGIN + TILE, None), slice(0, MARGIN))}     # (in big, in the neighbour)
            ds_ = getattr(imagery, "deshadow", None)
            for (dx, dy), k in nb.items():
                t = got.get(k)
                if t is not None and not t.uniform:
                    # the neighbour as the mosaic shows it before its own gain: relit with --deshadow
                    # (the samples are taken times this tile's side gains, like those inside)
                    r = None
                    if ds_ is not None:
                        try:
                            r = ds_.relight(k, t, imagery)
                        except Exception as e:  # ImageryError: raw this time, and not cached
                            failed.append(e)
                    big[cut[dy][0], cut[dx][0]] = (t.rgb if r is None else r)[cut[dy][1], cut[dx][1]]
            return big
        t0 = time.perf_counter()
        out = F.apply(rgb, z, x, y, self.rz, margin)
        kind = "same" if out is None else "corrected"
        self._count(kind)
        self._count("ms:" + kind, round((time.perf_counter() - t0) * 1000, 2))
        return out, not failed

    def close(self):
        with self._lock:
            self._closed = True
            self._cv.notify_all()
