"""Sandy shores in ESA WorldCover (GitHub #578: rocks and grass on Perth's beaches).

WorldCover has no sand class. A beach is a strip of "bare / sparse vegetation" (60) between the water (80) and the
land, and the 10 m pixels of its berm are often "grassland" (30): at Cottesloe (-32.021, 115.752) the sea, ~8 m of bare,
~15 m of grassland, ~40 m of bare, then the town. vegetation.py scattered its desert rocks on the bare pixels and the
mod's GroundCover grew grass on the grassland ones (surface.py GRASS).

A sample is sand when it is bare, or grassland with bare land within GRASS_BARE_M, and open water (at least
OPEN_WATER_M across: a stream's bank keeps its grass) lies within SHORE_M. Pure functions of the land cover, numpy only.
"""
import numpy as np
import dnp

BARE, GRASS, WATER = 60, 30, 80
SHORE_M = 60.0          # m from open water a beach's sand reaches (Cottesloe's: ~60 m to the promenade)
OPEN_WATER_M = 40.0     # m of water across, at least: the sea, a lake, a wide river (not a stream or a ditch)
GRASS_BARE_M = 20.0     # m: a grassland pixel this near bare land on a shore is the beach's berm, not a lawn
DIRECTIONS = 8
SHORE_STEPS = (15.0, 30.0, 45.0, 60.0)      # m: where the water is looked for, in each direction (point test)


def sand_points(classes, sample):
    """bool per point: sand by the rule above. classes: the points' WorldCover classes; sample(idx, dx, dy) -> the
    classes at points idx moved (dx, dy) m (east, north). Only bare and grassland points are tested."""
    classes = np.asarray(classes)
    out = np.zeros(classes.shape, bool)
    cand = np.flatnonzero((classes == BARE) | (classes == GRASS))
    if not len(cand):
        return out
    ang = np.arange(DIRECTIONS) * (2 * np.pi / DIRECTIONS)
    dirs = np.c_[dnp.cos(ang), dnp.sin(ang)]
    shore = np.zeros(len(cand), bool)
    for ux, uy in dirs:
        for d in SHORE_STEPS:
            todo = ~shore
            if not todo.any():
                break
            idx = cand[todo]
            hit = (sample(idx, ux * d, uy * d) == WATER) & \
                (sample(idx, ux * (d + OPEN_WATER_M), uy * (d + OPEN_WATER_M)) == WATER)
            shore[np.flatnonzero(todo)[hit]] = True
    grass = shore & (classes[cand] == GRASS)
    if grass.any():
        gi = np.flatnonzero(grass)
        near_bare = np.zeros(len(gi), bool)
        for ux, uy in dirs:
            for d in (GRASS_BARE_M / 2, GRASS_BARE_M):
                near_bare |= sample(cand[gi], ux * d, uy * d) == BARE
        shore[gi[~near_bare]] = False
    out[cand[shore]] = True
    return out


def _box_sum(m, r, edge=False):
    """Sum of m (2D) over the (2r + 1)^2 box round each cell (cells outside count 0, or as the nearest edge cell)."""
    a = np.pad(m.astype(np.int32), ((r + 1, r), (r + 1, r)), mode="edge" if edge else "constant")
    c = a.cumsum(0).cumsum(1)
    return c[2 * r + 1:, 2 * r + 1:] - c[:-2 * r - 1, 2 * r + 1:] - c[2 * r + 1:, :-2 * r - 1] + c[:-2 * r - 1, :-2 * r - 1]


def sand_grid(classes, square):
    """bool grid: sand by the rule above on a land-cover grid (2D, `square` m between samples; box neighbourhoods:
    the grid's own approximation of the point test). Water beyond the grid is not seen."""
    classes = np.asarray(classes)
    if classes.ndim != 2 or min(classes.shape) < 2:
        return np.zeros(classes.shape, bool)
    cand = (classes == BARE) | (classes == GRASS)
    if not cand.any():
        return np.zeros(classes.shape, bool)
    water = classes == WATER
    if not water.any():
        return np.zeros(classes.shape, bool)
    r_open = max(1, int(round(OPEN_WATER_M / 2 / square)))
    n_open = dnp.sq(2 * r_open + 1)
    core = _box_sum(water, r_open, edge=True) >= n_open     # water OPEN_WATER_M across round it (the sea
    # running on past the grid's edge)
    shore = _box_sum(core, max(1, int(round((SHORE_M + OPEN_WATER_M / 2) / square)))) > 0
    near_bare = _box_sum(classes == BARE, max(1, int(round(GRASS_BARE_M / square)))) > 0
    return shore & ((classes == BARE) | ((classes == GRASS) & near_bare))
