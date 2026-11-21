"""Replay a production exception on frozen data and isolate switches that reproduce it."""
import argparse
import fcntl
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading

from common import CACHE, RAW, REVAMPED, ROOT, TREE, digest, places, read, save
from compare import confirmed_failure, minimize_switches, run_name
import run


def exception(log):
    """Only a traceback ending in the production server identifies a product build failure."""
    source = log.read_text(errors="replace")
    frames = re.findall(r'^  File "([^"]+)", line (\d+), in (.+)$', source, re.M)
    lines=source.rstrip().splitlines()
    error=re.fullmatch(r'([\w.]+): (.+)',lines[-1]) if lines else None
    if not frames or not error or not error[1].endswith(("Error","Exception")):
        return None
    path, line, function = frames[-1]
    if not Path(path).is_relative_to(TREE / "server"):
        return None
    kind, message = error.groups()
    return dict(type=kind, file=str(Path(path).relative_to(TREE)), line=int(line),
                function=function, message=message)


def signature(error):
    return tuple(error[k] for k in ("type","file","function")) if error else None


def copy_inputs(source, target):
    target.mkdir(parents=True,exist_ok=True)
    for name in RAW:
        if (source/name).is_dir():
            subprocess.run(["cp","-a","--reflink=auto",source/name,target],check=True)


def snapshot_token(root):
    checksum = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            checksum.update(path.relative_to(root).as_posix().encode()+b"\0")
            with path.open("rb") as stream:
                checksum.update(hashlib.file_digest(stream,"sha256").digest())
    return checksum.hexdigest()


def diagnose(directory, place, manifest):
    pid=place["id"]
    radius, code=manifest["radius"], manifest["code"]
    identity=digest([place,radius,code])
    verified=confirmed_failure(directory,place,manifest["configs"],code,radius)
    if verified:
        return verified
    out=directory/"failures"/pid
    out.mkdir(parents=True,exist_ok=True)
    first=None
    for name in ("warm.revamped.log","revamped.log"):
        log=directory/pid/name
        if log.exists() and exception(log):
            first=exception(log)
            shutil.copy2(log,out/("first-"+name))
            break
    if first is None:
        raise ValueError("no terminal production traceback in the Revamped build logs")

    key=digest([place["lat"],place["lon"],radius,place.get("country")])[:20]
    source=CACHE/"work"/("failure-input-"+key)
    if not source.is_dir():
        with tempfile.TemporaryDirectory(dir=CACHE/"work",prefix="failure-acquire-") as temp:
            work=Path(temp)
            seed=CACHE/"seeds"/key
            if not seed.exists():
                # A pruned source can be reacquired through the same per-host gates as the corpus.
                seed=work/"raw"
                seed.mkdir()
                run.build_process(place,seed,work/"default",radius,{},False,out/"cold-default.log")
                try:
                    run.build_process(place,seed,work/"revamped",radius,REVAMPED,False,out/"cold-revamped.log")
                except RuntimeError:
                    if not exception(out/"cold-revamped.log"):
                        raise
            staged=work/"frozen"
            copy_inputs(seed,staged)
            staged.rename(source)
    token=snapshot_token(source)
    memo={}
    def trial(keys):
        env={k:REVAMPED[k] for k in sorted(keys)}
        label=digest(env)[:16]
        if label in memo:
            return memo[label]
        record=out/"trials"/(label+".json")
        if record.exists():
            cached=read(record)
            if (cached.get("identity"),cached.get("snapshot"),cached.get("env"))==(identity,token,env):
                memo[label]=cached
                return cached
        log=out/(label+".log")
        with tempfile.TemporaryDirectory(dir=CACHE/"work",prefix="failure-trial-") as temp:
            work=Path(temp)
            copy_inputs(source,work/"raw")
            products=work/"tiles"
            error=None
            try:
                run.build_process(place,work/"raw",products,radius,env,True,log)
            except RuntimeError:
                error=exception(log)
                if error is None:
                    raise
            result=dict(identity=identity,code=code,snapshot=token,env=env,error=error,
                        finished=sorted(p.name.removesuffix(".source.json")
                                        for p in products.glob("*.source.json")))
            if not error and not env:
                from checks import measure
                result["tiles"]=measure(place,products,ROOT/"bin/scan")
            save(record,result)
            memo[label]=result
            print(pid, sorted(env), error["type"] if error else "built",flush=True)
            return result

    baseline=trial([])
    full=trial(REVAMPED)
    result=dict(id=pid,identity=identity,code=code,snapshot=token,default_finished=baseline["finished"],
                candidate_finished=full["finished"],default_error=baseline["error"],
                candidate_error=full["error"],initial_error=first)
    if baseline["error"] or signature(full["error"])!=signature(first):
        result["status"]="inconclusive"
    else:
        target=signature(first)
        def reproduces(keys):
            tested=trial(keys)
            return signature(tested["error"])==target and tested["finished"]==full["finished"]
        sufficient=minimize_switches(REVAMPED,reproduces)
        complement=trial(set(REVAMPED)-set(sufficient))
        missing=set(baseline["finished"])-set(full["finished"])
        order=lambda t:tuple(map(int,t.split("_")[1:]))
        result.update(status="confirmed",sufficient_set=sufficient,without_set_error=complement["error"],
                      failed_tile=min(missing,key=order) if missing else None)
    result["trials"]={k:v["env"] for k,v in memo.items()}
    save(out/"attribution.json",result)
    if result["status"]=="confirmed":
        shutil.rmtree(source)
    return result


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run",type=run_name,default="current")
    ap.add_argument("--places",required=True)
    args=ap.parse_args()
    cpus=sorted(os.sched_getaffinity(0))
    os.sched_setaffinity(0,cpus[:max(1,len(cpus)//2)][-1:])
    os.nice(max(0,10-os.nice(0)))
    os.umask(0o077)
    directory=ROOT/"runs"/args.run
    manifest=read(directory/"manifest.json")
    selected=places(args.places)
    if manifest["code"]!=run.fingerprint() or manifest["configs"].get("revamped")!=REVAMPED:
        ap.error("the recorded run must match this checkout and the full Revamped set")
    if any(p["id"] not in manifest["places"] for p in selected):
        ap.error("place absent from manifest")
    stop=threading.Event()
    def guard():
        while not stop.is_set():
            try:
                full=run.size(CACHE)>=run.LIMIT_BYTES-400_000_000
            except OSError:
                full=True
            if full:
                run.space_exhausted.set()
            else:
                run.space_exhausted.clear()
            stop.wait(10)
    with (ROOT/"failure-diagnostics.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if run.size(CACHE)>=run.LIMIT_BYTES-1_000_000_000:
            ap.error("insufficient scratch cache headroom")
        threading.Thread(target=guard,daemon=True).start()
        try:
            results=[diagnose(directory,p,manifest) for p in selected]
        finally:
            stop.set()
    return 0 if all(r["status"]=="confirmed" for r in results) else 2


if __name__=="__main__":
    raise SystemExit(main())
