"""Conservative, boundary-locked LODs for generated cliff skins.

Cluster only edge-connected vertices in a spatial cell. Tile outlines, holes,
UV chart edges and material boundaries stay exact at every level. Reject folds
and nonmanifold edges instead of bridging gaps in thin/opposing cliff faces.
"""
import numpy as np
from runtimeprofiler import step as profile_step

# Camera-projected shape sizes in pixels, paired with clustering cells in metres.
# The server scales only these thresholds before a fresh shape slot is loaded.
LEVELS = ((1200, 1.5), (600, 3.0), (240, 6.0))
MIN_TRIANGLES = 128
MIN_REDUCTION = .25  # a near-duplicate LOD adds load cost with negligible rendering benefit


def edges(tris):
    return np.sort(np.concatenate((tris[:, :2], tris[:, 1:], tris[:, ::2])), axis=1)


def edge_groups(tris, vertex_count):
    """Native undirected edge groups shared by render and collision simplification."""
    import fast
    return fast._ef.cliff_lod_edges(np.ascontiguousarray(tris, np.int64), vertex_count)


def _prepare(rock):
    """Topology and original terrain clearance shared by the three LOD trials."""
    p, t = np.asarray(rock.positions), np.asarray(rock.tris)
    material = np.zeros(len(t), np.int32) if rock.tri_materials is None else rock.tri_materials
    import fast
    e, locked, normal = fast._ef.cliff_lod_prepare(np.ascontiguousarray(p, float),
        np.ascontiguousarray(t, np.int64), np.ascontiguousarray(material, np.int32))
    return dict(edges=e, locked=locked, material=material, clearance={}, normal=normal)



def simplify(rock, cell_m, prepared=None, fallback=None):
    """Return one coarser mesh; all attributes follow the same vertex mapping."""
    p, t = np.asarray(rock.positions), np.asarray(rock.tris)
    n = len(p)
    prepared = _prepare(rock) if prepared is None else prepared
    e, locked, material = (prepared[k] for k in ('edges', 'locked', 'material'))
    baseline = np.arange(n, dtype=np.int64) if fallback is None else fallback
    import fast
    result = fast._ef.cliff_lod_reduce(np.ascontiguousarray(p, float),
        np.ascontiguousarray(t, np.int64), e, locked, prepared['normal'],
        np.empty((0, 3)) if rock.normals is None else np.ascontiguousarray(rock.normals, float),
        np.ascontiguousarray(baseline, np.int64), cell_m,
        getattr(rock, 'lod_ground', None), prepared['clearance'])
    if result is not None:
        mapping, pos, tri, keep, labels = result
        if fallback is not None:
            prepared['accepted_labels'] = labels
        return fast._ef.cliff_lod_output(pos, tri, keep, mapping,
            np.ascontiguousarray(rock.uvs, float), np.ascontiguousarray(rock.colors, float),
            np.ascontiguousarray(material, np.int32), rock.terrain_blend,
            None if rock.normals is None else np.ascontiguousarray(rock.normals, float),
            None if rock.uvs2 is None else np.ascontiguousarray(rock.uvs2, float))
    # Pathological input remains intact rather than producing an unsafe coarse mesh.
    return dict(positions=p, tris=t, uvs=rock.uvs, colors=rock.colors,
                normals=rock.normals, tri_materials=material,
                uvs2=getattr(rock,'uvs2',None), tangent_uvs=rock.uvs if rock.terrain_blend else getattr(rock,'uvs2',None))


@profile_step('Cliff render LODs')
def levels(rock):
    """Keep only levels that reduce triangle count; tiny faces need no LOD chain."""
    result, previous = [], len(rock.tris)
    if previous < MIN_TRIANGLES:
        return result
    prepared = _prepare(rock)
    fallback = np.arange(len(rock.positions))
    for pixels, cell in LEVELS:
        coarse = simplify(rock, cell, prepared, fallback)
        # Rejected large clusters fall back to this already validated mapping.
        # Always validate against the ORIGINAL surface, not a previously reduced
        # mesh, so repeated levels cannot accumulate terrain penetration/error.
        fallback = prepared.get('accepted_labels', fallback)
        count = len(coarse['tris'])
        if count < previous * (1-MIN_REDUCTION) and count:
            result.append((pixels, coarse))
            previous = count
    return result
