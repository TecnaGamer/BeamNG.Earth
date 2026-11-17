"""build_ecoregions.py Ecoregions2017.shp: server/ecoregions_2017_0p05.png and server/ecoregions_2017.json from
RESOLVE's Ecoregions 2017 (Dinerstein et al. 2017, BioScience 67(6): 534-545; https://ecoregions.appspot.com, the
shapefile https://storage.googleapis.com/teow2016/Ecoregions2017.zip, CC BY 4.0; CREDITS.md).

The raster: 0.05 degree pixels (7200 x 3600 from 180 W, 90 N), a 16-bit PNG of each pixel's ECO_ID (1-847; 0 none: the
sea, the large lakes; ECO_ID 0 is the shapefile's "Rock and Ice", kept as 0 too). A pixel takes the ecoregion its
centre falls in (each record's rings drawn alone, its holes cut out, then laid in: one record's hole never erases
another's island). Then the coasts grown COAST_PX pixels into the sea (a 0 pixel takes the most common ecoregion
of its 8 neighbours, COAST_PX times over): a tree on a quay or a small island whose pixel's centre lies at sea finds
its land without a search at run time. The JSON: {ECO_ID: [name, biome number, realm]}. Needs pyshp and Pillow."""
import json
import os
import sys

import numpy as np
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = 0.05
W, H = int(round(360 / RES)), int(round(180 / RES))
COAST_PX = 3


def grow_coasts(grid, steps=COAST_PX):
    """Each 0 pixel next to land takes its neighbours' most common ecoregion (ties: the lowest ID), steps times; the
    columns wrap at 180 degrees."""
    g = grid.copy()
    shifts = [(dy, dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1) if dy or dx]
    for _ in range(steps):
        nb = np.stack([np.roll(np.pad(g, ((1, 1), (0, 0)))[1 + dy:1 + dy + g.shape[0]], -dx, axis=1)
                       for dy, dx in shifts])                 # (8, h, w) the neighbours
        zero = (g == 0) & (nb > 0).any(axis=0)
        if not zero.any():
            break
        cand = nb[:, zero]                                    # (8, n)
        best = np.zeros(cand.shape[1], np.uint16)
        best_n = np.zeros(cand.shape[1], np.int64)
        for row in cand:                                      # (a mode over 8 values: count each candidate's matches)
            n = (cand == row).sum(axis=0)
            take = (row > 0) & ((n > best_n) | ((n == best_n) & (row < best)))
            best = np.where(take, row, best)
            best_n = np.where(take, n, best_n)
        g[zero] = best
    return g


def _px(points):
    """lon/lat points -> pixel coordinates (PIL's polygon fills pixels whose centres lie inside: x + 0.5 grid)."""
    return [((x + 180.0) / RES - 0.5, (90.0 - y) / RES - 0.5) for x, y in points]


def _signed_area(pts):
    a = 0.0
    for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]):
        a += x0 * y1 - x1 * y0
    return a / 2.0


def main(path):
    import shapefile
    r = shapefile.Reader(path, encoding="latin-1")
    grid = np.zeros((H, W), np.uint16)
    meta = {}
    for n, sr in enumerate(r.iterShapeRecords()):
        rec, shp = sr.record, sr.shape
        eid = int(rec["ECO_ID"])
        if eid <= 0 or not shp.points:
            continue
        meta[eid] = [rec["ECO_NAME"].strip(), int(rec["BIOME_NUM"]), rec["REALM"].strip()]
        x0, y0, x1, y1 = shp.bbox
        c0, c1 = max(int((x0 + 180.0) / RES) - 1, 0), min(int((x1 + 180.0) / RES) + 2, W)
        r0, r1 = max(int((90.0 - y1) / RES) - 1, 0), min(int((90.0 - y0) / RES) + 2, H)
        if c1 <= c0 or r1 <= r0:
            continue
        mask = Image.new("1", (c1 - c0, r1 - r0), 0)
        draw = ImageDraw.Draw(mask)
        parts = list(shp.parts) + [len(shp.points)]
        for a, b in zip(parts, parts[1:]):
            ring = shp.points[a:b]
            if len(ring) < 3:
                continue
            # (the shapefile's rings: outer clockwise, holes counter-clockwise, in lon / lat with y up)
            hole = _signed_area(ring) > 0
            pts = [(x - c0, y - r0) for x, y in _px(ring)]
            draw.polygon(pts, fill=0 if hole else 1)
        m = np.asarray(mask, bool)
        grid[r0:r1, c0:c1][m] = eid
        if n % 100 == 0:
            print(f"{n} / {len(r)}", flush=True)
    grid = grow_coasts(grid)
    out = os.path.join(ROOT, "server", "ecoregions_2017_0p05.png")
    Image.fromarray(grid).save(out, optimize=True)
    js = os.path.join(ROOT, "server", "ecoregions_2017.json")
    with open(js, "w", encoding="utf-8", newline="\n") as f:
        json.dump({str(k): meta[k] for k in sorted(meta)}, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{out}: {os.path.getsize(out) / 1e6:.2f} MB, {len(np.unique(grid)) - 1} ecoregions drawn of {len(meta)}; "
          f"{js}: {os.path.getsize(js) / 1e3:.0f} kB")


if __name__ == "__main__":
    main(sys.argv[1])
