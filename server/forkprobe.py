"""forkprobe.py LAT LON R --cache DIR [--json OUT] [--overlay DIR] [--refs REFS.json] [--half M] [--max N]

Road revamp v2 phase P2a's probe of the fork records (roadshape.cell_forks, earthfast.fork_records; BNE_ROADSHAPE=sheet
writes the same records beside the build). Reads the map cells round the point from a scratch cache (offline: the
cache's own data; never the player's ~/.cache/beamng-earth) and checks every fork whose node lies within R:

  width       the carriageway width along the record (near pavement edge - far edge per row, OUT_M apart) and its
              steps: max |dW| between rows, max |dW / ds|; seam_m: the record's first width against the trunk's
              mapped pavement; end_m: its width at the physical nose against the through's own pavement plus the
              gore and the branch's (the pieces meet)
  lanes       the record's lanes (trunk, through, branch) against its evidence: "lanes" (the trunk carries both, a
              lane drop / add), "turn_lanes" (the trunk's turn:lanes give the branch its own lanes), "design" (no
              lane evidence: the speed's design length); balance = lT - (lM + lB)
  noses       the painted nose's and physical nose's distance from the mapped split node along the main line, and the
              shared length (taper start .. painted nose)
  refs        with --refs {node id: [lat, lon] of the real physical nose measured on the aerial}: the record's
              physical nose's error in m

--overlay DIR: per fork (the --max nearest the point) the finest aerial (tools/ortho_hi.py) with the record drawn on
it: main line (white), far / near pavement edges (yellow), the branch's driving edges up to the physical nose (cyan),
the painted gore (magenta), the branch's own line (cyan, dashed by dots), taper start (green), painted nose (orange),
physical nose (red), mapped node (blue), a 10 m grid. A measurement tool (Python is fine here); nothing reads it."""
import argparse
import json
import math
import os
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _refuse_player_cache(path):
    p = os.path.realpath(os.path.expanduser(path))
    real = os.path.realpath(os.path.expanduser("~/.cache/beamng-earth"))
    if p == real or p.startswith(real + os.sep):
        sys.exit(f"refused: {path} is the player's cache")
    return p


def to_ll(rec, pts_mm):
    """lat / lon (k, 2) of points in mm in the record's cell frame."""
    import geodesy
    fr = geodesy.EnuFrame(*rec["frame"])
    p = np.asarray(pts_mm, np.float64).reshape(-1, 2) / 1000.0
    la, lo, _ = fr.enu_to_geodetic(p[:, 0], p[:, 1], 0.0)
    return np.c_[np.asarray(la, np.float64), np.asarray(lo, np.float64)]


def ground_m(a, b):
    """Metres between two lat / lon points (local flat earth: probe use only)."""
    k = math.cos(math.radians((a[0] + b[0]) / 2.0))  # det-ok: a probe's measurement / drawing, never an output
    return math.hypot((a[1] - b[1]) * 111320.0 * k, (a[0] - b[0]) * 110574.0)


def edges(rec):
    """Per row: s (m) and xy (m) of the far edge, main near driving edge, branch inner / outer driving edges, near
    pavement edge, centre."""
    r = np.asarray(rec["rows"], np.float64)
    s = r[:, 0] / 1000.0
    c = r[:, 1:3] / 1000.0
    n = r[:, 3:5] / 1e6
    offs = r[:, 5:10] / 1000.0
    out = {"s": s, "centre": c}
    for k, name in enumerate(("far", "main_drive", "branch_in", "branch_out", "near")):
        out[name] = c + n * offs[:, k:k + 1]
    out["W"] = offs[:, 4] - offs[:, 0]
    return out


def score_record(rec):
    """The probe's numbers for one resolved record (module doc)."""
    e = edges(rec)
    s, W = e["s"], e["W"]
    ds = np.diff(s)
    dW = np.abs(np.diff(W))
    w = [v / 1000.0 for v in rec["w"]]          # dM, hM, dB, hB, trunk pavement
    dM, hM, dB, hB, paveT = w
    st = {k: v / 1000.0 for k, v in rec["s"].items()}
    lT, lM, lB = rec["lanes"]
    # the width at the physical nose: the through's pavement, the gore's pavement gap and the branch's pavement
    w_n = float(np.interp(st["physical"], s, W))  # det-ok: a probe's measurement / drawing, never an output
    import roadshape
    expect_n = (dM + 2 * hM) + roadshape.PHYS_GAP_M + (dB + 2 * hB)     # far .. near across the nose
    return {
        "node": rec["node"], "kind": rec["kind"], "side": rec["side"], "evidence": rec["evidence"],
        "lanes": [lT, lM, lB], "balance": (lT - lM - lB) if min(lT, lM, lB) >= 1 else None,
        "painted_from_node_m": round(st["painted"], 2), "physical_from_node_m": round(st["physical"], 2),
        "shared_m": round(st["painted"] - st["taper"], 2), "taper_m": round(st["full"] - st["taper"], 2),
        "gore_m": round(st["physical"] - st["painted"], 2),
        "max_step_m": round(float(dW.max()) if len(dW) else 0.0, 4),
        "max_slope": round(float((dW / np.maximum(ds, 1e-9)).max()) if len(dW) else 0.0, 4),
        # the record's first row against the mapped pavement where it starts (its seam piece's start)
        "seam_m": round(abs(float(W[0]) - rec["pieces"][0][3] / 1000.0), 4),
        "trunk_step_m": round(abs(rec["pieces"][0][4] - rec["pieces"][0][3]) / 1000.0, 3),
        "end_m": round(abs(w_n - expect_n), 3),
        "flags": rec.get("flags", []),
    }


def merc(lat, lon):
    R = 6378137.0
    # det-ok: a probe's drawing, never an output
    return R * math.radians(lon), R * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))  # det-ok: drawing


def overlay(rec, out, half, ref=None):
    """The record drawn on the aerial round its physical nose (module doc)."""
    from PIL import Image, ImageDraw
    nose = to_ll(rec, [rec["pt"]["physical"]])[0]
    img_path = out + ".aerial.jpg"
    if not os.path.exists(img_path):
        subprocess.run([sys.executable, os.path.join(HERE, "..", "tools", "ortho_hi.py"), repr(float(nose[0])),
                        repr(float(nose[1])), str(half), img_path, "auto", "1400"], check=True,
                       stdout=subprocess.DEVNULL)
    im = Image.open(img_path).convert("RGB")
    W_, H_ = im.size
    k = 1.0 / math.cos(math.radians(nose[0]))  # det-ok: a probe's measurement / drawing, never an output
    cx, cy = merc(nose[0], nose[1])
    x0, y1 = cx - half * k, cy + half * k
    sc = W_ / (2 * half * k)

    def px(ll):
        x, y = merc(ll[0], ll[1])
        return ((x - x0) * sc, (y1 - y) * sc)
    d = ImageDraw.Draw(im, "RGBA")
    # a 10 m grid (ground metres round the nose)
    for i in range(-int(half) // 10 * 10, int(half) + 1, 10):
        g = i / half * W_ / 2 + W_ / 2
        d.line([(g, 0), (g, H_)], fill=(255, 255, 255, 40), width=1)
        d.line([(0, g), (W_, g)], fill=(255, 255, 255, 40), width=1)
    e = edges(rec)
    s = e["s"]
    sp, sn = rec["s"]["painted"] / 1000.0, rec["s"]["physical"] / 1000.0
    llof = {name: to_ll(rec, e[name] * 1000.0) for name in ("far", "main_drive", "branch_in", "branch_out", "near",
                                                              "centre")}
    lines = lambda name, m, col, w: d.line([px(p) for p in llof[name][m]], fill=col, width=w)  # noqa: E731
    allm = np.ones(len(s), bool)
    upto = s <= sn + 1e-6
    gore = (s >= sp - 1e-6) & upto
    if gore.sum() >= 2:
        poly = [px(p) for p in llof["main_drive"][gore]] + [px(p) for p in llof["branch_in"][gore][::-1]]
        d.polygon(poly, fill=(255, 0, 255, 90))
    lines("centre", allm, (255, 255, 255, 200), 2)
    lines("far", allm, (255, 230, 0, 230), 3)
    lines("near", upto, (255, 230, 0, 230), 3)
    lines("main_drive", allm, (255, 255, 255, 160), 1)
    lines("branch_in", upto & (s >= rec["s"]["taper"] / 1000.0), (0, 230, 255, 230), 2)
    lines("branch_out", upto & (s >= rec["s"]["taper"] / 1000.0), (0, 230, 255, 230), 2)
    if rec.get("branch_line"):
        for p in to_ll(rec, rec["branch_line"]):
            x, y = px(p)
            d.ellipse([x - 2, y - 2, x + 2, y + 2], fill=(0, 230, 255, 255))
    for name, col in (("taper", (0, 255, 0)), ("painted", (255, 150, 0)), ("physical", (255, 0, 0))):
        x, y = px(to_ll(rec, [rec["pt"][name]])[0])
        d.ellipse([x - 7, y - 7, x + 7, y + 7], outline=col + (255,), width=3)
    x, y = px(to_ll(rec, [rec["node_xy"]])[0])
    d.rectangle([x - 6, y - 6, x + 6, y + 6], outline=(40, 120, 255, 255), width=3)
    if ref is not None:
        x, y = px(ref)
        d.line([(x - 9, y - 9), (x + 9, y + 9)], fill=(255, 255, 255, 255), width=3)
        d.line([(x - 9, y + 9), (x + 9, y - 9)], fill=(255, 255, 255, 255), width=3)
    d.text((8, 8), f"{rec['node'][:12]} {rec['kind']} {rec['side']} {rec['evidence']} lanes {rec['lanes']} "
                   f"painted {sp:.0f} m physical {sn:.0f} m (grid 10 m)", fill=(255, 255, 255, 255))
    im.save(out, quality=90)


def overview(lat, lon, half, out, memo, recs):
    """The place's aerial with every mapped road of the fork classes (ramps cyan, others orange, one-way arrows by a dot
    at the end), each fork's node (blue: resolved, grey: not) with its list index, its noses and its gore."""
    from PIL import Image, ImageDraw
    img_path = out + ".aerial.jpg"
    if not os.path.exists(img_path):
        subprocess.run([sys.executable, os.path.join(HERE, "..", "tools", "ortho_hi.py"), repr(lat), repr(lon),
                        str(half), img_path, "auto", "1600"], check=True, stdout=subprocess.DEVNULL)
    im = Image.open(img_path).convert("RGB")
    W_ = im.size[0]
    k = 1.0 / math.cos(math.radians(lat))  # det-ok: a probe's measurement / drawing, never an output
    cx, cy = merc(lat, lon)
    x0, y1 = cx - half * k, cy + half * k
    sc = W_ / (2 * half * k)

    def px(ll):
        x, y = merc(ll[0], ll[1])
        return ((x - x0) * sc, (y1 - y) * sc)
    d = ImageDraw.Draw(im, "RGBA")
    seen = set()
    for cell in sorted(memo):
        for fid, r in sorted(memo[cell].items()):
            if fid in seen:
                continue
            seen.add(fid)
            pts = [px((p[1], p[0])) for p in r["ll"]]
            d.line(pts, fill=(0, 230, 255, 220) if r["link"] else (255, 140, 0, 220), width=2)
            if r["dir"]:
                x, y = pts[-1] if r["dir"] > 0 else pts[0]
                d.ellipse([x - 3, y - 3, x + 3, y + 3], fill=(255, 255, 0, 220))
    for i, rec in enumerate(recs):
        x, y = px(rec["node_ll"])
        ok = "rows" in rec
        d.rectangle([x - 6, y - 6, x + 6, y + 6], outline=(40, 120, 255, 255) if ok else (160, 160, 160, 255), width=3)
        d.text((x + 8, y - 6), str(i), fill=(255, 255, 255, 255))
        if ok:
            e = edges(rec)
            s = e["s"]
            sp, sn = rec["s"]["painted"] / 1000.0, rec["s"]["physical"] / 1000.0
            g = (s >= sp - 1e-6) & (s <= sn + 1e-6)
            if g.sum() >= 2:
                poly = [px(p) for p in to_ll(rec, e["main_drive"][g] * 1000.0)] + \
                       [px(p) for p in to_ll(rec, e["branch_in"][g][::-1] * 1000.0)]
                d.polygon(poly, fill=(255, 0, 255, 120))
            for name, col in (("taper", (0, 255, 0)), ("painted", (255, 150, 0)), ("physical", (255, 0, 0))):
                x, y = px(to_ll(rec, [rec["pt"][name]])[0])
                d.ellipse([x - 5, y - 5, x + 5, y + 5], outline=col + (255,), width=2)
    im.save(out, quality=90)


def collect(lat, lon, R, cache, memo=None):
    import mapcells
    import mapdata
    import netfetch
    import roadshape
    md = mapdata.MapData(cache, fetcher=netfetch.Fetcher(offline=True))
    cell_fn = lambda key: md.cell(key, layers=("roads",), kind="demand").layers.get("roads")   # noqa: E731
    dlat = R / 110574.0
    dlon = R / (111320.0 * math.cos(math.radians(lat)))  # det-ok: a probe's measurement / drawing, never an output
    memo = {} if memo is None else memo
    recs = []
    for key in sorted(mapcells.cells_for_bbox(lat - dlat, lon - dlon, lat + dlat, lon + dlon), key=str):
        for rec in roadshape.cell_forks(key, cell_fn, memo):
            p = to_ll(rec, [rec["node_xy"]])[0]
            rec["node_ll"] = [round(float(p[0]), 7), round(float(p[1]), 7)]
            rec["dist_m"] = round(ground_m((lat, lon), p), 1)
            if rec["dist_m"] <= R:
                recs.append(rec)
    return sorted(recs, key=lambda r: (r["dist_m"], r["node"]))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("lat", type=float)
    ap.add_argument("lon", type=float)
    ap.add_argument("r", type=float, nargs="?", default=300.0)
    ap.add_argument("--cache", required=True, help="a scratch cache (never the player's ~/.cache/beamng-earth)")
    ap.add_argument("--json", help="the records and scores here")
    ap.add_argument("--overlay", help="overlay images into this folder")
    ap.add_argument("--refs", help="JSON {node id: [lat, lon]} of the real physical noses")
    ap.add_argument("--half", type=float, default=90.0, help="m round the nose each overlay shows")
    ap.add_argument("--max", type=int, default=4, help="overlays: this many forks nearest the point")
    a = ap.parse_args()
    cache = _refuse_player_cache(a.cache)
    memo = {}
    recs = collect(a.lat, a.lon, a.r, cache, memo)
    refs = json.load(open(a.refs)) if a.refs else {}
    scores = []
    for rec in recs:
        if "rows" not in rec:
            scores.append({"node": rec["node"], "kind": rec["kind"], "flags": rec.get("flags", []),
                           "dist_m": rec["dist_m"]})
            continue
        sc = score_record(rec)
        sc["dist_m"] = rec["dist_m"]
        sc["node_ll"] = rec["node_ll"]
        nose = to_ll(rec, [rec["pt"]["physical"]])[0]
        sc["physical_ll"] = [round(float(nose[0]), 7), round(float(nose[1]), 7)]
        if rec["node"] in refs:
            sc["nose_err_m"] = round(ground_m(nose, refs[rec["node"]]), 2)
        scores.append(sc)
    ok = [s for s in scores if "max_step_m" in s]
    summ = {"forks": len(scores), "resolved": len(ok),
            "evidence": {k: sum(1 for s in ok if s["evidence"] == k) for k in ("lanes", "turn_lanes", "design")},
            "max_step_m": max((s["max_step_m"] for s in ok), default=0.0),
            "max_slope": max((s["max_slope"] for s in ok), default=0.0),
            "max_seam_m": max((s["seam_m"] for s in ok), default=0.0),
            "max_end_m": max((s["end_m"] for s in ok if s["end_m"] is not None), default=0.0),
            "nose_err_m": [s["nose_err_m"] for s in ok if "nose_err_m" in s]}
    print(json.dumps(summ))
    for s in scores:
        print(json.dumps(s))
    if a.json:
        with open(a.json, "w") as fh:
            json.dump({"place": [a.lat, a.lon, a.r], "summary": summ, "scores": scores, "records": recs}, fh,
                      sort_keys=True, separators=(",", ":"))
    if a.overlay:
        os.makedirs(a.overlay, exist_ok=True)
        try:
            out = os.path.join(a.overlay, "overview.jpg")
            overview(a.lat, a.lon, min(a.r, 250.0), out, memo, recs)
            print("overlay", out)
        except Exception as ex:     # noqa: BLE001
            print("overview failed", type(ex).__name__, ex)
        done = 0
        for rec in recs:
            if "rows" not in rec or done >= a.max:
                continue
            out = os.path.join(a.overlay, f"fork_{rec['node'][:16]}.jpg")
            ref = refs.get(rec["node"])
            try:
                overlay(rec, out, a.half, ref)
                print("overlay", out)
                done += 1
            except Exception as ex:     # noqa: BLE001 (no aerial here: the next)
                print("overlay failed", rec["node"], type(ex).__name__, ex)
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
