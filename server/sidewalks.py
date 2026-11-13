"""Raised sidewalks with curbs as one mesh per road tile (docs/ARCHITECTURE.md "Raised sidewalks").

How West Coast USA does it (2026-10-11 study of its level files, after the user found our first
curbs "invisible" and plain): no MeshRoads anywhere. Each district's sidewalks are one modelled
mesh (64 `sidewalk_<district>.dae`, 231k triangles in all) with a curb profile textured from the
`curbs` trim sheet (8 horizontal bands: plain, red / yellow / white / green painted; in each band the
light upper part is the curb top and the darker lower part the face) and slab tops (`sidewalk1`,
a 4096 x 1024 sheet of 5 sidewalk strips with panel joints along them), plus a collision mesh. One
draw call per material per district instead of three per sidewalk piece.

Here: the sidewalk runs of a road tile (roads.build_tile, after the cuts at crossings) are swept
along a fixed profile into one shape (cdae.py, two materials). The profile, across from the curb
line (offset 0) outwards, heights above the road:
    face   (0, -SINK) -> (BATTER, h - NOSE)   curbs, the band's dark part
    nose   -> (NOSE_W, h)                     curbs, light
    top    -> (CURB_W, h)                     curbs, light
    slab   -> (W, h + W * CROSS_SLOPE)        sidewalk1 (rising away from the road, as built)
    back   -> (W, -SINK)                      curbs, dark
with capped ends, or a short descent to the ground at the free ends of European sidewalks.
The mod shows it as a TSStatic that collides with its visible mesh. Pure functions; deterministic.
"""
import numpy as np
import dnp

import cdae

CURB_H = 0.15           # m above the road (US curbs: 0.15)
SINK = 0.10             # m the faces reach below the road / ground
BACK_RUN = 2.0          # the slab's back falls to the ground at 1:2, not straight down: the ground behind a sidewalk is
                        # graded at road level (grading.SIDEWALK_MARGIN_M), so a vertical back was a ~20 cm ledge that
                        # caught a wheel leaving the sidewalk for the verge ("pulling the car down", user 2026-10-14)
BATTER = 0.02           # m the face leans back over its height
NOSE = 0.025            # m: the bevelled nose, down from the top ...
NOSE_W = 0.045          # ... and in from the face
CURB_W = 0.15           # m of curb top band
CROSS_SLOPE = 0.015     # sidewalk cross slope, rising away from the road
CURB_REPEAT_M = 9.0     # m of curb per texture width (3 joints of 3 m)
SLAB_REPEAT_M = 45.0    # m of sidewalk per texture width (4096 px, a 205 px strip ~ 2.25 m)
EU_SURFACES = ("light_asphalt", "concrete", "slabs", "pavers", "pavers_mixed", "stone", "gravel")
MATERIALS = ("curbs", "sidewalk1", "tactile_indicator",  # West Coast USA (art/shapes/roads), loaded by the mod
             "bne_sidewalk_brick", "bne_eu_curb", "bne_eu_asphalt_walk", "bne_eu_tactile",
             "bne_sidewalk_sett",        # (7: a roundabout's paved apron, plan 038: the brick slab's UVs, its own look)
             "bne_island_lawn", "bne_island_shrubs", "bne_island_gravel", "bne_island_mulch",    # (8-11: its planting)
             *(f"bne_eu_walk_{name}" for name in EU_SURFACES))      # (PR #505's European pavements, after them)
SETT = 7
PLANTINGS = (8, 9, 10, 11)      # (lawn, shrub bed, gravel, bark mulch)
PLANT_REPEAT_M = (3.0, 3.0, 2.0, 3.0)    # m per repeat of each planting texture (planar UVs: no sidewalk strip)
EU_SLAB = {"asphalt": 5, "brick": 3,
           **{name: i for i, name in enumerate(EU_SURFACES, len(MATERIALS) - len(EU_SURFACES))}}
BRICK = 3
BRICK_REPEAT_M = 1.2        # m per repeat of the brick paver texture

# curbs sheet (1024 x 512, 8 bands of 64 px; Collada v up, so band 0 is v in [7/8, 1]):
# rows 0-40 of a band are the light curb top, 40-64 the darker face
V_BAND_TOP, V_LIGHT_LOW, V_BAND_LOW = 1.0 - 1 / 512, 1.0 - 40 / 512, 1.0 - 63 / 512
# sidewalk1 sheet: strip 0 (the top row) across the slab
V_SLAB_IN, V_SLAB_OUT = 1.0 - 0.195, 1.0 - 0.005


def _normals_2d(xy):
    """Unit left normals at each point of a polyline (mitred like roads.offset_polyline, clamped)."""
    d = np.diff(xy, axis=0)
    L = dnp.hypot(d[:, 0], d[:, 1])
    L[L == 0] = 1.0
    t = d / L[:, None]
    nseg = np.stack([-t[:, 1], t[:, 0]], axis=1)
    n = np.empty((len(xy), 2))
    n[0], n[-1] = nseg[0], nseg[-1]
    if len(xy) > 2:
        m = nseg[:-1] + nseg[1:]
        ml = dnp.hypot(m[:, 0], m[:, 1])
        ml[ml < 1e-9] = 1.0
        m = m / ml[:, None]
        cos = np.clip((m * nseg[1:]).sum(1), 0.5, 1.0)          # miter at most 2x
        n[1:-1] = m / cos[:, None]
    if len(xy) > 3 and float(dnp.hypot(*(xy[0] - xy[-1]))) < 1e-6:
        # a closed loop (an island's curb band): its ends are one vertex, mitred across the seam like any other, else the two
        # one-sided ends leave a wedge between them (a segment's turn wide at a wide band: the roundabout's apron, 2026-11-10)
        m = nseg[-1] + nseg[0]
        ml = float(dnp.hypot(*m))
        if ml > 1e-9:
            m = m / ml
            n[0] = n[-1] = m / max(float(dnp.dot(m, nseg[0])), 0.5)
    return n


RAMP_RUN = 1.8          # m over which a curb ramp's slab climbs back to the sidewalk (1:12)
RAMP_FLARE = 0.6        # m of curb sloping down on each side of a ramp
RAMP_H = 0.01           # m: the curb at a ramp (flush with the gutter)
TACTILE_REPEAT_M = 0.6  # m per tactile pad texture repeat
PAD_W = 0.6             # m of a ramp's slope from the curb that is the tactile pad (US: 2 ft of domes)


def profile(h=CURB_H, w=2.5, hr=None):
    """(The reference the Rust sweep follows, earthfast sidewalks.rs profile: tests pin its shape.) The cross-section points (offset from the curb line outwards, height above the road): face
    bottom, face top, nose end, curb top end, ramp end, slab outer top, back bottom (BACK_RUN out). hr: the curb's
    height (h, or lower at a curb ramp: the slab then climbs from hr at the curb top to h at
    RAMP_RUN)."""
    hr = h if hr is None else hr
    f = hr / h if h > 0 else 0.0
    # a band narrower than RAMP_RUN (a median's 0.3-0.6 m, an island's 1.2 m) ends its ramp inside it: past its outer
    # edge the profile folded back over itself, and the folded collision caught wheels ("the median eats tires",
    # GitHub #55; a truck held on an island band in Bodø, 2026-10-20)
    rr = max(CURB_W + NARROW_EDGE_M, min(RAMP_RUN, w - NARROW_EDGE_M))
    zr = h + rr * CROSS_SLOPE
    xp = CURB_W + min(PAD_W, (rr - CURB_W) / 2)
    zp = hr + (zr - hr) * (xp - CURB_W) / (rr - CURB_W)    # on the ramp's slope, PAD_W past the curb top
    top = h + w * CROSS_SLOPE
    return np.array([[0.0, -SINK], [BATTER * f, hr - NOSE * f], [NOSE_W * f, hr], [CURB_W, hr],
                     [xp, zp], [rr, zr], [w, top], [w + BACK_RUN * (top + SINK), -SINK]])


NARROW_EDGE_M = 0.05    # m a narrow band's ramp end stays inside its outer edge (profile)


# strips: (from profile point, to point, material index, v at from, v at to); material 1 on the
# ramp strip is replaced by 2 (tactile) where the curb is lowered
_SL = (RAMP_RUN - CURB_W) / (2.5 - CURB_W)
_SP = PAD_W / (2.5 - CURB_W)
V_SLAB_MID = V_SLAB_IN + _SL * (V_SLAB_OUT - V_SLAB_IN)
V_SLAB_PAD = V_SLAB_IN + _SP * (V_SLAB_OUT - V_SLAB_IN)
STRIPS = ((0, 1, 0, V_BAND_LOW, V_LIGHT_LOW), (1, 2, 0, V_LIGHT_LOW, V_BAND_TOP - 0.02),
          (2, 3, 0, V_BAND_TOP - 0.02, V_BAND_TOP), (3, 4, 1, V_SLAB_IN, V_SLAB_PAD),
          (4, 5, 1, V_SLAB_PAD, V_SLAB_MID), (5, 6, 1, V_SLAB_MID, V_SLAB_OUT), (6, 7, 0, V_LIGHT_LOW, V_BAND_LOW))
CURB_STRIPS = STRIPS[:3]


# The lean collision mesh (Builder(collision=True), written as the tile's "walkcol"): the drawn mesh's 8-point profile
# and 0.5 m ramp steps cost the game's collision rebuild ~7 ms of ~12 in Fells Point (2026-10-16: 31 tiles, 245k
# triangles). Physics gets the face, the curb top to the ramp run, the slab and the back (profile points LEAN) at the
# points that matter: a run's points within LEAN_TOL_M of the line between their neighbours kept (Douglas-Peucker,
# the curb height counted too, so ramps keep their shape).
LEAN = (0, 2, 5, 6, 7)
LEAN_STRIPS = tuple((i, i + 1, 0, 0.0, 0.0) for i in range(len(LEAN) - 1))
LEAN_TOL_M = 0.03
END_TAPER_M = 1.35


def simplify(pts, tol=LEAN_TOL_M):
    """Indices of the points (k, d) Douglas-Peucker keeps at tolerance tol (both ends always). Pure."""
    pts = np.asarray(pts, np.float64)
    k = len(pts)
    if k <= 2:
        return np.arange(k)
    keep = np.zeros(k, bool)
    keep[0] = keep[-1] = True
    stack = [(0, k - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        seg = pts[b] - pts[a]
        rel = pts[a + 1:b] - pts[a]
        L2 = float(dnp.matmul(seg, seg))
        if L2 > 0:
            t = np.clip(dnp.matmul(rel, seg) / L2, 0.0, 1.0)
            d = rel - t[:, None] * seg
        else:
            d = rel
        d2 = (d * d).sum(1)
        i = int(np.argmax(d2))
        if d2[i] > tol * tol:
            m = a + 1 + i
            keep[m] = True
            stack.append((a, m))
            stack.append((m, b))
    return np.nonzero(keep)[0]


def _soft_parts(p):
    """Keep the native sweep in the middle and sample its free ends more closely."""
    kind, curb, out, _, ramps, w, h, caps, slab, europe, mat, surface, taper = p
    if not taper or not any(caps):
        return p, []
    stations = np.r_[0., np.cumsum(dnp.hypot(*np.diff(curb[:, :2], axis=0).T))]
    length = stations[-1]
    reach = min(END_TAPER_M, length * .4)
    lo = min(reach, min((a - RAMP_FLARE for a, _ in ramps), default=reach)) if caps[0] else 0.
    hi = min(reach, min((length - b - RAMP_FLARE for _, b in ramps), default=reach)) if caps[1] else 0.
    lo = lo if lo >= .25 else 0.
    hi = hi if hi >= .25 else 0.
    if not (lo or hi):
        return p, []

    def sample(s):
        columns = np.c_[curb, out]
        a = np.column_stack([np.interp(s, stations, v) for v in columns.T])
        return a[:, :3], a[:, 3:]

    def section(a, b, steps):
        return np.unique(np.r_[np.linspace(a, b, steps), stations[(stations > a) & (stations < b)]])

    mid = section(lo, length - hi, 2)
    mid_curb, mid_out = sample(mid)
    centre = (kind, mid_curb, mid_out, None, [(a - lo, b - lo) for a, b in ramps], w, h,
              (caps[0] and not lo, caps[1] and not hi), slab, europe, mat, surface, False)
    tips = []
    for width, a, b, start in ((lo, 0., lo, True), (hi, length - hi, length, False)):
        if not width:
            continue
        s = section(a, b, 6)
        tip, direction = sample(s)
        pr = profile(h, w)
        points = tip[:, None, :] + np.concatenate(
            [direction[:, None, :] * pr[None, :, :1], np.broadcast_to(pr[None, :, 1:], (len(s), len(pr), 1))],
            axis=2)
        t = (s - a) / width if start else (b - s) / width
        weight = (t * t) * (3 - 2 * t)
        tips.append((points, weight, s - lo, direction, pr, slab))
    return centre, tips


def _tip_mesh(points, stations, direction, pr, strips, origin, slab=1):
    """The two meshes use the same end profile and heights."""
    k = len(points)
    P, T, UV, M = [], [], [], []
    i = np.arange(k - 1)
    tris = np.r_[np.c_[i, k + i + 1, i + 1], np.c_[i, k + i, k + i + 1]]
    for a, b, material, va, vb in strips:
        p = np.concatenate((points[:, a], points[:, b])) - origin
        delta = pr[b] - pr[a]
        want = np.c_[-delta[1] * direction[:-1], np.full(k - 1, delta[0])]
        tri = tris.copy()
        normal = np.cross(p[tri[:, 1]] - p[tri[:, 0]], p[tri[:, 2]] - p[tri[:, 0]])
        reverse = np.sum(normal * np.tile(want, (2, 1)), axis=1) < 0
        tri[reverse] = tri[reverse, ::-1]
        T.append(tri + len(P) * 2 * k)
        P.append(p)
        if material == 1 and slab == BRICK:
            material, repeat = BRICK, BRICK_REPEAT_M
            va, vb = pr[[a, b], 0] / repeat
        else:
            repeat = CURB_REPEAT_M if material == 0 else SLAB_REPEAT_M
        UV.append(np.c_[np.tile(stations / repeat, 2), np.repeat([va, vb], k)])
        M.append(np.full(len(tri), material, np.int64))
    return tuple(np.concatenate(a) for a in (P, T, UV, M))


class Builder:
    """Accumulates sidewalk runs and corners into one mesh (positions relative to `origin`), swept in Rust
    (earthfast.sidewalk_mesh: the profile, ramps, tactile pads, caps and corner slabs, in the old Python's arithmetic
    and order) when its arrays are asked for; collision: the lean collision mesh beside it (self.col.arrays())."""

    def __init__(self, origin, collision=False, ground=None):
        self.origin = np.asarray(origin, np.float64)
        self.ground = ground
        self.pieces = []            # (kind 0 run / 1 corner, points (k, 3), dirs (k, 2), outer (m, 3), ramps, w, h, caps,
        self._built = None          # slab, europe, mat, surface, taper); the Rust meshes once built (full, lean)
        self.discs = []             # island plantings (add_disc)
        self.col = _Lean(self) if collision else None

    def add_walk(self, walk, default_width):
        """Render a walk record from roads.build_tile in either tile pipeline (PR #505: a European walk's own surface,
        kerb height and soft ends; an island's planting disc and slab material as before)."""
        if "disc" in walk:
            d = walk["disc"]
            self.add_disc(d["ring"], d["apex"], d["mat"], d["h"], d["dome"])
            return
        from europewalks import LOW_EDGE_HEIGHT

        h = walk.get("height", walk.get("h", CURB_H))
        style = {"h": h, "europe": walk.get("europe", False), "surface": walk.get("surface")}
        if "corner" in walk:
            c = walk["corner"]
            ramps = c.get("ramps", ()) if h > LOW_EDGE_HEIGHT else ()
            self.add_corner(c["arc"], c["inward"], c["outer"], ramps=ramps, **style)
            return
        width = walk.get("w", default_width)
        curb, out = run_geometry(walk["pts"], walk["side"], width)
        slab = BRICK if walk.get("brick") and style["surface"] is None else 1
        ramps = walk.get("ramps", ()) if h > LOW_EDGE_HEIGHT else ()
        self.add_run(curb, out, width, caps=walk["caps"], ramps=ramps, slab=slab, taper=style["europe"],
                     mat=int(walk.get("mat", 0)), **style)

    def add_run(self, curb, toward_out, w, h=CURB_H, caps=(True, True), ramps=(), slab=1, europe=False, mat=0,
                surface=None, taper=False):
        """One sidewalk run: curb (k, 3) the curb line (z = the road there), toward_out (k, 2) unit
        vectors from the curb outwards (scaled by the miter), w its width; caps: end caps at the
        (start, end) (not where the run goes on in the next tile or meets a corner piece); ramps:
        curb ramps [(s0, s1)] in arc length along the curb."""
        curb = np.asarray(curb, np.float64)
        if len(curb) < 2:
            return
        self.pieces.append((0, curb[:, :3], np.asarray(toward_out, np.float64)[:, :2], None, list(ramps), float(w),
                            float(h), (bool(caps[0]), bool(caps[1])), int(slab), bool(europe), int(mat), surface,
                            bool(taper)))
        self._built = None

    def add_corner(self, arc, inward, outer, h=CURB_H, ramps=(), europe=False, surface=None):
        """A corner piece: the curb along the polyline arc (k, 3) (a curb return, with the straight
        runs and tapers of curb extensions; inward: unit vectors toward the sidewalk), the slab from
        the curb top to the outer corner points [(x, y, z) ...] (from the second sidewalk's end round
        to the first's), and the back faces along the outer edges. ramps: [(s0, s1)] arc lengths
        where the curb is lowered (the slab then slopes up from it toward the outer corner)."""
        outer = np.asarray(outer, np.float64).reshape(-1, 3)
        if europe and len(outer) == 3:
            # Native corners with exactly three outer points use a fan from the
            # middle one. At a concave corner, use its polygon triangulation instead.
            outer = np.vstack((outer[0], (outer[0] + outer[1]) / 2, outer[1:]))
        self.pieces.append((1, np.asarray(arc, np.float64)[:, :3], np.asarray(inward, np.float64)[:, :2],
                            outer, list(ramps), 2.5, float(h), (False, False),
                            1, bool(europe), 0, surface, False))
        self._built = None

    def add_disc(self, ring, apex, mat, h, dome):
        """An island's planting: a fan of triangles from the closed ring (k, 3), counter-clockwise, to the apex (3,), in slab
        material mat (planar UVs); z are the ground's: the ring is lifted h and the apex h + dome (a gentle mound)."""
        ring = np.asarray(ring, np.float64)[:, :3]
        if len(ring) > 1 and np.allclose(ring[0], ring[-1]):
            ring = ring[:-1]
        if len(ring) < 3:
            return
        self.discs.append((ring, np.asarray(apex, np.float64)[:3], int(mat), float(h), float(dome)))
        self._built = None

    def _disc_mesh(self):
        """(positions, tris, uvs, materials) of the fans, relative to the origin, or None."""
        if not self.discs:
            return None
        parts, base = [], 0
        for ring, apex, mat, h, dome in self.discs:
            n = len(ring)
            P = np.vstack([ring + (0.0, 0.0, h), apex + (0.0, 0.0, h + dome)]) - np.asarray(self.origin, np.float64)
            i = np.arange(n)
            T = np.stack([i, (i + 1) % n, np.full(n, n)], 1) + base
            scale = PLANT_REPEAT_M[PLANTINGS.index(mat)] if mat in PLANTINGS else 3.0
            parts.append((P, T, P[:, :2] / scale, np.full(n, mat, np.int64)))
            base += n + 1
        return (np.concatenate([p[0] for p in parts]), np.concatenate([p[1] for p in parts]),
                np.concatenate([p[2] for p in parts]), np.concatenate([p[3] for p in parts]))

    def empty(self):
        return not self.pieces and not self.discs

    def _native(self, ps):
        """Unmodified 1.7.2 native geometry, for a batch of pieces with one palette."""
        import fast
        fast.require(104, "sidewalk_mesh")
        kind = np.array([p[0] for p in ps], np.int64)
        pts = np.ascontiguousarray(np.concatenate([p[1] for p in ps]) if ps else np.zeros((0, 3)))
        dirs = np.ascontiguousarray(np.concatenate([p[2] for p in ps]) if ps else np.zeros((0, 2)))
        off = np.r_[0, np.cumsum([len(p[1]) for p in ps])].astype(np.int64)
        outs = [p[3] if p[3] is not None else np.zeros((0, 3)) for p in ps]
        outer = np.ascontiguousarray(np.concatenate(outs) if ps else np.zeros((0, 3)))
        ooff = np.r_[0, np.cumsum([len(o) for o in outs])].astype(np.int64)
        rs = [np.asarray(p[4], np.float64).reshape(-1, 2) for p in ps]
        ramps = np.ascontiguousarray(np.concatenate(rs) if ps else np.zeros((0, 2)))
        roff = np.r_[0, np.cumsum([len(r) for r in rs])].astype(np.int64)
        return fast._ef.sidewalk_mesh(
            kind, pts, dirs, off, outer, ooff, ramps, roff, np.array([p[5] for p in ps], np.float64),
            np.array([p[6] for p in ps], np.float64), np.array([p[7] for p in ps], bool).reshape(-1, 2),
            np.array([p[8] for p in ps], np.int64), tuple(float(v) for v in self.origin), self.col is not None)

    @staticmethod
    def _join(meshes):
        if len(meshes) == 1:
            return meshes[0]
        offsets = np.r_[0, np.cumsum([len(m[0]) for m in meshes])]
        return (np.concatenate([m[0] for m in meshes]),
                np.concatenate([m[1] + offsets[i] for i, m in enumerate(meshes)]),
                np.concatenate([m[2] for m in meshes]), np.concatenate([m[3] for m in meshes]))

    def _build(self):
        if self._built is None:
            # Keep contiguous batches to preserve the order of the mesh pieces.
            from itertools import groupby
            batches = [(style, list(ps)) for style, ps in groupby(self.pieces, key=lambda p: p[9:12])]
            if not batches:
                batches = [((False, 0, None), [])]
            full, lean = [], []
            for (european, mat, surface), ps in batches:
                pieces, tips = [], []
                for p in ps:
                    middle, ends = _soft_parts(p)
                    pieces.append(middle)
                    tips.extend(ends)
                drawn, collision = self._native(pieces)
                if tips:
                    drawn, collision = self._with_tips(drawn, collision, tips)
                if european:
                    P, T, UV, M = drawn
                    UV = UV.copy()
                    # Tactile UVs stay intact. The continuous surfaces repeat
                    # in metres with the same phase in neighbouring tiles.
                    curb = np.unique(T[M == 0])
                    slab = np.setdiff1d(np.unique(T[M == 1]), np.unique(T[M == 2]))
                    UV[curb, 0] *= CURB_REPEAT_M
                    if surface is None:
                        UV[slab] = P[slab, :2] / 2.
                    else:
                        repeat = BRICK_REPEAT_M if surface == "brick" else 2.
                        UV[slab] = (P[slab, :2] + self.origin[:2]) / repeat
                    drawn = P, T, UV, np.take((4, EU_SLAB.get(surface, 5), 6, 3), M)
                if mat:                      # an island's apron or planting: its slab in its own material (plan 038)
                    P, T, UV, M = drawn
                    if mat in PLANTINGS:         # (planar UVs: a planting is no row of pavers)
                        UV = UV.copy()
                        slab = np.setdiff1d(np.unique(T[M == BRICK]), np.unique(T[M != BRICK]))
                        UV[slab] = P[slab, :2] / PLANT_REPEAT_M[PLANTINGS.index(mat)]
                    drawn = P, T, UV, np.where(M == BRICK, mat, M)
                full.append(drawn)
                lean.append(collision)       # palette never changes collision geometry/materials
            disc = self._disc_mesh()
            if disc is not None:
                full.append(disc)
                lean.append(disc[:3] + (np.zeros(len(disc[3]), np.int64),))
            self._built = self._join(full), self._join(lean)
        return self._built

    def _with_tips(self, drawn, collision, tips):
        samples = np.concatenate([points.reshape(-1, 3) for points, *_ in tips])
        z = np.asarray(self.ground(samples[:, 0], samples[:, 1])) if self.ground is not None else None
        full, lean, k = [drawn], [collision], 0
        for points, weight, stations, direction, pr, slab in tips:
            count = points.shape[0] * points.shape[1]
            ground = points[:, :1, 2] + SINK if z is None else z[k:k + count].reshape(points.shape[:2])
            ground = np.where(np.isfinite(ground), ground, points[:, :1, 2] + SINK)
            points[:, :, 2] = weight[:, None] * points[:, :, 2] + (1 - weight[:, None]) * (ground - .005)
            full.append(_tip_mesh(points, stations, direction, pr, STRIPS, self.origin, slab))
            if self.col is not None:
                lean.append(_tip_mesh(points[:, LEAN], stations, direction, pr[list(LEAN)], LEAN_STRIPS, self.origin))
            k += count
        return self._join(full), self._join(lean)

    def arrays(self):
        P, T, UV, M = self._build()[0]
        return P, T, UV, M

    def write(self, path):
        P, T, UV, M = self.arrays()
        return cdae.write(path, P, T, uvs=UV, material=list(MATERIALS), tri_materials=M, object_name="walk")

    def write_collision(self, path):
        """The lean collision mesh (Builder(collision=True)) as a never-drawn collision shape."""
        P, T, UV, M = self.col.arrays()
        return cdae.write(path, P, T, uvs=UV, material=MATERIALS[0], collision_only=True)


class _Lean:
    """A Builder's lean collision mesh (the same pieces: earthfast.sidewalk_mesh's second mesh)."""

    def __init__(self, b):
        self.b = b

    def arrays(self):
        P, T, UV, M = self.b._build()[1]
        return P, T, UV, M

    def empty(self):
        return self.b.empty()


def run_geometry(walk_line, side, w):
    """The curb line and outward vectors of a sidewalk run given its centreline (k, 3) and the side of
    the road it is on (+1: the road's left, as roads.offset_polyline). Pure."""
    xy = np.asarray(walk_line, np.float64)[:, :2]
    n = _normals_2d(xy)
    out = n * side                      # left of the road's direction is +n for side +1
    curb = np.array(walk_line, np.float64)
    curb[:, :2] = xy - out * (w / 2)
    return curb, out


def _slab_edges(walk):
    if "corner" in walk:
        corner = walk["corner"]
        return np.asarray(corner["arc"])[:, :2], np.asarray(corner["outer"])[::-1, :2]
    curb, out = run_geometry(walk["pts"], walk.get("side", 1), walk["w"])
    return curb[:, :2], curb[:, :2] + walk["w"] * out


def _slab_quads(curb, back):
    return np.stack((curb[:-1], curb[1:], back[1:], back[:-1]), axis=1)


def _cut_slab(walk, edges, coverage):
    """Trim the length of a run where any part of its slab crosses an earlier sidewalk."""
    from buildings import _area2

    pts = np.asarray(walk["pts"], np.float64)
    step = np.diff(pts[:, :2], axis=0)
    distance = dnp.hypot(*step.T)
    arc = np.r_[0., np.cumsum(distance)]
    blocked = []
    for i, quad in enumerate(_slab_quads(*edges)):
        if distance[i] <= 1e-6:
            continue
        direction = step[i] / distance[i]
        # At a mitre the cross section is oblique. Include its reach along
        # the centreline so a trimmed end cannot poke back into the slab.
        skew = max(abs(dnp.matmul(quad[0] - pts[i, :2], direction)), abs(dnp.matmul(quad[1] - pts[i + 1, :2], direction)))
        for hit in coverage.overlaps(quad):
            if _area2(hit) < .025:
                continue
            s = dnp.matmul(hit - pts[i, :2], direction) + arc[i]
            blocked.append((max(0., float(s.min()) - skew - .04), min(arc[-1], float(s.max()) + skew + .04)))
    if not blocked:
        return [walk]
    spans = []
    start = 0.
    for lo, hi in sorted(blocked) + [(arc[-1], arc[-1])]:
        if lo - start >= .8:
            spans.append((start, lo))
        start = max(start, hi)
    out = []
    for lo, hi in spans:
        s = np.r_[lo, arc[(arc > lo) & (arc < hi)], hi]
        first, last = lo <= 1e-6, hi >= arc[-1] - 1e-6
        caps = walk["caps"]
        w = dict(walk, pts=np.column_stack([np.interp(s, arc, v) for v in pts.T]),
                 caps=(caps[0] or not first, caps[1] or not last),
                 ramps=[(max(a, lo) - lo, min(b, hi) - lo) for a, b in walk.get("ramps", ()) if b > lo and a < hi])
        if "drain_ends" in w:
            a, b = w["drain_ends"]
            w["drain_ends"] = a and first, b and last
        out.append(w)
    return out


def clear_overlaps(walks):
    """A junction slab, then a surveyed path, takes precedence over an intersecting inferred run."""
    if len(walks) < 2 or not any(w.get("europe") for w in walks):
        return walks
    discs = [w for w in walks if "disc" in w]       # (an island's planting: no slab, kept as it is)
    if discs:
        return clear_overlaps([w for w in walks if "disc" not in w]) + discs
    from frontage_geometry import Coverage

    edges = [_slab_edges(w) for w in walks]
    points = np.concatenate([p for pair in edges for p in pair])
    coverage = Coverage(np.r_[points.min(axis=0), points.max(axis=0)])
    priority = sorted(range(len(walks)), key=lambda i: (
        "corner" not in walks[i], not walks[i].get("median"), not walks[i].get("mapped"), str(walks[i].get("fid", ""))))
    kept = [[] for _ in walks]
    for i in priority:
        walk = walks[i]
        if "corner" in walk:
            curb, back = edges[i]
            outline = np.concatenate((curb, back[::-1]))
            if not coverage.overlaps_any(outline):
                kept[i] = [walk]
                coverage.reserve(outline)
            continue
        kept[i] = _cut_slab(walk, edges[i], coverage)
        for piece in kept[i]:
            for quad in _slab_quads(*_slab_edges(piece)):
                coverage.reserve(quad)
    return [w for run in kept for w in run]
