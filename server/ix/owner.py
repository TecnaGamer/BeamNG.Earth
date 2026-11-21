"""One interchange, one owner (BNE_SHAPES): a region's box cuts a large interchange differently from every other region's,
and every region and process solved the pieces it found (one system of about 1,300 pieces solved 141 times over 87
regions, a build of 729 s taking 15025 s). Now the region's piece is only the way in:

- grow() follows it, through its joins and crossings, to the whole interchange whatever box it is found in, so every
  region reaches the same members and the same key (gradeshapes.interchange_key). An interchange too wide to follow
  whole (a city's elevated roads, joined end to end for tens of kilometres: one system of thousands of pieces and
  crossings, more than the kernel can hold) is followed as far as the tile of the asking cell (TILE_UP levels over it),
  the same box for every cell of that tile, and its key names the tile;
- the cell holding the interchange's centre is its owner (owner_cell); its solution is made once, by the process that
  takes the key's lock (plancache.PlanCache.shape_claim), and every other thread and process reaching the key waits for
  it and reads it. A holder that is gone hands the lock to the next waiter: there is no wait after which an interchange is
  solved beside its holder, and a holder that is abandoned (a place a teleport left) gives the lock back.

The solution is stored as lat / lon and heights (no planner's frame in it) for the run's other processes
(PlanCache.shape_store), and packed the same way in this process (PROCESS) for the other planners', so a process with no
run store (the plan cache off) still solves an interchange once."""
import logging
import os
import threading
import time
from collections import OrderedDict

import numpy as np

import gradeshapes as _gsh

from . import s1_paths, s2_extent

log = logging.getLogger("earthd.grading")

BOX_DEG = 0.0003            # deg (about 30 m): how far outside its box (in the index of interchanges) a road's point may be
#                             and still be of it
LOCK = threading.Lock()     # (the tables below: short holds)
FLIGHTS = {}                # (the extents and solutions this process is asking for now: once)
PROCESS = OrderedDict()     # interchange key -> its solution as stored (pack): this process's, every planner's. Made short of
#                             something it is not in the run's store; with no run store it is the only one
INDEX = {}                  # road canonical id -> [(interchange key, lat / lon box of its seeds)]: this process's
KEPT = 512                  # solutions kept (per planner, per process)
TILE_UP = 2                 # map zoom levels over a cell: the tile (4 x 4 cells, about 8 km) an interchange is followed to at most
FIELDS = ("key", "failed", "pieces", "crossings", "owner", "pid", "secs")


def version():
    """What makes a stored solve stale: the plans' format and the settings that change them (plancache.plans_format)."""
    import plancache
    return plancache.plans_format()


def once(planner, kind, name, have, work):
    """(answer, made here): have() (the answer or None) when there is one, else work() made by this process alone of the
    run: one thread of this process asks for the claim (the process's other threads wait for it), then the claim is the
    run's (PlanCache.shape_claim: have() read while another process makes it, as long as it does). work stores its result
    itself, before the claim is given back (also when it raises: pools.Abandoned of a place a teleport left, the next
    waiter makes it)."""
    sh = planner.shared
    shared = sh is not None and hasattr(sh, "shape_claim")
    flight = (kind, name)
    while True:
        got = have()
        if got is not None:
            return got, False
        with LOCK:
            ev = FLIGHTS.get(flight)
            mine = ev is None
            if mine:
                ev = FLIGHTS[flight] = threading.Event()
        if not mine:
            ev.wait()
            continue
        try:
            if not shared:
                return work(), True
            got, _owner = sh.shape_claim(kind, name, have)
            if got is not None:
                return got, False
            try:
                return work(), True
            finally:
                sh.shape_release(kind, name)
        finally:
            with LOCK:
                FLIGHTS.pop(flight, None)
            ev.set()


# ---- the whole interchange ----

def centre(planner, net, sysm):
    """(lat, lon) of the centre of the interchange sysm is (the whole one: grow)."""
    c = _gsh.system_centre(net, sysm)
    la, lo, _ = planner.frame.enu_to_geodetic(np.array([c[0]]), np.array([c[1]]), 0.0)
    return float(la[0]), float(lo[0])


def owner_cell(planner, net, sysm):
    """The cell that owns the interchange sysm is (the whole one: grow): the one holding its centre."""
    import mapcells
    return mapcells.cell_of(*centre(planner, net, sysm))


def tile_of(home):
    """The tile of cell home: the cell TILE_UP zoom levels over it."""
    import mapcells
    return mapcells.CellKey(home.z - TILE_UP, home.x >> TILE_UP, home.y >> TILE_UP)


def tile_box(planner, home):
    """(lo, hi) of the tile of cell home in the planner's frame (ENU), SHAPES_PAD_M round it: what an interchange is
    followed within when it is too wide to follow whole."""
    import grading
    lon0, lat0, lon1, lat1 = (float(v) for v in tile_of(home).bounds())
    e, n, _ = planner.frame.geodetic_to_enu(np.array([lat0, lat0, lat1, lat1]), np.array([lon0, lon1, lon0, lon1]), 0.0)
    return (np.array([np.min(e), np.min(n)]) - grading.SHAPES_PAD_M,
            np.array([np.max(e), np.max(n)]) + grading.SHAPES_PAD_M)


def pick(entries, pt, tile=""):
    """The key of the entry [(key, (lat0, lon0, lat1, lon1), cut)] whose box holds pt (lat, lon) within BOX_DEG (the smallest
    box where several do), else None; an entry bounded by a tile (cut) only for that tile (a string)."""
    best = None
    for ik, (a, b, c, d), cut in entries:
        if cut not in ("", "-", tile):
            continue
        if a - BOX_DEG <= pt[0] <= c + BOX_DEG and b - BOX_DEG <= pt[1] <= d + BOX_DEG:
            area = (c - a) * (d - b)
            if best is None or area < best[0]:
                best = (area, ik)
    return None if best is None else best[1]


def known_key(planner, cid, pt, tile=""):
    """The key of the interchange the road cid (a canonical id) at pt (lat, lon) is part of (for tile: the cell's), when
    this process or the run's index knows it, else None."""
    with LOCK:
        ik = pick(INDEX.get(cid, ()), pt, tile)
    sh = planner.shared
    if ik is None and sh is not None and hasattr(sh, "shape_index"):
        ents = sh.shape_index(cid)
        if ents:
            with LOCK:
                INDEX[cid] = ents
            ik = pick(ents, pt, tile)
    return ik


def probe(planner, net, sysm, cid):
    """(lat, lon) of a point on the first seed of the system that is road cid: what tells which of a road's interchanges it
    is (the index's boxes)."""
    i = next(i for i in sysm["seeds"] if _gsh.canonical_id(net.m[i]) == cid)
    p = net.lines[i][len(net.lines[i]) // 2]
    la, lo, _ = planner.frame.enu_to_geodetic(np.array([p[0]]), np.array([p[1]]), 0.0)
    return float(la[0]), float(lo[0])


def index_add(planner, ik, net, sysm, cut=""):
    """Interchange ik's seeds' roads into the process's index and the run's: each road's box over its seeds (lat, lon)."""
    boxes = {}
    for i in sysm["seeds"]:
        la, lo, _ = planner.frame.enu_to_geodetic(net.lines[i][:, 0], net.lines[i][:, 1], 0.0)
        b = (float(np.min(la)), float(np.min(lo)), float(np.max(la)), float(np.max(lo)))
        cid = _gsh.canonical_id(net.m[i])
        old = boxes.get(cid)
        boxes[cid] = b if old is None else (min(old[0], b[0]), min(old[1], b[1]), max(old[2], b[2]), max(old[3], b[3]))
    with LOCK:
        if len(INDEX) > 50000:
            INDEX.clear()
        for cid, b in boxes.items():
            INDEX[cid] = [e for e in INDEX.get(cid, ()) if e[0] != ik] + [(ik, b, cut)]
    sh = planner.shared
    if sh is not None and hasattr(sh, "shape_index_store"):
        sh.shape_index_store(ik, boxes, cut)


def grow(planner, net, sysm, cid, pt, home, adopt=True, clock=None):
    """(key, net, system, the net's other systems, cut) of the whole interchange the system sysm (a piece of it, found in some
    region's box) is part of: the members within SHAPES_PAD_M of the system's seeds (what a ramp reaches) found again as
    systems, the one holding the road cid taken, and so on until it takes in nothing more. Whichever piece it starts
    from, the end is the same set of roads and the same last box (a system taking in nothing at its own box's size is
    closed, and every interchange has one such set), so every region finds the same key. adopt: the run's index is looked
    at between rounds, and an interchange another process has found meanwhile is taken as it is ((key, None, None, None,
    cut)). home: the asking cell: where the whole is too wide it is followed as far as the cell's tile (tile_box) and
    cut is the tile's name, else "". clock (a dict): the seconds of the members' reading and of find_systems added to its "paths" and "extent"."""
    import grading
    import xproc
    clock = {} if clock is None else clock
    ver = version()
    tile = str(tile_of(home))
    bound_lo, bound_hi = tile_box(planner, home)
    cut = ""
    ids = _gsh.system_ids(net, sysm)
    cur_net, cur = net, sysm
    byk, loaded = {}, set()
    while True:
        xproc.check_cancel()
        if adopt:
            known = known_key(planner, cid, pt, tile)
            if known is not None:
                return known, None, None, None, cut
        seeds = np.concatenate([cur_net.lines[i] for i in cur["seeds"]])
        lo, hi = seeds.min(0) - grading.SHAPES_PAD_M, seeds.max(0) + grading.SHAPES_PAD_M
        if (lo < bound_lo).any() or (hi > bound_hi).any():      # (too wide to follow whole: as far as the tile)
            lo, hi, cut = np.maximum(lo, bound_lo), np.minimum(hi, bound_hi), tile
        t0 = time.perf_counter()
        s1_paths.load(planner, lo, hi, byk, loaded)
        members = s1_paths.members_of(byk, lo, hi)
        t1 = time.perf_counter()
        nxt_net, systems = _gsh.find_systems(members, lambda n_: (planner._ground_line, s2_extent.make_fixed(planner, n_)))
        clock["paths"] = clock.get("paths", 0.0) + t1 - t0
        clock["extent"] = clock.get("extent", 0.0) + time.perf_counter() - t1
        nxt = next((s for s in systems if cid in {_gsh.canonical_id(nxt_net.m[i]) for i in s["seeds"]}), None)
        if nxt is None:         # (its seeds are not seeds in the wider find: the system as it was)
            log.error("grading: SHAPES interchange: road %s no longer a seed of the system round it", cid)
            return _gsh.interchange_key(ids, centre(planner, cur_net, cur), ver, cut), cur_net, cur, [], cut
        more = _gsh.system_ids(nxt_net, nxt)
        if more <= ids:
            return (_gsh.interchange_key(more, centre(planner, nxt_net, nxt), ver, cut), nxt_net, nxt,
                    [s for s in systems if s is not nxt], cut)
        ids, cur_net, cur = more, nxt_net, nxt


# ---- the solution, in a planner's frame and as stored ----

def keep(planner, ik, got, packed=None):
    """Interchange ik's solution kept by this planner (in its frame) and by this process (packed, for the other planners':
    a solution is made once a process, whichever planning region reaches the interchange)."""
    memo = planner.__dict__.setdefault("_shape_sols", {})
    with planner.lock:
        if len(memo) > KEPT:
            memo.clear()
        memo[ik] = got
    if packed is None:
        packed = pack(planner, got)
    with LOCK:
        PROCESS[ik] = packed
        PROCESS.move_to_end(ik)
        while len(PROCESS) > KEPT:
            PROCESS.popitem(last=False)


def have(planner, ik):
    """Interchange ik's solution this planner has, or this process's, or the run's stored one (read into this planner's
    frame), or None."""
    with planner.lock:
        got = planner.__dict__.setdefault("_shape_sols", {}).get(ik)
    if got is not None:
        return got
    with LOCK:
        stored = PROCESS.get(ik)
    sh = planner.shared
    if stored is None and sh is not None and hasattr(sh, "shape_load"):
        stored = sh.shape_load(ik)
    if stored is None:
        return None
    got = unpack(planner, *stored)
    keep(planner, ik, got, stored)
    return got


def pack(planner, got):
    """(meta, lat, lon, z) of a solution as plancache stores it: its roads' lines in lat / lon, no frame in them."""
    roads, xs, zs = [], [], []
    for fid, items in got["roads"].items():
        for xy, z, mode in items:
            roads.append({"fid": fid, "mode": mode, "n": len(z)})
            xs.append(np.asarray(xy, np.float64).reshape(-1, 2))
            zs.append(np.asarray(z, np.float64).ravel())
    meta = {k: got[k] for k in FIELDS}
    meta["roads"] = roads
    if xs:
        xy, z = np.concatenate(xs), np.concatenate(zs)
        la, lo, _ = planner.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
    else:
        la = lo = z = np.zeros(0)
    return meta, np.asarray(la, np.float64), np.asarray(lo, np.float64), z


def unpack(planner, meta, lat, lon, z):
    """A stored solution (PlanCache.shape_load) in this planner's frame."""
    if len(lat):
        e, n, _ = planner.frame.geodetic_to_enu(lat, lon, 0.0)
        xy = np.stack([np.asarray(e, np.float64), np.asarray(n, np.float64)], axis=1)
    else:
        xy = np.zeros((0, 2))
    roads, k = {}, 0
    for r in meta["roads"]:
        roads.setdefault(r["fid"], []).append((xy[k:k + r["n"]], np.array(z[k:k + r["n"]]), r["mode"]))
        k += r["n"]
    got = {key: meta[key] for key in FIELDS}
    got["roads"] = roads
    return got


# ---- one interchange, asked for by a region ----

def interchange(planner, net, sysm, home, hk, clock):
    """The solution of the interchange the system sysm of net (found in region hk's box) is part of: {"key", "roads"
    ({road feature id: [(xy, z, mode)]}), "failed" (None, or why it has none), "pieces", "crossings", "owner" (the cell
    holding its centre), "pid" and "secs" of the process that solved it}. Made once for the run: by the process that takes
    the interchange's lock, read by the others while they wait for it."""
    import xproc
    t0 = time.perf_counter()
    sh = planner.shared
    cid = sorted({_gsh.canonical_id(net.m[i]) for i in sysm["seeds"]})[0]
    pt = probe(planner, net, sysm, cid)
    tile = str(tile_of(home))

    def solution(ik, ctx):
        def solve():
            c = ctx
            if c is None:       # (the extent was found by another process or thread: found here for the solve)
                _ik, n2, s2, others, _cut = grow(planner, net, sysm, cid, pt, home, adopt=False, clock=clock)
                c = (n2, s2, others)
            return made(planner, ik, hk, clock, *c)
        return once(planner, "sol", ik, lambda: have(planner, ik), solve)
    ik = known_key(planner, cid, pt, tile)
    got = mine = None
    if ik is None:
        name = sh.shape_member_name(cid) if sh is not None and hasattr(sh, "shape_member_name") else cid

        def known():
            found = known_key(planner, cid, pt, tile)
            return None if found is None else (found, None, False)

        def grown():
            # (the interchange's own solution is taken before the claim on its extent is given back: a thread or process
            # that learns its key from the index meanwhile finds the solution claimed, not the whole interchange to be
            # followed and solved again)
            ik_, n2, s2, others, cut = grow(planner, net, sysm, cid, pt, home, clock=clock)
            if n2 is None:
                return ik_, None, False
            index_add(planner, ik_, n2, s2, cut)
            return (ik_,) + solution(ik_, (n2, s2, others))
        (ik, got, mine), _ = once(planner, "ext", name, known, grown)
    if got is None:
        got, mine = solution(ik, None)
    xproc.check_cancel()
    with planner.lock:
        logged = planner.__dict__.setdefault("_shape_logged", set())
        first = ik not in logged
        logged.add(ik)
    if first:
        secs = time.perf_counter() - t0
        log.warning("grading: shapes interchange: %s owner cell %s, %d pieces, %d crossings, asked by region %s: %s%s",
                    ik[:12], got["owner"], got["pieces"], got["crossings"], hk,
                    ("solved here, pid %d, %.1f s" % (os.getpid(), secs)) if mine else
                    ("waited %.1f s for pid %s, which solved it in %.1f s" % (secs, got["pid"], got["secs"])),
                    ("; no solution: %s" % got["failed"]) if got["failed"] else "")
    return got


def made(planner, ik, hk, clock, net, sysm, others):
    """Interchange ik's solution made now (pipeline.interchange: the steps of its one system, its embankments), stored for
    the run's other processes (when nothing it read was short: a solve made without some elevation is this process's
    alone) and kept."""
    import revisit
    from . import pipeline
    t0 = time.perf_counter()
    pieces, crossings = _gsh.system_size(net, sysm)
    with revisit.scope() as b:
        roads, why = pipeline.interchange(planner, ik, net, sysm, others, hk, clock)
    got = dict(key=ik, roads=roads, failed=why, pieces=pieces, crossings=crossings,
               owner=str(owner_cell(planner, net, sysm)), pid=os.getpid(), secs=round(time.perf_counter() - t0, 3))
    sh = planner.shared
    packed = pack(planner, got)
    if sh is not None and hasattr(sh, "shape_store") and b.complete:
        sh.shape_store(ik, *packed)
    keep(planner, ik, got, packed)
    return got
