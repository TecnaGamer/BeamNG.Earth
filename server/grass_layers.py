"""Apply cached photo colours to GroundCover layers, retaining surface exclusions.

Only the four palette layers are new. They have exactly the physics of their
original grass/shrub surface. The .ter lists only materials actually in use;
there is no permanent reservation of every possible palette in every block.
"""
import os
import struct

import numpy as np
from PIL import Image

import grass
import heightmap
import staging
import surface as sf

BASES = {sf.GRASS_DRY: sf.GRASS, sf.GRASS_LIGHT: sf.GRASS,
         sf.DUSTY_LIGHT: sf.DUSTY, sf.DUSTY_LUSH: sf.DUSTY}


def read_ter(path):
    """Read generated .ter heights and global surface indices (south-first)."""
    with open(path, "rb") as stream:
        data = stream.read()
    n = struct.unpack_from("<I", data, 1)[0]
    qgrid = np.frombuffer(data, "<u2", n * n, 5).reshape(n, n)
    off = 5 + 2 * n * n
    local = np.frombuffer(data, np.uint8, n * n, off).reshape(n, n)
    count = struct.unpack_from("<I", data, off + n * n)[0]
    off += n * n + 4
    order = [name for name, _ in sf.MATERIALS]
    used = []
    for _ in range(count):
        length = data[off]
        used.append(order.index(data[off + 1:off + 1 + length].decode()))
        off += 1 + length
    lookup = np.full(256, heightmap.TER_HOLE, np.uint8)
    lookup[:count] = used
    return qgrid, lookup[local], used


def sample_colors(colors, n, eligible=None):
    """Bilinear colour at terrain samples, with the photo's half-sample shift.

    Input north-first; output south-first, as the .ter. The analysis is aligned
    on the same world lattice as the photo, independent of the block's origin.
    With eligible, return only those samples in boolean-index order.
    """
    colors = np.asarray(colors, np.float32)
    h, w = colors.shape[:2]
    x = np.clip((np.arange(n) + 0.5) * w / n - 0.5, 0, w - 1)
    y = np.clip((n - 1 - np.arange(n) + 0.5) * h / n - 0.5, 0, h - 1)
    if eligible is None:
        x, y = x[None, :], y[:, None]
    else:
        eligible = np.asarray(eligible, dtype=bool)
        if eligible.shape != (n, n):
            raise ValueError("eligible mask must match the terrain grid")
        # Dense grids are faster with broadcast rows; sparse grids need only
        # the selected samples, avoiding full-grid RGB intermediates twice.
        if np.count_nonzero(eligible) > n * n // 2:
            return sample_colors(colors, n)[eligible]
        rows, cols = np.nonzero(eligible)
        x, y = x[cols], y[rows]
    ix, iy = x.astype(int), y.astype(int)
    fx, fy = x - ix, y - iy
    top = colors[iy, ix] * (1 - fx[..., None])
    top += colors[iy, np.minimum(ix + 1, w - 1)] * fx[..., None]
    bottom = colors[np.minimum(iy + 1, h - 1), ix] * (1 - fx[..., None])
    bottom += colors[np.minimum(iy + 1, h - 1), np.minimum(ix + 1, w - 1)] * fx[..., None]
    return top * (1 - fy[..., None]) + bottom * fy[..., None]


def apply(out_dir, ter_name, masks, rgb, colors, frame, cx, cy, size):
    """Patch a generated block once per photo, returning its compact material list.

    Existing lawn/snow vetoes run first. The land-cover/slope/road/water/building
    decisions already encoded in the layer map always win over imagery colour.
    Reapplying a photo is deterministic and may change a previous palette.
    """
    sf.photo_lawns(out_dir, ter_name, masks, rgb)
    path = os.path.join(out_dir, ter_name)
    qgrid, before, old_used = read_ter(path)
    layer = before.copy()
    for variant, base in BASES.items():
        layer[before == variant] = base
    eligible = (layer == sf.GRASS) | (layer == sf.DUSTY)
    if colors is not None and eligible.any():
        n = layer.shape[0]
        xs = cx - size / 2 + np.arange(n) * (size / n)
        ys = cy - size / 2 + np.arange(n) * (size / n)
        rows, cols = np.nonzero(eligible)
        lat, lon, _ = frame.enu_to_geodetic(xs[cols], ys[rows], 0.0)
        # Neutral/shadowed imagery is uncertain. Retain the shrubland's dry
        # palette, and the existing broad regional prior for grassland.
        dry = (layer[eligible] == sf.DUSTY) | sf.dry_west(lat, lon)
        # Do colour math at the small image scale, then interpolate weights.
        # The contextual prior is blended after sampling its confidence by
        # differencing the two extreme priors (color_weights is affine in it).
        dry_weights = sample_colors(grass.color_weights(colors, (1, 0, 0)), n, eligible)
        lush_weights = sample_colors(grass.color_weights(colors, (0, 0, 1)), n, eligible)
        weights = np.where(dry[:, None], dry_weights, lush_weights)
        palette = grass.choose(weights, lat, lon)
        original = layer[eligible]
        grass_layers = np.array((sf.GRASS_DRY, sf.GRASS_LIGHT, sf.GRASS), np.uint8)
        shrub_layers = np.array((sf.DUSTY, sf.DUSTY_LIGHT, sf.DUSTY_LUSH), np.uint8)
        layer[eligible] = np.where(original == sf.GRASS, grass_layers[palette], shrub_layers[palette])
    used = sf.used_materials(layer)
    if not np.array_equal(layer, before) or used != old_used:
        _, used = sf.write_ter(out_dir, ter_name, qgrid, layer)
        # Empty old masks as well: importMaps remains compatible with .ter swap.
        for material in set(old_used) | set(used):
            target = os.path.join(out_dir, masks[material])
            temp = staging.tmp_for(target)
            Image.fromarray(np.where(layer[::-1] == material, 255, 0).astype(np.uint8)).save(
                temp, format="PNG", compress_level=1)
            staging.replace(temp, target)
    return used
