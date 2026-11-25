"""make_precip_textures.py: the rain object's streak (art/weather/bngEarth_raindrop.color.png, PrecipitationData
bngEarthRainDrop) and the snow object's flakes (levels/beamng_earth/art/weather/bngEarth_snowflake.color.png, a 2 x 2
atlas for the PrecipitationData bngEarthSnowflake, dropsPerSide 2: art/weather/precipitation.datablocks.json).

Why (the weather study 2026-11-24, its probe P10: snow was invisible in every broker shot): the stock datablock the
level used, Snow_menu, draws Particle_snow.dds, a 1024 px sheet of ~70 scattered specks made for the main menu's huge
sprites, and with the default dropsPerSide 4 each drop showed one 256 px cell of it: at our 0.25 m drop size each
speck was about a centimetre across, most cells nearly empty. Here each drop is one flake filling its cell: four
shapes (a soft round flake, an oval one, a wet clump of three, a small six-armed crystal softened), white, the alpha
the shape (premultiplied edges are not needed: the precipitation shader blends by alpha).

The rain streak (phase A's daylight check, 2026-11-24: the stock rain_drop streak's alpha peaks at 0.29, so even a
storm read as dry at noon in Tokyo while the same drops showed at night): the same shape (a thin vertical streak in a
square, the drop billboard), its alpha up to RAIN_ALPHA, a cool white.

    python3 tools/make_precip_textures.py
    python3 tools/mod_textures_dds.py mod/beamng_earth/levels/beamng_earth/art/weather/bngEarth_snowflake.color.png \
        mod/beamng_earth/levels/beamng_earth/art/weather/bngEarth_raindrop.color.png

Deterministic: no randomness, the same bytes every run.
"""
import math
import os

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "mod", "beamng_earth", "levels", "beamng_earth", "art", "weather",
                   "bngEarth_snowflake.color.png")
RAIN_OUT = OUT.replace("bngEarth_snowflake", "bngEarth_raindrop")
CELL = 64            # px per flake (the atlas is 2 x 2 cells)
RAIN_PX = 128        # the streak's square
RAIN_ALPHA = 0.65    # its peak (stock rain_drop: 0.29)
RAIN_SIGMA_PX = 2.6  # its half width (gaussian)
RAIN_RGB = (232, 238, 245)
SOFT = 0.5           # the edge's softness, share of the radius (a flake in motion is soft)


def blob(x, y, cx, cy, rx, ry, angle=0.0):
    """Alpha (0..1) of a soft ellipse at (cx, cy), radii rx, ry (cell units 0..1), turned by angle."""
    c, s = math.cos(angle), math.sin(angle)
    dx, dy = x - cx, y - cy
    u, v = (dx * c + dy * s) / rx, (-dx * s + dy * c) / ry
    d = np.sqrt(u * u + v * v)
    return np.clip((1.0 - d) / SOFT, 0.0, 1.0) ** 1.5


def crystal(x, y):
    """A six-armed flake, softened: arms of a thin ellipse every 60 degrees and a core."""
    a = blob(x, y, 0.5, 0.5, 0.16, 0.16)
    for k in range(3):
        a = np.maximum(a, 0.85 * blob(x, y, 0.5, 0.5, 0.40, 0.07, k * math.pi / 3))
    return a


def cell(kind):
    t = (np.arange(CELL) + 0.5) / CELL
    x, y = np.meshgrid(t, t)
    if kind == 0:
        a = blob(x, y, 0.5, 0.5, 0.40, 0.40)
    elif kind == 1:
        a = blob(x, y, 0.5, 0.5, 0.42, 0.30, 0.6)
    elif kind == 2:
        a = np.maximum.reduce([blob(x, y, 0.40, 0.42, 0.26, 0.24), blob(x, y, 0.62, 0.48, 0.22, 0.22),
                               blob(x, y, 0.48, 0.66, 0.20, 0.19)])
    else:
        a = crystal(x, y)
    return np.clip(a * 0.95, 0.0, 1.0)


def raindrop():
    t = np.arange(RAIN_PX) + 0.5
    x, y = np.meshgrid(t, t)
    across = np.exp(-0.5 * ((x - RAIN_PX / 2) / RAIN_SIGMA_PX) ** 2)
    v = (y - 4) / (RAIN_PX - 8)                               # 0 at the top end .. 1 at the bottom
    along = np.clip(np.sin(np.clip(v, 0, 1) * math.pi), 0, 1) ** 0.6 * (0.55 + 0.45 * np.clip(v, 0, 1))
    a = np.clip(across * along * RAIN_ALPHA, 0, 1)
    img = np.zeros((RAIN_PX, RAIN_PX, 4), dtype=np.uint8)
    img[..., :3] = RAIN_RGB
    img[..., 3] = np.round(a * 255).astype(np.uint8)
    return img


def main():
    Image.fromarray(raindrop(), "RGBA").save(RAIN_OUT)
    print(RAIN_OUT)
    atlas = np.zeros((2 * CELL, 2 * CELL, 4), dtype=np.uint8)
    atlas[..., :3] = 255
    for k in range(4):
        r, c = divmod(k, 2)
        atlas[r * CELL:(r + 1) * CELL, c * CELL:(c + 1) * CELL, 3] = np.round(cell(k) * 255).astype(np.uint8)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    Image.fromarray(atlas, "RGBA").save(OUT)
    print(OUT)


if __name__ == "__main__":
    main()
