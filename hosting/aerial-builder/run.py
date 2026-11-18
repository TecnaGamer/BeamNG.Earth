#!/usr/bin/env python3
"""The on-demand aerial builder service (systemd user unit bnge-aerial-builder, hosting/systemd/; docs/HOSTING.md):
a thin wrapper round a pinned copy of tools/aerial_ondemand.py (the commit in ./COMMIT), adding what the service needs
and the repo's loop lacks:

  * the R2 credentials from the unit's EnvironmentFile (%h/bnge-builder/r2.env: R2_ACCOUNT_ID, R2_ACCESS_KEY_ID,
    R2_SECRET_ACCESS_KEY, R2_BUCKET) mapped to the names the builder / aws CLI read (AWS_ACCESS_KEY_ID,
    AWS_SECRET_ACCESS_KEY, R2_ENDPOINT); never printed, never on a command line;
  * a disk guard: no cell is started while the disk holding WORK has less than MIN_FREE_GB free (checked before
    every cell; a cell's work folder is ~60 MB);
  * cells left in SPOOL/building/ by a stop or crash go back to the queue at start.

    run.py SPOOL WORK [--workers N] [--speed S] [--min-free-gb G]    # the service
    run.py SPOOL WORK --dry-run                                     # what it would do now (no network, no R2)

Layout next to this file (copied from one BeamNG.Earth commit; hosting/README.md):
    COMMIT                                  the commit the files below came from
    tools/aerial_ondemand.py, tools/build_aerial_packs.py
    server/tilepack.py, server/dnp.py, server/fast.py
    venv/                                   numpy, Pillow >= 11.3 (AVIF), rasterio, awscli
"""
import argparse
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "tools"))
import aerial_ondemand  # noqa: E402


def free_gb(path):
    return shutil.disk_usage(path).free / 1e9


def creds():
    """True when every R2 variable is set; maps them to the builder's names (values never printed)."""
    need = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
    if not all(os.environ.get(k) for k in need):
        return False
    os.environ["AWS_ACCESS_KEY_ID"] = os.environ["R2_ACCESS_KEY_ID"]
    os.environ["AWS_SECRET_ACCESS_KEY"] = os.environ["R2_SECRET_ACCESS_KEY"]
    os.environ["R2_ENDPOINT"] = f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com"
    if os.environ.get("R2_BUCKET"):
        aerial_ondemand.BUCKET = os.environ["R2_BUCKET"]
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("spool")
    ap.add_argument("work")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--speed", type=int, default=9)
    ap.add_argument("--min-free-gb", type=float, default=20.0)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    for sub in ("queue", "building", "done", "none", "failed"):
        os.makedirs(os.path.join(a.spool, sub), exist_ok=True)
    os.makedirs(a.work, exist_ok=True)
    have = creds()
    q = os.path.join(a.spool, "queue")
    if a.dry_run:
        names = sorted((n for n in os.listdir(q) if not n.startswith(".")),
                       key=lambda n: os.path.getmtime(os.path.join(q, n)))
        print(f"commit {open(os.path.join(HERE, 'COMMIT')).read().strip()[:12]}; R2 credentials "
              f"{'present' if have else 'MISSING (the service waits for r2.env)'}; free {free_gb(a.work):.1f} GB "
              f"(guard {a.min_free_gb:.0f} GB); queue {len(names)}: {' '.join(names[:20])}")
        for n in names[:20]:
            print(f"  would build cell {n} -> s3://{aerial_ondemand.BUCKET}/{aerial_ondemand.PREFIX}/g1/...")
        return
    if not have:
        print("R2 credentials missing (r2.env lacks R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY): idle",
              flush=True)
        while True:
            time.sleep(3600)
    b = os.path.join(a.spool, "building")
    for n in os.listdir(b):                     # (a stop or crash mid-build)
        if not n.startswith("."):
            os.replace(os.path.join(b, n), os.path.join(q, n))
            print(f"cell {n}: back to the queue (left building by the last run)", flush=True)
    orig = aerial_ondemand.oldest
    low = [False]

    def guarded(d):
        if d == q:
            f = free_gb(a.work)
            if f < a.min_free_gb:
                if not low[0]:
                    print(f"disk guard: {f:.1f} GB free < {a.min_free_gb:.0f} GB: not building", flush=True)
                low[0] = True
                time.sleep(55)              # (+5 s in the loop: checked about once a minute)
                return None
            if low[0]:
                print(f"disk guard: {f:.1f} GB free again: building", flush=True)
                low[0] = False
        return orig(d)

    aerial_ondemand.oldest = guarded
    print(f"aerial builder up (commit {open(os.path.join(HERE, 'COMMIT')).read().strip()[:12]}, "
          f"{a.workers} workers, speed {a.speed}, guard {a.min_free_gb:.0f} GB)", flush=True)
    sys.argv = ["aerial_ondemand.py", a.spool, a.work, "--workers", str(a.workers), "--speed", str(a.speed)]
    aerial_ondemand.main()


if __name__ == "__main__":
    main()
