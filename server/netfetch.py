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
  * response classes: 404/410 -> NotFound (the caller remembers it: missindex.record; miss_alive() gives the
    marker its policy TTL), a 3xx from a tile host -> rate limited (EOX redirects throttled clients
    to its home page), a 200 with an HTML page from a tile host -> BadResponse (not cached, counted
    as a failure), other 4xx -> FetchFailed (no marker: some servers answer 400 when overloaded).

--offline (Fetcher(offline=True)): every request raises Offline at once; the callers' disk caches
still serve what they have. `Fetcher.offline` is also true when every host of the registered
chains has its breaker open, and earthd then says "Offline - using cached data" in its status.

One User-Agent for the whole project (USER_AGENT): the app, its version and the project URL; no
user name, e-mail, machine name, location or session id is ever sent.
"""
import collections
import ioprofiler
import netstate
import email.utils
import http.client
import json
import logging
import missindex
import os
import random
import ssl
import threading
import weakref
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
import base64
import errno
import urllib.request
from urllib.parse import urlsplit

log = logging.getLogger("earthd.net")

def _app_version():
    """This copy's VERSION (the folder above server/, or the packaged app's bundle: updates.local_version's place), for
    the User-Agent the data hosts see and the hello's serverVersion; it said "0.4.0" since October (Cloudflare's
    analytics, 2026-11-11)."""
    import sys
    root = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        with open(os.path.join(root, "VERSION"), encoding="utf-8") as f:
            v = f.read().strip()
    except OSError:
        v = ""
    return "".join(c for c in v if c.isalnum() or c in ".-+")[:40] or "unknown"


SERVER_VERSION = _app_version()
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
PREDOWNLOAD_SHARE = 0.5         # saved areas' downloads ahead (areas.py) stop at this share of a host's daily budget
                                # (soft, else hard): live play keeps the rest of the day
BUDGET_REPEAT_S = 3600.0        # over budget: the warning repeats this often
STATS_FLUSH_S = 60.0
DAY = 86400.0
TOKEN_EPS = 1e-4                # a token short by rounding (clock resolution) counts as a token
OFFLINE_TEXT = "Offline - using cached data"

# testing a slow connection (a player's large downloads ran at ~1 MB/s each, 2026-11-04): every response takes at least
# its size at this many MB/s, plus BNGE_NET_LATENCY_MS, while it holds its connection slot. Off without the variable.
TEST_MBPS = float(os.environ.get("BNGE_NET_MBPS") or 0)
TEST_LATENCY_S = float(os.environ.get("BNGE_NET_LATENCY_MS") or 0) / 1000.0
NETLOG = os.environ.get("BNGE_NETLOG")      # debugging: every network request, one line each, from every process
NETLOG_STACK = os.environ.get("BNGE_NETLOG_STACK") == "1"   # ... with its caller (the server's own frames, innermost first)
_STACK_SKIP = ("netfetch", "ioprofiler", "threading", "concurrent", "thread", "queue")


_why = threading.local()             # .tag: the caller a pool thread fetches for (set by its submitter: imagery)


def _caller(depth=2):
    """"mod.func<mod.func<...@thread" of the server's frames that asked (BNGE_NETLOG_STACK; debugging only); a pool
    thread's own frames, then its submitter's (imagery._future's tag)."""
    import sys
    import multiprocessing
    out = []
    f = sys._getframe(depth)
    here = os.path.dirname(os.path.abspath(__file__))
    while f is not None and len(out) < 8:
        fn = f.f_code.co_filename
        mod = os.path.splitext(os.path.basename(fn))[0]
        if os.path.dirname(os.path.abspath(fn)) == here and mod not in _STACK_SKIP:
            out.append(f"{mod}.{f.f_code.co_name}")
        f = f.f_back
    me = ("<".join(out) or "-") + "@" + threading.current_thread().name.replace(" ", "_")
    ctx = getattr(_why, "ctx", None)
    if ctx:
        me += "#" + ctx
    tag = getattr(_why, "tag", None)
    return (me + "<<" + tag if tag else me + "@@" + multiprocessing.current_process().name.replace(" ", "_"))


def _netlog(url, headers, status, body):
    """Append "<unix time> <pid> <status> <bytes> <url> <range>[ <caller>]" to the BNGE_NETLOG file (one write per
    line: whole lines from every process)."""
    line = (f"{time.time():.3f} {os.getpid()} {status} {len(body or b'')} {url} "
            f"{(headers or {}).get('Range', '-')}{(' ' + _caller()) if NETLOG_STACK else ''}\n")
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
    connect_timeout: float | None = None    # s to open a new connection (None: timeout): a source with a fallback
                                            # gives up on a host that does not answer at once
    max_wait: float = 30.0                  # s a demand request may wait for a token
    redirect_limited: bool = False          # a 3xx means "rate limited" (tile hosts never redirect)
    binary: bool = False                    # a 200 with an HTML page is an error page
    referer: bool = False                   # send PROJECT_URL as Referer (hosts that ask for one)
    optional: bool = False                  # the world builds without it (gravity, weather, search, routing,
                                            # updates): left out of the player's "Degraded" status (GitHub #23)
    shared: bool = False                    # admitted across the server's processes (SharedGate: SHARED_DIR)


TILE_HOST = dict(redirect_limited=True, binary=True)
# docs/research/2026-10-10-rate-limits.md §5.2, the hosts the server contacts today (rates per player).
POLICIES = {
    # (2026-10-16, the user: raise them, lower again on trouble; a first visit's far field waited ~100 s on 15/s)
    # (2026-10-23, the user: raise it again: 24 at once; a PC already got ~26/s with 12 and the same far field with 24,
    # all answered, so it helps players far from USGS, whose every request waits longer)
    # (2026-11-07, the user: "i think its safe to increase USGS": 100/s, 32 at once; behind CloudFront, telemetry over
    # ~500 players showed no 429 and 0.1 % errors at 50/s, and the 3D map's fresh views waited on ~1,350 deferred)
    "basemap.nationalmap.gov": HostPolicy(rate=100.0, burst=300.0, max_conc=32, daily_budget=150000,
                                          allow_prefetch=True, offline_ok=True, miss_ttl=90 * DAY, **TILE_HOST),
    # NAIP through USGS's ArcGIS exportImage (imagery.py "naip"): one 1024² image a request, 4-10 s each
    # and 5xx under parallel requests (docs/research/2026-10-10-us-state-imagery.md, in-game 2026-10-13):
    # one at a time, only imagery.warm()'s look-ahead (never on a render's critical path)
    "imagery.nationalmap.gov": HostPolicy(rate=0.5, burst=4.0, max_conc=1, daily_budget=3000, shared=True,
                                          allow_prefetch=True, offline_ok=True, miss_ttl=90 * DAY,
                                          binary=True, timeout=40.0, max_attempts=2, base_delay=2.0,
                                          breaker_threshold=6, breaker_window=120.0),
    # USDA FPAC's NAIP ImageServer (imagery.py "naipusda"): 1.6-4 s a 1024² image in-game 2026-10-13
    "apps.geo.fpac.usda.gov": HostPolicy(rate=1.0, burst=4.0, max_conc=1, daily_budget=4000, shared=True,
                                         allow_prefetch=True, offline_ok=True, miss_ttl=90 * DAY,
                                         binary=True, timeout=30.0, max_attempts=2, base_delay=2.0,
                                         breaker_threshold=6, breaker_window=120.0),
    # MD iMAP six-inch MapServer export (imagery.py "md6in"): ~1.3 s a 1024² image in-game 2026-10-13
    "mdgeodata.md.gov": HostPolicy(rate=1.0, burst=4.0, max_conc=1, daily_budget=4000, shared=True,
                                   allow_prefetch=True, offline_ok=True, miss_ttl=90 * DAY,
                                   binary=True, timeout=30.0, max_attempts=2, base_delay=2.0,
                                   breaker_threshold=6, breaker_window=120.0),
    # NOAA NCEI's DEM global mosaic (bathymetry for the sea floor, seacarve.py): one export per map-data cell
    "gis.ngdc.noaa.gov": HostPolicy(rate=1.0, burst=4.0, max_conc=2, daily_budget=4000,
                                    allow_prefetch=True, offline_ok=True, miss_ttl=30 * DAY,
                                    binary=True, timeout=60.0, max_attempts=2, base_delay=2.0,
                                    breaker_threshold=6, breaker_window=120.0),
    # (20/s, 12 at once: the user's OK 2026-10-30; at 10/s a first visit's far field outside the US took ~4 min, 761
    # tiles in 241 s at Canterbury; s2maps-tiles.eu, EOX's other domain, refuses us: no mirror)
    # (40/s, 24 at once, 80,000 a day: the user, 2026-11-07, "i think we could increase our own limit if needed": the 3D
    # map's first views waited on 4,648 deferred EOX requests and used 23,258 of the 30,000 by mid-morning)
    "tiles.maps.eox.at": HostPolicy(rate=40.0, burst=160.0, max_conc=24, daily_budget=80000,
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
    # the clouds seen from space (spaceclouds.py: NASA GIBS's latest VIIRS true-colour day, up to 144 small tiles when a
    # camera rises above the clouds; public domain, no key)
    "gibs.earthdata.nasa.gov": HostPolicy(rate=8.0, burst=40.0, max_conc=6, daily_budget=6000, cache_ttl=3 * 3600.0,
                                          miss_ttl=3600.0, timeout=20.0, max_attempts=2, optional=True, **TILE_HOST),
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
    # Overture's two copies of each release (AWS, Azure: rangeread spreads the reads over both): 8 at once each (the
    # user, 2026-10-30: a fresh place's road cells waited on our 4; one connection gave ~5-27 MB/s, 8 ~40-50)
    **{h: HostPolicy(rate=40.0, burst=80.0, max_conc=8, daily_budget=None, allow_prefetch=True,
                     offline_ok=True, miss_ttl=None, binary=True, timeout=30.0)
       for h in ("overturemaps-us-west-2.s3.us-west-2.amazonaws.com",
                 "overturemapswestus2.blob.core.windows.net")},
    # Overture's own vector tiles (pmtiles.py): its tile CDN and the bucket behind it under its two names, range reads
    # of a release's immutable archives (a cell's tile and its directories), each mirror with its own limits
    **{h: HostPolicy(rate=40.0, burst=80.0, max_conc=8, daily_budget=None, allow_prefetch=True,
                     offline_ok=True, miss_ttl=None, binary=True, timeout=30.0)
       for h in ("tiles.overturemaps.org", "overturemaps-extras-us-west-2.s3.us-west-2.amazonaws.com",
                 "overturemaps-extras-us-west-2.s3.amazonaws.com")},
    **{h: HostPolicy(rate=20.0, burst=40.0, max_conc=4, daily_budget=None, allow_prefetch=True,
                     offline_ok=True, miss_ttl=None, binary=True, timeout=30.0)
       for h in ("esa-worldcover.s3.eu-central-1.amazonaws.com",
                 "dataforgood-fb-data.s3.amazonaws.com",
                 "copernicus-dem-30m.s3.amazonaws.com")},      # the polar caps' DEM (polardem.py)
    # British Columbia's lidar DEMs (lidarbc.py): range reads of the Province's open bucket; optional
    "nrs.objectstore.gov.bc.ca": HostPolicy(rate=20.0, burst=40.0, max_conc=6, daily_budget=None, allow_prefetch=True,
                                            offline_ok=True, miss_ttl=None, binary=True, timeout=30.0, optional=True),
    # Canada's lidar terrain model (hrdem.py): range reads of NRCan's COG mosaic; optional (Mapterhorn stands in)
    "canelevation-dem.s3.ca-central-1.amazonaws.com": HostPolicy(rate=20.0, burst=40.0, max_conc=6, daily_budget=None,
                                                                 allow_prefetch=True, offline_ok=True, miss_ttl=None,
                                                                 binary=True, timeout=30.0, optional=True),
    # OurAirports' nightly CSV dump on GitHub Pages (airports.py): two files (~3 MB gzipped) once a week,
    # one request at a time, a hard cap well above a week's need; the copy on disk serves offline
    "davidmegginson.github.io": HostPolicy(rate=0.2, burst=2.0, max_conc=1, daily_budget=20, daily_hard=40,
                                           offline_ok=True, miss_ttl=None, binary=True, timeout=120.0,
                                           max_attempts=2, base_delay=5.0),
    # Photon (komoot's OpenStreetMap geocoder, made for search-as-you-type; geocode.py): the map's search box,
    # debounced and cached; a modest rate, a daily cap (fair use of a free public service)
    "photon.komoot.io": HostPolicy(rate=2.0, burst=4.0, max_conc=1, daily_budget=2000, daily_hard=3000,
                                   cache_ttl=7 * DAY, miss_ttl=None, max_wait=5.0, timeout=12.0, max_attempts=1, optional=True),
    # Nominatim (OpenStreetMap's own geocoder; geocode.py): streets and house numbers when the player presses Enter.
    # Its usage policy: one request a second at most, no autocomplete, a User-Agent that names the app
    "nominatim.openstreetmap.org": HostPolicy(rate=1.0, burst=1.0, max_conc=1, daily_budget=300, daily_hard=500,
                                              cache_ttl=7 * DAY, miss_ttl=None, max_wait=2.5, timeout=8.0,
                                              max_attempts=1, optional=True),
    # Wikidata and Wikipedia (geocode.py): landmarks as you type, debounced by the map; a modest rate and a User-Agent
    # that names the app, as their API etiquette asks (the Wikipedias: WIKIPEDIA_LANGS below)
    "www.wikidata.org": HostPolicy(rate=4.0, burst=8.0, max_conc=2, daily_budget=3000, daily_hard=5000,
                                   cache_ttl=7 * DAY, miss_ttl=None, max_wait=3.0, timeout=6.0, max_attempts=1,
                                   optional=True),
    # OSRM on OpenStreetMap by FOSSGIS (routing.py): one request per GPS destination or re-route; their fair use:
    # ~1 request a second at most, no bulk (burst 1: two destinations set 2 s apart were the second answered 429,
    # GitHub #1027; the second now waits its turn, earthd.route asks again after a 429)
    "routing.openstreetmap.de": HostPolicy(rate=0.5, burst=1.0, max_conc=1, daily_budget=300, daily_hard=500,
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
    # (4 at once: ~1-5 MB/s a connection, uncached at Cloudflare's edge; a fresh place's first graded block waited ~20 s
    # on 2, the user's OK 2026-10-30; no mirror of the file exists)
    "data.openstreetmap.us": HostPolicy(rate=8.0, burst=16.0, max_conc=4, daily_budget=5000, allow_prefetch=True,
                                        offline_ok=True, miss_ttl=None, binary=True, timeout=30.0, optional=True),
    # USGS 3DEP lidar point clouds (roadz.py: the US bridge decks, BNE_LIDAR_DECKS): EPT metadata (kept on disk) and
    # ~0.3 MB LAZ nodes along a cell's bridges once (the decks made from them are kept); optional (the inference)
    # (10 a second, 4 at once held a cold place's decks: Joplin's 158 nodes took 16 s at least; the user, 2026-11-11: "We
    # can raise the limit as high as we need to"; S3 serves thousands a second a prefix)
    "s3-us-west-2.amazonaws.com": HostPolicy(rate=100.0, burst=300.0, max_conc=16, daily_budget=100000, daily_hard=200000,
                                             allow_prefetch=True, offline_ok=True, miss_ttl=None, binary=True,
                                             timeout=30.0, optional=True),
    # Norway's road database (roadz.py: road, bridge and tunnel heights, NVDB API v4): a few pages per map-data cell
    # once, kept on disk; a modest rate for a public API that asks clients to name themselves (X-Client, no user data)
    "nvdbapiles.atlas.vegvesen.no": HostPolicy(rate=2.0, burst=4.0, max_conc=2, daily_budget=3000, daily_hard=6000,
                                               allow_prefetch=True, offline_ok=True, miss_ttl=None, timeout=30.0,
                                               max_attempts=3, base_delay=2.0, optional=True),
    # 3DBAG's WFS (threedbag.py: the Netherlands' measured roofs): one request per 250 m square of the Dutch grid as road
    # tiles there are built, kept on disk; a modest rate for a university's public service
    "data.3dbag.nl": HostPolicy(rate=2.0, burst=4.0, max_conc=2, daily_budget=3000, daily_hard=6000,
                                allow_prefetch=True, offline_ok=True, miss_ttl=None, timeout=30.0,
                                max_attempts=3, base_delay=2.0, optional=True),
    # Hong Kong's road heights (roadz.py "hk", BNE_ROADZ_HK): the CSDI Portal's ArcGIS services (the Transport
    # Department's road centrelines, the CEDD LiDAR sheet index) a few requests per map-data cell, and the LiDAR sheets
    # themselves (zips of 5-6 MB, kept in a size-capped folder; ~0.8 MB/s a connection, 2026-11-10); optional
    "portal.csdi.gov.hk": HostPolicy(rate=2.0, burst=4.0, max_conc=2, daily_budget=3000, daily_hard=6000,
                                     allow_prefetch=True, offline_ok=True, miss_ttl=None, timeout=30.0,
                                     max_attempts=3, base_delay=2.0, optional=True),
    "bulkdata.csdi.gov.hk": HostPolicy(rate=1.0, burst=4.0, max_conc=4, daily_budget=1000, daily_hard=2000,
                                       allow_prefetch=True, offline_ok=True, miss_ttl=None, binary=True, timeout=120.0,
                                       max_attempts=3, base_delay=2.0, optional=True),
    # never at runtime (OSM Overpass, OSMF vector tiles, Protomaps builds): §3.3
    "overpass-api.de": HostPolicy(denied=True, daily_hard=0),
    "vector.openstreetmap.org": HostPolicy(denied=True, daily_hard=0),
    "build.protomaps.com": HostPolicy(denied=True, daily_hard=0),
}
# The Wikipedias the map's search asks (geocode.wiki_langs): by the player's country and the computer's language
WIKIPEDIA_LANGS = ("en", "de", "fr", "it", "es", "pt", "nl", "pl", "cs", "sk", "sv", "no", "da", "fi", "hu", "ro", "el",
                   "tr", "ru", "uk", "ja", "ko", "zh", "hr", "sl", "bg", "sr")
POLICIES.update({f"{lang}.wikipedia.org": HostPolicy(rate=4.0, burst=8.0, max_conc=2, daily_budget=3000, daily_hard=5000,
                                                     cache_ttl=7 * DAY, miss_ttl=None, max_wait=3.0, timeout=6.0,
                                                     max_attempts=1, optional=True) for lang in WIKIPEDIA_LANGS})
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
                                   shared=True, miss_ttl=30 * DAY, binary=True, timeout=30.0, max_attempts=2,
                                   base_delay=2.0, breaker_threshold=6, breaker_window=120.0, optional=True)
    return out


POLICIES.update(_regional_policies())
# swisstopo's WMTS (SWISSIMAGE, the "swiss" photos): the FSDI's General Terms of Use (geo.admin.ch/en/general-terms-of-use-
# fsdi, §2.3's table, read 2026-11-13) allow wmts.geo.admin.ch 1'200 requests / minute (20/s; 631 M a year in all;
# doubled on 2025-01-01, geo.admin.ch/en/change-limits-fair-use) as fair use, and desktop applications are fair use. The
# regional tile hosts' 6/s x4 held a cold Stelvio far field 168 s on its 1,175 tiles (expo baseline, 2026-11-12): 15/s
# (3/4 of the limit), 8 at once.
POLICIES["wmts.geo.admin.ch"] = replace(POLICIES["wmts.geo.admin.ch"], rate=15.0, burst=30.0, max_conc=8,
                                        daily_budget=40000)
# New York State's orthoimagery (orthos.its.ny.gov ArcGIS export, NYS ITS Geospatial Services): no request limit is
# published (the service's metadata and licence, gis.ny.gov/orthoimagery and its FAQs, read 2026-11-13: free, "Users
# can use web services"; the server's own caps: 4096 px images). One request at a time (~1.4 s each) kept a cold
# Manhattan's photos fetching for 177 s: 3 at once at 2/s, still a few times under what one map viewer asks for.
POLICIES["orthos.its.ny.gov"] = replace(POLICIES["orthos.its.ny.gov"], rate=2.0, burst=6.0, max_conc=3)
# The project's own tile host (cdn.py, imagery "bngs2": Cloudflare R2 behind its CDN; tile packs, ~64 tiles a request)
_CDN_HOST = os.environ.get("BNGE_CDN_BASE", "https://tiles.tgforever.net").split("://", 1)[-1].split("/", 1)[0]
POLICIES[_CDN_HOST] = HostPolicy(rate=50.0, burst=200.0, max_conc=16, daily_budget=60000, allow_prefetch=True,
                                 offline_ok=True, miss_ttl=30 * DAY, **TILE_HOST)
# A self-hosted BeamMP collector gets the server list's limits too (keep existing policies for known hosts).
_SERVER_LIST_HOST = urlsplit(os.environ.get("BNGE_SERVER_LIST_URL", "https://earth.tgforever.net/servers")).hostname
if _SERVER_LIST_HOST:
    POLICIES.setdefault(_SERVER_LIST_HOST, POLICIES["earth.tgforever.net"])
# Our prebaked input tiles (bnwt.py, BNGE_BNWT_URL: off when unset): range reads of a few archives on a CDN, like the
# project's tile host (which they will likely share); a host of its own gets the same policy
_BNWT_URL = (os.environ.get("BNGE_BNWT_URL") or "").strip()
_BNWT_HOST = (urlsplit(_BNWT_URL).hostname or "") if _BNWT_URL.startswith(("http://", "https://")) else ""
if _BNWT_HOST:
    # (their own lane, "host#bnwt", whatever else the host serves: the server falls back to the sources themselves, so
    # a host that does not answer is given up quickly: 2 s to connect, 2 tries)
    POLICIES[_BNWT_HOST + "#bnwt"] = replace(POLICIES.get(_BNWT_HOST, POLICIES[_CDN_HOST]), connect_timeout=2.0,
                                             timeout=10.0, max_attempts=2, base_delay=0.25)
# The shared decision archives (decisions.py: the CDN's dec/, or BNGE_DECISIONS_URL): a lane of their own, given up
# quickly like the BNWT archives' (the server plans the cell itself when they do not answer)
_DEC_URL = (os.environ.get("BNGE_DECISIONS_URL") or "").strip()
_DEC_HOST = (urlsplit(_DEC_URL).hostname or "") if _DEC_URL.startswith(("http://", "https://")) else _CDN_HOST
POLICIES[_DEC_HOST + "#dec"] = replace(POLICIES.get(_DEC_HOST, POLICIES[_CDN_HOST]), connect_timeout=2.0,
                                       timeout=15.0, max_attempts=2, base_delay=0.25)
# IGN's Géoplateforme (France's photos, WMTS; roadz's WFS on the same host): its tile service is outside the per-IP
# rate limits and the WFS allows 30 requests/s (cartes.gouv.fr "Limites d'usage", 2026-11-07). The 6/s of the regional
# tile hosts made a fresh French place load ~3 far tiles/s (Paris on the 3D map: 198 s, 2026-11-07)
# 2026-11-11: the photo tiles 50/s, 24 at once: a cold French teleport's ~2,300 tiles kept the 25/s full for its whole
# far field (the settled time was this host's, EOX finished ~30 s earlier). cartes.gouv.fr "Limites d'usage des API"
# (read again 2026-11-11): every API has a per-IP limit "à l'exception notable de l'API « Diffusion d'images tuilées
# WMTS »" (a limit exceeded: HTTP 429 for 5 s, retry-after; netfetch honours it and its breaker stays on). The WFS
# ("ign_wfs" lane: roadz, bdtopo; 30/s published) keeps its own 25/s. BNGE_IGN_RATE: the photo tiles' rate (25: before).
_IGN_RATE = float(os.environ.get("BNGE_IGN_RATE") or 50.0)
POLICIES["data.geopf.fr#ign_wfs"] = replace(POLICIES["data.geopf.fr"], rate=25.0, burst=50.0, max_conc=12,
                                            daily_budget=100000)
POLICIES["data.geopf.fr"] = replace(POLICIES["data.geopf.fr"], rate=_IGN_RATE, burst=2.0 * _IGN_RATE,
                                    max_conc=max(12, int(_IGN_RATE / 2)), daily_budget=100000)
DEFAULT_POLICY = HostPolicy()

# What each source gives the world, and what the player gets while it does not answer (the user, 2026-10-27: "in case
# a certain source is not responding ... tell the player what that source effects"). Shown after the host in the
# status line (Server.net_note); SOURCES.md says more about each.
ROLES = {
    "basemap.nationalmap.gov": "US aerial photos; coarser satellite photos meanwhile",
    _CDN_HOST: "satellite photos of the distant land, road tags and the weekly railway, lane and sign data (the "
               "project's own tile host); the other photo sources, Pages' copy of the data meanwhile",
    "imagery.nationalmap.gov": "detailed US aerial photos (NAIP); coarser photos meanwhile",
    **({_BNWT_HOST: "map data prebaked (our own tiles: water, land use, roads, buildings, places); Overture's files "
                    "meanwhile"} if _BNWT_HOST and _BNWT_HOST != _CDN_HOST else {}),
    "apps.geo.fpac.usda.gov": "detailed US aerial photos (NAIP, second source); coarser photos meanwhile",
    "mdgeodata.md.gov": "Maryland's 6-inch aerial photos; coarser photos meanwhile",
    "tiles.maps.eox.at": "worldwide satellite photos; new ground outside the detailed-photo regions may stay plain",
    "tiles.mapterhorn.com": "terrain heights; new places use the coarser backup meanwhile",
    "download.mapterhorn.com": "the list of where detailed terrain heights exist; new places may load slower",
    "s3.amazonaws.com": "terrain heights (backup source); only matters while the main one is down too",
    "copernicus-dem-30m.s3.amazonaws.com": "terrain heights near the poles",
    "canelevation-dem.s3.ca-central-1.amazonaws.com": "Canada's lidar ground under the distant terrain; as before meanwhile",
    "nrs.objectstore.gov.bc.ca": "British Columbia's detailed aerial photos (and its lidar, if on); coarser photos meanwhile",
    "stac.overturemaps.org": "the map data's file list; the copy that comes with the app is used meanwhile",
    "overturemaps-us-west-2.s3.us-west-2.amazonaws.com":
        "roads, buildings, water and land use; places not visited before may come without them",
    "overturemapswestus2.blob.core.windows.net":
        "roads, buildings, water and land use (second source); only matters while the first is down too",
    "tiles.overturemaps.org": "roads, buildings, water and land use, fast; the other map sources stand in",
    "overturemaps-extras-us-west-2.s3.us-west-2.amazonaws.com":
        "roads, buildings, water and land use, fast (second source); the other map sources stand in",
    "overturemaps-extras-us-west-2.s3.amazonaws.com":
        "roads, buildings, water and land use, fast (third source); the other map sources stand in",
    "esa-worldcover.s3.eu-central-1.amazonaws.com":
        "land cover (ground materials, rocks, town streets); places not visited before get a plain ground meanwhile",
    "dataforgood-fb-data.s3.amazonaws.com": "the trees; places not visited before have none meanwhile",
    "gis.ngdc.noaa.gov": "sea depths; new coasts get a plain sloping sea floor meanwhile",
    "davidmegginson.github.io": "the airport list; the copy on disk is used meanwhile",
    "data.openstreetmap.us": "road details from OpenStreetMap (lanes, widths, sidewalks); estimated meanwhile",
    "s3-us-west-2.amazonaws.com": "US lidar for bridge deck heights; estimated meanwhile",
    "nvdbapiles.atlas.vegvesen.no": "Norway's measured road, bridge and tunnel heights; estimated meanwhile",
    "data.3dbag.nl": "the Netherlands' measured roof shapes and heights (3DBAG); predicted meanwhile",
    "portal.csdi.gov.hk": "Hong Kong's road lines and LiDAR index for measured road heights; estimated meanwhile",
    "bulkdata.csdi.gov.hk": "Hong Kong's LiDAR for measured road and bridge heights and the terrain; estimated "
                            "meanwhile",
    "tecnagamer.github.io": "railway details, US railroad crossing equipment, lane markings and New York's "
                            "building setbacks; estimated meanwhile",
    "gibs.earthdata.nasa.gov": "the clouds seen from space; a clear sky from orbit meanwhile",
    "api.open-meteo.com": "live weather; the weather stays as it is meanwhile",
    "downloads.sourceforge.net": "the gravity model; standard gravity meanwhile",
    "photon.komoot.io": "the map's address search (the other search sources stand in)",
    "nominatim.openstreetmap.org": "street and address search on Enter",
    "www.wikidata.org": "landmarks in the map's search",
    "routing.openstreetmap.de": "GPS routes",
    "earth.tgforever.net": "the BeamNG.Earth server list in BeamMP",
    "raw.githubusercontent.com": "the update check",
    "api.github.com": "one-click updates",
    "github.com": "one-click updates",
}


ROLES.update({f"{lang}.wikipedia.org": "landmarks in the map's search" for lang in WIKIPEDIA_LANGS})


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
_quiet = threading.local()      # set while a "predownload" request runs: its errors are no build's (no revisit.fault)


class FetchError(IOError):
    """A request that did not give data. retry_after: s before asking this host again helps."""

    def __init__(self, msg, url=None, host=None, retry_after=0.0, status=None):
        super().__init__(msg)
        self.url, self.host, self.retry_after, self.status = url, host, retry_after, status
        # (no data there, a dropped prefetch: as it should be; a saved area's download ahead: no build waited on it)
        if not isinstance(self, (NotFound, Deferred)) and not getattr(_quiet, "on", False):
            import revisit
            revisit.fault()             # (a build that goes on without it is not kept)


class NotFound(FetchError):
    """404 / 410: the host has no data there (the caller remembers it: missindex.record)."""


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
    """Is the 404 remembered for marker path ("<dir>/<y>.404": missindex.py's column index, or an old marker file
    whose mtime is the time of the 404) still valid: younger than ttl (None: forever)?"""
    t = missindex.when(path)
    if t is None:
        return False
    if ttl is None:
        return True
    return (time.time() if now_wall is None else now_wall) - t < ttl


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


def default_ssl_context():
    """The TLS context http.client makes for an HTTPS connection it is not handed one (certificates verified, ALPN
    http/1.1), made once: each connection made its own, the system's certificate store read again every time (~3 ms of
    CPU idle, far more on a loaded machine; ~1 CPU-s of a cold place's ~250 connections, py-spy 2026-11-11)."""
    make = getattr(http.client, "_create_https_context", None)
    try:
        return make(11) if make is not None else ssl.create_default_context()
    except (TypeError, ValueError, ssl.SSLError):
        return None


class HttpsTransport:
    """GET over persistent HTTP(S) connections shared by the threads (a fresh TLS handshake per tile roughly doubled
    the latency): a request takes an idle connection to its host (and proxy) or opens one, and gives it back once its
    answer is read. Each thread had its own, so the short-lived pools' threads (a lidar cell's node reads, a cell's
    tile reads) opened ~250 connections for ~800 requests of a cold place, ~170 ms each (Salina KS, 2026-11-11). Idle
    ones are kept POOL_IDLE_MAX per host, POOL_IDLE_S at most. No redirects are followed. Honours HTTP_PROXY /
    HTTPS_PROXY / NO_PROXY (and urllib.request.getproxies() as a fallback) so users behind a corporate proxy can reach
    the data sources; SOCKS proxies are not supported (PySocks is not a dependency)."""

    POOL_IDLE_MAX = 16          # idle connections kept per host (and proxy)
    POOL_IDLE_S = 30.0          # s an idle connection is kept (hosts close theirs after ~20-60 s idle: a request on an
                                # older one would likely cost a failed send and a fresh connection anyway)

    def __init__(self):
        self._idle = {}             # (scheme, netloc, proxy...) -> [(connection, monotonic time it was given back)]
        self._idle_lock = threading.Lock()
        self._tls_context = windows_ssl_context() or default_ssl_context()

    def _take(self, key):
        """An idle connection to key's host (the most recently used), or None; older ones are closed."""
        now = time.monotonic()
        stale, got = [], None
        with self._idle_lock:
            idle = self._idle.get(key)
            while idle:
                conn, at = idle.pop()
                if now - at <= self.POOL_IDLE_S:
                    got = conn
                    break
                stale.append(conn)
        for conn in stale:
            conn.close()
        return got

    def _give(self, key, conn):
        """conn (its answer read) kept for the next request to its host, or closed when enough are kept."""
        with self._idle_lock:
            idle = self._idle.setdefault(key, [])
            if len(idle) < self.POOL_IDLE_MAX:
                idle.append((conn, time.monotonic()))
                return
        conn.close()

    # --- proxy helpers ---------------------------------------------------------------
    SYSTEM_PROXIES_S = 60.0     # the system's proxy settings (getproxies: the registry on Windows) read at most this often

    _system = {"fn": None, "at": float("-inf"), "val": {}}

    @classmethod
    def _system_proxies(cls):
        """urllib.request.getproxies(), kept SYSTEM_PROXIES_S: it was read up to three times per request (on Windows
        from the registry), every tile's download (2026-11-02). A different getproxies (a test's mock) is read anew."""
        fn, now, got = urllib.request.getproxies, time.monotonic(), cls._system
        if got["fn"] is not fn or now - got["at"] > cls.SYSTEM_PROXIES_S:
            try:
                val = fn() or {}
            except Exception:
                val = {}
            got.update(fn=fn, at=now, val=val)
        return got["val"]

    @classmethod
    def _resolve_proxy(cls, scheme, host):
        """Return (proxy_host, proxy_port, proxy_user, proxy_pass) or None when no proxy applies."""
        # NO_PROXY / no_proxy: comma-separated list of hosts (or suffixes) to bypass.
        no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
        if not no_proxy:
            no_proxy = cls._system_proxies().get("no", "") or ""
        for entry in (e.strip() for e in no_proxy.split(",") if e.strip()):
            if entry == "*" or host == entry or host.endswith("." + entry):
                return None
        # env first (higher priority), then urllib's view of the system proxy
        for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            val = os.environ.get(var)
            if val:
                break
        else:
            sysp = cls._system_proxies()
            val = sysp.get(scheme) or sysp.get("all")
        if not val:
            log.debug("net: no proxy for %s (no env var / system proxy)", host)
            return None
        val = val.strip()
        if "://" in val:
            val = val.split("://", 1)[1]
        user = passw = None
        if "@" in val:
            creds, val = val.rsplit("@", 1)
            if ":" in creds:
                user, passw = creds.split(":", 1)
            else:
                user = creds
        if ":" in val:
            phost, pport = val.rsplit(":", 1)
            try:
                pport = int(pport)
            except ValueError:
                phost, pport = val, (443 if scheme == "https" else 80)
        else:
            phost, pport = val, (443 if scheme == "https" else 80)
        if phost.lower().startswith("socks"):
            log.warning("net: SOCKS proxies are not supported; ignoring %s", val)
            return None
        log.debug("net: proxy resolved for %s: %s:%d (user=%s)", host, phost, pport, user or "-")
        return phost, pport, user, passw

    def request(self, url, headers, timeout):
        """(status, {lower-case header: value}, body). A public S3 source on an IPv6-only network (GitHub #763: its
        hosts have no AAAA record) goes to the bucket's dual-stack endpoint (PR #808), but only once an IPv4 connection
        to it found no route (_no_route), and from then on for this run: Python's http.client has no Happy Eyeballs, so
        on the many networks where IPv6 is set up but broken, dual-stack hosts first would wait out every new
        connection's IPv6 attempt. Source URLs stay the cache, quota and NO_PROXY identities; proxied requests are left
        to the proxy."""
        u = urlsplit(url)
        proxy = self._resolve_proxy(u.scheme, u.hostname or "")
        host = (u.hostname or "").lower()
        auth = any(k.lower() == "authorization" for k in headers)
        if proxy is None and host in _DUALSTACK_HOSTS:
            dual = _dualstack(url, auth)
            if dual != url:
                return self._request_once(dual, headers, timeout, proxy)
        try:
            return self._request_once(url, headers, timeout, proxy)
        except OSError as e:
            if proxy is not None or not _no_route(e):
                raise
            dual = _dualstack(url, auth)
            if dual == url:
                raise
            got = self._request_once(dual, headers, timeout, proxy)
            if host not in _DUALSTACK_HOSTS:
                _DUALSTACK_HOSTS.add(host)
                log.info("net: %s has no IPv4 route here: its dual-stack endpoint from now on (%s)", host,
                         urlsplit(dual).hostname)
            return got

    def _request_once(self, url, headers, timeout, proxy):
        """request's one connection: url as given (proxy: _resolve_proxy's for it, or None). timeout: s, or (s to
        connect, s for each read after)."""
        connect_timeout, timeout = timeout if isinstance(timeout, tuple) else (timeout, timeout)
        u = urlsplit(url)
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        if proxy is None:
            key = (u.scheme, u.netloc, None)
        else:
            log.debug("net: %s -> proxy %s:%d", url, proxy[0], proxy[1])
            key = (u.scheme, u.netloc, proxy[0], proxy[1])
        fresh = False                   # (once a reused connection failed: a new one)
        while True:
            conn = None if fresh else self._take(key)
            reused = conn is not None
            if reused:                  # (this request's timeout, not the one the connection was opened with)
                conn.timeout = timeout
                sock = getattr(conn, "sock", None)
                if sock is not None:
                    try:
                        sock.settimeout(timeout)
                    except OSError:
                        pass
            else:
                if proxy is not None and u.scheme == "https":
                    phost, pport, puser, ppass = proxy
                    kwargs = {"timeout": connect_timeout}
                    if self._tls_context is not None:
                        kwargs["context"] = self._tls_context
                    conn = http.client.HTTPSConnection(phost, pport, **kwargs)
                    if puser is not None:
                        creds = base64.b64encode(f"{puser}:{ppass or ''}".encode()).decode()
                        conn.set_tunnel(u.hostname, u.port or 443,
                                       headers={"Proxy-Authorization": f"Basic {creds}"})
                    else:
                        conn.set_tunnel(u.hostname, u.port or 443)
                elif proxy is not None:
                    phost, pport, puser, ppass = proxy
                    conn = http.client.HTTPConnection(phost, pport, timeout=connect_timeout)
                    if puser is not None:
                        creds = base64.b64encode(f"{puser}:{ppass or ''}".encode()).decode()
                        conn._proxy_auth = f"Basic {creds}"
                else:
                    if u.scheme == "https":
                        kwargs = {"timeout": connect_timeout}
                        if self._tls_context is not None:
                            kwargs["context"] = self._tls_context
                        conn = http.client.HTTPSConnection(u.netloc, **kwargs)
                    else:
                        conn = http.client.HTTPConnection(u.netloc, timeout=connect_timeout)
            try:
                if not reused and connect_timeout != timeout:   # (connected within connect_timeout; reads: timeout)
                    conn.connect()
                    conn.timeout = timeout
                    conn.sock.settimeout(timeout)
                req_headers = dict(headers, Connection="keep-alive")
                if proxy is not None and u.scheme == "http":
                    phost, pport, puser, ppass = proxy
                    if getattr(conn, "_proxy_auth", None):
                        req_headers["Proxy-Authorization"] = conn._proxy_auth
                    # absolute URL for the proxy
                    full_url = url
                    conn.request("GET", full_url, headers=req_headers)
                else:
                    conn.request("GET", path, headers=req_headers)
                r = conn.getresponse()
                ioprofiler.headers_received()
                rh = {k.lower(): v for k, v in r.getheaders()}
                if r.status == 200 and (headers or {}).get("Range") and \
                        _range_ignored(headers, rh.get("content-length")):
                    # a range asked for and the whole file coming (the CDN's osmtags archive, 2,366 MB for a 9-80 KB
                    # range, twice on 2026-11-11, read into memory beside a teleport): not read, the connection
                    # dropped; the caller sees a 200 with no body (a range error to it, as before, without the file)
                    log.warning("net: %s: HTTP 200 with %s bytes for %s: not read", url, rh.get("content-length"),
                                headers.get("Range"))
                    conn.close()
                    return r.status, rh, b""
                body = r.read()
                out = r.status, rh, body
            except (http.client.HTTPException, OSError):
                conn.close()
                if not reused:          # a fresh connection failed: that is the attempt's result (the
                    raise               # Fetcher retries through the bucket, with backoff)
                fresh = True            # the server may have closed the idle keep-alive connection: reconnect once
                continue
            except BaseException:       # (not given back half read)
                conn.close()
                raise
            self._give(key, conn)
            return out


RANGE_IGNORED_MIN = 16 * 1024 * 1024    # bytes: a 200 this big (and 8x the range) for a range request is not read


def _range_ignored(headers, content_length):
    """A request with a Range header answered 200 with a body far larger than the range (the server ignored it): True
    when Content-Length is over RANGE_IGNORED_MIN and 8x the range asked for (a small file sent whole is read)."""
    rng = (headers or {}).get("Range")
    if not rng or not content_length:
        return False
    try:
        n = int(content_length)
        a, b = rng.split("=", 1)[1].split(",", 1)[0].split("-", 1)
        want = int(b) - int(a) + 1 if a and b else 0
    except (ValueError, IndexError):
        return False
    return n > RANGE_IGNORED_MIN and n > 8 * max(want, 1)


# IPv6-only networks (GitHub #763): the public S3 hosts that answered "no route" over IPv4 this run, used through
# their dual-stack endpoints since (transport request)
_DUALSTACK_HOSTS = set()
# errno / WinError of a connection with no route to the address (an IPv6-only network, no NAT64): ENETUNREACH,
# EHOSTUNREACH, EADDRNOTAVAIL; WSAENETUNREACH 10051, WSAEHOSTUNREACH 10065, WSAEADDRNOTAVAIL 10049
_NO_ROUTE = {errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EADDRNOTAVAIL, 10051, 10065, 10049}


def _no_route(e):
    """Is OSError e a connection that found no route to the host's (IPv4) address?"""
    return getattr(e, "winerror", None) in _NO_ROUTE or getattr(e, "errno", None) in _NO_ROUTE


def _dualstack(url, authorized):
    """The bucket's dual-stack endpoint URL for a known public S3 source (native earthfast s3_dualstack_url), else
    url."""
    import fast
    fast.require(153, "S3 dual-stack endpoints")
    return fast._ef.s3_dualstack_url(url, bool(authorized))


# --- download speed ---------------------------------------------------------------------------
RATE_MIN_BYTES = 512 * 1024     # a response this large or larger is a speed sample (smaller ones are mostly latency)
RATE_WINDOW_S = 120.0           # the samples of the last this many seconds count ...
RATE_MIN_SAMPLES = 3            # ... at least this many of them
SLOW_MBPS = 0.4                 # MB/s a large response comes in at or slower (the median): a slow connection. A Windows
                                # PC on Wi-Fi got 0.02-0.15 MB/s a request; a first visit's ~75 MB of map data took
                                # minutes and the roads looked stuck (GitHub #241); a wired PC gets 5-15 MB/s


# --- per-host state ----------------------------------------------------------------------------
COUNTERS = ("requests", "bytes", "s2xx", "s3xx", "s4xx", "s404", "s429", "s5xx", "errors", "retries",
            "trips", "deferred", "unavailable",
            # our own limits (telemetry: how often the world waits on them, the user 2026-11-11: "make sure your
            # checking our own set rate limits"): demand / urgent requests that waited WAIT_MS_MIN or more for a slot
            # or a token, the ms they waited, those refused after max_wait, and refusals by the daily budgets
            "waited", "wait_ms", "limit_fail", "budget")
WAIT_MS_MIN = 5


class Host:
    def __init__(self, name, policy, clock, sleep):
        self.name, self.policy = name, policy
        self.bucket = TokenBucket(policy.rate, policy.burst, clock, sleep)
        self.sem = threading.BoundedSemaphore(max(1, policy.max_conc))
        self.urgent_sem = threading.BoundedSemaphore(URGENT_CONC)  # (urgent requests' own slots, then the shared ones)
        self.breaker = CircuitBreaker(policy.breaker_threshold, policy.breaker_window, policy.breaker_open,
                                      policy.breaker_max, clock)
        self.day = dict.fromkeys(COUNTERS, 0)       # this UTC day (persisted)
        self.total = dict.fromkeys(COUNTERS, 0)     # this process's lifetime (telemetry.py sums every process's)
        self.minute = dict.fromkeys(COUNTERS, 0)    # since the last summary
        self.warned = 0.0                           # wall time of the 80 % budget warning (0 = none)
        self.warned_over = 0.0                      # ... and of the last over-budget one
        self.half_rate = False


# --- every process's totals, for telemetry.py -------------------------------------------------------------------------
# BNE_NETSTATS_PROC (earthd sets it before its worker processes start, so they inherit it): a folder where every process
# writes its fetchers' lifetime per-host counters (<pid>.json, at most every PROC_WRITE_S, and at exit); the main
# process's telemetry sums what changed. The worker processes' fetchers keep no stats_dir of their own.
_PROC_DIR = os.environ.get("BNE_NETSTATS_PROC") or None
PROC_WRITE_S = 30.0
_ALL = weakref.WeakSet()            # this process's fetchers
_proc_state = {"at": 0.0, "lock": threading.Lock(), "exit": False}


def process_totals():
    """{host: {counter: n}} of every fetcher of this process, over its lifetime."""
    out = {}
    for f in list(_ALL):
        with f._lock:
            for name, h in f._hosts.items():
                d = out.setdefault(name, dict.fromkeys(COUNTERS, 0))
                for k, v in h.total.items():
                    d[k] += v
    return out


def write_process_totals():
    """This process's totals to <BNE_NETSTATS_PROC>/<pid>.json (atomic)."""
    if not _PROC_DIR:
        return
    try:
        os.makedirs(_PROC_DIR, exist_ok=True)
        path = os.path.join(_PROC_DIR, f"{os.getpid()}.json")
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"pid": os.getpid(), "at": time.time(), "hosts": process_totals()}, fh)
        os.replace(tmp, path)
    except OSError as e:
        log.debug("net: process totals: %s", e)


def _proc_writer():
    st = _proc_state
    now = time.monotonic()
    if now - st["at"] < PROC_WRITE_S or not st["lock"].acquire(blocking=False):
        return
    try:
        st["at"] = now
        if not st["exit"]:
            import atexit
            atexit.register(write_process_totals)
            st["exit"] = True
        write_process_totals()
    finally:
        st["lock"].release()


# --- one host's admission shared by the server's processes ------------------------------------------------------------
# A host whose policy is shared (the one-at-a-time export services: Maryland's md6in, NAIP, the regional WMS / ArcGIS
# exports) is admitted across every process of the server, not each process alone: the road-tile processes fetch the
# roofs' photos from the same hosts as the main process's warm (buildings.roof_photo, imagery.fetch_lazy), and each
# process's own bucket, request slots and breaker let four of them ask md6in (1 request/s, one at a time) four times as
# fast, and none saw another's Retry-After or outage (2026-11-13 review). BNGE_NET_SHARED (earthd sets it before its
# worker processes start, so they inherit it): the folder of the gates; unset (tests, tools, a process alone), each
# process's limits as before. Not BNE_: those are part of the revisit cache's code key.
SHARED_ENV = "BNGE_NET_SHARED"
SHARED_DIR = os.environ.get(SHARED_ENV) or None
SHARED_POLL_S = 0.02                # s between tries for a request slot another process holds
GATE_LOG = os.environ.get("BNGE_GATE_LOG") or None      # (measuring: a line per gate admission that waited)
_SHARED_FMT = "<3d"                 # the gate file: tokens, their time (monotonic), blocked until (monotonic)
_SHARED_SIZE = 24
IS_WIN = os.name == "nt"


def _flock(fd, blocking=True):
    """An exclusive lock on fd's file (byte 0 on Windows); True when held. Released by the system when the process
    ends, so a dead process never holds a gate."""
    if IS_WIN:
        import msvcrt
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                if not blocking:
                    return False
                time.sleep(SHARED_POLL_S)
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        return True
    except BlockingIOError:
        return False


def _funlock(fd):
    try:
        if IS_WIN:
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


class SharedGate:
    """One host's admission for every process that opens the same folder: a token bucket (rate, burst) and a
    blocked-until time in <dir>/<host>.gate, read and written under its lock, and max_conc request slots, files
    <host>.<i>.slot each locked while a request runs. Times are time.monotonic (one clock for the machine's
    processes). A demand / urgent request waits for a slot and a token (up to max_wait: RateLimited); prefetch and
    background ones take them only when free (Deferred). blocked(): a Retry-After or an open breaker another process
    saw."""

    def __init__(self, d, host, policy, clock=time.monotonic, sleep=time.sleep):
        self.rate, self.burst = float(policy.rate), float(policy.burst)
        self.max_conc = max(1, int(policy.max_conc))
        self.clock, self.sleep = clock, sleep
        safe = "".join(c if c.isalnum() or c in "-." else "_" for c in host)
        os.makedirs(d, exist_ok=True)
        self.path = os.path.join(d, safe + ".gate")
        self.slots = [os.path.join(d, f"{safe}.{i}.slot") for i in range(self.max_conc)]

    def _state(self, fn):
        """fn(tokens, t, blocked, now) -> (tokens, t, blocked, result) under the gate's lock; result."""
        import struct
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
        try:
            _flock(fd)
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                raw = os.read(fd, _SHARED_SIZE)
                now = self.clock()
                if len(raw) == _SHARED_SIZE:
                    tokens, t, blocked = struct.unpack(_SHARED_FMT, raw)
                    if t > now + 1.0:           # (written before a restart of the clock: the machine's reboot)
                        tokens, t, blocked = self.burst, now, 0.0
                else:
                    tokens, t, blocked = self.burst, now, 0.0
                tokens = min(self.burst, tokens + max(0.0, now - t) * self.rate)
                tokens, t, blocked, res = fn(tokens, max(t, now), blocked, now)
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, struct.pack(_SHARED_FMT, tokens, t, blocked))
                return res
            finally:
                _funlock(fd)
        finally:
            os.close(fd)

    def blocked(self):
        """Seconds the host is blocked for (another process's Retry-After or open breaker), 0 when not."""
        return self._state(lambda tk, t, b, now: (tk, t, b, max(0.0, b - now)))

    def block(self, seconds):
        """The host blocked for every process for seconds (at least: a longer block stands)."""
        self._state(lambda tk, t, b, now: (tk, t, max(b, now + float(seconds)), None))

    def _slot(self, wait_s):
        """A request slot (its open fd, locked), or None when none came free within wait_s."""
        deadline = self.clock() + max(0.0, wait_s)
        while True:
            for p in self.slots:
                fd = os.open(p, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
                if _flock(fd, blocking=False):
                    return fd
                os.close(fd)
            if self.clock() >= deadline:
                return None
            self.sleep(SHARED_POLL_S)

    def acquire(self, kind, max_wait, url=None, host=None):
        """A slot and a token for one request: (the slot: release() it after the request, the seconds waited for
        them). Raises HostUnavailable (blocked), RateLimited (a demand that would wait past max_wait), Deferred
        (prefetch / background: busy); OSError (the gate's files: a full disk) with nothing held."""
        t_start = self.clock()
        wait = kind in ("demand", "urgent")
        b = self.blocked()
        if b > 0:
            raise HostUnavailable(f"{host}: unavailable for {b:.0f} s (another process's answer)", url, host,
                                  retry_after=b)
        fd = self._slot(max_wait if wait else 0.0)
        if fd is None:
            if wait:
                raise RateLimited(f"{host}: no free request slot within {max_wait:.0f} s (shared)", url, host)
            raise Deferred(f"{host}: busy (shared), {kind} dropped", url, host)

        def take(tk, t, blocked, now):
            if blocked > now:
                return tk, t, blocked, ("blocked", blocked - now)
            if tk >= 1.0 - TOKEN_EPS:
                return tk - 1.0, t, blocked, ("go", 0.0)
            need = (1.0 - tk) / self.rate
            if not wait or need > max_wait:
                return tk, t, blocked, ("full", need)
            return tk - 1.0, t, blocked, ("go", need)    # (reserved: the debt is paid by the refill while it sleeps)
        try:
            what, s = self._state(take)
            if what == "blocked":
                raise HostUnavailable(f"{host}: unavailable for {s:.0f} s (another process's answer)", url, host,
                                      retry_after=s)
            if what == "full":
                if wait:
                    raise RateLimited(f"{host}: rate limited for {s:.1f} s (shared)", url, host, retry_after=s)
                raise Deferred(f"{host}: bucket low (shared), {kind} dropped", url, host)
            if s > 0:
                self.sleep(s)
        except BaseException:
            self.release(fd)
            raise
        waited = self.clock() - t_start
        if GATE_LOG and waited >= WAIT_MS_MIN / 1000:
            try:
                with open(GATE_LOG, "a") as fh:
                    fh.write(f"{time.time():.3f} {os.getpid()} {host} {kind} {waited:.3f}\n")
            except OSError:
                pass
        return fd, waited

    @staticmethod
    def release(fd):
        if fd is None:
            return
        _funlock(fd)
        try:
            os.close(fd)
        except OSError:
            pass


_GATES = {}
_GATES_LOCK = threading.Lock()


def shared_gate(host, policy, d=None):
    """The process's SharedGate of host (policy.shared, a folder set: SHARED_DIR), or None."""
    d = d or SHARED_DIR
    if not d or not getattr(policy, "shared", False):
        return None
    key = (d, host)
    with _GATES_LOCK:
        g = _GATES.get(key)
        if g is None:
            try:
                g = _GATES[key] = SharedGate(d, host, policy)
            except OSError as e:
                log.debug("net: %s's shared gate: %s", host, e)
                return None
        return g


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
        self._rates = collections.deque(maxlen=64)     # (clock, MB/s) of the large responses (throughput)
        _ALL.add(self)

    def throughput(self):
        """The median speed (MB/s) the large responses of the last RATE_WINDOW_S came in at, or None (too few)."""
        now = self.clock()
        with self._lock:
            r = sorted(v for t, v in self._rates if now - t <= RATE_WINDOW_S)
        return r[len(r) // 2] if len(r) >= RATE_MIN_SAMPLES else None

    def slow(self):
        """The throughput when it is SLOW_MBPS or less (a slow connection), else None."""
        r = self.throughput()
        return r if r is not None and r <= SLOW_MBPS else None

    # --- hosts and policies ------------------------------------------------------------------
    def policy(self, host):
        # (a lane, "host#name": its own policy where there is one ("data.geopf.fr#ign_wfs"), else the host's; its own
        # bucket and breaker: get's lane)
        if "#" in host and host in self.policies:
            return self.policies[host]
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
        """A 404 remembered for url at marker path (missindex.py) is valid: within the host's miss_ttl, or the host
        cannot be asked now (offline / breaker open), so an expired one still counts as "no data" instead of an
        error."""
        t = missindex.when(path)
        if t is None:
            return False
        host = host_of(url)
        ttl = self.policy(host).miss_ttl
        return ttl is None or self.wall() - t < ttl or not self.available(host)

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

    def budget_share(self, url):
        """The share of url's host's soft daily budget used today (0 without one)."""
        h = self.host(host_of(url))
        if not h.policy.daily_budget:
            return 0.0
        with self._lock:
            return h.day["requests"] / h.policy.daily_budget

    def _check_budget(self, h, url, kind):
        p = h.policy
        with self._lock:
            used = h.day["requests"]
        if p.daily_hard is not None and used >= p.daily_hard:
            self._bump(h, "budget")
            raise BudgetExceeded(f"{h.name}: daily limit of {p.daily_hard} requests reached", url, h.name)
        if kind == "predownload" and p.daily_hard is not None and used >= PREDOWNLOAD_SHARE * p.daily_hard:
            raise BudgetExceeded(f"{h.name}: past {round(100 * PREDOWNLOAD_SHARE)} % of the daily limit (downloads "
                                 "ahead)", url, h.name)
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
        if kind == "predownload" and used >= PREDOWNLOAD_SHARE * p.daily_budget:
            raise BudgetExceeded(f"{h.name}: past {round(100 * PREDOWNLOAD_SHARE)} % of the daily budget (downloads "
                                 "ahead)", url, h.name)
        if kind not in ("demand", "urgent") and used >= BUDGET_WARN * p.daily_budget:
            self._bump(h, "budget")
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
        (_release gives it back). A demand / urgent request's wait for them is counted (waited, wait_ms, limit_fail)."""
        if kind not in ("urgent", "demand"):
            return self._acquire_now(h, url, kind)
        t0 = self.clock()
        netstate.wait(h.name, True)
        try:
            sem = self._acquire_now(h, url, kind)
        except RateLimited:
            self._bump(h, "limit_fail")
            self._bump(h, "wait_ms", int((self.clock() - t0) * 1000))
            raise
        finally:
            netstate.wait(h.name, False)
        ms = int((self.clock() - t0) * 1000)
        if ms >= WAIT_MS_MIN:
            self._bump(h, "waited")
            self._bump(h, "wait_ms", ms)
        return sem

    def _acquire_now(self, h, url, kind):
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
        if kind != "predownload":
            return self._get(url, kind, headers, lane)
        prev = getattr(_quiet, "on", False)
        _quiet.on = True        # (its failures mark no build incomplete: revisit.fault)
        try:
            return self._get(url, kind, headers, lane)
        finally:
            _quiet.on = prev

    def _get(self, url, kind, headers, lane):
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
                gate, slot = shared_gate(h.name.split("#", 1)[0], p), None
                try:
                    slot = self._gate_admit(h, gate, url, kind)
                except BaseException:
                    sem.release()       # (every way out without the request: the slot is this process's again)
                    raise
                try:
                    self._count(h, "requests")
                    t0 = self.clock()
                    netstate.begin(h.name)  # (the loading line's "downloading" part, across the server's processes)
                    body = None
                    try:
                        status, rh, body = ioprofiler.request(
                            self.transport, url, hd,
                            p.timeout if p.connect_timeout is None else (p.connect_timeout, p.timeout))
                    except (http.client.HTTPException, OSError) as e:
                        self._count(h, "errors")
                        self._failed(h, gate)
                        probe = False
                        err = FetchFailed(f"{url}: {type(e).__name__}: {e}", url, h.name)
                        continue
                    elapsed = self.clock() - t0
                    if TEST_MBPS > 0 or TEST_LATENCY_S > 0:       # (testing: a slow connection)
                        want = TEST_LATENCY_S + (len(body or b"") / (TEST_MBPS * 1e6) if TEST_MBPS > 0 else 0.0)
                        if want > elapsed:
                            time.sleep(want - elapsed)
                            elapsed = want
                    if body and len(body) >= RATE_MIN_BYTES and elapsed > 0:
                        with self._lock:
                            self._rates.append((t0 + elapsed, len(body) / elapsed / 1e6))
                    if NETLOG:
                        _netlog(url, hd, status, body)
                finally:
                    netstate.end(len(body) if body else 0, h.name)
                    SharedGate.release(slot)
                    sem.release()
                res = self._classify(h, url, status, {k.lower(): v for k, v in (rh or {}).items()}, body, elapsed)
                probe = False               # the breaker has its verdict
                if gate is not None:        # (the others hold off too: its Retry-After, its breaker opened here)
                    if isinstance(res, RateLimited) and res.retry_after:
                        self._gate_block(gate, res.retry_after)
                    elif h.breaker.is_open() and h.breaker.open_until > self.clock():
                        self._gate_block(gate, h.breaker.open_until - self.clock())
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
        if status == 304:                   # "not modified": the answer to a request with If-None-Match (addons.py's
            self._succeeded(h)              # GitHub pages); only such a request gets one
            return Response(status, body or b"", rh, elapsed)
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

    def _gate_admit(self, h, gate, url, kind):
        """The shared gate's slot for one attempt (None: no gate, or its files failed: this process's own limits
        alone, the request not refused for a full disk); its wait counted with the others' (waited, wait_ms)."""
        if gate is None:
            return None
        try:
            fd, waited = gate.acquire(kind, h.policy.max_wait, url, h.name)
        except FetchError as e:
            self._bump(h, "deferred" if isinstance(e, Deferred) else "unavailable"
                       if isinstance(e, HostUnavailable) else "limit_fail")
            raise
        except OSError as e:
            log.debug("net: %s's shared gate: %s (this process's limits alone)", h.name, e)
            return None
        ms = int(waited * 1000)
        if kind in ("demand", "urgent") and ms >= WAIT_MS_MIN:
            self._bump(h, "waited")
            self._bump(h, "wait_ms", ms)
        return fd

    @staticmethod
    def _gate_block(gate, seconds):
        try:
            gate.block(seconds)
        except OSError as e:
            log.debug("net: shared gate: %s", e)

    def _failed(self, h, gate=None):
        if h.breaker.failure():
            self._count(h, "trips")
            log.warning("net: %s: circuit open for %.0f s", h.name, h.breaker.open_s)
            if gate is not None:
                self._gate_block(gate, h.breaker.open_s)

    def _succeeded(self, h):
        if h.breaker.success():
            log.info("net: %s: circuit closed", h.name)

    @ioprofiler.session_request
    @contextmanager
    def session(self, url, kind="demand", lane=None):
        """For a caller that does its own HTTP (gravity.py's resumable download through urllib, which
        follows SourceForge's mirror redirects): admission, a slot and a token for one request. Inside,
        set `s.status` and `s.nbytes`; an exception counts as a failure (an HTTPError's status and
        Retry-After are honoured) and is re-raised. lane: as get's."""
        h, probe = self._admit(url, kind, lane)
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
            h.total[what] += n
        if _PROC_DIR and what == "requests":
            _proc_writer()

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
