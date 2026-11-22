"""BNE_ROADSYS: the road surface + bridge rewrite (bridgerewrite/DESIGN.md, approved 2026-11-21), its kernels in Rust
(native/roadsys, through earthfast's roadsys_* functions). This module is thin glue: it reads the map cells a tile's
product declares and hands them over.

  off (the default, or the variable absent): nothing here runs; the server is byte-identical to before.
  shadow (M1): every road geotile the server builds is built again as the new product in one background thread, off
      the delivery path: its read list (earthfast.roadsys_reads, asked again until no cell is missing: a function of
      the map data alone, never of the visit order), then the product (earthfast.roadsys_build: the lane graph, one
      profile per carriageway with the approach rule, the planar partition, markings, the tile's canonical bytes,
      checks and keys). One log line per tile and <cache>/roadsys/shadow-<format>/<frame>/<tile>.json; nothing the
      player sees reads them (revisit.NOT_SWITCHES: no key holds the switch).

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

log = logging.getLogger("roadsys")

MODE = os.environ.get("BNE_ROADSYS", "off").strip().lower()
SHADOW = MODE == "shadow"
FORMAT = 1                  # the shadow records' JSON (their folder name)

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

    def __init__(self, md):
        self.md = md
        self.memo = {}
        self.rows = {}
        self.lock = threading.Lock()

    def cell_fn(self, key):
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
    """One cell's segment columns (lon / lat points), PT3's exporter's fields."""
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
            for a, b, v in _ranges(feats.rules.get("flags"), idx):
                if v in ("is_bridge", "is_tunnel"):
                    rg.append((a, b, 0.0 if v == "is_bridge" else 1.0, 0.0))
            for a, b, v in _ranges(feats.rules.get("level"), idx, vals=True):
                rg.append((a, b, 2.0, float(v)))
        out["rng"].append(rg)
    return out


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
    fast.require(596, "roadsys_build")
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
        boxes, fids, counts, cpu = fast._ef.roadsys_reads(segs, box, CUT)
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

    tb = time.perf_counter()
    text, data = fast._ef.roadsys_build(segs, [tuple(map(float, q)) for q in quad], (int(gx), int(gy)), fid, ground, CUT,
                                        dump, [list(map(float, b)) for b in boxes])
    rec = json.loads(text)
    kept, copies, parents, sections = segs.counts()
    rec.update(tile=geotiles.key(gx, gy), frame=f"{FRAME_Z}/{(fid >> 24) & 0xffffff}/{fid & 0xffffff}",
               complete=complete, rounds=rounds, cells=len(cells),
               segments={"kept": kept, "copies": copies, "roads_for_sections": parents, "sections_for_road": sections},
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
