#!/usr/bin/env python3
"""The mini PC's on-demand builder of the 1 m aerial packs (not deployed: the lead's; native/serverlist aerial.rs is the
front door that spools requests, this takes them):

    aerial_ondemand.py SPOOL WORK [--gen 1] [--workers 8] [--speed 9] [--once]

Loop: the oldest SPOOL/queue/<x>_<y> -> SPOOL/building/, build the cell with tools/build_aerial_packs.py into
WORK/<x>_<y>/ (--fragments), upload its packs (immutable) and then its fragment to R2, pull the region's fragments,
rebuild and upload the region's coverage file (aerial/v1/cov/<rx>/<ry>.json, max-age 300), then SPOOL/done/<x>_<y>
(or SPOOL/none/<x>_<y>: no open photos there, the fragment says g 0 so no player asks again), WORK/<x>_<y> removed.
A failed build goes back to the queue's end at most RETRIES times, then to SPOOL/failed/.

R2 credentials: the environment only (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / R2_ENDPOINT from a systemd
EnvironmentFile readable by this service's user alone); never printed, never passed on the command line. Needs the
aws CLI, and the builder's packages (numpy, Pillow >= 11.3, rasterio) in the Python that runs it. Disk: a cell's
work folder is ~60 MB; nothing else stays.
"""
import argparse
import os
import shutil
import subprocess
import sys
import time

BUCKET, PREFIX = "bnge-tiles", "aerial/v1"
RETRIES = 2
HERE = os.path.dirname(os.path.abspath(__file__))


def aws(*args):
    cmd = ["aws", "s3", *args, "--endpoint-url", os.environ["R2_ENDPOINT"], "--only-show-errors"]
    env = dict(os.environ, AWS_DEFAULT_REGION="auto", AWS_REQUEST_CHECKSUM_CALCULATION="when_required",
               AWS_RESPONSE_CHECKSUM_VALIDATION="when_required")
    return subprocess.run(cmd, env=env, capture_output=True, text=True).returncode == 0


def oldest(d):
    names = [n for n in os.listdir(d) if not n.startswith(".")]
    return min(names, key=lambda n: os.path.getmtime(os.path.join(d, n))) if names else None


def build_one(a, name):
    cx, cy = (int(v) for v in name.split("_"))
    work = os.path.join(a.work, name)
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    r = subprocess.run([sys.executable, os.path.join(HERE, "build_aerial_packs.py"), work, "--cells", f"{cx},{cy}",
                        "--gen", str(a.gen), "--workers", str(a.workers), "--speed", str(a.speed), "--fragments"],
                       capture_output=True, text=True)
    print(r.stdout.strip()[-300:], flush=True)
    if r.returncode != 0:
        print(r.stderr.strip()[-600:], file=sys.stderr, flush=True)
        return None
    g = os.path.join(work, f"g{a.gen}")
    if os.path.isdir(g) and not aws("cp", g, f"s3://{BUCKET}/{PREFIX}/g{a.gen}", "--recursive",
                                    "--content-type", "application/octet-stream",
                                    "--cache-control", "public, max-age=31536000, immutable"):
        return None
    if not aws("cp", os.path.join(work, "_cells"), f"s3://{BUCKET}/{PREFIX}/_cells", "--recursive",
               "--content-type", "application/json", "--cache-control", "no-cache"):
        return None
    # the region's coverage from all of its fragments (the workflow's and earlier builds')
    rx, ry = cx >> 5, cy >> 5
    frags = os.path.join(work, "frags", "_cells")
    os.makedirs(frags)
    if not aws("cp", f"s3://{BUCKET}/{PREFIX}/_cells/", frags, "--recursive", "--exclude", "*",
               *[x for i in range(32) for x in ("--include", f"{(rx << 5) + i}_*")]):
        return None
    for n in os.listdir(frags):                 # (the region's own cells only)
        x, y = (int(v) for v in n[:-5].split("_"))
        if (x >> 5, y >> 5) != (rx, ry):
            os.remove(os.path.join(frags, n))
    subprocess.run([sys.executable, os.path.join(HERE, "build_aerial_packs.py"), work, "--merge",
                    os.path.join(work, "frags")], check=True, capture_output=True)
    if not aws("cp", os.path.join(work, "cov", str(rx), f"{ry}.json"), f"s3://{BUCKET}/{PREFIX}/cov/{rx}/{ry}.json",
               "--content-type", "application/json", "--cache-control", "public, max-age=300"):
        return None
    has = os.path.isdir(g)
    shutil.rmtree(work, ignore_errors=True)
    return "done" if has else "none"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("spool")
    ap.add_argument("work")
    ap.add_argument("--gen", type=int, default=1)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--speed", type=int, default=9)     # (aom 9: ~4x faster than 8 for +5 % bytes: a player waits)
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    for sub in ("queue", "building", "done", "none", "failed"):
        os.makedirs(os.path.join(a.spool, sub), exist_ok=True)
    tries = {}
    while True:
        name = oldest(os.path.join(a.spool, "queue"))
        if name is None:
            if a.once:
                return
            time.sleep(5)
            continue
        q, b = os.path.join(a.spool, "queue", name), os.path.join(a.spool, "building", name)
        os.replace(q, b)
        t0 = time.time()
        st = build_one(a, name)
        if st is None:
            tries[name] = tries.get(name, 0) + 1
            os.replace(b, q if tries[name] <= RETRIES else os.path.join(a.spool, "failed", name))
            os.utime(q if tries[name] <= RETRIES else os.path.join(a.spool, "failed", name))
        else:
            os.replace(b, os.path.join(a.spool, st, name))
        print(f"cell {name}: {st or 'failed'} in {time.time() - t0:.0f} s", flush=True)


if __name__ == "__main__":
    main()
