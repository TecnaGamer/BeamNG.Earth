"""Road surface v3, phase S2: the ground road surface of motorways and trunks as ONE continuous mesh
(.claude/work/road-surface-v3-plan-2026-11-19.md §2-3; the user approved v3 on 2026-11-19, the ground surface first,
decks in S3). Rust kernel: earthfast.lane_surface (native/earthfast/src/roadshape/surface.rs, whose module doc has the
rules); the lane graph is S1's (lanegraph.py).

Switch BNE_ROADSURF_V3=surface (lanegraph.MODE; keyed like every BNE_* build switch, revisit.switches_key): in each road
tile the motorway / trunk roads' GROUND parts (no bridge or tunnel) have a continuous pavement footprint. Urban roads
painted into the terrain use that pavement directly. The other parts draw a mesh, each lane event of S1's (a diverge, merge, add or drop) as a piece of lane strips sharing their rows,
welded to the roads on shared rows; the road tile's own lines and overlays inside a piece are cut out (cut_lines) and the
piece's markings drawn from its boundaries: edge lines, lane lines (through-lane dashes go on past an exit), the exit /
auxiliary dashes of the added lane (the country's pattern, EXIT_DASH), the gore's channelizing lines and chevrons (US,
Canada) or hatching. With BNE_SURFACE_V2 the pieces get the variation overlay on their own triangles and the edge blends
on their outer edges. Heights: today's (the mesh is draped on the graded ground as every ground road surface). Ground pieces end at structures and leave crossing traffic to the junction painter. roadsurf3_decks uses the same lane mesh on supported bridge profiles.

Each tile reads the home cells of events that reach it and the neighbours that may trim those events. Each home
graph records its actual input cells, including distant copies of long segments. The per-process cache validates
those layers and their lane revisions on each use. Road tiles use revisit's code, map and surface-switch keys."""
from collections import OrderedDict
from threading import Lock

import math
import os

import numpy as np

import dnp
import lanegraph

ON = lanegraph.MODE == "surface"
SURF_VERSION = 550              # earthfast.lane_surface, cut_lines
V3_CLASSES = ("motorway", "trunk")
STRIP = {"through": 1, "gore": 2, "branch": 3, "branch_merging": 3, "added": 4, "dropped": 4}
LINE = ("edge", "edge_left", "lane", "exit", "channel", "stripe")
BLEND_MIN_M = 30.0              # m: a piece's end eased toward the surface it joins over at least this ...
BLEND_K = 60.0                  # ... BLEND_K x the largest move (a move of 1 m over 60 m: 0.031 m per m at the
BLEND_MAX_M = 150.0             # quintic's steepest, under the 1:21 exit taper's 0.048) ... at most this (and 45 % of it)
LINE_W_MIN = 0.75               # a lane line between a through lane and a branch / added lane where it is this wide
GORE_MIN_M = 0.4                # m: a gore this wide gets its channelizing lines (its stripes at twice this)
STRIPE_STEP_M = 12.0            # m between gore stripes (markings.GORE_STEP_M: MUTCD practice 20-50 ft)
HATCH_STEP_M = 8.0              # m between hatching stripes (Europe)
ROAD_STEP_M = 2.0               # m between a road strip's rows (roadmesh.DRAPE_STEP)
SNAP_M = 0.75                   # m: a boundary welded onto an earlier piece's end row takes its point this near
DECK_JOIN_M = 6.0               # m: a structure's end this far past a piece's last row is its join
CROSS_COS = 0.85                # a road crosses a piece's end row as its continuation within 32 degrees
STRUCT_COS = 0.9                # a structure trims a piece's rows when it runs within 26 degrees of it
CUT_M = 0.3                     # m past a piece's outer edges the tile's own lines and overlays are cut out ...
CUT_WIDE_M = 2.5                # ... and its edge blends (they lie up to 1.9 m outside the asphalt)
BOX_MARGIN_M = 8.0              # m round the tile the kernel keeps triangles
END_EXT_M = 25.0                # m past a piece's edges its end rows look for the roads going on (a ramp's mapped line
                                # beside the lane S1 gives it at the nose)
ROAD_CLEAR = 0.9                # a road strip is cut where it comes this share of its half width onto a piece
REACH_M = 60.0                  # m round the pieces the roads are read
CHEVRON = {"US", "CA"}          # chevrons in the gores; elsewhere hatching
# exit / auxiliary lane lines (dash, gap) m: the real-world study's measured values (US: the state profile's ramp
# pattern, roadmarkings); a country without a measured one: 3 / 3
EXIT_DASH = {"DE": (6.0, 6.0), "AT": (6.0, 6.0), "CH": (6.0, 6.0), "NL": (1.15, 2.8), "CA": (3.0, 3.0)}
EXIT_DASH_DEFAULT = (3.0, 3.0)
EXIT_W = 0.20                   # m: the wide dotted lane line (MUTCD 3B.04: 8 in)
CHANNEL_W = 0.20                # m: the gore's channelizing lines (markings.GORE_CHANNEL_W)
STRIPE_W = 0.30                 # m: the gore stripes (markings.GORE_W)
VAR_LOOK = {"m": "bne_wear_var_01", "p": 5, "layer": "var", "tl": 12.0}
EDGE_LIFT_LAYERS = ("mark", "edge", "gutter", "wear", "wear2", "wline", "spot", "var", "patch", "frontage")


def params(country):
    chev = str(country or "").upper() in CHEVRON
    return np.array([BLEND_MIN_M, BLEND_K, BLEND_MAX_M, LINE_W_MIN, GORE_MIN_M,
                     STRIPE_STEP_M if chev else HATCH_STEP_M, 1.0 if chev else 0.0, ROAD_STEP_M, SNAP_M, DECK_JOIN_M,
                     CROSS_COS, STRUCT_COS, CUT_M, CUT_WIDE_M, END_EXT_M, ROAD_CLEAR], np.float64)


_LANES_LRU = OrderedDict()
_LANES_LOCK = Lock()
_LANES_LRU_N = 48

# A home cell's lane graph kept on disk for the server's other processes and later runs of the same code (2026-11-21,
# road waits: every road, terrain and photo process made each one again, reading the 25-odd cells round it, their
# road specs and those cells' neighbours; 3-5 s of a Montreal road tile). Beside the home cell's roads mirror (pruned
# with the mirror: mapdata.prune_mirror). Its key: LANES_DISK_FORMAT, the code and the build switches (revisit.code_key:
# any change of the server's modules or its native module makes them again), the home cell, the border data, the map
# data's versions (mapdata.MapData.versions: the release, the tag data, the DEM, the pipeline's switches) and the lane
# kernel's version and mode. Its read list: every cell the graph read (its input window, distant copies too) with its
# roads file's stamp, checked on every read. Kept only when complete: nothing read short, every read cell's layer from
# its own file with the tag data its decode asked for (mapdata.MapData.roads_complete), its revision unchanged.
# BNE_LANES_DISK=off: in memory only, as before. A reader passes its MapData as the cell function's lanes_md.
LANES_DISK = os.environ.get("BNE_LANES_DISK", "on") not in ("off", "0", "false")
LANES_DISK_FORMAT = 1


def _cell_lanes(key, cell_fn, memo):
    import countrybounds
    import revisit
    # Per tile, read each dependency once, including an absent cell. Keep the
    # snapshots with this tile's segment memo so all home graphs see the same inputs.
    snapshots = memo.setdefault("features", {})

    def snapshot(k):
        if k not in snapshots:
            with revisit.scope() as read:
                f = cell_fn(k)
            # An absent layer has no row data. Its presence (and any read fault)
            # is checked again on every use, including cache hits.
            if f is not None and not read.complete:
                memo.setdefault("short_lane_rows", set()).add(k)
            # A token survives in the cache key without retaining the layer. Unlike
            # id(f), a newly decoded feature can never recycle an evicted layer's token.
            stamp = None if f is None else (f.__dict__.setdefault("_lane_graph_token", object()),
                                            getattr(f, "_road_revision", 0))
            snapshots[k] = f, stamp
        return snapshots[k]

    home = (key, countrybounds.signature())
    with _LANES_LOCK:
        entry = _LANES_LRU.get(home)
    if entry is not None:
        keys, stamps, got = entry
        if all(snapshot(k)[1] == stamp for k, stamp in zip(keys, stamps)):
            with _LANES_LOCK:
                if _LANES_LRU.get(home) is entry:
                    _LANES_LRU.move_to_end(home)
            return got
    md = getattr(cell_fn, "lanes_md", None) if LANES_DISK else None
    disk = _disk_key(md, key, memo) if md is not None else None
    if disk is not None:
        got = _disk_load(md, key, disk)
        if got is not None:
            # (not into the LRU: its entries are checked by the decoded layers' tokens, which a hit never decodes)
            return got
    keys = []
    with revisit.scope() as built:
        got = lanegraph.cell_lanes(key, lambda k: snapshot(k)[0], memo, read_cells=keys, events_only=True)
    stamps = [snapshot(k)[1] for k in keys]
    if any(stamp is not None and getattr(snapshots[k][0], "_road_revision", 0) != stamp[1]
           for k, stamp in zip(keys, stamps)):
        # Lane tags can join a decoded layer while another tile builds its graph.
        revisit.fault()
    elif not memo.get("short_lane_rows", set()).intersection(keys):
        with _LANES_LOCK:
            _LANES_LRU[home] = (keys, stamps, got)
            _LANES_LRU.move_to_end(home)
            if len(_LANES_LRU) > _LANES_LRU_N:
                _LANES_LRU.popitem(last=False)
        if disk is not None and built.complete:
            _disk_store(md, key, disk, got, [(k, snapshots[k][0]) for k in keys])
    return got


def _disk_key(md, key, memo):
    """The disk key (a str) of home cell key's lane graph, or None (no mirror, no code key)."""
    import countrybounds
    import hashlib
    import json
    import revisit
    try:
        vers = memo.get("_lanes_versions")
        if vers is None:            # (once per build: the same for every home cell it reads)
            vers = memo["_lanes_versions"] = json.dumps(md.versions(), sort_keys=True, default=str)
        parts = [LANES_DISK_FORMAT, revisit.code_key(), str(key), countrybounds.signature(), vers,
                 lanegraph.LANES_VERSION, lanegraph.LANES_FORMAT, lanegraph.MODE, True]
        return hashlib.sha256(json.dumps(parts, default=str).encode("utf-8")).hexdigest()
    except Exception:               # (no key: made here, as before)
        return None


def _disk_load(md, key, want):
    """Home cell key's lane graph kept under key want whose every read cell's roads file is as it was, or None."""
    import json
    d = md.roads_product_dir(key, "lanes")
    if d is None:
        return None
    try:
        with open(os.path.join(d, "ok"), encoding="utf-8") as fh:
            got, head = fh.readline().rstrip("\n"), fh.read()
        if got != want:
            return None
        rec = json.loads(head)
        for cell, stamp in rec["read"]:
            if md.roads_stamp(cell) != stamp:
                return None
        os.utime(os.path.join(d, "ok"))         # (read: the last to go, as the mirror's)
        return rec["graph"]
    except FileNotFoundError:
        return None
    except Exception:               # (damaged, or another format's: made again)
        return None


def _disk_store(md, key, want, got, reads):
    """Home cell key's lane graph got kept under key want with its read list, when every read cell's layer may be
    (reads: [(cell, its decoded layer or None)]); nothing raised."""
    import json
    import shutil
    import threading
    d = md.roads_product_dir(key, "lanes")
    if d is None:
        return
    tmp = None
    try:
        read = []
        for k, f in reads:
            stamp = md.roads_stamp(str(k))
            if f is None or stamp is None or not md.roads_complete(k, f):
                return              # (a cell not there, a stand-in, its tags not joined: made again next time)
            read.append([str(k), stamp])
        body = json.dumps({"read": read, "graph": got}, separators=(",", ":"))
        tmp = f"{d}.{os.getpid()}.{threading.get_ident()}.tmp"
        os.makedirs(tmp, exist_ok=True)
        with open(os.path.join(tmp, "ok"), "w", encoding="utf-8") as fh:
            fh.write(want + "\n" + body)
        if os.path.isdir(d):        # (another key's, or another process's of the same)
            shutil.rmtree(d, ignore_errors=True)
        os.rename(tmp, d)
        tmp = None
    except Exception:
        pass
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


def _cells_of(frame, box):
    import mapcells
    x0, y0, x1, y1 = box
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x1, x0, x1]), np.array([y0, y0, y1, y1]), 0.0)
    return sorted(mapcells.cells_for_bbox(float(np.min(la)), float(np.min(lo)), float(np.max(la)), float(np.max(lo))),
                  key=str)


def _to_frame(frame, rec_frame, mm):
    """Points in mm in a lane record's cell frame -> frame's east / north (m)."""
    import geodesy
    fr = geodesy.EnuFrame(*rec_frame)
    p = np.asarray(mm, np.float64).reshape(-1, 2) / 1000.0
    la, lo, _ = fr.enu_to_geodetic(p[:, 0], p[:, 1], 0.0)
    e, n, _u = frame.geodetic_to_enu(np.asarray(la, np.float64), np.asarray(lo, np.float64), 0.0)
    return np.c_[np.asarray(e, np.float64), np.asarray(n, np.float64)]


def _projected_events(frame, graph):
    # A home graph is immutable and dependency-checked. Keep just its most
    # recent destination frame, so many player anchors cannot grow the LRU.
    key = (frame.lat0, frame.lon0, frame.h0)
    hit = graph.get("_projected")
    if hit is not None and hit[0] == key:
        return hit[1]
    out = []
    for ev in graph["events"]:
        rows = ev.get("rows")
        if not rows or ev["kind"] not in ("diverge", "merge", "add", "drop"):
            continue
        if "opposite" in ev.get("strips", ()) or any(f in ev.get("flags", ()) for f in ("at_grade", "unordered")):
            continue
        r = np.asarray(rows, np.float64)
        xy = _to_frame(frame, graph["frame"], r[:, 1:3])
        reach = float(np.max(np.abs(r[:, 5:]))) / 1000.0 + 1.0
        bb = (float(xy[:, 0].min()) - reach, float(xy[:, 1].min()) - reach, float(xy[:, 0].max()) + reach,
              float(xy[:, 1].max()) + reach)
        out.append((ev, np.c_[r[:, 0] / 1000.0, xy], r[:, 5:] / 1000.0, bb, reach))
    graph["_projected"] = key, out
    return out


def tile_events(frame, box, cell_fn, memo=None):
    """The lane events whose rows reach box (x0, y0, x1, y1, in frame): [(event, rows in frame (k, 3): s m, x, y,
    boundaries (k, nb) m)], read from every cell within lane_rings of the box's cells, each event once (its home
    cell's), in the kernel's order: forks before adds and drops, then node id."""
    # Snapshot only this build. Cross-tile reuse is validated in _cell_lanes.
    return _tile_events(frame, box, cell_fn, {})


def _tile_events(frame, box, cell_fn, memo):
    # Deck and ground ask for the same event window within a tile. A build
    # memo owns its dependency snapshot, including any reads marked short.
    windows = memo.setdefault("event_windows", {})
    window = (frame.lat0, frame.lon0, frame.h0, *box)
    if window in windows:
        return windows[window]
    graphs = memo.setdefault("home_graphs", {})
    # S1 walks at most LANE_REACH_M along each arm. Allow 256 m across
    # the rows (both carriageways, their placement offsets and the painted nose).
    pad = lanegraph.LANE_REACH_M + 256.0 + BOX_MARGIN_M
    def homes(b):
        return _cells_of(frame, (b[0] - pad, b[1] - pad, b[2] + pad, b[3] + pad))

    x0, y0, x1, y1 = box
    m = BOX_MARGIN_M + BLEND_MAX_M + REACH_M
    cand = []

    def collect(key):
        if key not in graphs:
            graphs[key] = _cell_lanes(key, cell_fn, memo)
        for ev, r, bounds, bb, reach in _projected_events(frame, graphs[key]):
            xy = r[:, 1:]
            mm_ = m + reach
            here = bool(((xy[:, 0] >= x0 - mm_) & (xy[:, 0] <= x1 + mm_) & (xy[:, 1] >= y0 - mm_)
                         & (xy[:, 1] <= y1 + mm_)).any())
            cand.append((ev, r, bounds, bb, here))
    # the pieces reaching the tile and every piece whose box meets one of theirs (the ones that may trim them: each
    # piece the same whichever tile reads it)
    direct = set(homes((x0 - m, y0 - m, x1 + m, y1 + m)))
    for key in sorted(direct):
        collect(key)
    near = [c[3] for c in cand if c[4]]
    context = {key for b in near for key in homes(b)}
    for key in sorted(context - direct):
        collect(key)
    meets = lambda a, b: not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])   # noqa: E731
    out = [(ev, r, t) for ev, r, t, bb, here in cand if here or any(meets(bb, nb) for nb in near)]
    rank = {"diverge": 0, "merge": 0, "add": 1, "drop": 1}
    out.sort(key=lambda e: (rank[e[0]["kind"]], e[0]["node"], e[0]["kind"], str(e[0].get("segs"))))
    windows[window] = out
    return out


def tile_roads(frame, box, cell_fn, urban_fn=None, widths=None, left_hand=False):
    """The v3-class roads round box: ({base fid: spec}, ground parts [(fid, spec, xy (k, 2))], structures [(fid, spec,
    xy (k, 2))]) in frame, every segment id from one copy (the most points, then the least cell), sorted by fid. A
    ground part's line as the road tile draws it (roads.ground_parts, the placement's shift)."""
    import lanesections
    import roads as roadsmod
    import mapcells
    keys = _cells_of(frame, box)
    by_cell = {}

    def cell(k):
        ks = str(k)
        if ks not in by_cell:
            f = cell_fn(k)
            by_cell[ks] = (k, f if f is not None and len(f) else None)
        return by_cell[ks]
    for k in keys:
        cell(k)
    best = {}

    def scan(ks):
        k, feats = by_cell[ks]
        if feats is None:
            return []
        specs = roadsmod.road_specs(feats, urban_fn, widths, left_hand)
        bspecs = roadsmod.BridgeProfiles.specs_of(feats)
        found = []
        for i in range(len(feats)):
            sp = specs[i] if specs[i] is not None else bspecs[i]
            if sp is None or sp.cls not in V3_CLASSES or sp.kind != "paved":
                continue
            fid = str(feats.ids[i])
            n = sum(len(r) for part in feats.parts(i) for r in part)
            cand = (-n, ks, i, sp, specs[i] is not None)
            if fid not in best or cand[:2] < best[fid][:2]:
                best[fid] = cand
            found.append((fid, feats, i))
        return found
    # every copy of a segment from every cell its points lie in (the most points, then the least cell: the same copy
    # whichever tile reads it, as roadshape._segments_round)
    first = []
    for ks in sorted(by_cell):
        first.extend(scan(ks))
    more = set()
    for fid, feats, i in first:
        ll = np.concatenate([np.asarray(r, np.float64) for part in feats.parts(i) for r in part])
        cs = mapcells.cells_for_points(ll[:, 1], ll[:, 0], z=keys[0].z) if len(ll) else []
        if len(cs) <= 16:
            for c in cs:
                if str(c) not in by_cell:
                    cell(c)
                    more.add(str(c))
    for ks in sorted(more):
        scan(ks)
    replaced = set()
    if lanesections.ON:
        replaced = lanesections.replaced_roads([f for _k, f in by_cell.values() if f is not None])
    best = {f: v for f, v in best.items() if f not in replaced}
    feats_list = [by_cell[ks][1] for ks in sorted(by_cell) if by_cell[ks][1] is not None]
    placement = roadsmod.placement_context(feats_list, urban_fn, widths, left_hand)
    x0, y0, x1, y1 = box
    specs_of, ground, structs = {}, [], []

    def xy_of(ring):
        e, n, _ = frame.geodetic_to_enu(np.asarray(ring[:, 1], np.float64), np.asarray(ring[:, 0], np.float64), 0.0)
        return np.c_[np.asarray(e, np.float64), np.asarray(n, np.float64)]

    def near(xy):
        return not (xy[:, 0].max() < x0 or xy[:, 0].min() > x1 or xy[:, 1].max() < y0 or xy[:, 1].min() > y1)
    for fid in sorted(best):
        _n, ks, i, sp, has_road = best[fid]
        feats = by_cell[ks][1]
        specs_of[fid] = sp
        if has_road:
            for ring in roadsmod.ground_parts(feats, i):
                ring = np.asarray(ring, np.float64)
                if len(ring) < 2 or not near(xy_of(ring)):
                    continue
                ring = placement.ring(frame, feats, i, ring, sp, fid)
                xy = xy_of(np.asarray(ring, np.float64))
                if sp.oneway and (sp.dir or 1) < 0:
                    xy = xy[::-1].copy()        # (in its travel direction: the kernel's continuation test)
                if len(xy) >= 2:
                    ground.append((fid, sp, xy))
        parts = (roadsmod.bridge_parts(feats, i) + roadsmod.tunnel_parts(feats, i)) if has_road else \
            [r for part in feats.parts(i) for r in part]
        for ring in parts:
            ring = np.asarray(ring, np.float64)
            if len(ring) >= 2:
                xy = xy_of(ring)
                if near(xy):
                    structs.append((fid, sp, xy))
    return specs_of, ground, structs


def _base(fid):
    return str(fid).split("|", 1)[0].split("~", 1)[0]


def tile_surface(frame, box, cell_fn, urban_fn=None, widths=None, left_hand=False, country=None, memo=None, build_memo=None, outlines_only=False):
    """The kernel's result for the tile box: a dict (mesh: v (n, 2), uv, tri (m, 3), owner, kind; lines: list of
    (kind name, xy (k, 2), phase m, piece); outlines, cut / cut_wide polygons per kept piece; pieces: [(event, main
    spec)]; roads: [(fid, spec)]; status), or None (no v3 road near)."""
    import fast
    import roads as roadsmod
    fast.require(SURF_VERSION, "lane_surface")
    evs = (tile_events(frame, box, cell_fn, memo) if build_memo is None else
           _tile_events(frame, box, cell_fn, build_memo))
    if outlines_only and not evs:
        return None
    roi = list(box)
    for _ev, r, _t in evs:
        roi = [min(roi[0], float(r[:, 1].min())), min(roi[1], float(r[:, 2].min())),
               max(roi[2], float(r[:, 1].max())), max(roi[3], float(r[:, 2].max()))]
    roi = (roi[0] - REACH_M, roi[1] - REACH_M, roi[2] + REACH_M, roi[3] + REACH_M)
    specs_of, ground, structs = tile_roads(frame, roi, cell_fn, urban_fn, widths, left_hand)
    if not ground and not evs:
        return None
    return _mesh_surface(box, country, evs, specs_of, ground, structs, outlines_only)


def _mesh_surface(box, country, evs, specs_of, ground, structs, outlines_only=False):
    import roads as roadsmod
    pieces, rows, t, toff, kinds, koff, meta = [], [], [], [0], [], [0], []
    for ev, r, tb in evs:
        segs = [_base(s) for s in ev.get("segs", ()) if s]
        main = specs_of.get(segs[1] if len(segs) > 1 else "") or specs_of.get(segs[0] if segs else "")
        if main is None:
            continue
        ks = [STRIP.get(n) for n in ev["strips"]]
        if any(k is None for k in ks) or tb.shape[1] != len(ks) + 1:
            continue
        keep = np.r_[True, np.diff(r[:, 0]) > 1e-6]
        r, tb = r[keep], tb[keep]
        if len(r) < 3:
            continue
        drive, _lanes = roadsmod.lane_layout(main)
        sh = max(0.0, (float(main.width) - float(drive)) / 2.0)
        lw = float(ev.get("lane_w", 3650)) / 1000.0
        shl = shr = sh
        branch = specs_of.get(segs[2]) if ev["kind"] in ("diverge", "merge") and len(segs) > 2 else None
        if branch is not None:
            # the branch side's shoulder the branch road's own (its paved width less its lanes, halved: its inner
            # shoulder is the gore's)
            lb = max(1, int(ev.get("lanes", [0, 0, 1])[2] or 1))
            bsh = max(0.0, (float(branch.width) - lb * lw) / 2.0)
            if ev.get("side") == "left":
                shl = bsh
            else:
                shr = bsh
        pieces.append((ev, main))
        rows.append(r)
        t.append(tb.reshape(-1))
        toff.append(toff[-1] + tb.size)
        kinds.extend(ks)
        koff.append(len(kinds))
        meta.append((shl, shr, lw))
    rxy = [g[2] for g in ground]
    sxy = [s[2] for s in structs]
    m = BOX_MARGIN_M
    stack = lambda parts, c: np.ascontiguousarray(np.concatenate(parts) if parts else np.zeros((0, c)), np.float64)  # noqa: E731
    offs = lambda parts: np.r_[0, np.cumsum([len(p) for p in parts])].astype(np.int64)  # noqa: E731
    inp = {"rows": stack(rows, 3), "row_off": offs(rows), "t": stack([x[:, None] for x in t], 1).reshape(-1),
           "t_off": np.asarray(toff, np.int64), "kinds": np.asarray(kinds, np.int64), "k_off": np.asarray(koff, np.int64),
           "pmeta": np.ascontiguousarray(np.asarray(meta, np.float64).reshape(-1, 3)),
           "rxy": stack(rxy, 2), "r_off": offs(rxy), "r_hw": np.array([float(g[1].width) / 2.0 for g in ground]),
           "r_oneway": np.array([bool(g[1].oneway) for g in ground], bool),
           "sxy": stack(sxy, 2), "s_off": offs(sxy), "s_hw": np.array([float(x[1].width) / 2.0 for x in structs]),
           "box": np.array([box[0] - m, box[1] - m, box[2] + m, box[3] + m], np.float64), "params": params(country)}
    got = run_kernel(inp, outlines_only) if outlines_only else run_kernel(inp)
    got.update(pieces=pieces, roads=[(g[0], g[1]) for g in ground], inputs=inp)
    return got


def run_kernel(inp, outlines_only=False):
    """earthfast.lane_surface on tile_surface's inputs (a dict of arrays: a test fixture keeps them): its result without
    the pieces' and roads' records (tile_surface adds them)."""
    import json
    import fast
    fast.require(SURF_VERSION, "lane_surface")
    (v, uv, tri, owner, kind), (lxy, loff, lkind, lphase, lpiece), (oxy, ooff), status = fast._ef.lane_surface(
        np.ascontiguousarray(inp["rows"], np.float64).reshape(-1, 3), np.asarray(inp["row_off"]).tolist(),
        np.ascontiguousarray(inp["t"], np.float64), np.asarray(inp["t_off"]).tolist(), np.asarray(inp["kinds"]).tolist(),
        np.asarray(inp["k_off"]).tolist(), np.ascontiguousarray(inp["pmeta"], np.float64).reshape(-1, 3),
        np.ascontiguousarray(inp["rxy"], np.float64), np.asarray(inp["r_off"]).tolist(),
        [float(x) for x in inp["r_hw"]], [bool(x) for x in inp["r_oneway"]],
        np.ascontiguousarray(inp["sxy"], np.float64).reshape(-1, 2), np.asarray(inp["s_off"]).tolist(),
        [float(x) for x in inp["s_hw"]], [float(b) for b in inp["box"]],
        np.ascontiguousarray(inp["params"], np.float64), *([True] if outlines_only else []))
    lxy = np.asarray(lxy).reshape(-1, 2)
    lines = [(LINE[int(lkind[k])], lxy[loff[k]:loff[k + 1]], float(lphase[k]), int(lpiece[k]))
             for k in range(len(lkind))]
    oxy = np.asarray(oxy).reshape(-1, 2)
    polys = [oxy[ooff[k]:ooff[k + 1]] for k in range(len(ooff) - 1)]
    n = len(polys) if outlines_only else len(polys) // 3
    st = json.loads(status)
    return {"mesh": {"v": np.asarray(v).reshape(-1, 2), "uv": np.asarray(uv).reshape(-1, 2),
                     "tri": np.asarray(tri, np.int64).reshape(-1, 3), "owner": np.asarray(owner, np.int64),
                     "kind": np.asarray(kind, np.int64)},
            "lines": lines, "outlines": polys[:n], "cut": polys[n:2 * n], "cut_wide": polys[2 * n:],
            "kept": [x["piece"] for x in st if x.get("kept")], "status": st}


def grade_plans(planner, box, plans, specs, country):
    """Give P1a the welded ground outline of a kept lane event on its main road."""
    import fast
    import grading
    import navgraph
    major = [(fid, p, sp) for (fid, p), sp in zip(plans, specs) if sp.kind == "paved" and sp.cls in V3_CLASSES]
    if not major or box is None:
        return
    fast.require(555, "surface_grade_sections")
    got = tile_surface(planner.frame, box, planner._roads_cell, widths=planner.widths,
                       left_hand=navgraph.road_rules(country)["rightHandDrive"], country=country, outlines_only=True)
    outlines = {}
    for k, outline in zip((got or {}).get("kept", ()), (got or {}).get("outlines", ())):
        event = got["pieces"][k][0]
        for fid in sorted({_base(fid) for fid in event["segs"][:2]}):
            outlines.setdefault(fid, []).append(outline)
    for fid, plan, _spec in major:
        edges = outlines.get(_base(fid))
        if not edges:
            continue
        rows = fast._ef.surface_grade_sections(plan.xy, edges)
        inside = rows[:, 2] > 0.
        if not inside.any():
            continue
        # PlanPack interpolates one width and bank along its centreline.
        # Recentring the section carries its old bank plane onto the new axis.
        plan.xy = rows[:, :2].copy()
        if plan.bank is not None:
            plan.z = plan.z + plan.bank * rows[:, 3]
        widen = np.zeros(len(rows)) if plan.widen is None else plan.widen.copy()
        # PlanPack adds widen to both core and banking widths. A clipped
        # nose can be narrower than the old shoulder. Use one physical edge
        # here, preserving the old full core on the rest of this way.
        widen += plan.half - plan.road_half
        plan.half = plan.road_half
        widen[inside] = rows[inside, 2] - plan.road_half
        plan.surface_width = True
        grading.set_widen(plan, widen)


def street_pavement(spec):
    """The mapped cross section of a paved urban arterial, including turn-lane tapers and median gaps."""
    return spec.kind == "paved" and spec.cls == "trunk" and spec.urban


def _painted_street_surface(spec):
    """Kerbed surface streets can extend beyond the urban land-cover polygon.
    Their ground painter already owns the pavement, including any V3 triangles.
    """
    if spec.kind != "paved" or spec.cls != "trunk":
        return False
    import roads
    return spec.urban or roads.has_sidewalks(spec) or (
        roads.has_median(spec) and spec.kmh is not None and roads._arterial(spec))


def _signalled_link_owners(frame, got, signal_fn):
    """Ordinary ground trunk links which meet a mapped traffic signal. Read both original
    ends, even when the painted tile contains only the middle of the link."""
    import fast
    import furniture
    candidates = []
    keys = set()
    for owner in np.unique(got["mesh"]["owner"]):
        if owner >= 0:
            continue
        i = -1 - int(owner)
        spec = got["roads"][i][1]
        if spec.cls != "trunk" or not spec.link or _painted_street_surface(spec):
            continue
        offsets = got["inputs"]["r_off"]
        ends = got["inputs"]["rxy"][[offsets[i], offsets[i + 1] - 1]]
        reach = furniture.SIGNAL_NEAR_M + spec.width / 2
        for x, y in ends:
            keys.update(_cells_of(frame, (x - reach, y - reach, x + reach, y + reach)))
        candidates.append((owner, ends, reach))
    if not keys:
        return set()
    signals = signal_fn(sorted(keys))
    if signals is None or not len(signals):
        return set()
    signals = np.ascontiguousarray(signals, np.float64)
    fast.require(0, "points_near_segments")
    return {owner for owner, ends, reach in candidates
            if np.asarray(fast._ef.points_near_segments(ends, signals, signals, np.full(len(signals), reach))).any()}


def replaces_ground_paint(spec):
    return spec.kind == "paved" and spec.cls in V3_CLASSES and not street_pavement(spec)


def paint_geometry(surface, frame, box, widths, pending=None):
    """Ground triangles for the terrain painter. Use its cached map cells and queue
    missing ones through Surface, as for the other layers of a terrain block."""
    import countrybounds
    import navgraph
    import revisit

    missing = set()

    def cell(key):
        if not surface.md.has(key, "roads"):
            if key not in missing:
                missing.add(key)
                surface._queue(key, ("roads",))
            return None
        return surface.md.cell(key, layers=("roads",), kind="background").layers.get("roads")
    cell.lanes_md = surface.md          # (the lane graphs kept on disk: LANES_DISK; a cell not there is never kept)

    try:
        country = countrybounds.bundled().at(frame.lat0, frame.lon0)
        got = tile_surface(frame, box, cell, surface.urban, widths, navgraph.road_rules(country)["rightHandDrive"],
                           country)
        if missing:
            raise LookupError(f"{len(missing)} road cells pending")
        if got is None:
            return None
        mesh = got["mesh"]
        # Urban trunk roads already paint their mapped joint tapers, corner fills, sidewalk
        # margins and median gaps. Only an actual lane event adds coverage to those streets.
        # Negative owner IDs index ordinary roads from the end of the same lookup tables.
        specs = [sp for _, sp in got["pieces"]] + [sp for _, sp in reversed(got["roads"])]
        owner = mesh["owner"]
        concrete = np.array([sp.surf == "concrete" for sp in specs], np.uint8)
        keep = (owner >= 0) | np.array([not street_pavement(sp) for sp in specs], bool)[owner]
        return mesh["v"], mesh["tri"][keep], concrete[owner[keep]]
    except Exception as ex:
        import logging
        revisit.fault()
        if pending is not None:
            from surface import PHOTO_ROADS_PENDING
            pending.add(PHOTO_ROADS_PENDING)
        logging.getLogger("earthd.roadsurf3").debug("road paint at %s: %s", box, ex)
        return None


def paint_coverage(geom, xy, width, height, antialias):
    import fast
    fast.require(550, "pavement_coverage")
    return fast._ef.pavement_coverage(np.ascontiguousarray(xy, np.float64), geom[1], geom[2], width, height, antialias)


def cut_chunks(chunks, cut, cut_wide, is_edge):
    """chunks with their draped lines and overlays' parts inside the polygons cut out (cut; cut_wide for is_edge(chunk):
    the edge blends) and a filled polygon whose middle lies inside dropped; decks, tunnels and fixed heights kept."""
    import fast
    if not cut:
        return chunks
    out, todo = [], []
    for c in chunks:
        if c.get("deck") or c.get("fixed") or c.get("tunnel") or c.get("tri") or c.get("ctx"):
            out.append(c)
            continue
        if c.get("poly") is not None:
            p = np.asarray(c["poly"], np.float64)
            if len(p) and _inside_any(p[:, :2].mean(axis=0), cut_wide if is_edge(c) else cut):
                continue
            out.append(c)
            continue
        n = c.get("n")
        if n is None or len(n) < 2:
            out.append(c)
            continue
        todo.append(c)
    for wide in (False, True):
        sel = [c for c in todo if bool(is_edge(c)) == wide]
        if not sel:
            continue
        polys = cut_wide if wide else cut
        cols = max(len(c["n"][0]) for c in sel)
        pts = [np.asarray([list(p) + [0.0] * (cols - len(p)) for p in c["n"]], np.float64) for c in sel]
        off = np.r_[0, np.cumsum([len(p) for p in pts])].astype(np.int64).tolist()
        poff = np.r_[0, np.cumsum([len(p) for p in polys])].astype(np.int64).tolist()
        P, O, L, S = fast._ef.cut_lines(np.ascontiguousarray(np.concatenate(pts)), off,
                                        np.ascontiguousarray(np.concatenate(polys), np.float64), poff, 0.05)
        P = np.asarray(P).reshape(-1, cols)
        whole = {}
        for k in range(len(L)):
            whole.setdefault(int(L[k]), []).append(k)
        for li, c in enumerate(sel):
            ks = whole.get(li, [])
            if len(ks) == 1 and O[ks[0] + 1] - O[ks[0]] == len(c["n"]) and float(S[ks[0]]) == 0.0:
                out.append(c)               # (untouched)
                continue
            ncol = len(c["n"][0])
            for k in ks:
                q = P[O[k]:O[k + 1], :ncol]
                d = {**c, "n": np.round(q, 3).tolist()}
                s0 = float(S[k])
                if "dash" in c:
                    d["phase"] = float(c.get("phase", 0.0)) + s0
                if c.get("s0") is not None and c.get("s1") is not None:
                    seg = math.fsum(dnp.hypot(*np.diff(q[:, :2], axis=0).T).tolist())     # (exact: no reduction order)
                    d["s0"] = round(float(c["s0"]) + s0, 3)
                    d["s1"] = round(float(d["s0"]) + float(seg), 3)
                out.append(d)
    return out


def _inside_any(q, polys):
    for p in polys:
        x, y = p[:, 0], p[:, 1]
        xj, yj = np.roll(x, 1), np.roll(y, 1)
        cross = (y > q[1]) != (yj > q[1])
        with np.errstate(divide="ignore", invalid="ignore"):
            xs = x + (q[1] - y) * (xj - x) / (yj - y)
        if int(np.count_nonzero(cross & (q[0] < xs))) % 2:
            return True
    return False


def apply(frame, box, chunks, cell_fn, urban_fn=None, widths=None, left_hand=False, country=None, lon=None,
          surface_v2=False, log=None, build_memo=None, terrain_pavement=False, signal_fn=None):
    """BNE_ROADSURF_V3=surface in a road tile (roads.build_tile, before the dashes are cut): chunks with the v3 ground
    surface added (its mesh as "tri" chunks, its markings and overlays as chunks) and the tile's own lines and overlays
    inside its pieces cut out. box: the tile's own box. Never raises: on a fault the chunks as they were (logged)."""
    try:
        got = tile_surface(frame, box, cell_fn, urban_fn, widths, left_hand, country, build_memo=build_memo)
        if got is not None:
            links = _signalled_link_owners(frame, got, signal_fn) if terrain_pavement and signal_fn else ()
            draw = [c for c in chunks if "_v3_fallback" not in c]
            return cut_chunks(draw, got["cut"], got["cut_wide"], _is_edge) + surface_chunks(
                got, box, country, lon, surface_v2, terrain_pavement, links)
    except Exception as ex:
        import revisit
        revisit.fault()
        if log is not None:
            log.warning("road surface v3: %s: %s", type(ex).__name__, ex)
    for c in chunks:
        c.pop("_v3_fallback", None)
    return chunks


def _is_edge(c):
    import roads as roadsmod
    return c.get("m") in (roadsmod.EDGE_DECAL["m"], roadsmod.GRASS_EDGE_DECAL["m"], "bne_edge_gravel") or \
        c.get("layer") == "edge"


def surface_chunks(got, box, country=None, lon=None, surface_v2=False, terrain_pavement=False, signalled_links=()):
    """The road mesh chunks of tile_surface's result: one "tri" chunk per owner material (draped, as the ground
    roads), the markings (dashed ones with their phase from the piece's station 0), and with surface_v2 the variation
    overlay on the pieces' lanes and the edge blends on their outer edges."""
    import markings
    import roadmarkings
    import roads as roadsmod
    mesh = got["mesh"]
    out = []
    v, uv, tri, owner, kind = mesh["v"], mesh["uv"], mesh["tri"], mesh["owner"], mesh["kind"]
    pieces, roads_ = got["pieces"], got["roads"]

    def spec_of(o):
        return pieces[o][1] if o >= 0 else roads_[-1 - o][1]
    if len(tri):
        groups = {}
        for o in np.unique(owner).tolist():
            look = roadsmod.look_of(spec_of(int(o)))
            groups.setdefault((look["m"], float(look["tl"]), int(look["p"])), []).append(int(o))
        for (mat, tl, p), os_ in sorted(groups.items()):
            sel = np.isin(owner, os_)
            # The terrain painter consumes this same urban pavement footprint. A second
            # opaque, independently draped skin can cover the paint at a crowned junction.
            base = os_
            if terrain_pavement:
                base = [o for o in os_ if o not in signalled_links and not _painted_street_surface(spec_of(o))]
            if base:
                base_sel = sel if len(base) == len(os_) else np.isin(owner, base)
                out.append(_tri_chunk(v, uv, tri[base_sel], mat, tl, p, "base"))
            if surface_v2:
                lanes = sel & (owner >= 0) & np.isin(kind, (1, 3, 4))
                if lanes.any():
                    out.append(_tri_chunk(v, uv, tri[lanes], VAR_LOOK["m"], VAR_LOOK["tl"], VAR_LOOK["p"],
                                          VAR_LOOK["layer"]))
    st = markings.style(country, lon)
    memo = {}

    def looks_of(k):
        # the line looks of piece k: the country's style, a US road's state profile (roadmarkings.for_road)
        if k in memo:
            return memo[k]
        sp = pieces[k][1]
        prof = roadmarkings.for_road(sp, country, lon or 0.0)
        cc = str(country or "").upper()
        exit_dash = (prof.ramp[:2] if prof is not None and prof.ramp else
                     roadmarkings.DEFAULT.ramp[:2] if prof is not None or cc == "US" else
                     EXIT_DASH.get(cc, EXIT_DASH_DEFAULT))
        lane_dash = prof.travel[:2] if prof is not None else st.lane_motorway
        el = {"m": "line_white", "tl": roadsmod.EDGE_LINE["tl"], "p": 2}
        memo[k] = {
            "edge": (el, st.edge_motorway),
            "edge_left": ({**el, "m": st.left_edge or "line_white"}, st.edge_motorway),
            "lane": ({"m": "line_white", "tl": 12.0, "p": 6, "dash": tuple(lane_dash)}, st.w_motorway),
            "exit": ({"m": "line_white", "tl": 12.0, "p": 6, "dash": tuple(exit_dash)}, EXIT_W),
            "channel": (el, CHANNEL_W),
            "stripe": (el, STRIPE_W),
        }
        return memo[k]
    x0, y0, x1, y1 = box
    o = roadsmod.OVERLAP
    for name, xy, phase, piece_k in got["lines"]:
        look, w = looks_of(piece_k)[name]
        line = np.c_[xy, np.zeros(len(xy))]
        if "dash" in look:
            look = {**look, "_source": line, "_phase0": phase}
        for piece in roadsmod.clip_polyline(line, x0 - o, y0 - o, x1 + o, y1 + o):
            for c in roadsmod.split(roadsmod.densify(roadsmod.simplify(piece))):
                if len(c) >= 2:
                    out.append(roadsmod._chunk(look, w, c))
    if surface_v2:          # the edge blends on the pieces' outer edges (extras' offset: the decal's u = 1 on the asphalt)
        dw = roadsmod.EDGE_DECAL["w"]
        for k, ol in zip(got["kept"], got["outlines"]):
            nr = len(ol) // 2
            sp = pieces[k][1]
            look = roadsmod.GRAVEL_EDGE_DECAL if (roadsmod.GRAVEL_EDGE_DECAL is not None and not sp.urban
                                                  and sp.cls in roadsmod.GRAVEL_EDGE_ROADS) else roadsmod.EDGE_DECAL
            off = (roadsmod.EDGE_DECAL_AT - 0.5) * dw
            for side, edge in ((1, ol[:nr]), (-1, ol[nr:][::-1])):
                e3 = np.c_[edge, np.zeros(len(edge))]
                line = roadsmod.offset_polyline(e3, side * off)
                line = line if side > 0 else line[::-1].copy()
                for piece in roadsmod.clip_polyline(line, x0 - o, y0 - o, x1 + o, y1 + o):
                    for c in roadsmod.split(roadsmod.densify(roadsmod.simplify(piece))):
                        if len(c) >= 2:
                            out.append(roadsmod._chunk({**look, "side": side}, dw, c))
    return out


def _tri_chunk(v, uv, tri, mat, tl, p, layer):
    """A "tri" chunk (roadmesh.build: draped triangles): its own vertices, uv (u across 0..1, v m along / tl)."""
    used = np.unique(tri)
    remap = np.full(len(v), -1, np.int64)
    remap[used] = np.arange(len(used))
    return {"m": mat, "tl": tl, "p": p, "w": 0.0, "n": [], "layer": layer, "wear": 1, "tri": True,
            "xy": np.asarray(v[used], np.float64), "T": remap[tri],
            "uv": np.c_[uv[used, 0], uv[used, 1] / float(tl)]}
