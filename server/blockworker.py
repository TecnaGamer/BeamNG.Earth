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
        import railways as _railways    # (coarse elevation: rail decks raised over the roads under them)
        _railways.COARSE_AT = self.dem.coarse_at
        # runways graded in (--airports on): the copy the server loaded, never refreshed here
        self.airports = load_airports(cache_dir, self.dem) if airports else None
        md = mapdatamod.MapData(cache_dir, fetcher=net) if (roads or surface or sea) else None
        if md is not None:
            md.dem = self.dem       # (gradesep's ground)
        # the sea's and the lakes' floors carved in (--sea on)
        self.sea = seacarvemod.SeaCarver(md, cache_dir, dem=self.dem) if sea else None
        self.md = md if roads else None
        import fra
        self.fra = fra.Crossings(cache_dir, net) if roads else None     # (the US's level crossings' devices)
        self.grading = bool(grading) and self.md is not None
        self.widths = (roadwidthmod.RoadWidths(
            lambda key: md.cell(key, layers=("buildings",), kind="demand").layers.get("buildings"),
            roadwidthmod.store_dir(cache_dir, md)).get
            if road_widths and self.md is not None else None)
        self.surface = surfacemod.Surface(md) if surface else None
        if self.surface is not None:    # gas station forecourts painted asphalt (--gas-stations on)
            self.surface.gas_pads = bool(gas_stations)
            if self.airports is not None:   # ... and the runways (GitHub #337)
                self.surface.extra_pads = self.airports.runway_pads
        self.graders = {}           # anchor id -> grading.Grader
        self.chunks = {}            # anchor id -> heightmap.ChunkCache
        self.bridges = {}           # (unused since the chain stores: _chain_store)
        self.tunnels = {}           # (unused since the chain stores: _chain_store)
        self.lock = threading.Lock()
        self.want = []              # (grader, cell key) to plan, most urgent first
        self.cv = threading.Condition()

    def anchor(self, aid, lat, lon):
        """(Grader or None, ChunkCache) of an anchor; only the last few anchors are kept."""
        with self.lock:
            if aid not in self.chunks:
                frame = geodesy.EnuFrame(lat, lon)
                self.chunks[aid] = heightmap.ChunkCache()
                if self.grading and getattr(self, "plan_store", None) is None:
                    # (one store for every anchor: the plans are the planning regions', whatever the anchor)
                    self.plan_store = gradingmod.PlanStore(
                        self.md, lambda la, lo: self.dem.sample(la, lo, gradingmod.PROFILE_ZOOM), self.widths)
                    # (its tunnels' drowned question asked of the road tiles' chain store: resolved once for both)
                    self.plan_store.tunnel_store = lambda: self.__dict__.get("chain_stores", {}).get("tunnels")
                self.graders[aid] = (gradingmod.Grader(frame, self.md, self.plan_store.elev, self.widths,
                                                       store=self.plan_store)
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
    if warm_q is not None:      # (only the block pools plan roads ahead: the far, road and far building pools pass None)
        threading.Thread(target=_read_warm, args=(warm_q,), daemon=True, name="warm-read").start()
        threading.Thread(target=_warm_loop, daemon=True, name="roads-ahead").start()


def _watch_parent(ppid):
    """Exit with the server: a killed server (SIGTERM) left its block processes running. Windows: os.getppid() keeps
    the parent's id after it is gone, so the poll never saw it go (from Madderose's PR #41: orphaned workers held the
    cache's files across server restarts); there the process handle is waited on."""
    if os.name == "nt":
        _wait_process(ppid)
        os._exit(0)
    while os.getppid() == ppid:
        time.sleep(PARENT_POLL_S)
    os._exit(0)


def _wait_process(pid):
    """Windows: return once process pid has exited (at once if it cannot be opened: gone already)."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    h = k32.OpenProcess(0x00100000, False, int(pid))       # SYNCHRONIZE
    if not h:
        return
    try:
        k32.WaitForSingleObject(h, 0xFFFFFFFF)             # INFINITE
    finally:
        k32.CloseHandle(h)


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



def plan_ahead(g, frame, cx, cy, half):
    """A thread planning the block's road cells (grading.Grader.prepare) while the block's builder samples its ground
    and sea floor; None without a grader."""
    if g is None or not hasattr(g, "prepare"):
        return None
    xs = cx + np.linspace(-half, half, 5)
    ys = cy + np.linspace(-half, half, 5)
    X, Y = np.meshgrid(xs, ys)
    la, lo, _ = frame.enu_to_geodetic(X.ravel(), Y.ravel(), 0.0)

    def run():
        try:
            g.prepare(np.asarray(la), np.asarray(lo))
        except Exception as e:      # the builder plans them itself then
            log.debug("plans ahead of block (%s, %s): %s: %s", cx, cy, type(e).__name__, e)
    t = threading.Thread(target=run, daemon=True, name="block-plans")
    t.start()
    return t

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
    part_s = {"dem": 0.0, "sea": 0.0, "tunnels": 0.0}  # (the rest of "heights": the stages' log, 2026-10-30)

    def elev(la, lo):
        t1 = time.perf_counter()
        z = _s.dem.sample(la, lo, hm_zoom)
        t2 = time.perf_counter()
        z = ground(frame, la, lo, z)
        part_s["dem"] += t2 - t1
        part_s["sea"] += time.perf_counter() - t2
        if g is None:
            return z
        t1 = time.perf_counter()
        z = g.apply(la, lo, z, gradingmod.ROAD_LIFT)
        grade_s[0] += time.perf_counter() - t1
        return z
    import roads as roadsmod
    plan_ahead(g, frame, cx, cy, size * heightmap.SQUARE / 2)
    raw = lambda la, lo: ground(frame, la, lo, _s.dem.sample(la, lo, hm_zoom))     # noqa: E731
    gen = elev
    if ter_name is not None and getattr(_s, "md", None) is not None:
        t1 = time.perf_counter()
        tun = tunnel_profiles(aid, frame, elev, g)
        part_s["tunnels"] += time.perf_counter() - t1
        half = size * heightmap.SQUARE / 2
        cell_fn = lambda key: _s.md.cell(key, layers=("roads",), kind="demand").layers.get("roads")   # noqa: E731

        def gen(la, lo):        # the ground over the low arches raised over their vaults (roads.arch_raise)
            return roadsmod.block_arch_raise(tun, cell_fn, frame, la, lo, elev(la, lo),
                                             (cx - half, cy - half, cx + half, cy + half))
    info = heightmap.generate(frame, gen, os.path.join(out_dir, name), cx, cy, q=q, size=size, keep=keep,
                              chunks=chunks)
    stages = {"heights": time.perf_counter() - t0, "grading": grade_s[0], **part_s}
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
        lots = _s.surface.lots(keep["lat"], keep["lon"]) if hasattr(_s.surface, "lots") else None
        if lots:                        # farmland under the carriageways (a field does not cover the asphalt)
            idx = surfacemod.paint_fields(idx, keep["lat"], keep["lon"], lots)
        if road_feats:
            before = idx
            idx = surfacemod.paint_roads(idx, keep["lat"], keep["lon"], road_feats, heightmap.SQUARE,
                                         _s.surface.urban, _s.widths, _open_tunnels(g, keep))
            if not (idx != before).any():   # (drawn roads over it, none painted: Discord's gravel mountain roads)
                import roads as roadsmod
                la0, la1 = float(np.min(keep["lat"])), float(np.max(keep["lat"]))
                lo0, lo1 = float(np.min(keep["lon"])), float(np.max(keep["lon"]))
                over = n_ground = 0             # (points of roads with a spec in it; of those, with ground parts)
                for f in road_feats:
                    if f is None or not len(f):
                        continue
                    specs = roadsmod.road_specs(f, _s.surface.urban, _s.widths)
                    for i in range(len(f)):
                        if specs[i] is None:
                            continue            # (paths, tracks ...: never painted)
                        ll = np.concatenate([np.asarray(r)[:, :2] for part in f.parts(i) for r in part])
                        n = int(((ll[:, 0] >= lo0) & (ll[:, 0] <= lo1) & (ll[:, 1] >= la0) & (ll[:, 1] <= la1)).sum())
                        over += n
                        n_ground += n if len(roadsmod.ground_parts(f, i)) else 0
                if over:
                    log.warning("block (%s, %s, %s): %d points of drawn roads in it (%d of them with ground parts) from "
                                "%d cells, none painted", aid, cx, cy, over, n_ground, len(road_feats))
        idx = surfacemod.paint_lots(idx, keep["lat"], keep["lon"], lots) if lots else idx
        if hasattr(_s.surface, "fuel_pads"):    # gas station forecourts (--gas-stations on)
            idx = surfacemod.paint_pads(idx, keep["lat"], keep["lon"], _s.surface.fuel_pads(keep["lat"], keep["lon"]))
        stages["roadPaint"] = time.perf_counter() - t1
        info["layers"] = surfacemod.write_masks(idx[::-1], out_dir, name[:-4])
        info["surfaceComplete"] = complete and roads_complete
        info["roadsComplete"] = roads_complete
    if ter_name is not None:
        t1 = time.perf_counter()
        idx = _tunnel_holes(aid, frame, elev, keep, cx, cy, idx, info, g)
        info["ter"], mats = surfacemod.write_ter(out_dir, ter_name, keep["qgrid"], idx)
        if mats is not None:          # the .ter's own material list (MATERIALS indices)
            info["mats"] = mats
        stages["ter"] = time.perf_counter() - t1
    info["stages"] = stages
    return info


def _chain_store(name, cls, elev, setup=None):
    """This process's roads.ChainStore `name` (one per purpose: every anchor's tiles share it), made on first use with
    elev (the physics ground: its values are the same whatever the anchor, the plans being the planning regions')."""
    import roads as roadsmod
    with _s.lock:
        stores = _s.__dict__.setdefault("chain_stores", {})
        got = stores.get(name)
        if got is None:
            got = stores[name] = roadsmod.ChainStore(
                cls, lambda key: _s.md.cell(key, layers=("roads",), kind="demand").layers.get("roads"), elev, setup)
        elif got.elev is None and elev is not None:     # (made as another's preferred one, reading only)
            got.elev = elev
            for st in list(got.stores.values()):
                st.elev = elev
        return got


def _bridge_setup(p):
    sea = getattr(_s, "sea", None)
    p.wet = sea.depth if sea is not None else None        # (decks clear the water: roads.water_clear)
    p.infra_fn = lambda key: _s.md.cell(key, layers=("infrastructure",), kind="demand").layers.get(
        "infrastructure")                                   # (bridge:structure tags: bridgetypes)


def _bridges(aid, frame, elev):
    """The anchor's view (roads.ChainView) of this process's bridge chains (deck heights of whole chains, each solved
    once whatever the anchor: roads.ChainStore)."""
    import roads as roadsmod
    return roadsmod.ChainView(_chain_store("bridges", roadsmod.BridgeProfiles, elev, _bridge_setup), frame)


NAV_GRADED_M = 900.0        # m (Chebyshev) round the AI graph's centre where its ground is graded in full: the road tiles'
                            # reach (roads.SEND_M + a tile), whose cells they plan anyway; beyond, only planned cells


def _nav_bridges(aid, frame, elev):
    """The AI graph's view of the bridge chains: the road tiles' decks where solved (one profile per chain), else its
    own chains' (elev grades only the planned cells)."""
    import roads as roadsmod
    return roadsmod.ChainView(_chain_store("nav_bridges", roadsmod.BridgeProfiles, elev, _bridge_setup), frame,
                              prefer=_chain_store("bridges", roadsmod.BridgeProfiles, elev, _bridge_setup))


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
               mph=True, q=None, gas_stations=False, buried=0.0, lamp_region=None, country=None):
    """earthd.Server.build_roads_tile here (road-tile pool): the anchor's road tile (tx, ty) built and
    written; returns roads.write_tile's (vfs path, chunks) and its stations' pads (gasstations.tile_sites pads: the
    server keeps them for the ground's painters). In the server's process its Python made up
    ~80 % of the main process's busy threads (in-game profile 2026-10-13): the block photos and far
    textures waited for the GIL. A tile built in this anchor's place before comes from the revisit cache."""
    watch = _stack_watch(f"road tile {tx}_{ty}")
    try:
        return _roads_tile_cached(aid, lat, lon, zoom, tx, ty, out_dir, vfs_dir, sidewalks, lift, buildings,
                                  left_hand, mph, q, gas_stations, buried, lamp_region, country)
    finally:
        watch.cancel()


STACK_DUMP_S = float(os.environ.get("BNGE_STACK_DUMP_S", "") or 75.0)   # s before a slow tile's stacks go to the log


def signs_tile(aid, lat, lon, zoom, tx, ty, out_dir, vfs_dir, left_hand=False, mph=True, country=None):
    """signstream.build_tile here: the highway signs of a road tile the mod has (from cody196's PR #363), in a road
    process: its map cells are warm there. Returns (vfs path of the sign tile's JSON, number of meshes)."""
    import signstream
    g, _ = _s.anchor(aid, lat, lon)
    frame = geodesy.EnuFrame(lat, lon)

    def elev(la, lo):           # the graded ground, as the road tile's
        z = ground(frame, la, lo, _s.dem.sample(la, lo, zoom))
        return g.apply(la, lo, z, gradingmod.ROAD_LIFT) if g is not None else z
    return signstream.build_tile(_s.md, frame, aid, tx, ty, elev, out_dir, vfs_dir,
                                 _s.surface.urban if _s.surface is not None else None, _s.widths, left_hand, mph,
                                 country)


def _stack_watch(what):
    """A timer that writes every thread's stack of this worker to the server log (stderr) when what is still running
    after STACK_DUMP_S: where a hung road tile waits (the server's own stuck warning says only that it waits)."""
    def dump():
        import faulthandler
        import sys
        out = sys.stderr
        if out is None:
            return
        try:
            log.warning("%s still building after %.0f s in worker %d: its threads' stacks follow", what,
                        STACK_DUMP_S, os.getpid())
            out.flush()
            faulthandler.dump_traceback(file=out, all_threads=True)
            out.flush()
        except Exception:               # (diagnostics only)
            pass
    t = threading.Timer(STACK_DUMP_S, dump)
    t.daemon = True
    t.start()
    return t


def _roads_tile_cached(aid, lat, lon, zoom, tx, ty, out_dir, vfs_dir, sidewalks, lift, buildings, left_hand, mph, q,
                       gas_stations, buried, lamp_region, country=None):
    import revisit
    k = revisit.key("roads", round(float(lat), 7), round(float(lon), 7), zoom, tx, ty, bool(sidewalks), float(lift),
                    bool(buildings), bool(left_hand), bool(mph), None if q is None else float(q), bool(gas_stations),
                    _s.md.versions() if _s.md is not None else None, float(buried), lamp_region, country)
    wh = _where_enu(lat, lon, *roadsmod_tile_bbox(tx, ty),
                    sidewalks=bool(sidewalks), lift=float(lift), buildings=bool(buildings),
                    gas_stations=bool(gas_stations), md=_md_versions())
    return _revisit_tile("roads", k, aid, tx, ty, out_dir, vfs_dir, f"roads_{aid}_{tx}_{ty}.json",
                         lambda: _roads_tile(aid, lat, lon, zoom, tx, ty, out_dir, vfs_dir, sidewalks, lift, buildings,
                                             left_hand, mph, q, gas_stations, buried, lamp_region, country),
                         lambda res: {"n": int(res[1]), "pads": [[sid, [[np.asarray(la).tolist(), np.asarray(lo).tolist()]
                                                                       for la, lo in rs]] for sid, rs in res[2]]},
                         lambda path, extra: (path, int(extra.get("n", 0)),
                                              [(sid, [(np.asarray(la), np.asarray(lo)) for la, lo in rs])
                                               for sid, rs in extra.get("pads", [])]), wh)


def _md_versions():
    return _s.md.versions() if _s.md is not None else None


def roadsmod_tile_bbox(tx, ty):
    """A road tile's square (anchor frame, m): roads.tile_bbox."""
    import roads as roadsmod
    return roadsmod.tile_bbox(tx, ty)


def _where_enu(lat, lon, x0, y0, x1, y1, **settings):
    """revisit.where of an item over the box x0..x1, y0..y1 (m) of anchor lat / lon, built with these settings (the
    names earthd.Server.world_signature gives); None when it cannot be had."""
    import revisit
    try:
        f = geodesy.EnuFrame(lat, lon)
        la, lo, _ = f.enu_to_geodetic(np.array([x0, x1, x0, x1], float), np.array([y0, y0, y1, y1], float),
                                      np.zeros(4))
        la, lo = np.asarray(la, float), np.asarray(lo, float)
        return revisit.where(lat, lon, la.min(), lo.min(), la.max(), lo.max(), revisit.sig(**settings))
    except Exception:
        return None


def _revisit_tile(kind, k, aid, tx, ty, out_dir, vfs_dir, name, build, extra_of, result_of, where=None):
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
            revisit.store(_s.cache_dir, kind, k, blobs, extra_of(res), where=where)
        except Exception as e:          # (not kept rather than no tile)
            log.debug("revisit: %s tile %d_%d not kept: %s: %s", kind, tx, ty, type(e).__name__, e)
    return res


def _write_bytes(path, data):
    """path written atomically (staging: the game sees only finished files)."""
    tmp = staging.tmp_for(path)
    with open(tmp, "wb") as f:
        f.write(data)
    staging.replace(tmp, path)


# Phase 0 of the origin-independent tiles (geotiles): a road tile's stages timed. "content" stages make what does not
# depend on the anchor but for its placement (buildings, decks, tunnels, walls, wires, rails, props, guardrails,
# parking, fuel, wells, sound); "skin" stages follow the anchor's 2 m lattice and height quantum (the draped road mesh,
# plazas, sidewalks / walk collision in write_tile); "shared" are the stores both read (grading plans, chain profiles).
TILE_STAGE_KIND = {"warm": "shared", "anchor": "shared", "profiles": "shared", "fuel": "content", "items": "content",
                   "build_tile": "content", "rails": "content", "airports": "content", "doors_park": "content",
                   "markings": "content", "guardrails": "content", "medians": "content", "mesh": "skin",
                   "plazas": "skin", "deck_mesh": "content", "tunnel_mesh": "content", "railways": "content",
                   "bld": "content", "powerlines": "content", "write": "skin"}
_tile_stages = threading.local()


def last_tile_stages():
    """{stage: s} of the last road tile this thread built (_roads_tile; {} before one or on a revisit hit)."""
    return dict(getattr(_tile_stages, "last", {}))


def _stage_clock():
    """(stages {name: s}, mark(name)): this thread's road tile stage timers, started now (last_tile_stages)."""
    stages = _tile_stages.last = {}
    clock = [time.perf_counter()]

    def mark(name):
        t = time.perf_counter()
        stages[name] = stages.get(name, 0.0) + t - clock[0]
        clock[0] = t
    return stages, mark


class _Parts:
    """What a road tile is built from and made of (_tile_parts): its chunks and their context, its sidewalk pieces,
    the meshes and rows of everything else."""


def _tile_parts(mark, frame, tile, zoom, g, buildings, left_hand, mph, gas_stations, buried, lamp_region, country,
                q, rkey, centre, skin_margin=0.0, walks_raw=None):
    """A road tile's parts in frame: tile (tx, ty) of an anchor, or a geotiles.GeoBox (its own frame, no anchor in
    it). g: the frame's grading.Grader view (None: no grading); q: the height quantum the sidewalks' curbs are put on
    (None: the ground as it is: a geotile's content); rkey: the railways' ground cache key; centre (lat, lon): the
    place's (the brick pavement's region). skin_margin, walks_raw: roads.build_tile's (a geotile's skin pieces)."""
    import parking as parkingmod
    import rails as railsmod
    import roadmesh as roadmeshmod
    import roads as roadsmod
    tx, ty = (tile, None) if hasattr(tile, "box") else tile
    P = _Parts()
    # everything the tile will ask for, fetched at once (one after another, a fresh place's first tile took ~1 min)
    _s.md.warm(roadsmod.tile_needs(frame, tx, ty, buildings, _s.widths is not None,
                                   1 + seacarvemod.LINE_CELLS if _s.sea is not None else 0))
    if getattr(_s, "fra", None) is not None:       # (the US's level crossings' devices, alongside)
        _s.fra.prefetch(roadsmod.cells_of_tile(frame, tx, ty))
    mark("warm")
    cells = [_s.md.cell(k, layers=("roads",), kind="demand").layers.get("roads")
             for k in roadsmod.cells_of_tile(frame, tx, ty)]
    mark("anchor")

    def elev(la, lo):           # the physics ground (lifted under roads: grading.ROAD_LIFT)
        z = ground(frame, la, lo, _s.dem.sample(la, lo, zoom))
        return g.apply(la, lo, z, gradingmod.ROAD_LIFT) if g is not None else z
    P.elev = elev
    bridges = _bridges(None, frame, elev)
    tunnels = tunnel_profiles(None, frame, elev, g)
    P.bridges, P.tunnels = bridges, tunnels
    mark("profiles")
    walks = []
    urban = _s.surface.urban if _s.surface is not None else None
    fuel, skip, fuel_cuts, fuel_pads = [], frozenset(), [], []
    if gas_stations:                    # pumps and refuel areas; their canopy outlines left out of the buildings
        import gasstations
        fuel, skip = gasstations.tile_sites(frame, tx, ty, _s.md, elev, cuts=fuel_cuts, pads=fuel_pads)
    mark("fuel")
    props = [] if buildings else None
    lines = [] if buildings else None
    wires = [] if buildings else None
    park = [] if buildings else None
    items = town = None
    wrows = []
    box = roadsmod.tile_bbox(tx, ty)
    side = roadsmod.tile_side(tx, ty)
    if buildings:
        import buildings as buildingsmod
        # stock towers, containers, billboards and barns; a replaced barn's id is left out of the buildings
        try:
            import worldprops
            wrows, barn_ids = worldprops.tile_props(frame, tx, ty, _s.md, elev)
            skip = frozenset(skip) | frozenset(barn_ids)
        except Exception as ex:
            log.warning("world props: tile %s_%s: %s: %s", tx, ty, type(ex).__name__, ex)
            wrows = []
        # (on the graded ground, the one the terrain is drawn with: on the raw elevation a building beside a road's
        # cutting stood over the cut; hashk014's PR #69)
        items = buildingsmod.road_tile_items(frame, tx, ty, _s.md, elev, decks=_tile_decks(bridges, cells, tx, ty),
                                             skip=skip)
        if wrows:
            props.extend(wrows)
        town = buildingsmod.coverage(items, side) < buildingsmod.POLES_COVERAGE
    mark("items")
    rwalls = []
    ctx = []
    cell_fn = lambda key: _s.md.cell(key, layers=("roads",), kind="demand").layers.get("roads")   # noqa: E731

    def drawn(la, lo):          # the block's ground: raised over the low arches as the terrain is (_make_heightmap)
        return roadsmod.block_arch_raise(tunnels, cell_fn, frame, la, lo, elev(la, lo),
                                         (box[0] - 8, box[1] - 8, box[2] + 8, box[3] + 8))
    P.drawn = drawn
    cover = []                          # the ground carriageways for the decks' parapets (roadmesh.deck ground=)
    marks = []                          # stall lines along parking aisles (parking.finish_markings adds the lot polygons')
    near_roads = []                     # the ground carriageways near the tile (the median barriers': medians.tile_rows)
    chunks = roadsmod.build_tile(frame, tx, ty, cells, elev, urban, walks=walks, deck_context=ctx, bridges=bridges, widths=_s.widths,
                                 walk_cuts=fuel_cuts,    # (the stations' made-up driveways: no sidewalk across them)
                                 props=props, lines=lines, wires=wires, town=town, park=park,
                                 left_hand=left_hand, mph=mph, buried=buried, lamp_region=lamp_region, country=country,
                                 structures=buildingsmod.centres(items, tx, ty) if items else (),
                                 tunnels=tunnels, walls=rwalls,
                                 ground=lambda x, y: roadmeshmod.drawn_ground(frame, drawn, np.c_[x, y], q),
                                 terrain_paint=getattr(_s, "surface", None) is not None, cover_out=cover, marks=marks,
                                 roads_out=near_roads, skin_margin=skin_margin, walks_raw=walks_raw,
                                 signals=furniture.signals_for(_s.md, frame,
                                                               roadsmod.cells_of_tile(frame, tx, ty, SIGNALS_MARGIN_M)))
    mark("build_tile")
    chunks += railsmod.tile_chunks(frame, tx, ty, cells)     # railway and tram tracks on the ground
    mark("rails")
    if _s.airports is not None:         # runway markings, drawn by the road mesh like the lane lines
        chunks += _s.airports.tile_chunks(frame, tx, ty, elev, roadsmod.tile_bbox)
    mark("airports")
    x0, y0, _, _ = box
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
        buildingsmod.brick_walks(walks, items, centre[0], centre[1], side)
        if walks_raw is not None:
            buildingsmod.brick_walks(walks_raw, items, centre[0], centre[1], side)
    mark("doors_park")
    chunks += parkingmod.finish_markings(frame, tx, ty, _s.md, elev, items, marks)
    mark("markings")
    # Share the bridge mesh's exact parapet geometry with the tile builder.
    # (the junction pads on the bridges round the tile: their decks' parapets stop at them; the tile draws its own:
    # roads.deck_pads)
    try:
        pad_parts = list(bridges.parts(cells, (box[0] - 40, box[1] - 40, box[2] + 40, box[3] + 40)))
        pads_near = roadsmod.deck_pads([(f, sp, xy, z) for f, sp, xy, z, _lon in pad_parts], near_roads, box,
                                       bridges._ground, own=False)
    except Exception as ex:
        log.warning("deck pads: tile %s_%s: %s: %s", tx, ty, type(ex).__name__, ex)
        pads_near = []
    pads = [p for p in pads_near if roadsmod.box_owns(box, float(p[1][0]), float(p[1][1]))]
    parapets = roadmeshmod.parapet_runs(chunks, ctx, ground=cover, pads=pads_near)
    import guardrails as guardrailsmod
    infra_fn = lambda key: _s.md.cell(key, layers=("infrastructure",), kind="demand").layers.get("infrastructure")
    low_parapet_decks = set()
    deck_parts = []                 # the bridges' parts near the tile (the median barriers' on joined decks too)
    try:
        deck_parts = list(bridges.parts(cells, (box[0]-10, box[1]-10, box[2]+10, box[3]+10)))
        guardrail_decks = [{"n": np.column_stack((xy, z)), "w": spec.width}
                           for _fid, spec, xy, z, _lon in deck_parts]
        guardrail_rows = guardrailsmod.forest_rows(frame, infra_fn, box, elev,
                                                   decks=guardrail_decks, parapets=parapets,
                                                   bridge_rails=roadmeshmod.BRIDGE_RAILS,
                                                   supported_decks_out=low_parapet_decks)
    except Exception as ex:
        log.warning("mapped guardrails: tile %s_%s: %s: %s", tx, ty, type(ex).__name__, ex)
        low_parapet_decks.clear()
        guardrail_rows = []
    mark("guardrails")
    guardrail_rows = guardrail_rows + _median_rows(frame, near_roads, box, elev, left_hand, country, guardrail_rows,
                                                   tx, ty, deck_parts)
    mark("medians")
    P.dmesh = roadmeshmod.deck(chunks, (x0, y0, 0.0), bridges.piers_in(box), ctx, bridges.beams_in(box), ground=cover,
                               parapets=parapets, low_parapet_decks=low_parapet_decks, pads=pads)
    mark("deck_mesh")
    P.tmesh = roadmeshmod.tunnel(chunks, (x0, y0, 0.0), rwalls)
    mark("tunnel_mesh")
    import railways
    rcells = [cell_fn(k) for k in roadsmod.cells_of_tile(frame, tx, ty, railways.REACH_M)]
    rground = railways.point_ground(frame, elev, rkey)
    fra_kits = None                 # (the US: the FRA inventory's gates and lamps, else the road classes')
    if getattr(_s, "fra", None) is not None:
        try:
            fra_kits = _s.fra.kits(frame, roadsmod.cells_of_tile(frame, tx, ty))
        except Exception as e:
            log.debug("fra kits: %s: %s", type(e).__name__, e)
    rr_kits = railways.crossing_props(frame, tx, ty, rcells, rground, left_hand, fra_kits)   # level crossings
    if rr_kits:
        props = (props or []) + rr_kits
    P.rmesh = railways.tile_mesh(frame, tx, ty, rcells, rground,
                                 far_ground=railways.point_ground(
                                     frame, lambda la, lo: ground(frame, la, lo, _s.dem.sample(la, lo, zoom)),
                                     ("raw",) + tuple(rkey)))
    mark("railways")
    bld = None
    garage_sound = ()
    oil_wells = []
    if buildings:
        import buildings as buildingsmod
        import oilwells
        bld = buildingsmod.road_tile(frame, tx, ty, _s.md, elev, None, None, items)
        mark("bld")
        garage_sound = buildingsmod.garage_sound_boxes(items, *box[:2], roadsmod.SOUND_GARAGE)
        oil_wells = oilwells.tile_wells(frame, tx, ty, elev)     # (the mod's pump jacks)
        import powerlines
        # high-voltage towers and conductors, on the ground as the DEM has it: graded heights at towers up to a span
        # past the tile planned the grading of 12 more map cells per road tile (16 cells planned, 40 roads cells read
        # for one Szeged tile against 1.6.3's 4 and 16; the grading only moves ground near roads, which towers keep off)
        powerlines.tile_lines(frame, tx, ty, _s.md, lambda la, lo: ground(frame, la, lo, _s.dem.sample(la, lo, zoom)),
                              props, wires)
        import fences
        try:
            props.extend(fences.rows(frame, infra_fn, box, elev))
        except Exception as ex:
            log.warning("fences: tile %s_%s: %s: %s", tx, ty, type(ex).__name__, ex)
        import picnic
        land_fn = lambda key: _s.md.cell(key, layers=("land_use",), kind="demand").layers.get("land_use")
        bld_fn = lambda key: _s.md.cell(key, layers=("buildings",), kind="demand").layers.get("buildings")
        osm = getattr(_s.md, "picnic_osm", None)
        try:
            props.extend(picnic.rows(frame, infra_fn, land_fn, box, elev,
                                     osm.cells if osm is not None else None, bld_fn))
        except Exception as ex:
            log.warning("picnic: tile %s_%s: %s: %s", tx, ty, type(ex).__name__, ex)
        import shelters
        try:
            props.extend(shelters.rows(frame, infra_fn, box, elev, near_roads))
        except Exception as ex:
            log.warning("shelters: tile %s_%s: %s: %s", tx, ty, type(ex).__name__, ex)
        import industry
        garage_sound = (garage_sound + industry.refinery_sound_rows(frame, tx, ty, _s.md, elev)
                        + industry.power_plant_sound_rows(frame, tx, ty, _s.md, elev))
        mark("powerlines")
    P.chunks, P.ctx, P.walks, P.box, P.origin = chunks, ctx, walks, box, (x0, y0, 0.0)
    P.bld, P.props, P.wires, P.park, P.fuel, P.fuel_pads = bld, props, wires, park, fuel, fuel_pads
    P.guardrail_rows, P.garage_sound, P.wells = guardrail_rows, garage_sound, oil_wells
    return P


def _roads_tile(aid, lat, lon, zoom, tx, ty, out_dir, vfs_dir, sidewalks, lift, buildings, left_hand, mph, q,
                gas_stations, buried=0.0, lamp_region=None, country=None):
    """An anchor's road tile (tx, ty) built and written (the anchor tiles: --geo-tiles off, an older mod)."""
    import roadmesh as roadmeshmod
    import roads as roadsmod
    _stages, mark = _stage_clock()
    frame = geodesy.EnuFrame(lat, lon)
    g, _ = _s.anchor(aid, lat, lon)
    P = _tile_parts(mark, frame, (tx, ty), zoom, g, buildings, left_hand, mph, gas_stations, buried, lamp_region,
                    country, q, (aid, lat, lon), (lat, lon))
    mesh = roadmeshmod.build(frame, P.chunks, P.elev, P.origin, lift, clip=P.box, follow=True, q=q, drawn=P.drawn,
                             context=P.ctx)
    mark("mesh")
    mesh = _with_plazas(mesh, frame, P.box, P.elev, P.origin, getattr(_s, "surface", None))
    mark("plazas")
    out = roadsmod.write_tile(out_dir, vfs_dir, aid, tx, ty, P.chunks, P.walks if sidewalks else None, mesh, P.dmesh,
                              P.bld, P.props, P.wires, P.park, tunnel_mesh=P.tmesh, fuel=P.fuel, rail_mesh=P.rmesh,
                              guardrail_rows=P.guardrail_rows, garage_sound=P.garage_sound,
                              wells=P.wells) + (P.fuel_pads,)
    mark("write")
    return out


# --- road geotiles (phase 1c): content once per geotile, a skin per anchor ----------------------------------------

GEO_SKIN_MARGIN_M = 3.0     # m past a geotile's box its content keeps the skin's pieces (roads.build_tile skin_margin)
GEO_FRAMES_KEPT = 64        # geotile frames' grading views kept per process (_frame_grader)
GEO_INPUTS_KEPT = 96        # geotiles' skin inputs kept in memory per process (_skin_inputs)
EXACT_TOL_COL_M = 0.03      # m a colliding part placed rigidly may be off before it gets its exact variant ...
EXACT_TOL_M = 0.10          # ... and a render-only part
GEO_COLLIDING = ("bldcol", "deck", "tunnel", "railcol", "rail")     # parts the car hits ("bld*" too without a bldcol)
_geo_lock = threading.Lock()
_geo_graders = {}           # (lat0, lon0) -> grading.Grader view of a geotile's frame
_geo_inputs = {}            # geotile key -> its skin inputs (dict), the most recently used last


def _frame_grader(frame):
    """A grading.Grader view of frame (a geotile's), sharing the process's PlanStore (heights: the planning regions',
    whatever the frame); None without grading."""
    if not _s.grading:
        return None
    k = (float(frame.lat0), float(frame.lon0))
    with _geo_lock:
        got = _geo_graders.get(k)
        if got is not None:
            return got
    with _s.lock:
        if getattr(_s, "plan_store", None) is None:     # (one store for every frame: _State.anchor's)
            _s.plan_store = gradingmod.PlanStore(
                _s.md, lambda la, lo: _s.dem.sample(la, lo, gradingmod.PROFILE_ZOOM), _s.widths)
            _s.plan_store.tunnel_store = lambda: _s.__dict__.get("chain_stores", {}).get("tunnels")
    gr = gradingmod.Grader(frame, _s.md, _s.plan_store.elev, _s.widths, store=_s.plan_store)
    gr.wet = _s.sea.depth if getattr(_s, "sea", None) is not None else None
    gr.dem = _s.dem
    if getattr(_s.plan_store, "shared", None) is None:
        gr.shared = plancache.for_grader(_s.cache_dir, gr, _s.widths, gr.wet)
    with _geo_lock:
        got = _geo_graders.setdefault(k, gr)
        while len(_geo_graders) > GEO_FRAMES_KEPT:
            _geo_graders.pop(next(iter(_geo_graders)))
    return got


def geo_names(key, aid=None):
    """The session files of geotile key: {"json", "skin", what -> shape} of its content (aid None) or of anchor aid's
    skin ("json", "road", "walk", "walkcol")."""
    import roads as roadsmod
    if aid is None:
        out = {"json": f"rg_{key}.json", "inputs": f"rg_{key}.skin.pkl"}
        for what in roadsmod.shape_files(0, 0, 0):
            if what not in ("road", "walk", "walkcol"):
                out[what] = f"rg_{key}_{what}.cdae"
        return out
    return {"json": f"rs_{aid}_{key}.json", "road": f"rs_{aid}_{key}_road.cdae", "walk": f"rs_{aid}_{key}_walk.cdae",
            "walkcol": f"rs_{aid}_{key}_walkcol.cdae"}


def _shape_info(path, pos, what):
    """{shape, pos, pivot, relief, col} of a written content shape (earthfast.cdae_zstats: pivot the median height of
    its vertices in the tile's frame, relief the farthest one from it)."""
    import fast
    import geotiles
    fast.require(geotiles.NEED_CONTENT, "cdae_zstats")
    with open(path, "rb") as f:
        n, med, lo, hi = fast._ef.cdae_zstats(f.read())
    oz = float(pos[2])
    piv = oz + float(med) if n else oz
    rel = max(abs(oz + float(hi) - piv), abs(piv - oz - float(lo))) if n else 0.0
    return {"pivot": round(piv, 3), "relief": round(rel, 3)}


def _content_hash(text, vfs_dir):
    import hashlib
    return hashlib.sha1(text.replace(f"{vfs_dir}/", "@V@/").encode("utf-8")).hexdigest()[:16]


def _write_geo_content(out_dir, vfs_dir, tile, P):
    """A geotile's content written to the session folder: its shapes (rg_<key>_<what>.cdae, each with its pivot and
    relief), its rows (props, guard rails, parking, stations, wells, sound) and colliders in its own frame, and the
    skin's inputs (rg_<key>.skin.pkl: the chunks, their deck context and the sidewalk pieces). Returns (vfs path of
    the JSON, chunk count, its info for the tracker: {"h", "parts": {what: {pivot, relief, col}}})."""
    import json
    import pickle
    import roads as roadsmod
    names = geo_names(tile.key)
    # (written aside in the staging folder, outside the game's user folder: in the session folder each of its files was
    # created, written and deleted under the game's file watcher, "Handle error ... .rg_g17_..." in its log)
    import staging
    aside = staging.staging_dir(os.path.join(out_dir, "x")) or out_dir
    tmpdir = os.path.join(aside, f".rg_{tile.key}_{os.getpid()}_{threading.get_ident()}")
    os.makedirs(tmpdir, exist_ok=True)
    try:
        # (roads.write_tile writes the shapes and the rows; its names are the anchor tile's: written aside, renamed)
        _, n = roadsmod.write_tile(tmpdir, vfs_dir, 0, tile, None, P.chunks, None, None, P.dmesh, P.bld, P.props,
                                   P.wires, P.park, tunnel_mesh=P.tmesh, fuel=P.fuel, rail_mesh=P.rmesh,
                                   guardrail_rows=P.guardrail_rows, garage_sound=P.garage_sound, wells=P.wells,
                                   decals=False)
        with open(os.path.join(tmpdir, f"roads_0_{tile}_None.json"), encoding="utf-8") as f:
            data = json.loads(f.read())
        files = roadsmod.shape_files(0, tile, None)
        parts = {}
        for what, fname in files.items():
            e = data.get(what)
            src = os.path.join(tmpdir, fname)
            if not isinstance(e, dict) or not os.path.exists(src):
                continue
            info = _shape_info(src, e["pos"], what)
            dst = names[what]
            with open(src, "rb") as fh:
                _write_bytes(os.path.join(out_dir, dst), fh.read())
            e["shape"] = f"{vfs_dir}/{dst}"
            e.update(info)
            parts[what] = dict(info)
        data["chunks"] = []                 # (the draped surfaces are the skin's)
        data.pop("walk", None)
        data.pop("walkcol", None)
        data.pop("road", None)
        cols = data.get("colliders") or {}
        for w in ("walk", "walkcol"):
            cols.pop(w, None)
        if cols:
            data["colliders"] = cols
        else:
            data.pop("colliders", None)
        lean = "bldcol" in parts
        for what, info in parts.items():
            info["col"] = what in GEO_COLLIDING or (what.startswith("bld") and not lean)
        data["key"] = tile.key
        data["frame"] = [float(tile.frame.lat0), float(tile.frame.lon0)]
        data["box"] = [round(float(v), 4) for v in tile.box]
        text = json.dumps(data, separators=(",", ":"), sort_keys=True)
        h = _content_hash(text, vfs_dir)
        data["h"] = h
        text = json.dumps(data, separators=(",", ":"), sort_keys=True)
        _write_bytes(os.path.join(out_dir, names["json"]), text.encode("utf-8"))
        inputs = {"chunks": P.chunks, "ctx": P.ctx, "walks": P.walks_raw, "h": h}
        _write_bytes(os.path.join(out_dir, names["inputs"]), pickle.dumps(inputs, protocol=4))
        with _geo_lock:
            _geo_inputs[tile.key] = inputs
            while len(_geo_inputs) > GEO_INPUTS_KEPT:
                _geo_inputs.pop(next(iter(_geo_inputs)))
        return f"{vfs_dir}/{names['json']}", n, {"h": h, "parts": parts}
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def _geo_content_key(key, zoom, buildings, left_hand, mph, gas_stations, buried, lamp_region, country, md=None):
    """The revisit key of a geotile's content (md: the map data's versions, when not this worker's: the server's own
    check, earthd.Server.geo_prebuilt)."""
    import revisit
    return revisit.key("roadsg", str(key), int(zoom), bool(buildings), bool(left_hand), bool(mph), bool(gas_stations),
                       _md_versions() if md is None else md, float(buried), lamp_region, country, GEO_SKIN_MARGIN_M)


def prebuild_geo(gx, gy, zoom, scratch, buildings, left_hand, mph, gas_stations, buried=0.0, lamp_region=None,
                 country=None):
    """Saved areas' world phase (areas.py, the user 2026-10-29: "pre generate along a route or area"): geotile (gx, gy)'s
    road content (_geo_content: roads, sidewalks, bridges, buildings, street furniture) built into the revisit cache
    ahead of play; nothing for the game (its files written to scratch, then removed). The revisit key when the content
    is in the cache after (built now or before), else None (something it read was missing: tried again later)."""
    import geotiles
    import revisit
    tile_key = geotiles.geobox(gx, gy).key
    k = _geo_content_key(tile_key, zoom, buildings, left_hand, mph, gas_stations, buried, lamp_region, country)
    path = revisit._path(_s.cache_dir, "roadsg", k)
    if not os.path.exists(path):
        os.makedirs(scratch, exist_ok=True)
        try:
            _geo_content(gx, gy, zoom, scratch, "/x", buildings, left_hand, mph, gas_stations, buried, lamp_region,
                         country)
        finally:
            for name in geo_names(tile_key).values():
                try:
                    os.remove(os.path.join(scratch, name))
                except OSError:
                    pass
            with _geo_lock:
                _geo_inputs.pop(tile_key, None)
    return k if os.path.exists(path) else None


def _geo_content(gx, gy, zoom, out_dir, vfs_dir, buildings, left_hand, mph, gas_stations, buried, lamp_region,
                 country):
    """Geotile (gx, gy)'s content (_write_geo_content), from the revisit cache (kind "roadsg": its key, settings and
    code; no anchor, no height quantum, no lift) or built in its own frame. (vfs path, chunks, info, fuel pads)."""
    import geotiles
    import revisit
    tile = geotiles.geobox(gx, gy)
    names = geo_names(tile.key)
    k = _geo_content_key(tile.key, zoom, buildings, left_hand, mph, gas_stations, buried, lamp_region, country)
    got = revisit.load(_s.cache_dir, "roadsg", k)
    if got is not None:
        blobs, extra = got
        for name, data in blobs.items():
            if name.endswith(".json"):
                data = data.decode("utf-8").replace("@V@/", f"{vfs_dir}/").encode("utf-8")
            _write_bytes(os.path.join(out_dir, name), data)
        _geo_stats["content_hits"] += 1
        return (f"{vfs_dir}/{names['json']}", int(extra.get("n", 0)), extra.get("info") or {},
                [(sid, [(np.asarray(la), np.asarray(lo)) for la, lo in rs]) for sid, rs in extra.get("pads", [])])
    _stages, mark = _stage_clock()
    f0 = revisit.faults()
    g = _frame_grader(tile.frame)
    walks_raw = []
    P = _tile_parts(mark, tile.frame, tile, zoom, g, buildings, left_hand, mph, gas_stations, buried, lamp_region,
                    country, None, ("g", tile.key), geotiles.centre(gx, gy), GEO_SKIN_MARGIN_M, walks_raw)
    P.walks_raw = walks_raw
    path, n, info = _write_geo_content(out_dir, vfs_dir, tile, P)
    mark("write")
    _geo_stats["content_built"] += 1
    if revisit.ON and revisit.faults() == f0:     # (complete: nothing it read failed or was missing)
        try:
            blobs = {}
            for what, name in names.items():
                p = os.path.join(out_dir, name)
                if os.path.exists(p):
                    with open(p, "rb") as fh:
                        data = fh.read()
                    if name.endswith(".json"):
                        data = data.decode("utf-8").replace(f"{vfs_dir}/", "@V@/").encode("utf-8")
                    blobs[name] = data
            s, w, nn, e = geotiles.info(gx, gy)[2:]
            wh = revisit.where(*geotiles.centre(gx, gy), s, w, nn, e,
                               revisit.sig(buildings=bool(buildings), gas_stations=bool(gas_stations),
                                           md=_md_versions()))
            revisit.store(_s.cache_dir, "roadsg", k, blobs,
                          {"n": int(n), "info": info,
                           "pads": [[sid, [[np.asarray(la).tolist(), np.asarray(lo).tolist()] for la, lo in rs]]
                                    for sid, rs in P.fuel_pads]}, where=wh)
        except Exception as ex:          # (not kept rather than no tile)
            log.debug("revisit: geotile %s not kept: %s: %s", tile.key, type(ex).__name__, ex)
    return path, n, info, P.fuel_pads


def _skin_inputs(key, out_dir, content):
    """The skin inputs of geotile key (_write_geo_content): kept in memory, else its session file, else content()
    (its content built or loaded again, which writes the file). None when there are none."""
    import pickle
    with _geo_lock:
        got = _geo_inputs.get(key)
    if got is not None:
        return got
    p = os.path.join(out_dir, geo_names(key)["inputs"])
    if not os.path.exists(p):
        content()
    try:
        with open(p, "rb") as f:
            got = pickle.load(f)
    except (OSError, EOFError, pickle.UnpicklingError):
        return None
    with _geo_lock:
        _geo_inputs[key] = got
        while len(_geo_inputs) > GEO_INPUTS_KEPT:
            _geo_inputs.pop(next(iter(_geo_inputs)))
    return got


def _map_chunks(src, dst, chunks):
    """Chunks of src's world in dst's world (geotiles.world_map: their nodes exactly, heights too: a deck's surface;
    a filled polygon's corners)."""
    import geotiles
    if not chunks:
        return []
    pts, idx = [], []
    for k, c in enumerate(chunks):
        n = c.get("n")
        if n is not None and len(n):
            a = np.asarray(n, np.float64)
            pts.append(np.c_[a[:, :2], a[:, 2] if a.shape[1] > 2 else np.zeros(len(a))])
            idx.append((k, "n", len(a), a.shape[1]))
        pq = c.get("poly")
        if pq is not None and len(pq):
            a = np.asarray(pq, np.float64)
            pts.append(np.c_[a[:, :2], a[:, 2] if a.shape[1] > 2 else np.zeros(len(a))])
            idx.append((k, "poly", len(a), a.shape[1]))
    out = [dict(c) for c in chunks]
    if not pts:
        return out
    m = geotiles.world_map(src, dst, np.concatenate(pts))
    at = 0
    for k, field, ln, cols in idx:
        v = m[at:at + ln, :max(2, min(cols, 3))]
        at += ln
        out[k][field] = v if field == "n" else v.tolist()
    return out


def _map_walks(src, dst, walks):
    """Sidewalk pieces (roads.build_tile walks, before draping) of src's world in dst's: their points exactly
    (geotiles.world_map), a corner's inward vectors turned with the frames."""
    import geotiles
    if not walks:
        return []
    r, _t, _c = geotiles.placement(src, dst, (0.0, 0.0, 0.0))
    r2 = r[:2, :2]

    def mp(a):
        a = np.asarray(a, np.float64)
        if not len(a):
            return a
        q = np.c_[a[:, :2], a[:, 2] if a.shape[1] > 2 else np.zeros(len(a))]
        w = geotiles.world_map(src, dst, q)
        return w[:, :max(2, min(a.shape[1], 3))]
    out = []
    for wk in walks:
        wk = dict(wk)
        if "corner" in wk:
            c = dict(wk["corner"])
            c["arc"] = mp(c["arc"])
            c["outer"] = [tuple(v) for v in mp(np.asarray(c["outer"], np.float64).reshape(-1, 3))]
            inw = np.asarray(c["inward"], np.float64).reshape(-1, 2) @ r2.T
            c["inward"] = inw / np.maximum(np.hypot(*inw.T), 1e-12)[:, None]
            wk["corner"] = c
        else:
            wk["pts"] = mp(wk["pts"])
        out.append(wk)
    return out


def _clip_mesh(mesh, origin, quad):
    """A mesh (positions relative to origin, tris, uvs, materials, tri material) cut to quad (4, 2: a geotile's, in the
    mesh's world; earthfast.clip_tris_convex), or None when nothing is left."""
    import fast
    import geotiles
    if mesh is None:
        return None
    P, T, UV, mats, M = mesh
    if not len(T):
        return None
    fast.require(geotiles.NEED_CONTENT, "clip_tris_convex")
    o = np.asarray(origin, np.float64)
    q = np.ascontiguousarray(np.asarray(quad, np.float64)[:, :2] - o[:2])
    P2, UV2, T2, src = fast._ef.clip_tris_convex(np.ascontiguousarray(np.asarray(P, np.float64)[:, :3]),
                                                 np.ascontiguousarray(np.asarray(UV, np.float64)[:, :2]),
                                                 np.ascontiguousarray(np.asarray(T, np.int64)), q)
    if not len(T2):
        return None
    used = np.unique(T2)
    remap = np.full(len(P2), -1, np.int64)
    remap[used] = np.arange(len(used))
    return P2[used], remap[T2], UV2[used], mats, np.asarray(M)[src]


def _geo_skin(aid, lat, lon, zoom, gx, gy, out_dir, vfs_dir, sidewalks, lift, q, content):
    """Anchor aid's skin of geotile (gx, gy): the draped road surfaces (the road mesh and plazas: roadmesh.build on the
    anchor's lattice and height quantum q, follow) and the sidewalks (curbs on the drawn ground, their lean collision
    mesh), from the content's skin inputs mapped into the anchor's world, cut exactly at the tile's quad there (its
    corners bit-equal with the neighbours': no overlap, no gap). Written as rs_<aid>_<key>*; returns (vfs path of its
    JSON, {"lift": m}) or None without inputs."""
    import json
    import cdae
    import geotiles
    import roadmesh as roadmeshmod
    import roads as roadsmod
    import sidewalks as sidewalksmod
    _stages, mark = _stage_clock()
    tile = geotiles.geobox(gx, gy)
    inputs = _skin_inputs(tile.key, out_dir, content)
    mark("inputs")
    if inputs is None:
        return None
    frame = geodesy.EnuFrame(lat, lon)
    g, _ = _s.anchor(aid, lat, lon)
    quad = np.asarray(geotiles.quad(frame, gx, gy), np.float64)
    qx0, qy0 = float(quad[:, 0].min()), float(quad[:, 1].min())
    qx1, qy1 = float(quad[:, 0].max()), float(quad[:, 1].max())
    bbox = (qx0, qy0, qx1, qy1)
    origin = (qx0, qy0, 0.0)

    def elev(la, lo):
        z = ground(frame, la, lo, _s.dem.sample(la, lo, zoom))
        return g.apply(la, lo, z, gradingmod.ROAD_LIFT) if g is not None else z
    tunnels = tunnel_profiles(aid, frame, elev, g)
    cell_fn = lambda key: _s.md.cell(key, layers=("roads",), kind="demand").layers.get("roads")   # noqa: E731

    def drawn(la, lo):
        return roadsmod.block_arch_raise(tunnels, cell_fn, frame, la, lo, elev(la, lo),
                                         (qx0 - 8, qy0 - 8, qx1 + 8, qy1 + 8))
    chunks = _map_chunks(tile.frame, frame, inputs.get("chunks") or [])
    ctx = _map_chunks(tile.frame, frame, inputs.get("ctx") or [])
    mark("map")
    mesh = roadmeshmod.build(frame, chunks, elev, origin, lift, clip=bbox, follow=True, q=q, drawn=drawn,
                             context=ctx, clip_poly=quad)
    mark("mesh")
    mesh = _with_plazas(mesh, frame, bbox, elev, origin, getattr(_s, "surface", None), quad=quad)
    mark("plazas")
    names = geo_names(tile.key, aid)
    data = {"v": roadsmod.VERSION, "key": tile.key, "anchorId": aid, "lift": round(float(lift), 3),
            "q": float(q) if q else 0.0}
    # its pivot: the quad's centre at the median height of its road mesh (the mod moves a kept skin there at a rebase)
    pz = float(np.median(np.asarray(mesh[0], np.float64)[:, 2])) if mesh is not None and len(mesh[0]) else 0.0
    data["pivot"] = [round(float(quad[:, 0].mean()), 3), round(float(quad[:, 1].mean()), 3), round(pz, 3)]
    if mesh is not None:
        roadmeshmod.write(os.path.join(out_dir, names["road"]), mesh)
        data["road"] = {"shape": f"{vfs_dir}/{names['road']}", "pos": list(origin)}
    elif chunks:                    # (no mesh: the mod's DecalRoads, as an anchor tile's)
        data["chunks"] = [dict(c, n=np.asarray(c["n"]).tolist()) if c.get("n") is not None else c
                          for c in chunks if not c.get("tube_only")]
    walks = _map_walks(tile.frame, frame, inputs.get("walks") or []) if sidewalks else []
    if walks:
        roadsmod.drape_walks(frame, elev, walks,
                             lambda x, y: roadmeshmod.drawn_ground(frame, drawn, np.c_[x, y], q))
        mark("drape")
        b = sidewalksmod.Builder(origin, collision=True)
        for wk in walks:
            if "corner" in wk:
                c = wk["corner"]
                b.add_corner(c["arc"], c["inward"], c["outer"], ramps=c.get("ramps", ()))
                continue
            ww = wk.get("w", roadsmod.SIDEWALK["w"])
            curb, outv = sidewalksmod.run_geometry(wk["pts"], wk["side"], ww)
            b.add_run(curb, outv, ww, caps=wk["caps"], ramps=wk.get("ramps", ()),
                      slab=sidewalksmod.BRICK if wk.get("brick") else 1)
        mark("walks")
        if not b.empty():
            P_, T_, UV_, M_ = b.arrays()
            wm = _clip_mesh((P_, T_, UV_, list(sidewalksmod.MATERIALS), M_), origin, quad)
            if wm is not None:
                cdae.write(os.path.join(out_dir, names["walk"]), wm[0], wm[1], uvs=wm[2], material=wm[3],
                           tri_materials=wm[4], object_name="walk")
                data["walk"] = {"shape": f"{vfs_dir}/{names['walk']}", "pos": list(origin)}
                P_, T_, UV_, M_ = b.col.arrays()
                cm = _clip_mesh((P_, T_, UV_, [sidewalksmod.MATERIALS[0]], M_), origin, quad)
                if cm is not None:
                    cdae.write(os.path.join(out_dir, names["walkcol"]), cm[0], cm[1], uvs=cm[2],
                               material=sidewalksmod.MATERIALS[0], collision_only=True)
                    data["walkcol"] = {"shape": f"{vfs_dir}/{names['walkcol']}", "pos": list(origin)}
                boxes = roadsmod.collider_boxes(walks, None)
                if boxes.get("walk"):
                    data["colliders"] = {"walk": boxes["walk"]}
                    if "walkcol" in data:
                        data["colliders"]["walkcol"] = boxes["walk"]
    mark("write")
    _write_bytes(os.path.join(out_dir, names["json"]), json.dumps(data, separators=(",", ":")).encode("utf-8"))
    _geo_stats["skins"] += 1
    return f"{vfs_dir}/{names['json']}", {"lift": float(lift)}


_geo_stats = {"content_built": 0, "content_hits": 0, "skins": 0, "exact": 0}


def geo_stats():
    """This process's road geotile counters (content built / from the revisit cache, skins, exact variants)."""
    return dict(_geo_stats)


def _geo_exact(aid, lat, lon, gx, gy, out_dir, vfs_dir, parts):
    """Exact variants of geotile (gx, gy)'s parts for anchor aid (parts: {what: {pivot}}): each shape's vertices moved
    by s (z - pivot) (earthfast.cdae_shear; s = R^T c of geotiles.placement at the pivot), so the mod's rigid
    placement there lands them where the exact world map does. {what: vfs path of rx_<aid>_<key>_<what>.cdae}."""
    import fast
    import geotiles
    fast.require(geotiles.NEED_CONTENT, "cdae_shear")
    tile = geotiles.geobox(gx, gy)
    frame = geodesy.EnuFrame(lat, lon)
    names = geo_names(tile.key)
    out = {}
    import json
    try:
        with open(os.path.join(out_dir, names["json"]), encoding="utf-8") as f:
            data = json.loads(f.read())
    except (OSError, ValueError):
        return out
    for what, info in parts.items():
        e = data.get(what)
        src = os.path.join(out_dir, names.get(what, ""))
        if not isinstance(e, dict) or not os.path.exists(src):
            continue
        piv = float(info.get("pivot", e.get("pivot", 0.0)))
        r, _t, c = geotiles.placement(tile.frame, frame, (0.0, 0.0, piv))
        s = r.T @ c
        with open(src, "rb") as f:
            blob = fast._ef.cdae_shear(f.read(), float(e["pos"][2]), float(s[0]), float(s[1]), float(s[2]), piv)
        name = f"rx_{aid}_{tile.key}_{what}.cdae"
        _write_bytes(os.path.join(out_dir, name), blob)
        out[what] = f"{vfs_dir}/{name}"
        _geo_stats["exact"] += 1
    return out


def roads_geo(aid, lat, lon, zoom, gx, gy, gzoom, out_dir, vfs_dir, sidewalks, lift, buildings, left_hand, mph, q,
              gas_stations, buried=0.0, lamp_region=None, country=None, content=True, skin=True, exact=None):
    """earthd.Server.build_roads_geo here (road-tile pool): geotile (gx, gy)'s content (content: built in its own
    frame, or from the revisit cache; gzoom its DEM zoom, the rules its own place's), anchor aid's skin (skin) and its
    parts' exact variants for that anchor (exact: {what: {pivot}}, or "auto": those of the content built here that its
    rigid placement would leave off, roads.geo_exact_auto). Returns {"content": (vfs path, chunks, info,
    fuel pads) or None, "skin": (vfs path, info) or None, "exact": {what: vfs path}, "stages": {...}}."""
    watch = _stack_watch(f"road geotile {gx}_{gy}")
    try:
        out = {"content": None, "skin": None, "exact": {}, "stages": {}}
        t0 = time.perf_counter()
        c0 = time.process_time()

        def build_content():
            return _geo_content(gx, gy, gzoom, out_dir, vfs_dir, buildings, left_hand, mph, gas_stations, buried,
                                lamp_region, country)
        if content:
            out["content"] = build_content()
            out["stages"]["content"] = last_tile_stages()
        t1, c1 = time.perf_counter(), time.process_time()
        if skin:
            out["skin"] = _geo_skin(aid, lat, lon, zoom, gx, gy, out_dir, vfs_dir, sidewalks, lift, q, build_content)
            out["stages"]["skin"] = last_tile_stages()
        t2, c2 = time.perf_counter(), time.process_time()
        if exact == "auto":             # (a fresh content: the parts its anchor needs exact, roads.geo_exact_auto)
            import geotiles
            import roads as roadsmod
            info = out["content"][2] if out["content"] is not None else {}
            exact = roadsmod.geo_exact_auto(geotiles.frame(gx, gy), geodesy.EnuFrame(lat, lon), info.get("parts"))
        if exact:
            out["exact"] = _geo_exact(aid, lat, lon, gx, gy, out_dir, vfs_dir, exact)
        out["time"] = {"content_s": t1 - t0, "content_cpu": c1 - c0, "skin_s": t2 - t1, "skin_cpu": c2 - c1,
                       "exact_s": time.perf_counter() - t2}
        return out
    finally:
        watch.cancel()


def _median_rows(frame, near_roads, box, elev, left_hand, country, mapped, tx, ty, decks=()):
    """The median barriers' Forest rows of a road tile (medians.tile_rows; none where the mapped rows have one; decks:
    the bridges' parts near it), [] if they fail (logged: the tile goes on without them)."""
    import medians
    try:
        return medians.tile_rows(frame, near_roads, box, elev, left_hand, country, mapped, decks)
    except Exception as ex:
        log.warning("median barriers: tile %s_%s: %s: %s", tx, ty, type(ex).__name__, ex)
        return []


PLAZA_GEO_MARGIN_M = 12.0    # m past a geotile's quad its skin's plazas are made (then cut exactly at the quad)


def _with_plazas(mesh, frame, box, elev, origin, surface, quad=None):
    """The road mesh with the tile's squares and pedestrian areas paved (plazas.py) over box (x0, y0, x1, y1), from the
    cached land use (none when it is not cached yet: surface.Surface.lots never waits). quad (4, 2): a geotile's skin:
    the plazas made PLAZA_GEO_MARGIN_M past the box and cut exactly at the quad (as its road mesh)."""
    if surface is None or not hasattr(surface, "lots"):
        return mesh
    import plazas
    try:
        if quad is not None:
            m = PLAZA_GEO_MARGIN_M
            box = (box[0] - m, box[1] - m, box[2] + m, box[3] + m)
        la, lo, _ = frame.enu_to_geodetic(np.array([box[0], box[2]]), np.array([box[1], box[3]]), np.zeros(2))
        rings = plazas.polygons(surface.lots(la, lo))
        if not rings:
            return mesh
        cover = None
        if mesh is not None and len(mesh[1]):
            P, T = np.asarray(mesh[0], np.float64), np.asarray(mesh[1], np.int64)
            cover = (P[T][:, :, :2] + np.asarray(origin, np.float64)[:2]).reshape(-1, 6)
        pm = plazas.mesh(frame, tuple(box), rings, elev, origin, cover)
        if quad is not None:
            pm = _clip_mesh(pm, origin, quad)
        return plazas.merge(mesh, pm)
    except Exception:                   # no plazas rather than no road tile
        log.exception("plazas of the tile at %s", tuple(round(float(v), 1) for v in box[:2]))
        return mesh


def _tile_decks(bridges, cells, tx, ty):
    """[(xy (k, 2), z (k,), width m)] of the bridge decks over road tile (tx, ty) (the buildings they run through get a
    way, the ones standing on them their floor on the deck: buildings.road_tile_items), [] without bridge profiles."""
    if bridges is None:
        return []
    import roads as roadsmod
    x0, y0, x1, y1 = roadsmod.tile_bbox(tx, ty)
    try:
        return [(np.asarray(xy, np.float64)[:, :2], np.asarray(z, np.float64), float(spec.width))
                for _, spec, xy, z, _ in bridges.parts(cells, (x0 - 20, y0 - 20, x1 + 20, y1 + 20))]
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
    """The anchor's view of this process's tunnel chains (road tiles and the blocks' terrain holes share them): their
    ground read at grading.PROFILE_ZOOM (raw; ends: graded), as in every other process."""
    import roads as roadsmod
    if getattr(_s, "tunnels", None) is None:        # (a state made without it: tests)
        _s.tunnels = {}

    def setup(t):
        sea = getattr(_s, "sea", None)
        t.wet = sea.depth if sea is not None else None
        t.raw = lambda la, lo: ground(None, la, lo, _s.dem.sample(la, lo, gradingmod.PROFILE_ZOOM))  # noqa: E731
        if g is not None:
            t.ends = lambda la, lo: g.apply(la, lo, t.raw(la, lo), gradingmod.ROAD_LIFT, roads_only=True)    # noqa: E731
    return roadsmod.ChainView(_chain_store("tunnels", roadsmod.TunnelProfiles, elev, setup), frame)


def far_buildings_tile(aid, lat, lon, zoom, tx, ty, out_dir, vfs_dir, lamp_region=None):
    """earthd.Server.build_far_bld_tile here (road-tile pool): buildings.far_tile (from the revisit cache when built in
    this anchor's place before)."""
    import buildings as buildingsmod
    import revisit
    frame = geodesy.EnuFrame(lat, lon)
    k = revisit.key("bldfar", round(float(lat), 7), round(float(lon), 7), zoom, tx, ty,
                    _s.md.versions() if _s.md is not None else None, lamp_region)
    return _revisit_tile("bldfar", k, aid, tx, ty, out_dir, vfs_dir, f"bldfar_{aid}_{tx}_{ty}.json",
                         lambda: buildingsmod.far_tile(frame, tx, ty, _s.md,
                                                       lambda la, lo: ground(frame, la, lo, _s.dem.sample(la, lo, zoom)),
                                                       out_dir, vfs_dir, aid, lamp_region),
                         lambda res: {"n": int(res[1])},
                         lambda path, extra: (path, int(extra.get("n", 0))),
                         _where_enu(lat, lon, *roadsmod_tile_bbox(tx, ty), md=_md_versions()))


def skyline_tile(aid, lat, lon, zoom, ux, uy, out_dir, vfs_dir, lamp_region=None):
    """earthd.Server.build_sky_tile here (the far buildings' pool): buildings.skyline_tile (from the revisit cache when
    built in this anchor's place before)."""
    import buildings as buildingsmod
    import revisit
    frame = geodesy.EnuFrame(lat, lon)
    k = revisit.key("bldsky", round(float(lat), 7), round(float(lon), 7), zoom, ux, uy,
                    _s.md.versions() if _s.md is not None else None, lamp_region)
    u = buildingsmod.SKY_UNIT
    sx0, sy0, _, _ = roadsmod_tile_bbox(ux * u, uy * u)
    _, _, sx1, sy1 = roadsmod_tile_bbox(ux * u + u - 1, uy * u + u - 1)
    return _revisit_tile("bldsky", k, aid, ux, uy, out_dir, vfs_dir, f"bldsky_{aid}_{ux}_{uy}.json",
                         lambda: buildingsmod.skyline_tile(frame, ux, uy, _s.md,
                                                           lambda la, lo: ground(frame, la, lo, _s.dem.sample(la, lo, zoom)),
                                                           out_dir, vfs_dir, aid, lamp_region),
                         lambda res: {"n": int(res[1])},
                         lambda path, extra: (path, int(extra.get("n", 0))),
                         _where_enu(lat, lon, sx0, sy0, sx1, sy1, md=_md_versions()))


def far_bridge_tile(aid, lat, lon, zoom, tx, ty, out_dir, vfs_dir):
    """earthd.Server.build_far_bridge_tile here (the far buildings' pool): buildings.far_bridge_tile, its decks from an
    anchor's own roads.BridgeProfiles on the raw ground (grading cells kilometres away for them would cost far more
    than the metre it changes there), kept per anchor in this process; from the revisit cache when seen before."""
    import buildings as buildingsmod
    import revisit
    import roads as roadsmod
    frame = geodesy.EnuFrame(lat, lon)

    def far_setup(p):
        sea = getattr(_s, "sea", None)
        p.wet = sea.depth if sea is not None else None
    # (the road tiles' decks where solved: the bridge looks the same near and far; else on the raw ground)
    bridges = roadsmod.ChainView(
        _chain_store(f"far_bridges_{zoom}", roadsmod.BridgeProfiles,
                     lambda la, lo: ground(None, la, lo, _s.dem.sample(la, lo, zoom)), far_setup), frame,
        prefer=_chain_store("bridges", roadsmod.BridgeProfiles, None, _bridge_setup))
    k = revisit.key("brfar", round(float(lat), 7), round(float(lon), 7), zoom, tx, ty,
                    _s.md.versions() if _s.md is not None else None)
    return _revisit_tile("brfar", k, aid, tx, ty, out_dir, vfs_dir, f"brfar_{aid}_{tx}_{ty}.json",
                         lambda: buildingsmod.far_bridge_tile(frame, tx, ty, _s.md, bridges, out_dir, vfs_dir, aid),
                         lambda res: {"n": int(res[1])},
                         lambda path, extra: (path, int(extra.get("n", 0))),
                         _where_enu(lat, lon, *roadsmod_tile_bbox(tx, ty), md=_md_versions()))


# --- far geotiles (phase 2): far buildings, the skyline and far bridges once per geotile ---------------------------
# Each built in its own tile's frame (geotiles.FAR_Z / SKY_Z / BRIDGE_Z), with the rules and DEM zoom of its own place,
# cached without an anchor in its key (revisit kinds "bldfar" / "bldsky" / "brfar", the key's first part "g"); the mod
# keeps it across rebases and places it rigidly at its pivot (render-only: no skin, no exact variant).

FAR_GEO = {"bldfar": "bf", "bldsky": "bs", "brfar": "bb"}       # kind -> its files' prefix (<prefix>_<key>[_<sub>])
FAR_LAMP_ROUND = 1          # decimals of the lamps' coordinates


def far_geo_zoom(kind):
    """The geotile zoom of a far kind."""
    import geotiles
    return {"bldfar": geotiles.FAR_Z, "bldsky": geotiles.SKY_Z, "brfar": geotiles.BRIDGE_Z}[kind]


def far_geo_names(kind, key):
    """The session file names of far geotile key: {"json": ..., "shape": ...} (a far buildings or skyline tile's mesh);
    a far bridge tile's pieces are <prefix>_<key>_<road geotile key>.cdae (far_geo_piece)."""
    pre = FAR_GEO[kind]
    return {"json": f"{pre}_{key}.json", "shape": f"{pre}_{key}.cdae"}


def far_geo_piece(key, sub):
    return f"{FAR_GEO['brfar']}_{key}_{sub}.cdae"


def _lamps_by_geotile(frame, lamps):
    """Far lamps [[x, y, z], ...] of frame's world as {road geotile key: {"pivot": m, "rows": [[x, y, z], ...]}}: each
    in the geotile (geotiles.FAR_Z) its place lies in, in that tile's own frame (the mod's trees.lua places each group
    there and with it at a rebase); pivot the median height of its rows."""
    import geotiles
    if not lamps:
        return {}
    p = np.asarray(lamps, np.float64).reshape(-1, 3)
    la, lo, h = geotiles.world_to_geodetic(frame, p)
    la, lo, h = np.asarray(la, np.float64), np.asarray(lo, np.float64), np.asarray(h, np.float64)
    gx, gy = geotiles.tile_of(la, lo, geotiles.FAR_Z)
    out = {}
    for kx, ky in sorted({(int(a), int(b)) for a, b in zip(gx, gy)}):
        sel = (gx == kx) & (gy == ky)
        x, y, z = geotiles.geodetic_to_world(geotiles.frame(kx, ky, geotiles.FAR_Z), la[sel], lo[sel], h[sel])
        rows = np.round(np.c_[np.asarray(x, np.float64), np.asarray(y, np.float64), np.asarray(z, np.float64)],
                        FAR_LAMP_ROUND)
        out[geotiles.key(kx, ky, geotiles.FAR_Z)] = {"pivot": round(float(np.median(rows[:, 2])), 2),
                                                      "rows": rows.tolist()}
    return out


def _far_geo_build(kind, tile, gzoom, out_dir, vfs_dir, lamp_region):
    """Far geotile tile's content written to out_dir: (JSON data, {what: file name} of its shapes, count) or None
    when its cells are not at hand (asked again later)."""
    import buildings as buildingsmod
    import geotiles
    import roads as roadsmod
    frame = tile.frame
    x0, y0, x1, y1 = tile.box

    def raw(la, lo):
        return ground(None, la, lo, _s.dem.sample(la, lo, gzoom))
    data = {"v": 1, "key": tile.key, "frame": [float(frame.lat0), float(frame.lon0)]}
    names = far_geo_names(kind, tile.key)
    shapes = {}
    if kind == "bldfar":
        items, lamps, _box = buildingsmod.far_items(frame, tile, None, _s.md, raw, lamp_region)
        mesh = buildingsmod.build(items, lite=True)
        n = len(items)
    elif kind == "bldsky":
        keys = roadsmod.cells_of_tile(frame, tile, None, margin=0.0)
        got = buildingsmod.sky_items(frame, (x0, y0, x1, y1), sorted(keys, key=str), _s.md, raw, lamp_region,
                                     own=tile.box)
        if got is None:
            return None
        picked, lamps = got
        mesh = buildingsmod._sky_mesh(picked) if picked else None
        n = len(picked) if mesh is not None else 0
        if mesh is None:
            lamps = []
    else:                               # brfar: the slabs in a piece per road geotile under it
        keys = roadsmod.cells_of_tile(frame, tile, None, margin=0.0)
        if not all(_s.md.has(k, "roads") for k in keys):
            for k in keys:
                _s.md.prefetch(k, layers=("roads",))
            return None
        cells = [_s.md.cell(k, layers=("roads",), kind="background").layers.get("roads") for k in keys]

        def far_setup(p):
            sea = getattr(_s, "sea", None)
            p.wet = sea.depth if sea is not None else None
        # (the raw ground's chains of this DEM zoom: the same deck whatever the anchor or the order of the asks)
        bridges = roadsmod.ChainView(_chain_store(f"far_bridges_{gzoom}", roadsmod.BridgeProfiles, raw, far_setup),
                                     frame)
        parts = [(xy, z, float(spec.width)) for _fid, spec, xy, z, _lon in bridges.parts(cells, (x0, y0, x1, y1))]
        meshes = buildingsmod.bridge_slabs(parts, (x0, y0), owners=(frame, geotiles.FAR_Z, tile.z, tile.gx,
                                                                   tile.gy)) if parts else {}
        pieces = {}
        for (gx, gy), m in sorted(meshes.items()):
            sub = geotiles.key(gx, gy, geotiles.FAR_Z)
            fname = far_geo_piece(tile.key, sub)
            buildingsmod.write(os.path.join(out_dir, fname), m)
            e = {"shape": f"{vfs_dir}/{fname}", "pos": [x0, y0, 0.0]}
            e.update(_shape_info(os.path.join(out_dir, fname), e["pos"], "brfar"))
            pieces[sub] = e
            shapes[sub] = fname
        if pieces:
            data["parts"] = pieces
        return data, shapes, len(parts) if pieces else 0
    if mesh is not None:
        buildingsmod.write(os.path.join(out_dir, names["shape"]), mesh)
        data["shape"] = f"{vfs_dir}/{names['shape']}"
        data["pos"] = [x0, y0, 0.0]
        data.update(_shape_info(os.path.join(out_dir, names["shape"]), data["pos"], kind))
        shapes["shape"] = names["shape"]
    by = _lamps_by_geotile(frame, lamps)
    if by:
        data["lamps"] = by
    return data, shapes, n


def far_geo(kind, gx, gy, gzoom, out_dir, vfs_dir, lamp_region=None):
    """earthd.Server.build_far_geo here (the far buildings' pool): far geotile (gx, gy) of kind ("bldfar": its far
    buildings, the lite mesh of those road geotile (gx, gy) holds, and their streets' lamps; "bldsky": the skyline of a
    SKY_Z square; "brfar": the bridge decks of a BRIDGE_Z square as a piece per road geotile under it), built in its own
    frame (gzoom its DEM zoom, lamp_region its rules) or from the revisit cache (no anchor in the key). Written as
    <prefix>_<key>.json (+ its shapes); returns (vfs path of the JSON, count, frame (lat, lon), pivot, {what: shape
    file name}) or None when its cells are not at hand yet."""
    import json
    import geotiles
    import revisit
    z = far_geo_zoom(kind)
    tile = geotiles.geobox(gx, gy, z)
    names = far_geo_names(kind, tile.key)
    k = revisit.key(kind, "g", tile.key, int(gzoom), _md_versions(), lamp_region)
    frame_ll = (float(tile.frame.lat0), float(tile.frame.lon0))
    got = revisit.load(_s.cache_dir, kind, k)
    if got is not None:
        blobs, extra = got
        for name, data in blobs.items():
            if name.endswith(".json"):
                data = data.decode("utf-8").replace("@V@/", f"{vfs_dir}/").encode("utf-8")
            _write_bytes(os.path.join(out_dir, name), data)
        _far_geo_stats["hits"] += 1
        return (f"{vfs_dir}/{names['json']}", int(extra.get("n", 0)), frame_ll, float(extra.get("pivot", 0.0)),
                dict(extra.get("shapes") or {}))
    f0 = revisit.faults()
    res = _far_geo_build(kind, tile, gzoom, out_dir, vfs_dir, lamp_region)
    if res is None:
        return None
    data, shapes, n = res
    text = json.dumps(data, separators=(",", ":"), sort_keys=True)
    data["h"] = _content_hash(text, vfs_dir)
    text = json.dumps(data, separators=(",", ":"), sort_keys=True)
    _write_bytes(os.path.join(out_dir, names["json"]), text.encode("utf-8"))
    _far_geo_stats["built"] += 1
    pivot = float(data.get("pivot", 0.0))
    if revisit.ON and revisit.faults() == f0:     # (complete: nothing it read failed or was missing)
        try:
            blobs = {names["json"]: text.replace(f"{vfs_dir}/", "@V@/").encode("utf-8")}
            for fname in shapes.values():
                with open(os.path.join(out_dir, fname), "rb") as fh:
                    blobs[fname] = fh.read()
            s, w, nn, e = geotiles.info(gx, gy, z)[2:]
            wh = revisit.where(*frame_ll, s, w, nn, e, revisit.sig(md=_md_versions()))
            revisit.store(_s.cache_dir, kind, k, blobs, {"n": int(n), "pivot": pivot, "shapes": shapes}, where=wh)
        except Exception as ex:          # (not kept rather than no tile)
            log.debug("revisit: far geotile %s %s not kept: %s: %s", kind, tile.key, type(ex).__name__, ex)
    return f"{vfs_dir}/{names['json']}", int(n), frame_ll, pivot, shapes


_far_geo_stats = {"built": 0, "hits": 0}


def far_geo_stats():
    """This process's far geotile counters (built, from the revisit cache)."""
    return dict(_far_geo_stats)


def base_entry(user_dir, base_vfs, ao_vfs, rgb, ao_value):
    """terraincache.base_entry here (its BC7 encode is the base photo's main cost)."""
    return terraincache.base_entry(user_dir, base_vfs, ao_vfs, rgb, ao_value)


def far_geometry(aid, lat, lon, tile, zoom, q, ref_zoom, with_sink, layout):
    """farfield.build_tile here (far-tile workers: the geometry, graded at levels <= 0 like the block, is
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
        revisit.store(_s.cache_dir, "fargeo", k, {"built.npz": blob}, meta,
                      where=_where_enu(lat, lon, *tile.bbox, layout=repr(layout), md=_md_versions()))
    return built


def far_global_geometry(z, x, y, zoom, ref_zoom, layout):
    """farfield.build_global here (a global far tile, geotiles phase 3b: in its own frame, the same for every anchor);
    one built before (any anchor, any session) comes from the revisit cache."""
    import revisit
    k = revisit.key("fargeo", *_far_global_key(z, x, y, zoom, ref_zoom, layout))
    got = revisit.load(_s.cache_dir, "fargeo", k)
    if got is not None:
        return _built_from(got[0]["built.npz"], got[1])
    f0 = revisit.faults()
    base = _s.airports.wrap(_s.dem) if _s.airports is not None else _s.dem
    if _s.sea is not None:
        base = seacarvemod.SeaDem(base, None)       # (carving stops at seacarve.CARVE_MAX_LEVEL < the global levels)
    built = farfield.build_global(base, z, x, y, zoom, ref_zoom, layout)
    if revisit.ON and revisit.faults() == f0:
        blob, meta = _built_blob(built)
        la, lo = farfield.global_frame_ll(z, x, y)
        revisit.store(_s.cache_dir, "fargeo", k, {"built.npz": blob}, meta,
                      where=_where_enu(la, lo, -built.size / 2, -built.size / 2, built.size / 2, built.size / 2,
                                       layout=repr(layout), md=_md_versions()))
    return built


def _far_global_key(z, x, y, zoom, ref_zoom, layout):
    """What a global far tile's geometry is built from (no anchor: the revisit key any anchor finds)."""
    return ("m", farfield.GLOBAL_GEO_VERSION, int(z), int(x), int(y), zoom, ref_zoom, repr(layout),
            _s.md.versions() if _s.md is not None else None, _s.sea is not None, _s.airports is not None)


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
    src = gradingmod.GradedDem(base, g) if (g is not None and tile.level <= 0) else base
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


def base_photo(lat, lon, x0, y0, size, px, tex, buildings, rock_zoom, path, cache, fuel_pads=None, out_px=None):
    """earthd.Server.write_base here, for a server without its imagery analyses (a small machine): the block's base
    photo from the imagery tiles on disk, its building footprints filled and its cliffs as rock, written to path
    (PNG) and packed into the game's terrain texture cache (cache: terraincache.base_entry's arguments but the rgb).
    Returns (rgb, sources used, lazy tiles still to come), or None when an imagery tile it needs is not on disk (the
    server renders it, fetching what is missing). In the server process it ran 2 s alone but 8-11 s next to the
    rest of the world's threads on a Steam Deck (one interpreter lock), and the next block waited for it. fuel_pads:
    the gas station pads around it (_take_pads). px: the imagery's render size, out_px: the photo's (enlarged before
    the roads are painted in: earthd.BASE_PX)."""
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
    import surface as surfacemod
    if roadsmod.ROADS_IN_GROUND:          # (earthd.Server.write_base)
        try:
            rgb = surfacemod.unghost_square(rgb, frame, x0, y0, size, getattr(_s, "surface", None), _s.widths)
        except Exception as e:      # the photo stays as it is
            log.info("base photo: road ghosts: %s", e)
    rgb = surfacemod.enlarge(rgb, out_px or px)
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
    with open(tmp, "wb") as f:
        f.write(terraincache.stored_png(rgb))
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
            revisit.store(_s.cache_dir, "far", k, blobs, meta,
                          where=_where_enu(lat, lon, *tile.bbox, layout=repr(layout), md=_md_versions(),
                                           chain=tuple(tex["chain"]), tone=tex["tone"], leaf_on=tex["leaf_on"],
                                           season_mode=tex["season_mode"]))
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
far_geo = _accounted(far_geo)
base_entry = _accounted(base_entry)
far_geometry = _accounted(far_geometry)
far_global_geometry = _accounted(far_global_geometry)
far_tile = _accounted(far_tile)
base_photo = _accounted(base_photo)
navgraph = _accounted(navgraph)
