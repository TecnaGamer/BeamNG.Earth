"""Run trackcheck.py over every track in a world list, resumable, a few at a time, below normal priority.

    python .claude/devtools/trackbatch.py TRACKS.csv OUTDIR [--workers 3] [--shuffle]

TRACKS.csv: name,country,lat,lon,length,wikidata (D:/Projects/BeamMaps/racetracks-world.csv). Per track OUTDIR gets
<n>.json (trackcheck --json) and <n>.log; a track with a tunnel under it is checked again with the digs off
(BNE_RACEWAY_DIG=off) and the racing surface compared (dig_diff_cm: a tunnel must not move the track above it at
all). OUTDIR/ranked.csv is rewritten after every track, worst first. A track whose json exists is skipped.
"""
import csv
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
CHECK = os.path.join(HERE, "trackcheck.py")
TIMEOUT_S = 3600         # s (Bonneville, Balocco and Shah Alam took over 1500: kilometres of raceway ways)
RADIUS_M = 2500          # m round the listed point (the list's points are a track's centre, give or take)
RADIUS_FAR_M = 6000      # m: a point with no raceway within RADIUS_M is tried again this far out (a list point off the
                         # track: Wikidata's coordinates are the venue's, or its town's), else "no raceway mapped"
LOCK = threading.Lock()


def low_priority():
    try:
        import psutil
        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if os.name == "nt" else 10)
    except Exception:
        pass


def run(args, env_extra, log):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", **env_extra)
    with open(log, "w", encoding="utf-8") as fh:
        try:
            r = subprocess.run([sys.executable, CHECK] + args, cwd=ROOT, env=env, stdout=fh, stderr=subprocess.STDOUT,
                               timeout=TIMEOUT_S, creationflags=getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0))
            return r.returncode
        except subprocess.TimeoutExpired:
            fh.write("\nTIMEOUT\n")
            return -1


def _rough(z, w=25):
    """|z - its w-sample running mean| (samples 1 m apart: trackcheck's roughness)."""
    k = np.ones(w) / w
    pad = np.pad(z, w // 2, mode="edge")
    return np.abs(z - np.convolve(pad, k, mode="valid")[:len(z)])


def dig_diff(a, b):
    """cm: how much rougher the racing surface is with the tunnels dug (a) than without (b), worst point, per way at
    equal samples. (Not the height difference: with the digs off the road under is at grade and the crossing pulls
    the track down to it, Losail's 3.4 m, so off is no reference for heights; a dig that disturbs the track shows as
    roughness only it has.)"""
    try:
        A, B = np.load(a), np.load(b)
    except Exception:
        return None
    worst = 0.0
    for k in A.files:
        if k in B.files and A[k].shape == B[k].shape and len(A[k]) > 25:
            za, zb = A[k][:, 2], B[k][:, 2]
            if not (np.isfinite(za).all() and np.isfinite(zb).all()):
                continue
            worst = max(worst, float(np.max(_rough(za) - _rough(zb))))
    return round(max(worst, 0.0) * 100, 1)


def score(j):
    """Worst first: the rules' severity (trackrules.severity: joins, stands, props, buildings, holes, vault),
    tunnel faults, then dig disturbance, then bumps."""
    t = j.get("tunnel_flags") or []
    return ((j.get("severity") or 0) + len(t) * 100 + (j.get("no_tube") or 0) * 100 + (j.get("dig_diff_cm") or 0) * 10 +
            (j.get("wheelbase_cm") or 0) + (j.get("built_solid") or 0) * 20 + (j.get("maxdev") or 0) * 50)


def rank(out, tracks):
    rows = []
    for n, t in enumerate(tracks):
        p = os.path.join(out, f"{n:04d}.json")
        if not os.path.exists(p):
            continue
        try:
            j = json.load(open(p))
        except Exception:
            continue
        rows.append((score(j), n, t, j))
    rows.sort(key=lambda r: -r[0])
    with LOCK, open(os.path.join(out, "ranked.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["score", "n", "name", "country", "lat", "lon", "tunnel_faults", "no_tube", "dig_diff_cm",
                    "wheelbase_cm", "wheelbase_at", "maxdev_m", "dxslope", "built_solid", "severity", "join", "seam", "join_step_m", "join_twist_m", "worst_join",
                    "stand_away", "stand_band", "stand_off", "stand_cover", "prop", "building", "holes", "vault",
                    "walls_min_share", "width_guess", "status"])
        for sc, n, t, j in rows:
            w.writerow([round(sc, 1), n, t["name"], t["country"], t["lat"], t["lon"],
                        ";".join(f"{'+'.join(f[0])}@{f[3][0]:.5f},{f[3][1]:.5f}" for f in j.get("tunnel_flags") or []),
                        j.get("no_tube"), j.get("dig_diff_cm"), j.get("wheelbase_cm"), j.get("wheelbase_at"),
                        j.get("maxdev"), j.get("dxslope"), j.get("built_solid"), j.get("severity"),
                        *[(j.get("flags") or {}).get(k, 0) for k in ("join", "seam")], j.get("join_step"),
                        j.get("join_twist"), j.get("worst_join"),
                        *[(j.get("flags") or {}).get(k, 0) for k in ("stand_away", "stand_band", "stand_off",
                                                                       "stand_cover", "prop", "building", "holes",
                                                                       "vault")],
                        min([w[0] for w in j.get("walls") or []] or [""], key=lambda v: (v == "", v)),
                        j.get("width_guess"), j.get("status", "ok")])


QUIET = ((21, 20), (22, 35))   # local time no new track starts (another job's nightly, 21:50-22:30; a track runs up to 25 min)


def wait_quiet():
    import time
    while True:
        now = time.localtime()
        hm = (now.tm_hour, now.tm_min)
        if not (QUIET[0] <= hm < QUIET[1]) or "--no-quiet" in sys.argv:
            return
        time.sleep(60)


def one(n, t, out, tracks):
    js = os.path.join(out, f"{n:04d}.json")
    if os.path.exists(js):
        return
    wait_quiet()
    args = [str(t["lat"]), str(t["lon"]), str(RADIUS_M)]
    dump_on = os.path.join(out, f"{n:04d}.on.npz")
    log = os.path.join(out, f"{n:04d}.log")
    rc = run(args + [f"--json={js}", f"--dump={dump_on}"], {}, log)
    if not os.path.exists(js) and "no raceways within" in open(log, encoding="utf-8", errors="replace").read():
        args = [str(t["lat"]), str(t["lon"]), str(RADIUS_FAR_M)]
        rc = run(args + [f"--json={js}", f"--dump={dump_on}"], {}, log)
        if not os.path.exists(js):
            if "no raceways within" in open(log, encoding="utf-8", errors="replace").read():
                json.dump({"status": "no raceway mapped"}, open(js, "w"))
        else:
            j = json.load(open(js))
            j["radius_m"] = RADIUS_FAR_M
            json.dump(j, open(js, "w"))
    if not os.path.exists(js):
        json.dump({"status": "timeout" if rc == -1 else f"error {rc}"}, open(js, "w"))
    else:
        j = json.load(open(js))
        if j.get("under"):
            dump_off = os.path.join(out, f"{n:04d}.off.npz")
            run(args + [f"--dump={dump_off}"], {"BNE_RACEWAY_DIG": "off"}, os.path.join(out, f"{n:04d}.off.log"))
            j["dig_diff_cm"] = dig_diff(dump_on, dump_off)
            json.dump(j, open(js, "w"))
    rank(out, tracks)
    print(f"{n:04d} {t['name']}", flush=True)


def main():
    src, out = sys.argv[1], sys.argv[2]
    workers = int(sys.argv[sys.argv.index("--workers") + 1]) if "--workers" in sys.argv else 3
    os.makedirs(out, exist_ok=True)
    low_priority()
    with open(src, newline="", encoding="utf-8") as fh:
        tracks = list(csv.DictReader(fh))
    order = list(range(len(tracks)))
    if "--shuffle" in sys.argv:
        import random
        random.Random(1).shuffle(order)
    with ThreadPoolExecutor(workers) as ex:
        for n in order:
            ex.submit(one, n, tracks[n], out, tracks)
    rank(out, tracks)


if __name__ == "__main__":
    main()
