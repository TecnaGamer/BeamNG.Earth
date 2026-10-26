"""Physics-block builds in a process of their own (2026-10-12).

The server is one Python process: its threads share one interpreter lock, so while three far-tile workers
built, a physics block took 3-10x longer than alone (land cover 1.4 s instead of 0.1 s), too long to follow a
car at 58 m/s, where the 512² block moves every 2.2 s. This process builds the blocks the follow and the
prefetch ask for on a core of its own. It keeps its own DEM reader, road plans (grading.Grader per anchor),
street widths, height chunks and land-cover reader over the same disk caches. All of them are deterministic,
so a block built here is the one the server would build (the far tiles, graded in the server process, agree
with it).

init(...) runs once in the process (ProcessPoolExecutor initializer); build(...), base_entry(...), base_photo(...) and
far_geometry(...) (the far-tile workers, the same state) are its jobs. warm_q (a multiprocessing queue) carries the road cells to plan ahead of the player: (anchor id, lat,
lon, [mapcells.CellKey]), the latest list replacing the earlier one.
"""
import logging
import os
import queue
import threading
import time

import numpy as np

import dem as demmod
import farfield
import furniture
import geodesy
import grading as gradingmod
import heightmap
import mapdata as mapdatamod
import netfetch
import plancache
import roadwidth as roadwidthmod
import seacarve as seacarvemod
import staging
import surface as surfacemod
import terraincache
import xproc

log = logging.getLogger("earthd.blockworker")

_s = None       # this process's state (init)


class _State:
    def __init__(self, cache_dir, offline, roads, grading, road_widths, surface, airports=False, sea=False,
                 gas_stations=False):
        self.cache_dir = cache_dir
        import revisit
        revisit.CACHE_DIR = cache_dir
        self.imagery = None         # (its settings, imagery.Imagery from the disk alone): far_tile's textures
        net = netfetch.Fetcher(offline=offline)
        self.dem = demmod.Dem(os.path.join(cache_dir, "dem"), fetcher=net)
        # runways graded in (--airports on): the copy the server loaded, never refreshed here
        self.airports = load_airports(cache_dir, self.dem) if airports else None
        md = mapdatamod.MapData(cache_dir, fetcher=net) if (roads or surface or sea) else None
        # the sea's and the lakes' floors carved in (--sea on)
        self.sea = seacarvemod.SeaCarver(md, cache_dir, dem=self.dem) if sea else None
        self.md = md if roads else None
        self.grading = bool(grading) and self.md is not None
        self.widths = (roadwidthmod.RoadWidths(
            lambda key: md.cell(key, layers=("buildings",), kind="demand").layers.get("buildings"),
            roadwidthmod.store_dir(cache_dir, md)).get
            if road_widths and self.md is not None else None)
        self.surface = surfacemod.Surface(md) if surface else None
        if self.surface is not None:    # gas station forecourts painted asphalt (--gas-stations on)
            self.surface.gas_pads = bool(gas_stations)
        self.graders = {}           # anchor id -> grading.Grader
        self.chunks = {}            # anchor id -> heightmap.ChunkCache
        self.bridges = {}           # anchor id -> roads.BridgeProfiles (roads_tile)
        self.tunnels = {}           # anchor id -> roads.TunnelProfiles (roads_tile, the block's holes)
        self.lock = threading.Lock()
        self.want = []              # (grader, cell key) to plan, most urgent first
        self.cv = threading.Condition()

    def anchor(self, aid, lat, lon):
        """(Grader or None, ChunkCache) of an anchor; only the last few anchors are kept."""
        with self.lock:
            if aid not in self.chunks:
                frame = geodesy.EnuFrame(lat, lon)
                self.chunks[aid] = heightmap.ChunkCache()
                self.graders[aid] = (gradingmod.Grader(
                    frame, self.md, lambda la, lo: self.dem.sample(la, lo, gradingmod.PROFILE_ZOOM), self.widths)
                    if self.grading else None)
                if self.graders[aid] is not None:
                    self.graders[aid].wet = self.sea.depth if getattr(self, "sea", None) is not None else None
                    self.graders[aid].dem = self.dem          # (its urgent lane: _plan_missing)
                    self.graders[aid].shared = plancache.for_grader(self.cache_dir, self.graders[aid], self.widths,
                                                                   self.graders[aid].wet)
                for old in sorted(self.chunks)[:-ANCHORS_KEPT]:
                    del self.chunks[old], self.graders[old]
            return self.graders[aid], self.chunks[aid]


ANCHORS_KEPT = 2
SIGNALS_MARGIN_M = 60.0         # m around a road tile its junctions' mapped traffic signals are taken from
WATER_BED_M = 0.05           # ground carved deeper than this under the water gets the bed layer (MUD)


def load_airports(cache_dir, dem):
    """An airportgrade.AirportGrader over the OurAirports copy in the cache (no download), or None."""
    import airportgrade
    import airports as airportsmod
    try:
        db = airportsmod.Store(cache_dir, netfetch.Fetcher(offline=True)).load(
            max_age=float("inf"))
    except (airportsmod.AirportsUnavailable, OSError, ValueError) as e:
        log.warning("airports: no runway grading (%s)", e)
        return None
    return airportgrade.AirportGrader(db, dem)


def _rss_mb():
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1e6
    except (OSError, ValueError):
        return 0.0


def _trace_memory(every_s=60.0, top=10):
    """BNGE_TRACEMALLOC: log this worker's resident size, what Python and numpy hold of it and its top allocation
    sites every every_s (the rest is native: DuckDB, the allocator's free lists)."""
    import tracemalloc
    tracemalloc.start(1)

    def loop():
        log = logging.getLogger("earthd.worker")
        while True:
            time.sleep(every_s)
            snap = tracemalloc.take_snapshot()
            total = sum(st.size for st in snap.statistics("filename"))
            log.info("memory pid %d: rss %.0f MB, traced %.0f MB; top: %s", os.getpid(), _rss_mb(), total / 1e6,
                     "; ".join(f"{os.path.basename(st.traceback[0].filename)}:{st.traceback[0].lineno} "
                               f"{st.size / 1e6:.0f} MB" for st in snap.statistics("lineno")[:top]))
    threading.Thread(target=loop, daemon=True, name="trace-mem").start()


def init(cache_dir, offline, roads, grading, road_widths, surface, warm_q, log_level=logging.INFO, airports=False,
         sea=False, nice=0, roads_in_ground=None, gas_stations=False):
    """nice: this worker's niceness on top of the server's (earthd NICE_*): the terrain blocks' workers keep the
    server's own, the road tiles' and the far field's run below them, so the ground under the car wins the server's
    few cores (2026-10-19: at 180 mph the next block waited 6 s behind the road tiles ahead and a ball fell off the
    block's edge)."""
    global _s
    # A helper may serve clients with different choices. Never use its own launcher
    # preference for a client's job, or mix painted and mesh roads within one world.
    if roads_in_ground is not None:
        import roads as roadsmod
        roadsmod.ROADS_IN_GROUND = roads_in_ground
    import budget
    try:
        budget.lower_priority(nice, worker=True)    # (Windows: the server's class, as inherited)
    except (OSError, AttributeError):
        pass
    logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    budget.apply()              # (this process's caches: smaller on a small machine)
    if os.environ.get("BNGE_TRACEMALLOC"):      # debug: this worker's memory (earthd's trace_mem)
        _trace_memory()
    import sampler
    sampler.maybe_start(f".{os.getpid()}")      # BNGE_SAMPLE: this worker's own sampling profile (debugging)
    xproc.PRIORITY = nice
    _s = _State(cache_dir, offline, roads, grading, road_widths, surface, airports, sea, gas_stations)
    threading.Thread(target=_watch_parent, args=(os.getppid(),), daemon=True, name="parent-watch").start()
    threading.Thread(target=_read_warm, args=(warm_q,), daemon=True, name="warm-read").start()
    threading.Thread(target=_warm_loop, daemon=True, name="roads-ahead").start()


def _watch_parent(ppid):
    """Exit with the server: a killed server (SIGTERM) left its block processes running."""
    while os.getppid() == ppid:
        time.sleep(PARENT_POLL_S)
    os._exit(0)


PARENT_POLL_S = 2.0


def _read_warm(warm_q):
    while True:
        try:
            aid, lat, lon, keys = warm_q.get()
        except (EOFError, OSError):
            return
        try:
            while True:         # only the latest list matters
                aid, lat, lon, keys = warm_q.get_nowait()
        except queue.Empty:
            pass
        g, _ = _s.anchor(aid, lat, lon)
        if g is None:
            continue
        with _s.cv:
            _s.want = [(g, k) for k in keys]
            _s.cv.notify()


def _warm_loop():
    while True:
        with _s.cv:
            job = None
            while job is None:
                job = next(((g, k) for g, k in _s.want if not g.planned(k)), None)
                if job is None:
                    _s.cv.wait()
        try:
            t0 = time.perf_counter()
            job[0].warm(job[1])
            log.debug("road plans ahead: %s in %.2f s", job[1], time.perf_counter() - t0)
        except Exception:
            log.exception("road plans ahead for %s", job[1])
            with _s.cv:
                _s.want = [w for w in _s.want if w[1] != job[1]]


def ground(frame, la, lo, z):
    """The DEM heights z at (la, lo) shaped before the road grading: runways graded in (--airports on),
    the sea floor carved in (--sea on)."""
    if _s.airports is not None:
        z = _s.airports.apply(la, lo, z)
    if _s.sea is not None:
        z = _s.sea.carve(la, lo, z)
    return z


def _take_pads(fuel_pads):
    """The server's gas station pads for this job's area (earthd.Server.block_fuel_pads) as this process's Surface's:
    only these (none when the server's gas stations are switched off)."""
    if getattr(_s, "surface", None) is not None and hasattr(_s.surface, "clear_fuel_pads"):
        _s.surface.clear_fuel_pads()
        _s.surface.add_fuel_pads(fuel_pads or ())


def build(aid, lat, lon, hm_zoom, q, cx, cy, size, out_dir, name, ter_name=None, fuel_pads=None):
    """Build and write the block (heightmap PNG name in out_dir, its surface masks beside it, and the
    .ter ter_name if given). fuel_pads: the gas station pads around it (the server's: _take_pads). Returns the info
    dict of earthd's make_heightmap (without seconds / zoom / base)."""
    _take_pads(fuel_pads)
    t0 = time.perf_counter()
    g, chunks = _s.anchor(aid, lat, lon)
    frame = geodesy.EnuFrame(lat, lon)
    keep = {}
    grade_s = [0.0]

    def elev(la, lo):
        z = ground(frame, la, lo, _s.dem.sample(la, lo, hm_zoom))
        if g is None:
            return z
        t1 = time.perf_counter()
        z = g.apply(la, lo, z, gradingmod.ROAD_LIFT)
        grade_s[0] += time.perf_counter() - t1
        return z
    import roads as roadsmod
    raw = lambda la, lo: ground(frame, la, lo, _s.dem.sample(la, lo, hm_zoom))     # noqa: E731
    gen = elev
    if ter_name is not None and getattr(_s, "md", None) is not None:
        tun = tunnel_profiles(aid, frame, elev, g)
        half = size * heightmap.SQUARE / 2
        cell_fn = lambda key: _s.md.cell(key, layers=("roads",), kind="demand").layers.get("roads")   # noqa: E731

        def gen(la, lo):        # the ground over the low arches raised over their vaults (roads.arch_raise)
            return roadsmod.block_arch_raise(tun, cell_fn, frame, la, lo, elev(la, lo),
                                             (cx - half, cy - half, cx + half, cy + half))
    info = heightmap.generate(frame, gen, os.path.join(out_dir, name), cx, cy, q=q, size=size, keep=keep,
                              chunks=chunks)
    stages = {"heights": time.perf_counter() - t0, "grading": grade_s[0]}
    idx = None
    if _s.surface is not None:
        t1 = time.perf_counter()
        classes, complete = _s.surface.classes(keep["lat"], keep["lon"])
        idx = surfacemod.classify(classes, keep["u"], heightmap.SQUARE, keep["lat"], keep["lon"])
        if _s.sea is not None:          # under water: the bed (no grass growing through the surface)
            idx[_s.sea.depth(keep["lat"], keep["lon"]) > WATER_BED_M] = surfacemod.MUD
        road_feats, roads_complete = _s.surface.roads(keep["lat"], keep["lon"])
        stages["landcover"] = time.perf_counter() - t1
        t1 = time.perf_counter()
        if road_feats:
            idx = surfacemod.paint_roads(idx, keep["lat"], keep["lon"], road_feats, heightmap.SQUARE,
                                         _s.surface.urban, _s.widths, _open_tunnels(g, keep))
        idx = (surfacemod.paint_lots(idx, keep["lat"], keep["lon"], _s.surface.lots(keep["lat"], keep["lon"]))
            if hasattr(_s.surface, "lots") else idx)
        if hasattr(_s.surface, "fuel_pads"):    # gas station forecourts (--gas-stations on)
            idx = surfacemod.paint_pads(idx, keep["lat"], keep["lon"], _s.surface.fuel_pads(keep["lat"], keep["lon"]))
        stages["roadPaint"] = time.perf_counter() - t1
        info["layers"] = surfacemod.write_masks(idx[::-1], out_dir, name[:-4])
        info["surfaceComplete"] = complete and roads_complete
    if ter_name is not None:
        t1 = time.perf_counter()
        idx = _tunnel_holes(aid, frame, elev, keep, cx, cy, idx, info, g)
        info["ter"], mats = surfacemod.write_ter(out_dir, ter_name, keep["qgrid"], idx)
        if mats is not None:          # the .ter's own material list (MATERIALS indices)
            info["mats"] = mats
        stages["ter"] = time.perf_counter() - t1
    info["stages"] = stages
    return info


def _bridges(aid, frame, elev):
    """The anchor's roads.BridgeProfiles (deck heights of whole bridge chains), made once per anchor in this process."""
    import roads as roadsmod
    with _s.lock:
        bridges = _s.bridges.get(aid)
        if bridges is None:
            bridges = _s.bridges[aid] = roadsmod.BridgeProfiles(
                frame, lambda key: _s.md.cell(key, layers=("roads",), kind="demand").layers.get("roads"), elev,
                getattr(_s.surface, 'urban', None), _s.widths)
            sea = getattr(_s, "sea", None)
            bridges.wet = sea.depth if sea is not None else None        # (decks clear the water: roads.water_clear)
            bridges.infra_fn = lambda key: _s.md.cell(key, layers=("infrastructure",), kind="demand").layers.get(
                "infrastructure")                                       # (bridge:structure tags: bridgetypes)
            for old in sorted(_s.bridges)[:-ANCHORS_KEPT]:
                del _s.bridges[old]
    return bridges


NAV_GRADED_M = 900.0        # m (Chebyshev) round the AI graph's centre where its ground is graded in full: the road tiles'
                            # reach (roads.SEND_M + a tile), whose cells they plan anyway; beyond, only planned cells


def _nav_bridges(aid, frame, elev):
    """The AI graph's roads.BridgeProfiles of an anchor (its own: elev grades only the planned cells), kept per anchor
    in this process."""
    import roads as roadsmod
    with _s.lock:
        nav = _s.__dict__.setdefault("nav_bridges", {})
        bridges = nav.get(aid)
        if bridges is None:
            bridges = nav[aid] = roadsmod.BridgeProfiles(
                frame, lambda key: _s.md.cell(key, layers=("roads",), kind="demand").layers.get("roads"), elev,
                getattr(_s.surface, 'urban', None), _s.widths)
            sea = getattr(_s, "sea", None)
            bridges.wet = sea.depth if sea is not None else None
            bridges.infra_fn = lambda key: _s.md.cell(key, layers=("infrastructure",), kind="demand").layers.get(
                "infrastructure")
            for old in sorted(nav)[:-ANCHORS_KEPT]:
                del nav[old]
    return bridges


def navgraph(aid, lat, lon, zoom, x, y, out_dir, vfs_dir, serial, left_hand, mph, rules):
    """earthd.Server.build_navgraph here (a road-tile process): the AI road graph around (x, y) written to out_dir;
    returns (vfs path, nodes, edges), in a road process (in the server's interpreter it cost 13 s of its CPU in a
    4-minute drive, 2026-10-20). Past NAV_GRADED_M the ground is graded only under cells planned already."""
    import navgraph as navgraphmod
    import mapcells
    g, _ = _s.anchor(aid, lat, lon)
    frame = geodesy.EnuFrame(lat, lon)
    w = navgraphmod.WINDOW_M + 200.0
    la, lo, _ = frame.enu_to_geodetic(np.array([x - w, x + w]), np.array([y - w, y + w]), 0.0)
    keys = mapcells.cells_for_bbox(float(la.min()), float(lo.min()), float(la.max()), float(lo.max()))
    feats = [_s.md.cell(k, layers=("roads",), kind="demand").layers.get("roads") for k in keys]

    def elev(la_, lo_):         # the physics ground, lifted under roads (as the road tiles'); past their reach
        z = ground(frame, la_, lo_, _s.dem.sample(la_, lo_, zoom))     # (NAV_GRADED_M) only under cells planned already
        if g is None:
            return z
        la_, lo_, z = np.asarray(la_, np.float64), np.asarray(lo_, np.float64), np.asarray(z, np.float64)
        e, n, _ = frame.geodetic_to_enu(la_.ravel(), lo_.ravel(), 0.0)
        near = np.maximum(np.abs(np.asarray(e) - x), np.abs(np.asarray(n) - y)).reshape(la_.shape) <= NAV_GRADED_M
        out = np.array(z, np.float64, copy=True)
        if near.any():
            out[near] = g.apply(la_[near], lo_[near], z[near], gradingmod.ROAD_LIFT)
        if (~near).any():
            out[~near] = g.apply(la_[~near], lo_[~near], z[~near], gradingmod.ROAD_LIFT, planned_only=True)
        return out
    decks = {}
    try:
        # (its own deck heights, on that ground: the road tiles' profiles plan every cell they touch, and the graph's
        # 2.2 km window planned ~50 cells, minutes of a road process on a Windows PC, 2026-10-25)
        for fid, _spec, xy, z, _lon in _nav_bridges(aid, frame, elev).parts(feats, (x - w, y - w, x + w, y + w)):
            decks.setdefault(fid, []).append((np.asarray(xy, np.float64)[:, :2], np.asarray(z, np.float64)))
    except Exception as e:          # no decks: the graph follows the ground under bridges
        log.debug("navgraph decks: %s: %s", type(e).__name__, e)
    urban = _s.surface.urban if _s.surface is not None else None
    graph = navgraphmod.build(frame, feats, elev, (x, y), urban, decks=decks, widths=_s.widths, left_hand=left_hand,
                              mph=mph, signals=furniture.signals_for(_s.md, frame, keys))
    graph["rules"] = rules
    path = navgraphmod.write(out_dir, vfs_dir, aid, serial, graph)
    return path, len(graph["nodes"]), len(graph["edges"])


def terrain_patch(aid, lat, lon, zoom, box, path):
    """earthd.Server.build_terrain_patch here (road-tile pool): the collision patch of the ground over box under a
    collision island off the terrain block (terrainpatch.py), the block's own ground (graded, lifted under roads)."""
    import terrainpatch
    g, _ = _s.anchor(aid, lat, lon)
    frame = geodesy.EnuFrame(lat, lon)

    def elev(la, lo):
        z = ground(frame, la, lo, _s.dem.sample(la, lo, zoom))
        return g.apply(la, lo, z, gradingmod.ROAD_LIFT) if g is not None else z
    return terrainpatch.build(frame, elev, box, path)


def roads_tile(aid, lat, lon, zoom, tx, ty, out_dir, vfs_dir, sidewalks, lift, buildings=False, left_hand=False,
               mph=True, q=None, gas_stations=False):
    """earthd.Server.build_roads_tile here (road-tile pool): the anchor's road tile (tx, ty) built and
    written; returns roads.write_tile's (vfs path, chunks) and its stations' pads (gasstations.tile_sites pads: the
    server keeps them for the ground's painters). In the server's process its Python made up
    ~80 % of the main process's busy threads (in-game profile 2026-10-13): the block photos and far
    textures waited for the GIL. A tile built in this anchor's place before comes from the revisit cache."""
    import revisit
    k = revisit.key("roads", round(float(lat), 7), round(float(lon), 7), zoom, tx, ty, bool(sidewalks), float(lift),
                    bool(buildings), bool(left_hand), bool(mph), None if q is None else float(q), bool(gas_stations),
                    _s.md.versions() if _s.md is not None else None)
    return _revisit_tile("roads", k, aid, tx, ty, out_dir, vfs_dir, f"roads_{aid}_{tx}_{ty}.json",
                         lambda: _roads_tile(aid, lat, lon, zoom, tx, ty, out_dir, vfs_dir, sidewalks, lift, buildings,
                                             left_hand, mph, q, gas_stations),
                         lambda res: {"n": int(res[1]), "pads": [[sid, [[np.asarray(la).tolist(), np.asarray(lo).tolist()]
                                                                       for la, lo in rs]] for sid, rs in res[2]]},
                         lambda path, extra: (path, int(extra.get("n", 0)),
                                              [(sid, [(np.asarray(la), np.asarray(lo)) for la, lo in rs])
                                               for sid, rs in extra.get("pads", [])]))


def _revisit_tile(kind, k, aid, tx, ty, out_dir, vfs_dir, name, build, extra_of, result_of):
    """A tile written to out_dir as name (a JSON pointing to its shapes in vfs_dir) from the revisit cache (key k), or
    built (build() -> its result) and kept there when complete (revisit.faults unchanged). extra_of(result): JSON data
    kept with it; result_of(vfs path of name, that data): the result again on a hit. The session's names (vfs_dir, the
    anchor id) are taken out of the kept files and put back."""
    import revisit
    mine = [(f"{vfs_dir}/", "@V@/"), (f"_{aid}_{tx}_{ty}", f"_@A@_{tx}_{ty}")]
    got = revisit.load(_s.cache_dir, kind, k)
    if got is not None:
        blobs, extra = got
        back = [(b, a) for a, b in reversed(mine)]
        for g, data in blobs.items():
            if g.endswith(".json"):
                data = revisit.swap(data.decode("utf-8"), back).encode("utf-8")
            _write_bytes(os.path.join(out_dir, revisit.swap(g, back)), data)
        return result_of(f"{vfs_dir}/{name}", extra or {})
    f0 = revisit.faults()
    res = build()
    if res is not None and revisit.ON and revisit.faults() == f0:     # (complete: nothing it read failed or was missing)
        try:
            with open(os.path.join(out_dir, name), encoding="utf-8") as f:
                text = f.read()
            blobs = {revisit.swap(name, mine): revisit.swap(text, mine).encode("utf-8")}
            for n in revisit.shapes_in(text, vfs_dir):
                with open(os.path.join(out_dir, n), "rb") as f:
                    blobs[revisit.swap(n, mine)] = f.read()
            revisit.store(_s.cache_dir, kind, k, blobs, extra_of(res))
        except Exception as e:          # (not kept rather than no tile)
            log.debug("revisit: %s tile %d_%d not kept: %s: %s", kind, tx, ty, type(e).__name__, e)
    return res


def _write_bytes(path, data):
    """path written atomically (staging: the game sees only finished files)."""
    tmp = staging.tmp_for(path)
    with open(tmp, "wb") as f:
        f.write(data)
    staging.replace(tmp, path)


def _roads_tile(aid, lat, lon, zoom, tx, ty, out_dir, vfs_dir, sidewalks, lift, buildings, left_hand, mph, q,
                gas_stations):
    import parking as parkingmod
    import rails as railsmod
    import roadmesh as roadmeshmod
    import roads as roadsmod
    started = checkpoint = time.perf_counter()
    stages = []

    def timed(name):
        nonlocal checkpoint
        now = time.perf_counter()
        stages.append((name, now - checkpoint))
        checkpoint = now

    frame = geodesy.EnuFrame(lat, lon)
    # everything the tile will ask for, fetched at once (one after another, a fresh place's first tile took ~1 min)
    _s.md.warm(roadsmod.tile_needs(frame, tx, ty, buildings, _s.widths is not None,
                                   1 + seacarvemod.LINE_CELLS if _s.sea is not None else 0))
    g, _ = _s.anchor(aid, lat, lon)
    cells = [_s.md.cell(k, layers=("roads",), kind="demand").layers.get("roads")
             for k in roadsmod.cells_of_tile(frame, tx, ty)]
    timed("map/anchor")

    def elev(la, lo):           # the physics ground (lifted under roads: grading.ROAD_LIFT)
        z = ground(frame, la, lo, _s.dem.sample(la, lo, zoom))
        return g.apply(la, lo, z, gradingmod.ROAD_LIFT) if g is not None else z
    bridges = _bridges(aid, frame, elev)
    tunnels = tunnel_profiles(aid, frame, elev, g)
    timed("profiles")
    walks = []
    urban = _s.surface.urban if _s.surface is not None else None
    fuel, skip, fuel_cuts, fuel_pads = [], frozenset(), [], []
    if gas_stations:                    # pumps and refuel areas; their canopy outlines left out of the buildings
        import gasstations
        fuel, skip = gasstations.tile_sites(frame, tx, ty, _s.md, elev, cuts=fuel_cuts, pads=fuel_pads)
    props = [] if buildings else None
    lines = [] if buildings else None
    wires = [] if buildings else None
    park = [] if buildings else None
    items = town = None
    if buildings:
        import buildings as buildingsmod
        # (on the graded ground, the one the terrain is drawn with: on the raw elevation a building beside a road's
        # cutting stood over the cut; hashk014's PR #69)
        items = buildingsmod.road_tile_items(frame, tx, ty, _s.md, elev, decks=_tile_decks(bridges, cells, tx, ty),
                                             skip=skip)
        town = buildingsmod.coverage(items) < buildingsmod.POLES_COVERAGE
    timed("sites/buildings")
    rwalls = []
    ctx = []
    box = roadsmod.tile_bbox(tx, ty)
    cell_fn = lambda key: _s.md.cell(key, layers=("roads",), kind="demand").layers.get("roads")   # noqa: E731

    def drawn(la, lo):          # the block's ground: raised over the low arches as the terrain is (_make_heightmap)
        return roadsmod.block_arch_raise(tunnels, cell_fn, frame, la, lo, elev(la, lo),
                                         (box[0] - 8, box[1] - 8, box[2] + 8, box[3] + 8))
    cover = []                          # the ground carriageways for the decks' parapets (roadmesh.deck ground=)
    chunks = roadsmod.build_tile(frame, tx, ty, cells, elev, urban, walks=walks, deck_context=ctx, bridges=bridges, widths=_s.widths,
                                 walk_cuts=fuel_cuts,    # (the stations' made-up driveways: no sidewalk across them)
                                 props=props, lines=lines, wires=wires, town=town, park=park,
                                 left_hand=left_hand, mph=mph, structures=buildingsmod.centres(items, tx, ty) if items else (),
                                 tunnels=tunnels, walls=rwalls,
                                 ground=lambda x, y: roadmeshmod.drawn_ground(frame, drawn, np.c_[x, y], q),
                                 terrain_paint=getattr(_s, "surface", None) is not None, cover_out=cover,
                                 signals=furniture.signals_for(_s.md, frame,
                                                               roadsmod.cells_of_tile(frame, tx, ty, SIGNALS_MARGIN_M)))
    timed("layout")
    chunks += railsmod.tile_chunks(frame, tx, ty, cells)     # railway and tram tracks on the ground
    if _s.airports is not None:         # runway markings, drawn by the road mesh like the lane lines
        chunks += _s.airports.tile_chunks(frame, tx, ty, elev, roadsmod.tile_bbox)
    x0, y0, _, _ = roadsmod.tile_bbox(tx, ty)
    if items is not None:               # front doors and shopfronts, driveways into the road mesh
        drives = []
        props.extend(buildingsmod.doors(items, lines, x0, y0, drives))
        props.extend(buildingsmod.toll_booth_rows(items, x0, y0))
        drives.extend(buildingsmod.garage_drives(items, lines, x0, y0))
        chunks += drives
        park = parkingmod.clear_of(park, props, drives)
        park = parkingmod.tile_lot_spots(frame, tx, ty, _s.md, elev, items, park)
        if fuel:
            import gasstations
            park = gasstations.clear_park(park, fuel)
        buildingsmod.brick_walks(walks, items, lat, lon)
    timed("props/parking")
    mesh = roadmeshmod.build(frame, chunks, elev, (x0, y0, 0.0), lift, clip=box, follow=True, q=q, drawn=drawn)
    mesh = _with_plazas(mesh, frame, tx, ty, elev, (x0, y0, 0.0), getattr(_s, "surface", None))
    timed("ground mesh")
    dmesh = roadmeshmod.deck(chunks, (x0, y0, 0.0), bridges.piers_in(roadsmod.tile_bbox(tx, ty)), ctx,
                              bridges.beams_in(roadsmod.tile_bbox(tx, ty)), ground=cover)
    tmesh = roadmeshmod.tunnel(chunks, (x0, y0, 0.0), rwalls)
    import railways
    rmesh = railways.tile_mesh(frame, tx, ty,
                               [cell_fn(k) for k in roadsmod.cells_of_tile(frame, tx, ty, railways.REACH_M)],
                               railways.point_ground(frame, elev, (aid, lat, lon)),
                               far_ground=railways.point_ground(
                                   frame, lambda la, lo: ground(frame, la, lo, _s.dem.sample(la, lo, zoom)),
                                   ("raw", aid, lat, lon)))
    bld = None
    if buildings:
        import buildings as buildingsmod
        bld = buildingsmod.road_tile(frame, tx, ty, _s.md, elev, None, None, items)
    timed("structures/rails")
    result = roadsmod.write_tile(out_dir, vfs_dir, aid, tx, ty, chunks, walks if sidewalks else None, mesh, dmesh,
                                bld, props, wires, park, tunnel_mesh=tmesh, fuel=fuel, rail_mesh=rmesh) + (fuel_pads,)
    timed("write")
    elapsed = checkpoint - started
    if elapsed >= 10.:
        log.info("roads tile (%d, %d) anchor %s: %.2f s; %s", tx, ty, aid, elapsed,
                 ", ".join("%s %.2f s" % stage for stage in stages))
    return result


def signs_tile(aid, lat, lon, zoom, tx, ty, out_dir, vfs_dir, left_hand=False, mph=True):
    """Sign-only counterpart of Server.build_signs_tile, isolated from road workers."""
    import signstream
    g, _ = _s.anchor(aid, lat, lon)
    frame = geodesy.EnuFrame(lat, lon)
    def elev(la, lo):
        z = ground(frame, la, lo, _s.dem.sample(la, lo, zoom))
        return g.apply(la, lo, z, gradingmod.ROAD_LIFT) if g is not None else z
    return signstream.build_tile(_s.md, frame, aid, tx, ty, elev, out_dir, vfs_dir,
                                 getattr(_s.surface, 'urban', None), _s.widths, left_hand, mph)


def _with_plazas(mesh, frame, tx, ty, elev, origin, surface):
    """The road mesh with the tile's squares and pedestrian areas paved (plazas.py), from the cached land use (none
    when it is not cached yet: surface.Surface.lots never waits)."""
    if surface is None or not hasattr(surface, "lots"):
        return mesh
    import plazas
    import roads as roadsmod
    try:
        box = roadsmod.tile_bbox(tx, ty)
        la, lo, _ = frame.enu_to_geodetic(np.array([box[0], box[2]]), np.array([box[1], box[3]]), np.zeros(2))
        rings = plazas.polygons(surface.lots(la, lo))
        if not rings:
            return mesh
        cover = None
        if mesh is not None and len(mesh[1]):
            P, T = np.asarray(mesh[0], np.float64), np.asarray(mesh[1], np.int64)
            cover = (P[T][:, :, :2] + np.asarray(origin, np.float64)[:2]).reshape(-1, 6)
        return plazas.merge(mesh, plazas.mesh(frame, box, rings, elev, origin, cover))
    except Exception:                   # no plazas rather than no road tile
        log.exception("plazas of tile %s_%s", tx, ty)
        return mesh


def _tile_decks(bridges, cells, tx, ty):
    """[(xy (k, 2), z (k,))] of the bridge decks over road tile (tx, ty) (the buildings they run through get a way:
    buildings.road_tile_items), [] without bridge profiles."""
    if bridges is None:
        return []
    import roads as roadsmod
    x0, y0, x1, y1 = roadsmod.tile_bbox(tx, ty)
    try:
        return [(np.asarray(xy, np.float64)[:, :2], np.asarray(z, np.float64))
                for _, _, xy, z, _ in bridges.parts(cells, (x0 - 20, y0 - 20, x1 + 20, y1 + 20))]
    except Exception as e:      # no ways cut rather than no buildings
        log.debug("tile decks: %s: %s", type(e).__name__, e)
        return []


def _open_tunnels(g, keep):
    """The tunnels' open sections over a block (grading.Grader.open_tunnels; none without a grader)."""
    if g is None:
        return []
    try:
        return g.open_tunnels(keep["lat"], keep["lon"])
    except Exception:
        log.exception("open tunnel sections")
        return []


def _tunnel_holes(aid, frame, elev, keep, cx, cy, idx, info, g=None):
    """idx with the block's tunnel portals as holes (roads.block_tunnel_holes); info["tunnelHoles"]: how many."""
    import roads as roadsmod
    if getattr(_s, "md", None) is None:     # no map data (roads off): no tunnels
        return idx
    try:
        holes = roadsmod.block_tunnel_holes(
            tunnel_profiles(aid, frame, elev, g),
            lambda key: _s.md.cell(key, layers=("roads",), kind="demand").layers.get("roads"),
            frame, keep, cx, cy, heightmap.SQUARE)
    except Exception:                   # no holes rather than no block
        log.exception("tunnel holes of block (%s, %s)", cx, cy)
        return idx
    if holes is None or not holes.any():
        return idx
    idx = np.zeros(holes.shape, np.uint8) if idx is None else idx
    idx[holes] = heightmap.TER_HOLE
    info["tunnelHoles"] = int(holes.sum())
    return idx


def tunnel_profiles(aid, frame, elev, g=None):
    """The anchor's roads.TunnelProfiles in this process (road tiles and the block's terrain holes share it): its
    ground read at grading.PROFILE_ZOOM (raw; ends: graded by g), as in every other process."""
    import roads as roadsmod
    with _s.lock:
        if getattr(_s, "tunnels", None) is None:        # (a state made without it: tests)
            _s.tunnels = {}
        t = _s.tunnels.get(aid)
        if t is None:
            t = _s.tunnels[aid] = roadsmod.TunnelProfiles(
                frame, lambda key: _s.md.cell(key, layers=("roads",), kind="demand").layers.get("roads"), elev)
            sea = getattr(_s, "sea", None)
            t.wet = sea.depth if sea is not None else None
            t.raw = lambda la, lo: ground(frame, la, lo, _s.dem.sample(la, lo, gradingmod.PROFILE_ZOOM))  # noqa: E731
            if g is not None:
                t.ends = lambda la, lo: g.apply(la, lo, t.raw(la, lo), gradingmod.ROAD_LIFT, roads_only=True)    # noqa: E731
            for old in sorted(_s.tunnels)[:-ANCHORS_KEPT]:
                del _s.tunnels[old]
        return t


def far_buildings_tile(aid, lat, lon, zoom, tx, ty, out_dir, vfs_dir):
    """earthd.Server.build_far_bld_tile here (road-tile pool): buildings.far_tile (from the revisit cache when built in
    this anchor's place before)."""
    import buildings as buildingsmod
    import revisit
    frame = geodesy.EnuFrame(lat, lon)
    k = revisit.key("bldfar", round(float(lat), 7), round(float(lon), 7), zoom, tx, ty,
                    _s.md.versions() if _s.md is not None else None)
    return _revisit_tile("bldfar", k, aid, tx, ty, out_dir, vfs_dir, f"bldfar_{aid}_{tx}_{ty}.json",
                         lambda: buildingsmod.far_tile(frame, tx, ty, _s.md,
                                                       lambda la, lo: ground(frame, la, lo, _s.dem.sample(la, lo, zoom)),
                                                       out_dir, vfs_dir, aid),
                         lambda res: {"n": int(res[1])},
                         lambda path, extra: (path, int(extra.get("n", 0))))


def base_entry(user_dir, base_vfs, ao_vfs, rgb, ao_value):
    """terraincache.base_entry here (its BC7 encode is the base photo's main cost)."""
    return terraincache.base_entry(user_dir, base_vfs, ao_vfs, rgb, ao_value)


def far_geometry(aid, lat, lon, tile, zoom, q, ref_zoom, with_sink, layout):
    """farfield.build_tile here (far-tile workers: geometry graded through ROAD_GRADE_MAX_LEVEL is
    most of a far tile's cost; its texture is rendered in the server, where the imagery corrections live).
    Returns the Built; one built in this anchor's place before comes from the revisit cache."""
    import revisit
    k = revisit.key("fargeo", *_far_key(lat, lon, tile, zoom, q, ref_zoom, with_sink, layout))
    got = revisit.load(_s.cache_dir, "fargeo", k)
    if got is not None:
        return _built_from(got[0]["built.npz"], got[1])
    f0 = revisit.faults()
    built = _far_geometry(aid, lat, lon, tile, zoom, q, ref_zoom, with_sink, layout)
    if revisit.ON and revisit.faults() == f0:
        blob, meta = _built_blob(built)
        revisit.store(_s.cache_dir, "fargeo", k, {"built.npz": blob}, meta)
    return built


def _far_key(lat, lon, tile, zoom, q, ref_zoom, with_sink, layout):
    """What a far tile's geometry is built from (the revisit cache's key)."""
    return (round(float(lat), 7), round(float(lon), 7), (tile.level, tile.i, tile.j), zoom,
            None if q is None else float(q), ref_zoom, bool(with_sink), repr(layout),
            _s.md.versions() if _s.md is not None else None, _s.sea is not None, _s.airports is not None)


def _built_blob(built):
    """(npz bytes, meta) of a farfield.Built (_built_from reads them back)."""
    import io
    buf = io.BytesIO()
    arrays = {n: getattr(built, n) for n in ("positions", "normals", "uvs", "planar", "tris")}
    if built.grid is not None:
        arrays["grid"] = built.grid
    np.savez(buf, **arrays)
    meta = {"pos": [float(v) for v in built.pos], "err": float(built.err),
            "sink": None if built.sink is None else float(built.sink), "skirt": float(built.skirt),
            "size": float(built.size)}
    return buf.getvalue(), meta


def _built_from(blob, meta):
    import io
    with np.load(io.BytesIO(blob)) as z:
        a = {n: z[n] for n in z.files}
    return farfield.Built(pos=list(meta["pos"]), positions=a["positions"], normals=a["normals"], uvs=a["uvs"],
                          planar=a["planar"], tris=a["tris"], grid=a.get("grid"), err=meta["err"], sink=meta["sink"],
                          skirt=meta["skirt"], size=meta["size"])


def _far_geometry(aid, lat, lon, tile, zoom, q, ref_zoom, with_sink, layout):
    g, _ = _s.anchor(aid, lat, lon)
    frame = geodesy.EnuFrame(lat, lon)
    base = _s.airports.wrap(_s.dem) if _s.airports is not None else _s.dem
    if _s.sea is not None:
        base = seacarvemod.SeaDem(base, _s.sea if tile.level <= seacarvemod.CARVE_MAX_LEVEL else None)
    src = gradingmod.GradedDem(base, g) if (g is not None and tile.level <= farfield.ROAD_GRADE_MAX_LEVEL) else base
    return farfield.build_tile(frame, src, tile, zoom, q, ref_zoom, with_sink=with_sink,
                               layout=layout)


WORKER_IMAGERY_TILES = 96      # decoded imagery tiles a far-tile worker keeps (imagery.MEM_TILES: 512, the server's)


def _imagery(tex):
    """This process's imagery.Imagery for the server's chain settings (tex: chain, tone, leaf_on, season_mode): the
    imagery tiles on disk alone (strict: a missing tile fails the render), or, for a compute-sharing helper
    (tex["online"], peerwork.py), fetched as needed."""
    import imagery as imagerymod
    online = bool(tex.get("online"))
    key = (tuple(tex["chain"]), tex["tone"], tex["leaf_on"], tex["season_mode"], online)
    if _s.imagery is None or _s.imagery[0] != key:
        im = imagerymod.Imagery(os.path.join(_s.cache_dir, "imagery"), key[0], fetch_workers=4 if online else 1,
                                mem_tiles=WORKER_IMAGERY_TILES, tone=key[1],
                                fetcher=netfetch.Fetcher(offline=not online), leaf_on=key[2], season_mode=key[3])
        im.strict = not online
        _s.imagery = (key, im)
    return _s.imagery[1]


def base_photo(lat, lon, x0, y0, size, px, tex, buildings, rock_zoom, path, cache, fuel_pads=None):
    """earthd.Server.write_base here, for a server without its imagery analyses (a small machine): the block's base
    photo from the imagery tiles on disk, its building footprints filled and its cliffs as rock, written to path
    (PNG) and packed into the game's terrain texture cache (cache: terraincache.base_entry's arguments but the rgb).
    Returns (rgb, sources used, lazy tiles still to come), or None when an imagery tile it needs is not on disk (the
    server renders it, fetching what is missing). In the server process it ran 2 s alone but 8-11 s next to the
    rest of the world's threads on a Steam Deck (one interpreter lock), and the next block waited for it. fuel_pads:
    the gas station pads around it (_take_pads)."""
    _take_pads(fuel_pads)
    import imagery as imagerymod
    import roads as roadsmod
    import rockface as rockfacemod
    import staging
    from PIL import Image
    import buildings as buildingsmod
    im = _imagery(tex)
    frame = geodesy.EnuFrame(lat, lon)
    pending = set()
    try:
        rgb, used = im.render(frame, x0, y0, size, px, pending)
    except imagerymod.ImageryError:
        return None
    rgb = np.asarray(rgb, np.uint8)
    if buildings and _s.md is not None:
        try:
            mask = buildingsmod.photo_mask(frame, x0, y0, size, px, _s.md)
            if mask is not None:
                rgb = buildingsmod.fill_photo(rgb, mask, size, px)
        except Exception as e:      # the photo stays as it is
            log.info("base photo: building mask: %s", e)
    if roadsmod.ROADS_IN_GROUND:          # (earthd.Server.write_base)
        try:
            import surface as surfacemod
            rgb = surfacemod.paint_square(rgb, frame, x0, y0, size, getattr(_s, "surface", None), _s.widths)
        except Exception as e:      # the photo stays as it is
            log.info("base photo: roads: %s", e)
    try:            # (earthd.Server.write_base)
        n = 65
        xs = x0 + np.arange(n) * (size / (n - 1))
        ys = y0 + np.arange(n) * (size / (n - 1))
        _, U = heightmap.surface(frame, lambda la, lo: _s.dem.sample(la, lo, rock_zoom), xs, ys)
        rgb = rockfacemod.rock(rgb, frame, x0, y0, size, U, x0, y0, size / (n - 1))
    except Exception as e:
        log.debug("base photo: rock faces: %s", e)
    tmp = staging.tmp_for(path)
    # stored, not deflated: the game reads the packed cache entry (terraincache), this file only if that is stale;
    # level 1 was 0.15 s of a block photo on a Steam Deck, level 0 0.08 (3 MB, a session keeps a few)
    Image.fromarray(rgb, "RGB").save(tmp, format="PNG", compress_level=0)
    staging.replace(tmp, path)
    user_dir, base_vfs, ao_vfs, ao_value = cache
    terraincache.base_entry(user_dir, base_vfs, ao_vfs, rgb, ao_value)
    return rgb, used, pending


def far_tile(aid, lat, lon, tile, zoom, q, ref_zoom, with_sink, layout, tex):
    """far_geometry and the tile's texture here, written to tex["path"] (the server without its imagery analyses:
    de-shadow and colour match off, a small machine). The texture from the imagery tiles on disk alone, the server's
    chain (Imagery.strict): (Built, (texture bytes, sources used)), or (Built, None) when a tile it needs is not on
    disk (the server renders it, fetching what is missing). On a Steam Deck the textures were most of the server
    process's own CPU, one interpreter for every thread (55 s of a drive), and the far field waited on it. A tile
    built in this anchor's place before (and its texture) comes from the revisit cache."""
    import revisit
    mask = tex.get("mask") if tex.get("mask") and tex["mask"][1] else None
    k = revisit.key("far", *_far_key(lat, lon, tile, zoom, q, ref_zoom, with_sink, layout), tex["px"],
                    mask[1] if mask else 0, tuple(tex["chain"]), tex["tone"], tex["leaf_on"], tex["season_mode"],
                    bool(tex.get("online")), _s.widths is not None, _s.surface is not None)
    got = revisit.load(_s.cache_dir, "far", k)
    if got is not None:
        blobs, extra = got
        if mask and "mask.dds" in blobs:
            _write_bytes(mask[0], blobs["mask.dds"])
        _write_bytes(tex["path"], blobs["tex.dds"])
        return _built_from(blobs["built.npz"], extra), (int(extra["tex_bytes"]), extra["used"])
    f0 = revisit.faults()
    built, got = _far_tile(aid, lat, lon, tile, zoom, q, ref_zoom, with_sink, layout, tex, mask)
    if got is not None and revisit.ON and revisit.faults() == f0:
        try:
            blob, meta = _built_blob(built)
            blobs = {"built.npz": blob}
            with open(tex["path"], "rb") as f:
                blobs["tex.dds"] = f.read()
            if mask and os.path.exists(mask[0]):
                with open(mask[0], "rb") as f:
                    blobs["mask.dds"] = f.read()
            meta.update(tex_bytes=int(got[0]), used={str(n): int(c) for n, c in (got[1] or {}).items()})
            revisit.store(_s.cache_dir, "far", k, blobs, meta)
        except Exception as e:          # (not kept rather than no tile)
            log.debug("revisit: far tile %s not kept: %s: %s", (tile.level, tile.i, tile.j), type(e).__name__, e)
    return built, got


def _far_tile(aid, lat, lon, tile, zoom, q, ref_zoom, with_sink, layout, tex, mask):
    import imagery as imagerymod
    import rockface as rockfacemod
    built = _far_geometry(aid, lat, lon, tile, zoom, q, ref_zoom, with_sink, layout)    # (its CPU counted here)
    im = _imagery(tex)
    frame = geodesy.EnuFrame(lat, lon)
    x0, y0, _, _ = tile.bbox
    px = tex["px"]
    sq = imagerymod.gutter_square(x0, y0, tile.size, px)
    try:
        rgb, used = im.render(frame, *sq, px)
    except imagerymod.ImageryError:
        return built, None
    if built.grid is not None:          # (earthd.Server.rock_faces)
        try:
            g = np.asarray(built.grid)
            rgb = rockfacemod.rock(rgb, frame, *sq, g, x0, y0, tile.size / (g.shape[1] - 1))
        except Exception as e:
            log.debug("rock faces of %s: %s", tile.key, e)
    import roads as roadsmod
    if roadsmod.ROADS_IN_GROUND:          # (earthd.Server.road_faces)
        try:
            import surface as surfacemod
            rgb = surfacemod.paint_square(rgb, frame, *sq, getattr(_s, "surface", None), _s.widths)
        except Exception as e:
            log.debug("road faces of %s: %s", tile.key, e)
    if mask:                            # (earthd.Server.write_road_mask)
        try:
            import surface as surfacemod
            m = surfacemod.road_mask(frame, *sq, mask[1], getattr(_s, "surface", None), _s.widths)
            if m is not None:
                imagerymod.write_dds(mask[0], np.repeat(m[..., None], 3, axis=2))
        except Exception as e:
            log.debug("road mask of %s: %s", tile.key, e)
    return built, (imagerymod.write_dds(tex["path"], rgb), used)


# --- CPU accounting ---------------------------------------------------------------------------------------------------
# Each job's CPU seconds by kind, logged by the process every CPU_LOG_S at debug level ("worker <pid> cpu: ..."), so a
# recorded session says which pool (road tiles, far buildings, far geometry, terrain blocks) the server's CPU went to.
CPU_LOG_S = 30.0
_cpu = {}
_cpu_logged = [time.monotonic()]


def _accounted(fn):
    import functools

    @functools.wraps(fn)
    def run(*a, **kw):
        c0 = time.process_time()
        try:
            return fn(*a, **kw)
        finally:
            n, t = _cpu.get(fn.__name__, (0, 0.0))
            _cpu[fn.__name__] = (n + 1, t + time.process_time() - c0)
            now = time.monotonic()
            if now - _cpu_logged[0] >= CPU_LOG_S:
                _cpu_logged[0] = now
                shared = [g.shared.stats for g in list(getattr(_s, "graders", {}).values())
                          if g is not None and g.shared is not None]
                log.debug("worker %d cpu: %s%s", os.getpid(),
                          ", ".join(f"{k} {v[0]} jobs {v[1]:.1f} s" for k, v in sorted(_cpu.items())),
                          "; shared road plans " + ", ".join(f"{k} {sum(st[k] for st in shared):.0f}"
                                                             for k in ("read", "written", "waited", "wait_s"))
                          if shared else "")
    return run


build = _accounted(build)
terrain_patch = _accounted(terrain_patch)
roads_tile = _accounted(roads_tile)
far_buildings_tile = _accounted(far_buildings_tile)
base_entry = _accounted(base_entry)
far_geometry = _accounted(far_geometry)
far_tile = _accounted(far_tile)
base_photo = _accounted(base_photo)
navgraph = _accounted(navgraph)
