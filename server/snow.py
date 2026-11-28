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
NATIVE_RELIEF = 644         # earthfast: snow_band_table_relief

CELL_DEG = weather.CELL_DEG     # the forcing's cell (~11 km): the weather's
CACHE_MAX = 64
REFETCH_M = 5000.0          # a new state after this far ...
REZ_M = 25.0                # ... or this much higher / lower (the snow line) ...
RETRY_S = 300.0             # ... or after a failed fetch: at most this long, ...
RETRY_FAST_S = 15.0         # ... from this, doubling (netfetch's background requests: one per 10 s, dropped while a
                            # teleport loads: Pikes Peak's first state waited the whole 300 s, 2026-11-27)
TIMEOUT_S = weather.TIMEOUT_S
# A teleport: the player this far from its last pose AND from the place of the shared table (its state's site): the
# table is that other place's, dropped until this place's state is in (no snow rather than another place's snow: Valdez,
# Alaska's deep snow drawn over Pikes Peak for 5 min, 2026-11-27). A rebase or a short hop keeps it.
NEW_PLACE_M = 50000.0
# The cell's real relief (our DEM) the band table is cut to (snow.rs band_table_relief): the highest of a grid of
# samples over the forcing cell at this zoom (512 px tiles: ~150 m pixels at the equator)
RELIEF_ZOOM = 9
RELIEF_N = 41               # samples a side (~280 m apart over a 0.1-degree cell)
RELIEF_MAX = 512            # cells kept

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
    ground's cover a P0..P4 road keeps (by its condition). raw["relief"] (the cell's highest ground, m: Service's
    cell_relief), when there: no row above it (snow.rs band_table_relief: the kernel's bands past the cell's real
    ground are extrapolated, and the photos hold the top row for every higher mountain in view)."""
    bands = [list(b) for b in raw["bands"]]
    relief = raw.get("relief")
    if relief is None:
        rows, road = fast._ef.snow_band_table(bands, list(prev_bins), raw["conds"])
        bins = [int(r[2]) for r in rows]
    else:
        fast.require(NATIVE_RELIEF, "the snow bands cut to the relief")
        rows, road, bins = fast._ef.snow_band_table_relief(bands, list(prev_bins), raw["conds"], int(relief))
        bins = [int(b) for b in bins]
    rows = tuple(tuple(int(v) for v in r) for r in rows)
    look = raw.get("look")
    if look:        # (W3: the lanes', wheel tracks' and edges' shares per class)
        road = tuple(int(look["shares"][c][z]) for z in range(3) for c in range(5))
    return rows, tuple(int(v) for v in road), bins


def cell_relief(dem, lat, lon):
    """The highest ground (m, rounded up) of the forcing cell centred on (lat, lon) in our DEM (dem.Dem): the highest
    of RELIEF_N x RELIEF_N samples at RELIEF_ZOOM over it (snow.rs RELIEF_MARGIN_M covers the summits the grid
    misses), or None (no finite height). Blocks (the DEM's tiles): run it in an executor."""
    h = CELL_DEG / 2.0
    t = np.linspace(-h, h, RELIEF_N)
    la, lo = np.meshgrid(np.clip(lat + t, -89.9, 89.9), ((lon + t) + 180.0) % 360.0 - 180.0, indexing="ij")
    z = np.asarray(dem.sample(la, lo, RELIEF_ZOOM), np.float64)
    z = z[np.isfinite(z)]
    return int(math.ceil(float(z.max()))) if z.size else None


def _row_text(r):
    return "none" if r is None else f"{r[0]}/8 D{r[1]} a{r[2]}"


def table_change(old, new, old_view, view):
    """What changed between two shared tables ((rows, road, ice) or None) and the player's own views (the grass): the
    log line's text (the plan's 14:53:44 re-texture of the whole world while driving down, its cause unlogged)."""
    parts = []
    if old is None:
        parts.append("a first table for this place")
    else:
        if old[0] != new[0]:
            a = {r[0]: r[1:] for r in old[0]}
            b = {r[0]: r[1:] for r in new[0]}
            parts.append("bands " + ", ".join(f"{z} m {_row_text(a.get(z))} -> {_row_text(b.get(z))}"
                                              for z in sorted(set(a) | set(b)) if a.get(z) != b.get(z)))
        if tuple(old[1]) != tuple(new[1]):
            parts.append(f"road shares {list(old[1])} -> {list(new[1])}")
        if old[2] != new[2]:
            parts.append(f"lake ice {old[2]} -> {new[2]}")
    ov, nv = old_view or {}, view or {}
    if (ov.get("cover"), ov.get("depth")) != (nv.get("cover"), nv.get("depth")):
        parts.append(f"grass (the player's cover) {ov.get('cover')}/8 D{ov.get('depth')} -> "
                     f"{nv.get('cover')}/8 D{nv.get('depth')}")
    return "; ".join(parts) or "nothing"


def _snowcover():
    """W2's snowcover module when it is there, on and in its real mode (the provider's consumer), else None."""
    try:
        import snowcover
    except ImportError:
        return None
    return snowcover if getattr(snowcover, "ON", False) and getattr(snowcover, "MODE", "") == "real" else None


def _drawn(t):
    """A shared table (rows, road, ice) as the photos draw it: None when there is none or nothing white in it."""
    return t if t is not None and (any(r[1] > 0 for r in t[0]) or t[2] > 0) else None


class Service:
    """The hourly forcing shared by every connection: fetch (injectable: tests pass a fake) + the (cell, UTC hour)
    cache, one fetch in flight per key. get() blocks: run it in an executor.

    For W2's snow on the ground photos (snowcover.py, BNE_SNOW=real): the band table of the last state computed (any
    connection's) is the provider's Field, one table everywhere (Open-Meteo stage A fetches the player's cell only;
    the band rows carry the elevation; W5's pack will give tables by cell). The table is quantized (depth bins with
    hysteresis, age classes): a change is a new key for every texture, so commit() tells the server (snow_changed)
    only when the table itself changed, and logs what in it did (table_change).

    The table belongs to the place its state was computed at (site): a teleport far from it (Tracker: NEW_PLACE_M)
    drops it (leave) until the new place's state is in, so no place is drawn with another's snow. relief(lat, lon):
    the highest ground (m) of the forcing cell centred there (earthd: cell_relief over the server's DEM), the band
    table cut to it (w2_table); None (or failing): the kernel's bands as they are."""

    def __init__(self, fetch=None, mode=None, clock=time.time, retry_s=RETRY_S, cover=None, winter=None, relief=None,
                 retry_fast_s=RETRY_FAST_S):
        self.fetch = fetch if fetch is not None else weather.fetch_hourly
        self.mode = mode or MODE or "real"
        self.winter = (WINTER or self.mode in PRESETS) if winter is None else bool(winter)
        self.window = WINDOW_WINTER if self.winter else WINDOW_WET
        self.clock, self.retry_s, self.retry_fast_s = clock, retry_s, retry_fast_s
        self.relief = relief
        self.reliefs = {}           # forcing cell -> its highest ground m (relief's, kept)
        self.cache, self.inflight = {}, {}
        self.lock = threading.Lock()
        self.fetches = 0
        self.table = None           # (rows, road, ice) of W2's Table, the provider's
        self.site = None            # (lat, lon) of the state behind the table: its place
        self.view = None            # {mode, cover, depth}: winterlook.lua's ('snow' message)
        self.bins = [-1] * 8
        self.bank_bins = [-1] * 5    # W3's bank bins (hysteresis)
        self.road_snow = None        # W3: the road skins' winter part (snowroad.wear_snow; Server.wear_arg)
        self.ice = -1
        self.table_changes = 0
        self.places_left = 0
        self.cover = (cover if cover is not None else _snowcover()) if self.winter else None
        if self.cover is not None:
            # (a preset's provider draws the climatic permanent snow too, as BNE_SNOW's presets; the real state never:
            # snow only where its forcing put it)
            self.cover.set_provider(self.provider, climate=self.mode in PRESETS)

    def provider(self, south, west, north, east):
        """snowcover's provider: the last table as a uniform Field (None while there is none or no cover in it)."""
        return self.field_of(self.table)

    def field_of(self, t):
        """A shared table (rows, road, ice) as the provider's uniform Field (None: none, or nothing white in it)."""
        t = _drawn(t)
        if t is None or self.cover is None:
            return None
        try:
            table = self.cover.Table(t[0], t[1], t[2])
        except TypeError:           # (a snowcover before Table.ice)
            table = self.cover.Table(t[0], t[1])
        return self.cover.Field(CELL_DEG, uniform=table)

    def _changed(self, s, old, new):
        """Server.snow_changed with the provider's Fields before and after: only the textures whose look changed are
        made again (snowcover.look_changes)."""
        try:
            fields = (self.field_of(old), self.field_of(new))
        except Exception as e:      # (a cover without Field: every texture, as before)
            log.debug("snow: no fields for the change: %s: %s", type(e).__name__, e)
            fields = None
        return s.snow_changed(fields=fields)

    def backoff(self, fails):
        """Seconds before another try after `fails` failed fetches in a row: retry_fast_s doubling, at most retry_s."""
        return min(float(self.retry_s), float(self.retry_fast_s) * 2.0 ** max(0, int(fails) - 1))

    def commit(self, conn, raw):
        """(event loop) The W2 table of a new state: when it differs from the last, the provider's Field changes, the
        server re-renders what is shown (Server.snow_changed) and every connection gets {"type": "snow"}."""
        if raw is not None and raw.get("look") is not None:
            Service._commit_roads(self, conn, raw["look"])
        if self.cover is None or raw is None or not raw.get("bands"):
            return False
        with self.lock:
            rows, road, bins = w2_table(raw, self.bins)
            self.bins = bins
            lake = raw.get("lake") or [0, 0, 0]
            self.ice = int(fast._ef.snow_lake_share(int(lake[1]), self.ice))
            g = raw["ground"]
            view = {"mode": "real", "cover": int(g[9]), "depth": int(g[8])}
            old, old_view = self.table, self.view
            changed = (rows, road, self.ice) != old
            self.table, self.view = (rows, road, self.ice), view
            site = raw.get("site")
            if site is not None:
                self.site = (float(site[0]), float(site[1]))
        if changed:
            self.table_changes += 1
            drawn = _drawn(old) != _drawn(self.table)
            if site is not None:
                log.info("snow table changed at %.3f, %.3f, %d m (cell ground to %s m)%s: %s", site[0], site[1],
                         site[2], raw.get("relief"), "" if drawn else " (no snow drawn before or after)",
                         table_change(old, self.table, old_view, view))
            s = conn.s
            if drawn and hasattr(s, "snow_changed"):     # (no snow before and after: nothing drawn changes)
                Service._changed(self, s, old, self.table)
            for c in list(getattr(s, "connections", ()) or ()):
                c.send({"type": "snow", "snow": dict(view)})
        return changed

    def leave(self, conn, lat, lon):
        """(event loop) A connection's player arrived at (lat, lon) far from its last pose (Tracker: a teleport). When
        the shared table's place is far from there too (NEW_PLACE_M), the table, its hysteresis, the player's view and
        the road skins' winter part are dropped until this place's state is in: no snow rather than another place's.
        What was drawn with snow is drawn again without (Server.snow_changed, 'snow' with no cover; the road tiles:
        snow_roads_changed). A preset is the same everywhere: kept. True when a table went."""
        if self.mode in PRESETS:
            return False
        with self.lock:
            site = self.site
            if self.table is None and self.road_snow is None:
                self.site = None
                return False
            if site is not None and weather.distance_m(site[0], site[1], lat, lon) <= NEW_PLACE_M:
                return False
            had = _drawn(self.table) is not None
            old = self.table
            roads = self.road_snow is not None
            self.table, self.view, self.site = None, None, None
            self.bins, self.ice, self.bank_bins, self.road_snow = [-1] * 8, -1, [-1] * 5, None
            self.places_left += 1
        log.info("snow: a new place at %.3f, %.3f (the last place's table %s): dropped until this one's state is in",
                 lat, lon, "had snow" if had else "had none")
        s = conn.s
        if had:
            if hasattr(s, "snow_changed"):
                Service._changed(self, s, old, None)
            for c in list(getattr(s, "connections", ()) or ()):
                c.send({"type": "snow", "snow": {"mode": "real", "cover": 0, "depth": 0}})
        if roads and hasattr(s, "snow_roads_changed"):
            s.snow_roads_changed()
        return True

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
        return weather.cell(lat, lon) + (int(now // 3600),)

    def relief_at(self, clat, clon):
        """The highest ground (m) of the forcing cell centred on (clat, clon) (relief's, kept per cell), or None (no
        relief function, or it failed: asked again next time)."""
        if self.relief is None:
            return None
        k = (clat, clon)
        with self.lock:
            if k in self.reliefs:
                return self.reliefs[k]
        try:
            z = self.relief(clat, clon)
        except Exception as e:      # (the DEM's tiles unavailable: the bands uncut this time)
            log.info("snow: no relief for the cell at %.2f, %.2f: %s: %s", clat, clon, type(e).__name__, e)
            return None
        if z is None:
            return None
        with self.lock:
            self.reliefs[k] = int(z)
            while len(self.reliefs) > RELIEF_MAX:
                self.reliefs.pop(next(iter(self.reliefs)))
        return int(z)

    def forcing(self, lat, lon):
        """(records, times, z_cell, cell lat, cell lon) at (lat, lon) for this UTC hour."""
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
            recs, times, z_cell, _off = records(self.fetch(k[0], k[1], *self.window))
            if not times:
                raise IOError("snow: no hourly data")
            hit = (recs, times, z_cell, k[0], k[1])
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
        """The snowstate message at (lat, lon) with the ground z_site m above sea level (raw: see message; it gets the
        site too, and the cell's relief for the band table)."""
        if self.mode in PRESETS:
            return preset_message(self.mode, lat, lon, prev_bin, raw=raw, prev_banks=self.bank_bins)
        recs, times, z_cell, clat, clon = self.forcing(lat, lon)
        now = self.clock()
        i = hour_index(times, now)
        msg = message(recs, i, times[i], clat, clon, z_site, z_cell, regime_at(lat, lon), prev_bin, raw=raw,
                      winter=self.winter, prev_banks=self.bank_bins)
        if raw is not None:
            raw["site"] = (float(lat), float(lon), int(z_site))
            if self.winter and self.cover is not None:
                raw["relief"] = self.relief_at(clat, clon)
        return msg


class Tracker:
    """Per connection: when to compute (a new UTC hour, a move of REFETCH_M, a climb of REZ_M), the last message (re-sent
    on hello), the depth bin's hysteresis, the retry's backoff after failed fetches (Service.backoff) and the place: a
    jump of more than NEW_PLACE_M between two poses (a teleport), or a first pose far from the table's place, is a new
    place (Service.leave) whose state is asked for at once; a state still being fetched for the old place is dropped
    when it comes."""

    def __init__(self):
        self.last = None            # (lat, lon, z, hour)
        self.inflight = False
        self.retry_at = 0.0
        self.fails = 0              # failed fetches in a row
        self.msg = None
        self.bin = -1
        self.pose = None            # (lat, lon) of the last pose
        self.epoch = 0              # the place's number: a teleport makes a new one

    def due(self, svc, lat, lon, z, now):
        if self.inflight or now < self.retry_at:
            return False
        if self.last is None:
            return True
        llat, llon, lz, lh = self.last
        return (int(now // 3600) != lh or abs(z - lz) >= REZ_M
                or weather.distance_m(llat, llon, lat, lon) > REFETCH_M)

    def arrive(self, conn, svc, lat, lon):
        """The player at (lat, lon): True (the last place's state dropped, this one's asked for at once) when that is
        more than NEW_PLACE_M from the last pose."""
        p, self.pose = self.pose, (lat, lon)
        if p is None:
            # (this connection's first pose: a game that started elsewhere than the server's last place, "Where you
            # left off": the table goes when its place is far from here)
            svc.leave(conn, lat, lon)
            return False
        if weather.distance_m(p[0], p[1], lat, lon) <= NEW_PLACE_M:
            return False
        self.epoch += 1
        self.last, self.msg, self.bin = None, None, -1
        self.inflight, self.retry_at, self.fails = False, 0.0, 0
        svc.leave(conn, lat, lon)
        return True

    def on_pose(self, conn, frame, x, y, z):
        """earthd Connection._pose_ground: the player at (x, y, z) in its anchor's frame. Nothing without the service
        (BNE_WINTER off)."""
        svc = conn.s.snow
        if svc is None:
            return
        lat, lon, h = (float(v) for v in frame.enu_to_geodetic(x, y, z))
        if not (math.isfinite(lat) and math.isfinite(lon) and math.isfinite(h)):
            return
        self.arrive(conn, svc, lat, lon)
        now = svc.clock()
        if not self.due(svc, lat, lon, h, now):
            return
        self.inflight = True
        zq = int(round(h / 10.0)) * 10
        epoch = self.epoch

        async def run():
            msg, wait, raw, current = None, 0.0, {}, False
            try:
                msg = await conn.loop.run_in_executor(None, svc.get, lat, lon, zq, self.bin, raw)
            except Exception as e:      # network, HTTP, JSON, the rate limiter: keep the last state
                wait = getattr(e, "retry_after", 0.0) or 0.0
                (log.info if isinstance(e, netfetch.Offline) else log.warning)(
                    "snow state at %.3f, %.3f failed: %s: %s (again in %.0f s)", lat, lon, type(e).__name__, e,
                    max(svc.backoff(self.fails + 1), wait))
            finally:
                current = epoch == self.epoch       # (else a teleport since: the old place's state, dropped)
                if current:
                    self.inflight = False
                    if msg is None:
                        self.fails += 1
                        self.retry_at = svc.clock() + max(svc.backoff(self.fails), wait)
                    else:
                        self.fails = 0
                        self.last, self.msg, self.bin = (lat, lon, h, int(now // 3600)), msg, msg["ground"][1]
            if msg is not None and current:
                conn.send(msg)
                svc.commit(conn, raw)
        conn.spawn(run())
