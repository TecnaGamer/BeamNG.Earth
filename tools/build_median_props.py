"""build_median_props.py: the median cable barrier (mod/.../art/shapes/beamngEarth/bne_cable_barrier.dae and
median_props.materials.json): the stock maps have none (2026-10-28; server medians.py puts it in grass medians).

    python3 tools/build_median_props.py

One 3 m module along X (-1.5 .. 1.5, the guard rails' module length: trees.lua's rows place it like Italy's rail), Z
up from the ground at its origin: a galvanised I-section post at its middle (sunk 0.3 m, a white reflector band near
its top) and four steel ropes the module's length (high-tension cable barriers: four ropes 0.48-0.96 m up, posts
about every 3 m). Two levels of detail (the far one the posts and ropes as single quads) and a collision box the
ropes' height.

Also Sweden's kantstolpe (bne_kantstolpe.dae, server kantstolpar.py), as VGU 2024 12.2.2.1 (TRVINFRA-00396, remiss version
0.9): white, 1.05 m high, 0.10 m wide, a 0.25 m black band round its 0.80 m high reflectors: facing the traffic on its
right a 45 x 160 mm one, on the other face two of 60 mm, 100 mm apart. Nothing to hit: it gives way (K237854).

And Sweden's give-way sign B1 (bne_giveway_b1.dae, server furniture.junction_controls), yellow where the stock prop is
white. TSFS 2019:74 3 kap. 1 §: S1, across its rounded corners, 0.90 m (Normal, a road not a motorway). The face is
Transportstyrelsen's artwork b1-1.png on bne_se_signs.png (tools/build_swedish_signs.py), whose outline this follows:
456 x 405 px, the sides extended to sharp corners 507 px. 1 kap. 13 §: 1.0-2.0 m up to its lower edge.
"""
import json
import math
import os
import uuid

from build_rail_crossing_props import OUT, Mesh, write_dae

ROPES = (0.48, 0.64, 0.80, 0.96)    # m up
ROPE_R = 0.011                       # m: a 3/4 in rope
POST_H = 1.05
POST_SINK = 0.3
HALF_L = 1.5
COLOURS = {
    "bne_cable_steel": ((0.58, 0.59, 0.60), 0.85, 0.5),      # galvanised
    "bne_cable_reflector": ((0.92, 0.92, 0.90), 0.0, 0.3),   # its white reflective band
    "bne_post_white": ((0.86, 0.87, 0.85), 0.0, 0.55),
    "bne_post_black": ((0.02, 0.02, 0.02), 0.0, 0.55),
}
B1_W = 0.90                         # m across the rounded corners
B1_S = B1_W * 507 / 456             # m: the sides extended to sharp corners
B1_R = (B1_S - B1_W) / (2 * (3 ** 0.5 - 1))     # m: the corners' radius, from the two
B1_LOW = 1.5                        # m up to the point
B1_TOP = B1_LOW + B1_S * 3 ** 0.5 / 2 - B1_R    # (bne_giveway_b1's height: trees.lua's catalog)
B1_DEPTH = 0.02                     # m: the plate's folded edge
B1_POST = 0.03                      # m: its radius
B1_CELL = (640.0, 640.0)            # px of 1024: the middle of the artwork on bne_se_signs.png (cell 2, 2)
B1_PX = (456 / B1_W) * 244 / 458    # px of the sheet to the metre (its longer side, 458 px, drawn at 244)


def module(detail=True):
    m = Mesh(6)
    if detail:
        # an I-section post: two flanges and a web
        m.box("bne_cable_steel", (0.0, 0.0, (POST_H - POST_SINK) / 2), (0.004, 0.035, (POST_H + POST_SINK) / 2))
        for x in (-0.03, 0.03):
            m.box("bne_cable_steel", (x, 0.0, (POST_H - POST_SINK) / 2), (0.004, 0.04, (POST_H + POST_SINK) / 2))
        m.box("bne_cable_reflector", (0.0, 0.0, POST_H - 0.12), (0.036, 0.042, 0.05))
        for z in ROPES:     # ropes as square tubes (four faces, ends left open: the next module goes on)
            for a, b in (((-1, -1), (-1, 1)), ((-1, 1), (1, 1)), ((1, 1), (1, -1)), ((1, -1), (-1, -1))):
                y0, z0 = a[0] * ROPE_R, z + a[1] * ROPE_R
                y1, z1 = b[0] * ROPE_R, z + b[1] * ROPE_R
                m.face("bne_cable_steel", [(-HALF_L, y0, z0), (HALF_L, y0, z0), (HALF_L, y1, z1), (-HALF_L, y1, z1)])
                m.face("bne_cable_steel", [(-HALF_L, y1, z1), (HALF_L, y1, z1), (HALF_L, y0, z0), (-HALF_L, y0, z0)])
    else:
        # far: the post a slab, each rope a thin vertical strip seen from either side
        m.box("bne_cable_steel", (0.0, 0.0, (POST_H - POST_SINK) / 2), (0.03, 0.035, (POST_H + POST_SINK) / 2))
        for z in ROPES:
            for side in (1, -1):
                pts = [(-HALF_L, 0.0, z - 0.015), (HALF_L, 0.0, z - 0.015), (HALF_L, 0.0, z + 0.015),
                       (-HALF_L, 0.0, z + 0.015)]
                m.face("bne_cable_steel", pts if side > 0 else list(reversed(pts)))
    return m


def collision():
    """What a car hits: a box the ropes' length and height."""
    m = Mesh(4)
    m.box("bne_cable_steel", (0.0, 0.0, (ROPES[-1] + 0.05) / 2), (HALF_L, 0.05, (ROPES[-1] + 0.05) / 2))
    return m


def post(near):
    """A kantstolpe: Z up from the ground, the traffic it faces coming along +X (its reflectors' face -X)."""
    m = Mesh(4)
    for z0, z1, mat in ((-POST_SINK, 0.675, "bne_post_white"), (0.675, 0.925, "bne_post_black"),
                        (0.925, 1.05, "bne_post_white")):
        m.box(mat, (0.0, 0.0, (z0 + z1) / 2), (0.025, 0.05, (z1 - z0) / 2))
    if near:
        m.box("bne_cable_reflector", (-0.026, 0.0, 0.80), (0.001, 0.0225, 0.08))
        for z in (0.72, 0.88):
            m.box("bne_cable_reflector", (0.026, 0.0, z), (0.001, 0.03, 0.03))
    return m


def b1_outline(segs):
    """B1's edge, counter-clockwise from the front: (x, z) from the middle of its height, the point down."""
    h = 3 ** 0.5 / 2
    out = []
    for ca, cz, a0 in ((0.0, -2 / 3, 210), (0.5, 1 / 3, 330), (-0.5, 1 / 3, 90)):       # the corners, each arc's start
        n = math.hypot(ca * B1_S, cz * B1_S * h)
        cx = ca * B1_S * (1 - 2 * B1_R / n)
        cy = cz * B1_S * h * (1 - 2 * B1_R / n)
        out += [(cx + B1_R * math.cos(math.radians(a0 + 120 * k / segs)),
                 cy + B1_R * math.sin(math.radians(a0 + 120 * k / segs))) for k in range(segs + 1)]
    mid = (B1_S * h / 3 + B1_R - 2 * B1_S * h / 3) / 2      # (of the true outline: a far model's corners are cut)
    return [(x, z - mid) for x, z in out]


def giveway(near):
    """B1: Z up from the ground, point down, its face towards -Y (the traffic it faces), on a post behind it."""
    edge = b1_outline(6 if near else 1)
    mid = B1_TOP - (B1_TOP - B1_LOW) / 2
    front, back = -B1_POST - B1_DEPTH, -B1_POST
    m = Mesh(8)
    m.face("bne_se_signs", [(x, front, mid + z) for x, z in edge],
           [((B1_CELL[0] + x * B1_PX) / 1024, 1 - (B1_CELL[1] - z * B1_PX) / 1024) for x, z in edge])
    m.face("bne_cable_steel", [(x, back, mid + z) for x, z in reversed(edge)])
    if near:
        for (x0, z0), (x1, z1) in zip(edge, edge[1:] + edge[:1]):
            m.face("bne_cable_steel", [(x0, back, mid + z0), (x1, back, mid + z1), (x1, front, mid + z1),
                                       (x0, front, mid + z0)])
        m.cylinder("bne_cable_steel", 0.0, 0.0, -POST_SINK, B1_TOP - 0.05, B1_POST, caps=False)
    return m


def materials():
    out = {}
    pid = lambda n: str(uuid.uuid5(uuid.NAMESPACE_URL, "beamng-earth/" + n))
    for k, (col, met, rough) in COLOURS.items():
        out[k] = {"name": k, "mapTo": k, "class": "Material", "version": 1.5, "persistentId": pid(k),
                  "Stages": [{"baseColorFactor": [*col, 1.0], "metallicFactor": met, "roughnessFactor": rough},
                             {}, {}, {}]}
    with open(os.path.join(OUT, "median_props.materials.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)


def main():
    materials()
    path = os.path.join(OUT, "bne_cable_barrier.dae")
    lods = [(48, module(True)), (6, module(False))]
    write_dae(path, "bne_cable_barrier", lods, collision())
    print(path, os.path.getsize(path), "bytes", sum(len(t) for t in lods[0][1].tri.values()), "triangles")
    path = os.path.join(OUT, "bne_kantstolpe.dae")
    write_dae(path, "bne_kantstolpe", [(24, post(True)), (4, post(False))], Mesh(4))
    print(path, os.path.getsize(path), "bytes")
    path = os.path.join(OUT, "bne_giveway_b1.dae")
    pole = Mesh(4)
    pole.box("bne_cable_steel", (0.0, 0.0, B1_TOP / 2), (0.04, 0.04, B1_TOP / 2))
    write_dae(path, "bne_giveway_b1", [(32, giveway(True)), (4, giveway(False))], pole)
    print(path, os.path.getsize(path), "bytes", round(B1_TOP, 2), "m")


if __name__ == "__main__":
    main()
