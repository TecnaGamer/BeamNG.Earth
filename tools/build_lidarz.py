#!/usr/bin/env python3
"""Build the lidar road heights product (server/lidarz.py): for every z14 map cell of the places whose roads the USGS
3DEP point clouds measure, exactly what a player's server would make of the lidar (roadz.RoadZ._lidar, the server's
own code and map data, as a player's road processes have them), packed per z12 block and signed with the decision
archives' key. Run by the project's own machine (the mini PC, hosting/decisions-builder/run.py --product lidarz);
never from anything a player uploaded.

  python3 tools/build_lidarz.py build --places "manhattan" --cache ~/scratch/lz-cache --out ~/scratch/lz \\
      --key ~/scratch/test.key --procs 2
  python3 tools/build_lidarz.py places --places "@top:200"      # the place list it would build (no work)
  python3 tools/build_lidarz.py check --out ~/scratch/lz [--pubkey HEX]   # every block and the manifest verified

Places: as tools/build_decisions.py --places (a name, "lat,lon[,km]", @docs, @top:N, @asked:FILE), each a square of
--km of z14 cells; only cells in the 3DEP's reach (roadz.sources_at) with a bridge to measure are built (the others
need no read: a player's server makes them from the map alone).

Output: <out>/lz<FORMAT>/<lkey>/manifest.bnlm and 12/<x>/<y>.<h>.bnlz (h: the file's hash; a rebuilt block is a new
file, the old one in retired.json): upload the blocks first, the manifest last. A block built before is merged (its
records kept unless made again: by cell) and signed again. Each record's key holds the digest of the inputs it was
measured along; a cell whose inputs, and DEM heights where its measuring read the DEM, are those of a record already
made for this lkey (<cache>/lidarz_work/) is not read again, so building after a new map release reads only the
changed cells.

Its environment is a player's (tools/build_decisions.player_env: the build switches reset, the lidar given time) and
the product is never read here (BNGE_LIDARZ=off).
"""
import argparse
import json
import multiprocessing
import os
import re
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(ROOT, "server")
sys.path.insert(0, SERVER)
sys.path.insert(0, HERE)
import npyguard  # noqa: E402,F401  (first: numpy's AVX-512 kernels held back before anything imports numpy)

import build_decisions as bd  # noqa: E402

DEFAULT_KM = bd.DEFAULT_KM
TOOL = "build_lidarz 1"


def player_env():
    bd.player_env()
    os.environ["BNGE_LIDARZ"] = "off"           # (the builder reads the lidar itself: never its own product)


def work_dir(cache, vk):
    return os.path.join(cache, "lidarz_work", vk)


def record_path(cache, vk, cell, ikey):
    z, x, y = cell
    return os.path.join(work_dir(cache, vk), f"{z}_{x}_{y}.{ikey.split('@', 1)[1]}.bin")


def _worker(args):
    """Each cell of cells measured (or its record taken from an earlier build when its inputs and DEM are the same):
    {"cells": [{"cell", "key", "path", "made", ...}], "requests", "mb", "cpu_s"}."""
    cache, cells, wid = args
    import logging
    import blockworker as bw
    import decisions
    import earthd
    import lidarz
    import mapcells
    import roadz
    earthd.raise_open_files()
    bw.init(cache, False, True, True, True, True, None, logging.WARNING, True, True, 0, None, True)
    md = bw._s.md
    if bd.wait_weekly(md) is None:          # (the roads' lane tags: a player's server has them)
        return {"error": "the weekly datasets' manifests could not be had", "cells": []}
    rz = roadz.PROVIDER
    if rz is None or not rz.enabled or not rz.lidar:
        return {"error": "roadz or its lidar is off", "cells": []}
    roadz._tl.prefetch = True               # (this thread's reads never stop: no rule decision here)
    vk = lidarz.lkey()[0]
    out, t0 = [], time.time()
    for done, (z, x, y) in enumerate(cells, 1):
        key = mapcells.CellKey(z, x, y)
        row = {"cell": [z, x, y]}
        try:
            parts, cands, names = rz.lidar_inputs(key)
            if not parts or not names:
                continue                    # (nothing a player's server would read)
            ikey = lidarz.item_key((z, x, y), lidarz.inputs_digest(key, parts, cands, names))
            path = record_path(cache, vk, (z, x, y), ikey)
            row.update(key=ikey, path=path, decks=len(parts), candidates=len(cands))
            old = None
            if os.path.exists(path):
                with open(path, "rb") as f:
                    old = f.read()
            if old is not None and lidarz.record_ok(rz, old):
                row["made"] = False         # (made before from the same inputs, and the DEM where it read it the same)
            else:
                r0, b0 = rz.stats["requests"], rz.stats["bytes"]
                got, rec, ikey2 = lidarz.record_for(rz, key)
                if ikey2 != ikey:
                    raise RuntimeError("the cell's inputs changed during its build")
                if not same_lines(got, lidarz.decode(rec)[0]):
                    raise RuntimeError("the record does not give the lines back bit for bit")
                decisions._write(path, rec)
                row.update(made=True, requests=rz.stats["requests"] - r0, mb=round((rz.stats["bytes"] - b0) / 1e6, 2))
                # (the cell's partial node reads: a cell built whole needs them no more, as RoadZ.lines removes them)
                shutil.rmtree(os.path.join(rz.dir, "eptparts", str(key).replace("/", "_")), ignore_errors=True)
            with open(path, "rb") as f:
                row.update(lidarz.record_info(f.read()))
        except Exception as ex:             # (left out: players read that cell's lidar themselves)
            row["error"] = f"{type(ex).__name__}: {ex}"
            print(f"worker {wid}: cell {key}: {row['error']}", flush=True)
        out.append(row)
        if done % 10 == 0:
            print(f"worker {wid}: {done}/{len(cells)} cells, {time.time() - t0:.0f} s", flush=True)
    return {"cells": out, "requests": rz.stats["requests"], "mb": round(rz.stats["bytes"] / 1e6, 1),
            "cpu_s": time.process_time(), "lkey": vk}


def same_lines(a, b):
    import lidarz
    return lidarz.same_lines(a, b)


def us_cells(s, w, n, e):
    """The z14 cells of a box the 3DEP lidar reaches (roadz.sources_at at each cell's centre)."""
    import mapcells
    import roadz
    keys = mapcells.cells_for_bbox(s, w, n, e, max_cells=200000)
    return sorted((c.z, c.x, c.y) for c in keys if "lidar" in roadz.sources_at(*c.center(), lidar=True))


def build_place(a, name, box, seed):
    import decisions
    import fast
    import lidarz
    cells = us_cells(*box)
    if not cells:
        print(f"{name}: no cell in the 3DEP's reach", flush=True)
        return None
    t0, c0 = time.time(), os.times()
    procs = max(1, min(a.procs, len(cells)))
    jobs = [(a.cache, cells[i::procs], i) for i in range(procs)]
    with multiprocessing.get_context("spawn").Pool(procs) as pool:
        res = pool.map(_worker, jobs)
    errs = sorted({r["error"] for r in res if r.get("error")})
    keys = {r.get("lkey") for r in res if r.get("lkey")}
    vk, vkb = lidarz.lkey()
    if errs or keys - {vk}:
        print(f"{name}: workers failed or made another lkey ({errs}, {keys}): not packed", flush=True)
        return None
    rows = [c for r in res for c in r["cells"]]
    by_block = {}
    for c in rows:
        if c.get("error") or not c.get("path"):
            continue
        with open(c["path"], "rb") as f:
            rec = f.read()
        by_block.setdefault(decisions.block_of(c["cell"]), {})[c["key"]] = (rec, False)
    sizes = bd.pack(by_block, a.out, vk, vkb, lidarz.lkey_doc(), seed,
                    {"native": int(fast._ef.VERSION), "tool": TOOL}, store=lidarz.Store, npz=lambda k: False)
    c1 = os.times()
    ok = [c for c in rows if not c.get("error")]
    st = {"place": name, "bbox": list(box), "cells": len(cells), "lkey": vk,
          "records": len(ok), "made": sum(1 for c in ok if c.get("made")), "failed": len(rows) - len(ok),
          "record_bytes": sum(c.get("bytes", 0) for c in ok),
          "ept_requests": sum(r.get("requests", 0) for r in res), "ept_mb": round(sum(r.get("mb", 0) for r in res), 1),
          "wall_s": round(time.time() - t0, 1),
          "cpu_s": round(sum(r.get("cpu_s", 0) for r in res) + (c1.user - c0.user) + (c1.system - c0.system), 1),
          "blocks": {f"{bx}/{by}": sz for (bx, by), sz in sizes.items()},
          "per_cell": {"/".join(map(str, c["cell"])): {k: c.get(k) for k in ("bytes", "lines", "vertices", "gaps",
                                                                            "made", "requests", "mb")}
                       for c in ok}}
    print(json.dumps({k: v for k, v in st.items() if k != "per_cell"}), flush=True)
    return st


def check(out, pubkeys=None):
    """Every lkey folder of out: the manifest and each block it names verified (signature, name hash, block, lkey),
    each record decoded. Returns {lkey: (blocks, bytes, records)}; SystemExit on any failure."""
    import hashlib
    import decisions
    import fast
    import lidarz
    root = os.path.join(out, f"lz{lidarz.FORMAT}")
    got = {}
    for vk in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        d = os.path.join(root, vk)
        with open(os.path.join(d, lidarz.Store.MANIFEST), "rb") as f:
            man = json.loads(fast._ef.dec_open_manifest(f.read(), pubkeys, domain="lidarz"))
        vkb = hashlib.sha256(decisions.canonical(man["doc"])).digest()
        if man.get("vkey") != vk or vkb.hex()[:32] != vk or man.get("format") != lidarz.FORMAT:
            raise SystemExit(f"{vk}: the manifest is another version's")
        n = size = recs = 0
        for bx, by, h in man["blocks"]:
            rel = f"{decisions.BLOCK_Z}/{bx}/{by}.{h}.{lidarz.Store.EXT}"
            with open(os.path.join(d, rel), "rb") as f:
                b = f.read()
            if hashlib.sha256(b).hexdigest()[:decisions.HASH_HEX] != h:
                raise SystemExit(f"{rel}: not the file its name holds")
            fast._ef.dec_verify_block(b, decisions.BLOCK_Z, bx, by, vkb, pubkeys, domain="lidarz")
            for k in fast._ef.dec_block_info(b, domain="lidarz")[4]:
                lidarz.decode(fast._ef.dec_block_item(b, k, domain="lidarz"))
                recs += 1
            n += 1
            size += len(b)
        got[vk] = (n, size, recs)
    return got


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--places", required=True)
    b.add_argument("--km", type=float, default=DEFAULT_KM, help="side of each place's square (km)")
    b.add_argument("--cache", required=True, help="the builder's own cache folder")
    b.add_argument("--out", required=True)
    b.add_argument("--key", required=True, help="the signing key's 32-byte seed (hex) file")
    b.add_argument("--procs", type=int, default=2)
    b.add_argument("--stats", help="write per-place timings / sizes (JSON) here")
    p = sub.add_parser("places")
    p.add_argument("--places", default="@top:200")
    p.add_argument("--km", type=float, default=DEFAULT_KM)
    c = sub.add_parser("check")
    c.add_argument("--out", required=True)
    c.add_argument("--pubkey", help="hex public key to accept instead of the compiled ones")
    a = ap.parse_args()
    if a.cmd == "check":
        got = check(a.out, [bytes.fromhex(a.pubkey)] if a.pubkey else None)
        print(json.dumps({vk: {"blocks": n, "bytes": s, "records": r} for vk, (n, s, r) in got.items()}))
        return
    places = bd.resolve(a.places, a.km)
    if a.cmd == "places":
        cells = set()
        for _name, s, w, n, e in places:
            cells |= set(us_cells(s, w, n, e))
        print(json.dumps({"places": len(places), "lidar_cells": len(cells),
                          "z12_blocks": len({(x >> 2, y >> 2) for _z, x, y in cells})}))
        return
    player_env()
    import fast
    import lidarz
    fast.require(lidarz.NATIVE, "lidar road heights")
    seed = bd.read_seed(a.key)
    os.makedirs(a.cache, exist_ok=True)
    import cdn
    import netfetch
    if "BNGE_DEM_CDN" not in os.environ:    # (the DEM packs' release from the switch file, as a player's server)
        os.environ["BNGE_DEM_CDN"] = cdn.dem_release(netfetch.Fetcher(), a.cache) or "off"
    stats = []
    for name, s, w, n, e in places:
        st = build_place(a, re.sub(r"\s+", " ", name), (s, w, n, e), seed)
        if st is not None:
            stats.append(st)
    if a.stats:
        with open(a.stats, "w") as f:
            json.dump(stats, f, indent=1)


if __name__ == "__main__":
    main()
