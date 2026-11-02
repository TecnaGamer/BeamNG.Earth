"""Integration drive (protocol 4, docs/V1_3_SPEC.md over docs/V1_2_DESIGN.md): the REAL server
process (earthd.py) driven by a client that replays the mod frame by frame with the mod's exact
rules (modsim.py: ports of core.lua updateWorld, follow.lua, rebase.lua, governor.lua,
crossover.lua, farfield.lua and satmat.lua, every threshold read from the Lua files;
test_modsim.py checks the ports against the Lua under luajit).

Three drives over the same route:
  TerrainDriveTest  --near-visual terrain --imagery off: the v1.2 path (the terrain block is
                    drawn, far tiles use the static palette material, L0 tiles carry `sink`);
  MeshDriveTest     --near-visual mesh with imagery (the v1.3 defaults): the block is physics only,
                    L-2 / L-1 tiles draw the ground near the player, every tile has its own texture
                    and material. Imagery comes from a deterministic offline source
                    (fake_imagery_earthd.py), so the drive needs no imagery network access;
  MeshDrive64Test   the same with --cells 64 (docs/V1_4_SPEC.md §3.1, M1): FORMAT 4, 8704-tri tiles
                    down to L-1 (the exact level), far_tile.tex and the textured kinds
                    cdaeTex128/256/512, with the cost model per kind and object count of §3.4.

Route: teleport to 36.5785, -118.2923 -> stand until the first far_done -> drive at 30 m/s,
east for 3 km, a 1 km bend to 50 deg (north-east, so blocks move on both axes and the uncovered
strip is L-shaped), on to the rebase distance (default 10 km: the whole 10 km is one anchor),
600 m more in the new frame -> stop and wait for the final far_done.

Engine work is replaced by a cost model (ms): a far-tile import (.cdae, BNGE_DRIVE_IMPORT_MS,
default 2.0; probe P5: 1.5-3.4), a textured import 3.0 (BNGE_DRIVE_TEX_IMPORT_MS) and its
Material 0.3 (the middle of the lead's p_sat.lua ranges 2.5-3.5 / 0.13-0.46), a Material delete
0.3 (unmeasured: as an object delete), a reveal 0.05, a delete 0.3, one setPosition 0.006 per moving tile; the terrain-follow
commit 44 (P7: importMaps ~42 + reloadCollision 1.6), the rebase 65, the teleport 67, on a base
frame of 16.7 ms (60 fps; BNGE_DRIVE_BASE_MS). The governor sees these as its measured costs and
frame times, and each frame's simulated length (dtReal) is the previous frame's modelled wall
time, as in the game.
64-cell drive (§3.4 Tests): c(kind, n) = prior(kind) (1 + alpha (n - 300)), n = live + old + deleting
tile objects, alpha = BNGE_DRIVE_ALPHA (0.0016, R3); the 256 px textured import BNGE_DRIVE_TEX_IMPORT_MS
(default 6 for 64 cells, a planning value until P8), the texture part scaling with its area
(BNGE_DRIVE_DDS_MS = 1.5 ms of it at 256 px: 128 px 4.9 ms, 512 px 10.5 ms).

Asserted (design §9 acceptance and V1_3_SPEC acceptance, the parts checkable offline):
  (a) overlapping terrain samples are bit-identical across every terrain-follow commit: the two
      blocks' PNGs are decoded and their heights compared in world coordinates (float64, and as
      the engine computes them in float32);
  (b) terrain mode: far-tile keys are stable across commits: no far_tile / far_remove / far_swap
      key in the area the commit stops forcing (old block - new block) within 2 s after it, and
      the server's selection rule gives the same keys with and without the old block; the counts
      of far messages in the 2 s around each commit are reported (mesh mode: the selection
      ignores blocks, so only the counts are reported);
  (c) no hole at any step: the block's collidable ground [cx-1024, cx+1022]^2 (terrain mode only:
      in mesh mode the block is invisible), the shown far tiles that are not hidden (cover state
      as the mod decides it: edge ring sunk, interior hidden; nothing covered in mesh mode) and
      the previous anchor's kept tiles cover the radius square around the player, including the
      2 m N/E strip [cx+1022, cx+1024] (sampled every 8 m); checked at every pose and on every
      frame for 0.5 s after each commit; terrain mode: a rebase may open a transient gap that
      closes in 2 s; mesh mode: additionally every frame within NEAR_HALF of the player, and no
      transient gap at all;
  (d) every far tile is a .cdae that exists when it arrives, when the mod imports it and as long
      as the mod has a TSStatic for it, and a sample parses with the server's own reader
      (cdae.read) into one 2304-triangle (64 cells: 8704) mesh with the right material (the static one, or in
      mesh mode the tile's own bngEarthSat_<anchorId>_<key> with UV1); mesh mode: every tile has
      a texture (DDS DXT1, full mip chain) that exists while a Material refers to it;
  (e) time-warp proxy: the governor's per-frame budget replayed against the server's stream:
      max imports / estimated ms / spent ms per frame, no frame over its budget (b + the largest
      unit, plus the cost model's excess over the estimate), streaming-frame p99, and no unit
      (import, material, reveal, delete, material delete) within QUIET_BEFORE / QUIET_AFTER
      frames of a terrain commit or rebase;
  (f) mesh mode: the visible ground near the player is the physics surface on the far field's 2 m lattice
      (heightmap.FAR_SQUARE: every second sample of the 1 m block since 2026-11-01): the tile under the
      player is the exact level (L-2; L-1 with 64 cells) at every pose, and the vertices of every
      shown exact tile over the committed block equal every second heightmap sample (float64
      exactly, float32 as the engine adds them), with the TerrainBlock's diagonal pattern on that lattice; between
      the vertices the mesh height at random points under the player differs from the bilinear surface of
      those samples by p99 <= 5 cm and at most |twist|/4 of its 2 m square (V1_4_SPEC Targets, E7; >= 200 points;
      the deviation from the 1 m collision itself is reported); tiles within the exact radius are exact at
      the end;
  (g) materials do not leak (every Material is referenced by a tile object, a deleted
      material's tiles are gone, none left over at the end), and the server deletes no burst of
      files the mod had loaded (shapes and textures: at most its queue rate in any second, 5/s
      in terrain mode, at most DELETE_MAX_RATE evenly spaced in mesh mode);
plus the v3/v4 protocol rules: no protocol error or server warning, terrain_ready before the
commit threshold, one prepare_anchor per rebase, no tile of a superseded anchor, no far_remove
of a tile the mod never had, every swap group replaces its own footprint, far_done counts match,
terrain mode: the committed block sits on its L0 tiles at the end (16 for the 1 km block).

Needs network on a cold DEM cache (<tmp>/beamng-earth-test-cache, shared with test_e2e.py).
Environment:
  BNGE_DRIVE_SPEEDUP    simulated seconds per wall second (default 4)
  BNGE_DRIVE_REBASE_M   rebase distance (default: rebase.lua DEFAULT_DISTANCE, 10 km)
  BNGE_DRIVE_IMPORT_MS  modelled cost of one .cdae import (default 2.0)
  BNGE_DRIVE_TEX_IMPORT_MS  ... of one textured .cdae import (default 3.0; 64 cells: 6, the 256 px kind)
  BNGE_DRIVE_DDS_MS     64 cells: the texture part of the 256 px import (default 1.5; scales with area)
  BNGE_DRIVE_ALPHA      64 cells: the object-count term (default 0.0016 per object over 300)
  BNGE_DRIVE_KEEP_LOG   copy the server log to this file at the end (<name>.<mode> per drive)
  BNGE_DRIVE_MODES      comma list of drives to run (default terrain,mesh,mesh64)
  BNGE_SKIP_DRIVE=1     skip both drives
"""
import asyncio
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

SERVER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import cdae  # noqa: E402
import earthd  # noqa: E402
import farfield as ff  # noqa: E402
import geodesy  # noqa: E402
import heightmap  # noqa: E402
import imagery  # noqa: E402
import modsim  # noqa: E402
from modsim import CORE, GOV, LATTICE, REBASE, cheb  # noqa: E402
from netclient import Client, stop_server  # noqa: E402

LAT, LON = 36.5785, -118.2923
SPEED = 30.0
CACHE = os.path.join(tempfile.gettempdir(), "beamng-earth-test-cache")
SPEEDUP = float(os.environ.get("BNGE_DRIVE_SPEEDUP", "4"))
REBASE_M = float(os.environ.get("BNGE_DRIVE_REBASE_M", REBASE["DEFAULT_DISTANCE"]))
AFTER_REBASE_M = 600.0
# The mod's frame time is the modelled wall time of the previous frame (dtReal): a base frame of
# 16.7 ms (60 fps) plus the streaming work, motion and heavy operations it ran.
BASE_MS = float(os.environ.get("BNGE_DRIVE_BASE_MS", 1000 / 60))
# cost model (ms), see the module docstring
COSTS = {"cdae": float(os.environ.get("BNGE_DRIVE_IMPORT_MS", "2.0")), "dae": 8.3, "reveal": 0.05,
         "delete": 0.3, "motion": 0.4 / 64,
         # v1.3 imagery (p_sat.lua: material 0.13-0.46 ms, textured spawn 2.5-3.5 ms: the middle of
         # each range; a material delete is unmeasured: as an object delete, above its prior)
         "cdaeTex": float(os.environ.get("BNGE_DRIVE_TEX_IMPORT_MS", "3.0")), "material": 0.3,
         "matDelete": 0.3}
# 64-cell drive (§3.4 Tests): per-kind costs and the object-count term
ALPHA = float(os.environ.get("BNGE_DRIVE_ALPHA", "0.0016"))
OBJECTS_N0 = 300
TEX256_64_MS = float(os.environ.get("BNGE_DRIVE_TEX_IMPORT_MS", "6.0"))
DDS256_MS = float(os.environ.get("BNGE_DRIVE_DDS_MS", "1.5"))
COSTS_64 = {**COSTS, **{f"cdaeTex{px}": TEX256_64_MS + DDS256_MS * ((px / 256) ** 2 - 1) for px in (128, 256, 512)}}
# a 64-cell .cdae is 348 KB textured (§3.1); a 32-cell one 94 KB (100 with margin), 90 untextured (§6.4)
CDAE_MAX_KB = {(32, False): 90, (32, True): 100, (64, True): 360}
# |mesh - bilinear collision| under the player (V1_4_SPEC Targets, E7): every point within |twist|/4 of
# its 2 m square, and p99 <= 5 cm. The 5 cm (and the 31 cm max) were measured on the v1.3 p_diag
# block (145 m relief); where the route's own terrain has more twist than that (p99 of |twist|/4
# over the same points above 5 cm) the bound is the terrain's, not the tiles': the deviation is
# exactly the triangulation's |twist|/4 at a square centre, the same for any layout (the
# centre-vertex option of §3.1 would cut it to |twist|/16).
BILINEAR_P99_M, BILINEAR_MIN_POINTS, BILINEAR_PER_POSE = 0.05, 200, 4
# per terrain block size (M4, docs/V1_4_SPEC.md §3.5 [I]: a 512² commit ~18 ms + reloadCollision 0.4; the
# rebase loses the 1024² import's ~26 ms); BNGE_DRIVE_COMMIT_MS / BNGE_DRIVE_REBASE_MS override both
# (by samples: 1024² is the 1 km block at 1 m since 2026-11-01; 2048², the 2 km one at 1 m, is unmeasured: 4x 1024²)
COMMIT_MS = {2048: 4 * 44.0, 1024: 44.0, 512: 18.4}
REBASE_MS = {2048: 4 * 65.0, 1024: 65.0, 512: 44.0}
for _k, _env in (("COMMIT", COMMIT_MS), ("REBASE", REBASE_MS)):
    if os.environ.get(f"BNGE_DRIVE_{_k}_MS"):
        _env.update(dict.fromkeys(_env, float(os.environ[f"BNGE_DRIVE_{_k}_MS"])))
TELEPORT_MS = 67.0
SLOW_MS = 4.0                   # a frame this much over base counts as slow (§5.7 "p99 <= base + 4 ms")
# 64 cells: one import unit is 5-10 ms by itself (§3.1 Risks), so the streaming-frame p99 rule is the
# governor's own bound: the target budget plus one unit at its modelled cost (the in-game rule, p99 frame
# <= 25 ms, docs/V1_4_SPEC.md §3.1, depends on the real unit costs: gate P8). Back-to-back import
# frames (each over base + 4 ms) are allowed up to 1 % of the steady streaming frames: the dt filter's
# median of 3 rejects single ones, and M3b's adaptive budget replaces this rule.
P99_FRAME_MS_64 = 25.0
SLOW_PAIRS_64 = 0.01
TRANSIENT_MAX_S = 2.0           # a vacated area must be covered again within this (simulated) time
WINDOW_S = 2.0                  # (b): far messages within this after / around a commit
CHECK_AFTER_COMMIT = 30         # (c): frames after a commit checked one by one
FINE, COARSE = 32.0, 512.0      # coverage sample spacing near / far (tiles beyond FINE_HALF are >= 1 km)
FINE_HALF = 6000.0              # fine samples within this Chebyshev distance of the player
EDGE = 1000.0                   # coverage is checked inside RADIUS - EDGE
STRIP_STEP = 8.0                # N/E strip samples
CDAE_SAMPLE = 4                 # parse every n-th far tile with cdae.read
# mesh mode: per-frame coverage window around the player. Current-frame tiles are 64 m aligned
# squares, so a hole is at least one 64 m cell: sampling every 32 m finds every one.
NEAR_HALF, NEAR_STEP = 512.0, 32.0
# (g) loaded files the server may delete in any 1 s: its queue rate (+ sampling slack)
DELETES_PER_S = {"terrain": 1 / earthd.DELETE_INTERVAL + 2, "mesh": earthd.DELETE_MAX_RATE + 2}
OBSERVE_DELETES_S = 5.0         # (g): after the stop, watch the old anchor's files being deleted this long
OBSERVE_MAX_S = 75.0            # ... waiting at most this long (wall) after the stop for them to start
# (e) a unit may overshoot the budget by its cost's excess over the governor's estimate, which
# starts at the prior and only moves towards the (constant) modelled cost
EXCESS_MS = max(0.0, *(COSTS[k] - GOV["PRIOR"][k] for k in COSTS if k in GOV["PRIOR"]))
MATERIAL = modsim.lua_string("farfield.lua", "MATERIAL")
MOD_VERSION = re.search(r"MOD_VERSION = '([^']+)'", (modsim.MOD_DIR / "core.lua").read_text()).group(1)
MODES = os.environ.get("BNGE_DRIVE_MODES", "terrain,mesh,mesh64,mesh64b2048,follow512").split(",")
SQ = LATTICE["SQUARE"]


def heading(s):
    """Route heading (rad, from east) after s metres driven."""
    a = 0.0 if s < 3000 else min(1.0, (s - 3000) / 1000) * 50.0
    return math.radians(a)


def to_frame(src, dst, x, y, z):
    e, n, u = dst.ecef_to_enu(*src.enu_to_ecef(x, y, z))
    return float(e), float(n), float(u)


def tile_bbox(key):
    return ff.tile_of(key).bbox


def cells(keys):
    """The footprint of tiles as a set of 64 m cells (the finest tile of either layout or smaller)."""
    out = set()
    for k in keys:
        t = ff.tile_of(k)
        n = 1 << (t.level + 2)
        out |= {(t.i * n + a, t.j * n + b) for a in range(n) for b in range(n)}
    return out


def radius_edge_group(adds, removes, focus, slack=2000.0):
    """A swap group whose two sides differ only beyond the far radius: a node refined or merged at
    the radius edge keeps only its part inside the radius square, so one side's footprint is the
    other's minus area at least RADIUS - slack (Chebyshev) from the player (the server selected
    around its own, slightly older, focus). Seen with 64-cell tiles (an L8 parent, one L7 child)."""
    for small, big in ((adds, removes), (removes, adds)):
        sb, bb = [tile_bbox(k) for k in small], [tile_bbox(k) for k in big]
        if not all(any(b[0] <= a[0] and a[2] <= b[2] and b[1] <= a[1] and a[3] <= b[3] for b in bb) for a in sb):
            continue
        rest = bb
        for a in sb:
            rest = [r for box in rest for r in subtract(box, a)]
        fx, fy = focus
        if all(max(max(r[0] - fx, 0.0, fx - r[2]), max(r[1] - fy, 0.0, fy - r[3])) >= ff.RADIUS - slack
               for r in rest):
            return True
    return False


def subtract(a, b):
    """a minus b for axis-aligned boxes: list of boxes."""
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    if bx0 >= bx1 or by0 >= by1:
        return [a]
    out = [(ax0, ay0, bx0, ay1), (bx1, ay0, ax1, ay1), (bx0, ay0, bx1, by0), (bx0, by1, bx1, ay1)]
    return [r for r in out if r[0] < r[2] and r[1] < r[3]]


def read_q16(path, n):
    """The n x n heightmap's uint16 grid indexed [gy, gx] (gy = north; PNG row 0 is north)."""
    with Image.open(path) as im:
        a = np.array(im)
    assert a.shape == (n, n) and a.dtype in (np.uint16, np.int32), (a.shape, a.dtype)
    return a.astype(np.uint16)[::-1]


def overlap_heights(old, new):
    """(samples, max |dh| float64, max |dh| float32) over the samples two blocks share.
    old/new: (cx, cy, q16 grid, zBase, maxHeight)."""
    ocx, ocy, go, zo, mo = old
    ncx, ncy, gn, zn, mn = new
    N = go.shape[0]
    dx, dy = int((ncx - ocx) / LATTICE["SQUARE"]), int((ncy - ocy) / LATTICE["SQUARE"])
    # new sample (gx, gy) is old sample (gx + dx, gy + dy)
    xs_n = slice(max(0, -dx), min(N, N - dx))
    ys_n = slice(max(0, -dy), min(N, N - dy))
    xs_o = slice(xs_n.start + dx, xs_n.stop + dx)
    ys_o = slice(ys_n.start + dy, ys_n.stop + dy)
    a, b = go[ys_o, xs_o], gn[ys_n, xs_n]
    if a.size == 0:
        return 0, 0.0, 0.0
    h64 = np.abs((zo + a * (mo / 65536)) - (zn + b * (mn / 65536)))
    f = np.float32
    h32 = np.abs((f(zo) + a.astype(f) * f(mo / 65536)) - (f(zn) + b.astype(f) * f(mn / 65536)))
    return int(a.size), float(h64.max()), float(h32.max())


def _cell_code(i, j):
    """One int64 per quadtree cell index pair (|i|, |j| < 2^30)."""
    return (np.asarray(i, np.int64) + (1 << 30)) * (1 << 31) + (np.asarray(j, np.int64) + (1 << 30))


def old_cover(old_by_anchor, frames, cur):
    """mask(X, Y, cov): sets cov where a previous anchor's shown tile covers the current-frame
    points (X, Y). Its tiles are quadtree cells of their own frame, looked up per level."""
    old_cells = {}
    for aid, boxes in old_by_anchor.items():
        by_size = old_cells.setdefault(aid, {})
        for bb in boxes:
            size = bb[2] - bb[0]
            by_size.setdefault(size, []).append(_cell_code(round(bb[0] / size), round(bb[1] / size)))

    def mask(X, Y, cov):
        for aid, by_size in old_cells.items():
            ox, oy, _ = frames[aid].ecef_to_enu(*frames[cur].enu_to_ecef(X, Y, 0.0))
            for size, codes in by_size.items():
                cov |= np.isin(_cell_code(np.floor(ox / size), np.floor(oy / size)), codes)
    return mask


def uncovered(far, frames, cur, focus, block, block_drawn=True, size=1024):
    """Sample points (x, y) of the checked area that neither the block's ground (if drawn: not in
    mesh near-visual mode) nor a shown, unhidden far tile covers: a coarse grid to RADIUS - EDGE,
    a fine one near the player, and the 2 m N/E strip past the block's collidable width."""
    gx0, gy0, gx1, gy1 = modsim.ground_box(*block, size)
    # the drawn ground (an empty box when the block is invisible)
    dx0, dy0, dx1, dy1 = (gx0, gy0, gx1, gy1) if block_drawn else (math.inf,) * 4
    cur_boxes, old_by_anchor = far.visible()
    old_mask = old_cover(old_by_anchor, frames, cur)
    fx, fy = focus
    holes = []

    for spacing, half in ((COARSE, ff.RADIUS - EDGE), (FINE, FINE_HALF)):
        i0, i1 = math.floor((fx - half) / spacing), math.ceil((fx + half) / spacing)
        j0, j1 = math.floor((fy - half) / spacing), math.ceil((fy + half) / spacing)
        xs, ys = (np.arange(i0, i1) + 0.5) * spacing, (np.arange(j0, j1) + 0.5) * spacing
        cov = np.zeros((len(ys), len(xs)), bool)
        cov[np.searchsorted(ys, dy0):np.searchsorted(ys, dy1, "right"),
            np.searchsorted(xs, dx0):np.searchsorted(xs, dx1, "right")] = True
        _fill(cov, xs, ys, cur_boxes)
        if old_by_anchor and not cov.all():
            jj, ii = np.nonzero(~cov)
            sub = np.zeros(len(jj), bool)
            old_mask(xs[ii], ys[jj], sub)
            cov[jj[sub], ii[sub]] = True
        jj, ii = np.nonzero(~cov)
        holes += list(zip(xs[ii], ys[jj]))
    # the 2 m N/E strip [cx+1022, cx+1024] beyond the collidable ground (P1)
    cx0, cy0, cx1, cy1 = modsim.cover_box(*block, size)
    along = np.arange(cx0 + STRIP_STEP / 2, cx1, STRIP_STEP)
    P = np.concatenate([np.stack([along, np.full_like(along, (gy1 + cy1) / 2)], 1),
                        np.stack([np.full_like(along, (gx1 + cx1) / 2), along - cx0 + cy0], 1)])
    cov = (P[:, 0] >= dx0) & (P[:, 0] <= dx1) & (P[:, 1] >= dy0) & (P[:, 1] <= dy1)
    for bb in cur_boxes:
        cov |= (P[:, 0] >= bb[0]) & (P[:, 0] < bb[2]) & (P[:, 1] >= bb[1]) & (P[:, 1] < bb[3])
    if old_by_anchor:
        old_mask(P[:, 0], P[:, 1], cov)
    holes += [tuple(p) + ("strip",) for p in P[~cov]]
    return holes


def near_holes(far, frames, cur, focus):
    """Mesh mode, every frame: sample points within NEAR_HALF of the player (every NEAR_STEP m)
    that no shown, unhidden tile covers (the previous anchor's ones included)."""
    fx, fy = focus
    k = np.arange(int(2 * NEAR_HALF / NEAR_STEP))
    xs = (math.floor((fx - NEAR_HALF) / NEAR_STEP) + k + 0.5) * NEAR_STEP
    ys = (math.floor((fy - NEAR_HALF) / NEAR_STEP) + k + 0.5) * NEAR_STEP
    cov = np.zeros((len(ys), len(xs)), bool)
    cur_boxes, old_by_anchor = far.visible()
    _fill(cov, xs, ys, cur_boxes)
    jj, ii = np.nonzero(~cov)
    if len(jj) and old_by_anchor:
        sub = np.zeros(len(jj), bool)
        old_cover(old_by_anchor, frames, cur)(xs[ii], ys[jj], sub)
        jj, ii = jj[~sub], ii[~sub]
    return list(zip(xs[ii], ys[jj]))


def _cyclic(tris):
    """Triangles as a set of index triples rotated to start at their smallest index (winding kept)."""
    t = np.asarray(tris).reshape(-1, 3)
    idx = (np.argmin(t, axis=1)[:, None] + np.arange(3)) % 3
    return set(map(tuple, np.take_along_axis(t, idx, axis=1).tolist()))


def _fill(cov, xs, ys, boxes):
    """Mark the sample points (xs, ys: sorted cell centres) inside any box [x0, x1) x [y0, y1)."""
    if not len(boxes):
        return
    b = np.asarray(boxes, np.float64)
    b = b[(b[:, 0] < xs[-1]) & (b[:, 2] > xs[0]) & (b[:, 1] < ys[-1]) & (b[:, 3] > ys[0])]
    i0, i1 = np.searchsorted(xs, b[:, 0]), np.searchsorted(xs, b[:, 2])
    j0, j1 = np.searchsorted(ys, b[:, 1]), np.searchsorted(ys, b[:, 3])
    for a, c, d, e in zip(i0.tolist(), i1.tolist(), j0.tolist(), j1.tolist()):
        cov[d:e, a:c] = True


def tile_vertex_heights(path, pos_z, n_cells):
    """(heights float64 [j, i], heights float32 [j, i], surface triangles ok) of a .cdae tile's
    (n_cells + 1)² surface vertices (pos z + vertex z) as the engine sees them. The writer orders the
    vertices its own way, so they are put on the grid by their x, y; a skirt vertex shares an
    edge vertex's x, y and lies lower. The surface triangles (all three corners on the surface)
    must be exactly grid_triangles(): the TerrainBlock's diagonals, counter-clockwise from +Z
    (the .cdae stores them reversed, clockwise from outside, as the engine does)."""
    m = cdae.mesh_arrays(cdae.read(path)["meshes"][0])
    n = n_cells + 1
    v = m["verts"]
    sp = float(v[:, 0].max()) / n_cells
    gi, gj = np.rint(v[:, 0] / sp).astype(int), np.rint(v[:, 1] / sp).astype(int)
    order = np.lexsort((v[:, 2], gj * n + gi))          # by cell, highest z last
    top = np.full(n * n, -1)
    top[(gj * n + gi)[order]] = order                    # the highest vertex of each grid point
    vz = v[top, 2].reshape(n, n)
    grid_of = np.full(len(v), -1)
    grid_of[top] = np.arange(n * n)
    t = grid_of[m["indices"].reshape(-1, 3)]
    tris_ok = _cyclic(t[(t >= 0).all(axis=1)][:, ::-1]) == _cyclic(ff.grid_triangles(n_cells))
    return pos_z + vz.astype(np.float64), np.float32(pos_z) + vz, tris_ok


FAR_ST = round(LATTICE["FAR_SQUARE"] / SQ)     # block samples per far-lattice square (2: 2 m on the 1 m block)


def block_heights(blk, x0, y0, n):
    """The terrain block's heights (float64, float32 as the engine computes them) at the n x n
    far-lattice points (every FAR_ST-th sample) from (x0, y0), [j, i]; None where they leave the block's valid
    samples. blk: (cx, cy, q16 grid, zBase, maxHeight)."""
    cx, cy, g, zb, mh = blk
    N = g.shape[0]
    half = heightmap.half_extent(N)
    gx0 = round((x0 - (cx - half)) / SQ)
    gy0 = round((y0 - (cy - half)) / SQ)
    last = FAR_ST * (n - 1) + 1
    if gx0 < 0 or gy0 < 0 or gx0 + last > N or gy0 + last > N:
        return None
    q = g[gy0:gy0 + last:FAR_ST, gx0:gx0 + last:FAR_ST]
    f = np.float32
    return zb + q * (mh / 65536), f(zb) + q.astype(f) * f(mh / 65536), (gx0, gy0)


class DriveBase(unittest.TestCase):
    """The drive; subclasses pick the server's near-visual mode, imagery and tile layout (cells)."""
    near_visual = "terrain"
    server_args = ("--near-visual", "terrain", "--imagery", "off")
    launcher = "earthd.py"
    cells = 32
    block = ff.TERRAIN_BLOCK    # M4 (docs/V1_4_SPEC.md §3.5): the server's --block (terrain mode: its 2 km block)
    # simulated seconds per wall second: half of SPEEDUP since the blocks went to 1 m squares (2026-11-01: 4x the
    # samples of the 2 m blocks the follow tables' lead was tuned for, the server's builds that much slower; at 4 the
    # builds of a cold start or a rebase arrived past the commit distance)
    @property
    def speedup(self):
        return SPEEDUP / 2

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.user = self.tmp.name
        self.errlog = open(os.path.join(self.user, "earthd.log"), "w+")
        script = SERVER_DIR / self.launcher if self.launcher == "earthd.py" else Path(__file__).parent / self.launcher
        self.proc = subprocess.Popen(
            [sys.executable, str(script), "--port", "0", "--user-dir", self.user, "--cache-dir", CACHE,
             "--weather", "off", "--gravity", "off", "--update-check", "off",  # no network: own tests
             # roads (and the grading under them) wait on Overture road data per block: own tests
             "--roads", "off", *self.server_args],
            stdout=subprocess.PIPE, stderr=self.errlog, text=True)
        self.addCleanup(stop_server, self.proc)     # a failed start too: no server or workers left behind
        line = self.proc.stdout.readline()
        self.assertIn("listening on 127.0.0.1:", line)
        self.c = Client(int(line.split("127.0.0.1:")[1].split()[0]))

    def tearDown(self):
        self.c.close()
        stop_server(self.proc)
        self.proc.stdout.close()
        self.errlog.close()
        keep = os.environ.get("BNGE_DRIVE_KEEP_LOG")
        if keep:
            Path(f"{keep}.{self.near_visual}{'' if self.cells == 32 else self.cells}{'' if self.block == 1024 else f'_b{self.block}'}"
                 ).write_text(Path(self.errlog.name).read_text())
        self.tmp.cleanup()

    def fs(self, vfs):
        return os.path.join(self.user, vfs.lstrip("/"))

    def run_drive(self):
        self.assertTrue(REBASE["MIN_DISTANCE"] <= REBASE_M <= REBASE["MAX_DISTANCE"])
        mesh = self.near_visual == "mesh"
        lay = ff.Layout(self.cells, self.block, mesh=mesh)     # what the server runs with (--cells, --block)
        ex = lay.exact_level
        size = lay.block
        N = size
        FT = modsim.follow_table(size)               # the mod's follow table for hello.blockSize (follow.configure)
        commit_ms, rebase_ms = COMMIT_MS[size], REBASE_MS[size]
        c = self.c
        frames = {}                                  # anchorId -> EnuFrame
        costs = COSTS_64 if self.cells == 64 else COSTS
        # 64 cells: the object-count term of the cost model (§3.4 Tests)
        scale = (lambda n: 1.0 + ALPHA * (n - OBJECTS_N0)) if self.cells == 64 else None
        far = modsim.FarField(costs, self.fs, cost_scale=scale)
        far.on_import = lambda inst: on_import(inst)
        gov = modsim.Governor()
        st = {"anchor": None, "block": None, "pending": None, "tp": None, "next": 1, "clock": 0.0,
              "pose_t": 0.0, "place": None, "loading_until": 0.0, "slow_for": 0.0, "active": False,
              "phase": "wait", "driven": 0.0, "driven_at_rebase": None, "check_until": -1}
        stats = {"terrain": [], "late_terrain": 0, "prepare_anchor": 0, "rebases": 0, "transients": [],
                 "hard_holes": [], "superseded_tiles": 0, "far_done": [], "errors": [], "commit_wall": {},
                 "seen_anchor_msg": set(), "hard_commits": 0, "gated_frames": 0, "tiles": 0, "cdae_parsed": 0,
                 "cdae_bad": [], "tile_bad": [], "terrain_bad": [], "max_lag_s": 0.0, "coverage_checks": 0,
                 "strip_checks": 0, "near_checks": 0, "near_holes": [], "tex_bad": [], "dds_checked": 0,
                 "l2_checked": 0, "l2_bad": [], "l2_d64": 0.0, "l2_d32": 0.0, "under_not_l2": [],
                 "mat_bad": [], "max_mats": 0, "budget_over": [], "bilinear": [], "bilinear_over": [],
                 "max_unit_ms": 0.0, "objects_max": 0, "edge_min": math.inf, "edge_min_at": None,
                 "terrain_latency": None, "anchor_latency": None}
        exact_grids = {}                             # (anchorId, key, shape) -> float64 vertex heights
        rng = np.random.default_rng(1234)
        textures = {}                                # material name -> local texture path
        l2_done = set()                              # (anchorId, key, shape) of checked L-2 tiles
        files = {"dir": None, "seen": set(), "loaded": set(), "deleted": []}  # (g)
        clock = {"sim": 0.0, "wall0": time.monotonic(), "frame": 0, "dt": BASE_MS / 1000}
        player = {"p": [0.0, 0.0, 0.0], "v": [0.0, 0.0]}
        transient = {"open": None}
        hm = {}                                      # (aid, cx, cy) -> (cx, cy, q16, zBase, maxHeight)
        events = []                                  # (sim s, type, keys) of far messages
        commits = []                                 # terrain-follow / rebase / teleport commits
        frame_log = []                               # per frame: see record()
        heavy = {"ms": 0.0, "kind": None}

        def rid():
            st["next"] += 1
            return st["next"] - 1

        # --- messages (core.lua onMessage) ---------------------------------------------------
        def check_terrain(aid, tr):
            if (tr["size"], tr["squareSize"]) != (N, LATTICE["SQUARE"]) or (tr["cx"] % FT["GRID"], tr["cy"] % FT["GRID"]) != (0, 0) \
                    or tr["maxHeight"] != tr["q"] * LATTICE["Q_SCALE"] \
                    or tr["q"] not in LATTICE["Q_LADDER"] or (tr["zBase"] / tr["q"]) % 1 != 0:
                stats["terrain_bad"].append(tr)
            path = self.fs(tr["heightmap"])
            self.assertTrue(os.path.exists(path), tr["heightmap"])
            hm[(aid, tr["cx"], tr["cy"])] = (tr["cx"], tr["cy"], read_q16(path, N), tr["zBase"], tr["maxHeight"])
            while len(hm) > 8:                      # the committed block is among the newest few
                del hm[next(iter(hm))]

        def check_tile(m):
            stats["tiles"] += 1
            path = self.fs(m["shape"])
            t = ff.tile_of(m["key"])
            ok = (os.path.exists(path) and m["shape"].endswith(".cdae") and m["level"] == t.level
                  and (m["pos"][0], m["pos"][1]) == t.bbox[:2] and m["err"] >= 0 and m.get("state") == "group"
                  and abs(m["skirt"] - ff.skirt_depth(t.size, m["err"])) <= 0.01
                  and lay.min_level <= t.level <= ff.MAX_LEVEL)
            if t.level == 0 and not mesh:   # the mod hides a covered tile without `sink`: the N/E strip needs it
                ok = ok and m.get("sink") is not None and m["sink"] >= ff.SINK_MARGIN - 0.005
            elif mesh:                      # nothing is covered or sunk in mesh mode
                ok = ok and "sink" not in m
            # imagery: the offline source never fails, so every mesh-mode tile is textured
            want_mat = m.get("material") if mesh else None      # a pooled name (matslots.py)
            ok = (ok and (want_mat is None or re.fullmatch(r"bngEarthFar_[0-9a-f]{4}_\d{5}", want_mat) is not None)
                  and m.get("material") == want_mat and ("texture" in m) == mesh and ("detail" in m) == mesh)
            # M1: the texture size rides on FORMAT 4 tiles only (the mod's governor kind)
            ok = ok and m.get("tex") == (lay.tex_size(t.level) if mesh and lay.format >= 4 else None)
            if mesh and ok:
                tex = self.fs(m["texture"])
                textures[want_mat] = tex
                ok = (m["detail"] is True and os.path.exists(tex)
                      and os.path.basename(m["shape"]) == f"slot_{want_mat.rsplit('_', 1)[1]}.cdae")
                if ok and stats["tiles"] % CDAE_SAMPLE == 1:
                    with open(tex, "rb") as f:
                        w, h, mips, fourcc, sizes = imagery.read_dds_info(f.read(imagery.DDS_HEADER))
                    stats["dds_checked"] += 1
                    full = w.bit_length()                   # mips down to 1 x 1
                    if not (w == h == lay.tex_size(t.level) and fourcc == b"DXT1" and mips == full
                            and os.path.getsize(tex) == imagery.DDS_HEADER + sum(sizes)):
                        stats["tex_bad"].append((m["texture"], w, h, mips, fourcc))
            if not ok:
                stats["tile_bad"].append(m)
            if stats["tiles"] % CDAE_SAMPLE == 1 and os.path.exists(path):
                try:
                    d = cdae.read(path)
                    mesh_ = cdae.mesh_arrays(d["meshes"][0])
                    nv = (lay.cells + 1) ** 2 + 4 * (lay.cells + 1)
                    good = (d["version"] == cdae.VERSION_WORD and len(d["meshes"]) == 1
                            and [x["name"] for x in d["materials"]] == [m.get("material") or MATERIAL]
                            # the full lattice: 2304 indices, 8704 with 64 cells; the adaptive 64-cell meshes
                            # (rtin_mesh) keep fewer where the ground is flat
                            and (len(mesh_["verts"]) == nv if lay.cells != 64 else 0 < len(mesh_["verts"]) <= nv)
                            and (len(mesh_["indices"]) == 3 * lay.tris if lay.cells != 64
                                 else 0 < len(mesh_["indices"]) <= 3 * lay.tris and len(mesh_["indices"]) % 3 == 0)
                            and abs(d["bounds"][3] - d["bounds"][0] - t.size) < 1e-3
                            # UV1 (detail map) exactly on textured tiles
                            and len(mesh_["tverts2"]) == (len(mesh_["verts"]) if mesh else 0)
                            # textured shapes carry UV1: 94 KB (V1_3 server report), 348 KB with 64 cells
                            and os.path.getsize(path) <= CDAE_MAX_KB[(lay.cells, mesh)] * 1024)
                    stats["cdae_parsed"] += 1
                    if not good:
                        stats["cdae_bad"].append((m["shape"], d["bounds"], d["materials"]))
                except cdae.CdaeError as e:
                    stats["cdae_bad"].append((m["shape"], str(e)))

        def handle(m):
            t = m["type"]
            aid = m.get("anchorId")
            if t in ("anchor_error", "terrain_error"):
                stats["errors"].append(m)
            elif t == "status" and "mismatch" in m["text"]:
                stats["errors"].append(m)
            elif t == "hello":
                # the server's format is its layout's; the mod reads it (FORMATS) and takes the
                # cell count from hello.cells (crossover.setCells, farfield.exactLevelFor)
                self.assertEqual((m["protocol"], m["format"]), (CORE["PROTOCOL"], lay.format))
                self.assertIn(m["format"], CORE["FORMATS"])
                self.assertEqual(m.get("cells"), 64 if self.cells == 64 else None)
                # M4: hello.blockSize (core.lua setBlockSize: the lattice and follow table above)
                self.assertEqual(m.get("blockSize", 1024), size)
                far.cells = modsim.set_cells(m.get("cells"))
                self.assertEqual(modsim.exact_level_for(far.cells), ex)
                self.assertEqual((m["farFormat"], m["material"]), ("cdae", MATERIAL))
                # core.lua: hello.nearVisual sets the mode of the terrain and the far field
                self.assertEqual(m["nearVisual"], self.near_visual)
                far.set_near_visual(m["nearVisual"])
                files["dir"] = self.fs(f"/temp/beamngEarth/{m['sessionId']}")
            elif t == "anchor_ready":
                p = st["pending"]
                if not p or m["requestId"] != p["requestId"]:
                    return
                frames[m["anchorId"]] = geodesy.EnuFrame(m["lat"], m["lon"])
                check_terrain(m["anchorId"], m["terrain"])
                p["ready"], p["msg"] = True, m
                if p["purpose"] == "rebase":
                    stats["anchor_latency"] = st["clock"] - p["sent"]
                if p["purpose"] == "teleport":
                    st["pending"] = None
                    commit_teleport(m)
            elif t == "terrain_ready":
                p = st["tp"]
                if not p or m["requestId"] != p["requestId"] or aid != st["anchor"]:
                    return
                self.assertEqual((m["cx"], m["cy"]), (p["cx"], p["cy"]))
                check_terrain(aid, m)
                p["ready"], p["msg"] = True, m
                stats["terrain_latency"] = st["clock"] - p["sent"]
                x, y = player["p"][:2]
                stats["terrain"].append({"wall_ms": (time.monotonic() - p["wall"]) * 1000,
                                         "margin_m": FT["COMMIT_DIST"] - cheb(x, y, *st["block"])})
            elif t in ("far_tile", "far_swap", "far_remove", "far_done"):
                if aid != st["anchor"]:
                    # another anchor: never committed (a bug), or a tile of the previous one sent
                    # after the server had already switched (a bug) rather than while our commit
                    # was in flight (dropped by the mod, harmless)
                    if aid not in stats["commit_wall"] or (
                            t == "far_tile" and st["anchor"] in stats["seen_anchor_msg"]):
                        stats["superseded_tiles"] += 1
                else:
                    stats["seen_anchor_msg"].add(aid)
                if t == "far_tile":
                    check_tile(m)
                    events.append((clock["sim"], t, [m["key"]]))
                elif t == "far_remove":
                    events.append((clock["sim"], t, [m["key"]]))
                elif t == "far_swap":
                    events.append((clock["sim"], t, m["add"] + m["remove"]))
                    if (m["add"] and m["remove"] and cells(m["add"]) != cells(m["remove"])
                            and not radius_edge_group(m["add"], m["remove"], player["p"][:2])):
                        # adds reaching past the removes are fine over ground no tile of this anchor shows (a
                        # previous anchor's, after a rebase: the mod drops those once covered); a hole (removes past
                        # the adds) or an overlap with a tile of this anchor that stays is a bug
                        ca, cr = cells(m["add"]), cells(m["remove"])
                        stays = cells([k for k in far.live if k not in m["remove"]]) if aid == far.anchor_id else set()
                        if (cr - ca) or ((ca - cr) & stays):
                            stats.setdefault("bad_swaps", []).append(m)
                getattr(far, {"far_tile": "on_tile", "far_swap": "on_swap", "far_remove": "on_remove",
                              "far_done": "on_done"}[t])(m)
                if t == "far_done" and aid == st["anchor"]:
                    stats["far_done"].append((m["count"], len(far.eager_keys())))

        def pump(until_wall):
            while True:
                try:
                    m = c.recv(max(until_wall - time.monotonic(), 0.0005))
                except TimeoutError:
                    if time.monotonic() >= until_wall:
                        return
                    continue
                handle(m)

        # --- commits (core.lua commitTeleport / commitTerrain / commitRebase) -----------------
        def send_commits(aid, cx, cy):
            stats["commit_wall"][aid] = time.monotonic()
            c.send({"type": "commit_anchor", "anchorId": aid})
            c.send({"type": "commit_terrain", "anchorId": aid, "cx": cx, "cy": cy})

        def commit_teleport(m):
            gov.heavy_now()
            heavy["ms"], heavy["kind"] = heavy["ms"] + TELEPORT_MS, "teleport"
            tr = m["terrain"]
            far.switch_anchor(m["anchorId"], False, (tr["cx"], tr["cy"]))
            st["loading_until"] = st["clock"] + CORE["LOADING_AFTER_TELEPORT_S"]
            st["place"] = int(CORE["PLACE_DELAY_FRAMES"])
            player["p"], player["v"] = [0.0, 0.0, (m["groundZ"] or 0.0) + 1.0], [0.0, 0.0]
            st["anchor"], st["block"], st["tp"] = m["anchorId"], (tr["cx"], tr["cy"]), None
            send_commits(m["anchorId"], tr["cx"], tr["cy"])
            st["active"], st["phase"] = True, "stand"
            st["pose_t"] = CORE["POSE_INTERVAL"]
            commits.append({"kind": "teleport", "frame": clock["frame"], "sim": clock["sim"]})

        def commit_terrain(msg):
            heavy["ms"], heavy["kind"] = heavy["ms"] + commit_ms, "follow"
            old, new = st["block"], (msg["cx"], msg["cy"])
            far.on_block_commit(*new)
            st["block"] = new
            c.send({"type": "commit_terrain", "anchorId": st["anchor"], "cx": new[0], "cy": new[1]})
            x, y = player["p"][:2]
            if cheb(x, y, *old) > FT["HARD_COMMIT_DIST"]:
                stats["hard_commits"] += 1
            # (a) the samples both blocks share
            n, d64, d32 = overlap_heights(hm[(st["anchor"], *old)], hm[(st["anchor"], *new)])
            # (b) the server's selection with and without the old block forced (pending = new);
            # mesh mode forces no blocks
            diff = 0
            if not mesh:
                k0, r0 = ff.select(x, y, [lay.block_at(*old).bbox, lay.block_at(*new).bbox])
                k1, _ = ff.select(x, y, [lay.block_at(*new).bbox], r0)
                diff = len(k0.keys() ^ k1.keys())
            commits.append({"kind": "follow", "frame": clock["frame"], "sim": clock["sim"], "old": old, "new": new,
                            "samples": n, "d64": d64, "d32": d32, "select_diff": diff,
                            "dist": cheb(x, y, *old)})
            open_transient("follow", lay.block_at(*old).bbox, new)
            st["check_until"] = clock["frame"] + CHECK_AFTER_COMMIT

        def commit_rebase(m):
            heavy["ms"], heavy["kind"] = heavy["ms"] + rebase_ms, "rebase"
            gov.heavy_now()
            src, dst = frames[st["anchor"]], frames[m["anchorId"]]
            old_bb = ff.transform_bbox(src, dst, lay.block_at(*st["block"]).bbox)
            x, y, z = player["p"]
            vx, vy = player["v"]
            player["p"] = list(to_frame(src, dst, x, y, z))
            ex, ey, _ = to_frame(src, dst, x + vx, y + vy, z)
            player["v"] = [ex - player["p"][0], ey - player["p"][1]]
            tr = m["terrain"]
            far.transform(src, dst)
            far.switch_anchor(m["anchorId"], True, (tr["cx"], tr["cy"]))
            st["anchor"], st["block"], st["tp"] = m["anchorId"], (tr["cx"], tr["cy"]), None
            send_commits(m["anchorId"], tr["cx"], tr["cy"])
            st["pose_t"] = CORE["POSE_INTERVAL"]
            stats["rebases"] += 1
            st["driven_at_rebase"] = st["driven"]
            commits.append({"kind": "rebase", "frame": clock["frame"], "sim": clock["sim"]})
            st["rebase_wall"] = time.monotonic()
            open_transient("rebase", old_bb, st["block"])
            st["check_until"] = clock["frame"] + CHECK_AFTER_COMMIT

        # --- coverage (c) --------------------------------------------------------------------
        def check_files():
            """No shape file may vanish while the mod still has a TSStatic for it (the engine
            watches loaded files): every loaded instance, shown, held, kept or awaiting deletion.
            Mesh mode: no texture may vanish while its Material exists (the engine loads it
            when a tile using the material is drawn), and the Materials are exactly the ones
            the mod's tile objects refer to (satmat.lua reference counts, no leak)."""
            insts = list(far.live.values()) + list(far.old.values()) + list(far.pending.values()) + far.doomed
            # a running crossover's outgoing tiles are in none of those
            insts = list({id(i): i for i in insts + [r for g in far.groups for r in g.rems]}.values())
            gone = [i.path for i in insts if i.loaded and not os.path.exists(i.path)]
            # the texture each material in use shows (an unused pooled one keeps a deleted tile's name: harmless)
            gone += [far.mat_tex[n] for n, r in far.mats.items()
                     if r and far.mat_tex.get(n) and not os.path.exists(self.fs(far.mat_tex[n]))]
            stats["files_checked"] = stats.get("files_checked", 0) + len(insts) + len(far.mats)
            if gone:
                stats.setdefault("vanished", []).append((round(clock["sim"], 2), gone[:3]))
            refs = {}
            for i in insts:
                if i.mat is not None:
                    refs[i.mat] = refs.get(i.mat, 0) + 1
            unused = {n for n, r in far.mats.items() if r == 0 and not modsim.pooled(n)}
            if ({n: r for n, r in far.mats.items() if r} != refs or not unused <= set(far.mat_del)
                    or any(not i.material for i in insts if i.mat)):
                stats["mat_bad"].append((round(clock["sim"], 2), len(far.mats), len(refs), len(unused)))
            stats["max_mats"] = max(stats["max_mats"], len(far.mats))
            watch_deletes()

        def on_import(inst):
            files["loaded"].add(inst.path)
            if inst.material:
                files["loaded"].add(textures[inst.material])

        def watch_deletes():
            """(g) files of the session folder that disappeared since the last look (wall time)."""
            if files["dir"] is None or not os.path.isdir(files["dir"]):
                return
            now = set(os.listdir(files["dir"]))
            t = time.monotonic()
            for n in files["seen"] - now:
                files["deleted"].append((t, os.path.join(files["dir"], n) in files["loaded"]))
            files["seen"] = now

        def check_near_ground():
            """(f) mesh mode, at each pose: the tile under the player is the exact level, every shown
            exact tile over the committed block has the block's heights and diagonals, and between
            the vertices the mesh under the player is within |twist|/4 of the bilinear collision."""
            x, y = player["p"][:2]
            # (right after a rebase the previous anchor's tiles, at their footprint in this frame)
            under = [i for i in list(far.live.values()) + list(far.old.values())
                     if not far.hidden(i) and i.x0 <= x < i.x1 and i.y0 <= y < i.y1]
            if not under or any(i.level != ex for i in under):
                stats["under_not_l2"].append((round(clock["sim"], 2), [i.key for i in under]))
            blk = hm.get((st["anchor"], *st["block"]))
            if blk is None:
                return
            n = lay.cells + 1
            for i in list(far.live.values()):
                ck = (i.aid, i.key, i.path)
                if i.level != ex or not i.loaded or ck in l2_done:
                    continue
                ref = block_heights(blk, i.bbox0[0], i.bbox0[1], n)
                if ref is None:
                    continue                    # not over the committed block (yet)
                l2_done.add(ck)
                h64, h32, tris_ok = tile_vertex_heights(i.path, i.z, lay.cells)
                exact_grids[ck] = h64
                d64 = float(np.abs(h64 - ref[0]).max())
                d32 = float(np.abs(h32 - ref[1]).max())
                stats["l2_checked"] += 1
                stats["l2_d64"], stats["l2_d32"] = max(stats["l2_d64"], d64), max(stats["l2_d32"], d32)
                gx0, gy0 = ref[2]
                # the tile's cell (i, j) is far-lattice square (gx0 / FAR_ST + i, gy0 / FAR_ST + j): same split when
                # those are even
                if d64 > 1e-9 or d32 > 1e-3 or gx0 % (2 * FAR_ST) or gy0 % (2 * FAR_ST) or not tris_ok:
                    stats["l2_bad"].append((i.key, d64, d32, (gx0, gy0)))
            # the visible surface between vertices against the engine's bilinear collision (E7):
            # random points in the current-anchor exact tile under the player
            cur = [i for i in under if i.aid == st["anchor"] and i.level == ex and i.loaded]
            grid = exact_grids.get((cur[0].aid, cur[0].key, cur[0].path)) if cur else None
            if grid is None:
                return
            cx, cy, q16, zb, mh = blk
            tx0, ty0 = cur[0].bbox0[:2]
            size = ff.level_size(ex)
            fsq = heightmap.FAR_SQUARE
            for px_, py_ in rng.random((BILINEAR_PER_POSE, 2)) * size:
                # the bilinear surface of the far lattice's samples (every FAR_ST-th), and the 1 m collision itself
                bil = heightmap.height_at(q16[::FAR_ST, ::FAR_ST], zb, mh, cx, cy, tx0 + px_, ty0 + py_, square=fsq)
                col = heightmap.height_at(q16, zb, mh, cx, cy, tx0 + px_, ty0 + py_)
                if bil is None or col is None:
                    continue                    # the block's N/E strip: no collision there
                z = float(ff.surface_at(grid, fsq, px_, py_))
                gi, gj = min(int(px_ // fsq), lay.cells - 1), min(int(py_ // fsq), lay.cells - 1)
                twist = abs(grid[gj, gi] - grid[gj, gi + 1] - grid[gj + 1, gi] + grid[gj + 1, gi + 1])
                d = abs(z - bil)
                stats.setdefault("collision_1m", []).append(abs(z - col))
                stats["bilinear"].append(d)
                stats.setdefault("bilinear_bound", []).append(twist / 4)
                if d > twist / 4 + 1e-6:
                    stats["bilinear_over"].append((cur[0].key, round(px_, 2), round(py_, 2), d, twist / 4))

        def check_coverage():
            if not stats.get("loaded"):
                return
            stats["coverage_checks"] += 1
            holes = uncovered(far, frames, st["anchor"], player["p"][:2], st["block"], block_drawn=not mesh, size=size)
            op = transient["open"]
            vac = op["vacated"] if op and op["kind"] == "rebase" else []
            hard = [h for h in holes if not any(v[0] <= h[0] < v[2] and v[1] <= h[1] < v[3] for v in vac)]
            if hard:
                stats["hard_holes"].append((round(clock["sim"], 2), len(hard), hard[:3], player["p"][:2]))
            if op:
                if holes:
                    op["max_pts"] = max(op["max_pts"], len(holes))
                    op["last_holes"] = holes[:5]
                else:
                    op["closed_sim_s"] = clock["sim"] - op["sim"]
                    stats["transients"].append(op)
                    transient["open"] = None

        def open_transient(kind, old_block_bb, new_block):
            # mesh mode: the block is not drawn, so moving it vacates nothing (every hole is hard)
            vac = [] if mesh else subtract(old_block_bb, lay.block_at(*new_block).bbox)
            if transient["open"]:
                stats["transients"].append(transient["open"])
            transient["open"] = {"kind": kind, "vacated": vac, "max_pts": 0, "sim": clock["sim"],
                                 "closed_sim_s": None}

        # --- the mod's frame (core.lua onUpdate / updateWorld) --------------------------------
        def send_pose():
            x, y, z = player["p"]
            vx, vy = player["v"]
            lat, lon, _ = frames[st["anchor"]].enu_to_geodetic(x, y, z)
            sp = math.hypot(vx, vy)
            pose = {"type": "pose", "anchorId": st["anchor"], "x": x, "y": y, "z": z, "vx": vx, "vy": vy,
                    "lat": float(lat), "lon": float(lon), "speed": sp, "cam": camera()}
            gone = far.take_slots_gone()
            if gone:                    # pooled far-tile slots let go (matslots.py)
                pose["slotsGone"] = gone
            c.send(pose)

        def camera():
            """An orbit camera 8 m behind and 3 m above the car, looking along the heading."""
            x, y, z = player["p"]
            vx, vy = player["v"]
            sp = math.hypot(vx, vy)
            fx, fy = (vx / sp, vy / sp) if sp > 1 else (1.0, 0.0)
            return {"x": x - 8 * fx, "y": y - 8 * fy, "z": z + 3, "fx": fx, "fy": fy, "fz": -0.05, "fovDeg": 60}

        def update_world():
            if st["place"] is not None:
                st["place"] -= 1
                if st["place"] <= 0:
                    st["place"] = None
                return
            x, y, z = player["p"]
            vx, vy = player["v"]
            st["pose_t"] += clock["dt"]
            if st["pose_t"] >= CORE["POSE_INTERVAL"]:
                st["pose_t"] = 0.0
                send_pose()
                check_coverage()
                check_files()
                if mesh and stats.get("loaded"):
                    check_near_ground()
            # origin rebase first
            p = st["pending"]
            stale = edge = None
            if p and p.get("ready"):
                m = p["msg"]
                nx, ny, _ = to_frame(frames[st["anchor"]], frames[m["anchorId"]], x, y, z)
                stale = cheb(nx, ny, m["terrain"]["cx"], m["terrain"]["cy"])
                gx0, gy0, gx1, gy1 = modsim.ground_box(m["terrain"]["cx"], m["terrain"]["cy"], size)
                edge = min(nx - gx0, gx1 - nx, ny - gy0, gy1 - ny)
            act = modsim.rebase_decide(math.hypot(x, y), p, stale, REBASE_M, edge=edge, size=size)
            if act == "prefetch":
                lat, lon, _ = frames[st["anchor"]].enu_to_geodetic(x, y, z)
                cx, cy = modsim.rebase_hint(vx, vy, math.hypot(x, y), REBASE_M, size, stats["anchor_latency"])
                st["pending"] = {"requestId": rid(), "purpose": "rebase", "wall": time.monotonic(),
                                 "sent": st["clock"]}
                stats["prepare_anchor"] += 1
                c.send({"type": "prepare_anchor", "requestId": st["pending"]["requestId"],
                        "lat": float(lat), "lon": float(lon), "cx": cx, "cy": cy})
            elif act == "commit":
                if gov.request_heavy():
                    st["pending"] = None
                    commit_rebase(p["msg"])
                return
            elif act == "discard":
                st["pending"] = None
            # terrain-follow
            b = st["block"]
            gate = lambda ncx, ncy: far.uncover_ready(b[0], b[1], ncx, ncy)  # noqa: E731
            tp = st["tp"]
            fa = modsim.follow_decide(x, y, vx, vy, b[0], b[1], tp, gate=gate, latency=stats["terrain_latency"],
                                      size=size)
            if fa is None and tp and tp.get("ready") and cheb(x, y, *b) > FT["COMMIT_DIST"]:
                stats["gated_frames"] += 1
            # late: a request made inside the commit distance not ready past it (the 1 km block: the commit-time
            # re-check re-prepares past the commit distance on purpose; those are not counted)
            if (tp and not tp.get("ready") and (tp["on_time"] or size * SQ >= 2048) and cheb(x, y, *b) > FT["COMMIT_DIST"]
                    and not tp.get("late")):
                tp["late"] = True
                stats["late_terrain"] += 1
                stats.setdefault("late_at", []).append((round(st["clock"], 2), tp["cx"], tp["cy"], b,
                                                        round(st["clock"] - tp["sent"], 2)))
            if fa and fa[0] == "prepare":
                st["tp"] = {"requestId": rid(), "cx": fa[1], "cy": fa[2], "wall": time.monotonic(),
                            "sent": st["clock"], "on_time": cheb(x, y, *b) <= FT["COMMIT_DIST"]}
                stats["prepares"] = stats.get("prepares", 0) + 1
                if not st["tp"]["on_time"]:
                    stats["late_prepares"] = stats.get("late_prepares", 0) + 1
                c.send({"type": "prepare_terrain", "requestId": st["tp"]["requestId"], "anchorId": st["anchor"],
                        "cx": fa[1], "cy": fa[2]})
            elif fa and fa[0] == "commit":
                if gov.request_heavy():
                    st["tp"] = None
                    commit_terrain(tp["msg"])
            elif fa and fa[0] == "discard":
                st["tp"] = None

        def drive():
            """The driver: velocity for this frame, and the phase transitions."""
            if st["phase"] == "stand":
                player["v"] = [0.0, 0.0]
                if stats["far_done"] and far.done and not far.has_work() and not far.old:
                    st["phase"] = "drive"
                    stats["loaded"] = True
                    stats["initial"] = {"tiles": stats["far_done"][0][0], "sim": clock["sim"],
                                        "wall": time.monotonic() - stats["t_teleport"]}
                    check_coverage()
            elif st["phase"] == "drive":
                h = heading(st["driven"])
                player["v"] = [SPEED * math.cos(h), SPEED * math.sin(h)]
                if st["driven_at_rebase"] is not None and st["driven"] - st["driven_at_rebase"] >= AFTER_REBASE_M:
                    st["phase"] = "stop"
                    player["v"] = [0.0, 0.0]
                    st["stop_wall"] = st["quiet_wall"] = time.monotonic()
                    st["log_n"] = busy_msgs(c.log)

        def frame():
            clock["frame"] += 1
            dt = clock["dt"] = (frame_log[-1][-1] if frame_log else BASE_MS) / 1000
            clock["sim"] += dt
            st["clock"] += dt
            heavy["ms"], heavy["kind"] = 0.0, None
            gov.loading = (st["place"] is not None or st["clock"] < st["loading_until"]
                           or st["slow_for"] > CORE["SLOW_S"])
            gov.begin_frame(far.has_work(), frame_log[-1][-1] if frame_log else None)
            pump(clock["wall0"] + clock["sim"] / self.speedup)
            far_units = imports = 0
            est = 0.0
            if st["active"]:
                drive()
                x, y, z = player["p"]
                vx, vy = player["v"]
                player["p"] = [x + vx * dt, y + vy * dt, z]
                st["driven"] += math.hypot(vx, vy) * dt
                speed = math.hypot(vx, vy)
                st["slow_for"] = st["slow_for"] + dt if speed < CORE["SLOW_SPEED"] else 0.0
                update_world()
                # the player against the committed block's collidable area (M4: never within 150 m of an edge)
                if st["block"] is not None and st["place"] is None:
                    gx0, gy0, gx1, gy1 = modsim.ground_box(*st["block"], size)
                    px_, py_ = player["p"][:2]
                    e = min(px_ - gx0, gx1 - px_, py_ - gy0, gy1 - py_)
                    if e < stats["edge_min"]:
                        stats["edge_min"], stats["edge_min_at"] = e, (round(clock["sim"], 2), px_, py_, st["block"])
                mu0 = gov.max_unit()
                far.update(gov, dt, camera())
                far_units, imports, est = far.frame["units"], far.frame["imports"], far.frame["est"]
                stats["objects_max"] = max(stats["objects_max"], far.objects())
                # (e) the frame's budget: every unit was granted while frame_spent + est <= b + max unit;
                # the last one may overshoot by its cost's excess over its estimate (64 cells: costs
                # move with the object count, so the frame's own excess)
                cap = gov.b + max(mu0, gov.max_unit()) + max(EXCESS_MS, far.frame["excess"])
                stats["max_unit_ms"] = max(stats["max_unit_ms"], gov.max_unit())
                if gov.frame_spent > cap + 1e-9:
                    stats["budget_over"].append((clock["frame"], gov.frame_spent, cap))
                if clock["frame"] <= st["check_until"] or transient["open"]:
                    check_coverage()
                if mesh and stats.get("loaded"):
                    stats["near_checks"] += 1
                    h = near_holes(far, frames, st["anchor"], player["p"][:2])
                    if h:
                        stats["near_holes"].append((round(clock["sim"], 2), len(h), h[:3], player["p"][:2]))
            wall = BASE_MS + gov.frame_spent + heavy["ms"]
            # (frame, sim, units, imports, est ms, spent ms, heavy kind, loading, b, wall)
            frame_log.append((clock["frame"], clock["sim"], far_units, imports, est, gov.frame_spent, heavy["kind"],
                              gov.loading, gov.b, wall))
            stats["max_lag_s"] = max(stats["max_lag_s"], time.monotonic() - (clock["wall0"] + clock["sim"] / self.speedup))
            self.assertFalse(stats["errors"], stats["errors"])

        # --- run ---------------------------------------------------------------------------
        c.send({"type": "hello", "protocol": int(CORE["PROTOCOL"]), "format": int(CORE["FORMAT"]),
                "modVersion": MOD_VERSION})
        st["pending"] = {"requestId": rid(), "purpose": "teleport", "wall": time.monotonic()}
        c.send({"type": "prepare_anchor", "requestId": st["pending"]["requestId"], "lat": LAT, "lon": LON,
                "cx": 0, "cy": 0})
        t0 = stats["t_teleport"] = time.monotonic()
        while st["phase"] in ("wait", "stand"):
            self.assertLess(time.monotonic() - t0, 300, "no initial far set")
            frame()
        self.assertFalse(stats["hard_holes"], stats["hard_holes"][:1])
        drive0 = {"tiles": stats["tiles"], "events": len(events), "frame": clock["frame"]}
        while st["phase"] == "drive":
            self.assertLess(st["driven"], REBASE_M * 1.6 + 5000, "no rebase")
            frame()
        drive1 = {"tiles": stats["tiles"], "frame": clock["frame"]}
        # stopped: wait until the current anchor's set is done and the server is quiet
        while not (far.done and not far.has_work() and time.monotonic() - st["quiet_wall"] > 1.0):
            self.assertLess(time.monotonic() - st["stop_wall"], 120, "no final far_done")
            frame()
            if busy_msgs(c.log) != st["log_n"]:
                st["log_n"], st["quiet_wall"] = busy_msgs(c.log), time.monotonic()
        check_coverage()
        if transient["open"]:
            stats["transients"].append(transient["open"])
        # (g) the server prunes the previous anchor's files PRUNE_DELAY after its new set is done:
        # watch the first OBSERVE_DELETES_S of it (rate-limited, never a burst)
        while time.monotonic() - st["stop_wall"] < OBSERVE_MAX_S:
            first = next((t for t, loaded in files["deleted"] if loaded), None)
            if first is not None and time.monotonic() - first >= OBSERVE_DELETES_S:
                break
            frame()                                 # (the poses look at the folder)

        # --- evaluation --------------------------------------------------------------------
        log = Path(self.errlog.name).read_text()
        bad = [ln for ln in log.splitlines() if re.search(r"Traceback| ERROR | WARNING ", ln)]
        built = re.findall(r"INFO earthd: terrain \((\d+), (-?[\d.]+), (-?[\d.]+)\) z", log)
        hm_built = len(built)
        asked = set(re.findall(r"terrain_ready \((\d+), (-?[\d.]+), (-?[\d.]+)\)", log))
        asked |= {(a, f"{float(x)}", f"{float(y)}")
                  for a, x, y in re.findall(r"anchor (\d+) \(.*terrain at \((-?\d+), (-?\d+)\)", log)}
        unasked = [b for b in built if b not in asked]
        km = st["driven"] / 1000.0
        follows = [x for x in commits if x["kind"] == "follow"]
        reb_tr = [t for t in stats["transients"] if t["kind"] == "rebase"]
        fol_tr = [t for t in stats["transients"] if t["kind"] == "follow"]
        lat_ms = [t["wall_ms"] for t in stats["terrain"]]

        # (b) far messages around each terrain-follow commit
        ev_t = np.array([e[0] for e in events])
        for cm in follows:
            tc = cm["sim"]
            near = [events[i] for i in np.nonzero((ev_t >= tc - WINDOW_S / 2) & (ev_t <= tc + WINDOW_S / 2))[0]]
            cm["around"] = sum(len(e[2]) for e in near)
            vac = subtract(lay.block_at(*cm["old"]).bbox, lay.block_at(*cm["new"]).bbox)
            after = [events[i] for i in np.nonzero((ev_t > tc) & (ev_t <= tc + WINDOW_S))[0]]
            cm["caused"] = [(round(e[0] - tc, 2), e[1], k) for e in after for k in e[2]
                            if any(ff.overlaps(tile_bbox(k), v) for v in vac)]

        # (e) governor replay
        F = np.array([(f[0], f[2], f[3], f[4], f[5], f[7], f[8], f[9]) for f in frame_log])
        fidx, units, imps, ests, spent, loading, bvals, walls = (F[:, i] for i in range(8))
        heavy_frames = [(f[0], f[6]) for f in frame_log if f[6]]
        qb, qa = int(GOV["QUIET_BEFORE"]), int(GOV["QUIET_AFTER"])
        pos = {int(f): i for i, f in enumerate(fidx)}
        isolation = []
        for fr, kind in heavy_frames:
            if kind == "teleport":
                continue                            # urgent: granted at once by design
            i = pos[fr]
            win = units[max(0, i - qb):i + qa + 1]
            if win.any():
                isolation.append((fr, kind, units[max(0, i - qb):i + qa + 1].tolist()))
        steady = (loading == 0) & (bvals <= GOV["DEFAULT_TARGET_MS"] + 1e-9)
        stream = steady & (units > 0)
        not_heavy = np.ones(len(F), bool)
        for fr, _ in heavy_frames:
            not_heavy[pos[fr]] = False
        slow = (walls > BASE_MS + SLOW_MS) & not_heavy & (loading == 0)
        slow_pairs = int((slow[1:] & slow[:-1]).sum())
        ramp = (loading == 0) & (bvals > GOV["DEFAULT_TARGET_MS"] + 1e-9)
        drive_sl = slice(pos[drive0["frame"] + 1], pos[drive1["frame"]] + 1)
        p99 = float(np.percentile(spent[stream], 99)) if stream.any() else 0.0
        # (g) deletions of files the mod had loaded: the most in any 1 s (wall)
        dl = sorted(t for t, loaded in files["deleted"] if loaded)
        burst = max((int(np.searchsorted(dl, t + 1.0) - i) for i, t in enumerate(dl)), default=0)
        mat_left = {n: r for n, r in far.mats.items() if r or not modsim.pooled(n)}   # pooled ones stay
        late = far.counters["late_tiles"]
        mat_used = {i.mat for i in far.live.values() if i.mat}

        print(f"\n  speed-up {self.speedup:g} (max client lag {stats['max_lag_s']:.2f} s wall), rebase at "
              f"{REBASE_M:.0f} m, drove {km:.2f} km, {clock['frame']} frames ({clock['sim']:.0f} s simulated)\n"
              f"  initial far set {stats['initial']['tiles']} tiles, shown by sim {stats['initial']['sim']:.1f} s "
              f"({stats['initial']['wall']:.1f} s wall)\n"
              f"  terrain_ready x{len(lat_ms)}: wall ms min/median/max {min(lat_ms):.0f}/"
              f"{sorted(lat_ms)[len(lat_ms) // 2]:.0f}/{max(lat_ms):.0f}; min margin to the commit threshold "
              f"{min(t['margin_m'] for t in stats['terrain']):.0f} m; late {stats['late_terrain']} {stats.get('late_at', [])}\n"
              f"  follow commits {len(follows)} at {[round(x['dist']) for x in follows]} m from the old centre "
              f"(hard {stats['hard_commits']}, gated frames {stats['gated_frames']})\n"
              f"  (a) overlap samples per commit {sorted({x['samples'] for x in follows})}; max |dh| float64 "
              f"{max(x['d64'] for x in follows):.3g} m, float32 {max(x['d32'] for x in follows):.3g} m\n"
              f"  (b) far keys within +-{WINDOW_S / 2:g} s of a commit {[x['around'] for x in follows]}; "
              f"caused by the commit (old-block area, {WINDOW_S:g} s after) {sum(len(x['caused']) for x in follows)}; "
              f"selection diff without the old block {sum(x['select_diff'] for x in follows)}\n"
              f"  (c) {stats['coverage_checks']} coverage checks (with the N/E strip), hard holes "
              f"{len(stats['hard_holes'])}; holes at follow commits {[t['max_pts'] for t in fol_tr]}; "
              f"rebase gap {[t['max_pts'] for t in reb_tr]} points, closed after sim s "
              f"{[None if t['closed_sim_s'] is None else round(t['closed_sim_s'], 2) for t in reb_tr]}\n"
              f"  (d) {stats['tiles']} far tiles ({(drive1['tiles'] - drive0['tiles']) / km:.0f}/km while driving), "
              f"{stats['cdae_parsed']} parsed with cdae.read, bad {len(stats['cdae_bad'])}; tile field issues "
              f"{len(stats['tile_bad'])}; imports {far.counters['imports']}, failed {far.counters['failed']}; "
              f"shape files of loaded tiles checked {stats.get('files_checked', 0)}, vanished "
              f"{len(stats.get('vanished', []))}\n"
              f"  layout: {lay.cells} cells (format {lay.format}, exact level L{ex}); tile objects max "
              f"{stats['objects_max']}\n"
              f"  (e) governor (import {costs['cdae']:g} ms, base {BASE_MS:g} ms): max per frame "
              f"{int(imps.max())} imports / est {ests.max():.2f} ms / spent {spent.max():.2f} ms; driving: max "
              f"{int(imps[drive_sl].max())} imports, spent {spent[drive_sl].max():.2f} ms; steady streaming frames "
              f"{int(stream.sum())}, spend p99 {p99:.2f} ms; heavy slots {len(heavy_frames)}, isolation "
              f"violations {len(isolation)}; slow ({SLOW_MS:g} ms over base) adjacent pairs {slow_pairs}; "
              f"drive-off ramp from loading mode {int(ramp.sum())} frames, max spent "
              f"{spent[ramp].max() if ramp.any() else 0:.2f} ms\n"
              f"      textured import {', '.join(f'{k} {v:g}' for k, v in costs.items() if k.startswith('cdaeTex'))} ms"
              f"{' x (1 + %g (n - %d))' % (ALPHA, OBJECTS_N0) if scale else ''} / material {costs['material']:g} ms, "
              f"learnt estimates { {k: round(v, 2) for k, v in gov.est.items() if k in gov.used} }; max unit "
              f"{stats['max_unit_ms']:.2f} ms; frames over budget {len(stats['budget_over'])}\n"
              f"  far: crossovers {far.counters['crossovers']}, instant {far.counters['instant']}, pops "
              f"{far.counters['pops']}, cap waits {far.counters['cap_waits']}, deletes {far.counters['deletes']}\n"
              f"  (g) materials created {far.counters['materials']}, deleted {far.counters['mat_deletes']}, max "
              f"alive {stats['max_mats']}, at the end {len(mat_left)} for {len(far.live)} shown tiles; "
              f"bookkeeping issues {len(stats['mat_bad'])}; server deleted {len(files['deleted'])} files "
              f"({len(dl)} the mod had loaded, at most {burst} in 1 s)\n"
              f"  block {size}² (commit {commit_ms:g} ms, rebase {rebase_ms:g} ms): player min {stats['edge_min']:.0f} m "
              f"inside the block's collidable area {stats['edge_min_at']}; prepare_terrain x{stats.get('prepares', 0)} "
              f"({stats.get('late_prepares', 0)} past the commit distance: re-check re-prepares)\n"
              f"  rebase {stats['rebases']}, prepare_anchor x{stats['prepare_anchor']}; heightmaps built {hm_built} "
              f"for {len(stats['terrain'])} terrain-follow + {stats['prepare_anchor'] + 1} anchor requests "
              f"(never asked for: {unasked})\n"
              f"  late tiles (other anchor) {far.counters['late_tiles']}, superseded {stats['superseded_tiles']}, "
              f"unknown removes {len(far.unknown_removes)}, bad swaps {len(stats.get('bad_swaps', []))}, "
              f"server log issues {len(bad)}", file=sys.stderr)
        bl = np.array(stats["bilinear"]) if stats["bilinear"] else np.zeros(1)
        bb = np.array(stats.get("bilinear_bound") or [0.0])
        wall_p99 = float(np.percentile(walls[steady & not_heavy], 99)) if (steady & not_heavy).any() else 0.0
        if mesh:
            print(f"  (f) mesh: {stats['near_checks']} per-frame checks within {NEAR_HALF:g} m, holes "
                  f"{len(stats['near_holes'])}; poses without an L{ex} tile under the player "
                  f"{len(stats['under_not_l2'])} {stats['under_not_l2'][:4]}; L{ex} tiles checked against the block {stats['l2_checked']}, "
                  f"max |dz| float64 {stats['l2_d64']:.3g} m, float32 {stats['l2_d32']:.3g} m, bad "
                  f"{len(stats['l2_bad'])}; textures checked {stats['dds_checked']}, bad {len(stats['tex_bad'])}\n"
                  f"      mesh vs bilinear collision under the player: {len(stats['bilinear'])} points, median "
                  f"{100 * np.median(bl):.2f} cm, p99 {100 * np.percentile(bl, 99):.2f} cm, max {100 * bl.max():.2f} cm "
                  f"(the points' |twist|/4: p99 {100 * np.percentile(bb, 99):.2f} cm, max {100 * bb.max():.2f} cm), "
                  f"over |twist|/4 {len(stats['bilinear_over'])}; against the 1 m collision: p99 "
                  f"{100 * np.percentile(stats.get('collision_1m') or [0.0], 99):.2f} cm, max "
                  f"{100 * max(stats.get('collision_1m') or [0.0]):.2f} cm; steady frame wall p99 {wall_p99:.1f} ms "
                  f"(in-game M1 rule <= {P99_FRAME_MS_64:g})",
                  file=sys.stderr)

        # protocol and process
        self.assertFalse(stats["errors"], stats["errors"])
        self.assertFalse(bad, bad[:5])
        self.assertFalse(stats["terrain_bad"], stats["terrain_bad"][:2])
        self.assertFalse(stats["tile_bad"], stats["tile_bad"][:2])
        self.assertEqual(stats["late_terrain"], 0, "terrain_ready after the commit threshold")
        self.assertEqual(stats["rebases"], 1)
        self.assertEqual(stats["prepare_anchor"], 1, "one prepare_anchor per rebase")
        # the prefetch predicts the mod's requests: each terrain is built once, and only a straight-line
        # prediction that the bend or the final stop overtook is never asked for (512 blocks' 5 s lead since
        # 2026-10-15: a prediction further ahead, a few more of those; their 256 m minimum lead since 2026-10-23: up to
        # ~8 at this drive's bend, but 52 heightmaps built for 42 moves instead of 61 for 54)
        self.assertEqual(len(set(built)), hm_built, "a terrain was built twice")
        self.assertLessEqual(len(unasked), 9 if size * SQ < 2048 else 5, unasked)       # (1 km: the old 512² rule)
        self.assertEqual(stats["superseded_tiles"], 0)
        self.assertFalse(far.unknown_removes, far.unknown_removes[:5])
        self.assertEqual(far.counters["unknown_swap_keys"], 0)
        self.assertFalse(stats.get("bad_swaps"), stats.get("bad_swaps", [])[:2])
        for count, mod_count in stats["far_done"]:
            self.assertEqual(count, mod_count)
        self.assertEqual(len(far.live), stats["far_done"][-1][0])
        if not mesh:
            bb = lay.block_at(*st["block"]).bbox
            under = [k for k in far.live if ff.overlaps(tile_bbox(k), bb)]
            L0 = ff.level_size(0)       # the L0 cells the block touches (16 for the 1 km block on the 256 m grid)
            self.assertEqual(len(under), (math.ceil(bb[2] / L0) - math.floor(bb[0] / L0))
                             * (math.ceil(bb[3] / L0) - math.floor(bb[1] / L0)))
            self.assertTrue(all(ff.tile_of(k).level == 0 for k in under))
        self.assertFalse(far.old)
        # (a)
        # at 30 m/s a commit moves the block 512 m (follow.lua lookahead), so >= one per 600 m (512²: the next block
        # at least MIN_LEAD (256 m) ahead and asked for once the player is past the centre, 2026-10-23: ~384 m per
        # commit, so >= one per 400 m)
        self.assertGreaterEqual(len(follows), int(REBASE_M / (600 if size * SQ >= 2048 else 400)))   # (by width)
        # M4: the player never came within 150 m of the committed block's collidable edge
        self.assertGreaterEqual(stats["edge_min"], 150.0, stats["edge_min_at"])
        for x in follows:
            self.assertGreater(x["samples"], 0)
            self.assertEqual((x["d64"], x["d32"]), (0.0, 0.0), x)
        # (b)
        for x in follows:
            if not mesh:                # mesh mode: the area behind the player coarsens anyway
                self.assertFalse(x["caused"], x)
            self.assertEqual(x["select_diff"], 0, x)
        # (c)
        self.assertFalse(stats["hard_holes"], stats["hard_holes"][:2])
        for t in fol_tr:
            self.assertEqual(t["max_pts"], 0, t)
        for t in reb_tr:
            self.assertIsNotNone(t["closed_sim_s"], t)
            self.assertLessEqual(t["closed_sim_s"], TRANSIENT_MAX_S, t)
        self.assertEqual(stats["hard_commits"], 0, "a commit had to override the gating at 700 m")
        if mesh:
            self.assertEqual(stats["gated_frames"], 0, "mesh mode never gates a commit on far tiles")
            self.assertGreater(stats["near_checks"], 1000)
            self.assertFalse(stats["near_holes"], stats["near_holes"][:2])
        # (d)
        self.assertGreater(stats["cdae_parsed"], 100)
        self.assertFalse(stats["cdae_bad"], stats["cdae_bad"][:2])
        self.assertEqual(far.counters["failed"], 0, "a shape file was gone when the mod imported it")
        self.assertFalse(stats.get("vanished"), "a shape file was deleted under a live TSStatic")
        if mesh:
            self.assertGreater(stats["dds_checked"], 50)
            self.assertFalse(stats["tex_bad"], stats["tex_bad"][:2])
            self.assertEqual(far.counters["textured"], stats["tiles"] - late)      # all but the dropped ones
        # (e)
        self.assertFalse(isolation, isolation[:3])
        self.assertFalse(stats["budget_over"], stats["budget_over"][:3])
        if self.cells == 32:
            self.assertLessEqual(p99, SLOW_MS)
            self.assertEqual(slow_pairs, 0)
        else:
            unit_max = max(v for k, v in costs.items() if k.startswith("cdaeTex")) * scale(stats["objects_max"])
            self.assertLessEqual(p99, GOV["DEFAULT_TARGET_MS"] + unit_max)
            self.assertLessEqual(slow_pairs, SLOW_PAIRS_64 * max(1, int(stream.sum())))
        self.assertEqual(far.counters["pops"], 0)
        # (f)
        if mesh:
            self.assertFalse(stats["under_not_l2"], stats["under_not_l2"][:3])
            self.assertGreater(stats["l2_checked"], 100)
            self.assertFalse(stats["l2_bad"], stats["l2_bad"][:3])
            self.assertGreaterEqual(len(stats["bilinear"]), BILINEAR_MIN_POINTS)
            self.assertFalse(stats["bilinear_over"], stats["bilinear_over"][:3])
            self.assertLessEqual(float(np.percentile(bl, 99)), max(BILINEAR_P99_M, float(np.percentile(bb, 99))))
            x, y = player["p"][:2]
            close = [k for k in far.live if ff.tile_of(k).distance(x, y) < lay.near_radii[ex]]
            self.assertTrue(close)
            self.assertTrue(all(ff.tile_of(k).level == ex for k in close),
                            sorted({ff.tile_of(k).level for k in close}))
        # (g)
        self.assertFalse(stats["mat_bad"], stats["mat_bad"][:3])
        self.assertFalse(far.mat_conflicts, far.mat_conflicts[:3])     # a pooled name never on two tiles at once
        self.assertEqual(set(mat_left), mat_used)               # nothing left over, nothing missing
        self.assertTrue(all(r > 0 for r in mat_left.values()))
        self.assertTrue(dl, "no prune of the previous anchor's files was observed")
        if mesh:        # pooled names: Materials re-used, never deleted, fewer made than tiles shown over the run
            self.assertEqual(far.counters["mat_deletes"], 0)
            self.assertLess(far.counters["materials"], stats["tiles"])
        else:
            self.assertEqual((far.counters["materials"], len(mat_left)), (0, 0))
        self.assertLessEqual(burst, DELETES_PER_S[self.near_visual])


@unittest.skipIf(os.environ.get("BNGE_SKIP_DRIVE") == "1", "BNGE_SKIP_DRIVE=1")
@unittest.skipUnless("terrain" in MODES, "BNGE_DRIVE_MODES")
class TerrainDriveTest(DriveBase):
    """The v1.2 path: --near-visual terrain --imagery off."""

    def test_drive_and_rebase(self):
        self.run_drive()


@unittest.skipIf(os.environ.get("BNGE_SKIP_DRIVE") == "1", "BNGE_SKIP_DRIVE=1")
@unittest.skipUnless("mesh" in MODES, "BNGE_DRIVE_MODES")
class MeshDriveTest(DriveBase):
    """The v1.3 layout: --near-visual mesh with imagery (offline fake source), --cells 32 (the
    command line defaults to 64 cells in mesh mode since P8: MeshDrive64Test)."""
    near_visual = "mesh"
    server_args = ("--near-visual", "mesh", "--cells", "32", "--block", "1024", "--no-horizon", "--colormatch", "off")
    launcher = "fake_imagery_earthd.py"
    block = 1024

    def test_drive_and_rebase(self):
        self.run_drive()


@unittest.skipIf(os.environ.get("BNGE_SKIP_DRIVE") == "1", "BNGE_SKIP_DRIVE=1")
@unittest.skipUnless("mesh64" in MODES, "BNGE_DRIVE_MODES")
class MeshDrive64Test(DriveBase):
    """docs/V1_4_SPEC.md §3.1 (M1): the mesh drive with --cells 64 (FORMAT 4, L-1 exact, 8704 tris,
    far_tile.tex) and the per-kind, per-object-count cost model. (Not a MeshDriveTest subclass:
    it would inherit that class's skip flag.)"""
    near_visual = "mesh"
    server_args = ("--near-visual", "mesh", "--cells", "64", "--block", "1024", "--no-horizon", "--colormatch", "off")
    launcher = "fake_imagery_earthd.py"
    block = 1024
    cells = 64

    def test_drive_and_rebase(self):
        self.run_drive()


@unittest.skipIf(os.environ.get("BNGE_SKIP_DRIVE") == "1", "BNGE_SKIP_DRIVE=1")
@unittest.skipUnless("mesh64b2048" in MODES, "BNGE_DRIVE_MODES")
class MeshDrive64Block2048Test(DriveBase):
    """The 64-cell mesh drive with the other block size, --block 2048 (the 2 km block at 1 m since 2026-11-01; it
    was docs/V1_4_SPEC.md §3.5 M4's --block 512, the 1 km block at 2 m, now the default): hello.blockSize /
    squareSize, the 2048² physics block on a 256 m grid with the mod's 2 km (_1024) follow table, assertion (a)
    for its blocks (overlapping samples bit-identical across every move), (f) against its heightmap, the player
    >= 150 m inside the block's collidable area at every frame."""
    near_visual = "mesh"
    server_args = ("--near-visual", "mesh", "--cells", "64", "--block", "2048", "--no-horizon", "--colormatch", "off")
    launcher = "fake_imagery_earthd.py"
    cells = 64
    block = 2048

    def test_drive_and_rebase(self):
        self.run_drive()


class _NoFarConnection(earthd.Connection):
    """A server connection without the far field (no tile selection or streaming), for the
    terrain-only follow drives; `anchor_delay` / `terrain_delay` (s) hold back the server's
    anchor / terrain answers like a cold DEM cache."""
    anchor_delay = 0.0

    def update_far(self):
        pass

    async def prepare_anchor(self, msg):
        if self.anchor_delay:
            await asyncio.sleep(self.anchor_delay)
        await super().prepare_anchor(msg)


# Messages the server sends on every pose however settled it is (the altitude rule's ground_ahead,
# twice a second): "quiet" means no other message for a second.
PERIODIC = {"ground_ahead"}


def busy_msgs(log):
    return sum(1 for m in log if m.get("type") not in PERIODIC)


class _SlowServer(earthd.Server):
    """make_heightmap takes `terrain_delay` s longer (a cold DEM: every request and prefetch)."""
    terrain_delay = 0.0

    def make_heightmap(self, anchor, cx, cy, progress, *args):
        if self.terrain_delay:
            time.sleep(self.terrain_delay)
        return super().make_heightmap(anchor, cx, cy, progress, *args)


@unittest.skipIf(os.environ.get("BNGE_SKIP_DRIVE") == "1", "BNGE_SKIP_DRIVE=1")
@unittest.skipUnless("follow512" in MODES, "BNGE_DRIVE_MODES")
class FollowDrive512Test(unittest.TestCase):
    """docs/V1_4_SPEC.md §3.5 (M4) at speed: an in-process server with the 1 km block (1024² at 1 m since
    2026-11-01, M4's 512² at 2 m before; synthetic DEM, no
    far field) answers the mod's terrain-follow and rebase requests, in real time, while the client
    replays the mod's rules (modsim: follow.lua with the 1 km (_512) table, fast mode and re-check, measured
    latencies passed back; rebase.lua with the stale-anchor rule and core.lua's rebaseHint) at 60
    frames/s on a straight route, rebasing at 2 km. Every frame the player must be on the committed
    block's collidable area:
      - 134 m/s, heading 20 deg: no late terrain (every request made at or inside the commit
        distance is ready before the player passes it), never within 150 m of an edge;
      - 200 m/s, heading 10 deg, +1.2 s heightmap latency (fast mode): on the block, >= 90 m
        inside (the fast mode's margin);
      - 250 m/s, heading 0 deg, anchor latency 3 s and 5 s: the rebase happens (no discard loop;
        5 s: the first answer is stale and discarded, the re-request lands where the player is)."""
    REBASE_DIST = 2000.0
    DT = 1 / 60

    def run_follow(self, speed, hdg, distance, terrain_delay=0.0, anchor_delay=0.0):
        import test_server
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        server = _SlowServer(tmp.name, os.path.join(tmp.name, "cache"), tile_workers=1, imagery=None,
                             near_visual="mesh", cells=64, block=1024)
        server.dem = test_server.FakeDem()
        server.terrain_delay = terrain_delay
        self.addCleanup(server.hm_pool.shutdown)
        self.addCleanup(server.tile_pool.shutdown)
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()

        def conn(r, w):
            c = _NoFarConnection(server, r, w)
            c.anchor_delay = anchor_delay
            return c.run()

        srv = asyncio.run_coroutine_threadsafe(asyncio.start_server(conn, "127.0.0.1", 0), loop).result(10)
        client = Client(srv.sockets[0].getsockname()[1])

        def stop():
            client.close()

            async def close():
                srv.close()
                tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            asyncio.run_coroutine_threadsafe(close(), loop).result(10)
            loop.call_soon_threadsafe(loop.stop)
            thread.join(10)
            loop.close()
        self.addCleanup(stop)

        client.send({"type": "hello", "protocol": int(CORE["PROTOCOL"]), "format": int(CORE["FORMAT"]),
                     "modVersion": MOD_VERSION})
        h, _ = client.until(lambda m: m["type"] == "hello", 10)
        self.assertEqual((h["blockSize"], h["squareSize"]), (1024, SQ))
        client.send({"type": "prepare_anchor", "requestId": 1, "lat": LAT, "lon": LON, "cx": 0, "cy": 0})
        a, _ = client.until(lambda m: m["type"] == "anchor_ready", 60)
        aid, frame = a["anchorId"], geodesy.EnuFrame(LAT, LON)
        client.send({"type": "commit_anchor", "anchorId": aid})
        client.send({"type": "commit_terrain", "anchorId": aid, "cx": 0, "cy": 0})
        size, T = 1024, modsim.follow_table(1024)
        st = {"block": (a["terrain"]["cx"], a["terrain"]["cy"]), "tp": None, "pending": None, "next": 2,
              "t_lat": None, "a_lat": None}
        out = {"edge_min": math.inf, "edge_at": None, "late": 0, "commits": 0, "rebases": 0, "prepare_anchor": 0,
               "anchor_discards": 0, "prepares": 0, "fast_frames": 0, "frames": 0}
        a_hd = math.radians(hdg)
        vx, vy = speed * math.cos(a_hd), speed * math.sin(a_hd)
        x = y = 0.0
        driven = clock = 0.0
        wall0 = time.monotonic()

        def handle(m):
            t = m["type"]
            self.assertNotIn(t, ("anchor_error", "terrain_error"), m)
            if t == "terrain_ready":
                p = st["tp"]
                if p and m["requestId"] == p["requestId"] and m["anchorId"] == aid:
                    self.assertEqual((m["size"], m["cx"] % 128, m["cy"] % 128), (1024, 0, 0))
                    p["ready"], p["msg"] = True, m
                    st["t_lat"] = clock - p["sent"]
                    if p["on_time"] and cheb(x, y, *st["block"]) > T["COMMIT_DIST"]:
                        out["late"] += 1
            elif t == "anchor_ready":
                p = st["pending"]
                if p and m["requestId"] == p["requestId"]:
                    self.assertEqual(m["terrain"]["size"], 1024)
                    p["ready"], p["msg"] = True, m
                    st["a_lat"] = clock - p["sent"]

        while driven < distance:
            out["frames"] += 1
            clock += self.DT
            while True:                         # real time: the server's latency is the real one
                try:
                    handle(client.recv(max(0.0005, wall0 + clock - time.monotonic())))
                except TimeoutError:
                    break
                if time.monotonic() >= wall0 + clock:
                    break
            x, y = x + vx * self.DT, y + vy * self.DT
            driven += speed * self.DT
            out["fast_frames"] += modsim.follow_mode(vx, vy, size)[2] > 0
            # origin rebase first (core.lua updateWorld)
            p = st["pending"]
            stale = edge = None
            if p and p.get("ready"):
                m = p["msg"]
                nf = geodesy.EnuFrame(m["lat"], m["lon"])
                nx, ny, _ = to_frame(frame, nf, x, y, 0.0)
                stale = cheb(nx, ny, m["terrain"]["cx"], m["terrain"]["cy"])
                gx0, gy0, gx1, gy1 = modsim.ground_box(m["terrain"]["cx"], m["terrain"]["cy"], size)
                edge = min(nx - gx0, gx1 - nx, ny - gy0, gy1 - ny)
            act = modsim.rebase_decide(math.hypot(x, y), p, stale, self.REBASE_DIST, edge=edge, size=size)
            if act == "prefetch":
                lat, lon, _ = frame.enu_to_geodetic(x, y, 0.0)
                cx, cy = modsim.rebase_hint(vx, vy, math.hypot(x, y), self.REBASE_DIST, size, st["a_lat"])
                st["pending"] = {"requestId": st["next"], "purpose": "rebase", "sent": clock}
                st["next"] += 1
                out["prepare_anchor"] += 1
                client.send({"type": "prepare_anchor", "requestId": st["pending"]["requestId"], "lat": float(lat),
                             "lon": float(lon), "cx": cx, "cy": cy})
            elif act == "commit":
                m = st["pending"]["msg"]
                nf = geodesy.EnuFrame(m["lat"], m["lon"])
                ex, ey, _ = to_frame(frame, nf, x + vx, y + vy, 0.0)
                x, y, _ = to_frame(frame, nf, x, y, 0.0)
                vx, vy = ex - x, ey - y
                frame, aid = nf, m["anchorId"]
                st["block"], st["tp"], st["pending"] = (m["terrain"]["cx"], m["terrain"]["cy"]), None, None
                client.send({"type": "commit_anchor", "anchorId": aid})
                client.send({"type": "commit_terrain", "anchorId": aid, "cx": st["block"][0], "cy": st["block"][1]})
                out["rebases"] += 1
            elif act == "discard":
                st["pending"] = None
                out["anchor_discards"] += 1
            if act != "commit":
                # terrain-follow (mesh mode: no commit gating)
                b, tp = st["block"], st["tp"]
                fa = modsim.follow_decide(x, y, vx, vy, b[0], b[1], tp, clock, 0.0, None, st["t_lat"], size)
                if fa and fa[0] == "prepare":
                    st["tp"] = {"requestId": st["next"], "cx": fa[1], "cy": fa[2], "sent": clock,
                                "on_time": cheb(x, y, *b) <= T["COMMIT_DIST"]}
                    st["next"] += 1
                    out["prepares"] += 1
                    client.send({"type": "prepare_terrain", "requestId": st["tp"]["requestId"], "anchorId": aid,
                                 "cx": fa[1], "cy": fa[2]})
                elif fa and fa[0] == "commit":
                    st["block"], st["tp"] = (tp["cx"], tp["cy"]), None
                    out["commits"] += 1
                    client.send({"type": "commit_terrain", "anchorId": aid, "cx": tp["cx"], "cy": tp["cy"]})
                elif fa and fa[0] == "discard":
                    st["tp"] = None
            if out["frames"] % 30 == 0:         # poses at 2 Hz: the server's prefetch
                lat, lon, _ = frame.enu_to_geodetic(x, y, 0.0)
                client.send({"type": "pose", "anchorId": aid, "x": x, "y": y, "z": 0.0, "vx": vx, "vy": vy,
                             "lat": float(lat), "lon": float(lon), "speed": speed})
            gx0, gy0, gx1, gy1 = modsim.ground_box(*st["block"], size)
            e = min(x - gx0, gx1 - x, y - gy0, gy1 - y)
            if e < out["edge_min"]:
                out["edge_min"], out["edge_at"] = e, (round(clock, 2), round(x), round(y), st["block"])
        out["lag_s"] = time.monotonic() - (wall0 + clock)
        out["t_lat"], out["a_lat"] = st["t_lat"], st["a_lat"]
        print(f"\n  1 km follow drive {speed:g} m/s at {hdg:g} deg (+{terrain_delay:g} s heightmap, +{anchor_delay:g} s "
              f"anchor): {out}", file=sys.stderr)
        self.assertLess(out["lag_s"], 1.0, "the client could not keep real time")
        return out

    def test_134_no_late_terrain(self):
        r = self.run_follow(134.0, 20.0, 3500.0)
        self.assertEqual(r["late"], 0)
        self.assertGreaterEqual(r["edge_min"], 150.0, r["edge_at"])
        self.assertEqual((r["fast_frames"], r["rebases"]), (0, 1))
        self.assertGreater(r["commits"], 5)

    def test_200_fast_mode_slow_heightmaps(self):
        r = self.run_follow(200.0, 10.0, 3500.0, terrain_delay=1.2)
        self.assertGreaterEqual(r["edge_min"], modsim.FOLLOW["EDGE_MARGIN_FAST"], r["edge_at"])
        self.assertEqual(r["fast_frames"], r["frames"])
        self.assertEqual(r["rebases"], 1)

    def test_250_slow_anchor_rebases(self):
        for delay, dist in ((3.0, 3500.0), (5.0, 4600.0)):
            r = self.run_follow(250.0, 0.0, dist, anchor_delay=delay)
            self.assertEqual(r["rebases"], 1, (delay, r))
            self.assertLessEqual(r["prepare_anchor"], 2, (delay, r))
            self.assertEqual(r["anchor_discards"], 0 if delay == 3.0 else 1, (delay, r))
            self.assertGreater(r["edge_min"], 0.0, r["edge_at"])


if __name__ == "__main__":
    unittest.main()
