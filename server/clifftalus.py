"""Forest-instance rubble restricted to the edges of sustained natural cliffs."""
import math

import numpy as np

MIN_SLOPE_DEG = 40.0
FULL_SLOPE_DEG = 52.0
MAX_SLOPE_DEG = 78.0
REACH_M = 12.0
SPACING_M = 2.0
MAX_DENSITY = 1.0
MAX_TILT_DEG = 65.0
TINT_LIGHT_STEP = .04
TINT_WARMTH_STEP = .04


def probability(slope, distance):
    """No talus below 40 degrees, on near-vertical walls, or away from a cliff."""
    t = np.clip((slope-MIN_SLOPE_DEG)/(FULL_SLOPE_DEG-MIN_SLOPE_DEG), 0, 1)
    near = np.clip(1-np.asarray(distance)/REACH_M, 0, 1)
    return MAX_DENSITY*t*t*(3-2*t)*near*(np.asarray(slope) < MAX_SLOPE_DEG)


def mineral_tints(biomes, rgb=None):
    """Bounded mineral warmth, never a direct RGB aerial colour multiplier.

    Quantize brightness and warm/cool axes rather than each RGB channel: RGB
    buckets create green/pink jumps. All channels share the same mineral axis.
    """
    import cliffrocks as cr
    raw = cr.rock_colors(biomes,rgb).astype(float)/255
    light = np.clip(raw @ np.array((.2126,.7152,.0722)),.64,.84)
    light = np.rint(light/TINT_LIGHT_STEP)*TINT_LIGHT_STEP
    # Account for the warm scan substrate as well as the regional multiplier.
    # Use only broad exposed-ground red/blue balance; green and aerial exposure
    # never become independent material channels.
    warmth = .035+(raw[...,0]-raw[...,2])*.5
    if rgb is not None:
        rgb = np.asarray(rgb,float)
        light_rgb = rgb @ np.array((.2126,.7152,.0722))
        r,g,b = np.moveaxis(rgb,-1,0)
        valid = (light_rgb>55) & (g<=r*1.04) & (b<=r*1.08) & (g>np.minimum(r,b)*.88)
        regional = .035+.5*(r-b)/np.maximum(light_rgb,1)
        warmth = np.where(valid,regional,warmth)
    warmth = np.clip(warmth,-.04,.18)
    warmth = np.rint(warmth/TINT_WARMTH_STEP)*TINT_WARMTH_STEP
    return np.rint(np.stack((light*(1+warmth),light,light*(1-warmth)),axis=-1)*255).astype(int)


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


def rows(frame, box, xs, ys, keep, classes, blocked, biomes, elev, color_at=None, skin_at=None):
    """17-field Forest rows with a tilted orthonormal basis and regional tint.

    The same geographic lattice owns every candidate across tile boundaries.
    The qualifying cliff mask is mandatory: a steep isolated anomaly never seeds
    rubble. City/road/water exclusions apply to its whole coarse footprint.
    """
    import cliffrocks as cr
    from vegetation import _hash01
    scale = math.cos(math.radians(frame.lat0))
    # Physical distance, including unequal longitude/latitude cell dimensions.
    # A square dilation alone extends the band too far diagonally or at latitude.
    distance = np.full(keep.shape, np.inf)
    rx, ry = math.ceil(REACH_M/(cr.GRID_M*scale)), math.ceil(REACH_M/cr.GRID_M)
    for dj in range(-ry,ry+1):
        for di in range(-rx,rx+1):
            d = math.hypot(di*cr.GRID_M*scale,dj*cr.GRID_M)
            if d >= REACH_M: continue
            j0,j1 = max(0,dj),min(keep.shape[0],keep.shape[0]+dj)
            i0,i1 = max(0,di),min(keep.shape[1],keep.shape[1]+di)
            target = distance[j0:j1,i0:i1]
            source = keep[j0-dj:j1-dj,i0-di:i1-di]
            np.minimum(target,np.where(source,d,np.inf),out=target)
    # Only the rim on the inside; the main cliff remains a continuous face.
    inside = np.zeros_like(keep)
    inside[1:-1,1:-1] = (keep[1:-1,1:-1] & keep[:-2,1:-1] & keep[2:,1:-1]
                        & keep[1:-1,:-2] & keep[1:-1,2:])
    safe = np.isin(classes, (10,20,30,60,100))
    safe &= ~cr._grow(classes == 50, math.ceil(cr.CITY_BUFFER_M/(cr.GRID_M*scale)))
    allowed = (safe[:-1,:-1] & safe[1:,:-1] & safe[:-1,1:] & safe[1:,1:]
               & ~blocked & ~inside & (distance < REACH_M))
    j, i = np.nonzero(allowed)
    if not len(i):
        return []
    # Four independent 2 m candidates per detection cell, jittered away from edges.
    i, j = np.repeat(i,4), np.repeat(j,4)
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
    selected = owned & np.isfinite(z+steep) & (_hash01(gi,gj,33) < probability(slope,distance[j,i]))
    k = np.flatnonzero(selected)
    if not len(k):
        return []
    rgb = None if color_at is None else color_at(la[k],lo[k])
    tint = mineral_tints(biomes[j[k],i[k]],rgb)
    height = 1.0 + .7*_hash01(gi[k],gj[k],34)
    yaw = _hash01(gi[k],gj[k],35)*2*np.pi
    rank = _hash01(gi[k],gj[k],36)
    # Stones rest at a shallower attitude than the face, with random heading.
    tilt = np.minimum(np.radians(slope[k])-math.radians(6), math.radians(MAX_TILT_DEG))
    up = np.column_stack((-gx[k]/steep[k]*np.sin(tilt),-gy[k]/steep[k]*np.sin(tilt),np.cos(tilt)))
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
        positions = np.column_stack((e[k],n[k],z[k]))
    positions -= normal*(height*.12)[:,None]
    out = []
    for at in range(len(k)):
        group = 'talus_' + ''.join(f'{v:02x}' for v in tint[at])
        basis = np.concatenate((tangent[at],bitangent[at],up[at]))
        out.append([group,*np.round(positions[at],3).tolist(),round(float(np.degrees(yaw[at])),2),
                    1.0,round(float(height[at]),3),round(float(rank[at]),5),*np.round(basis,6).tolist()])
    return out
