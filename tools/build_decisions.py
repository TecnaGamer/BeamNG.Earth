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

Output: <out>/d<FORMAT>/<vkey>/manifest.bngm and 12/<x>/<y>.<h>.bngd (h: the file's hash: a rebuilt block is a new
file, the old one listed in retired.json): upload the new files first, the manifest last. A block built before is
merged (its items kept unless made again) and signed again; the manifest lists each block's current file
(current.json). The weekly datasets' builds are not in the vkey: each item's key holds its ring's digest of them
(decisions.slice_digest; the ring's way ids are in the block), so a weekly build only misses the cells it changed;
after a weekly build, building the places again replaces exactly the items whose digests changed.

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
WEEKLY_KEYS = ("rails", "lanes", "stations")      # (decisions.WEEKLY: kept in each record's versions, not the vkey)
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
    md = bw._s.md
    # (the weekly datasets' builds known before anything is planned or keyed: a cold cache's first workers planned
    # without the lane / rail tags, their plans keyed without them, 2026-11-13)
    start = wait_weekly(md)
    if start is None:
        rec.close()
        return {"error": "the weekly datasets' manifests could not be had", "versions": None, "vkey": None,
                "doc": None, "cpu_s": time.process_time()}
    g, _ = bw._s.anchor(1, lat0, lon0)
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
    out = {"versions": json.loads(json.dumps(v, sort_keys=True)), "vkey": vv[0] if vv else None,
           "doc": decisions.vkey_doc(v), "cpu_s": time.process_time()}
    if weekly_of(v) != start:
        out["error"] = f"a weekly dataset's build changed during the run ({start} -> {weekly_of(v)})"
    return out


WEEKLY_WAIT_S = 180.0


def weekly_of(versions):
    return {k: versions.get(k) for k in WEEKLY_KEYS} if isinstance(versions, dict) else None


def wait_weekly(md, wait_s=None, sleep=time.sleep):
    """The weekly datasets' builds ({rails, lanes, stations}) once every one is known (each manifest asked, again
    every 10 s for up to wait_s), or None. Planning without them would make plans without their tags."""
    srcs = [getattr(md, a, None) for a in ("rail_tags", "lane_tags", "station_tags")]
    t0 = time.monotonic()
    wait_s = WEEKLY_WAIT_S if wait_s is None else wait_s
    while True:
        for src in srcs:
            if src is not None:
                src.version()
        got = weekly_of(md.versions())
        if all(got.get(k, 0) is not None for k in WEEKLY_KEYS):
            return got
        if time.monotonic() - t0 >= wait_s:
            return None
        sleep(10.0)


def _ways_worker(args):
    """The way ids and weekly slice digest of each cell of cells (decisions.ids_of / slice_digest), as records
    {"w": [z, x, y], "ids", "s", "weekly"} in out_jsonl."""
    cache, cells, out_jsonl, wid = args
    import logging
    import blockworker as bw
    import decisions
    import earthd
    import mapcells
    earthd.raise_open_files()
    bw.init(cache, False, True, True, True, True, None, logging.WARNING, True, True, 0, None, True)
    md = bw._s.md
    start = wait_weekly(md)
    if start is None:
        return {"error": "the weekly datasets' manifests could not be had"}
    sources = {"rails": getattr(md, "rail_tags", None), "lanes": getattr(md, "lane_tags", None)}
    with open(out_jsonl, "w") as rec:
        for z, x, y in cells:
            try:
                f = md.cell(mapcells.CellKey(z, x, y), layers=("roads",), kind="demand").layers.get("roads")
                ids = decisions.ids_of(f)
                d = decisions.slice_digest(sources, (z, x, y), ids)
            except Exception as ex:          # (no slice: the items whose rings hold it are left out)
                print(f"ways worker {wid}: cell {z}/{x}/{y}: {type(ex).__name__}: {ex}", flush=True)
                continue
            rec.write(json.dumps({"w": [z, x, y], "ids": ids, "s": d, "weekly": start}) + "\n")
    return {"weekly": start if weekly_of(md.versions()) == start else None}


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
            got = pool.map(_worker, pj)
        results += got
        jobs += pj
        final = final_versions(got)
        have = complete_cells(_records(jobs), final)
        todo = [c for c in cells if (c[0], c[1], c[2]) not in have]
        print(f"{label} pass {pass_ + 1}: {len(cells) - len(todo)}/{len(cells)} cells' plans complete, "
              f"{time.time() - t0:.0f} s", flush=True)
        if not todo:
            break
    return list(_records(jobs)), results, final


def ways_area(cache, cells, procs, work, label):
    """{(z, x, y): (way ids, slice digest or None)} of cells, and the weekly builds they were made with (None when
    they changed meanwhile or could not be had)."""
    os.makedirs(work, exist_ok=True)
    cells = sorted(cells)
    jobs = [(cache, cells[i::procs], os.path.join(work, f"{label}_ways{i}.jsonl"), i) for i in range(procs)]
    jobs = [j for j in jobs if j[1]]
    with multiprocessing.get_context("spawn").Pool(len(jobs)) as pool:
        res = pool.map(_ways_worker, jobs)
    weekly = {json.dumps(r.get("weekly"), sort_keys=True) for r in res}
    out = {}
    for j in jobs:
        if os.path.exists(j[2]):
            with open(j[2]) as f:
                for ln in f:
                    r = json.loads(ln)
                    out[tuple(r["w"])] = (r["ids"], r["s"])
    return out, (json.loads(weekly.pop()) if len(weekly) == 1 else None)


def final_versions(results):
    """The map data's versions every worker of a pass ended with (weekly builds included), or None (they differ, or
    a worker failed)."""
    vs = {json.dumps(r.get("versions"), sort_keys=True) for r in results}
    if len(vs) != 1 or any(r.get("error") for r in results):
        return None
    return json.loads(vs.pop())


def _same(versions, final):
    if versions is None or final is None:
        return False
    if isinstance(versions, str):
        versions = json.loads(versions)
    return json.dumps(versions, sort_keys=True) == json.dumps(final, sort_keys=True)


def complete_cells(records, final):
    """The cells whose plans records were made with the final versions: only those are packed (pack), so only those
    count as done; a record keyed with other versions (a cold cache's first plans, before the weekly manifests came)
    leaves its cell to be planned again."""
    return {tuple(r["plans"]) for r in records if "plans" in r and _same(r.get("versions"), final)}


def _records(jobs):
    for j in jobs:
        if os.path.exists(j[2]):
            with open(j[2]) as f:
                for ln in f:
                    yield json.loads(ln)


# --- packing -------------------------------------------------------------------------------------------------------
def block_items(records, ways, final, place):
    """{(bx, by): {item key: (bytes, npz)}} of the records made with the final versions for cells of place (a set of
    (z, x, y)), each key with its ring's digest (decisions.dep_digest of the ways' slice digests), and each block's
    ring cells' way ids (w/...). An item whose ring has a cell without a slice is left out (players plan it)."""
    import decisions
    by_block = {}

    def dep(cell, r):
        sl = []
        for c in decisions.ring(cell, r):
            w = ways.get(c)
            if w is None or w[1] is None:
                return None, None
            sl.append((c, w[1]))
        return decisions.dep_digest(sl), [c for c, _ in sl]

    def add(cell, key, data, is_npz, r):
        d, ring_cells = dep(cell, r)
        if d is None:
            return
        items = by_block.setdefault(decisions.block_of(cell), {})
        items[f"{key}@{d}"] = (data, is_npz)
        for c in ring_cells:
            items[decisions.ways_key(c)] = (json.dumps(ways[c][0], separators=(",", ":")).encode("utf-8"), False)
    for r in records:
        if "plans" in r:
            cell = tuple(r["plans"])
            if cell not in place or not _same(r.get("versions"), final):
                continue
            try:
                with open(r["path"], "rb") as f:
                    data = f.read()
            except OSError:
                continue
            add(cell, decisions.plans_key(r["sig"], cell), data, True, decisions.RING_PLANS)
        elif "facts" in r:
            cell = tuple(int(v) for v in r["cell"].split("/"))
            if cell not in place or not _same(r["versions"], final):
                continue
            add(cell, decisions.facts_key(r["layer"], r["facts"], r["extra"], r["cell"]), r["data"].encode("utf-8"),
                False, decisions.RING_FACTS)
    return by_block


def _index(root):
    try:
        with open(os.path.join(root, "current.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def pack(by_block, out, vkey_hex, vkey_bytes, doc, seed, built_by):
    """by_block's items into their blocks under <out>/d<FORMAT>/<vkey>/12/<x>/<y>.<h>.bngd (h: the file's own sha256,
    16 hex; merged with the block's current file: its items kept unless made again, by base key), signed; the
    current names in current.json, the replaced ones in retired.json (with when: run.py prune-out removes them after
    a grace period, so a client holding the old manifest still finds them); the manifest rewritten. Returns
    {(bx, by): bytes}."""
    import decisions
    import fast
    root = os.path.join(out, f"d{decisions.FORMAT}", vkey_hex)
    os.makedirs(root, exist_ok=True)
    cur = _index(root)
    try:
        with open(os.path.join(root, "retired.json")) as f:
            retired = json.load(f)
    except (OSError, ValueError):
        retired = {}
    pub = fast._ef.dec_public_key(seed)
    sizes = {}
    for (bx, by), items in sorted(by_block.items()):
        name = f"{bx}/{by}"
        old_h = cur.get(name)
        if old_h:                       # (a block built before: its items kept unless made again now)
            with open(os.path.join(root, str(decisions.BLOCK_Z), str(bx), f"{by}.{old_h}.bngd"), "rb") as f:
                old = f.read()
            fast._ef.dec_verify_block(old, decisions.BLOCK_Z, bx, by, vkey_bytes, [pub])
            made = {decisions.base_key(k) for k in items}
            for k in fast._ef.dec_block_info(old)[4]:
                if decisions.base_key(k) not in made:
                    items[k] = (fast._ef.dec_block_item(old, k), k.startswith("p/"))
        body = fast._ef.dec_pack_block(decisions.BLOCK_Z, bx, by, vkey_bytes,
                                       [(k, v[0], v[1]) for k, v in sorted(items.items())], seed)
        h = hashlib.sha256(body).hexdigest()[:decisions.HASH_HEX]
        decisions._write(os.path.join(root, str(decisions.BLOCK_Z), str(bx), f"{by}.{h}.bngd"), body)
        if old_h and old_h != h:
            retired[f"{decisions.BLOCK_Z}/{bx}/{by}.{old_h}.bngd"] = time.time()
        cur[name] = h
        sizes[(bx, by)] = len(body)
    decisions._write(os.path.join(root, "retired.json"), json.dumps(retired, sort_keys=True).encode())
    decisions._write(os.path.join(root, "current.json"), json.dumps(cur, sort_keys=True).encode())
    blocks = sorted([int(k.split("/")[0]), int(k.split("/")[1]), h] for k, h in cur.items())
    man = {"format": decisions.FORMAT, "vkey": vkey_hex, "doc": doc, "blocks": blocks,
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
        recs, results, final = plan_area(a.cache, cells, min(a.procs, len(cells)), work, label)
        errs = sorted({r["error"] for r in results if r.get("error")})
        if final is None or errs:
            print(f"{name}: the last pass's workers did not agree on the map data's versions or failed ({errs}): "
                  "not packed", flush=True)
            continue
        vv = decisions.vkey(final)
        vk, doc = vv[0], decisions.vkey_doc(final)
        vkb = vv[1]
        place = {(c[0], c[1], c[2]) for c in cells}
        need = set()
        for c in place:
            need.update(decisions.ring(c, decisions.RING_PLANS))
        ways, weekly = ways_area(a.cache, need, min(a.procs, len(need)), work, label)
        if weekly != weekly_of(final):
            print(f"{name}: the weekly slices were made with other builds ({weekly}) than the plans "
                  f"({weekly_of(final)}): not packed", flush=True)
            continue
        by_block = block_items(recs, ways, final, place)
        sizes = pack(by_block, a.out, vk, vkb, doc, seed, {"native": int(fast._ef.VERSION),
                                                            "tool": "build_decisions 2"})
        c1 = os.times()
        st = {"place": name, "bbox": [s, w, n, e], "cells": len(cells), "vkey": vk,
              "plans": sum(1 for r in recs if "plans" in r), "facts": sum(1 for r in recs if "facts" in r),
              "items": sum(len(v) for v in by_block.values()), "weekly": weekly,
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
