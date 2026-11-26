#!/usr/bin/env python3
"""The puddle atlas (the road puddles' depth masks: server roadwear.puddle_chunks, material bne_puddle in
roadwear.materials.json, wetlook.lua's alpha threshold): drawn by the native earthfast.puddle_atlas_px (roadwear.rs
puddle_atlas: 4 x 4 cells, 4 shapes a row, the rows ever shallower; deterministic, integer-hashed value noise) and
written as the mod's lossless source (mod/texture_sources, WebP) with its manifest entry (install.py encodes it into
BC4).

  python3 tools/make_puddle_atlas.py [--size 512] [--preview OUT.png]
"""
import argparse
import io
import os
import sys

import numpy as np
from PIL import Image

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "server"))
import modtextures  # noqa: E402

REL = "levels/beamng_earth/art/shapes/beamngEarth/bne_puddle_o.data"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--size", type=int, default=512)
    ap.add_argument("--preview", help="a PNG of the atlas")
    args = ap.parse_args()
    import fast
    fast.require(637, "puddle_atlas_px")
    px = np.asarray(fast._ef.puddle_atlas_px(args.size), np.uint8).reshape(args.size, args.size)
    img = Image.fromarray(px, "L").convert("RGB")
    if args.preview:
        img.save(args.preview)
    src = os.path.join(modtextures.SOURCES, *REL.split("/")) + ".webp"
    os.makedirs(os.path.dirname(src), exist_ok=True)
    b = io.BytesIO()
    img.save(b, "WEBP", lossless=True, quality=100, method=6, exact=True)
    with open(src, "wb") as f:
        f.write(b.getvalue())
    entries = modtextures.manifest()
    entries[REL + ".dds"] = {"source": REL + ".webp", "format": "BC4", "role": "data"}
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import mod_textures_dds
    mod_textures_dds.write_manifest(entries)
    print("wrote", src)


if __name__ == "__main__":
    main()
