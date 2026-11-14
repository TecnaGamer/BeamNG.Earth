"""Paint lists: a ground photo's paint (surface.paint_square: crop rows, golf, parking lots and pads, roundabout islands,
the carriageways in the photo's own tone, the rounded corners) made from lists built once per set of map cells and
painted with one native call per photo (earthfast 295 paintlist.rs: paint_photo, without the interpreter lock).

The far field's road paint was the main process's Python, again for every far square of a cell: the carriageways'
specs, placement, tapers and frame conversion, the gores, the roundabouts, the junction corners and the lots, 44-153
sample-s a visit beside the event loop (expo-baseline 2026-11-12 mechanism 4; research-2026-11-13-realworld-streaming
proposal 5: "a per-cell paint list, clipped once and rasterized many times"). Here each of those is made once per
(anchor frame, cells, land cover and widths) in the anchor frame's metres and kept (PARTS_KEPT sets in memory, and in
the revisit cache for the server's other processes and the next visit: the far textures are painted in the far-tile
processes, earthd.Server.road_faces / blockworker.far_paint, and ten of them made each cell's parts ten times before;
a part made while something it read was short, revisit.fault, is not kept: used again for SHORT_KEEP_S, its faults told
to every photo painted with it), and a square only offsets, scales and draws.

What a part holds is what the Python per square computed, point by point the same numbers (the same functions, the
same conversions), so a photo painted from it is byte-identical (tests/test_paintlist.py against
tests/paint_reference.py, the per-square Python it replaced). The parts that looked around the square:
    - the junction corners (roadcorners: the corners round the square's bounds rounded out to 512 m) per such bounds,
      exactly as before;
    - the ramp gores (markings.gores over the roads within 300 m of the square) and the roundabouts (islands.find over
      the roads within 150 m) over all of the cells' roads at once: the same wedges and footprints wherever they reach
      the square (a gore reaches GORE_REACH_M along its ramp, a roundabout ISLAND_REACH_M); the per-square view could
      miss the motorway a ramp ending ~300 m away joins.
"""
import logging
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future

import numpy as np

import fast
import mapgeom
import revisit
import roads as roadsmod

log = logging.getLogger("earthd.paintlist")

NATIVE = 295
PARTS_KEPT = 16             # cell sets whose parts are kept (each holds its cells' layers alive while kept)
CORNER_GROUPS_KEPT = 64     # corner bound groups kept per cell set
PARTS_PER_SET = 128         # parts kept per cell set (roads and islands per group, lots, fields)
BOUNDS_ROUND_M = 512.0      # (roadcorners.BOUNDS_ROUND_M: one corner group per rounded bounds)

_LOCK = threading.Lock()
_SETS = OrderedDict()       # key -> _Set
_STYLE = [None, None]       # (constants, earthfast.PaintStyle)


def available():
    """The native paint is there (earthfast 295)."""
    try:
        fast.require(NATIVE, "paint lists")
        return True
    except Exception:
        return False


def style():
    """The earthfast.PaintStyle of surface's paint constants (made again if a constant changed)."""
    import surface as s
    c = ([s.PHOTO_TONE_RADIUS_M, *s.PHOTO_TONE_SAMPLE_LUMA, s.PHOTO_TONE_SAMPLE_SAT, s.PHOTO_TONE_GREEN,
          s.ISLAND_MIN_GREEN, s.ISLAND_LAWN_SHARE, *s.ISLAND_GRASS, *s.ISLAND_LAWN, s.LAWN_GREEN, s.FARM_ROW_M,
          s.FARM_FURROW_M, s.FARM_MIX, *s.FARM_SOIL] + [v for _, col in s.GOLF_PHOTO for v in col])
    key = (tuple(c), tuple(s.PHOTO_ROAD_RGBA.items()), tuple(s.PHOTO_ROAD_TONE.items()))
    got = _STYLE
    if got[0] != key:
        fast.require(NATIVE, "paint lists")
        colors = np.array([s.PHOTO_ROAD_RGBA[k] for k in s.PHOTO_ROAD_RGBA], np.float32)
        tone = np.array([s.PHOTO_ROAD_TONE[k] for k in s.PHOTO_ROAD_RGBA], np.float32)
        if list(s.PHOTO_ROAD_RGBA).index("paved") != 4 or len(s.GOLF_PHOTO) != 4:
            raise ValueError("paintlist: the native paint expects paved as the fifth road kind and four golf layers")
        _STYLE[:] = [key, fast._ef.PaintStyle(colors, tone, [float(v) for v in c])]
        got = _STYLE
    return got[1]


def _polys(rings):
    """earthfast.PaintPolys of [(k, 2) metres]."""
    return _make(_polys_spec(rings))


def _polys_spec(rings):
    """_polys' constructor spec ("PaintPolys", [xy, offsets])."""
    off = np.zeros(len(rings) + 1, np.int64)
    if rings:
        off[1:] = np.cumsum([len(r) for r in rings])
        xy = np.ascontiguousarray(np.concatenate([np.asarray(r, np.float64).reshape(-1, 2) for r in rings]))
    else:
        xy = np.zeros((0, 2))
    return ("PaintPolys", [xy, off])


def lonlat_polys(frame, pads):
    """[(lat (k,), lon (k,))] pads in frame's metres (one conversion: point by point frame.geodetic_to_enu's), as
    [(k, 2)]."""
    pads = [(np.asarray(la, np.float64).ravel(), np.asarray(lo, np.float64).ravel()) for la, lo in pads]
    pads = [(la, lo) for la, lo in pads if len(la)]
    if not pads:
        return []
    la = np.concatenate([p[0] for p in pads])
    lo = np.concatenate([p[1] for p in pads])
    e, n, _ = frame.geodetic_to_enu(la, lo, np.zeros(len(la)))
    e, n = np.asarray(e, np.float64), np.asarray(n, np.float64)
    out, k = [], 0
    for p in pads:
        m = len(p[0])
        out.append(np.c_[e[k:k + m], n[k:k + m]])
        k += m
    return out


def pad_polys(frame, pads):
    """earthfast.PaintPolys of [(lat, lon)] pads (surface._square_pads'), or None."""
    rings = lonlat_polys(frame, pads) if pads else []
    return _polys(rings) if rings else None


# --- the parts of a cell set ----------------------------------------------------------------------------------------

def _cell_key(f):
    """A layer's identity in the keys: its cell, layer and data versions (mapgeom.Features._bnge_cell) and lane
    revision, else the object itself (kept alive by the set while it is kept)."""
    if f is None:
        return None
    cid = f.__dict__.get("_bnge_cell")
    rev = getattr(f, "_road_revision", 0)
    if cid is None:
        return ("id", id(f), rev)
    import roadsurf
    return (cid, rev, roadsurf.layer_tags(f) if len(cid) > 1 and cid[1] == "roads" and len(f) else ())


def _callable_key(fn):
    return roadsmod._callable_key(fn)


def _make(spec):
    """The native objects of a part's spec: ("Paint...", [constructor arrays]), or a list of specs, or None."""
    if spec is None:
        return None
    if isinstance(spec, list):
        return [_make(x) for x in spec]
    return getattr(fast._ef, spec[0])(*spec[1])


def _encode(spec):
    """A spec as bytes (an npz of its arrays and the layout in JSON): a part kept in the revisit cache."""
    import io
    import json
    arrays = []

    def lay(x):
        if x is None:
            return None
        if isinstance(x, list):
            return [lay(v) for v in x]
        k = len(arrays)
        arrays.extend(np.ascontiguousarray(v) for v in x[1])
        return {"c": x[0], "a": list(range(k, len(arrays)))}
    layout = lay(spec)
    buf = io.BytesIO()
    np.savez(buf, layout=np.frombuffer(json.dumps(layout).encode(), np.uint8),
             **{f"a{k}": v for k, v in enumerate(arrays)})
    return buf.getvalue()


def _decode(data):
    """_encode's spec back."""
    import io
    import json
    with np.load(io.BytesIO(data), allow_pickle=False) as z:
        layout = json.loads(bytes(z["layout"]).decode())

        def unlay(x):
            if x is None:
                return None
            if isinstance(x, list):
                return [unlay(v) for v in x]
            if x["c"] not in ("PaintRoads", "PaintPolys", "PaintFields"):
                raise ValueError(f"paint list: unknown part {x['c']!r}")
            return (x["c"], [z[f"a{k}"] for k in x["a"]])
        return unlay(layout)


KIND = "paint"              # (the revisit cache's kind: a cell set's part, keyed by its cells' data and the code)
SHARE_WAIT_S = 30.0         # a part another process is making is waited for this long at most ...
SHARE_STALE_S = 120.0       # ... and its marker counts as left behind past this age
SHARE_POLL_S = 0.05
SHORT_KEEP_S = 10.0         # a part made while its cells' data were short is used again this long (_Set._short)
SHORTS_KEPT = 16            # ... at most this many of them per cell set


class _Set:
    """The parts of one (frame, road cells, lot cells, land cover, widths): each made on first use, by one thread of
    this process (the others wait for it) and, where the cells are the map data's (disk_key), by one process of the
    server: kept in the revisit cache for the others (the far-tile processes paint side by side, each the same cells'
    first squares at a teleport: ten processes made every part ten times) and for the next visit."""

    def __init__(self, frame, roads, lots, urban, widths, disk_key=None):
        self.frame, self.urban, self.widths = frame, urban, widths
        # (a set of the map data's layers holds none of them: each call brings its own, view(); one keyed by its layers'
        # identity holds them, so that the ids stay theirs. Keeping them made the far-tile processes hold up to
        # PARTS_KEPT sets' layers past the map data's own memory limit)
        self.held = (list(roads), list(lots)) if disk_key is None else None
        self.disk_key = disk_key
        self.parts = OrderedDict()
        self.corner_groups = OrderedDict()
        self.lock = threading.Lock()
        self.pending = {}
        self.shorts = OrderedDict()    # name -> (made at, part, faults, lidar faults): parts made short

    def part(self, name, build):
        """A part (its native objects), made by build() -> (spec, clean) on first use; kept only when made without a
        fault (one made short is used again for SHORT_KEEP_S, its faults told to each photo painted with it)."""
        with self.lock:
            got = self.parts.get(name)
            if got is not None:
                return got
            fut = self.pending.get(name)
            owner = fut is None
            if owner:
                fut = self.pending[name] = Future()
        if not owner:
            got = fut.result()
            if got is not None:
                return got
            got = self._short(name)
            return got if got is not None else _built(build)[0]
        got = self._short(name)
        if got is not None:
            with self.lock:
                self.pending.pop(name, None)
            fut.set_result(None)
            return got
        n0, l0 = revisit.thread_faults(), revisit.thread_lidar_faults()
        try:
            got, clean = self._shared(name, build)
        except BaseException as e:
            with self.lock:
                self.pending.pop(name, None)
            fut.set_result(None)
            raise e
        with self.lock:
            self.pending.pop(name, None)
            if clean:
                self.parts[name] = got
                self.shorts.pop(name, None)
                while len(self.parts) > PARTS_PER_SET:
                    self.parts.popitem(last=False)
            else:
                self._keep_short(name, got, revisit.thread_faults() - n0, revisit.thread_lidar_faults() - l0)
        fut.set_result(got if clean else None)
        return got

    def _short(self, name):
        """A part made short less than SHORT_KEEP_S ago (its faults told to this photo's build: not kept), or None.
        On a first visit the cells' data come in over minutes; a part made meanwhile was made again for every
        square (as the per-square Python did), by every far-tile process."""
        with self.lock:
            self._expire_shorts()
            got = self.shorts.get(name)
        if got is None:
            return None
        revisit.carry(max(got[2], 1 if not got[3] else 0), got[3])
        _count("reused_short")
        return got[1]

    def _keep_short(self, name, got, faults, lidar):
        """(under the lock) A part made short kept for SHORT_KEEP_S: the expired ones dropped, at most SHORTS_KEPT."""
        self._expire_shorts()
        self.shorts.pop(name, None)
        self.shorts[name] = (time.monotonic(), got, faults, lidar)
        while len(self.shorts) > SHORTS_KEPT:
            self.shorts.popitem(last=False)

    def _expire_shorts(self):
        """(under the lock) The short parts older than SHORT_KEEP_S dropped (the oldest first: insertion order)."""
        now = time.monotonic()
        while self.shorts:
            k, v = next(iter(self.shorts.items()))
            if now - v[0] <= SHORT_KEEP_S:
                break
            del self.shorts[k]

    def _shared(self, name, build):
        """(native objects, clean): the revisit cache's part when another process (or visit) made it, else build()'s,
        kept there when clean. While another process makes it (its marker), waited for."""
        k = None if self.disk_key is None or revisit.CACHE_DIR is None or not revisit.ON else \
            revisit.key(KIND, self.disk_key, name)
        if k is None:
            return _built(build)
        got = _load(k)
        if got is not None:
            _count("loaded")
            return got, True
        marker = _claim(k)
        if marker is None:              # (another process makes it: its result, or ours if it ends without one)
            got = _await(k)
            if got is not None:
                _count("waited")
                return got, True
            marker = _claim(k, force=True)
        try:
            spec, clean, objs = _built(build, spec_too=True)
            if clean:
                try:
                    revisit.store(revisit.CACHE_DIR, KIND, k, {"part.npz": _encode(spec)})
                except Exception as e:  # (not kept rather than no paint)
                    log.debug("paint list %s not kept: %s: %s", name, type(e).__name__, e)
            return objs, clean
        finally:
            _release(marker)

    def view(self, roads, lots):
        """What the part makers read: this set's frame, land cover and widths with this call's layers."""
        from types import SimpleNamespace
        return SimpleNamespace(frame=self.frame, roads=list(roads or ()), lots=list(lots or ()), urban=self.urban,
                               widths=self.widths)

    def corners(self, x0, y0, size, roads):
        """The corner pads round the square (surface.corner_pads), per rounded bounds group (roadcorners' own)."""
        bounds = (x0 - 4.0, y0 - 4.0, x0 + size + 4.0, y0 + size + 4.0)
        key = group(x0, y0, size)
        with self.lock:
            got = self.corner_groups.get(key)
            if got is not None:
                self.corner_groups.move_to_end(key)
                return got
        name = ("corners",) + key
        got = self._short(name)
        if got is not None:
            return got
        n0, l0 = revisit.thread_faults(), revisit.thread_lidar_faults()
        got, clean = self._shared(name, lambda: _clean(lambda: _build_corners(self.view(roads, ()), bounds)))
        with self.lock:
            if clean:
                self.corner_groups[key] = got
                self.shorts.pop(name, None)
                while len(self.corner_groups) > CORNER_GROUPS_KEPT:
                    self.corner_groups.popitem(last=False)
            else:
                self._keep_short(name, got, revisit.thread_faults() - n0, revisit.thread_lidar_faults() - l0)
        return got


STATS = {"made": 0, "short": 0, "reused_short": 0, "loaded": 0, "waited": 0, "made_s": 0.0}
_STATS_AT = [0.0]
STATS_LOG = bool(os.environ.get("BNGE_PAINT_STATS"))     # (measuring: this process's counts every 30 s at INFO)


def _count(what, n=1):
    with _LOCK:
        STATS[what] += n
        if STATS_LOG and time.monotonic() - _STATS_AT[0] > 30.0:
            _STATS_AT[0] = time.monotonic()
            log.info("paint lists (process %d): %s", os.getpid(), STATS)


def _built(build, spec_too=False):
    """build()'s (native objects, clean) (spec_too: (spec, clean, objects)), counted."""
    t0 = time.perf_counter()
    spec, clean = build()
    _count("made_s", time.perf_counter() - t0)
    _count("made" if clean else "short")
    objs = _make(spec)
    return (spec, clean, objs) if spec_too else (objs, clean)


def group(x0, y0, size):
    """The group of a photo square (x0, y0, size): its bounds and 4 m (surface.corner_pads') rounded out to
    BOUNDS_ROUND_M, roadcorners' corner groups; the square lies inside it. The parts that depend on where the square is
    are made per group (squares of a group share them)."""
    import math
    r = BOUNDS_ROUND_M
    b = (x0 - 4.0, y0 - 4.0, x0 + size + 4.0, y0 + size + 4.0)
    return (math.floor(b[0] / r) * r, math.floor(b[1] / r) * r, math.ceil(b[2] / r) * r, math.ceil(b[3] / r) * r)


def _load(k):
    try:
        hit = revisit.load(revisit.CACHE_DIR, KIND, k)
        return None if hit is None else _make(_decode(hit[0]["part.npz"]))
    except Exception as e:      # (made again)
        log.debug("paint list %s unreadable: %s: %s", k, type(e).__name__, e)
        return None


def _marker_path(k):
    return os.path.join(revisit.CACHE_DIR, "paintmaking", k)


def _claim(k, force=False):
    """This process's marker that it makes part k (its path; "" without one), or None when another process's fresh
    marker is there."""
    path = _marker_path(k)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not force:
            try:
                os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                return path
            except FileExistsError:
                if time.time() - os.path.getmtime(path) <= SHARE_STALE_S:
                    return None
        with open(path, "w"):
            pass
        return path
    except OSError:
        return ""               # (no marker: made here all the same)


def _release(path):
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


def _await(k):
    """Part k once the process making it kept it, or None (it ended without keeping it, or SHARE_WAIT_S passed)."""
    end = time.monotonic() + SHARE_WAIT_S
    path = _marker_path(k)
    while time.monotonic() < end:
        if revisit.has(revisit.CACHE_DIR, KIND, k) or not os.path.exists(path):
            return _load(k)     # (its marker gone: kept just before, or not at all)
        time.sleep(SHARE_POLL_S)
    return None


def _clean(build):
    """(build(), whether it read nothing short): build() in a revisit scope of its own (its faults still reach the
    scopes round it), clean when that scope is complete (no fault of this thread's, nor of a thread no scope covers:
    a Surface's background fetch failing meanwhile)."""
    with revisit.scope() as b:
        got = build()
    return got, b.complete


def cell_set(frame, roads, lots=(), urban=None, widths=None):
    """The _Set of these cells (kept for the next squares of the same cells)."""
    import busstations
    roads_k, lots_k = tuple(_cell_key(f) for f in roads), tuple(_cell_key(f) for f in lots)
    passages = tuple(sorted(busstations.PASSAGE_IDS))
    key = (frame.lat0, frame.lon0, frame.h0, roads_k, lots_k, _callable_key(urban), _callable_key(widths),
           len(passages))
    with _LOCK:
        got = _SETS.get(key)
        if got is not None:
            _SETS.move_to_end(key)
            return got
    # (shared with the server's other processes only when every layer is the map data's own: its cell, layer and data
    # versions say what it holds; the land cover and the measured widths are the server's, from the same cache)
    disk = None
    if all(k is None or k[0] != "id" for k in roads_k + lots_k):
        disk = (repr(float(frame.lat0)), repr(float(frame.lon0)), repr(float(frame.h0)), roads_k, lots_k,
                urban is not None, widths is not None, passages)
    with _LOCK:
        got = _SETS.get(key)
        if got is None:
            got = _SETS[key] = _Set(frame, roads, lots, urban, widths, disk)
            while len(_SETS) > PARTS_KEPT:
                _SETS.popitem(last=False)
        else:
            _SETS.move_to_end(key)
    return got


def clear():
    """Every kept part dropped (tests; a memory shed)."""
    with _LOCK:
        _SETS.clear()


def _build_roads(s, g):
    """surface._road_segments' per-feature work for the cells' features that can reach a square of group g (x0, y0, x1,
    y1: the squares' bounds rounded out to BOUNDS_ROUND_M, group(); earthfast.PaintRoads): each feature once (its first
    copy in the cells' order, whether in the group or not, as the per-square loop's), its ground parts placed
    (roads.placement_context of all the cells) in the frame's metres, tapered half widths per vertex (each joint's taper
    from its own cell: roads.joint_desc), its box; and the gores' paved wedges (surface._gore_fills of the roads within
    300 m of the group: the per-square loop's within 300 m of the square, a subset). A feature the group's box test
    leaves out fails every square's of the group (the square lies inside it): the same segments, in the same order and
    with the same road numbers, as the per-square loop's. (Whole cells were made at first: a far square of 1-2 km
    across the corner of a cell set of four Manhattan cells made all four's roads for it.)"""
    import surface as sf

    def build():
        frame, feats_list, urban_fn, widths = s.frame, s.roads, s.urban, s.widths
        kinds = list(sf.PHOTO_ROAD_RGBA)
        v, hp, ring_off, ring_kind, ring_taper, ring_ghost = [], [], [0], [], [], []
        feat_ring, feat_box, feat_m, feat_hwm = [0], [], [], []
        seen = set()
        copies = roadsmod.copies_of(feats_list)
        placement = roadsmod.placement_context(feats_list, urban_fn, widths)

        def taper_of(f):
            return roadsmod.road_tapers(f, urban_fn, widths)
        for feats in feats_list:
            if feats is None or len(feats) == 0:
                continue
            specs = roadsmod.road_specs(feats, urban_fn, widths)
            box = sf._feature_boxes(feats, frame)
            ids = feats.ids.tolist()
            for i in range(len(feats)):
                fid = str(ids[i])
                sp = specs[i]
                if sp is None or fid in seen or sp.kind not in sf.PHOTO_ROAD_RGBA:
                    continue
                kname = "concrete" if sp.kind == "paved" and sp.surf == "concrete" else sp.kind
                seen.add(fid)
                m = sp.width / 2
                if box[i, 1] < g[0] - m or box[i, 0] > g[2] + m or box[i, 3] < g[1] - m or box[i, 2] > g[3] + m:
                    continue
                ghost = bool(not roadsmod.has_sidewalks(sp) and sp.kind == "paved")
                tp = None
                for ring in roadsmod.ground_parts(feats, i):
                    ring = placement.ring(frame, feats, i, ring, sp, fid)
                    e, n = mapgeom.enu_xy(feats, frame, ring)
                    e, n = np.asarray(e, np.float64), np.asarray(n, np.float64)
                    if len(e) < 2:
                        continue
                    if tp is None:
                        tp = roadsmod.joint_desc(copies.get(fid, [(feats, i)]), taper_of)
                    ws = roadsmod.taper_widths(sp.width, tp, ring[:, 0], ring[:, 1])
                    v.append(np.c_[e, n])
                    if ws is None:
                        hp.append(np.full(len(e), np.nan))
                    else:
                        hp.append(np.asarray(ws / 2 + sf.PHOTO_ROAD_EDGE_M, np.float64))
                    ring_off.append(ring_off[-1] + len(e))
                    ring_kind.append(kinds.index(kname))
                    ring_taper.append(ws is not None)
                    ring_ghost.append(ghost)
                feat_ring.append(len(ring_kind))
                feat_box.append(box[i])
                feat_m.append(sp.width / 2)
                feat_hwm.append(sp.width / 2 + sf.PHOTO_ROAD_EDGE_M)
        gores = sf._gore_fills(frame, feats_list, (g[0] - 300, g[1] - 300, g[2] + 300, g[3] + 300), urban_fn, widths)
        gore_rings = [np.asarray(mid, np.float64)[:, :2] for _, mid in gores]
        gore_hw = [np.asarray(hw_, np.float64) for hw_, _ in gores]
        goff = np.zeros(len(gores) + 1, np.int64)
        goff[1:] = np.cumsum([len(r) for r in gore_rings])
        cat = (lambda a, shape: np.ascontiguousarray(np.concatenate(a), np.float64) if a else np.zeros(shape))
        return ("PaintRoads", [
            cat(v, (0, 2)), cat(hp, (0,)), np.asarray(ring_off, np.int64), np.asarray(ring_kind, np.uint8),
            np.asarray(ring_taper, bool), np.asarray(ring_ghost, bool), np.asarray(feat_ring, np.int64),
            np.asarray(feat_box, np.float64).reshape(-1, 4), np.asarray(feat_m, np.float64),
            np.asarray(feat_hwm, np.float64), cat(gore_rings, (0, 2)), cat(gore_hw, (0,)), goff])
    return _clean(build)


def _build_islands(s, g):
    """The roundabouts' full footprints (surface._roundabout_footprints of the cells' roads within ISLAND_REACH_M of
    group g: the per-square code's within it of the square, a subset)."""
    import surface as sf
    return _clean(lambda: _polys_spec(sf._roundabout_footprints(sf._roads_list_enu(s.frame, s.roads, s.urban,
                                                                                   s.widths, g))))


def _build_corners(s, bounds):
    """surface.corner_pads of the square bounds (roadcorners.pads and curb_pads: the corner group of its rounded
    bounds), in metres."""
    import roadcorners
    import surface as sf
    if not s.roads:
        return _polys_spec([])
    pads = (roadcorners.pads(s.roads, s.urban, s.widths, s.frame, bounds=bounds, grow=sf.PHOTO_ROAD_EDGE_M)
            + roadcorners.curb_pads(s.roads, s.urban, s.widths, s.frame, bounds=bounds, grow=sf.PHOTO_ROAD_EDGE_M))
    return _polys_spec(lonlat_polys(s.frame, pads))


def _build_lots(s):
    """The parking lots (surface.lot_pads) and the golf layers (surface._golf_rings per GOLF_PHOTO row), in metres."""
    import surface as sf

    def build():
        frame = s.frame
        lots = _polys_spec(lonlat_polys(frame, sf.lot_pads(s.lots)))
        golf = []
        for classes, _ in sf.GOLF_PHOTO:
            rings = sf._golf_rings(s.lots, classes)
            golf.append(_polys_spec(lonlat_polys(frame, [(r[:, 1], r[:, 0]) for r in rings])) if rings else None)
        return [lots, golf]
    return _clean(build)


def _build_fields(s):
    """The farmland rings with their furrows' phase, direction and centre (surface.photo_fields), and the cells' dirt
    roads' ground parts with their cleared width (surface._dirt_road_mask)."""
    import surface as sf

    def build():
        frame = s.frame
        rings, params = [], []
        for feats in s.lots:
            if feats is None or len(feats) == 0:
                continue
            sel = sf._farmland(feats)
            if sel is None:
                continue
            ids = getattr(feats, "ids", None)
            for i in np.flatnonzero(sel & (np.asarray(feats.kind) == 3)):
                parts = feats.parts(int(i))
                if not parts or not parts[0] or len(parts[0][0]) < 3:
                    continue
                ring = np.asarray(parts[0][0], np.float64)
                e, n = mapgeom.enu_xy(feats, frame, ring)
                xy = np.column_stack([np.asarray(e, np.float64), np.asarray(n, np.float64)])
                u = sf._long_axis(xy)
                centre = xy.mean(axis=0)
                rings.append(xy)
                params.append((sf._field_phase(None if ids is None else ids[int(i)], xy), -float(u[1]), float(u[0]),
                               float(centre[0]), float(centre[1])))
        lines, wsum = [], []
        if rings:               # (the dirt roads only matter under a field)
            for feats in s.roads:
                if feats is None or len(feats) == 0:
                    continue
                specs = roadsmod.road_specs(feats, s.urban, s.widths)
                for i, sp in enumerate(specs):
                    if sp is None or sp.kind != "dirt":
                        continue
                    for ring in roadsmod.ground_parts(feats, i):
                        ring = np.asarray(ring, np.float64)
                        if len(ring) < 2:
                            continue
                        e, n = mapgeom.enu_xy(feats, frame, ring)
                        lines.append(np.c_[np.asarray(e, np.float64), np.asarray(n, np.float64)])
                        wsum.append(sp.width + 2 * sf.PHOTO_ROAD_EDGE_M + 2 * sf.FARM_ROAD_CLEAR_M)
        off = np.zeros(len(rings) + 1, np.int64)
        off[1:] = np.cumsum([len(r) for r in rings])
        loff = np.zeros(len(lines) + 1, np.int64)
        loff[1:] = np.cumsum([len(r) for r in lines])
        cat = (lambda a: np.ascontiguousarray(np.concatenate(a), np.float64) if a else np.zeros((0, 2)))
        return ("PaintFields", [cat(rings), off, np.asarray(params, np.float64).reshape(-1, 5), cat(lines), loff,
                                np.asarray(wsum, np.float64)])
    return _clean(build)


# --- painting ------------------------------------------------------------------------------------------------------

ALL = frozenset(("fields", "golf", "lots", "pads", "roads"))


def paint(rgb, frame, x0, y0, size, roads, lots=(), urban=None, widths=None, pads=(), simple=False, steps=ALL):
    """surface.paint_square's paint of the photo rgb (h x w x 3, row 0 north, over [x0, x0 + size]² of frame) with
    the road Features roads and the lot Features lots of its cells and the pads [(lat, lon)], in one native call
    (earthfast.paint_photo); steps: the parts painted (surface's photo_* helpers each paint one). A new array."""
    roads, lots = list(roads or ()), list(lots or ())
    s = cell_set(frame, roads, lots, urban, widths)
    v = s.view(roads, lots)
    kw = {}
    if not simple:
        if lots and "fields" in steps:
            kw["fields"] = s.part("fields", lambda: _build_fields(v))
        if lots and ("golf" in steps or "lots" in steps):
            lt, golf = s.part("lots", lambda: _build_lots(v))
            if "golf" in steps and any(g is not None for g in golf):
                kw["golf"] = golf
            if "lots" in steps and len(lt):
                kw["lots"] = lt
        if pads and "pads" in steps:
            kw["pads"] = pad_polys(frame, pads)
    has_roads = bool(roads) and "roads" in steps
    if has_roads:
        g = group(x0, y0, size)
        kw["roads"] = s.part(("roads",) + g, lambda: _build_roads(v, g))
        if not simple:
            kw["islands"] = s.part(("islands",) + g, lambda: _build_islands(v, g))
            kw["corners"] = s.corners(x0, y0, size, roads)
    return fast._ef.paint_photo(np.ascontiguousarray(rgb, np.uint8), float(x0), float(y0), float(size), style(),
                                has_roads=has_roads, simple=bool(simple), **kw)


def road_segments(frame, x0, y0, size, w, h, roads, urban=None, widths=None):
    """surface._road_segments from the cells' kept carriageways: (segs, hws, kind, road, curbless) or None."""
    s = cell_set(frame, list(roads or ()), (), urban, widths)
    v = s.view(roads, ())
    g = group(x0, y0, size)
    return s.part(("roads",) + g, lambda: _build_roads(v, g)).segments(float(x0), float(y0), float(size), int(w),
                                                                       int(h))
