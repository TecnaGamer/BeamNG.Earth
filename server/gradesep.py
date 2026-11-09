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
import math
import os
import threading
import weakref

import numpy as np

import fast
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
VERSION = 1             # in MapData.versions(): every cell's ranges (cell_facts) and tiles made again on a change
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
    refs = None         # per key: a weak reference to its roads layer, or None (none there)

    def run():
        nonlocal refs
        with lock:
            if refs is None:
                got = _neighbours(md, key, keys)
            else:
                got = [r() if r is not None else None for r in refs]
                gone = [i for i, r in enumerate(refs) if r is not None and got[i] is None]
                if gone:
                    for i, f in zip(gone, _neighbours(md, key, [keys[i] for i in gone])):
                        got[i] = f
            refs = [weakref.ref(f) if f is not None else None for f in got]
            return got
    return run


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
    """(kind, half width m) of feature i as a crossing line: 0 a drawn road, 1 a drawn railway; None neither."""
    sub = str(feats.attrs["subtype"][i]) if "subtype" in feats.attrs else "road"
    if sub == "rail":
        return (1, RAIL_HALF_M) if railways.drawn(feats, i) is not None else None
    if sub != "road":
        return None
    cls = str(feats.attrs["class"][i]) if "class" in feats.attrs else "unknown"
    st = roads.STYLE.get(cls)
    if st is None or roads.unbuilt(feats, i):
        return None
    return 0, st[0] / 2.0


def _row(feats, i):
    """(kind, half width, _pieces) of feature i, (None, None, None) for no crossing line; kept on the decoded layer
    (a cell's neighbours are read again for each of the nine cells round them)."""
    memo = feats.__dict__.get("_gs_rows")
    if memo is None:
        memo = feats.__dict__.setdefault("_gs_rows", {})
    got = memo.get(i)
    if got is None:
        kh = _kind_half(feats, i)
        got = memo[i] = (None, None, None) if kh is None else (kh[0], kh[1], _pieces(feats, i, kh[0]))
    return got


def _pieces(feats, i, kind):
    """[(kind, from, to, deck)] of feature i by its map ranges: its ground pieces (kind), a railway's short tunnels
    (2), a road's (3: an underpass if a line crosses over it close above, _underpasses), and its bridges (deck True:
    their ends are deck ends)."""
    rngs = roads.tag_level_ranges(feats, i)
    L = None                # (the railway's length, measured when first needed: a road never needs it)
    out, at = [], 0.0
    for a, b, k in rngs:
        if a - at > roads.MIN_PART:
            out.append((kind, at, a, False))
        if k == "bridge":
            out.append((None, a, b, True))
        elif kind == 1:
            if L is None:
                L = roads._feature_length_m(feats, i)
            if (b - a) * L < railways.SHORT_TUNNEL_M:
                out.append((2, a, b, False))
        elif kind == 0:
            out.append((3, a, b, False))        # (any length: _underpasses measures the tunnel it is part of)
        at = max(at, b)
    if 1.0 - at > roads.MIN_PART:
        out.append((kind, at, 1.0, False))
    return out


def _layer_rows(f):
    """Every feature's _row of a layer as arrays, once per decoded layer: (kind (-1: no crossing line), half width,
    piece offsets, piece kinds (-1: a deck), from, to, the ids as str)."""
    got = f.__dict__.get("_gs_layer")
    if got is None:
        n = len(f)
        kinds, halves = np.full(n, -1, np.int64), np.zeros(n)
        poff, pk, pa, pb = [0], [], [], []
        if "subtype" in f.attrs:
            # (_kind_half for the whole layer: a road's style by its class, unbuilt as an array; the few railways one at
            # a time. _row per feature was 1.5 s of two cells' planning, 2026-11-02)
            def strs(col):
                return [str(v) for v in (col.tolist() if hasattr(col, "tolist") else col)]
            subs = strs(f.attrs["subtype"])
            clss = strs(f.attrs["class"]) if "class" in f.attrs else ["unknown"] * n
            unb = roads.unbuilt_mask(f)
            for i in range(n):
                if subs[i] == "road":
                    st = roads.STYLE.get(clss[i])
                    if st is None or unb[i]:
                        continue
                    kinds[i], halves[i] = 0, st[0] / 2.0
                elif subs[i] == "rail" and railways.drawn(f, i) is not None:
                    kinds[i], halves[i] = 1, RAIL_HALF_M
            tags = roads._tag_ranges_list(f)
            for i in range(n):
                k = int(kinds[i])
                if k >= 0:
                    if not tags[i]:             # (no level range: the whole line, as _pieces makes it)
                        pk.append(k)
                        pa.append(0.0)
                        pb.append(1.0)
                    else:
                        for kk, a, b, deck in _pieces(f, i, k):
                            pk.append(-1 if deck else kk)
                            pa.append(a)
                            pb.append(b)
                poff.append(len(pk))
        else:
            poff += [0] * n
        got = f.__dict__["_gs_layer"] = (kinds, halves, np.asarray(poff, np.int64), np.asarray(pk, np.int64),
                                         np.asarray(pa, np.float64), np.asarray(pb, np.float64),
                                         [str(x) for x in f.ids])
    return got


def _metric(f, frame, i):
    """Feature i's one ring's arcs (sub_polyline's metric: the level ranges' fractions, lon scaled by the cosine of its
    mean latitude; and the frame's), or None for no single ring of 2 points or more. The gradesep kernels measure along
    the frame's east/north; the ranges they read and give are sub_polyline's: on a 15 km motorway the two put one point
    8.7 m apart, its deck off the road under it (the A4 at Glaireuse, #1020)."""
    po, ro, co = f.part_off, f.ring_off, f.coord_off
    rings = [(int(co[r]), int(co[r + 1])) for p in range(int(po[i]), int(po[i + 1]))
             for r in range(int(ro[p]), int(ro[p + 1])) if int(co[r + 1]) - int(co[r]) >= 2]
    if len(rings) != 1:
        return None
    a, b = rings[0]
    ll = np.asarray(f.lonlat[a:b, :2], np.float64)
    xy = f.xy_in(frame)[a:b]
    c = math.cos(math.radians(float(np.mean(ll[:, 1]))))
    ss = np.r_[0.0, np.cumsum(np.hypot(np.diff(ll[:, 0]) * c, np.diff(ll[:, 1])))]
    se = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
    if ss[-1] <= 0 or se[-1] <= 0:
        return None
    return ss / ss[-1], se / se[-1]


def _to_frame(f, frame, r):
    """Layer rows r (_layer_rows) with their pieces' fractions in the frame's metric (_metric), kept per frame."""
    memo = f.__dict__.setdefault("_gs_frame_pab", {})
    key = (frame.lat0, frame.lon0)
    got = memo.get(key)
    if got is None:
        pa, pb = r[4].copy(), r[5].copy()
        inner = np.flatnonzero(((pa > 0.0) & (pa < 1.0)) | ((pb > 0.0) & (pb < 1.0)))
        if len(inner):
            poff = r[2]
            for i in np.unique(np.searchsorted(poff, inner, side="right") - 1).tolist():
                m = _metric(f, frame, i)
                if m is not None:
                    q = slice(int(poff[i]), int(poff[i + 1]))
                    pa[q], pb[q] = np.interp(pa[q], m[0], m[1]), np.interp(pb[q], m[0], m[1])
        if len(memo) >= 4:
            memo.clear()
        got = memo[key] = (pa, pb)
    return got


def _layer_ids(f):
    """(the layer's ids as a numpy str array, True when they ascend strictly), kept on the decoded layer."""
    got = f.__dict__.get("_gs_ids")
    if got is None:
        ids = _layer_rows(f)[6]
        u = f.ids if getattr(f.ids, "dtype", None) is not None and f.ids.dtype.kind == "U" else np.array(ids, dtype=str)
        got = f.__dict__["_gs_ids"] = (u, bool(len(u) < 2 or (u[1:] > u[:-1]).all()))
    return got


def _rows(frame, lays):
    """_spans' rows: the features of lays (the cell's own layer first) whose boxes in frame come within NEAR_M of the
    own layer's box, each id once (the first layer and row it is in), in the order of their ids (the same order
    whichever cell asks): (layer index, row) arrays and the ids; None when the own layer has no box.
    earthfast.gradesep_select (Features.boxes and a Python loop over the rows were ~1/4 of _spans, 2026-11-08)."""
    fast.require(169, "gradesep_select")
    got = fast._ef.gradesep_select([(f.xy_in(frame), np.ascontiguousarray(f.coord_off, np.int64),
                                     np.ascontiguousarray(f.ring_off, np.int64),
                                     np.ascontiguousarray(f.part_off, np.int64)) for f in lays], NEAR_M)
    if got is None:
        return None
    off, sel = got
    lay = np.repeat(np.arange(len(lays), dtype=np.int64), np.diff(off))
    if not len(sel):
        return lay, sel, np.zeros(0, str)
    fids = np.concatenate([_layer_ids(f)[0][sel[off[k]:off[k + 1]]] for k, f in enumerate(lays)])
    order = np.argsort(fids, kind="stable")             # (stable: each id's first copy leads)
    s = fids[order]
    first = order[np.r_[True, s[1:] != s[:-1]]]
    return lay[first], sel[first], fids[first]


def _owners(feats, row_layer, row, fids):
    """Per row (row_layer, row, fids: _rows') the row in feats (lays[0]) with its id (the last one), -1 where none:
    one of feats' own rows is itself (its ids ascend strictly), another layer's is looked up."""
    u, ascending = _layer_ids(feats)
    if not ascending:
        own = {str(f): i for i, f in enumerate(feats.ids)}
        return np.array([own.get(str(f), -1) for f in fids], np.int64)
    out = np.where(row_layer == 0, row, -1).astype(np.int64)
    o = np.flatnonzero(row_layer != 0)
    if len(o):
        k = np.searchsorted(u, fids[o])
        hit = k < len(u)
        hit[hit] = u[k[hit]] == fids[o][hit]
        out[o] = np.where(hit, k, -1)
    return out


def _spans(md, key, feats):
    if feats is None or not len(feats) or "subtype" not in feats.attrs:
        return {}
    if not ON and not any(p[0] in (2, 3) for i in range(len(feats)) for p in (_row(feats, i)[2] or ())):
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
    row_layer, row, fids = got
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
    oi = _owners(feats, row_layer, row, fids)[groups]                   # (each line's row in the cell's own layer, -1: none)
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
    metric = {}

    def back(i, x):                     # (a fraction in the frame's metric as the level ranges measure it: _metric)
        if i not in metric:
            metric[i] = _metric(feats, frame, i)
        m = metric[i]
        return x if m is None or x <= 0.0 or x >= 1.0 else float(np.interp(x, m[1], m[0]))
    for ln in np.flatnonzero((kinds == 3) & (oi >= 0)).tolist():   # the underpasses: ground under the road bridging them
        i, a, b = oi_l[ln], back(oi_l[ln], oa_l[ln]), back(oi_l[ln], ob_l[ln])
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
    for ln, s0, s1, how in sp:
        ln = int(ln)
        i, a, b = oi_l[ln], oa_l[ln], ob_l[ln]
        L = float(np.hypot(*np.diff(pts[starts[ln]:starts[ln + 1]], axis=0).T).sum())
        if i < 0 or L <= 0 or s1 - s0 <= 0:
            continue
        fa = round(back(i, a + (b - a) * float(s0) / L), 6)
        fbb = round(back(i, a + (b - a) * float(s1) / L), 6)
        out.setdefault(i, []).append((fa, fbb, "bridge"))
        why.append((i, fa, fbb, int(how)))
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


class _LineList:
    """lines[n]: pts rows starts[n]..starts[n + 1] (a view), without a list of every line."""

    def __init__(self, pts, starts):
        self.pts, self.starts = pts, starts.tolist()

    def __getitem__(self, n):
        return self.pts[self.starts[n]:self.starts[n + 1]]


def _crossings(lines, over, cand):
    """roads._crossings_by([lines[n] for n in over], [lines[c] for c in cand]) from lines' points as they are (the
    over lines picked by a mask, not stacked again one by one)."""
    fast.require(76, "segment_crossings_g")
    st = np.asarray(lines.starts, np.int64)
    n = np.diff(st)
    sel = np.zeros(len(n), bool)
    sel[over] = True
    lp = np.ascontiguousarray(lines.pts[np.repeat(sel, n)])
    gp = [lines[c] for c in cand]
    out = [[] for _ in gp]
    for pi, g, arc, x, y in fast._ef.segment_crossings_g(
            lp, np.r_[0, np.cumsum(n[over])].astype(np.int64), np.ascontiguousarray(np.vstack(gp)),
            np.r_[0, np.cumsum([len(p) for p in gp])].astype(np.int64)):
        out[g].append((pi, arc, np.array([x, y])))
    return out


def _underpasses(md, frame, pts, starts, kinds, groups):
    """keep per line (pts rows starts[n]..starts[n + 1]; kinds, groups arrays): False for a road's tunnel (kind 3)
    that is no underpass: none of the other ground lines crosses over it away from their ends, or the ground there
    stands UNDERPASS_COVER_M or more (roads.TUNNEL_OPEN_COVER for a tunnel of SHORT_ROAD_TUNNEL_M or longer) over the
    ground PORTAL_OUT_M past its portals (a short tunnel through a hill with a road over it). OSM maps an underpass as the
    lower road's tunnel under a road on the ground; built as a tunnel, its vault and soil stood over the motorway on
    top (the A7 at Brackel and at Schuby, GitHub #228 / #243)."""
    keep = np.ones(len(kinds), bool)
    cand = np.flatnonzero(kinds == 3).tolist()
    if not cand:
        return keep
    lines = _LineList(pts, starts)
    # (the whole tunnel: Overture splits one at every node, a 2.6 km motorway tunnel into 50 m pieces)
    lens = {c: float(np.hypot(*np.diff(lines[c], axis=0).T).sum()) for c in cand}
    ends = np.array([[lines[c][0], lines[c][-1]] for c in cand], np.float64)       # (n, 2, 2)
    parent = list(range(len(cand)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    # (pieces touching end to end joined through a TOUCH_M grid of their ends: every piece against every other was
    # ~0.9 CPU-s of a cold Munich road tile, 2026-11-09)
    grid = {}
    for p in range(len(cand)):
        for e in ends[p]:
            gx, gy = int(math.floor(e[0] / TOUCH_M)), int(math.floor(e[1] / TOUCH_M))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for q, f in grid.get((gx + dx, gy + dy), ()):
                        if math.hypot(e[0] - f[0], e[1] - f[1]) < TOUCH_M:
                            parent[find(q)] = find(p)
            grid.setdefault((gx, gy), []).append((p, e))
    total = {}
    for p, c in enumerate(cand):
        total[find(p)] = total.get(find(p), 0.0) + lens[c]
    # a longer one is an underpass only with the ground over it so low that it would be opened anyway
    # (roads.TUNNEL_OPEN_COVER: a road in the open under a road the map draws on the ground): Highway 3 under Paasikiventie
    # and the Goodman centre in Hameenlinna (GitHub #1009), tunnel pieces over 80 m in all in a cutting the DEM shows
    # open under the bridges; opened with no bridge over it, the crossing roads were graded down into the cutting
    # (and only in a cutting the DEM shows: the ground of the line over it SEP_M higher either side of the crossing; a
    # covered tunnel on a flat or coarse DEM is no underpass, or every street over it got a humped bridge)
    long = {c for p, c in enumerate(cand) if total[find(p)] >= SHORT_ROAD_TUNNEL_M}
    cover = {c: (roads.TUNNEL_OPEN_COVER if c in long else UNDERPASS_COVER_M) for c in cand}
    if not cand:
        return keep
    over = np.flatnonzero((kinds == 0) | (kinds == 1))
    if len(over):                   # (only the lines whose box meets a candidate's: the crossing kernel tries every pair)
        st = np.asarray(lines.starts, np.int64)
        lo = np.minimum.reduceat(lines.pts[:, :2], st[:-1]) if len(lines.pts) else np.zeros((0, 2))
        hi = np.maximum.reduceat(lines.pts[:, :2], st[:-1]) if len(lines.pts) else np.zeros((0, 2))
        cb = np.array([[lo[c, 0], lo[c, 1], hi[c, 0], hi[c, 1]] for c in cand], np.float64)
        ob = np.c_[lo[over], hi[over]]
        meet = ((ob[:, None, 0] <= cb[None, :, 2] + 1.0) & (ob[:, None, 2] >= cb[None, :, 0] - 1.0)
                & (ob[:, None, 1] <= cb[None, :, 3] + 1.0) & (ob[:, None, 3] >= cb[None, :, 1] - 1.0)).any(axis=1)
        over = over[meet]
    hits = _crossings(lines, over, cand) if len(over) else [[] for _ in cand]
    over = over.tolist()
    ask, at = [], []                # (DEM points, (candidate, crossings' slice, portals' slice))
    for c, cr in zip(cand, hits):
        pts, owners = [], []
        for pi, _arc, pt in cr:
            o = over[pi]
            ends = (lines[o][0], lines[o][-1])
            if groups[o] != groups[c] and min(np.hypot(*(e - pt)) for e in ends) >= END_M:
                pts.append(np.asarray(pt, np.float64))
                owners.append(o)
        if not pts:
            keep[c] = False
            continue
        xy = lines[c]
        u0 = xy[0] - xy[1]
        u1 = xy[-1] - xy[-2]
        portals = [xy[0] + PORTAL_OUT_M * u0 / max(np.hypot(*u0), 1e-9),
                   xy[-1] + PORTAL_OUT_M * u1 / max(np.hypot(*u1), 1e-9)]
        sides = []
        if c in long:               # (the line over it, CUT_SIDE_M either way along it from each crossing)
            for pt, o in zip(pts, owners):
                ol = lines[o]
                k = int(np.argmin(np.hypot(*(0.5 * (ol[:-1] + ol[1:]) - pt).T))) if len(ol) > 1 else 0
                t = ol[min(k + 1, len(ol) - 1)] - ol[k]
                t = t / max(float(np.hypot(*t)), 1e-9)
                sides += [pt + d * t for d in CUT_SIDE_M] + [pt - d * t for d in CUT_SIDE_M]
        at.append((c, len(ask), len(ask) + len(pts), len(sides)))
        ask += pts + portals + sides
    if ask:
        P = np.asarray(ask, np.float64)
        la, lo, _ = frame.enu_to_geodetic(P[:, 0], P[:, 1], 0.0)
        z = np.asarray(_dem(md).sample(np.asarray(la), np.asarray(lo), DEM_ZOOM), np.float64)
        for c, i0, i1, ns in at:
            if not (np.nanmax(z[i0:i1]) - np.nanmin(z[i1:i1 + 2]) < cover[c]):
                keep[c] = False
            elif ns:                # (a longer one: in a cutting at one of its crossings at least)
                zs = z[i1 + 2:i1 + 2 + ns].reshape(-1, 2, len(CUT_SIDE_M))     # (crossing, side, distance)
                if not (np.nanmin(np.nanmax(zs, axis=2), axis=1) - z[i0:i1] >= SEP_M).any():
                    keep[c] = False
    return keep


def _cut(xy, a, b):
    """The part of polyline xy between fractions a < b of its length."""
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
    if s[-1] <= 0:
        return xy[:1]
    sa, sb = a * s[-1], b * s[-1]
    pa = [np.interp(sa, s, xy[:, 0]), np.interp(sa, s, xy[:, 1])]
    pb = [np.interp(sb, s, xy[:, 0]), np.interp(sb, s, xy[:, 1])]
    return np.vstack([pa, xy[(s > sa) & (s < sb)], pb])


def _dem(md):
    d = getattr(md, "dem", None)
    if d is None:
        import dem as demmod
        d = md.dem = demmod.Dem(os.path.join(md.cache_dir, "dem"), fetcher=md.fetcher)
    return d
