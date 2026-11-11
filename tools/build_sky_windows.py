"""build_sky_windows.py: the skyline buildings' lit windows at night (buildings.py _sky_mesh, material bne_bld_sky;
nightlights.lua turns them on after dusk): bne_sky_night_e.png beside the building materials, an emissive map of
BAYS x FLOORS windows (SKY_BAY_M x SKY_FLOOR_M each in the skyline's wall UVs), about LIT of them lit, each a room's
light (warm, now and then cool white) at its own level, and fewer of them late at night (the night's steps, as
tools/build_night_windows.py: bne_sky_night_e_late.png from 23:15, _dawn.png from 3:00, each lighting a subset of the
one before; the skyline never goes dark: about three fifths of the evening's at 3 am). The skyline is 1-3 miles
off (the user, 2026-10-27: "extend the street and window lights as far as the buildings render"): its windows are a
pixel or less there, the map's mip levels their mean, a wall's soft speckled glow. Deterministic.

    python3 tools/build_sky_windows.py
"""
import os

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "mod", "beamng_earth", "levels", "beamng_earth", "art", "shapes", "beamngEarth",
                   "bne_sky_night_e{}.png")
STEPS = ("", "_late", "_dawn")    # (build_night_windows.STEPS)
BAYS, FLOORS = 16, 16       # windows across and up the map (buildings.py SKY_BAY_M, SKY_FLOOR_M)
CELL = 16                   # px a window's cell
WIN = (3, 4, 13, 12)        # px of the cell the window fills (x0, y0, x1, y1 from the top)
LIT = 0.4                   # share lit (the plaster sheet's ~40 %)
# each lit one's level: (share, level) (the user, 2026-11-08: "the brightness of windows should be randomized at
# night"; 0.6-1 drew them alike: the glow saturates the screen from ~0.3 of full, S-moreo's PR #916)
LEVELS = ((0.45, 1.0), (0.3, 0.2), (0.25, 0.06))
# the step each lit one goes dark at: (share, step; 0 lit all night)
OUTS = ((0.68, 0), (0.16, 1), (0.16, 2))
WARM, COOL = (255, 196, 128), (220, 228, 255)


def _draw(u, shares):
    acc = 0.0
    for share, v in shares:
        acc += share
        if u < acc:
            return v
    return shares[-1][1]


def main():
    rng = np.random.default_rng(20261027)
    imgs = [np.zeros((FLOORS * CELL, BAYS * CELL, 3), np.uint8) for _ in STEPS]
    for f in range(FLOORS):
        for b in range(BAYS):
            if rng.random() >= LIT:
                continue
            cool = rng.random() < 0.15
            level = _draw(rng.random(), [(s, v) for s, v in LEVELS])
            c = np.array(COOL if cool else WARM, np.float64) * level * (0.8 + 0.2 * rng.random())
            out = _draw(rng.random(), OUTS)
            y0, x0 = f * CELL, b * CELL
            for step, img in enumerate(imgs):
                if out == 0 or step < out:
                    img[y0 + WIN[1]:y0 + WIN[3], x0 + WIN[0]:x0 + WIN[2]] = np.clip(c, 0, 255).astype(np.uint8)
    for img, suffix in zip(imgs, STEPS):
        Image.fromarray(img).save(OUT.format(suffix))
        print(OUT.format(suffix), img.shape, f"{(img.max(2) > 0).mean():.1%} of pixels lit")


if __name__ == "__main__":
    main()
