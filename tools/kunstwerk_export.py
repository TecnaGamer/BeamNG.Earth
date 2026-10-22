#!/usr/bin/env python3
"""A kunstwerk's model (parts.json: tools/kunstwerk_design.py, touched up in Blender) to the server's data
(server/data/kunstwerken/<name>.kunstwerk.json, server/kunstwerken.py): its triangles with the game's materials and its
UVs (texture metres), its decks' planes, and a 1 m raster of its floor (the lowest road surface it draws per cell, NaN
where it draws none): where the model draws its streets the generator's are left out and the terrain is kept under them.

    python3 tools/kunstwerk_export.py ../design/kunstwerken/droogleever --name droogleever
"""
import argparse
import json
import os

import numpy as np

FLOOR_PARTS = ("road", "street_", "decks")      # parts whose top is a floor a car or a cyclist is on
UV_M = {"road_asphalt_2lane": 8.0, "bne_sidewalk_brick": 1.2, "sidewalk_concrete": 1.5, "italy_cobblestone": 1.0,
        "bne_tunnel_soil": 2.0, "bne_grass": 2.0, "curbs": 2.0, "bne_bridge_concrete": 1.0, "bne_truss_steel": 1.0,
        "bne_concrete_dark": 4.0, "bne_cycle_red": 8.0, "bne_paint_white": 8.0}


def raster_floor(P, T, x0, y0, nx, ny):
    """The lowest top per 1 m cell of upward triangles (NaN: none). Pure."""
    fl = np.full(nx * ny, np.inf)
    A, B, C = P[T[:, 0]], P[T[:, 1]], P[T[:, 2]]
    up = np.cross(B - A, C - A)[:, 2] > 1e-9
    for a, b, c in zip(A[up], B[up], C[up]):
        lo = np.floor(np.minimum(np.minimum(a, b), c)[:2] - [x0, y0]).astype(int)
        hi = np.floor(np.maximum(np.maximum(a, b), c)[:2] - [x0, y0]).astype(int)
        lo, hi = np.maximum(lo, 0), np.minimum(hi, [nx - 1, ny - 1])
        if (hi < lo).any():
            continue
        ii, jj = np.meshgrid(np.arange(lo[0], hi[0] + 1), np.arange(lo[1], hi[1] + 1))
        px, py = x0 + ii.ravel() + 0.5, y0 + jj.ravel() + 0.5
        d = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
        if abs(d) < 1e-12:
            continue
        w1 = ((b[1] - c[1]) * (px - c[0]) + (c[0] - b[0]) * (py - c[1])) / d
        w2 = ((c[1] - a[1]) * (px - c[0]) + (a[0] - c[0]) * (py - c[1])) / d
        w3 = 1 - w1 - w2
        k = (w1 >= -1e-6) & (w2 >= -1e-6) & (w3 >= -1e-6)
        if not k.any():
            continue
        z = w1 * a[2] + w2 * b[2] + w3 * c[2]
        idx = jj.ravel()[k] * nx + ii.ravel()[k]
        np.minimum.at(fl, idx, z[k])
    fl[~np.isfinite(fl)] = np.nan
    return fl.reshape(ny, nx)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("kunstwerk")
    ap.add_argument("--name", required=True)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server", "data",
                                                  "kunstwerken"))
    a = ap.parse_args(argv)
    data = json.load(open(os.path.join(a.kunstwerk, "parts.json")))
    mats, P, T, UV, TM = [], [], [], [], []
    fP, fT, nf = [], [], 0
    n = 0
    for p in data["parts"]:
        pos = np.asarray(p["positions"], np.float64)
        tri = np.asarray(p["tris"], np.int64)
        uv = np.asarray(p["uvs"], np.float64) / UV_M.get(p["material"], 1.0)
        if p["material"] not in mats:
            mats.append(p["material"])
        P.append(pos)
        T.append(tri + n)
        UV.append(uv)
        TM.append(np.full(len(tri), mats.index(p["material"]), np.int64))
        n += len(pos)
        if p["name"].startswith(FLOOR_PARTS):
            fP.append(pos)
            fT.append(tri + nf)
            nf += len(pos)
    P, T, UV, TM = np.concatenate(P), np.concatenate(T), np.concatenate(UV), np.concatenate(TM)
    FP, FT = np.concatenate(fP), np.concatenate(fT)
    x0, y0 = np.floor(FP[:, :2].min(0)) - 1
    x1, y1 = np.ceil(FP[:, :2].max(0)) + 1
    nx, ny = int(x1 - x0), int(y1 - y0)
    floor = raster_floor(FP, FT, x0, y0, nx, ny)
    decks = json.load(open(os.path.join(a.kunstwerk, "decks.json")))["decks"] \
        if os.path.exists(os.path.join(a.kunstwerk, "decks.json")) else []
    out = {"name": a.name, "origin_rd": data["origin_rd"], "origin_wgs": data["origin_wgs"],
           "positions": P.round(3).tolist(), "tris": T.tolist(), "uvs": UV.round(4).tolist(),
           "materials": mats, "tri_material": TM.tolist(),
           "decks": [{"ring": d["ring"], "plane": d["plane"]} for d in decks if d.get("layer") == "tunneldeel"],
           "floor": {"x0": float(x0), "y0": float(y0), "nx": nx, "ny": ny,
                     "z": np.where(np.isfinite(floor), np.round(floor, 2), -999).astype(float).ravel().tolist()}}
    os.makedirs(a.out, exist_ok=True)
    dst = os.path.join(a.out, f"{a.name}.kunstwerk.json")
    json.dump(out, open(dst, "w"))
    print(f"{dst}: {len(P)} vertices, {len(T)} triangles, materials {mats}; floor {nx}x{ny} "
          f"({np.isfinite(floor).sum()} m2)")


if __name__ == "__main__":
    main()
