"""Low-frequency mineral colour from cached regional imagery, without downloads.

Sampling is on a shared geographic lattice so neighbouring cliff tiles receive
the same tint. Near cliffs infer mineral hue and reject photo shadows; distant
cliffs use the blurred terrain photo including its broad baked shading. Neither
path changes surface geometry or initiates imagery downloads.
"""
from cliffsettings import CLIFF_KERNEL
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image

GRID_M = 64.0
RADIUS_M = 64.0
REGIONAL_GRID_M = 512.0
REGIONAL_RADIUS_M = 512.0
# With no usable imagery anywhere, use neutral stone rather than a blue climate tint.
NEUTRAL_STONE = (180.0, 180.0, 180.0)
METRES_PER_DEGREE = 111320.0
# Shadow rejection already establishes mineral brightness; do not pull it back
# toward the darker authored texture when matching distant cliffs.
CANYON_LIGHT_BLEND = 1.0
CANYON_HUE_BLEND = .85


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
    Wider regional evidence fills rejected nodes before interpolation. Both scales
    use geographic lattices, so adjoining tiles resolve the same boundary colour.
    """
    import fast
    fast.require(CLIFF_KERNEL, "continuous cliff mineral colour field")
    lat, lon = np.broadcast_arrays(lat, lon)
    if not lat.size:
        return np.empty((*lat.shape, 3))
    probes, indices, weights = fast._ef.cliff_photo_queries(
        np.ascontiguousarray(lat.ravel(), np.float64), np.ascontiguousarray(lon.ravel(), np.float64),
        grid_m, radius_m)
    # Probe centres identify the local lattice nodes independently of query order.
    centers = probes[:, probes.shape[1]//2, :]
    regional, ri, rw = fast._ef.cliff_photo_queries(
        np.ascontiguousarray(centers[:, 0]), np.ascontiguousarray(centers[:, 1]),
        REGIONAL_GRID_M, REGIONAL_RADIUS_M)
    regional_rgb = _samples(cache_dir, regional[:, :, 0], regional[:, :, 1])
    fallback = fast._ef.cliff_photo_tints(np.ascontiguousarray(regional_rgb, np.float64), ri, rw,
        np.tile(NEUTRAL_STONE, (len(regional), 1)), regional=True)
    rgb = _samples(cache_dir, probes[:, :, 0], probes[:, :, 1])
    return fast._ef.cliff_photo_tints(np.ascontiguousarray(rgb, np.float64), indices, weights,
        fallback).reshape(*lat.shape, 3)


def _transfer(rgb, encode):
    import fast
    fast.require(CLIFF_KERNEL, "cliff colour transfer")
    rgb = np.asarray(rgb, float)
    return fast._ef.cliff_color_transfer(np.ascontiguousarray(rgb.ravel()), encode).reshape(rgb.shape)


def linear(rgb):
    """sRGB to linear light; preserve material-factor headroom."""
    return _transfer(rgb, False)


def srgb(rgb):
    """Linear light to sRGB, retaining HDR values."""
    return _transfer(rgb, True)


def material_tint(rgb_linear, reference, amount, count=None, *, hue_amount=None, exposure=1.0):
    """Native satellite-to-material balance, encoded for the engine's vertex input."""
    import fast
    import cliffmaterial as cm
    fast.require(CLIFF_KERNEL, "cliff material tint")
    rgb = np.zeros((count, 3)) if rgb_linear is None else np.asarray(rgb_linear, float)
    if rgb.shape[-1:] != (3,):
        raise ValueError("one RGB colour per point required")
    colors = fast._ef.cliff_material_tints(np.ascontiguousarray(rgb.reshape(-1, 3)),
        np.asarray(reference, float).tolist(), amount, cm.SATELLITE_HEADROOM, exposure,
        rgb_linear is not None, hue_amount)
    return colors.reshape(*rgb.shape[:-1], 4)


def canyon_tint(biomes, rgb=None):
    """Near canyon mineral hue without satellite shadow patterns."""
    import fast
    import cliffmaterial as cm
    fast.require(CLIFF_KERNEL, "canyon mineral tint")
    shape = np.shape(biomes)
    photo = np.zeros((*shape, 3)) if rgb is None else np.asarray(rgb, float)
    if photo.shape != (*shape, 3):
        raise ValueError("terrain colours must be finite RGB, one per point")
    reference = linear(cm.CANYON_MEDIAN_SRGB) * cm.CANYON_COLOR_FACTOR
    colors = fast._ef.cliff_material_tints(np.ascontiguousarray(photo.reshape(-1, 3)),
        reference.tolist(), 1.0, cm.SATELLITE_HEADROOM, 1.0, True,
        CANYON_HUE_BLEND, CANYON_LIGHT_BLEND)
    return colors.reshape(*shape, 4)
