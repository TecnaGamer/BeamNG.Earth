#!/usr/bin/env python3
"""sheetview.py LAT LON HALF_M OUT.png --cache DIR [--server DIR] [--offline] [--aerial IMG] [--title T] [--r R]

Road revamp v2 phase P2b's look at the bridge decks round a point as the tiles draw them (structprobe.collect: the
road tiles built the way the server does, BNE_* switches from the environment) over the finest aerial
(tools/ortho_hi.py, fetched once as OUT_aerial.jpg unless --aerial): the drawn deck top triangles (DECK_TOP, the slab
under the asphalt) filled per triangle, raster points where two tops lie within structprobe.FACE_DZ (z-fighting)
in magenta, the parapets as red lines, each bridge member's centre line in white. Prints structprobe's summary for
R (default HALF_M) plus the drawn tops' height steps (structprobe.top_steps). A developer's look; nothing in the game
reads it. The cache must be a scratch cache (structprobe refuses the player's)."""
import argparse
import math
import os
import subprocess
import sys

import numpy as np

R_EARTH = 6378137.0


def merc(lat, lon):
    return R_EARTH * math.radians(lon), R_EARTH * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("lat", type=float)
    ap.add_argument("lon", type=float)
    ap.add_argument("half", type=float)
    ap.add_argument("out")
    ap.add_argument("--cache", required=True)
    ap.add_argument("--server")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--aerial")
    ap.add_argument("--title", default="")
    ap.add_argument("--r", type=float)
    ap.add_argument("--dump", help="pickle the probe's inputs here (tops, ribbons, parapets, members, pads)")
    a = ap.parse_args()
    here = os.path.dirname(os.path.abspath(__file__))
    srv = os.path.realpath(a.server or os.path.join(here, "..", "server"))
    sys.path.insert(0, srv)
    import structprobe as sp
    from PIL import Image, ImageDraw
    cache = sp._refuse_player_cache(a.cache)
    import structures
    structures.ACCEPT_SHORT = True
    R = a.r or a.half
    import roadshape
    seen = {}
    orig = roadshape.deck_sheets

    def sheets(parts, **kw):                # (the P2b records the tiles made, printed once each)
        recs, dz = orig(parts, **kw)
        for r in recs:
            k = (str(parts[r["t"]][0])[:10], str(parts[r["b"]][0])[:10], len(r["cover"]))
            seen.setdefault(k, (r["side"], round(r["s_b"], 1), round(r["gap"], 2), round(r["dz"], 2), r["parted"],
                                [round(float(v), 1) for v in r["at"]]))
        return recs, dz
    roadshape.deck_sheets = sheets
    inp = sp.collect(a.lat, a.lon, R + 60.0, cache, a.offline)
    for k in sorted(seen):
        print("sheet", k, seen[k])
    frame = inp["frame"]
    if a.dump:
        import pickle
        with open(a.dump, "wb") as f:
            pickle.dump({k: v for k, v in inp.items() if k != "frame"}, f)

    def frame_ll(p):
        la, lo, _ = frame.enu_to_geodetic(np.array([float(p[0])]), np.array([float(p[1])]), 0.0)
        return [round(float(la[0]), 6), round(float(lo[0]), 6)]
    res = sp.score(inp, R, frame_ll)
    st = sp.top_steps(inp["tops"], R, frame_ll)
    s = res["summary"]
    for e in inp["errors"][:3]:
        print("error", e)
    print(f"overlap_m2 {s['overlap_m2']} (pad {res['overlap_pad_m2']}) hole_m2 {s['hole_m2']} "
          f"parapet_in_cw_m {s['parapet_in_cw_m']} top_step n {st['n']} m {st['m']} max {st['max']} "
          f"deck_tris {s['deck_tris']} errors {len(inp['errors'])}")
    aerial = a.aerial or os.path.splitext(a.out)[0] + "_aerial.jpg"
    if not os.path.exists(aerial):
        subprocess.run([sys.executable, os.path.join(here, "ortho_hi.py"), repr(a.lat), repr(a.lon), repr(a.half),
                        aerial], check=True)
    img = Image.open(aerial).convert("RGB")
    W, H = img.size
    k = 1.0 / math.cos(math.radians(a.lat))
    cx, cy = merc(a.lat, a.lon)
    x0, y0, x1, y1 = cx - a.half * k, cy - a.half * k, cx + a.half * k, cy + a.half * k

    def px(xy):
        xy = np.asarray(xy, np.float64).reshape(-1, 2)
        la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
        out = []
        for p, q in zip(np.atleast_1d(la), np.atleast_1d(lo)):
            x, y = merc(float(p), float(q))
            out.append(((x - x0) / (x1 - x0) * W, (y1 - y) / (y1 - y0) * H))
        return out
    over = Image.new("RGBA", img.size, (0, 0, 0, 0))
    dr = ImageDraw.Draw(over)
    tops = np.asarray(inp["tops"], np.float64)
    near = (np.abs(tops[:, :, 0]).min(1) < a.half * 1.2) & (np.abs(tops[:, :, 1]).min(1) < a.half * 1.2)
    for t in tops[near]:
        p = px(t[:, :2])
        dr.polygon(p, fill=(80, 160, 255, 60), outline=(255, 255, 0, 110))
    tk, tz, _ = sp.raster_tris(tops[near])
    ov = sp.overlap_keys(tk, tz)
    for q in px(sp.key_xy(ov)) if len(ov) else ():
        dr.rectangle([q[0] - 1, q[1] - 1, q[0] + 1, q[1] + 1], fill=(255, 0, 255, 200))
    sk = st["keys"]
    for q in px(sp.key_xy(sk)) if len(sk) else ():
        dr.rectangle([q[0] - 1, q[1] - 1, q[0] + 1, q[1] + 1], fill=(255, 128, 0, 230))
    for m in inp["members"]:
        dr.line(px(m["xy"]), fill=(255, 255, 255, 200), width=1)
    for edge, _z in inp["parapets"]:
        dr.line(px(edge), fill=(255, 30, 30, 255), width=3)
    out = Image.alpha_composite(img.convert("RGBA"), over)
    pane = Image.new("RGBA", (W, 40), (0, 0, 0, 255))
    pd = ImageDraw.Draw(pane)
    pd.text((6, 4), f"{a.title}  {a.lat:.5f},{a.lon:.5f} +-{a.half:g} m  {os.environ.get('BNE_ROADSHAPE', 'off')}",
            fill=(255, 255, 255))
    pd.text((6, 20), f"overlap {s['overlap_m2']} m2  hole {s['hole_m2']} m2  parapet in cw {s['parapet_in_cw_m']} m  "
                     f"top steps {st['m']} m (max {st['max']} m)  [magenta z-fight, orange step, red parapet]",
            fill=(255, 255, 255))
    full = Image.new("RGBA", (W, H + 40))
    full.paste(out, (0, 0))
    full.paste(pane, (0, H))
    full.convert("RGB").save(a.out)
    print(a.out)
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
