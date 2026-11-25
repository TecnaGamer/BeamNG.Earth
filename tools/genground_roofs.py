#!/usr/bin/env python3
"""Roof colours with and without the generated ground (BNE_GEN_GROUND), top down, offline: each building's roof
material and tint as buildings.plan picks them (the texture's mean colour times the tint), drawn into its footprint,
(a) with the roof photo's colours (the regional 1 m photo, today's default) and (b) without (the switch on: mapped
roof colour / material, else the style's hashed pick), beside the orthophoto. An approximation of what the road tiles
draw: plan() with the footprint's own Overture attributes, its shared walls and the box's coverage (town / dense)
as the road tile computes them (no OSM colour fill).

    python3 tools/genground_roofs.py --lat 39.2826 --lon -76.5927 --name fells --half 128

Reads the scratch cache genground_compare.py filled (map data, Maryland's tiles on disk); writes
<out>/<name>_roofs.png (truth | photo roofs | generated-ground roofs).
"""
import argparse
import os
import sys
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

import buildings  # noqa: E402
import genground  # noqa: E402
import geodesy  # noqa: E402
import mapcells  # noqa: E402
import mapdata as mapdatamod  # noqa: E402
import netfetch  # noqa: E402


def look_rgb(key):
    """sRGB 0..255 of a roof look key (material mean x tint)."""
    mat = key.split("|")[0]
    mean = np.array(buildings.ROOF_TEX_MEAN.get(mat, (0.5, 0.5, 0.5)))
    if key in buildings.LOOKS:
        tint = np.array(buildings.LOOKS[key][1], np.float64)
    elif "|c_" in key:
        tint = np.array([float(v) for v in key.split("|c_")[1].split("_")])
    elif "|" in key:
        tail = key.split("|")[1]
        try:
            tint = np.array(buildings.ROOF_TINTS[mat][int(tail)])
        except (KeyError, ValueError, IndexError):
            tint = np.ones(3)
    else:
        tint = np.ones(3)
    return tuple(int(v) for v in np.clip(mean * tint * 255.0, 0, 255))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--half", type=float, default=128.0)
    ap.add_argument("--out", default=os.path.expanduser("~/bnge-tests/genground/compare"))
    ap.add_argument("--cache", default=os.path.expanduser("~/bnge-tests/genground/cache"))
    a = ap.parse_args()
    md = mapdatamod.MapData(a.cache, netfetch.Fetcher(offline=True))
    frame = geodesy.EnuFrame(a.lat, a.lon)
    h = a.half
    px = int(2 * h * 2)                 # 0.5 m a pixel
    la, lo, _ = frame.enu_to_geodetic(np.array([-h, h]), np.array([-h, h]), 0.0)
    keys = mapcells.cells_for_bbox(float(la.min()), float(lo.min()), float(la.max()), float(lo.max()))
    photo, final, _ = buildings.roof_photo_final(frame, -h - 20, -h - 20, h + 20, h + 20, a.cache, online=False)
    print("roof photo:", photo is not None, final)
    # the buildings as the road tile gives them to plan: their shared walls (row houses) and the box's coverage (dense)
    blds = []
    seen = set()
    for k in keys:
        f = md.cell(k, layers=("buildings",), kind="demand").layers.get("buildings")
        if f is None:
            continue
        xy = f.xy_in(frame)
        names = [c for c in f.attrs if not c.startswith("_")]
        for i in range(len(f)):
            fid = str(f.ids[i])
            if fid in seen or f.kind[i] != 3:
                continue
            seen.add(fid)
            ring = np.asarray(f.parts(i, xy)[0][0], np.float64)
            if ring[:, 0].max() < -h or ring[:, 0].min() > h or ring[:, 1].max() < -h or ring[:, 1].min() > h:
                continue
            at = {c: f.attrs[c][i] for c in names}
            at = {c: (None if isinstance(v, float) and not np.isfinite(v) else v) for c, v in at.items()}
            at = {c: (v if not isinstance(v, str) or v not in ("", "None", "nan") else None) for c, v in at.items()}
            at["seed_id"] = fid
            blds.append((ring[:-1] if len(ring) > 3 and np.allclose(ring[0], ring[-1]) else ring, at))
    shared = buildings._shared_walls([r for r, _ in blds])
    cover = sum(abs(buildings._area2(r)) / 2 for r, _ in blds) / (2 * h) ** 2
    town, dense = cover < buildings.TOWN_COVERAGE, cover > buildings.BRICK_WALK_COVERAGE
    print(f"{len(blds)} buildings, coverage {cover:.2f} (dense {dense})")
    sheets = []
    shapes = {}
    for label, use_photo in (("photo roofs (switch off)", True), ("generated-ground roofs (switch on)", False)):
        img = Image.new("RGB", (px, px), (128, 128, 120))
        d = ImageDraw.Draw(img)
        n = 0
        with mock.patch.object(genground, "ON", not use_photo):
            for (ring, at), sh in zip(blds, shared):
                at = dict(at)
                if use_photo and photo is not None and not at.get("roof_color"):
                    rgb = photo.colours([ring])[0]
                    if np.isfinite(rgb).all():
                        at["_roof_photo"] = tuple(float(v) for v in rgb)
                loc = ring - ring.mean(0)
                if buildings._area2(loc) < 0:
                    loc = loc[::-1]
                try:
                    spec = buildings.plan(loc, at, int(sh), a.lat, a.lon, None, town, dense)
                except Exception as e:
                    print("plan failed", at["seed_id"], type(e).__name__, e)
                    continue
                shapes[spec.roof] = shapes.get(spec.roof, 0) + 1
                col = look_rgb(spec.roof_mat)
                pts = [((x + h) * 2, (h - y) * 2) for x, y in ring]
                d.polygon(pts, fill=col, outline=(60, 60, 60))
                n += 1
        print(label, n, "buildings, roof shapes", shapes)
        shapes = {}
        dd = ImageDraw.Draw(img)
        dd.rectangle((0, 0, 7 * len(label) + 8, 16), fill=(0, 0, 0))
        dd.text((4, 2), label, fill=(255, 255, 255))
        sheets.append(img)
    tp = os.path.join(a.out, f"{a.name}_truth.png")
    if os.path.exists(tp):
        t = Image.open(tp).convert("RGB")
        s = t.width / 1024.0                # (the truth covers the 1 km block)
        c = t.width / 2
        t = t.crop((int(c - h * s), int(c - h * s), int(c + h * s), int(c + h * s))).resize((px, px), Image.LANCZOS)
        sheets.insert(0, t)
    out = Image.new("RGB", (px * len(sheets) + 4 * (len(sheets) - 1), px), (255, 255, 255))
    for i, s_ in enumerate(sheets):
        out.paste(s_, (i * (px + 4), 0))
    p = os.path.join(a.out, f"{a.name}_roofs.png")
    out.save(p)
    print(p)


if __name__ == "__main__":
    main()
