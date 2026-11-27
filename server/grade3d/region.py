"""Step 9 of the 3D solve: the solve in the build, what _Planner._shape_region gives a map cell.

    cell_profiles(planner, home) -> {fid: [(xy, z, mode)]}     a cell's heights (outputs' form), blended from the blocks
    block(planner, bx, by) -> Block                             one block of the planning frame's grid, solved once

The planning frame is cut into square cores of BLOCK_M on a grid fixed in the frame (block (bx, by): bx * BLOCK_M ..), so
a block is the same whichever cell asks for it. A block reads every member over its core and RAMP_M + MARGIN_M round it
(and READ_PAD_M more, so the lines it solves are whole past its reach), solves that alone (steps 1 to 7 on its own line
network; its paths cut free MARGIN_M past where its heights are used) and keeps its heights as outputs' pieces.

A cell's heights: its members (over the cell and CELL_PAD_M round it) as pieces (outputs.pieces of its own sampling),
each point's height the blend of the blocks whose core it is within RAMP_M of (solve.blend_weight: weights summing to 1),
each block's height there read off that block's piece of the same feature and mode running the same way (DIR_COS) within
NEAR_M. Two cells, and two blocks across a seam, give a point one height."""
from dataclasses import dataclass
import logging
import threading
import time

import numpy as np

import gradeshapes as _gsh
import linenet as _linenet
from . import ground, laws, layers, network, outputs, problem, samples, solve

log = logging.getLogger("earthd.grading")

BLOCK_M = solve.BLOCK_M
RAMP_M = solve.RAMP_M
MARGIN_M = solve.MARGIN_M
READ_PAD_M = 300.0          # m of members read past a block's reach
CELL_PAD_M = 150.0          # m round a cell whose members are its pieces (a plan reaches past its cell's edge)
NEAR_M = 3.0                # m: a block's piece this near a point gives its height there (grading.SHAPES_NEAR_M)
DIR_COS = 0.5               # ... running within 60 deg of the point's line (a road over itself is not its own height)
KEPT = 128                  # blocks a planner keeps


@dataclass
class Block:
    """A solved block: core (lo, hi); prof {(fid, mode): (a (m, 2), b (m, 2), za (m,), zb (m,))} the segments of its
    pieces where it solved them; seconds; diagnostics (the solve's)."""
    core: tuple
    prof: dict
    seconds: float
    diagnostics: dict


def touches(line, lo, hi):
    """Whether a segment of line (k, 2) has its box over the box lo..hi."""
    line = np.asarray(line, np.float64)[:, :2]
    if len(line) == 1:
        return bool(((line[0] >= lo) & (line[0] <= hi)).all())
    a, b = line[:-1], line[1:]
    return bool(((np.minimum(a, b) <= hi) & (np.maximum(a, b) >= lo)).all(1).any())


def members_in(planner, lo, hi):
    """The members over the box lo..hi (frame m): the map cells' there, each key's longest line (ix.s1_paths.load), those
    with a segment over the box, a road's lane sections in place of its unsplit copy (gradeshapes.one_road)."""
    from ix import s1_paths
    lo, hi = np.asarray(lo, np.float64), np.asarray(hi, np.float64)
    byk, loaded = {}, set()
    s1_paths.load(planner, lo, hi, byk, loaded)
    return _gsh.one_road([byk[q] for q in sorted(byk, key=str) if touches(byk[q].line, lo, hi)])


def _segments(pcs_prof):
    """{(fid, mode): (a, b, za, zb)} of {fid: [(xy, z, mode)]}, the segments of the finite points only."""
    acc = {}
    for fid, lst in pcs_prof.items():
        for xy, z, mode in lst:
            ok = np.isfinite(z)
            seg = ok[:-1] & ok[1:]
            if not seg.any():
                continue
            k = np.flatnonzero(seg)
            acc.setdefault((fid, mode), []).append((xy[k], xy[k + 1], z[k], z[k + 1]))
    return {key: tuple(np.concatenate(c) for c in zip(*v)) for key, v in acc.items()}


def solve_block(planner, bx, by):
    """Block (bx, by) of the planner's frame, solved (not kept: block() keeps it)."""
    t0 = time.perf_counter()
    c0 = np.array([bx, by], np.float64) * BLOCK_M
    c1 = c0 + BLOCK_M
    reach = RAMP_M + MARGIN_M
    ms = members_in(planner, c0 - reach - READ_PAD_M, c1 + reach + READ_PAD_M)
    if not ms:
        return Block((c0, c1), {}, time.perf_counter() - t0, {})
    net = network.build(ms, _linenet.Network())
    sm = samples.sample(net, layers.order(net))
    g = ground.smooth(sm, ground.read(ground.planner_elev(planner), sm))
    pr = problem.pose(net, sm, laws.lay(net, sm, g))
    b = solve.block_of(pr, sm, c0, c1, reach)
    z = np.full(len(sm), np.nan)
    d = {}
    if b is not None:
        zb, d, _s = solve.solve_block(pr, b)
        z[b.idx] = zb
    prof = outputs.profiles(net, sm, z)
    return Block((c0, c1), _segments(prof), time.perf_counter() - t0, {k: d.get(k) for k in ("status", "rows")})


def block(planner, bx, by):
    """Block (bx, by), solved once per planner (the first asking thread solves it, the others wait for it)."""
    with planner.lock:
        memo = planner.__dict__.setdefault("_grade3d_blocks", {})
        locks = planner.__dict__.setdefault("_grade3d_block_locks", {})
        got = memo.get((bx, by))
        if got is not None:
            return got
        lk = locks.setdefault((bx, by), threading.RLock())
    with lk:
        with planner.lock:
            got = memo.get((bx, by))
        if got is not None:
            return got
        got = solve_block(planner, bx, by)
        log.info("grading: 3D solve block (%d, %d): %.1f s, %s", bx, by, got.seconds, got.diagnostics.get("status"))
        with planner.lock:
            if len(memo) >= KEPT:
                memo.clear()
            memo[(bx, by)] = got
        return got


def z_at(blk, fid, mode, pts, tan):
    """The height of block blk's piece of (fid, mode) at the points pts (n, 2) whose line runs along tan (n, 2): its
    segment nearest each within NEAR_M running the same way; NaN where none is."""
    out = np.full(len(pts), np.nan)
    got = blk.prof.get((fid, mode))
    if got is None or not len(pts):
        return out
    a, b, za, zb = got
    ab = b - a
    L2 = np.maximum((ab * ab).sum(1), 1e-12)
    u = ab / np.sqrt(L2)[:, None]
    for c0 in range(0, len(pts), 256):
        q = pts[c0:c0 + 256, None, :]
        t = np.clip(((q - a) * ab).sum(2) / L2, 0.0, 1.0)
        d = np.hypot(*(a + t[..., None] * ab - q).transpose(2, 0, 1))
        same = np.abs(tan[c0:c0 + 256] @ u.T) >= DIR_COS
        d = np.where(same, d, np.inf)
        j = np.argmin(d, axis=1)
        r = np.arange(len(j))
        ok = d[r, j] <= NEAR_M
        zz = za[j] + (zb[j] - za[j]) * t[r, j]
        out[c0:c0 + len(j)] = np.where(ok, zz, np.nan)
    return out


def _tangents(xy):
    if len(xy) < 2:
        return np.zeros_like(xy)
    d = np.gradient(xy, axis=0)
    n = np.hypot(*d.T)
    return d / np.where(n > 0, n, 1.0)[:, None]


def blocks_over(lo, hi):
    """[(bx, by)] of the blocks whose core is within RAMP_M of the box lo..hi."""
    a = np.floor((np.asarray(lo) - RAMP_M) / BLOCK_M).astype(int)
    b = np.floor((np.asarray(hi) + RAMP_M) / BLOCK_M).astype(int)
    return [(x, y) for x in range(a[0], b[0] + 1) for y in range(a[1], b[1] + 1)]


def cell_box(planner, home):
    """(lo, hi) of map cell home in the planner's frame."""
    import mapcells
    w, s, e, n = (home if isinstance(home, mapcells.CellKey) else mapcells.parse(str(home))).bounds()
    x, y, _ = planner.frame.geodetic_to_enu(np.array([s, s, n, n], np.float64), np.array([w, e, w, e], np.float64), 0.0)
    xy = np.stack([np.asarray(x, np.float64), np.asarray(y, np.float64)], axis=1)
    return xy.min(0), xy.max(0)


def cell_profiles(planner, home):
    """{fid: [(xy, z, mode)]} of map cell home: its members' pieces, their heights the blocks' blended."""
    lo, hi = cell_box(planner, home)
    ms = members_in(planner, lo - CELL_PAD_M, hi + CELL_PAD_M)
    if not ms:
        return {}
    net = network.build(ms, _linenet.Network())
    sm = samples.sample(net)
    pcs = outputs.pieces(net, sm, np.zeros(len(sm)))
    if not pcs:
        return {}
    allxy = np.concatenate([xy for _i, xy, _z in pcs])
    need = blocks_over(allxy.min(0), allxy.max(0))
    if len(need) > 1:           # (the blocks no cell has asked for yet, side by side: the kernel releases the GIL)
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(min(len(need), solve.WORKERS)) as ex:
            got = dict(zip(need, ex.map(lambda k: block(planner, *k), need)))
    else:
        got = {k: block(planner, *k) for k in need}
    prof = {}
    for i, xy, _z in pcs:
        m = net.members[i]
        fid, mode = outputs.fid_of(m), outputs.MODE[m.kind]
        tan = _tangents(xy)
        num, den = np.zeros(len(xy)), np.zeros(len(xy))
        for blk in got.values():
            w = solve.blend_weight(xy, blk.core, RAMP_M)
            if not (w > 0).any():
                continue
            zb = z_at(blk, fid, mode, xy, tan)
            ok = (w > 0) & np.isfinite(zb)
            num[ok] += w[ok] * zb[ok]
            den[ok] += w[ok]
        z = np.where(den > 0, num / np.where(den > 0, den, 1.0), np.nan)
        ok = np.isfinite(z)
        if not ok.any():
            continue
        if not ok.all():            # (a point no block gave a height: its piece's neighbours', along it)
            s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
            z = np.interp(s, s[ok], z[ok])
        prof.setdefault(fid, []).append((xy, z, mode))
    return prof
