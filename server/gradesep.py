"""The grade separations the map has no bridge for (2026-10-27, the user: "Sure start with the missing then steep decks,
then tunnels"; GitHub #387: a railway over a trunk road with no bridge on either was drawn as a hump the road climbed
over). Where two ground lines (drawn roads, drawn railways) cross with no junction, the ground on either side of the
crossing tells which is above: the upper one, higher by SEP_M (and by SIDE_M on each side of each line, so a road up
a hillside crossing one along it is no bridge), gets a bridge over the lower one's carriageway and CLEAR_M either side.
Two more without the heights: a line over a railway's short tunnel (railways.SHORT_TUNNEL_M: OSM tags the line under a
road bridge so; the road over it had none, 2026-10-25), and a short stretch between two decks crossing a ground line (a
flyover's middle left untagged). A span past a line's end goes on along the line joined there.

A cell's are found among its roads and its neighbours' (the crossings of its lines out of the cell), with the map's own
ranges (roads.tag_level_ranges), so every cell finds the same bridges for a line: roads.level_ranges adds them where
the map has no range (MapData.cell hooks a cell's roads layer: hook()). The kernels: earthfast gradesep_candidates /
gradesep_spans; the ground: the DEM at DEM_ZOOM, raw (not graded).
"""
import logging
import os
import threading
import weakref

import numpy as np

import fast
import dnp
import mapcells
import roads
import railways

log = logging.getLogger("earthd.gradesep")

# All of it off by default (2026-10-27 in-game A/B on fresh caches): every cell whose level ranges are asked reads its
# eight neighbours, and grading asks a ring of cells round a road tile, so a first visit read about twice the roads
# cells (PC: +7-12 s to the first road tile; Steam Deck, no warm-up threads: first road tile 141 s against 91 s off).
# BNE_GRADESEP=on turns it on. The default, "tunnels": only a line over another's short tunnel (an underpass OSM maps
# as the lower road's tunnel, a railway's short "tunnel" under a road bridge: GitHub #450, the A-7 at Malaga on two
# short decks over an open road, drawn as a tube in an embankment), and a cell's neighbours read only when the cell
# has such a tunnel itself (few do: no cost elsewhere). off: none.
MODE = os.environ.get("BNE_GRADESEP", "tunnels")
ON = MODE == "on"
TUNNELS = MODE in ("on", "tunnels")
VERSION = 2             # in MapData.versions(): every cell's ranges (cell_facts) and tiles made again on a change
DEM_ZOOM = 15
SEP_M = 4.0             # m the upper line's ground stands above the lower one's (medians of four heights each)
SIDE_M = 2.0            # m ... at least, on each side of each line
END_M = 2.0             # m: a crossing this close to a line's end is a junction (or a level crossing's join)
SAMPLE_GAP_M = 8.0      # m past the other line's half width (over the sine) to the nearer height, then
SAMPLE_STEP_M = 12.0    # m on to the farther one
MIN_SIN = 0.3           # flatter crossings taken as this (the reach would run away)
CLEAR_M = 5.0           # m of span past the lower line's half width either side (shoulders, abutment slopes)
MIN_HALF_M = 8.0        # m of span at least either side of the crossing
GAP_MAX_M = 150.0       # m: a stretch between two decks this short crossing a ground line is a bridge whole, and
GAP_SHARE = 1.0         # one shorter than this times the longer deck it joins (an interchange ramp's untagged piece:
# Springfield's flyover 97 m between a 373 m and a 62 m deck stood 0 m over I-95 coming down to it; a freeway between
# its short street bridges runs on the ground). (One joining a deck that crosses a road close by, unable to come down
# there, was tried too: the DEM's ground under a short overpass is the lower road's dip smoothed, and urban freeways
# became viaducts.)
TOUCH_M = 1.0           # m: ends this close are joined
TURN_COS = 0.7          # a span goes on onto a joined line turning less than ~45 degrees
SHORT_ROAD_TUNNEL_M = 80.0  # m: a road's tunnel this short crossed by a ground line ...
UNDERPASS_COVER_M = 12.0    # ... with less ground over it than this (over its approaches PORTAL_OUT_M past its portals)
PORTAL_OUT_M = 10.0         # is an underpass: ground under the line over it, which gets the bridge (_underpasses)
CUT_SIDE_M = (15.0, 25.0, 35.0, 45.0)  # m along the line over a longer one's crossing where its ground is read either
                            # side: a cutting (SEP_M over the crossing's ground somewhere on both) makes it an underpass
                            # (a dual carriageway's cutting is 40 m wide and more: Highway 3 at Hameenlinna)
JOINED_ENDS = os.environ.get("BNE_GRADESEP_JOINED", "on") != "off"   # a junction over a tunnel: its arms' crossings
                            # within END_M of the node count for the underpass test (_underpasses)
NEAR_M = 60.0           # m round the cell's own lines that neighbours' lines are taken from
RAIL_HALF_M = 2.5       # m: a track's half width (its ballast)


def hook(md, key):
    """The callable a cell's roads layer carries (roads.level_ranges): {feature row: [(from, to, "bridge" | "ground")
    fractions]} of the bridges found and of the underpasses under them, made once per decoded layer."""
    lock = threading.Lock()
    memo = {}

    def run(feats):
        got = memo.get("spans")
        if got is not None:
            return got
        with lock:
            got = memo.get("spans")
            if got is None:
                got = memo["spans"] = _spans_kept(md, key, feats) if TUNNELS else {}
        return got
    return run


def _spans_kept(md, key, feats):
    """_spans through this process's cell cache (roads._FACTS_MEM, by the layer's cell id): the map data reloads a
    city's layers again and again, and each copy made them again (33 makings of ~0.2 s for 3 Manhattan cells'
    plans, 2026-11-02). Kept when made with no fault of this thread (a neighbour not there yet)."""
    import revisit
    import roads
    cid = feats.__dict__.get("_bnge_cell")
    mk = (cid, "gradesep", MODE, VERSION, ON, TUNNELS) if cid is not None else None
    if mk is not None:
        with roads._FACTS_LOCK:
            got = roads._FACTS_MEM.get(mk)
        if got is not None:
            spans, why = got
            feats.__dict__["_gradesep_why"] = why
            return spans
    f0 = revisit.thread_faults()
    spans = _spans(md, key, feats)
    if mk is not None and revisit.thread_faults() == f0:
        with roads._FACTS_LOCK:
            roads._FACTS_MEM[mk] = (spans, feats.__dict__.get("_gradesep_why"))
            while len(roads._FACTS_MEM) > roads.FACTS_MEM_KEPT:
                roads._FACTS_MEM.popitem(last=False)
    return spans


_POOL = None
_POOL_LOCK = threading.Lock()


def neighbours_hook(md, key):
    """The callable a cell's roads layer carries (roads.lane_flow): the roads layers of the eight cells round it
    (_neighbours), held weakly: handed back while the map data (or a caller) still has them, asked again once gone; a
    cell without one is not asked again. A plain list made every layer keep its neighbours alive, and those theirs, so
    no roads layer a server process decoded was ever freed by MapData's memory cap (GitHub #893: 270 cells driven three
    times at MEM_MB 192, 647 layers alive for the 175 it kept and 500 MB of private memory, still growing; now 180 for
    180 and 241 MB, flat, in the same time)."""
    lock = threading.Lock()
    keys = _neighbour_keys(key)
    names = [str(k) for k in keys]
    refs = [_UNREAD] * len(keys)    # per key: a weak reference to its roads layer, None (none there) or _UNREAD

    def run(want=None):
        """The eight layers (None where a cell has none); want: only the cells with these names are read (the others
        None unless at hand already): a caller that needs a few of them (a road's home cell, the cells a road
        crosses) does not have the rest decoded, and theirs (2026-11-11: most of a cold first block's halo)."""
        with lock:
            got = [r() if r is not None and r is not _UNREAD else None for r in refs]
            todo = [i for i, r in enumerate(refs) if (r is _UNREAD or (r is not None and got[i] is None))
                    and (want is None or names[i] in want)]
            if todo:
                for i, f in zip(todo, _neighbours(md, key, [keys[i] for i in todo])):
                    got[i] = f
                    refs[i] = weakref.ref(f) if f is not None else None
            return got
    run.names = names
    return run


_UNREAD = object()


def _neighbour_keys(key):
    """The keys of the eight cells round key (fewer at the map's top and bottom rows)."""
    return [mapcells.CellKey(key.z, (key.x + dx) % key.n, key.y + dy)
            for dy in (-1, 0, 1) for dx in (-1, 0, 1) if (dx or dy) and 0 <= key.y + dy < key.n]


def _neighbours(md, key, keys=None):
    """The roads layers of the eight cells round key (or of keys), asked at once (one after another, cold, they held a
    fresh place's first road tile ~7 s: 2026-10-27 A/B)."""
    global _POOL
    keys = _neighbour_keys(key) if keys is None else keys
    with _POOL_LOCK:
        if _POOL is None:
            from runtimeprofiler import ThreadPoolExecutor
            _POOL = ThreadPoolExecutor(8, thread_name_prefix="gradesep-cells")
    futs = [_POOL.submit(lambda k: md.cell(k, layers=("roads",), kind="demand").layers.get("roads"), k) for k in keys]
    return [f.result() for f in futs]


def _kind_half(feats, i):
    """(kind, half width m) of feature i as a crossing line: 0 a drawn road, 1 a drawn railway; None neither
    (_layer_rows')."""
    kinds, halves = _layer_rows(feats)[:2]
    return None if kinds[i] < 0 else (int(kinds[i]), float(halves[i]))


def _layer_rows(f):
    """Every feature's crossing line of a layer as arrays, once per decoded layer: (kind (-1: no crossing line; 0 a
    drawn road: its class's style, built; 1 a drawn railway), half width, piece offsets, piece kinds (-1: a deck), from,
    to, the ids as str). The pieces by its map ranges (roads.tag_level_ranges): its ground pieces (kind), a railway's
    short tunnels (2), a road's tunnels (3: an underpass if a line crosses over it close above, _underpasses) and its
    bridges (deck: their ends are deck ends); earthfast.gradesep_layer_rows (per feature in Python, 2026-11-02: 1.5 s
    of two cells' planning)."""
    got = f.__dict__.get("_gs_layer")
    if got is None:
        n = len(f)
        kinds, halves = np.full(n, -1, np.int64), np.zeros(n)
        if "subtype" in f.attrs:
            def strs(col):
                return np.asarray([str(v) for v in (col.tolist() if hasattr(col, "tolist") else col)], dtype=object)
            subs = strs(f.attrs["subtype"])
            clss = strs(f.attrs["class"]) if "class" in f.attrs else np.full(n, "unknown", dtype=object)
            unb = np.asarray(roads.unbuilt_mask(f), bool)
            road = (subs == "road") & ~unb
            for c in set(clss[road].tolist()):
                st = roads.STYLE.get(c)
                if st is not None:
                    sel = road & (clss == c)
                    if c == "cycleway":         # (only a street closed to cars is a road: roads.cycle_streets)
                        sel &= roads.cycle_streets(f)
                    kinds[sel], halves[sel] = 0, st[0] / 2.0
            for i in np.flatnonzero(subs == "rail").tolist():
                if railways.drawn(f, i) is not None:
                    kinds[i], halves[i] = 1, RAIL_HALF_M
            fast.require(189, "gradesep_layer_rows")
            toff, trng, tkind = roads._tag_flat(f)
            poff, pk, pa, pb = fast._ef.gradesep_layer_rows(
                kinds, toff, trng, tkind, np.ascontiguousarray(f.lonlat, np.float64).reshape(-1, 2),
                np.ascontiguousarray(f.coord_off, np.int64), np.ascontiguousarray(f.ring_off, np.int64),
                np.ascontiguousarray(f.part_off, np.int64), roads.MIN_PART, railways.SHORT_TUNNEL_M)
        else:
            poff, pk, pa, pb = np.zeros(n + 1, np.int64), np.zeros(0, np.int64), np.zeros(0), np.zeros(0)
        got = f.__dict__["_gs_layer"] = (kinds, halves, poff, pk, pa, pb, [str(x) for x in f.ids])
    return got


def _to_frame(f, frame, r):
    """Layer rows r (_layer_rows) with their pieces' fractions in the frame's metric, kept per frame
    (earthfast.gradesep_to_frame: the gradesep kernels measure along the frame's east/north; the ranges they read and
    give are sub_polyline's, lon scaled by the cosine of the ring's mean latitude: on a 15 km motorway the two put one
    point 8.7 m apart, its deck off the road under it, the A4 at Glaireuse, #1020)."""
    memo = f.__dict__.setdefault("_gs_frame_pab", {})
    key = (frame.lat0, frame.lon0)
    got = memo.get(key)
    if got is None:
        got = fast._ef.gradesep_to_frame(np.ascontiguousarray(f.lonlat, np.float64).reshape(-1, 2), f.xy_in(frame),
                                         np.ascontiguousarray(f.coord_off, np.int64),
                                         np.ascontiguousarray(f.ring_off, np.int64),
                                         np.ascontiguousarray(f.part_off, np.int64), r[2], r[4], r[5])
        if len(memo) >= 4:
            memo.clear()
        memo[key] = got
    return got


def _rows(frame, lays):
    """_spans' rows: the features of lays (the cell's own layer first) whose boxes in frame come within NEAR_M of the
    own layer's box, each id once (the first layer and row it is in), in the order of their ids (the same order
    whichever cell asks), with each one's row in the own layer (by its id; -1 none): (layer index, row, owner) arrays;
    None when the own layer has no box. earthfast.gradesep_rows."""
    return fast._ef.gradesep_rows([(f.xy_in(frame), np.ascontiguousarray(f.coord_off, np.int64),
                                    np.ascontiguousarray(f.ring_off, np.int64),
                                    np.ascontiguousarray(f.part_off, np.int64), roads._lane_flow_ids(f))
                                   for f in lays], NEAR_M)


def _spans(md, key, feats):
    if feats is None or not len(feats) or "subtype" not in feats.attrs:
        return {}
    if not ON and not np.isin(_layer_rows(feats)[3], (2, 3)).any():
        return {}                   # (tunnels only, and the cell has none)
    import revisit
    try:
        others = _neighbours(md, key)
    except Exception as e:      # (left out, and not kept: revisit.fault)
        log.debug("gradesep %s neighbours: %s: %s", key, type(e).__name__, e)
        revisit.fault()
        return {}
    lat_c, lon_c = key.center()
    import geodesy
    frame = geodesy.EnuFrame(lat_c, lon_c, 0.0)
    lays = [f for f in [feats] + others if f is not None and len(f) and "subtype" in f.attrs]
    got = _rows(frame, lays)
    if got is None:
        return {}
    row_layer, row, owner = got
    if not len(row):
        return {}
    # the lines: the rows' ground pieces in the frame (earthfast.gradesep_gather_rows, the layers as they are)
    fast.require(169, "gradesep_gather_rows")
    lr = [_layer_rows(f) for f in lays]
    fpab = [_to_frame(f, frame, r) for f, r in zip(lays, lr)]
    pts, starts, groups, kinds, oa, ob, halves, de = fast._ef.gradesep_gather_rows(
        [(f.xy_in(frame), np.ascontiguousarray(f.coord_off, np.int64), np.ascontiguousarray(f.ring_off, np.int64),
          np.ascontiguousarray(f.part_off, np.int64), r[2], r[3], ab[0], ab[1], r[1]) for f, r, ab in zip(lays, lr, fpab)],
        row_layer, row)
    oi = owner[groups]                  # (each line's row in the cell's own layer, -1: none)
    if (kinds == 3).any():
        try:
            keep = _underpasses(md, frame, pts, starts, kinds, groups)
        except Exception as e:
            log.debug("gradesep %s underpasses: %s: %s", key, type(e).__name__, e)
            revisit.fault()
            return {}
        if not keep.all():
            pts = pts[np.repeat(keep, np.diff(starts))]
            starts = np.r_[0, np.cumsum(np.diff(starts)[keep])].astype(np.int64)
            kinds, halves, groups, oi, oa, ob = (v[keep] for v in (kinds, halves, groups, oi, oa, ob))
    if not len(kinds):
        return {}
    fast.require(169, "gradesep_candidates")
    cand, smp = fast._ef.gradesep_candidates(pts, starts, kinds, halves, groups, de, np.array(
        [END_M, SAMPLE_GAP_M, SAMPLE_STEP_M, MIN_SIN, TOUCH_M, GAP_MAX_M, TURN_COS, GAP_SHARE, 0.0 if ON else 1.0],
        np.float64))
    if not ON and len(cand):        # (tunnels only: the lines over a short tunnel, how 1; only those were tried)
        one = cand[:, 5] == 1.0
        cand, smp = cand[one], smp.reshape(-1, 8, 2)[one].reshape(-1, 2)
    oi_l, oa_l, ob_l = oi.tolist(), oa.tolist(), ob.tolist()
    out, why = {}, []                   # (why: (row, from, to, how) for probes, feats._gradesep_why)

    def back(rows, xs):                 # (fractions in the frame's metric as the level ranges measure them)
        if not rows:
            return []
        return fast._ef.gradesep_back(np.ascontiguousarray(feats.lonlat, np.float64).reshape(-1, 2),
                                      feats.xy_in(frame), np.ascontiguousarray(feats.coord_off, np.int64),
                                      np.ascontiguousarray(feats.ring_off, np.int64),
                                      np.ascontiguousarray(feats.part_off, np.int64), np.asarray(rows, np.int64),
                                      np.asarray(xs, np.float64)).tolist()
    under = np.flatnonzero((kinds == 3) & (oi >= 0)).tolist()     # the underpasses: ground under the road bridging them
    ab = back([oi_l[ln] for ln in under for _ in (0, 1)], [x for ln in under for x in (oa_l[ln], ob_l[ln])])
    for k, ln in enumerate(under):
        i, a, b = oi_l[ln], ab[2 * k], ab[2 * k + 1]
        out.setdefault(i, []).append((round(a, 6), round(b, 6), "ground"))
        why.append((i, round(a, 6), round(b, 6), 3))
    if not len(cand):
        return _merged(feats, out, why)
    z = np.full(len(smp), np.nan)
    good = np.isfinite(smp).all(axis=1)
    if good.any():
        try:
            la, lo, _ = frame.enu_to_geodetic(smp[good, 0], smp[good, 1], 0.0)
            z[good] = np.asarray(_dem(md).sample(np.asarray(la), np.asarray(lo), DEM_ZOOM), np.float64)
        except Exception as e:
            log.debug("gradesep %s ground: %s: %s", key, type(e).__name__, e)
            revisit.fault()
            return {}
    sp = fast._ef.gradesep_spans(pts, starts, kinds, halves, cand, np.ascontiguousarray(z.reshape(-1, 8)), np.array(
        [SEP_M, SIDE_M, CLEAR_M, MIN_HALF_M, MIN_SIN, TOUCH_M, TURN_COS], np.float64))
    got = []
    for ln, s0, s1, how in sp:
        ln = int(ln)
        i, a, b = oi_l[ln], oa_l[ln], ob_l[ln]
        L = float(dnp.hypot(*np.diff(pts[starts[ln]:starts[ln + 1]], axis=0).T).sum())
        if i < 0 or L <= 0 or s1 - s0 <= 0:
            continue
        got.append((i, a + (b - a) * float(s0) / L, a + (b - a) * float(s1) / L, int(how)))
    ab = back([g[0] for g in got for _ in (0, 1)], [x for g in got for x in g[1:3]])
    for k, (i, _, _, how) in enumerate(got):
        fa, fbb = round(ab[2 * k], 6), round(ab[2 * k + 1], 6)
        out.setdefault(i, []).append((fa, fbb, "bridge"))
        why.append((i, fa, fbb, how))
    return _merged(feats, out, why)


def _merged(feats, out, why):
    """out with each row's ranges of one kind sorted and joined where they overlap; why kept for the probes."""
    for i, v in out.items():
        m = []
        for a, b, k in sorted(v, key=lambda r: (r[2], r[0], r[1])):
            if m and m[-1][2] == k and a <= m[-1][1]:
                m[-1][1] = max(m[-1][1], b)
            else:
                m.append([a, b, k])
        out[i] = [tuple(r) for r in m]
    feats.__dict__["_gradesep_why"] = why
    return out


def _underpasses(md, frame, pts, starts, kinds, groups):
    """keep per line (pts rows starts[n]..starts[n + 1]; kinds, groups arrays): False for a road's tunnel (kind 3)
    that is no underpass: none of the other ground lines crosses over it away from their ends, or the ground there
    stands UNDERPASS_COVER_M or more (roads.TUNNEL_OPEN_COVER for a tunnel of SHORT_ROAD_TUNNEL_M or longer: the whole
    tunnel, Overture splits one at every node, a 2.6 km motorway tunnel into 50 m pieces) over the ground PORTAL_OUT_M
    past its portals (a short tunnel through a hill with a road over it). OSM maps an underpass as the lower road's
    tunnel under a road on the ground; built as a tunnel, its vault and soil stood over the motorway on top (the A7 at
    Brackel and at Schuby, GitHub #228 / #243). A longer one is an underpass only in a cutting the DEM shows: the ground
    of the line over it SEP_M higher either side of a crossing (CUT_SIDE_M along it), Highway 3 at Hameenlinna (GitHub
    #1009); a covered tunnel on a flat or coarse DEM is no underpass, or every street over it got a humped bridge.
    earthfast.gradesep_underpasses (the DEM read here, once)."""
    def ground(x, y):
        la, lo, _ = frame.enu_to_geodetic(x, y, 0.0)
        return np.asarray(_dem(md).sample(np.asarray(la), np.asarray(lo), DEM_ZOOM), np.float64)
    fast.require(238 if JOINED_ENDS else 169, "gradesep_underpasses")
    return np.asarray(fast._ef.gradesep_underpasses(
        np.ascontiguousarray(pts, np.float64).reshape(-1, 2), np.ascontiguousarray(starts, np.int64),
        np.ascontiguousarray(kinds, np.int64), np.ascontiguousarray(groups, np.int64),
        [TOUCH_M, END_M, PORTAL_OUT_M, SHORT_ROAD_TUNNEL_M, UNDERPASS_COVER_M, roads.TUNNEL_OPEN_COVER, SEP_M]
        + ([1.0] if JOINED_ENDS else []), list(CUT_SIDE_M), ground), bool)


def _dem(md):
    d = getattr(md, "dem", None)
    if d is None:
        import dem as demmod
        d = md.dem = demmod.Dem(os.path.join(md.cache_dir, "dem"), fetcher=md.fetcher)
    return d
