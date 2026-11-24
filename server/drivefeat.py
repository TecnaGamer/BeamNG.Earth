"""Mapped driving features (the user, 2026-11-22: "feel free to use as many [OSM/Overture data points] as possible if
they would improve the generator"): the switches and the shared rules of the mapped traffic calming, unpaved road
grades and fords, road barriers and gates, road ends (turning circles, mini roundabouts, passing places), mapped stop
/ give-way / zebra markings, railway signals / points / buffer stops and the street lamps' mapped colour, count and
mount. Each one off by default; on, it is a BNE_* build switch (revisit.keyed_env: the plans', facts' and tiles' keys),
and the ones that read the "drive" shards (drivetags.py) put that build in MapData.versions().

Sources: Overture's infrastructure layer (barriers, gates, bollards, cattle grids, height restrictors, highway=stop /
give_way / crossing with crossing:markings, street lamps with their OSM tags) and the weekly drive shards (traffic
calming, turning circles, mini roundabouts, passing places, fords, railway signals / points / buffer stops; the
tracktype / ford / traffic_calming of highway ways).

Switches (environment, read at import):
  BNE_CALMING=on         speed bumps / humps / tables / cushions in the road's graded profile (the physics ground and
                         everything drawn on it), their markings; chicanes / chokers / islands as kerbed build-outs
  BNE_UNPAVED=on         tracktype / smoothness choose an unpaved road's look and roughness; fords dip to water
  BNE_ROAD_BARRIERS=on   gates, lift gates, bollards, cattle grids, height restrictors where mapped on roads
  BNE_ROAD_ENDS=on       turning circles / loops as paved bulbs, mini roundabouts painted, passing places widened
  BNE_NODE_MARKINGS=on   stop lines, give-way lines and zebras at the mapped nodes, every country
  BNE_RAIL_PROPS=on      railway signals, points and buffer stops where mapped
  BNE_LAMP_TAGS=on       the mapped street lamps' light:colour / light:count / lamp_mount
"""
import os


_ON = ("on", "1", "true", "yes")
CALMING = os.environ.get("BNE_CALMING", "off").strip().lower() in _ON
UNPAVED = os.environ.get("BNE_UNPAVED", "off").strip().lower() in _ON
BARRIERS = os.environ.get("BNE_ROAD_BARRIERS", "off").strip().lower() in _ON
ENDS = os.environ.get("BNE_ROAD_ENDS", "off").strip().lower() in _ON
MARKINGS = os.environ.get("BNE_NODE_MARKINGS", "off").strip().lower() in _ON
RAIL_PROPS = os.environ.get("BNE_RAIL_PROPS", "off").strip().lower() in _ON
LAMP_TAGS = os.environ.get("BNE_LAMP_TAGS", "off").strip().lower() in _ON
# the switches that read the drive shards (their build in MapData.versions() only then)
SHARDS = CALMING or UNPAVED or ENDS or RAIL_PROPS
VERSION = 1                 # the rules' version (MapData.versions()' "drive" entry): bumped when one changes output

SWITCHES = ("BNE_CALMING", "BNE_UNPAVED", "BNE_ROAD_BARRIERS", "BNE_ROAD_ENDS", "BNE_NODE_MARKINGS", "BNE_RAIL_PROPS",
            "BNE_LAMP_TAGS")


# the switches whose output depends on the border data (countrybounds: the calming heights' country factors, a turning
# circle's radius and a passing place's side, the paint's country rules): its signature keyed only while one is on
COUNTRY = CALMING or ENDS or MARKINGS


def identity(md):
    """MapData.versions()' "drive" entry (the plans', cell facts' and tiles' keys) while a mapped-feature switch that
    reads data is on: (VERSION, the drive shards' build in use or None, the border data's signature
    (countrybounds.signature) or None); None with them off (nothing keyed: every switch off keeps trunk's keys)."""
    if not (SHARDS or COUNTRY):
        return None
    t = getattr(md, "drive_tags", None) if SHARDS else None
    border = None
    if COUNTRY:
        import countrybounds
        border = countrybounds.signature()
    return (VERSION, t.known() if t is not None else None, border)


# --- BNE_NODE_MARKINGS: the mapped stop / give-way / zebra nodes as paint (roads.build_tile, every country but Sweden
# and Germany, which read the same nodes their own way) ----------------------------------------------------------------
ARM_NEAR_M = 30.0           # m along a junction arm a mapped stop / give-way node is that arm's
ARM_OFF_M = 3.0             # m off the arm's half width it may stand (mapped on the way, or on the kerb by the sign)
SAME_CROSSWALK_M = 6.0      # m between a mapped zebra and a drawn crosswalk: the same crossing
DOUBLE_DASH = ("GB", "IE", "IM", "JE", "GG")    # TSRGD diagram 1003: the give-way line is a double broken line
D1003_W, D1003_DASH, D1003_GAP, D1003_SEP = 0.20, 0.60, 0.30, 0.30


def _arms_near(arms, pts, spec_of=lambda a: a[1]):
    """The arms (furniture.give_way_arms') with a point of pts (n, 2) within ARM_NEAR_M along their first stretch and
    the arm's half width + ARM_OFF_M off it."""
    import dnp
    import numpy as np
    if pts is None or not len(pts):
        return []
    out = []
    for a in arms:
        line = np.asarray(a[2], np.float64)
        s = np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(line[:, :2], axis=0).T))]
        st = np.arange(0.0, min(float(s[-1]), ARM_NEAR_M) + 1e-9, 1.0)
        if not len(st):
            continue
        along = np.stack([dnp.interp(st, s, line[:, k]) for k in range(2)], 1)
        d = dnp.hypot(along[:, None, 0] - pts[None, :, 0], along[:, None, 1] - pts[None, :, 1])
        if float(d.min()) <= spec_of(a).width / 2 + ARM_OFF_M:
            out.append(a)
    return out


def _double_dash(lines):
    """TSRGD 1003's two broken lines (quads (4, 3)) in place of each stop-line row {"a", "b"} (across the lanes coming
    in): 0.6 m marks, 0.3 m gaps, 0.2 m wide, 0.3 m apart."""
    import dnp
    import numpy as np
    import markings
    out = []
    for ln in lines:
        a, b = np.asarray(ln["a"], np.float64), np.asarray(ln["b"], np.float64)
        d = b - a
        span = float(dnp.hypot(*d[:2]))
        if span < D1003_DASH:
            continue
        n = d / span
        t = np.array([-n[1], n[0], 0.0])
        for off in (-(D1003_SEP + D1003_W) / 2, (D1003_SEP + D1003_W) / 2):
            for p in markings.dashes(np.stack([a + t * off, b + t * off]), (D1003_DASH, D1003_GAP)):
                h = t * D1003_W / 2
                out.append(np.stack([p[0] + h, p[-1] + h, p[-1] - h, p[0] - h]))
    return out


def mapped_marks(roads, found, crosswalks, jlines, signals, country, left_hand):
    """(crosswalks, teeth, jlines, the zebras added) with the mapped nodes' paint added (found: secrossings.nodes of the tile's cells):
    a zebra at each mapped striped crossing (secrossings.marked) not already drawn (SAME_CROSSWALK_M), a stop line on
    each junction arm with a mapped stop node that has none, give-way paint on each arm with a mapped give-way node
    (shark's teeth, markings.give_way_lines; TSRGD 1003's double broken line in DOUBLE_DASH countries) in place of a
    drawn stop line there. The other arms and crosswalks as before."""
    import dnp
    import numpy as np
    import furniture
    import markings
    import secrossings
    if found is None:
        return crosswalks, [], jlines, []
    crosswalks = list(crosswalks)
    added = []
    centres = np.array([0.5 * (np.asarray(c["a"])[:2] + np.asarray(c["b"])[:2]) for c in crosswalks
                        if not c.get("stop")]).reshape(-1, 2)
    new, _sq, _ramps = secrossings.place(roads, {"crossings": [(x, y, secrossings.M15) for x, y in found["striped"]]})
    for cw in new:
        c = 0.5 * (np.asarray(cw["a"])[:2] + np.asarray(cw["b"])[:2])
        if len(centres) and float(dnp.hypot(*(centres - c).T).min()) < SAME_CROSSWALK_M:
            continue
        crosswalks.append(cw)
        added.append(cw)
    pr = found["priority"]
    stops_at = pr[pr[:, 2] == 1.0][:, :2] if len(pr) else np.zeros((0, 2))
    yields_at = pr[pr[:, 2] == 0.0][:, :2] if len(pr) else np.zeros((0, 2))
    teeth = []
    jlines = {k: list(v) for k, v in jlines.items()}
    # the junction arms with a mapped sign (furniture._mapped_priority: the map's give-way / stop node on the arm), each
    # from the junction out (give_way_arms' form), whatever the country's guessed controls say
    lines = {fid: pts for fid, _, pts, _ in roads}
    garms, sarms_m = [], []
    for k, arms in furniture._arms(roads).items():
        if len(arms) < 3:
            continue
        wide = max(a[1].width for a in arms) / 2
        for a in arms:
            if a[1].kind != "paved" or furniture._outbound(a):
                continue
            what = furniture._mapped_priority(a, pr)
            if what is None:
                continue
            arm = (a[0], a[1], np.asarray(lines[a[0]] if a[4] else lines[a[0]][::-1], np.float64), wide)
            (garms if what == furniture.G_YIELD else sarms_m).append(arm)
    # give-way arms: the paint replaces the drawn stop line on that arm (give_way_lines' "gone")
    if garms:
        if str(country or "").upper() in DOUBLE_DASH:
            lines, cuts = markings.stop_lines([(f, sp, ln, w, furniture.G_STOP) for f, sp, ln, w in garms], crosswalks,
                                              None, left_hand)
            teeth.extend(_double_dash(lines))
            gone = {j for j, cw in enumerate(crosswalks) if cw.get("stop") and any(
                float(dnp.hypot(*(0.5 * (np.asarray(cw["a"])[:2] + np.asarray(cw["b"])[:2])
                                  - 0.5 * (np.asarray(ln["a"])[:2] + np.asarray(ln["b"])[:2])))) < 6.0
                for ln in lines)}
        else:
            tri, gone = markings.give_way_lines(garms, crosswalks, left_hand)
            teeth.extend(tri)
            cuts = {}
        crosswalks = [cw for j, cw in enumerate(crosswalks) if j not in gone]
        for k, v in cuts.items():
            jlines.setdefault(k, []).extend(v)
    # stop arms without a drawn stop line
    sarms = [arm + (furniture.G_STOP,) for arm in sarms_m]
    if sarms:
        have = np.array([0.5 * (np.asarray(c["a"])[:2] + np.asarray(c["b"])[:2]) for c in crosswalks
                         if c.get("stop")]).reshape(-1, 2)
        todo = [a for a in sarms if not _arms_near([a], have)]     # (an arm with a stop line drawn keeps it)
        lines, cuts = markings.stop_lines(todo, crosswalks, found.get("stop_lines"), left_hand)
        crosswalks.extend(lines)
        for k, v in cuts.items():
            jlines.setdefault(k, []).extend(v)
    return crosswalks, teeth, jlines, added


def tile_inputs(md, frame, keys, nodes=None):
    """build_tile's drive= for the road tile of map cells keys: {"marks": secrossings.nodes of them (BNE_NODE_MARKINGS;
    the tile's own nodes when it read them), "nodes": [(x, y, tags, id)] of the drive shards' nodes over the cells (a
    switch that reads them), ascending by id, "infra": infra_points of BARRIER_CLASSES (BNE_ROAD_BARRIERS)}; None with
    every switch off."""
    if not (MARKINGS or SHARDS or BARRIERS):
        return None
    import numpy as np
    out = {"marks": None, "nodes": [], "infra": []}
    if BARRIERS:
        out["infra"] = infra_points(md, keys, frame, BARRIER_CLASSES)
    if MARKINGS:
        import secrossings
        out["marks"] = nodes if nodes is not None else secrossings.read(md, frame, keys)
    if SHARDS:
        import drivetags
        t = drivetags.of(md)
        if t is not None and keys:
            b = np.array([k.bounds() for k in keys], np.float64)
            got = t.nodes(float(b[:, 1].min()), float(b[:, 0].min()), float(b[:, 3].max()), float(b[:, 2].max()))
            if got:
                ll = np.array([[g[1], g[2]] for g in got], np.float64)
                e, n, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.0)
                out["nodes"] = [(float(x), float(y), g[3], g[0]) for x, y, g in zip(np.asarray(e), np.asarray(n), got)]
    return out


# --- the drive nodes' paint in the road tiles (roads.build_tile's teeth): the humps' triangles, mini roundabouts ------
HUMP_MARK_COUNTRIES = ("GB", "IE", "IM", "JE", "GG", "US", "CA", "AU", "NZ")   # TSRGD 1062, MUTCD 3B-29 (an option)
MINI_R = (1.0, 2.0)         # m: a mini roundabout's disc radius, a quarter of its narrowest road's width within this
MARK_REACH_M = 1.5


def tile_teeth(roads, nodes, country, left_hand):
    """[(k, 3) polygons] of white paint for the tile's drive nodes [(x, y, tags, id)] (tile_inputs): with BNE_CALMING
    the triangles on each mapped hump / table / cushion's up-ramps in HUMP_MARK_COUNTRIES (calming.profile_of: its
    ramp and top), with BNE_ROAD_ENDS a mini roundabout's disc (TSRGD 1003.4: a white disc at its middle). The roads
    [(fid, spec, pts (k, 3), lon)] the tile's; paved ones only. [] none."""
    import numpy as np
    import dnp
    if not nodes or not roads:
        return []
    paved = [r for r in roads if r[1].kind in ("paved", "cobble") and len(r[2]) >= 2]
    if not paved:
        return []
    rows = []
    cc = str(country or "").upper()
    if CALMING and cc in HUMP_MARK_COUNTRIES:
        import calming
        for x, y, tags, _i in nodes:
            if tags.get("traffic_calming") and calming.kind_of(tags) in ("hump", "table", "cushion", "yes"):
                prof = calming.profile_of(tags, cc, 0.0)
                rows.append([x, y, 1.0, prof[1], prof[2], 0.0])
    if ENDS:
        for x, y, tags, _i in nodes:
            if tags.get("highway") == "mini_roundabout":
                rows.append([x, y, 2.0, 0.0, 0.0, 0.0])
    if not rows:
        return []
    import fast
    fast.require(612, "drive_marks")
    lines = [np.ascontiguousarray(np.asarray(r[2], np.float64)[:, :2]) for r in paved]
    half = [float(r[1].width) / 2 for r in paved]
    rows = np.array(rows, np.float64)
    for k in np.flatnonzero(rows[:, 2] == 2.0):     # (the disc: a quarter of the narrowest road near it)
        d = [float(np.min(dnp.hypot(*(ln - rows[k, :2]).T))) for ln in lines]
        near = [h for h, dd in zip(half, d) if dd < 25.0]
        rows[k, 3] = min(MINI_R[1], max(MINI_R[0], 0.5 * min(near))) if near else MINI_R[0]
    dirs = [int(getattr(r[1], "dir", 0) or 0) if r[1].oneway else 0 for r in paved]
    lanes = [max(1, int((r[1].lanes or 2) // (1 if r[1].oneway else 2))) for r in paved]
    out = []
    for poly in fast._ef.drive_marks(lines, half, dirs, lanes, np.ascontiguousarray(rows), MARK_REACH_M,
                                     bool(left_hand), 20):
        p = np.asarray(poly, np.float64)
        out.append(np.c_[p, np.zeros(len(p))])
    return out


# --- BNE_UNPAVED: an unpaved road's mapped grade as its roughness (grading.dirt_bumps' gain) --------------------------
# OpenStreetMap's tracktype (grade1 solid .. grade5 soft) and smoothness (excellent .. impassable): how rough the
# surface damage of grading.dirt_bumps is, 1 = its calibrated minor dirt road. smoothness, where mapped, decides (it is
# the mapper's word on the ride); else tracktype.
TRACK_GAIN = {"grade1": 0.35, "grade2": 0.6, "grade3": 0.9, "grade4": 1.2, "grade5": 1.5}
SMOOTH_GAIN = {"excellent": 0.1, "good": 0.2, "intermediate": 0.45, "bad": 0.8, "very_bad": 1.15, "horrible": 1.5,
               "very_horrible": 1.8, "impassable": 2.0}


# ... and where neither is mapped (sparse data: most of the world's tracks), a plausible default from what is known: the
# surface value (OSM's, else Overture's rule: roads._map_surface_values), else the class
SURFACE_GAIN = {"compacted": 0.4, "fine_gravel": 0.5, "pebblestone": 0.7, "gravel": 0.7, "wbm": 0.6,
                "laterite": 0.8, "unpaved": 0.9, "ground": 1.0, "dirt": 1.0, "earth": 1.0, "soil": 1.0,
                "clay": 1.1, "sand": 0.8, "grass": 1.2, "grass_paver": 0.6, "mud": 1.4, "rock": 1.3, "rocky": 1.3}
# (an unclassified gravel road is often a county road driven at 45-55 mph: a gentle prior; a track the full one)
CLASS_GAIN = {"track": 1.0, "path": 1.0, "unclassified": 0.45, "service": 0.6, "residential": 0.45}
DEFAULT_GAIN = 0.5


def rough_resolve(feats, i):
    """(gain, source rank, confidence) of an unpaved road feature i's roughness, the evidence model's ladder
    (~/bnge-tests/adaptiveplan/PLAN.md 2.1; mapped and inferred kept apart): "S1:smoothness" / "S1:tracktype" /
    "S1:surface" (mapped, confidence 3), "S4:class" (a prior by class, 1), "S5:default" (0). The surface's own source
    (OSM mapped, or filled from the tracktype: drivetags.join's surface_src "S3:tracktype") counts as it is."""
    for col, table, rank in (("osm_smoothness", SMOOTH_GAIN, "S1:smoothness"),
                             ("osm_tracktype", TRACK_GAIN, "S1:tracktype")):
        v = feats.attrs.get(col)
        if v is None:
            continue
        g = table.get(str(v[i]).strip().lower()) if v[i] is not None else None
        if g is not None:
            return g, rank, 3
    try:
        import roads as roadsmod
        sv = roadsmod._map_surface_values(feats)[i]
        g = SURFACE_GAIN.get(roadsmod.surface_token(sv)) if sv is not None else None
    except Exception:
        g = None
    if g is not None:
        src = feats.attrs.get("surface_src")
        rank = str(src[i]) if src is not None and src[i] else "S1:surface"
        return g, rank, 2 if rank.startswith("S3") else 3
    cls = feats.attrs.get("class")
    c = str(cls[i]) if cls is not None else ""
    if c in CLASS_GAIN:
        return CLASS_GAIN[c], "S4:class", 1
    return DEFAULT_GAIN, "S5:default", 0


def rough_gain(feats, i):
    """grading.dirt_bumps' gain of road feature i (an unpaved road's: dirt_bumps leaves built roads alone):
    rough_resolve's; None with BNE_UNPAVED off (the road as BNE_DIRT_BUMP has it)."""
    if not UNPAVED:
        return None
    return rough_resolve(feats, i)[0]


# --- BNE_UNPAVED: the fords' water (the sea message's River pieces: earthd's river plan) ------------------------------
FORD_REACH_M = 400.0        # m round the player the fords' water is sent (riverlines.RADIUS_M's job for the rivers)
FORD_SIDE_M = 5.0           # m the water reaches past the carriageway's edges either side
FORD_RIVER_DEPTH_M = 0.5    # the River object's own depth below its surface (its shape; the ground decides the rest)


def ford_rivers(md, frame, x, y, ground, raw):
    """[riverlines.River] of the mapped fords within FORD_REACH_M of (x, y): one short piece across the road at each
    (a ford node, or a ford way's middle: calming.ford_ways), its surface calming.FORD_WATER_M over the dipped road's
    graded height there (ground(lat, lon), graded where planned; raw(lat, lon): the DEM, to tell a cell not planned yet:
    no water there until it is), as wide along the road as the dip's water reaches. [] when off or none."""
    if not UNPAVED or md is None:
        return []
    import numpy as np
    import calming
    import dnp
    import drivetags
    import mapcells
    import mapdata as mapdatamod
    import riverlines
    import roads as roadsmod
    t = drivetags.of(md)
    r = FORD_REACH_M
    la0, lo0, _ = frame.enu_to_geodetic(x - r, y - r, 0.0)
    la1, lo1, _ = frame.enu_to_geodetic(x + r, y + r, 0.0)
    la0, la1, lo0, lo1 = float(la0), float(la1), float(lo0), float(lo1)
    pts = []                                        # (x, y, flat bottom length)
    nodes = t.nodes(la0, lo0, la1, lo1) if t is not None else None
    for _id, lo, la, tags in nodes or ():
        if str(tags.get("ford", "")).lower() in calming.FORDS:
            e, n, _ = frame.geodetic_to_enu(np.array([la]), np.array([lo]), 0.0)
            pts.append((float(np.asarray(e)[0]), float(np.asarray(n)[0]), calming.FORD_TOP_M))
    lines = []
    try:
        keys = mapcells.cells_for_bbox(min(la0, la1), min(lo0, lo1), max(la0, la1), max(lo0, lo1), max_cells=256)
    except ValueError:
        keys = []
    for k in keys:
        if not md.has(mapdatamod._key(k), "roads"):
            continue
        f = md.cell(k, layers=("roads",)).layers.get("roads")
        if f is None or not len(f):
            continue
        for row in calming.ford_ways(frame, f).tolist():
            pts.append((row[0], row[1], row[4]))
        sub = f.attrs.get("subtype")
        specs = roadsmod.road_specs(f)
        for i in range(len(f)):
            if specs[i] is None or (sub is not None and str(sub[i]) != "road"):
                continue
            for part in roadsmod.ground_parts(f, i):
                e, n, _ = frame.geodetic_to_enu(part[:, 1], part[:, 0], 0.0)
                lines.append((np.c_[np.asarray(e), np.asarray(n)], specs[i].width / 2))
    out, seen = [], set()
    for px, py, top in sorted(set(pts)):
        if (round(px, 1), round(py, 1)) in seen or not lines:
            continue
        seen.add((round(px, 1), round(py, 1)))
        best = None
        for xy, half in lines:
            a, b = xy[:-1], xy[1:]
            d = b - a
            l2 = np.maximum((d * d).sum(1), 1e-9)
            tt = np.clip(((np.array([px, py]) - a) * d).sum(1) / l2, 0.0, 1.0)
            q = a + d * tt[:, None]
            off = dnp.hypot(q[:, 0] - px, q[:, 1] - py)
            j = int(np.argmin(off))
            if best is None or off[j] < best[0]:
                best = (float(off[j]), d[j] / max(float(np.sqrt(l2[j])), 1e-9), half, q[j])
        if best is None or best[0] > 3.0:
            continue
        _, u, half, c = best
        nrm = np.array([-u[1], u[0]])
        la, lo, _ = frame.enu_to_geodetic(np.array([c[0]]), np.array([c[1]]), 0.0)
        zg = float(np.asarray(ground(la, lo), np.float64).reshape(-1)[0])
        zr = float(np.asarray(raw(la, lo), np.float64).reshape(-1)[0])
        if not np.isfinite(zg) or abs(zg - zr) < 1e-6:
            continue                                # (its cell not planned yet: no dip to fill)
        level = float(np.asarray(frame.geodetic_to_enu(la, lo, np.array([zg + calming.FORD_WATER_M]))[2])[0])
        width = top + 2 * 0.6 * calming.FORD_RAMP_M
        reach = half + FORD_SIDE_M
        a, b = c - nrm * reach, c + nrm * reach
        out.append(riverlines.River(f"ford_{int(round(c[0] * 10))}_{int(round(c[1] * 10))}", "stream", 0.2,
                                    ((float(a[0]), float(a[1]), level, width, FORD_RIVER_DEPTH_M),
                                     (float(b[0]), float(b[1]), level, width, FORD_RIVER_DEPTH_M))))
    return out


# --- BNE_ROAD_BARRIERS: gates, lift gates, bollards, height restrictors and cattle grids on roads --------------------
# Overture's infrastructure points (barrier subtype; their OSM tags). A barrier is the road's where its point is within
# its carriageway's half width + BARRIER_ON_M of the road's line. Never a closed one on a road the AI drives
# (navgraph.DRIVE classes, not a driveway / parking aisle: the AI graph's own rule): there a gate stands open and the
# bollards stand at the carriageway's edges. Closed (drawn across the road, colliding) only with access=private / no
# on a road the AI graph leaves out.
BARRIER_CLASSES = ("gate", "lift_gate", "swing_gate", "bollard", "height_restrictor", "cattle_grid")
BARRIER_ON_M = 1.0
CLOSED_ACCESS = ("private", "no")
GATE_LEAF_M = 3.6           # tools/build_drive_props.py's field gate leaf
BOOM_M = 4.6                # ... and lift gate boom
HR_WIDTHS = (4, 6, 8, 10)
EDGE_PAD_M = 0.35           # m outside the carriageway's edge a post / cabinet / moved bollard stands
CATTLE_GRID_LEN_M = 2.6     # m along the road (a vehicle grid's 2.4-3 m)
CATTLE_GRID_MAT = "m_metal_grid_drain"      # (stock, driver_training: loaded by roads.lua for the storm drains)


def infra_points(md, keys, frame, classes):
    """[(x, y, class, tags, id)] of the cells' infrastructure points of classes, each once, ascending by id."""
    import json
    import numpy as np
    import mapgeom
    got = {}
    for key in keys:
        try:
            f = md.cell(key, layers=("infrastructure",), kind="demand").layers.get("infrastructure")
        except Exception:
            continue
        if f is None or not len(f):
            continue
        cls = f.attrs.get("class")
        if cls is None:
            continue
        tags = f.attrs.get("tags")
        for i in np.flatnonzero(np.isin(np.asarray(cls).astype(str), list(classes))).tolist():
            if int(f.kind[i]) != mapgeom.POINT:
                continue
            fid = str(f.ids[i])
            if fid in got:
                continue
            a = f.coord_off[f.ring_off[f.part_off[i]]]
            b = f.coord_off[f.ring_off[f.part_off[i + 1]]]
            lo, la = (float(v) for v in f.lonlat[a:b].mean(axis=0))
            try:
                t = json.loads(str(tags[i])) if tags is not None else {}
            except ValueError:
                t = {}
            got[fid] = (lo, la, str(cls[i]), t if isinstance(t, dict) else {})
    if not got:
        return []
    ids = sorted(got)
    ll = np.array([[got[i][0], got[i][1]] for i in ids], np.float64)
    e, n, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.0)
    return [(float(x), float(y), got[i][2], got[i][3], i) for x, y, i in zip(np.asarray(e), np.asarray(n), ids)]


def routed_of(cells):
    """{fid: whether the AI graph drives the road} of road Features cells (navgraph's rule: a DRIVE class, not a
    SKIP_SUBCLASSES subclass)."""
    import navgraph
    import roads as roadsmod
    out = {}
    for f in cells:
        if f is None or not len(f):
            continue
        cls = f.attrs.get("class")
        sub = f.rules.get("subclass")
        for i in range(len(f)):
            c = str(cls[i]) if cls is not None else "unknown"
            out[str(f.ids[i])] = c in navgraph.DRIVE and roadsmod._rule_value(sub, i) not in navgraph.SKIP_SUBCLASSES
    return out


def _on_roads(roads, pts):
    """line_feet of pts (m, 2) on the roads' lines: (m, 6) rows, and the roads it indexes."""
    import numpy as np
    import fast
    fast.require(612, "line_feet")
    lines = [np.ascontiguousarray(np.asarray(r[2], np.float64)[:, :2]) for r in roads]
    return fast._ef.line_feet(lines, np.ascontiguousarray(np.asarray(pts, np.float64).reshape(-1, 2)))


def barrier_rows(frame, box, points, roads, elev, routed):
    """Forest rows of the barriers on the roads (points: infra_points of BARRIER_CLASSES; roads: build_tile's roads_out
    rows (fid, spec, pts (k, 3), lon); routed: routed_of) with their point in box (x0, y0, x1, y1): a gate's leaf (two
    leaves on a road wider than one), a lift gate's cabinet and boom, a height restrictor's frame (the narrowest
    HR_WIDTHS one spanning the road), bollards. Open / closed as the module says. Cattle grids: cattle_grid_chunks."""
    import numpy as np
    import dnp
    import areaprops
    pts = [p for p in points if p[2] != "cattle_grid" and box[0] <= p[0] < box[2] and box[1] <= p[1] < box[3]]
    roads = [r for r in roads if len(r[2]) >= 2]
    if not pts or not roads:
        return []
    feet = _on_roads(roads, [(p[0], p[1]) for p in pts])
    rows = []
    for (x, y, cls, tags, fid), ft in zip(pts, feet.tolist()):
        li = int(ft[0])
        if li < 0:
            continue
        rf, spec = roads[li][0], roads[li][1]
        half = float(spec.width) / 2
        if ft[5] > half + BARRIER_ON_M:
            continue
        u = np.array([ft[3], ft[4]])
        left = np.array([-u[1], u[0]])
        c = np.array([ft[1], ft[2]])
        on_ai = routed.get(rf, True)
        closed = (not on_ai) and str(tags.get("access", "")).lower() in CLOSED_ACCESS
        yaw_u = float(np.degrees(dnp.arctan2(u[1], u[0])))
        yaw_left = yaw_u + 90.0
        r01 = round(0.3 * areaprops._h01(fid, "r"), 4)
        if cls in ("gate", "swing_gate"):
            n = 1 if 2 * half <= GATE_LEAF_M + 0.4 else 2
            leaf = min(1.25, max(0.8, (2 * half / n) / GATE_LEAF_M))
            for k in range(n):
                side = -1.0 if k == 0 else 1.0          # (hinged at the right edge, and the left for a pair)
                hinge = c + left * side * (half + EDGE_PAD_M)
                if closed:
                    yaw = yaw_left + (0.0 if side < 0 else 180.0)       # the leaf across, toward the middle
                else:
                    yaw = yaw_u + (180.0 if side < 0 else 180.0)        # swung open along the edge, back from the road
                rows.append(["dp_gate", hinge, yaw % 360.0, 1.35 * leaf, r01])
        elif cls == "lift_gate":
            cab = c - left * (half + EDGE_PAD_M)
            if closed:
                rows.append(["dp_liftgate", cab, yaw_left % 360.0, 1.04, r01])
            else:
                rows.append(["dp_liftgate_up", cab, yaw_left % 360.0, 5.55, r01])
        elif cls == "height_restrictor":
            w = next((v for v in HR_WIDTHS if v >= 2 * half + 0.2), HR_WIDTHS[-1])
            rows.append([f"dp_hr{w}", c, yaw_left % 360.0, 2.5, r01])
        elif cls == "bollard":
            if closed or not on_ai:
                rows.append(["dp_bollard", np.array([x, y]), 0.0, 0.95, r01])
            else:           # (a road the AI drives: at its edges, the carriageway kept open)
                for side in (-1.0, 1.0):
                    rows.append(["dp_bollard", c + left * side * (half + EDGE_PAD_M), 0.0, 0.95, r01])
    if not rows:
        return []
    xy = np.array([r[1] for r in rows], np.float64)
    z = areaprops._z(frame, elev, xy[:, 0], xy[:, 1])
    return [[g, round(float(p[0]), 2), round(float(p[1]), 2), round(float(zz), 2), round(float(yaw), 1), 1.0,
             float(h), r] for (g, p, yaw, h, r), zz in zip(rows, z.tolist())]


def cattle_grid_chunks(points, roads, box):
    """Road chunks (roads.build_tile's) of the cattle grids on the roads: a grid of CATTLE_GRID_MAT across the
    carriageway, CATTLE_GRID_LEN_M along it, flush (draped on the road: no collision of its own)."""
    import numpy as np
    pts = [p for p in points if p[2] == "cattle_grid" and box[0] <= p[0] < box[2] and box[1] <= p[1] < box[3]]
    roads = [r for r in roads if len(r[2]) >= 2]
    if not pts or not roads:
        return []
    feet = _on_roads(roads, [(p[0], p[1]) for p in pts])
    out = []
    for ft in feet.tolist():
        li = int(ft[0])
        if li < 0:
            continue
        half = float(roads[li][1].width) / 2
        if ft[5] > half + BARRIER_ON_M:
            continue
        u = np.array([ft[3], ft[4]])
        left = np.array([-u[1], u[0]])
        c = np.array([ft[1], ft[2]])
        h = CATTLE_GRID_LEN_M / 2
        poly = [c - u * h - left * half, c + u * h - left * half, c + u * h + left * half, c - u * h + left * half]
        out.append({"m": CATTLE_GRID_MAT, "w": 0.0, "tl": 1.0, "p": 30, "layer": "mark",
                    "poly": [[round(float(q[0]), 3), round(float(q[1]), 3), 0.0] for q in poly],
                    "uvq": [[0.0, 0.0], [CATTLE_GRID_LEN_M, 0.0], [CATTLE_GRID_LEN_M, 2 * half], [0.0, 2 * half]],
                    "n": []})
    return out


# --- BNE_RAIL_PROPS: railway signals and point machines where mapped (the drive shards' railway nodes) -----------------
SIGNAL_OFF_M = 2.4          # m from the track's axis a signal's mast stands (beside the structure gauge)
DWARF_OFF_M = 1.6
POINT_OFF_M = 1.5           # a point machine beside its switch
RAIL_ON_M = 2.0             # m from a track's line its node is that track's


def _signal_kind(tags):
    """'main' / 'dwarf' / None of a railway=signal node: a ground signal where it is mapped as one (a minor signal
    alone, or railway:signal:*:height / position=ground), else a main mast; overhead / bridge ones: None (their
    gantries are not drawn)."""
    pos = str(tags.get("railway:signal:position", "")).lower()
    if pos in ("bridge", "overhead"):
        return None
    keys = [k for k in tags if k.startswith("railway:signal:")]
    main = any(k.split(":")[2] in ("main", "distant", "combined") for k in keys if k.count(":") >= 2)
    minor = any(k.split(":")[2] in ("minor", "shunting") for k in keys if k.count(":") >= 2)
    if pos == "ground" or (minor and not main) or any(str(tags.get(k, "")).lower() == "dwarf" for k in keys):
        return "dwarf"
    return "main"


def rail_rows(frame, box, nodes, rail_lines, elev, left_running=False):
    """Forest rows of the mapped railway signals and point machines in box (nodes: tile_inputs' drive nodes; rail_lines:
    the tracks' lines (k, 2) in frame): a signal beside its track on the side railway:signal:position says (else the
    right of its direction; left_running: the left), facing the trains railway:signal:direction gives (forward: along the
    way, which the shards do not carry: along the nearest track's own points), a point machine beside each switch."""
    import numpy as np
    import dnp
    import areaprops
    if not RAIL_PROPS or not nodes or not rail_lines:
        return []
    pts = [n for n in nodes if n[2].get("railway") in ("signal", "switch")
           and box[0] <= n[0] < box[2] and box[1] <= n[1] < box[3]]
    if not pts:
        return []
    import fast
    fast.require(612, "line_feet")
    lines = [np.ascontiguousarray(np.asarray(l, np.float64)[:, :2]) for l in rail_lines if len(l) >= 2]
    if not lines:
        return []
    feet = fast._ef.line_feet(lines, np.ascontiguousarray(np.array([[p[0], p[1]] for p in pts], np.float64)))
    rows = []
    for (x, y, tags, nid), ft in zip(pts, feet.tolist()):
        if int(ft[0]) < 0 or ft[5] > RAIL_ON_M:
            continue
        u = np.array([ft[3], ft[4]])
        if str(tags.get("railway:signal:direction", "")).lower() == "backward":
            u = -u
        right = np.array([u[1], -u[0]])
        c = np.array([ft[1], ft[2]])
        r01 = round(0.3 * areaprops._h01(nid, "r"), 4)
        if tags.get("railway") == "signal":
            kind = _signal_kind(tags)
            if kind is None:
                continue
            pos = str(tags.get("railway:signal:position", "")).lower()
            side = -1.0 if (pos == "left" or (pos != "right" and left_running)) else 1.0
            off = SIGNAL_OFF_M if kind == "main" else DWARF_OFF_M
            p = c + right * side * off
            # (the model's face is -Y: toward the trains coming; its +Y along their way)
            yaw = float(np.degrees(dnp.arctan2(u[1], u[0]))) - 90.0
            rows.append(["dp_rsignal" if kind == "main" else "dp_rsignal_dwarf", p, yaw % 360.0,
                         4.75 if kind == "main" else 0.8, r01])
        else:
            side = -1.0 if str(tags.get("railway:turnout_side", "")).lower() == "right" else 1.0
            p = c + right * side * POINT_OFF_M
            yaw = float(np.degrees(dnp.arctan2(u[1], u[0])))
            rows.append(["dp_point", p, yaw % 360.0, 0.37, r01])
    if not rows:
        return []
    xy = np.array([r[1] for r in rows], np.float64)
    z = areaprops._z(frame, elev, xy[:, 0], xy[:, 1])
    return [[g, round(float(p[0]), 2), round(float(p[1]), 2), round(float(zz), 2), round(float(yaw), 1), 1.0,
             float(h), r] for (g, p, yaw, h, r), zz in zip(rows, z.tolist())]


def rail_lines_of(frame, cells):
    """The cells' railway lines (roads layer subtype rail, ground parts) in frame, each feature once."""
    import numpy as np
    import mapgeom
    import roads as roadsmod
    out, seen = [], set()
    for f in cells:
        if f is None or not len(f):
            continue
        sub = f.attrs.get("subtype")
        if sub is None:
            continue
        for i in np.flatnonzero(np.asarray(sub).astype(str) == "rail").tolist():
            fid = str(f.ids[i])
            if fid in seen:
                continue
            seen.add(fid)
            for part in roadsmod.ground_parts(f, i):
                e, n = mapgeom.enu_xy(f, frame, part)
                out.append(np.c_[np.asarray(e), np.asarray(n)])
    return out


# --- BNE_LAMP_TAGS: a mapped street lamp's mount, head count and colour (worldprops._street_lamps) --------------------
NO_POST_MOUNTS = ("wall", "wall_mounted", "suspended", "cable", "wire", "ceiling", "building")
LAMP_COLOURS = {"white": "white", "cool_white": "white", "warm_white": "yellow", "yellow": "yellow",
                "orange": "orange", "amber": "orange"}
# (lamp_type: the light's source where no colour is mapped: LEDs and metal halide white, sodium orange)
LAMP_TYPE_COLOURS = {"led": "white", "metal-halide": "white", "metal_halide": "white", "mercury": "white",
                     "fluorescent": "white", "sodium": "orange", "high_pressure_sodium": "orange",
                     "low_pressure_sodium": "orange"}


def lamp_tags(tags):
    """(group, height, colour or None) of a mapped street lamp's tags (worldprops.MAPPED_LAMP_MODELS' posts), None for
    one without a post of its own (lamp_mount on a wall or a wire): light:count 2+ on a plain or straight mast is the
    double-arm post; the colour from light:colour, else lamp_type / light:method (LED, sodium ...)."""
    import worldprops
    mount = str(tags.get("lamp_mount", "")).strip().lower()
    if mount in NO_POST_MOUNTS:
        return None
    group, height = worldprops.MAPPED_LAMP_MODELS.get(mount, ("streetlight", worldprops.STREET_LAMP_H))
    try:
        count = int(str(tags.get("light:count", "1")).split(";")[0])
    except ValueError:
        count = 1
    if count >= 2 and group in ("streetlight", "streetlight_straight_mast"):
        group, height = worldprops.MAPPED_LAMP_MODELS["double_arm"]
    colour = LAMP_COLOURS.get(str(tags.get("light:colour", "")).strip().lower())
    if colour is None:
        for k in ("lamp_type", "light:method"):
            colour = LAMP_TYPE_COLOURS.get(str(tags.get(k, "")).strip().lower().replace(" ", "_"))
            if colour:
                break
    return group, height, (colour if colour != "orange" else None)


KERB_KINDS = {"island": 3.0, "choker": 4.0, "chicane": 5.0}


def tile_kerbs(roads, nodes):
    """[(k, 3) outlines] of the kerbed traffic calming on the tile's roads (BNE_CALMING): a mapped island's refuge on
    the centre line, a choker's two build-outs, a chicane's two (earthfast drive_marks kinds 3-5); roads.build_tile
    draws each as a kerbed island (its splitters' curbs)."""
    import numpy as np
    if not CALMING or not nodes or not roads:
        return []
    paved = [r for r in roads if r[1].kind in ("paved", "cobble") and len(r[2]) >= 2]
    rows = []
    for x, y, tags, _i in nodes:
        for v in str(tags.get("traffic_calming") or "").lower().split(";"):
            k = KERB_KINDS.get(v.strip())
            if k is not None:
                rows.append([x, y, k, 0.0, 0.0, 0.0])
                break
    if not rows or not paved:
        return []
    import fast
    fast.require(612, "drive_marks")
    lines = [np.ascontiguousarray(np.asarray(r[2], np.float64)[:, :2]) for r in paved]
    half = [float(r[1].width) / 2 for r in paved]
    dirs = [0] * len(paved)
    lanes = [1] * len(paved)
    # (k, 3), z 0 as the other kerb runs' points (draped; build_tile's walks read three coordinates)
    out = []
    for p in fast._ef.drive_marks(lines, half, dirs, lanes, np.array(rows, np.float64), MARK_REACH_M, False, 8):
        p = np.asarray(p, np.float64)
        out.append(np.c_[p, np.zeros(len(p))])
    return out
