"""markprobe.py LAT LON R [--cache DIR] [--offline] [--q Q] [--json OUT] [--server DIR]

The road revamp v2's overlay probe (P1c, .claude/work/road-revamp-v2-road-shape-first-2026-11-19.md section 2.7). It
builds the road tiles round a point the way the server does (blockworker.roads_tile, mesh mode: lift 0, the anchor's
height quantum) and measures every road mesh's overlays (markings, edge blends, gutters, wear, patches, the surface
variation: every chunk whose layer is not "base") against the ground they lie on:

  dz            overlay z minus the drawn ground under it (the terrain block's triangles: the asphalt is painted into
                it, roads.ROADS_IN_GROUND; roadmesh.drawn_ground) at the overlay's vertices, the lattice nodes inside an
                overlay triangle, where its edges cross the terrain's (grid lines and cell diagonals) and a RASTER_M
                raster: per layer min / p1 / p50 / p99, the samples below DZ_MIN_M and
                the negatives (an overlay under the ground: z-fighting, the asphalt showing through). Left out: an
                overlay further than LEVEL_DZ from the ground or over a chunk on fixed heights (a deck's), and the
                terrain cells holding a step (follow_ground's cap holds a surface down there on purpose: a line in
                front of a wall), with STEP_RING cells round them; "worst" says where the lowest samples are
  coverage      the share of the asphalt's raster points under at least one overlay triangle (any layer; the vanilla
                maps 72-93 %), and per layer
  cost          per tile: road mesh materials (one draw call each), triangles, points, and the bytes the tile wrote

Everything is rounded and sorted: the same code and data give the same JSON. A measurement tool (Python is fine
here); nothing in the game reads it. The scratch cache must not be the player's ~/.cache/beamng-earth (refused)."""
import argparse
import json
import logging
import os
import sys

SERVER = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "server"))
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)      # (the server's modules: this probe lives in tools/probes/)

import numpy as np  # noqa: E402

import dnp  # noqa: E402

RASTER_M = 0.25         # m between the raster's samples (dz and coverage)
CELL_M = 2.0            # m: the triangle lookup's buckets
LEVEL_DZ = 0.3          # m: an overlay further than this from the drawn ground lies on another level (a deck)
DZ_MIN_M = 0.002        # m: the P1c target, overlay at least this over its asphalt
STEP_RING = 0           # cells round a terrain cell holding a step (roadmesh.FOLLOW_STEP_M) left out with it
_JX, _JY = 0.0123457, 0.0098765     # m: the raster's points off the cell centres (no mesh vertex lies on one)


def lookup(tri, pts, cell=CELL_M):
    """(point index, triangle index, z) for every point of pts (n, 2) inside a triangle of tri (m, 3, 3): the
    triangle's plane z there (barycentric). Pure numpy, deterministic order (by point, then triangle)."""
    tri = np.asarray(tri, np.float64).reshape(-1, 3, 3)
    pts = np.asarray(pts, np.float64).reshape(-1, 2)
    e = np.zeros(0, np.int64)
    if not len(tri) or not len(pts):
        return e, e, np.zeros(0)
    lo = np.floor(tri[:, :, :2].min(1) / cell).astype(np.int64)
    hi = np.floor(tri[:, :, :2].max(1) / cell).astype(np.int64)
    w = hi[:, 0] - lo[:, 0] + 1
    cnt = w * (hi[:, 1] - lo[:, 1] + 1)
    ok = cnt <= 4096
    cnt = np.where(ok, cnt, 0)
    tid = np.repeat(np.arange(len(tri)), cnt)
    k = np.arange(int(cnt.sum())) - np.repeat(np.cumsum(cnt) - cnt, cnt)
    rw = np.repeat(w, cnt)
    ci = np.repeat(lo[:, 0], cnt) + k % rw
    cj = np.repeat(lo[:, 1], cnt) + k // rw
    key = ci * (1 << 31) + cj
    order = np.lexsort((tid, key))
    key, tid = key[order], tid[order]
    pc = np.floor(pts / cell).astype(np.int64)
    pk = pc[:, 0] * (1 << 31) + pc[:, 1]
    a = np.searchsorted(key, pk, "left")
    b = np.searchsorted(key, pk, "right")
    n = b - a
    pi = np.repeat(np.arange(len(pts)), n)
    if not len(pi):
        return e, e, np.zeros(0)
    ti = tid[np.repeat(a, n) + (np.arange(int(n.sum())) - np.repeat(np.cumsum(n) - n, n))]
    t = tri[ti]
    p = pts[pi]
    v0, v1, v2 = t[:, 0, :2], t[:, 1, :2], t[:, 2, :2]
    d = (v1[:, 1] - v2[:, 1]) * (v0[:, 0] - v2[:, 0]) + (v2[:, 0] - v1[:, 0]) * (v0[:, 1] - v2[:, 1])
    good = np.abs(d) > 1e-12
    d = np.where(good, d, 1.0)
    l0 = ((v1[:, 1] - v2[:, 1]) * (p[:, 0] - v2[:, 0]) + (v2[:, 0] - v1[:, 0]) * (p[:, 1] - v2[:, 1])) / d
    l1 = ((v2[:, 1] - v0[:, 1]) * (p[:, 0] - v2[:, 0]) + (v0[:, 0] - v2[:, 0]) * (p[:, 1] - v2[:, 1])) / d
    l2 = 1.0 - l0 - l1
    eps = -1e-9
    inside = good & (l0 >= eps) & (l1 >= eps) & (l2 >= eps)
    z = l0 * t[:, 0, 2] + l1 * t[:, 1, 2] + l2 * t[:, 2, 2]
    return pi[inside], ti[inside], z[inside]


def carriageway_mask(roads, box, step=RASTER_M):
    """(ny, nx) bool raster (RASTER_M over the tile box, row 0 at y0, the raster's points) of the paved ground
    carriageways: every paved road's centre line widened to its spec.width (segments and round joints)."""
    return ribbon_mask([(pts, float(spec.width) / 2) for _fid, spec, pts, *_ in roads
                        if getattr(spec, "kind", None) == "paved"], box, step)


def ribbon_mask(lines, box, step=RASTER_M):
    """carriageway_mask's raster of polylines (k, 2+) widened by their half widths: lines [(pts, half width)]."""
    from PIL import Image, ImageDraw
    nx = int(round((box[2] - box[0]) / step))
    ny = int(round((box[3] - box[1]) / step))
    im = Image.new("1", (nx, ny), 0)
    dr = ImageDraw.Draw(im)
    for pts, half in lines:
        if len(pts) < 2:
            continue
        p = (np.asarray(pts, np.float64)[:, :2] - [box[0] + _JX, box[1] + _JY]) / step - 0.5
        hw = float(half) / step
        for i in range(len(p) - 1):
            d = p[i + 1] - p[i]
            L = float(dnp.hypot(*d))
            if L < 1e-9:
                continue
            n = np.array([-d[1], d[0]]) / L * hw
            dr.polygon([tuple(p[i] + n), tuple(p[i + 1] + n), tuple(p[i + 1] - n), tuple(p[i] - n)], fill=1)
        for c in p:
            dr.ellipse([c[0] - hw, c[1] - hw, c[0] + hw, c[1] + hw], fill=1)
    return np.asarray(im, bool)


def tile_metrics(P, T, mats, M, layer_of_mat, box, ground, roads, decks=()):
    """dz samples per layer and coverage counts of one road mesh (world positions P (n, 3), triangles T, materials,
    per triangle material index M) over the drawn ground (ground(xy (n, 2)) -> z: the terrain block's triangles, which
    carry the painted asphalt: roads.ROADS_IN_GROUND), its raster limited to box (x0, y0, x1, y1); roads: the tile's
    ground roads (roadwear.tile_chunks's: the paved carriageways, coverage); decks: [(pts, half width)] of the chunks on
    their own fixed heights (a deck's surface and markings: not on the ground, left out of dz)."""
    P = np.asarray(P, np.float64)
    T = np.asarray(T, np.int64).reshape(-1, 3)
    M = np.asarray(M, np.int64)
    lay = np.array([layer_of_mat.get(m, "?") for m in mats], dtype=object)
    tl = lay[M] if len(M) else np.zeros(0, dtype=object)
    over = (tl != "base") & (tl != "?") & (tl != "frontage")
    out = {"dz": {}, "cov": {"asphalt": 0, "any": 0}, "covl": {}}
    xs = np.arange(box[0] + RASTER_M / 2, box[2], RASTER_M) + _JX
    ys = np.arange(box[1] + RASTER_M / 2, box[3], RASTER_M) + _JY
    gx, gy = np.meshgrid(xs, ys)
    grid = np.c_[gx.ravel(), gy.ravel()]
    asphalt = carriageway_mask(roads or (), box).ravel()
    if asphalt.shape[0] != len(grid):
        raise ValueError("raster / mask size")
    out["cov"]["asphalt"] = int(asphalt.sum())
    deck = ribbon_mask(decks, box)
    nyx = deck.shape
    # the terrain's lattice nodes over the box, and its cells holding a step (corners more than FOLLOW_STEP_M apart: a
    # wall or kerb the lattice draws as teeth; follow_ground holds the surfaces down there on purpose)
    import roadmesh as rm
    c = rm.GRID_M
    i0, j0 = int(np.ceil(box[0] / c)), int(np.ceil(box[1] / c))
    nxs = np.arange(i0, int(np.floor(box[2] / c)) + 1) * c
    nys = np.arange(j0, int(np.floor(box[3] / c)) + 1) * c
    ngx, ngy = np.meshgrid(nxs, nys)
    nodes = np.c_[ngx.ravel(), ngy.ravel()]
    nz = ground(nodes).reshape(ngx.shape)
    q4 = np.stack([nz[:-1, :-1], nz[:-1, 1:], nz[1:, :-1], nz[1:, 1:]])
    step = ~(np.nanmax(q4, 0) - np.nanmin(q4, 0) <= rm.FOLLOW_STEP_M)
    for _ in range(STEP_RING):          # (and the cells round one: a triangle with a capped corner reaches into them)
        s = step.copy()
        s[1:] |= step[:-1]
        s[:-1] |= step[1:]
        s[:, 1:] |= step[:, :-1]
        s[:, :-1] |= step[:, 1:]
        step = s

    def on_deck(xy):
        """On a deck (fixed heights) or in a step cell: not the drape's to answer for."""
        i = np.clip(np.rint((xy[:, 0] - box[0] - _JX) / RASTER_M - 0.5).astype(np.int64), 0, nyx[1] - 1)
        j = np.clip(np.rint((xy[:, 1] - box[1] - _JY) / RASTER_M - 0.5).astype(np.int64), 0, nyx[0] - 1)
        ci = np.floor(xy[:, 0] / c).astype(np.int64) - i0
        cj = np.floor(xy[:, 1] / c).astype(np.int64) - j0
        inside = (ci >= 0) & (cj >= 0) & (ci < step.shape[1]) & (cj < step.shape[0])
        st = np.ones(len(xy), bool)
        st[inside] = step[cj[inside], ci[inside]]
        return deck[j, i] | st
    otri_idx = np.flatnonzero(over)
    if not len(otri_idx):
        return out
    otri = P[T[otri_idx]]
    olay = tl[otri_idx]
    samples = {}
    opi, oti, oz = lookup(otri, grid)
    zg = ground(grid[opi]) if len(opi) else np.zeros(0)
    d = oz - zg
    ok = np.isfinite(d) & (np.abs(d) <= LEVEL_DZ)        # (an overlay on a deck: another level)
    ok &= ~on_deck(grid[opi])
    covered = np.zeros(len(grid), bool)
    covered[opi[ok]] = True
    out["cov"]["any"] = int((covered & asphalt).sum())
    out["worst"] = []
    for L in sorted(set(olay[oti].tolist())):
        m = ok & (olay[oti] == L)
        samples.setdefault(L, []).append(d[m])
        cl = np.zeros(len(grid), bool)
        cl[opi[m]] = True
        out["covl"][L] = int((cl & asphalt).sum())
        if m.any():                         # (the raster's lowest sample of the layer: where to look)
            k = np.flatnonzero(m)[int(np.argmin(d[m]))]
            out["worst"].append((round(float(d[k]) * 1000, 2), L, round(float(grid[opi[k], 0]), 2),
                                 round(float(grid[opi[k], 1]), 2)))
    # the overlays' own vertices (a hair inside their triangle, toward its centroid)
    cen = otri.mean(1, keepdims=True)
    vp = (otri + (cen - otri) * 1e-4).reshape(-1, 3)
    vl = np.repeat(olay, 3)
    inb = (vp[:, 0] >= box[0]) & (vp[:, 0] <= box[2]) & (vp[:, 1] >= box[1]) & (vp[:, 1] <= box[3])
    vd = vp[:, 2] - ground(vp[:, :2])
    vdeck = on_deck(vp[:, :2])
    for L in sorted(set(vl.tolist())):
        m = (vl == L) & inb & np.isfinite(vd) & (np.abs(vd) <= LEVEL_DZ) & ~vdeck
        samples.setdefault(L, []).append(vd[m])
    # the terrain's lattice nodes under an overlay face (its creases inside an overlay triangle)
    api, ati, az = lookup(otri, nodes)
    if len(api):
        nd = az - nz.ravel()[api]
        ndeck = on_deck(nodes[api])
        for L in sorted(set(olay[ati].tolist())):
            m = (olay[ati] == L) & np.isfinite(nd) & (np.abs(nd) <= LEVEL_DZ) & ~ndeck
            samples.setdefault(L, []).append(nd[m])
    # where an overlay edge crosses a terrain edge (x = k c, y = k c, the cells' south-west to north-east diagonals
    # y - x = k c): with the vertices and the nodes, every place the gap between two triangulated surfaces can be least
    E = np.concatenate([otri[:, [0, 1]], otri[:, [1, 2]], otri[:, [2, 0]]])
    el = np.concatenate([olay, olay, olay])
    for nx_, ny_ in ((1.0, 0.0), (0.0, 1.0), (-1.0, 1.0)):
        da = E[:, 0, 0] * nx_ + E[:, 0, 1] * ny_
        db = E[:, 1, 0] * nx_ + E[:, 1, 1] * ny_
        lo, hi = np.minimum(da, db), np.maximum(da, db)
        k0 = np.floor(lo / c).astype(np.int64) + 1
        cnt = np.clip(np.ceil(hi / c).astype(np.int64) - 1 - k0 + 1, 0, 256)
        idx = np.repeat(np.arange(len(E)), cnt)
        if not len(idx):
            continue
        k = k0[idx] + (np.arange(len(idx)) - np.repeat(np.cumsum(cnt) - cnt, cnt))
        line = k * c
        ok_ = (line > lo[idx]) & (line < hi[idx])
        idx, line = idx[ok_], line[ok_]
        f = (line - da[idx]) / (db[idx] - da[idx])
        p = E[idx, 0] + f[:, None] * (E[idx, 1] - E[idx, 0])
        inb = (p[:, 0] >= box[0]) & (p[:, 0] <= box[2]) & (p[:, 1] >= box[1]) & (p[:, 1] <= box[3])
        xd = p[:, 2] - ground(p[:, :2])
        good = inb & np.isfinite(xd) & (np.abs(xd) <= LEVEL_DZ) & ~on_deck(p[:, :2])
        for L in sorted(set(el[idx].tolist())):
            samples.setdefault(L, []).append(xd[good & (el[idx] == L)])
    out["dz"] = {L: np.concatenate(v) for L, v in samples.items()}
    return out


def summarize(dz):
    dz = np.asarray(dz, np.float64)
    if not len(dz):
        return {"n": 0}
    q = np.percentile(dz, [1, 50, 99])
    return {"n": int(len(dz)), "min_mm": round(float(dz.min()) * 1000, 2), "p1_mm": round(float(q[0]) * 1000, 2),
            "p50_mm": round(float(q[1]) * 1000, 2), "p99_mm": round(float(q[2]) * 1000, 2),
            "neg": int((dz < 0).sum()), "below_min": int((dz < DZ_MIN_M - 1e-6).sum())}


def collect(lat, lon, R, cache, offline=False, q=None):
    os.environ.setdefault("BNE_REVISIT_SKIP", "roads")
    import tempfile
    import shutil
    import blockworker as bw
    import geodesy
    import roads as roadsmod
    import roadmesh as rm
    bw.init(cache, bool(offline), True, True, True, False, None, logging.ERROR, False, True)
    frame = geodesy.EnuFrame(lat, lon)
    if q is None:
        try:
            import earthd
            q = earthd.survey_q(frame, bw._s.dem)
        except Exception:
            q = 1.0 / 64
    cur = {}
    orig = rm.build

    import roadwear
    orig_wear = roadwear.tile_chunks

    def g_build(frame_, chunks, elev, origin, *a, **kw):
        r = orig(frame_, chunks, elev, origin, *a, **kw)
        drawn, qq = kw.get("drawn") or elev, kw.get("q")
        cur.setdefault("meshes", []).append(
            (list(chunks), np.asarray(origin, np.float64), r,
             lambda xy, f=frame_, d=drawn, qq=qq: rm.drawn_ground(f, d, xy, qq)))
        return r

    def g_wear(roads, *a, **kw):
        cur.setdefault("roads", list(roads))
        return orig_wear(roads, *a, **kw)
    rm.build, roadwear.tile_chunks = g_build, g_wear
    tiles, errors = [], []
    out = tempfile.mkdtemp(prefix="markprobe")
    try:
        for tx, ty in roadsmod.tiles_near(0.0, 0.0, R):
            cur.clear()
            before = _bytes(out)
            try:
                bw.roads_tile("p", lat, lon, 15, tx, ty, out, "/x", True, 0.0, buildings=False, q=q)
            except Exception as ex:
                errors.append(f"tile {tx},{ty}: {type(ex).__name__}: {ex}"[:300])
                continue
            tiles.append(((tx, ty), roadsmod.tile_bbox(tx, ty), cur.get("meshes", [])[:1], _bytes(out) - before,
                          cur.get("roads", [])))
    finally:
        rm.build, roadwear.tile_chunks = orig, orig_wear
        shutil.rmtree(out, True)
    return {"tiles": tiles, "errors": errors, "q": q}


def _bytes(d):
    n = 0
    for root, _dirs, files in os.walk(d):
        for f in files:
            try:
                n += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return n


def score(inp):
    import roadmesh as rm
    dz, cov, covl, cost, tris_l, worst = {}, {"asphalt": 0, "any": 0}, {}, [], {}, []
    for (tx, ty), box, meshes, nbytes, roads in inp["tiles"]:
        row = {"tile": [tx, ty], "bytes": int(nbytes), "mats": 0, "tris": 0, "pts": 0}
        for chunks, origin, mesh, ground in meshes:
            if mesh is None:
                continue
            P, T, _UV, mats, M = mesh[:5]
            row.update(mats=len(mats), tris=int(len(T)), pts=int(len(P)))
            lom = {}
            for c in chunks:
                if c.get("m") is not None:
                    lom.setdefault(c["m"], rm.layer_of(c))
            counts = np.bincount(np.asarray(M, np.int64), minlength=len(mats))
            for j, m in enumerate(mats):
                L = lom.get(m, "?")
                tris_l[L] = tris_l.get(L, 0) + int(counts[j])
            decks = [(np.asarray(c["n"], np.float64), float(c.get("w") or 0.0) / 2 + 0.5) for c in chunks
                     if (c.get("fixed") or c.get("deck")) and c.get("n")]
            r = tile_metrics(np.asarray(P, np.float64) + origin, T, mats, M, lom, box, ground, roads, decks)
            for L, v in r["dz"].items():
                dz.setdefault(L, []).append(v)
            for k in cov:
                cov[k] += r["cov"][k]
            for L, v in r["covl"].items():
                covl[L] = covl.get(L, 0) + v
            worst.extend(r.get("worst", ()))
        cost.append(row)
    res = {"dz": {L: summarize(np.concatenate(v)) for L, v in sorted(dz.items())},
           "coverage": round(cov["any"] / max(1, cov["asphalt"]), 4),
           "coverage_by_layer": {L: round(v / max(1, cov["asphalt"]), 4) for L, v in sorted(covl.items())},
           "asphalt_m2": round(cov["asphalt"] * RASTER_M * RASTER_M, 1),
           "cost": sorted(cost, key=lambda r: r["tile"]),
           "q": inp["q"], "errors": sorted(inp["errors"])}
    alld = [np.concatenate(v) for v in dz.values()]
    res["dz_all"] = summarize(np.concatenate(alld)) if alld else {"n": 0}
    tot = {k: sum(r[k] for r in cost) for k in ("bytes", "tris", "pts")}
    tot["mats_mean"] = round(sum(r["mats"] for r in cost) / max(1, len(cost)), 2)
    tot["tiles"] = len(cost)
    res["cost_total"] = tot
    res["tris_by_layer"] = dict(sorted(tris_l.items()))
    res["worst"] = sorted(worst)[:8]        # (dz mm, layer, x, y in the place's frame)
    return res


def _refuse_player_cache(cache):
    real = os.path.realpath(os.path.expanduser("~/.cache/beamng-earth"))
    cache = os.path.realpath(os.path.expanduser(cache))
    if cache == real or cache.startswith(real + os.sep):
        sys.exit("refused: the player's cache")
    os.makedirs(cache, exist_ok=True)
    for root, dirs, files in os.walk(cache):
        for n in dirs + files:
            p = os.path.join(root, n)
            if os.path.islink(p) and os.path.realpath(p).startswith(real):
                sys.exit(f"refused: {p} links into the player's cache")
    return cache


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("lat", type=float)
    ap.add_argument("lon", type=float)
    ap.add_argument("r", type=float, nargs="?", default=200.0)
    ap.add_argument("--cache", default="~/bnge-tests/p1c/cache", help="a scratch cache (never the player's)")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--q", type=float, help="height quantum (default: the anchor's survey)")
    ap.add_argument("--json", help="write the result here (deterministic)")
    ap.add_argument("--server", help="probe this server/ checkout instead of the one beside this file")
    a = ap.parse_args()
    here = SERVER
    if a.server:
        srv = os.path.realpath(a.server)
        sys.path[:] = [srv] + [p for p in sys.path if os.path.realpath(p or ".") != here]
    elif here not in sys.path:
        sys.path.insert(0, here)
    cache = _refuse_player_cache(a.cache)
    res = score(collect(a.lat, a.lon, a.r, cache, a.offline, a.q))
    res["place"] = [a.lat, a.lon, a.r]
    print(json.dumps({k: res[k] for k in ("dz_all", "coverage", "asphalt_m2", "cost_total", "tris_by_layer", "q")}))
    for L, s in res["dz"].items():
        print(f"  {L:10s} {s}  cov {res['coverage_by_layer'].get(L, 0)}")
    print("  worst (mm, layer, x, y):", res["worst"][:4])
    if res["errors"]:
        print("errors:", res["errors"][:5])
    if a.json:
        with open(a.json + ".tmp", "w") as f:
            json.dump(res, f, sort_keys=True, separators=(",", ":"), allow_nan=False)
            f.write("\n")
        os.replace(a.json + ".tmp", a.json)
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
