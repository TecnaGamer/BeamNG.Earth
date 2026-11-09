"""build_lamp_glow.py: the street lamps' far glow (mod/.../art/shapes/beamngEarth/bne_lamp_glow.dae), for night only
(nightlights.lua turns its group on after dusk; furniture.py G_GLOW rows at every lens). The lens (build_lamp_lens.py)
is flat and 0.6 m long: past about 60 m it is a pixel or two edge-on, and the lamps vanished a short way from the camera
(the user, 2026-10-27: "at night street lamps only turn on when close to the camera"). This is a glowing ball at the
lens that grows with distance, in the engine's own camera detail levels (the stock shapes' base00 / start01 hierarchy:
a node named <object><size> is that detail's mesh): near the camera a speck of HIDDEN_R m inside the lens (the lens is
there; an empty "nulldetail" level first drew the whole shape nowhere, at any distance, in 0.40), then balls of GLOW_R
m, each one kept until its picture spans END_PX pixels.

Each ball has its own material (bne_lamp_glow<k>, lamps.materials.json; nightlights.lua M.GLOW_NITS lights them after
dusk), its brightness the lens's own light, LENS_CD candela, spread over its disc: a lamp's glow on screen then falls
with the square of its distance, as a real lamp's does, down to GLOW_NITS_MIN, where it stays (a floodlit wall's
brightness: the skyline's lamps faint points; at the lit windows' 200 nits they outshone the near ones, the user,
2026-11-08: dimmer, and with no far glow at all "you cant even see the streetlights anymore"). Before, every ball was the lens's 4000 nits (its material), each level's
ball 1.6 times wider, so the far lamps barely dimmed (one 600 m off gave a fifth of the light of one at 60 m, not a
hundredth) and bloomed into blobs (the user, 2026-11-07: "street lights look blown out from afar", 100-400 m off on a
divided highway at dusk); 4 to 11 pixels wide, now 4 near the camera down to about 2 a few km off (the user,
2026-11-08: "shrink the glow size the further it gets away"; a steady 2.5 to 4 drew the far ones as big as the near).

    python3 tools/build_lamp_glow.py
"""
import json
import math
import os
import sys
import uuid

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))
import dae  # noqa: E402
import staging  # noqa: E402

GLOW_R = (0.1, 0.16, 0.26, 0.42, 0.67, 1.1, 1.75, 2.8)  # m: the balls' radii, nearest first (each 1.6 times the last)
HIDDEN_R = 0.01             # m: the speck drawn near the camera
NEAR_M = 60.0               # m: the speck nearer (the lens shows there)
# px: each ball's width on screen when the next, bigger one takes over (the last's: where the shape stops), falling
# with distance, so a far lamp is a smaller point than a near one
END_PX = (2.5, 2.3, 2.1, 1.9, 1.7, 1.5, 1.3)
# cd: the lens's light (build_lamp_lens.py's 0.60 x 0.34 m ellipse at nightlights.lua's 4000 nits, seen from under it;
# a cutoff road lamp throws less toward the horizon, 300-800 cd from a 16,000 lm head)
LENS_CD = 4000.0 * math.pi * 0.30 * 0.17
GLOW_NITS_MIN = 25.0        # nits: the far balls' least (the floodlit stone's, nightlights.lua bne_lm_floodlit)
GLOW_RGB = (1.0, 0.66, 0.33)  # the lens's sodium colour (nightlights.lua)
SCREEN_PX = 1130.0          # px per radian on screen: 1440 pixels high at the game's 65-degree field of view
# The engine's detail size per (shape radius / distance): measured on the PC (2560 x 1440, mesh quality high, its
# detailAdjust 2: the speck gave way to the next ball at 190 m, that one to the next at 575 m). Other screens and mesh
# qualities scale every switch distance alike: lower ones switch nearer (the balls a little larger on screen).
ENGINE_PX = 4100.0
LAST_PX = 2.0               # the detail size where the last ball stops (the engine's own files' smallest)
SEG, RINGS = 8, 5


def sphere(r):
    """(positions (n,3), tris (m,3) counter-clockwise from outside) of a UV sphere of radius r at the origin."""
    P = [(0.0, 0.0, r)]
    for i in range(1, RINGS):
        th = math.pi * i / RINGS
        for k in range(SEG):
            ph = 2 * math.pi * k / SEG
            P.append((r * math.sin(th) * math.cos(ph), r * math.sin(th) * math.sin(ph), r * math.cos(th)))
    P.append((0.0, 0.0, -r))
    T = []
    ring = lambda i, k: 1 + (i - 1) * SEG + k % SEG            # noqa: E731
    for k in range(SEG):
        T.append((0, ring(1, k), ring(1, k + 1)))
        T.append((len(P) - 1, ring(RINGS - 1, k + 1), ring(RINGS - 1, k)))
    for i in range(1, RINGS - 1):
        for k in range(SEG):
            a, b, c, d = ring(i, k), ring(i, k + 1), ring(i + 1, k), ring(i + 1, k + 1)
            T += [(a, c, d), (a, d, b)]
    return np.array(P), np.array(T, np.int64)


def detail_sizes():
    """The detail sizes (the engine's) where the speck and each ball start, falling: the speck from NEAR_M in, each
    ball out to where it spans its END_PX on screen."""
    R = GLOW_R[-1] * math.sqrt(3.0)                 # the shape's radius: its bounding box's half diagonal
    ends = [NEAR_M] + [2.0 * r * SCREEN_PX / px for r, px in zip(GLOW_R[:-1], END_PX)]
    return [R * ENGINE_PX / d for d in ends] + [LAST_PX]


def glow_nits():
    """Each ball's brightness (nits, 3 figures): LENS_CD over its disc, at least GLOW_NITS_MIN."""
    return [max(GLOW_NITS_MIN, float(f"{LENS_CD / (math.pi * r * r):.3g}")) for r in GLOW_R]


def write_materials(path):
    """lamps.materials.json with a bne_lamp_glow<k> per ball (dark by day, as the lens), the others kept."""
    with open(path) as f:
        mats = json.load(f)
    mats = {k: v for k, v in mats.items() if not k.startswith("bne_lamp_glow")}
    for k in range(len(GLOW_R)):
        name = f"bne_lamp_glow{k}"
        mats[name] = {"name": name, "mapTo": name, "class": "Material", "version": 1.5,
                      "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, "beamng-earth:" + name)),
                      "Stages": [{"baseColorFactor": [*GLOW_RGB, 1.0], "metallicFactor": 0, "roughnessFactor": 1,
                                  "emissiveFactor": [0, 0, 0]}, {}, {}, {}],
                      "castShadows": False}
    tmp = staging.tmp_for(path)
    with open(tmp, "w", newline="\n") as f:
        json.dump(mats, f, indent=2)
        f.write("\n")
    staging.replace(tmp, path)


def main():
    sizes = detail_sizes()
    geoms, nodes = [], []
    for k, r in enumerate((HIDDEN_R,) + GLOW_R):
        mat = "bne_lamp_lens" if k == 0 else f"bne_lamp_glow{k - 1}"
        P, T = sphere(r)
        n = P / r
        UV = np.c_[0.5 + 0.5 * n[:, 0], 0.5 + 0.5 * n[:, 1]]
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
            f'offset="2" set="0"/><p>{dae._indices(T, 3)}</p></triangles>\n</mesh></geometry>')
        name = f"glow{int(round(sizes[k]))}"
        nodes.append(f'<node id="{name}" name="{name}" type="NODE"><matrix sid="transform">1 0 0 0 0 1 0 0 0 0 1 0 0 0 '
                     f'0 1</matrix><instance_geometry url="#{g}"><bind_material><technique_common><instance_material '
                     f'symbol="mat0" target="#{mat}"/></technique_common></bind_material></instance_geometry>'
                     '</node>')
    names = ["bne_lamp_lens"] + [f"bne_lamp_glow{k}" for k in range(len(GLOW_R))]
    effects = [f'<effect id="{m}-fx"><profile_COMMON><technique sid="common"><lambert><diffuse><color>1 0.8 0.5 1'
               '</color></diffuse></lambert></technique></profile_COMMON></effect>' for m in names]
    materials = [f'<material id="{m}" name="{m}"><instance_effect url="#{m}-fx"/></material>' for m in names]
    ident = '<matrix sid="transform">1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1</matrix>'
    xml = f'''<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
<asset><unit name="meter" meter="1"/><up_axis>Z_UP</up_axis></asset>
<library_effects>{"".join(effects)}</library_effects>
<library_materials>{"".join(materials)}</library_materials>
<library_geometries>{"".join(geoms)}</library_geometries>
<library_visual_scenes><visual_scene id="scene"><node id="base00" name="base00" type="NODE">{ident}<node id="start01" name="start01" type="NODE">{ident}
{"".join(nodes)}
</node></node></visual_scene></library_visual_scenes>
<scene><instance_visual_scene url="#scene"/></scene>
</COLLADA>
'''
    shapes = os.path.join(HERE, "..", "mod", "beamng_earth", "levels", "beamng_earth", "art", "shapes", "beamngEarth")
    out = os.path.join(shapes, "bne_lamp_glow.dae")
    tmp = staging.tmp_for(out)
    with open(tmp, "w", newline="\n") as f:
        f.write(xml)
    staging.replace(tmp, out)
    write_materials(os.path.join(shapes, "lamps.materials.json"))
    print(out, os.path.getsize(out), "bytes; detail sizes", [round(s, 1) for s in sizes])
    print("nightlights.lua M.GLOW_NITS = {", ", ".join(f"{n:g}" for n in glow_nits()), "}")


if __name__ == "__main__":
    main()
