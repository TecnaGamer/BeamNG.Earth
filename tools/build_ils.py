"""build_ils.py: two unbranded ILS aerials (mod/.../art/shapes/beamngEarth/bne_ils_loc.dae, bne_ils_gs.dae and
ils.materials.json), for the airports' runway ends (server airfieldlights.py ils_masts; trees.lua CATALOG ils_loc, ils_gs).

    python tools/build_ils.py

    bne_ils_loc  a localizer array: a row of LOC_N thin dipole masts 30 m wide (LOC_W), 3 m tall (LOC_H) on a low frame
                 (two rails and a post at each end): each mast a white square pole with a red dipole element across it
                 and, at the high detail, a second shorter one lower down on a boom, the front (the elements) toward +Y
    bne_ils_gs   a glide-slope mast: a 15 m (GS_H) square lattice tower (four legs in red / white 3 m bands, horizontal ties
                 and one diagonal a face a panel), three white dipole antennas on booms toward +Y and an equipment
                 cabinet at the foot

Z up, front (the antennas) toward local +Y, the origin on the ground at the centre of the array / the tower's foot, scale 1 =
real size, so a model's height is its top (airfieldlights.ILS_H and trees.lua's catalogue carry it). Three flat colour
materials (ils.materials.json, double sided): bne_ils_white, bne_ils_red, bne_ils_grey. No textures, no lettering. Two
levels of detail (nodes bne_ils_<kind>96 and <kind>12: the engine draws them by pixel size, as the other bne props), no
collision mesh (trees.lua: the aerials never collide; real ones are frangible). Built on build_aircraft's Mesh.
"""
import json
import math
import os
import sys
import uuid

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))
sys.path.insert(0, HERE)
import build_aircraft as ba  # noqa: E402

OUT = ba.OUT
LODS = (96, 12)                 # the pixel sizes the two levels of detail start at (the node names' suffixes)
LOC_W, LOC_H, LOC_N = 30.0, 3.0, 12     # m: the array's width, its masts' height, how many masts
GS_H = 15.0                     # m: the glide-slope mast's height (its top)
GS_HALF = 0.30                  # m: the lattice's half width (legs at +-GS_HALF)
GS_BAND_M = 3.0                 # m: a red / white band (and a panel of the lattice)
MATERIALS = {                   # name -> (baseColorFactor, metallic, roughness)
    "bne_ils_white": ((0.92, 0.92, 0.90, 1.0), 0.0, 0.5),
    "bne_ils_red": ((0.78, 0.07, 0.05, 1.0), 0.0, 0.55),
    "bne_ils_grey": ((0.55, 0.57, 0.60, 1.0), 0.35, 0.5),
}
W, R, G = "bne_ils_white", "bne_ils_red", "bne_ils_grey"
KINDS = ("loc", "gs")


def box(m, mat, x0, x1, y0, y1, z0, z1):
    """A closed box (flat faces)."""
    ring = lambda z: np.array([[x0, y0, z], [x1, y0, z], [x1, y1, z], [x0, y1, z]], np.float64)
    m.loft(mat, [ring(z0), ring(z1)], caps=(mat, mat), smooth=False)


def bar(m, mat, p0, p1, r, k=3):
    """A thin bar from p0 to p1 (a k-sided tube, open ends: they sit inside other parts or are too small to see)."""
    m.tube(mat, p0, p1, r, r, k, smooth=False)


def localizer(lod):
    """The localizer array's Mesh (lod 0 / 1)."""
    m = ba.Mesh()
    hi = lod == 0
    xs = np.linspace(-LOC_W / 2.0 + 1.0, LOC_W / 2.0 - 1.0, LOC_N)
    k = 4 if hi else 3
    for x in xs:
        bar(m, W, (x, 0.0, 0.0), (x, 0.0, LOC_H), 0.05, k)                      # the mast
        bar(m, R, (x - 0.65, 0.35, 2.7), (x + 0.65, 0.35, 2.7), 0.025, 3)         # the dipole element
        if hi:
            bar(m, W, (x, 0.0, 2.7), (x, 0.35, 2.7), 0.02, 3)                     # its boom
            bar(m, R, (x - 0.45, 0.25, 1.7), (x + 0.45, 0.25, 1.7), 0.02, 3)      # a shorter element lower down
            bar(m, W, (x, 0.0, 1.7), (x, 0.25, 1.7), 0.015, 3)
    for z in ((0.35, 2.2) if hi else (2.2,)):                                      # the frame: rails along the row
        bar(m, G, (-LOC_W / 2.0, -0.15, z), (LOC_W / 2.0, -0.15, z), 0.05, 4)
    for x in (-LOC_W / 2.0, LOC_W / 2.0):                                          # ... and a post at each end
        bar(m, G, (x, -0.15, 0.0), (x, -0.15, 2.5), 0.08, 4)
    return m


def glideslope(lod):
    """The glide-slope mast's Mesh (lod 0 / 1): the lattice tower, its antennas and the cabinet."""
    m = ba.Mesh()
    hi = lod == 0
    h = GS_HALF
    legs = [(sx * h, sy * h) for sx, sy in ((1, 1), (-1, 1), (-1, -1), (1, -1))]
    n_band = int(round(GS_H / GS_BAND_M))
    for i in range(n_band):                                                        # the legs in banded sections
        z0, z1 = i * GS_BAND_M, (i + 1) * GS_BAND_M
        for x, y in legs:
            bar(m, R if i % 2 == 0 else W, (x, y, z0), (x, y, z1), 0.04, 4 if hi else 3)
        if hi:
            for j in range(4):                                                     # a diagonal a face a panel
                (xa, ya), (xb, yb) = legs[j], legs[(j + 1) % 4]
                up = (j + i) % 2 == 0
                bar(m, G, (xa, ya, z0 if up else z1), (xb, yb, z1 if up else z0), 0.015, 3)
    if hi:
        for i in range(n_band + 1):                                                # the horizontal ties
            z = min(max(i * GS_BAND_M, 0.03), GS_H - 0.02)
            for j in range(4):
                (xa, ya), (xb, yb) = legs[j], legs[(j + 1) % 4]
                bar(m, G, (xa, ya, z), (xb, yb, z), 0.015, 3)
    for z in (4.3, 8.6, 14.5):                                                     # the antennas, toward +Y
        bar(m, G, (0.0, h, z), (0.0, h + 0.45, z), 0.03, 3)
        box(m, W, -0.55, 0.55, h + 0.45, h + 0.60, z - 0.20, z + 0.20)
    box(m, G, -0.45, 0.45, h + 0.35, h + 1.05, 0.0, 1.2)                           # the equipment cabinet
    return m


def build(kind, lod):
    return (localizer if kind == "loc" else glideslope)(lod)


def parts(kind, lod):
    """{material: (vertices, normals, triangles)} of one level of detail."""
    return build(kind, lod).finish()


def height(kind):
    """The model's height over its origin (its top)."""
    return float(ba.bounds(parts(kind, 0))[1][2])


def collada(kind):
    """One .dae: base00 > start01 > a node per level of detail (named <name><pixel size>), one geometry each; an empty
    collision-1 node beside start01 (no collision: the aerials are frangible, trees.lua NO_COLLIDE)."""
    name = "bne_ils_" + kind
    geoms, nodes = [], []
    for size, lod in zip(LODS, (0, 1)):
        pt = parts(kind, lod)
        gid = f"{name}{size}-g"
        geoms.append(ba._geometry(gid, pt))
        nodes.append(f'<node id="{name}{size}" name="{name}{size}"><instance_geometry url="#{gid}"><bind_material><technique_common>'
                     + "".join(f'<instance_material symbol="{k}-mat" target="#{k}-mat"/>' for k in sorted(pt))
                     + '</technique_common></bind_material></instance_geometry></node>\n')
    effects = "".join(f'<effect id="{k}-fx"><profile_COMMON><technique sid="common"><lambert><diffuse><color>'
                      f'{" ".join(f"{c:g}" for c in MATERIALS[k][0])}</color></diffuse></lambert></technique></profile_COMMON></effect>\n'
                      for k in MATERIALS)
    mats = "".join(f'<material id="{k}-mat" name="{k}"><instance_effect url="#{k}-fx"/></material>\n' for k in MATERIALS)
    return f'''<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
<asset><contributor><authoring_tool>BeamNG.Earth tools/build_ils.py</authoring_tool></contributor>
<unit meter="1" name="meter"/><up_axis>Z_UP</up_axis></asset>
<library_effects>
{effects}</library_effects>
<library_materials>
{mats}</library_materials>
<library_geometries>
{"".join(geoms)}</library_geometries>
<library_visual_scenes><visual_scene id="scene" name="scene">
<node id="base00" name="base00"><node id="start01" name="start01">
{"".join(nodes)}</node><node id="collision-1" name="collision-1"/></node>
</visual_scene></library_visual_scenes>
<scene><instance_visual_scene url="#scene"/></scene>
</COLLADA>
'''


def materials():
    """The materials file: the three flat materials, double sided."""
    out = {}
    for name, (rgba, metal, rough) in MATERIALS.items():
        out[name] = {"name": name, "mapTo": name, "class": "Material", "version": 1.5,
                     "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, "beamng.earth/" + name)),
                     "Stages": [{"baseColorFactor": list(rgba), "metallicFactor": metal, "roughnessFactor": rough},
                                {}, {}, {}],
                     "doubleSided": True}
    return json.dumps(out, indent=2) + "\n"


def main():
    import staging
    files = [("bne_ils_" + kind + ".dae", collada(kind)) for kind in KINDS] + [("ils.materials.json", materials())]
    for fname, text in files:
        path = os.path.join(OUT, fname)
        tmp = staging.tmp_for(path)
        with open(tmp, "w", newline="\n") as f:
            f.write(text)
        staging.replace(tmp, path)
        print(path, os.path.getsize(path), "bytes")
    for kind in KINDS:
        for lod in (0, 1):
            pt = parts(kind, lod)
            lo, hi = ba.bounds(pt)
            print(f"{kind} LOD{lod}: {ba.tri_count(pt)} triangles; x {lo[0]:.2f}..{hi[0]:.2f}, y {lo[1]:.2f}..{hi[1]:.2f}, "
                  f"z {lo[2]:.2f}..{hi[2]:.2f}")


if __name__ == "__main__":
    main()
