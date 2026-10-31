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
# materials the code names at run time, not in a constant the scan below finds
EXTRA = ("line_yellow", "line_white", "bne_wire", "bne_cable_steel", "bne_cable_reflector",
         "bne_dirt_variation_01", "bne_dirt_variation_02", "bne_dirt_variation_03", "bne_dirt_variation_04",
         "bne_dirt_gravel")


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
    for name in ("DECK_SIDE", "TRUSS_STEEL", "TUNNEL_INNER", "TUNNEL_STONE", "TUNNEL_STONE_IN", "TUNNEL_SOIL",
                 "TUNNEL_LIGHT"):
        out.add(getattr(roadmesh, name))
    return sorted(out)


def write(out_dir, vfs_dir):
    """The shape (a tiny triangle per material, far below anything) written to out_dir; returns its vfs path."""
    import cdae
    mats = materials()
    n = len(mats)
    k = np.arange(n, dtype=np.float64)[:, None] * 0.1
    P = np.concatenate([np.hstack([k, np.zeros((n, 2))]), np.hstack([k + 0.05, np.zeros((n, 2))]),
                        np.hstack([k, np.full((n, 1), 0.05), np.zeros((n, 1))])])
    T = np.stack([np.arange(n), np.arange(n) + n, np.arange(n) + 2 * n], axis=1)
    cdae.write(os.path.join(out_dir, NAME), P, T, uvs=np.zeros((3 * n, 2)), material=mats,
               tri_materials=np.arange(n), object_name="prewarm")
    return f"{vfs_dir}/{NAME}"
