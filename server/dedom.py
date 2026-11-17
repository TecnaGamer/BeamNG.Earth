"""Germany's measured bridge decks: the states' open surface models (DOM, buildings and decks included) and terrain
models (DGM, decks removed) read as height samples along each bridge, for roadz.py's lidar path (its deck_lidar /
gap / approach measuring, the stacked-deck guard in roads.BridgeProfiles._observed).

The samples are made once, offline (tools/build_de_decks.py, on the build machine: never by a player's game, GitHub
#1079), and served as shards of a z10 tile like the other German data: <PAGES>/dedecks/<x>/<y>.npz, each sample's
position, its DOM height and (every DGM_EVERY-th) its DGM height. The game reads only the shards; BNE_DE_DECKS_DIR
reads a local folder of them instead.

Overture and OSM give a bridge's order (layer) but no height, and the inference assumed 6 m of clearance; Germany's
standard is 4.70 m. Where a state serves its DOM and DGM as an open WCS (a subset of any box, no key), every road
bridge of a cell is sampled on a 1 m grid across its width (SAMPLE_M) and the samples go to the lidar measuring as
points; the DGM under the same samples brings the state's heights onto our DEM's (roadz.datum_fit). Outside a
service's state its WCS has no data there: no points, the inference stands.

    service_at(lat, lon)            the SERVICES entry whose box holds the point, or None
    sample(fetch, svc, lat, lon)    (dom, dgm) heights (NaN: none) of the points, read in boxes of at most box_m
    points(fetch, s, w, n, e)       (lat, lon, dom, dgm) of the shards' samples in a box (the game's side)
"""
import io
import math
import os
import threading
import zipfile
from collections import OrderedDict

import numpy as np

import dmath
import dnp

SAMPLE_M = 1.0              # m between a deck's samples, along and across (the DOMs' own 1 m grid)
PAD_M = 2.0                 # m the box of a request reaches past its samples
NODATA_BELOW = -1000.0      # heights below this are a service's no-data
ZOOM = 10                   # the shards' tiles (~25 km)
DGM_EVERY = 4               # every this many samples keep their DGM height (the datum needs a few thousand a cell)
NO_Z = -(1 << 31)           # a shard's height for none (cm)
LOCAL_ENV = "BNE_DE_DECKS_DIR"

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


# ---- the shards ----
_lock = threading.Lock()
_shards = OrderedDict()     # (x, y) -> decoded


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
    """Tile (x, y)'s samples ({} none there). fetch(url) -> bytes, raising netfetch.NotFound where the site has none
    (anything else it raises goes to the caller: the cell is then built again later)."""
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
        import netfetch
        import railtags
        try:
            body = fetch(f"{railtags.PAGES}/dedecks/{x}/{y}.npz")
        except netfetch.NotFound:
            body = b""
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
