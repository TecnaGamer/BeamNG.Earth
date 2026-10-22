#!/usr/bin/env python3
"""Check a Street View camera's pose against the map before trusting a comparison: the BGT's light masts (8 m) and
the OSM signals (3 m) projected with the view's camera (compare/views.json: place, heading, tilt, vertical fov; the
camera CAM_H over the AHN's ground) onto the reference, to compare/proj_<id>.jpg. Where the drawn poles stand on the
photo's poles the pose is right; a sideways shift of all of them is a heading error, a spread a place error.

    python3 tools/compare_project.py ../design/kunstwerken/droogleever [--dh DEG] [--dx M] [--dy M]
"""
import argparse
import json
import math
import os
import sys

import numpy as np
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))
CAM_H = 2.5


def project(P, cam, heading, tilt, fov_v, w, h):
    """World points (k, 3) to pixels (k, 2) and a mask of those in front."""
    hd, pt = math.radians(heading), math.radians(tilt - 90)
    f = np.array([math.sin(hd) * math.cos(pt), math.cos(hd) * math.cos(pt), math.sin(pt)])
    r = np.array([math.cos(hd), -math.sin(hd), 0.0])
    u = np.cross(r, f)
    d = P - cam
    zc, xc, yc = d @ f, d @ r, d @ u
    fpx = (h / 2) / math.tan(math.radians(fov_v) / 2)
    ok = zc > 0.5
    return np.c_[w / 2 + fpx * xc / np.maximum(zc, 1e-6), h / 2 - fpx * yc / np.maximum(zc, 1e-6)], ok


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("kunstwerk")
    ap.add_argument("--dh", type=float, default=0.0)
    ap.add_argument("--dx", type=float, default=0.0)
    ap.add_argument("--dy", type=float, default=0.0)
    ap.add_argument("--only")
    a = ap.parse_args(argv)
    sys.path.insert(0, HERE)
    from kunstwerk_build import Ground
    import ahn
    ref_dir = os.path.join(a.kunstwerk, "ref")
    ref = json.load(open(os.path.join(ref_dir, "ref.json"), encoding="utf-8"))
    g = Ground(ref_dir)
    poles = [np.asarray(f["rings"][0][0], np.float64).reshape(-1)[:2] for f in ref["features"]
             if f["layer"] == "paal" and f.get("functie") in ("lichtmast", "portaal")]
    o = ref["origin_rd"]
    sigs = []
    for e in json.load(open(os.path.join(ref_dir, "osm_signals.json")))["elements"]:
        if e["tags"].get("highway") == "traffic_signals":
            x, y = ahn.wgs_to_rd(e["lat"], e["lon"])
            sigs.append(np.array([x - o[0], y - o[1]]))
    folder = os.path.join(a.kunstwerk, "compare")
    for v in json.load(open(os.path.join(folder, "views.json"))):
        if a.only and v["id"] not in a.only.split(","):
            continue
        cx, cy = v["x"] + a.dx, v["y"] + a.dy
        z_ground = v.get("ground", float(g.at(cx, cy)))        # (the model's surface there: on a deck the lidar's
        cam = np.array([cx, cy, z_ground + CAM_H])                # ground is the trench under it)
        im = Image.open(os.path.join(folder, f"ref_{v['id']}.jpg")).convert("RGB")
        d = ImageDraw.Draw(im)
        for pts, top, col in ((poles, 8.0, (255, 0, 255)), (sigs, 3.0, (0, 255, 255))):
            for p in pts:
                if np.hypot(*(p - cam[:2])) > 120:
                    continue
                z0 = float(g.at(*p))
                uv, ok = project(np.array([[*p, z0], [*p, z0 + top]]), cam, v["heading"] + a.dh, v["tilt"],
                                 v["fov"], v["w"], v["h"])
                if ok.all():
                    d.line([tuple(uv[0]), tuple(uv[1])], fill=col, width=3)
        im.save(os.path.join(folder, f"proj_{v['id']}.jpg"), quality=90)
        print(v["id"], "ground", round(float(cam[2] - CAM_H), 2))


if __name__ == "__main__":
    main()
