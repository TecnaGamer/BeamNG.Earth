"""build_windsock.py: an airfield windsock (mod/.../art/shapes/beamngEarth/bne_windsock.dae and windsock.materials.json).

An unbranded ICAO-style windsock (Annex 14 5.1.1: a truncated cone, 3.6 m long, 0.9 m across the mouth, 0.45 m at the
small end, five bands alternating orange and white): a tapered steel pole, a horizontal-axis ring frame on its top and
the sock hanging from the ring. Z up, origin on the ground at the foot of the pole.

    AXES: the sock points along local +Y (the ring lies in the XZ plane at y = 0; the sock streams away from it towards
    +y), drooping: its centreline bends down (-Z) 0..DROOP_END_DEG along its length, so the chord from the mouth to
    the tip sits about 10 degrees below horizontal. The forest row's yaw turns it (server windsocks.py: yaw = the
    direction the sock points, counter-clockwise from +x, minus 90).

Three materials (bne_windsock_pole, _orange, _white; windsock.materials.json, double sided: the fabric is a one-sided
sheet seen from both). One level of detail, no collision geometry (trees.lua: the group never collides). About 210
triangles. HEIGHT is the mesh's height (server/windsocks.py WINDSOCK_H and trees.lua's catalogue height).

    python tools/build_windsock.py
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

OUT = os.path.join(HERE, "..", "mod", "beamng_earth", "levels", "beamng_earth", "art", "shapes", "beamngEarth")
NAME = "bne_windsock"
RING_Z = 6.0                    # m: the ring's centre (and the sock's mouth) over the ground
RING_R = 0.45                   # m: the mouth's radius (0.9 m across)
BAR = 0.02                      # m: half the ring bar's square section
RING_SEGS = 12
POLE_R0, POLE_R1 = 0.10, 0.05   # m: the pole's radius at its foot and at the ring
POLE_SIDES = 8
SOCK_LEN = 3.6                  # m along the sock (the sum of its band chords)
TIP_R = 0.225                   # m: the small end (0.45 m across)
BANDS = 5                       # orange, white, orange, white, orange (the mouth's band orange)
SOCK_SIDES = 10
DROOP_END_DEG = 20.0            # the centreline's slope at the tip (it grows linearly from 0 at the mouth: the chord ~ half)
MATERIALS = {                   # name -> (baseColorFactor, metallic, roughness)
    "bne_windsock_pole": ((0.62, 0.63, 0.65, 1.0), 0.35, 0.5),
    "bne_windsock_orange": ((0.95, 0.30, 0.02, 1.0), 0.0, 0.85),
    "bne_windsock_white": ((0.92, 0.92, 0.90, 1.0), 0.0, 0.85),
}


def _skin(rings, closed=False):
    """Vertices (n, 3) and outward triangles (m, 3) of a tube through rings (a list of (k, 3) arrays, the same k, the
    points in the same order round): quads between consecutive rings (the last to the first when closed), each wound
    to face away from the mean of its two rings' centres."""
    rings = [np.asarray(r, np.float64) for r in rings]
    k = len(rings[0])
    verts = np.concatenate(rings)
    tris = []
    pairs = [(i, i + 1) for i in range(len(rings) - 1)] + ([(len(rings) - 1, 0)] if closed else [])
    for a, b in pairs:
        centre = (rings[a].mean(axis=0) + rings[b].mean(axis=0)) / 2.0
        for j in range(k):
            j1 = (j + 1) % k
            q = (a * k + j, a * k + j1, b * k + j1, b * k + j)
            for t in ((q[0], q[1], q[2]), (q[0], q[2], q[3])):
                p0, p1, p2 = verts[t[0]], verts[t[1]], verts[t[2]]
                n = np.cross(p1 - p0, p2 - p0)
                if n @ ((p0 + p1 + p2) / 3.0 - centre) < 0.0:
                    t = (t[0], t[2], t[1])
                tris.append(t)
    return verts, np.array(tris, np.int64)


def _join(parts):
    """One (verts, tris) of several (the second's indices after the first's vertices)."""
    verts, tris, base = [], [], 0
    for v, t in parts:
        verts.append(v)
        tris.append(t + base)
        base += len(v)
    return np.concatenate(verts), np.concatenate(tris)


def pole():
    """The tapered octagonal pole from the ground to the ring's lowest point."""
    top = RING_Z - RING_R
    a = np.linspace(0.0, 2.0 * math.pi, POLE_SIDES, endpoint=False)
    rings = [np.c_[r * np.cos(a), r * np.sin(a), np.full(POLE_SIDES, z)] for r, z in ((POLE_R0, 0.0), (POLE_R1, top))]
    return _skin(rings)


def ring():
    """The ring frame: a square-section bar round a circle of radius RING_R in the XZ plane about (0, 0, RING_Z)."""
    stations = []
    for i in range(RING_SEGS):
        t = 2.0 * math.pi * i / RING_SEGS
        c = np.array([RING_R * math.cos(t), 0.0, RING_Z + RING_R * math.sin(t)])
        radial = np.array([math.cos(t), 0.0, math.sin(t)])
        side = np.array([0.0, 1.0, 0.0])
        stations.append(np.array([c + BAR * (sx * radial + sy * side) for sx, sy in ((1, 1), (-1, 1), (-1, -1), (1, -1))]))
    return _skin(stations, closed=True)


def sock_stations():
    """The sock's BANDS + 1 station rings (SOCK_SIDES points each) from the mouth to the tip, the centreline drooping."""
    seg = SOCK_LEN / BANDS
    c = np.array([0.0, 0.0, RING_Z])
    a = np.linspace(0.0, 2.0 * math.pi, SOCK_SIDES, endpoint=False)
    out = []
    for k in range(BANDS + 1):
        alpha = math.radians(DROOP_END_DEG * k / BANDS)             # the section's tilt from the vertical
        r = RING_R + (TIP_R - RING_R) * k / BANDS
        u, v = np.array([1.0, 0.0, 0.0]), np.array([0.0, math.sin(alpha), math.cos(alpha)])
        out.append(np.array([c + r * (math.cos(t) * u + math.sin(t) * v) for t in a]))
        if k < BANDS:
            mid = math.radians(DROOP_END_DEG * (k + 0.5) / BANDS)
            c = c + seg * np.array([0.0, math.cos(mid), -math.sin(mid)])
    return out


def sock(colour_of):
    """The (verts, tris) of the bands whose index satisfies colour_of(band)."""
    st = sock_stations()
    parts = [_skin(st[k:k + 2]) for k in range(BANDS) if colour_of(k)]
    return _join(parts)


def parts():
    """[(geometry name, material, verts, tris)]."""
    return [("frame", "bne_windsock_pole", *_join([pole(), ring()])),
            ("orange", "bne_windsock_orange", *sock(lambda k: k % 2 == 0)),
            ("white", "bne_windsock_white", *sock(lambda k: k % 2 == 1))]


def height():
    """The mesh's height over its origin."""
    return float(max(v[:, 2].max() for _, _, v, _ in parts()))


def collada():
    geos, nodes, effects, mats = [], [], [], []
    for name, mat, pos, tris in parts():
        nv, nt = len(pos), len(tris)
        geos.append(
            f'<geometry id="{name}" name="{name}"><mesh>'
            f'<source id="{name}-pos"><float_array id="{name}-pos-a" count="{nv * 3}">{dae._floats(pos.ravel())}</float_array>'
            f'<technique_common><accessor source="#{name}-pos-a" count="{nv}" stride="3">'
            f'<param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>'
            f'</accessor></technique_common></source>'
            f'<source id="{name}-uv"><float_array id="{name}-uv-a" count="{nv * 2}">{dae._floats(np.zeros(nv * 2))}</float_array>'
            f'<technique_common><accessor source="#{name}-uv-a" count="{nv}" stride="2">'
            f'<param name="S" type="float"/><param name="T" type="float"/></accessor></technique_common></source>'
            f'<vertices id="{name}-vtx"><input semantic="POSITION" source="#{name}-pos"/></vertices>'
            f'<triangles material="{name}mat" count="{nt}">'
            f'<input semantic="VERTEX" source="#{name}-vtx" offset="0"/>'
            f'<input semantic="TEXCOORD" source="#{name}-uv" offset="1"/>'
            f'<p>{dae._indices(tris, 2)}</p></triangles></mesh></geometry>')
        nodes.append(
            f'<node id="{name}" name="{name}"><instance_geometry url="#{name}"><bind_material><technique_common>'
            f'<instance_material symbol="{name}mat" target="#{mat}"/></technique_common></bind_material>'
            f'</instance_geometry></node>')
        rgb = " ".join(f"{c:g}" for c in MATERIALS[mat][0])
        effects.append(f'<effect id="{mat}-fx"><profile_COMMON><technique sid="common"><lambert>'
                       f'<diffuse><color>{rgb}</color></diffuse></lambert></technique></profile_COMMON></effect>')
        mats.append(f'<material id="{mat}" name="{mat}"><instance_effect url="#{mat}-fx"/></material>')
    return f'''<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
<asset><unit name="meter" meter="1"/><up_axis>Z_UP</up_axis></asset>
<library_effects>{"".join(effects)}</library_effects>
<library_materials>{"".join(mats)}</library_materials>
<library_geometries>{"".join(geos)}</library_geometries>
<library_visual_scenes><visual_scene id="scene">{"".join(nodes)}</visual_scene></library_visual_scenes>
<scene><instance_visual_scene url="#scene"/></scene>
</COLLADA>
'''


def materials():
    """The materials file: the three materials, the fabric ones and the frame double sided."""
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
    for fname, text in ((NAME + ".dae", collada()), ("windsock.materials.json", materials())):
        path = os.path.join(OUT, fname)
        tmp = staging.tmp_for(path)
        with open(tmp, "w", newline="\n") as f:
            f.write(text)
        staging.replace(tmp, path)
        print(path, os.path.getsize(path), "bytes")
    tris = sum(len(t) for _, _, _, t in parts())
    print(f"{tris} triangles, height {height():.3f} m")


if __name__ == "__main__":
    main()
