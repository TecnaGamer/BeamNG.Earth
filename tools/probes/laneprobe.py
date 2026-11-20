"""laneprobe.py LAT LON R --cache DIR [--online] [--json OUT] [--overlay DIR] [--half M] [--max N]
laneprobe.py --accept PLACES.json --cache DIR [--online] [--overlay DIR] [--json OUT]

Road surface v3 phase S1's probe of the lane graph (lanegraph.cell_lanes, earthfast.lane_graph; BNE_ROADSURF_V3=shadow
with BNE_ROADSURF_V3_DEBUG=1 writes the same records beside the build). Reads the map cells round the point from a
scratch cache (never the player's ~/.cache/beamng-earth; --online fetches what it lacks into it) and lists every lane
event whose node lies within R: kind (diverge / merge / add / drop), side, lanes (trunk, through, branch), evidence,
type, and its stations along the main line in the direction of travel, the mapped node at 0: taper start / end,
parallel lane start / end, theoretical gore, physical nose. Every row's boundaries are checked ordered (t_0 >= t_1 ...).

--overlay DIR: per event (the --max nearest) the finest aerial (tools/ortho_hi.py) with its lane boundaries drawn:
outer edges yellow, lane lines white, the event's own lane (branch / added / dropped) cyan, the painted gore magenta,
taper start / end green, theoretical gore orange, physical nose red, the mapped node a blue square, a 10 m grid.

--accept PLACES.json: [{"name", "lat", "lon", "node": id prefix (else the nearest event of "kind" / "side"),
"measured": {"gore": m, "nose": m, "taper0": m, "taper1": m, "par0": m, "par1": m} relative to the mapped node, travel
direction}, ...]: the error table of every measured station (ours - measured), and each place's record computed from
three start cells / orders (the bytes must agree: window independence). A measurement tool; nothing reads it."""
import argparse
import json
import math
import os
import subprocess
import sys

import numpy as np

HERE = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "server"))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from forkprobe import _refuse_player_cache, ground_m, merc, to_ll  # noqa: E402

STATIONS = ("taper0", "taper1", "par0", "par1", "gore", "nose")


def _md(cache, online):
    import mapdata
    import netfetch
    md = mapdata.MapData(cache, fetcher=netfetch.Fetcher(offline=not online))
    return lambda key: md.cell(key, layers=("roads",), kind="demand").layers.get("roads")   # noqa: E731


def cells_round(lat, lon, R):
    import mapcells
    dlat = R / 110574.0
    dlon = R / (111320.0 * math.cos(math.radians(lat)))  # det-ok: a probe's measurement / drawing, never an output
    return sorted(mapcells.cells_for_bbox(lat - dlat, lon - dlon, lat + dlat, lon + dlon), key=str)


def collect(lat, lon, R, cell_fn, memo=None):
    """The events within R of the point, nearest first (each with its cell's frame, node_ll, dist_m)."""
    import lanegraph
    memo = {} if memo is None else memo
    recs = []
    for key in cells_round(lat, lon, R):
        got = lanegraph.cell_lanes(key, cell_fn, memo)
        for ev in got["events"]:
            ev["frame"] = got["frame"]
            ev["cell"] = got["cell"]
            p = to_ll(ev, [ev["node_xy"]])[0]
            ev["node_ll"] = [round(float(p[0]), 7), round(float(p[1]), 7)]
            ev["dist_m"] = round(ground_m((lat, lon), p), 1)
            if ev["dist_m"] <= R:
                recs.append(ev)
    return sorted(recs, key=lambda r: (r["dist_m"], r["node"], r["kind"]))


def ordered(ev):
    """Every row's boundaries in order (left edge first, never crossing)."""
    return all(all(r[5 + k] >= r[6 + k] for k in range(len(r) - 6)) for r in ev.get("rows", ()))


def line(ev):
    s = ev.get("s", {})
    st = " ".join(f"{k}={s[k] / 1000.0:+.0f}" for k in STATIONS if k in s)
    return (f"{ev['node'][:10]} {ev['kind']:7s} {ev['side']:5s} lanes={ev['lanes']} {ev.get('evidence', '-'):10s} "
            f"{ev.get('type', '-'):9s} {st} flags={ev.get('flags', [])} d={ev['dist_m']}")


def boundaries(ev):
    """(s (m), [k] -> (rows, 2) xy m of boundary k)."""
    r = np.asarray(ev["rows"], np.float64)
    s = r[:, 0] / 1000.0
    c = r[:, 1:3] / 1000.0
    n = r[:, 3:5] / 1e6
    return s, [c + n * (r[:, 5 + k:6 + k] / 1000.0) for k in range(r.shape[1] - 5)]


def overlay(ev, out, half=None):
    """The event's boundaries on the aerial round the middle of its rows (module doc)."""
    from PIL import Image, ImageDraw
    s, bs = boundaries(ev)
    st = ev["s"]
    mid_s = (st["lo"] + st["hi"]) / 2000.0
    k = int(np.argmin(np.abs(s - mid_s)))
    mid = to_ll(ev, [(bs[0][k] + bs[-1][k]) * 500.0])[0]
    if half is None:
        half = max(90.0, (st["hi"] - st["lo"]) / 2000.0 + 30.0)
    img_path = out + ".aerial.jpg"
    if not os.path.exists(img_path):
        subprocess.run([sys.executable, os.path.join(HERE, "..", "tools", "ortho_hi.py"), repr(float(mid[0])),
                        repr(float(mid[1])), str(round(half)), img_path, "auto", "1600"], check=True,
                       stdout=subprocess.DEVNULL)
    im = Image.open(img_path).convert("RGB")
    W_, H_ = im.size
    kk = 1.0 / math.cos(math.radians(mid[0]))  # det-ok: a probe's drawing, never an output
    cx, cy = merc(mid[0], mid[1])
    x0, y1 = cx - half * kk, cy + half * kk
    sc = W_ / (2 * half * kk)

    def px(ll):
        x, y = merc(ll[0], ll[1])
        return ((x - x0) * sc, (y1 - y) * sc)
    d = ImageDraw.Draw(im, "RGBA")
    for i in range(-int(half) // 10 * 10, int(half) + 1, 10):
        g = i / half * W_ / 2 + W_ / 2
        d.line([(g, 0), (g, H_)], fill=(255, 255, 255, 30), width=1)
        d.line([(0, g), (W_, g)], fill=(255, 255, 255, 30), width=1)
    names = ev["strips"]
    lls = [to_ll(ev, b * 1000.0) for b in bs]
    r = np.asarray(ev["rows"], np.float64)
    for j, nm in enumerate(names):      # strip j between boundaries j and j + 1
        wj = (r[:, 5 + j] - r[:, 6 + j]) / 1000.0
        on = wj > 0.05
        if nm == "gore" and on.sum() >= 2:
            poly = [px(p) for p in lls[j][on]] + [px(p) for p in lls[j + 1][on][::-1]]
            d.polygon(poly, fill=(255, 0, 255, 110))
        if nm in ("branch", "added", "dropped") and on.sum() >= 2:
            poly = [px(p) for p in lls[j][on]] + [px(p) for p in lls[j + 1][on][::-1]]
            d.polygon(poly, fill=(0, 230, 255, 70))
    for j, ll in enumerate(lls):
        edge = j == 0 or j == len(lls) - 1
        d.line([px(p) for p in ll], fill=(255, 230, 0, 230) if edge else (255, 255, 255, 170), width=3 if edge else 2)
    s_rows = s

    def at(sv, kb):
        i = int(np.argmin(np.abs(s_rows - sv)))
        return px(lls[kb][i])
    nb = len(lls)
    for name, col, kb in (("taper0", (0, 255, 0), nb // 2), ("taper1", (0, 255, 0), nb // 2),
                          ("gore", (255, 150, 0), nb // 2), ("nose", (255, 0, 0), nb // 2)):
        if name in st and st["lo"] <= st[name] <= st["hi"]:
            x, y = at(st[name] / 1000.0, kb)
            d.ellipse([x - 8, y - 8, x + 8, y + 8], outline=col + (255,), width=3)
    x, y = px(to_ll(ev, [ev["node_xy"]])[0])
    d.rectangle([x - 7, y - 7, x + 7, y + 7], outline=(40, 120, 255, 255), width=3)
    d.text((8, 8), line(ev)[:150] + " (grid 10 m)", fill=(255, 255, 255, 255))
    im.save(out, quality=90)


def plain_overlay(lat, lon, half, cell_fn, memo, out):
    """Every road's lane boundaries round the point (its lanes from its placement, constant along it: no event there)
    on the aerial: edges yellow, lane lines white."""
    from PIL import Image, ImageDraw
    import lanegraph
    import roadshape
    img_path = out + ".aerial.jpg"
    if not os.path.exists(img_path):
        subprocess.run([sys.executable, os.path.join(HERE, "..", "tools", "ortho_hi.py"), repr(lat), repr(lon),
                        str(round(half)), img_path, "auto", "1600"], check=True, stdout=subprocess.DEVNULL)
    im = Image.open(img_path).convert("RGB")
    W_ = im.size[0]
    kk = 1.0 / math.cos(math.radians(lat))  # det-ok: a probe's drawing, never an output
    cx, cy = merc(lat, lon)
    x0, y1 = cx - half * kk, cy + half * kk
    sc = W_ / (2 * half * kk)
    d = ImageDraw.Draw(im, "RGBA")
    k_lon = 111320.0 * math.cos(math.radians(lat))  # det-ok: drawing
    seen = set()
    for key in cells_round(lat, lon, half):
        segs = roadshape._segments_round(key, cell_fn, memo, 1, lanegraph._cell_lane_segments)
        for fid, r in sorted(segs.items()):
            if fid in seen or r["cls"] <= 0:
                continue
            seen.add(fid)
            ll = r["ll"]
            xy = np.c_[(ll[:, 0] - lon) * k_lon, (ll[:, 1] - lat) * 110574.0]
            if len(xy) < 2 or np.abs(xy).min(axis=0).max() > 2 * half:
                continue
            t = np.gradient(xy, axis=0)
            t /= np.maximum(np.hypot(t[:, 0], t[:, 1]), 1e-9)[:, None]
            nrm = np.c_[-t[:, 1], t[:, 0]] * (1.0 if r["dir"] >= 0 else -1.0)
            w = float(lanegraph.TAPER_TABLE[r["region"], 0]) if r["cls"] <= 2 else float(r["lane_w"])
            n = r["lanes"] if r["lanes"] >= 1 else max(1, int(round(float(r["drive"]) / max(w, 1.0))))
            left = n * w / 2.0
            kind, _, kv = (r.get("place") or "").partition(":")
            if r["dir"] != 0 and kv.strip().isdigit():
                kv = int(kv)
                left = {"left_of": (kv - 1) * w, "middle_of": (kv - 0.5) * w, "right_of": kv * w}.get(kind, left)
            offs = [left - k * w for k in range(n + 1)]
            for j, o in enumerate(offs):
                q = xy + nrm * o
                pts = [((cx + x * kk - x0) * sc, (y1 - (cy + y * kk)) * sc) for x, y in q]
                edge = j == 0 or j == len(offs) - 1
                d.line(pts, fill=(255, 230, 0, 220) if edge else (255, 255, 255, 150), width=3 if edge else 1)
    d.text((8, 8), f"lane boundaries round {lat:.5f}, {lon:.5f}: no lane event within the place (grid none)",
           fill=(255, 255, 255, 255))
    im.save(out, quality=90)


def pick(evs, place):
    want = place.get("node")
    if want:
        got = [e for e in evs if e["node"].startswith(want) and (not place.get("kind") or e["kind"] == place["kind"])]
        return got[0] if got else None
    got = [e for e in evs if (not place.get("kind") or e["kind"] == place["kind"])
           and (not place.get("side") or e["side"] == place["side"])]
    return got[0] if got else None


def three_ways(key, cell_fn):
    """The cell's lane graph from three start cells / orders, as bytes: (fresh, after its east neighbour, after its
    north-west neighbour with a reversed warm-up)."""
    import lanegraph
    import mapcells
    out = []
    for warm in ((), ((1, 0),), ((-1, -1), (0, 1))):
        memo = {}
        for dx, dy in warm:
            lanegraph.cell_lanes(mapcells.CellKey(key.z, (key.x + dx) % key.n, key.y + dy), cell_fn, memo)
        out.append(json.dumps(lanegraph.cell_lanes(key, cell_fn, memo), sort_keys=True))
    return out


def accept(places, cell_fn, overlay_dir=None):
    import mapcells
    table = []
    for pl in places:
        memo = {}
        evs = collect(pl["lat"], pl["lon"], pl.get("r", 400.0), cell_fn, memo)
        if pl.get("expect_none"):
            real = [e for e in evs if "rows" in e]
            row = {"name": pl["name"], "expect_none": True, "events_near": len(real), "pass": not real,
                   "note": pl.get("note", "")}
            if overlay_dir and pl.get("report"):
                os.makedirs(overlay_dir, exist_ok=True)
                out = os.path.join(overlay_dir, f"{pl['name']}.jpg")
                try:
                    plain_overlay(pl["lat"], pl["lon"], 120.0, cell_fn, memo, out)
                    row["overlay"] = out
                except Exception as ex:     # noqa: BLE001
                    row["overlay"] = f"failed: {type(ex).__name__}: {ex}"
            table.append(row)
            print(json.dumps(row))
            continue
        ev = pick(evs, pl)
        row = {"name": pl["name"], "found": ev is not None, "events_near": len(evs)}
        if ev is None:
            row["note"] = pl.get("note", "")
            table.append(row)
            print(json.dumps(row))
            continue
        row["rows_ok"] = bool(ev.get("rows"))
        row.update({"node": ev["node"][:12], "kind": ev["kind"], "side": ev["side"], "lanes": ev["lanes"],
                    "evidence": ev.get("evidence"), "type": ev.get("type"), "flags": ev.get("flags", []),
                    "ordered": ordered(ev)})
        ours = {k: round(ev["s"][k] / 1000.0, 1) for k in STATIONS if k in ev.get("s", {})}
        row["ours"] = ours
        row["measured"] = pl.get("measured", {})
        row["err"] = {k: round(ours[k] - v, 1) for k, v in pl.get("measured", {}).items() if k in ours}
        key = roadshape._key_of(ev["cell"])     # (the event's own home cell, never one found from rounded points)
        b = three_ways(key, cell_fn)
        row["window_independent"] = b[0] == b[1] == b[2]
        if "lane_start_max" in pl:
            row["lane_start_ok"] = ours.get("taper0", 0.0) <= pl["lane_start_max"]
        if "lane_end_min" in pl:
            row["lane_end_ok"] = ours.get("taper1", 0.0) >= min(pl["lane_end_min"], 900.0)
        if overlay_dir and (pl.get("report") or pl.get("overlay")):
            os.makedirs(overlay_dir, exist_ok=True)
            out = os.path.join(overlay_dir, f"{pl['name']}.jpg")
            try:
                overlay(ev, out)
                row["overlay"] = out
            except Exception as ex:     # noqa: BLE001 (no aerial: the table still counts)
                row["overlay"] = f"failed: {type(ex).__name__}: {ex}"
        table.append(row)
        print(json.dumps(row))
    return table


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("lat", type=float, nargs="?")
    ap.add_argument("lon", type=float, nargs="?")
    ap.add_argument("r", type=float, nargs="?", default=300.0)
    ap.add_argument("--cache", required=True, help="a scratch cache (never the player's ~/.cache/beamng-earth)")
    ap.add_argument("--online", action="store_true", help="fetch the cells the cache lacks")
    ap.add_argument("--json", help="the records (or the acceptance table) here")
    ap.add_argument("--overlay", help="overlay images into this folder")
    ap.add_argument("--half", type=float, default=None, help="m round each event's middle the overlay shows")
    ap.add_argument("--max", type=int, default=4, help="overlays: this many events nearest the point")
    ap.add_argument("--accept", help="the measured places (module doc)")
    a = ap.parse_args()
    cell_fn = _md(_refuse_player_cache(a.cache), a.online)
    if a.accept:
        table = accept(json.load(open(a.accept)), cell_fn, a.overlay)
        if a.json:
            with open(a.json, "w") as fh:
                json.dump(table, fh, indent=1, sort_keys=True)
        # (strict: a place whose event is missing, has no rows, is out of order, differs between start cells, or an
        # expected-none place with an event, fails the run; the station errors are reported, not judged)
        bad = [r["name"] for r in table if (r.get("expect_none") and not r.get("pass"))
               or (not r.get("expect_none") and (not r.get("found") or not r.get("rows_ok", True)
                                                 or r.get("ordered") is False or r.get("window_independent") is False))]
        missing = [(r["name"], k) for r in table for k in r.get("measured", {}) if k not in r.get("ours", {})]
        print(json.dumps({"failed": bad, "stations_missing": missing}))
        sys.stdout.flush()
        os._exit(1 if bad else 0)
    else:
        recs = collect(a.lat, a.lon, a.r, cell_fn)
        for i, ev in enumerate(recs):
            print(i, line(ev), "" if ordered(ev) else "UNORDERED")
        if a.json:
            with open(a.json, "w") as fh:
                json.dump(recs, fh, sort_keys=True, separators=(",", ":"))
        if a.overlay:
            os.makedirs(a.overlay, exist_ok=True)
            for ev in recs[:a.max]:
                out = os.path.join(a.overlay, f"{ev['kind']}_{ev['node'][:12]}.jpg")
                try:
                    overlay(ev, out, a.half)
                    print("overlay", out)
                except Exception as ex:     # noqa: BLE001
                    print("overlay failed", ev["node"], type(ex).__name__, ex)
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
