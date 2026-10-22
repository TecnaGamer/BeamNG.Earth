#!/usr/bin/env python3
"""Side by side for checking a kunstwerk against Street View: per view in compare/views.json the reference
(ref_<id>.jpg) over the Blender render from the same spot (render_<id>.png, tools/blender_compare_views.py), with a
10 % grid over both so a difference can be named by its place, to compare/sheet_<id>.jpg.

    python3 tools/compare_sheet.py ../design/kunstwerken/droogleever/compare
"""
import json
import os
import sys

from PIL import Image, ImageDraw


def main(folder):
    for v in json.load(open(os.path.join(folder, "views.json"))):
        ref = os.path.join(folder, f"ref_{v['id']}.jpg")
        ren = os.path.join(folder, f"render_{v['id']}.png")
        if not (os.path.exists(ref) and os.path.exists(ren)):
            continue
        a, b = Image.open(ref).convert("RGB"), Image.open(ren).convert("RGB").resize((v["w"], v["h"]))
        s = Image.new("RGB", (v["w"], v["h"] * 2 + 24), (20, 20, 20))
        s.paste(a, (0, 0))
        s.paste(b, (0, v["h"] + 24))
        d = ImageDraw.Draw(s)
        for top in (0, v["h"] + 24):
            for i in range(1, 10):
                x = v["w"] * i // 10
                d.line([(x, top), (x, top + v["h"])], fill=(255, 255, 0), width=1)
                d.text((x + 3, top + 3), f"{i * 10}%", fill=(255, 255, 0))
        d.text((6, v["h"] + 5), f"{v['id']}: above Street View (reference), below the model (Blender)", fill=(255, 255, 255))
        s.save(os.path.join(folder, f"sheet_{v['id']}.jpg"), quality=92)
        print(v["id"])


if __name__ == "__main__":
    main(sys.argv[1])
