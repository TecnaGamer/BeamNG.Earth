"""Build the European street lamp posts: a tapered galvanised steel tube and a concrete post, each with an up-swept
bracket arm and a cobra-head luminaire (the user, 2026-11-07: "the lamps are not the local ones": the American
eca_lamppost stood on every street). No Blender, no outside assets.

    python tools/build_city_lamps.py

Metres, Z up, ground origin, the arm reaching out along local +X (toward the road: furniture.py rows' yaw turns +X to
the carriageway, as for the stock lamp post). Catalog height 9.0 m. The luminaire's lens is the separate lamplens /
lampglow rows (furniture.lamp_lenses at LAMP_HEADS' point), lit at night.
"""
import json
import math
from pathlib import Path
import sys
import uuid

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "tools"))
from build_rail_crossing_props import Mesh, write_dae  # noqa: E402

OUT = ROOT / "mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/city_lamps"
VFS = "/levels/beamng_earth/art/shapes/beamngEarth/city_lamps"
STEEL, CONCRETE, HEAD, BRACKET = "bne_lamp_steel", "bne_lamp_concrete", "bne_lamp_head", "bne_lamp_bracket"
COLOURS = {STEEL: (0.52, 0.55, 0.56), CONCRETE: (0.64, 0.63, 0.60), HEAD: (0.30, 0.32, 0.33),
           BRACKET: (0.40, 0.42, 0.43)}
H = 9.0                                     # the catalog height (trees.lua CATALOG)
ARM = ((0.0, 8.15), (0.55, 8.40), (1.10, 8.60), (1.60, 8.72), (2.00, 8.78))     # (x, z) of the bracket's axis
HEAD_AT = (2.10, 8.84)                      # the luminaire's middle (x, z); its underside is HEAD_AT z - HEAD_HALF[2]
HEAD_HALF = (0.52, 0.16, 0.07)
# the lens (lamp_lenses): under the luminaire's middle
LENS_AT = (HEAD_AT[0], 0.0, HEAD_AT[1] - HEAD_HALF[2])


def ring(r, z, segs, phase=0.0):
    return [(r * math.cos(phase + 2 * math.pi * i / segs), r * math.sin(phase + 2 * math.pi * i / segs), z)
            for i in range(segs)]


def frustum(m, mat, z0, z1, r0, r1, segs, caps=True):
    """A tapered round tube between heights z0 and z1 (flat faces: segs sides)."""
    a, b = ring(r0, z0, segs), ring(r1, z1, segs)
    for i in range(segs):
        j = (i + 1) % segs
        m.face(mat, [a[i], a[j], b[j], b[i]])
    if caps:
        m.face(mat, list(reversed(a)))
        m.face(mat, b)


def rect_frustum(m, mat, z0, z1, w0, d0, w1, d1):
    """A tapered rectangular post: half width (X) and half thickness (Y) at both ends."""
    a = [(-w0, -d0, z0), (w0, -d0, z0), (w0, d0, z0), (-w0, d0, z0)]
    b = [(-w1, -d1, z1), (w1, -d1, z1), (w1, d1, z1), (-w1, d1, z1)]
    for i in range(4):
        j = (i + 1) % 4
        m.face(mat, [a[i], a[j], b[j], b[i]])
    m.face(mat, list(reversed(a)))
    m.face(mat, b)


def arm(m, mat, r, pts=ARM):
    """The bracket: boxes along the axis' segments."""
    for (x0, z0), (x1, z1) in zip(pts[:-1], pts[1:]):
        d = np.array([x1 - x0, 0.0, z1 - z0])
        ln = float(np.linalg.norm(d))
        u = d / ln
        side = np.array([0.0, 1.0, 0.0])
        up = np.cross(u, side)
        m.box(mat, ((x0 + x1) / 2, 0.0, (z0 + z1) / 2), (ln / 2 + 0.005, r, r), [u, side, up])


def luminaire(m, detail):
    cx, cz = HEAD_AT
    hx, hy, hz = HEAD_HALF
    m.box(HEAD, (cx, 0.0, cz), (hx, hy, hz))
    if detail < 2:
        # the tapered nose over the road and the cap over the bracket
        m.box(HEAD, (cx + hx + 0.06, 0.0, cz - 0.01), (0.06, hy * 0.7, hz * 0.8))
        m.box(HEAD, (cx - hx * 0.6, 0.0, cz + hz + 0.02), (0.14, 0.09, 0.03))


def steel(detail):
    m = Mesh(12 if detail == 0 else 6)
    segs = 12 if detail == 0 else 8 if detail == 1 else 6
    frustum(m, STEEL, 0.0, 8.30, 0.135, 0.062, segs)
    if detail < 2:
        frustum(m, STEEL, 0.0, 0.55, 0.19, 0.19, segs)                    # the base sleeve
        m.box(BRACKET, (0.14, 0.0, 1.0), (0.012, 0.075, 0.24))             # the door
    arm(m, BRACKET if detail < 2 else STEEL, 0.034)
    luminaire(m, detail)
    return m


def concrete(detail):
    m = Mesh(8)
    rect_frustum(m, CONCRETE, 0.0, 8.35, 0.17, 0.12, 0.10, 0.075)
    if detail < 2:
        for z in (1.6, 3.2, 4.8, 6.4):                                      # shuttering seams
            w = 0.17 - 0.07 * z / 8.35
            d = 0.12 - 0.045 * z / 8.35
            m.box(CONCRETE, (0, 0, z), (w + 0.004, d + 0.004, 0.012))
        m.box(HEAD, (0.0, -0.13, 1.6), (0.07, 0.012, 0.11))                 # a plain service plate
    # a steel clamp ring where the bracket leaves the post
    m.box(BRACKET, (0.0, 0.0, 8.15), (0.12, 0.09, 0.05))
    arm(m, BRACKET, 0.032)
    luminaire(m, detail)
    return m


def collision(kind):
    """One convex hull of the post (what a car hits: the arm is overhead)."""
    m = Mesh(6)
    if kind == "steel":
        frustum(m, STEEL, -0.2, 8.30, 0.14, 0.062, 8)
    else:
        rect_frustum(m, CONCRETE, -0.2, 8.35, 0.17, 0.12, 0.10, 0.075)
    return m


MODELS = {"bne_lamp_eu_steel": (steel, "steel"), "bne_lamp_eu_concrete": (concrete, "concrete")}


def materials(out):
    data = {}
    for name, colour in COLOURS.items():
        stage = {"baseColorFactor": [*colour, 1.0], "metallicFactor": 0.7 if name in (STEEL, BRACKET) else 0.0,
                 "roughnessFactor": 0.85 if name == CONCRETE else 0.55}
        if name == CONCRETE:
            stage["baseColorMap"] = "/levels/beamng_earth/art/shapes/beamngEarth/regional_poles/concrete_grain.png"
        data[name] = {"name": name, "mapTo": name, "class": "Material", "version": 1.5,
                      "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, "beamng-earth:city-lamps:" + name)),
                      "Stages": [stage], "doubleSided": False}
    (out / "main.materials.json").write_bytes((json.dumps(data, indent=2) + "\n").encode("utf-8"))


def build(out=OUT):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    info = {}
    for name, (fn, kind) in MODELS.items():
        lods = [(96, fn(0)), (24, fn(1)), (2, fn(2))]
        path = out / (name + ".dae")
        write_dae(path, name, lods, collision(kind))
        xml = path.read_text(encoding="utf-8").replace("tools/build_rail_crossing_props.py", "tools/build_city_lamps.py")
        path.write_bytes(xml.encode("utf-8"))
        info[name] = {"lod_triangles": [sum(map(len, m.tri.values())) for _, m in lods],
                      "collision_triangles": sum(map(len, collision(kind).tri.values()))}
    materials(out)
    return info


if __name__ == "__main__":
    print(json.dumps(build(), indent=1))
    print("lens at", LENS_AT)
