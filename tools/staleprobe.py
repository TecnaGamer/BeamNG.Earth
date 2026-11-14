#!/usr/bin/env python3
"""staleprobe.py LAT LON --out DIR --cache CACHE [--bump TAG] [--stale on|off] [-- extra earthd args]: one teleport
to a place without the game (as tools/farprobe.py: the real server and its worker processes, the mod's side played),
timed for the "older version shown while the current one is built" work (revisit.stale):

  anchor_s        the request to anchor_ready (the provisional first block)
  graded_s        ... to the graded first block (the first terrain_refresh): the drivable ground
  far_done_s      ... to far_done
  kinds           per detail kind (content: a road geotile's roads, furniture, lamps, parking, buildings; skin: its draped
                  road surface and sidewalks; trees; signs): the tiles sent, the first / the last one's time, the radius
                  round the car within which the kind was complete 0 / 2 / 5 / 10 / 20 s after the graded block (m; the
                  nearest tile still missing then, among those that came in the end), and the time after the graded
                  block the kind was complete under the car (0), within 150 m and 300 m
  swap            what was sent AGAIN outside the barrier (with --settle: the probe poses on until the server says
                  the swap is done or the seconds are over): blocks, road contents, skins, tree tiles, sign tiles
  barrier         the generation swap's commit barrier, as the mod sees it (the probe answers swap_done at once): when
                  it began, the blocks and skins staged under its token, when it was committed

--bump TAG sets BNGE_SIM_BUMP (another code key and other format markers: an update's keys without one); --stale off
sets BNE_STALE_REVISIT=off (the behaviour before). CACHE is used as it is (make a copy for each run: a run changes
it); never the real cache. A first teleport to --warm starts the worker processes as a session's earlier places.

    cp -a --reflink=always warm_cache c1 && python3 tools/staleprobe.py 39.2833 -76.5967 --out r1 --cache c1 \\
        --bump b --stale off -- --offline
"""
import argparse
import json
import math
import os
import re
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECOMMENDED = ["--deshadow", "full", "--near-visual", "detail", "--airports", "on", "--sea", "on", "--trees", "on",
               "--buildings", "on", "--gas-stations", "on"]      # (launcher.RECOMMENDED)
AFTER_S = (0, 2, 5, 10, 20)
RINGS_M = (0.0, 150.0, 300.0)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("lat", type=float)
    ap.add_argument("lon", type=float)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--server-dir", default=os.path.join(ROOT, "server"))
    ap.add_argument("--bump", default="")
    ap.add_argument("--stale", default="on", choices=("on", "off"), help="BNE_STALE_REVISIT (the server's default: off)")
    ap.add_argument("--timeout", type=float, default=400)
    ap.add_argument("--settle", type=float, default=0.0, help="s to pose on after far_done (the swap)")
    ap.add_argument("--warm", default="30.0,-140.0")
    ap.add_argument("--user", default=None, help="the game's user folder (default OUT/user). The same one for every "
                    "run of a series: its path is in the terrain blocks' keys (BNGE_USER_DIR), as a player's is")
    ap.add_argument("extra", nargs="*")
    a = ap.parse_args()
    import signal
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))      # (killed: the server stopped with it, finally below)
    real = os.path.realpath(os.path.expanduser("~/.cache/beamng-earth"))
    if os.path.realpath(a.cache).startswith(real):
        raise SystemExit("refusing the real cache: use a copy")
    user = a.user or os.path.join(a.out, "user")
    os.makedirs(os.path.join(user, "settings"), exist_ok=True)
    with open(os.path.join(user, "settings", "beamngEarth.json"), "w") as f:
        json.dump({"teleportPriority": True}, f)
    sys.path.insert(0, a.server_dir)
    sys.path.insert(0, os.path.join(a.server_dir, "tests"))
    from netclient import Client, stop_server
    cmd = [sys.executable, os.path.join(a.server_dir, "earthd.py"), "--port", "0", "--user-dir", user,
           "--cache-dir", a.cache, "--update-check", "off", "--telemetry", "off", "--companion-feed", "off",
           "--lod-px", "2"] + RECOMMENDED + a.extra
    env = dict(os.environ, PYTHONUNBUFFERED="1", BNE_STALE_REVISIT=a.stale)
    env.pop("BNGE_SIM_BUMP", None)
    if a.bump:
        env["BNGE_SIM_BUMP"] = a.bump
    log_path = os.path.join(a.out, "server.log")
    logf = open(log_path, "w")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=logf, text=True, env=env)
    port = None
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
            logf.flush()
    threading.Thread(target=pump, daemon=True).start()
    c = Client(port)
    try:
        c.send({"type": "hello", "protocol": 4, "format": 4, "modVersion": "staleprobe", "streamLease": 1,
                "vegFlat": True, "geoTiles": 17, "roadGeo": 1, "farGeo": 1, "turbineGeo": 1, "globalFar": 1,
                "trafficVisuals": 1, "readySet": 1, "swapGen": 1, "held": [], "sessions": [], "slotsHeld": []})
        c.until(lambda m: m["type"] == "hello", timeout=60)
        c.send({"type": "graphics", "lodScale": 1.3, "treeScale": 1.3, "buildingScale": 1.3, "photoPx": 2048})
        if a.warm:
            wl, wo = (float(v) for v in a.warm.split(","))
            visit(c, wl, wo, 1, 120, 0.0, None, a.server_dir)
        res = visit(c, a.lat, a.lon, 2, a.timeout, a.settle, log_path, a.server_dir)
    finally:
        c.close()
        stop_server(proc)
        logf.close()
    res["bump"], res["stale"] = a.bump, a.stale
    with open(log_path, errors="replace") as f:
        log = f.read()
    res["log"] = [ln[-260:] for ln in log.splitlines()
                  if re.search(r"older version|current version|swap to|graded first block", ln)][:40]
    with open(os.path.join(a.out, "result.json"), "w") as f:
        json.dump(res, f, indent=1)
    print(json.dumps({k: v for k, v in res.items() if k != "log"}), flush=True)
    for ln in res["log"]:
        print("  log:", ln, flush=True)


def _dist(lat0, lon0, la, lo):
    dy = (la - lat0) * 111320.0
    dx = (lo - lon0) * 111320.0 * math.cos(math.radians(lat0))
    return math.hypot(dx, dy)


def visit(c, lat, lon, req, timeout, settle, log_path, server_dir):
    sys.path.insert(0, server_dir)
    import roads as roadsmod
    import geodesy
    import geotiles as geotilesmod
    frame = geodesy.EnuFrame(lat, lon)
    t0 = time.monotonic()
    c.send({"type": "prepare_anchor", "requestId": req, "lat": lat, "lon": lon, "purpose": "teleport"})
    ready, _ = c.until(lambda m: m["type"] in ("anchor_ready", "anchor_error"), timeout=300)
    if ready["type"] != "anchor_ready":
        raise SystemExit(f"anchor error: {ready}")
    t_anchor = time.monotonic() - t0
    aid, gz = ready["anchorId"], float(ready.get("groundZ") or 0.0)
    c.send({"type": "commit_anchor", "anchorId": aid})
    first = {"content": {}, "skin": {}, "trees": {}, "signs": {}}       # kind -> key -> (s, m from the car)
    again = {"content": [], "skin": [], "trees": [], "signs": [], "block": [], "barrier": []}
    barrier = None
    t_graded, t_done, last_pose, t_end = None, None, 0.0, None
    slots_gone, slot_of, held = [], {}, set()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        now = time.monotonic()
        if t_done is not None:
            if settle <= 0 or now - t0 > t_done + settle:
                break
            if log_path and _swapped(log_path):
                t_end = t_end or now + 6.0           # (the tiles sent again after the blocks)
            if t_end is not None and now > t_end:
                break
        if now - last_pose >= 0.25:
            last_pose = now
            c.send({"type": "pose", "anchorId": aid, "x": 0.0, "y": 0.0, "z": gz + 1.0, "vx": 0, "vy": 0, "lat": lat,
                    "lon": lon, "speed": 0, "follow": True, "held": [], "sessions": [],
                    "slotsGone": slots_gone or None,
                    "cam": {"x": 0.0, "y": -6.0, "z": gz + 3.0, "fx": 0.0, "fy": 0.995, "fz": -0.1, "fovDeg": 65,
                            "h": 1080}})
            slots_gone = []
        try:
            m = c.recv(0.05)
        except TimeoutError:
            continue
        t, typ = time.monotonic() - t0, m.get("type")
        if m.get("anchorId") != aid:
            continue
        if typ == "swap_begin":             # (the mod's commit barrier: what follows with the token is staged)
            barrier = {"gen": m["gen"], "t0": round(t, 1), "blocks": 0, "skins": 0}
            again["barrier"].append(barrier)
            continue
        if typ == "swap_end":               # (all staged: committed in one frame, said so)
            barrier["t_commit"] = round(t, 1)
            c.send({"type": "swap_done", "gen": m["gen"], "ok": True})
            continue
        if typ == "swap_abort":
            barrier["aborted"] = round(t, 1)
            continue
        if m.get("swap") is not None:
            barrier["blocks" if typ == "terrain_refresh" else "skins"] += 1
            continue
        if typ == "terrain_refresh":
            if t_graded is None:
                t_graded = t
            else:
                again["block"].append(round(t, 1))
        elif typ in ("roads_tile", "roads_skin", "veg_tile"):
            key = str(m.get("key"))
            if typ == "roads_tile" and key.startswith("s"):
                kind = "signs"
                tx, ty = (int(v) for v in key[1:].split("_"))
                x0, y0, x1, y1 = roadsmod.tile_bbox(tx, ty)
                d = math.hypot(max(x0, min(0.0, x1)), max(y0, min(0.0, y1)))
            else:
                kind = {"roads_tile": "content", "roads_skin": "skin", "veg_tile": "trees"}[typ]
                try:                    # (m from the car to the geotile's square)
                    z, gx, gy = geotilesmod.parse(key)
                    d = float(geotilesmod.quad_dist(geotilesmod.quad(frame, gx, gy, z), 0.0, 0.0))
                except Exception:
                    d = -1.0
            if key in first[kind]:
                again[kind].append(round(t, 1))
            else:
                first[kind][key] = (t, d)
        elif typ == "far_tile":
            held.add(m["key"])
            if m.get("slotSerial") is not None and m.get("material"):
                slot_of[m["key"]] = (int(m["material"].rsplit("_", 1)[1]), m["slotSerial"])
        elif typ in ("far_swap", "far_remove"):
            for k in (m.get("remove", []) if typ == "far_swap" else [m["key"]]):
                if k in slot_of:
                    slots_gone.append(list(slot_of.pop(k)))
        elif typ == "far_done" and t_done is None:
            t_done = t
    tg = t_graded if t_graded is not None else t_anchor
    kinds = {}
    for kind, got in first.items():
        ts = sorted(v[0] for v in got.values())
        row = {"n": len(ts), "first_s": round(ts[0], 1) if ts else None, "last_s": round(ts[-1], 1) if ts else None}
        # the radius round the car the kind is complete in, some seconds after the graded block
        row["complete_m_after"] = {}
        for dt in AFTER_S:
            missing = [v[1] for v in got.values() if v[0] > tg + dt and v[1] >= 0]
            row["complete_m_after"][str(dt)] = round(min(missing)) if missing else "all"
        row["complete_s_after_block"] = {}
        for r in RINGS_M:
            inside = [v[0] for v in got.values() if 0 <= v[1] <= r]
            row["complete_s_after_block"][str(int(r))] = round(max(inside) - tg, 1) if inside else None
        kinds[kind] = row
    return {"lat": lat, "lon": lon, "anchor_s": round(t_anchor, 2),
            "graded_s": None if t_graded is None else round(t_graded, 2),
            "far_done_s": None if t_done is None else round(t_done, 2), "kinds": kinds,
            "swap": {k: {"n": len(v), "first_s": v[0] if v else None, "last_s": v[-1] if v else None}
                     for k, v in again.items() if k != "barrier"},
            "barrier": again["barrier"]}


def _swapped(log_path):
    try:
        with open(log_path, errors="replace") as f:
            txt = f.read()
            return "the current version swapped in" in txt or "the swap to the current version was given up" in txt
    except OSError:
        return False


if __name__ == "__main__":
    main()
