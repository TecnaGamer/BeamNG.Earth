"""Conservative, boundary-locked LODs for generated cliff skins.

Cluster only edge-connected vertices in a spatial cell. Tile outlines, holes,
UV chart edges and material boundaries stay exact at every level. Reject folds
and nonmanifold edges instead of bridging gaps in thin/opposing cliff faces.
"""
import numpy as np

# Camera-projected shape sizes in pixels, paired with clustering cells in metres.
# The server scales only these thresholds before a fresh shape slot is loaded.
LEVELS = ((1200, 1.5), (600, 3.0), (240, 6.0))
MIN_TRIANGLES = 128
MIN_REDUCTION = .25  # a near-duplicate LOD adds load cost with negligible rendering benefit
MIN_NORMAL_COS = 0.15
# Each LOD may retreat only a small fraction of the skin clearance.
# Averages otherwise cut across convex rock and widen the intentionally sunken rim.
MAX_INWARD_ERROR_M = 0.03
MAX_SURFACE_RETREAT_M = 0.12


def edges(tris):
    return np.sort(np.concatenate((tris[:, :2], tris[:, 1:], tris[:, ::2])), axis=1)


def simplify(rock, cell_m):
    """Return one coarser mesh; all attributes follow the same vertex mapping."""
    p, t = np.asarray(rock.positions), np.asarray(rock.tris)
    n = len(p)
    material = np.zeros(len(t), np.int32) if rock.tri_materials is None else rock.tri_materials
    e, inv, counts = np.unique(edges(t), axis=0, return_inverse=True, return_counts=True)
    locked = np.zeros(n, bool)
    locked[e[counts != 2].ravel()] = True
    emin, emax = np.full(len(e), np.iinfo(np.int32).max), np.full(len(e), -1)
    np.minimum.at(emin, inv, np.tile(material, 3))
    np.maximum.at(emax, inv, np.tile(material, 3))
    locked[e[emin != emax].ravel()] = True
    cells = np.floor((p-p.min(axis=0))/cell_m).astype(np.int64)
    join = e[np.all(cells[e[:, 0]] == cells[e[:, 1]], axis=1) & ~locked[e].any(axis=1)]
    labels = np.arange(n)
    # Connected components inside each cell, never position-only welding.
    while len(join):
        old = labels.copy()
        low = np.minimum(labels[join[:, 0]], labels[join[:, 1]])
        np.minimum.at(labels, join[:, 0], low)
        np.minimum.at(labels, join[:, 1], low)
        labels = labels[labels]
        if np.array_equal(old, labels):
            break
    original_normal = np.cross(p[t[:, 1]]-p[t[:, 0]], p[t[:, 2]]-p[t[:, 0]])
    for _ in range(8):
        _, mapping = np.unique(labels, return_inverse=True)
        weights = np.bincount(mapping)
        def average(a):
            a = np.asarray(a)
            out = np.zeros((len(weights), a.shape[1]), float)
            np.add.at(out, mapping, a)
            return out/weights[:, None]
        pos = average(p)
        tri = mapping[t]
        keep = (tri[:, 0] != tri[:, 1]) & (tri[:, 1] != tri[:, 2]) & (tri[:, 0] != tri[:, 2])
        nn = np.cross(pos[tri[:, 1]]-pos[tri[:, 0]], pos[tri[:, 2]]-pos[tri[:, 0]])
        lengths = np.linalg.norm(nn, axis=1)*np.linalg.norm(original_normal, axis=1)
        ground = getattr(rock, 'lod_ground', None)
        bad = np.zeros(len(t), bool)
        if ground is not None:
            # Check faces, not just vertices: a chord between clear endpoints
            # can cross a terrain ridge. Compare the same source locations at
            # sunken edges so simplification cannot widen their existing blend.
            changed = keep & np.any(weights[tri] > 1, axis=1)
            ids = np.flatnonzero(changed)
            new, source = pos[tri[ids]], p[t[ids]]
            for bary in ((1,0,0),(0,1,0),(0,0,1),(.5,.5,0),
                         (0,.5,.5),(.5,0,.5),(1/3,1/3,1/3)):
                q = np.einsum('ijk,j->ik', new, bary)
                old = np.einsum('ijk,j->ik', source, bary)
                clearance = q[:,2]-ground(q)
                previous = old[:,2]-ground(old)
                bad[ids] |= (~np.isfinite(clearance) |
                             (clearance < np.minimum(previous, 0)-MAX_INWARD_ERROR_M))
        else:
            # Standalone meshes without terrain retain a conservative geometric
            # safeguard; production near skins use their actual terrain envelope.
            unit = nn / np.maximum(np.linalg.norm(nn, axis=1, keepdims=True), 1e-12)
            retreat = np.max(np.sum((p[t]-pos[tri[:, :1]])*unit[:, None, :], axis=2), axis=1)
            bad = keep & (retreat > MAX_SURFACE_RETREAT_M)
            if rock.normals is not None:
                retreat = np.sum((p-pos[mapping])*rock.normals, axis=1)
                bad |= (retreat > MAX_SURFACE_RETREAT_M)[t].any(axis=1)
        bad |= keep & ((np.sum(nn*original_normal, axis=1) <= MIN_NORMAL_COS*lengths) | (lengths < 1e-12))
        # A collapse must not create duplicate faces or more than two faces per edge.
        _, ti, tc = np.unique(np.sort(tri[keep], axis=1), axis=0, return_inverse=True, return_counts=True)
        bad[np.flatnonzero(keep)[tc[ti] > 1]] = True
        ue, ec = np.unique(edges(tri[keep]), axis=0, return_counts=True)
        bad_vertices = np.unique(ue[ec > 2])
        if len(bad_vertices):
            bad |= keep & np.isin(tri, bad_vertices).any(axis=1)
        if not bad.any():
            used, remap = np.unique(tri[keep], return_inverse=True)
            normal = None if rock.normals is None else average(rock.normals)[used]
            if normal is not None:
                normal /= np.maximum(np.linalg.norm(normal, axis=1, keepdims=True), 1e-12)
            return dict(positions=pos[used], tris=remap.reshape(-1, 3),
                        uvs=average(rock.uvs)[used], colors=np.rint(average(rock.colors)[used]).astype(np.uint8),
                        normals=normal, tri_materials=material[keep],
                        uvs2=None if getattr(rock,'uvs2',None) is None else average(rock.uvs2)[used],
                        tangent_uvs=average(rock.uvs if rock.terrain_blend else rock.uvs2)[used] if rock.uvs2 is not None else None)
        restore = np.isin(mapping, np.unique(tri[bad]))
        labels[restore] = np.arange(n)[restore]
    # Pathological input remains intact rather than producing an unsafe coarse mesh.
    return dict(positions=p, tris=t, uvs=rock.uvs, colors=rock.colors,
                normals=rock.normals, tri_materials=material,
                uvs2=getattr(rock,'uvs2',None), tangent_uvs=rock.uvs if rock.terrain_blend else getattr(rock,'uvs2',None))


def levels(rock):
    """Keep only levels that reduce triangle count; tiny faces need no LOD chain."""
    result, previous = [], len(rock.tris)
    if previous < MIN_TRIANGLES:
        return result
    for pixels, cell in LEVELS:
        coarse = simplify(rock, cell)
        count = len(coarse['tris'])
        if count < previous * (1-MIN_REDUCTION) and count:
            result.append((pixels, coarse))
            previous = count
    return result


SCALES = {"low": .55, "medium": .75, "normal": 1.0, "high": 1.5, "extreme": 2.0}


def place(src, dst, level):
    """Publish a private, quality-adjusted copy to an unheld slot, atomically.

    Never patch the cache or a hard link in place: other connections and loaded
    shapes may still use it. Geometry, collision and final cull size stay intact.
    """
    import cdae
    import staging
    from pathlib import Path
    data = cdae.scale_lod_thresholds(Path(src).read_bytes(), SCALES.get(level, 1.0))
    tmp = staging.tmp_for(dst)
    try:
        Path(tmp).write_bytes(data)
        staging.replace(tmp, dst)
    finally:
        Path(tmp).unlink(missing_ok=True)
