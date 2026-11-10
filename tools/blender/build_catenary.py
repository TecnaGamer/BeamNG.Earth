"""build_catenary.py: the railway catenary masts (mod/.../art/shapes/beamngEarth/bne_cat_*.dae) in Blender:
  bne_cat_mast   concrete spun mast with a cantilever (ex-USSR, Poland, Czechia ...: catenary.STYLE "su")
  bne_cat_steel  steel H-beam mast with the same cantilever (Germany and most of Europe: "eu")
  bne_cat_post_su / bne_cat_post_eu   the plain posts of a rigid portal (concrete / steel); its truss girder is drawn
                 by the tile's wire mesh (catenary.portal_lines), as its length is the tracks' width

    D:/blender-4.5/blender.exe --background --python tools/blender/build_catenary.py

Origin on the ground at the mast's axis, Z up, the cantilever toward +X (the track: server/catenary.py turns the model's
+X to it). A tapered concrete mast with a foundation collar, a galvanised cantilever (horizontal tube, an inclined tie,
a registration arm lower down) and glass insulators. Nodes as the game's shapes want them (as bne_rr_*.dae:
base00 > start01 > <name><size> detail levels, Colmesh_<name>-1 for the collision); materials bne_cat_* (rail_xing /
catenary.materials.json). Pure geometry: no textures here, the UVs are cylindrical / planar for the tiles of
tools/ingest_rail_textures.py (concrete: ~2 m a tile)."""
import math
import os
import sys

import bpy
import bmesh
from mathutils import Vector

OUT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "mod", "beamng_earth", "levels",
                                       "beamng_earth", "art", "shapes", "beamngEarth"))
NAME = "bne_cat_mast"
H = 9.0                     # m: the mast's top over the ground
R0, R1 = 0.175, 0.105       # m: its radius at the foot and the top
SINK = 0.5                  # m of the mast below the ground
ARM_X = 3.3                 # m: the cantilever's reach (the mast stands 3.3 m from the track's axis)
Z_MESS, Z_REG = 7.55, 6.20  # m: the messenger's bracket and the registration arm over the ground
Z_TIE = 8.55                # m: the tie's top on the mast


def mat(name, rgb, metallic=0.0, rough=0.6):
    m = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*rgb, 1.0)
    b.inputs["Metallic"].default_value = metallic
    b.inputs["Roughness"].default_value = rough
    return m


MATS = {
    "concrete": mat("bne_cat_concrete", (0.62, 0.61, 0.58), 0.0, 0.9),
    "galv": mat("bne_cat_galv", (0.62, 0.64, 0.65), 0.85, 0.45),
    "insulator": mat("bne_cat_insulator", (0.03, 0.03, 0.035), 0.0, 0.3),   # black: composite / glazed holders
    "plate": mat("bne_cat_plate", (0.9, 0.85, 0.1), 0.0, 0.5),
    "paint": mat("bne_cat_paint", (0.44, 0.48, 0.45), 0.3, 0.7),          # 4: the EU masts' painted steel
    "foundation": mat("bne_cat_foundation", (0.54, 0.53, 0.5), 0.0, 0.95),   # 5: the cast foundation blocks
}


def new_object(name, parent=None):
    mesh = bpy.data.meshes.new(name)
    ob = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(ob)
    if parent is not None:
        ob.parent = parent
    return ob


def frustum(bm, p0, p1, r0, r1, seg, mi, cap=True, uv_len=2.0):
    """A cone frustum from p0 to p1 (Vectors), vertex rings, UVs: around 0..1, along the length in uv_len m."""
    axis = (p1 - p0)
    L = axis.length
    z = axis.normalized()
    x = z.cross(Vector((0, 0, 1))) if abs(z.z) < 0.99 else Vector((1, 0, 0))
    x.normalize()
    y = z.cross(x)
    uv = bm.loops.layers.uv.verify()
    rings = []
    for p, r in ((p0, r0), (p1, r1)):
        rings.append([bm.verts.new(p + (x * math.cos(2 * math.pi * k / seg) + y * math.sin(2 * math.pi * k / seg)) * r)
                      for k in range(seg)])
    for k in range(seg):
        k1 = (k + 1) % seg
        f = bm.faces.new((rings[0][k], rings[0][k1], rings[1][k1], rings[1][k]))
        f.material_index = mi
        f.smooth = True
        for loop, (u, v) in zip(f.loops, ((k / seg, 0), ((k + 1) / seg, 0), ((k + 1) / seg, L / uv_len), (k / seg, L / uv_len))):
            loop[uv].uv = (u, v)
    if cap:
        top = bm.faces.new(rings[1][::-1])
        top.material_index = mi
        for loop in top.loops:
            loop[uv].uv = (0.5, 0.5)
    return rings



def radius_at(z):
    return R0 + (R1 - R0) * (z - 0.35) / (H - 0.35)


def ribbed(bm, p, d, length, ts, discs=5):
    """A glass insulator string: discs of alternating radius along unit direction d, centred on p."""
    for k in range(discs):
        t0 = -length / 2 + length * k / discs
        t1 = -length / 2 + length * (k + 0.8) / discs
        r = 0.062 if k % 2 == 0 else 0.046
        frustum(bm, p + d * t0, p + d * t1, r, r * 0.92, ts, 2)
    frustum(bm, p - d * length / 2 - d * 0.02, p + d * length / 2 + d * 0.02, 0.022, 0.022, ts, 1)


def plate(bm, centre, normal, size, mi):
    """A thin square plate (the mast's warning / number plate) on the mast, its UVs the whole texture."""
    uv = bm.loops.layers.uv.verify()
    n = normal.normalized()
    x = n.cross(Vector((0, 0, 1))).normalized()
    z = Vector((0, 0, 1))
    h = size / 2
    c = centre + n * 0.006
    vs = [bm.verts.new(c + x * a * h + z * b * h) for a, b in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
    f = bm.faces.new(vs)
    f.material_index = mi
    for loop, uvc in zip(f.loops, ((0, 0), (1, 0), (1, 1), (0, 1))):
        loop[uv].uv = uvc


def joint_rings(bm, seg):
    """The concrete mast's section joints: a collar 3 cm proud at each."""
    for z in (3.2, 6.0):
        frustum(bm, Vector((0, 0, z - 0.06)), Vector((0, 0, z + 0.06)), radius_at(z) + 0.03, radius_at(z) + 0.03, seg, 0,
                uv_len=1.1)


def mast_details(bm, ts, x_face, z_top, attach_r):
    """Details common to every mast with a cantilever (detail level only): an earthing conductor down the track-side
    face to a foot clamp, bolted flange bands at the cantilever's three clamps, a saddle (a cradle) for the messenger
    over the tube's tip, and a drip collar under each insulator."""
    # earthing conductor: a thin galvanised rod from the top clamp to a clamp at the foot, on the field-side face
    frustum(bm, Vector((-x_face, 0.0, 0.5)), Vector((-x_face, 0.0, z_top)), 0.009, 0.009, 4, 1)
    for z in (0.5, 1.8, 3.6, 5.6, 7.4):           # (stand-off clips)
        frustum(bm, Vector((-x_face + 0.012, 0.0, z)), Vector((-x_face - 0.01, 0.0, z)), 0.022, 0.022, 4, 1)
    frustum(bm, Vector((-x_face, 0.0, 0.42)), Vector((-x_face, 0.0, 0.58)), 0.04, 0.04, 6, 1)     # the foot clamp
    # bolted flange bands around the mast at the three clamps
    for z in (Z_MESS, Z_REG, Z_TIE):
        for dz in (-0.07, 0.07):
            frustum(bm, Vector((0, 0, z + dz - 0.012)), Vector((0, 0, z + dz + 0.012)), attach_r + 0.035, attach_r + 0.035, 8, 1)
    # the messenger's saddle: a cradle (two cheeks and a base) on the tube's tip
    tip = Vector((ARM_X + 0.15, 0, Z_MESS + 0.30))
    box(bm, tip + Vector((0, 0, 0.05)), 0.035, 0.05, 0.012, 1)
    for sy in (-1, 1):
        box(bm, tip + Vector((0, sy * 0.045, 0.085)), 0.03, 0.008, 0.04, 1)
    # a hook-bolt under the registration arm's tip and a stay bracket from the arm to the tie
    br0, br1 = Vector((ARM_X - 1.2, 0, Z_REG)), Vector((ARM_X - 0.15, 0, Z_MESS - 0.02))
    frustum(bm, br0, br1, 0.014, 0.014, 4, 1)
    box(bm, br0, 0.03, 0.04, 0.03, 1)
    # drip collars under the two tie insulators and a bolted flange at the tube's tip
    box(bm, Vector((ARM_X + 0.15, 0, Z_MESS)), 0.025, 0.06, 0.06, 1)


def build(seg, detail):
    bm = bmesh.new()
    # the mast: foot collar (concrete foundation) and the tapered pole
    frustum(bm, Vector((0, 0, -SINK)), Vector((0, 0, 0.35)), 0.30, 0.27, max(8, seg), 5)
    frustum(bm, Vector((0, 0, 0.35)), Vector((0, 0, H)), R0, R1, seg, 0, uv_len=1.1)
    ts = max(5, seg // 2)
    # the cantilever: the horizontal tube, the registration arm (thinner), the inclined tie
    frustum(bm, Vector((0.10, 0, Z_MESS)), Vector((ARM_X + 0.15, 0, Z_MESS)), 0.036, 0.030, ts, 1)
    frustum(bm, Vector((0.10, 0.0, Z_REG)), Vector((ARM_X + 0.10, 0.0, Z_REG)), 0.028, 0.024, ts, 1)
    frustum(bm, Vector((0.10, 0, Z_TIE)), Vector((ARM_X - 0.1, 0, Z_MESS + 0.03)), 0.022, 0.022, ts, 1)
    if detail:
        joint_rings(bm, seg)
        for sx in (1.0, -1.0):          # the plates on the track side and the field side, at eye height
            plate(bm, Vector((sx * (radius_at(2.2) + 0.0), 0, 2.2)), Vector((sx, 0, 0)), 0.30, 3)
        # the cantilever's clamps on the mast and glass insulators along the tie and at the arm's tip
        for z in (Z_MESS, Z_REG, Z_TIE):
            frustum(bm, Vector((-0.06, 0, z - 0.06)), Vector((-0.06, 0, z + 0.06)), 0.12, 0.12, ts, 1)
        a_ = Vector((0.10, 0, Z_TIE))
        b_ = Vector((ARM_X - 0.1, 0, Z_MESS + 0.03))
        d_ = (b_ - a_).normalized()
        for t in (0.30, 0.62):
            ribbed(bm, a_.lerp(b_, t), d_, 0.34, ts)
        tip = Vector((ARM_X + 0.15, 0, Z_MESS))
        frustum(bm, tip + Vector((0, 0, 0.02)), tip + Vector((0, 0, 0.30)), 0.055, 0.045, ts, 2)
        frustum(bm, Vector((ARM_X + 0.1, 0, Z_REG)) + Vector((0, 0, 0.02)), Vector((ARM_X + 0.1, 0, Z_REG + 0.22)), 0.05, 0.04, ts, 2)
        # black holders: a ribbed insulator at the root of the horizontal tube and of the registration arm, a clamp block
        # at the messenger's tip and a wire holder (a hook plate under the arm's tip) at the contact wire
        ribbed(bm, Vector((0.62, 0, Z_MESS)), Vector((1, 0, 0)), 0.40, ts, discs=6)
        ribbed(bm, Vector((0.55, 0, Z_REG)), Vector((1, 0, 0)), 0.34, ts, discs=5)
        for p0, w in ((Vector((ARM_X + 0.15, 0, Z_MESS + 0.32)), 0.05), (Vector((ARM_X + 0.10, 0, Z_REG + 0.24)), 0.045)):
            frustum(bm, p0, p0 + Vector((0, 0, 0.07)), w * 1.5, w * 1.5, 4, 2)         # the clamp
        hold = Vector((ARM_X + 0.10, 0, Z_REG))
        frustum(bm, hold + Vector((0, 0, 0.0)), hold - Vector((0, 0, 0.16)), 0.016, 0.016, ts, 2)     # the hanger
        frustum(bm, hold - Vector((0.04, 0, 0.17)), hold - Vector((-0.04, 0, 0.17)), 0.028, 0.028, ts, 2)  # the wire's clip
        mast_details(bm, ts, radius_at(4.0), H - 0.3, R0)
    return bm



def box(bm, c, hx, hy, hz, mi):
    """An axis-aligned box centred at c (Vector) with half sizes, UVs per face 0..1."""
    uv = bm.loops.layers.uv.verify()
    vs = [bm.verts.new(c + Vector((sx * hx, sy * hy, sz * hz))) for sz in (-1, 1) for sy in (-1, 1) for sx in (-1, 1)]
    for idx in ((0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)):
        f = bm.faces.new([vs[i] for i in idx])
        f.material_index = mi
        for loop, uvc in zip(f.loops, ((0, 0), (1, 0), (1, 1), (0, 1))):
            loop[uv].uv = uvc


def ibeam(bm, z0, z1, mi, depth=0.30, width=0.20, web=0.014, fl=0.018):
    """A vertical steel I-beam (web along x, flanges across y... the section's depth along x) from z0 to z1 at the origin."""
    box(bm, Vector((0, 0, (z0 + z1) / 2)), depth / 2 - fl, web / 2, (z1 - z0) / 2, mi)          # the web (across x)
    for sx in (-1, 1):
        box(bm, Vector((sx * (depth / 2 - fl / 2), 0, (z0 + z1) / 2)), fl / 2, width / 2, (z1 - z0) / 2, mi)


def cantilever(bm, ts, detail, attach_r):
    """The cantilever of build(): tube, registration arm, tie, holders; attach_r: the mast's half depth (the clamps)."""
    frustum(bm, Vector((attach_r, 0, Z_MESS)), Vector((ARM_X + 0.15, 0, Z_MESS)), 0.036, 0.030, ts, 1)
    frustum(bm, Vector((attach_r, 0, Z_REG)), Vector((ARM_X + 0.10, 0, Z_REG)), 0.028, 0.024, ts, 1)
    frustum(bm, Vector((attach_r, 0, Z_TIE)), Vector((ARM_X - 0.1, 0, Z_MESS + 0.03)), 0.022, 0.022, ts, 1)
    if not detail:
        return
    for z in (Z_MESS, Z_REG, Z_TIE):
        box(bm, Vector((attach_r, 0, z)), 0.04, 0.08, 0.06, 1)
    a_ = Vector((attach_r, 0, Z_TIE))
    b_ = Vector((ARM_X - 0.1, 0, Z_MESS + 0.03))
    d_ = (b_ - a_).normalized()
    for t in (0.30, 0.62):
        ribbed(bm, a_.lerp(b_, t), d_, 0.34, ts)
    ribbed(bm, Vector((attach_r + 0.55, 0, Z_MESS)), Vector((1, 0, 0)), 0.40, ts, discs=6)
    ribbed(bm, Vector((attach_r + 0.45, 0, Z_REG)), Vector((1, 0, 0)), 0.34, ts, discs=5)
    for p0, w in ((Vector((ARM_X + 0.15, 0, Z_MESS + 0.32)), 0.05), (Vector((ARM_X + 0.10, 0, Z_REG + 0.24)), 0.045)):
        frustum(bm, p0, p0 + Vector((0, 0, 0.07)), w * 1.5, w * 1.5, 4, 2)
    hold = Vector((ARM_X + 0.10, 0, Z_REG))
    frustum(bm, hold, hold - Vector((0, 0, 0.16)), 0.016, 0.016, ts, 2)
    frustum(bm, hold - Vector((0.04, 0, 0.17)), hold - Vector((-0.04, 0, 0.17)), 0.028, 0.028, ts, 2)
    mast_details(bm, ts, 0.17, 8.8, 0.15)


def build_steel(seg, detail):
    """A steel H-beam mast (0.30 x 0.20 section, 9.3 m, a base plate with bolts) with the cantilever."""
    bm = bmesh.new()
    ts = max(5, seg // 2)
    box(bm, Vector((0, 0, (0.34 - SINK) / 2)), 0.30, 0.30, (SINK + 0.34) / 2, 5)    # the foundation's head
    box(bm, Vector((0, 0, 0.36)), 0.22, 0.22, 0.02, 1)                       # the base plate
    ibeam(bm, 0.38, 9.3, 4)
    if detail:
        for sx in (-1, 1):
            for sy in (-1, 1):
                frustum(bm, Vector((sx * 0.17, sy * 0.17, 0.38)), Vector((sx * 0.17, sy * 0.17, 0.46)), 0.016, 0.016, 6, 1)
        plate(bm, Vector((0.16, 0, 2.2)), Vector((1, 0, 0)), 0.30, 3)
        plate(bm, Vector((-0.16, 0, 2.2)), Vector((-1, 0, 0)), 0.30, 3)
    cantilever(bm, ts, detail, 0.15)
    return bm


def build_post(kind, seg, detail):
    """A portal's post: concrete (kind "su": the tapered mast, 10.4 m) or steel (an I-beam), with a top seat for the truss."""
    bm = bmesh.new()
    if kind == "su":
        frustum(bm, Vector((0, 0, -SINK)), Vector((0, 0, 0.35)), 0.30, 0.27, max(8, seg), 5)
        frustum(bm, Vector((0, 0, 0.35)), Vector((0, 0, 10.4)), 0.19, 0.12, seg, 0, uv_len=1.1)
        if detail:
            joint_rings(bm, seg)
        box(bm, Vector((0, 0, 10.4)), 0.14, 0.30, 0.05, 1)
    else:
        box(bm, Vector((0, 0, (0.34 - SINK) / 2)), 0.30, 0.30, (SINK + 0.34) / 2, 5)
        box(bm, Vector((0, 0, 0.36)), 0.22, 0.22, 0.02, 1)
        ibeam(bm, 0.38, 10.4, 4, depth=0.36, width=0.24)
        box(bm, Vector((0, 0, 10.4)), 0.14, 0.30, 0.05, 1)
    if detail:
        plate(bm, Vector((0.0, 0.20 if kind == "su" else 0.13, 2.2)), Vector((0, 1, 0)), 0.30, 3)
        # the truss's bearing: a bolted plate with four anchor bolts and a stiffening bracket on each side, an earthing rod
        box(bm, Vector((0, 0, 10.47)), 0.18, 0.34, 0.02, 1)
        for sx in (-1, 1):
            for sy in (-1, 1):
                frustum(bm, Vector((sx * 0.12, sy * 0.28, 10.49)), Vector((sx * 0.12, sy * 0.28, 10.58)), 0.016, 0.016, 6, 1)
        for sy in (-1, 1):
            box(bm, Vector((0, sy * 0.2, 10.2)), 0.025, 0.012, 0.22, 1)
        frustum(bm, Vector((0.0, -0.2 if kind == "su" else -0.14, 0.5)), Vector((0.0, -0.2 if kind == "su" else -0.14, 10.3)),
                0.009, 0.009, 4, 1)
    return bm

def make(name, bm, parent):
    ob = new_object(name, parent)
    bm.to_mesh(ob.data)
    bm.free()
    for k in ("concrete", "galv", "insulator", "plate", "paint", "foundation"):
        ob.data.materials.append(MATS[k])
    return ob


def export(name, lod_hi, lod_lo, col):
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    base = bpy.data.objects.new("base00", None)
    bpy.context.collection.objects.link(base)
    start = bpy.data.objects.new("start01", None)
    bpy.context.collection.objects.link(start)
    start.parent = base
    make(f"{name}64", lod_hi, start)
    make(f"{name}8", lod_lo, start)
    c = make(f"Colmesh_{name}-1", col, start)
    c.data.materials.clear()
    coll = bpy.data.objects.new("collision-1", None)
    bpy.context.collection.objects.link(coll)
    coll.parent = base
    bpy.ops.object.select_all(action="SELECT")
    path = os.path.join(OUT_DIR, name + ".dae")
    os.makedirs(OUT_DIR, exist_ok=True)
    bpy.ops.wm.collada_export(filepath=path, apply_modifiers=True, selected=False, triangulate=True,
                              use_texture_copies=False, export_global_forward_selection="Y",
                              export_global_up_selection="Z")
    print("wrote", path)


def column(h, r0, r1, seg):
    bm = bmesh.new()
    frustum(bm, Vector((0, 0, -SINK)), Vector((0, 0, h)), r0, r1, seg, 0)
    return bm


def main():
    export(NAME, build(14, True), build(6, False), column(H, 0.22, 0.12, 6))
    export("bne_cat_steel", build_steel(8, True), build_steel(4, False), column(9.3, 0.20, 0.15, 6))
    export("bne_cat_post_su", build_post("su", 14, True), build_post("su", 6, False), column(10.4, 0.22, 0.12, 6))
    export("bne_cat_post_eu", build_post("eu", 8, True), build_post("eu", 4, False), column(10.4, 0.2, 0.15, 6))


main()
