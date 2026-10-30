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

The finished pyramid (geotiles phase 3a, 2026-10-29): what does not depend on the anchor is done once per source
tile and kept: Imagery.finished() is the tile de-shadowed (--deshadow), colour-matched (--colormatch) and tone-mapped,
with its nodata mask, under <cache>/imagery/fin<FIN_VERSION>-<settings>/<source>/z/x/y.bnf (a JPEG, FIN_QUALITY), in
memory as that JPEG (MEM_FINISHED_MB) and the recent ones decoded (MEM_DECODED). A texture for any anchor is then a
warp of finished tiles (earthfast.tile_warp: decode, mosaic, Pillow's MESH / BILINEAR, the chain's composite, no GIL),
the season grade (earthfast.season_img: date and texel size dependent, so per texture) and its DDS: a rebase or a
teleport back renders none of the corrections again. A tile made while a correction's region was not ready (or beyond
REQUEST_M) is kept with that region noted and made again once it is ready (_fresh).

Output: DDS DXT1 with a full mip chain (write_dds): linear-light 2 x 2 levels, each sharpened, and their BC1 blocks
(earthfast.dds_levels: the GPU's encoder when there is one) under our own header (DDSD_MIPMAPCOUNT,
DDSCAPS_MIPMAP|COMPLEX).
"""
import contextlib
import hashlib
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
FETCH_WORKERS = 16      # (the photo hosts allow 24 (USGS, 2026-10-23) / 6 at once; more threads here only queue)
URGENT_WORKERS = 4      # Imagery.urgent(): its own fetch threads (netfetch gives urgent requests URGENT_CONC slots)
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
NODATA_BLOCK = 8                   # px: inside a tile, a square this size all this black is outside the data
NODATA_EDGE = 2                    # ... and this much round it (the JPEG's grey fringe on the black)
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
DEFAULT_CHAIN = ("md6in", *imagery_regional.REGIONAL, *(("bcortho",) if os.environ.get("BNE_BCORTHO", "1") != "0" else ()),
                 "naipusda", "naip", "usgs", "eox2023", "eox2016")
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


def _under(pz, px, py, z):
    """(x0, x1, y0, y1) of the zoom-z tiles under tile (pz, px, py), or its ancestor at z when z < pz."""
    if z >= pz:
        s = z - pz
        return px << s, ((px + 1) << s) - 1, py << s, ((py + 1) << s) - 1
    s = pz - z
    return px >> s, px >> s, py >> s, py >> s


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


def _box_any(a, k):
    """For each element of the bool image a: is it inside some k x k window of a that is all True? Pure."""
    h, w = a.shape
    if h < k or w < k:
        return np.zeros_like(a)
    s = np.zeros((h + 1, w + 1), np.int32)
    s[1:, 1:] = a.cumsum(0).cumsum(1)
    full = (s[k:, k:] - s[:-k, k:] - s[k:, :-k] + s[:-k, :-k]) == k * k      # window starts, (h-k+1, w-k+1)
    f = np.zeros((h, w), bool)
    f[:h - k + 1, :w - k + 1] = full
    # a texel (i, j) is covered when a full window starts within [i-k+1, i] x [j-k+1, j]: the starts grown down-right
    g = np.zeros((h + 1, w + 1), np.int32)
    g[1:, 1:] = f.cumsum(0).cumsum(1)
    i0, i1 = np.clip(np.arange(h) - k + 1, 0, h), np.arange(h) + 1
    j0, j1 = np.clip(np.arange(w) - k + 1, 0, w), np.arange(w) + 1
    return (g[np.ix_(i1, j1)] - g[np.ix_(i0, j1)] - g[np.ix_(i1, j0)] + g[np.ix_(i0, j0)]) > 0


def _dilate(m, e):
    """m grown by e texels each way (a (2e+1)-square window). Pure."""
    h, w = m.shape
    s = np.zeros((h + 1, w + 1), np.int32)
    s[1:, 1:] = m.cumsum(0).cumsum(1)
    i0, i1 = np.clip(np.arange(h) - e, 0, h), np.clip(np.arange(h) + e + 1, 0, h)
    j0, j1 = np.clip(np.arange(w) - e, 0, w), np.clip(np.arange(w) + e + 1, 0, w)
    return (s[np.ix_(i1, j1)] - s[np.ix_(i0, j1)] - s[np.ix_(i1, j0)] + s[np.ix_(i0, j0)]) > 0


def nodata_mask(rgb):
    """The texels of a source tile outside its data: in a NODATA_BLOCK square all black (NODATA_MAX), and NODATA_EDGE
    round it; None when there are none. A regional photo stops mid-tile with black past its edge (New Jersey's over
    Manhattan drew black streets and blocks: GitHub #251); a whole tile mostly black was caught (NODATA_SHARE, export
    sources), a black part of one was not. A deep shadow is rarely a whole square of pure black. Pure."""
    dark = np.maximum(np.maximum(rgb[..., 0], rgb[..., 1]), rgb[..., 2]) <= NODATA_MAX    # (max(axis=2): 20x slower)
    if dark.sum() < NODATA_BLOCK * NODATA_BLOCK:
        return None
    h, w = dark.shape
    q = NODATA_BLOCK // 2           # an all-black square of NODATA_BLOCK holds an all-black aligned one of half its size
    if h % q == 0 and w % q == 0 and not dark.reshape(h // q, q, w // q, q).all(axis=(1, 3)).any():
        return None
    m = _box_any(dark, NODATA_BLOCK)
    if not m.any():
        return None
    return _dilate(m, NODATA_EDGE) if NODATA_EDGE else m


@dataclass(frozen=True)
class Tile:
    """A decoded source tile: RGB uint8 [256, 256, 3], and whether it is one colour (blank)."""
    rgb: np.ndarray
    uniform: bool


def as_tile(rgb):
    return Tile(rgb, is_uniform(rgb))


# The finished pyramid (geotiles phase 3a, .claude/work/geotiles_plan.md): every source tile once de-shadowed,
# colour-matched and tone-mapped (Imagery.finished), kept on disk under <cache>/imagery/fin<FIN_VERSION>-<settings>/
# <source>/z/x/y.bnf, so a far tile's texture or a block's photo for any anchor is a warp of these (earthfast.tile_warp)
# and a rebase or a teleport back renders none of it again.
FIN_VERSION = 1             # bump: every finished tile is made again (its directory holds it)
FIN_MAGIC = b"BNF1"
FIN_QUALITY = 95            # its JPEG (4:4:4): ~44 KB a tile, PSNR ~50 dB to the finished pixels, 0.6 ms to decode
MEM_FINISHED_MB = 160       # finished tiles kept in memory as their JPEGs (~44 KB; a rebase's far field ~2000 of them)
MEM_DECODED = 384           # ... and the most recent ones decoded too (192 KB each: neighbouring textures share tiles)


@dataclass
class Finished:
    """A source tile as the textures sample it (Imagery.finished), de-shadowed, colour-matched and tone-mapped. data:
    its JPEG as kept on disk (bytes: earthfast.tile_warp decodes it, so a texture is the same from memory or disk),
    or its RGB (256, 256, 3) uint8 (a uniform tile, or one not kept); relit: by --deshadow (its texels count as
    such); uniform: one colour (a later source fills it); hole: its nodata texels (nodata_mask, None: none), left out
    where a later source can fill them; cm: the colour match changed it; wait: the corrections not applied because
    their region was not ready, (("ds" | "cm", (rx, ry)), ...): it is made again once one is (Imagery._fresh)."""
    data: object
    relit: bool
    uniform: bool
    hole: object = None
    cm: bool = False
    wait: tuple = ()

    def for_mosaic(self, last):
        """(data, alpha, hole) for earthfast.tile_warp: the nodata texels left out for a source that is not the chain's
        last (the next one fills them); a relit tile's alpha 254 (counted as relit)."""
        return self.data, 254 if self.relit else 255, (None if last else self.hole)

    def nbytes(self):
        return (len(self.data) if isinstance(self.data, bytes) else self.data.nbytes) + \
            (self.hole.nbytes if self.hole is not None else 0) + 200

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
        # urgent(): the player's block's photo, fetched on threads of its own as netfetch "urgent" (ahead of the far
        # field's demand: a teleport's first block waited 37 s behind it with EOX at its budget, GitHub #116)
        self.urgent_pool = ThreadPoolExecutor(URGENT_WORKERS, thread_name_prefix="img-urgent")
        self._caller = threading.local()
        self.mem_tiles = MEM_TILES if mem_tiles is None else mem_tiles
        self.leaf_on = leaf_on
        self.season_mode = season_mode
        self.lut = tone_lut(tone)
        self.deshadow = deshadow
        if deshadow is not None:
            deshadow.bind(self)
            self.lut_ds = tone_lut(tone, deshadow.tone_lift)    # texels of relit source tiles
        self.colormatch = colormatch
        if colormatch is not None:
            colormatch.bind(self)
        self.mem_finished = MEM_FINISHED_MB * 1_000_000      # (bytes)
        self._fin = OrderedDict()           # (source, z, x, y) -> Finished (finished())
        self._fin_bytes = 0
        self._fin_rgb = OrderedDict()       # (source, z, x, y) -> (its Finished, its JPEG decoded): MEM_DECODED
        self._fin_busy = {}                 # (source, z, x, y) -> Event: a thread is finishing it
        self._fin_sigs = {}                 # source name -> its finished tiles' settings hash (_fin_sig)
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
            deferred = kind not in ("demand", "urgent") and isinstance(e, _DEFERRED)
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
                deferred = kind not in ("demand", "urgent") and isinstance(e, _DEFERRED)
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

    def predownload_sources(self, pz, px, py, zw, fetch_probes=True, excluded=None):
        """A saved area's square (areas.py): per source of the chain in order, (src, [(src, z, x, y)]) of the tiles a
        render at zoom zw would ask for under tile (pz, px, py): its zoom capped at the source's, inside its bounds.
        Sources whose host takes no pre-download (the regional services) are left out (their names added to
        `excluded`), and a source whose coverage probe says it has nothing there (fetch_probes: the probe fetched as
        "predownload"; else only a probe known on disk). Lazy sources (NAIP, Maryland's 6-inch photos: only warm()
        fetches them while driving) are in: a render uses their tiles once they are on disk; one request of an export
        source writes 4 ** export tiles (raw: _download_export). The caller fetches a source's tiles and stops at the
        first that had any."""
        for src in self.chain:
            z = min(zw, src.max_zoom)
            p = self.fetcher.policy(netfetch.host_of(src.url))
            if z < src.min_zoom:
                continue
            if p is None or p.interactive_only or not p.offline_ok:
                if excluded is not None and range_in_bounds(src, z, *_under(pz, px, py, z)):
                    excluded.add(src.name)
                continue
            x0, x1, y0, y1 = _under(pz, px, py, z)
            if not range_in_bounds(src, z, x0, x1, y0, y1):
                continue
            keys = [(src, z, x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1) if in_bounds(src, z, x, y)]
            if not keys:
                continue
            probe = self._probe_key(keys[0])
            if probe is not None and _under(pz, px, py, probe[1])[0] == _under(pz, px, py, probe[1])[1]:
                if fetch_probes:
                    got = self.tiles([probe], kind="predownload").get(probe)
                    if not self._covered(got):
                        continue
                elif os.path.exists(self._paths(*probe)[1]):
                    continue
            yield src, keys

    def on_disk(self, key):
        """A source tile's file is there (no read)."""
        return self._on_disk(key)

    def on_disk_or_missing(self, key):
        """A source tile is on disk or known to be missing there (its .404 marker)."""
        jpg, miss = self._paths(*key)
        return os.path.exists(jpg) or os.path.exists(miss)

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
        fetch whose done callback has not run yet). Caller holds the lock. An urgent one does not wait for a
        fetch queued as another kind: it fetches on the urgent threads."""
        fut = self._inflight.get(key)
        if fut is None or fut is stale or (kind == "urgent" and not getattr(fut, "urgent", False)):
            pool = self.urgent_pool if kind == "urgent" else self.pool
            fut = self._inflight[key] = pool.submit(self._load_kind, key, kind)
            fut.urgent = kind == "urgent"
            fut.add_done_callback(lambda f: self._finish(key, f))
        return fut

    @contextlib.contextmanager
    def urgent(self):
        """Within: this thread's demand fetches go as netfetch "urgent" (the block the player waits on)."""
        prev = getattr(self._caller, "urgent", False)
        self._caller.urgent = True
        try:
            yield
        finally:
            self._caller.urgent = prev

    def _result(self, key, fut, kind):
        """fut's tile; a demand caller that met a dropped prefetch fetches the tile itself (one fetch
        shared by every caller that met the same dropped one)."""
        while True:                         # (a demand fetch is never dropped: this ends)
            try:
                return fut.result()
            except ImageryError as e:
                if not e.deferred or kind not in ("demand", "urgent"):
                    raise
            with self._lock:
                fut = self._future(key, stale=fut)

    def tiles(self, keys, kind="demand"):
        """{(source, z, x, y): Tile or None (404, or outside the coverage probe)}, fetched
        concurrently and deduplicated across callers. Raises ImageryError if a fetch failed.
        kind: the netfetch kind of the requests ("background": de-shadow region fits; demand within urgent(): urgent)."""
        if kind == "demand" and getattr(self._caller, "urgent", False):
            kind = "urgent"
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

    def _needed(self, frame, x0, y0, size, px):
        """The source tiles render(frame, x0, y0, size, px) may need, source by source in chain order: (k, src,
        host, [keys]); nothing past the poles, a mosaic over MAX_MOSAIC, or a source's area ending the chain (as
        render fails over)."""
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
            yield k, src, netfetch.host_of(src.url), [(src, z, tx % n, ty) for ty in range(ty0, ty1 + 1)
                                                       for tx in range(tx0, tx1 + 1)]

    def _missing(self, k, key):
        """Is key's tile known to be missing (404, or uniform where a later source can fill it)? None: not known."""
        if key not in self._lru:
            return None
        a = self._lru[key]
        return a is None or (k < len(self.chain) - 1 and a.uniform)

    def prefetch(self, frame, x0, y0, size, px, soon=False):
        """Start fetching the source tiles render(frame, x0, y0, size, px) will need, without
        waiting: the first source of the chain, or the next one where every tile of the
        previous source is already known to be missing (an unknown coverage probe is fetched
        first). Errors are left for render to meet. Skips a source whose host's circuit is open
        (render fails over past it) and stops at one whose host takes no prefetch now (policy,
        budget, bucket below half): render fetches those on demand. soon: the far field's next tiles, needed in
        moments, not a guess: a host that takes no prefetch now still gets them, as demand (with a host past 80 %
        of its daily budget the far field fetched only its 2 tiles in flight at a time: a Deck's took 231 s, GitHub
        #116)."""
        for k, src, host, keys in self._needed(frame, x0, y0, size, px):
            if not self.fetcher.available(host):
                continue                    # circuit open: render fails over to the next source, warm that
            # --colormatch: their regions' seams, in the background (not under the lock: it may read a
            # field from disk); before the prefetch check, so a host that takes no prefetch right now
            # still gets the regions render will need queued (they fetch as kind "background")
            if self.colormatch is not None:
                for key in keys:
                    self.colormatch.request(key)
            kind = "prefetch"
            if not self.fetcher.allow(host, "prefetch"):
                if not soon or not self.fetcher.allow(host, "demand"):
                    return
                kind = "demand"
            missing = True                  # every tile of this source known to be missing
            with self._lock:
                for key in keys:
                    cover = self._probe_status(key)
                    if cover == "outside":
                        continue
                    if cover == "unknown":          # a probe is being fetched first
                        missing = False
                        continue
                    m = self._missing(k, key)
                    if m is not None:
                        missing &= m
                        continue
                    missing = False
                    self._future(key, kind)
            if not missing:
                return

    def fetch_needed(self, frame, x0, y0, size, px):
        """Fetch (on demand, waiting) the source tiles render(frame, x0, y0, size, px) needs: the first source of the
        chain, the next where every tile of the previous is missing. For a far-tile worker that met a tile not on
        disk (its prefetch was dropped: a host at its budget, GitHub #116, or a fast drive): the tiles come here,
        the render goes back to the worker processes (one main-process render held the far field on a Deck).
        True when the tiles are there (or known to be missing); False on a fetch error."""
        for k, src, host, keys in self._needed(frame, x0, y0, size, px):
            if not self.fetcher.available(host):
                continue
            try:
                got = self.tiles(keys)
            except ImageryError:
                return False
            if not all(v is None or (k < len(self.chain) - 1 and v.uniform) for v in got.values()):
                return True
        return True

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

    # --- the finished pyramid (geotiles phase 3a) ------------------------------------------------
    def _fin_sig(self, src):
        """The settings a source's finished tiles depend on, as a short hash (their directory): FIN_VERSION, the
        tone curves and, for the source the de-shadow / colour match correct, their mode and version."""
        parts = [FIN_VERSION, self.lut.tobytes(), self.lut_ds.tobytes() if self.deshadow is not None else None]
        for obj in (self.deshadow, self.colormatch):
            if obj is not None and getattr(obj, "source", src.name) == src.name:
                parts.append((type(obj).__name__, getattr(obj, "mode", None), getattr(obj, "version", None),
                              sorted((getattr(obj, "meta", None) or {}).items())))
        return hashlib.sha1(repr(parts).encode()).hexdigest()[:10]

    def _fin_path(self, key):
        src, z, x, y = key
        sig = self._fin_sigs.get(src.name)
        if sig is None:
            sig = self._fin_sigs[src.name] = self._fin_sig(src)
        return os.path.join(self.cache_dir, f"fin{FIN_VERSION}-{sig}", src.name, str(z), str(x), f"{y}.bnf")

    def _raw_stamp(self, key):
        """(size, mtime ns) of a source tile's raw file, (0, 0) when there is none on disk."""
        try:
            st = os.stat(self._paths(*key)[0])
        except OSError:
            return 0, 0
        return st.st_size, st.st_mtime_ns

    def _fin_keep(self, key, f):
        """A finished tile kept: to disk (a small header: flags, the raw file it was made from, the corrections still
        to come; its RGB as a JPEG (FIN_QUALITY, 4:4:4; a uniform tile's as a PNG); its nodata mask as packed bits if it
        has one) and to memory as the disk has it (the JPEG: a texture is the same from either)."""
        path = self._fin_path(key)
        b = io.BytesIO()
        if f.uniform:                   # (one colour: a PNG of ~1 KB, exact)
            Image.fromarray(f.data, "RGB").save(b, "PNG", compress_level=1)
        else:
            Image.fromarray(f.data, "RGB").save(b, "JPEG", quality=FIN_QUALITY, subsampling=0)
        jpg = b.getvalue()
        if not f.uniform:
            f.data = jpg
        self._fin_put(key, f)
        flags = (1 if f.uniform else 0) | (2 if f.relit else 0) | (4 if f.cm else 0) | (8 if f.hole is not None else 0)
        head = FIN_MAGIC + struct.pack("<BBH2Q", flags, len(f.wait), 0, *self._raw_stamp(key))
        head += b"".join(struct.pack("<B2I", 1 if k == "ds" else 2, int(rk[0]), int(rk[1])) for k, rk in f.wait)
        data = head + struct.pack("<I", len(jpg)) + jpg + (np.packbits(f.hole).tobytes() if f.hole is not None else b"")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = staging.tmp_for(path)
            with open(tmp, "wb") as fh:
                fh.write(data)
            staging.replace(tmp, path)
            self.disk.wrote(path, len(data))
        except OSError as e:            # (a full disk: made again next time)
            log.debug("finished tile %s not kept: %s", key[1:], e)

    def _fin_read(self, key):
        """A finished tile from disk, or None (none, unreadable, or made from a raw file that has changed since)."""
        path = self._fin_path(key)
        try:
            with open(path, "rb") as fh:
                data = fh.read()
            if data[:4] != FIN_MAGIC:
                return None
            flags, nwait, _, size, mtime = struct.unpack_from("<BBH2Q", data, 4)
            stamp = self._raw_stamp(key)
            if stamp != (0, 0) and stamp != (size, mtime):
                return None                 # the raw tile was fetched again: made again from it
            o = 24
            wait = []
            for _ in range(nwait):
                k, rx, ry = struct.unpack_from("<B2I", data, o)
                wait.append(("ds" if k == 1 else "cm", (rx, ry)))
                o += 9
            (n,) = struct.unpack_from("<I", data, o)
            o += 4
            img = data[o:o + n]
            if flags & 1:               # (a uniform tile's PNG: decoded here)
                im = Image.open(io.BytesIO(img))
                img = np.asarray(im if im.mode == "RGB" else im.convert("RGB"))
                if img.shape != (TILE, TILE, 3):
                    return None
            elif len(img) != n or img[:2] != b"\xff\xd8":
                return None
            hole = None
            if flags & 8:
                hole = np.unpackbits(np.frombuffer(data, np.uint8, TILE * TILE // 8, o + n))[:TILE * TILE] \
                    .reshape(TILE, TILE).astype(bool)
        except (OSError, ValueError, struct.error):
            return None
        self.disk.used(path, len(data))
        self._count("finished:disk")
        return Finished(img, bool(flags & 2), bool(flags & 1), hole, bool(flags & 4), tuple(wait))

    def _fresh(self, key, f, pending):
        """Can a finished tile made before a correction's region was ready still be used? False once one of them
        is (then it is made again); else its regions still to come go into pending, as when it was made."""
        for kind, rk in f.wait:
            obj = self.deshadow if kind == "ds" else self.colormatch
            st = obj.poll(rk) if obj is not None else "ready"
            if st == "ready":
                return False
            if st == "wait" and pending is not None:
                pending.add((DS_PENDING,) + tuple(rk) if kind == "ds" else tuple(rk))
        return True

    def _make_finished(self, key, tile, pending):
        """(Finished, keep) of a decoded source tile: raw -> de-shadow -> colour gain -> tone curve (colormatch.py
        §6), and its nodata mask. keep: nothing it needed failed (a neighbour's fetch) and every correction not
        applied is one whose region can be polled later (Finished.wait)."""
        rgb, relit, wait, keep = tile.rgb, None, [], True
        ds = self.deshadow
        if ds is not None:
            # a region still to come is asked before the relight (see deshadow.pending_region)
            ds_wait = ds.pending_region(key, tile) if hasattr(ds, "pending_region") else None
            if hasattr(ds, "relight_final"):
                relit, state = ds.relight_final(key, tile, self)
            else:                       # (a stand-in without the states: never kept)
                relit, state = ds.relight(key, tile, self), None
            if relit is None and ds_wait is not None and pending is not None:
                pending.add((DS_PENDING,) + tuple(ds_wait))
            if isinstance(state, tuple) and state[0] == "wait" and hasattr(ds, "poll"):
                wait.append(("ds", tuple(state[1])))
            elif state != "final":
                keep = False
        if relit is not None:
            rgb = relit
        cm = False
        c = self.colormatch
        if c is not None and not tile.uniform and c.handles(key):
            out, info = c.apply(key, rgb, self)
            if out is c.PENDING:        # its region is not ready: raw this time
                if pending is not None:
                    pending.add(info)
                if hasattr(c, "poll"):
                    wait.append(("cm", tuple(info)))
                else:
                    keep = False
            else:
                if info == "far" and hasattr(c, "poll"):    # beyond REQUEST_M of the anchor: raw while it is
                    wait.append(("cm", tuple(c.region_of(*key[1:]))))
                elif info is not True:                      # a neighbour-fetch fallback
                    keep = False
                if out is not None:
                    rgb, cm = out, True
        self._count("finished:made")
        return Finished(np.ascontiguousarray((self.lut_ds if relit is not None else self.lut)[rgb]), relit is not None,
                        tile.uniform, nodata_mask(tile.rgb), cm, tuple(wait)), keep

    def _fin_put(self, key, f):
        with self._lock:
            old = self._fin.pop(key, None)
            if old is not None:
                self._fin_bytes -= old.nbytes()
            self._fin[key] = f
            self._fin_bytes += f.nbytes()
            while self._fin_bytes > self.mem_finished and len(self._fin) > 1:
                self._fin_bytes -= self._fin.popitem(last=False)[1].nbytes()

    def finished(self, keys, pending=None):
        """{(source, z, x, y): Finished, or None (no data there: 404, outside the coverage probes, a lazy tile not
        fetched)} of source tiles: from memory, else from disk (the finished pyramid), else made from the raw tile
        (fetched / decoded as tiles() does) and kept. pending: collects the correction regions not ready yet, as
        (DS_PENDING, rx, ry) / (rx, ry). Raises ImageryError (a raw tile could not be fetched)."""
        keys = list(dict.fromkeys(keys))
        out, need = {}, []
        for k in keys:
            with self._lock:
                f = self._fin.get(k)
                if f is not None:
                    self._fin.move_to_end(k)
            if f is None:
                f = self._fin_read(k)
                if f is not None and (not f.wait or self._fresh(k, f, pending)):
                    self._fin_put(k, f)
                    out[k] = f
                    continue
            elif not f.wait or self._fresh(k, f, pending):
                out[k] = f
                continue
            need.append(k)
        if not need:
            return out
        raw = self.tiles(need)
        for k in need:
            t = raw.get(k)
            if t is None:
                out[k] = None
                continue
            with self._lock:            # another texture finishing the same tile: wait for it
                ev = self._fin_busy.get(k)
                mine = ev is None
                if mine:
                    ev = self._fin_busy[k] = threading.Event()
            if not mine:
                ev.wait()
                with self._lock:
                    f = self._fin.get(k)
                if f is not None and (not f.wait or self._fresh(k, f, pending)):
                    out[k] = f
                    continue
            try:
                f, keep = self._make_finished(k, t, pending)
                if keep:
                    self._fin_keep(k, f)
                out[k] = f
            finally:
                if mine:
                    with self._lock:
                        self._fin_busy.pop(k, None)
                    ev.set()
        return out

    def _mosaic_keys(self, src, z, fx, fy):
        """(tx0, ty0, nx, ny, {(tx, ty): key}) of src's zoom-z tiles covering global pixel coordinates fx, fy, or None
        if that would take more than MAX_MOSAIC tiles."""
        n = 2 ** z
        tx0, tx1 = int(math.floor(fx.min() - 1)) // TILE, int(math.floor(fx.max() + 1)) // TILE
        ty0 = max(0, int(math.floor(fy.min() - 1)) // TILE)
        ty1 = min(n - 1, int(math.floor(fy.max() + 1)) // TILE)
        nx, ny = tx1 - tx0 + 1, ty1 - ty0 + 1
        if nx * ny > MAX_MOSAIC or ny <= 0:
            return None
        return tx0, ty0, nx, ny, {(tx, ty): (src, z, tx % n, ty) for ty in range(ty0, ty1 + 1)
                                  for tx in range(tx0, tx1 + 1)}

    def render(self, frame, x0, y0, size, px, pending=None):
        """_render(); texels from a lazy source (NAIP) then take their large-scale colour from the rest of
        the chain (match_colours): NAIP's detail, USGS's colours, so the block's photo meets the far tiles
        around it (USGS, colour-matched and de-shadowed) without a seam (in-game 2026-10-13, Annapolis: the
        NAIP block was a darker, greener square, its water much darker)."""
        return self.render_where(EnuSquare(frame, x0, y0, size), px, pending)

    def render_merc(self, z, x, y, px, pending=None):
        """render() of Web-Mercator tile (z, x, y) with a gutter(px) border: px x px, the tile itself covering texels
        g .. px - g on both axes (gutter_uv of its Mercator position), row 0 north (the far field's global tiles,
        farfield.GTile: the same texture for every anchor)."""
        return self.render_where(MercSquare(z, x, y, px), px, pending)

    def render_where(self, where, px, pending=None):
        """render() of a square (EnuSquare, MercSquare)."""
        rgb, used = self._render(where, px, pending)
        if any(s.lazy and s.name in used for s in self.chain) and not all(s.lazy for s in self.chain):
            # the reference only gives the colour above MATCH_SIGMA_M: rendered and matched at MATCH_DOWN times
            # coarser texels (64x fewer texels to render, blur and stream through memory than at full size)
            small = max(MATCH_MIN_PX, px // MATCH_DOWN)
            try:
                ref, _ = self._render(where, small, pending, skip_lazy=True)
            except ImageryError:            # the reference is unreachable: NAIP as served
                ref = None
            if ref is not None:
                rgb = match_colours(rgb, ref, MATCH_SIGMA_M / (where.size / small))
                if self.leaf_on and any(s.leaf_off and s.name in used for s in self.chain):
                    rgb = season.leaf_on(rgb, ref)
                    used["leafon"] = sum(used.get(s.name, 0) for s in self.chain if s.leaf_off)
        return self._seasoned(where, rgb), used

    SEASON_GRID = 17            # world positions for the season noise: on this grid, bilinear in between

    def _seasoned(self, where, rgb):
        """rgb graded toward the season (season.grade; season_mode "off": unchanged)."""
        if self.season_mode in (None, "off", "summer"):
            return rgb
        lat, lon = where.grid(self.SEASON_GRID - 1)             # row 0 = north
        amounts = season.amounts_for(self.season_mode, float(np.mean(lat)))
        if not any(v > 0 for v in amounts.values()):
            return rgb
        xm, ym = season.world_metres(lat, lon)
        return season.grade(rgb, xm, ym, amounts)       # (the grid, upscaled to the texels in the kernel)

    def _render(self, where, px, pending=None, skip_lazy=False):
        """Texture of a square (where: EnuSquare: [x0, x0+size] x [y0, y0+size] of an anchor's frame; MercSquare),
        px x px,
        row 0 = north edge, column 0 = west edge (texel centres at (i + 0.5) * size / px).
        Returns (uint8 [px, px, 3], tone-mapped, {source: texels}).

        A warp of the finished source tiles (finished(): de-shadowed, colour-matched, tone-mapped once per
        source tile and kept on disk, so nothing of that is done again for another anchor's tiles): the ENU ->
        lat/lon -> Web-Mercator mapping is evaluated on a MESH_CELLS² grid of cell corners and mapped bilinearly
        in between (Pillow's MESH transform, BILINEAR sampling, earthfast.tile_warp): the mapping is smooth
        enough that this is exact to well under a texel (second-order error ~ R tan(lat) dphi² / 8 for a cell of
        dphi rad: ~4 m on a 131 km tile at 36 deg, 0.01 of its 512 m texel; nothing measurable on near tiles).
        Each texel takes the first source of the chain whose four bilinear taps all have data. pending: a set that
        collects the colour-match regions (--colormatch) that were not ready, i.e. whose tiles stayed raw here, and
        the de-shadow regions (--deshadow) still to come, as (DS_PENDING, rx, ry).
        The counts also carry "none" (fallback colour), "deshadow" (relit texels) and, with
        --colormatch, "colormatch": the texels of the sources whose mosaic had tiles the colour match
        changed (their colours then come partly from its S2 references: the data credits name them)."""
        cells = min(MESH_CELLS, px)
        cw = px // cells
        lat_raw, lon = where.grid(cells)                         # [row (north first), col]
        lat = np.clip(lat_raw, -89.9, 89.9)
        zw = zoom_for(float(lat[cells // 2, cells // 2]), where.size / px)
        out = np.zeros((px, px, 3), np.uint8)
        filled = np.zeros((px, px), np.uint8)
        used, error, relit, full = {}, None, 0, False
        fast.require(108, "imagery tile_warp")
        for i, src in enumerate(self.chain):
            last = i == len(self.chain) - 1
            z = min(zw, src.max_zoom)
            if z < src.min_zoom or (skip_lazy and src.lazy):
                continue
            if src.bounds:                  # a regional source elsewhere: not even its mosaic's keys
                bs, bw, bn, be = src.bounds
                if lat.max() < bs or lat.min() > bn or lon.max() < bw or lon.min() > be:
                    continue
            m = None
            while True:
                fx, fy = mercator_px(lat, lon, z)
                w = TILE * 2 ** z
                fx = fx - w * np.round((fx - fx[0, 0]) / w)        # unwrap across the antimeridian
                m = self._mosaic_keys(src, z, fx, fy)
                if m is not None or z <= src.min_zoom:
                    break
                z -= 1                      # footprint too large for the zoom: coarser
            if m is None:
                continue
            tx0, ty0, nx, ny, keys = m
            try:
                fin = self.finished(keys.values(), pending)
            except ImageryError as e:       # this source is unreachable: the next one may do
                if self.strict:
                    raise
                error = e
                continue
            tiles, cm_any, jpeg_of = [], False, {}
            for (tx, ty), key in keys.items():
                f = fin.get(key)
                if f is None or (not last and f.uniform):
                    continue                # missing here: the next source fills it
                data, alpha, hole = f.for_mosaic(last)
                if isinstance(data, bytes):
                    with self._lock:
                        d = self._fin_rgb.get(key)
                        if d is not None and d[0] is f:
                            self._fin_rgb.move_to_end(key)
                            data = d[1]
                        else:
                            jpeg_of[len(tiles)] = (key, f)
                tiles.append((tx - tx0, ty - ty0, data, alpha, hole))
                cm_any |= f.cm
            if not tiles:
                continue
            qx = np.ascontiguousarray(fx - tx0 * TILE, np.float64)
            qy = np.ascontiguousarray(fy - ty0 * TILE, np.float64)
            hits, rh, full, decoded = fast._ef.tile_warp(tiles, nx, ny, qx, qy, cells, cw, out, filled)
            if decoded:
                with self._lock:
                    for i, rgb in decoded:
                        if i in jpeg_of:
                            key, f = jpeg_of[i]
                            self._fin_rgb[key] = (f, rgb)
                            self._fin_rgb.move_to_end(key)
                    while len(self._fin_rgb) > MEM_DECODED:
                        self._fin_rgb.popitem(last=False)
            if hits:
                used[src.name] = int(hits)
                relit += int(rh)
                if cm_any:
                    used["colormatch"] = used.get("colormatch", 0) + int(hits)
            if full:
                break
        if not full:
            if error is not None:           # not known to be uncovered: do not bake a fallback
                raise error
            hole = filled == 0
            out[hole] = self.lut[np.array(FALLBACK_RGB)]
            if np.abs(lat_raw).max() > MAX_LAT:      # the polar caps: snow
                i, j = np.nonzero(hole)
                la = where.texel_lat(i, j, px)
                cap = np.abs(la) > MAX_LAT
                out[i[cap], j[cap]] = self.lut[np.array(POLAR_RGB)]
                if cap.any():
                    used["polar"] = int(cap.sum())
            used["none"] = int(hole.sum()) - used.get("polar", 0)
            if not used["none"]:
                del used["none"]
        if relit:
            used["deshadow"] = relit
        return out, used

    def close(self):
        if self.deshadow is not None:
            self.deshadow.close()
        if self.colormatch is not None:
            self.colormatch.close()
        self.pool.shutdown(wait=False, cancel_futures=True)


class EnuSquare:
    """The square [x0, x0 + size] x [y0, y0 + size] of an anchor's frame (render's area)."""

    def __init__(self, frame, x0, y0, size):
        self.frame, self.x0, self.y0, self.size = frame, x0, y0, size

    def grid(self, n):
        """(lat, lon) of its (n + 1)^2 grid of corners, row 0 north, column 0 west."""
        k = np.arange(n + 1) * (self.size / n)
        E, N = np.meshgrid(self.x0 + k, self.y0 + self.size - k)
        lat, lon, _ = self.frame.enu_to_geodetic(E, N, 0.0)
        return lat, lon

    def texel_lat(self, i, j, px):
        t = self.size / px
        la, _, _ = self.frame.enu_to_geodetic(self.x0 + (j + 0.5) * t, self.y0 + self.size - (i + 0.5) * t, 0.0)
        return la


class MercSquare:
    """Web-Mercator tile (z, x, y) and a gutter(px) border round it (render_merc's area): its size the tile's edge
    (m, at its centre's latitude) over the texels the tile covers, times px."""

    def __init__(self, z, x, y, px):
        g = gutter(px)
        self.z, self.x, self.y = int(z), int(x), int(y)
        self.f0 = -g / (px - 2 * g)                     # the square in tile fractions: [f0, f1]
        self.f1 = 1.0 - self.f0
        lat_c = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * (y + 0.5) / 2 ** z))))
        self.size = EARTH_CIRC * math.cos(math.radians(lat_c)) / 2 ** z * (self.f1 - self.f0)

    def _ll(self, fx, fy):
        w = float(2 ** self.z)
        lon = (self.x + fx) / w * 360.0 - 180.0
        lat = np.degrees(np.arctan(np.sinh(np.pi * (1.0 - 2.0 * (self.y + fy) / w))))
        return lat, lon

    def grid(self, n):
        f = self.f0 + np.arange(n + 1) * ((self.f1 - self.f0) / n)
        FX, FY = np.meshgrid(f, f)                       # row 0 north: y fraction grows southwards
        return self._ll(FX, FY)

    def texel_lat(self, i, j, px):
        t = (self.f1 - self.f0) / px
        return self._ll(self.f0 + (j + 0.5) * t, self.f0 + (i + 0.5) * t)[0]


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


def encode_dds(rgb):
    """DDS bytes of an RGB image (uint8 [h, w, 3], power-of-two sides): DXT1 (BC1, no alpha)
    with the full mip chain (mip_arrays' levels), in one Rust call (earthfast.dds_levels: the GPU's BC1 encoder when
    there is one, ~1 block in 1000 differs from the CPU's; a 512 px texture's CPU time 9.3 -> 2.0 ms; the CPU's
    encoder: endpoints on each block's principal axis, refined, nearest-colour indices)."""
    global _BC1_GPU_FAILED
    rgb = np.ascontiguousarray(np.asarray(rgb, np.uint8))
    h, w = rgb.shape[:2]
    fast.require(108, "imagery dds_levels")
    data, n, err = fast._ef.dds_levels(rgb, _TO_LIN, float(MIP_SHARPEN), not _BC1_GPU_FAILED and bool(fast.gpu()))
    if err is not None and not _BC1_GPU_FAILED:
        _BC1_GPU_FAILED = True          # (logged once; the CPU encoder from then on)
        log.warning("imagery: GPU BC1 encoding failed (%s); the CPU from now on", err)
    assert len(data) == sum(dxt1_size(max(1, w >> k), max(1, h >> k)) for k in range(n)), (rgb.shape, len(data))
    flags = DDSD_CAPS | DDSD_HEIGHT | DDSD_WIDTH | DDSD_PIXELFORMAT | DDSD_MIPMAPCOUNT | DDSD_LINEARSIZE
    head = (DDS_MAGIC
            + struct.pack("<7I", 124, flags, h, w, dxt1_size(w, h), 0, n)
            + struct.pack("<11I", *((0,) * 11))
            + struct.pack("<2I4s5I", 32, DDPF_FOURCC, b"DXT1", 0, 0, 0, 0, 0)
            + struct.pack("<5I", DDSCAPS_TEXTURE | DDSCAPS_MIPMAP | DDSCAPS_COMPLEX, 0, 0, 0, 0))
    assert len(head) == DDS_HEADER
    return head + data


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
