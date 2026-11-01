"""Low-frequency mineral colour from cached regional imagery, without downloads.

Sampling is on a shared geographic lattice so neighbouring cliff tiles receive
the same tint. Missing/vegetated/wet samples retain the biome fallback. Raw
imagery is used only for colour, never as shading or surface geometry.
"""
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image

GRID_M = 64.0
RADIUS_M = 64.0
METRES_PER_DEGREE = 111320.0


@lru_cache(maxsize=96)
def _tile(path, stamp):
    with Image.open(path) as im:
        return np.asarray(im.convert('RGB')).copy()


def _samples(cache_dir, lat, lon):
    """First available cached photo in the normal source order, RGB or zero."""
    import imagery
    result = np.zeros((*lat.shape, 3), float)
    remaining = np.ones(lat.shape, bool)
    root = Path(cache_dir)/'imagery'
    for name in imagery.DEFAULT_CHAIN:
        src = imagery.SOURCES[name]
        if src.bounds:
            south, west, north, east = src.bounds
            if lat.max() < south or lat.min() > north or lon.max() < west or lon.min() > east:
                continue
        # Near photos first, then cached coarser coverage. Never initiate a fetch.
        for zoom in range(src.max_zoom, max(src.min_zoom, src.max_zoom-3)-1, -1):
            indices = np.flatnonzero(remaining)
            if not len(indices):
                return result
            x, y = imagery.mercator_px(lat.flat[indices], lon.flat[indices], zoom)
            tx, ty = np.floor(x/256).astype(int), np.floor(y/256).astype(int)
            keys, inverse = np.unique(np.column_stack((tx, ty)), axis=0, return_inverse=True)
            for k, (ix, iy) in enumerate(keys):
                path = root/name/str(zoom)/str(ix)/f'{iy}.jpg'
                try:
                    im = _tile(str(path), path.stat().st_mtime_ns)
                except (OSError, ValueError):
                    continue
                take = inverse == k
                px = np.clip(((x[take]/256-ix)*im.shape[1]).astype(int), 0, im.shape[1]-1)
                py = np.clip(((y[take]/256-iy)*im.shape[0]).astype(int), 0, im.shape[0]-1)
                rgb = im[py, px]
                # Source coverage voids must fall through to the next source.
                valid = (rgb.max(axis=1) > 8) & (rgb.min(axis=1) < 245)
                target = indices[take][valid]
                result.reshape(-1, 3)[target] = rgb[valid]
                remaining.flat[target] = False
    return result


def sample(cache_dir, lat, lon, *, grid_m=GRID_M, radius_m=RADIUS_M):
    """Bilinear regional colour field from exposed-ground neighbourhood medians.

    Evaluate a few hundred lattice nodes, not hundreds of thousands of mesh
    vertices. Reject canopy/water chromaticity and deep shadows before averaging.
    Zero denotes missing evidence, leaving the climate-derived tint intact.
    """
    lat, lon = np.broadcast_arrays(lat, lon)
    x, y = lon*METRES_PER_DEGREE/grid_m, lat*METRES_PER_DEGREE/grid_m
    ix, iy = np.floor(x).astype(int), np.floor(y).astype(int)
    xs = np.arange(ix.min(), ix.max()+2)
    ys = np.arange(iy.min(), iy.max()+2)
    gx, gy = np.meshgrid(xs*grid_m, ys*grid_m)
    offsets = np.linspace(-radius_m, radius_m, 9)
    ox, oy = np.meshgrid(offsets, offsets)
    la = (gy[..., None]+oy.ravel())/METRES_PER_DEGREE
    lo = (gx[..., None]+ox.ravel()/np.maximum(np.cos(np.radians(la)), .1))/METRES_PER_DEGREE
    rgb = _samples(cache_dir, la, lo)
    r, g, b = rgb.transpose(3, 0, 1, 2)
    light = rgb @ np.array((.2126, .7152, .0722))
    green_excess = g-(r+b)*.5
    valid = ((light > 55) & (light < 235) & (g < r*1.04) & (b < r*1.08)
             & (green_excess < light*.045))
    # Infer a regional mineral colour from the brighter exposed ground. The
    # local photo's shadow pattern must not become the cliff's albedo pattern.
    values = np.sort(np.where(valid, light, np.inf), axis=2)
    count = valid.sum(axis=2)
    bright_at = np.minimum((count*.6).astype(int), values.shape[2]-1)
    threshold = np.take_along_axis(values, bright_at[..., None], axis=2)
    bright = valid & (light >= threshold)
    masked = np.ma.array(rgb, mask=np.broadcast_to(~bright[..., None], rgb.shape))
    field = np.ma.median(masked, axis=2).filled(0)
    reliable = bright.sum(axis=2) >= 3
    field *= reliable[..., None]
    i, j = ix-xs[0], iy-ys[0]
    fx, fy = x-ix, y-iy
    out = np.zeros((*x.shape, 3))
    weight = np.zeros(x.shape)
    for di, dj, w in ((0, 0, (1-fx)*(1-fy)), (1, 0, fx*(1-fy)),
                      (0, 1, (1-fx)*fy), (1, 1, fx*fy)):
        w = w*reliable[j+dj, i+di]
        out += w[..., None]*field[j+dj, i+di]
        weight += w
    return out/np.maximum(weight[..., None], 1e-6)
