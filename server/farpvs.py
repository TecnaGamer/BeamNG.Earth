"""The far field's potentially visible set (the retro study's item C, Quake's PVS / Doom's REJECT for far textures):
far tiles hidden behind terrain from everywhere the player can be soon keep a small texture until they are not.

Far textures are the rate-limited, CPU-heavy part of a first visit (~900-1,600 aerial photo requests per first fill,
each texture's warp, season grade and BC1 encode). Much of the far field is behind terrain at driver eye height:
tools/farpvs_probe.py measured 2026-11-11 (the server's default selection, horizon on, the player standing; this
module's rules, eye class 8 m, the 1.5 km square): far tiles (L3+) hidden 66 % at Zermatt, 67 % Yosemite Valley, 59 %
Vail, 56 % Leadville, 40 % Wichita (the earth's bulge past a few km on a plain), 20-33 % Asheville / Pittsburgh / Genoa /
San Francisco / NYC, 28 % on the Stelvio pass; photo requests per first fill 25-50 % fewer in the mountain valleys, 5-20 %
elsewhere.

How (per connection, in its active anchor's plane):
  - Rings: coarse elevation rasters round the anchor's origin, each a min- and a max-pooled raster (RING_SPECS: 100 m
    cells to 32 km, 400 m to 120 km, 1.5 km to the far radius; DEM zooms the far tiles of those distances read anyway),
    built once per anchor (and again if the far radius outgrows them).
  - Viewpoints: a VP_STEP grid over the square round the player's look-ahead point (half HALF_MIN, growing with the
    speed: LEAD_S of travel, at most HALF_MAX), only those a car could reach (|ground - player's ground| within GRADE x
    their distance + SLACK_M: on a mountain road the slopes above it are not viewpoints), each eye an EYE_CLASSES
    height above the highest ground near it (the rings' max raster; the class the camera's height needs). A
    viewpoint's visibility (earthfast.far_pvs: rays to the radius, the running horizon) is kept per anchor and eye
    class, so moving on only adds the new row of them.
  - The set: the OR of its viewpoints' visibility (blocks of GRAN x GRAN cells); a tile is hidden when no block over
    its footprint (grown by one block) is visible, it lies wholly in the rings, it is level >= MIN_LEVEL and away from
    the region (NEAR_M).
  - Valid while the player stays within the square less MARGIN_M and the camera within its eye class of the ground's
    highest there (else the next eye class, past the last none: everything visible, as without it). earthd re-renders
    a hidden tile's texture at its full size (far_retex) as soon as a new set no longer hides it: the square reaches
    HALF_MIN (LEAD_S of travel at speed) round the player and is made again every VP_STEP, so that happens about a
    kilometre before the tile can be seen.

Conservative where it can be (farpvs.rs): occluders from the min raster with the earth's own drop, targets from the max
raster with the refracted drop, each raster eroded / dilated by a cell. Not exact between viewpoints 500 m apart, hence
the margins, the eye height and the grown footprints. BNGE_FAR_PVS=0 turns it off.
"""
import math
import os
import threading
from collections import OrderedDict

import numpy as np

import dem as demmod
import fast

NEED = 218                  # earthfast farpvs.rs (far_pvs)
ENABLED = os.environ.get("BNGE_FAR_PVS", "1") != "0"
TEX_PX = int(os.environ.get("BNGE_FAR_PVS_PX", "128"))     # a hidden tile's texture (the coarse cover's FIRST_RAMP_TEX_PX)
MIN_LEVEL = 1               # finer levels always full (the ground round the player)
# (half-size m, None: the radius + RING_MARGIN; cell m; m added to the max raster: a coarse DEM's summits are lower than
# the ground's, the far targets' angles must not be)
RING_SPECS = ((32000.0, 100.0, 10.0), (120000.0, 400.0, 30.0), (None, 1500.0, 80.0))
RING_MARGIN = 25000.0       # the last ring past the far radius (the player's offset from the anchor, ANCHOR_CENTRE_MAX)
GRAN = 4                    # cells per visibility block
AZIMUTHS = 1024            # (rays ~180 m apart at ring 0's edge: its maxima spread a cell each way, 300 m)
D0 = 100.0                  # m: rays start here (the viewpoint's own cell)
VP_STEP = 500.0
HALF_MIN = 1500.0           # m: the viewpoint square's half-size ...
HALF_MAX = 5000.0
LEAD_S = 20.0               # ... at least this many seconds of travel, centred this far ahead / 2
MARGIN_M = 500.0            # the set holds while the player is this far inside its square
NEAR_M = 250.0              # tiles within this of the square: always full
GRADE = 0.12                # a viewpoint the car can reach: its ground within GRADE x distance + SLACK_M of the player's
SLACK_M = 75.0
EYE_CLASSES = (8.0, 30.0, 120.0, 280.0)    # m above the highest ground near a viewpoint; past the last: no PVS
# (past 280 m: earthd sizes far textures by the screen for a flying camera, FLY_TEX_EYE_M, as for the 3D map)
R_OCC = 6371000.0           # the occluders' drop: the earth's own radius (farfield.R_EARTH; lower: conservative)
R_TGT = 6371000.0 / (1.0 - 0.13)        # the targets': refracted (farfield.R_REFRACTED; higher: conservative)
VP_CACHE = 1500             # viewpoint visibilities kept per anchor (packed: ~10 KB each)
EYE_MARGIN_M = 2.0          # the camera this far under its class (the update_eye quantum is 10 m, read on the exact one)
GROUND_STEP_M = 50.0        # a set is made again when the player's ground moved this much (its reachable viewpoints)
RADIUS_ROOM = 1.15          # the rings reach this much past the far radius they were made for
# Textures by the screen's need (the research round 2, Q3: on flat or gently sloping ground the far textures were far
# finer than anything the GPU samples from a driver's eye): a visible far tile's texture is the smallest power of two
# whose texels are finer than OVERSAMPLE x less than what the screen resolves of the tile at its nearest from the square
# (d / ppr across the view, d / (ppr sin a) along it, a: the steepest the view can meet the ground there, the eye's
# depression to the tile's lowest ground plus its steepest slope; ANISO: the GPU's anisotropic filtering takes
# that many texels along the view per pixel, 16x the most demanding setting), never under SLOPE_PX_MIN, never above
# the level's own. Levels under SLOPE_MIN_LEVEL and tiles within SLOPE_NEAR_M of the square keep their own.
# Measured 2026-11-11 (tools/farpvs_probe.py, eye class 8 m): with these conservative defaults (the steepest cell under
# the tile, x SLOPE_SAFETY, + SLOPE_FLOOR, OVERSAMPLE) hardly a tile is reduced even on plains (Wichita 0, Garden City
# 1, a Dutch polder 4 of ~300): every far tile has a 4-10 % slope somewhere at the rings' 100 m cells, and above ~3.6
# deg (1 / ANISO) the anisotropic gain is gone (the levels' textures already match d / ppr across the view). Looser
# (BNGE_FAR_SLOPE_Q=0.9, _SAFETY=1, _FLOOR=0.01, _OVERSAMPLE=1, _RELIEF=0): photo requests 25 / 28 / 38 % fewer than
# without (the PVS included) at Wichita / Garden City / Flevoland, against 20 / 15 / 15 % with the PVS alone; the
# median slope and no margins: 48 / 48 / 76 %. How loose still looks right is an in-game A/B.
SLOPE_SIZING = os.environ.get("BNGE_FAR_SLOPE_PX", "1") != "0"
ANISO = 16.0
OVERSAMPLE = float(os.environ.get("BNGE_FAR_SLOPE_OVERSAMPLE", "1.5"))
SLOPE_SAFETY = float(os.environ.get("BNGE_FAR_SLOPE_SAFETY", "1.25"))   # x the rings' slope (their coarse samples)
SLOPE_RELIEF = os.environ.get("BNGE_FAR_SLOPE_RELIEF", "1") != "0"    # ... at least their 3 x 3 relief over its width
SLOPE_FLOOR = float(os.environ.get("BNGE_FAR_SLOPE_FLOOR", "0.02"))  # tan added to every slope (facets, a field's relief)
SLOPE_CAP = 2.0
SLOPE_QUANTILE = float(os.environ.get("BNGE_FAR_SLOPE_Q", "1.0"))   # of the cells under a tile (1: the steepest)
SLOPE_PX_MIN = 64
# Pixel-aware far textures (the user, 2026-11-11, at Ouray: "The texture on the mountains look low ... can we use a
# higher res image further out ... Make it pixel aware too"): a tile in sight whose level's texture is coarser than the
# screen resolves of it from where the player can be (_screen_px's need: a mountain face turned to the road needs
# d / ppr, its level's 512 px over 2-4 km were 4-8 m texels for ~2 m screen pixels at 3 km) gets a larger one, up to
# UP_PX_MAX and never finer than the sharpest photos there (the caller's `up`: earthd.Server.up_px); the photo zoom
# follows the texel. The hidden tiles' saving (TEX_PX) pays for it.
UP_ON = os.environ.get("BNGE_FAR_PX_UP", "1") != "0"
UP_PX_MAX = int(os.environ.get("BNGE_FAR_PX_MAX", "2048"))
SLOPE_MIN_LEVEL = 2
SLOPE_NEAR_M = 1000.0
PPR_MIN = 1080 / (2 * math.tan(math.radians(30.0)))     # pixels per radian at least (1080 lines, 60 deg fov)


def _spread(a, f):
    """a with each cell the f (np.maximum / np.minimum) of its 3 x 3 neighbourhood (edges: their own), separably."""
    b = a.copy()
    f(b[1:], a[:-1], out=b[1:])
    f(b[:-1], a[1:], out=b[:-1])
    c = b.copy()
    f(c[:, 1:], b[:, :-1], out=c[:, 1:])
    f(c[:, :-1], b[:, 1:], out=c[:, :-1])
    return c


class Rings:
    """The elevation rings of one anchor (heights above sea level): [(lo, hi, x0, y0, cell)], nested, square, centred
    on the anchor's origin; reach: the last one's half-size."""

    def __init__(self, frame, dem, radius, specs=RING_SPECS):
        self.rings = []
        self.slopes = []                    # per ring: the steepest ground near each cell (tan)
        self.reach = 0.0
        lat0 = float(frame.lat0)
        for half, cell, pad in specs:
            half = radius + RING_MARGIN if half is None else half
            n = int(math.ceil(2 * half / cell))
            half = n * cell / 2
            k = (np.arange(n) + 0.5) * cell - half
            E, N = np.meshgrid(k, k)                      # [j north, i east], row 0 south
            lat, lon, _ = frame.enu_to_geodetic(E, N, 0.0)
            z = demmod.zoom_for(lat0, cell, 14)    # (pixels at most a cell: z10 / z8 / z6 at 46 N, the far field's own)
            a = np.asarray(dem.sample(lat, lon, z), np.float32).reshape(n, n)
            # (each cell's centre sampled, then its 3 x 3 neighbourhood's least / most: the ground between samples
            # within a cell of one; the pad for summits finer than the samples)
            lo = _spread(a, np.minimum)
            hi = _spread(a, np.maximum)
            hi += np.float32(pad)
            self.rings.append((np.ascontiguousarray(lo), np.ascontiguousarray(hi), -half, -half, float(cell)))
            # the steepest ground near each cell (tan): the central differences' gradient and the 3 x 3 relief over
            # its width, the larger, its neighbourhood's most, times SLOPE_SAFETY (a coarse sample smooths a slope)
            g = np.zeros_like(a)
            gx = np.abs(a[:, 2:] - a[:, :-2]) / (2 * cell)
            gy = np.abs(a[2:, :] - a[:-2, :]) / (2 * cell)
            g[:, 1:-1] = gx
            g[1:-1, :] = np.hypot(g[1:-1, :], gy)
            if SLOPE_RELIEF:
                g = np.maximum(g, (hi - lo - np.float32(pad)) / np.float32(3 * cell))
            sl = np.minimum(_spread(g, np.maximum) * np.float32(SLOPE_SAFETY) + np.float32(SLOPE_FLOOR),
                            np.float32(SLOPE_CAP))
            self.slopes.append(np.ascontiguousarray(sl))
            self.reach = half
        self.radius = float(radius)

    def tile_ground(self, bbox):
        """(lowest ground, steepest slope (tan)) under a footprint (anchor plane bbox, a cell round it) from the finest
        ring holding it whole, or None when none does."""
        x0, y0, x1, y1 = bbox
        for (lo, _, rx0, ry0, cell), sl in zip(self.rings, self.slopes):
            n = lo.shape[0]
            i0, j0 = int(math.floor((x0 - rx0) / cell)) - 1, int(math.floor((y0 - ry0) / cell)) - 1
            i1, j1 = int(math.floor((x1 - rx0) / cell)) + 1, int(math.floor((y1 - ry0) / cell)) + 1
            if i0 < 0 or j0 < 0 or i1 >= n or j1 >= n:
                continue
            w = sl[j0:j1 + 1, i0:i1 + 1]
            s = float(w.max()) if SLOPE_QUANTILE >= 1.0 else float(np.quantile(w, SLOPE_QUANTILE))
            return float(lo[j0:j1 + 1, i0:i1 + 1].min()), s
        return None

    def region_top(self, cx, cy, half):
        """The highest ground of a square (the finest ring holding it)."""
        for lo, hi, x0, y0, cell in self.rings:
            n = hi.shape[0]
            i0, j0 = int(math.floor((cx - half - x0) / cell)), int(math.floor((cy - half - y0) / cell))
            i1, j1 = int(math.floor((cx + half - x0) / cell)), int(math.floor((cy + half - y0) / cell))
            if i0 >= 0 and j0 >= 0 and i1 < n and j1 < n:
                return float(hi[j0:j1 + 1, i0:i1 + 1].max())
        return None

    def ground_hi(self, x, y):
        """The highest ground near (x, y) (the finest ring holding it), or None outside them all."""
        for lo, hi, x0, y0, cell in self.rings:
            i, j = int(math.floor((x - x0) / cell)), int(math.floor((y - y0) / cell))
            if 0 <= i < hi.shape[1] and 0 <= j < hi.shape[0]:
                return float(hi[j, i])
        return None

    def ground_hi_many(self, xs, ys):
        out = np.full(len(xs), np.nan)
        for lo, hi, x0, y0, cell in reversed(self.rings):       # (finest last: it wins)
            i = np.floor((np.asarray(xs) - x0) / cell).astype(np.int64)
            j = np.floor((np.asarray(ys) - y0) / cell).astype(np.int64)
            ok = (i >= 0) & (j >= 0) & (i < hi.shape[1]) & (j < hi.shape[0])
            out[ok] = hi[j[ok], i[ok]]
        return out

    def ground_lo_many(self, xs, ys):
        out = np.full(len(xs), np.nan)
        for lo, hi, x0, y0, cell in reversed(self.rings):
            i = np.floor((np.asarray(xs) - x0) / cell).astype(np.int64)
            j = np.floor((np.asarray(ys) - y0) / cell).astype(np.int64)
            ok = (i >= 0) & (j >= 0) & (i < lo.shape[1]) & (j < lo.shape[0])
            out[ok] = lo[j[ok], i[ok]]
        return out


class Field:
    """One anchor's rings and the visibility of its viewpoints (per eye class), computed as needed."""

    def __init__(self, aid, frame, rings):
        self.aid, self.frame, self.rings = aid, frame, rings
        self.cache = OrderedDict()          # (ix, iy, eye class) -> [packed block bitmaps per ring]
        self.lock = threading.Lock()
        self.computed = 0
        self.shapes = [((lo.shape[0] + GRAN - 1) // GRAN,) * 2 for lo, _, _, _, _ in rings.rings]

    def viewpoints(self, keys, eye_class):
        """Packed visibility bitmaps (a list per ring) of grid viewpoints keys [(ix, iy)] (VP_STEP apart) at an eye
        class, in order (None: outside the rings); the ones not kept yet made in one far_pvs call."""
        out = [None] * len(keys)
        todo = []
        with self.lock:
            for q, (ix, iy) in enumerate(keys):
                got = self.cache.get((ix, iy, eye_class))
                if got is not None:
                    self.cache.move_to_end((ix, iy, eye_class))
                    out[q] = got
                else:
                    todo.append(q)
        views, at = [], []
        for q in todo:
            x, y = keys[q][0] * VP_STEP, keys[q][1] * VP_STEP
            g = self.rings.ground_hi(x, y)
            if g is not None:
                views.append((x, y, g + EYE_CLASSES[eye_class]))
                at.append(q)
        if views:
            fast.require(NEED, "far PVS")
            vis = fast._ef.far_pvs(self.rings.rings, np.array(views, np.float64), AZIMUTHS, GRAN, D0,
                                   self.rings.reach * 3.0, R_OCC, R_TGT)
            with self.lock:
                for k, q in enumerate(at):
                    got = [np.packbits(np.asarray(v[k], np.uint8).ravel()) for v in vis]
                    out[q] = got
                    self.cache[(keys[q][0], keys[q][1], eye_class)] = got
                    self.computed += 1
                while len(self.cache) > VP_CACHE:
                    self.cache.popitem(last=False)
        return out


def region(px, py, vx, vy):
    """(centre x, y on the viewpoint grid, half-size) of the square of viewpoints for a player at (px, py) moving at
    (vx, vy) m/s."""
    v = math.hypot(vx, vy)
    half = min(HALF_MAX, max(HALF_MIN, v * LEAD_S))
    # (centred ahead, at most halfway to where the set stops holding: re-centred on the 500 m grid as the player goes)
    lead = min((half - MARGIN_M) / 2, v * LEAD_S / 2) / v if v > 0.5 else 0.0
    cx, cy = px + vx * lead, py + vy * lead
    return round(cx / VP_STEP) * VP_STEP, round(cy / VP_STEP) * VP_STEP, math.ceil(half / VP_STEP) * VP_STEP


def available():
    """The far PVS is on (BNGE_FAR_PVS) and the native module has its kernel."""
    return ENABLED and getattr(fast._ef, "VERSION", 0) >= NEED


class Set:
    """A computed PVS: the region (square centre, half-size), its eye class, the player's place and ground it was made
    for, and the OR of its viewpoints' visibility as per-ring summed-area tables. hidden(key, bbox, level) per tile."""

    def __init__(self, field, cx, cy, half, eye_class, px, py, ground, grade=None, slack=None, ppr=None):
        self.field, self.aid = field, field.aid
        self.cx, self.cy, self.half, self.eye_class, self.ground = cx, cy, half, eye_class, ground
        self.eye_m = EYE_CLASSES[eye_class]
        rings = field.rings
        n = int(round(half / VP_STEP))
        ii, jj = np.meshgrid(np.arange(-n, n + 1), np.arange(-n, n + 1))
        ix = (ii.ravel() + int(round(cx / VP_STEP)))
        iy = (jj.ravel() + int(round(cy / VP_STEP)))
        xs, ys = ix * VP_STEP, iy * VP_STEP
        # reachable on the ground: within GRADE x distance from the player (+ SLACK_M) of the player's ground, judged on
        # the lowest and the highest ground near each viewpoint (any part of its cell in reach counts); the viewpoint
        # nearest the player always
        lo = rings.ground_lo_many(xs, ys)
        hi = rings.ground_hi_many(xs, ys)
        d = np.hypot(xs - px, ys - py)
        reach = (GRADE if grade is None else grade) * d + (SLACK_M if slack is None else slack)
        ok = np.isfinite(lo) & (lo - reach <= ground) & (hi + reach >= ground)
        ok[int(np.argmin(d))] = True
        acc, used = None, 0
        for got in field.viewpoints([(int(a), int(b)) for a, b in zip(ix[ok], iy[ok])], eye_class):
            if got is None:
                continue
            used += 1
            acc = list(got) if acc is None else [np.bitwise_or(p, q) for p, q in zip(acc, got)]
        self.viewpoints = used
        self.sats = []
        for r, (shape, (lo_r, _, x0, y0, cell)) in enumerate(zip(field.shapes, rings.rings)):
            if acc is None:
                break
            v = np.unpackbits(acc[r])[:shape[0] * shape[1]].reshape(shape).astype(np.int32)
            sat = np.zeros((shape[0] + 1, shape[1] + 1), np.int32)
            sat[1:, 1:] = v.cumsum(0).cumsum(1)
            self.sats.append((sat, x0, y0, cell * GRAN, shape[0]))
        self.empty = acc is None            # (no viewpoint in the rings: nothing is hidden)
        self.reach = rings.reach
        self.cache = {}
        top = rings.region_top(cx, cy, half)
        self.eye_top = None if top is None else top + self.eye_m     # (the highest the eye can be in the square)
        self.ppr = max(PPR_MIN, ppr or 0.0)
        self.px_cache = {}

    def tex_px(self, key, bbox, level, size, full, up=None):
        """The texture px a tile needs for this set's eyes: TEX_PX hidden (or less where the screen needs less), the
        screen's need when visible (SLOPE_SIZING, see the module's constants): `full` (the level's own) at most, or up to
        `up` (UP_ON: the most the photos there are worth) where the screen resolves more of it than `full` gives."""
        top = max(full, up) if (UP_ON and up) else full
        got = self.px_cache.get(key)
        if got is not None and got[0] == (bbox, full, top):
            return got[1]
        px = full
        hidden = self.hidden(key, bbox, level)
        if SLOPE_SIZING:
            px = self._screen_px(bbox, level, size, full, top)
        if hidden:
            px = min(px, TEX_PX)
        self.px_cache[key] = ((bbox, full, top), px)
        return px

    def _screen_px(self, bbox, level, size, full, top=None):
        top = top or full
        if self.eye_top is None:
            return full
        x0, y0, x1, y1 = bbox
        dx = max(x0 - (self.cx + self.half), (self.cx - self.half) - x1, 0.0)
        dy = max(y0 - (self.cy + self.half), (self.cy - self.half) - y1, 0.0)
        d = math.hypot(dx, dy)
        # (the near rings, under SLOPE_MIN_LEVEL or within SLOPE_NEAR_M: never smaller, larger where the screen resolves
        # more of them: the slopes 1-2 km round a valley road were 2 m texels, Ouray 2026-11-11)
        near = level < SLOPE_MIN_LEVEL or d < SLOPE_NEAR_M
        if near and top <= full:
            return full
        if near:
            need = d / (self.ppr * OVERSAMPLE)
        else:
            g = self.field.rings.tile_ground(bbox)
            if g is None:
                return full
            zmin, slope = g
            sin_a = min(1.0, max(0.0, (self.eye_top - zmin) / d) + d / (2 * R_OCC) + slope)
            need = d / (self.ppr * OVERSAMPLE) * max(1.0, 1.0 / (ANISO * max(sin_a, 1e-6)))
        px = full
        while px * 2 <= top and size / px > need:       # (coarser than the screen resolves: larger, up to top)
            px *= 2
        while not near and px > SLOPE_PX_MIN and size / (px // 2) <= need:
            px //= 2
        return px

    def valid_at(self, aid, x, y, eye):
        """The set still holds for a player at (x, y) of anchor aid with the camera eye m above sea level."""
        if aid != self.aid:
            return False
        if max(abs(x - self.cx), abs(y - self.cy)) > self.half - MARGIN_M:
            return False
        g = self.field.rings.ground_hi(x, y)
        return g is not None and (eye is None or eye <= g + self.eye_m)

    def hidden(self, key, bbox, level):
        """Is a tile (its footprint bbox in the anchor's plane, its level) hidden from every viewpoint?"""
        got = self.cache.get(key)
        if got is not None and got[0] == bbox:
            return got[1]
        h = self._hidden(bbox, level)
        self.cache[key] = (bbox, h)
        return h

    def _hidden(self, bbox, level):
        if level < MIN_LEVEL or self.empty:
            return False
        x0, y0, x1, y1 = bbox
        r = self.half + NEAR_M
        if x0 < self.cx + r and x1 > self.cx - r and y0 < self.cy + r and y1 > self.cy - r:
            return False
        if x0 < -self.reach or y0 < -self.reach or x1 > self.reach or y1 > self.reach:
            return False                    # (not wholly in the rings)
        for sat, rx0, ry0, b, n in self.sats:
            i0 = max(0, int(math.floor((x0 - rx0) / b)) - 1)
            j0 = max(0, int(math.floor((y0 - ry0) / b)) - 1)
            i1 = min(n, int(math.floor((x1 - rx0) / b)) + 2)
            j1 = min(n, int(math.floor((y1 - ry0) / b)) + 2)
            if i0 >= i1 or j0 >= j1:
                continue
            if sat[j1, i1] - sat[j0, i1] - sat[j1, i0] + sat[j0, i0] > 0:
                return False
        return True


def eye_class_for(clear):
    """The smallest eye class holding a camera clear m above the ground's highest near it, or None (higher)."""
    for k, e in enumerate(EYE_CLASSES):
        if clear <= e - EYE_MARGIN_M:
            return k
    return None
