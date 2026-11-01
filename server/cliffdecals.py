"""D0206 transition patches along qualified cliff boundaries, never general slopes."""
import math

import numpy as np

SPACING_M = 3.0
MIN_SIZE_M = 7.0
MAX_SIZE_M = 9.0
MIN_SLOPE_DEG = 40.0


def rows(frame, box, xs, ys, keep, classes, blocked, biomes, elev, color_at=None):
    """Emit [tint, x,y,z, nx,ny,nz, rotation, size] in the tile's ENU frame.

    Trace shared edges of the qualification mask. Do not outline tile borders:
    the mask includes a halo and ownership is assigned only after sampling.
    Spacing is measured on the sloping surface, not its horizontal footprint.
    """
    import cliffrocks as cr
    from clifftalus import mineral_tints
    from vegetation import _hash01
    scale = math.cos(math.radians(frame.lat0))
    safe = np.isin(classes, (10, 20, 30, 60, 100))
    safe &= ~cr._grow(classes == 50, math.ceil(cr.CITY_BUFFER_M/(cr.GRID_M*scale)))
    unsafe = blocked | ~(safe[:-1, :-1] & safe[1:, :-1] & safe[:-1, 1:] & safe[1:, 1:])
    # Exclude the entire brush footprint near roads, buildings and water.
    unsafe = cr._grow(unsafe, math.ceil(MAX_SIZE_M/2/(cr.GRID_M*scale)))
    segments = []
    for axis in (0, 1):
        diff = keep[:-1, :] != keep[1:, :] if axis == 0 else keep[:, :-1] != keep[:, 1:]
        for j, i in zip(*np.nonzero(diff)):
            jj, ii = (j if keep[j, i] else j+1, i) if axis == 0 else (j, i if keep[j, i] else i+1)
            if unsafe[jj, ii]:
                continue
            if axis == 0:
                segments.append((xs[i], ys[j+1], xs[i+1], ys[j+1], biomes[jj, ii]))
            else:
                segments.append((xs[i+1], ys[j], xs[i+1], ys[j+1], biomes[jj, ii]))
    if not segments:
        return []
    seg = np.asarray(segments)
    height = lambda x, y: np.asarray(elev(y/cr.WORLD_M_PER_DEG, (x/cr.WORLD_M_PER_DEG+180)%360-180), float)
    z0, z1 = height(seg[:, 0], seg[:, 1]), height(seg[:, 2], seg[:, 3])
    lengths = np.sqrt(((seg[:, 2]-seg[:, 0])*scale)**2+(seg[:, 3]-seg[:, 1])**2+(z1-z0)**2)
    points = []
    for s, length in zip(seg, lengths):
        if not np.isfinite(length):
            continue
        count = max(1, math.ceil(length/SPACING_M))
        for t in (np.arange(count)+.5)/count:
            points.append((s[0]+t*(s[2]-s[0]), s[1]+t*(s[3]-s[1]), s[4]))
    if not points:
        return []
    x, y, biome = np.asarray(points).T
    z = height(x, y)
    gx = (height(x+1/scale, y)-height(x-1/scale, y))/2
    gy = (height(x, y+1)-height(x, y-1))/2
    normal = np.column_stack((-gx, -gy, np.ones(len(x))))
    normal /= np.linalg.norm(normal, axis=1)[:, None]
    lat, lon = y/cr.WORLD_M_PER_DEG, (x/cr.WORLD_M_PER_DEG+180)%360-180
    e, n, _ = frame.geodetic_to_enu(lat, lon, np.zeros(len(x)))
    owned = (e >= box[0]) & (e < box[2]) & (n >= box[1]) & (n < box[3])
    valid = owned & np.isfinite(z+gx+gy) & (np.degrees(np.arctan(np.hypot(gx, gy))) >= MIN_SLOPE_DEG)
    k = np.flatnonzero(valid)
    if not len(k):
        return []
    rgb = None if color_at is None else color_at(lat[k], lon[k])
    tints = mineral_tints(biome[k].astype(int), rgb)
    gi, gj = np.rint(x[k]*100).astype(np.int64), np.rint(y[k]*100).astype(np.int64)
    rotation = _hash01(gi, gj, 71)*2*np.pi
    sizes = MIN_SIZE_M+(MAX_SIZE_M-MIN_SIZE_M)*_hash01(gi, gj, 72)
    return [['talus_'+''.join(f'{c:02x}' for c in tint),
             *np.round([e[idx], n[idx], z[idx], *normal[idx], angle, size], 6).tolist()]
            for idx, tint, angle, size in zip(k, tints, rotation, sizes)]
