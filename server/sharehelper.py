#!/usr/bin/env python3
"""The BeamMP server's helper (shared world generation v2, .claude/work/mp_share_v2.md; the user, 2026-11-02: "also let
the server itself help generate terrain"): our world server's planning, headless, next to a BeamMP server with the
BeamNG.Earth plugin. It reads the plugin's spool (Resources/Server/BeamNGEarth/spool: players.json, misses.json),
plans the map cells round the players (nearest first, ahead of their headings) and the ones they asked for and the
store did not have, and writes each into the plugin's store (store/<id[:2]>/<id>: a header line, then base64 chunks
in the plugin's alphabet, zlib-compressed), where the plugin serves it to every player who asks.

Only for the players whose expect is this helper's own (the same release's formats, settings and map data versions):
their items have the same keys. Workers are set up as the world server's road processes are (blockworker.init, as
tools/bake_area.py does).

  python3 server/sharehelper.py --share-dir ~/BeamMP-Earth-Server/Resources/Server/BeamNGEarth --cache ~/.cache/bnge-helper --procs 4
"""
import argparse
import json
import logging
import math
import multiprocessing
import os
import sys
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
# (what it plans goes to other players: never with Hong Kong's measured heights or terrain, roadz.hk_cell)
os.environ["BNE_ROADZ_HK"] = "off"
os.environ["BNE_HKDTM"] = "off"

log = logging.getLogger("earthd.sharehelper")

RADIUS_M = float(os.environ.get("BNGE_HELPER_RADIUS_M", 2500.0))  # the cells this near a player are planned
AHEAD_S = 30.0                  # ... and those this many seconds ahead along its heading (at an assumed speed)
AHEAD_V = 25.0                  # m/s for that (the spool has headings, not speeds)
POLL_S = 3.0
SIG = "w1s1"                    # the plans' usual signature (road widths and the sea on; the workers give the real one)

_w = {}


def _init_worker(cache):
    """A planning process: blockworker set up as the server's road processes are (as bake_area does)."""
    import blockworker as bw
    import earthd
    earthd.raise_open_files()
    bw.init(cache, False, True, True, True, True, None, logging.WARNING, True, True, 0, None, False)
    _w["bw"] = bw
    _warm_versions()


def _warm_versions():
    """The weekly rail / lane builds' versions asked for (the map data's versions() knows them only once asked: a grader
    made before would key its plans by versions with None in them, not the players')."""
    md = getattr(_w["bw"]._s, "md", None)
    for t in (getattr(md, "rail_tags", None), getattr(md, "lane_tags", None)):
        if t is not None and hasattr(t, "version"):
            try:
                t.version()
            except Exception as ex:
                log.info("helper: %s version: %s: %s", type(t).__name__, type(ex).__name__, ex)


def _expect(lat, lon):
    """(signature, map data versions) of this worker's plans (its grader's PlanCache: the players' road processes make
    theirs the same way), or None."""
    _warm_versions()
    g, _ = _w["bw"]._s.anchor(1, lat, lon)
    pc = getattr(g, "shared", None) if g is not None else None
    if pc is None or pc.persist is None:
        return None
    return pc.signature, pc.persist[1][2]        # (as it is: the revisit key is its repr, a tuple must stay one)


def _plan(args):
    """Plan cell (z, x, y) (its plans and every neighbour's they read), kept in this helper's revisit cache. -> (cell,
    seconds, ok)."""
    z, x, y = args
    import numpy as np
    import mapcells
    bw = _w["bw"]
    t0 = time.perf_counter()
    try:
        k = mapcells.CellKey(z, x, y)
        w, s, e, n = k.bounds()
        la, lo = k.center()
        g, _ = bw._s.anchor(1, la, lo)
        LA, LO = np.meshgrid(np.linspace(s, n, 3), np.linspace(w, e, 3))
        g.prepare(LA.ravel(), LO.ravel())
        return (z, x, y), time.perf_counter() - t0, True
    except Exception as ex:
        log.warning("helper: cell %s: %s: %s", (z, x, y), type(ex).__name__, ex)
        return (z, x, y), time.perf_counter() - t0, False


def b64(data):
    """The plugin's base64 ("-" for "Z": BeamMP's launcher aborts on "Zp")."""
    import base64
    return base64.b64encode(data).replace(b"Z", b"-").decode("ascii")


def store_put(share_dir, iid, key, code, data, chunk):
    """Item key (its plans npz bytes) into the plugin's store as an upload would be."""
    z = zlib.compress(data, 6)
    parts = [b64(z[i:i + chunk]) for i in range(0, max(len(z), 1), chunk)]
    head = json.dumps({"iid": iid, "key": key, "c": code, "n": len(parts), "z": 1}, separators=(",", ":"))
    d = os.path.join(share_dir, "store", iid[:2])
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, iid + ".helper.tmp")
    with open(tmp, "w", encoding="ascii") as f:
        f.write(head + "\n" + "\n".join(parts))
    os.replace(tmp, os.path.join(d, iid))


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def wanted_cells(players, misses, code, have, sig=SIG):
    """[(distance m, cell)] to plan for the players of expect code: the cells within RADIUS_M of each and AHEAD_S ahead
    of its heading, and the missed ones; nearest to any of those players first. have(cell): already there."""
    import mapcells
    mine = [p for p in players if isinstance(p, dict) and (p.get("codes") or {}).get("plans") == code
            and isinstance(p.get("lat"), (int, float)) and isinstance(p.get("lon"), (int, float))]
    if not mine:
        return []
    out = {}

    def dist(la, lo):
        return min(math.hypot((la - p["lat"]) * 111320.0,
                              (lo - p["lon"]) * 111320.0 * math.cos(math.radians(p["lat"]))) for p in mine)
    for p in mine:
        la, lo = p["lat"], p["lon"]
        centres = [(la, lo, RADIUS_M)]
        h = p.get("h")
        if isinstance(h, (int, float)):
            d = AHEAD_V * AHEAD_S
            centres.append((la + d * math.cos(math.radians(h)) / 111320.0,
                            lo + d * math.sin(math.radians(h)) / (111320.0 * max(math.cos(math.radians(la)), 0.01)),
                            RADIUS_M * 0.6))
        for cl, cg, r in centres:
            dl, dg = r / 111320.0, r / (111320.0 * max(math.cos(math.radians(cl)), 0.01))
            for k in mapcells.cells_for_bbox(cl - dl, cg - dg, cl + dl, cg + dg, max_cells=800):
                cell = (k.z, k.x, k.y)
                if cell not in out and not have(cell):
                    c = k.center()
                    out[cell] = dist(c[0], c[1])
    for m in misses or ():
        if isinstance(m, dict) and m.get("c") == code and isinstance(m.get("key"), str):
            parts = m["key"].split("/")
            if len(parts) == 3 and parts[0] == "plans" and parts[1] == sig:
                try:
                    cell = tuple(int(v) for v in parts[2].split("_"))
                except ValueError:
                    continue
                if len(cell) == 3 and cell not in out and not have(cell):
                    k = mapcells.CellKey(*cell)
                    c = k.center()
                    out[cell] = dist(c[0], c[1]) * 0.5          # (asked for: sooner)
    return sorted((d, c) for c, d in out.items())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--share-dir", required=True, help="the plugin's folder (Resources/Server/BeamNGEarth)")
    ap.add_argument("--cache", required=True, help="this helper's own cache folder (the map data, DEM, its plans)")
    ap.add_argument("--procs", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    import mpshare
    import plancache
    os.makedirs(a.cache, exist_ok=True)
    plancache.start_run(a.cache)
    # (a cell whose lidar answer is late keeps the rule and its plans are not kept: wait for it, as bake_area does; the
    # items' codes are the bake expect's, without the build switches, so the players still match)
    os.environ.setdefault("BNE_LIDAR_GAPS_WAIT", "120")
    import earthd
    earthd.raise_open_files()
    pool = multiprocessing.get_context("spawn").Pool(a.procs, initializer=_init_worker, initargs=(a.cache,))
    chunk = mpshare.CHUNK
    pending = {}                    # cell -> AsyncResult
    done = set()
    stats = {"planned": 0, "stored": 0, "failed": 0, "s": 0.0}
    log.info("helper: %d planning processes, share dir %s", a.procs, a.share_dir)
    last_log = 0.0
    while True:
        players = (_read(os.path.join(a.share_dir, "spool", "players.json")) or {}).get("players") or []
        misses = (_read(os.path.join(a.share_dir, "spool", "misses.json")) or {}).get("misses") or []
        here = next(((p["lat"], p["lon"]) for p in players if isinstance(p, dict)
                     and isinstance(p.get("lat"), (int, float)) and isinstance(p.get("lon"), (int, float))), None)
        got = pool.apply(_expect, here) if here else None
        if not got or any(v is None for v in (got[1] or {}).values() if not isinstance(v, (list, dict))):
            time.sleep(POLL_S)          # (nobody there, or the map data's weekly versions not known yet)
            continue
        sig, versions = got
        pc = plancache.PlanCache(os.path.join(a.cache, "plans", os.environ.get(plancache.RUN_ENV) or "helper"), sig)
        pc.persist = (a.cache, (plancache.FRAMES, sig, versions))
        code = mpshare.expect_code(pc._bake_expect())

        def have(cell, code=code, sig=sig):
            key = f"plans/{sig}/{cell[0]}_{cell[1]}_{cell[2]}"
            iid = mpshare.item_id(code, key)
            return cell in done or cell in pending or os.path.exists(
                os.path.join(a.share_dir, "store", iid[:2], iid))
        todo = wanted_cells(players, misses, code, have, sig)
        free = a.procs * 2 - len(pending)
        for _d, cell in todo[:max(0, free)]:
            pending[cell] = pool.apply_async(_plan, (cell,))
        for cell in [c for c, r in pending.items() if r.ready()]:
            r = pending.pop(cell)
            _cell, sec, ok = r.get()
            stats["s"] += sec
            got = pc._kept(cell) if ok else None
            if got is None:
                stats["failed"] += 1        # (short of something: planned again when asked again)
                continue
            key = f"plans/{sig}/{cell[0]}_{cell[1]}_{cell[2]}"
            store_put(a.share_dir, mpshare.item_id(code, key), key, code, got.read(), chunk)
            done.add(cell)
            stats["planned"] += 1
            stats["stored"] += 1
        now = time.monotonic()
        if now - last_log > 30:
            last_log = now
            n_mine = sum(1 for p in players if (p.get("codes") or {}).get("plans") == code)
            log.info("helper: %d players (%d with our code %s), %d cells to plan, %d planning, %d stored (%.0f CPU-s), "
                     "%d failed", len(players), n_mine, code, len(todo), len(pending), stats["stored"], stats["s"],
                     stats["failed"])
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
