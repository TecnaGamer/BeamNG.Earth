"""build_leaftype_cci.py DIR [RES]: server/leaftype_cci_2020.png from the ESA CCI Land Cover / C3S global land cover
map 2020 (v2.1.1, 300 m; its 32 tiles C3S-LC-L4-LCCS-Map-300m-P1Y-2020-v2.1.1-<tile>-lccs_class.tif, Microsoft's
Planetary Computer's Cloud Optimized GeoTIFFs of the CDS's NetCDF, in DIR; "(c) ESA Climate Change Initiative - Land
Cover led by UCLouvain (2017)", CREDITS.md). Classes (LCCS): 50 tree broadleaved evergreen, 60-62 broadleaved
deciduous, 70-72 needleleaved evergreen, 80-82 needleleaved deciduous, 90 mixed, 160 flooded (fresh water, counted
broadleaved), 170 flooded (saline: mangroves, broadleaved evergreen).

The output as build_leaftype.py's (leaftype.py reads both and averages them): RES degree pixels (default 0.05: 7200 x
3600 from 180 W, 90 N), an 8-bit LA PNG, band 0 the conifers' share of the tree pixels (a mixed pixel half), band 1
the evergreen broadleaves', both 0-250 for 0-1; 255 where fewer than MIN_TREE tree pixels. (39: against the national
forest inventories' plots, EU-Forest's 675 one-degree cells in Europe, its conifer share correlates 0.80, LC100's
0.70: LC100 reads Sardinia's maquis, Lapland's mountain birch woods and Spain's holm oak woods as conifers.)"""
import glob
import os
import sys

try:        # (first: numpy's AVX-512 kernels held back before anything imports numpy, tools/npyfirst.py)
    import npyfirst  # noqa: E402,F401
except ImportError:     # (loaded by its path into another process, a test: that process's entry point guards)
    pass
import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIN_TREE = 3                # tree pixels (300 m, 9 ha each) an output pixel needs for a share
NEEDLE = (70, 71, 72, 80, 81, 82)
BROAD = (60, 61, 62, 160)
EVB = (50, 170)
MIXED = 90


def main(src, res=0.05):
    import tifffile
    ow, oh = int(round(360 / res)), int(round(180 / res))
    needle2 = np.zeros((oh, ow), np.uint32)
    evb = np.zeros((oh, ow), np.uint32)
    total = np.zeros((oh, ow), np.uint32)
    files = sorted(glob.glob(os.path.join(src, "*lccs_class.tif")))
    if not files:
        sys.exit(f"no tiles in {src}")
    for path in files:
        with tifffile.TiffFile(path) as tif:
            page = tif.pages[0]
            scale = page.tags["ModelPixelScaleTag"].value
            tie = page.tags["ModelTiepointTag"].value
            a = page.asarray()
        px = float(scale[0])
        x0, y0 = float(tie[3]), float(tie[4])
        k = int(round(res / px))                    # source pixels per output pixel (18 at 0.05)
        h, w = (a.shape[0] // k) * k, (a.shape[1] // k) * k
        a = a[:h, :w]
        r0 = int(round((90.0 - y0) / res))
        c0 = int(round((x0 + 180.0) / res))

        def blocks(m):
            return m.reshape(h // k, k, w // k, k).sum(axis=(1, 3), dtype=np.uint32)
        nd = blocks(np.isin(a, NEEDLE)) * 2 + blocks(a == MIXED)
        eb = blocks(np.isin(a, EVB))
        tt = blocks(np.isin(a, NEEDLE + BROAD + EVB + (MIXED,)))
        sl = (slice(r0, r0 + h // k), slice(c0, c0 + w // k))
        needle2[sl] += nd
        evb[sl] += eb
        total[sl] += tt
        print(f"{os.path.basename(path)}: {int(tt.sum())} tree px", flush=True)
    ok = total >= MIN_TREE
    tt = np.maximum(total, 1)
    con = np.where(ok, np.round(250.0 * needle2 / (2.0 * tt)), 255).astype(np.uint8)
    ev = np.where(ok, np.round(250.0 * evb / tt), 255).astype(np.uint8)
    out = os.path.join(ROOT, "server", "leaftype_cci_2020.png")
    Image.fromarray(np.dstack([con, ev]), "LA").save(out, optimize=True)
    print(f"{out}: {ow} x {oh}, {os.path.getsize(out) / 1e6:.2f} MB, {int(ok.sum())} tree pixels")


if __name__ == "__main__":
    main(sys.argv[1], *(float(a) for a in sys.argv[2:]))
