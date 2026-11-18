"""build_au_warnsigns.py: Australia's yellow diamond warning signs (mod/.../art/shapes/beamngEarth/bne_au_warn_*.dae,
au_warnsigns.materials.json), placed by server/warnsigns.py (Queensland).

    python tools/build_au_warnsigns.py

AS 1742.2 W-series (warning signs: black symbol and border on a yellow diamond, here the 750 mm size of a sign on a
rural road, 1.06 m corner to corner, its corners rounded), on a galvanised round post behind it: the lower corner 1.5 m
over the ground (a post sunk 0.3 m). Four models: a curve and a turn, each to the left and to the right (the mirror
image). The face looks towards -Y, the traffic it warns, the post at the origin behind it (the layout of
build_median_props.py's Swedish signs, so the server's rows are alike).
"""
import json
import math
import os
import uuid

from build_rail_crossing_props import OUT, Mesh, write_dae

SIDE = 0.75                         # m: the diamond's side (AS 1742.2: 600 or 750 mm; the 750 of a rural road)
HALF = SIDE * math.sqrt(0.5) * 1.0  # m: half its diagonal, 0.53
LOW = 1.5                           # m: the lower corner's height
CENTRE_Z = LOW + HALF
TOP = CENTRE_Z + HALF               # m: the model's height
POST_R, POST_SINK = 0.03, 0.3
FRONT, BACK = -0.045, -0.035        # m: the plate's front and back faces' Y (the post just behind it)
INK = -0.004                       # m: the black artwork stands this far in front of the yellow
CORNER = 0.05                       # m: a corner's chamfer
BORDER = (0.014, 0.034)             # m: the black border's outer and inner edge inside the plate's edge
COLOURS = {
    "bne_au_warn_yellow": ((0.96, 0.74, 0.04), 0.1, 0.3),       # retroreflective yellow
    "bne_au_warn_black": ((0.02, 0.02, 0.02), 0.0, 0.5),
    "bne_au_warn_steel": ((0.62, 0.64, 0.65), 0.7, 0.45),       # the galvanised post and the plate's back
}
YELLOW, BLACK, STEEL = COLOURS


def plate(inset=0.0):
    """The diamond's outline, counter-clockwise from the front: (u, v) from its middle, corners chamfered, its edge pulled
    in by inset."""
    h = HALF - inset * math.sqrt(2.0)
    tips = [(h, 0.0), (0.0, h), (-h, 0.0), (0.0, -h)]
    out = []
    for k, t in enumerate(tips):
        for n in (tips[k - 1], tips[(k + 1) % 4]):          # (along the edge from the previous corner, then to the next)
            d = math.hypot(n[0] - t[0], n[1] - t[1])
            out.append((t[0] + (n[0] - t[0]) / d * CORNER, t[1] + (n[1] - t[1]) / d * CORNER))
    return out


class Face:
    """Faces of one sign's front: (u, v) points to the mesh's (x, y, z), turned round when mirrored."""

    def __init__(self, mesh, mirror):
        self.m, self.s = mesh, -1.0 if mirror else 1.0

    def poly(self, mat, pts, y):
        pts = [(self.s * u, y, CENTRE_Z + v) for u, v in pts]
        self.m.face(mat, pts if self.s > 0 else pts[::-1])

    def stroke(self, pts, w, y=INK + FRONT):
        """A line of width w along pts (u, v) in black, its joints filled."""
        for a, b in zip(pts, pts[1:]):
            d = math.hypot(b[0] - a[0], b[1] - a[1])
            if d < 1e-9:
                continue
            nx, ny = -(b[1] - a[1]) / d * w / 2, (b[0] - a[0]) / d * w / 2
            self.poly(BLACK, [(a[0] - nx, a[1] - ny), (b[0] - nx, b[1] - ny), (b[0] + nx, b[1] + ny),
                              (a[0] + nx, a[1] + ny)], y)
        for p in pts[1:-1]:                                 # (a square where it bends: the quads leave a notch)
            r = w / 2
            self.poly(BLACK, [(p[0] - r, p[1] - r), (p[0] + r, p[1] - r), (p[0] + r, p[1] + r), (p[0] - r, p[1] + r)], y)

    def arrow_head(self, tip, d, length, width, y=INK + FRONT):
        """A triangle with its point at tip, pointing along unit d."""
        bx, by = tip[0] - d[0] * length, tip[1] - d[1] * length
        nx, ny = -d[1] * width / 2, d[0] * width / 2
        self.poly(BLACK, [(bx - nx, by - ny), tip, (bx + nx, by + ny)], y)


def arc(cx, cy, r, a0, a1, n=10):
    """Points round (cx, cy) from angle a0 to a1 (degrees)."""
    return [(cx + r * math.cos(math.radians(a0 + (a1 - a0) * k / n)), cy + r * math.sin(math.radians(a0 + (a1 - a0) * k / n)))
            for k in range(n + 1)]


STROKE = 0.075      # m: the arrow's line
HEAD = (0.17, 0.20) # m: the arrowhead's length and width


def symbol(f, kind):
    """The black artwork of a turn to the right (the mirror image does the left): kind 'curve' a long bend (the road's
    line turns about 50 degrees), 'turn' a square corner."""
    if kind == "curve":
        # up the middle, curving over to the right: stem, an arc (radius 0.30 m) from heading up to 40 degrees right of up
        r = 0.30
        x0 = -0.10
        stem = [(x0, -0.31), (x0, -0.12)]
        bend = arc(x0 + r, -0.12, r, 180, 180 - 55)
        tail = bend[-1]
        d = (math.cos(math.radians(90 - 55)), math.sin(math.radians(90 - 55)))     # (the heading there)
        end = (tail[0] + d[0] * 0.04, tail[1] + d[1] * 0.04)
        f.stroke(stem + bend + [end], STROKE)
        f.arrow_head((end[0] + d[0] * HEAD[0], end[1] + d[1] * HEAD[0]), d, HEAD[0], HEAD[1])
    else:
        # a corner at (-0.10, 0.08): up from the foot, round a small radius, off to the right
        c = (-0.10, 0.08)
        r = 0.06
        up = [(c[0], -0.31), (c[0], c[1] - r)]
        corner = arc(c[0] + r, c[1] - r, r, 180, 90, 5)
        along = [(c[0] + r, c[1]), (0.14, c[1])]
        f.stroke(up + corner + along, STROKE)
        f.arrow_head((0.14 + HEAD[0], c[1]), (1.0, 0.0), HEAD[0], HEAD[1])


def sign(kind, left, near):
    """One sign: Z up from the ground, the face to -Y."""
    m = Mesh(8)
    f = Face(m, left)
    edge = plate()
    f.poly(YELLOW, edge, FRONT)
    # (the back: the plate's own steel, seen the same way whichever way the sign is mirrored)
    m.face(STEEL, [(u, BACK, CENTRE_Z + v) for u, v in reversed(edge)])
    for (u0, v0), (u1, v1) in zip(edge, edge[1:] + edge[:1]):       # (the plate's edge)
        m.face(STEEL, [(u0, BACK, CENTRE_Z + v0), (u1, BACK, CENTRE_Z + v1), (u1, FRONT, CENTRE_Z + v1),
                       (u0, FRONT, CENTRE_Z + v0)])
    if near:
        outer, inner = plate(BORDER[0]), plate(BORDER[1])
        for k in range(len(outer)):
            k1 = (k + 1) % len(outer)
            f.poly(BLACK, [outer[k], outer[k1], inner[k1], inner[k]], INK + FRONT)
        symbol(f, kind)
    # the post behind it, up to the diamond's middle
    m.cylinder(STEEL, 0.0, BACK + POST_R, -POST_SINK, CENTRE_Z, POST_R, caps=False)
    return m


def pole():
    m = Mesh(4)
    m.box(STEEL, (0.0, BACK + POST_R, TOP / 2), (POST_R, POST_R, TOP / 2))
    return m


def materials():
    out = {}
    pid = lambda n: str(uuid.uuid5(uuid.NAMESPACE_URL, "beamng-earth/" + n))
    for k, (col, met, rough) in COLOURS.items():
        out[k] = {"name": k, "mapTo": k, "class": "Material", "version": 1.5, "persistentId": pid(k),
                  "Stages": [{"baseColorFactor": [*col, 1.0], "metallicFactor": met, "roughnessFactor": rough},
                             {}, {}, {}]}
    with open(os.path.join(OUT, "au_warnsigns.materials.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)


def main():
    materials()
    for kind in ("curve", "turn"):
        for left in (False, True):
            name = f"bne_au_warn_{kind}_{'l' if left else 'r'}"
            path = os.path.join(OUT, name + ".dae")
            lods = [(32, sign(kind, left, True)), (4, sign(kind, left, False))]
            write_dae(path, name, lods, pole())
            print(path, os.path.getsize(path), "bytes", sum(len(t) for t in lods[0][1].tri.values()), "triangles",
                  round(TOP, 2), "m")


if __name__ == "__main__":
    main()
