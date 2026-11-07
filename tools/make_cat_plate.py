#!/usr/bin/env python3
"""make_cat_plate.py: the catenary mast's warning plate (bne_cat_plate_*.png; its material is in catenary.materials.json).

    python3 tools/make_cat_plate.py

A yellow plate with a black triangle and a lightning bolt (the general "high voltage" sign) and a small black number
field, drawn with PIL at 4x and reduced (a sign's lettering is exact where an image generator's is not). Written as
bne_cat_plate_b.color.png (sRGB) and a flat normal / roughness pair."""
import os
import sys

import numpy as np
from PIL import Image, ImageDraw

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "mod", "beamng_earth", "levels", "beamng_earth",
                   "art", "shapes", "beamngEarth")
N = 256
S = 4                           # supersampling


def plate():
    n = N * S
    im = Image.new("RGB", (n, n), (236, 196, 24))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, n - 1, n - 1], outline=(20, 20, 20), width=int(n * 0.03))
    # the triangle (point up), black border, yellow inside
    p = [(n * 0.5, n * 0.10), (n * 0.90, n * 0.74), (n * 0.10, n * 0.74)]
    d.polygon(p, fill=(20, 20, 20))
    inset = n * 0.05
    q = [(n * 0.5, n * 0.10 + inset * 1.9), (n * 0.90 - inset * 1.35, n * 0.74 - inset * 0.55),
         (n * 0.10 + inset * 1.35, n * 0.74 - inset * 0.55)]
    d.polygon(q, fill=(236, 196, 24))
    # the bolt
    b = [(0.53, 0.28), (0.40, 0.52), (0.49, 0.52), (0.44, 0.69), (0.62, 0.45), (0.52, 0.45), (0.58, 0.28)]
    d.polygon([(x * n, y * n) for x, y in b], fill=(20, 20, 20))
    # the number field
    d.rectangle([n * 0.18, n * 0.82, n * 0.82, n * 0.94], fill=(20, 20, 20))
    for k in range(5):
        x = n * (0.24 + 0.105 * k)
        d.rectangle([x, n * 0.85, x + n * 0.06, n * 0.91], fill=(236, 196, 24))
    return im.resize((N, N), Image.LANCZOS)


def main():
    base = os.path.join(OUT, "bne_cat_plate")
    plate().save(base + "_b.color.png")
    Image.fromarray(np.dstack([np.full((N, N), 128, np.uint8), np.full((N, N), 128, np.uint8),
                               np.full((N, N), 255, np.uint8)])).save(base + "_nm.normal.png")
    Image.fromarray(np.full((N, N), 150, np.uint8), "L").save(base + "_r.data.png")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import ingest_rail_textures as ing     # (the plate's material: its maps)
    import json
    path = os.path.join(OUT, "catenary.materials.json")
    d = json.load(open(path, encoding="utf8"))
    if "bne_cat_plate" not in d:
        import uuid
        d["bne_cat_plate"] = {"name": "bne_cat_plate", "mapTo": "bne_cat_plate", "class": "Material", "version": 1.5,
                              "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, "bne_cat_plate")),
                              "Stages": [{"metallicFactor": 0.0}, {}, {}, {}]}
        json.dump(d, open(path, "w", encoding="utf8"), indent=2)
    ing.link_material("catenary.materials.json", "bne_cat_plate", "bne_cat_plate")
    print("wrote", base + "_*.png")


if __name__ == "__main__":
    main()
