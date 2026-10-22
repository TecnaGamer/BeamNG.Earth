"""Run inside Blender (the Game Asset bridge's exec, or Blender's text editor): (re)load a kunstwerk's parts.json
(tools/kunstwerk_build.py) into the collection MODEL with the game's own textures (the stock materials' images,
extracted to design/kunstwerken/_textures). Set KUNSTWERK first, e.g. KUNSTWERK = r"E:\\BeamNG\\design\\kunstwerken\\droogleever".
"""
import json
import os

import bpy
import numpy as np

BASE = os.path.dirname(KUNSTWERK)  # noqa: F821  (set by the caller)
TX = os.path.join(BASE, "_textures")
SPEC = {  # the game's material -> (base colour, normal, roughness, repeats per metre of UV)
    "bne_bridge_concrete": ("t_concrete_plain_b.color.dds", "t_concrete_plain_nm.normal.dds", "t_concrete_plain_r.data.dds", 1.0),
    "road_asphalt_2lane": ("t_asphalt_b.color.dds", "t_asphalt_nm.normal.dds", None, 1 / 3),
    "bne_sidewalk_brick": ("t_brick_herringbone_b.color.dds", "t_brick_herringbone_nm.normal.dds", "t_brick_herringbone_r.data.dds", 1 / 1.2),
    "sidewalk_concrete": ("t_concrete_plain_b.color.dds", "t_concrete_plain_nm.normal.dds", None, 1 / 1.5),
    "italy_cobblestone": ("t_brick_herringbone_b.color.dds", "t_brick_herringbone_nm.normal.dds", None, 1.0),
    "bne_grass": ("t_grass_01_b.png", "t_grass_01_nm.png", "t_grass_01_r.png", 1 / 2),
    "bne_tunnel_soil": ("t_grass_01_b.png", "t_grass_01_nm.png", "t_grass_01_r.png", 1 / 2),
    "curbs": ("t_curbs_b.color.dds", "t_curbs_nm.normal.dds", "t_curbs_r.data.dds", 1 / 2),
    "bne_truss_steel": ("t_metal_paint_b.color.dds", "t_metal_paint_nm.normal.dds", None, 1.0),
    "bne_cycle_red": ("t_asphalt_b.color.dds", "t_asphalt_nm.normal.dds", None, 1 / 8),
    "bne_paint_white": ("t_asphalt_b.color.dds", "t_asphalt_nm.normal.dds", None, 1 / 8),
    "bne_rail_steel": ("t_metal_paint_b.color.dds", "t_metal_paint_nm.normal.dds", None, 1.0),
    "bne_concrete_dark": ("t_concrete_plain_b.color.dds", "t_concrete_plain_nm.normal.dds", "t_concrete_plain_r.data.dds", 1 / 2),
    "bne_steel_black": ("t_metal_paint_b.color.dds", "t_metal_paint_nm.normal.dds", None, 1.0),
    "bne_signs": (os.path.join(BASE, "..", "..", "src", "mod", "beamng_earth", "levels",
                               "beamng_earth", "art", "shapes", "beamngEarth", "bne_signs3.png"), None,
                  os.path.join(BASE, "..", "..", "src", "mod", "beamng_earth", "levels", "beamng_earth", "art", "shapes",
                               "beamngEarth", "bne_signs3_r.png"), 1.0),
}
TINT = {"bne_tunnel_soil": (0.45, 0.75, 0.3, 1), "bne_grass": (0.55, 0.68, 0.36, 1), "bne_concrete_dark": (0.3, 0.29, 0.27, 1),
        "road_asphalt_2lane": (0.42, 0.42, 0.42, 1), "bne_cycle_red": (0.72, 0.3, 0.24, 1),  # as the materials' baseColorFactor
        "bne_paint_white": (2.2, 2.2, 2.15, 1), "bne_rail_steel": (0.3, 0.29, 0.27, 1),
        "bne_steel_black": (0.06, 0.06, 0.06, 1)}


def material(name):
    m = bpy.data.materials.get(name)
    if m:
        return m
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    b = nt.nodes["Principled BSDF"]
    if name.startswith("line_"):            # road paint: plain white
        b.inputs["Base Color"].default_value = (0.85, 0.85, 0.82, 1)
        b.inputs["Roughness"].default_value = 0.6
        return m
    base, nm, rough, sc = SPEC.get(name, SPEC["bne_bridge_concrete"])
    tc = nt.nodes.new("ShaderNodeTexCoord")
    mp = nt.nodes.new("ShaderNodeMapping")
    mp.inputs["Scale"].default_value = (sc, sc, 1)
    nt.links.new(tc.outputs["UV"], mp.inputs["Vector"])

    def img(fn, noncolor=False):
        t = nt.nodes.new("ShaderNodeTexImage")
        t.image = bpy.data.images.load(os.path.join(TX, fn), check_existing=True)
        if noncolor:
            t.image.colorspace_settings.name = "Non-Color"
        nt.links.new(mp.outputs["Vector"], t.inputs["Vector"])
        return t
    col = img(base).outputs["Color"]
    if name in TINT:                        # the game tints these textures in the material; the preview the same
        mix = nt.nodes.new("ShaderNodeMix")
        mix.data_type, mix.blend_type = "RGBA", "MULTIPLY"
        mix.inputs["Factor"].default_value = 1.0
        mix.inputs["B"].default_value = TINT[name]
        nt.links.new(col, mix.inputs["A"])
        col = mix.outputs["Result"]
    nt.links.new(col, b.inputs["Base Color"])
    if nm:
        n = nt.nodes.new("ShaderNodeNormalMap")
        nt.links.new(img(nm, True).outputs["Color"], n.inputs["Color"])
        nt.links.new(n.outputs["Normal"], b.inputs["Normal"])
    if rough:
        nt.links.new(img(rough, True).outputs["Color"], b.inputs["Roughness"])
    else:
        b.inputs["Roughness"].default_value = 0.9
    if name == "bne_truss_steel":
        b.inputs["Metallic"].default_value = 0.6
    if name == "bne_signs":                 # its metallic map beside its colour (sign_atlas.py)
        nt.links.new(img(base.replace(".png", "_m.png"), True).outputs["Color"], b.inputs["Metallic"])
    return m


data = json.load(open(os.path.join(KUNSTWERK, "parts.json")))  # noqa: F821
c = bpy.data.collections.get("MODEL") or bpy.data.collections.new("MODEL")
if c.name not in bpy.context.scene.collection.children:
    bpy.context.scene.collection.children.link(c)
for o in list(c.objects):
    bpy.data.objects.remove(o, do_unlink=True)
for p in data["parts"]:
    P, T, UV = np.array(p["positions"]), np.array(p["tris"]), np.array(p["uvs"])
    me = bpy.data.meshes.new("M_" + p["name"])
    me.from_pydata(P.tolist(), [], T.tolist())
    uvl = me.uv_layers.new(name="UVMap")
    li = np.zeros(len(me.loops), np.int64)
    me.loops.foreach_get("vertex_index", li)
    uvl.data.foreach_set("uv", UV[li].ravel())
    me.validate()
    me.update()
    ob = bpy.data.objects.new("M_" + p["name"], me)
    ob.data.materials.append(material(p["material"]))
    c.objects.link(ob)
bpy.ops.wm.save_mainfile()
result = [o.name for o in c.objects]
