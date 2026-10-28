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

import numpy as np

import fast
import mapcells
import roads
import railways

log = logging.getLogger("earthd.gradesep")

# Off by default (2026-10-27 in-game A/B on fresh caches): every cell whose level ranges are asked reads its eight
# neighbours, and grading asks a ring of cells round a road tile, so a first visit read about twice the roads cells
# (PC: +7-12 s to the first road tile; Steam Deck, no warm-up threads: first road tile 141 s against 91 s off).
# BNE_GRADESEP=on turns it on.
ON = os.environ.get("BNE_GRADESEP", "off") == "on"
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
                got = memo["spans"] = _spans(md, key, feats) if ON else {}
        return got
    return run


_POOL = None
_POOL_LOCK = threading.Lock()


def _neighbours(md, key):
    """The roads layers of the eight cells round key, asked at once (one after another, cold, they held a fresh
    place's first road tile ~7 s: 2026-10-27 A/B)."""
    global _POOL
    keys = [mapcells.CellKey(key.z, (key.x + dx) % key.n, key.y + dy)
            for dy in (-1, 0, 1) for dx in (-1, 0, 1) if (dx or dy) and 0 <= key.y + dy < key.n]
    with _POOL_LOCK:
        if _POOL is None:
            from concurrent.futures import ThreadPoolExecutor
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
    L = roads._feature_length_m(feats, i) if rngs else 0.0
    out, at = [], 0.0
    for a, b, k in rngs:
        if a - at > roads.MIN_PART:
            out.append((kind, at, a, False))
        if k == "bridge":
            out.append((None, a, b, True))
        elif kind == 1 and (b - a) * L < railways.SHORT_TUNNEL_M:
            out.append((2, a, b, False))
        elif kind == 0:
            out.append((3, a, b, False))        # (any length: _underpasses measures the tunnel it is part of)
        at = max(at, b)
    if 1.0 - at > roads.MIN_PART:
        out.append((kind, at, 1.0, False))
    return out


def _spans(md, key, feats):
    if feats is None or not len(feats) or "subtype" not in feats.attrs:
        return {}
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
    own = {str(f): i for i, f in enumerate(feats.ids)}
    fb = feats.boxes(frame)
    ok = np.isfinite(fb).all(axis=1)
    if not ok.any():
        return {}
    box = (fb[ok, 0].min() - NEAR_M, fb[ok, 1].min() - NEAR_M, fb[ok, 2].max() + NEAR_M, fb[ok, 3].max() + NEAR_M)
    seen, rows = set(), []           # (fid, feats, row)
    for f in [feats] + others:
        if f is None or not len(f) or "subtype" not in f.attrs:
            continue
        bx = f.boxes(frame)
        for i in range(len(f)):
            fid = str(f.ids[i])
            if fid in seen or bx[i, 2] < box[0] or bx[i, 0] > box[2] or bx[i, 3] < box[1] or bx[i, 1] > box[3]:
                continue
            seen.add(fid)
            rows.append((fid, f, i))
    rows.sort(key=lambda r: r[0])   # (the same order whichever cell asks)
    lines, kinds, halves, groups, owner, decks = [], [], [], [], [], []
    enu = {}                        # id(feats) -> its coordinates in frame (one conversion a layer)
    for g, (fid, f, i) in enumerate(rows):
        kind, half, pieces = _row(f, i)
        if kind is None:
            continue
        allxy = enu.get(id(f))
        if allxy is None:
            e, n, _ = frame.geodetic_to_enu(f.lonlat[:, 1], f.lonlat[:, 0], 0.0)
            allxy = enu[id(f)] = np.stack([np.asarray(e, np.float64), np.asarray(n, np.float64)], 1)
        for part in f.parts(i, allxy):
            for ring in part:
                if len(ring) < 2:
                    continue
                for k, a, b, deck in pieces:
                    xy = ring if (a, b) == (0.0, 1.0) else _cut(ring, a, b)
                    if len(xy) < 2:
                        continue
                    if deck:
                        decks.append((xy, g))
                        continue
                    lines.append(xy)
                    kinds.append(k)
                    halves.append(half)
                    groups.append(g)
                    owner.append((own.get(fid), a, b))   # (a row of this cell's, or None)
    try:
        keep = _underpasses(md, frame, lines, kinds, groups)
    except Exception as e:
        log.debug("gradesep %s underpasses: %s: %s", key, type(e).__name__, e)
        revisit.fault()
        return {}
    lines, kinds, halves, groups, owner = ([v[n] for n in range(len(lines)) if keep[n]]
                                           for v in (lines, kinds, halves, groups, owner))
    if not lines:
        return {}
    de = np.zeros((2 * len(decks), 3))         # (the decks' ends and lengths)
    for d, (xy, _g) in enumerate(decks):
        de[2 * d, :2], de[2 * d + 1, :2] = xy[0], xy[-1]
        de[2 * d:2 * d + 2, 2] = float(np.hypot(*np.diff(xy, axis=0).T).sum())
    fast.require(86, "gradesep_candidates")
    pts = np.ascontiguousarray(np.vstack(lines))
    starts = np.r_[0, np.cumsum([len(x) for x in lines])].astype(np.int64)
    kinds = np.asarray(kinds, np.int64)
    halves = np.asarray(halves, np.float64)
    cand, smp = fast._ef.gradesep_candidates(pts, starts, kinds, halves, np.asarray(groups, np.int64),
                                             np.ascontiguousarray(de), np.array(
        [END_M, SAMPLE_GAP_M, SAMPLE_STEP_M, MIN_SIN, TOUCH_M, GAP_MAX_M, TURN_COS, GAP_SHARE], np.float64))
    out, why = {}, []                   # (why: (row, from, to, how) for probes, feats._gradesep_why)
    for ln, k in enumerate(kinds):      # the underpasses: ground under the road now bridging them
        i, a, b = owner[ln]
        if k == 3 and i is not None:
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
        i, a, b = owner[ln]
        L = float(np.hypot(*np.diff(lines[ln], axis=0).T).sum())
        if i is None or L <= 0 or s1 - s0 <= 0:
            continue
        fa = round(a + (b - a) * float(s0) / L, 6)
        fbb = round(a + (b - a) * float(s1) / L, 6)
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


def _underpasses(md, frame, lines, kinds, groups):
    """keep per line: False for a road's short tunnel (kind 3) that is no underpass: none of the other ground lines
    crosses over it away from their ends, or the ground there stands UNDERPASS_COVER_M or more over the ground
    PORTAL_OUT_M past its portals (a short tunnel through a hill with a road over it). OSM maps an underpass as the
    lower road's tunnel under a road on the ground; built as a tunnel, its vault and soil stood over the motorway on
    top (the A7 at Brackel and at Schuby, GitHub #228 / #243)."""
    keep = [True] * len(lines)
    cand = [n for n, k in enumerate(kinds) if k == 3]
    if not cand:
        return keep
    # (the whole tunnel: Overture splits one at every node, a 2.6 km motorway tunnel into 50 m pieces)
    lens = {c: float(np.hypot(*np.diff(lines[c], axis=0).T).sum()) for c in cand}
    ends = np.array([[lines[c][0], lines[c][-1]] for c in cand], np.float64)       # (n, 2, 2)
    parent = list(range(len(cand)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for p in range(len(cand)):
        d = np.hypot(ends[:, :, None, 0] - ends[p, None, None, :, 0], ends[:, :, None, 1] - ends[p, None, None, :, 1])
        for q in np.flatnonzero((d < TOUCH_M).any(axis=(1, 2))):
            parent[find(int(q))] = find(p)
    total = {}
    for p, c in enumerate(cand):
        total[find(p)] = total.get(find(p), 0.0) + lens[c]
    for p, c in enumerate(cand):
        if total[find(p)] >= SHORT_ROAD_TUNNEL_M:
            keep[c] = False
    cand = [c for c in cand if keep[c]]
    if not cand:
        return keep
    over = [n for n, k in enumerate(kinds) if k in (0, 1)]
    hits = roads._crossings_by([lines[n] for n in over], [lines[c] for c in cand]) if over else [[] for _ in cand]
    ask, at = [], []                # (DEM points, (candidate, crossings' slice, portals' slice))
    for c, cr in zip(cand, hits):
        pts = []
        for pi, _arc, pt in cr:
            o = over[pi]
            ends = (lines[o][0], lines[o][-1])
            if groups[o] != groups[c] and min(np.hypot(*(e - pt)) for e in ends) >= END_M:
                pts.append(np.asarray(pt, np.float64))
        if not pts:
            keep[c] = False
            continue
        xy = lines[c]
        u0 = xy[0] - xy[1]
        u1 = xy[-1] - xy[-2]
        portals = [xy[0] + PORTAL_OUT_M * u0 / max(np.hypot(*u0), 1e-9),
                   xy[-1] + PORTAL_OUT_M * u1 / max(np.hypot(*u1), 1e-9)]
        at.append((c, len(ask), len(ask) + len(pts)))
        ask += pts + portals
    if ask:
        P = np.asarray(ask, np.float64)
        la, lo, _ = frame.enu_to_geodetic(P[:, 0], P[:, 1], 0.0)
        z = np.asarray(_dem(md).sample(np.asarray(la), np.asarray(lo), DEM_ZOOM), np.float64)
        for c, i0, i1 in at:
            if not (np.nanmax(z[i0:i1]) - np.nanmin(z[i1:i1 + 2]) < UNDERPASS_COVER_M):
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
