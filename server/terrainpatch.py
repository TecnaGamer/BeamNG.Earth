"""Terrain collision patches for collision islands off the terrain block (BeamMP support, the user, 2026-10-22: "As cars
spread apart they can have their own island of collision").

The engine collides with one TerrainBlock only (the first; the block follows the active car), so a vehicle far from
it has no ground. The mod (terrainpatch.lua) asks for a patch under each other island (collisionfoci.lua): a
collision-only shape (cdae collision_only: never drawn) of the same ground the block is made of (the graded DEM,
lifted under roads like the block), on a STEP_M grid, in PIECE_M pieces each placed at its own centre.
"""
import os

import numpy as np

STEP_M = 8.0                # grid spacing (the ground a far car rests on; 4 m until 2026-10-24: 4x the cells for it)
MAX_SIDE_M = 1280.0         # a patch's side at most
SNAP_M = 128.0              # boxes are snapped out to this grid (keys stay the same while a car moves a little)
# A patch is written as PIECE_M squares, one collision mesh each: the Windows game builds a collision mesh in time
# growing with about the square of its triangles (a Windows 10 / RTX 3060 test PC, 2026-10-24: one 51k-triangle patch
# 3.81 s, the same ground as 16 meshes 0.09 s, 12.8k triangles in one mesh 0.28 s; players froze 6-26 s each time the
# game rebuilt its collision with patches in; Linux builds 1.3M triangles in 0.09 s either way).
PIECE_M = 128.0             # 16 x 16 quads, 512 triangles a piece (a Steam Deck in a 3-player session made 977 cells
                            # of 64 m as another player drove 4 km; these are a quarter as many)
# The pieces' material: one the level loads (main.materials.json), with no ground type, as the missing default was
# (cdae's default "bngEarthDae" has no Material: an engine error line for every piece loaded, 195 in one PC session)
MATERIAL = "bngEarthFar_v3"


def snap_box(x0, y0, x1, y1):
    """The box snapped outward to SNAP_M and clipped to MAX_SIDE_M round its centre."""
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    hw, hh = min((x1 - x0) / 2, MAX_SIDE_M / 2), min((y1 - y0) / 2, MAX_SIDE_M / 2)
    return (float(np.floor((cx - hw) / SNAP_M) * SNAP_M), float(np.floor((cy - hh) / SNAP_M) * SNAP_M),
            float(np.ceil((cx + hw) / SNAP_M) * SNAP_M), float(np.ceil((cy + hh) / SNAP_M) * SNAP_M))


def grid(box, step=STEP_M):
    """(xs, ys) of the patch's grid over box (x0, y0, x1, y1), both ends included."""
    x0, y0, x1, y1 = box
    nx, ny = max(1, int(round((x1 - x0) / step))), max(1, int(round((y1 - y0) / step)))
    return np.linspace(x0, x1, nx + 1), np.linspace(y0, y1, ny + 1)


def mesh(xs, ys, z, origin):
    """(positions (n, 3) float32 relative to origin (x, y, 0), triangles (m, 3) int32) of the height grid z
    (len(ys), len(xs)), two triangles a quad, counter-clockwise seen from above (the Rust earthfast.grid_mesh)."""
    import fast
    fast.require(46, "grid_mesh")
    pos, tris = fast._ef.grid_mesh(np.ascontiguousarray(xs, np.float64), np.ascontiguousarray(ys, np.float64),
                                   np.ascontiguousarray(z, np.float64), float(origin[0]), float(origin[1]))
    return np.asarray(pos), np.asarray(tris)


def pieces(box, side=PIECE_M):
    """The PIECE_M squares (x0, y0, x1, y1) covering box (snapped to SNAP_M, so it divides evenly), row by row."""
    x0, y0, x1, y1 = box
    nx, ny = max(1, int(round((x1 - x0) / side))), max(1, int(round((y1 - y0) / side)))
    xs, ys = np.linspace(x0, x1, nx + 1), np.linspace(y0, y1, ny + 1)
    return [(float(xs[i]), float(ys[j]), float(xs[i + 1]), float(ys[j + 1])) for j in range(ny) for i in range(nx)]


def cell_name(px0, py0, side=PIECE_M):
    """A piece's cell on the PIECE_M grid of the anchor frame, as a file-name part ("3_m2": x index 3, y index -2)."""
    return f"{int(round(px0 / side))}_{int(round(py0 / side))}".replace("-", "m")


def build(frame, elev, box, prefix, step=STEP_M):
    """Write the patch for box (anchor-frame metres) as PIECE_M pieces, prefix + _<cell>.cdae each (collision only),
    named by their cell: a piece another patch of the same anchor already wrote is used as it is (the mod shares one
    object per cell, and fewer distinct shape paths load). elev(lat, lon) -> the ground height in the anchor frame (as
    the block's). Returns ([(path, (cx, cy))], triangles): each piece's file and centre, their triangles in all."""
    import cdae
    todo = []
    for px0, py0, px1, py1 in pieces(box):
        path = f"{prefix}_{cell_name(px0, py0)}.cdae"
        todo.append((path, (px0, py0, px1, py1), os.path.exists(path)))
    out, total = [], 0
    if any(not done for _, _, done in todo):
        xs, ys = grid(box, step)
        gx, gy = np.meshgrid(xs, ys)
        la, lo, _ = frame.enu_to_geodetic(gx.ravel(), gy.ravel(), np.zeros(gx.size))
        z = np.asarray(elev(la, lo), np.float64).reshape(gx.shape)
    per_piece = 2 * int(round(PIECE_M / step)) ** 2
    for path, (px0, py0, px1, py1), done in todo:
        cx, cy = (px0 + px1) / 2, (py0 + py1) / 2
        if not done:
            i0, i1 = int(round((px0 - box[0]) / step)), int(round((px1 - box[0]) / step))
            j0, j1 = int(round((py0 - box[1]) / step)), int(round((py1 - box[1]) / step))
            pos, tris = mesh(xs[i0:i1 + 1], ys[j0:j1 + 1], z[j0:j1 + 1, i0:i1 + 1], (cx, cy))
            # (atomic through staging: whole or not at all, the game may load it at once; read once on Windows, warm)
            cdae.write(path, pos, tris, collision_only=True, material=MATERIAL)
        out.append((path, (cx, cy)))
        total += per_piece
    return out, total
