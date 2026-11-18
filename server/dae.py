"""Minimal Collada (.dae) writer for BeamNG TSStatic tiles.

BeamNG imports Collada with Z-up when <up_axis>Z_UP</up_axis> is set; the
engine converts and caches it as .cdae next to the source.
"""
import os

import numpy as np
import dmath
import staging


def _floats(a):
    v = np.asarray(a, dtype=np.float64).ravel().tolist()
    return ("%.4f " * len(v) % tuple(v))[:-1]


def _indices(tris, inputs):
    """Collada <p>: one index per input per corner; all streams share the vertex index."""
    v = np.repeat(np.asarray(tris, dtype=np.int64).ravel(), inputs).tolist()
    return ("%d " * len(v) % tuple(v))[:-1]


def write_grid_dae(path, positions, uvs, tris, material="bngEarthDae", texture=None, normals=None, uvs2=None):
    """positions: flat [x,y,z,...], uvs: flat [u,v,...], tris: flat vertex indices
    (counter-clockwise seen from +Z = facing up), normals: optional flat [nx,ny,nz,...]
    per vertex, uvs2: optional second texture coordinate set (TEXCOORD set 1, e.g. a detail
    map's). Accepts lists or flat numpy arrays."""
    nv = len(positions) // 3
    nt = len(tris) // 3
    if normals is None:
        p = _indices(tris, 2)
        nrm_src = nrm_in = ""
        uv_off = 1
    else:
        p = _indices(tris, 3)
        nrm_src = (f'<source id="g0-nrm"><float_array id="g0-nrm-a" count="{nv*3}">{_floats(normals)}</float_array>\n'
                   f'<technique_common><accessor source="#g0-nrm-a" count="{nv}" stride="3"><param name="X" type="float"/>'
                   '<param name="Y" type="float"/><param name="Z" type="float"/></accessor></technique_common></source>\n')
        nrm_in = '<input semantic="NORMAL" source="#g0-nrm" offset="1"/>'
        uv_off = 2
    uv2_src = uv2_in = ""
    if uvs2 is not None:
        uv2_src = (f'<source id="g0-uv2"><float_array id="g0-uv2-a" count="{nv*2}">{_floats(uvs2)}</float_array>\n'
                   f'<technique_common><accessor source="#g0-uv2-a" count="{nv}" stride="2"><param name="S" type="float"/>'
                   '<param name="T" type="float"/></accessor></technique_common></source>\n')
        uv2_in = f'<input semantic="TEXCOORD" source="#g0-uv2" offset="{uv_off}" set="1"/>'
    img = ""
    effect_diffuse = '<color>0.6 0.6 0.6 1</color>'
    if texture:
        img = f'<library_images><image id="img0"><init_from>{texture}</init_from></image></library_images>'
        effect_diffuse = '<texture texture="samp0" texcoord="UVMap"/>'
        sampler = ('<newparam sid="surf0"><surface type="2D"><init_from>img0</init_from></surface></newparam>'
                   '<newparam sid="samp0"><sampler2D><source>surf0</source></sampler2D></newparam>')
    else:
        sampler = ""
    xml = f'''<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
<asset><unit name="meter" meter="1"/><up_axis>Z_UP</up_axis></asset>
{img}
<library_effects><effect id="{material}-fx"><profile_COMMON>{sampler}<technique sid="common"><lambert><diffuse>{effect_diffuse}</diffuse></lambert></technique></profile_COMMON></effect></library_effects>
<library_materials><material id="{material}" name="{material}"><instance_effect url="#{material}-fx"/></material></library_materials>
<library_geometries><geometry id="g0" name="tile"><mesh>
<source id="g0-pos"><float_array id="g0-pos-a" count="{nv*3}">{_floats(positions)}</float_array>
<technique_common><accessor source="#g0-pos-a" count="{nv}" stride="3"><param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/></accessor></technique_common></source>
{nrm_src}<source id="g0-uv"><float_array id="g0-uv-a" count="{nv*2}">{_floats(uvs)}</float_array>
<technique_common><accessor source="#g0-uv-a" count="{nv}" stride="2"><param name="S" type="float"/><param name="T" type="float"/></accessor></technique_common></source>
{uv2_src}<vertices id="g0-vtx"><input semantic="POSITION" source="#g0-pos"/></vertices>
<triangles material="mat0" count="{nt}"><input semantic="VERTEX" source="#g0-vtx" offset="0"/>{nrm_in}<input semantic="TEXCOORD" source="#g0-uv" offset="{uv_off}" set="0"/>{uv2_in}<p>{p}</p></triangles>
</mesh></geometry></library_geometries>
<library_visual_scenes><visual_scene id="scene"><node id="tile" name="tile"><instance_geometry url="#g0"><bind_material><technique_common><instance_material symbol="mat0" target="#{material}"/></technique_common></bind_material></instance_geometry></node></visual_scene></library_visual_scenes>
<scene><instance_visual_scene url="#scene"/></scene>
</COLLADA>
'''
    tmp = staging.tmp_for(path)
    with open(tmp, "w", newline="\n") as f:     # the same bytes on Windows (no CRLF)
        f.write(xml)
    staging.replace(tmp, path)


def grid(n_cells, spacing, zfun=lambda x, y: 0.0, uv_repeat=8.0):
    N = n_cells + 1
    pos, uv, tri = [], [], []
    for j in range(N):
        for i in range(N):
            x, y = i * spacing, j * spacing
            pos += [x, y, zfun(x, y)]
            uv += [i / n_cells * uv_repeat, j / n_cells * uv_repeat]
    for j in range(n_cells):
        for i in range(n_cells):
            a, b, c, d = j * N + i, j * N + i + 1, (j + 1) * N + i, (j + 1) * N + i + 1
            tri += [a, b, c, b, d, c]  # CCW from +Z in a right-handed Z-up frame
    return pos, uv, tri


if __name__ == "__main__":
    import math
    import sys
    pos, uv, tri = grid(64, 2.0, lambda x, y: dmath.sin(x * 0.05) * 3)
    write_grid_dae(sys.argv[1], pos, uv, tri, texture="probe_checker_v1.png")
    print("wrote", sys.argv[1], len(tri) // 3, "tris")
