"""build_rail_crossing_props.py: the level crossings' equipment (mod/.../art/shapes/beamngEarth/bne_rr_*.dae and
rail_crossing.materials.json, its textures): the stock maps have none (2026-10-27).

    python3 tools/build_rail_crossing_props.py

Each model stands on the ground at its origin, Z up, its face towards -Y (the traffic it faces), the road to its -X
(the driver's left as it is seen: right-hand traffic; the _l models are mirrored for left-hand traffic):
  bne_rr_crossbuck    a crossbuck on its post (the US and Canada: RAILROAD CROSSING)
  bne_rr_cross_eu     a St Andrew's cross (red and white: the Vienna Convention's countries)
  bne_rr_flasher      a mast with the crossbuck, two red lamps on a cross-arm and a bell
  bne_rr_flasher_eu   the same with the St Andrew's cross
  bne_rr_gate(_l)     a flasher with the gate's mechanism and its arm, raised (as it stands with no train: one model
                      whatever the road's width)
  bne_rr_gate_eu(_l)  the same with the St Andrew's cross
Two levels of detail and a collision mesh (the post or mast) each; materials bne_rr_* (plain colours, the boards'
faces textured).
"""
import json
import math
import os
import uuid

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "mod", "beamng_earth", "levels", "beamng_earth", "art", "shapes", "beamngEarth")
TEX_DIR = "/levels/beamng_earth/art/shapes/beamngEarth"
FONTS = ["/usr/share/fonts/TTF/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/liberation/LiberationSans-Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"]

# the materials: (base colour, metallic, roughness, emissive)
COLOURS = {
    "bne_rr_white": ((0.86, 0.86, 0.84), 0.0, 0.5, None),
    "bne_rr_black": ((0.04, 0.04, 0.04), 0.0, 0.6, None),
    "bne_rr_red": ((0.62, 0.05, 0.04), 0.0, 0.45, None),
    "bne_rr_metal": ((0.62, 0.63, 0.64), 0.8, 0.45, None),     # galvanised steel
    "bne_rr_lens": ((0.55, 0.04, 0.03), 0.0, 0.15, None),
    "bne_rr_concrete": ((0.55, 0.54, 0.51), 0.0, 0.85, None),
}
BOARD_L, BOARD_W, BOARD_T = 1.22, 0.23, 0.02    # a crossbuck's boards (the MUTCD's 48 x 9 in)
BOARD_Z = 2.75                                  # m: the boards' crossing over the ground (a post) ...
MAST_BOARD_Z = 4.0                              # ... and on a mast
ARM_Z, LAMP_X, LAMP_R, LENS_R = 3.05, 0.6, 0.3, 0.15
GATE_PIVOT_Z, GATE_ARM_L = 1.05, 7.0            # the arm's pivot, its length (raised: straight up)
STRIPE_M = 0.6                                  # the arm's red and white bands


class Mesh:
    """Triangles per material, flat-shaded."""

    def __init__(self, segs=16):
        self.segs = segs
        self.p, self.n, self.uv, self.tri = [], [], [], {}

    def face(self, mat, pts, uvs=None):
        """A planar polygon (pts counter-clockwise seen from its front), fanned."""
        pts = [np.asarray(q, np.float64) for q in pts]
        nrm = np.cross(pts[1] - pts[0], pts[2] - pts[0])
        nrm /= max(np.linalg.norm(nrm), 1e-12)
        k = len(self.p)
        for i, q in enumerate(pts):
            self.p.append(q)
            self.n.append(nrm)
            self.uv.append(uvs[i] if uvs is not None else (q[0] + q[2], q[1] + q[2]))
        t = self.tri.setdefault(mat, [])
        for i in range(1, len(pts) - 1):
            t.append((k, k + i, k + i + 1))

    def box(self, mat, c, half, axes=None):
        """A box at c, half sizes half (x, y, z) along axes (3 unit vectors; default the world's)."""
        ax = np.eye(3) if axes is None else np.asarray(axes, np.float64)
        c = np.asarray(c, np.float64)
        h = np.asarray(half, np.float64)
        v = lambda sx, sy, sz: c + ax[0] * sx * h[0] + ax[1] * sy * h[1] + ax[2] * sz * h[2]
        for a, b, d, e in (((-1, -1, -1), (-1, -1, 1), (-1, 1, 1), (-1, 1, -1)),
                           ((1, -1, -1), (1, 1, -1), (1, 1, 1), (1, -1, 1)),
                           ((-1, -1, -1), (1, -1, -1), (1, -1, 1), (-1, -1, 1)),
                           ((-1, 1, -1), (-1, 1, 1), (1, 1, 1), (1, 1, -1)),
                           ((-1, -1, -1), (-1, 1, -1), (1, 1, -1), (1, -1, -1)),
                           ((-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1))):
            self.face(mat, [v(*a), v(*b), v(*d), v(*e)])

    def cylinder(self, mat, x, y, z0, z1, r, caps=True):
        """An upright cylinder."""
        ang = [2 * math.pi * i / self.segs for i in range(self.segs)]
        ring = [(x + r * math.cos(a), y + r * math.sin(a)) for a in ang]
        for i in range(self.segs):
            (ax_, ay_), (bx, by) = ring[i], ring[(i + 1) % self.segs]
            self.face(mat, [(ax_, ay_, z0), (bx, by, z0), (bx, by, z1), (ax_, ay_, z1)])
        if caps:
            self.face(mat, [(px, py, z1) for px, py in ring])
            self.face(mat, [(px, py, z0) for px, py in reversed(ring)])

    def disc_y(self, mat, x, y, z, r, front=True):
        """A disc in the XZ plane facing -Y (front) or +Y."""
        ang = [2 * math.pi * i / self.segs for i in range(self.segs)]
        pts = [(x + r * math.cos(a), y, z + r * math.sin(a)) for a in ang]
        self.face(mat, pts if front else list(reversed(pts)))

    def mirrored(self):
        """This mesh mirrored in X (the windings turned so the faces stay outward)."""
        m = Mesh(self.segs)
        m.p = [np.array([-q[0], q[1], q[2]]) for q in self.p]
        m.n = [np.array([-q[0], q[1], q[2]]) for q in self.n]
        m.uv = list(self.uv)
        m.tri = {k: [(a, c, b) for a, b, c in v] for k, v in self.tri.items()}
        return m


def boards(m, z, face_mat, back_mat):
    """The crossed boards centred at height z, their textured faces towards -Y (each board half the texture)."""
    for k, sgn in enumerate((1, -1)):
        a = math.radians(45.0) * sgn
        ax = np.array([math.cos(a), 0.0, math.sin(a)])
        up = np.array([-math.sin(a), 0.0, math.cos(a)])
        c = np.array([0.0, -0.01 * (k + 1), z])
        hl, hw, ht = BOARD_L / 2, BOARD_W / 2, BOARD_T / 2
        q = lambda s, t, y: c + ax * s * hl + up * t * hw + np.array([0.0, y, 0.0])
        v0 = 0.5 * k
        # (ax x up is -Y: these corners in this order face the traffic)
        m.face(face_mat, [q(-1, -1, -ht), q(1, -1, -ht), q(1, 1, -ht), q(-1, 1, -ht)],
               [(0.0, v0), (1.0, v0), (1.0, v0 + 0.5), (0.0, v0 + 0.5)])
        m.face(back_mat, [q(-1, -1, ht), q(1, -1, ht), q(1, 1, ht), q(-1, 1, ht)][::-1])
        for s0, t0, s1, t1 in ((-1, -1, 1, -1), (1, -1, 1, 1), (1, 1, -1, 1), (-1, 1, -1, -1)):
            m.face(back_mat, [q(s0, t0, -ht), q(s0, t0, ht), q(s1, t1, ht), q(s1, t1, -ht)])


def crossbuck(segs, face):
    m = Mesh(segs)
    m.cylinder("bne_rr_metal", 0.0, 0.06, 0.0, BOARD_Z + 0.45, 0.04)
    boards(m, BOARD_Z, face, "bne_rr_metal")
    return m


def flasher(segs, face, detail=True):
    m = Mesh(segs)
    m.box("bne_rr_concrete", (0.0, 0.1, 0.1), (0.35, 0.35, 0.1))
    m.cylinder("bne_rr_metal", 0.0, 0.1, 0.2, MAST_BOARD_Z + 0.55, 0.07)
    boards(m, MAST_BOARD_Z, face, "bne_rr_metal")
    m.box("bne_rr_metal", (0.0, 0.02, ARM_Z), (LAMP_X + 0.2, 0.03, 0.04))
    for x in (-LAMP_X, LAMP_X):
        m.disc_y("bne_rr_black", x, -0.02, ARM_Z, LAMP_R)                      # the backplate
        m.disc_y("bne_rr_black", x, -0.015, ARM_Z, LAMP_R, front=False)
        m.disc_y("bne_rr_lens", x, -0.05, ARM_Z, LENS_R)
        if detail:                                                              # the lamp's hood
            m.box("bne_rr_black", (x, -0.12, ARM_Z + LENS_R + 0.02), (LENS_R + 0.03, 0.09, 0.012))
    if detail:
        m.cylinder("bne_rr_black", 0.0, 0.1, MAST_BOARD_Z + 0.55, MAST_BOARD_Z + 0.7, 0.13)    # the bell
        m.box("bne_rr_metal", (0.0, 0.3, 1.0), (0.18, 0.12, 0.25))                             # the junction box
    return m


def gate(segs, face, detail=True):
    m = flasher(segs, face, detail)
    x = -0.55                                                       # the mechanism, on the road's side
    m.box("bne_rr_metal", (x, 0.1, 0.65), (0.2, 0.2, 0.45))
    m.box("bne_rr_black", (x, -0.15, GATE_PIVOT_Z), (0.12, 0.08, 0.12))
    n = int(round(GATE_ARM_L / STRIPE_M))
    for i in range(n):                                              # the raised arm, in bands
        z0 = GATE_PIVOT_Z + i * STRIPE_M
        m.box("bne_rr_red" if i % 2 else "bne_rr_white", (x, -0.25, z0 + STRIPE_M / 2), (0.05, 0.05, STRIPE_M / 2))
    if detail:                                                      # the counterweight, below the pivot
        m.box("bne_rr_metal", (x, -0.25, GATE_PIVOT_Z - 0.35), (0.06, 0.06, 0.3))
    return m


def collision(kind):
    """The post's or mast's box: what a car hits."""
    m = Mesh(4)
    if kind == "crossbuck":
        m.box("bne_rr_metal", (0.0, 0.06, 1.6), (0.05, 0.05, 1.6))
    else:
        m.box("bne_rr_metal", (0.0, 0.1, 2.3), (0.1, 0.1, 2.3))
    return m


def _f(a):
    return " ".join(f"{float(v):.5g}" for v in np.asarray(a).ravel())


def _geom(gid, m, mats):
    n = len(m.p)
    blocks = "".join(
        f'<triangles count="{len(m.tri[k])}" material="{k}-mat">'
        f'<input semantic="VERTEX" source="#{gid}-v" offset="0"/>'
        f'<input semantic="NORMAL" source="#{gid}-n" offset="0"/>'
        f'<input semantic="TEXCOORD" source="#{gid}-uv" offset="0" set="0"/>'
        f'<p>{" ".join(str(i) for t in m.tri[k] for i in t)}</p></triangles>\n' for k in mats if m.tri.get(k))
    acc = lambda sid, cnt, names: (f'<technique_common><accessor source="#{sid}-a" count="{cnt}" '
                                   f'stride="{len(names)}">' + "".join(f'<param name="{x}" type="float"/>'
                                                                       for x in names)
                                   + "</accessor></technique_common>")
    return (f'<geometry id="{gid}"><mesh>\n'
            f'<source id="{gid}-p"><float_array id="{gid}-p-a" count="{n * 3}">{_f(m.p)}</float_array>'
            f'{acc(gid + "-p", n, "XYZ")}</source>\n'
            f'<source id="{gid}-n"><float_array id="{gid}-n-a" count="{n * 3}">{_f(m.n)}</float_array>'
            f'{acc(gid + "-n", n, "XYZ")}</source>\n'
            f'<source id="{gid}-uv"><float_array id="{gid}-uv-a" count="{n * 2}">{_f(m.uv)}</float_array>'
            f'{acc(gid + "-uv", n, "ST")}</source>\n'
            f'<vertices id="{gid}-v"><input semantic="POSITION" source="#{gid}-p"/></vertices>\n'
            f'{blocks}</mesh></geometry>\n')


def write_dae(path, name, lods, col):
    """lods: [(size px, Mesh)], nearest first; col: the collision Mesh."""
    mats = sorted({k for _, m in lods for k in m.tri} | set(col.tri))
    geoms, nodes = "", ""
    for size, m in lods:
        gid = f"{name}{size}-g"
        geoms += _geom(gid, m, mats)
        nodes += (f'<node id="{name}{size}" name="{name}{size}"><instance_geometry url="#{gid}"><bind_material>'
                  '<technique_common>' + "".join(f'<instance_material symbol="{k}-mat" target="#{k}-mat"/>'
                                                 for k in mats if m.tri.get(k))
                  + '</technique_common></bind_material></instance_geometry></node>\n')
    geoms += _geom(f"{name}-col-g", col, mats)
    nodes += (f'<node id="Colmesh_{name}-1" name="Colmesh_{name}-1"><instance_geometry url="#{name}-col-g">'
              '<bind_material><technique_common>' + "".join(f'<instance_material symbol="{k}-mat" target="#{k}-mat"/>'
                                                            for k in mats if col.tri.get(k))
              + '</technique_common></bind_material></instance_geometry></node>\n')
    effects = "".join(f'<effect id="{k}-fx"><profile_COMMON><technique sid="common"><lambert><diffuse>'
                      f'<color>0.8 0.8 0.8 1</color></diffuse></lambert></technique></profile_COMMON></effect>\n'
                      for k in mats)
    materials = "".join(f'<material id="{k}-mat" name="{k}"><instance_effect url="#{k}-fx"/></material>\n'
                        for k in mats)
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
<asset><contributor><authoring_tool>BeamNG.Earth tools/build_rail_crossing_props.py</authoring_tool></contributor>
<unit meter="1" name="meter"/><up_axis>Z_UP</up_axis></asset>
<library_effects>
{effects}</library_effects>
<library_materials>
{materials}</library_materials>
<library_geometries>
{geoms}</library_geometries>
<library_visual_scenes><visual_scene id="scene" name="scene">
<node id="base00" name="base00"><node id="start01" name="start01">
{nodes}</node><node id="collision-1" name="collision-1"/></node>
</visual_scene></library_visual_scenes>
<scene><instance_visual_scene url="#scene"/></scene>
</COLLADA>
"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(xml)


def _font(size):
    for f in FONTS:
        if os.path.exists(f):
            return ImageFont.truetype(f, size)
    raise SystemExit("no bold font found (DejaVu Sans Bold or Liberation Sans Bold)")


def textures():
    """The boards' faces: 1024 x 384, the top half the first board, the bottom half the second."""
    W, H = 1024, 384
    us = Image.new("RGB", (W, H), (236, 236, 232))
    d = ImageDraw.Draw(us)
    for k, word in enumerate(("RAILROAD", "CROSSING")):
        y0 = k * H // 2
        d.rectangle((6, y0 + 6, W - 7, y0 + H // 2 - 7), outline=(20, 20, 20), width=10)
        f = _font(150)
        l, t, r, b = d.textbbox((0, 0), word, font=f)
        d.text(((W - (r - l)) / 2 - l, y0 + (H // 2 - (b - t)) / 2 - t), word, font=f, fill=(15, 15, 15))
    us.transpose(Image.FLIP_TOP_BOTTOM).save(os.path.join(OUT, "bne_rr_crossbuck.png"))
    eu = Image.new("RGB", (W, H), (236, 236, 232))
    d = ImageDraw.Draw(eu)
    for k in range(2):                       # white with a red border (the Vienna Convention's A,28a)
        y0 = k * H // 2
        d.rectangle((0, y0, W - 1, y0 + H // 2 - 1), outline=(190, 20, 20), width=34)
    eu.transpose(Image.FLIP_TOP_BOTTOM).save(os.path.join(OUT, "bne_rr_cross_eu.png"))


def materials():
    out = {}
    pid = lambda n: str(uuid.uuid5(uuid.NAMESPACE_URL, "beamng-earth/" + n))
    for k, (col, met, rough, _em) in COLOURS.items():
        out[k] = {"name": k, "mapTo": k, "class": "Material", "version": 1.5, "persistentId": pid(k),
                  "Stages": [{"baseColorFactor": [*col, 1.0], "metallicFactor": met, "roughnessFactor": rough},
                             {}, {}, {}]}
    for k in ("bne_rr_crossbuck", "bne_rr_cross_eu"):
        out[k] = {"name": k, "mapTo": k, "class": "Material", "version": 1.5, "persistentId": pid(k),
                  "Stages": [{"baseColorMap": f"{TEX_DIR}/{k}.png", "metallicFactor": 0, "roughnessFactor": 0.4},
                             {}, {}, {}]}
    with open(os.path.join(OUT, "rail_crossing.materials.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)


def main():
    textures()
    materials()
    for face, sfx in (("bne_rr_crossbuck", ""), ("bne_rr_cross_eu", "_eu")):
        models = {
            ("bne_rr_cross_eu" if sfx else "bne_rr_crossbuck"): ([(64, crossbuck(16, face)), (8, crossbuck(6, face))],
                                                                  collision("crossbuck")),
            "bne_rr_flasher" + sfx: ([(64, flasher(16, face)), (8, flasher(6, face, False))], collision("mast")),
        }
        g = [(64, gate(16, face)), (8, gate(6, face, False))]
        models["bne_rr_gate" + sfx] = (g, collision("mast"))
        models["bne_rr_gate" + sfx + "_l"] = ([(s, m.mirrored()) for s, m in g], collision("mast"))
        for name, (lods, col) in models.items():
            path = os.path.join(OUT, name + ".dae")
            write_dae(path, name, lods, col)
            print(path, os.path.getsize(path), "bytes", sum(len(t) for t in lods[0][1].tri.values()), "triangles")


if __name__ == "__main__":
    main()
