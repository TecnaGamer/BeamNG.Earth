#!/usr/bin/env python3
"""structoverlay.py LAT LON HALF_M SHADOW_DIR OUT.png [--aerial IMG] [--title T]

The road revamp's structure groups (track RS, step S1: server/structures.py, BNE_DECK_STRUCT=shadow) drawn over the
finest aerial of the place (tools/ortho_hi.py, fetched once next to OUT as OUT_aerial.jpg unless --aerial is given):
every member of every chain JSON in SHADOW_DIR (a scratch cache's debug/structures/) within the square, its carriageway
width in its group's colour (one colour per group, a single deck thin and grey), conflicts as red crosses between the
pair (a never-union rule applied), a legend of the groups. Prints the groups it drew. A developer's look at the real
place (the IRL rule): nothing in the game reads it."""
import argparse
import glob
import json
import math
import os
import subprocess
import sys

from PIL import Image, ImageDraw

R = 6378137.0
COLOURS = [(230, 25, 75), (60, 180, 75), (255, 225, 25), (0, 130, 200), (245, 130, 48), (145, 30, 180),
           (70, 240, 240), (240, 50, 230), (210, 245, 60), (250, 190, 212), (0, 128, 128), (220, 190, 255),
           (170, 110, 40), (255, 250, 200), (128, 0, 0), (170, 255, 195), (128, 128, 0), (255, 215, 180)]


def merc(lat, lon):
    return R * math.radians(lon), R * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("lat", type=float)
    ap.add_argument("lon", type=float)
    ap.add_argument("half", type=float)
    ap.add_argument("shadow_dir")
    ap.add_argument("out")
    ap.add_argument("--aerial")
    ap.add_argument("--title", default="")
    a = ap.parse_args()
    aerial = a.aerial or os.path.splitext(a.out)[0] + "_aerial.jpg"
    if not os.path.exists(aerial):
        subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "ortho_hi.py"),
                        repr(a.lat), repr(a.lon), repr(a.half), aerial], check=True)
    img = Image.open(aerial).convert("RGB")
    W, H = img.size
    k = 1.0 / math.cos(math.radians(a.lat))
    cx, cy = merc(a.lat, a.lon)
    x0, y0, x1, y1 = cx - a.half * k, cy - a.half * k, cx + a.half * k, cy + a.half * k
    mpp = 2 * a.half / W                # ground m per pixel

    def px(ll):
        x, y = merc(ll[0], ll[1])
        return ((x - x0) / (x1 - x0) * W, (y1 - y) / (y1 - y0) * H)

    def inbox(p):
        return -0.1 * W <= p[0] <= 1.1 * W and -0.1 * H <= p[1] <= 1.1 * H
    groups, conflicts = {}, []
    for path in sorted(glob.glob(os.path.join(a.shadow_dir, "*.json"))):
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        if "members" not in doc:        # (a window's marker, a dropped-jobs marker)
            continue
        mem = {m["key"]: m for m in doc["members"]}
        for m in doc["members"]:
            if m["kind"] == "outside" or not m.get("ll") or m.get("dup_of"):      # (S2a: a duplicate drawn once)
                continue
            pts = [px(q) for q in m["ll"]]
            if not any(inbox(p) for p in pts):
                continue
            g = groups.setdefault(m["group"], {"members": {}, "doc": doc})
            g["members"][m["key"]] = (pts, m)
        for c in doc["conflicts"]:
            ma, mb = mem.get(c["a"]), mem.get(c["b"])
            if ma and mb and ma.get("ll") and mb.get("ll"):
                pa, pb = px(ma["ll"][len(ma["ll"]) // 2]), px(mb["ll"][len(mb["ll"]) // 2])
                if inbox(pa) or inbox(pb):
                    conflicts.append((pa, pb, c))
    over = Image.new("RGBA", img.size, (0, 0, 0, 0))
    dr = ImageDraw.Draw(over)
    order = sorted(groups, key=lambda g: (-len(groups[g]["members"]), g))
    legend = []
    ci = 0
    for gid in order:
        g = groups[gid]
        multi = len(g["members"]) > 1
        col = COLOURS[ci % len(COLOURS)] if multi else (200, 200, 200)
        ci += multi
        for key, (pts, m) in sorted(g["members"].items()):
            w = max(2, int(round(2 * (m["half"] or 0) / 1000.0 / mpp))) if multi else 3
            dr.line(pts, fill=col + (110 if multi else 160,), width=w, joint="curve")
            dr.line(pts, fill=col + (255,), width=2)
        if multi:
            gd = next(x for x in g["doc"]["groups"] if x["id"] == gid)
            kinds = sorted({e[0] for e in gd["edges"]})
            legend.append((col, f"{gid[:14]} x{len(g['members'])} {'/'.join(gd['classes'])} lv {gd['levels']} "
                                f"[{', '.join(kinds)}]"))
    for pa, pb, c in conflicts:
        mx, my = (pa[0] + pb[0]) / 2, (pa[1] + pb[1]) / 2
        dr.line([(mx - 7, my - 7), (mx + 7, my + 7)], fill=(255, 0, 0, 255), width=3)
        dr.line([(mx - 7, my + 7), (mx + 7, my - 7)], fill=(255, 0, 0, 255), width=3)
    out = Image.alpha_composite(img.convert("RGBA"), over)
    lh = 16
    pane = Image.new("RGBA", (W, lh * (len(legend) + 3) + 8), (0, 0, 0, 255))
    pd = ImageDraw.Draw(pane)
    pd.text((6, 4), f"{a.title}  {a.lat:.5f},{a.lon:.5f} +-{a.half:g} m  groups {len(legend)} (multi) / "
                    f"{len(groups) - len(legend)} single, conflicts (red x) {len(conflicts)}", fill=(255, 255, 255))
    for i, (col, txt) in enumerate(legend):
        pd.rectangle([6, 4 + lh * (i + 1), 18, 4 + lh * (i + 1) + 10], fill=col + (255,))
        pd.text((24, 4 + lh * (i + 1)), txt, fill=(255, 255, 255))
    full = Image.new("RGBA", (W, H + pane.size[1]))
    full.paste(out, (0, 0))
    full.paste(pane, (0, H))
    full.convert("RGB").save(a.out)
    print(a.out)
    for col, txt in legend:
        print(" ", txt)
    print(f"  single decks {len(groups) - len(legend)}, conflicts {len(conflicts)}: " +
          ", ".join(sorted({f"{c['reason']}/{c['kind']}" for _a, _b, c in conflicts})))


if __name__ == "__main__":
    main()
