#!/usr/bin/env python3
"""Game-speed and frame-pacing A/B on a fixed drive (the user, 2026-11-11: "Consistency is more important than max
frame rate"). Drives tools/nyc_speed.py's Manhattan route with drive.py's per-frame driver (the ball prop) while
tools/framerec.lua records every frame (wall clock, dtReal / dtSim / dtRaw, speed) and cycles variants every --seg
seconds, so each variant sees the same streets and the same moment of the session (repeated runs in one session
drift: game-perf-measurement notes). A variant may set the dtsmooth filter (the preload library's test control file:
the game started with BNGEARTH_DTSMOOTH_CTL=1) and the mod's frame pacer.

  python3 tools/framerec.py run --out /tmp/fr --mph 35 --km 6 --set dt      (the dt filter variants, pacer off)
  python3 tools/framerec.py run --out /tmp/fr --mph 35 --km 6 --set pacer   (pacer variants, the new filter)
  python3 tools/framerec.py analyze /tmp/fr/frames.csv --variants /tmp/fr/variants.json

Metrics per variant (frames at >= 90 % of the nominal speed, the first --skip s of each segment left out):
  speed p1/p5/p95/p99: game time / real time over 0.25 s windows (dtSim against the wall clock; 1.00 = steady);
  game/real: over the variant's whole time; fps; frame time p50/p90/p99/p99.9; jitter p95: |frame time - the previous|;
  >33 / >50 / >100: frames per minute over those ms.
"""
import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

USER = os.environ.get("BNGEARTH_USER_DIR", os.path.expanduser("~/.local/share/BeamNG/BeamNG.drive/current"))
MPH = 0.44704
LAG = 2                 # frames between a frame interval and the physics step that uses it (analyze)


def ctl(mode, hitch=0.010, rel=1.0, tau=4.0, repay=0.10, dmax=0.5, n=3, top_n=16):
    """A line of the dtsmooth test control file (bngearth_dtsmooth.c read_ctl; defaults: bnge_dtf_defaults)."""
    return f"{mode} {hitch} {rel} {tau} {repay} {dmax} {n} {top_n}"


SETS = {
    # the dt filter, pacer off: stock engine, the 2026-11-03 cap (re-implemented), the new filter dropping / repaying
    "dt": [
        {"name": "stock", "ctl": ctl(0), "pacer": "off"},
        {"name": "oldcap", "ctl": ctl(2, hitch=0.010), "pacer": "off"},
        {"name": "lock-drop", "ctl": ctl(1, repay=0.0), "pacer": "off"},
        {"name": "lock-r10", "ctl": ctl(1), "pacer": "off"},
        {"name": "lock-nocap", "ctl": ctl(1, hitch=2.0), "pacer": "off"},
    ],
    # the pacer, with the new filter
    "pacer": [
        {"name": "pacer-off", "ctl": ctl(1), "pacer": "off"},
        {"name": "pacer-p70", "ctl": ctl(1), "pacer": {"PERCENTILE": 0.70}},
        {"name": "pacer-p50", "ctl": ctl(1), "pacer": {"PERCENTILE": 0.50}},
    ],
    # before / after: the 1.9 default (stock filter + percentile pacer) against the new filter with and without pacing
    "final": [
        {"name": "1.9: stock+p70", "ctl": ctl(0), "pacer": {"PERCENTILE": 0.70}},
        {"name": "stock, no pacer", "ctl": ctl(0), "pacer": "off"},
        {"name": "new, no pacer", "ctl": ctl(1), "pacer": "off"},
        {"name": "new+p70", "ctl": ctl(1), "pacer": {"PERCENTILE": 0.70}},
        {"name": "new+p50", "ctl": ctl(1), "pacer": {"PERCENTILE": 0.50}},
    ],
    # stock engine filter with the shipped pacer vs nothing (the 1.9 default against bare)
    "base": [
        {"name": "stock+p70", "ctl": ctl(0), "pacer": {"PERCENTILE": 0.70}},
        {"name": "stock-off", "ctl": ctl(0), "pacer": "off"},
    ],
}


def mcp_lua(code, timeout=60):
    import mcp
    return mcp.call("run_lua", {"code": code})


def run(a):
    import nyc_speed as ns
    from drive import lua_long
    variants = json.load(open(a.variants)) if a.variants else SETS[a.set]
    if a.only:
        keep = a.only.split(",")
        variants = [v for v in variants if v["name"] in keep]
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "variants.json"), "w") as f:
        json.dump(variants, f, indent=1)
    start, end = ns.ROUTES[a.route]
    ns.use_vehicle(a.vehicle)
    ns.teleport(*start, 0.0)
    path = ns.gps_path(start, end)
    prof = ns.profile(path, a.mph, a.km)
    with open(os.path.join(a.out, "profile.json"), "w") as f:
        json.dump(prof, f)
    ns.teleport(*path[0], ns.heading_of(path), wait_s=90)
    if a.settle:
        time.sleep(a.settle)
    rec = open(os.path.join(HERE, "framerec.lua")).read()
    print(mcp_lua(f"return loadstring({lua_long(rec)})()"))
    mcp_lua("be:setDynamicCollisionEnabled(false) return 1")
    cfg = json.dumps({"seg": a.seg, "variants": variants})
    print(mcp_lua(f"return FREC.start({lua_long(cfg)})"))
    t0 = time.time()
    try:
        subprocess.run([sys.executable, os.path.join(HERE, "drive.py"), "--out", os.path.join(a.out, "drive"),
                        "--profile", os.path.join(a.out, "profile.json"), "--max-s", "1800", "--poll", str(a.poll),
                        "--label", "framerec"], capture_output=True, text=True)
    finally:
        print(mcp_lua("return FREC.stop()"))
        mcp_lua("be:setDynamicCollisionEnabled(true) return 1")
    name = f"frec_{int(t0)}.csv"
    print(mcp_lua(f"return FREC.dump('/temp/{name}')"))
    src = os.path.join(USER, "temp", name)
    dst = os.path.join(a.out, "frames.csv")
    for _ in range(20):
        if os.path.exists(src):
            break
        time.sleep(0.5)
    shutil.move(src, dst)
    res = analyze(dst, variants, a.mph, a.skip)
    with open(os.path.join(a.out, "result.json"), "w") as f:
        json.dump(res, f, indent=1)
    print(table(res))


def pct(xs, p):
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, int(p * len(s)))]


def analyze(path, variants, mph=None, skip=1.5, win=0.25):
    rows = list(csv.DictReader(open(path)))
    t = [float(r["t_ms"]) for r in rows]
    # (the physics of frame k advance by the dtReal of frame k - 2, measured 2026-11-11: dtSim[k] follows dtReal[k-2],
    # correlation 0.998; so the game time of the interval ending at frame k is dtSim[k + LAG])
    ds0 = [float(r["dtSim"]) for r in rows]
    ds = ds0[LAG:] + [0.0] * LAG
    var = [int(r["variant"]) for r in rows]
    # moving: the speed averaged over ~61 frames (the scripted driver's ball reads +-20 % frame to frame when dtSim
    # varies: it places the prop by the dtSim it is handed, physics runs another; an artifact of the test driver)
    v0 = [float(r["speed"]) for r in rows]
    acc, v = [0.0], []
    for x in v0:
        acc.append(acc[-1] + x)
    for k in range(len(v0)):
        a, b = max(0, k - 30), min(len(v0), k + 31)
        v.append((acc[b] - acc[a]) / (b - a))
    # a hold (the mod's edge guard pauses physics while the world loads: 3+ frames without game time) is a break too,
    # with 0.5 s either side
    hold = [False] * len(rows)
    k = 0
    while k < len(rows):
        j = k
        while j < len(rows) and ds0[j] == 0:
            j += 1
        if j - k >= 3:
            for m in range(max(0, k - 30), min(len(rows), j + 30 + LAG)):
                hold[m] = True
        k = j + 1
    v = [0.0 if h else x for x, h in zip(v, hold)]
    nominal = mph * MPH if mph else max(v) * 0.95
    # segments: runs of one variant; frames kept from `skip` s after its start, moving at >= 90 % of nominal
    segs, i = [], 1
    while i < len(rows):
        j = i
        while j + 1 < len(rows) and var[j + 1] == var[i]:
            j += 1
        segs.append((i, j))
        i = j + 1
    out = {}
    for vi, vdef in enumerate(variants, 1):
        gaps, sp, steps, judder, game, wall = [], [], [], [], 0.0, 0.0
        for (s0, s1) in segs:
            if var[s0] != vi:
                continue
            k0 = s0
            while k0 <= s1 and t[k0] - t[s0] < skip * 1000:
                k0 += 1
            # frames of this segment at speed
            ks = [k for k in range(max(k0, 2), s1 + 1) if v[k] >= 0.8 * nominal]
            if len(ks) < 10:
                continue
            ks_set = set(ks)
            for k in ks:
                g = t[k] - t[k - 1]
                gaps.append(g)
                game += ds[k]
                wall += g
                if k - 1 in ks_set:
                    steps.append(abs(g - (t[k - 1] - t[k - 2])))
                if g < 40:          # judder: what the rendered frame advanced against the time it stood for
                    judder.append(ds0[k] - g)
            # 0.25 s windows wholly inside the kept run of frames (consecutive)
            run_ = []
            for k in ks:
                if run_ and k != run_[-1] + 1:
                    sp += windows(run_, t, ds, win)
                    run_ = []
                run_.append(k)
            sp += windows(run_, t, ds, win)
        if not gaps:
            continue
        mins = wall / 60000.0
        out[vdef["name"]] = {
            "minutes": round(mins, 2), "frames": len(gaps), "fps": round(len(gaps) / (wall / 1000.0), 1),
            "speed_p1": r3(pct(sp, 0.01)), "speed_p5": r3(pct(sp, 0.05)), "speed_p95": r3(pct(sp, 0.95)),
            "speed_p99": r3(pct(sp, 0.99)), "game_real": round(game / wall, 4),
            "ft_p50": r2(pct(gaps, 0.5)), "ft_p90": r2(pct(gaps, 0.9)), "ft_p99": r2(pct(gaps, 0.99)),
            "ft_p999": r2(pct(gaps, 0.999)), "jitter_p95": r2(pct(steps, 0.95)),
            "judder_rms": r2((sum(x * x for x in judder) / len(judder)) ** 0.5) if judder else None,
            "over33_pm": round(sum(g > 33.3 for g in gaps) / mins, 1),
            "over50_pm": round(sum(g > 50 for g in gaps) / mins, 1),
            "over100_pm": round(sum(g > 100 for g in gaps) / mins, 1),
        }
    return out


def windows(ks, t, ds, win):
    """Game speed over consecutive `win` s windows of the frames ks (non-overlapping)."""
    out = []
    i = 0
    while i < len(ks):
        w, g, j = 0.0, 0.0, i
        while j < len(ks) and w < win * 1000:
            k = ks[j]
            w += t[k] - t[k - 1]
            g += ds[k]
            j += 1
        if w >= win * 1000:
            out.append(g / w)
        i = j
    return out


def r2(x):
    return None if x is None else round(x, 2)


def r3(x):
    return None if x is None else round(x, 3)


def table(res):
    cols = ["minutes", "fps", "speed_p1", "speed_p5", "speed_p95", "speed_p99", "game_real", "ft_p50", "ft_p90",
            "ft_p99", "ft_p999", "jitter_p95", "judder_rms", "over33_pm", "over50_pm", "over100_pm"]
    lines = ["| variant | " + " | ".join(cols) + " |", "|" + "---|" * (len(cols) + 1)]
    for name, m in res.items():
        lines.append(f"| {name} | " + " | ".join(str(m.get(c)) for c in cols) + " |")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--out", required=True)
    r.add_argument("--set", choices=sorted(SETS), default="dt")
    r.add_argument("--variants", help="a JSON list of variants instead of --set")
    r.add_argument("--only", help="comma-separated variant names of the set")
    r.add_argument("--route", default="uptown")
    r.add_argument("--mph", type=float, default=35.0)
    r.add_argument("--km", type=float, default=6.0)
    r.add_argument("--seg", type=float, default=20.0)
    r.add_argument("--skip", type=float, default=1.5)
    r.add_argument("--poll", type=float, default=5.0, help="drive.py's MCP poll interval (each poll is a slow frame)")
    r.add_argument("--settle", type=float, default=20.0, help="s to wait at the start before driving")
    r.add_argument("--vehicle", default="ball")
    z = sub.add_parser("analyze")
    z.add_argument("csv")
    z.add_argument("--variants", required=True)
    z.add_argument("--mph", type=float, default=35.0)
    z.add_argument("--skip", type=float, default=1.5)
    a = ap.parse_args()
    if a.cmd == "run":
        run(a)
    else:
        res = analyze(a.csv, json.load(open(a.variants)), a.mph, a.skip)
        print(table(res))


if __name__ == "__main__":
    main()
