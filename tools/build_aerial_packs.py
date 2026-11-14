#!/usr/bin/env python3
"""1 m aerial photo packs for the project's own tile host (imagery source "bngaer", server/aerialpacks.py): AVIF 4:4:4
tiles in BNGP packs (server/tilepack.py), only from photos whose licence lets us hand out derived tiles (the user,
2026-11-13: NAIP / USGS and the open European agencies; never EOX 2018+, Esri, Google, Bing or Mapbox).

Layout under the base (tiles.tgforever.net/aerial/v1/, R2 bnge-tiles):

    g{gen}/{z}/{px}/{py}.bngp   zooms 17 (~0.93 m at 39 N) .. 14, packs of 8 x 8 tiles (k = 3): a zoom-17 pack is one
                                zoom-14 tile (~1.9 km at 39 N, ~0.3-0.7 MB); gen: the cell's build generation (packs
                                are immutable; a rebuild of a cell is a new generation, the old one pruned later)
    cov/{x}/{y}.json            per zoom-6 region: which zoom-11 cells (8 x 8 zoom-17 packs, ~15 km at 39 N) hold
                                which zoom-17 packs, and at which generation (COVERAGE below). Not cached by the edge
                                (*.json): an on-demand build shows up for every player at their next look.

A zoom-16 / 15 / 14 pack exists where any zoom-17 pack under it does (built from them by halving: the far rings of
the same photos, instead of USGS / EOX), so a cell's packs are its 64 + 16 + 4 + 1.

COVERAGE (cov/{x}/{y}.json):
    {"format": 1, "region": [x, y], "updated": "2026-11-13T00:00:00Z",
     "cells": {"587,782": {"g": 1, "p": "ffffffffffffffff", "src": "naip", "year": 2023}, ...}}
  "p": the cell's zoom-17 packs as a 64-bit mask in hex (bit (py % 8) * 8 + (px % 8) of the pack's zoom-14 tile,
  least significant first); "g": 0 with no "p": no open photos there (an on-demand build found none: never asked
  again). Cells absent: not built (yet): the server may ask for an on-demand build (aerialpacks.request).

Sources (--source):
  naip-pc    USDA NAIP from Microsoft's Planetary Computer (STAC collection "naip": 4-band COGs, UTM, 0.3 / 0.6 m,
             overviews /2 /4 ...; free, a SAS token per collection from its token endpoint). Public domain ("USDA
             Farm Production and Conservation Business Center"). The newest flight per quarter quad, newest on top.
             The overview read is the coarsest whose pixel is <= --res-ratio x the zoom-17 texel (default 1.0: a
             0.6 m state reads the full image at 39 N; 1.3 reads its 1.2 m overview: a quarter of the bytes).

    build_aerial_packs.py OUT --cells "587,782 588,782"            # whole cells (on demand: the mini PC)
    build_aerial_packs.py OUT --packs packs.npz                    # zoom-14 tiles (x, y arrays): the prebaked set
    build_aerial_packs.py OUT --point 39.283 -76.593 --radius 3000 # the packs within 3 km (tests)

Writes OUT/g{gen}/{z}/{px}/{py}.bngp and OUT/cov/{x}/{y}.json (merged with an existing one there: --cov-in DIR reads
the current ones, e.g. synced from the bucket), and a JSON line per cell on stdout (packs, MB, seconds). Needs numpy,
Pillow >= 11.3 (AVIF, its aom encoder) and rasterio (GDAL: COG reads and the reprojection).
"""
import argparse
import io
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ProcessPoolExecutor

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
import tilepack  # noqa: E402

K = 3                       # packs of 8 x 8 tiles
TILE = 256
Z_TOP = 17                  # the photo zoom
Z_LOW = 14                  # ... down to this one (a cell: zoom 11 = Z_LOW - K)
CELL_Z = Z_LOW - K          # 11
REGION_Z = 6                # coverage files per zoom-6 region (32 x 32 cells)
PACK_PX = TILE << K         # 2048
R = 6378137.0
ORIGIN = math.pi * R
QUALITY = 60                # AVIF 4:4:4 q60: 122 KB / km2 average, SSIM 0.957 after the game's BC1 (expo B)
SPEED = 8                   # aom speed 8: ~3x faster than 6 for +-1 % bytes (Baltimore, 2026-11-13); 9: 10x, +5 %
UA = "BeamNG.Earth tile builder"
STAC = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
SAS = "https://planetarycomputer.microsoft.com/api/sas/v1/token/{collection}"
TOKEN_TTL_S = 1800.0        # a SAS token is fetched again after this (they last ~1 h)
GDAL_ENV = dict(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.TIF",
                GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES", GDAL_HTTP_MULTIPLEX="YES", GDAL_HTTP_VERSION="2",
                GDAL_HTTP_MAX_RETRY="5", GDAL_HTTP_RETRY_DELAY="2", VSI_CACHE="TRUE", VSI_CACHE_SIZE="67108864",
                GDAL_HTTP_USERAGENT=UA)


def res(z):
    """Web-Mercator metres per pixel at zoom z."""
    return 2 * ORIGIN / (TILE << z)


def tile_ll(z, x, y):
    """(south, west, north, east) degrees of tile (z, x, y)."""
    n = 1 << z
    w, e = x / n * 360 - 180, (x + 1) / n * 360 - 180
    nn = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    s = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return s, w, nn, e


def ll_tile(lat, lon, z):
    n = 1 << z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    return min(n - 1, max(0, x)), min(n - 1, max(0, y))


def pack_bounds(z, px, py):
    """EPSG:3857 (minx, miny, maxx, maxy) of pack (z, px, py): the zoom z - K tile px, py."""
    s = PACK_PX * res(z)
    x0, y1 = -ORIGIN + px * s, ORIGIN - py * s
    return x0, y1 - s, x0 + s, y1


# --- sources ------------------------------------------------------------------------------------------------------
def _get_json(url, data=None, timeout=60):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"User-Agent": UA, "Content-Type": "application/json"})
    for i in range(5):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception:
            if i == 4:
                raise
            time.sleep(2 * (i + 1))


class NaipPC:
    """NAIP from the Planetary Computer: items over a box, the newest flight per quarter quad."""
    name = "naip"
    credit = "USDA NAIP (public domain), via Microsoft Planetary Computer"

    def __init__(self, min_year=2018):
        self.min_year = min_year
        self._tok, self._tok_t = None, 0.0

    def token(self):
        if self._tok is None or time.time() - self._tok_t > TOKEN_TTL_S:
            self._tok = _get_json(SAS.format(collection="naip"))["token"]
            self._tok_t = time.time()
        return self._tok

    def items(self, s, w, n, e):
        """[{href, year, gsd, date}] newest first (one per quarter quad: its newest flight since min_year)."""
        body = {"collections": ["naip"], "bbox": [w, s, e, n], "limit": 500}
        feats, url = [], STAC
        while url:
            d = _get_json(url, body)
            feats += d.get("features", [])
            nxt = [l for l in d.get("links", []) if l.get("rel") == "next"]
            url, body = (nxt[0]["href"], nxt[0].get("body", body)) if nxt else (None, None)
        best = {}
        for f in feats:
            p = f["properties"]
            year = int(p.get("naip:year") or p["datetime"][:4])
            if year < self.min_year:
                continue
            qq = "_".join(f["id"].split("_")[1:4])        # m_3907644_sw
            if qq not in best or (year, p["datetime"]) > (best[qq]["year"], best[qq]["date"]):
                best[qq] = dict(href=f["assets"]["image"]["href"], year=year, gsd=float(p.get("gsd") or 0.6),
                                date=p["datetime"], bbox=f["bbox"])
        return sorted(best.values(), key=lambda it: it["date"], reverse=True)

    def open_href(self, href):
        return "/vsicurl/" + href + "?" + self.token()


SOURCES = {"naip-pc": NaipPC}


# --- one zoom-17 pack (a worker process) -------------------------------------------------------------------------
_W = {}                     # per worker: open datasets


def _dataset(path, level):
    import rasterio
    key = (path.split("?")[0], level)
    ds = _W.get(key)
    if ds is None:
        if len(_W) > 24:
            for v in _W.values():
                v.close()
            _W.clear()
        ds = _W[key] = rasterio.open(path, overview_level=level) if level is not None else rasterio.open(path)
    return ds


def _level_for(path, target_m, ratio):
    """The overview level (None: the full image) whose pixel is the coarsest <= ratio x target_m."""
    ds = _dataset(path, None)
    r0 = abs(ds.res[0])
    best, best_f = None, 1
    for i, f in enumerate(ds.overviews(1)):
        if r0 * f <= target_m * ratio + 1e-9 and f > best_f:
            best, best_f = i, f
    return best, r0 * best_f


def encode_tile(rgb, quality=QUALITY, speed=SPEED):
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, format="AVIF", quality=quality, subsampling="4:4:4", speed=speed, max_threads=1)
    return buf.getvalue()


def encode_pack(z, px, py, img, filled, quality, speed):
    """The pack's bytes from its 2048 px RGB image (texels not filled: black, the chain's nodata), None if empty."""
    tiles = {}
    n = 1 << K
    for i in range(n * n):
        ty, tx = divmod(i, n)
        sl = (slice(ty * TILE, (ty + 1) * TILE), slice(tx * TILE, (tx + 1) * TILE))
        f = filled[sl]
        if not f.any():
            continue
        t = img[sl]
        if not f.all():
            t = np.where(f[..., None], t, 0).astype(np.uint8)
        tiles[i] = encode_tile(np.ascontiguousarray(t), quality, speed)
    return tilepack.build(z, px, py, K, tiles) if tiles else None


def build_top_pack(job):
    """Worker: the zoom-17 pack px, py (a zoom-14 tile) from the source items, newest on top. Returns (px, py, pack
    bytes or None, half-size RGB + filled (for the zooms below), stats)."""
    import rasterio
    from rasterio.warp import reproject, Resampling
    px, py, items, src_name, token, ratio, quality, speed = job
    os.environ.update(GDAL_ENV)
    src = SOURCES[src_name]()
    src._tok, src._tok_t = token, time.time()
    x0, y0, x1, y1 = pack_bounds(Z_TOP, px, py)
    lat = math.degrees(math.atan(math.sinh(((y0 + y1) / 2) / R)))
    target = res(Z_TOP) * math.cos(math.radians(lat))       # the texel's ground size
    dst_t = rasterio.transform.from_origin(x0, y1, res(Z_TOP), res(Z_TOP))
    img = np.zeros((3, PACK_PX, PACK_PX), np.uint8)
    filled = np.zeros((PACK_PX, PACK_PX), bool)
    t0 = time.time()
    used, read_m = [], []
    with rasterio.Env(GDAL_CACHEMAX=256, **GDAL_ENV):
        for it in items:
            b = it["bbox"]                  # (w, s, e, n) degrees: a cheap reject
            s_, w_, n_, e_ = tile_ll(Z_LOW, px, py)
            if b[0] > e_ or b[2] < w_ or b[1] > n_ or b[3] < s_:
                continue
            path = src.open_href(it["href"])
            level, rm = _level_for(path, target, ratio)
            ds = _dataset(path, level)
            tmp = np.zeros((3, PACK_PX, PACK_PX), np.uint8)
            reproject(source=rasterio.band(ds, (1, 2, 3)), destination=tmp, src_transform=ds.transform,
                      src_crs=ds.crs, dst_transform=dst_t, dst_crs="EPSG:3857", dst_nodata=0,
                      resampling=Resampling.average if rm < target * 0.95 else Resampling.cubic, num_threads=1)
            m = tmp.any(axis=0) & ~filled
            if m.any():
                img[:, m] = tmp[:, m]
                filled |= m
                used.append(it["year"])
                read_m.append(rm)
            if filled.all():
                break
    t1 = time.time()
    rgb = np.ascontiguousarray(img.transpose(1, 2, 0))
    data = encode_pack(Z_TOP, px, py, rgb, filled, quality, speed) if filled.any() else None
    t2 = time.time()
    half = Image.fromarray(rgb).reduce(2) if filled.any() else None
    hf = filled.reshape(PACK_PX // 2, 2, PACK_PX // 2, 2).any(axis=(1, 3)) if filled.any() else None
    return (px, py, data, (np.asarray(half) if half is not None else None, hf),
            dict(read_s=t1 - t0, enc_s=t2 - t1, years=sorted(set(used)), read_m=sorted(set(round(r, 2) for r in read_m))))


def build_low_packs(cx, cy, canvas, cfill, quality, speed):
    """The cell's zoom-16 .. 14 packs from its zoom-16 canvas (8192 px; filled mask): {(z, px, py): bytes}."""
    out = {}
    img, fill = canvas, cfill
    for z in range(Z_TOP - 1, Z_LOW - 1, -1):
        n = img.shape[0] // PACK_PX
        for j in range(n):
            for i in range(n):
                sl = (slice(j * PACK_PX, (j + 1) * PACK_PX), slice(i * PACK_PX, (i + 1) * PACK_PX))
                if not fill[sl].any():
                    continue
                px, py = (cx << (z - K - CELL_Z)) + i, (cy << (z - K - CELL_Z)) + j
                d = encode_pack(z, px, py, img[sl], fill[sl], quality, speed)
                if d:
                    out[(z, px, py)] = d
        if z > Z_LOW:
            img = np.asarray(Image.fromarray(img).reduce(2))
            fill = fill.reshape(fill.shape[0] // 2, 2, fill.shape[1] // 2, 2).any(axis=(1, 3))
    return out


# --- coverage ----------------------------------------------------------------------------------------------------
# Several writers (the workflow's matrix jobs, the mini PC's on-demand builder) never write a region's file from its
# previous copy: each built cell leaves a fragment _cells/{cx}_{cy}.json, and --merge rebuilds every region file from
# all of its region's fragments (a merge lost to a race is made good by the next one).
def cov_path(out, rx, ry):
    return os.path.join(out, "cov", str(rx), f"{ry}.json")


def frag_path(out, cx, cy):
    return os.path.join(out, "_cells", f"{cx}_{cy}.json")


def write_fragments(out, cells, src, gen):
    for (cx, cy), (mask, year) in cells.items():
        p = frag_path(out, cx, cy)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        doc = {"cell": [cx, cy], "g": gen if mask else 0, "src": src, "year": year,
               "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        if mask:
            doc["p"] = f"{mask:016x}"
        with open(p + ".tmp", "w") as fh:
            json.dump(doc, fh, separators=(",", ":"), sort_keys=True)
        os.replace(p + ".tmp", p)


def merge_fragments(frag_dir, out, regions=None):
    """Region files under OUT/cov from every fragment in frag_dir/_cells (only `regions` {(rx, ry)} when given): a
    cell's newest generation wins. Returns the regions written."""
    by_region = {}
    d = os.path.join(frag_dir, "_cells")
    for n in sorted(os.listdir(d)) if os.path.isdir(d) else ():
        if not n.endswith(".json"):
            continue
        with open(os.path.join(d, n)) as fh:
            f = json.load(fh)
        cx, cy = (int(v) for v in f["cell"])
        r = (cx >> (CELL_Z - REGION_Z), cy >> (CELL_Z - REGION_Z))
        if regions is not None and r not in regions:
            continue
        cells = by_region.setdefault(r, {})
        k = f"{cx},{cy}"
        old = cells.get(k)
        if old is not None and old.get("p") and (not f.get("p") or old["g"] > f["g"]):
            continue
        e = {"g": int(f["g"]), "src": f.get("src", "")}
        if f.get("p"):
            e["p"], e["year"] = f["p"], f.get("year")
        cells[k] = e
    for (rx, ry), cells in by_region.items():
        doc = {"format": 1, "region": [rx, ry], "cells": cells,
               "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        p = cov_path(out, rx, ry)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p + ".tmp", "w") as fh:
            json.dump(doc, fh, separators=(",", ":"), sort_keys=True)
        os.replace(p + ".tmp", p)
    return sorted(by_region)


def cov_merge(out, cov_in, cells, src, gen):
    """Writes the coverage files of the regions of `cells` ({(cx, cy): (mask, year)}), merged with cov_in's."""
    by_region = {}
    for (cx, cy), v in cells.items():
        by_region.setdefault((cx >> (CELL_Z - REGION_Z), cy >> (CELL_Z - REGION_Z)), {})[(cx, cy)] = v
    for (rx, ry), cs in by_region.items():
        doc = None
        for d in (cov_in, out):
            if d and os.path.exists(cov_path(d, rx, ry)):
                with open(cov_path(d, rx, ry)) as fh:
                    doc = json.load(fh)
                break
        if not doc or doc.get("format") != 1:
            doc = {"format": 1, "region": [rx, ry], "cells": {}}
        for (cx, cy), (mask, year) in cs.items():
            k = f"{cx},{cy}"
            old = doc["cells"].get(k) or {}
            if mask:
                if old.get("p") and old.get("g") == gen:     # (packs added to this generation's cell)
                    mask |= int(old["p"], 16)
                doc["cells"][k] = {"g": gen, "p": f"{mask:016x}", "src": src, "year": year}
            elif not old.get("p"):
                doc["cells"][k] = {"g": 0, "src": src}      # no photos there
        doc["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        p = cov_path(out, rx, ry)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p + ".tmp", "w") as fh:
            json.dump(doc, fh, separators=(",", ":"), sort_keys=True)
        os.replace(p + ".tmp", p)


# --- driver ------------------------------------------------------------------------------------------------------
def write(out, gen, z, px, py, data):
    p = os.path.join(out, f"g{gen}", tilepack.path(z, px, py))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".tmp", "wb") as fh:
        fh.write(data)
    os.replace(p + ".tmp", p)
    return len(data)


def build_cell(args, src, cx, cy, packs):
    """Builds the cell's zoom-17 packs `packs` ({(px, py)}) and its zoom-16 .. 14 packs. Returns (mask, year, stats)."""
    t0 = time.time()
    s, w, n, e = tile_ll(CELL_Z, cx, cy)
    items = src.items(s, w, n, e)
    if not items:
        return 0, None, dict(cell=[cx, cy], packs=0, mb=0.0, s=round(time.time() - t0, 1), note="no photos")
    tok = src.token()
    corr = getattr(args, "corr_packs", set())
    jobs = [(px, py, items, args.source, tok, args.res_ratio_corr if (px, py) in corr and args.res_ratio_corr
             else args.res_ratio, args.quality, args.speed) for px, py in sorted(packs)]
    canvas = np.zeros((PACK_PX * 4, PACK_PX * 4, 3), np.uint8)       # zoom 16 of the whole cell
    cfill = np.zeros((PACK_PX * 4, PACK_PX * 4), bool)
    mask, nbytes, npk, years, rs, es, rms = 0, 0, 0, set(), 0.0, 0.0, set()
    with ProcessPoolExecutor(args.workers) as pool:                  # (a fresh pool per cell: CI lesson 2026-11-11)
        for px, py, data, (half, hf), st in pool.map(build_top_pack, jobs):
            rs += st["read_s"]
            es += st["enc_s"]
            years.update(st["years"])
            rms.update(st["read_m"])
            if data is None:
                continue
            i, j = px - (cx << K), py - (cy << K)
            mask |= 1 << (j * 8 + i)
            nbytes += write(args.out, args.gen, Z_TOP, px, py, data)
            npk += 1
            sl = (slice(j * PACK_PX // 2, (j + 1) * PACK_PX // 2), slice(i * PACK_PX // 2, (i + 1) * PACK_PX // 2))
            canvas[sl] = half
            cfill[sl] = hf
    t1 = time.time()
    low = build_low_packs(cx, cy, canvas, cfill, args.quality, args.speed) if mask else {}
    for (z, px, py), d in low.items():
        nbytes += write(args.out, args.gen, z, px, py, d)
    t2 = time.time()
    year = max(years) if years else None
    return mask, year, dict(cell=[cx, cy], packs=npk, low_packs=len(low), mb=round(nbytes / 1e6, 2),
                            s=round(t2 - t0, 1), top_s=round(t1 - t0, 1), low_s=round(t2 - t1, 1),
                            read_cpu_s=round(rs, 1), enc_cpu_s=round(es, 1), years=sorted(years),
                            read_m=sorted(rms))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--cells", help='zoom-11 cells "x,y x,y"')
    g.add_argument("--packs", help="npz with x, y arrays of zoom-14 tiles (zoom-17 packs)")
    g.add_argument("--point", nargs=2, type=float, metavar=("LAT", "LON"))
    g.add_argument("--merge", metavar="FRAG_DIR", help="only rebuild OUT/cov from FRAG_DIR/_cells (no photos read)")
    ap.add_argument("--only-cells", help='with --packs: only these cells "x,y x,y" (a matrix job\'s share)')
    ap.add_argument("--fragments", action="store_true",
                    help="leave OUT/_cells fragments only (several writers: merged later by --merge)")
    ap.add_argument("--radius", type=float, default=2000.0, help="m round --point")
    ap.add_argument("--source", default="naip-pc", choices=sorted(SOURCES))
    ap.add_argument("--gen", type=int, default=1)
    ap.add_argument("--quality", type=int, default=QUALITY)
    ap.add_argument("--speed", type=int, default=SPEED)
    ap.add_argument("--res-ratio", type=float, default=1.0)
    ap.add_argument("--res-ratio-corr", type=float, default=0.0,
                    help="--packs with a cls field: this ratio for its 'corr' packs (1.3: NAIP's 1.2 m overview, a "
                         "quarter of the bytes read, SSIM 0.96-0.97 against the 0.6 m read)")
    ap.add_argument("--min-year", type=int, default=2018)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--cov-in", help="the current coverage files (a folder with cov/), merged into OUT's")
    ap.add_argument("--deadline", type=float, default=0.0, help="unix time: no cell started after it (exit 75)")
    args = ap.parse_args()
    if args.merge:
        print(json.dumps({"regions": merge_fragments(args.merge, args.out)}))
        return
    src = SOURCES[args.source](args.min_year) if args.source == "naip-pc" else SOURCES[args.source]()
    want = {}                                   # {(cx, cy): {(px, py)}}
    if args.cells:
        for c in args.cells.split():
            cx, cy = (int(v) for v in c.split(","))
            want[(cx, cy)] = {((cx << K) + i, (cy << K) + j) for j in range(8) for i in range(8)}
    else:
        if args.packs:
            d = np.load(args.packs, allow_pickle=False)
            tiles = list(zip(d["x"].tolist(), d["y"].tolist()))
            if "cls" in d.files:
                args.corr_packs = {t for t, c in zip(tiles, d["cls"].tolist()) if c == "corr"}
        else:
            lat, lon = args.point
            dl = args.radius / 111320.0
            dlo = dl / math.cos(math.radians(lat))
            x0, y0 = ll_tile(lat + dl, lon - dlo, Z_LOW)
            x1, y1 = ll_tile(lat - dl, lon + dlo, Z_LOW)
            tiles = [(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]
        for px, py in tiles:
            want.setdefault((px >> K, py >> K), set()).add((px, py))
        if args.only_cells:
            keep = {tuple(int(v) for v in c.split(",")) for c in args.only_cells.split()}
            want = {c: p for c, p in want.items() if c in keep}
    done = {}

    def finish():
        write_fragments(args.out, done, src.name, args.gen)
        if not args.fragments:
            cov_merge(args.out, args.cov_in, done, src.name, args.gen)

    for (cx, cy), packs in sorted(want.items()):
        if args.deadline and time.time() > args.deadline:
            print(json.dumps(dict(cell=[cx, cy], note="left for the next run")), flush=True)
            finish()
            sys.exit(75)
        mask, year, st = build_cell(args, src, cx, cy, packs)
        done[(cx, cy)] = (mask, year)
        print(json.dumps(st), flush=True)
    finish()


if __name__ == "__main__":
    main()
