"""A minimal Cloud-Optimised GeoTIFF reader over byte ranges (no GDAL, no tifffile).

Enough for the map-data rasters (ESA WorldCover 10 m, Meta/WRI canopy height): classic TIFF and
BigTIFF, either byte order, tiled single-band images of 8/16/32-bit integers or 32/64-bit floats,
compression none / Deflate (zlib, from the standard library) / PackBits, predictor 1 (none), 2
(horizontal differencing) or 3 (floating point, float32: the Copernicus DEM); any other compression
libtiff knows (LZW, GDAL's default for COGs: NRCan's HRDEM; ZSTD, LZMA) through Pillow's libtiff, the
tile wrapped in a one-strip TIFF of its own (_libtiff_decode). The geotransform
comes from ModelPixelScale + ModelTiepoint, the CRS from the GeoKeys (EPSG of the geographic or
projected type), nodata from GDAL's tag 42113. The full-resolution image (the first IFD) is read,
or one of the overviews after it (level); masks are skipped. A TileCache keeps decoded tiles.

Cog(remote) takes anything with .read(start, end, kind) and .size (rangeread.RemoteFile, or
rangeread.MemoryRange for tests): the header is read once (HEADER bytes, more if an IFD array
lies beyond), then read_window() fetches only the tiles under the window, neighbouring tiles of
one row in one read when they lie close together in the file (COGs store them in row order).
Missing tiles (offset 0, byte count 0: GDAL's sparse files) are nodata.
"""
import struct
import threading
import zlib
from collections import OrderedDict

import numpy as np

import fast

HEADER = 64 * 1024          # bytes read first: every IFD of the files in use fits in it
MERGE_GAP = 64 * 1024       # tiles closer than this in the file are read in one request
BATCH = 512                 # strips decoded by one libtiff call (_libtiff_many)

TYPES = {1: ("B", 1), 2: ("s", 1), 3: ("H", 2), 4: ("I", 4), 5: ("II", 8), 6: ("b", 1), 7: ("B", 1),
         8: ("h", 2), 9: ("i", 4), 10: ("ii", 8), 11: ("f", 4), 12: ("d", 8), 16: ("Q", 8), 17: ("q", 8),
         18: ("Q", 8)}
TAGS = {"width": 256, "height": 257, "bits": 258, "compression": 259, "photometric": 262,
        "samples": 277, "planar": 284, "predictor": 317, "tile_w": 322, "tile_h": 323,
        "tile_offsets": 324, "tile_counts": 325, "sample_format": 339, "subfile": 254,
        "pixel_scale": 33550, "tiepoint": 33922, "geokeys": 34735, "nodata": 42113}
COMPRESSIONS = {1: "none", 8: "deflate", 32946: "deflate", 32773: "packbits", 5: "libtiff", 34925: "libtiff",
                50000: "libtiff", 50001: "libtiff",     # LZW, LZMA, ZSTD, WebP: through Pillow's libtiff
                7: "jpeg"}                              # JPEG (orthophotos, YCbCr): Pillow's JPEG decoder
LAZY_TAGS = (324, 325, 273, 279)      # tile offsets / byte counts: loaded for the image in use only (a 1 m national mosaic's
                            # full-resolution tables are ~15 MB; its overviews' a few hundred kB)


class _Lazy:
    """A tag array not read yet: (file offset, TIFF type, count)."""
    __slots__ = ("off", "typ", "cnt")

    def __init__(self, off, typ, cnt):
        self.off, self.typ, self.cnt = off, typ, cnt


class CogError(ValueError):
    """Not a TIFF this reader supports (or a corrupt one)."""


def _dtype(bits, fmt, bo):
    kind = {1: "u", 2: "i", 3: "f"}.get(fmt)
    if kind is None or bits not in (8, 16, 32, 64) or (kind == "f" and bits < 32):
        raise CogError(f"unsupported sample format {fmt} / {bits} bits")
    return np.dtype(f"{bo}{kind}{bits // 8}")


def _packbits(data, size):
    out = bytearray()
    i, n = 0, len(data)
    while i < n and len(out) < size:
        c = data[i]
        i += 1
        if c < 128:
            out += data[i:i + c + 1]
            i += c + 1
        elif c > 128:
            out += bytes([data[i]]) * (257 - c)
            i += 1
    return bytes(out)


class TileCache:
    """Decoded tiles of any number of Cogs, least recently used out beyond max_bytes (thread-safe)."""

    def __init__(self, max_bytes):
        self.max_bytes, self.bytes = max_bytes, 0
        self._d = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            a = self._d.get(key)
            if a is not None:
                self._d.move_to_end(key)
            return a

    def put(self, key, a):
        with self._lock:
            old = self._d.pop(key, None)
            if old is not None:
                self.bytes -= old.nbytes
            self._d[key] = a
            self.bytes += a.nbytes
            while self.bytes > self.max_bytes and len(self._d) > 1:
                _, gone = self._d.popitem(last=False)
                self.bytes -= gone.nbytes


class Cog:
    def __init__(self, remote, kind="demand", level=0, tiles=None):
        """level: 0 the full-resolution image, n the n-th overview after it (its pixels as many times larger as the
        image is smaller; the georeferencing is the full image's, scaled). tiles: a TileCache that keeps decoded tiles (files read
        again and again, the polar DEM), or None."""
        self.remote, self.kind, self.level, self.tiles = remote, kind, level, tiles
        self._buf = remote.read(0, HEADER, kind)
        b = self._buf
        if b[:2] == b"II":
            self.bo = "<"
        elif b[:2] == b"MM":
            self.bo = ">"
        else:
            raise CogError("not a TIFF")
        magic = struct.unpack(self.bo + "H", b[2:4])[0]
        if magic == 42:
            self.big = False
            first = struct.unpack(self.bo + "I", b[4:8])[0]
        elif magic == 43:
            self.big = True
            first = struct.unpack(self.bo + "Q", b[8:16])[0]
        else:
            raise CogError(f"bad TIFF magic {magic}")
        self.tags = self._ifd(first)
        full = self.tags
        for _ in range(level):
            if not self._next:
                raise CogError(f"no overview {level}")
            self.tags = self._ifd(self._next)
        t = self.tags
        self.stripped = TAGS["tile_w"] not in t
        if self.stripped and 273 not in t:
            raise CogError("neither tiled nor stripped")
        self.bands = t.get(TAGS["samples"], (1,))[0]
        if self.bands not in (1, 3, 4) or (self.bands > 1 and t.get(TAGS["planar"], (1,))[0] != 1):
            raise CogError("only single-band and interleaved RGB(A) images are supported")
        self.photometric = t.get(TAGS["photometric"], (1,))[0]
        tables = t.get(347, full.get(347))
        self.jpeg_tables = bytes(tables) if tables else b""
        self.width, self.height = t[TAGS["width"]][0], t[TAGS["height"]][0]
        if self.stripped:       # a striped TIFF (LidarBC's DEMs): each strip a full-width "tile" of RowsPerStrip rows
            self.tile_w = self.width
            self.tile_h = min(self.height, t.get(278, (self.height,))[0])
        else:
            self.tile_w, self.tile_h = t[TAGS["tile_w"]][0], t[TAGS["tile_h"]][0]
        comp = t.get(TAGS["compression"], (1,))[0]
        if comp not in COMPRESSIONS:
            raise CogError(f"unsupported compression {comp}")
        self.compression = COMPRESSIONS[comp]
        self.predictor = t.get(TAGS["predictor"], (1,))[0]
        if self.predictor not in (1, 2, 3):
            raise CogError(f"unsupported predictor {self.predictor}")
        self.dtype = _dtype(t[TAGS["bits"]][0], t.get(TAGS["sample_format"], (1,))[0], self.bo)
        if self.bands > 1 and self.dtype != np.uint8:
            raise CogError("multi-band images are read as 8-bit only")
        self.offsets = self._array(t[273 if self.stripped else TAGS["tile_offsets"]])
        self.counts = self._array(t[279 if self.stripped else TAGS["tile_counts"]])
        self.compression_code = comp
        self.tiles_x = -(-self.width // self.tile_w)
        self.tiles_y = -(-self.height // self.tile_h)
        if len(self.offsets) < self.tiles_x * self.tiles_y:
            raise CogError("tile offset table too short")
        nd = t.get(TAGS["nodata"], full.get(TAGS["nodata"]))
        self.nodata = None
        if nd:
            s = nd if isinstance(nd, str) else ""
            s = s.strip("\x00 ")
            try:
                self.nodata = float(s)
            except ValueError:
                self.nodata = None
        # georeferencing: pixel (col, row) corner -> (x0 + col * sx, y0 - row * sy)
        ps, tp = full.get(TAGS["pixel_scale"]), full.get(TAGS["tiepoint"])
        if ps and tp:
            sx, sy = float(ps[0]), float(ps[1])
            self.x0 = float(tp[3]) - float(tp[0]) * sx
            self.y0 = float(tp[4]) + float(tp[1]) * sy
            self.sx = sx * full[TAGS["width"]][0] / self.width
            self.sy = sy * full[TAGS["height"]][0] / self.height
        else:
            self.sx = self.sy = self.x0 = self.y0 = None
        self.epsg = self._epsg(full.get(TAGS["geokeys"]))
        self.reads = 0

    # --- header parsing -----------------------------------------------------------------------
    def _bytes(self, off, n):
        if off + n > len(self._buf):
            if off + n <= HEADER * 64:      # extend the header buffer (tables past the first read)
                self._buf += self.remote.read(len(self._buf), max(off + n, len(self._buf) + HEADER), self.kind)
            else:
                return self.remote.read(off, off + n, self.kind)
        if off + n > len(self._buf):
            raise CogError("truncated TIFF header")
        return self._buf[off:off + n]

    def _ifd(self, off):
        bo = self.bo
        if self.big:
            n = struct.unpack(bo + "Q", self._bytes(off, 8))[0]
            p, esz, inl = off + 8, 20, 8
        else:
            n = struct.unpack(bo + "H", self._bytes(off, 2))[0]
            p, esz, inl = off + 2, 12, 4
        if n > 4096:
            raise CogError("implausible IFD")
        raw = self._bytes(p, n * esz)
        tags = {}
        for i in range(n):
            e = raw[i * esz:(i + 1) * esz]
            if self.big:
                tag, typ, cnt = struct.unpack(bo + "HHQ", e[:12])
                val = e[12:20]
            else:
                tag, typ, cnt = struct.unpack(bo + "HHI", e[:8])
                val = e[8:12]
            if typ not in TYPES:
                continue
            fmt, sz = TYPES[typ]
            total = sz * cnt
            if total <= inl:
                data = val[:total]
            else:
                o = struct.unpack(bo + ("Q" if self.big else "I"), val)[0]
                if tag in LAZY_TAGS and typ in (3, 4, 16):
                    tags[tag] = _Lazy(o, typ, cnt)
                    continue
                data = self._bytes(o, total)
            if typ == 2:
                tags[tag] = data.decode("latin-1")
            else:
                tags[tag] = struct.unpack(bo + fmt[0] * (cnt * len(fmt)), data)
        nxt = p + n * esz
        self._next = struct.unpack(bo + ("Q" if self.big else "I"), self._bytes(nxt, 8 if self.big else 4))[0]
        return tags

    def _array(self, v):
        """A tile table as int64 (a _Lazy one read now)."""
        if isinstance(v, _Lazy):
            fmt, sz = TYPES[v.typ]
            data = self._bytes(v.off, sz * v.cnt)
            return np.frombuffer(data, np.dtype(self.bo + fmt), count=v.cnt).astype(np.int64)
        return np.asarray(v, np.int64)

    @staticmethod
    def _epsg(keys):
        if not keys or len(keys) < 4:
            return None
        found = {}
        for i in range(keys[3]):
            k, loc, cnt, val = keys[4 + 4 * i:8 + 4 * i]
            if loc == 0:
                found[k] = val
        for k in (3072, 2048):              # ProjectedCSTypeGeoKey, GeographicTypeGeoKey
            if k in found and found[k] not in (0, 32767):
                return found[k]
        return None

    # --- pixels -------------------------------------------------------------------------------
    def _rows_of(self, i):
        """Rows block i holds in the file (a striped image's last strip may be short; tiles are always full)."""
        if not self.stripped:
            return self.tile_h
        return min(self.tile_h, self.height - i * self.tile_h)

    def _decode(self, data, i=None):
        """Block bytes -> a (tile_h, tile_w) array (a short last strip padded with nodata)."""
        rows = self.tile_h if i is None else self._rows_of(i)
        if rows != self.tile_h:
            a = self._decode_rows(data, rows)
            out = np.full((self.tile_h, self.tile_w) + a.shape[2:], self._fill(), a.dtype)
            out[:rows] = a
            return out
        return self._decode_rows(data, rows)

    def _decode_rows(self, data, rows):
        if self.compression == "jpeg":
            return self._jpeg_decode(data, rows)
        if self.bands > 1:
            if self.compression == "none":
                a = np.frombuffer(data, np.uint8, count=self.tile_w * rows * self.bands)
                return a.reshape(rows, self.tile_w, self.bands)
            return self._libtiff_decode(data, rows)
        if self.compression == "libtiff" or (self.predictor == 2 and self.dtype.kind == "f"):
            # (libtiff's predictor 2 on floats differences their 32-bit words, not the float values)
            return self._libtiff_decode(data, rows)
        if self.compression == "deflate":
            data = zlib.decompress(data)
        elif self.compression == "packbits":
            data = _packbits(data, self.tile_w * rows * self.dtype.itemsize)
        if self.predictor == 3:
            if rows != self.tile_h:
                raise CogError("a short strip with the float predictor")
            return self._float_predictor(data)
        a = np.frombuffer(data, self.dtype, count=self.tile_w * rows).reshape(rows, self.tile_w)
        if self.predictor == 2:
            a = np.cumsum(a, axis=1, dtype=a.dtype)
        return a.astype(self.dtype.newbyteorder("="), copy=False)

    PIL_MODES = {"f4": "F", "u1": "L", "u2": "I;16", "i2": "I;16S", "i4": "I"}

    def _jpeg_decode(self, data, rows):
        """A JPEG-compressed block: its abbreviated stream completed with the file's JPEGTables (their SOI .. tables,
        without the EOI, then the block without its SOI) and decoded by Pillow (YCbCr -> RGB)."""
        import io
        from PIL import Image
        if self.jpeg_tables and len(self.jpeg_tables) > 4 and data[:2] == b"\xff\xd8":
            data = self.jpeg_tables[:-2] + data[2:]
        try:
            im = Image.open(io.BytesIO(data))
            im.load()
        except Exception as e:
            raise CogError(f"JPEG block not decodable: {e}") from None
        a = np.asarray(im.convert("RGB") if self.bands >= 3 else im.convert("L"))
        h, w = min(rows, a.shape[0]), min(self.tile_w, a.shape[1])
        out = np.zeros((rows, self.tile_w) + ((3,) if self.bands >= 3 else ()), np.uint8)
        out[:h, :w] = a[:h, :w]
        return out

    def _libtiff_decode(self, data, rows=None):
        """A tile in a compression this module does not decode itself (LZW, ZSTD, ...): wrapped in a minimal one-strip
        TIFF of the file's byte order and decoded by Pillow's libtiff (which undoes the predictor too)."""
        return self._libtiff_many([data], self.tile_h if rows is None else rows)[0]

    def _libtiff_many(self, parts, last_rows):
        """Blocks (whole strips, or one tile) in one wrapper TIFF of len(parts) strips, decoded by one Pillow call:
        thousands of 1-row strips cost ~1 ms each one by one (6 s a canopy cell, 2026-10-23). The last part holds
        last_rows rows. Returns the decoded blocks (the last cut to its rows)."""
        import io
        from PIL import Image
        mode = self.PIL_MODES.get(self.dtype.kind + str(self.dtype.itemsize))
        if mode is None:
            raise CogError(f"no Pillow mode for {self.dtype}")
        bo, w, th, n = self.bo, self.tile_w, self.tile_h, len(parts)
        h = th * (n - 1) + last_rows
        fmt = {"f": 3, "u": 1, "i": 2}[self.dtype.kind]
        nb = self.bands
        counts = [len(p) for p in parts]
        # (tag, type, values): type 3 SHORT, 4 LONG; a list of more than fits in 4 bytes goes after the IFD
        entries = [(256, 4, [w]), (257, 4, [h]), (258, 3, [self.dtype.itemsize * 8] * nb),
                   (259, 3, [self.compression_code]), (262, 3, [2 if nb > 1 else 1]), (273, 4, [0] * n),
                   (277, 3, [nb]), (278, 4, [th if n > 1 else last_rows]), (279, 4, counts), (284, 3, [1]),
                   (317, 3, [self.predictor]), (339, 3, [fmt] * nb)]
        if nb == 4:
            entries.append((338, 3, [2]))                   # ExtraSamples: unassociated alpha
        size = {3: 2, 4: 4}
        ifd_len = 2 + 12 * len(entries) + 4
        extra_off = 8 + ifd_len
        extra_len = sum(size[t] * len(v) for _, t, v in entries if size[t] * len(v) > 4)
        data_off = extra_off + extra_len
        offs, o = [], data_off
        for c in counts:
            offs.append(o)
            o += c
        out = bytearray((b"II" if bo == "<" else b"MM") + struct.pack(bo + "HI", 42, 8))
        out += struct.pack(bo + "H", len(entries))
        extra = bytearray()
        for tag, typ, val in entries:
            if tag == 273:
                val = offs
            f = "H" if typ == 3 else "I"
            if size[typ] * len(val) > 4:
                out += struct.pack(bo + "HHII", tag, typ, len(val), extra_off + len(extra))
                extra += struct.pack(bo + f * len(val), *val)
            else:
                packed = struct.pack(bo + f * len(val), *val)
                out += struct.pack(bo + "HHI", tag, typ, len(val)) + packed + b"\0" * (4 - len(packed))
        out += struct.pack(bo + "I", 0)
        out += extra
        for p_ in parts:
            out += p_
        try:
            im = Image.open(io.BytesIO(bytes(out)))
            im.load()
        except Exception as e:      # (libtiff without the codec, a corrupt tile)
            raise CogError(f"tile not decodable (compression {self.compression_code}): {e}") from None
        a = np.asarray(im)
        if a.dtype != self.dtype.newbyteorder("="):
            a = a.astype(self.dtype.newbyteorder("="))
        a = a.reshape((h, w) + ((nb,) if nb > 1 else ()))
        return [a[k * th:min(h, (k + 1) * th)] for k in range(n)]

    def _float_predictor(self, data):
        """TIFF's floating-point predictor (3; the Copernicus DEM): each row holds its values' bytes as planes, most
        significant first whatever the file's byte order, differenced byte by byte along the row (earthfast)."""
        if self.dtype.kind != "f" or self.dtype.itemsize != 4:
            raise CogError("predictor 3 is read for float32 samples only")
        fast.require(56, "float_predictor_f32")
        return fast._ef.float_predictor_f32(data, self.tile_w, self.tile_h)

    def _fill(self):
        return self.nodata if self.nodata is not None else 0

    def read_window(self, col0, row0, w, h):
        """Pixels [row0, row0 + h) x [col0, col0 + w) as a (h, w) array; outside the image: nodata."""
        out = np.full((h, w) + ((min(self.bands, 3),) if self.bands > 1 else ()), self._fill(),
                      self.dtype.newbyteorder("="))
        c0, r0 = max(0, col0), max(0, row0)
        c1, r1 = min(self.width, col0 + w), min(self.height, row0 + h)
        if c1 <= c0 or r1 <= r0:
            return out
        tx0, tx1 = c0 // self.tile_w, (c1 - 1) // self.tile_w
        ty0, ty1 = r0 // self.tile_h, (r1 - 1) // self.tile_h
        def place(i, tile):
            ty, tx = divmod(i, self.tiles_x)
            x0, y0 = tx * self.tile_w, ty * self.tile_h
            a0, a1 = max(c0, x0), min(c1, x0 + self.tile_w)
            b0, b1 = max(r0, y0), min(r1, y0 + self.tile_h)
            out[b0 - row0:b1 - row0, a0 - col0:a1 - col0] = tile[b0 - y0:b1 - y0, a0 - x0:a1 - x0, ...][..., :3] \
                if self.bands > 1 else tile[b0 - y0:b1 - y0, a0 - x0:a1 - x0]

        for ty in range(ty0, ty1 + 1):
            idx = []
            for tx in range(tx0, tx1 + 1):
                i = ty * self.tiles_x + tx
                tile = self.tiles.get((self.remote, self.level, i)) if self.tiles is not None else None
                if tile is not None:
                    place(i, tile)
                else:
                    idx.append(i)
            for group in self._groups(idx):
                start = int(self.offsets[group[0]])
                end = max(int(self.offsets[i] + self.counts[i]) for i in group)
                blob = self.remote.read(start, end, self.kind)
                self.reads += 1
                for i in group:
                    o, n = int(self.offsets[i]) - start, int(self.counts[i])
                    tile = self._decode(blob[o:o + n], i)
                    if self.tiles is not None:      # (the part inside the image: an overview's tile is mostly padding)
                        ty_, tx_ = divmod(i, self.tiles_x)
                        tile = np.ascontiguousarray(tile[:min(self.tile_h, self.height - ty_ * self.tile_h),
                                                         :min(self.tile_w, self.width - tx_ * self.tile_w)])
                        self.tiles.put((self.remote, self.level, i), tile)
                    place(i, tile)
        return out

    def _blocks(self, idx):
        """Decoded blocks idx (a dict), only these fetched (neighbours in the file in one read)."""
        got, need = {}, []
        for i in idx:
            b = self.tiles.get((self.remote, self.level, i)) if self.tiles is not None else None
            if b is not None:
                got[i] = b
            else:
                need.append(i)
        batch = self.stripped and (self.compression == "libtiff"
                                   or (self.predictor == 2 and self.dtype.kind == "f" and self.compression != "jpeg"))
        for group in self._groups(need):
            start = int(self.offsets[group[0]])
            end = max(int(self.offsets[i] + self.counts[i]) for i in group)
            blob = self.remote.read(start, end, self.kind)
            self.reads += 1
            parts = [blob[int(self.offsets[i]) - start:int(self.offsets[i]) - start + int(self.counts[i])] for i in group]
            if batch and len(group) > 1:
                # full strips first, a short last strip of the image on its own
                full = [k for k, i in enumerate(group) if self._rows_of(i) == self.tile_h]
                dec = {}
                for c0 in range(0, len(full), BATCH):
                    ks = full[c0:c0 + BATCH]
                    for k, a in zip(ks, self._libtiff_many([parts[k] for k in ks], self.tile_h)):
                        dec[k] = a
                for k, i in enumerate(group):
                    b = dec[k] if k in dec else self._decode(parts[k], i)
                    if self.tiles is not None:
                        self.tiles.put((self.remote, self.level, i), b)
                    got[i] = b
                continue
            for k, i in enumerate(group):
                b = self._decode(parts[k], i)
                if self.tiles is not None:
                    self.tiles.put((self.remote, self.level, i), b)
                got[i] = b
        return got

    def sample_bilinear(self, col, row):
        """Bilinear values at fractional pixel-centre coordinates (col, row: pixel (c, r)'s centre at (c, r)), and
        whether each is valid (its 4 neighbours inside the image, present and not nodata). Only the blocks under the
        points are read: a coarse grid of points over a striped image reads only the strips of its rows. Vectorised:
        the blocks are stacked once and every corner gathered from the stack (millions of points per call)."""
        col = np.asarray(col, np.float64).ravel()
        row = np.asarray(row, np.float64).ravel()
        shape = np.shape(col)
        c0 = np.floor(col).astype(np.int64)
        r0 = np.floor(row).astype(np.int64)
        fu, fv = (col - c0).astype(np.float32), (row - r0).astype(np.float32)
        out = np.zeros(shape, np.float32)
        ok = (r0 >= 0) & (r0 + 1 < self.height) & (c0 >= 0) & (c0 + 1 < self.width)
        if not ok.any():
            return out, ok
        th, tw = self.tile_h, self.tile_w
        rr, cc = r0[ok], c0[ok]
        blks = []
        for dr, dc in ((0, 0), (0, 1), (1, 0), (1, 1)):
            blks.append(((rr + dr) // th) * self.tiles_x + (cc + dc) // tw)
        uniq = np.unique(np.concatenate(blks))
        present = (self.counts[uniq] > 0) & (self.offsets[uniq] > 0)
        blocks = self._blocks([int(b) for b in uniq[present]])
        fill = np.float32(np.nan)
        stack = np.full((len(uniq), th, tw), fill, np.float32)     # blocks with no data stay NaN
        for k, b in enumerate(uniq.tolist()):
            a = blocks.get(b)
            if a is not None:
                stack[k, :a.shape[0], :a.shape[1]] = a
        v = []
        for (dr, dc), blk in zip(((0, 0), (0, 1), (1, 0), (1, 1)), blks):
            pos = np.searchsorted(uniq, blk)
            r, c = rr + dr, cc + dc
            x = stack[pos, r - (blk // self.tiles_x) * th, c - (blk % self.tiles_x) * tw]
            if self.nodata is not None:
                x[x == np.float32(self.nodata)] = np.nan
            v.append(x)
        a00, a01, a10, a11 = v
        fu_, fv_ = fu[ok], fv[ok]
        h = (a00 * (1 - fu_) + a01 * fu_) * (1 - fv_) + (a10 * (1 - fu_) + a11 * fu_) * fv_
        good = np.isfinite(h)
        idx = np.nonzero(ok)[0]
        ok[idx[~good]] = False
        out[idx[good]] = h[good]
        return out, ok

    def sample_nearest(self, col, row):
        """Values of the pixels holding points (col, row: pixel corners at integers, as pixel_of) and their validity;
        only the blocks holding them are read (one strip per sampled row of a striped image)."""
        c = np.floor(np.asarray(col, np.float64).ravel()).astype(np.int64)
        r = np.floor(np.asarray(row, np.float64).ravel()).astype(np.int64)
        out = np.zeros(c.shape, np.float32)
        ok = (r >= 0) & (r < self.height) & (c >= 0) & (c < self.width)
        if not ok.any():
            return out, ok
        rr, cc = r[ok], c[ok]
        blk = (rr // self.tile_h) * self.tiles_x + cc // self.tile_w
        uniq = np.unique(blk)
        present = (self.counts[uniq] > 0) & (self.offsets[uniq] > 0)
        blocks = self._blocks([int(b) for b in uniq[present]])
        v = np.full(rr.shape, np.nan, np.float32)
        for b, a in blocks.items():
            m = blk == b
            ty, tx = divmod(b, self.tiles_x)
            v[m] = a[rr[m] - ty * self.tile_h, cc[m] - tx * self.tile_w]
        if self.nodata is not None:
            v[v == np.float32(self.nodata)] = np.nan
        good = np.isfinite(v)
        idx = np.nonzero(ok)[0]
        ok[idx[~good]] = False
        out[idx[good]] = v[good]
        return out, ok

    def _groups(self, idx):
        """Tile indices with data, split into runs that lie close together in the file."""
        have = sorted((i for i in idx if self.counts[i] > 0 and self.offsets[i] > 0),
                      key=lambda i: self.offsets[i])
        groups, cur, end = [], [], 0
        for i in have:
            o, e = int(self.offsets[i]), int(self.offsets[i] + self.counts[i])
            if cur and o - end > MERGE_GAP:
                groups.append(cur)
                cur = []
            end = e if not cur else max(end, e)
            cur.append(i)
        if cur:
            groups.append(cur)
        return groups

    def pixel_of(self, x, y):
        """Fractional (col, row) of CRS coordinates (x, y) (pixel corners at integers)."""
        return (np.asarray(x) - self.x0) / self.sx, (self.y0 - np.asarray(y)) / self.sy
