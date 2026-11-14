"""build_golf_flag.py: a pin flag (mod/.../art/shapes/beamngEarth/bne_golf_flag.dae).

A thin pole and a small pennant, Z up, origin on the ground. golf.py places one where OpenStreetMap
marks a hole (golf=pin, or the end of a golf=hole way). The pennant is local +Y so the forest yaw
turns it.

    python tools/build_golf_flag.py
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))
import dae  # noqa: E402

OUT = os.path.join(HERE, "..", "mod", "beamng_earth", "levels", "beamng_earth", "art", "shapes", "beamngEarth")
POLE_H = 2.05
POLE = 0.035          # half-width of the pole (m)
FLAG_Y = 0.62         # pennant length
FLAG_Z0, FLAG_Z1 = 1.62, 2.02


def _box(x0, x1, y0, y1, z0, z1):
    """Vertices and triangles of a box (both windings are the outward faces)."""
    p = np.array([
        [x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
        [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1],
    ], np.float64)
    # two triangles a face, counter-clockwise from outside
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    return p, np.array(faces, np.int64)


def _pennant():
    """A double-sided triangle off the pole, tip at +Y."""
    p = np.array([
        [0.0, 0.0, FLAG_Z0],
        [0.0, 0.0, FLAG_Z1],
        [0.0, FLAG_Y, (FLAG_Z0 + FLAG_Z1) / 2.0],
    ], np.float64)
    return p, np.array([[0, 1, 2], [0, 2, 1]], np.int64)


def _mesh(positions, tris, material):
    nv = len(positions)
    uv = np.zeros((nv, 2), np.float64)
    return positions.ravel(), uv.ravel(), tris.ravel(), material


def main():
    pole_p, pole_t = _box(-POLE, POLE, -POLE, POLE, 0.0, POLE_H)
    flag_p, flag_t = _pennant()
    # one Collada file, two materials: the writer is one mesh, so the geometries are concatenated
    # under two nodes by writing the document here (dae.write_grid_dae is a single material).
    parts = (
        ("pole", "bne_golf_pole", pole_p, pole_t),
        ("flag", "bne_golf_flag", flag_p, flag_t),
    )
    geos, nodes = [], []
    for name, mat, pos, tris in parts:
        nv, nt = len(pos), len(tris)
        uv = np.zeros((nv, 2))
        geos.append(
            f'<geometry id="{name}" name="{name}"><mesh>'
            f'<source id="{name}-pos"><float_array id="{name}-pos-a" count="{nv * 3}">{dae._floats(pos.ravel())}</float_array>'
            f'<technique_common><accessor source="#{name}-pos-a" count="{nv}" stride="3">'
            f'<param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>'
            f'</accessor></technique_common></source>'
            f'<source id="{name}-uv"><float_array id="{name}-uv-a" count="{nv * 2}">{dae._floats(uv.ravel())}</float_array>'
            f'<technique_common><accessor source="#{name}-uv-a" count="{nv}" stride="2">'
            f'<param name="S" type="float"/><param name="T" type="float"/></accessor></technique_common></source>'
            f'<vertices id="{name}-vtx"><input semantic="POSITION" source="#{name}-pos"/></vertices>'
            f'<triangles material="{name}mat" count="{nt}">'
            f'<input semantic="VERTEX" source="#{name}-vtx" offset="0"/>'
            f'<input semantic="TEXCOORD" source="#{name}-uv" offset="1"/>'
            f'<p>{dae._indices(tris, 2)}</p></triangles></mesh></geometry>')
        nodes.append(
            f'<node id="{name}" name="{name}"><instance_geometry url="#{name}">'
            f'<bind_material><technique_common>'
            f'<instance_material symbol="{name}mat" target="#{mat}"/>'
            f'</technique_common></bind_material></instance_geometry></node>')
    effects = []
    for mat, rgb in (("bne_golf_pole", "0.28 0.28 0.30 1"), ("bne_golf_flag", "0.93 0.16 0.12 1")):
        effects.append(
            f'<effect id="{mat}-fx"><profile_COMMON><technique sid="common"><lambert>'
            f'<diffuse><color>{rgb}</color></diffuse></lambert></technique></profile_COMMON></effect>')
    mats = "".join(
        f'<material id="{m}" name="{m}"><instance_effect url="#{m}-fx"/></material>'
        for m in ("bne_golf_pole", "bne_golf_flag"))
    xml = f'''<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
<asset><unit name="meter" meter="1"/><up_axis>Z_UP</up_axis></asset>
<library_effects>{"".join(effects)}</library_effects>
<library_materials>{mats}</library_materials>
<library_geometries>{"".join(geos)}</library_geometries>
<library_visual_scenes><visual_scene id="scene">{"".join(nodes)}</visual_scene></library_visual_scenes>
<scene><instance_visual_scene url="#scene"/></scene>
</COLLADA>
'''
    path = os.path.join(OUT, "bne_golf_flag.dae")
    import staging
    tmp = staging.tmp_for(path)
    with open(tmp, "w", newline="\n") as f:
        f.write(xml)
    staging.replace(tmp, path)
    print(path, os.path.getsize(path), "bytes")


if __name__ == "__main__":
    main()
