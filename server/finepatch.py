"""Fine ground: where a terrain block's graded ground is sharp (a cutting's wall, a mountain road's steep side, an
abutment's slope), its 2 m lattice draws a staircase of slanted cells (teeth along the Stelvio's hairpins) and its
triangles reach across a road's edge into the slope beside it: the lanes tilt and bump (the drawn ground 0.31 m over
the graded profile in the Stelvio's outer lane, 21 cm bumps; the user, 2026-11-01: "the road feels a bit bumpy",
"would it help if we increased the terrain mesh resolution"). A terrain block has one grid size, so those cells are
holes in the block and a finer mesh of the same graded ground fills them, drawn with the block's own aerial photo
(its roads painted in) and colliding.

The block's own process makes both from the same heights (its .ter's holes and the mesh's pieces: written beside it,
listed in its info `fine`), and the mod makes the pieces in the frame it swaps the .ter in: a hole never shows
without its fill. The cells (earthfast.fine_cells): corners SHARP_DZ_M or more apart where the grading moved the
ground GRADED_DZ_M (natural cliffs stay the terrain's), grown by GROW cells. Their rim is the terrain's own edge
(earthfast.fine_tin, its rim covers earthfast.fine_rim_mesh). Pieces of PIECE_M square (each its own small collision mesh: Windows rebuilds one big one in
seconds, collision-engine-facts). The prototype of 2026-10-25 (branch fine-ground) put the mesh in the road tiles.
BNE_FINE=on turns it on (off by default while it is tried out).
"""
import os

import numpy as np

import heightmap

FINE_ON = os.environ.get("BNE_FINE", "off") == "on"
SHARP_DZ_M = 1.5            # m between a cell's highest and lowest corner: a sharp cell ...
GRADED_DZ_M = 0.5           # ... where the grading moved the ground this much at one of its corners
GROW = 2                    # cells round each sharp one drawn finely too (a slope's foot and top, the lanes beside; 1:
                            # the outline on a cutting's crest, its straight lattice edges drew 2 m stairs, 2026-11-01)
K = 2                       # quads per cell side: 1 m (4 at 0.5 m: 4x the triangles, no visible gain)
PIECE_M = 64.0              # m: a piece's square (at most 32 x 32 cells, 8192 triangles)
MAX_CELLS = 40000           # per block (a mountain's worth); more: the steepest kept
EDGE_CELLS = 3              # cells at the block's edge left to the terrain (its edge holes, the next block's seam)
RIM_LIFT_M = 0.01           # m the mesh lies over the terrain inside the rim cells (the terrain draws them, pale; 4 cm
                            # stood up at its edges, a shadow line on the road: the user, 2026-11-01)
TIN_K = 8                   # the fine cells' heights sampled every 1 / TIN_K cell (0.25 m) for fine_tin ...
TIN_TOL_M = 0.02            # ... put in where the triangles are this far off them (4 cm: a quarter fewer triangles, but a
                            # cutting's face drawn in steps again, 2026-11-01)
TIN_MAX_TRIS = 16384        # per piece (32 x 32 cells; the 1 m grid's were 8192)
# fine_rim_mesh's centre rule: the renderer draws a terrain cell split north-west to south-east (its collision is
# bilinear: rays at cells' quarter points, 2026-11-01): covers on the other split left white half squares, on the higher
# diagonal they bulged up steep cells as pale stairs
RIM_CENTRE = 2
DETAIL_M = 2.0              # the second UV set's metres per unit (farfield.DETAIL_M: satmat.lua's ground grain)


def apply(frame, gen, raw, keep, cx, cy, size, out_dir, stem, aid, idx, info):
    """The block's fine cells as holes in idx (its .ter's layer indices, None: none yet) and their pieces in
    info["fine"]; returns idx."""
    if not FINE_ON:
        return idx
    import time
    t0 = time.perf_counter()
    holes, fine = block_fine(frame, gen, raw, keep, cx, cy, size, out_dir, stem, aid, layers=idx)
    if fine is None:
        return idx
    idx = np.zeros(holes.shape, np.uint8) if idx is None else idx
    idx[holes] = heightmap.TER_HOLE
    fine["holes"] = int(holes.sum())
    fine["seconds"] = round(time.perf_counter() - t0, 2)
    info["fine"] = fine
    return idx


def _normals(P, T, u, x0, y0, sq, cells):
    """Vertex normals of a piece (points P (n, 3), triangles T): its faces' (cdae: as the engine derives them), but a
    lattice sample on the fine cells' outline takes the terrain's own there (its heightmap's central difference, the
    engine's terrain shader) so the light goes on across the seam."""
    import cdae
    N = cdae.face_normal_sum(P, T)
    gx, gy = (P[:, 0] - x0) / sq, (P[:, 1] - y0) / sq
    i, j = np.rint(gx).astype(np.int64), np.rint(gy).astype(np.int64)
    ny, nx = u.shape
    on = (np.abs(gx - i) < 1e-6) & (np.abs(gy - j) < 1e-6) & (i >= 1) & (j >= 1) & (i < nx - 1) & (j < ny - 1)
    k = np.flatnonzero(on)
    if len(k):
        ci, cj = i[k], j[k]
        cy, cx = cells.shape
        def fine(a, b):
            a, b = np.clip(a, 0, cy - 1), np.clip(b, 0, cx - 1)
            return cells[a, b] != 0
        rim = ~(fine(cj, ci) & fine(cj - 1, ci) & fine(cj, ci - 1) & fine(cj - 1, ci - 1))
        k, ci, cj = k[rim], ci[rim], cj[rim]
        n = np.c_[(u[cj, ci - 1] - u[cj, ci + 1]) / (2 * sq), (u[cj - 1, ci] - u[cj + 1, ci]) / (2 * sq), np.ones(len(k))]
        N[k] = n / np.linalg.norm(n, axis=1)[:, None]
    return N


def _cell_layers(layers, J, I, n, fallback):
    """Each cell's surface layer (cells J, I of a block's per-sample layers [j, i]): the one most of its four corner
    samples have (the south-west one's on a tie). The terrain blends its samples' layers across the cell; a cell drawn
    with its south-west sample's alone made one lighter sample a sharp pale square (the user, 2026-11-01)."""
    ny, nx = layers.shape
    c = np.stack([layers[J, I], layers[J, np.minimum(I + 1, nx - 1)], layers[np.minimum(J + 1, ny - 1), I],
                  layers[np.minimum(J + 1, ny - 1), np.minimum(I + 1, nx - 1)]])
    c = np.where((c < 0) | (c >= n), fallback, c)                   # (a tunnel's hole: 255)
    votes = (c[:, None, :] == c[None, :, :]).sum(1)                 # (each corner: how many share its layer)
    best = np.argmax(votes, 0)                                      # (the first of the most: the south-west first)
    return c[best, np.arange(c.shape[1])]


def material_name(bank_, layer):
    """The Material of a bank's (bank) pieces of surface layer `layer` (surface.MATERIALS index): the mod makes it with the
    block's photo and that layer's detail texture (terrain.lua)."""
    return f"bngEarthFine{int(bank_)}_{int(layer)}"


def bank(cx, cy, size):
    """A block's material bank, 0..3 by its centre's parity on the blocks' centre grid (size / 4 samples of the lattice):
    a block and its neighbours (the next block, whose pieces are made while this one shows) never share one."""
    g = size * heightmap.SQUARE / 8.0
    return int(round(cx / g)) % 2 + 2 * (int(round(cy / g)) % 2)


def block_fine(frame, gen, raw, keep, cx, cy, size, out_dir, stem, aid, layers=None):
    """The fine cells of a terrain block: (holes (ny, nx) bool for its .ter, info {"material", "parts": [{"shape", "pos"}],
    "cells"} or None). gen(lat, lon): the block's heights function (as heightmap.generate drew keep["u"] with), raw: the
    natural ground; keep: heightmap.generate's (u [j, i]); layers: the block's surface layer per sample (surface.MATERIALS
    indices, [j, i]; None: one). The pieces are written to out_dir as <stem>_<px>_<py>.cdae (their names in "parts",
    relative: earthd.terrain_msg makes them the game's paths), each cell drawn with its layer's material (the mod gives
    it the block's photo and the layer's detail texture: the terrain's look, terrain.lua)."""
    import fast
    fast.require(132, "fine_cells")      # (132: its islands filled)
    import cdae
    u = np.ascontiguousarray(keep["u"], np.float64)
    # (the mesh's outline and rim on the terrain's own stored heights, its height quantum's steps: up to 1.5 cm off
    # them, the edges stood over the terrain or under it)
    ud = np.ascontiguousarray(keep.get("drawn", u), np.float64)
    xs, ys = heightmap.sample_coords(cx, cy, size)
    r = np.ascontiguousarray(heightmap.surface_u(frame, raw, xs, ys), np.float64)
    cells = np.asarray(fast._ef.fine_cells(u, r, SHARP_DZ_M, GRADED_DZ_M, GROW))
    e = EDGE_CELLS
    cells[:e, :] = 0
    cells[-e:, :] = 0
    cells[:, :e] = 0
    cells[:, -e:] = 0
    J, I = np.nonzero(cells)
    if not len(J):
        return np.zeros(u.shape, bool), None
    if len(J) > MAX_CELLS:
        c = np.stack([u[J, I], u[J, I + 1], u[J + 1, I], u[J + 1, I + 1]])
        keep_ = np.sort(np.argsort(-(c.max(0) - c.min(0)), kind="stable")[:MAX_CELLS])
        cells = np.zeros_like(cells)
        cells[J[keep_], I[keep_]] = 1
        J, I = np.nonzero(cells)
    cells8 = np.ascontiguousarray(cells, np.uint8)
    holes = np.asarray(fast._ef.fine_holes(cells8)).astype(bool)
    # (the rim the terrain still draws, pale: the mesh over it too, RIM_LIFT_M up inside)
    rim = np.asarray(fast._ef.fine_rim(cells8))
    rim[:e - 1, :] = 0
    rim[:, :e - 1] = 0
    RJ, RI = np.nonzero(rim)
    nfine = len(J)
    x0, y0, sq = float(xs[0]), float(ys[0]), heightmap.SQUARE
    per = int(round(PIECE_M / sq))

    def heights(ji, k):
        pts = np.asarray(fast._ef.fine_points(x0, y0, sq, k, ji))
        la, lo, _ = frame.enu_to_geodetic(pts[:, 0], pts[:, 1], np.zeros(len(pts)))
        la, lo = np.asarray(la, np.float64), np.asarray(lo, np.float64)
        z = np.asarray(frame.geodetic_to_enu(la, lo, np.asarray(gen(la, lo), np.float64))[2], np.float64)
        return np.ascontiguousarray(z.reshape(len(ji), (k + 1) ** 2))

    # the fine cells: an adaptive triangulation of their ground (earthfast.fine_tin), not a grid
    ji = np.ascontiguousarray(np.c_[J, I], np.int64)
    fast.require(127, "fine_tin")
    import time
    t0 = time.perf_counter()
    zs = heights(ji, TIN_K)
    t1 = time.perf_counter()
    P, T, tcell, tpiece = fast._ef.fine_tin(x0, y0, sq, ji, zs, ud, cells8, per, TIN_TOL_M, TIN_MAX_TRIS)
    t2 = time.perf_counter()
    P, T, tcell, tpiece = np.asarray(P), np.asarray(T), np.asarray(tcell), np.asarray(tpiece)
    # (RIM_LIFT_M over the terrain as the rim round it: their seam level, neither on the pale squares the rim covers)
    P = P + [0.0, 0.0, RIM_LIFT_M]
    # (the rim cells: covers drawn only, RIM_LIFT_M over the terrain's own pale squares, however it splits them)
    if len(RJ):
        rji = np.ascontiguousarray(np.c_[RJ, RI], np.int64)
        fast.require(131, "fine_rim_mesh")
        RP, RT = fast._ef.fine_rim_mesh(x0, y0, sq, rji, ud, RIM_LIFT_M, RIM_CENTRE)
        RP, RT = np.asarray(RP), np.asarray(RT)
        rcell = nfine + np.repeat(np.arange(len(RJ)), 4)          # (fine_rim_mesh: 4 triangles a cell)
        rpiece = (RJ // per)[rcell - nfine] * 100000 + (RI // per)[rcell - nfine] + 10 ** 10
        T = np.r_[T, RT + len(P)]
        P = np.r_[P, RP]
        tcell = np.r_[tcell, rcell]
        tpiece = np.r_[tpiece, rpiece]
    J, I = np.r_[J, RJ], np.r_[I, RI]
    extent = size * sq                                  # (the block's photo covers it from its south-west corner)
    import surface as surfacemod
    if layers is not None:
        lay = _cell_layers(np.asarray(layers, np.int64), J, I, len(surfacemod.MATERIALS), surfacemod.DIRT)
    else:
        lay = np.full(len(J), surfacemod.DIRT, np.int64)
    used = sorted(set(int(v) for v in np.unique(lay)))
    b = bank(cx, cy, size)
    # (the rim's own materials, casting no shadow: material_name + "r")
    mats = [material_name(b, k) for k in used] + [material_name(b, k) + "r" for k in used]
    tri_layer = np.searchsorted(used, lay[tcell]) + np.where(tpiece >= 10 ** 10, len(used), 0)
    # the pieces: PIECE_M squares from the block's corner (fine_tin's), the rim cells in pieces of their own, drawn
    # only (the terrain under them collides; the mesh 4 cm over it made ridges across the lanes)
    parts = []
    t3 = time.perf_counter()
    for key in np.unique(tpiece):
        ti = np.flatnonzero(tpiece == key)
        vi, inv = np.unique(T[ti].ravel(), return_inverse=True)
        rimp = key >= 10 ** 10
        kk = int(key % 10 ** 10)
        pj, pi = kk // 100000, kk % 100000
        ox, oy = x0 + pi * PIECE_M, y0 + pj * PIECE_M
        pos = P[vi] - [ox, oy, 0.0]
        # the terrain's own texture coordinates: its samples' (block-local, half a sample on: the engine's terrain
        # shader), the photo's as its base texture (v down), the detail's as its layers' detail textures (v up, as
        # this format stores it flipped): the roads in the photo and the ground's grain go on across the seam
        sx, sy = (P[vi, 0] - x0 + sq / 2) / sq, (P[vi, 1] - y0 + sq / 2) / sq
        uv = np.c_[sx * sq / extent, sy * sq / extent]
        uv2 = np.c_[sx * sq / DETAIL_M, 1.0 - sy * sq / DETAIL_M]
        tris = inv.reshape(-1, 3)
        name = f"{stem}_{pi}_{pj}{'r' if rimp else ''}.cdae"
        # (tangents along the detail's coordinates: its normal map's relief lit as the terrain's; along the photo's,
        # whose v runs the other way, it was lit from the wrong side)
        cdae.write(os.path.join(out_dir, name), pos, tris, uvs=uv, uvs2=uv2, tangent_uvs=uv2, material=mats,
                   normals=_normals(P[vi], tris, ud, x0, y0, sq, cells8), tri_materials=tri_layer[ti], object_name="fine")
        parts.append({"shape": name, "pos": [round(ox, 3), round(oy, 3), 0.0]} | ({"collide": False} if rimp else {}))
    times = {"heights": round(t1 - t0, 2), "tin": round(t2 - t1, 2), "write": round(time.perf_counter() - t3, 2)}
    return holes, {"times": times, "tris": int(len(T)), "materials": [[material_name(b, k), surfacemod.MATERIALS[k][0]] for k in used]
                   + [[material_name(b, k) + "r", surfacemod.MATERIALS[k][0], True] for k in used], "bank": b,
                   "parts": parts, "cells": int(nfine), "rim": int(len(RJ))}
