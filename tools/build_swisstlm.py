#!/usr/bin/env python3
"""Cut swisstopo's swissTLM3D into the "tlm" road-height cells (server/roadz.py): Switzerland's and Liechtenstein's
measured bridge, tunnel and road heights.

swisstopo serves swissTLM3D only whole (one file a release, ~5 GB as a zip; no service by area: the geo.admin.ch API
answers roads flat and without their kind of structure). This reads the release's FileGDB straight from swisstopo's
zip by range requests (GDAL /vsizip//vsicurl: only the parts a box needs come over the network), z12 tile by z12 tile,
and writes every z14 cell it covers as the Lines file roadz keeps (write_cell: lonlat, z, off, level, ref, name,
corrected): every road (TLM_STRASSE) that reaches into the cell, whole, its heights swisstopo's (above the sea, LN02:
roadz ties them to its own DEM by the cell's ground roads, as for the other sources), its level from its kind of
structure (server/tlmcodes.py, which roadz reads too; a road in or on a building is left out), its name both of
swisstopo's (the structure's and the street's: roadz matches either to the map's). A cell without roads is not
written (the server takes a 404 for none).

Needs GDAL through pyogrio, geopandas and pyproj (pip install pyogrio geopandas shapely pyproj numpy); not the
server's packages.

    python tools/build_swisstlm.py --out <cache>/roadz/v<roadz.VERSION>/tlm1      (all of Switzerland: hours)
    python tools/build_swisstlm.py --out cells --box 47.36,8.50,47.41,8.56         (lat0,lon0,lat1,lon1)

<out> is a folder of <z>/<x>/<y>.npz: the server's cache folder for "tlm" (roadz.TLM_CELLS: it reads its cells there
before asking BNGE_ROADZ_TLM_URL), or a folder to serve under that URL.

Data: swissTLM3D (c) swisstopo, Open Government Data (https://www.swisstopo.admin.ch/en/terms-of-use-free-geodata-
and-geoservices): free use with the attribution "swisstopo".
"""
import argparse
import math
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

RELEASE = "2026-02"
URL = ("/vsizip//vsicurl/https://data.geo.admin.ch/ch.swisstopo.swisstlm3d/swisstlm3d_{r}/"
       "swisstlm3d_{r}_2056_5728.gdb.zip")
CH_BOX = (45.80, 5.94, 47.82, 10.50)       # lat0, lon0, lat1, lon1 (roadz.CH_BOUNDS)
LAYER, COLUMNS = "TLM_STRASSE", ["KUNSTBAUTE", "NAME", "STRASSENNAME"]
PAD_M = 300.0                              # m round a z12 tile its box reads further (a line reaching in from outside)
SAMPLE_DEG = 0.0002                        # deg between the samples a line's cells are found by (~15-20 m)


def tile_of(lat, lon, z):
    n = 1 << z
    x = int((lon + 180.0) / 360.0 * n)
    s = math.sin(math.radians(lat))
    y = int((0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n)
    return min(n - 1, max(0, x)), min(n - 1, max(0, y))


def bounds(z, x, y):
    """(lon0, lat0, lon1, lat1) of a Web-Mercator tile (mapcells.CellKey.bounds)."""
    n = 1 << z

    def lat(t):
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * t / n))))
    return x / n * 360.0 - 180.0, lat(y + 1), (x + 1) / n * 360.0 - 180.0, lat(y)


def cells_of_line(lonlat, z=14):
    """The z14 cells a line's vertices and the straight pieces between them pass (sampled every SAMPLE_DEG)."""
    out = set()
    for (a, b) in zip(lonlat[:-1], lonlat[1:]):
        k = max(1, int(math.hypot(b[0] - a[0], b[1] - a[1]) / SAMPLE_DEG))
        for t in np.linspace(0.0, 1.0, k + 1):
            out.add(tile_of(a[1] + t * (b[1] - a[1]), a[0] + t * (b[0] - a[0]), z))
    return out


def name_of(*names):
    """swisstopo's names of a line, each once ("Milchbucktunnel / Hofwiesenstrasse"): roadz matches the map's name to
    any of them."""
    out = []
    for v in names:
        if isinstance(v, str) and v.strip() and v.strip() not in out:
            out.append(v.strip())
    return " / ".join(out)


def write_cell(path, rows):
    """rows [(lonlat (k, 2), z (k,), level, ref, name)] as the Lines file roadz keeps (roadz.Lines.save's arrays; its
    tests load it with Lines.load), written whole or not at all."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    off = np.r_[0, np.cumsum([len(r[0]) for r in rows])].astype(np.int64)
    with open(path + ".tmp", "wb") as fh:
        np.savez_compressed(fh, lonlat=np.concatenate([np.asarray(r[0], np.float64) for r in rows]),
                            z=np.concatenate([np.asarray(r[1], np.float64) for r in rows]), off=off,
                            level=np.array([r[2] for r in rows], np.int8),
                            ref=np.array([r[3] for r in rows], str), name=np.array([r[4] for r in rows], str),
                            corrected=np.array(False))
    os.replace(path + ".tmp", path)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--box", help="lat0,lon0,lat1,lon1 (default: all of Switzerland)")
    ap.add_argument("--release", default=RELEASE)
    ap.add_argument("--gdb", help="a local swissTLM3D FileGDB (.gdb or its .zip) instead of swisstopo's")
    a = ap.parse_args(argv)
    os.environ.setdefault("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".zip")
    os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
    import pyogrio
    import pyproj
    from tlmcodes import tlm_level
    src = a.gdb or URL.format(r=a.release)
    to_ll = pyproj.Transformer.from_crs("EPSG:2056", "EPSG:4326", always_xy=True)
    to_lv = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:2056", always_xy=True)
    lat0, lon0, lat1, lon1 = [float(v) for v in a.box.split(",")] if a.box else CH_BOX
    x0, y1 = tile_of(lat0, lon0, 12)
    x1, y0 = tile_of(lat1, lon1, 12)
    t0, cells, lines_n = time.time(), 0, 0
    for ty in range(y0, y1 + 1):
        for tx in range(x0, x1 + 1):
            w, s, e, n = bounds(12, tx, ty)
            # (all four corners: LV95's grid is turned against the meridians, ~2 deg in the east)
            ex, ey = to_lv.transform([w, e, w, e], [s, s, n, n])
            box = (min(ex) - PAD_M, min(ey) - PAD_M, max(ex) + PAD_M, max(ey) + PAD_M)
            per = {}
            df = pyogrio.read_dataframe(src, layer=LAYER, bbox=box, columns=COLUMNS)
            for _, r in df.iterrows():
                g = r.geometry
                if g is None or g.is_empty:
                    continue
                level = tlm_level(r.get("KUNSTBAUTE"))
                if level is None:
                    continue
                name = name_of(r.get("NAME"), r.get("STRASSENNAME"))
                for part in getattr(g, "geoms", [g]):
                    c = np.asarray(part.coords, np.float64)
                    if len(c) < 2 or c.shape[1] < 3:
                        continue
                    lon, lat = to_ll.transform(c[:, 0], c[:, 1])
                    ll = np.stack([lon, lat], 1)
                    row = (ll, c[:, 2], level, "", name)
                    for cx, cy in cells_of_line(ll):
                        if (cx >> 2) == tx and (cy >> 2) == ty:     # (this z12 tile's own cells only)
                            per.setdefault((cx, cy), []).append(row)
            for (cx, cy), rows in per.items():
                write_cell(os.path.join(a.out, "14", str(cx), f"{cy}.npz"), rows)
                cells += 1
                lines_n += len(rows)
            print(f"z12 {tx}/{ty}: {len(per)} cells ({cells} cells, {lines_n} lines, {time.time() - t0:.0f} s)",
                  flush=True)
    print(f"done: {cells} cells, {lines_n} lines into {a.out} in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
