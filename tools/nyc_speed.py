#!/usr/bin/env python3
"""High-speed drives through dense Manhattan, for world-generation speed (the user, 2026-11-02: "create a tool where
you can simulate high speed driving down dense new york streets ... even cached places are too slow to generate").

The vehicle (a ball prop by default: --vehicle) is moved by tools/drive.py's in-game driver (a scripted profile: the
speed held whatever the traffic, the ground followed, no AI) along a route of avenues and cross streets, while this
tool records what keeping up means:

  - the edge guard: frames in "warn" / "hold" (the mod's banner / pause), polled every POLL_S;
  - every terrain block the mod asked for: request -> ready (server.log terrain_ready), provisional ones, their
    build stages (queue / work / heights / photo ...), the catch-up boosts;
  - the road tiles' lead ahead of the car (roads.ahead) and the tree tiles pending near it;
  - drive.py's frame statistics (fps, frames over 33 / 50 / 100 ms).

  python3 tools/nyc_speed.py --out /tmp/nyc --speeds 100,150 --route uptown
  python3 tools/nyc_speed.py --out /tmp/nyc --cold --speeds 120        (a fresh cache: restarts the game and server)

--cold restarts the dev session (.claude/devtools/session.sh) with the server on a new, empty cache folder (the
user's own cache untouched), and --warm-first drives the route once slowly before the timed runs. Each run writes
<out>/<label>/{drive/, watch.jsonl, log.txt, summary.json}; <out>/runs.json lists them all.
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

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import mcp  # noqa: E402

LOG = os.path.expanduser("~/.cache/beamng-earth/logs/server.log")
# another machine's game (BNGE_MCP_URL through a tunnel): its server log read over ssh (BNGE_LOG_SSH=user@host)
LOG_SSH = os.environ.get("BNGE_LOG_SSH")


# a Windows machine (BNGE_LOG_WIN=1 with BNGE_LOG_SSH): its log at %LOCALAPPDATA%\beamng-earth\logs, read with PowerShell
LOG_WIN = os.environ.get("BNGE_LOG_WIN") == "1"
WIN_LOG = "$env:LOCALAPPDATA\\beamng-earth\\logs\\server.log"


def _win(script, timeout=60):
    import base64
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    return subprocess.run(["ssh", "-o", "BatchMode=yes", LOG_SSH, f"powershell -NoProfile -EncodedCommand {enc}"],
                          capture_output=True, timeout=timeout)


def log_size():
    if LOG_SSH and LOG_WIN:
        r = _win(f"(Get-Item {WIN_LOG}).Length", 30)
        return int(r.stdout.decode().strip() or 0)
    if LOG_SSH:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", LOG_SSH, "stat -c %s ~/.cache/beamng-earth/logs/server.log"],
                           capture_output=True, text=True, timeout=30)
        return int(r.stdout.strip() or 0)
    return os.path.getsize(LOG)
POLL_S = 1.0
MPH = 0.44704
GRID = 29.0             # Manhattan's avenues run 29 deg east of north

# Routes: start and end (lat, lon); the path between is the GPS's road route (the server's routing), its corners
# rounded (Chaikin's corner cutting, CHAIKIN passes: no 90 degree turn in one frame at speed).
ROUTES = {
    # the Lower East Side up the east side avenues to East Harlem
    "uptown": ((40.7238, -73.9887), (40.7967, -73.9380)),
    # through Midtown's towers: Chelsea to the Upper East Side
    "midtown": ((40.7426, -73.9928), (40.7740, -73.9560)),
    # the Upper West Side down to the Financial District
    "downtown": ((40.7831, -73.9785), (40.7075, -74.0113)),
}
CHAIKIN = 3
CORNER_M = 40.0             # m either side of a corner the rounding stays within (gps_path)
ROUTE_WAIT_S = 120

WATCH_LUA = r"""
local s = beamngEarth_core._state()
local v = getPlayerVehicle(0)
if not (s and v and s.anchor) then return 'none' end
local p, u = v:getPosition(), v:getVelocity()
local b, tp = s.block or {}, s.terrainPending
local sp = math.sqrt(u.x ^ 2 + u.y ^ 2)
local arr, road, walk, tree = nil, nil, nil, nil
if sp > 5 then
  local ok, list = pcall(require('beamngEarth/roads').ahead, p.x, p.y, u.x / sp, u.y / sp, AHEAD_PROBE_M)
  if ok and list then
    for _, e in ipairs(list) do
      if not arr and not e.arrived then arr = e.d end
      if not road and not (e.arrived and e.road) then road = e.d end
      if not walk and (not e.arrived or e.walkWait) then walk = e.d end
      if not tree and e.trees == false then tree = e.d end
    end
  end
end
return jsonEncode({ t = os.clock(), x = p.x, y = p.y, mph = sp * 2.237, guard = s.guard and s.guard.mode,
  holds = s.guard and s.guard.holds, bx = b.cx, by = b.cy, px = tp and tp.cx, py = tp and tp.cy,
  pready = tp and tp.ready and true or false, pask = tp and tp.sentAt and (s.clock - tp.sentAt) or nil,
  arrived = arr, roadLead = road, walkLead = walk, treeLead = tree })
"""
# how far ahead the road tiles are looked for (BNGE_AHEAD_PROBE_M; road lead past it reads as no gap)
AHEAD_PROBE_M = int(os.environ.get("BNGE_AHEAD_PROBE_M", "1500"))
WATCH_LUA = WATCH_LUA.replace("AHEAD_PROBE_M", str(AHEAD_PROBE_M))


def lua(code, timeout=30):
    return mcp.call("run_lua", {"code": code}) if timeout else None


def gps_path(start, end):
    """The GPS's road route start -> end as [(lat, lon)], corners rounded (the car must be at start).
    BNGE_ROUTE_CACHE=<folder>: the route's points kept there and used again (A/B runs drive the same route, and an
    older build whose GPS asked a public router that answered RateLimited still drives: 2026-11-08 bisect)."""
    cache = os.environ.get("BNGE_ROUTE_CACHE")
    cfile = (os.path.join(cache, "route_%.4f_%.4f_%.4f_%.4f.json" % (start[0], start[1], end[0], end[1]))
             if cache else None)
    pts = None
    if cfile and os.path.exists(cfile):
        with open(cfile) as f:
            pts = [tuple(p) for p in json.load(f)]
    if pts is None:
        lua(f"require('beamngEarth/gps').setDestination({end[0]}, {end[1]}, 'nyc_speed') return 1")
        t0 = time.monotonic()
        while time.monotonic() - t0 < ROUTE_WAIT_S:
            time.sleep(2)
            r = lua("local r = require('beamngEarth/gps').route() return r and jsonEncode(r.points) or ''")
            if r:
                pts = [(float(p[0]), float(p[1])) for p in json.loads(r)]
                if len(pts) >= 2:
                    break
        else:
            raise SystemExit("no GPS route")
        lua("require('beamngEarth/gps').clear() return 1")
        if cfile:
            os.makedirs(cache, exist_ok=True)
            with open(cfile, "w") as f:
                json.dump(pts, f)
    lat0 = pts[0][0]
    kx = 111320.0 * math.cos(math.radians(lat0))
    xy = [((lo - pts[0][1]) * kx, (la - lat0) * 110540.0) for la, lo in pts]
    # (points CORNER_M either side of each corner first: Chaikin cuts a quarter of each segment, and on a route of a few
    # long avenues that cut the corners by hundreds of metres, through the blocks: the car went unstable at 150 mph)
    dense = [xy[0]]
    for (x0, y0), (x1, y1) in zip(xy, xy[1:]):
        ln = math.hypot(x1 - x0, y1 - y0)
        f = min(0.5, CORNER_M / ln) if ln > 0 else 0.5
        dense += [(x0 + (x1 - x0) * f, y0 + (y1 - y0) * f), (x1 - (x1 - x0) * f, y1 - (y1 - y0) * f), (x1, y1)]
    xy = dense
    for _ in range(CHAIKIN):        # corners cut at 1/4 and 3/4 of each segment; the ends kept
        out = [xy[0]]
        for (x0, y0), (x1, y1) in zip(xy, xy[1:]):
            out += [(0.75 * x0 + 0.25 * x1, 0.75 * y0 + 0.25 * y1), (0.25 * x0 + 0.75 * x1, 0.25 * y0 + 0.75 * y1)]
        out.append(xy[-1])
        xy = out
    return [(lat0 + y / 110540.0, pts[0][1] + x / kx) for x, y in xy]


def profile(path, mph, km=None, accel=0.0):
    """drive.py's profile following path at a constant speed (km: at most this far); accel (m/s^2): from a near standstill
    up to it first, as a player sets off (a cold 200 mph start had nothing built yet and ended at once)."""
    total = 0.0
    kx = 111320.0 * math.cos(math.radians(path[0][0]))
    for (la0, lo0), (la1, lo1) in zip(path, path[1:]):
        total += math.hypot((lo1 - lo0) * kx, (la1 - la0) * 110540.0)
    km_ = total / 1000.0 if not km else min(km, total / 1000.0)
    v = mph * MPH
    pts = [{"s": 0, "speed": v}, {"s": total, "speed": v}]
    if accel > 0:
        v0 = min(v, 5.0)
        ramp = min(total, (v * v - v0 * v0) / (2 * accel))
        # (piecewise linear in distance: speed^2 linear in distance is constant acceleration; a few knots of it)
        pts = [{"s": round(ramp * k / 8, 1), "speed": math.sqrt(v0 * v0 + (v * v - v0 * v0) * k / 8)} for k in range(9)]
        pts.append({"s": total, "speed": v})
    # (clearance: the ride height kept over the ground; measured at install it took a tree or an awning the car stood on)
    return {"points": pts, "km": km_, "path": [list(p) for p in path], "clearance": 0.5}


def heading_of(path):
    kx = math.cos(math.radians(path[0][0]))
    (la0, lo0), (la1, lo1) = path[0], path[min(5, len(path) - 1)]
    return math.degrees(math.atan2((lo1 - lo0) * kx, la1 - la0)) % 360


def use_vehicle(model):
    """The player's vehicle as `model` (a jbeam name), replaced in place when it is another one. The user, 2026-11-03:
    "Instead of using a full car. Use a simple prop to reduce the chance of instability": the scripted driver places
    the vehicle every frame, and a car's soft body blew up (NaN) at 150 mph under overpasses; the ball prop has a few
    nodes. 'car' keeps whatever the session spawned."""
    if model in (None, "", "car"):
        return
    got = lua(f"local v = getPlayerVehicle(0) if v and v:getJBeamFilename() == '{model}' then return 'same' end "
              f"core_vehicles.replaceVehicle('{model}', {{}}) return 'replaced'")
    for _ in range(20):
        time.sleep(1)
        if lua(f"local v = getPlayerVehicle(0) return tostring(v ~= nil and v:getJBeamFilename() == '{model}')") == "true":
            break
    else:
        raise SystemExit(f"could not make the player vehicle a {model}")
    print(f"vehicle: {model} ({got})", flush=True)


def teleport(lat, lon, heading, wait_s=240):
    """The car to (lat, lon) facing heading, and the world around it built (the edge guard clear, the road tiles near)."""
    lua(f"beamngEarth_core.teleport({lat}, {lon}, nil, {{record = false}}) return 1")
    t0 = time.monotonic()
    while time.monotonic() - t0 < wait_s:
        time.sleep(3)
        try:
            st = lua("local s = beamngEarth_core._state() return tostring(s.placeFrames == nil and s.block ~= nil and "
                     "not s.paused)")
        except Exception:
            continue
        if st == "true" and time.monotonic() - t0 > 20:
            break
    rad = math.radians(heading)
    lua(f"""local v = getPlayerVehicle(0) local s = beamngEarth_core._state()
local x, y = s.anchor:geodeticToEnu({lat}, {lon}, 0)
local z = core_terrain.getTerrainHeight(vec3(x, y, 0))
local d = castRayStatic(vec3(x, y, z + 3), vec3(0, 0, -1), 6)     -- (a deck or a road mesh just above the terrain)
if d and d < 6 then z = z + 3 - d end
spawn.safeTeleport(v, vec3(x, y, z + 0.5), quatFromDir(vec3({math.sin(rad)}, {math.cos(rad)}, 0), vec3(0, 0, 1)),
  nil, nil, false, nil, true)
return 1""")
    time.sleep(5)


class Watch(threading.Thread):
    def __init__(self, path):
        super().__init__(daemon=True)
        self.path, self.stop, self.rows = path, threading.Event(), []

    def run(self):
        with open(self.path, "w") as f:
            while not self.stop.is_set():
                try:
                    r = lua(WATCH_LUA)
                    if r and r != "none":
                        d = json.loads(r)
                        d["wall"] = time.time()
                        self.rows.append(d)
                        f.write(json.dumps(d) + "\n")
                        f.flush()
                except Exception:
                    pass
                self.stop.wait(POLL_S)


def log_lines(since_pos):
    if LOG_SSH and LOG_WIN:     # (shared read: the server has it open)
        r = _win(f"$f = [IO.File]::Open(\"{WIN_LOG}\".Replace('$env:LOCALAPPDATA', $env:LOCALAPPDATA), 'Open', 'Read', "
                 f"'ReadWrite'); $null = $f.Seek({int(since_pos)}, 0); $r = New-Object IO.StreamReader($f); "
                 "[Console]::Out.Write($r.ReadToEnd()); $r.Close()")
        return r.stdout.decode("utf-8", "replace").splitlines()
    if LOG_SSH:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", LOG_SSH,
                            f"tail -c +{since_pos + 1} ~/.cache/beamng-earth/logs/server.log"],
                           capture_output=True, timeout=60)
        return r.stdout.decode("utf-8", "replace").splitlines()
    with open(LOG, "rb") as f:
        f.seek(since_pos)
        return f.read().decode("utf-8", "replace").splitlines()


def summarize(rows, lines, drive):
    """The run's numbers (see the module doc)."""
    moving = [r for r in rows if r.get("mph", 0) > 20]
    modes = {}
    for r in moving:
        modes[r.get("guard") or "off"] = modes.get(r.get("guard") or "off", 0) + 1
    holds = max((r.get("holds") or 0 for r in rows), default=0) - min((r.get("holds") or 0 for r in rows), default=0)
    ready = [int(m.group(2)) for m in (re.search(r"terrain_ready \((.*?)\) after (\d+) ms", l) for l in lines) if m]
    prov = sum(1 for l in lines if "terrain_ready" in l and "provisional" in l)
    boosts = sum(1 for l in lines if "catch-up boost on" in l)
    stages = {}
    for l in lines:
        m = re.search(r"terrain \(.*?\) z\d+ in ([\d.]+) s \((.*?)\)", l)
        if m:
            stages.setdefault("total", []).append(float(m.group(1)))
            for k, v in re.findall(r"(\w+) ([\d.]+)", m.group(2)):
                stages.setdefault(k, []).append(float(v))

    def pct(a, p):
        a = sorted(a)
        return round(a[min(len(a) - 1, int(p * len(a)))], 2) if a else None
    # (every tile within the probe in: its whole length, not left out, which read the lead low)
    lead = [r["arrived"] if r.get("arrived") is not None else AHEAD_PROBE_M for r in moving]
    wlead = [r["walkLead"] if r.get("walkLead") is not None else AHEAD_PROBE_M for r in moving]
    tlead = [r["treeLead"] if r.get("treeLead") is not None else AHEAD_PROBE_M for r in moving]
    geo = {}                    # slow road / trees geotiles (earthd ROAD_GEO_SLOW_S): build time and queue
    for l in lines:
        m = re.search(r"(road|trees) geotile \S+ in ([\d.]+) s \(queue ([\d.]+)[);]", l)
        if m:
            geo.setdefault(m.group(1), []).append((float(m.group(2)), float(m.group(3))))
    stand_ins = sum(1 for l in lines if "quick base photo" in l and " in " in l and "not in" not in l)
    out = {
        "polls_moving": len(moving), "guard_polls": modes, "guard_share": round(
            sum(v for k, v in modes.items() if k in ("warn", "hold")) / max(1, len(moving)), 3), "holds": holds,
        "blocks": {"n": len(ready), "provisional": prov, "ready_ms_median": pct(ready, 0.5),
                   "ready_ms_p90": pct(ready, 0.9), "ready_ms_max": max(ready) if ready else None},
        "stages_median_s": {k: pct(v, 0.5) for k, v in stages.items()},
        "stages_p90_s": {k: pct(v, 0.9) for k, v in stages.items()},
        "catchup_boosts": boosts,
        "road_lead_m": {"p10": pct(lead, 0.1), "median": pct(lead, 0.5), "zero_polls": sum(1 for x in lead if x == 0)},
        # (how far ahead the sidewalks / kerbs and the trees are placed: the first spot ahead still waiting for them)
        "walk_lead_m": {"p10": pct(wlead, 0.1), "median": pct(wlead, 0.5)},
        "tree_lead_m": {"p10": pct(tlead, 0.1), "median": pct(tlead, 0.5)},
        "slow_geotiles": {k: {"n": len(v), "s_median": pct([a for a, _ in v], 0.5), "s_max": max(a for a, _ in v),
                              "queue_median": pct([q for _, q in v], 0.5), "queue_max": max(q for _, q in v)}
                          for k, v in geo.items()},
        "stand_in_photos": stand_ins,
    }
    if drive:
        out["drive"] = {k: drive.get(k) for k in ("reason", "error", "km", "wallS", "effectiveRatio", "fpsMean", "p50",
                                                  "p99", "maxMs", "over50", "rebases", "commitFrameMaxMs", "pathJumps")}
    return out


USER_DIR = os.environ.get("BNGE_USER_DIR", os.path.expanduser("~/.local/share/BeamNG/BeamNG.drive/current"))


def keepup_save(d):
    """The keep-up recorder's record (keepup.lua) saved, stopped, and copied to <d>/keepup.csv; its table printed."""
    import shutil
    n = lua("local k = require('beamngEarth/keepup') local n = k.save('/temp/bne_keepup.csv') k.stop() "
            "return n", timeout=120)
    dst = os.path.join(d, "keepup.csv")
    if LOG_SSH and not LOG_WIN:     # (another machine's game, BNGE_LOG_SSH: the file is in ITS user folder)
        rdir = os.environ.get("BNGE_REMOTE_USER_DIR", ".local/share/BeamNG/BeamNG.drive/current")
        rsrc = f"{rdir}/temp/bne_keepup.csv"
        for _ in range(20):
            if subprocess.run(["scp", "-q", "-o", "BatchMode=yes", f"{LOG_SSH}:{rsrc}", dst]).returncode == 0:
                break
            time.sleep(0.5)
        subprocess.run(["ssh", "-o", "BatchMode=yes", LOG_SSH, f"rm -f {rsrc}"], timeout=30)
    else:
        src = os.path.join(USER_DIR, "temp", "bne_keepup.csv")
        for _ in range(20):
            if os.path.exists(src):
                break
            time.sleep(0.5)
        shutil.copy(src, dst)
        os.remove(src)
    import keepup
    print(f"keepup: {n} lines", flush=True)
    print(keepup.table([dict(keepup.analyze(os.path.join(d, "keepup.csv")), label=os.path.basename(d))]), flush=True)


def session(label, cache_dir=None):
    """A fresh dev session (the game and server restarted), on cache_dir when given."""
    env = dict(os.environ)
    if cache_dir:
        env["BNGE_LAUNCH_EXTRA"] = f"--cache-dir {cache_dir}"
    for pat in ("python3 /home/tecnagamer/Documents/BeamNG.Earth/[l]auncher", "^python3 [l]auncher.py"):
        for pid in subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True).stdout.split():
            subprocess.run(["kill", pid])
    time.sleep(3)
    # (BNGE_SESSION_SH: the main checkout's helper, for a worktree without .claude/, with BNGE_SESSION_ROOT=<worktree>)
    sh = os.environ.get("BNGE_SESSION_SH") or os.path.join(REPO, ".claude/devtools/session.sh")
    subprocess.run(["sh", sh, label], env=env, timeout=900, check=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--route", choices=sorted(ROUTES), default="uptown")
    ap.add_argument("--from", dest="start", default=None, help="LAT,LON: another route's start (with --to; e.g. an "
                    "interstate for 200+ mph, GitHub #664)")
    ap.add_argument("--to", dest="end", default=None, help="LAT,LON: that route's end")
    ap.add_argument("--speeds", default="100", help="mph, comma-separated: one run each")
    ap.add_argument("--km", type=float, default=None, help="cap each run's distance")
    ap.add_argument("--cold", action="store_true", help="restart the session on a new, empty cache folder first")
    ap.add_argument("--restart", action="store_true", help="restart the session (warm cache) first")
    ap.add_argument("--cache-dir", default=None, help="with --restart: the server on this cache folder (a scratch one)")
    ap.add_argument("--warm-first", action="store_true", help="drive the route once at 40 mph first (untimed)")
    ap.add_argument("--label", default=None)
    ap.add_argument("--bake", action="append", help="with --cold: a baked pack (.bnge) put in the fresh cache first")
    ap.add_argument("--vehicle", default=os.environ.get("BNGE_DRIVE_VEHICLE", "ball"),
                    help="the driven vehicle: a jbeam model ('ball': a simple prop, the default) or 'car' (the session's)")
    ap.add_argument("--lua-setup", default=None, help="Lua run in the game before each timed drive (A/B switches)")
    ap.add_argument("--keepup", action="store_true", help="record the details' keep-up (tools/keepup.py) in each run")
    ap.add_argument("--accel", type=float, default=0.0, help="m/s^2: set off from a near standstill (0: at speed at once)")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    if a.cold:
        cache = os.path.join(os.path.abspath(a.out), "cache")
        os.makedirs(cache, exist_ok=True)
        for pk in a.bake or ():          # (baked packs in the fresh cache: server/bakepack.py)
            import shutil
            os.makedirs(os.path.join(cache, "bake"), exist_ok=True)
            shutil.copy(pk, os.path.join(cache, "bake", os.path.basename(pk)))
        session(a.label or "nyccold", cache)
    elif a.restart:     # (--cache-dir: on that cache, e.g. a cold run's, now filled: a warm test that leaves the
        session(a.label or "nycwarm", a.cache_dir)      # player's own cache alone)
    runs = []
    speeds = [float(x) for x in a.speeds.split(",") if x]
    if a.warm_first:
        speeds = [40.0] + speeds
    for i, mph in enumerate(speeds):
        label = f"{'custom' if a.start else a.route}_{int(mph)}mph" + ("_warmup" if a.warm_first and i == 0 else "")
        if a.label:     # (each labelled run keeps its own data: runs with one --out overwrote each other's, 2026-11-03)
            label = f"{a.label}_{label}"
        d = os.path.join(a.out, label)
        os.makedirs(d, exist_ok=True)
        start, end = ROUTES.get(a.route) if not a.start else (
            tuple(map(float, a.start.split(","))), tuple(map(float, a.end.split(","))))
        print(f"{label}: teleport to {start}", flush=True)
        use_vehicle(a.vehicle)
        teleport(*start, 0.0)
        path = gps_path(start, end)
        prof = profile(path, mph, a.km, a.accel)
        with open(os.path.join(d, "profile.json"), "w") as f:
            json.dump(prof, f)
        teleport(*path[0], heading_of(path), wait_s=60)
        print(f"{label}: route {prof['km']:.1f} km, {len(path)} points", flush=True)
        pos = log_size()
        # (no vehicle-vehicle collisions during the run: traffic and parked cars on the route threw the car off)
        lua("be:setDynamicCollisionEnabled(false) return 1")
        if a.lua_setup:     # (switches for an A/B, e.g. "require('beamngEarth/trees').PROP_LEAD_S = 4")
            print(f"lua setup: {lua(a.lua_setup + ' return 1')}", flush=True)
        if a.keepup:        # (the detail keep-up recorder: tools/keepup.py)
            lua("return require('beamngEarth/keepup').start()")
        w = Watch(os.path.join(d, "watch.jsonl"))
        w.start()
        t0 = time.time()
        r = subprocess.run([sys.executable, os.path.join(HERE, "drive.py"), "--out", os.path.join(d, "drive"),
                            "--profile", os.path.join(d, "profile.json"), "--max-s", "900",
                            "--label", label], capture_output=True, text=True)
        w.stop.set()
        w.join(5)
        lua("be:setDynamicCollisionEnabled(true) return 1")
        if a.keepup:
            keepup_save(d)
        lines = log_lines(pos)
        with open(os.path.join(d, "log.txt"), "w") as f:
            f.write("\n".join(lines))
        try:
            drive = json.load(open(os.path.join(d, "drive", "summary.json")))
        except (OSError, ValueError):
            drive = None
            print(r.stdout[-2000:], r.stderr[-2000:])
        s = summarize(w.rows, lines, drive)
        s.update(label=label, mph=mph, route=a.route, cold=a.cold, wall_s=round(time.time() - t0, 1))
        with open(os.path.join(d, "summary.json"), "w") as f:
            json.dump(s, f, indent=1)
        runs.append(s)
        b = s["blocks"]
        print(f"{label}: guard {s['guard_share'] * 100:.0f}% of moving polls ({s['guard_polls']}), holds {s['holds']}; "
              f"blocks {b['n']} (provisional {b['provisional']}) ready median {b['ready_ms_median']} ms p90 "
              f"{b['ready_ms_p90']} max {b['ready_ms_max']}; road lead p10 {s['road_lead_m']['p10']} m, walk {s['walk_lead_m']['p10']}, "
              f"trees {s['tree_lead_m']['p10']}; "
              f"fps {(s.get('drive') or {}).get('fpsMean')}, drove {(s.get('drive') or {}).get('km')} km "
              f"({(s.get('drive') or {}).get('reason')})", flush=True)
    with open(os.path.join(a.out, "runs.json"), "w") as f:
        json.dump(runs, f, indent=1)


if __name__ == "__main__":
    main()
