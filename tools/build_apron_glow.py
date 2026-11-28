"""build_apron_glow.py: the pool of light on the ground under each apron mast (mod/.../art/shapes/beamngEarth/bne_apron_pool.dae,
its opacity map bne_apron_pool_o.data.png, the material bne_apron_pool in lamps.materials.json), for night only.

The real lights (nightlights.lua: a pool of spot lights, the apron masts' APRON_MAST taking a share of it) reach a few hundred
metres from the camera; an apron farther off was a dark patch with a few lamp balls on it, where a real one is one wash of
light (the user, 2026-11-17: the reference photos of floodlit aprons, lit evenly from end to end, every mast on). Each mast
carries a row of the group apron_glow (server airsideprops.py G_POOL): a flat square, 2 x HALF_M m, a radial gradient (the
opacity map: opaque at the middle, clear at the edge: it ends in a circle, the corners clear) laid on the ground, drawn
by the material's emissive light added to what is under it. Pools of masts 90 m apart overlap, so the apron is one even glow.
Beyond what the real lights reach, nothing else lights it, and it costs one flat quad a mast.

It is made as the street lamps' far glow is (build_lamp_glow.py), in the engine's own camera detail levels: within NEAR_M
(the real lights' reach: the pool would double their light) a speck of no size, then the quad. The material is dark by day (the
group is off then too: trees.lua groupOff, switched with the lamps' glow by nightlights.lua); nightlights.lua gives it its
emissive colour and brightness after dusk.

    python3 tools/build_apron_glow.py     (then python3 tools/mod_textures_dds.py: the map into its lossless source)
"""
import json
import math
import os
import uuid

import numpy as np

import build_lamp_glow as lg

dae, staging = lg.dae, lg.staging
HALF_M = 80.0               # m: the pool's half width, to its clear edge (masts 90 m apart: pools overlap by 70 m)
NEAR_M = 200.0              # m: nearer, only the speck (the masts there have their real lights: nightlights.lua APRON_NEAR_M
                            # reaches 450 m ahead of the camera, so from here out the glow adds to a real light's)
SPECK_M = 0.01
TEX_PX = 128
OPACITY = 0.55              # the material's opacityFactor: how much of the glow shows at the middle of a pool
MATERIAL = "bne_apron_pool"
SHAPE = "bne_apron_pool.dae"
TEXTURE = "bne_apron_pool_o.data"                   # (.png here, then tools/mod_textures_dds.py: .dds in the material)
TEXTURE_VFS = "/levels/beamng_earth/art/shapes/beamngEarth/" + TEXTURE + ".dds"
# the falloff: flat over the middle FLAT of the radius, then a smooth fall (smoothstep) to nothing at the edge
FLAT = 0.12
POWER = 1.5
SHAPES = lg.SHAPES


def opacity_map(px=TEX_PX):
    """The pool's opacity (px, px) uint8: 255 at the middle, 0 from the radius px / 2 out (a circle: the corners clear)."""
    c = (np.arange(px) + 0.5) / px * 2.0 - 1.0
    r = np.hypot(*np.meshgrid(c, c))
    t = np.clip((r - FLAT) / (1.0 - FLAT), 0.0, 1.0)
    fall = 1.0 - t * t * (3.0 - 2.0 * t)
    return np.round(255.0 * fall ** POWER).astype(np.uint8)


def detail_sizes(half_m=HALF_M, near_m=NEAR_M):
    """The engine's detail sizes of the speck and the quad: the speck down to where the quad's shape radius (its box's
    half diagonal) spans lg.ENGINE_PX over near_m, the quad from there out to lg.LAST_PX."""
    radius = half_m * math.sqrt(2.0)
    return [radius * lg.ENGINE_PX / near_m, lg.LAST_PX]


def shape_xml(half_m=HALF_M, near_m=NEAR_M):
    """The shape's COLLADA text: a speck (tiny triangle), then the flat quad half_m each way from the origin, up-facing,
    its texture across it, both with the one material."""
    sizes = detail_sizes(half_m, near_m)
    pieces = [  # (positions (n, 3), uvs (n, 2), triangles)
        (np.array([[-SPECK_M, 0, 0], [SPECK_M, 0, 0], [0, SPECK_M, 0]]), np.array([[0.5, 0.5]] * 3), [(0, 1, 2)]),
        (np.array([[-half_m, -half_m, 0], [half_m, -half_m, 0], [half_m, half_m, 0], [-half_m, half_m, 0]], np.float64),
         np.array([[0, 0], [1, 0], [1, 1], [0, 1]], np.float64), [(0, 1, 2), (0, 2, 3)])]
    geoms, nodes = [], []
    for k, (P, UV, T) in enumerate(pieces):
        n = np.tile([0.0, 0.0, 1.0], (len(P), 1))
        g = f"g{k}"
        geoms.append(
            f'<geometry id="{g}" name="{g}"><mesh>\n'
            f'<source id="{g}-pos"><float_array id="{g}-pos-a" count="{P.size}">{dae._floats(P)}</float_array>'
            f'<technique_common><accessor source="#{g}-pos-a" count="{len(P)}" stride="3"><param name="X" type="float"/>'
            '<param name="Y" type="float"/><param name="Z" type="float"/></accessor></technique_common></source>\n'
            f'<source id="{g}-nrm"><float_array id="{g}-nrm-a" count="{n.size}">{dae._floats(n)}</float_array>'
            f'<technique_common><accessor source="#{g}-nrm-a" count="{len(n)}" stride="3"><param name="X" type="float"/>'
            '<param name="Y" type="float"/><param name="Z" type="float"/></accessor></technique_common></source>\n'
            f'<source id="{g}-uv"><float_array id="{g}-uv-a" count="{UV.size}">{dae._floats(UV)}</float_array>'
            f'<technique_common><accessor source="#{g}-uv-a" count="{len(UV)}" stride="2"><param name="S" type="float"/>'
            '<param name="T" type="float"/></accessor></technique_common></source>\n'
            f'<vertices id="{g}-vtx"><input semantic="POSITION" source="#{g}-pos"/></vertices>\n'
            f'<triangles material="mat0" count="{len(T)}"><input semantic="VERTEX" source="#{g}-vtx" offset="0"/>'
            f'<input semantic="NORMAL" source="#{g}-nrm" offset="1"/><input semantic="TEXCOORD" source="#{g}-uv" '
            f'offset="2" set="0"/><p>{dae._indices(np.array(T), 3)}</p></triangles>\n</mesh></geometry>')
        name = f"pool{int(round(sizes[k]))}"
        nodes.append(f'<node id="{name}" name="{name}" type="NODE"><matrix sid="transform">1 0 0 0 0 1 0 0 0 0 1 0 0 0 '
                     f'0 1</matrix><instance_geometry url="#{g}"><bind_material><technique_common><instance_material '
                     f'symbol="mat0" target="#{MATERIAL}"/></technique_common></bind_material></instance_geometry>'
                     '</node>')
    ident = '<matrix sid="transform">1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1</matrix>'
    return f'''<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
<asset><unit name="meter" meter="1"/><up_axis>Z_UP</up_axis></asset>
<library_effects><effect id="{MATERIAL}-fx"><profile_COMMON><technique sid="common"><lambert><diffuse><color>1 0.86 0.62 1</color></diffuse></lambert></technique></profile_COMMON></effect></library_effects>
<library_materials><material id="{MATERIAL}" name="{MATERIAL}"><instance_effect url="#{MATERIAL}-fx"/></material></library_materials>
<library_geometries>{"".join(geoms)}</library_geometries>
<library_visual_scenes><visual_scene id="scene"><node id="base00" name="base00" type="NODE">{ident}<node id="start01" name="start01" type="NODE">{ident}
{"".join(nodes)}
</node></node></visual_scene></library_visual_scenes>
<scene><instance_visual_scene url="#scene"/></scene>
</COLLADA>
'''


def material():
    """The pool's material: translucent, its opacity the gradient map, added to the ground (AddAlpha), lit by its emissive
    only (a black base colour: the lights and the sky add nothing to it), dark by day (nightlights.lua sets the emissive)."""
    return {"name": MATERIAL, "mapTo": MATERIAL, "class": "Material", "version": 1.5,
            "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, "beamng-earth:" + MATERIAL)),
            "Stages": [{"baseColorFactor": [0.0, 0.0, 0.0, 1.0], "metallicFactor": 0, "roughnessFactor": 1,
                        "emissiveFactor": [0, 0, 0], "opacityMap": TEXTURE_VFS, "opacityFactor": OPACITY}, {}, {}, {}],
            "translucent": True, "translucentBlendOp": "AddAlpha", "translucentZWrite": False,
            "castShadows": False, "doubleSided": True}


def write_materials(path):
    """lamps.materials.json with the pool's material, the others kept."""
    with open(path) as f:
        mats = json.load(f)
    mats[MATERIAL] = material()
    lg.write_json(path, mats)


def write_texture(path):
    from PIL import Image
    tmp = staging.tmp_for(path)
    Image.fromarray(opacity_map()).save(tmp, format="PNG", optimize=True)
    staging.replace(tmp, path)


def main():
    out = os.path.join(SHAPES, SHAPE)
    lg.write_shape(out, shape_xml())
    write_materials(os.path.join(SHAPES, "lamps.materials.json"))
    tex = os.path.join(SHAPES, TEXTURE + ".png")
    write_texture(tex)
    print(out, os.path.getsize(out), "bytes; detail sizes", [round(s) for s in detail_sizes()])
    print(tex, "(now: python3 tools/mod_textures_dds.py)")


if __name__ == "__main__":
    main()
