"""The German states that publish their DOM and DGM as tiles, not as a WCS: the build machine's side of
tools/build_de_decks.py (never the game's). A state's tiles are found through an index (mundialis' open tile indices,
github.com/mundialis/tile-indices, a state's own listing, or the state's fixed naming), read where a bridge's samples
fall, and kept decoded while the cells round them are built.

    Sampler(state, index_dir, fetch).sample(lat, lon)  -> (dom, dgm) heights (NaN: none)

Reading a tile:
  * a GeoTIFF whose server answers ranges (Bavaria's 0.2 m DOM, 48 MB a km): only the TIFF blocks the samples
    fall in (tifffile reads the directory through RangeFile);
  * a zip of one tile (Baden-Wuerttemberg, Sachsen, Thueringen: 2 km, ~15 MB): the whole zip;
  * a member of one large zip (Hamburg, Bremen: 1.3 GB a product): the member alone, through RangeFile;
  * Schleswig-Holstein's download script (no ranges): the whole tile, the 0.2 m DOM thinned to 1 m;
  * ASCII XYZ (Bremen, Schleswig-Holstein's DGM1): parsed into a 1 m grid.
"""
import io
import math
import os
import re
import sqlite3
import threading
import zipfile
from collections import OrderedDict

import numpy as np

NODATA_BELOW = -1000.0
GRID_CACHE = 24             # decoded tiles kept (each ~4 MB at 1 m; a blockwise one up to SEGS_PER_GRID blocks)
BLOCK = 1 << 18             # RangeFile's read size (256 KiB)
SEGS_PER_GRID = 48          # decoded TIFF blocks kept per tile (Bavaria: 256 x 256 = 0.5 MB each)
FILE_BLOCKS = 16            # RangeFile blocks kept per file

MUNDIALIS = "https://raw.githubusercontent.com/mundialis/tile-indices/main/"
# (state: (DOM index, DGM index)): an index is ("gpkg", mundialis path) / ("listing", url, regex) / ("fixed", name)
INDEX = {
    "by": (("fixed", "by_dom"), ("fixed", "by_dgm")),
    "bw": (("gpkg", "DSM/BW/BW_DSM_tindex_proj"), ("derive", "bw_dgm")),
    "ni": (("gpkg", "DSM/NI/NI_DSM_tindex_proj"), ("gpkg", "DTM/NI/NI_DTM_tindex_proj")),
    "rp": (("listing", "https://geobasis-rlp.de/data/dom1/current/tif/", r"dom1_32_[0-9]+_[0-9]+_1_rp_[0-9]+\.tif"),
           ("gpkg", "DTM/RP/rp_dtm_tindex_proj")),
    "sn": (("gpkg", "DSM/SN/SN_DOM_tileindex_proj"), ("gpkg", "DTM/SN/SN_DGM_tileindex_proj")),
    "th": (("gpkg", "DSM/TH/th_dsm_tindex_proj"), ("gpkg", "DTM/TH/th_dtm_tindex_proj")),
    "hh": (("gpkg", "iDSM/HH/hh_bdom_tindex_proj"), ("gpkg", "DTM/HH/hh_dgm1_tindex_proj")),
    "hb": (("gpkg", "DSM/HB/hb_dom1_tindex_proj"), ("gpkg", "DTM/HB/hb_dgm1_tindex_proj")),
    "sh": (("gpkg", "iDSM/SH/sh_bdom_tindex_proj"), ("gpkg", "DTM/SH/sh_dtm_tindex_proj")),
}
# Bremen's indices name a member of the state's zips (Bremen 2017, Bremerhaven 2015)
HB_ZIPS = {"DOM": "https://gdi2.geo.bremen.de/inspire/download/DOM/data/",
           "DGM": "https://gdi2.geo.bremen.de/inspire/download/DGM/data/"}

# a tile's name: zone, east and north km of its lower left corner, its size in km (dom1_32_445_5418_2_bw,
# dom1_32465_5896_1_hb, dgm1_33360_5598_2_sn, bdom20nc_32_425_6002_1_sh)
NAME = re.compile(r"_(32|33)_?(\d{3})_(\d{4})_(\d)_")
YEAR = re.compile(r"_(\d{4})(?:-\d{4})?\.(?:tif|zip|xyz)")


def tile_of_name(name):
    m = NAME.search(name)
    if not m:
        return None
    return int(m.group(2)), int(m.group(3)), int(m.group(4))


class ServiceDown(IOError):
    """A state's download answered with something else than its tile (a maintenance page)."""


class Index:
    """km cell (east, north) -> (url, zip member or None, tile size km) of one product of one state."""

    def __init__(self):
        self.cells = {}

    def add(self, e, n, size, url, member=None, rank=0):
        for i in range(size):
            for j in range(size):
                old = self.cells.get((e + i, n + j))
                if old is None or rank >= old[3]:
                    self.cells[(e + i, n + j)] = (url, member, size, rank)

    def at(self, e_km, n_km):
        got = self.cells.get((e_km, n_km))
        return None if got is None else got[:3]

    def __len__(self):
        return len(self.cells)


def _gpkg_rows(path):
    con = sqlite3.connect(path)
    try:
        table = con.execute("select table_name from gpkg_contents").fetchone()[0]
        cols = [r[1] for r in con.execute(f'pragma table_info("{table}")')]
        extra = "datum" if "datum" in cols else "NULL"
        return con.execute(f'select location, {extra} from "{table}"').fetchall()
    finally:
        con.close()


def _fetch_file(fetch, url, path):
    if not os.path.exists(path):
        body = fetch(url)
        if url.endswith(".gz"):
            import gzip
            body = gzip.decompress(body)
        with open(path + ".part", "wb") as fh:
            fh.write(body)
        os.replace(path + ".part", path)
    return path


def build_index(state, which, index_dir, fetch):
    """The Index of state's product which (0 DOM, 1 DGM)."""
    spec = INDEX[state][which]
    ix = Index()
    os.makedirs(index_dir, exist_ok=True)
    if spec[0] == "fixed":
        return spec[1]          # (Bavaria: every km by name, Sampler resolves it)
    if spec[0] == "derive":     # (Baden-Wuerttemberg's DGM: the DOM's names in the DGM's folder)
        dom = build_index(state, 0, index_dir, fetch)
        for k, (url, member, size, rank) in dom.cells.items():
            ix.cells[k] = (url.replace("/data/dom1/dom1_", "/data/dgm/dgm1_"), member, size, rank)
        return ix
    if spec[0] == "listing":
        path = _fetch_file(fetch, spec[1], os.path.join(index_dir, f"{state}_{which}_listing.html"))
        with open(path, encoding="utf-8", errors="replace") as fh:
            names = sorted(set(re.findall(spec[2], fh.read())))
        for name in names:
            t = tile_of_name(name)
            y = YEAR.search(name)
            if t:
                ix.add(*t, spec[1] + name, rank=int(y.group(1)) if y else 0)
        return ix
    path = _fetch_file(fetch, MUNDIALIS + spec[1] + ".gpkg.gz",
                       os.path.join(index_dir, spec[1].replace("/", "_") + ".gpkg"))
    for loc, datum in _gpkg_rows(path):
        loc = str(loc)
        member = None
        if loc.startswith("/vsizip/vsicurl/"):
            loc = loc[len("/vsizip/vsicurl/"):].replace("https://daten-hamburg.de/", "https://www.daten-hamburg.de/")
            k = loc.find(".zip/")
            loc, member = loc[:k + 4], loc[k + 5:]
        elif state == "hb":
            folder, member = loc.split("/", 1)          # Gitternetz_DOM1_2017/dom1_32465_5896_1_hb.xyz
            kind = "DOM" if "DOM" in folder else "DGM"
            where = "HB" if folder.endswith("2017") else "BHV"
            loc = f"{HB_ZIPS[kind]}{folder}_{where}_ASCII_XYZ.zip"
        name = member or loc
        if state == "sh":
            name = loc.split("file=", 1)[1].split("&", 1)[0]
        t = tile_of_name(name.rsplit("/", 1)[-1])
        if not t:
            continue
        y = YEAR.search(name)
        rank = int(str(datum).replace("-", "")[:8]) if datum else (int(y.group(1)) if y else 0)
        if "Vollkacheln" not in loc and state == "ni":
            rank -= 1           # (Lower Saxony's partial tiles yield to its full ones)
        ix.add(*t, loc, member, rank)
    return ix


class RangeFile(io.RawIOBase):
    """A remote file read by HTTP ranges, BLOCK at a time (the blocks kept): seekable, for tifffile and zipfile.
    If the server answers a range with the whole body, that body is the file."""

    def __init__(self, fetch_range, url):
        super().__init__()
        self.fetch_range, self.url = fetch_range, url
        self.pos, self.size, self.blocks, self.whole = 0, None, OrderedDict(), None
        self._block(0)

    def _block(self, i):
        if self.whole is not None:
            return self.whole[i * BLOCK:(i + 1) * BLOCK]
        got = self.blocks.get(i)
        if got is not None:
            self.blocks.move_to_end(i)
            return got
        status, body, total = self.fetch_range(self.url, i * BLOCK, (i + 1) * BLOCK - 1)
        if status != 206:
            self.whole, self.size = body, len(body)
            return body[i * BLOCK:(i + 1) * BLOCK]
        if total is not None:
            self.size = total
        self.blocks[i] = body
        while len(self.blocks) > FILE_BLOCKS:
            self.blocks.popitem(last=False)
        return body

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else self.pos + off if whence == 1 else self.size + off
        return self.pos

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        n = max(0, min(n, self.size - self.pos))
        out = bytearray()
        while len(out) < n:
            i, o = divmod(self.pos + len(out), BLOCK)
            b = self._block(i)
            take = b[o:o + n - len(out)]
            if not take:
                break
            out += take
        self.pos += len(out)
        return bytes(out)

    def readinto(self, buf):
        got = self.read(len(buf))
        buf[:len(got)] = got
        return len(got)


class Grid:
    """Heights of one tile: a north-up grid with its left x / top y and pixel size, or a TIFF read block by block."""

    def __init__(self, a=None, x0=0.0, y0=0.0, dx=1.0, dy=1.0, tiff=None):
        self.a, self.x0, self.y0, self.dx, self.dy, self.tiff = a, x0, y0, dx, dy, tiff
        self.segs = OrderedDict()

    def at(self, x, y):
        c = np.floor((x - self.x0) / self.dx).astype(np.int64)
        r = np.floor((self.y0 - y) / self.dy).astype(np.int64)
        out = np.full(len(x), np.nan)
        if self.tiff is None:
            ok = (r >= 0) & (r < self.a.shape[0]) & (c >= 0) & (c < self.a.shape[1])
            out[ok] = self.a[r[ok], c[ok]]
        else:
            page, h, w, th, tw = self.tiff
            ok = (r >= 0) & (r < h) & (c >= 0) & (c < w)
            per_row = (w + tw - 1) // tw
            seg = np.where(ok, (r // th) * per_row + c // tw, -1)
            for s in np.unique(seg[ok]):
                k = np.flatnonzero(seg == s)
                block = self._seg(page, int(s))
                rr, cc = r[k] - (s // per_row) * th, c[k] - (s % per_row) * tw
                good = (rr < block.shape[0]) & (cc < block.shape[1])
                out[k[good]] = block[rr[good], cc[good]]
        return np.where(out < NODATA_BELOW, np.nan, out)

    def _seg(self, page, s):
        got = self.segs.get(s)
        if got is not None:
            return got
        fh = page.parent.filehandle
        fh.seek(page.dataoffsets[s])
        data = fh.read(page.databytecounts[s])
        seg, _idx, _shape = page.decode(data, s)
        a = np.asarray(seg, np.float64).reshape(seg.shape[-3], seg.shape[-2]) if seg.ndim > 2 else \
            np.asarray(seg, np.float64)
        nd = page.nodata
        if nd is not None:
            a = np.where(a == nd, np.nan, a)
        self.segs[s] = a
        while len(self.segs) > SEGS_PER_GRID:
            self.segs.popitem(last=False)
        return a


def _geo(page):
    tags = {t.code: t.value for t in page.tags.values()}
    scale, tie = tags.get(33550), tags.get(33922)
    if scale is None or tie is None:
        raise ValueError("GeoTIFF without tie point / pixel scale")
    x0 = float(tie[3]) - float(tie[0]) * float(scale[0])
    y0 = float(tie[4]) + float(tie[1]) * float(scale[1])
    return x0, y0, float(scale[0]), float(scale[1])


def grid_of_tiff(fh, thin_to=1.0, blockwise=False):
    import tifffile
    tf = tifffile.TiffFile(fh)
    page = tf.pages[0]
    x0, y0, dx, dy = _geo(page)
    if blockwise and page.is_tiled:
        return Grid(None, x0, y0, dx, dy, tiff=(page, page.imagelength, page.imagewidth, page.tilelength,
                                                 page.tilewidth))
    a = np.asarray(page.asarray(), np.float64)
    if a.ndim == 3:
        a = a[..., 0]
    nd = page.nodata
    if nd is not None:
        a = np.where(a == nd, np.nan, a)
    tf.close()
    k = int(round(thin_to / dx)) if dx < thin_to else 1
    if k > 1:                   # (a 0.2 m DOM: the middle pixel of each k x k block, the block's own centre)
        a = a[k // 2::k, k // 2::k]
        dx, dy = dx * k, dy * k
    return Grid(np.ascontiguousarray(a), x0, y0, dx, dy)


def grid_of_xyz(body):
    head = body[:200].lstrip()
    if head[:1].isalpha():                      # (a header line: "x y z")
        body = body.split(b"\n", 1)[1] if b"\n" in body else b""
    v = np.array(body.replace(b";", b" ").split(), np.float64)
    v = v[: len(v) // 3 * 3].reshape(-1, 3)
    x, y, z = v[:, 0], v[:, 1], v[:, 2]
    step = 1.0
    xmin, ymax = x.min(), y.max()
    c = np.round((x - xmin) / step).astype(np.int64)
    r = np.round((ymax - y) / step).astype(np.int64)
    a = np.full((r.max() + 1, c.max() + 1), np.nan)
    a[r, c] = z
    return Grid(a, xmin - step / 2.0, ymax + step / 2.0, step, step)


class Sampler:
    """(dom, dgm) of points from a tile state's DOM and DGM tiles."""

    def __init__(self, state, zone, index_dir, fetch, fetch_range):
        self.state, self.zone = state, zone
        self.fetch, self.fetch_range = fetch, fetch_range
        self.index = [build_index(state, w, index_dir, fetch) for w in (0, 1)]
        self.grids = OrderedDict()
        self.zips = OrderedDict()
        self.lock = threading.Lock()
        self.stats = {"tiles": 0, "missing": 0}

    def _where(self, which, e_km, n_km):
        ix = self.index[which]
        if ix == "by_dom":
            return f"https://download1.bayernwolke.de/a/dom20/DOM/32{e_km}_{n_km}_20_DOM.tif", None, 1
        if ix == "by_dgm":
            return f"https://download1.bayernwolke.de/a/dgm/dgm1/{e_km}_{n_km}.tif", None, 1
        return ix.at(e_km, n_km)

    def _member(self, names, member, e_km, n_km):
        """The zip member of km (e_km, n_km): the named one (Bremen's indices name it without its folder), else the
        tile of that km (Baden-Wuerttemberg's 2 km zips hold four 1 km tiles), else the first."""
        if member:
            got = [n for n in names if n == member or n.endswith("/" + member)]
            if got:
                return got[0]
        tiles = [n for n in names if n.lower().endswith((".tif", ".tiff", ".xyz"))]
        for n in tiles:
            t = tile_of_name(n.rsplit("/", 1)[-1])
            if t and t[0] <= e_km < t[0] + t[2] and t[1] <= n_km < t[1] + t[2]:
                return n
        return tiles[0] if tiles else None     # (a few of Baden-Wuerttemberg's border zips hold only csv files)

    def _zip(self, url, ranged):
        z = self.zips.get(url)
        if z is None:
            if ranged:
                z = zipfile.ZipFile(RangeFile(self.fetch_range, url))
            else:
                body = self.fetch(url)
                if body[:2] != b"PK":       # (Thueringen's maintenance page)
                    raise ServiceDown(url)
                z = zipfile.ZipFile(io.BytesIO(body))
            self.zips[url] = z
            while len(self.zips) > 3:
                self.zips.popitem(last=False)
        else:
            self.zips.move_to_end(url)
        return z

    def _grid(self, which, where, e_km, n_km):
        import netfetch
        url, member, _size = where
        try:
            name = None
            if url.endswith(".zip"):
                z = self._zip(url, ranged=self.state in ("hh", "hb"))
                name = self._member(z.namelist(), member, e_km, n_km)
                if name is None:
                    raise netfetch.NotFound(url)
            key = (which, url, name)
            got = self.grids.get(key)
            if got is not None:
                self.grids.move_to_end(key)
                return got
            if name is not None:
                body = z.read(name)
                g = grid_of_xyz(body) if name.endswith(".xyz") else grid_of_tiff(io.BytesIO(body))
            elif "massen.php" in url:
                body = self.fetch(url)
                g = grid_of_xyz(body) if ".xyz" in url else grid_of_tiff(io.BytesIO(body))
            elif self.state == "by" and which == 0:
                g = grid_of_tiff(RangeFile(self.fetch_range, url), blockwise=True)
            else:
                g = grid_of_tiff(io.BytesIO(self.fetch(url)))
            self.stats["tiles"] += 1
        except netfetch.NotFound:
            key, g = (which, url, None), None
            self.stats["missing"] += 1
        self.grids[key] = g
        while len(self.grids) > GRID_CACHE:
            self.grids.popitem(last=False)
        return g

    def sample(self, lat, lon):
        import bcproj
        x, y = bcproj.utm(lat, lon, self.zone)
        x, y = np.asarray(x, np.float64), np.asarray(y, np.float64)
        out = (np.full(len(x), np.nan), np.full(len(x), np.nan))
        ek, nk = np.floor(x / 1000.0).astype(np.int64), np.floor(y / 1000.0).astype(np.int64)
        for which in (0, 1):
            for e, n in sorted(set(zip(ek.tolist(), nk.tolist()))):
                where = self._where(which, e, n)
                if where is None:
                    continue
                g = self._grid(which, where, e, n)
                if g is None:
                    continue
                k = np.flatnonzero((ek == e) & (nk == n))
                out[which][k] = g.at(x[k], y[k])
        return out
