"""Run inside Blender: render the kunstwerk from Street View's own spots (compare/views.json: the panorama's place in
the kunstwerk's local metres, its heading clockwise from north, its tilt (90: level) and field of view) to
compare/render_<id>.png, the same size as the reference compare/ref_<id>.jpg, for comparing them side by side
(tools/compare_sheet.py). Set COMPARE (the compare folder) first; FOV_AXIS "vertical" or "horizontal" (which way the
view's fov runs), CAM_H the camera's height over the ground (Street View's car: about 2.5 m), ONLY a list of ids."""
import json
import math
import os

import bpy
import mathutils

FOV_AXIS = globals().get("FOV_AXIS", "vertical")
CAM_H = globals().get("CAM_H", 2.5)
ONLY = globals().get("ONLY")
SUFFIX = globals().get("SUFFIX", "")

views = json.load(open(os.path.join(COMPARE, "views.json")))  # noqa: F821  (set by the caller)
sc = bpy.context.scene
for c in ("REF", "REF_BGT_L-1", "REF_BGT_L0", "REF_BGT_L1", "Collection", "KIT"):
    if c in bpy.data.collections:
        bpy.data.collections[c].hide_render = True
cam = bpy.data.objects["REF_cam"]
sc.camera = cam
dg = bpy.context.evaluated_depsgraph_get()
done = []
for v in views:
    if ONLY and v["id"] not in ONLY:
        continue
    hit, loc, *_ = sc.ray_cast(dg, mathutils.Vector((v["x"], v["y"], 60.0)), mathutils.Vector((0, 0, -1)))
    z = (loc.z if hit else 3.5) + CAM_H
    cam.location = (v["x"], v["y"], z)
    cam.rotation_euler = (math.radians(v["tilt"]), 0.0, math.radians(-v["heading"]))
    cam.data.lens_unit = "FOV"
    cam.data.sensor_fit = "VERTICAL" if FOV_AXIS == "vertical" else "HORIZONTAL"
    cam.data.angle = math.radians(v["fov"])
    cam.data.clip_start, cam.data.clip_end = 0.1, 3000
    sc.render.resolution_x, sc.render.resolution_y = v["w"], v["h"]
    sc.render.resolution_percentage = 100
    sc.render.filepath = os.path.join(COMPARE, f"render_{v['id']}{SUFFIX}.png")
    bpy.ops.render.render(write_still=True)
    done.append((v["id"], round(z, 2)))
result = done
