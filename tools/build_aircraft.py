"""build_aircraft.py: four unbranded parked-aircraft models (mod/.../art/shapes/beamngEarth/bne_ac_*.dae and
aircraft.materials.json), for the airports' apron stands (server parkedaircraft.py; trees.lua CATALOG ac_*).

    python tools/build_aircraft.py

    bne_ac_light_<livery>      a four-seat high-wing single (nose propeller, fixed tricycle gear)
    bne_ac_turboprop_<livery>  a regional twin turboprop, high wing, T-tail
    bne_ac_narrow_<livery>     a narrow-body twin-turbofan jet, low wing, underwing engines
    bne_ac_wide_<livery>       a wide-body twin-turbofan jet, low wing, large engines, six-wheel main bogies

Seven liveries of each (LIVERIES: plain white, and a white fuselage with a red / navy / sky blue / green / orange / purple
tail fin, some with a thin cheatline of the same colour under the window row, some with a coloured belly). Each its own .dae
(the forest props are one mesh each) sharing the mesh, LODs and collision, differing in the tail, cheatline and belly materials.

Z up, nose along local +Y, the origin on the ground under the aircraft's centre (half the length back from the nose tip),
the wheels on z = 0, scale 1 = real size, so the model's height is its tail fin's top (DIMS: length / span / tail height,
m; trees.lua's catalogue carries the tail height). The right wing is +X. Built from primitives, parametrically:

    fuselage   a lofted elliptical tube: a rounded nose (a blunt cowling on the light single), a straight cabin, a tail cone
               rising a little (upsweep); its belly and the wings' undersides the light grey material, the rest white
    windows    dark flat patches laid on the skin: a windscreen band of four or six flat panels on the nose's front face, a row of cabin windows a side (a dash
               a window at LOD0, one strip at LOD1)
    wing       swept, tapered, with dihedral: a loft of six-point aerofoil sections; a high wing's struts on the light single
    tail       a swept tapered fin to the tail height and a tailplane (on the fin's top on the T-tail)
    engines    a nose propeller (spinner and blades) on the light single, two wing-mounted tractor propellers on the
               turboprop, two underwing nacelles on pylons on the jets, each with its dark inlet and exhaust faces
    gear       struts and wheel cylinders: the nose leg, two main legs (a six-wheel bogie each on the wide-body)

Ten flat colour materials (aircraft.materials.json, double sided): bne_ac_white, bne_ac_grey, bne_ac_dark (windows, engine
inlets and exhausts), bne_ac_tyre (tyres, propeller blades), and the six livery colours bne_ac_red, _navy, _sky, _green,
_orange, _purple. No textures, no logos, no real airline's colours. Two levels of detail (nodes
bne_ac_<kind>_<livery>160 and <kind>_<livery>16: the engine draws them by pixel size, as the other bne props) and a low-poly
collision mesh (Colmesh_bne_ac_<kind>_<livery>-1, collision() below: a few tapered boxes for the fuselage, wings, fin, tailplane and, where they
hang low, the engines; the landing gear is left out), so a car or a person is stopped by a parked aircraft.
"""
import json
import math
import os
import sys
import uuid

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))

OUT = os.path.join(HERE, "..", "mod", "beamng_earth", "levels", "beamng_earth", "art", "shapes", "beamngEarth")
DIMS = {                        # kind: (length m, wing span m, tail fin top height m)
    "light": (8.3, 11.0, 2.7),
    "turboprop": (27.2, 27.1, 7.6),
    "narrow": (37.6, 35.8, 11.8),
    "wide": (63.7, 60.1, 17.5),
}
COL_FIT = 0.9                   # the collision boxes' half sizes as a fraction of the skin's (a box's corners stand off an ellipse)
COL_MIN_T = 0.4                 # m: the least half thickness of a collision wing, tailplane or fin section (at
                                # 0.15 a car's nodes went through the fin and tailplane between two physics steps)
LODS = (160, 16)                # the pixel sizes the two levels of detail start at (the node names' suffixes)
MATERIALS = {                   # name -> (baseColorFactor, metallic, roughness)
    "bne_ac_white": ((0.90, 0.90, 0.88, 1.0), 0.0, 0.45),
    "bne_ac_grey": ((0.62, 0.64, 0.67, 1.0), 0.15, 0.55),
    "bne_ac_dark": ((0.05, 0.06, 0.08, 1.0), 0.0, 0.15),
    "bne_ac_tyre": ((0.03, 0.03, 0.03, 1.0), 0.0, 0.9),
    "bne_ac_red": ((0.70, 0.09, 0.09, 1.0), 0.0, 0.45),
    "bne_ac_navy": ((0.05, 0.10, 0.33, 1.0), 0.0, 0.45),
    "bne_ac_sky": ((0.28, 0.60, 0.86, 1.0), 0.0, 0.45),
    "bne_ac_green": ((0.08, 0.46, 0.22, 1.0), 0.0, 0.45),
    "bne_ac_orange": ((0.93, 0.47, 0.08, 1.0), 0.0, 0.45),
    "bne_ac_purple": ((0.38, 0.18, 0.52, 1.0), 0.0, 0.45),
}
W, G, D, T = "bne_ac_white", "bne_ac_grey", "bne_ac_dark", "bne_ac_tyre"
# The liveries, in the order trees.lua catalogues them (the server's parkedaircraft.LIVERIES is the same list; the index is
# the row's choice): (name, colour material or None, a cheatline under the window row, a coloured belly). The tail fin takes
# the colour; the first is the plain white aircraft.
LIVERIES = (
    ("white", None, False, False),
    ("red", "bne_ac_red", True, False),
    ("navy", "bne_ac_navy", False, False),
    ("sky", "bne_ac_sky", False, True),
    ("green", "bne_ac_green", True, False),
    ("orange", "bne_ac_orange", False, False),
    ("purple", "bne_ac_purple", False, True),
)
CHEAT_M = 0.009                 # the cheatline's width as a fraction of the aircraft's length (at least CHEAT_MIN_M)
CHEAT_MIN_M = 0.10
WS_DEG = (28.0, 55.0, 70.0)      # the windscreen band's panel edges: angles round the axis from the top (the centre pair, a side panel, a rear side panel)
WS_BOT = 0.15                   # the windscreen's level bottom edge over the fuselage centreline, in units of rz
WS_LIFT = 0.02                  # m: a windscreen panel stands off the skin
FOIL = [(0.0, 0.0), (0.2, 0.55), (0.55, 0.5), (1.0, 0.0), (0.55, -0.4), (0.2, -0.45)]   # (chord fraction, thickness fraction)

# The numbers per kind. Fuselage: rx / rz the cabin's half width / half height, zc its centreline's height over the ground,
# nose_ext the spinner beyond the skin's nose, nose_len / tail_len the nose and tail cone as fractions of the skin's length,
# nose_tip / tail_end the end radii as fractions of the cabin's, upsweep the tail's rise, nose_drop the nose's sag.
# win = (first, last d from the skin's nose m, pitch, dash length, centre angle, half angle deg); ws = (the windscreen band's front and rear limits:
# the nose's radius there as a fraction of the cabin's; 2 or 3 panels a side: centre, side, rear side).
# wing = (root LE d, root chord, tip chord, LE sweep deg, dihedral deg, root t/c, tip t/c, root x, root centre z over the
# fuselage's centreline in units of rz). tail = (fin root chord, tip chord, LE sweep deg, root TE metres from the tail tip,
# stab span, stab root chord, tip chord, sweep deg, dihedral deg, TE metres from the tail tip).
AC = {
    "light": dict(nose_ext=0.30, rx=0.60, rz=0.68, zc=1.36, nose_len=0.30, nose_tip=0.50, tail_len=0.50, tail_end=0.10,
                  upsweep=0.35, nose_drop=0.0,
                  win=(2.45, 3.95, 0.75, 0.55, 38.0, 22.0), ws=(0.45, 0.85, 2),
                  wing=(2.55, 1.66, 1.13, 0.0, 1.5, 0.12, 0.09, 0.40, 1.0),
                  tail=(1.50, 0.90, 28.0, 0.25, 3.40, 1.20, 0.85, 8.0, 0.0, 0.20)),
    "turboprop": dict(nose_ext=0.0, rx=1.40, rz=1.40, zc=2.30, nose_len=0.09, nose_tip=0.06, tail_len=0.30, tail_end=0.10,
                      upsweep=0.80, nose_drop=0.15,
                      win=(3.6, 18.4, 0.66, 0.40, 10.0, 7.0), ws=(0.40, 0.80, 2),
                      wing=(11.0, 2.90, 1.35, 3.0, 0.0, 0.14, 0.10, 0.70, 0.9),
                      tail=(4.20, 1.80, 28.0, 1.20, 7.30, 1.90, 1.00, 0.0, 0.0, 1.20)),
    "narrow": dict(nose_ext=0.0, rx=1.97, rz=1.97, zc=3.80, nose_len=0.09, nose_tip=0.03, tail_len=0.24, tail_end=0.10,
                   upsweep=0.90, nose_drop=0.25,
                   win=(5.5, 28.0, 0.75, 0.42, 12.0, 5.0), ws=(0.40, 0.80, 3),
                   wing=(12.4, 6.30, 1.60, 27.0, 5.0, 0.13, 0.10, 1.00, -0.45),
                   tail=(6.20, 2.40, 35.0, 2.60, 12.45, 3.00, 1.20, 30.0, 6.0, 2.60)),
    "wide": dict(nose_ext=0.0, rx=3.10, rz=3.10, zc=5.10, nose_len=0.085, nose_tip=0.03, tail_len=0.22, tail_end=0.10,
                 upsweep=1.50, nose_drop=0.40,
                 win=(8.0, 48.0, 0.95, 0.50, 11.0, 4.2), ws=(0.40, 0.80, 3),
                 wing=(22.0, 10.80, 2.40, 36.0, 6.0, 0.13, 0.10, 1.50, -0.35),
                 tail=(9.50, 3.80, 40.0, 4.00, 21.50, 6.30, 2.30, 35.0, 7.0, 4.00)),
}


def unit(v):
    v = np.asarray(v, np.float64)
    return v / np.linalg.norm(v)


class Mesh:
    """Triangles per material, as (positions (3, 3), normals (3, 3)) rows; finish() welds them into indexed meshes."""

    def __init__(self):
        self.rows = {}

    def _emit(self, mat, p, n):
        self.rows.setdefault(mat, []).append((p, n))

    def loft(self, mat, rings, caps=(None, None), smooth=True):
        """A tube through rings (a list of (k, 3) arrays, the same k, the points in the same order round), outward faces:
        mat is a material name or a function of the face normal giving one; smooth shares the normals along the rings;
        caps = (start material, end material) closes the ends with flat fans."""
        R = [np.asarray(r, np.float64) for r in rings]
        k = len(R[0])
        V = np.concatenate(R)
        cen = [r.mean(axis=0) for r in R]
        faces = []
        for a in range(len(R) - 1):
            c, b = (cen[a] + cen[a + 1]) / 2.0, a + 1
            for j in range(k):
                j1 = (j + 1) % k
                q = (a * k + j, a * k + j1, b * k + j1, b * k + j)
                for t in ((q[0], q[1], q[2]), (q[0], q[2], q[3])):
                    p0, p1, p2 = V[t[0]], V[t[1]], V[t[2]]
                    n = np.cross(p1 - p0, p2 - p0)
                    if n @ ((p0 + p1 + p2) / 3.0 - c) < 0.0:
                        t, n = (t[0], t[2], t[1]), -n
                    faces.append((t, n))
        Nv = np.zeros_like(V)
        for t, n in faces:
            for i in t:
                Nv[i] += n
        Nv /= np.maximum(np.linalg.norm(Nv, axis=1), 1e-12)[:, None]
        for t, n in faces:
            ln = np.linalg.norm(n)
            if ln < 1e-12:
                continue
            nf = n / ln
            self._emit(mat(nf) if callable(mat) else mat, V[list(t)], Nv[list(t)] if smooth else np.tile(nf, (3, 1)))
        for end, cm in ((0, caps[0]), (-1, caps[1])):
            if cm is None:
                continue
            ring, other = R[end], cen[1 if end == 0 else -2]
            c = ring.mean(axis=0)
            for j in range(k):
                self.poly(cm, [c, ring[j], ring[(j + 1) % k]], other, away=True)

    def poly(self, mat, pts, ref, away=True):
        """A planar polygon fanned, its front facing away from (or towards) the point ref."""
        P = [np.asarray(p, np.float64) for p in pts]
        for i in range(1, len(P) - 1):
            n = np.cross(P[i] - P[0], P[i + 1] - P[0])
            ln = np.linalg.norm(n)
            if ln < 1e-12:
                continue
            tri = (P[0], P[i], P[i + 1])
            if (n @ ((P[0] + P[i] + P[i + 1]) / 3.0 - ref) < 0.0) == away:
                tri, n = (P[0], P[i + 1], P[i]), -n
            self._emit(mat, np.array(tri), np.tile(n / ln, (3, 1)))

    def grid(self, mat, P, refs):
        """A (rows, cols) grid of points as quads, each facing away from its row's reference point."""
        for i in range(len(P) - 1):
            for j in range(len(P[0]) - 1):
                ref = (refs[i] + refs[i + 1]) / 2.0
                self.poly(mat, [P[i][j], P[i][j + 1], P[i + 1][j + 1], P[i + 1][j]], ref)

    def tube(self, mat, p0, p1, r0, r1, k, caps=(None, None), smooth=True):
        """A cylinder / cone from p0 to p1."""
        p0, p1 = np.asarray(p0, np.float64), np.asarray(p1, np.float64)
        a = unit(p1 - p0)
        u = unit(np.cross(a, [0.0, 0.0, 1.0] if abs(a[2]) < 0.9 else [1.0, 0.0, 0.0]))
        v = np.cross(a, u)
        ang = np.linspace(0.0, 2.0 * math.pi, k, endpoint=False)
        ring = lambda c, r: np.array([c + r * (math.cos(t) * u + math.sin(t) * v) for t in ang])
        self.loft(mat, [ring(p0, r0), ring(p1, r1)], caps, smooth)

    def finish(self):
        """{material: (vertices (n, 3), normals (n, 3), triangles (m, 3))}, equal (position, normal) pairs welded."""
        out = {}
        for mat, rows in self.rows.items():
            P = np.concatenate([p for p, _ in rows])
            N = np.concatenate([n for _, n in rows])
            key = np.round(np.c_[P, N], 4)
            uniq, inv = np.unique(key, axis=0, return_inverse=True)
            out[mat] = (uniq[:, :3], uniq[:, 3:], np.asarray(inv).reshape(-1, 3))
        return out


def foil(O, cvec, tvec, t):
    """The six-point aerofoil section's ring: leading edge O, the chord along cvec (its length the chord), thickness t along tvec."""
    return np.array([O + s * cvec + h * t * np.asarray(tvec) for s, h in FOIL])


def livery_spec(livery):
    """(name, colour material or None, cheatline, coloured belly) of a livery by name."""
    for spec in LIVERIES:
        if spec[0] == livery:
            return spec
    raise KeyError(livery)


def build(kind, lod, livery="white"):
    """The Mesh of one level of detail of an aircraft kind (lod 0 or 1) in a livery (a LIVERIES name), and its measured
    pieces for the report."""
    p = AC[kind]
    _name, tint, cheat, belly = livery_spec(livery)
    TAIL = tint or W                                    # the fin's material
    BELLY = tint if (tint and belly) else G
    L, span, tail_h = DIMS[kind]
    hi = lod == 0
    kf, kn, kw, ks = (24, 16, 12, 6) if hi else (8, 6, 8, 3)         # sides: fuselage, nacelles and spinners, wheels, struts
    m = Mesh()
    rx, rz, zc0 = p["rx"], p["rz"], p["zc"]
    yn = L / 2.0 - p["nose_ext"]                  # the skin's nose tip
    Lf = yn + L / 2.0                                   # the skin's length
    nl, tl = p["nose_len"] * Lf, p["tail_len"] * Lf

    def prof(y):
        """(half width, half height, centreline height) of the skin at y."""
        d = yn - y
        s, dz = 1.0, 0.0
        if d < nl:
            u = max(d / nl, 0.0)
            s = p["nose_tip"] + (1.0 - p["nose_tip"]) * math.sqrt(max(1.0 - (1.0 - u) ** 2, 0.0))
            dz -= p["nose_drop"] * (1.0 - u) ** 2
        e = (d - (Lf - tl)) / tl
        if e > 0.0:
            e = min(e, 1.0)
            s = 1.0 - (1.0 - p["tail_end"]) * e ** 1.3
            dz += p["upsweep"] * e ** 2
        return rx * s, rz * s, zc0 + dz

    # --- fuselage
    us = (0.0, 0.05, 0.12, 0.22, 0.34, 0.5, 0.68, 0.84, 1.0) if hi else (0.0, 0.35, 0.75, 1.0)
    es = (0.0, 0.12, 0.26, 0.42, 0.6, 0.8, 1.0) if hi else (0.0, 0.5, 1.0)
    ds = [u * nl for u in us] + [Lf - tl + e * tl for e in es]
    ds[-1] = Lf
    ang = np.linspace(0.0, 2.0 * math.pi, kf, endpoint=False)
    rings = []
    for d in ds:
        a, b, zc = prof(yn - d)
        rings.append(np.array([[a * math.cos(t), yn - d, zc + b * math.sin(t)] for t in ang]))
    body = lambda n: BELLY if n[2] < -0.6 else W
    m.loft(body, rings, caps=(W, W))
    f = 1.0 / math.cos(math.pi / kf) + 0.004                # the skin's polygon sits inside the ellipse: lift the patches

    def skin(y, deg, side=1.0):
        a, b, zc = prof(y)
        t = math.radians(deg)
        return np.array([side * a * f * math.cos(t), y, zc + b * f * math.sin(t)])

    # --- windows: the windscreen band, a row of cabin windows a side
    # Flat dark quads on the nose's front face, their corners sampled on the skin: a centre pair (a shallow V), a side panel each
    # side angled back along the fuselage and, on the jets, a rear side pair set a little further back. The top edge of each
    # sits on the skin at the rear of the band (angles round the axis from the top, WS_DEG); the bottom edge is level, WS_BOT * rz over the
    # centreline, where the skin allows (the nose's front limit s_f: the radius there as a fraction of the cabin's). Each quad
    # stands WS_LIFT off the skin, and further where the round skin bulges past its plane, so it never sinks.
    s_f, s_r, pairs = p["ws"]
    z_bot = zc0 + WS_BOT * rz
    dgrid = np.linspace(0.0, nl * 1.2, 400)

    def d_of(s_frac):
        """The least d from the skin's nose where the nose's radius is s_frac of the cabin's."""
        return next((d for d in dgrid if prof(yn - d)[0] / rx >= s_frac), dgrid[-1])

    def skin_z(y, x):
        a, b, zc = prof(y)
        return None if abs(x) >= a else zc + b * math.sqrt(1.0 - (x / a) ** 2)

    def lower(x, d0, d1):
        """The skin's point at the lateral x, the first from d0 aft to d1 where it is at least z_bot high."""
        for d in np.linspace(d0, d1, 160):
            z = skin_z(yn - d, x)
            if z is not None and z >= z_bot:
                return np.array([x, yn - d, z])
        return np.array([x, yn - d1, skin_z(yn - d1, x) or z_bot])

    def upper(deg, d):
        a, b, zc = prof(yn - d)
        t = math.radians(deg)
        return np.array([a * math.sin(t), yn - d, zc + b * math.cos(t)])

    def panel(deg0, deg1, d0, d1):
        """A quad over the angles deg0..deg1 (from the top, the +x side) from the band's d0 to d1, lifted off the skin."""
        xs = [upper(g, d1)[0] for g in (deg0, deg1)]
        q = [lower(xs[0], d0, d1), lower(xs[1], d0, d1), upper(deg1, d1), upper(deg0, d1)]
        ref = np.array([0.0, np.mean([v[1] for v in q]), prof(q[0][1])[2]])
        n = unit(np.cross(q[1] - q[0], q[3] - q[0]))
        if n @ (q[0] - ref) < 0.0:
            n = -n
        bulge = 0.0
        for u in np.linspace(0.0, 1.0, 7):
            for v in np.linspace(0.0, 1.0, 7):
                pt = (q[0] * (1 - u) + q[1] * u) * (1 - v) + (q[3] * (1 - u) + q[2] * u) * v
                z = skin_z(pt[1], pt[0])
                if z is not None:
                    bulge = max(bulge, float(n @ (np.array([pt[0], pt[1], z]) - pt)))
        return [v + n * (bulge + WS_LIFT) for v in q]

    bands = [((0.0, WS_DEG[0]), s_f, s_r), ((WS_DEG[0], WS_DEG[1]), s_f, s_r)]
    if pairs > 2:
        bands.append(((WS_DEG[1], WS_DEG[2]), s_f + 0.10, min(s_r + 0.08, 0.95)))
    if not hi:
        bands = [((0.0, WS_DEG[1]), s_f, s_r)]                  # (a coarse level: the two centre panels, wider)
    for (g0, g1), sf, sr in bands:
        for side in (1.0, -1.0):
            q = panel(g0, g1, d_of(sf), d_of(sr))
            q = [np.array([side * v[0], v[1], v[2]]) for v in q]
            m.poly(D, q, np.array([0.0, q[0][1], prof(q[0][1])[2]]))
    w0, w1, pitch, dash, dc, dh = p["win"]
    for side in (1.0, -1.0):
        starts = np.arange(w0, w1 - dash + 1e-9, pitch) if hi else [w0]
        length = dash if hi else w1 - w0
        for d in starts:
            ya, yb = yn - d, yn - d - length
            quad = [skin(ya, dc - dh, side), skin(yb, dc - dh, side), skin(yb, dc + dh, side), skin(ya, dc + dh, side)]
            m.poly(D, quad, np.array([0.0, ya, prof(ya)[2]]))

    # --- the cheatline: a thin band of the livery's colour along the cabin just under the window row, on both sides
    if cheat and tint:
        width = max(CHEAT_MIN_M, CHEAT_M * L)
        half = math.degrees(width / 2.0 / max(p["rz"], 1e-6))           # (the band's half angle round the skin)
        mid = dc - dh - half * 1.4                                       # (a gap of 0.4 half widths under the windows)
        d0, d1 = w0, Lf - tl * 0.5
        n_seg = max(2, int(math.ceil((d1 - d0) / 3.0))) if hi else 3
        ys = [yn - (d0 + (d1 - d0) * i / n_seg) for i in range(n_seg + 1)]
        for side in (1.0, -1.0):
            P = [[skin(y, mid - half, side), skin(y, mid + half, side)] for y in ys]
            m.grid(tint, P, [np.array([0.0, y, prof(y)[2]]) for y in ys])

    # --- wing
    ld, cr, ct, sweep, dih, tr, tt, x0, zk = p["wing"]
    y_le0 = yn - ld
    x1 = span / 2.0
    tsw, tdh = math.tan(math.radians(sweep)), math.tan(math.radians(dih))
    z_root = zc0 + zk * rz + (0.12 if kind == "light" else 0.0)
    le_y = lambda x: y_le0 - tsw * (x - x0)
    chord = lambda x: cr + (ct - cr) * (x - x0) / (x1 - x0)
    thick = lambda x: (tr + (tt - tr) * (x - x0) / (x1 - x0)) * chord(x)
    wz = lambda x: z_root + tdh * (x - x0)
    wb = lambda n: G if n[2] < -0.3 else W
    for side in (1.0, -1.0):
        st = [foil(np.array([side * x, le_y(x), wz(x)]), np.array([0.0, -chord(x), 0.0]), [0, 0, 1], thick(x))
              for x in (x0, x1)]
        m.loft(wb, st, caps=(W, W), smooth=False)

    # --- tail: the fin to the tail height, the tailplane
    fcr, fct, fsw, fte, sspan, scr, sct, ssw, sdh, ste = p["tail"]
    ty = -L / 2.0
    yr = ty + fte + fcr / 2.0
    fz0 = prof(yr)[2] - 0.05 if kind == "light" else prof(yr)[2]
    fh = tail_h - fz0
    fsw_t = min(math.tan(math.radians(fsw)), (ty + fte + fcr - fct - ty - 0.3) / fh)    # (the tip's trailing edge stays inside the tail)
    fin_le = lambda z: ty + fte + fcr - fsw_t * (z - fz0)
    fin_c = lambda z: fcr + (fct - fcr) * (z - fz0) / fh
    st = [foil(np.array([0.0, fin_le(z), z]), np.array([0.0, -fin_c(z), 0.0]), [1, 0, 0], 0.12 * fin_c(z))
          for z in (fz0, tail_h)]
    m.loft(TAIL, st, caps=(TAIL, TAIL), smooth=False)
    tt_ = kind == "turboprop"
    if tt_:                                             # T-tail: the tailplane on the fin's top, its root chord at the tip's
        sle0, sz = fin_le(tail_h) - 0.1, tail_h - 0.30
        sx0 = 0.0
    else:
        sle0 = ty + ste + scr
        sz = prof(ty + ste + scr / 2.0)[2] + 0.1 * rz
        sx0 = 0.3
    sx1 = sspan / 2.0
    ssw_t, sdh_t = math.tan(math.radians(ssw)), math.tan(math.radians(sdh))
    ssw_t = min(ssw_t, (sle0 - sct - (ty + 0.3)) / (sx1 - sx0))
    for side in (1.0, -1.0):
        st = []
        for x in (sx0, sx1):
            c = scr + (sct - scr) * (x - sx0) / (sx1 - sx0)
            st.append(foil(np.array([side * x, sle0 - ssw_t * (x - sx0), sz + sdh_t * (x - sx0)]),
                           np.array([0.0, -c, 0.0]), [0, 0, 1], 0.09 * c))
        m.loft(wb, st, caps=(W, W), smooth=False)

    # --- engines and gear
    def wheel(x, y, z, r, w):
        m.tube(T, (x - w / 2.0, y, z), (x + w / 2.0, y, z), r, r, kw, caps=(T, T))

    def strut(a, b, r):
        m.tube(G, a, b, r, r, ks)

    def nacelle(xe, y0, ze, R, Ln, prof_):
        rg = []
        ang_n = np.linspace(0.0, 2.0 * math.pi, kn, endpoint=False)
        for fy, fr in prof_:
            rg.append(np.array([[xe + R * fr * math.cos(t), y0 - fy * Ln, ze + R * fr * math.sin(t)] for t in ang_n]))
        m.loft(W, rg, caps=(D, D))

    def prop(xe, ze, y_base, y_tip, r_sp, R_p, blades, phase, root_w, tip_w):
        m.tube(G, (xe, y_base, ze), (xe, y_tip, ze), r_sp, 0.03, kn, caps=(None, G))
        for i in range(blades):
            a = math.radians(phase) + 2.0 * math.pi * i / blades
            ra, ta = np.array([math.cos(a), 0.0, math.sin(a)]), np.array([-math.sin(a), 0.0, math.cos(a)])
            c = np.array([xe, y_base + 0.12, ze])
            m.poly(T, [c + ra * r_sp * 0.8 + ta * root_w / 2, c + ra * R_p + ta * tip_w / 2,
                       c + ra * R_p - ta * tip_w / 2, c + ra * r_sp * 0.8 - ta * root_w / 2], c, away=True)

    info = {}
    gear_col = []                   # (x, y, half x, half y, top z): the gear's collision boxes, from the ground
    if kind == "light":
        prop(0.0, zc0 - 0.02, yn, yn + p["nose_ext"], 0.17, 0.95, 2, 80.0, 0.12, 0.07)
        ys_ = yn - 3.3
        gear_col = [(s * 1.25, ys_, 0.2, 0.3, zc0 - 0.5) for s in (1.0, -1.0)] + [(0.0, yn - 1.6, 0.15, 0.25, zc0 - 0.58)]
        for side in (1.0, -1.0):                         # spring-steel mains, the wing struts
            strut(np.array([side * 0.40, ys_, zc0 - 0.50]), np.array([side * 1.25, ys_, 0.30]), 0.04)
            wheel(side * 1.25, ys_, 0.27, 0.27, 0.16)
            sx = 2.6
            strut(np.array([side * 0.60, y_le0 - 0.55, zc0 - 0.55]), np.array([side * sx, le_y(sx) - 0.45 * chord(sx), wz(sx) - thick(sx) * 0.4]), 0.03)
        yg = yn - 1.6
        strut(np.array([0.0, yg, zc0 - 0.58]), np.array([0.0, yg, 0.22]), 0.04)
        wheel(0.0, yg, 0.20, 0.20, 0.12)
    elif kind == "turboprop":
        xe, R = 4.5, 0.50
        ysp = le_y(xe) + 3.3
        yf = ysp - 0.9
        ze = wz(xe) - 0.10
        Ln = yf - (le_y(xe) - 0.9 * chord(xe))
        pr = [(0.0, 0.80), (0.06, 1.0), (0.35, 0.96), (0.8, 0.8), (1.0, 0.4)] if hi else [(0.0, 0.8), (0.1, 1.0), (1.0, 0.4)]
        for side in (1.0, -1.0):
            nacelle(side * xe, yf, ze, R, Ln, pr)
            prop(side * xe, ze, yf, ysp, 0.30, 1.96, 6 if hi else 4, 15.0, 0.30, 0.16)
            ym = le_y(xe) - 0.9
            strut(np.array([side * xe, ym, ze - R * 0.8]), np.array([side * xe, ym, 0.45]), 0.12)
            wheel(side * xe, ym, 0.45, 0.45, 0.55)
        yg = yn - 3.2
        strut(np.array([0.0, yg, zc0 - rz + 0.1]), np.array([0.0, yg, 0.30]), 0.07)
        wheel(0.0, yg, 0.30, 0.30, 0.34)
    else:
        big = kind == "wide"
        xe, R, Ln = (10.9, 2.0, 7.3) if big else (5.75, 1.05, 4.3)
        yf = le_y(xe) + (4.4 if big else 2.4)
        ze = wz(xe) - thick(xe) / 2.0 - 0.35 - R * 0.9
        ze = max(ze, R + 0.55)
        info["engine_clearance"] = ze - R
        pr = [(0.0, 0.90), (0.05, 1.0), (0.45, 1.0), (0.8, 0.86), (1.0, 0.55)] if hi else [(0.0, 0.9), (0.45, 1.0), (1.0, 0.55)]
        pw = 0.28 if big else 0.16
        for side in (1.0, -1.0):
            nacelle(side * xe, yf, ze, R, Ln, pr)
            yp0, yp1 = yf - 0.3 * Ln, yf - 0.85 * Ln
            zt = wz(xe) - thick(xe) * 0.3
            m.loft(W, [np.array([[side * xe + sx_ * pw, y_, z_] for sx_, y_, z_ in ((-1, yp0, ze + R * 0.6), (1, yp0, ze + R * 0.6), (1, yp0, zt), (-1, yp0, zt))]),
                       np.array([[side * xe + sx_ * pw, y_, z_] for sx_, y_, z_ in ((-1, yp1, ze + R * 0.6), (1, yp1, ze + R * 0.6), (1, yp1, zt), (-1, yp1, zt))])],
                   caps=(W, W), smooth=False)
        # main gear under the wing, behind its rear spar line; nose gear under the nose
        xm, ym = (5.5, -0.6) if big else (3.8, 1.5)
        rw, ww = (0.70, 1.15) if big else (0.58, 0.95)
        for side in (1.0, -1.0):
            xs = xm * 0.8
            strut(np.array([side * xs, ym, wz(xs) - thick(xs) / 2.0 + 0.05]), np.array([side * xm, ym, rw + 0.5]), 0.28 if big else 0.16)
            if big:
                strut(np.array([side * xm, ym - 1.4, rw]), np.array([side * xm, ym + 1.4, rw]), 0.16)
                for dy in (-1.4, 0.0, 1.4):
                    wheel(side * xm, ym + dy, rw, rw, ww)
            else:
                wheel(side * xm, ym, rw, rw, ww)
        yg = yn - (6.5 if big else 4.7)
        rn, wn = (0.55, 0.90) if big else (0.38, 0.45)
        # (the gear's collision: a box a leg from the ground to the wing or the belly; a car's nodes went through it)
        gear_col = [(side * xm, ym, ww / 2.0 + 0.1, (1.4 + rw) if big else rw, wz(xm * 0.8) - thick(xm * 0.8) / 2.0)
                    for side in (1.0, -1.0)]
        gear_col.append((0.0, yg, wn / 2.0 + 0.1, rn, prof(yg)[2] - prof(yg)[1] + 0.1))
        strut(np.array([0.0, yg, prof(yg)[2] - prof(yg)[1] + 0.1]), np.array([0.0, yg, rn]), 0.2 if big else 0.1)
        wheel(0.0, yg, rn, rn, wn)

    info.update(root_wing_z=z_root, fin_h=fh)
    stab_c = lambda x: scr + (sct - scr) * (x - sx0) / (sx1 - sx0)
    info["col"] = dict(
        prof=prof, yn=yn, nl=nl, tl=tl, rings_y=(-L / 2.0, -L / 2.0 + tl, yn - nl, yn),
        wing=(x0, x1, le_y, chord, thick, wz),
        fin=(fz0, tail_h, fin_le, fin_c),
        stab=(sx0, sx1, lambda x: sle0 - ssw_t * (x - sx0), stab_c, lambda x: sz + sdh_t * (x - sx0)),
        engines=[] if kind == "light" else [(xe, yf, ze, R, Ln)], gear=gear_col)
    return m, info


def parts(kind, lod, livery="white"):
    """{material: (vertices, normals, triangles)} of one level of detail in a livery."""
    return build(kind, lod, livery)[0].finish()


def tri_count(pt):
    return sum(len(t) for _, _, t in pt.values())


def bounds(pt):
    v = np.concatenate([a for a, _, _ in pt.values()])
    return v.min(axis=0), v.max(axis=0)


def _f(a):
    return " ".join(f"{float(v):.4f}" for v in np.asarray(a).ravel())


_COLLISION = {}


def collision(kind):
    """The collision Mesh (one material, flat faces): a tapered box a part, each a closed convex prism. The fuselage's
    cross-section a rectangle inside the skin's ellipse (COL_FIT), the wings, tailplane and fin lofted between two
    rectangular sections, the underwing engines a box each; under 150 triangles."""
    if kind in _COLLISION:
        return _COLLISION[kind]
    c = build(kind, 1)[1]["col"]
    m = Mesh()
    box = lambda ring_list: m.loft(W, ring_list, caps=(W, W), smooth=False)
    rect = lambda x, y, z, hw, hh: np.array([[x - hw, y, z - hh], [x + hw, y, z - hh], [x + hw, y, z + hh], [x - hw, y, z + hh]])
    rings = []
    for y in c["rings_y"]:
        a, b, zc = c["prof"](y)
        rings.append(rect(0.0, y, zc, a * COL_FIT, b * COL_FIT))
    box(rings)
    x0, x1, le_y, chord, thick, wz = c["wing"]
    sx0, sx1, s_le, s_c, s_z = c["stab"]
    section = lambda x, le, ch, z, t: np.array([[x, le, z - t], [x, le - ch, z - t], [x, le - ch, z + t], [x, le, z + t]])
    for side in (1.0, -1.0):
        box([section(side * x, le_y(x), chord(x), wz(x), max(0.5 * thick(x), COL_MIN_T)) for x in (x0, x1)])
        box([section(side * x, s_le(x), s_c(x), s_z(x), COL_MIN_T) for x in (sx0, sx1)])
    fz0, top, fin_le, fin_c = c["fin"]
    box([np.array([[-COL_MIN_T, fin_le(z), z], [COL_MIN_T, fin_le(z), z],
                   [COL_MIN_T, fin_le(z) - fin_c(z), z], [-COL_MIN_T, fin_le(z) - fin_c(z), z]]) for z in (fz0, top)])
    for xe, yf, ze, R, Ln in c["engines"]:
        for side in (1.0, -1.0):
            box([rect(side * xe, y, ze, R * COL_FIT, R * COL_FIT) for y in (yf, yf - Ln)])
    for gx, gy, hx, hy, top in c["gear"]:
        box([rect(gx, y, top / 2.0, hx, top / 2.0) for y in (gy + hy, gy - hy)])
    _COLLISION[kind] = m
    return m


def _geometry(gid, pt):
    """One <geometry>: the materials' vertices side by side, a <triangles> block each."""
    pos = np.concatenate([pt[k][0] for k in sorted(pt)])
    nrm = np.concatenate([pt[k][1] for k in sorted(pt)])
    blocks, base = "", 0
    for k in sorted(pt):
        v, _, t = pt[k]
        blocks += (f'<triangles count="{len(t)}" material="{k}-mat">'
                   f'<input semantic="VERTEX" source="#{gid}-v" offset="0"/>'
                   f'<input semantic="NORMAL" source="#{gid}-n" offset="0"/>'
                   f'<input semantic="TEXCOORD" source="#{gid}-uv" offset="0" set="0"/>'
                   f'<p>{" ".join(str(int(i)) for i in (t + base).ravel())}</p></triangles>\n')
        base += len(v)
    n = len(pos)
    acc = lambda sid, cnt, names: (f'<technique_common><accessor source="#{sid}-a" count="{cnt}" stride="{len(names)}">'
                                   + "".join(f'<param name="{x}" type="float"/>' for x in names)
                                   + '</accessor></technique_common>')
    return (f'<geometry id="{gid}"><mesh>\n'
            f'<source id="{gid}-p"><float_array id="{gid}-p-a" count="{n * 3}">{_f(pos)}</float_array>{acc(gid + "-p", n, "XYZ")}</source>\n'
            f'<source id="{gid}-n"><float_array id="{gid}-n-a" count="{n * 3}">{_f(nrm)}</float_array>{acc(gid + "-n", n, "XYZ")}</source>\n'
            f'<source id="{gid}-uv"><float_array id="{gid}-uv-a" count="{n * 2}">{_f(np.zeros(n * 2))}</float_array>{acc(gid + "-uv", n, "ST")}</source>\n'
            f'<vertices id="{gid}-v"><input semantic="POSITION" source="#{gid}-p"/></vertices>\n{blocks}</mesh></geometry>\n')


def collada(kind, livery="white"):
    """One .dae of an aircraft kind in a livery: base00 > start01 > a node per level of detail (named <name><pixel size>),
    one geometry each, and the collision mesh (Colmesh_<name>-1; an empty collision-1 node beside start01, as the other
    bne props); name = bne_ac_<kind>_<livery>."""
    name = f"bne_ac_{kind}_{livery}"
    geoms, nodes = [], []
    for size, lod in zip(LODS, (0, 1)):
        pt = parts(kind, lod, livery)
        gid = f"{name}{size}-g"
        geoms.append(_geometry(gid, pt))
        nodes.append(f'<node id="{name}{size}" name="{name}{size}"><instance_geometry url="#{gid}"><bind_material><technique_common>'
                     + "".join(f'<instance_material symbol="{k}-mat" target="#{k}-mat"/>' for k in sorted(pt))
                     + '</technique_common></bind_material></instance_geometry></node>\n')
    col = collision(kind).finish()
    geoms.append(_geometry(f"{name}-col-g", col))
    nodes.append(f'<node id="Colmesh_{name}-1" name="Colmesh_{name}-1"><instance_geometry url="#{name}-col-g"><bind_material>'
                 '<technique_common>' + "".join(f'<instance_material symbol="{k}-mat" target="#{k}-mat"/>' for k in sorted(col))
                 + '</technique_common></bind_material></instance_geometry></node>\n')
    effects = "".join(f'<effect id="{k}-fx"><profile_COMMON><technique sid="common"><lambert><diffuse><color>'
                      f'{" ".join(f"{c:g}" for c in MATERIALS[k][0])}</color></diffuse></lambert></technique></profile_COMMON></effect>\n'
                      for k in MATERIALS)
    mats = "".join(f'<material id="{k}-mat" name="{k}"><instance_effect url="#{k}-fx"/></material>\n' for k in MATERIALS)
    return f'''<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
<asset><contributor><authoring_tool>BeamNG.Earth tools/build_aircraft.py</authoring_tool></contributor>
<unit meter="1" name="meter"/><up_axis>Z_UP</up_axis></asset>
<library_effects>
{effects}</library_effects>
<library_materials>
{mats}</library_materials>
<library_geometries>
{"".join(geoms)}</library_geometries>
<library_visual_scenes><visual_scene id="scene" name="scene">
<node id="base00" name="base00"><node id="start01" name="start01">
{"".join(nodes)}</node><node id="collision-1" name="collision-1"/></node>
</visual_scene></library_visual_scenes>
<scene><instance_visual_scene url="#scene"/></scene>
</COLLADA>
'''


def materials():
    """The materials file: the flat materials (the four basics and the six livery colours), double sided."""
    out = {}
    for name, (rgba, metal, rough) in MATERIALS.items():
        out[name] = {"name": name, "mapTo": name, "class": "Material", "version": 1.5,
                     "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, "beamng.earth/" + name)),
                     "Stages": [{"baseColorFactor": list(rgba), "metallicFactor": metal, "roughnessFactor": rough},
                                {}, {}, {}],
                     "doubleSided": True}
    return json.dumps(out, indent=2) + "\n"


def main():
    import staging
    for kind in DIMS:
        old = os.path.join(OUT, "bne_ac_" + kind + ".dae")        # (the one unliveried model of before)
        if os.path.exists(old):
            os.remove(old)
        for lv in LIVERIES:
            path = os.path.join(OUT, f"bne_ac_{kind}_{lv[0]}.dae")
            tmp = staging.tmp_for(path)
            with open(tmp, "w", newline="\n") as f:
                f.write(collada(kind, lv[0]))
            staging.replace(tmp, path)
            print(path, os.path.getsize(path), "bytes")
    path = os.path.join(OUT, "aircraft.materials.json")
    tmp = staging.tmp_for(path)
    with open(tmp, "w", newline="\n") as f:
        f.write(materials())
    staging.replace(tmp, path)
    print(path, os.path.getsize(path), "bytes")
    for kind, (L, span, tail) in DIMS.items():
        print(f"{kind}: DIMS length {L} m, span {span} m, tail {tail} m")
        for lod in (0, 1):
            m, info = build(kind, lod)
            pt = m.finish()
            lo, hi = bounds(pt)
            print(f"  LOD{lod}: {tri_count(pt)} triangles; measured length {hi[1] - lo[1]:.2f} (y {lo[1]:.2f}..{hi[1]:.2f}), "
                  f"span {hi[0] - lo[0]:.2f}, height {hi[2]:.2f}, lowest z {lo[2]:.3f}"
                  + (f", engine clearance {info['engine_clearance']:.2f}" if "engine_clearance" in info else ""))


if __name__ == "__main__":
    main()
