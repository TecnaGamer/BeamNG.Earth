"""Resolve scanned relief along tall canyon faces before building render LODs.

A fine horizontal DEM grid can still have edges tens of metres high. Sampling
the rock height only at those endpoints stretches one value down an entire
wall. Conforming edge splits add samples in surface space without more terrain
queries, and preserve the texture chart and shared triangle edges.
"""
import numpy as np

import cliffmaterial as cm


def refine_canyon(rock, *, full_relief=False):
    """Sample wall relief after refinement; preserve rims and other families.

    full_relief consumes an undisplaced canyon backing. The residual mode also
    supports previously displaced input without applying its height twice.
    """
    if rock.tri_materials is None or not np.any(rock.tri_materials == cm.CANYON):
        return rock
    import fast
    p, t = np.asarray(rock.positions,float), np.asarray(rock.tris)
    uv, colors = np.asarray(rock.uvs,float), np.asarray(rock.colors,float)
    normals = np.asarray(rock.normals,float)
    material = rock.tri_materials
    # Fade the correction to all existing chart/tile/material boundaries.
    weight = fast._ef.cliff_detail_weights(np.ascontiguousarray(t, np.int64),
        np.ascontiguousarray(material, np.int32), len(p), cm.CANYON)
    if rock.relief_weight is not None:
        weight *= rock.relief_weight
    coarse_height = cm.geometry_sample(uv[:,0],uv[:,1],family=cm.CANYON)[:,None]
    start = len(p)
    # One interpolation pass carries every vertex channel through the same
    # conforming edge splits, including the geometric rim taper.
    channels = [uv, colors, normals, weight, coarse_height]
    p, t, attributes, material = fast._ef.cliff_detail_subdivide(
        np.ascontiguousarray(p, float), np.ascontiguousarray(t, np.int64),
        np.ascontiguousarray(np.column_stack(channels), float),
        np.ascontiguousarray(material, np.int32), cm.CANYON)
    channels = np.split(attributes, np.cumsum([a.shape[1] for a in channels])[:-1], axis=1)
    uv, colors, normals, weight, coarse_height = channels[:5]
    if len(p) == start and not full_relief:
        return rock
    sampled = cm.geometry_sample(uv[:,0],uv[:,1],family=cm.CANYON)
    candidate, shaded = fast._ef.cliff_detail_displace(np.ascontiguousarray(p, float),
        np.ascontiguousarray(t, np.int64), np.ascontiguousarray(normals, float),
        np.ascontiguousarray(weight[:,0], float), np.ascontiguousarray(sampled, float),
        np.ascontiguousarray(coarse_height[:,0], float), full_relief, rock.lod_ground)
    rock.positions = candidate.astype(np.float32)
    rock.tris = t.astype(np.int32)
    rock.uvs = uv.astype(np.float32)
    rock.colors = np.rint(colors).astype(np.uint8)
    rock.relief_weight = weight.astype(np.float32)
    rock.normals = shaded.astype(np.float32)
    rock.tri_materials = material
    return rock
