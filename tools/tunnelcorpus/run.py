"""The tunnel corpus (tunnel plan T0): every real mouth of refs.json built through the production road geotile
pipeline (tools/widecorpus build.py) and measured by the M0 audit with its tunnel checks (native/roadsys tunaudit.rs),
the built portals written beside each result for report.py's portal offsets.

    python3 tools/tunnelcorpus/run.py warm  [--ids a,b]               # online: fill the shared seed for each mouth
    python3 tools/tunnelcorpus/run.py build LABEL [--revamped] [--env K=V ...] [--ids a,b] [--tree DIR]

Scratch under ROOT (TUNNEL_CORPUS_ROOT, default ~/bnge-tests/tunnelcorpus): cache/seed/ (the raw inputs of every mouth, one
folder: reflinked from SEED_FROM when it is first made), run/<id>/<LABEL>.json + .portals.txt, tiles/<id>/<LABEL>/
when KEEP_TILES is set. Each measured build is offline, in a fresh process, from a reflink copy of the seed, so only
the code and the switches differ between labels. --tree builds with another checkout's server and tools (the
baseline). AUDITOR (default: the roadsys binary of this checkout's native/roadsys built into ROOT/bin) measures."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
TREE = HERE.parents[1]
ROOT = Path(os.environ.get("TUNNEL_CORPUS_ROOT") or Path.home() / "bnge-tests/tunnelcorpus")
SEED_FROM = Path(os.environ.get("SEED_FROM") or Path.home() / "bnge-tests/tunnels/cache")
RAW = ("dem", "mapdata", "pagesdata", "roadz", "gravity", "cdn", "airports", "seacarve")
RADIUS = 30
SCRATCH = ROOT / "cache"        # (widecorpus build.py builds only below <WIDE_CORPUS_ROOT>/cache)
os.environ["WIDE_CORPUS_ROOT"] = str(ROOT)


def places(ids=None):
    rows = json.loads((HERE / "refs.json").read_text())["mouths"]
    out = [dict(id="tun-" + r["id"], name=f"{r['place']} {r['mouth']}", lat=r["lat"], lon=r["lon"]) for r in rows]
    if ids:
        want = set(ids.split(","))
        out = [p for p in out if p["id"] in want or p["id"][4:] in want]
    return out


def reflink(src, dst):
    subprocess.run(["cp", "-a", "--reflink=auto", str(src), str(dst)], check=True)


def seed():
    s = SCRATCH / "seed"
    if not s.is_dir():
        tmp = SCRATCH / "seed.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        for part in RAW:
            if (SEED_FROM / part).is_dir():
                reflink(SEED_FROM / part, tmp / part)
        tmp.rename(s)
    return s


def tree_modules(tree):
    sys.path[:0] = [str(tree / "tools/widecorpus")]
    import common
    import run as wrun
    return common, wrun


def warm(ids):
    common, wrun = tree_modules(TREE)
    s = seed()
    for p in places(ids):
        mark = ROOT / "warm" / (p["id"] + ".ok")
        if mark.exists():
            continue
        mark.parent.mkdir(parents=True, exist_ok=True)
        ok = True
        for label, env in (("default", {}), ("revamped", dict(common.REVAMPED, BNE_ROADSYS="surface"))):
            with tempfile.TemporaryDirectory(prefix="tc-", dir=SCRATCH) as d:
                try:
                    wrun.build_process(p, s, Path(d) / "tiles", RADIUS, env, False, Path(d) / "warm.log")
                except Exception as e:
                    ok = False
                    print(p["id"], label, "warm FAILED", type(e).__name__, str(e)[:200], flush=True)
                    log = Path(d) / "warm.log"
                    if log.exists():
                        (ROOT / "warm" / (p["id"] + f".{label}.log")).write_text(log.read_text()[-20000:])
        for item in s.iterdir():        # (only the raw inputs stay: derived caches are each build's own)
            if item.is_dir() and item.name not in RAW:
                shutil.rmtree(item)
        if ok:
            mark.write_text("ok\n")
        print(p["id"], "warm", "ok" if ok else "partial", flush=True)


def build(label, ids, env, tree):
    common, wrun = tree_modules(tree)
    from checks import measure
    s = seed()
    scan = Path(os.environ.get("SCANNER") or Path.home() / "bnge-tests/roadsys/m5/fh/bnge-tests/widecorpus/cache/m5/bin/scan")
    auditor = Path(os.environ.get("AUDITOR") or ROOT / "bin/roadsys")
    for p in places(ids):
        out = ROOT / "run" / p["id"]
        res = out / (label + ".json")
        if res.exists():
            continue
        out.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="tc-", dir=SCRATCH) as d:
            work = Path(d)
            cache = work / "cache"
            cache.mkdir()
            for part in RAW:
                if (s / part).is_dir():
                    reflink(s / part, cache / part)
            try:
                wrun.build_process(p, cache, work / "tiles", RADIUS, env, True, out / (label + ".log"))
                por = out / (label + ".portals.txt")
                por.unlink(missing_ok=True)
                os.environ["ROADSYS_AUDIT_PORTALS"] = str(por)
                tiles = measure(p, work / "tiles", scan, auditor)
            except Exception as e:
                (out / ("error-" + label + ".txt")).write_text(f"{type(e).__name__}: {e}")
                print(p["id"], label, "FAILED", type(e).__name__, str(e)[:200], flush=True)
                continue
            common.save(res, dict(id=p["id"], env=env, tree=str(tree), tiles=tiles))
            if os.environ.get("KEEP_TILES"):
                keep = ROOT / "tiles" / p["id"] / label
                shutil.rmtree(keep, ignore_errors=True)
                keep.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(work / "tiles", keep)
        print(p["id"], label, "done", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("warm", "build", "list"))
    ap.add_argument("label", nargs="?")
    ap.add_argument("--ids")
    ap.add_argument("--revamped", action="store_true", help="the live Revamped roads switches (+ BNE_ROADSYS=surface)")
    ap.add_argument("--env", action="append", default=[])
    ap.add_argument("--tree", default=str(TREE))
    a = ap.parse_args()
    SCRATCH.mkdir(parents=True, exist_ok=True)
    if a.cmd == "list":
        for p in places(a.ids):
            print(p["id"], p["lat"], p["lon"])
    elif a.cmd == "warm":
        warm(a.ids)
    else:
        tree = Path(a.tree)
        common, _ = tree_modules(tree)
        env = dict(common.REVAMPED, BNE_ROADSYS="surface") if a.revamped else {}
        for kv in a.env:
            k, v = kv.split("=", 1)
            if v:
                env[k] = v
            else:
                env.pop(k, None)
        build(a.label, a.ids, env, tree)


if __name__ == "__main__":
    main()
