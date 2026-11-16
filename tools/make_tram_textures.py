#!/usr/bin/env python3
"""make_tram_textures.py: the tram rails' textures (BNE_TRAM, server/tramtrack.py RAIL and STREET) and their materials.

Rail (bne_tram_rail): an opaque ribbon, steel head in the middle, dark flange groove either side. Slab (bne_tram_slab):
the concrete slabs a street track lies in in the ex-USSR; elsewhere the road's own asphalt is used (tramtrack.paving).
Nothing here needs an alpha channel: a mod's colour map is not read for alpha in the game (a cut-out decal came out
solid black).
"""
import json
import os
import uuid

import numpy as np
from PIL import Image

OUT = os.path.join("mod", "beamng_earth", "levels", "beamng_earth", "art", "shapes", "beamngEarth")
W, H = 64, 512          # across x along (px)
PREFIX = "/levels/beamng_earth/art/shapes/beamngEarth/"


def tile_noise(h, w, octaves, rng):
    """Value noise periodic along the first axis (the ribbon's length), 0..1."""
    out = np.zeros((h, w))
    amp, tot = 1.0, 0.0
    for o in range(octaves):
        ny, nx = 4 * 2 ** o, max(2, 2 * 2 ** o)
        g = rng.random((ny, nx + 1))
        g[-1] = g[0]
        ys = np.linspace(0, ny, h, endpoint=False)
        xs = np.linspace(0, nx, w)
        y0 = ys.astype(int); fy = (ys - y0)[:, None]
        x0 = np.minimum(xs.astype(int), nx - 1); fx = (xs - x0)[None, :]
        y1 = (y0 + 1) % ny
        v = (g[y0][:, x0] * (1 - fy) + g[y1][:, x0] * fy) * (1 - fx) + (g[y0][:, x0 + 1] * (1 - fy) + g[y1][:, x0 + 1] * fy) * fx
        out += amp * v
        tot += amp
        amp *= 0.5
    return out / tot


def build():
    rng = np.random.default_rng(20261106)
    u = np.linspace(-1, 1, W)[None, :]                      # across: -1 .. 1
    n = tile_noise(H, W, 5, rng)
    streak = tile_noise(H, W, 3, np.random.default_rng(7))
    head = np.clip(1.0 - (np.abs(u) / 0.55) ** 4, 0, 1)      # the polished head, 55 % of the width
    worn = np.clip(1.0 - (np.abs(u) / 0.22) ** 2, 0, 1)      # the wheels' bright band
    steel = np.array([128, 130, 134], np.float64)
    bright = np.array([176, 178, 182], np.float64)
    rust = np.array([30, 27, 25], np.float64)       # (the dark flange groove either side of the head)
    col = rust[None, None, :] * (1 - head[..., None]) + steel[None, None, :] * head[..., None]
    col = col * (1 - 0.65 * worn[..., None]) + bright[None, None, :] * 0.65 * worn[..., None]
    shade = 0.82 + 0.36 * n + 0.10 * (streak - 0.5)
    col = np.clip(col * shade[..., None], 0, 255)
    rough = np.clip(0.72 - 0.38 * head - 0.12 * worn + 0.22 * (n - 0.5), 0.12, 1.0)
    # normal across: the head's crown (z up, x across)
    slope = -2.2 * u * (np.abs(u) < 0.6)
    nx = slope + 0.25 * (n - 0.5)
    ny = 0.15 * (streak - 0.5) * np.ones_like(u)
    nz = np.ones_like(nx)
    ln = np.sqrt(nx ** 2 + ny ** 2 + nz ** 2)
    normal = np.stack([nx / ln, ny / ln, nz / ln], -1) * 0.5 + 0.5
    return col.astype(np.uint8), (normal * 255).astype(np.uint8), (rough * 255).astype(np.uint8)



SLW, SLH = 256, 256     # the slab strip: across x along (px); tl 3 m along, u across the strip (gauge + 1 m)


def build_slab():
    """Concrete track slabs: light grey concrete with a transverse joint, a thin longitudinal one in the middle, wear."""
    rng = np.random.default_rng(20261108)
    n = tile_noise(SLH, SLW, 5, rng)
    fine = np.random.default_rng(3).random((SLH, SLW))
    stain = tile_noise(SLH, SLW, 2, np.random.default_rng(5))
    base = np.array([152.0, 150.0, 145.0])
    shade = 0.80 + 0.30 * n + 0.10 * (fine - 0.5) - 0.12 * stain
    col = np.clip(base[None, None, :] * shade[..., None], 0, 255)
    joint = np.zeros((SLH, SLW))
    joint[:3, :] = 1.0                                              # transverse joint at the tile's seam
    joint[:, SLW // 2 - 1:SLW // 2 + 1] = np.maximum(joint[:, SLW // 2 - 1:SLW // 2 + 1], 0.7)
    col = col * (1 - 0.55 * joint[..., None])
    rough = np.clip(0.82 + 0.15 * (n - 0.5) + 0.1 * joint, 0.3, 1.0)
    gx = np.gradient(n, axis=1) * 6.0
    gy = np.gradient(n, axis=0) * 6.0
    nx = gx + joint * 0.4
    ln = np.sqrt(nx ** 2 + gy ** 2 + 1.0)
    normal = np.stack([nx / ln, gy / ln, 1.0 / ln], -1) * 0.5 + 0.5
    return col.astype(np.uint8), (normal * 255).astype(np.uint8), (rough * 255).astype(np.uint8)

def main():
    col, nrm, rgh = build()
    os.makedirs(OUT, exist_ok=True)
    Image.fromarray(col, "RGB").save(os.path.join(OUT, "bne_tram_rail_b.color.png"))
    Image.fromarray(nrm, "RGB").save(os.path.join(OUT, "bne_tram_rail_nm.normal.png"))
    Image.fromarray(rgh, "L").save(os.path.join(OUT, "bne_tram_rail_r.data.png"))
    name = "bne_tram_rail"
    mat = {name: {"name": name, "mapTo": name, "class": "Material", "version": 1.5,
                  "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, "beamng-earth/" + name)),
                  "Stages": [{"baseColorMap": PREFIX + "bne_tram_rail_b.color.png",
                              "normalMap": PREFIX + "bne_tram_rail_nm.normal.png",
                              "roughnessMap": PREFIX + "bne_tram_rail_r.data.png",
                              "metallicFactor": 0.55, "baseColorFactor": [1, 1, 1, 1]}, {}, {}, {}],
                  "groundType": "METAL", "translucent": False}}
    scol, snrm, srgh = build_slab()
    Image.fromarray(scol, "RGB").save(os.path.join(OUT, "bne_tram_slab_b.color.png"))
    Image.fromarray(snrm, "RGB").save(os.path.join(OUT, "bne_tram_slab_nm.normal.png"))
    Image.fromarray(srgh, "L").save(os.path.join(OUT, "bne_tram_slab_r.data.png"))
    sname = "bne_tram_slab"
    mat[sname] = {"name": sname, "mapTo": sname, "class": "Material", "version": 1.5,
                  "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, "beamng-earth/" + sname)),
                  "Stages": [{"baseColorMap": PREFIX + "bne_tram_slab_b.color.png",
                              "normalMap": PREFIX + "bne_tram_slab_nm.normal.png",
                              "roughnessMap": PREFIX + "bne_tram_slab_r.data.png",
                              "metallicFactor": 0, "baseColorFactor": [1, 1, 1, 1]}, {}, {}, {}],
                  "groundType": "CONCRETE", "translucent": False}
    with open(os.path.join(OUT, "tram.materials.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(mat, f, indent=2)
        f.write("\n")
    print("wrote", name, sname, col.shape, scol.shape)


if __name__ == "__main__":
    main()
