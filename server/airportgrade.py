"""Runways graded into the ground (docs/research/2026-10-11-airports.md §6; ROADMAP quick win 4).

    ag = AirportGrader(db, dem)                 # db: airports.AirportDB, dem: dem.Dem
    z = ag.apply(lat, lon, z_dem)               # the DEM with the runways near (lat, lon) graded in
    src = ag.wrap(dem)                          # a dem.Dem-like sampler: sample(lat, lon, zoom) graded

The groups of runways (AirportDB.groups_in_cell) touching the map-data cells of the points' box are
planned once (runwaygeom.plan_group) and cached by their runway ids and the data snapshot. A plan's
profile samples the DEM at PLAN_ZOOM_M pixels (dem.zoom_for at the group's first runway), whichever block,
tile or process asks first, so every process grades a runway the same way and the physics block and the
mesh tiles meet. Points away from every plan's box keep the DEM bit for bit (runwaygeom.apply).
"""
import logging
import threading

import numpy as np

import dem as demmod
import geodesy
import mapcells
import runwaygeom
import runwaymarks

log = logging.getLogger("earthd.airportgrade")

PLAN_ZOOM_M = 2.0           # m: the DEM pixels a runway's profile is fitted on (z15 almost everywhere)
MAX_CELLS = 64              # a box spanning more map-data cells (a far tile) asks only its corners' cells
# Markings as road-tile chunks (roads.build_tile's format; the road mesh draws them like the lane lines)
MARK_PIECE_M = 50.0         # strokes cut into pieces this long, each in the tile of its midpoint
MARK_NODE_M = 10.0          # ... with a node this often (they follow the graded ground)
RUNWAY_PAD_SHOULDER_M = 1.5    # m of paved shoulder painted each side of a runway (runway_pads)
MARK_LOOK = {"white": {"m": "line_runway_white", "tl": 6.4, "p": 2},      # mod: levels/beamng_earth/art/runway
             "yellow": {"m": "line_runway_yellow", "tl": 6.4, "p": 2}}


class AirportGrader:
    def __init__(self, db, dem):
        self.db, self.dem = db, dem
        self._plans = {}                # frozenset of runway ids -> [Plan]
        self._group_locks = {}          # frozenset of runway ids -> the lock its planning holds
        self._mark_cache = {}           # runway id -> its markings in lat/lon (_marks)
        self._pad_cache = {}            # runway id -> its rectangle in lat/lon (runway_pads)
        self._lock = threading.Lock()
        self.stats = {"plans": 0, "graded_calls": 0}

    def _groups(self, lat, lon):
        la0, la1 = float(np.min(lat)), float(np.max(lat))
        lo0, lo1 = float(np.min(lon)), float(np.max(lon))
        try:
            keys = mapcells.cells_for_bbox(la0, lo0, la1, lo1, max_cells=MAX_CELLS)
        except ValueError:          # a very large box: its corners' and centre's cells (within the map cells' rows)
            if la0 > mapcells.MAX_LAT or la1 < -mapcells.MAX_LAT:
                return []
            m = mapcells.MAX_LAT - 1e-9
            la0, la1 = max(la0, -m), min(la1, m)
            keys = list(dict.fromkeys(mapcells.cell_of(a, o) for a, o in
                                      ((la0, lo0), (la0, lo1), (la1, lo0), (la1, lo1), ((la0 + la1) / 2, (lo0 + lo1) / 2))))
        out = {}
        for k in keys:
            for g in self.db.groups_in_cell(k):
                out.setdefault(frozenset(r.id for r in g), g)
        return [out[k] for k in sorted(out, key=min)]

    def plans_for(self, group):
        key = frozenset(r.id for r in group)
        with self._lock:
            p = self._plans.get(key)
            if p is not None:
                return p
            lock = self._group_locks.setdefault(key, threading.Lock())
        with lock:                      # one thread plans a group; the others wait for it
            with self._lock:
                p = self._plans.get(key)
            if p is not None:
                return p
            return self._plan(key, group)

    def _plan(self, key, group):
        rw0 = min(group, key=lambda r: r.id)
        zoom = demmod.zoom_for(rw0.center[0], PLAN_ZOOM_M)
        p = runwaygeom.plan_group(group, lambda la, lo: self.dem.sample(la, lo, zoom))
        with self._lock:
            self._plans[key] = p
            self.stats["plans"] += 1
        log.info("airports: planned %s (%d runway(s))", ", ".join(sorted({r.airport_ident for r in group})), len(group))
        return p

    def apply(self, lat, lon, z):
        """z (the DEM at lat, lon) with the runways near graded in; z itself when there are none."""
        lat = np.asarray(lat, np.float64)
        lon = np.asarray(lon, np.float64)
        if lat.size == 0:
            return z
        groups = self._groups(lat, lon)
        if not groups:
            return z
        plans = [p for g in groups for p in self.plans_for(g)]
        self.stats["graded_calls"] += 1
        return runwaygeom.apply(plans, lat, lon, z)[0]

    def tile_chunks(self, frame, tx, ty, elev, tile_bbox):
        """The runway markings (runwaymarks) inside road tile (tx, ty) of `frame`, as road chunks
        ({"m", "w", "tl", "p", "n": [[x, y, z], ...]}): every stroke near a runway touching the tile's
        map-data cells, cut into MARK_PIECE_M pieces, each in the tile of its midpoint; z from elev (the
        graded ground) at nodes MARK_NODE_M apart. tile_bbox(tx, ty) -> (x0, y0, x1, y1)."""
        x0, y0, x1, y1 = tile_bbox(tx, ty)
        la, lo, _ = frame.enu_to_geodetic(np.array([x0, x1, x0, x1]), np.array([y0, y0, y1, y1]), np.zeros(4))
        pieces = []
        for g in self._groups(la, lo):
            for rw in g:
                strokes = self._marks(rw)
                if not strokes:
                    continue
                pts = np.array([[a, b, c, d] for a, b, c, d, *_ in strokes])
                ex, ny, _ = frame.geodetic_to_enu(pts[:, [0, 2]].ravel(), pts[:, [1, 3]].ravel(), np.zeros(2 * len(pts)))
                ex, ny = np.asarray(ex).reshape(-1, 2), np.asarray(ny).reshape(-1, 2)
                for (sx0, sx1), (sy0, sy1), st in zip(ex, ny, strokes):
                    L = float(np.hypot(sx1 - sx0, sy1 - sy0))
                    n = max(1, int(np.ceil(L / MARK_PIECE_M)))
                    for i in range(n):
                        a, b = i / n, (i + 1) / n
                        mx, my = sx0 + (sx1 - sx0) * (a + b) / 2, sy0 + (sy1 - sy0) * (a + b) / 2
                        if not (x0 <= mx < x1 and y0 <= my < y1):
                            continue
                        k = max(1, int(np.ceil(L * (b - a) / MARK_NODE_M)))
                        f = np.linspace(a, b, k + 1)
                        pieces.append((sx0 + (sx1 - sx0) * f, sy0 + (sy1 - sy0) * f, st))
        if not pieces:
            return []
        # one elevation call for every node (the road grader's cost is per call, roads.build_tile)
        xs = np.concatenate([p[0] for p in pieces])
        ys = np.concatenate([p[1] for p in pieces])
        nla, nlo, _ = frame.enu_to_geodetic(xs, ys, np.zeros_like(xs))
        _, _, zs = frame.geodetic_to_enu(nla, nlo, np.asarray(elev(nla, nlo), np.float64))
        out, i = [], 0
        for px, py, st in pieces:
            m = len(px)
            look = MARK_LOOK.get(st[6], MARK_LOOK["white"])
            out.append({"m": look["m"], "w": round(float(st[4]), 2), "tl": look["tl"], "p": look["p"],
                        "n": [[round(float(x), 3), round(float(y), 3), round(float(z), 3)]
                              for x, y, z in zip(px, py, zs[i:i + m])]})
            i += m
        return out

    def runway_pads(self, lat, lon):
        """[(lat (4,), lon (4,))]: the paved, open runways near the points (their map-data cells) as rectangles end to
        end and RUNWAY_PAD_SHOULDER_M wider each side than the runway, painted in like the gas stations' forecourts
        (surface.Surface.extra_pads: the ground's asphalt layer and the photo). Only the markings were drawn: where
        the photo is soft or pale the runway looked unpaved (Tallinn, GitHub #337)."""
        out = []
        for g in self._groups(lat, lon):
            for rw in g:
                if not rw.paved or rw.closed or rw.surface == "water":
                    continue
                with self._lock:
                    pad = self._pad_cache.get(rw.id)
                if pad is None:
                    geo = runwaygeom.geometry(rw)
                    f = geodesy.EnuFrame(geo.lat_c, geo.lon_c, 0.0)
                    hl, hw = geo.length / 2, geo.width / 2 + RUNWAY_PAD_SHOULDER_M
                    ue, un = geo.ue, geo.un
                    e = np.array([-hl * ue - hw * -un, hl * ue - hw * -un, hl * ue + hw * -un, -hl * ue + hw * -un])
                    n = np.array([-hl * un - hw * ue, hl * un - hw * ue, hl * un + hw * ue, -hl * un + hw * ue])
                    la, lo, _ = f.enu_to_geodetic(e, n, np.zeros(4))
                    pad = (np.asarray(la, np.float64), np.asarray(lo, np.float64))
                    with self._lock:
                        self._pad_cache[rw.id] = pad
                out.append(pad)
        return out

    def _marks(self, rw):
        """runwaymarks.to_world of a runway, cached: [(lat0, lon0, lat1, lon1, width, kind, color)]."""
        with self._lock:
            m = self._mark_cache.get(rw.id)
        if m is None:
            m = runwaymarks.to_world(runwaygeom.geometry(rw), runwaymarks.markings(rw).strokes)
            with self._lock:
                self._mark_cache[rw.id] = m
        return m

    def wrap(self, dem):
        return GradedRunwayDem(dem, self)


class GradedRunwayDem:
    """A dem.Dem-like sampler with the runways graded in (for farfield.build_tile, grading.GradedDem)."""

    def __init__(self, dem, grader):
        self.dem, self.grader = dem, grader

    def sample(self, lat, lon, zoom, progress=None):
        z = self.dem.sample(lat, lon, zoom, progress) if progress is not None else self.dem.sample(lat, lon, zoom)
        return self.grader.apply(lat, lon, z)

    def __getattr__(self, name):        # everything else (zoom limits, stats...) is the DEM's
        return getattr(self.dem, name)
