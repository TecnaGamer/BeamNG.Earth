#!/usr/bin/env python3
"""Check the road tiles around a point as the world server builds them (--nl-bgt on): per 1 m cell, are there gaps
in the road, surfaces through each other, roads floating over or sunk under the terrain, terrain far from the lidar?
Numbers per check and a map (red: problems) to look at, without the game.

    python3 tools/road_check.py 51.9083 4.4656 [--tiles 1] [--out check.png] [--anchor 51.90891,4.46804]

Checks (each cell 1 m, the tiles' meshes rasterised: road, deck, tunnel):
  gap       a BGT road surface with no mesh over it
  stacked   two surfaces over each other 5 cm .. 2 m apart (through each other, z-fighting), not over a passage
  floating  the top road surface more than FLOAT_M over the terrain, not on a deck or bridge
  sunk      the terrain more than SUNK_M over the top road surface (it shows through the road)
  terrain   the terrain more than TERRAIN_M off the lidar, not in a passage (a pit or a mound the grading made)
"""
import argparse
import math
import os
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))

STACK_MIN_M, STACK_MAX_M = 0.05, 2.0
FLOAT_M = 0.3
SUNK_M = 0.05
TERRAIN_M = 1.0
TRUTH_M = 0.5               # m a truth point's road surface may be off
CELL = 1.0


def road_only(mesh):
    """The mesh's triangles of road surface materials (the BGT's and the road mesh's), not walls, tubes or soil."""
    if mesh is None or len(mesh) < 5:
        return mesh
    import bgt
    V, T, UV, mats, idx = mesh
    keep_m = {m for m, _ in (bgt.ASPHALT, bgt.STONE, bgt.TILES, bgt.RED, bgt.GRAVEL)} | {bgt.KERB}
    ok = np.array([m in keep_m for m in mats], bool)[np.asarray(idx)]
    return (V, np.asarray(T)[ok], UV, mats, np.asarray(idx)[ok]) if ok.any() else None


def raster(mesh, origin, box, cell=CELL):
    """(top, bottom) heights per cell of a mesh's triangles in box (x0, y0, x1, y1) (NaN: none), 1 m cells."""
    x0, y0, x1, y1 = box
    nx, ny = int(round((x1 - x0) / cell)), int(round((y1 - y0) / cell))
    top = np.full(nx * ny, -np.inf)
    bot = np.full(nx * ny, np.inf)
    if mesh is None:
        return top.reshape(ny, nx), bot.reshape(ny, nx)
    V = np.asarray(mesh[0], np.float64) + np.asarray(origin, np.float64)
    T = np.asarray(mesh[1], np.int64)
    A, B, C = V[T[:, 0]], V[T[:, 1]], V[T[:, 2]]
    nrm = np.cross(B - A, C - A)
    lying = np.abs(nrm[:, 2]) > 0.5 * np.maximum(np.linalg.norm(nrm, axis=1), 1e-12)    # (walls, kerb faces: out)
    A, B, C = A[lying], B[lying], C[lying]
    lo = np.floor((np.minimum(np.minimum(A, B), C)[:, :2] - [x0, y0]) / cell).astype(np.int64)
    hi = np.floor((np.maximum(np.maximum(A, B), C)[:, :2] - [x0, y0]) / cell).astype(np.int64)
    size = np.maximum(hi - lo + 1, 1)
    K = 8
    small = (size[:, 0] <= K) & (size[:, 1] <= K)
    for idx, k in ((np.flatnonzero(small), K), (np.flatnonzero(~small), None)):
        if not len(idx):
            continue
        if k is None:           # big triangles: one at a time
            batches = [(np.array([i]), int(size[i].max())) for i in idx]
        else:
            batches = [(idx[a:a + 20000], k) for a in range(0, len(idx), 20000)]
        for ids, kk in batches:
            oi, oj = np.meshgrid(np.arange(kk), np.arange(kk))
            ci = lo[ids, 0][:, None] + oi.ravel()[None, :]
            cj = lo[ids, 1][:, None] + oj.ravel()[None, :]
            px = x0 + (ci + 0.5) * cell
            py = y0 + (cj + 0.5) * cell
            a, b, c = A[ids], B[ids], C[ids]
            d = (b[:, 1] - c[:, 1]) * (a[:, 0] - c[:, 0]) + (c[:, 0] - b[:, 0]) * (a[:, 1] - c[:, 1])
            d = np.where(np.abs(d) < 1e-12, np.nan, d)
            w1 = ((b[:, 1] - c[:, 1])[:, None] * (px - c[:, 0][:, None])
                  + (c[:, 0] - b[:, 0])[:, None] * (py - c[:, 1][:, None])) / d[:, None]
            w2 = ((c[:, 1] - a[:, 1])[:, None] * (px - c[:, 0][:, None])
                  + (a[:, 0] - c[:, 0])[:, None] * (py - c[:, 1][:, None])) / d[:, None]
            w3 = 1 - w1 - w2
            ins = (w1 >= -1e-9) & (w2 >= -1e-9) & (w3 >= -1e-9) & (ci >= 0) & (ci < nx) & (cj >= 0) & (cj < ny)
            ins &= (ci <= hi[ids, 0][:, None]) & (cj <= hi[ids, 1][:, None])
            if not ins.any():
                continue
            z = w1 * a[:, 2][:, None] + w2 * b[:, 2][:, None] + w3 * c[:, 2][:, None]
            flat = (cj * nx + ci)[ins]
            np.maximum.at(top, flat, z[ins])
            np.minimum.at(bot, flat, z[ins])
    return top.reshape(ny, nx), bot.reshape(ny, nx)


def poly_mask(rings_xy, box, cell=CELL):
    """Which cells' centres lie in any of the polygons (each: [outer ring, holes] in the frame)."""
    import bgt
    x0, y0, x1, y1 = box
    nx, ny = int(round((x1 - x0) / cell)), int(round((y1 - y0) / cell))
    gx, gy = np.meshgrid(x0 + (np.arange(nx) + 0.5) * cell, y0 + (np.arange(ny) + 0.5) * cell)
    pts = np.c_[gx.ravel(), gy.ravel()]
    m = np.zeros(len(pts), bool)
    for rings in rings_xy:
        lo, hi = rings[0].min(0), rings[0].max(0)
        c = ~m & (pts[:, 0] >= lo[0]) & (pts[:, 0] <= hi[0]) & (pts[:, 1] >= lo[1]) & (pts[:, 1] <= hi[1])
        if c.any():
            k = np.flatnonzero(c)
            m[k[bgt._inside_poly(rings, pts[k])]] = True
    return m.reshape(ny, nx), pts


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("lat", type=float)
    ap.add_argument("lon", type=float)
    ap.add_argument("--anchor", default=None, help="lat,lon of the frame (default: the point)")
    ap.add_argument("--tiles", type=int, default=0, help="road tiles on each side of the point's (0: its own)")
    ap.add_argument("--zoom", type=int, default=15)
    ap.add_argument("--out", default="road_check.png")
    ap.add_argument("--truth", default=os.path.join(HERE, "road_check_points.json"),
                    help="points with the road surface's true height (json: points [{name, lat, lon, top}])")
    ap.add_argument("--dump", choices=("gap", "stacked", "floating", "sunk", "terrain"), help="print sample cells")
    ap.add_argument("--cache-dir", default=os.path.expanduser("~/.cache/beamng-earth"))
    a = ap.parse_args(argv)
    os.environ["BNGE_NL_BGT"] = "1"
    import blockworker
    import bgt
    import geodesy
    import roads as roadsmod
    alat, alon = (float(v) for v in a.anchor.split(",")) if a.anchor else (a.lat, a.lon)
    frame = geodesy.EnuFrame(alat, alon)
    px, py, _ = frame.geodetic_to_enu(a.lat, a.lon, 0.0)
    blockworker._s = blockworker._State(a.cache_dir, False, True, True, True, False)
    got = {}
    real_write = roadsmod.write_tile

    def capture(od, vd, aid, tx, ty, chunks, wk, mesh, deck=None, *rest, **kw):
        got[(tx, ty)] = {"road": mesh, "deck": deck, "tunnel": kw.get("tunnel_mesh"),
                         "ground": getattr(blockworker._s, "last_ground", None)}
        return None, []
    roadsmod.write_tile = capture
    t0x, t0y = None, None
    for tx in range(-64, 64):           # the road tile under the point
        x0, y0, x1, y1 = roadsmod.tile_bbox(tx, 0)
        if x0 <= px < x1:
            t0x = tx
            break
    for ty in range(-64, 64):
        x0, y0, x1, y1 = roadsmod.tile_bbox(0, ty)
        if y0 <= py < y1:
            t0y = ty
            break
    out = tempfile.mkdtemp(prefix="road_check_")
    tiles = [(t0x + i, t0y + j) for i in range(-a.tiles, a.tiles + 1) for j in range(-a.tiles, a.tiles + 1)]
    for tx, ty in tiles:
        blockworker._s.last_ground = None
        blockworker.roads_tile(99, alat, alon, a.zoom, tx, ty, out, "/x", True, 0.0)
    roadsmod.write_tile = real_write

    total = {"cells": 0, "road": 0, "gap": 0, "stacked": 0, "floating": 0, "sunk": 0, "terrain": 0}
    from PIL import Image
    boxes = {t: roadsmod.tile_bbox(*t) for t in tiles}
    X0 = min(b[0] for b in boxes.values())
    Y0 = min(b[1] for b in boxes.values())
    X1 = max(b[2] for b in boxes.values())
    Y1 = max(b[3] for b in boxes.values())
    W, H = int(X1 - X0), int(Y1 - Y0)
    img = np.zeros((H, W, 3), np.uint8) + 40
    spots = []
    for t in tiles:
        g = got.get(t)
        if g is None:
            continue
        box = boxes[t]
        x0, y0, x1, y1 = box
        origin = (x0, y0, 0.0)
        tops, bots = [], []
        for kind in ("road", "deck", "tunnel"):     # (their upright faces left out: raster)
            tp, bt = raster(road_only(g[kind]) if kind == "tunnel" else g[kind], origin, box)
            tops.append(tp)
            bots.append(bt)
        top = np.max(tops, 0)
        bot = np.min(bots, 0)
        has = np.isfinite(top)
        deck = np.isfinite(tops[1])
        nx, ny = top.shape[1], top.shape[0]
        gx, gy = np.meshgrid(x0 + np.arange(nx) + 0.5, y0 + np.arange(ny) + 0.5)
        la, lo, _ = frame.enu_to_geodetic(gx.ravel(), gy.ravel(), np.zeros(gx.size))
        ground = g["ground"]
        if ground is None or ground[0] != t[0] or ground[1] != t[1]:
            print("tile", t, ": no BGT here (or none built)")
            continue
        _, _, dug, raw, passes = ground
        import kunstwerken as kwmod     # (a kunstwerk's decks are decks too)
        deck = deck | np.isfinite(kwmod.deck_top(la, lo)).reshape(ny, nx)
        ter = np.asarray(frame.geodetic_to_enu(la, lo, np.asarray(dug(la, lo), np.float64))[2]).reshape(ny, nx)
        lid = np.asarray(frame.geodetic_to_enu(la, lo, np.asarray(raw(la, lo), np.float64))[2]).reshape(ny, nx)
        # the BGT road surfaces here
        polys = blockworker._bgt_polygons(frame, t[0], t[1], [])
        rings_xy = []
        for p in polys:
            if bgt.look(p.functie, p.voorkomen) is None:
                continue
            rr = []
            for ring in p.rings:
                e, n, _ = frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
                rr.append(np.c_[np.asarray(e), np.asarray(n)])
            rings_xy.append(rr)
        road_m, pts = poly_mask(rings_xy, box)
        in_pass = (bgt._in_passages(passes, pts, ends=True) >= 0).reshape(ny, nx) if passes else np.zeros_like(has)
        gap = road_m & ~has
        spread = np.where(has, top - bot, 0.0)
        stacked = has & (spread > STACK_MIN_M) & (spread < STACK_MAX_M) & ~in_pass & ~deck
        floating = has & ~deck & ~in_pass & (top - ter > FLOAT_M)
        sunk = has & (ter - top > SUNK_M)
        terrain = ~in_pass & (np.abs(ter - lid) > TERRAIN_M)
        if a.dump:
            m = {"gap": gap, "stacked": stacked, "floating": floating, "sunk": sunk, "terrain": terrain}[a.dump]
            js, is_ = np.nonzero(m)
            which = np.argmax(np.stack(tops), 0)
            names = ("road", "deck", "tunnel")
            for j, i in list(zip(js, is_))[::max(1, len(js) // 25)]:
                cla, clo, _ = frame.enu_to_geodetic(x0 + i + 0.5, y0 + j + 0.5, 0.0)
                print(f"  {a.dump} ({x0 + i:.0f},{y0 + j:.0f}) {float(cla):.6f},{float(clo):.6f} top {top[j, i]:.2f} "
                      f"({names[which[j, i]]}) bot {bot[j, i]:.2f} terrain {ter[j, i]:.2f} lidar {lid[j, i]:.2f} "
                      f"passage {bool(in_pass[j, i])}")
        # the truth points in this tile: the top road surface there (or the terrain) against their height
        truth = []
        try:
            import json
            truth = json.load(open(a.truth, encoding="utf-8"))["points"]
        except (OSError, ValueError, KeyError):
            pass
        for tp in truth:
            ex, ey, _ = frame.geodetic_to_enu(tp["lat"], tp["lon"], 0.0)
            i, j = int(float(ex) - x0), int(float(ey) - y0)
            if not (0 <= i < nx and 0 <= j < ny):
                continue
            want = float(frame.geodetic_to_enu(tp["lat"], tp["lon"], tp["top"])[2])
            got_z = float(top[j, i]) if has[j, i] else float(ter[j, i])
            ok = abs(got_z - want) <= TRUTH_M
            total["truth"] = total.get("truth", 0) + 1
            total["truth_ok"] = total.get("truth_ok", 0) + int(ok)
            spots.append(("truth " + ("OK  " if ok else "FOUT"), round(got_z - want, 2), tp["lat"], tp["lon"]))
            print(f"  {'OK  ' if ok else 'FOUT'} {tp['name']}: game {got_z:.2f}, echt {want:.2f} ({got_z - want:+.2f} m)")
        total["cells"] += has.size
        total["road"] += int(road_m.sum())
        for k, m in (("gap", gap), ("stacked", stacked), ("floating", floating), ("sunk", sunk), ("terrain", terrain)):
            total[k] += int(m.sum())
            if m.any():
                j, i = np.argwhere(m)[len(np.argwhere(m)) // 2]
                cla, clo, _ = frame.enu_to_geodetic(x0 + i + 0.5, y0 + j + 0.5, 0.0)
                spots.append((k, int(m.sum()), round(float(cla), 6), round(float(clo), 6)))
        # the map: grey road, blue deck, red problems (orange: terrain), y up
        tile = np.zeros((ny, nx, 3), np.uint8) + 40
        tile[road_m] = (90, 90, 90)
        tile[has] = (140, 140, 140)
        tile[deck] = (80, 120, 200)
        tile[terrain] = (230, 140, 0)
        tile[floating | sunk] = (255, 0, 255)
        tile[stacked] = (255, 255, 0)
        tile[gap] = (255, 0, 0)
        oy, ox = int(Y1 - y1), int(x0 - X0)
        img[oy:oy + ny, ox:ox + nx] = tile[::-1]
    Image.fromarray(img).resize((W * 3, H * 3), Image.NEAREST).save(a.out)
    road = max(total["road"], 1)
    print(f"tiles {len(tiles)}; BGT road {total['road']} m2")
    print(f"  gap       {total['gap']:6d} m2  ({100 * total['gap'] / road:.1f} % of the road)   red")
    print(f"  stacked   {total['stacked']:6d} m2  (surfaces 5 cm - 2 m through each other)  yellow")
    print(f"  floating  {total['floating']:6d} m2  (> {FLOAT_M} m over the terrain)   magenta")
    print(f"  sunk      {total['sunk']:6d} m2  (terrain through the road)   magenta")
    print(f"  terrain   {total['terrain']:6d} m2  (> {TERRAIN_M} m off the lidar)   orange")
    if total.get("truth"):
        print(f"  truth     {total['truth_ok']}/{total['truth']} points within {TRUTH_M} m of the real height")
    for s in sorted((s for s in spots if not s[0].startswith("truth")), key=lambda s: -s[1])[:12]:
        print("   ", s[0], s[1], "m2 around", f"{s[2]},{s[3]}")
    print("map:", os.path.abspath(a.out))


if __name__ == "__main__":
    main()
