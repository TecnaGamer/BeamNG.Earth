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

import fast

log = logging.getLogger("earthd.snowfield")

SWITCH = "BNE_LOCAL_WEATHER"
PARTS = frozenset(p.strip() for p in os.environ.get("BNE_LOCAL_WEATHER", "").lower().replace(";", ",").split(",")
                  if p.strip())
ON = "snowfield" in PARTS
NATIVE = 645

CELL_DEG = 0.25
BLOCK_DEG = 1.0
NEAR_KM = 90.0
FAR_KM = 450.0
REFRESH_NEAR_S = 3600.0
REFRESH_FAR_S = 3 * 3600.0
WEIGHT_CAP = 2000.0             # Open-Meteo weighted calls a day (PLAN_FINAL D-3: Experimental parts)
LOCATIONS_MAX = 200             # locations a request (Open-Meteo allows 1,000; a near ring holds ~40)
TABLE_TTL_S = 24 * 3600.0
GROUND_ZOOM = 9
GROUND_RES = 0.002              # deg a DEM sample under the IMS pixels (~220 m: ~20 a 1 km pixel)
GROUND_VERSION = 1
IMS_URL = "https://noaadata.apps.nsidc.org/NOAA/G02156/GIS/1km/{y}/ims{y}{doy:03d}_1km_GIS_v1.3.tif.gz"
IMS_DAYS_BACK = 4               # the newest file is looked for this many days back
IMS_KEEP = 3                    # day files kept on disk


def cell_of(lat, lon):
    """The 0.25-degree cell (ci, cj) of a point (snowcover.Field's indices)."""
    return int(math.floor((lon + 180.0) / CELL_DEG)), int(math.floor((90.0 - lat) / CELL_DEG))


def cell_box(ci, cj):
    """(south, west, north, east) of a cell."""
    west, north = ci * CELL_DEG - 180.0, 90.0 - cj * CELL_DEG
    return north - CELL_DEG, west, north, west + CELL_DEG


def _km(lat0, lon0, lat1, lon1):
    k = math.cos(math.radians(0.5 * (lat0 + lat1)))
    return math.hypot((lat1 - lat0) * 111.2, (((lon1 - lon0) + 180.0) % 360.0 - 180.0) * 111.2 * k)


def rings(lat, lon, near_km=NEAR_KM, far_km=FAR_KM):
    """(near cells, far cells by their 1-degree block centre {block centre (lat, lon): [cells]}) round a point, each
    list nearest first (the anchor's own cell first: S-6's urgent table)."""
    near, far = [], {}
    span_lat = far_km / 111.2
    span_lon = far_km / (111.2 * max(0.05, math.cos(math.radians(min(abs(lat), 89.0)))))
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


class _Cell:
    __slots__ = ("key", "point", "ground", "table", "made", "states", "z_cell", "rows")

    def __init__(self, key, point):
        self.key, self.point = key, point
        self.ground = None          # (z, aspect, lat_e5, lon_e5, n, r, c, zmin, zmax) or False (no ground: the sea)
        self.table = None           # snowcover.Table of S1 rows (or None: no snow)
        self.made = 0.0             # when the table was made (clock)
        self.states = []            # the bins' states (snowfield_table's, S-7)
        self.z_cell = None
        self.rows = None            # the last rows (logs)


class SnowField:
    """The S1 snow field round the anchor (stage A). update() blocks (network, DEM, kernels): run it in an executor.

    fetch_json(url) -> the parsed body (Open-Meteo); fetch_bytes(url) -> bytes or None (a 404: not there yet);
    dem: dem.Dem (sample()); cache_dir: where the IMS days and the cells' ground are kept. Tests pass fakes."""

    def __init__(self, dem, fetch_json, fetch_bytes, cache_dir, clock=time.time, cover=None):
        fast.require(NATIVE, "the snow field (BNE_LOCAL_WEATHER=snowfield)")
        self.dem, self.fetch_json, self.fetch_bytes = dem, fetch_json, fetch_bytes
        self.dir = cache_dir
        self.clock = clock
        self.cover = cover
        self.lock = threading.Lock()
        self.cells = {}             # (ci, cj) -> _Cell
        self.refreshed = {}         # ring name -> clock of its last forcing
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
        win = fast._ef.snowobs_ims_windows(body, [box])[0]
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
        path = os.path.join(self.dir, "ground", f"v{GROUND_VERSION}_{NATIVE}_z{GROUND_ZOOM}", f"{ci}_{cj}.npz")
        if os.path.exists(path):
            with np.load(path) as g:
                cell.ground = tuple(g[k] for k in ("z", "aspect", "lat", "lon", "n", "r", "c")) + \
                    (int(g["zmin"]), int(g["zmax"]))
            return cell.ground
        s, w, n, e = cell_box(ci, cj)
        m = 2 * GROUND_RES
        rows, cols = int(round((n - s + 2 * m) / GROUND_RES)), int(round((e - w + 2 * m) / GROUND_RES))
        la = (n + m) - (np.arange(rows) + 0.5) * GROUND_RES
        lo = (w - m) + (np.arange(cols) + 0.5) * GROUND_RES
        z = np.asarray(self.dem.sample(la[:, None], lo[None, :], GROUND_ZOOM), np.float32)
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
            gz_, ga, gla, glo, gn = ef.snowobs_ground_ims(z, n + m, w - m, GROUND_RES, r0, c0, r1 - r0 + 1,
                                                         c1 - c0 + 1)
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
    def _forcing(self, points):
        """[(records, times, z_cell)] of the points (one request a LOCATIONS_MAX), or None (over budget, failed)."""
        import snow as snowmod
        out = []
        for i in range(0, len(points), LOCATIONS_MAX):
            part = points[i:i + LOCATIONS_MAX]
            if not self._spend(om_weight(len(part))):
                log.info("snow field: Open-Meteo weight cap (%d a day) reached: tables kept as they are", WEIGHT_CAP)
                return None
            body = self.fetch_json(hourly_url_many(part))
            body = body if isinstance(body, list) else [body]
            if len(body) != len(part):
                raise IOError(f"snow field: {len(body)} answers for {len(part)} locations")
            for om in body:
                recs, times, z_cell, _off = snowmod.records(om)
                out.append((recs, times, z_cell))
        return out

    def _make(self, cells, forcing, obs_days):
        """The tables of cells (each with its forcing (records, times, z_cell)); the region's seed from all of them.
        Returns the cells whose table changed."""
        import snow as snowmod
        import snowcover
        ef = fast._ef
        now = self.clock()
        seed = [(int(z), int(r[0, 9]) if r.shape[0] and r[0, 9] != snowmod.MISSING else snowmod.MISSING)
                for r, t, z in forcing if t]
        changed = []
        for cell, (recs, times, z_cell) in zip(cells, forcing):
            if not times:
                continue
            g = self._ground(cell)
            if not g:
                if cell.table is not None:
                    cell.table = None
                    changed.append(cell.key)
                continue
            zmin, zmax = g[7], g[8]
            k0, n = ef.snowfield_rows(zmin, zmax)
            today = self._counts(cell, obs_days[0][1] if obs_days else None, k0, n)
            before = self._counts(cell, obs_days[1][1] if len(obs_days) > 1 else None, k0, n)
            upto = snowmod.hour_index(times, now)
            lat_e2 = int(round((cell_box(*cell.key)[0] + CELL_DEG / 2) * 100))
            k_lo, rows, states, _runs = ef.snowfield_table(recs, upto, int(z_cell), lat_e2, zmin, zmax, seed,
                                                           today, k0, before, k0, cell.states)
            cell.states, cell.z_cell, cell.rows, cell.made = states, int(z_cell), rows, now
            look = tuple(tuple(int(v) for v in (r[0], r[1], r[2], r[3], r[4], r[6])) for r in rows)
            new = snowcover.Table(look, snowcover.NO_ROAD, 0) if any(r[1] > 0 or r[3] > 0 for r in look) else None
            if new != cell.table:
                cell.table = new
                changed.append(cell.key)
        return changed

    def update(self, lat, lon):
        """(executor) The anchor at (lat, lon): the rings' due work (the near ring hourly, the far ring every 3 h, the
        IMS days once a day). Returns the cells whose table changed (the server re-renders what shows them)."""
        now = self.clock()
        near, far = rings(lat, lon)
        with self.lock:
            # (S-6: a table older than a day, or out of the rings, goes: never another place's snow)
            keep = set(near) | {c for v in far.values() for c in v}
            gone = [k for k, c in self.cells.items() if k not in keep or (c.made and now - c.made > TABLE_TTL_S)]
            for k in gone:
                self.cells.pop(k, None)
            for k in keep:
                if k not in self.cells:
                    s, w, n, e = cell_box(*k)
                    self.cells[k] = _Cell(k, (0.5 * (s + n), 0.5 * (w + e)))
        changed = [k for k in gone]
        box = self._ims_box(lat, lon)
        obs = self._ims_newest(box) if box is not None else []
        ring_key = (cell_of(lat, lon), int(now // REFRESH_NEAR_S))
        if self.refreshed.get("near") != ring_key:
            cells = [self.cells[k] for k in near]
            forcing = self._forcing([c.point for c in cells])
            if forcing is not None:
                changed += self._make(cells, forcing, obs)
                self.refreshed["near"] = ring_key
        far_key = (cell_of(lat, lon), int(now // REFRESH_FAR_S))
        if self.refreshed.get("far") != far_key and far:
            pts = list(far)
            forcing = self._forcing(pts)
            if forcing is not None:
                for bc, f in zip(pts, forcing):
                    cells = [self.cells[k] for k in far[bc]]
                    changed += self._make(cells, [f] * len(cells), obs)
                self.refreshed["far"] = far_key
        if changed:
            self.changes += 1
            log.info("snow field: %d cells changed round %.2f, %.2f (%d with snow)", len(changed), lat, lon,
                     sum(1 for c in self.cells.values() if c.table is not None))
        return changed

    def _ims_box(self, lat, lon):
        """The IMS window (r0, r1, c0, c1) round the rings, or None (the southern hemisphere)."""
        span = FAR_KM / 111.2 + CELL_DEG
        spl = span / max(0.05, math.cos(math.radians(min(abs(lat) + span, 89.0))))
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
