"""Bus station render and collision meshes, using the road tile's frame and ground callbacks.

The native kernels generate platforms, roofs, fixtures, paint and collision boxes.
Railway meshes share the face, wall, column and triangle-clipping helpers.
"""
import numpy as np
import fast

import busbays
import busstations as bs

_native = fast.native(545, "bus station mesh")

PLATFORM_H_M = 0.18         # a platform's height over the yard (a 15-18 cm kerb)
COLUMN_M = 0.28             # a column's side
REPEAT_M = 3.0              # m per texture repeat of the concrete and steel

PLATFORM_TOP = ("bne_bridge_concrete", REPEAT_M)
KERB = ("bne_bridge_concrete", REPEAT_M)
STEEL = ("bne_truss_steel", REPEAT_M)
LIGHT = ("bne_tunnel_light", 1.0)   # the emissive material of the tunnel lamps (glows at night): the roof fixtures


class _Mesh:
    def __init__(self):
        self.mats, self.P, self.UV, self.T, self.M, self.n = [], [], [], [], [], 0

    def add(self, mat, P, UV, T):
        return _native.bus_add_chunk(self, mat, np.asarray(P, np.float64).reshape(-1, 3),
                                     np.asarray(UV, np.float64).reshape(-1, 2), np.asarray(T, np.int64).reshape(-1, 3))

    def result(self, box, origin, overlap=False):
        if self.P:
            return _native.bus_mesh_result(np.concatenate(self.P), np.concatenate(self.T), np.concatenate(self.UV),
                                           self.mats, np.concatenate(self.M), tuple(box), origin, bool(overlap))
        return None


def _ccw(xy):
    xy = bs._open(xy)
    return xy if _signed(xy) >= 0 else xy[::-1]


def _signed(xy):
    return _native.bus_signed(xy)


class DrawnGround:
    """The ground as the road surfaces lie on it (roadmesh.drawn_ground: the terrain as it is drawn, not the physics
    block's height): fn(xy (k, 2)) -> z (k,) in the frame. Paint (the bay lines and numbers) lifted 6 cm over the physics
    ground disappeared under the terrain's bumps (the user, 2026-11-06: no numbers)."""

    def __init__(self, fn):
        self.at = fn


def ground(frame, elev, xy):
    """Ground height (frame z) at xy (k, 2): elev(lat, lon) as plazas.mesh does, or a DrawnGround's."""
    xy = np.asarray(xy, np.float64).reshape(-1, 2)
    if isinstance(elev, DrawnGround):
        return np.asarray(elev.at(xy), np.float64).reshape(-1)
    la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
    h = np.asarray(elev(la, lo), np.float64)
    _, _, z = frame.geodetic_to_enu(la, lo, h)
    return np.asarray(z, np.float64)


def _triangulate(xy):
    import buildings
    return np.asarray(buildings.triangulate(xy), np.int64).reshape(-1, 3)


def top_face(mesh, mat, rep, xy, z, up=True):
    return _native.bus_top(mesh, mat, rep, bs._xy(xy), np.asarray(z, float).ravel(), bool(up))


def walls(mesh, mat, rep, xy, z0, z1):
    return _native.bus_walls(mesh, mat, rep, bs._xy(xy), np.asarray(z0, float).ravel(), np.asarray(z1, float).ravel())


def _segment_quads(path, half):
    return _native.bus_segment_quads(bs._xy(path), half)


def _platform_inputs(st):
    return [(p["kind"], bs._xy(p["xy"]), p.get("tags")) for p in st.platforms]


def _platform_rings(st):
    return _native.bus_platforms(_platform_inputs(st))


TACTILE = ("line_white", 1.0)    # the strip for the blind along a platform's kerb (EU: light, ribbed)
TACTILE_IN_M, TACTILE_W_M = 0.25, 0.40
TACTILE_UP_M = 0.006            # over the slab's top (no z-fighting)


def _sd_ring(ring, pts):
    return _native.bus_sdf(bs._xy(ring), bs._xy(pts))


def walk_field(st):
    if getattr(st, "_walk", None) is None:
        own = {p["id"] for p in st.platforms if p["kind"] == "polygon"}
        roofs = [_ccw(c["ring"]) for c in st.canopies if c["src"] not in own and len(bs._open(c["ring"])) >= 3]
        st._walk = _native.bus_field(_platform_rings(st), roofs, _paths(st), st.mouths)
    return st._walk


def walkable(st):
    x0, y0, field = walk_field(st)
    return x0, y0, _native.bus_walkable(field)


def _kerb_heights(st, pts):
    return _native.bus_kerbs(bs._xy(pts), crossings(st))


def zone(frame, elev, st, mesh, tactile=True):
    """The raised walkable ground (platforms, and the walking zone under the roofs with the drive lanes left free): marching
    squares over walk_field's distance, so the edge follows the real outline; 2 m quads inside, partial cells clipped at
    the edge, a kerb wall along every cut edge; heights from the ground at the lattice points. The cells in Rust
    (earthfast.station_zone, 494)."""
    import fast
    x0, y0, F = walk_field(st)
    lattice = _native.bus_lattice(x0, y0, F)
    if lattice is None:
        return
    zg = ground(frame, elev, lattice).reshape(F.shape)
    H = _kerb_heights(st, lattice).reshape(F.shape)
    fast.require(494, "station_zone")
    P, T, wall_P, wall_UV, wall_T, tac_P, tac_UV, tac_T = fast._ef.station_zone(
        float(x0), float(y0), np.ascontiguousarray(F, np.float64), np.ascontiguousarray(zg, np.float64),
        np.ascontiguousarray(H, np.float64), SLAB_FOOT_M, KERB[1], (TACTILE_UP_M, TACTILE_IN_M, TACTILE_W_M, TACTILE[1]))
    if len(P):
        mesh.add(PLATFORM_TOP[0], P, P[:, :2] / PLATFORM_TOP[1], T)
    if len(wall_P):
        mesh.add(KERB[0], wall_P, wall_UV, wall_T)
    if len(tac_P) and tactile:
        mesh.add(TACTILE[0], tac_P, tac_UV, tac_T)


def platforms(frame, elev, st, mesh):
    """(kept for callers; the platforms are part of zone()'s walkable cells)"""


SLAB_STEP_M = 2.0           # the slab's top is cut to this (the graded ground's 2 m lattice: a coarse top let it show through)
SLAB_FOOT_M = 0.30          # the walls reach this far below the ground at their foot


def _densify(ring, step):
    return _native.bus_densify(bs._xy(ring), float(step))


def _obb(ring):
    return _native.bus_obb(bs._xy(ring))


def roof_height(cn):
    return float(cn["height"]) if cn.get("mapped") else _native.bus_roof_height(bs._xy(cn["ring"]), None)


def _gable(ring):
    return _native.bus_gable(bs._xy(ring))


ROOF_STEP_M = 12.0          # a roof's faces and walls are cut to this, so that every road tile has its share of them
LANE_CLEAR_M = 4.8          # a roof over a drive lane or a mouth is at least this high (a bus is 3.4 m)


def _face(mesh, mat, rep, xy, zfun, up=True, step=ROOF_STEP_M):
    return _native.bus_face(mesh, mat, rep, bs._xy(xy), zfun, bool(up), float(step))


def _over_lane(st, ring):
    return _native.bus_over_lane(bs._xy(ring), _paths(st), st.mouths)


ROOF_DEM_M = 4.0            # the DEM pixel a roof's base height is read at


def roof_base(frame, dem, st, cn, ring):
    """The ground height (frame z) a roof stands on: the median of the raw DEM over its footprint, the same for every
    tile and process (the tile's own ground differs: its road grading, its height quantum, whether the yard's pads had
    arrived, so one roof's pieces stood at different heights and showed the sky between them). Memoised on the station.
    dem: an object with sample(lat, lon, zoom)."""
    import dem as demmod
    memo = st.__dict__.setdefault("_roof_base", {})
    key = (cn["src"], round(frame.lat0, 6), round(frame.lon0, 6))
    if key in memo:
        return memo[key]
    pts = _native.bus_dem_points(bs._xy(ring))
    la, lo_, _ = frame.enu_to_geodetic(pts[:, 0], pts[:, 1], np.zeros(len(pts)))
    h = np.asarray(dem.sample(la, lo_, demmod.zoom_for(float(np.mean(la)), ROOF_DEM_M)), np.float64).ravel()
    _, _, z = frame.geodetic_to_enu(la, lo_, h)
    memo[key] = float(np.median(z))
    return memo[key]


def roofs(frame, elev, st, mesh, dem=None):
    base = None if dem is None else lambda cn, ring: roof_base(frame, dem, st, cn, ring)
    return _native.bus_roofs(mesh, st.canopies, _paths(st), st.mouths, _walls(st),
                             lambda xy: ground(frame, elev, xy), base)


def _paths(st, width="half"):
    return [(bs._xy(ln["xy"]), ln[width]) for ln in st.lanes]


def _walls(st):
    return [bs._xy(it["xy"]) for it in st.buildings] + [bs._xy(r) for r in st.obstacles]


def _bay_rects(st):
    return [busbays._rect(b) for b in busbays.layout_bays(st)]


def _column_points(ring, st=None):
    return _native.bus_columns(bs._xy(ring), _paths(st) if st else [], st.mouths if st else [],
                               _walls(st) if st else [])


def _in_wall(st, ring, p):
    return _native.bus_in_wall(bs._xy(ring), _walls(st), tuple(p))


def line_chunks(stations, box):
    import parking
    segs = _native.bus_bay_lines([b for st in stations if st.confident() for b in busbays.layout_bays(st)])
    return parking._boxed_chunks(segs, box) if segs else []


def lane_chunks(stations, box):
    return [chunk for st in stations if st.confident()
            for chunk in _native.bus_lane_marks(tuple(box), [(bs._xy(l["xy"]), l["core"]) for l in st.lanes if l["oneway"]],
                                                st.mouths, _bay_rects(st))]


ZEBRA_STRIPE_M = 0.5     # a crossing's stripes (EU: 0.5 m bars, 3 m deep)


def _walk_ok(st, pts):
    return _native.bus_walk_ok(*walk_field(st), bs._xy(pts))


def _lane_frames(ln, step=1.0):
    return _native.bus_frames(bs._xy(ln["xy"]), float(step))


def crossings(st):
    if getattr(st, "_crossings", None) is None:
        st._crossings = _native.bus_crossings(*walk_field(st), _paths(st, "core"), st.mouths, _bay_rects(st))
    return st._crossings


def crossing_chunks(stations, box):
    return [chunk for st in stations if st.confident()
            for chunk in _native.bus_cross_marks(tuple(box), crossings(st))]


def edge_runs(st):
    if getattr(st, "_edge_runs", None) is None:
        st._edge_runs = _native.bus_edges(*walk_field(st), _paths(st, "core"), st.mouths, _bay_rects(st), crossings(st))
    return st._edge_runs


def edge_chunks(stations, box):
    return [chunk for st in stations if st.confident()
            for chunk in _native.bus_edge_marks(tuple(box), edge_runs(st))]


def bollard_points(st):
    if getattr(st, "_bollards", None) is None:
        st._bollards = _native.bus_bollards(*walk_field(st), _platform_inputs(st), _paths(st, "core")) if st.lanes else []
    return st._bollards


def _bollard(mesh, frame, elev, st, p, collide=False):
    xy = np.asarray(p, float).reshape(1, 2)
    z = float(ground(frame, elev, xy)[0]) + float(_kerb_heights(st, xy)[0])
    return _native.bus_post(mesh, tuple(p), z)


def zigzag_chunks(stations, box):
    return [chunk for st in stations if st.confident()
            for chunk in _native.bus_zigzags(tuple(box), busbays.layout_bays(st))]


def kiosk_spot(st):
    if "_kiosk" not in st.__dict__:
        st._kiosk = None if st.buildings or not st.platforms else _native.bus_kiosk(
            *walk_field(st), _platform_rings(st), [c["ring"] for c in st.canopies], _paths(st), st.mouths)
    return st._kiosk


def _kiosk(mesh, frame, elev, st, spot):
    return _native.bus_kiosk_mesh(mesh, spot, lambda xy: ground(frame, elev, xy), False)


def collision(frame, box, stations, elev, origin):
    m = _Mesh()
    sample = lambda xy: ground(frame, elev, xy)
    for st in stations:
        if st.confident():
            zone(frame, elev, st, m, tactile=False)
            _native.bus_collision_columns(m, [c["ring"] for c in st.canopies], _paths(st), st.mouths, _walls(st), sample)
            for p in bollard_points(st):
                _bollard(m, frame, elev, st, p)
            ks = kiosk_spot(st)
            if ks is not None:
                _native.bus_kiosk_mesh(m, ks, sample, True)
    got = m.result(box, origin)
    return got, _native.bus_boxes(got[0], origin) if got is not None else []


def mesh(frame, box, stations, elev, origin, dem=None, overlap=False):
    """The station meshes of the tile box (x0, y0, x1, y1): (V, T, UV, mats, idx) in the road mesh's form, or None.
    (The yard's paving is the ground's: busyard.py.)"""
    m = _Mesh()
    for st in stations:
        if not st.confident():
            continue
        zone(frame, elev, st, m)
        roofs(frame, elev, st, m, dem)
        for p in bollard_points(st):
            _bollard(m, frame, elev, st, p)
        ks = kiosk_spot(st)
        if ks is not None:
            _kiosk(m, frame, elev, st, ks)
    return m.result(box, origin, overlap)
