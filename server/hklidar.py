"""Hong Kong's 2020 airborne LiDAR (CEDD, from the CSDI Portal): the 0.5 m DTM and DSM sheets, shared by the road
heights (roadz.py source "hk") and the terrain (hkdtm.py).

A sheet is a 750 x 600 m zip holding one float32 GeoTIFF in HK1980 Grid (EPSG:2326), heights above the Hong Kong
Principal Datum (HKPD), -9999 where it has none (the sea). The index (an ArcGIS layer per product) names each sheet
and its URL. A zip cannot be read in parts, so each sheet is downloaded whole, once: one of the server's processes
fetches it (xproc), and every process reads the kept copy from <cache>/hklidar/<layer>/<name>.zip. The folder is
capped at SHEETS_MB, the least recently read sheets dropped first.

    sheets = at(cache_dir, fetcher)
    for name, url, box in sheets.index("dtm", (x0, y0, x1, y1)):
        z = sheets.get("dtm", name, url).sample(x, y)
"""
import contextlib
import io
import logging
import os
import queue
import re
import struct
import threading
import urllib.parse
import zipfile
from collections import OrderedDict

import numpy as np

log = logging.getLogger("earthd.hklidar")

BOUNDS = (22.13, 113.82, 22.57, 114.45)     # the Hong Kong SAR: lat0, lon0, lat1, lon1
INDEX_URL = ("https://portal.csdi.gov.hk/server/rest/services/common/cedd_rcd_1629267205233_87895/FeatureServer/"
             "{}/query")
LAYERS = {"dtm": 0, "dsm": 1}               # the index's layers: DTM_2020, DSM_2020
SHEET_HOST = "bulkdata.csdi.gov.hk"
PAGE = 2000                 # (the service's maxRecordCount: 3000)
PAGES = 20
FETCH_THREADS = 4           # sheets downloaded at once a process (bulkdata.csdi.gov.hk's policy: 4)
BACKGROUND_THREADS = 2      # of them for the downloads nothing waits for (later)
# (a Sha Tin place's road heights read 240 sheets, 1.3 GB; its terrain about 160 DTM sheets, a third of them the same:
# three such places went past 2 GB and dropped the road heights' DSM sheets)
SHEETS_MB = float(os.environ.get("BNE_HK_SHEETS_MB", "4096"))
MEM_SHEETS = 8              # decoded sheets kept in memory a process (~7 MB each)
NODATA = -9000.0            # the sheets' nodata is -9999


def inside(lat, lon):
    return BOUNDS[0] <= lat <= BOUNDS[2] and BOUNDS[1] <= lon <= BOUNDS[3]


def valid(name, url):
    """Is (name, url) a sheet of the index: a plain sheet name and an https URL on SHEET_HOST?"""
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return u.scheme == "https" and u.hostname == SHEET_HOST and bool(re.fullmatch(r"[0-9A-Z]{4,8}", name))


def arcgis(get_json, url, params):
    """The features of an ArcGIS REST query, every page (ordered by OBJECTID: the same pages each time)."""
    feats = []
    for page in range(PAGES):
        q = dict(params, f="json", resultOffset=page * PAGE, resultRecordCount=PAGE)
        d = get_json(f"{url}?{urllib.parse.urlencode(q, safe=',')}")
        if "error" in d:            # (ArcGIS answers its errors with a 200)
            raise ValueError(f"ArcGIS {url}: {d['error']}")
        feats += d.get("features") or []
        if not d.get("exceededTransferLimit"):
            return feats
    raise ValueError(f"ArcGIS {url}: over {PAGES} pages")


class Sheet:
    """One CEDD LiDAR sheet: a zip holding a classic little-endian GeoTIFF, one float32 band in uncompressed tiles,
    HK1980 Grid. sample(x, y): its heights at those points, NaN outside it or where it has none."""

    _TYPES = {3: ("H", 2), 4: ("I", 4), 12: ("d", 8)}

    def __init__(self, zipped):
        with zipfile.ZipFile(io.BytesIO(zipped)) as z:
            names = [n for n in z.namelist() if n.lower().endswith((".tif", ".tiff"))]
            if len(names) != 1:
                raise ValueError(f"a LiDAR sheet holds one TIFF, not {len(names)}")
            d = z.read(names[0])
        if d[:4] != b"II*\x00":
            raise ValueError("not a classic little-endian TIFF")
        ifd = struct.unpack_from("<I", d, 4)[0]
        tags = {}
        for k in range(struct.unpack_from("<H", d, ifd)[0]):
            e = ifd + 2 + 12 * k
            tag, typ, cnt = struct.unpack_from("<HHI", d, e)
            if typ in self._TYPES:
                fmt, size = self._TYPES[typ]
                at = e + 8 if cnt * size <= 4 else struct.unpack_from("<I", d, e + 8)[0]
                tags[tag] = struct.unpack_from(f"<{cnt}{fmt}", d, at)
        one = lambda t, default=None: tags[t][0] if t in tags else default
        if (one(259) != 1 or one(258) != 32 or one(339, 3) != 3 or one(277, 1) != 1
                or not {322, 323, 324, 33550, 33922} <= set(tags)):
            raise ValueError("a LiDAR sheet is one float32 band in uncompressed tiles, georeferenced")
        self.w, self.h, self.tw, self.th = one(256), one(257), one(322), one(323)
        self.across = -(-self.w // self.tw)
        self.offs = np.asarray(tags[324], np.int64)
        if len(self.offs) != self.across * -(-self.h // self.th) or self.offs.max() + self.tw * self.th * 4 > len(d):
            raise ValueError("a LiDAR sheet's tiles do not fit it")
        self.dx, self.dy = tags[33550][:2]
        tie = tags[33922]
        self.x0, self.y0 = tie[3] - tie[0] * self.dx, tie[4] + tie[1] * self.dy
        self.box = (self.x0, self.y0 - self.h * self.dy, self.x0 + self.w * self.dx, self.y0)
        self.raw = np.frombuffer(d, np.uint8)

    def _at(self, col, row):
        """The heights of pixels (col, row) (int arrays, inside the sheet), NaN where it has none."""
        at = (self.offs[(row // self.th) * self.across + col // self.tw]
              + ((row % self.th) * self.tw + col % self.tw) * 4)
        v = self.raw[at[..., None] + np.arange(4)].view("<f4")[..., 0].astype(np.float64)
        return np.where(v > NODATA, v, np.nan)

    def sample(self, x, y):
        x, y = np.asarray(x, np.float64), np.asarray(y, np.float64)
        out = np.full(x.shape, np.nan)
        col = np.floor((x - self.x0) / self.dx)
        row = np.floor((self.y0 - y) / self.dy)
        ok = (col >= 0) & (col < self.w) & (row >= 0) & (row < self.h)
        out[ok] = self._at(col[ok].astype(np.int64), row[ok].astype(np.int64))
        return out

    def bilinear(self, x, y):
        """Heights between the pixel centres at (x, y) (in the sheet; the half pixel round its edge extended from the
        last two), NaN where one of the four has none."""
        x, y = np.asarray(x, np.float64), np.asarray(y, np.float64)
        out = np.full(x.shape, np.nan)
        u = (x - self.x0) / self.dx - 0.5
        v = (self.y0 - y) / self.dy - 0.5
        ok = (u >= -0.5) & (u < self.w - 0.5) & (v >= -0.5) & (v < self.h - 0.5)
        u, v = u[ok], v[ok]
        c0 = np.clip(np.floor(u).astype(np.int64), 0, max(self.w - 2, 0))
        r0 = np.clip(np.floor(v).astype(np.int64), 0, max(self.h - 2, 0))
        fu, fv = u - c0, v - r0
        c1, r1 = np.minimum(c0 + 1, self.w - 1), np.minimum(r0 + 1, self.h - 1)
        out[ok] = ((self._at(c0, r0) * (1 - fu) + self._at(c1, r0) * fu) * (1 - fv)
                   + (self._at(c0, r1) * (1 - fu) + self._at(c1, r1) * fu) * fv)
        return out


class Sheets:
    """The sheets kept under root (None: in memory only) and fetched through fetcher (netfetch.Fetcher)."""

    def __init__(self, root, fetcher):
        self.root, self.fetcher = root, fetcher
        self._lock = threading.Lock()
        self._locks = {}                    # (layer, name) -> its download's lock in this process
        self._slots = threading.BoundedSemaphore(FETCH_THREADS)
        self._mem = OrderedDict()           # path -> Sheet, the most recently read last
        self._later = set()                 # keys of the background jobs queued or running
        self._queue = None
        self.stats = {"requests": 0, "bytes": 0}

    def path(self, layer, name):
        return os.path.join(self.root, layer, f"{name}.zip") if self.root else None

    def _fetch(self, url, kind):
        body = self.fetcher.get(url, kind=kind).body
        with self._lock:
            self.stats["requests"] += 1
            self.stats["bytes"] += len(body)
        return body

    def index(self, layer, box, kind="demand"):
        """[(name, url, (x0, y0, x1, y1))] of the layer's sheets meeting box (HK1980 Grid), by name."""
        import json
        x0, y0, x1, y1 = box
        feats = arcgis(lambda u: json.loads(self._fetch(u, kind).decode("utf-8")), INDEX_URL.format(LAYERS[layer]), {
            "where": "1=1", "geometry": f"{x0:.1f},{y0:.1f},{x1:.1f},{y1:.1f}", "geometryType": "esriGeometryEnvelope",
            "inSR": 2326, "spatialRel": "esriSpatialRelIntersects", "outSR": 2326, "outFields": "TILE_NAME,URL",
            "orderByFields": "TILE_NAME"})
        out = []
        for f in feats:
            a = f.get("attributes") or {}
            ring = np.asarray(((f.get("geometry") or {}).get("rings") or [[]])[0], np.float64).reshape(-1, 2)
            name, url = str(a.get("TILE_NAME") or ""), str(a.get("URL") or "")
            # (a name becomes a file name under root and in the run's decisions: only a sheet name, from the host)
            if len(ring) and valid(name, url):
                out.append((name, url, (*ring.min(0).tolist(), *ring.max(0).tolist())))
        return out

    def kept(self, layer, name):
        """The kept sheet (its use noted for trim), or None. No network."""
        p = self.path(layer, name)
        if p is None:
            return None
        with self._lock:
            got = self._mem.get(p)
            if got is not None:
                self._mem.move_to_end(p)
        if got is None:
            try:
                with open(p, "rb") as fh:
                    got = Sheet(fh.read())
            except FileNotFoundError:
                return None
            except (OSError, ValueError, zipfile.BadZipFile) as e:
                log.debug("hklidar sheet %s: %s", p, e)
                return None
            with self._lock:
                self._mem[p] = got
                while len(self._mem) > MEM_SHEETS:
                    self._mem.popitem(last=False)
        with contextlib.suppress(OSError):
            os.utime(p)
        return got

    def get(self, layer, name, url, kind="demand"):
        """The sheet: kept, or downloaded by one of the server's processes (xproc: the others read it when it is
        kept)."""
        if not valid(name, url):
            raise ValueError(f"LiDAR sheet {name!r}: {url!r}")
        u = urllib.parse.urlsplit(url)
        p = self.path(layer, name)
        with self._lock:
            lock = self._locks.setdefault((layer, name), threading.Lock())
        with lock:
            got = self.kept(layer, name)
            if got is not None:
                return got
            # (FETCH_THREADS downloads a process: the road cells built side by side (roadz.gap_answers) queued past
            # netfetch's max_wait for the host's request slots, and their builds failed)
            with self._slots:
                mine = False
                if p:
                    import xproc
                    got, mine = xproc.once(p + ".lock", lambda: self.kept(layer, name), xproc.STALE_S, kind)
                    if got is not None:
                        return got
                try:
                    body = self._fetch(urllib.parse.urlunsplit(u._replace(path=urllib.parse.quote(u.path))), kind)
                    got = Sheet(body)
                    if p:
                        import staging
                        os.makedirs(os.path.dirname(p), exist_ok=True)
                        tmp = f"{p}.{os.getpid()}.{threading.get_ident()}.tmp"
                        with open(tmp, "wb") as fh:
                            fh.write(body)
                        staging.replace_cached(tmp, p)
                finally:
                    if mine:
                        xproc.release(p + ".lock")
            if p:
                self.trim()
            return got

    def later(self, key, fn):
        """fn() run in the background, once at a time for key (a sheet's or index's download nothing waits for)."""
        with self._lock:
            if key in self._later:
                return
            self._later.add(key)
            if self._queue is None:
                self._queue = queue.Queue()
                for i in range(BACKGROUND_THREADS):     # (daemons: a server run that ends does not wait for them)
                    threading.Thread(target=self._background, name=f"hklidar-later-{i}", daemon=True).start()
        self._queue.put((key, fn))

    def _background(self):
        while True:
            key, fn = self._queue.get()
            try:
                fn()
            except Exception as e:          # (netfetch counts it; tried again when asked again)
                log.debug("hklidar later %s: %s: %s", key, type(e).__name__, e)
            finally:
                with self._lock:
                    self._later.discard(key)

    def trim(self):
        """The kept sheets cut back to SHEETS_MB, the least recently read first."""
        files = []
        for dp, _dn, fns in os.walk(self.root):
            for fn in fns:
                if fn.endswith(".zip"):
                    try:
                        st = os.stat(os.path.join(dp, fn))
                    except OSError:
                        continue
                    files.append((st.st_mtime, st.st_size, os.path.join(dp, fn)))
        total = sum(f[1] for f in files)
        for _t, size, path in sorted(files):
            if total <= SHEETS_MB * 1e6:
                break
            with contextlib.suppress(OSError):
                os.remove(path)
                total -= size
                with self._lock:
                    self._mem.pop(path, None)


_AT = {}
_AT_LOCK = threading.Lock()


def at(cache_dir, fetcher):
    """The process's Sheets of the server cache cache_dir (<cache_dir>/hklidar; None: in memory only), one for the
    road heights and the terrain both."""
    root = os.path.join(cache_dir, "hklidar") if cache_dir else None
    with _AT_LOCK:
        got = _AT.get((root, id(fetcher)))
        if got is None:
            got = _AT[(root, id(fetcher))] = Sheets(root, fetcher)
        return got
