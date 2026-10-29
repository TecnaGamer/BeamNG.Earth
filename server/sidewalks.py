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
with flat end caps (curbs). Heights follow the road's (graded) centreline z, carried by the runs.
The mod shows it as a TSStatic that collides with its visible mesh. Pure functions; deterministic.
"""
import numpy as np

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
MATERIALS = ("curbs", "sidewalk1", "tactile_indicator",  # West Coast USA (art/shapes/roads), loaded by the mod
             "bne_sidewalk_brick", "bne_eu_curb", "bne_eu_asphalt_walk", "bne_eu_tactile")
BRICK = 3
BRICK_REPEAT_M = 1.2        # m per repeat of the brick paver texture

# curbs sheet (1024 x 512, 8 bands of 64 px; Collada v up, so band 0 is v in [7/8, 1]):
# rows 0-40 of a band are the light curb top, 40-64 the darker face
V_BAND_TOP, V_LIGHT_LOW, V_BAND_LOW = 1.0 - 1 / 512, 1.0 - 40 / 512, 1.0 - 63 / 512
# sidewalk1 sheet: strip 0 (the top row) across the slab
V_SLAB_IN, V_SLAB_OUT = 1.0 - 0.195, 1.0 - 0.005


def _orient(tris, P, want):
    """tris (m, 3) flipped where their normal points against want (m, 3) or (3,)."""
    a, b, c = P[tris[:, 0]], P[tris[:, 1]], P[tris[:, 2]]
    n = np.cross(b - a, c - a)
    flip = (n * want).sum(-1) < 0
    tris = tris.copy()
    tris[flip] = tris[flip][:, ::-1]
    return tris


def _normals_2d(xy):
    """Unit left normals at each point of a polyline (mitred like roads.offset_polyline, clamped)."""
    d = np.diff(xy, axis=0)
    L = np.hypot(d[:, 0], d[:, 1])
    L[L == 0] = 1.0
    t = d / L[:, None]
    nseg = np.stack([-t[:, 1], t[:, 0]], axis=1)
    n = np.empty((len(xy), 2))
    n[0], n[-1] = nseg[0], nseg[-1]
    if len(xy) > 2:
        m = nseg[:-1] + nseg[1:]
        ml = np.hypot(m[:, 0], m[:, 1])
        ml[ml < 1e-9] = 1.0
        m = m / ml[:, None]
        cos = np.clip((m * nseg[1:]).sum(1), 0.5, 1.0)          # miter at most 2x
        n[1:-1] = m / cos[:, None]
    return n


RAMP_RUN = 1.8          # m over which a curb ramp's slab climbs back to the sidewalk (1:12)
RAMP_FLARE = 0.6        # m of curb sloping down on each side of a ramp
RAMP_H = 0.01           # m: the curb at a ramp (flush with the gutter)
TACTILE_REPEAT_M = 0.6  # m per tactile pad texture repeat
PAD_W = 0.6             # m of a ramp's slope from the curb that is the tactile pad (US: 2 ft of domes)


def profile(h=CURB_H, w=2.5, hr=None):
    """The cross-section points (offset from the curb line outwards, height above the road): face
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
        L2 = float(seg @ seg)
        if L2 > 0:
            t = np.clip(rel @ seg / L2, 0.0, 1.0)
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


def curb_heights(s, ramps, h=CURB_H):
    """The curb height at arc lengths s along a run with curb ramps [(s0, s1)]: RAMP_H inside, sloping
    back to h over RAMP_FLARE either side. Pure."""
    hr = np.full(len(s), float(h))
    for s0, s1 in ramps:
        d = np.maximum(s0 - s, s - s1)          # < 0 inside the ramp
        f = np.clip(d / RAMP_FLARE, 0.0, 1.0)
        hr = np.minimum(hr, RAMP_H + f * (h - RAMP_H))
    return hr


def refine(s, ramps, step=0.5, margin=1.0):
    """Arc lengths s with points added every `step` m around the ramps (so they have their shape)."""
    extra = [np.arange(max(0.0, a - RAMP_FLARE - margin), min(s[-1], b + RAMP_FLARE + margin), step)
             for a, b in ramps]
    ends = [v for a, b in ramps for v in (a, b, a - RAMP_FLARE, b + RAMP_FLARE) if 0 < v < s[-1]]
    return np.unique(np.r_[s, np.concatenate(extra) if extra else [], ends])


class Builder:
    """Accumulates sidewalk runs and corners into one mesh (positions relative to `origin`)."""

    def __init__(self, origin, collision=False):
        self.origin = np.asarray(origin, np.float64)
        self.P, self.UV, self.T, self.M = [], [], [], []
        self.n = 0
        self.col = Builder._lean(origin) if collision else None   # the lean collision mesh beside it
        self.lean = False
        self.europe = False

    def _materials(self, indices):
        return np.take((4, 5, 6, 3), indices) if self.europe else indices

    @classmethod
    def _lean(cls, origin):
        b = cls(origin)
        b.lean = True
        return b

    def _add(self, P, UV):
        k = self.n
        self.P.append(P)
        self.UV.append(UV)
        self.n += len(P)
        return k

    def _sections(self, curb, toward_out, w, h, hr):
        """ring (k, 7, 3) of the cross-sections, and the per-point profiles (k, 7, 2)."""
        k = len(curb)
        prof = np.stack([profile(h, w, float(x)) for x in hr])
        ring = np.empty((k, prof.shape[1], 3))
        ring[:, :, :2] = curb[:, None, :2] + toward_out[:, None, :] * prof[:, :, 0:1]
        ring[:, :, 2] = curb[:, None, 2] + prof[:, :, 1]
        return ring - self.origin, prof

    def _strips(self, ring, prof, toward_out, s, strips, tactile=None, slab=1):
        k = len(ring)
        for a, b, mat, va, vb in strips:
            rep = CURB_REPEAT_M if mat == 0 else SLAB_REPEAT_M
            if self.europe:
                rep = 1.0 if mat == 0 else 2.0
            P = np.concatenate([ring[:, a], ring[:, b]])
            UV = np.concatenate([np.stack([s / rep, np.full(k, va)], 1), np.stack([s / rep, np.full(k, vb)], 1)])
            if self.europe and mat == 1:
                UV = np.concatenate([np.c_[s/2., prof[:, a, 0]/2.], np.c_[s/2., prof[:, b, 0]/2.]])
            if mat == 1 and slab == BRICK:      # brick pavers: a tileable texture in world metres
                UV = np.concatenate([np.stack([s / BRICK_REPEAT_M, prof[:, a, 0] / BRICK_REPEAT_M], 1),
                                     np.stack([s / BRICK_REPEAT_M, prof[:, b, 0] / BRICK_REPEAT_M], 1)])
                mat = BRICK
            mats = np.full(k - 1, mat)
            if tactile is not None and a == 3:  # the ramp's slope: a tactile pad where lowered
                mats[tactile] = 2
                UV = UV.copy()
                tu = s / TACTILE_REPEAT_M
                UV[:k, 0] = np.where(np.concatenate((tactile, tactile[-1:])), tu, UV[:k, 0])
                UV[k:, 0] = np.where(np.concatenate((tactile, tactile[-1:])), tu, UV[k:, 0])
            base = self._add(P, UV)
            i = np.arange(k - 1)
            A, A1, B1, B = base + i, base + i + 1, base + k + i + 1, base + k + i
            tris = np.concatenate([np.stack([A, B1, A1], 1), np.stack([A, B, B1], 1)])
            # counter-clockwise seen from outside (dae convention): the profile runs clockwise
            # (offset out, z up), so an edge's outward normal is its left normal (-dz, doff)
            dp = 0.5 * ((prof[:-1, b] - prof[:-1, a]) + (prof[1:, b] - prof[1:, a]))
            o3 = np.concatenate([toward_out[i], np.zeros((k - 1, 1))], 1)
            want = -dp[:, 1:2] * o3 + dp[:, 0:1] * np.array([0.0, 0.0, 1.0])
            self.T.append(base + _orient(tris - base, P, np.concatenate([want, want])))
            self.M.append(self._materials(np.r_[mats, mats]))

    def add_run(self, curb, toward_out, w, h=CURB_H, caps=(True, True), ramps=(), slab=1, europe=False):
        """One sidewalk run: curb (k, 3) the curb line (z = the road there), toward_out (k, 2) unit
        vectors from the curb outwards (scaled by the miter), w its width; caps: end caps at the
        (start, end) (not where the run goes on in the next tile or meets a corner piece); ramps:
        curb ramps [(s0, s1)] in arc length along the curb."""
        self.europe = bool(europe and not self.lean)
        curb = np.asarray(curb, np.float64)
        toward_out = np.asarray(toward_out, np.float64)
        if len(curb) < 2:
            return
        if self.col is not None:
            self.col.add_run(curb, toward_out, w, h, caps, ramps)
        s = np.concatenate(([0.0], np.cumsum(np.hypot(*np.diff(curb[:, :2], axis=0).T))))
        if ramps:
            s2 = refine(s, ramps)
            curb = np.stack([np.interp(s2, s, curb[:, c]) for c in range(3)], axis=1)
            toward_out = np.stack([np.interp(s2, s, toward_out[:, c]) for c in range(2)], axis=1)
            s = s2
        hr = curb_heights(s, ramps, h)
        if self.lean:                           # the collision mesh: fewer points, the lean profile
            keep = simplify(np.c_[curb, hr])
            curb, toward_out, s, hr = curb[keep], toward_out[keep], s[keep], hr[keep]
        k = len(curb)
        ring, prof = self._sections(curb, toward_out, w, h, hr)
        if self.lean:
            ring, prof = ring[:, LEAN], prof[:, LEAN]
            self._strips(ring, prof, toward_out, s, LEAN_STRIPS)
        else:
            low = hr < h - 1e-6
            tactile = (low[:-1] | low[1:]) if low.any() else None
            self._strips(ring, prof, toward_out, s, STRIPS, tactile, slab)
        for end, sgn, cap in ((0, -1, caps[0]), (k - 1, 1, caps[1])):   # end caps: a fan over the profile
            if not cap:
                continue
            P = ring[end]
            pr = prof[end]
            UV = np.stack([pr[:, 0] / CURB_REPEAT_M, V_BAND_LOW + (pr[:, 1] + SINK) / (h + SINK + 0.05)
                           * (V_LIGHT_LOW - V_BAND_LOW)], 1)
            base = self._add(P, UV)
            fan = np.array([(base, base + j, base + j + 1) for j in range(1, len(pr) - 1)])
            t = curb[min(end + 1, k - 1), :2] - curb[max(end - 1, 0), :2]
            t = np.r_[t / max(np.hypot(*t), 1e-9), 0.0] * sgn
            self.T.append(base + _orient(fan - base, P, t))
            self.M.append(self._materials(np.full(len(fan), 0)))

    def add_corner(self, arc, inward, outer, h=CURB_H, ramps=(), europe=False):
        """A corner piece: the curb along the polyline arc (k, 3) (a curb return, with the straight
        runs and tapers of curb extensions; inward: unit vectors toward the sidewalk), the slab from
        the curb top to the outer corner points [(x, y, z) ...] (from the second sidewalk's end round
        to the first's), and the back faces along the outer edges. ramps: [(s0, s1)] arc lengths
        where the curb is lowered (the slab then slopes up from it toward the outer corner)."""
        self.europe = bool(europe and not self.lean)
        arc = np.asarray(arc, np.float64)
        inward = np.asarray(inward, np.float64)
        if self.col is not None:
            self.col.add_corner(arc, inward, outer, h, ramps)
        s = np.concatenate(([0.0], np.cumsum(np.hypot(*np.diff(arc[:, :2], axis=0).T))))
        if ramps:
            s2 = refine(s, ramps)
            arc = np.stack([np.interp(s2, s, arc[:, c]) for c in range(3)], axis=1)
            inward = np.stack([np.interp(s2, s, inward[:, c]) for c in range(2)], axis=1)
            inward /= np.maximum(np.hypot(*inward.T), 1e-9)[:, None]
            s = s2
        hr = curb_heights(s, ramps, h)
        if self.lean:                           # the collision mesh: fewer points, the curb as one face
            keep = simplify(np.c_[arc, hr])
            arc, inward, s, hr = arc[keep], inward[keep], s[keep], hr[keep]
        k = len(arc)
        ring, prof = self._sections(arc, inward, 2.5, h, hr)
        self._strips(ring, prof, inward, s, ((0, 3, 0, 0.0, 0.0),) if self.lean else CURB_STRIPS)
        inner = ring[:, 3]                                  # the curb top's inner edge, at h
        out = np.asarray(outer, np.float64) - self.origin
        out[:, 2] = out[:, 2] + h + 2.5 * CROSS_SLOPE
        poly = np.vstack([inner, out])                      # arc from Ti to Tj, then Tj_out, O, Ti_out
        # planar, at the runs' texel density (the sheet is 4096 x 1024: SLAB_REPEAT_M wide, a quarter
        # of that high); a constant v stretched one texel row over the corner (the user, 2026-10-11)
        UV = np.c_[poly[:, 0] / SLAB_REPEAT_M, poly[:, 1] / (SLAB_REPEAT_M / 4.0)]
        if self.europe:
            UV = poly[:, :2]/2.
        base = self._add(poly, UV)
        m = len(poly)
        if len(out) == 3:                                   # a fan from O
            o_idx = k + 1
            fan = np.array([(o_idx, a, (a + 1) % m) for a in range(m) if a != o_idx and (a + 1) % m != o_idx])
        else:                                               # round a merged junction's connector: ear clipping
            import buildings
            xy = poly[:, :2]
            area = float(np.sum(xy[:, 0] * np.roll(xy[:, 1], -1) - np.roll(xy[:, 0], -1) * xy[:, 1]))
            order = np.arange(m) if area > 0 else np.arange(m)[::-1]
            fan = order[buildings.triangulate(xy[order])]
        self.T.append(base + _orient(fan, poly, np.array([0.0, 0.0, 1.0])))
        self.M.append(self._materials(np.full(len(fan), 1)))
        c = poly[:, :2].mean(axis=0)
        drop = h + SINK + 2.5 * CROSS_SLOPE
        for a, b in zip(out[:-1], out[1:]):                 # back faces down to the ground, sloping out (BACK_RUN)
            t = b[:2] - a[:2]
            nrm = np.array([t[1], -t[0]]) / max(float(np.hypot(*t)), 1e-9)
            if nrm @ (0.5 * (a[:2] + b[:2]) - c) < 0:
                nrm = -nrm
            run = np.r_[nrm * BACK_RUN * drop, -drop]
            P = np.array([a, b, b + run, a + run])
            L = float(np.hypot(*(b[:2] - a[:2]))) / CURB_REPEAT_M
            UV = np.array([[0, V_LIGHT_LOW], [L, V_LIGHT_LOW], [L, V_BAND_LOW], [0, V_BAND_LOW]])
            mid = 0.5 * (a[:2] + b[:2])
            want = np.r_[mid - c, 0.0]
            base = self._add(P, UV)
            quad = np.array([(0, 1, 2), (0, 2, 3)])
            self.T.append(base + _orient(quad, P, want))
            self.M.append(self._materials(np.zeros(2, int)))

    def empty(self):
        return self.n == 0

    def arrays(self):
        P = np.concatenate(self.P)
        UV = np.concatenate(self.UV)
        T = np.concatenate(self.T)
        M = np.concatenate(self.M)
        return P, T, UV, M

    def write(self, path):
        P, T, UV, M = self.arrays()
        return cdae.write(path, P, T, uvs=UV, material=list(MATERIALS), tri_materials=M, object_name="walk")

    def write_collision(self, path):
        """The lean collision mesh (Builder(collision=True)) as a never-drawn collision shape."""
        P, T, UV, M = self.col.arrays()
        return cdae.write(path, P, T, uvs=UV, material=MATERIALS[0], collision_only=True)


def run_geometry(walk_line, side, w):
    """The curb line and outward vectors of a sidewalk run given its centreline (k, 3) and the side of
    the road it is on (+1: the road's left, as roads.offset_polyline). Pure."""
    xy = np.asarray(walk_line, np.float64)[:, :2]
    n = _normals_2d(xy)
    out = n * side                      # left of the road's direction is +n for side +1
    curb = np.array(walk_line, np.float64)
    curb[:, :2] = xy - out * (w / 2)
    return curb, out
