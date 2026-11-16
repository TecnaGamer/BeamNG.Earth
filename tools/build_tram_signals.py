"""build_tram_signals.py: the tram signals' models (mod/.../art/shapes/beamngEarth/bne_tramsig_*.dae, tram_signals.materials.json,
their textures), plan 046 stage H. Run: python tools/build_tram_signals.py

Each model stands on the ground at its origin (the post's foot), Z up, the face towards -Y (the oncoming tram), the model's
+X the right of the tram's travel (server/tramsignals.py yaw). One shape per face style of the kernel's tram::signals:
  bne_tramsig_bar   the ex-USSR / Czech / American bar signal: a square black head with a visor on a grey post, a horizontal
                    bar (stop) and a vertical bar (go) of white lamps
  bne_tramsig_bos   the BOStrab-like head (Germany, Finland and the rest): a small round black head with a hood on a dark post
  ..._l / ..._r     the same with a third lamp, a diagonal bar leaning to the left / right (slot 2: a switch ahead branches that
                    way; tram::signals' branch)
Two levels of detail and a collision mesh (the post) each. The lamps are separate triangles of the material bne_tramsig_lamps,
the engine's palette-instanced emissive material the stock traffic lights use (instanceDiffuse + instanceEmissive +
colorPaletteMap, additive): the three bands of the lamp texture are the TSStatic's instanceColor, instanceColor1 and
instanceColor2 (the palette's pure red, green, blue bands); the mod's tramsignals.lua sets each to '1 1 1 1' (lit) or
'0 0 0 1' (dark): slot 0 the horizontal bar (stop), slot 1 the vertical bar (go), slot 2 the diagonal bar (the branch).
The textures are procedural placeholders (flat white lamp bands with an LED dot pattern, plain colours for the metal): the
generated ones (docs/new/046_prompts_tram-textures_2026-11-13.md) replace them under the same names.
"""
import json
import math
import os
import sys
import uuid

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import build_rail_crossing_props as base      # (its Mesh and DAE writer)

OUT = base.OUT
TEX_DIR = base.TEX_DIR
TEX = 512                    # the lamp and palette textures: three bands (rows 0-170 slot 0, 171-341 slot 1, 342-511 slot 2)
BAND_T = [1.0 - (85.5 + 170.667 * k) / TEX for k in range(3)]       # V of the bands' centres (the importer flips V)
BAND_H = 0.26                # V span of a lamp's picture inside its band

COLOURS = {
    "bne_ts_black": ((0.025, 0.025, 0.027), 0.0, 0.55),
    "bne_ts_grey": ((0.46, 0.47, 0.48), 0.75, 0.45),          # galvanised post (the bar style)
    "bne_ts_dark": ((0.07, 0.075, 0.08), 0.35, 0.5),         # painted post (the BOStrab style)
    "bne_ts_concrete": ((0.55, 0.54, 0.51), 0.0, 0.85),
}
LAMPS = "bne_ts_lamps"
POST_H = 3.4                 # m: the post's top over the ground
FOUNDATION_H = 0.12


class Mesh(base.Mesh):
    def quad_y(self, mat, c, w, h, y, tc, vertical=False):
        """A lamp: a quad in the XZ plane at depth y facing -Y, w wide, h tall, its picture the band's tc."""
        x, z = c
        x0, x1, z0, z1 = x - w / 2, x + w / 2, z - h / 2, z + h / 2
        v0, v1 = tc - BAND_H / 2, tc + BAND_H / 2
        if vertical:
            uvs = [(0.0, v0), (0.0, v1), (1.0, v1), (1.0, v0)]
        else:
            uvs = [(0.0, v0), (1.0, v0), (1.0, v1), (0.0, v1)]
        self.face(mat, [(x0, y, z0), (x1, y, z0), (x1, y, z1), (x0, y, z1)], uvs)

    def quad_diag(self, mat, c, length, width, y, tc, lean):
        """A lamp: a bar at 45 degrees facing -Y, `lean` -1 up to the left (as the tram driver sees the face: the model's -X is the
        left of the travel) or +1 up to the right; its picture the band's tc, u along its length."""
        x, z = c
        ux, uz = lean * math.sqrt(0.5), math.sqrt(0.5)
        nx, nz = -uz, ux
        hl, hw = length / 2, width / 2
        v0, v1 = tc - BAND_H / 2, tc + BAND_H / 2
        pts = [(x - ux * hl - nx * hw, y, z - uz * hl - nz * hw), (x + ux * hl - nx * hw, y, z + uz * hl - nz * hw),
               (x + ux * hl + nx * hw, y, z + uz * hl + nz * hw), (x - ux * hl + nx * hw, y, z - uz * hl + nz * hw)]
        self.face(mat, pts, [(0.0, v0), (1.0, v0), (1.0, v1), (0.0, v1)])

    def cylinder_y(self, mat, x, z, y0, y1, r, caps=True):
        """A cylinder along Y from y0 to y1 (the round head)."""
        ang = [2 * math.pi * i / self.segs for i in range(self.segs)]
        ring = [(x + r * math.cos(a), z + r * math.sin(a)) for a in ang]
        for i in range(self.segs):
            (ax_, az_), (bx, bz) = ring[i], ring[(i + 1) % self.segs]
            self.face(mat, [(ax_, y0, az_), (bx, y0, bz), (bx, y1, bz), (ax_, y1, az_)][::-1])
        if caps:
            self.face(mat, [(px, y0, pz) for px, pz in ring])                # the front (towards -Y): counter-clockwise from there
            self.face(mat, [(px, y1, pz) for px, pz in reversed(ring)])


def _foot(m, post_mat, detail):
    m.cylinder("bne_ts_concrete", 0.0, 0.0, 0.0, FOUNDATION_H, 0.17)
    m.cylinder(post_mat, 0.0, 0.0, FOUNDATION_H, POST_H, 0.055)
    if detail:
        m.cylinder(post_mat, 0.0, 0.0, FOUNDATION_H, FOUNDATION_H + 0.03, 0.09)          # the base plate
        m.cylinder("bne_ts_black", 0.0, 0.0, POST_H, POST_H + 0.03, 0.065)                # the cap
        m.box("bne_ts_grey", (0.0, -0.075, 1.25), (0.075, 0.03, 0.22))                   # the control box's door
        m.box(post_mat, (0.0, -0.04, 1.25), (0.06, 0.04, 0.2))


def bar(segs, detail=True, branch=0):
    """The bar signal: a square head on a bracket, its lamps on the front; branch -1 / 1: the diagonal bar of that side too."""
    m = Mesh(segs)
    _foot(m, "bne_ts_grey", detail)
    zc = 3.0
    m.box("bne_ts_black", (0.0, -0.09, zc), (0.19, 0.09, 0.19))                  # the head: y -0.18 .. 0
    m.box("bne_ts_grey", (0.0, 0.025, zc), (0.045, 0.045, 0.06))                 # the bracket to the post (y 0 .. 0.07)
    if detail:
        m.box("bne_ts_black", (0.0, -0.21, zc + 0.205), (0.2, 0.12, 0.011))     # the visor
        for sx in (-0.205, 0.205):
            m.box("bne_ts_black", (sx, -0.2, zc + 0.1), (0.008, 0.1, 0.1))      # its sides
    m.quad_y(LAMPS, (0.0, zc), 0.27, 0.052, -0.181, BAND_T[0])                  # the horizontal bar: stop
    m.quad_y(LAMPS, (0.0, zc), 0.052, 0.27, -0.182, BAND_T[1], vertical=True)   # the vertical bar: go
    if branch:
        m.quad_diag(LAMPS, (0.0, zc), 0.27, 0.052, -0.183, BAND_T[2], -branch)   # the diagonal bar: the branch's way
    return m


def bos(segs, detail=True, branch=0):
    """The BOStrab-like signal: a small round head with a hood; branch -1 / 1: the diagonal bar of that side too."""
    m = Mesh(max(segs, 8))
    _foot(m, "bne_ts_dark", detail)
    zc = 3.05
    m.cylinder_y("bne_ts_black", 0.0, zc, -0.16, 0.0, 0.165)
    m.box("bne_ts_dark", (0.0, 0.025, zc), (0.04, 0.045, 0.05))
    if detail:
        m.box("bne_ts_black", (0.0, -0.2, zc + 0.172), (0.15, 0.1, 0.009))       # the hood
        for sx in (-0.15, 0.15):
            m.box("bne_ts_black", (sx, -0.2, zc + 0.115), (0.008, 0.1, 0.06))
    m.quad_y(LAMPS, (0.0, zc), 0.215, 0.042, -0.161, BAND_T[0])
    m.quad_y(LAMPS, (0.0, zc), 0.042, 0.215, -0.162, BAND_T[1], vertical=True)
    if branch:
        m.quad_diag(LAMPS, (0.0, zc), 0.215, 0.042, -0.163, BAND_T[2], -branch)
    return m


def collision():
    """The post: what a car hits (the head is overhead)."""
    m = Mesh(4)
    m.box("bne_ts_grey", (0.0, 0.0, 1.7), (0.09, 0.09, 1.7))
    return m


def textures():
    os.makedirs(OUT, exist_ok=True)
    lamp = np.zeros((TEX, TEX, 3), np.uint8)
    pal = np.zeros((TEX, TEX, 3), np.uint8)
    yy, xx = np.mgrid[0:TEX, 0:TEX]
    dots = 0.5 + 0.5 * np.cos(2 * np.pi * (xx % 16) / 16.0) * np.cos(2 * np.pi * (yy % 16) / 16.0)
    lum = (236 + 19 * dots).clip(0, 255)
    for k in range(3):
        a, b = int(round(k * TEX / 3)), int(round((k + 1) * TEX / 3))
        for c in range(3):
            lamp[a:b, :, c] = (lum[a:b] * (1.0, 0.985, 0.93)[c]).astype(np.uint8)
        pal[a:b, :, k] = 255
    Image.fromarray(lamp, "RGB").save(os.path.join(OUT, "bne_ts_lamps.color.png"))
    Image.fromarray(pal, "RGB").save(os.path.join(OUT, "bne_ts_palette.color.png"))


def materials():
    out = {}
    pid = lambda n: str(uuid.uuid5(uuid.NAMESPACE_URL, "beamng-earth/" + n))
    for k, (col, met, rough) in COLOURS.items():
        out[k] = {"name": k, "mapTo": k, "class": "Material", "version": 1.5, "persistentId": pid(k),
                  "Stages": [{"baseColorFactor": [*col, 1.0], "metallicFactor": met, "roughnessFactor": rough},
                             {}, {}, {}]}
    # (the stock instanced_trafficlight's fields: the lamps are lit by the object's instance colours)
    out[LAMPS] = {"name": LAMPS, "mapTo": LAMPS, "class": "Material", "version": 1.5, "persistentId": pid(LAMPS),
                  "Stages": [{"baseColorMap": f"{TEX_DIR}/bne_ts_lamps.color.png",
                              "colorPaletteMap": f"{TEX_DIR}/bne_ts_palette.color.png",
                              "emissive": True, "emissiveFactor": [1, 1, 1], "emissiveIntensityNits": 3000,
                              "emissiveMap": f"{TEX_DIR}/bne_ts_lamps.color.png",
                              "instanceDiffuse": True, "instanceEmissive": True, "pixelSpecular": True,
                              "roughnessFactor": 0.6},
                             {}, {}, {}],
                  "annotation": "TRAFFIC_SIGNALS", "castShadows": False, "translucent": True,
                  "translucentBlendOp": "Add"}
    with open(os.path.join(OUT, "tram_signals.materials.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)


def main(placeholders=False):
    if placeholders:        # (the user's textures replaced them: tools/ingest_tram_textures.py; this overwrites them)
        textures()
        materials()
    for name, fn in (("bne_tramsig_bar", bar), ("bne_tramsig_bos", bos)):
        for suffix, branch in (("", 0), ("_l", 1), ("_r", -1)):        # (branch 1: a track leaves to the left)
            path = os.path.join(OUT, name + suffix + ".dae")
            base.write_dae(path, name + suffix, [(64, fn(12, True, branch)), (8, fn(6, False, branch))], collision())
            print(path, os.path.getsize(path), "bytes", sum(len(t) for t in fn(12, True, branch).tri.values()), "triangles")


if __name__ == "__main__":
    main("--placeholders" in sys.argv)
