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
    uv2 = None if rock.uvs2 is None else np.asarray(rock.uvs2,float)
    strip_colors = None if rock.strip_colors is None else np.asarray(rock.strip_colors, float)
    material = rock.tri_materials
    # Fade the correction to all existing chart/tile/material boundaries.
    weight = fast._ef.cliff_detail_weights(np.ascontiguousarray(t, np.int64),
        np.ascontiguousarray(material, np.int32), len(p), cm.CANYON)
    if rock.terrain_blend:
        import clifftransition
        weight = clifftransition.relief_weights(weight, uv2)
    coarse_height = cm.geometry_sample(uv[:,0],uv[:,1],family=cm.CANYON)[:,None]
    start = len(p)
    # One interpolation pass carries every vertex channel through the same
    # conforming edge splits. Optional channels retain their original layout.
    channels = [uv, colors, normals, weight, coarse_height]
    if uv2 is not None:
        channels.append(uv2)
    if strip_colors is not None:
        channels.append(strip_colors)
    p, t, attributes, material = fast._ef.cliff_detail_subdivide(
        np.ascontiguousarray(p, float), np.ascontiguousarray(t, np.int64),
        np.ascontiguousarray(np.column_stack(channels), float),
        np.ascontiguousarray(material, np.int32), cm.CANYON)
    channels = np.split(attributes, np.cumsum([a.shape[1] for a in channels])[:-1], axis=1)
    uv, colors, normals, weight, coarse_height = channels[:5]
    optional = iter(channels[5:])
    uv2 = next(optional) if uv2 is not None else None
    strip_colors = next(optional) if strip_colors is not None else None
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
    rock.uvs2 = None if uv2 is None else uv2.astype(np.float32)
    rock.colors = np.rint(colors).astype(np.uint8)
    rock.strip_colors = None if strip_colors is None else np.rint(strip_colors).astype(np.uint8)
    rock.normals = shaded.astype(np.float32)
    rock.tri_materials = material
    return rock
