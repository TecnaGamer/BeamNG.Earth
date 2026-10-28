"""build_sky_windows.py: the skyline buildings' lit windows at night (buildings.py _sky_mesh, material bne_bld_sky;
nightlights.lua turns them on after dusk): bne_sky_night_e.png beside the building materials, an emissive map of
BAYS x FLOORS windows (SKY_BAY_M x SKY_FLOOR_M each in the skyline's wall UVs), about LIT of them lit, each a room's
light (warm, now and then cool white). The skyline is 1-3 miles off (the user, 2026-10-27: "extend the street and
window lights as far as the buildings render"): its windows are a pixel or less there, the map's mip levels their
mean, a wall's soft speckled glow. Deterministic.

    python3 tools/build_sky_windows.py
"""
import os

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "mod", "beamng_earth", "levels", "beamng_earth", "art", "shapes", "beamngEarth",
                   "bne_sky_night_e.png")
BAYS, FLOORS = 16, 16       # windows across and up the map (buildings.py SKY_BAY_M, SKY_FLOOR_M)
CELL = 16                   # px a window's cell
WIN = (3, 4, 13, 12)        # px of the cell the window fills (x0, y0, x1, y1 from the top)
LIT = 0.4                   # share lit (the plaster sheet's ~40 %)
WARM, COOL = (255, 196, 128), (220, 228, 255)


def main():
    rng = np.random.default_rng(20261027)
    img = np.zeros((FLOORS * CELL, BAYS * CELL, 3), np.uint8)
    for f in range(FLOORS):
        for b in range(BAYS):
            if rng.random() >= LIT:
                continue
            c = np.array(COOL if rng.random() < 0.15 else WARM, np.float64) * (0.6 + 0.4 * rng.random())
            y0, x0 = f * CELL, b * CELL
            img[y0 + WIN[1]:y0 + WIN[3], x0 + WIN[0]:x0 + WIN[2]] = np.clip(c, 0, 255).astype(np.uint8)
    Image.fromarray(img).save(OUT)
    print(OUT, img.shape)


if __name__ == "__main__":
    main()
