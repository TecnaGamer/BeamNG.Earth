"""Local weather S1: snow by elevation and aspect, with observed snow cover (BNE_LOCAL_WEATHER=snowfield, on the
Experimental page; ~/bnge-tests/localweather/PLAN_FINAL.md S-1 to S-9). Stage A: the test harness that feeds the
S1 band tables from Open-Meteo per cell round the anchor; the shipped source is the planet pack (S1b).

The rule (the user, 2026-11-27): snow on the ground only where real data says it snowed or lies. Every table here
comes from a cell's own forcing (the snow kernel over the past 10 days, the model's snow depth as the seed) and the
satellites' observed cover of that cell (NOAA IMS 1 km); never from the season, the calendar or the climate.

Glue only: the kernels are native/earthfast/src/snowfield.rs (the tables) and snowobs.rs (the IMS reader, the ground
under its pixels, the band counts). This module fetches (netfetch), caches, schedules and hands snowcover a Field of
S1 tables by 0.25-degree cell (the pack's cells, so S1b slots in).

  cells        the 0.25-degree cells within NEAR_KM of the anchor, each with its own forcing; beyond, to FAR_KM, the
               cells of each 1-degree block share the block centre's forcing (the observations stay per cell)
  forcing      one Open-Meteo request per ring (many locations), the server's variables and window (weather.py
               SNOW_HOURLY, 240 + 6 h); near ring hourly, far ring every 3 h; at most WEIGHT_CAP weighted calls a day
               (Open-Meteo's weight: 1.2 a location for 12 variables)
  observations the newest two IMS 1 km days (about a day late), the box round the rings read in one pass
  ground       each cell's IMS pixels' mean height and aspect class from our DEM (zoom GROUND_ZOOM), and the cell's
               lowest / highest ground: static, kept on disk by cell and kernel version
  tables       snowfield_table per cell; the bins settle over two evaluations (S-7); a cell's table older than
               TABLE_TTL_S is dropped (S-6: yesterday's snow at the same place may show while today's loads, never
               another place's: a table is its own cell's)

Off (the part not named): nothing here is imported by the server, nothing fetched, keyed or drawn.
Never in plans, cell facts or the decision archives (revisit.NOT_SWITCHES): the snow photos are keyed by their tables.
"""
import datetime
import logging
import math
import os
import threading
import time
import urllib.parse

import numpy as np

import dmath
import fast

log = logging.getLogger("earthd.snowfield")

SWITCH = "BNE_LOCAL_WEATHER"
PARTS = frozenset(p.strip() for p in os.environ.get("BNE_LOCAL_WEATHER", "").lower().replace(";", ",").split(",")
                  if p.strip())
ON = "snowfield" in PARTS
NATIVE = 655

CELL_DEG = 0.25
BLOCK_DEG = 1.0
NEAR_KM = 90.0
FAR_KM = 450.0
REFRESH_NEAR_S = 3600.0
REFRESH_FAR_S = 6 * 3600.0
# The near ring's priority (Codex on a656188c: Tromso's 96 near cells hourly were 2,765 a day, the anchor's own cell held
# till midnight after ~17 h): its CORE_CELLS nearest (the anchor's own first) every hour, their weight for the rest of
# the UTC day reserved before anything else; the rest of the near ring every REFRESH_REST_S as the budget allows, then
# the far ring every REFRESH_FAR_S, each only with the core's reserve kept
CORE_CELLS = 16
REFRESH_REST_S = 3 * 3600.0
SLOT_S = {"near": REFRESH_NEAR_S, "core": REFRESH_NEAR_S, "rest": REFRESH_REST_S, "far": REFRESH_FAR_S}
WEIGHT_CAP = 2000.0             # Open-Meteo weighted calls a day (PLAN_FINAL D-3: Experimental parts)
LOCATIONS_MAX = 200             # locations a request (Open-Meteo allows 1,000; a near ring holds ~40)
TABLE_TTL_S = 24 * 3600.0
GROUND_ZOOM = 9
GROUND_RES = 0.002              # deg a DEM sample under the IMS pixels (~220 m: ~20 a 1 km pixel)
FAR_GROUND_ZOOM = 8             # ... the far ring's cells (~1,000 of them: a quarter of the tiles, ~5 samples a pixel)
FAR_GROUND_RES = 0.004
RETRY_S = (15.0, 300.0)         # a failed ring's next try: from 15 s doubling to 5 min
GROUND_VERSION = 1
IMS_URL = "https://noaadata.apps.nsidc.org/NOAA/G02156/GIS/1km/{y}/ims{y}{doy:03d}_1km_GIS_v1.3.tif.gz"
IMS_DAYS_BACK = 4               # the newest file is looked for this many days back
IMS_KEEP = 3                    # day files kept on disk
# The clear-satellite rule (snowfield.rs Fusion.clear_gap): the near ring's cells also read the latest clear day of
# VIIRS NOAA-20 / MODIS Terra NDSI snow cover (NASA GIBS, EPSG:4326 500m level 7 tiles) of the CLEAR_DAYS before today
# (VIIRS first: its 375 m pixels), a day counting where it saw at least CLEAR_SEEN of the cell's land clear
GIBS_URL = "https://gibs.earthdata.nasa.gov/wmts/epsg4326/best/{layer}/default/{date}/500m/7/{row}/{col}.png"
GIBS_CMAP_URL = "https://gibs.earthdata.nasa.gov/colormaps/v1.3/MODIS_NDSI_Snow_Cover.xml"
CLEAR_LAYERS = ("VIIRS_NOAA20_NDSI_Snow_Cover", "MODIS_Terra_NDSI_Snow_Cover")
CLEAR_DAYS = 3
CLEAR_SEEN = 0.7
CLEAR_NDSI = 35                 # NDSI x 100 from which a clear pixel is snow (snowobs.rs NDSI_SNOW; P-A's sweep)
GIBS_DEG = 2.25
GIBS_PX = 512
GIBS_KEEP_DAYS = 5              # tile days kept on disk


def cell_of(lat, lon):
    """The 0.25-degree cell (ci, cj) of a point (snowcover.Field's indices)."""
    return int(math.floor((lon + 180.0) / CELL_DEG)), int(math.floor((90.0 - lat) / CELL_DEG))


def cell_box(ci, cj):
    """(south, west, north, east) of a cell."""
    west, north = ci * CELL_DEG - 180.0, 90.0 - cj * CELL_DEG
    return north - CELL_DEG, west, north, west + CELL_DEG


def _km(lat0, lon0, lat1, lon1):
    k = dmath.cos(math.radians(0.5 * (lat0 + lat1)))
    return math.hypot((lat1 - lat0) * 111.2, (((lon1 - lon0) + 180.0) % 360.0 - 180.0) * 111.2 * k)


def rings(lat, lon, near_km=NEAR_KM, far_km=FAR_KM):
    """(near cells, far cells by their 1-degree block centre {block centre (lat, lon): [cells]}) round a point, each
    list nearest first (the anchor's own cell first: S-6's urgent table)."""
    near, far = [], {}
    span_lat = far_km / 111.2
    span_lon = far_km / (111.2 * max(0.05, dmath.cos(math.radians(min(abs(lat), 89.0)))))
    ci0, cj0 = cell_of(lat + span_lat, lon - span_lon)
    ci1, cj1 = cell_of(lat - span_lat, lon + span_lon)
    for cj in range(max(cj0, 0), min(cj1, int(180 / CELL_DEG) - 1) + 1):
        for ci in range(ci0, ci1 + 1):
            ci_w = ci % int(360 / CELL_DEG)
            s, w, n, e = cell_box(ci_w, cj)
            d = _km(lat, lon, 0.5 * (s + n), 0.5 * (w + e))
            if d <= near_km:
                near.append((d, (ci_w, cj)))
            elif d <= far_km:
                bc = (math.floor((s + n) / 2 / BLOCK_DEG) * BLOCK_DEG + BLOCK_DEG / 2,
                      (math.floor(((w + e) / 2 + 180.0) / BLOCK_DEG) * BLOCK_DEG - 180.0) + BLOCK_DEG / 2)
                far.setdefault(bc, []).append((d, (ci_w, cj)))
    near.sort()
    blocks = {k: [c for _d, c in sorted(v)] for k, v in sorted(far.items(), key=lambda kv: min(kv[1])[0])}
    return [c for _d, c in near], blocks


def om_weight(n_locations, n_vars=12, hours=246):
    """Open-Meteo's weight of a request (its source: max(1, v/10, v/10 x days/14) a location, summed)."""
    v = float(n_vars)
    return n_locations * max(1.0, v / 10.0, v / 10.0 * (hours / 24.0) / 14.0)


def hourly_url_many(points, past_hours=240, forecast_hours=6):
    """The snow kernel's request (weather.hourly_url's) for many locations at once."""
    import weather
    q = {"latitude": ",".join(f"{p[0]:.4f}" for p in points), "longitude": ",".join(f"{p[1]:.4f}" for p in points),
         "hourly": ",".join(weather.SNOW_HOURLY), "past_hours": int(past_hours), "forecast_hours": int(forecast_hours),
         "wind_speed_unit": "ms", "timeformat": "unixtime", "timezone": "auto", "cell_selection": "land"}
    return weather.API + "?" + urllib.parse.urlencode(q, safe=",")


def ims_url(day):
    return IMS_URL.format(y=day.year, doy=day.timetuple().tm_yday)


class _Deferred(Exception):
    """A ring held back by the day's Open-Meteo weight."""


class _Cell:
    __slots__ = ("key", "point", "ground", "table", "made", "states", "z_cell", "rows", "far", "gground")

    def __init__(self, key, point):
        self.key, self.point = key, point
        self.far = False            # (a far-ring cell: its ground from FAR_GROUND_ZOOM)
        self.gground = None         # the ground under its GIBS pixels (z, aspect, lat, lon, n, global row, column)
        self.ground = None          # (z, aspect, lat_e5, lon_e5, n, r, c, zmin, zmax) or False (no ground: the sea)
        self.table = None           # snowcover.Table of S1 rows (or None: no snow)
        self.made = 0.0             # when the table was made (clock)
        self.states = []            # the bins' states (snowfield_table's, S-7)
        self.z_cell = None
        self.rows = None            # the last rows (logs)


class SnowField:
    """The S1 snow field round the anchor (stage A). update() blocks (network, DEM, kernels): run it in an executor.

    fetch_json(url) -> the parsed body (Open-Meteo); fetch_bytes(url) -> bytes or None (a 404: not there yet);
    dem: dem.Dem (sample()); cache_dir: where the IMS days and the cells' ground are kept; ims_reader(body, boxes): the
    IMS windows (earthfast snowobs_ims_windows). Tests pass fakes."""

    def __init__(self, dem, fetch_json, fetch_bytes, cache_dir, clock=time.time, ims_reader=None):
        fast.require(NATIVE, "the snow field (BNE_LOCAL_WEATHER=snowfield)")
        self.dem, self.fetch_json, self.fetch_bytes = dem, fetch_json, fetch_bytes
        self.dir = cache_dir
        self.clock = clock
        self.ims_reader = ims_reader if ims_reader is not None else fast._ef.snowobs_ims_windows
        self.lock = threading.Lock()
        self.cells = {}             # (ci, cj) -> _Cell
        self.refreshed = {}         # ring name -> (anchor cell, refresh slot) of its last forcing
        self.retry = {}             # ring name -> (clock of the next try, failures in a row)
        self.fcache = {}            # ((lat, lon), ring, refresh slot) -> (records, times, z_cell): forcing reused
        self.weight_day = (None, 0.0)
        self.ims = {}               # day -> (r0, c0, window uint8) round the last box
        self.changes = 0

    # -- the request budget
    def _spend(self, w):
        today = datetime.datetime.fromtimestamp(self.clock(), datetime.timezone.utc).date()
        day, used = self.weight_day
        if day != today:
            used = 0.0
        if used + w > WEIGHT_CAP:
            return False
        self.weight_day = (today, used + w)
        return True

    # -- observations
    def _ims_day(self, day, box):
        """The IMS window (r0, c0, classes) of a day round box (r0, r1, c0, c1), or None (no file)."""
        hit = self.ims.get(day)
        if hit is not None and hit[3] == box:
            return hit
        path = os.path.join(self.dir, "ims", os.path.basename(ims_url(day)))
        body = None
        if os.path.exists(path):
            with open(path, "rb") as f:
                body = f.read()
        else:
            body = self.fetch_bytes(ims_url(day))
            if body is None:
                return None
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.{os.getpid()}.part"
            with open(tmp, "wb") as f:
                f.write(body)
            os.replace(tmp, path)
            self._prune_ims()
        r0, r1, c0, c1 = box
        win = self.ims_reader(body, [box])[0]
        out = (r0, c0, fast._ef.snowobs_classes(win.ravel(), "ims").reshape(win.shape), box)
        self.ims[day] = out
        while len(self.ims) > IMS_KEEP:
            self.ims.pop(min(self.ims))
        return out

    def _prune_ims(self):
        d = os.path.join(self.dir, "ims")
        files = sorted(f for f in os.listdir(d) if f.endswith(".tif.gz"))
        for f in files[:-IMS_KEEP]:
            try:
                os.remove(os.path.join(d, f))
            except OSError:
                pass

    def _ims_newest(self, box):
        """The newest two IMS days round box: [(day, window), ...] newest first (fewer when missing)."""
        today = datetime.datetime.fromtimestamp(self.clock(), datetime.timezone.utc).date()
        out = []
        for back in range(IMS_DAYS_BACK + 1):
            day = today - datetime.timedelta(days=back)
            try:
                w = self._ims_day(day, box)
            except Exception as e:      # (a network failure: the older days, or none)
                log.info("snow field: IMS %s: %s: %s", day, type(e).__name__, e)
                w = None
            if w is not None:
                out.append((day, w))
                if len(out) == 2:
                    break
            elif out:
                break           # (the day before the newest missing: no 2-day rule today)
        return out

    # -- ground
    def _ground(self, cell):
        """The cell's IMS pixels' ground and its relief, kept on disk (static: the DEM and the grid)."""
        if cell.ground is not None:
            return cell.ground
        ci, cj = cell.key
        zoom, res = (FAR_GROUND_ZOOM, FAR_GROUND_RES) if cell.far else (GROUND_ZOOM, GROUND_RES)
        path = os.path.join(self.dir, "ground", f"v{GROUND_VERSION}_{NATIVE}_z{zoom}", f"{ci}_{cj}.npz")
        if os.path.exists(path):
            with np.load(path) as g:
                cell.ground = tuple(g[k] for k in ("z", "aspect", "lat", "lon", "n", "r", "c")) + \
                    (int(g["zmin"]), int(g["zmax"]))
            return cell.ground
        s, w, n, e = cell_box(ci, cj)
        m = 2 * res
        rows, cols = int(round((n - s + 2 * m) / res)), int(round((e - w + 2 * m) / res))
        la = (n + m) - (np.arange(rows) + 0.5) * res
        lo = (w - m) + (np.arange(cols) + 0.5) * res
        z = np.asarray(self.dem.sample(la[:, None], lo[None, :], zoom), np.float32)
        inner = z[2:-2, 2:-2]
        land = inner[np.isfinite(inner) & (inner > 0.5)]
        if land.size == 0:
            cell.ground = False
            return False
        zmin, zmax = int(np.floor(land.min())), int(np.ceil(land.max()))
        ef = fast._ef
        pr, pc = ef.snowobs_ims_pixels(np.array([s, s, n, n, 0.5 * (s + n)]), np.array([w, e, w, e, 0.5 * (w + e)]))
        if (pr < 0).any():
            gz = tuple(np.zeros(0, t) for t in (np.int32, np.int8, np.int32, np.int32, np.uint32, np.int64, np.int64))
        else:
            r0, r1, c0, c1 = int(pr.min()) - 2, int(pr.max()) + 2, int(pc.min()) - 2, int(pc.max()) + 2
            gz_, ga, gla, glo, gn = ef.snowobs_ground_ims(z, n + m, w - m, res, r0, c0, r1 - r0 + 1, c1 - c0 + 1)
            ix = np.arange(gz_.size)
            box = (int(round(s * 1e5)), int(round(w * 1e5)), int(round(n * 1e5)), int(round(e * 1e5)))
            keep = (gn > 0) & (gla >= box[0]) & (gla < box[2]) & (glo >= box[1]) & (glo < box[3])
            ix = ix[keep]
            gz = (gz_[keep], ga[keep], gla[keep], glo[keep], gn[keep], r0 + ix // (c1 - c0 + 1),
                  c0 + ix % (c1 - c0 + 1))
        cell.ground = gz + (zmin, zmax)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp.npz"
        np.savez(tmp, z=gz[0], aspect=gz[1], lat=gz[2], lon=gz[3], n=gz[4], r=gz[5], c=gz[6], zmin=zmin, zmax=zmax)
        os.replace(tmp, path)
        return cell.ground

    # -- the clear-satellite rule's maps (the near ring)
    def _cmap(self):
        """(rgb, value) lists of the NDSI layers' GIBS colour map (kept on disk once fetched)."""
        cm = getattr(self, "_cmap_cache", None)
        if cm is not None:
            return cm
        import xml.etree.ElementTree as ET
        path = os.path.join(self.dir, "gibs", "MODIS_NDSI_Snow_Cover.xml")
        if os.path.exists(path):
            with open(path, "rb") as f:
                body = f.read()
        else:
            body = self.fetch_bytes(GIBS_CMAP_URL)
            if body is None:
                raise IOError("snow field: no GIBS colour map")
            self._keep(path, body)
        rgb, val = [], []
        for e in ET.fromstring(body).iter():
            if e.tag.endswith("ColorMapEntry"):
                sv = (e.get("sourceValue") or "").strip("[]")
                r, g, b = (int(x) for x in e.get("rgb").split(","))
                if sv.isdigit() and int(sv) <= 255:
                    rgb.append(r << 16 | g << 8 | b)
                    val.append(int(sv))
        self._cmap_cache = (rgb, val)
        return self._cmap_cache

    def _keep(self, path, body):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.part"
        with open(tmp, "wb") as f:
            f.write(body)
        os.replace(tmp, path)

    def _gibs_tile(self, layer, day, row, col):
        """A GIBS NDSI tile's data values (uint8 512 x 512), None when not published (kept on disk: a past day's
        tiles never change)."""
        cache = getattr(self, "_tiles", None)
        if cache is None:
            cache = self._tiles = {}
        k = (layer, day, row, col)
        if k in cache:
            return cache[k]
        path = os.path.join(self.dir, "gibs", layer, day.isoformat(), f"{row}_{col}.png")
        if os.path.exists(path):
            with open(path, "rb") as f:
                body = f.read()
        else:
            body = self.fetch_bytes(GIBS_URL.format(layer=layer, date=day.isoformat(), row=row, col=col))
            if body is None:
                cache[k] = None
                return None
            self._keep(path, body)
        rgb, val = self._cmap()
        v = fast._ef.snowobs_gibs_values(body, rgb, val)
        cache[k] = v
        while len(cache) > 64:
            cache.pop(next(iter(cache)))
        return v

    def _prune_gibs(self, today):
        d = os.path.join(self.dir, "gibs")
        if not os.path.isdir(d):
            return
        import shutil
        for layer in CLEAR_LAYERS:
            ld = os.path.join(d, layer)
            if not os.path.isdir(ld):
                continue
            for day in os.listdir(ld):
                try:
                    old = (today - datetime.date.fromisoformat(day)).days > GIBS_KEEP_DAYS
                except ValueError:
                    continue
                if old:
                    shutil.rmtree(os.path.join(ld, day), ignore_errors=True)

    def _gibs_ground(self, cell):
        """The ground under the GIBS pixels of a cell (their global row / column on the level-7 grid), kept on disk."""
        if cell.gground is not None:
            return cell.gground
        ci, cj = cell.key
        path = os.path.join(self.dir, "ground", f"v{GROUND_VERSION}_{NATIVE}_gibs_z{GROUND_ZOOM}", f"{ci}_{cj}.npz")
        if os.path.exists(path):
            with np.load(path) as g:
                cell.gground = tuple(g[k] for k in ("z", "aspect", "lat", "lon", "n", "r", "c"))
            return cell.gground
        s, w, n, e = cell_box(ci, cj)
        res, m = GROUND_RES, 2 * GROUND_RES
        rows, cols = int(round((n - s + 2 * m) / res)), int(round((e - w + 2 * m) / res))
        la = (n + m) - (np.arange(rows) + 0.5) * res
        lo = (w - m) + (np.arange(cols) + 0.5) * res
        z = np.asarray(self.dem.sample(la[:, None], lo[None, :], GROUND_ZOOM), np.float32)
        px = GIBS_DEG / GIBS_PX
        r0, r1 = int(math.floor((90.0 - (n + m)) / px)), int(math.floor((90.0 - (s - m)) / px))
        c0, c1 = int(math.floor((w - m + 180.0) / px)), int(math.floor((e + m + 180.0) / px))
        gz, ga, gla, glo, gn = fast._ef.snowobs_ground_grid(z, n + m, w - m, res, 90.0 - r0 * px, -180.0 + c0 * px, px,
                                                           r1 - r0 + 1, c1 - c0 + 1)
        box = (int(round(s * 1e5)), int(round(w * 1e5)), int(round(n * 1e5)), int(round(e * 1e5)))
        keep = (gn > 0) & (gla >= box[0]) & (gla < box[2]) & (glo >= box[1]) & (glo < box[3])
        ix = np.arange(gz.size)[keep]
        cell.gground = (gz[keep], ga[keep], gla[keep], glo[keep], gn[keep], r0 + ix // (c1 - c0 + 1),
                        c0 + ix % (c1 - c0 + 1))
        g = cell.gground
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp.npz"
        np.savez(tmp, z=g[0], aspect=g[1], lat=g[2], lon=g[3], n=g[4], r=g[5], c=g[6])
        os.replace(tmp, path)
        return cell.gground

    def _clear_counts(self, cell, k0, n):
        """The band counts of the latest clear VIIRS / MODIS day of a cell (the CLEAR_DAYS before today, VIIRS first),
        None when none saw CLEAR_SEEN of its land."""
        g = self._gibs_ground(cell)
        if g is None or g[0].size == 0:
            return None
        today = datetime.datetime.fromtimestamp(self.clock(), datetime.timezone.utc).date()
        rr, cc = g[5], g[6]
        trow, tcol = rr // GIBS_PX, cc // GIBS_PX
        tiles = sorted(set(zip(trow.tolist(), tcol.tolist())))
        s, w, nn, e = cell_box(*cell.key)
        box = (int(round(s * 1e5)), int(round(w * 1e5)), int(round(nn * 1e5)), int(round(e * 1e5)))
        for back in range(1, CLEAR_DAYS + 1):
            day = today - datetime.timedelta(days=back)
            for layer in CLEAR_LAYERS:
                vals = np.full(g[0].size, 255, np.uint8)
                ok = True
                for tr, tc in tiles:
                    v = self._gibs_tile(layer, day, tr, tc)
                    if v is None:
                        ok = False
                        break
                    m = (trow == tr) & (tcol == tc)
                    vals[m] = v[rr[m] - tr * GIBS_PX, cc[m] - tc * GIBS_PX]
                if not ok:
                    continue
                land = (vals != 237) & (vals != 239)
                if (vals <= 100).sum() < CLEAR_SEEN * max(1, int(land.sum())):
                    continue
                obs = fast._ef.snowobs_classes(vals, "ndsi", CLEAR_NDSI)
                return fast._ef.snowobs_cell_counts(g[0], g[1], g[2], g[3], g[4], obs, box, k0, n).astype(np.int64)
        return None

    def _counts(self, cell, day_win, k0, n):
        """The cell's band counts from one IMS day's window (None: no map there)."""
        g = cell.ground
        if not g or g[0].size == 0 or day_win is None:
            return None
        r0, c0, win, _box = day_win
        rr, cc = g[5] - r0, g[6] - c0
        ok = (rr >= 0) & (cc >= 0) & (rr < win.shape[0]) & (cc < win.shape[1])
        obs = np.zeros(g[0].size, np.uint8)
        obs[ok] = win[rr[ok], cc[ok]]
        s, w, nn, e = cell_box(*cell.key)
        box = (int(round(s * 1e5)), int(round(w * 1e5)), int(round(nn * 1e5)), int(round(e * 1e5)))
        return fast._ef.snowobs_cell_counts(g[0], g[1], g[2], g[3], g[4], obs, box, k0, n).astype(np.int64)

    # -- forcing and tables
    def _slot(self, ring):
        return int(self.clock() // SLOT_S[ring])

    @staticmethod
    def _pkey(point):
        return (round(float(point[0]), 4), round(float(point[1]), 4))

    def _missing(self, points, ring):
        """The points whose forcing for this refresh slot is not had yet (forcing is reused by location and slot: a
        ring that moved within its slot asks only for its new points)."""
        slot = self._slot(ring)
        return [q for q in points if (self._pkey(q), ring, slot) not in self.fcache]

    def _admit(self, group, n_new, n_core, extra=0.0):
        """May a group ("core": the anchor's nearest cells, "rest": the rest of the near ring, "far") ask Open-Meteo
        for n_new locations now? The core within the day's weight cap; the rest and the far ring only with the core's
        hourly weight (n_core locations) reserved for the hours left in the UTC day after this one. extra: weight this
        update spends first (the core's). Checked before any of the ring's work (the IMS days, the ground, the kernel)."""
        now = self.clock()
        today = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).date()
        day, used = self.weight_day
        if day != today:
            used = 0.0
        w = (om_weight(n_new) if n_new else 0.0) + extra
        if group != "core" and n_core:
            left_h = math.ceil((86400.0 - now % 86400.0) / REFRESH_NEAR_S) - 1
            w += om_weight(n_core) * max(0, left_h)
        return used + w <= WEIGHT_CAP

    def _defer(self, ring, why):
        """A ring held back until the day's weight refills (the next UTC midnight): no retry on every pose meanwhile."""
        now = self.clock()
        until = (math.floor(now / 86400.0) + 1) * 86400.0
        self.retry[ring] = (until, self.retry.get(ring, (0.0, 0))[1])
        log.info("snow field: the %s ring waits for the next day's Open-Meteo weight (%s, cap %d)", ring, why,
                 WEIGHT_CAP)

    def _forcing(self, points, ring, kind="background"):
        """[(records, times, z_cell)] of the points, those not had for this slot fetched (one request a
        LOCATIONS_MAX), the rest from the cache. A failed request raises (update: the ring's retry); the weight of
        one that was not answered is given back."""
        import snow as snowmod
        slot = self._slot(ring)
        need = [q for q in points if (self._pkey(q), ring, slot) not in self.fcache]
        for i in range(0, len(need), LOCATIONS_MAX):
            part = need[i:i + LOCATIONS_MAX]
            if not self._spend(om_weight(len(part))):
                raise _Deferred("weight cap")
            try:
                body = self.fetch_json(hourly_url_many(part), kind)
            except Exception:
                self._spend(-om_weight(len(part)))      # (not asked after all: the day's weight given back)
                raise
            body = body if isinstance(body, list) else [body]
            if len(body) != len(part):
                raise IOError(f"snow field: {len(body)} answers for {len(part)} locations")
            for q, om in zip(part, body):
                recs, times, z_cell, _off = snowmod.records(om)
                self.fcache[(self._pkey(q), ring, slot)] = (recs, times, z_cell)
        for k in [k for k in self.fcache if k[2] < self._slot(k[1]) - 1]:
            del self.fcache[k]
        return [self.fcache[(self._pkey(q), ring, slot)] for q in points]

    def _make(self, cells, forcing, obs_days, staged):
        """The tables of cells (each with its forcing (records, times, z_cell)); the region's seed from all of them.
        Staged (staged["tables"][key] = (table, states, z_cell, rows, made)): published on the event loop with the
        renders' invalidation (publish), never here."""
        import snow as snowmod
        import snowcover
        ef = fast._ef
        now = self.clock()
        seed = [(int(z), int(r[0, 9]) if r.shape[0] and r[0, 9] != snowmod.MISSING else snowmod.MISSING)
                for r, t, z in forcing if t]
        out = staged["tables"]
        for cell, (recs, times, z_cell) in zip(cells, forcing):
            if not times:
                continue
            g = self._ground(cell)
            if not g:
                out[cell.key] = (None, [], None, None, now)
                continue
            zmin, zmax = g[7], g[8]
            k0, n = ef.snowfield_rows(zmin, zmax)
            today = self._counts(cell, obs_days[0][1] if obs_days else None, k0, n)
            before = self._counts(cell, obs_days[1][1] if len(obs_days) > 1 else None, k0, n)
            clear = None
            if not cell.far:            # (the clear-satellite rule: the near ring's cells)
                try:
                    clear = self._clear_counts(cell, k0, n)
                except Exception as e:      # (GIBS down: IMS alone this time)
                    log.info("snow field: no clear day for %s: %s: %s", cell.key, type(e).__name__, e)
            upto = snowmod.hour_index(times, now)
            lat_e2 = int(round((cell_box(*cell.key)[0] + CELL_DEG / 2) * 100))
            k_lo, rows, states, _runs = ef.snowfield_table(recs, upto, int(z_cell), lat_e2, zmin, zmax, seed,
                                                           today, k0, before, k0, cell.states, clear=clear,
                                                           k0_clear=k0)
            look = tuple(tuple(int(v) for v in (r[0], r[1], r[2], r[3], r[4], r[6])) for r in rows)
            new = snowcover.Table(look, snowcover.NO_ROAD, 0) if any(r[1] > 0 or r[3] > 0 for r in look) else None
            out[cell.key] = (new, states, int(z_cell), rows, now)

    def _ring_key(self, ring, lat, lon):
        return (cell_of(lat, lon), self._slot(ring))

    def due(self, lat, lon):
        """Is a ring's work due round (lat, lon) (a new anchor cell, a new refresh slot), and not held back (a failed
        ring's retry time, a ring waiting for the day's weight)?"""
        now = self.clock()
        return any(self.refreshed.get(r) != self._ring_key(r, lat, lon) and now >= self.retry.get(r, (0.0, 0))[0]
                   for r in ("near", "far"))

    def update(self, lat, lon, ring="near"):
        """(executor) The anchor at (lat, lon): one ring's due work ("near": hourly, its own forcing a cell, asked for
        as demand: the teleport's own place is never dropped; "far": every 6 h, a 1-degree block's forcing, background,
        only with the near ring's weight for the day reserved), the IMS days once a day. Returns a staged result (the
        cells' new tables, the cells gone) for publish on the event loop, or None (nothing due, held back, failed):
        nothing drawn changes here."""
        now = self.clock()
        near, far = rings(lat, lon, NEAR_KM, FAR_KM)
        far_cells = {c for v in far.values() for c in v}
        staged = {"ring": ring, "key": self._ring_key(ring, lat, lon), "tables": {}, "gone": []}
        with self.lock:
            # (S-6: a table older than a day, or out of the rings, goes (at publish): never another place's snow)
            keep = set(near) | far_cells
            staged["gone"] = [k for k, c in self.cells.items()
                              if k not in keep or (c.made and now - c.made > TABLE_TTL_S)]
            for k in keep:
                if k not in self.cells:
                    s_, w_, n_, e_ = cell_box(*k)
                    self.cells[k] = _Cell(k, (0.5 * (s_ + n_), 0.5 * (w_ + e_)))
                c = self.cells[k]
                tier = k in far_cells
                if c.far != tier:       # (both ways, every time: the ground from the tier's zoom, the clear day near)
                    c.far, c.ground = tier, None
        self._prune_gibs(datetime.datetime.fromtimestamp(now, datetime.timezone.utc).date())
        if self.refreshed.get(ring) == staged["key"] or now < self.retry.get(ring, (0.0, 0))[0]:
            return staged if staged["gone"] else None
        core_pts = [self.cells[k].point for k in near[:CORE_CELLS]]
        rest_pts = [self.cells[k].point for k in near[CORE_CELLS:]]
        rest_ok = False
        if ring == "near":
            n_core = len(self._missing(core_pts, "core"))
            if not self._admit("core", n_core, len(core_pts)):
                self._defer(ring, f"{n_core} new locations")
                return staged if staged["gone"] else None
            n_rest = len(self._missing(rest_pts, "rest"))
            # (the rest of the near ring: when the budget allows with the core's reserve, else its last forcing)
            rest_ok = not n_rest or self._admit("rest", n_rest, len(core_pts),
                                                om_weight(n_core) if n_core else 0.0)
            pts = core_pts
        else:
            pts = list(far)
            if not self._admit("far", len(self._missing(pts, "far")), len(core_pts)):
                self._defer(ring, f"{len(self._missing(pts, 'far'))} new locations")
                return staged if staged["gone"] else None
        try:
            box = self._ims_box(lat, lon)
            obs = self._ims_newest(box) if box is not None else []
            if ring == "near":
                core = [self.cells[k] for k in near[:CORE_CELLS]]
                forcing = self._forcing(core_pts, "core", "demand")
                rest = [self.cells[k] for k in near[CORE_CELLS:]]
                rforcing = None
                if rest and rest_ok:
                    try:
                        rforcing = self._forcing(rest_pts, "rest", "demand")
                    except _Deferred:
                        rforcing = None
                if rforcing is None and rest:
                    # (not admitted: the rest's cells refreshed from forcing still had for this slot, the others kept)
                    had = [(c, self.fcache.get((self._pkey(c.point), "rest", self._slot("rest")))) for c in rest]
                    rest = [c for c, h in had if h is not None]
                    rforcing = [h for _c, h in had if h is not None]
                # (one seed region for the whole near ring: the core's and the rest's model cells together)
                self._make(core + rest, forcing + list(rforcing or []), obs, staged)
            elif pts:
                forcing = self._forcing(pts, "far", "background")
                for bc, f in zip(pts, forcing):
                    cells = [self.cells[k] for k in far[bc]]
                    self._make(cells, [f] * len(cells), obs, staged)
        except _Deferred as e:
            self._defer(ring, str(e))
            return staged if staged["gone"] else None
        except Exception as e:      # (network, a DEM tile: this ring again after the backoff)
            fails = self.retry.get(ring, (0.0, 0))[1] + 1
            wait = min(RETRY_S[1], RETRY_S[0] * 2 ** (fails - 1))
            self.retry[ring] = (now + wait, fails)
            log.info("snow field: the %s ring round %.2f, %.2f failed: %s: %s (again in %.0f s)", ring, lat, lon,
                     type(e).__name__, e, wait)
            return staged if staged["gone"] else None
        self.retry.pop(ring, None)
        staged["done"] = True
        return staged

    def publish(self, staged):
        """(event loop) A staged update in effect: the cells gone dropped, the new tables and bin states taken, the
        ring's refresh noted. Returns the cells whose drawn table changed (the caller invalidates their renders in the
        same step: Service._publish)."""
        changed = []
        with self.lock:
            tables = staged.get("tables", {})
            for k in staged.get("gone", ()):
                if k in tables:
                    continue            # (its fresh table replaces the expired one: Codex on a656188c)
                c = self.cells.pop(k, None)
                if c is not None and c.table is not None:
                    changed.append(k)
            for k, (table, states, z_cell, rows, made) in staged.get("tables", {}).items():
                c = self.cells.get(k)
                if c is None:
                    continue
                c.states, c.z_cell, c.rows, c.made = states, z_cell, rows, made
                if table != c.table:
                    c.table = table
                    changed.append(k)
            if staged.get("done"):
                self.refreshed[staged["ring"]] = staged["key"]
        if changed:
            self.changes += 1
            log.info("snow field: %s ring, %d cells changed (%d with snow)", staged.get("ring"), len(changed),
                     sum(1 for c in self.cells.values() if c.table is not None))
        return changed

    def _ims_box(self, lat, lon):
        """The IMS window (r0, r1, c0, c1) round the rings, or None (the southern hemisphere); from the anchor cell's
        centre, so it stays one window (and the day's file is read once) while the anchor stays in its cell."""
        s_, w_, n_, e_ = cell_box(*cell_of(lat, lon))
        lat, lon = 0.5 * (s_ + n_), 0.5 * (w_ + e_)
        span = FAR_KM / 111.2 + CELL_DEG
        spl = span / max(0.05, dmath.cos(math.radians(min(abs(lat) + span, 89.0))))
        la = np.clip(np.array([lat - span, lat - span, lat + span, lat + span, lat]), -89.9, 89.9)
        lo = np.array([lon - spl, lon + spl, lon - spl, lon + spl, lon])
        r, c = fast._ef.snowobs_ims_pixels(la.astype(np.float64), lo.astype(np.float64))
        ok = r >= 0
        if not ok.any():
            return None
        return (max(0, int(r[ok].min()) - 4), min(24575, int(r[ok].max()) + 4),
                max(0, int(c[ok].min()) - 4), min(24575, int(c[ok].max()) + 4))

    def provider(self, south, west, north, east):
        """snowcover's provider: a Field of the cells' S1 tables (None: no snow known in the box)."""
        import snowcover
        with self.lock:
            cells = {k: c.table for k, c in self.cells.items() if c.table is not None}
        if not cells:
            return None
        return snowcover.Field(CELL_DEG, cells=cells)


def make(dem, net, cache_dir):
    """The server's SnowField (earthd, when the part is on): Open-Meteo and the IMS days through the server's
    netfetch.Fetcher (its policies: api.open-meteo.com, noaadata.apps.nsidc.org), its files under cache_dir/snowfield
    (the IMS days prune themselves to IMS_KEEP; a cell's ground is a few kB)."""
    import json
    import netfetch

    def fetch_json(url, kind="background"):
        with netfetch.stale_ok():
            return json.loads(net.get(url, kind=kind).body.decode("utf-8"))

    def fetch_bytes(url):
        try:
            # (demand: one file a day, waited for in the field's executor; background was dropped whenever the
            # host's bucket was low, and the field ran without the map)
            return net.get(url, kind="demand").body
        except netfetch.NotFound:        # (the day's file not published yet)
            return None

    return SnowField(dem, fetch_json, fetch_bytes, os.path.join(cache_dir, "snowfield"))
