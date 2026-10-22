#!/usr/bin/env python3
"""Street furniture as modules, modelled after Street View around the Droogleever Fortuynplein (2026-10-21), not
blocked out: every module from a few lofted, lathed and bevelled primitives, textured from one atlas
(tools/sign_atlas.py, the material bne_signs).

- traffic_light(): a 114 mm pole on a base flange, black up to the band, the black-and-white band (0.4 to 2.0 m), black
  again to a domed cap; the head a black body with rounded corners, three 200 mm lenses each in its bezel under a
  visor, held off the pole by two brackets on clamp rings; optional a cyclists' head and a request button lower down.
- mast_arm(): the big junctions' pole (light grey, 6.3 m, the band at its foot) with an arm over the lanes and a head
  hanging over each of them, a head on the pole too.
- sign(): a 60 mm galvanised post with its cap; the plate (a disc, a square or a point-down triangle with rounded
  corners) with its folded rim, its face the sign's cell, its back grey with two rails, clamped to the post.
- gantry(): two tube posts, a box beam, the route signs on it.

Each module is built in its own frame (its foot at the origin, z up, its face toward +y: the traffic it speaks to is
at +y) as (P, T, UV); place() turns it to look along a direction and moves it to its spot."""
import math

import numpy as np

from sign_atlas import CELLS, GRID, ROUTE, SPAN

ATLAS = 4
MATERIAL = "bne_signs"

POLE_R, POLE_H = 0.057, 3.3
BAND = (0.3, 1.9)               # m: where the rings start, and one atlas cell's 8 rings over this much (1.6 m)
HEAD = (0.36, 0.22, 1.02)       # m: the head's width, depth, height (a stadium)
LENS_R = 0.105                  # m: a 200 mm lens
SIGN_POST_R = 0.030
PLATES = {"B6": ("triangle", 0.90), "G11": ("disc", 0.60), "D2": ("disc", 0.70), "L2": ("square", 0.60)}
SIGN_TOP = 2.6                  # m: a plate's top over the ground


def cell_uv(name, u, v):
    """(u, v) in 0..1 over the named cell to the atlas's UV (Collada: v up)."""
    k = CELLS[name]
    c, r = k % GRID[0], k // GRID[0]
    sp = SPAN.get(name, 1)
    u = (0.004 + np.asarray(u, np.float64) * 0.992) * sp
    v = (0.004 + np.asarray(v, np.float64) * 0.992) * sp
    return np.c_[(c + u) / GRID[0], 1 - (r + sp - v) / GRID[1]]


def rrect(w, d, r, n=4):
    """A rounded rectangle (w across x, d along y, corner radius r), counter-clockwise from +x."""
    r = min(r, w / 2 - 1e-4, d / 2 - 1e-4)
    out = []
    for cx, cy, a0 in ((w / 2 - r, d / 2 - r, 0), (-w / 2 + r, d / 2 - r, 90), (-w / 2 + r, -d / 2 + r, 180),
                       (w / 2 - r, -d / 2 + r, 270)):
        for a in np.linspace(a0, a0 + 90, n + 1):
            out.append((cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a))))
    return np.array(out)


class Mesh:
    def __init__(self):
        self.P, self.T, self.UV, self.n = [], [], [], 0

    def add(self, P, T, UV, two_sided=False):
        T = np.asarray(T, np.int64).reshape(-1, 3)
        if two_sided:                   # (thin parts: seen from either side)
            T = np.r_[T, T[:, ::-1]]
        self.P.append(np.asarray(P, np.float64))
        self.T.append(T + self.n)
        self.UV.append(np.asarray(UV, np.float64))
        self.n += len(P)

    def loft(self, rings, cell, cap_bottom=False, cap_top=False, v_span=(0, 1)):
        """A surface through closed rings (each (k, 3), counter-clockwise seen from above, the same k): its outside
        out; u round the rings, v up them over v_span of the cell; flat caps on request."""
        R = [np.asarray(r, np.float64) for r in rings]
        k, m = len(R[0]), len(R)
        P = np.concatenate([np.r_[r, r[:1]] for r in R])        # each ring closed (its seam with its own u)
        u = np.tile(np.linspace(0, 1, k + 1), m)
        v = np.repeat(np.linspace(v_span[0], v_span[1], m), k + 1)
        T = []
        for j in range(m - 1):
            for i in range(k):
                a, b = j * (k + 1) + i, j * (k + 1) + i + 1
                c, d = b + k + 1, a + k + 1
                T += [[a, b, c], [a, c, d]]
        self.add(P, T, cell_uv(cell, u, v))
        for ring, up in ((R[0], False), (R[-1], True)):
            if (up and cap_top) or (not up and cap_bottom):
                c = ring.mean(0)
                fan = [[0, i + 1, (i + 1) % k + 1] for i in range(k)]
                self.add(np.r_[[c], ring], fan if up else [f[::-1] for f in fan],
                         cell_uv(cell, np.full(k + 1, 0.5), np.full(k + 1, 0.5)))

    def lathe(self, profile, cell, x=0.0, y=0.0, seg=16, v_span=(0, 1), cap_bottom=False, cap_top=False):
        """A body of revolution about the vertical through (x, y): profile [(r, z)] bottom to top."""
        a = np.linspace(0, 2 * math.pi, seg, endpoint=False)
        rings = [np.c_[x + r * np.cos(a), y + r * np.sin(a), np.full(seg, z)] for r, z in profile]
        self.loft(rings, cell, cap_bottom=cap_bottom, cap_top=cap_top, v_span=v_span)

    def rounded_box(self, c, w, d, h, r_side, r_edge, cell):
        """A box with rounded vertical corners (r_side) and rounded top and bottom edges (r_edge), centred on c."""
        x, y, z = c
        rings = []
        for th in np.linspace(0, math.pi / 2, 4):           # the bottom edge's quarter round, then the top's
            inset, dz = r_edge * (1 - math.sin(th)), r_edge * (1 - math.cos(th))
            for zz in (z - h / 2 + dz, z + h / 2 - dz):
                ring = rrect(w - 2 * inset, d - 2 * inset, max(r_side - inset, 0.003))
                rings.append((zz, np.c_[ring[:, 0] + x, ring[:, 1] + y, np.full(len(ring), zz)]))
        rings.sort(key=lambda t: t[0])
        self.loft([r for _, r in rings], cell, cap_bottom=True, cap_top=True)

    def disc_y(self, x, y, z, r, cell, seg=24, bulge=0.0, flip=False):
        """A disc facing +y (or -y with flip) at (x, y, z), the whole cell on it; bulge pushes its middle out."""
        a = np.linspace(0, 2 * math.pi, seg, endpoint=False)
        s = -1.0 if flip else 1.0
        ring = np.c_[x + s * r * np.cos(a), np.full(seg, y), z + r * np.sin(a)]
        P = np.r_[[[x, y + s * bulge, z]], ring]
        uv = np.r_[[[0.5, 0.5]], np.c_[0.5 + 0.5 * np.cos(a), 0.5 + 0.5 * np.sin(a)]]
        T = [[0, (i + 1) % seg + 1, i + 1] for i in range(seg)]
        self.add(P, T, cell_uv(cell, uv[:, 0], uv[:, 1]), two_sided=True)

    def annulus_y(self, x, y, z, r0, r1, cell, depth=0.0, seg=24):
        """A ring facing +y between r0 and r1 (a lens's bezel), its inner edge depth behind its outer."""
        a = np.linspace(0, 2 * math.pi, seg, endpoint=False)
        outer = np.c_[x + r1 * np.cos(a), np.full(seg, y), z + r1 * np.sin(a)]
        inner = np.c_[x + r0 * np.cos(a), np.full(seg, y - depth), z + r0 * np.sin(a)]
        T = []
        for i in range(seg):
            j = (i + 1) % seg
            T += [[i, i + seg, j + seg], [i, j + seg, j]]
        self.add(np.r_[outer, inner], T, cell_uv(cell, np.full(2 * seg, 0.5), np.full(2 * seg, 0.5)), two_sided=True)

    def visor(self, x, y, z, r, length, cell, t=0.006, a0=-25.0, a1=205.0, seg=14):
        """A lens's hood: a part cylinder over the lens (from a0 to a1 degrees, 90 at its top) sticking out along +y,
        t thick (its two skins and its front lip)."""
        a = np.radians(np.linspace(a0, a1, seg))
        uv = cell_uv(cell, np.r_[np.linspace(0, 1, seg), np.linspace(0, 1, seg)], [0.0] * seg + [1.0] * seg)
        q = [[i, i + 1, i + 1 + seg] for i in range(seg - 1)] + [[i, i + 1 + seg, i + seg] for i in range(seg - 1)]
        for rr in (r, r + t):
            arc = np.c_[x + rr * np.cos(a), z + rr * np.sin(a)]
            P = np.r_[np.c_[arc[:, 0], np.full(seg, y), arc[:, 1]], np.c_[arc[:, 0], np.full(seg, y + length), arc[:, 1]]]
            self.add(P, q, uv, two_sided=True)
        P = np.r_[np.c_[x + r * np.cos(a), np.full(seg, y + length), z + r * np.sin(a)],
                  np.c_[x + (r + t) * np.cos(a), np.full(seg, y + length), z + (r + t) * np.sin(a)]]
        self.add(P, q, uv, two_sided=True)

    def bar(self, a, b, w, h, cell):
        """A rectangular bar from a to b (3D points, horizontal), w across, h tall."""
        a, b = np.asarray(a, np.float64), np.asarray(b, np.float64)
        dv = b - a
        dv[2] = 0
        L = np.hypot(dv[0], dv[1])
        dv /= max(L, 1e-9)
        n = np.array([-dv[1], dv[0], 0.0]) * w / 2
        up = np.array([0, 0, h / 2])
        rings = [np.array([p - n - up, p + n - up, p + n + up, p - n + up]) for p in (a, b)]
        # a loft along the bar: its sides; then its ends
        k = 4
        P = np.r_[rings[0], rings[1]]
        T = []
        for i in range(k):
            j = (i + 1) % k
            T += [[i, j, j + k], [i, j + k, i + k]]
        T += [[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7]]
        self.add(P, T, cell_uv(cell, [0, 1, 1, 0, 0, 1, 1, 0], [0, 0, 1, 1, 0, 0, 1, 1]), two_sided=True)

    def tube(self, a, b, r, cell, seg=12):
        """A capped tube between two 3D points (any direction)."""
        a, b = np.asarray(a, np.float64), np.asarray(b, np.float64)
        ax = b - a
        L = np.linalg.norm(ax)
        ax /= L
        ref = np.array([0, 0, 1.0]) if abs(ax[2]) < 0.9 else np.array([1.0, 0, 0])
        e1 = np.cross(ax, ref)
        e1 /= np.linalg.norm(e1)
        e2 = np.cross(ax, e1)
        ang = np.linspace(0, 2 * math.pi, seg, endpoint=False)
        ring = np.outer(np.cos(ang), e1) * r + np.outer(np.sin(ang), e2) * r
        self.loft([a + ring, b + ring], cell, cap_bottom=True, cap_top=True)

    def out(self):
        return np.concatenate(self.P), np.concatenate(self.T), np.concatenate(self.UV)


def _pole(m, r, h, band=BAND, flange=True):
    """A signal pole as Rotterdam's: a flange, then black and white rings from just over the ground to its top, a
    domed cap."""
    if flange:
        m.lathe([(r + 0.07, 0.0), (r + 0.07, 0.02), (r + 0.01, 0.02), (r + 0.01, 0.05)], "grey", seg=16)
    m.lathe([(r, -0.05), (r, band[0])], "black", seg=16)
    z, seg_h = band[0], band[1] - band[0]              # (the stripes cell: 8 rings over seg_h; repeated up the pole)
    while z < h - 0.05:
        z1 = min(z + seg_h, h - 0.03)
        m.lathe([(r, z), (r, z1)], "stripes", seg=16, v_span=(0, (z1 - z) / seg_h))
        z = z1
    m.lathe([(r, h - 0.03), (r * 0.85, h + 0.01), (r * 0.45, h + 0.035), (0.001, h + 0.04)], "black", seg=16)


def _stadium(m, x, y0, y1, zc, w, h, cell, seg=10):
    """A Dutch signal head's body: its front a stadium (round at the top and the bottom, straight sides), from y0 back
    to y1 front, its edges softened (the back a little smaller)."""
    a, L = w / 2, max(h / 2 - w / 2, 0.0)
    t = np.r_[np.linspace(0, math.pi, seg, endpoint=False), np.linspace(math.pi, 2 * math.pi, seg, endpoint=False)]
    zs = np.where(t < math.pi, L, -L) + a * np.sin(t)
    xs = a * np.cos(t)
    rings = []
    for yy, k in ((y0, 0.9), (y0 + 0.025, 1.0), (y1 - 0.01, 1.0), (y1, 0.985)):
        # (counter-clockwise seen from +y, the loft's "up": seen from there x runs to the left)
        rings.append(np.c_[x - xs * k, np.full(len(t), yy), zc + zs * k])
    m.loft(rings, cell, cap_bottom=True, cap_top=True)


def _stadium_face(m, x, y, zc, w, h, cell, seg=12):
    """A flat stadium facing +y (the head's front, its rim, its plates)."""
    a, L = w / 2, max(h / 2 - w / 2, 0.0)
    t = np.r_[np.linspace(0, math.pi, seg, endpoint=False), np.linspace(math.pi, 2 * math.pi, seg, endpoint=False)]
    ring = np.c_[x - a * np.cos(t), np.full(len(t), y), zc + np.where(t < math.pi, L, -L) + a * np.sin(t)]
    k = len(ring)
    uv = np.c_[0.5 - 0.5 * np.cos(t), 0.5 + 0.5 * (np.where(t < math.pi, L, -L) + a * np.sin(t)) / (h / 2)]
    m.add(np.r_[[[x, y, zc]], ring], [[0, i + 1, (i + 1) % k + 1] for i in range(k)],
          cell_uv(cell, np.r_[0.5, uv[:, 0]], np.r_[0.5, uv[:, 1]]), two_sided=True)


def _head(m, y_back, zc, scale=1.0, x=0.0):
    """A Rotterdam three-lens head facing +y (its back at y_back, its middle at height zc): a black stadium body, the
    white contrast rim round its face, three lenses flush in it (no hoods), its number plate under the lowest."""
    w, d, h = (v * scale for v in HEAD)
    yf = y_back + d
    _stadium(m, x, y_back, yf, zc, w, h, "black")
    _stadium_face(m, x, yf + 0.001, zc, w, h, "white")                 # the rim
    _stadium_face(m, x, yf + 0.002, zc, w - 0.07 * scale, h - 0.07 * scale, "black")
    lr = LENS_R * scale
    pitch = (h - 0.07 * scale - 2 * lr - 0.12 * scale) / 2
    top = zc + (h - 0.07 * scale) / 2 - lr - 0.03 * scale
    for i, cell in enumerate(("lens_red", "lens_amber", "lens_green")):
        z = top - i * pitch
        m.annulus_y(x, yf + 0.005, z, lr, lr + 0.012 * scale, "black", depth=0.002)
        m.disc_y(x, yf + 0.004, z, lr, cell, bulge=0.006 * scale)
    zl = top - 2 * pitch - lr - 0.045 * scale
    m.add([(x + 0.035 * scale, yf + 0.004, zl - 0.018 * scale), (x - 0.035 * scale, yf + 0.004, zl - 0.018 * scale),
           (x - 0.035 * scale, yf + 0.004, zl + 0.018 * scale), (x + 0.035 * scale, yf + 0.004, zl + 0.018 * scale)],
          [[0, 2, 1], [0, 3, 2]], cell_uv("white", [0, 1, 1, 0], [0, 0, 1, 1]), two_sided=True)


def _plate_only(code):
    """A sign's plate (sign(code) without its post), its back at y = 0 and its top at z = 0."""
    P, T, UV = sign(code)
    keep_t = T[np.all(P[T][:, :, 1] > 0.0, axis=1)]
    used = np.unique(keep_t)
    remap = -np.ones(len(P), int)
    remap[used] = np.arange(len(used))
    Q = P[used].copy()
    Q[:, 1] -= Q[:, 1].min()
    Q[:, 2] -= Q[:, 2].max()
    return Q, remap[keep_t], UV[used]


def traffic_light(bike=False, button=True, plates=()):
    """The low signal: the ringed pole, the head at its top on its side facing +y, a cyclists' head lower down and the
    request button as options; plates: signs (codes) clamped to the same pole under the head, facing the same way
    (Rotterdam's give-way triangle under the light, Street View 2026-10-22)."""
    m = Mesh()
    z_top = POLE_H - HEAD[2] - 0.15
    for code in plates:
        Q, T_, UV_ = _plate_only(code)
        Q = Q + [0.0, POLE_R + 0.02, z_top]
        m.add(Q, T_, UV_)
        for zz in (z_top - 0.1, Q[:, 2].min() + 0.1):
            m.lathe([(POLE_R + 0.008, zz - 0.025), (POLE_R + 0.008, zz + 0.025)], "grey", seg=12, cap_top=True,
                    cap_bottom=True)
        z_top = Q[:, 2].min() - 0.08
    _pole(m, POLE_R, POLE_H)
    w, d, h = HEAD
    zc = POLE_H - h / 2 - 0.02
    _head(m, POLE_R + 0.03, zc)
    for z in (zc + h / 2 - 0.15, zc - h / 2 + 0.15):              # its two clamps
        m.lathe([(POLE_R + 0.008, z - 0.03), (POLE_R + 0.008, z + 0.03)], "black", seg=16, cap_top=True,
                cap_bottom=True)
        m.bar((0, 0.0, z), (0, POLE_R + 0.04, z), 0.06, 0.04, "black")
    if bike:
        _head(m, POLE_R + 0.03, 1.75, scale=0.55)
    if button:
        m.rounded_box((0, POLE_R + 0.055, 1.12), 0.13, 0.10, 0.27, 0.02, 0.012, "black")
        m.add([(0.05, POLE_R + 0.106, 1.04), (-0.05, POLE_R + 0.106, 1.04), (-0.05, POLE_R + 0.106, 1.20),
               (0.05, POLE_R + 0.106, 1.20)], [[0, 2, 1], [0, 3, 2]], cell_uv("button", [0, 1, 1, 0], [0, 0, 1, 1]),
              two_sided=True)
    return m.out()


def mast_arm(reach, heads=2, h=6.4, arm=1):
    """Rotterdam's mast with its arm over the lanes (toward +x: the drivers' left, the mast at their right): a light
    grey mast, the arm rising a little to its tip, a head on the arm over each lane (the arm behind its middle), and a
    low head on the mast's ringed foot."""
    m = Mesh()
    r = 0.12
    m.lathe([(r + 0.12, 0.0), (r + 0.12, 0.03), (r + 0.02, 0.03), (r + 0.02, 0.08)], "grey", seg=20)
    z, seg_h = 0.3, BAND[1] - BAND[0]
    while z < 3.3:                                                 # its foot ringed up to the low head
        z1 = min(z + seg_h, 3.3)
        m.lathe([(r, z), (r, z1)], "stripes", seg=20, v_span=(0, (z1 - z) / seg_h))
        z = z1
    m.lathe([(r, -0.05), (r, 0.3)], "black", seg=20)
    m.lathe([(r, 3.3), (r * 0.9, h - 0.1), (r * 0.85, h)], "grey", seg=20)
    m.lathe([(r * 0.85, h), (r * 0.5, h + 0.05), (0.001, h + 0.06)], "grey", seg=20)
    za = h - 0.45
    m.tube((0, 0, za), (reach, 0, za + 0.35), 0.075, "grey", seg=14)
    m.lathe([(0.07, 0), (0.001, 0.02)], "grey", x=reach, seg=12)   # (its end cap, roughly)
    for i in range(heads):
        x = reach * (i + 0.6) / heads
        zt = za + 0.35 * (x / reach)
        _head(m, 0.08, zt - 0.1, x=x)
        m.bar((x, 0.0, zt), (x, 0.09, zt), 0.08, 0.06, "black")
    _head(m, r + 0.03, 3.3 - HEAD[2] / 2 - 0.05)
    P, T, UV = m.out()
    if arm < 0:             # its arm toward the drivers' right (the mast on their left): mirrored, faces kept outward
        P = P * [-1, 1, 1]
        T = T[:, ::-1]
    return P, T, UV


def _plate_outline(shape, size):
    if shape == "disc":
        a = np.linspace(0, 2 * math.pi, 36, endpoint=False)
        return np.c_[size / 2 * np.cos(a), size / 2 * np.sin(a)]
    if shape == "square":
        return rrect(size, size, 0.03, n=3)
    hgt = size * math.sqrt(3) / 2         # point down, its corners rounded
    corners = np.array([[0, -hgt * 2 / 3], [size / 2, hgt / 3], [-size / 2, hgt / 3]])
    out = []
    rc = 0.045
    for i in range(3):
        p, a, b = corners[i], corners[i - 1], corners[(i + 1) % 3]
        da, db = (a - p) / np.linalg.norm(a - p), (b - p) / np.linalg.norm(b - p)
        bis = (da + db) / np.linalg.norm(da + db)
        half = math.acos(np.clip(np.dot(da, db), -1, 1)) / 2
        cen = p + bis * rc / math.sin(half)
        t0 = math.atan2(*(p + da * rc / math.tan(half) - cen)[::-1])
        t1 = math.atan2(*(p + db * rc / math.tan(half) - cen)[::-1])
        if t1 - t0 > math.pi:
            t1 -= 2 * math.pi
        if t0 - t1 > math.pi:
            t1 += 2 * math.pi
        for t in np.linspace(t0, t1, 5):
            out.append(cen + rc * np.array([math.cos(t), math.sin(t)]))
    ring = np.array(out)
    if np.sum(ring[:, 0] * np.roll(ring[:, 1], -1) - np.roll(ring[:, 0], -1) * ring[:, 1]) < 0:
        ring = ring[::-1]
    return ring


def sign(code):
    """A sign on its post: the plate's face toward +y, its back clamped to the post."""
    shape, size = PLATES[code]
    m = Mesh()
    ring = _plate_outline(shape, size)
    lo, hi = ring.min(0), ring.max(0)
    zc = SIGN_TOP - hi[1]
    py = SIGN_POST_R + 0.045                     # the plate's back, off the post's axis
    # the post and its cap
    m.lathe([(SIGN_POST_R, -0.05), (SIGN_POST_R, zc + hi[1] + 0.02), (SIGN_POST_R * 0.7, zc + hi[1] + 0.04),
             (0.001, zc + hi[1] + 0.045)], "grey", seg=12)
    # the plate: face, folded rim (a 2 cm lip back), back
    side = max(*(hi - lo))
    k = len(ring)
    u = 1 - (ring[:, 0] - (lo[0] + hi[0]) / 2 + side / 2) / side
    v = (ring[:, 1] - (lo[1] + hi[1]) / 2 + side / 2) / side
    mid = (lo[1] + hi[1]) / 2
    face = np.c_[ring[:, 0], np.full(k, py + 0.022), ring[:, 1] + zc]
    m.add(np.r_[[[0, py + 0.022, zc + mid]], face], [[0, (i + 1) % k + 1, i + 1] for i in range(k)],
          cell_uv(code, np.r_[0.5, u], np.r_[0.5, v]), two_sided=True)
    lip = np.c_[ring[:, 0], np.full(k, py), ring[:, 1] + zc]                      # the folded rim, 2 cm deep
    back = np.c_[ring[:, 0] * 0.94, np.full(k, py + 0.004), (ring[:, 1] - mid) * 0.94 + mid + zc]
    for A, B in ((face, lip), (lip, back)):
        P = np.r_[A, B]
        T = [[i, (i + 1) % k, (i + 1) % k + k] for i in range(k)] + [[i, (i + 1) % k + k, i + k] for i in range(k)]
        m.add(P, T, cell_uv("grey", np.r_[np.linspace(0, 1, k), np.linspace(0, 1, k)], [0.0] * k + [1.0] * k),
              two_sided=True)
    m.add(np.r_[[[0, py + 0.004, zc + mid]], back], [[0, i + 1, (i + 1) % k + 1] for i in range(k)],
          cell_uv("back", np.full(k + 1, 0.5), np.full(k + 1, 0.5)), two_sided=True)
    # two rails across its back and the clamps round the post
    for f in (0.28, 0.72):
        z = zc + lo[1] + (hi[1] - lo[1]) * f
        half = min(size * 0.38, np.ptp(ring[np.abs(ring[:, 1] + zc - z) < size * 0.2][:, 0]) / 2 * 0.85
                   if (np.abs(ring[:, 1] + zc - z) < size * 0.2).any() else size * 0.3)
        m.bar((-half, py - 0.012, z), (half, py - 0.012, z), 0.024, 0.04, "grey")
        m.lathe([(SIGN_POST_R + 0.008, z - 0.025), (SIGN_POST_R + 0.008, z + 0.025)], "grey", seg=12,
                cap_top=True, cap_bottom=True)
        m.bar((0, SIGN_POST_R, z), (0, py - 0.02, z), 0.03, 0.03, "grey")
    return m.out()


def gantry(span, h=6.6, signs=("route_havens", "route_centrum", "route_a13")):
    """A portal over a carriageway as the one on the Fortuynplein's east arm: two light grey tube posts (slightly
    tapered, on flanges over concrete footings) at x = 0 and x = span, one box beam at h held in a clamp on each, the
    route signs hung in front of it (+y) on hangers, the middle one standing over the beam, and a traffic light hung
    from the beam in the gap after the second."""
    m = Mesh()
    top = h + 0.75
    for x in (0.0, span):
        m.rounded_box((x, 0, 0.05), 1.0, 1.0, 0.3, 0.02, 0.01, "white")          # the footing (its top 0.2 up)
        m.lathe([(0.34, 0.2), (0.34, 0.24), (0.21, 0.24), (0.21, 0.6), (0.165, top), (0.12, top + 0.04),
                 (0.001, top + 0.06)], "grey", x=x, seg=24)
        m.rounded_box((x, 0, h), 0.55, 0.55, 0.9, 0.04, 0.02, "grey")             # the clamp
    m.rounded_box((span / 2, 0, h), span + 0.3, 0.32, 0.46, 0.03, 0.02, "grey")    # the beam
    # the signs: as wide as they are (ROUTE), side by side over the span, the light's gap kept
    widths = [ROUTE[n][0][0] for n in signs]
    gap = 0.9                                    # m between a sign and its neighbour (the light's: twice)
    total = sum(widths) + gap * (len(signs) + 2)
    sc = min(1.0, (span - 0.8) / total)
    x = (span - total * sc) / 2 + gap * sc
    yb = 0.16                                   # the beam's front
    for i, name in enumerate(signs):
        w, hh = (v * sc for v in ROUTE[name][0])
        xc = span - (x + w / 2)                 # (seen from +y the module's +x is on the left: the first sign there)
        z0 = h - 0.95 if hh < 3.0 else h - 1.5  # (the tall one stands over the beam)
        zc = z0 + hh / 2
        yf = yb + 0.28
        m.rounded_box((xc, yf - 0.03, zc), w, 0.05, hh, 0.04, 0.01, "back")
        m.add([(xc - w / 2 + 0.01, yf + 0.0, z0 + 0.01), (xc + w / 2 - 0.01, yf + 0.0, z0 + 0.01),
               (xc + w / 2 - 0.01, yf + 0.0, z0 + hh - 0.01), (xc - w / 2 + 0.01, yf + 0.0, z0 + hh - 0.01)],
              [[0, 2, 1], [0, 3, 2]], cell_uv(name, [1, 0, 0, 1], [0, 0, 1, 1]))
        for hx in (xc - w * 0.32, xc + w * 0.32):   # its hangers: two uprights clamped to the beam
            m.bar((hx, yb, h + 0.18), (hx, yf - 0.06, h + 0.18), 0.06, 0.08, "grey")
            m.tube((hx, yf - 0.08, z0 + 0.2), (hx, yf - 0.08, z0 + hh - 0.15), 0.035, "grey", seg=8)
        if i == 1:
            lx = span - (x + w + gap * sc)
        x += w + gap * sc * (2 if i == 1 else 1)
    # the light hung from the beam in the gap after the middle sign, facing the traffic; a street light on the
    # left post's top (Street View 2026-10-22, the east arm)
    m.bar((lx, yb - 0.1, h - 0.15), (lx, yb + 0.12, h - 0.15), 0.08, 0.1, "black")
    _head(m, yb + 0.1, h - 0.35 - HEAD[2] / 2, x=lx)
    m.tube((lx, yb + 0.2, h - 0.15), (lx, yb + 0.2, h - 0.35), 0.03, "black", seg=8)
    L = lamp_post(h=1.6, arm=1.4)
    LP = L[0].copy()
    LP[:, :2] = LP[:, [1, 0]] * [1, 1]          # (its arm out over the outside, +x, away from the road)
    LP[:, 0] += span
    LP[:, 2] += top - 0.1
    m.add(LP, L[1][:, ::-1], L[2])
    return m.out()


def _plate(m, name, xc, yf, z0, w, hh, back="back"):
    """A route sign facing +y: its body (5 cm, rounded) and its face, the cell's whole face on it."""
    m.rounded_box((xc, yf - 0.03, z0 + hh / 2), w, 0.05, hh, 0.03, 0.01, back)
    m.add([(xc + w / 2 - 0.01, yf, z0 + 0.01), (xc - w / 2 + 0.01, yf, z0 + 0.01),
           (xc - w / 2 + 0.01, yf, z0 + hh - 0.01), (xc + w / 2 - 0.01, yf, z0 + hh - 0.01)],
          [[0, 1, 2], [0, 2, 3]], cell_uv(name, [0, 1, 1, 0], [0, 0, 1, 1]))     # (facing +y: the game culls back faces)


def cantilever_sign(left, right, reach_left, reach_right, h=6.2):
    """A sign mast on a median as on the 's-Gravendijkwal (Street View B221): a light grey mast, a box arm across at h
    reaching reach_left (to the drivers' left, +x) and reach_right, a route sign hung over each way facing +y."""
    m = Mesh()
    top = h + 0.6
    m.rounded_box((0, 0, 0.05), 0.9, 0.9, 0.3, 0.02, 0.01, "white")
    m.lathe([(0.3, 0.2), (0.3, 0.24), (0.19, 0.24), (0.19, 0.6), (0.15, top), (0.1, top + 0.04),
             (0.001, top + 0.06)], "grey", seg=20)
    m.rounded_box(((reach_left - reach_right) / 2, 0, h), reach_left + reach_right, 0.3, 0.4, 0.03, 0.02, "grey")
    for name, xc in ((left, reach_left - ROUTE[left][0][0] / 2 - 0.3),
                     (right, -(reach_right - ROUTE[right][0][0] / 2 - 0.3))):
        w, hh = ROUTE[name][0]
        z0 = h + 0.2 - hh             # (hung from the arm: its top a little over it, Street View B221)
        _plate(m, name, xc, 0.32, z0, w, hh)
        for hx in (xc - w * 0.3, xc + w * 0.3):     # its clamps onto the arm
            m.bar((hx, 0.15, h), (hx, 0.29, h), 0.08, 0.12, "grey")
    return m.out()


def nose_bollard(code="D2", h=1.25):
    """A median nose's sign as the Dutch ones: a yellow and black reflector post, the round blue sign on top facing +y
    (Street View B221)."""
    m = Mesh()
    m.rounded_box((0, 0, h / 2), 0.2, 0.2, h, 0.06, 0.02, "button")
    m.rounded_box((0, 0, 0.05), 0.24, 0.24, 0.1, 0.06, 0.02, "black")
    m.lathe([(0.03, h), (0.03, h + 0.35)], "grey", seg=8)
    n0 = len(m.P)
    m.plate("disc", 0.5, h + 0.55, cell=code) if hasattr(m, "plate") else None
    if len(m.P) == n0:                         # (the plate built as the signs' own: the disc on its face)
        P, T, UV = sign(code)
        lo = P[:, 2].max() - 0.75
        keep = np.flatnonzero(P[:, 2] > lo)
        tri = T[np.all(np.isin(T, keep), axis=1)]
        remap = -np.ones(len(P), int)
        remap[keep] = np.arange(len(keep))
        Q = P[keep].copy()
        Q[:, 2] += (h + 0.8) - Q[:, 2].max()
        m.add(Q, remap[tri], UV[keep])
    return m.out()


def cantilever_sign_single(name, h=5.2):
    """A route sign on its own light grey post beside the road, the plate's middle over the post (Street View C250)."""
    m = Mesh()
    w, hh = ROUTE[name][0]
    m.lathe([(0.25, 0.0), (0.25, 0.04), (0.12, 0.04), (0.1, h)], "grey", seg=14)
    _plate(m, name, 0.0, 0.16, h - hh, w, hh)
    return m.out()


def finger_post(fingers, h=4.6):
    """A blue and white ringed post with route fingers stacked on it (Street View B130): each finger an arrow-ended
    plate pointing to the drivers' right (-x), its face toward +y."""
    m = Mesh()
    r = 0.055
    m.lathe([(r + 0.06, 0.0), (r + 0.06, 0.02), (r, 0.02), (r, 0.4)], "black", seg=14)
    z = 0.4
    while z < h - 0.05:                          # (the rings: the stripes cell, blue here by its own tint is not had:
        z1 = min(z + (BAND[1] - BAND[0]), h)     # the black and white band stands in below the fingers)
        m.lathe([(r, z), (r, z1)], "stripes", seg=14, v_span=(0, (z1 - z) / (BAND[1] - BAND[0])))
        z = z1
    m.lathe([(r, h), (0.001, h + 0.04)], "black", seg=14)
    for i, name in enumerate(fingers):
        w, hh = ROUTE[name][0]
        z0 = h - 0.15 - (i + 1) * (hh + 0.12)
        xc = -w / 2 + 0.05
        _plate(m, name, xc, r + 0.05, z0, w, hh)
        tip = [(-w + 0.05, r + 0.05 + 0.001, z0), (-w - 0.25, r + 0.05 + 0.001, z0 + hh / 2),
               (-w + 0.05, r + 0.05 + 0.001, z0 + hh)]
        m.add(tip, [[0, 1, 2]], cell_uv("route", [0.5, 0.5, 0.5], [0.5, 0.5, 0.5]), two_sided=True)
    return m.out()


def route_pole(plates, h=3.2):
    """A galvanised post with small route plates stacked on it, faces toward +y (Street View A269, B20)."""
    m = Mesh()
    m.lathe([(SIGN_POST_R, -0.05), (SIGN_POST_R, h), (0.001, h + 0.03)], "grey", seg=10)
    z = h - 0.1
    for name in plates:
        w, hh = ROUTE[name][0]
        z -= hh + 0.05
        _plate(m, name, 0.0, SIGN_POST_R + 0.06, z, w, hh)
    return m.out()


def lamp_post(h=8.0, arm=1.6):
    """A street light as the junction's: a grey tapered pole, an arm out toward +y at its top, a flat luminaire."""
    m = Mesh()
    m.lathe([(0.13, 0.0), (0.13, 0.03), (0.085, 0.03), (0.085, 0.6), (0.06, h - 0.2), (0.05, h)], "grey", seg=12)
    m.lathe([(0.05, h), (0.03, h + 0.04), (0.001, h + 0.05)], "grey", seg=12)
    m.tube((0, 0, h - 0.25), (0, arm * 0.4, h + 0.05), 0.035, "grey", seg=8)
    m.tube((0, arm * 0.4, h + 0.05), (0, arm, h + 0.1), 0.03, "grey", seg=8)
    m.rounded_box((0, arm + 0.25, h + 0.08), 0.28, 0.6, 0.09, 0.08, 0.03, "grey")
    m.add([(0.11, arm + 0.02, h + 0.033), (-0.11, arm + 0.02, h + 0.033), (-0.11, arm + 0.5, h + 0.033),
           (0.11, arm + 0.5, h + 0.033)], [[0, 1, 2], [0, 2, 3]], cell_uv("white", [0, 1, 1, 0], [0, 0, 1, 1]),
          two_sided=True)
    return m.out()


def tram_pole(reach=2.5, h=7.5):
    """A tram line's mast: a grey pole and its cantilever out toward +y over the track (the wire left out)."""
    m = Mesh()
    m.lathe([(0.16, 0.0), (0.16, 0.03), (0.1, 0.03), (0.1, 0.5), (0.08, h)], "grey", seg=12)
    m.lathe([(0.08, h), (0.001, h + 0.04)], "grey", seg=12)
    m.tube((0, 0, h - 0.9), (0, reach, h - 0.75), 0.03, "grey", seg=8)
    m.tube((0, 0, h - 0.2), (0, reach * 0.8, h - 0.75), 0.02, "grey", seg=6)
    return m.out()


def place(module, at, facing, z):
    """The module's (P, T, UV) set at `at` (x, y) on the ground at z, its face (+y) looking along `facing` (x, y)."""
    P, T, UV = module
    f = np.asarray(facing, np.float64)
    f = f / max(np.hypot(*f), 1e-9)
    c, s = f[1], -f[0]
    R = np.array([[c, -s], [s, c]])
    xy = P[:, :2] @ R.T + np.asarray(at, np.float64)
    return np.c_[xy, P[:, 2] + z], T, UV
