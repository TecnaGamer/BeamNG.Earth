"""Hong Kong's lidar terrain: CEDD's 2020 airborne LiDAR DTM (0.5 m bare earth), for the near terrain.

Why: Mapterhorn has nothing deeper than z12 over Hong Kong (~30 m a pixel; z13-z17 404 over Sha Tin), a surface
model of its towers and wooded hills. Against the DTM at three Sha Tin interchanges it stood a median 2.2-2.6 m
above the ground with a 6-9 m spread, and roads graded on it were p90 4.7-6.5 m off; the bridge decks measured from
the LiDAR (roadz "hk") then met ends pinned 3-5 m low (2026-11-10, hk-investigation/measurements).

Data: the DTM sheets of hklidar.py (750 x 600 m zips, HK1980 Grid, heights above HKPD), the ones the road heights
read: one folder, each sheet downloaded once. The sheet index is asked per INDEX_ZOOM tile and kept on disk.

    t = HkDtm(hklidar.at(cache_dir, fetcher))
    h, ok, short = t.tile(z, x, y)      # heights float32 [512, 512] on dem.py's grid, valid bool, short: see below

Never waited for: a sheet (or index) not on disk when a server run first asks for it is downloaded in the background
and read from the next run on; this run keeps Mapterhorn's heights there (short), in every process the same (the
first ask decides for the run: xproc.decide), and dem.py keeps nothing built on them (its mirror, revisit.fault).

Projection: HK1980 Grid (EPSG:2326), Transverse Mercator on the International 1924 ellipsoid, with the HK1980 to
WGS 84 datum shift (EPSG:1825, the Survey and Mapping Office's 7 parameters, coordinate frame rotation) as PROJ 9
applies it; within 0.4 mm of PROJ over the SAR (tests/test_hkdtm.py).

Vertical: HKPD lies about 1.23 m below mean sea level (Chart Datum is 0.146 m below HKPD; CEDD, the Observatory), and
Mapterhorn's heights are EGM2008's (Copernicus). At the Lands Department's 74 height-model control points (ITRF96
ellipsoidal and HKPD heights, version 1.0, 2006) an EGM2008 height is the HKPD height minus 0.42 m (median -0.424,
s.d. 0.039, -0.51 to -0.34; EGM2008 to degree 2190, gravity.py), which TO_EGM2008_M applies.

Its terms (DATA.GOV.HK, CSDI) allow redistribution with an indemnity and terms that may change without notice: road
plans made on it stay on this computer (roadz.hk_cell). BNE_HKDTM=off: Mapterhorn's heights, as before.
"""
import json
import math
import os
import threading

import numpy as np

import hklidar
import hrdem
import xproc

ON = os.environ.get("BNE_HKDTM", "on") != "off"
MIN_ZOOM = 15               # the near terrain (dem.FINE_MIN_ZOOM); the far rings keep Mapterhorn's
INDEX_ZOOM = 14             # the sheet index asked per tile of this zoom (~2.3 km, ~15 sheets)
TO_EGM2008_M = -0.42        # HKPD height to EGM2008 height (above)
TILE = 512

# HK1980 Grid (EPSG:2326) on the International 1924 ellipsoid
_A, _F = 6378388.0, 1 / 297.0
_E2 = _F * (2 - _F)
_E = math.sqrt(_E2)
_LAT0 = math.radians(22 + 18 / 60 + 43.68 / 3600)
_LON0 = math.radians(114 + 10 / 60 + 42.80 / 3600)
_FE, _FN = 836694.05, 819069.80
# WGS 84 to HK1980: the inverse of EPSG:1825 (HK1980 to WGS 84, coordinate frame: X' = T + (1 + s) R X)
_WA, _WF = 6378137.0, 1 / 298.257223563
_WE2 = _WF * (2 - _WF)
_T = np.array([-162.619, -276.959, -161.764])
_RX, _RY, _RZ = (math.radians(v / 3600) for v in (-0.067753, 2.243648, 1.158828))
_R_INV = np.linalg.inv((1 - 1.094246e-6) * np.array([[1, _RZ, -_RY], [-_RZ, 1, _RX], [_RY, -_RX, 1]]))
# Transverse Mercator by Krueger's series to n^4 (EPSG Guidance Note 7-2)
_N = _F / (2 - _F)
_B = _A / (1 + _N) * (1 + _N ** 2 / 4 + _N ** 4 / 64)
_H = (_N / 2 - 2 * _N ** 2 / 3 + 5 * _N ** 3 / 16 + 41 * _N ** 4 / 180,
      13 * _N ** 2 / 48 - 3 * _N ** 3 / 5 + 557 * _N ** 4 / 1440,
      61 * _N ** 3 / 240 - 103 * _N ** 4 / 140,
      49561 * _N ** 4 / 161280)


def _tm(phi, dlam):
    """(northing from the equator, easting from the central meridian), m, of HK1980 latitude phi, longitude dlam
    from the central meridian (radians)."""
    s = np.sin(phi)
    t = np.sinh(np.arctanh(s) - _E * np.arctanh(_E * s))
    xi = np.arctan2(t, np.cos(dlam))
    eta = np.arctanh(np.sin(dlam) / np.sqrt(1 + t * t))
    n, e = xi, eta
    for j, h in enumerate(_H, 1):
        n = n + h * np.sin(2 * j * xi) * np.cosh(2 * j * eta)
        e = e + h * np.cos(2 * j * xi) * np.sinh(2 * j * eta)
    return _B * n, _B * e


_M0 = float(_tm(np.float64(_LAT0), 0.0)[0])


def grid(lat, lon):
    """HK1980 Grid easting, northing (m) of WGS 84 lat / lon (degrees; arrays)."""
    phi, lam = np.radians(np.asarray(lat, np.float64)), np.radians(np.asarray(lon, np.float64))
    shape = np.broadcast(phi, lam).shape
    phi, lam = np.broadcast_to(phi, shape).ravel(), np.broadcast_to(lam, shape).ravel()
    nu = _WA / np.sqrt(1 - _WE2 * np.sin(phi) ** 2)
    xyz = np.stack([nu * np.cos(phi) * np.cos(lam), nu * np.cos(phi) * np.sin(lam), nu * (1 - _WE2) * np.sin(phi)])
    x, y, z = _R_INV @ (xyz - _T[:, None])
    lam = np.arctan2(y, x)
    p = np.hypot(x, y)
    phi = np.arctan2(z, p * (1 - _E2))
    for _ in range(4):
        nu = _A / np.sqrt(1 - _E2 * np.sin(phi) ** 2)
        phi = np.arctan2(z + _E2 * nu * np.sin(phi), p)
    n, e = _tm(phi, lam - _LON0)
    return (_FE + e).reshape(shape), (_FN + n - _M0).reshape(shape)


def covers(z, x, y):
    """Whether near-terrain tile (z, x, y) meets Hong Kong."""
    if z < MIN_ZOOM:
        return False
    s, w, n, e = hrdem.tile_bounds(z, x, y)
    S, W, N, E = hklidar.BOUNDS
    return s < N and n > S and w < E and e > W


def _box(lat, lon):
    """The HK1980 Grid box (x0, y0, x1, y1) round lat / lon points."""
    e, n = grid(lat, lon)
    return float(e.min()), float(n.min()), float(e.max()), float(n.max())


class HkDtm:
    def __init__(self, sheets):
        self.sheets = sheets
        self._lock = threading.Lock()
        self._run = None
        self._decided = {}              # what -> this run's decision (it never changes within a run)
        self._indexes = {}              # chunk -> [(name, url, box)] read from disk
        self._wanted = {}               # chunk -> grid boxes whose sheets to download once its index is had
        self.stats = {"tiles": 0, "short": 0}

    # --- this run's decisions ------------------------------------------------------------------------------------
    def _decide(self, what, now):
        """This run's decision for what: now (whether it is on disk) when this is the run's first ask of it, else the
        first ask's (every process of the run the same)."""
        with self._lock:
            got = self._decided.get(what)
        if got is not None:
            return got
        if self.sheets.root:
            if self._run is None:
                self._run = xproc.run_dir(os.path.join(self.sheets.root, "run"))
            got = bool(xproc.decide(os.path.join(self._run, what), bool(now)))
        else:
            got = bool(now)
        with self._lock:
            self._decided[what] = got
        return got

    # --- the index -----------------------------------------------------------------------------------------------
    def _index_path(self, chunk):
        return os.path.join(self.sheets.root, "index", "dtm", "%d_%d_%d.json" % chunk) if self.sheets.root else None

    @staticmethod
    def _read_index(p):
        try:
            with open(p, encoding="utf-8") as fh:
                return [(str(n), str(u), tuple(map(float, b))) for n, u, b in json.load(fh)
                        if hklidar.valid(str(n), str(u))]
        except (OSError, ValueError, TypeError):
            return None

    def _index(self, chunk):
        """The chunk's sheets [(name, url, box)] when this run reads its index (kept on disk), else None."""
        with self._lock:
            got = self._indexes.get(chunk)
        if got is not None:
            return got
        p = self._index_path(chunk)
        if p is None or not self._decide("index_%d_%d_%d" % chunk, os.path.exists(p)):
            return None
        got = self._read_index(p)
        if got is not None:
            with self._lock:
                self._indexes[chunk] = got
        return got

    def _want(self, chunk, box):
        """The sheets under grid box downloaded (later), the chunk's index first when it is not on disk."""
        p = self._index_path(chunk)
        with self._lock:                    # (with _fetch_index's write: a box is wanted or its sheets asked for)
            have = os.path.exists(p)
            if not have:
                self._wanted.setdefault(chunk, []).append(box)
        if have:
            self._fetch_sheets(self._read_index(p) or (), [box])
        else:
            self.sheets.later(("index", chunk), lambda: self._fetch_index(chunk))

    def _fetch_index(self, chunk):
        """The chunk's index from the net (kept), then its wanted boxes' sheets downloaded (later)."""
        p = self._index_path(chunk)
        got = self._read_index(p)
        if got is None:
            z, x, y = chunk
            s, w, n, e = hrdem.tile_bounds(z, x, y)
            got = self.sheets.index("dtm", _box(np.array([s, s, n, n]), np.array([w, e, w, e])), kind="predownload")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = f"{p}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump([[nm, u, list(b)] for nm, u, b in got], fh)
            with self._lock:
                os.replace(tmp, p)
        with self._lock:
            boxes = self._wanted.pop(chunk, [])
        self._fetch_sheets(got, boxes)

    def _fetch_sheets(self, index, boxes):
        for nm, u, b in index:
            if any(b[0] < q[2] and q[0] < b[2] and b[1] < q[3] and q[1] < b[3] for q in boxes):
                self._fetch_later(nm, u)

    def _fetch_later(self, name, url):
        p = self.sheets.path("dtm", name)

        def get():
            import netfetch
            try:
                self.sheets.get("dtm", name, url, kind="predownload")
            except netfetch.NotFound:
                open(p + ".404", "wb").close()      # (the index lists it, the host has none: no ground there)
        if p and not os.path.exists(p):
            self.sheets.later(("dtm", name), get)

    # --- tiles ---------------------------------------------------------------------------------------------------
    def _sheet(self, name, url):
        """The kept sheet when this run reads it; None and its download asked for when not; False: there is none."""
        p = self.sheets.path("dtm", name)
        if os.path.exists(p + ".404"):
            return False
        got = self.sheets.kept("dtm", name) if self._decide("dtm_" + name, os.path.exists(p)) else None
        if got is None:
            self._fetch_later(name, url)
        return got

    def tile(self, z, x, y, size=TILE):
        """(heights float32 [size, size] above EGM2008, valid bool, short) on dem.py's tile (z, x, y): short when this
        run goes without a sheet (or the index) it has, read from the next run on. No network: the downloads it asks
        for run in the background."""
        h = np.zeros((size, size), np.float32)
        ok = np.zeros((size, size), bool)
        if not covers(z, x, y) or not self.sheets.root:
            return h, ok, False
        lat, lon = hrdem.tile_lat_lon(z, x, y, size)
        ex, ny = grid(lat, lon)
        box = (float(ex.min()), float(ny.min()), float(ex.max()), float(ny.max()))
        d = z - INDEX_ZOOM
        chunk = (INDEX_ZOOM, x >> d, y >> d)
        idx = self._index(chunk)
        short = idx is None
        if short:
            self._want(chunk, box)
        taken = np.zeros((size, size), bool)
        for name, url, (x0, y0, x1, y1) in idx or ():
            if not (x0 < box[2] and box[0] < x1 and y0 < box[3] and box[1] < y1):
                continue
            m = (ex >= x0) & (ex < x1) & (ny >= y0) & (ny < y1) & ~taken
            if not m.any():
                continue
            taken |= m
            s = self._sheet(name, url)
            if s is None:
                short = True
            if not s:
                continue
            v = s.bilinear(ex[m], ny[m])
            good = np.isfinite(v)
            h[m] = np.where(good, v + TO_EGM2008_M, 0.0)
            ok[m] = good
        with self._lock:
            self.stats["tiles"] += 1
            self.stats["short"] += short
        return h, ok, short
