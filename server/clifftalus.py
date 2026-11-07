"""Forest-instance rubble settled below the toes of sustained natural cliffs."""
import math
from functools import lru_cache

import numpy as np

# Talus rests on benches/ground, never on the generating wall or its crest.
FULL_SLOPE_DEG = 28.0
MAX_SLOPE_DEG = 44.0
REACH_M = 22.0
SCATTER_M = 5.0
SPACING_M = 2.0
MAX_DENSITY = .65
MAX_TILT_DEG = 28.0
TINT_STEP = 16.0
# Conservative pivot-centred sphere per metre of requested instance height.
# Bounds measured in BeamNG for all six clifftalus.lua models: the widest is
# collection_02 (~2.20). This also encloses tilted clusters and off-centre pivots.
ROAD_RADIUS_PER_HEIGHT = 2.25
ROAD_SHOULDER_M = .5
ROAD_RADIUS_BIN_M = .5


def road_exclusion(frame, road_cells, urban, widths, ground_roads):
    """Road corridors for rubble, including elevated surface roads.

    The cliff detector deliberately uses ground roads only. Rubble must also
    avoid bridge/deck approaches (Hoover Dam Access Road is partly elevated).
    Conservatively exclude their XY corridor; tunnels remain available above.
    This uses already-fetched cells and adds no elevation or network queries.
    """
    import mapgeom
    import roads
    segments = {str(k): (pts[:, :2], spec.width/2)
                for k, (_, spec, pts, _) in enumerate(ground_roads)}
    for feats in road_cells:
        if feats is None or not len(feats):
            continue
        specs = roads.road_specs(feats, urban, widths)
        for i, spec in enumerate(specs):
            if spec is None:
                continue
            for ring in roads.bridge_parts(feats, i):
                e, n = mapgeom.enu_xy(feats, frame, ring)
                if len(e) >= 2:
                    segments[str(len(segments))] = (np.column_stack((e, n)), spec.width/2)
    return roads._Segs(segments)


def road_clear(positions, heights, road_segments):
    """Check final seated footprints, batching native segment tests by size.

    Rounded-up radius bins bound the number of native calls and avoid a Python
    rock-by-road loop. The half-metre shoulder also covers output quantization.
    """
    clear = np.ones(len(positions), bool)
    if road_segments is None or not len(road_segments.h):
        return clear
    radius = np.asarray(heights)*ROAD_RADIUS_PER_HEIGHT + ROAD_SHOULDER_M
    bins = np.ceil(radius/ROAD_RADIUS_BIN_M).astype(int)
    for size in np.unique(bins):
        k = np.flatnonzero(bins == size)
        clear[k] = ~road_segments.near(positions[k, :2], size*ROAD_RADIUS_BIN_M)
    return clear


def probability(slope, distance):
    """Dense at the toe, thinning onto stable ground farther downhill."""
    t = np.clip((np.asarray(slope)-FULL_SLOPE_DEG)/(MAX_SLOPE_DEG-FULL_SLOPE_DEG), 0, 1)
    near = np.clip(1-np.asarray(distance)/REACH_M, 0, 1)
    return MAX_DENSITY*(1-t*t*(3-2*t))*near**1.4


@lru_cache(maxsize=1)
def _cliff_means():
    import json
    from pathlib import Path
    return np.asarray(json.loads(Path(__file__).with_name('cliff_transition_means.json').read_text()))


def mineral_tints(biomes, rgb=None, family=0):
    """Match the cliff's final albedo, including its texture and material gain.

    The bounded RGB palette shares private Forest materials across nearby rocks.
    Sample the source cliff, not the vegetation or shadow under the fallen rock.
    """
    import cliffrocks as cr
    import cliffcolors as cc
    import cliffmaterial as cm
    means = _cliff_means()
    tint = cr.rock_colors(biomes, rgb, family)[..., :3]
    canyon = np.asarray(family) == cm.CANYON
    if np.any(canyon):
        tint = np.where(canyon[..., None], cc.canyon_tint(biomes, rgb)[..., :3], tint)
    target = cc.srgb(cc.linear(tint.astype(float)/255)*means[np.asarray(family)])*255
    return np.clip(np.rint(target/TINT_STEP)*TINT_STEP, 0, 255).astype(int)


def base_seeds(xs, ys, keep, heights):
    """Downhill exits of a sustained cliff mask; uphill rims never seed rubble."""
    h = (heights[:-1,:-1]+heights[1:,:-1]+heights[:-1,1:]+heights[1:,1:])*.25
    seeds = []
    source_j, source_i = np.nonzero(keep)
    for dj, di in ((0,1),(0,-1),(1,0),(-1,0)):
        j, i = source_j, source_i
        oj, oi = j+dj, i+di
        valid = (oj>=0)&(oj<keep.shape[0])&(oi>=0)&(oi<keep.shape[1])
        j,i,oj,oi = j[valid],i[valid],oj[valid],oi[valid]
        valid = ~keep[oj,oi] & (h[j,i] > h[oj,oi]+.25)
        j,i = j[valid],i[valid]
        if not len(i): continue
        x = (xs[i]+xs[i+1])*.5+di*(xs[1]-xs[0])*.5
        y = (ys[j]+ys[j+1])*.5+dj*(ys[1]-ys[0])*.5
        seeds.append(np.column_stack((x,y,np.full(len(i),di),np.full(len(i),dj),j,i)))
    return np.concatenate(seeds) if seeds else np.empty((0,6))


def base_cells(xs, ys, keep, seeds, scale):
    """Bounded downhill fans, with at most five metres of lateral scatter.

    Work only around detected toes, not all candidate/seed pairs. Stable sorting
    makes nearest-base ownership deterministic across independently built tiles.
    """
    if not len(seeds): return np.empty((0,3),int)
    dx,dy = xs[1]-xs[0],ys[1]-ys[0]
    si = np.floor((seeds[:,0]-xs[0])/dx).astype(int)
    sj = np.floor((seeds[:,1]-ys[0])/dy).astype(int)
    hits = []
    for dj in range(-math.ceil(REACH_M/dy), math.ceil(REACH_M/dy)+1):
        for di in range(-math.ceil(REACH_M/(dx*scale)), math.ceil(REACH_M/(dx*scale))+1):
            i,j = si+di,sj+dj
            ox = (xs[0]+(i+.5)*dx-seeds[:,0])*scale
            oy = ys[0]+(j+.5)*dy-seeds[:,1]
            along = ox*seeds[:,2]+oy*seeds[:,3]
            across = abs(ox*seeds[:,3]-oy*seeds[:,2])
            dist = np.hypot(ox,oy)
            valid = (i>=0)&(i<keep.shape[1])&(j>=0)&(j<keep.shape[0])
            # Add one cell of query padding; exact candidate checks follow.
            valid &= (along>=-max(dx*scale,dy))&(dist<REACH_M+dy)&(across<SCATTER_M+dy)
            k = np.flatnonzero(valid)
            if len(k): hits.append(np.column_stack((j[k]*keep.shape[1]+i[k], dist[k], k)))
    if not hits: return np.empty((0,3),int)
    hits = np.concatenate(hits)
    hits = hits[np.lexsort((hits[:,2],hits[:,1],hits[:,0]))]
    hits = hits[np.r_[True,np.diff(hits[:,0])!=0]].astype(int)
    return np.column_stack((hits[:,0]//keep.shape[1],hits[:,0]%keep.shape[1],hits[:,2]))


def surface_sampler(xs, ys, heights, surface):
    """Interpolate the same two triangles used by the visible cliff mesh.

    Sample its displacement, not an unfaded heightmap value: the latter leaves
    rocks floating metres away from the edge where cliff relief fades to zero.
    Outside the skin the terrain wins over the buried boundary vertices.
    """
    ground = np.stack((*np.meshgrid(xs, ys), heights), axis=-1)
    delta = surface-ground
    # A buried skirt must not pull an otherwise exposed rock through the ground.
    delta[..., 2] = np.maximum(delta[..., 2], 0)
    def sample(x, y, z):
        u = np.clip((x-xs[0])/(xs[1]-xs[0]),0,len(xs)-1-1e-6)
        v = np.clip((y-ys[0])/(ys[1]-ys[0]),0,len(ys)-1-1e-6)
        i,j = u.astype(int),v.astype(int)
        u,v = (u-i)[:,None],(v-j)[:,None]
        a,b,c,d = delta[j,i],delta[j,i+1],delta[j+1,i+1],delta[j+1,i]
        offset = np.where(u>=v,a*(1-u)+b*(u-v)+c*v,a*(1-v)+c*u+d*(v-u))
        return np.column_stack((x,y,z))+offset
    return sample


def rows(frame, box, xs, ys, keep, classes, blocked, biomes, elev, color_at=None, skin_at=None, road_segments=None):
    """17-field Forest rows with a tilted orthonormal basis and regional tint.

    The same geographic lattice owns every candidate across tile boundaries.
    The qualifying cliff mask is mandatory: a steep isolated anomaly never seeds
    rubble. City/road/water exclusions apply to its whole coarse footprint.
    """
    import cliffrocks as cr
    from vegetation import _hash01
    if not np.any(keep): return []
    scale = math.cos(math.radians(frame.lat0))
    xx,yy = np.meshgrid(xs,ys)
    heights = np.asarray(elev(yy/cr.WORLD_M_PER_DEG,(xx/cr.WORLD_M_PER_DEG+180)%360-180))
    seeds = base_seeds(xs,ys,keep,heights)
    cells = base_cells(xs,ys,keep,seeds,scale)
    if not len(cells): return []
    safe = np.isin(classes, (10,20,30,60,100))
    safe &= ~cr._grow(classes == 50, math.ceil(cr.CITY_BUFFER_M/(cr.GRID_M*scale)))
    allowed = (safe[:-1,:-1] & safe[1:,:-1] & safe[:-1,1:] & safe[1:,1:] & ~blocked)
    j,i,seed = cells.T
    source_j,source_i = seeds[seed,4:].astype(int).T
    valid = allowed[j,i] & ~blocked[source_j,source_i]
    j,i,seed = j[valid],i[valid],seed[valid]
    if not len(i): return []
    # Four independent 2 m candidates per detection cell, jittered away from edges.
    i, j = np.repeat(i,4), np.repeat(j,4)
    seed = np.repeat(seed,4)
    sub = np.tile(np.arange(4),len(i)//4)
    gi = np.rint(xs[i]/SPACING_M).astype(np.int64)+sub%2
    gj = np.rint(ys[j]/SPACING_M).astype(np.int64)+sub//2
    x = (gi+.2+.6*_hash01(gi,gj,31))*SPACING_M
    y = (gj+.2+.6*_hash01(gi,gj,32))*SPACING_M
    la, lo = y/cr.WORLD_M_PER_DEG, (x/cr.WORLD_M_PER_DEG+180)%360-180
    z = np.asarray(elev(la,lo),float)
    e,n,_ = frame.geodetic_to_enu(la,lo,np.zeros_like(z))
    owned = (e >= box[0]) & (e < box[2]) & (n >= box[1]) & (n < box[3])
    d = 1.0/cr.WORLD_M_PER_DEG
    gx = (elev(la,lo+d/scale)-elev(la,lo-d/scale))/2
    gy = (elev(la+d,lo)-elev(la-d,lo))/2
    steep = np.hypot(gx,gy)
    slope = np.degrees(np.arctan(steep))
    base = seeds[seed]
    ox,oy = (x-base[:,0])*scale,y-base[:,1]
    along = ox*base[:,2]+oy*base[:,3]
    across = abs(ox*base[:,3]-oy*base[:,2])
    distance = np.hypot(ox,oy)
    # Scatter beside the intersection, then continue downhill as a thinning fan.
    footprint = (along>=-1)&(across<=SCATTER_M)&(distance<REACH_M)
    base_z = np.asarray(elev(base[:,1]/cr.WORLD_M_PER_DEG,(base[:,0]/cr.WORLD_M_PER_DEG+180)%360-180))
    footprint &= z <= base_z+.5
    # Low-frequency deterministic clusters break up an evenly peppered apron.
    cluster = .45+.55*_hash01(gi//4,gj//4,39)
    selected = owned & footprint & np.isfinite(z+steep) & (_hash01(gi,gj,33) < probability(slope,distance)*cluster)
    k = np.flatnonzero(selected)
    if not len(k): return []
    source_j,source_i = base[k,4:].astype(int).T
    source_la = (ys[source_j]+ys[source_j+1])*.5/cr.WORLD_M_PER_DEG
    source_lo = ((xs[source_i]+xs[source_i+1])*.5/cr.WORLD_M_PER_DEG+180)%360-180
    rgb = None if color_at is None else color_at(source_la,source_lo)
    import cliffmaterial as cm
    tint = mineral_tints(biomes[source_j,source_i],rgb,cm.natural_family(source_la,source_lo))
    height = (.65+1.2*_hash01(gi[k],gj[k],34))*(1-.45*distance[k]/REACH_M)
    yaw = _hash01(gi[k],gj[k],35)*2*np.pi
    rank = _hash01(gi[k],gj[k],36)
    # Stones rest at a shallower attitude than the face, with random heading.
    tilt = np.minimum(np.radians(slope[k])*.65, math.radians(MAX_TILT_DEG))
    up = np.column_stack((-gx[k]/np.maximum(steep[k],1e-12)*np.sin(tilt),-gy[k]/np.maximum(steep[k],1e-12)*np.sin(tilt),np.cos(tilt)))
    tangent = np.column_stack((np.cos(yaw),np.sin(yaw),np.zeros(len(k))))
    tangent -= up*np.sum(tangent*up,axis=1,keepdims=True)
    tangent /= np.linalg.norm(tangent,axis=1,keepdims=True)
    bitangent = np.cross(up,tangent)
    # Use the exact faded skin for the inner rim, and original terrain outside.
    # Collection pivots are near their centre. Bury their centre a little along
    # the support normal so multiple constituent stones intersect the ground.
    normal = np.column_stack((-gx[k],-gy[k],np.ones(len(k))))/np.sqrt(1+steep[k]**2)[:,None]
    if skin_at is not None:
        skin = skin_at(x[k],y[k],z[k])
        se,sn,sz = frame.geodetic_to_enu(skin[:,1]/cr.WORLD_M_PER_DEG,
                                      (skin[:,0]/cr.WORLD_M_PER_DEG+180)%360-180,skin[:,2])
        positions = np.column_stack((se,sn,sz))
    else:
        positions = np.column_stack(frame.geodetic_to_enu(la[k],lo[k],z[k]))
    positions -= normal*(height*.12)[:,None]
    clear = road_clear(positions, np.round(height, 3), road_segments)
    out = []
    for at in np.flatnonzero(clear):
        group = 'talus_' + ''.join(f'{v:02x}' for v in tint[at])
        basis = np.concatenate((tangent[at],bitangent[at],up[at]))
        out.append([group,*np.round(positions[at],3).tolist(),round(float(np.degrees(yaw[at])),2),
                    1.0,round(float(height[at]),3),round(float(rank[at]),5),*np.round(basis,6).tolist()])
    return out
