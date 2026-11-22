#!/usr/bin/env python3
"""make_localpoi_textures.py: the local POIs' tiling textures from generated pictures in assets/ai_inbox/.

    python tools/make_localpoi_textures.py

For each name below whose picture exists (assets/ai_inbox/localpoi_<short>.png): the seam blended over a band, the
light evened out (divided by its blur), resized to 1024, written as <name>.color.png beside the models (mod/.../localpoi),
with a normal map (height = blurred luminance, wrapped gradient) and a grey roughness map (.data.png, from the luminance).
bne_lp_ash has no picture: it is drawn here. Then main.materials.json is rewritten (tools/make_localpoi_models.py picks the
maps up when they exist). Run tools/make_localpoi_models.py's materials again by running this script, not the other way round.

Pictures (generated with an image model from a short prompt: a flat, evenly lit, tileable close-up of the material):
    localpoi_wood.png      weathered grey-brown boards          -> bne_lp_wood      (1 repeat = 1 m, grain along u)
    localpoi_shingle.png   mossy wood shingle / bitumen roof     -> bne_lp_shingle
    localpoi_stone.png     yellow-grey sandstone                -> bne_lp_stone
    localpoi_oak.png       dark oak beam                        -> bne_lp_oak       (crosses, stage C)
    localpoi_castiron.png  black-painted cast iron              -> bne_lp_iron
    localpoi_tin.png       old painted tin                      -> bne_lp_tin
"""
import json
import os
import sys

import numpy as np
from PIL import Image, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import make_localpoi_models as models  # noqa: E402

INBOX = os.path.join(HERE, "..", "assets", "ai_inbox")
OUT = models.OUT
SIZE = 1024
BLEND_PX = 96
# name -> (inbox file, normal strength, roughness (dark, light))
PICTURES = {
    "bne_lp_wood": ("localpoi_wood.png", 2.2, (0.95, 0.78)),
    "bne_lp_shingle": ("localpoi_shingle.png", 3.0, (0.92, 0.80)),
    "bne_lp_stone": ("localpoi_stone.png", 3.5, (0.96, 0.85)),
    "bne_lp_oak": ("localpoi_oak.png", 2.4, (0.95, 0.80)),
    "bne_lp_iron": ("localpoi_castiron.png", 2.0, (0.75, 0.55)),
    "bne_lp_tin": ("localpoi_tin.png", 1.5, (0.80, 0.55)),
}


def blend_seams(a, px=BLEND_PX):
    """Cross-fade the wrap seams of a (H, W, C) over px lines, both axes (the half-tile roll has its seam in the middle)."""
    a = a.copy()
    for axis in (0, 1):
        n = a.shape[axis]
        rolled = np.roll(a, n // 2, axis=axis)
        w = np.ones(n)
        ramp = np.linspace(0.0, 1.0, px)
        w[:px] = ramp
        w[-px:] = ramp[::-1]
        shape = [1, 1, 1]
        shape[axis] = n
        w = w.reshape(shape)
        a = a * w + rolled * (1 - w)
    return a


def even_light(a):
    img = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))
    blur = np.asarray(img.filter(ImageFilter.GaussianBlur(a.shape[0] / 4)), float)
    lum = blur.mean(2, keepdims=True)
    return a / np.maximum(lum, 1.0) * float(lum.mean())


def height_of(a):
    lum = a.mean(2)
    img = Image.fromarray(np.clip(lum, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(1.2))
    return np.asarray(img, float) / 255.0


def normal_map(a, strength):
    """Tangent-space normal (OpenGL-style, green up) from the blurred luminance, wrapped at the edges."""
    h = height_of(a)
    dx = (np.roll(h, -1, axis=1) - np.roll(h, 1, axis=1)) * 0.5 * strength * 8
    dy = (np.roll(h, -1, axis=0) - np.roll(h, 1, axis=0)) * 0.5 * strength * 8
    n = np.dstack((-dx, dy, np.ones_like(h)))
    n /= np.linalg.norm(n, axis=2, keepdims=True)
    return ((n * 0.5 + 0.5) * 255).astype(np.uint8)


def data_map(a, rough):
    """Grey roughness, as the stock *_r.data.png maps (the dark hollows rougher, the light faces rough)."""
    h = height_of(a)
    lo, hi = np.percentile(h, 5), np.percentile(h, 95)
    t = np.clip((h - lo) / max(hi - lo, 1e-6), 0, 1)
    r = rough[0] + (rough[1] - rough[0]) * t
    return np.repeat((r * 255).astype(np.uint8)[:, :, None], 3, 2)


def ash():
    """Fire-pit ash: near-black with grey flecks and a few dull embers."""
    rng = np.random.default_rng(7)
    base = rng.normal(0.0, 1.0, (SIZE // 4, SIZE // 4))
    img = Image.fromarray(np.clip(base * 40 + 128, 0, 255).astype(np.uint8)).resize((SIZE, SIZE), Image.BICUBIC)
    n = np.asarray(img, float) / 255.0
    fine = rng.normal(0.0, 1.0, (SIZE, SIZE)) * 0.06
    v = np.clip(0.10 + (n - 0.5) * 0.22 + fine, 0.03, 0.4)
    rgb = np.dstack((v * 255, v * 245, v * 235))
    embers = rng.random((SIZE, SIZE)) > 0.9992
    rgb[embers] = (150, 60, 20)
    return np.clip(rgb, 0, 255)


def save_maps(name, a, strength, rough):
    Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)).save(os.path.join(OUT, f"{name}.color.png"))
    Image.fromarray(normal_map(a, strength)).save(os.path.join(OUT, f"{name}.normal.png"))
    Image.fromarray(data_map(a, rough)).save(os.path.join(OUT, f"{name}.data.png"))


def build():
    os.makedirs(OUT, exist_ok=True)
    for name, (src, strength, rough) in PICTURES.items():
        path = os.path.join(INBOX, src)
        if not os.path.exists(path):
            print(f"{name}: no {src} in assets/ai_inbox yet (flat colour stays)")
            continue
        a = np.asarray(Image.open(path).convert("RGB"), float)
        a = even_light(a)
        img = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)).resize((SIZE, SIZE), Image.LANCZOS)
        a = blend_seams(np.asarray(img, float))
        a = np.asarray(Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)).filter(ImageFilter.UnsharpMask(1.2, 40, 2)),
                       float)
        seam = float(np.abs(a[:, 0] - a[:, -1]).mean()), float(np.abs(a[0] - a[-1]).mean())
        save_maps(name, a, strength, rough)
        print(f"{name}: written, wrap seam x/y {seam[0]:.1f}/{seam[1]:.1f}")
    save_maps("bne_lp_ash", ash(), 1.5, (1.0, 0.9))
    with open(os.path.join(OUT, "main.materials.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(models.materials_json(), fh, indent=2)
        fh.write("\n")


if __name__ == "__main__":
    build()
