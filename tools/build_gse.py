"""build_gse.py: unbranded airside vehicles and clutter (mod/.../art/shapes/beamngEarth/bne_gse_*.dae and gse.materials.json),
for the apron's busy-ness (server airsideprops.py; trees.lua CATALOG gse_*).

    python tools/build_gse.py

    bne_gse_tug_<livery>        a baggage tractor (cab, hood, an open deck behind)
    bne_gse_cart_<livery>       a baggage cart: a low deck with side and end walls, a drawbar
    bne_gse_pushback_<livery>   a pushback tug: a low, wide body, a glazed cab, an amber beacon
    bne_gse_belt_<livery>       a belt loader: a cab, a body, a conveyor boom rising to its tip
    bne_gse_fuel_<livery>       a refuelling truck: cab, cylindrical tank, equipment cabinet, six wheels
    bne_gse_catering_<livery>   a catering truck: cab, scissor lift, a raised box (its rear door at the narrow-body sill)
    bne_gse_stairs_<livery>     mobile passenger stairs: a flight with handrails up to a railed platform
    bne_gse_container_<colour>  a 20 ft cargo container, ribbed sides
    bne_gse_cone                a traffic cone

Z up, forward along local +Y, the origin on the ground under the middle of the footprint, scale 1 = real size, so the model's
height is its top's (HEIGHTS; trees.lua's catalogue carries them). The vehicles come in three plain liveries (white, yellow
and grey), the containers in white, grey and blue: each a file of its own (trees.lua lists them as the variants of one group
and the row's rank picks one). Materials (gse.materials.json, flat colours, double sided): bne_gse_<livery colour>,
bne_gse_frame (chassis, booms, wheel arches), bne_gse_dark (glass), bne_gse_tyre, bne_gse_orange (cones, beacons). No
textures, no logos. Two levels of detail (nodes <name>160 and <name>16 as the other bne props; the vehicles under 300
triangles each) and a box collision mesh (Colmesh_<name>-1) on all but the cone, so a car stops against a truck.
"""
import json
import math
import os
import sys
import uuid

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "server"))

import build_aircraft as ba  # noqa: E402

OUT = ba.OUT
LODS = (160, 16)
LIVERIES = ("white", "yellow", "grey")
CONTAINER_COLOURS = ("white", "grey", "blue")
KINDS = ("tug", "cart", "pushback", "belt", "fuel", "catering", "stairs", "container", "cone")
MATERIALS = {                   # name -> (baseColorFactor, metallic, roughness)
    "bne_gse_white": ((0.88, 0.88, 0.86, 1.0), 0.0, 0.5),
    "bne_gse_yellow": ((0.94, 0.74, 0.06, 1.0), 0.0, 0.5),
    "bne_gse_grey": ((0.55, 0.57, 0.60, 1.0), 0.1, 0.55),
    "bne_gse_blue": ((0.14, 0.28, 0.48, 1.0), 0.1, 0.6),
    "bne_gse_frame": ((0.26, 0.27, 0.29, 1.0), 0.2, 0.6),
    "bne_gse_dark": ((0.05, 0.07, 0.10, 1.0), 0.0, 0.15),
    "bne_gse_tyre": ((0.03, 0.03, 0.03, 1.0), 0.0, 0.9),
    "bne_gse_orange": ((0.95, 0.38, 0.04, 1.0), 0.0, 0.7),
}
F, D, T, O = "bne_gse_frame", "bne_gse_dark", "bne_gse_tyre", "bne_gse_orange"
# (length, width, height) m of each model's bounds (the server's footprints and the catalogue's heights; checked in the tests)
DIMS = {"tug": (2.9, 1.4, 1.8), "cart": (3.0, 1.7, 1.1), "pushback": (3.64, 2.26, 1.77), "belt": (6.0, 2.25, 2.54),
        "fuel": (7.26, 2.5, 2.9), "catering": (7.26, 2.5, 4.4), "stairs": (6.0, 2.3, 3.4), "container": (6.09, 2.5, 2.59),
        "cone": (0.36, 0.36, 0.70)}


def box(m, mat, x0, x1, y0, y1, z0, z1):
    """An axis-aligned box, flat faces."""
    c = np.array([(x0 + x1) / 2.0, (y0 + y1) / 2.0, (z0 + z1) / 2.0])
    P = lambda x, y, z: np.array([x, y, z], np.float64)
    for q in ([P(x0, y0, z0), P(x0, y1, z0), P(x0, y1, z1), P(x0, y0, z1)],
              [P(x1, y0, z0), P(x1, y1, z0), P(x1, y1, z1), P(x1, y0, z1)],
              [P(x0, y0, z0), P(x1, y0, z0), P(x1, y0, z1), P(x0, y0, z1)],
              [P(x0, y1, z0), P(x1, y1, z0), P(x1, y1, z1), P(x0, y1, z1)],
              [P(x0, y0, z0), P(x1, y0, z0), P(x1, y1, z0), P(x0, y1, z0)],
              [P(x0, y0, z1), P(x1, y0, z1), P(x1, y1, z1), P(x0, y1, z1)]):
        m.poly(mat, q, c)


def slab(m, mat, a, b):
    """A sloped slab between two rectangular sections a, b: each (x0, x1, y, z0, z1)."""
    ring = lambda x0, x1, y, z0, z1: np.array([[x0, y, z0], [x1, y, z0], [x1, y, z1], [x0, y, z1]], np.float64)
    m.loft(mat, [ring(*a), ring(*b)], caps=(mat, mat), smooth=False)


def wheel(m, hi, x, y, z, r, w):
    """A wheel on the axle at (x, y, z): an octagonal drum with its outer face closed (a box at the far level)."""
    if not hi:
        box(m, T, x - w / 2.0, x + w / 2.0, y - r, y + r, z - r, z + r)
        return
    caps = (None, T) if x >= 0 else (T, None)
    m.tube(T, (x - w / 2.0, y, z), (x + w / 2.0, y, z), r, r, 8, caps=caps, smooth=False)


def tug(m, hi, B):
    box(m, F, -0.55, 0.55, -1.40, 1.40, 0.22, 0.50)
    box(m, B, -0.65, 0.65, -1.45, -0.20, 0.50, 0.70)            # (the deck's skirt)
    box(m, F, -0.60, 0.60, -1.45, -0.20, 0.70, 0.74)            # (the open deck)
    box(m, B, -0.65, 0.65, -0.20, 0.90, 0.50, 1.00)             # the cab's base
    box(m, B, -0.60, 0.60, 0.90, 1.45, 0.50, 1.05)              # the hood
    box(m, D, -0.58, 0.58, -0.12, 0.84, 1.00, 1.70)             # the glazing
    box(m, B, -0.70, 0.70, -0.25, 0.95, 1.70, 1.80)             # the roof
    for sx in (-1, 1):
        for y in (-0.95, 0.95):
            wheel(m, hi, sx * 0.60, y, 0.28, 0.28, 0.22)


def cart(m, hi, B):
    box(m, F, -0.85, 0.85, -1.50, 1.10, 0.30, 0.42)             # the deck
    for sx in (-1, 1):
        box(m, B, min(sx * 0.80, sx * 0.85), max(sx * 0.80, sx * 0.85), -1.50, 1.10, 0.42, 1.10)    # the sides
    box(m, B, -0.85, 0.85, -1.50, -1.45, 0.42, 1.10)            # the end walls
    box(m, B, -0.85, 0.85, 1.05, 1.10, 0.42, 1.10)
    box(m, F, -0.05, 0.05, 1.10, 1.50, 0.35, 0.45)              # the drawbar
    for sx in (-1, 1):
        for y in (-1.05, 0.65):
            wheel(m, hi, sx * 0.78, y, 0.20, 0.20, 0.12)


def pushback(m, hi, B):
    box(m, B, -0.95, 0.95, -1.70, 1.70, 0.30, 0.95)
    box(m, F, -1.0, 1.0, 1.70, 1.82, 0.30, 0.65)                # the bumpers
    box(m, F, -1.0, 1.0, -1.82, -1.70, 0.30, 0.65)
    box(m, D, -0.55, 0.55, -0.30, 0.90, 0.95, 1.60)             # the glazed cab
    box(m, B, -0.60, 0.60, -0.35, 0.95, 1.60, 1.68)
    box(m, O, -0.08, 0.08, 0.0, 0.16, 1.68, 1.77)               # the beacon
    for sx in (-1, 1):
        for y in (-1.10, 1.10):
            wheel(m, hi, sx * 1.0, y, 0.32, 0.32, 0.26)


def belt(m, hi, B):
    box(m, F, -0.90, 0.90, -3.0, 0.40, 0.35, 0.75)
    box(m, B, -0.95, 0.95, -0.15, 0.40, 0.75, 1.15)             # the body behind the cab
    box(m, B, -0.90, 0.90, -3.0, -1.8, 0.75, 2.0)               # the cab
    box(m, D, -0.75, 0.75, -1.82, -1.70, 1.2, 1.85)             # (its glazing, toward the boom)
    slab(m, F, (-0.60, 0.60, -1.5, 1.15, 1.50), (-0.60, 0.60, 3.0, 2.15, 2.50))                # the boom
    slab(m, D, (-0.48, 0.48, -1.5, 1.50, 1.54), (-0.48, 0.48, 3.0, 2.50, 2.54))                # its belt
    box(m, F, -0.08, 0.08, 0.80, 0.96, 0.75, 1.60)              # (the boom's prop)
    for sx in (-1, 1):
        for y in (-2.2, 0.0):
            wheel(m, hi, sx * 1.0, y, 0.35, 0.35, 0.25)


def fuel(m, hi, B):
    box(m, F, -0.90, 0.90, -3.60, 3.60, 0.45, 0.80)
    box(m, B, -1.20, 1.20, 1.90, 3.60, 0.80, 2.60)              # the cab
    box(m, D, -1.0, 1.0, 3.58, 3.66, 1.5, 2.4)                  # the windscreen
    for sx in (-1, 1):
        box(m, D, sx * 1.20, sx * 1.25, 2.3, 3.3, 1.5, 2.35)    # the side glass
    box(m, F, -1.10, 1.10, -3.60, -2.80, 0.80, 1.80)            # the equipment cabinet
    m.tube(B, (0.0, -2.7, 1.85), (0.0, 1.7, 1.85), 1.05, 1.05, 10 if hi else 8, caps=(B, B), smooth=False)    # the tank
    for sx in (-1, 1):
        for y in (2.8, -1.4, -2.6):
            wheel(m, hi, sx * 1.0, y, 0.50, 0.50, 0.35)


def catering(m, hi, B):
    box(m, F, -0.90, 0.90, -3.60, 3.60, 0.45, 0.85)
    box(m, B, -1.20, 1.20, 2.0, 3.6, 0.85, 2.50)                # the cab
    box(m, D, -1.0, 1.0, 3.58, 3.66, 1.55, 2.4)
    for sx in (-1, 1):
        box(m, D, sx * 1.20, sx * 1.25, 2.4, 3.3, 1.55, 2.4)
    box(m, F, -0.85, 0.85, -2.9, -2.7, 0.85, 2.40)              # the scissor lift's panels
    box(m, F, -0.85, 0.85, -0.2, 0.0, 0.85, 2.40)
    box(m, B, -1.25, 1.25, -3.5, 0.9, 2.40, 4.40)               # the raised box
    box(m, D, -0.9, 0.9, -3.55, -3.5, 2.6, 4.1)                 # (its rear door, at the aircraft)
    for sx in (-1, 1):
        for y in (2.8, -1.4, -2.6):
            wheel(m, hi, sx * 1.0, y, 0.45, 0.45, 0.30)


def stairs(m, hi, B):
    box(m, F, -0.95, 0.95, -2.2, 3.0, 0.40, 0.75)
    box(m, B, -1.0, 1.0, 0.6, 3.1, 0.75, 2.50)                  # the body under the platform
    box(m, F, -1.15, 1.15, 1.2, 3.2, 2.50, 2.65)                # the platform
    for sx in (-1, 1):
        box(m, B, min(sx * 1.11, sx * 1.15), max(sx * 1.11, sx * 1.15), 1.2, 3.2, 2.65, 3.40)    # (its rails)
    slab(m, F, (-0.55, 0.55, -2.8, 0.25, 0.40), (-0.55, 0.55, 1.2, 2.50, 2.65))                # the flight
    for sx in (-1, 1):
        x0, x1 = sorted((sx * 0.55, sx * 0.62))
        slab(m, B, (x0, x1, -2.8, 0.40, 1.25), (x0, x1, 1.2, 2.65, 3.40))
    for sx in (-1, 1):
        for y in (2.3, -1.4):
            wheel(m, hi, sx * 1.0, y, 0.40, 0.40, 0.30)


def container(m, hi, B):
    box(m, B, -1.22, 1.22, -3.03, 3.03, 0.0, 2.59)
    box(m, D, -1.0, 1.0, 3.03, 3.06, 0.2, 2.4)                  # (the door end's shadow)
    if hi:
        for sx in (-1, 1):                                      # ribs
            for y in (-2.4, -1.44, -0.48, 0.48, 1.44, 2.4):
                x0, x1 = sorted((sx * 1.22, sx * 1.25))
                box(m, B, x0, x1, y - 0.08, y + 0.08, 0.1, 2.5)


def cone(m, hi, B):
    box(m, O, -0.18, 0.18, -0.18, 0.18, 0.0, 0.04)
    m.tube(O, (0.0, 0.0, 0.04), (0.0, 0.0, 0.70), 0.14, 0.03, 8 if hi else 5, caps=(None, O), smooth=False)


BUILD = {"tug": tug, "cart": cart, "pushback": pushback, "belt": belt, "fuel": fuel, "catering": catering, "stairs": stairs,
         "container": container, "cone": cone}


def collision(kind):
    """The collision Mesh: a few boxes (None for the cone: it is not solid)."""
    m = ba.Mesh()
    b = lambda *a: box(m, F, *a)
    if kind == "tug":
        b(-0.65, 0.65, -1.45, 1.45, 0.2, 1.0)
        b(-0.65, 0.65, -0.25, 0.95, 1.0, 1.8)
    elif kind == "cart":
        b(-0.85, 0.85, -1.5, 1.1, 0.2, 1.1)
    elif kind == "pushback":
        b(-1.0, 1.0, -1.82, 1.82, 0.3, 0.95)
        b(-0.6, 0.6, -0.35, 0.95, 0.95, 1.77)
    elif kind == "belt":
        b(-0.95, 0.95, -3.0, 0.4, 0.3, 1.15)
        b(-0.9, 0.9, -3.0, -1.8, 1.15, 2.0)
        slab(m, F, (-0.60, 0.60, -1.5, 1.15, 1.54), (-0.60, 0.60, 3.0, 2.15, 2.54))
    elif kind == "fuel":
        b(-1.1, 1.1, -3.6, 1.85, 0.4, 2.9)
        b(-1.2, 1.2, 1.9, 3.66, 0.4, 2.6)
    elif kind == "catering":
        b(-1.2, 1.2, 1.9, 3.66, 0.4, 2.5)
        b(-0.9, 0.9, -3.6, 1.9, 0.4, 2.4)
        b(-1.25, 1.25, -3.55, 0.9, 2.4, 4.4)
    elif kind == "stairs":
        b(-1.0, 1.0, -2.2, 3.1, 0.35, 2.5)
        b(-1.15, 1.15, 1.2, 3.2, 2.5, 3.4)
        slab(m, F, (-0.62, 0.62, -2.8, 0.25, 0.40), (-0.62, 0.62, 1.2, 2.65, 3.40))
    elif kind == "container":
        b(-1.22, 1.22, -3.03, 3.03, 0.0, 2.59)
    else:
        return None
    return m


def parts(kind, lod, body):
    m = ba.Mesh()
    BUILD[kind](m, lod == 0, body)
    return m.finish()


def tri_count(pt):
    return sum(len(t) for _, _, t in pt.values())


def variants(kind):
    """[(file stem, body material)] of a kind's models."""
    if kind == "cone":
        return [("bne_gse_cone", O)]
    names = CONTAINER_COLOURS if kind == "container" else LIVERIES
    return [(f"bne_gse_{kind}_{c}", f"bne_gse_{c}") for c in names]


def _nodes(name, parts_by_lod, col):
    geoms, nodes, used = [], [], set()
    bind = lambda pt: ('<bind_material><technique_common>'
                       + "".join(f'<instance_material symbol="{k}-mat" target="#{k}-mat"/>' for k in sorted(pt))
                       + '</technique_common></bind_material>')
    for size, pt in zip(LODS, parts_by_lod):
        gid = f"{name}{size}-g"
        geoms.append(ba._geometry(gid, pt))
        nodes.append(f'<node id="{name}{size}" name="{name}{size}"><instance_geometry url="#{gid}">{bind(pt)}'
                     '</instance_geometry></node>\n')
        used |= set(pt)
    if col is not None:
        geoms.append(ba._geometry(f"{name}-col-g", col))
        nodes.append(f'<node id="Colmesh_{name}-1" name="Colmesh_{name}-1"><instance_geometry url="#{name}-col-g">{bind(col)}'
                     '</instance_geometry></node>\n')
        used |= set(col)
    return geoms, nodes, used


def collada(kind, name, body):
    """One .dae: base00 > start01 > a node per level of detail, and the collision mesh (Colmesh_<name>-1; an empty
    collision-1 node beside start01, as the other bne props)."""
    col = collision(kind)
    geoms, nodes, used = _nodes(name, [parts(kind, 0, body), parts(kind, 1, body)], col.finish() if col else None)
    effects = "".join(f'<effect id="{k}-fx"><profile_COMMON><technique sid="common"><lambert><diffuse><color>'
                      f'{" ".join(f"{c:g}" for c in MATERIALS[k][0])}</color></diffuse></lambert></technique></profile_COMMON></effect>\n'
                      for k in sorted(used))
    mats = "".join(f'<material id="{k}-mat" name="{k}"><instance_effect url="#{k}-fx"/></material>\n' for k in sorted(used))
    return f'''<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
<asset><contributor><authoring_tool>BeamNG.Earth tools/build_gse.py</authoring_tool></contributor>
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
    """The materials file: flat colours, double sided."""
    out = {}
    for name, (rgba, metal, rough) in MATERIALS.items():
        out[name] = {"name": name, "mapTo": name, "class": "Material", "version": 1.5,
                     "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, "beamng.earth/" + name)),
                     "Stages": [{"baseColorFactor": list(rgba), "metallicFactor": metal, "roughnessFactor": rough},
                                {}, {}, {}],
                     "doubleSided": True}
    return json.dumps(out, indent=2) + "\n"


def _write(path, text):
    import staging
    tmp = staging.tmp_for(path)
    with open(tmp, "w", newline="\n") as f:
        f.write(text)
    staging.replace(tmp, path)
    print(path, os.path.getsize(path), "bytes")


def main():
    for kind in KINDS:
        for name, body in variants(kind):
            _write(os.path.join(OUT, name + ".dae"), collada(kind, name, body))
        L, W, H = DIMS[kind]
        for lod in (0, 1):
            pt = parts(kind, lod, variants(kind)[0][1])
            v = np.concatenate([a for a, _, _ in pt.values()])
            lo, hi = v.min(axis=0), v.max(axis=0)
            print(f"{kind} LOD{lod}: {tri_count(pt)} triangles; length {hi[1] - lo[1]:.2f} (y {lo[1]:.2f}..{hi[1]:.2f}), "
                  f"width {hi[0] - lo[0]:.2f}, height {hi[2]:.2f}, lowest z {lo[2]:.3f}   (DIMS {L} x {W} x {H})")
    _write(os.path.join(OUT, "gse.materials.json"), materials())


if __name__ == "__main__":
    main()
