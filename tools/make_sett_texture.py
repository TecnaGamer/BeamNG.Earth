#!/usr/bin/env python3
"""The roundabout apron's granite sett texture (plan 038) from the user's generated picture assets/ai_inbox/granite_sett.png:
cropped to a whole number of the setts' repeats (the crop whose next lines match its first best), the light evened out,
resized to 1024 px, the wrap seam blended, written as bne_sidewalk_sett.color.png (sRGB albedo), .normal.png (from the
blurred luminance as height, an even strength) and .data.png (grey roughness, the joints rougher) to
levels/beamng_earth/art/shapes/beamngEarth/roundabouts/. The material (bne_sidewalk_sett, sidewalks.MATERIALS' last) is in
buildings.materials.json and names these files.

    python tools/make_sett_texture.py
"""
import os

import numpy as np
from PIL import Image, ImageFilter

from make_park_pavers import blend_seams, even_light, seam

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SRC = os.path.join(ROOT, "assets", "ai_inbox", "granite_sett.png")
OUT = os.path.join(ROOT, "mod", "beamng_earth", "levels", "beamng_earth", "art", "shapes", "beamngEarth", "roundabouts")
NAME = "bne_sidewalk_sett"
SIZE = 1024
NORMAL_STRENGTH = 2.2
BLEND_PX = 48           # the generated picture is not tileable: a wide cross-fade of its wrap seam


def normal_map(rgb):
    """Tangent-space normal (OpenGL, +Y up) of a tile from its luminance, wrapping at the edges."""
    lum = rgb.mean(2)
    h = np.asarray(Image.fromarray(np.clip(lum, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(1.4)), float) / 255
    dx = (np.roll(h, -1, 1) - np.roll(h, 1, 1)) * 0.5 * SIZE / 64
    dy = (np.roll(h, -1, 0) - np.roll(h, 1, 0)) * 0.5 * SIZE / 64
    n = np.stack([-dx * NORMAL_STRENGTH, dy * NORMAL_STRENGTH, np.ones_like(h)], 2)
    n /= np.linalg.norm(n, axis=2, keepdims=True)
    return ((n * 0.5 + 0.5) * 255).astype(np.uint8)


def roughness(rgb):
    """Grey roughness (the dark joints rougher, the stone tops 0.8), as the stock *_r.data.png roughness maps."""
    lum = rgb.mean(2)
    lo, hi = np.percentile(lum, 5), np.percentile(lum, 95)
    t = np.clip((lum - lo) / max(hi - lo, 1.0), 0, 1)
    rough = 0.97 - 0.17 * t
    return np.repeat((rough * 255)[:, :, None], 3, 2).astype(np.uint8)


def build():
    os.makedirs(OUT, exist_ok=True)
    a = np.asarray(Image.open(SRC).convert("RGB"), float)
    h, w, _ = a.shape
    n, cx, cy = min(h, w), 0.0, 0.0       # (the whole picture: no crop of it matches its first lines better than the seam blend)
    a = a[:n, :n]
    before = seam(a)
    a = even_light(a)
    img = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)).resize((SIZE, SIZE), Image.LANCZOS)
    a = blend_seams(np.asarray(img, float), BLEND_PX)
    img = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)).filter(ImageFilter.UnsharpMask(1.2, 40, 2))
    rgb = np.asarray(img, float)
    img.save(os.path.join(OUT, f"{NAME}.color.png"))
    Image.fromarray(normal_map(rgb)).save(os.path.join(OUT, f"{NAME}.normal.png"))
    Image.fromarray(roughness(rgb)).save(os.path.join(OUT, f"{NAME}.data.png"))
    print(f"crop {n}x{n} of {w}x{h} (match {cx:.1f}, {cy:.1f}); seam x/y {before[0]:.1f}/{before[1]:.1f}"
          f" -> {seam(rgb)[0]:.1f}/{seam(rgb)[1]:.1f}; mean {rgb.mean():.0f}")


if __name__ == "__main__":
    build()
