"""build_tram_poles.py: the tram overhead line's poles (mod/.../art/shapes/beamngEarth/bne_tram_*.dae) in Blender (plan 046,
stage E; server/tramcat.py and native/earthfast/src/tram place them):

  bne_tram_<family>_<kind>   family: tube (galvanised tapered tubular pole on a flange and a concrete foundation, the
                             Germany / Czechia / Netherlands look), square (black square tube, Helsinki), lattice (black
                             lattice mast, Tampere), concrete (spun concrete pole, ex-USSR)
                             kind: span (a pole in a street: an eye for the span wire; the lamp bracket is its own model),
                             arm (a cantilever 3.45 m toward +X: a track of its own beside it), centre (a pole between two
                             tracks: a cantilever 2.2 m to each side, +X and -X)
  bne_tram_lamp_g / _k / _t  the street lamp on a pole: an arm with a lantern (galvanised / black) and Helsinki's T head
  bne_tram_rosette           a wall rosette: the plate a span wire is anchored to on a building's face

    D:/blender-4.5/blender.exe --background --python tools/blender/build_tram_poles.py

Origin on the ground at the pole's axis (a rosette: on the wall's face at the fixing), Z up, the cantilever / lamp arm / the
rosette's face toward +X (tramcat turns +X to the track / the street). Every pole is H = 9 m tall at scale 1. The span wire's
eye is at SPAN_Z = 7.4 m (native/earthfast/src/tram/wires.rs), a cantilever's contact wire at ARM_Z = 5.6 m under its tip.
Nodes as the game's shapes want them (tools/blender/build_catenary.py: base00 > start01 > <name>64 / <name>8 detail levels,
Colmesh_<name>-1 the collision); materials: bne_tram_galv, bne_tram_concrete, bne_tram_black (trampoles.materials.json; the first two borrow the catenary's textures until the user's images of 046_prompts_poles come), bne_cat_insulator and bne_cat_foundation (catenary.materials.json).
Pure geometry: UVs cylindrical / planar for the textures."""
import math
import os

import bpy
import bmesh
from mathutils import Vector

OUT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "mod", "beamng_earth", "levels",
                                       "beamng_earth", "art", "shapes", "beamngEarth"))
H = 9.0                     # m: every pole's top over the ground
SINK = 0.5                  # m of the foundation below the ground
SPAN_Z = 7.4                # m: the span wire's fixing
ARM_Z = 5.6                 # m: the contact wire under a cantilever's tip
ARM_REACH = 3.45            # m: a mast's cantilever (the pole stands MAST_OFF_M from the track's axis)
CENTRE_REACH = 2.2          # m: each side of a centre pole
ARM_ATT = 6.25              # m: the cantilever tube's height on the pole
TIE_ATT = 8.30              # m: the tie's top on the pole
LAMP_X, LAMP_Z = 1.55, 8.07  # m: the arm lamp's luminaire centre (furniture.LAMP_HEADS)
T_Y, T_Z = 0.85, 8.67       # m: Helsinki's T head: each luminaire out along the bar, and its height

C_CONC, C_GALV, C_INS, C_PLATE, C_BLACK, C_FOUND = range(6)


def mat(name, rgb, metallic=0.0, rough=0.6):
    m = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*rgb, 1.0)
    b.inputs["Metallic"].default_value = metallic
    b.inputs["Roughness"].default_value = rough
    return m


MATS = [
    mat("bne_tram_concrete", (0.62, 0.61, 0.58), 0.0, 0.9),
    mat("bne_tram_galv", (0.62, 0.64, 0.65), 0.85, 0.45),
    mat("bne_cat_insulator", (0.03, 0.03, 0.035), 0.0, 0.3),
    mat("bne_cat_plate", (0.9, 0.85, 0.1), 0.0, 0.5),
    mat("bne_tram_black", (0.05, 0.05, 0.055), 0.2, 0.5),
    mat("bne_cat_foundation", (0.54, 0.53, 0.5), 0.0, 0.95),
]


def new_object(name, parent=None):
    mesh = bpy.data.meshes.new(name)
    ob = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(ob)
    if parent is not None:
        ob.parent = parent
    return ob


def frustum(bm, p0, p1, r0, r1, seg, mi, cap=True, uv_len=2.0, rot=0.0):
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
        rings.append([bm.verts.new(p + (x * math.cos(rot + 2 * math.pi * k / seg) + y * math.sin(rot + 2 * math.pi * k / seg)) * r)
                      for k in range(seg)])
    for k in range(seg):
        k1 = (k + 1) % seg
        f = bm.faces.new((rings[0][k], rings[0][k1], rings[1][k1], rings[1][k]))
        f.material_index = mi
        f.smooth = seg > 4
        for loop, (u, v) in zip(f.loops, ((k / seg, 0), ((k + 1) / seg, 0), ((k + 1) / seg, L / uv_len), (k / seg, L / uv_len))):
            loop[uv].uv = (u, v)
    if cap:
        top = bm.faces.new(rings[1][::-1])
        top.material_index = mi
        for loop in top.loops:
            loop[uv].uv = (0.5, 0.5)
    return rings


def box(bm, c, hx, hy, hz, mi):
    """An axis-aligned box centred at c (Vector) with half sizes, UVs per face 0..1."""
    uv = bm.loops.layers.uv.verify()
    vs = [bm.verts.new(c + Vector((sx * hx, sy * hy, sz * hz))) for sz in (-1, 1) for sy in (-1, 1) for sx in (-1, 1)]
    for idx in ((0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)):
        f = bm.faces.new([vs[i] for i in idx])
        f.material_index = mi
        for loop, uvc in zip(f.loops, ((0, 0), (1, 0), (1, 1), (0, 1))):
            loop[uv].uv = uvc


def bar(bm, a, b, w, mi):
    """A square-section bar of half width w from a to b (Vectors)."""
    frustum(bm, a, b, w * 1.414, w * 1.414, 4, mi, uv_len=1.0, rot=math.pi / 4)


def sq_frustum(bm, z0, z1, h0, h1, mi, uv_len=2.0):
    """A square tube tapering from half width h0 (at z0) to h1 (at z1): faces to the axes."""
    uv = bm.loops.layers.uv.verify()
    rings = []
    for z, h in ((z0, h0), (z1, h1)):
        rings.append([bm.verts.new(Vector((sx * h, sy * h, z))) for sx, sy in ((1, 1), (-1, 1), (-1, -1), (1, -1))])
    L = z1 - z0
    for k in range(4):
        k1 = (k + 1) % 4
        f = bm.faces.new((rings[0][k], rings[0][k1], rings[1][k1], rings[1][k]))
        f.material_index = mi
        for loop, (u, v) in zip(f.loops, ((k / 4, 0), ((k + 1) / 4, 0), ((k + 1) / 4, L / uv_len), (k / 4, L / uv_len))):
            loop[uv].uv = (u, v)
    top = bm.faces.new(rings[1][::-1])
    top.material_index = mi
    for loop in top.loops:
        loop[uv].uv = (0.5, 0.5)


def ribbed(bm, p, d, length, ts, discs=5):
    """A porcelain insulator string: discs of alternating radius along unit direction d, centred on p."""
    for k in range(discs):
        t0 = -length / 2 + length * k / discs
        t1 = -length / 2 + length * (k + 0.8) / discs
        r = 0.062 if k % 2 == 0 else 0.046
        frustum(bm, p + d * t0, p + d * t1, r, r * 0.92, ts, C_INS)
    frustum(bm, p - d * length / 2 - d * 0.02, p + d * length / 2 + d * 0.02, 0.022, 0.022, ts, C_GALV)


# --- the foot -----------------------------------------------------------------------------------------------------
def foot(bm, hw, mi, detail):
    """The foundation's head (a concrete block, 0.6 m square, 0.25 m over the ground), the flange plate on it (hw half
    width), four anchor bolts with nuts and four gusset ribs."""
    box(bm, Vector((0, 0, (0.12 - SINK) / 2)), 0.30, 0.30, (SINK + 0.12) / 2, C_FOUND)
    box(bm, Vector((0, 0, 0.135)), hw, hw, 0.015, mi)
    if not detail:
        return
    for sx in (-1, 1):
        for sy in (-1, 1):
            p = Vector((sx * (hw - 0.05), sy * (hw - 0.05), 0.15))
            frustum(bm, p, p + Vector((0, 0, 0.09)), 0.016, 0.016, 6, mi)
            box(bm, p + Vector((0, 0, 0.03)), 0.024, 0.024, 0.014, mi)
    for ax in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        bar(bm, Vector((ax[0] * (hw - 0.03), ax[1] * (hw - 0.03), 0.15)), Vector((ax[0] * 0.15, ax[1] * 0.15, 0.65)), 0.012, mi)


# --- the shafts: (radius at height z) for the eye and the cantilever attach --------------------------------------------
def shaft_tube(bm, seg, detail):
    mi = C_GALV
    foot(bm, 0.22, mi, detail)
    frustum(bm, Vector((0, 0, 0.15)), Vector((0, 0, H - 0.1)), 0.165, 0.085, seg, mi, cap=False, uv_len=1.5)
    frustum(bm, Vector((0, 0, H - 0.1)), Vector((0, 0, H)), 0.10, 0.04, seg, mi, uv_len=1.5)     # the cap
    if detail:
        for z in (3.1, 6.1):                                   # (the tube's sections' welded joints)
            frustum(bm, Vector((0, 0, z - 0.03)), Vector((0, 0, z + 0.03)), 0.165 - 0.0107 * (z - 0.15) + 0.012,
                    0.165 - 0.0107 * (z - 0.15) + 0.012, seg, mi, cap=False, uv_len=1.5)
        box(bm, Vector((0.145 - 0.006, 0, 1.0)), 0.006, 0.075, 0.13, mi)                  # the hand-hole's cover
    return lambda z: 0.165 - (0.165 - 0.085) * (z - 0.15) / (H - 0.25)


def shaft_concrete(bm, seg, detail):
    foot(bm, 0.24, C_GALV, detail)
    frustum(bm, Vector((0, 0, 0.15)), Vector((0, 0, H)), 0.19, 0.115, seg, C_CONC, uv_len=1.1)
    if detail:
        for z in (3.0, 6.0):                                   # (the spun sections' joints)
            r = 0.19 - 0.075 * (z - 0.15) / (H - 0.15)
            frustum(bm, Vector((0, 0, z - 0.05)), Vector((0, 0, z + 0.05)), r + 0.02, r + 0.02, seg, C_CONC, cap=False, uv_len=1.1)
    return lambda z: 0.19 - 0.075 * (z - 0.15) / (H - 0.15)


def shaft_square(bm, seg, detail):
    foot(bm, 0.26, C_BLACK, detail)
    sq_frustum(bm, 0.15, H - 0.04, 0.125, 0.085, C_BLACK, uv_len=1.5)
    box(bm, Vector((0, 0, H - 0.02)), 0.095, 0.095, 0.02, C_BLACK)                       # the cap
    if detail:
        box(bm, Vector((0.126, 0, 1.0)), 0.006, 0.07, 0.14, C_BLACK)                       # the hand-hole's door
        for z in (4.0, 7.0):                                    # (a band round the tube)
            h = 0.125 - 0.04 * (z - 0.15) / (H - 0.19)
            box(bm, Vector((0, 0, z)), h + 0.012, h + 0.012, 0.03, C_BLACK)
    return lambda z: 0.125 - 0.04 * (z - 0.15) / (H - 0.19)


def shaft_lattice(bm, seg, detail):
    b0, b1, w = 0.22, 0.115, 0.024                             # the legs' centre lines at the foot and the top
    foot(bm, 0.30, C_BLACK, detail)
    z0, z1 = 0.16, H - 0.08

    def corner(sx, sy, z):
        t = (z - z0) / (z1 - z0)
        b = b0 + (b1 - b0) * t
        return Vector((sx * b, sy * b, z))
    for sx, sy in ((1, 1), (-1, 1), (-1, -1), (1, -1)):
        bar(bm, corner(sx, sy, z0), corner(sx, sy, z1), w, C_BLACK)
    panels = 10 if detail else 5
    zs = [z0 + (z1 - z0) * k / panels for k in range(panels + 1)]
    for z in zs:                                               # (the horizontal rings)
        for k in range(4):
            sx0, sy0 = ((1, 1), (-1, 1), (-1, -1), (1, -1))[k]
            sx1, sy1 = ((1, 1), (-1, 1), (-1, -1), (1, -1))[(k + 1) % 4]
            bar(bm, corner(sx0, sy0, z), corner(sx1, sy1, z), w * 0.7, C_BLACK)
    if detail:
        for i in range(panels):                                # (a diagonal in each panel of each face, alternating)
            for k in range(4):
                sx0, sy0 = ((1, 1), (-1, 1), (-1, -1), (1, -1))[k]
                sx1, sy1 = ((1, 1), (-1, 1), (-1, -1), (1, -1))[(k + 1) % 4]
                a, b = (zs[i], zs[i + 1]) if (i + k) % 2 == 0 else (zs[i + 1], zs[i])
                bar(bm, corner(sx0, sy0, a), corner(sx1, sy1, b), w * 0.55, C_BLACK)
    box(bm, Vector((0, 0, H - 0.04)), 0.15, 0.15, 0.04, C_BLACK)                         # the top plate
    return lambda z: b0 + (b1 - b0) * (z - z0) / (z1 - z0) + w


SHAFTS = {"tube": (shaft_tube, C_GALV), "concrete": (shaft_concrete, C_GALV), "square": (shaft_square, C_BLACK),
          "lattice": (shaft_lattice, C_BLACK)}


# --- the attachments --------------------------------------------------------------------------------------------------
def eye(bm, radius_at, mi, ts):
    """The span wire's fixing at SPAN_Z on the +X side: a collar round the pole, a lug and an eye ring."""
    r = radius_at(SPAN_Z)
    frustum(bm, Vector((0, 0, SPAN_Z - 0.06)), Vector((0, 0, SPAN_Z + 0.06)), r + 0.022, r + 0.022, max(6, ts), mi, cap=False)
    box(bm, Vector((r + 0.05, 0, SPAN_Z)), 0.05, 0.022, 0.03, mi)
    frustum(bm, Vector((r + 0.11, 0, SPAN_Z - 0.04)), Vector((r + 0.11, 0, SPAN_Z + 0.04)), 0.045, 0.045, 8, mi, cap=False)


def cantilever(bm, radius_at, reach, sign, mi, ts, detail):
    """One cantilever toward sign * X: the horizontal tube at ARM_ATT, the tie from TIE_ATT to the tip, the hanger
    (a porcelain insulator and a clamp) down to the contact wire at ARM_Z under the tip."""
    r = radius_at(ARM_ATT)
    a = Vector((sign * r, 0, ARM_ATT))
    tip = Vector((sign * reach, 0, ARM_ATT))
    frustum(bm, a, tip + Vector((sign * 0.12, 0, 0)), 0.04, 0.032, ts, mi)
    t0 = Vector((sign * radius_at(TIE_ATT), 0, TIE_ATT))
    t1 = Vector((sign * (reach - 0.1), 0, ARM_ATT + 0.03))
    frustum(bm, t0, t1, 0.022, 0.022, ts, mi)
    if not detail:
        return
    for z in (ARM_ATT, TIE_ATT):
        frustum(bm, Vector((0, 0, z - 0.07)), Vector((0, 0, z + 0.07)), radius_at(z) + 0.03, radius_at(z) + 0.03, max(6, ts), mi, cap=False)
    d = (t1 - t0).normalized()
    for t in (0.35, 0.68):
        ribbed(bm, t0.lerp(t1, t), d, 0.30, ts, discs=4)
    hold = Vector((sign * reach, 0, ARM_ATT))
    ribbed(bm, hold - Vector((0, 0, 0.2)), Vector((0, 0, 1)), 0.34, ts, discs=4)             # the hanger insulator
    frustum(bm, hold - Vector((0, 0, 0.37)), hold - Vector((0, 0, ARM_ATT - ARM_Z - 0.02)), 0.016, 0.016, ts, mi)
    box(bm, Vector((sign * reach, 0, ARM_Z + 0.02)), 0.07, 0.02, 0.02, mi)                 # the clamp at the wire


def build_pole(family, kind, seg, detail):
    bm = bmesh.new()
    shaft, mi = SHAFTS[family]
    ts = max(5, seg // 2)
    radius_at = shaft(bm, seg, detail)
    if kind == "span":
        eye(bm, radius_at, mi, ts)
    elif kind == "arm":
        cantilever(bm, radius_at, ARM_REACH, 1.0, mi, ts, detail)
    else:
        cantilever(bm, radius_at, CENTRE_REACH, 1.0, mi, ts, detail)
        cantilever(bm, radius_at, CENTRE_REACH, -1.0, mi, ts, detail)
    return bm


def build_lamp(kind, seg, detail):
    """kind g (galvanised arm), k (black arm), t (Helsinki's T head: a black bar across the pole's top with a luminaire at
    each end). The row's origin is the pole's foot, so the geometry stands at the pole's height."""
    bm = bmesh.new()
    ts = max(5, seg // 2)
    if kind in ("g", "k"):
        mi = C_GALV if kind == "g" else C_BLACK
        a = Vector((0.08, 0, 7.62))
        tip = Vector((LAMP_X - 0.2, 0, LAMP_Z - 0.02))
        frustum(bm, a, tip, 0.032, 0.026, ts, mi)                                           # the arm, rising a little
        bar(bm, Vector((0.08, 0, 7.30)), Vector((LAMP_X * 0.55, 0, LAMP_Z - 0.12)), 0.012, mi)       # its stay
        box(bm, Vector((LAMP_X, 0, LAMP_Z + 0.04)), 0.34, 0.15, 0.04, mi)                   # the lantern's housing
        box(bm, Vector((LAMP_X - 0.02, 0, LAMP_Z - 0.01)), 0.30, 0.13, 0.015, C_INS)       # its glass (the lens' place)
        if detail:
            frustum(bm, Vector((LAMP_X - 0.34, 0, LAMP_Z + 0.02)), Vector((LAMP_X - 0.2, 0, LAMP_Z + 0.02)), 0.05, 0.04, ts, mi)
    else:
        frustum(bm, Vector((0, -T_Y - 0.12, 8.78)), Vector((0, T_Y + 0.12, 8.78)), 0.035, 0.035, ts, C_BLACK, uv_len=1.0)
        for s in (-1, 1):
            box(bm, Vector((0, s * T_Y, T_Z + 0.07)), 0.30, 0.13, 0.05, C_BLACK)
            box(bm, Vector((0, s * T_Y, T_Z + 0.015)), 0.27, 0.115, 0.012, C_INS)
            bar(bm, Vector((0, s * T_Y, 8.78)), Vector((0, s * T_Y, T_Z + 0.1)), 0.015, C_BLACK)
    return bm


def build_rosette(seg, detail):
    """A wall rosette: a round plate on the wall's face (its centre at the origin, the face toward +X) with four bolts and
    the eye the span wire's insulator hooks to."""
    bm = bmesh.new()
    frustum(bm, Vector((-0.03, 0, 0)), Vector((0.0, 0, 0)), 0.13, 0.13, max(8, seg), C_GALV, uv_len=0.3, rot=0.0)
    uv = bm.loops.layers.uv.verify()
    # (a disc on the face: the frustum's axis is x, the faces are its rim; the cap is the plate's face)
    for a in range(4):
        ang = math.pi / 4 + a * math.pi / 2
        c = Vector((0.0, math.cos(ang) * 0.09, math.sin(ang) * 0.09))
        frustum(bm, c, c + Vector((0.025, 0, 0)), 0.014, 0.014, 6, C_GALV)
    frustum(bm, Vector((0.0, 0, 0)), Vector((0.07, 0, 0)), 0.02, 0.02, 6, C_GALV)
    frustum(bm, Vector((0.07, 0, -0.035)), Vector((0.07, 0, 0.035)), 0.04, 0.04, 8, C_GALV, cap=False)
    return bm


def make(name, bm, parent, materials=True):
    ob = new_object(name, parent)
    bm.to_mesh(ob.data)
    bm.free()
    if materials:
        for m in MATS:
            ob.data.materials.append(m)
    return ob


def collision(hw, with_foundation=True):
    """The pole's shaft and foot as one box prism pair: what a car hits (poleprofiles.COLLISION_BOXES is outside it)."""
    bm = bmesh.new()
    if with_foundation:
        box(bm, Vector((0, 0, 0.07)), 0.30, 0.30, 0.08, 0)
    box(bm, Vector((0, 0, H / 2)), hw, hw, H / 2, 0)
    return bm


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
    if col is not None:
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


COL_HW = {"tube": 0.22, "square": 0.20, "lattice": 0.28, "concrete": 0.25}


def main():
    for family in SHAFTS:
        for kind in ("span", "arm", "centre"):
            export(f"bne_tram_{family}_{kind}", build_pole(family, kind, 14, True), build_pole(family, kind, 6, False),
                   collision(COL_HW[family]))
    for kind in ("g", "k", "t"):
        export(f"bne_tram_lamp_{kind}", build_lamp(kind, 8, True), build_lamp(kind, 5, False), None)
    export("bne_tram_rosette", build_rosette(10, True), build_rosette(6, False), None)


main()
