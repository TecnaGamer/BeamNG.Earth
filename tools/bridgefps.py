"""bridgefps.py DRIVE_DIR [DRIVE_DIR ...]: frame times on bridges, in tunnels and on the ground, from a drive.py run.

Each second's poll (drive/samples.jsonl: the car's z and the terrain height under it, drive_driver.lua) classes that
second's frames (drive/frames.jsonl): "bridge" when the car is more than CLEAR_M above the terrain, "tunnel" when it
is more than CLEAR_M below, else "ground". A Discord report (2026-11-05): "Bridges and tunnels are extream laggy".
"""
import json
import os
import sys

CLEAR_M = 4.0


def pct(v, q):
    v = sorted(v)
    return v[min(len(v) - 1, int(q * len(v)))] if v else float("nan")


def classes(run):
    samples = [json.loads(line) for line in open(os.path.join(run, "samples.jsonl")) if line.strip()]
    frames = [json.loads(line) for line in open(os.path.join(run, "frames.jsonl")) if line.strip()]
    where = {}
    for s in samples:
        d = s.get("drive") or {}
        z, terr = d.get("z"), d.get("terr")
        if z is None or terr is None or terr < -60:         # (no block under it: getTerrainHeight's -64)
            continue
        dz = z - terr
        where[round(s["t"])] = "bridge" if dz > CLEAR_M else "tunnel" if dz < -CLEAR_M else "ground"
    out = {}
    for f in frames:
        k = where.get(round(f["t"]))
        if k is None:
            continue
        out.setdefault(k, []).extend(f["ms"])
    return out


def main():
    total = {}
    for run in sys.argv[1:]:
        for k, ms in classes(run).items():
            total.setdefault(k, []).extend(ms)
    print(f"{'where':8s} {'s':>6s} {'frames':>7s} {'fps':>6s} {'p50':>6s} {'p95':>6s} {'p99':>6s} {'>33':>5s} {'>50':>5s}")
    for k in ("ground", "bridge", "tunnel"):
        ms = total.get(k, [])
        if not ms:
            print(f"{k:8s} (none)")
            continue
        s = sum(ms) / 1000
        print(f"{k:8s} {s:6.0f} {len(ms):7d} {len(ms) / s:6.1f} {pct(ms, .5):6.1f} {pct(ms, .95):6.1f} "
              f"{pct(ms, .99):6.1f} {sum(m > 33 for m in ms):5d} {sum(m > 50 for m in ms):5d}")


if __name__ == "__main__":
    main()
