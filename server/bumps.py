"""Road surface roughness: waves, dips, patches, a little cross-slope wobble and road condition that changes in
sections. Off unless BNE_BUMPS > 0.

The grading pass (grading.py) smooths every road's profile (SIGMA_M .. SIGMA_MAJOR_M) so the elevation data's noise
goes; real roads are not that smooth. apply() adds controlled roughness to each road Plan right after _plan
(grading.plan_roads), so the physics block, the level <= 0 mesh tiles and the DecalRoads all take the same heights.

The roughness is a pure function of the point's latitude / longitude (never the anchor or the planning region's
frame): a stretch of road has the same bumps on every visit, from every cell and region that plans it, with no step
at their seams. Two road parts meeting at a vertex get the same offset there.

Road condition (BNE_BUMPS_SECTIONS, on by default when BNE_BUMPS > 0): long sections (jittered Voronoi cells of
SECTION_M; along a road mostly 0.3-1.3 km, median ~600 m) are "decent", "average" or "poor" (major roads also "fresh
overlay": raised by its layer's thickness, a small step at either end), blended over STATE_BLEND_M; short "very bad"
stretches (BAD_M cells, ~200 m along a road) cut in now and then (~every 3.5 km on a tertiary road). Worse sections
carry more patches (flat pads 0.5-1.5 cm up or down).

Calibration: a quarter-car IRI (International Roughness Index, golden car at 80 km/h) of the graded profile. With
BNE_BUMPS=1 and sections off, tertiary ~2.5 m/km (average asphalt), primary ~1.9, unclassified ~3.2. With sections on,
a section's multiplier (STATES) is on that same scale: "average" 2.5 = 2.5 x that roughness.

Limits: the plan has a vertex every 4 m (grading.VERTEX_STEP) and the physics block a 2 m lattice, so the shortest
wavelength that reaches the tyres is ~5 m: no potholes or ruts. Bridges: the offset fades out over END_FADE_M before
an abutment (a deck is level). Railway and airport plans are not made by plan_roads and are left as they are.

Settings (environment, read at import; every BNE_* variable is in revisit.code_key, so a change rebuilds the cached
plans and tiles):
  BNE_BUMPS            overall strength; 0 (default) = off, roads exactly as before
  BNE_BUMPS_DIPS       dip depth multiplier (1.0)
  BNE_BUMPS_ROLL       cross-slope wobble multiplier (1.0)
  BNE_BUMPS_SEED       noise seed (1)
  BNE_BUMPS_SECTIONS   1 = road condition in sections (default), 0 = the same roughness everywhere
  BNE_BUMPS_SECTION_M  section cell size, m (1000)
  BNE_BUMPS_BAD        frequency multiplier of the very bad stretches (1.0; 0 = none)
  BNE_BUMPS_BAD_M      very bad stretch cell size, m (260)
  BNE_BUMPS_PATCHES    patch multiplier (1.0; 0 = none)
  BNE_BUMPS_OVERLAY    fresh overlay thickness multiplier (1.0)
"""
import logging
import os

import numpy as np
import dnp

log = logging.getLogger("earthd.bumps")
_said = []

SCALE = float(os.environ.get("BNE_BUMPS", "0"))
DIPS = float(os.environ.get("BNE_BUMPS_DIPS", "1.0"))
ROLL = float(os.environ.get("BNE_BUMPS_ROLL", "1.0"))
SEED = int(os.environ.get("BNE_BUMPS_SEED", "1"))
SECTIONS = os.environ.get("BNE_BUMPS_SECTIONS", "1") not in ("0", "off")
SECTION_M = float(os.environ.get("BNE_BUMPS_SECTION_M", "1000"))
BAD_M = float(os.environ.get("BNE_BUMPS_BAD_M", "260"))
BAD_SHARE = float(os.environ.get("BNE_BUMPS_BAD", "1.0"))
PATCHES = float(os.environ.get("BNE_BUMPS_PATCHES", "1.0"))
OVERLAY = float(os.environ.get("BNE_BUMPS_OVERLAY", "1.0"))

# (wavelength m, amplitude m) of the value-noise layers (see the module doc for the IRI they give)
WAVES = [
    (60.0, 0.060),      # long, gentle swell
    (25.0, 0.030),
    (11.0, 0.030),
    (5.0, 0.038),       # the shortest the 2 m lattice carries: most of the felt roughness
]
DIP_SCALE_M = 7.0       # size of a dip
DIP_DEPTH_M = 0.035     # depth of the deepest dip at multiplier 1
DIP_SHARE = 0.80        # noise threshold: higher = fewer dips
ROLL_WAVE_M = 40.0      # cross-slope wobble wavelength
ROLL_MAX = 0.004        # ... and its largest cross slope (m per m; 1.4 cm over a 3.5 m lane)
END_FADE_M = 20.0       # fade-out before a bridge abutment

# Road condition: (name, roughness and dip multiplier, patch share, raise m)
STATES = [
    ("fresh overlay", 0.60, 0.00, 0.030),
    ("decent", 1.80, 0.08, 0.0),
    ("average", 2.50, 0.25, 0.0),
    ("poor", 3.30, 0.40, 0.0),
]
BAD_STATE = ("very bad", 4.00, 0.35, 0.0)      # the short stretches (their own cell layer, BAD_M)
# (odds of STATES, share of very bad cells) by road group
STATE_ODDS = {
    "major": ((0.30, 0.50, 0.17, 0.03), 0.01),     # motorway, trunk, primary (damped by CLASS_GAIN as well)
    "mid": ((0.00, 0.40, 0.38, 0.22), 0.07),       # secondary, tertiary
    "minor": ((0.00, 0.25, 0.40, 0.35), 0.12),     # the rest, and unpaved roads
}
MAJOR = {"motorway", "trunk", "primary"}
MID = {"secondary", "tertiary"}
STATE_BLEND_M = 30.0    # sections blend into each other over about this distance
BAD_BLEND_M = 20.0      # ... and into a very bad stretch
OVERLAY_EDGE_M = 1.0    # an overlay's end step (in practice one plan segment, 4 m)
PATCH_M = 6.0           # patch size
PATCH_HEIGHT_M = (0.005, 0.015)     # a patch is this much up or down
PATCH_EDGE_M = 0.6      # its edge

# per class / kind (Overture class, Spec.kind): motorways smooth, minor roads rougher
CLASS_GAIN = {
    "motorway": 0.35, "trunk": 0.5, "primary": 0.7, "secondary": 0.85, "tertiary": 1.0,
    "unclassified": 1.25, "residential": 1.15, "living_street": 1.2, "service": 1.3, "track": 1.6,
}
KIND_GAIN = {"paved": 1.0, "cobble": 1.4, "gravel": 1.5, "dirt": 1.6}

_M_PER_DEG = 111320.0
_MASK = np.uint64(0xFFFFFFFF)


def _hash01(ix, iy, salt):
    """A deterministic number in [0, 1) per integer cell (ix, iy). Pure."""
    h = (ix.astype(np.int64).view(np.uint64) * np.uint64(0x9E3779B1)
         ^ iy.astype(np.int64).view(np.uint64) * np.uint64(0x85EBCA77)
         ^ np.uint64((SEED * 0xC2B2AE3D + salt * 0x27D4EB2F) & 0xFFFFFFFF))
    h &= _MASK
    h ^= h >> np.uint64(15)
    h = (h * np.uint64(0x2C1B3C6D)) & _MASK
    h ^= h >> np.uint64(12)
    h = (h * np.uint64(0x297A2D39)) & _MASK
    h ^= h >> np.uint64(15)
    return h.astype(np.float64) / 4294967296.0


def noise(x, y, salt=0):
    """Smooth 2D value noise in [-1, 1]; x, y in cells. Pure."""
    ix, iy = np.floor(x), np.floor(y)
    fx, fy = x - ix, y - iy
    ux, uy = fx * fx * (3 - 2 * fx), fy * fy * (3 - 2 * fy)
    ix, iy = ix.astype(np.int64), iy.astype(np.int64)
    a, b = _hash01(ix, iy, salt), _hash01(ix + 1, iy, salt)
    c, d = _hash01(ix, iy + 1, salt), _hash01(ix + 1, iy + 1, salt)
    v = a + (b - a) * ux + (c - a) * uy + (a - b - c + d) * ux * uy
    return v * 2.0 - 1.0


def cells(X, Y, size, salt):
    """Jittered Voronoi cells of mean size `size` m around the points: (distances m to the centres of the 5 x 5
    cells around, their numbers in [0, 1)), each (25, n). Pure."""
    x, y = X / size, Y / size
    ix, iy = np.floor(x).astype(np.int64), np.floor(y).astype(np.int64)
    dist, rnd = [], []
    for dx in (-2, -1, 0, 1, 2):
        for dy in (-2, -1, 0, 1, 2):
            cx, cy = ix + dx, iy + dy
            px = cx + _hash01(cx, cy, salt + 1)
            py = cy + _hash01(cx, cy, salt + 2)
            dist.append(dnp.hypot(x - px, y - py) * size)
            rnd.append(_hash01(cx, cy, salt + 3))
    return np.array(dist), np.array(rnd)


def soft(dist, values, width):
    """The cells' values blended: a cell's own inside it, the mean of the neighbours on a border, over about `width`
    m. Continuous everywhere, at corners of three cells too. Pure."""
    gap = dist - dist.min(axis=0)               # how much farther than the nearest centre (0 on a border)
    w = np.clip(1.0 - gap / (2.0 * width), 0.0, 1.0)
    w = w * w * (3 - 2 * w)
    return (w * values).sum(axis=0) / w.sum(axis=0)


def _metres(lat, lon):
    """Metric coordinates of no anchor or region (a continuous function of the place)."""
    lat = np.asarray(lat, np.float64)
    lon = np.asarray(lon, np.float64)
    return lon * _M_PER_DEG * dnp.cos(np.radians(lat)), lat * _M_PER_DEG


def _group(spec):
    cls = getattr(spec, "cls", None)
    if getattr(spec, "kind", "paved") not in ("paved", "cobble"):
        return "minor"
    return "major" if cls in MAJOR else "mid" if cls in MID else "minor"


def _state_params(r, group):
    """(multiplier, patch share, raise) of the STATES picked by the cells' numbers r."""
    odds = np.cumsum(STATE_ODDS[group][0])
    idx = np.minimum(np.searchsorted(odds, r * odds[-1], side="right"), len(STATES) - 1)
    table = np.array([st[1:] for st in STATES], np.float64)
    return np.moveaxis(table[idx], -1, 0)


def sections(X, Y, group):
    """Road condition at the points: (roughness multiplier, patch share, raise m), blended between sections, the
    very bad stretches cut in. Pure."""
    dist, rnd = cells(X, Y, SECTION_M, salt=200)
    gain, share, lift = _state_params(rnd, group)
    gain, share = soft(dist, gain, STATE_BLEND_M), soft(dist, share, STATE_BLEND_M)
    lift = soft(dist, lift, OVERLAY_EDGE_M) * OVERLAY
    bdist, brnd = cells(X, Y, BAD_M, salt=400)
    bad = soft(bdist, (brnd < STATE_ODDS[group][1] * BAD_SHARE).astype(np.float64), BAD_BLEND_M)
    gain = gain + bad * np.maximum(BAD_STATE[1] - gain, 0.0)
    share = share + bad * (BAD_STATE[2] - share)
    lift = lift * (1.0 - bad)
    return gain, share, lift


def patches(X, Y, share):
    """Patches: flat pads up or down, in a share `share` of the PATCH_M cells. Pure."""
    dist, rnd = cells(X, Y, PATCH_M, salt=300)
    sign = np.where((rnd * 1000.0) % 1.0 < 0.5, -1.0, 1.0)
    h = PATCH_HEIGHT_M[0] + (PATCH_HEIGHT_M[1] - PATCH_HEIGHT_M[0]) * ((rnd * 7919.0) % 1.0)
    on = np.clip((share[None, :] - rnd) / 0.05, 0.0, 1.0)      # (gradual where the share changes along a road)
    return soft(dist, on * sign * h, PATCH_EDGE_M) * PATCHES


def offsets(lat, lon, gain=1.0, group="mid"):
    """(height offset m, cross slope m/m) at the points (lat, lon). Pure."""
    X, Y = _metres(lat, lon)
    if SECTIONS:
        sgain, sshare, lift = sections(X, Y, group)
        sdips = sgain
    else:
        sgain, sdips, sshare, lift = 1.0, 1.0, 0.0, 0.0
    waves = np.zeros_like(X)
    for i, (wave, amp) in enumerate(WAVES):
        waves += noise(X / wave, Y / wave, salt=1 + i) * amp
    dz = waves * SCALE * gain * sgain
    if DIPS > 0:
        n = noise(X / DIP_SCALE_M, Y / DIP_SCALE_M, salt=50)
        hole = np.clip((n - DIP_SHARE) / (1.0 - DIP_SHARE), 0.0, 1.0)
        dz -= hole * hole * DIP_DEPTH_M * DIPS * SCALE * gain * sdips
    if SECTIONS and PATCHES > 0:
        dz += patches(X, Y, sshare) * min(SCALE, 2.0)
    dz += lift
    roll = noise(X / ROLL_WAVE_M, Y / ROLL_WAVE_M, salt=80) * ROLL_MAX * ROLL * SCALE * gain * sgain
    return dz, roll


def _gain(spec):
    g = KIND_GAIN.get(getattr(spec, "kind", "paved"), 1.0)
    g *= CLASS_GAIN.get(getattr(spec, "cls", None), 1.0)
    return g


def apply(frame, plan, spec, abut=(False, False)):
    """Plan with its roughness added (plan.z, plan.bank changed in place; returned). abut: (start, end) lead onto a
    bridge. Off (BNE_BUMPS 0), None or a failure: the plan as it was."""
    if plan is None or SCALE <= 0 or len(plan.xy) < 2:
        return plan
    if not _said:
        _said.append(1)
        log.info("road roughness on (BNE_BUMPS=%s, dips %s, roll %s, seed %s, sections %s at %s m, patches %s)",
                 SCALE, DIPS, ROLL, SEED, SECTIONS, SECTION_M, PATCHES)
    try:
        xy = np.asarray(plan.xy, np.float64)
        la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
        dz, roll = offsets(la, lo, _gain(spec), _group(spec))
        seg = dnp.hypot(np.diff(xy[:, 0]), np.diff(xy[:, 1]))
        s = np.r_[0.0, np.cumsum(seg)]
        w = np.ones_like(s)
        if abut[0]:
            w = np.minimum(w, np.clip(s / END_FADE_M, 0.0, 1.0))
        if abut[1]:
            w = np.minimum(w, np.clip((s[-1] - s) / END_FADE_M, 0.0, 1.0))
        w = w * w * (3 - 2 * w)
        plan.z = np.asarray(plan.z, np.float64) + dz * w
        if ROLL > 0:
            base = np.zeros(len(xy)) if plan.bank is None else np.asarray(plan.bank, np.float64)
            plan.bank = base + roll * w
    except Exception:       # never lose a road to its roughness
        log.debug("bumps: failed, the road keeps its smooth profile", exc_info=True)
    return plan
