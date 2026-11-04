"""Thin native contour/material bridge for shared opaque cliff-to-terrain rims."""
import json
from pathlib import Path
import numpy as np
import fast

MATERIALS = ('bne_cliff_strata_terrain', 'bne_cliff_roadcut_terrain',
             'bne_cliff_roadcut_arid_terrain', 'bne_cliff_canyon_sat_terrain')
MEANS = json.loads(Path(__file__).with_name('cliff_transition_means.json').read_text())


def prepare(surface, heights, keep, dx, dy, scale, sink, rgb=None, cache_dir=None):
    fast.require(146, 'cliff_transition_surface')
    fitted, weights, contour = fast._ef.cliff_transition_surface(
        np.ascontiguousarray(surface.reshape(-1,3),float), np.ascontiguousarray(heights,float),
        np.ascontiguousarray(keep,bool), float(dx), float(dy), float(scale), float(sink))
    # Only the rim samples the raw cached photograph. The interior retains its
    # shadow-rejecting mineral inference. No downloads or per-tile textures.
    target = np.zeros_like(fitted)
    if rgb is not None:
        target[:] = np.pad(rgb, ((0,1),(0,1),(0,0)),mode='edge').reshape(-1,3)
    if cache_dir is not None:
        import cliffcolors
        ids = np.flatnonzero(weights[:,0]>0)
        if len(ids):
            target[ids] = cliffcolors._samples(cache_dir, fitted[ids,1]/111320.,
                                               (fitted[ids,0]/111320.+180)%360-180)
    return fitted.reshape(surface.shape), weights, target, contour


def attributes(colors, weights, target, family):
    fast.require(146, 'cliff_transition_attributes')
    return fast._ef.cliff_transition_attributes(np.ascontiguousarray(colors.reshape(-1,4),float),
        np.ascontiguousarray(target,float), np.ascontiguousarray(weights,float), MEANS[family])


def clip(points, triangles, attributes, contour, ground):
    fast.require(146, 'cliff_transition_clip')
    return fast._ef.cliff_transition_clip(np.ascontiguousarray(points,float),
        np.ascontiguousarray(triangles,np.int32), np.ascontiguousarray(attributes,float),
        np.ascontiguousarray(contour,float), np.ascontiguousarray(ground,float))


def relief_weights(weights, uvs):
    fast.require(146, 'cliff_transition_relief_weights')
    return fast._ef.cliff_transition_relief_weights(np.ascontiguousarray(weights,float),
                                                  np.ascontiguousarray(uvs,float))
