#!/usr/bin/env python3
"""The launch's up-to-date checks, timed (docs/DEVTOOLS.md's numbers for statcache.py):

    python tools/bench_launch.py                 the launch's checks on this machine
    python tools/bench_launch.py --cold          the cache cleared first (a first launch's cost)

The checks: the installed mod against this folder's (install.py's tree_hash, both trees), the
texture sources' digests (modtextures.plan), the installed textures' own digests
(modtextures.current), and the server's --help text (start.py's server_flags). The world server is
not started. Uses .venv's Python when it is there, else this one.
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import apppaths  # noqa: E402
import install  # noqa: E402
import modtextures  # noqa: E402
import statcache  # noqa: E402

CACHE = os.path.join(apppaths.data_dir(), statcache.FILE)


def apppaths_data_dir():
    import apppaths
    return apppaths.data_dir()
def step(name):
    print(f"  {name:<28} ", end="", flush=True)


def timed(fn, *a, **k):
    t0 = time.time()
    out = fn(*a, **k)
    return time.time() - t0, out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--cold", action="store_true", help="clear the digest caches first")
    args = ap.parse_args()

    if args.cold:
        for path in (CACHE, os.path.join(apppaths.data_dir(), "flagcache.json")):
            if os.path.isfile(path):
                os.remove(path)
                print(f"  cleared {os.path.basename(path)}")
    cfg = install.load_config()
    user_dir = cfg.get("user_dir")
    if not user_dir:
        found = install.candidate_user_dirs()
        user_dir = found[0] if found else None
    if not user_dir:
        sys.exit("no BeamNG.drive user folder known: run install.py first")

    dest = os.path.join(user_dir, "mods", "unpacked", install.MOD_NAME)
    if not os.path.isdir(dest):
        sys.exit(f"the mod is not installed in {dest}: run install.py first")

    print(f"  installed mod: {dest}")
    print(f"  cache: {'cold' if args.cold else 'warm'}")
    total = 0.0

    step("modtextures.plan (sources)")
    secs, todo = timed(modtextures.plan, env=dict(os.environ))
    print(f"{secs:6.2f} s   ({len(todo)} textures)")
    total += secs

    made = modtextures.generated(todo)
    step("tree_hash installed mod")
    secs, _ = timed(install.tree_hash, dest, made)
    print(f"{secs:6.2f} s")
    total += secs

    step("tree_hash this folder's mod")
    secs, _ = timed(install.tree_hash, install.MOD_SRC, made)
    print(f"{secs:6.2f} s")
    total += secs

    step("modtextures.current (DDS)")
    secs, cur = timed(modtextures.current, dest, todo)
    print(f"{secs:6.2f} s   ({'up to date' if cur else 'to encode'})")
    total += secs

    step("server_flags (earthd --help)")
    from start import server_flags
    py = install.venv_python() if os.path.isfile(install.venv_python()) else sys.executable
    secs, text = timed(server_flags, py)
    print(f"{secs:6.2f} s   ({len(text.splitlines())} help lines)")
    total += secs

    print(f"  {'TOTAL':<28} {total:6.2f} s")


if __name__ == "__main__":
    main()
