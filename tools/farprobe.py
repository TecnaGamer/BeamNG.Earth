#!/usr/bin/env python3
"""farprobe.py LAT LON --out DIR [options] [-- extra earthd args]: a teleport's far field without the game.

Starts the real server (server/earthd.py, the launcher's recommended options, --lod-px 2, full priority loading as the
settings page's teleportPriority) on a fresh cache DIR/cache (never the real one: --cache points at another copy) and a
user folder DIR/user, plays the mod's side (hello as core.lua sends it, graphics, prepare_anchor 'teleport',
commit_anchor, a pose every 0.25 s with a chase camera looking north, slotsGone for the tiles it drops) and records how
fast the far field covers the horizon. A first teleport to --warm (mid-Pacific: little map data) starts the server's
worker processes as a session's earlier places would; the measured visit is the second.

Printed (and written to DIR/result.json, the server's log to DIR/server.log, every far tile sent logged:
BNGE_FARTRACE): seconds from the request to anchor_ready, to the first far tile, to 50/90/99/100 % of the final far
field's ground shown by some tile (any level: points on a polar grid, log-spaced radii, so near and far ground weigh
alike), to far_done; tiles, swaps and the server's CPU seconds (all its processes) over the visit (--window: over
that many seconds from the request). Network-bound: compare builds only with fresh caches, alternating the order per
place (the second visit of a place is faster: the hosts' own caches), on a quiet machine.

    python3 tools/farprobe.py 46.8625 -113.985 --out ~/bnge-tests/farfirst/missoula
    python3 tools/farprobe.py 46.8625 -113.985 --out DIR --server-dir OTHER/server     (another build: A/B)
"""
import argparse
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECOMMENDED = ["--deshadow", "full", "--near-visual", "detail", "--airports", "on", "--sea", "on", "--trees", "on",
               "--buildings", "on", "--gas-stations", "on"]      # (launcher.RECOMMENDED)
COVER_MARKS = (0.5, 0.9, 0.99, 1.0)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("lat", type=float)
    ap.add_argument("lon", type=float)
    ap.add_argument("--out", required=True, help="a new folder for this run (cache, user folder, log, result)")
    ap.add_argument("--server-dir", default=os.path.join(ROOT, "server"))
    ap.add_argument("--cache", default=None, help="cache folder (default OUT/cache: empty, a first visit)")
    ap.add_argument("--timeout", type=float, default=400, help="s to wait for far_done")
    ap.add_argument("--lod-scale", type=float, default=1.3, help="the graphics page's terrain detail (lodScale)")
    ap.add_argument("--warm", default="30.0,-140.0", help="lat,lon of a first teleport ('' for none)")
    ap.add_argument("--pyspy", default=None, help="record the server under py-spy into this file (raw format; "
                    "PYSPY_GIL=1: only threads holding the interpreter lock)")
    ap.add_argument("--window", type=float, default=0.0, help="s from the request the CPU is counted over (posing on "
                    "after far_done: the re-renders that follow; 0: to far_done)")
    ap.add_argument("extra", nargs="*", help="more earthd arguments")
    a = ap.parse_args()
    real = os.path.realpath(os.path.expanduser("~/.cache/beamng-earth"))
    cache = a.cache or os.path.join(a.out, "cache")
    if os.path.realpath(cache).startswith(real):
        raise SystemExit("refusing the real cache: use a copy (its eviction would delete the user's files)")
    user = os.path.join(a.out, "user")
    os.makedirs(cache, exist_ok=True)
    os.makedirs(os.path.join(user, "settings"), exist_ok=True)
    with open(os.path.join(user, "settings", "beamngEarth.json"), "w") as f:
        json.dump({"teleportPriority": True}, f)
    sys.path.insert(0, a.server_dir)
    sys.path.insert(0, os.path.join(a.server_dir, "tests"))
    from netclient import Client, stop_server
    cmd = [sys.executable, os.path.join(a.server_dir, "earthd.py"), "--port", "0", "--user-dir", user,
           "--cache-dir", cache, "--update-check", "off", "--telemetry", "off", "--companion-feed", "off",
           "--lod-px", "2"] + RECOMMENDED + a.extra
    if a.pyspy:
        spy = shutil.which("py-spy") or os.path.expanduser("~/.local/bin/py-spy")
        mode = ["--gil"] if os.environ.get("PYSPY_GIL") else ["--idle", "-s"]
        cmd = [spy, "record", "-r", "50", "--format", "raw", *mode, "-o", a.pyspy, "--"] + cmd
    env = dict(os.environ, BNGE_FARTRACE="1", PYTHONUNBUFFERED="1")
    logf = open(os.path.join(a.out, "server.log"), "w")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=logf, text=True, env=env)
    port, t0 = None, time.monotonic()
    while port is None:
        line = proc.stdout.readline()
        if not line:
            raise SystemExit("the server did not start (see server.log)")
        logf.write(line)
        if "listening on 127.0.0.1:" in line:
            port = int(line.split("127.0.0.1:")[1].split()[0])

    def pump():
        for ln in proc.stdout:
            logf.write(ln)
    threading.Thread(target=pump, daemon=True).start()
    print(f"server up in {time.monotonic() - t0:.1f} s", flush=True)
    c = Client(port)
    try:
        run(a, c, proc.pid)
    finally:
        c.close()
        if a.pyspy:         # (the server itself stopped first: py-spy writes its file when its child exits)
            for k in _children(proc.pid):
                try:
                    with open(f"/proc/{k}/cmdline") as f:
                        if "earthd.py" in f.read():
                            os.kill(k, signal.SIGTERM)
                except OSError:
                    pass
            try:
                proc.wait(60)
            except subprocess.TimeoutExpired:
                pass
        stop_server(proc)
        logf.close()


def run(a, c, pid):
    c.send({"type": "hello", "protocol": 4, "format": 4, "modVersion": "farprobe", "streamLease": 1, "vegFlat": True,
            "geoTiles": 17, "roadGeo": 1, "farGeo": 1, "turbineGeo": 1, "globalFar": 1, "trafficVisuals": 1,
            "readySet": 1, "held": [], "sessions": [], "slotsHeld": []})
    c.until(lambda m: m["type"] == "hello", timeout=30)
    c.send({"type": "graphics", "lodScale": a.lod_scale, "treeScale": a.lod_scale, "buildingScale": a.lod_scale,
            "photoPx": 2048})
    slots = {"gone": [], "of": {}}
    if a.warm:
        wl, wo = (float(v) for v in a.warm.split(","))
        t = time.monotonic()
        visit(c, wl, wo, 1, min(a.timeout, 120), slots)
        print(f"warm-up at {wl},{wo}: {time.monotonic() - t:.1f} s", flush=True)
    cpu0, t0 = tree_cpu(pid), time.monotonic()
    res = visit(c, a.lat, a.lon, 2, a.timeout, slots)
    while time.monotonic() - t0 < a.window:            # (the same pose on: what the far field still does)
        c.send({"type": "pose", "anchorId": res["anchor_id"], "x": 0.0, "y": 0.0, "z": res["ground_z"] + 1.0,
                "vx": 0, "vy": 0, "lat": a.lat, "lon": a.lon, "speed": 0, "follow": True, "held": [], "sessions": []})
        try:
            for _ in range(1000):
                c.recv(0.25)
        except TimeoutError:
            pass
    res["cpu_s"] = round(tree_cpu(pid) - cpu0, 1)       # (the server's processes)
    res["cpu_window_s"] = round(time.monotonic() - t0, 1)
    with open(os.path.join(a.out, "result.json"), "w") as f:
        json.dump(res, f, indent=1)
    print(json.dumps({k: v for k, v in res.items() if k != "cover_curve"}), flush=True)


def visit(c, lat, lon, req, timeout, slots):
    """One teleport: its numbers (see the module doc)."""
    import farfield as ff
    t0 = time.monotonic()
    c.send({"type": "prepare_anchor", "requestId": req, "lat": lat, "lon": lon, "purpose": "teleport"})
    ready, _ = c.until(lambda m: m["type"] in ("anchor_ready", "anchor_error"), timeout=300)
    if ready["type"] != "anchor_ready":
        raise SystemExit(f"anchor error: {ready}")
    t_anchor = time.monotonic() - t0
    aid, gz = ready["anchorId"], float(ready.get("groundZ") or 0.0)
    c.send({"type": "commit_anchor", "anchorId": aid})
    foot, held, shown, events, levels = {}, set(), set(), [], {}
    swaps, t_first, t_done, last_pose = 0, None, None, 0.0
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if time.monotonic() - last_pose >= 0.25:
            last_pose = time.monotonic()
            c.send({"type": "pose", "anchorId": aid, "x": 0.0, "y": 0.0, "z": gz + 1.0, "vx": 0, "vy": 0, "lat": lat,
                    "lon": lon, "speed": 0, "follow": True, "held": [], "sessions": [],
                    "slotsGone": slots["gone"] or None,
                    "cam": {"x": 0.0, "y": -6.0, "z": gz + 3.0, "fx": 0.0, "fy": 0.995, "fz": -0.1, "fovDeg": 65,
                            "h": 1080}})
            slots["gone"] = []
        try:
            m = c.recv(0.05)
        except TimeoutError:
            continue
        t, typ = time.monotonic() - t0, m.get("type")
        if m.get("anchorId") != aid:
            continue
        if typ == "far_tile":
            k = m["key"]
            t_first = t if t_first is None else t_first
            levels[m["level"]] = levels.get(m["level"], 0) + 1
            foot[k] = ("poly", m["poly"]) if m.get("poly") else ("bbox",) + tuple(ff.tile_of(k).bbox)
            held.add(k)
            if m.get("slotSerial") is not None and m.get("material"):
                slots["of"][k] = (int(m["material"].rsplit("_", 1)[1]), m["slotSerial"])
        elif typ in ("far_swap", "far_remove"):
            gone = m.get("remove", []) if typ == "far_swap" else [m["key"]]
            adds = [k for k in m.get("add", []) if k in held]
            swaps += typ == "far_swap"
            for k in gone:
                shown.discard(k)
                held.discard(k)
                if k in slots["of"]:
                    slots["gone"].append(list(slots["of"].pop(k)))
            held.difference_update(adds)
            shown.update(adds)
            events.append((t, adds, gone))
        elif typ == "far_done":
            t_done = t
            break
    marks, curve, rmax = cover(foot, shown, events)
    return {"lat": lat, "lon": lon, "anchor_id": aid, "ground_z": gz, "anchor_s": round(t_anchor, 2),
            "first_tile_s": None if t_first is None else round(t_first, 2),
            "far_done_s": None if t_done is None else round(t_done, 2),
            "cover_s": {str(k): v for k, v in marks.items()}, "swaps": swaps, "tiles_sent": sum(levels.values()),
            "tiles_by_level": dict(sorted(levels.items())), "shown_final": len(shown), "rmax_km": round(rmax / 1000, 1),
            "cover_curve": curve[::max(1, len(curve) // 200)]}


def cover(foot, final, events):
    """({mark: s}, [(s, share)], the far field's reach in m): the share of the final far field's ground shown by some
    tile over time, replayed from the swaps."""
    import numpy as np

    def inside(f, P):
        if f[0] == "bbox":
            return (P[:, 0] >= f[1]) & (P[:, 0] <= f[3]) & (P[:, 1] >= f[2]) & (P[:, 1] <= f[4])
        q = np.asarray(f[1], float).reshape(-1, 2)
        res = np.zeros(len(P), bool)
        for i in range(len(q)):         # (even-odd crossings: convex quads and pieces)
            (x1, y1), (x2, y2) = q[i], q[(i + 1) % len(q)]
            cut = (y1 > P[:, 1]) != (y2 > P[:, 1])
            res ^= cut & (P[:, 0] < (x2 - x1) * (P[:, 1] - y1) / ((y2 - y1) + 1e-12) + x1)
        return res
    rmax = 100.0
    for k in final:
        f = foot[k]
        xs, ys = (f[1::2], f[2::2]) if f[0] == "bbox" else (f[1][0::2], f[1][1::2])
        rmax = max([rmax] + [math.hypot(x, y) for x in xs for y in ys])
    ang = [[2 * math.pi * (i + 0.5 * (n % 2)) / 72 for i in range(72)] for n in range(60)]
    pts = np.array([(r * math.cos(b), r * math.sin(b)) for n, r in enumerate(np.geomspace(50.0, rmax, 60))
                    for b in ang[n]])
    seen = np.zeros(len(pts), bool)
    for k in final:
        seen |= inside(foot[k], pts)
    P = pts[seen]
    counts = np.zeros(len(P), int)
    on, marks, curve = {}, {}, []
    for t, adds, gone in events:
        for k in gone:
            if k in on:
                counts[on.pop(k)] -= 1
        for k in adds:
            idx = np.nonzero(inside(foot[k], P))[0]
            on[k] = idx
            counts[idx] += 1
        share = float((counts > 0).mean()) if len(P) else 0.0
        curve.append((round(t, 2), round(share, 4)))
        for w in COVER_MARKS:
            if w not in marks and share >= w - 1e-9:
                marks[w] = round(t, 2)
    return marks, curve, rmax


def tree_cpu(pid):
    """CPU seconds (user + system) the process pid and every live descendant have used so far."""
    tick = os.sysconf("SC_CLK_TCK")
    total, todo = 0.0, [pid]
    while todo:
        p = todo.pop()
        try:
            with open(f"/proc/{p}/stat") as f:
                fields = f.read().rsplit(")", 1)[1].split()
            total += (int(fields[11]) + int(fields[12])) / tick
        except (OSError, IndexError, ValueError):
            continue
        todo += _children(p)
    return total


def _children(pid):
    try:
        with open(f"/proc/{pid}/task/{pid}/children") as f:
            return [int(k) for k in f.read().split()]
    except OSError:
        return []


if __name__ == "__main__":
    main()
