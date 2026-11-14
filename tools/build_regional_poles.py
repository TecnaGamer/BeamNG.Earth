"""Build the original recessed-concrete pole prototype; no Blender or external assets.

    python tools/build_regional_poles.py

Metres, Z up, ground origin, local X across the road, cable along local Y.
Blind pockets on both broad faces leave a solid central web: these are real
recesses, not black rectangles or transparent holes. Dimensions are a visual
prototype based on the supplied photographs, not a manufacturer's specification.
Uses the repository's existing COLLADA/LOD/collision exporter.
"""
import argparse
import json
from pathlib import Path
import sys
import uuid

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import poleprofiles  # noqa: E402
from build_rail_crossing_props import Mesh, write_dae  # noqa: E402

OUT = ROOT / "mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/regional_poles"
VFS = "/levels/beamng_earth/art/shapes/beamngEarth/regional_poles"
NAME = "bne_pole_concrete_recessed"
CONCRETE, STEEL, DARK = "bne_pole_concrete", "bne_pole_steel", "bne_pole_clamp"
H = poleprofiles.RECESSED.height
POCKETS = tuple((round(0.65 + i * 0.55, 4), round(1.06 + i * 0.55, 4)) for i in range(13))
DEPTH = 0.06
COLOURS = {CONCRETE: (0.64, 0.63, 0.60), STEEL: (0.35, 0.38, 0.39), DARK: (0.07, 0.075, 0.08)}


def section(z):
    """Half width (X), half thickness (Y) of the tapered mast."""
    return 0.20 - 0.08 * z / H, 0.13 - 0.045 * z / H


def face(m, mat, pts, outward):
    pts = np.asarray(pts, np.float64)
    if np.dot(np.cross(pts[1] - pts[0], pts[2] - pts[0]), outward) < 0:
        pts = pts[::-1]
    # UV metres: grain repeats, without a photographed/third-party texture.
    n = np.asarray(outward)
    axes = (0, 2) if abs(n[1]) >= abs(n[0]) else (1, 2)
    m.face(mat, pts, [(p[axes[0]], p[axes[1]]) for p in pts])


def mast(m, pockets=True, bevel=True):
    bands = sorted({0.0, H} | ({z for p in POCKETS for z in p} if pockets else set()))
    for a, b in zip(bands[:-1], bands[1:]):
        wa, da = section(a)
        wb, db = section(b)
        # Two narrow outside faces, divided at the pockets' ends.
        for s in (-1, 1):
            face(m, CONCRETE, [(s * wa, -da, a), (s * wb, -db, b),
                              (s * wb, db, b), (s * wa, da, a)], (s, 0, 0))
        ia, ib = wa - 0.055, wb - 0.055
        inset = pockets and (a, b) in POCKETS
        for s in (-1, 1):
            # Split even the solid bands into three strips: no T-junctions.
            xa, xb = (-wa, -ia, ia, wa), (-wb, -ib, ib, wb)
            for k in (0, 2) if inset else range(3):
                face(m, CONCRETE, [(xa[k], s * da, a), (xa[k + 1], s * da, a),
                                  (xb[k + 1], s * db, b), (xb[k], s * db, b)], (0, s, 0))
            if inset:
                outer = np.array([(-ia, s * da, a), (ia, s * da, a),
                                  (ib, s * db, b), (-ib, s * db, b)])
                # A slightly bevelled lip, represented by the four walls to the back.
                lip = 0.012 if bevel else 0.0
                inner = np.array([(-ia + lip, s * (da - DEPTH), a + lip),
                                  (ia - lip, s * (da - DEPTH), a + lip),
                                  (ib - lip, s * (db - DEPTH), b - lip),
                                  (-ib + lip, s * (db - DEPTH), b - lip)])
                for k, direction in enumerate(((0, 0, 1), (-1, 0, 0), (0, 0, -1), (1, 0, 0))):
                    j = (k + 1) % 4
                    face(m, CONCRETE, [outer[k], outer[j], inner[j], inner[k]], direction)
                face(m, CONCRETE, inner, (0, s, 0))
    for z, sign in ((0.0, -1), (H, 1)):
        w, d = section(z)
        xx = (-w, -w + 0.055, w - 0.055, w)
        for a, b in zip(xx[:-1], xx[1:]):
            face(m, CONCRETE, [(a, -d, z), (b, -d, z), (b, d, z), (a, d, z)], (0, 0, sign))


def band(m, z):
    w, d = section(z)
    for s in (-1, 1):
        m.box(STEEL, (0, s * (d + 0.004), z), (w + 0.008, 0.004, 0.018))
        m.box(STEEL, (s * (w + 0.004), 0, z), (0.004, d, 0.018))


def model(detail):
    """0 near / 1 middle / 2 far; all retain the same wire attachment point."""
    m = Mesh(8 if detail == 0 else 4)
    mast(m, pockets=detail < 2, bevel=detail == 0)
    ax, ay, az = poleprofiles.RECESSED.wire_anchors[0]
    # A compact steel bracket and insulated bundle clamp, not a US cross-arm.
    m.box(STEEL, (ax / 2, ay, az - 0.13), (ax / 2 + 0.025, 0.035, 0.025))
    m.box(STEEL, (ax, ay, az - 0.065), (0.025, 0.035, 0.065))
    m.box(DARK, (ax, ay, az), (0.045, 0.07, 0.045))
    if detail < 2:
        band(m, az - 0.13)
    if detail == 0:
        for z in (1.7, 3.4, 5.1, 6.8):
            band(m, z)
        # Plain service box: no claimed utility branding/identification label.
        _, d = section(1.75)
        m.box(DARK, (0, -d - 0.035, 1.75), (0.065, 0.035, 0.11))
    return m


def collision():
    """One convex tapered hull, 12 triangles, with no expensive pocket collision."""
    m = Mesh(4)
    w0, d0 = section(0)
    w1, d1 = section(H)
    for s in (-1, 1):
        face(m, CONCRETE, [(s * w0, -d0, 0), (s * w0, d0, 0),
                          (s * w1, d1, H), (s * w1, -d1, H)], (s, 0, 0))
        face(m, CONCRETE, [(-w0, s * d0, 0), (w0, s * d0, 0),
                          (w1, s * d1, H), (-w1, s * d1, H)], (0, s, 0))
    for z, w, d, sign in ((0, w0, d0, -1), (H, w1, d1, 1)):
        face(m, CONCRETE, [(-w, -d, z), (w, -d, z), (w, d, z), (-w, d, z)], (0, 0, sign))
    return m


def materials(out):
    rng = np.random.default_rng(46871)
    grain = np.clip(0.91 + rng.normal(0, 0.025, (128, 128)), 0.8, 1.0)
    tex = (np.repeat(grain[:, :, None], 3, axis=2) * 255).astype(np.uint8)
    Image.fromarray(tex).save(out / "concrete_grain.png")
    data = {}
    for name, colour in COLOURS.items():
        stage = {"baseColorFactor": [*colour, 1.0], "metallicFactor": 0.65 if name == STEEL else 0.0,
                 "roughnessFactor": 0.9 if name == CONCRETE else 0.65}
        if name == CONCRETE:
            stage["baseColorMap"] = VFS + "/concrete_grain.png"
        data[name] = {"name": name, "mapTo": name, "class": "Material", "version": 1.5,
                      "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, "beamng-earth:regional-poles:" + name)),
                      "Stages": [stage], "doubleSided": False}
    (out / "main.materials.json").write_bytes((json.dumps(data, indent=2) + "\n").encode("utf-8"))


def build(out=OUT):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    lods = [(96, model(0)), (24, model(1)), (2, model(2))]
    path = out / (NAME + ".dae")
    write_dae(path, NAME, lods, collision())
    xml = path.read_text(encoding="utf-8").replace("tools/build_rail_crossing_props.py", "tools/build_regional_poles.py")
    path.write_bytes(xml.encode("utf-8"))
    materials(out)
    return {"height_m": H, "pockets_per_face": len(POCKETS),
            "lod_triangles": [sum(map(len, m.tri.values())) for _, m in lods],
            "collision_triangles": sum(map(len, collision().tri.values())),
            "wire_anchors": poleprofiles.RECESSED.wire_anchors}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=OUT)
    print(json.dumps(build(ap.parse_args().out), indent=2))
