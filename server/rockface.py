"""Rock on steep ground in the far tiles' photos (2026-10-15, world tour: Lauterbrunnen's and Geiranger's
cliffs were the aerial photo stretched down the wall, vertical streaks of the trees and meadows at its top).

A far tile's texture is the photo seen from above, mapped straight down: a wall gets a few texels stretched
over its height. Where the ground is steeper than STEEP_DEG the photo's colour is drawn toward rock: its own
colour desaturated and darkened, with a value noise taken at the texel's 3D position (so it is even on the
wall, not streaked) and faint horizontal strata. Noise finer than a texel's height on the wall is left out
(it would alias into streaks again), so the steepest walls are a plain rock tint of the photo's colour.

    rgb = rock(rgb, frame, x0, y0, size, U, ux0, uy0, usp)
      rgb: (px, px, 3) uint8, row 0 north, covering [x0, x0 + size)^2 of the anchor frame
      U: the tile's height grid [j, i] (j north from uy0), spacing usp, origin (ux0, uy0)
"""
import math

import numpy as np

import fast     # the texel loop is the Rust kernel earthfast.rock
import dmath

STEEP_DEG = (38.0, 58.0)    # rock from the first slope, fully at the second
OCTAVES = ((28.0, 0.55), (9.0, 0.3), (3.0, 0.15))  # (m, weight) of the noise
STRATA_M = 7.0              # the strata's spacing (m of height)
STRATA_W = 0.10
ROCK_TINT = np.array((1.04, 1.0, 0.94), np.float32)   # warm grey
DESAT = 0.7                 # how much of the photo's colour goes
DARKEN = 0.82


def steepness(slope):
    """0 .. 1 from the slope (rise / run)."""
    a, b = (dmath.tan(math.radians(d)) for d in STEEP_DEG)
    t = np.clip((slope - a) / (b - a), 0.0, 1.0)
    return t * t * (3 - 2 * t)


def rock(rgb, frame, x0, y0, size, U, ux0, uy0, usp):
    """rgb with the steep texels drawn toward rock (see the module doc); rgb itself when nothing is steep."""
    rgb = np.asarray(rgb)
    U = np.asarray(U, np.float64)
    # no texel can be steeper than the grid's steepest cell edge (the bilinear surface's gradient within a cell
    # interpolates its edges' differences; np.gradient averages it): below the rock slope there, nothing to do
    # (most tiles: the full-resolution heights and gradient below were computed for them all, ~25 % of a far
    # tile's CPU, 2026-10-15)
    if U.shape[0] >= 2 and U.shape[1] >= 2:
        gmax = math.hypot(float(np.abs(np.diff(U, axis=1)).max()), float(np.abs(np.diff(U, axis=0)).max())) / usp
        if steepness(gmax) <= 0.01:
            return rgb
    fast.require(0, "rockface.rock")
    a, b = (dmath.tan(math.radians(d)) for d in STEEP_DEG)
    got = fast._ef.rock(np.ascontiguousarray(rgb, np.uint8), np.ascontiguousarray(U), float(x0), float(y0),
                        float(size), float(ux0), float(uy0), float(usp), frame.lat0, frame.lon0, 111320.0,
                        111320.0 * dmath.cos(math.radians(frame.lat0)), a, b, np.array(OCTAVES, np.float64),
                        STRATA_M, STRATA_W, tuple(float(v) for v in ROCK_TINT), DESAT, DARKEN)
    return rgb if got is None else got
