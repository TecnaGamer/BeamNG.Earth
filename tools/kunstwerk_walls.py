#!/usr/bin/env python3
"""The retaining walls of a sunken road from the AHN, for modelling it in Blender (tools/blender_ref.py's reference):
the trench (ground under --low m NAP in --band, its largest piece) traced along its rim, smoothed, and kept where the
ground rises more than --steep m within 4 m outward (a wall; less is a grass slope). Writes walls.json (per wall its line,
floor, top and outward normals, local RD metres) and walls.png (them over the aerial photo).

    python3 tools/kunstwerk_walls.py ../design/kunstwerken/droogleever/ref --steep 0.8
"""
import argparse
import json
import os
from collections import deque

import numpy as np


def trench_mask(dtm, X, Y, low, band):
    """The largest 4-connected piece of ground under `low` inside the band (a, b, c, w: |a x + b y - c| < w)."""
    a, b, c, w = band
    m = (dtm < low) & (np.abs(a * X + b * Y - c) < w) & np.isfinite(dtm)
    H, W = m.shape
    lab = np.zeros(m.shape, np.int32)
    best, best_n, cur = 0, 0, 0
    for r0, c0 in zip(*np.nonzero(m)):
        if lab[r0, c0]:
            continue
        cur += 1
        q = deque([(r0, c0)])
        lab[r0, c0] = cur
        n = 0
        while q:
            r, cc = q.popleft()
            n += 1
            for rr, c2 in ((r + 1, cc), (r - 1, cc), (r, cc + 1), (r, cc - 1)):
                if 0 <= rr < H and 0 <= c2 < W and m[rr, c2] and not lab[rr, c2]:
                    lab[rr, c2] = cur
                    q.append((rr, c2))
        if n > best_n:
            best, best_n = cur, n
    return lab == best


def rim_loops(m):
    """The mask's outline as loops of cell corners (r, c)."""
    H, W = m.shape
    pad = np.pad(m, 1)
    edges = {}
    for dr, dc, a, b in ((-1, 0, (0, 0), (0, 1)), (1, 0, (1, 1), (1, 0)), (0, -1, (1, 0), (0, 0)), (0, 1, (0, 1), (1, 1))):
        rr, cc = np.nonzero(m & ~pad[1 + dr:H + 1 + dr, 1 + dc:W + 1 + dc])
        for r, c in zip(rr, cc):
            edges.setdefault((r + a[0], c + a[1]), []).append((r + b[0], c + b[1]))
    loops, used = [], set()
    for start in list(edges):
        for nxt in edges[start]:
            if (start, nxt) in used:
                continue
            loop, cur, n2 = [start], start, nxt
            while (cur, n2) not in used:
                used.add((cur, n2))
                loop.append(n2)
                cur = n2
                cands = [q for q in edges.get(cur, []) if (cur, q) not in used]
                if not cands:
                    break
                n2 = cands[0]
            if len(loop) > 20:
                loops.append(np.array(loop, float))
    return loops


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("ref")
    ap.add_argument("--low", type=float, default=0.3, help="m NAP: the trench is the ground under this")
    ap.add_argument("--band", default="1,0.33,10,45", help="a,b,c,w: the band |a x + b y - c| < w along the trench")
    ap.add_argument("--steep", type=float, default=0.8, help="m of rise within 4 m outward that makes a wall")
    ap.add_argument("--min-len", type=int, default=4, help="m: shorter runs left out")
    a = ap.parse_args(argv)
    d = np.load(os.path.join(a.ref, "ahn.npz"))
    dtm = d["dtm"].astype(float)
    res, x0, y1 = float(d["res"]), float(d["x0"]), float(d["y1"])
    H, W = dtm.shape
    X, Y = np.meshgrid(x0 + (np.arange(W) + 0.5) * res, y1 - (np.arange(H) + 0.5) * res)
    m = trench_mask(dtm, X, Y, a.low, [float(v) for v in a.band.split(",")])
    np.save(os.path.join(a.ref, "trench_mask.npy"), m)

    def zat(p):
        c = np.clip(((p[:, 0] - x0) / res - 0.5).astype(int), 0, W - 1)
        r = np.clip(((y1 - p[:, 1]) / res - 0.5).astype(int), 0, H - 1)
        return dtm[r, c]
    walls = []
    for L in rim_loops(m):
        P = np.c_[x0 + L[:, 1] * res, y1 - L[:, 0] * res]
        for _ in range(12):                 # the 0.5 m staircase smoothed
            P[1:-1] = 0.25 * P[:-2] + 0.5 * P[1:-1] + 0.25 * P[2:]
        s = np.r_[0, np.cumsum(np.hypot(*np.diff(P, axis=0).T))]
        t = np.arange(0, s[-1], 1.0)
        P = np.c_[np.interp(t, s, P[:, 0]), np.interp(t, s, P[:, 1])]
        dv = np.gradient(P, axis=0)
        dv /= np.maximum(np.hypot(*dv.T), 1e-9)[:, None]
        nrm = np.c_[-dv[:, 1], dv[:, 0]]
        out = np.where((zat(P - nrm * 1.5) > zat(P + nrm * 1.5))[:, None], -nrm, nrm)    # toward the higher ground
        floor = np.minimum(zat(P - out * 1.5), zat(P - out * 0.8))
        top = np.maximum.reduce([zat(P + out * k) for k in (1.0, 2.0, 3.0, 4.0)])
        steep = (top - floor) > a.steep
        k = 0
        while k < len(P):
            if not steep[k]:
                k += 1
                continue
            j = k
            while j < len(P) and steep[j]:
                j += 1
            if j - k >= a.min_len:
                walls.append({"line": P[k:j].round(3).tolist(), "floor": floor[k:j].round(3).tolist(),
                              "top": top[k:j].round(3).tolist(), "out": out[k:j].round(4).tolist()})
            k = j
    json.dump({"walls": walls}, open(os.path.join(a.ref, "walls.json"), "w"))
    try:
        from PIL import Image
        img = np.array(Image.open(os.path.join(a.ref, "photo.jpg")).convert("RGB").resize((W, H)))
        for w in walls:
            for x, y in w["line"]:
                c, r = int((x - x0) / res), int((y1 - y) / res)
                img[max(r - 1, 0):r + 2, max(c - 1, 0):c + 2] = (255, 255, 0)
        Image.fromarray(img).save(os.path.join(a.ref, "walls.png"))
    except Exception as e:
        print("png:", e)
    print(f"trench {m.sum() * res * res:.0f} m2; walls {len(walls)}, {sum(len(w['line']) for w in walls)} m")


if __name__ == "__main__":
    main()
