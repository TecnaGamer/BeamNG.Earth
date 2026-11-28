"""One scene per airport, built once and cut to each tile (the user, 2026-11-17: "build each airport's scene once").

An airport's markings, lead-ins, stand marks, signs and taxiway centre lights are made from every Features of its cells
together (taxiwaymarks.Scene: a junction's fillet, a way that crosses a cell's edge, a stand's lane in the next cell).
They used to be made per road tile, from the cells that tile reads (its box and REACH_M round it), and every distinct list
of cells was a new Scene with its own lead-ins and names: Brisbane's 12 lists cost 64 s of CPU against 11 s for the
airport once, Atlanta's 49 lists (25 cells) about 4 minutes. Now the tile asks `of_cells` which airports its cells touch;
each is built once, whole, kept (the last CACHE_MAX), and the tile only cuts its part out of it (taxiwaymarks._clip,
airfieldlights._light_rows: both already clipped to the tile's box).

An airport is the set of cells that hold airport geometry (a way, a runway line, a holding position, a gate, an apron:
extent) and are chained: two cells are one airport when the airport features in them come within LINK_M of each other.
That relation is symmetric and depends on the data alone, so every tile reaching any cell of an airport gets the same
cells, in the same (row-major) order, whichever tile asked first. A cell that cannot be read (not downloaded, an error)
means no airport at all this time and nothing is kept: of_cells returns None and the caller does what it did before (a
scene of the tile's own cells), so a partial airport is never built and cached as final.

What is kept is checked against what a tile reads: each cell the tile reads has the stamp of its Features (the map data's
own: cell, layer, versions; the object itself where it has none) the airport was built from and the OurAirports snapshot
the runway lines were snapped to (runwaysnap.token). The map data's LRU may evict and reload a cell: the same stamp, the
same airport. Memory: the airport holds its cells' infrastructure Features (a few MB), the Scene and its marks (about the
size of the airport's lines and polygons as float arrays: tens of MB at the biggest airports; run tools or the tests'
tracemalloc for the number), not the buildings.
"""
import collections
import logging
import threading
import time

import numpy as np

import aeroways
import dmath
import mapcells
import parkedaircraft as pa
import runwaysnap
import taxiwaymarks as tm

log = logging.getLogger(__name__)

LINK_M = tm.REACH_M             # m: airport features of two cells this near each other are one airport (a way, a stand's lane across an edge)
MAX_CELLS = 64                  # an airport of more cells than this is not built whole (the tile's own cells: as before)
CACHE_MAX = 6                   # airports kept (LRU)
FAIL_RETRY_S = 20.0             # s a failed build (a cell that could not be read) is not tried again
STATS = {"floods": 0, "builds": 0, "marks": 0, "hits": 0, "failed": 0}
_EXT = "_airport_extent"
_LOCK = threading.RLock()
_AIRPORTS = collections.OrderedDict()   # airport key (its cells' strings) -> Airport, least recently used first
_CELL_OF = {}                           # str(cell) -> airport key
_FAILED = {}                            # str(cell) -> monotonic time of its airport's last failed build


def extent(feats):
    """(south, west, north, east) of the airport features (not the aerodrome's own polygon) of the infrastructure Features
    `feats` when it holds anything the airport modules draw from (a way, a runway line, a holding position, a gate or an
    apron), else None. Kept on it."""
    if feats is None or not len(feats):
        return None
    got = feats.__dict__.get(_EXT, False)
    if got is not False:
        return got
    got = None
    ways, runways, holds, _intl = tm._raw(feats)
    if ways or runways or holds or pa.has_stands(feats):
        # (not the aerodrome's own polygon or point: its property reaches over cells with nothing to draw in them)
        cls = np.asarray(feats.attrs.get("class")).astype(str)
        air = (np.asarray(feats.attrs.get("subtype")).astype(str) == aeroways.SUBTYPE) & np.array([pa._tier(c) is None for c in cls], bool)
        b = feats.lonlat_boxes()[air]
        b = b[np.isfinite(b).all(axis=1)] if len(b) else b
        if len(b):
            got = (float(b[:, 1].min()), float(b[:, 0].min()), float(b[:, 3].max()), float(b[:, 2].max()))
    feats.__dict__[_EXT] = got
    return got


def _gap_m(a, b):
    """The distance in metres between the boxes a and b (south, west, north, east): the larger of the two axes' gaps (0 where
    they overlap). Symmetric."""
    lat = (a[0] + a[2] + b[0] + b[2]) / 4.0
    dy = max(0.0, b[0] - a[2], a[0] - b[2]) * aeroways.M_PER_DEG
    dx = max(0.0, b[1] - a[3], a[1] - b[3]) * aeroways.M_PER_DEG * dmath.cos(dmath.radians(lat))
    return max(dx, dy)


def _near_cells(e):
    """The cells within LINK_M of the box e (south, west, north, east): the cells another airport cell's geometry that links
    with it can lie in."""
    pad = LINK_M * 1.02 / aeroways.M_PER_DEG
    worst = max(abs(e[0]), abs(e[2]))
    padlon = pad / max(dmath.cos(dmath.radians(min(worst, 85.0))), 0.05)
    return mapcells.cells_for_bbox(e[0] - pad, e[1] - padlon, e[2] + pad, e[3] + padlon)


def _order(k):
    return (k.y, k.x, k.z)


class _TooBig(Exception):
    pass


class Airport:
    """One airport: cells (its map cells, row-major), feats (their infrastructure Features in that order). scene(), marks()
    and stands_site() are made on first use and kept. Everything a tile reads of the airport is a plain read of these."""

    def __init__(self, cells, feats):
        feats = list(feats)
        self.cells = tuple(cells)
        self.feats = list({id(f): f for f in feats}.values())     # (a Features once, in cell order)
        self.by_cell = dict(zip(self.cells, feats))
        self.key = tuple(str(c) for c in self.cells)
        self.stamps = {str(c): aeroways.stamp(f) for c, f in zip(self.cells, feats)}
        self.token = runwaysnap.token()
        self._lock = threading.RLock()
        self._scene = False
        self._marks = None                      # (lines, boxes, polys, pboxes) of taxiwaymarks._all_marks
        self._stands = None                     # (stands, site) of stands_site, once both were made

    def valid_for(self, key, feats):
        """Is the airport still what the cell `key` (its Features now `feats`) says it is?"""
        return self.token == runwaysnap.token() and self.stamps.get(str(key)) == aeroways.stamp(feats)

    def scene(self):
        """The taxiwaymarks Scene of the whole airport (None without a way), made once."""
        with self._lock:
            if self._scene is False:
                self._scene = tm.scene(self.feats)
            return self._scene

    def stands_site(self, infra_fn, buildings_fn, load=None):
        """(stands, site, complete): the airport's stands with a lead-in (parkedaircraft.lead_stands over all its cells: each
        cell's judged from the cells round its own gates and aprons) and the Site of its terminals (taxiwaymarks.site_of);
        complete False where one failed (logged; the centrelines are worth more than the lead-ins, the stands' marks more than
        the service roads), which is not kept. infra_fn(key) -> infrastructure Features, buildings_fn(key) -> [buildings,
        building_parts] (parkedaircraft.building_layers_fn), load(keys, layers): the map data's read-ahead of the cells
        (optional: they are asked for one by one without it)."""
        with self._lock:
            if self._stands is not None:
                return self._stands[0], self._stands[1], True
            stands, site, complete = [], None, True
            if any(pa.has_stands(f) for f in self.feats):
                if load is not None:
                    try:
                        load(list(self.cells), ("buildings", "building_parts"))
                    except Exception:           # (a read-ahead: each cell is asked for below, and fails there if it is not there)
                        pass
                near = lambda k: self.by_cell[k] if k in self.by_cell else infra_fn(k)
                try:
                    stands = pa.lead_stands(self.cells, near, buildings_fn)
                except Exception as ex:         # (the centrelines are worth more than the lead-ins)
                    log.warning("stand lead-ins: %s: %s", type(ex).__name__, ex)
                    stands, complete = [], False
                if stands:
                    try:
                        got = [buildings_fn(k) for k in self.cells]
                        site = tm.site_of(self.feats, [g[0] for g in got], [g[1] for g in got])
                    except Exception as ex:     # (the stands' marks are worth more than the roads and the red boxes)
                        log.warning("stand marks' site: %s: %s", type(ex).__name__, ex)
                        site, complete = None, False
            if complete:
                self._stands = (stands, site)
            return stands, site, complete

    def marks(self, infra_fn, buildings_fn, load=None):
        """(lines, boxes, polys, pboxes) of taxiwaymarks._all_marks for the whole airport with its stands and site: made once
        (not kept where the stands or the site could not be made)."""
        with self._lock:
            if self._marks is not None:
                return self._marks
            stands, site, complete = self.stands_site(infra_fn, buildings_fn, load)
            got = tm._all_marks(self.feats, stands, site)
            STATS["marks"] += 1
            if complete:
                self._marks = got
            return got


def _unregister(ap):
    _AIRPORTS.pop(ap.key, None)
    for k in ap.key:
        if _CELL_OF.get(k) == ap.key:
            del _CELL_OF[k]


def _flood(seed, seed_feats, infra_fn, prefetch):
    """({cell: Features} of the airport the cell `seed` belongs to: every cell reached from it through links (LINK_M between the
    airport features of the two cells), read through infra_fn (raises where a cell cannot be read)."""
    comp = {seed: seed_feats}
    ext = {seed: extent(seed_feats)}
    feats_of = {seed: seed_feats}
    known = {seed: ext[seed]}                   # every cell looked at: its extent (None: no airport geometry in it)
    queue = [seed]
    while queue:
        k = queue.pop()
        cand = [c for c in _near_cells(ext[k]) if c != k]
        new = [c for c in cand if c not in known]
        if new and prefetch is not None:
            try:
                prefetch(new, ("infrastructure",))
            except Exception:                   # (a read-ahead: each cell is asked for below)
                pass
        for c in new:
            f = infra_fn(c)
            feats_of[c] = f
            known[c] = extent(f)
        for c in cand:
            if c in comp or known.get(c) is None:
                continue
            if _gap_m(ext[k], known[c]) <= LINK_M:
                comp[c], ext[c] = feats_of[c], known[c]
                queue.append(c)
                if len(comp) > MAX_CELLS:
                    raise _TooBig(f"more than {MAX_CELLS} cells")
    STATS["floods"] += 1
    return comp


def of_cells(infra_fn, keys, prefetch=None):
    """The Airports the infrastructure cells `keys` touch (each built whole, once, and kept): [] where no cell holds airport
    geometry; None where an airport could not be built (a cell round it could not be read, or it is more than MAX_CELLS
    cells): nothing is kept then, and the caller reads its own cells as it did before. infra_fn(key) -> the cell's
    infrastructure Features (or None), read for the keys (an error there is the caller's, as for any tile) and for the
    cells round an airport. prefetch(keys, layers): the map data's read-ahead (optional)."""
    keys = list(keys)
    if not all(isinstance(k, mapcells.CellKey) for k in keys):
        return None                             # (not map cells: nothing to chain by)
    seeds = {}
    for k in keys:
        if k not in seeds:
            f = infra_fn(k)
            if extent(f) is not None:
                seeds[k] = f
    if not seeds:
        return []
    out, covered = [], set()
    with _LOCK:
        for k in sorted(seeds, key=_order):
            if k in covered:
                continue
            ap = _AIRPORTS.get(_CELL_OF.get(str(k)))
            if ap is not None and not ap.valid_for(k, seeds[k]):
                _unregister(ap)                 # (another version of the data: the airport is made again)
                ap = None
            if ap is None:
                if time.monotonic() - _FAILED.get(str(k), -FAIL_RETRY_S) < FAIL_RETRY_S:
                    return None
                try:
                    comp = _flood(k, seeds[k], infra_fn, prefetch)
                    cells = sorted(comp, key=_order)
                    ap = Airport(cells, [comp[c] for c in cells])
                except Exception as ex:         # (a cell that could not be read, or too big: no airport, nothing kept)
                    STATS["failed"] += 1
                    _FAILED[str(k)] = time.monotonic()
                    if len(_FAILED) > 4096:
                        _FAILED.clear()
                    log.debug("airport at %s not built whole: %s: %s", k, type(ex).__name__, ex)
                    return None
                STATS["builds"] += 1
                _AIRPORTS[ap.key] = ap
                for c in ap.key:
                    _CELL_OF[c] = ap.key
                while len(_AIRPORTS) > CACHE_MAX:
                    _unregister(next(iter(_AIRPORTS.values())))
            else:
                STATS["hits"] += 1
                _AIRPORTS.move_to_end(ap.key)
            out.append(ap)
            covered.update(ap.cells)
    return out


def clear():
    """Forget every airport and zero STATS (the tests': a test file run after another in one process starts from none)."""
    with _LOCK:
        _AIRPORTS.clear()
        _CELL_OF.clear()
        _FAILED.clear()
        for k in STATS:
            STATS[k] = 0
