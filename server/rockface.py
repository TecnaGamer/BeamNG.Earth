"""Rock on steep ground in the far tiles' photos (2026-10-15, world tour: Lauterbrunnen's and Geiranger's
cliffs were the aerial photo stretched down the wall, vertical streaks of the trees and meadows at its top).

A far tile's texture is the photo seen from above, mapped straight down: a wall gets a few texels stretched
over its height. Where the ground is steeper than 38 degrees the photo's colour is drawn toward rock: its own
colour desaturated and darkened, with a value noise taken at the texel's 3D position (so it is even on the
wall, not streaked) and faint horizontal strata. Noise finer than a texel's height on the wall is left out
(it would alias into streaks again), so the steepest walls are a plain rock tint of the photo's colour.

    rgb = rock(rgb, frame, x0, y0, size, U, ux0, uy0, usp)
      rgb: (px, px, 3) uint8, row 0 north, covering [x0, x0 + size)^2 of the anchor frame
      U: the tile's height grid [j, i] (j north from uy0), spacing usp, origin (ux0, uy0)
"""
import numpy as np

import fast     # the texel loop is the Rust kernel earthfast.rock


def inputs(frame, x0, y0, size, U, ux0, uy0, usp):
    return (np.asarray(U, np.float64), (x0, y0, size, ux0, uy0, usp, frame.lat0, frame.lon0))


def tile_inputs(frame, x0, y0, size, px, grid):
    """Rock inputs for a far texture with the usual gutter, or None for a tile without heights."""
    if grid is None:
        return None
    import imagery
    g = np.asarray(grid, np.float64)
    if g.ndim != 2 or min(g.shape) < 2:
        return None
    return inputs(frame, *imagery.gutter_square(x0, y0, size, px), g, x0, y0, size / (g.shape[1] - 1))


def rock(rgb, frame, x0, y0, size, U, ux0, uy0, usp):
    """rgb with the steep texels drawn toward rock (see the module doc); rgb itself when nothing is steep."""
    rgb = np.asarray(rgb)
    fast.require(559, "rockface.rock")
    got = fast._ef.rock(np.asarray(rgb, np.uint8), *inputs(frame, x0, y0, size, U, ux0, uy0, usp))
    return rgb if got is None else got
