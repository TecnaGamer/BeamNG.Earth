"""build_catenary.py: the railway catenary mast (mod/.../art/shapes/beamngEarth/bne_cat_mast.dae) in Blender.

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

OUT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "mod", "beamng_earth", "levels",
                                   "beamng_earth", "art", "shapes", "beamngEarth", "bne_cat_mast.dae"))
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
    "insulator": mat("bne_cat_insulator", (0.55, 0.66, 0.6), 0.0, 0.15),
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


def build(seg, detail):
    bm = bmesh.new()
    # the mast: foot collar (concrete foundation) and the tapered pole
    frustum(bm, Vector((0, 0, -SINK)), Vector((0, 0, 0.35)), 0.30, 0.27, max(8, seg), 0)
    frustum(bm, Vector((0, 0, 0.35)), Vector((0, 0, H)), R0, R1, seg, 0, uv_len=2.5)
    ts = max(5, seg // 2)
    # the cantilever: the horizontal tube, the registration arm (thinner), the inclined tie
    frustum(bm, Vector((0.10, 0, Z_MESS)), Vector((ARM_X + 0.15, 0, Z_MESS)), 0.036, 0.030, ts, 1)
    frustum(bm, Vector((0.10, 0.0, Z_REG)), Vector((ARM_X + 0.10, 0.0, Z_REG)), 0.028, 0.024, ts, 1)
    frustum(bm, Vector((0.10, 0, Z_TIE)), Vector((ARM_X - 0.1, 0, Z_MESS + 0.03)), 0.022, 0.022, ts, 1)
    if detail:
        # the cantilever's clamps on the mast and glass insulators along the tie and at the arm's tip
        for z in (Z_MESS, Z_REG, Z_TIE):
            frustum(bm, Vector((-0.06, 0, z - 0.06)), Vector((-0.06, 0, z + 0.06)), 0.12, 0.12, ts, 1)
        for t in (0.35, 0.55, 0.75):
            p = Vector((0.10, 0, Z_TIE)).lerp(Vector((ARM_X - 0.1, 0, Z_MESS + 0.03)), t)
            d = Vector((ARM_X - 0.2, 0, Z_MESS - Z_TIE + 0.03)).normalized()
            frustum(bm, p - d * 0.11, p + d * 0.11, 0.05, 0.05, ts, 2)
        tip = Vector((ARM_X + 0.15, 0, Z_MESS))
        frustum(bm, tip + Vector((0, 0, 0.02)), tip + Vector((0, 0, 0.30)), 0.055, 0.045, ts, 2)
        frustum(bm, Vector((ARM_X + 0.1, 0, Z_REG)) + Vector((0, 0, 0.02)), Vector((ARM_X + 0.1, 0, Z_REG + 0.22)), 0.05, 0.04, ts, 2)
    return bm


def make(name, bm, parent):
    ob = new_object(name, parent)
    bm.to_mesh(ob.data)
    bm.free()
    for k in ("concrete", "galv", "insulator"):
        ob.data.materials.append(MATS[k])
    return ob


def main():
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    base = bpy.data.objects.new("base00", None)
    bpy.context.collection.objects.link(base)
    start = bpy.data.objects.new("start01", None)
    bpy.context.collection.objects.link(start)
    start.parent = base
    make(f"{NAME}64", build(14, True), start)
    make(f"{NAME}8", build(6, False), start)
    bm = bmesh.new()
    frustum(bm, Vector((0, 0, -SINK)), Vector((0, 0, H)), 0.22, 0.12, 6, 0)
    col = make(f"Colmesh_{NAME}-1", bm, start)
    col.data.materials.clear()
    coll = bpy.data.objects.new("collision-1", None)
    bpy.context.collection.objects.link(coll)
    coll.parent = base
    bpy.ops.object.select_all(action="SELECT")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    bpy.ops.wm.collada_export(filepath=OUT, apply_modifiers=True, selected=False, triangulate=True,
                              use_texture_copies=False, export_global_forward_selection="Y",
                              export_global_up_selection="Z")
    print("wrote", OUT)


main()
