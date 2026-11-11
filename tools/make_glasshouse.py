#!/usr/bin/env python3
"""make_glasshouse.py: the greenhouse glazing (bne_bld_glasshouse_*.png; its material is in buildings.materials.json).

    python3 tools/make_glasshouse.py

Westland's Venlo greenhouses (52.010 4.260 against the aerial photo, 2026-11-10) are pale grey glass panes in thin
aluminium bars. One texture repeat is PANES x PANES panes: the walls repeat every buildings.SIDING_UV_M (3 m: 0.75 m
panes), the roofs every ROOF_UV_M (6 m: 1.5 m panes). The panes are pale with a little per-pane variation and a smooth
(low-roughness) face, so the sky shows in them; the bars are light metal, a bit rougher. Vertex colours tint it per
building (clear, whitewashed, shaded)."""
import json
import os
import sys
import uuid

import numpy as np
from PIL import Image

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "mod", "beamng_earth", "levels", "beamng_earth",
                   "art", "shapes", "beamngEarth")
NAME = "bne_bld_glasshouse"
N = 512
PANES = 4
BAR = 0.045                     # a bar's width, a share of a pane
PANE = (214, 220, 221)          # pale glass (a building's tint multiplies it)
BAR_RGB = (186, 189, 190)       # aluminium


def maps():
    rng = np.random.default_rng(1)
    t = (np.arange(N) + 0.5) / N * PANES
    u, v = np.meshgrid(t, t)
    fu, fv = u % 1.0, v % 1.0
    bar = (np.minimum(fu, 1 - fu) < BAR / 2) | (np.minimum(fv, 1 - fv) < BAR / 2)
    pu, pv = np.floor(u).astype(int), np.floor(v).astype(int)
    shade = 1.0 + 0.05 * (rng.random((PANES, PANES)) - 0.5) * 2         # each pane its own faint tone
    k = shade[pv, pu] * (1.0 - 0.04 * fv)                               # (and lighter toward its top: the sky)
    rgb = np.empty((N, N, 3))
    for c in range(3):
        rgb[..., c] = np.where(bar, BAR_RGB[c], PANE[c] * k)
    rough = np.where(bar, 140, 45).astype(np.uint8)
    nm = np.dstack([np.full((N, N), 128), np.full((N, N), 128), np.full((N, N), 255)]).astype(np.uint8)
    # the bars stand a little proud: their edges lean out
    edge_u = bar & ~np.roll(bar, 1, 1)
    edge_d = bar & ~np.roll(bar, -1, 1)
    nm[edge_u, 0] = 100
    nm[edge_d, 0] = 156
    edge_l = bar & ~np.roll(bar, 1, 0)
    edge_r = bar & ~np.roll(bar, -1, 0)
    nm[edge_l, 1] = 100
    nm[edge_r, 1] = 156
    return np.clip(rgb + 0.5, 0, 255).astype(np.uint8), nm, rough


def main():
    rgb, nm, rough = maps()
    base = os.path.join(OUT, NAME)
    Image.fromarray(rgb, "RGB").save(base + "_b.color.png")
    Image.fromarray(nm, "RGB").save(base + "_nm.normal.png")
    Image.fromarray(rough, "L").save(base + "_r.data.png")
    path = os.path.join(OUT, "buildings.materials.json")
    d = json.load(open(path, encoding="utf8"))
    vfs = "/levels/beamng_earth/art/shapes/beamngEarth/" + NAME
    d[NAME] = {"name": NAME, "mapTo": NAME, "class": "Material", "version": 1.5,
               "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, NAME)),
               "Stages": [{"baseColorMap": vfs + "_b.color.png", "normalMap": vfs + "_nm.normal.png",
                           "roughnessMap": vfs + "_r.data.png", "metallicFactor": 0, "baseColorFactor": [1, 1, 1, 1],
                           "vertColor": True}, {}, {}, {}],
               "translucent": False}
    with open(path, "w", encoding="utf8") as f:
        json.dump(d, f, indent=2)
        f.write("\n")
    print("wrote", base + "_*.png", "mean", (rgb.reshape(-1, 3).mean(0) / 255).round(3))


if __name__ == "__main__":
    sys.exit(main())
