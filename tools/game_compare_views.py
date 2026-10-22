#!/usr/bin/env python3
"""The game's own view from Street View's spots (compare/views.json, tools/blender_compare_views.py's), for checking a
kunstwerk in the game itself: per view the free camera at the panorama's place (CAM_H over the ground the game has
there), its heading and tilt, a vertical field of view as the reference's, a screenshot, copied to
compare/game_<id>.png. The game runs the BeamNG.Earth level at the anchor ANCHOR (lat, lon); its frame is that place's
east-north-up.

    python3 tools/game_compare_views.py ../design/kunstwerken/droogleever/compare --anchor 51.90849367,4.46599907
"""
import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CAM_H = 2.5
POS = {"A": (51.90849367, 4.46599907), "B": (51.90842595, 4.4654976), "C": (51.90849514, 4.4667234),
       "D": (51.9082834, 4.46669429)}


def lua(code):
    r = subprocess.run([sys.executable, os.path.join(HERE, "mcp.py"), "lua", code], capture_output=True, text=True)
    return r.stdout.strip()


def call(tool, args):
    r = subprocess.run([sys.executable, os.path.join(HERE, "mcp.py"), "call", tool, json.dumps(args)],
                       capture_output=True, text=True)
    return r.stdout.strip()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("compare")
    ap.add_argument("--anchor", required=True)
    ap.add_argument("--only")
    ap.add_argument("--settle", type=float, default=8.0, help="s to wait at each view for the tiles to stream")
    a = ap.parse_args(argv)
    la0, lo0 = (float(v) for v in a.anchor.split(","))
    for v in json.load(open(os.path.join(a.compare, "views.json"))):
        if a.only and v["id"] not in a.only.split(","):
            continue
        la, lo = POS[v["id"][0]]
        x = (lo - lo0) * math.cos(math.radians(la0)) * 111320.0
        y = (la - la0) * 110574.0
        hd, pt = math.radians(v["heading"]), math.radians(v["tilt"] - 90)
        d = (math.sin(hd) * math.cos(pt), math.cos(hd) * math.cos(pt), math.sin(pt))
        # (the camera's horizontal fov for the screen's aspect, from the reference's vertical one)
        code = f"""
local ok, res = pcall(function()
local x, y = {x:.3f}, {y:.3f}
local hit = castRayStatic(vec3(x, y, 200), vec3(0, 0, -1), 400)
local z = {v.get("ground", 3.5):.3f}      -- (the model's surface there; a ray may pass a deck still streaming)
local rot = quatFromDir(vec3({d[0]:.5f}, {d[1]:.5f}, {d[2]:.5f}), vec3(0, 0, 1))
commands.setFreeCamera()
core_camera.setPosRot(0, x, y, z + {CAM_H}, rot.x, rot.y, rot.z, rot.w)
local w, h = 16, 9
local hfov = 2 * math.deg(math.atan(math.tan(math.rad({v['fov'] / 2:.3f})) * w / h))
if core_camera.setFOV then core_camera.setFOV(0, hfov) elseif setCameraFovDeg then setCameraFovDeg(hfov) end
return string.format('%.2f %.2f %.2f fov %.1f', x, y, z, hfov)
end)
return tostring(res)"""
        print(v["id"], lua(code))
        time.sleep(a.settle)
        shot = call("screenshot", {"jpg": False})
        path = None
        for tok in shot.replace('"', " ").split():
            if tok.lower().endswith(".png"):
                for _ in range(20):                 # (the game writes it a few frames later)
                    if os.path.exists(tok) and os.path.getsize(tok) > 0:
                        path = tok
                        break
                    time.sleep(0.5)
        if path is None:
            print("  no screenshot:", shot[:200])
            continue
        shutil.copy(path, os.path.join(a.compare, f"game_{v['id']}.png"))
        print("  ->", f"game_{v['id']}.png")


if __name__ == "__main__":
    main()
