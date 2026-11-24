"""Germany's measured bridge decks: the states' open surface models (DOM, buildings and decks included) and terrain
models (DGM, decks removed) read as height samples along each bridge, for roadz.py's lidar path (its deck_lidar /
gap / approach measuring, the stacked-deck guard in roads.BridgeProfiles._observed).

The samples are made once, offline (tools/build_de_decks.py, on the build machine: never by a player's game, GitHub
#1079), and served as shards of a z10 tile like the other German data: dedecks/<x>/<y>.npz, each sample's position,
its DOM height and (every DGM_EVERY-th) its DGM height. The game reads only the shards, through the dataset's manifest
<PAGES>/dedecks/manifest.json (pagesdata.Manifest: pinned on disk, fetched once): the shards come from the host its
"data" names (the data repo's workflow puts them on the tile host only: ~645 MB would push the Pages site past its
1 GB limit), else beside the manifest. No manifest published (404): no samples, the inference stands.
BNE_DE_DECKS_DIR reads a local folder of shards instead.

Overture and OSM give a bridge's order (layer) but no height, and the inference assumed 6 m of clearance; Germany's
standard is 4.70 m over motorways and 4.50 m on other roads.
Where a state serves its DOM and DGM as an open WCS (a subset of any box, no key), every road
bridge of a cell is sampled on a 1 m grid across its width (SAMPLE_M) and the samples go to the lidar measuring as
points; the DGM under the same samples brings the state's heights onto our DEM's (roadz.datum_fit). Outside a
service's state its WCS has no data there: no points, the inference stands.

    service_at(lat, lon)            the SERVICES entry whose box holds the point, or None
    identity()                      the build the measurements are made with (roadz's folder and plan tag)
    sample(fetch, svc, lat, lon)    (dom, dgm) heights (NaN: none) of the points, read in boxes of at most box_m
    points(fetch, s, w, n, e)       (lat, lon, dom, dgm) of the shards' samples in a box (the game's side)
    plausible(xy, half, s, z, ...)  whether a measured deck reads a road surface (not tree crowns over a track)
"""
import hashlib
import io
import math
import re
import os
import threading
import types
import zipfile
from collections import OrderedDict

import numpy as np

import dmath
import dnp
import pagesdata

SAMPLE_M = 1.0              # m between a deck's samples, along and across (the DOMs' own 1 m grid)
PAD_M = 2.0                 # m the box of a request reaches past its samples
NODATA_BELOW = -1000.0      # heights below this are a service's no-data
ZOOM = 10                   # the shards' tiles (~25 km)
DGM_EVERY = 4               # every this many samples keep their DGM height (the datum needs a few thousand a cell)
NO_Z = -(1 << 31)           # a shard's height for none (cm)
LOCAL_ENV = "BNE_DE_DECKS_DIR"
PIN_RECHECK_S = 1.0         # s between looks for the run's decision another process made (identity, as railtags')
NAME = "dedecks"            # the dataset's folder on the data site (its manifest.json; the shards beside or on "data")

# A deck is measured from the samples on its mapped carriageway only (its line +- its half width, not past its ends:
# DECK_PAD_M beyond), and a deck that stands high over the ground at both free ends is kept only where its samples
# read a road surface (plausible). The surface model also holds tree crowns, masts and trusses: a crown over a forest
# track's little bridge reads as a deck 14-28 m over the ground at both ends, its samples scattered round the profile.
# The profile itself cannot jump (roadz's kernel allows LIDAR_MAX_GRADE between stations), so the scatter is measured:
# the share of the samples (at the stations the profile measured) within SUPPORT_DZ_M of it. On the 14 states' spots
# (1250 deck reads, every sample and every 4th): the real decks standing ABOVE_M or more over the ground at both free
# ends (viaducts, the Elbbruecken's truss spans) have 0.73-1.0 of their samples on the profile, the crowns and masts
# 0.11-0.60. Neither alone is a rule: dozens of real decks stand high at both ends, and occluded decks (under another
# deck) have few samples on their profile anywhere.
DECK_PAD_M = 0.0            # m beyond the carriageway's half width a deck's samples count (the US lidar's: 2 m)
ABOVE_M = 5.0               # m over the ground beyond both free ends from which a deck's samples must read a road ...
SUPPORT_MIN = 0.65          # ... this share of them within SUPPORT_DZ_M of its profile ...
SUPPORT_MIN_N = 10          # ... of at least this many samples
SUPPORT_DZ_M = 0.5
BEYOND_M = (1.0, 12.0)      # m past a deck's end its ground is read (the DGM samples of its approach, else our DEM)
TOUCH_M = 3.0               # an end this near another deck's line goes on as that deck (not onto the ground)

# The states whose surface model is open: a box round each (south, west, north, east; the game reads the shards there,
# a point in the box but outside the state finds none), its UTM zone (EPSG 25832 / 25833 = zone 32 / 33), the credit
# line. A state that serves its DOM and DGM as an open WCS 2.0 has them here: the coverages, the largest subset a
# request may ask (m), the subset's axis names, and whether the answer is multipart (a GML part beside the GeoTIFF).
# The others publish tiles (tools/de_dom_tiles.py reads those on the build machine). Saarland publishes no open DOM.
_BB_WCS = {"dom": ("https://isk.geobasis-bb.de/ows/bdom_wcs", "bb_bdom"),
           "dgm": ("https://isk.geobasis-bb.de/ows/dgm_wcs", "bb_dgm")}
SERVICES = [
    {"name": "nw", "box": (50.32, 5.86, 52.53, 9.46), "zone": 32, "max_m": 2000.0,
     "dom": ("https://www.wcs.nrw.de/geobasis/wcs_nw_dom", "nw_dom"),
     "dgm": ("https://www.wcs.nrw.de/geobasis/wcs_nw_dgm", "nw_dgm"),
     "credit": "Geobasis NRW, DOM1 / DGM1 (dl-de/zero-2-0)"},
    {"name": "be", "box": (52.33, 13.08, 52.68, 13.77), "zone": 33, "max_m": 1000.0, **_BB_WCS,
     "credit": "GeoBasis-DE/LGB, bDOM / DGM (dl-de/by-2-0); Geoportal Berlin (dl-de/by-2-0)"},
    {"name": "bb", "box": (51.36, 11.26, 53.56, 14.77), "zone": 33, "max_m": 1000.0, **_BB_WCS,
     "credit": "GeoBasis-DE/LGB, bDOM / DGM (dl-de/by-2-0)"},
    {"name": "mv", "box": (53.10, 10.59, 54.69, 14.42), "zone": 33, "max_m": 1000.0,
     "dom": ("https://www.geodaten-mv.de/dienste/dom_wcs", "mv_dom1"),
     "dgm": ("https://www.geodaten-mv.de/dienste/dgm_wcs", "mv_dgm"),
     "credit": "GeoBasis-DE/M-V 2026, DOM1 / DGM1"},
    {"name": "he", "box": (49.39, 7.77, 51.66, 10.24), "zone": 32, "max_m": 1000.0, "axes": ("E", "N"),
     "dom": ("https://inspire-hessen.de/raster/dom1/ows", "dom1"),
     "dgm": ("https://inspire-hessen.de/raster/dgm1/ows", "he_dgm1"),
     "credit": "HVBG, DOM1 / DGM1 (dl-de/zero-2-0)"},
    {"name": "st", "box": (50.94, 10.56, 53.04, 13.19), "zone": 32, "max_m": 1000.0, "multipart": True,
     "dom": ("https://www.geodatenportal.sachsen-anhalt.de/ows_WCS_ST_DOM1", "Coverage1"),
     "dgm": ("https://www.geodatenportal.sachsen-anhalt.de/ows_WCS_ST_DGM1", "Coverage1"),
     "credit": "GeoBasis-DE / LVermGeo ST, DOM1 / DGM1 (dl-de/by-2-0)"},
    {"name": "by", "box": (47.27, 8.97, 50.57, 13.84), "zone": 32,
     "credit": "Bayerische Vermessungsverwaltung, www.geodaten.bayern.de, DOM20 / DGM1 (CC BY 4.0)"},
    {"name": "bw", "box": (47.53, 7.51, 49.79, 10.50), "zone": 32,
     "credit": "LGL-BW 2026, DOM1 / DGM1, www.lgl-bw.de (dl-de/by-2-0)"},
    {"name": "ni", "box": (51.29, 6.65, 53.89, 11.60), "zone": 32,
     "credit": "LGLN, DOM1 / DGM1 (dl-de/by-2-0)"},
    {"name": "rp", "box": (48.97, 6.11, 50.94, 8.51), "zone": 32,
     "credit": "GeoBasis-DE/LVermGeoRP 2026, DOM1 / DGM1 (dl-de/by-2-0)"},
    {"name": "sn", "box": (50.17, 11.87, 51.69, 15.04), "zone": 33,
     "credit": "GeoSN, DOM1 / DGM1 (dl-de/by-2-0)"},
    {"name": "th", "box": (50.20, 9.87, 51.65, 12.66), "zone": 32,
     "credit": "GDI-Th, DOM1 / DGM1 (dl-de/by-2-0)"},
    {"name": "hh", "box": (53.39, 8.10, 53.97, 10.33), "zone": 32,
     "credit": "Freie und Hansestadt Hamburg, LGV, bDOM / DGM1 (dl-de/by-2-0)"},
    {"name": "hb", "box": (53.01, 8.48, 53.61, 8.99), "zone": 32,
     "credit": "Landesamt GeoInformation Bremen, DOM1 / DGM1 (CC BY 4.0)"},
    {"name": "sh", "box": (53.36, 7.86, 55.06, 11.32), "zone": 32,
     "credit": "GeoBasis-DE/LVermGeo SH, bDOM20 / DGM1 (CC BY 4.0)"},
]


def service_at(lat, lon):
    for s in SERVICES:
        b = s["box"]
        if b[0] <= lat <= b[2] and b[1] <= lon <= b[3]:
            return s
    return None


def url(base, cov, x0, y0, x1, y1, axes=("x", "y")):
    return (f"{base}?SERVICE=WCS&VERSION=2.0.1&REQUEST=GetCoverage&COVERAGEID={cov}&FORMAT=image/tiff"
            f"&SUBSET={axes[0]}({x0:.0f},{x1:.0f})&SUBSET={axes[1]}({y0:.0f},{y1:.0f})")


def tiff_part(body):
    """The GeoTIFF of a WCS answer: the body itself, or the TIFF part of a multipart one (Sachsen-Anhalt answers a GML
    part and the TIFF part, multipart/related)."""
    if body[:4] in (b"II*\x00", b"MM\x00*"):
        return body
    i = body.find(b"II*\x00")
    if i < 0:
        i = body.find(b"MM\x00*")
    if i < 0:
        raise ValueError("no GeoTIFF in the answer")
    end = body.find(b"\r\n--", i)
    return body[i:end] if end > 0 else body[i:]


def read_tiff(body):
    """(grid (rows, cols) float64, x of its left edge, y of its top edge, pixel width, pixel height) of a GeoTIFF's
    bytes: its ModelTiepoint and ModelPixelScale (a north-up grid, as the WCS answers); no-data as NaN."""
    body = tiff_part(body)
    try:                        # (the build machine reads every state's compression; Pillow only some)
        import tifffile
        with tifffile.TiffFile(io.BytesIO(body)) as tf:
            page = tf.pages[0]
            a = np.asarray(page.asarray(), np.float64)
            tags = {t.code: t.value for t in page.tags.values()}
    except ImportError:
        from PIL import Image
        im = Image.open(io.BytesIO(body))
        a = np.asarray(im, np.float64)
        tags = im.tag_v2
    if a.ndim == 3:
        a = a[..., 0]
    scale, tie = tags.get(33550), tags.get(33922)
    if scale is None or tie is None:
        raise ValueError("GeoTIFF without tie point / pixel scale")
    nd = tags.get(42113)
    if nd is not None:
        try:
            a = np.where(a == float(str(nd).strip("\x00 ")), np.nan, a)
        except ValueError:
            pass
    a = np.where(a < NODATA_BELOW, np.nan, a)
    # (tie: i, j, k, x, y, z: raster point (i, j) at (x, y))
    x0 = float(tie[3]) - float(tie[0]) * float(scale[0])
    y0 = float(tie[4]) + float(tie[1]) * float(scale[1])
    return a, x0, y0, float(scale[0]), float(scale[1])


def at(grid, x, y):
    """The grid's heights at x, y (arrays; NaN outside)."""
    a, x0, y0, dx, dy = grid
    c = np.floor((np.asarray(x) - x0) / dx).astype(np.int64)
    r = np.floor((y0 - np.asarray(y)) / dy).astype(np.int64)
    ok = (r >= 0) & (r < a.shape[0]) & (c >= 0) & (c < a.shape[1])
    out = np.full(len(c), np.nan)
    out[ok] = a[r[ok], c[ok]]
    return out


def boxes(x, y, max_m):
    """[(x0, y0, x1, y1, index array)]: the points grouped by the max_m grid they fall in, each box their extent
    plus PAD_M (so no request is larger than the service allows)."""
    gx = np.floor(x / max_m).astype(np.int64)
    gy = np.floor(y / max_m).astype(np.int64)
    out = []
    for key in sorted(set(zip(gx.tolist(), gy.tolist()))):
        idx = np.flatnonzero((gx == key[0]) & (gy == key[1]))
        x0 = max(float(x[idx].min()) - PAD_M, key[0] * max_m)
        x1 = min(float(x[idx].max()) + PAD_M, (key[0] + 1) * max_m)
        y0 = max(float(y[idx].min()) - PAD_M, key[1] * max_m)
        y1 = min(float(y[idx].max()) + PAD_M, (key[1] + 1) * max_m)
        out.append((math.floor(x0), math.floor(y0), math.ceil(x1), math.ceil(y1), idx))
    return out


def sample(fetch, svc, lat, lon, box_m=None):
    """(dom, dgm): the service's heights at the points (lat, lon arrays), NaN where it has none, read in boxes of at
    most box_m (the service's limit by default; the builder asks small boxes round each bridge, not whole km).
    fetch(url) -> bytes (raises when the service cannot be reached: the caller's build is then not kept)."""
    import bcproj
    x, y = bcproj.utm(lat, lon, svc["zone"])
    x, y = np.asarray(x, np.float64), np.asarray(y, np.float64)
    dom, dgm = np.full(len(x), np.nan), np.full(len(x), np.nan)
    axes = svc.get("axes", ("x", "y"))
    for x0, y0, x1, y1, idx in boxes(x, y, min(box_m or svc["max_m"], svc["max_m"])):
        for out, (base, cov) in ((dom, svc["dom"]), (dgm, svc["dgm"])):
            out[idx] = at(read_tiff(fetch(url(base, cov, x0, y0, x1, y1, axes))), x[idx], y[idx])
    return dom, dgm


def grid_along(xy, half, step=SAMPLE_M):
    """(k, 2) samples on a step grid along polyline xy (local metres) and across it, half m to either side."""
    import roadz
    pts, _ = roadz.resample_line(xy, step)
    if len(pts) < 2:
        return pts
    d = np.gradient(pts, axis=0)
    n = np.stack([-d[:, 1], d[:, 0]], 1) / np.maximum(dnp.hypot(d[:, 0], d[:, 1]), 1e-9)[:, None]
    off = np.arange(-half, half + 1e-9, step)
    return (pts[:, None, :] + off[None, :, None] * n[:, None, :]).reshape(-1, 2)


def on_line(xy, half, p):
    """(on, arc): which points p (n, 2) lie within half of polyline xy and not past its ends (as roadz's deck_lidar
    counts them), and their arc along xy (the nearest segment's; the first of equals)."""
    p = np.asarray(p, np.float64).reshape(-1, 2)
    arc = np.r_[0.0, np.cumsum(dnp.hypot(np.diff(xy[:, 0]), np.diff(xy[:, 1])))]
    best = np.full(len(p), np.inf)
    at = np.zeros(len(p))
    seg = np.full(len(p), -1, np.int64)
    tr = np.zeros(len(p))
    for i in range(len(xy) - 1):
        ax, ay = float(xy[i, 0]), float(xy[i, 1])
        bx, by = float(xy[i + 1, 0]) - ax, float(xy[i + 1, 1]) - ay
        t = ((p[:, 0] - ax) * bx + (p[:, 1] - ay) * by) / max(bx * bx + by * by, 1e-12)
        tc = np.clip(t, 0.0, 1.0)
        d = dnp.hypot(p[:, 0] - (ax + tc * bx), p[:, 1] - (ay + tc * by))
        k = d < best
        best[k], at[k], seg[k], tr[k] = d[k], arc[i] + tc[k] * (arc[i + 1] - arc[i]), i, t[k]
    past = ((seg == 0) & (tr < 0.0)) | ((seg == len(xy) - 2) & (tr > 1.0))
    return (best <= half) & ~past, at


def support(xy, half, s, z, pts):
    """(share, n): of the n samples pts (n, 3) on the carriageway (on_line) of a deck measured as heights z at the
    evenly spaced stations s along xy, at the stations it measured (z finite: not where another deck hides it), the
    share within SUPPORT_DZ_M of the profile ((NaN, 0): none)."""
    s, z = np.asarray(s, np.float64), np.asarray(z, np.float64)
    ok = np.isfinite(z)
    if not ok.any() or len(s) < 2 or not len(pts):
        return float("nan"), 0
    lo, hi = xy.min(0) - half, xy.max(0) + half
    near = np.flatnonzero((pts[:, 0] >= lo[0]) & (pts[:, 0] <= hi[0]) & (pts[:, 1] >= lo[1]) & (pts[:, 1] <= hi[1]))
    on, at = on_line(xy, half, pts[near, :2])
    at = at[on]
    st = np.minimum(np.round(at / (s[1] - s[0])).astype(np.int64), len(s) - 1)     # (the kernel's station of each)
    k = ok[st]
    if not k.any():
        return float("nan"), 0
    prof = dnp.interp(at[k], s[ok], z[ok])
    dz = np.abs(pts[near[on][k], 2] - prof)
    return float(np.count_nonzero(dz <= SUPPORT_DZ_M)) / len(dz), int(len(dz))


def ground_beyond(xy, half, ground, end):
    """The median ground height (ground (k, 3) points: the state's DGM on our datum) from BEYOND_M[0] to BEYOND_M[1]
    past end 0 (the first point) or 1 (the last) of line xy, within half of its direction; NaN: no sample there."""
    if not len(ground):
        return float("nan")
    q, q2 = (xy[0], xy[1]) if end == 0 else (xy[-1], xy[-2])
    ux, uy = float(q[0] - q2[0]), float(q[1] - q2[1])
    n = dmath.hypot(ux, uy)
    if n <= 0.0:
        return float("nan")
    ux, uy = ux / n, uy / n
    dx, dy = ground[:, 0] - float(q[0]), ground[:, 1] - float(q[1])
    along, across = dx * ux + dy * uy, np.abs(dy * ux - dx * uy)
    k = (along >= BEYOND_M[0]) & (along <= BEYOND_M[1]) & (across <= half)
    return float(np.median(ground[k, 2])) if k.any() else float("nan")


def line_dist(xy, q):
    """The distance of point q (2,) from polyline xy (k, 2), its ends included."""
    best = float("inf")
    for i in range(len(xy) - 1):
        ax, ay = float(xy[i, 0]), float(xy[i, 1])
        bx, by = float(xy[i + 1, 0]) - ax, float(xy[i + 1, 1]) - ay
        t = min(1.0, max(0.0, ((float(q[0]) - ax) * bx + (float(q[1]) - ay) * by) / max(bx * bx + by * by, 1e-12)))
        best = min(best, dmath.hypot(float(q[0]) - (ax + t * bx), float(q[1]) - (ay + t * by)))
    return best


def plausible(xy, half, s, z, pts, ground, dem, others=()):
    """(ok, why): whether a deck measured as heights z at stations s along its line xy (local metres) reads a road
    surface. pts (n, 3): the samples on our datum; ground (k, 3): the DGM samples on our datum; dem(x, y): our DEM
    there (the ground at an end the DGM has no sample beyond, NaN: none); others: the other decks' lines (an end
    within TOUCH_M of one goes on as that deck: a viaduct's ends stand high, on other decks). Not a road surface (the
    inference stands for it): both ends free and ABOVE_M or more over the ground beyond them, and under SUPPORT_MIN of
    its samples on the profile, or fewer than SUPPORT_MIN_N samples to tell (a crown of trees over a track)."""
    ok = np.flatnonzero(np.isfinite(z))
    if not len(ok):
        return True, ""
    over = []
    for end, zi in ((0, ok[0]), (1, ok[-1])):
        q = xy[0] if end == 0 else xy[-1]
        if any(len(o) >= 2 and line_dist(o, q) <= TOUCH_M for o in others):
            return True, ""
        g = ground_beyond(xy, half, ground, end)
        if not np.isfinite(g):
            g = float(dem(float(q[0]), float(q[1])))
        if not (np.isfinite(g) and float(z[zi]) - g >= ABOVE_M):
            return True, ""
        over.append(float(z[zi]) - g)
    share, n = support(xy, half, s, z, pts)
    if n >= SUPPORT_MIN_N and share >= SUPPORT_MIN:
        return True, ""
    return False, (f"both ends {min(over):.1f} m or more over the ground, {share:.2f} of {n} samples on the profile "
                   "(tree crowns?)")


# ---- the shards ----
_lock = threading.Lock()
_shards = OrderedDict()     # (x, y) -> decoded
_state = {"cache": None, "fetcher": None, "pages": None}


def configure(cache_dir, fetcher):
    """The server's cache folder and fetcher (roadz.RoadZ: MapData's) for the dataset's manifest, pinned in
    <cache_dir>/pagesdata/dedecks. The first fetcher given is kept; without one the manifest is read through the
    fetch a caller of points() passes."""
    with _lock:
        if _state["fetcher"] is None and fetcher is not None:
            _state["cache"], _state["fetcher"], _state["pages"] = cache_dir, fetcher, None
            _shards.clear()


class _Via:
    """pagesdata's fetcher (.get(url, kind).body) over a caller's fetch(url) -> bytes (roadz's lane and stats)."""

    def __init__(self, fetch):
        self._fetch = fetch

    def get(self, url, kind="demand", **_kw):
        return types.SimpleNamespace(body=self._fetch(url))


def _changed():
    """Another build is in use (called under the Manifest's lock): the shards decoded before are not it."""
    with _lock:
        _shards.clear()


def manifest(fetch=None):
    """The dataset's pagesdata.Manifest, made on the first ask. Once one is in use this run keeps it (recheck=False:
    every cell measured in a run reads the same build)."""
    with _lock:
        got = _state["pages"]
        if got is None:
            fx = _state["fetcher"] or (_Via(fetch) if fetch is not None else None)
            got = _state["pages"] = pagesdata.Manifest(_state["cache"], fx, NAME, retry_s=pagesdata.RETRY_S,
                                                       recheck=False, on_change=_changed)
        return got


def identity(network=True):
    """The dedecks build this run's measurements are made with, as a name part: "b-<built>" (the manifest's build),
    "none" (none for the run: not published, or not reachable, when the inference stands), "dir-<hash>" (BNE_DE_DECKS_DIR,
    its path's), or None (network false and the run's manifest not decided yet). roadz names its German answers'
    folder and tags the German cells' plans and decision slices with it, so answers and plans measured with one build
    are never read for another (nor an answer saved while nothing was published, once it is)."""
    local = os.environ.get(LOCAL_ENV)
    if local:
        return "dir-" + hashlib.blake2s(os.path.abspath(local).encode(), digest_size=6).hexdigest()
    pages = manifest()
    if network:
        pages.current()
    elif pages.known(PIN_RECHECK_S) is None and not pages.decided:
        return None                 # (looked at again: another process may decide the run's meanwhile)
    built = pages.built
    if built is None:
        return "none"
    return "b-" + re.sub(r"[^A-Za-z0-9._-]", "_", str(built))[:80]


def _remote(fetch, x, y):
    """Tile (x, y)'s shard bytes (b"": none there) of the build the manifest names, from its "data" host else beside
    it (pagesdata.fetch_data); raises when the manifest or the shard cannot be had (not a 404)."""
    import netfetch
    pages = manifest(fetch)
    if pages.current() is None:
        if pages.missing or pages.decided:
            # (not published, or none for this run, in the process that fetched or one that took the run's decision:
            # no samples, the inference stands; the build is "none" in every key, identity)
            return b""
        raise netfetch.FetchFailed(f"{NAME}: no manifest reachable")
    try:
        return pagesdata.fetch_data(_Via(fetch), pages.data, pages.base, f"{x}/{y}.npz")
    except netfetch.NotFound:
        return b""


def tile_of(lat, lon, zoom=ZOOM):
    n = 1 << zoom
    r = math.radians(lat)
    return int((lon + 180.0) / 360.0 * n), int((1.0 - dmath.asinh(dmath.tan(r)) / math.pi) / 2.0 * n)


def tiles_over(south, west, north, east):
    x0, y0 = tile_of(north, west)
    x1, y1 = tile_of(south, east)
    return [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


def encode(lat, lon, dom, dgm):
    """A shard's bytes: the samples sorted, positions to 1e-6 degree (~0.1 m), heights in cm (dgm NaN: none)."""
    la = np.round(np.asarray(lat) * 1e6).astype(np.int64)
    lo = np.round(np.asarray(lon) * 1e6).astype(np.int64)
    order = np.lexsort((lo, la))
    zd = np.round(np.asarray(dom, np.float64)[order] * 100.0).astype(np.int64)
    zg = np.asarray(dgm, np.float64)[order]
    zg = np.where(np.isfinite(zg), np.round(np.nan_to_num(zg) * 100.0), NO_Z).astype(np.int64)
    buf = io.BytesIO()
    np.savez_compressed(buf, dlat=np.diff(la[order], prepend=0).astype(np.int32),
                        dlon=np.diff(lo[order], prepend=0).astype(np.int32), dom=zd.astype(np.int32),
                        dgm=zg.astype(np.int32))
    return buf.getvalue()


def decode(body):
    """{lat, lon, dom, dgm} arrays (heights m, NaN none) of a shard's bytes; {} none or unreadable."""
    if not body:
        return {}
    try:
        with np.load(io.BytesIO(body), allow_pickle=False) as z:
            dom, dgm = z["dom"].astype(np.float64), z["dgm"].astype(np.float64)
            return {"lat": np.cumsum(z["dlat"].astype(np.int64)) / 1e6,
                    "lon": np.cumsum(z["dlon"].astype(np.int64)) / 1e6,
                    "dom": np.where(dom == NO_Z, np.nan, dom / 100.0),
                    "dgm": np.where(dgm == NO_Z, np.nan, dgm / 100.0)}
    except (OSError, ValueError, KeyError, EOFError, zipfile.BadZipFile):
        return {}


def shard(fetch, x, y):
    """Tile (x, y)'s samples ({} none there). fetch(url) -> bytes, raising netfetch.NotFound where the host has none
    (anything else it raises goes to the caller, and a manifest that cannot be had raises: the cell is then built
    again later). The shard's file is the manifest's build's (_remote)."""
    with _lock:
        got = _shards.get((x, y))
    if got is not None:
        return got
    local = os.environ.get(LOCAL_ENV)
    if local:
        p = os.path.join(local, str(x), f"{y}.npz")
        body = b""
        if os.path.exists(p):
            with open(p, "rb") as fh:
                body = fh.read()
    else:
        body = _remote(fetch, x, y)
    got = decode(body)
    with _lock:
        _shards[(x, y)] = got
        while len(_shards) > 16:
            _shards.popitem(last=False)
    return got


def points(fetch, south, west, north, east):
    """(lat, lon, dom, dgm) arrays of the shards' samples in the box."""
    out = [[], [], [], []]
    for x, y in tiles_over(south, west, north, east):
        d = shard(fetch, x, y)
        if not d:
            continue
        k = (d["lat"] >= south) & (d["lat"] <= north) & (d["lon"] >= west) & (d["lon"] <= east)
        for o, name in zip(out, ("lat", "lon", "dom", "dgm")):
            o.append(d[name][k])
    if not out[0]:
        return tuple(np.zeros(0) for _ in range(4))
    return tuple(np.concatenate(o) for o in out)
