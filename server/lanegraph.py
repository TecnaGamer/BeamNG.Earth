"""Road surface v3, phase S1: the lane graph (.claude/work/road-surface-v3-plan-2026-11-19.md §2-3; the user approved
v3 on 2026-11-19: one continuous road surface, the ground surface first). Rust kernel: earthfast.lane_graph
(native/earthfast/src/roadshape/lanes.rs, whose module doc has the rules).

Switch BNE_ROADSURF_V3 (off / shadow / surface: S2's ground surface mesh built from these records, roadsurf3.py). BNE_ROADSURF is taken (roadsurf.py: the photo road
surface verdicts, a build switch on by default), hence the _V3.
- shadow, with BNE_ROADSURF_V3_DEBUG=1: each map cell's lane graph (strokes' links, anchored ordered lane boundaries,
  gores from the geometry, tapers from the TaperRule table) written as JSON beside the build, in
  <cache>/roadsurf_v3/lanes-<LANES_FORMAT>/<z>_<x>_<y>.json. NOTHING BUILT READS IT: plans, facts and tiles are the
  same bytes as with the switch off (the debug switch is revisit.NOT_SWITCHES; the mode switch stays keyed, for S2).
- off (the default): no code of this module runs.

Window independence: a cell's records are a function of the map within LANE_REACH_M of its home nodes (the rings
read cover it; every segment from one copy, roadshape._segments_round), whatever cell or order they are asked from.
tools/probes/laneprobe.py draws them on the finest aerial and scores them against the 17 measured places."""
import math
import os

import numpy as np

import dmath

MODE = os.environ.get("BNE_ROADSURF_V3", "off")
SHADOW = MODE == "shadow"
DEBUG = os.environ.get("BNE_ROADSURF_V3_DEBUG", "") in ("1", "on", "true")

LANES_VERSION = 570         # earthfast.lane_graph
LANES_FORMAT = 2            # the records' JSON (shadow_lanes' folder name; 2: roadshape.whole_or_sections)
LANE_REACH_M = 900.0        # m along every stroke from an event's node (inputs within it: an auxiliary lane's 750 m
                            # to the next exit and the walk past it)
LANE_DIR_M = 20.0           # m an arm's direction is read over (forks.rs's FORK_DIR_M)
LANE_FORK_DEG = 40.0        # a fork's pair within this of the trunk (forks.rs's FORK_DEG)
LANE_STEP_M = 1.0           # m between the branch's samples
LANE_OUT_M = 2.0            # m between rows
SHIFT_M = 40.0              # m an anchor offset change at a joint is blended over (quintic, centred on the joint)
AUX_MAX_M = 750.0           # m: an entrance of two or more lanes the road does not carry, and an exit on its side
                            # within this: one auxiliary lane between them (AASHTO / TxDOT: join them below 2,000 ft;
                            # MD-295's 600 m, the real-world study (d)5)
FEED_M = 600.0              # m: an added lane whose exit follows within this feeds it (its entry taper); a dropped
                            # lane after an entrance within this ends it (its merging taper)
NOSE_W_M = 4.0              # m: the painted gore's width at the physical nose (the study: noses 4-6 m in the US)
AT_GRADE_M = 60.0           # forks.rs's: an arm stopping this near at a junction with crossing traffic: a slip lane
MARGIN_M = 20.0             # m of rows before a taper's start / after its end

LANE_PARAMS = np.array([LANE_REACH_M, LANE_DIR_M, dmath.cos(math.radians(LANE_FORK_DEG)), LANE_STEP_M, LANE_OUT_M,
                        SHIFT_M, AUX_MAX_M, FEED_M, NOSE_W_M, AT_GRADE_M, MARGIN_M], np.float64)

# The TaperRule table: one row per region, from the standards and the 17 aerial-measured places of
# .claude/work/real-lane-changes-exits-study-2026-11-19.md (a): (V) verified there, (M) from memory, (A) the aerial.
TAPER_FIELDS = ("lane_w",       # m: a motorway / trunk lane
                "exit_k",       # an exit's entry taper (onto a parallel deceleration lane): exit_k x lane width m ...
                "exit_min",     # ... at least this
                "dec_par",      # m: the parallel deceleration lane (taper end to the theoretical gore) at >= 80 km/h
                "acc_par",      # m: the parallel acceleration lane (theoretical gore to the merging taper)
                "entry_k",      # an entrance's merging taper: entry_k x lane width m ...
                "entry_min",    # ... at least this
                "div_k",        # a taper-type divergence (a ramp splitting from a ramp): div_k x lane width m
                "gore_1",       # m: the painted gore, theoretical gore to physical nose, of a one-lane branch ...
                "gore_x",       # ... and m more per further branch lane
                "drop_k",       # a lane drop without a ramp: drop_k x the change m; 0: MUTCD L = W S / W S^2 / 60
                "add_place",    # the share of an added lane's taper before its mapped joint (OSM puts the change a
                                # little into the taper: Utrecht 14 of 80 m, Hwy 417 WB 26 of 110 m (A))
                "lht",          # 1: left-hand traffic
                "taper_merge",  # m: a taper-type entrance (a ramp joining a ramp) from the theoretical gore
                "gore_in")      # m: an entrance's painted gore, physical nose to the theoretical merge point
REGIONS = ("default", "US", "DE", "GB", "NL", "CA")
REGION_OF = {"US": 1, "DE": 2, "GB": 3, "NL": 4, "CA": 5}
TAPER_TABLE = np.array([
    # default: the US values
    [3.65, 21.0, 76.0, 150.0, 160.0, 30.0, 91.0, 20.0, 100.0, 70.0, 0.0, 0.2, 0.0, 80.0, 50.0],
    # US: AASHTO exit taper 250 ft = 76 m (V), entrance taper >= 300 ft = 91 m (V), measured 1:30 at I-270 (A); the
    # parallel lanes measured 140-200 / 160 m (A; the Green Book's tables (M) run longer); taper-type divergence 2-5
    # deg (V); gores I-95 100 m, I-270 85-90 m one lane, 170-180 m two (A); MD-295's taper-type entrance 80 m (A);
    # entrance gores I-95 NB 60 m, MD-295 50 m, I-270 0-30 m (A)
    [3.65, 21.0, 76.0, 150.0, 160.0, 30.0, 91.0, 20.0, 100.0, 70.0, 0.0, 0.2, 0.0, 80.0, 50.0],
    # DE (RAA 2008): Ausfaedelungs- / Einfaedelungsstreifen 250 m (V), Verziehung 60 m (M; the A3/A44 aerial 30-50 m,
    # so 50), the A3/A44 gore about 40 m (A); lanes 3.75 m (RQ 31/36, M)
    [3.75, 0.0, 50.0, 250.0, 250.0, 0.0, 60.0, 20.0, 50.0, 30.0, 0.0, 0.2, 0.0, 60.0, 30.0],
    # GB (DMRB CD 122): min auxiliary lane 200 m diverge / 230 m merge (V), merge taper 75 m (V), taper diverge
    # 170 m (V, div_k 46), nose 1:15 with a hatched nose <= 8 m (V: ~120 m), merge nose 1:40 / 115 m (V); left-hand
    # traffic
    [3.65, 25.0, 75.0, 200.0, 230.0, 0.0, 75.0, 46.0, 120.0, 30.0, 0.0, 0.2, 1.0, 75.0, 115.0],
    # NL (ROA/RONA: no numbers found): Utrecht's 1:22 taper, 200 m parallel lane, 95 m hatched gore, 3.5 m lanes (A)
    [3.5, 22.0, 75.0, 200.0, 200.0, 25.0, 75.0, 20.0, 95.0, 40.0, 0.0, 0.2, 0.0, 75.0, 50.0],
    # CA (TAC / OTM; Ontario): Hwy 417 WB 1:30 taper, 140 m parallel, 130 m gore; EB 105-130 m added lane, 1:34
    # taper, its nose 15 m before the merge (A); 3.5 m lanes (A)
    [3.5, 30.0, 100.0, 140.0, 120.0, 34.0, 100.0, 20.0, 130.0, 40.0, 0.0, 0.2, 0.0, 80.0, 30.0],
], np.float64)
assert TAPER_TABLE.shape == (len(REGIONS), len(TAPER_FIELDS))


def lane_rings(key):
    """Rings of cells round key that LANE_REACH_M can reach from a node inside it (at least 1)."""
    w = key.size_m()[0]
    return max(1, int(math.ceil(1.25 * LANE_REACH_M / max(w, 1.0))))


def _cell_lane_segments(key, cell_fn, memo):
    """The lane rows and native copy index of a decoded cell, validated together."""
    import fast
    import roadshape
    import countrybounds
    import revisit
    feats = cell_fn(key)
    if feats is None or not len(feats):
        return {}, fast._ef.SegmentCell(key.z, [], [], roadshape.COPY_CELLS_MAX)
    stamp = (key.z, getattr(feats, "_road_revision", 0), countrybounds.signature())
    saved = feats.__dict__.get("_lane_graph_rows")
    if saved is not None and saved[0] == stamp:
        return saved[1]
    with revisit.scope() as built:
        import railtags
        fast.require(553, "lane_cell_rows")
        names = ("lanes:forward", "lanes:backward", "turn:lanes:forward", "turn:lanes:backward",
                 "placement", "placement:forward")
        cols = [feats.attrs.get(railtags.lane_attr(name)) for name in names]
        import roads
        def columns(sp):
            if sp is None:
                return None
            cls = roadshape.FORK_CLASSES.get(sp.cls, 0) if sp.kind == "paved" else 0
            drive, inferred = roads.lane_layout(sp)
            return (cls, bool(sp.link), int(sp.dir or 0) if sp.oneway else 0, int(sp.lanes or -1),
                    float(sp.kmh or roadshape.CLASS_KMH.get(sp.cls, 50.)), sp.width, drive, inferred)

        ground = roads.road_specs(feats)
        bridges = (roads.bridge_specs(feats) if "_bridge_specs" in feats.__dict__ else
                   roads._bridge_specs(feats, ground_fallback=ground))
        specs = [columns(sp or bridge) for sp, bridge in zip(ground, bridges)]
        con = feats.rules.get("connectors")
        rows = fast._ef.lane_cell_rows(feats.lonlat, feats.part_off.tolist(), feats.ring_off.tolist(),
                                       feats.coord_off.tolist(), feats.ids.tolist(), specs,
                                       [list(c) if c is not None else None for c in cols],
                                       list(feats.attrs.get("osm_turn_lanes", ())),
                                       con.feature.tolist() if con else [], con.start.tolist() if con else [],
                                       [str(c) for c in con.value] if con else [])
        points = [(r["ll"][0, 1], r["ll"][0, 0]) for r in rows.values()]
        for r, code in zip(rows.values(), countrybounds.bundled().codes_at(points)):
            r["region"] = REGION_OF.get(str(code or "").upper(), 0)
    indexed = rows, fast._ef.SegmentCell(key.z, list(rows), [r["ll"] for r in rows.values()], roadshape.COPY_CELLS_MAX)
    if built.complete:
        feats._lane_graph_rows = stamp, indexed
    else:
        memo.setdefault("short_lane_rows", set()).add(key)
    return indexed


def cell_lanes(key, cell_fn, memo=None, read_cells=None, events_only=False):
    """The lane graph of map cell key's home nodes and segments (earthfast.lane_graph's JSON, parsed): {"events": [...]
    sorted by connector id, "segs": [...] by id, "links": the home nodes' stroke links, "frame": the cell's centre
    lat / lon, "cell"}, points in mm in the cell's frame. cell_fn(key) -> the cell's roads features; memo: {cell: segments} shared across calls (optional)."""
    import json
    import fast
    import geodesy
    import roadshape
    fast.require(553, "lane_connectors")
    memo = {} if memo is None else memo
    segs = roadshape._segments_round(key, cell_fn, memo, lane_rings(key), _cell_lane_segments, read_cells, indexed=True)
    fids = sorted(segs)
    lat0, lon0 = key.center()
    if not fids:
        return {"events": [], "segs": [], "links": [], "frame": [lat0, lon0], "cell": str(key)}
    frame = geodesy.EnuFrame(lat0, lon0)
    lines = [segs[fid]["ll"] for fid in fids]
    ll_all = np.concatenate(lines)
    e, n, _ = frame.geodetic_to_enu(ll_all[:, 1], ll_all[:, 0], 0.0)
    xy = np.column_stack((e, n))
    off = np.r_[0, np.cumsum([len(ll) for ll in lines])].tolist()
    col = lambda name: [segs[f][name] for f in fids]   # noqa: E731
    home, con_id, con_seg, con_vtx, con_home = fast._ef.lane_connectors(ll_all, off, col("cons"), key.bounds())
    fast.require(LANES_VERSION, "lane_graph")
    got = fast._ef.lane_graph(
        xy, off, fids, col("cls"), col("link"), col("dir"),
        col("lanes"), col("lfw"), col("lbw"), col("kmh"), col("lane_w"), col("turn"), col("turn_bw"), col("place"),
        col("region"), home, con_id, con_seg, con_vtx, con_home, LANE_PARAMS,
        np.ascontiguousarray(TAPER_TABLE.reshape(-1)), events_only, divided_crossings=(MODE == "surface"))
    out = json.loads(got)
    out["frame"] = [lat0, lon0]
    out["cell"] = str(key)
    return out


_SHADOW_DONE = set()


def shadow_lanes(cell_keys, cell_fn, cache_dir):
    """BNE_ROADSURF_V3=shadow with BNE_ROADSURF_V3_DEBUG=1: each map cell's lane graph (cell_lanes) as JSON in
    cache_dir/roadsurf_v3/lanes-<format>/, written once per cell and process (again by the next process: never a file
    of older map data or code kept). Never raises: nothing built reads them."""
    import json
    import logging
    for key in cell_keys:
        k = str(key)
        if k in _SHADOW_DONE:
            continue
        d = os.path.join(cache_dir, "roadsurf_v3", f"lanes-{LANES_FORMAT}")
        path = os.path.join(d, f"{key.z}_{key.x}_{key.y}.json")
        try:
            import overture
            rec = cell_lanes(key, cell_fn)
            os.makedirs(d, exist_ok=True)
            tmp = f"{path}.{os.getpid()}.part"
            with open(tmp, "w") as fh:      # (who made it from what: a reader can tell an older file)
                json.dump({"format": LANES_FORMAT, "kernel": LANES_VERSION, "release": overture.effective(),
                           "cell": k, "lanes": rec}, fh, sort_keys=True, separators=(",", ":"))
            os.replace(tmp, path)
            _SHADOW_DONE.add(k)
        except Exception as ex:      # noqa: BLE001 (shadow output: a fault never reaches the build)
            logging.getLogger("earthd.lanegraph").warning("lane graph of %s: %s: %s", k, type(ex).__name__, ex)
