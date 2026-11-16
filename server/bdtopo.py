"""France's measured buildings: IGN BD TOPO `batiment` (Etalab Licence Ouverte 2.0) joined to the map's footprints.

Ported from Madderose's PR #456 (ign_fr.py: the WFS request, the material codes, the idea of joining BD TOPO's heights,
storeys, roof / wall materials and use to the map's buildings), rebuilt the way the German LoD2 roofs (lod2roofs.py)
and the IGN road heights (roadz.py) flow: nothing on a tile's path waits serially on IGN.

- What comes down: per z14 map cell, the cell's `batiment` objects from the Geoplateforme WFS (data.geopf.fr, the
  "ign_wfs" lane of netfetch: its rate limit, User-Agent and host policy; gzip; 5 000 a page, a central Paris cell is
  ~5 400 buildings, ~0.6 MB gzipped a page). Kept on disk as one small record per building (BDTOPO_DIR/<z>/<x>/<y>.npz:
  a point inside its outline, its area, eave height, roof rise, storeys, dwellings, wall / roof material codes, use,
  nature), the outlines dropped (earthfast.bdtopo_points). A cell with no French building is kept as an empty record
  set; a failed request is not kept (asked again after RETRY_S), so the predicted buildings are never cached for good.
- When: MapData.warm / prefetch of a cell's buildings layer starts the cell's download in the background (THREADS at
  once, one process per cell: xproc lock), so it runs beside the Overture download of the same cell. A road or far
  building tile waits for its cells' records together (wait(), at most WAIT_S, SLOW_WAIT_S for a while after one did
  not come); MapData.cell() joins the records into a decoded buildings layer as soon as they are there and never waits.
  A French buildings layer handed out without its records is a revisit fault (the tile built from it is not kept).
- The join (earthfast.bdtopo_match): the record whose point stands inside a footprint, the largest when several do.
  What the map says itself wins (a mapped height, storeys, roof shape, materials or class are kept); a machine-learned
  height (buildings.ML_HEIGHT_SOURCES) gives way to the measured one.

BD TOPO's `hauteur` is the height from the lowest ground to the lowest point of the roof (the eaves: checked against
altitude_minimale_toit - altitude_minimale_sol on 500 Aix-en-Provence buildings, 2026-11-10); the roof's rise is
altitude_maximale_toit - altitude_minimale_toit (none in central Paris). The material codes are the DGFiP's (MAJIC):
walls 1 stone, 2 millstone, 3 concrete, 4 brick, 5 concrete block, 6 wood, 9 other; roofs 1 tiles, 2 slate, 3 zinc /
aluminium, 4 concrete, 9 other; two digits, the first the main material (Paris: roofs "30", zinc; Aix: "10", tiles).

Off: BNE_BDTOPO=off. Outside France (FRANCE: a coarse outline of the mainland and Corsica, the overseas departments'
boxes) nothing is asked and nothing changes.
"""
import gzip
import logging
import math
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import numpy as np

import fast
import dmath
import mapcells

log = logging.getLogger("earthd.bdtopo")

ON = os.environ.get("BNE_BDTOPO", "on") != "off"
NATIVE = 183                    # earthfast.VERSION of bdtopo_points / bdtopo_match
VERSION = 1                     # the records' layout on disk (BDTOPO_DIR)
WFS_URL = "https://data.geopf.fr/wfs/ows"
LANE = "ign_wfs"                # (roadz's lane: one bucket for IGN's WFS)
PAGE = 5000                     # the Geoplateforme's most features a request
PAGES = 12
FIELDS = ("geometrie,nature,usage_1,construction_legere,nombre_d_etages,nombre_de_logements,materiaux_des_murs,"
          "materiaux_de_la_toiture,hauteur,altitude_minimale_toit,altitude_maximale_toit")
THREADS = 4                     # background cell downloads at once per process (the host allows 12, roadz shares it)
# (BNGE_: not part of the revisit cache's code key; the wait changes when a tile is built, not what)
WAIT_S = float(os.environ.get("BNGE_BDTOPO_WAIT", "4"))
SLOW_WAIT_S = 0.5               # a tile's wait for a while after one did not come in WAIT_S (a slow line, IGN busy)
SLOW_FOR_S = 60.0
RETRY_S = 600.0                 # a cell whose download failed is asked again after this
LOCK_WAIT_S = 90.0              # another process downloading the same cell: waited for this long at most
MEM_CELLS = 128
RISE_FLAT_M = 0.8               # a measured roof rise below this: a flat roof (Aix's pitched roofs: 1.2-4.5 m; the
                                # same as buildings.RISE_FLAT_M)
RISE_MAX_M = 15.0               # above: a tower's spire or a bad survey point, no roof rise known

# metropolitan France (lon, lat), a coarse outline a few km outside the land borders and off the coast: a cell is
# French when it overlaps it. Monaco, Andorra and a strip of the neighbours' border towns fall inside: their cells ask
# once and keep an empty record set (the WFS has no buildings there).
_MAINLAND = np.array([
    (2.50, 51.12), (2.95, 50.82), (3.30, 50.82), (3.75, 50.56), (4.25, 50.38), (4.72, 50.21), (4.90, 50.20),
    (4.95, 49.82), (5.45, 49.62), (5.85, 49.58), (6.40, 49.50), (6.75, 49.25), (7.10, 49.18), (7.65, 49.10),
    (8.27, 49.01), (7.95, 48.62), (7.70, 48.30), (7.65, 47.95), (7.62, 47.60), (7.20, 47.52), (6.98, 47.52),
    (6.50, 47.12), (6.20, 46.75), (6.15, 46.42), (6.00, 46.30), (6.25, 46.32), (6.55, 46.47), (6.85, 46.45),
    (6.85, 46.12), (7.08, 45.85), (7.20, 45.48), (6.90, 45.12), (7.10, 44.85), (6.95, 44.45), (7.12, 44.20),
    (7.72, 44.12), (7.60, 43.70), (7.10, 43.38), (6.20, 42.95), (5.00, 43.10), (4.20, 43.35), (3.40, 43.10),
    (3.10, 42.40), (2.55, 42.30), (1.95, 42.32), (1.40, 42.40), (0.70, 42.78), (0.00, 42.62), (-0.75, 42.88),
    (-1.40, 42.98), (-1.85, 43.30), (-1.95, 43.55), (-1.55, 44.60), (-1.50, 45.40), (-2.45, 46.55), (-2.85, 47.15),
    (-3.60, 47.55), (-4.85, 47.70), (-5.25, 48.40), (-4.80, 48.80), (-3.00, 49.00), (-1.95, 48.75), (-2.05, 49.75),
    (-1.20, 49.80), (-0.20, 49.45), (0.05, 49.75), (1.40, 50.15), (1.50, 51.00)], np.float64)
# Corsica, and the overseas departments (lon0, lat0, lon1, lat1): Guadeloupe, Martinique, Guyane, La Reunion, Mayotte
_BOXES = ((8.50, 41.33, 9.62, 43.05), (-61.90, 15.80, -60.95, 16.55), (-61.25, 14.35, -60.75, 14.90),
          (-54.70, 2.05, -51.55, 5.85), (55.15, -21.42, 55.88, -20.85), (44.95, -13.05, 45.35, -12.60))

# what the records' codes become (the map's own value always wins): use (usage_1) and nature, in USES / NATURES order
USES = ("", "Résidentiel", "Commercial et services", "Industriel", "Agricole", "Religieux", "Sportif", "Annexe")
NATURES = ("", "Eglise", "Chapelle", "Serre", "Silo", "Industriel, agricole ou commercial")
WALLS = {1: "stone", 2: "stone", 3: "concrete", 4: "brick", 5: "plaster", 6: "wood"}
ROOFS = {1: "tile", 2: "slate", 3: "zinc", 4: "concrete"}
HOUSE_MAX_M2 = 300.0            # one dwelling, at most 3 storeys, at most this big: a house
SHED_MAX_M2 = 100.0             # an annex or a light construction this small: a shed
GENERIC_CLASSES = ("", "yes", "building")


def _in_ring(ring, x, y):
    c = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[i - 1]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            c = not c
    return c


def covers(bounds):
    """Does a cell (lon0, lat0, lon1, lat1) overlap France (FRANCE's outline or boxes)? Pure, no network."""
    lon0, lat0, lon1, lat1 = (float(v) for v in bounds)
    for b in _BOXES:
        if lon1 >= b[0] and lon0 <= b[2] and lat1 >= b[1] and lat0 <= b[3]:
            return True
    if lon1 < -5.3 or lon0 > 8.3 or lat1 < 42.2 or lat0 > 51.2:
        return False
    for x, y in ((lon0, lat0), (lon1, lat0), (lon0, lat1), (lon1, lat1), ((lon0 + lon1) / 2, (lat0 + lat1) / 2)):
        if _in_ring(_MAINLAND, x, y):
            return True
    m = _MAINLAND
    return bool(((m[:, 0] >= lon0) & (m[:, 0] <= lon1) & (m[:, 1] >= lat0) & (m[:, 1] <= lat1)).any())


def _key(key):
    return mapcells.CellKey(int(key[0]), int(key[1]), int(key[2]))


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return math.nan
    return f if math.isfinite(f) else math.nan


def _code(v):
    s = str(v or "").strip()
    return int(s) if s.isdigit() and len(s) <= 2 else 255


def parse(features):
    """The records (dict of arrays) of BD TOPO `batiment` GeoJSON features (CRS:84): one per building with an outline
    of 3 points or more. Pure (but for the native points)."""
    fast.require(NATIVE, "bdtopo_points")
    coords, off = [], [0]
    h, rise, floors, dwell, wall, roof, use, nat, light = [], [], [], [], [], [], [], [], []
    for f in features or ():
        if not isinstance(f, dict):
            continue
        g = f.get("geometry") or {}
        cs = g.get("coordinates") if isinstance(g, dict) else None
        if not isinstance(cs, list) or not cs:
            continue
        multi = g.get("type") == "MultiPolygon"
        ring = cs[0] if g.get("type") == "Polygon" else (cs[0][0] if multi and cs[0] else None)
        try:
            ring = np.asarray([(float(p[0]), float(p[1])) for p in ring], np.float64)
        except (TypeError, ValueError, IndexError):
            continue
        if len(ring) < 4 or not np.isfinite(ring).all():
            continue
        p = f.get("properties") or {}
        coords.append(ring)
        off.append(off[-1] + len(ring))
        eave = _num(p.get("hauteur"))
        h.append(eave if eave > 0 else math.nan)
        r = _num(p.get("altitude_maximale_toit")) - _num(p.get("altitude_minimale_toit"))
        rise.append(r if 0.0 <= r <= RISE_MAX_M else math.nan)
        nf = _num(p.get("nombre_d_etages"))
        floors.append(int(nf) if nf > 0 else 0)
        nd = _num(p.get("nombre_de_logements"))
        dwell.append(int(nd) if nd >= 0 else -1)
        wall.append(_code(p.get("materiaux_des_murs")))
        roof.append(_code(p.get("materiaux_de_la_toiture")))
        u = str(p.get("usage_1") or "")
        use.append(USES.index(u) if u in USES else 0)
        n_ = str(p.get("nature") or "")
        nat.append(NATURES.index(n_) if n_ in NATURES else 0)
        light.append(bool(p.get("construction_legere")))
    if coords:
        pts, area = fast._ef.bdtopo_points(np.asarray(off, np.int64), np.ascontiguousarray(np.concatenate(coords)))
    else:
        pts, area = np.zeros((0, 2)), np.zeros(0)
    return {"lon": np.asarray(pts[:, 0], np.float64), "lat": np.asarray(pts[:, 1], np.float64),
            "area": np.asarray(area, np.float32), "h": np.asarray(h, np.float32),
            "rise": np.asarray(rise, np.float32), "floors": np.asarray(floors, np.int16),
            "dwell": np.asarray(dwell, np.int16), "wall": np.asarray(wall, np.uint8),
            "roof": np.asarray(roof, np.uint8), "use": np.asarray(use, np.uint8), "nature": np.asarray(nat, np.uint8),
            "light": np.asarray(light, bool)}


FIELDS_NPZ = ("lon", "lat", "area", "h", "rise", "floors", "dwell", "wall", "roof", "use", "nature", "light")


def interpret(r):
    """The map attributes each record gives (arrays over the records): height (m, total: eaves + roof rise; NaN none),
    eave_only (the height is the eaves' alone: no rise measured), roof_rise (m, NaN none), num_floors (NaN none),
    facade_material / roof_material / class (str, "" none). Pure."""
    n = len(r["lon"])
    h = r["h"].astype(np.float64)
    rise = r["rise"].astype(np.float64)
    out = {"height": np.where(np.isfinite(rise) & (rise >= RISE_FLAT_M), h + rise, h),
           "eave_only": ~np.isfinite(rise), "roof_rise": rise,
           "num_floors": np.where(r["floors"] > 0, r["floors"], np.nan)}
    use, nat, area = r["use"], r["nature"], r["area"].astype(np.float64)
    cls = np.full(n, "", dtype=object)
    cls[use == USES.index("Commercial et services")] = "commercial"
    cls[use == USES.index("Industriel")] = "industrial"
    cls[use == USES.index("Agricole")] = "farm_auxiliary"
    cls[((use == USES.index("Annexe")) | r["light"]) & (area < SHED_MAX_M2)] = "shed"
    cls[(use == USES.index("Résidentiel")) & (r["dwell"] == 1) & (r["floors"] <= 3) & (area < HOUSE_MAX_M2)] = "house"
    cls[nat == NATURES.index("Serre")] = "greenhouse"
    cls[nat == NATURES.index("Silo")] = "silo"
    cls[nat == NATURES.index("Eglise")] = "church"
    cls[nat == NATURES.index("Chapelle")] = "chapel"
    w1 = r["wall"].astype(np.int64) // 10
    w1 = np.where(r["wall"] < 10, r["wall"], w1)            # (a one-digit code: that digit)
    wall = np.array([WALLS.get(int(c), "") for c in w1], dtype=object)
    # (concrete walls of a house or a block of flats are rendered in France: plaster; bare on a shed or a works)
    housing = np.isin(use, (USES.index("Résidentiel"), USES.index("Commercial et services"), 0))
    wall[(wall == "concrete") & housing] = "plaster"
    r1 = np.where(r["roof"] < 10, r["roof"], r["roof"].astype(np.int64) // 10)
    roof = np.array([ROOFS.get(int(c), "") for c in r1], dtype=object)
    out.update(cls=cls, facade_material=wall, roof_material=roof)
    return out


def join(feats, recs):
    """The attrs of buildings layer feats with what the records add (a new dict: the layer is shared, it is swapped
    whole), or None when no footprint has a record. The map's own values stay. recs: Records."""
    import mapgeom
    n = len(feats)
    if n == 0 or recs.n == 0:
        return None
    po, ro, co = (np.asarray(a, np.int64) for a in (feats.part_off, feats.ring_off, feats.coord_off))
    has = (po[1:] > po[:-1]) & (np.asarray(feats.kind) == mapgeom.POLYGON)
    first = ro[np.minimum(po[:-1], len(ro) - 1)]
    first = np.minimum(first, len(co) - 2)
    start = np.where(has, co[first], 0)
    end = np.where(has, co[first + 1], 0)
    idx = np.asarray(fast._ef.bdtopo_match(np.ascontiguousarray(start), np.ascontiguousarray(end),
                                           np.ascontiguousarray(feats.lonlat, np.float64), recs.lon, recs.lat,
                                           recs.area.astype(np.float64)), np.int64)
    m = idx >= 0
    if not m.any():
        return None
    j = idx[m]
    a = dict(feats.attrs)
    it = recs.attrs

    def num_col(name):
        v = a.get(name)
        return np.asarray(v, np.float32).copy() if v is not None else np.full(n, np.nan, np.float32)

    def text(name):
        v = a.get(name)
        return np.asarray(v).astype(str).astype(object) if v is not None else np.full(n, "", dtype=object)

    import buildings
    src = text("height_src")
    height = num_col("height")
    ml = np.isin(src, list(buildings.ML_HEIGHT_SOURCES))
    rec_h = it["height"][j]
    take = ~np.isfinite(height[m]) | ml[m]
    take &= np.isfinite(rec_h)
    rows = np.flatnonzero(m)[take]
    height[rows] = rec_h[take]
    src[rows] = np.where(it["eave_only"][j][take], "ign_bdtopo_eave", "ign_bdtopo")
    a["height"], a["height_src"] = height, mapgeom.text_column(src.tolist())
    floors = num_col("num_floors")
    rec_f = it["num_floors"][j]
    t = ~np.isfinite(floors[m]) & np.isfinite(rec_f)
    floors[np.flatnonzero(m)[t]] = rec_f[t]
    a["num_floors"] = floors
    # the measured rise (plan: a pitched or flat roof where the map names none), and the roof's own height
    rise = np.full(n, np.nan, np.float32)
    rise[m] = it["roof_rise"][j]
    a["roof_rise"] = rise
    rh = num_col("roof_height")
    rec_r = it["roof_rise"][j]
    t = ~np.isfinite(rh[m]) & np.isfinite(rec_r) & (rec_r >= RISE_FLAT_M)
    rh[np.flatnonzero(m)[t]] = rec_r[t]
    a["roof_height"] = rh
    for name, key in (("facade_material", "facade_material"), ("roof_material", "roof_material"), ("class", "cls")):
        col = text(name)
        rec_v = it[key][j]
        t = np.isin(np.char.lower(col[m].astype(str)), GENERIC_CLASSES if name == "class" else ("",)) & (rec_v != "")
        col[np.flatnonzero(m)[t]] = rec_v[t]
        a[name] = mapgeom.text_column(col.tolist())
    return a


class Records:
    """A cell's records (FIELDS_NPZ arrays) and what they give (interpret)."""

    def __init__(self, arrays):
        self.lon = np.ascontiguousarray(arrays["lon"], np.float64)
        self.lat = np.ascontiguousarray(arrays["lat"], np.float64)
        self.area = np.ascontiguousarray(arrays["area"], np.float32)
        self.n = len(self.lon)
        self.raw = arrays
        self.attrs = interpret(arrays) if self.n else {}


EMPTY = Records({k: np.zeros(0) for k in FIELDS_NPZ})


class Source:
    """The cells' records: fetched in the background, kept on disk and in memory (see the module docstring)."""

    def __init__(self, cache_dir, fetcher):
        self.dir = os.path.join(cache_dir, "bdtopo", f"v{VERSION}") if cache_dir else None
        self.fetcher = fetcher
        self._lock = threading.Lock()
        self._mem = OrderedDict()           # (z, x, y) -> Records
        self._pending = {}                  # (z, x, y) -> threading.Event of its background download
        self._failed = {}                   # (z, x, y) -> time its download failed
        self._ex = None
        self._slow_until = 0.0
        self.stats = {"cells": 0, "requests": 0, "bytes": 0, "seconds": 0.0, "waits": 0, "missed": 0,
                      "wait_s": 0.0}

    def path(self, key):
        return os.path.join(self.dir, str(key.z), str(key.x), f"{key.y}.npz") if self.dir else None

    def wanted(self, key):
        return ON and covers(_key(key).bounds())

    # ---- what is here ----
    def cached(self, key):
        """The cell's Records in memory or on disk, or None (never the network)."""
        key = _key(key)
        k = (key.z, key.x, key.y)
        with self._lock:
            got = self._mem.get(k)
            if got is not None:
                self._mem.move_to_end(k)
                return got
        p = self.path(key)
        if not p or not os.path.exists(p):
            return None
        try:
            with np.load(p, allow_pickle=False) as z:
                got = Records({f: z[f] for f in FIELDS_NPZ}) if "lon" in z.files else EMPTY
        except Exception as e:          # (cut short, an older layout: fetched again)
            log.debug("bdtopo read %s: %s", p, e)
            return None
        self._keep(k, got)
        return got

    def _keep(self, k, got):
        with self._lock:
            self._mem[k] = got
            self._mem.move_to_end(k)
            while len(self._mem) > MEM_CELLS:
                self._mem.popitem(last=False)

    # ---- the background downloads ----
    def prefetch(self, keys):
        """Start the downloads of these cells' records (French ones not here yet) without waiting; [Event] of theirs."""
        evs = []
        if not ON or self.fetcher is None:
            return evs
        for key in keys or ():
            key = _key(key)
            if not covers(key.bounds()):
                continue
            k = (key.z, key.x, key.y)
            with self._lock:
                if k in self._mem:
                    continue
            p = self.path(key)
            if p and os.path.exists(p) and self.cached(key) is not None:     # (on disk: read here, ~1 ms)
                continue
            with self._lock:
                ev = self._pending.get(k)
                if ev is None:
                    if time.time() - self._failed.get(k, -1e18) < RETRY_S:
                        continue
                    ev = self._pending[k] = threading.Event()
                    if self._ex is None:
                        self._ex = ThreadPoolExecutor(THREADS, thread_name_prefix="bdtopo")
                    self._ex.submit(self._run, key, ev)
            evs.append(ev)
        return evs

    def _run(self, key, ev):
        k = (key.z, key.x, key.y)
        try:
            if self.cached(key) is None:
                self.load(key)
        except Exception as e:
            log.debug("bdtopo %s: %s: %s", key, type(e).__name__, e)
        finally:
            with self._lock:
                self._pending.pop(k, None)
            ev.set()

    def load(self, key, kind="demand"):
        """The cell's Records, downloaded now when not here (one process at a time: an xproc lock), or None when the
        download failed (asked again after RETRY_S). Blocks: the background threads and saved-area downloads only."""
        import xproc
        key = _key(key)
        got = self.cached(key)
        if got is not None:
            return got
        if not covers(key.bounds()):
            return EMPTY
        k = (key.z, key.x, key.y)
        p = self.path(key)
        mine = False
        if p:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            got, mine = xproc.once(p + ".lock", lambda: self.cached(key), LOCK_WAIT_S, kind)
            if got is not None:
                return got
        try:
            t0 = time.monotonic()
            try:
                arrays = parse(self._features(key, kind))
            except Exception as e:      # (offline, IGN down, a bad answer: the predicted buildings meanwhile)
                log.debug("bdtopo fetch %s: %s: %s", key, type(e).__name__, e)
                with self._lock:
                    self._failed[k] = time.time()
                return None
            got = Records(arrays) if len(arrays["lon"]) else EMPTY
            if p:
                import staging
                tmp = staging.tmp_for(p)
                with open(tmp, "wb") as fh:
                    np.savez_compressed(fh, **({f: arrays[f] for f in FIELDS_NPZ} if got.n else {"empty": np.zeros(0)}))
                staging.replace_cached(tmp, p)
            with self._lock:
                self.stats["cells"] += 1
                self.stats["seconds"] += time.monotonic() - t0
                self._failed.pop(k, None)
            self._keep(k, got)
            return got
        finally:
            if mine:
                xproc.release(p + ".lock")

    def _features(self, key, kind):
        import json
        lon0, lat0, lon1, lat1 = key.bounds()
        feats = []
        for page in range(PAGES):
            url = (f"{WFS_URL}?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature&TYPENAMES=BDTOPO_V3:batiment"
                   f"&PROPERTYNAME={FIELDS}&BBOX={lon0:.6f},{lat0:.6f},{lon1:.6f},{lat1:.6f},CRS:84&SRSNAME=CRS:84"
                   f"&OUTPUTFORMAT=application/json&COUNT={PAGE}&STARTINDEX={page * PAGE}&SORTBY=cleabs")
            res = self.fetcher.get(url, kind=kind, lane=LANE, headers={"Accept-Encoding": "gzip"})
            body = res.body
            if body[:2] == b"\x1f\x8b":
                body = gzip.decompress(body)
            with self._lock:
                self.stats["requests"] += 1
                self.stats["bytes"] += len(res.body)
            got = json.loads(body.decode("utf-8")).get("features") or []
            feats += got
            if len(got) < PAGE:
                return feats
        raise IOError(f"bdtopo {key}: more than {PAGES * PAGE} buildings")

    # ---- a tile's wait ----
    def wait(self, keys):
        """Wait for these cells' records together (their downloads started now if not yet), at most WAIT_S (SLOW_WAIT_S
        for SLOW_FOR_S after one did not come). True when all French ones are here."""
        if not ON:
            return True
        evs = self.prefetch(keys)
        if not evs:
            return all(self.cached(k) is not None for k in keys if covers(_key(k).bounds()))
        t0 = time.monotonic()
        budget = SLOW_WAIT_S if t0 < self._slow_until else WAIT_S
        t_end = t0 + budget
        try:
            for ev in evs:
                if not ev.wait(max(0.0, t_end - time.monotonic())):
                    with self._lock:
                        self.stats["missed"] += 1
                    self._slow_until = time.monotonic() + SLOW_FOR_S
                    return False
            return all(self.cached(k) is not None for k in keys if covers(_key(k).bounds()))
        finally:
            with self._lock:
                self.stats["waits"] += 1
                self.stats["wait_s"] += time.monotonic() - t0


def credited_near(cache_dir, lat, lon, near_m):
    """Did BD TOPO records with buildings come to this computer for the cells within near_m of lat, lon (credits: the
    Etalab licence asks for the source)? On-disk records only, never a download."""
    if not ON or not cache_dir:
        return False
    d = os.path.join(cache_dir, "bdtopo", f"v{VERSION}")
    dlat = near_m / 110540.0
    dlon = near_m / (111320.0 * max(0.1, dmath.cos(math.radians(lat))))
    try:
        keys = mapcells.cells_for_bbox(lat - dlat, lon - dlon, lat + dlat, lon + dlon, max_cells=64)
    except ValueError:
        return False
    for k in keys:
        try:
            if os.path.getsize(os.path.join(d, str(k.z), str(k.x), f"{k.y}.npz")) > 400:   # (an empty set: ~250 B)
                return True
        except OSError:
            pass
    return False
