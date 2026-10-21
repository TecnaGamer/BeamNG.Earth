#!/usr/bin/env python3
"""Repeat the measured A3 streaming case with a fixed scene and a fully settled start.

    python tools/roadstudy/a3_stream.py --out docs/research/a3-live/controlled-before

Requires the Earth level, companion server and localhost game MCP. Uses the existing kinematic
drive harness: this measures streaming, not collision safety. Time/weather stay fixed for repeat
runs; the saved pre-test environment is in environment.json. Restore with benchmark.lua's restore
helper when finished. The ordinary benchmark and real-physics autodrive are separate checks.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import benchmark
import drive
import mcp

START = (48.99047902657597, 12.071765737262986)
PROFILE = ROOT / "docs/drives/a3-regensburg-east-200kph.json"

FPS_REASON_LUA = "return jsonEncode(Engine.getCurrentFpsLimitReason())"
FPS_CAPTURE_LUA = r'''
assert(CppSettings, 'runtime FPS settings unavailable')
local saved = { runtime = {}, configured = {}, reason = Engine.getCurrentFpsLimitReason() }
for _, k in ipairs({'fpsLimit', 'fpsLimitEnabled', 'fpsLimitBackground', 'fpsLimitBackgroundEnabled'}) do
  assert(CppSettings[k] ~= nil, 'missing runtime FPS setting: ' .. k)
  saved.runtime[k] = CppSettings[k]
  saved.configured[k] = settings.getValue(k)
end
return jsonEncode(saved)
'''
FPS_APPLY_LUA = r'''
-- The settings module and the editor's runtime graphics controls use CppSettings for these keys.
-- Change only the live background policy; settings.setValue would also schedule a persistent save.
CppSettings.fpsLimitBackground = CppSettings.fpsLimit
CppSettings.fpsLimitBackgroundEnabled = CppSettings.fpsLimitEnabled
return 'background FPS matched to foreground'
'''


def lua(code):
    return mcp.call("run_lua", {"code": code})


def call(name, value=None):
    arg = benchmark.lua_long(json.dumps(value)) if value is not None else ""
    return json.loads(lua(f"return BNGE_BENCH.{name}({arg})"))


@contextmanager
def temporary_fps_limits():
    saved = json.loads(lua(FPS_CAPTURE_LUA))
    try:
        lua(FPS_APPLY_LUA)
        yield saved
    finally:
        values = benchmark.lua_long(json.dumps(saved["runtime"]))
        lua("local saved=jsonDecode(" + values + "); for k,v in pairs(saved) do CppSettings[k]=v end "
            "return 'FPS settings restored'")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--timeout", type=float, default=240)
    ap.add_argument("--terrain-shadows", choices=("keep", "on", "off"), default="keep",
                    help="temporary diagnostic override, restored after the drive")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    environment = {}
    with temporary_fps_limits() as saved:
        environment["fps_limits_saved"] = saved
        environment["fps_limits_test"] = {"fpsLimitBackground": saved["runtime"]["fpsLimit"],
                                          "fpsLimitBackgroundEnabled": saved["runtime"]["fpsLimitEnabled"]}
        try:
            return run_study(a, environment)
        finally:
            try:
                environment["fps_limit_reason_end"] = json.loads(lua(FPS_REASON_LUA))
            except (Exception, SystemExit) as e:
                environment["fps_limit_reason_end"] = {"error": str(e)}
            (a.out / "environment.json").write_text(json.dumps(environment, indent=2) + "\n", encoding="utf-8")


def run_study(a, environment):
    mcp.quiet_ui()
    lua(benchmark.LUA.read_text())
    before = call("info")
    call("zeroInputs")
    result = call("teleport", {"lat": START[0], "lon": START[1]})
    if not result.get("ok"):
        raise RuntimeError(f"teleport refused: {result}")
    end = time.monotonic() + a.timeout
    stable, polls = 0, 0
    while time.monotonic() < end:
        p = call("progress")
        f, r, pl = p.get("far", {}), p.get("roads", {}), p.get("player", {})
        here = abs(pl.get("lat", 0) - START[0]) < 1e-5 and abs(pl.get("lon", 0) - START[1]) < 1e-5
        settled = here and not p.get("pending") and f.get("queued") == 0 and f.get("old") == 0 \
            and r.get("walksQueued") == 0 and r.get("queued") == 0 and p.get("serverBacklog") == 0 \
            and (p.get("messages") or {}).get("queued", 0) == 0
        stable = stable + 1 if settled else 0
        if polls % 10 == 0:
            print(json.dumps({"status": p.get("status"), "queued": f.get("queued"),
                              "roadQueue": r.get("walksQueued"), "backlog": p.get("serverBacklog")}), flush=True)
        if stable >= 3:
            break
        polls += 1
        time.sleep(1)
    else:
        raise RuntimeError("A3 start did not settle; no timing run was made")
    call("zeroInputs")
    fixed_time = benchmark.tod_fields({"lat": START[0], "lon": START[1], "utcOffset": 2},
                                     {"date": "2026-10-20", "hour": 10})
    call("setTime", fixed_time)
    call("setWeather", {"coverage": 0, "cirrus": 0, "visM": 60000})
    original_shadow = None
    try:
        if a.terrain_shadows != "keep":
            original_shadow = lua("local t=require('beamngEarth/terrain').get(); "
                                  "return t:getField('castShadows',0)")
            lua("local t=require('beamngEarth/terrain').get(); t:setField('castShadows',0,'"
                + ("1" if a.terrain_shadows == "on" else "0") + "'); t:postApply(); return 'set'")
        lua("local v=getPlayerVehicle(0); local p=v:getPosition(); "
            "local h=math.rad(77.4680573830177); "
            "local q=quatFromDir(vec3(math.sin(h),math.cos(h),0),vec3(0,0,1)); "
            "v:setPositionRotation(p.x,p.y,p.z,q.x,q.y,q.z,q.w); "
            "commands.setGameCamera(); core_camera.setByName(0,'orbit'); core_camera.setFOV(0,65); return 'ready'")
        time.sleep(3)
        # Read files only to identify exactly which installed Lua candidate was exercised.
        import os
        mod = Path(os.environ.get("LOCALAPPDATA", "")) / "BeamNG/BeamNG.drive/current/mods/unpacked/beamng_earth/lua/ge/extensions/beamngEarth"
        hashes = {name: hashlib.sha256((mod / name).read_bytes()).hexdigest()
                  for name in ("core.lua", "farfield.lua", "roads.lua", "satmat.lua", "terrain.lua", "messagequeue.lua")
                  if (mod / name).exists()}
        environment.update({"saved": before, "start": START, "profile_sha256": hashlib.sha256(PROFILE.read_bytes()).hexdigest(),
                            "fixed_time": fixed_time, "weather": "clear, visibility 60000 m", "lua_sha256": hashes,
                            "terrain_shadow_override": a.terrain_shadows,
                            "render": json.loads(mcp.call("get_render_info")), "tod": json.loads(mcp.call("get_time_of_day"))})
        environment["imagery_shadows"] = json.loads(lua(
            "local c={enabled=0,disabled=0}; for _,id in ipairs(scenetree.findClassObjects('Material')) do "
            "local m=scenetree.findObject(id); if m and m:getName():find('^bngEarthFar_') then "
            "local s=m:getField('castShadows',0); if s=='0' or s=='false' then c.disabled=c.disabled+1 "
            "else c.enabled=c.enabled+1 end end end return jsonEncode(c)"))
        environment["fps_limit_reason_start"] = json.loads(lua(FPS_REASON_LUA))
        (a.out / "environment.json").write_text(json.dumps(environment, indent=2) + "\n", encoding="utf-8")
        return drive.main(["--profile", str(PROFILE), "--label", a.out.name, "--out", str(a.out), "--max-s", "180"])
    finally:
        if original_shadow is not None:
            lua("local t=require('beamngEarth/terrain').get(); if t then t:setField('castShadows',0,"
                + benchmark.lua_long(original_shadow) + "); t:postApply() end return 'restored'")


if __name__ == "__main__":
    sys.exit(main())
