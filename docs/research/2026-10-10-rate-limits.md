# Rate limits and usage rules of every online service (research 2026-10-10)

Scope: every host the companion server (`server/`) contacts today, on a feature branch, or in a
plan (CREDITS.md, the imagery, weather, GPS, world-map and world-data reports). For each one this
report gives the official limits and rules, with source URL and quote. It then sets those against
our demand, measured from the code, and proposes a fetch layer that enforces the limits.

Labels:
- **[Q]** quoted from the official page at the URL given (fetched 2026-10-10).
- **[H]** response header read with one GET today (generic user agent, no personal data).
- **[M]** measured from our code: `farfield.select` plus the zoom rules of `imagery.py` and `dem.py`,
  driven offline. Script: scratchpad `ratesim/sim.py`. No network.
- **[I]** inference or recommendation.

No load testing. Four single GETs were made, one each to USGS, EOX, Mapterhorn and AWS, to read
their headers. Neither the game nor its MCP server was contacted.

---

## 0. Summary

**Top risks, worst first:**

1. **No rate limiter anywhere, only thread pools.**
   - The imagery pool has 6 workers (`imagery.py:51`); the DEM pool has 8 (`dem.py`, `Dem(fetch_workers=8)`).
   - At start-up the server fetches about **1,430 imagery tiles and about 180–400 DEM tiles**,
     limited only by latency [M].
   - At the measured latencies that is about **30 req/s to USGS** (186 ms) and **up to about 80 req/s
     to Mapterhorn**.
   - The same burst follows every teleport.
2. **EOX applies rate limits but publishes no numbers** [Q maps.eox.at]. Outside the US, EOX is
   the only imagery source:
   - about 250–300 req/min at 134 m/s;
   - about 450 req/min in a 250 m/s flight;
   - about 1,200–1,430 in the first minute [M].
   - `eox2023` and `eox2016` are on the **same host**. When 2023 gets throttled, the chain falls back
     to 2016 and doubles the load on a host that is already pushing back (`imagery.py:265` backs off
     per *source*, not per host).
3. **429/503 handling ignores `Retry-After`.**
   - DEM retries 4× with 0.5–4 s sleeps and no circuit breaker (`dem.py` `_download`). Every new tile
     starts another round of retries.
   - Imagery tries 3× and then pauses that source for 30 s.
   - Neither code path reads `Retry-After` or treats 429 differently from 500.
4. **Policy-bound services must never be streamed.** OSM raster/vector tiles, Nominatim and Overpass
   forbid prefetch, bulk download and offline use [Q]. Our streaming model is prefetch by design, so
   these hosts need a hard "interactive only" policy in code, not just a note in a document.
5. **National high-res imagery multiplies demand.**
   - A source with data to z18–19 needs about **1,400 req/min (23 req/s) at 134 m/s** and about
     300–400 req/min at 30 m/s [M].
   - swisstopo publishes **1,200 req/min** for its WMTS [Q], so we would exceed it.
   - The ArcGIS export, WMS and NAIP services render each image on demand (NAIP took 1.5–12 s per
     request today), so even 1 req/s is heavy for them.
6. **The User-Agent is `BeamNG.Earth/0.1 (+personal mod)`.** It names the app but gives no contact
   or URL. OSM and Nominatim need an identifying UA and recommend a contact [Q].
7. **Caches never expire.**
   - Tiles and `.404` markers stay on disk forever. That is fine for the licences.
   - Mapterhorn 404 markers stop us from picking up lidar that Mapterhorn adds later.
   - USGS sends `max-age=86400`, EOX and Mapterhorn `max-age=604800` [H]; we ignore all three. That is
     fine for static mosaics, but the generic fetch layer must honour them for OSM-type hosts.

**Actions:**
- A shared `server/netfetch.py` (§5) with a per-host token bucket and concurrency cap.
- Exponential backoff with full jitter.
- Honour `Retry-After` and use a per-host circuit breaker that fails over to the next source.
- Per-host daily budgets with logging.
- A per-host "interactive only / no prefetch / no offline" flag.
- Cache TTLs per licence, plus 404 marker TTLs.
- UA `BeamNG.Earth/<version> (+https://github.com/TecnaGamer/BeamNG.Earth)`.
- National sources limited to near levels and requested as 512 px exports.

---

## 1. Live header check (one GET each, 2026-10-10) [H]

| Host | Status | Rate-limit headers | Cache-Control | Served by |
|---|---|---|---|---|
| `basemap.nationalmap.gov` (USGS z10 tile) | 200 | none | `max-age=86400` | CloudFront (`x-cache: Hit from cloudfront`) in front of nginx/ArcGIS |
| `tiles.maps.eox.at` (s2cloudless-2023 z10) | 200 | none | `max-age=604800` | Apache 2.4.59, HTTP/2, no CDN header |
| `tiles.mapterhorn.com` (z10) | 200 | none | `public, max-age=604800` | Cloudflare (`cf-cache-status: HIT`, age 5.4 days) |
| `s3.amazonaws.com/elevation-tiles-prod` (z10) | 200 | none | none (`Last-Modified: 2017-11-12`) | Amazon S3 |

No host sends `X-RateLimit-*` or `Retry-After` on a normal 200, so the limiter cannot learn a budget
in advance. It can only react to 429/503, plus `Retry-After` when the server sends one.

EOX is the only origin here without a CDN in front of it, which explains why it is the one that
says it rate-limits.

---

## 2. Our demand, measured from the code [M]

### 2.1 How the server fetches today

| Item | Imagery (`server/imagery.py`) | DEM (`server/dem.py`) | Weather (`feature/weather:server/weather.py`) | Gravity (`feature/gravity:server/gravity.py`) |
|---|---|---|---|---|
| Hosts | basemap.nationalmap.gov, tiles.maps.eox.at | tiles.mapterhorn.com, s3.amazonaws.com | api.open-meteo.com | downloads.sourceforge.net (redirects to mirrors) |
| Concurrency | pool of 6 (`FETCH_WORKERS`); one persistent HTTPS connection per thread and host | pool of 8; urllib, new connection per request | one request in flight per connection (`Tracker.inflight`) | 1 |
| Retries | `ATTEMPTS = 3`, sleeps 0.5 s then 1 s; then the **source** is "down" for `SOURCE_BACKOFF = 30 s` | 4 attempts, sleeps 0.5/1/2/4 s; no backoff state, no breaker; the failed tile is retried on its next request | failure keeps the last weather; retry after `RETRY_S = 120 s` | 10 attempts, sleep min(2^i, 10) s, resumes with Range |
| 429/503/Retry-After | treated like any non-200; Retry-After ignored | same | same | same |
| 400/404 | 400 **and** 404 write a permanent `.404` marker | 404 writes a permanent `.404` marker | n/a | n/a |
| Disk cache | `<cache>/imagery/<source>/z/x/y.jpg`, forever | `<cache>/dem/<source>/z/x/y.{webp,png}`, forever | in memory, (0.1° cell, 15-min slot), 256 entries | 74 MB archive, once, SHA-256 checked |
| Memory cache | LRU 512 decoded tiles | LRU 512 tiles | | |
| Prefetch | `Imagery.prefetch` for the next `IMAGERY_PREFETCH = 12` queued tiles (`earthd.py:1303`); terrain prefetch of the predicted block | the predicted block's heightmap (`Connection.maybe_prefetch`) | none | n/a |
| User-Agent | `BeamNG.Earth/0.1 (+personal mod)` (`dem.USER_AGENT`) | same | same | same (its own copy) |
| Dedup | in-flight futures per key | in-flight futures per key | none across connections (fine with one) | n/a |

### 2.2 Requests per minute by scenario

Settings and method:
- Default CLI layout: mesh, 64 cells, textures 256/256/256/512 px, 1024² block, 80 km square;
  horizon mode for the flight.
- The real `farfield.select` was run at 4 Hz with hysteresis and HOLD_S, as `tests/test_demand.py`
  runs it.
- For every added tile, the imagery source tiles its texture needs (`imagery.render` zoom and mosaic
  rules) and the DEM tiles its build samples (vertex zoom plus reference zoom) were counted.
- A key not seen before counts as one HTTP request, i.e. a disk-cache miss.
- Rebase every 10 km (the mod's default `rebaseDistance`).
- Start points: Baltimore (US: USGS z16 first) and Munich (EU: EOX z14 first).

| Scenario | Tiles added/min | Imagery req/min US (USGS) | Imagery req/min EU (EOX) | Mapterhorn req/min (lidar area) | + AWS req/min where Mapterhorn 404s above z12 |
|---|---|---|---|---|---|
| **Session start / teleport** (first selection, cold cache) | 300 tiles | **1,431** in a burst | **1,185** | 177 (US) / 237 (EU) | +214 (US) |
| 30 m/s, steady | 190–230 | 90–230 (median ~150) | 6–60 | 16–24 | +25–45 |
| 30 m/s, minute with a rebase | 517 | 276 | 202 | 37 | +60 |
| 134 m/s (rebase every ~75 s) | 940–1,260 | 600–800 (**~13 req/s**) | 70–310 (~5 req/s) | 87–119 (~2 req/s) | +170 |
| 134 m/s without rebases (for comparison) | 935–983 | 607–737 | | 87–117 | |
| 250 m/s flight at 10 km, horizon disc 468 km | ~1,800 | **1,240–1,310 (~21 req/s)** | 310–450 (~7 req/s) | 180–200 | +380 |
| Same 134 m/s drive with a z19-capable national source first | 935–983 | **1,370–1,480 (~23 req/s)** | | | |
| Same 30 m/s drive with a z19 source | 190–230 | 270–400 | | | |

Notes:
- **A rebase costs ~+100 imagery and ~+15 DEM requests** (re-keyed tiles at new boundaries). Most
  source tiles are reused from disk because the source grid is global [M].
- **Teleport = session start.**
  - At 6 workers and 186–540 ms per tile, the 1,431-tile burst takes ~45–130 s at **11–32 req/s**.
  - Against NAIP's 10 s per request it would take about 40 min, so NAIP cannot be a streaming source
    for far levels.
- **The flight case is inflated.** `select()` uses horizontal distance only, so a plane at 10 km
  still requests L-1/L0 tiles (z17–18 texels) under itself that nobody can see. Selecting by 3D
  distance (eye height) would remove most of the 21 req/s. **[I] Worth doing** for rate limits as well
  as CPU.
- **The concurrency cap is the only limiter today.** The real peak rate is ≈ workers / latency: USGS
  6 / 0.19 s ≈ 32 req/s; Mapterhorn (Cloudflare edge, ~0.1 s) 8 / 0.1 ≈ 80 req/s. A faster network
  means more requests per second against the origin.
- **De-shadow** (`feature/deshadow`, off by default):
  - Per USGS z12 region: 16 z14 tiles + 4 z16 tiles + DEM z13.
  - Regions are queued within 16–22 km of the anchor, so about 35 regions ≈ **~700 USGS +
    ~150–400 Mapterhorn requests** at a new area.
  - About 200 more per rebase ahead (estimate from the region constants [I]).
- **Weather** (`feature/weather`):
  - One GET at start, then every 10 min (`REFRESH_S`) or every 25 km (`REFETCH_M`). The 15-min cache
    slot absorbs some of them.
  - Worst case at 250 m/s: 25 km per 100 s, so **~36 requests/h**.
  - Each request asks for 18 `current` + 2 `hourly` variables = **20 variables, which Open-Meteo
    counts as 2.0 calls** [Q pricing]. So ~72 calls/h worst, ~10–12 calls/h normally.
- **Gravity:** one 74 MB download per install; resume and checksum already implemented.

### 2.3 Daily totals (cold cache, one player) [M, I]

| Session | USGS | EOX (non-US) | Mapterhorn | AWS (no lidar) | Open-Meteo calls |
|---|---|---|---|---|---|
| 1 h at 30 m/s (108 km, ~11 rebases) | ~11,000 | ~3,000 | ~1,500 | ~2,500 | ~12–14 |
| 1 h at 134 m/s (482 km) | ~44,000 | ~15,000 | ~6,500 | ~10,000 | ~40 |
| 1 h flight 250 m/s (900 km) | ~76,000 | ~25,000 | ~11,500 | ~23,000 | ~72 |

These are per player. A popular public release multiplies them by the player count, which is
what makes "no published limit" hosts (EOX, USGS, Mapterhorn) a project risk, not just a session
risk.

---

## 3. Limits per service

Risk scale:
- **low**: our worst case is far below a published limit, or the host is a CDN or object store.
- **med**: no published number, or within a factor of ~3 of one.
- **high**: our worst case exceeds the limit or breaks a rule.
- **block**: the rules forbid our use pattern.

### 3.1 In use (main)

**USGS The National Map ImageryOnly** (`basemap.nationalmap.gov`, ArcGIS Server tile cache behind CloudFront)
- **Official limit and rules:** No rate limit published (searched usgs.gov). Licence [Q
  https://www.usgs.gov/faqs/are-there-any-costs-or-restrictions-usage-data-downloaded-national-map]:
  "Data from The National Map is free and in the public domain." "There are no restrictions on
  downloaded data"; they request the statement "Data available from U.S. Geological Survey,
  National Geospatial Program." Header: `max-age=86400` [H]. No offline or caching restriction.
  Consequence of excess: unpublished; ArcGIS Server answers 429/503 under load.
- **Our worst case:** 1,431 burst at start/teleport (~32 req/s at 6 conc.); 13 req/s at 134 m/s;
  21 req/s in flight; de-shadow +700 per area.
- **Risk:** **med** (unpublished; bursts of 30 req/s per player).
- **Action:** 6 concurrent, 15 req/s sustained, burst 60. Prefetch only above 50 % bucket. Soft
  budget 60k/day, then log a warning. Breaker → EOX. Keep the cache forever. Ask tnm_help@usgs.gov
  before a public release.

**EOX Sentinel-2 cloudless 2023 / 2016** (`tiles.maps.eox.at`)
- **Official limit and rules:**
  - Service [Q https://maps.eox.at/]: "provided as is without any guarantee"; "Content and
    availability may change without notice"; **"We had to start applying rate limiting"**, and users
    may get "HTTP errors and redirects to this page"; a "dedicated service under SLA" is offered. No
    numbers.
  - Licence [Q https://cloudless.eox.at/license-non-commercial]: 2018–2025 "licensed under the
    Creative Commons Attribution-NonCommercial-ShareAlike 4.0"; 2016 "Creative Commons Attribution
    4.0". "The maximum request size is limited to 4096px".
  - **Attribution changed (June 2026):** "EOxCloudless https://cloudless.eox.at by EOX IT Services
    GmbH (Contains modified Copernicus Sentinel data 2023)". The 2016 one reads "...data 2016 & 2017".
  - Commercial use needs an "EOX Commercial Attribution-RestrictedUse 1.2 License" [Q
    https://cloudless.eox.at/documentation/license].
  - Bulk delivery is a paid service [Q https://cloudless.eox.at/documentation/usage].
  - Header: `max-age=604800` [H].
- **Our worst case:** 1,185 burst at start (EU); 5 req/s at 134 m/s; 7.5 req/s in flight. In the US,
  EOX only serves far levels (USGS min zoom 10). The 2016 fallback hits the same host.
- **Risk:** **high** (stated rate limiting, no numbers, no CDN, our only global imagery).
- **Action:**
  - 4 concurrent, 6 req/s, burst 30.
  - One breaker per **host**, shared by `eox2023` and `eox2016`.
  - Prefetch only above 50 % bucket.
  - Treat a 3xx redirect to maps.eox.at as a 429.
  - Soft budget 20k/day per player.
  - Update the attribution strings (`imagery.py:88-97`, CREDITS.md) to the EOxCloudless wording.
  - Email EOX (cloudless@eox.at) about a free mod's expected volume before release.
  - Bundle low-zoom (z0–6) EOX 2016 (CC BY) tiles with the mod so far levels never hit the network.

**Mapterhorn terrain** (`tiles.mapterhorn.com`)
- **Official limit and rules:** No terms, limits or caching rules on mapterhorn.com or
  /data-access [Q https://mapterhorn.com/data-access]. The PMTiles archives are the main
  distribution ("The tiles are bundled into multiple PMTiles archives"); single tiles "can be
  retrieved from a zxy-endpoint". "Cloudflare is generously supporting Mapterhorn with R2 Object
  Storage, Workers, and bandwidth." Attribution per source at https://mapterhorn.com/attribution.
  Header: `public, max-age=604800`, Cloudflare edge hits [H].
- **Our worst case:** 177–237 burst at start (up to ~80 req/s at 8 conc.); ~2 req/s at 134 m/s;
  ~3.3 req/s in flight; de-shadow +150–400 per area.
- **Risk:** **med** (unpublished; sponsored bandwidth; a volunteer project).
- **Action:** 8 concurrent, 20 req/s, burst 80. Breaker → AWS for the same tile. 404 marker TTL 90
  days (Mapterhorn adds lidar areas). Keep tiles forever but revalidate with `If-Modified-Since`
  after 180 days. Longer term: read the PMTiles archive by HTTP range, or offer a regional
  pre-download.

**AWS Terrain Tiles** (`s3.amazonaws.com/elevation-tiles-prod`, us-east-1; EU replica `elevation-tiles-prod-eu`)
- **Official limit and rules:**
  - Registry [Q https://registry.opendata.aws/terrain-tiles/]: no account needed ("--no-sign-request");
    an EU replica exists; no limits stated; attribution per
    https://github.com/tilezen/joerd/blob/master/docs/attribution.md.
  - S3 [Q https://docs.aws.amazon.com/AmazonS3/latest/userguide/optimizing-performance.html]: "at
    least ... 5,500 GET/HEAD requests per second per partitioned Amazon S3 prefix"; "you may see some
    503 (Slow Down) errors".
  - Data static since 2017 [H].
- **Our worst case:** up to 6 req/s in flight where Mapterhorn has no data deeper than z12 (each
  z13/z14 key → 4 AWS tiles).
- **Risk:** **low**.
- **Action:** 8 concurrent, 50 req/s. Back off on 503 SlowDown. Cache forever. Use the
  `-eu` bucket for players in Europe [I].

### 3.2 Feature branches

**Open-Meteo forecast** (`api.open-meteo.com`, feature/weather)
- **Official limit and rules:**
  - [Q https://open-meteo.com/en/terms]: "Less than 10'000 API calls per day, 5'000 per hour and 600
    per minute"; "300.000 calls / month"; "You may only use the free API services for non-commercial
    purposes"; "We reserve the right to block applications and IP addresses that misuse our service
    without prior notice"; data "CC-BY 4.0".
  - [Q https://open-meteo.com/en/pricing]: "Requests for data covering more than 10 weather
    variables ... are considered multiple API calls"; "a request for 2 weeks of data with 15 weather
    variables will be calculated as 1.5 API calls".
  - No 429 behaviour documented.
- **Our worst case:** 20 variables per request = **2 calls**. ~6/h normally (12 calls); worst
  ~36/h at 250 m/s (72 calls/h); 1 at start/teleport.
- **Risk:** **low** (1–2 % of the hourly limit).
- **Action:**
  - 1 concurrent, ≥10 s between calls.
  - Self-cap 1,000 calls/day.
  - Breaker on 429/5xx: keep the last weather, retry after max(Retry-After, 120 s, backoff).
  - Dedupe in-flight requests in `Service.get`.
  - Keep "Weather data by Open-Meteo.com".
  - Optionally drop to ≤10 variables to halve the call weight.

**SourceForge / GeographicLib EGM2008** (`downloads.sourceforge.net`, feature/gravity)
- **Official limit and rules:** No published download policy found. An old report describes an
  hourly hit threshold that blocks an IP and clears once traffic drops [I, mailing-list evidence
  only]. Data: NGA, public domain; GeographicLib MIT.
- **Our worst case:** 1 × 74 MB per install, plus resume attempts (≤10).
- **Risk:** **low**.
- **Action:** 1 concurrent. Honour Retry-After. Never re-download once the SHA-256 matches (already
  so). Consider mirroring the converted `.npy` in a GitHub release to take load off SourceForge.

### 3.3 Planned: world data (vector, land cover, trees, bathymetry)

**Overture Maps GeoParquet + STAC** (`overturemaps-us-west-2` S3; Azure blob; `stac.overturemaps.org`)
- **Official limit and rules:**
  - No limits on the docs page [Q https://docs.overturemaps.org/getting-data/]; the client "transfers
    only the data inside your bounding box".
  - S3: 5,500 GET/s per prefix [Q AWS above].
  - Azure: "Target request rate for a single block blob: Up to 3,000 requests per second"; on overload
    "503 (Server Busy) or ... 500 (Operation Timeout)", and "use an exponential backoff policy" [Q
    https://learn.microsoft.com/en-us/azure/storage/blobs/scalability-targets].
  - Licence: ODbL / CDLA per theme (CREDITS.md).
- **Our worst case:** per new area, one STAC lookup plus ~10–50 range reads per bbox (DuckDB). It
  scales with area, not speed, if extracted per 10 km cell.
- **Risk:** **low**.
- **Action:** 4 concurrent ranges per host. STAC catalog TTL 1 day; release files forever (immutable
  per release). Extract per region cell once and cache locally. Back off on 503.

**ESA WorldCover** (`esa-worldcover.s3.eu-central-1`) and **Meta/WRI canopy** (`dataforgood-fb-data` S3)
- **Official limit and rules:** S3 limits as above. Licences CC BY 4.0 (CREDITS.md). No service
  rules beyond S3.
- **Our worst case:** COG range reads, a few per km² of new ground (the report measured 0.2–0.35 s
  per window). Same order as the DEM.
- **Risk:** **low**.
- **Action:** 4 concurrent, 20 req/s. Cache windows forever.

**NLCD** (MRLC WMS/WCS)
- **Official limit and rules:** No published limit found.
- **Our worst case:** planned, small.
- **Risk:** **med** (a WMS renders per request).
- **Action:** prefer the downloadable rasters (one-off). If WMS: 2 concurrent, 2 req/s.

**GEBCO grid**, **NOAA NCEI coastal DEMs / Great Lakes**
- **Official limit and rules:** No rate limits published (one-off file downloads). GEBCO public
  domain with attribution; NOAA public domain (CREDITS.md).
- **Our worst case:** one-off, GB-scale.
- **Risk:** **low**.
- **Action:** 1 concurrent with resume, as gravity does. Never stream per tile; ship a converted
  tile set or download once.

**OpenStreetMap Overpass API** (`overpass-api.de`)
- **Official limit and rules:**
  - [Q https://wiki.openstreetmap.org/wiki/Overpass_API]: "less than 10,000 queries per day" and
    "less than 1 GB data per day"; for regular use "divide those numbers by 100".
  - "No parallel running of multiple scripts".
  - "If you receive an HTTP error code such as 429 or 406, pause for 30 seconds".
  - "Commercial use should use self-hosted or paid Overpass servers"; the UA or Referer must uniquely
    identify the app.
- **Our worst case:** streaming would be thousands per day.
- **Risk:** **block** for streaming.
- **Action (category search exception, 2026-11-14):** bounded interactive queries on Enter only, never streaming.
  0.1/s, burst 1, 1 concurrent, 50/day soft and 100/day hard; no retries, 60-second breaker on failure,
  one-hour application cache, 5-second query timeout, 8 MiB memory budget and at most 200 centers.

**Geofabrik extracts** (`download.geofabrik.de`)
- **Official limit and rules:** Not researched in detail. One-off state or region files.
- **Our worst case:** one-off, 0.1–1.3 GB.
- **Risk:** **low**.
- **Action:** 1 concurrent, resume, refresh ≤ weekly.

**OSMF vector tiles** (`vector.openstreetmap.org`)
- **Official limit and rules:** [Q https://operations.osmfoundation.org/policies/vector/] "Bulk
  downloading is prohibited" (incl. "downloading for offline usage"); "Cache Tile downloads locally
  according to HTTP Expiry Header, alternatively a minimum of 7 days"; valid identifying User-Agent;
  "access may still be blocked without prior notice".
- **Our worst case:** streaming = prefetch = bulk.
- **Risk:** **block**.
- **Action:** **do not use** (already the report's conclusion).

**OpenFreeMap** (`tiles.openfreemap.org`)
- **Official limit and rules:** "no limits on the number of map views or requests", commercial OK,
  no SLA (quoted in docs/research/2026-10-10-world-data-sources.md §2.3, from openfreemap.org).
- **Our worst case:** world-map UI only.
- **Risk:** **low**.
- **Action:** 4 concurrent, 10 req/s anyway. Cache by HTTP headers.

**Protomaps builds** (`build.protomaps.com`)
- **Official limit and rules:** "hotlinking is discouraged": copy to your own storage or `pmtiles
  extract` (world-data report §2.3).
- **Our worst case:** n/a.
- **Risk:** **block** for hotlinking.
- **Action:** extract once, never stream.

### 3.4 Planned: world map, search, routing (interactive)

**OSM standard tiles** (`tile.openstreetmap.org`)
- **Official limit and rules:**
  - [Q https://operations.osmfoundation.org/policies/tiles/]: "Send a clear, unique User-Agent string
    that names your app and optionally includes a contact URL or email"; "Do not use a library
    default User-Agent".
  - "cache each tile for at least 7 days"; never send no-cache headers.
  - "Bulk downloading is any pre-emptive fetching of tiles other than those a user is actively
    viewing".
  - **"Offline use is not permitted"**; attribution must be visible.
  - "We may block access, without notice"; "Repeated violations may lead to longer-term or
    network-level blocks".
  - No numeric rate or concurrency limit.
- **Our worst case:** map viewing only: tens of tiles per pan.
- **Risk:** **block** for prefetch or pre-download; low for viewing.
- **Action:** opt-in layer, `interactive_only`, no prefetch, excluded from pre-download.
  2 concurrent, honour Cache-Control (min 7 days). Keep the visible attribution.

**Nominatim** (`nominatim.openstreetmap.org`)
- **Official limit and rules:** [Q https://operations.osmfoundation.org/policies/nominatim/]
  - "an absolute maximum of 1 request per second" for the whole application (all users).
  - Scheduled bulk: "4 requests per minute".
  - "stock User-Agents as set by http libraries will not do".
  - Results "must be cached"; no autocomplete ("you must not implement such a service on the client
    side").
  - Grid reverse lookups forbidden; violations "will get you banned".
- **Our worst case:** search on Enter and reverse lookups: <1/min.
- **Risk:** **med** (the limit is app-wide across all players).
- **Action:** 1 concurrent, ≤1 req/s **and** ≤0.2 req/s per player (so many players stay under 1/s
  in total). Enter-only. Cache results 30 days. Never reverse-geocode per pose.

**Photon** (`photon.komoot.io`)
- **Official limit and rules:** [Q https://photon.komoot.io/] "please be fair - extensive usage will
  be throttled"; "We do not guarantee for the availability". No numbers.
- **Our worst case:** type-ahead, debounced: ~1/s while typing.
- **Risk:** **med**.
- **Action:** 1 concurrent, ≤1 req/s, 400 ms debounce, ≥3 characters, cache 1 day. Breaker →
  Nominatim (Enter only) or the offline gazetteer.

**OSRM demo** (`router.project-osrm.org`)
- **Official limit and rules:** [Q https://github.com/Project-OSRM/osrm-backend/wiki/Demo-server]
  "Do not exceed 1 request per second"; "reasonable, non-commercial use-cases"; "no guarantees wrt.
  uptime, latency, or data updates".
- **Our worst case:** one route per destination plus re-routes (~1/min aggressive).
- **Risk:** **low**.
- **Action:** 1 concurrent, ≤1 req/s globally, 10 s re-route cool-down, cache by rounded URL. Breaker
  → FOSSGIS.

**FOSSGIS OSRM** (`routing.openstreetmap.de`)
- **Official limit and rules:** [Q https://routing.openstreetmap.de/about.html] "One request per
  second max."; "No scraping, no heavy usage"; "Use a valid user agent and, if applicable, a correct
  referrer"; attribution plus a "fix the map" link.
- **Our worst case:** same as OSRM demo.
- **Risk:** **low**.
- **Action:** same as OSRM demo. Breaker → OSRM demo → "routing unavailable".

**FOSSGIS Valhalla**
- **Official limit and rules:** Fair use; "1 call/user/sec" unconfirmed; max 1,500 km per route
  (GPS report §2.2).
- **Our worst case:** ≥4 chained requests for a cross-country route.
- **Risk:** **low**.
- **Action:** 1 req/s, as above.

**Mapillary API** (`graph.mapillary.com`, `tiles.mapillary.com`)
- **Official limit and rules:** [Q https://www.mapillary.com/developer/api-documentation]
  - Entity APIs "limited to 60,000 per minute per app"; search "10,000 per minute per app"; tiles
    "limited to 50,000 per day".
  - All requests need a token.
  - Over the limit: "Application request limit reached" (code 4), tiles "status code 4xx".
  - Licence CC BY-SA 4.0 (CREDITS.md).
- **Our worst case:** reference and tuning only, offline tools.
- **Risk:** **low**.
- **Action:** dev tool only. 2 concurrent, 5 req/s. The token comes from an environment variable and
  is never committed.

**Esri World Imagery** (opt-in, user's own key)
- **Official limit and rules:** Esri terms: account or key required, "not intended to export tiles
  for offline" (imagery report). ArcGIS Online has applied per-org rate limiting since 2020; users
  report 429 with "Retry after 60 sec" (community.esri.com, not official).
- **Our worst case:** as USGS.
- **Risk:** **block** without a key.
- **Action:** BYO key only. **No disk cache** (private HTTP cache only). Honour Retry-After.

### 3.5 Planned: national and state orthophotos, grouped by provider type

Rates below are per player at the default layout [M]:
- A z18–19 source used for every level: ~5 req/s at 30 m/s and ~23 req/s at 134 m/s.
- The same source limited to L-1..L1 (within ~1.5 km) with 512 px requests: about ¼ of that.

**XYZ / WMTS national tile caches**
- **Services:** swisstopo, IGN Géoplateforme WMTS, PDOK, basemap.at, ČÚZK, ICGC, Luxembourg, GSI,
  NLSC, Maa-amet, LINZ, NSW/Vic/Tas, and the US state XYZ caches (CT, VT, PA, DE, VA, NJ, Indiana,
  Ohio, Kentucky, NC).
- **Official limits:**
  - **swisstopo:** WMTS "1'200 requests / minute" and "631 Mio requests / year"; WMS "20 requests /
    minute for an image size of 360,000 pixels"; REST "40 requests / minute"; access "may be
    restricted or refused" if exceeded; "Automatic parsing of geoservices via bots with high query
    intensities is to be avoided. The download service should be used ... (use in offline systems,
    databases, etc.)" [Q https://www.geo.admin.ch/en/general-terms-of-use-fsdi, table §2.3; limits
    doubled from 2025-01-01 per https://geo.admin.ch/en/change-limits-fair-use].
  - **IGN Géoplateforme:** the WMTS page states no limit. Sibling services state per-IP limits: "Son
    usage est limité à 40 requêtes par seconde depuis une même adresse IP" (WMS-Raster), 30/s (WFS),
    10/s (Téléchargement) [Q data.gouv.fr dataservices API].
  - **PDOK:** no fair-use page found. Large users (>10 M hits on a base register) are charged since
    2021 (Dutch consultation document) [I].
  - **GSI:** live loading needs only attribution: "地理院タイルをウェブサイトやソフトウェア、アプリケーション上でリアルタイムに読み込んで利用する場合、...出典の明示のみで申請不要" [Q https://maps.gsi.go.jp/development/ichiran.html]. Nothing on rate.
  - **LINZ:** key required; 1,000 req/min, 1 M/month, keys expire after 90 days (imagery report).
  - **Others:** unpublished.
- **Risk at 134 m/s, full use:** **high**. 23 req/s is above swisstopo's 20 req/s and twice IGN's
  WMS-R figure.
- **Action:**
  - Per-host policy entry with the published figure halved (swisstopo 10 req/s, IGN WMTS 10 req/s,
    LINZ 8 req/s, the rest 5 req/s).
  - 4 concurrent.
  - Use national sources only for levels ≤ L1; EOX or USGS beyond.
  - Prefetch only above 50 % bucket.
  - Breaker → USGS/EOX.

**WMS national and state services**
- **Services:** German Länder DOP, Flanders, Wallonia, Brussels, PT DGT, IT PCN/Toscana, PL
  Geoportal, São Paulo, MN MnGeo, the DE state services.
- **Official limits:** mostly unpublished. swisstopo's WMS figure (20/min for 600×600 px) shows how
  much lower WMS budgets are than tile budgets.
- **Risk:** **high** (server-side render per request).
- **Action:**
  - 2 concurrent, 2 req/s per host.
  - Request 512×512 (or 1024×1024 where allowed) and split into four or sixteen virtual tiles.
  - Near levels only.
  - Breaker after 2 slow responses (>5 s) as well as on 429/503.

**ArcGIS Server / ArcGIS Online services**
- **Services:**
  - USGS NAIP ImageServer; MD iMAP; NYS; Ontario; QLD; and the state ImageServers (Oregon, Illinois,
    Missouri, Kansas, DC, MS, LA, TX).
  - `tiles.arcgis.com` hosted caches (MA, NYC, Philadelphia, SC, Sioux Falls).
- **Official limits:**
  - None published per service.
  - ArcGIS Online enforces per-org rate limits and answers 429 with a Retry-After (community
    reports).
  - Today's tests: NAIP took 1.5–12.5 s per request and gave 504s; WV took 60 s; Wisconsin 15–18 s.
- **Risk:** **high** for `exportImage`/`export`; **med** for hosted `/tile/` caches.
- **Action:**
  - `/tile/` caches: 4 concurrent, 5 req/s.
  - `export`/`exportImage`: 2 concurrent, 1 req/s, 512 px requests, near levels only. Breaker on a
    p50 latency > 5 s, so slow NAIP never stalls the queue.
  - Honour Retry-After from `tiles.arcgis.com`.
  - "View only" sources (Michigan, Tennessee, WV mosaic) stay off or memory-only.

**OpenAerialMap** (`tiles.openaerialmap.org`, `api.openaerialmap.org`)
- **Official limits:** none found on docs.openaerialmap.org. Licence per image (CC BY 4.0 usually;
  imagery report).
- **Risk:** **med** (volunteer infrastructure).
- **Action:** 2 concurrent, 5 req/s. Catalog query cache 7 days.

**NOAA NGS storm imagery** (`stormscdn.ngs.noaa.gov`)
- **Official limits:** none published; CDN; public domain.
- **Risk:** **low**.
- **Action:** 4 concurrent, 10 req/s.

---

## 4. Code risks found (beyond the summary)

1. **`imagery._download` makes a 400 permanent** (`imagery.py:258`). Some ArcGIS and WMS servers
   answer 400 to a malformed or over-sized request, or while overloaded. That writes a `.404` marker
   forever. Treat only 404 (and 204 or a "blank" image) as no coverage. Give markers a TTL.
2. **Redirects are not followed and not classified.** EOX says rate-limited clients get "redirects to
   this page". `http.client` returns a 3xx, which counts as a generic failure and is retried 3×.
   Classify 3xx-to-HTML from a tile host as `RateLimited`.
3. **The DEM has no breaker.** A Mapterhorn outage costs each tile 4 attempts × (20 s timeout +
   sleeps), across 8 workers, before falling to AWS. The heightmap the mod is waiting for stalls for
   minutes. AWS is used only after a Mapterhorn 404, never after a Mapterhorn *error*.
4. **A palette fallback is never retried.** `make_tile` gives a tile the palette material when the
   imagery fails (`earthd.py:619`), and nothing re-textures it after recovery. ROADMAP robustness
   item 4 needs a "degraded" mark and a background re-texture once the breaker closes.
5. **Prefetch competes equally with demand.** `Imagery.prefetch` submits to the same pool, with no
   priority, so prefetched tiles use the same budget as visible ones. Under a limiter, prefetch must
   come last and give up first.
6. **Weather `Service.get` has no in-flight dedupe.** Two connections in the same cell both fetch. It
   is harmless with one mod and should be fixed when moving to the shared fetcher.
7. **The UA lives in two copies** (`dem.py`, `gravity.py`) and has no version or URL.

---

## 5. Recommended design: `server/netfetch.py`

### 5.1 Components

**`HostPolicy` (dataclass, one per host):**
- `rate` (tokens/s), `burst`, `max_conc`, `daily_budget` (soft) and `daily_hard` (optional).
- `allow_prefetch`, `interactive_only` (OSM/Nominatim/Photon), `offline_ok` (may be pre-downloaded).
- `cache_ttl` (None = forever), `miss_ttl` (404 markers), `honor_cache_headers`.
- `retry_statuses = {429, 500, 502, 503, 504}`, `max_attempts = 3`, `base_delay = 0.5`, `max_delay = 60`.
- `breaker_threshold = 5` failures in 30 s; `breaker_open = 30 s` doubling to 15 min.
- `slow_ms` (counts as a failure; for WMS and export hosts).
- The policies live in a table (`POLICIES`, keyed by host) next to the source table that the imagery
  report plans (`sources.json`).

**`TokenBucket(rate, burst, clock)`:** refill on read. `try_take()` for prefetch;
`take(timeout)` for demand. Prefetch may take only while `tokens > burst / 2`.

**Concurrency:** one `threading.BoundedSemaphore(max_conc)` per host. It is independent of the
worker pools, so a pool of 8 can never put 8 requests on a host whose cap is 2.

**Priority:** demand (`render`, a heightmap the mod asked for) goes ahead of prefetch, which goes
ahead of background work (de-shadow, re-texture). The simplest version: prefetch and background use
`try_take()` and drop (not queue) when refused. The tile is fetched later at build time if it is
still needed.

**Retry:** exponential backoff with full jitter: `sleep = random.uniform(0, min(max_delay, base *
2**attempt))`, using an injectable `rng`.

**`Retry-After`:**
- Parse delta-seconds or an HTTP-date. Clamp to [1 s, 15 min].
- On 429 or 503 it **replaces** the backoff.
- It also pauses the whole host: the bucket is emptied and no tokens are issued until then. So the
  other workers do not keep hitting a host that has said stop.

**`CircuitBreaker` per host:**
- `closed` → `open` after `threshold` failures in the window, or after one 429/503 that carries a
  Retry-After.
- `open` → `half-open` after the open time, which then lets 1 probe request through.
- Success → `closed`, and the open time resets. Failure → `open` with the time doubled.
- While open, `get()` raises `HostUnavailable` immediately (no network).
- The chains already fall over per source:
  - Imagery: national → USGS → EOX 2023 → EOX 2016 → palette. EOX 2023 and 2016 share **one**
    breaker because they share a host.
  - DEM: Mapterhorn → AWS → offline coarse DEM (ROADMAP robustness 4) → flat sea level plus a
    warning.

**Offline mode:**
- `Fetcher.offline` is true when every breaker on the active chains is open, or the user set
  `--offline`.
- Disk-cache hits still work. Misses raise at once.
- The server sends `status` "Offline - using cached data" (ROADMAP robustness 1).
- Degraded tiles are recorded and re-textured or rebuilt when a breaker closes (fixes §4.4).

**Cache lifetimes (per licence, via `HostPolicy`):**
- **Forever:** USGS, AWS, NOAA (public domain); EOX (CC BY / CC BY-NC-SA, static yearly mosaics);
  Overture release files (immutable); WorldCover and CHM.
- **Forever with revalidation:** Mapterhorn, revalidate after 180 days.
- **TTLs:** Open-Meteo 15-min slot (as now). Nominatim and Photon 30 days / 1 day. OSRM routes 1 day.
  OSM tiles by `Cache-Control`/`Expires`, min 7 days, never pre-downloaded.
- **No disk cache:** Esri and "view only" state services.
- **404 markers:** 90 days, so new coverage gets picked up.
- **Separate cache roots:** open data versus `private/` (BYO key), as `docs/ARCHITECTURE.md`
  specifies.

**User-Agent:** `f"BeamNG.Earth/{SERVER_VERSION} (+https://github.com/TecnaGamer/BeamNG.Earth)"`.
- One constant in `netfetch.py`.
- No user name, e-mail, machine name, location or session id.
- Optional `Referer` for hosts that ask for one (FOSSGIS): the same project URL.

**Budgets and logging:**
- Per-host counters: requests, bytes, 2xx/3xx/4xx/429/5xx, retries, breaker trips, budget left.
- Persisted per UTC day in `<cache>/netstats/<date>.json`, written every 60 s and at exit.
- At 80 % of the soft budget: one warning, and prefetch stops for that host.
- At 100 %: demand still allowed at half rate, plus one warning per hour. With `daily_hard` (Open-Meteo
  1,000 calls, Nominatim, Overpass): the source is skipped and the chain falls over.
- A one-line summary per minute in the server log (as `imagery.summary()` does now). The per-host
  state goes in `status` for the devtools window.

### 5.2 Default policies [I]

Rates are per player. "Prefetch" means the host may be asked ahead of need.

| Host | rate / burst | conc | daily soft (hard) | prefetch | cache | miss TTL |
|---|---|---|---|---|---|---|
| basemap.nationalmap.gov | 15/s, 60 | 6 | 60k | yes (≥50 %) | forever | 90 d |
| tiles.maps.eox.at | 6/s, 30 | 4 | 20k | yes (≥50 %) | forever | 90 d |
| tiles.mapterhorn.com | 20/s, 80 | 8 | 30k | yes | forever, revalidate 180 d | 90 d |
| s3.amazonaws.com (elevation) | 50/s, 100 | 8 | — | yes | forever | forever |
| api.open-meteo.com | 0.1/s, 2 | 1 | 500 req (1,000 calls) | no | 15-min slot | — |
| downloads.sourceforge.net | 1/s, 1 | 1 | — | n/a | forever (SHA-256) | — |
| National XYZ/WMTS (default) | 5/s, 20 | 4 | 20k | ≥50 % | per licence | 90 d |
| wmts.geo.admin.ch | 10/s, 40 | 4 | 50k | ≥50 % | per licence | 90 d |
| data.geopf.fr (WMTS) | 10/s, 40 | 4 | 50k | ≥50 % | forever (Etalab 2.0) | 90 d |
| WMS hosts (default) | 2/s, 4 | 2 | 5k | no | per licence | 30 d |
| ArcGIS export/exportImage (incl. NAIP) | 1/s, 2 | 2 | 3k | no | forever (PD) / per licence | 30 d |
| tiles.arcgis.com (hosted caches) | 5/s, 20 | 4 | 20k | ≥50 % | per licence | 90 d |
| tile.openstreetmap.org | 2/s, 6 | 2 | 2k | **never** (interactive only) | headers, ≥7 d, not offline | — |
| nominatim.openstreetmap.org | 0.2/s, 1 | 1 | (500) | never | 30 d | — |
| photon.komoot.io | 1/s, 2 | 1 | 2k | never | 1 d | — |
| router.project-osrm.org, routing.openstreetmap.de | 1/s, 1 | 1 | (500) | never | 1 d | — |
| overpass-api.de | 0.1/s, 1 | 1 | 50 / 100 | never | 1 h memory | Enter-only category search |
| vector.openstreetmap.org, build.protomaps.com | not allowed | — | (0) | never | — | — |
| Overture / WorldCover / CHM S3, Azure | 20/s, 40 | 4 | — | yes | forever | — |
| tiles.openaerialmap.org | 5/s, 10 | 2 | 5k | ≥50 % | forever (CC BY) | 30 d |

### 5.3 Where it goes in the code

**New `server/netfetch.py`:**
- `HostPolicy`, `POLICIES`, `TokenBucket`, `CircuitBreaker`.
- `Fetcher(clock=time.monotonic, sleep=time.sleep, rng=random.Random(), transport=HttpsTransport(),
  stats_dir=None)`.
- `Fetcher.get(url, *, kind="demand"|"prefetch"|"background", headers=None)` returns `Response(status,
  body, headers)`, or raises `NotFound`, `RateLimited(retry_after)`, `HostUnavailable`,
  `BudgetExceeded` or `PolicyDenied`.
- `Fetcher.allow(host, kind)`, `Fetcher.summary()`, `Fetcher.offline`.
- `HttpsTransport` is the per-thread keep-alive pool moved out of `Imagery._http_get`.

**`server/dem.py`:**
- `USER_AGENT` moves to `netfetch`.
- `Dem.__init__(..., fetcher)`.
- `_download` calls `fetcher.get` and drops its own retry loop. It writes `.404` with an mtime, and
  the TTL check reads it.
- `_load_tile`: on `HostUnavailable` or `RateLimited` from Mapterhorn, go to `_aws_tile` (failover);
  if both fail, raise `DemUnavailable` (a later offline coarse DEM goes here).

**`server/imagery.py`:**
- `_http_get` and the `_down` dict go away. `_download` uses `fetcher.get`.
- 400 no longer means a miss; `.404` markers get a TTL.
- `prefetch()` passes `kind="prefetch"` and stops at the first `PolicyDenied` or `BudgetExceeded`.
- `ImageryError` wraps the netfetch exceptions, so `render()`'s source fallback keeps working.
- The breaker is keyed by host, so the eox2023 → eox2016 fallback is skipped while the EOX host is
  open.
- Attribution strings move to the EOxCloudless wording.

**`server/earthd.py`:**
- `Server.__init__` builds one `Fetcher` and passes it to `Dem`, `Imagery` and later `Weather`,
  routing and geocoding.
- `make_tile` records palette-fallback tiles in `self.degraded`. A low-priority task re-queues them
  when `fetcher` reports the host closed again.
- `refresh_status` adds the offline / degraded text.
- `--offline` flag.
- The log loop prints `fetcher.summary()`.

**`feature/weather` `server/weather.py`:**
- `fetch()` uses `fetcher.get(kind="background")`.
- `Service.get` dedupes in-flight cells.
- Tracker retry = max(`RETRY_S`, `RateLimited.retry_after`).

**`feature/gravity` `server/gravity.py`:** `_download` uses the fetcher's UA and the policy
(1 concurrent); keep its resume logic.

**`feature/deshadow` `server/deshadow.py`:** `im.tiles(keys)` goes through `kind="background"`, so
region fits never use the demand budget.

**`farfield.select`** ([I], separate change): select fine levels by 3D distance to the eye in
flight, which removes most of the 21 req/s at 10 km.

### 5.4 Tests (no network; `server/tests/test_netfetch.py`)

**Fixtures:**
- `FakeClock` (`now`, `advance`) and `FakeSleep` (advances the clock and records sleeps).
- A seeded `random.Random`.
- `FakeTransport`: a script per URL pattern (status, headers, body, latency), with call log and
  timestamps.

**Cases:**
1. **Token bucket:** N demand requests at t=0 → `burst` go through at once; the rest are spaced at
   1/rate (check the timestamps). Prefetch is refused below half a bucket.
2. **Concurrency cap:** 16 threads against a host with `max_conc=2` → the transport never sees more
   than 2 in flight.
3. **Backoff and jitter:** 503, 503, 200 → two sleeps, each within [0, base·2^k]; total attempts ≤
   `max_attempts`.
4. **Retry-After:**
   - `Retry-After: 7` → the next request to that host (from any thread) is not sent before t+7.
   - An HTTP-date is parsed; values >15 min are clamped.
   - 429 without the header → backoff.
5. **Circuit breaker:** 5 failures → open; `get` raises with no transport call; after the open time
   one probe passes; a failed probe doubles the open time; success closes it.
6. **Failover:**
   - Imagery: EOX host open → `render` uses the next source with no EOX calls, and eox2016 is
     skipped too (same host).
   - DEM: Mapterhorn open → AWS tiles are used for the same key.
7. **Classification:**
   - 404 → `.404` marker with mtime; after `miss_ttl` the tile is fetched again.
   - 400 → no marker.
   - 3xx to an HTML page → `RateLimited`.
   - 200 with an HTML body → not cached (exists today) and counted as a failure.
8. **Policy:** an `interactive_only` host refuses `kind="prefetch"`; pre-download refuses
   non-`offline_ok` hosts; Overpass, vector.openstreetmap.org and Protomaps are always `PolicyDenied`.
9. **Budget:** prefetch stops at 80 %; at 100 % the demand rate halves; a hard budget raises
   `BudgetExceeded`; the counter persists across a `Fetcher` restart on the same UTC day and resets
   the next day.
10. **User-Agent:** every transport call carries the project UA. Headers contain no user name, home
    path, hostname or e-mail (check `os.getlogin()`, `socket.gethostname()` and the home directory
    against all headers and URLs).
11. **Offline:** all chain hosts open → `Fetcher.offline` is true, `status` says so, disk hits still
    served, degraded tiles re-queued after a close (with the fake earthd server from
    `tests/fake_imagery_earthd.py`).
12. **Demand regression:** the `ratesim` driver (moved into `tests/test_demand.py` as a new row set)
    checks that worst-case requests/s per host at 30/134 m/s and flight stay below each host's policy
    rate. A layout change that raises network demand then fails a test instead of a server.

---

## 6. Open questions to ask the providers before a public release

- **EOX** (cloudless@eox.at): acceptable request volume for a free, non-commercial mod with disk
  caching. Would a bundled low-zoom set and ~6 req/s per player be acceptable? What exactly does the
  rate-limit response look like (status or redirect)?
- **USGS** (tnm_help@usgs.gov): any rate guidance for `basemap.nationalmap.gov` tile clients.
- **Mapterhorn:** whether many game clients streaming from the zxy endpoint are welcome, or whether
  regional PMTiles downloads are preferred.
- **swisstopo, IGN, PDOK:** confirm the per-IP figures apply per player (they are fair-use limits per
  user, and 20,000 daily web users count as fair use for swisstopo).

## Sources (fetched 2026-10-10)

- Open-Meteo terms https://open-meteo.com/en/terms and pricing https://open-meteo.com/en/pricing
- OSMF tile policy https://operations.osmfoundation.org/policies/tiles/, vector
  https://operations.osmfoundation.org/policies/vector/, Nominatim
  https://operations.osmfoundation.org/policies/nominatim/
- Overpass https://wiki.openstreetmap.org/wiki/Overpass_API
- OSRM demo https://github.com/Project-OSRM/osrm-backend/wiki/Demo-server; FOSSGIS
  https://routing.openstreetmap.de/about.html
- Photon https://photon.komoot.io/
- EOX https://maps.eox.at/, https://cloudless.eox.at/documentation/license,
  https://cloudless.eox.at/license-non-commercial, https://cloudless.eox.at/documentation/usage
- USGS https://www.usgs.gov/faqs/are-there-any-costs-or-restrictions-usage-data-downloaded-national-map
- Mapterhorn https://mapterhorn.com/data-access
- AWS https://registry.opendata.aws/terrain-tiles/,
  https://docs.aws.amazon.com/AmazonS3/latest/userguide/optimizing-performance.html
- Azure https://learn.microsoft.com/en-us/azure/storage/blobs/scalability-targets
- Overture https://docs.overturemaps.org/getting-data/
- Mapillary https://www.mapillary.com/developer/api-documentation
- swisstopo https://www.geo.admin.ch/en/general-terms-of-use-fsdi, https://geo.admin.ch/en/change-limits-fair-use
- IGN Géoplateforme (data.gouv.fr dataservices API, `?q=geoplateforme`)
- GSI https://maps.gsi.go.jp/development/ichiran.html
- ArcGIS Online 429 reports https://community.esri.com/ (community, not official)

Category search also uses `query.wikidata.org`: 0.1/s, burst/concurrency 1, 50/day soft, 100/day hard,
no retries or prefetch, 60-second breaker on failure, one-hour application cache. Queries return at most 24
notable class-family instances (P31/P279*); motorsport searches exclude airports explicitly. Overpass has a
4-second HTTP timeout, named selectors, and a 15 km radius for line/area categories (50 km for peaks, race tracks,
airports and dams). Candidate distances are recomputed after the cache; admission failures are cached for at most 10 seconds. Both hosts use netfetch's project User-Agent.
