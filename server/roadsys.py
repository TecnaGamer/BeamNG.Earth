"""BNE_ROADSYS: the road surface + bridge rewrite (bridgerewrite/DESIGN.md, approved 2026-11-21), its kernels in Rust
(native/roadsys, through earthfast's roadsys_* functions). This module is thin glue: it reads the map cells a tile's
product declares and hands them over.

  off (the default, or the variable absent): nothing here runs; the server is byte-identical to before.
  shadow (M1): every road geotile the server builds is built again as the new product in one background thread, off
      the delivery path: its read list (earthfast.roadsys_reads, asked again until no cell is missing: a function of
      the map data alone, never of the visit order; M2: its own lane graph only), then the product
      (earthfast.roadsys_build: the lane graph, the profiles from the profile squares round it (M2: square(), each
      from its own window's cells), the planar partition, markings, the tile's canonical bytes, checks and keys). One log line per tile and <cache>/roadsys/shadow-<format>/<frame>/<tile>.json; nothing the
      player sees reads them (revisit.UNKEYED_VALUES: no key holds the switch at off or shadow).

  heights (M2, M3): today's deck builder and grading take the one profile per carriageway, solved per profile square
      (native/roadsys square.rs: a fixed 2 km grid of the region frame; each square from the map cells of its own
      window, the square + 1.1 km, and the ground there alone; the squares blended by a smoothstep partition of
      unity): every whole bridge chain the road tiles' stores solve (roads.BridgeProfiles._chain_solve) takes its
      decks' heights per vertex from the blend on each deck's own map feature (chain_heights; in place of
      deck_profile + _tie_beside, as BNE_ROADSHAPE=profile's P3b), and grading's second stage takes the squares' terrain
      targets over its first (pins_near, grading.PROFILE_PINS through blockworker._profile_pins: by square, no chain
      asked, so a point's target is the same whichever query asks). A deck vertex on no structure of its feature, or a
      square read short: today's heights, the build not kept. Squares are memoised when read complete and kept in the
      revisit cache (kind "rssq", DISK). It changes what is built: a build switch (revisit.keyed_env, UNKEYED_VALUES;
      the plans' format suffix, plancache.plans_format).
      M3: the floors take today's headroom and each structure's own drawn depth (_deck_depth; the deck builder draws
      that depth, chain_heights' "depth"); a chain is brought onto its abutments' approach fills, lifted or dropped,
      a drop held back by the floors the part holds (native/roadsys endmatch.rs); the terrain targets change the plans'
      ground only where a floor digs the road under a structure (pins_near), fading back to the plans' own.

Frames: every product is built in the ENU frame of its region (the z8 Web-Mercator tile holding the geotile's
centre; ~120-160 km), so a component built for two neighbouring tiles is built in one frame, from one read list.
The frame's identity is in every key."""
import json
import logging
import os
import queue
import threading
import time

import numpy as np

log = logging.getLogger("earthd.roadsys")

MODE = os.environ.get("BNE_ROADSYS", "off").strip().lower()
SHADOW = MODE == "shadow"
HEIGHTS = MODE == "heights"
FORMAT = 2                  # the shadow records' JSON (their folder name; 2: M2's profiles, whole components read)
HEIGHTS_KERNEL = 602        # earthfast.roadsys_heights_reads / roadsys_heights

FRAME_Z = 8                 # the region frames' Web-Mercator zoom
CUT = (4000.0, 2000.0)       # strokes cut at a joint past every line of this grid (m, offset): net.rs Opts
MAX_ROUNDS = 16             # read-list rounds before a tile's read counts as short (provisional)
MAX_CELLS = 160             # map cells a tile's read may reach (beyond: provisional, recorded)
QUEUE_MAX = 64              # tiles waiting for the shadow thread (beyond: skipped, counted)
DEM_PIXEL_M = 2.0           # the profiles' ground: the DEM zoom of a 2 m pixel (PT3's exporter)


def region(lat, lon):
    """(frame id, WorldFrame) of the region holding (lat, lon)."""
    import geotiles
    import mapcells
    k = mapcells.cell_of(lat, lon, FRAME_Z)
    la, lo = k.center()
    return (FRAME_Z << 48) | (k.x << 24) | k.y, geotiles.WorldFrame(la, lo)


# ------------------------------------------------------------------------------------------ the cells' segments

class _Cells:
    """Each map cell's lane rows as the v3 lane graph reads them (lanegraph._cell_lane_segments: lane counts, widths,
    turn lanes, placement, regions exactly as the server's), with their bridge / tunnel / level ranges and spec
    widths, as lon / lat columns. Memoised per cell and road revision (a pure function of the cell's layer)."""

    def __init__(self, md=None, cell_fn=None):
        self.md = md
        self._fn = cell_fn
        self.memo = {}
        self.rows = {}
        self.lock = threading.Lock()

    def cell_fn(self, key):
        if self._fn is not None:
            return self._fn(key)
        return self.md.cell(key, layers=("roads",), kind="demand").layers.get("roads")

    def table(self, key):
        import lanegraph
        feats = self.cell_fn(key)
        stamp = (str(key), getattr(feats, "_road_revision", 0) if feats is not None else None)
        with self.lock:
            got = self.rows.get(str(key))
            if got is not None and got[0] == stamp:
                return got[1]
        tab = None
        if feats is not None and len(feats):
            rows, _idx = lanegraph._cell_lane_segments(key, self.cell_fn, self.memo)
            tab = _table(str(key), feats, rows)
        with self.lock:
            self.rows[str(key)] = (stamp, tab)
            while len(self.rows) > 512:
                self.rows.pop(next(iter(self.rows)))
        return tab


def _ranges(rule, i, vals=False):
    if rule is None:
        return []
    sel = np.nonzero(rule.feature == i)[0]
    return [(float(rule.start[j]), float(rule.end[j]), rule.value[j] if vals else str(rule.value[j])) for j in sel]


def _table(cell, feats, rows):
    """One cell's segment columns (lon / lat points), PT3's exporter's fields; the structure and tunnel ranges the
    deck builder's (roads.level_ranges), the levels the map's."""
    import countrybounds
    import roads
    ground = roads.road_specs(feats)
    bridges = (roads.bridge_specs(feats) if "_bridge_specs" in feats.__dict__ else
               roads._bridge_specs(feats, ground_fallback=ground))
    by_fid = {}
    for i, fid in enumerate(feats.ids.tolist()):
        by_fid.setdefault(str(fid), []).append(i)
    keys = sorted(rows)
    lls = [np.asarray(rows[k]["ll"], np.float64) for k in keys]
    codes = countrybounds.bundled().codes_at([(ll[0, 1], ll[0, 0]) for ll in lls]) if keys else []
    out = dict(ids=[], texts=[], nums=[], ints=[], ll=[], cons=[], rng=[], cell=cell)
    for k, ll, code in zip(keys, lls, codes):
        r = rows[k]
        fid = k.split("|", 1)[0]
        idx = None
        for i in by_fid.get(fid, []):
            a = feats.coord_off[feats.ring_off[feats.part_off[i]]]
            if np.allclose(feats.lonlat[a], ll[0], atol=1e-9, rtol=0):
                idx = i
                break
        sp = None if idx is None else (ground[idx] or bridges[idx])
        if sp is not None:
            drive, inferred = roads.lane_layout(sp)
            spec = (float(sp.width), float(drive), int(inferred), str(sp.cls), 1 if getattr(sp, "urban", False) else 0)
        else:
            spec = (0.0, 0.0, 0, "", 0)
        out["ids"].append(k)
        out["texts"].append((str(code or ""), spec[3], str(r["turn"] or ""), str(r["turn_bw"] or ""), str(r["place"] or "")))
        out["nums"].append((spec[0], spec[1], float(r["kmh"]), float(r["lane_w"])))
        out["ints"].append((spec[2], spec[4], int(r["cls"]), 1 if r["link"] else 0, int(r["dir"]), int(r["lanes"]),
                            int(r["lfw"]), int(r["lbw"]), int(r["region"])))
        out["ll"].append(ll[:, :2])
        out["cons"].append([(str(c), int(v)) for c, v in r["cons"]])
        rg = []
        if idx is not None:
            # (the off-ground ranges the deck builder takes, roads.level_ranges: the flags, the level rule, inferred
            # grade separations, ranges lengthened over the roads under them; one support label for both)
            depth = 0.0
            for a, b, v in roads.level_ranges(feats, idx):
                if v in ("bridge", "tunnel"):
                    rg.append((float(a), float(b), 0.0 if v == "bridge" else 1.0, 0.0))
                if v == "bridge":
                    depth = max(depth, _deck_depth(ll[:, :2], float(a), float(b)))
            if depth > 0.0:     # (M3: the drawn depth of its decks, the floors' and the deck builder's: _deck_depth)
                rg.append((0.0, 1.0, 3.0, round(depth, 4)))
            for a, b, v in _ranges(feats.rules.get("level"), idx, vals=True):
                rg.append((a, b, 2.0, float(v)))
        out["rng"].append(rg)
    return out


def _deck_depth(ll, a, b):
    """m of slab and girders the deck builder draws under the bridge range [a, b] (fractions of its length) of a
    segment (lon / lat points), by roads.BridgeProfiles._depth's rule on that part alone: the inventory's main span where
    a record matches, else its geometry's (roads.geom_span), roads.deck_depth of it; 0 with the legacy profile mode
    (the full depth). The floors take it and the deck builder draws it (chain_heights' depth) (M3)."""
    import geotiles
    import roads
    if roads.bridge_profile_mode() != "curve" or len(ll) < 2:
        return 0.0
    fr = geotiles.WorldFrame(float(ll[0, 1]), float(ll[0, 0]))
    e, n, _u = fr.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.0)
    xy = np.column_stack((e, n))
    import dnp
    d = np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(xy, axis=0).T))]
    if d[-1] <= 0:
        return 0.0
    s0, s1 = a * d[-1], b * d[-1]
    keep = (d > s0) & (d < s1)
    part = np.vstack([[dnp.interp(s0, d, xy[:, 0]), dnp.interp(s0, d, xy[:, 1])], xy[keep],
                      [dnp.interp(s1, d, xy[:, 0]), dnp.interp(s1, d, xy[:, 1])]])
    span = 0.0
    try:
        import bridgetypes
        got = bridgetypes.matches(fr, [part])
        if got:
            span = float(bridgetypes.record(got[0][1])["span"])
    except Exception as ex:
        log.debug("roadsys deck span: %s: %s", type(ex).__name__, ex)
    return float(roads.deck_depth(span if span > 0 else roads.geom_span([part])))


def _segments(tables, frame, style):
    """earthfast.RoadsysSegments of the tables' segments in frame (every copy: the kernel keeps one)."""
    import fast
    ids, cells, texts, nums, ints, lls, off = [], [], [], [], [], [], [0]
    con_seg, con_id, con_v, rng_seg, rng = [], [], [], [], []
    for tab in tables:
        if tab is None:
            continue
        for j, k in enumerate(tab["ids"]):
            g = len(ids)
            ids.append(k)
            cells.append(tab["cell"])
            texts.append(tab["texts"][j])
            nums.append(tab["nums"][j])
            ints.append(tab["ints"][j])
            lls.append(tab["ll"][j])
            off.append(off[-1] + len(tab["ll"][j]))
            for c, v in tab["cons"][j]:
                con_seg.append(g)
                con_id.append(c)
                con_v.append(v)
            for x in tab["rng"][j]:
                rng_seg.append(g)
                rng.append(x)
    ll = np.concatenate(lls) if lls else np.zeros((0, 2))
    e, n, _u = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.0)
    pts = np.ascontiguousarray(np.column_stack((e, n)), np.float64)
    return fast._ef.RoadsysSegments(
        ids, cells, texts, np.asarray(nums, np.float64).reshape(-1, 4), np.asarray(ints, np.int64).reshape(-1, 9), pts,
        np.asarray(off, np.int64), np.asarray(con_seg, np.int64), con_id, np.asarray(con_v, np.int64),
        np.asarray(rng_seg, np.int64), np.asarray(rng, np.float64).reshape(-1, 4), style)


def _cells_of_boxes(frame, boxes):
    import mapcells
    out = set()
    for x0, y0, x1, y1 in boxes:
        la, lo, _ = frame.enu_to_geodetic(np.array([x0, x1, x1, x0]), np.array([y0, y0, y1, y1]))
        out.update(mapcells.cells_for_bbox(float(la.min()), float(lo.min()), float(la.max()), float(lo.max())))
    return out


def _copy_cells(tables, fids):
    """The cells every copy of these segments may lie in (each copy's own extent), as roadshape.SegmentWindow
    discovers them: the kernel chooses among all of them."""
    import mapcells
    want = {f.split("|", 1)[0] for f in fids}     # (a copy's row id ends in its own first point: by segment)
    out = set()
    for tab in tables:
        if tab is None:
            continue
        for k, ll in zip(tab["ids"], tab["ll"]):
            if k.split("|", 1)[0] in want:
                out.update(mapcells.cells_for_bbox(float(ll[:, 1].min()), float(ll[:, 0].min()),
                                                   float(ll[:, 1].max()), float(ll[:, 0].max())))
    return out


# ----------------------------------------------------------------------------------------------------- a tile

def build_tile(gx, gy, md, dem, cells_cache=None, country=None, dump=None):
    """Geotile (gx, gy)'s shadow product: (record dict, canonical bytes). Reads through md (MapData) and dem (Dem)."""
    import fast
    import geotiles
    import mapcells
    import markings
    fast.require(HEIGHTS_KERNEL, "roadsys_build")
    t0 = time.perf_counter()
    lat, lon = geotiles.centre(gx, gy)
    fid, frame = region(lat, lon)
    quad = np.asarray(geotiles.quad(frame, gx, gy), np.float64)[:, :2]
    box = [float(quad[:, 0].min()), float(quad[:, 1].min()), float(quad[:, 0].max()), float(quad[:, 1].max())]
    st = markings.style(country, lon)
    style = (st.centre, st.left_edge or "", float(st.w), float(st.w_motorway), float(st.edge), float(st.edge_motorway),
             tuple(map(float, st.centre_urban)), tuple(map(float, st.lane_urban)), tuple(map(float, st.lane_motorway)),
             bool(st.double_centre_urban))
    cc = cells_cache or _Cells(md)
    cells = _cells_of_boxes(frame, [[box[0] - 60, box[1] - 60, box[2] + 60, box[3] + 60]])
    rounds, complete, segs, reads_cpu = 0, False, None, 0.0
    t_read = 0.0
    while rounds < MAX_ROUNDS:
        rounds += 1
        tr = time.perf_counter()
        tables = [cc.table(k) for k in sorted(cells, key=str)]
        segs = _segments(tables, frame, style)
        t_read += time.perf_counter() - tr
        boxes, fids, counts, cpu = fast._ef.roadsys_reads(segs, box, CUT, True)
        reads_cpu += cpu
        need = _cells_of_boxes(frame, boxes) | _copy_cells(tables, fids)
        missing = need - cells
        if not missing:
            complete = True
            break
        if len(cells | need) > MAX_CELLS:
            cells |= set(sorted(missing, key=str)[:max(0, MAX_CELLS - len(cells))])
            break
        cells |= need
    zoom = None
    try:
        import dem as demmod
        zoom = demmod.zoom_for(lat, DEM_PIXEL_M)
    except Exception:       # (a test's stand-in)
        zoom = 16

    def ground(e, n):
        la, lo, _u = frame.enu_to_geodetic(np.asarray(e, np.float64), np.asarray(n, np.float64))
        return np.asarray(dem.sample(np.asarray(la), np.asarray(lo), zoom), np.float64)

    def ground_ll(la, lo):
        return np.asarray(dem.sample(np.asarray(la, np.float64), np.asarray(lo, np.float64), zoom), np.float64)
    ground_ll.tag = f"dem{zoom}"
    # (M2: the profiles from the profile squares round the tile's read, no vertical component read; their own cells)
    ts = time.perf_counter()
    s0 = dict(STATS)
    rbox = [box[0] - FIELD_REACH_M, box[1] - FIELD_REACH_M, box[2] + FIELD_REACH_M, box[3] + FIELD_REACH_M]
    sqs = _squares_for(fid, frame, rbox, cc, ground_ll)
    if any(q is None for q in sqs):
        raise RuntimeError("a profile square not at hand")
    import hashlib
    fkey = int(hashlib.sha256(repr(sorted((q.info()[2], q.info()[1]) for q in sqs)).encode()).hexdigest()[:15], 16)

    def z_of(pts, tans, feats):
        return fast._ef.roadsys_blend(sqs, np.ascontiguousarray(pts, np.float64), np.ascontiguousarray(tans, np.float64),
                                      list(feats), False)
    t_sq = time.perf_counter() - ts

    tb = time.perf_counter()
    text, data = fast._ef.roadsys_build(segs, [tuple(map(float, q)) for q in quad], (int(gx), int(gy)), fid, ground, CUT,
                                        dump, [list(map(float, b)) for b in boxes], (z_of, fkey))
    rec = json.loads(text)
    kept, copies, parents, sections = segs.counts()
    rec.update(tile=geotiles.key(gx, gy), frame=f"{FRAME_Z}/{(fid >> 24) & 0xffffff}/{fid & 0xffffff}",
               complete=complete, rounds=rounds, cells=len(cells),
               segments={"kept": kept, "copies": copies, "roads_for_sections": parents, "sections_for_road": sections},
               squares={"used": len(sqs), "solved": STATS["squares"] - s0["squares"],
                        "cells": STATS["square_cells"] - s0["square_cells"],
                        "cells_max": STATS["square_cells_max"], "cpu_ms": round(STATS["square_cpu_ms"] - s0["square_cpu_ms"], 1),
                        "wall_ms": round(t_sq * 1e3, 1)},
               wall_ms={"read": round(t_read * 1e3, 1), "reads_cpu": round(reads_cpu * 1e3, 1),
                        "build": round((time.perf_counter() - tb) * 1e3, 1),
                        "total": round((time.perf_counter() - t0) * 1e3, 1)})
    return rec, data


# ----------------------------------------------------------------------------------------- the shadow thread

_q = None
_skipped = 0
_lock = threading.Lock()


def _out_dir(cache_dir, rec):
    return os.path.join(cache_dir, "roadsys", f"shadow-{FORMAT}", rec["frame"].replace("/", "_"))


def _worker(md, dem, cache_dir):
    cc = _Cells(md)
    while True:
        item = _q.get()
        if item is None:
            return
        gx, gy, country = item
        try:
            rec, data = build_tile(gx, gy, md, dem, cc, country)
            d = _out_dir(cache_dir, rec)
            os.makedirs(d, exist_ok=True)
            tmp = os.path.join(d, f".{rec['tile']}.{os.getpid()}.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(rec, f, sort_keys=True)
            os.replace(tmp, os.path.join(d, rec["tile"] + ".json"))
            c, p = rec["checks"], rec["profile"]
            log.info("roadsys shadow %s: %.0f ms (read %.0f, kernels %.1f cpu), %d cells in %d rounds%s; overlap %.1f m2, "
                     "holes %d/%d, T %d, folds %d; floors %d/%d; hash %s", rec["tile"], rec["wall_ms"]["total"],
                     rec["wall_ms"]["read"], rec["cpu_ms"]["total"], rec["cells"], rec["rounds"],
                     "" if rec["complete"] else " (short)", c["overlap_m2"], c["hole_uncovered"], c["hole_samples"],
                     c["tjunctions"], c["folded"], p["floors_met"], p["floors"], rec["tile_hash"])
        except Exception as e:         # (shadow only: never in the way of the delivered tile)
            log.warning("roadsys shadow %s_%s failed: %s", gx, gy, e, exc_info=log.isEnabledFor(logging.DEBUG))


def enqueue(gx, gy, md, dem, cache_dir, country=None):
    """BNE_ROADSYS=shadow: geotile (gx, gy) to the shadow thread (started at the first call). Never blocks."""
    global _q, _skipped
    if not SHADOW or md is None or dem is None or cache_dir is None:
        return
    with _lock:
        if _q is None:
            _q = queue.Queue(QUEUE_MAX)
            threading.Thread(target=_worker, args=(md, dem, cache_dir), name="roadsys-shadow", daemon=True).start()
    try:
        _q.put_nowait((int(gx), int(gy), country))
    except queue.Full:
        _skipped += 1
        if _skipped in (1, 10, 100) or _skipped % 1000 == 0:
            log.info("roadsys shadow: %d tiles skipped (queue full)", _skipped)


# ------------------------------------------------------------------------------ M2: profile squares, heights, targets

GROUND = None               # (lat, lon) -> geodetic heights: the profiles' ground (blockworker: the plans' own DEM)
DISK = None                 # (load(name) -> bytes or None, save(name, bytes)): squares kept between processes
DUMP_DIR = os.environ.get("ROADSYS_SQUARE_DUMP") or None   # (tools only: each square's whole input written there)
DISK_FN = None              # () -> DISK or None, asked once a process has its cache (blockworker._square_disk)
STATS = {"chains": 0, "solved": 0, "unmatched": 0, "short": 0, "error": 0, "squares": 0, "square_memo": 0,
         "square_disk": 0, "square_cells": 0, "square_cells_max": 0, "square_read_s": 0.0, "square_kernel_s": 0.0,
         "square_cpu_ms": 0.0, "pins_squares": 0, "obs_runs": 0, "obs_measured": 0, "total_s": 0.0}
SQ_MEMO_MAX = 96            # squares kept per process (by region, square and their cells' road revisions)
PIN_MEMO_MAX = 48
_squares = {}
_pins = {}
_memo_lock = threading.Lock()
_sq_locks = {}
_cells_of = {}              # (one _Cells per cell function: a store's roads layer reads)
# (markings play no part in the heights: any style)
_STYLE = ("white", "", 0.12, 0.15, 0.15, 0.2, (3.0, 9.0), (3.0, 9.0), (3.0, 9.0), False)
SQUARE_M = 2000.0           # native/roadsys square.rs SQUARE_M
FIELD_REACH_M = 300.0       # m round a shadow tile its knots take the squares' heights (product.rs FIELD_REACH_M)
CLIP_PAD_M = 5.0            # m past a query's reach its targets' lines are kept (pins_near)


def _cells_for(cell_fn):
    if isinstance(cell_fn, _Cells):         # (the shadow thread's own tables)
        return cell_fn
    with _memo_lock:
        got = _cells_of.get(id(cell_fn))
        if got is None or got[0] is not cell_fn:
            got = _cells_of[id(cell_fn)] = (cell_fn, _Cells(cell_fn=cell_fn))
            while len(_cells_of) > 8:
                _cells_of.pop(next(iter(_cells_of)))
        return got[1]


def _obs(frame):
    """The measured deck heights the deck builder reads (roads.BridgeProfiles._observed: roadz.PROVIDER's lidar, BD
    TOPO, NVDB) as the profile squares' obs callback, or None without a provider."""
    import roadz
    prov = roadz.PROVIDER
    if prov is None or not getattr(prov, "enabled", False):
        return None

    def obs(runs):
        out = []
        for pts, half, level in runs:
            z = None
            try:
                z = prov.observed(frame, np.asarray(pts, np.float64), float(half), max(1, int(round(level))), "")
            except Exception as e:          # (the inference rather than no deck, as the deck builder does)
                log.debug("roadsys observed: %s: %s", type(e).__name__, e)
            out.append(np.full(len(pts), np.nan) if z is None else np.asarray(z, np.float64).reshape(-1))
            with _memo_lock:
                STATS["obs_runs"] += 1
                STATS["obs_measured"] += int(z is not None and np.isfinite(out[-1]).any())
        return out
    return obs


def _g(ground_ll, frame):
    def ground(e, n):
        la, lo, _ = frame.enu_to_geodetic(np.asarray(e, np.float64), np.asarray(n, np.float64), 0.0)
        return np.asarray(ground_ll(np.asarray(la, np.float64), np.asarray(lo, np.float64)), np.float64)
    return ground


def square(fid, frame, sq, cell_fn, ground_ll=None):
    """Square sq of region fid (frame): earthfast.RoadsysSquare, solved from the map cells covering its window (every
    one read through cell_fn: a short read faults the caller's revisit scope), memoised per process by those cells'
    road revisions, kept on disk (DISK) when read complete; None when its ground could not be read (the caller's
    scope faulted: what it builds without the square is not kept)."""
    import fast
    import revisit
    fast.require(HEIGHTS_KERNEL, "roadsys_square")
    sq = (int(sq[0]), int(sq[1]))
    cc = _cells_for(cell_fn)
    tr = time.perf_counter()
    win = fast._ef.roadsys_square_window(sq)
    cells = sorted(_cells_of_boxes(frame, [win]), key=str)
    with revisit.scope() as b:
        tables = [cc.table(k) for k in cells]
    if not b.complete:
        revisit.fault(here=True)        # (the caller's scope too: what is made from this square is not kept)
    stamps = tuple(cc.rows.get(str(k), (None,))[0] for k in cells)
    gfn = ground_ll or GROUND
    mk = (fid, sq, stamps, getattr(gfn, "tag", getattr(gfn, "__name__", "")))
    with _memo_lock:
        got = _squares.get(mk)
        lk = _sq_locks.setdefault((fid, sq), threading.Lock())
    if got is not None:
        with _memo_lock:
            STATS["square_memo"] += 1
        return got
    with lk:                            # (one solve per square at a time: a second asker waits for the first's)
        with _memo_lock:
            got = _squares.get(mk)
        if got is not None:
            return got
        name = None
        disk = DISK
        if disk is None and DISK_FN is not None:
            disk = DISK_FN()
        if disk is not None and b.complete:
            import hashlib
            name = "sq%d.%d_%d_%d_%s" % (FORMAT, int(getattr(fast._ef, "VERSION", 0)), sq[0], sq[1],
                                          hashlib.sha256(repr((fid, stamps, mk[3])).encode()).hexdigest()[:24])
            try:
                data = disk[0](name)
                if data is not None:
                    got = fast._ef.RoadsysSquare.from_bytes(data)
                    with _memo_lock:
                        STATS["square_disk"] += 1
            except Exception as e:      # (a bad file: solved again)
                log.debug("roadsys square %s: %s", name, e)
                got = None
        t_read = time.perf_counter() - tr
        if got is None:
            segs = _segments(tables, frame, _STYLE)
            tk = time.perf_counter()
            dump = None
            if DUMP_DIR:
                os.makedirs(DUMP_DIR, exist_ok=True)
                dump = os.path.join(DUMP_DIR, "%d_%d.rssq" % sq)
            try:
                with revisit.scope() as sb:     # (what it reads: its ground, the measured decks)
                    got = fast._ef.roadsys_square(segs, sq, _g(gfn, frame), CUT, dump, _obs(frame))
            except Exception as e:      # (its ground not at hand, a DEM tile missing: no square, the asker not kept)
                revisit.fault(here=True)
                with _memo_lock:
                    STATS["short"] += 1
                log.debug("roadsys square %s %s: %s: %s", fid, sq, type(e).__name__, e)
                return None
            if not (b.complete and sb.complete):
                # (read short: used by this asker alone, which is not kept; never memoised nor kept on disk, so no later
                # build takes it as complete)
                revisit.fault(here=True)
                with _memo_lock:
                    STATS["short"] += 1
                return got
            rec = json.loads(got.info()[0])
            with _memo_lock:
                STATS["squares"] += 1
                STATS["square_cells"] += len(cells)
                STATS["square_cells_max"] = max(STATS["square_cells_max"], len(cells))
                STATS["square_read_s"] += t_read
                STATS["square_kernel_s"] += time.perf_counter() - tk
                inf = got.info()
                STATS["square_cpu_ms"] += (json.loads(inf[3]) if len(inf) > 3 and inf[3] else
                                           json.loads(inf[0]).get("cpu_ms", {})).get("total", 0.0)
            log.debug("roadsys square %s %s: %d cells, read %.2f s: %s", fid, sq, len(cells), t_read, got.info()[0])
            if name is not None:
                try:
                    disk[1](name, got.to_bytes())
                except Exception as e:
                    log.debug("roadsys square %s: not kept: %s", name, e)
        with _memo_lock:
            _squares[mk] = got
            while len(_squares) > SQ_MEMO_MAX:
                _squares.pop(next(iter(_squares)))
        return got


def _squares_for(fid, frame, box, cell_fn, ground_ll):
    import fast
    return [square(fid, frame, sq, cell_fn, ground_ll) for sq in fast._ef.roadsys_squares_of_box(list(map(float, box)))]


def _tangents(xy):
    n = len(xy)
    a = xy[np.maximum(np.arange(n) - 1, 0)]
    b = xy[np.minimum(np.arange(n) + 1, n - 1)]
    d = b - a
    ln = np.sqrt(d[:, 0] * d[:, 0] + d[:, 1] * d[:, 1])
    out = np.zeros_like(d)
    ok = ln > 1e-9
    out[ok] = d[ok] / ln[ok, None]
    return np.ascontiguousarray(out, np.float64)


def feature(fid):
    """A map feature's id as the deck builder and the lane graph share it (a lane section's suffix left off)."""
    return str(fid).split("|", 1)[0].split("~lanes", 1)[0]


HOLD_M = 3.0                # m a floor's point may lie off a deck part's line to hold it (chain_heights)


def _stations_near(line, pts, reach):
    """Arc stations along line (n, 2) of the points within reach of it (their feet), sorted."""
    if not pts or len(line) < 2:
        return []
    a, b = line[:-1], line[1:]
    ab = b - a
    l2 = ab[:, 0] * ab[:, 0] + ab[:, 1] * ab[:, 1]
    seg = np.sqrt(l2)
    s0 = np.r_[0.0, np.cumsum(seg)][:-1]
    out = []
    for x, y in pts:
        t = np.where(l2 > 1e-12, ((x - a[:, 0]) * ab[:, 0] + (y - a[:, 1]) * ab[:, 1]) / np.where(l2 > 1e-12, l2, 1.0), 0.0)
        t = np.clip(t, 0.0, 1.0)
        fx, fy = a[:, 0] + t * ab[:, 0] - x, a[:, 1] + t * ab[:, 1] - y
        d = np.sqrt(fx * fx + fy * fy)
        j = int(np.argmin(d))
        if float(d[j]) <= reach:
            out.append(float(s0[j] + t[j] * seg[j]))
    return sorted(out)


END_PAST_M = 0.5            # m past a deck's free end its approach's ground is read (chain_heights' lift)


def chain_heights(store, comp, parts, ground_ll=None):
    """BNE_ROADSYS=heights: a whole bridge chain's deck heights from the one profile per carriageway (the blended
    profile squares round its decks), as roadshape._built's record: {"deck_z": {part key: heights per vertex of its
    line, the store's frame (ENU up)}, "pins": [] (the terrain targets come by square: pins_near), "digest",
    "knots": []}, or None: a deck vertex on no structure of its own map feature (today's heights for the whole chain,
    counted). parts: {key (fid, j): (spec, xy, z, lon)} as published (xy in store.frame)."""
    import fast
    fast.require(HEIGHTS_KERNEL, "roadsys_blend")
    t0 = time.perf_counter()
    parts_in = parts
    keys, seen = [], set()
    for k in sorted(comp, key=str):     # (one deck kept twice under two part keys: once, the first key's)
        v = parts.get(k)
        if v is None or len(v[1]) < 2:
            continue
        xy = np.asarray(v[1], np.float64)[:, :2]
        sig, rsig = np.round(xy, 2).tobytes(), np.round(xy[::-1], 2).tobytes()
        if sig in seen or rsig in seen:
            continue
        seen.add(sig)
        keys.append(k)
    with _memo_lock:
        STATS["chains"] += 1
    if not keys:
        return None
    sf = store.frame
    lls = []
    for k in keys:
        xy = np.asarray(parts[k][1], np.float64)[:, :2]
        la, lo, _ = sf.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
        lls.append((np.asarray(la, np.float64), np.asarray(lo, np.float64)))
    fid, frame = region(float(lls[0][0][0]), float(lls[0][1][0]))
    decks = []
    for la, lo in lls:
        e, n, _u = frame.geodetic_to_enu(la, lo, 0.0)
        decks.append(np.ascontiguousarray(np.column_stack((e, n)), np.float64))
    allp = np.concatenate(decks)
    box = [float(allp[:, 0].min()), float(allp[:, 1].min()), float(allp[:, 0].max()), float(allp[:, 1].max())]
    sqs = _squares_for(fid, frame, box, store.cell_fn, ground_ll)
    if any(q is None for q in sqs):
        return None                     # (a square not at hand: today's heights, the build not kept)
    out = {}
    depth = {}
    bad = 0
    for k, (la, lo), d in zip(keys, lls, decks):
        feat = feature(k[0] if isinstance(k, tuple) else k)
        tg = _tangents(d)
        z = np.asarray(fast._ef.roadsys_blend(sqs, d, tg, [feat] * len(d), True), np.float64)
        # (M3: the depth the floors were solved with, its structure's: the deck builder draws it)
        dp = np.asarray(fast._ef.roadsys_deck_depth(sqs, d, tg, [feat] * len(d)), np.float64)
        dp = dp[np.isfinite(dp) & (dp > 0)]
        if len(dp):
            depth[k] = float(dp.max())
        if not np.isfinite(z).all():
            bad += int((~np.isfinite(z)).sum())
            continue
        out[k] = np.asarray(sf.geodetic_to_enu(la, lo, z)[2], np.float64)
    if not bad and out:
        # (M3: the chain brought onto its abutments' ground, the plans' first stage just past each free end (their
        # approach fills), lifted or dropped; a drop reaches no further than the part's nearest floor over another road
        # (its holds), so the floors the profile met stay met; native/roadsys endmatch.rs. The terrain targets leave the
        # approaches to the plans, pins_near)
        ks = [k for k in keys if k in out]
        plist, ends_ll = [], []
        for k in ks:
            xy = np.asarray(parts_in[k][1], np.float64)[:, :2]
            plist.append((np.ascontiguousarray(xy), np.ascontiguousarray(out[k], np.float64)))
            for a, b in ((xy[0], xy[1]), (xy[-1], xy[-2])):
                d = a - b
                n = float(np.sqrt(d[0] * d[0] + d[1] * d[1]))
                ends_ll.append(a + (d / n if n > 1e-9 else 0.0) * END_PAST_M)
        ep = np.asarray(ends_ll, np.float64)
        la, lo, _ = sf.enu_to_geodetic(ep[:, 0], ep[:, 1], 0.0)
        la, lo = np.asarray(la, np.float64), np.asarray(lo, np.float64)
        g = np.asarray(store.elev(la, lo), np.float64).reshape(-1)
        gu = np.asarray(sf.geodetic_to_enu(la, lo, g)[2], np.float64)
        gu = np.where(np.isfinite(gu), gu, np.nan)
        # (the floors each part holds over other roads, its holds: a dropped end comes back to the profile by its nearest)
        fp = fast._ef.roadsys_floor_points(sqs, [box[0] - 5.0, box[1] - 5.0, box[2] + 5.0, box[3] + 5.0])
        holds = []
        for k in ks:
            d = decks[keys.index(k)]
            feat = feature(k[0] if isinstance(k, tuple) else k)
            holds.append(_stations_near(d, [(x, y) for x, y, f in fp if f == feat], HOLD_M))
        lifts = fast._ef.roadsys_end_lifts(plist, [(float(gu[2 * i]), float(gu[2 * i + 1])) for i in range(len(ks))],
                                           float("inf"), float("inf"), holds)
        for k, lf in zip(ks, lifts):
            out[k] = np.asarray(out[k], np.float64) + np.round(np.asarray(lf, np.float64), 4)
    if bad:
        with _memo_lock:
            STATS["unmatched"] += 1
        log.debug("roadsys heights: chain %s: %d deck vertices on no structure of their feature: today's heights",
                  min(comp, key=str), bad)
        return None
    kept = {}
    for k in keys:
        xy = np.asarray(parts[k][1], np.float64)[:, :2]
        kept.setdefault(np.round(xy, 2).tobytes(), (k, False))
        kept.setdefault(np.round(xy[::-1], 2).tobytes(), (k, True))
    for k in comp:          # (a deck kept twice under two part keys: the first key's heights, its way round)
        v = parts.get(k)
        if k in out or v is None or len(v[1]) < 2:
            continue
        got2 = kept.get(np.round(np.asarray(v[1], np.float64)[:, :2], 2).tobytes())
        if got2 is not None:
            z = out[got2[0]]
            out[k] = z[::-1].copy() if got2[1] else z.copy()
            if got2[0] in depth:
                depth[k] = depth[got2[0]]
    import hashlib
    h = hashlib.sha256()
    for k in sorted(out, key=str):
        h.update(str(k).encode() + np.ascontiguousarray(out[k], np.float64).tobytes())
    with _memo_lock:
        STATS["solved"] += 1
        STATS["total_s"] += time.perf_counter() - t0
    for k in sorted(depth, key=str):
        h.update(str(k).encode() + np.float64(depth[k]).tobytes())
    return {"deck_z": out, "depth": depth, "pins": [], "digest": h.hexdigest()[:16], "knots": [],
            "squares": [s.info()[2] for s in sqs]}


def _regions_of(la, lo):
    """Each point's region frame id (FRAME_Z Web-Mercator tile: mapcells.cells_of's exact edge rules)."""
    import mapcells
    ks = mapcells.cells_of(np.asarray(la, np.float64), np.asarray(lo, np.float64), FRAME_Z)
    return np.array([(FRAME_Z << 48) | (k.x << 24) | k.y for k in ks], np.int64)


def _square_pins(fid, frame, sq, cell_fn, ground_ll):
    """Square sq's terrain target lines: [(xy (n, 2) in the frame, lat (n,), lon (n,), height (n,), half width, fade
    (n,), share (n,))], heights blended over it and the neighbours its lines' bands reach, the points of its own
    region only; None when a square is not at hand (the asker's scope faulted)."""
    import revisit
    with revisit.scope() as ps:
        got = _square_pins_in(fid, frame, sq, cell_fn, ground_ll)
    if not ps.complete:
        revisit.fault(here=True)
    return got[0]


def _square_pins_in(fid, frame, sq, cell_fn, ground_ll):
    """_square_pins' work, in its own revisit scope: (lines or None, memo entry or None)."""
    import fast
    import revisit
    me = square(fid, frame, sq, cell_fn, ground_ll)
    if me is None:
        return None, None
    mk = (fid, tuple(sq))
    with _memo_lock:
        got = _pins.get(mk)
    if got is not None:
        # (valid while every square its heights blended is still the one it was: each looked up again, memoised)
        if all((square(fid, frame, q, cell_fn, ground_ll) or me).info()[1] == k if q != tuple(sq) else me.info()[1] == k
               for q, k in got[0]):
            return got[1], None
    out = []
    cache = {tuple(sq): me}
    for feat, pts, tans, w, share, half, hand, low in me.pins():
        box = [float(pts[:, 0].min()), float(pts[:, 1].min()), float(pts[:, 0].max()), float(pts[:, 1].max())]
        nb = []
        for q in fast._ef.roadsys_squares_of_box(box):
            q = tuple(q)
            if q not in cache:
                cache[q] = square(fid, frame, q, cell_fn, ground_ll)
            if cache[q] is None:
                return None, None
            nb.append(cache[q])
        z = np.asarray(fast._ef.roadsys_blend(nb, pts, tans, [feat] * len(pts), False), np.float64)
        la, lo, _ = frame.enu_to_geodetic(pts[:, 0], pts[:, 1], 0.0)
        la, lo = np.asarray(la, np.float64), np.asarray(lo, np.float64)
        ok = np.isfinite(z) & (_regions_of(la, lo) == fid)
        idx = np.flatnonzero(ok)
        if len(idx) < 2:
            continue
        for run in np.split(idx, np.flatnonzero(np.diff(idx) > 1) + 1):
            if len(run) >= 2:
                out.append((np.ascontiguousarray(pts[run]), np.round(la[run], 9), np.round(lo[run], 9),
                            np.asarray(z[run], np.float64), round(float(half), 3),
                            np.round(np.asarray(w, np.float64)[run], 4), np.round(np.asarray(share, np.float64)[run], 4),
                            np.round(np.asarray(hand, np.float64)[run], 4), np.round(np.asarray(low, np.float64)[run], 4)))
    entry = (tuple(sorted((q, v.info()[1]) for q, v in cache.items())), out)
    with _memo_lock:
        STATS["pins_squares"] += 1
        if not revisit.scope_short():           # (every square it read complete: kept for the next query)
            _pins[mk] = entry
            while len(_pins) > PIN_MEMO_MAX:
                _pins.pop(next(iter(_pins)))
    return out, entry


def pins_near(la0, lo0, la1, lo1, pad_m, cell_fn, today_ll, ground_ll=None):
    """grading.PROFILE_PINS under BNE_ROADSYS=heights: the terrain targets of the profile squares within pad_m of the box
    (degrees), as roadshape._built's pins [(lat, lon, dz against today_ll, height today, half width, fade, share)]:
    each line cut to the box grown by pad_m + CLIP_PAD_M (one point past each cut: every segment within pad_m of a
    point in the box is whole, so a point's change is the same whichever query asks), each point's from its own
    region's squares, sorted by content (roadshape.pins_near). today_ll is read only at those points."""
    import math
    import dmath
    import revisit
    import roadshape
    reach = pad_m + CLIP_PAD_M
    dla = reach / 110574.0
    dlo = reach / (111320.0 * max(0.05, dmath.cos(math.radians(max(abs(la0), abs(la1))))))
    g0, g1, h0, h1 = la0 - dla, la1 + dla, lo0 - dlo, lo1 + dlo
    corners = [(g0, h0), (g0, h1), (g1, h0), (g1, h1)]
    regs = {}
    for a, b in corners:
        f, fr = region(a, b)
        regs.setdefault(f, fr)
    lines = []
    for fid, frame in sorted(regs.items()):
        la = np.array([c[0] for c in corners])
        lo = np.array([c[1] for c in corners])
        e, n, _ = frame.geodetic_to_enu(la, lo, 0.0)
        i0, i1 = int(math.floor(float(np.min(e)) / SQUARE_M)), int(math.floor(float(np.max(e)) / SQUARE_M))
        j0, j1 = int(math.floor(float(np.min(n)) / SQUARE_M)), int(math.floor(float(np.max(n)) / SQUARE_M))
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                got = _square_pins(fid, frame, (i, j), cell_fn, ground_ll)
                if got is None:
                    revisit.fault(here=True)
                    continue
                for xy, pla, plo, z, half, w, share, hand, low in got:
                    inn = (pla >= g0) & (pla <= g1) & (plo >= h0) & (plo <= h1)
                    if not inn.any():
                        continue
                    keep = inn.copy()
                    keep[1:] |= inn[:-1]
                    keep[:-1] |= inn[1:]
                    idx = np.flatnonzero(keep)
                    for run in np.split(idx, np.flatnonzero(np.diff(idx) > 1) + 1):
                        if len(run) >= 2:
                            lines.append((pla[run], plo[run], z[run], half, w[run], share[run], hand[run], low[run]))
    if not lines:
        return []
    alla = np.concatenate([x[0] for x in lines])
    allo = np.concatenate([x[1] for x in lines])
    today = np.asarray(today_ll(alla, allo), np.float64)
    pins = []
    k = 0
    for pla, plo, z, half, w, share, hand, low in lines:
        t = today[k:k + len(pla)]
        k += len(pla)
        # (M3: the plans' ground stays the plans' (their approach fills: the chains meet them, chain_heights) but round a
        # floor under a structure, where the profile digs the road under (low), fading back over the approach reach; not
        # round its own structures' ends (hand). M2 laid the whole section's profile over the plans' ground: the two
        # smoothings fought everywhere, the buried and the bumps; raising the approaches to the profile's deck ends
        # buried the roads beside them and steepened their ramps)
        d0 = z - t
        dz = (1.0 - hand) * low * np.minimum(d0, 0.0)
        if not np.isfinite(dz).all() or float(np.abs(dz * w).max()) < 0.001:
            continue
        pins.append((pla, plo, np.round(dz, 4), np.round(t, 4), half, w, share))
    return roadshape.pins_near([{"pins": pins}], la0, lo0, la1, lo1, pad_m)
