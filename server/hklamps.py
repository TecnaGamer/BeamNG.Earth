"""Hong Kong's street lamps: the Highways Department's Lamp Post Location Data (CSDI Portal, (c) Government of the Hong
Kong SAR), the points the department keeps up, drawn as the mapped street lamps worldprops.py draws from
OpenStreetMap (OpenStreetMap has 8 in 95 test tiles of the city).

- What comes down: per z14 map cell, the layer's points (an ArcGIS FeatureServer, portal.csdi.gov.hk: its host policy
  and gzip in netfetch, 2 000 rows a page, 1 000-1 500 in a city cell) inside the cell's box. A point is a lamp post's
  number (LAMP_NO) and its place; the layer says nothing of the post's type. Kept on disk as one record per point
  (<cache>/hklamps/v1/<z>/<x>/<y>.npz: lon, lat, number; the points in the cell's box only, so a point is in one cell;
  points within SAME_M of an earlier one (by number) are one, made when the cell is read from the department: a cell's
  fact, so every tile agrees). A cell with none is kept as an empty record set; a failed request is not kept (RETRY_S).
- When: as for Hong Kong's buildings (hkbuildings.py, France's bdtopo.py): worldprops.tile_props starts a road tile's
  cells' downloads, then waits for them together (wait(), WAIT_S), and a tile whose records are not here yet is built
  without them and is a revisit fault (not kept: the next visit has them).
- Which points become lamps (choose; the rest are not drawn), in this order:
  * the points within SAME_M of another cell's are one lamp too (points(): the cells a tile reads together, in one
    order, before the tile's box is cut);
  * an OpenStreetMap lamp within OSM_WINS_M of a point wins (the mapped one keeps its mount type and direction);
  * a point on a bridge or in a tunnel, that is within the half width (and FOOT_PAD_M) of a piece the server's own
    reading of the map makes one (roads.bridge_parts / tunnel_parts, roads.bridge_specs' widths): a mapped lamp stands on
    the ground, so one that really stands on a flyover is dropped by furniture.under_decks while its road, with mapped
    lamps along it, would guess none (a flyover's approaches dark), and one in a tunnel would stand on the hill over it.
    The department's points carry no height: one under a flyover goes too (under_decks would drop it where the deck is
    low, and a high bridge's ground road guesses its own lamps there). The road keeps its guessed lamps, as without
    this source;
  * a point with no drivable road within OFFROAD_M (worldprops.LAMP_NEAR_M) is not drawn: the game draws no path there;
  * a point inside the carriageway A of a road on the ground (nearer its centreline than half its width + MARGIN_M, the
    width roads.road_specs gives that road for the build: the same land cover, measured widths and driving side)
    is moved along the perpendicular through its foot q on A's centreline, to the first of:
      1. the near side, where furniture.place puts a guessed lamp on a street with no sidewalk (q + half width A +
         furniture.LAMP_EDGE_M), if that is clear of every other ground carriageway (MARGIN_M) and of the bridge and
         tunnel footprints;
      2. the median: of the offsets half width A + 0.3 .. 1.2 m (MEDIAN_OFFSETS_M) on the near side, the one with the
         most room to the carriageways of all the ground roads (A's too), if that is at least furniture.LAMP_CURB_M
         (the kerb clearance a sidewalk lamp gets) and off the footprints: a lamp in the middle of the median;
      3. the far side, q - (half width A + furniture.LAMP_EDGE_M) of the perpendicular, if clear as in 1 (the model's own
         lamps on a divided road stand on the outer edges);
      4. none: not drawn (BLOCKED).
    A point outside the carriageways stays where the department has it. A post moved by 2 or 3 that ends within
    DOUBLE_M of another kept post (moved or not; the earlier of the posts, in the order of points(), stays) is not
    drawn (DOUBLED): a surveyed post is not doubled by a moved one.
  The rest are mapped lamps at ground height with the ordinary post (worldprops.STREET_LAMP_H), the arm turned to the
  nearest drivable road (worldprops._aim_lamps); a road with MAPPED_ALONG_MIN of them along it guesses none
  (furniture.place), its utility poles stay as they are.

Off: BNE_HK_LAMPS=off. Outside Hong Kong (hklidar.BOUNDS) nothing is asked and nothing changes.
"""
import json
import logging
import math
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import numpy as np

import dmath
import dnp
import hklidar
import mapcells

log = logging.getLogger("earthd.hklamps")

ON = os.environ.get("BNE_HK_LAMPS", "on") != "off"
VERSION = 1                     # the records' layout on disk
URL = "https://portal.csdi.gov.hk/server/rest/services/common/hyd_rcd_1629267205229_84645/FeatureServer/0/query"
FIELDS = "OBJECTID,LAMP_NO"
ID_PREFIX = "hk:hyd:"           # (a lamp's id: the department's number, as "osm:node:" is the map's)
SAME_M = 1.0                    # points this close are one lamp
OSM_WINS_M = 5.0                # a mapped OpenStreetMap lamp this close to a point is the lamp
FOOT_PAD_M = 2.0                # m past the half width of a bridge or tunnel that still count as on it (furniture.DECK_SIDE_M and the deck's
                                # own sidewalks and parapets beyond the width the map's road has)
THREADS = 2                     # background cell downloads at once per process (the host allows 2 at once: roadz's too)
# (BNGE_: not part of the revisit cache's code key; the wait changes when a tile is built, not what)
WAIT_S = float(os.environ.get("BNGE_HK_LAMPS_WAIT", "4"))
SLOW_WAIT_S = 0.5               # a tile's wait for a while after one did not come in WAIT_S (a slow line, host busy)
SLOW_FOR_S = 60.0
RETRY_S = 600.0                 # a cell whose download failed is asked again after this
LOCK_WAIT_S = 90.0              # another process downloading the same cell: waited for this long at most
MEM_CELLS = 128
MARGIN_M = 0.25                 # m past a carriageway's half width a post is still taken for standing in it
CELL_M = 96.0                   # m: the squares the points and the road segments are tested in
MEM_TILES = 24                  # tiles' points kept in memory (neighbours read the same cells)
FIELDS_NPZ = ("lon", "lat", "no")
# choose's reasons a point is not drawn
KEPT, OSM, BRIDGE, TUNNEL, OFFROAD, BLOCKED, DOUBLED = range(7)
# how a post in a carriageway was moved (settle's "moved": 0 not): the near side, the median, the far side
NEAR_SIDE, MEDIAN, FAR_SIDE = 1, 2, 3
MEDIAN_OFFSETS_M = (0.3, 0.45, 0.6, 0.75, 0.9, 1.05, 1.2)      # m past a carriageway's edge a median lamp is tried at
DOUBLE_M = 6.0                  # a post moved to the median or the far side this close to another kept post is dropped
# what a piece of road is
K_GROUND, K_BRIDGE, K_TUNNEL = range(3)


def covers(bounds):
    """Does a cell (lon0, lat0, lon1, lat1) overlap Hong Kong (hklidar.BOUNDS)? Pure, no network."""
    lon0, lat0, lon1, lat1 = (float(v) for v in bounds)
    s, w, n, e = hklidar.BOUNDS
    return lon0 < e and lon1 > w and lat0 < n and lat1 > s


def _key(key):
    return mapcells.CellKey(int(key[0]), int(key[1]), int(key[2]))


def metres(lon, lat, lat0):
    """(n, 2) metres east and north of the points (lon, lat) on the plane of a cell at latitude lat0: for distances of
    a few metres (SAME_M). Pure."""
    return np.column_stack([np.asarray(lon, np.float64) * (111320.0 * dmath.cos(math.radians(lat0))),
                            np.asarray(lat, np.float64) * 110540.0])


def parse(features, bounds):
    """The records (dict of arrays, ordered by number, then place) of the layer's ArcGIS JSON features inside bounds
    (lon0, lat0, lon1, lat1; the east and north edges are the next cell's), the points within SAME_M of an earlier one
    left out. A point without a number is "o<OBJECTID>". Pure."""
    lon0, lat0, lon1, lat1 = (float(v) for v in bounds)
    lon, lat, no = [], [], []
    for f in features or ():
        if not isinstance(f, dict):
            continue
        g = f.get("geometry") or {}
        a = f.get("attributes") or {}
        try:
            x, y = float(g["x"]), float(g["y"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (math.isfinite(x) and math.isfinite(y) and lon0 <= x < lon1 and lat0 <= y < lat1):
            continue
        n = str(a.get("LAMP_NO") or "").strip()
        lon.append(x)
        lat.append(y)
        no.append(n or f"o{a.get('OBJECTID')}")
    lon, lat, no = np.asarray(lon, np.float64), np.asarray(lat, np.float64), np.asarray(no, "U16")
    order = np.lexsort((lat, lon, no)) if len(no) else np.zeros(0, np.int64)
    lon, lat, no = lon[order], lat[order], no[order]
    keep = one_each(metres(lon, lat, 0.5 * (lat0 + lat1)))
    return {"lon": lon[keep], "lat": lat[keep], "no": no[keep]}


class Records:
    """A cell's records (FIELDS_NPZ arrays)."""

    def __init__(self, arrays):
        self.lon = np.ascontiguousarray(arrays["lon"], np.float64)
        self.lat = np.ascontiguousarray(arrays["lat"], np.float64)
        self.no = np.asarray(arrays["no"], "U16")
        self.n = len(self.lon)


EMPTY = Records({"lon": np.zeros(0), "lat": np.zeros(0), "no": np.zeros(0, "U16")})


class Source:
    """The cells' records: fetched in the background, kept on disk and in memory (see the module docstring)."""

    def __init__(self, cache_dir, fetcher):
        self.dir = os.path.join(cache_dir, "hklamps", f"v{VERSION}") if cache_dir else None
        self.fetcher = fetcher
        self._lock = threading.Lock()
        self._mem = OrderedDict()           # (z, x, y) -> Records
        self._joined = OrderedDict()        # the cells a tile reads, together -> (lon, lat, number, {frame: (n, 2) metres})
        self._pending = {}                  # (z, x, y) -> threading.Event of its background download
        self._failed = {}                   # (z, x, y) -> time its download failed
        self._ex = None
        self._slow_until = 0.0
        self.stats = {"cells": 0, "requests": 0, "bytes": 0, "seconds": 0.0, "waits": 0, "missed": 0, "wait_s": 0.0}

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
            log.debug("hklamps read %s: %s", p, e)
            return None
        self._keep(k, got)
        return got

    def _keep(self, k, got):
        with self._lock:
            self._mem[k] = got
            self._mem.move_to_end(k)
            while len(self._mem) > MEM_CELLS:
                self._mem.popitem(last=False)

    def joined(self, ident):
        """points()'s arrays of these cells (sorted (z, x, y)) as kept, or None."""
        with self._lock:
            got = self._joined.get(ident)
            if got is not None:
                self._joined.move_to_end(ident)
            return None if got is None else got[:3]

    def keep_joined(self, ident, arrays):
        with self._lock:
            self._joined[ident] = tuple(arrays) + ({},)
            while len(self._joined) > MEM_TILES:
                self._joined.popitem(last=False)
        return arrays

    def projected(self, ident, frame, lon, lat):
        """The points' (n, 2) east and north in frame (the arrays points() gave for these cells), made once per frame."""
        with self._lock:
            got = self._joined.get(ident)
            per = got[3] if got is not None else None
            key = (frame.lat0, frame.lon0, frame.h0)
            if per is not None and key in per:
                return per[key]
        x, y, _ = frame.geodetic_to_enu(lat, lon, np.zeros(len(lat)))
        xy = np.column_stack([np.asarray(x, np.float64), np.asarray(y, np.float64)])
        if per is not None:
            with self._lock:
                if len(per) >= 2:
                    per.pop(next(iter(per)), None)
                per[key] = xy
        return xy

    # ---- the background downloads ----
    def prefetch(self, keys):
        """Start the downloads of these cells' records (Hong Kong ones not here yet) without waiting; [Event] of
        theirs."""
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
                        self._ex = ThreadPoolExecutor(THREADS, thread_name_prefix="hklamps")
                    self._ex.submit(self._run, key, ev)
            evs.append(ev)
        return evs

    def _run(self, key, ev):
        k = (key.z, key.x, key.y)
        try:
            if self.cached(key) is None:
                self.load(key)
        except Exception as e:
            log.debug("hklamps %s: %s: %s", key, type(e).__name__, e)
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
                arrays = parse(self._features(key, kind), key.bounds())
            except Exception as e:      # (offline, the host down, a bad answer: the guessed lamps meanwhile)
                log.debug("hklamps fetch %s: %s: %s", key, type(e).__name__, e)
                with self._lock:
                    self._failed[k] = time.time()
                return None
            got = Records(arrays) if len(arrays["lon"]) else EMPTY
            if p:
                import staging
                tmp = staging.tmp_for(p)
                with open(tmp, "wb") as fh:
                    np.savez_compressed(fh, **(arrays if got.n else {"empty": np.zeros(0)}))
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
        lon0, lat0, lon1, lat1 = key.bounds()

        def get_json(url):
            body = self.fetcher.get(url, kind=kind).body
            with self._lock:
                self.stats["requests"] += 1
                self.stats["bytes"] += len(body)
            return json.loads(body.decode("utf-8"))
        return hklidar.arcgis(get_json, URL, {
            "where": "1=1", "geometry": f"{lon0:.7f},{lat0:.7f},{lon1:.7f},{lat1:.7f}",
            "geometryType": "esriGeometryEnvelope", "inSR": 4326, "spatialRel": "esriSpatialRelIntersects",
            "outSR": 4326, "geometryPrecision": 7, "outFields": FIELDS, "orderByFields": "OBJECTID"})

    # ---- a tile's wait ----
    def wait(self, keys):
        """Wait for these cells' records together (their downloads started now if not yet), at most WAIT_S (SLOW_WAIT_S
        for SLOW_FOR_S after one did not come). True when all Hong Kong ones are here."""
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
    """Did lamp records with lamps come to this computer for the cells within near_m of lat, lon (credits: the
    Government's terms ask for the source)? On-disk records only, never a download."""
    if not ON or not cache_dir:
        return False
    d = os.path.join(cache_dir, "hklamps", f"v{VERSION}")
    dlat = near_m / 110540.0
    dlon = near_m / (111320.0 * max(0.1, dmath.cos(math.radians(lat))))
    try:
        keys = mapcells.cells_for_bbox(lat - dlat, lon - dlon, lat + dlat, lon + dlon, max_cells=64)
    except ValueError:
        return False
    for k in keys:
        try:
            if os.path.getsize(os.path.join(d, str(k.z), str(k.x), f"{k.y}.npz")) > 400:   # (an empty set: ~200 B)
                return True
        except OSError:
            pass
    return False


# ---- the points of a tile ----

def prefetch(md, keys):
    """Start the downloads of the tile's cells' records, asking for nothing outside Hong Kong (or with the source off)."""
    src = getattr(md, "hklamps", None)
    if src is not None and ON:
        src.prefetch(keys)


def points(md, keys):
    """(lon, lat, number) arrays of the points of these cells (Hong Kong's), the lamps within SAME_M of an earlier one
    (by number, then place) left out, waiting for their downloads together and bounded (Source.wait); None when there is
    nothing to ask (no source, no Hong Kong cell) or they are not all here (a revisit fault: the tile is built without
    them and not kept). Kept per set of cells: the tiles beside each other read the same ones."""
    src = getattr(md, "hklamps", None)
    ks = [k for k in keys if src is not None and ON and covers(_key(k).bounds())]
    if not ks:
        return None
    if not src.wait(ks):
        import revisit
        revisit.fault()
        return None
    ident = tuple(sorted((k[0], k[1], k[2]) for k in ks))
    got = src.joined(ident)
    if got is not None:
        return got[:3]
    recs = [src.cached(k) for k in ks]
    if any(r is None for r in recs):
        import revisit
        revisit.fault()
        return None
    recs = [r for r in recs if r.n]
    if not recs:
        return src.keep_joined(ident, (np.zeros(0), np.zeros(0), np.zeros(0, "U16")))[:3]
    lon, lat, no = (np.concatenate([r.lon for r in recs]), np.concatenate([r.lat for r in recs]),
                    np.concatenate([r.no for r in recs]))
    order = np.lexsort((lat, lon, no))                  # (the same order whichever cells they came in)
    lon, lat, no = lon[order], lat[order], no[order]
    keep = one_each(metres(lon, lat, float(np.mean(lat))))
    return src.keep_joined(ident, (lon[keep], lat[keep], no[keep]))[:3]


def _close_pairs(xy, m):
    """[(i, j)] with i < j and |xy[i] - xy[j]| < m, in order of i, then j. Pure."""
    n = len(xy)
    if n < 2:
        return []
    order = np.argsort(xy[:, 0], kind="stable")
    x = xy[order, 0]
    found = []
    step = 1
    while step < n:
        near = np.flatnonzero(x[step:] - x[:-step] < m)     # (sorted: no farther pair of this step is any nearer)
        if not len(near):
            break
        i, j = order[near], order[near + step]
        ok = np.sum(dnp.sq(xy[i] - xy[j]), axis=1) < m * m
        found.extend(zip(np.minimum(i, j)[ok].tolist(), np.maximum(i, j)[ok].tolist()))
        step += 1
    found.sort()
    return found


def _pairs_within(a, b, m):
    """[(i, j)] with |a[i] - b[j]| < m, a (n, 2) and b (k, 2), in order of i, then j. Pure."""
    if not len(a) or not len(b):
        return []
    order = np.argsort(b[:, 0], kind="stable")
    bx = b[order, 0]
    lo, hi = np.searchsorted(bx, a[:, 0] - m, "left"), np.searchsorted(bx, a[:, 0] + m, "right")
    out = []
    for i in np.flatnonzero(hi > lo).tolist():
        j = order[lo[i]:hi[i]]
        j = j[np.sum(dnp.sq(b[j] - a[i]), axis=1) < m * m]
        out.extend((i, int(k)) for k in np.sort(j).tolist())
    return out


def one_each(xy, same_m=SAME_M):
    """(n,) bool of the points that stay when those within same_m of an earlier one (in the given order) are one lamp:
    the first of a group stays, whatever was dropped near it. Pure."""
    keep = np.ones(len(xy), bool)
    for i, j in _close_pairs(np.asarray(xy, np.float64).reshape(-1, 2), same_m):
        if keep[i]:
            keep[j] = False
    return keep


# ---- the roads near a tile ----

class Net:
    """The pieces of road near a tile as segments: their ends a, b (m, 2), the half width and the kind (K_GROUND,
    K_BRIDGE, K_TUNNEL) of the piece each is of, and in a stable order. known False: no roads were read (nothing is
    judged by them)."""

    def __init__(self, pieces, known=True):
        """pieces: [(a (k, 2), b (k, 2), half width, kind)]."""
        self.known = known
        n = [len(p[0]) for p in pieces]
        self.a = np.ascontiguousarray(np.concatenate([p[0] for p in pieces]) if pieces else np.zeros((0, 2)), np.float64)
        self.b = np.ascontiguousarray(np.concatenate([p[1] for p in pieces]) if pieces else np.zeros((0, 2)), np.float64)
        self.half = np.repeat(np.array([p[2] for p in pieces], np.float64), n)
        self.kind = np.repeat(np.array([p[3] for p in pieces], np.int64), n)


NO_NET = Net([], known=False)


def _piece(feats, frame, ring, half, kind):
    import mapgeom
    ring = np.asarray(ring, np.float64)
    if len(ring) < 2:
        return None
    e, n = mapgeom.enu_xy(feats, frame, ring)
    xy = np.column_stack([np.asarray(e, np.float64), np.asarray(n, np.float64)])
    box = (xy[:, 0].min(), xy[:, 1].min(), xy[:, 0].max(), xy[:, 1].max())
    return xy[:-1], xy[1:], float(half), kind, box


def _feature_pieces(feats, frame, i, spec, bridge, tunnel):
    """[(a, b, half width, kind, box)] of feature i: the on-ground parts of the road at the width of its spec (the
    carriageway build_tile draws), the bridge and tunnel pieces at their specs' (roads.ground_parts / bridge_parts /
    tunnel_parts: the map's flags and levels joined with the lidar's elevated stretches). Pure."""
    import roads
    out = []
    for kind, sp, parts in ((K_GROUND, spec, roads.ground_parts), (K_BRIDGE, bridge, roads.bridge_parts),
                            (K_TUNNEL, tunnel, roads.tunnel_parts)):
        if sp is None:
            continue
        for ring in parts(feats, i):
            got = _piece(feats, frame, ring, float(sp.width) / 2, kind)
            if got is not None:
                out.append(got)
    return out


def road_net(frame, layers, near, spec_args=None):
    """The Net of the roads of layers [(feats, rows)] (worldprops' layers of the roads round the tile) whose boxes meet near
    (x0, y0, x1, y1): the pieces of each of its features in a stable order (feature id, kind, ends). The ground carriageway
    is as wide as roads.road_specs makes it for the build, spec_args = (urban_fn, widths, left_hand) as build_tile's;
    None: the land cover and measured widths left out. Pure given the layers."""
    import roads
    import worldprops as wp
    if not layers:
        return NO_NET
    urban_fn, widths, left_hand = spec_args if spec_args is not None else (None, None, False)
    found = []
    for feats, rows in layers:
        if len(feats.kind) == 0:
            continue
        specs = roads.road_specs(feats, urban_fn, widths, left_hand)
        bridges, tunnels = roads.bridge_specs(feats), roads.tunnel_specs(feats)
        boxes = feats.boxes(frame)
        pick = wp._picked(feats, rows, np.arange(len(feats.kind)))
        meet = (boxes[:, 2] >= near[0]) & (boxes[:, 0] <= near[2]) & (boxes[:, 3] >= near[1]) & (boxes[:, 1] <= near[3])
        if pick is not None:
            meet &= pick
        memo = wp._memo(feats, "hk_pieces", frame, dict)        # (feature -> its pieces, made as a tile asks)
        for i in np.flatnonzero(meet).tolist():
            if specs[i] is None and bridges[i] is None and tunnels[i] is None:
                continue
            have = memo.get(i)
            if have is None or have[0] != (specs[i], bridges[i], tunnels[i]):
                have = memo[i] = ((specs[i], bridges[i], tunnels[i]),
                                  _feature_pieces(feats, frame, i, specs[i], bridges[i], tunnels[i]))
            fid = str(feats.ids[i])
            found.extend((fid, p) for p in have[1] if not (p[4][2] < near[0] or p[4][0] > near[2] or p[4][3] < near[1]
                                                          or p[4][1] > near[3]))
    found.sort(key=lambda r: (r[0], r[1][3], tuple(r[1][0][0]), tuple(r[1][1][-1])))
    return Net([(p[0], p[1], p[2], p[3]) for _, p in found])


def _blocks(p, a, b, reach):
    """[(point indices, segment indices)]: the points in CELL_M squares with the segments that come within reach of
    the square. Pure."""
    cell = CELL_M
    gx, gy = np.floor(p[:, 0] / cell).astype(np.int64), np.floor(p[:, 1] / cell).astype(np.int64)
    key = gx * 1000003 + gy
    order = np.argsort(key, kind="stable")
    ks = key[order]
    starts = np.flatnonzero(np.concatenate([[True], ks[1:] != ks[:-1]]))
    ends = np.concatenate([starts[1:], [len(ks)]])
    lo, hi = np.minimum(a, b) - reach, np.maximum(a, b) + reach
    out = []
    for s, e in zip(starts.tolist(), ends.tolist()):
        pi = order[s:e]
        x0, y0 = gx[pi[0]] * cell, gy[pi[0]] * cell
        seg = np.flatnonzero((hi[:, 0] >= x0) & (lo[:, 0] <= x0 + cell) & (hi[:, 1] >= y0) & (lo[:, 1] <= y0 + cell))
        out.append((pi, seg))
    return out


def probe(p, net, reach):
    """Where the points p (n, 2) stand to the roads of net: (slack (n,): the distance to the nearest ground road's
    centreline less its half width, inf where none is within reach; at (n,): that segment's index in net, -1 none; q
    (n, 2): the nearest point of it; drive (n,): the distance to the nearest centreline of a road on the ground or a
    bridge, inf past reach; bridge, tunnel (n,) bool: within the half width + FOOT_PAD_M of a piece of that kind).
    Pure."""
    n = len(p)
    slack, drive = np.full(n, np.inf), np.full(n, np.inf)
    at, q = np.full(n, -1, np.int64), np.zeros((n, 2))
    bridge, tunnel = np.zeros(n, bool), np.zeros(n, bool)
    if not n or not len(net.half):
        return slack, at, q, drive, bridge, tunnel
    ground = net.kind == K_GROUND
    for pi, seg in _blocks(p, net.a, net.b, reach + FOOT_PAD_M):
        if not len(seg):
            continue
        a, b, half, kind = net.a[seg], net.b[seg], net.half[seg], net.kind[seg]
        ab = b - a
        l2 = np.maximum(np.sum(ab * ab, axis=1), 1e-9)
        P = p[pi]
        t = np.clip(np.sum((P[:, None, :] - a[None]) * ab[None], axis=2) / l2[None], 0.0, 1.0)
        foot = a[None] + t[..., None] * ab[None]
        gap = P[:, None, :] - foot
        d = dnp.hypot(gap[..., 0], gap[..., 1])
        g = ground[seg]
        sl = np.where(g[None], d - half[None], np.inf)
        j = np.argmin(sl, axis=1)
        rows = np.arange(len(pi))
        slack[pi] = sl[rows, j]
        at[pi] = np.where(np.isfinite(sl[rows, j]), seg[j], -1)
        q[pi] = foot[rows, j]
        drive[pi] = np.where((kind != K_TUNNEL)[None], d, np.inf).min(axis=1)
        within = d <= half[None] + FOOT_PAD_M
        bridge[pi] = (within & (kind == K_BRIDGE)[None]).any(axis=1)
        tunnel[pi] = (within & (kind == K_TUNNEL)[None]).any(axis=1)
    return slack, at, q, drive, bridge, tunnel


def _room(p, net, near_m, margin):
    """(slack (n,), clear (n,) bool) of the spots p: the room to the nearest ground carriageway, and whether the spot is off
    the bridge and tunnel footprints and has `margin` of it. Pure."""
    slack, _at, _q, _drive, br, tu = probe(p, net, near_m)
    return slack, ~(br | tu) & (slack >= margin)


def settle(xy, net, near_m, margin=MARGIN_M):
    """(why (n,) int, xy (n, 2), moved (n,) int): for the points xy (n, 2) (already past the lamp and OpenStreetMap
    rules) the reason each is not drawn (BRIDGE, TUNNEL, OFFROAD, BLOCKED, DOUBLED), KEPT 0 when it is, where it stands (a
    point in a ground road's carriageway moved by the first of the module's four ways that has room, see the docstring;
    the others where they are), and how each was moved (0 not, NEAR_SIDE, MEDIAN, FAR_SIDE; a BLOCKED one shows the near
    side, a DOUBLED one the spot it would have had). A net of unread roads judges nothing. Pure, and the order of the
    points matters only to DOUBLED."""
    import furniture
    n = len(xy)
    why, out, moved = np.zeros(n, np.int64), np.array(xy, np.float64).reshape(-1, 2), np.zeros(n, np.int8)
    if not n or not net.known:
        return why, out, moved
    slack, at, q, drive, bridge, tunnel = probe(out, net, near_m)
    why[bridge] = BRIDGE
    why[tunnel] = TUNNEL                                # (the tunnel's before the bridge's)
    why[(why == KEPT) & ~(drive <= near_m)] = OFFROAD
    inside = np.flatnonzero((why == KEPT) & (slack < margin))
    if len(inside):
        a, b = net.a[at[inside]], net.b[at[inside]]
        away = out[inside] - q[inside]
        gap = dnp.hypot(away[:, 0], away[:, 1])
        ab = b - a
        along = dnp.hypot(ab[:, 0], ab[:, 1])
        left = np.column_stack([-ab[:, 1], ab[:, 0]]) / np.maximum(along, 1e-9)[:, None]
        unit = np.where((gap > 1e-6)[:, None], away / np.maximum(gap, 1e-9)[:, None], left)
        foot, half = q[inside], net.half[at[inside]]
        near_side = foot + unit * (half + furniture.LAMP_EDGE_M)[:, None]
        step = np.zeros(len(inside), np.int8)
        spot = near_side.copy()
        ok = _room(near_side, net, near_m, margin)[1]                       # 1: the near side
        step[ok] = NEAR_SIDE
        todo = np.flatnonzero(~ok)
        if len(todo):                                                       # 2: the median
            best, where = np.full(len(todo), -np.inf), np.zeros((len(todo), 2))
            for s in MEDIAN_OFFSETS_M:
                c = foot[todo] + unit[todo] * (half[todo] + s)[:, None]
                room, free = _room(c, net, near_m, -np.inf)       # (free: off the footprints)
                better = free & (room > best)
                best, where = np.where(better, room, best), np.where(better[:, None], c, where)
            hit = best >= furniture.LAMP_CURB_M
            step[todo[hit]] = MEDIAN
            spot[todo[hit]] = where[hit]
            todo = todo[~hit]
        if len(todo):                                                       # 3: the far side
            far = foot[todo] - unit[todo] * (half[todo] + furniture.LAMP_EDGE_M)[:, None]
            hit = _room(far, net, near_m, margin)[1]
            step[todo[hit]] = FAR_SIDE
            spot[todo[hit]] = far[hit]
            todo = todo[~hit]
        step[todo] = NEAR_SIDE                                              # 4: none
        why[inside[todo]] = BLOCKED
        out[inside], moved[inside] = spot, step
        late = np.flatnonzero(step >= MEDIAN)                               # the 6 m rule
        if len(late):
            others = np.flatnonzero((why == KEPT) & ~np.isin(np.arange(n), inside[late]))
            have = out[others]
            for k in late.tolist():
                i = inside[k]
                if len(have):
                    d = dnp.hypot(have[:, 0] - out[i, 0], have[:, 1] - out[i, 1])
                    if (d < DOUBLE_M).any():
                        why[i] = DOUBLED
                        continue
                have = np.vstack([have, out[i][None]])
    return why, out, moved


def choose(xy, osm_xy, net, near_m=40.0):
    """(why (n,) int, xy (n, 2), moved (n,) int: settle's) of the points xy (n, 2) (past points()'s one-each): OSM where a mapped
    OpenStreetMap lamp (osm_xy (m, 2)) is within OSM_WINS_M, else settle's. Pure."""
    why = np.zeros(len(xy), np.int64)
    out, moved = np.array(xy, np.float64).reshape(-1, 2), np.zeros(len(xy), np.int8)
    for i, _j in _pairs_within(out, np.asarray(osm_xy, np.float64).reshape(-1, 2), OSM_WINS_M):
        why[i] = OSM
    live = np.flatnonzero(why == KEPT)
    w, o, m = settle(out[live], net, near_m)
    why[live], out[live], moved[live] = w, o, m
    return why, out, moved


def add(placed, md, frame, keys, box, layers_fn, spec_args=None):
    """Add the Highways Department's lamps in box (x0, y0, x1, y1 in frame) to placed (worldprops._street_lamps'
    records, the OpenStreetMap ones already in it) as mapped lamps: the cells keys' points, the rules of choose, the
    roads from layers_fn() (worldprops' layers of the roads round the tile) at the widths roads.road_specs gives for
    spec_args = (urban_fn, widths, left_hand). Returns (xy (n, 2), why, xy placed (n, 2), moved (n,)) of the points
    in box (lab and tests), None when nothing was asked."""
    pts = points(md, keys)
    if pts is None:
        return None
    lon, lat, no = pts
    if not len(lon):
        return (np.zeros((0, 2)), np.zeros(0, np.int64), np.zeros((0, 2)), np.zeros(0, bool))
    import worldprops as wp
    src = md.hklamps
    xy = src.projected(tuple(sorted((k[0], k[1], k[2]) for k in keys if covers(_key(k).bounds()))), frame, lon, lat)
    inside = (xy[:, 0] >= box[0]) & (xy[:, 0] < box[2]) & (xy[:, 1] >= box[1]) & (xy[:, 1] < box[3])
    if not inside.any():
        return (np.zeros((0, 2)), np.zeros(0, np.int64), np.zeros((0, 2)), np.zeros(0, bool))
    lon, lat, no, xy = lon[inside], lat[inside], no[inside], xy[inside]
    osm = [(p["x"], p["y"]) for p in placed if "lamp_id" in p]
    reach = wp.LAMP_NEAR_M
    near = (xy[:, 0].min() - reach, xy[:, 1].min() - reach, xy[:, 0].max() + reach, xy[:, 1].max() + reach)
    why, fin, moved = choose(xy, osm, road_net(frame, layers_fn(), near, spec_args), reach)
    if moved.any():
        for i in np.flatnonzero(moved).tolist():
            la, lo, _ = frame.enu_to_geodetic(np.array([fin[i, 0]]), np.array([fin[i, 1]]), np.zeros(1))
            lat[i], lon[i] = float(la[0]), float(lo[0])
    for i in np.flatnonzero(why == KEPT).tolist():
        placed.append({"group": "streetlight", "x": float(fin[i, 0]), "y": float(fin[i, 1]), "lat": float(lat[i]),
                       "lon": float(lon[i]), "lift": 0., "h": wp.STREET_LAMP_H, "yaw": 0., "rank": 0.,
                       "lamp_id": ID_PREFIX + str(no[i]), "arm": 0, "layer": math.nan})
    return xy, why, fin, moved
