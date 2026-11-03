#!/usr/bin/env python3
"""Unified Cold-Cache Build-and-Stream Benchmark & A/B Harness for BeamNG.Earth (v1.7.0-beta).

Provides standardized cold-start benchmarking, deterministic multi-layer cache purging,
in-engine per-frame .bngperfgraph.json recording, autonomous corridor spline driving,
and rigorous A/B comparison reporting between builds/branches.

Subcommands:
  purge    - Purge compiled caches and reset raw network cache (build-and-stream or full-network)
  seed     - Capture a pristine byte-identical raw network cache snapshot from ~/.cache/beamng-earth
  run      - Execute the complete end-to-end cold-start benchmark (teleport build + 3km spline drive)
  compare  - Compare two benchmark runs with delta tables and objective KEEP/REJECT verdict

Usage:
  python tools/cold_ab_bench.py purge [--cold-mode build-and-stream|full-network] [--seed-dir cache/cold_seed_lillehammer_e6]
  python tools/cold_ab_bench.py seed [--seed-dir cache/cold_seed_lillehammer_e6]
  python tools/cold_ab_bench.py run --label baseline_v170 [options]
  python tools/cold_ab_bench.py compare runs/run_baseline runs/run_candidate [--out report_dir]
"""

import argparse
import datetime
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import time

try:
    import psutil
except ImportError:
    psutil = None

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)
SERVER_DIR = os.path.join(ROOT, "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

import drive
import geocode
import mcp
import netfetch
import routing

# Standardized Mont Ventoux D974 Corridor Defaults (START -> TOP)
DEFAULT_START = (44.171279, 5.139169)   # Mont Ventoux D974 START
DEFAULT_TOP = (44.173433, 5.277728)     # Mont Ventoux D974 TOP
DEFAULT_START_NAME = "Mont Ventoux D974 START"
DEFAULT_TOP_NAME = "Mont Ventoux D974 TOP"
DEFAULT_KM = 3.0
DEFAULT_SPEED = 53.6448                  # 53.64 m/s = 193.1 km/h = 120.0 mph
DEFAULT_SEED_DIR = os.path.join(ROOT, "cache", "cold_seed_mont_ventoux")
DEFAULT_TELEPORT_TIMEOUT = 150.0
SERVER_PORT = 47613

# Cache definition constants
RAW_DIRS = ("dem", "imagery", "mapdata", "mapdata-mirror", "roadwidth", "airports", "gravity")
RAW_FILES = ("index.tsv", "VERSION")
COMPILED_CACHE_DIRS = ("vegetation", "seacarve", "colormatch", "deshadow", "revisit", "plans", "pagesdata", "peercache", "dem/decoded")
USER_TEMP_DIRS = ("temp/beamngEarth", "temp/levels/beamng_earth")
USER_LEVEL_RUNTIME_DIRS = (
    "levels/beamng_earth/far",
    "levels/beamng_earth/roads",
    "levels/beamng_earth/veg",
    "levels/beamng_earth/blocks",
)


# --- Path & Discovery Helpers -----------------------------------------------------------

def get_user_dir():
    """Locate BeamNG user directory from .beamng_earth.json or standard local appdata."""
    cfg_path = os.path.join(ROOT, ".beamng_earth.json")
    if os.path.isfile(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data.get("user_dir") and os.path.isdir(data["user_dir"]):
                    return os.path.abspath(data["user_dir"])
        except Exception:
            pass
    if os.name != "nt":
        try:
            if ROOT not in sys.path:
                sys.path.insert(0, ROOT)
            import install
            cands = install.candidate_user_dirs()
            if cands:
                return os.path.abspath(cands[0])
        except Exception:
            pass
        return os.path.expanduser("~/.local/share/BeamNG/BeamNG.drive/current")
    default_path = os.path.expandvars(r"%LOCALAPPDATA%\BeamNG\BeamNG.drive\current")
    if os.path.isdir(default_path):
        return os.path.abspath(default_path)
    return default_path


def get_cache_dir():
    """Locate persistent BeamNG.Earth cache directory (~/.cache/beamng-earth)."""
    home = os.path.expanduser("~")
    return os.path.join(home, ".cache", "beamng-earth")


def ensure_spline_driver():
    """Ensure drive.py uses the enhanced driver supporting exact (x, y) spline coordinates."""
    drive.DRIVER = os.path.join(HERE, "drive_driver.lua")


# --- Process Management -----------------------------------------------------------------

def is_server_listening(port=SERVER_PORT):
    """Check if companion server or external process is listening on the given TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def stop_earthd():
    """Safely terminate python processes running server/earthd.py without touching agy.exe or others."""
    if not psutil:
        return False
    stopped = False
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (p.info["name"] or "").lower()
            if "python" in name:
                cmd = " ".join(p.info["cmdline"] or [])
                if "server/earthd.py" in cmd or "server\\earthd.py" in cmd:
                    print(f"Stopping earthd.py process (PID {p.info['pid']})...", flush=True)
                    p.terminate()
                    try:
                        p.wait(timeout=3.0)
                    except psutil.TimeoutExpired:
                        p.kill()
                    stopped = True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return stopped


# --- Cache Management & Seeding ---------------------------------------------------------

def purge_caches(cold_mode="build-and-stream", seed_dir=None, keep_server=False, restart_server=False):
    """Purge compiled runtime caches and reset network cache according to cold_mode."""
    user_dir = get_user_dir()
    cache_dir = get_cache_dir()
    seed_path = os.path.abspath(seed_dir) if seed_dir else DEFAULT_SEED_DIR

    print(f"\n[CACHE PURGE] Mode: {cold_mode} | Cache: {cache_dir} | User: {user_dir}", flush=True)

    # 1. Server management: stop earthd if needed
    listening = is_server_listening(SERVER_PORT)
    if listening:
        if restart_server or not keep_server:
            print(f"Stopping existing server on port {SERVER_PORT}...", flush=True)
            stop_earthd()
            time.sleep(1.0)
        else:
            print(f"Preserving running companion server on port {SERVER_PORT} (--keep-server active).", flush=True)

    # 2. Always delete compiled/generated caches in <cache_dir>
    for sub in COMPILED_CACHE_DIRS:
        p = os.path.join(cache_dir, sub)
        if os.path.exists(p):
            print(f"  Removing compiled cache: {sub}", flush=True)
            shutil.rmtree(p, ignore_errors=True)

    # 3. Always delete generated runtime folders in <user_dir>
    for sub in USER_TEMP_DIRS:
        p = os.path.join(user_dir, sub)
        if os.path.exists(p):
            print(f"  Removing user temp: {sub}", flush=True)
            shutil.rmtree(p, ignore_errors=True)

    for sub in USER_LEVEL_RUNTIME_DIRS:
        p = os.path.join(user_dir, sub)
        if os.path.exists(p):
            print(f"  Removing level runtime: {sub}", flush=True)
            shutil.rmtree(p, ignore_errors=True)

    # 4. Handle cold mode network data
    if cold_mode == "full-network":
        print("  Full network cold mode: deleting raw network cache folders...", flush=True)
        for sub in ("dem", "imagery", "mapdata", "mapdata-mirror", "roadwidth"):
            p = os.path.join(cache_dir, sub)
            if os.path.exists(p):
                shutil.rmtree(p, ignore_errors=True)
    elif cold_mode == "build-and-stream":
        has_seed = os.path.isdir(seed_path) and (
            os.path.isdir(os.path.join(seed_path, "dem")) or os.path.isdir(os.path.join(seed_path, "mapdata"))
        )
        if has_seed:
            print(f"  Applying seed snapshot from: {seed_path}", flush=True)
            if not keep_server:
                for sub in RAW_DIRS:
                    p = os.path.join(cache_dir, sub)
                    if os.path.exists(p):
                        shutil.rmtree(p, ignore_errors=True)
            for sub in RAW_DIRS:
                src = os.path.join(seed_path, sub)
                dst = os.path.join(cache_dir, sub)
                if os.path.isdir(src):
                    if not os.path.isdir(dst):
                        shutil.copytree(src, dst, dirs_exist_ok=True)
                    else:
                        for root_d, _, files_d in os.walk(src):
                            rel_d = os.path.relpath(root_d, src)
                            target_d = dst if rel_d == "." else os.path.join(dst, rel_d)
                            os.makedirs(target_d, exist_ok=True)
                            for fn in files_d:
                                sf = os.path.join(root_d, fn)
                                df = os.path.join(target_d, fn)
                                if not os.path.exists(df):
                                    try:
                                        shutil.copy2(sf, df)
                                    except OSError:
                                        pass
            for f in RAW_FILES:
                src = os.path.join(seed_path, f)
                dst = os.path.join(cache_dir, f)
                if os.path.isfile(src):
                    try:
                        shutil.copy2(src, dst)
                    except OSError:
                        pass
            print("  Seed snapshot applied successfully.", flush=True)
        else:
            print(f"  Seed snapshot directory not found or unpopulated ({seed_path}). Preserving existing raw cache.", flush=True)

    print("[CACHE PURGE COMPLETE]\n", flush=True)


def save_seed(seed_dir=None):
    """Save raw network data (RAW_DIRS and RAW_FILES) from <cache_dir> into seed_dir."""
    cache_dir = get_cache_dir()
    seed_path = os.path.abspath(seed_dir) if seed_dir else DEFAULT_SEED_DIR
    os.makedirs(seed_path, exist_ok=True)
    print(f"\n[SEED SNAPSHOT] Saving raw network cache to: {seed_path}...", flush=True)

    copied_dirs = 0
    for sub in RAW_DIRS:
        src = os.path.join(cache_dir, sub)
        dst = os.path.join(seed_path, sub)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
            copied_dirs += 1

    copied_files = 0
    for f in RAW_FILES:
        src = os.path.join(cache_dir, f)
        dst = os.path.join(seed_path, f)
        if os.path.isfile(src):
            shutil.copy2(src, dst)
            copied_files += 1

    print(f"[SEED SNAPSHOT COMPLETE] Copied {copied_dirs} directories and {copied_files} files.\n", flush=True)


# --- In-Engine Per-Frame .bngperfgraph.json Recorder -------------------------------------

LUA_START_RECORDER = r"""
if _G.bngEarthPerfCap and _G.bngEarthPerfCap.proxy then
  pcall(function() _G.bngEarthPerfCap.proxy:destroy() end)
  _G.bngEarthPerfCap = nil
  _G.__bngPerfCapture = nil
end

local proxy = newExtensionProxy(nil, 'bngEarthPerfCap')
local sink = {}

local hwCpu = Engine.Platform.getCPUInfo and Engine.Platform.getCPUInfo() or {}
local hwGpu = Engine.Platform.getGPUInfo and Engine.Platform.getGPUInfo() or {}
local osInfo = Engine.Platform.getOSInfo and Engine.Platform.getOSInfo() or {}

_G.__bngPerfCapture = {
  info = {
    HW = {
      GPU = {
        name = hwGpu.name or "Unknown GPU",
        version = hwGpu.version or "",
        memoryMB = hwGpu.memoryMB or 0
      },
      cpu = {
        brandName = hwCpu.name or "Unknown CPU",
        clockSpeed = hwCpu.clockSpeed or 0,
        coresLogical = hwCpu.coresLogical or 0,
        coresPhysical = hwCpu.coresPhysical or 0
      },
      os = {
        fullname = osInfo.name or "Microsoft Windows",
        bits = 64
      }
    },
    SW = {
      version = beamng_version or "unknown",
      versiontitle = beamng_versiontitle or "BeamNG.drive"
    }
  },
  FPS = {
    FPS = {}
  },
  CPU = {},
  GPU = {},
  memory = {
    osPhysAvailable = {},
    osPhysUsed = {},
    osVirtAvailable = {},
    osVirtUsed = {}
  },
  stats = {
    ShadowActiveMaps = {},
    ShadowUpdatedMaps = {},
    ShadowPolyCount = {},
    ShadowDrawCalls = {},
    ForestTotalCells = {},
    ForestCellsRendered = {},
    ForestCellItemsRendered = {},
    GFXPolyCount = {},
    GFXDrawCalls = {}
  },
  frameCount = 0
}

local cap = _G.__bngPerfCapture
local m_buf = {}

function sink:onPreRender(dtReal, dtSim)
  if not _G.__bngPerfCapture then return end
  local c = _G.__bngPerfCapture
  c.frameCount = c.frameCount + 1

  -- 1. FPS
  local fps = dtReal > 0 and (1.0 / dtReal) or 60.0
  table.insert(c.FPS.FPS, fps)

  -- 2. CPU & Memory Subsystems from getLastPerformanceMetrics
  table.clear(m_buf)
  Engine.Debug.getLastPerformanceMetrics(m_buf)
  for k, val in pairs(m_buf) do
    if k == 'physAvailable' then
      table.insert(c.memory.osPhysAvailable, val)
    elseif k == 'physUsed' then
      table.insert(c.memory.osPhysUsed, val)
    elseif k == 'virtAvailable' then
      table.insert(c.memory.osVirtAvailable, val)
    elseif k == 'virtUsed' then
      table.insert(c.memory.osVirtUsed, val)
    else
      local arr = c.CPU[k]
      if not arr then
        arr = {}
        c.CPU[k] = arr
      end
      table.insert(arr, val)
    end
  end

  -- 3. GPU timings
  local g = Engine.Debug.getGpuTimings()
  if type(g) == 'table' then
    for k, val in pairs(g) do
      local arr = c.GPU[k]
      if not arr then
        arr = {}
        c.GPU[k] = arr
      end
      table.insert(arr, val)
    end
  end

  -- 4. Console numbers
  local v = VariableRegistry
  local sm = tonumber(v.get("ShadowStats::activeMaps", "0")) or 0
  local su = tonumber(v.get("ShadowStats::updatedMaps", "0")) or 0
  local sp = tonumber(v.get("ShadowStats::polyCount", "0")) or 0
  local sd = tonumber(v.get("ShadowStats::drawCalls", "0")) or 0
  local ft = tonumber(v.get("Forest::totalCells", "0")) or 0
  local fcr = tonumber(v.get("Forest::cellsRendered", "0")) or 0
  local fir = tonumber(v.get("Forest::cellItemsRendered", "0")) or 0
  local gp = tonumber(v.get("GFXDeviceStatistics::polyCount", "0")) or 0
  local gd = tonumber(v.get("GFXDeviceStatistics::drawCalls", "0")) or 0

  table.insert(c.stats.ShadowActiveMaps, sm)
  table.insert(c.stats.ShadowUpdatedMaps, su)
  table.insert(c.stats.ShadowPolyCount, sp)
  table.insert(c.stats.ShadowDrawCalls, sd)
  table.insert(c.stats.ForestTotalCells, ft)
  table.insert(c.stats.ForestCellsRendered, fcr)
  table.insert(c.stats.ForestCellItemsRendered, fir)
  table.insert(c.stats.GFXPolyCount, gp)
  table.insert(c.stats.GFXDrawCalls, gd)
end

proxy:submitEventSinks({ sink }, {})
_G.bngEarthPerfCap = { proxy = proxy }
return 'recording_started'
"""

LUA_STOP_RECORDER = r"""
local ok = false
if _G.__bngPerfCapture then
  ok = jsonWriteFile("temp/bngperf_out.json", _G.__bngPerfCapture, false)
end
if _G.bngEarthPerfCap and _G.bngEarthPerfCap.proxy then
  pcall(function() _G.bngEarthPerfCap.proxy:destroy() end)
end
_G.bngEarthPerfCap = nil
_G.__bngPerfCapture = nil
if _G.__perfGraphToggledOn and togglePerformanceGraph then
  togglePerformanceGraph()
  _G.__perfGraphToggledOn = nil
end
if Engine.Debug and Engine.Debug.setGpuTimingOverlayVisible then
  Engine.Debug.setGpuTimingOverlayVisible(false)
end
if core_gamestate and core_gamestate.setGameState then
  pcall(core_gamestate.setGameState, 'freeroam', 'beamngEarth', 'freeroam')
end
if ActionMap and ActionMap.enableInputCommands then
  pcall(ActionMap.enableInputCommands, true)
end
return tostring(ok)
"""


def start_perfgraph_recording():
    """Ensure play state, toggle graph, and inject per-frame Lua recorder extension."""
    try:
        mcp.call("set_ui_state", {"route": "play"})
        mcp.call("resume_physics")
        mcp.call("run_lua", {"code": "if simTimeAuthority and simTimeAuthority.unpause then simTimeAuthority.unpause() end"})
        mcp.call("run_lua", {"code": "if not _G.__perfGraphToggledOn and togglePerformanceGraph then togglePerformanceGraph(); _G.__perfGraphToggledOn = true end"})
        mcp.call("run_lua", {"code": "if Engine.Debug and Engine.Debug.setGpuTimingOverlayVisible then Engine.Debug.setGpuTimingOverlayVisible(true) end"})
        res = mcp.call("run_lua", {"code": LUA_START_RECORDER})
        print(f"  [PerfRecorder] Started in-engine recording: {res.strip()}", flush=True)
    except Exception as e:
        print(f"  [PerfRecorder Warning] Failed to start recorder: {e}", flush=True)
    return time.time()


def stop_perfgraph_recording(out_path, user_dir=None, t_start=None):
    """Serialize Lua perf data to temp/bngperf_out.json, move to out_path, and check native perf captures."""
    user_dir = user_dir or get_user_dir()
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    try:
        res = mcp.call("run_lua", {"code": LUA_STOP_RECORDER})
        print(f"  [PerfRecorder] Stopped in-engine recording (written: {res.strip()})", flush=True)
    except Exception as e:
        print(f"  [PerfRecorder Warning] Failed to stop recorder: {e}", flush=True)

    temp_json = os.path.join(user_dir, "temp", "bngperf_out.json")
    if os.path.isfile(temp_json):
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
            shutil.move(temp_json, out_path)
            print(f"  [PerfRecorder] Saved per-frame telemetry to: {out_path}", flush=True)
        except Exception as e:
            print(f"  [PerfRecorder Warning] Failed to move {temp_json} to {out_path}: {e}", flush=True)
    else:
        print(f"  [PerfRecorder Warning] Expected {temp_json} not found.", flush=True)

    # Scan for any native PerfCapture_*.bngperfgraph.json created by BeamNG engine
    if t_start is not None and os.path.isdir(user_dir):
        try:
            for item in os.listdir(user_dir):
                if item.startswith("PerfCapture_") and item.endswith(".bngperfgraph.json"):
                    full_p = os.path.join(user_dir, item)
                    if os.path.isfile(full_p) and os.path.getmtime(full_p) >= t_start - 2.0:
                        dst = os.path.join(os.path.dirname(out_path), f"Native_{item}")
                        shutil.copy2(full_p, dst)
                        print(f"  [PerfRecorder] Copied native engine capture to: {dst}", flush=True)
        except Exception as e:
            print(f"  [PerfRecorder Warning] Native capture scan failed: {e}", flush=True)


# --- Statistical Computations -----------------------------------------------------------

def percentile(vals, p):
    """Compute exact percentile for a list of floats."""
    if not vals:
        return 0.0
    s = sorted(vals)
    k = (len(s) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return float(s[int(k)])
    return float(s[f] * (c - k) + s[c] * (k - f))


def analyze_bngperfgraph(perf_path):
    """Parse and calculate frame time percentiles, hitches, and subsystem timings from .bngperfgraph.json."""
    if not os.path.isfile(perf_path):
        return {}

    with open(perf_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    fps_raw = data.get("FPS", {}).get("FPS", [])
    fps_list = [float(x) for x in fps_raw if x and float(x) > 0]
    total_frames = len(fps_list)
    if total_frames == 0:
        return {}

    ft_list = [1000.0 / x for x in fps_list]
    ft_p50 = percentile(ft_list, 50)
    ft_p90 = percentile(ft_list, 90)
    ft_p95 = percentile(ft_list, 95)
    ft_p99 = percentile(ft_list, 99)
    ft_max = max(ft_list)
    ft_mean = sum(ft_list) / total_frames

    frames_under_16 = sum(1 for x in ft_list if x < 16.67)
    pct_under_16 = 100.0 * frames_under_16 / total_frames
    hitches_16 = sum(1 for x in ft_list if x > 16.67)
    hitches_33 = sum(1 for x in ft_list if x > 33.33)
    hitches_50 = sum(1 for x in ft_list if x > 50.0)

    fps_mean = sum(fps_list) / total_frames
    fps_min = min(fps_list)
    fps_max = max(fps_list)

    # Subsystem analysis
    cpu_data = data.get("CPU", {})
    subsystems = {}
    for name, arr in cpu_data.items():
        if isinstance(arr, list) and arr:
            num_arr = [float(x) for x in arr]
            subsystems[name] = {
                "mean": round(sum(num_arr) / len(num_arr), 2),
                "p50": round(percentile(num_arr, 50), 2),
                "p95": round(percentile(num_arr, 95), 2),
                "p99": round(percentile(num_arr, 99), 2),
                "max": round(max(num_arr), 2),
                "spikes_5ms": sum(1 for x in num_arr if x > 5.0),
                "spikes_10ms": sum(1 for x in num_arr if x > 10.0),
                "spikes_20ms": sum(1 for x in num_arr if x > 20.0),
            }

    gpu_data = data.get("GPU", {})
    gpu_subsystems = {}
    for name, arr in gpu_data.items():
        if isinstance(arr, list) and arr:
            num_arr = [float(x) for x in arr]
            gpu_subsystems[name] = {
                "mean": round(sum(num_arr) / len(num_arr), 2),
                "p50": round(percentile(num_arr, 50), 2),
                "p95": round(percentile(num_arr, 95), 2),
                "max": round(max(num_arr), 2),
            }

    return {
        "total_frames": total_frames,
        "fps_mean": round(fps_mean, 2),
        "fps_min": round(fps_min, 2),
        "fps_max": round(fps_max, 2),
        "ft_mean": round(ft_mean, 2),
        "ft_p50": round(ft_p50, 2),
        "ft_p90": round(ft_p90, 2),
        "ft_p95": round(ft_p95, 2),
        "ft_p99": round(ft_p99, 2),
        "ft_max": round(ft_max, 2),
        "pct_under_16_67ms": round(pct_under_16, 2),
        "hitches_16ms": hitches_16,
        "hitches_33ms": hitches_33,
        "hitches_50ms": hitches_50,
        "subsystems": subsystems,
        "gpu_subsystems": gpu_subsystems,
        "info": data.get("info", {})
    }


# --- Teleport Milestone Polling ---------------------------------------------------------

LUA_MILESTONES = r"""
local s = loadstring('return ' .. beamngEarth_core.status())() or {}
local rs = require('beamngEarth/roads').status() or {}
local f, t = s.far or {}, s.trees or {}
return jsonEncode({
  anchor = s.anchorId,
  farDone = f.done,
  farQueued = f.queued,
  live = f.live,
  roadTiles = rs.tiles,
  roadQ = rs.queued,
  walkQ = rs.walksQueued,
  buildings = rs.buildings,
  roads = rs.roads,
  treePending = t.pending,
  backlog = s.serverBacklog,
  status = s.status,
  net = s.net
})
"""


def poll_milestone_status():
    """Poll in-engine core and road status via MCP."""
    raw = mcp.call("run_lua", {"code": LUA_MILESTONES})
    return json.loads(raw)


def run_teleport_milestones(lat, lon, timeout=DEFAULT_TELEPORT_TIMEOUT):
    """Execute teleport and track exact build milestones until fully settled."""
    print(f"\n[Phase A: Cold Teleport & Milestones] Teleporting to ({lat:.6f}, {lon:.6f})...", flush=True)
    before = {}
    try:
        before = poll_milestone_status()
    except Exception:
        pass

    t0 = time.time()
    mcp.call("run_lua", {"code": f"return tostring(beamngEarth_core.teleport({lat}, {lon}, nil, {{record = false}}))"})

    marks = {}
    last_status = None
    while time.time() - t0 < timeout:
        time.sleep(0.5)
        try:
            s = poll_milestone_status()
        except Exception as e:
            print(f"  Milestone poll warning: {e}", file=sys.stderr)
            continue

        t = round(time.time() - t0, 1)
        moved = s.get("anchor") is not None and s.get("anchor") != before.get("anchor")

        done = {
            "anchor": moved,
            "far": moved and bool(s.get("farDone")) and not s.get("farQueued"),
            "roads": moved and (s.get("roadTiles") or 0) > 0 and not s.get("roadQ") and not s.get("walkQ"),
            "trees": moved and not s.get("treePending"),
            "backlog": moved and s.get("backlog") == 0,
        }
        for n in (9, 18, 36):
            done[f"roads{n}"] = moved and (s.get("roadTiles") or 0) >= n

        for k, v in done.items():
            if v and k not in marks:
                marks[k] = t
                print(f"  {t:6.1f}s | Milestone '{k}' achieved (roads: {s.get('roadTiles', 0)}, backlog: {s.get('backlog', 0)})", flush=True)

        last_status = s
        if all(done.get(k) for k in ("anchor", "far", "roads", "trees", "backlog")):
            # Wait for 5s of quiet for settled milestone
            quiet_until = time.time() + 5.0
            settled_ok = True
            while time.time() < quiet_until:
                time.sleep(0.5)
                s2 = poll_milestone_status()
                if s2.get("roadQ") or s2.get("walkQ") or s2.get("farQueued") or s2.get("treePending") or s2.get("backlog"):
                    quiet_until = time.time() + 5.0
                    settled_ok = False
                else:
                    settled_ok = True
            if settled_ok:
                settled_t = round(time.time() - t0 - 5.0, 1)
                marks["settled"] = settled_t
                print(f"  {settled_t:6.1f}s | Milestone 'settled' achieved (all subsystems completely idle).", flush=True)
                break

    return {"marks": marks, "last": last_status, "duration": round(time.time() - t0, 1)}


# --- Route Geometry & Spline Profile Planning -------------------------------------------

def fetch_unsimplified_route(start_lat, start_lon, top_lat, top_lon):
    """Fetch unsimplified OSRM route geometry via routing.url, falling back to routing.plan()."""
    try:
        u = routing.url(start_lat, start_lon, top_lat, top_lon)
        body = netfetch.default_fetcher().get(u, kind="demand").body
        data = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
        if data.get("code") == "Ok" and data.get("routes"):
            coords = data["routes"][0]["geometry"]["coordinates"]
            pts = [(float(c[1]), float(c[0])) for c in coords]
            if len(pts) >= 2:
                return pts
    except Exception as e:
        print(f"Warning: Direct unsimplified OSRM fetch failed ({e}), falling back to routing.plan()...", flush=True)

    plan = routing.plan(start_lat, start_lon, top_lat, top_lon)
    return plan.get("points", [])


def snap_anchor(lat_deg, lon_deg):
    """Exact match of geodesy.lua snapAnchor (0.01 deg grid)."""
    if abs(lat_deg) > 85.0:
        return lat_deg, lon_deg
    snap_deg = 0.01
    lat = math.floor(lat_deg / snap_deg + 0.5) * snap_deg
    c_lat = math.cos(math.radians(lat))
    k = 1
    while c_lat * k * 2 <= 1.0 + 1e-12 and k < 128:
        k *= 2
    step = snap_deg * k
    lon = math.floor(lon_deg / step + 0.5) * step
    if lon >= 180:
        lon -= 360
    elif lon < -180:
        lon += 360
    return float(f"{lat:.6f}"), float(f"{lon:.6f}")


def _geodetic_to_ecef(lat_deg, lon_deg, h=0.0):
    a = 6378137.0
    f = 1.0 / 298.257223563
    e2 = f * (2.0 - f)
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    sl, cl = math.sin(lat), math.cos(lat)
    n = a / math.sqrt(1.0 - e2 * sl * sl)
    r = (n + h) * cl
    return r * math.cos(lon), r * math.sin(lon), (n * (1.0 - e2) + h) * sl


def geodetic_to_enu(lat, lon, lat0, lon0):
    """Exact WGS84 ECEF -> ENU conversion matching geodesy.lua Anchor:geodeticToEnu."""
    ox, oy, oz = _geodetic_to_ecef(lat0, lon0, 0.0)
    px, py, pz = _geodetic_to_ecef(lat, lon, 0.0)
    dx, dy, dz = px - ox, py - oy, pz - oz
    la, lo = math.radians(lat0), math.radians(lon0)
    sl, cl = math.sin(la), math.cos(la)
    so, co = math.sin(lo), math.cos(lo)
    e = -so * dx + co * dy
    n = -sl * co * dx - sl * so * dy + cl * dz
    return e, n


def plan_corridor_profile(start_lat, start_lon, top_lat, top_lon, target_km, speed_mps, step_m=2.0):
    """Generate dense centripetal Catmull-Rom spline points tracking the centerline."""
    print(f"Planning route from ({start_lat:.6f}, {start_lon:.6f}) to ({top_lat:.6f}, {top_lon:.6f})...", flush=True)
    route_pts = fetch_unsimplified_route(start_lat, start_lon, top_lat, top_lon)
    if not route_pts or len(route_pts) < 2:
        raise RuntimeError(f"Routing failed or returned insufficient points ({len(route_pts)} points)")

    lat0, lon0 = snap_anchor(start_lat, start_lon)
    try:
        raw_st = mcp.call("run_lua", {"code": "local s = loadstring('return ' .. beamngEarth_core.status())() return s and s.anchor and string.format('%.6f,%.6f', s.anchor.lat, s.anchor.lon) or ''"}).strip()
        if "," in raw_st:
            p_la, p_lo = raw_st.split(",")
            lat0, lon0 = float(p_la), float(p_lo)
    except Exception:
        pass
    print(f"  Using anchor origin: ({lat0:.6f}, {lon0:.6f})", flush=True)
    enu_pts = [geodetic_to_enu(la, lo, lat0, lon0) for la, lo in route_pts]

    # Clean near duplicates (< 0.5m)
    clean = [enu_pts[0]]
    for p in enu_pts[1:]:
        if math.hypot(p[0] - clean[-1][0], p[1] - clean[-1][1]) > 0.5:
            clean.append(p)

    n = len(clean)
    p_pre = (2 * clean[0][0] - clean[1][0], 2 * clean[0][1] - clean[1][1])
    p_post = (2 * clean[-1][0] - clean[-2][0], 2 * clean[-1][1] - clean[-2][1])
    ext = [p_pre] + clean + [p_post]

    dense_pts = []
    for i in range(1, n):
        p0, p1, p2, p3 = ext[i - 1], ext[i], ext[i + 1], ext[i + 2]
        chord = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
        num_sub = max(2, int(math.ceil(chord / 0.5)))

        t0 = 0.0
        t1 = t0 + math.hypot(p1[0] - p0[0], p1[1] - p0[1]) ** 0.5
        t2 = t1 + math.hypot(p2[0] - p1[0], p2[1] - p1[1]) ** 0.5
        t3 = t2 + math.hypot(p3[0] - p2[0], p3[1] - p2[1]) ** 0.5

        for k in range(num_sub if i < n - 1 else num_sub + 1):
            t = t1 + (t2 - t1) * (k / num_sub)
            a1_x = (t1 - t) / (t1 - t0) * p0[0] + (t - t0) / (t1 - t0) * p1[0]
            a1_y = (t1 - t) / (t1 - t0) * p0[1] + (t - t0) / (t1 - t0) * p1[1]
            a2_x = (t2 - t) / (t2 - t1) * p1[0] + (t - t1) / (t2 - t1) * p2[0]
            a2_y = (t2 - t) / (t2 - t1) * p1[1] + (t - t1) / (t2 - t1) * p2[1]
            a3_x = (t3 - t) / (t3 - t2) * p2[0] + (t - t2) / (t3 - t2) * p3[0]
            a3_y = (t3 - t) / (t3 - t2) * p2[1] + (t - t2) / (t3 - t2) * p3[1]

            b1_x = (t2 - t) / (t2 - t0) * a1_x + (t - t0) / (t2 - t0) * a2_x
            b1_y = (t2 - t) / (t2 - t0) * a1_y + (t - t0) / (t2 - t0) * a2_y
            b2_x = (t3 - t) / (t3 - t1) * a2_x + (t - t1) / (t3 - t1) * a3_x
            b2_y = (t3 - t) / (t3 - t1) * a2_y + (t - t1) / (t3 - t1) * a3_y

            cx = (t2 - t) / (t2 - t1) * b1_x + (t - t1) / (t2 - t1) * b2_x
            cy = (t2 - t) / (t2 - t1) * b1_y + (t - t1) / (t2 - t1) * b2_y
            dense_pts.append((cx, cy))

    cum_s = [0.0]
    for (x1, y1), (x2, y2) in zip(dense_pts, dense_pts[1:]):
        cum_s.append(cum_s[-1] + math.hypot(x2 - x1, y2 - y1))

    total_route_m = cum_s[-1]
    limit_m = min(total_route_m, target_km * 1000.0) if target_km else total_route_m
    print(f"Full route: {total_route_m / 1000.0:.2f} km ({len(route_pts)} raw OSM nodes). Target drive: {limit_m / 1000.0:.2f} km.", flush=True)

    samples = []
    curr_s = 0.0
    idx = 0
    while curr_s <= limit_m:
        while idx < len(cum_s) - 2 and cum_s[idx + 1] < curr_s:
            idx += 1
        s0, s1 = cum_s[idx], cum_s[idx + 1]
        x0, y0 = dense_pts[idx]
        x1, y1 = dense_pts[idx + 1]
        f = (curr_s - s0) / max(1e-6, s1 - s0)
        rx = x0 + f * (x1 - x0)
        ry = y0 + f * (y1 - y0)
        i_fwd = min(len(dense_pts) - 1, idx + 4)
        i_bwd = max(0, idx - 4)
        tx = dense_pts[i_fwd][0] - dense_pts[i_bwd][0]
        ty = dense_pts[i_fwd][1] - dense_pts[i_bwd][1]
        heading = math.degrees(math.atan2(tx, ty)) % 360.0
        samples.append({
            "s": round(curr_s, 2),
            "speed": float(speed_mps),
            "heading": round(heading, 2),
            "x": round(rx, 3),
            "y": round(ry, 3)
        })
        curr_s += step_m

    if samples[-1]["s"] < limit_m:
        samples.append({
            "s": round(limit_m, 2),
            "speed": float(speed_mps),
            "heading": samples[-1]["heading"],
            "x": dense_pts[-1][0],
            "y": dense_pts[-1][1]
        })

    return {
        "points": samples,
        "km": round(limit_m / 1000.0, 3),
        "lat0": lat0,
        "lon0": lon0,
        "enu_start": (samples[0]["x"], samples[0]["y"]),
        "initial_heading": samples[0]["heading"]
    }


def snap_vehicle_to_road(enu_x, enu_y, heading):
    """Snap active test vehicle directly onto road start heading using spawn.safeTeleport."""
    snap_lua = f"""
local veh = getPlayerVehicle(0)
if not veh then return 'no vehicle' end
local z = be:getSurfaceHeightBelow(vec3({enu_x}, {enu_y}, 2500))
if not z or z < -1000 then z = veh:getPosition().z end
local dir = vec3(math.sin(math.rad({heading})), math.cos(math.rad({heading})), 0)
spawn.safeTeleport(veh, vec3({enu_x}, {enu_y}, z + 0.5), quatFromDir(dir:normalized(), vec3(0, 0, 1)), nil, nil, false, nil, true)
veh:applyClusterVelocityScaleAdd(-1, 0, 0, 0, 0)
return string.format('snapped to (%.2f, %.2f, %.2f) heading %.1f', {enu_x}, {enu_y}, z + 0.5, {heading})
"""
    res = mcp.call("run_lua", {"code": snap_lua})
    print(f"  [Vehicle Snap] {res.strip()}", flush=True)
    time.sleep(2.0)


# --- Report Generation ------------------------------------------------------------------

def write_run_report(run_dir, label, summary, milestones, perf_stats, km, speed_mps):
    """Generate comprehensive executive Markdown report and combined summary.json."""
    os.makedirs(run_dir, exist_ok=True)

    # 1. Combined summary JSON
    combined = {
        "label": label,
        "timestamp": datetime.datetime.now().isoformat(),
        "km": km,
        "nominalSpeed": speed_mps,
        "drive_summary": summary,
        "teleport_milestones": milestones.get("marks", {}),
        "perfgraph_stats": perf_stats,
        "pass": summary.get("pass", False)
    }
    summary_path = os.path.join(run_dir, "summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(combined, f, indent=2)

    # 2. Markdown report
    md_lines = [
        f"# BeamNG.Earth Cold Benchmark Report: {label}",
        f"",
        f"- **Date / Time**: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- **Test Corridor**: Mont Ventoux D974 (START to TOP) (3.0 km Continuous Spline Drive)",
        f"- **Nominal Speed**: {speed_mps:.1f} m/s ({speed_mps * 3.6:.1f} km/h | {speed_mps * 2.23694:.1f} mph)",
        f"- **Effective Speed**: {summary.get('effectiveSpeed', 0.0):.2f} m/s ({summary.get('effectiveRatio', 0.0) * 100.0:.1f}% nominal)",
        f"- **Overall Verdict**: **{'PASS' if summary.get('pass') else 'FAIL'}**",
        f"",
        f"---",
        f"",
        f"## 1. Phase A: Cold Teleport & World-Build Milestones",
        f"",
        f"| Milestone | Time (s) | Description |",
        f"| :--- | :--- | :--- |",
    ]

    milestone_desc = {
        "anchor": "World anchor initialized and vehicle frame placed",
        "roads9": "First 9 near-field road tiles generated",
        "roads18": "First 18 road tiles generated",
        "roads36": "36 road tiles generated",
        "roads": "All initial road and bridge mesh queues cleared",
        "far": "Far-field LOD terrain tiles fully loaded",
        "trees": "Vegetation / forest placement queue completed",
        "backlog": "Companion server processing backlog completely zeroed",
        "settled": "Full map quiet state sustained for 5.0 seconds"
    }

    marks = milestones.get("marks", {})
    for m_name in ("anchor", "roads9", "roads18", "roads36", "roads", "far", "trees", "backlog", "settled"):
        val = marks.get(m_name)
        val_str = f"{val:.1f}s" if val is not None else "N/A"
        desc = milestone_desc.get(m_name, "")
        md_lines.append(f"| `{m_name}` | **{val_str}** | {desc} |")

    md_lines.extend([
        f"",
        f"---",
        f"",
        f"## 2. Phase B: 3.0 km Corridor Spline Drive Performance",
        f"",
        f"### Frame Rate & Frame Time Latencies",
        f"",
        f"| Metric | In-Engine .bngperfgraph | Drive Harness Steady | Budget / Target |",
        f"| :--- | :--- | :--- | :--- |",
        f"| **Mean Frame Rate** | **{perf_stats.get('fps_mean', 0.0):.1f} FPS** | {summary.get('fpsMean', 0.0):.1f} FPS | ≥ 60.0 FPS |",
        f"| **Frame Time (p50)** | **{perf_stats.get('ft_p50', 0.0):.2f} ms** | {summary.get('p50', 0.0):.2f} ms | < 16.67 ms |",
        f"| **Frame Time (p90)** | **{perf_stats.get('ft_p90', 0.0):.2f} ms** | N/A | < 16.67 ms |",
        f"| **Frame Time (p95)** | **{perf_stats.get('ft_p95', 0.0):.2f} ms** | {summary.get('p95', 0.0):.2f} ms | **< 16.67 ms (CRITICAL)** |",
        f"| **Frame Time (p99)** | **{perf_stats.get('ft_p99', 0.0):.2f} ms** | {summary.get('p99', 0.0):.2f} ms | < 25.00 ms |",
        f"| **Maximum Hitch** | **{perf_stats.get('ft_max', 0.0):.2f} ms** | {summary.get('maxMs', 0.0):.2f} ms | < 50.00 ms |",
        f"| **60+ FPS Compliance** | **{perf_stats.get('pct_under_16_67ms', 0.0):.1f}%** | N/A | **≥ 95.0%** |",
        f"| **Hitches > 16.67 ms** | {perf_stats.get('hitches_16ms', 0)} frames | {summary.get('over50', 0)} (>50ms) | - |",
        f"| **Hitches > 33.33 ms** | {perf_stats.get('hitches_33ms', 0)} frames | - | 0 |",
        f"| **Severe Hitches > 50 ms**| {perf_stats.get('hitches_50ms', 0)} frames | {summary.get('over50Steady', 0)} | 0 |",
        f"",
        f"---",
        f"",
        f"## 3. Subsystem Timing Breakdown (CPU & GPU Passes)",
        f"",
        f"| Subsystem | Mean (ms) | p50 (ms) | p95 (ms) | Max Spike (ms) | Spikes > 10ms |",
        f"| :--- | :--- | :--- | :--- | :--- | :--- |",
    ])

    subsystems = perf_stats.get("subsystems", {})
    key_subsystems = ("RenderShadows", "Lua", "LuaGC", "FileWatchers", "Ticking", "PhysicsAndVLUA", "Render", "PreRender", "Audio")
    for k in key_subsystems:
        if k in subsystems:
            s = subsystems[k]
            md_lines.append(f"| `{k}` | {s['mean']:.2f} ms | {s['p50']:.2f} ms | **{s['p95']:.2f} ms** | {s['max']:.2f} ms | {s['spikes_10ms']} |")

    md_lines.extend([
        f"",
        f"---",
        f"",
        f"## 4. Vehicle Physics & Streaming Pipeline Health",
        f"",
        f"- **Physics Falls**: {summary.get('falls', 0)} falls",
        f"- **Vehicle Resets**: {summary.get('resets', 0)} resets",
        f"- **Heading Resyncs**: {summary.get('headingResyncs', 0)} resyncs",
        f"- **Rebases Handled**: {summary.get('rebases', 0)} rebases",
        f"- **Peak Server Backlog**: {summary.get('backlogMax', 0)} tiles",
        f"- **Tile Level Coverage**: `{json.dumps(summary.get('underLevels', {}))}`",
        f"",
    ])

    report_path = os.path.join(run_dir, "report.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines))
    print(f"  [Report] Executive report written to: {report_path}", flush=True)


# --- Compare Workflow -------------------------------------------------------------------

def compare_runs(run_a_dir, run_b_dir, out_dir=None):
    """Compare two cold benchmark runs with side-by-side delta tables and KEEP/REJECT verdict."""
    run_a_dir = os.path.abspath(run_a_dir)
    run_b_dir = os.path.abspath(run_b_dir)

    a_sum_path = os.path.join(run_a_dir, "summary.json")
    b_sum_path = os.path.join(run_b_dir, "summary.json")

    if not os.path.isfile(a_sum_path):
        raise FileNotFoundError(f"Run A summary not found: {a_sum_path}")
    if not os.path.isfile(b_sum_path):
        raise FileNotFoundError(f"Run B summary not found: {b_sum_path}")

    with open(a_sum_path, "r", encoding="utf-8") as f:
        a_data = json.load(f)
    with open(b_sum_path, "r", encoding="utf-8") as f:
        b_data = json.load(f)

    a_label = a_data.get("label", "Run_A")
    b_label = b_data.get("label", "Run_B")

    a_perf = a_data.get("perfgraph_stats", {})
    b_perf = b_data.get("perfgraph_stats", {})
    a_marks = a_data.get("teleport_milestones", {})
    b_marks = b_data.get("teleport_milestones", {})
    a_drive = a_data.get("drive_summary", {})
    b_drive = b_data.get("drive_summary", {})

    # Evaluate verdict criteria
    reasons = []
    verdict = "KEEP"

    # 1. Physics stability
    b_falls = b_drive.get("falls", 0)
    b_resets = b_drive.get("resets", 0)
    if b_falls > 0:
        verdict = "REJECT"
        reasons.append(f"Candidate encountered {b_falls} physics falls.")
    if b_resets > 0:
        verdict = "REJECT"
        reasons.append(f"Candidate encountered {b_resets} vehicle resets.")

    # 2. Driving speed ratio
    b_eff_ratio = b_drive.get("effectiveRatio", 0.0)
    if b_eff_ratio < 0.95:
        verdict = "REJECT"
        reasons.append(f"Candidate effective speed ratio {b_eff_ratio*100:.1f}% is below 95% threshold.")

    # 3. p95 frame latency regression (> +2.0% increase)
    a_p95 = a_perf.get("ft_p95", 0.0)
    b_p95 = b_perf.get("ft_p95", 0.0)
    if a_p95 > 0 and b_p95 > 0:
        p95_pct = 100.0 * (b_p95 - a_p95) / a_p95
        if p95_pct > 2.0:
            verdict = "REJECT"
            reasons.append(f"p95 frame latency regressed by +{p95_pct:.2f}% ({a_p95:.2f}ms -> {b_p95:.2f}ms).")

    # 4. 60+ FPS compliance regression (> -2.0% decrease)
    a_c60 = a_perf.get("pct_under_16_67ms", 0.0)
    b_c60 = b_perf.get("pct_under_16_67ms", 0.0)
    if a_c60 > 0 and (a_c60 - b_c60) > 2.0:
        verdict = "REJECT"
        reasons.append(f"60+ FPS compliance degraded from {a_c60:.1f}% to {b_c60:.1f}%.")

    # Format Markdown Comparison Report
    lines = [
        f"# BeamNG.Earth A/B Benchmark Comparison Report",
        f"",
        f"- **Baseline (Run A)**: `{a_label}` ({run_a_dir})",
        f"- **Candidate (Run B)**: `{b_label}` ({run_b_dir})",
        f"- **Date / Time**: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- **VERDICT**: **{verdict}**",
        f"",
    ]
    if reasons:
        lines.append("### Rejection Reasons:")
        for r in reasons:
            lines.append(f"- ❌ {r}")
        lines.append("")
    else:
        lines.append("### Justification:")
        lines.append(f"- ✅ Candidate meets all stability, frame latency budget, and milestone requirements.")
        lines.append("")

    lines.extend([
        f"---",
        f"",
        f"## 1. Frame Latencies & Frame Rate Comparison",
        f"",
        f"| Metric | Baseline (Run A) | Candidate (Run B) | Delta | % Change |",
        f"| :--- | :--- | :--- | :--- | :--- |",
    ])

    def ft_row(name, a_val, b_val, lower_is_better=True):
        if a_val is None or b_val is None:
            return f"| {name} | N/A | N/A | N/A | N/A |"
        delta = b_val - a_val
        pct = (100.0 * delta / a_val) if a_val != 0 else 0.0
        good = (delta <= 0) if lower_is_better else (delta >= 0)
        sym = "🟢" if good or abs(pct) < 1.0 else "🔴"
        return f"| **{name}** | {a_val:.2f} | {b_val:.2f} | {delta:+.2f} | {sym} {pct:+.2f}% |"

    lines.append(ft_row("Mean FPS", a_perf.get("fps_mean", 0.0), b_perf.get("fps_mean", 0.0), lower_is_better=False))
    lines.append(ft_row("Frame Time (p50) ms", a_perf.get("ft_p50", 0.0), b_perf.get("ft_p50", 0.0)))
    lines.append(ft_row("Frame Time (p95) ms", a_perf.get("ft_p95", 0.0), b_perf.get("ft_p95", 0.0)))
    lines.append(ft_row("Frame Time (p99) ms", a_perf.get("ft_p99", 0.0), b_perf.get("ft_p99", 0.0)))
    lines.append(ft_row("Max Hitch ms", a_perf.get("ft_max", 0.0), b_perf.get("ft_max", 0.0)))
    lines.append(ft_row("60+ FPS Compliance %", a_perf.get("pct_under_16_67ms", 0.0), b_perf.get("pct_under_16_67ms", 0.0), lower_is_better=False))

    lines.extend([
        f"",
        f"---",
        f"",
        f"## 2. Teleport & World-Build Milestones Comparison",
        f"",
        f"| Milestone | Baseline (Run A) | Candidate (Run B) | Delta (s) | % Change |",
        f"| :--- | :--- | :--- | :--- | :--- |",
    ])

    for m in ("anchor", "roads9", "roads18", "roads36", "roads", "far", "trees", "backlog", "settled"):
        va = a_marks.get(m)
        vb = b_marks.get(m)
        if va is not None and vb is not None:
            delta = vb - va
            pct = 100.0 * delta / va if va > 0 else 0.0
            sym = "🟢" if delta <= 0 else "🔴"
            lines.append(f"| `{m}` | {va:.1f}s | {vb:.1f}s | {delta:+.1f}s | {sym} {pct:+.1f}% |")
        else:
            lines.append(f"| `{m}` | {f'{va:.1f}s' if va else 'N/A'} | {f'{vb:.1f}s' if vb else 'N/A'} | N/A | N/A |")

    lines.extend([
        f"",
        f"---",
        f"",
        f"## 3. Subsystem Breakdown Comparison (p95 ms)",
        f"",
        f"| Subsystem | Baseline p95 | Candidate p95 | Delta | % Change |",
        f"| :--- | :--- | :--- | :--- | :--- |",
    ])

    a_sub = a_perf.get("subsystems", {})
    b_sub = b_perf.get("subsystems", {})
    for k in ("RenderShadows", "Lua", "LuaGC", "FileWatchers", "Ticking", "PhysicsAndVLUA", "Render"):
        p_a = a_sub.get(k, {}).get("p95")
        p_b = b_sub.get(k, {}).get("p95")
        if p_a is not None and p_b is not None:
            delta = p_b - p_a
            pct = 100.0 * delta / p_a if p_a > 0 else 0.0
            sym = "🟢" if delta <= 0 else "🔴"
            lines.append(f"| `{k}` | {p_a:.2f} ms | {p_b:.2f} ms | {delta:+.2f} ms | {sym} {pct:+.1f}% |")

    report_text = "\n".join(lines)
    print("\n" + "=" * 78)
    print(f"  A/B BENCHMARK COMPARISON VERDICT: {verdict}")
    print("=" * 78)
    print(f"  Baseline:  {a_label} ({run_a_dir})")
    print(f"  Candidate: {b_label} ({run_b_dir})")
    print("-" * 78)
    print(f"  p95 Frame Time:  {a_perf.get('ft_p95', 0.0):.2f} ms -> {b_perf.get('ft_p95', 0.0):.2f} ms ({b_perf.get('ft_p95', 0.0) - a_perf.get('ft_p95', 0.0):+.2f} ms)")
    print(f"  60+ FPS Compl:   {a_perf.get('pct_under_16_67ms', 0.0):.1f}% -> {b_perf.get('pct_under_16_67ms', 0.0):.1f}%")
    print(f"  Settled Build:   {a_marks.get('settled', 0.0):.1f}s -> {b_marks.get('settled', 0.0):.1f}s")
    print(f"  Falls / Resets:  {b_falls} falls / {b_resets} resets")
    print("-" * 78)
    print(f"  FINAL VERDICT:   {verdict}")
    if reasons:
        for r in reasons:
            print(f"    - {r}")
    print("=" * 78 + "\n")

    out_dir = out_dir or os.path.join(run_b_dir, f"comparison_vs_{a_label}")
    os.makedirs(out_dir, exist_ok=True)
    out_md = os.path.join(out_dir, "ab_compare_report.md")
    with open(out_md, "w", encoding="utf-8") as f:
        f.write(report_text)

    out_json = os.path.join(out_dir, "ab_compare_summary.json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({
            "verdict": verdict,
            "reasons": reasons,
            "baseline": a_label,
            "candidate": b_label,
            "baseline_dir": run_a_dir,
            "candidate_dir": run_b_dir,
            "baseline_perf": a_perf,
            "candidate_perf": b_perf,
            "baseline_milestones": a_marks,
            "candidate_milestones": b_marks
        }, f, indent=2)

    print(f"Saved comparison report to: {out_md}", flush=True)
    return 0 if verdict == "KEEP" else 1


# --- Run Workflow -----------------------------------------------------------------------

def run_bench(args):
    """Execute complete cold benchmark workflow (Steps 1 through 7)."""
    t_run_start = time.time()
    user_dir = get_user_dir()
    cache_dir = get_cache_dir()
    seed_dir = os.path.abspath(args.seed_dir) if args.seed_dir else DEFAULT_SEED_DIR
    seed_existed = os.path.isdir(seed_dir) and (
        os.path.isdir(os.path.join(seed_dir, "dem")) or os.path.isdir(os.path.join(seed_dir, "mapdata"))
    )

    # Resolve speed
    if args.mph is not None:
        speed_mps = args.mph * 0.44704
    elif args.kmh is not None:
        speed_mps = args.kmh / 3.6
    elif args.speed is not None:
        speed_mps = args.speed
    else:
        speed_mps = DEFAULT_SPEED

    # Resolve coordinates
    start_lat, start_lon = DEFAULT_START
    top_lat, top_lon = DEFAULT_TOP
    if args.start:
        parts = [float(x.strip()) for x in args.start.split(",")]
        start_lat, start_lon = parts[0], parts[1]
    if args.top:
        parts = [float(x.strip()) for x in args.top.split(",")]
        top_lat, top_lon = parts[0], parts[1]

    # Resolve output directory
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    label = args.label or f"cold_{timestamp}"
    run_dir = os.path.abspath(args.out) if args.out else os.path.join(ROOT, "runs", f"{label}_{timestamp}")
    os.makedirs(run_dir, exist_ok=True)

    print("\n" + "=" * 78)
    print(f"  BEAMNG.EARTH COLD-CACHE BENCHMARK RUN: {label}")
    print("=" * 78)
    print(f"  Output Directory:    {run_dir}")
    print(f"  Corridor Start:      ({start_lat:.6f}, {start_lon:.6f})")
    print(f"  Corridor End:        ({top_lat:.6f}, {top_lon:.6f})")
    print(f"  Drive Target:        {args.km:.2f} km @ {speed_mps:.1f} m/s ({speed_mps * 3.6:.1f} km/h)")
    print(f"  Cold Mode:           {args.cold_mode}")
    print(f"  Seed Directory:      {seed_dir} (existed: {seed_existed})")
    print("=" * 78 + "\n")

    # Step 1: Purge Caches
    print("STATUS_UPDATE: Purging caches (10%)", flush=True)
    purge_caches(cold_mode=args.cold_mode, seed_dir=seed_dir, keep_server=args.keep_server, restart_server=args.restart_server)

    if args.dry_run:
        print("[DRY RUN] Cache purged. Skipping live in-engine test.")
        return 0

    # Step 2: Mod Sync
    print("STATUS_UPDATE: Syncing mod via install.py (25%)", flush=True)
    install_cmd = [sys.executable, os.path.join(ROOT, "install.py"), "--no-venv", "--yes"]
    subprocess.check_call(install_cmd)

    # Step 3: Ensure BeamNG is running, level is beamng_earth, reset extensions
    print("STATUS_UPDATE: Verifying BeamNG level and cold-resetting Lua VM (35%)", flush=True)
    try:
        lvl = mcp.call("run_lua", {"code": "return tostring(getCurrentLevelIdentifier())"}).strip()
        print(f"  Active BeamNG level: '{lvl}'", flush=True)
        if lvl != "beamng_earth":
            print(f"  Warning: Engine level is '{lvl}', expected 'beamng_earth'.", flush=True)
        mcp.quiet_ui()
        mcp.call("run_lua", {"code": (
            "if simTimeAuthority and simTimeAuthority.pause then pcall(simTimeAuthority.pause, false) end; "
            "if bullettime and bullettime.pause then pcall(bullettime.pause, false) end; "
            "extensions.unload('beamngEarth_settings'); extensions.unload('beamngEarth_core'); "
            "for k in pairs(package.loaded) do "
            "  if type(k) == 'string' and k:find('beamngEarth', 1, true) then package.loaded[k] = nil end "
            "end; "
            "extensions.load('beamngEarth_core'); extensions.load('beamngEarth_settings'); "
            "pcall(function() require('beamngEarth/trees').adoptExistingForests() end); "
            "return 'cold_reset'"
        )})
        time.sleep(2.5)
    except Exception as e:
        raise RuntimeError(f"Failed to communicate with BeamNG via MCP: {e}") from e

    # Step 4: Companion server lifecycle
    print("STATUS_UPDATE: Ensuring companion server earthd is running (45%)", flush=True)
    server_proc = None
    server_log_f = None
    if not is_server_listening(SERVER_PORT) or args.restart_server:
        earthd_log = os.path.join(run_dir, "earthd.log")
        server_log_f = open(earthd_log, "w", encoding="utf-8")
        if getattr(args, "appimage", None):
            server_cmd = [
                os.path.abspath(args.appimage),
                "--appimage-extract-and-run",
                "--server",
                "--user-dir", user_dir,
            ]
        else:
            server_cmd = [
                sys.executable,
                os.path.join(ROOT, "server", "earthd.py"),
                "--user-dir", user_dir,
            ]
        server_cmd += [
            "--deshadow", "full",
            "--near-visual", "detail",
            "--airports", "on",
            "--sea", "on",
            "--trees", "on",
            "--buildings", "on",
            "--gas-stations", "on",
            "--status-interval", "1"
        ]
        if getattr(args, "peers", None):
            server_cmd += ["--peers", args.peers]
        print(f"  Starting background server -> {earthd_log}...", flush=True)
        popen_kw = {"stdout": server_log_f, "stderr": subprocess.STDOUT}
        if os.name != "nt":
            popen_kw["start_new_session"] = True
        server_proc = subprocess.Popen(server_cmd, **popen_kw)

        # Wait for port 47613
        t_wait = time.time()
        while time.time() - t_wait < 30.0:
            if is_server_listening(SERVER_PORT):
                print(f"  Server listening on port {SERVER_PORT}.", flush=True)
                break
            time.sleep(0.5)
        else:
            raise RuntimeError(f"Timed out waiting for earthd to bind port {SERVER_PORT}")

    # Wait for beamngEarth_core to connect
    t_wait_net = time.time()
    while time.time() - t_wait_net < 30.0:
        try:
            st = poll_milestone_status()
            if st.get("net") == "connected":
                print("  beamngEarth_core is connected to server!", flush=True)
                break
        except Exception:
            pass
        time.sleep(0.5)

    try:
        # Step 5: Phase A - Cold Teleport & Milestones
        print("STATUS_UPDATE: Phase A - Cold teleport and world build milestones (55%)", flush=True)
        t_tele_start = start_perfgraph_recording()
        milestones = run_teleport_milestones(start_lat, start_lon, timeout=args.teleport_timeout)
        tele_perf_path = os.path.join(run_dir, f"PerfCapture_{label}_teleport.bngperfgraph.json")
        stop_perfgraph_recording(tele_perf_path, user_dir=user_dir, t_start=t_tele_start)

        # Save milestones JSON
        milestones_path = os.path.join(run_dir, "teleport_milestones.json")
        with open(milestones_path, "w", encoding="utf-8") as f:
            json.dump(milestones, f, indent=2)

        # Step 6: Phase B - 3.0 km Corridor Spline Drive
        print("STATUS_UPDATE: Phase B - 3.0 km corridor spline drive (75%)", flush=True)
        profile_data = plan_corridor_profile(start_lat, start_lon, top_lat, top_lon, args.km, speed_mps)
        effective_km = profile_data["km"]

        profile_path = os.path.join(run_dir, "road_profile.json")
        with open(profile_path, "w", encoding="utf-8") as f:
            json.dump(profile_data, f, indent=1)

        # Snap vehicle to start of road
        snap_vehicle_to_road(profile_data["enu_start"][0], profile_data["enu_start"][1], profile_data["initial_heading"])
        ensure_spline_driver()

        # Start drive perf recording
        t_drive_start = start_perfgraph_recording()

        drive_args = argparse.Namespace(
            out=run_dir,
            profile=profile_path,
            speed=None,
            heading=None,
            km=effective_km,
            label=f"{label}_drive",
            poll=1.0,
            shots=0.0,
            max_s=7200.0,
            exact_level=None,
            use_player_vehicle=args.use_player_vehicle,
            step_mode="ge"
        )

        polls = drive.run_ge(drive_args, profile_data)
        drive_perf_path = os.path.join(run_dir, f"PerfCapture_{label}_drive.bngperfgraph.json")
        stop_perfgraph_recording(drive_perf_path, user_dir=user_dir, t_start=t_drive_start)

        drive_summary = drive.summarize(polls, profile_data, label=drive_args.label, visual=False)
        perf_stats = analyze_bngperfgraph(drive_perf_path)
        moving = [r for r in polls if (r.get("drive") or {}).get("s", 0) > 0]
        if moving:
            idx0 = polls.index(moving[0])
            t_start_m = polls[idx0 - 1]["t"] if idx0 > 0 else 0.0
            t_end_m = moving[-1]["t"]
            dist_m = moving[-1]["drive"]["s"]
            active_wall = max(1e-3, t_end_m - t_start_m)
            eff_speed = round(dist_m / active_wall, 2)
            eff_ratio = round(eff_speed / speed_mps, 3)
            drive_resets = max(0, (moving[-1]["drive"].get("resets", 0) or 0) - (moving[0]["drive"].get("resets", 0) or 0))
            drive_summary["rawWallS"] = drive_summary.get("wallS")
            drive_summary["wallS"] = round(active_wall, 2)
            drive_summary["effectiveSpeed"] = eff_speed
            drive_summary["effectiveRatio"] = eff_ratio
            drive_summary["spawnResets"] = drive_summary.get("resets", 0)
            drive_summary["resets"] = drive_resets
            drive_summary["pass"] = (
                drive_summary.get("falls", 0) == 0
                and drive_resets == 0
                and eff_ratio >= 0.95
                and perf_stats.get("ft_p95", 99.0) < 16.67
                and perf_stats.get("pct_under_16_67ms", 0.0) >= 95.0
            )

        # Write run report and combined summary
        print("STATUS_UPDATE: Generating executive performance report (90%)", flush=True)
        write_run_report(run_dir, label, drive_summary, milestones, perf_stats, effective_km, speed_mps)

    finally:
        # Step 7: Clean shutdown of earthd if we started it
        if server_proc:
            print("Stopping background earthd.py subprocess...", flush=True)
            server_proc.terminate()
            try:
                server_proc.wait(timeout=3.0)
            except Exception:
                server_proc.kill()
        if server_log_f:
            server_log_f.close()

    # Save seed snapshot if cold-mode is build-and-stream and seed didn't exist before
    if args.cold_mode == "build-and-stream" and not seed_existed:
        print(f"Auto-saving new seed snapshot into {seed_dir}...", flush=True)
        save_seed(seed_dir)

    print(f"\nSTATUS_UPDATE: Benchmark completed in {time.time() - t_run_start:.1f}s (100%)", flush=True)
    return 0 if drive_summary.get("pass") else 1


# --- CLI Main ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Unified Cold-Cache Build-and-Stream Benchmark & A/B Harness (v1.7.0-beta)",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(dest="subcommand", help="Subcommand to execute")

    # 1. purge
    p_purge = subparsers.add_parser("purge", help="Purge runtime caches and reset network cache")
    p_purge.add_argument("--cold-mode", choices=("build-and-stream", "full-network"), default="build-and-stream",
                         help="build-and-stream (re-seed raw network data) or full-network (wipe everything)")
    p_purge.add_argument("--seed-dir", default=DEFAULT_SEED_DIR,
                         help=f"Seed snapshot directory (default: {DEFAULT_SEED_DIR})")
    p_purge.add_argument("--keep-server", action="store_true",
                         help="Do not terminate running companion server on port 47613")
    p_purge.add_argument("--restart-server", action="store_true",
                         help="Force stop/restart companion server on port 47613")

    # 2. seed
    p_seed = subparsers.add_parser("seed", help="Save current raw network cache as pristine seed snapshot")
    p_seed.add_argument("--seed-dir", default=DEFAULT_SEED_DIR,
                        help=f"Seed snapshot directory destination (default: {DEFAULT_SEED_DIR})")

    # 3. run
    p_run = subparsers.add_parser("run", help="Execute complete cold teleport and 3.0 km drive benchmark")
    p_run.add_argument("--label", required=True,
                       help="Descriptive label for this benchmark run (e.g. baseline_v170, patch_branch)")
    p_run.add_argument("--from", "--start", dest="start", default=None,
                       help=f"Starting GPS coordinates 'lat,lon' (default: {DEFAULT_START[0]},{DEFAULT_START[1]})")
    p_run.add_argument("--to", "--top", dest="top", default=None,
                       help=f"Ending GPS coordinates 'lat,lon' (default: {DEFAULT_TOP[0]},{DEFAULT_TOP[1]})")
    p_run.add_argument("--km", type=float, default=DEFAULT_KM,
                       help=f"Corridor drive distance in km (default: {DEFAULT_KM} km)")
    p_run.add_argument("--speed", type=float, default=DEFAULT_SPEED,
                       help=f"Target driving speed in m/s (default: {DEFAULT_SPEED} m/s = 90 km/h)")
    p_run.add_argument("--mph", type=float, default=None,
                       help="Target driving speed in mph (e.g. 55.9, 60.0)")
    p_run.add_argument("--kmh", type=float, default=None,
                       help="Target driving speed in km/h (e.g. 90.0, 110.0)")
    p_run.add_argument("--cold-mode", choices=("build-and-stream", "full-network"), default="build-and-stream",
                       help="Cold start staging mode (default: build-and-stream)")
    p_run.add_argument("--seed-dir", default=DEFAULT_SEED_DIR,
                       help=f"Seed snapshot directory (default: {DEFAULT_SEED_DIR})")
    p_run.add_argument("--out", default=None,
                       help="Output directory for benchmark artifacts and reports")
    p_run.add_argument("--keep-server", action="store_true",
                       help="Do not terminate external companion server if already running")
    p_run.add_argument("--restart-server", action="store_true",
                       help="Force stop/restart companion server")
    p_run.add_argument("--teleport-timeout", type=float, default=DEFAULT_TELEPORT_TIMEOUT,
                       help=f"Max wait time in seconds for teleport build milestones (default: {DEFAULT_TELEPORT_TIMEOUT}s)")
    p_run.add_argument("--use-player-vehicle", action="store_true",
                       help="Drive player's active vehicle instead of stock test car")
    p_run.add_argument("--appimage", default=None,
                       help="Linux: start the world server from this packaged .AppImage instead of server/earthd.py")
    p_run.add_argument("--peers", default=None,
                       help="Optional compute-sharing helper peers (e.g. 100.119.236.84:47620)")
    p_run.add_argument("--dry-run", "--plan-only", dest="dry_run", action="store_true",
                       help="Purge cache and plan profile without running game drive")

    # 4. compare
    p_cmp = subparsers.add_parser("compare", help="Compare two benchmark runs and output delta tables with verdict")
    p_cmp.add_argument("run_a", help="Path to Run A directory (Baseline)")
    p_cmp.add_argument("run_b", help="Path to Run B directory (Candidate)")
    p_cmp.add_argument("--out", default=None, help="Output directory for comparison markdown and json reports")

    args = parser.parse_args(argv)

    if not args.subcommand:
        parser.print_help()
        return 0

    if args.subcommand == "purge":
        purge_caches(cold_mode=args.cold_mode, seed_dir=args.seed_dir, keep_server=args.keep_server, restart_server=args.restart_server)
        return 0
    elif args.subcommand == "seed":
        save_seed(args.seed_dir)
        return 0
    elif args.subcommand == "run":
        return run_bench(args)
    elif args.subcommand == "compare":
        return compare_runs(args.run_a, args.run_b, out_dir=args.out)

    return 0


if __name__ == "__main__":
    sys.exit(main())
