#!/usr/bin/env python3
"""build server/data/au_mvg_v7.npz, the Australian vegetation grid vegclass.py reads.

    python tools/build_au_vegetation.py [--cache DIR] [--res 0.01] [--out server/data/au_mvg_v7.npz]

Source: NVIS Version 7.0, Present (extant) Major Vegetation Groups raster, (c) Commonwealth of Australia (Department of
Climate Change, Energy, the Environment and Water), CC BY 4.0 (data.gov.au "Australia - Present Major Vegetation Groups
and Subgroups - NVIS Version 7.0 Rasters Download", hosted at fed.dcceew.gov.au). The zip (about 148 MB, an Esri file
geodatabase holding the 100 m Albers raster NVIS7_0_AUST_EXT_MVG_ALB, GDA2020 / Australian Albers) is downloaded to the
cache directory outside the repository. Reading it needs rasterio (GDAL's OpenFileGDB driver: `pip install rasterio`;
not a runtime dependency of the server).

Resampled to a regular latitude / longitude grid of --res degrees (0.01 = about 1 km), 112 E to 154 E and 9 S to 44.5 S:
each cell holds the most common vegetation group among SUB x SUB source points spread over it (land wins over the
sea). Cells hold the MVG number (LEGEND), 0 the sea, no data and the outside. The file holds grid (uint8, rows from the
north), lat0, lon0 (the grid's north-west corner), res.
"""
import argparse
import os
import sys
import urllib.request
import zipfile

import numpy as np

URL = "https://www.arcgis.com/sharing/rest/content/items/5e70b5afc36a4c458a2cceb313eb3889/data"
ZIP_NAME = "NVIS_V7_0_AUST_RASTERS_EXT_ALL.zip"
GDB_REL = "NVIS_V7_0_AUST_RASTERS_EXT_ALL/NVIS_V7_0_AUST_EXT.gdb"
LAYER = "NVIS7_0_AUST_EXT_MVG_ALB"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server", "data", "au_mvg_v7.npz")
LON0, LON1, LAT0, LAT1 = 112.0, 154.0, -9.0, -44.5      # the grid's west, east, north, south edges
SUB = 5                 # source points per cell side (the mode of SUB * SUB of them)
BAND = 10               # output rows a pass

# The dataset's own legend (the Value Attribute Table VAT_NVIS7_0_AUST_EXT_MVG_ALB of the geodatabase)
LEGEND = {
    1: "Rainforests and Vine Thickets", 2: "Eucalypt Tall Open Forests", 3: "Eucalypt Open Forests",
    4: "Eucalypt Low Open Forests", 5: "Eucalypt Woodlands", 6: "Acacia Forests and Woodlands",
    7: "Callitris Forests and Woodlands", 8: "Casuarina Forests and Woodlands",
    9: "Melaleuca Forests and Woodlands", 10: "Other Forests and Woodlands", 11: "Eucalypt Open Woodlands",
    12: "Tropical Eucalypt Woodlands/Grasslands", 13: "Acacia Open Woodlands", 14: "Mallee Woodlands and Shrublands",
    15: "Low Closed Forests and Tall Closed Shrublands", 16: "Acacia Shrublands", 17: "Other Shrublands",
    18: "Heathlands", 19: "Tussock Grasslands", 20: "Hummock Grasslands",
    21: "Other Grasslands, Herblands, Sedgelands and Rushlands",
    22: "Chenopod Shrublands, Samphire Shrublands and Forblands", 23: "Mangroves",
    24: "Inland aquatic - freshwater, salt lakes, lagoons", 25: "Cleared, non-native vegetation, buildings",
    26: "Unclassified native vegetation", 27: "Naturally bare - sand, rock, claypan, mudflat",
    28: "Sea and estuaries", 29: "Regrowth, modified native vegetation", 31: "Other Open Woodlands",
    32: "Mallee Open Woodlands and Sparse Mallee Shrublands", 99: "Unknown/no data",
}
DROP = (28, 99)         # stored as 0

# GDA2020 / Australian Albers (EPSG:9473): GRS80, standard parallels 18 S and 36 S, centre 0 N 132 E
A, F = 6378137.0, 1 / 298.257222101
LAT1_, LAT2_, LON_C = -18.0, -36.0, 132.0


def albers(lat, lon):
    """Snyder's Albers equal-area conic (ellipsoidal) forward: (x, y) metres of arrays of degrees."""
    e2 = 2 * F - F * F
    e = np.sqrt(e2)

    def q(phi):
        s = np.sin(phi)
        return (1 - e2) * (s / (1 - e2 * s * s) - np.log((1 - e * s) / (1 + e * s)) / (2 * e))

    def m(phi):
        s = np.sin(phi)
        return np.cos(phi) / np.sqrt(1 - e2 * s * s)

    p1, p2 = np.radians(LAT1_), np.radians(LAT2_)
    n = (m(p1) ** 2 - m(p2) ** 2) / (q(p2) - q(p1))
    c = m(p1) ** 2 + n * q(p1)
    rho0 = A * np.sqrt(c - n * q(0.0)) / n
    phi = np.radians(np.asarray(lat, np.float64))
    rho = A * np.sqrt(c - n * q(phi)) / n
    th = n * np.radians(np.asarray(lon, np.float64) - LON_C)
    return rho * np.sin(th), rho0 - rho * np.cos(th)


def fetch(cache):
    """The geodatabase's path: the zip downloaded into cache and unpacked there when it is not yet."""
    gdb = os.path.join(cache, "gdb", GDB_REL)
    if os.path.isdir(gdb):
        return gdb
    os.makedirs(cache, exist_ok=True)
    zp = os.path.join(cache, ZIP_NAME)
    if not os.path.exists(zp):
        print("downloading", URL)
        urllib.request.urlretrieve(URL, zp + ".part")
        os.replace(zp + ".part", zp)
    with zipfile.ZipFile(zp) as z:
        z.extractall(os.path.join(cache, "gdb"))
    return gdb


def mode_land(vals):
    """The most common nonzero value along the last axis (0 where all are zero)."""
    counts = np.zeros(vals.shape[:-1] + (256,), np.uint8)
    for k in range(vals.shape[-1]):
        np.add.at(counts, (*np.indices(vals.shape[:-1], sparse=True), vals[..., k]), 1)
    counts[..., 0] = 0
    best = counts.argmax(axis=-1).astype(np.uint8)
    return np.where(counts.max(axis=-1) > 0, best, 0).astype(np.uint8)


def build(gdb, res):
    import rasterio
    from rasterio.windows import Window
    rows, cols = int(round((LAT0 - LAT1) / res)), int(round((LON1 - LON0) / res))
    out = np.zeros((rows, cols), np.uint8)
    lut = np.arange(256, dtype=np.uint8)
    lut[list(DROP)] = 0
    off = (np.arange(SUB) + 0.5) / SUB
    with rasterio.open('OpenFileGDB:"%s":%s' % (gdb, LAYER)) as ds:
        t = ds.transform
        for r0 in range(0, rows, BAND):
            r1 = min(r0 + BAND, rows)
            jj = (r0 + off[None, :] + np.arange(r1 - r0)[:, None]).ravel()          # (BAND * SUB) fractional rows
            ii = (off[None, :] + np.arange(cols)[:, None]).ravel()
            lat = LAT0 - jj * res
            lon = LON0 + ii * res
            x, y = albers(lat[:, None], lon[None, :])
            c = np.floor((x - t.c) / t.a).astype(np.int64)
            r = np.floor((y - t.f) / t.e).astype(np.int64)
            ok = (c >= 0) & (c < ds.width) & (r >= 0) & (r < ds.height)
            v = np.zeros(c.shape, np.uint8)
            if ok.any():
                w0, w1 = int(r[ok].min()), int(r[ok].max()) + 1
                v0, v1 = int(c[ok].min()), int(c[ok].max()) + 1
                blk = lut[ds.read(1, window=Window(v0, w0, v1 - v0, w1 - w0))]
                v[ok] = blk[r[ok] - w0, c[ok] - v0]
            # (rows * SUB, cols * SUB) -> (rows, cols, SUB * SUB)
            v = v.reshape(r1 - r0, SUB, cols, SUB).transpose(0, 2, 1, 3).reshape(r1 - r0, cols, SUB * SUB)
            out[r0:r1] = mode_land(v)
            if r0 % 500 == 0:
                print("rows %d / %d" % (r1, rows), flush=True)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cache", default=os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache"),
                                                    "beamng_earth_cache", "nvis"))
    ap.add_argument("--res", type=float, default=0.01)
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args()
    grid = build(fetch(a.cache), a.res)
    np.savez_compressed(a.out, grid=grid, lat0=LAT0, lon0=LON0, res=a.res)
    print("%s: %s, %.2f MB; cells by group:" % (a.out, grid.shape, os.path.getsize(a.out) / 1e6))
    codes, n = np.unique(grid, return_counts=True)
    for k, v in zip(codes, n):
        print("  %3d %9d  %s" % (k, v, LEGEND.get(int(k), "(sea / none)")))


if __name__ == "__main__":
    sys.exit(main())
