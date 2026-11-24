"""Snow on the ground photos (winter plan W2, ~/bnge-tests/winterplan/PLAN.md 3.1 / 3.2; the user, 2026-11-24: a "white
Christmas" build first). BNE_WINTER=on (the settings page's Experimental "Winter") turns it on; off, nothing here is
read, keyed or applied and every photo and key is today's.

The block's base photo and every far tile take the same snow term (earthfast.SnowLook, snowlook.rs), after their roads
and rock, so the two agree at their seam: white where the snow state says so at the texel's own height (our DEM, the
texture's height grid), lower on slopes facing the pole, none on steep rock, the forest's dark crowns mostly kept,
already-white texels left alone, permanent snow above the climatic snowline in polar / alpine climates.

The snow state: a FIELD of band tables per forcing cell (the interface with W1's kernel and W5's pack). A Table is up to
8 rows (band elevation m, cover 0..8, depth bin 0..6, age 0..3), lowest first, linear in elevation between rows and
held past the ends, and the road shares P0..P4 (eighths of the ground's cover a road of that plow class keeps: W3's
hook; the photos get no road mask yet, so the roads take the ground's cover). A Field is a resolution (deg) and the
tables by cell (ci, cj) = (floor((lon + 180) / res), floor((90 - lat) / res)), or one table everywhere. Where it comes
from (BNE_SNOW):
  none                  nothing (the keys are today's exactly);
  dusting light heavy deep alpine spring: a fixed debug preset (PRESETS), the same everywhere and on every machine;
  real (the default with BNE_WINTER on): the provider W1 registers (set_provider); none until it does.

Keys: a texture's snow is a pure function of its SPEC (spec_for_box: the tables of the cells round its square, a hash
of them, and whether permanent snow can occur there); the spec's key part (key_parts) joins the season's in the far
tiles' and base photos' revisit keys. No snow on a square (no table with cover there, no polar / alpine climate near
it) is no key part at all: those keys stay what they were. A builder never asks "the current state" twice: the server
makes the spec once and hands the same one to the key and to the render (tex["snow"], earthd's calls).

Never in plans, cell facts or the decision archives (revisit.PLAN_NEUTRAL; nothing the planning reaches imports this).
"""
import hashlib
import os
from collections import namedtuple

import numpy as np

import fast

ON = os.environ.get("BNE_WINTER", "off").strip().lower() in ("on", "1", "yes", "true")
MODE = (os.environ.get("BNE_SNOW", "") or ("real" if ON else "none")).strip().lower()
# The look's own version: in every spec's key part (bump it with any change of PARAMS, the kernel's snow term or how a
# spec is cut from a field)
LOOK_VERSION = 1
GEO_N = 9               # nodes a side of the geodetic grid over a texture (lat, lon, curvature drop, snowline)
FIELD_PAD_CELLS = 1     # cells past the square a spec holds (the texels near its edge blend with them)

Table = namedtuple("Table", "rows road")        # rows: ((z m, cover 0..8, depth 0..6, age 0..3), ...); road: 5 x 0..8
NO_ROAD = (8, 8, 8, 8, 8)

# Debug presets (the plan's 2.11: "a preset is a fixed synthetic forcing record"; here the band table it leads to)
PRESETS = {
    "dusting": Table(((0, 4, 1, 0),), NO_ROAD),
    "light": Table(((0, 7, 2, 0),), NO_ROAD),
    "heavy": Table(((0, 8, 4, 0),), NO_ROAD),
    "deep": Table(((0, 8, 6, 0),), NO_ROAD),
    # valleys bare, a line about 900 m, deep above 2,200 m
    "alpine": Table(((700, 0, 0, 1), (1100, 8, 3, 0), (2200, 8, 5, 0)), NO_ROAD),
    # a late-spring line about 1,700 m, old snow
    "spring": Table(((1500, 0, 0, 2), (1900, 6, 2, 2), (2600, 8, 4, 1)), NO_ROAD),
}

# snowlook.rs SnowParams, in its order
PARAMS = np.array((
    300.0,                  # aspect_m: the line this much lower on a slope facing the pole (higher facing the sun)
    0.70, 1.19,             # steep_tan: snow slides off from 35 deg, none holds past 50 deg (surface.ROCK_SLOPE_DEG 35)
    150.0, 250.0,           # perm_band: permanent snow fades in from 150 m below the climatic snowline to 250 m above
    3.0, 20.0, 0.4,         # patch_m, patch_w: grass tips through a dusting, drifts and melt patches
    0.15, 1.4, 0.2,         # soft, gain, bias of the patch threshold
    0.16, 0.32, 0.75,       # canopy_v, canopy_keep: dark green crowns keep most of their colour (shadows do not)
    0.72, 0.88, 0.12,       # white_v, white_sat: texels already snow (snowy imagery) left as they are
    0.80, 0.45, 1.04,       # shade: the snow's brightness follows the photo's own shading a little
    236.0, 240.0, 246.0,    # colour: fresh
    222.0, 225.0, 230.0,    # settled
    204.0, 207.0, 212.0,    # old
    186.0, 183.0, 178.0,    # dirty
    0.0, 0.6, 0.85, 1.0, 1.0, 1.0, 1.0,    # opacity by depth bin D0..D6
    4.0, 256.0,             # coarse_m, coarse_max: the cover's grid
    6.0, 1.0,               # perm_depth, perm_age
    0.50, 0.80,             # canopy_rel: a green texel this much darker than the ground round it is a crown
    0.0, 0.04,              # canopy_green: green over red (G - R) from which a dark texel is vegetation
    250.0,                  # canopy_r_m: the radius of "the ground round it"
), np.float64)


class Field:
    """A snow state over the world: tables by forcing cell at res degrees, or one table everywhere (uniform)."""

    def __init__(self, res=0.25, cells=None, uniform=None):
        self.res = float(res)
        self.cells = dict(cells or {})      # (ci, cj) -> Table
        self.uniform = uniform

    def table(self, ci, cj):
        return self.uniform if self.uniform is not None else self.cells.get((ci, cj))


def _has_cover(t):
    return t is not None and any(int(r[1]) > 0 for r in t.rows)


_provider = None


def set_provider(fn):
    """W1's snow state: fn(south, west, north, east) -> Field (or None: no snow known there) for BNE_SNOW=real. The
    Field it gives for a box must not change while a spec made from it is in use (the server makes a spec once per
    texture); a change of state is a new spec, and the server re-renders what is shown (W1 / W5)."""
    global _provider
    _provider = fn


def field_for(south, west, north, east):
    if not ON or MODE == "none":
        return None
    if MODE in PRESETS:
        return Field(1.0, uniform=PRESETS[MODE])
    if MODE == "real" and _provider is not None:
        return _provider(south, west, north, east)
    return None


Spec = namedtuple("Spec", "sig field perm")     # field: (res, ci0, cj0, idx int32 (nj, ni), tables, counts) or None


def _window(f, south, west, north, east):
    """(res, ci0, cj0, idx, tables (n, 8, 4) int32, counts (n, 6) int32) of the field's cells round the box, or None
    when none of them has any cover."""
    if f is None:
        return None
    res = f.res
    if f.uniform is not None:
        if not _has_cover(f.uniform):
            return None
        ci0 = int(np.floor((0.5 * (west + east) + 180.0) / res))
        cj0 = int(np.floor((90.0 - 0.5 * (south + north)) / res))
        tabs, idx = [f.uniform], np.zeros((1, 1), np.int32)
    else:
        ci0 = int(np.floor((west + 180.0) / res)) - FIELD_PAD_CELLS
        ci1 = int(np.floor((east + 180.0) / res)) + FIELD_PAD_CELLS
        cj0 = int(np.floor((90.0 - north) / res)) - FIELD_PAD_CELLS
        cj1 = int(np.floor((90.0 - south) / res)) + FIELD_PAD_CELLS
        idx = np.full((cj1 - cj0 + 1, ci1 - ci0 + 1), -1, np.int32)
        tabs, seen = [], {}
        for cj in range(cj0, cj1 + 1):
            for ci in range(ci0, ci1 + 1):
                t = f.table(ci, cj)
                if not _has_cover(t):
                    continue
                k = (tuple(map(tuple, t.rows)), tuple(t.road))
                if k not in seen:
                    seen[k] = len(tabs)
                    tabs.append(t)
                idx[cj - cj0, ci - ci0] = seen[k]
        if not tabs:
            return None
    tables = np.zeros((len(tabs), 8, 4), np.int32)
    counts = np.zeros((len(tabs), 6), np.int32)
    for n, t in enumerate(tabs):
        rows = sorted((int(z), int(c), int(d), int(a)) for z, c, d, a in t.rows)[:8]
        if not rows:
            raise ValueError("snow table without rows")
        for r, row in enumerate(rows):
            if not (0 <= row[1] <= 8 and 0 <= row[2] <= 6 and 0 <= row[3] <= 3):
                raise ValueError(f"snow table row out of range: {row}")
            tables[n, r] = row
        counts[n, 0] = len(rows)
        counts[n, 1:] = [min(max(int(v), 0), 8) for v in tuple(t.road)[:5]]
    return (res, ci0, cj0, idx, tables, counts)


def perm_possible(south, west, north, east):
    """Can permanent snow lie in the box (a polar / alpine Koppen class within a pixel of it)?"""
    import climate
    fast.require(623, "snow_perm_possible")
    return bool(fast._ef.snow_perm_possible(climate._load(), climate.RES, float(south), float(west), float(north),
                                            float(east)))


def spec_for_box(south, west, north, east):
    """The snow a texture over the box (deg) is made with, or None (no snow can lie there: no key part)."""
    if not ON or MODE == "none":
        return None
    win = _window(field_for(south, west, north, east), south, west, north, east)
    perm = perm_possible(south, west, north, east)
    if win is None and not perm:
        return None
    h = hashlib.sha1(repr(("snow", LOOK_VERSION, PARAMS.tobytes(), GEO_N, perm)).encode())
    if win is not None:
        res, ci0, cj0, idx, tables, counts = win
        h.update(repr((res, ci0, cj0, idx.shape)).encode())
        h.update(idx.tobytes())
        h.update(tables.tobytes())
        h.update(counts.tobytes())
    return Spec(("snow", h.hexdigest()[:20]), win, perm)


def key_parts(spec):
    """A spec's part of a revisit key: () for no snow (the key stays what it was), else (its signature,)."""
    return () if spec is None else (tuple(spec.sig),)


def box_of(frame, x0, y0, size):
    """(south, west, north, east) of the square [x0, x0 + size]^2 of frame (its corners and edge midpoints)."""
    s = np.array((0.0, 0.5, 1.0))
    e, n = np.meshgrid(x0 + s * size, y0 + s * size)
    lat, lon, _ = frame.enu_to_geodetic(e.ravel(), n.ravel(), 0.0)
    lat, lon = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
    return float(lat.min()), float(lon.min()), float(lat.max()), float(lon.max())


def spec_for_square(frame, x0, y0, size):
    """spec_for_box of a frame's square (None at once while winter is off: no geodesy)."""
    if not ON or MODE == "none":
        return None
    return spec_for_box(*box_of(frame, x0, y0, size))


def look(spec, frame, x0, y0, size, U, ux0, uy0, usp):
    """The earthfast.SnowLook of a texture over [x0, x0 + size]^2 of frame with height grid U (frame z, [j north from
    uy0, i east from ux0], spacing usp), or None (no spec / no heights)."""
    if spec is None or U is None:
        return None
    U = np.ascontiguousarray(np.asarray(U, np.float64))
    if U.ndim != 2 or min(U.shape) < 2:
        return None
    fast.require(623, "SnowLook")
    t = np.linspace(0.0, 1.0, GEO_N)
    e, n = np.meshgrid(x0 + t * size, y0 + size - t * size)       # row 0 north
    lat, lon, h = frame.enu_to_geodetic(e.ravel(), n.ravel(), 0.0)
    lat = np.asarray(lat, np.float64)
    lon = np.asarray(lon, np.float64)
    # the geodetic height of a point at frame up 0 there: the frame's curvature drop (a far tile's z is lower by it)
    zoff = np.asarray(h, np.float64)
    if spec.perm:
        import climate
        ela = np.asarray(fast._ef.snow_ela(lat, lon, climate._load(), climate.RES), np.float64)
    else:
        ela = np.full(lat.shape, 1.0e9)
    shp = (GEO_N, GEO_N)
    geo = tuple(np.ascontiguousarray(a.reshape(shp)) for a in (lat, lon, zoff, ela))
    field = None
    if spec.field is not None:
        res, ci0, cj0, idx, tables, counts = spec.field
        field = (float(res), int(ci0), int(cj0), np.ascontiguousarray(idx, np.int32),
                 np.ascontiguousarray(tables, np.int32), np.ascontiguousarray(counts, np.int32))
    return fast._ef.SnowLook((float(x0), float(y0), float(size)), (frame.lat0, frame.lon0), U,
                             (float(ux0), float(uy0), float(usp)), geo, field, PARAMS)


def look_from_rock(spec, frame, rock):
    """look() from a far texture's rock inputs (rockface.tile_inputs: (U, (x0, y0, size, ux0, uy0, usp, lat, lon))).
    A spec whose look cannot be made (no heights, an error): None, and the texture is not kept (revisit.fault: its
    key says snow)."""
    if spec is None:
        return None
    try:
        if rock is None:
            raise ValueError("no height grid")
        U, (x0, y0, size, ux0, uy0, usp, _lat, _lon) = rock
        sl = look(spec, frame, x0, y0, size, U, ux0, uy0, usp)
        if sl is None:
            raise ValueError("no look")
        return sl
    except Exception as e:
        import logging
        import revisit
        logging.getLogger("earthd.snowcover").debug("snow look not made: %s: %s", type(e).__name__, e)
        revisit.fault()
        return None


def apply(spec, rgb, frame, x0, y0, size, U, ux0, uy0, usp):
    """rgb (px, px, 3) over [x0, x0 + size]^2 of frame with its snow, or rgb itself (no snow on it)."""
    sl = look(spec, frame, x0, y0, size, U, ux0, uy0, usp)
    if sl is None:
        return rgb
    out = sl.apply(np.ascontiguousarray(np.asarray(rgb, np.uint8)[..., :3]))
    return rgb if out is None else out


def apply_kept(spec, rgb, frame, x0, y0, size, U, ux0, uy0, usp):
    """apply() for a texture that is kept under its spec's key: when its snow cannot be made (no heights, an error) it
    is shown without it and not kept (revisit.fault: its key says snow)."""
    if spec is None:
        return rgb
    try:
        if U is None:
            raise ValueError("no height grid")
        return apply(spec, rgb, frame, x0, y0, size, U, ux0, uy0, usp)
    except Exception as e:          # (the photo without its snow, this once)
        import logging
        import revisit
        logging.getLogger("earthd.snowcover").debug("snow not applied: %s: %s", type(e).__name__, e)
        revisit.fault()
        return rgb


def hello(lat=None, lon=None):
    """The mod's part of the server's hello (winterlook.lua): None while off, else the mode and, for a preset, its
    cover and depth at the anchor's height band (the ground cover's density, the snow layer's look)."""
    if not ON:
        return None
    m = {"mode": MODE}
    t = PRESETS.get(MODE)
    if t is not None:
        top = max(t.rows, key=lambda r: (r[1], r[2]))
        m["cover"], m["depth"] = int(top[1]), int(top[2])
    return m
