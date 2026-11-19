"""Road revamp v2, "road shape first" (.claude/work/road-revamp-v2-road-shape-first-2026-11-19.md): the glue of the
Rust kernels in native/earthfast/src/roadshape. Switch BNE_ROADSHAPE (off / decks; later sheet, profile, full).

Phase P1b (earthfast 498, BNE_ROADSHAPE=decks; the user's go 2026-11-19 on banked decks and buried deck ends):
curved, skewed decks with buried ends for the SIMPLE structures. In a bridge chain's solve (roads.BridgeProfiles.
_chain_solve, shape_chain) each deck asks part_simple, the rule grading's bank_through asks too: its structure group
(structures.groups, S1's edge table, height free) read from the map round each of its ends (end_simple, bounded);
a group of road decks with no END_ON / OVERLAP edge whose JOIN edges form a path (earthfast.deck_simple: one deck or
twins, each perhaps in pieces end to end; plain overpasses, twins, mountain bridges) is simple. (The design said "one
or two members": roads._skew_decks carries a short bridge that is a segment of its own onto the roads at its ends, so
a plain overpass is three members joined end to end.) Each of its decks gets (earthfast.deck_shape):
- its line from the road's smooth line: each end with a road going on from it leaves along that road's direction (a
  cubic blend over up to BLEND_MAX_M), rows by the chord rule (CHORD_TOL_M, MAX_STEP_M). The cause it removes
  (KGBXY1DG, Baring Creek 48.67835 -113.59579): the map's bridge is one straight 21 m chord whose end vertex stays a
  9.6 degree corner (roadsmooth pins both vertices of a piece holding a connector, a side road 6 m before the deck),
  so the slab ran straight across a curving road;
- skewed end lines along the road or railway it crosses near each end (BNE_DECK_SKEW, on with the switch): the end
  rows of its slab, its road surface and its parapets (roadmesh.ribbons), and the approach's end row (roadmesh.build's
  deck-end weld);
- buried ends: OVERLAP_M of its approach under a skirt that continues the slab and girders, its top SKIRT_TOP_M under
  the road at the abutment and SKIRT_DIP_M at the far end, its soffit up to a SKIRT_LIP_M lip (roadmesh.deck), in place
  of a step;
- its parapets follow the curved edges (they are the slab's edges).
Every other deck is drawn as before. The shape is published with the part (done, piers along the new line) and taken
over with it by every store (_adopt): every tile, the AI graph and the far bridges read one line per deck.

Off (the default): no code of this module runs; plans, facts and tiles are byte-identical. The switch is a BNE_*
build switch (revisit.keyed_env: in the plans', facts' and tiles' keys) with its version in revisit.SWITCH_VERSIONS."""
import math
import os

import numpy as np
import dmath
import dnp

MODE = os.environ.get("BNE_ROADSHAPE", "off")
DECKS = MODE in ("decks",)
SKEW = DECKS and os.environ.get("BNE_DECK_SKEW", "on") not in ("off", "0", "false")

CHORD_TOL_M = 0.01          # m: a row piece's largest departure from the curve (the design's 2 cm chord rule, less the
                            # tiles' own thinning of the rows, ROWS_SIMPLIFY_M)
ROWS_SIMPLIFY_M = 0.005     # m: roads._deck_rows' RDP tolerance on a shaped deck's rows (its collinear rows only)
MAX_STEP_M = 4.0            # m between rows at most
BLEND_MAX_M = 30.0          # m of a deck re-shaped from an end at most
APPROACH_DEG = 25.0         # a road going on from a deck's end within this of the deck: its approach
JOIN_M = 0.75               # m: a line's end this near a deck's end goes on from it (roads.GAP_JOIN_M)
OVERLAP_M = 10.0            # m of its approach a buried end runs over (the design's 10-25 m, vanilla Italy's 25 m)
SKEW_REACH_M = 40.0         # m along the deck from its end to the crossing it is the end of (structprobe's)
SKEW_MIN_CROSS_DEG = 15.0   # a line meeting the deck flatter than this runs along it (structprobe's)
SKEW_MAX_DEG = 60.0         # an end line at most this off square (clamped)
SKEW_BEND_DEG = 30.0        # the deck turning more between the crossing and its end: square (the design: no skew on decks
                            # curving more than 30 degrees)
DEV_MIN_M = 0.5             # m a blend may leave the deck's mapped line in any case ...
DEV_SHARE = 0.1             # ... or this share of its length (more: the road going on is another's)
AMBIG_DEG = 10.0            # two roads going on within this of each other from a deck's end: a fork, no approach
DIR_M = 10.0                # m of a road its direction is read over
SKEW_NEAR_M = 1.0           # m: a crossing nearer the end than this joins it (structprobe's)
NEAR_M = 60.0               # m round a deck the lines are offered to the kernel from
SKIRT_TOP_M = 0.05          # m a buried end's top lies under the deck's top at the abutment ...
SKIRT_DIP_M = 0.45          # ... and at its far end (the design's 0.3-0.5 m)
SKIRT_LIP_M = 0.3           # m of slab at its far end (the soffit pulled up to a lip)
SKIRT_GRADE_MAX = 0.08      # the deck's end grade carried over the skirt at most

PARAMS = np.array([CHORD_TOL_M, MAX_STEP_M, BLEND_MAX_M, dmath.cos(math.radians(APPROACH_DEG)), JOIN_M, OVERLAP_M,
                   SKEW_REACH_M, dmath.sin(math.radians(SKEW_MIN_CROSS_DEG)), dmath.cos(math.radians(SKEW_MAX_DEG)),
                   dmath.cos(math.radians(SKEW_BEND_DEG)), DEV_MIN_M, DEV_SHARE, dmath.cos(math.radians(AMBIG_DEG)),
                   DIR_M, SKEW_NEAR_M], np.float64)
VERSION = 498               # the earthfast kernels


def simple_members(members, got):
    """[bool] per member of structures.groups' members / output got: is its group simple (earthfast.deck_simple)."""
    import fast
    fast.require(VERSION, "deck_simple")
    if not members:
        return []
    return [bool(v) for v in fast._ef.deck_simple([int(r) for r in got["root"]], [int(d) for d in got["dup"]],
                                                  [(int(e[0]), int(e[1]), int(e[2])) for e in got["edges"]],
                                                  [int(m["kind"]) for m in members])]


# ---- which decks are simple: ONE rule for the deck and the plans (v2 P1b fix) --------------------------------------
# The deck's simplicity was decided from its whole bridge chain (shape_chain's groups) and grading's from the decks
# within 110 m of the road's end (Grader._deck_end_simple_at): where they disagreed a banked approach met a level deck
# (or the reverse), a cross-slope step at the abutment (Codex review of P1b). Grading plans before any chain is solved
# (the chain solves read the plans), so it cannot read the chain's record; both now take end_simple, a bounded local
# rule that is a function of the map alone (same function, same inputs: the cells' road decks from cell_decks, height
# free, grouped in a frame at the deck's own end), and a deck is simple when it is simple at BOTH its ends
# (part_simple; grading's bank_through asks the same pair for the deck the road ends on).
GROUP_MAX_M = 1000.0        # m: a simple structure's points' box diagonal at most (larger: not simple, its group not
                            # read whole round one end)
GROUP_MAX_N = 48            # members at most
END_CELLS_MAX = 32          # map cells end_simple may need (more: not simple)
COPY_PAD_M = 60.0           # m round a deck's ring its copies' cells are read in (grading.STRUCT_COPY_PAD_M)
_M_PER_DEG_LAT = 110574.0   # m per degree of latitude at least (pads in degrees: always at least the metres asked)
_M_PER_DEG_LON = 111320.0   # m per degree of longitude at the equator


def cell_decks(feats):
    """One map cell's road decks as end_simple reads them: {(fid, part key): (ring lon / lat (k, 2), spec, level,
    box [lon0, lat0, lon1, lat1])} (the parts, keys, specs and levels the bridge chains load: roads.BridgeProfiles'
    keys_of / parts_of / specs_of / levels_of; a key twice in a feature: its longest ring). Kept on the features when
    made without a fault (a spec rule's fallback answers this call alone)."""
    if feats is None or not len(feats):
        return {}
    st = feats.__dict__
    got = st.get("_roadshape_decks")
    if got is not None:
        return got
    import revisit
    import roads as roadsmod
    out = {}
    with revisit.scope() as b:
        bp = roadsmod.BridgeProfiles
        bs = bp.specs_of(feats)
        for i in range(len(feats)):
            if bs[i] is None:
                continue
            fid = str(feats.ids[i])
            lvls = bp.levels_of(feats, i)
            for j, (pk, ring) in enumerate(roadsmod._keyed(bp.keys_of, bp.parts_of, feats, i)):
                ring = np.ascontiguousarray(np.asarray(ring, np.float64)[:, :2])
                k = (fid, pk)
                if len(ring) < 2 or not np.isfinite(ring).all() or (k in out and len(ring) <= len(out[k][0])):
                    continue
                box = np.r_[ring.min(0), ring.max(0)]
                out[k] = (ring, bs[i], float(lvls[j]) if j < len(lvls) else 1.0, box)
    if b.complete:
        st["_roadshape_decks"] = out
    return out


def _skey(k):
    return (str(k[0]), str(k[1]))


def _better(v, old):
    """Of two copies of one deck (cell_decks' values): the one with the most points, then the least by its bytes (the
    same copy whatever cells were read first)."""
    return old is None or len(v[0]) > len(old[0]) or (len(v[0]) == len(old[0]) and v[0].tobytes() < old[0].tobytes())


def _pads(box, m):
    """(pad lat, pad lon) degrees covering m metres round the box [lon0, lat0, lon1, lat1]."""
    pl = m / _M_PER_DEG_LAT
    lat = min(89.9, max(abs(float(box[1])), abs(float(box[3]))) + pl)
    return pl, m / (_M_PER_DEG_LON * max(0.05, dmath.cos(math.radians(lat))))


def _reach(half):
    """m: structures.halo's reach of a deck of half width half (the farthest a deck one structure with it can lie)."""
    import structures
    return 1.1 * (min(float(half), structures.HALF_MAX_M) + structures.HALF_MAX_M + structures.TOUCH_M)


def _cells(box, m):
    import mapcells
    pl, po = _pads(box, m)
    return {str(c): c for c in mapcells.cells_for_bbox(float(box[1]) - pl, float(box[0]) - po, float(box[3]) + pl,
                                                       float(box[2]) + po)}


def end_simple(cell_fn, key, end, ll):
    """Is deck `key` (a (fid, part key) of cell_decks) simple as seen from its end `end` (0: its ring's first point,
    1: its last)? ll: (lon, lat) near that end (the caller's copy of it), where the reading starts. cell_fn(cell) -> the
    cell's roads features.

    Its group G: the structure group (structures.groups, height free: every deck at 0, levels as the chains load them)
    of the deck among the road decks near it, every deck read whole (every cell of its ring, COPY_PAD_M round it) and
    every cell within reach (structures.halo's) of each member read. Grown as a fixpoint: the group of the decks near
    the last group, until it holds still; each group so found is part of the true one (edges are pairwise and every
    deck is whole when grouped), so it only grows, and the fixpoint IS the true group whichever cells were read first
    (each member's partners all lie within its reach). True when G is simple (earthfast.deck_simple), at most
    GROUP_MAX_N members and GROUP_MAX_M across; False as soon as a part of it is over either, or when the cells its
    reading needs pass END_CELLS_MAX (the cells needed so far are always among the final set's: the same answer from
    any start). The grouping runs in a frame at the deck's own end (its whole ring's, from the map): one key and end,
    one answer, bit for bit, whoever asks. A pure function of the map round the structure."""
    import geodesy
    import roads as roadsmod
    import structures
    cells = {}                  # str(cell) -> cell_decks
    decks = {}                  # key -> its best copy over the cells read
    idx = [None]                # (keys sorted, their boxes (n, 4)) of decks

    def load(need):
        for s in sorted(need):
            if s in cells:
                continue
            got = cell_decks(cell_fn(need[s]))
            cells[s] = got
            for k, v in got.items():
                if _better(v, decks.get(k)):
                    decks[k] = v
                    idx[0] = None

    try:
        ll = (float(ll[0]), float(ll[1]))
        load(_cells(np.array([ll[0], ll[1], ll[0], ll[1]]), _reach(structures.HALF_MAX_M)))
        group = {key}
        for _ in range(END_CELLS_MAX + GROUP_MAX_N + 16):   # (never reached: each pass reads a cell or grows the group)
            if key not in decks:
                return False
            if idx[0] is None:
                ks = sorted(decks, key=_skey)
                idx[0] = (ks, np.array([decks[k][3] for k in ks], np.float64).reshape(-1, 4))
            ks, boxes = idx[0]
            # the decks within reach of a member (by their boxes in degrees: the same set from any frame)
            need = {}
            hit = np.zeros(len(ks), bool)
            for k in sorted(group, key=_skey):
                v = decks.get(k)
                if v is None:
                    continue
                r = _reach(float(v[1].width) / 2)
                pl, po = _pads(v[3], r)
                hit |= ((boxes[:, 0] <= v[3][2] + po) & (boxes[:, 2] >= v[3][0] - po)
                        & (boxes[:, 1] <= v[3][3] + pl) & (boxes[:, 3] >= v[3][1] - pl))
                need.update(_cells(v[3], r))
            near = [ks[i] for i in np.flatnonzero(hit)]
            for k in near:
                need.update(_cells(decks[k][3], COPY_PAD_M))
            if len(need) > END_CELLS_MAX:
                return False
            if any(s not in cells for s in need):
                load(need)
                continue
            ring = decks[key][0]
            p = ring[0] if end == 0 else ring[-1]
            fr = geodesy.EnuFrame(float(p[1]), float(p[0]))
            members = []
            for k in near:
                ring, spec, lv, _b = decks[k]
                e, n, _ = fr.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
                xy = roadsmod.densify(roadsmod._dedupe(np.stack([np.asarray(e, np.float64), np.asarray(n, np.float64)],
                                                                axis=1)), roadsmod.DECK_STEP)
                xy = np.asarray(xy, np.float64)[:, :2]
                members.append(structures._member(k, spec, xy, np.zeros(len(xy)), lv, None, False))
            got = structures.groups(members)
            t = near.index(key)
            root = [int(r) for r in got["root"]]
            now = {near[i] for i in range(len(near)) if root[i] == root[t]}
            allp = np.concatenate([members[i]["xy"] for i in range(len(near)) if root[i] == root[t]])
            span = allp.max(0) - allp.min(0)
            if len(now) > GROUP_MAX_N or float(dnp.hypot(span[0], span[1])) > GROUP_MAX_M:
                return False
            if now == group:
                return bool(simple_members(members, got)[t])
            group = now
    except ValueError:          # (cells_for_bbox: a deck over its cell limit; it only grows: the final answer too)
        return False
    return False


def part_simple(cell_fn, key, ll0, ll1):
    """Is deck `key` simple (end_simple at both its ends: ll0 / ll1 (lon, lat) near its first / last point)? The deck
    is shaped and banked (shape_chain) exactly when this holds, and grading keeps a banked road's bank onto it exactly
    then (Grader._deck_end_simple)."""
    return end_simple(cell_fn, key, 0, ll0) and end_simple(cell_fn, key, 1, ll1)


def deck_end_at(cell_fn, frame, xy, touch):
    """((fid, part key), end) of the road deck end nearest frame point xy within touch m (every deck near it read whole:
    each cell of its ring, COPY_PAD_M round it), its ring's ends ((lon, lat) x 2), or None. Ties: the least key."""
    la, lo, _ = frame.enu_to_geodetic(float(xy[0]), float(xy[1]), 0.0)
    q = np.array([float(lo), float(la), float(lo), float(la)])
    pl, po = _pads(q, touch)
    zone = (q[0] - po, q[1] - pl, q[2] + po, q[3] + pl)
    cells, decks = {}, {}
    need = _cells(q, touch)
    for _ in range(END_CELLS_MAX + 1):
        for s in sorted(need):
            if s not in cells:
                cells[s] = cell_decks(cell_fn(need[s]))
                for k, v in cells[s].items():
                    if _better(v, decks.get(k)):
                        decks[k] = v
        near = [k for k in sorted(decks, key=_skey) if decks[k][3][0] <= zone[2] and decks[k][3][2] >= zone[0]
                and decks[k][3][1] <= zone[3] and decks[k][3][3] >= zone[1]]
        more = dict(need)
        for k in near:
            more.update(_cells(decks[k][3], COPY_PAD_M))
        if len(more) > END_CELLS_MAX:
            return None
        if all(s in cells for s in more):
            break
        need = more
    else:
        return None
    best, bd = None, float(touch)
    for k in near:
        ring = decks[k][0]
        for e in (0, 1):
            p = ring[0] if e == 0 else ring[-1]
            pe, pn, _ = frame.geodetic_to_enu(float(p[1]), float(p[0]), 0.0)
            d = float(dnp.hypot(float(pe) - float(xy[0]), float(pn) - float(xy[1])))
            if d <= bd and (best is None or d < bd):
                best, bd = (k, e), d
    if best is None:
        return None
    ring = decks[best[0]][0]
    return best[0], best[1], (tuple(float(v) for v in ring[0]), tuple(float(v) for v in ring[-1])), decks[best[0]][1]


def shape_chain(comp, pool, zs, ground, simple_of=None):
    """{part key: shape} of the simple decks of a chain's solve: comp its parts' keys (its order), pool {key: (spec,
    xy, lon, level, ...)}, zs their heights (comp's order), ground the chain's ground {(fid, "g" | "r"): [lines]},
    simple_of(key) -> bool: is the deck simple (part_simple, the rule grading's bank_through reads too; None: none
    is). A shape: {"xy" (k, 2), "z" (k,), "skew": [unit end line (2,) or None] x 2, "ov": [overlap line (j, 2) or
    None] x 2, "tan": [the approach's kind or None] x 2, "bank"}. Pure given its inputs (sorted keys, one kernel call
    per deck)."""
    import fast
    import structures
    fast.require(VERSION, "deck_shape")
    if not comp:
        return {}
    members = []
    for k in comp:                              # (the duplicates below: one way kept twice under two part keys)
        v = pool[k]
        xy = np.asarray(v[1], np.float64)[:, :2]
        members.append(structures._member(k, v[0], xy, np.zeros(len(xy)), structures.level_of(v), None, False))
    got = structures.groups(members)
    simple = [simple_of is not None and bool(simple_of(k)) for k in comp]
    dup = [int(d) for d in got["dup"]]          # (one way kept twice under two part keys: never the other's joint)
    glines = []                                 # (xy (k, 2), kind, fid) in the ground's keys' order
    for gk in sorted(ground, key=str):
        if len(gk) != 2 or gk[1] not in ("g", "r"):
            continue
        for ln in ground[gk]:
            ln = np.asarray(ln, np.float64)
            if ln.ndim == 2 and len(ln) >= 2:
                glines.append((ln[:, :2], 0 if gk[1] == "g" else 1, str(gk[0])))
    out = {}
    for n, k in enumerate(comp):
        if not simple[n]:
            continue
        xy = np.ascontiguousarray(np.asarray(pool[k][1], np.float64)[:, :2])
        z = np.ascontiguousarray(np.asarray(zs[n], np.float64).reshape(-1))
        if len(xy) < 2 or len(z) != len(xy) or not np.isfinite(z).all():
            continue
        lo, hi = xy.min(0) - NEAR_M, xy.max(0) + NEAR_M
        lines, kinds, owns = [], [], []
        for ln, kind, fid in glines:
            if (ln.max(0) < lo).any() or (ln.min(0) > hi).any():
                continue
            lines.append(ln)
            kinds.append(kind)
            owns.append(1 if fid == str(k[0]) else 0)
        for m, k2 in enumerate(comp):
            if m == n or dup[m] == n or dup[n] == m or dup[m] == dup[n]:
                continue
            ln = np.asarray(pool[k2][1], np.float64)[:, :2]
            if (ln.max(0) < lo).any() or (ln.min(0) > hi).any():
                continue
            lines.append(ln)
            kinds.append(2)
            owns.append(0)
        off = np.r_[0, np.cumsum([len(a) for a in lines])].astype(np.int64)
        pts = np.ascontiguousarray(np.concatenate(lines) if lines else np.zeros((0, 2)), np.float64)
        sxy, sz, ends, ov, ooff = fast._ef.deck_shape(xy, z, pts, off, np.asarray(kinds, np.int64),
                                                      np.asarray(owns, np.int64), PARAMS)
        sxy = np.asarray(sxy, np.float64).reshape(-1, 2)
        ends = np.asarray(ends, np.float64).reshape(2, 8)
        ov = np.asarray(ov, np.float64).reshape(-1, 2)
        rec = {"xy": sxy, "z": np.asarray(sz, np.float64), "skew": [None, None], "ov": [None, None],
               "tan": [None, None], "bank": None}
        for e in (0, 1):
            if ends[e, 0] >= 0:
                rec["tan"][e] = int(ends[e, 0])
            if SKEW and ends[e, 3] > 0:
                rec["skew"][e] = ends[e, 4:6].copy()
            a, b = int(ooff[e]), int(ooff[e + 1])
            if b - a >= 2:
                rec["ov"][e] = ov[a:b].copy()
        rec["bank"] = deck_bank(pool[k][0], sxy, [lines[int(ends[e, 7])] if ends[e, 0] >= 0 else None for e in (0, 1)])
        out[k] = rec
    return out


BANK_CONTEXT_M = 150.0      # m of the road going on from each end of a deck its curve is read over (grading's stencil,
                            # runoff and smoothing reach)


def deck_bank(spec, xy, goes_on):
    """(k,) the cross slope (m per m toward the left: grading.bank's) of a simple deck's shaped line xy (k, 2) of a
    banked road (grading.BANK_DESIGN), read as grading reads its approaches when they bank on over the deck
    (plan_roads bank_through): grading.bank over the line joined with the roads going on from its ends (goes_on: per
    end a line (j, 2) with an end at the deck's, or None; BANK_CONTEXT_M of each) at grading.PROFILE_STEP, not faded at
    the deck's ends. None: level."""

    import grading
    if not grading.BANK_ON or getattr(spec, "cls", None) not in grading.BANK_DESIGN or len(xy) < 2:
        return None
    xy = np.asarray(xy, np.float64)[:, :2]
    parts = []
    for e, ln in ((0, goes_on[0]), (1, goes_on[1])):
        if ln is None or len(ln) < 2:
            parts.append(np.zeros((0, 2)))
            continue
        ln = np.asarray(ln, np.float64)[:, :2]
        p = xy[0] if e == 0 else xy[-1]
        if float(dnp.hypot(*(ln[-1] - p))) < float(dnp.hypot(*(ln[0] - p))):
            ln = ln[::-1]                       # (from the deck's end outward)
        a = np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(ln, axis=0).T))]
        keep = a <= BANK_CONTEXT_M
        ln = ln[keep][1:]
        parts.append(ln[::-1] if e == 0 else ln)
    joined = np.concatenate([parts[0], xy, parts[1]])
    s_j = np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(joined, axis=0).T))]
    if s_j[-1] <= 1.0:
        return None
    n = max(2, int(math.ceil(s_j[-1] / grading.PROFILE_STEP)) + 1)
    t = np.linspace(0.0, s_j[-1], n)
    rs = np.c_[dnp.interp(t, s_j, joined[:, 0]), dnp.interp(t, s_j, joined[:, 1])]
    b = grading.bank(rs, spec, (False, False), (1, 1))
    if b is None:
        return None
    a0 = float(s_j[len(parts[0])])
    s_d = a0 + np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(xy, axis=0).T))]
    out = dnp.interp(s_d, t, np.asarray(b, np.float64))
    return out if np.any(out != 0.0) else None


def mapped(rec, mp):
    """A shape mapped into another frame (roads._Mapper mp: its pts on (k, 3)): its line's and overlap lines' points
    (its heights as they are: the part's own are mapped with the part), the skew directions through the deck's ends;
    its bank per point as it is."""
    if rec is None:
        return None
    xy = np.asarray(rec["xy"], np.float64)
    out = {"xy": mp.pts(np.c_[xy, np.zeros(len(xy))])[:, :2].copy(), "z": rec["z"], "tan": list(rec["tan"]),
           "skew": [None, None], "ov": [None, None], "bank": rec.get("bank")}
    for e in (0, 1):
        p = xy[0] if e == 0 else xy[-1]
        d = rec["skew"][e]
        if d is not None:
            q = mp.pts(np.array([[p[0], p[1], 0.0], [p[0] + 10.0 * d[0], p[1] + 10.0 * d[1], 0.0]]))
            v = q[1, :2] - q[0, :2]
            out["skew"][e] = v / max(float(dnp.hypot(v[0], v[1])), 1e-12)
        o = rec["ov"][e]
        if o is not None:
            out["ov"][e] = mp.pts(np.c_[np.asarray(o, np.float64), np.zeros(len(o))])[:, :2].copy()
    return out
