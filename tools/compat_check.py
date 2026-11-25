#!/usr/bin/env python3
"""After a BeamNG.drive update: does BeamNG.Earth still work? One pass over what the mod relies on (2026-10-18, ahead of
BeamNG 0.40). Run with the game in the BeamNG.Earth level (the launcher started it, the car placed, a minute after):

    python3 tools/compat_check.py [--user-dir DIR]

Prints PASS / WARN / FAIL per check and what it means. Needs the game's MCP server (Options > Advanced).
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mcp  # noqa: E402

PROBE = r'''
local out = {}
local function has(path)
  local t = _G
  for k in path:gmatch('[^%.:]+') do if type(t) ~= 'table' and type(t) ~= 'userdata' then return false end t = t[k] end
  return t ~= nil
end
out.version = tostring(beamng_versiond)
out.core = beamngEarth_core ~= nil
out.apis = {}
for _, a in ipairs({ 'simTimeAuthority.pause', 'simTimeAuthority.getPause', 'castRayStatic', 'spawn.safeTeleport',
    'core_environment.setTimeOfDay', 'core_camera.setPosition', 'commands.dropPlayerAtCamera', 'loadJsonMaterialsFile',
    'Engine.Debug.getLastPerformanceMetrics', 'hptimer', 'ui_message', 'createObject', 'scenetree.findObject',
    'extensions.load', 'getCurrentLevelIdentifier' }) do out.apis[a] = has(a) end
out.extensions = {}
for _, e in ipairs({ 'core_multiSpawn', 'gameplay_traffic', 'gameplay_parking', 'core_environment' }) do
  out.extensions[e] = rawget(_G, e) ~= nil
end
local okIm, im = pcall(function() return ui_imgui end)
out.imgui = okIm and im ~= nil and im.ImDrawList_AddText1 ~= nil and im.GetBackgroundDrawList1 ~= nil
if beamngEarth_core then
  local s = load('return ' .. beamngEarth_core.status())()
  out.inOurLevel, out.active, out.net = s.inOurLevel, s.active, s.net
  out.block, out.follows, out.far = s.block, s.followCount, s.far and { live = s.far.live, failed = s.far.failed,
    materials = s.far.materials }
  local t = require('beamngEarth/terrain')
  out.slots = t.slots and #t.slots or 0
  out.lastSwap = t.lastSwap
  local tb = t.get()
  out.terrainFile = tb and tb:getField('terrainFile', 0)
  local n, slots = 0, 0
  for _, nm in ipairs(scenetree.findClassObjects('TSStatic') or {}) do
    local o = scenetree.findObject(nm)
    local sh = o and o:getField('shapeName', 0) or ''
    if sh:find('/temp/beamngEarth/', 1, true) then n = n + 1 end
  end
  out.tsstatics = n
  out.credits = require('beamngEarth/credits').serverText()
  out.trees = s.trees and s.trees.items
  out.waterBlocks = #(scenetree.findClassObjects('WaterBlock') or {})
  -- winter (winter.lua): stock ai.lua's getMinPlanLen still the text its planning-horizon patch rewrites
  local okW, w = pcall(require, 'beamngEarth/winter')
  out.aiPlanLine = okW and w.aiLine and w.aiLine() or false
  out.precip = #(scenetree.findClassObjects('Precipitation') or {})
end
return jsonEncode(out)
'''


def check(results, ok, name, detail, warn=False):
    tag = "PASS" if ok else ("WARN" if warn else "FAIL")
    results.append(tag)
    print(f"{tag:4}  {name}: {detail}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user-dir", default=os.path.expanduser("~/.local/share/BeamNG/BeamNG.drive/current"))
    a = ap.parse_args()
    res = []
    try:
        d = json.loads(str(mcp.call("run_lua", {"code": PROBE})))
    except Exception as e:
        print(f"FAIL  the game's MCP server: {e} (Options > Advanced > MCP on, the game running?)")
        return 1
    print(f"BeamNG.drive {d.get('version')}\n")
    check(res, d.get("core"), "mod loaded", "beamngEarth_core present" if d.get("core") else
          "not loaded: mods off after the update? (main menu: enable mods; Mods > Manage: BeamNG.Earth on)")
    if not d.get("core"):
        return 1
    check(res, d.get("inOurLevel") and d.get("active"), "in the BeamNG.Earth level, active",
          f"inOurLevel={d.get('inOurLevel')} active={d.get('active')} net={d.get('net')}")
    missing = [k for k, v in d["apis"].items() if not v]
    check(res, not missing, "engine Lua APIs", "all present" if not missing else "missing: " + ", ".join(missing))
    mext = [k for k, v in d["extensions"].items() if not v]
    check(res, not mext, "game extensions (traffic, parking, multiSpawn, environment)",
          "loaded" if not mext else "not loaded: " + ", ".join(mext), warn=True)
    check(res, d.get("imgui"), "imgui draw calls (credits, banner)", "present" if d.get("imgui") else "missing")
    check(res, (d.get("slots") or 0) >= 1 and d.get("terrainFile", "").endswith(".ter"), "terrain block (.ter swap)",
          f"{d.get('slots')} material slots, file {d.get('terrainFile')}, last swap {d.get('lastSwap')}")
    check(res, (d.get("follows") or 0) > 0, "terrain follow commits", f"{d.get('follows')} so far (drive a bit if 0)",
          warn=True)
    far = d.get("far") or {}
    check(res, far.get("live", 0) > 0 and not far.get("failed"), "far field tiles",
          f"live {far.get('live')} failed {far.get('failed')}")
    check(res, (d.get("tsstatics") or 0) > 0, "road / building shapes (.cdae)", f"{d.get('tsstatics')} objects")
    check(res, bool(d.get("credits")), "credits overlay text", (d.get("credits") or "none")[:90])
    check(res, (d.get("trees") or 0) > 0, "trees (Forest items)", f"{d.get('trees')} items", warn=True)
    check(res, bool(d.get("aiPlanLine")), "winter AI planning-horizon patch (ai.lua getMinPlanLen)",
          f"stock text at line {d.get('aiPlanLine')}" if d.get("aiPlanLine") else
          "the stock function changed: winter.lua AI_STOCK_FN needs the new text (until then traffic is speed-capped on "
          "snow and ice instead)", warn=True)
    # the game's log
    log = os.path.join(a.user_dir, "beamng.log")
    try:
        text = open(log, encoding="utf-8", errors="replace").read()
    except OSError:
        text = ""
    # the game packs its own terrain textures once after an update (new files, no cache yet): only our base photos'
    # keys count (terraincache.base_key of every *_base*.png in the session folders)
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server"))
    import glob
    import terraincache
    ours = set()
    for p in glob.glob(os.path.join(a.user_dir, "temp", "beamngEarth", "*", "*_base*.png")):
        rel = "/temp/beamngEarth/" + os.path.basename(os.path.dirname(p)) + "/" + os.path.basename(p)
        ours.add(terraincache.base_key(rel, "/levels/beamng_earth/art/terrains/bngEarth_base_ao.png").upper())
    packed = re.findall(r"Texture added for packing[^/]*/temp/art/terrainMaterialCache/([0-9A-Fa-f]{16})\.dds", text)
    mine = [k for k in packed if k.upper() in ours]
    check(res, not mine, "terrain texture cache keys (terraincache.py)",
          (f"our photos all from the cache ({len(packed)} of the game's own textures packed once)" if not mine else
           f"{len(mine)} of our base photos re-encoded by the game: the cache key changed (a freeze per block move)"))
    # (detail_ground.dds: "only RGB formats are supported!" is logged and the texture loads anyway, since 2026-10-10)
    errs = [ln for ln in text.splitlines() if "|E|" in ln and "beamngEarth" in ln and "detail_ground.dds" not in ln]
    check(res, not errs, "mod errors in the log", "none" if not errs else f"{len(errs)}, first: {errs[0][:140]}")
    # a shape's material that no loaded materials file maps draws orange "NO TEXTURE" (0.40 moved tro_tree_1's vines
    # into a materials file of their own; trees.lua MATERIALS lists the files). Only shapes loaded this session show.
    nomat = sorted({m.group(1) for m in re.finditer(r"\[NO-MATERIAL\] Unable to find material mapping to: (\S+)", text)})
    check(res, not nomat, "shapes' materials mapped (trees.lua MATERIALS)",
          "none missing" if not nomat else f"{len(nomat)} missing (orange NO TEXTURE): " + ", ".join(nomat[:8]))
    shape = [ln for ln in text.splitlines() if ("Unable to load" in ln or "invalid detail" in ln.lower())
             and "/temp/beamngEarth/" in ln]
    check(res, not shape, "shape files load", "no load errors" if not shape else f"{len(shape)}: {shape[0][:140]}")
    # every engine patch (tools/gcpatch on Linux, enginepatch*.py on Windows): after a game update a patch that no longer
    # finds its code says "not applied: another game build (...)" (0.40.6 dropped terfast and texkeep that way)
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    import launcher
    for name, what in launcher.ENGINE_PATCHES.items():
        try:
            ptxt = open(os.path.join(a.user_dir, "temp", f"beamngEarth_{name}.txt")).read().strip()
        except OSError:
            ptxt = "no status file (not started by the launcher, or a Linux-only patch on Windows)"
        state, _ = launcher.patch_state(ptxt)
        check(res, state == "on", f"engine patch {name} ({what})", ptxt[:160], warn=state != "failed")
    print(f"\n{res.count('PASS')} pass, {res.count('WARN')} warn, {res.count('FAIL')} fail")
    return 1 if "FAIL" in res else 0


if __name__ == "__main__":
    sys.exit(main())
