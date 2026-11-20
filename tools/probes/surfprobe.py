"""surfprobe.py LAT LON R --cache DIR [--json OUT] [--render OUT.jpg] [--half M] [--tiles]

Road surface v3 phase S2's probe (BNE_ROADSURF_V3=surface; roadsurf3.py, earthfast.lane_surface). For every road tile
of an anchor at the point within R it builds the v3 ground surface as a road tile does (roadsurf3.tile_surface: the lane
graph and the roads from the map cells round the tile) and measures:

  overlap_m2     the surface's triangles (pieces and road strips) covering a point twice, on a RASTER_M raster, per
                 tile inside its own box (the drawn mesh is cut there): 0 by construction
  width_rate     per kept piece the largest change of its paved width per metre between rows (m / m), against its
                 taper's own peak (the quintic's 1.875 x lane width / taper length)
  seam           every pair of neighbouring tiles: each tile's mesh cut at its own box (earthfast.clip_tris_convex, as
                 roadmesh.build), the used vertices on the shared edge compared bit for bit, and the
                 triangles both tiles keep before the cut the same bits
  pieces         the kernel's status per piece (kept, trimmed, welds, free ends, deck joins and their gaps)

--render OUT.jpg: the tile mesh round the point (--half m) on the finest aerial (tools/ortho_hi.py): through lanes grey,
branch / added lanes cyan, gores magenta, shoulders dark, road strips blue edges, the markings white / yellow.
--tiles: also build the real road tiles (blockworker.roads_tile, the revisit cache bypassed) and time them (wall s per
tile), the A/B of the switch (run once with BNE_ROADSURF_V3=off in the environment, once with surface).

A measurement tool (Python is fine here); nothing in the game reads it. The scratch cache must not be the player's
~/.cache/beamng-earth (refused)."""
import argparse
import json
import math
import os
import subprocess
import sys
import time

HERE = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "server"))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("BNE_ROADSURF_V3", "surface")

import numpy as np  # noqa: E402

from forkprobe import _refuse_player_cache, merc  # noqa: E402

RASTER_M = 0.1
SEAM_TOL_M = 1e-6


def raster_keys(tri_xy, cell=RASTER_M):
    """Raster keys of triangles (n, 3, 2) (structprobe.raster_tris' top-left rule: a shared edge counted once)."""
    import structprobe
    t = np.asarray(tri_xy, np.float64).reshape(-1, 3, 2)
    t3 = np.concatenate([t, np.zeros(t.shape[:2] + (1,))], axis=2)
    keys, _z, idx = structprobe.raster_tris(t3, cell)
    return keys, idx


def overlap_m2(v, tri, box, owner=None, cell=RASTER_M):
    """m^2 of box covered twice by the triangles (v (n, 2), tri (m, 3)): raster points inside two of them; where
    (the first points) and by owner pair kind (piece-piece, piece-road, road-road same / other road; owner: the
    kernel's per triangle)."""
    if not len(tri):
        return 0.0, [], {}
    keys, idx = raster_keys(v[tri], cell)
    import structprobe
    xy = structprobe.key_xy(keys, cell)
    inb = (xy[:, 0] >= box[0]) & (xy[:, 0] < box[2]) & (xy[:, 1] >= box[1]) & (xy[:, 1] < box[3])
    keys, idx = keys[inb], idx[inb]
    order = np.argsort(keys, kind="stable")
    keys, idx = keys[order], idx[order]
    u, first, c = np.unique(keys, return_index=True, return_counts=True)
    dup = u[c > 1]
    kinds = {}
    if owner is not None:
        for f, n in zip(first[c > 1], c[c > 1]):
            os_ = sorted({int(owner[idx[f + j]]) for j in range(n)})
            if len(os_) == 1:
                k = "same_owner"
            elif all(o >= 0 for o in os_):
                k = "piece-piece"
            elif any(o >= 0 for o in os_):
                k = "piece-road"
            else:
                k = "road-road"
            kinds[k] = kinds.get(k, 0.0) + cell * cell
    where = structprobe.key_xy(dup[:20], cell).round(1).tolist() if len(dup) else []
    return float(len(dup)) * cell * cell, where, {k: round(v, 2) for k, v in kinds.items()}


def width_rates(got):
    """Per kept piece: the steepest change of a lane strip's width per m between its rows and of a shoulder's (the
    kernel's status; the gore opens as it will), against the steepest the piece's own taper has (the quintic's peak,
    1.875 x lane width / taper length; a piece without one: its rule's exit / entry taper)."""
    out = []
    st = {s["piece"]: s for s in got["status"] if s.get("kept")}
    for k in got["kept"]:
        ev = got["pieces"][k][0]
        s = ev.get("s", {})
        lw = ev.get("lane_w", 3650) / 1000.0
        tl = abs(s.get("taper1", 0) - s.get("taper0", 0)) / 1000.0
        rule = ev.get("rule", {})
        if tl <= 1.0:
            tl = max(rule.get("exit_taper", 0), rule.get("entry_taper", 0), rule.get("taper", 0)) / 1000.0
        peak = 1.875 * lw / tl if tl > 1.0 else None
        out.append({"piece": k, "node": ev["node"][:12], "kind": ev["kind"], "rate": st[k].get("lane_rate"),
                    "at_s": st[k].get("lane_rate_s"), "shoulder": st[k].get("shoulder_rate"),
                    "taper_peak": None if peak is None else round(peak, 4), "flags": st[k].get("flags")})
    return out


def clip_tile(v, tri, box):
    import fast
    P = np.c_[v, np.zeros(len(v))]
    poly = np.array([[box[0], box[1]], [box[2], box[1]], [box[2], box[3]], [box[0], box[3]]], np.float64)
    P2, _uv, T2, _src = fast._ef.clip_tris_convex(np.ascontiguousarray(P), np.zeros((len(P), 2)),
                                                 np.ascontiguousarray(tri, np.int64), poly, True)
    return np.asarray(P2)[:, :2], np.asarray(T2)


def seam_check(a, b, box_a, box_b):
    """The shared edge of two tiles' boxes: their cut meshes' vertices on it (sorted) the same, and the triangles both
    keep before the cut (wholly inside both margins) the same bits."""
    va, ta = clip_tile(a["mesh"]["v"], a["mesh"]["tri"], box_a)
    vb, tb = clip_tile(b["mesh"]["v"], b["mesh"]["tri"], box_b)
    if box_a[2] == box_b[0]:
        axis, at = 0, box_a[2]
    elif box_a[3] == box_b[1]:
        axis, at = 1, box_a[3]
    else:
        return None
    # clip_tris_convex also returns the original (possibly unused) vertices.
    va, vb = va[np.unique(ta)], vb[np.unique(tb)]
    on_a = np.unique(va[np.abs(va[:, axis] - at) < 1e-7], axis=0)
    on_b = np.unique(vb[np.abs(vb[:, axis] - at) < 1e-7], axis=0)
    same_pts = len(on_a) == len(on_b) and (len(on_a) == 0 or float(np.max(np.abs(on_a - on_b))) <= SEAM_TOL_M)

    def tris_in(got, lo, hi):
        v, t = got["mesh"]["v"], got["mesh"]["tri"]
        c = v[t]
        m = (c[:, :, axis].min(1) >= lo) & (c[:, :, axis].max(1) <= hi)
        return sorted(x.tobytes() for x in c[m])
    band = (at - 6.0, at + 6.0)
    same_tris = tris_in(a, *band) == tris_in(b, *band)
    return {"edge_pts": [len(on_a), len(on_b)], "same_pts": same_pts,
            "same_bits": on_a.tobytes() == on_b.tobytes(), "same_tris": same_tris}


def render(got, frame, lat, lon, half, out):
    from PIL import Image, ImageDraw
    img = out + ".aerial.jpg"
    if not os.path.exists(img):
        subprocess.run([sys.executable, os.path.join(HERE, "..", "tools", "ortho_hi.py"), repr(float(lat)),
                        repr(float(lon)), str(round(half)), img, "auto", "1600"], check=True, stdout=subprocess.DEVNULL)
    im = Image.open(img).convert("RGB")
    W_, H_ = im.size
    kk = 1.0 / math.cos(math.radians(lat))  # det-ok: a probe's drawing, never an output
    cx, cy = merc(lat, lon)
    x0, y1 = cx - half * kk, cy + half * kk
    sc = W_ / (2 * half * kk)

    def px(xy):
        la, lo, _ = frame.enu_to_geodetic(np.asarray(xy[:, 0]), np.asarray(xy[:, 1]), 0.0)
        out_ = []
        for a, b in zip(np.atleast_1d(la), np.atleast_1d(lo)):
            x, y = merc(float(a), float(b))
            out_.append(((x - x0) * sc, (y1 - y) * sc))
        return out_
    d = ImageDraw.Draw(im, "RGBA")
    m = got["mesh"]
    col = {0: (60, 60, 60, 120), 1: (150, 150, 150, 90), 2: (255, 0, 255, 120), 3: (0, 230, 255, 100),
           4: (0, 230, 255, 100)}
    v = m["v"]
    for t, o, k in zip(m["tri"], m["owner"], m["kind"]):
        c = (40, 90, 255, 70) if o < 0 else col.get(int(k), (255, 255, 255, 60))
        d.polygon(px(v[t]), fill=c)
    lc = {"edge": (255, 255, 255, 255), "edge_left": (255, 220, 0, 255), "lane": (255, 255, 255, 255),
          "exit": (255, 255, 255, 255), "channel": (255, 255, 255, 255), "stripe": (255, 255, 255, 255)}
    for name, xy, _ph, _p in got["lines"]:
        d.line(px(xy), fill=lc[name], width=2 if name in ("lane", "exit") else 3)
    d.text((8, 8), f"v3 S2 surface: {len(m['tri'])} tris, {len(got['kept'])} pieces (grid none)", fill=(255, 255, 255, 255))
    im.save(out, quality=90)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("lat", type=float)
    ap.add_argument("lon", type=float)
    ap.add_argument("R", type=float)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--json")
    ap.add_argument("--render")
    ap.add_argument("--half", type=float, default=200.0)
    ap.add_argument("--tiles", action="store_true")
    ap.add_argument("--only-tiles", action="store_true", help="the tile timing alone, first (a cold process)")
    ap.add_argument("--fixture", help="write the kernel inputs of the first pair of neighbouring tiles that both keep "
                                      "a piece crossing their seam (server/tests/fixtures/, test_seamprobe's v3 case)")
    a = ap.parse_args()
    cache = _refuse_player_cache(a.cache)
    import geodesy
    import mapdata
    import netfetch
    import roads as roadsmod
    import roadsurf3
    md = mapdata.MapData(cache, fetcher=netfetch.Fetcher(offline=True))
    def cell_fn(key):           # (offline: a cell the scratch cache lacks is no roads, the same in every tile)
        try:
            return md.cell(key, layers=("roads",), kind="demand").layers.get("roads")
        except Exception:       # noqa: BLE001
            return None
    frame = geodesy.EnuFrame(a.lat, a.lon)
    res = {"place": [a.lat, a.lon], "R": a.R, "tiles": {}, "seams": [], "width": [], "overlap_m2": 0.0}
    if a.only_tiles:
        res["tile_build"] = time_tiles(a.lat, a.lon, a.R, cache)
        res["mode"] = os.environ.get("BNE_ROADSURF_V3")
        with open(a.json, "w") as fh:
            fh.write(json.dumps(res, indent=1, sort_keys=True))
        print(res["tile_build"])
        return
    got_of = {}
    t0 = time.perf_counter()
    for tx, ty in sorted(roadsmod.tiles_near(0.0, 0.0, a.R)):
        box = roadsmod.tile_bbox(tx, ty)
        got = roadsurf3.tile_surface(frame, box, cell_fn, country="US" if a.lon < -30 else None)
        if got is None:
            continue
        got_of[(tx, ty)] = (got, box)
        ov, where, by = overlap_m2(got["mesh"]["v"], got["mesh"]["tri"], box, got["mesh"]["owner"])
        res["overlap_m2"] += ov
        for k, v_ in by.items():
            res.setdefault("overlap_by", {})[k] = round(res.get("overlap_by", {}).get(k, 0.0) + v_, 2)
        res["tiles"][f"{tx},{ty}"] = {"tris": int(len(got["mesh"]["tri"])), "pieces": len(got["kept"]),
                                      "overlap_m2": round(ov, 2), "where": where[:5], "by": by}
    res["kernel_s"] = round(time.perf_counter() - t0, 2)
    seen = set()
    for (tx, ty), (got, box) in sorted(got_of.items()):
        for k, ol in zip(got["kept"], got["outlines"]):
            key = got["pieces"][k][0]["node"] + got["pieces"][k][0]["kind"]
            if key in seen:
                continue
            seen.add(key)
            res["width"].extend(w for w in width_rates(got) if w["piece"] == k)
        for dx, dy in ((1, 0), (0, 1)):
            nb = got_of.get((tx + dx, ty + dy))
            if nb is not None:
                r = seam_check(got, nb[0], box, nb[1])
                if r is not None:
                    res["seams"].append({"tiles": [f"{tx},{ty}", f"{tx + dx},{ty + dy}"], **r})
    if a.fixture:
        for (tx, ty), (got, box) in sorted(got_of.items()):
            for dx, dy in ((1, 0), (0, 1)):
                nb = got_of.get((tx + dx, ty + dy))
                if nb is None or not got["kept"] or not nb[0]["kept"]:
                    continue
                shared = {got["pieces"][k][0]["node"] for k in got["kept"]} & \
                    {nb[0]["pieces"][k][0]["node"] for k in nb[0]["kept"]}
                if not shared:
                    continue
                arrs = {"box_a": np.array(box, np.float64), "box_b": np.array(nb[1], np.float64)}
                for tag, g in (("a", got), ("b", nb[0])):
                    for k, v in g["inputs"].items():
                        arrs[f"{tag}_{k}"] = np.asarray(v)
                np.savez_compressed(a.fixture, **arrs)
                res["fixture"] = {"tiles": [f"{tx},{ty}", f"{tx + dx},{ty + dy}"], "pieces": sorted(shared)}
                break
            if "fixture" in res:
                break
    centre = got_of.get((0, 0)) or (next(iter(got_of.values())) if got_of else None)
    if centre is not None:
        res["status"] = centre[0]["status"]
    res["overlap_m2"] = round(res["overlap_m2"], 2)
    if a.render and got_of:
        merged = _merge([g for g, _b in got_of.values()], a.half, frame)
        render(merged, frame, a.lat, a.lon, a.half, a.render)
        res["render"] = a.render
    if a.tiles:
        res["tile_build"] = time_tiles(a.lat, a.lon, a.R, cache)
    txt = json.dumps(res, indent=1, sort_keys=True)
    if a.json:
        with open(a.json, "w") as fh:
            fh.write(txt)
    print(json.dumps({k: res[k] for k in ("overlap_m2", "overlap_by", "kernel_s") if k in res}),
          "seams", sum(1 for s in res["seams"] if s["same_bits"] and s["same_tris"]), "/", len(res["seams"]),
          "max width rate", max((w["rate"] for w in res["width"]), default=None))


def _merge(gots, half, frame):
    """One drawing of several tiles' kernel meshes (each tile's own box part: duplicates are harmless in a picture)."""
    v, tri, owner, kind, lines = [], [], [], [], []
    n = 0
    for g in gots:
        m = g["mesh"]
        v.append(m["v"])
        tri.append(m["tri"] + n)
        owner.append(m["owner"])
        kind.append(m["kind"])
        n += len(m["v"])
        lines.extend(g["lines"])
    return {"mesh": {"v": np.concatenate(v), "tri": np.concatenate(tri), "owner": np.concatenate(owner),
                     "kind": np.concatenate(kind)}, "lines": lines, "kept": [p for g in gots for p in g["kept"]]}


def time_tiles(lat, lon, R, cache):
    """Wall seconds per real road tile (blockworker.roads_tile, no buildings, the revisit cache bypassed)."""
    os.environ.setdefault("BNE_REVISIT_SKIP", "roads")
    import logging
    import shutil
    import tempfile
    import blockworker as bw
    import roads as roadsmod
    import roadsurf3
    bw.init(cache, True, True, True, True, False, None, logging.ERROR, False, True)
    out = tempfile.mkdtemp(prefix="surfprobe")
    times, errors = [], []
    v3 = {"tiles": 0, "kept": 0, "tris": 0, "faults": []}
    orig_ts, orig_apply = roadsurf3.tile_surface, roadsurf3.apply

    def tolerant(cell_fn):      # (offline: a cell the seed lacks is no roads)
        def f(key):
            try:
                return cell_fn(key)
            except Exception:   # noqa: BLE001
                return None
        return f

    def ts(*a, **kw):
        got = orig_ts(*a, **kw)
        if got is not None:
            v3["tiles"] += 1
            v3["kept"] += len(got["kept"])
            v3["tris"] += int(len(got["mesh"]["tri"]))
        return got

    class Log:
        def warning(self, fmt, *args):
            v3["faults"].append((fmt % args)[:200])

    def apply(frame, box, chunks, cell_fn, *a, **kw):
        kw["log"] = Log()
        return orig_apply(frame, box, chunks, tolerant(cell_fn), *a, **kw)
    roadsurf3.tile_surface, roadsurf3.apply = ts, apply
    try:
        for tx, ty in sorted(roadsmod.tiles_near(0.0, 0.0, R)):
            t = time.perf_counter()
            try:
                bw.roads_tile("p", lat, lon, 15, tx, ty, out, "/x", True, True, buildings=False)
            except Exception as ex:     # noqa: BLE001
                errors.append(f"{tx},{ty}: {type(ex).__name__}: {ex}"[:200])
            times.append(time.perf_counter() - t)
    finally:
        roadsurf3.tile_surface, roadsurf3.apply = orig_ts, orig_apply
        shutil.rmtree(out, True)
    return {"n": len(times), "total_s": round(sum(times), 2), "max_s": round(max(times), 2) if times else 0,
            "errors": errors[:5], "v3": {**v3, "faults": v3["faults"][:3]}}


if __name__ == "__main__":
    main()
