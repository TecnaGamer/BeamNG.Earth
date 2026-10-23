"""Satellite imagery for the visual tiles (docs/V1_3_SPEC.md §A).

Sources (all Web-Mercator XYZ, 256 px tiles, no key; verified 2026-10-10):
  usgs     USGS The National Map ImageryOnly. Public domain. US only, z <= 16 (~1.9 m/px at 36 N).
           Outside its coverage it answers 404, or (at low zooms) a uniform white tile.
  eox2023  Sentinel-2 cloudless 2023 by EOX. Global, ~10 m (native around z14). CC BY-NC-SA 4.0.
           Open ocean is a uniform deep blue tile (a real colour, not "no data").
  eox2016  Sentinel-2 cloudless 2016 by EOX. Global, ~10 m. CC BY 4.0. Last fallback.

A texture is rendered for a tile's ENU footprint by sampling the imagery at every texel centre:
ENU plane point (anchor frame) -> lat/lon (the same mapping the tile geometry uses, so the
image sits exactly on the terrain) -> Web-Mercator pixel at a zoom whose ground resolution is
about the texel size (never finer than the source's max zoom) -> bilinear. Each sample takes the
first source of the chain that has data there: a source tile that is 404, or uniform (blank)
while a later source remains, counts as missing, so the chain falls back per source tile.
Samples no source covers (404 everywhere) get FALLBACK_RGB; beyond +-85.05 deg (all ice) POLAR_RGB.

Caching: raw tiles on disk under <cache>/<source>/<z>/<x>/<y>.jpg (whatever format the server
sent), 404s remembered as <y>.404 markers (as dem.py does; they expire after the host's miss_ttl,
90 days), decoded tiles in an in-memory LRU. Downloads run on a small pool (FETCH_WORKERS <= 6)
through a netfetch.Fetcher: the host's rate limit, concurrency cap, retries and circuit breaker
(keyed by host, so eox2023 and eox2016 share one), one persistent HTTPS connection per thread and
host. A source whose host cannot be asked fails at once (ImageryError), and render() takes the
next source. prefetch() asks with kind="prefetch" (dropped when the host's bucket is low; render
then fetches the tile on demand).

Output: DDS DXT1 with a full mip chain (write_dds). Pillow 12 writes DXT1 but no mipmaps (its
header always says 0 mips), so every level is box-downsampled and BC1-encoded by Pillow separately
and the levels are concatenated under our own header (DDSD_MIPMAPCOUNT, DDSCAPS_MIPMAP|COMPLEX).
"""
import io
import logging
import math
import os
import struct
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np

from PIL import Image, ImageFilter

import cache as cachemod
import dem as demmod
import fast     # Rust kernels (earthfast): the warp, mips and BC1 blocks
import netfetch
import season
import staging

log = logging.getLogger("earthd.imagery")

TILE = 256
MAX_LAT = demmod.MAX_LAT
EARTH_CIRC = 40075016.686          # m, equatorial circumference (Web-Mercator scale)
R_MERC = EARTH_CIRC / (2 * math.pi)  # m, the Web-Mercator sphere
FETCH_WORKERS = 12      # (the photo hosts allow 12 / 6 at once, 2026-10-16)
MEM_TILES = 512                    # decoded 256² RGB tiles kept in memory (~100 MB)
# Attempts, retry delays, timeouts and the "source unreachable" backoff are the host's netfetch
# policy now: once its circuit is open, its tiles fail at once instead of each paying the attempts
# and timeouts (offline, every texture took ~10 s before the backoff existed), and fall back to
# the next source or, with none left, to the palette material.
# source tiles per texture and source (zoom is lowered beyond). A 1024 m ground block at z17 takes 6 x 6 at 39 deg, 7 x 7
# north of ~47.5 deg, 8 x 8 north of ~57 deg: at 36 every z17-only regional source (Germany, Poland, Czechia, the
# Netherlands...) was skipped for the near ground there (found adding Poland, 2026-10-22); 81 = 9 x 9 (~64 deg),
# a 2304 px RGBA mosaic (~21 MB) while one texture renders
MAX_MOSAIC = 81
MESH_CELLS = 8                     # render(): exact reprojection on an 8 x 8 grid of texture cells
FALLBACK_RGB = (100, 89, 69)       # no imagery anywhere: the terrain material's dirt colour
POLAR_RGB = (226, 232, 240)        # ... past Web Mercator's limit, where it is all ice (polar ice sheet, sea ice): snow
BLANK_RANGE = 2                    # a tile whose pixels all lie within this many levels is uniform
NODATA_MAX, NODATA_SHARE = 6, 0.02   # an export tile with > 2 % texels this black is outside the data
NODATA_WHITE_MIN, NODATA_WHITE_SHARE = 250, 0.25   # ... or (nodata "white") > 25 % this white: white roofs
                                                   # are bright but rarely a quarter of a 240 m tile (MD
                                                   # iMAP's white comes back as 253 in its JPEGs)


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    max_zoom: int
    attribution: str
    # Coverage probes: a tile is only fetched if its ancestors at these zooms have data (not 404,
    # not blank). USGS answers outside the US with 404s or white tiles, ~280 ms each; without the
    # probes a start in the Alps asked it 1132 times before using EOX.
    probe_zooms: tuple = ()
    # Below this zoom the source is not used. USGS serves a global low-resolution basemap at
    # z <= 9 (verified 2026-10-10: imagery for the Alps / Pyrenees / open Pacific at z6 and z8,
    # 404 at z10), which would put a different source on the far rings outside the US.
    min_zoom: int = 0
    # ArcGIS exportImage instead of XYZ tiles: one request covers 2^export x 2^export tiles of zoom z
    # (the parent at z - export, (TILE << export)² px), split into z tiles on disk; url takes {bbox}
    # (EPSG:3857) and {size}. 0: an XYZ source (url takes {z}, {x}, {y}).
    export: int = 0
    # Never fetched by a render: a tile not on disk yet is missing there this time (the next source fills
    # it); warm() fetches them ahead of the player, one request at a time (slow hosts: USGS's NAIP
    # service took ~10 s a request in-game, 512 or 1024 px alike, and 5xx'd under parallel requests).
    lazy: bool = False
    # The coverage probes ask this source instead (an export source's own probe would be a huge area)
    probe_source: str = ""
    # (south, west, north, east) degrees: tiles wholly outside are never asked (a state's own service)
    bounds: tuple = ()
    # The colour an export source fills outside its data with ("black" or "white"), see NODATA_*
    nodata: str = "black"
    # Flown leaf-off (bare trees, dormant grass): render() gives its vegetation the summer reference's colour
    # (season.leaf_on) when the Imagery's leaf_on is set
    leaf_off: bool = False


SOURCES = {
    "usgs": Source("usgs", "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}",
                   16, "USGS The National Map (public domain)", probe_zooms=(10, 12), min_zoom=10),
    "eox2023": Source("eox2023", "https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2023_3857/default/g/{z}/{y}/{x}.jpg",
                      14, "EOxCloudless https://cloudless.eox.at by EOX IT Services GmbH (Contains modified "
                          "Copernicus Sentinel data 2023), CC BY-NC-SA 4.0"),
    "eox2016": Source("eox2016", "https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless_3857/default/g/{z}/{y}/{x}.jpg",
                      14, "EOxCloudless https://cloudless.eox.at by EOX IT Services GmbH (Contains modified "
                          "Copernicus Sentinel data 2016 & 2017), CC BY 4.0"),
}
# NAIP (USDA aerial photography, 0.3-0.6 m, the flights the USGS layer above is made from at ~1.9 m),
# at z17 only (~0.9 m): the block's base photo (1 m texels) and the nearest far tiles (L0 1 m, L-1
# 0.5 m texels: capped at z17 so they use the tiles warm() fetched for the block; 0.5 m texels from
# z18 looked hardly sharper in the 2026-10-13 comparison); where it has
# nothing (outside the lower 48: a uniform image) or its tile is not fetched yet, USGS takes over.
SOURCES["naip"] = Source(
    "naip", "https://imagery.nationalmap.gov/arcgis/rest/services/USGSNAIPImagery/ImageServer/exportImage"
            "?bbox={bbox}&bboxSR=3857&imageSR=3857&size={size},{size}&format=jpg&f=image",
    17, "USDA NAIP via USGS The National Map (public domain)", probe_zooms=(10, 12), min_zoom=17,
    export=2, lazy=True, probe_source="usgs")
# The same NAIP from USDA's own service: newer flights (2024-25, some states 0.3 m) and 2-5x faster
# (1.6-4 s per 1024² in-game 2026-10-13 vs ~10 s), but blank in places (Maryland, Denver: black, so
# missing, and "naip" fills in).
SOURCES["naipusda"] = Source(
    "naipusda", "https://apps.geo.fpac.usda.gov/geo-imagery/rest/services/naip/conus_naip/ImageServer/exportImage"
                "?bbox={bbox}&bboxSR=3857&imageSR=3857&size={size},{size}&format=jpg&f=image",
    17, "USDA NAIP (public domain)", probe_zooms=(10, 12), min_zoom=17, export=2, lazy=True, probe_source="usgs")
# Maryland's own six-inch orthophotos (MD iMAP, 0.15 m, flown 2023-25, leaf-off spring): ~1.3 s per
# 1024² (in-game 2026-10-13, vs ~10 s for the USGS NAIP service Maryland otherwise needs; USDA's has none
# there). White outside the state. Free with acknowledgement (docs/research/2026-10-10-us-state-imagery.md).
# A cached MapServer: it draws only at ~1 m per pixel or coarser (0.75 and 0.5 m requests came back all
# white, 2026-10-13); the z15 parent at 1024 px is 0.93 m at 39 N. Keep max_zoom 17 / export 2 (finer
# texels need the three-inch ImageServer of that research).
SOURCES["md6in"] = Source(
    "md6in", "https://mdgeodata.md.gov/imagery/rest/services/SixInch/SixInchImagery2023_2024_2025/MapServer/export"
             "?bbox={bbox}&bboxSR=3857&imageSR=3857&size={size},{size}&format=jpg&f=image",
    17, "State of Maryland, MD iMAP / DoIT six-inch imagery", min_zoom=17, export=2, lazy=True,
    bounds=(37.88, -79.49, 39.73, -75.04), nodata="white", leaf_off=True)
BASE_CHAIN = ("md6in", "naipusda", "naip", "usgs", "eox2023", "eox2016")     # the chain before the regional sources
# Regional aerial imagery (imagery_regional.py: state and national orthophoto services, each within its box)
import bcortho as bcorthomod  # noqa: E402
import imagery_regional  # noqa: E402
# A state's tile cache serves the far rings from this zoom on; coarser rings (texels of ~15 m and more, kilometres
# away) take USGS / Sentinel-2 (the user, 2026-10-22: "the world gen is really slow": Columbus's far field waited
# 4 minutes on Ohio's cache at its 6 requests/s, ~1,000 tiles for the 2-4 km tiles: z15 / z14).  z16: L2 and nearer.
REGIONAL_XYZ_MIN_ZOOM = 16
for _n, (_url, _attr, _bounds, _kind, _leaf) in imagery_regional.REGIONAL.items():
    if _kind == "xyz":          # a tile cache: also on the nearer far rings (REGIONAL_XYZ_MIN_ZOOM+)
        SOURCES[_n] = Source(_n, _url, 17, _attr, probe_zooms=(12,), min_zoom=REGIONAL_XYZ_MIN_ZOOM, bounds=_bounds,
                             leaf_off=_leaf)
    else:                       # a WMS / ArcGIS export: one 1024 px image per z15 parent, ahead of the player
        SOURCES[_n] = Source(_n, _url, 17, _attr, min_zoom=17, export=2, lazy=True, bounds=_bounds, nodata="white",
                             leaf_off=_leaf)
# British Columbia's lidar-programme orthophotos (bcortho.py: 10-15 cm, rendered from the Province's GeoTIFFs, not
# fetched as images): after the regional services, like an export source
SOURCES["bcortho"] = Source("bcortho", bcorthomod.URL, 17, bcorthomod.ATTRIBUTION, min_zoom=17, export=2, lazy=True,
                            bounds=(47.9, -130.1, 51.1, -121.9), nodata="black")
DEFAULT_CHAIN = ("md6in", *imagery_regional.REGIONAL, "bcortho", "naipusda", "naip", "usgs", "eox2023", "eox2016")
# The line the mod shows (V1_3_SPEC §C), for the default chain: every source's own credit, EOX's in
# its June 2026 wording (docs/research/2026-10-10-rate-limits.md: "EOxCloudless https://cloudless.eox.at
# by EOX IT Services GmbH (Contains modified Copernicus Sentinel data 2023)", 2016: "... 2016 & 2017").
ATTRIBUTION = "Imagery: " + "; ".join(SOURCES[n].attribution for n in DEFAULT_CHAIN)


class ImageryError(IOError):
    """Imagery could not be fetched (network or server errors, not a missing tile). deferred: a
    prefetch / background request the limiter dropped (a demand fetch may still get it)."""

    def __init__(self, msg, deferred=False):
        super().__init__(msg)
        self.deferred = deferred


# netfetch errors of a non-demand request that a demand request would not get
_DEFERRED = (netfetch.Deferred, netfetch.PolicyDenied, netfetch.BudgetExceeded)


def parse_chain(text):
    """--imagery value -> tuple of source names, or None for 'off'."""
    if text.strip().lower() == "off":
        return None
    chain = tuple(s.strip().lower() for s in text.split(",") if s.strip())
    bad = [s for s in chain if s not in SOURCES]
    if bad or not chain:
        raise ValueError(f"unknown imagery source(s) {bad or text!r}; choose from {', '.join(SOURCES)} or off")
    return chain


def tone_lut(strength=1.0, lift=1.0):
    """Mild global tone curve as a 256-entry LUT (applied per channel, sRGB values).

    Satellite images have the sun's shading and shadows baked in, and the engine lights the
    ground again, so the curve lifts the deepest shadows a little (+0.06 at black) and pulls
    the brightest highlights down (-0.10 at white, so bare rock and sand do not glow under the
    sun) while leaving mid-tones almost unchanged (-0.005 at 0.5). strength 0 = identity; the
    curve stays monotonic for strength <= 3. lift scales the shadow lift alone (de-shadowed
    texels, deshadow.FULL_TONE_LIFT)."""
    x = np.arange(256) / 255.0
    y = x + strength * (lift * 0.06 * (1 - x) ** 3 - 0.10 * x ** 3)
    return np.clip(np.rint(y * 255), 0, 255).astype(np.uint8)


def ground_res(lat, z):
    """Web-Mercator ground resolution (m per 256 px tile pixel) at latitude lat and zoom z."""
    return EARTH_CIRC * math.cos(math.radians(min(abs(lat), MAX_LAT))) / (TILE * 2 ** z)


def zoom_for(lat, texel_m, max_zoom=19):
    """Zoom whose ground resolution is closest (in log2) to the texel size."""
    c = EARTH_CIRC * max(math.cos(math.radians(min(abs(lat), MAX_LAT))), 1e-6)
    return int(min(max_zoom, max(0, round(math.log2(c / (TILE * texel_m))))))


def mercator_px(lat, lon, z):
    """Global pixel coordinates (x right, y down; pixel centres at +0.5) of lat/lon at zoom z."""
    w = TILE * 2 ** z
    x = (np.asarray(lon, np.float64) / 360.0 + 0.5) * w
    y = (0.5 - np.arcsinh(np.tan(np.radians(lat))) / (2 * np.pi)) * w
    return x, y


def is_uniform(a):
    """True for a one-colour (blank) tile: every channel varies by at most BLANK_RANGE. A sparse sample first: its
    range is at most the tile's, so a photo is told apart without reducing every pixel (~9 s of a teleport's
    fetch threads in Fells Point, 2026-10-16); only a sample that looks blank checks them all."""
    if a.ndim == 3 and a.shape[0] >= 32 and a.shape[1] >= 32:
        sm = a[::16, ::16]
        if int((sm.max(axis=(0, 1)).astype(int) - sm.min(axis=(0, 1))).max()) > BLANK_RANGE:
            return False
    return int((a.max(axis=(0, 1)).astype(int) - a.min(axis=(0, 1))).max()) <= BLANK_RANGE


MATCH_SIGMA_M = 40.0     # m: match_colours keeps the detail below this scale, the colours above it (20: haloes)


MATCH_DOWN, MATCH_MIN_PX = 8, 16    # render(): the reference at px / MATCH_DOWN (at least MATCH_MIN_PX)


def match_colours(rgb, ref, sigma_px):
    """rgb with the large-scale colour of ref (uint8; ref the same size or coarser, sigma_px in ref's texels):
    rgb * blur(ref) / blur(rgb), per channel, the gain clipped to [1/3, 3] (Gaussian blur of sigma_px
    texels) and, for a coarser ref, computed at its size and scaled up. Where rgb already is ref, nothing
    changes; elsewhere the detail is rgb's and everything coarser than ~sigma ref's."""
    r = max(1.0, float(sigma_px))
    h, w = ref.shape[:2]
    im = Image.fromarray(rgb, "RGB")
    if (h, w) != rgb.shape[:2]:
        im = im.resize((w, h), Image.BOX)
    a = im.filter(ImageFilter.GaussianBlur(r))
    b = Image.fromarray(ref, "RGB").filter(ImageFilter.GaussianBlur(r))
    gain = np.clip((np.asarray(b, np.float32) + 4.0) / (np.asarray(a, np.float32) + 4.0), 1 / 3, 3)
    if (h, w) != rgb.shape[:2]:
        gain = np.stack([np.asarray(Image.fromarray(gain[..., c], "F").resize(rgb.shape[1::-1], Image.BILINEAR))
                         for c in range(3)], -1)
    out = rgb.astype(np.float32) * gain
    return np.clip(np.rint(out), 0, 255).astype(np.uint8)


def in_bounds(src, z, x, y):
    """Tile (z, x, y) overlaps src.bounds (True when the source has none)."""
    if not src.bounds:
        return True
    s, w, n, e = src.bounds
    k = 2 ** z
    lon0, lon1 = x / k * 360 - 180, (x + 1) / k * 360 - 180
    lat1 = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / k))))
    lat0 = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / k))))
    return lon1 > w and lon0 < e and lat1 > s and lat0 < n


# The US federal sources have no bounds (their coverage probes find where they have data); for the world map's
# credits their area is the US: (s, w, n, e), the lower 48 for NAIP, with Alaska and Hawaii for USGS
US_CREDIT_BOXES = {"naip": (24.0, -125.0, 50.0, -66.0), "naipusda": (24.0, -125.0, 50.0, -66.0),
                   "usgs": (18.0, -180.0, 72.0, -65.0)}


def range_in_bounds(src, z, tx0, tx1, ty0, ty1):
    """Any tile of the range (z; tx0..tx1, ty0..ty1, inclusive) overlaps src.bounds."""
    if not src.bounds:
        return True
    s, w, n, e = src.bounds
    k = 2 ** z
    lon0, lon1 = tx0 / k * 360 - 180, (tx1 + 1) / k * 360 - 180
    lat1 = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty0 / k))))
    lat0 = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (ty1 + 1) / k))))
    return lon1 > w and lon0 < e and lat1 > s and lat0 < n


def lat_lon_bbox(mb):
    """An EPSG:3857 extent (minx, miny, maxx, maxy) as WMS 1.3.0's EPSG:4326 BBOX: "south,west,north,east" (latitude
    first). The source draws it in plate carree: over an export parent (~1 km) its rows are within ~1e-4 of the
    tile's Mercator rows (a fraction of a pixel)."""
    x0, y0, x1, y1 = mb
    lon = lambda x: math.degrees(x / R_MERC)                                            # noqa: E731
    lat = lambda y: math.degrees(2.0 * math.atan(math.exp(y / R_MERC)) - math.pi / 2)  # noqa: E731
    return f"{lat(y0):.7f},{lon(x0):.7f},{lat(y1):.7f},{lon(x1):.7f}"


def mercator_bbox(z, x, y):
    """EPSG:3857 extent (minx, miny, maxx, maxy) of XYZ tile (z, x, y)."""
    size = EARTH_CIRC / 2 ** z
    x0, y1 = -EARTH_CIRC / 2 + x * size, EARTH_CIRC / 2 - y * size
    return x0, y1 - size, x0 + size, y1


@dataclass(frozen=True)
class Tile:
    """A decoded source tile: RGB uint8 [256, 256, 3], and whether it is one colour (blank)."""
    rgb: np.ndarray
    uniform: bool


def as_tile(rgb):
    return Tile(rgb, is_uniform(rgb))


_SAME = object()                    # colour-match LRU marker: the tile is unchanged
# render(pending=) entries: a colour-match region is (rx, ry); a de-shadow region still to come is
# (DS_PENDING, rx, ry) (both z12, so the tag keeps them apart; earthd asks the matching object)
DS_PENDING = "ds"


class Imagery:
    disk = cachemod.NONE    # the server's cache.Cache (last use per file, the size cap); set by earthd
    # strict: a source that fails (offline: its tile not on disk) fails the render instead of the next source
    # standing in, so a far-tile worker's texture (blockworker.far_tile, from the disk alone) is the one the server
    # would render, or none (the server renders it)
    strict = False

    def __init__(self, cache_dir, chain=DEFAULT_CHAIN, fetch_workers=FETCH_WORKERS, mem_tiles=None,
                 tone=1.0, deshadow=None, colormatch=None, fetcher=None, leaf_on=True, season_mode="off"):
        """deshadow: an optional deshadow.Deshadow (--deshadow lambert|full): _mosaic relights its
        source's tiles; None = the imagery as served (byte-identical textures).
        colormatch: an optional colormatch.ColorMatch (--colormatch s2): _mosaic removes the colour
        seams of its source's tiles, after the relight and before the tone curve; None = off.
        fetcher: the server's netfetch.Fetcher (shared limits); None = one of its own.
        leaf_on: leaf-off sources' vegetation takes the summer reference's colour (season.leaf_on).
        season_mode: the season grade of every texture (season.amounts_for: "auto" = today's, "fall", "off")."""
        self.cache_dir = cache_dir
        self.chain = tuple(SOURCES[s] if isinstance(s, str) else s for s in chain)
        self.fetcher = fetcher if fetcher is not None else netfetch.Fetcher()
        self.fetcher.register_chain("imagery", [s.url for s in self.chain])
        self.pool = ThreadPoolExecutor(min(fetch_workers, FETCH_WORKERS), thread_name_prefix="img-fetch")
        self.mem_tiles = MEM_TILES if mem_tiles is None else mem_tiles
        self.leaf_on = leaf_on
        self.season_mode = season_mode
        self.lut = tone_lut(tone)
        self.lut_table = self.lut.tolist() * 3          # Image.point: one LUT per band
        self.deshadow = deshadow
        if deshadow is not None:
            deshadow.bind(self)
            self.lut_ds = tone_lut(tone, deshadow.tone_lift)    # texels of relit source tiles
        self.colormatch = colormatch
        if colormatch is not None:
            colormatch.bind(self)
        self._cm_lru = OrderedDict()        # (source, z, x, y, "cm1", relit) -> corrected RGB, or the input
        self._lru = OrderedDict()           # (source, z, x, y) -> Tile, or None (404)
        self._export_locks = {}             # export parent -> its fetch lock (_download_export)
        self._warm_want = []                # warm(): lazy tiles ahead of the player, nearest first
        self._warm_cv = threading.Condition()
        self._warm_thread = None
        self._inflight = {}
        self._lock = threading.RLock()     # RLock: a done future's callback runs at once
        self._local = threading.local()     # per fetch thread: the kind of the request being loaded
        self.stats = {}                     # counters ("usgs:net", "eox2023:404", ...), and fetch times
        self.fetch_ms = []                  # network fetch latencies (ms), for the log / measurements

    def attribution(self):
        """Attribution line for the sources in use (with --colormatch, its reference layers too:
        their data shape the USGS colours even where they are not in the chain)."""
        names = [s.name for s in self.chain]
        extra = [SOURCES[n] for n in getattr(self.colormatch, "refs", ()) if n not in names]
        if names == list(DEFAULT_CHAIN) and not extra:
            return ATTRIBUTION
        return "Imagery: " + "; ".join(s.attribution for s in list(self.chain) + extra)

    def credits_by_area(self):
        """[[attribution, w, s, e, n] or [attribution]] per source in use (a box: the source covers only that
        area), for the world map to credit what its view shows (2026-10-15: the map listed 31 regional services
        over Paris)."""
        names = [s.name for s in self.chain]
        extra = [SOURCES[n] for n in getattr(self.colormatch, "refs", ()) if n not in names]
        out = []
        for src in list(self.chain) + extra:
            box = src.bounds or US_CREDIT_BOXES.get(src.name)
            if box:
                s_, w_, n_, e_ = box
                out.append([src.attribution, w_, s_, e_, n_])
            else:
                out.append([src.attribution])
        return out

    def _count(self, what):
        with self._lock:
            self.stats[what] = self.stats.get(what, 0) + 1

    def stats_snapshot(self):
        with self._lock:
            return dict(self.stats)

    # --- raw tiles -------------------------------------------------------------------------
    def summary(self):
        """Counters and network fetch latency (median / p95 ms), for the log."""
        with self._lock:
            ms = sorted(self.fetch_ms)
            st = dict(sorted(self.stats.items()))
        lat = f"fetch {ms[len(ms) // 2]:.0f}/{ms[int(len(ms) * 0.95)]:.0f} ms (median/p95, n={len(ms)})" if ms else "no fetches"
        extra = [o.summary() for o in (self.deshadow, self.colormatch) if o is not None]
        return "; ".join([lat, str(st)] + extra)

    def _paths(self, src, z, x, y):
        d = os.path.join(self.cache_dir, src.name, str(z), str(x))
        return os.path.join(d, f"{y}.jpg"), os.path.join(d, f"{y}.404")

    def _discard(self, src, z, x, y):
        try:
            os.remove(self._paths(src, z, x, y)[0])
        except OSError:
            pass
        self.disk.forget(self._paths(src, z, x, y)[0])

    def _download(self, src, z, x, y):
        """Raw tile bytes from the disk cache or the network; None for a (remembered) 404."""
        path, miss = self._paths(src, z, x, y)
        data = self.disk.read(path)             # None: not cached (or evicted a moment ago)
        if data is not None:
            self._count(src.name + ":disk")
            return data
        if src.export:
            return self._download_export(src, z, x, y)
        url = src.url.format(z=z, x=x, y=y, ty=(1 << z) - 1 - y)     # {ty}: a TMS cache's flipped row
        if self.fetcher.miss_alive(miss, url):
            return None
        kind = getattr(self._local, "kind", "demand")
        try:
            r = self.fetcher.get(url, kind=kind)
        except netfetch.NotFound:               # outside the source's coverage
            os.makedirs(os.path.dirname(miss), exist_ok=True)
            open(miss, "wb").close()            # (re)written: its mtime starts the marker's TTL
            self.disk.wrote(miss, 0)
            self._count(src.name + ":404")
            return None
        except netfetch.FetchError as e:
            deferred = kind != "demand" and isinstance(e, _DEFERRED)
            self._count(src.name + (":deferred" if deferred else ":skipped" if isinstance(e, netfetch.HostUnavailable)
                                    else ":error"))
            raise ImageryError(f"imagery fetch failed: {url}: {e}", deferred) from None
        data = r.body
        with self._lock:
            self.fetch_ms.append(r.elapsed * 1000)
            del self.fetch_ms[:-2000]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{threading.get_ident()}.tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        staging.replace_cached(tmp, path)
        self.disk.wrote(path, len(data))
        if os.path.exists(miss):                # an expired marker: the source has data there now
            os.remove(miss)
            self.disk.forget(miss)
        self._count(src.name + ":net")
        return data

    def _download_export(self, src, z, x, y):
        """An export source's tile: its parent (src.export levels up) fetched as one image and split into
        its children on disk (one fetch per parent at a time); None if the host has no image there."""
        k = src.export
        parent = (src.name, z - k, x >> k, y >> k)
        with self._lock:
            lock = self._export_locks.setdefault(parent, threading.Lock())
        with lock:
            path, miss = self._paths(src, z, x, y)
            data = self.disk.read(path)         # a sibling's fetch wrote it meanwhile
            if data is not None:
                self._count(src.name + ":disk")
                return data
            mb = mercator_bbox(z - k, x >> k, y >> k)
            if src.url == bcorthomod.URL:
                return self._render_bcortho(src, z, x, y, mb)
            url = src.url.format(bbox=",".join(f"{v:.3f}" for v in mb), bbox4326=lat_lon_bbox(mb), size=TILE << k)
            if self.fetcher.miss_alive(miss, url):
                return None
            kind = getattr(self._local, "kind", "demand")
            try:
                r = self.fetcher.get(url, kind=kind)
            except netfetch.NotFound:
                os.makedirs(os.path.dirname(miss), exist_ok=True)
                open(miss, "wb").close()
                self.disk.wrote(miss, 0)
                self._count(src.name + ":404")
                return None
            except netfetch.FetchError as e:
                deferred = kind != "demand" and isinstance(e, _DEFERRED)
                self._count(src.name + (":deferred" if deferred else ":skipped" if isinstance(e, netfetch.HostUnavailable)
                                        else ":error"))
                raise ImageryError(f"imagery fetch failed: {url}: {e}", deferred) from None
            with self._lock:
                self.fetch_ms.append(r.elapsed * 1000)
                del self.fetch_ms[:-2000]
            try:
                im = Image.open(io.BytesIO(r.body)).convert("RGB")
            except (OSError, ValueError) as e:
                raise ImageryError(f"undecodable {src.name} image {parent}: {e}") from None
            if im.size != (TILE << k, TILE << k):
                im = im.resize((TILE << k, TILE << k), Image.BILINEAR)
            out = self._split_export(src, z, x, y, np.asarray(im))
            self._count(src.name + ":net")
            return out

    def _render_bcortho(self, src, z, x, y, mb):
        """The bcortho source's export parent rendered from the LidarBC orthophotos (bcortho.py) instead of fetched,
        split like an export's (texels it has no photo for are black: the next source fills those tiles)."""
        k = src.export
        bo = getattr(self, "_bcortho", None)
        if bo is None:
            with self._lock:
                bo = getattr(self, "_bcortho", None)
                if bo is None:
                    root = os.path.dirname(os.path.abspath(self.cache_dir))     # <cache>/imagery -> <cache>
                    bo = self._bcortho = bcorthomod.BCOrtho(os.path.join(root, "imagery", "bcortho-src"), self.fetcher)
        path, miss = self._paths(src, z, x, y)
        try:
            rgb, ok = bo.render(mb, TILE << k)
        except netfetch.FetchError as e:
            kind = getattr(self._local, "kind", "demand")
            deferred = kind != "demand" and isinstance(e, _DEFERRED)
            self._count(src.name + (":deferred" if deferred else ":error"))
            raise ImageryError(f"imagery render failed: {src.name} {z}/{x}/{y}: {e}", deferred) from None
        if rgb is None:
            os.makedirs(os.path.dirname(miss), exist_ok=True)
            open(miss, "wb").close()
            self.disk.wrote(miss, 0)
            self._count(src.name + ":404")
            return None
        rgb = np.where(ok[..., None], rgb, 0).astype(np.uint8)          # (the export's black: outside the photos)
        out = self._split_export(src, z, x, y, rgb)
        self._count(src.name + ":net")
        return out

    def _split_export(self, src, z, x, y, a):
        """An export parent's image a ((TILE << export)² RGB) written as its children's tiles; tile (x, y)'s bytes, or
        None when it is outside the data (a miss marker written)."""
        k = src.export
        out = None
        for j in range(1 << k):
            for i in range(1 << k):
                cx, cy = ((x >> k) << k) + i, ((y >> k) << k) + j
                cpath, cmiss = self._paths(src, z, cx, cy)
                child = a[j * TILE:(j + 1) * TILE, i * TILE:(i + 1) * TILE]
                if (child.max(axis=2) <= NODATA_MAX).mean() > NODATA_SHARE or (
                        src.nodata == "white" and (child.min(axis=2) >= NODATA_WHITE_MIN).mean() > NODATA_WHITE_SHARE):
                    # outside the flights (the image is black there): missing, the next source fills it
                    os.makedirs(os.path.dirname(cmiss), exist_ok=True)
                    open(cmiss, "wb").close()
                    self.disk.wrote(cmiss, 0)
                    continue
                buf = io.BytesIO()
                Image.fromarray(child).save(buf, "JPEG", quality=92)
                b = buf.getvalue()
                os.makedirs(os.path.dirname(cpath), exist_ok=True)
                tmp = f"{cpath}.{threading.get_ident()}.tmp"
                with open(tmp, "wb") as f:
                    f.write(b)
                staging.replace_cached(tmp, cpath)
                self.disk.wrote(cpath, len(b))
                if (cx, cy) == (x, y):
                    out = b
        return out

    def raw(self, src, z, x, y, kind="demand"):
        """A source tile's bytes as served (disk cache, else the network; in the calling thread), None where the
        source has none (404). Also for sources outside the chain (the world map's EOX map / labels layers,
        maptiles.LAYER_SOURCES). Raises ImageryError when the fetch failed."""
        self._local.kind = kind
        try:
            return self._download(src, z, x, y)
        finally:
            self._local.kind = "demand"

    def _on_disk(self, key):
        src, z, x, y = key
        return os.path.exists(self._paths(src, z, x, y)[0])

    def _load_kind(self, key, kind):
        """_load(key) in a pool thread, its requests made as `kind` (subclasses override _load(key))."""
        self._local.kind = kind
        try:
            return self._load(key)
        finally:
            self._local.kind = "demand"

    def _load(self, key):
        """Decoded tile (Tile), or None if the source has none there."""
        src, z, x, y = key
        data = self._download(src, z, x, y)
        if data is None:
            return None
        try:
            im = Image.open(io.BytesIO(data))
            if im.mode in ("P", "LA", "RGBA"):     # EOX's ocean PNG: paletted with transparency
                im = im.convert("RGBA")
            im = im.convert("RGB")
        except (OSError, ValueError) as e:     # not an image (an error page served as 200?)
            self._discard(src, z, x, y)
            raise ImageryError(f"undecodable {src.name} tile {z}/{x}/{y}: {e}") from None
        if im.size != (TILE, TILE):
            im = im.resize((TILE, TILE), Image.BILINEAR)
        return as_tile(np.asarray(im))

    @staticmethod
    def _probe_key(key):
        """The finest coverage-probe ancestor of a tile, or None (probes chain: the probe's own
        probe is checked when the probe is fetched through tiles())."""
        src, z, x, y = key
        pz = max((p for p in src.probe_zooms if p < z), default=None)
        if pz is None:
            return None
        return (SOURCES[src.probe_source] if src.probe_source else src, pz, x >> (z - pz), y >> (z - pz))

    @staticmethod
    def _covered(probe):
        return probe is not None and not probe.uniform

    def _probe_status(self, key):
        """'covered' / 'outside' / 'unknown' from the key's coverage probes, coarsest first;
        starts the fetch of the first unknown one. Caller holds the lock."""
        src, z, x, y = key
        if not in_bounds(*key):
            return "outside"                # (never ask a source about a place outside its area)
        psrc = SOURCES[src.probe_source] if src.probe_source else src
        for pz in sorted(p for p in src.probe_zooms if p < z):
            p = (psrc, pz, x >> (z - pz), y >> (z - pz))
            if p not in self._lru:
                self._future(p, "prefetch")
                return "unknown"
            if not self._covered(self._lru[p]):
                return "outside"
        return "covered"

    def _future(self, key, kind="demand", stale=None):
        """The fetch of key (started if needed, or when the one in flight is `stale`, a finished
        fetch whose done callback has not run yet). Caller holds the lock."""
        fut = self._inflight.get(key)
        if fut is None or fut is stale:
            fut = self._inflight[key] = self.pool.submit(self._load_kind, key, kind)
            fut.add_done_callback(lambda f: self._finish(key, f))
        return fut

    def _result(self, key, fut, kind):
        """fut's tile; a demand caller that met a dropped prefetch fetches the tile itself (one fetch
        shared by every caller that met the same dropped one)."""
        while True:                         # (a demand fetch is never dropped: this ends)
            try:
                return fut.result()
            except ImageryError as e:
                if not e.deferred or kind != "demand":
                    raise
            with self._lock:
                fut = self._future(key, stale=fut)

    def tiles(self, keys, kind="demand"):
        """{(source, z, x, y): Tile or None (404, or outside the coverage probe)}, fetched
        concurrently and deduplicated across callers. Raises ImageryError if a fetch failed.
        kind: the netfetch kind of the requests ("background": de-shadow region fits)."""
        keys = list(dict.fromkeys(keys))
        probes = {k: self._probe_key(k) for k in keys}
        known = self.tiles({p for p in probes.values() if p is not None}, kind) if any(probes.values()) else {}
        out, futs = {}, {}
        with self._lock:
            for k in keys:
                if probes[k] is not None and not self._covered(known[probes[k]]):
                    out[k] = None           # the source has nothing in this area
                elif k in self._lru:
                    self._lru.move_to_end(k)
                    out[k] = self._lru[k]
                elif not in_bounds(*k):
                    out[k] = None                   # outside the source's area
                elif k[0].lazy and not self._on_disk(k):
                    out[k] = None                   # not fetched (warm() does, ahead): the next source now
                else:
                    futs[k] = self._future(k, kind)
        err = None
        for k, fut in futs.items():
            try:
                out[k] = self._result(k, fut, kind)
            except ImageryError as e:
                err = e
        if err is not None:
            raise err
        return out

    def prefetch(self, frame, x0, y0, size, px):
        """Start fetching the source tiles render(frame, x0, y0, size, px) will need, without
        waiting: the first source of the chain, or the next one where every tile of the
        previous source is already known to be missing (an unknown coverage probe is fetched
        first). Errors are left for render to meet. Skips a source whose host's circuit is open
        (render fails over past it) and stops at one whose host takes no prefetch now (policy,
        budget, bucket below half): render fetches those on demand."""
        t = size / px
        c = np.array([0.0, 0.5, 1.0]) * size
        E, N = np.meshgrid(x0 + c, y0 + c)
        lat, lon, _ = frame.enu_to_geodetic(E, N, 0.0)
        if np.abs(lat).max() > MAX_LAT:
            return
        zw = zoom_for(float(lat[1, 1]), t)
        for k, src in enumerate(self.chain):
            z = min(zw, src.max_zoom)
            if z < src.min_zoom or src.lazy:    # (a lazy source's tiles: only warm() fetches them)
                continue
            n = 2 ** z
            fx, fy = mercator_px(lat.ravel(), lon.ravel(), z)
            fx = fx - n * TILE * np.round((fx - fx[0]) / (n * TILE))
            tx0, tx1 = int(math.floor(fx.min() - 1)) // TILE, int(math.floor(fx.max() + 1)) // TILE
            ty0, ty1 = max(0, int(math.floor(fy.min() - 1)) // TILE), min(n - 1, int(math.floor(fy.max() + 1)) // TILE)
            if (tx1 - tx0 + 1) * (ty1 - ty0 + 1) > MAX_MOSAIC:
                return
            if not range_in_bounds(src, z, tx0, tx1, ty0, ty1):
                # outside the source's area: never asked, and the prefetch ends here as it did when this
                # source's coverage probe was fetched first (2026-10-15: carrying on to the base sources made
                # the integration drive's far field 1729 coverage holes; tonight's game measurements all ran
                # with this behaviour; render fetches what is missing on demand)
                return
            host = netfetch.host_of(src.url)
            if not self.fetcher.available(host):
                continue                    # circuit open: render fails over to the next source, warm that
            # --colormatch: their regions' seams, in the background (not under the lock: it may read a
            # field from disk); before the prefetch check, so a host that takes no prefetch right now
            # still gets the regions render will need queued (they fetch as kind "background")
            if self.colormatch is not None:
                for ty in range(ty0, ty1 + 1):
                    for tx in range(tx0, tx1 + 1):
                        self.colormatch.request((src, z, tx % n, ty))
            if not self.fetcher.allow(host, "prefetch"):
                return
            missing = True                  # every tile of this source known to be missing
            with self._lock:
                for ty in range(ty0, ty1 + 1):
                    for tx in range(tx0, tx1 + 1):
                        key = (src, z, tx % n, ty)
                        cover = self._probe_status(key)
                        if cover == "outside":
                            continue
                        if cover == "unknown":          # a probe is being fetched first
                            missing = False
                            continue
                        if key in self._lru:
                            a = self._lru[key]
                            missing &= a is None or (k < len(self.chain) - 1 and a.uniform)
                            continue
                        missing = False
                        self._future(key, "prefetch")
            if not missing:
                return

    def warm(self, frame, points, radius, texel_m):
        """Fetch the lazy sources' tiles ahead of the player (one background thread, nearest first): the
        tiles at the zoom of `texel_m` m texels (the block's base photo) within `radius` m of the ENU
        points (the player's path, in order). Replaces the previous list (the player moved on)."""
        lazy = [s for s in self.chain if s.lazy]
        if not lazy or not points:
            return
        pts = np.asarray(points, float)
        lat, lon, _ = frame.enu_to_geodetic(pts[:, 0], pts[:, 1], np.zeros(len(pts)))
        if np.abs(lat).max() > MAX_LAT:
            return
        want, seen = [], set()
        pad = radius / 111000.0 * 2         # deg: a generous margin for the bounds test below
        for la, lo in zip(np.atleast_1d(lat), np.atleast_1d(lon)):
            for src in lazy:                # per place, the lazy sources in chain order (_warm_done)
                if src.bounds:              # a regional source elsewhere: none of its tiles (17 of them since
                    bs, bw, bn, be = src.bounds     # the regional chain: their per-tile loop cost the far field)
                    if la < bs - pad or la > bn + pad or lo < bw - pad or lo > be + pad:
                        continue
                z = min(zoom_for(float(lat[0]), texel_m), src.max_zoom)
                if z < src.min_zoom:
                    continue
                k = src.export
                r_px = radius / ground_res(float(la), z)
                fx, fy = mercator_px(np.array([la]), np.array([lo]), z)
                for ty in range(int((fy[0] - r_px) // TILE), int((fy[0] + r_px) // TILE) + 1):
                    for tx in range(int((fx[0] - r_px) // TILE), int((fx[0] + r_px) // TILE) + 1):
                        key = (src, z, (tx >> k) << k, (ty >> k) << k)   # one child per parent
                        if key not in seen and in_bounds(*key):
                            seen.add(key)
                            want.append(key)
        with self._warm_cv:
            self._warm_want = want
            if self._warm_thread is None:
                self._warm_thread = threading.Thread(target=self._warm_loop, name="img-warm", daemon=True)
                self._warm_thread.start()
            self._warm_cv.notify()

    def _warm_loop(self):
        self._local.kind = "prefetch"
        done, want = set(), None    # keys of this list found done: not looked at on disk again at every wake
        while True:
            with self._warm_cv:
                key = None
                while key is None:
                    if self._warm_want is not want:     # a new list (the player moved on): looked at afresh
                        done, want = set(), self._warm_want
                    for k in self._warm_want:
                        if k in done:
                            continue
                        if self._warm_done(k):
                            done.add(k)
                            continue
                        key = k
                        break
                    if key is None:
                        self._warm_cv.wait()
            src, z, x, y = key
            last = (1 << src.export) - 1
            try:
                # the child _warm_done looks for: with only some children on disk (the cache cap evicted the
                # others) the first one was a disk hit, the key never done, and this thread read it in a loop
                # (170 s of the server process's CPU in a 4-minute drive, 2026-10-20)
                self._download(src, z, x | last, y | last)
                self._count(src.name + ":warm")
            except ImageryError as e:       # the host takes no prefetch now / failed: later
                with self._warm_cv:
                    self._warm_cv.wait(5.0 if e.deferred else 30.0)
                continue
            except Exception:
                log.exception("imagery warm %s", key[1:])
            with self._warm_cv:             # fetched (or no use asking again): never the same key in a loop
                done.add(key)

    def _warm_done(self, key):
        """Nothing (more) to fetch for key's parent: it was fetched (its last child, the one written last, is
        on disk or known to be missing), an earlier lazy source of the chain has data there (its last
        child is on disk: this one is only the fallback where that one is black), or the area is outside
        the coverage probes."""
        src, z, x, y = key
        last = (1 << src.export) - 1
        for s in self.chain:
            if s is src:
                break
            if s.lazy and s.export == src.export and os.path.exists(self._paths(s, z, x | last, y | last)[0]):
                return True
        path, miss = self._paths(src, z, x | last, y | last)
        if os.path.exists(path) or os.path.exists(miss):
            return True
        with self._lock:
            return self._probe_status(key) == "outside"

    def lazy_ready(self, frame, x0, y0, size, px):
        """True when render(frame, x0, y0, size, px) would find every lazy-source tile it can use on disk
        (or known missing, or outside the coverage probes): rendering again would not get any sharper."""
        lazy = [s for s in self.chain if s.lazy]
        if not lazy:
            return True
        c = np.array([0.0, 0.5, 1.0]) * size
        E, N = np.meshgrid(x0 + c, y0 + c)
        lat, lon, _ = frame.enu_to_geodetic(E, N, 0.0)
        if np.abs(lat).max() > MAX_LAT:
            return True
        zw = zoom_for(float(lat[1, 1]), size / px)
        z = min(zw, lazy[0].max_zoom)
        if z < lazy[0].min_zoom:
            return True
        fx, fy = mercator_px(lat.ravel(), lon.ravel(), z)
        lazy = [s_ for s_ in lazy if not s_.bounds or not (lat.max() < s_.bounds[0] or lat.min() > s_.bounds[2]
                                                           or lon.max() < s_.bounds[1] or lon.min() > s_.bounds[3])]
        for ty in range(int(math.floor(fy.min() - 1)) // TILE, int(math.floor(fy.max() + 1)) // TILE + 1):
            for tx in range(int(math.floor(fx.min() - 1)) // TILE, int(math.floor(fx.max() + 1)) // TILE + 1):
                for src in lazy:            # the first with data decides; a missing one passes to the next
                    if not in_bounds(src, z, tx, ty):
                        continue
                    path, miss = self._paths(src, z, tx, ty)
                    if os.path.exists(path):
                        break
                    if os.path.exists(miss):
                        continue
                    with self._lock:
                        if self._probe_status((src, z, tx, ty)) == "outside":
                            continue
                    return False
        return True

    def _finish(self, key, fut):
        with self._lock:
            if self._inflight.get(key) is fut:
                del self._inflight[key]
            if not fut.cancelled() and fut.exception() is None:
                self._lru[key] = fut.result()
                while len(self._lru) > self.mem_tiles:
                    self._lru.popitem(last=False)

    # --- sampling ----------------------------------------------------------------------------
    def _mosaic(self, src, z, fx, fy, last, pending=None, corrected=None):
        """RGBA mosaic of src's tiles at zoom z covering global pixel coordinates fx, fy (A = 255
        where the source has data, 254 where a de-shadowed tile replaced it); returns (image,
        origin x, origin y, any data) or None if it would take more than MAX_MOSAIC tiles.
        pending: a set that collects the colour-match regions not ready yet (tiles left raw) and
        the de-shadow regions still to come, as (DS_PENDING, rx, ry);
        corrected: a set that collects the keys of the tiles the colour match changed."""
        n = 2 ** z
        tx0, tx1 = int(math.floor(fx.min() - 1)) // TILE, int(math.floor(fx.max() + 1)) // TILE
        ty0 = max(0, int(math.floor(fy.min() - 1)) // TILE)
        ty1 = min(n - 1, int(math.floor(fy.max() + 1)) // TILE)
        nx, ny = tx1 - tx0 + 1, ty1 - ty0 + 1
        if nx * ny > MAX_MOSAIC or ny <= 0:
            return None
        keys = {(tx, ty): (src, z, tx % n, ty) for ty in range(ty0, ty1 + 1) for tx in range(tx0, tx1 + 1)}
        got = self.tiles(keys.values())
        mosaic = np.zeros((ny * TILE, nx * TILE, 4), np.uint8)
        any_data = False
        for (tx, ty), k in keys.items():
            a = got[k]
            if a is None or (not last and a.uniform):
                continue                    # missing here: the next source fills it
            c, r = (tx - tx0) * TILE, (ty - ty0) * TILE
            # --deshadow: a region still to come is asked before relight() (see pending_region)
            ds_wait = (self.deshadow.pending_region(k, a)
                       if pending is not None and hasattr(self.deshadow, "pending_region") else None)
            relit = None if self.deshadow is None else self.deshadow.relight(k, a, self)
            if relit is None and ds_wait is not None:
                pending.add((DS_PENDING,) + tuple(ds_wait))
            rgb = a.rgb if relit is None else relit
            if self.colormatch is not None and not a.uniform and self.colormatch.handles(k):
                cm = self._colormatched(k, rgb, relit is not None, pending)
                if cm is not rgb and corrected is not None:
                    corrected.add(k)
                rgb = cm
            mosaic[r:r + TILE, c:c + TILE, :3] = rgb
            mosaic[r:r + TILE, c:c + TILE, 3] = 255 if relit is None else 254
            any_data = True
        return mosaic, tx0 * TILE, ty0 * TILE, any_data

    def _colormatched(self, key, rgb, relit, pending):
        """The colour-seam corrected RGB of a source tile (raw or relit: raw -> de-shadow -> colour
        gain -> tone curve), from a second LRU; the raw Tile in _lru stays as it is."""
        ck = key + ("cm1", relit)
        with self._lock:
            v = self._cm_lru.get(ck)
            if v is not None:
                self._cm_lru.move_to_end(ck)
                return rgb if v is _SAME else v
        out, info = self.colormatch.apply(key, rgb, self)
        if out is self.colormatch.PENDING:     # its region is not ready: raw this time
            if pending is not None:
                pending.add(info)
            return rgb
        if info:                            # cacheable (not a neighbour-fetch fallback)
            with self._lock:
                self._cm_lru[ck] = _SAME if out is None else out
                while len(self._cm_lru) > self.mem_tiles:
                    self._cm_lru.popitem(last=False)
        return rgb if out is None else out

    def render(self, frame, x0, y0, size, px, pending=None):
        """_render(); texels from a lazy source (NAIP) then take their large-scale colour from the rest of
        the chain (match_colours): NAIP's detail, USGS's colours, so the block's photo meets the far tiles
        around it (USGS, colour-matched and de-shadowed) without a seam (in-game 2026-10-13, Annapolis: the
        NAIP block was a darker, greener square, its water much darker)."""
        rgb, used = self._render(frame, x0, y0, size, px, pending)
        if any(s.lazy and s.name in used for s in self.chain) and not all(s.lazy for s in self.chain):
            # the reference only gives the colour above MATCH_SIGMA_M: rendered and matched at MATCH_DOWN times
            # coarser texels (64x fewer texels to render, blur and stream through memory than at full size)
            small = max(MATCH_MIN_PX, px // MATCH_DOWN)
            try:
                ref, _ = self._render(frame, x0, y0, size, small, pending, skip_lazy=True)
            except ImageryError:            # the reference is unreachable: NAIP as served
                ref = None
            if ref is not None:
                rgb = match_colours(rgb, ref, MATCH_SIGMA_M / (size / small))
                if self.leaf_on and any(s.leaf_off and s.name in used for s in self.chain):
                    rgb = season.leaf_on(rgb, ref)
                    used["leafon"] = sum(used.get(s.name, 0) for s in self.chain if s.leaf_off)
        return self._seasoned(frame, x0, y0, size, rgb), used

    SEASON_GRID = 17            # world positions for the season noise: on this grid, bilinear in between

    def _seasoned(self, frame, x0, y0, size, rgb):
        """rgb graded toward the season (season.grade; season_mode "off": unchanged)."""
        if self.season_mode in (None, "off", "summer"):
            return rgb
        n = self.SEASON_GRID
        k = np.linspace(0.0, size, n)
        E, N = np.meshgrid(x0 + k, y0 + size - k)               # row 0 = north
        lat, lon, _ = frame.enu_to_geodetic(E, N, 0.0)
        amounts = season.amounts_for(self.season_mode, float(np.mean(lat)))
        if not any(v > 0 for v in amounts.values()):
            return rgb
        xm, ym = season.world_metres(lat, lon)
        px = rgb.shape[0]
        up = lambda a: np.asarray(Image.fromarray(a.astype(np.float32), "F").resize((px, px), Image.BILINEAR))
        return season.grade(rgb, up(xm - xm.min()) + float(xm.min()), up(ym - ym.min()) + float(ym.min()), amounts)

    def _render(self, frame, x0, y0, size, px, pending=None, skip_lazy=False):
        """Texture of the ENU square [x0, x0+size] x [y0, y0+size] (anchor `frame`), px x px,
        row 0 = north edge, column 0 = west edge (texel centres at (i + 0.5) * size / px).
        Returns (uint8 [px, px, 3] after the tone curve, {source: texels}).

        The ENU -> lat/lon -> Web-Mercator mapping is evaluated on a MESH_CELLS² grid of cell
        corners and mapped bilinearly in between (Pillow's MESH transform, BILINEAR sampling):
        the mapping is smooth enough that this is exact to well under a texel (second-order
        error ~ R tan(lat) dphi² / 8 for a cell of dphi rad: ~4 m on a 131 km tile at 36 deg,
        0.01 of its 512 m texel; nothing measurable on near tiles). Each texel takes the first
        source of the chain whose four bilinear taps all have data. pending: a set that collects the
        colour-match regions (--colormatch) that were not ready, i.e. whose tiles stayed raw here, and
        the de-shadow regions (--deshadow) still to come, as (DS_PENDING, rx, ry).
        The counts also carry "none" (fallback colour), "deshadow" (relit texels) and, with
        --colormatch, "colormatch": the texels of the sources whose mosaic had tiles the colour match
        changed (their colours then come partly from its S2 references: the data credits name them)."""
        cells = min(MESH_CELLS, px)
        cw = px // cells
        k = np.arange(cells + 1) * (size / cells)
        E, N = np.meshgrid(x0 + k, y0 + size - k)                 # [row (north first), col]
        lat_raw, lon, _ = frame.enu_to_geodetic(E, N, 0.0)
        lat = np.clip(lat_raw, -89.9, 89.9)
        zw = zoom_for(float(lat[cells // 2, cells // 2]), size / px)
        out = np.zeros((px, px, 3), np.uint8)
        filled = np.zeros((px, px), bool)
        relit = None if self.deshadow is None else np.zeros((px, px), bool)
        used, error = {}, None
        for i, src in enumerate(self.chain):
            cm_keys = set() if self.colormatch is not None else None
            z = min(zw, src.max_zoom)
            if z < src.min_zoom or (skip_lazy and src.lazy):
                continue
            if src.bounds:                  # a regional source elsewhere: not even its mosaic's keys
                bs, bw, bn, be = src.bounds
                if lat.max() < bs or lat.min() > bn or lon.max() < bw or lon.min() > be:
                    continue
            while True:
                fx, fy = mercator_px(lat, lon, z)
                w = TILE * 2 ** z
                fx = fx - w * np.round((fx - fx[0, 0]) / w)        # unwrap across the antimeridian
                try:
                    m = self._mosaic(src, z, fx, fy, i == len(self.chain) - 1, pending, cm_keys)
                except ImageryError as e:   # this source is unreachable: the next one may do
                    if self.strict:
                        raise
                    error, m = e, False
                    break
                if m is not None or z <= src.min_zoom:
                    break
                z -= 1                      # footprint too large for the zoom: coarser
            if not m:
                continue
            mos, ox, oy, any_data = m
            if not any_data:
                continue
            qx, qy = fx - ox, fy - oy
            # Pillow's MESH / BILINEAR warp of the RGBA mosaic, byte for byte, in Rust (no GIL, no per-cell calls)
            fast.require(0, "mesh_warp")
            res = fast._ef.mesh_warp(mos, np.ascontiguousarray(qx, np.float64), np.ascontiguousarray(qy, np.float64),
                                     cells, cw, px)
            alpha = res[..., 3]
            hit = alpha == 255 if relit is None else alpha >= 254
            if not used and hit.all():      # the usual case: one source covers the texture
                out[:] = res[..., :3]
                filled[:] = True
                if relit is not None:
                    relit[:] = alpha == 254
            else:
                hit &= ~filled
                np.copyto(out, res[..., :3], where=hit[..., None])
                filled |= hit
                if relit is not None:
                    relit |= hit & (alpha == 254)
            if hit.any():
                used[src.name] = int(hit.sum())
                if cm_keys:
                    used["colormatch"] = used.get("colormatch", 0) + int(hit.sum())
            if filled.all():
                break
        if not filled.all():
            if error is not None:           # not known to be uncovered: do not bake a fallback
                raise error
            out[~filled] = FALLBACK_RGB
            if np.abs(lat_raw).max() > MAX_LAT:      # the polar caps: snow
                i, j = np.nonzero(~filled)
                t = size / px
                la, _, _ = frame.enu_to_geodetic(x0 + (j + 0.5) * t, y0 + size - (i + 0.5) * t, 0.0)
                cap = np.abs(la) > MAX_LAT
                out[i[cap], j[cap]] = POLAR_RGB
                if cap.any():
                    used["polar"] = int(cap.sum())
            used["none"] = int((~filled).sum()) - used.get("polar", 0)
            if not used["none"]:
                del used["none"]
        if relit is not None and relit.any():
            used["deshadow"] = int(relit.sum())
            res = self.lut[out]
            res[relit] = self.lut_ds[out[relit]]
            return res, used
        return np.asarray(Image.fromarray(out, "RGB").point(self.lut_table)), used

    def close(self):
        if self.deshadow is not None:
            self.deshadow.close()
        if self.colormatch is not None:
            self.colormatch.close()
        self.pool.shutdown(wait=False, cancel_futures=True)


# --- texture gutter ----------------------------------------------------------------------
def gutter(px):
    """Border (texels per side) rendered around a tile's footprint. The engine samples material
    textures with a fixed WRAP sampler (shaders/common/bng.hlsl defaultSampler2D), so without a
    border every tile edge blends in the opposite edge's colour: a thin seam line along each
    tile boundary (seen in-game, v1.3). With g real-imagery texels around the footprint, mip
    levels 0 .. log2(g) are seam-free; deeper mips are only used where a texel is far below a
    pixel. 8 texels (3 % per side at 256 px) keeps levels 0-3 clean."""
    return max(1, min(8, px // 32))


def gutter_square(x0, y0, size, px):
    """(x0', y0', size') of the rendered square: the footprint plus gutter(px) texels per side,
    the footprint covering texels g .. px - g exactly."""
    g = gutter(px)
    t = size / (px - 2 * g)
    return x0 - g * t, y0 - g * t, px * t


def gutter_uv(planar, px):
    """UV0 of footprint coordinates in [0, 1] for a texture with a gutter (see gutter())."""
    g = gutter(px)
    return (g + np.asarray(planar, np.float64) * (px - 2 * g)) / px


# --- DDS ---------------------------------------------------------------------------------
DDS_MAGIC = b"DDS "
DDSD_CAPS, DDSD_HEIGHT, DDSD_WIDTH, DDSD_PIXELFORMAT = 0x1, 0x2, 0x4, 0x1000
DDSD_MIPMAPCOUNT, DDSD_LINEARSIZE = 0x20000, 0x80000
DDPF_FOURCC = 0x4
DDSCAPS_COMPLEX, DDSCAPS_TEXTURE, DDSCAPS_MIPMAP = 0x8, 0x1000, 0x400000
DDS_HEADER = 128                    # magic + 124-byte header


def dxt1_size(w, h):
    return max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * 8


# Mip levels (LOD research 2026-10-14: the plain sRGB 2 x 2 box is the softest possible chain, and ground at a
# grazing angle is drawn from the small levels): averaged in linear light (dark detail does not swallow light
# detail) and each smaller level sharpened a little (MIP_SHARPEN, a 3 x 3 unsharp mask).
MIP_SHARPEN = 0.35
_TO_LIN = ((np.arange(256) / 255.0) ** 2.2).astype(np.float32)


def mip_arrays(rgb):
    """[level 0, 1, ...] (uint8 [h, w, 3] arrays) down to 1 x 1 of a power-of-two image: linear-light 2 x 2
    averages, each level from the previous unsharpened one, then sharpened by MIP_SHARPEN."""
    rgb = np.asarray(rgb, np.uint8)
    fast.require(0, "mip_levels")       # the Rust loop (float32 maths)
    return [rgb] + list(fast._ef.mip_levels(np.ascontiguousarray(rgb), _TO_LIN, float(MIP_SHARPEN)))


def mip_chain(img):
    """[level 0, 1, ...] PIL images down to 1 x 1 (mip_arrays)."""
    return [Image.fromarray(a, "RGB") for a in mip_arrays(np.asarray(img.convert("RGB")))]


_BC1_GPU_FAILED = False


def _bc1_gpu(levels):
    """The levels' BC1 blocks on the GPU (earthfast.bc1_encode_gpu: the same encoder in float32, ~1 block in 1000
    differs; a 512 px texture's CPU time 9.3 -> 2.0 ms), or None: no GPU, or it failed (logged once; the CPU encoder
    from then on)."""
    global _BC1_GPU_FAILED
    if _BC1_GPU_FAILED or not fast.gpu() or getattr(fast._ef, "VERSION", 0) < 59:
        return None
    try:
        return fast._ef.bc1_encode_gpu(levels)
    except Exception as e:
        _BC1_GPU_FAILED = True
        log.warning("imagery: GPU BC1 encoding failed (%s); the CPU from now on", e)
        return None


def encode_dds(rgb):
    """DDS bytes of an RGB image (uint8 [h, w, 3], power-of-two sides): DXT1 (BC1, no alpha)
    with the full mip chain."""
    rgb = np.asarray(rgb, np.uint8)
    h, w = rgb.shape[:2]
    blobs = []
    # the Rust BC1 encoder: endpoints on each block's principal axis, refined, nearest-colour indices (2026-10-16:
    # RMSE 3.39 -> 2.86 on 60 imagery tiles against Pillow's min / max 565 endpoints), without the GIL
    fast.require(0, "bc1_encode")
    levels = [np.ascontiguousarray(lv) for lv in mip_arrays(rgb)]
    blobs = _bc1_gpu(levels) or [fast._ef.bc1_encode(lv) for lv in levels]
    for lv, data in zip(levels, blobs):
        assert len(data) == dxt1_size(lv.shape[1], lv.shape[0]), (lv.shape, len(data))
    flags = DDSD_CAPS | DDSD_HEIGHT | DDSD_WIDTH | DDSD_PIXELFORMAT | DDSD_MIPMAPCOUNT | DDSD_LINEARSIZE
    head = (DDS_MAGIC
            + struct.pack("<7I", 124, flags, h, w, dxt1_size(w, h), 0, len(blobs))
            + struct.pack("<11I", *((0,) * 11))
            + struct.pack("<2I4s5I", 32, DDPF_FOURCC, b"DXT1", 0, 0, 0, 0, 0)
            + struct.pack("<5I", DDSCAPS_TEXTURE | DDSCAPS_MIPMAP | DDSCAPS_COMPLEX, 0, 0, 0, 0))
    assert len(head) == DDS_HEADER
    return head + b"".join(blobs)


def write_dds(path, rgb):
    """encode_dds() to path atomically; returns the file size."""
    data = encode_dds(rgb)
    tmp = staging.tmp_for(path)
    with open(tmp, "wb") as f:
        f.write(data)
    staging.replace(tmp, path)
    return len(data)


def read_dds_info(data):
    """(width, height, mip count, fourcc, [level byte sizes]) of a DXT1 DDS (for tests/checks)."""
    if data[:4] != DDS_MAGIC:
        raise ValueError("not a DDS file")
    size, flags, h, w, linear, depth, mips = struct.unpack_from("<7I", data, 4)
    fourcc = data[84:88]
    mips = mips if flags & DDSD_MIPMAPCOUNT else 1
    sizes = []
    for k in range(max(1, mips)):
        sizes.append(dxt1_size(max(1, w >> k), max(1, h >> k)))
    return w, h, mips, fourcc, sizes
