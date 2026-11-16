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

And Germany's junction signs (bne_de_<group>.dae, one per server/designs.py GROUPS: Zeichen 205, 206, 301, 306, 307,
some over a 1002 plate, in Größe 1, 2 and 3), at their true size on one galvanised post; their faces are
tools/build_de_signs.py's sheet, which this maps them on (run that first when the faces change).
"""
import json
import math
import os
import uuid

import build_de_signs
import designs          # (server/: build_de_signs puts it on the path)
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


# A19-1, the moose (server/sesigns.py moose_signs): B1's triangle point up, its artwork beside B1's on bne_se_signs.png
# (cell 3, 2), the same size (TSFS 2019:74 2 kap. 1 §: a warning sign's "normal" side 900 mm, B1's too)
A19_CELL = (896.0, 640.0)
A19_TOP = B1_TOP                    # (its lower edge B1's point: 1.5 m)


def moose(near):
    """A19: Z up from the ground, point up, its face towards -Y, on a post behind it."""
    edge = [(x, -z) for x, z in reversed(b1_outline(6 if near else 1))]
    mid = B1_LOW + (B1_TOP - B1_LOW) / 2
    front, back = -B1_POST - B1_DEPTH, -B1_POST
    m = Mesh(8)
    m.face("bne_se_signs", [(x, front, mid + z) for x, z in edge],
           [((A19_CELL[0] + x * B1_PX) / 1024, 1 - (A19_CELL[1] - z * B1_PX) / 1024) for x, z in edge])
    m.face("bne_cable_steel", [(x, back, mid + z) for x, z in reversed(edge)])
    if near:
        for (x0, z0), (x1, z1) in zip(edge, edge[1:] + edge[:1]):
            m.face("bne_cable_steel", [(x0, back, mid + z0), (x1, back, mid + z1), (x1, front, mid + z1),
                                       (x0, front, mid + z0)])
        m.cylinder("bne_cable_steel", 0.0, 0.0, -POST_SINK, A19_TOP - 0.05, B1_POST, caps=False)
    return m


# Sweden's signs of server/sesigns.py, on tools/build_swedish_signs.py's second sheet: picture's middle in px of 1024, its
# drawn size in px, the plate in m and its shape. Sizes: "normal" of TSFS 2019:74 (F16-F18: 8 kap. 2 §, 1.20 m high; C1:
# 4 kap. 1 § 0.60 m; B3: 3 kap. 1 § 0.60 m; E1, E2: 6 kap. 1 §, 0.90 m wide); F2 has none, its text's scale made it 2 m.
SE_PLATES = {"f17": ((128, 128), (174, 244), 0.855, 1.20, "plate"), "f18_11": ((384, 128), (244, 244), 1.20, 1.20, "plate"),
             "f18_12": ((640, 128), (244, 189), 1.55, 1.20, "plate"),
             "f18_21": ((896, 128), (244, 189), 1.55, 1.20, "plate"),
             "t400": ((128, 384), (244, 93), 0.855, 0.326, "plate"), "t300": ((384, 384), (244, 93), 0.855, 0.326, "plate"),
             "t200": ((640, 384), (244, 93), 0.855, 0.326, "plate"), "c1": ((896, 384), (244, 244), 0.60, 0.60, "disc"),
             "b3": ((128, 640), (243, 244), 0.60, 0.60, "plate"), "e1": ((384, 640), (183, 244), 0.90, 1.20, "plate"),
             "e2": ((128, 896), (183, 244), 0.90, 1.20, "plate"), "f2": ((768, 768), (483, 500), 1.93, 2.00, "plate")}
SE_LOW, SE_LOW_PLATED, SE_GAP = 1.5, 1.2, 0.05      # m: a sign's lower edge (1 kap. 13, 17 §§: 1.0-2.0 m), with T2 under it
# group of the rows (guardrails.GROUPS) -> plates bottom up, lower edge of the lowest, both faces drawn, poles' x
SE_SIGNS = {"se_f17": (("f17",), SE_LOW, False, (0.0,)), "se_f17_400": (("t400", "f17"), SE_LOW_PLATED, False, (0.0,)),
            "se_f17_300": (("t300", "f17"), SE_LOW_PLATED, False, (0.0,)),
            "se_f17_200": (("t200", "f17"), SE_LOW_PLATED, False, (0.0,)),
            "se_f18_11": (("f18_11",), SE_LOW, False, (0.0,)), "se_f18_12": (("f18_12",), SE_LOW, False, (-0.45, 0.45)),
            "se_f18_21": (("f18_21",), SE_LOW, False, (-0.45, 0.45)), "se_f2": (("f2",), SE_LOW, False, (-0.6, 0.6)),
            "se_c1": (("c1",), SE_LOW, False, (0.0,)), "se_b3": (("b3",), 2.0, True, (0.0,)),
            "se_e1": (("e1",), SE_LOW, False, (0.0,)), "se_e2": (("e2",), SE_LOW, False, (0.0,))}
SE_R = 0.03                 # m: the plates' corners


def outline(w, h, shape, segs):
    """A plate's edge, counter-clockwise from the front: (x, z) from its middle."""
    if shape == "disc":
        n = segs * 4
        return [(w / 2 * math.cos(2 * math.pi * k / n), w / 2 * math.sin(2 * math.pi * k / n)) for k in range(n)]
    out = []
    for cx, cz, a0 in ((w / 2 - SE_R, -h / 2 + SE_R, -90), (w / 2 - SE_R, h / 2 - SE_R, 0),
                       (-w / 2 + SE_R, h / 2 - SE_R, 90), (-w / 2 + SE_R, -h / 2 + SE_R, 180)):
        for k in range(segs + 1):
            a = math.radians(a0 + 90 * k / max(segs, 1))
            out.append((cx + SE_R * math.cos(a), cz + SE_R * math.sin(a)))
    return out


def se_sign(group, near):
    """One of SE_SIGNS: Z up from the ground, its face towards -Y (the traffic it faces), on its poles behind it."""
    plates, low, both, poles = SE_SIGNS[group]
    m = Mesh(8)
    z = low
    for name in plates:
        (cx, cy), (pw, ph), w, h, shape = SE_PLATES[name]
        edge = outline(w, h, shape, 2 if near else 1)
        mid = z + h / 2
        uv = [((cx + x / w * pw) / 1024, 1 - (cy - q / h * ph) / 1024) for x, q in edge]
        front, back = -B1_POST - B1_DEPTH, -B1_POST
        m.face("bne_se_signs2", [(x, front, mid + q) for x, q in edge], uv)
        if both:        # (a face each way: its picture reads the same from behind)
            m.face("bne_se_signs2", [(-x, back, mid + q) for x, q in edge], uv)
        else:
            m.face("bne_cable_steel", [(x, back, mid + q) for x, q in reversed(edge)])
        if near:
            for (x0, z0), (x1, z1) in zip(edge, edge[1:] + edge[:1]):
                m.face("bne_cable_steel", [(x0, back, mid + z0), (x1, back, mid + z1), (x1, front, mid + z1),
                                           (x0, front, mid + z0)])
        z += h + SE_GAP
    if near:
        for x in poles:
            m.cylinder("bne_cable_steel", x, 0.0, -POST_SINK, z - SE_GAP - 0.05, B1_POST, caps=False)
    return m


def se_height(group):
    plates, low, _, _ = SE_SIGNS[group]
    return low + sum(SE_PLATES[n][3] for n in plates) + SE_GAP * (len(plates) - 1)


def se_pole(group):
    plates, low, _, poles = SE_SIGNS[group]
    m = Mesh(4)
    for x in poles:
        m.box("bne_cable_steel", (x, 0.0, se_height(group) / 2), (0.04, 0.04, se_height(group) / 2))
    return m


# Germany's junction signs (server/designs.py GROUPS: a face alone or over its 1002 plate, Größe 1, 2 or 3), one mesh a
# group at its true size: trees.lua's catalog has designs.HEIGHTS, so rows draw them at scale 1. Their faces are
# tools/build_de_signs.py's sheet (material bne_de_signs, mapped by its uv()); group -> [(picture, shape, size m, the
# middle's height)] bottom up, the lowest edge 2 m up (VwV-StVO zu den §§ 39 bis 43 Rn. 42). Posts, frames and backs
# grey (Rn. 24: "Pfosten, Rahmen und Schilderrückseiten sollen grau sein"): galvanised.
DE_SIGNS = {g: designs.panels(g) for g in designs.GROUPS}


def de_sign(group, near):
    """One of DE_SIGNS: Z up from the ground, its faces towards -Y (the traffic it faces), on one post behind them up
    to a quarter of the top face's height over its middle."""
    front, back = -(designs.POST_R_M + designs.DEPTH_M), -designs.POST_R_M
    m = Mesh(8)
    for pic, shape, size, mid in DE_SIGNS[group]:
        # (far: each rounded corner one cut, its points the arc's ends, so that outline lies a hair inside the near
        # one's extremes: designs.HEIGHTS / WIDTHS are the near LOD's, and at 4 px the difference is not seen)
        edge = designs.edge(shape, size, 4 if near else 1)
        f = designs.SIZE[shape][1] / size       # (the sheet's picture is Größe 2)
        m.face("bne_de_signs", [(x, front, mid + z) for x, z in edge],
               [build_de_signs.uv(pic, x * f, z * f) for x, z in edge])
        m.face("bne_cable_steel", [(x, back, mid + z) for x, z in reversed(edge)])
        if near:
            for (x0, z0), (x1, z1) in zip(edge, edge[1:] + edge[:1]):
                m.face("bne_cable_steel", [(x0, back, mid + z0), (x1, back, mid + z1), (x1, front, mid + z1),
                                           (x0, front, mid + z0)])
    if near:
        _, shape, size, mid = DE_SIGNS[group][-1]
        _, z0, _, z1 = designs.extent(shape, size)
        m.cylinder("bne_cable_steel", 0.0, 0.0, -designs.SINK_M, mid + (z0 + z1) / 2 + (z1 - z0) / 4, designs.POST_R_M)
    return m


def de_pole(group):
    """What a car hits: the post's box, se_pole's."""
    m = Mesh(4)
    h = designs.HEIGHTS[group]
    m.box("bne_cable_steel", (0.0, 0.0, h / 2), (0.04, 0.04, h / 2))
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
    for group in SE_SIGNS:
        name = "bne_" + group
        path = os.path.join(OUT, name + ".dae")
        write_dae(path, name, [(32, se_sign(group, True)), (4, se_sign(group, False))], se_pole(group))
        print(path, os.path.getsize(path), "bytes", round(se_height(group), 2), "m")
    path = os.path.join(OUT, "bne_se_a19.dae")
    pole = Mesh(4)
    pole.box("bne_cable_steel", (0.0, 0.0, A19_TOP / 2), (0.04, 0.04, A19_TOP / 2))
    write_dae(path, "bne_se_a19", [(32, moose(True)), (4, moose(False))], pole)
    print(path, os.path.getsize(path), "bytes", round(A19_TOP, 2), "m")
    for group in DE_SIGNS:
        name = "bne_" + group
        path = os.path.join(OUT, name + ".dae")
        write_dae(path, name, [(32, de_sign(group, True)), (4, de_sign(group, False))], de_pole(group))
    print(len(DE_SIGNS), "German junction signs", "bne_de_*.dae")


if __name__ == "__main__":
    main()
