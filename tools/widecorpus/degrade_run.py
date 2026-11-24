"""Run the degradation test (degrade.py, plan J1): each place built with its full data (D0) and with each step's data
removed (D1-D6) from one frozen input snapshot, every build offline in a fresh process; then each step measured
against D0 (degrade_metrics.py) and the corpus checks (checks.py) counted per step.

    chrt -i 0 python3 tools/widecorpus/degrade_run.py --run baseline --jobs 2
    chrt -i 0 python3 tools/widecorpus/degrade_run.py --run baseline --report

Places: the auto-tuner's 40 training places (its frozen split, ~/bnge-tests/codextune) unless --places. Scratch:
~/bnge-tests/j1degrade (WIDE_CORPUS_ROOT): seeds (inputs, copied from the corpus's own snapshot where it has one, then
warmed online with D0 and D3 so the coarse DEM is in it too), runs/RUN/PLACE/STEP (the tile outputs, kept),
STEP.json (corpus checks), rows_STEP.npz / bld_STEP.json (the step against D0). Repeat the command to resume."""
import os
import sys
from pathlib import Path

J1 = Path.home() / "bnge-tests/j1degrade"
os.environ.setdefault("WIDE_CORPUS_ROOT", str(J1))

import argparse  # noqa: E402
from concurrent.futures import ThreadPoolExecutor, as_completed  # noqa: E402
import fcntl  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402

from common import CACHE, HERE, RAW, ROOT, TREE, digest, places, read, save  # noqa: E402
import degrade  # noqa: E402

WIDE_SEEDS = Path.home() / "bnge-tests/widecorpus/cache/seeds"
TUNER = Path.home() / "bnge-tests/codextune/experiments/returns-world-grid/manifest.json"
# (the steps that read other cells or other DEM tiles than D0: each warmed online, so every offline build finds its
# inputs; a step that still faults more than D0 is left out of the tables, degrade_report)
WARM = ("D0", "D1", "D2", "D3")


def fingerprint():
    # (the server's and native code's committed trees, not HEAD: a commit of tools alone keeps the results)
    head = subprocess.check_output(["git", "rev-parse", "HEAD:server", "HEAD:native"], cwd=TREE).strip().decode()
    diff = subprocess.check_output(["git", "diff", "--binary", "HEAD", "--", "server", "native"], cwd=TREE)
    files = sorted([HERE / n for n in ("run.py", "common.py", "build.py", "checks.py", "scan.cpp", "degrade.py")] +
                   [HERE.parent / "roadgeoprobe.py", HERE.parent / "probes/markprobe.py"])
    return digest([head, hashlib.sha256(diff).hexdigest(), [hashlib.sha256(p.read_bytes()).hexdigest() for p in files]])


def seed_name(p, radius):
    return digest([p["lat"], p["lon"], radius, p.get("country")])[:20]      # (run.py's: the corpus's seeds reused)


def drop_decoded_roads(cache):
    """The decoded roads layers of a cache copy: made again from the raw inputs, through the filter (degrade.py)."""
    for d in (cache / "mapdata").glob("v*/*/*/roads*"):
        shutil.rmtree(d)
    shutil.rmtree(cache / "mapdata-mirror", ignore_errors=True)


def tree_hash(root):
    h = hashlib.sha256()
    for item in sorted(root.rglob("*")):
        if item.is_file() and item.name != "snapshot.json":
            h.update(str(item.relative_to(root)).encode() + b"\0")
            with item.open("rb") as f:
                fh = hashlib.sha256()
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    fh.update(chunk)
            h.update(fh.digest())
    return h.hexdigest()


def warm(p, radius, logs):
    """The place's input snapshot (a seed folder of RAW sources), warmed online with WARM's steps."""
    from run import build_process
    seed = CACHE / "seeds" / seed_name(p, radius)
    mark = seed / "snapshot.json"
    if mark.exists() and set(read(mark).get("warm_s", {})) >= set(WARM):
        return seed
    seed.mkdir(parents=True, exist_ok=True)
    staged = seed / "warming.json"
    if mark.exists():           # (warmed with fewer steps: the new ones added)
        old = read(mark)
        stages = {}
        for step, secs in old.get("warm_s", {}).items():
            stages[step] = digest([seed.name, degrade.env(step), (HERE / "build.py").read_text()])
            stages[step + "_s"] = secs
        save(staged, stages)
        mark.unlink()
    wide = WIDE_SEEDS / seed.name
    if (wide / "snapshot.json").exists() and not any((seed / r).exists() for r in RAW):
        for r in RAW:
            if (wide / r).is_dir():
                subprocess.run(["cp", "-a", "--reflink=auto", wide / r, seed], check=True)
    stages = read(staged) if staged.is_file() else {}
    for step in WARM:
        key = digest([seed.name, degrade.env(step), (HERE / "build.py").read_text()])
        if stages.get(step) == key:
            continue
        drop_decoded_roads(seed)
        for item in seed.iterdir():
            if item.is_dir() and item.name not in RAW:
                shutil.rmtree(item)
        with tempfile.TemporaryDirectory(prefix="warm-", dir=CACHE / "work") as d:
            t0 = time.monotonic()
            build_process(p, seed, Path(d) / "tiles", radius, degrade.env(step), False, logs / f"warm-{step}.log")
            stages[step] = key
            stages[step + "_s"] = round(time.monotonic() - t0, 1)
            save(staged, stages)
    for item in seed.iterdir():
        if item.is_dir() and item.name not in RAW:
            shutil.rmtree(item)
    save(mark, {"sha256": tree_hash(seed), "warm_s": {k: stages.get(k + "_s") for k in WARM}})
    staged.unlink(missing_ok=True)
    return seed


def build_step(p, seed, step, radius, out, code, token):
    """One offline build of step from the seed (a private copy, decoded roads dropped), its tiles kept in out/step,
    and the corpus checks of them in out/step.json."""
    from run import build_process
    from checks import measure
    result = out / (step + ".json")
    identity = digest([p, radius, code, degrade.env(step)])
    if result.is_file():
        try:
            row = read(result)
            if row.get("identity") == identity and row.get("snapshot") == token and row.get("tiles"):
                return row
        except ValueError:
            pass
    tiles = out / step
    shutil.rmtree(tiles, ignore_errors=True)
    with tempfile.TemporaryDirectory(prefix="deg-", dir=CACHE / "work") as d:
        work = Path(d)
        cache = work / "cache"
        cache.mkdir()
        for part in RAW:
            if (seed / part).is_dir():
                subprocess.run(["cp", "-a", "--reflink=auto", seed / part, cache], check=True)
        drop_decoded_roads(cache)
        t0 = time.monotonic()
        build_process(p, cache, work / "tiles", radius, degrade.env(step), True, out / (step + ".log"))
        wall = time.monotonic() - t0
        shutil.move(str(work / "tiles"), str(tiles))
    t1 = time.monotonic()
    measured = measure(p, tiles, ROOT / "bin/scan", ROOT / "bin/roadsys")
    for junk in ("scan.bin", "lanes.bin", "base.bin", "audit.bin"):
        (tiles / junk).unlink(missing_ok=True)
    row = dict(id=p["id"], identity=identity, snapshot=token, code=code, env=degrade.env(step), tiles=measured,
               build_wall_s=round(wall, 1), measure_s=round(time.monotonic() - t1, 1))
    save(result, row)
    return row


def compare_step(p, out, step, areas):
    """degrade_metrics of step against D0: rows_STEP.npz, bld_STEP.json, cmp_STEP.json (the corpus checks' pair)."""
    import degrade_metrics as dm
    from compare import pair
    full = dm.Build(out / "D0", p)
    deg = dm.Build(out / step, p)
    rows = dm.place_rows(full, deg, areas)
    dm.save_rows(out / f"rows_{step}.npz", rows)
    mapped = set((full.buildings or {}).get("mapped", []))
    b = dm.building_rows(full.buildings, deg.buildings, mapped)
    save(out / f"bld_{step}.json", {k: v.tolist() for k, v in b.items()})
    a, c = read(out / "D0.json"), read(out / (step + ".json"))
    a = dict(a, code="same")
    c = dict(c, code="same")
    save(out / f"cmp_{step}.json", pair(p, a, c))


def place_job(p, radius, steps, run, code, areas_fn):
    out = run / p["id"]
    out.mkdir(parents=True, exist_ok=True)
    try:
        seed = warm(p, radius, out)
        token = read(seed / "snapshot.json")["sha256"]
        rows = {}
        for step in steps:
            rows[step] = build_step(p, seed, step, radius, out, code, token)
        areas = areas_fn(p, out) if areas_fn else None
        for step in steps:
            if step == "D0":
                if areas:
                    import degrade_metrics as dm
                    dm.save_rows(out / "rows_D0.npz", dm.place_rows(dm.Build(out / "D0", p), None, areas))
                continue
            compare_step(p, out, step, areas)
        (out / "error.txt").unlink(missing_ok=True)
        print(p["id"], "done", flush=True)
        return True
    except Exception as e:
        (out / "error.txt").write_text(traceback.format_exc())
        print(p["id"], type(e).__name__, str(e)[:200], flush=True)
        return False


def repeat(p, radius, step, run, code):
    """A/A: step built again in a fresh process from the same snapshot (out/repeat/STEP); the files that differ from
    the first build's (the logs aside). An empty list: the step is deterministic for this place."""
    out = run / p["id"]
    seed = CACHE / "seeds" / seed_name(p, radius)
    token = read(seed / "snapshot.json")["sha256"]
    again = out / "repeat"
    again.mkdir(exist_ok=True)
    build_step(p, seed, step, radius, again, code, token)
    a, b = out / step, again / step
    differ = []
    for f in sorted(a.rglob("*")):
        if f.is_file() and not f.name.endswith(".log"):
            g = b / f.relative_to(a)
            if not g.is_file() or f.read_bytes() != g.read_bytes():
                differ.append(str(f.relative_to(a)))
    extra = [str(g.relative_to(b)) for g in b.rglob("*") if g.is_file() and not (a / g.relative_to(b)).exists()]
    return differ + extra


def ensure_bins():
    (ROOT / "bin").mkdir(parents=True, exist_ok=True)
    subprocess.run(["c++", "-O2", "-std=c++17", "-ffp-contract=off", HERE / "scan.cpp", "-o", ROOT / "bin/scan"],
                   check=True)
    target = ROOT / "bin/roadsys-target"
    subprocess.run(["cargo", "build", "--release", "--quiet", "-j", "2", "--bin", "roadsys"],
                   cwd=TREE / "native/roadsys", check=True, env={**os.environ, "CARGO_TARGET_DIR": str(target)})
    shutil.copy2(target / "release/roadsys", ROOT / "bin/roadsys")


def training_split():
    return read(TUNER)["train"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", default="baseline")
    ap.add_argument("--places", help="comma-separated ids (default: the tuner's 40 training places)")
    ap.add_argument("--steps", default=",".join(degrade.STEPS))
    ap.add_argument("--radius", type=int, default=30)
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--report", action="store_true", help="only write the tables of what has run")
    ap.add_argument("--no-bins", action="store_true", help="use the binaries already in bin/")
    ap.add_argument("--repeat", help="A/A: build these steps again for --places and list the files that differ")
    ap.add_argument("--resume", action="store_true",
                    help="finish the run with the code fingerprint it started with (after changes that build nothing "
                         "differently: the runner, the tables)")
    args = ap.parse_args()
    if Path(args.run).name != args.run or args.run in ("", ".", ".."):
        ap.error("invalid run name")
    steps = args.steps.split(",")
    if steps[0] != "D0" or any(s not in degrade.STEPS for s in steps):
        ap.error("steps start with D0 and are among " + ",".join(degrade.STEPS))
    ids = args.places.split(",") if args.places else training_split()
    ps = places(",".join(ids))
    run = ROOT / "runs" / args.run
    if args.repeat:
        code = read(run / "manifest.json")["code"]
        result = {p["id"]: {s: repeat(p, args.radius, s, run, code) for s in args.repeat.split(",")} for p in ps}
        save(run / "repeat.json", result)
        print(json.dumps(result, indent=1))
        return 0 if not any(v for r in result.values() for v in r.values()) else 1
    if args.report:
        import degrade_report
        degrade_report.write(run, ps, steps)
        return 0
    os.umask(0o077)
    for path in (CACHE / "work", CACHE / "seeds", ROOT / "netgate", run):
        path.mkdir(parents=True, exist_ok=True)
    lock = (ROOT / f"run-{args.run}.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    for old in (CACHE / "work").glob("*-*"):
        if old.is_dir():
            shutil.rmtree(old)
    if not args.no_bins:
        ensure_bins()
    code = read(run / "manifest.json")["code"] if args.resume and (run / "manifest.json").is_file() else fingerprint()
    save(run / "manifest.json", dict(code=code, radius=args.radius, steps=steps, places=ids,
                                     envs={s: degrade.env(s) for s in steps}))
    import degrade_areas
    ok = []
    with ThreadPoolExecutor(args.jobs) as pool:
        pending = [pool.submit(place_job, p, args.radius, steps, run, code, degrade_areas.areas_for) for p in ps]
        for f in as_completed(pending):
            ok.append(f.result())
    import degrade_report
    degrade_report.write(run, ps, steps)
    return 0 if all(ok) else 2


if __name__ == "__main__":
    sys.exit(main())
