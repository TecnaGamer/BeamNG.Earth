"""Grass underlay textures (docs/new/013_grass-underlay): ChatGPT pictures in assets/ai_inbox/orig/grass/ ->
mod/beamng_earth/art/beamngEarth/grass/ground/<name>_{b.png,nm.normal.png,r.data.png,ao.data.png}.

Same file naming and colour handling as tools/make_crop_ai_assets.py (the engine multiplies the aerial photo by a detail /
macro colour map; plain .png is read as linear, a mean of 0.5 is neutral, so the luminance mean is normalised to 0.5 and
only a part of the picture's own colour (CHROMA) is kept: the photo carries the colour, the grass texture the structure
and the tone). Tones: lush, dry, hay, pasture (mown stripes) as macro maps; close / close_dry as detail maps.

    python tools/make_grass_ai_assets.py [src_dir] [out_dir]
"""
import json
import os
import sys

import numpy as np
from PIL import Image, ImageFilter

from make_crop_ai_assets import flatten_light, normal_from_height, seamless

HERE = os.path.dirname(os.path.abspath(__file__))
IN = os.path.join(HERE, "..", "assets", "ai_inbox", "orig", "grass")
OUT = os.path.join(HERE, "..", "mod", "beamng_earth", "art", "beamngEarth", "grass")
TILE = 1024
SMALL = TILE        # (the terrain material set wants every detail / macro map at its one size, 1024: 256 px roughness and AO were refused, 2026-11-05)

# name -> (source picture, chroma kept (1 = the picture's own colour, 0 = grey), light flattening amount, normal strength,
# mean level (0.5 = neutral, below darkens the photo). Too bright / saturated: a lime "salad" green and sun-struck far slopes
# (the user, 2026-11-05), so dark and nearly grey: the photo carries the colour.)
TEXTURES = {
    "lush": ("grass_lush", 0.75, 0.5, 3.0, 0.42),
    "dry": ("grass_dry", 0.5, 0.5, 3.0, 0.45),
    "hay": ("grass_hay", 0.5, 0.5, 3.0, 0.47),
    "pasture": ("grass_pasture_mown", 0.45, 0.0, 2.0, 0.36),    # (the stripes are light: not flattened)
    "close": ("grass_close", 0.8, 0.4, 4.0, 0.46),
    "close_dry": ("grass_close_dry", 0.6, 0.4, 4.0, 0.48),
}
# (lush and close are the second ChatGPT pictures, natural dark green: not turned)
# the hue each map is turned to (its mean colour; the pattern stays): the pictures' greens read lime / "salad" in game, the
# user wants a dark grass green (2026-11-05)
HUE = {"pasture": (60, 86, 50), "dry": (98, 100, 62),
       "close_dry": (100, 100, 64)}
BLUE = 1.35      # the pictures' greens have almost no blue (57, 92, 14): lime; lifted before the chroma is cut


def seam_ratio(a):
    """How much bigger the jump across the tile's wrap-around edges is than between neighbouring columns / rows (1 = none)."""
    lum = a.mean(2)
    inner = (np.abs(np.diff(lum, axis=1)).mean() + np.abs(np.diff(lum, axis=0)).mean()) / 2
    edge = (np.abs(lum[:, 0] - lum[:, -1]).mean() + np.abs(lum[0, :] - lum[-1, :]).mean()) / 2
    return float(edge / max(inner, 1e-3))


def build(src, out):
    gdir = os.path.join(out, "ground")
    os.makedirs(gdir, exist_ok=True)
    stats = {}
    for name, (pic, chroma, flat, nstr, level) in TEXTURES.items():
        im = Image.open(os.path.join(src, pic + ".png")).convert("RGB").resize((TILE, TILE), Image.LANCZOS)
        a = np.asarray(im, np.float32)
        if flat > 0:
            a = flatten_light(a, amount=flat)
        before = seam_ratio(a)
        if before > 1.6:                    # a visible wrap-around seam: cross-faded from a half-shifted copy
            a = seamless(a)
        after = seam_ratio(a)
        a = np.clip(a, 0, 255)
        if name in HUE:
            m = a.reshape(-1, 3).mean(0)
            t = np.array(HUE[name], np.float32)
            a = a * (t / t.mean()) / (m / m.mean())
        else:
            a = a * np.array([1.0, 1.0, BLUE], np.float32)
        lum0 = a.mean(2, keepdims=True)
        norm = a * (255.0 * level / max(float(lum0.mean()), 1.0))
        lum1 = norm.mean(2, keepdims=True)
        norm = lum1 + (norm - lum1) * chroma
        norm = np.clip(norm - (norm.mean() - 255.0 * level), 0, 255)
        Image.fromarray(norm.astype(np.uint8), "RGB").save(os.path.join(gdir, f"{name}_b.png"), optimize=True)
        lum = norm.mean(2) / 255.0
        hgt = np.asarray(Image.fromarray((lum * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.9)), np.float32) / 255.0
        Image.fromarray(normal_from_height(hgt, nstr), "RGB").save(os.path.join(gdir, f"{name}_nm.normal.png"), optimize=True)
        rough = np.clip(0.82 + (0.5 - lum) * 0.2, 0.65, 0.95)       # grass is matte
        small = lambda x: Image.fromarray((np.dstack([x, x, x]) * 255).astype(np.uint8), "RGB").resize((SMALL, SMALL), Image.BILINEAR)
        small(rough).save(os.path.join(gdir, f"{name}_r.data.png"), optimize=True)
        ao = np.clip(0.6 + 0.4 * np.clip((lum - lum.min()) / max(lum.max() - lum.min(), 1e-3), 0, 1), 0, 1)
        small(ao).save(os.path.join(gdir, f"{name}_ao.data.png"), optimize=True)
        stats[name] = {"mean": [round(float(v), 1) for v in a.reshape(-1, 3).mean(0)],
                       "seam": [round(before, 2), round(after, 2)]}
    with open(os.path.join(gdir, "grass_assets.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=1)
    return stats


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else IN
    out = sys.argv[2] if len(sys.argv) > 2 else OUT
    print(json.dumps(build(src, out), indent=1))
