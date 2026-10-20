"""Physics-block builds in a process of their own (2026-10-12).

The server is one Python process: its threads share one interpreter lock, so while three far-tile workers
built, a physics block took 3-10x longer than alone (land cover 1.4 s instead of 0.1 s), too long to follow a
car at 58 m/s, where the 512² block moves every 2.2 s. This process builds the blocks the follow and the
prefetch ask for on a core of its own. It keeps its own DEM reader, road plans (grading.Grader per anchor),
street widths, height chunks and land-cover reader over the same disk caches. All of them are deterministic,
so a block built here is the one the server would build (the far tiles, graded in the server process, agree
with it).

init(...) runs once in the process (ProcessPoolExecutor initializer); build(...), base_entry(...) and
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
import geodesy
import grading as gradingmod
import heightmap
import mapdata as mapdatamod
import netfetch
import roadwidth as roadwidthmod
import seacarve as seacarvemod
import surface as surfacemod
import terraincache

log = logging.getLogger("earthd.blockworker")

_s = None       # this process's state (init)


class _State:
    def __init__(self, cache_dir, offline, roads, grading, road_widths, surface, airports=False, sea=False):
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
            lambda key: md.cell(key, layers=("buildings",), kind="demand").layers.get("buildings")).get
            if road_widths and self.md is not None else None)
        self.surface = surfacemod.Surface(md) if surface else None
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
                for old in sorted(self.chunks)[:-ANCHORS_KEPT]:
                    del self.chunks[old], self.graders[old]
            return self.graders[aid], self.chunks[aid]


ANCHORS_KEPT = 2
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


def init(cache_dir, offline, roads, grading, road_widths, surface, warm_q, log_level=logging.INFO, airports=False,
         sea=False, nice=0):
    """nice: this worker's niceness on top of the server's (earthd NICE_*): the terrain blocks' workers keep the
    server's own, the road tiles' and the far field's run below them, so the ground under the car wins the server's
    few cores (2026-10-19: at 180 mph the next block waited 6 s behind the road tiles ahead and a ball fell off the
    block's edge)."""
    global _s
    if nice and hasattr(os, "nice"):
        try:
            os.nice(int(nice))
        except OSError:
            pass
    logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    _s = _State(cache_dir, offline, roads, grading, road_widths, surface, airports, sea)
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


def build(aid, lat, lon, hm_zoom, q, cx, cy, size, out_dir, name, ter_name=None):
    """Build and write the block (heightmap PNG name in out_dir, its surface masks beside it, and the
    .ter ter_name if given). Returns the info dict of earthd's make_heightmap (without seconds / zoom /
    base)."""
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


def roads_tile(aid, lat, lon, zoom, tx, ty, out_dir, vfs_dir, sidewalks, lift, buildings=False, left_hand=False,
               mph=True):
    """earthd.Server.build_roads_tile here (road-tile pool): the anchor's road tile (tx, ty) built and
    written; returns roads.write_tile's (vfs path, chunks). In the server's process its Python made up
    ~80 % of the main process's busy threads (in-game profile 2026-10-13): the block photos and far
    textures waited for the GIL."""
    import parking as parkingmod
    import roadmesh as roadmeshmod
    import roads as roadsmod
    g, _ = _s.anchor(aid, lat, lon)
    frame = geodesy.EnuFrame(lat, lon)
    cells = [_s.md.cell(k, layers=("roads",), kind="demand").layers.get("roads")
             for k in roadsmod.cells_of_tile(frame, tx, ty)]

    def elev(la, lo):           # the physics ground (lifted under roads: grading.ROAD_LIFT)
        z = ground(frame, la, lo, _s.dem.sample(la, lo, zoom))
        return g.apply(la, lo, z, gradingmod.ROAD_LIFT) if g is not None else z
    with _s.lock:
        bridges = _s.bridges.get(aid)
        if bridges is None:
            bridges = _s.bridges[aid] = roadsmod.BridgeProfiles(
                frame, lambda key: _s.md.cell(key, layers=("roads",), kind="demand").layers.get("roads"), elev)
            sea = getattr(_s, "sea", None)
            bridges.wet = sea.depth if sea is not None else None        # (decks clear the water: roads.water_clear)
            bridges.infra_fn = lambda key: _s.md.cell(key, layers=("infrastructure",), kind="demand").layers.get(
                "infrastructure")                                       # (bridge:structure tags: bridgetypes)
            for old in sorted(_s.bridges)[:-ANCHORS_KEPT]:
                del _s.bridges[old]
    tunnels = tunnel_profiles(aid, frame, elev, g)
    walks = []
    urban = _s.surface.urban if _s.surface is not None else None
    props = [] if buildings else None
    lines = [] if buildings else None
    wires = [] if buildings else None
    park = [] if buildings else None
    items = town = None
    if buildings:
        import buildings as buildingsmod
        items = buildingsmod.road_tile_items(frame, tx, ty, _s.md, elev,
                                             decks=_tile_decks(bridges, cells, tx, ty))
        town = buildingsmod.coverage(items) < buildingsmod.POLES_COVERAGE
    rwalls = []
    ctx = []
    frontage_walks = []
    chunks = roadsmod.build_tile(frame, tx, ty, cells, elev, urban, walks=walks, deck_context=ctx, bridges=bridges, widths=_s.widths,
                                 props=props, lines=lines, wires=wires, town=town, park=park,
                                 left_hand=left_hand, mph=mph, structures=buildingsmod.centres(items, tx, ty) if items else (),
                                 tunnels=tunnels, walls=rwalls, frontage_walks=frontage_walks)
    if _s.airports is not None:         # runway markings, drawn by the road mesh like the lane lines
        chunks += _s.airports.tile_chunks(frame, tx, ty, elev, roadsmod.tile_bbox)
    x0, y0, _, _ = roadsmod.tile_bbox(tx, ty)
    if items is not None:               # front doors and shopfronts, driveways into the road mesh
        drives = []
        props.extend(buildingsmod.doors(items, lines, x0, y0, drives))
        drives.extend(buildingsmod.garage_drives(items, lines, x0, y0))
        chunks += drives
        park = parkingmod.clear_of(park, props, drives)
        park = parkingmod.tile_lot_spots(frame, tx, ty, _s.md, elev, items, park)
        buildingsmod.brick_walks(walks, items, lat, lon)
        buildingsmod.pave_frontages(items, frontage_walks, x0, y0)
    mesh = roadmeshmod.build(frame, chunks, elev, (x0, y0, 0.0), lift)
    mesh = _with_plazas(mesh, frame, tx, ty, elev, (x0, y0, 0.0), getattr(_s, "surface", None))
    dmesh = roadmeshmod.deck(chunks, (x0, y0, 0.0), bridges.piers_in(roadsmod.tile_bbox(tx, ty)), ctx,
                              bridges.beams_in(roadsmod.tile_bbox(tx, ty)))
    tmesh = roadmeshmod.tunnel(chunks, (x0, y0, 0.0), rwalls)
    bld = None
    if buildings:
        import buildings as buildingsmod
        bld = buildingsmod.road_tile(frame, tx, ty, _s.md, elev, None, None,
                                     items)
    return roadsmod.write_tile(out_dir, vfs_dir, aid, tx, ty, chunks, walks if sidewalks else None, mesh, dmesh,
                               bld, props, wires, park, tunnel_mesh=tmesh)


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
        return plazas.merge(mesh, plazas.mesh(frame, box, rings, elev, origin)) if rings else mesh
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
    """earthd.Server.build_far_bld_tile here (road-tile pool): buildings.far_tile."""
    import buildings as buildingsmod
    frame = geodesy.EnuFrame(lat, lon)
    g, _ = _s.anchor(aid, lat, lon)

    def elev(la, lo):
        z = ground(frame, la, lo, _s.dem.sample(la, lo, zoom))
        return g.apply(la, lo, z, gradingmod.ROAD_LIFT) if g is not None else z
    return buildingsmod.far_tile(frame, tx, ty, _s.md, elev, out_dir, vfs_dir, aid)


def base_entry(user_dir, base_vfs, ao_vfs, rgb, ao_value):
    """terraincache.base_entry here (its BC7 encode is the base photo's main cost)."""
    return terraincache.base_entry(user_dir, base_vfs, ao_vfs, rgb, ao_value)


def far_geometry(aid, lat, lon, tile, zoom, q, ref_zoom, with_sink, layout):
    """farfield.build_tile here (far-tile workers: the geometry, graded at levels <= 0 like the block, is
    most of a far tile's cost; its texture is rendered in the server, where the imagery corrections live).
    Returns the Built."""
    g, _ = _s.anchor(aid, lat, lon)
    frame = geodesy.EnuFrame(lat, lon)
    base = _s.airports.wrap(_s.dem) if _s.airports is not None else _s.dem
    if _s.sea is not None:
        base = seacarvemod.SeaDem(base, _s.sea if tile.level <= seacarvemod.CARVE_MAX_LEVEL else None)
    src = gradingmod.GradedDem(base, g) if (g is not None and tile.level <= 0) else base
    return farfield.build_tile(frame, src, tile, zoom, q, ref_zoom, with_sink=with_sink,
                               layout=layout)


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
                log.debug("worker %d cpu: %s", os.getpid(),
                          ", ".join(f"{k} {v[0]} jobs {v[1]:.1f} s" for k, v in sorted(_cpu.items())))
    return run


build = _accounted(build)
roads_tile = _accounted(roads_tile)
far_buildings_tile = _accounted(far_buildings_tile)
base_entry = _accounted(base_entry)
far_geometry = _accounted(far_geometry)
