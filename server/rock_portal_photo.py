"""Local cliff colours for bare-rock tunnel mouths from cached terrain imagery."""

from functools import lru_cache
from pathlib import Path
import time

import numpy as np
from PIL import Image

import imagery
import revisit
import rockface


# Compare both banks, a few metres into the hillside and beyond the cut.
BANK_OFFSETS = np.array([3., 6., 9., 12.])
INWARD_OFFSETS = np.array([2., 6., 10., 14.])
SLOPE_PROBE_M = 5.
TONE = imagery.tone_lut(1.)
LUMA = np.array((0.299, 0.587, 0.114))
PHOTO_RETRY_S = 900.


@lru_cache(maxsize=24)
def _photo_tile(path, mtime):
    with Image.open(path) as image:
        rgb = image.convert("RGB")
        if rgb.size != (imagery.TILE, imagery.TILE):
            rgb = rgb.resize((imagery.TILE, imagery.TILE), Image.Resampling.BILINEAR)
        tile = np.asarray(rgb, np.uint8)
    return None if imagery.is_uniform(tile) else tile


def _read_pixels(root, source, zoom, lat, lon):
    """Copy cached pixels into rows, leaving cache misses as NaN."""
    x, y = imagery.mercator_px(lat, lon, zoom)
    n = imagery.TILE
    i, j = np.floor(x).astype(int), np.floor(y).astype(int)
    keys = np.column_stack(((i // n) % (1 << zoom), j // n))
    result = np.full((len(lat), 3), np.nan)
    for tx, ty in np.unique(keys, axis=0):
        path = root / source / str(zoom) / str(tx) / f"{ty}.jpg"
        try:
            tile = _photo_tile(path, path.stat().st_mtime_ns)
        except (OSError, ValueError):
            continue
        if tile is not None:
            at = (keys[:, 0] == tx) & (keys[:, 1] == ty)
            result[at] = TONE[tile[j[at] % n, i[at] % n]] / 255.
    return result


def _cached_colours(root, lat, lon):
    """Use the highest local cached resolution of terrain's preferred source."""
    chain = list(imagery.DEFAULT_CHAIN)
    chain.insert(chain.index("usgs"), "bngs2")
    chain.insert(0, "bngaer")
    lat0, lon0 = float(lat.mean()), float(lon.mean())
    for name in chain:
        source = imagery.SOURCES[name]
        if source.bounds:
            south, west, north, east = source.bounds
            if not (south <= lat0 <= north and west <= lon0 <= east):
                continue
        max_zoom, _ = imagery.zoom_caps(source, lat0)
        for zoom in range(max_zoom, max(8, source.min_zoom) - 1, -1):
            rgb = _read_pixels(root, name, zoom, lat, lon)
            if np.count_nonzero(np.isfinite(rgb[:, 0])) >= len(rgb) / 2:
                return rgb
    return None


def sampler(frame, elev, cache_dir):
    """Sample (left RGB, right RGB), in sRGB 0..1, or None at an uncached mouth.

    Point and directions are in the tile frame; elev reads vector lat/lon.
    Only the two banks around the mouth are read. The road's photo is excluded.
    """
    root = Path(cache_dir) / "imagery" if cache_dir is not None else None

    def sample(p, left, outward, half_width):
        if root is None or elev is None:
            return None
        p, left, outward = (np.asarray(v, float)[:2] for v in (p, left, outward))
        banks, depth = np.meshgrid(half_width + BANK_OFFSETS, INWARD_OFFSETS, indexing="ij")
        across = np.r_[banks.ravel(), -banks.ravel()]
        probes = p + across[:, None] * left - np.tile(depth.ravel(), 2)[:, None] * outward
        lat, lon, _ = frame.enu_to_geodetic(*probes.T, 0.)
        rgb = _cached_colours(root, np.asarray(lat), np.asarray(lon))
        if rgb is None:
            revisit.expires(time.time() + PHOTO_RETRY_S)
            return None
        offsets = SLOPE_PROBE_M * np.array([[1., 0.], [-1., 0.], [0., 1.], [0., -1.]])
        query = (probes + offsets[:, None]).reshape(-1, 2)
        lat1, lon1, _ = frame.enu_to_geodetic(*query.T, 0.)
        try:
            z = np.asarray(elev(lat1, lon1), float).reshape(4, len(probes))
        except (OSError, ValueError):
            return None
        slope = np.hypot(z[0] - z[1], z[2] - z[3]) / (2 * SLOPE_PROBE_M)
        strength = rockface.steepness(slope)
        grey = rgb @ LUMA
        rock = ((1. - rockface.DESAT) * rgb + rockface.DESAT * grey[:, None]) * rockface.ROCK_TINT * rockface.DARKEN
        colour = rgb + (rock - rgb) * strength[:, None]
        # Drop flat ground, deep imagery shadow and green vegetation.
        green = rgb[:, 1] - np.maximum(rgb[:, 0], rgb[:, 2])
        good = (strength > .18) & (grey > .07) & (green < .10)
        left_bank, right_bank = (_bank_colour(colour[side], good[side])
                                 for side in (across > 0, across < 0))
        if left_bank is None and right_bank is None:
            return None
        return (left_bank if left_bank is not None else right_bank,
                right_bank if right_bank is not None else left_bank)

    return sample


def _bank_colour(colours, good):
    return tuple(np.clip(np.median(colours[good], axis=0), 0., 1.)) if np.count_nonzero(good) >= 3 else None
