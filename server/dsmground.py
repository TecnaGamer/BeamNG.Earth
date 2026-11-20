"""The ground under a surface model: buildings and tree stands taken out of a DSM (2026-10-15).

Outside the national lidar models Mapterhorn's elevation is Copernicus GLO-30, a surface model: a city block
stands 5-25 m above its streets (Mexico City's Zocalo, a flat square, read 9-20 m below the blocks around it;
roads graded into the ground ran in trenches and the square was a pit), and Botafogo's beach stood several
metres up beside its towers. A morphological opening (the lowest surface a window of WINDOW_M can reach from
below) keeps the terrain's shapes wider than the window and cuts what is narrower: a block between streets,
a building, a small stand of trees. What it cuts beyond a threshold (higher on slopes, where a narrow ridge or
knoll is real) is taken off, softly at its edge; larger forests and whole hills stay as they are.

    ground(a, px_m) -> float32 array (a: elevation [rows, cols] in m, px_m: its pixel size in m)
"""
import numpy as np

import fast

WINDOW_M = 400.0        # features narrower than this are surface objects (blocks, and contiguous blocks between
                        # streets too narrow for 30 m pixels: Mexico City's historic centre needed ~400 m)
THRESHOLD_M = 2.0       # a cut this deep at least is an object (GLO-30's noise is ~1-2 m)
SLOPE_K = 0.35          # ... plus this times the ground's rise across half a window (steep ground: real knolls)
MAX_CUT_M = 45.0        # never lower the ground more than this (a tower's footprint in a 30 m pixel)
RELIEF_M = 600.0        # the window the local relief is measured over ...
RELIEF_FLAT_M = 40.0    # ... cut fully where the opened ground's relief is under this, not at all over
RELIEF_HILLS_M = 90.0   # this: the opening also shaves real peaks and ridges, so mountains are left alone
RELIEF_OPEN_M = 150.0   # the relief is that of the ground opened this much (towers out, hills kept)
TERRAIN_CUT_M = (35.0, 55.0)    # a cut this deep within a window is a hill, not a building (GLO-30's blocks and
                                # trees stand 5-30 m): nothing is cut around it (fully over the second value)


def ground(a, px_m, window_m=WINDOW_M, threshold_m=THRESHOLD_M, slope_k=SLOPE_K, max_cut_m=MAX_CUT_M,
           relief_m=RELIEF_M):
    """a with the surface objects narrower than window_m taken down to the ground around them (where the ground
    is flat enough to tell them from hills). The work is the native module's (earthfast dsm_ground, 2026-10-23: the
    same float32 steps as the numpy code it replaced, to the bit, ~3.7x faster and off the interpreter's lock):

    - the opening o of a over w (window_m in samples): cut = a - o;
    - the opened ground's rise across half a window (the gradient of o's w // 2 box mean): thr = threshold_m +
      slope_k * rise; k = clip((cut - thr) / max(threshold_m, 0.5), 0, 1) (a soft edge);
    - times clip((RELIEF_HILLS_M - relief) / (RELIEF_HILLS_M - RELIEF_FLAT_M), 0, 1), relief the max - min over
      relief_m of the ground opened RELIEF_OPEN_M (towers out, hills kept: Sugarloaf Mountain's 300 m);
    - times clip((TERRAIN_CUT_M[1] - deep) / (TERRAIN_CUT_M[1] - TERRAIN_CUT_M[0]), 0, 1), deep the cut's max over
      w (a hill nearby: nothing cut around it);
    - a - min(cut * k, max_cut_m)."""
    fast.require(565, "dsmground.ground")
    return fast._ef.dem_ground(np.ascontiguousarray(a, np.float32), px_m, window_m, threshold_m, slope_k,
                               max_cut_m, relief_m, RELIEF_OPEN_M, RELIEF_HILLS_M, RELIEF_FLAT_M, *TERRAIN_CUT_M)
