"""The level's preview (the freeroam level select): the whole Earth as the in-game map shows it zoomed out all the way
(EarthMap.vue mapmath MIN_Z = 2), from Sentinel-2 cloudless 2016 by EOX (CC BY 4.0: free to ship with its credit,
which the level's description carries; EOX's 2018+ mosaics are CC BY-NC-SA and not used anywhere).

    python3 tools/make_level_preview.py [--zoom 3] [--out mod/beamng_earth/levels/beamng_earth/beamng_earth_preview.jpg]

The tiles of --zoom (3: 8 x 8, 2048 px for the world; the map's own z2 view at twice its pixels) come through the
server's imagery code (its disk cache and network limits); the picture is the 16:9 band of the world centred on
CENTRE_LAT (from the Arctic coasts to Patagonia), scaled to SIZE.
"""
import argparse
import io
import math
import os
import sys

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
import imagery as imagerymod  # noqa: E402

SIZE = (1280, 720)
CENTRE_LAT = 12.0           # the band's middle: most land, little Antarctica
SOURCE = "eox2016"


def world(img, z):
    """The whole Web Mercator world at zoom z as one RGB array (256 * 2^z px square)."""
    n = 2 ** z
    out = np.zeros((256 * n, 256 * n, 3), np.uint8)
    src = imagerymod.SOURCES[SOURCE]
    for x in range(n):
        for y in range(n):
            data = img.raw(src, z, x, y)
            if data is None:
                raise SystemExit(f"no {SOURCE} tile {z}/{x}/{y}")
            out[256 * y:256 * (y + 1), 256 * x:256 * (x + 1)] = np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))
    return out


def band(rgb, centre_lat, aspect):
    """The full-width band of the world picture rgb with this aspect (w / h), centred on centre_lat."""
    side = rgb.shape[1]
    h = int(round(side / aspect))
    lat = math.radians(centre_lat)
    yc = (1.0 - math.log(math.tan(lat) + 1.0 / math.cos(lat)) / math.pi) / 2.0 * side
    y0 = int(round(min(max(yc - h / 2, 0), side - h)))
    return rgb[y0:y0 + h]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--zoom", type=int, default=3)
    ap.add_argument("--out", default=os.path.join(ROOT, "mod", "beamng_earth", "levels", "beamng_earth",
                                                  "beamng_earth_preview.jpg"))
    ap.add_argument("--cache-dir", default=os.path.expanduser("~/.cache/beamng-earth"))
    a = ap.parse_args(argv)
    img = imagerymod.Imagery(os.path.join(a.cache_dir, "imagery"), [SOURCE])
    pic = Image.fromarray(band(world(img, a.zoom), CENTRE_LAT, SIZE[0] / SIZE[1])).resize(SIZE, Image.LANCZOS)
    pic.save(a.out, quality=88, optimize=True, progressive=True)
    print(f"{a.out}: {pic.size[0]} x {pic.size[1]}, {os.path.getsize(a.out) // 1024} KB")


if __name__ == "__main__":
    main()
