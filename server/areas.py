"""Saved areas (the user, 2026-10-28: "add the option to select an area and ... download all data"): a box picked on
the in-game map whose data (map data cells, elevation, imagery) is downloaded ahead of play, kept in the cache
(pinned: never evicted, and "Clear cache" keeps it) and resumed after a restart. Glue: the tile sets and the pin
test are Rust (native/earthfast areacover.rs); the downloads are the producers' own (mapdata, dem, imagery) at
netfetch's "predownload" kind (hosts that allow offline use only; at most PREDOWNLOAD_SHARE of a host's daily
budget, so live play keeps the rest).

Gates (nothing may slow live play): one request at a time, never while the player waits for a teleport's first
block (pacer.hold), only while the live worker pools are idle (Server.live_busy), and while driving at most DUTY_DRIVE
of the time. A refused request (Deferred: the host's spare tokens are gone) waits and tries again; a host's daily
share used up (BudgetExceeded) waits for the next UTC day ("waiting for tomorrow"); a failed one is skipped and the
area gets another pass later.

Store: <cache>/areas/areas.json (the list; atomic writes) and <cache>/areas/<id>.done (the imagery squares done, one
line each; appended). The plan of an area is a pure function of its box and the server's settings: a resume walks it
again and skips what is on disk.

Routes (the user, 2026-10-29: "options on pre downloading/generating along a gps route"): an area can be a corridor
instead of a box: the GPS route's points and a half width ("route": {"lat", "lon", "half"}; s, w, n, e its bounds).
Its plan takes the corridor's tiles at each zoom (earthfast corridor_tiles: in order along the route, so the start is
ready first) where a box takes its rectangle's; its pins are the cache keys under the corridor's map data cells
(keys_in_cover).

Modes and phases: an area's mode names the phases its job walks, in order (MODES); each phase is a plan of steps
(phase_steps) the thread fetches the same way. "data": the downloads. "world" (the user, 2026-10-29: "pre generate
along a route or area"): the downloads, then every road geotile's content (roads, sidewalks, bridges, buildings,
street furniture: built the same from any anchor since the geotiles) built into the revisit cache ahead of play
(earthd.Server.prebuild_geo), so arriving there loads it instead of building it. Its keys are listed in
<cache>/areas/<id>.world (rewritten each pass: the cache's code key changes with every update, the old ones fall
out) and kept out of the revisit cache's trim (pinned_world).
"""
import json
import logging
import math
import os
import threading
import time

import fast
import netfetch
import pacer

log = logging.getLogger("earthd.areas")

SUBDIR = "areas"
STORE = "areas.json"
STORE_VERSION = 1
MAX_AREAS = 10
MAX_KM2 = 2500.0            # (965 square miles) an area at most
MIN_KM2 = 0.05
PIN_SHARE = 0.9             # saved areas may pin at most this share of the cache's cap
NAME_MAX = 60
CELL_Z = 14                 # map data cells (mapcells.ZOOM)
CELL_RING = 2               # cells round the box (roads, bridges and buildings reaching in)
DEM_ZOOMS_FAR = (12, 13, 14)    # the far rings' and the ground filter's zooms (dem.GLOBAL_ZOOM ..)
DEM_RING = 1                # z12 tiles round the box (the ground filter's neighbours)
IMG_PARENT_Z = 13           # the imagery is walked in squares of this zoom
IMG_TEXEL_M = 1.0           # the block's base photo (earthd BASE_RENDER_PX over a 1 km block)
IMG_STEPS = (0, 2, 4)       # that zoom, and 2 and 4 coarser (the far tiles inside the area)
DUTY_DRIVE = 0.4            # share of the time while the player drives
IDLE_WAIT_S = 1.0           # the gates are looked at again after this
DEFER_WAIT_S = 3.0          # a refused request (no spare tokens) is tried again after this ...
DEFER_TRIES = 20            # ... this often, then skipped
BUDGET_RETRY_S = 900.0      # a host's daily share used up: looked at again after this
OFFLINE_RETRY_S = 300.0
PASS_RETRY_S = 1800.0       # a pass with failed items: another pass after this
STATUS_S = 2.0              # the 'areas' message at most this often while one runs
PIN_EVERY_S = 600.0         # the pinned bytes counted again after this (and after an area is added or removed)
# the estimate's rough numbers
MAPDATA_DL_BYTES = 6e6      # downloaded per new cell, all its layers (Overture's row groups are ~9.5 MB; neighbours
                            # share them)
REQUEST_S = 0.35            # one request at a time
CELL_S = 3.0                # a cell's layers built from the downloads
RAW_PER_CELL = 1.2e6        # the sources' blocks kept on disk per new cell (mapdata/raw: range reads, ~30 cells
                            # measured 2026-10-28)
ROUTE_HALF_M = (200.0, 5000.0)  # a route's half width (m): at least, at most
ROUTE_MAX_POINTS = 20000
ROUTE_PREVIEW_POINTS = 400  # the route drawn on the map (the 'areas' message): at most this many of its points
WORLD_Z = 17                # the road geotiles' zoom (geotiles.Z)
WORLD_TILE_BYTES = 0.78e6   # a road geotile's content in the revisit cache (mean of 482, Baltimore 2026-10-29)
WORLD_TILE_S = 2.0          # ... and its build in a road process (rough)
MAX_WORLD_KM2 = 400.0       # (155 square miles, ~4,300 road geotiles, ~3.4 GB) a "world" area at most
SIZES = {"dem": 110e3, "img": 25e3, "cell": 120e3}      # bytes on disk (a cell: one layer's file) when the cache
                                                        # knows no better


def area_layers():
    import mapdata as mapdatamod
    return tuple(mapdatamod.LAYERS) + ("building_parts", "places")


def check_box(s, w, n, e):
    """(s, w, n, e) as floats, or ValueError: a box inside Web Mercator's latitudes, not crossing the antimeridian,
    MIN_KM2 .. MAX_KM2."""
    try:
        s, w, n, e = (float(v) for v in (s, w, n, e))
    except (TypeError, ValueError):
        raise ValueError("an area needs south, west, north and east in degrees") from None
    if not all(math.isfinite(v) for v in (s, w, n, e)) or not (-85.0 < s < n < 85.0) or not (-180.0 <= w < e <= 180.0):
        raise ValueError("that box is not on the map (or it crosses the 180th meridian)")
    km2 = fast._ef.area_km2(s, w, n, e)
    if km2 > MAX_KM2:
        raise ValueError(f"too large: {km2 / 2.59:,.0f} square miles; at most {MAX_KM2 / 2.59:,.0f}")
    if km2 < MIN_KM2:
        raise ValueError("too small")
    return s, w, n, e


def check_route(lats, lons, half):
    """(lats, lons, half) as lists and a float, or ValueError: 2 .. ROUTE_MAX_POINTS points on the map, a half width
    in ROUTE_HALF_M, a corridor of at most MAX_KM2."""
    try:
        lats = [float(v) for v in lats]
        lons = [float(v) for v in lons]
        half = float(half)
    except (TypeError, ValueError):
        raise ValueError("a route needs its points and a width") from None
    if len(lats) != len(lons) or len(lats) < 2:
        raise ValueError("no route: plan one with the GPS first")
    if len(lats) > ROUTE_MAX_POINTS:
        raise ValueError("that route has too many points")
    if not all(math.isfinite(v) and -85.0 < v < 85.0 for v in lats) or \
            not all(math.isfinite(v) and -180.0 <= v <= 180.0 for v in lons):
        raise ValueError("that route is not on the map")
    if not (math.isfinite(half) and ROUTE_HALF_M[0] <= half <= ROUTE_HALF_M[1]):
        raise ValueError(f"the width each side must be {ROUTE_HALF_M[0]:.0f} .. {ROUTE_HALF_M[1]:.0f} m")
    km2 = fast._ef.corridor_size(lats, lons, half)[1]
    if km2 > MAX_KM2:
        raise ValueError(f"too large: {km2 / 2.59:,.0f} square miles; at most {MAX_KM2 / 2.59:,.0f} (a narrower "
                         "width, or a shorter route)")
    return lats, lons, half


def route_bounds(route):
    """(s, w, n, e) round a route's corridor."""
    lats, lons, half = route["lat"], route["lon"], route["half"]
    dlat = half / 111_195.0
    dlon = half / (111_195.0 * max(0.01, math.cos(math.radians(max(abs(min(lats)), abs(max(lats)))))))
    return (max(-85.0, min(lats) - dlat), max(-180.0, min(lons) - dlon), min(85.0, max(lats) + dlat),
            min(180.0, max(lons) + dlon))


def preview(route):
    """At most ROUTE_PREVIEW_POINTS of a route's points (evenly by index, the ends kept): [[lat, lon], ...]."""
    n = len(route["lat"])
    step = max(1, math.ceil((n - 1) / (ROUTE_PREVIEW_POINTS - 1)))
    idx = list(range(0, n, step))
    if idx[-1] != n - 1:
        idx.append(n - 1)
    return [[round(route["lat"][i], 6), round(route["lon"][i], 6)] for i in idx]


def box_tiles(box):
    """tiles(z, ring) -> [(x, y)] of a box (area_tiles)."""
    s, w, n, e = box
    return lambda z, ring: fast._ef.area_tiles(s, w, n, e, z, ring)


def route_tiles(route):
    """tiles(z, ring) -> [(x, y)] of a route's corridor, in order along it (corridor_tiles)."""
    lats, lons, half = route["lat"], route["lon"], route["half"]
    return lambda z, ring: [(x, y) for x, y, _ in fast._ef.corridor_tiles(lats, lons, half, z, ring)]


MODES = {"data": ("data",), "world": ("data", "world")}      # mode -> its phases, in order
DEFAULT_MODE = "data"


def check_mode(mode, server=None):
    """mode as given, or ValueError: one of MODES; "world" needs the revisit cache on and a server that pre-builds."""
    mode = str(mode or DEFAULT_MODE)
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    if "world" in MODES[mode]:
        import revisit
        if not revisit.ON or (server is not None and not hasattr(server, "prebuild_geo")):
            raise ValueError("building the world ahead needs the world cache (it is off)")
    return mode


def world_too_big(km2):
    return (f"too large to build the world ahead: {km2 / 2.59:,.0f} square miles; at most "
            f"{MAX_WORLD_KM2 / 2.59:,.0f} (or download its data only)")


def pinned_world(cache_dir):
    """The revisit keys the saved areas' world phases built (their <id>.world lists): kept out of the revisit cache's
    trim (earthd.prune_own_caches). Never raises."""
    out = set()
    d = os.path.join(cache_dir, SUBDIR)
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for n in names:
        if n.endswith(".world"):
            try:
                with open(os.path.join(d, n), encoding="utf-8") as f:
                    out.update(line.strip() for line in f if line.strip())
            except OSError:
                continue
    return out


def plan_steps(tiles, hm_zooms, img_zoom, layers):
    """The download plan of an area (tiles(z, ring) -> its [(x, y)] at zoom z with ring more round them: box_tiles,
    route_tiles), in order: ("cell", z, x, y, layer) for the map data cells (with CELL_RING), ("dem", z, x, y) at the
    near terrain's zooms (hm_zooms(x6, y6) -> its zoom under that z6 tile) and DEM_ZOOMS_FAR, then ("img",
    IMG_PARENT_Z, x, y, zoom) squares at img_zoom and IMG_STEPS coarser. Deterministic."""
    if isinstance(tiles, tuple):        # (a box)
        tiles = box_tiles(tiles)
    out = []
    for x, y in tiles(CELL_Z, CELL_RING):
        for layer in layers:
            out.append(("cell", CELL_Z, x, y, layer))
    near = {}
    for x, y in tiles(6, 0):
        near.setdefault(hm_zooms(x, y), True)
    zooms = sorted(set(DEM_ZOOMS_FAR) | set(near), reverse=True)
    for z in zooms:
        for x, y in tiles(z, DEM_RING if z == min(DEM_ZOOMS_FAR) else 0):
            if z in DEM_ZOOMS_FAR or hm_zooms(x >> (z - 6), y >> (z - 6)) == z:
                out.append(("dem", z, x, y))
    for step in IMG_STEPS:
        zw = img_zoom - step
        for x, y in tiles(IMG_PARENT_Z, 0):
            out.append(("img", IMG_PARENT_Z, x, y, zw))
    return out


class Areas:
    """The saved areas of one server: the list, its store, the download thread. server: an earthd.Server (dem,
    mapdata, imagery, net, cache, live_busy, driving); notify(): something changed (the 'areas' message)."""

    def __init__(self, cache_dir, server, notify=None):
        fast.require(114, "areas")
        self.dir = os.path.join(cache_dir, SUBDIR)
        self.s = server
        self.notify = notify or (lambda: None)
        self.lock = threading.RLock()
        self.areas = []                 # dicts (the store's fields + the runtime's)
        self.next_id = 1
        self.wake = threading.Event()
        self.closed = False
        self.current = None             # the area id the thread works on
        self.pinned_bytes = None
        self.pin_at = -1e18
        self.sent_at = 0.0
        self._thread = None
        self._load()

    # --- the store ----------------------------------------------------------------------------------
    def _load(self):
        try:
            with open(os.path.join(self.dir, STORE), encoding="utf-8") as f:
                doc = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.warning("areas: %s unreadable (%s): no saved areas", STORE, e)
            return
        for a in doc.get("areas", []) if isinstance(doc, dict) else []:
            try:
                route = a.get("route")
                if route is not None:
                    lats, lons, half = check_route(route["lat"], route["lon"], route["half"])
                    route = {"lat": lats, "lon": lons, "half": half}
                    box = route_bounds(route)
                else:
                    box = check_box(a["s"], a["w"], a["n"], a["e"])
                self.areas.append({"id": str(a["id"])[:16], "name": str(a.get("name", ""))[:NAME_MAX], "route": route,
                                   "s": box[0], "w": box[1], "n": box[2], "e": box[3],
                                   "created": float(a.get("created", 0)), "paused": bool(a.get("paused")),
                                   "complete": bool(a.get("complete")), "total": int(a.get("total", 0)),
                                   "diskBytes": int(a.get("diskBytes", 0)), "done": 0, "state": "queued", "msg": "",
                                   "retry_at": 0.0, "mode": a.get("mode") if a.get("mode") in MODES else DEFAULT_MODE})
            except (KeyError, TypeError, ValueError):
                continue
        self.next_id = max([int(doc.get("next", 1))] + [int(a["id"][1:]) + 1 for a in self.areas
                                                         if a["id"][1:].isdigit()]) if isinstance(doc, dict) else 1
        for a in self.areas:
            a["state"] = "paused" if a["paused"] else "done" if a["complete"] else "queued"
            a["done"] = a["total"] if a["complete"] else 0

    def _save(self):
        doc = {"v": STORE_VERSION, "next": self.next_id,
               "areas": [{k: a[k] for k in ("id", "name", "s", "w", "n", "e", "created", "paused", "complete", "total",
                                            "diskBytes", "mode", "route") if k != "route" or a[k] is not None}
                         for a in self.areas]}
        try:
            os.makedirs(self.dir, exist_ok=True)
            p = os.path.join(self.dir, STORE)
            tmp = f"{p}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(doc, f, indent=1)
            os.replace(tmp, p)
        except OSError as e:
            log.warning("areas: not saved: %s", e)

    def _done_path(self, aid):
        return os.path.join(self.dir, f"{aid}.done")

    def _world_path(self, aid):
        return os.path.join(self.dir, f"{aid}.world")

    def _write_world(self, aid, keys):
        """The area's pre-built road geotiles' revisit keys (pinned_world), the whole list at once (atomic)."""
        try:
            p = self._world_path(aid)
            tmp = f"{p}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("".join(k + "\n" for k in keys))
            os.replace(tmp, p)
        except OSError as e:
            log.warning("areas: %s world list not saved: %s", aid, e)

    def _done_set(self, aid):
        try:
            with open(self._done_path(aid), encoding="utf-8") as f:
                return {line.strip() for line in f if line.strip()}
        except OSError:
            return set()

    def _mark_done(self, aid, key):
        try:
            fd = os.open(self._done_path(aid), os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0),
                         0o644)
            try:
                os.write(fd, (key + "\n").encode())
            finally:
                os.close(fd)
        except OSError:
            pass

    # --- the plan and its estimate ----------------------------------------------------------------------
    def _hm_zooms(self, box):
        """x6, y6 -> the near terrain's DEM zoom there (earthd.new_anchor's, capped where Mapterhorn has less)."""
        import dem as demmod
        lat = (box[0] + box[2]) / 2
        hm = demmod.zoom_for(lat, 1.25, demmod.FINE_ZOOM)
        try:        # (Mapterhorn's file list: on disk, read again weekly; None when it cannot be had)
            cov = self.s.dem._coverage() if hasattr(self.s.dem, "_coverage") else demmod._coverage.get("index")
        except Exception:
            cov = None

        def zoom(x6, y6):
            cap = cov.get((x6, y6), demmod.GLOBAL_ZOOM) if cov is not None else demmod.GLOBAL_ZOOM
            return max(demmod.GLOBAL_ZOOM, min(hm, cap))
        return zoom

    def _img_zoom(self, box):
        import imagery as imagerymod
        return imagerymod.zoom_for((box[0] + box[2]) / 2, IMG_TEXEL_M)

    def phase_steps(self, phase, box, route=None):
        """The steps of one phase of an area's job ("data": plan_steps) for a box, or a route's corridor (box: its
        bounds)."""
        tiles = route_tiles(route) if route is not None else box_tiles(box)
        if phase == "data":
            return plan_steps(tiles, self._hm_zooms(box), self._img_zoom(box),
                              area_layers() if self.s.mapdata is not None else ())
        if phase == "world":
            return [("geo", WORLD_Z, x, y) for x, y in tiles(WORLD_Z, 0)]
        raise ValueError(f"unknown area phase {phase!r}")

    def steps(self, box, mode=DEFAULT_MODE, route=None):
        """Every step of an area's job: its mode's phases in order."""
        out = []
        for phase in MODES[mode]:
            out += self.phase_steps(phase, box, route)
        return out

    def _present(self, step, done_imgs=None):
        """Is a step's data on disk already (no network)?"""
        kind = step[0]
        if kind == "cell":
            import mapcells
            return self.s.mapdata.has(mapcells.CellKey(step[1], step[2], step[3]), step[4])
        if kind == "dem":
            return self.s.dem.has(step[1], step[2], step[3])
        if kind == "geo":
            return self.s.geo_prebuilt(step[2], step[3]) is not None
        return done_imgs is not None and img_key(step) in done_imgs

    def plan(self, s, w, n, e, mode=DEFAULT_MODE):
        """The confirm box's estimate of a new area: {ok, km2, items, missing, downloadBytes, diskBytes, seconds, days,
        hosts [[host, requests, days]], excluded [source names], worldTiles, worldMissing, error?}. No network."""
        try:
            box = check_box(s, w, n, e)
            mode = check_mode(mode, self.s)
        except ValueError as err:
            return {"ok": False, "error": str(err)}
        km2 = fast._ef.area_km2(*box)
        if "world" in MODES[mode] and km2 > MAX_WORLD_KM2:
            return {"ok": False, "error": world_too_big(km2)}
        return self._estimate(self.steps(box, mode), km2)

    def plan_route(self, lats, lons, half, mode=DEFAULT_MODE):
        """plan() for a route's corridor (half: its half width, m), with its length "km" too."""
        try:
            lats, lons, half = check_route(lats, lons, half)
            mode = check_mode(mode, self.s)
        except ValueError as err:
            return {"ok": False, "error": str(err)}
        route = {"lat": lats, "lon": lons, "half": half}
        length, km2 = fast._ef.corridor_size(lats, lons, half)
        if "world" in MODES[mode] and km2 > MAX_WORLD_KM2:
            return {"ok": False, "error": world_too_big(km2)}
        out = self._estimate(self.steps(route_bounds(route), mode, route), km2)
        out["km"] = round(length / 1000.0, 2)
        return out

    def _estimate(self, steps, km2):
        im = self.s.imagery
        sizes = self._sizes()
        req = {}
        missing = {"cell": set(), "layer": 0, "dem": 0, "img": 0}
        dl = disk = 0.0
        for st in steps:
            if st[0] == "cell":
                if not self._present(st):
                    missing["cell"].add(st[1:4])
                    missing["layer"] += 1
            elif st[0] == "dem":
                if not self._present(st):
                    missing["dem"] += 1
                    req["tiles.mapterhorn.com"] = req.get("tiles.mapterhorn.com", 0) + 1
                    dl += sizes["dem"]
        cells = len(missing["cell"])
        if cells:
            req["overturemaps-us-west-2.s3.us-west-2.amazonaws.com"] = cells * 4     # (a few range reads a cell)
        n_layers = max(1, len(area_layers()))
        disk = dl + missing["layer"] * sizes["cell"] + cells * RAW_PER_CELL
        dl += missing["layer"] * MAPDATA_DL_BYTES / n_layers
        excluded = set()
        if im is not None:
            for st in steps:
                if st[0] != "img":
                    continue
                for src, keys in im.predownload_sources(st[1], st[2], st[3], st[4], fetch_probes=False,
                                                        excluded=excluded):
                    host = netfetch.host_of(src.url)
                    todo = [k for k in keys if not im.on_disk_or_missing(k)]
                    if todo:
                        req[host] = req.get(host, 0) + math.ceil(len(todo) / 4 ** src.export)
                        missing["img"] += len(todo)
                        dl += len(todo) * sizes.get(src.name, sizes["img"])
                        disk += len(todo) * sizes.get(src.name, sizes["img"])
                    break
        geo = [st for st in steps if st[0] == "geo"]
        geo_missing = sum(1 for st in geo if not self._present(st))
        disk += geo_missing * WORLD_TILE_BYTES
        n_req = sum(req.values())
        hosts = []
        days = 0.0
        for host, r in sorted(req.items(), key=lambda kv: -kv[1]):
            p = self.s.net.policy(host) if self.s.net is not None else netfetch.DEFAULT_POLICY
            cap = (p.daily_budget or p.daily_hard)
            d = r / (netfetch.PREDOWNLOAD_SHARE * cap) if cap else 0.0
            days = max(days, d)
            hosts.append([host, int(r), round(d, 2)])
        seconds = n_req * REQUEST_S + cells * CELL_S + geo_missing * WORLD_TILE_S
        return {"ok": True, "km2": round(km2, 2), "items": len(steps),
                "missing": missing["layer"] + missing["dem"] + missing["img"],
                "downloadBytes": int(dl), "diskBytes": int(disk), "seconds": int(seconds), "days": round(days, 2),
                "hosts": hosts, "excluded": sorted(excluded), "worldTiles": len(geo), "worldMissing": geo_missing}

    def _sizes(self):
        """Average bytes a file of each kind takes in this cache (the estimate), else SIZES."""
        out = dict(SIZES)
        cache = self.s.cache
        if cache is None or not getattr(cache, "ready", False):
            return out
        sums = {}
        for k, b in cache.sizes():
            parts = k.split("/", 2)
            if len(parts) < 3 or not b:
                continue
            group = "dem" if parts[0] == "dem" else parts[1] if parts[0] == "imagery" else \
                "cell" if parts[0] == "mapdata" and parts[1].startswith("v") else None
            if group is None:
                continue
            t = sums.setdefault(group, [0, 0])
            t[0] += b
            t[1] += 1
        for g, (b, c) in sums.items():
            if c >= 20:
                out[g] = b / c
        return out

    # --- the list ---------------------------------------------------------------------------------
    def add(self, s, w, n, e, name="", mode=DEFAULT_MODE):
        """A new saved area (its download starts in the background). (True, area id) or (False, why)."""
        try:
            box = check_box(s, w, n, e)
            mode = check_mode(mode, self.s)
        except ValueError as err:
            return False, str(err)
        return self._add(box, None, self.plan(*box, mode=mode), name, mode)

    def add_route(self, lats, lons, half, name="", mode=DEFAULT_MODE):
        """A new saved route (its corridor: half m each side). (True, area id) or (False, why)."""
        try:
            lats, lons, half = check_route(lats, lons, half)
            mode = check_mode(mode, self.s)
        except ValueError as err:
            return False, str(err)
        route = {"lat": lats, "lon": lons, "half": half}
        return self._add(route_bounds(route), route, self.plan_route(lats, lons, half, mode), name, mode)

    def _add(self, box, route, est, name, mode=DEFAULT_MODE):
        if not est.get("ok"):
            return False, est.get("error") or "no estimate"
        with self.lock:
            if len(self.areas) >= MAX_AREAS:
                return False, f"at most {MAX_AREAS} saved areas: delete one first"
            cap = self.s.cache.cap if self.s.cache is not None else 0
            pinned = self.pinned_bytes or sum(a["diskBytes"] for a in self.areas)
            if cap and pinned + est.get("diskBytes", 0) > PIN_SHARE * cap:
                return False, (f"not enough room: the saved areas would hold {(pinned + est['diskBytes']) / 1e9:.1f} GB "
                               f"of the {cap / 1e9:.0f} GB cache (raise Download cache size in the settings)")
            aid = f"a{self.next_id}"
            self.next_id += 1
            label = str(name or "").strip()[:NAME_MAX] or f"{'Route' if route else 'Area'} {aid[1:]}"
            self.areas.append({"id": aid, "name": label, "route": route, "s": box[0], "w": box[1], "n": box[2],
                               "e": box[3],
                               "created": time.time(), "paused": False, "complete": False, "total": est.get("items", 0),
                               "diskBytes": est.get("diskBytes", 0), "done": 0, "state": "queued", "msg": "",
                               "retry_at": 0.0, "mode": mode})
            self._save()
        self.pin_at = -1e18
        self.wake.set()
        self._changed(force=True)
        return True, aid

    def command(self, aid, op):
        """pause / resume / delete an area (delete: it stops, its data stays in the cache, unpinned). True if done."""
        with self.lock:
            a = next((x for x in self.areas if x["id"] == aid), None)
            if a is None or op not in ("pause", "resume", "delete"):
                return False
            if op == "pause":
                a["paused"], a["state"], a["msg"] = True, "paused", ""
            elif op == "resume":
                a["paused"], a["complete"], a["retry_at"], a["state"], a["msg"] = False, False, 0.0, "queued", ""
            else:
                self.areas.remove(a)
                for p in (self._done_path(aid), self._world_path(aid)):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            self._save()
        if op == "delete":
            self.pin_at = -1e18
        self.wake.set()
        self._changed(force=True)
        return True

    def message(self):
        """The 'areas' message's fields."""
        with self.lock:
            out = {"areas": [dict({k: a[k] for k in ("id", "name", "s", "w", "n", "e", "state", "done", "total", "msg",
                                                     "mode")}, **({"route": self._preview(a), "half": a["route"]["half"]} if a["route"] else {}))
                             for a in self.areas]}
        if self.pinned_bytes is not None:
            out["pinnedBytes"] = int(self.pinned_bytes)
        return out

    @staticmethod
    def _preview(a):
        """A route area's points for the map (computed once)."""
        if "_preview" not in a:
            a["_preview"] = preview(a["route"])
        return a["_preview"]

    def _changed(self, force=False):
        now = time.monotonic()
        if force or now - self.sent_at >= STATUS_S:
            self.sent_at = now
            try:
                self.notify()
            except Exception:
                pass

    # --- pins -------------------------------------------------------------------------------------
    def boxes(self):
        """The saved areas' boxes with their ring of map data cells (what the plan reads); routes not."""
        pad = CELL_RING * 360.0 / (1 << CELL_Z)
        with self.lock:
            return [(max(-85.0, a["s"] - pad), max(-180.0, a["w"] - pad), min(85.0, a["n"] + pad),
                     min(180.0, a["e"] + pad)) for a in self.areas if not a["route"]]

    def cover(self):
        """The saved routes' map data cells (CELL_Z, with CELL_RING: what the plan reads) as (z, x, y) tiles."""
        with self.lock:
            routes = [a for a in self.areas if a["route"]]
        out = set()
        for a in routes:
            if "_cover" not in a:
                a["_cover"] = [(CELL_Z, x, y) for x, y in route_tiles(a["route"])(CELL_Z, CELL_RING)]
            out.update(a["_cover"])
        return sorted(out)

    def pinned(self, keys):
        """cache.Cache.pinned: [bool] per cache key (its z/x/y tile in a saved area or under a saved route)."""
        boxes, cover = self.boxes(), self.cover()
        keys = list(keys)
        mask = fast._ef.keys_in_boxes(keys, boxes) if boxes else [False] * len(keys)
        if cover:
            mask = [a or b for a, b in zip(mask, fast._ef.keys_in_cover(keys, cover))]
        return mask

    def count_pinned(self):
        cache = self.s.cache
        if cache is None or not getattr(cache, "ready", False):
            return
        if not self.areas:
            self.pinned_bytes = None
            cache.pinned_bytes = None
            return
        items = cache.sizes()
        mask = self.pinned([k for k, _ in items])
        self.pinned_bytes = sum(b for (_, b), p in zip(items, mask) if p)
        cache.pinned_bytes = self.pinned_bytes
        self.pin_at = time.monotonic()

    # --- the download thread --------------------------------------------------------------------------
    def start(self):
        if self.s.cache is not None:
            self.s.cache.pinned = self.pinned
        self._thread = threading.Thread(target=self._run, name="areas", daemon=True)
        self._thread.start()
        return self

    def close(self):
        self.closed = True
        self.wake.set()

    def _next(self):
        now = time.monotonic()
        with self.lock:
            for a in self.areas:
                if not a["paused"] and not a["complete"] and a["retry_at"] <= now:
                    return a
        return None

    def _run(self):
        while not self.closed:
            try:
                if time.monotonic() - self.pin_at >= PIN_EVERY_S:
                    self.count_pinned()
                a = self._next()
                if a is None:
                    self.wake.wait(30.0)
                    self.wake.clear()
                    continue
                self._pass(a)
            except Exception:
                log.exception("areas: download thread")
                self.wake.wait(60.0)
                self.wake.clear()

    def _alive(self, a):
        with self.lock:
            return not self.closed and a in self.areas and not a["paused"]

    def _gate(self, a):
        """Wait until the download may go on (False: the area was paused / deleted, or the server closes)."""
        while self._alive(a):
            pacer.hold()
            busy = getattr(self.s, "live_busy", None)
            if busy is not None and busy():
                self.wake.wait(IDLE_WAIT_S)
                self.wake.clear()
                continue
            return True
        return False

    def _pace(self, t0):
        """After a step: sleep so that the downloads run at most DUTY_DRIVE of the time while the player drives."""
        driving = getattr(self.s, "driving", None)
        if driving is not None and driving():
            spent = time.monotonic() - t0
            time.sleep(min(30.0, spent * (1.0 / DUTY_DRIVE - 1.0)))

    def _pass(self, a):
        """One walk over the area's plan: what is on disk is counted, the rest fetched."""
        box = (a["s"], a["w"], a["n"], a["e"])
        steps = self.steps(box, a.get("mode", DEFAULT_MODE), a["route"])
        done_imgs = self._done_set(a["id"])
        with self.lock:
            a["total"], a["done"], a["state"], a["msg"] = len(steps), 0, "checking", ""
        self._changed(force=True)
        failed = 0
        todo = []
        world = []                      # the pre-built geotiles' keys (the .world list)
        for st in steps:
            if st[0] == "geo":
                k = self.s.geo_prebuilt(st[2], st[3])
                if k is not None:
                    world.append(k)
                    a["done"] += 1
                    continue
                todo.append(st)
            elif self._present(st, done_imgs):
                a["done"] += 1
            else:
                todo.append(st)
        if any(st[0] == "geo" for st in steps):
            self._write_world(a["id"], world)
        a["_world"] = world
        with self.lock:
            a["state"] = "running"
        self._changed(force=True)
        self.current = a["id"]
        try:
            for st in todo:
                if not self._gate(a):
                    return
                ok = self._step(a, st)
                if ok is None:          # waiting (budget / offline): this pass ends
                    return
                if ok:
                    a["done"] += 1
                else:
                    failed += 1
                self._changed()
        finally:
            self.current = None
            if a.get("_world") is not None and any(st[0] == "geo" for st in steps):
                self._write_world(a["id"], a["_world"])
        with self.lock:
            if failed:
                a["state"], a["msg"] = "queued", f"{failed} items failed: tried again later"
                a["retry_at"] = time.monotonic() + PASS_RETRY_S
            else:
                a["complete"], a["state"], a["msg"], a["done"] = True, "done", "", a["total"]
            self._save()
        self.pin_at = -1e18
        self._changed(force=True)

    def _wait(self, a, msg, seconds):
        with self.lock:
            a["state"], a["msg"], a["retry_at"] = "waiting", msg, time.monotonic() + seconds
        self._changed(force=True)

    def _step(self, a, st):
        """One plan step's downloads: True done, False failed (skipped this pass), None the pass waits."""
        for _ in range(DEFER_TRIES):
            t0 = time.monotonic()
            try:
                self._fetch(a, st)
                self._pace(t0)
                return True
            except Exception as e:
                # (imagery wraps the request's error in an ImageryError: the netfetch one is its context)
                err = e.__context__ if type(e).__name__ == "ImageryError" and e.__context__ is not None else e
                if isinstance(err, netfetch.Offline):
                    self._wait(a, "offline: waiting", OFFLINE_RETRY_S)
                    return None
                if isinstance(err, netfetch.BudgetExceeded):
                    self._wait(a, f"waiting for tomorrow: {err.host or 'a host'}'s daily share for downloads ahead "
                                  "is used up", BUDGET_RETRY_S)
                    return None
                if isinstance(err, (netfetch.Deferred, netfetch.RateLimited)) or getattr(e, "deferred", False):
                    self.wake.wait(DEFER_WAIT_S)        # (no spare tokens now: live play has them)
                    self.wake.clear()
                    if not self._gate(a):
                        return False
                    continue
                # a host down, a bad answer: skipped, the next pass tries again
                log.debug("areas: %s %s: %s: %s", a["id"], st, type(e).__name__, e)
                self._pace(t0)
                return False
        return False

    def _fetch(self, a, st):
        kind = st[0]
        if kind == "geo":
            k = self.s.prebuild_geo(st[2], st[3])
            if k is None:
                raise RuntimeError("geotile content not complete")     # (a failed item: the next pass tries again)
            world = a.setdefault("_world", [])
            world.append(k)
            if len(world) % 25 == 0:
                self._write_world(a["id"], world)
            return
        if kind == "cell":
            import mapcells
            key = mapcells.CellKey(st[1], st[2], st[3])
            if self.s.mapdata.predownload(key, st[4]) and self.s.cache is not None and hasattr(self.s.cache, "wrote"):
                self.s.cache.wrote(self.s.mapdata.path(key, st[4]))     # (counted against the cap from now on)
        elif kind == "dem":
            self.s.dem.predownload(st[1], st[2], st[3])
        else:
            im = self.s.imagery
            if im is not None:
                for src, keys in im.predownload_sources(st[1], st[2], st[3], st[4], fetch_probes=True):
                    got = False
                    for k in keys:
                        if not self._alive(a):
                            raise netfetch.Deferred("area paused")
                        if im.on_disk_or_missing(k):
                            got = got or im.on_disk(k)
                            continue
                        if not self._gate(a):
                            raise netfetch.Deferred("area paused")
                        t0 = time.monotonic()
                        got = im.raw(*k, kind="predownload") is not None or got
                        self._pace(t0)
                    if got:
                        break
            self._mark_done(a["id"], img_key(st))


def img_key(step):
    return "/".join(str(v) for v in step[1:])
