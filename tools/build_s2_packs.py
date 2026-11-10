#!/usr/bin/env python3
"""Tile packs (server/tilepack.py) of Sentinel-2 photos for the project's own tile host (the user, 2026-11-07: "sure
lets setup the r2 stuff"; server/cdn.py, imagery source "bngs2").

Source: ESA WorldCover's 2021 Sentinel-2 RGBNIR composite (s3://esa-worldcover-s2, eu-central-1, public; CC BY 4.0,
"Contains modified Copernicus Sentinel data 2021"): one Cloud-Optimised GeoTIFF per 1 x 1 degree (EPSG:4326, 12000 px,
1024 px tiles, Deflate + horizontal predictor, 4 x uint16 reflectance x 10^4: red, green, blue, NIR; overviews / 2 to /
16). Each Web-Mercator tile is sampled (bilinear) from the overview nearest its pixel size, tone-mapped (TONE) and
written as a 256 px JPEG; nodata (open sea, outside the files) black, as the imagery chain's nodata (the next source).

    build_s2_packs.py OUT --zoom 9 --bbox S W N E         # every pack of zoom 9 over the box (packs of 8 x 8, k = 3)
    build_s2_packs.py OUT --zoom 12 --pack 1240 1501      # one pack (the zoom-9 tile 1240, 1501's zoom-12 tiles)

Writes OUT/{z}/{px}/{py}.bngp (no file for a pack with no tile). The data repo's GitHub Action runs it by region and
uploads OUT to R2 (s2wc/v1/...).
"""
import argparse
import io
import math
import os
import struct
import sys
import threading
import urllib.error
import http.client
import time
import urllib.request
import zlib
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
import tilepack  # noqa: E402

BASE = "https://esa-worldcover-s2.s3.eu-central-1.amazonaws.com/rgbnir/2021"
PPD = 12000                 # pixels per degree at full resolution
K = 3                       # packs of 8 x 8 tiles
TILE = 256
JPEG_Q = 86
# reflectance (x 10^4) -> display: (r / WHITE) ^ (1 / GAMMA), saturation SAT round the luminance
TONE = {"white": 3000.0, "gamma": 2.0, "sat": 1.2}
UA = "BeamNG.Earth tile builder"


def _get(url, start=None, end=None):
    h = {"User-Agent": UA}
    if start is not None:
        h["Range"] = f"bytes={start}-{end - 1}"
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if attempt == 3:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException, OSError):
            # (a cut-off body too: IncompleteRead failed a whole region's job, 2026-11-08)
            if attempt == 3:
                raise
        time.sleep(1.5 * 2 ** attempt)


class Cog:
    """One 1-degree file: its levels' tile tables (offsets, counts, width) and a block cache."""

    def __init__(self, name, url):
        self.name, self.url = name, url
        head = _get(url, 0, 1 << 16)
        if head is None:
            raise FileNotFoundError(name)
        self.levels = []
        big = struct.unpack_from("<H", head, 2)[0] == 43
        off = struct.unpack_from("<Q", head, 8)[0] if big else struct.unpack_from("<I", head, 4)[0]
        while off:
            tags, off = self._ifd(head, off, big)
            w = tags[256][0]
            self.levels.append({"w": w, "tw": tags[322][0], "off": tags[324], "cnt": tags[325]})

    def _ifd(self, b, off, big):
        types = {3: ("H", 2), 4: ("I", 4), 16: ("Q", 8)}
        if big:
            n = struct.unpack_from("<Q", b, off)[0]
            p, esz, nxt_fmt = off + 8, 20, "<Q"
        else:
            n = struct.unpack_from("<H", b, off)[0]
            p, esz, nxt_fmt = off + 2, 12, "<I"
        tags = {}
        for i in range(n):
            e = p + i * esz
            tag, typ = struct.unpack_from("<HH", b, e)
            cnt = struct.unpack_from("<Q" if big else "<I", b, e + 4)[0]
            if typ not in types:
                continue
            f, sz = types[typ]
            voff = e + (12 if big else 8)
            if cnt * sz > (8 if big else 4):
                voff = struct.unpack_from("<Q" if big else "<I", b, voff)[0]
                if voff + cnt * sz > len(b):        # (a table past the header read: fetched)
                    data = _get(self.url, voff, voff + cnt * sz)
                    tags[tag] = struct.unpack_from(f"<{cnt}{f}", data, 0)
                    continue
            tags[tag] = struct.unpack_from(f"<{cnt}{f}", b, voff)
        nxt = struct.unpack_from(nxt_fmt, b, p + n * esz)[0]
        return tags, nxt


_cogs = {}
_blocks = OrderedDict()
_lock = threading.Lock()
BLOCK_CACHE = 160           # decoded 1024 px blocks kept (~12 MB each as float32 RGB: ~2 GB)


def cog_for(lat_sw, lon_sw):
    """The file of the degree with south-west corner (lat_sw, lon_sw), or None (no file: sea)."""
    ns = f"{'N' if lat_sw >= 0 else 'S'}{abs(lat_sw):02d}"
    ew = f"{'E' if lon_sw >= 0 else 'W'}{abs(lon_sw):03d}"
    name = f"ESA_WorldCover_10m_2021_v200_{ns}{ew}_S2RGBNIR.tif"
    key = (lat_sw, lon_sw)
    with _lock:
        if key in _cogs:
            return _cogs[key]
    try:
        c = Cog(name, f"{BASE}/{ns}/{name}")
    except FileNotFoundError:
        c = None
    with _lock:
        _cogs[key] = c
    return c


def block(c, lvl, bx, by):
    """Decoded block (1024, 1024, 3) float32 reflectance of level lvl, or None (sparse)."""
    key = (c.name, lvl, bx, by)
    with _lock:
        if key in _blocks:
            _blocks.move_to_end(key)
            return _blocks[key]
    L = c.levels[lvl]
    nx = -(-L["w"] // L["tw"])
    i = by * nx + bx
    o, n = L["off"][i], L["cnt"][i]
    a = None
    if n:
        raw = zlib.decompress(_get(c.url, o, o + n))
        t = L["tw"]
        a = np.frombuffer(raw, "<u2").reshape(t, t, 4)
        a = (np.cumsum(a.astype(np.uint32), axis=1) & 0xFFFF)[..., :3].astype(np.float32)   # predictor 2
    with _lock:
        _blocks[key] = a
        while len(_blocks) > BLOCK_CACHE:
            _blocks.popitem(last=False)
    return a


def level_for(z, lat):
    """The overview level (0 full, 4 = / 16) whose pixels are nearest a zoom-z tile pixel at lat, not coarser."""
    merc = 156543.034 * math.cos(math.radians(lat)) / (1 << z)     # m per tile pixel
    s2 = 111320.0 / PPD                                             # m per full-resolution pixel (north-south)
    return int(max(0, min(4, math.floor(math.log2(max(merc / s2, 1.0))))))


def sample(lat, lon, lvl):
    """Bilinear RGB reflectance (n, 3) and validity (n) at points (lat, lon arrays) from overview lvl."""
    out = np.zeros((lat.size, 3), np.float32)
    ok = np.zeros(lat.size, bool)
    f = 1 << lvl
    ppd = PPD / f
    lat_sw = np.floor(lat).astype(int)
    lon_sw = np.floor(lon).astype(int)
    for la, lo in sorted(set(zip(lat_sw.tolist(), lon_sw.tolist()))):
        sel = (lat_sw == la) & (lon_sw == lo)
        c = cog_for(la, lo)
        if c is None or lvl >= len(c.levels):
            continue
        L = c.levels[lvl]
        t = L["tw"]
        col = (lon[sel] - lo) * ppd - 0.5
        row = (la + 1 - lat[sel]) * ppd - 0.5
        col = np.clip(col, 0, L["w"] - 1.001)
        row = np.clip(row, 0, L["w"] - 1.001)
        c0, r0 = np.floor(col).astype(int), np.floor(row).astype(int)
        fc, fr = (col - c0)[:, None], (row - r0)[:, None]
        acc = np.zeros((sel.sum(), 3), np.float32)
        good = np.ones(sel.sum(), bool)
        for dr, dc, w in ((0, 0, (1 - fr) * (1 - fc)), (0, 1, (1 - fr) * fc), (1, 0, fr * (1 - fc)), (1, 1, fr * fc)):
            rr = np.minimum(r0 + dr, L["w"] - 1)
            cc = np.minimum(c0 + dc, L["w"] - 1)
            v = np.zeros((rr.size, 3), np.float32)
            bx, by = cc // t, rr // t
            for (x, y) in set(zip(bx.tolist(), by.tolist())):
                m = (bx == x) & (by == y)
                b = block(c, lvl, x, y)
                if b is None:
                    good &= ~m
                    continue
                v[m] = b[rr[m] - y * t, cc[m] - x * t]
            good &= v.max(axis=1) > 0
            acc += v * w
        idx = np.flatnonzero(sel)
        out[idx] = acc
        ok[idx] = good
    return out, ok


def tone(rgb):
    x = np.clip(rgb / TONE["white"], 0, 1) ** (1 / TONE["gamma"])
    lum = (x * [0.2126, 0.7152, 0.0722]).sum(axis=-1, keepdims=True)
    x = np.clip(lum + (x - lum) * TONE["sat"], 0, 1)
    return (x * 255 + 0.5).astype(np.uint8)


def tile_latlon(z, x, y):
    """Pixel-centre lat, lon (TILE x TILE) of Web-Mercator tile (z, x, y)."""
    n = TILE << z
    i = (np.arange(TILE) + 0.5)
    lon = (x * TILE + i) / n * 360.0 - 180.0
    my = (y * TILE + i) / n
    lat = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * my))))
    return np.broadcast_to(lat[:, None], (TILE, TILE)), np.broadcast_to(lon[None, :], (TILE, TILE))


def build_pack(z, px, py):
    """The pack's bytes, or None when none of its tiles has data."""
    tiles = {}
    for i in range((1 << K) ** 2):
        tz, tx, ty = tilepack.tile_at(z, px, py, K, i)
        lat, lon = tile_latlon(tz, tx, ty)
        lvl = level_for(tz, float(lat[TILE // 2, 0]))
        rgb, ok = sample(lat.ravel(), lon.ravel(), lvl)
        if not ok.any():
            continue
        img = tone(rgb).reshape(TILE, TILE, 3)
        img[~ok.reshape(TILE, TILE)] = 0
        buf = io.BytesIO()
        Image.fromarray(img).save(buf, "JPEG", quality=JPEG_Q, optimize=True)
        tiles[i] = buf.getvalue()
    return tilepack.build(z, px, py, K, tiles) if tiles else None


_child_packs = OrderedDict()


def child_tile(src_dir, z, x, y):
    """Decoded tile (z, x, y) from the packs in src_dir (uint8 RGB), or None (no tile)."""
    pk, i = tilepack.pack_of(z, x, y, K)
    with _lock:
        tiles = _child_packs.get(pk)
    if tiles is None:
        try:
            with open(os.path.join(src_dir, tilepack.path(*pk)), "rb") as fh:
                tiles = tilepack.parse(fh.read())[4]
        except OSError:
            tiles = {}
        with _lock:
            _child_packs[pk] = tiles
            while len(_child_packs) > 64:
                _child_packs.popitem(last=False)
    b = tiles.get(i)
    return None if b is None else np.asarray(Image.open(io.BytesIO(b)).convert("RGB"))


def build_pack_down(z, px, py, src_dir):
    """A coarser zoom's pack from the next zoom's packs in src_dir: each tile the 2 x 2 average of its four children
    (black, no data, where a child is missing or black: as the source's nodata)."""
    tiles = {}
    for i in range((1 << K) ** 2):
        tz, tx, ty = tilepack.tile_at(z, px, py, K, i)
        img = np.zeros((TILE * 2, TILE * 2, 3), np.uint8)
        have = False
        for dy in (0, 1):
            for dx in (0, 1):
                c = child_tile(src_dir, tz + 1, 2 * tx + dx, 2 * ty + dy)
                if c is not None:
                    img[dy * TILE:(dy + 1) * TILE, dx * TILE:(dx + 1) * TILE] = c
                    have = True
        if not have:
            continue
        v = img.reshape(TILE, 2, TILE, 2, 3).astype(np.uint16)
        dark = (v.max(axis=4) <= 4)                         # (no data in a child pixel: the parent pixel black too)
        out = (v.sum(axis=(1, 3)) // 4).astype(np.uint8)
        out[dark.any(axis=(1, 3))] = 0
        buf = io.BytesIO()
        Image.fromarray(out).save(buf, "JPEG", quality=JPEG_Q, optimize=True)
        tiles[i] = buf.getvalue()
    return tilepack.build(z, px, py, K, tiles) if tiles else None


def packs_in(z, s, w, n, e):
    pz = z - K
    m = 1 << pz

    def xy(lat, lon):
        x = (lon + 180.0) / 360.0 * m
        y = (1 - math.asinh(math.tan(math.radians(max(-85.05, min(85.05, lat))))) / math.pi) / 2 * m
        return int(min(m - 1, max(0, x))), int(min(m - 1, max(0, y)))
    x0, y0 = xy(n, w)
    x1, y1 = xy(s, e)
    return [(z, x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--zoom", type=int, required=True)
    ap.add_argument("--bbox", type=float, nargs=4, metavar=("S", "W", "N", "E"))
    ap.add_argument("--pack", type=int, nargs=2, metavar=("PX", "PY"))
    ap.add_argument("--z5", type=int, nargs=2, metavar=("X", "Y"),
                    help="the packs under zoom-5 tile X Y (one region job: zoom 12 down to 8, --down)")
    ap.add_argument("--down", action="store_true", help="this zoom's packs from the next zoom's packs in OUT")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    if a.z5:
        x5, y5 = a.z5
        for z in range(a.zoom, K + 4, -1):          # (zoom 12 from the photos, 11 .. 8 from the zoom below)
            d = z - K - 5
            todo = [(z, (x5 << d) + i, (y5 << d) + j) for j in range(1 << d) for i in range(1 << d)]
            run(a.out, todo, a.workers, down=z != a.zoom)
        return
    if a.zoom < K:
        sys.exit(f"zoom must be >= {K}")
    todo = [(a.zoom, *a.pack)] if a.pack else packs_in(a.zoom, *a.bbox)
    run(a.out, todo, a.workers, down=a.down)


def run(out, todo, workers, down=False):
    done = [0, 0]

    def one(p):
        path = os.path.join(out, tilepack.path(*p))
        if os.path.exists(path):
            return
        data = build_pack_down(*p, out) if down else build_pack(*p)
        done[0] += 1
        if data is None:
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "wb") as fh:
            fh.write(data)
        os.replace(path + ".tmp", path)
        done[1] += 1
    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(one, todo))
    print(f"zoom {todo[0][0] if todo else '-'}: {len(todo)} packs, {done[1]} written", flush=True)


if __name__ == "__main__":
    main()
