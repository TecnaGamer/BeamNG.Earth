"""Horizontal bounds of low stock scenery, for keeping walking surfaces clear.

Measured from BeamNG 0.40 DAEs by tools/survey_path_footprints.py. Each pair is
(trees.lua catalog height, radius about the model origin), covering all LODs.
Only dimensions are stored, not game geometry. A plant's canopy-derived radius
is not its rendered footprint: the 1.8 m generibush reaches 3.31 m sideways.
"""
import numpy as np

CATALOG = {
    'shrub': ((1.8, 3.31), (2.2, 1.098), (2.7, 1.176), (3.1, 1.773), (3.3, 1.504), (3.9, 1.826), (4.1, 1.333)),
    'desert_shrub': ((0.7, 0.426), (0.7, 0.426), (1.0, 0.694), (1.0, 0.694), (1.2, 0.979), (2.0, 1.435),
                     (2.4, 1.141), (2.9, 2.617)),
    'creosote': ((1.15, 0.924), (1.63, 1.985), (2.43, 1.534)),
    'rock': ((0.6, 1.155), (0.7, 1.097), (0.9, 0.457), (1.5, 1.765), (1.6, 2.106), (1.9, 3.243), (1.9, 4.179)),
    'desert_rock': ((0.3, 0.469), (0.5, 1.317), (0.6, 0.716), (0.9, 0.893), (1.4, 1.21), (1.7, 1.313),
                    (1.9, 1.396), (2.1, 1.289)),
    'vine': ((2.6, 8.551),),
    'tuft': ((0.11, 1.175), (0.17, 1.681)),
    'fern': ((0.62, 0.755), (0.75, 1.565), (1.0, 1.218), (1.49, 1.763)),
    'hydrant_eu': ((0.79, 0.27),),
    'postbox_eu': ((1.6, 0.341),),
    'postbox_us': ((1.56, 0.464),),
    'bikestand': ((1.16, 2.08),),
    'recyclebin': ((1.48, 0.902),),
    'picnictable': ((0.85, 1.229),),
    'picnicgrill': ((1.48, 0.566),),
}
CATALOG['fr_hedge'] = CATALOG['fr_fence_hedge'] = CATALOG['shrub'][:1]


def radii(group, heights, fallback=0.4):
    """Conservative radius of any variant trees.lua M.pick can select at these heights.

    Use the same two size windows, nearest-size fallback and scale clamp. Taking
    the largest eligible variant also covers float32 rank rounding in the tile.
    """
    h = np.asarray(heights, np.float64)
    out = np.broadcast_to(np.asarray(fallback, np.float64), h.shape).copy()
    specs = CATALOG.get(group)
    if specs is None or not h.size:
        return out
    specs = np.asarray(specs, np.float64)
    base, radius = specs[:, 0], specs[:, 1]
    candidates = np.zeros(h.shape + (len(base),), bool)
    for low, high in ((0.8, 1.25), (0.6, 1.6)):
        candidates |= (~candidates.any(-1))[..., None] & (base >= h[..., None] * low) & (base <= h[..., None] * high)
    nearest = np.argmin(np.abs(np.log(base / np.maximum(h[..., None], .1))), axis=-1)
    candidates |= (~candidates.any(-1))[..., None] & (np.arange(len(base)) == nearest[..., None])
    sizes = radius * np.clip(h[..., None] / base, .3, 2.5)
    return np.maximum(out, np.where(candidates, sizes, 0.).max(-1))
