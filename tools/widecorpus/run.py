"""Warm a data snapshot, build in isolated processes, then compare only completed pairs."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import fcntl
import hashlib
import os
import re
from pathlib import Path
import shutil
import signal
import site
import subprocess
import sys
import tempfile
import threading
import time
from collections import defaultdict

from common import CACHE, HERE, RAW, REVAMPED, ROOT, TREE, digest, places, read, save

# Leave room for in-flight downloads. Only directories published by this tool can be evicted.
EVICT_BYTES = 27_000_000_000
LIMIT_BYTES = 39_000_000_000
budget_lock = threading.Lock()
active = set()
pinned = set()
space_exhausted = threading.Event()


def size(path):
    proc = subprocess.run(["du", "-s", "-B1", path], capture_output=True, text=True,
                          env={**os.environ, "LC_ALL":"C"})
    if proc.returncode and (not proc.stdout or any("No such file" not in line
                                                   for line in proc.stderr.splitlines())):
        raise OSError(proc.stderr.strip())
    return int(proc.stdout.split()[0])


def trim():
    used = size(CACHE)
    if used < EVICT_BYTES:
        return used
    with budget_lock:
        old = sorted((p for p in (CACHE / "seeds").iterdir() if re.fullmatch(r"[0-9a-f]{20}",p.name)
                      and p.is_dir() and p.name not in active),
                     key=lambda p: (p.name in pinned,p.stat().st_mtime))
        for seed in old:
            if used <= EVICT_BYTES:
                break
            if seed.name not in pinned or used > LIMIT_BYTES-3_000_000_000:
                n = size(seed)
                shutil.rmtree(seed)
                used -= n
    return used


def fingerprint():
    # Include dirty source, native binaries, bundled datasets and the measurement code.
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=TREE).strip().decode()
    diff = subprocess.check_output(["git", "diff", "--binary", "HEAD", "--", "server", "native"], cwd=TREE)
    files = sorted([HERE / n for n in ("run.py", "common.py", "build.py", "checks.py", "scan.cpp")] +
                   [HERE.parent / "roadgeoprobe.py", HERE.parent / "probes/markprobe.py"])
    return digest([head, hashlib.sha256(diff).hexdigest(),
                   [hashlib.sha256(p.read_bytes()).hexdigest() for p in files]])


def build_process(p, cache, out, radius, env, offline, log):
    out.mkdir()
    runtime = out.parent
    temp = runtime / "tmp"
    temp.mkdir(exist_ok=True)
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("BNE_", "BNGE_"))}
    clean.update(env, HOME=str(runtime), XDG_CACHE_HOME=str(runtime / "xdg"), TMPDIR=str(temp),
                 PYTHONPATH=site.getusersitepackages(), PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1",
                 OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", RAYON_NUM_THREADS="1",
                 BNGE_NET_SHARED=str(ROOT / "netgate"), BNE_REVISIT="off", BNGE_PLAN_RUN="wide")
    import json
    cmd = [sys.executable, str(HERE / "build.py"), json.dumps(p), str(cache), str(out), str(radius)]
    if offline:
        cmd.append("--offline")
    with log.open("w") as f:
        with subprocess.Popen(cmd, env=clean, stdout=f, stderr=subprocess.STDOUT, cwd=runtime,
                              start_new_session=True) as worker:
            last_tile = prev = time.monotonic()
            written = 0
            try:
                while worker.poll() is None:
                    if space_exhausted.wait(2):
                        raise OSError("39 GB scratch cache limit")
                    now = time.monotonic()
                    if now - prev > 60:     # (this process was stopped, a gate's SIGSTOP: the time is not the build's)
                        last_tile += now - prev
                    prev = now
                    count = sum(1 for _ in out.glob("*.ter"))
                    if count > written:
                        written = count
                        last_tile = time.monotonic()
                    if time.monotonic()-last_tile > 2400:
                        raise TimeoutError("no finished terrain block for 40 minutes")
            finally:
                if worker.poll() is None:
                    os.killpg(worker.pid, signal.SIGKILL)
            if worker.returncode:
                raise RuntimeError(f"build exited {worker.returncode}, see {log}")


def snapshot(p, radius, log):
    name = digest([p["lat"], p["lon"], radius, p.get("country")])[:20]
    seed = CACHE / "seeds" / name
    mark = seed / "snapshot.json"
    if mark.exists():
        return seed
    seed.mkdir(parents=True, exist_ok=True)
    # Only these named source directories survive interrupted attempts.
    for item in seed.iterdir():
        if item.is_dir() and item.name not in RAW:
            shutil.rmtree(item)
    with tempfile.TemporaryDirectory(prefix="wide-", dir=CACHE / "work") as d:
        work = Path(d)
        # Record a completed cold stage only after its derived directories have been cleared.
        staged = seed / "warming.json"
        stages = read(staged) if staged.is_file() else {}
        # Cold builds may introduce data specific to an experimental path. Warm the union.
        for label, env in (("default", {}), ("revamped", REVAMPED)):
            key = digest([name, env, (HERE/"build.py").read_text()])
            if stages.get(label) == key:
                continue
            build_process(p, seed, work / label, radius, env, False, log.with_suffix(f".{label}.log"))
            for item in seed.iterdir():
                if item.name not in RAW and item.is_dir():
                    shutil.rmtree(item)
            stages[label] = key
            save(staged, stages)
        staged.unlink(missing_ok=True)
    hashes = hashlib.sha256()
    for item in sorted(seed.rglob("*")):
        if item.is_file():
            hashes.update(str(item.relative_to(seed)).encode() + b"\0")
            with item.open("rb") as f:
                file_hash = hashlib.sha256()
                for chunk in iter(lambda: f.read(1024*1024), b""):
                    file_hash.update(chunk)
                hashes.update(file_hash.digest())
    save(mark, {"sha256": hashes.hexdigest()})
    return seed


def place_run(p, radius, configs, run, code, warm_only=False):
    pid = p["id"]
    out = run / pid
    out.mkdir(parents=True, exist_ok=True)
    name = digest([p["lat"], p["lon"], radius, p.get("country")])[:20]
    identity = digest([p, radius, code])
    with budget_lock:
        active.add(name)
    try:
        def current(label, env):
            result = out / (label + ".json")
            if not result.is_file():
                return False
            try:
                row = read(result)
                return row.get("identity") == digest([identity, env]) and bool(row.get("tiles"))
            except ValueError:
                return False
        if not warm_only and all(current(k, v) for k, v in configs.items()):
            seeds = {read(out / (k + ".json"))["snapshot"] for k in configs}
            if len(seeds) == 1:
                return True
        if space_exhausted.is_set():
            raise OSError("cache budget exhausted")
        seed = snapshot(p, radius, out / "warm.log")
        mark = seed / "snapshot.json"
        token = read(mark)["sha256"]
        os.utime(mark, None)
        if not warm_only:
            for label, env in configs.items():
                result = out / (label + ".json")
                if current(label, env) and read(result)["snapshot"] == token:
                    continue
                with tempfile.TemporaryDirectory(prefix="wide-", dir=CACHE / "work") as d:
                    work = Path(d)
                    cache = work / "cache"
                    cache.mkdir()
                    for part in RAW:
                        if (seed / part).is_dir():
                            subprocess.run(["cp", "-a", "--reflink=auto", seed / part, cache], check=True)
                    build_process(p, cache, work / "tiles", radius, env, True, out / (label + ".log"))
                    from checks import measure
                    tiles = measure(p, work / "tiles", ROOT / "bin/scan", ROOT / "bin/roadsys")
                    save(result, dict(id=pid, identity=digest([identity, env]), snapshot=token,
                                      code=code, env=env, tiles=tiles))
        (out / "error.txt").unlink(missing_ok=True)
        print(pid, "ready" if warm_only else "done", flush=True)
        return True
    except Exception as exc:
        (out / "error.txt").write_text(str(exc))
        print(pid, type(exc).__name__, str(exc)[:180], flush=True)
        return False
    finally:
        with budget_lock:
            active.remove(name)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--places", help="comma-separated place IDs")
    ap.add_argument("--extra", type=Path, action="append", default=[],
                    help="a JSON array of more places kept outside Git (the bridge study's sites), as places.json")
    ap.add_argument("--public-only", action="store_true")
    ap.add_argument("--radius", type=int, default=30)
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--switch", choices=sorted(REVAMPED))
    ap.add_argument("--env", action="append", default=[], metavar="NAME=VALUE",
                    help="a configuration of these BNE_* settings instead of Revamped (repeatable; e.g. the mapped "
                         "driving features' switches)")
    ap.add_argument("--warm-only", action="store_true")
    ap.add_argument("--run", default="current")
    ap.add_argument("--bisect", action="store_true")
    args = ap.parse_args()
    if args.bisect and args.switch:
        ap.error("--bisect requires the full Revamped set")
    if Path(args.run).name != args.run or args.run in ("", ".", ".."):
        ap.error("invalid run name")
    cpus = sorted(os.sched_getaffinity(0))
    if not 1 <= args.jobs <= max(1, len(cpus)//2) or not 0 <= args.radius <= 500:
        ap.error("jobs must fit half the available CPUs and radius must be between 0 and 500 m")
    os.sched_setaffinity(0, cpus[:max(1, len(cpus)//2)])
    os.nice(max(0, 10 - os.nice(0)))
    os.umask(0o077)
    for path in (CACHE / "work", ROOT / "bin", ROOT / "netgate"):
        path.mkdir(parents=True, exist_ok=True)
    lock = (ROOT / "run.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    for old in (CACHE/"work").glob("wide-*"):
        if old.is_dir():
            shutil.rmtree(old)
    run = ROOT / "runs" / args.run
    ps = places(args.places, args.public_only, args.extra)
    coordinates=[(p["lat"],p["lon"]) for p in ps]
    if len(set(coordinates))!=len(ps):
        ap.error("duplicate coordinates in selection")
    code = fingerprint()
    configs = {"default": {}}
    if args.env:
        env = dict(e.split("=", 1) for e in args.env)
        if not all(k.startswith("BNE_") for k in env):
            ap.error("--env takes BNE_* settings")
        configs["custom"] = env
    elif args.switch:
        configs[args.switch] = {args.switch: REVAMPED[args.switch]}
    else:
        configs["revamped"] = REVAMPED
    if not args.warm_only:
        subprocess.run(["c++", "-O2", "-std=c++17", "-ffp-contract=off", HERE / "scan.cpp", "-o", ROOT / "bin/scan"], check=True)
        # The M0 audit (native/roadsys, audit.rs): this tree's crate, built apart from the shipped module
        target = ROOT / "bin/roadsys-target"
        subprocess.run(["cargo", "build", "--release", "--quiet", "-j", "2", "--bin", "roadsys"],
                       cwd=TREE / "native/roadsys", check=True, env={**os.environ, "CARGO_TARGET_DIR": str(target)})
        shutil.copy2(target / "release/roadsys", ROOT / "bin/roadsys")
    stop = threading.Event()
    (CACHE / "seeds").mkdir(exist_ok=True)
    if trim() >= LIMIT_BYTES:
        ap.error("39 GB scratch cache limit reached")
    def monitor():
        while not stop.wait(10):
            try:
                full = trim() >= LIMIT_BYTES
            except OSError:
                full = True
            if full:
                space_exhausted.set()
            else:
                space_exhausted.clear()
    threading.Thread(target=monitor, daemon=True).start()
    # Interleave geographic strata so an interrupted run still exercises different road systems.
    strata = defaultdict(list)
    for place in ps:
        strata[place.get("region","reported")].append(place)
    ps = [group[i] for i in range(max(map(len,strata.values()))) for group in strata.values() if i<len(group)]
    run.mkdir(parents=True, exist_ok=True)
    if not args.warm_only:
        save(run / "manifest.json", dict(code=code, radius=args.radius, configs=configs, places=[p["id"] for p in ps],
                                         **({"extra": [str(x.resolve()) for x in args.extra]} if args.extra else {})))
    with ThreadPoolExecutor(args.jobs) as pool:
        pending = {pool.submit(place_run,p,args.radius,configs,run,code,args.warm_only):p for p in ps}
        ok = []
        for completed in as_completed(pending):
            ok.append(completed.result())
            if not args.warm_only:
                from compare import ranking
                partial = ranking(run,ps,configs,code,args.radius)
                save(run/"progress.json",partial)
                leaders = {r["id"] for r in partial["places"][:10] if r["regression"]>0}
                with budget_lock:
                    pinned.clear()
                    pinned.update(digest([p["lat"],p["lon"],args.radius,p.get("country")])[:20]
                                  for p in ps if p["id"] in leaders)
    if not args.warm_only:
        from compare import compare
        result = compare(run, ps, configs, code, args.radius, args.bisect, args.jobs, builder=place_run)
        ok.append(not result["missing"])
        if any(t["status"]=="incomplete" for t in result.get("attribution",{}).values()):
            ok.append(False)
    stop.set()
    return 0 if all(ok) else 2


if __name__ == "__main__":
    sys.exit(main())
