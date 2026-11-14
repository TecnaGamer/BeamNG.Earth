"""One small shape listing every material the server's own shapes use (buildings, road meshes, sidewalks, bridges,
tunnels, wires, runway markings), loaded once by the mod when it connects (hello.prewarm).

The first shape in a game session that uses a material sets it up (its shaders): a tunnel's first tile took 65 ms
to load, every later one ~2 ms; one shape with a triangle per material took 117 ms once, after which the tunnel's
first tile took 2.9 ms (this PC, 2026-10-20; a Steam Deck is ~2.5x slower). Loaded while the level starts, those
first uses no longer stall the frames of a drive.

    vfs = prewarm.write(out_dir, vfs_dir)     # "<vfs_dir>/prewarm.cdae"
"""
import os

import numpy as np

NAME = "prewarm.cdae"
NAME_X = "prewarm_x.cdae"     # the road meshes' other vertex format (road_materials); the mod loads it beside NAME
# materials the code names at run time, not in a constant the scan below finds
EXTRA = ("line_yellow", "line_white", "bne_wire", "bne_cat_wire_copper", "bne_cat_wire_bronze", "bne_cat_wire_steel", "bne_cat_wire_ins", "bne_cable_steel", "bne_cable_reflector")   # (the median cable barrier's: medians.py)


def _marks(v, out):
    """Every {"m": material} string in v (nested dicts, lists, tuples)."""
    if isinstance(v, dict):
        for k, x in v.items():
            if k == "m" and isinstance(x, str):
                out.add(x)
            _marks(x, out)
    elif isinstance(v, (list, tuple)):
        for x in v:
            _marks(x, out)


def _named(v, out):
    """Every "bne_..." material name in v (a constant, or nested in dicts' keys and values, lists, tuples): modules
    that name their materials as plain strings (railways.BALLAST, buildings' tint tables) rather than {"m": ...}.
    Their first tile instanced them mid-drive: a 104 ms rail import at 120 mph, 2026-11-04."""
    if isinstance(v, str):
        if v.startswith("bne_") and not v.endswith("_"):
            out.add(v)
    elif isinstance(v, dict):
        for k, x in v.items():
            _named(k, out)
            _named(x, out)
    elif isinstance(v, (list, tuple, set, frozenset)):
        for x in v:
            _named(x, out)


def materials():
    """The sorted material names the server's shapes may use."""
    import airportgrade
    import buildings
    import junctions
    import roadmesh
    import roads
    import sidewalks
    import exitsigns
    import mergesigns
    import milemarkers
    import speedsigns
    out = set(buildings.MATERIALS) | set(sidewalks.MATERIALS) | set(EXTRA)
    # the highway signs' (signstream.py)
    out |= set(exitsigns.GUIDE_MATERIALS) | set(milemarkers.MATERIALS) | set(mergesigns.MATERIALS)
    out |= set(speedsigns.MATERIALS)
    for mod in (roads, roadmesh, junctions, airportgrade):
        for name, v in vars(mod).items():
            if name.isupper():
                _marks(v, out)
    for name in ("DECK_SIDE", "TRUSS_STEEL", "TUNNEL_INNER", "TUNNEL_STONE", "TUNNEL_STONE_IN", "TUNNEL_SOIL", "WALL_SCREE",
                 "TUNNEL_LIGHT"):
        out.add(getattr(roadmesh, name))
    out |= set(roads.DIRT_MATERIALS)            # (the dirt and gravel strips: roads.DIRT_LOOK, one variation per road)
    import roadwear
    out |= set(roadwear.MATERIALS)              # (the road wear's: roadwear.LOOKS)
    import rails
    import railways
    # (not buildings / ships: their "bne_bld_fac_..." / "bne_precast" names are looks, a base material and a tint)
    for mod in (rails, railways, roadmesh):
        for name, v in vars(mod).items():
            if name.isupper():
                _marks(v, out)
                _named(v, out)
    return sorted(out)


def road_materials():
    """The sorted materials a road mesh with a dirt or gravel strip may use: such a mesh has the second uv and colour
    (roadmesh.write), another vertex format, whose material instances the plain prewarm shape does not set up."""
    import airportgrade
    import junctions
    import plazas
    import roadmesh
    import roads
    import roadwear
    out = set(EXTRA[:2]) | set(roads.DIRT_MATERIALS) | set(roadwear.MATERIALS)     # (a worn road's mesh: second uv)
    for mod in (roads, roadmesh, junctions, airportgrade, plazas):
        for name, v in vars(mod).items():
            if name.isupper():
                _marks(v, out)
    return sorted(out)


def _shape(path, mats, extra=False):
    """A tiny triangle per material, far below anything; extra: with the second uv and a colour (road_materials)."""
    import cdae
    n = len(mats)
    k = np.arange(n, dtype=np.float64)[:, None] * 0.1
    P = np.concatenate([np.hstack([k, np.zeros((n, 2))]), np.hstack([k + 0.05, np.zeros((n, 2))]),
                        np.hstack([k, np.full((n, 1), 0.05), np.zeros((n, 1))])])
    T = np.stack([np.arange(n), np.arange(n) + n, np.arange(n) + 2 * n], axis=1)
    kw = dict(uvs2=np.full((3 * n, 2), 0.5), colors=np.full((3 * n, 4), 255, np.uint8)) if extra else {}
    cdae.write(path, P, T, close=False, uvs=np.zeros((3 * n, 2)), material=mats, tri_materials=np.arange(n), object_name="prewarm",
               **kw)


def write(out_dir, vfs_dir):
    """The shape written to out_dir (and, with the dirt strips on, its NAME_X sibling); returns its vfs path."""
    import roads
    _shape(os.path.join(out_dir, NAME), materials())
    if roads.DIRT_STRIPS:
        _shape(os.path.join(out_dir, NAME_X), road_materials(), extra=True)
    else:
        try:
            os.remove(os.path.join(out_dir, NAME_X))
        except OSError:
            pass
    return f"{vfs_dir}/{NAME}"
