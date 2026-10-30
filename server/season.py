"""Seasons in the imagery (the user, 2026-10-14: colour-correct Maryland's leaf-off photos, "we could use this
later for different seasons depending on the time of year; it's currently fall and trees are changing colour").

Two steps on a rendered texture (imagery.Imagery.render; the block's photo and every far tile go through it, so
they agree):
  1. leaf_on: a leaf-off source (Maryland's six-inch photos, flown in early spring: dormant grass, bare branches)
     takes the colour of the summer reference (the USGS / NAIP mosaic, the rest of the chain) where that
     reference is vegetation: its chroma at the reference's ~8 m scale and, partly, its brightness (a leafy canopy
     is darker than bare twigs over leaf litter); the photo's own detail stays. Roofs, roads, dirt and water keep
     their colours (the reference is not green there).
  2. grade: from that summer look toward the season of the date at the place (season_amounts): fall turns the
     tree canopy yellow / orange / rust in patches (world-anchored noise, so tiles and anchors agree) and yellows
     everything green a little; the share of turned trees grows with the amount. Winter and spring: later.
Pure functions of their inputs (no state); numpy, and the grade in Rust (earthfast.season_img).
"""
import datetime

import numpy as np
from PIL import Image, ImageFilter

import fast

M_PER_DEG = 111320.0
# leaf_on: the reference is vegetation where its green chromaticity g / (r + g + b) is above this (summer USGS at
# Columbia: canopy and grass 0.35-0.38, pavement and roofs ~0.33)
VEG_CHROMA = (0.345, 0.37)
LEAF_ON_LUM = 0.8           # share of the reference's large-scale brightness taken in vegetation
LUM_GAIN = (0.5, 1.5)
# grade: canopy = vegetation that is darker and textured at the 2-3 m scale (lawns and verges are smooth and light)
VEG_HUE = (60.0, 75.0, 160.0, 175.0)        # deg: green between the ramps
CANOPY_V = (0.44, 0.52)                     # value above which it is not canopy
CANOPY_STD = (0.012, 0.028)                 # local std (value) below which it is not canopy
FALL_PALETTE = ((46.0, 1.12, 1.05), (30.0, 1.02, 1.10), (18.0, 0.92, 1.0))   # (hue deg, value x, sat x): yellow, orange, rust
FALL_YELLOW = 0.35          # every green pixel's hue moves this share toward yellow at full fall
PATCH_M = (18.0, 6.0, 25.0)  # noise scales: stands, single trees, colour regions
NOISE_GRID = 128            # the noise is computed on at most this many cells a side and interpolated
CANOPY_TEXTURE_MAX_M = 2.5  # coarser texels: the canopy by brightness only
FALL_PEAK_DOY_40N = 300     # ~Oct 27 at 40 degrees; 2.5 days earlier per degree further from the equator
FALL_HALF_DAYS = 28.0
WINTER_BARE_DESAT = 0.7     # saturation lost by bare canopy at full winter


def _smooth(x, a, b):
    t = np.clip((x - a) / (b - a), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


WINTER_DOY_40N = (330, 365 + 60)   # bare from ~Nov 26 to ~Mar 1 at 40 N (ramps of WINTER_RAMP_DAYS either side)
WINTER_RAMP_DAYS = 20.0
SPRING_PEAK_DOY_40N = 112           # ~Apr 22: the fresh flush
SPRING_HALF_DAYS = 22.0


def season_amounts(date, lat):
    """{"fall", "winter", "spring": 0..1} at a date (datetime.date) and latitude: how far the leaves have turned,
    how bare the deciduous canopy is, how fresh the spring flush is (0 in the tropics; the southern hemisphere half a
    year later; 2.5 days earlier per degree north for fall, later for spring). Pure."""
    if abs(lat) < 23.0:
        return {"fall": 0.0, "winter": 0.0, "spring": 0.0}
    doy = date.timetuple().tm_yday
    if lat < 0:
        doy = (doy + 182) % 365
    shift = (abs(lat) - 40.0) * 2.5
    fall = max(0.0, 1.0 - abs(doy - (FALL_PEAK_DOY_40N - shift)) / FALL_HALF_DAYS)
    w0, w1 = WINTER_DOY_40N[0] - shift, WINTER_DOY_40N[1] + shift
    d = doy if doy >= w0 - WINTER_RAMP_DAYS else doy + 365          # Jan-Mar count from the previous year
    winter = min(max(0.0, (d - (w0 - WINTER_RAMP_DAYS)) / WINTER_RAMP_DAYS),
                 max(0.0, (w1 + WINTER_RAMP_DAYS - d) / WINTER_RAMP_DAYS), 1.0)
    spring = max(0.0, 1.0 - abs(doy - (SPRING_PEAK_DOY_40N + shift)) / SPRING_HALF_DAYS)
    return {"fall": float(fall), "winter": float(winter), "spring": float(spring)}


def amounts_for(mode, lat, today=None):
    """season_amounts for --season: "auto" (today's date), "off" (none), "fall" / "winter" / "spring" (full)."""
    if mode in ("fall", "winter", "spring"):
        return {"fall": 0.0, "winter": 0.0, "spring": 0.0, mode: 1.0}
    if mode == "auto":
        return season_amounts(today or datetime.date.today(), lat)
    return {"fall": 0.0, "winter": 0.0, "spring": 0.0}


def leaf_on(rgb, ref):
    """rgb (uint8 [h, w, 3], a leaf-off photo already matched to ref at the large scale) with the summer reference
    ref's (coarser uint8) colour in its vegetation: chroma, and LEAF_ON_LUM of the brightness, at ref's scale."""
    h, w = rgb.shape[:2]
    rim = Image.fromarray(ref, "RGB").filter(ImageFilter.GaussianBlur(1.0))
    c = np.asarray(rim, np.float32)
    veg = _smooth(c[..., 1] / (c.sum(-1) + 1.0), *VEG_CHROMA)
    if not (veg > 0.01).any():
        return rgb
    vw = np.asarray(Image.fromarray(veg.astype(np.float32), "F").resize((w, h), Image.BILINEAR))
    ycc = np.asarray(Image.fromarray(rgb, "RGB").convert("YCbCr"), np.float32).copy()
    rc = np.asarray(rim.resize((w, h), Image.BILINEAR).convert("YCbCr"), np.float32)
    ycc[..., 1:] += vw[..., None] * (rc[..., 1:] - ycc[..., 1:])
    # brightness: ratio of the reference's to ours, both at ref's texels
    small = (ref.shape[1], ref.shape[0])
    ys = np.asarray(Image.fromarray(rgb, "RGB").resize(small, Image.BOX).convert("L")
                    .filter(ImageFilter.GaussianBlur(1.0)), np.float32)
    yr = np.asarray(rim.convert("L"), np.float32)
    g = np.clip((yr + 4.0) / (ys + 4.0), *LUM_GAIN)
    g = np.asarray(Image.fromarray(g.astype(np.float32), "F").resize((w, h), Image.BILINEAR))
    ycc[..., 0] *= 1.0 + vw * LEAF_ON_LUM * (g - 1.0)
    out = Image.fromarray(np.clip(np.rint(ycc), 0, 255).astype(np.uint8), "YCbCr").convert("RGB")
    return np.asarray(out)


GRADE_HALF_PX = 512         # textures this big are graded at half size and applied as a colour ratio
_PAL32 = np.array(FALL_PALETTE, np.float32)


def grade(rgb, xm, ym, amounts):
    """rgb (uint8 (h, w, 3), a summer look) toward the season: amounts {"fall": 0..1, "winter", "spring"}; xm, ym (float
    (h, w)) the texels' world position in metres (east, north: noise anchored to the world). Unchanged when every
    amount is 0. Only the vegetation's texels are converted; the canopy is the darker texels textured at the 2-3 m
    scale (by brightness alone for texels coarser than CANOPY_TEXTURE_MAX_M), the noise computed on a grid of ~2 m
    and interpolated; at GRADE_HALF_PX or more, computed on a half-size copy and applied as a per-channel ratio (the
    colour change is a few metres coarse at most, the detail stays the texture's own). xm, ym may instead be a 2-D
    grid over the texture (corner to corner), upscaled to its texels by Pillow's BILINEAR. The Rust kernel
    earthfast.season_img (Pillow's resampling where this module used it: the same numbers)."""
    fall, winter, spring = (float(amounts.get(k, 0.0)) for k in ("fall", "winter", "spring"))
    if fall <= 0.0 and winter <= 0.0 and spring <= 0.0:
        return rgb
    fast.require(108, "season grade")

    def f32(a):     # the texels' own (anything broadcasting to them), or a 2-D grid over the texture (upscaled there)
        a = np.asarray(a, np.float32)
        if a.ndim != 2 or a.shape == rgb.shape[:2]:
            a = np.broadcast_to(a, rgb.shape[:2])
        return np.ascontiguousarray(a)
    out = fast._ef.season_img(np.ascontiguousarray(np.asarray(rgb, np.uint8)[..., :3]), f32(xm), f32(ym), _PAL32,
                              (fall, winter, spring, FALL_YELLOW, WINTER_BARE_DESAT, tuple(CANOPY_V), tuple(CANOPY_STD),
                               tuple(PATCH_M), NOISE_GRID, CANOPY_TEXTURE_MAX_M, GRADE_HALF_PX))
    return rgb if out is None else out


def world_metres(lat, lon):
    """(east, north) metres of geodetic arrays, on a fixed world grid (noise anchors). Pure."""
    lat = np.asarray(lat, np.float64)
    return (np.asarray(lon, np.float64) * M_PER_DEG * np.cos(np.radians(lat))).astype(np.float32), \
        (lat * M_PER_DEG).astype(np.float32)
