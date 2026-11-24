"""Plan views for the degradation test: aerial | full data (D0) | a degraded step, the same square, north up.

    chrt -i 0 python3 tools/widecorpus/degrade_views.py --run baseline --places warsaw-rondo-dmowskiego --steps D1,D2

Generated panels: the drawn road surface (terrain paint and road meshes) grey, deck tops orange, markings white and
yellow, each road's centre line coloured by its painted lane count (1 blue, 2 green, 3 yellow, 4 orange, 5+ red),
the measured tile outlined. The aerial is tools/ortho_hi.py's finest public orthophoto (run with HOME in the scratch
folder). Images go to RUN/views/PLACE_STEP.jpg."""
import os
import sys
from pathlib import Path

J1 = Path.home() / "bnge-tests/j1degrade"
os.environ.setdefault("WIDE_CORPUS_ROOT", str(J1))

import argparse  # noqa: E402
import subprocess  # noqa: E402

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from common import ROOT, TREE, places  # noqa: E402
import degrade  # noqa: E402
import degrade_metrics as dm  # noqa: E402

LANE_RGB = {1: (60, 120, 255), 2: (40, 200, 60), 3: (240, 220, 40), 4: (255, 140, 0)}
PX = 900


def box_of(build):
    q = np.concatenate([t["quad"] for t in build.tiles.values()])
    lo, hi = q.min(0), q.max(0)
    c = (lo + hi) / 2
    half = float(max(hi - lo) / 2 + 10)
    return c, half


def render(build, c, half, px=PX):
    """The generated panel of a build: (PIL image)."""
    res = 2 * half / px
    im = Image.new("RGB", (px, px), (34, 40, 34))
    d = ImageDraw.Draw(im)

    def to_px(xy):
        xy = np.asarray(xy, float)
        return np.column_stack(((xy[:, 0] - (c[0] - half)) / res, (c[1] + half - xy[:, 1]) / res))
    lo = np.array([c[0] - half, c[1] - half])
    for key in sorted(build.tiles):
        surf, tops, floors, terrain, marks = build.surface(key)
        grid = dm.paint_mask(terrain, dm.raster(np.zeros((0, 3, 3)), np.array([lo, lo + 2 * half]), res=1.0, pad=0))
        mask, glo, gres = grid
        ys, xs = np.nonzero(mask)
        for x, y in zip(xs.tolist(), ys.tolist()):
            p = to_px([[glo[0] + x * gres, glo[1] + y * gres], [glo[0] + (x + 1) * gres, glo[1] + (y + 1) * gres]])
            d.rectangle([p[0, 0], p[1, 1], p[1, 0], p[0, 1]], fill=(105, 105, 105))
        for tri, col in ((surf, (105, 105, 105)), (tops, (230, 130, 40))):
            for t in tri:
                d.polygon([tuple(v) for v in to_px(t[:, :2]).tolist()], fill=col)
        for mat, tri in marks:
            col = (235, 200, 40) if "yellow" in mat else (240, 240, 240)
            for t in tri:
                d.polygon([tuple(v) for v in to_px(t[:, :2]).tolist()], fill=col)
    for key in sorted(build.tiles):
        for road in build.tiles[key]["specs"]:
            if road["cls"] in dm.NOT_DRIVEN or road["level"] == "tunnel":
                continue
            col = LANE_RGB.get(int(road["lanes"]), (230, 40, 40))
            d.line([tuple(v) for v in to_px(road["xy"]).tolist()], fill=col, width=2)
        q = to_px(build.tiles[key]["quad"])
        d.line([tuple(v) for v in np.vstack((q, q[:1])).tolist()], fill=(0, 220, 255), width=1)
    return im


def aerial(p, c, half, out, px=PX):
    """tools/ortho_hi.py's orthophoto of the square, or None."""
    import geodesy
    if out.is_file():
        return Image.open(out).convert("RGB").resize((px, px))
    fr = geodesy.EnuFrame(p["lat"], p["lon"])
    la, lo, _ = fr.enu_to_geodetic(np.array([c[0]]), np.array([c[1]]), 0.)
    home = ROOT / "views-home"
    home.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["chrt", "-i", "0", sys.executable, str(TREE / "tools/ortho_hi.py"), f"{la[0]:.7f}",
                        f"{lo[0]:.7f}", f"{half:.1f}", str(out), "auto", str(px)],
                       env=dict(os.environ, HOME=str(home)), capture_output=True, text=True, timeout=900)
    if not out.is_file():
        print(r.stdout[-300:], r.stderr[-300:])
        return None
    return Image.open(out).convert("RGB").resize((px, px))


def sheet(run, p, step, px=PX):
    out = run / p["id"]
    full = dm.Build(out / "D0", p)
    deg = dm.Build(out / step, p)
    c, half = box_of(full)
    views = run / "views"
    views.mkdir(exist_ok=True)
    a = aerial(p, c, half, views / f"aerial_{p['id']}.jpg", px)
    panels = [a or Image.new("RGB", (px, px), (0, 0, 0)), render(full, c, half, px), render(deg, c, half, px)]
    im = Image.new("RGB", (3 * px + 20, px + 40), (255, 255, 255))
    for i, pan in enumerate(panels):
        im.paste(pan, (i * (px + 10), 40))
    d = ImageDraw.Draw(im)
    d.text((8, 6), f"{p['id']}: aerial | full data (D0) | {step} {degrade.NAMES[step]}   ({2 * half:.0f} m square; "
                   "centre lines by painted lanes: 1 blue, 2 green, 3 yellow, 4 orange, 5+ red; tile cyan)",
           fill=(0, 0, 0))
    path = views / f"{p['id']}_{step}.jpg"
    im.save(path, quality=88)
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", default="baseline")
    ap.add_argument("--places", required=True)
    ap.add_argument("--steps", default="D1,D2")
    args = ap.parse_args()
    run = ROOT / "runs" / args.run
    for p in places(args.places):
        for step in args.steps.split(","):
            print(sheet(run, p, step), flush=True)


if __name__ == "__main__":
    sys.exit(main())
