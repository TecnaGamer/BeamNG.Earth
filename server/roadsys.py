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

  structures (M3 structures): heights, and the deck ends' hand-over (native/roadsys handover.rs; bridgerewrite/DESIGN.md
      6.2): at every ground end of a deck (a deck part's end no other deck part nor tunnel goes on from, where a ground
      part of a road begins: the map's own topology) the physics ground under the deck's first metres is lowered
      (handover_drop: from 1.5 lattice squares past the end, DROP_M, within the deck's width, never on another ground
      road's surface), by every evaluator of the physics ground (roads.block_arch_raise: the terrain blocks, the skins,
      the content's drawn ground), each point by the ends read from the 3 x 3 map cells round its own cell (a declared
      read: the same ends whoever asks); and the deck's exact variant for each anchor (always made: content parts "ho",
      earthfast road_exact) is rebuilt round those ends on that anchor's lattice (handover_deck: the deck's top meets
      the terrain on the terrain's own quantised nodes, 0 mm; its parts at ground level over the approach cleared; the
      end's mismatch faded out over 6 m). Keyed as heights is (its own value, revisit.keyed_value; plans ".rss").

  surface (M4): structures, and every deck drawn from the road system's own surface faces (native/roadsys deck.rs;
      deck_faces): the planar partition of each road geotile's strokes, built in the geotile's own frame from a
      complete read, its faces on a structure the deck's top (the drawn road and the collider from the same faces),
      their heights today's chain solve's on each map feature's deck (the profile the terrain and the hand-over were
      made for), flat across; the slab under them the slab's share of the structure's drawn depth
      (BridgeProfiles.depth_of: the floors'; its girders and other substructure are M5's kits);
      parapets on the free edges only (an edge no other drivable face shares, never across a deck end or a joint), on a
      PARAPET_W base strip; the markings the partition's own. Today's deck chunks of the features so drawn stay for the
      tile's other users (clearances, signs, sound, furniture) but are drawn by neither the road mesh nor the deck mesh
      ("rs_hide"); no junction pads. A structure the faces cannot take (no deck heights for a knot near the tile, a
      short read) keeps today's deck, recorded (STATS["deck_fallback"]).

  BNE_ROADSYS_AREAS (osmareas.py; with shadow or surface): audit: the products read OSM's mapped road areas
      (area:highway=*) of their read's cells and report how their faces meet them (the shadow record's "areas": edge
      distance, IoU, width error, splits; native/roadsys areas.rs); on: the areas are also the carriageway edges' soft
      targets (lane widths, outer shoulders, a shift; the lane rules keep the lanes), for the shadow tiles and M4's
      decks alike; keyed (a BNE_ switch; MapData.versions()["osmareas"]; the pieces' keys hold every area they read).

  BNE_ROADSYS_XSEC=on (with any of the modes above; off by default): the road system's cross-sections (native/roadsys
      xsec.rs): each segment's OSM street tags (railtags.XSEC_ATTR, the lane shards' "xsec" table: parking, cycle lanes,
      bus lanes, shoulders, sidewalks, width:carriageway / est_width, with OSM width and sidewalk) handed to the kernels
      as one text per segment (_xsec_text); every stroke gets strips for parking, cycle lanes and their buffers, its
      lanes from a mapped carriageway width where that is plausible. Keyed as every BNE_ setting is (revisit.keyed_env,
      SWITCH_VERSIONS); the segments' digests (the read lists' keys) hold the texts.

  Both on (BNE_ROADSYS_AREAS=on and BNE_ROADSYS_XSEC=on, earthfast 617): the areas fit acts as a mapped width, setting
      the carriageway's whole width and its edges where an area gives them; the cross-section's strips divide it: the
      lanes take the rest within the fit's bounds, then the outer shoulders down to their least, then the strips are
      squeezed (native/roadsys areas.rs fit_stroke, the knots' strip factor). Where no area applies (no polygon, a
      junction mouth, a fork's window, a structure under surface) the cross-section stands as it does alone. With one
      switch on, each is exactly as alone; with both off, nothing changes.

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
SURFACE = MODE == "surface"             # (M4 surface: structures, and the decks from the road system's own faces)
STRUCTURES = MODE in ("structures", "surface")      # (M3 structures: heights and the deck ends' hand-over, below)
HEIGHTS = MODE in ("heights", "structures", "surface")
FORMAT = 2                  # the shadow records' JSON (their folder name; 2: M2's profiles, whole components read)
HEIGHTS_KERNEL = 623        # earthfast.roadsys_heights_reads / roadsys_heights
STRUCTURES_KERNEL = 603     # earthfast.RoadsysEnds / roadsys_handover_zone, road_exact's "ho" parts
SURFACE_KERNEL = 623        # earthfast.roadsys_decks
XSEC = os.environ.get("BNE_ROADSYS_XSEC", "off").strip().lower() == "on"
XSEC_KERNEL = 617           # earthfast.RoadsysSegments(xsec=...), native/roadsys xsec.rs (617: with areas=, fit= in one binary; trunk 616 has no xsec)

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


_XSEP = "\x1f"


def _xsec_text(feats, i):
    """(BNE_ROADSYS_XSEC) feature i's cross-section tags as the kernels read them (native/roadsys xsec.rs parse):
    "key=value" pairs sorted by key, joined by U+001F; the lane shards' street tags (railtags.XSEC_ATTR) with OSM's
    width and sidewalk (osmtags) beside them; "" for a road without street tags."""
    import railtags
    col = feats.attrs.get(railtags.XSEC_ATTR)
    t = col[i] if col is not None else None
    if not t:
        return ""
    got = {str(k): str(v) for k, v in t.items()}
    w = feats.attrs.get("osm_width")
    if w is not None and w[i] is not None:
        try:
            v = float(w[i])
            if np.isfinite(v) and v > 0:
                got.setdefault("width", repr(v))
        except (TypeError, ValueError):
            pass
    sw = feats.attrs.get("osm_sidewalk")
    if sw is not None and sw[i] not in (None, ""):
        got.setdefault("sidewalk", str(sw[i]))
    return _XSEP.join(f"{k}={v}".replace(_XSEP, " ") for k, v in sorted(got.items()))


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
    out = dict(ids=[], texts=[], nums=[], ints=[], ll=[], cons=[], rng=[], cell=cell, xsec=[])
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
        out["xsec"].append(_xsec_text(feats, idx) if XSEC and idx is not None else "")
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


def _areas(md, cells, frame):
    """(areas, fit) for a read of cells in frame: earthfast.RoadsysAreas of their mapped road areas (osmareas.py,
    BNE_ROADSYS_AREAS audit / on) and whether they are the carriageways' targets (on); (None, False) when off or none."""
    import osmareas
    if not osmareas.ON or md is None:
        return None, False
    try:
        a = osmareas.areas_for(md, cells, frame)
    except Exception as ex:         # (no areas rather than no road: the build faults to be made again)
        log.warning("roadsys areas: %s: %s", type(ex).__name__, ex)
        import revisit
        revisit.fault()
        a = None
    return a, osmareas.FIT and a is not None


AREAS_MD_FN = None          # () -> MapData for the decks' areas (blockworker: its process's own), or None


def _segments(tables, frame, style, areas=None, fit=False):
    """earthfast.RoadsysSegments of the tables' segments in frame (every copy: the kernel keeps one); areas
    (RoadsysAreas) and fit: the mapped road areas round them (_areas)."""
    import fast
    ids, cells, texts, nums, ints, lls, off, xs = [], [], [], [], [], [], [0], []
    con_seg, con_id, con_v, rng_seg, rng = [], [], [], [], []
    for tab in tables:
        if tab is None:
            continue
        for j, k in enumerate(tab["ids"]):
            g = len(ids)
            ids.append(k)
            cells.append(tab["cell"])
            texts.append(tab["texts"][j])
            xs.append(tab["xsec"][j] if XSEC and "xsec" in tab else "")
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
    kw = {}
    if XSEC:                # (the cross-section model on: every stroke's strips; the texts in the segments' digests)
        fast.require(XSEC_KERNEL, "RoadsysSegments xsec")
        kw["xsec"] = xs
    return fast._ef.RoadsysSegments(
        ids, cells, texts, np.asarray(nums, np.float64).reshape(-1, 4), np.asarray(ints, np.int64).reshape(-1, 9), pts,
        np.asarray(off, np.int64), np.asarray(con_seg, np.int64), con_id, np.asarray(con_v, np.int64),
        np.asarray(rng_seg, np.int64), np.asarray(rng, np.float64).reshape(-1, 4), style, areas=areas, fit=bool(fit), **kw)


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
        areas, fit = _areas(md, cells, frame)
        segs = _segments(tables, frame, style, areas, fit)
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
STATS = {"chains": 0, "solved": 0, "unmatched": 0, "short": 0, "short_memo": 0, "error": 0, "squares": 0, "square_memo": 0,
         "square_disk": 0, "square_cells": 0, "square_cells_max": 0, "square_read_s": 0.0, "square_kernel_s": 0.0,
         "square_cpu_ms": 0.0, "pins_squares": 0, "obs_runs": 0, "obs_measured": 0, "total_s": 0.0}
SQ_MEMO_MAX = 96            # squares kept per process (by region, square and their cells' road revisions)
PIN_MEMO_MAX = 48
_squares = {}
_short_sq = {}              # (M5) squares read short: (monotonic time, square), SHORT_TTL_S
SHORT_TTL_S = 30.0
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
        short = _short_sq.get(mk)
    if got is not None:
        with _memo_lock:
            STATS["square_memo"] += 1
        return got
    if short is not None and time.monotonic() - short[0] < SHORT_TTL_S:
        # (M5: a square read short is the same square while its cells' revisions are: kept a short while for this
        # process's other askers (each of them faulted, never kept), not solved again on every grading query: 628 short
        # solves over 36 corpus places)
        revisit.fault(here=True)
        with _memo_lock:
            STATS["short_memo"] += 1
        return short[1]
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
                # (read short: used by its askers alone, which are not kept; never memoised as complete nor kept on disk,
                # so no later build takes it as complete; SHORT_TTL_S in this process: _short_sq)
                revisit.fault(here=True)
                with _memo_lock:
                    STATS["short"] += 1
                    _short_sq[mk] = (time.monotonic(), got)
                    while len(_short_sq) > SQ_MEMO_MAX:
                        _short_sq.pop(next(iter(_short_sq)))
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
DECK_HALF_MIN_M = 2.0       # m: a deck part's half width at least, for the ends on another deck (_ends_on_decks)


def _half_width(part):
    """A published deck part's half width (its spec's width; DECK_HALF_MIN_M at least)."""
    try:
        w = float(getattr(part[0], "width", 0.0) or 0.0)
    except (TypeError, ValueError, IndexError):
        w = 0.0
    return max(DECK_HALF_MIN_M, 0.5 * w)


def _ends_on_decks(pts, owner, decks):
    """(n,) bool: each point (n, 2) within the half width of a deck line of decks [(key, line (m, 2), half)] other
    than its owner's (owner: n keys) and of the same chain: an end there goes on onto that deck."""
    out = np.zeros(len(pts), bool)
    for i, (x, y) in enumerate(np.asarray(pts, np.float64)):
        for k, line, half in decks:
            if k == owner[i] or len(line) < 2:
                continue
            a, b = line[:-1], line[1:]
            ab = b - a
            l2 = ab[:, 0] * ab[:, 0] + ab[:, 1] * ab[:, 1]
            t = np.clip(np.where(l2 > 1e-12, ((x - a[:, 0]) * ab[:, 0] + (y - a[:, 1]) * ab[:, 1])
                                 / np.where(l2 > 1e-12, l2, 1.0), 0.0), 0.0, 1.0)
            fx, fy = a[:, 0] + t * ab[:, 0] - x, a[:, 1] + t * ab[:, 1] - y
            if float(np.min(fx * fx + fy * fy)) <= half * half:
                out[i] = True
                break
    return out


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
        # (M5: an end whose approach point lies on another deck of the chain is no abutment: the deck goes on there
        # (two parts of one viaduct a metre or two apart, past endmatch's JOIN_M), so no ground is read for it. The
        # ground under the next deck brought a viaduct's joint down to the floor of the valley: an 8 m V, railramp)
        others = [(k, np.asarray(parts_in[k][1], np.float64)[:, :2], _half_width(parts_in[k]))
                  for k in keys]
        on_deck = _ends_on_decks(ep, [k for k in ks for _ in (0, 1)], others)
        gu = np.where(on_deck, np.nan, gu)
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
    mk = (fid, tuple(sq), getattr(ground_ll or GROUND, "tag", getattr(ground_ll or GROUND, "__name__", "")))
    with _memo_lock:
        got = _pins.get(mk)
    if got is not None:
        # (M5: kept only when every square it blended was read complete, and a square is a pure function of its
        # window's map cells: taken as it is. Looking every square up again read each one's window of map cells on
        # every grading query, 15,000 cell reads at the JFX: a quarter of the heights mode's build time)
        return got[1], None
    me = square(fid, frame, sq, cell_fn, ground_ll)
    if me is None:
        return None, None
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


# ------------------------------------------------------------------------- M3 structures: the deck ends' hand-over

HANDOVER_CELLS_KEPT = 64        # RoadsysEnds kept per process (per frame and centre cell)
_ends_memo = {}
_ends_lock = threading.Lock()
HANDOVER_STATS = {"ends_built": 0, "ends_hits": 0, "points": 0, "dropped": 0, "zones": 0, "zone_errors": 0}


def _end_rows(feats):
    """One cell's road parts for the hand-over (memoised on the cell's features): [(kind, fid, part no., lon / lat
    (n, 2), half width)] by the map's own tags (roads.tag_level_ranges: the structure ends as mapped, never the deck
    builder's lengthened or inferred ranges, which the map calls ground): kind "b" a tagged bridge part that a deck of
    the deck builder's covers (roads.bridge_parts; its half width roads.bridge_specs': the deck builder's), "g" a
    tagged ground part (its half width road_specs' plain one: no urban or measured widths, the same in every process),
    "t" a tunnel part. BNE_ROADSYS=surface: by the support labels instead (roads.level_ranges, the decks' faces')."""
    got = feats.__dict__.get("_roadsys_end_rows")
    if got is not None:
        return got
    import roads
    out = []
    if feats is not None and len(feats):
        bs = roads.bridge_specs(feats)
        gs = roads.road_specs(feats)
        for i in range(len(feats)):
            fid = str(feats.ids[i])
            sp = gs[i] if gs[i] is not None else bs[i]
            if sp is None:
                continue
            # (M4 surface: the support labels the decks are drawn from, roads.level_ranges: lengthened and inferred
            # deck ends hand over too, where their faces end)
            tags = roads.level_ranges(feats, i) if SURFACE else roads.tag_level_ranges(feats, i)
            if bs[i] is not None:
                decks = [np.asarray(r, np.float64)[:, :2] for r in roads.bridge_parts(feats, i)]
                for j, ring in enumerate(roads._parts_by_level_new(feats, i, "bridge", rngs=tags)):
                    ll = np.asarray(ring, np.float64)[:, :2]
                    if decks and _overlaps(ll, decks):
                        out.append(("b", fid, j, ll, float(bs[i].width) / 2))
            for j, ring in enumerate(roads._parts_by_level_new(feats, i, "ground", rngs=tags)):
                out.append(("g", fid, j, np.asarray(ring, np.float64)[:, :2], float(sp.width) / 2))
            for j, ring in enumerate(roads.tunnel_parts(feats, i)):
                out.append(("t", fid, j, np.asarray(ring, np.float64)[:, :2], 0.0))
    feats.__dict__["_roadsys_end_rows"] = out
    return out


def _overlaps(ll, parts):
    """Does lon / lat polyline ll share a stretch with one of parts (a vertex of either within ~1 m of the other's
    line, in degrees scaled to metres)?"""
    import dmath
    import math
    k = dmath.cos(math.radians(float(ll[0, 1])))

    def near(a, b):
        ax, ay = a[:, 0] * k * 111320.0, a[:, 1] * 111320.0
        bx, by = b[:, 0] * k * 111320.0, b[:, 1] * 111320.0
        for x, y in zip(ax, ay):
            dx, dy = bx[1:] - bx[:-1], by[1:] - by[:-1]
            l2 = dx * dx + dy * dy
            u = np.clip(np.where(l2 > 0, ((x - bx[:-1]) * dx + (y - by[:-1]) * dy) / np.where(l2 > 0, l2, 1.0), 0.0), 0.0, 1.0)
            ex, ey = bx[:-1] + u * dx - x, by[:-1] + u * dy - y
            d2 = ex * ex + ey * ey
            if len(d2) and float(d2.min()) < 1.0:
                return True
        return False
    return any(len(p) >= 2 and len(ll) >= 2 and (near(p, ll) or near(ll, p)) for p in parts)


def _frame_key(frame):
    return (type(frame).__name__, float(frame.lat0), float(frame.lon0))


def _ends_from(frame, cell_fn, keys, reg):
    """earthfast.RoadsysEnds of the road parts of map cells keys (in frame) coming within the frame box reg; None when
    a cell cannot be read."""
    import fast
    import heightmap
    fast.require(STRUCTURES_KERNEL, "the deck ends' hand-over")
    rows = {}
    for k in keys:
        try:
            feats = cell_fn(k)
        except Exception as ex:             # (no ends rather than no ground: nothing built with it kept)
            log.debug("roadsys hand-over: cell %s: %s: %s", k, type(ex).__name__, ex)
            import revisit
            revisit.fault()
            return None
        if feats is None or not len(feats):
            continue
        xs = _xsec_extents(cell_fn, k) if XSEC else {}
        for kind, fid, j, ll, hw in _end_rows(feats):
            if kind == "b" and feature(fid) in xs:      # (a deck drawn from its cross-section: its own half extent)
                hw = max(hw, xs[feature(fid)])
            rows.setdefault((kind, fid, j), (ll, hw))     # (a feature in several cells: once)
    br, gr, tu = [], [], []
    for (kind, fid, j) in sorted(rows):
        ll, hw = rows[(kind, fid, j)]
        if len(ll) < 2:
            continue
        e, nn, _u = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], np.zeros(len(ll)))
        xy = np.ascontiguousarray(np.column_stack((np.asarray(e, np.float64), np.asarray(nn, np.float64))), np.float64)
        if xy[:, 0].max() < reg[0] or xy[:, 0].min() > reg[2] or xy[:, 1].max() < reg[1] or xy[:, 1].min() > reg[3]:
            continue
        if kind == "b":
            br.append((xy, hw))
        elif kind == "g":
            gr.append((xy, hw))
        else:
            tu.append(xy)
    return fast._ef.RoadsysEnds(br, gr, tu, float(heightmap.SQUARE))


def _xsec_extents(cell_fn, key):
    """(BNE_ROADSYS_XSEC) {map feature: half extent of its carriageway with its cross-section} of cell key's segments
    whose cross-section shapes them (earthfast.roadsys_xsec_half_extent: the deck faces' own width), the widest copy."""
    import fast
    fast.require(XSEC_KERNEL, "roadsys_xsec_half_extent")
    tab = _cells_for(cell_fn).table(key)
    out = {}
    if tab is None:
        return out
    for k, texts, nums, ints, x in zip(tab["ids"], tab["texts"], tab["nums"], tab["ints"], tab["xsec"]):
        if not x:
            continue
        e = fast._ef.roadsys_xsec_half_extent(tuple(texts), list(map(float, nums)), list(map(int, ints)), x)
        if e is not None:
            f = feature(k.split("|", 1)[0])
            out[f] = max(out.get(f, 0.0), float(e))
    return out


def ends_of_cell(frame, cell_fn, key):
    """earthfast.RoadsysEnds of the deck ends that may lower the ground in map cell key (in frame): read from the 3 x 3
    cells round it (an end reaches handover.REACH_M + its half width, well inside one cell's neighbours), so a point's
    drop is the same whichever caller asks. None when a cell cannot be read (no drop: nothing kept, revisit.fault)."""
    import heightmap
    import mapcells
    mk = (_frame_key(frame), str(key), float(heightmap.SQUARE))
    with _ends_lock:
        got = _ends_memo.get(mk)
        if got is not None:
            HANDOVER_STATS["ends_hits"] += 1
            return got
    n = 1 << key.z
    keys = [mapcells.CellKey(key.z, (key.x + dx) % n, key.y + dy) for dy in (-1, 0, 1) for dx in (-1, 0, 1)
            if 0 <= key.y + dy < n]
    # the region the centre cell's points can be reached from (its bounds + 60 m), in the frame
    la0, lo0, la1, lo1 = _cell_bounds(key)
    bx, by, _ = frame.geodetic_to_enu(np.array([la0, la0, la1, la1]), np.array([lo0, lo1, lo0, lo1]), np.zeros(4))
    reg = (float(np.min(bx)) - 60.0, float(np.min(by)) - 60.0, float(np.max(bx)) + 60.0, float(np.max(by)) + 60.0)
    got = _ends_from(frame, cell_fn, keys, reg)
    if got is None:
        return None
    with _ends_lock:
        HANDOVER_STATS["ends_built"] += 1
        _ends_memo[mk] = got
        while len(_ends_memo) > HANDOVER_CELLS_KEPT:
            _ends_memo.pop(next(iter(_ends_memo)))
    return got


HANDOVER_BACK_M = 40.0          # m round a deck shape's box its hand-over's lattice may reach (handover.rs BACK_M + some)


def ends_for_box(frame, cell_fn, box):
    """earthfast.RoadsysEnds of the deck ends round a frame box (a deck shape's hand-over: the zone's geometry; the
    drops themselves always from handover_drop), from the cells within 60 m of it. None when a cell cannot be read."""
    import mapcells
    x0, y0, x1, y1 = box
    reg = (x0 - 60.0, y0 - 60.0, x1 + 60.0, y1 + 60.0)
    la, lo, _ = frame.enu_to_geodetic(np.array([reg[0], reg[2], reg[2], reg[0]]), np.array([reg[1], reg[1], reg[3], reg[3]]),
                                      np.zeros(4))
    keys = mapcells.cells_for_bbox(float(np.min(la)), float(np.min(lo)), float(np.max(la)), float(np.max(lo)))
    return _ends_from(frame, cell_fn, keys, reg)


def _cell_bounds(key):
    """(lat0, lon0, lat1, lon1) of a map cell (mapcells.CellKey.bounds)."""
    lon0, lat0, lon1, lat1 = key.bounds()
    return lat0, lon0, lat1, lon1


def handover_drop(frame, cell_fn, la, lo):
    """m the physics ground at the points (la, lo) is lowered by under the first metres of the decks' ground ends
    (BNE_ROADSYS=structures; 0 elsewhere, and everywhere in any other mode), the points in frame (each by the ends of
    its own map cell's neighbourhood: ends_of_cell)."""
    la = np.asarray(la, np.float64)
    shape = la.shape
    out = np.zeros(la.size)
    if not STRUCTURES or not la.size:
        return out.reshape(shape)
    import mapcells
    la = la.ravel()
    lo = np.asarray(lo, np.float64).ravel()
    e, n, _u = frame.geodetic_to_enu(la, lo, 0.0)
    e = np.ascontiguousarray(np.asarray(e, np.float64).ravel())
    n = np.ascontiguousarray(np.asarray(n, np.float64).ravel())
    ok = np.isfinite(la) & np.isfinite(lo)
    keys = mapcells.cells_of(np.where(ok, la, 0.0), np.where(ok, lo, 0.0))
    groups = {}
    for i, k in enumerate(keys):
        if ok[i]:
            groups.setdefault(k, []).append(i)
    for k in sorted(groups, key=str):
        ends = ends_of_cell(frame, cell_fn, k)
        if ends is None or not ends.count():
            continue
        idx = np.asarray(groups[k], np.int64)
        out[idx] = ends.drops(np.ascontiguousarray(e[idx]), np.ascontiguousarray(n[idx]))
    HANDOVER_STATS["points"] += int(la.size)
    HANDOVER_STATS["dropped"] += int((out > 0).sum())
    return out.reshape(shape)


# ------------------------------------------------------------------------- M4 surface: decks from the road faces

DECK_WINDOW_M = 600.0       # m round the tile's box its decks' read reaches (their lane programs: taper reads 400 m)
DECK_STATS = {"tiles": 0, "built": 0, "faces": 0, "runs": 0, "fallback": 0, "short": 0, "short_cell": 0, "short_max": 0,
              "errors": 0, "cells": 0,
              "kernel_s": 0.0, "read_s": 0.0}
DECK_DUMP = os.environ.get("ROADSYS_DECK_DUMP") or None     # (tools only: each tile's decks written there)
DECK_INPUTS = os.environ.get("ROADSYS_DECK_INPUTS") or None  # (tools only: each tile's decks' whole input, `roadsys decks`)
DECK_KEEP_M = 30.0          # m round the tile's box its decks' faces are built (native/roadsys deck.rs KEEP_M)
DECK_LINES_M = 50.0         # m round the box today's deck heights are read (KEEP_M + EXT_M + some)
K_STRIP = 9                 # deck.rs K_STRIP: a parapet's base, the slab's top only
K_MEDIAN = 10               # deck.rs K_MEDIAN: the filled gap to a twin deck, the slab's top only, thin
MEDIAN_DEPTH_M = 0.04       # m of drawn depth a twin gap's fill takes (its slab ~2 cm: below the envelope's own band)
DECK_GROUND_M = 300.0       # m round the tile's box the decks' build reads the ground (farther: that box's edge)


def _style_of(country, lon):
    """The marking style tuple RoadsysSegments takes (build_tile's)."""
    import markings
    st = markings.style(country, lon)
    return (st.centre, st.left_edge or "", float(st.w), float(st.w_motorway), float(st.edge), float(st.edge_motorway),
            tuple(map(float, st.centre_urban)), tuple(map(float, st.lane_urban)), tuple(map(float, st.lane_motorway)),
            bool(st.double_centre_urban))


def _clip_boxes(boxes, w):
    out = []
    for b in boxes:
        c = [max(b[0], w[0]), max(b[1], w[1]), min(b[2], w[2]), min(b[3], w[3])]
        if c[0] <= c[2] and c[1] <= c[3]:
            out.append(c)
    return out


def _deck_read(frame, cell_fn, box, style):
    """(segs, boxes, cells) of a complete read for a tile's decks in frame (roadsys_reads with the profiles from the
    deck heights: no vertical component read), within the tile's window (its box + DECK_WINDOW_M: the read list's boxes
    cut to it, a stroke reaching out of it cut there; a fixed window per tile, so the read is the map's alone), or None
    (a cell not at hand: revisit.fault; past MAX_CELLS)."""
    import fast
    import revisit
    cc = _cells_for(cell_fn)
    w = [box[0] - DECK_WINDOW_M, box[1] - DECK_WINDOW_M, box[2] + DECK_WINDOW_M, box[3] + DECK_WINDOW_M]
    wcells = _cells_of_boxes(frame, [w])
    cells = _cells_of_boxes(frame, [[box[0] - 60, box[1] - 60, box[2] + 60, box[3] + 60]])
    seen = set()
    for _r in range(MAX_ROUNDS):
        for k in sorted(cells - seen, key=str):
            if cell_fn(k) is None:
                revisit.fault()
                with _memo_lock:
                    DECK_STATS["short_cell"] += 1
                return None
        seen |= cells
        tables = [cc.table(k) for k in sorted(cells, key=str)]
        areas, fit = _areas(AREAS_MD_FN() if AREAS_MD_FN is not None else None, cells, frame)
        segs = _segments(tables, frame, style, areas, fit)
        boxes, fids, _counts, _cpu = fast._ef.roadsys_reads(segs, list(map(float, box)), CUT, True)
        boxes = _clip_boxes(boxes, w)
        need = (_cells_of_boxes(frame, boxes) | _copy_cells(tables, fids)) & wcells
        missing = need - cells
        if not missing:
            return segs, boxes, len(cells)
        if len(cells | need) > MAX_CELLS:
            with _memo_lock:
                DECK_STATS["short_max"] += 1
            return None
        cells |= need
    return None


def deck_faces(frame, exact, bridges, cells, country=None):
    """BNE_ROADSYS=surface: the decks of the tile box exact (x0, y0, x1, y1, in frame) from the road system's own
    faces (native/roadsys deck.rs) as roads.build_tile chunks: ([chunk], {map features drawn so}), or None (no deck
    here, or today's decks: a read short, the kernel failing). Chunks: per material the deck's top ("tri" chunks,
    "deck_z" today's heights flat across, "deck_depth" the structure's drawn depth: its slab's share, roadmesh.deck's
    faces; the first carries the parapet runs, "deck_runs"), the parapets' base strips ("rs_slab": the slab's top only) and the
    markings ("rs_mark")."""
    import fast
    import roadmesh
    import roads
    import dnp
    import roadsurf3
    fast.require(SURFACE_KERNEL, "roadsys_decks")
    cell_fn = getattr(bridges, "cell_fn", None)
    if cell_fn is None:
        return None
    x0, y0, x1, y1 = map(float, exact)
    m = DECK_LINES_M
    got = [(fid, sp, np.asarray(xy, np.float64), np.asarray(z, np.float64)) for fid, sp, xy, z, _lon in
           bridges.parts(cells, (x0 - m, y0 - m, x1 + m, y1 + m))]
    got = [g for g in got if len(g[2]) >= 2 and np.isfinite(g[3]).all()]
    if not got:
        return None
    with _memo_lock:
        DECK_STATS["tiles"] += 1
    t0 = time.perf_counter()
    try:
        _la, _lo, _u = frame.enu_to_geodetic(np.array([(x0 + x1) / 2]), np.array([(y0 + y1) / 2]), 0.0)
        read = _deck_read(frame, cell_fn, (x0, y0, x1, y1), _style_of(country, float(np.asarray(_lo).reshape(-1)[0])))
    except Exception as ex:          # (today's decks rather than none)
        log.warning("roadsys decks: read: %s: %s", type(ex).__name__, ex)
        read = None
    t1 = time.perf_counter()
    if read is None:
        with _memo_lock:
            DECK_STATS["short"] += 1
        return None
    segs, boxes, ncells = read
    lines = [(feature(fid), np.ascontiguousarray(np.c_[xy[:, :2], z], np.float64)) for fid, _sp, xy, z in got]
    spec_of, depth_of = {}, {}
    dd = getattr(bridges, "depth_of", None)
    for fid, sp, _xy, _z in sorted(got, key=lambda g: str(g[0])):
        f = feature(fid)
        spec_of.setdefault(f, sp)
        if dd is not None:
            depth_of[f] = max(depth_of.get(f, 0.0), float(dd(fid)))
    quad = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]

    gb = (x0 - DECK_GROUND_M, y0 - DECK_GROUND_M, x1 + DECK_GROUND_M, y1 + DECK_GROUND_M)

    def ground(e, n):
        # (the ground near the tile only: a point farther out takes the ground at its nearest point of that box; only
        # the decks' faces near the tile are kept, their heights today's decks')
        e = np.clip(np.asarray(e, np.float64), gb[0], gb[2])
        n = np.clip(np.asarray(n, np.float64), gb[1], gb[3])
        return np.asarray(bridges._ground(np.column_stack((e, n))), np.float64)
    try:
        kw = {"dump": os.path.join(DECK_INPUTS, f"decks_{float(frame.lat0):.6f}_{float(frame.lon0):.6f}.rsdk")} if DECK_INPUTS else {}
        (V, T, K, F, D, feats, runs, MV, MT, MC, fallback, js) = fast._ef.roadsys_decks(
            segs, quad, ground, lines, CUT, [list(map(float, b)) for b in boxes], **kw)
    except Exception as ex:
        with _memo_lock:
            DECK_STATS["errors"] += 1
        log.warning("roadsys decks: %s: %s", type(ex).__name__, ex)
        return None
    t2 = time.perf_counter()
    with _memo_lock:
        DECK_STATS["built"] += 1
        DECK_STATS["cells"] += ncells
        DECK_STATS["read_s"] += t1 - t0
        DECK_STATS["kernel_s"] += t2 - t1
        DECK_STATS["fallback"] += len(fallback)
    if len(fallback):
        log.debug("roadsys decks: today's decks for %s (no deck heights near the tile)", fallback)
    covered = sorted({feats[i] for i in set(np.asarray(F, np.int64).tolist()) if i >= 0} - set(fallback))
    if not len(T) or not covered:
        return None
    V, T = np.asarray(V, np.float64), np.asarray(T, np.int64)
    K, F, D = np.asarray(K, np.int64), np.asarray(F, np.int64), np.asarray(D, np.float64)
    box_poly = np.asarray(quad, np.float64)
    uv_all = np.column_stack((V[:, 0] / roadmesh.DECK_TOP_TILE_M, V[:, 1]))
    default_depth = max(depth_of.values()) if depth_of else roads.DECK_FULL_DEPTH
    out = []
    groups = {}
    for i in range(len(T)):
        f = feats[F[i]] if F[i] >= 0 else None
        sp = spec_of.get(f) if f is not None else None
        if sp is None:
            sp = spec_of[min(spec_of)]
        depth = depth_of.get(f, 0.0) if f is not None else 0.0
        if not depth > 0.0:
            depth = float(D[i]) if D[i] > 0 else default_depth
        look = roads._deck_look(sp)
        if K[i] == K_MEDIAN:        # (a twin's filled gap: a thin slab, no walls between the twins' slabs)
            depth = MEDIAN_DEPTH_M
        key = ("strip" if K[i] in (K_STRIP, K_MEDIAN) else "top", look["m"], look["tl"], look["p"], round(depth, 3))
        groups.setdefault(key, []).append(i)
    for key in sorted(groups):
        kind, mat, tl, prio, depth = key
        sel = np.asarray(groups[key], np.int64)
        xyz, uv, clipped, _src = fast._ef.clip_tris_convex(np.ascontiguousarray(V), np.ascontiguousarray(uv_all),
                                                           np.ascontiguousarray(T[sel]), box_poly, True)
        xyz, clipped = np.asarray(xyz, np.float64), np.asarray(clipped, np.int64)
        if not len(clipped):
            continue
        if kind == "strip":
            ch = roadsurf3._tri_chunk(xyz[:, :2], np.asarray(uv, np.float64), clipped, roadmesh.DECK_SIDE, 10.0, prio,
                                      "base")
            ch["rs_slab"] = True
        else:
            ch = roadsurf3._tri_chunk(xyz[:, :2], np.asarray(uv, np.float64), clipped, mat, tl, prio, "base")
        ch.update(fixed=True, deck_z=xyz[np.unique(clipped), 2], deck_depth=float(depth), rs_face=True)
        out.append(ch)
    if not out:
        return None
    # the parapets: on the free edges' base strips (their outer lines), clipped to the box after the whole run
    runs_out = []
    for pts, nrm, closed, _fi in runs:
        pts, nrm = np.asarray(pts, np.float64), np.asarray(nrm, np.float64)
        if len(pts) < 2:
            continue
        full = np.column_stack((pts[:, :2], pts[:, 2], nrm))
        if closed:
            full = np.vstack((full, full[:1]))
        ends = [] if closed else [tuple(map(float, full[0, :2])), tuple(map(float, full[-1, :2]))]
        for r in roads.clip_polyline(full, x0, y0, x1, y1):
            r = np.asarray(r, np.float64)
            if len(r) < 2:
                continue
            o = r[:, 3:5] / np.maximum(dnp.hypot(r[:, 3], r[:, 4]), 1e-9)[:, None]
            runs_out.append(dict(edge=r[:, :2], z=r[:, 2], out=o, mid=r[:, :2],
                                 inner=r[:, :2] - o * roadmesh.PARAPET_W,
                                 line=r[:, :2] - o * (roadmesh.PARAPET_W * 0.5), ends=list(ends), nose=False, v=None))
    out[0]["deck_runs"] = runs_out
    # the markings on the decks' faces
    MV, MT, MC = np.asarray(MV, np.float64), np.asarray(MT, np.int64), np.asarray(MC, np.int64)
    for colour in sorted(set(MC.tolist())):
        sel = MT[MC == colour]
        mat = "line_yellow" if colour == 1 else "line_white"
        xyz, uv, clipped, _src = fast._ef.clip_tris_convex(
            np.ascontiguousarray(MV), np.ascontiguousarray(np.column_stack((MV[:, 0], MV[:, 1]))),
            np.ascontiguousarray(sel), box_poly, True)
        xyz, clipped = np.asarray(xyz, np.float64), np.asarray(clipped, np.int64)
        if not len(clipped):
            continue
        ch = roadsurf3._tri_chunk(xyz[:, :2], np.zeros((len(xyz), 2)), clipped, mat, 10.0, 30, "mark")
        ch.update(fixed=True, deck_z=xyz[np.unique(clipped), 2], rs_mark=True)
        out.append(ch)
    st_ = json.loads(js)
    with _memo_lock:
        DECK_STATS["faces"] += int(st_.get("faces", 0))
        DECK_STATS["runs"] += len(runs_out)
    if DECK_DUMP:               # (probes: what each tile's decks took)
        with open(os.path.join(DECK_DUMP, f"decks_{float(frame.lat0):.6f}_{float(frame.lon0):.6f}.json"), "w", encoding="utf-8") as f:
            json.dump({"stats": st_, "covered": sorted(covered), "fallback": list(fallback), "feats": list(feats),
                       "lines": [(fid, xy.round(2).tolist(), z.round(3).tolist()) for fid, _sp, xy, z in got],
                       "runs": [np.asarray(p, np.float64).round(3).tolist() for p, _n, _c, _f in runs],
                       "faces": {"v": V.round(3).tolist(), "t": T.tolist(), "k": K.tolist(), "f": F.tolist()}}, f)
    log.debug("roadsys decks: %s, %d chunks, read %.0f ms (%d cells), kernel %.0f ms", js, len(out),
              (t1 - t0) * 1e3, ncells, (t2 - t1) * 1e3)
    return out, set(covered)
