"""The one door to the network: per-host rate limits, priorities, retries and circuit breakers
(docs/research/2026-10-10-rate-limits.md §5).

Every request the server makes goes through a Fetcher (earthd builds one and hands it to the DEM,
the imagery, the weather and the gravity download). Per host (POLICIES, by URL host name; unknown
hosts get DEFAULT_POLICY):

  * a token bucket (rate tokens/s, burst) and a concurrency cap (a semaphore independent of the
    worker pools, so a pool of 8 never puts 8 requests on a host whose cap is 4);
  * priorities: kind="demand" (a texture or heightmap being built) waits for a token (up to
    max_wait); "prefetch" and "background" (de-shadow regions, weather) only take a token while
    the bucket is above PREFETCH_RESERVE / BACKGROUND_RESERVE of its burst and a request slot is
    free, and are dropped (Deferred), not queued, when refused: what is still needed is fetched
    later on demand;
  * retries of 429/5xx/network errors with exponential backoff and full jitter
    (uniform(0, min(max_delay, base_delay * 2**attempt)));
  * Retry-After (delta-seconds or an HTTP date, clamped to [1 s, 15 min]) on a 429/503 pauses the
    whole host: the bucket is emptied, the breaker opens for that long and the request raises
    RateLimited at once, so no other worker keeps hitting a host that said stop (demand requests
    already waiting for a token give up too: their chains fail over);
  * a circuit breaker: open after breaker_threshold failed attempts within breaker_window (or one
    Retry-After), then requests raise HostUnavailable without touching the network; after the open
    time one probe goes through (half-open): its success closes it (open time back to breaker_open;
    a late success of a request sent before it opened does not), failure re-opens it for twice as
    long (up to breaker_max). Callers fail over along their own chains (imagery usgs -> eox2023 ->
    eox2016 -> palette; DEM Mapterhorn -> AWS);
  * daily budgets (soft: at 80 % prefetch / background stop with one warning, at 100 % demand runs
    at half rate with a warning per hour; hard: BudgetExceeded), counted per UTC day and persisted
    in <stats_dir>/<YYYY-MM-DD>.json (every STATS_FLUSH_S and at close), so a restart keeps them;
  * response classes: 404/410 -> NotFound (the caller writes its .404 marker; miss_alive() gives the
    marker its policy TTL), a 3xx from a tile host -> rate limited (EOX redirects throttled clients
    to its home page), a 200 with an HTML page from a tile host -> BadResponse (not cached, counted
    as a failure), other 4xx -> FetchFailed (no marker: some servers answer 400 when overloaded).

--offline (Fetcher(offline=True)): every request raises Offline at once; the callers' disk caches
still serve what they have. `Fetcher.offline` is also true when every host of the registered
chains has its breaker open, and earthd then says "Offline - using cached data" in its status.

One User-Agent for the whole project (USER_AGENT): the app, its version and the project URL; no
user name, e-mail, machine name, location or session id is ever sent.
"""
import email.utils
import http.client
import json
import logging
import os
import random
import ssl
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from urllib.parse import urlsplit

log = logging.getLogger("earthd.net")

SERVER_VERSION = "0.4.0"
PROJECT_URL = "https://github.com/TecnaGamer/BeamNG.Earth"
USER_AGENT = f"BeamNG.Earth/{SERVER_VERSION} (+{PROJECT_URL})"
KINDS = ("demand", "prefetch", "background", "predownload", "urgent")
# "urgent": demand that goes first (the block the player waits on: its base photo). Demand takes a token only while
# URGENT_TOKENS more stay in the bucket and leaves URGENT_CONC request slots of its own to urgent, so with a host at
# its rate (a daily budget at half rate: GitHub #116, a teleport's first block waited 37 s behind the far field's
# hundreds of tile requests) the player's block still gets its tiles at once.
URGENT_TOKENS = 2.0
URGENT_CONC = 2
PREFETCH_RESERVE = 0.5          # prefetch takes a token only while the bucket holds more than this x burst
BACKGROUND_RESERVE = 0.75       # background (de-shadow, weather) only above this: it comes after prefetch
RETRY_AFTER_MIN, RETRY_AFTER_MAX = 1.0, 900.0
BUDGET_WARN = 0.8               # share of the soft daily budget where prefetch / background stop
BUDGET_REPEAT_S = 3600.0        # over budget: the warning repeats this often
STATS_FLUSH_S = 60.0
DAY = 86400.0
TOKEN_EPS = 1e-4                # a token short by rounding (clock resolution) counts as a token
OFFLINE_TEXT = "Offline - using cached data"

NETLOG = os.environ.get("BNGE_NETLOG")      # debugging: every network request, one line each, from every process


def _netlog(url, headers, status, body):
    """Append "<unix time> <pid> <status> <bytes> <url> <range>" to the BNGE_NETLOG file (one write per line:
    whole lines from every process)."""
    line = (f"{time.time():.3f} {os.getpid()} {status} {len(body or b'')} {url} "
            f"{(headers or {}).get('Range', '-')}\n")
    try:
        fd = os.open(NETLOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)
    except OSError:
        pass


@dataclass(frozen=True)
class HostPolicy:
    rate: float = 2.0                       # tokens per second (sustained requests/s)
    burst: float = 4.0                      # bucket size
    max_conc: int = 2                       # requests in flight at once
    daily_budget: int | None = 5000         # soft: warn at 80 % (prefetch stops) and 100 % (half rate)
    daily_hard: int | None = None           # hard: BudgetExceeded beyond it
    allow_prefetch: bool = False            # may be asked ahead of need
    interactive_only: bool = False          # only what the player is looking at (OSM-type hosts)
    offline_ok: bool = False                # may be pre-downloaded for offline play
    denied: bool = False                    # never contacted at runtime (PolicyDenied)
    cache_ttl: float | None = None          # s the caller may keep a tile (None = forever)
    revalidate_after: float | None = None   # s after which a cached tile should be revalidated (not used yet)
    miss_ttl: float | None = 30 * DAY       # s a .404 marker stays valid (None = forever)
    honor_cache_headers: bool = False
    retry_statuses: frozenset = frozenset({429, 500, 502, 503, 504})
    max_attempts: int = 3
    base_delay: float = 0.5                 # s, backoff base (full jitter)
    max_delay: float = 60.0
    breaker_threshold: int = 5              # failed attempts ...
    breaker_window: float = 30.0            # ... within this many seconds open the breaker
    breaker_open: float = 30.0              # s open, doubled after a failed probe ...
    breaker_max: float = 900.0              # ... up to this
    slow_ms: float | None = None            # a slower response counts as a failure (WMS / export hosts)
    timeout: float = 20.0                   # s per HTTP request
    max_wait: float = 30.0                  # s a demand request may wait for a token
    redirect_limited: bool = False          # a 3xx means "rate limited" (tile hosts never redirect)
    binary: bool = False                    # a 200 with an HTML page is an error page
    referer: bool = False                   # send PROJECT_URL as Referer (hosts that ask for one)
    optional: bool = False                  # the world builds without it (gravity, weather, search, routing,
                                            # updates): left out of the player's "Degraded" status (GitHub #23)


TILE_HOST = dict(redirect_limited=True, binary=True)
# docs/research/2026-10-10-rate-limits.md §5.2, the hosts the server contacts today (rates per player).
POLICIES = {
    # (2026-10-16, the user: raise them, lower again on trouble; a first visit's far field waited ~100 s on 15/s)
    # (2026-10-23, the user: raise it again: 24 at once; a PC already got ~26/s with 12 and the same far field with 24,
    # all answered, so it helps players far from USGS, whose every request waits longer)
    "basemap.nationalmap.gov": HostPolicy(rate=50.0, burst=150.0, max_conc=24, daily_budget=150000,
                                          allow_prefetch=True, offline_ok=True, miss_ttl=90 * DAY, **TILE_HOST),
    # NAIP through USGS's ArcGIS exportImage (imagery.py "naip"): one 1024² image a request, 4-10 s each
    # and 5xx under parallel requests (docs/research/2026-10-10-us-state-imagery.md, in-game 2026-10-13):
    # one at a time, only imagery.warm()'s look-ahead (never on a render's critical path)
    "imagery.nationalmap.gov": HostPolicy(rate=0.5, burst=4.0, max_conc=1, daily_budget=3000,
                                          allow_prefetch=True, offline_ok=True, miss_ttl=90 * DAY,
                                          binary=True, timeout=40.0, max_attempts=2, base_delay=2.0,
                                          breaker_threshold=6, breaker_window=120.0),
    # USDA FPAC's NAIP ImageServer (imagery.py "naipusda"): 1.6-4 s a 1024² image in-game 2026-10-13
    "apps.geo.fpac.usda.gov": HostPolicy(rate=1.0, burst=4.0, max_conc=1, daily_budget=4000,
                                         allow_prefetch=True, offline_ok=True, miss_ttl=90 * DAY,
                                         binary=True, timeout=30.0, max_attempts=2, base_delay=2.0,
                                         breaker_threshold=6, breaker_window=120.0),
    # MD iMAP six-inch MapServer export (imagery.py "md6in"): ~1.3 s a 1024² image in-game 2026-10-13
    "mdgeodata.md.gov": HostPolicy(rate=1.0, burst=4.0, max_conc=1, daily_budget=4000,
                                   allow_prefetch=True, offline_ok=True, miss_ttl=90 * DAY,
                                   binary=True, timeout=30.0, max_attempts=2, base_delay=2.0,
                                   breaker_threshold=6, breaker_window=120.0),
    # NOAA NCEI's DEM global mosaic (bathymetry for the sea floor, seacarve.py): one export per map-data cell
    "gis.ngdc.noaa.gov": HostPolicy(rate=1.0, burst=4.0, max_conc=2, daily_budget=4000,
                                    allow_prefetch=True, offline_ok=True, miss_ttl=30 * DAY,
                                    binary=True, timeout=60.0, max_attempts=2, base_delay=2.0,
                                    breaker_threshold=6, breaker_window=120.0),
    "tiles.maps.eox.at": HostPolicy(rate=10.0, burst=40.0, max_conc=6, daily_budget=30000,
                                    allow_prefetch=True, offline_ok=True, miss_ttl=90 * DAY, **TILE_HOST),
    # (a tile not in Cloudflare's edge cache takes ~1 s before its first byte: requests in flight set the rate)
    "tiles.mapterhorn.com": HostPolicy(rate=50.0, burst=200.0, max_conc=32, daily_budget=80000,
                                       allow_prefetch=True, offline_ok=True, revalidate_after=180 * DAY,
                                       miss_ttl=90 * DAY, **TILE_HOST),
    "s3.amazonaws.com": HostPolicy(rate=50.0, burst=100.0, max_conc=8, daily_budget=None,
                                   allow_prefetch=True, offline_ok=True, miss_ttl=None, **TILE_HOST),
    # BeamNG.Earth's own data on GitHub Pages (railtags.py: the weekly OSM rail shards, ~300 km each, kept on disk)
    "tecnagamer.github.io": HostPolicy(rate=4.0, burst=8.0, max_conc=2, daily_budget=2000, allow_prefetch=True,
                                       offline_ok=True, miss_ttl=7 * DAY, timeout=20.0, max_attempts=2,
                                       base_delay=2.0, breaker_threshold=6, breaker_window=120.0, optional=True),
    # 20 variables a request = 2 Open-Meteo calls: 500 requests = 1,000 calls a day, hard
    "api.open-meteo.com": HostPolicy(rate=0.1, burst=2.0, max_conc=1, daily_budget=500, daily_hard=500,
                                     cache_ttl=900.0, miss_ttl=None, max_wait=15.0, timeout=10.0, optional=True),
    # the EGM2008 coefficients (74 MB once; gravity.py resumes and checks the SHA-256 itself)
    "downloads.sourceforge.net": HostPolicy(rate=1.0, burst=1.0, max_conc=1, daily_budget=None,
                                            offline_ok=True, miss_ttl=None, max_attempts=1, timeout=30.0, optional=True),
    # map data (mapdata.py; §3.3 / §5.2 "Overture / WorldCover / CHM S3, Azure": 20/s, 40, 4 at once):
    # range reads of immutable release files, kept forever; the STAC index is small JSON on CloudFront
    "stac.overturemaps.org": HostPolicy(rate=5.0, burst=10.0, max_conc=2, daily_budget=5000,
                                        allow_prefetch=True, offline_ok=True, timeout=20.0),
    **{h: HostPolicy(rate=20.0, burst=40.0, max_conc=4, daily_budget=None, allow_prefetch=True,
                     offline_ok=True, miss_ttl=None, binary=True, timeout=30.0)
       for h in ("overturemaps-us-west-2.s3.us-west-2.amazonaws.com",
                 "overturemapswestus2.blob.core.windows.net",
                 "esa-worldcover.s3.eu-central-1.amazonaws.com",
                 "dataforgood-fb-data.s3.amazonaws.com",
                 "copernicus-dem-30m.s3.amazonaws.com")},      # the polar caps' DEM (polardem.py)
    # OurAirports' nightly CSV dump on GitHub Pages (airports.py): two files (~3 MB gzipped) once a week,
    # one request at a time, a hard cap well above a week's need; the copy on disk serves offline
    "davidmegginson.github.io": HostPolicy(rate=0.2, burst=2.0, max_conc=1, daily_budget=20, daily_hard=40,
                                           offline_ok=True, miss_ttl=None, binary=True, timeout=120.0,
                                           max_attempts=2, base_delay=5.0),
    # Photon (komoot's OpenStreetMap geocoder, made for search-as-you-type; geocode.py): the map's search box,
    # debounced and cached; a modest rate, a daily cap (fair use of a free public service)
    "photon.komoot.io": HostPolicy(rate=2.0, burst=4.0, max_conc=1, daily_budget=2000, daily_hard=3000,
                                   cache_ttl=7 * DAY, miss_ttl=None, max_wait=5.0, timeout=12.0, max_attempts=1, optional=True),
    # OSRM on OpenStreetMap by FOSSGIS (routing.py): one request per GPS destination or re-route; their fair use:
    # ~1 request a second at most, no bulk
    "routing.openstreetmap.de": HostPolicy(rate=0.5, burst=2.0, max_conc=1, daily_budget=300, daily_hard=500,
                                           cache_ttl=DAY, miss_ttl=None, max_wait=10.0, timeout=20.0, max_attempts=1, optional=True),
    # the BeamNG.Earth server list (native/serverlist on the project's mini PC): the tab in BeamMP's server browser,
    # fetched when the player opens it or presses Refresh
    "earth.tgforever.net": HostPolicy(rate=0.2, burst=3.0, max_conc=1, daily_budget=3000, daily_hard=6000,
                                      cache_ttl=0, miss_ttl=None, max_wait=5.0, timeout=8.0, max_attempts=1,
                                      optional=True),
    # the update check (updates.py): the project's VERSION file on GitHub, a few times a day at most
    "raw.githubusercontent.com": HostPolicy(rate=0.1, burst=1.0, max_conc=1, daily_budget=24, daily_hard=48,
                                            cache_ttl=6 * 3600, miss_ttl=None, max_wait=5.0, timeout=8.0,
                                            max_attempts=1, optional=True),
    # the one-click update (selfupdate.py, the player's button): the latest release's details, then its file (a
    # redirect from github.com to GitHub's asset host, followed by urllib inside the session)
    "api.github.com": HostPolicy(rate=0.2, burst=2.0, max_conc=1, daily_budget=20, daily_hard=40, max_wait=10.0,
                                 timeout=30.0, max_attempts=1, optional=True),
    "github.com": HostPolicy(rate=0.2, burst=2.0, max_conc=1, daily_budget=10, daily_hard=20, max_wait=10.0,
                             timeout=60.0, max_attempts=1, optional=True),
    # OSM US Layercake (osmtags.py): the OSM highways GeoParquet on Cloudflare, read in ranges (the footer once, a few
    # MB per new area); optional: without it roads keep their width guesses
    "data.openstreetmap.us": HostPolicy(rate=4.0, burst=8.0, max_conc=2, daily_budget=5000, allow_prefetch=True,
                                        offline_ok=True, miss_ttl=None, binary=True, timeout=30.0, optional=True),
    # never at runtime (OSM Overpass, OSMF vector tiles, Protomaps builds): §3.3
    "overpass-api.de": HostPolicy(denied=True, daily_hard=0),
    "vector.openstreetmap.org": HostPolicy(denied=True, daily_hard=0),
    "build.protomaps.com": HostPolicy(denied=True, daily_hard=0),
}
# Any other host: the conservative WMS-type row of §5.2 (2/s, 2 at once, no prefetch).
# Regional orthophoto services (imagery_regional.py): tile caches at a modest shared rate, exports one at a time.
# Optional: the global imagery stands in while one is down (Lower Saxony's WMS answered a maintenance page for hours,
# 2026-10-21, and the banner named it while Sentinel-2 drew every tile); tiles left without imagery still say so
def _regional_policies():
    import urllib.parse
    import imagery_regional
    out = {}
    for url, _attr, _bounds, kind, _leaf in imagery_regional.REGIONAL.values():
        host = urllib.parse.urlsplit(url).hostname
        if host in POLICIES or host in out:
            continue
        if kind == "xyz":
            out[host] = HostPolicy(rate=6.0, burst=20.0, max_conc=4, daily_budget=20000, allow_prefetch=True,
                                   miss_ttl=30 * DAY, timeout=20.0, optional=True, **TILE_HOST)
        else:
            out[host] = HostPolicy(rate=1.0, burst=4.0, max_conc=1, daily_budget=4000, allow_prefetch=True,
                                   miss_ttl=30 * DAY, binary=True, timeout=30.0, max_attempts=2, base_delay=2.0,
                                   breaker_threshold=6, breaker_window=120.0, optional=True)
    return out


POLICIES.update(_regional_policies())
DEFAULT_POLICY = HostPolicy()

# What each source gives the world, and what the player gets while it does not answer (the user, 2026-10-27: "in case
# a certain source is not responding ... tell the player what that source effects"). Shown after the host in the
# status line (Server.net_note); SOURCES.md says more about each.
ROLES = {
    "basemap.nationalmap.gov": "US aerial photos; coarser satellite photos meanwhile",
    "imagery.nationalmap.gov": "detailed US aerial photos (NAIP); coarser photos meanwhile",
    "apps.geo.fpac.usda.gov": "detailed US aerial photos (NAIP, second source); coarser photos meanwhile",
    "mdgeodata.md.gov": "Maryland's 6-inch aerial photos; coarser photos meanwhile",
    "tiles.maps.eox.at": "worldwide satellite photos; new ground outside the detailed-photo regions may stay plain",
    "tiles.mapterhorn.com": "terrain heights; new places use the coarser backup meanwhile",
    "download.mapterhorn.com": "the list of where detailed terrain heights exist; new places may load slower",
    "s3.amazonaws.com": "terrain heights (backup source); only matters while the main one is down too",
    "copernicus-dem-30m.s3.amazonaws.com": "terrain heights near the poles",
    "stac.overturemaps.org": "the map data's file list; the copy that comes with the app is used meanwhile",
    "overturemaps-us-west-2.s3.us-west-2.amazonaws.com":
        "roads, buildings, water and land use; places not visited before may come without them",
    "overturemapswestus2.blob.core.windows.net":
        "roads, buildings, water and land use (second source); only matters while the first is down too",
    "esa-worldcover.s3.eu-central-1.amazonaws.com":
        "land cover (ground materials, rocks, town streets); places not visited before get a plain ground meanwhile",
    "dataforgood-fb-data.s3.amazonaws.com": "the trees; places not visited before have none meanwhile",
    "gis.ngdc.noaa.gov": "sea depths; new coasts get a plain sloping sea floor meanwhile",
    "davidmegginson.github.io": "the airport list; the copy on disk is used meanwhile",
    "data.openstreetmap.us": "road details from OpenStreetMap (lanes, widths, sidewalks); estimated meanwhile",
    "tecnagamer.github.io": "railway details and US railroad crossing equipment; estimated meanwhile",
    "api.open-meteo.com": "live weather; the weather stays as it is meanwhile",
    "downloads.sourceforge.net": "the gravity model; standard gravity meanwhile",
    "photon.komoot.io": "the map's address search",
    "routing.openstreetmap.de": "GPS routes",
    "earth.tgforever.net": "the BeamNG.Earth server list in BeamMP",
    "raw.githubusercontent.com": "the update check",
    "api.github.com": "one-click updates",
    "github.com": "one-click updates",
}


def role_of(host):
    """What host gives the world and what is missing while it does not answer (ROLES; a regional photo source by its
    place), or None for a host not known here."""
    host = host.split("#", 1)[0]
    got = ROLES.get(host)
    if got is not None:
        return got
    import urllib.parse
    import imagery_regional
    for name, (url, *_rest) in imagery_regional.REGIONAL.items():
        if urllib.parse.urlsplit(url).hostname == host:
            return f"{imagery_regional.PLACE.get(name, name)}'s detailed aerial photos; coarser photos meanwhile"
    return None


# --- errors ------------------------------------------------------------------------------------
class FetchError(IOError):
    """A request that did not give data. retry_after: s before asking this host again helps."""

    def __init__(self, msg, url=None, host=None, retry_after=0.0, status=None):
        super().__init__(msg)
        self.url, self.host, self.retry_after, self.status = url, host, retry_after, status
        if not isinstance(self, (NotFound, Deferred)):     # (no data there, a dropped prefetch: as it should be)
            import revisit
            revisit.fault()             # (a build that goes on without it is not kept)


class NotFound(FetchError):
    """404 / 410: the host has no data there (the caller writes a .404 marker)."""


class RateLimited(FetchError):
    """429 / 3xx from a tile host / Retry-After, or a demand request that would wait > max_wait."""


class HostUnavailable(FetchError):
    """The host's circuit breaker is open: no network request was made."""


class Offline(HostUnavailable):
    """--offline: only the disk caches serve."""


class BudgetExceeded(FetchError):
    """The host's daily budget is used up (hard), or prefetch / background past 80 % of the soft one."""


class PolicyDenied(FetchError):
    """The host's policy forbids this request (never at runtime, interactive only, no prefetch)."""


class Deferred(FetchError):
    """A prefetch / background request refused for lack of tokens or a free slot (dropped, not queued)."""


class FetchFailed(FetchError):
    """Network errors or bad statuses after every attempt."""


class BadResponse(FetchFailed):
    """A 200 that is not the data (an HTML error page from a tile host)."""


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict = field(default_factory=dict)     # lower-case names
    elapsed: float = 0.0                            # s, the successful attempt alone


def host_of(url):
    return urlsplit(url).hostname or ""


def parse_retry_after(value, now_wall=None):
    """Retry-After (delta-seconds or an HTTP date) -> seconds clamped to [1, 900], or None."""
    if value is None:
        return None
    value = str(value).strip()
    try:
        s = float(value)
    except ValueError:
        try:
            d = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
        if d is None:
            return None
        s = d.timestamp() - (time.time() if now_wall is None else now_wall)
    if s != s:      # NaN
        return None
    return min(RETRY_AFTER_MAX, max(RETRY_AFTER_MIN, s))


def miss_alive(path, ttl, now_wall=None):
    """Is the .404 marker at path still valid? Markers are empty files; their mtime is the time of the
    404 (markers written before TTLs existed keep working: they expire ttl after they were made)."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return False
    if ttl is None:
        return True
    return (time.time() if now_wall is None else now_wall) - mtime < ttl


# --- token bucket ------------------------------------------------------------------------------
class TokenBucket:
    """rate tokens/s up to burst, refilled on read. take() waits (demand), try_take() does not."""

    def __init__(self, rate, burst, clock=time.monotonic, sleep=time.sleep):
        self.rate, self.burst = float(rate), float(burst)
        self.clock, self.sleep = clock, sleep
        self.tokens = float(burst)
        self.t = clock()
        self.paused_until = None
        self.lock = threading.Lock()

    def _refill(self, now):
        if self.paused_until is not None:
            if now < self.paused_until:
                return
            self.t, self.paused_until = max(self.t, self.paused_until), None
        if now > self.t:
            self.tokens = min(self.burst, self.tokens + (now - self.t) * self.rate)
            self.t = now

    def level(self):
        with self.lock:
            self._refill(self.clock())
            return self.tokens

    def _wait(self, now, floor=0.0):
        """Seconds until one token (and floor more) is available (lock held)."""
        self._refill(now)
        need = 1.0 + floor
        if self.paused_until is not None:
            return self.paused_until - now + max(0.0, need - self.tokens) / self.rate
        return 0.0 if self.tokens >= need - TOKEN_EPS else (need - self.tokens) / self.rate

    def take(self, timeout=None, floor=0.0):
        """Take one token, sleeping until there is one; False (nothing taken) if that would take
        longer than timeout seconds, or the bucket is paused (a Retry-After, also while waiting: the
        host's breaker is open for as long, so the caller fails over instead of sleeping it out). floor: tokens that
        must stay in the bucket (demand leaves URGENT_TOKENS to urgent requests)."""
        floor = min(float(floor), max(0.0, self.burst - 1.0))
        deadline = None if timeout is None else self.clock() + timeout
        while True:
            with self.lock:
                now = self.clock()
                wait = self._wait(now, floor)
                if self.paused_until is not None:
                    return False
                if wait <= 0.0:
                    self.tokens -= 1.0
                    return True
            if deadline is not None and now + wait > deadline:
                return False
            self.sleep(wait)

    def wait_time(self):
        with self.lock:
            return self._wait(self.clock())

    def try_take(self, reserve=0.0):
        """Take one token now if the bucket holds more than reserve x burst (and at least one)."""
        with self.lock:
            self._refill(self.clock())
            if self.paused_until is None and self.tokens >= 1.0 - TOKEN_EPS and self.tokens > reserve * self.burst:
                self.tokens -= 1.0
                return True
            return False

    def give_back(self):
        with self.lock:
            self.tokens = min(self.burst, self.tokens + 1.0)

    def pause(self, until):
        """No tokens until `until` (clock time); the bucket is emptied."""
        with self.lock:
            self._refill(self.clock())
            self.tokens = 0.0
            self.paused_until = max(until, self.paused_until or until)

    def set_rate(self, rate):
        with self.lock:
            self._refill(self.clock())
            self.rate = float(rate)


# --- circuit breaker ---------------------------------------------------------------------------
class CircuitBreaker:
    """closed -> open after `threshold` failures within `window` s (or trip()); open -> half-open
    after the open time: one probe; its success closes the breaker (open time reset), its failure
    re-opens it for twice as long (up to max_open)."""

    def __init__(self, threshold=5, window=30.0, open_s=30.0, max_open=900.0, clock=time.monotonic):
        self.threshold, self.window = threshold, window
        self.base_open, self.max_open = open_s, max_open
        self.open_s = open_s
        self.clock = clock
        self.state = "closed"
        self.failures = deque()
        self.open_until = 0.0
        self.probing = False
        self.trips = 0
        self.lock = threading.Lock()

    def allow(self):
        """(allowed, is_probe, seconds until it may be asked again)."""
        with self.lock:
            now = self.clock()
            if self.state == "closed":
                return True, False, 0.0
            if self.state == "open":
                if now < self.open_until:
                    return False, False, self.open_until - now
                self.state = "half-open"
            if self.probing:
                return False, False, 1.0
            self.probing = True
            return True, True, 0.0

    def is_open(self):
        """Not closed: open, or half-open and not yet proven."""
        with self.lock:
            return self.state != "closed"

    def release_probe(self):
        """The probe ended without a verdict (dropped before the network): the next request probes."""
        with self.lock:
            self.probing = False

    def success(self):
        """True if this closed the breaker. A success while open is a request that started before it
        opened (a late answer after a Retry-After): only the probe may close it."""
        with self.lock:
            if self.state == "open":
                return False
            self.failures.clear()
            self.probing = False
            if self.state == "closed":
                return False
            self.state, self.open_s = "closed", self.base_open
            return True

    def failure(self):
        """True if this opened the breaker."""
        with self.lock:
            now = self.clock()
            if self.state == "half-open":
                self.open_s = min(self.max_open, self.open_s * 2)
                self._open(now, self.open_s)
                return True
            if self.state == "open":
                return False            # a request that started before it opened
            self.failures.append(now)
            while self.failures and now - self.failures[0] > self.window:
                self.failures.popleft()
            if len(self.failures) >= self.threshold:
                self._open(now, self.open_s)
                return True
            return False

    def trip(self, seconds):
        """Open now for `seconds` (a Retry-After)."""
        with self.lock:
            self._open(self.clock(), seconds)

    def _open(self, now, seconds):
        was_open = self.state == "open"
        self.open_until = max(now + seconds, self.open_until) if was_open else now + seconds
        self.state, self.probing = "open", False
        self.failures.clear()
        self.trips += 1


# --- transport ---------------------------------------------------------------------------------
def windows_ssl_context():
    """Use Windows' native certificate verification for HTTPS (not CERT_NONE).

    Windows curl/Schannel can build a valid certificate path while OpenSSL's
    chain selection rejects a stale or expired intermediate. truststore uses
    Windows CryptoAPI to validate the whole chain, including hostname.
    """
    if os.name != "nt":
        return None
    try:
        import truststore
    except ImportError:
        log.warning("Windows native TLS certificate verification unavailable: install truststore")
        return None
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


class HttpsTransport:
    """GET over one persistent HTTP(S) connection per thread and host (a fresh TLS handshake per tile
    roughly doubled the latency). No redirects are followed."""

    def __init__(self):
        self._local = threading.local()
        self._tls_context = windows_ssl_context()

    def request(self, url, headers, timeout):
        """(status, {lower-case header: value}, body)."""
        u = urlsplit(url)
        conns = self._local.__dict__.setdefault("conns", {})
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        key = (u.scheme, u.netloc)
        while True:
            conn = conns.get(key)
            reused = conn is not None
            if conn is None:
                if u.scheme == "https":
                    kwargs = {"timeout": timeout}
                    if self._tls_context is not None:
                        kwargs["context"] = self._tls_context
                    conn = conns[key] = http.client.HTTPSConnection(u.netloc, **kwargs)
                else:
                    conn = conns[key] = http.client.HTTPConnection(u.netloc, timeout=timeout)
            try:
                conn.request("GET", path, headers=dict(headers, Connection="keep-alive"))
                r = conn.getresponse()
                body = r.read()
                return r.status, {k.lower(): v for k, v in r.getheaders()}, body
            except (http.client.HTTPException, OSError):
                conn.close()
                del conns[key]
                if not reused:          # a fresh connection failed: that is the attempt's result (the
                    raise               # Fetcher retries through the bucket, with backoff)
                # else: the server may have closed the idle keep-alive connection; reconnect once


# --- per-host state ----------------------------------------------------------------------------
COUNTERS = ("requests", "bytes", "s2xx", "s3xx", "s4xx", "s404", "s429", "s5xx", "errors", "retries",
            "trips", "deferred", "unavailable")


class Host:
    def __init__(self, name, policy, clock, sleep):
        self.name, self.policy = name, policy
        self.bucket = TokenBucket(policy.rate, policy.burst, clock, sleep)
        self.sem = threading.BoundedSemaphore(max(1, policy.max_conc))
        self.urgent_sem = threading.BoundedSemaphore(URGENT_CONC)  # (urgent requests' own slots, then the shared ones)
        self.breaker = CircuitBreaker(policy.breaker_threshold, policy.breaker_window, policy.breaker_open,
                                      policy.breaker_max, clock)
        self.day = dict.fromkeys(COUNTERS, 0)       # this UTC day (persisted)
        self.minute = dict.fromkeys(COUNTERS, 0)    # since the last summary
        self.warned = 0.0                           # wall time of the 80 % budget warning (0 = none)
        self.warned_over = 0.0                      # ... and of the last over-budget one
        self.half_rate = False


class Fetcher:
    """See the module docstring. clock / sleep / rng / transport / wall are injectable (tests)."""

    def __init__(self, clock=time.monotonic, sleep=time.sleep, rng=None, transport=None, stats_dir=None,
                 policies=None, default_policy=DEFAULT_POLICY, offline=False, wall=time.time):
        self.clock, self.sleep, self.wall = clock, sleep, wall
        self.rng = rng if rng is not None else random.Random()
        self.transport = transport if transport is not None else HttpsTransport()
        self.stats_dir = stats_dir
        self.policies = dict(POLICIES if policies is None else policies)
        self.default_policy = default_policy
        self.offline_flag = bool(offline)
        self._hosts = {}
        self._chains = {}               # name -> hosts of a failover chain (imagery, dem)
        self._lock = threading.Lock()
        self._rng_lock = threading.Lock()
        self._date = self._today()
        self._flushed = clock()
        self._load_stats()

    # --- hosts and policies ------------------------------------------------------------------
    def policy(self, host):
        # (a lane, "host#name": the host's own policy, its own bucket and breaker: get's lane)
        return self.policies.get(host.split("#", 1)[0], self.default_policy)

    def host(self, name):
        with self._lock:
            h = self._hosts.get(name)
            if h is None:
                h = self._hosts[name] = Host(name, self.policy(name), self.clock, self.sleep)
                if self._loaded.get(name):
                    h.day.update({k: v for k, v in self._loaded[name].items() if k in h.day})
            return h

    def register_chain(self, name, urls_or_hosts):
        """A failover chain (imagery sources, DEM sources): `offline` is true when all are open."""
        hosts = tuple(dict.fromkeys(host_of(u) if "/" in u else u for u in urls_or_hosts))
        with self._lock:
            self._chains[name] = hosts

    @property
    def offline(self):
        """--offline, or every host of the registered chains has its breaker open."""
        if self.offline_flag:
            return True
        with self._lock:
            hosts = {h for c in self._chains.values() for h in c}
        return bool(hosts) and all(self.host(h).breaker.is_open() for h in hosts)

    def unavailable_hosts(self, optional=True):
        """Hosts whose breaker is open (or half-open), sorted; optional=False leaves out the hosts the world
        builds without (HostPolicy.optional)."""
        with self._lock:
            hosts = list(self._hosts.values())
        return sorted(h.name for h in hosts if h.breaker.is_open() and (optional or not h.policy.optional))

    def available(self, host):
        return not self.offline_flag and not self.host(host).breaker.is_open()

    def miss_alive(self, path, url):
        """A .404 marker for url is valid: within the host's miss_ttl, or the host cannot be asked now
        (offline / breaker open), so an expired marker still counts as "no data" instead of an error."""
        if not os.path.exists(path):
            return False
        host = host_of(url)
        return miss_alive(path, self.policy(host).miss_ttl, self.wall()) or not self.available(host)

    # --- admission ---------------------------------------------------------------------------
    def _check_policy(self, h, url, kind):
        p = h.policy
        if kind not in KINDS:
            raise ValueError(f"unknown request kind {kind!r}")
        if p.denied:
            raise PolicyDenied(f"{h.name}: not allowed at runtime", url, h.name)
        if kind == "prefetch" and (p.interactive_only or not p.allow_prefetch):
            raise PolicyDenied(f"{h.name}: no prefetch", url, h.name)
        if kind == "predownload" and (p.interactive_only or not p.offline_ok):
            raise PolicyDenied(f"{h.name}: no pre-download", url, h.name)

    def _check_budget(self, h, url, kind):
        p = h.policy
        with self._lock:
            used = h.day["requests"]
        if p.daily_hard is not None and used >= p.daily_hard:
            raise BudgetExceeded(f"{h.name}: daily limit of {p.daily_hard} requests reached", url, h.name)
        if p.daily_budget is None:
            return
        now = self.wall()
        if used >= p.daily_budget:
            if not h.half_rate:
                h.half_rate = True
                h.bucket.set_rate(p.rate / 2)
            if not h.warned_over or now - h.warned_over >= BUDGET_REPEAT_S:
                h.warned = h.warned_over = now
                log.warning("net: %s: %d requests today, over the daily budget of %d: half rate, demand only",
                            h.name, used, p.daily_budget)
        elif used >= BUDGET_WARN * p.daily_budget and not h.warned:
            h.warned = now
            log.warning("net: %s: %d requests today, %d %% of the daily budget of %d: no more prefetch",
                        h.name, used, round(100 * BUDGET_WARN), p.daily_budget)
        if kind not in ("demand", "urgent") and used >= BUDGET_WARN * p.daily_budget:
            raise BudgetExceeded(f"{h.name}: past {round(100 * BUDGET_WARN)} % of the daily budget", url, h.name)

    def allow(self, host, kind="prefetch"):
        """Would a request of this kind be admitted now (without taking anything)?"""
        h = self.host(host)
        try:
            self._check_policy(h, "", kind)
            self._check_budget(h, "", kind)
        except FetchError:
            return False
        if self.offline_flag or h.breaker.is_open():
            return False
        if kind in ("demand", "urgent"):
            return True
        reserve = PREFETCH_RESERVE if kind == "prefetch" else BACKGROUND_RESERVE
        lv = h.bucket.level()
        return lv >= 1.0 - TOKEN_EPS and lv > reserve * h.policy.burst

    def _acquire(self, h, url, kind):
        """A request slot and a token for one attempt (raises Deferred / RateLimited). Returns the semaphore taken
        (_release gives it back)."""
        p = h.policy
        if kind == "urgent":
            sem = h.urgent_sem if h.urgent_sem.acquire(blocking=False) else h.sem
            if sem is h.sem and not h.sem.acquire(timeout=p.max_wait):
                raise RateLimited(f"{h.name}: no free request slot within {p.max_wait:.0f} s", url, h.name)
            if not h.bucket.take(p.max_wait):
                sem.release()
                wait = h.bucket.wait_time()
                raise RateLimited(f"{h.name}: rate limited for {wait:.1f} s", url, h.name, retry_after=wait)
            return sem
        if kind == "demand":
            if not h.sem.acquire(timeout=p.max_wait):
                raise RateLimited(f"{h.name}: no free request slot within {p.max_wait:.0f} s", url, h.name)
            if not h.bucket.take(p.max_wait, floor=URGENT_TOKENS):
                h.sem.release()
                wait = h.bucket.wait_time()
                raise RateLimited(f"{h.name}: rate limited for {wait:.1f} s", url, h.name, retry_after=wait)
            return h.sem
        if not h.sem.acquire(blocking=False):
            self._bump(h, "deferred")
            raise Deferred(f"{h.name}: busy, {kind} dropped", url, h.name)
        reserve = PREFETCH_RESERVE if kind == "prefetch" else BACKGROUND_RESERVE
        if not h.bucket.try_take(reserve):
            h.sem.release()
            self._bump(h, "deferred")
            raise Deferred(f"{h.name}: bucket low, {kind} dropped", url, h.name)
        return h.sem

    def _admit(self, url, kind, lane=None):
        """Everything before the network: (Host, is_probe). The caller then _acquire()s per attempt."""
        if self.offline_flag:
            raise Offline(f"offline: {url}", url, host_of(url))
        h = self.host(host_of(url) + ("#" + lane if lane else ""))
        self._check_policy(h, url, kind)
        self._check_budget(h, url, kind)
        ok, probe, wait = h.breaker.allow()
        if not ok:
            self._bump(h, "unavailable")
            raise HostUnavailable(f"{h.name} unavailable for {wait:.0f} s (circuit open)", url, h.name,
                                  retry_after=wait)
        return h, probe

    # --- requests ----------------------------------------------------------------------------
    def headers(self, host, extra=None):
        hd = {"User-Agent": USER_AGENT}
        if self.policy(host).referer:
            hd["Referer"] = PROJECT_URL
        if extra:
            hd.update(extra)
        return hd

    def _backoff(self, p, attempt):
        with self._rng_lock:
            return self.rng.uniform(0.0, min(p.max_delay, p.base_delay * 2 ** attempt))

    def get(self, url, *, kind="demand", headers=None, lane=None):
        """GET url under its host's policy: a Response with status 2xx, or one of the errors above. lane: a name of
        its own (its own rate bucket, breaker and day's count under the host's policy): one kind of request whose
        failures must not shut another out (the status bar's town lookups and the map's search, GitHub #350)."""
        h, probe = self._admit(url, kind, lane)
        p = h.policy
        attempts = 1 if probe else max(1, p.max_attempts)
        hd = self.headers(h.name, headers)
        err = None
        try:
            for attempt in range(attempts):
                if attempt:
                    self._bump(h, "retries")
                    self.sleep(self._backoff(p, attempt - 1))
                    ok, probe2, wait = h.breaker.allow()   # another worker may have opened it meanwhile
                    if not ok:
                        self._bump(h, "unavailable")
                        raise HostUnavailable(f"{h.name} unavailable for {wait:.0f} s (circuit open)", url,
                                              h.name, retry_after=wait)
                    probe = probe or probe2
                sem = self._acquire(h, url, kind)
                try:
                    self._count(h, "requests")
                    t0 = self.clock()
                    try:
                        status, rh, body = self.transport.request(url, hd, p.timeout)
                    except (http.client.HTTPException, OSError) as e:
                        self._count(h, "errors")
                        self._failed(h)
                        probe = False
                        err = FetchFailed(f"{url}: {type(e).__name__}: {e}", url, h.name)
                        continue
                    elapsed = self.clock() - t0
                    if NETLOG:
                        _netlog(url, hd, status, body)
                finally:
                    sem.release()
                res = self._classify(h, url, status, {k.lower(): v for k, v in (rh or {}).items()}, body, elapsed)
                probe = False               # the breaker has its verdict
                if isinstance(res, Response):
                    return res
                if isinstance(res, (NotFound, BadResponse)) or (isinstance(res, RateLimited) and res.retry_after):
                    raise res
                if isinstance(res, FetchFailed) and res.status not in p.retry_statuses:
                    raise res               # 400, 401, 403...: asking again will not help
                err = res
            raise err
        finally:
            if probe:                       # dropped before the network answered: let the next one probe
                h.breaker.release_probe()

    def _classify(self, h, url, status, rh, body, elapsed):
        """A Response (success) or the error for this attempt; updates counters and the breaker."""
        p = h.policy
        self._count(h, "bytes", len(body or b""))
        cls = f"s{status // 100}xx" if 200 <= status < 600 else "errors"
        self._count(h, cls)
        if 200 <= status < 300:
            ctype = rh.get("content-type", "")
            head = (body or b"")[:64].lstrip().lower()
            if p.binary and ("text/html" in ctype or head.startswith((b"<!doctype html", b"<html"))):
                self._failed(h)
                return BadResponse(f"{url}: an HTML page instead of data", url, h.name, status=status)
            if p.slow_ms is not None and elapsed * 1000 > p.slow_ms:
                self._failed(h)
            else:
                self._succeeded(h)
            return Response(status, body, rh, elapsed)
        if status in (404, 410):
            self._count(h, "s404")
            self._succeeded(h)              # the host answered: it is healthy
            return NotFound(f"{url}: HTTP {status}", url, h.name, status=status)
        ra = parse_retry_after(rh.get("retry-after"), self.wall()) if status in (429, 503) else None
        if status == 429:
            self._count(h, "s429")
        if ra is not None:                  # the host said when: pause it, no retries
            h.bucket.pause(self.clock() + ra)
            h.breaker.trip(ra)
            self._count(h, "trips")
            log.warning("net: %s answered %d, Retry-After %.0f s: paused", h.name, status, ra)
            return RateLimited(f"{url}: HTTP {status}, retry after {ra:.0f} s", url, h.name, retry_after=ra,
                               status=status)
        if 300 <= status < 400 and p.redirect_limited:
            self._failed(h)
            return RateLimited(f"{url}: HTTP {status} redirect (rate limited)", url, h.name, status=status)
        if status == 429:
            self._failed(h)
            return RateLimited(f"{url}: HTTP 429", url, h.name, status=status)
        self._failed(h)
        return FetchFailed(f"{url}: HTTP {status}", url, h.name, status=status)

    def _failed(self, h):
        if h.breaker.failure():
            self._count(h, "trips")
            log.warning("net: %s: circuit open for %.0f s", h.name, h.breaker.open_s)

    def _succeeded(self, h):
        if h.breaker.success():
            log.info("net: %s: circuit closed", h.name)

    @contextmanager
    def session(self, url, kind="demand"):
        """For a caller that does its own HTTP (gravity.py's resumable download through urllib, which
        follows SourceForge's mirror redirects): admission, a slot and a token for one request. Inside,
        set `s.status` and `s.nbytes`; an exception counts as a failure (an HTTPError's status and
        Retry-After are honoured) and is re-raised."""
        h, probe = self._admit(url, kind)
        try:
            sem = self._acquire(h, url, kind)
        except FetchError:
            if probe:
                h.breaker.release_probe()
            raise
        s = _Session(self.headers(h.name))
        self._count(h, "requests")
        t0 = self.clock()
        try:
            yield s
        except BaseException as e:
            code = getattr(e, "code", None)
            if isinstance(code, int):
                hdr = getattr(e, "headers", None) or {}
                self._classify(h, url, code, {k.lower(): v for k, v in dict(hdr).items()}, b"", 0.0)
            elif isinstance(e, (OSError, http.client.HTTPException)):
                self._count(h, "errors")
                self._failed(h)
            elif probe:
                h.breaker.release_probe()
            raise
        else:
            self._classify(h, url, s.status, {}, b"", self.clock() - t0)
            self._count(h, "bytes", max(0, s.nbytes))
        finally:
            sem.release()

    # --- counters, budgets, persistence ------------------------------------------------------
    def _today(self):
        return time.strftime("%Y-%m-%d", time.gmtime(self.wall()))

    def _bump(self, h, what, n=1):
        with self._lock:
            h.day[what] += n
            h.minute[what] += n

    def _count(self, h, what, n=1):
        self._rollover()
        self._bump(h, what, n)
        if what == "requests" and self.stats_dir is not None and self.clock() - self._flushed >= STATS_FLUSH_S:
            self.flush()

    def _rollover(self):
        """A new UTC day: persist the old one and start the counters (and budgets) again."""
        d = self._today()
        if d == self._date:
            return
        self.flush()
        with self._lock:
            self._date, self._loaded = d, {}
            for h in self._hosts.values():
                h.day = dict.fromkeys(COUNTERS, 0)
                h.warned = h.warned_over = 0.0
                if h.half_rate:
                    h.half_rate = False
                    h.bucket.set_rate(h.policy.rate)

    def _stats_path(self, date):
        return os.path.join(self.stats_dir, f"{date}.json")

    def _load_stats(self):
        self._loaded = {}
        if self.stats_dir is None:
            return
        try:
            with open(self._stats_path(self._date)) as f:
                d = json.load(f)
        except (OSError, ValueError):
            return
        if isinstance(d, dict) and isinstance(d.get("hosts"), dict):
            self._loaded = {k: v for k, v in d["hosts"].items() if isinstance(v, dict)}

    def snapshot(self):
        """{host: today's counters, plus budget left and breaker state}."""
        with self._lock:
            hosts = dict(self._hosts)
            out = {name: dict(self._loaded.get(name, {})) for name in self._loaded if name not in hosts}
            for name, h in hosts.items():
                out[name] = dict(h.day)
        for name, h in hosts.items():
            b = h.policy.daily_budget
            out[name]["budget_left"] = None if b is None else max(0, b - out[name]["requests"])
            out[name]["breaker"] = h.breaker.state
        return out

    def flush(self):
        """Write today's counters to <stats_dir>/<date>.json (atomically)."""
        self._flushed = self.clock()
        if self.stats_dir is None:
            return
        try:
            os.makedirs(self.stats_dir, exist_ok=True)
            path = self._stats_path(self._date)
            tmp = f"{path}.{threading.get_ident()}.tmp"
            with open(tmp, "w") as f:
                json.dump({"date": self._date, "hosts": self.snapshot()}, f, indent=1, sort_keys=True)
            os.replace(tmp, path)
        except OSError as e:
            log.warning("net: could not write the request statistics: %s", e)

    def summary(self, reset=False):
        """One line: per host the requests since the last reset (status classes, retries, MB) and
        the open breakers; "" when nothing happened."""
        parts = []
        with self._lock:
            hosts = sorted(self._hosts.values(), key=lambda h: h.name)
            for h in hosts:
                m = h.minute
                if any(m.values()):
                    extra = ", ".join(f"{k} {m[k]}" for k in ("s404", "s429", "s3xx", "s5xx", "errors", "retries",
                                                               "deferred", "unavailable", "trips") if m[k])
                    parts.append(f"{h.name} {m['requests']} req {m['bytes'] / 1e6:.1f} MB"
                                 + (f" ({extra})" if extra else "") + f", today {h.day['requests']}")
                if reset:
                    h.minute = dict.fromkeys(COUNTERS, 0)
        down = self.unavailable_hosts()
        if down:
            parts.append("circuit open: " + ", ".join(down))
        if self.offline_flag:
            parts.append("offline")
        return "; ".join(parts)

    def tick(self):
        """Once a minute (earthd): persist the counters; the summary of the last minute, or ""."""
        self._rollover()
        self.flush()
        return self.summary(reset=True)

    def close(self):
        self.flush()


class _Session:
    def __init__(self, headers):
        self.headers = headers
        self.status = 200
        self.nbytes = 0


_default = None
_default_lock = threading.Lock()


def default_fetcher():
    """The process-wide Fetcher for code that was not handed one (library use, tools)."""
    global _default
    with _default_lock:
        if _default is None:
            _default = Fetcher()
        return _default


def with_policy(policies, host, **changes):
    """A copy of a policy table with one host's policy changed (tests, tools)."""
    out = dict(policies)
    out[host] = replace(out.get(host, DEFAULT_POLICY), **changes)
    return out
