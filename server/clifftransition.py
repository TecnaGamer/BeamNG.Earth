"""Thin native contour/material bridge for shared opaque cliff-to-terrain rims."""
import json
from pathlib import Path
import numpy as np
import fast

MATERIALS = ('bne_cliff_strata_terrain', 'bne_cliff_roadcut_terrain',
             'bne_cliff_roadcut_arid_terrain', 'bne_cliff_canyon_sat_terrain')
MEANS = json.loads(Path(__file__).with_name('cliff_transition_means.json').read_text())


def prepare(surface, heights, keep, dx, dy, scale, sink, rgb=None, cache_dir=None, ground=None):
    fast.require(147, 'cliff_transition_surface')
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
            sample_points = fitted if ground is None else ground.reshape(-1,3)
            raw = cliffcolors._samples(cache_dir, sample_points[ids,1]/111320.,
                                        (sample_points[ids,0]/111320.+180)%360-180)
            target[ids] = raw
    return fitted.reshape(surface.shape), weights, target, contour


def attributes(colors, weights, target, family):
    fast.require(147, 'cliff_transition_attributes')
    return fast._ef.cliff_transition_attributes(np.ascontiguousarray(colors.reshape(-1,4),float),
        np.ascontiguousarray(target,float), np.ascontiguousarray(weights,float), MEANS[family])


def clip(points, triangles, attributes, contour, ground, boundary=None):
    fast.require(148, 'cliff_transition_clip')
    return fast._ef.cliff_transition_clip(np.ascontiguousarray(points,float),
        np.ascontiguousarray(triangles,np.int32), np.ascontiguousarray(attributes,float),
        np.ascontiguousarray(contour,float), np.ascontiguousarray(ground,float),
        None if boundary is None else np.ascontiguousarray(boundary,float))


def relief_weights(weights, uvs):
    fast.require(147, 'cliff_transition_relief_weights')
    return fast._ef.cliff_transition_relief_weights(np.ascontiguousarray(weights,float),
                                                  np.ascontiguousarray(uvs,float))


def normals(surface_normals, heights, weights, dx, dy):
    fast.require(148, 'cliff_transition_normals')
    return fast._ef.cliff_transition_normals(np.ascontiguousarray(surface_normals.reshape(-1,3),float),
        np.ascontiguousarray(heights,float), np.ascontiguousarray(weights,float), float(dx), float(dy))


def fill_gaps(keep, heights, classes, blocked, step, city_buffer):
    fast.require(148, 'cliff_transition_fill_gaps')
    return fast._ef.cliff_transition_fill_gaps(np.ascontiguousarray(keep,bool),
        np.ascontiguousarray(heights,float), np.ascontiguousarray(classes,np.int32),
        np.ascontiguousarray(blocked,bool), float(step[0]), float(step[1]), float(city_buffer))
