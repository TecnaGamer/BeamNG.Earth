"""Minimal Collada (.dae) writer for BeamNG TSStatic tiles.

BeamNG imports Collada with Z-up when <up_axis>Z_UP</up_axis> is set; the
engine converts and caches it as .cdae next to the source.
"""
import numpy as np

import cdae
import dmath
import staging


# ColladaAppMesh::getPrimitives reserves room for three *new* tuples before every triangle,
# even a triangle that reuses all three. At 65,534 it can pad the array to 65,536 and restart.
IMPORT_VERTS_MAX = cdae.VERTS_MAX - 2


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
    P = np.asarray(positions).reshape(-1, 3)
    T = np.asarray(tris, np.int64).reshape(-1, 3)
    if T.size and (T.min() < 0 or T.max() >= len(P)):
        raise ValueError("triangle index outside positions")
    if len(P) <= IMPORT_VERTS_MAX:
        geometries = [_geometry("g0", "tile", positions, uvs, tris, normals, uvs2)]
        nodes = [_node("g0", "tile", material)]
    else:
        # A separate geometry AND scene object for each piece. Names end in letters so the
        # importer cannot interpret a piece number as a different trailing-number LOD.
        UV = np.asarray(uvs).reshape(-1, 2)
        UV2 = None if uvs2 is None else np.asarray(uvs2).reshape(-1, 2)
        N = cdae.face_normal_sum(P, T) if normals is None else np.asarray(normals).reshape(-1, 3)
        geometries, nodes = [], []
        for i, (vertices, local) in enumerate(cdae.mesh_parts(T, len(P), IMPORT_VERTS_MAX)):
            gid, name = f"g{i}", f"tile_part{i}_mesh"
            geometries.append(_geometry(gid, name, P[vertices].ravel(), UV[vertices].ravel(), local.ravel(),
                                        N[vertices].ravel(), None if UV2 is None else UV2[vertices].ravel()))
            nodes.append(_node(gid, name, material))
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
<library_geometries>{"".join(geometries)}</library_geometries>
<library_visual_scenes><visual_scene id="scene">{"".join(nodes)}</visual_scene></library_visual_scenes>
<scene><instance_visual_scene url="#scene"/></scene>
</COLLADA>
'''
    tmp = staging.tmp_for(path)
    with open(tmp, "w", newline="\n") as f:     # the same bytes on Windows (no CRLF)
        f.write(xml)
    staging.replace(tmp, path)


def _node(gid, name, material):
    return (f'<node id="{name}" name="{name}"><instance_geometry url="#{gid}"><bind_material><technique_common>'
            f'<instance_material symbol="mat0" target="#{material}"/></technique_common></bind_material>'
            '</instance_geometry></node>')


def _geometry(gid, name, positions, uvs, tris, normals, uvs2):
    nv = len(positions) // 3
    nt = len(tris) // 3
    if nv > IMPORT_VERTS_MAX:
        raise ValueError("DAE geometry exceeds the 16-bit vertex limit")
    if normals is None:
        p = _indices(tris, 2)
        nrm_src = nrm_in = ""
        uv_off = 1
    else:
        p = _indices(tris, 3)
        nrm_src = (f'<source id="{gid}-nrm"><float_array id="{gid}-nrm-a" count="{nv*3}">{_floats(normals)}</float_array>\n'
                   f'<technique_common><accessor source="#{gid}-nrm-a" count="{nv}" stride="3"><param name="X" type="float"/>'
                   '<param name="Y" type="float"/><param name="Z" type="float"/></accessor></technique_common></source>\n')
        nrm_in = f'<input semantic="NORMAL" source="#{gid}-nrm" offset="1"/>'
        uv_off = 2
    uv2_src = uv2_in = ""
    if uvs2 is not None:
        uv2_src = (f'<source id="{gid}-uv2"><float_array id="{gid}-uv2-a" count="{nv*2}">{_floats(uvs2)}</float_array>\n'
                   f'<technique_common><accessor source="#{gid}-uv2-a" count="{nv}" stride="2"><param name="S" type="float"/>'
                   '<param name="T" type="float"/></accessor></technique_common></source>\n')
        uv2_in = f'<input semantic="TEXCOORD" source="#{gid}-uv2" offset="{uv_off}" set="1"/>'
    return f'''<geometry id="{gid}" name="{name}"><mesh>
<source id="{gid}-pos"><float_array id="{gid}-pos-a" count="{nv*3}">{_floats(positions)}</float_array>
<technique_common><accessor source="#{gid}-pos-a" count="{nv}" stride="3"><param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/></accessor></technique_common></source>
{nrm_src}<source id="{gid}-uv"><float_array id="{gid}-uv-a" count="{nv*2}">{_floats(uvs)}</float_array>
<technique_common><accessor source="#{gid}-uv-a" count="{nv}" stride="2"><param name="S" type="float"/><param name="T" type="float"/></accessor></technique_common></source>
{uv2_src}<vertices id="{gid}-vtx"><input semantic="POSITION" source="#{gid}-pos"/></vertices>
<triangles material="mat0" count="{nt}"><input semantic="VERTEX" source="#{gid}-vtx" offset="0"/>{nrm_in}<input semantic="TEXCOORD" source="#{gid}-uv" offset="{uv_off}" set="0"/>{uv2_in}<p>{p}</p></triangles>
</mesh></geometry>'''


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
