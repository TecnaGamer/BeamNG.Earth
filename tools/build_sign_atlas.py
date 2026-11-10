#!/usr/bin/env python3
"""The road sign atlases (task 017): every role of server/data/sign_roles.json drawn to its cell, from vectors, so the
geometry, colours and symbols are exact and the artwork is ours (no third party's picture goes in).

    python tools/build_sign_atlas.py            writes mod/.../art/shapes/beamngEarth/bne_signs_<system>.color.png
    python tools/build_sign_atlas.py --check    fails if a role has no drawing or a cell falls outside the sheet

A cell is the unit square [-1, 1]^2 with y up. The sign's outline is cut by the mesh (native roadsigns.rs: outline(),
the same corner rounding as OUTLINES here), so the sheet is opaque: outside the outline a cell is its rim's colour and
a mip level cannot bleed a neighbour in. The pictograms follow the Vienna Convention's designs, simplified to what
reads at 256 px.
"""
import argparse
import json
import math
import os
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROLES = os.path.join(ROOT, "server", "data", "sign_roles.json")
OUT = os.path.join(ROOT, "mod", "beamng_earth", "levels", "beamng_earth", "art", "shapes", "beamngEarth")
SS = 4                          # supersampling

RED, BLUE, BLACK, WHITE = (196, 18, 32), (0, 72, 150), (24, 24, 24), (244, 244, 240)
YELLOW, GREEN, GREY, AMBER = (250, 200, 10), (0, 140, 70), (120, 120, 120), (240, 160, 0)
TRI_TOP, TRI_BOTTOM = 0.90, -0.83       # the triangles' apex and base (native TRI_*)
CORNER = 0.12                   # the triangles' and diamond's corner radius in cell units


def rounded(verts, r, steps=4):
    """A convex polygon's corners rounded by r: the outline shared with the mesh (native round_poly)."""
    out = []
    n = len(verts)
    for i in range(n):
        p, a, b = verts[i - 1], verts[i], verts[(i + 1) % n]
        u = (p[0] - a[0], p[1] - a[1])
        v = (b[0] - a[0], b[1] - a[1])
        lu, lv = math.hypot(*u), math.hypot(*v)
        u, v = (u[0] / lu, u[1] / lu), (v[0] / lv, v[1] / lv)
        half = math.acos(max(-1.0, min(1.0, u[0] * v[0] + u[1] * v[1]))) / 2.0
        d = r / math.tan(half)
        s = (a[0] + u[0] * d, a[1] + u[1] * d)
        e = (a[0] + v[0] * d, a[1] + v[1] * d)
        bis = ((u[0] + v[0]), (u[1] + v[1]))
        lb = math.hypot(*bis)
        c = (a[0] + bis[0] / lb * r / math.sin(half), a[1] + bis[1] / lb * r / math.sin(half))
        a0 = math.atan2(s[1] - c[1], s[0] - c[0])
        a1 = math.atan2(e[1] - c[1], e[0] - c[0])
        da = (a1 - a0 + math.pi) % (2 * math.pi) - math.pi
        for k in range(steps + 1):
            t = a0 + da * k / steps
            out.append((c[0] + r * math.cos(t), c[1] + r * math.sin(t)))
    return out


def tri_up():
    return [(0.0, TRI_TOP), (-1.0, TRI_BOTTOM), (1.0, TRI_BOTTOM)]


def tri_down():
    return [(0.0, -TRI_TOP), (1.0, -TRI_BOTTOM), (-1.0, -TRI_BOTTOM)]


def diamond():
    return [(0.0, 0.98), (-0.98, 0.0), (0.0, -0.98), (0.98, 0.0)]


def octagon():
    t = 0.98 * math.tan(math.pi / 8)
    return [(t, .98), (-t, .98), (-.98, t), (-.98, -t), (-t, -.98), (t, -.98), (.98, -t), (.98, t)]


def square():
    return [(.97, .97), (-.97, .97), (-.97, -.97), (.97, -.97)]


class Pen:
    """Vector drawing in cell units on a supersampled cell."""
    def __init__(self, px, bg):
        self.px = px * SS
        self.img = Image.new("RGB", (self.px, self.px), bg)
        self.d = ImageDraw.Draw(self.img)

    def xy(self, p):
        return ((p[0] + 1.0) * self.px / 2.0, (1.0 - p[1]) * self.px / 2.0)

    def poly(self, pts, col):
        self.d.polygon([self.xy(p) for p in pts], fill=col)

    def disc(self, c, r, col):
        x, y = self.xy(c)
        q = r * self.px / 2.0
        self.d.ellipse((x - q, y - q, x + q, y + q), fill=col)

    def line(self, pts, w, col, round_ends=True):
        """A polyline of width w, its joints round."""
        for a, b in zip(pts[:-1], pts[1:]):
            dx, dy = b[0] - a[0], b[1] - a[1]
            l = max(math.hypot(dx, dy), 1e-9)
            nx, ny = -dy / l * w / 2, dx / l * w / 2
            self.poly([(a[0] + nx, a[1] + ny), (b[0] + nx, b[1] + ny), (b[0] - nx, b[1] - ny), (a[0] - nx, a[1] - ny)], col)
            if round_ends:
                self.disc(a, w / 2, col)
                self.disc(b, w / 2, col)

    def arrow(self, pts, w, col, head=2.3):
        """A polyline ending in an arrowhead."""
        a, b = pts[-2], pts[-1]
        dx, dy = b[0] - a[0], b[1] - a[1]
        l = max(math.hypot(dx, dy), 1e-9)
        ux, uy = dx / l, dy / l
        hl, hw = w * head * .95, w * head / 1.25
        base = (b[0] - ux * hl, b[1] - uy * hl)
        self.line(pts[:-1] + [base], w, col, round_ends=False)
        self.poly([b, (base[0] - uy * hw, base[1] + ux * hw), (base[0] + uy * hw, base[1] - ux * hw)], col)

    def text(self, c, h, txt, col, width=None):
        """txt centred at c, h cell units high (capitals), squeezed to `width` cell units when given."""
        size = max(int(h * self.px / 2.0 / 0.72), 8)
        font = None
        for name in ("arialbd.ttf", "DejaVuSans-Bold.ttf", "Arial Bold.ttf"):
            try:
                font = ImageFont.truetype(name, size)
                break
            except OSError:
                continue
        font = font or ImageFont.load_default()
        l, t, r, b = self.d.textbbox((0, 0), txt, font=font)
        w, hh = r - l, b - t
        layer = Image.new("L", (w + 4, hh + 4), 0)
        ImageDraw.Draw(layer).text((2 - l, 2 - t), txt, font=font, fill=255)
        if width:
            layer = layer.resize((max(int(width * self.px / 2.0), 1), layer.size[1]), Image.LANCZOS)
        x, y = self.xy(c)
        self.img.paste(Image.new("RGB", layer.size, col), (int(x - layer.size[0] / 2), int(y - layer.size[1] / 2)), layer)

    def done(self, px):
        return self.img.resize((px, px), Image.LANCZOS)


def scaled(pts, k, cx=0.0, cy=0.0):
    return [(cx + (x - cx) * k, cy + (y - cy) * k) for x, y in pts]


def inset(pts, k, c=(0.0, 0.0)):
    return [(c[0] + (x - c[0]) * k, c[1] + (y - c[1]) * k) for x, y in pts]


# ---- pictograms (each draws in a box about [-1, 1]^2, scaled by the place() caller) ----

def place(fn, k=1.0, x=0.0, y=0.0, mirror=False):
    """A pictogram function's points through a scale, a shift and an optional mirror."""
    def tr(p):
        return ((-p[0] if mirror else p[0]) * k + x, p[1] * k + y)
    return tr


def walker(pen, col, tr, stride=1.0):
    """A walking figure."""
    w = lambda *pts: [tr(p) for p in pts]       # noqa: E731
    k = abs(tr((1, 0))[0] - tr((0, 0))[0])
    pen.disc(tr((0.04, 0.80)), 0.17 * k, col)
    pen.line(w((0.02, 0.56), (-0.04, 0.02)), 0.26 * k, col)
    pen.line(w((0.02, 0.50), (-0.30, 0.22), (-0.42, -0.04)), 0.11 * k, col)
    pen.line(w((0.05, 0.50), (0.32, 0.26), (0.48, 0.10)), 0.11 * k, col)
    pen.line(w((-0.04, 0.06), (-0.22 * stride, -0.36), (-0.46 * stride, -0.80)), 0.15 * k, col)
    pen.line(w((-0.02, 0.06), (0.22 * stride, -0.34), (0.32 * stride, -0.80)), 0.15 * k, col)
    pen.line(w((0.32 * stride, -0.80), (0.50 * stride, -0.82)), 0.10 * k, col)
    pen.line(w((-0.46 * stride, -0.80), (-0.28 * stride, -0.84)), 0.10 * k, col)


def bike(pen, col, tr, rider=False):
    """A bicycle, with or without a rider."""
    k = abs(tr((1, 0))[0] - tr((0, 0))[0])
    w = lambda *pts: [tr(p) for p in pts]       # noqa: E731
    bg = pen.bg if hasattr(pen, "bg") else None
    for c in ((-0.56, -0.34), (0.56, -0.34)):
        q = tr(c)
        pen.disc(q, 0.36 * k, col)
        pen.disc(q, 0.27 * k, pen.inner)
    pen.line(w((-0.56, -0.34), (-0.12, 0.20), (0.40, 0.20), (0.56, -0.34)), 0.075 * k, col)
    pen.line(w((-0.56, -0.34), (0.0, -0.34), (0.40, 0.20)), 0.075 * k, col)
    pen.line(w((-0.12, 0.20), (0.0, -0.34)), 0.075 * k, col)
    pen.line(w((0.34, 0.40), (0.40, 0.20)), 0.075 * k, col)
    pen.line(w((0.22, 0.42), (0.46, 0.42)), 0.08 * k, col)
    pen.line(w((-0.24, 0.26), (-0.02, 0.26)), 0.09 * k, col)
    if rider:
        pen.disc(tr((0.12, 0.86)), 0.15 * k, col)
        pen.line(w((-0.12, 0.28), (0.04, 0.66)), 0.20 * k, col)
        pen.line(w((0.04, 0.62), (0.34, 0.44)), 0.09 * k, col)
        pen.line(w((-0.08, 0.26), (0.20, 0.06), (0.0, -0.32)), 0.11 * k, col)


def deer(pen, col, tr):
    """A leaping deer."""
    k = abs(tr((1, 0))[0] - tr((0, 0))[0])
    pen.poly([tr(p) for p in [(-0.86, -0.02), (-0.72, 0.16), (-0.30, 0.20), (0.14, 0.14), (0.36, 0.34), (0.42, 0.62),
                              (0.58, 0.60), (0.80, 0.48), (0.82, 0.40), (0.60, 0.40), (0.56, 0.20), (0.40, -0.08),
                              (0.22, -0.16), (-0.16, -0.14), (-0.52, -0.16)]], col)
    w = lambda *pts: [tr(p) for p in pts]       # noqa: E731
    pen.line(w((0.30, -0.08), (0.56, -0.34), (0.84, -0.30)), 0.08 * k, col)
    pen.line(w((0.16, -0.10), (0.32, -0.46), (0.52, -0.62)), 0.08 * k, col)
    pen.line(w((-0.52, -0.08), (-0.78, -0.38), (-0.98, -0.66)), 0.09 * k, col)
    pen.line(w((-0.42, -0.10), (-0.56, -0.48), (-0.72, -0.80)), 0.09 * k, col)
    pen.line(w((0.46, 0.60), (0.36, 0.86), (0.20, 0.96)), 0.05 * k, col)
    pen.line(w((0.40, 0.74), (0.52, 0.92)), 0.05 * k, col)
    pen.line(w((0.54, 0.60), (0.56, 0.82), (0.44, 1.0)), 0.05 * k, col)


def car_rear(pen, col, tr):
    k = abs(tr((1, 0))[0] - tr((0, 0))[0])
    pen.poly([tr(p) for p in [(-0.62, -0.2), (-0.62, 0.06), (-0.40, 0.56), (0.40, 0.56), (0.62, 0.06), (0.62, -0.2)]], col)
    pen.poly([tr(p) for p in [(-0.34, 0.14), (-0.22, 0.46), (0.22, 0.46), (0.34, 0.14)]], pen.inner)
    pen.line([tr((-0.46, -0.24)), tr((-0.46, -0.40))], 0.22 * k, col, round_ends=False)
    pen.line([tr((0.46, -0.24)), tr((0.46, -0.40))], 0.22 * k, col, round_ends=False)


def worker(pen, col, tr):
    """A man digging: the roadworks sign."""
    k = abs(tr((1, 0))[0] - tr((0, 0))[0])
    w = lambda *pts: [tr(p) for p in pts]       # noqa: E731
    pen.disc(tr((-0.10, 0.74)), 0.17 * k, col)
    pen.line(w((-0.18, 0.50), (-0.40, 0.02)), 0.26 * k, col)
    pen.line(w((-0.38, 0.0), (-0.20, -0.40), (-0.26, -0.82)), 0.15 * k, col)
    pen.line(w((-0.42, 0.0), (-0.66, -0.40), (-0.80, -0.82)), 0.15 * k, col)
    pen.line(w((-0.16, 0.44), (0.20, 0.20), (0.38, 0.10)), 0.11 * k, col)
    pen.line(w((-0.02, 0.60), (0.56, -0.40)), 0.06 * k, col)
    pen.poly([tr(p) for p in [(0.50, -0.34), (0.70, -0.40), (0.68, -0.62), (0.46, -0.56)]], col)
    pen.poly([tr(p) for p in [(0.30, -0.84), (0.60, -0.56), (0.92, -0.84)]], col)


def child(pen, col, tr):
    walker(pen, col, tr, 0.8)


# ---- frames ----

def ring_sign(pen, hole=WHITE, ring=RED, band=0.20):
    """The prohibitory disc: white rim, red ring, a field."""
    pen.disc((0, 0), 0.99, WHITE)
    pen.disc((0, 0), 0.95, ring)
    pen.disc((0, 0), 0.95 - band, hole)
    pen.inner = hole


def slash(pen, a=-45.0, col=RED, w=0.18, r=0.76):
    c, s = math.cos(math.radians(a)), math.sin(math.radians(a))
    pen.line([(-c * r, s * -r * -1 * -1), (c * r, s * r)], w, col, round_ends=False)


def blue_disc(pen):
    pen.disc((0, 0), 0.99, WHITE)
    pen.disc((0, 0), 0.95, BLUE)
    pen.inner = BLUE


def warn_frame(pen, field):
    """The warning triangle: white rim, red band, field (the system's)."""
    outer = rounded(tri_up(), CORNER)
    pen.poly(outer, WHITE)
    c = (0.0, (TRI_TOP + 2 * TRI_BOTTOM) / 3.0)
    pen.poly(rounded(inset(tri_up(), 0.955, c), CORNER * 0.9), RED)
    pen.poly(rounded(inset(tri_up(), 0.70, c), CORNER * 0.55), field)
    pen.inner = field


def blue_square(pen, field=BLUE):
    pen.poly(rounded(square(), 0.10), WHITE)
    pen.poly(rounded(inset(square(), 0.955), 0.08), field)
    pen.inner = field


def white_panel(pen):
    """A white square with a black inner line (living street, pedestrian zone...: the Vienna E signs)."""
    pen.poly(rounded(square(), 0.10), BLACK)
    pen.poly(rounded(inset(square(), 0.955), 0.08), WHITE)
    pen.inner = WHITE


def clip_rect(pts, x0, x1, y0, y1):
    """A polygon clipped to the rectangle (Sutherland-Hodgman)."""
    def cut(poly, inside, cross):
        out = []
        for a, b in zip(poly, poly[1:] + poly[:1]):
            if inside(a):
                out.append(a)
                if not inside(b):
                    out.append(cross(a, b))
            elif inside(b):
                out.append(cross(a, b))
        return out
    at_x = lambda x: (lambda a, b: (x, a[1] + (b[1] - a[1]) * (x - a[0]) / (b[0] - a[0])))       # noqa: E731
    at_y = lambda y: (lambda a, b: (a[0] + (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]), y))       # noqa: E731
    for inside, cross in ((lambda q: q[0] >= x0, at_x(x0)), (lambda q: q[0] <= x1, at_x(x1)),
                          (lambda q: q[1] >= y0, at_y(y0)), (lambda q: q[1] <= y1, at_y(y1))):
        pts = cut(pts, inside, cross)
        if not pts:
            return []
    return pts


# ---- the roles ----

def W(draw):
    """A warning sign: the field colour comes from the system."""
    def go(pen, field):
        warn_frame(pen, field)
        draw(pen)
    go.warning = True
    return go


Y = -0.28                       # the centre of a warning triangle's field


def curve(mirror):
    def d(p):
        s = -1 if mirror else 1
        p.arrow([(-0.16 * s, -0.48), (-0.16 * s, -0.10), (0.20 * s, 0.26)], 0.135, BLACK, 2.0)
    return d


def double(mirror):
    def d(p):
        s = -1 if mirror else 1
        p.arrow([(-0.16 * s, -0.50), (-0.16 * s, -0.32), (0.16 * s, -0.12), (0.16 * s, 0.06), (-0.14 * s, 0.28)], 0.125, BLACK, 2.0)
    return d


def side_road(side):
    def d(p):
        p.line([(0, -0.52), (0, 0.36)], 0.17, BLACK, round_ends=False)
        p.line([(0, -0.14), (0.40 * side, -0.14)], 0.13, BLACK, round_ends=False)
    return d


def crossroads(p):
    p.line([(0, -0.50), (0, 0.32)], 0.15, BLACK, round_ends=False)
    p.line([(-0.40, -0.14), (0.40, -0.14)], 0.15, BLACK, round_ends=False)


def w_round(p):
    for a in (90, 210, 330):
        r = 0.30
        t = [math.radians(a + d) for d in (0, 40, 80)]
        pts = [(r * math.cos(x), Y + r * math.sin(x)) for x in t]
        p.arrow(pts, 0.095, BLACK, 1.9)


def signals(p):
    p.poly([(-0.17, 0.30), (0.17, 0.30), (0.17, -0.56), (-0.17, -0.56)], BLACK)
    for y, c in ((0.15, RED), (-0.13, AMBER), (-0.41, GREEN)):
        p.disc((0, y), 0.105, c)


def gate(p):
    p.poly([(-0.46, -0.56), (0.46, -0.56), (0.46, -0.44), (-0.46, -0.44)], BLACK)
    for x in range(-4, 5):
        pass
    p.poly([(-0.42, -0.42), (-0.42, -0.26), (0.42, -0.26), (0.42, -0.42)], BLACK)
    p.line([(-0.30, -0.16), (0.34, 0.24)], 0.09, BLACK, round_ends=False)
    for t in (0.2, 0.5, 0.8):
        x, y = -0.30 + 0.64 * t, -0.16 + 0.40 * t
        p.poly([(x - .04, y - .03), (x + .04, y + .03), (x + .04, y - .13), (x - .04, y - .16)], p.inner)
    p.line([(-0.36, -0.26), (-0.36, -0.10)], 0.09, BLACK, round_ends=False)


def steam(p):
    p.poly([(-0.46, -0.50), (0.42, -0.50), (0.42, -0.40), (-0.46, -0.40)], BLACK)
    p.poly([(-0.40, -0.36), (0.08, -0.36), (0.08, -0.04), (-0.40, -0.04)], BLACK)
    p.poly([(0.10, -0.36), (0.40, -0.36), (0.40, 0.14), (0.10, 0.14)], BLACK)
    p.poly([(-0.34, -0.02), (-0.20, -0.02), (-0.22, 0.18), (-0.32, 0.18)], BLACK)
    for x in (-0.28, -0.04, 0.26):
        p.disc((x, -0.44), 0.09, p.inner)
        p.disc((x, -0.44), 0.06, BLACK)
    p.disc((-0.14, 0.26), 0.06, BLACK)
    p.disc((-0.02, 0.32), 0.05, BLACK)


def steep(up):
    def d(p):
        pts = [(-0.46, -0.50), (0.46, -0.50), (0.46, -0.04)] if up else [(-0.46, -0.04), (-0.46, -0.50), (0.46, -0.50)]
        p.poly(pts, BLACK)
    return d


def narrow(left, right):
    def d(p):
        for s, on in ((-1, left), (1, right)):
            bend = 0.11 if on else 0.21
            p.line([(s * 0.21, -0.54), (s * 0.21, -0.24), (s * bend, -0.04), (s * bend, 0.34)], 0.12, BLACK, round_ends=False)
    return d


def bump(p):
    p.poly([(-0.48, -0.52), (0.48, -0.52), (0.48, -0.40), (0.24, -0.40), (0.12, -0.20), (0, -0.14), (-0.12, -0.20),
            (-0.24, -0.40), (-0.48, -0.40)], BLACK)


def slippery(p):
    car_rear(p, BLACK, lambda q: (q[0] * 0.52, q[1] * 0.52 - 0.02))
    p.line([(-0.30, -0.36), (-0.14, -0.26), (-0.32, -0.08 - 0.10), (-0.20, -0.52)], 0.05, BLACK, round_ends=False)
    p.line([(0.20, -0.36), (0.32, -0.26), (0.18, -0.16), (0.32, -0.52)], 0.05, BLACK, round_ends=False)


def danger(p):
    p.poly([(-0.08, 0.30), (0.08, 0.30), (0.05, -0.28), (-0.05, -0.28)], BLACK)
    p.disc((0, -0.45), 0.09, BLACK)


def on_tri(fn, *a, k=0.44):
    def d(p):
        fn(p, BLACK, lambda q: (q[0] * k, q[1] * k + Y + 0.02), *a)
    return d


def children(p):
    child(p, BLACK, lambda q: (q[0] * 0.36 - 0.16, q[1] * 0.36 - 0.17))
    child(p, BLACK, lambda q: (q[0] * 0.28 + 0.21, q[1] * 0.28 - 0.25))


def ring(fn):
    def go(pen, _f):
        ring_sign(pen)
        fn(pen)
    return go


def disc(fn):
    def go(pen, _f):
        blue_disc(pen)
        fn(pen)
    return go


def blue(fn):
    def go(pen, _f):
        blue_square(pen)
        fn(pen)
    return go


def panel(fn):
    def go(pen, _f):
        white_panel(pen)
        fn(pen)
    return go


def no_entry(pen, _f):
    pen.disc((0, 0), 0.99, WHITE)
    pen.disc((0, 0), 0.95, RED)
    pen.poly([(-0.70, 0.17), (0.70, 0.17), (0.70, -0.17), (-0.70, -0.17)], WHITE)


def main_road(end):
    def go(pen, _f):
        pen.poly(rounded(diamond(), CORNER), BLACK)
        pen.poly(rounded(inset(diamond(), 0.965), CORNER * 0.9), WHITE)
        pen.poly(rounded(inset(diamond(), 0.60), CORNER * 0.5), YELLOW)
        pen.inner = WHITE
        if end:
            for k in (-0.34, -0.12, 0.10, 0.32):
                pen.line([(k - 0.42, -0.52 + 0.0), (k + 0.42, 0.52)], 0.055, BLACK, round_ends=False)
    return go


def give_way(pen, _f):
    outer = rounded(tri_down(), CORNER)
    pen.poly(outer, WHITE)
    c = (0.0, -(TRI_TOP + 2 * TRI_BOTTOM) / 3.0)
    pen.poly(rounded(inset(tri_down(), 0.955, c), CORNER * 0.9), RED)
    pen.poly(rounded(inset(tri_down(), 0.62, c), CORNER * 0.5), WHITE)


def stop(pen, _f):
    pen.poly(octagon(), WHITE)
    pen.poly(inset(octagon(), 0.95), RED)
    # STOP from strokes: no font on the build machine has to be the same
    h, y0, sw = 0.46, -0.23, 0.095
    pen.line([(-.56, y0 + h), (-.76, y0 + h), (-.76, y0 + h / 2), (-.56, y0 + h / 2), (-.56, y0), (-.76, y0)], sw, WHITE, False)
    pen.line([(-.46, y0 + h), (-.16, y0 + h)], sw, WHITE, False)
    pen.line([(-.31, y0 + h), (-.31, y0 - sw / 2)], sw, WHITE, False)
    pen.line([(-.03, y0), (-.03, y0 + h), (.21, y0 + h), (.21, y0), (-.03 - sw / 2, y0)], sw, WHITE, False)
    pen.line([(.36, y0 - sw / 2), (.36, y0 + h), (.60, y0 + h), (.60, y0 + h / 2 - .02), (.36, y0 + h / 2 - .02)], sw, WHITE, False)


def priority_oncoming(pen, _f):
    ring_sign(pen)
    pen.arrow([(0.26, -0.56), (0.26, 0.60)], 0.15, BLACK)
    pen.arrow([(-0.26, 0.56), (-0.26, -0.60)], 0.15, RED)


def priority_over(pen, _f):
    blue_square(pen)
    pen.arrow([(-0.28, -0.70), (-0.28, 0.72)], 0.17, WHITE)
    pen.arrow([(0.28, 0.70), (0.28, -0.72)], 0.17, RED)


def car_pair(p, a=BLACK, b=RED):
    """The overtaking sign's two cars, seen from behind."""
    for x, col in ((-0.28, a), (0.28, b)):
        p.inner_keep = p.inner
        p.poly([(x - .22, -.24), (x - .22, .02), (x - .16, .22), (x + .16, .22), (x + .22, .02), (x + .22, -.24)], col)
        p.poly([(x - .13, .03), (x - .09, .16), (x + .09, .16), (x + .13, .03)], p.inner)
        p.line([(x - .15, -.24), (x - .15, -.34)], .08, col, False)
        p.line([(x + .15, -.24), (x + .15, -.34)], .08, col, False)


def no_overtaking(p):
    car_pair(p)


def grey_end(pen, draw):
    pen.disc((0, 0), 0.99, BLACK)
    pen.disc((0, 0), 0.965, WHITE)
    pen.inner = WHITE
    draw(pen)
    for k in (-0.24, -0.08, 0.08, 0.24):
        a = (-0.56 + k, -0.56 - k)
        b = (0.56 + k, 0.56 - k)
        pen.line([a, b], 0.05, BLACK, False)


def no_park(p):
    p.line([(-0.60, 0.60), (0.60, -0.60)], 0.18, RED, False)


def no_park_field(pen, _f):
    ring_sign(pen, hole=BLUE, band=0.20)
    pen.line([(-0.58, 0.58), (0.58, -0.58)], 0.20, RED, False)


def no_stop_field(pen, _f):
    no_park_field(pen, _f)
    pen.line([(-0.58, -0.58), (0.58, 0.58)], 0.20, RED, False)


def no_turn(s):
    def d(p):
        p.arrow([(-0.16 * s, -0.56), (-0.16 * s, 0.04), (0.56 * s, 0.04)], 0.16, BLACK)
        p.line([(-0.56, 0.56), (0.56, -0.56)], 0.15, RED, False)
    return d


def no_uturn(p):
    pts = [(0.34, -0.54), (0.34, 0.20)]
    pts += [(0.34 - 0.27 + 0.27 * math.cos(t), 0.20 + 0.27 * math.sin(t)) for t in [i * math.pi / 8 for i in range(1, 9)]]
    pts += [(-0.20, 0.02), (-0.20, -0.42)]
    p.arrow(pts, 0.15, BLACK, 2.0)
    p.line([(-0.56, 0.56), (0.56, -0.56)], 0.15, RED, False)


A = 0.20                        # the mandatory arrows' width


def d_ahead(p):
    p.arrow([(0, -0.66), (0, 0.72)], A, WHITE)


def d_turn(s):
    def d(p):
        p.arrow([(-0.18 * s, -0.66), (-0.18 * s, 0.12), (0.70 * s, 0.12)], A, WHITE)
    return d


def d_ahead_turn(s):
    def d(p):
        p.arrow([(-0.16 * s, -0.66), (-0.16 * s, 0.70)], A * .9, WHITE)
        p.arrow([(-0.16 * s, -0.24), (0.68 * s, -0.24 + 0.34)], A * .9, WHITE)
    return d


def d_keep(s):
    def d(p):
        p.arrow([(-0.46 * s, 0.46), (0.40 * s, -0.40)], A, WHITE)
    return d


def d_either(p):
    p.arrow([(0, 0.0), (-0.54, -0.54)], A * .85, WHITE)
    p.arrow([(0, 0.0), (0.54, -0.54)], A * .85, WHITE)
    p.line([(0, 0.0), (0, 0.66)], A * .85, WHITE, False)


def d_round(p):
    for a in (90, 210, 330):
        r = 0.46
        t = [math.radians(a + d) for d in (0, 30, 62)]
        p.arrow([(r * math.cos(x), r * math.sin(x)) for x in t], 0.15, WHITE, 2.0)


def d_ped(p):
    walker(p, WHITE, lambda q: (q[0] * 0.72, q[1] * 0.72 - 0.02))


def d_bike(p):
    bike(p, WHITE, lambda q: (q[0] * 0.92, q[1] * 0.92 + 0.08))


def i_motorway(end):
    def d(p):
        p.line([(-0.13, 0.76), (-0.46, -0.76)], 0.15, WHITE, False)
        p.line([(0.13, 0.76), (0.46, -0.76)], 0.15, WHITE, False)
        p.line([(-0.40, 0.08), (0.40, 0.08)], 0.16, WHITE, False)
        p.line([(-0.26, 0.40), (0.26, 0.40)], 0.14, WHITE, False)
        p.poly([(-0.09, -0.76), (0.09, -0.76), (0.04, 0.0), (-0.04, 0.0)], WHITE)
        if end:
            p.line([(-0.82, -0.72), (0.82, 0.72)], 0.14, RED, False)
    return d


def i_oneway(p):
    p.arrow([(0, -0.78), (0, 0.80)], 0.26, WHITE, 2.2)


def i_dead_end(p):
    p.line([(0, -0.78), (0, 0.48)], 0.30, WHITE, False)
    p.line([(-0.56, 0.52), (0.56, 0.52)], 0.30, RED, False)


def i_park(p):
    x0, y0, w = -0.38, -0.76, 0.24
    p.line([(x0, y0), (x0, 0.74)], w, WHITE, False)
    p.line([(x0, 0.64), (0.14, 0.64), (0.40, 0.38), (0.40, 0.14), (0.14, -0.08), (x0, -0.08)], w * .84, WHITE)


def i_bus(p, col=WHITE):
    p.poly([(-0.62, -0.32), (-0.62, 0.42), (-0.54, 0.54), (0.54, 0.54), (0.62, 0.42), (0.62, -0.32)], col)
    p.poly([(-0.52, 0.06), (-0.52, 0.42), (0.52, 0.42), (0.52, 0.06)], p.inner)
    p.line([(-0.42, -0.32), (-0.42, -0.50)], 0.20, col, False)
    p.line([(0.42, -0.32), (0.42, -0.50)], 0.20, col, False)
    p.disc((-0.40, -0.14), 0.08, p.inner)
    p.disc((0.40, -0.14), 0.08, p.inner)


def i_crossing(mirror):
    def go(pen, _f):
        blue_square(pen)
        s = -1 if mirror else 1
        pen.poly([(0, 0.80), (-0.86, -0.72), (0.86, -0.72)], WHITE)
        pen.inner = WHITE
        for i in range(4):
            x0 = -0.56 + i * 0.32
            pen.poly([(x0, -0.64), (x0 + .22, -0.64), (x0 + .22 + .08, -0.44), (x0 + .08, -0.44)], BLACK)
        walker(pen, BLACK, lambda q: (s * q[0] * 0.55, q[1] * 0.55 - 0.02))
    return go


def i_tunnel(pen, _f):
    blue_square(pen)
    pen.poly(rounded(inset(square(), 0.84), 0.04), BLACK if False else WHITE)
    pen.poly([(-.80, -.80), (.80, -.80), (.80, 0), (.80 * math.cos(.3), .8 * math.sin(.3)), (0, .72),
              (-.8 * math.cos(.3), .8 * math.sin(.3)), (-.80, 0)], WHITE)
    pen.disc((0, -0.18), 0.56, BLACK)
    pen.poly([(-.56, -.80), (.56, -.80), (.56, -.18), (-.56, -.18)], BLACK)
    pen.poly([(-.03, -.76), (.03, -.76), (.03, -.20), (-.03, -.20)], WHITE)


def i_living(end):
    def d(p):
        p.poly([(-0.86, -0.26), (-0.86, 0.30), (-0.54, 0.56), (-0.22, 0.30), (-0.22, -0.26)], BLUE)
        p.poly([(-0.22, -0.26), (-0.22, 0.04), (0.42, 0.04), (0.42, -0.26)], BLUE)
        walker(p, BLUE, lambda q: (q[0] * 0.34 + 0.14, q[1] * 0.34 - 0.40))
        car_rear(p, BLUE, lambda q: (q[0] * 0.36 + 0.56, q[1] * 0.36 - 0.52))
        p.poly([(-0.92, -0.86), (0.92, -0.86), (0.92, -0.78), (-0.92, -0.78)], BLUE)
        if end:
            p.line([(-0.86, -0.72), (0.86, 0.72)], 0.13, RED, False)
    return d


def i_pedzone(p):
    walker(p, BLACK, lambda q: (q[0] * 0.56 - 0.26, q[1] * 0.56 - 0.10))
    walker(p, BLACK, lambda q: (-q[0] * 0.50 + 0.30, q[1] * 0.50 - 0.14))


def chevron(side):
    """A bend's direction board (Ukraine's 1.4.1, the Czech and Polish long boards alike): a long red board with a thin white
    rim and five white chevrons pointing the way the road turns, four times as wide as high; it fills the middle band of its
    cell (outline 6)."""
    def go(pen, _f):
        x0, x1, y0, y1 = -0.97, 0.97, -0.25, 0.25
        pen.poly(rounded([(x1, y1), (x0, y1), (x0, y0), (x1, y0)], 0.04), WHITE)
        pen.poly(rounded([(x1 - 0.025, y1 - 0.05), (x0 + 0.025, y1 - 0.05), (x0 + 0.025, y0 + 0.05), (x1 - 0.025, y0 + 0.05)], 0.03), RED)
        for k in range(5):
            cx = (k - 2) * 0.36 * side
            pen.line([(cx - 0.09 * side, 0.15), (cx + 0.09 * side, 0.0), (cx - 0.09 * side, -0.15)], 0.085, WHITE, False)
    return go


def bus_cz(pen, _f):
    """The Czech stop sign: a white disc with blue caps, a bus and ZASTÁVKA in the white band."""
    pen.disc((0, 0), 0.99, WHITE)
    pen.disc((0, 0), 0.93, BLUE)
    ys = [0.5 - k * 0.05 for k in range(21)]
    pen.poly([(-math.sqrt(0.93 ** 2 - y * y), y) for y in ys] + [(math.sqrt(0.93 ** 2 - y * y), y) for y in reversed(ys)], WHITE)
    pen.poly([(-0.55, -0.02), (-0.55, 0.40), (-0.50, 0.46), (0.50, 0.46), (0.55, 0.40), (0.55, -0.02)], BLACK)
    for i in range(5):
        x = -0.46 + i * 0.185
        pen.poly([(x, 0.10), (x + 0.15, 0.10), (x + 0.15, 0.34), (x, 0.34)], WHITE)
    pen.disc((-0.34, -0.04), 0.085, BLACK)
    pen.disc((0.34, -0.04), 0.085, BLACK)
    pen.text((0, -0.30), 0.24, "ZASTÁVKA", BLACK, width=1.55)
    pen.inner = WHITE


def bus_de(pen, _f):
    """The German stop sign (Zeichen 224): a yellow disc in a green ring with a green H."""
    pen.disc((0, 0), 0.99, WHITE)
    pen.disc((0, 0), 0.95, GREEN)
    pen.disc((0, 0), 0.82, YELLOW)
    pen.line([(-0.30, -0.50), (-0.30, 0.50)], 0.24, GREEN, False)
    pen.line([(0.30, -0.50), (0.30, 0.50)], 0.24, GREEN, False)
    pen.line([(-0.30, 0.0), (0.30, 0.0)], 0.22, GREEN, False)
    pen.inner = YELLOW


def i_bus_lane(p):
    i_bus(p)
    p.poly([(-.84, -.72), (.84, -.72), (.84, -.60), (-.84, -.60)], WHITE)


DRAW = {
    "prio.main_road": main_road(False), "prio.main_road_end": main_road(True), "prio.give_way": give_way, "prio.stop": stop,
    "prio.oncoming": priority_oncoming, "prio.over_oncoming": priority_over,
    "reg.no_entry": no_entry, "reg.no_vehicles": ring(lambda p: None), "reg.no_overtaking": ring(no_overtaking),
    "reg.no_overtaking_end": lambda p, f: grey_end(p, lambda q: car_pair(q, BLACK, BLACK)),
    "reg.no_parking": no_park_field, "reg.no_stopping": no_stop_field,
    "reg.end_restrictions": lambda p, f: grey_end(p, lambda q: None),
    "reg.no_left": ring(no_turn(-1)), "reg.no_right": ring(no_turn(1)), "reg.no_uturn": ring(no_uturn),
    "mand.ahead": disc(d_ahead), "mand.right": disc(d_turn(1)), "mand.left": disc(d_turn(-1)),
    "mand.ahead_right": disc(d_ahead_turn(1)), "mand.ahead_left": disc(d_ahead_turn(-1)),
    "mand.keep_right": disc(d_keep(1)), "mand.keep_left": disc(d_keep(-1)), "mand.pass_either": disc(d_either),
    "mand.roundabout": disc(d_round), "mand.pedestrian": disc(d_ped), "mand.bicycle": disc(d_bike),
    "warn.curve_right": W(curve(False)), "warn.curve_left": W(curve(True)), "warn.double_right": W(double(False)),
    "warn.double_left": W(double(True)), "warn.crossroads": W(crossroads), "warn.side_right": W(side_road(1)),
    "warn.side_left": W(side_road(-1)), "warn.roundabout": W(w_round), "warn.signals": W(signals),
    "warn.level_gate": W(gate), "warn.level_nogate": W(steam), "warn.steep_down": W(steep(False)),
    "warn.steep_up": W(steep(True)), "warn.narrow_both": W(narrow(True, True)), "warn.narrow_right": W(narrow(False, True)),
    "warn.narrow_left": W(narrow(True, False)), "warn.bump": W(bump), "warn.slippery": W(slippery),
    "warn.pedestrians": W(on_tri(walker)), "warn.children": W(children), "warn.cyclists": W(on_tri(bike, True, k=0.40)),
    "warn.animals": W(on_tri(deer, k=0.46)), "warn.roadworks": W(on_tri(worker, k=0.46)), "warn.danger": W(danger),
    "info.motorway": blue(i_motorway(False)), "info.motorway_end": blue(i_motorway(True)), "info.oneway": blue(i_oneway),
    "info.dead_end": blue(i_dead_end), "info.living_street": panel(i_living(False)),
    "info.living_street_end": panel(i_living(True)), "info.pedestrian_zone": panel(i_pedzone), "info.tunnel": i_tunnel,
    "info.parking": blue(i_park), "info.bus_stop": blue(i_bus), "info.crossing_right": i_crossing(False),
    "info.crossing_left": i_crossing(True), "info.bus_lane": blue(i_bus_lane),
    "warn.chevron_right": chevron(1), "warn.chevron_left": chevron(-1), "info.bus_stop_cz": bus_cz,
    "info.bus_stop_de": bus_de,
}


def roles():
    with open(ROLES, encoding="utf-8") as f:
        return json.load(f)


def bg_of(role):
    return {"circle": WHITE, "tri_up": WHITE, "tri_down": WHITE, "octagon": WHITE, "diamond": BLACK}.get(role["shape"], WHITE)


def sheet(table, field):
    """The atlas of one system: field is the warning triangle's colour."""
    px, cols, rows = table["cell_px"], table["cols"], table["rows"]
    out = Image.new("RGB", (cols * px, rows * px), WHITE)
    for k, role in enumerate(table["roles"]):
        pen = Pen(px, bg_of(role))
        pen.inner = WHITE
        DRAW[role["id"]](pen, field)
        out.paste(pen.done(px), ((k % cols) * px, (k // cols) * px))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args()
    table = roles()
    ids = [r["id"] for r in table["roles"]]
    missing = sorted(set(ids) - set(DRAW))
    extra = sorted(set(DRAW) - set(ids))
    if missing or extra or len(ids) != len(set(ids)) or len(ids) > table["cols"] * table["rows"]:
        print(f"roles without a drawing: {missing}; drawings without a role: {extra}", file=sys.stderr)
        return 1
    if a.check:
        return 0
    for name, field in table["systems"].items():
        path = os.path.join(a.out, f"bne_signs_{name}.color.png")
        sheet(table, {"white": WHITE, "yellow": YELLOW}[field]).quantize(colors=224, dither=Image.NONE).save(path, optimize=True)
        print(path, os.path.getsize(path) // 1024, "KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
