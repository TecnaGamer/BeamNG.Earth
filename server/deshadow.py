"""De-shadowing of the USGS imagery: undo the capture sun's terrain shading and cast shadows
(docs/research/2026-10-10-deshadow-prototype.md, method A; --deshadow off|lambert|full).

The photo of a ravine carries the shadow of the ridge next to it, and every slope its
capture-time shading; the engine lights the ground again, so both double up and point the wrong
way at other times of day. Model per pixel (linear RGB):

    I_c = A_c (amb_c + sun_c * lam * lit) + haze_c

lam = Lambert term of the DEM normal for the capture sun, lit = 1 - cast shadow. The relight
replaces lam * lit by the flat-ground sun sin(el) (the albedo lit by a flat sun):
    gain_c = clip((rho_c + sin el) / (rho_c + lam), 1/LAM_CAP, LAM_CAP) * (rho_c + lam) / (rho_c + lam * lit)
with rho_c = amb_c / sun_c, all channels of a pixel capped together (keeps the hue).

Per USGS z12 region (~8 km; the source is a mosaic of flights, so the sun is fitted per region),
computed once in a background thread when an anchor is set and cached on disk (its imagery is
fetched as netfetch kind "background": dropped while the host's bucket is kept for demand, and the
region is tried again after RETRY_S):
  - the region grid is the z14 pixel grid (~7.7 m at 36 N): z14 imagery, DEM z13 (512 px tiles:
    the same pixels), padded by PAD_M for the horizon march and EXT cells for tile margins;
  - sun fit: for candidate (az, el), score = signed r^2 of luminance against lam * lit plus
    0.5 x MCC of the predicted shadow against the dark pixels; coarse at 4 cells (5 deg / 2.5 deg),
    refined at 2 cells (2 deg / 1 deg); el >= EL_MIN (aerial programmes fly with a high sun);
    gate: r^2 >= R2_MIN, else the region is left unchanged;
  - lit (soft terminator) and lam at the fitted sun, and the DEM shadow mask blurred over ~77 m
    (the prior of the per-tile shadow snap) as uint8 grids (<cache>/deshadow/usgs/12/x/y.npz);
  - a two-Gaussian shadow classifier in (log luminance, blue ratio) fitted on the confident
    interiors; separation gate SEP_MIN: if the image does not confirm the DEM's shadows (dark
    rock, misregistration, wrong sun), the cast term is off and only the Lambert part stays;
  - haze (dark-object subtraction) and rho from the shadow edge (same surfaces on both sides, so
    the albedo cancels): k_c = shadow / sun = rho_c / (rho_c + lam_e);
  - the classifier, the halo guard's statistics, haze and rho again from a few z16 tiles at the
    shadow edges and interiors (refine_fine): z14 averages the dark objects away, blurs the
    terminator and has less texture variance than the pixels the tiles classify.
  Parameters as JSON (<cache>/deshadow/usgs/12/x/y.json), always computed with the cast term:
  every mode reuses them ("lambert" drops the cast term when it reads them).

Per source tile (z12..z16, called from imagery.Imagery._mosaic): the tile with an M px margin
from its neighbours, the grids sampled at its pixels, the shadow mask snapped onto the image's
own shadow edge (Bayes posterior with the blurred DEM prior, guided filter, linear mixing on the
terminator itself, halo guard; only on z14+ tiles within ~77 m of a terminator), the gain, chroma
smoothing where boosted. A tile whose terms show nothing to undo is decided before its neighbours
are fetched. Pixels whose gain is 1 keep their exact value. Results: memory LRU and
<cache>/imagery/usgs-ds<VERSION>-<mode>/z/x/y.png (".same" marker for unchanged tiles); the raw
tiles stay untouched. Until a region is ready its tiles stay raw, and a texture built meanwhile
keeps them until the tile is rebuilt (LOD change, rebase): there is no message to swap a live
tile's texture yet. render() waits up to WAIT_S for the regions within NEAR_M of a new anchor.
Regions within PREP_M are queued at an anchor, others when a tile asks; beyond REQUEST_M the tiles
stay raw. Source tiles coarser than z12 span several regions and stay raw.

"lambert" mode: slope shading only (no cast-shadow term). "full": both; imagery then lifts the
deepest shadows of relit texels less (tone_lut lift FULL_TONE_LIFT).
"""
import heapq
import json
import contextlib
import logging
import math
import os
import threading
import time
from collections import OrderedDict

import mercator
from dataclasses import dataclass

import numpy as np

import fast

import pacer
import staging
from PIL import Image

import cache as cachemod

log = logging.getLogger("earthd.deshadow")

VERSION = 1                         # bump: region values and relit tiles are recomputed
MODES = ("off", "lambert", "full")
SOURCE = "usgs"
TILE = 256
REGION_Z = 12                       # one sun fit / parameter set per z12 tile
GRID_Z = 14                         # region grid = the z14 pixel grid (DEM z13 512 px tiles: same pixels)
GRID_N = TILE << (GRID_Z - REGION_Z)    # 1024 cells per region side
MIN_Z, MAX_Z = 12, 16               # source zooms relit (coarser tiles span several regions: left raw)
EXT = 32                            # grid cells kept around the region: the margin of a z12 tile
M = 8                               # px of neighbouring tiles around a tile (filter support)
PAD_M = 1500.0                      # horizon march distance (m): cast shadows of a ~1000 m ridge
FIT_F = (4, 2)                      # sun fit: coarse and refined grid (in region cells)
EL_MIN, EL_MAX = 25.0, 80.0         # prior: aerial imagery is flown with a high sun (NAIP: >= 30 deg)
R2_MIN = 0.25                       # gate: luminance must follow lam * lit at the fitted sun
SEP_MIN = 0.9                       # gate: the image's own shadow cues must separate the DEM's classes
SOFT = 0.05                         # soft terminator (tan units): DEM noise
BLUR_CELLS = 10                     # ~77 m: expected misregistration (shadow-snap prior)
LAM_STRENGTH = 0.7                  # Lambert only partly trusted (bare-earth DEM vs canopy, registration)
LAM_CAP = 1.6
GMAX = 10.0                         # total gain cap (the prototype's; deep shadows need x8-10)
RHO_DEFAULT = (0.070, 0.072, 0.197)  # sky/sun per channel measured at the ravine (no edge in a region)
MIN_CLASS = 500                     # grid cells per shadow class needed to fit the classifier
MIN_EDGE = 300                      # edge-band cells needed to trust the region's own rho
FINE_Z, FINE_FD = 16, 2             # haze and rho refined on full-resolution tiles at shadow edges
FINE_TILES = 4
FINE_MIN_HAZE = 10000               # shadow px needed for the z16 haze
FINE_MIN_EDGE = 5000                # edge px per side needed for the z16 rho (the prototype's)
NOOP = 0.01                         # |lam * lit - sin el| below this everywhere: the tile is unchanged
FULL_TONE_LIFT = 0.5                # tone curve shadow lift for relit texels in "full" mode
WAIT_S = 30.0                       # render() waits this long for the regions around an anchor
NEAR_M = 2000.0                     # ... within this distance of it
PREP_M = 16000.0                    # regions queued at an anchor
REQUEST_M = 24000.0                 # regions further than this from the anchor stay raw
RETRY_S = 60.0                      # a region that failed (network) is tried again after this
MEM_TILES = 512
MEM_REGIONS = 16                    # regions whose grids stay decoded (3 x 1088^2 bytes each)
SAME = object()                     # LRU marker: the relit tile equals the raw one


# --- colour -----------------------------------------------------------------------------------
_x = np.arange(256) / 255.0
SRGB2LIN = np.where(_x <= 0.04045, _x / 12.92, ((_x + 0.055) / 1.055) ** 2.4).astype(np.float32)
LUT_N = 1 << 14


def to_srgb(lin):
    lin = np.clip(lin, 0, 1)
    s = np.where(lin <= 0.0031308, lin * 12.92, 1.055 * np.power(lin, 1 / 2.4) - 0.055)
    return np.clip(np.rint(s * 255), 0, 255).astype(np.uint8)


LIN2SRGB = to_srgb(np.arange(LUT_N) / (LUT_N - 1.0))


def to_lin(rgb):
    return SRGB2LIN[rgb]


def luma(lin):
    return lin[..., 0] * 0.2126 + lin[..., 1] * 0.7152 + lin[..., 2] * 0.0722


def shoulder(x):
    """Soft roll-off above 0.8 instead of a hard clip (boosted highlights)."""
    return np.where(x > 0.8, 0.8 + 0.2 * np.tanh((x - 0.8) / 0.2), x)


# --- filters ----------------------------------------------------------------------------------
_CNT = {}


def box(a, r):
    """Mean over a (2r+1)^2 box, edge-normalised (the window is cut at the array border): one
    integral image, edge-replicated so the four corner reads are plain slices."""
    a = np.asarray(a, np.float32)
    if r <= 0:
        return a
    n, m = a.shape
    S = np.zeros((n + 1 + 2 * r, m + 1 + 2 * r), np.float64)
    core = S[r + 1:r + 1 + n, r + 1:r + 1 + m]
    np.cumsum(a, axis=0, out=core)
    np.cumsum(core, axis=1, out=core)
    S[r + 1 + n:, :] = S[r + n, :]
    S[:, r + 1 + m:] = S[:, r + m:r + m + 1]
    k = 2 * r + 1
    tot = S[k:k + n, k:k + m] - S[:n, k:k + m] - S[k:k + n, :m] + S[:n, :m]
    key = (n, m, r)
    inv = _CNT.get(key)
    if inv is None:
        cr = np.minimum(np.arange(n) + r + 1, n) - np.maximum(np.arange(n) - r, 0)
        cc = np.minimum(np.arange(m) + r + 1, m) - np.maximum(np.arange(m) - r, 0)
        inv = _CNT[key] = 1.0 / np.outer(cr, cc)
    return (tot * inv).astype(np.float32)


def guided(I, p, r, eps):
    """He et al. guided filter: p smoothed along the edges of the guide I."""
    mI, mp = box(I, r), box(p, r)
    cov = box(I * p, r) - mI * mp
    var = box(I * I, r) - mI * mI
    a = cov / (var + eps)
    b = mp - a * mI
    return box(a, r) * I + box(b, r)


def down(a, f):
    """f x f block means (trailing rows / columns that do not fill a block are dropped)."""
    if f == 1:
        return a
    h, w = (a.shape[0] // f) * f, (a.shape[1] // f) * f
    a = a[:h, :w]
    return a.reshape(h // f, f, w // f, f, *a.shape[2:]).mean(axis=(1, 3))


# --- terrain ----------------------------------------------------------------------------------
def normals(h, res):
    """Unit normals (E, N, Up) of a north-up grid (row 0 = north)."""
    gy, gx = np.gradient(h.astype(np.float64), res)
    nx, ny = -gx, gy
    n = np.sqrt(nx * nx + ny * ny + 1.0)
    return (nx / n).astype(np.float32), (ny / n).astype(np.float32), (1.0 / n).astype(np.float32)


def lambert(nrm, az, el):
    a, e = math.radians(az), math.radians(el)
    return np.maximum(0.0, (nrm[0] * math.sin(a) + nrm[1] * math.cos(a)) * math.cos(e) + nrm[2] * math.sin(e))


HORIZON_BATCH = 16          # horizon_tan steps per Rust call (pacer.pace between them)


def horizon_tan(hpad, res, pad, n, az, max_dist=PAD_M, dmax=None, growth=1.04):
    """Max tangent of the terrain's elevation angle toward azimuth az, for the n x n window at
    (pad, pad) of the padded grid hpad. Steps grow geometrically from one cell; the march stops
    at max_dist, at the pad, or at dmax (beyond it nothing can rise above the sun). A fixed
    direction makes every step one shift of the whole grid: four slices, no gather."""
    best = np.full((n, n), -1e9, np.float32)
    s, c = math.sin(math.radians(az)), math.cos(math.radians(az))
    stop = min(max_dist, (pad - 1.5) * res, max_dist if dmax is None else dmax)
    d = res
    # Rust (earthfast.horizon_steps): no full-grid temporaries; paced between batches
    fast.require(1, "horizon_tan")
    hp = np.ascontiguousarray(hpad, np.float32)
    steps = []
    while d <= stop:
        dy, dx = -c * d / res, s * d / res
        iy, ix = math.floor(dy), math.floor(dx)
        steps.append((iy, ix, dy - iy, dx - ix, d))
        d = max(d + res, d * growth)
    for i in range(0, len(steps), HORIZON_BATCH):
        pacer.pace()
        fast._ef.horizon_steps(hp, best, pad, np.asarray(steps[i:i + HORIZON_BATCH], np.float64))
    return best


def march_limit(hpad, h, el):
    """Distance beyond which no terrain of hpad rises above elevation el seen from h."""
    return (float(hpad.max()) - float(h.min())) / math.tan(math.radians(el)) + 1.0


def terrain_terms(hpad, res, pad, n, az, el):
    """(lit, lam) on the n x n window: soft cast-shadow terminator and Lambert term."""
    h = hpad[pad:pad + n, pad:pad + n]
    ht = horizon_tan(hpad, res, pad, n, az, dmax=march_limit(hpad, h, el))
    lit = np.clip(0.5 + (math.tan(math.radians(el)) - ht) / (2 * SOFT), 0, 1)
    lam = lambert(normals(box(h, 1), res), az, el)
    return lit.astype(np.float32), lam.astype(np.float32)


# --- sun fit ----------------------------------------------------------------------------------
def otsu(x):
    hist, edges = np.histogram(x, 256)
    p = hist / hist.sum()
    w = np.cumsum(p)
    mu = np.cumsum(p * edges[:-1])
    sb = (mu[-1] * w - mu) ** 2 / np.maximum(w * (1 - w), 1e-12)
    return edges[np.argmax(sb)]


def score_grid(hpad, L, res, pad, azs, els):
    """(signed r^2, MCC) of each (az, el): luminance L (n x n) against lam * lit, and the predicted
    shadow (cast, or lam < 0.15) against the dark pixels (Otsu)."""
    n = L.shape[0]
    h = hpad[pad:pad + n, pad:pad + n]
    nx, ny, nz = normals(box(h, 1), res)
    Lc = (L - L.mean()).ravel()
    Lv = float((Lc ** 2).sum()) + 1e-12
    dark = (L < otsu(L)).ravel()
    nd = int(dark.sum())
    R2 = np.zeros((len(azs), len(els)))
    MCC = np.zeros_like(R2)
    dmax = march_limit(hpad, h, min(els))
    for i, az in enumerate(azs):
        ht = horizon_tan(hpad, res, pad, n, az, dmax=dmax).ravel()
        a = (nx * math.sin(math.radians(az)) + ny * math.cos(math.radians(az))).ravel()
        for j, el in enumerate(els):
            pacer.pace()
            lit = ht < math.tan(math.radians(el))
            dirc = np.maximum(0.0, a * math.cos(math.radians(el)) + nz.ravel() * math.sin(math.radians(el))) * lit
            dc = dirc - dirc.mean()
            cov = float(dc @ Lc)
            var = float(dc @ dc) + 1e-12
            r = cov / math.sqrt(var * Lv)
            R2[i, j] = r * abs(r)
            sh = ~lit | (dirc < 0.15)
            tp = int((sh & dark).sum())
            ns = int(sh.sum())
            fp, fn = ns - tp, nd - tp
            tn = sh.size - tp - fp - fn
            den = math.sqrt(float(tp + fp) * (tp + fn) * (tn + fp) * (tn + fn))
            MCC[i, j] = (tp * tn - fp * fn) / den if den > 0 else 0.0
    return R2, MCC


def fit_sun(hpad, L, res, pad):
    """Capture sun of a region: hpad the padded DEM and L the linear luminance (n x n), both on the
    region grid of cell size res (m). Coarse on FIT_F[0] cells, refined on FIT_F[1].
    Returns dict(az, el, r2, mcc)."""
    best = None
    for k, f in enumerate(FIT_F):
        pacer.pace()
        off = pad % f
        hp, Lf, pf = down(hpad[off:, off:], f), down(L, f), (pad - off) // f
        if k == 0:
            azs, els = np.arange(0.0, 360.0, 5.0), np.arange(EL_MIN, EL_MAX + 1e-6, 2.5)
        else:
            azs = (best[0] + np.arange(-10.0, 10.1, 2.0)) % 360
            els = np.arange(max(EL_MIN, best[1] - 5), min(EL_MAX, best[1] + 5) + 1e-6, 1.0)
        R2, MCC = score_grid(hp, Lf, res * f, pf, azs, els)
        S = R2 + 0.5 * MCC
        i, j = np.unravel_index(np.argmax(S), S.shape)
        best = (float(azs[i]), float(els[j]), float(R2[i, j]), float(MCC[i, j]))
    return dict(az=best[0], el=best[1], r2=best[2], mcc=best[3])


# --- region parameters ------------------------------------------------------------------------
def features(I, r=1):
    """Shadow cues per pixel: log luminance and the blue ratio (shadows are lit by the sky only)."""
    bs = np.stack([box(I[..., c], r) for c in range(3)], -1)
    return np.stack([np.log(luma(bs) + 1e-4), bs[..., 2] / (bs.sum(-1) + 1e-5)], -1)


def gauss_fit(X, m, maxs=20000):
    x = X[m]
    if len(x) > maxs:
        x = x[np.random.default_rng(0).choice(len(x), maxs, replace=False)]
    mu = x.mean(0)
    C = np.cov(x.T) + np.eye(2) * 1e-5
    return dict(mu=mu.tolist(), icov=np.linalg.inv(C).tolist(), logdet=float(np.log(np.linalg.det(C))),
                sd=float(math.sqrt(C[0, 0])))


def loglik(X, g):
    mu, A = g["mu"], g["icov"]
    d0, d1 = X[..., 0] - mu[0], X[..., 1] - mu[1]
    return -0.5 * (A[0][0] * d0 * d0 + (A[0][1] + A[1][0]) * d0 * d1 + A[1][1] * d1 * d1 + g["logdet"])


def guard_lum(I):
    """Per-pixel log luminance: what the halo guard compares (and its statistics; a 3 x 3 mean
    would let the sunlit side lift the first shadow pixel: a dark rim)."""
    return np.log(luma(I) + 1e-4)


def mix_edge(lit, L, band=1, r=4):
    """Pixels on the terminator are partly shadowed (penumbra, JPEG's chroma blur): within band px
    of the mask's edge, lit from the pixel's luminance between the local shadow and sun levels
    (linear mixing, means over (2r+1)^2) instead of the sharpened mask (a dark rim otherwise)."""
    m = (lit > 0.5).astype(np.float32)
    nb = box(m, band)
    edge = (nb > 0) & (nb < 1)
    if not edge.any():
        return lit
    s, l = (lit < 0.1).astype(np.float32), (lit > 0.9).astype(np.float32)
    ws, wl = box(s, r), box(l, r)
    Ls = box(L * s, r) / np.maximum(ws, 1e-6)
    Ll = box(L * l, r) / np.maximum(wl, 1e-6)
    ok = edge & (ws > 0.05) & (wl > 0.05) & (Ll > 1.5 * Ls)
    t = np.clip((L - Ls) / np.maximum(Ll - Ls, 1e-6), 0, 1)
    return np.where(ok, t, lit).astype(np.float32)


def snap_mask(lit_dem, blur, X, I, gs, gl, fd=1, guard=None):
    """Shadow mask snapped onto the image: posterior of the DEM prior (mask blurred by the expected
    misregistration, clipped so the image has the last word) and the classifier on features X
    (computed at 1/fd of I's resolution), then a guided filter on log luminance (r = 2) and a 3x
    sharpening around 0.5 (penumbra of a few px: no halo), linear mixing on the terminator itself
    (mix_edge), and the halo guard: a pixel brighter than any plausible shadow pixel (guard =
    (mean, sd) of guard_lum over shadow interiors at this resolution) is sunlit."""
    llr = np.clip(loglik(X, gs) - loglik(X, gl), -12, 12)
    if fd > 1:
        llr = np.repeat(np.repeat(llr, fd, 0), fd, 1)
    prior = np.clip(1 - blur, 0.03, 0.97)
    lit = 1 / (1 + np.exp(np.log(prior / (1 - prior)) + llr))
    Y = luma(I)
    lp = np.log(box(Y, 1) + 1e-3)
    g = (lp - lp.mean()) / (lp.std() + 1e-6) * 0.25
    lit = np.clip((guided(g, lit.astype(np.float32), 2, 0.01) - 0.5) * 3 + 0.5, 0, 1)
    lit = mix_edge(lit, Y)
    mu, sd = guard if guard is not None else (gs["mu"][0], gs["sd"])
    return np.maximum(lit, np.clip((guard_lum(I) - (mu + 2.5 * sd)) / 0.3, 0, 1)).astype(np.float32)


def edge_sides(lit, band, gap):
    """(shadow, sun) masks of the band along the cast-shadow terminator: both sides within band
    px of it, but at least gap px away (off the penumbra and the mask's own error)."""
    near = box((lit > 0.5).astype(np.float32), band)
    gm = box((lit > 0.5).astype(np.float32), gap)
    b = (near > 0.02) & (near < 0.98) & ((gm < 0.01) | (gm > 0.99))
    return b & (lit < 0.1), b & (lit > 0.9)


def rho_from_edge(shadow, sun, lam_sun):
    """rho_c = amb_c / sun_c from haze-free pixels on both sides of the terminator (same surfaces,
    so the albedo cancels): k_c = median shadow / median sun = rho_c / (rho_c + lam_e).
    Returns (rho or None, pixels per side)."""
    ne = int(min(len(shadow), len(sun)))
    if ne < 50:
        return None, ne
    k = np.clip(np.median(shadow, axis=0) / np.maximum(np.median(sun, axis=0), 1e-4), 0.03, 0.95)
    lam_e = float(np.median(lam_sun))
    return np.clip(k * lam_e / (1 - k), 0.01, 1.0), ne


def region_params(rgb, lit, lam_raw, el, cast=True):
    """Region values from the grid image rgb (n x n uint8) and the DEM terms at the fitted sun:
    blurred-mask prior, shadow classifier (separation gate), haze, rho. Returns the params dict."""
    I = to_lin(rgb)
    sz = math.sin(math.radians(el))
    lam = sz + LAM_STRENGTH * (lam_raw - sz)
    blur = box((lit > 0.5).astype(np.float32), BLUR_CELLS)     # the shadow-snap prior
    P = dict(cast=False, sep=None, gs=None, gl=None)
    snapped = lit
    if cast:
        S, Lm = blur < 0.01, blur > 0.99
        if S.sum() < MIN_CLASS or Lm.sum() < MIN_CLASS:
            P["cast_reason"] = "no cast shadow"
        else:
            X = features(I, 0)
            gs, gl = gauss_fit(X, S), gauss_fit(X, Lm)
            llr = loglik(X, gs) - loglik(X, gl)
            sep = 0.5 * ((llr[S] > 0).mean() + (llr[Lm] < 0).mean())
            P["sep"] = float(sep)
            if sep >= SEP_MIN:
                gq = guard_lum(I)[S]
                P.update(cast=True, gs=gs, gl=gl, guard=[float(gq.mean()), float(gq.std())])
                snapped = snap_mask(lit, blur, X, I, gs, gl, guard=P["guard"])
            else:
                P["cast_reason"] = "image does not confirm the DEM shadows"
    else:
        P["cast_reason"] = "lambert mode"
    shm = snapped < 0.5
    haze = 0.9 * np.percentile(I[shm] if shm.sum() > 2000 else I.reshape(-1, 3), 0.5, axis=0)
    rho, ne = (None, 0)
    if P["cast"]:
        s, l = edge_sides(snapped, 4, 1)            # 31 m band, 7.7 m gap
        Ih = np.maximum(I - haze, 0)
        rho, ne = rho_from_edge(Ih[s], Ih[l], lam[l])
    ok = rho is not None and ne >= MIN_EDGE
    P.update(haze=[float(v) for v in haze], n_edge=ne, rho=[float(v) for v in (rho if ok else RHO_DEFAULT)],
             rho_src="edge14" if ok else "default", haze_src="z14")
    return P


def edge_tiles(grids, n_core=GRID_N, count=FINE_TILES, z=FINE_Z):
    """(column, row) within the region of the zoom-z tiles sampled at full resolution: up to
    `count` holding the most cells of the shadow-edge band (the snap prior between 0.05 and
    0.95), plus the one with the most shadow-interior and the one with the most sunlit-interior
    cells (the classifier and the guard are fitted on interiors)."""
    c = TILE >> (z - GRID_Z)                   # grid cells per tile side
    blur = grids[EXT:EXT + n_core, EXT:EXT + n_core, 2]
    m = n_core // c
    out = []
    for sel, k, need in (((blur > 12) & (blur < 243), count, 0.05), (blur < 3, 1, 0.5), (blur > 252, 1, 0.5)):
        t = sel[:m * c, :m * c].astype(np.float32).reshape(m, c, m, c).sum(axis=(1, 3))
        for j in np.argsort(-t.ravel(), kind="stable")[:k + len(out)]:
            cr = (int(j % m), int(j // m))
            if t.flat[j] >= need * c * c and cr not in out and k > 0:
                out.append(cr)
                k -= 1
    return out


def core_of(margin):
    return (slice(margin, -margin or None),) * 2


def refine_fine(P, samples, margin=M):
    """The shadow classifier, the halo guard, haze and rho again from full-resolution tiles at
    shadow edges (samples = [(rgb_m, terms)] as relight_tile takes them): z14 averages away the
    dark objects, blurs the terminator and has less texture variance than the pixels the tiles
    classify. Updates P (each value only if the tiles hold enough pixels for it)."""
    if not samples:
        return P
    sz = math.sin(math.radians(P["el"]))
    core = (slice(margin, -margin or None),) * 2
    # the shadow classifier and the guard at the resolution the tiles use them
    Xs, Bs, Gs, Ss = [], [], [], []
    for rgb_m, terms in samples:
        I = to_lin(rgb_m)
        Xs.append(features(down(I, FINE_FD), 1)[core_of(margin // FINE_FD)].reshape(-1, 2))
        Bs.append(down(terms[..., 2], FINE_FD)[core_of(margin // FINE_FD)].ravel())
        Gs.append(guard_lum(I)[core].ravel())
        Ss.append(terms[..., 2][core].ravel() < 0.01)
    X, B = np.concatenate(Xs)[:, None, :], np.concatenate(Bs)
    S, Lm = B < 0.01, B > 0.99
    if S.sum() >= 4 * MIN_CLASS and Lm.sum() >= 4 * MIN_CLASS:
        gs, gl = gauss_fit(X[:, 0], S), gauss_fit(X[:, 0], Lm)
        llr = (loglik(X, gs) - loglik(X, gl))[:, 0]
        sep = 0.5 * ((llr[S] > 0).mean() + (llr[Lm] < 0).mean())
        P["sep_z16"] = float(sep)
        if sep >= SEP_MIN:
            gq = np.concatenate(Gs)[np.concatenate(Ss)]
            P.update(gs=gs, gl=gl, guard=[float(gq.mean()), float(gq.std())], classes_src="z16")
    Is, lits, lams = [], [], []
    for rgb_m, terms in samples:
        lit, _, I = tile_lit(rgb_m, terms, P, FINE_FD)
        Is.append((to_lin(rgb_m) if I is None else I)[core])
        lits.append(lit[core])
        lams.append((sz + LAM_STRENGTH * (terms[..., 1] - sz))[core])
    if not Is:
        return P
    shadow = np.concatenate([I[lit < 0.5] for I, lit in zip(Is, lits)])
    if len(shadow) >= FINE_MIN_HAZE:
        haze = 0.9 * np.percentile(shadow, 0.5, axis=0)
        P.update(haze=[float(v) for v in haze], haze_src="z16")
    haze = np.asarray(P["haze"], np.float32)
    S, L, LS = [], [], []
    for I, lit, lam in zip(Is, lits, lams):
        s, l = edge_sides(lit, 16, 4)               # the prototype's band: 4-16 px at 1.9 m
        Ih = np.maximum(I - haze, 0)
        S.append(Ih[s]), L.append(Ih[l]), LS.append(lam[l])
    rho, ne = rho_from_edge(np.concatenate(S), np.concatenate(L), np.concatenate(LS))
    P["n_edge_z16"] = ne
    if rho is not None and ne >= FINE_MIN_EDGE:
        P.update(rho=[float(v) for v in rho], rho_src="edge16")
    return P


# --- per-tile relight -------------------------------------------------------------------------
def gain_field(lam, direct, rho, sz):
    """Per-pixel, per-channel gain (h, w, 3): flat-ground sun over the capture light, the Lambert
    part capped at LAM_CAP; the total capped at GMAX with all channels scaled together."""
    gain = np.empty(lam.shape + (3,), np.float32)
    for c in range(3):                  # per channel: numpy reduces a size-3 last axis slowly
        r = float(rho[c])
        gain[..., c] = np.clip((r + sz) / (r + lam), 1 / LAM_CAP, LAM_CAP) * (r + lam) / (r + direct)
    gm = np.maximum(np.maximum(gain[..., 0], gain[..., 1]), gain[..., 2])
    gain *= np.minimum(1.0, GMAX / gm)[..., None]
    return gain


def chroma_smooth(out, w, r=3):
    """Boosted shadows carry JPEG chroma noise x gain: keep their luminance detail (half-way to a
    3x3 mean), take the chroma from a (2r+1)^2 mean; w = how much the pixel was boosted."""
    Y = luma(out)
    Y = (Y + w * 0.5 * (box(Y, 1) - Y))[..., None]
    bs = np.stack([box(out[..., c], r) for c in range(3)], -1)
    cs = bs / np.maximum(luma(bs)[..., None], 1e-5)
    w = w[..., None]
    return out + w * (cs * Y - out)


def tile_lit(rgb_m, terms, P, fd=0):
    """Lit fraction at a tile's pixels (with margin): 1 without the cast term, the DEM mask far
    from a terminator or without a snap (fd 0), else the mask snapped onto the image. Returns
    (lit, path: 'lambert' | 'fast' | 'edge', linear image or None)."""
    lit_dem, blur = terms[..., 0], terms[..., 2]
    if not P["cast"]:
        return np.ones_like(lit_dem), "lambert", None
    if blur.min() > 0.99 or blur.max() < 0.01 or not fd:
        return lit_dem, "fast", None        # no terminator within ~77 m of this tile
    I = to_lin(rgb_m)
    Ih = down(I, fd) if fd > 1 else I
    return snap_mask(lit_dem, blur, features(Ih, 1), I, P["gs"], P["gl"], fd, P.get("guard")), "edge", I


def unchanged(terms, P, margin=M):
    """True if the tile is unchanged whatever its pixels (no shadow edge to snap, flat and lit):
    decided from the terms alone, before its neighbours are fetched."""
    sz = math.sin(math.radians(P["el"]))
    core = (slice(margin, -margin or None),) * 2
    lam = sz + LAM_STRENGTH * (terms[core + (1,)] - sz)
    if not P["cast"]:
        lit = 1.0
    elif terms[..., 2].min() > 0.99:
        lit = terms[core + (0,)]
    else:
        return False
    return bool(np.abs(lam * lit - sz).max() < NOOP)


def relight_tile(rgb_m, terms, P, fd=0, margin=M):
    """Relight one tile. rgb_m: the tile with `margin` px of its neighbours (uint8 (n+2m)^2 x 3);
    terms: lit, lam, blur at its pixels (float32 (n+2m)^2 x 3); P: region params; fd: shadow-snap
    feature downsampling (0 = no snap: the DEM mask as is). Returns (uint8 tile or None if
    unchanged, path: 'noop' | 'lambert' | 'fast' | 'edge')."""
    sz = math.sin(math.radians(P["el"]))
    lam = sz + LAM_STRENGTH * (terms[..., 1] - sz)
    core = (slice(margin, -margin or None),) * 2
    lit, path, I = tile_lit(rgb_m, terms, P, fd)
    direct = lam * lit
    if np.abs(direct[core] - sz).max() < NOOP:
        return None, "noop"
    if I is None:
        I = to_lin(rgb_m)
    gain = gain_field(lam, direct, P["rho"], sz)
    haze = np.asarray(P["haze"], np.float32)
    out = I + (gain - 1) * np.maximum(I - haze, 0)
    gl_ = luma(gain)
    if gl_.max() > 1.5:                 # only boosted shadows carry amplified chroma noise
        out = chroma_smooth(out, np.clip((gl_ - 1) / 3, 0, 1))
    out, gain, rgb = out[core], gain[core], rgb_m[core]
    gmax = np.maximum(np.maximum(gain[..., 0], gain[..., 1]), gain[..., 2])
    gmin = np.minimum(np.minimum(gain[..., 0], gain[..., 1]), gain[..., 2])
    hi = (gmax > 1) & (np.maximum(np.maximum(out[..., 0], out[..., 1]), out[..., 2]) > 0.8)
    if hi.any():                        # boosted highlights: soft shoulder, faded in with the boost
        o = out[hi]
        out[hi] = o + np.clip((gmax[hi, None] - 1) / 0.1, 0, 1) * (shoulder(o) - o)
    res = LIN2SRGB[np.clip(out * (LUT_N - 1) + 0.5, 0, LUT_N - 1).astype(np.int32)]
    same = (gmax <= 1 + 1e-3) & (gmin >= 1 - 1e-3)
    res[same] = rgb[same]                # gain 1: the exact source value (no requantisation)
    return res, path


# --- regions (I/O, background worker) ---------------------------------------------------------
@dataclass
class Region:
    params: dict
    grids: object = None                # uint8 (G, G, 3): lit, lam, blur x 255; None until loaded / when off


def region_of(z, x, y):
    return x >> (z - REGION_Z), y >> (z - REGION_Z)


def lonlat_of_px(X, Y, z):
    """lon, lat of global Web-Mercator pixel coordinates at zoom z."""
    w = TILE * 2.0 ** z
    lon = np.asarray(X, np.float64) / w * 360.0 - 180.0
    lat = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * np.asarray(Y, np.float64) / w))))
    return lon, lat


def res_at(lat, z):
    return 40075016.686 * math.cos(math.radians(lat)) / (TILE * 2 ** z)


def grid_pad(res):
    """DEM cells beyond the region's EXT ring for the horizon march (PAD_M), rounded so that the
    sun fit's coarser grids start on a block boundary."""
    pad = int(math.ceil(PAD_M / res)) + 4
    return pad + (-(pad + EXT)) % max(FIT_F)


def compute_region(rgb, hpad, res, pad, cast=True, fine=None):
    """All region values from the core grid image rgb (GRID_N^2) and the DEM on the grid with
    pad + EXT cells around it; fine(params, grids) -> [(rgb_m, terms)] of full-resolution tiles
    at shadow edges (edge_tiles) refines haze and rho. Returns (params, grids uint8 or None)."""
    t0 = time.perf_counter()
    n = rgb.shape[0]
    pe = pad + EXT
    L = luma(to_lin(rgb))
    sun = fit_sun(hpad, L, res, pe)
    t1 = time.perf_counter()
    P = dict(version=VERSION, **sun, ok=sun["r2"] >= R2_MIN, cast=False, res=res)
    if not P["ok"]:
        P.update(reason=f"r2 {sun['r2']:.2f} < {R2_MIN}", fit_s=t1 - t0, total_s=time.perf_counter() - t0)
        return P, None
    G = n + 2 * EXT
    lit, lam = terrain_terms(hpad, res, pad, G, sun["az"], sun["el"])
    core = (slice(EXT, EXT + n),) * 2
    rp = region_params(rgb, lit[core], lam[core], sun["el"], cast)
    blur = box((lit > 0.5).astype(np.float32), BLUR_CELLS)
    P.update(rp)
    grids = np.rint(np.stack([lit, np.clip(lam, 0, 1), blur], -1) * 255).astype(np.uint8)
    if P["cast"] and fine is not None:
        refine_fine(P, fine(P, grids))
    P.update(fit_s=t1 - t0, total_s=time.perf_counter() - t0)
    return P, grids


def sample_grids(grids, rx, ry, z, x, y, margin=M, n_core=GRID_N):
    """lit, lam, blur (float32 (256+2m)^2 x 3) at the pixels of source tile (z, x, y) plus margin,
    from its region's grids (bilinear for z >= GRID_Z, block means below). n_core: region cells
    per side (tests use smaller regions)."""
    n = TILE + 2 * margin
    gx0, gy0 = rx * n_core - EXT, ry * n_core - EXT         # global grid index of cell 0
    if z < GRID_Z:                      # a pixel covers f x f cells: their mean
        f = 2 ** (GRID_Z - z)
        c0x, c0y = (x * TILE - margin) * f - gx0, (y * TILE - margin) * f - gy0
        win = grids[max(c0y, 0):c0y + n * f, max(c0x, 0):c0x + n * f]
        pads = ((max(-c0y, 0), n * f - win.shape[0] - max(-c0y, 0)),
                (max(-c0x, 0), n * f - win.shape[1] - max(-c0x, 0)), (0, 0))
        if any(p != (0, 0) for p in pads):
            win = np.pad(win, pads, mode="edge")
        return (down(win.astype(np.float32), f) * (1 / 255.0)).astype(np.float32)
    k = 2 ** (z - GRID_Z)
    j = np.arange(n)
    u = (x * TILE - margin + j + 0.5) / k - 0.5 - gx0
    v = (y * TILE - margin + j + 0.5) / k - 0.5 - gy0
    ix = np.clip(np.floor(u).astype(np.int64), 0, grids.shape[1] - 2)
    iy = np.clip(np.floor(v).astype(np.int64), 0, grids.shape[0] - 2)
    fx = np.clip(u - ix, 0, 1).astype(np.float32)[None, :, None]
    fy = np.clip(v - iy, 0, 1).astype(np.float32)[:, None, None]
    r0, c0 = int(iy[0]), int(ix[0])                         # only the window is converted
    g = grids[r0:int(iy[-1]) + 2, c0:int(ix[-1]) + 2].astype(np.float32) * (1 / 255.0)
    iy, ix = iy - r0, ix - c0
    rows = g[iy] * (1 - fy) + g[iy + 1] * fy                # separable: rows first (few columns)
    return rows[:, ix] * (1 - fx) + rows[:, ix + 1] * fx


def _atomic_write(path, write):
    """write(file object) to a temporary file, then rename it to path."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{threading.get_ident()}.tmp"
    with open(tmp, "wb") as f:
        write(f)
    staging.replace_cached(tmp, path)


class Deshadow:
    """Region values (background thread) and per-tile relight for one Imagery (bind())."""
    disk = cachemod.NONE    # the server's cache.Cache (last use per file, the size cap); set by earthd
    source, version = SOURCE, VERSION       # (the finished pyramid's settings: imagery.Imagery._fin_sig)

    def __init__(self, cache_dir, dem, mode="full", mem_tiles=MEM_TILES, wait_s=WAIT_S):
        if mode not in MODES[1:]:
            raise ValueError(f"deshadow mode {mode!r}: choose from {', '.join(MODES[1:])}")
        self.mode = mode
        self.cast = mode == "full"
        self.tone_lift = FULL_TONE_LIFT if self.cast else 1.0
        self.dem = dem
        self.imagery = None
        self.region_dir = os.path.join(cache_dir, "deshadow", SOURCE, str(REGION_Z))
        self.tile_dir = os.path.join(cache_dir, "imagery", f"{SOURCE}-ds{VERSION}-{mode}")
        self.mem_tiles = mem_tiles
        self.wait_s = wait_s
        self._tl = threading.local()        # no_wait(): this thread's renders never wait for a region
        self._lock = threading.Lock()
        self._lru = OrderedDict()           # (z, x, y) -> uint8 tile or SAME
        self._params = {}                   # (rx, ry) -> params of a ready region
        self._grids = OrderedDict()         # (rx, ry) -> uint8 grids (decoded LRU)
        self._events = {}                   # (rx, ry) -> Event, set when the region is done (or failed)
        self._failed = {}                   # (rx, ry) -> monotonic time of the failure
        self._wait_until = {}               # (rx, ry) -> monotonic deadline for waiting renders
        self._queue = []                    # heap of (distance m, seq, (rx, ry))
        self._seq = 0
        self._center = None                 # (lat, lon) of the last anchor
        self._cv = threading.Condition(self._lock)
        self._closed = False
        self._listeners = []
        self.stats = {}
        self._thread = threading.Thread(target=self._worker, name="deshadow", daemon=True)
        self._thread.start()

    def bind(self, imagery):
        self.imagery = imagery

    def add_listener(self, fn):
        """fn(rk, changed) from the worker thread once a region is ready (changed: it relights, so
        textures rendered raw over it while it was pending differ from what they would be now)."""
        self._listeners.append(fn)

    def _count(self, what, n=1):
        with self._lock:
            self.stats[what] = self.stats.get(what, 0) + n

    def summary(self):
        with self._lock:
            ready = sum(1 for p in self._params.values() if p.get("ok"))
            return (f"deshadow {self.mode}: regions {ready} relit / {len(self._params)} ready, "
                    f"{len(self._queue)} queued; tiles {dict(sorted(self.stats.items()))}")

    # --- region bookkeeping --------------------------------------------------------------------
    def _paths(self, rk):
        d = os.path.join(self.region_dir, str(rk[0]))
        return os.path.join(d, f"{rk[1]}.json"), os.path.join(d, f"{rk[1]}.npz")

    def _load_params(self, rk):
        """Params from the disk cache (current VERSION only). Caller holds the lock."""
        if rk in self._params:
            return self._params[rk]
        jp, npz = self._paths(rk)
        try:
            with open(jp) as f:
                P = json.load(f)
        except (OSError, ValueError):
            return None
        if P.get("version") != VERSION or (P.get("ok") and not os.path.exists(npz)):
            return None
        self.disk.used(jp)
        self._params[rk] = P
        return P

    def _region_center(self, rk):
        lon, lat = lonlat_of_px((rk[0] + 0.5) * TILE, (rk[1] + 0.5) * TILE, REGION_Z)
        return float(lat), float(lon)

    def _distance(self, rk):
        if self._center is None:
            return 0.0
        lat, lon = self._region_center(rk)
        la0, lo0 = self._center
        dy = (lat - la0) * 111320.0
        dx = (lon - lo0) * 111320.0 * math.cos(math.radians(la0))
        return math.hypot(dx, dy)

    def _enqueue(self, rk):
        """Queue a region (once). Caller holds the lock."""
        if rk in self._events or self._load_params(rk) is not None:
            return
        f = self._failed.get(rk)
        if f is not None and time.monotonic() - f < RETRY_S:
            return
        self._failed.pop(rk, None)
        self._events[rk] = threading.Event()
        self._seq += 1
        heapq.heappush(self._queue, (self._distance(rk), self._seq, rk))
        self._cv.notify()

    def prepare(self, lat, lon, radius=PREP_M):
        """Anchor set: queue the regions within radius, nearest first (renders wait up to wait_s
        for the ones within NEAR_M)."""
        z = REGION_Z
        w = TILE * 2 ** z
        with self._lock:
            self._center = (lat, lon)
            self._queue = sorted((self._distance(rk), s, rk) for _, s, rk in self._queue)
        if abs(lat) >= mercator.MAX_LAT:        # no Web-Mercator imagery to correct (the poles)
            return
        lat_r = math.radians(lat)
        x = (lon / 360.0 + 0.5) * w
        y = (0.5 - math.asinh(math.tan(lat_r)) / (2 * math.pi)) * w
        rpx = radius / res_at(lat, z)
        with self._lock:
            now = time.monotonic()
            for ry in range(max(0, int((y - rpx) // TILE)), min(2 ** z - 1, int((y + rpx) // TILE)) + 1):
                for rx in mercator.columns(x, rpx, TILE, 2 ** z):
                    rk = (rx % 2 ** z, ry)
                    if not 0 <= ry < 2 ** z:
                        continue
                    d = self._distance(rk)
                    if d > radius + 6000:
                        continue
                    self._enqueue(rk)
                    if d <= NEAR_M + 6000 and self._near(rk, x, y, lat):
                        self._wait_until[rk] = now + self.wait_s

    @staticmethod
    def _near(rk, x, y, lat):
        """The region's square comes within NEAR_M of the point (global z12 px x, y)."""
        r = NEAR_M / res_at(lat, REGION_Z)
        dx = max(rk[0] * TILE - x, 0, x - (rk[0] + 1) * TILE)
        dy = max(rk[1] * TILE - y, 0, y - (rk[1] + 1) * TILE)
        return math.hypot(dx, dy) <= r

    def region(self, rk, wait=True):
        """Region (params, grids) if ready; else None (queued; waits up to the anchor deadline)."""
        with self._lock:
            P = self._load_params(rk)
            if P is None:
                if self._center is not None and self._distance(rk) > REQUEST_M:
                    return None
                self._enqueue(rk)
                ev = self._events.get(rk)
                deadline = self._wait_until.get(rk, 0.0)
        if P is None:
            left = deadline - time.monotonic()
            if not wait or ev is None or left <= 0 or not ev.wait(left):
                return None
            with self._lock:
                P = self._params.get(rk)
            if P is None:
                return None
        if not P.get("ok"):
            return Region(P)
        if not self.cast and P.get("cast"):
            P = dict(P, cast=False)         # lambert mode: the slope term only
        with self._lock:
            g = self._grids.get(rk)
            if g is not None:
                self._grids.move_to_end(rk)
                return Region(P, g)
        npz = self._paths(rk)[1]
        try:
            with np.load(npz) as f:
                g = f["grids"]
        except (OSError, ValueError, KeyError):
            with self._lock:            # evicted from the disk cache (cache.py): computed again
                self._params.pop(rk, None)
            return None
        self.disk.used(npz)
        with self._lock:
            self._grids[rk] = g
            while len(self._grids) > MEM_REGIONS:
                self._grids.popitem(last=False)
        return Region(P, g)

    @contextlib.contextmanager
    def no_wait(self):
        """Renders on this thread take a region that is not ready as pending at once (the block's base
        photo: a block answer must not wait for an analysis; earthd renders it again once it is done)."""
        old = getattr(self._tl, "nowait", False)
        self._tl.nowait = True
        try:
            yield
        finally:
            self._tl.nowait = old

    def pending_region(self, key, tile):
        """The region of a source tile whose relight is still to come: its region is not ready but
        wanted (within REQUEST_M of the anchor), else None. Tile workers call it BEFORE relight(),
        so a region finishing in between is reported pending (one extra re-render) rather than
        missed (a texture left raw for good)."""
        src, z, x, y = key
        if getattr(src, "name", src) != SOURCE or not MIN_Z <= z <= MAX_Z or tile is None or tile.uniform:
            return None
        rk = region_of(z, x, y)
        with self._lock:
            if self._load_params(rk) is not None:
                return None
            if self._center is not None and self._distance(rk) > REQUEST_M:
                return None
        return rk

    def status(self, rk):
        """None (not ready), False (ready, leaves the imagery as it is) or True (ready, relights).
        On earthd's event loop: memory, else the region file's existence (a stat call). A region that
        failed (tried again later) is not ready: a file it has is one renders cannot use (another
        VERSION), else it would not have been computed; calling it ready made earthd re-render a
        shown tile in a loop (the render reports it pending again)."""
        with self._lock:
            P = self._params.get(rk)
            if P is not None:
                return bool(P.get("ok"))
            if rk in self._events or rk in self._failed:
                return None
        return True if os.path.exists(self._paths(rk)[0]) else None

    def _worker(self):
        pacer.enable(pacer.DUTY)     # a background analysis: paced (pacer.py)
        while True:
            with self._lock:
                while not self._queue and not self._closed:
                    self._cv.wait()
                if self._closed:
                    return
                _, _, rk = heapq.heappop(self._queue)
            pacer.idle()                # the wait above was not busy time
            try:
                P, grids = self._compute(rk)
            except Exception as e:      # network / DEM trouble: tried again later
                if self._closed:
                    return
                # deferred: the imagery host's bucket is kept for demand (a cold start): not a fault
                (log.debug if getattr(e, "deferred", False) else log.warning)(
                    "deshadow region %s: %s: %s", rk, type(e).__name__, e)
                with self._lock:
                    self._failed[rk] = time.monotonic()
                    self._events.pop(rk).set()
                continue
            jp, npz = self._paths(rk)
            if grids is not None:
                _atomic_write(npz, lambda f: np.savez_compressed(f, grids=grids))
                self.disk.wrote(npz)
            _atomic_write(jp, lambda f: f.write(json.dumps(P).encode()))
            self.disk.wrote(jp)
            with self._lock:
                self._params[rk] = P
                if grids is not None:
                    self._grids[rk] = grids
                    while len(self._grids) > MEM_REGIONS:
                        self._grids.popitem(last=False)
                self._events.pop(rk).set()
            if P.get("ok"):
                log.info("deshadow region %s: sun az %.0f el %.1f (r2 %.2f, mcc %.2f), cast %s (sep %s%s), "
                         "rho %s (%s), haze %s; fit %.1f s, total %.1f s", rk, P["az"], P["el"], P["r2"], P["mcc"],
                         "on" if P["cast"] else "off", "-" if P["sep"] is None else f"{P['sep']:.2f}",
                         "" if P["cast"] else f": {P.get('cast_reason')}", [round(v, 3) for v in P["rho"]],
                         P["rho_src"], [round(v, 4) for v in P["haze"]], P["fit_s"], P["total_s"])
            else:
                log.info("deshadow region %s: left unchanged (%s; sun az %s el %s), %.1f s", rk, P.get("reason"),
                         P.get("az"), P.get("el"), P.get("total_s", 0.0))
            for fn in list(self._listeners):
                try:
                    fn(rk, bool(P.get("ok")))
                except Exception:
                    log.exception("deshadow listener")

    def _compute(self, rk):
        """Fetch the region's z14 imagery and DEM, then compute_region()."""
        im = self.imagery
        src = im.chain[[s.name for s in im.chain].index(SOURCE)]
        k = GRID_N // TILE
        keys = [(src, GRID_Z, rk[0] * k + i, rk[1] * k + j) for j in range(k) for i in range(k)]
        got = im.tiles(keys, kind="background")     # never the demand budget (netfetch)
        lat_c, _ = self._region_center(rk)
        if any(got[key] is None or got[key].uniform for key in keys):
            return dict(version=VERSION, ok=False, cast=False, reason="no full imagery coverage"), None
        rgb = np.zeros((GRID_N, GRID_N, 3), np.uint8)
        for (_, _, x, y) in keys:
            rgb[(y - rk[1] * k) * TILE:(y - rk[1] * k + 1) * TILE, (x - rk[0] * k) * TILE:(x - rk[0] * k + 1) * TILE] = \
                got[(src, GRID_Z, x, y)].rgb
        res = res_at(lat_c, GRID_Z)
        pad = grid_pad(res)
        pe = pad + EXT
        idx = np.arange(-pe, GRID_N + pe) + 0.5
        lon, _ = lonlat_of_px(rk[0] * GRID_N + idx, 0, GRID_Z)
        _, lat = lonlat_of_px(0, rk[1] * GRID_N + idx, GRID_Z)
        hpad = self.dem.sample(lat[:, None], lon[None, :], GRID_Z - 1).astype(np.float32)

        def fine(P, grids):             # z16 tiles at the shadow edges, for haze, rho and the classifier
            k = 2 ** (FINE_Z - REGION_Z)
            fk = [(src, FINE_Z, rk[0] * k + c, rk[1] * k + r) for c, r in edge_tiles(grids)]
            fgot = im.tiles(fk, kind="background")
            return [(np.pad(fgot[key].rgb, ((M, M), (M, M), (0, 0)), mode="edge"),
                     sample_grids(grids, rk[0], rk[1], FINE_Z, key[2], key[3]))
                    for key in fk if fgot[key] is not None and not fgot[key].uniform]
        return compute_region(rgb, hpad, res, pad, True, fine)     # cached for every mode

    # --- tiles ---------------------------------------------------------------------------------
    def _tile_paths(self, z, x, y):
        d = os.path.join(self.tile_dir, str(z), str(x))
        return os.path.join(d, f"{y}.png"), os.path.join(d, f"{y}.same")

    def relight(self, key, tile, imagery):
        """Relit RGB of a decoded source tile, or None to use it as is (other source or zoom, blank,
        region not ready / gated off, nothing to undo)."""
        return self.relight_final(key, tile, imagery)[0]

    def relight_final(self, key, tile, imagery):
        """(relight(), state): "final" (the tile's answer for good), ("wait", rk) (its region rk is not ready, or
        beyond REQUEST_M of the anchor: raw for now; poll(rk) says when that changes) or "error" (a neighbour
        could not be fetched: raw this time). The finished pyramid (imagery.Imagery.finished) keeps only final
        answers for good."""
        src, z, x, y = key
        if getattr(src, "name", src) != SOURCE or not MIN_Z <= z <= MAX_Z or tile.uniform:
            return None, "final"
        ck = (z, x, y)
        with self._lock:
            v = self._lru.get(ck)
            if v is not None:
                self._lru.move_to_end(ck)
                return (None if v is SAME else v), "final"
        rk = region_of(z, x, y)
        R = self.region(rk, wait=not getattr(self._tl, "nowait", False))
        if R is None:
            self._count("pending")
            return None, ("wait", rk)
        if not R.params.get("ok"):
            self._count("off")
            return None, "final"
        png, same = self._tile_paths(z, x, y)
        v = None
        if os.path.exists(same):
            v = SAME
            self.disk.used(same)
        elif os.path.exists(png):
            try:
                v = np.asarray(Image.open(png).convert("RGB"))
                self.disk.used(png)
            except (OSError, ValueError):
                v = None
        if v is None:
            v = self._relight_new(key, tile, imagery, R, rk)
            if v is None:
                return None, "error"
            _atomic_write(same if v is SAME else png,
                          (lambda f: None) if v is SAME else
                          (lambda f: Image.fromarray(v, "RGB").save(f, "PNG", compress_level=1)))
            self.disk.wrote(same if v is SAME else png)
        else:
            self._count("disk")
        with self._lock:
            self._lru[ck] = v
            while len(self._lru) > self.mem_tiles:
                self._lru.popitem(last=False)
        return (None if v is SAME else v), "final"

    def poll(self, rk):
        """A region relight_final left a tile raw for: "ready" (its tile is to be relit again: the region is
        ready, waited for as relight would), "wait" (wanted, queued) or "far" (beyond REQUEST_M of the anchor)."""
        if self.region(rk, wait=not getattr(self._tl, "nowait", False)) is not None:
            return "ready"
        with self._lock:
            far = self._center is not None and self._distance(rk) > REQUEST_M
        return "far" if far else "wait"

    def _relight_new(self, key, tile, imagery, R, rk):
        src, z, x, y = key
        t0 = time.perf_counter()
        terms = sample_grids(R.grids, rk[0], rk[1], z, x, y)
        if unchanged(terms, R.params):
            self._count("noop")
            return SAME
        n = 2 ** z
        nb = {(dx, dy): (src, z, (x + dx) % n, y + dy) for dy in (-1, 0, 1) for dx in (-1, 0, 1)
              if 0 <= y + dy < n and (dx, dy) != (0, 0)}
        try:
            got = imagery.tiles(nb.values())
        except Exception as e:          # ImageryError: not cached, tried again next time
            self._count("neighbour-error")
            log.debug("deshadow tile %s: neighbours: %s", key[1:], e)
            return None
        big = np.zeros((3 * TILE, 3 * TILE, 3), np.uint8)
        big[TILE:2 * TILE, TILE:2 * TILE] = tile.rgb
        have = np.zeros((3, 3), bool)
        have[1, 1] = True
        for (dx, dy), k in nb.items():
            t = got.get(k)
            if t is not None and not t.uniform:
                big[(dy + 1) * TILE:(dy + 2) * TILE, (dx + 1) * TILE:(dx + 2) * TILE] = t.rgb
                have[dy + 1, dx + 1] = True
        rgb_m = big[TILE - M:2 * TILE + M, TILE - M:2 * TILE + M]
        if not have.all():                  # missing neighbours: the tile's edge, replicated
            core = np.pad(tile.rgb, ((M, M), (M, M), (0, 0)), mode="edge")
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    if not have[dy + 1, dx + 1]:
                        rs = slice(0, M) if dy < 0 else slice(M + TILE, None) if dy > 0 else slice(M, M + TILE)
                        cs = slice(0, M) if dx < 0 else slice(M + TILE, None) if dx > 0 else slice(M, M + TILE)
                        rgb_m[rs, cs] = core[rs, cs]
        fd = 0 if z < GRID_Z else max(1, 2 ** (z - FINE_Z + 1))
        out, path = relight_tile(rgb_m, terms, R.params, fd)
        dt = time.perf_counter() - t0
        self._count(path)
        self._count("ms:" + path, round(dt * 1000, 1))
        return SAME if out is None else out

    def close(self):
        with self._lock:
            self._closed = True
            self._cv.notify_all()
