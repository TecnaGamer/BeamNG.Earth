"""Road revamp v2, "road shape first" (.claude/work/road-revamp-v2-road-shape-first-2026-11-19.md): the glue of the
Rust kernels in native/earthfast/src/roadshape. Switch BNE_ROADSHAPE (off / decks; later sheet, profile, full).

Phase P1b (earthfast 498, BNE_ROADSHAPE=decks; the user's go 2026-11-19 on banked decks and buried deck ends):
curved, skewed decks with buried ends for the SIMPLE structures. In a bridge chain's published solve
(roads.BridgeProfiles._chain_solve, shape_chain) the chain's decks are grouped (structures.groups, S1's edge table);
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


def shape_chain(comp, pool, zs, ground):
    """{part key: shape} of the simple decks of a chain's solve: comp its parts' keys (its order), pool {key: (spec,
    xy, lon, level, ...)}, zs their heights (comp's order), ground the chain's ground {(fid, "g" | "r"): [lines]}. A
    shape: {"xy" (k, 2), "z" (k,), "skew": [unit end line (2,) or None] x 2, "ov": [overlap line (j, 2) or None] x 2,
    "tan": [the approach's kind or None] x 2}. Pure given its inputs (sorted keys, one kernel call per deck)."""
    import fast
    import structures
    fast.require(VERSION, "deck_shape")
    if not comp:
        return {}
    members = []
    for k in comp:                              # (height-free, as grading's Grader._deck_end_simple_at: every deck at 0)
        v = pool[k]
        xy = np.asarray(v[1], np.float64)[:, :2]
        members.append(structures._member(k, v[0], xy, np.zeros(len(xy)), structures.level_of(v), None, False))
    got = structures.groups(members)
    simple = simple_members(members, got)
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
