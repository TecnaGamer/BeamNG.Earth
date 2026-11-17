"""build_leaftype.py FOREST_TYPE.tif [RES]: server/leaftype_2019.png from the Copernicus Global Land Service's Land
Cover 100 m, collection 3, epoch 2019, its Forest Type layer (Buchhorn et al. 2020; Zenodo record 3939050, the file
PROBAV_LC100_global_v3.0.1_2019-nrt_Forest-Type-layer_EPSG-4326.tif; CC BY 4.0; CREDITS.md): per 100 m pixel of
forest 1 evergreen needleleaf, 2 evergreen broadleaf, 3 deciduous needleleaf, 4 deciduous broadleaf, 5 mixed, 0 unknown
or no forest.

The output: RES degree pixels (default 0.05: 7200 x 3600 from 180 W, 90 N), an 8-bit two-band PNG (LA): band 0 the
conifers' share of the forest pixels (needleleaf, evergreen or deciduous; a mixed pixel half), band 1 the evergreen
broadleaves' share, both 0-250 for 0-1; 255 where a pixel holds fewer than MIN_FOREST forest pixels (no say: the
coarse rule stands). Needs tifffile and imagecodecs (the source is a tiled, compressed GeoTIFF)."""
import os
import sys

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIN_FOREST = 25             # forest pixels (100 m) an output pixel needs for a share (25 ha)


def main(path, res=0.05):
    import tifffile
    with tifffile.TiffFile(path) as tif:
        page = tif.pages[0]
        h, w = page.shape[:2]
        scale = page.tags["ModelPixelScaleTag"].value
        tie = page.tags["ModelTiepointTag"].value
        px, py = float(scale[0]), float(scale[1])
        x0, y0 = float(tie[3]), float(tie[4])
        print(f"{w} x {h} px of {px:.7f} deg from ({x0}, {y0}); tiled: {page.is_tiled}", flush=True)
        ow, oh = int(round(360 / res)), int(round(180 / res))
        # (per output pixel: 2 x needleleaf + mixed, evergreen broadleaf, all forest)
        needle2 = np.zeros(oh * ow, np.uint32)
        evb = np.zeros(oh * ow, np.uint32)
        total = np.zeros(oh * ow, np.uint32)
        n = 0
        for data, idx, shape in page.segments():
            if data is None:
                continue
            a = np.asarray(data).reshape(shape[1], shape[2])
            r0, c0 = idx[2], idx[3]         # (tifffile's segment indices: sample, depth, row, column (pixels), ...)
            a = a[:max(min(a.shape[0], h - r0), 0), :max(min(a.shape[1], w - c0), 0)]
            m = (a >= 1) & (a <= 5)
            if not m.any():
                continue
            rr, cc = np.nonzero(m)
            v = a[rr, cc]
            lat = y0 - (r0 + rr + 0.5) * py
            lon = x0 + (c0 + cc + 0.5) * px
            j = np.clip(((90.0 - lat) / res).astype(np.int64), 0, oh - 1)
            i = np.clip(((lon + 180.0) / res).astype(np.int64), 0, ow - 1)
            keys, inv = np.unique(j * ow + i, return_inverse=True)
            nw = np.where((v == 1) | (v == 3), 2, np.where(v == 5, 1, 0))
            total[keys] += np.bincount(inv).astype(np.uint32)
            needle2[keys] += np.bincount(inv, weights=nw).astype(np.uint32)
            evb[keys] += np.bincount(inv, weights=(v == 2)).astype(np.uint32)
            n += 1
            if n % 2000 == 0:
                print(f"{n} segments", flush=True)
    t, nd, eb = total.reshape(oh, ow), needle2.reshape(oh, ow), evb.reshape(oh, ow)
    ok = t >= MIN_FOREST
    tt = np.maximum(t, 1)
    con = np.where(ok, np.round(250.0 * nd / (2.0 * tt)), 255).astype(np.uint8)
    ev = np.where(ok, np.round(250.0 * eb / tt), 255).astype(np.uint8)
    out = os.path.join(ROOT, "server", "leaftype_2019.png")
    Image.fromarray(np.dstack([con, ev]), "LA").save(out, optimize=True)
    print(f"{out}: {ow} x {oh}, {os.path.getsize(out) / 1e6:.2f} MB, {int(ok.sum())} forest pixels")


if __name__ == "__main__":
    main(sys.argv[1], *(float(a) for a in sys.argv[2:]))
