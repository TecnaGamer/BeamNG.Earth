#!/usr/bin/env python3
"""BeamNG.Earth Performance Capture & Reporting Tool.

Connects to BeamNG's built-in MCP server, hooks a lightweight per-frame GE Lua recorder
(onPreRender) to capture high-resolution frame telemetry, subsystem latencies,
GPU passes, shadow and forest statistics into native .bngperfgraph.json format,
samples companion streaming status and vehicle kinematics over a designated duration,
and generates an executive markdown performance report and raw JSON data.

Usage:
  python tools/perf_capture.py [options]

Examples:
  python tools/perf_capture.py --duration 10
  python tools/perf_capture.py --duration 15 --toggle-graph --unpause
  python tools/perf_capture.py --duration 30 --interval 0.25 --out perf_reports/high_speed_drive.md
  python tools/perf_capture.py --duration 10 --bngperfgraph-out perf_reports/my_run.bngperfgraph.json
"""

import argparse
import datetime
import json
import math
import os
import sys
import time

# Ensure local tools can be imported
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import mcp  # noqa: E402


def ensure_play_state():
    """Ensure game UI is back to play and unpaused."""
    try:
        mcp.call("set_ui_state", {"route": "play"})
        mcp.call("resume_physics")
        unpause_lua = """
        if simTimeAuthority then
            if simTimeAuthority.clearPauseStack then simTimeAuthority.clearPauseStack() end
            if simTimeAuthority.pause then simTimeAuthority.pause(false) end
            if simTimeAuthority.unpause then simTimeAuthority.unpause() end
        end
        """
        mcp.call("run_lua", {"code": unpause_lua})
    except Exception as e:
        print(f"[Warning] Failed to ensure play state: {e}", file=sys.stderr)


def toggle_perf_graph():
    """Ensure native performance graph is toggled."""
    try:
        mcp.call("trigger_action", {"action": "toggle_performance_graph"})
    except Exception as e:
        try:
            mcp.call("run_lua", {"code": "togglePerformanceGraph()"})
        except Exception:
            print(f"[Warning] Failed to toggle performance graph: {e}", file=sys.stderr)


def enable_metrics_overlay():
    """Ensure core_metrics is loaded and GPU timing overlay is visible."""
    code = """
    extensions.load('core_metrics')
    if core_metrics and core_metrics.currentMode == 0 then
        core_metrics.toggle()
    end
    if Engine.Debug and Engine.Debug.setGpuTimingOverlayVisible then
        Engine.Debug.setGpuTimingOverlayVisible(true)
    end
    """
    try:
        mcp.call("run_lua", {"code": code})
    except Exception as e:
        print(f"[Warning] Failed to enable metrics overlay: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Per-Frame GE Lua Recorder (hooks onPreRender into pre-allocated buffers)
# ---------------------------------------------------------------------------

START_RECORDER_LUA = """
local function init_recorder()
  if _G.__bngEarthPerfRecorder and _G.__bngEarthPerfRecorder.proxy then
    pcall(function() _G.__bngEarthPerfRecorder.proxy:submitEventSinks({}, {}) end)
    pcall(function() _G.__bngEarthPerfRecorder.proxy:destroy() end)
    _G.__bngEarthPerfRecorder = nil
  end

  local expectedDuration = tonumber(__RECORD_DURATION__) or 10.0
  local cap = math.max(1000, math.ceil(expectedDuration * 200) + 1000)

  local R = {
    active = true,
    cap = cap,
    count = 0,
    startTime = os.clockhp(),
    CPU = {},
    GPU = {},
    FPS = table.new(cap, 0),
    memory = {
      osPhysAvailable = table.new(cap, 0),
      osPhysUsed = table.new(cap, 0),
      osVirtAvailable = table.new(cap, 0),
      osVirtUsed = table.new(cap, 0),
      processPhysUsed = table.new(cap, 0),
      processVirtUsed = table.new(cap, 0),
    },
    ShadowStats = {
      activeMaps = table.new(cap, 0),
      updatedMaps = table.new(cap, 0),
      polyCount = table.new(cap, 0),
      drawCalls = table.new(cap, 0),
      drawStateChanges = table.new(cap, 0),
      rtChanges = table.new(cap, 0),
    },
    Forest = {
      totalCells = table.new(cap, 0),
      cellsRendered = table.new(cap, 0),
      cellsBatched = table.new(cap, 0),
      cellItemsRendered = table.new(cap, 0),
      cellItemsBatched = table.new(cap, 0),
    },
    timing = {},
  }

  local hw = {
    cpu = (Engine.Platform and Engine.Platform.getCPUInfo and Engine.Platform.getCPUInfo()) or {},
    os = (Engine.Platform and Engine.Platform.getOSInfo and Engine.Platform.getOSInfo()) or {},
    power = (Engine.Platform and Engine.Platform.getPowerInfo and Engine.Platform.getPowerInfo()) or {},
    GPU = (Engine.Platform and Engine.Platform.getGPUInfo and Engine.Platform.getGPUInfo()) or {},
  }
  if hw.cpu and not hw.cpu.brandName then
    hw.cpu.brandName = hw.cpu.name or "Unknown CPU"
  end

  local sw = {
    version = tostring(beamng_version or "0.0.0"),
    versiontitle = tostring(beamng_versiontitle or ("BeamNG.drive " .. tostring(beamng_version or ""))),
    buildType = tostring(beamng_buildtype or "RELEASE"),
    architecture = tostring(beamng_arch or "x64"),
  }
  R.info = { HW = hw, SW = sw }

  local primeCpu = {}
  if Engine.Debug and Engine.Debug.getLastPerformanceMetrics then
    Engine.Debug.getLastPerformanceMetrics(primeCpu)
  end
  for k in pairs(primeCpu) do
    if k ~= "physAvailable" and k ~= "physUsed" and k ~= "virtAvailable" and k ~= "virtUsed" then
      R.CPU[k] = table.new(cap, 0)
    end
  end

  local primeGpu = (Engine.Debug and Engine.Debug.getGpuTimings and Engine.Debug.getGpuTimings()) or {}
  for k in pairs(primeGpu) do
    R.GPU[k] = table.new(cap, 0)
  end

  local cpuBuf = {}
  local sink = {}

  function sink:onPreRender(dtReal, dtSim, dtRaw)
    if not R.active then return end
    R.count = R.count + 1
    local i = R.count

    table.clear(cpuBuf)
    if Engine.Debug and Engine.Debug.getLastPerformanceMetrics then
      Engine.Debug.getLastPerformanceMetrics(cpuBuf)
    end
    for k, list in pairs(R.CPU) do
      list[i] = cpuBuf[k] or 0.0
    end
    for k, v in pairs(cpuBuf) do
      if not R.CPU[k] and k ~= "physAvailable" and k ~= "physUsed" and k ~= "virtAvailable" and k ~= "virtUsed" then
        local list = table.new(R.cap, 0)
        for j = 1, i - 1 do list[j] = 0.0 end
        list[i] = v or 0.0
        R.CPU[k] = list
      end
    end

    local gpuTimings = (Engine.Debug and Engine.Debug.getGpuTimings and Engine.Debug.getGpuTimings()) or {}
    for k, list in pairs(R.GPU) do
      list[i] = gpuTimings[k] or 0.0
    end
    for k, v in pairs(gpuTimings) do
      if not R.GPU[k] then
        local list = table.new(R.cap, 0)
        for j = 1, i - 1 do list[j] = 0.0 end
        list[i] = v or 0.0
        R.GPU[k] = list
      end
    end

    local mem = (Engine.Platform and Engine.Platform.getMemoryInfo and Engine.Platform.getMemoryInfo()) or {}
    R.memory.osPhysAvailable[i] = cpuBuf.physAvailable or mem.osPhysAvailable or 0
    R.memory.osPhysUsed[i] = cpuBuf.physUsed or mem.osPhysUsed or 0
    R.memory.osVirtAvailable[i] = cpuBuf.virtAvailable or mem.osVirtAvailable or 0
    R.memory.osVirtUsed[i] = cpuBuf.virtUsed or mem.osVirtUsed or 0
    R.memory.processPhysUsed[i] = mem.processPhysUsed or 0
    R.memory.processVirtUsed[i] = mem.processVirtUsed or 0

    local fps = tonumber(VariableRegistry.get("$fps::instantaneous", nil))
    if not fps or fps <= 0 then
      fps = (dtReal and dtReal > 0) and (1.0 / dtReal) or 0.0
    end
    R.FPS[i] = fps

    for k, list in pairs(R.ShadowStats) do
      local val = tonumber(VariableRegistry.get("$ShadowStats::" .. k, nil)) or 0
      list[i] = val
    end

    for k, list in pairs(R.Forest) do
      local val = tonumber(VariableRegistry.get("$Forest::" .. k, nil)) or 0
      list[i] = val
    end
  end

  R.proxy = newExtensionProxy(nil, "bngEarthPerfRecorder")
  R.proxy:submitEventSinks({ sink }, {})
  _G.__bngEarthPerfRecorder = R
  return jsonEncode({ status = "started", capacity = cap })
end

return init_recorder()
"""

STOP_RECORDER_LUA = """
local R = _G.__bngEarthPerfRecorder
if not R then return jsonEncode({ error = "no_recorder_active" }) end

R.active = false
if R.proxy then
  pcall(function() R.proxy:submitEventSinks({}, {}) end)
  pcall(function() R.proxy:destroy() end)
  R.proxy = nil
end

local count = R.count
local res = {
  CPU = R.CPU,
  GPU = R.GPU,
  FPS = { FPS = R.FPS },
  memory = R.memory,
  info = R.info or {},
  timing = R.timing or {},
  ShadowStats = R.ShadowStats,
  Forest = R.Forest,
}

local userPath = (FS and FS.getUserPath and FS:getUserPath()) or ""
local tempRel = "temp_bngperfgraph_" .. tostring(os.time()) .. ".json"
local tempVfs = "/" .. tempRel
local wroteFile = false

if jsonWriteFile then
  wroteFile = jsonWriteFile(tempVfs, res, false)
end

_G.__bngEarthPerfRecorder = nil

return jsonEncode({
  wroteFile = wroteFile,
  userPath = userPath,
  tempRel = tempRel,
  tempVfs = tempVfs,
  frameCount = count
})
"""


def start_frame_recorder(duration):
    """Start the lightweight per-frame onPreRender GE Lua recorder in BeamNG."""
    try:
        code = START_RECORDER_LUA.replace("__RECORD_DURATION__", str(duration))
        raw = mcp.call("run_lua", {"code": code})
        res = json.loads(raw)
        return res.get("status") == "started"
    except Exception as e:
        print(f"[Warning] Failed to hook per-frame Lua recorder: {e}", file=sys.stderr)
        return False


def stop_frame_recorder():
    """Stop the per-frame GE Lua recorder and retrieve the recorded perfgraph data."""
    try:
        raw = mcp.call("run_lua", {"code": STOP_RECORDER_LUA})
        meta = json.loads(raw)
        if "error" in meta:
            print(f"[Warning] Frame recorder stop returned error: {meta['error']}", file=sys.stderr)
            return None

        # Check if saved to userPath temp file on disk
        if meta.get("wroteFile") and meta.get("userPath") and meta.get("tempRel"):
            disk_path = os.path.join(meta["userPath"], meta["tempRel"])
            if os.path.exists(disk_path):
                try:
                    with open(disk_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    try:
                        os.remove(disk_path)
                    except OSError:
                        pass
                    return data
                except Exception as e:
                    print(f"[Warning] Failed reading temp perfgraph from disk: {e}", file=sys.stderr)

        # Fallback to requesting JSON string directly from VFS
        if meta.get("tempVfs"):
            try:
                vfs_content = mcp.call("run_lua", {"code": f"return readFile('{meta['tempVfs']}')"})
                mcp.call("run_lua", {"code": f"FS:removeFile('{meta['tempVfs']}')"})
                return json.loads(vfs_content)
            except Exception:
                pass

        return None
    except Exception as e:
        print(f"[Warning] Failed to retrieve per-frame perfgraph data: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Periodic Telemetry Sampling (Subsystems, Streaming Status, Kinematics)
# ---------------------------------------------------------------------------

def query_sample():
    """Query a single frame sample of telemetry from BeamNG MCP."""
    sample = {
        "timestamp": time.time(),
        "metrics": {},
        "console_vars": {},
        "vram": None,
        "streaming": None,
        "speed_mph": 0.0,
        "damage": 0.0,
    }

    # 1. Subsystem metrics from Engine.Debug
    try:
        raw_m = mcp.call("get_performance_metrics")
        sample["metrics"] = json.loads(raw_m)
    except Exception:
        pass

    # 2. Console FPS & timing variables
    lua_vars = """
    local v = {}
    local names = {
        "fps::instantaneous", "fps::avg", "fps::p90", "fps::p95", "fps::p99",
        "fps::min", "fps::max", "fps::waitForGPU", "fps::cpuTime", "fps::gpuTime"
    }
    for _, n in ipairs(names) do
        local val = VariableRegistry.get("$"..n, nil)
        if val then v[n] = tonumber(val) end
    end
    return jsonEncode(v)
    """
    try:
        raw_cv = mcp.call("run_lua", {"code": lua_vars})
        sample["console_vars"] = json.loads(raw_cv)
    except Exception:
        pass

    # 3. GPU VRAM
    try:
        raw_vram = mcp.call("get_vram_usage")
        sample["vram"] = json.loads(raw_vram) if raw_vram.startswith("{") else raw_vram.strip()
    except Exception:
        pass

    # 4. Companion streaming & vehicle speed (compatible with v1.7.0-beta table structure)
    lua_core = """
    local s = beamngEarth_core and beamngEarth_core.status and beamngEarth_core.status()
    local statusTbl = nil
    if type(s) == 'table' then
        statusTbl = s
    elseif type(s) == 'string' then
        local ok, parsed = pcall(function() return load('return ' .. s)() end)
        if ok and type(parsed) == 'table' then
            statusTbl = parsed
        end
    end

    local v = getPlayerVehicle(0)
    local vel = v and v:getVelocity()
    local speed = vel and (vel:length() * 2.23694) or 0
    local dmg = (v and map and map.objects and map.objects[v:getId()] and map.objects[v:getId()].damage) or 0
    return jsonEncode({
        status = statusTbl,
        status_raw = (type(s) == 'string' and s or nil),
        speed_mph = speed,
        damage = dmg
    })
    """
    try:
        raw_core = mcp.call("run_lua", {"code": lua_core})
        d_core = json.loads(raw_core)
        sample["speed_mph"] = d_core.get("speed_mph", 0.0)
        sample["damage"] = d_core.get("damage", 0.0)
        if d_core.get("status"):
            sample["streaming"] = d_core["status"]
        if d_core.get("status_raw"):
            sample["streaming_raw"] = d_core["status_raw"]
    except Exception:
        pass

    return sample


# ---------------------------------------------------------------------------
# Statistics Computation
# ---------------------------------------------------------------------------

def calculate_stats(samples, perfgraph=None):
    """Compute comprehensive performance statistics from recorded samples and perfgraph."""
    fps_list = []
    cpu_ms_list = []
    gpu_ms_list = []
    render_ms_list = []
    lua_ms_list = []
    phys_ms_list = []
    shadow_ms_list = []
    wait_gpu_ms_list = []
    speeds = []
    hitches = []

    # If per-frame perfgraph data is available, compute metrics from high-resolution dataset
    if perfgraph and perfgraph.get("FPS", {}).get("FPS"):
        raw_fps = perfgraph["FPS"]["FPS"]
        fps_list = [float(f) for f in raw_fps if f and f > 0]

        cpu_sec = perfgraph.get("CPU", {})
        gpu_sec = perfgraph.get("GPU", {})
        shadow_sec = perfgraph.get("ShadowStats", {})

        # Subsystems
        render_ms_list = [float(x) for x in cpu_sec.get("Render", []) if x is not None]
        lua_ms_list = [float(x) for x in cpu_sec.get("Lua", []) if x is not None]
        phys_and_vlua = cpu_sec.get("PhysicsAndVLUA", [])
        phys_nodes = cpu_sec.get("PhysicsNodes", [])
        max_phys_len = max(len(phys_and_vlua), len(phys_nodes))
        phys_ms_list = [
            (phys_and_vlua[i] if i < len(phys_and_vlua) else 0.0) +
            (phys_nodes[i] if i < len(phys_nodes) else 0.0)
            for i in range(max_phys_len)
        ]
        shadow_ms_list = [float(x) for x in cpu_sec.get("RenderShadows", []) if x is not None]

        wait_next = cpu_sec.get("WaitNextFrame", [])
        wait_gpu_ms_list = [float(x) for x in wait_next if x is not None]

        # Hitches (>33.33ms -> drops below 30 FPS)
        for i, fps in enumerate(fps_list):
            fms = 1000.0 / fps if fps > 0 else 999.0
            if fms > 33.33:
                hitches.append({
                    "frame_index": i + 1,
                    "frame_ms": fms,
                    "fps": fps,
                    "render_ms": render_ms_list[i] if i < len(render_ms_list) else 0.0,
                    "lua_ms": lua_ms_list[i] if i < len(lua_ms_list) else 0.0,
                    "physics_ms": phys_ms_list[i] if i < len(phys_ms_list) else 0.0,
                    "shadow_ms": shadow_ms_list[i] if i < len(shadow_ms_list) else 0.0,
                    "shadow_drawcalls": shadow_sec.get("drawCalls", [])[i] if i < len(shadow_sec.get("drawCalls", [])) else 0,
                    "speed_mph": 0.0,
                })
    else:
        # Fallback to periodic sampled data
        for s in samples:
            cv = s.get("console_vars", {})
            m = s.get("metrics", {})

            inst_fps = cv.get("fps::instantaneous")
            if inst_fps and inst_fps > 0:
                fps_list.append(inst_fps)
                frame_ms = 1000.0 / inst_fps
                if frame_ms > 33.33:
                    hitches.append({
                        "frame_index": len(fps_list),
                        "frame_ms": frame_ms,
                        "fps": inst_fps,
                        "render_ms": m.get("Render", 0.0),
                        "lua_ms": m.get("Lua", 0.0),
                        "physics_ms": m.get("PhysicsAndVLUA", 0.0) + m.get("PhysicsNodes", 0.0),
                        "shadow_ms": m.get("RenderShadows", 0.0),
                        "shadow_drawcalls": 0,
                        "wait_gpu_ms": cv.get("fps::waitForGPU", 0.0),
                        "speed_mph": s.get("speed_mph", 0.0),
                    })

            if "fps::cpuTime" in cv and cv["fps::cpuTime"] is not None:
                cpu_ms_list.append(cv["fps::cpuTime"])
            if "fps::gpuTime" in cv and cv["fps::gpuTime"] is not None and cv["fps::gpuTime"] >= 0:
                gpu_ms_list.append(cv["fps::gpuTime"])
            if "fps::waitForGPU" in cv and cv["fps::waitForGPU"] is not None:
                wait_gpu_ms_list.append(cv["fps::waitForGPU"])

            if "Render" in m:
                render_ms_list.append(m["Render"])
            if "Lua" in m:
                lua_ms_list.append(m["Lua"])
            p_total = m.get("PhysicsAndVLUA", 0.0) + m.get("PhysicsNodes", 0.0)
            if p_total > 0:
                phys_ms_list.append(p_total)
            if "RenderShadows" in m:
                shadow_ms_list.append(m["RenderShadows"])

    for s in samples:
        speeds.append(s.get("speed_mph", 0.0))

    def percentile(arr, p):
        if not arr:
            return 0.0
        sorted_arr = sorted(arr)
        k = (len(sorted_arr) - 1) * (p / 100.0)
        f = math.floor(k)
        c = math.ceil(k)
        if f == c:
            return sorted_arr[int(k)]
        d0 = sorted_arr[int(f)] * (c - k)
        d1 = sorted_arr[int(c)] * (k - f)
        return d0 + d1

    def mean(arr):
        return sum(arr) / max(1, len(arr))

    frame_ms_list = [1000.0 / f for f in fps_list] if fps_list else []

    stats = {
        "sample_count": len(samples),
        "frame_count": len(fps_list),
        "has_perfgraph": perfgraph is not None,
        "fps": {
            "mean": mean(fps_list),
            "median": percentile(fps_list, 50),
            "min": min(fps_list) if fps_list else 0.0,
            "max": max(fps_list) if fps_list else 0.0,
            "p1_low": percentile(fps_list, 1),
            "p5_low": percentile(fps_list, 5),
        },
        "frame_ms": {
            "mean": mean(frame_ms_list),
            "median": percentile(frame_ms_list, 50),
            "min": min(frame_ms_list) if frame_ms_list else 0.0,
            "max": max(frame_ms_list) if frame_ms_list else 0.0,
            "p90": percentile(frame_ms_list, 90),
            "p95": percentile(frame_ms_list, 95),
            "p99": percentile(frame_ms_list, 99),
        },
        "subsystems": {
            "render_ms": {"mean": mean(render_ms_list), "max": max(render_ms_list) if render_ms_list else 0.0},
            "lua_ms": {"mean": mean(lua_ms_list), "max": max(lua_ms_list) if lua_ms_list else 0.0},
            "physics_ms": {"mean": mean(phys_ms_list), "max": max(phys_ms_list) if phys_ms_list else 0.0},
            "shadow_ms": {"mean": mean(shadow_ms_list), "max": max(shadow_ms_list) if shadow_ms_list else 0.0},
            "cpu_ms": {"mean": mean(cpu_ms_list), "max": max(cpu_ms_list) if cpu_ms_list else 0.0},
            "gpu_ms": {"mean": mean(gpu_ms_list), "max": max(gpu_ms_list) if gpu_ms_list else 0.0},
            "wait_gpu_ms": {"mean": mean(wait_gpu_ms_list), "max": max(wait_gpu_ms_list) if wait_gpu_ms_list else 0.0},
        },
        "budget": {
            "under_16ms_pct": (sum(1 for m in frame_ms_list if m <= 16.67) / max(1, len(frame_ms_list))) * 100.0,
            "under_33ms_pct": (sum(1 for m in frame_ms_list if m <= 33.33) / max(1, len(frame_ms_list))) * 100.0,
            "hitch_count": len(hitches),
        },
        "hitches": hitches,
        "speed": {
            "mean_mph": mean(speeds),
            "max_mph": max(speeds) if speeds else 0.0,
        },
    }

    # Shadow & Forest stats if available in perfgraph
    if perfgraph and perfgraph.get("ShadowStats"):
        ss = perfgraph["ShadowStats"]
        stats["shadow_stats"] = {
            "active_maps": max(ss.get("activeMaps", [0]) or [0]),
            "poly_count_mean": mean(ss.get("polyCount", [0]) or [0]),
            "poly_count_max": max(ss.get("polyCount", [0]) or [0]),
            "draw_calls_mean": mean(ss.get("drawCalls", [0]) or [0]),
            "draw_calls_max": max(ss.get("drawCalls", [0]) or [0]),
        }
    if perfgraph and perfgraph.get("Forest"):
        fs = perfgraph["Forest"]
        stats["forest_stats"] = {
            "total_cells": max(fs.get("totalCells", [0]) or [0]),
            "cells_rendered_mean": mean(fs.get("cellsRendered", [0]) or [0]),
            "cell_items_mean": mean(fs.get("cellItemsRendered", [0]) or [0]),
            "cell_items_max": max(fs.get("cellItemsRendered", [0]) or [0]),
        }

    return stats


# ---------------------------------------------------------------------------
# Executive Markdown Report Generator
# ---------------------------------------------------------------------------

def generate_markdown_report(stats, duration, screenshot_path=None, bngperfgraph_path=None, latest_streaming=None):
    """Format performance statistics into an executive markdown document."""
    b = stats["budget"]
    fps = stats["fps"]
    fms = stats["frame_ms"]
    sub = stats["subsystems"]

    passed = fms["p95"] <= 16.67 and b["under_16ms_pct"] >= 95.0
    status_badge = "✅ PASS" if passed else ("⚠️ ACCEPTABLE" if b["under_33ms_pct"] >= 95.0 else "❌ REGRESSION")

    lines = [
        f"# BeamNG.Earth Performance Report",
        f"",
        f"**Date:** {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ",
        f"**Duration:** {duration:.1f} seconds ({stats.get('frame_count', 0)} frames recorded, {stats['sample_count']} telemetry samples)  ",
        f"**Status:** {status_badge}  ",
    ]

    if bngperfgraph_path:
        rel_graph = os.path.basename(bngperfgraph_path)
        lines.append(f"**Native PerfGraph:** [{rel_graph}]({bngperfgraph_path.replace(os.sep, '/')})  ")

    lines += [
        f"",
        f"---",
        f"",
        f"## 1. Executive Summary",
        f"",
        f"| Metric | Target | Measured | Result |",
        f"| :--- | :--- | :--- | :--- |",
        f"| **Average Framerate** | ≥ 60.0 FPS | **{fps['mean']:.1f} FPS** | {'✅' if fps['mean'] >= 60.0 else '⚠️'} |",
        f"| **Median Framerate** | ≥ 60.0 FPS | **{fps['median']:.1f} FPS** | {'✅' if fps['median'] >= 60.0 else '⚠️'} |",
        f"| **1% Low Framerate** | ≥ 45.0 FPS | **{fps['p1_low']:.1f} FPS** | {'✅' if fps['p1_low'] >= 45.0 else '⚠️'} |",
        f"| **Mean Frame Time** | < 16.67 ms | **{fms['mean']:.2f} ms** | {'✅' if fms['mean'] <= 16.67 else '❌'} |",
        f"| **p95 Frame Latency** | < 16.67 ms | **{fms['p95']:.2f} ms** | {'✅' if fms['p95'] <= 16.67 else '❌'} |",
        f"| **p99 Frame Latency** | < 25.00 ms | **{fms['p99']:.2f} ms** | {'✅' if fms['p99'] <= 25.00 else '⚠️'} |",
        f"| **Max Hitch Frame** | < 50.00 ms | **{fms['max']:.2f} ms** | {'✅' if fms['max'] < 50.00 else '❌'} |",
        f"| **Frames within 16.6ms** | ≥ 95.0% | **{b['under_16ms_pct']:.1f}%** | {'✅' if b['under_16ms_pct'] >= 95.0 else '⚠️'} |",
        f"",
        f"---",
        f"",
        f"## 2. Subsystem Latency Breakdown",
        f"",
        f"| Engine Subsystem | Mean Latency (ms) | Peak Latency (ms) | % of Frame Budget |",
        f"| :--- | :--- | :--- | :--- |",
        f"| **Render Core** | {sub['render_ms']['mean']:.2f} ms | {sub['render_ms']['max']:.2f} ms | {sub['render_ms']['mean']/16.67*100.0:.1f}% |",
        f"| **Shadow Passes** | {sub['shadow_ms']['mean']:.2f} ms | {sub['shadow_ms']['max']:.2f} ms | {sub['shadow_ms']['mean']/16.67*100.0:.1f}% |",
        f"| **Game Engine Lua VM** | {sub['lua_ms']['mean']:.2f} ms | {sub['lua_ms']['max']:.2f} ms | {sub['lua_ms']['mean']/16.67*100.0:.1f}% |",
        f"| **Physics Core & VLUA** | {sub['physics_ms']['mean']:.2f} ms | {sub['physics_ms']['max']:.2f} ms | {sub['physics_ms']['mean']/16.67*100.0:.1f}% |",
        f"| **Wait for GPU** | {sub['wait_gpu_ms']['mean']:.2f} ms | {sub['wait_gpu_ms']['max']:.2f} ms | - |",
        f"",
    ]

    if "shadow_stats" in stats or "forest_stats" in stats:
        lines += [
            f"### Shadow & Forest Rendering Load",
            f"",
            f"| Metric | Mean | Peak |",
            f"| :--- | :--- | :--- |",
        ]
        if "shadow_stats" in stats:
            ss = stats["shadow_stats"]
            lines.append(f"| **Shadow Draw Calls** | {ss['draw_calls_mean']:.0f} calls | {ss['draw_calls_max']} calls |")
            lines.append(f"| **Shadow Poly Count** | {ss['poly_count_mean']:.0f} polys | {ss['poly_count_max']} polys |")
        if "forest_stats" in stats:
            fs = stats["forest_stats"]
            lines.append(f"| **Forest Items Rendered** | {fs['cell_items_mean']:.0f} items | {fs['cell_items_max']} items |")
            lines.append(f"| **Forest Total Cells** | {fs['total_cells']} cells | {fs['total_cells']} cells |")
        lines.append("")

    lines += [
        f"---",
        f"",
        f"## 3. Hitches & Drops Analysis",
        f"",
    ]

    hitches = stats["hitches"]
    if not hitches:
        lines.append("> [!TIP]\n> **Zero frame hitches recorded!** Frame times remained strictly within smooth operational margins throughout the entire test.")
    else:
        lines.append(f"> [!WARNING]\n> **{len(hitches)} frame drop(s)** exceeded the 33.3ms threshold (>30 FPS drops):")
        lines.append("")
        lines.append("| Frame # | Frame Time | Inst FPS | Render | Lua | Physics | Shadows | Vehicle Speed |")
        lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
        for h in hitches[:15]:
            lines.append(f"| #{h.get('frame_index', '-')} | **{h['frame_ms']:.1f} ms** | {h['fps']:.1f} | {h['render_ms']:.1f} ms | {h['lua_ms']:.1f} ms | {h['physics_ms']:.1f} ms | {h.get('shadow_ms', 0.0):.1f} ms | {h.get('speed_mph', 0.0):.1f} mph |")
        if len(hitches) > 15:
            lines.append(f"| ... | *({len(hitches) - 15} additional hitches truncated)* | | | | | | |")

    lines += [
        f"",
        f"---",
        f"",
        f"## 4. Vehicle & Streaming Context",
        f"",
        f"- **Average Speed:** {stats['speed']['mean_mph']:.1f} mph (Peak: {stats['speed']['max_mph']:.1f} mph)",
    ]

    if latest_streaming and isinstance(latest_streaming, dict):
        net_st = latest_streaming.get("net", "unknown")
        status_txt = latest_streaming.get("status", "ready")
        vis_dist = latest_streaming.get("visibleDistance", 0)
        far_data = latest_streaming.get("far", {})
        trees_data = latest_streaming.get("trees", {})
        gov = latest_streaming.get("governor", {})

        lines += [
            f"- **Network Status:** `{net_st}` (Banner: `{latest_streaming.get('banner', 'ok')}`)",
            f"- **Streaming Status:** `{status_txt}`",
            f"- **Visible Distance:** {vis_dist/1000.0:.1f} km",
            f"- **Farfield Live Tiles:** {far_data.get('live', 0)} (Preloaded: {far_data.get('preloaded', 0)}, Queued: {far_data.get('queued', 0)})",
            f"- **Active Forest Items:** {trees_data.get('items', 0):,} trees",
            f"- **Governor Workload:** {gov.get('units', 0)} units (Tokens: {gov.get('tokens', 0):.1f}, Target: {gov.get('target', 0)}ms)",
            f"- **Rebases Occurred:** {latest_streaming.get('rebaseCount', 0)} (Lag: {latest_streaming.get('rebaseLag', 0)}s)",
        ]

    if screenshot_path and os.path.exists(screenshot_path):
        lines += [
            f"",
            f"---",
            f"",
            f"## 5. In-Engine Capture",
            f"",
            f"![Performance Capture Screenshot]({screenshot_path.replace(os.sep, '/')})",
        ]

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main Orchestration Routine
# ---------------------------------------------------------------------------

def capture_performance(duration=10.0, interval=0.5, out_md=None, out_json=None,
                        out_bngperfgraph=None, toggle_graph=False, toggle_metrics=False,
                        unpause=True, screenshot=True, optick=False, no_perfgraph=False):
    """Main program entry to capture performance telemetry and write reports."""
    print("[PerfCapture] Initializing BeamNG performance session...")
    print(f"[PerfCapture] Active MCP endpoint: {mcp.get_mcp_url()}")

    if unpause:
        ensure_play_state()

    if toggle_graph:
        toggle_perf_graph()

    if toggle_metrics:
        enable_metrics_overlay()

    if optick:
        try:
            mcp.call("profiler_run", {"duration": duration})
            print(f"[PerfCapture] Optick capture initiated ({duration:.1f}s)")
        except Exception as e:
            print(f"[PerfCapture] Optick profiler not available: {e}")

    # Hook the high-speed per-frame GE Lua recorder
    recorder_active = False
    if not no_perfgraph:
        recorder_active = start_frame_recorder(duration)
        if recorder_active:
            print("[PerfCapture] Per-frame GE Lua recorder hooked (onPreRender active)")
        else:
            print("[PerfCapture] Note: Running in periodic sampling mode without per-frame hook")

    samples = []
    start_time = time.time()
    next_sample = start_time
    screenshot_taken = None

    print(f"[PerfCapture] Sampling telemetry for {duration:.1f}s at {interval:.2f}s intervals...")
    screenshot_due = start_time + (duration / 2.0)

    while True:
        now = time.time()
        elapsed = now - start_time
        if elapsed >= duration:
            break

        if screenshot and not screenshot_taken and now >= screenshot_due:
            try:
                res = mcp.call("screenshot")
                if res and os.path.exists(res):
                    screenshot_taken = res
            except Exception:
                pass

        if now >= next_sample:
            s = query_sample()
            samples.append(s)
            next_sample = now + interval

        time.sleep(0.04)

    # Stop per-frame recorder and retrieve data
    perfgraph_data = None
    if recorder_active:
        print("[PerfCapture] Stopping per-frame GE Lua recorder and collecting data...")
        perfgraph_data = stop_frame_recorder()

    print(f"[PerfCapture] Completed recording ({len(samples)} periodic samples).")

    # Calculate statistics
    stats = calculate_stats(samples, perfgraph=perfgraph_data)

    # Determine report paths
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    reports_dir = os.path.join(os.getcwd(), "perf_reports")
    os.makedirs(reports_dir, exist_ok=True)

    if not out_md:
        out_md = os.path.join(reports_dir, f"perf_report_{timestamp}.md")
    if not out_json:
        out_json = os.path.join(reports_dir, f"perf_data_{timestamp}.json")
    if not out_bngperfgraph:
        out_bngperfgraph = os.path.join(reports_dir, f"PerfCapture_{timestamp}.bngperfgraph.json")

    # Save native .bngperfgraph.json file if available
    if perfgraph_data:
        try:
            with open(out_bngperfgraph, "w", encoding="utf-8") as f:
                json.dump(perfgraph_data, f, indent=2)
            print(f"[PerfCapture] Native perfgraph saved: {out_bngperfgraph}")
            stats["bngperfgraph_path"] = out_bngperfgraph
        except Exception as e:
            print(f"[Warning] Failed writing .bngperfgraph.json: {e}", file=sys.stderr)

    # Save JSON raw data
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({"stats": stats, "samples": samples}, f, indent=2)
    print(f"[PerfCapture] Raw telemetry saved: {out_json}")

    # Extract latest streaming context
    latest_streaming = None
    for s in reversed(samples):
        if s.get("streaming"):
            latest_streaming = s["streaming"]
            break

    # Generate and save Markdown report
    md_content = generate_markdown_report(
        stats,
        duration,
        screenshot_path=screenshot_taken,
        bngperfgraph_path=out_bngperfgraph if perfgraph_data else None,
        latest_streaming=latest_streaming
    )
    with open(out_md, "w", encoding="utf-8") as f:
        f.write(md_content)
    print(f"[PerfCapture] Performance report saved: {out_md}")

    # Print summary to stdout
    fps = stats["fps"]
    fms = stats["frame_ms"]
    print("\n" + "=" * 60)
    print("           BEAMNG.EARTH PERFORMANCE SUMMARY")
    print("=" * 60)
    print(f" Recorded Frames    : {stats.get('frame_count', 0)} frames ({stats['sample_count']} samples)")
    print(f" Average FPS        : {fps['mean']:.1f} FPS (Median: {fps['median']:.1f} FPS)")
    print(f" 1% Low FPS         : {fps['p1_low']:.1f} FPS (Min: {fps['min']:.1f} FPS)")
    print(f" Frame Latency Mean : {fms['mean']:.2f} ms")
    print(f" Frame Latency p95  : {fms['p95']:.2f} ms")
    print(f" Frame Latency p99  : {fms['p99']:.2f} ms")
    print(f" Max Hitch Latency  : {fms['max']:.2f} ms")
    print(f" Frames < 16.67ms   : {stats['budget']['under_16ms_pct']:.1f}%")
    print(f" Drops > 33.33ms    : {stats['budget']['hitch_count']} frames")
    print("=" * 60)

    return out_md, out_json, stats


def main():
    parser = argparse.ArgumentParser(description="Capture BeamNG.Earth performance telemetry and generate report")
    parser.add_argument("--duration", type=float, default=10.0, help="Capture duration in seconds (default: 10.0)")
    parser.add_argument("--interval", type=float, default=0.5, help="Sampling interval in seconds (default: 0.5)")
    parser.add_argument("--out", type=str, default=None, help="Output markdown report path")
    parser.add_argument("--json-out", type=str, default=None, help="Output raw JSON data path")
    parser.add_argument("--bngperfgraph-out", type=str, default=None, help="Output native .bngperfgraph.json path")
    parser.add_argument("--toggle-graph", action="store_true", help="Toggle native performance graph on")
    parser.add_argument("--toggle-metrics", action="store_true", help="Toggle core_metrics overlay on")
    parser.add_argument("--no-unpause", action="store_true", help="Do not force play/unpause state")
    parser.add_argument("--no-screenshot", action="store_true", help="Do not capture midpoint screenshot")
    parser.add_argument("--optick", action="store_true", help="Trigger concurrent Optick profiler capture")
    parser.add_argument("--no-perfgraph", action="store_true", help="Disable per-frame GE Lua recorder hook")

    args = parser.parse_args()

    capture_performance(
        duration=args.duration,
        interval=args.interval,
        out_md=args.out,
        out_json=args.json_out,
        out_bngperfgraph=args.bngperfgraph_out,
        toggle_graph=args.toggle_graph,
        toggle_metrics=args.toggle_metrics,
        unpause=not args.no_unpause,
        screenshot=not args.no_screenshot,
        optick=args.optick,
        no_perfgraph=args.no_perfgraph,
    )


if __name__ == "__main__":
    main()
