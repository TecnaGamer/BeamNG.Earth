#!/usr/bin/env python3
"""Build the shared decision archives (server/decisions.py): popular places' road plans and cell facts, made by the
server's own planning code for exactly this server version, packed per z12 block and signed. Run by the project's own
machine (the mini PC) after each release; never from anything a player uploaded.

  python3 tools/build_decisions.py build --places "fells;manhattan" --cache ~/scratch/dec-cache --out ~/scratch/dec \\
      --key ~/scratch/test.key --procs 4
  python3 tools/build_decisions.py places --top 200          # the place list it would build (no work)
  python3 tools/build_decisions.py make-test-key PATH        # a throw-away signing key (tests, local A/Bs only)

Places (no player location data needed): --places takes names of the built-in list (TEST_PLACES), "lat,lon[,km]",
"@docs" (docs/PLACES.md's table) or "@top:N" (the N most prominent cities of Natural Earth's populated places,
server/data/world_labels.json.gz, by its label zoom and rank: a stand-in for population), and "@asked:FILE" (blocks
consenting players asked for: one "x,y" z12 block a line, from the collector service). Each place is planned over a
square of --km (default: 6 km; a metro's core) of z14 cells.

Output: <out>/d<FORMAT>/<vkey>/manifest.bngm and 12/<x>/<y>.bngd: upload the vkey folder as it is (blocks first, the
manifest last). A block built before is merged (its items kept unless made again) and signed again; the manifest
lists every block of the folder.

Its environment is a player's: the build switches (BNE_*) are reset to what earthd gives every player
(PLAYER_SWITCHES), so the version key (decisions.vkey) is the one players compute. Cells with Hong Kong's measured
heights are left out (roadz.hk_cell), and the photo's road-surface verdicts are not made here (no photos).
"""
import argparse
import gzip
import hashlib
import json
import math
import multiprocessing
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(ROOT, "server")
sys.path.insert(0, SERVER)

PASSES = 3
# what earthd puts in every player's environment (its --bridge-profile default; BNE_NETSTATS_PROC is not a switch)
PLAYER_SWITCHES = {"BNE_BRIDGE_PROFILE": "curve"}
TEST_PLACES = {"fells": (39.28, -76.60), "manhattan": (40.76, -73.98), "stelvio": (46.53, 10.45),
               "i10": (33.45, -112.72)}
DEFAULT_KM = 6.0


def player_env():
    """os.environ as a default player's server has it (the build switches), the lidar given time."""
    for k in [k for k in os.environ if k.startswith("BNE_")]:
        if k not in PLAYER_SWITCHES:
            print(f"(build switch {k} dropped: players do not have it)", file=sys.stderr)
            del os.environ[k]
    os.environ.update(PLAYER_SWITCHES)
    os.environ["BNE_LIDAR_GAPS_WAIT"] = "120"   # (not a switch: revisit.NOT_SWITCHES)
    # (every product made again, none read from the builder's own revisit cache: all of them pass the recording
    # hooks; not a switch either)
    os.environ["BNE_REVISIT_SKIP"] = "plans,cellfacts"


# --- places --------------------------------------------------------------------------------------------------------
def top_cities(n):
    with gzip.open(os.path.join(SERVER, "data", "world_labels.json.gz")) as f:
        rows = json.load(f)["rows"]
    cities = [r for r in rows if r[0] == "city"]
    cities.sort(key=lambda r: (r[4], r[6]))     # (label zoom: the most prominent first; then Natural Earth's rank)
    return [(r[1], r[2], r[3]) for r in cities[:n]]


def docs_places():
    out = []
    with open(os.path.join(ROOT, "docs", "PLACES.md"), encoding="utf-8") as f:
        for ln in f:
            m = re.match(r"\|\s*([^|]+?)\s*\|\s*(-?\d+\.\d+),\s*(-?\d+\.\d+)\s*\|", ln)
            if m:
                out.append((m[1], float(m[2]), float(m[3])))
    return out


def _square(name, lat, lon, km):
    half = km * 500.0
    dlat, dlon = half / 110540.0, half / (111320.0 * math.cos(math.radians(lat)))
    return name, lat - dlat, lon - dlon, lat + dlat, lon + dlon


def resolve(spec, km):
    """[(name, s, w, n, e)] of a --places value: items separated by ";" (a name, "lat,lon[,km]", @docs, @top:N,
    @asked:FILE)."""
    out = []
    for item in [s.strip() for s in spec.replace(";", "|").split("|") if s.strip()]:
        if item in TEST_PLACES:
            out.append(_square(item, *TEST_PLACES[item], km))
        elif item == "@docs":
            out += [_square(name, lat, lon, km) for name, lat, lon in docs_places()]
        elif item.startswith("@top:"):
            out += [_square(name, lat, lon, km) for name, lat, lon in top_cities(int(item[5:]))]
        elif item.startswith("@asked:"):
            import mapcells
            with open(item[7:]) as f:
                for ln in f:
                    if ln.strip():
                        x, y = (int(v) for v in ln.split(","))
                        w, s, e, n = mapcells.CellKey(12, x, y).bounds()
                        out.append((f"asked {x},{y}", s, w, n, e))
        elif re.match(r"^-?\d", item):
            v = [float(t) for t in item.split(",")]
            out.append(_square(f"{v[0]},{v[1]}", v[0], v[1], v[2] if len(v) > 2 else km))
        else:
            raise SystemExit(f"unknown place {item!r} (names: {', '.join(TEST_PLACES)}; lat,lon[,km]; @docs, @top:N, "
                             "@asked:FILE; separated by ';')")
    return out


# --- planning (worker processes) -----------------------------------------------------------------------------------
def _worker(args):
    cache, cells, out_jsonl, wid = args
    import logging
    import numpy as np
    import blockworker as bw
    import decisions
    import earthd
    import mapcells
    import plancache
    import roads
    import roadz
    earthd.raise_open_files()
    # (a terrain block process's settings: roads, grading, measured widths, surface, airports, sea, gas stations)
    bw.init(cache, False, True, True, True, True, None, logging.WARNING, True, True, 0, None, True)
    rec = open(out_jsonl, "a")
    orig_store = plancache.PlanCache.store
    orig_facts = roads._facts_store

    def store(self, key, plans, keeps, covers, opens, decks=None, persist=False):
        ok = orig_store(self, key, plans, keeps, covers, opens, decks, persist=persist)
        if ok and persist and not roadz.hk_cell(key):
            rec.write(json.dumps({"plans": [int(k) for k in key], "sig": self.signature, "path": self._path(key),
                                  "versions": self.persist[1][2] if self.persist else None}, sort_keys=True) + "\n")
            rec.flush()
        return ok

    def facts(feats, kind, extra, data):
        orig_facts(feats, kind, extra, data)
        cid = feats.__dict__.get("_bnge_cell")
        if cid is None or roads._facts_key(feats, kind, extra) is None:
            return
        cell = tuple(int(v) for v in cid[0].split("/"))
        if cell[0] != decisions.CELL_Z or roadz.hk_cell(cell):
            return
        rec.write(json.dumps({"facts": kind, "layer": cid[1], "extra": extra, "cell": cid[0], "versions": cid[2],
                              "data": json.dumps(data)}) + "\n")
        rec.flush()
    plancache.PlanCache.store = store
    roads._facts_store = facts
    lat0, lon0 = cells[0][3], cells[0][4]
    g, _ = bw._s.anchor(1, lat0, lon0)
    md = bw._s.md
    for src in (getattr(md, "rail_tags", None), getattr(md, "lane_tags", None), getattr(md, "station_tags", None)):
        if src is not None:         # (the weekly datasets' builds known before anything is keyed: earthd's
            src.version()           # warm_manifests, here at once)
    t0 = time.time()
    for done, (z, x, y, _la, _lo) in enumerate(cells, 1):
        k = mapcells.CellKey(z, x, y)
        try:
            w, s, e, n = k.bounds()
            lat = np.linspace(s, n, 3)
            lon = np.linspace(w, e, 3)
            LA, LO = np.meshgrid(lat, lon)
            g.prepare(LA.ravel(), LO.ravel())       # (the cell's plans and every neighbour's facts they read)
            f = md.cell(k, layers=("roads",), kind="demand").layers.get("roads")
            if f is not None and len(f):
                for i in range(len(f)):
                    roads.level_ranges(f, i)
                for lh in (False, True):
                    roads.road_specs(f, None, bw._s.widths, lh)
                    roads.road_specs(f, None, None, lh)
        except Exception as ex:          # (a cell short of something: left out, players plan it themselves)
            print(f"worker {wid}: cell {k}: {type(ex).__name__}: {ex}", flush=True)
        if done % 5 == 0:
            print(f"worker {wid}: {done}/{len(cells)} cells, {time.time() - t0:.0f} s", flush=True)
    rec.close()
    v = md.versions()
    vv = decisions.vkey(v)
    return {"versions": json.loads(json.dumps(v, sort_keys=True)), "vkey": vv[0] if vv else None,
            "doc": decisions.vkey_doc(v), "cpu_s": time.process_time()}


def plan_area(cache, cells, procs, work, label):
    """Plan cells in procs processes (PASSES passes: what was short of something is planned again). Returns
    ([records], [worker results])."""
    import plancache
    os.makedirs(work, exist_ok=True)
    todo, results, jobs = cells, [], []
    t0 = time.time()
    for pass_ in range(PASSES):
        plancache.start_run(cache)          # (a new run: the last run's short plans are not read again)
        pj = [(cache, todo[i::procs], os.path.join(work, f"{label}_p{pass_}w{i}.jsonl"), i) for i in range(procs)]
        pj = [j for j in pj if j[1]]
        for j in pj:
            if os.path.exists(j[2]):
                os.remove(j[2])
        with multiprocessing.get_context("spawn").Pool(len(pj)) as pool:
            results += pool.map(_worker, pj)
        jobs += pj
        have = {tuple(r["plans"]) for r in _records(jobs) if "plans" in r}
        todo = [c for c in cells if (c[0], c[1], c[2]) not in have]
        print(f"{label} pass {pass_ + 1}: {len(cells) - len(todo)}/{len(cells)} cells' plans complete, "
              f"{time.time() - t0:.0f} s", flush=True)
        if not todo:
            break
    return list(_records(jobs)), results


def _records(jobs):
    for j in jobs:
        if os.path.exists(j[2]):
            with open(j[2]) as f:
                for ln in f:
                    yield json.loads(ln)


# --- packing -------------------------------------------------------------------------------------------------------
def pack(records, out, vkey_hex, vkey_bytes, doc, seed, built_by):
    """The records' items into their blocks under <out>/d<FORMAT>/<vkey>/ (merged with blocks there), signed; the
    manifest rewritten. Returns {(bx, by): bytes}."""
    import decisions
    import fast
    root = os.path.join(out, f"d{decisions.FORMAT}", vkey_hex)
    by_block = {}
    vjson = json.dumps(doc["versions"], sort_keys=True)
    for r in records:
        if "plans" in r:
            if r.get("versions") is not None and json.dumps(r["versions"], sort_keys=True) != vjson:
                continue
            try:
                with open(r["path"], "rb") as f:
                    data = f.read()
            except OSError:
                continue
            cell = tuple(r["plans"])
            by_block.setdefault(decisions.block_of(cell), {})[decisions.plans_key(r["sig"], cell)] = (data, True)
        elif "facts" in r:
            if json.dumps(json.loads(r["versions"]), sort_keys=True) != vjson:
                continue
            cell = tuple(int(v) for v in r["cell"].split("/"))
            key = decisions.facts_key(r["layer"], r["facts"], r["extra"], r["cell"])
            by_block.setdefault(decisions.block_of(cell), {})[key] = (r["data"].encode("utf-8"), False)
    pub = fast._ef.dec_public_key(seed)
    sizes = {}
    for (bx, by), items in sorted(by_block.items()):
        path = os.path.join(root, str(decisions.BLOCK_Z), str(bx), f"{by}.bngd")
        if os.path.exists(path):        # (a block built before: its items kept unless made again now)
            with open(path, "rb") as f:
                old = f.read()
            fast._ef.dec_verify_block(old, decisions.BLOCK_Z, bx, by, vkey_bytes, [pub])
            for k in fast._ef.dec_block_info(old)[4]:
                if k not in items:
                    items[k] = (fast._ef.dec_block_item(old, k), k.startswith("p/"))
        body = fast._ef.dec_pack_block(decisions.BLOCK_Z, bx, by, vkey_bytes,
                                       [(k, v[0], v[1]) for k, v in sorted(items.items())], seed)
        decisions._write(path, body)
        sizes[(bx, by)] = len(body)
    blocks = []
    zdir = os.path.join(root, str(decisions.BLOCK_Z))
    for x in os.listdir(zdir) if os.path.isdir(zdir) else ():
        for n in os.listdir(os.path.join(zdir, x)):
            if n.endswith(".bngd"):
                blocks.append([int(x), int(n[:-5])])
    man = {"format": decisions.FORMAT, "vkey": vkey_hex, "doc": doc, "blocks": sorted(blocks),
           "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "builder": built_by,
           "key": pub.hex()[:16]}
    decisions._write(os.path.join(root, "manifest.bngm"),
                     fast._ef.dec_make_manifest(json.dumps(man, sort_keys=True).encode("utf-8"), seed))
    return sizes


def read_seed(path):
    with open(path) as f:
        s = bytes.fromhex(f.read().strip())
    if len(s) != 32:
        raise SystemExit(f"{path}: not a 32-byte hex seed")
    return s


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--places", required=True)
    b.add_argument("--km", type=float, default=DEFAULT_KM, help="side of each place's square (km)")
    b.add_argument("--cache", required=True, help="the builder's own cache folder")
    b.add_argument("--out", required=True)
    b.add_argument("--key", required=True, help="the signing key's 32-byte seed (hex) file")
    b.add_argument("--procs", type=int, default=4)
    b.add_argument("--stats", help="write per-place timings / sizes (JSON) here")
    p = sub.add_parser("places")
    p.add_argument("--places", default="@top:200")
    p.add_argument("--km", type=float, default=DEFAULT_KM)
    k = sub.add_parser("make-test-key")
    k.add_argument("path")
    a = ap.parse_args()
    if a.cmd == "make-test-key":
        seed = os.urandom(32)
        fd = os.open(a.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(seed.hex() + "\n")
        import fast
        print(fast._ef.dec_public_key(seed).hex())
        return
    import mapcells
    places = resolve(a.places, a.km)
    if a.cmd == "places":
        cells = set()
        for name, s, w, n, e in places:
            cells |= set(mapcells.cells_for_bbox(s, w, n, e, max_cells=200000))
        km2 = len(places) * a.km * a.km
        print(json.dumps({"places": len(places), "km2": km2, "z14_cells": len(cells),
                          "z12_blocks": len({(c.x >> 2, c.y >> 2) for c in cells}),
                          "first": [p[0] for p in places[:10]]}))
        return
    player_env()
    import fast
    import decisions
    fast.require(decisions.NATIVE, "decision archives")
    seed = read_seed(a.key)
    os.makedirs(a.cache, exist_ok=True)
    # (what earthd's main process does before its workers start, where it changes plans: the airports' runways
    # graded from its copy of OurAirports; the DEM packs' release from the switch file)
    import airports
    import cdn
    import netfetch
    airports.Store(a.cache, netfetch.Fetcher()).load()
    if "BNGE_DEM_CDN" not in os.environ:
        os.environ["BNGE_DEM_CDN"] = cdn.dem_release(netfetch.Fetcher(), a.cache) or "off"
    work = os.path.join(a.cache, "dec_work")
    stats = []
    for name, s, w, n, e in places:
        keys = mapcells.cells_for_bbox(s, w, n, e, max_cells=200000)
        cells = [(c.z, c.x, c.y, *c.center()) for c in keys]
        label = re.sub(r"[^A-Za-z0-9]+", "_", name)[:40]
        t0, c0 = time.time(), os.times()
        recs, results = plan_area(a.cache, cells, min(a.procs, len(cells)), work, label)
        vks = {r["vkey"] for r in results}
        if len(vks) != 1 or None in vks:
            print(f"{name}: the workers' version keys differ or are unknown ({vks}): not packed", flush=True)
            continue
        vk = vks.pop()
        doc = results[0]["doc"]
        vkb = hashlib.sha256(decisions.canonical(doc)).digest()
        assert vkb.hex()[:32] == vk
        sizes = pack(recs, a.out, vk, vkb, doc, seed, {"native": int(fast._ef.VERSION), "tool": "build_decisions 1"})
        c1 = os.times()
        st = {"place": name, "bbox": [s, w, n, e], "cells": len(cells), "vkey": vk,
              "plans": sum(1 for r in recs if "plans" in r), "facts": sum(1 for r in recs if "facts" in r),
              "wall_s": round(time.time() - t0, 1),
              "cpu_s": round(sum(r["cpu_s"] for r in results) + (c1.user - c0.user) + (c1.system - c0.system), 1),
              "blocks": {f"{bx}/{by}": sz for (bx, by), sz in sizes.items()}}
        stats.append(st)
        print(json.dumps(st), flush=True)
    if a.stats:
        with open(a.stats, "w") as f:
            json.dump(stats, f, indent=1)


if __name__ == "__main__":
    main()
