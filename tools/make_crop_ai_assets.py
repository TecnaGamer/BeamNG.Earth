#!/usr/bin/env python3
"""Crop plants and canopy ground textures from the ChatGPT-generated pictures (docs/new/009): the game files under
mod/beamng_earth/art/beamngEarth/crops/ (plants atlas) and .../crops/ground/ (one tileable canopy per crop layer).

Inputs (assets/ai_inbox/orig/, 1254² RGB tiles and 1024 x 1536 / 1536 x 1024 RGBA plants on a transparent background):
  <crop>_plant_NN.png, <crop>_canopy.png with crop = maize, wheat, sunflower, rapeseed, beet.

Plants: cropped to their alpha, scaled into the cells of a 2048² atlas (4 x 2 cells of 512 x 1024, plants standing on
the bottom edge, centred), the colour bled into the transparent edge (mips and filtering then do not fringe dark) and the
alpha thickened a little so thin stalks and ears survive the mips under alpha test. Cell order = groundcover.lua's CROP_DEF.
Canopies: made seamless (a half-tile roll blended with the original by a window that is 0 on the tile edges), the light
flattened (a very blurred luminance divided out), 1024², with a normal map from the luminance, a roughness and an AO map.

  python3 tools/make_crop_ai_assets.py [inbox_orig_dir] [out_dir]
"""
import json
import os
import sys

import numpy as np
from PIL import Image, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from make_crop_atlas import bleed  # noqa: E402

IN = os.path.join(HERE, "..", "assets", "ai_inbox", "orig")
OUT = os.path.join(HERE, "..", "mod", "beamng_earth", "art", "beamngEarth", "crops")
CELL_W, CELL_H, COLS = 512, 1024, 4
# (cell, source picture, mirrored): cereal x2, maize x2, sunflower x2, rape, beet
CELLS = (("wheat_plant_01", False), ("wheat_plant_01", True), ("maize_plant_01", False), ("maize_plant_01", True),
         ("sunflower_plant_01", False), ("sunflower_plant_02", False), ("rapeseed_plant_01", False),
         ("beet_plant_01", False))
# ground layer -> canopy picture
GROUND = {"cereal": "wheat_canopy", "maize": "maize_canopy", "sunflower": "sunflower_canopy", "rape": "rapeseed_canopy",
          "root": "beet_canopy"}
TILE = 1024
DETAIL_CHROMA = 0.4       # how much of the canopy's own colour the detail map keeps (1 = all, 0 = grey)
ATLAS_NAME = "crops_v5"


def crop_alpha(im, thr=24):
    a = np.asarray(im.getchannel("A"))
    ys, xs = np.nonzero(a > thr)
    pad = 3
    return im.crop((max(xs.min() - pad, 0), max(ys.min() - pad, 0), min(xs.max() + pad + 1, im.width),
                    min(ys.max() + pad + 1, im.height)))


def premult_resize(im, size):
    a = np.asarray(im, np.float32)
    al = a[..., 3:4] / 255.0
    pm = Image.fromarray(np.clip(a[..., :3] * al, 0, 255).astype(np.uint8))
    pa = Image.fromarray(a[..., 3].astype(np.uint8), "L")
    pm, pa = pm.resize(size, Image.LANCZOS), pa.resize(size, Image.LANCZOS)
    pm, pa = np.asarray(pm, np.float32), np.asarray(pa, np.float32)[..., None]
    rgb = np.where(pa > 1, pm / np.maximum(pa / 255.0, 1e-3), 0)
    return Image.fromarray(np.dstack([np.clip(rgb, 0, 255), pa]).astype(np.uint8), "RGBA")


def thicken(alpha, k=0.5):
    """alpha with a 1 px dilation mixed in (thin stems and ears keep their coverage in the mips)."""
    im = Image.fromarray(alpha, "L")
    d = np.asarray(im.filter(ImageFilter.MaxFilter(3)), np.float32)
    return np.clip(alpha * (1 - k) + d * k, 0, 255).astype(np.uint8)


def build_atlas(src, out):
    atlas = Image.new("RGBA", (CELL_W * COLS, CELL_H * 2), (0, 0, 0, 0))
    rects = []
    for i, (name, flip) in enumerate(CELLS):
        im = crop_alpha(Image.open(os.path.join(src, name + ".png")).convert("RGBA"))
        if flip:
            im = im.transpose(Image.FLIP_LEFT_RIGHT)
        if name.startswith("maize"):        # the tassels came out golden: a grown, not ripe field (green with pale tassels)
            a = np.asarray(im, np.float32).copy()
            warm = (a[..., 0] > a[..., 1] * 0.93) & (a[..., 3] > 0)
            a[warm, :3] *= np.array([0.80, 0.97, 0.74], np.float32)
            lum = a[..., :3].mean(2, keepdims=True)
            a[warm, :3] = a[warm, :3] * 0.72 + lum[warm] * 0.28
            im = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "RGBA")
        s = min(CELL_W / im.width, CELL_H / im.height)
        im = premult_resize(im, (max(1, round(im.width * s)), max(1, round(im.height * s))))
        cx, cy = (i % COLS) * CELL_W, (i // COLS) * CELL_H
        x, y = cx + (CELL_W - im.width) // 2, cy + CELL_H - im.height
        atlas.paste(im, (x, y))
        rects.append({"cell": i, "src": name, "flip": flip, "w": im.width, "h": im.height,
                      "uv": [round(x / atlas.width, 5), round(y / atlas.height, 5),
                             round(im.width / atlas.width, 5), round(im.height / atlas.height, 5)]})
    arr = np.asarray(atlas).copy()
    arr[..., 3] = thicken(arr[..., 3])
    inner = arr.copy()
    inner[..., 3] = np.where(arr[..., 3] > 235, 255, 0)      # the picture's own edge pixels are dark (a halo at close range):
    rgb = bleed(inner)                                       # the edge takes the colour of the solid leaf inside instead
    Image.fromarray(rgb, "RGB").save(os.path.join(out, ATLAS_NAME + "_b.color.png"), optimize=True)
    Image.fromarray(arr[..., 3], "L").save(os.path.join(out, ATLAS_NAME + "_o.data.png"), optimize=True)
    with open(os.path.join(out, ATLAS_NAME + "_cells.json"), "w") as f:
        json.dump(rects, f, indent=1)
    return rects


def window(n, lo=0.12, hi=0.42):
    """1-D weight: 0 at the tile edges, 1 from `hi` of the half-width inward (smoothstep between)."""
    t = np.abs(np.linspace(-1, 1, n))              # 1 at the edges, 0 in the middle
    d = 1.0 - t
    x = np.clip((d - lo) / (hi - lo), 0, 1)
    return x * x * (3 - 2 * x)


def seamless(a):
    h, w = a.shape[:2]
    r = np.roll(np.roll(a, h // 2, 0), w // 2, 1)
    m = (window(h)[:, None] * window(w)[None, :])[..., None]
    return a * m + r * (1 - m)


def flatten_light(a, sigma_frac=0.25, amount=0.7):
    lum = a.mean(2)
    s = int(a.shape[0] * sigma_frac)
    # blur with wrap by tiling 3x3 so the flattening is itself seamless
    t = np.tile(lum, (3, 3))
    b = np.asarray(Image.fromarray(np.clip(t, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(s)), np.float32)
    b = b[a.shape[0]:2 * a.shape[0], a.shape[1]:2 * a.shape[1]]
    gain = np.clip(b.mean() / np.maximum(b, 1.0), 0.65, 1.5)
    return a * (1 + (gain[..., None] - 1) * amount)


def normal_from_height(hgt, strength):
    """Tangent-space normal (RGB uint8, +Y up as the engine's .normal.png) from a height field, wrap-around."""
    gx = (np.roll(hgt, -1, 1) - np.roll(hgt, 1, 1)) * 0.5
    gy = (np.roll(hgt, -1, 0) - np.roll(hgt, 1, 0)) * 0.5
    nx, ny, nz = -gx * strength, gy * strength, np.ones_like(hgt)
    ln = np.sqrt(nx * nx + ny * ny + nz * nz)
    n = np.dstack([nx / ln, ny / ln, nz / ln]) * 0.5 + 0.5
    return (np.clip(n, 0, 1) * 255).astype(np.uint8)


def build_ground(src, out):
    gdir = os.path.join(out, "ground")
    os.makedirs(gdir, exist_ok=True)
    stats = {}
    for layer, name in GROUND.items():
        im = Image.open(os.path.join(src, name + ".png")).convert("RGB").resize((TILE, TILE), Image.LANCZOS)
        a = np.asarray(im, np.float32)
        a = flatten_light(a)
        a = seamless(a)
        a = np.clip(a, 0, 255)
        # The engine multiplies the photo by a detail colour map (stock ones are plain .png, read as linear, mean 0.5 =
        # neutral). A picture of leaves has a mean of ~0.35-0.5 in sRGB, ~0.1-0.2 in linear: black ground under the
        # plants (Suchy Dul, 2026-11-05). So: its luminance mean normalised to 0.5 (the leaf pattern and a part of the
        # green stay: the chroma is kept at DETAIL_CHROMA), saved as a plain .png like the stock detail maps.
        lum0 = a.mean(2, keepdims=True)
        norm = a * (127.5 / max(float(lum0.mean()), 1.0))
        lum1 = norm.mean(2, keepdims=True)
        norm = lum1 + (norm - lum1) * DETAIL_CHROMA
        norm = np.clip(norm - (norm.mean() - 127.5), 0, 255)
        rgb = Image.fromarray(norm.astype(np.uint8), "RGB")
        rgb.save(os.path.join(gdir, f"{layer}_b.png"), optimize=True)
        a = norm
        lum = a.mean(2) / 255.0
        hgt = np.asarray(Image.fromarray((lum * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(1.2)), np.float32) / 255.0
        Image.fromarray(normal_from_height(hgt, 5.0), "RGB").save(os.path.join(gdir, f"{layer}_nm.normal.png"), optimize=True)
        # data: leaf canopy is rough and matte (R roughness-ish channel used by the engine's roughness slot, G / B unused)
        rough = np.clip(0.78 + (0.5 - lum) * 0.25, 0.6, 0.95)
        Image.fromarray((np.dstack([rough, rough, rough]) * 255).astype(np.uint8), "RGB").save(
            os.path.join(gdir, f"{layer}_r.data.png"), optimize=True)
        ao = np.clip(0.55 + 0.45 * np.clip((lum - lum.min()) / max(lum.max() - lum.min(), 1e-3), 0, 1), 0, 1)
        Image.fromarray((np.dstack([ao, ao, ao]) * 255).astype(np.uint8), "RGB").save(
            os.path.join(gdir, f"{layer}_ao.data.png"), optimize=True)
        stats[layer] = [round(float(v), 1) for v in a.reshape(-1, 3).mean(0)]
    return stats


PH = "polyhaven"
SOIL_TILE_M = 2.0       # farm_soil covers about 2 x 2 m
# name -> (mean sRGB-free detail level the colour is normalised to, warm straw tint multiplier, chroma kept)
SOILS = {"soil_dark": (0.46, (1.0, 1.0, 1.0), 0.2), "soil_straw": (0.5, (1.1, 1.03, 0.85), 0.25)}
SOIL_CONTRAST = 0.65     # the soil's light / dark variation kept (the detail multiplies the photo twice as hard as it looks)


def build_soil(src, out):
    """Ground under the plants (detail slot, 0-40 m): Poly Haven farm_soil (CC0), two tones: a dark moist soil under maize,
    sunflower, rape and roots, a lighter one with straw tint under cereal. Detail colour maps are multiplied onto the photo
    (plain .png, linear: a mean of 0.5 is neutral, below darkens), so the level is set here. Same file naming as the canopy."""
    gdir = os.path.join(out, "ground")
    d16 = lambda f: np.asarray(Image.open(os.path.join(src, PH, f)))
    diff = np.asarray(Image.open(os.path.join(src, PH, "farm_soil_diff_2k.png")).convert("RGB"), np.float32)
    nrm = Image.open(os.path.join(src, PH, "farm_soil_nor_gl_2k.png")).convert("RGB")
    rough = d16("farm_soil_rough_2k.png").astype(np.float32) / 65535.0
    ao = d16("farm_soil_ao_2k.png").astype(np.float32) / 65535.0
    to = lambda a: Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)).resize((TILE, TILE), Image.LANCZOS)
    stats = {}
    for name, (level, tint, chroma) in SOILS.items():
        a = diff * np.array(tint, np.float32)
        lum = a.mean(2, keepdims=True)
        a = a * (255.0 * level / max(float(lum.mean()), 1.0))
        l2 = a.mean(2, keepdims=True)
        a = l2 + (a - l2) * chroma
        a = a.mean() + (a - a.mean()) * SOIL_CONTRAST
        to(a).save(os.path.join(gdir, f"{name}_b.png"), optimize=True)
        nrm.resize((TILE, TILE), Image.LANCZOS).save(os.path.join(gdir, f"{name}_nm.normal.png"), optimize=True)
        r = np.clip(rough * 0.9 + 0.1, 0, 1) * 255
        to(np.dstack([r, r, r])).save(os.path.join(gdir, f"{name}_r.data.png"), optimize=True)
        o = (0.55 + 0.45 * np.clip(ao, 0, 1)) * 255
        to(np.dstack([o, o, o])).save(os.path.join(gdir, f"{name}_ao.data.png"), optimize=True)
        stats[name] = [round(float(v), 1) for v in a.reshape(-1, 3).mean(0)]
    return stats


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else IN
    out = sys.argv[2] if len(sys.argv) > 2 else OUT
    os.makedirs(out, exist_ok=True)
    for r in build_atlas(src, out):
        print(r)
    print(build_ground(src, out))
    print(build_soil(src, out))
