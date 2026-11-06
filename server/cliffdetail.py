"""Resolve scanned relief along tall canyon faces before building render LODs.

A fine horizontal DEM grid can still have edges tens of metres high. Sampling
the rock height only at those endpoints stretches one value down an entire
wall. Conforming edge splits add samples in surface space without more terrain
queries, and preserve the texture chart and shared triangle edges.
"""
import numpy as np

MIN_FACE_AREA_RATIO = .2
FOLD_REFINE_STEPS = 12
import cliffmaterial as cm

VERTICAL_STEP_M = 2.0
MAX_SPLITS = 8
MAX_ADDED_VERTICES = 200000
MAX_RESIDUAL_M = .75
MAX_FULL_RELIEF_M = 1.5
# a,b,c, midpoint(ab), midpoint(bc), midpoint(ca), preserving winding.
SPLITS = (
    ((0,1,2),),
    ((0,3,2),(3,1,2)),
    ((1,4,0),(4,2,0)),
    ((1,4,3),(0,3,2),(3,4,2)),
    ((2,5,1),(5,0,1)),
    ((0,3,5),(3,1,2),(3,2,5)),
    ((2,5,4),(0,1,5),(1,4,5)),
    ((0,3,5),(3,1,4),(5,4,2),(3,4,5)),
)


def refine_canyon(rock, *, full_relief=False):
    """Sample wall relief after refinement; preserve rims and other families.

    full_relief consumes an undisplaced canyon backing. The residual mode also
    supports previously displaced input without applying its height twice.
    """
    if rock.tri_materials is None or not np.any(rock.tri_materials == cm.CANYON):
        return rock
    import cdae
    p, t = np.asarray(rock.positions,float), np.asarray(rock.tris)
    uv, colors = np.asarray(rock.uvs,float), np.asarray(rock.colors,float)
    normals = np.asarray(rock.normals,float).copy()
    uv2 = None if rock.uvs2 is None else np.asarray(rock.uvs2,float)
    strip_colors = None if rock.strip_colors is None else np.asarray(rock.strip_colors, float)
    material = rock.tri_materials
    # Fade the correction to all existing chart/tile/material boundaries.
    e = np.sort(np.stack((t[:,[0,1]],t[:,[1,2]],t[:,[2,0]]),axis=1).reshape(-1,2),axis=1)
    edges, counts = np.unique(e,axis=0,return_counts=True)
    weight = np.ones((len(p),1))
    weight[np.unique(edges[counts != 2])] = 0
    weight[np.unique(t[material != cm.CANYON])] = 0
    if rock.terrain_blend:
        import clifftransition
        weight = clifftransition.relief_weights(weight, uv2)
    coarse_height = cm.geometry_sample(uv[:,0],uv[:,1],family=cm.CANYON)[:,None]
    start = len(p)
    for _ in range(MAX_SPLITS):
        e = np.sort(np.stack((t[:,[0,1]],t[:,[1,2]],t[:,[2,0]]),axis=1).reshape(-1,2),axis=1)
        edges, inverse = np.unique(e,axis=0,return_inverse=True)
        edge_ids = inverse.reshape(-1,3)
        canyon_edge = np.zeros(len(edges),bool)
        canyon_edge[edge_ids[material == cm.CANYON]] = True
        split = canyon_edge & (np.abs(p[edges[:,0],2]-p[edges[:,1],2]) > VERTICAL_STEP_M)
        ids = np.flatnonzero(split)
        if not len(ids) or len(p)+len(ids) > start+MAX_ADDED_VERTICES:
            break
        mid = np.full(len(edges),-1,np.int32)
        mid[ids] = np.arange(len(p),len(p)+len(ids))
        pair = edges[ids]
        p = np.concatenate((p,p[pair].mean(1)))
        uv = np.concatenate((uv,uv[pair].mean(1)))
        if uv2 is not None:
            uv2 = np.concatenate((uv2,uv2[pair].mean(1)))
        colors = np.concatenate((colors,colors[pair].mean(1)))
        if strip_colors is not None:
            strip_colors = np.concatenate((strip_colors,strip_colors[pair].mean(1)))
        normals = np.concatenate((normals,normals[pair].mean(1)))
        weight = np.concatenate((weight,weight[pair].mean(1)))
        coarse_height = np.concatenate((coarse_height,coarse_height[pair].mean(1)))
        mask = split[edge_ids] @ np.array((1,2,4))
        nodes = np.column_stack((t,mid[edge_ids]))
        tris, materials = [], []
        for code, pattern in enumerate(SPLITS):
            chosen = mask == code
            tris.append(nodes[chosen][:,np.asarray(pattern)].reshape(-1,3))
            materials.append(np.repeat(material[chosen],len(pattern)))
        t, material = np.concatenate(tris), np.concatenate(materials)
    if len(p) == start and not full_relief:
        return rock
    normals /= np.maximum(np.linalg.norm(normals,axis=1,keepdims=True),1e-12)
    # Production canyon backing has no endpoint relief. Legacy displaced
    # input can instead request only the missing variation between samples.
    sampled = cm.geometry_sample(uv[:,0],uv[:,1],family=cm.CANYON)
    residual = sampled if full_relief else sampled-coarse_height[:,0]
    residual = np.clip(residual,-MAX_RESIDUAL_M,MAX_FULL_RELIEF_M if full_relief else MAX_RESIDUAL_M)*weight[:,0]
    candidate = p+normals*residual[:,None]
    ground = rock.lod_ground
    if ground is not None:
        old_gap = p[:,2]-ground(p)
        gap = candidate[:,2]-ground(candidate)
        bad = (~np.isfinite(gap)) | (gap < np.minimum(old_gap,0))
        # Shorten the displacement, rather than lifting vertices vertically:
        # the latter turns near-vertical ledges into long texture fins.
        lo, hi = np.zeros(bad.sum()),np.ones(bad.sum())
        for _ in range(6):
            alpha = (lo+hi)*.5
            q = p[bad]+normals[bad]*(residual[bad]*alpha)[:,None]
            gap = q[:,2]-ground(q)
            safe = np.isfinite(gap) & (gap >= np.minimum(old_gap[bad],0))
            lo[safe],hi[~safe] = alpha[safe],alpha[~safe]
        candidate[bad] = p[bad]+normals[bad]*(residual[bad]*lo)[:,None]
    # Reject local orientation flips caused by neighboring normal offsets at
    # tight crests. Shorten all incident offsets together; never lift a vertex.
    base_cross = np.cross(p[t[:,1]]-p[t[:,0]],p[t[:,2]]-p[t[:,0]])
    for _ in range(FOLD_REFINE_STEPS):
        cross = np.cross(candidate[t[:,1]]-candidate[t[:,0]],candidate[t[:,2]]-candidate[t[:,0]])
        bad = np.sum(cross*base_cross,axis=1) < MIN_FACE_AREA_RATIO*np.sum(base_cross*base_cross,axis=1)
        if not bad.any():
            break
        ids = np.unique(t[bad])
        candidate[ids] = (candidate[ids]+p[ids])*.5
    shaded = cdae.face_normal_sum(candidate,t)
    boundary = weight[:,0] == 0
    shaded[boundary] = normals[boundary]
    rock.positions = candidate.astype(np.float32)
    rock.tris = t.astype(np.int32)
    rock.uvs = uv.astype(np.float32)
    rock.uvs2 = None if uv2 is None else uv2.astype(np.float32)
    rock.colors = np.rint(colors).astype(np.uint8)
    rock.strip_colors = None if strip_colors is None else np.rint(strip_colors).astype(np.uint8)
    rock.normals = shaded.astype(np.float32)
    rock.tri_materials = material
    return rock
