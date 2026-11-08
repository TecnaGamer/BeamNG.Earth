"""build_au_tboard.py: Australia's T-junction hazard board (mod/.../art/shapes/beamngEarth/bne_au_tboard.dae,
au_tboard.materials.json), placed by server/tboards.py (fork: Queensland).

    python tools/build_au_tboard.py

The wide black-and-white chevron board across the far side of a T: 3.6 m x 0.6 m, white with black chevrons pointing
outward from the middle (to the left over the left half, to the right over the right), its lower edge 1.0 m over the ground,
on four galvanised posts behind it (sunk 0.3 m), its back the same grey. Geometry only (the chevrons are black polygons just
in front of the white, no texture). The face looks towards -Y, the traffic it warns, the board centred on the origin (the
layout of build_au_warnsigns.py, so the server's rows are alike).
"""
import json
import os
import uuid

from build_rail_crossing_props import OUT, Mesh, write_dae

WIDTH, HEIGHT = 3.6, 0.6            # m: the board
LOW = 1.0                           # m: its lower edge over the ground
TOP = LOW + HEIGHT                  # m: the model's height
POST_R, POST_SINK = 0.04, 0.3
POSTS = (-1.5, -0.5, 0.5, 1.5)      # m: along the board
FRONT, BACK = -0.045, -0.025        # m: the board's front and back faces' Y (the posts just behind it)
INK = -0.004                        # m: the black stands this far in front of the white
CHEVRON = (0.30, 0.26)              # m: a chevron's width (along the board) and half its height
PITCH, STRIPE = 0.40, 0.10          # m: chevrons' spacing along the board, and a stripe's width along it
COLOURS = {
    "bne_au_tboard_white": ((0.92, 0.92, 0.90), 0.1, 0.3),      # retroreflective white
    "bne_au_tboard_black": ((0.02, 0.02, 0.02), 0.0, 0.5),
    "bne_au_tboard_steel": ((0.62, 0.64, 0.65), 0.7, 0.45),     # the posts and the board's back
}
WHITE, BLACK, STEEL = COLOURS


def chevrons(m):
    """Black chevrons over the front, each a pair of slanted stripes meeting at its outer tip: '<' over the left half, '>'
    over the right half, the nearest pair clear of the middle."""
    w, h = CHEVRON
    z0 = LOW + HEIGHT / 2
    for side in (-1.0, 1.0):
        x = PITCH / 2
        while x + w <= WIDTH / 2 - 0.05:
            tip = side * (x + w)                    # (the tip points outward, its arms run back towards the middle)
            far = side * x
            for vs in (-1.0, 1.0):
                pts = [(tip, 0.0), (tip - side * STRIPE, 0.0), (far - side * STRIPE, vs * h), (far, vs * h)]
                pts = [(u, FRONT + INK, z0 + v) for u, v in pts]
                # (counter-clockwise seen from the front, -Y looking at +Y with x to the right)
                area = sum(a[0] * b[2] - b[0] * a[2] for a, b in zip(pts, pts[1:] + pts[:1]))
                m.face(BLACK, pts if area > 0 else pts[::-1])
            x += PITCH


def board(near):
    m = Mesh(6)
    z0, z1 = LOW, TOP
    x = WIDTH / 2
    m.face(WHITE, [(-x, FRONT, z0), (x, FRONT, z0), (x, FRONT, z1), (-x, FRONT, z1)])
    m.face(STEEL, [(x, BACK, z0), (-x, BACK, z0), (-x, BACK, z1), (x, BACK, z1)])
    for a, b in (((-x, z0), (x, z0)), ((x, z0), (x, z1)), ((x, z1), (-x, z1)), ((-x, z1), (-x, z0))):
        m.face(STEEL, [(a[0], BACK, a[1]), (b[0], BACK, b[1]), (b[0], FRONT, b[1]), (a[0], FRONT, a[1])])
    if near:
        chevrons(m)
    for px in POSTS:
        m.cylinder(STEEL, px, BACK + POST_R, -POST_SINK, z1, POST_R, caps=False)
    return m


def materials():
    out = {}
    pid = lambda n: str(uuid.uuid5(uuid.NAMESPACE_URL, "beamng-earth/" + n))
    for k, (col, met, rough) in COLOURS.items():
        out[k] = {"name": k, "mapTo": k, "class": "Material", "version": 1.5, "persistentId": pid(k),
                  "Stages": [{"baseColorFactor": [*col, 1.0], "metallicFactor": met, "roughnessFactor": rough},
                             {}, {}, {}]}
    with open(os.path.join(OUT, "au_tboard.materials.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)


def main():
    materials()
    name = "bne_au_tboard"
    path = os.path.join(OUT, name + ".dae")
    lods = [(32, board(True)), (4, board(False))]
    write_dae(path, name, lods, Mesh(4))        # (no collision mesh: a board on posts, nothing to hit)
    print(path, os.path.getsize(path), "bytes", sum(len(t) for t in lods[0][1].tri.values()), "triangles",
          round(TOP, 2), "m")


if __name__ == "__main__":
    main()
