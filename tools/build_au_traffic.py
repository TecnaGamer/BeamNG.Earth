"""build_au_traffic.py: Australia's Give Way sign and traffic signals (Queensland):

    mod/.../art/shapes/beamngEarth/bne_au_giveway.dae           R1-2 Give Way on a galvanised post (server furniture.au_group)
    mod/.../art/shapes/beamngEarth/bne_au_signal.dae            a signal pole with one lantern (the static Forest item)
    mod/.../art/shapes/beamngEarth/bne_au_signal_mast.dae       a mast arm over the road with two lanterns and a pole lantern
    mod/.../art/shapes/beamngEarth/bne_au_signal_sw.dae         ... the same two with the stock instanced lens material, which
    mod/.../art/shapes/beamngEarth/bne_au_signal_mast_sw.dae    navgraph.lua lights per aspect (the AI's switchable heads)
    mod/.../art/shapes/beamngEarth/bne_au_giveway.png           the sign's face
    mod/.../art/shapes/beamngEarth/au_traffic.materials.json    materials (loaded by trees.lua)

    python tools/build_au_traffic.py

Give Way (AS 1742.2 R1-2): an inverted equilateral triangle 750 mm a side, a red border, a white field and the black legend GIVE
over WAY, on a 60 mm galvanised post, its lower point 2.0 m above the ground (the face's top edge at 2.65 m). The model is 2.7 m
high, the post's axis at the origin, the face toward local -Y (the drivers coming in: furniture.approach_pose) at y -0.045.

Signals (AS 1742.14, Queensland TMR): 3-aspect 300 mm lanterns, red over yellow over green, black housings in black target boards
with a white border, on a grey galvanised pole. Z up from the ground at the pole's axis; the lens faces toward local -Y, and the
arm (the mast) reaches toward local +X, over the road: furniture.approach_pose puts an Australian signal's pole on the drivers'
left kerb (a left-hand-traffic prop), local +X toward their right. Two model families, the same shape: the static one's lenses
are dark (bne_au_sig_lens: the stock lamp sheet dimmed, no glow), the switchable one's use the stock `instanced_trafficlight`
material on the stock sheet's bands (red v 0.67-1, yellow 0.33-0.67, green 0-0.33) with a white vertex colour, as the stock boom's
lenses have: navgraph.lua sets the object's instance colours and the right lens lights.
"""
import json
import math
import os
import re
import uuid

import numpy as np
from PIL import Image, ImageDraw

from build_au_streetscape import frustum
from build_rail_crossing_props import FONTS, OUT, TEX_DIR, Mesh, write_dae

LIGHTS = "/assets/materials/utility/trafficLight/instanced_trafficlight_lights.png"     # (a stock texture: referenced, not copied)
LENS_STOCK = "instanced_trafficlight"       # the stock lamp material
LENS_STATIC = "bne_au_sig_lens"

# Give Way
GW_SIDE = 0.75                              # m: the triangle's side
GW_H = GW_SIDE * math.sqrt(3) / 2           # 0.6495 m
GW_TOP_Z = 2.65                             # m: the face's top edge
GW_FACE_Y = -0.045                          # m: the face, in front of the post (local -Y: the traffic's)
GW_POST_R = 0.0305                          # a 60.3 mm tube
GW_POST_TOP = 2.67
GW_BORDER = 0.05                            # m: the red border's width (measured along the inradius)
TEX = 512

# Signals
LANTERN_W, LANTERN_D, LANTERN_H = 0.38, 0.25, 1.12      # the housing: 3 x 300 mm lenses, 0.37 m apart
LENS_R, LENS_PITCH = 0.15, 0.37
BOARD_W, BOARD_H = 0.62, 1.38                           # the target board
BORDER_W = 0.028                                        # its white border
POLE_R = 0.057
SIGNAL_POLE_TOP = 3.95
SIGNAL_LANTERN_Z = 3.15                                 # the pole lantern's middle (housing 2.59 to 3.71 m)
MAST_POLE_TOP = 7.0
MAST_ARM_Z, MAST_ARM_L = 6.55, 7.2                      # the arm's axis; its reach (the pole's axis to its end)
MAST_LANTERNS = (3.4, 6.2)                              # m along the arm
MAST_ARM_LANTERN_Z = MAST_ARM_Z - 0.045 - BOARD_H / 2   # the board's top at the arm's underside (the lenses 5.3 m up at least)
LENS_BANDS = (0.835, 0.5, 0.165)                        # v of the red, yellow, green bands' middles

COLOURS = {                                  # plain materials: (base colour, metallic, roughness)
    "bne_au_galv": ((0.60, 0.62, 0.63), 0.75, 0.45),            # galvanised steel
    "bne_au_sig_black": ((0.014, 0.014, 0.016), 0.0, 0.55),
    "bne_au_sig_white": ((0.88, 0.88, 0.86), 0.0, 0.5),
}


def _font(size):
    from PIL import ImageFont
    for f in FONTS:
        if os.path.exists(f):
            return ImageFont.truetype(f, size)
    return ImageFont.load_default()


def giveway_triangle():
    """The face's corners in the texture, pixels (x right, y down): top left, top right, the lower point."""
    y0 = (TEX - TEX * GW_H / GW_SIDE) / 2
    return [(0.0, y0), (float(TEX), y0), (TEX / 2.0, y0 + TEX * GW_H / GW_SIDE)]


def _inset(tri, k):
    cx = sum(p[0] for p in tri) / 3
    cy = sum(p[1] for p in tri) / 3
    return [(cx + (p[0] - cx) * k, cy + (p[1] - cy) * k) for p in tri]


def giveway_texture():
    """R1-2: red, a white field inside the border, GIVE over WAY in black bold capitals; supersampled twice."""
    s = 2
    img = Image.new("RGBA", (TEX * s, TEX * s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    tri = giveway_triangle()
    d.polygon([(x * s, y * s) for x, y in tri], fill=(196, 18, 28, 255))
    inradius = TEX / (2 * math.sqrt(3))
    k = (inradius - GW_BORDER / GW_SIDE * TEX) / inradius
    inner = _inset(tri, k)
    d.polygon([(x * s, y * s) for x, y in inner], fill=(244, 244, 240, 255))
    top = inner[0][1]
    h = inner[2][1] - top
    width_at = lambda y: (inner[1][0] - inner[0][0]) * (1.0 - (y - top) / h)          # noqa: E731
    for word, at in (("GIVE", 0.20), ("WAY", 0.50)):
        y = top + at * h
        size = 20
        while True:                                  # the biggest bold capitals that fit 82 % of the field's width at the baseline
            font = _font(size * s)
            box = d.textbbox((0, 0), word, font=font)
            if (box[2] - box[0]) / s > 0.82 * width_at(y + (box[3] - box[1]) / s * 0.55) or size > 140:
                break
            size += 1
        font = _font((size - 1) * s)
        box = d.textbbox((0, 0), word, font=font)
        w, hh = box[2] - box[0], box[3] - box[1]
        d.text((TEX * s / 2 - w / 2 - box[0], y * s - hh / 2 - box[1]), word, font=font, fill=(10, 10, 10, 255))
    out = img.resize((TEX, TEX), Image.LANCZOS)
    flat = Image.new("RGB", (TEX, TEX), (196, 18, 28))            # (the corners outside the face: its red)
    flat.paste(out, mask=out.split()[3])
    flat.save(os.path.join(OUT, "bne_au_giveway.png"))


def giveway():
    m = Mesh(12)
    m.cylinder("bne_au_galv", 0.0, 0.0, -0.3, GW_POST_TOP, GW_POST_R)
    for z in (GW_TOP_Z - 0.12, GW_TOP_Z - 0.38):                 # the clamps behind the face
        m.box("bne_au_galv", (0.0, GW_FACE_Y + 0.0045, z), (0.035, 0.0105, 0.014))
    tri = giveway_triangle()
    h = GW_SIDE / 2
    z0 = GW_TOP_Z - GW_H
    pts3 = [(-h, GW_FACE_Y, GW_TOP_Z), (0.0, GW_FACE_Y, z0), (h, GW_FACE_Y, GW_TOP_Z)]      # TL, the lower point, TR: faces -Y
    uv = [(x / TEX, 1.0 - y / TEX) for x, y in (tri[0], tri[2], tri[1])]
    m.face("bne_au_giveway_face", pts3, uv)
    back = [(p[0], GW_FACE_Y + 0.003, p[2]) for p in pts3][::-1]
    m.face("bne_au_galv", back)
    return m


def lantern(m, xc, zc, yfront, lens):
    """A 3-aspect 300 mm lantern with its target board: the housing from yfront (the lens face) 0.25 m back, the board across it
    0.10 m behind the lens faces, a visor over each lens, the lenses red over yellow over green facing -Y."""
    m.box("bne_au_sig_black", (xc, yfront + LANTERN_D / 2, zc), (LANTERN_W / 2, LANTERN_D / 2, LANTERN_H / 2))
    yb = yfront + 0.10
    m.box("bne_au_sig_black", (xc, yb, zc), (BOARD_W / 2, 0.012, BOARD_H / 2))
    fy = yb - 0.012 - 0.0015
    bw, bh = BOARD_W / 2 - 0.012, BOARD_H / 2 - 0.012
    for sz in (-1.0, 1.0):                                       # the white border: two strips along the board's top and bottom
        m.box("bne_au_sig_white", (xc, fy, zc + sz * (bh - BORDER_W / 2)), (bw, 0.0015, BORDER_W / 2))
    for sx in (-1.0, 1.0):                                       # ... and two up its sides
        m.box("bne_au_sig_white", (xc + sx * (bw - BORDER_W / 2), fy, zc), (BORDER_W / 2, 0.0015, bh))
    for i, band in enumerate(LENS_BANDS):
        z = zc + (1 - i) * LENS_PITCH
        m.box("bne_au_sig_black", (xc, yfront - 0.05, z + LENS_R + 0.015), (0.17, 0.06, 0.008))
        ang = [2 * math.pi * k / 16 for k in range(16)]
        pts = [(xc + LENS_R * math.cos(a), yfront - 0.001, z + LENS_R * math.sin(a)) for a in ang]
        m.face(lens, pts, [(0.5 + 0.1 * math.cos(a), band + 0.1 * math.sin(a)) for a in ang])       # (inside its band: 0.165 +- 0.1)


def signal(lens):
    m = Mesh(12)
    m.cylinder("bne_au_galv", 0.0, 0.0, -0.3, SIGNAL_POLE_TOP, POLE_R)
    m.cylinder("bne_au_galv", 0.0, 0.0, 0.0, 0.22, POLE_R + 0.02)           # the base's collar
    lantern(m, 0.0, SIGNAL_LANTERN_Z, -0.31, lens)
    return m


def mast(lens):
    m = Mesh(12)
    frustum(m, "bne_au_galv", -0.3, MAST_POLE_TOP, 0.095, 0.065)
    m.cylinder("bne_au_galv", 0.0, 0.0, 0.0, 0.25, 0.115)                   # the base
    m.box("bne_au_galv", (MAST_ARM_L / 2, -0.025, MAST_ARM_Z), (MAST_ARM_L / 2, 0.045, 0.045))
    lantern(m, 0.0, 3.2, -0.33, lens)                                       # the pole's own (the primary, at the stop line's side)
    for x in MAST_LANTERNS:
        m.box("bne_au_galv", (x, -0.025, MAST_ARM_Z - 0.045 - 0.02), (0.03, 0.03, 0.02))    # its hanger
        lantern(m, x, MAST_ARM_LANTERN_Z, -0.15, lens)
    return m


def collision(mast_arm):
    m = Mesh(4)
    if mast_arm:
        m.box("bne_au_galv", (0.0, 0.0, 3.35), (0.10, 0.10, 3.65))
        m.box("bne_au_galv", (MAST_ARM_L / 2, -0.025, MAST_ARM_Z), (MAST_ARM_L / 2, 0.045, 0.045))
    else:
        m.box("bne_au_galv", (0.0, 0.0, (SIGNAL_POLE_TOP - 0.3) / 2), (0.07, 0.07, (SIGNAL_POLE_TOP + 0.3) / 2))
    return m


def materials():
    pid = lambda n: str(uuid.uuid5(uuid.NAMESPACE_URL, "beamng-earth/" + n))                # noqa: E731
    out = {}

    def add(name, stage, **extra):
        out[name] = {"name": name, "mapTo": name, "class": "Material", "version": 1.5, "persistentId": pid(name),
                     "Stages": [stage, {}, {}, {}], **extra}
    for k, (col, met, rough) in COLOURS.items():
        add(k, {"baseColorFactor": [*col, 1.0], "metallicFactor": met, "roughnessFactor": rough})
    add(LENS_STATIC, {"baseColorMap": LIGHTS, "baseColorFactor": [0.12, 0.12, 0.12, 1.0], "metallicFactor": 0.0,
                      "roughnessFactor": 0.15})
    add("bne_au_giveway_face", {"baseColorMap": f"{TEX_DIR}/bne_au_giveway.png", "baseColorFactor": [1.0, 1.0, 1.0, 1.0],
                                "metallicFactor": 0.0, "roughnessFactor": 0.35})
    with open(os.path.join(OUT, "au_traffic.materials.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(out, fh, indent=2)


def add_vertex_colour(path):
    """The stock lens material reads a vertex colour (vertColor / vertColorEmissive) and the stock boom's lenses carry white: give every
    vertex of the file's meshes one (a COLOR source, white, beside the UVs)."""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()

    def one(g):
        gid = g.group(1)
        n = int(re.search(r'<source id="%s-p"><float_array id="[^"]*" count="(\d+)"' % re.escape(gid), g.group(0)).group(1)) // 3
        src = (f'<source id="{gid}-c"><float_array id="{gid}-c-a" count="{n * 3}">{" ".join(["1"] * (n * 3))}</float_array>'
               f'<technique_common><accessor source="#{gid}-c-a" count="{n}" stride="3"><param name="R" type="float"/>'
               f'<param name="G" type="float"/><param name="B" type="float"/></accessor></technique_common></source>\n')
        body = g.group(0).replace("<vertices", src + "<vertices", 1)
        uv = f'<input semantic="TEXCOORD" source="#{gid}-uv" offset="0" set="0"/>'
        return body.replace(uv, uv + f'<input semantic="COLOR" source="#{gid}-c" offset="0" set="0"/>')
    text = re.sub(r'<geometry id="([^"]*)">.*?</geometry>', one, text, flags=re.S)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def main():
    giveway_texture()
    materials()
    models = {"bne_au_giveway": (giveway(), Mesh(4), False),
              "bne_au_signal": (signal(LENS_STATIC), collision(False), False),
              "bne_au_signal_mast": (mast(LENS_STATIC), collision(True), False),
              "bne_au_signal_sw": (signal(LENS_STOCK), collision(False), True),
              "bne_au_signal_mast_sw": (mast(LENS_STOCK), collision(True), True)}
    for name, (m, col, vertex_colour) in models.items():
        path = os.path.join(OUT, name + ".dae")
        write_dae(path, name, [(4, m)], col)
        if vertex_colour:
            add_vertex_colour(path)
        print(path, os.path.getsize(path), "bytes", sum(len(t) for t in m.tri.values()), "triangles")


if __name__ == "__main__":
    main()
