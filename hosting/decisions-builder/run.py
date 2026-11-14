#!/usr/bin/env python3
"""The shared decision-archive builder (systemd user unit bnge-decisions-builder, hosting/systemd/; docs/HOSTING.md;
server/decisions.py, tools/build_decisions.py): a thin wrapper round a pinned server checkout in ./src (the commit in
./COMMIT: `git archive <commit> server tools/build_decisions.py docs/PLACES.md`), adding what a service needs:

  * build: each place line of a places file built in turn (tools/build_decisions.py build, its own process, without
    any R2 variable in its environment), the signing seed read by that process from --key only; a disk guard (no
    place is started while the disk has less than --min-free-gb free) and a cache cap (none while ./cache is over
    --cache-cap-gb); per-place timings appended to ./stats.jsonl;
  * upload: the R2 credentials from the unit's EnvironmentFile (%h/bnge-builder/r2.env: R2_ACCOUNT_ID,
    R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, optional R2_BUCKET) mapped to the aws CLI's names (never printed, never
    on a command line); every block and the manifest checked against the keys compiled into earthfast first (what a
    player's server accepts); the new block files first (names = content hashes, Cache-Control immutable), the
    manifest last (no-cache), to s3://<bucket>/dec/d<FORMAT>/<vkey>/ (https://tiles.tgforever.net/dec/...);
  * all: build, then upload every version folder built by this run.

    run.py build  --places-file FILE [--procs 4] [--km 6]
    run.py upload [--vkey V]                      # default: the newest version folder in ./out
    run.py all    --places-file FILE
    run.py status                                 # free disk, cache size, version folders (no network, no R2)
    run.py prune-out --keep V [--grace-days 7]    # other version folders of ./out removed (after a release), and
                                                  # V's replaced block files past the grace period (here, and in R2
                                                  # when run through the unit)

The output of the current version stays in ./out: a later build of the same version merges into its blocks and
rewrites the manifest from current.json, so deleting it would drop the uploaded blocks from the next manifest.
Place lines: "block X,Y" (one z12 block), or anything tools/build_decisions.py --places takes (a name,
"lat,lon[,km]", @top:N, @asked:FILE of "x,y" z12 blocks); "#" starts a comment.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "src")
CACHE = os.path.join(HERE, "cache")
OUT = os.path.join(HERE, "out")
STATS = os.path.join(HERE, "stats.jsonl")
KEY = os.path.expanduser("~/bnge-builder/decisions-signing.key")
AWS = os.path.join(HERE, "venv", "bin", "aws")                       # (this venv's aws CLI, else the aerial builder's)
if not os.path.exists(AWS):
    AWS = os.path.expanduser("~/bnge-builder/aerial/venv/bin/aws")
PY = sys.executable
GRACE_DAYS = 7.0            # a replaced block file stays this long (local and in R2): clients holding the old manifest
BLOCK_CACHE = "public, max-age=31536000, immutable"     # (a block's name is its content hash: never changes)
MANIFEST_CACHE = "no-cache"
sys.path.insert(0, os.path.join(SRC, "server"))


def dfmt():
    import decisions
    return f"d{decisions.FORMAT}"
SECRET_NAMES = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY", "R2_ENDPOINT")


def free_gb(path):
    return shutil.disk_usage(path).free / 1e9


def du_gb(path):
    n = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                n += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return n / 1e9


def commit():
    with open(os.path.join(HERE, "COMMIT")) as f:
        return f.read().strip()


def place_lines(path):
    out = []
    with open(path) as f:
        for ln in f:
            ln = ln.split("#", 1)[0].strip()
            if ln:
                out.append(ln)
    return out


def versions():
    root = os.path.join(OUT, dfmt())
    if not os.path.isdir(root):
        return []
    return sorted(os.listdir(root), key=lambda v: os.path.getmtime(os.path.join(root, v)))


WARM = r"""
import sys
sys.path.insert(0, sys.argv[2])
import netfetch, railtags
f = netfetch.Fetcher()
got = {}
for name, cls, on in (("rails", railtags.RailTags, railtags.ON), ("lanes", railtags.LaneTags, railtags.LANES_ON),
                      ("stations", railtags.StationTags, railtags.STATIONS_ON)):
    if on:
        got[name] = cls(sys.argv[1], f).version()
print("weekly datasets:", got, flush=True)
sys.exit(0 if all(v is not None for v in got.values()) else 1)
"""


def warm(env):
    """The weekly datasets' manifests pinned in the builder's cache before any planning (a cold cache's first
    workers keyed their plans without them: build_decisions dropped those as another version and never planned the
    cells again, 2026-11-13). True when every one is known."""
    for _ in range(3):
        if subprocess.run([PY, "-c", WARM, CACHE, os.path.join(SRC, "server")], env=env, cwd=HERE).returncode == 0:
            return True
        time.sleep(30)
    return False


def packed_plans(sizes):
    """Count of plan items in the blocks a place wrote (what players can use), by block."""
    sys.path.insert(0, os.path.join(SRC, "server"))
    import fast
    out = {}
    for b in sizes:
        bx, by = b.split("/")
        for vk in versions():
            try:
                with open(os.path.join(OUT, dfmt(), vk, "current.json")) as f:
                    h = json.load(f).get(b)
            except (OSError, ValueError):
                h = None
            p = os.path.join(OUT, dfmt(), vk, "12", bx, f"{by}.{h}.bngd")
            if h and os.path.exists(p):
                with open(p, "rb") as f:
                    keys = fast._ef.dec_block_info(f.read())[4]
                out[b] = sum(1 for k in keys if k.startswith("p/"))
    return out


def build(a):
    lines = place_lines(a.places_file)
    env = {k: v for k, v in os.environ.items() if k not in SECRET_NAMES}
    os.makedirs(CACHE, exist_ok=True)
    os.makedirs(OUT, exist_ok=True)
    if not warm(env):
        print("the weekly datasets' manifests (rails, lanes, stations) could not be had: not building (the plans "
              "would be keyed without them)", flush=True)
        return 4
    print(f"decisions builder (commit {commit()[:12]}): {len(lines)} place lines, procs {a.procs}, guard "
          f"{a.min_free_gb:.0f} GB free, cache cap {a.cache_cap_gb:.0f} GB", flush=True)
    failed = 0
    for i, place in enumerate(lines, 1):
        f, c = free_gb(HERE), du_gb(CACHE)
        if f < a.min_free_gb:
            print(f"disk guard: {f:.1f} GB free < {a.min_free_gb:.0f} GB: stopping before {place!r}", flush=True)
            return 3
        if c > a.cache_cap_gb:
            print(f"cache cap: {c:.1f} GB > {a.cache_cap_gb:.0f} GB: stopping before {place!r} (rm -rf "
                  f"{CACHE} frees it; it is only downloads)", flush=True)
            return 3
        st_path = os.path.join(HERE, "work", f"stats_{int(time.time())}_{i}.json")
        os.makedirs(os.path.dirname(st_path), exist_ok=True)
        spec = place
        if place.startswith("block "):          # ("block X,Y": one z12 block, as build_decisions' @asked:FILE)
            x, y = (int(v) for v in place[6:].split(","))
            spec = os.path.join(HERE, "work", f"block_{x}_{y}.txt")
            with open(spec, "w") as fh:
                fh.write(f"{x},{y}\n")
            spec = "@asked:" + spec
        t0 = time.time()
        print(f"[{i}/{len(lines)}] {place}: {f:.1f} GB free, cache {c:.2f} GB", flush=True)
        r = subprocess.run([PY, os.path.join(SRC, "tools", "build_decisions.py"), "build", "--places", spec,
                            "--km", str(a.km), "--cache", CACHE, "--out", OUT, "--key", KEY, "--procs",
                            str(a.procs), "--stats", st_path], env=env, cwd=HERE)
        dt = time.time() - t0
        if r.returncode != 0:
            failed += 1
            print(f"[{i}/{len(lines)}] {place}: build failed (exit {r.returncode}) after {dt:.0f} s", flush=True)
            continue
        try:
            with open(st_path) as fh:
                rows = json.load(fh)
        except (OSError, ValueError):
            rows = []
        with open(STATS, "a") as fh:
            for row in rows:
                row["packed_plans"] = packed_plans(row.get("blocks", {}))
                row = dict(row, line=place, commit=commit()[:12], at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                           cache_gb=round(du_gb(CACHE), 2))
                fh.write(json.dumps(row, sort_keys=True) + "\n")
                kb = sum(row.get("blocks", {}).values()) / 1e3
                print(f"[{i}/{len(lines)}] {row['place']}: {row['cells']} cells, {row['plans']} plans, "
                      f"{row['facts']} facts, {len(row.get('blocks', {}))} blocks {kb:.0f} KB, {row['wall_s']} s "
                      f"wall, {row['cpu_s']} s CPU, vkey {row['vkey']}; plans packed per block "
                      f"{row['packed_plans']}", flush=True)
    return 1 if failed else 0


def creds():
    need = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
    if not all(os.environ.get(k) for k in need):
        return None
    env = dict(os.environ)
    env["AWS_ACCESS_KEY_ID"] = env["R2_ACCESS_KEY_ID"]
    env["AWS_SECRET_ACCESS_KEY"] = env["R2_SECRET_ACCESS_KEY"]
    env.setdefault("AWS_DEFAULT_REGION", "auto")
    env["R2_ENDPOINT"] = f"https://{env['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com"
    return env


def check(vk):
    """Every block the manifest of version folder vk names (its file's hash = its name) and the manifest verified with
    earthfast's compiled keys (what players accept). Returns (blocks, bytes, [relative block paths])."""
    import decisions
    import fast
    root = os.path.join(OUT, dfmt(), vk)
    with open(os.path.join(root, "manifest.bngm"), "rb") as f:
        man = json.loads(fast._ef.dec_open_manifest(f.read(), None))
    if man.get("vkey") != vk or man.get("format") != decisions.FORMAT:
        raise SystemExit(f"manifest of {man.get('vkey')} (format {man.get('format')}) in folder {vk}")
    vkb = hashlib.sha256(decisions.canonical(man["doc"])).digest()
    if vkb.hex()[:32] != vk:
        raise SystemExit(f"{vk}: the manifest's doc hashes to {vkb.hex()[:32]}")
    n = size = 0
    rels = []
    for bx, by, h in man["blocks"]:
        rel = f"{decisions.BLOCK_Z}/{bx}/{by}.{h}.bngd"
        with open(os.path.join(root, rel), "rb") as f:
            b = f.read()
        if hashlib.sha256(b).hexdigest()[:decisions.HASH_HEX] != h:
            raise SystemExit(f"{rel}: not the file its name holds")
        fast._ef.dec_verify_block(b, decisions.BLOCK_Z, bx, by, vkb, None)
        n += 1
        size += len(b)
        rels.append(rel)
    return n, size, rels


def aws_base(env):
    return [AWS, "s3", "--endpoint-url", env["R2_ENDPOINT"], "--only-show-errors"]


def upload(a, vks=None):
    env = creds()
    if env is None:
        print("R2 credentials missing (run through the unit: EnvironmentFile=%h/bnge-builder/r2.env)", flush=True)
        return 2
    bucket = env.get("R2_BUCKET") or "bnge-tiles"
    vks = vks or ([a.vkey] if getattr(a, "vkey", None) else versions()[-1:])
    if not vks:
        print("nothing built", flush=True)
        return 1
    for vk in vks:
        n, size, rels = check(vk)
        root = os.path.join(OUT, dfmt(), vk)
        dst = f"s3://{bucket}/dec/{dfmt()}/{vk}"
        t0 = time.time()
        # (the blocks by name: a name is its content, so sync uploads only the new ones; retired ones are left
        # there until prune-out)
        r = subprocess.run(aws_base(env)[:2] + ["sync"] + aws_base(env)[2:]
                           + [os.path.join(root, "12"), f"{dst}/12", "--exclude", "*", "--include", "*.bngd",
                              "--content-type", "application/octet-stream", "--cache-control", BLOCK_CACHE],
                           env=env)
        if r.returncode != 0:
            print(f"{vk}: block upload failed (exit {r.returncode}); manifest not uploaded", flush=True)
            return 1
        r = subprocess.run(aws_base(env)[:2] + ["cp"] + aws_base(env)[2:]
                           + [os.path.join(root, "manifest.bngm"), f"{dst}/manifest.bngm",
                              "--content-type", "application/octet-stream", "--cache-control", MANIFEST_CACHE],
                           env=env)
        if r.returncode != 0:
            print(f"{vk}: manifest upload failed (exit {r.returncode})", flush=True)
            return 1
        print(f"uploaded {vk}: manifest of {n} blocks ({size / 1e6:.2f} MB) to {dst}/ in {time.time() - t0:.1f} s "
              f"(https://tiles.tgforever.net/dec/{dfmt()}/{vk}/manifest.bngm)", flush=True)
    return 0


def status(_a):
    print(f"commit {commit()[:12]}; free {free_gb(HERE):.1f} GB; cache {du_gb(CACHE):.2f} GB; R2 credentials "
          f"{'present' if creds() else 'not in this environment (the unit loads them)'}")
    for vk in versions():
        root = os.path.join(OUT, dfmt(), vk)
        try:
            with open(os.path.join(root, "current.json")) as f:
                cur = json.load(f)
            with open(os.path.join(root, "retired.json")) as f:
                ret = json.load(f)
        except (OSError, ValueError):
            cur, ret = {}, {}
        print(f"  {vk}: {du_gb(root) * 1e3:.1f} MB, {len(cur)} blocks, {len(ret)} retired files")
    return 0


def prune_out(a):
    """Other version folders removed here; this version's retired block files older than the grace period removed
    here and (with credentials: through the unit's EnvironmentFile) from R2; other versions' R2 folders are left
    (old clients still read them; small)."""
    for vk in versions():
        if vk != a.keep:
            shutil.rmtree(os.path.join(OUT, dfmt(), vk))
            print(f"removed out/{dfmt()}/{vk}")
    root = os.path.join(OUT, dfmt(), a.keep)
    path = os.path.join(root, "retired.json")
    try:
        with open(path) as f:
            ret = json.load(f)
    except (OSError, ValueError):
        return 0
    env = creds()
    bucket = (env or {}).get("R2_BUCKET") or "bnge-tiles"
    keep = {}
    for rel, t in ret.items():
        if time.time() - t < a.grace_days * 86400:
            keep[rel] = t
            continue
        if env is not None:
            r = subprocess.run(aws_base(env)[:2] + ["rm"] + aws_base(env)[2:]
                               + [f"s3://{bucket}/dec/{dfmt()}/{a.keep}/{rel}"], env=env)
            if r.returncode != 0:
                keep[rel] = t
                continue
        elif a.remote:
            print("R2 credentials missing: nothing removed from R2", flush=True)
            return 2
        try:
            os.remove(os.path.join(root, rel))
        except OSError:
            pass
        print(f"retired {rel} removed{' (here and in R2)' if env is not None else ' (here only)'}")
    with open(path + ".tmp", "w") as f:
        json.dump(keep, f, sort_keys=True)
    os.replace(path + ".tmp", path)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("build", "all"):
        p = sub.add_parser(name)
        p.add_argument("--places-file", required=True)
        p.add_argument("--procs", type=int, default=4)
        p.add_argument("--km", type=float, default=6.0)
        p.add_argument("--min-free-gb", type=float, default=20.0)
        p.add_argument("--cache-cap-gb", type=float, default=15.0)
    p = sub.add_parser("upload")
    p.add_argument("--vkey")
    sub.add_parser("status")
    p = sub.add_parser("prune-out")
    p.add_argument("--keep", required=True)
    p.add_argument("--grace-days", type=float, default=GRACE_DAYS)
    p.add_argument("--remote", action="store_true", help="fail unless R2 credentials are there (the unit)")
    a = ap.parse_args()
    if a.cmd == "build":
        return build(a)
    if a.cmd == "upload":
        return upload(a)
    if a.cmd == "status":
        return status(a)
    if a.cmd == "prune-out":
        return prune_out(a)
    before = {vk: os.path.getmtime(os.path.join(OUT, dfmt(), vk)) for vk in versions()}
    rc = build(a)
    built = [vk for vk in versions() if os.path.getmtime(os.path.join(OUT, dfmt(), vk)) != before.get(vk)]
    if not built:
        print("nothing new built: no upload", flush=True)
        return rc or 1
    return upload(a, built) or rc


if __name__ == "__main__":
    sys.exit(main())
