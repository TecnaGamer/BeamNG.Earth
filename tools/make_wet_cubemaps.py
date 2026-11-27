#!/usr/bin/env python3
"""The puddles' reflection cubemaps (the puddle plan R1, ~/bnge-tests/puddles/PLAN_FINAL.md 1.7; wetlook.lua's new
puddle look, CubemapData objects in wetsky.materials.json, normalized by the scene's own sky light so they follow the
time of day): an overcast and a clearing sky, each with a built-up horizon (a skyline of facades with window rows, up
to ~35 degrees: a puddle 20 m ahead is seen at 3-4 degrees and mirrors what stands beyond it) and an open one (a low
band of trees and hills). Six faces each in the D3D cube layout the engine samples with world vectors (z up: face 4 the
zenith, 5 the ground; checked against the stock BNG_Sky_01 faces). Written as the mod's lossless sources
(mod/texture_sources, WebP) with their manifest entries (install.py encodes them into BC7 sRGB).

Deterministic: integer hashes for the skyline, no randomness.

  python3 tools/make_wet_cubemaps.py [--size 128] [--preview OUT.png]
"""
import argparse
import io
import os
import sys

import numpy as np
from PIL import Image

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
import modtextures  # noqa: E402

REL = "levels/beamng_earth/art/shapes/beamngEarth/wetsky"
SKIES = ("overcast", "clear")
HORIZONS = ("city", "open")


def names():
    """The cubemaps' names (the CubemapData objects): bne_wetsky_<sky>_<horizon>."""
    return [f"bne_wetsky_{s}_{h}" for s in SKIES for h in HORIZONS]


def face_dirs(face, n):
    """World directions (x, y, z), (n, n, 3), of face's pixels (row 0 at the top) in the D3D cube layout."""
    c = (np.arange(n) + 0.5) / n * 2 - 1
    sc, tc = np.meshgrid(c, c)              # sc across, tc down
    one = np.ones_like(sc)
    d = {0: (one, -tc, -sc), 1: (-one, -tc, sc), 2: (sc, one, tc), 3: (sc, -one, -tc),
         4: (sc, -tc, one), 5: (-sc, -tc, -one)}[face]
    v = np.stack(d, axis=-1)
    return v / np.linalg.norm(v, axis=-1, keepdims=True)


def _hash(i, salt):
    """A deterministic integer hash of the integer array i -> 0..1."""
    h = (i.astype(np.uint64) * np.uint64(2654435761) + np.uint64(salt) * np.uint64(40503)) & np.uint64(0xFFFFFFFF)
    h ^= h >> np.uint64(15)
    h = (h * np.uint64(2246822519)) & np.uint64(0xFFFFFFFF)
    h ^= h >> np.uint64(13)
    return (h % np.uint64(10007)).astype(np.float64) / 10006.0


def colour(v, sky, horizon):
    """sRGB 0..1 (..., 3) of world directions v."""
    z = np.clip(v[..., 2], -1, 1)
    elev = np.degrees(np.arcsin(z))
    az = np.degrees(np.arctan2(v[..., 1], v[..., 0])) % 360
    s = np.clip(z, 0, 1)[..., None]
    if sky == "overcast":           # CIE overcast: brightest at the zenith, a third of it at the horizon (softened)
        zen, hor = np.array([0.88, 0.89, 0.91]), np.array([0.74, 0.76, 0.79])
        out = hor + (zen - hor) * np.sqrt(s)
    else:                           # clearing: blue overhead, white haze at the horizon
        zen, hor = np.array([0.30, 0.48, 0.80]), np.array([0.80, 0.85, 0.90])
        out = hor + (zen - hor) * (s ** 0.6)
    ground = np.array([0.10, 0.10, 0.105])
    out = np.where((z < 0)[..., None], ground, out)
    if horizon == "city":           # facades: 2.5-6 degree wide blocks, tops 8-35 degrees, window rows
        width = 4.0
        b = np.floor(az / width).astype(np.int64)
        top = 8 + 27 * _hash(b, 1) ** 1.4
        tone = 0.22 + 0.3 * _hash(b, 2)
        warm = 0.03 * _hash(b, 3)
        fac = np.stack([tone + warm, tone + warm * 0.5, tone], axis=-1)
        rows = (np.floor(elev / 1.6).astype(np.int64) % 2 == 0)
        cols = (np.floor((az % width) / 0.7).astype(np.int64) % 3 != 0)
        win = (rows & cols & (elev > 1.0))[..., None]
        fac = np.where(win, fac * 0.55 + 0.04, fac)           # windows: darker (they reflect the grey sky dimly)
        inside = ((elev >= 0) & (elev < top))[..., None]
        out = np.where(inside, fac, out)
    else:                           # trees and low hills: a dark band to 1.5-4 degrees
        b = np.floor(az / 2.0).astype(np.int64)
        top = 1.5 + 2.5 * _hash(b, 4)
        band = np.stack([0.17 + 0.0 * top, 0.20 + 0.0 * top, 0.17 + 0.0 * top], axis=-1)
        inside = ((elev >= 0) & (elev < top))[..., None]
        out = np.where(inside, band, out)
    return np.clip(out, 0, 1)


def faces(sky, horizon, n):
    return [Image.fromarray((colour(face_dirs(f, n), sky, horizon) * 255 + 0.5).astype(np.uint8), "RGB")
            for f in range(6)]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--size", type=int, default=128)
    ap.add_argument("--preview", help="a PNG of every face (a row a cubemap)")
    args = ap.parse_args()
    entries = modtextures.manifest()
    sheet = Image.new("RGB", (args.size * 6, args.size * 4)) if args.preview else None
    for row, (sky, hz) in enumerate((s, h) for s in SKIES for h in HORIZONS):
        for f, im in enumerate(faces(sky, hz, args.size)):
            rel = f"{REL}/bne_wetsky_{sky}_{hz}_{f}"
            src = os.path.join(modtextures.SOURCES, *rel.split("/")) + ".webp"
            os.makedirs(os.path.dirname(src), exist_ok=True)
            b = io.BytesIO()
            im.save(b, "WEBP", lossless=True, quality=100, method=6, exact=True)
            with open(src, "wb") as fh:
                fh.write(b.getvalue())
            entries[rel + ".dds"] = {"source": rel + ".webp", "format": "BC7_SRGB", "role": "color"}
            if sheet:
                sheet.paste(im, (f * args.size, row * args.size))
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import mod_textures_dds
    mod_textures_dds.write_manifest(entries)
    if sheet:
        sheet.save(args.preview)
    print("wrote", len(names()) * 6, "faces under", REL)


if __name__ == "__main__":
    main()
