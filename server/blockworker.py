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
        self.cache_dir = cache_dir
        self.imagery = None         # (its settings, imagery.Imagery from the disk alone): far_tile's textures
        net = netfetch.Fetcher(offline=offline)
        self.dem = demmod.Dem(os.path.join(cache_dir, "dem"), fetcher=net)
        # the Netherlands at 0.5 m: the AHN's ground for the terrain near the player, its surface for the decks
        # (ahn.py, earthd --nl-bgt on)
        import bgt as bgtmod
        import ahn as ahnmod
        self.ahn = ahnmod.Ahn(cache_dir, net) if bgtmod.enabled() else None
        if self.ahn is not None:
            self.dem = ahnmod.AhnDem(self.dem, self.ahn)
        # runways graded in (--airports on): the copy the server loaded, never refreshed here
        self.airports = load_airports(cache_dir, self.dem) if airports else None
        md = mapdatamod.MapData(cache_dir, fetcher=net) if (roads or surface or sea) else None
        # the sea's and the lakes' floors carved in (--sea on)
        self.sea = seacarvemod.SeaCarver(md, cache_dir, dem=self.dem) if sea else None
        self.md = md if roads else None
        # the Netherlands' road surfaces as mapped (bgt.py, earthd --nl-bgt on)
        import bgt as bgtmod
        self.bgt = bgtmod.Bgt(cache_dir, net) if (bgtmod.enabled() and self.md is not None) else None
        self.grading = bool(grading) and self.md is not None
        self.widths = (roadwidthmod.RoadWidths(
            lambda key: md.cell(key, layers=("buildings",), kind="demand").layers.get("buildings"),
            roadwidthmod.store_dir(cache_dir, md)).get
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
                if self.graders[aid] is not None:
                    self.graders[aid].wet = self.sea.depth if getattr(self, "sea", None) is not None else None
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
         sea=False, nice=0):
    """nice: this worker's niceness on top of the server's (earthd NICE_*): the terrain blocks' workers keep the
    server's own, the road tiles' and the far field's run below them, so the ground under the car wins the server's
    few cores (2026-10-19: at 180 mph the next block waited 6 s behind the road tiles ahead and a ball fell off the
    block's edge)."""
    global _s
    import budget
    try:
        budget.lower_priority(nice, worker=True)    # (Windows: the far field's workers idle, budget.WIN_IDLE_ABOVE)
    except (OSError, AttributeError):
        pass
    logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    budget.apply()              # (this process's caches: smaller on a small machine)
    if os.environ.get("BNGE_TRACEMALLOC"):      # debug: this worker's memory (earthd's trace_mem)
        _trace_memory()
    import sampler
    sampler.maybe_start(f".{os.getpid()}")      # BNGE_SAMPLE: this worker's own sampling profile (debugging)
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
    if getattr(_s, "bgt", None) is not None:     # the ground under a junction's deck dug out (bgt.cut)
        import bgt as bgtmod
        half = size * heightmap.SQUARE / 2
        try:
            bb = (cx - half, cy - half, cx + half, cy + half)
            passes = _s.bgt.passages(frame, bb)
            gen = bgtmod.cut(frame, passes, gen)
            if _s.bgt.tiles_box(frame, bb):     # (the Netherlands: the lidar's banks kept, bgt.banks)
                gen = bgtmod.banks(frame, passes, gen, raw)
            import kunstwerken as kwmod     # (under the hand-built structures' streets)
            gen = kwmod.cap(gen)
        except Exception as e:
            log.warning("bgt passages of block %s,%s: %s: %s", cx, cy, type(e).__name__, e)
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


def _bridges(aid, frame, elev):
    """The anchor's roads.BridgeProfiles (deck heights of whole bridge chains), made once per anchor in this process."""
    import roads as roadsmod
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
    return bridges


def navgraph(aid, lat, lon, zoom, x, y, out_dir, vfs_dir, serial, left_hand, mph, rules):
    """earthd.Server.build_navgraph here (a road-tile process): the AI road graph around (x, y) written to out_dir;
    returns (vfs path, nodes, edges). The bridges' deck heights are this process's (the road tiles' own); in the
    server they were built a second time, in its interpreter (13 s of its CPU in a 4-minute drive, 2026-10-20)."""
    import navgraph as navgraphmod
    import mapcells
    g, _ = _s.anchor(aid, lat, lon)
    frame = geodesy.EnuFrame(lat, lon)
    w = navgraphmod.WINDOW_M + 200.0
    la, lo, _ = frame.enu_to_geodetic(np.array([x - w, x + w]), np.array([y - w, y + w]), 0.0)
    keys = mapcells.cells_for_bbox(float(la.min()), float(lo.min()), float(la.max()), float(lo.max()))
    feats = [_s.md.cell(k, layers=("roads",), kind="demand").layers.get("roads") for k in keys]

    def elev(la_, lo_):         # the physics ground, lifted under roads (as the road tiles')
        z = ground(frame, la_, lo_, _s.dem.sample(la_, lo_, zoom))
        return g.apply(la_, lo_, z, gradingmod.ROAD_LIFT) if g is not None else z
    decks = {}
    try:
        for fid, _spec, xy, z, _lon in _bridges(aid, frame, elev).parts(feats, (x - w, y - w, x + w, y + w)):
            decks.setdefault(fid, []).append((np.asarray(xy, np.float64)[:, :2], np.asarray(z, np.float64)))
    except Exception as e:          # no decks: the graph follows the ground under bridges
        log.debug("navgraph decks: %s: %s", type(e).__name__, e)
    urban = _s.surface.urban if _s.surface is not None else None
    graph = navgraphmod.build(frame, feats, elev, (x, y), urban, decks=decks, widths=_s.widths, left_hand=left_hand,
                              mph=mph)
    graph["rules"] = rules
    path = navgraphmod.write(out_dir, vfs_dir, aid, serial, graph)
    return path, len(graph["nodes"]), len(graph["edges"])


def roads_tile(aid, lat, lon, zoom, tx, ty, out_dir, vfs_dir, sidewalks, lift, buildings=False, left_hand=False,
               mph=True, q=None):
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
    bridges = _bridges(aid, frame, elev)
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
        items = buildingsmod.road_tile_items(frame, tx, ty, _s.md,
                                             lambda la, lo: ground(frame, la, lo, _s.dem.sample(la, lo, zoom)),
                                             decks=_tile_decks(bridges, cells, tx, ty))
        town = buildingsmod.coverage(items) < buildingsmod.POLES_COVERAGE
    rwalls = []
    ctx = []
    chunks = roadsmod.build_tile(frame, tx, ty, cells, elev, urban, walks=walks, deck_context=ctx, bridges=bridges, widths=_s.widths,
                                 props=props, lines=lines, wires=wires, town=town, park=park,
                                 left_hand=left_hand, mph=mph, structures=buildingsmod.centres(items, tx, ty) if items else (),
                                 tunnels=tunnels, walls=rwalls)
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
        buildingsmod.brick_walks(walks, items, lat, lon)
    box = roadsmod.tile_bbox(tx, ty)
    cell_fn = lambda key: _s.md.cell(key, layers=("roads",), kind="demand").layers.get("roads")   # noqa: E731

    def drawn(la, lo):          # the block's ground: raised over the low arches as the terrain is (_make_heightmap)
        return roadsmod.block_arch_raise(tunnels, cell_fn, frame, la, lo, elev(la, lo),
                                         (box[0] - 8, box[1] - 8, box[2] + 8, box[3] + 8))
    polys = _bgt_polygons(frame, tx, ty, chunks)
    bgt_solid, bgt_decks = [], []
    if polys:       # the Netherlands: the mapped surfaces instead of the guessed ones; the markings stay over them
        import bgt as bgtmod
        import plazas
        raw = lambda la, lo: ground(frame, la, lo, _s.dem.sample(la, lo, zoom))     # noqa: E731
        try:                        # the underpasses: the ground dug out under a junction's deck as the terrain is
            passes = _s.bgt.passages(frame, (box[0] - 8, box[1] - 8, box[2] + 8, box[3] + 8))
        except Exception as e:
            log.warning("bgt passages of tile %s_%s: %s: %s", tx, ty, type(e).__name__, e)
            passes = []
        dug = bgtmod.banks(frame, passes, bgtmod.cut(frame, passes, elev), raw)
        import kunstwerken as kwmod     # the hand-built structures here (data/kunstwerken): their decks' planes
        kws = kwmod.near(frame, box)
        if kws:                         # their streets are theirs: the terrain under their floors, the markings left out
            dug = kwmod.cap(dug)
            drawn = kwmod.cap(drawn)

            def kw_marks(chs):
                out = []
                for ch in chs:
                    pts = np.asarray(ch.get("n") or (), np.float64)
                    if len(pts) >= 2:
                        cla, clo, _ = frame.enu_to_geodetic(pts[:, 0], pts[:, 1], np.zeros(len(pts)))
                        if kwmod.covered(np.asarray(cla), np.asarray(clo)).mean() > 0.5:
                            continue
                    out.append(ch)
                return out
        else:
            kw_marks = lambda chs: chs      # noqa: E731
        ahn_top = _s.ahn.deck_top if getattr(_s, "ahn", None) else None
        deck_top = (lambda la, lo: kwmod.deck_top(la, lo, ahn_top)) if kws else ahn_top    # noqa: E731
        _s.last_ground = (tx, ty, dug, raw, passes)     # (tools/road_check.py: the ground this tile was built on)
        blift = lift + bgtmod.SURFACE_LIFT_M     # (over the terrain between their 2 m vertices, markings with them)
        marks = roadmeshmod.build(frame, kw_marks(bgtmod.keep_markings(chunks)), dug, (x0, y0, 0.0), blift, clip=box,
                                  follow=True, q=q, drawn=bgtmod.cut(frame, passes, drawn))
        # (its kerbed footways and islands, its streets over a trench into bgt_solid: the colliding tunnel mesh; its
        # surfaces subdivided more coarsely where a tile's mesh would pass the 65535 vertices the engine draws)
        for step in (bgtmod.STEP_M, 2 * bgtmod.STEP_M, 4 * bgtmod.STEP_M, 8 * bgtmod.STEP_M):
            bgt_solid[:], bgt_decks[:] = [], []
            mesh = plazas.merge(marks, bgtmod.mesh(frame, box, polys, dug, raw, (x0, y0, 0.0), blift, solid=bgt_solid,
                                                   passes=passes, decks=bgt_decks, step=step,
                                                   deck_top=deck_top, skip=kwmod.covered if kws else None))
            if (mesh is None or len(mesh[0]) <= MESH_VERTS_MAX) and sum(len(m[0]) for m in bgt_solid) <= MESH_VERTS_MAX / 2:
                break
        sidewalks = False           # (the BGT's footways)
        try:                        # its tram tracks, its walls for the guessed ones, its street lights for the spaced
            mesh = plazas.merge(mesh, bgtmod.rails_mesh(frame, box, _s.bgt.features(frame, tx, ty, (bgtmod.RAILS,)),
                                                        elev, (x0, y0, 0.0), lift))
            bw = bgtmod.walls(frame, box, _s.bgt.features(frame, tx, ty, (bgtmod.WALLS,)), dug, raw,
                              passes=passes)
            if bw and kws:              # (a kunstwerk's own walls stand there)
                def kw_free(w):
                    b = np.asarray(w["base"], np.float64)
                    bla, blo, _ = frame.enu_to_geodetic(b[:, 0], b[:, 1], np.zeros(len(b)))
                    return not any(k.in_box(np.asarray(bla), np.asarray(blo)).any() for k in kws)
                bw = [w for w in bw if kw_free(w)]
            if bw:
                rwalls[:] = bw
            if props is not None:
                lamps = bgtmod.lamps(frame, box, _s.bgt.features(frame, tx, ty, (bgtmod.POLES,)), polys, elev)
                if lamps:
                    props[:] = [r for r in props if r[0] != "streetlight"] + lamps
                if kws and props:       # (a kunstwerk's own furniture stands there: none of ours in its box)
                    xy = np.asarray([[r[1], r[2]] for r in props], np.float64)
                    pla, plo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
                    inb = np.zeros(len(xy), bool)
                    for k in kws:
                        inb |= k.in_box(np.asarray(pla), np.asarray(plo))
                    props[:] = [r for r, b in zip(props, inb) if not b]
        except Exception as e:
            log.warning("bgt walls / lamps of tile %s_%s: %s: %s", tx, ty, type(e).__name__, e)
        if kws:                     # where a kunstwerk draws its streets, nothing of ours over them (its rails, paint)
            mesh = _kw_cut(mesh, frame, (x0, y0), kwmod)
    else:
        mesh = roadmeshmod.build(frame, chunks, elev, (x0, y0, 0.0), lift, clip=box, follow=True, q=q, drawn=drawn)
        mesh = _with_plazas(mesh, frame, tx, ty, elev, (x0, y0, 0.0), getattr(_s, "surface", None))
    dmesh = roadmeshmod.deck(chunks, (x0, y0, 0.0), bridges.piers_in(roadsmod.tile_bbox(tx, ty)), ctx,
                              bridges.beams_in(roadsmod.tile_bbox(tx, ty)))
    if bgt_decks:                   # (the streets over a trench: the deck mesh collides too)
        import plazas
        for m in bgt_decks:
            dmesh = plazas.merge(dmesh, m)
    tmesh = roadmeshmod.tunnel(chunks, (x0, y0, 0.0), rwalls)
    if polys:
        for k in kws:                   # the kunstwerken's walls and decks: colliding, in the tunnel mesh
            km = k.tile_mesh(frame, box, (x0, y0, 0.0))
            if km is not None:
                bgt_solid.append(km)
    if bgt_solid:                   # (the tunnel mesh collides and renders: kerbs and decks the car meets)
        import plazas
        for m in bgt_solid:
            tmesh = plazas.merge(tmesh, m)
    bld = None
    if buildings:
        import buildings as buildingsmod
        bld = buildingsmod.road_tile(frame, tx, ty, _s.md,
                                     lambda la, lo: ground(frame, la, lo, _s.dem.sample(la, lo, zoom)), None, None,
                                     items)
    for what, m in (("road", mesh), ("deck", dmesh)):     # (the tunnel mesh is written in parts: roads.split_mesh)
        if m is not None and len(m[0]) > 65535:     # (the engine draws the first 65535 vertices of an object only)
            log.warning("road tile %s_%s: %s mesh of %d vertices (> 65535: drawn broken)", tx, ty, what, len(m[0]))
    return roadsmod.write_tile(out_dir, vfs_dir, aid, tx, ty, chunks, walks if sidewalks else None, mesh, dmesh,
                               bld, props, wires, park, tunnel_mesh=tmesh)


def _kw_cut(mesh, frame, origin, kwmod):
    """The mesh (P relative to origin, T, UV, mats, M) without its triangles over a kunstwerk's floor
    (kunstwerken.covered at their centres): the generated surface, rails and paint left out where the kunstwerk has
    its own (in the game they showed through it, 2026-10-21)."""
    if mesh is None or not len(mesh[1]):
        return mesh
    P, T, UV, mats, M = mesh
    P, T = np.asarray(P, np.float64), np.asarray(T, np.int64)
    c = P[T].mean(1)
    la, lo, _ = frame.enu_to_geodetic(c[:, 0] + origin[0], c[:, 1] + origin[1], np.zeros(len(c)))
    keep = ~kwmod.covered(np.asarray(la), np.asarray(lo))
    if keep.all():
        return mesh
    T, M = T[keep], np.asarray(M)[keep]
    used = np.unique(T)
    remap = -np.ones(len(P), np.int64)
    remap[used] = np.arange(len(used))
    return P[used], remap[T], None if UV is None else np.asarray(UV)[used], mats, M


MESH_VERTS_MAX = 60000      # a road tile's meshes under the 65535 vertices the engine draws of an object (with room)


def _bgt_polygons(frame, tx, ty, chunks):
    """The BGT road polygons of road tile (tx, ty) not on a deck or in a tunnel (bgt.py), [] where there are none
    (--nl-bgt off, outside the Netherlands, PDOK not to be had: the guessed surfaces as before)."""
    if getattr(_s, "bgt", None) is None:
        return []
    import bgt as bgtmod
    try:
        return bgtmod.off_structures(frame, _s.bgt.polygons(frame, tx, ty), chunks)
    except Exception as e:          # the guessed surfaces rather than no road tile
        log.warning("bgt of tile %s_%s: %s: %s", tx, ty, type(e).__name__, e)
        return []


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
            if getattr(_s, "bgt", None) is not None:      # the BGT's underpasses instead of the map's tunnels there
                import bgt as bgtmod
                t.skip = bgtmod.skips_tunnel(_s.bgt, frame, t.raw)
            for old in sorted(_s.tunnels)[:-ANCHORS_KEPT]:
                del _s.tunnels[old]
        return t


def far_buildings_tile(aid, lat, lon, zoom, tx, ty, out_dir, vfs_dir):
    """earthd.Server.build_far_bld_tile here (road-tile pool): buildings.far_tile."""
    import buildings as buildingsmod
    frame = geodesy.EnuFrame(lat, lon)
    return buildingsmod.far_tile(frame, tx, ty, _s.md, lambda la, lo: ground(frame, la, lo, _s.dem.sample(la, lo, zoom)),
                                 out_dir, vfs_dir, aid)


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


WORKER_IMAGERY_TILES = 96      # decoded imagery tiles a far-tile worker keeps (imagery.MEM_TILES: 512, the server's)


def _imagery(tex):
    """This process's imagery.Imagery for the server's chain settings (tex: chain, tone, leaf_on, season_mode): the
    imagery tiles on disk alone (strict: a missing tile fails the render)."""
    import imagery as imagerymod
    key = (tuple(tex["chain"]), tex["tone"], tex["leaf_on"], tex["season_mode"])
    if _s.imagery is None or _s.imagery[0] != key:
        im = imagerymod.Imagery(os.path.join(_s.cache_dir, "imagery"), key[0], fetch_workers=1,
                                mem_tiles=WORKER_IMAGERY_TILES, tone=key[1], fetcher=netfetch.Fetcher(offline=True),
                                leaf_on=key[2], season_mode=key[3])
        im.strict = True
        _s.imagery = (key, im)
    return _s.imagery[1]


def base_photo(lat, lon, x0, y0, size, px, tex, buildings, rock_zoom, path, cache):
    """earthd.Server.write_base here, for a server without its imagery analyses (a small machine): the block's base
    photo from the imagery tiles on disk, its building footprints filled and its cliffs as rock, written to path
    (PNG) and packed into the game's terrain texture cache (cache: terraincache.base_entry's arguments but the rgb).
    Returns (rgb, sources used, lazy tiles still to come), or None when an imagery tile it needs is not on disk (the
    server renders it, fetching what is missing). In the server process it ran 2 s alone but 8-11 s next to the
    rest of the world's threads on a Steam Deck (one interpreter lock), and the next block waited for it."""
    import imagery as imagerymod
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
    try:            # (earthd.Server.write_base)
        n = 65
        xs = x0 + np.arange(n) * (size / (n - 1))
        ys = y0 + np.arange(n) * (size / (n - 1))
        _, U = heightmap.surface(frame, lambda la, lo: _s.dem.sample(la, lo, rock_zoom), xs, ys)
        rgb = rockfacemod.rock(rgb, frame, x0, y0, size, U, x0, y0, size / (n - 1))
    except Exception as e:
        log.debug("base photo: rock faces: %s", e)
    tmp = staging.tmp_for(path)
    Image.fromarray(rgb, "RGB").save(tmp, format="PNG", compress_level=1)
    staging.replace(tmp, path)
    user_dir, base_vfs, ao_vfs, ao_value = cache
    terraincache.base_entry(user_dir, base_vfs, ao_vfs, rgb, ao_value)
    return rgb, used, pending


def far_tile(aid, lat, lon, tile, zoom, q, ref_zoom, with_sink, layout, tex):
    """far_geometry and the tile's texture here, written to tex["path"] (the server without its imagery analyses:
    de-shadow and colour match off, a small machine). The texture from the imagery tiles on disk alone, the server's
    chain (Imagery.strict): (Built, (texture bytes, sources used)), or (Built, None) when a tile it needs is not on
    disk (the server renders it, fetching what is missing). On a Steam Deck the textures were most of the server
    process's own CPU, one interpreter for every thread (55 s of a drive), and the far field waited on it."""
    import imagery as imagerymod
    import rockface as rockfacemod
    geometry = getattr(far_geometry, "__wrapped__", far_geometry)        # (its CPU counted once, here)
    built = geometry(aid, lat, lon, tile, zoom, q, ref_zoom, with_sink, layout)
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
                log.debug("worker %d cpu: %s", os.getpid(),
                          ", ".join(f"{k} {v[0]} jobs {v[1]:.1f} s" for k, v in sorted(_cpu.items())))
    return run


build = _accounted(build)
roads_tile = _accounted(roads_tile)
far_buildings_tile = _accounted(far_buildings_tile)
base_entry = _accounted(base_entry)
far_geometry = _accounted(far_geometry)
far_tile = _accounted(far_tile)
base_photo = _accounted(base_photo)
navgraph = _accounted(navgraph)
