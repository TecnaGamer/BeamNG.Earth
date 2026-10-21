#!/usr/bin/env python3
"""Performance on a real drive: the car drives itself along a GPS route (the map's Autodrive, mod gps.lua: the car's
own AI on our road graph, in lane, at the legal speed, minding traffic) while the frame times are sampled every
second. Unlike tools/drive.py (the car moved along a profile by a script) the car really drives: streaming,
traffic, physics and the GPS all run as in play, and the same route gives the same drive again.

  python3 tools/autodrive_perf.py --to 39.2634,-76.5797 --minutes 5 --out /tmp/ad
  python3 tools/autodrive_perf.py --from 39.2834,-76.5934 --to 39.2634,-76.5797 --minutes 5 --out /tmp/ad --obs

--from teleports there first and waits for the world around it. --obs records the screen with OBS Studio (the
Flatpak, its saved scene) through its websocket (tools/obsctl.py; OBS is started in the tray when needed and left
open) and writes <out>/drive.mp4 without its audio track (the microphone is in it).

Checks (the user, 2026-10-14: "add a check to see if the car crashes or stops"): a crash (the car's damage jumps by
CRASH_DAMAGE between polls), a stop (slower than STALL_MS for STOP_S while the destination is still ahead) and
leaving the route by OFF_ROUTE_M are events: events.jsonl, a screenshot each (event_<n>.jpg), listed in the
summary. A crash or a stop ends the run (--keep-going drives on).
Writes <out>/samples.jsonl (one poll per line) and <out>/summary.json; prints the summary. The game's MCP server
must be on (Options > Advanced) and the companion server running.
Process CPU and memory use /proc on Linux and native Windows APIs on Windows. Windows discovers Python server
command lines once before driving; restart this tool after restarting the server, or pass --server-pid. Explicit
--server-pid and --game-pid also disambiguate multiple sessions. Missing telemetry is written as null.
--target-kmh requests a test speed through the car's AI, including route replans, and restores its normal command
generator afterward. The car still drives through physics and may brake or fail to reach the target; samples
record its actual speed. Without this option it follows legal road speeds.
"""
import argparse
from contextlib import contextmanager
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mcp  # noqa: E402
import obsctl  # noqa: E402

POLL_S = 1.0
STALL_MS = 0.5          # slower than this (m/s) counts as standing
STOP_S = 45.0           # standing this long, not arrived: a stop (longer than a red light)
CRASH_DAMAGE = 1500.0   # the damage rising this much between two polls: a crash (a curb scrape is a few hundred)
OFF_ROUTE_M = 25.0
ARRIVED_M = 60.0

ROAD_LIMIT_LUA = r"""
local v = getPlayerVehicle(0)
if not v or not map or not map.findClosestRoad then return '0' end
local a, b = map.findClosestRoad(v:getPosition())
local nodes = map.getMap().nodes
local na, nb = a and nodes[a], b and nodes[b]
local l = (na and na.links and na.links[b]) or (nb and nb.links and nb.links[a])   -- (each edge on one of its nodes)
return tostring(l and l.speedLimit or 0)
"""

LUA_POLL = r'''
if not beamngEarth_perfmon then extensions.load('beamngEarth_perfmon') end
local pm = beamngEarth_perfmon
if pm and pm.tagHarness then pm.tagHarness() end
local p = pm and pm.snapshot() or {}
local g = require('beamngEarth/gps').status() or {}
local v = getPlayerVehicle(0)
local pos, vel = v:getPosition(), v:getVelocity()
local s = loadstring('return ' .. beamngEarth_core.status())() or {}
local pl = s.player or {}
local ob = map and map.objects and map.objects[v:getId()]
local rs = require('beamngEarth/roads').status() or {}
-- how far ahead along the car's heading the road tiles are ready (roads.ahead): the first tile not arrived, the first
-- without its road mesh, the share of arrived tiles with buildings (the world keeping up at speed; --ai-mph)
local lead = nil
do
  local sp = math.sqrt(vel.x ^ 2 + vel.y ^ 2)
  if sp > 5 then
    local ok, list = pcall(require('beamngEarth/roads').ahead, pos.x, pos.y, vel.x / sp, vel.y / sp, 2000)
    if ok and list then
      local arr, road, nb, na = nil, nil, 0, 0
      for _, e in ipairs(list) do
        if not arr and not e.arrived then arr = e.d end
        if not road and not (e.arrived and e.road) then road = e.d end
        if e.arrived then na = na + 1; if e.bld then nb = nb + 1 end end
      end
      lead = { arrived = arr or 2000, road = road or 2000, bld = nb, tiles = na }
    end
  end
end
return jsonEncode({ roadsQ = rs.walksQueued, roadMeshes = rs.roadMeshes, t = os.clock(), x = pos.x, y = pos.y, z = pos.z, v = math.sqrt(vel.x ^ 2 + vel.y ^ 2), lat = pl.lat,
  lon = pl.lon, damage = ob and ob.damage or 0,
  perf = { frames = p.frames, avgMs = p.avgMs, p50 = p.p50, p95 = p.p95, p99 = p.p99, maxMs = p.maxMs,
           over33 = p.over33, over50 = p.over50, over100 = p.over100, simRatio = p.simRatio, simWall = p.simWall,
           slowSim = p.slowSimFrames, minSim = p.minSimRatio },
  backlog = (beamngEarth_core._state() or {}).serverBacklog,
  gps = { remaining = g.remaining, off = g.off, arrows = g.arrows, autodrive = g.autodrive, planning = g.planning },
  pending = s.pending, anchor = s.anchorId, guard = s.guard, banner = s.banner, terrainLatency = s.terrainLatency,
  lead = lead, hitches = beamngEarth_core.hitches and beamngEarth_core.hitches() or {} })
'''


def _procs():
    """Linux {pid: (ppid, cmdline)}. Processes may disappear during a snapshot."""
    out = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat") as f:
                st = f.read()
            with open(f"/proc/{d}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        ppid = int(st[st.rindex(")") + 2:].split()[1])
        out[int(d)] = (ppid, cmd)
    return out


def _cpu_rss(pids):
    """Linux (cpu seconds, rss MB) summed over pids; unavailable if any cannot be read."""
    if not pids:
        return None, None
    clk = os.sysconf("SC_CLK_TCK")
    page = os.sysconf("SC_PAGE_SIZE")
    cpu = rss = 0.0
    for pid in pids:
        try:
            with open(f"/proc/{pid}/stat") as f:
                st = f.read()
        except OSError:
            return None, None
        v = st[st.rindex(")") + 2:].split()
        cpu += (int(v[11]) + int(v[12])) / clk
        rss += int(v[21]) * page / 1e6
    return cpu, rss


class WindowsProcs:
    """Standard-library Windows telemetry. CIM discovers Python command lines once; native APIs poll."""

    def __init__(self, discover=True):
        import ctypes
        from ctypes import wintypes as w

        class ProcessEntry(ctypes.Structure):
            _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("pid", w.DWORD),
                        ("heap", ctypes.c_size_t), ("module", w.DWORD), ("threads", w.DWORD),
                        ("ppid", w.DWORD), ("priority", w.LONG), ("flags", w.DWORD), ("exe", w.WCHAR * 260)]

        class MemoryCounters(ctypes.Structure):
            _fields_ = [("cb", w.DWORD), ("faults", w.DWORD)] + [
                (name, ctypes.c_size_t) for name in ("peakWorkingSet", "workingSet", "peakPagedPool", "pagedPool",
                                                   "peakNonPagedPool", "nonPagedPool", "pagefile", "peakPagefile")]

        self.ctypes, self.w, self.ProcessEntry, self.MemoryCounters = ctypes, w, ProcessEntry, MemoryCounters
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.psapi = ctypes.WinDLL("psapi", use_last_error=True)
        for fn, args, ret in (
            (self.kernel.CreateToolhelp32Snapshot, [w.DWORD, w.DWORD], w.HANDLE),
            (self.kernel.Process32FirstW, [w.HANDLE, ctypes.POINTER(ProcessEntry)], w.BOOL),
            (self.kernel.Process32NextW, [w.HANDLE, ctypes.POINTER(ProcessEntry)], w.BOOL),
            (self.kernel.OpenProcess, [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            (self.kernel.CloseHandle, [w.HANDLE], w.BOOL),
            (self.kernel.GetProcessTimes, [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4, w.BOOL),
            (self.psapi.GetProcessMemoryInfo, [w.HANDLE, ctypes.POINTER(MemoryCounters), w.DWORD], w.BOOL),
        ):
            fn.argtypes, fn.restype = args, ret
        self.commands = self._commands() if discover else {}

    @staticmethod
    def _commands():
        # Only needed for Python servers; packaged servers and the game have distinctive executable names.
        # Keep PowerShell outside the drive's measured polls: repeatedly starting it would disturb the run.
        script = ("Get-CimInstance Win32_Process -Filter \"Name LIKE 'python%.exe'\" | "
                  "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")
        powershell = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                  "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        try:
            result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command", script],
                                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
                                    check=True, creationflags=subprocess.CREATE_NO_WINDOW)
            rows = (json.loads(result.stdout) or []) if result.stdout.strip() else []
            if isinstance(rows, dict):
                rows = [rows]
            return {int(r["ProcessId"]): r.get("CommandLine") or "" for r in rows}
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            print(f"Process command lines unavailable ({e}); use --server-pid for a Python server", file=sys.stderr)
            return {}

    def procs(self):
        c, k = self.ctypes, self.kernel
        handle = k.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
        if handle == c.c_void_p(-1).value:
            return {}
        out, entry = {}, self.ProcessEntry()
        entry.dwSize = c.sizeof(entry)
        try:
            ok = k.Process32FirstW(handle, c.byref(entry))
            while ok:
                out[entry.pid] = (entry.ppid, self.commands.get(entry.pid) or entry.exe)
                ok = k.Process32NextW(handle, c.byref(entry))
        finally:
            k.CloseHandle(handle)
        # Do not apply an exited Python process's command line to a later process that reuses its PID.
        self.commands = {p: cmd for p, cmd in self.commands.items() if p in out}
        return out

    def cpu_rss(self, pids):
        if not pids:
            return None, None
        c, k, w = self.ctypes, self.kernel, self.w
        cpu = rss = 0.0
        for pid in pids:
            handle = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return None, None
            try:
                created, exited, kernel, user = (w.FILETIME() for _ in range(4))
                memory = self.MemoryCounters()
                memory.cb = c.sizeof(memory)
                if not (k.GetProcessTimes(handle, c.byref(created), c.byref(exited), c.byref(kernel), c.byref(user))
                        and self.psapi.GetProcessMemoryInfo(handle, c.byref(memory), memory.cb)):
                    return None, None
                cpu += sum((t.dwHighDateTime << 32) | t.dwLowDateTime for t in (kernel, user)) / 1e7
                rss += memory.workingSet / 1e6
            finally:
                k.CloseHandle(handle)
        return cpu, rss


def _is_server(cmd):
    """The world server's process: earthd.py from the source folder, or the launcher's --server mode (the source
    launcher, the AppImage, the .exe)."""
    head = cmd.split(" ", 2)
    return "grep" not in cmd and ("earthd.py" in cmd or (" --server " in cmd + " " and any(
        h.endswith(("launcher.py", ".AppImage", ".exe")) for h in head)))


class ProcSampler:
    """CPU % (of one core) and memory of the world server (with its worker processes) and of the game, per poll."""

    def __init__(self, server_pid=None, game_pid=None):
        self.last = {}
        self.server_pid, self.game_pid = server_pid, game_pid
        self.windows = WindowsProcs(discover=server_pid is None) if os.name == "nt" else None

    def groups(self):
        ps = self.windows.procs() if self.windows else _procs()
        server = ([self.server_pid] if self.server_pid in ps else []) if self.server_pid is not None else [
            p for p, (_, c) in ps.items() if "grep" not in c and (
                _is_server(c) or ("launcher.py" in c.lower() and "beamng.earth" in c.lower())
                or re.search(r'(^|[/\\"])beamng\.earth[^/\\"]*\.exe(?:[" ]|$)', c, re.I))]
        game_names = [p for p, (_, c) in ps.items() if
                      re.search(r'(^|[/\\"])beamng\.drive(?:\.x64)?(?:\.exe)?(?:[" ]|$)', c, re.I)]
        game = ([self.game_pid] if self.game_pid in ps else []) if self.game_pid is not None else [
            p for p in game_names if "--type=" not in ps[p][1]]

        def descendants(roots):
            kids = set(roots)
            changed = True
            while changed:
                changed = False
                for p, (pp, _) in ps.items():
                    if pp in kids and p not in kids:
                        kids.add(p)
                        changed = True
            return kids

        # Source and packaged launchers can start both the server and the game. Exclude the entire game
        # branch even if --server-pid selects their common parent, or --game-pid selects another session.
        server_tree = descendants(server) - descendants(game_names + game)
        return {"server": sorted(server_tree), "game": sorted(game)}

    def sample(self):
        now = time.monotonic()
        out = {}
        for name, pids in self.groups().items():
            cpu, rss = self.windows.cpu_rss(pids) if self.windows else _cpu_rss(pids)
            prev = self.last.get(name)
            # Worker churn changes cumulative CPU totals: start a fresh interval instead of a false spike.
            pct = (round(100 * (cpu - prev[1]) / (now - prev[0]), 1)
                   if cpu is not None and prev and prev[1] is not None and tuple(pids) == prev[2]
                   and now > prev[0] and cpu >= prev[1] else None)
            self.last[name] = (now, cpu, tuple(pids))
            out[name] = {"cpu": pct, "rssMB": round(rss) if rss is not None else None, "procs": len(pids)}
        return out


def lua(code):
    return mcp.call("run_lua", {"code": code})


def lua_json(code):
    return json.loads(lua(code))


def latlon(text):
    a, b = (float(x) for x in text.split(","))
    return a, b


def target_kmh(text):
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("target km/h must be finite and positive") from None
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("target km/h must be finite and positive")
    return value


RESTORE_SPEED_LUA = r'''
local saved = rawget(_G, 'BNGE_AUTODRIVE_SPEED')
if saved then
  if saved.gps.aiCommand == saved.wrapper then saved.gps.aiCommand = saved.original end
  _G.BNGE_AUTODRIVE_SPEED = nil
end
return 'speed override restored'
'''

TARGET_SPEED_LUA = r'''
local gps = require('beamngEarth/gps')
local previous = rawget(_G, 'BNGE_AUTODRIVE_SPEED')
if previous and previous.gps.aiCommand == previous.wrapper then previous.gps.aiCommand = previous.original end
local original = gps.aiCommand
assert(type(original) == 'function', 'GPS aiCommand unavailable')
local saved = { gps = gps, original = original }
saved.wrapper = function(...)
  local command = original(...)
  -- Assign gsub's first result: returning gsub directly also returns its replacement count.
  local changed = command:gsub('routeSpeedMode%s*=%s*"legal"',
    'routeSpeedMode = "set", routeSpeed = __TARGET_MPS__', 1)
  return changed
end
_G.BNGE_AUTODRIVE_SPEED = saved
gps.aiCommand = saved.wrapper
return 'speed override installed'
'''


@contextmanager
def speed_override(kmh):
    """Test-local GPS command override, including subsequent replans; restore after any exit path."""
    if kmh is None:
        yield
        return
    try:
        lua(TARGET_SPEED_LUA.replace("__TARGET_MPS__", repr(kmh / 3.6)))
        yield
    finally:
        try:
            lua(RESTORE_SPEED_LUA)
        except (Exception, SystemExit) as e:
            print(f"GPS speed override could not be restored: {e}", file=sys.stderr)


def obs_start():
    """Start recording through OBS's websocket (tools/obsctl.py: OBS stays open in the tray); True, or None."""
    try:
        obsctl.start()
        return True
    except (OSError, obsctl.ObsError) as e:
        print(f"OBS did not start recording: {e}", file=sys.stderr)
        return None


SHARE_MB = 29.0         # the small copy for sending (a chat upload limit of 30 MB)


def share_copy(video):
    """A 720p 30 fps copy of video beside it (drive_small.mp4) under SHARE_MB, or None."""
    out = os.path.join(os.path.dirname(video), "drive_small.mp4")
    for crf in (30, 34, 38):
        subprocess.run(["nice", "ffmpeg", "-v", "error", "-y", "-i", video, "-vf", "scale=1280:-2,fps=30", "-c:v",
                        "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p", "-movflags",
                        "+faststart", out], check=True)
        if os.path.getsize(out) <= SHARE_MB * 1024 * 1024:
            return out
    return None


def obs_stop(started, out_path):
    """Stop the recording (OBS stays open) and write its file without audio."""
    try:
        rec = obsctl.stop()
    except (OSError, obsctl.ObsError) as e:
        print(f"OBS did not stop recording: {e}", file=sys.stderr)
        return None
    if not rec or not os.path.exists(rec):
        return None
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", rec, "-map", "0:v", "-c", "copy", out_path], check=True)
    os.remove(rec)
    return out_path


def shot(out_dir, name, quiet=True):
    """A screenshot of the game into out_dir/name (best effort). quiet: our welcome page closed first (automated
    runs); False leaves the UI as it is (a player's game: a menu they have open stays open)."""
    try:
        if quiet:
            mcp.quiet_ui()
        p = mcp.call("screenshot", {"jpg": True}).strip()
        for _ in range(20):
            time.sleep(0.25)
            if os.path.exists(p) and os.path.getsize(p) > 0:
                break
        time.sleep(0.5)
        shutil.move(p, os.path.join(out_dir, name))
        return name
    except (Exception, SystemExit) as e:
        return f"no screenshot: {e}"


class Checks:
    """The run's events from the polls (pure but for the screenshot callback): crash, stop, off route."""

    def __init__(self, on_event):
        self.on_event = on_event
        self.prev = None
        self.still_since = None
        self.off = False
        self.events = []

    def feed(self, r):
        g = r["gps"]
        rem = g.get("remaining")
        ahead = rem is None or rem > ARRIVED_M
        ev = None
        if self.prev is not None and (r.get("damage") or 0) - (self.prev.get("damage") or 0) > CRASH_DAMAGE:
            ev = {"kind": "crash", "damage": round(r["damage"]), "mph_before": round(self.prev["v"] * 2.23694, 1)}
        if r["v"] < STALL_MS and ahead and not g.get("planning"):
            self.still_since = self.still_since if self.still_since is not None else r["t"]
            if ev is None and r["t"] - self.still_since >= STOP_S:
                ev = {"kind": "stop", "seconds": round(r["t"] - self.still_since)}
                self.still_since = float("inf")     # once per stop
        else:
            self.still_since = None
        off = (g.get("off") or 0) > OFF_ROUTE_M
        if ev is None and off and not self.off:
            ev = {"kind": "off_route", "m": round(g.get("off"), 1)}
        self.off = off
        self.prev = r
        if ev is not None:
            ev.update({"t": round(r["t"], 1), "x": round(r["x"], 1), "y": round(r["y"], 1), "lat": r.get("lat"),
                       "lon": r.get("lon"), "arrows": g.get("arrows")})
            ev["screenshot"] = self.on_event(len(self.events), ev)
            self.events.append(ev)
        return ev


def hitch_cause(h):
    """The part a hitch frame went to: ours (mod.<part>) when it took 40 % of the frame, else the engine's largest."""
    mod = h.get("mod") or {}
    if mod:
        k, v = max(mod.items(), key=lambda kv: kv[1])
        if v >= 0.4 * h["ms"]:
            return "mod." + k
    eng = h.get("engine") or {}
    if eng:
        return "engine." + max(eng.items(), key=lambda kv: kv[1])[0]
    return "unknown"


def summarize(rows):
    """The drive's statistics from the polls (frames weighted by their count)."""
    frames = sum(r["perf"].get("frames") or 0 for r in rows)
    def wavg(key):
        n = sum((r["perf"].get("frames") or 0) for r in rows if r["perf"].get(key) is not None)
        return sum((r["perf"].get(key) or 0) * (r["perf"].get("frames") or 0) for r in rows) / n if n else None
    dist = sum(math.hypot(b["x"] - a["x"], b["y"] - a["y"]) for a, b in zip(rows, rows[1:])
               if a.get("anchor") == b.get("anchor") and math.hypot(b["x"] - a["x"], b["y"] - a["y"]) < 200)
    dur = rows[-1]["t"] - rows[0]["t"] if len(rows) > 1 else 0
    moving = [r for r in rows if r["v"] >= STALL_MS]
    avg_ms = wavg("avgMs")
    return {
        "polls": len(rows), "seconds": round(dur, 1), "frames": frames,
        "fps": round(1000 / avg_ms, 1) if avg_ms else None,
        "frame_ms": {"avg": avg_ms and round(avg_ms, 2), "p50": wavg("p50") and round(wavg("p50"), 2),
                     "p95": wavg("p95") and round(wavg("p95"), 2), "p99": wavg("p99") and round(wavg("p99"), 2),
                     "max": max((r["perf"].get("maxMs") or 0) for r in rows)},
        "frames_over": {k: sum(r["perf"].get(k) or 0 for r in rows) for k in ("over33", "over50", "over100")},
        "miles": round(dist / 1609.344, 2),
        "avg_mph_moving": round(sum(r["v"] for r in moving) / len(moving) * 2.23694, 1) if moving else 0,
        "standing_s": round(sum(POLL_S for r in rows if r["v"] < STALL_MS), 1),
        "off_route_max_m": round(max((r["gps"].get("off") or 0) for r in rows), 1),
        "arrows_fallback_polls": sum(1 for r in rows if not str(r["gps"].get("arrows") or "").startswith(("r", "final"))),
        "remaining_m": rows[-1]["gps"].get("remaining"),
        "hitch_causes": _causes(rows),
        "edge": {m: sum(1 for r in rows if r.get("guard") == m) for m in ("warn", "hold")},
        "cpu_cores": _cpu(rows),
    }


def _cpu(rows):
    """Average and peak cores busy (a poll's CPU %/100) of the server (with its workers) and of the game."""
    out = {}
    for name in ("server", "game"):
        v = [r["procs"][name]["cpu"] for r in rows if (r.get("procs") or {}).get(name, {}).get("cpu") is not None]
        if v:
            rss = [r["procs"][name]["rssMB"] for r in rows
                   if (r.get("procs") or {}).get(name, {}).get("rssMB") is not None]
            out[name] = {"avg": round(sum(v) / len(v) / 100, 2), "max": round(max(v) / 100, 2),
                         "rssMB": max(rss, default=None)}
    return out


def _causes(rows):
    """{cause: [count, worst ms]} of the hitch frames (core.lua HITCH_MS), most frequent first."""
    out = {}
    for r in rows:
        hs = r.get("hitches") or []
        for h in (hs.values() if isinstance(hs, dict) else hs):
            c = hitch_cause(h)
            n, w = out.get(c, (0, 0))
            out[c] = (n + 1, max(w, h["ms"]))
    return {k: list(v) for k, v in sorted(out.items(), key=lambda kv: -kv[1][0])}


# The car on the nearest road-graph node (a teleport lands it where the point is, a sidewalk or a field: the AI
# then drove along a sidewalk into a pole, 2026-10-15), facing the linked node that points most toward the goal.
SNAP_LUA = r"""
local tlat, tlon, minSpeed = %r, %r, %r
local m = map.getMap()
local veh = getPlayerVehicle(0)
if not (m and m.nodes and veh) then return 'snap: no graph' end
local p = veh:getPosition()
local st = beamngEarth_core and beamngEarth_core._state and beamngEarth_core._state()
local a = st and st.anchor
local gx, gy = 0, 0
if a and a.geodeticToEnu then gx, gy = a:geodeticToEnu(tlat, tlon, 0) end
-- every edge both ways it may be driven (the game keeps an edge on one node only; one-way edges from their inNode),
-- fast enough, running toward the goal: the nearest one's start
local best, bd, bdir = nil, math.huge, nil
local function try(from, to, e)
  local A, B = m.nodes[from], m.nodes[to]
  if not (A and B and A.pos and B.pos) then return end
  if (tonumber(e.speedLimit) or 0) < minSpeed then return end
  if e.oneWay and e.inNode and e.inNode ~= from then return end
  local dx, dy = B.pos.x - A.pos.x, B.pos.y - A.pos.y
  local L = math.sqrt(dx * dx + dy * dy)
  local tx, ty = gx - A.pos.x, gy - A.pos.y
  local T = math.sqrt(tx * tx + ty * ty)
  if L < 1 or (T > 0 and (dx * tx + dy * ty) / (L * T) < 0.3) then return end
  local d = (A.pos.x - p.x) ^ 2 + (A.pos.y - p.y) ^ 2
  if d < bd then best, bd, bdir = from, d, vec3(dx / L, dy / L, 0) end
end
for id, n in pairs(m.nodes) do
  for other, e in pairs(n.links or {}) do try(id, other, e); try(other, id, e) end
end
if not best then return 'snap: no node' end
local n = m.nodes[best]
local rot = quatFromDir(bdir, vec3(0, 0, 1))
local z = be:getSurfaceHeightBelow(n.pos + vec3(0, 0, 3))
veh:setPositionRotation(n.pos.x, n.pos.y, ((z and z > -1e6) and z or n.pos.z) + 0.5, rot.x, rot.y, rot.z, rot.w)
return string.format('snap: node %%s %%.1f m away', tostring(best), math.sqrt(bd))
"""

# On the GPS route: the car set at the route's node nearest it (not the last two), facing the next one; for a start
# whose nearest fast road is not the route's (the other carriageway of a motorway: the AI cut across the median to its
# route, deck_fast10 2026-10-20).
ROUTE_SNAP_LUA = r"""
local gm = rawget(_G, 'core_groundMarkers')
local veh = getPlayerVehicle(0)
local m = map.getMap()
if not (gm and gm.routePlanner and veh and m and m.nodes) then return 'route snap: no route' end
local ids = require('beamngEarth/gps').pathNodes(gm.routePlanner.path)
local p = veh:getPosition()
local best, bd = nil, math.huge
for k = 1, #ids - 2 do
  local n = m.nodes[ids[k]]
  if n and n.pos and m.nodes[ids[k + 1]] then
    local d = (n.pos.x - p.x) ^ 2 + (n.pos.y - p.y) ^ 2
    if d < bd then best, bd = k, d end
  end
end
if not best then return 'route snap: no node' end
local A, B = m.nodes[ids[best]].pos, m.nodes[ids[best + 1]].pos
local dir = vec3(B.x - A.x, B.y - A.y, 0):normalized()
local rot = quatFromDir(dir, vec3(0, 0, 1))
local z = be:getSurfaceHeightBelow(A + vec3(0, 0, 3))
veh:setPositionRotation(A.x, A.y, ((z and z > -1e6) and z or A.z) + 0.5, rot.x, rot.y, rot.z, rot.w)
return string.format('route snap: node %%d of %%d, %%.1f m away', best, #ids, math.sqrt(bd))
"""
ROUTE_SNAP_OFF_M = 15.0     # the start this far off the GPS route: set on it (ROUTE_SNAP_LUA)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="start", type=latlon, help="lat,lon to teleport to first")
    ap.add_argument("--to", required=True, type=latlon, help="lat,lon of the destination")
    ap.add_argument("--minutes", type=float, default=5.0)
    ap.add_argument("--out", required=True)
    ap.add_argument("--settle", type=float, default=45.0, help="seconds to wait after the teleport")
    ap.add_argument("--obs", action="store_true", help="record the screen with OBS (writes drive.mp4, no audio)")
    ap.add_argument("--keep-going", action="store_true", help="drive on after a crash or a stop")
    ap.add_argument("--rec", action="store_true", help="record the game's own frames (tools/gamerec.py: no screen "
                    "capture, no permission dialog; ~15 ms a frame, so the frame times are not the game's then)")
    ap.add_argument("--rec-fps", type=float, default=15.0)
    speeds = ap.add_mutually_exclusive_group()
    speeds.add_argument("--ai-mph", type=float, default=0.0,
                    help="the car drives at this speed instead of the legal one (gps.speedOverride): the world keeping "
                    "up at speed; the summary's lead says how far ahead the road meshes were ready")
    ap.add_argument("--fast-after", type=float, default=0.0,
                    help="with --ai-mph: drive at the legal speed this many seconds first (out of town onto the "
                    "motorway), then at --ai-mph (ai.setSpeed, its path kept) once the road under the car has a "
                    "limit of --fast-kmh or more (the AI crashed at 105 mph in town, deck_fast5 2026-10-20)")
    ap.add_argument("--fast-kmh", type=float, default=80.0)
    ap.add_argument("--snap-kmh", type=float, default=0.0,
                    help="start on a road this fast at least (the nearest such graph node): a motorway test")
    speeds.add_argument("--target-kmh", type=target_kmh,
                    help="test-only AI target speed, including replans (default: legal road speeds)")
    ap.add_argument("--server-pid", type=int, help="world server PID (includes its worker descendants)")
    ap.add_argument("--game-pid", type=int, help="BeamNG game PID (overrides automatic process discovery)")
    a = ap.parse_args()
    with speed_override(a.target_kmh):
        run_drive(a)


def run_drive(a):
    os.makedirs(a.out, exist_ok=True)
    mcp.quiet_ui()          # (the recording too: our welcome page closed and kept away this session)
    if a.start:
        aid0 = lua("return tostring(beamngEarth_core._state().anchorId)").strip()
        lua("return tostring(beamngEarth_core.teleport(%r, %r, 'autodrive_perf start', {record = false}))" % a.start)
        # the game teleports once the server answers (after a server restart that was ~85 s later: the snap and the
        # drive ran at the old place, Cluj 2026-10-15): the settle counts from the arrival, a new anchor (an old one at
        # the same place does not count: A9 2026-10-15)
        t_arrive = time.time() + 300
        while time.time() < t_arrive:
            try:
                aid = lua("return tostring(beamngEarth_core._state().anchorId)").strip()
            except (Exception, SystemExit):
                aid = aid0
            if aid != aid0:
                break
            time.sleep(2)
        else:
            print("the teleport did not happen within 300 s", file=sys.stderr)
        time.sleep(a.settle)
    if a.start:                     # on the road: the nearest graph node, facing along its edge toward the goal
        for _ in range(30):         # (the new place's road graph can come a while after the settle)
            r = lua(SNAP_LUA % (a.to[0], a.to[1], a.snap_kmh / 3.6))
            if "snap: node" in r:
                break
            time.sleep(2)
        print(r, flush=True)
        time.sleep(3)
    sampler = ProcSampler(server_pid=a.server_pid, game_pid=a.game_pid)
    sampler.sample()  # Windows command-line discovery and the first CPU baseline precede the drive.
    obs = grec = evf = video = None
    rows = []
    autodrive_seen, end_reason = False, "duration"
    fast = repr(a.ai_mph * 0.44704) if a.ai_mph else "nil"
    fast_at = time.time() + a.fast_after if a.ai_mph and a.fast_after else None

    def road_limit():
        """The speed limit (m/s) of the graph road nearest the car, 0 when none or on an error."""
        try:
            return float(lua(ROAD_LIMIT_LUA).strip() or 0)
        except (Exception, SystemExit, ValueError):
            return 0.0
    checks = Checks(lambda n, ev: shot(a.out, f"event_{n}_{ev['kind']}.jpg"))
    try:
        lua("require('beamngEarth/gps').speedOverride = %s return 1" % (fast if not a.fast_after else "nil"))
        lua("beamngEarth_settings.uiMapGps(%r, %r, 'autodrive_perf') return 'ok'" % a.to)
        if a.start:                 # the nearest road may be the route's other carriageway
            for _ in range(15):
                time.sleep(2)
                try:
                    g = lua_json("return jsonEncode(require('beamngEarth/gps').status())")
                except (Exception, SystemExit):
                    g = None
                if g and g.get("off") is not None and not g.get("planning"):
                    if g["off"] > ROUTE_SNAP_OFF_M:
                        print(lua(ROUTE_SNAP_LUA), flush=True)
                        time.sleep(3)
                    break
        # Setup and earlier drives leave both diagnostics buffered; reset them with activation.
        lua("if beamngEarth_core and beamngEarth_core.hitches then beamngEarth_core.hitches() end "
            "if beamngEarth_perfmon and beamngEarth_perfmon.snapshot then beamngEarth_perfmon.snapshot() end "
            "return tostring(beamngEarth_settings.uiGpsAutodrive(true).autodrive)")
        obs = obs_start() if a.obs else None
        if a.rec:                   # the game's own frames (no screen capture: works unattended on Wayland)
            import gamerec
            grec = gamerec.start(a.rec_fps)
        end = time.time() + a.minutes * 60
        evf = open(os.path.join(a.out, "events.jsonl"), "w")
        with open(os.path.join(a.out, "samples.jsonl"), "w") as f:
            while time.time() < end:
                t0 = time.time()
                if fast_at is not None and t0 >= fast_at and road_limit() >= a.fast_kmh / 3.6:  # (--fast-after)
                    fast_at = None
                    # the AI's speed alone (its path kept; the override for the paths given later): restarted, the
                    # AI got a fresh path and cut across a motorway median to it (deck_fast9, 2026-10-20)
                    lua("require('beamngEarth/gps').speedOverride = %s getPlayerVehicle(0):queueLuaCommand("
                        "'ai.setSpeedMode(\"set\") ai.setSpeed(%s)') return 1" % (fast, fast))
                    print("now at %.0f mph" % a.ai_mph, flush=True)
                try:
                    r = lua_json(LUA_POLL)
                except (Exception, SystemExit) as e:    # a busy frame, a tool error: the next poll
                    print(f"poll: {e}", file=sys.stderr)
                    r = None
                if r:
                    r["procs"] = sampler.sample()
                    rows.append(r)
                    f.write(json.dumps(r) + "\n")
                    f.flush()
                    ev = checks.feed(r) if len(rows) > 2 else None
                    if ev is not None:
                        print("event: " + json.dumps(ev), flush=True)
                        evf.write(json.dumps(ev) + "\n")
                        evf.flush()
                        if ev["kind"] in ("crash", "stop") and not a.keep_going:
                            end_reason = ev["kind"]
                            break
                    if r["gps"].get("autodrive") is True:
                        autodrive_seen = True
                    elif autodrive_seen:
                        end_reason = "autodrive_ended"
                        break                   # arrival clears the GPS fields; stopping by hand also ends it
                time.sleep(max(0.0, POLL_S - (time.time() - t0)))
    finally:
        try:
            try:
                if obs is not None:
                    video = obs_stop(obs, os.path.join(a.out, "drive.mp4"))
            finally:
                if grec is not None:
                    video = grec.stop(os.path.join(a.out, "drive.mp4"))
        finally:
            try:
                lua("beamngEarth_settings.uiGpsAutodrive(false) beamngEarth_settings.uiMapGps(nil) "
                    "require('beamngEarth/gps').speedOverride = nil return 'ok'")
            finally:
                if evf is not None:
                    evf.close()
    summary = summarize(rows[1:]) if len(rows) > 2 else {"polls": len(rows)}   # the first poll covers the setup
    summary["events"] = checks.events
    summary["targetKmh"] = a.target_kmh
    summary["endReason"] = end_reason
    leads = [r["lead"] for r in rows[1:] if isinstance(r.get("lead"), dict)]
    if leads:
        rl = sorted(x["road"] for x in leads)
        summary["lead"] = {"polls": len(leads), "road_median_m": rl[len(rl) // 2], "road_p10_m": rl[len(rl) // 10],
                           "road_under_200m_s": sum(1 for x in rl if x < 200),
                           "road_missing_under_car_s": sum(1 for x in rl if x == 0),
                           "arrived_median_m": sorted(x["arrived"] for x in leads)[len(leads) // 2]}
    if video:
        summary["video"] = video
        summary["video_small"] = share_copy(video)
    with open(os.path.join(a.out, "summary.json"), "w") as f:
        json.dump(summary, f, indent=1)
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
