"""One interchange, one owner (BNE_SHAPES): a region's box cuts a large interchange differently from every other region's,
and every region and process solved the pieces it found (one system of about 1,300 pieces solved 141 times over 87
regions, a build of 729 s taking 15025 s). Now the region's piece is only the way in:

- grow() follows it, through its joins and crossings, to the whole interchange whatever box it is found in, so every
  region reaches the same members and the same key (gradeshapes.interchange_key). It is done once: the first to reach a
  piece of the interchange takes the claim on that piece (once, "ext"), and the index it stores names every member piece
  (index_add: road id to key). Whoever reaches ANY piece of it finds the key there, or waits for the growth running. Two
  growths that reach each other's pieces are one interchange: the one that started from the lower road id grows it, the
  other gives way (GiveWay: its claim released, then it waits for the lower one's result, holding no lock), so the
  claims wait only on lower ids and never in a circle;
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

from . import limits, s1_paths, s2_extent

log = logging.getLogger("earthd.grading")

BOX_DEG = 0.0003            # deg (about 30 m): how far outside its box (in the index of interchanges) a road's point may be
#                             and still be of it
LOCK = threading.Lock()     # (the tables below: short holds)
FLIGHTS = {}                # (the extents and solutions this process is asking for now: once)
PROCESS = OrderedDict()     # interchange key -> its solution as stored (pack): this process's, every planner's. Made short of
#                             something it is not in the run's store; with no run store it is the only one
INDEX = {}                  # road canonical id -> [(interchange key, lat / lon box of its seeds)]: this process's
KEPT = 512                  # solutions kept (per planner, per process)
EXTENTS = {}                # interchange key -> its extent record: {"box": (lat0, lon0, lat1, lon1) of its seeds, "keys": the
#                             member keys of its seeds, all its systems'} (the growth's: the reach, computed once, is in it)
FIELDS = ("key", "failed", "pieces", "crossings", "owner", "pid", "secs", "relaxed")
LIMITED = {}                # interchange key (or ("road", id) of a growth) -> its failure, where a step went past its time limit
#                             (ix/limits.py): this process does not make it again for every region that reaches it, and the
#                             run's store never has it: another process tries it once


class GiveWay(Exception):
    """A growth reached a piece (cid) of its interchange that a growth from a lower road id is running from: it is that
    one's to grow. Raised out of the claim, so it is given back before the wait."""

    def __init__(self, cid):
        super().__init__(cid)
        self.cid = cid


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


def name_of(planner, cid):
    """The name a growth from road cid claims: the index's file name of it (shared), else the id."""
    sh = planner.shared
    return sh.shape_member_name(cid) if sh is not None and hasattr(sh, "shape_member_name") else cid


def growing(planner, cid):
    """Whether a growth from road cid is running now (in this process or another)."""
    name = name_of(planner, cid)
    with LOCK:
        if ("ext", name) in FLIGHTS:
            return True
    sh = planner.shared
    return sh is not None and hasattr(sh, "shape_busy") and sh.shape_busy("ext", name)


def wait_for(planner, other, cid, pt):
    """Until the growth running from road `other` has ended: the key of the piece cid at pt is known then (the index), or
    the growth is gone (its holder died: nothing to read, the caller grows). Holds no claim while it waits."""
    once(planner, "ext", name_of(planner, other), lambda: known_key(planner, cid, pt), lambda: None)


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


def key_of(planner, net, sysm):
    """The key of the interchange sysm is (the whole one: grow): its roads, where its centre is, and what a solve of it is
    made from (gradeshapes.interchange_key). The same from every region, process and planning frame."""
    return _gsh.interchange_key(_gsh.system_ids(net, sysm), centre(planner, net, sysm), version())


def pick(entries, pt):
    """The key of the entry [(key, (lat0, lon0, lat1, lon1))] whose box holds pt (lat, lon) within BOX_DEG (the smallest
    box where several do), else None."""
    best = None
    for ik, (a, b, c, d) in entries:
        if a - BOX_DEG <= pt[0] <= c + BOX_DEG and b - BOX_DEG <= pt[1] <= d + BOX_DEG:
            area = (c - a) * (d - b)
            if best is None or area < best[0]:
                best = (area, ik)
    return None if best is None else best[1]


def known_key(planner, cid, pt):
    """The key of the interchange the road cid (a canonical id) at pt (lat, lon) is part of, when this process or the
    run's index knows it, else None."""
    with LOCK:
        ents = INDEX.get(cid, ())
        ik = pick(ents, pt)
    sh = planner.shared
    if ik is None and sh is not None and hasattr(sh, "shape_index"):
        ents = sh.shape_index(cid)
        if ents:
            with LOCK:
                INDEX[cid] = ents
            ik = pick(ents, pt)
    if ik is None and ents and os.environ.get("BNE_IX_PROFILE"):
        # (the road is in the index but nowhere near pt: how far, and what the nearest entry holds)
        near = min(ents, key=lambda e: max(e[1][0] - pt[0], pt[0] - e[1][2], 0.0) + max(e[1][1] - pt[1], pt[1] - e[1][3], 0.0))
        a, b, c, d = near[1]
        log.warning("ix profile: known_key: road %s at (%.6f, %.6f) has %d index entries, none holding it: the nearest "
                    "box (%.6f, %.6f, %.6f, %.6f) is %.0f m away, key %s", cid, pt[0], pt[1], len(ents), a, b, c, d,
                    111000 * (max(a - pt[0], pt[0] - c, 0.0) + max(b - pt[1], pt[1] - d, 0.0)), near[0][:12])
    return ik


def probe(planner, net, sysm, cid):
    """(lat, lon) of a point on the first seed of the system that is road cid: what tells which of a road's interchanges it
    is (the index's boxes)."""
    i = next(i for i in sysm["seeds"] if _gsh.canonical_id(net.m[i]) == cid)
    p = net.lines[i][len(net.lines[i]) // 2]
    la, lo, _ = planner.frame.enu_to_geodetic(np.array([p[0]]), np.array([p[1]]), 0.0)
    return float(la[0]), float(lo[0])


def points(planner, net, sysm):
    """{canonical id: (lat, lon)} of a point on each seed road of the system (what tells which of a road's interchanges
    it is: the index's boxes)."""
    seeds = list(sysm["seeds"])
    if not seeds:
        return {}
    mid = np.array([net.lines[i][len(net.lines[i]) // 2] for i in seeds])
    la, lo, _ = planner.frame.enu_to_geodetic(mid[:, 0], mid[:, 1], 0.0)
    out = {}
    for i, a, b in zip(seeds, la, lo):
        out.setdefault(_gsh.canonical_id(net.m[i]), (float(a), float(b)))
    return out


def index_add(planner, ik, net, sysm):
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
            INDEX[cid] = [e for e in INDEX.get(cid, ()) if e[0] != ik] + [(ik, b)]
        if os.environ.get("BNE_IX_PROFILE"):
            log.warning("ix profile: index %s: %s", ik[:12], "; ".join("%s box (%.6f, %.6f, %.6f, %.6f), %d entries"
                                                                       % (c[:20], b[0], b[1], b[2], b[3], len(INDEX[c]))
                                                                       for c, b in sorted(boxes.items())))
    sh = planner.shared
    if sh is not None and hasattr(sh, "shape_index_store"):
        sh.shape_index_store(ik, boxes)


def piece_system(cur_net, cur, nxt_net, found, cid):
    """The system of a wider find (found, in nxt_net) that the piece cur (in cur_net) is part of: of those with the road
    cid among their seeds, the one whose seeds lie nearest the piece's seeds (the least distance of a seed's line to a
    piece seed's middle point). A road's pieces within the pad are several systems of the same road, and the first found
    is not always the asking piece's: followed, it is an interchange the index knows (the same key), so nothing is
    indexed for the piece, and every region holding it grows it again (Compiegne: a deck's two pieces 800 m apart,
    grown at every ask; Yokohama: 140 s of a build's growths). None when no system has the road among its seeds."""
    mid = [cur_net.lines[i][len(cur_net.lines[i]) // 2] for i in cur.sysm["seeds"]]
    best = None
    for x in found:
        seeds = x.sysm["seeds"]
        if cid not in {_gsh.canonical_id(nxt_net.m[i]) for i in seeds}:
            continue
        if not mid:
            return x
        d = min(float(np.min(np.hypot(nxt_net.lines[j][:, 0] - m[0], nxt_net.lines[j][:, 1] - m[1])))
                for j in seeds for m in mid)
        if best is None or d < best[0]:
            best = (d, x)
    return None if best is None else best[1]


def grow(planner, net, ix, cid, pt, adopt=True, hint=None):
    """(key, net, Interchange, the net's other systems) of the whole interchange the one found (ix, in net: a piece of it,
    as some region's box holds it) is part of: the members within SHAPES_PAD_M of its seeds (what a ramp reaches) found
    again as interchanges, the one the piece is part of (piece_system: of the systems with the road cid among their seeds,
    the one nearest the asking piece), and so on until it takes in nothing more. Whichever piece it
    starts from, the end is the same set of roads and the same last box (an interchange taking in nothing at its own box's
    size is closed, and every interchange has one such set), so every region finds the same key. adopt: the run's index is
    looked at for every piece the growth takes in, before the next round: an interchange another process has found
    meanwhile is taken as it is ((key, None, None, None)), and one a growth from a lower road id is running on is that
    growth's (GiveWay). hint: the interchange's extent record (extent_of), found already: its one round, with the reaches
    as the growth stored them, is the last, not the growth again. A growth's rounds scan only the members they add (gradeshapes.Scan). The Interchange returned carries the seconds this took (its paths: the members
    read, its relations; its extent: the finding), for the steps' lines."""
    import grading
    import xproc
    ids = _gsh.system_ids(net, ix.sysm)
    cur_net, cur = net, ix
    byk, loaded = {}, set()
    read_s = find_s = 0.0
    checked = {cid}
    box = None
    reach = True
    scan = _gsh.Scan()
    prev = None             # (the last round's find and its members' keys: a round with the same members is skipped)
    if hint is not None:
        b = hint["box"]
        e, n, _ = planner.frame.geodetic_to_enu(np.array([b[0], b[0], b[2], b[2]]), np.array([b[1], b[3], b[1], b[3]]), 0.0)
        box = (np.array([np.min(e), np.min(n)]), np.array([np.max(e), np.max(n)]))
        reach = [set(hint["keys"])]
    while True:
        xproc.check_cancel()
        if adopt:
            known = known_key(planner, cid, pt)
            if known is not None:
                return known, None, None, None
            fresh = ids - checked
            if fresh:
                pts = points(planner, cur_net, cur.sysm)
                for c in sorted(fresh):
                    if c not in pts:
                        continue        # (a road dug under it: the index holds seeds)
                    got = known_key(planner, c, pts[c])
                    if got is not None:
                        return got, None, None, None
                    if c < cid and growing(planner, c):
                        raise GiveWay(c)
                checked |= fresh
        if box is not None:
            lo, hi = box[0] - grading.SHAPES_PAD_M, box[1] + grading.SHAPES_PAD_M
        else:
            # (the growth's pad: a ramp runs as far as REACH_MAX_M from the end it left, so a pad shorter than that
            # finds the chain's next decks one ring at a time: Yokohama's stacked viaducts, 8 rounds of 78 s for a
            # chain 5 km long, the last three adding 38 roads. BNE_IX_GROW_PAD_M tries another)
            pad = float(os.environ.get("BNE_IX_GROW_PAD_M", grading.SHAPES_PAD_M))
            seeds = np.concatenate([cur_net.lines[i] for i in cur.sysm["seeds"]])
            lo, hi = seeds.min(0) - pad, seeds.max(0) + pad
        t0 = time.perf_counter()
        with limits.scope("paths", read_s):         # (the steps' time limits count the growth: its reading is the paths')
            s1_paths.load(planner, lo, hi, byk, loaded)
            members = s1_paths.members_of(byk, lo, hi)
        t1 = time.perf_counter()
        keys_ = [m.key for m in members]
        # (a round that holds the same members as the last finds what it did: the find is a function of them. Skipped:
        # the fixed point's last round, which only confirms it. Yokohama's stacked viaducts: two of eight rounds, 13 s)
        if prev is not None and keys_ == prev[2]:
            # (the last round's nxt is cur, whose ids are ids: closed)
            if os.environ.get("BNE_IX_PROFILE"):
                log.warning("ix profile: growth round of %s skipped: the same %d members as the last round", cid[:12],
                            len(members))
            break
        with limits.scope("extent", find_s):        # (and its finding the extent's)
            nxt_net, found = _gsh.find_interchanges(members, lambda n_: (planner._ground_line, s2_extent.make_fixed(planner, n_)),
                                                    scan=scan, reach=reach)
        prev = (nxt_net, found, keys_)
        read_s += t1 - t0
        find_s += time.perf_counter() - t1
        nxt = piece_system(cur_net, cur, nxt_net, found, cid)
        if os.environ.get("BNE_IX_PROFILE"):
            log.warning("ix profile: growth round of %s from %s: %d cells read in %.2f s, find of %d members in %.2f s, "
                        "ids %d -> %d, box %.0f x %.0f m", cid[:12], pt, len(loaded), read_s, len(members),
                        time.perf_counter() - t1, len(ids),
                        len(_gsh.system_ids(nxt_net, nxt.sysm)) if nxt is not None else -1,
                        0.0 if box is None else float(box[1][0] - box[0][0]), 0.0 if box is None else float(box[1][1] - box[0][1]))
        if nxt is None:         # (its seeds are not seeds in the wider find: the interchange as it was)
            if box is not None:
                box, reach = None, True     # (the stored extent did not give it: grown as without)
                continue
            log.error("grading: SHAPES interchange: road %s no longer a seed of the interchange round it", cid)
            nxt_net, nxt, found = cur_net, cur, [cur]
            break
        if box is not None:
            break               # (the whole extent was read: nothing to grow)
        more = _gsh.system_ids(nxt_net, nxt.sysm)
        if more <= ids:
            break
        ids, cur_net, cur = more, nxt_net, nxt
    # (what the finding cost is the first system's: the steps of each system carry the seconds of its own)
    parts = tuple(p._replace(paths_s=p.paths_s + (read_s if k == 0 else 0.0),
                             extent_s=p.extent_s + (find_s - nxt.paths_s if k == 0 else 0.0))
                  for k, p in enumerate(nxt.parts))
    whole = _gsh.Interchange(nxt.sysm, read_s + nxt.paths_s, find_s - nxt.paths_s, parts)
    return (key_of(planner, nxt_net, nxt.sysm), nxt_net, whole,
            [p.sysm for x in found if x is not nxt for p in (x.parts or (x,))])


def extent_add(planner, ik, net, sysm):
    """Interchange ik's extent stored, with the growth's one computation of its ramps' reaches (the member keys of its seeds:
    they are one interchange): what a solve in a process that did not grow it starts from, in one round."""
    boxes = []
    for i in sysm["seeds"]:
        la, lo, _ = planner.frame.enu_to_geodetic(net.lines[i][:, 0], net.lines[i][:, 1], 0.0)
        boxes.append((float(np.min(la)), float(np.min(lo)), float(np.max(la)), float(np.max(lo))))
    rec = {"box": [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)],
           "keys": sorted(str(net.m[i].key) for i in sysm["seeds"])}
    with LOCK:
        EXTENTS[ik] = rec
        if len(EXTENTS) > 50000:
            EXTENTS.clear()
            EXTENTS[ik] = rec
    sh = planner.shared
    if sh is not None and hasattr(sh, "shape_extent_store"):
        sh.shape_extent_store(ik, rec)


def extent_of(planner, ik):
    """The extent record of interchange ik (extent_add: this process's, or the run's), or None."""
    with LOCK:
        rec = EXTENTS.get(ik)
    sh = planner.shared
    if rec is None and sh is not None and hasattr(sh, "shape_extent_load"):
        rec = sh.shape_extent_load(ik)
        if rec is not None:
            with LOCK:
                EXTENTS[ik] = rec
    return rec


def skipper(planner, taken, h_lo, h_hi):
    """skip(net, i) -> bool for the region's find (gradeshapes.find_interchanges): member i is a piece of an interchange
    already solved (the index names its key, the solution is here or in the run's store), so it is not scanned again, nor
    is its interchange found: its solution is read. taken {key: True} gets the key of each such interchange that has a piece
    in the home box h_lo..h_hi (the region's roads it grades)."""
    memo = {}

    def skip(net, i):
        m = net.m[i]
        line = net.lines[i]
        cid = _gsh.canonical_id(m)
        p = line[len(line) // 2]
        la, lo, _ = planner.frame.enu_to_geodetic(np.array([p[0]]), np.array([p[1]]), 0.0)
        ik = known_key(planner, cid, (float(la[0]), float(lo[0])))
        if ik is None:
            return False
        if ik not in memo:
            memo[ik] = have(planner, ik) is not None
        if not memo[ik]:
            return False
        bx = net.box[i]
        if not ((bx[2:] < h_lo).any() or (bx[:2] > h_hi).any()):
            taken[ik] = True
        return True
    return skip


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
    got = {key: meta.get(key, 0 if key == "relaxed" else None) for key in FIELDS}     # (relaxed: from a store before it)
    got["roads"] = roads
    return got


# ---- one interchange, asked for by a region ----

def interchange(planner, net, ix, hk, clock):
    """_interchange, where a step of the interchange's growth that goes past its time limit (ix/limits.py: the paths and
    extent steps are its growth's) gives the failure, not the solution: nothing is stored, no claim is kept (once releases
    them as the exception leaves), and the process does not grow it again for every region that reaches it."""
    import xproc
    sysm = ix.sysm
    cid = sorted({_gsh.canonical_id(net.m[i]) for i in sysm["seeds"]})[0]
    if ("road", cid) in LIMITED:
        return LIMITED[("road", cid)]
    try:
        return _interchange(planner, net, ix, hk, clock)
    except xproc.TimeLimit as e:
        text = "the growth of the interchange of road %s ran past its %s limit of %g s: stopped, nothing of it stored" % (
            cid, e.what, e.secs)
        limits.record("growth:%s" % cid, e.what, e.secs, e.secs, text)
        log.warning("ix step: %s key - failed: time limit %g s @ %s growth from road %s", e.what, e.secs, hk, cid)
        log.error("grading: SHAPES infeasible system in region %s: %d crossings, %d ends; %s; members %s", hk,
                  len(sysm["crossings"]), len(sysm["ends"]), limits.reason(e.what, e.secs),
                  ", ".join(sorted({str(net.m[i].key[0])[:8] + ":" + net.m[i].kind for i in sysm["seeds"]}))[:600])
        got = dict(key="limit:%s" % cid, roads={}, failed=limits.reason(e.what, e.secs), pieces=0, crossings=0, relaxed=0,
                   owner="", pid=os.getpid(), secs=0.0)
        LIMITED[("road", cid)] = got
        return got


def _interchange(planner, net, ix, hk, clock):
    """The solution of the interchange ix (gradeshapes.Interchange of net, found in region hk's box) is part of: {"key",
    "roads" ({road feature id: [(xy, z, mode)]}), "failed" (None, or why it has none), "pieces", "crossings", "owner"
    (the cell holding its centre), "pid" and "secs" of the process that solved it}. Made once for the run: by the process
    that takes the interchange's lock, read by the others while they wait for it."""
    import xproc
    t0 = time.perf_counter()
    sh = planner.shared
    sysm = ix.sysm
    cid = sorted({_gsh.canonical_id(net.m[i]) for i in sysm["seeds"]})[0]
    pt = probe(planner, net, sysm, cid)

    def solution(ik, ctx):
        def solve():
            if ik in LIMITED:           # (made past its limit before, by this process: not again for every region)
                return LIMITED[ik]
            c = ctx
            if c is None:       # (the extent was found by another process or thread: read from the index for the solve)
                _ik, n2, i2, others = grow(planner, net, ix, cid, pt, adopt=False, hint=extent_of(planner, ik))
                c = (n2, i2, others)
            return made(planner, ik, hk, clock, *c)
        return once(planner, "sol", ik, lambda: have(planner, ik), solve)
    got = mine = None
    while True:
        ik = known_key(planner, cid, pt)
        if ik is not None:
            break
        name = name_of(planner, cid)

        def known():
            found = known_key(planner, cid, pt)
            return None if found is None else (found, None, False)

        def grown():
            # (the interchange's own solution is taken before the claim on its extent is given back: a thread or process
            # that learns its key from the index meanwhile finds the solution claimed, not the whole interchange to be
            # followed and solved again)
            ik_, n2, i2, others = grow(planner, net, ix, cid, pt)
            if os.environ.get("BNE_IX_PROFILE"):
                log.warning("ix profile: grown from road %s at (%.6f, %.6f): %s", cid[:20], pt[0], pt[1],
                            ("known key " + ik_[:12]) if n2 is None else
                            ("key %s, %d seeds (%s)" % (ik_[:12], len(i2.sysm["seeds"]),
                                                        ", ".join(sorted({_gsh.canonical_id(n2.m[i])[:20] for i in i2.sysm["seeds"]})))))
            if n2 is None:
                return ik_, None, False
            index_add(planner, ik_, n2, i2.sysm)
            extent_add(planner, ik_, n2, i2.sysm)
            return (ik_,) + solution(ik_, (n2, i2, others))
        try:
            (ik, got, mine), _ = once(planner, "ext", name, known, grown)
        except GiveWay as g:
            wait_for(planner, g.cid, cid, pt)
            continue
        break
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
                    (("; no solution: %s" % got["failed"]) if got["failed"] else "")
                    + ("; %d relaxed" % got["relaxed"] if got.get("relaxed") else ""))
    return got


def made(planner, ik, hk, clock, net, ix, others):
    """Interchange ik's solution made now (pipeline.interchange: the steps of its one interchange, its embankments), stored
    for the run's other processes (when nothing it read was short: a solve made without some elevation is this process's
    alone) and kept."""
    import revisit
    from . import pipeline
    t0 = time.perf_counter()
    pieces, crossings = _gsh.system_size(net, ix.sysm)
    r0 = clock.get("relaxed", 0)
    with revisit.scope() as b:
        roads, why = pipeline.interchange(planner, net, ix, others, hk, clock)
    got = dict(key=ik, roads=roads, failed=why, pieces=pieces, crossings=crossings, relaxed=clock.get("relaxed", 0) - r0,
               owner=str(owner_cell(planner, net, ix.sysm)), pid=os.getpid(), secs=round(time.perf_counter() - t0, 3))
    if limits.is_limit(why):    # (a step ran past its time limit: not stored, not kept, nothing in its place: no answer, the
        LIMITED[ik] = got       # claim is given back when this returns; the process does not try it again for each region)
        return got
    sh = planner.shared
    packed = pack(planner, got)
    if sh is not None and hasattr(sh, "shape_store") and b.complete:
        sh.shape_store(ik, *packed)
    keep(planner, ik, got, packed)
    return got
