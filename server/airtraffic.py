"""Live air traffic: the real aircraft round the player from a public ADS-B network (the mod's
beamngEarth/airtraffic.lua draws them). Based on devo343's Live Air Traffic add-on, with permission (CREDITS.md):
its companion's fetching, without the companion (no process, window or local port of its own).

Off by default (the mod's setting airTraffic, with its own consent text); the mod asks with an 'air_traffic' message
every ~15 s while the feature is on and the player is in the world, and gets an 'air_traffic_result' back over its
usual connection. Only a COARSE point leaves the PC: the player's lat / lon snapped to a CELL_DEG grid (about 28 km),
with the search radius widened to cover the cell, so every player in a cell asks the same question (and the answer
is shared by every connection of this server for SLOT_S). The aircraft are then cut to the player's own range and
nearest first here, in Rust (earthfast.adsb_compact: parsing, the age filter, the 400 cap, the compact rows).

Sources, each through netfetch with its own strict rate and daily caps (netfetch.POLICIES), tried in this order:
adsb.lol first; airplanes.live and adsb.fi only while adsb.lol is slow, refuses or rate-limits (the user,
2026-11-27). A source that answered 429 / 403 or used up its day rests REST_LIMITED_S; one that timed out or failed
rests REST_SLOW_S.

Routes (where a flight is going): api.adsbdb.com by airline callsign, only with the mod's separate routes switch,
at most one lookup every ROUTE_GAP_S, kept in memory only for ROUTE_TTL_S and never written anywhere (adsbdb's terms:
its routes may not be copied into other databases)."""
import collections
import logging
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import dmath
import fast
import netfetch

log = logging.getLogger("earthd.airtraffic")

KERNEL = 647                    # earthfast.adsb_compact / adsbdb_route
# Test runs (the in-game checks, the frame-cost A/B): BNE_AIRTRAFFIC_TEST=on in the environment of the server that the
# tester starts turns the feature on in its game for that session (hello airTrafficTest; nothing saved, the player's
# own setting untouched). Only whoever starts the server can set it; no setting or message from elsewhere does.
TEST_ON = os.environ.get("BNE_AIRTRAFFIC_TEST", "").strip().lower() == "on"
CELL_DEG = 0.25                 # the query point's grid (~28 km north-south): the only position that leaves the PC
SLOT_S = 15.0                   # one answer per cell and radius this long, shared by every connection
RANGE_NM = 60                   # the mod's default search radius (nautical miles round the player)
RANGE_MIN_NM, RANGE_MAX_NM = 10, 150
MAX_QUERY_NM = 250              # the sources' own limit
MAX_AIRCRAFT = 400
MAX_BYTES = 4 << 20             # an answer larger than this is refused (a busy hub at 60 NM is ~0.3 MB)
REST_LIMITED_S = 600.0          # a source that said 429 / 403, or whose day is used up
REST_SLOW_S = 120.0             # a source that timed out or failed
CACHE_MAX = 16
NM_KM = 1.852

# (name, host, URL template, the credit shown in game and in CREDITS.md)
PROVIDERS = (
    ("adsb.lol", "api.adsb.lol", "https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{nm}",
     "Aircraft: adsb.lol (ODbL)"),
    ("airplanes.live", "api.airplanes.live", "https://api.airplanes.live/v2/point/{lat}/{lon}/{nm}",
     "Aircraft: airplanes.live"),
    ("adsb.fi", "opendata.adsb.fi", "https://opendata.adsb.fi/api/v2/lat/{lat}/lon/{lon}/dist/{nm}",
     "Aircraft: adsb.fi (https://adsb.fi)"),
)
CREDITS = {name: credit for name, _h, _u, credit in PROVIDERS}
ROUTE_CREDIT = "Routes: adsbdb.com"
ROUTE_URL = "https://api.adsbdb.com/v0/callsign/{cs}"
ROUTE_GAP_S = 2.0
ROUTE_TTL_S = 6 * 3600.0
ROUTES_MAX = 4000               # routes kept (memory only)
ROUTE_QUEUE_MAX = 200
WANT_FRESH_S = 40.0             # a queued lookup not wanted again within this long is dropped (no lookups after the
                                # player turned routes or the feature off, left the world or quit)
AIRLINE_CS = re.compile(r"^[A-Z]{3}[0-9][0-9A-Z]{0,4}$")

POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="airtraffic")


def snap(lat, lon):
    """The query point: (lat, lon) snapped to the CELL_DEG grid (rounded to its nodes)."""
    la = round(round(float(lat) / CELL_DEG) * CELL_DEG, 4)
    lo = round(round(float(lon) / CELL_DEG) * CELL_DEG, 4)
    if lo >= 180.0:
        lo -= 360.0
    return max(-90.0, min(90.0, la)), lo


def query_nm(range_nm, lat):
    """The radius asked of the source round the snapped point: the player's range plus the farthest a point of the
    cell is from its node (half the cell's diagonal), so the player's whole range is inside the answer."""
    half_lat_nm = CELL_DEG / 2 * 60.0
    half_lon_nm = CELL_DEG / 2 * 60.0 * dmath.cos(math.radians(min(89.0, abs(lat))))
    return int(min(MAX_QUERY_NM, math.ceil(range_nm + math.hypot(half_lat_nm, half_lon_nm))))


def clamp_range(nm):
    try:
        nm = float(nm)
    except (TypeError, ValueError):
        return RANGE_NM
    if not math.isfinite(nm):
        return RANGE_NM
    return int(max(RANGE_MIN_NM, min(RANGE_MAX_NM, nm)))


def provider_url(tmpl, lat, lon, nm):
    return tmpl.format(lat=f"{lat:.2f}", lon=f"{lon:.2f}", nm=int(nm))


def _limited(e):
    """A source's own refusal (429 / 403 / its day used up here), not our limiter making a request wait."""
    if isinstance(e, netfetch.BudgetExceeded):
        return True
    return getattr(e, "status", None) in (403, 429)


class Service:
    """The answers shared by every connection: one fetch per (cell, radius, slot), the sources' rest times. get()
    blocks (network): run it in POOL. fetch(url) -> bytes is injectable (tests: no network)."""

    def __init__(self, net=None, fetch=None, clock=time.monotonic, wall=time.time):
        self.net, self.clock, self.wall = net, clock, wall
        self._fetch = fetch
        self.cache = collections.OrderedDict()
        self.inflight = {}
        self.rest = {}              # provider name -> clock() it may be asked again
        self.lock = threading.Lock()
        self.fetches = collections.Counter()
        self.last_error = None

    def fetch(self, url):
        if self._fetch is not None:
            return self._fetch(url)
        net = self.net if self.net is not None else netfetch.default_fetcher()
        # (no build's input: a failure faults nothing, netfetch.stale_ok)
        with netfetch.stale_ok():
            return net.get(url, kind="demand", max_bytes=MAX_BYTES).body

    def key(self, cell, nm):
        return cell + (nm, int(self.wall() // SLOT_S))

    def get(self, lat, lon, range_nm):
        """(body bytes, provider name, the answer's wall time in ms) for the player at lat, lon; raises IOError when
        no source answered (the last error's text)."""
        cell = snap(lat, lon)
        nm = query_nm(range_nm, cell[0])
        k = self.key(cell, nm)
        with self.lock:
            hit = self.cache.get(k)
            ev = None if hit is not None else self.inflight.get(k)
            if hit is None and ev is None:
                self.inflight[k] = threading.Event()
        if hit is not None:
            return hit
        if ev is not None:              # another connection is asking for this cell: share its answer
            ev.wait(30.0)
            with self.lock:
                hit = self.cache.get(k)
            if hit is not None:
                return hit
            raise IOError(self.last_error or "the shared request for this cell failed")
        try:
            hit = self._ask(cell, nm)
            with self.lock:
                self.cache[k] = hit
                while len(self.cache) > CACHE_MAX:
                    self.cache.popitem(last=False)
            return hit
        finally:
            with self.lock:
                self.inflight.pop(k).set()

    def _ask(self, cell, nm):
        errors = []
        now = self.clock()
        for name, _host, tmpl, _credit in PROVIDERS:
            if self.rest.get(name, 0.0) > now:
                continue
            url = provider_url(tmpl, cell[0], cell[1], nm)
            try:
                body = self.fetch(url)
            except netfetch.RateLimited as e:
                if getattr(e, "status", None) is not None:      # (the source's own 429 / 503 Retry-After / 3xx)
                    self.rest[name] = self.clock() + max(REST_LIMITED_S, getattr(e, "retry_after", 0.0) or 0.0)
                    errors.append(f"{name}: rate limited (resting {REST_LIMITED_S / 60:.0f} min)")
                    continue
                # our own limiter: too soon after the last ask (a teleport to a new cell); not the source's fault,
                # and no reason to ask the next source: the mod asks again shortly
                errors.append(f"{name}: {e}")
                break
            except netfetch.BudgetExceeded as e:
                self.rest[name] = self.clock() + REST_LIMITED_S
                errors.append(f"{name}: {e}")
                continue
            except (netfetch.FetchError, OSError) as e:
                self.rest[name] = self.clock() + (REST_LIMITED_S if _limited(e) else REST_SLOW_S)
                errors.append(f"{name}: {type(e).__name__}: {e}")
                continue
            self.fetches[name] += 1
            self.last_error = None
            return bytes(body), name, self.wall() * 1000.0
        self.last_error = "; ".join(errors) or "every source is resting after refusing or failing"
        raise IOError(self.last_error)


class Routes:
    """Where airline flights are going (api.adsbdb.com), memory only (never saved: adsbdb's terms). want() queues
    callsigns; a background thread looks one up every ROUTE_GAP_S; known() is what is known now."""

    def __init__(self, net=None, fetch=None, clock=time.monotonic, gap_s=ROUTE_GAP_S, start=True):
        self.net, self.clock, self.gap_s = net, clock, gap_s
        self._fetch = fetch
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.known_ = collections.OrderedDict()    # callsign -> (expires, (o, d, dn) or None)
        self.queue = collections.deque()
        self.queued = set()
        self.wanted = {}            # callsign -> clock() it was last wanted (a lookup no answer wants any more is dropped)
        self.lookups = 0
        self.thread = None
        self.stopped = False
        self.start = start

    def fetch(self, url):
        if self._fetch is not None:
            return self._fetch(url)
        net = self.net if self.net is not None else netfetch.default_fetcher()
        with netfetch.stale_ok():
            return net.get(url, kind="demand", max_bytes=64 << 10).body

    def want(self, callsigns):
        now = self.clock()
        with self.lock:
            for cs in callsigns:
                if not isinstance(cs, str) or not AIRLINE_CS.match(cs):
                    continue
                self.wanted[cs] = now
                if cs in self.queued:
                    continue
                k = self.known_.get(cs)
                if k is not None and k[0] > now:
                    continue
                if len(self.queue) >= ROUTE_QUEUE_MAX:
                    break
                self.queue.append(cs)
                self.queued.add(cs)
            if self.queue and self.start and (self.thread is None or not self.thread.is_alive()):
                self.thread = threading.Thread(target=self._run, name="airtraffic-routes", daemon=True)
                self.thread.start()
            self.cond.notify()

    def known(self, cs):
        k = self.known_.get(cs)
        if k is None or k[0] <= self.clock():
            return None
        return k[1]

    def step(self):
        """One lookup (the background thread's turn; tests call it directly). True if one was made."""
        with self.lock:
            cs = None
            while self.queue:
                c = self.queue.popleft()
                self.queued.discard(c)
                if self.clock() - self.wanted.get(c, -1e18) <= WANT_FRESH_S:
                    cs = c
                    break
                self.wanted.pop(c, None)    # (not wanted any more: the player moved on or turned routes off)
            if cs is None:
                return False
        val, keep = None, True
        try:
            val = fast._ef.adsbdb_route(self.fetch(ROUTE_URL.format(cs=cs)))
        except netfetch.NotFound:
            val = None                  # (an unknown callsign: remembered as none)
        except (netfetch.FetchError, OSError, ValueError) as e:
            keep = False                # (not known yet: asked again when it is wanted again)
            log.debug("route %s: %s: %s", cs, type(e).__name__, e)
        with self.lock:
            self.lookups += 1
            if keep:
                self.known_[cs] = (self.clock() + ROUTE_TTL_S, tuple(val) if val else None)
                self.known_.move_to_end(cs)
                while len(self.known_) > ROUTES_MAX:
                    self.known_.popitem(last=False)
        return True

    def _run(self):
        while not self.stopped:
            with self.lock:
                if not self.queue:
                    self.cond.wait(30.0)
                    if not self.queue:
                        return
            self.step()
            time.sleep(self.gap_s)

    def clear(self, known=True):
        """Drop the queued lookups (and, with known, what is known)."""
        with self.lock:
            if known:
                self.known_.clear()
            self.queue.clear()
            self.queued.clear()
            self.wanted.clear()


def answer(svc, routes, msg):
    """The 'air_traffic_result' for one 'air_traffic' request {"id", "lat", "lon", "rangeNm", "routes"} (blocking:
    run it in POOL)."""
    fast.require(KERNEL, "live air traffic")
    out = {"type": "air_traffic_result", "id": msg.get("id"), "ok": False}
    try:
        lat, lon = float(msg["lat"]), float(msg["lon"])
        if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("position out of range")
    except (KeyError, TypeError, ValueError) as e:
        out["error"] = f"bad request: {e}"
        return out
    range_nm = clamp_range(msg.get("rangeNm"))
    if msg.get("routes") is not True and routes is not None:
        routes.clear(known=False)       # (routes off: no lookup left queued)
    try:
        body, src, fetched_ms = svc.get(lat, lon, range_nm)
        now_ms, rows, read = fast._ef.adsb_compact(body, lat, lon, range_nm * NM_KM, MAX_AIRCRAFT, fetched_ms)
    except (IOError, ValueError) as e:
        out["error"] = str(e)[:300]
        return out
    credits = [CREDITS[src]]
    if msg.get("routes") is True and routes is not None:
        # the callsigns to look up: those of the whole cell's answer (what every player of the cell gets), in callsign
        # order: which flights are looked up first, or at all, never tells adsbdb where in the cell the player is
        cell = snap(lat, lon)
        _n, cell_rows, _r = fast._ef.adsb_compact(body, cell[0], cell[1], query_nm(range_nm, cell[0]) * NM_KM,
                                                  MAX_AIRCRAFT, fetched_ms)
        routes.want(sorted({r["f"] for r in cell_rows if r.get("f")}))
        for r in rows:
            k = routes.known(r["f"]) if r.get("f") else None
            if k:
                r["o"], r["d"], r["dn"] = k
        credits.append(ROUTE_CREDIT)
    # (srvNow: this server's clock as it answers, UTC ms: the mod dates each row by srvNow - ts, so a shared answer
    # up to SLOT_S old is still placed right)
    out.update(ok=True, src=src, credit="; ".join(credits), now=now_ms, srvNow=svc.wall() * 1000.0,
               rangeNm=range_nm, read=read, ac=rows)
    return out
