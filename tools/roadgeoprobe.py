#!/usr/bin/env python3
"""roadgeoprobe.py LAT LON [RINGS] [--offline] [--anchor-km KM]: road geotiles (phase 1c) at a place, from the cache.

Builds every geotile within RINGS tiles of the point (content in its own frame, then the skin of an anchor at the
point, and of a second anchor KM east: a rebase's), reports failures and checks:
  - seams: every skin vertex inside its tile's quad in the anchor's world (no overlap), the quads' shared corners
    bit-equal (no gap);
  - skin == the anchor tiles' road mesh: at each skin triangle's centroid the anchor tile's mesh of the same
    material has the same height within 1 mm (the drawn ground: the block and the skin agree);
  - the second anchor: skins only (content from the first build), the exact variants it needs;
and prints the CPU of content, skins and the anchor tiles. Map data from ~/.cache/beamng-earth (downloads unless
--offline). Run from the repository root."""
import argparse
import glob
import json
import logging
import math
import os
import shutil
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "server"))

import numpy as np  # noqa: E402


def mesh_of(path, pos):
    """(P (n, 3) world, T (m, 3), material name per triangle) of a .cdae's first detail at pos."""
    import cdae
    d = cdae.read(path)
    m = next(m for m in d["meshes"] if m["type"] == 0)     # (a collision-only shape: a null mesh first)
    a = cdae.mesh_arrays(m)
    P = np.asarray(a["verts"], np.float64) + np.asarray(pos, np.float64)
    idx = np.asarray(a["indices"], np.int64)
    mats = [x["name"] for x in d["materials"]]
    T, M = [], []
    for start, count, mi in np.asarray(a["primitives"]).reshape(-1, 3):
        tri = idx[start:start + count].reshape(-1, 3)
        T.append(tri)
        M += [mats[mi & cdae.PRIM_MATERIAL_MASK]] * len(tri)
    return P, (np.concatenate(T) if T else np.zeros((0, 3), np.int64)), np.asarray(M)


def heights_at(P, T, M, pts, mat, near=None):
    """z at points pts (k, 2) on the triangles of material mat (nan off them): the one nearest near (k,) (a road over
    another: a tunnel's floor under the road over it, a hairpin's stretches), else the highest (a layer's)."""
    sel = T[M == mat]
    out = np.full(len(pts), np.nan)
    if not len(sel):
        return out
    A, B, C = P[sel[:, 0]], P[sel[:, 1]], P[sel[:, 2]]
    lo = np.minimum(np.minimum(A, B), C)[:, :2]
    hi = np.maximum(np.maximum(A, B), C)[:, :2]
    for k, p in enumerate(pts):
        c = np.flatnonzero((lo[:, 0] <= p[0]) & (hi[:, 0] >= p[0]) & (lo[:, 1] <= p[1]) & (hi[:, 1] >= p[1]))
        for i in c:
            v0, v1 = B[i, :2] - A[i, :2], C[i, :2] - A[i, :2]
            v2 = p - A[i, :2]
            den = v0[0] * v1[1] - v1[0] * v0[1]
            if abs(den) < 1e-12:
                continue
            b1 = (v2[0] * v1[1] - v1[0] * v2[1]) / den
            b2 = (v0[0] * v2[1] - v2[0] * v0[1]) / den
            if b1 >= -1e-9 and b2 >= -1e-9 and b1 + b2 <= 1 + 1e-9:
                z = A[i, 2] + b1 * (B[i, 2] - A[i, 2]) + b2 * (C[i, 2] - A[i, 2])
                if np.isnan(out[k]):
                    out[k] = z
                elif near is not None:
                    out[k] = z if abs(z - near[k]) < abs(out[k] - near[k]) else out[k]
                else:
                    out[k] = max(out[k], z)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("lat", type=float)
    ap.add_argument("lon", type=float)
    ap.add_argument("rings", type=int, nargs="?", default=1)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--anchor-km", type=float, default=15.0)
    ap.add_argument("--no-compare", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    import blockworker as bw
    import geodesy
    import geotiles
    import revisit
    import roads
    revisit.ON = False
    bw._s = bw._State(os.path.expanduser("~/.cache/beamng-earth"), args.offline, True, True, True, True, True, True)
    out = tempfile.mkdtemp(prefix="roadgeoprobe_")
    try:
        lat, lon = args.lat, args.lon
        lon_b = lon + args.anchor_km * 1000.0 / (111320.0 * math.cos(math.radians(lat)))
        fa, fb = geodesy.EnuFrame(lat, lon), geodesy.EnuFrame(lat, lon_b)
        gx0, gy0 = geotiles.tile_of(lat, lon)
        keys = [(gx0 + i, gy0 + j) for j in range(-args.rings, args.rings + 1) for i in range(-args.rings, args.rings + 1)]
        q = 1 / 32
        fails, cpu = 0, {"content": 0.0, "skin_a": 0.0, "skin_b": 0.0, "exact_b": 0, "anchor": 0.0}
        skins = {}
        for gx, gy in keys:
            try:
                r = bw.roads_geo(1, lat, lon, 15, gx, gy, 15, out, "/x", True, 0.0, True, False, True, q, True,
                                 exact="auto")
                cpu["content"] += r["time"]["content_cpu"]
                cpu["skin_a"] += r["time"]["skin_cpu"]
                skins[(gx, gy)] = r["skin"][0] if r["skin"] else None
                rb = bw.roads_geo(2, lat, lon_b, 15, gx, gy, 15, out, "/x", True, 0.0, True, False, True, q, True,
                                  content=False, skin=True, exact=roads.geo_exact_auto(geotiles.frame(gx, gy), fb,
                                                                        r["content"][2].get("parts")))
                cpu["skin_b"] += rb["time"]["skin_cpu"]
                cpu["exact_b"] += len(rb["exact"])
                print(f"geotile {geotiles.key(gx, gy)} ok: content {r['time']['content_cpu']:.2f} CPU-s, skin "
                      f"{r['time']['skin_cpu']:.2f} / {rb['time']['skin_cpu']:.2f}, exact for B {sorted(rb['exact'])}",
                      flush=True)
            except Exception:
                fails += 1
                print(f"geotile {gx}_{gy} FAILED")
                traceback.print_exc()
        # seams: skins inside their quads; shared corners bit-equal
        worst_out = 0.0
        for (gx, gy), path in skins.items():
            if not path:
                continue
            q4 = geotiles.quad(fa, gx, gy)
            data = json.load(open(os.path.join(out, os.path.basename(path))))
            for w in ("road", "walk", "walkcol"):
                if w in data:
                    P, _T, _M = mesh_of(os.path.join(out, os.path.basename(data[w]["shape"])), data[w]["pos"])
                    for p in P[:, :2]:
                        worst_out = max(worst_out, geotiles.quad_dist(q4, float(p[0]), float(p[1])))
        import grading as gradingmod
        import roadmesh
        g, _ = bw._s.anchor(1, lat, lon)

        def elev(la, lo):
            z = bw.ground(fa, la, lo, bw._s.dem.sample(la, lo, 15))
            return g.apply(la, lo, z, gradingmod.ROAD_LIFT) if g is not None else z
        def on_ground(meshes):
            on, alln = 0, 0
            for P, T, M in meshes:
                gz = roadmesh.drawn_ground(fa, elev, P[:, :2], q)
                dz = P[:, 2] - gz
                for mat in set(M):
                    v = np.unique(T[M == mat])
                    d = dz[v]
                    d = d[np.isfinite(d) & (np.abs(d) < 0.3)]          # (decks: their own heights)
                    if not len(d):
                        continue
                    med = float(np.median(d))
                    on += int((np.abs(d - med) <= 0.001).sum())
                    alln += len(d)
            return on, alln
        sk_meshes = []
        for (gx, gy), path in skins.items():
            data = json.load(open(os.path.join(out, os.path.basename(path)))) if path else {}
            if "road" in data:
                sk_meshes.append(mesh_of(os.path.join(out, os.path.basename(data["road"]["shape"])),
                                         data["road"]["pos"]))
        on, alln = on_ground(sk_meshes)
        print(f"skin vertices on the drawn ground (+ their layer's lift): {on}/{alln} within 1 mm "
              f"({100.0 * on / max(alln, 1):.2f} %)")
        same = all(np.array_equal(geotiles.quad(fa, gx, gy)[1], geotiles.quad(fa, gx + 1, gy)[0]) and
                   np.array_equal(geotiles.quad(fa, gx, gy)[3], geotiles.quad(fa, gx, gy - 1)[0])
                   for gx, gy in keys)
        print(f"seams: skin vertices at most {worst_out * 1000:.3f} mm outside their quads (float32 shapes); "
              f"shared corners bit-equal: {same}")
        if not args.no_compare:
            # skin == the anchor tiles' road mesh (same materials, the same heights)
            old = os.path.join(out, "old")
            os.makedirs(old)
            ts = set()
            for gx, gy in keys:
                q4 = geotiles.quad(fa, gx, gy)
                for x in (q4[:, 0].min(), q4[:, 0].max()):
                    for y in (q4[:, 1].min(), q4[:, 1].max()):
                        ts.add((int(math.floor(x / roads.TILE)), int(math.floor(y / roads.TILE))))
            c0 = time.process_time()
            olds = {}
            for tx, ty in sorted(ts):
                path, _n, _p = bw.roads_tile(1, lat, lon, 15, tx, ty, old, "/x", True, 0.0, buildings=True,
                                             gas_stations=True, q=q)
                d = json.load(open(os.path.join(old, os.path.basename(path))))
                if "road" in d:
                    olds[(tx, ty)] = mesh_of(os.path.join(old, os.path.basename(d["road"]["shape"])), d["road"]["pos"])
            cpu["anchor"] = time.process_time() - c0
            on, alln = on_ground(list(olds.values()))
            print(f"anchor tiles' vertices on the drawn ground: {on}/{alln} within 1 mm ({100.0 * on / max(alln, 1):.2f} %)")
            dz, n_in, n_found = [], 0, 0
            for (gx, gy), path in skins.items():
                if not path:
                    continue
                data = json.load(open(os.path.join(out, os.path.basename(path))))
                if "road" not in data:
                    continue
                P, T, M = mesh_of(os.path.join(out, os.path.basename(data["road"]["shape"])), data["road"]["pos"])
                cen = P[T].mean(axis=1)
                rng = np.random.default_rng(gx * 7919 + gy)
                pick = rng.choice(len(T), min(len(T), 400), replace=False) if len(T) else []
                for i in pick:
                    c = cen[i]
                    tx, ty = int(math.floor(c[0] / roads.TILE)), int(math.floor(c[1] / roads.TILE))
                    x0, y0, x1, y1 = roads.tile_bbox(tx, ty)
                    if min(c[0] - x0, x1 - c[0], c[1] - y0, y1 - c[1]) < 1.0 or (tx, ty) not in olds:
                        continue
                    n_in += 1
                    z = heights_at(*olds[(tx, ty)], c[None, :2], M[i], c[None, 2])[0]
                    if np.isfinite(z):
                        n_found += 1
                        dz.append(abs(z - c[2]) if np.isfinite(c[2]) else np.inf)
                        if dz[-1] > 0.02 and os.environ.get("PROBE_SHOW"):
                            print(f"    off: {geotiles.key(gx, gy)} ({c[0]:.1f}, {c[1]:.1f}) {M[i]} skin {c[2]:.3f} "
                                  f"anchor tile {z:.3f}")
            dz = np.asarray(dz)
            big = np.flatnonzero(dz > 0.02)
            if len(big):
                print(f"  {len(big)} skin triangles more than 2 cm off the anchor tiles' mesh of their material")
            print(f"skin vs anchor tiles: {n_found}/{n_in} skin triangles found in the old mesh (same material); "
                  f"|dz| max {dz.max() * 1000 if len(dz) else 0:.3f} mm, p99 {np.percentile(dz, 99) * 1000 if len(dz) else 0:.3f} mm")
        print(f"CPU-s: content {cpu['content']:.1f} ({len(keys)} geotiles), skins anchor A {cpu['skin_a']:.2f}, "
              f"anchor B (the rebase) {cpu['skin_b']:.2f} with {cpu['exact_b']} exact variants; anchor tiles "
              f"{cpu['anchor']:.1f}; failures {fails}; stats {bw.geo_stats()}")
        return 1 if fails else 0
    finally:
        shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
