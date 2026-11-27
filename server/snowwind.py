"""Seasonal snow transport from hourly wind and directional DEM shelter."""

import math

import numpy as np

import dmath
import dnp
import geodesy

# Wind speed is measured at 10 m. Li and Pomeroy (1997) found mean onset speeds
# of 7.7 and 9.9 m/s for dry and wet snow, respectively. Temperature is a proxy
# used to interpolate between those observations, not a local snow wetness sensor.
DRY_ONSET = 7.7
WET_ONSET = 9.9
HISTORY_H = 72
HALF_LIFE_H = 24
VECTOR_SCALE = 127
_WEIGHTS = [dmath.pow(2, -age / HALF_LIFE_H) for age in range(HISTORY_H)]
_TRANSPORT_SCALE = VECTOR_SCALE / sum(_WEIGHTS)

# The shelter stencil samples the upwind terrain at two physical fetch lengths.
# Wind redistribution around slopes and ridges is a 10–100 m process, with
# shelter from farther terrain at larger slope breaks (Winstral et al. 2002).
FETCH_M = (100.0, 300.0)
DEM_ZOOM = 12
GRID_STEP = 0.1 / 128
VERSION = 1


def transport(times, directions, speeds, temps, upto):
    """East/north wind transport, in signed 1/127 units of available exposure."""
    if not directions:
        return 0, 0
    end = times[upto]
    east = north = 0.0
    for i in range(upto, -1, -1):
        age = max(0, (end - times[i]) // 3600)
        if age >= HISTORY_H:
            break
        direction = directions[i] if i < len(directions) else None
        if (isinstance(direction, bool) or not isinstance(direction, (int, float))
                or not 0 <= direction <= 360 or speeds[i] <= 0 or temps[i] < -10000):
            continue
        wet = min(max(temps[i] / 200 + 0.5, 0), 1)
        onset = DRY_ONSET + wet * (WET_ONSET - DRY_ONSET)
        speed = speeds[i] / 10
        if speed <= onset:
            continue
        excess = _WEIGHTS[age] * (1 - dnp.sq(onset / speed))
        angle = math.radians(direction % 360)
        # Meteorological direction is where the wind comes from.
        east -= excess * dmath.sin(angle)
        north -= excess * dmath.cos(angle)
    return round(east * _TRANSPORT_SCALE), round(north * _TRANSPORT_SCALE)


def _winds_at(field, lat, lon):
    res, ci0, cj0, vectors = field
    x = (lon + 180) / res - 0.5 - ci0
    y = (90 - lat) / res - 0.5 - cj0
    ny, nx = vectors.shape[:2]
    period = round(360 / res)
    x += np.rint(((nx - 1) * 0.5 - x) / period) * period
    i, j = np.floor(x).astype(int), np.floor(y).astype(int)
    fx, fy = x - i, y - j
    out = np.zeros((*np.broadcast_shapes(lat.shape, lon.shape), 2))
    for dj, wy in ((0, 1 - fy), (1, fy)):
        for di, wx in ((0, 1 - fx), (1, fx)):
            jj, ii = j + dj, i + di
            valid = (jj >= 0) & (ii >= 0) & (jj < ny) & (ii < nx)
            out += vectors[jj.clip(0, ny - 1), ii.clip(0, nx - 1)] * np.where(valid, wx * wy, 0)[..., None]
    return out / VECTOR_SCALE


def _upwind(lat, lon, east, north, metres):
    """Spherical forward geodesic at a short, physical fetch distance."""
    step = metres / geodesy.A
    sin, cos = dmath.sin(step), dmath.cos(step)
    lat, lon = np.deg2rad(lat), np.deg2rad(lon)
    sl, cl = dnp.sin(lat), dnp.cos(lat)
    new_lat = dnp.arcsin(np.clip(sl * cos - cl * sin * north, -1, 1))
    new_lon = lon + dnp.arctan2(-east * sin * cl, cos - sl * dnp.sin(new_lat))
    return np.rad2deg(new_lat), (np.rad2deg(new_lon) + 180) % 360 - 180


def shelter_grid(field, sample, south, west, north, east):
    """The dimensionless shelter effect on a fixed geographic grid, with one extra node at each edge."""
    res, ci0, cj0, vectors = field
    rows, cols = np.nonzero(np.any(vectors != 0, axis=2))
    centres_lon = (ci0 + cols + 0.5) * res - 180
    mid = (west + east) * 0.5
    centres_lon += np.rint((mid - centres_lon) / 360) * 360
    centres_lat = 90 - (cj0 + rows + 0.5) * res
    # A cell has no influence more than one cell away from its centre.
    west, east = max(west, centres_lon.min() - res), min(east, centres_lon.max() + res)
    south, north = max(south, centres_lat.min() - res), min(north, centres_lat.max() + res)
    if west > east or south > north:
        return None
    gap = GRID_STEP
    i0 = math.floor((west + 180) / gap) - 1
    i1 = math.ceil((east + 180) / gap) + 1
    j0 = math.floor((90 - north) / gap) - 1
    j1 = math.ceil((90 - south) / gap) + 1
    lon, lat = np.meshgrid(np.arange(i0, i1 + 1) * gap - 180,
                           (90 - np.arange(j0, j1 + 1) * gap).clip(-90, 90))
    wind = _winds_at(field, lat, lon)
    speed = dnp.hypot(wind[..., 0], wind[..., 1])
    active = speed > 0
    if not active.any():
        return None
    y, x = lat[active], (lon[active] + 180) % 360 - 180
    dy, dx = wind[..., 1][active] / speed[active], wind[..., 0][active] / speed[active]
    sites = [(y, x)] + [_upwind(y, x, dx, dy, step) for step in FETCH_M]
    heights = sample(np.stack([p[0] for p in sites]), np.stack([p[1] for p in sites]), DEM_ZOOM)
    slopes = (heights[1:] - heights[0]) / np.asarray(FETCH_M)[:, None]
    # The steepest upwind sight line determines shelter. The sine of that angle
    # bounds the response and is zero for level terrain.
    slope = np.nan_to_num(np.max(slopes, axis=0), nan=0, posinf=0, neginf=0)
    relief = slope / dnp.hypot(1, slope)
    out = np.full(lat.shape, VECTOR_SCALE, dtype=np.uint8)
    out[active] = VECTOR_SCALE + np.rint(VECTOR_SCALE * speed[active] * relief).clip(-VECTOR_SCALE, VECTOR_SCALE).astype(int)
    if (out == VECTOR_SCALE).all():
        return None
    # PackGrid's coordinates are edges, so shift by half a sample.
    return out, 90 - (j0 - 0.5) * gap, (i0 - 0.5) * gap - 180, gap, float(VECTOR_SCALE), 255
