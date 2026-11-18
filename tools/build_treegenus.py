"""build_treegenus.py DIR [LEVEL]: server/treegenus_2020.npz from ForestPaths' European Tree Genus Map 2020 (early
access, De Keersmaecker et al. 2025, Zenodo 10.5281/zenodo.13341104, CC BY 4.0; CREDITS.md): 10 m classes over
Europe, 100 km Cloud Optimized GeoTIFF tiles in EPSG:3035 (zipped by column, ulx_*.zip, in DIR): 0 Larix, 1 Picea,
2 Pinus, 3 Fagus, 4 Quercus, 5 other needleleaf, 6 other broadleaf, 7 no trees.

Each tile read at its overview LEVEL (default 2: 40 m; the classes sampled, not averaged), its pixels' centres taken
to latitude and longitude (laea_inverse: EPSG:3035's ellipsoidal inverse, IOGP 7-2) and counted per genus per 0.05
degree cell of a grid from GRID's corner. The output (uint16 counts, shape (7, rows, columns): the genera 0-6, the
trees only) with the grid's corner and resolution; vegetation.treegenus reads it."""
import glob
import math
import os
import shutil
import sys
import tempfile
import zipfile

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = 0.05
GRID = (72.0, -32.0, 27.0, 46.0)        # north, west, south, east: Europe's tiles all inside
GENERA = 7

# EPSG:3035: GRS 1980, latitude of origin 52 N, longitude 10 E, false easting 4 321 000, northing 3 210 000
A, E2 = 6378137.0, 0.0066943800229
LAT0, LON0, FE, FN = math.radians(52.0), math.radians(10.0), 4321000.0, 3210000.0


def _q(phi):
    e = math.sqrt(E2)
    s = np.sin(phi)
    return (1 - E2) * (s / (1 - E2 * s * s) - (1 / (2 * e)) * np.log((1 - e * s) / (1 + e * s)))


def laea_inverse(x, y):
    """EPSG:3035 easting, northing (m) to latitude, longitude (degrees): the ellipsoidal inverse (IOGP Guidance Note
    7-2, Lambert Azimuthal Equal Area, EPSG method 9820)."""
    qp, q0 = float(_q(np.array(math.pi / 2))), float(_q(np.array(LAT0)))
    rq = A * math.sqrt(qp / 2)
    b0 = math.asin(q0 / qp)
    d = A * math.cos(LAT0) / (math.sqrt(1 - E2 * math.sin(LAT0) ** 2) * rq * math.cos(b0))
    dx, dy = np.asarray(x, np.float64) - FE, np.asarray(y, np.float64) - FN
    rho = np.sqrt((dx / d) ** 2 + (d * dy) ** 2)
    c = 2 * np.arcsin(np.clip(rho / (2 * rq), -1, 1))
    with np.errstate(invalid="ignore", divide="ignore"):
        beta = np.arcsin(np.cos(c) * math.sin(b0) + np.where(rho > 0, d * dy * np.sin(c) * math.cos(b0) / rho, 0))
    e4, e6 = E2 * E2, E2 ** 3
    phi = (beta + (E2 / 3 + 31 * e4 / 180 + 517 * e6 / 5040) * np.sin(2 * beta)
           + (23 * e4 / 360 + 251 * e6 / 3780) * np.sin(4 * beta) + (761 * e6 / 45360) * np.sin(6 * beta))
    lam = LON0 + np.arctan2(dx * np.sin(c), d * rho * math.cos(b0) * np.cos(c) - d * d * dy * math.sin(b0) * np.sin(c))
    return np.degrees(phi), np.degrees(lam)


def _tile(path, level, counts, rows, cols):
    import tifffile
    with tifffile.TiffFile(path) as tif:
        s = tif.series[0]
        lv = s.levels[min(level, len(s.levels) - 1)]
        a = lv.asarray()
        page0 = tif.pages[0]
        scale = page0.tags["ModelPixelScaleTag"].value
        tie = page0.tags["ModelTiepointTag"].value
        h0, w0 = page0.shape[:2]
    if a.ndim > 2:
        a = a[..., 0] if a.shape[-1] < a.shape[0] else a[0]
    h, w = a.shape
    px, py = float(scale[0]) * w0 / w, float(scale[1]) * h0 / h
    x0, y0 = float(tie[3]), float(tie[4])
    tree = a < GENERA
    if not tree.any():
        return 0
    rr, cc = np.nonzero(tree)
    lat, lon = laea_inverse(x0 + (cc + 0.5) * px, y0 - (rr + 0.5) * py)
    north, west = GRID[0], GRID[1]
    j = np.floor((north - lat) / RES).astype(np.int64)
    i = np.floor((lon - west) / RES).astype(np.int64)
    ok = (j >= 0) & (j < rows) & (i >= 0) & (i < cols)
    key = (a[rr[ok], cc[ok]].astype(np.int64) * rows + j[ok]) * cols + i[ok]
    u, n = np.unique(key, return_counts=True)
    flat = counts.reshape(-1)
    flat[u] += n.astype(flat.dtype)
    return int(ok.sum())


def main(src, level=2):
    level = int(level)
    rows = int(round((GRID[0] - GRID[2]) / RES))
    cols = int(round((GRID[3] - GRID[1]) / RES))
    counts = np.zeros((GENERA, rows, cols), np.uint32)
    zips = sorted(glob.glob(os.path.join(src, "*.zip")))
    if not zips:
        sys.exit(f"no ulx_*.zip in {src}")
    tmp = tempfile.mkdtemp(prefix="treegenus_")
    try:
        for z in zips:
            with zipfile.ZipFile(z) as zf:
                names = [n for n in zf.namelist() if n.lower().endswith((".tif", ".tiff"))]
                total = 0
                for n in names:
                    p = zf.extract(n, tmp)
                    try:
                        total += _tile(p, level, counts, rows, cols)
                    finally:
                        os.remove(p)
            print(f"{os.path.basename(z)}: {len(names)} tiles, {total} tree px", flush=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    out = os.path.join(ROOT, "server", "treegenus_2020.npz")
    c16 = np.minimum(counts, 65535).astype(np.uint16)
    np.savez_compressed(out, counts=c16, north=GRID[0], west=GRID[1], res=RES, level=level)
    print(f"{out}: {cols} x {rows}, {os.path.getsize(out) / 1e6:.2f} MB, {int((counts.sum(0) > 0).sum())} cells")


if __name__ == "__main__":
    main(*sys.argv[1:])
