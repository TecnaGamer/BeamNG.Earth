"""Original coarse-to-fine lattice oracle; never imported by the server."""
import math
import numpy as np
from cliffrocks import (TOE_REACH_M, EDGE_BLEND_M, TERRAIN_SPAN_M, CLEARANCE_SEARCH_M,
                        GRID_M, WORLD_M_PER_DEG, SHORE_DRY_MIN_M, RENDER_SUBDIVISIONS)


def refine(xs, ys, keep, biomes, box, elev, color_at, shore=None, horizontal_scale=1.0, subdivisions=RENDER_SUBDIVISIONS):
    n = subdivisions
    # Toe propagation, edge fade, the two-axis terrain envelope and outward
    # clearance all consume context. Keep the full dependency radius outside
    # the exported tile so adjoining tiles produce the same fitted boundary.
    context = TOE_REACH_M + EDGE_BLEND_M + 2*TERRAIN_SPAN_M + CLEARANCE_SEARCH_M + GRID_M/n
    ax = math.ceil(context/(GRID_M*horizontal_scale))
    ay = math.ceil(context/GRID_M)
    i0 = max(0, np.searchsorted(xs, box[0])-ax)
    i1 = min(len(xs)-1, np.searchsorted(xs, box[2])+ax)
    j0 = max(0, np.searchsorted(ys, box[1])-ay)
    j1 = min(len(ys)-1, np.searchsorted(ys, box[3])+ay)
    rx = xs[i0] + np.arange((i1-i0)*n+1)*(GRID_M/n)
    ry = ys[j0] + np.arange((j1-j0)*n+1)*(GRID_M/n)
    rk = np.repeat(np.repeat(keep[j0:j1, i0:i1], n, axis=0), n, axis=1)
    rb = np.repeat(np.repeat(biomes[j0:j1, i0:i1], n, axis=0), n, axis=1)
    xx, yy = np.meshgrid(rx, ry)
    h = np.asarray(elev(yy/WORLD_M_PER_DEG, (xx/WORLD_M_PER_DEG+180)%360-180), float)
    rk &= np.isfinite(h[:-1, :-1]) & np.isfinite(h[1:, :-1]) & np.isfinite(h[:-1, 1:]) & np.isfinite(h[1:, 1:])
    if shore is not None:
        rs = np.repeat(np.repeat(shore[j0:j1, i0:i1], n, axis=0), n, axis=1)
        dry = (h[:-1, :-1] > SHORE_DRY_MIN_M) & (h[1:, :-1] > SHORE_DRY_MIN_M)
        dry &= (h[:-1, 1:] > SHORE_DRY_MIN_M) & (h[1:, 1:] > SHORE_DRY_MIN_M)
        rk &= ~rs | dry  # retain the wall above the waterline, never its submerged continuation
    rgb = None
    if color_at is not None:
        cx, cy = np.meshgrid((rx[:-1]+rx[1:])/2, (ry[:-1]+ry[1:])/2)
        rgb = color_at(cy/WORLD_M_PER_DEG, (cx/WORLD_M_PER_DEG+180)%360-180)
    return rx, ry, h, rk, rb, rgb
