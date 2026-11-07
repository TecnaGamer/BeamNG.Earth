"""build_au_streetscape.py: Australia's street furniture models and materials (fork: Queensland):

    mod/.../art/shapes/beamngEarth/bne_au_fence_<kind>.dae   boundary fences, 5.4 m long along x (server buildings.au_fence)
    mod/.../art/shapes/beamngEarth/bne_au_pole_conc.dae      a concrete pole with a cross-arm (server furniture.pole_group)
    mod/.../art/shapes/beamngEarth/bne_au_colorbond.png      the sheet's flat ribs (greyscale: the material's colour tints it)
    mod/.../art/shapes/beamngEarth/bne_au_paling.png         timber paling boards with their gaps
    mod/.../art/shapes/beamngEarth/au_streetscape.materials.json   fence / pole materials (loaded by trees.lua)
    mod/.../art/shapes/beamngEarth/au_roads.materials.json         the kerb channel's concrete (loaded by roads.lua)

    python tools/build_au_streetscape.py

Fences (reference: fork/QLD_REFERENCE.md, Brisbane streets): a Colorbond fence is a flat-ribbed steel sheet 1.8 m high between
steel posts, a capping along its top, in the range's muted colours (Monument, Woodland Grey, Surfmist, Basalt, Shale Grey,
Dune); a timber paling fence is 1.8 m of upright boards (new treated pine, or weathered grey) on rails and posts. Each model is
a 5.4 m panel centred on its origin, z up from the ground (0.1 m sunk for a slope), the panel's thin side along y, ready for
the stock fence's rows: group, x, y, z, yaw along the road, scale = height / 1.8.
Pole: Z up from the ground, the cross-arm along x (across the road, the wires along y: furniture.WIRE_ARMS 0.8 m either side
of the pole at WIRE_Z 7.9 m), the pole 9.2 m above the ground.
"""
import json
import math
import os
import uuid

import numpy as np
from PIL import Image

from build_rail_crossing_props import OUT, TEX_DIR, Mesh, write_dae

SEG_M = 5.4                       # buildings.FENCE_SEG_M
FENCE_H = 1.8                     # the models' height above the ground (trees.lua's catalog height)
SINK = 0.1                        # m below the ground
SHEET_T = 0.03                    # m: the sheet's thickness (a rib's depth)
POST = 0.075                      # m: a steel post
CB_REPEAT_M = 0.4                 # the ribs' texture repeat along the sheet
PAL_REPEAT_M = 0.4                # ... and the paling's (four boards of 0.1 m)

# Colorbond colours (the range's published swatches, sRGB 0-1, a little muted for the game's lighting)
COLORBOND = {
    "monument": ((0.20, 0.20, 0.21), "Monument"),
    "woodland": ((0.30, 0.31, 0.28), "Woodland Grey"),
    "surfmist": ((0.89, 0.88, 0.83), "Surfmist"),
    "basalt": ((0.43, 0.43, 0.44), "Basalt"),
    "shale": ((0.73, 0.74, 0.71), "Shale Grey"),
    "dune": ((0.64, 0.62, 0.56), "Dune"),
}
PALING = {"pine": (0.78, 0.60, 0.40), "grey": (0.56, 0.52, 0.47)}      # new treated pine; weathered silver-grey
COLOURS = {                       # plain materials: (base colour, metallic, roughness)
    "bne_au_conc": ((0.66, 0.66, 0.63), 0.0, 0.9),             # the poles' concrete
    "bne_au_steel": ((0.55, 0.57, 0.58), 0.7, 0.45),           # galvanised cross-arms
    "bne_au_insulator": ((0.72, 0.74, 0.76), 0.0, 0.3),
}
CHANNEL = ((0.68, 0.68, 0.65), 0.0, 0.88)    # the kerb channel's concrete (bne_au_channel)


def mat_cb(k):
    return f"bne_au_cb_{k}"


def mat_pal(k):
    return f"bne_au_pal_{k}"


def sheet_face(m, mat, x0, x1, y, z0, z1, front, repeat):
    """A textured upright face at y spanning x0..x1 and z0..z1, facing -y (front) or +y; u runs along x by `repeat` m."""
    q = [(x0, y, z0), (x1, y, z0), (x1, y, z1), (x0, y, z1)]
    uv = [(x0 / repeat, 0.0), (x1 / repeat, 0.0), (x1 / repeat, 1.0), (x0 / repeat, 1.0)]
    if front:
        m.face(mat, q, uv)
    else:
        m.face(mat, q[::-1], uv[::-1])


def flat_box(m, mat, c, half):
    """A box of one plain material whose faces take a single texel of the sheet (a flat tint)."""
    c, h = np.asarray(c, np.float64), np.asarray(half, np.float64)
    m.box(mat, c, h)
    # (Mesh.box's default uvs are arbitrary: point every vertex of this box's faces at the texture's middle)
    for i in range(len(m.uv) - 24, len(m.uv)):
        m.uv[i] = (0.5, 0.5)


def colorbond(k):
    """A Colorbond panel: the ribbed sheet, steel posts at both ends and the middle, a capping, in colour k."""
    m = Mesh(4)
    mat = mat_cb(k)
    z0, z1 = -SINK, FENCE_H - 0.045
    h = SEG_M / 2
    sheet_face(m, mat, -h, h, -SHEET_T / 2, z0, z1, True, CB_REPEAT_M)
    sheet_face(m, mat, -h, h, SHEET_T / 2, z0, z1, False, CB_REPEAT_M)
    for sx in (-1.0, 1.0):                        # the sheet's ends
        m.face(mat, [(sx * h, -SHEET_T / 2, z0), (sx * h, SHEET_T / 2, z0), (sx * h, SHEET_T / 2, z1),
                     (sx * h, -SHEET_T / 2, z1)][::1 if sx > 0 else -1], [(0.5, 0.5)] * 4)
    for x in (-h + POST / 2, 0.0, h - POST / 2):      # posts, a hair proud of the sheet each side
        flat_box(m, mat, (x, 0.0, (FENCE_H - SINK) / 2 - 0.02), (POST / 2, POST / 2 + 0.01, (FENCE_H + SINK) / 2 - 0.02))
    flat_box(m, mat, (0.0, 0.0, FENCE_H - 0.0225), (h, 0.032, 0.0225))      # the capping along the top
    return m


def paling(k):
    """A timber paling panel: boards, two rails on its +y side, posts, a capping."""
    m = Mesh(4)
    mat, rail = mat_pal(k), mat_pal(k + "_rail")
    z0, z1 = -SINK, FENCE_H - 0.07
    h = SEG_M / 2
    t = 0.0225                                    # half the boards' thickness
    sheet_face(m, mat, -h, h, -t, z0, z1, True, PAL_REPEAT_M)
    sheet_face(m, mat, -h, h, t, z0, z1, False, PAL_REPEAT_M)
    for sx in (-1.0, 1.0):
        m.face(rail, [(sx * h, -t, z0), (sx * h, t, z0), (sx * h, t, z1), (sx * h, -t, z1)][::1 if sx > 0 else -1],
               [(0.5, 0.5)] * 4)
    for z in (0.35, FENCE_H - 0.4):                # rails behind the boards
        flat_box(m, rail, (0.0, t + 0.02, z), (h, 0.02, 0.035))
    for x in (-h + 0.05, 0.0, h - 0.05):           # posts
        flat_box(m, rail, (x, t + 0.05, (FENCE_H - SINK) / 2 - 0.05), (0.05, 0.05, (FENCE_H + SINK) / 2 - 0.05))
    flat_box(m, rail, (0.0, 0.0, FENCE_H - 0.035), (h, 0.045, 0.035))      # the capping
    return m


def fence_collision():
    m = Mesh(4)
    m.box("bne_au_conc", (0.0, 0.0, (FENCE_H - SINK) / 2), (SEG_M / 2, 0.04, (FENCE_H + SINK) / 2))
    return m


def frustum(m, mat, z0, z1, r0, r1, segs=12):
    """A tapered upright shaft (closed at the top)."""
    ang = [2 * math.pi * i / segs for i in range(segs)]
    for i in range(segs):
        a, b = ang[i], ang[(i + 1) % segs]
        m.face(mat, [(r0 * math.cos(a), r0 * math.sin(a), z0), (r0 * math.cos(b), r0 * math.sin(b), z0),
                     (r1 * math.cos(b), r1 * math.sin(b), z1), (r1 * math.cos(a), r1 * math.sin(a), z1)],
               [(0.5, 0.5)] * 4)
    m.face(mat, [(r1 * math.cos(a), r1 * math.sin(a), z1) for a in ang], [(0.5, 0.5)] * segs)


def pole_conc():
    """A concrete pole: 9.2 m above the ground (0.3 m sunk), tapering from 0.17 to 0.095 m, a galvanised cross-arm 1.9 m
    long at 7.7 m with three pin insulators at the wires' places (0.8 m either side and the middle, 7.9 m up)."""
    m = Mesh(12)
    frustum(m, "bne_au_conc", -0.3, 9.2, 0.17, 0.095)
    flat_box(m, "bne_au_steel", (0.0, 0.0, 7.65), (0.95, 0.045, 0.05))
    for x in (-0.8, 0.0, 0.8):
        m.cylinder("bne_au_insulator", x, 0.0, 7.70, 7.93, 0.03)
    return m


def pole_collision():
    m = Mesh(4)
    m.box("bne_au_conc", (0.0, 0.0, 4.45), (0.17, 0.17, 4.75))
    return m


def textures():
    """The two sheets, greyscale (the material's baseColorFactor tints them), v up the sheet: Collada's v is up, rows here
    run bottom to top."""
    # Colorbond: four flat ribs across CB_REPEAT_M (each 0.1 m: a pan, a raised rib with a light and a dark flank)
    W = 64
    row = []
    for c in range(W):
        p = c % 16
        row.append(236 if p < 10 else 252 if p == 10 else 246 if p == 11 else 206 if p == 12 else 188 if p == 13 else 226)
    img = Image.new("RGB", (W, 4))
    img.putdata([(v, v, v) for v in row] * 4)
    img.save(os.path.join(OUT, "bne_au_colorbond.png"))
    # paling: boards of 0.1 m (32 px of 128), a dark gap between them, grain lines along each and a lighter or darker tone
    rng = np.random.RandomState(5)
    W, H = 128, 256
    a = np.zeros((H, W), np.float64)
    for b in range(4):
        tone = 0.86 + 0.12 * rng.rand()
        cols = slice(b * 32 + 1, b * 32 + 31)
        grain = 1.0 + 0.05 * np.sin(np.arange(H)[:, None] * 0.07 + rng.rand() * 6) + 0.04 * rng.randn(1, 30)
        a[:, cols] = tone * grain
        a[:, b * 32:b * 32 + 1] = 0.45
        a[:, b * 32 + 31:b * 32 + 32] = 0.45
    g = np.clip(a * 255, 0, 255).astype(np.uint8)
    Image.fromarray(np.stack([g, g, g], 2)).save(os.path.join(OUT, "bne_au_paling.png"))


def materials():
    pid = lambda n: str(uuid.uuid5(uuid.NAMESPACE_URL, "beamng-earth/" + n))
    out = {}

    def add(name, stage):
        out[name] = {"name": name, "mapTo": name, "class": "Material", "version": 1.5, "persistentId": pid(name),
                     "Stages": [stage, {}, {}, {}]}
    for k, (col, _n) in COLORBOND.items():
        add(mat_cb(k), {"baseColorMap": f"{TEX_DIR}/bne_au_colorbond.png", "baseColorFactor": [*col, 1.0],
                        "metallicFactor": 0.35, "roughnessFactor": 0.5})
    for k, col in PALING.items():
        add(mat_pal(k), {"baseColorMap": f"{TEX_DIR}/bne_au_paling.png", "baseColorFactor": [*col, 1.0],
                         "metallicFactor": 0.0, "roughnessFactor": 0.9})
        add(mat_pal(k + "_rail"), {"baseColorFactor": [*[c * 0.8 for c in col], 1.0], "metallicFactor": 0.0,
                                   "roughnessFactor": 0.9})
    for k in ("bne_au_conc", "bne_au_steel", "bne_au_insulator"):
        col, met, rough = COLOURS[k]
        add(k, {"baseColorFactor": [*col, 1.0], "metallicFactor": met, "roughnessFactor": rough})
    with open(os.path.join(OUT, "au_streetscape.materials.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(out, fh, indent=2)
    col, met, rough = CHANNEL
    ch = {"bne_au_channel": {"name": "bne_au_channel", "mapTo": "bne_au_channel", "class": "Material", "version": 1.5,
                             "persistentId": pid("bne_au_channel"),
                             "Stages": [{"baseColorFactor": [*col, 1.0], "metallicFactor": met, "roughnessFactor": rough},
                                        {}, {}, {}],
                             "translucent": True, "castShadows": False}}      # (translucent like the stock gutter: drawn over the asphalt)
    with open(os.path.join(OUT, "au_roads.materials.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(ch, fh, indent=2)


def main():
    textures()
    materials()
    models = {f"bne_au_fence_{k}": (colorbond(k), fence_collision()) for k in COLORBOND}
    models.update({f"bne_au_fence_paling_{k}": (paling(k), fence_collision()) for k in PALING})
    models["bne_au_pole_conc"] = (pole_conc(), pole_collision())
    for name, (m, col) in models.items():
        path = os.path.join(OUT, name + ".dae")
        write_dae(path, name, [(4, m)], col)
        print(path, os.path.getsize(path), "bytes", sum(len(t) for t in m.tri.values()), "triangles")


if __name__ == "__main__":
    main()
