"""Mapped traffic calming in the road's graded profile (BNE_CALMING=on, drivefeat.py): OpenStreetMap's
traffic_calming nodes (the drive shards, drivetags.py) as real bumps in the ground the car drives on. Each mapped bump,
hump, table, cushion or dip becomes a profile along its road (earthfast drivefeat.rs calming_apply: half-cosine ramps,
a flat top), added to the road's plan after every grading pass that moves heights (grading._make_cell_roads, after the
hairpins), so the physics block, the road meshes and decals take the same shape. The plan is densified round each
(every STEP_M) so the 1 m lattice samples the shape and not just its plan vertices 4 m apart.

Dimensions (height m, ramp m, flat top m), from the design guidance where it is common:
  hump     UK DfT LTN 1/07 round-top road hump 3.7 m long, 75 mm; US ITE speed hump 12 ft, 3-3.5 in: sinusoidal,
           ramp 1.85, no top, 0.075 m
  table    UK flat-top hump (ramps 1:15-1:25, top >= 2.5 m), US speed table 22 ft (6 ft ramps, 10 ft top, 3-3.5 in):
           ramp 2.0 (1:25), top 3.0, 0.08 m
  cushion  UK speed cushion 1.6-1.9 m wide, 3 m long, 75 mm, the wide wheels straddle it: a lower, uniform stand-in
           across the road (the ground is one height across a road): ramp 1.0, top 1.0, 0.04 m
  bump     a speed bump (car parks, private roads; 50-100 mm over 0.3-0.9 m in reality): ramp 1.0, top 0.4, 0.07 m:
           longer than the real thing, so the 1 m lattice samples it whatever its place and a car at the service roads'
           15 mph keeps its wheels on the road (tools/drivefeat_ride.py's quarter car: least tyre load 0.26 of static;
           the real 0.9 m one lifts the wheel off at 15 mph). Its top widened to MIN_BASE where that is longer.
Drivability (tools/drivefeat_ride.py, the IRI golden car): hump, table and cushion keep the wheel on the road at 30 mph
(least tyre load 0.29 / 0.22 / 0.10 of static), the hump at 40 mph too (0.09); the Dutch / Belgian 0.107 m table at
20 mph (0.57).
  dip      a drainage dip: -0.06 m, ramp 2.0
  mini_bumps 0.03 m bump; yes (unspecified): a hump
The Netherlands and Belgium build taller (CROW plateaus and drempels ~0.10-0.12 m): hump / table x 4/3.
Two features reaching one spot (one hump mapped twice) take the higher, never the sum (drivefeat.rs).
A mapped height (height=*, "0.1", "10 cm", "4 in") is taken within 0.02-0.15 m. chicane, choker, island,
rumble_strip, painted_table: no height here (their kerbs / paint: roads' build-outs and markings).

Off: nothing reads the shards and every plan is as before.
"""
import logging
import math
import re

import numpy as np

import drivefeat
import drivetags

log = logging.getLogger("earthd.calming")

NATIVE = 612
STEP_M = 0.25               # m between the stations added over a feature
REACH_M = 1.5               # m: a plan takes a feature whose foot on its line lies this close (the node is on its way)
# (height, ramp, top)
KINDS = {
    "hump": (0.075, 1.85, 0.0),
    "yes": (0.075, 1.85, 0.0),
    "table": (0.08, 2.0, 3.0),
    "cushion": (0.04, 1.0, 1.0),
    "bump": (0.07, 1.0, 0.4),
    "mini_bumps": (0.03, 1.0, 0.4),
    "dip": (-0.06, 2.0, 0.0),
}
# a ford (BNE_UNPAVED: ford=yes on a road's node or way): the road dips FORD_DIP_M over FORD_RAMP_M ramps either side of
# a FORD_TOP_M flat bottom, and water stands FORD_WATER_M over the bottom (fords.rivers: the sea message's River
# pieces, drivefeat.ford_rivers). A ford's own way: its flat bottom its length less the ramps, within FORD_TOP_MAX_M.
# The bottom is level (the lowest planned height over it less the dip) where the road rises at most drivefeat.rs
# LEVEL_MAX_RISE_M over it, so the water fills it evenly; on a steeper road the dip follows the slope.
FORD_DIP_M, FORD_RAMP_M, FORD_TOP_M, FORD_TOP_MAX_M = 0.30, 4.0, 4.0, 30.0
FORD_WATER_M = 0.22
FORDS = ("yes", "boat")
TALL = {"NL": 4.0 / 3.0, "BE": 4.0 / 3.0}
TALL_KINDS = ("hump", "table", "yes")     # (a bump stays: taller, it would lift a wheel at the 15 mph it is for)
H_MIN, H_MAX = 0.02, 0.15


def _height_tag(v):
    """A mapped height (m) of a height=* value ("0.1", "10 cm", "4 in", "100 mm"), None when it is none of those."""
    if not v:
        return None
    m = re.match(r"^\s*([0-9]+(?:[.,][0-9]+)?)\s*(m|cm|mm|in|\"|ft|')?\s*$", str(v))
    if not m:
        return None
    x = float(m.group(1).replace(",", "."))
    unit = m.group(2) or "m"
    x *= {"m": 1.0, "cm": 0.01, "mm": 0.001, "in": 0.0254, '"': 0.0254, "ft": 0.3048, "'": 0.3048}[unit]
    return min(H_MAX, max(H_MIN, x)) if x > 0 else None


def kind_of(tags):
    """The profile kind of a traffic_calming node's tags (KINDS' key), None for one without a height (chicane,
    choker, island, rumble_strip, painted_table, ...): the first of a ;-list that has one."""
    for v in str(tags.get("traffic_calming") or "").lower().split(";"):
        v = v.strip()
        if v in KINDS:
            return v
    return None


def profile_of(tags, country, min_base):
    """(height, ramp, top) of a node, None for none: KINDS', the country's factor, a mapped height, and a bump's top
    widened so its whole length is at least min_base."""
    k = kind_of(tags)
    if k is None:
        return None
    h, ramp, top = KINDS[k]
    if k in TALL_KINDS:
        h *= TALL.get((country or "").upper(), 1.0)
    got = _height_tag(tags.get("height"))
    if got is not None and h > 0:
        h = got
    if k in ("bump", "mini_bumps"):
        top = max(top, min_base - 2.0 * ramp)
    return (float(h), float(ramp), float(top))


def features(md, frame, plans, min_base):
    """(m, 6) feature rows [x, y, height, ramp, top, level] in the frame for the calming nodes round the plans, and their
    node ids (m,), ascending by id; None when the shards there could not be had (a fault: drivetags)."""
    t = drivetags.of(md)
    if t is None or not plans:
        return np.zeros((0, 6)), np.zeros(0, np.int64)
    xy = np.concatenate([np.asarray(p.xy, np.float64)[:, :2] for p in plans])
    pad = 20.0
    x0, y0 = xy.min(axis=0) - pad
    x1, y1 = xy.max(axis=0) + pad
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x1, x0, x1]), np.array([y0, y0, y1, y1]), 0.0)
    nodes = t.nodes(float(np.min(la)), float(np.min(lo)), float(np.max(la)), float(np.max(lo)))
    if nodes is None:
        return None
    import countrybounds
    cb = countrybounds.bundled()
    rows, ids = [], []
    for nid, nlo, nla, tags in nodes:
        if drivefeat.UNPAVED and str(tags.get("ford", "")).lower() in FORDS:
            prof = (-FORD_DIP_M, FORD_RAMP_M, FORD_TOP_M, 1.0)       # (level: drivefeat.rs)
        elif drivefeat.CALMING and tags.get("traffic_calming"):
            prof = profile_of(tags, cb.at(nla, nlo), min_base)
        else:
            continue
        if prof is None:
            continue
        e, n, _ = frame.geodetic_to_enu(np.array([nla]), np.array([nlo]), 0.0)
        rows.append([float(np.asarray(e)[0]), float(np.asarray(n)[0]), *prof, *(() if len(prof) > 3 else (0.0,))])
        ids.append(nid)
    return np.array(rows, np.float64).reshape(-1, 6), np.array(ids, np.int64)


def apply_plan(p, feats):
    """Plan p with the features (features()' rows) added in place (its vertices, heights, bank, widen and cut rows);
    the number of features it took."""
    import fast
    fast.require(NATIVE, "calming_apply")
    if not len(feats):
        return 0
    xy = np.ascontiguousarray(np.asarray(p.xy, np.float64)[:, :2])
    z = np.ascontiguousarray(np.asarray(p.z, np.float64))
    bank = np.ascontiguousarray(np.asarray(p.bank, np.float64)) if p.bank is not None else np.zeros(0)
    widen = np.ascontiguousarray(np.asarray(p.widen, np.float64)) if p.widen is not None else np.zeros(0)
    oxy, oz, ob, ow, segs, taken = fast._ef.calming_apply(xy, z, bank, widen, np.ascontiguousarray(feats),
                                                          REACH_M, STEP_M)
    if not len(taken):
        return 0
    p.xy, p.z = oxy, oz
    if p.bank is not None:
        p.bank = ob
    if p.widen is not None:
        p.widen = ow
    if p.cut is not None:
        p.cut = np.asarray(p.cut)[segs]
    return int(len(taken))


def ford_ways(frame, roads):
    """(m, 5) feature rows of the roads Features' ford ways (attrs osm_ford, drivetags.join): one dip at each ground
    part's middle, its flat bottom the part's length less the ramps (FORD_TOP_MAX_M at most)."""
    import dnp
    import roads as roadsmod
    out = []
    col = roads.attrs.get("osm_ford")
    if col is None:
        return np.zeros((0, 6))
    for i in np.flatnonzero(np.array([str(v).lower() in FORDS if v is not None else False for v in col], bool)):
        for part in roadsmod.ground_parts(roads, int(i)):
            if len(part) < 2:
                continue
            e, n, _ = frame.geodetic_to_enu(part[:, 1], part[:, 0], 0.0)
            xy = np.c_[np.asarray(e), np.asarray(n)]
            s_ = np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(xy, axis=0).T))]
            mid = np.array([dnp.interp(s_[-1] / 2, s_, xy[:, 0]), dnp.interp(s_[-1] / 2, s_, xy[:, 1])])
            top = min(FORD_TOP_MAX_M, max(FORD_TOP_M, float(s_[-1]) - 2 * FORD_RAMP_M))
            out.append([float(mid[0]), float(mid[1]), -FORD_DIP_M, FORD_RAMP_M, top, 1.0])
    return np.array(out, np.float64).reshape(-1, 6)


def apply(md, frame, plans, min_base, roads=None):
    """The calming features (and with BNE_UNPAVED the fords' dips; roads: the cell's road Features, their ford ways)
    added to the road plans [(fid, Plan)] (in place); the features taken. A shard that cannot be had: nothing added
    (and drivetags' fault: the cell is planned again later)."""
    if not (drivefeat.CALMING or drivefeat.UNPAVED) or not plans:
        return 0
    got = features(md, frame, [p for _f, p in plans], min_base)
    if got is None:
        return 0
    feats, _ids = got
    if drivefeat.UNPAVED and roads is not None:
        feats = np.vstack([feats, ford_ways(frame, roads)])
    if not len(feats):
        return 0
    n = 0
    for _fid, p in plans:
        n += apply_plan(p, feats)
    return n


def lattice_min_base():
    """Two of the physics block's lattice squares (heightmap.SQUARE): a bump at least this long has a lattice sample
    on its top wherever it falls."""
    import heightmap
    return 2.0 * float(heightmap.SQUARE)

