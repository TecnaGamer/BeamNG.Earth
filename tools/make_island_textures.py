#!/usr/bin/env python3
"""The roundabout islands' planting textures (plan 038): bne_island_lawn / _shrubs / _gravel / _mulch (sidewalks.MATERIALS
8-11) from the user's generated pictures assets/ai_inbox/island_<name>.png; a picture not there yet gets a procedural
stand-in (noise in the material's colours), so the world already has a planting. Each is the whole picture with its wrap
seam cross-faded, its light evened out, 1024 px; written as <name>.color.png (sRGB albedo), .normal.png and .data.png
(grey roughness) to levels/beamng_earth/art/shapes/beamngEarth/roundabouts/, with the materials added to
buildings.materials.json.

    python tools/make_island_textures.py
"""
import json
import os
import re
import uuid

import numpy as np
from PIL import Image, ImageFilter

from make_park_pavers import blend_seams, even_light
from make_sett_texture import normal_map, roughness

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
INBOX = os.path.join(ROOT, "assets", "ai_inbox")
SHAPES = os.path.join(ROOT, "mod", "beamng_earth", "levels", "beamng_earth", "art", "shapes", "beamngEarth")
OUT = os.path.join(SHAPES, "roundabouts")
VFS = "/levels/beamng_earth/art/shapes/beamngEarth/roundabouts"
SIZE = 1024
BLEND_PX = 48
# name: (base colour, speck colour, blob scale px (bigger: coarser), speck share, normal strength, roughness)
VARIANTS = {
    "lawn": ((88, 118, 52), (60, 92, 38), 6, 0.55, 1.4, 0.95),
    "shrubs": ((58, 84, 44), (34, 56, 30), 22, 0.65, 2.4, 0.95),
    "gravel": ((150, 146, 136), (98, 94, 88), 5, 0.6, 2.6, 0.9),
    "mulch": ((84, 58, 40), (52, 34, 24), 7, 0.55, 2.0, 0.97),
}


def stand_in(name, seed):
    """A procedural tile in the variant's colours (wrapping noise at three scales plus fine speckle)."""
    base, speck, blob, share, _n, _r = VARIANTS[name]
    rng = np.random.default_rng(seed)
    acc = np.zeros((SIZE, SIZE))
    for k, wgt in ((blob, 0.5), (blob * 3, 0.3), (blob * 9, 0.2)):
        n = rng.random((SIZE // k + 1, SIZE // k + 1))
        n = np.tile(n, (1, 1))
        img = Image.fromarray((n * 255).astype(np.uint8)).resize((SIZE, SIZE), Image.BICUBIC)
        acc += wgt * np.asarray(img, float) / 255
    fine = rng.random((SIZE, SIZE))
    t = np.clip((acc - 0.5) * 2.2 * share + (fine - 0.5) * 0.9 * (1 - share) + 0.5, 0, 1)[:, :, None]
    rgb = np.array(speck, float) * (1 - t) + np.array(base, float) * t * 1.15
    return np.clip(rgb, 0, 255)


def process(name, rgb, strength):
    import make_sett_texture as ms
    ms.NORMAL_STRENGTH = strength
    img = Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8)).resize((SIZE, SIZE), Image.LANCZOS)
    a = even_light(np.asarray(img, float))
    a = blend_seams(a, BLEND_PX)
    img = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)).filter(ImageFilter.UnsharpMask(1.0, 30, 2))
    rgb = np.asarray(img, float)
    img.save(os.path.join(OUT, f"bne_island_{name}.color.png"))
    Image.fromarray(normal_map(rgb)).save(os.path.join(OUT, f"bne_island_{name}.normal.png"))
    r = roughness(rgb)
    Image.fromarray(r).save(os.path.join(OUT, f"bne_island_{name}.data.png"))


def material_json(name):
    mid = f"bne_island_{name}"
    return {"name": mid, "mapTo": mid, "class": "Material", "version": 1.5,
            "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, "beamng-earth:" + mid)),
            "Stages": [{"baseColorMap": f"{VFS}/{mid}.color.png", "normalMap": f"{VFS}/{mid}.normal.png",
                        "roughnessMap": f"{VFS}/{mid}.data.png", "metallicFactor": 0, "baseColorFactor": [1, 1, 1, 1]},
                       {}, {}, {}], "translucent": False, "castShadows": False}


def add_materials():
    """The four materials into buildings.materials.json, as text appended before its last brace (the file's own layout kept)."""
    p = os.path.join(SHAPES, "buildings.materials.json")
    s = open(p, encoding="utf-8", newline="").read()
    nl = "\r\n" if "\r\n" in s else "\n"
    for name in VARIANTS:
        key = f'"bne_island_{name}": {{'
        if key in s:
            continue
        block = json.dumps({f"bne_island_{name}": material_json(name)}, indent=2).split("\n")[1:-1]
        text = nl.join(block)
        end = s.rstrip().rfind("}")
        head = s[:end].rstrip()
        s = head + "," + nl + text + nl + "}" + nl
    open(p, "w", encoding="utf-8", newline="").write(s)
    json.load(open(p, encoding="utf-8"))


def build():
    os.makedirs(OUT, exist_ok=True)
    for k, name in enumerate(VARIANTS):
        src = os.path.join(INBOX, f"island_{name}.png")
        if os.path.exists(src):
            rgb = np.asarray(Image.open(src).convert("RGB"), float)
            note = "picture"
        else:
            rgb, note = stand_in(name, 100 + k), "stand-in"
        process(name, rgb, VARIANTS[name][4])
        print(f"{name}: {note}")
    add_materials()


if __name__ == "__main__":
    build()
