#!/usr/bin/env python3
"""The reference for modelling a junction by hand in Blender: around a point, in local RD metres (x east, y north, z NAP;
origin = the box's centre, rounded to whole metres), written to --out:

    ref.json        origin (RD x, y; lat, lon), box, and the BGT: road surfaces (functie, voorkomen, level, rings),
                    walls (scheiding), passages (tunneldeel) and decks (overbruggingsdeel)
    ahn.npz         the AHN at 0.5 m over the box: dtm (holes filled, ahn.ground), kind (0 ground, 1 hole, 2 deck), dsm
    photo.jpg       PDOK's aerial photo of the box (Actueel_orthoHR), for the reference plane's texture

    python3 tools/blender_ref.py 51.9080 4.4658 --size 320 --out ../design/kunstwerken/droogleever/ref

The photo and the AHN are reference: what goes into the mod is the model built on them (design/kunstwerken).
"""
import argparse
import io
import json
import math
import os
import sys
import urllib.request

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))

PHOTO = ("https://service.pdok.nl/hwh/luchtfotorgb/wms/v1_0?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap"
         "&LAYERS=Actueel_orthoHR&STYLES=&CRS=EPSG:28992&BBOX={x0},{y0},{x1},{y1}&WIDTH={w}&HEIGHT={h}&FORMAT=image/jpeg")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("lat", type=float)
    ap.add_argument("lon", type=float)
    ap.add_argument("--size", type=float, default=320.0, help="m: the box's side")
    ap.add_argument("--photo-px", type=int, default=2560)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cache-dir", default=os.path.expanduser("~/.cache/beamng-earth"))
    a = ap.parse_args(argv)
    import ahn as ahnmod
    import bgt as bgtmod
    import geodesy
    import netfetch
    os.makedirs(a.out, exist_ok=True)
    cx, cy = (round(float(v)) for v in ahnmod.wgs_to_rd(a.lat, a.lon))
    h = a.size / 2
    x0, y0, x1, y1 = cx - h, cy - h, cx + h, cy + h
    net = netfetch.Fetcher()

    # the AHN
    A = ahnmod.Ahn(a.cache_dir, net)
    n = int(a.size / ahnmod.RES)
    xs = x0 + (np.arange(n) + 0.5) * ahnmod.RES
    ys = y1 - (np.arange(n) + 0.5) * ahnmod.RES          # rows north to south
    X, Y = np.meshgrid(xs, ys)
    # RD -> WGS84 by inverting wgs_to_rd numerically (Newton on its local derivative: two steps reach mm)
    lat = np.full(X.shape, a.lat)
    lon = np.full(X.shape, a.lon)
    for _ in range(4):
        rx, ry = ahnmod.wgs_to_rd(lat, lon)
        d = 1e-5
        rx1, ry1 = ahnmod.wgs_to_rd(lat + d, lon)
        rx2, ry2 = ahnmod.wgs_to_rd(lat, lon + d)
        j11, j12, j21, j22 = (rx1 - rx) / d, (rx2 - rx) / d, (ry1 - ry) / d, (ry2 - ry) / d
        det = j11 * j22 - j12 * j21
        ex, ey = X - rx, Y - ry
        lat = lat + (j22 * ex - j12 * ey) / det
        lon = lon + (-j21 * ex + j11 * ey) / det
    g, k, s = A.sample(lat, lon, kinds=True)
    np.savez_compressed(os.path.join(a.out, "ahn.npz"), dtm=g.astype(np.float32), kind=k.astype(np.uint8),
                        dsm=s.astype(np.float32), res=ahnmod.RES, x0=x0 - cx, y1=y1 - cy)

    # the BGT, its features over the box
    clat, clon = float(lat[n // 2, n // 2]), float(lon[n // 2, n // 2])
    frame = geodesy.EnuFrame(clat, clon)
    B = bgtmod.Bgt(a.cache_dir, net)
    layers = bgtmod.SURFACES + (bgtmod.WALLS, bgtmod.PASSAGE, "overbruggingsdeel", "kunstwerkdeel", "spoor", "paal")
    feats, seen = [], set()
    for tx, ty in B.tiles_box(frame, (-h - 10, -h - 10, h + 10, h + 10)):
        data = B.tile(tx, ty)
        if not data:
            continue
        for p in bgtmod.tile_polys(data, bgtmod.TILE_ZOOM, tx, ty, layers):
            key = (p.id.split("@")[0], p.layer)
            if key in seen:
                continue
            seen.add(key)
            rings = []
            for ring in p.rings:
                ring = np.asarray(ring, np.float64).reshape(-1, 2)
                rx, ry = ahnmod.wgs_to_rd(ring[:, 1], ring[:, 0])
                rings.append(np.round(np.c_[rx - cx, ry - cy], 3).tolist())
            if not rings or not any(-h - 20 <= x <= h + 20 and -h - 20 <= y <= h + 20 for x, y in rings[0]):
                continue
            feats.append({"id": key[0], "layer": p.layer, "functie": p.functie, "voorkomen": p.voorkomen,
                          "level": p.level, "rings": rings})
    ref = {"origin_rd": [cx, cy], "origin_wgs": [clat, clon], "size": a.size, "box": [-h, -h, h, h],
           "features": feats}
    json.dump(ref, open(os.path.join(a.out, "ref.json"), "w", encoding="utf-8"))

    # the aerial photo
    try:                        # (in 4 x 4 pieces: the WMS refuses big images)
        from PIL import Image
        k4, w = 4, a.photo_px // 4
        full = Image.new("RGB", (w * k4, w * k4))
        step = a.size / k4
        for i in range(k4):
            for j in range(k4):
                url = PHOTO.format(x0=x0 + i * step, y0=y1 - (j + 1) * step, x1=x0 + (i + 1) * step, y1=y1 - j * step,
                                   w=w, h=w)
                full.paste(Image.open(io.BytesIO(urllib.request.urlopen(url, timeout=120).read())).convert("RGB"),
                           (i * w, j * w))
        full.save(os.path.join(a.out, "photo.jpg"), quality=92)
    except Exception as e:
        print("photo:", e)
    by = {}
    for f in feats:
        by[f["layer"]] = by.get(f["layer"], 0) + 1
    print(f"origin RD {cx},{cy} ({clat:.6f},{clon:.6f}); {a.size:.0f} m; features {by}; AHN {n}x{n}; dsm finite "
          f"{np.isfinite(s).mean():.2f}; decks {(k == 2).mean():.3f}")


if __name__ == "__main__":
    main()
