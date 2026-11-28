#!/usr/bin/env python3
"""Probe P-A of the local weather plan (S1, ~/bnge-tests/localweather/PLAN_FINAL.md section 5): the observed snowline
from NOAA IMS 1 km against MODIS Terra and VIIRS NOAA-20 NDSI snow cover of the same day, per 0.25-degree cell and
aspect class (our DEM), over test places; then the S1 band tables (snow kernel + observed cover, earthfast snowfield)
against the same references.

All the work is in the Rust kernels (native/earthfast/src/snowobs.rs, snowfield.rs); this file is glue: downloads
(cached under --cache, rate-limited per host, each file once), arrays, and the statistics of the comparison.

    python3 tools/snowobs_probe.py fetch   [--cache DIR] [--dates ...]     IMS day files, GIBS tiles, DEM grids
    python3 tools/snowobs_probe.py lines   [--cache DIR] [--json OUT]      IMS vs MODIS / VIIRS lines per cell
    python3 tools/snowobs_probe.py tables  [--cache DIR] [--json OUT]      S1 tables vs MODIS (needs Open-Meteo once)

Sources: NOAA/NSIDC G02156 (IMS, no login), NASA GIBS (MODIS_Terra_NDSI_Snow_Cover, VIIRS_NOAA20_NDSI_Snow_Cover,
EPSG:4326 500m level 7), Mapterhorn / Copernicus DEM (server/dem.py), Open-Meteo's historical forecast (the snow
kernel's forcing, the same variables as the server: recorded once into the cache, re-runs need no network).
"""
import argparse
import datetime
import json
import math
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))
import fast  # noqa: E402

NATIVE = 645
UA = "BeamNG.Earth snow probe (https://github.com/TecnaGamer/BeamNG.Earth)"
IMS_URL = "https://noaadata.apps.nsidc.org/NOAA/G02156/GIS/1km/{y}/ims{y}{doy:03d}_1km_GIS_v1.3.tif.gz"
GIBS_URL = ("https://gibs.earthdata.nasa.gov/wmts/epsg4326/best/{layer}/default/{date}/500m/7/{row}/{col}.png")
CMAP_URL = "https://gibs.earthdata.nasa.gov/colormaps/v1.3/MODIS_NDSI_Snow_Cover.xml"
HF_API = "https://historical-forecast-api.open-meteo.com/v1/forecast"
LAYERS = {"modis": "MODIS_Terra_NDSI_Snow_Cover", "viirs": "VIIRS_NOAA20_NDSI_Snow_Cover"}
# per host: seconds between requests (well under each service's limits: GIBS's server policy is 8/s, NSIDC has none
# published, Open-Meteo 10 s apart as the server's background requests)
RATE_S = {"noaadata.apps.nsidc.org": 2.0, "gibs.earthdata.nasa.gov": 0.25, "historical-forecast-api.open-meteo.com": 10.0}

CELL = 0.25                 # the pack's forcing cell (PLAN_FINAL S1b)
DEM_RES = 0.001             # deg a DEM sample (~110 m)
DEM_ZOOM = 10
GIBS_DEG = 2.25             # a level-7 tile of the 500m matrix set
GIBS_PX = 512

# (name, lat, lon, kind): the plan's seven mountain places and its mild-winter cities
PLACES = (
    ("pikes-peak", 38.840, -105.044, "mountain"),
    ("mount-washington", 44.270, -71.303, "mountain"),
    ("zermatt", 46.020, 7.749, "mountain"),
    ("tioga-pass", 37.911, -119.258, "mountain"),
    ("rainier", 46.853, -121.760, "mountain"),
    ("furano", 43.342, 142.383, "mountain"),
    ("valdez", 61.130, -146.350, "mountain"),
    ("atlanta", 33.749, -84.388, "mild"),
    ("seattle", 47.606, -122.332, "mild"),
    ("tacoma", 47.253, -122.444, "mild"),
    ("london", 51.507, -0.128, "mild"),
    ("paris", 48.857, 2.352, "mild"),
    ("rome", 41.903, 12.496, "mild"),
    ("tokyo", 35.690, 139.692, "mild"),
    ("houston", 29.760, -95.370, "mild"),
)

# a reference line counts when the map saw most of the cell and the cover really changes inside it
VALID_MIN = 0.7             # share of the cell's land pixels seen clear
LINE_TOT_MIN = 30           # pixels in the column
LINE_ERR_MAX = 0.2          # misclassified share
TRANS = (0.05, 0.95)        # snow share of the column's pixels between these


def default_dates():
    out = []
    for a, b in (("2025-11-01", "2026-07-01"), ("2026-08-02", "2026-08-31"), ("2026-11-02", "2026-11-26")):
        d, e = datetime.date.fromisoformat(a), datetime.date.fromisoformat(b)
        while d <= e:
            out.append(d)
            d += datetime.timedelta(days=4)
    return out


# ---------------------------------------------------------------------------------------------------------------
# downloads (cached; one request at a time per host, spaced)

_last = {}
_lock = threading.Lock()


def get(url, path, binary=True, missing_ok=True):
    """The body of url, kept at path (a cached 404 is a .404 marker: asked once)."""
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()
    if os.path.exists(path + ".404"):
        return None
    host = urllib.parse.urlsplit(url).hostname
    with _lock:
        wait = _last.get(host, 0.0) + RATE_S.get(host, 1.0) - time.time()
        if wait > 0:
            time.sleep(wait)
        _last[host] = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
            break
        except urllib.error.HTTPError as e:
            if e.code == 404 and missing_ok:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                open(path + ".404", "w").close()
                return None
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(10 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < 3:
                time.sleep(10 * (attempt + 1))
                continue
            raise
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return data


def ims_file(cache, d):
    url = IMS_URL.format(y=d.year, doy=d.timetuple().tm_yday)
    return get(url, os.path.join(cache, "ims", os.path.basename(url)))


def colour_map(cache):
    """(rgb 0xRRGGBB list, data value list) of the NDSI layers' GIBS colour map."""
    body = get(CMAP_URL, os.path.join(cache, "gibs", "MODIS_NDSI_Snow_Cover.xml"), missing_ok=False)
    root = ET.fromstring(body)
    rgb, val = [], []
    for e in root.iter():
        if not e.tag.endswith("ColorMapEntry"):
            continue
        sv = (e.get("sourceValue") or "").strip("[]")
        r, g, b = (int(x) for x in e.get("rgb").split(","))
        if sv.isdigit() and int(sv) <= 255:
            rgb.append(r << 16 | g << 8 | b)
            val.append(int(sv))
    return rgb, val


# ---------------------------------------------------------------------------------------------------------------
# places, cells, DEM

def place_box(lat, lon):
    """(cells [(ci, cj)], south, west, north, east) of the 3 x 3 pack cells round a place."""
    ci, cj = int(math.floor((lon + 180.0) / CELL)), int(math.floor((90.0 - lat) / CELL))
    cells = [(ci + a, cj + b) for b in (-1, 0, 1) for a in (-1, 0, 1)]
    west, north = (ci - 1) * CELL - 180.0, 90.0 - (cj - 1) * CELL
    return cells, north - 3 * CELL, west, north, west + 3 * CELL


def cell_bounds(ci, cj):
    west, north = ci * CELL - 180.0, 90.0 - cj * CELL
    return north - CELL, west, north, west + CELL


def dem_grid(cache, name, lat, lon):
    """(heights float32 [rows, cols], north, west) over the place's box plus a margin (cached .npy)."""
    _cells, s, w, n, e = place_box(lat, lon)
    m = 0.02
    north, west = n + m, w - m
    rows, cols = int(round((n - s + 2 * m) / DEM_RES)), int(round((e - w + 2 * m) / DEM_RES))
    path = os.path.join(cache, "dem", f"{name}_{DEM_ZOOM}.npy")
    if os.path.exists(path):
        return np.load(path), north, west
    import dem as demmod
    d = demmod.Dem(os.path.join(cache, "dem", "tiles"))
    la = north - (np.arange(rows) + 0.5) * DEM_RES
    lo = west + (np.arange(cols) + 0.5) * DEM_RES
    h = d.sample(la[:, None], lo[None, :], DEM_ZOOM).astype(np.float32)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    np.save(path, h)
    return h, north, west


class Place:
    """A place's grids: its DEM, the ground under the IMS window and the GIBS mosaic (computed once)."""

    def __init__(self, cache, name, lat, lon, kind):
        self.name, self.lat, self.lon, self.kind = name, lat, lon, kind
        self.cells, self.s, self.w, self.n, self.e = place_box(lat, lon)
        self.dem, self.dn, self.dw = dem_grid(cache, name, lat, lon)
        ef = fast._ef
        # the IMS window: the pixels of the box's DEM samples, with a margin
        la = np.linspace(self.s, self.n, 61)
        lo = np.linspace(self.w, self.e, 61)
        r, c = ef.snowobs_ims_pixels(np.repeat(la, 61).astype(np.float64), np.tile(lo, 61).astype(np.float64))
        ok = r >= 0
        self.ims_box = None
        if ok.any():
            r0, r1, c0, c1 = int(r[ok].min()) - 2, int(r[ok].max()) + 2, int(c[ok].min()) - 2, int(c[ok].max()) + 2
            self.ims_box = (r0, r1, c0, c1)
            self.ims_ground = ef.snowobs_ground_ims(self.dem, self.dn, self.dw, DEM_RES, r0, c0, r1 - r0 + 1,
                                                    c1 - c0 + 1)
        # the GIBS tiles covering the box
        self.trow = range(int(math.floor((90.0 - self.n) / GIBS_DEG)), int(math.floor((90.0 - self.s) / GIBS_DEG)) + 1)
        self.tcol = range(int(math.floor((self.w + 180.0) / GIBS_DEG)),
                          int(math.floor((self.e + 180.0) / GIBS_DEG)) + 1)
        self.g_north = 90.0 - self.trow[0] * GIBS_DEG
        self.g_west = -180.0 + self.tcol[0] * GIBS_DEG
        self.g_rows, self.g_cols = len(self.trow) * GIBS_PX, len(self.tcol) * GIBS_PX
        self.gibs_ground = ef.snowobs_ground_grid(self.dem, self.dn, self.dw, DEM_RES, self.g_north, self.g_west,
                                                  GIBS_DEG / GIBS_PX, self.g_rows, self.g_cols)
        # each cell's ground range (DEM samples in it) and rows
        self.rows = {}
        for (ci, cj) in self.cells:
            s, w, n, e = cell_bounds(ci, cj)
            i0, i1 = int(round((self.dn - n) / DEM_RES)), int(round((self.dn - s) / DEM_RES))
            j0, j1 = int(round((w - self.dw) / DEM_RES)), int(round((e - self.dw) / DEM_RES))
            z = self.dem[i0:i1, j0:j1]
            z = z[np.isfinite(z)]
            land = z[z > 0.5] if (z > 0.5).any() else z
            zmin, zmax = int(np.floor(land.min())), int(np.ceil(land.max()))
            k0, nrows = ef.snowfield_rows(zmin, zmax)
            self.rows[(ci, cj)] = (zmin, zmax, k0, nrows)

    def gibs_values(self, cache, layer, date, cmap):
        """The mosaic's data values (uint8 [rows, cols]; 255 where a tile is missing)."""
        out = np.full((self.g_rows, self.g_cols), 255, np.uint8)
        for a, row in enumerate(self.trow):
            for b, col in enumerate(self.tcol):
                url = GIBS_URL.format(layer=layer, date=date.isoformat(), row=row, col=col)
                body = get(url, os.path.join(cache, "gibs", layer, date.isoformat(), f"{row}_{col}.png"))
                if body is None:
                    continue
                v = fast._ef.snowobs_gibs_values(body, cmap[0], [int(x) for x in cmap[1]])
                out[a * GIBS_PX:(a + 1) * GIBS_PX, b * GIBS_PX:(b + 1) * GIBS_PX] = v
        return out

    def counts(self, ground, obs, cell):
        """Band counts of a cell's pixels on its rows."""
        _zmin, _zmax, k0, n = self.rows[cell]
        s, w, nn, e = cell_bounds(*cell)
        box = tuple(int(round(v * 1e5)) for v in (s, w, nn, e))
        z, asp, la, lo, nd = ground
        return fast._ef.snowobs_cell_counts(z, asp, la, lo, nd, obs, box, k0, n), k0


def seen_share(ground, values, cell):
    """(land pixels seen clear / land pixels) of a GIBS mosaic in a cell: clear = NDSI 0..100; not land = inland water
    and ocean (237, 239); the rest (cloud, night, missing) not seen."""
    z, _asp, la, lo, nd = ground
    s, w, n, e = (int(round(v * 1e5)) for v in cell_bounds(*cell))
    m = (nd > 0) & (la >= s) & (la < n) & (lo >= w) & (lo < e)
    v = values.ravel()[m]
    land = (v != 237) & (v != 239)
    clear = v <= 100
    return float(clear.sum()) / max(1, int(land.sum()))


def line_of(counts, k0, col):
    l, err, tot = fast._ef.snowobs_snowline(counts, k0, col)
    snow = int(counts[:, 2 * col + 1].sum())
    return {"line": int(l), "err": int(err), "tot": int(tot), "share": snow / tot if tot else None}


def usable(ln):
    return (ln["tot"] >= LINE_TOT_MIN and ln["err"] <= LINE_ERR_MAX * ln["tot"] and ln["share"] is not None
            and TRANS[0] <= ln["share"] <= TRANS[1])


# ---------------------------------------------------------------------------------------------------------------
# commands

def cmd_fetch(a, places, dates):
    cmap = colour_map(a.cache)
    for d in dates:
        ims_file(a.cache, d)
        for p in places:
            for layer in LAYERS.values():
                p.gibs_values(a.cache, layer, d, cmap)
        print("fetched", d, flush=True)


def ims_obs_for(a, places, d):
    """{place name: IMS observation classes over its window} for day d (None: no file)."""
    body = ims_file(a.cache, d)
    if body is None:
        return None
    ps = [p for p in places if p.ims_box is not None]
    wins = fast._ef.snowobs_ims_windows(body, [p.ims_box for p in ps])
    return {p.name: fast._ef.snowobs_classes(w.ravel(), "ims") for p, w in zip(ps, wins)}


def cmd_lines(a, places, dates):
    cmap = colour_map(a.cache)
    rows = []
    for d in dates:
        ims = ims_obs_for(a, places, d)
        for p in places:
            gv = {k: p.gibs_values(a.cache, layer, d, cmap) for k, layer in LAYERS.items()}
            go = {k: fast._ef.snowobs_classes(v.ravel(), "ndsi") for k, v in gv.items()}
            for cell in p.cells:
                rec = {"place": p.name, "kind": p.kind, "date": d.isoformat(), "cell": list(cell),
                       "zmin": p.rows[cell][0], "zmax": p.rows[cell][1]}
                if ims is not None and p.name in ims:
                    c, k0 = p.counts(p.ims_ground, ims[p.name], cell)
                    rec["ims"] = [line_of(c, k0, col) for col in (0, 1)]
                    rec["ims_bands"] = c.tolist()
                    rec["k0"] = k0
                for k in LAYERS:
                    c, k0 = p.counts(p.gibs_ground, go[k], cell)
                    rec[k] = [line_of(c, k0, col) for col in (0, 1)]
                    rec[k + "_seen"] = round(seen_share(p.gibs_ground, gv[k], cell), 3)
                    rec[k + "_bands"] = c.tolist()
                rows.append(rec)
        print("lines", d, flush=True)
    if a.json:
        with open(a.json, "w") as f:
            json.dump(rows, f)
    summarise_lines(rows)
    return rows


def _stats(deltas):
    if not deltas:
        return "n 0"
    d = np.array(deltas, float)
    return (f"n {len(d):4d}  median |d| {np.median(np.abs(d)):5.0f} m  mean d {d.mean():+6.0f} m  "
            f"within 150 m {100 * np.mean(np.abs(d) <= 150):4.0f} %  within 300 m {100 * np.mean(np.abs(d) <= 300):4.0f} %")


def summarise_lines(rows):
    """IMS - MODIS and VIIRS - MODIS line differences where MODIS saw the cell and has a real line in it; band share
    agreement; the mild cities' snow days."""
    by = {}
    for r in rows:
        if r["kind"] != "mountain" or "ims" not in r or r["modis_seen"] < VALID_MIN:
            continue
        for col in (0, 1):
            m = r["modis"][col]
            if not usable(m):
                continue
            by.setdefault(r["place"], {"ims": [], "viirs": []})["ims"].append(r["ims"][col]["line"] - m["line"])
            v = r["viirs"][col]
            if r["viirs_seen"] >= VALID_MIN and usable(v):
                by[r["place"]]["viirs"].append(v["line"] - m["line"])
    print("\nObserved snowline, IMS (1 km) - MODIS Terra (500 m), per cell and aspect column, same day")
    allv, alli = [], []
    for name, d in sorted(by.items()):
        print(f"  {name:18s} IMS   {_stats(d['ims'])}")
        print(f"  {'':18s} VIIRS {_stats(d['viirs'])}")
        alli += d["ims"]
        allv += d["viirs"]
    print(f"  {'ALL':18s} IMS   {_stats(alli)}")
    print(f"  {'':18s} VIIRS {_stats(allv)}")
    # band agreement: rows with >= 5 pixels in both
    agree, tot, absd = 0, 0, []
    for r in rows:
        if "ims" not in r or r["modis_seen"] < VALID_MIN:
            continue
        for bi, bm in zip(r["ims_bands"], r["modis_bands"]):
            for col in (0, 1):
                ni, nm = bi[2 * col] + bi[2 * col + 1], bm[2 * col] + bm[2 * col + 1]
                if ni < 5 or nm < 5:
                    continue
                si, sm = bi[2 * col + 1] / ni, bm[2 * col + 1] / nm
                tot += 1
                agree += (si >= 0.5) == (sm >= 0.5)
                absd.append(abs(si - sm))
    if tot:
        print(f"\nBand snow share (rows >= 5 px in both): IMS vs MODIS majority agree {100 * agree / tot:.1f} % of {tot},"
              f" mean |share diff| {np.mean(absd):.3f}")
    # the mild cities: their own cell's snow share by IMS and MODIS
    print("\nMild-winter cities: dates with snow in the city's own cell (IMS share / MODIS share when seen)")
    for r in rows:
        if r["kind"] != "mild" or r["cell"] != list(place_cell(r["place"])):
            continue
        i = r.get("ims")
        si = sum(x[1] + x[3] for x in r.get("ims_bands", [])) / max(1, sum(sum(x) for x in r.get("ims_bands", [])))
        sm = sum(x[1] + x[3] for x in r["modis_bands"]) / max(1, sum(sum(x) for x in r["modis_bands"]))
        if (i and si > 0.02) or (r["modis_seen"] >= VALID_MIN and sm > 0.02):
            print(f"  {r['place']:10s} {r['date']}  IMS {si:5.2f}  MODIS {sm:5.2f} (seen {r['modis_seen']:.2f})")


def place_cell(name):
    p = next(x for x in PLACES if x[0] == name)
    return int(math.floor((p[2] + 180.0) / CELL)), int(math.floor((90.0 - p[1]) / CELL))


# ---------------------------------------------------------------------------------------------------------------
# the S1 tables against the references

def forcing(cache, p, d):
    """Open-Meteo's historical forecast for the place's 9 cell centres, 10 days to day d (recorded once)."""
    import weather
    lats, lons = [], []
    for (ci, cj) in p.cells:
        s, w, n, e = cell_bounds(ci, cj)
        lats.append(f"{(s + n) / 2:.4f}")
        lons.append(f"{(w + e) / 2:.4f}")
    q = {"latitude": ",".join(lats), "longitude": ",".join(lons),
         "start_date": (d - datetime.timedelta(days=10)).isoformat(), "end_date": d.isoformat(),
         "hourly": ",".join(weather.SNOW_HOURLY), "wind_speed_unit": "ms", "timeformat": "unixtime",
         "timezone": "auto", "cell_selection": "land"}
    path = os.path.join(cache, "om", f"{p.name}_{d.isoformat()}.json")
    body = get(HF_API + "?" + urllib.parse.urlencode(q, safe=","), path, missing_ok=False)
    out = json.loads(body.decode("utf-8"))
    return out if isinstance(out, list) else [out]


def old_line(rows, col_cover=1):
    """Today's W2 table (rows (z, cover, bin, age) at the cell's BAND_DZ, held past the ends): the height where the
    cover first reaches 4 and stays (None: never; -1e9: everywhere)."""
    rs = sorted(rows)
    above = [r[col_cover] >= 4 for r in rs]
    if all(above):
        return -10 ** 9
    for i in range(len(rs) - 1, -1, -1):
        if not above[i]:
            return None if i == len(rs) - 1 else (rs[i][0] + rs[i + 1][0]) // 2
    return None


def cover_at(k_lo, rows, z, col):
    """The table's cover (0..8) at height z for a column (0 pole, 1 equator, 2 the mean), linear between rows,
    held past the ends (as the look reads it)."""
    zs = np.array([r[0] for r in rows], float)
    if col == 2:
        cv = np.array([(r[1] + r[3]) / 2.0 for r in rows], float)
    else:
        cv = np.array([r[1 + 2 * col] for r in rows], float)
    return np.interp(z, zs, cv)


def pixel_scores(p, ground, obs, cell, k_lo, rows):
    """(false white, false bare, accuracy) of a table against a map's clear land pixels in a cell: predicted snow at a
    cover of 4 or more at the pixel's height and aspect."""
    z, asp, la, lo, nd = ground
    s, w, n, e = (int(round(v * 1e5)) for v in cell_bounds(*cell))
    m = (nd > 0) & (la >= s) & (la < n) & (lo >= w) & (lo < e) & (obs > 0)
    if not m.any():
        return None
    zz, aa, oo = z[m].astype(float), asp[m], obs[m]
    pred = np.where(aa < 0, cover_at(k_lo, rows, zz, 0), np.where(aa > 0, cover_at(k_lo, rows, zz, 1),
                                                                      cover_at(k_lo, rows, zz, 2))) >= 4
    snow = oo == 2
    fw = float(np.mean(pred & ~snow))
    fb = float(np.mean(~pred & snow))
    return {"false_white": fw, "false_bare": fb, "acc": 1.0 - fw - fb, "n": int(m.sum()), "obs_snow": float(snow.mean())}


def pick_dates(lines_json, places, per_place, mild_dates):
    """Per mountain place the dates with the most usable MODIS lines in its cells (8 days apart at least)."""
    with open(lines_json) as f:
        rows = json.load(f)
    score = {}
    for r in rows:
        if r["kind"] != "mountain" or r["modis_seen"] < VALID_MIN:
            continue
        k = (r["place"], r["date"])
        score[k] = score.get(k, 0) + sum(usable(r["modis"][c]) for c in (0, 1))
    out = {}
    for p in places:
        if p.kind != "mountain":
            out[p.name] = [datetime.date.fromisoformat(x) for x in mild_dates]
            continue
        cand = sorted(((v, d) for (n, d), v in score.items() if n == p.name and v > 0), reverse=True)
        picked = []
        for _v, d in cand:
            dd = datetime.date.fromisoformat(d)
            if all(abs((dd - x).days) >= 8 for x in picked):
                picked.append(dd)
            if len(picked) >= per_place:
                break
        out[p.name] = sorted(picked)
    return out


# The fusion rules the tables command compares (snowfield.rs Fusion: bare_max, snow_min permille, two_day, fresh_snow
# 0.01 mm): A the plan's (bare at most 25 % two days running, white from 50 %), B symmetric at 50 % two days, C symmetric
# one day, D one day and white only from 75 %
# (the observed rows' shift 0 here: the variants' "+150" shift them themselves; E with +150 is snowfield.rs's default)
FUSIONS = {"A": (250, 500, True, 100, 0, 2000), "D": (499, 750, False, 100, 0, 2000),
           "E": (499, 750, False, 500, 0, 2000), "F": (499, 750, False, 10 ** 9, 0, 2000),
           "G": (499, 750, True, 500, 0, 2000)}
# The clear-satellite rule's sweep (snowfield.rs Fusion.clear_gap): the latest clear MODIS / VIIRS day (the 1-3 days
# before the reference day, MODIS's cloud mask: at least VALID_MIN of the cell seen) at an NDSI threshold, its share
# standing for IMS's where it is at least the gap lower; the rest of the rule snowfield.rs's default
CLEAR_RULES = {"clear35@400": (35, 400), "clear35@250": (35, 250), "clear20@400": (20, 400), "clear20@250": (20, 250),
               "clear10@400": (10, 400)}


def cmd_tables(a, places, _dates):
    import snow as snowmod
    ef = fast._ef
    cmap = colour_map(a.cache)
    chosen = pick_dates(a.lines, places, a.per_place, a.mild_dates)
    for p in places:
        if p.kind == "mountain":
            for x in a.extra_dates:
                dx = datetime.date.fromisoformat(x)
                if dx not in chosen[p.name]:
                    chosen[p.name].append(dx)
    res = []
    for p in places:
        for d in chosen.get(p.name, []):
            oms = forcing(a.cache, p, d)
            ims = {k: ims_obs_for(a, [p], d - datetime.timedelta(days=k)) for k in (0, 1, 2)}
            gv = p.gibs_values(a.cache, LAYERS["modis"], d, cmap)
            mo = ef.snowobs_classes(gv.ravel(), "ndsi")
            # Sentinel-2 of the day (HLS, MODIS-clear pixels), when it passed
            s2 = hls_obs(a.cache, p, d, gv) if p.kind == "mountain" else None
            # VIIRS of the two days before (a live source: never the reference day itself)
            vo = {k: ef.snowobs_classes(p.gibs_values(a.cache, LAYERS["viirs"], d - datetime.timedelta(days=k),
                                                      cmap).ravel(), "ndsi") for k in (1, 2)}
            # the clear days before (VIIRS then MODIS, newest first): raw values, for the clear-satellite rule
            clear_days = [(k, lay, p.gibs_values(a.cache, LAYERS[lay], d - datetime.timedelta(days=k), cmap))
                          for k in (1, 2, 3) for lay in ("viirs", "modis")]
            # the region's seed: every cell's model ground and its first hour's model snow depth (mm)
            parts = [snowmod.records(om) for om in oms]
            seed = [(int(z), int(rec[0, 9]) if rec.shape[0] and rec[0, 9] != snowmod.MISSING else snowmod.MISSING)
                    for rec, _t, z, _o in parts]
            hour = int(round(11.0 - p.lon / 15.0)) % 24
            t_ref = int(datetime.datetime(d.year, d.month, d.day, hour, tzinfo=datetime.timezone.utc).timestamp())
            for cell, (recs, times, z_cell, _off) in zip(p.cells, parts):
                if not times:
                    continue
                upto = snowmod.hour_index(times, t_ref)
                zmin, zmax, k0, nrows = p.rows[cell]
                lat_e2 = int(round((cell_bounds(*cell)[0] + CELL / 2) * 100))
                obs = {}
                for k in (0, 1, 2):
                    if ims[k] is not None and p.name in ims[k]:
                        c, _k = p.counts(p.ims_ground, ims[k][p.name], cell)
                        obs[k] = c.astype(np.int64)
                vc = {k: p.counts(p.gibs_ground, vo[k], cell)[0].astype(np.int64) for k in (1, 2)}
                # (k0 shifts: the observed bands read one or two rows higher, a bias correction of +150 / +300 m;
                # fusions: FUSIONS' rules)
                variants = {"kernel": (None, None, 0, None, None),
                            "default": (obs.get(1), obs.get(2), 0, (499, 750, False, 500, 1, 2000), None)}
                if a.rule_sweep:
                    for fname, fus in FUSIONS.items():
                        variants[f"ims@{fname}"] = (obs.get(1), obs.get(2), 0, fus, None)
                        variants[f"ims+150@{fname}"] = (obs.get(1), obs.get(2), 1, fus, None)
                pick = next(((k, lay, v) for k, lay, v in clear_days
                             if seen_share(p.gibs_ground, v, cell) >= VALID_MIN), None)
                for cname, (thr, gap) in CLEAR_RULES.items():
                    if pick is None:
                        continue
                    cc = p.counts(p.gibs_ground, ef.snowobs_classes(pick[2].ravel(), "ndsi", thr), cell)[0]
                    variants[cname] = (obs.get(1), obs.get(2), 0, (499, 750, False, 500, 1, gap), cc.astype(np.int64))
                mc, mk0 = p.counts(p.gibs_ground, mo, cell)
                seen = seen_share(p.gibs_ground, gv, cell)
                ml = [line_of(mc, mk0, col) for col in (0, 1)]
                rec = {"place": p.name, "kind": p.kind, "date": d.isoformat(), "cell": list(cell), "z_cell": z_cell,
                       "zmin": zmin, "zmax": zmax, "modis_seen": round(seen, 3), "modis": ml, "v": {}}
                # today's W2 table (snow.rs BAND_DZ round the model cell, the climatic snowline not included)
                raw = {}
                snowmod.message(recs, upto, times[upto], 0.0, 0.0, z_cell, z_cell, 2, raw=raw)
                w2rows, _road, _bins = snowmod.w2_table(raw, [-1] * 8)
                rec["old_line"] = old_line(w2rows)
                if s2 is not None:
                    sc_, sk0 = p.counts(s2[0], s2[1], cell)
                    rec["s2"] = [line_of(sc_, sk0, col) for col in (0, 1)]
                rec["clear_day"] = None if pick is None else [pick[0], pick[1]]
                for name, (t, b, sh, fus, cc) in variants.items():
                    if name != "kernel" and t is None and cc is None:
                        continue
                    kl, rows, _states, runs = ef.snowfield_table(recs, upto, z_cell, lat_e2, zmin, zmax, seed,
                                                                 t, k0 + sh, b, k0 + sh, fusion=fus, clear=cc,
                                                                 k0_clear=k0)
                    lines = [ef.snowfield_line(kl, rows, col) for col in (0, 1)]
                    v = {"lines": [int(x[0]) for x in lines], "rows": rows}
                    if seen >= VALID_MIN:
                        v["px"] = pixel_scores(p, p.gibs_ground, mo, cell, kl, rows)
                    rec["v"][name] = v
                res.append(rec)
            print("tables", p.name, d, flush=True)
    if a.json:
        with open(a.json, "w") as f:
            json.dump(res, f)
    summarise_tables(res)


def per_place(res, names):
    """Each place's line error (the table's line - the reference's) vs MODIS and vs Sentinel-2, per rule."""
    for ref in ("modis", "s2"):
        print(f"   per place vs {ref.upper()} (median |d| m / mean d m / within 150 m %, n)")
        places = sorted({r["place"] for r in res if r["kind"] == "mountain"})
        for pl in places:
            cells = []
            for n in names:
                d = []
                for r in res:
                    if r["place"] != pl or ref not in r or (ref == "modis" and r["modis_seen"] < VALID_MIN):
                        continue
                    for col in (0, 1):
                        m = r[ref][col]
                        if usable(m) and n in r["v"]:
                            d.append(r["v"][n]["lines"][col] - m["line"])
                if d:
                    d = np.array(d, float)
                    cells.append(f"{n}: {np.median(np.abs(d)):4.0f}/{d.mean():+5.0f}/{100 * np.mean(np.abs(d) <= 150):3.0f}%"
                                 f" n{len(d)}")
            if cells:
                print(f"     {pl:16s} " + " | ".join(cells))


def summarise_tables(res):
    print("\nS1 tables against MODIS Terra, same day (cells MODIS saw clear)")
    names = tuple(dict.fromkeys(n for r in res for n in r["v"]))
    for kind in ("mountain", "mild"):
        line_d = {n: [] for n in names + ("old",)}
        px = {n: [] for n in names}
        for r in res:
            if r["kind"] != kind or r["modis_seen"] < VALID_MIN:
                continue
            for col in (0, 1):
                m = r["modis"][col]
                if not usable(m):
                    continue
                for n in names:
                    if n in r["v"]:
                        line_d[n].append(r["v"][n]["lines"][col] - m["line"])
                if r["old_line"] is not None and r["old_line"] > -10 ** 8:
                    line_d["old"].append(r["old_line"] - m["line"])
            for n in names:
                if n in r["v"] and r["v"][n].get("px"):
                    px[n].append(r["v"][n]["px"])
        s2_d = {n: [] for n in names + ("old",)}
        for r in res:
            if r["kind"] != kind or "s2" not in r:
                continue
            for col in (0, 1):
                m = r["s2"][col]
                if not usable(m):
                    continue
                for n in names:
                    if n in r["v"]:
                        s2_d[n].append(r["v"][n]["lines"][col] - m["line"])
                if r["old_line"] is not None and r["old_line"] > -10 ** 8:
                    s2_d["old"].append(r["old_line"] - m["line"])
        print(f" {kind}:")
        for n in ("old",) + names:
            print(f"   line {n:12s} vs MODIS {_stats(line_d[n])}")
        if kind == "mountain":
            per_place(res, ("default",) + tuple(x for x in names if x.startswith("clear")))
        for n in ("old",) + names:
            if s2_d[n]:
                print(f"   line {n:12s} vs S2    {_stats(s2_d[n])}")
        for n in names:
            if px[n]:
                w = np.array([x["n"] for x in px[n]], float)
                fw = np.average([x["false_white"] for x in px[n]], weights=w)
                fb = np.average([x["false_bare"] for x in px[n]], weights=w)
                print(f"   pixels {n:12s} false white {100 * fw:5.1f} %  false bare {100 * fb:5.1f} %  "
                      f"accuracy {100 * (1 - fw - fb):5.1f} %  ({len(px[n])} cells)")


# ---------------------------------------------------------------------------------------------------------------
# Sentinel-2 (HLS S30 through GIBS, level 9 of the 31.25m set: ~120 m pixels): the finest reference

HLS_LAYER = "HLS_S30_Nadir_BRDF_Adjusted_Reflectance"
HLS_URL = "https://gibs.earthdata.nasa.gov/wmts/epsg4326/best/" + HLS_LAYER + "/default/{date}/31.25m/9/{row}/{col}.png"
HLS_DEG = 288.0 / 2 ** 9
HLS_SNOW_MIN = 150          # a clear HLS pixel is snow when its darkest channel reaches this ...
HLS_SNOW_SPREAD = 45        # ... and its channels are this close (white, blue-white)


def hls_obs(cache, p, d, modis_vals):
    """(ground, obs classes) of the HLS true-colour mosaic over the place's box on day d: snow by brightness, only where
    MODIS saw the ground clear the same day (Terra and Sentinel-2 pass within the hour: MODIS's cloud mask), else None
    (no Sentinel-2 pass that day)."""
    from PIL import Image
    import io
    rows = range(int(math.floor((90.0 - p.n) / HLS_DEG)), int(math.floor((90.0 - p.s) / HLS_DEG)) + 1)
    cols = range(int(math.floor((p.w + 180.0) / HLS_DEG)), int(math.floor((p.e + 180.0) / HLS_DEG)) + 1)
    rgba = np.zeros((len(rows) * 512, len(cols) * 512, 4), np.uint8)
    for a, r in enumerate(rows):
        for b, c in enumerate(cols):
            body = get(HLS_URL.format(date=d.isoformat(), row=r, col=c),
                       os.path.join(cache, "gibs", HLS_LAYER, d.isoformat(), f"{r}_{c}.png"))
            if body is None:
                continue
            rgba[a * 512:(a + 1) * 512, b * 512:(b + 1) * 512] = np.asarray(Image.open(io.BytesIO(body)).convert("RGBA"))
    if not (rgba[..., 3] > 0).any():
        return None
    g_north, g_west, g_res = 90.0 - rows[0] * HLS_DEG, -180.0 + cols[0] * HLS_DEG, HLS_DEG / 512
    # MODIS clear at each HLS pixel
    yy = g_north - (np.arange(rgba.shape[0]) + 0.5) * g_res
    xx = g_west + (np.arange(rgba.shape[1]) + 0.5) * g_res
    mr = np.clip(((p.g_north - yy) / (GIBS_DEG / GIBS_PX)).astype(int), 0, p.g_rows - 1)
    mc = np.clip(((xx - p.g_west) / (GIBS_DEG / GIBS_PX)).astype(int), 0, p.g_cols - 1)
    clear = modis_vals[mr[:, None], mc[None, :]] <= 100
    lo, hi = rgba[..., :3].min(-1).astype(int), rgba[..., :3].max(-1).astype(int)
    snow = (lo >= HLS_SNOW_MIN) & (hi - lo <= HLS_SNOW_SPREAD)
    obs = np.where((rgba[..., 3] > 0) & clear, np.where(snow, 2, 1), 0).astype(np.uint8)
    ground = fast._ef.snowobs_ground_grid(p.dem, p.dn, p.dw, DEM_RES, g_north, g_west, g_res, rgba.shape[0],
                                          rgba.shape[1])
    return ground, obs.ravel()


def cmd_hls(a, places, dates):
    """IMS and VIIRS against Sentinel-2 (HLS) lines, and MODIS too, where MODIS saw the cell clear."""
    cmap = colour_map(a.cache)
    with open(a.lines) as f:
        lines = json.load(f)
    seen = {}
    for r in lines:
        if r["kind"] == "mountain":
            seen.setdefault((r["place"], r["date"]), []).append(r["modis_seen"])
    out = []
    for p in places:
        if p.kind != "mountain":
            continue
        for d in dates:
            s = seen.get((p.name, d.isoformat()), [])
            if sum(v >= 0.9 for v in s) < 3:
                continue
            mv = p.gibs_values(a.cache, LAYERS["modis"], d, cmap)
            h = hls_obs(a.cache, p, d, mv)
            if h is None:
                continue
            ims = ims_obs_for(a, [p], d)
            mo = fast._ef.snowobs_classes(mv.ravel(), "ndsi")
            for cell in p.cells:
                hc, k0 = p.counts(h[0], h[1], cell)
                rec = {"place": p.name, "date": d.isoformat(), "cell": list(cell),
                       "hls": [line_of(hc, k0, col) for col in (0, 1)]}
                mc, _ = p.counts(p.gibs_ground, mo, cell)
                rec["modis"] = [line_of(mc, k0, col) for col in (0, 1)]
                if ims is not None and p.name in ims:
                    ic, _ = p.counts(p.ims_ground, ims[p.name], cell)
                    rec["ims"] = [line_of(ic, k0, col) for col in (0, 1)]
                out.append(rec)
            print("hls", p.name, d, flush=True)
    if a.json:
        with open(a.json, "w") as f:
            json.dump(out, f)
    print("\nObserved snowlines against Sentinel-2 (HLS S30, ~120 m, MODIS-clear pixels), per cell and column")
    for src in ("ims", "modis"):
        dd = []
        for r in out:
            for col in (0, 1):
                hl = r["hls"][col]
                if src in r and usable(hl) and r[src][col]["tot"] >= 10:
                    dd.append(r[src][col]["line"] - hl["line"])
        print(f"  {src:6s} - S2  {_stats(dd)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=("fetch", "lines", "tables", "hls"))
    ap.add_argument("--cache", default=os.path.expanduser("~/bnge-tests/localweather/s1/data"))
    ap.add_argument("--dates", nargs="*", help="YYYY-MM-DD (default: every 4 days over the 2025-26 season)")
    ap.add_argument("--places", nargs="*")
    ap.add_argument("--json")
    ap.add_argument("--lines", help="tables: the lines command's JSON (picks each mountain place's clearest dates)")
    ap.add_argument("--per-place", type=int, default=5)
    ap.add_argument("--mild-dates", nargs="*", default=["2026-01-08", "2026-01-20", "2026-02-05"])
    ap.add_argument("--extra-dates", nargs="*", default=[], help="tables: more dates for every mountain place")
    ap.add_argument("--rule-sweep", action="store_true", help="tables: the first sweep's fusion rules too")
    a = ap.parse_args()
    fast.require(NATIVE, "snowobs probe")
    dates = [datetime.date.fromisoformat(x) for x in a.dates] if a.dates else default_dates()
    places = [Place(a.cache, *p) for p in PLACES if not a.places or p[0] in a.places]
    if a.cmd == "fetch":
        cmd_fetch(a, places, dates)
    elif a.cmd == "lines":
        cmd_lines(a, places, dates)
    elif a.cmd == "hls":
        cmd_hls(a, places, dates)
    else:
        cmd_tables(a, places, dates)


if __name__ == "__main__":
    main()
