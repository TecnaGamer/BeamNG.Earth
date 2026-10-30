"""Origin-independent tiles ("geotiles", .claude/work/geotiles_plan.md): glue over earthfast's geotiles.rs.

Content that does not depend on the anchor (trees, bushes, rocks first: vegetation.py; road content next) is made
once per tile of a global Web-Mercator grid at zoom Z (17: ~240 m at 39 deg, 8 x 8 of them in a map cell), in the
tile's own frame: the ENU tangent plane at its centre, in the world convention (x, y of a place at h = 0, z at its
height). Its key ("g17_x_y") never names an anchor, so a tile the mod has stays valid across rebases: the mod places it
into the current anchor's world (geotile.lua placement: the rigid move made exact at the tile's pivot, plus the shear
that carries the height difference from it) instead of the server building it again.

Planning frames (grading's plans, bridge / tunnel chain profiles) are the frames of zoom PLAN_Z regions (10: ~30 km),
the same way: one plan per region whatever the anchor.
"""
import functools

import numpy as np

import fast
import geodesy

Z = 17                  # content tiles (the mod's hello cap geoTiles = Z)
PLAN_Z = 10             # planning regions (grading)
CHAIN_Z = 12            # bridge / tunnel chain profile stores (~8 km: a deck solved in a nearly level frame)
NEED = 103              # earthfast's geotiles kernels
NEED_CONTENT = 104      # ... and the road geotiles' (geo_owns, clip_tris_convex, cdae_zstats / cdae_shear)
# Far content (phase 2): the far buildings in the road geotiles' own grid (a far tile and the road geotile of the same
# key hold the very same buildings: each building in one key, shown by one of the two), the skyline in squares of
# 4 x 4 of them (buildings.SKY_UNIT: the far buildings come as whole squares, a skyline tile shows while not all of its
# square's far tiles are here), the far bridges' jobs in those squares too, their slabs in pieces per road geotile (the
# mod hides each where its road geotile shows its own deck).
FAR_Z = Z               # far buildings (bldfar)
SKY_Z = Z - 2           # the skyline (bldsky): SKY_UNIT = 2 ** (FAR_Z - SKY_Z)
BRIDGE_Z = Z - 2        # far bridges (brfar): one job and one message a square, a piece per FAR_Z tile under it


def _ef():
    fast.require(NEED, "geotiles")
    return fast._ef


def tile_of(lat, lon, z=Z):
    """(x, y) of the zoom-z tile under each point (arrays in, int64 arrays out; scalars in, ints out)."""
    la, lo = np.atleast_1d(np.asarray(lat, np.float64)), np.atleast_1d(np.asarray(lon, np.float64))
    xs, ys = _ef().geo_tile_of(np.ascontiguousarray(la.ravel()), np.ascontiguousarray(lo.ravel()), int(z))
    if np.ndim(lat) == 0 and np.ndim(lon) == 0:
        return int(xs[0]), int(ys[0])
    return xs.reshape(la.shape), ys.reshape(la.shape)


def key(x, y, z=Z):
    """The tile's key in messages and caches: 'g<z>_<x>_<y>'."""
    return f"g{int(z)}_{int(x)}_{int(y)}"


def parse(k):
    """(z, x, y) of a key, or None."""
    try:
        zz, x, y = str(k).split("_")
        if not zz.startswith("g"):
            return None
        return int(zz[1:]), int(x), int(y)
    except (ValueError, AttributeError):
        return None


@functools.lru_cache(maxsize=65536)
def info(x, y, z=Z):
    """(centre lat, centre lon, south, west, north, east) of a tile."""
    return tuple(float(v) for v in _ef().geo_tile_info(int(z), int(x), int(y)))


def centre(x, y, z=Z):
    return info(x, y, z)[:2]


class WorldFrame(geodesy.EnuFrame):
    """An EnuFrame whose enu_to_geodetic(e, n, 0) is the exact inverse of the world convention (the place whose h = 0
    point has these e, n; geotiles.rs world_to_geodetic), not the plane point (e, n, 0), which lies d^2 / 2R over the
    ellipsoid: a place's lat / lon read back from its own x, y was d^3 / 2R^2 off (4 cm at 15 km, 25 cm at a planning
    region's corner). The frames content and plans are made in: what they sample is where their vertices are."""

    def enu_to_geodetic(self, e, n, u=0.0):
        if np.ndim(u) != 0 or float(u) != 0.0:
            return super().enu_to_geodetic(e, n, u)
        E, N = np.broadcast_arrays(np.asarray(e, np.float64), np.asarray(n, np.float64))
        shape = E.shape
        p = np.zeros((E.size, 3))
        p[:, 0], p[:, 1] = E.ravel(), N.ravel()
        la, lo, _ = _ef().geo_world_to_geodetic(float(self.lat0), float(self.lon0), p)
        if shape == ():
            return np.float64(la[0]), np.float64(lo[0]), np.float64(0.0)
        return la.reshape(shape), lo.reshape(shape), np.zeros(shape)


@functools.lru_cache(maxsize=4096)
def frame(x, y, z=Z):
    """The tile's frame: a WorldFrame at its centre (h = 0)."""
    la, lo = centre(x, y, z)
    return WorldFrame(la, lo)


def quad(anchor, x, y, z=Z):
    """The tile's corners (4, 2: SW, SE, NE, NW) in the world of anchor (an EnuFrame)."""
    return _ef().geo_tile_quad(float(anchor.lat0), float(anchor.lon0), int(z), int(x), int(y))


def quad_dist(q, x, y):
    """m from (x, y) to a quad (4, 2) (0 inside)."""
    return float(_ef().geo_quad_dist(np.ascontiguousarray(q, np.float64), float(x), float(y)))


def tiles_near(anchor, x, y, r, z=Z):
    """[(tx, ty, m)] of the tiles whose quad in anchor's world comes within r of (x, y), nearest first."""
    return _ef().geo_tiles_near(float(anchor.lat0), float(anchor.lon0), int(z), float(x), float(y), float(r))


def _pts(p):
    p = np.asarray(p, np.float64)
    if p.ndim == 1:
        p = p[None]
    return np.ascontiguousarray(p)


def world_map(src, dst, pts):
    """Points (n, 2 or 3) of src's world (EnuFrame) in dst's world, exactly (n, 3)."""
    return _ef().geo_world_map(float(src.lat0), float(src.lon0), float(dst.lat0), float(dst.lon0), _pts(pts))


def world_xy_map(src, dst, xy):
    """Ground points (n, 2) at h = 0 of src's world in dst's world (n, 2): the plans' and outlines' vertices."""
    xy = np.asarray(xy, np.float64)
    if src is dst or (src.lat0 == dst.lat0 and src.lon0 == dst.lon0):
        return xy[:, :2].copy()
    if not len(xy):
        return np.zeros((0, 2))
    p = np.zeros((len(xy), 3))
    p[:, :2] = xy[:, :2]
    out = world_map(src, dst, p)[:, :2]
    return np.ascontiguousarray(out)


def world_to_geodetic(f, pts):
    """(lat, lon, h) of points (n, 3) of f's world (the exact inverse of geodetic_to_world)."""
    return _ef().geo_world_to_geodetic(float(f.lat0), float(f.lon0), _pts(pts))


def geodetic_to_world(f, lat, lon, h=0.0):
    """(x, y, z) arrays: x, y of each place at h = 0, z at its height h."""
    x, y, _ = f.geodetic_to_enu(lat, lon, 0.0)
    _, _, z = f.geodetic_to_enu(lat, lon, h)
    return x, y, z


def placement(src, dst, pivot):
    """(R (3, 3), t (3,), c (3,)): src's world placed in dst's at pivot (src world): p' = R p + t + c (p.z - pivot.z)."""
    r, t, c = _ef().geo_placement(float(src.lat0), float(src.lon0), float(dst.lat0), float(dst.lon0),
                                  float(pivot[0]), float(pivot[1]), float(pivot[2]))
    return np.array(r, np.float64).reshape(3, 3), np.array(t, np.float64), np.array(c, np.float64)


def place(src, dst, pivot, pts):
    """pts (n, 3) of src's world by placement (the mod's way: geotile.lua M.apply)."""
    r, t, c = placement(src, dst, pivot)
    p = _pts(pts)
    return p @ r.T + t + np.outer(p[:, 2] - float(pivot[2]), c)


# --- planning regions ---------------------------------------------------------------------------------------------

def region_of(lat, lon):
    """The planning region (x, y) at PLAN_Z of a point."""
    return tile_of(lat, lon, PLAN_Z)


def region_of_cell(cell):
    """The planning region of a map cell (mapcells.CellKey, zoom >= PLAN_Z: nested, exactly)."""
    z, x, y = int(cell[0]), int(cell[1]), int(cell[2])
    sh = z - PLAN_Z
    if sh < 0:
        raise ValueError(f"geotiles: cell {cell} is coarser than the planning regions")
    return x >> sh, y >> sh


def region_frame(rx, ry):
    """The region's planning frame (EnuFrame at its centre)."""
    return frame(rx, ry, PLAN_Z)


def children(x, y, z, cz):
    """The zoom-cz tiles under tile (x, y) of zoom z (cz >= z: nested exactly), rows north to south, west to east."""
    k = 1 << (int(cz) - int(z))
    return [(int(x) * k + i, int(y) * k + j) for j in range(k) for i in range(k)]


def parent(x, y, z, pz):
    """The zoom-pz tile (pz <= z) a zoom-z tile lies under."""
    sh = int(z) - int(pz)
    return int(x) >> sh, int(y) >> sh


def tile_cell(x, y, z=Z, cell_z=14):
    """The map cell (z, x, y) a tile lies in (nested: zoom z >= cell_z)."""
    sh = int(z) - int(cell_z)
    return (int(cell_z), int(x) >> sh, int(y) >> sh)


# --- road geotiles (phase 1c): a geotile as the road builders take a tile ------------------------------------------

OWN_EDGE_M = 0.003      # m: points this far inside the quad's inner box are the tile's, this far outside its outer box
                        # not, without asking the exact test (the edges bow < 1 mm between the corners at z17)


class OwnedBox(tuple):
    """A tile's box (x0, y0, x1, y1) that knows which points are its own (owns): a geotile's, whose quad in its own
    frame is not quite the box (the meridians converge: its west / east edges lean in by ~3.5 mm at 39 deg). roads.
    box_owns asks it; a plain tuple is the anchor tiles' half-open box."""

    def __new__(cls, box, owner):
        self = super().__new__(cls, tuple(float(v) for v in box))
        self.owner = owner
        return self

    def owns(self, x, y):
        return self.owner.owns(x, y)

    @property
    def quad(self):
        return self.owner.quad


class GeoBox:
    """Geotile (gx, gy) as the road tile builders take a tile: roads.tile_bbox(tile, None) is its quad's bounding box
    in its own frame (an OwnedBox), roads.cells_of_tile its map cells; owns() tells its own points exactly (by their
    lat / lon: each place in one tile, whatever the frames). The builders' "tx" for it is this object, "ty" None."""

    def __init__(self, gx, gy, z=Z):
        self.gx, self.gy, self.z = int(gx), int(gy), int(z)
        self.key = key(gx, gy, z)
        self.frame = frame(gx, gy, z)
        q = np.asarray(quad(self.frame, gx, gy, z), np.float64)
        self.quad = q
        s, w, n, e = info(gx, gy, z)[2:]
        # (the edges' middles: a parallel bows from its chord between the corners)
        mids = []
        for la, lo in (((s, 0.5 * (w + e))), (n, 0.5 * (w + e)), (0.5 * (s + n), w), (0.5 * (s + n), e)):
            x, y, _ = self.frame.geodetic_to_enu(la, lo, 0.0)
            mids.append((float(x), float(y)))
        mids = np.asarray(mids)
        allp = np.vstack([q, mids])
        self.box = OwnedBox((allp[:, 0].min(), allp[:, 1].min(), allp[:, 0].max(), allp[:, 1].max()), self)
        self.inner = (max(q[0, 0], q[3, 0], mids[2, 0]) + OWN_EDGE_M, max(q[0, 1], q[1, 1], mids[0, 1]) + OWN_EDGE_M,
                      min(q[1, 0], q[2, 0], mids[3, 0]) - OWN_EDGE_M, min(q[2, 1], q[3, 1], mids[1, 1]) - OWN_EDGE_M)

    def __repr__(self):
        return self.key

    def owns(self, x, y):
        """Whether each point (x, y) of this frame's world (h = 0) lies in the tile: scalars in, a bool out; arrays in,
        a bool array."""
        scalar = np.ndim(x) == 0 and np.ndim(y) == 0
        X, Y = np.broadcast_arrays(np.atleast_1d(np.asarray(x, np.float64)), np.atleast_1d(np.asarray(y, np.float64)))
        X, Y = X.ravel(), Y.ravel()
        i0, j0, i1, j1 = self.inner
        b = self.box
        inside = (X >= i0) & (X <= i1) & (Y >= j0) & (Y <= j1)
        outside = (X < b[0] - OWN_EDGE_M) | (X > b[2] + OWN_EDGE_M) | (Y < b[1] - OWN_EDGE_M) | (Y > b[3] + OWN_EDGE_M)
        out = inside.copy()
        ask = ~inside & ~outside
        if ask.any():
            fast.require(NEED_CONTENT, "geo_owns")
            out[ask] = fast._ef.geo_owns(float(self.frame.lat0), float(self.frame.lon0), self.z, self.gx, self.gy,
                                         np.ascontiguousarray(X[ask]), np.ascontiguousarray(Y[ask]))
        if scalar:
            return bool(out[0])
        return out.reshape(np.broadcast(np.asarray(x), np.asarray(y)).shape)

    def side(self):
        """m: the side of a square of the tile's area (buildings.coverage)."""
        x0, y0, x1, y1 = self.box
        return float(np.sqrt((x1 - x0) * (y1 - y0)))


@functools.lru_cache(maxsize=2048)
def geobox(gx, gy, z=Z):
    return GeoBox(gx, gy, z)


def box_owns(box, x, y):
    """Whether point(s) (x, y) are the tile's own (each place in exactly one tile): a geotile's box (OwnedBox) by the
    points' lat / lon, an anchor tile's by its half-open box [x0, x1) x [y0, y1). Scalars: a bool; arrays: a bool
    array."""
    own = getattr(box, "owns", None)
    if own is not None:
        return own(x, y)
    x0, y0, x1, y1 = box[:4]
    if np.ndim(x) == 0 and np.ndim(y) == 0:
        return bool(x0 <= x < x1 and y0 <= y < y1)
    x, y = np.asarray(x, np.float64), np.asarray(y, np.float64)
    return (x >= x0) & (x < x1) & (y >= y0) & (y < y1)
