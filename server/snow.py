"""The road surface and snow state at the player as the 'snowstate' message (winter W1; the weather's wet roads), from
the Rust kernel (native/earthfast/src/snow.rs, ~/bnge-tests/winterplan/PLAN.md sections 2 and 4). This module is glue:
it turns Open-Meteo's hourly forcing (weather.fetch_hourly) into the kernel's integer records, calls it for the player's
site and builds the message; the mod (beamngEarth/winter.lua) sets the ground models' grip from it.

Two parts, two switches (the user, 2026-11-24: wet roads all year, snow and ice under winter), off by default:
  BNE_WET_ROADS=on   the road-surface part alone: rain's water film drying over hours (dry / damp / wet slots, the stock
                     ASPHALT_WET collision type for tyre spray and sound); the window is the past 24 h and the next 6 h
  BNE_WINTER=on      everything: the wet roads and the snow and ice (roads by plow class, decks, the ground's pack, lake
                     ice, W2's band table); the window is the past 10 days (the pack's memory) and the next 6 h
  BNE_WINTER=light / heavy / ice: a preset (a fixed synthetic forcing, snow.rs preset), the same for everyone, no network
One hourly Open-Meteo request per 0.1-degree cell and UTC hour serves both (the weather's API, caps and credit).
Neither set: no service, no tracker work, no message (the server's output is the same as without this module).

Never in plans, cell facts or the decision archives (revisit.NOT_SWITCHES has both): the state is live.
"""
import logging
import math
import os
import threading
import time

import numpy as np

import fast
import netfetch
import weather
from snowwind import transport

log = logging.getLogger("earthd.snow")

PRESETS = ("light", "heavy", "ice")
_YES = ("on", "1", "true", "real", "yes")
_RAW = os.environ.get("BNE_WINTER", "").strip().lower()
WET_ROADS = os.environ.get("BNE_WET_ROADS", "").strip().lower() in _YES
WINTER = _RAW in _YES or _RAW in PRESETS
MODE = ("real" if _RAW in _YES else _RAW) if WINTER else ("real" if WET_ROADS else None)
ON = MODE is not None
# the request's window (hours before the current one, after it): the pack's memory with winter, a day for wet roads
WINDOW_WINTER = (240, 6)
WINDOW_WET = (24, 6)
VERSION = 1                 # snowstate.v: the message layout
NATIVE = 624                # earthfast: snow_state / snow_preset / snow_regime

CELL_DEG = weather.CELL_DEG     # the forcing's cell (~11 km): the weather's
CACHE_MAX = 64
REFETCH_M = 5000.0          # a new state after this far ...
REZ_M = 25.0                # ... or this much higher / lower (the snow line) ...
RETRY_S = 300.0             # ... or after a failed fetch
TIMEOUT_S = weather.TIMEOUT_S

# Open-Meteo hourly name -> (kernel field, scale to the kernel's integer unit)
FIELDS = (("temperature_2m", "F_T2M", 100), ("dew_point_2m", "F_TD", 100), ("wet_bulb_temperature_2m", "F_TW", 100),
          ("precipitation", "F_PRECIP", 100), ("snowfall_water_equivalent", "F_SNOWWE", 100),
          ("wind_speed_10m", "F_WIND", 10), ("shortwave_radiation", "F_SW", 1),
          ("soil_temperature_0cm", "F_TSURF", 100), ("freezing_level_height", "F_ZFREEZE", 1),
          ("snow_depth", "F_DEPTH", 1000), ("relative_humidity_2m", "F_RH", 1), ("cloud_cover", "F_CLOUD", 1))
# snow.rs's record layout (F_* indices; NF columns)
F = {"F_T2M": 0, "F_TD": 1, "F_TW": 2, "F_PRECIP": 3, "F_SNOWWE": 4, "F_WIND": 5, "F_SW": 6, "F_TSURF": 7,
     "F_ZFREEZE": 8, "F_DEPTH": 9, "F_RH": 10, "F_HOUR": 11, "F_MONTH": 12, "F_CLOUD": 13}
NF = 14
MISSING = -2 ** 31


def _int(v, scale):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return MISSING
    return int(round(v * scale))


def records(om):
    """(records [n, NF] int32, unix times [n], the data's elevation m, UTC offset s) of an hourly response."""
    hourly = om.get("hourly") or {}
    times = [int(t) for t in hourly.get("time") or [] if isinstance(t, (int, float))]
    n = len(times)
    out = np.full((n, NF), MISSING, np.int32)
    for name, key, scale in FIELDS:
        vals = hourly.get(name) or []
        col = F[key]
        for i in range(min(n, len(vals))):
            out[i, col] = _int(vals[i], scale)
    off = om.get("utc_offset_seconds") if isinstance(om.get("utc_offset_seconds"), int) else 0
    for i, t in enumerate(times):
        out[i, F["F_HOUR"]] = ((t + off) // 3600) % 24
        out[i, F["F_MONTH"]] = time.gmtime(t).tm_mon
    elev = om.get("elevation")
    z_cell = int(round(elev)) if isinstance(elev, (int, float)) and math.isfinite(elev) else 0
    return out, times, z_cell, off


def hour_index(times, now):
    """The row of the UTC hour that holds `now` (the last row at or before it; 0 before the window)."""
    h = int(now // 3600) * 3600
    best = 0
    for i, t in enumerate(times):
        if t <= h:
            best = i
    return best


def regime_at(lat, lon):
    """The plow regime 0..4 from the climate (the Koppen-Geiger class at the point: snow.rs regime_of_koppen)."""
    import climate
    fast.require(NATIVE, "snow regime")
    try:
        k = climate.koppen_at(lat, lon)
    except Exception as e:      # (the climate map missing: a source install without it)
        log.warning("snow: no climate class at %.3f, %.3f (%s): regime M2", lat, lon, e)
        return 2
    return int(fast._ef.snow_regime(int(k)))


def _mm(v01):
    return round(v01 / 10.0, 1)


def message(recs, upto, t, lat, lon, z_site, z_cell, regime, prev_bin=-1, src="real", built=0, raw=None,
            winter=True, prev_banks=None):
    """The 'snowstate' message for a site (pure but for the kernel): recs [n, NF] int32, the current row `upto`, its
    UTC hour t, the site's elevation (m) and the forcing's (m), the regime, the previous depth bin (hysteresis).
    raw: a dict that gets the kernel's own band rows and road condition codes (the W2 table: w2_table) and W3's road
    look (snowroad.rs look: zone shares, paint, decks, sidewalks, bank bins from prev_banks, dirt). winter False: the
    wet-road part only (conditions dry / damp / wet: snow.rs wet_only; no ground snow, bands or lake ice)."""
    fast.require(NATIVE, "snow state")
    names = fast._ef.snow_conditions()
    lat_e2 = int(round(lat * 100))
    g, roads, bands, lake = fast._ef.snow_state(np.ascontiguousarray(recs, np.int32), int(upto), int(z_site),
                                                int(z_cell), lat_e2, int(built), 0, False, int(regime), int(prev_bin))
    if not winter:
        roads = [[fast._ef.snow_wet_only(r[0])] + list(r[1:3]) + [fast._ef.snow_wet_only(r[3]), 0, 0] + list(r[6:])
                 for r in roads]
        g = [0] * 10 + list(g[10:])
        bands, lake = [], []
    look = None
    if winter:
        import snowroad
        if snowroad.ON:
            look = dict(fast._ef.snowroad_look([[int(v) for v in r] for r in roads], int(g[0]), int(g[9]), int(g[5]),
                                               bool(g[6]), int(g[10]), int(regime),
                                               list(prev_banks) if prev_banks is not None else [-1] * 5))
    if raw is not None:
        raw["bands"], raw["conds"], raw["ground"], raw["lake"] = bands, [int(r[0]) for r in roads[:5]], g, lake
        raw["look"] = look

    def row(r):
        return [names[r[0]], _mm(r[1]), round(r[2] / 100.0, 2), names[r[3]], _mm(r[4]), int(r[5]),
                round(r[6] / 1000.0, 3)]

    return {"type": "snowstate", "v": VERSION, "t": int(t), "src": src, "winter": bool(winter),
            "lat": float(lat), "lon": float(lon),
            "elevM": int(z_site), "cellElevM": int(z_cell), "regime": int(regime),
            "airC": round(g[10] / 100.0, 2), "surfC": round(g[11] / 100.0, 2),
            # ground: depth mm, bin D0..D6, cover 0..8, wet, crust, SWE mm, hours since the last snowfall
            "ground": [_mm(g[0]), int(g[8]), int(g[9]), int(g[7]), int(g[6]), round(g[1] / 100.0, 2), int(g[5])],
            # per plow class P0..P4: [condition, lane depth mm, pavement degC, edge condition, edge depth mm, grit,
            # wheel-track progress 0..1]; bridges: the same classes' decks
            "roads": [row(r) for r in roads[:5]], "bridges": [row(r) for r in roads[5:]],
            # the pack's band table: [elevation m, depth mm facing the pole, facing the equator, forest floor,
            # hours since snow]
            "bands": [[int(b[0]), _mm(b[1]), _mm(b[2]), _mm(b[3]), int(b[4])] for b in bands],
            # lake ice at the site (plan 2.9; the 10-day window's memory only): freezing degree-days (degC day), ice
            # mm (2.7 sqrt(FDD) cm), whole days below 0 degC in a row
            "lake": [round(lake[0] / 100.0, 2), int(lake[1]), int(lake[2])] if lake else [],
            "credit": weather.CREDIT if src == "real" else "",
            # W3's road look (winterroad.lua): per plow class P0..P4 the paint shown and a deck's snow (0..1), the
            # bank bin 0..4 and its dirt 0..2; the sidewalks' [state, cover 0..8] by slot (main streets, residential
            # cleared, residential left, unmaintained; states covered / trampled / shovelled / clear / icy)
            **({"look": {"marking": [round(v / 1000.0, 3) for v in look["marking"]],
                         "deck": [round(v / 1000.0, 3) for v in look["deck_cover"]],
                         "banks": list(look["bank_bin"]), "dirt": list(look["dirt"]),
                         "walks": [list(w) for w in look["walks"]]}} if look else {})}


def preset_message(name, lat, lon, prev_bin=-1, raw=None, prev_banks=None):
    """A preset's snowstate (the same for every player: its own synthetic window, regime and site)."""
    fast.require(NATIVE, "snow preset")
    recs, regime, z, lat_e2 = fast._ef.snow_preset(name)
    n = recs.shape[0]
    msg = message(recs, n - 1, 0, lat_e2 / 100.0, lon, z, z, regime, prev_bin, src=name, raw=raw,
                  prev_banks=prev_banks)
    msg["lat"], msg["lon"] = float(lat), float(lon)
    return msg


def w2_table(raw, prev_bins):
    """(rows, road shares, new bins) of W2's snowcover.Table from message()'s raw parts: up to 8 rows (z m, cover 0..8,
    depth bin 0..6, age 0..3) lowest first, the bins with 15 % hysteresis from prev_bins; road = the eighths of the
    ground's cover a P0..P4 road keeps (by its condition)."""
    rows, road = fast._ef.snow_band_table([list(b) for b in raw["bands"]], list(prev_bins), raw["conds"])
    rows = tuple(tuple(int(v) for v in r) for r in rows)
    look = raw.get("look")
    if look:        # (W3: the lanes', wheel tracks' and edges' shares per class)
        road = tuple(int(look["shares"][c][z]) for z in range(3) for c in range(5))
    return rows, tuple(int(v) for v in road), [r[2] for r in rows]


def cover_cell(lat, lon):
    """Indices and centre of the live snow field cell (snowlook.rs uses cell centres)."""
    steps = 1 / CELL_DEG
    ni, nj = round(360 * steps), round(180 * steps)
    ci = math.floor(180 * steps + lon * steps) % ni
    cj = min(max(math.floor(90 * steps - lat * steps), 0), nj - 1)
    return (ci, cj), round(90 - (cj + 0.5) * CELL_DEG, 4), round((ci + 0.5) * CELL_DEG - 180, 4)


def _snowcover():
    """W2's snowcover module when it is there, on and in its real mode (the provider's consumer), else None."""
    try:
        import snowcover
    except ImportError:
        return None
    return snowcover if getattr(snowcover, "ON", False) and getattr(snowcover, "MODE", "") == "real" else None


class Service:
    """The hourly forcing shared by every connection: fetch (injectable: tests pass a fake) + the (cell, UTC hour)
    cache, one fetch in flight per key. get() blocks: run it in an executor.

    Live ground-cover tables are quantized by weather cell at the forcing elevation. The provider
    supplies the nearby observed cells, and commit() invalidates textures when their tables change.
    Synthetic presets still have one table everywhere."""

    def __init__(self, fetch=None, mode=None, clock=time.time, retry_s=RETRY_S, cover=None, winter=None):
        self.fetch = fetch if fetch is not None else weather.fetch_hourly
        self.mode = mode or MODE or "real"
        self.winter = (WINTER or self.mode in PRESETS) if winter is None else bool(winter)
        self.window = WINDOW_WINTER if self.winter else WINDOW_WET
        self.clock, self.retry_s = clock, retry_s
        self.cache, self.inflight = {}, {}
        self.lock = threading.Lock()
        self.fetches = 0
        self.table = None           # uniform debug preset (rows, road, ice)
        self.cells = {}             # (ci, cj) -> (forcing hour, table, wind), oldest commit first
        self.bank_bins = [-1] * 5    # W3's bank bins (hysteresis)
        self.road_snow = None        # W3: the road skins' winter part (snowroad.wear_snow; Server.wear_arg)
        self.table_changes = 0
        self.cover = (cover if cover is not None else _snowcover()) if self.winter else None
        if self.cover is not None:
            self.cover.set_provider(self.provider)

    def provider(self, south, west, north, east):
        """Snapshot the observed cells whose bilinear support reaches the requested box."""
        if self.mode in PRESETS:
            t = self.table
            if self.has_cover(t):
                return self.cover.Field(CELL_DEG, uniform=self.cover.Table(*t))
            return None
        with self.lock:
            observed = tuple(self.cells.items())
        cells, winds = {}, {}
        covered = False
        for (ci, cj), (_, table, wind) in observed:
            lat = 90 - (cj + 0.5) * CELL_DEG
            lon = (ci + 0.5) * CELL_DEG - 180
            if not south - CELL_DEG <= lat <= north + CELL_DEG:
                continue
            lon += 360 * math.ceil((west - CELL_DEG - lon) / 360)
            if lon <= east + CELL_DEG:
                cells[ci, cj] = self.cover.Table(*table)
                if self.has_cover(table):
                    covered = True
                    if wind != (0, 0):
                        winds[ci, cj] = wind
        return self.cover.Field(CELL_DEG, cells=cells, winds=winds) if covered else None

    def set_mode(self, mode):
        """Use a shared snow preset for roads, or resume the live forcing."""
        if mode == self.mode or (mode not in PRESETS and mode != "real"):
            return False
        self.mode = mode
        return True

    @staticmethod
    def has_cover(table):
        return table is not None and (any(r[1] > 0 for r in table[0]) or table[2] > 0)

    def _neighbours_have_cover(self, cell):
        # Caller holds the lock. A bare cell only matters where it can share a
        # bilinear stencil with a snowy cell, including across the date line.
        ci, cj = cell
        columns = round(360 / CELL_DEG)
        return any(self.has_cover(self.cells.get(((ci + i) % columns, cj + j), (None, None, None))[1])
                   for j in (-1, 0, 1) for i in (-1, 0, 1))

    def commit(self, conn, raw):
        """Apply a new visual state and send ground-view values to the player who supplied them."""
        if raw is not None and raw.get("look") is not None:
            self._commit_roads(conn, raw["look"])
        if self.cover is None or raw is None or not raw.get("bands"):
            return False
        cell = raw.get("cover_cell")
        with self.lock:
            if cell is None:
                table = self.quantize(raw, self.table)
                changed = table != self.table
                self.table = table
            else:
                stamp, source, wind = raw["cover_state"]
                previous = self.cells.get(cell)
                changed = False
                if previous is None or stamp > previous[0]:
                    old = previous[1] if previous else None
                    table = self.quantize(source, old)
                    self.cells.pop(cell, None)
                    self.cells[cell] = stamp, table, wind
                    changed = (old != table or (previous is not None and previous[2] != wind)) and (
                        self.has_cover(old) or self._neighbours_have_cover(cell))
                    if len(self.cells) > CACHE_MAX:
                        expired = next(iter(self.cells))
                        _, removed, _ = self.cells.pop(expired)
                        changed |= self.has_cover(removed) or self._neighbours_have_cover(expired)
        g = raw["ground"]
        view = {"type": "snow", "snow": {"mode": "real", "cover": int(g[9]), "depth": int(g[8])}}
        if getattr(self.cover, "MODE", "real") != "real":
            return changed  # An explicit terrain preset owns its own cover and view.
        if changed:
            self.table_changes += 1
            if hasattr(conn.s, "snow_changed"):
                conn.s.snow_changed()
        if cell is not None:
            conn.send(view)
        elif changed:
            for c in list(getattr(conn.s, "connections", ()) or ()):
                c.send(view)
        return changed

    @staticmethod
    def quantize(raw, previous):
        bins = [r[2] for r in previous[0]] if previous is not None else [-1] * 8
        rows, road, _ = w2_table(raw, bins)
        lake = raw.get("lake") or [0, 0, 0]
        ice = int(fast._ef.snow_lake_share(int(lake[1]), previous[2] if previous is not None else -1))
        return rows, road, ice

    def _commit_roads(self, conn, look):
        """(event loop) W3's look of a new state: the bank bins kept for the hysteresis; when the road skins' winter
        part changes (snowroad.wear_snow: a first snowy state, a bank bin moving) the server sends the road tiles
        again (Server.snow_roads_changed)."""
        import snowroad
        with self.lock:
            self.bank_bins = [int(b) for b in look["bank_bin"]]
            part = snowroad.wear_snow(look)
            changed = part != self.road_snow
            self.road_snow = part
        if changed and hasattr(conn.s, "snow_roads_changed"):
            conn.s.snow_roads_changed()
        return changed

    def key(self, lat, lon, now):
        if self.cover is not None:
            _, clat, clon = cover_cell(lat, lon)
        else:
            clat, clon = weather.cell(lat, lon)
        return clat, clon, int(now // 3600)

    def forcing(self, lat, lon):
        """The hourly records, cell centre and a separate wind direction series for the snowfield."""
        k = self.key(lat, lon, self.clock())
        with self.lock:
            hit = self.cache.get(k)
            ev = None if hit is not None else self.inflight.get(k)
            if hit is None and ev is None:
                self.inflight[k] = threading.Event()
        if hit is not None:
            return hit
        if ev is not None:
            ev.wait(2 * TIMEOUT_S)
            with self.lock:
                hit = self.cache.get(k)
            if hit is not None:
                return hit
            raise IOError("snow: the shared fetch for this cell failed")
        try:
            response = self.fetch(k[0], k[1], *self.window)
            recs, times, z_cell, _off = records(response)
            if not times:
                raise IOError("snow: no hourly data")
            directions = (response.get("hourly") or {}).get("wind_direction_10m") or ()
            hit = (recs, times, z_cell, k[0], k[1], directions)
            self.fetches += 1
            with self.lock:
                self.cache[k] = hit
                while len(self.cache) > CACHE_MAX:
                    self.cache.pop(next(iter(self.cache)))
        finally:
            with self.lock:
                self.inflight.pop(k).set()
        return hit

    def get(self, lat, lon, z_site, prev_bin=-1, raw=None):
        """The snowstate message at (lat, lon) with the ground z_site m above sea level (raw: see message)."""
        mode = self.mode
        if mode in PRESETS:
            return preset_message(mode, lat, lon, prev_bin, raw=raw, prev_banks=self.bank_bins)
        recs, times, z_cell, clat, clon, directions = self.forcing(lat, lon)
        now = self.clock()
        i = hour_index(times, now)
        prev_banks = self.bank_bins
        msg = message(recs, i, times[i], clat, clon, z_site, z_cell, regime_at(lat, lon), prev_bin, raw=raw,
                      winter=self.winter, prev_banks=prev_banks)
        if self.cover is not None and raw is not None and raw["bands"]:
            cell, _, _ = cover_cell(clat, clon)
            source = {}
            message(recs, i, times[i], clat, clon, z_cell, z_cell, regime_at(clat, clon), raw=source,
                    prev_banks=prev_banks)
            w = transport(times, directions, recs[:, F["F_WIND"]], recs[:, F["F_T2M"]], i)
            raw["cover_cell"], raw["cover_state"] = cell, (times[i], source, w)
        return msg


class Tracker:
    """Per connection: when to compute (a new UTC hour, a move of REFETCH_M, a climb of REZ_M), the last message (re-sent
    on hello) and the depth bin's hysteresis."""

    def __init__(self):
        self.last = None            # (lat, lon, z, hour)
        self.inflight = False
        self.retry_at = 0.0
        self.msg = None
        self.bin = -1

    def due(self, svc, lat, lon, z, now):
        if self.inflight or now < self.retry_at:
            return False
        if self.last is None:
            return True
        llat, llon, lz, lh = self.last
        return (int(now // 3600) != lh or abs(z - lz) >= REZ_M
                or weather.distance_m(llat, llon, lat, lon) > REFETCH_M)

    def on_pose(self, conn, frame, x, y, z):
        """earthd Connection._pose_ground: the player at (x, y, z) in its anchor's frame. Nothing without the service
        (BNE_WINTER off)."""
        svc = conn.s.snow
        if svc is None:
            return
        lat, lon, h = (float(v) for v in frame.enu_to_geodetic(x, y, z))
        now = svc.clock()
        if not (math.isfinite(lat) and math.isfinite(lon) and math.isfinite(h)) or not self.due(svc, lat, lon, h, now):
            return
        self.inflight = True
        zq = int(round(h / 10.0)) * 10

        async def run():
            msg, wait, raw = None, 0.0, {}
            try:
                msg = await conn.loop.run_in_executor(None, svc.get, lat, lon, zq, self.bin, raw)
            except Exception as e:      # network, HTTP, JSON, the rate limiter: keep the last state
                wait = getattr(e, "retry_after", 0.0) or 0.0
                (log.info if isinstance(e, netfetch.Offline) else log.warning)(
                    "snow state at %.3f, %.3f failed: %s: %s", lat, lon, type(e).__name__, e)
            finally:
                self.inflight = False
            if conn.snow is not self:
                return  # A live mode change replaced this tracker while it was computing.
            if msg is None:
                self.retry_at = svc.clock() + max(svc.retry_s, wait)
            else:
                self.last, self.msg, self.bin = (lat, lon, h, int(now // 3600)), msg, msg["ground"][1]
                conn.send(msg)
                svc.commit(conn, raw)
        conn.spawn(run())
