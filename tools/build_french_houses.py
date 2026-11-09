"""Original tileable French-house textures and BeamNG materials, no photo pixels.

Integer drawing/noise only: no host fonts, downloaded imagery or random state.
Geometry is generated on each real footprint by server/francehouse_mesh.py.
"""
import argparse
import json
import math
import os
from pathlib import Path
import uuid

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/fr_houses"
VFS = "/levels/beamng_earth/art/shapes/beamngEarth/fr_houses"
SIZE = 512
SURFACES = ("stucco", "stone", "brick", "wood", "roller", "tile", "canal", "slate")


def noise(x, y, seed=0):
    n = np.multiply(np.asarray(x, np.uint32), np.uint32(374761393), dtype=np.uint32) ^ np.multiply(np.asarray(y, np.uint32), np.uint32(668265263), dtype=np.uint32)
    n ^= np.uint32(seed)
    n = np.multiply(n ^ (n >> 13), np.uint32(1274126177), dtype=np.uint32)
    return (n ^ (n >> 16)) & np.uint32(255)


def pixels(kind):
    y, x = np.indices((SIZE, SIZE), dtype=np.int32)
    grain = noise(x, y, 19).astype(np.int16) % 11 - 5
    if kind == "stucco":
        return np.clip(241 + grain + noise(x // 9, y // 9, 3).astype(np.int16) % 4, 0, 255).astype(np.uint8)
    if kind == "wood":
        plank = x // 64
        value = 218 + noise(plank, 0, 4).astype(np.int16) % 25
        value = value + noise(x // 3, y // 31, 5).astype(np.int16) % 18 - 9 + grain
        value[x % 64 < 3] = 150
        return np.clip(value, 0, 255).astype(np.uint8)
    if kind == "roller":
        value = 238 + grain // 2
        value[y % 16 < 2] -= 25
        return np.clip(value, 0, 255).astype(np.uint8)
    if kind in {"brick", "slate"}:
        height, width = (32, 96) if kind == "brick" else (64, 64)
        row = y // height
        xx = (x + (row % 2) * (width // 2)) % width
        shade = noise((x + (row % 2) * (width // 2)) // width, row, 7).astype(np.int16) % 29
        value = 208 + shade + grain
        mortar = (y % height < (3 if kind == "brick" else 2)) | (xx < 2)
        value[mortar] = 157 if kind == "brick" else 103
        return np.clip(value, 0, 255).astype(np.uint8)
    if kind == "canal":
        # Fixed semicircular cross-section, with overlapping courses.
        curve = np.array([math.isqrt(max(0, 32 * 32 - (i - 32) ** 2)) for i in range(64)], np.int16)
        value = 168 + curve[x % 64] * 2 + grain
        value += noise(x // 64, y // 128, 14).astype(np.int16) % 19
        value[y % 128 < 4] -= 33
        return np.clip(value, 0, 255).astype(np.uint8)
    if kind == "tile":
        im = Image.new("L", (SIZE, SIZE), 155)
        d = ImageDraw.Draw(im)
        for row in range(-2, 17):
            for col in range(-2, 10):
                xx, yy = col * 64 + (row % 2) * 32, row * 32
                shade = 208 + int(noise(np.array(col % 8), np.array(row % 16), 20)) % 31
                d.ellipse((xx, yy, xx + 63, yy + 63), fill=shade, outline=145, width=2)
                d.rectangle((xx + 1, yy, xx + 62, yy + 31), fill=shade)
        return np.clip(np.asarray(im).astype(np.int16) + grain, 0, 255).astype(np.uint8)
    # Periodic irregular fieldstone: Voronoi cells, with a narrow mortar joint.
    first = np.full((SIZE, SIZE), 10**9, np.int32)
    second, ident = first.copy(), np.zeros_like(first)
    for iy in range(-1, 9):
        for ix in range(-1, 9):
            seed = (iy % 8) * 8 + ix % 8
            px = ix * 64 + 18 + (seed * 17 % 27)
            py = iy * 64 + 18 + (seed * 23 % 27)
            dist = (x - px) ** 2 + (y - py) ** 2
            new = dist < first
            second = np.where(new, first, np.minimum(second, dist))
            first = np.minimum(first, dist)
            ident[new] = seed
    value = 207 + (ident * 23 % 39) + grain
    value[second - first < 210] = 153
    return np.clip(value, 0, 255).astype(np.uint8)


def build(out=OUT):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    materials = {}
    for kind in SURFACES:
        gray = pixels(kind)
        save_png(Image.fromarray(np.repeat(gray[:, :, None], 3, axis=2)), out / (kind + ".color.png"))
        h = gray.astype(np.int16)
        strength = 1 if kind == "stucco" else 2
        nx = np.clip(128 + (np.roll(h, 1, 1) - np.roll(h, -1, 1)) * strength, 1, 254)
        ny = np.clip(128 - (np.roll(h, 1, 0) - np.roll(h, -1, 0)) * strength, 1, 254)
        normal = np.stack([nx, ny, np.full_like(nx, 255)], axis=2).astype(np.uint8)
        save_png(Image.fromarray(normal), out / (kind + ".normal.png"))
        name = "bne_fr_house_" + kind
        stage = {"baseColorMap": VFS + "/" + kind + ".color.png",
                 "normalMap": VFS + "/" + kind + ".normal.png", "baseColorFactor": [1, 1, 1, 1],
                 "metallicFactor": 0, "roughnessFactor": .88, "vertColor": True}
        materials[name] = material(name, stage)
    for kind in ("trim", "glass", "glass_lit", "metal"):
        name = "bne_fr_house_" + kind
        stage = {"baseColorFactor": [1, 1, 1, 1], "vertColor": True,
                 "metallicFactor": .7 if kind == "metal" else .05,
                 "roughnessFactor": .23 if kind.startswith("glass") else .55}
        if kind == "glass_lit":
            save_png(Image.new("RGB", (4, 4), (255, 235, 200)), out / "glass.emissive.png")
            stage.update(emissiveMap=VFS + "/glass.emissive.png", emissiveFactor=[0, 0, 0])
        materials[name] = material(name, stage)
    (out / "main.materials.json").write_text(json.dumps(materials, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return materials


def save_png(im, path):
    tmp = path.with_suffix(path.suffix + ".tmp")
    im.save(tmp, format="PNG")
    os.replace(tmp, path)


def material(name, stage):
    return {"name": name, "mapTo": name, "class": "Material", "version": 1.5,
            "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, "bne:fr-house:" + name)),
            "Stages": [stage], "translucent": False, "doubleSided": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUT)
    args = parser.parse_args()
    build(args.output)
    print("French-house materials and original textures:", args.output)
