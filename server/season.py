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
from PIL import Image

import fast

# grade: canopy = vegetation that is darker and textured at the 2-3 m scale (lawns and verges are smooth and light)
VEG_HUE = (60.0, 75.0, 160.0, 175.0)        # deg: green between the ramps
CANOPY_V = (0.44, 0.52)                     # value above which it is not canopy
CANOPY_STD = (0.012, 0.028)                 # local std (value) below which it is not canopy
FALL_PALETTE = ((46.0, 1.12, 1.05), (30.0, 1.02, 1.10), (18.0, 0.92, 1.0))   # (hue deg, value x, sat x): yellow, orange, rust
FALL_YELLOW = 0.35          # every green pixel's hue moves this share toward yellow at full fall
PATCH_M = (18.0, 6.0, 25.0)  # noise scales: stands, single trees, colour regions
NOISE_GRID = 128            # the noise is computed on at most this many cells a side and interpolated
CANOPY_TEXTURE_MAX_M = 2.5  # coarser texels: the canopy by brightness only
WINTER_BARE_DESAT = 0.7     # saturation lost by bare canopy at full winter


def season_amounts(date, lat):
    """{"fall", "winter", "spring": 0..1} at a date (datetime.date) and latitude: how far the leaves have turned,
    how bare the deciduous canopy is, how fresh the spring flush is (0 in the tropics; the southern hemisphere half a
    year later; 2.5 days earlier per degree north for fall, later for spring). Pure."""
    fast.require(563, "season_amounts")
    fall, winter, spring = fast._ef.season_amounts(date.timetuple().tm_yday, lat)
    return {"fall": fall, "winter": winter, "spring": spring}


# "auto" grades with the date of the week it is in (UTC, slots of SEASON_SLOT_DAYS from a fixed Monday), not today's:
# what a texture is graded with is then a function of its cache key (season_key), the same on every machine, and the
# far tiles and block photos made in October are not served as late November's (their keys held "auto" alone).
SEASON_SLOT_DAYS = 7
_SLOT_EPOCH = datetime.date(2026, 1, 5)     # a Monday
SEASON_KEY_LAT_PAD = 1.0    # deg: a key holds the slot where the season changes within this of its latitude


def utc_today():
    return datetime.datetime.now(datetime.timezone.utc).date()


def slot_date(today=None):
    """The first day of the SEASON_SLOT_DAYS slot that today (default: the UTC date) is in."""
    d = today or utc_today()
    return d - datetime.timedelta(days=(d - _SLOT_EPOCH).days % SEASON_SLOT_DAYS)


def amounts_for(mode, lat, today=None):
    """season_amounts for --season: "auto" (the date of this week's slot, slot_date), "off" (none), "fall" / "winter" /
    "spring" (full)."""
    if mode in ("fall", "winter", "spring"):
        return {"fall": 0.0, "winter": 0.0, "spring": 0.0, mode: 1.0}
    if mode == "auto":
        return season_amounts(slot_date(today), lat)
    return {"fall": 0.0, "winter": 0.0, "spring": 0.0}


def season_key(mode, lat, today=None):
    """The season's part of a texture's cache key at latitude lat: mode itself, or for "auto" the slot's date where
    any amount is above 0 within SEASON_KEY_LAT_PAD of lat (a far tile lies within that of its anchor), else "none"
    (the tropics and full summer: the textures stay put all those weeks)."""
    if mode != "auto":
        return mode
    d = slot_date(today)
    for la in (float(lat) - SEASON_KEY_LAT_PAD, float(lat), float(lat) + SEASON_KEY_LAT_PAD):
        if any(v > 0.0 for v in season_amounts(d, max(-90.0, min(90.0, la))).values()):
            return ("auto", d.isoformat())
    return ("auto", "none")


def leaf_on(rgb, ref):
    """rgb (uint8 [h, w, 3], a leaf-off photo already matched to ref at the large scale) with the summer reference
    ref's (coarser uint8) colour in its vegetation: chroma, and LEAF_ON_LUM of the brightness, at ref's scale."""
    fast.require(563, "leaf_on")
    h, w = rgb.shape[:2]
    rh, rw = ref.shape[:2]
    if (h == w and rh == rw) or (h, w) == (rh, rw):
        out = fast._ef.leaf_on(rgb, ref)
        return rgb if out is None else out

    # Pillow versions differ on the pass order for some rectangular resizes.
    small = np.asarray(Image.fromarray(rgb).resize((rw, rh), Image.Resampling.BOX))
    fields = fast._ef.leaf_fields(small, ref)
    if fields is None:
        return rgb
    return fast._ef.leaf_blend(rgb, *(np.asarray(Image.fromarray(a).resize((w, h), Image.Resampling.BILINEAR))
                                      for a in fields))


GRADE_HALF_PX = 512         # textures this big are graded at half size and applied as a colour ratio
_PAL32 = np.array(FALL_PALETTE, np.float32)


def grade_params(amounts):
    return (float(amounts.get("fall", 0.0)), float(amounts.get("winter", 0.0)), float(amounts.get("spring", 0.0)),
            FALL_YELLOW, WINTER_BARE_DESAT, tuple(CANOPY_V), tuple(CANOPY_STD), tuple(PATCH_M), NOISE_GRID,
            CANOPY_TEXTURE_MAX_M, GRADE_HALF_PX)


def grade(rgb, xm, ym, amounts):
    """rgb (uint8 (h, w, 3), a summer look) toward the season: amounts {"fall": 0..1, "winter", "spring"}; xm, ym (float
    (h, w)) the texels' world position in metres (east, north: noise anchored to the world). Unchanged when every
    amount is 0. Only the vegetation's texels are converted; the canopy is the darker texels textured at the 2-3 m
    scale (by brightness alone for texels coarser than CANOPY_TEXTURE_MAX_M), the noise computed on a grid of ~2 m
    and interpolated; at GRADE_HALF_PX or more, computed on a half-size copy and applied as a per-channel ratio (the
    colour change is a few metres coarse at most, the detail stays the texture's own). xm, ym may instead be a 2-D
    grid over the texture (corner to corner), upscaled to its texels by Pillow's BILINEAR. The Rust kernel
    earthfast.season_img (Pillow's resampling where this module used it: the same numbers)."""
    params = grade_params(amounts)
    if max(params[:3]) <= 0.0:
        return rgb
    fast.require(108, "season grade")

    def f32(a):     # the texels' own (anything broadcasting to them), or a 2-D grid over the texture (upscaled there)
        a = np.asarray(a, np.float32)
        if a.ndim != 2 or a.shape == rgb.shape[:2]:
            a = np.broadcast_to(a, rgb.shape[:2])
        return np.ascontiguousarray(a)
    out = fast._ef.season_img(np.ascontiguousarray(np.asarray(rgb, np.uint8)[..., :3]), f32(xm), f32(ym), _PAL32,
                              params)
    return rgb if out is None else out


def world_metres(lat, lon):
    """(east, north) metres of geodetic arrays, on a fixed world grid (noise anchors). Pure."""
    lat = np.asarray(lat, np.float64)
    fast.require(563, "season_metres")
    la, lo = np.broadcast_arrays(lat, np.asarray(lon, np.float64))
    east, north = fast._ef.season_metres(la, lo, lat)
    return (east if east.ndim else east[()], north if north.ndim else north[()])
