#!/usr/bin/env python3
"""lapprobe.py --out DIR --cache CACHE [--laps 4] [--mph 120] [-- extra earthd args]: laps of an oval without the
game (as tools/staleprobe.py: the real server and its worker processes, the mod's terrain-follow played with its own
rules, tests/modsim.py), counted per lap for the ground the player sees:

  commits         blocks committed (the mod's terrain-follow)
  brown           commits of a block without a photo (the level's flat brown base until one comes), and the seconds
                  the block in use had none
  standin         commits with the stand-in photo (_base_q), and the seconds the block in use showed one
  builds          terrain blocks built (server.log "terrain (...)"), base photos rendered (stand-ins in the block
                  process, refreshes "rendered again") and taken from the revisit cache
  mem_peak_mb     the server's peak memory (PSS of its processes, sampled every 2 s)

The oval: a stadium (two straights of --straight m, two half circles of --radius m; default 4.0 km a lap, Daytona's
tri-oval is 4.0 km) round LAT, LON (default Daytona International Speedway), its long axis at --axis degrees from
north. CACHE is used as it is (a copy for each run: a run changes it); never the real cache.

    cp -a --reflink=always warm c1 && python3 tools/lapprobe.py --out r1 --cache c1 --laps 4 -- --offline
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
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECOMMENDED = ["--deshadow", "full", "--near-visual", "detail", "--airports", "on", "--sea", "on", "--trees", "on",
               "--buildings", "on", "--gas-stations", "on"]      # (launcher.RECOMMENDED)
# the Experimental switches of the session this tool was made for (Daytona, 2026-11-19: Generated ground + Revamped
# roads on); --switches "" for none
SWITCHES = ("BNE_BATTER=vanilla,BNE_DECK_K_SPEED=on,BNE_FAST_DECK_ENDS=on,BNE_FAST_DECK_GROUND=on,BNE_FAST_GAP=on,"
            "BNE_FOOT_CLEAR=on,BNE_GEN_GROUND=on,BNE_RACEWAY_DIG=on,BNE_RACEWAY_PROPS=on,BNE_RAIL_CURVES=on,"
            "BNE_RAIL_LIFECYCLE=on,BNE_RAIL_ROAD_FIT=on,BNE_RAIL_SEAM_NATURAL=on,BNE_ROADSHAPE=sheet,BNE_SIDEWALK_V2=on,"
            "BNE_SPEED_TAPERS=on,BNE_SURFACE_V2=on,BNE_TUNNEL_ROAD_OVER=on")
DT = 1.0 / 60.0
LOG_TS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}) ")
COUNTS = {"builds": re.compile(r"INFO earthd: terrain \(\d+, "),
          "standin_renders": re.compile(r"quick base photo .* in the block process in|INFO earthd: quick base photo \("),
          "refreshes": re.compile(r"rendered again with its NAIP"),
          "revisit_hits": re.compile(r"base photo \(.*\) from the revisit cache"),
          "full_renders": re.compile(r"INFO earthd: base photo \(\d+, [-0-9.]+, [-0-9.]+\) r\d+ in ")}


class Stadium:
    def __init__(self, straight, radius, axis_deg):
        self.l, self.r = straight, radius
        self.len = 2 * straight + 2 * math.pi * radius
        a = math.radians(axis_deg)
        self.ux, self.uy = math.sin(a), math.cos(a)        # (the long axis: east, north)
        self.nx, self.ny = self.uy, -self.ux                 # (to its right)

    def at(self, s):
        """((u, v) on the oval s m from its start (the first straight's start), (du, dv) the unit heading), axis frame."""
        s %= self.len
        l, r = self.l, self.r
        if s < l:                                    # (the first straight, along +u at v = -r)
            return (-l / 2 + s, -r), (1.0, 0.0)
        s -= l
        if s < math.pi * r:                          # (the first turn, round (l/2, 0))
            t = -math.pi / 2 + s / r
            return (l / 2 + r * math.cos(t), r * math.sin(t)), (-math.sin(t), math.cos(t))
        s -= math.pi * r
        if s < l:
            return (l / 2 - s, r), (-1.0, 0.0)
        s -= l
        t = math.pi / 2 + s / r
        return (-l / 2 + r * math.cos(t), r * math.sin(t)), (-math.sin(t), math.cos(t))

    def enu(self, s):
        """(east, north), (unit east, unit north) of the point s m round, relative to the oval's centre."""
        (u, v), (du, dv) = self.at(s)
        # (v positive to the left of the long axis)
        e, n = u * self.ux - v * self.nx, u * self.uy - v * self.ny
        de, dn = du * self.ux - dv * self.nx, du * self.uy - dv * self.ny
        return (e, n), (de, dn)


def pss_mb(pid):
    try:
        with open(f"/proc/{pid}/smaps_rollup") as f:
            got = 0.0
            for ln in f:
                if ln.startswith(("Pss:", "SwapPss:")):
                    got += float(ln.split()[1]) / 1024
            return got
    except OSError:
        return 0.0


def tree(pid):
    out, todo = [], [pid]
    while todo:
        p = todo.pop()
        out.append(p)
        try:                        # (every thread's children: a pool's processes are started by its own thread)
            for t in os.listdir(f"/proc/{p}/task"):
                with open(f"/proc/{p}/task/{t}/children") as f:
                    todo += [int(k) for k in f.read().split()]
        except (OSError, ValueError):
            pass
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--lat", type=float, default=29.1852)
    ap.add_argument("--lon", type=float, default=-81.0705)
    ap.add_argument("--laps", type=int, default=4)
    ap.add_argument("--mph", type=float, default=120.0)
    ap.add_argument("--straight", type=float, default=1100.0)
    ap.add_argument("--radius", type=float, default=290.0)
    ap.add_argument("--axis", type=float, default=30.0, help="the long axis, degrees from north")
    ap.add_argument("--settle", type=float, default=45.0, help="s standing at the start before the laps (far_done)")
    ap.add_argument("--photo-gate", action="store_true", help="the mod's photo gate (core.photoGateOpen): a ready block "
                    "without its photo waits, until the hard commit distance, while the block in use shows one")
    ap.add_argument("--switches", default=SWITCHES)
    ap.add_argument("--server-dir", default=os.path.join(ROOT, "server"))
    ap.add_argument("--user", default=None, help="the game's user folder (default OUT/user)")
    ap.add_argument("extra", nargs="*")
    a = ap.parse_args()
    import signal
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))
    real = os.path.realpath(os.path.expanduser("~/.cache/beamng-earth"))
    if os.path.realpath(a.cache).startswith(real):
        raise SystemExit("refusing the real cache: use a copy")
    os.makedirs(a.out, exist_ok=True)
    user = a.user or os.path.join(a.out, "user")
    os.makedirs(os.path.join(user, "settings"), exist_ok=True)
    with open(os.path.join(user, "settings", "beamngEarth.json"), "w") as f:
        json.dump({"teleportPriority": True}, f)
    sys.path.insert(0, a.server_dir)
    sys.path.insert(0, os.path.join(a.server_dir, "tests"))
    from netclient import Client, stop_server
    import modsim
    import geodesy
    cmd = [sys.executable, os.path.join(a.server_dir, "earthd.py"), "--port", "0", "--user-dir", user,
           "--cache-dir", a.cache, "--update-check", "off", "--telemetry", "off", "--companion-feed", "off",
           "--lod-px", "2"] + RECOMMENDED + a.extra
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    for kv in filter(None, a.switches.split(",")):
        k, v = kv.split("=", 1)
        env[k] = v
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
    mem = {"peak": 0.0, "now": 0.0, "stop": False, "lap_peak": {}}
    lap_now = [0]

    def watch():
        while not mem["stop"]:
            tot = sum(pss_mb(p) for p in tree(proc.pid))
            mem["now"], mem["peak"] = tot, max(mem["peak"], tot)
            mem["lap_peak"][lap_now[0]] = max(mem["lap_peak"].get(lap_now[0], 0.0), tot)
            time.sleep(2.0)
    threading.Thread(target=watch, daemon=True).start()
    oval = Stadium(a.straight, a.radius, a.axis)
    centre = geodesy.EnuFrame(a.lat, a.lon)
    c = Client(port)
    res = {}
    try:
        c.send({"type": "hello", "protocol": int(modsim.CORE["PROTOCOL"]), "format": int(modsim.CORE["FORMAT"]),
                "modVersion": "lapprobe", "streamLease": 1, "vegFlat": True, "geoTiles": 17, "roadGeo": 1,
                "farGeo": 1, "turbineGeo": 1, "globalFar": 1, "trafficVisuals": 1, "swapGen": 1, "held": [],
                "sessions": [], "slotsHeld": []})
        c.until(lambda m: m["type"] == "hello", timeout=60)
        c.send({"type": "graphics", "lodScale": 1.3, "treeScale": 1.3, "buildingScale": 1.3, "photoPx": 2048})
        res = drive(c, a, oval, centre, geodesy, modsim, mem, lap_now)
    finally:
        c.close()
        mem["stop"] = True
        stop_server(proc)
        logf.close()
    with open(log_path, errors="replace") as f:
        log = f.read().splitlines()
    for lap in res["laps"]:
        t0, t1 = lap.pop("_t0"), lap.pop("_t1")
        got = {k: 0 for k in COUNTS}
        for ln in log:
            m = LOG_TS.match(ln)
            if not m:
                continue
            t = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S,%f").timestamp()
            if t0 <= t < t1:
                for k, rx in COUNTS.items():
                    if rx.search(ln):
                        got[k] += 1
        lap.update(got)
        lap["mem_peak_mb"] = round(mem["lap_peak"].get(lap["lap"], 0.0))
    res["mem_peak_mb"] = round(mem["peak"])
    res["memwatch"] = [ln[24:] for ln in log if "earthd.memwatch" in ln][-6:]
    with open(os.path.join(a.out, "result.json"), "w") as f:
        json.dump(res, f, indent=1)
    for lap in res["laps"]:
        print(json.dumps(lap), flush=True)
    print(json.dumps({k: v for k, v in res.items() if k != "laps"}), flush=True)


def drive(c, a, oval, centre, geodesy, modsim, mem, lap_now):
    speed = a.mph * 0.44704
    (e0, n0), _ = oval.enu(0.0)
    lat0, lon0, _ = centre.enu_to_geodetic(e0, n0, 0.0)
    c.send({"type": "prepare_anchor", "requestId": 1, "lat": float(lat0), "lon": float(lon0), "purpose": "teleport"})
    ready, _ = c.until(lambda m: m["type"] in ("anchor_ready", "anchor_error"), timeout=600)
    if ready["type"] != "anchor_ready":
        raise SystemExit(f"anchor error: {ready}")
    aid = ready["anchorId"]
    frame = geodesy.EnuFrame(ready["lat"], ready["lon"])
    gz = float(ready.get("groundZ") or 0.0)

    def local(s):
        (e, n), (de, dn) = oval.enu(s)
        la, lo, _ = centre.enu_to_geodetic(e, n, 0.0)
        x, y, _ = frame.geodetic_to_enu(la, lo, 0.0)
        la2, lo2, _ = centre.enu_to_geodetic(e + de, n + dn, 0.0)
        x2, y2, _ = frame.geodetic_to_enu(la2, lo2, 0.0)
        dx, dy = float(x2 - x), float(y2 - y)
        k = math.hypot(dx, dy) or 1.0
        return float(x), float(y), float(la), float(lo), dx / k, dy / k

    c.send({"type": "commit_anchor", "anchorId": aid})
    tr = ready["terrain"]
    c.send({"type": "commit_terrain", "anchorId": aid, "cx": tr["cx"], "cy": tr["cy"]})
    st = {"block": (tr["cx"], tr["cy"]), "base": tr.get("base"), "tp": None, "next": 2, "t_lat": None}
    size, T = 1024, modsim.follow_table(1024)
    laps = []
    lap = None
    swaps = {}

    def kind(base):
        if not base:
            return "brown"
        return "standin" if "_base_q" in base else "photo"

    def handle(m, clock):
        t = m.get("type")
        if m.get("anchorId") not in (None, aid):
            return
        if t == "terrain_ready":
            p = st["tp"]
            if p and m.get("requestId") == p["requestId"]:
                p["ready"], p["msg"] = True, m
                st["t_lat"] = clock - p["sent"]
        elif t == "terrain_error":
            p = st["tp"]
            if p and m.get("requestId") == p["requestId"]:
                st["tp"] = None
        elif t == "terrain_refresh" and m.get("swap") is None:
            p = st["tp"]
            if (m["cx"], m["cy"]) == st["block"]:
                st["base"] = m.get("base")
                if lap is not None:
                    lap["refresh_" + kind(st["base"])] += 1
            elif p and p.get("ready") and (m["cx"], m["cy"]) == (p["cx"], p["cy"]):
                p["msg"] = m
        elif t == "block_base":
            p = st["tp"]
            if (m["cx"], m["cy"]) == st["block"]:
                st["base"] = m["base"]
            if p and p.get("msg") and (m["cx"], m["cy"]) == (p["cx"], p["cy"]):
                p["msg"]["base"] = m["base"]
        elif t == "swap_begin":
            swaps[m["gen"]] = True
        elif t == "swap_end":
            c.send({"type": "swap_done", "gen": m["gen"], "ok": True})

    # standing at the start until far_done (or --settle s), as a player looks round after a teleport
    t_end = time.monotonic() + a.settle
    x, y, la, lo, hx, hy = local(0.0)
    last_pose = 0.0
    while time.monotonic() < t_end:
        if time.monotonic() - last_pose >= 0.5:
            last_pose = time.monotonic()
            c.send({"type": "pose", "anchorId": aid, "x": x, "y": y, "z": gz + 1.0, "vx": 0.0, "vy": 0.0, "lat": la,
                    "lon": lo, "speed": 0.0, "follow": True, "held": [], "sessions": [],
                    "cam": {"x": x - 6 * hx, "y": y - 6 * hy, "z": gz + 3.0, "fx": hx, "fy": hy, "fz": -0.1,
                            "fovDeg": 65, "h": 1080}})
        try:
            m = c.recv(0.05)
        except TimeoutError:
            continue
        handle(m, 0.0)
        if m.get("type") == "far_done" and m.get("anchorId") == aid:
            t_end = min(t_end, time.monotonic() + 3.0)
    clock = 0.0
    wall0 = time.monotonic()
    s = 0.0
    frames = 0
    lag_max = 0.0
    while s < a.laps * oval.len:
        n = int(s // oval.len) + 1
        if lap is None or lap["lap"] != n:
            if lap is not None:
                lap["_t1"] = time.time()
                laps.append(lap)
            lap = {"lap": n, "_t0": time.time(), "commits": 0, "commit_brown": 0, "commit_standin": 0,
                   "commit_photo": 0, "refresh_brown": 0, "refresh_standin": 0, "refresh_photo": 0, "brown_s": 0.0,
                   "standin_s": 0.0, "prepares": 0, "edge_min_m": math.inf}
            lap_now[0] = n
        frames += 1
        clock += DT
        while True:
            try:
                handle(c.recv(max(0.0005, wall0 + clock - time.monotonic())), clock)
            except TimeoutError:
                break
            if time.monotonic() >= wall0 + clock:
                break
        lag_max = max(lag_max, time.monotonic() - (wall0 + clock))
        s += speed * DT
        x, y, la, lo, hx, hy = local(s)
        vx, vy = hx * speed, hy * speed
        k = kind(st["base"])
        if k == "brown":
            lap["brown_s"] += DT
        elif k == "standin":
            lap["standin_s"] += DT
        b, tp = st["block"], st["tp"]
        gate = None
        if a.photo_gate:
            def gate(_cx, _cy):
                msg = (st["tp"] or {}).get("msg")
                return not st["base"] or not isinstance(msg, dict) or isinstance(msg.get("base"), str)
        fa = modsim.follow_decide(x, y, vx, vy, b[0], b[1], tp, clock, 0.0, gate, st["t_lat"], size)
        if fa and fa[0] == "prepare":
            st["tp"] = {"requestId": st["next"], "cx": fa[1], "cy": fa[2], "sent": clock}
            st["next"] += 1
            lap["prepares"] += 1
            c.send({"type": "prepare_terrain", "requestId": st["tp"]["requestId"], "anchorId": aid,
                    "cx": fa[1], "cy": fa[2]})
        elif fa and fa[0] == "commit":
            st["block"], st["base"], st["tp"] = (tp["cx"], tp["cy"]), tp["msg"].get("base"), None
            lap["commits"] += 1
            lap["commit_" + kind(st["base"])] += 1
            c.send({"type": "commit_terrain", "anchorId": aid, "cx": tp["cx"], "cy": tp["cy"]})
        elif fa and fa[0] == "discard":
            st["tp"] = None
        if frames % 30 == 0:
            c.send({"type": "pose", "anchorId": aid, "x": x, "y": y, "z": gz + 1.0, "vx": vx, "vy": vy, "lat": la,
                    "lon": lo, "speed": speed, "follow": True, "held": [], "sessions": [],
                    "cam": {"x": x - 6 * hx, "y": y - 6 * hy, "z": gz + 3.0, "fx": hx, "fy": hy, "fz": -0.1,
                            "fovDeg": 65, "h": 1080}})
        gx0, gy0, gx1, gy1 = modsim.ground_box(*st["block"], size)
        lap["edge_min_m"] = min(lap["edge_min_m"], x - gx0, gx1 - x, y - gy0, gy1 - y)
    lap["_t1"] = time.time()
    laps.append(lap)
    for lp in laps:
        lp["brown_s"], lp["standin_s"], lp["edge_min_m"] = (round(lp["brown_s"], 1), round(lp["standin_s"], 1),
                                                             round(lp["edge_min_m"]))
    return {"laps": laps, "lap_m": round(oval.len), "mph": a.mph, "lag_max_s": round(lag_max, 2),
            "anchor": [ready["lat"], ready["lon"]]}


if __name__ == "__main__":
    main()
