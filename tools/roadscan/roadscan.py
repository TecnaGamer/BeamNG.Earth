"""roadscan: scan hundreds of miles of generated road for problems, without anyone reviewing each piece of it.

A corridor (a route between two places on the OSM road graph, a polyline, or a box of roads of chosen classes) is cut
into the geotiles it crosses; each tile is built offline with the production road pipeline (tools/widecorpus build.py,
as the wide corpus builds its places), written as a pack (export.py) and scanned by native/roadscan (Rust): the M0
audit's drivability checks with each tile's neighbours placed beside it (so the seams are checked), a virtual drive
along every lane (steps, deck-end and tunnel-mouth steps, the ride in g at the class's speed, grades), the drawn road
against the physics ground, markings off the road, the drawn width against OSM's. The findings are clustered into
issues, ranked by severity and extent, placed at mileposts along the route; report.py makes the browsable page with
aerial | built plan views of the worst. Two code states built from the same snapshot of inputs compare issue by issue
(the regression gate).

    python3 tools/roadscan/roadscan.py corridor NAME --route "Baltimore, MD" "New York, NY" [--via LAT,LON ...]
    python3 tools/roadscan/roadscan.py corridor NAME --from LAT,LON --to LAT,LON [--clip A,B]   # miles A-B only
    python3 tools/roadscan/roadscan.py corridor NAME --polyline FILE      # JSON [[lat, lon], ...] / GeoJSON / "lat,lon" lines
    python3 tools/roadscan/roadscan.py corridor NAME --region S,W,N,E --classes motorway,trunk
    python3 tools/roadscan/roadscan.py warm   NAME [--revamped] [--env K=V ...]       # online: the inputs' snapshot
    python3 tools/roadscan/roadscan.py build  NAME --state S [--tree DIR] [--revamped] [--env K=V ...]   # offline
    python3 tools/roadscan/roadscan.py scan   NAME --state S                  # export + scan + rank
    python3 tools/roadscan/roadscan.py report NAME --state S [--top 30] [--against S0] [--no-aerial]
    python3 tools/roadscan/roadscan.py run    NAME --state S [build options]  # warm, build, scan, report
    python3 tools/roadscan/roadscan.py status NAME

Every step resumes where it stopped. Scratch: ~/bnge-tests/roadscan/NAME (ROADSCAN_ROOT for another folder under
~/bnge-tests); never the game's cache. CPU: --cpus N (default half the machine's) bounds every process (affinity),
--jobs the parallel builds (default: --cpus / 2, at most 3), all at nice 19 in the idle CPU and I/O classes; nothing
heavy starts while a gate is queued on the machine (ROADSCAN_WAIT_FOR, default "[g]ate_when_free"), checked before
every batch. On a machine without cargo set ROADSCAN_BIN to a static build (cargo build --release --target
x86_64-unknown-linux-musl). A state is one code tree and
one set of switches; states of one corridor share the inputs' snapshot (cache/seed, reflinked), so only the code
differs between them (the online warm builds only fill the snapshot: build.py records what the scan reads only
offline). Linux and Windows (no affinity / idle class there); the Rust part needs cargo.

Cost (2026-11-26, PC under other agents' load): ~10 geotiles per mile of route; a cold online warm ~40 s per tile
(downloads), an offline build ~10-25 s CPU per tile, export ~0.1 s and scan ~0.3 s per tile."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import site
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
TREE = HERE.parents[1]
TESTS = Path.home() / "bnge-tests"
BASE = Path(os.environ.get("ROADSCAN_ROOT") or TESTS / "roadscan")
RAW = ("dem", "mapdata", "pagesdata", "roadz", "gravity", "cdn", "airports", "seacarve")


def corridor_dir(name):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
        raise SystemExit(f"corridor name {name!r}: lower-case letters, digits, _ and - only")
    d = (BASE / name).resolve()
    if not d.is_relative_to(TESTS.resolve()):
        raise SystemExit(f"{d}: the scratch must be under {TESTS}")
    return d


def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=1, sort_keys=True))
    os.replace(tmp, path)


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


MAX_JOBS = 3                # builds at once (with this process: at most 4 heavy processes, the PC's etiquette)
WAIT_FOR = os.environ.get("ROADSCAN_WAIT_FOR", "[g]ate_when_free")      # a gate queued on this machine: start nothing


def budget(cpus):
    """This process and its children: CPUs 0..cpus-1 of the allowed set, nice 19, the idle CPU and I/O classes
    (Linux)."""
    try:
        allowed = sorted(os.sched_getaffinity(0))
        n = min(cpus, len(allowed)) if cpus else max(1, len(allowed) // 2)
        os.sched_setaffinity(0, allowed[:max(1, n)])
    except (AttributeError, OSError):
        n = cpus or max(1, (os.cpu_count() or 2) // 2)
    try:
        os.nice(max(0, 19 - os.nice(0)))
    except (AttributeError, OSError):
        pass
    try:
        os.sched_setscheduler(0, os.SCHED_IDLE, os.sched_param(0))
    except (AttributeError, OSError):
        pass
    if shutil.which("ionice"):
        subprocess.run(["ionice", "-c3", "-p", str(os.getpid())], stdin=subprocess.DEVNULL, capture_output=True)
    return n


def wait_for_gate():
    """Before anything heavy: while a gate is queued or running here (ROADSCAN_WAIT_FOR, a pgrep -f pattern), wait."""
    if not WAIT_FOR or os.name == "nt" or not shutil.which("pgrep"):
        return
    said = False
    while subprocess.run(["pgrep", "-f", WAIT_FOR], capture_output=True, stdin=subprocess.DEVNULL).returncode == 0:
        if not said:
            print(f"waiting: a gate ({WAIT_FOR}) is on this machine", flush=True)
            said = True
        time.sleep(60)


def child_env(runtime, env):
    """A build's environment: no BNE_/BNGE_ settings but its own, its home and temp in its runtime folder (never the
    game's cache), one thread per numeric library, the wide corpus's build settings."""
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "tmp").mkdir(exist_ok=True)
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("BNE_", "BNGE_"))}
    clean.update(env, HOME=str(runtime), XDG_CACHE_HOME=str(runtime / "xdg"), TMPDIR=str(runtime / "tmp"),
                 TEMP=str(runtime / "tmp"), TMP=str(runtime / "tmp"), PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1",
                 OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", RAYON_NUM_THREADS="1",
                 BNGE_NET_SHARED=str(runtime.parent / "netgate"), BNE_REVISIT="off", BNGE_PLAN_RUN="roadscan",
                 WIDE_CORPUS_ROOT=str(runtime.parent.parent))
    try:
        clean["PYTHONPATH"] = site.getusersitepackages()
    except AttributeError:
        pass
    return clean


# --- the corridor -------------------------------------------------------------------------------------------------

def _ll(s):
    la, lo = (float(v) for v in s.split(","))
    if not (-90 <= la <= 90 and -180 <= lo <= 180):
        raise ValueError(s)
    return la, lo


def read_polyline(path):
    """[[lat, lon], ...] from JSON [[lat, lon]], GeoJSON (LineString / MultiLineString / a FeatureCollection of them,
    lon-lat) or text lines "lat,lon"; a MultiLineString's parts joined in order."""
    text = Path(path).read_text()
    try:
        data = json.loads(text)
    except ValueError:
        return [list(_ll(ln.strip())) for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    if isinstance(data, list):
        return [[float(p[0]), float(p[1])] for p in data]
    out = []

    def geom(g):
        if g["type"] == "LineString":
            out.extend([float(c[1]), float(c[0])] for c in g["coordinates"])
        elif g["type"] == "MultiLineString":
            for part in g["coordinates"]:
                out.extend([float(c[1]), float(c[0])] for c in part)
    if data.get("type") == "FeatureCollection":
        for f in data["features"]:
            geom(f["geometry"])
    elif data.get("type") == "Feature":
        geom(data["geometry"])
    else:
        geom(data)
    return out


def _dist(a, b):
    c = math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot((a[0] - b[0]) * 111320.0, (a[1] - b[1]) * 111320.0 * c)


def route_points(args, d):
    """The route asked for: OSRM on OpenStreetMap through the server's own routing (one request, its fair use), the
    places geocoded by the server's search. In a child process whose home is the corridor's runtime folder."""
    jf = d / "runtime" / "route_job.json"
    save(jf, dict(route=args.route, frm=args.frm, to=args.to, via=args.via or []))
    r = subprocess.run([sys.executable, str(HERE / "roadscan.py"), "_route", str(jf)],
                       env=child_env(d / "runtime" / "net", {}), cwd=d / "runtime", capture_output=True, text=True,
                       stdin=subprocess.DEVNULL)
    if r.returncode:
        raise SystemExit(r.stderr.strip()[-2000:] or "route failed")
    return json.loads(r.stdout.strip().splitlines()[-1])


def _route(jf):
    sys.path.insert(0, str(TREE / "server"))
    import geocode
    import routing
    job = read(jf)
    ends = []
    if job["route"]:
        for q in job["route"]:
            hits = geocode.search(q, full=True)
            if not hits:
                raise SystemExit(f"no place found for {q!r}")
            ends.append((float(hits[0]["lat"]), float(hits[0]["lon"]), hits[0].get("name", q)))
    else:
        for s in (job["frm"], job["to"]):
            la, lo = _ll(s)
            ends.append((la, lo, s))
    via = [_ll(v) for v in job["via"]]
    r = routing.plan(ends[0][0], ends[0][1], ends[1][0], ends[1][1], via=via)
    print(json.dumps(dict(points=r["points"], dist=r["dist"], ends=[list(e) for e in ends])))


def cmd_corridor(args):
    d = corridor_dir(args.name)
    if (d / "corridor.json").exists() and not args.replace:
        raise SystemExit(f"{d / 'corridor.json'} exists (--replace to make it again; built tiles are kept)")
    if args.route or args.frm:
        r = route_points(args, d)
        lines = [r["points"]]
        source = dict(kind="route", ends=r["ends"], via=args.via or [], osrm_m=r["dist"])
    elif args.polyline:
        lines = [read_polyline(args.polyline)]
        source = dict(kind="polyline", file=str(args.polyline))
    elif args.region:
        box = [float(v) for v in args.region.split(",")]
        classes = args.classes.split(",")
        jf = d / "runtime" / "region_job.json"
        save(jf, dict(mode="region", cache=str(d / "cache" / "seed"), out=str(d / "runtime" / "region.json"), box=box,
                      classes=classes))
        subprocess.run([sys.executable, str(HERE / "buildtiles.py"), str(TREE), str(jf)],
                       env=child_env(d / "runtime" / "net", {}), cwd=d / "runtime", check=True,
                       stdin=subprocess.DEVNULL)
        roads = read(d / "runtime" / "region.json")
        lines = [r["ll"] for r in roads]
        source = dict(kind="region", box=box, classes=classes, roads=len(roads))
    else:
        raise SystemExit("one of --route, --from/--to, --polyline, --region")
    lines = [ln for ln in lines if len(ln) >= 2]
    if not lines:
        raise SystemExit("no road line")
    # the tiles (native/roadscan corridor.rs): the lines, the route clipped to --clip miles
    rt = d / "runtime"
    rt.mkdir(parents=True, exist_ok=True)
    (rt / "lines.txt").write_text("\n\n".join("\n".join(f"{a:.7f},{o:.7f}" for a, o in ln) for ln in lines) + "\n")
    cmd = [str(scanner(2)), "tiles", str(rt / "lines.txt"), str(rt / "tiles.tsv"), "--halfwidth", str(args.halfwidth)]
    route = source["kind"] != "region"
    if route:
        cmd += ["--route-out", str(d / "route.bin"), "--route-txt", str(rt / "route.txt")]
    if args.clip:
        cmd += ["--clip", args.clip]
        source["clip_miles"] = args.clip
    subprocess.run(cmd, check=True, stdin=subprocess.DEVNULL)
    rows = (rt / "tiles.tsv").read_text().splitlines()
    tiles = [dict(key=w[0], gx=int(w[1]), gy=int(w[2]), s=float(w[3]), lat=float(w[4]), lon=float(w[5]))
             for w in (r.split("\t") for r in rows[1:] if not r.startswith("#"))]
    miles = float(rows[-1].split()[-1])
    pts = [[float(v) for v in r.split(",")] for r in (rt / "route.txt").read_text().split()] if route else []
    save(d / "corridor.json", dict(name=args.name, source=source, halfwidth_m=args.halfwidth, miles=round(miles, 2),
                                   route=pts, tiles=tiles))
    print(f"{args.name}: {miles:.1f} miles of road, {len(tiles)} tiles ({len(tiles) / max(miles, 0.01):.1f} per mile)")


# --- builds -------------------------------------------------------------------------------------------------------

def state_env(args):
    env = {}
    if getattr(args, "revamped", False):
        sys.path.insert(0, str(TREE / "tools" / "widecorpus"))
        import common
        env.update(common.REVAMPED)
    for e in getattr(args, "env", None) or []:
        k, _, v = e.partition("=")
        if not k.startswith("BNE_"):
            raise SystemExit("--env takes BNE_* settings")
        env[k] = v
    return env


def run_batches(d, label, tiles, cache, offline, tree, env, jobs):
    """Every tile not yet built into cache/tiles/LABEL, in batches of consecutive tiles, `jobs` workers at once."""
    out = d / "cache" / "tiles" / label
    todo = [t for t in tiles if not (out / t["key"] / "tiles.json").exists()]
    if not todo:
        return 0
    per = max(1, min(8, math.ceil(len(todo) / max(1, jobs))))
    batches = [todo[i:i + per] for i in range(0, len(todo), per)]
    print(f"{label}: {len(todo)} tiles to build, {len(batches)} batches, {jobs} at once", flush=True)
    running, failed, t0, done = [], 0, time.monotonic(), 0
    logs = d / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    serial = len(list(logs.glob(f"{label}-*.log")))
    slots = list(range(jobs))
    while batches or running:
        while batches and slots:
            wait_for_gate()
            b = batches.pop(0)
            slot = slots.pop(0)
            jf = d / "runtime" / f"job-{label}-{serial:05d}.json"
            save(jf, dict(mode="build", cache=str(cache), out=str(out), offline=offline,
                          tiles=[dict(key=t["key"], lat=t["lat"], lon=t["lon"]) for t in b]))
            log = open(logs / f"{label}-{serial:05d}.log", "w")
            serial += 1
            rt = d / "runtime" / f"w{slot}"
            p = subprocess.Popen([sys.executable, str(HERE / "buildtiles.py"), str(tree), str(jf)],
                                 env=child_env(rt, env), cwd=rt, stdout=log, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, start_new_session=True)
            running.append((p, log, b, slot))
        time.sleep(1.0)
        for item in list(running):
            p, log, b, slot = item
            if p.poll() is None:
                continue
            log.close()
            running.remove(item)
            slots.append(slot)
            done += len(b)
            failed += sum(1 for t in b if (out / (t["key"] + ".err")).exists())
            el = time.monotonic() - t0
            print(f"{label}: {done}/{len(todo)} tiles, {failed} failed, {el / 60:.1f} min "
                  f"({el / max(done, 1):.1f} s per tile)", flush=True)
    return failed


def corridor(name):
    d = corridor_dir(name)
    if not (d / "corridor.json").exists():
        raise SystemExit(f"no corridor {name}: make it with the corridor command")
    return d, read(d / "corridor.json")


def warm_label(env):
    return "warm" if not env else "warm-" + digest(env)[:8]


def cmd_warm(args):
    d, c = corridor(args.name)
    env = state_env(args)
    label = warm_label(env)
    save(d / "states" / (label + ".json"), dict(tree=str(TREE), env=env, offline=False))
    bad = run_batches(d, label, c["tiles"], d / "cache" / "seed", False, TREE, env, args.jobs)
    print(f"{label}: done, {bad} failed (logs in {d / 'logs'}; KEY.err beside the tiles)")


def cmd_build(args):
    d, c = corridor(args.name)
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", args.state) or args.state.startswith("warm") or args.state in RAW \
            or args.state in ("seed", "tiles"):
        raise SystemExit("--state: lower-case letters, digits, _ and - (not warm*, seed, tiles or an input's name)")
    tree = Path(args.tree).resolve() if args.tree else TREE
    env = state_env(args)
    meta = dict(tree=str(tree), env=env, offline=True)
    sf = d / "states" / (args.state + ".json")
    if sf.exists() and read(sf) != meta and not args.fresh:
        raise SystemExit(f"state {args.state} was built with {read(sf)} (--fresh to build it again)")
    if args.fresh:
        shutil.rmtree(d / "cache" / "tiles" / args.state, ignore_errors=True)
        shutil.rmtree(d / "cache" / args.state, ignore_errors=True)
    save(sf, meta)
    cache = d / "cache" / args.state
    seed = d / "cache" / "seed"
    if not (cache / ".from_seed").exists():
        if not seed.is_dir():
            raise SystemExit("no inputs yet: run warm first")
        cache.mkdir(parents=True, exist_ok=True)
        for part in RAW:
            if (seed / part).is_dir() and not (cache / part).exists():
                # (a reflink copy where the file system has them: a snapshot, never a link into another cache)
                r = subprocess.run(["cp", "-a", "--reflink=auto", str(seed / part), str(cache / part)],
                                   stdin=subprocess.DEVNULL)
                if r.returncode:
                    shutil.copytree(seed / part, cache / part)
        (cache / ".from_seed").write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
    bad = run_batches(d, args.state, c["tiles"], cache, True, tree, env, args.jobs)
    print(f"{args.state}: done, {bad} failed")


# --- scan ---------------------------------------------------------------------------------------------------------

def scanner(jobs):
    """This checkout's native/roadscan, built (incrementally) into the scratch's bin; ROADSCAN_BIN: a binary built
    elsewhere (a machine without cargo: the static musl build)."""
    if os.environ.get("ROADSCAN_BIN"):
        return Path(os.environ["ROADSCAN_BIN"])
    wait_for_gate()
    target = BASE / "bin" / "roadscan-target"
    subprocess.run(["cargo", "build", "--release", "--quiet", "-j", str(max(1, jobs)), "--bin", "roadscan"],
                   cwd=TREE / "native" / "roadscan", check=True, stdin=subprocess.DEVNULL,
                   env={**os.environ, "CARGO_TARGET_DIR": str(target)})
    return target / "release" / ("roadscan.exe" if os.name == "nt" else "roadscan")


def cmd_scan(args):
    d, c = corridor(args.name)
    tiles_dir = d / "cache" / "tiles" / args.state
    if not tiles_dir.is_dir():
        raise SystemExit(f"state {args.state} has no builds")
    sources = []
    for t in c["tiles"]:
        f = tiles_dir / t["key"] / "place.json"
        if (tiles_dir / t["key"] / "tiles.json").exists() and f.exists():
            p = read(f)
            sources.append(dict(dir=str(tiles_dir / t["key"]), lat=p["lat"], lon=p["lon"]))
    scan_sources(d / "scan" / args.state, sources, args.jobs, args.threads, d / "route.bin", c.get("halfwidth_m", 40.0))
    summary(d / "scan" / args.state, len(c["tiles"]))


def scan_sources(out, sources, jobs, threads, route=None, halfwidth=40.0, force=False):
    """Packs of the build folders, the scan and the ranking into out; the scan's exit code."""
    sys.path.insert(0, str(HERE))
    import export
    out.mkdir(parents=True, exist_ok=True)
    wait_for_gate()
    t0 = time.monotonic()
    bad = export.export(out / "index.bin", sources, jobs)
    t1 = time.monotonic()
    exe = scanner(jobs)
    cmd = [str(exe), "scan", str(out / "index.bin"), str(out), "--threads", str(threads)] + (["--force"] if force else [])
    wait_for_gate()
    r = subprocess.run(cmd, stdin=subprocess.DEVNULL)
    t2 = time.monotonic()
    rank = [str(exe), "rank", str(out), "--halfwidth", str(halfwidth)]
    if route is not None and Path(route).exists():
        rank += ["--route", str(route)]
    subprocess.run(rank, check=True, stdin=subprocess.DEVNULL)
    save(out / "timing.json", dict(export_s=round(t1 - t0, 1), scan_s=round(t2 - t1, 1), packs=len(sources),
                                   export_failures=[list(b) for b in bad], scan_exit=r.returncode))
    return r.returncode


def summary(out, total):
    s = read(out / "issues.json")
    print(f"{s['tiles']} of {total} tiles scanned, {len(s['issues'])} issues")
    print((out / "summary.tsv").read_text())
    for i in s["issues"][:10]:
        mi = f"mile {i['mile']:.2f}" if i.get("mile") is not None else ""
        print(f"  {i['rank']:3d} sev {i['severity']} {i['kind']:22s} {i['magnitude']} {i['unit']:5s} "
              f"{i['lat']:.6f},{i['lon']:.6f} {mi}")


def report(scan, top, title, miles=None, against=None, aerial=True, jobs=2):
    """The report (native/roadscan report.rs), then the aerials it asks for (tools/ortho_hi.py: the project's own
    fetcher and sources), each once."""
    cmd = [str(scanner(jobs)), "report", str(scan), "--top", str(top), "--title", title]
    if miles:
        cmd += ["--miles", str(miles)]
    if against:
        cmd += ["--against", str(against)]
    page = subprocess.run(cmd, check=True, stdin=subprocess.DEVNULL, capture_output=True, text=True).stdout.strip()
    if aerial:
        ad = Path(scan) / "report" / "aerial"
        ad.mkdir(parents=True, exist_ok=True)
        net = Path(scan) / "report" / "net"
        env = child_env(net, {})
        for ln in (Path(scan) / "report" / "aerials.tsv").read_text().splitlines()[1:]:
            name, la, lo, half = ln.split("\t")
            if not (ad / name).exists():
                subprocess.run([sys.executable, str(TREE / "tools" / "ortho_hi.py"), la, lo, half, str(ad / name), "auto", "480"],
                               env=env, cwd=net, stdin=subprocess.DEVNULL, capture_output=True, timeout=300)
    return page


def cmd_report(args):
    d, c = corridor(args.name)
    against = d / "scan" / args.against if args.against else None
    print(report(d / "scan" / args.state, args.top, f"{args.name}: {args.state}", c.get("miles"), against,
                 not args.no_aerial, args.jobs))


def cmd_run(args):
    cmd_warm(args)
    cmd_build(args)
    cmd_scan(args)
    if args.top:
        cmd_report(args)


def cmd_status(args):
    d, c = corridor(args.name)
    print(f"{args.name}: {c['miles']} miles, {len(c['tiles'])} tiles, source {c['source'].get('kind')}")
    for label_dir in sorted((d / "cache" / "tiles").glob("*")):
        n = sum(1 for t in c["tiles"] if (label_dir / t["key"] / "tiles.json").exists())
        e = len(list(label_dir.glob("*.err")))
        scanned = d / "scan" / label_dir.name / "issues.json"
        print(f"  {label_dir.name:16s} built {n}/{len(c['tiles'])}, {e} failed"
              + (f", scanned: {len(read(scanned)['issues'])} issues" if scanned.exists() else ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common_opts(p, build=False):
        p.add_argument("name")
        p.add_argument("--cpus", type=int, default=0, help="CPUs for this run (default: half the machine's)")
        p.add_argument("--jobs", type=int, default=0, help="builds / exports at once (default: CPUs / 2)")
        if build:
            p.add_argument("--revamped", action="store_true", help="the Experimental Revamped roads switches")
            p.add_argument("--env", action="append", help="BNE_*=value (repeatable)")
    p = sub.add_parser("corridor")
    p.add_argument("name")
    p.add_argument("--route", nargs=2, metavar=("FROM", "TO"))
    p.add_argument("--from", dest="frm")
    p.add_argument("--to")
    p.add_argument("--via", action="append")
    p.add_argument("--polyline", type=Path)
    p.add_argument("--region")
    p.add_argument("--classes", default="motorway,trunk,motorway_link,trunk_link")
    p.add_argument("--halfwidth", type=float, default=40.0, help="m either side of the line whose tiles are built")
    p.add_argument("--clip", help="A,B: only miles A to B of the route")
    p.add_argument("--replace", action="store_true")
    common_opts(sub.add_parser("warm"), True)
    p = sub.add_parser("build")
    common_opts(p, True)
    p.add_argument("--state", required=True)
    p.add_argument("--tree", help="another checkout (a worktree) to build with")
    p.add_argument("--fresh", action="store_true")
    for name in ("scan", "report", "run"):
        p = sub.add_parser(name)
        common_opts(p, name == "run")
        p.add_argument("--state", required=True)
        p.add_argument("--threads", type=int, default=0)
        p.add_argument("--top", type=int, default=30 if name != "scan" else 0)
        p.add_argument("--against")
        p.add_argument("--no-aerial", action="store_true")
        if name == "run":
            p.add_argument("--tree")
            p.add_argument("--fresh", action="store_true")
    sub.add_parser("status").add_argument("name")
    sub.add_parser("_route").add_argument("job")
    args = ap.parse_args()
    if args.cmd == "_route":
        return _route(args.job)
    if args.cmd in ("warm", "build", "scan", "report", "run"):
        n = budget(args.cpus)
        args.jobs = min(args.jobs or max(1, n // 2), MAX_JOBS)
        if hasattr(args, "threads"):
            args.threads = min(args.threads or n, MAX_JOBS)
    d = corridor_dir(args.name)
    d.mkdir(parents=True, exist_ok=True)
    if args.cmd in ("warm", "build", "run") and os.name != "nt":
        import fcntl
        lock = open(d / "run.lock", "a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit(f"another run holds {d / 'run.lock'}")
    dict(corridor=cmd_corridor, warm=cmd_warm, build=cmd_build, scan=cmd_scan, report=cmd_report, run=cmd_run,
         status=cmd_status)[args.cmd](args)


if __name__ == "__main__":
    main()
