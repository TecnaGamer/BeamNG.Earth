#!/usr/bin/env python3
"""make_localpoi_models.py: the models and materials of the local POIs (server localpoi.py).

    python3 tools/make_localpoi_models.py

Writes mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/localpoi/:
  bne_lp_table_forest.dae   a forest picnic table with a bench each side (1.8 x 1.5 m, 0.77 m high)
  bne_lp_altan.dae          a wooden picnic hut, open at its +Y end (3.8 x 4.0 m, ridge 3.2 m)
  bne_lp_firepit.dae        a stone fire ring (1.4 m across) and a log bench each side
  bne_lp_cross_stone / _wood / _metal, bne_lp_shrine_column
                            roadside crosses (3.7 / 3.75 / 2.6 m) and a Bozi muka shrine (3.4 m), front towards -Y
  bne_lp_gp_post            a hiking guidepost's galvanised tube (2.6 m); its plates and arrows are the sign tile's
                            (server localpoisign.py)
  bne_lp_stele / _obelisk / _boulder / _statue / _stone_cross
                            memorials: a stele (1.9 m), an obelisk (3.5 m), a boulder with a plaque (1.3 m), a saint's statue
                            on a pedestal (3.75 m), a low stone cross (1.05 m); the front towards -Y
  main.materials.json       bne_lp_* materials; a map is used when its file is beside it (<name>.color.png,
                            <name>.normal.png, <name>.data.png), else a flat colour
All centred on the footprint, Z up from the ground. UVs are metric (1 repeat = 1 m, along a board's length), so the
tiling textures of tools/make_localpoi_textures.py fit any size. Collision: Colmesh boxes (no one drives through a hut).
"""
import json
import math
import os
import sys
import uuid

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from build_rail_crossing_props import Mesh, write_dae  # noqa: E402

OUT = os.path.join(HERE, "..", "mod", "beamng_earth", "levels", "beamng_earth", "art", "shapes", "beamngEarth",
                   "localpoi")
VFS = "/levels/beamng_earth/art/shapes/beamngEarth/localpoi/"

# material -> (flat colour (sRGB), roughness, double sided)
MATERIALS = {
    "bne_lp_wood": ((0.36, 0.30, 0.24), 0.88, False),        # weathered grey-brown boards
    "bne_lp_shingle": ((0.30, 0.34, 0.30), 0.85, False),     # moss-grey roof shingle
    "bne_lp_stone": ((0.56, 0.52, 0.44), 0.9, False),        # sandstone
    "bne_lp_ash": ((0.10, 0.09, 0.09), 1.0, False),          # fire-pit ash and embers
    "bne_lp_oak": ((0.30, 0.23, 0.17), 0.88, False),         # dark oak beams (wayside crosses)
    "bne_lp_iron": ((0.07, 0.07, 0.08), 0.6, False),         # black-painted cast iron
    "bne_lp_tin": ((0.30, 0.38, 0.33), 0.7, False),          # old painted tin (a wooden cross' small roof and plate)
    "bne_lp_paint": ((0.80, 0.81, 0.80), 0.6, False),        # white-painted steel
    "bne_lp_niche": ((0.04, 0.04, 0.04), 0.9, False),        # a shrine's dark niche, an inscription panel
    "bne_lp_galv": ((0.62, 0.64, 0.65), 0.45, False),        # galvanised steel (a hiking guidepost's tube)
    "bne_lp_granite": ((0.18, 0.18, 0.19), 0.35, False),     # polished dark granite (memorial plaques)
}


def _faces():
    return (((-1, -1, -1), (-1, -1, 1), (-1, 1, 1), (-1, 1, -1)),
            ((1, -1, -1), (1, 1, -1), (1, 1, 1), (1, -1, 1)),
            ((-1, -1, -1), (1, -1, -1), (1, -1, 1), (-1, -1, 1)),
            ((-1, 1, -1), (-1, 1, 1), (1, 1, 1), (1, 1, -1)),
            ((-1, -1, -1), (-1, 1, -1), (1, 1, -1), (1, -1, -1)),
            ((-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1)))


# materials whose picture has its grain along V (the oak beam, top to bottom), not along U (the boards)
GRAIN_ALONG_V = {"bne_lp_oak"}


def uv_for(pts, mat=None):
    """Metric UVs of a quad: u along its longer edge (a board's grain runs along its length), 1 repeat = 1 m; for a
    GRAIN_ALONG_V material the longer edge is V."""
    p = [np.asarray(q, np.float64) for q in pts]
    e1 = float(np.linalg.norm(p[1] - p[0]))
    e2 = float(np.linalg.norm(p[3] - p[0]))
    if e1 >= e2:
        uv = [(0.0, 0.0), (e1, 0.0), (e1, e2), (0.0, e2)]
    else:
        uv = [(0.0, 0.0), (0.0, e1), (e2, e1), (e2, 0.0)]
    if mat in GRAIN_ALONG_V:
        uv = [(v, u) for u, v in uv]
    return uv


def block(m, mat, x0, x1, y0, y1, z0, z1):
    """An axis-aligned box with metric UVs."""
    cx, cy, cz = (x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2
    hx, hy, hz = (x1 - x0) / 2, (y1 - y0) / 2, (z1 - z0) / 2
    for f in _faces():
        pts = [(cx + a * hx, cy + b * hy, cz + c * hz) for a, b, c in f]
        m.face(mat, pts, uv_for(pts, mat))


def quad(m, mat, a, b, c, d):
    """A quad, counter-clockwise seen from the front, metric UVs."""
    pts = [a, b, c, d]
    m.face(mat, pts, uv_for(pts, mat))


def prism_x(m, mat, x0, x1, y, z, r, sides=10):
    """A log along X (a regular prism), from x0 to x1, axis at (y, z)."""
    ang = [2 * math.pi * i / sides for i in range(sides)]
    ring = [(y + r * math.cos(a), z + r * math.sin(a)) for a in ang]
    for i in range(sides):
        (ya, za), (yb, zb) = ring[i], ring[(i + 1) % sides]
        pts = [(x0, ya, za), (x0, yb, zb), (x1, yb, zb), (x1, ya, za)]
        m.face(mat, pts, uv_for(pts, mat))
    m.face(mat, [(x1, py, pz) for py, pz in ring])
    m.face(mat, [(x0, py, pz) for py, pz in reversed(ring)])


# ------------------------------------------------------------------------------------------ the forest table

TABLE_X, TABLE_Y, TABLE_Z = 1.8, 1.5, 0.77


def _table(m, ox=0.0, oy=0.0, far=False):
    """Table and benches centred at (ox, oy). Table top z 0.72-0.77 (0.8 m wide), bench seats z 0.42-0.46."""
    hx = TABLE_X / 2
    if far:
        block(m, "bne_lp_wood", ox - hx, ox + hx, oy - 0.4, oy + 0.4, 0.70, 0.77)
        for s in (-1, 1):
            block(m, "bne_lp_wood", ox - hx, ox + hx, oy + s * 0.62 - 0.14, oy + s * 0.62 + 0.14, 0.40, 0.46)
        return
    # the top: four boards with a 1 cm gap
    w = 0.19
    for i in range(4):
        y0 = oy - 0.4 + i * (w + 0.0133)
        block(m, "bne_lp_wood", ox - hx, ox + hx, y0, y0 + w, 0.72, 0.77)
    for s in (-1, 1):                                   # a bench each side: 0.28 m seat on two legs
        yc = oy + s * 0.62
        block(m, "bne_lp_wood", ox - hx, ox + hx, yc - 0.14, yc + 0.14, 0.42, 0.46)
        for sx in (-1, 1):
            block(m, "bne_lp_wood", ox + sx * 0.62 - 0.04, ox + sx * 0.62 + 0.04, yc - 0.10, yc + 0.10, 0.0, 0.42)
    for sx in (-1, 1):                                  # trestle: two posts and a cross board to the benches
        xc = ox + sx * 0.62
        for s in (-1, 1):
            block(m, "bne_lp_wood", xc - 0.05, xc + 0.05, oy + s * 0.30 - 0.05, oy + s * 0.30 + 0.05, 0.0, 0.72)
        block(m, "bne_lp_wood", xc - 0.03, xc + 0.03, oy - 0.77, oy + 0.77, 0.30, 0.38)   # ties the legs


def table_forest():
    near, far, col = Mesh(), Mesh(), Mesh()
    _table(near)
    _table(far, far=True)
    block(col, "bne_lp_wood", -TABLE_X / 2, TABLE_X / 2, -TABLE_Y / 2, TABLE_Y / 2, 0.0, TABLE_Z)
    return [(60, near), (8, far)], col


# ------------------------------------------------------------------------------------------------- the hut

ALT_X, ALT_Y = 3.0, 4.0            # the posts' footprint
POST = 0.14
EAVE_Z, RIDGE_Z = 2.2, 3.2
OVER_X, OVER_Y = 0.45, 0.35         # the roof's overhang beyond the posts


def _altan(m, far=False):
    hx, hy = ALT_X / 2, ALT_Y / 2
    rx, ry = hx + OVER_X, hy + OVER_Y
    # roof: two slopes of shingle over a wood underside, gable ends in wood
    for s in (-1, 1):
        top = [(s * rx, -ry, EAVE_Z), (0.0, -ry, RIDGE_Z), (0.0, ry, RIDGE_Z), (s * rx, ry, EAVE_Z)]
        if s > 0:
            top = top[::-1]
        quad(m, "bne_lp_shingle", *top)
        if not far:
            under = [(s * rx, -ry, EAVE_Z - 0.05), (0.0, -ry, RIDGE_Z - 0.05), (0.0, ry, RIDGE_Z - 0.05),
                     (s * rx, ry, EAVE_Z - 0.05)]
            if s < 0:
                under = under[::-1]
            quad(m, "bne_lp_wood", *under)
    for y, flip in ((-ry, False), (ry, True)):          # gable triangles, both faces (UV by position)
        tri = [(-rx, y, EAVE_Z - 0.05), (rx, y, EAVE_Z - 0.05), (0.0, y, RIDGE_Z - 0.05)]
        uvs = [(px + rx, pz - EAVE_Z) for px, _py, pz in tri]
        if flip:
            tri, uvs = tri[::-1], uvs[::-1]
        m.face("bne_lp_wood", tri, uvs)
        m.face("bne_lp_wood", tri[::-1], uvs[::-1])
    for sx in (-1, 1):                                   # four posts
        for sy in (-1, 1):
            block(m, "bne_lp_wood", sx * hx - POST / 2, sx * hx + POST / 2, sy * hy - POST / 2, sy * hy + POST / 2,
                  0.0, EAVE_Z)
    if far:
        return
    # beams under the eaves
    for sx in (-1, 1):
        block(m, "bne_lp_wood", sx * hx - 0.06, sx * hx + 0.06, -hy, hy, EAVE_Z - 0.20, EAVE_Z - 0.05)
    for sy in (-1, 1):
        block(m, "bne_lp_wood", -hx, hx, sy * hy - 0.06, sy * hy + 0.06, EAVE_Z - 0.20, EAVE_Z - 0.05)
    # railings: both long sides and the -Y end, a top and a middle rail (the +Y end stays open)
    for z0, z1 in ((0.90, 0.99), (0.48, 0.55)):
        for sx in (-1, 1):
            block(m, "bne_lp_wood", sx * hx - 0.04, sx * hx + 0.04, -hy, hy, z0, z1)
        block(m, "bne_lp_wood", -hx, hx, -hy - 0.04, -hy + 0.04, z0, z1)
    # the table and its benches inside, along X
    _table_across(m)


def _table_across(m):
    """The table inside the hut, its length along Y (so the benches sit along the long walls)."""
    sub = Mesh()
    _table(sub)
    # rotate the sub-mesh a quarter turn (x, y) -> (-y, x) and merge it
    k = len(m.p)
    for p, n, uv in zip(sub.p, sub.n, sub.uv):
        m.p.append(np.array([-p[1], p[0], p[2]]))
        m.n.append(np.array([-n[1], n[0], n[2]]))
        m.uv.append(uv)
    for mat, tris in sub.tri.items():
        m.tri.setdefault(mat, []).extend((a + k, b + k, c + k) for a, b, c in tris)


def altan():
    near, far, col = Mesh(), Mesh(), Mesh()
    _altan(near)
    _altan(far, far=True)
    hx, hy = ALT_X / 2, ALT_Y / 2
    for sx in (-1, 1):
        for sy in (-1, 1):
            block(col, "bne_lp_wood", sx * hx - POST / 2, sx * hx + POST / 2, sy * hy - POST / 2, sy * hy + POST / 2,
                  0.0, EAVE_Z)
    for sx in (-1, 1):
        block(col, "bne_lp_wood", sx * hx - 0.05, sx * hx + 0.05, -hy, hy, 0.45, 1.0)
    block(col, "bne_lp_wood", -hx, hx, -hy - 0.05, -hy + 0.05, 0.45, 1.0)
    block(col, "bne_lp_wood", -0.75, 0.75, -0.9, 0.9, 0.0, 0.77)                  # the table inside
    block(col, "bne_lp_wood", -(hx + OVER_X), hx + OVER_X, -(hy + OVER_Y), hy + OVER_Y, EAVE_Z, RIDGE_Z)   # the roof
    return [(100, near), (10, far)], col


# --------------------------------------------------------------------------------------------- the fire pit

PIT_R = 0.62


def _firepit(m, far=False):
    rng = np.random.default_rng(44)
    n = 8 if far else 11
    for i in range(n):
        a = 2 * math.pi * (i + rng.uniform(-0.12, 0.12)) / n
        r = PIT_R + rng.uniform(-0.03, 0.04)
        h = rng.uniform(0.16, 0.26)
        sx, sy = rng.uniform(0.15, 0.22), rng.uniform(0.12, 0.18)
        yaw = a + math.pi / 2 + rng.uniform(-0.3, 0.3)
        ax = [[math.cos(yaw), math.sin(yaw), 0.0], [-math.sin(yaw), math.cos(yaw), 0.0], [0.0, 0.0, 1.0]]
        m.box("bne_lp_stone", (r * math.cos(a), r * math.sin(a), h / 2), (sx, sy, h / 2), ax)
    ang = [2 * math.pi * i / 16 for i in range(16)]
    m.face("bne_lp_ash", [(0.46 * math.cos(a), 0.46 * math.sin(a), 0.03) for a in ang])
    if far:
        return
    for s in (-1, 1):                                    # a log each side, parallel to X
        prism_x(m, "bne_lp_wood", -0.6, 0.6, s * 1.45, 0.15, 0.15)


def firepit():
    near, far, col = Mesh(), Mesh(), Mesh()
    _firepit(near)
    _firepit(far, far=True)
    block(col, "bne_lp_stone", -0.78, 0.78, -0.78, 0.78, 0.0, 0.28)
    return [(40, near), (6, far)], col


# ------------------------------------------------------------------------- wayside crosses and shrines (stage C)
# Centred, ground at z 0 with the foot sunk below it (a slope), the front towards -Y (localpoi turns it to the road).

def pyramid(m, mat, cx, cy, z0, hw, hd, z1):
    """A four-sided pyramid roof: base half sizes hw (x), hd (y) at z0, apex at z1."""
    c = [(cx - hw, cy - hd), (cx + hw, cy - hd), (cx + hw, cy + hd), (cx - hw, cy + hd)]
    apex = (cx, cy, z1)
    for k in range(4):
        a, b = c[k], c[(k + 1) % 4]
        pts = [(a[0], a[1], z0), (b[0], b[1], z0), apex]
        m.face(mat, pts, [(0.0, 0.0), (math.hypot(b[0] - a[0], b[1] - a[1]), 0.0), (0.4, z1 - z0)])


def gable(m, mat, cx, cy, z0, hw, hd, z1):
    """A gable roof, ridge along Y: slopes down to +-hw at z0, half length hd; the gable ends in the same material."""
    quad(m, mat, (cx - hw, cy - hd, z0), (cx, cy - hd, z1), (cx, cy + hd, z1), (cx - hw, cy + hd, z0))
    quad(m, mat, (cx + hw, cy + hd, z0), (cx, cy + hd, z1), (cx, cy - hd, z1), (cx + hw, cy - hd, z0))
    for y, flip in ((cy - hd, False), (cy + hd, True)):
        tri = [(cx - hw, y, z0), (cx + hw, y, z0), (cx, y, z1)]
        uvs = [(px - cx + hw, pz - z0) for px, _py, pz in tri]
        if flip:
            tri, uvs = tri[::-1], uvs[::-1]
        m.face(mat, tri, uvs)


def cross_stone(far=False):
    """A sandstone wayside cross: step, plinth, pedestal with an inscription panel, cast-iron cross (3.7 m)."""
    m = Mesh()
    block(m, "bne_lp_stone", -0.65, 0.65, -0.65, 0.65, -0.20, 0.15)                 # the step
    block(m, "bne_lp_stone", -0.43, 0.43, -0.43, 0.43, 0.15, 0.60)                   # plinth
    block(m, "bne_lp_stone", -0.30, 0.30, -0.30, 0.30, 0.60, 1.80)                   # pedestal
    block(m, "bne_lp_stone", -0.37, 0.37, -0.37, 0.37, 1.80, 1.90)                   # cornice
    block(m, "bne_lp_iron", -0.05, 0.05, -0.03, 0.03, 1.90, 3.70)                    # the cross: upright ...
    block(m, "bne_lp_iron", -0.35, 0.35, -0.03, 0.03, 3.00, 3.10)                    # ... and arm
    if not far:
        block(m, "bne_lp_niche", -0.15, 0.15, -0.315, -0.30, 0.95, 1.55)             # the panel (front, -Y)
        block(m, "bne_lp_stone", -0.34, 0.34, -0.34, 0.34, 0.54, 0.60)               # a moulding under the pedestal
        block(m, "bne_lp_iron", -0.10, 0.10, -0.04, 0.04, 3.70, 3.76)                # finial
        block(m, "bne_lp_stone", -0.06, 0.06, -0.045, -0.03, 2.62, 3.02)             # (the corpus' place: a pale figure)
    return m


def cross_stone_lods():
    col = Mesh()
    block(col, "bne_lp_stone", -0.65, 0.65, -0.65, 0.65, -0.20, 1.90)
    block(col, "bne_lp_iron", -0.35, 0.35, -0.05, 0.05, 1.90, 3.70)
    return [(100, cross_stone()), (10, cross_stone(far=True))], col


def cross_wood(far=False):
    """A wooden wayside cross under a small tin roof, a tin plate with the figure on its front (3.78 m)."""
    m = Mesh()
    block(m, "bne_lp_oak", -0.09, 0.09, -0.09, 0.09, -0.40, 3.50)                    # the post, sunk 0.4 m
    block(m, "bne_lp_oak", -0.75, 0.75, -0.07, 0.07, 2.62, 2.80)                     # the arm
    gable(m, "bne_lp_tin", 0.0, 0.0, 3.50, 0.34, 0.30, 3.78)
    if not far:
        block(m, "bne_lp_tin", -0.20, 0.20, -0.105, -0.09, 2.05, 2.55)               # the plate (front, -Y)
        block(m, "bne_lp_oak", -0.12, 0.12, -0.12, 0.12, -0.05, 0.20)                # a collar at the foot
    return m


def cross_wood_lods():
    col = Mesh()
    block(col, "bne_lp_oak", -0.10, 0.10, -0.10, 0.10, 0.0, 3.5)
    block(col, "bne_lp_oak", -0.75, 0.75, -0.08, 0.08, 2.62, 2.80)
    return [(100, cross_wood()), (10, cross_wood(far=True))], col


def cross_metal(far=False):
    """A metal roadside cross on a stone base (2.65 m)."""
    m = Mesh()
    block(m, "bne_lp_stone", -0.28, 0.28, -0.28, 0.28, -0.20, 0.55)
    block(m, "bne_lp_paint", -0.04, 0.04, -0.04, 0.04, 0.55, 2.60)
    block(m, "bne_lp_paint", -0.45, 0.45, -0.035, 0.035, 1.90, 1.98)
    if not far:
        block(m, "bne_lp_niche", -0.08, 0.08, -0.058, -0.04, 1.30, 1.62)             # a small plate
        block(m, "bne_lp_paint", -0.07, 0.07, -0.05, 0.05, 2.60, 2.65)
    return m


def cross_metal_lods():
    col = Mesh()
    block(col, "bne_lp_stone", -0.28, 0.28, -0.28, 0.28, -0.20, 0.55)
    block(col, "bne_lp_paint", -0.45, 0.45, -0.05, 0.05, 0.55, 2.60)
    return [(80, cross_metal()), (8, cross_metal(far=True))], col


def shrine_column(far=False):
    """A "Bozi muka" shrine: step, plinth, shaft, a lantern block with four niches, a pyramid roof and a cross (3.35 m)."""
    m = Mesh()
    block(m, "bne_lp_stone", -0.55, 0.55, -0.55, 0.55, -0.20, 0.15)
    block(m, "bne_lp_stone", -0.35, 0.35, -0.35, 0.35, 0.15, 0.55)
    block(m, "bne_lp_stone", -0.21, 0.21, -0.21, 0.21, 0.55, 1.90)
    block(m, "bne_lp_stone", -0.31, 0.31, -0.31, 0.31, 1.90, 2.50)                   # the lantern block
    block(m, "bne_lp_stone", -0.36, 0.36, -0.36, 0.36, 2.50, 2.58)                   # cornice
    pyramid(m, "bne_lp_stone", 0.0, 0.0, 2.58, 0.33, 0.33, 3.00)
    if not far:
        c = 0.31
        for x0, x1, y0, y1 in ((-0.14, 0.14, -c - 0.012, -c + 0.012), (-0.14, 0.14, c - 0.012, c + 0.012),
                               (-c - 0.012, -c + 0.012, -0.14, 0.14), (c - 0.012, c + 0.012, -0.14, 0.14)):
            block(m, "bne_lp_niche", x0, x1, y0, y1, 2.02, 2.38)
        block(m, "bne_lp_iron", -0.025, 0.025, -0.025, 0.025, 3.00, 3.35)
        block(m, "bne_lp_iron", -0.10, 0.10, -0.02, 0.02, 3.20, 3.25)
    return m


def shrine_column_lods():
    col = Mesh()
    block(col, "bne_lp_stone", -0.55, 0.55, -0.55, 0.55, -0.20, 0.15)
    block(col, "bne_lp_stone", -0.36, 0.36, -0.36, 0.36, 0.15, 3.0)
    return [(100, shrine_column()), (10, shrine_column(far=True))], col


# ----------------------------------------------------------------------------- guidepost and memorials (stages D, F)

def taper(m, mat, cx, cy, z0, z1, r0, r1, sides=8, turn=0.0):
    """A tapered prism: radius r0 at z0 to r1 at z1 (r1 = 0: a cone), `sides` sides, turned by `turn` radians."""
    a0 = [turn + 2 * math.pi * i / sides for i in range(sides)]
    lo = [(cx + r0 * math.cos(a), cy + r0 * math.sin(a), z0) for a in a0]
    hi = [(cx + r1 * math.cos(a), cy + r1 * math.sin(a), z1) for a in a0]
    for i in range(sides):
        j = (i + 1) % sides
        quad(m, mat, lo[i], lo[j], hi[j], hi[i]) if r1 > 1e-6 else m.face(
            mat, [lo[i], lo[j], hi[i]], [(0.0, 0.0), (r0, 0.0), (0.0, z1 - z0)])
    if r1 > 1e-6:
        m.face(mat, hi)
    m.face(mat, list(reversed(lo)))


def gp_post(far=False):
    """A hiking guidepost's galvanised tube (2.6 m, 60 mm): a domed cap, collars where the boards' brackets sit (the sign tile hangs
    them at 1.4-2.4 m), a foot flange on the ground and the tube sunk below it. The boards themselves are the sign tile's."""
    m = Mesh(segs=8 if far else 14)
    m.cylinder("bne_lp_galv", 0.0, 0.0, -0.3, 2.60, 0.03, caps=True)
    m.cylinder("bne_lp_galv", 0.0, 0.0, 0.0, 0.03, 0.062, caps=True)               # the foot flange
    if far:
        return m
    for r, z0, z1 in ((0.036, 2.60, 2.625), (0.026, 2.625, 2.645), (0.014, 2.645, 2.66)):    # a domed cap in three steps
        m.cylinder("bne_lp_galv", 0.0, 0.0, z0, z1, r, caps=True)
    m.cylinder("bne_lp_galv", 0.0, 0.0, 0.03, 0.06, 0.04, caps=True)
    for z in (1.30, 1.58, 1.86, 2.14, 2.42):                                        # clamp rings: a place for each board
        m.cylinder("bne_lp_galv", 0.0, 0.0, z, z + 0.012, 0.037, caps=True)
    return m


def gp_post_lods():
    col = Mesh()
    block(col, "bne_lp_galv", -0.04, 0.04, -0.04, 0.04, 0.0, 2.6)
    return [(60, gp_post()), (6, gp_post(far=True))], col


def stele(far=False):
    """A stele: a step, a slab with a stepped cap and a dark plaque on its front (-Y), 1.9 m."""
    m = Mesh()
    block(m, "bne_lp_stone", -0.75, 0.75, -0.40, 0.40, -0.10, 0.15)
    block(m, "bne_lp_stone", -0.55, 0.55, -0.26, 0.26, 0.15, 0.40)
    block(m, "bne_lp_stone", -0.45, 0.45, -0.15, 0.15, 0.40, 1.75)
    block(m, "bne_lp_stone", -0.50, 0.50, -0.19, 0.19, 1.75, 1.85)
    block(m, "bne_lp_stone", -0.36, 0.36, -0.12, 0.12, 1.85, 1.92)
    if not far:
        block(m, "bne_lp_granite", -0.30, 0.30, -0.165, -0.15, 0.80, 1.45)
    return m


def stele_lods():
    col = Mesh()
    block(col, "bne_lp_stone", -0.75, 0.75, -0.40, 0.40, -0.10, 0.15)
    block(col, "bne_lp_stone", -0.50, 0.50, -0.19, 0.19, 0.15, 1.92)
    return [(80, stele()), (8, stele(far=True))], col


def obelisk(far=False):
    """An obelisk: a base with a plaque and a square tapering shaft with a pyramid tip, 3.5 m."""
    m = Mesh()
    block(m, "bne_lp_stone", -0.65, 0.65, -0.65, 0.65, -0.10, 0.20)
    block(m, "bne_lp_stone", -0.50, 0.50, -0.50, 0.50, 0.20, 0.55)
    taper(m, "bne_lp_stone", 0.0, 0.0, 0.55, 3.15, 0.40, 0.20, sides=4, turn=math.pi / 4)
    taper(m, "bne_lp_stone", 0.0, 0.0, 3.15, 3.50, 0.20, 0.0, sides=4, turn=math.pi / 4)
    if not far:
        block(m, "bne_lp_granite", -0.26, 0.26, -0.515, -0.50, 0.28, 0.50)
    return m


def obelisk_lods():
    col = Mesh()
    block(col, "bne_lp_stone", -0.65, 0.65, -0.65, 0.65, -0.10, 0.20)
    taper(col, "bne_lp_stone", 0.0, 0.0, 0.20, 3.5, 0.5, 0.05, sides=4, turn=math.pi / 4)
    return [(80, obelisk()), (8, obelisk(far=True))], col


def boulder(far=False):
    """A memorial boulder: a few rough blocks and a dark plaque on the front (-Y), 1.3 m."""
    m = Mesh()
    ax = lambda yaw: [[math.cos(yaw), math.sin(yaw), 0.0], [-math.sin(yaw), math.cos(yaw), 0.0], [0.0, 0.0, 1.0]]
    m.box("bne_lp_stone", (0.0, 0.0, 0.50), (0.62, 0.42, 0.50), ax(0.15))
    m.box("bne_lp_stone", (-0.20, 0.05, 0.98), (0.40, 0.30, 0.28), ax(-0.35))
    m.box("bne_lp_stone", (0.34, -0.06, 0.18), (0.34, 0.30, 0.18), ax(0.5))
    if not far:
        block(m, "bne_lp_granite", -0.28, 0.18, -0.50, -0.46, 0.35, 0.78)
    return m


def boulder_lods():
    col = Mesh()
    block(col, "bne_lp_stone", -0.65, 0.65, -0.45, 0.45, 0.0, 1.3)
    return [(60, boulder()), (6, boulder(far=True))], col


def statue(far=False):
    """A saint's statue on a baroque pedestal: step, plinth, pedestal with a cornice, a robed figure with a head, a raised
    arm and a halo, 3.75 m. A simplified figure (no sculpted detail)."""
    m = Mesh()
    block(m, "bne_lp_stone", -0.55, 0.55, -0.55, 0.55, -0.10, 0.15)
    block(m, "bne_lp_stone", -0.40, 0.40, -0.40, 0.40, 0.15, 0.50)
    block(m, "bne_lp_stone", -0.28, 0.28, -0.28, 0.28, 0.50, 1.85)
    block(m, "bne_lp_stone", -0.36, 0.36, -0.36, 0.36, 1.85, 1.97)
    block(m, "bne_lp_stone", -0.30, 0.30, -0.30, 0.30, 1.97, 2.03)                    # the figure's plinth
    taper(m, "bne_lp_stone", 0.0, 0.0, 2.03, 3.05, 0.26, 0.17)                          # robe
    taper(m, "bne_lp_stone", 0.0, 0.0, 3.05, 3.30, 0.17, 0.20)                          # shoulders
    taper(m, "bne_lp_stone", 0.0, 0.0, 3.30, 3.42, 0.20, 0.06)                          # neck
    taper(m, "bne_lp_stone", 0.0, 0.0, 3.42, 3.66, 0.11, 0.09)                          # head
    block(m, "bne_lp_stone", 0.16, 0.26, -0.20, -0.08, 2.60, 3.20)                      # an arm, forward, with a crucifix
    if not far:
        block(m, "bne_lp_stone", 0.18, 0.24, -0.24, -0.16, 3.05, 3.50)
        block(m, "bne_lp_stone", 0.13, 0.29, -0.24, -0.16, 3.30, 3.36)
        taper(m, "bne_lp_iron", 0.0, 0.0, 3.70, 3.72, 0.20, 0.20, sides=10)           # the halo
        block(m, "bne_lp_granite", -0.19, 0.19, -0.295, -0.28, 0.95, 1.55)
    return m


def statue_lods():
    col = Mesh()
    block(col, "bne_lp_stone", -0.55, 0.55, -0.55, 0.55, -0.10, 0.15)
    block(col, "bne_lp_stone", -0.36, 0.36, -0.36, 0.36, 0.15, 2.03)
    taper(col, "bne_lp_stone", 0.0, 0.0, 2.03, 3.75, 0.26, 0.10)
    return [(100, statue()), (10, statue(far=True))], col


def stone_cross(far=False):
    """A low stone cross (a "smirci kriz", a penance cross), a little leaning, 1.05 m."""
    m = Mesh()
    block(m, "bne_lp_stone", -0.32, 0.32, -0.22, 0.22, -0.10, 0.10)
    block(m, "bne_lp_stone", -0.11, 0.11, -0.07, 0.07, 0.10, 0.95)
    block(m, "bne_lp_stone", -0.36, 0.36, -0.07, 0.07, 0.62, 0.84)
    block(m, "bne_lp_stone", -0.15, 0.15, -0.07, 0.07, 0.95, 1.05)
    return m


def stone_cross_lods():
    col = Mesh()
    block(col, "bne_lp_stone", -0.36, 0.36, -0.22, 0.22, -0.10, 1.05)
    return [(40, stone_cross()), (5, stone_cross(far=True))], col


MODELS = {"bne_lp_table_forest": table_forest, "bne_lp_altan": altan, "bne_lp_firepit": firepit,
          "bne_lp_cross_stone": cross_stone_lods, "bne_lp_cross_wood": cross_wood_lods,
          "bne_lp_cross_metal": cross_metal_lods, "bne_lp_shrine_column": shrine_column_lods,
          "bne_lp_gp_post": gp_post_lods, "bne_lp_stele": stele_lods, "bne_lp_obelisk": obelisk_lods,
          "bne_lp_boulder": boulder_lods, "bne_lp_statue": statue_lods, "bne_lp_stone_cross": stone_cross_lods}


def materials_json():
    out = {}
    for name, (rgb, rough, double) in MATERIALS.items():
        stage = {"baseColorFactor": [rgb[0], rgb[1], rgb[2], 1.0], "metallicFactor": 0.0, "roughnessFactor": rough}
        for key, ext in (("baseColorMap", "color"), ("normalMap", "normal"), ("roughnessMap", "data")):
            if os.path.exists(os.path.join(OUT, f"{name}.{ext}.png")):
                stage[key] = f"{VFS}{name}.{ext}.png"
                if key == "baseColorMap":
                    stage["baseColorFactor"] = [1.0, 1.0, 1.0, 1.0]
                if key == "roughnessMap":
                    stage.pop("roughnessFactor")
        out[name] = {"name": name, "mapTo": name, "class": "Material", "version": 1.5,
                     "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, "bne-localpoi/" + name)),
                     "Stages": [stage, {}, {}, {}], "doubleSided": double}
    return out


def main():
    os.makedirs(OUT, exist_ok=True)
    for name, make in MODELS.items():
        lods, col = make()
        path = os.path.join(OUT, name + ".dae")
        write_dae(path, name, lods, col)
        tris = sum(len(t) for t in lods[0][1].tri.values())
        print(path, os.path.getsize(path), "bytes,", tris, "triangles near")
    with open(os.path.join(OUT, "main.materials.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(materials_json(), fh, indent=2)
        fh.write("\n")


if __name__ == "__main__":
    main()
