"""Reuse a base-photo render, including a small halo for seamless grass colours.

One render per block, never per plant. The ground keeps its original footprint
and texel spacing; only the analysis sees the halo. No finished game textures or
proprietary assets are stored here.
"""
import math

import numpy as np
from PIL import Image

import grass
import imagery as imagerymod


def render_base(imagery, frame, x0, y0, size, px, pending):
    # Both the warp and its coarser lazy-source colour reference must divide
    # into complete native mesh cells. Keep the original metres per texel.
    step = size / px
    quantum = imagerymod.MESH_CELLS * imagerymod.MATCH_DOWN // 2
    halo = max(quantum, int(math.ceil(grass.SMOOTH_M / step / quantum)) * quantum)
    border = halo * step
    rgb, used = imagery.render(frame, x0 - border, y0 - border,
                               size + 2 * border, px + 2 * halo, pending)
    rgb = np.asarray(rgb, np.uint8)
    # Average before smoothing: bounded analysis memory, independent of the
    # 2048/4096px output setting. Coarse pixel spacing stays aligned across the
    # block lattice (128m moves, 1024/2048m footprints).
    down = max(1, px // 256)
    small = np.asarray(Image.fromarray(rgb).resize(
        ((px + 2 * halo) // down,) * 2, Image.Resampling.BOX))
    radius = max(1, int(math.ceil(grass.SMOOTH_M / (step * down))))
    colors = grass.smooth_colors(small, radius)
    h, n = halo // down, px // down
    colors = colors[h:h + n, h:h + n].copy()
    # Source counts are credits/diagnostics, not a classification mask.
    share = (px / (px + 2 * halo)) ** 2
    used = {name: int(round(count * share)) for name, count in used.items()}
    return rgb[halo:halo + px, halo:halo + px].copy(), used, colors
