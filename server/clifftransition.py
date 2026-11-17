"""Native contour fitting: bury cliff boundaries and match the terrain slope."""
import numpy as np
import fast


def prepare(surface, heights, keep, dx, dy, scale, sink):
    fitted, weights, contour = fast._ef.cliff_transition_surface(
        np.ascontiguousarray(surface.reshape(-1,3),float), np.ascontiguousarray(heights,float),
        np.ascontiguousarray(keep,bool), float(dx), float(dy), float(scale), float(sink))
    return fitted.reshape(surface.shape), weights, contour


def clip(points, triangles, attributes, contour, ground, boundary=None):
    fast.require(310, 'cliff_transition_clip')
    return fast._ef.cliff_transition_clip(np.ascontiguousarray(points,float),
        np.ascontiguousarray(triangles,np.int32), np.ascontiguousarray(attributes,float),
        np.ascontiguousarray(contour,float), np.ascontiguousarray(ground,float),
        None if boundary is None else np.ascontiguousarray(boundary,float))


def normals(surface_normals, heights, weights, dx, dy):
    fast.require(310, 'cliff_transition_normals')
    return fast._ef.cliff_transition_normals(np.ascontiguousarray(surface_normals.reshape(-1,3),float),
        np.ascontiguousarray(heights,float), np.ascontiguousarray(weights,float), float(dx), float(dy))


def fill_gaps(keep, heights, classes, blocked, step, city_buffer):
    fast.require(310, 'cliff_transition_fill_gaps')
    return fast._ef.cliff_transition_fill_gaps(np.ascontiguousarray(keep,bool),
        np.ascontiguousarray(heights,float), np.ascontiguousarray(classes,np.int32),
        np.ascontiguousarray(blocked,bool), float(step[0]), float(step[1]), float(city_buffer))
