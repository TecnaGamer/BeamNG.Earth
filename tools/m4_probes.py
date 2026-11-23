#!/usr/bin/env python3
"""In-game probes for v1.4 M4, the 512² physics block (docs/V1_4_SPEC.md §3.5 "Prerequisite probes",
§7): P2 (extended), P7 (a) and (b), P11. They decide whether `earthd.py --block 512` may become the
default; nothing in the game changes for good (every terrain step ends by re-importing the live
block's own heightmap at its own position, and reports the heights before / after).

Run ONLY while the user is not playing (P2 and P7 (b) pause physics for a few seconds, P11 lifts
the parked car by 0.3 m and deactivates it while the shrunk block's collision is stale), with the
game in our level, the server running with --block 1024 (the command line's default is 512 since
these probes passed) and the car parked (< 0.5 m/s) within 300 m of the terrain block's centre. The
probes need no mod update: the 512² heightmap is a crop of the live block's own 1024² one (the same
lattice, so the same heights), written with its layer map to <user-dir>/temp/beamngEarth/m4probe/.

    python3 tools/m4_probes.py check          preconditions only (reads, changes nothing)
    python3 tools/m4_probes.py p2             P2 extended (+ writes the 512² crop)
    python3 tools/m4_probes.py p11            P11
    python3 tools/m4_probes.py p7a-install    P7 (a): wrap the rebase / commit parts, then drive with
                                              rebases, e.g. a D134 run of tools/drive.py with the
                                              rebase distance at 4 km, 3 rebases
    python3 tools/m4_probes.py p7a-read       P7 (a): the breakdown per rebase and commit (repeatable)
    python3 tools/m4_probes.py p7a-uninstall
    python3 tools/m4_probes.py p7b            P7 (b): double-buffered terrain (run it last)
    python3 tools/m4_probes.py clean          delete <user-dir>/temp/beamngEarth/m4probe

Pass criteria (the decisions of §3.5):
  P2   GATE  steady-state 512² importMaps (median of 5, with the layer map) <= 30 ms; else M4 does
             not ship as is (P7 (b) and P11 decide the alternative). Also reported: the first import
             into the live 1024² block, without the layer map, reloadCollision, the 1024² baseline.
       must  after the switch the rays hit inside [c-512, c+510] on both axes and miss at c+511 (the
             2 m N/E strip), c-513 and c+562; heights where the blocks overlap equal (<= 2 mm); on
             the way back the 1024² extent [c-1024, c+1022] again; the restore leaves the heights as
             they were (<= 2 mm).
       info  setRenderEnabled(false) survives importMaps (terrain.lua re-applies it after every
             import anyway); whether collision is right in the import frame or only a frame later.
  P11  info  whether physics follows a re-import without reloadCollision (car lifted by DZ, the area
             the smaller block left stops colliding; the car is deactivated for that step and comes back
             after the restore), and after how long: "same frame" means a
             commit could drop reloadCollision (~0.4 ms for 512²); otherwise it stays (today's code).
  P7a  info  per rebase: terrain.load, farfield.transform / switchAnchor, camshift.shift,
             reloadCollision (the gap before commit_anchor), moveVehicles, their frames, the frame's
             wall ms and the remainder (frame - parts - the median frame). Rebase-frame target:
             <= 45 ms if P7 (b) passes or the remainder is <= 10 ms, else <= 60 ms (counted apart).
  P7b  PASS  the hidden second block does not collide before the swap (before or after a
             reloadCollision), after the swap (delete the live block, rename, reloadCollision) the
             rays hit the new block's heights (+DZ) across [c-511, c+509] and miss at c+700, and the
             swap costs <= 5 ms: then a commit / rebase becomes a swap and the import moves to a heavy
             slot of its own ahead of time.
"""
import argparse
import json
import math
import os
import shutil
import statistics
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import heightmap  # noqa: E402

LUA = ROOT / "probes" / "v14" / "m4_probes.lua"
PROBE_DIR = ("temp", "beamngEarth", "m4probe")
LAYER_1024 = "/levels/beamng_earth/art/terrains/beamngEarth_layer_1024.png"
GATE_IMPORT_MS = 30.0
HEIGHT_TOL = 0.002
SWAP_MAX_MS = 5.0
MAX_SPEED, MAX_OFFSET = 0.5, 300.0
DZ_P11, DZ_P7B = 0.3, 5.0
REMAINDER_MAX_MS = 10.0


class ProbeError(SystemExit):
    pass


def lua(code):
    """Run GE Lua through the game's MCP server (tools/mcp.py) and decode its JSON answer."""
    import mcp
    text = mcp.call("run_lua", {"code": code})
    try:
        return json.loads(text)
    except ValueError:
        return text


def load_chunk():
    if lua(LUA.read_text()) != "ok":
        raise ProbeError("could not load probes/v14/m4_probes.lua")


def step(name, cfg=None):
    arg = "" if cfg is None else json.dumps(json.dumps(cfg))
    out = lua(f"return BNGE_M4.{name}({arg})")
    if isinstance(out, dict) and out.get("error"):
        raise ProbeError(f"{name}: {out['error']}")
    return out


# --- preparation (pure: unit-tested in server/tests/test_m4_probes_tool.py) -------------------
def check_info(info):
    """Problems that forbid the terrain probes (empty: go)."""
    bad = []
    if not info.get("mod"):
        bad.append("the BeamNG.Earth mod is not running")
    if not info.get("terrain"):
        bad.append("no bngEarthTerrain block")
    if info.get("blockSize", 1024) != 1024:
        bad.append(f"the session runs a {info.get('blockSize')}² block: run the probes against --block 1024")
    car, blk = info.get("car"), info.get("block")
    if not car:
        bad.append("no player vehicle")
    elif car["speed"] > MAX_SPEED:
        bad.append(f"the car moves ({car['speed']:.1f} m/s): park it")
    if car and blk:
        d = max(abs(car["x"] - blk["cx"]), abs(car["y"] - blk["cy"]))
        if d > MAX_OFFSET:
            bad.append(f"the car is {d:.0f} m from the block centre (max {MAX_OFFSET:.0f}): wait for a terrain commit")
    return bad


def heightmap_path(user_dir, info):
    """The installed block's exact heightmap, with the centre-based lookup for older mods."""
    if info.get("heightmap"):
        vfs = info["heightmap"]
        path = Path(user_dir, vfs.lstrip("/"))
        if not path.is_file():
            raise ProbeError(f"live heightmap {vfs} missing under {user_dir}")
        return path, vfs
    cx, cy = info["block"]["cx"], info["block"]["cy"]
    name = f"hm_{info['anchorId']}_{int(cx)}_{int(cy)}.png"
    sid = info.get("session")
    cands = [Path(user_dir, "temp", "beamngEarth", sid, name)] if sid else []
    cands += sorted(Path(user_dir, "temp", "beamngEarth").glob(f"*/{name}"), key=lambda p: p.stat().st_mtime)[::-1]
    for p in cands:
        if p.is_file():
            return p, "/" + p.relative_to(user_dir).as_posix()
    raise ProbeError(f"{name} not found under {user_dir}/temp/beamngEarth (is the server's session dir there?)")


def read_q16(path):
    """The heightmap's uint16 grid, PNG order (row 0 = north)."""
    with Image.open(path) as im:
        return np.array(im).astype(np.uint16)


def solve_max_height(q16, info):
    """maxHeight of the live import, from the block's heights at lattice samples (h = zBase + q16 *
    maxH / 65536 exactly there) snapped to q * 65536 on the server's ladder; checked against
    getHeightScaleUser when the game reports it."""
    n = q16.shape[0]
    z = info["tb"]["z"]
    est = []
    for s in info.get("samples", []):
        if s.get("h") is False or s.get("h") is None:
            continue
        q = int(q16[n - 1 - int(s["gy"]), int(s["gx"])])
        if q > 0:
            est.append((s["h"] - z) * 65536 / q)
    if not est:
        raise ProbeError("no terrain samples to derive maxHeight from")
    m = statistics.median(est)
    best = min((q * 65536 for q in heightmap.Q_LADDER), key=lambda v: abs(v - m))
    if abs(best - m) > 1e-3 * best:
        raise ProbeError(f"maxHeight {m:.4f} is not q * 65536 on the ladder: refusing to re-import")
    hsu = info["tb"].get("heightScaleUser")
    return best, hsu


def write_crop(q16, out_dir, cx, cy):
    """The 512² block at the same centre: samples 256..767 of the 1024² one (PNG rows too, row 0 =
    north), written as the server writes heightmaps (16-bit, uncompressed), plus a 512² layer map."""
    n = q16.shape[0]
    if n != 1024:
        raise ProbeError(f"the live heightmap is {n}², expected 1024²")
    os.makedirs(out_dir, exist_ok=True)
    crop = np.ascontiguousarray(q16[256:768, 256:768])
    hm = os.path.join(out_dir, f"hm512_{int(cx)}_{int(cy)}.png")
    Image.fromarray(crop).save(hm + ".tmp", format="PNG", compress_level=0)
    os.replace(hm + ".tmp", hm)
    layer = os.path.join(out_dir, "layer_512.png")
    Image.new("RGB", (512, 512), (255, 255, 255)).save(layer)
    return hm, layer


def prepare(user_dir):
    load_chunk()
    info = step("info")
    bad = check_info(info)
    if bad:
        raise ProbeError("not now: " + "; ".join(bad))
    cx, cy = info["block"]["cx"], info["block"]["cy"]
    tb = info["tb"]
    if abs(tb["x"] - (cx - 1024)) > 1e-6 or abs(tb["y"] - (cy - 1024)) > 1e-6:
        raise ProbeError(f"the block sits at ({tb['x']}, {tb['y']}), not at the centre {cx, cy} - 1024")
    local, vfs = heightmap_path(user_dir, info)
    q16 = read_q16(local)
    max_h, hsu = solve_max_height(q16, info)
    hm, layer = write_crop(q16, os.path.join(user_dir, *PROBE_DIR), cx, cy)
    base = "/" + "/".join(PROBE_DIR)
    cfg = {"cx": cx, "cy": cy, "hm512": f"{base}/{os.path.basename(hm)}", "layer512": f"{base}/layer_512.png",
           "orig": {"path": vfs, "maxH": max_h, "x": tb["x"], "y": tb["y"], "z": tb["z"], "render": tb["render"],
                    "layer": LAYER_1024}}
    print(f"live block ({cx:.0f}, {cy:.0f}) zBase {tb['z']:.3f} maxHeight {max_h:g} "
          f"(getHeightScaleUser {hsu}), render {tb['render']}, heightmap {vfs}", file=sys.stderr)
    return info, cfg


# --- evaluation (pure) ---------------------------------------------------------------------
def _hits(rays, want_hit):
    """[(dx, dy, ok)] for rays expected to hit (inside) or miss."""
    return [(r["dx"], r["dy"], (r["h"] is not False) == want_hit) for r in rays]


def inside(dx, dy, half):
    """Is (c + dx, c + dy) inside the collidable [c-half, c+half-2] of a block centred at c?"""
    return -half <= dx <= half - 2 and -half <= dy <= half - 2


def extent_ok(rays, half):
    """Rays inside the block's collidable area hit, the others miss (the probe's extentPoints)."""
    return all(ok for r in rays for *_, ok in _hits([r], inside(r["dx"], r["dy"], half)))


def heights_equal(a, b, tol=HEIGHT_TOL, dz=0.0):
    """Every point that hits in both sets is at the same height (+dz in b)."""
    m = {(r["dx"], r["dy"]): r["h"] for r in a if r["h"] is not False}
    pairs = [(m[(r["dx"], r["dy"])], r["h"]) for r in b if r["h"] is not False and (r["dx"], r["dy"]) in m]
    return bool(pairs) and all(abs(hb - ha - dz) <= tol for ha, hb in pairs)


def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else math.nan


def evaluate_p2(a, b):
    imp = lambda series: med([s["importMs"] for s in series if s.get("ok")])  # noqa: E731
    rl = lambda series: med([s["reloadMs"] for s in series])  # noqa: E731
    r = {
        "first512_import_ms": a["first512"]["importMs"], "first512_reload_ms": a["first512"]["reloadMs"],
        "steady512_layer_ms": imp(b["steady512Layer"]), "steady512_nolayer_ms": imp(b["steady512NoLayer"]),
        "steady512_reload_ms": rl(b["steady512Layer"]),
        "back1024_import_ms": b["back1024"]["importMs"], "steady1024_ms": imp(b["steady1024Layer"]),
        "render_survives": a["renderAfterImport"] == a["renderBefore"],
        "extent512_same_frame": extent_ok(a["sameFrame"], 512), "extent512": extent_ok(b["laterFrame"], 512),
        "heights512": heights_equal(a["before512"], b["laterFrame"]),
        "extent1024_back": extent_ok(b["back1024Rays"], 1024),
        "restored": heights_equal(a["before"], b["after"]) and extent_ok(b["after"], 1024),
        "imports_ok": a["first512"]["ok"] and all(s["ok"] for s in b["steady512Layer"] + b["steady512NoLayer"]),
        "error": b.get("error"),
    }
    r["gate"] = r["steady512_layer_ms"] <= GATE_IMPORT_MS
    r["pass"] = (r["gate"] and r["extent512"] and r["heights512"] and r["extent1024_back"] and r["restored"]
                 and r["imports_ok"] and not r["error"])
    return r


def evaluate_p11(a, samples, b, b_samples, dz=DZ_P11):
    """When rays (and the car) followed the re-import without reloadCollision."""
    def follow_t(seq, test):
        for s in seq:
            if test(s):
                return s["ms"]
        return None
    before = a["rayBefore"]
    lifted = lambda h: h is not False and before is not False and abs(h - before - dz) <= 0.02  # noqa: E731
    same_a = lifted(a["raySameFrame"])
    t_a = 0.0 if same_a else follow_t(samples, lambda s: lifted(s["ray"]))
    car_dz = samples[-1]["carZ"] - a["carBefore"][2] if samples else math.nan
    gone = lambda rs: all(r["h"] is False for r in rs if r["dx"] or r["dy"])  # noqa: E731
    same_b = gone(b["sameFrame"])
    t_b = 0.0 if same_b else follow_t(b_samples, lambda s: gone(s["rays"]))
    r = {"lift_same_frame": same_a, "lift_after_ms": t_a, "car_dz_after": car_dz,
         "shrink_same_frame": same_b, "shrink_after_ms": t_b}
    r["reload_can_go"] = same_a and same_b
    r["verdict"] = ("a commit may skip reloadCollision" if r["reload_can_go"] else
                    "keep reloadCollision in every commit" + (" (physics follows by itself later)"
                                                              if t_a is not None else " (physics does not follow)"))
    return r


def evaluate_p7a(read, base_ms=None):
    """Per rebase (moveVehicles ... send.commit_anchor) and per terrain-follow commit (terrain.load ...
    farfield.onBlockCommit): the parts, their frames and the frames' wall ms."""
    calls, frames, fb = read["calls"], read["frames"], read.get("frameBase", 0)
    base = base_ms if base_ms is not None else (med(frames) if frames else math.nan)

    def frame_ms(f):
        i = f + 1 - fb - 1                      # a call with frame = f ran in frame number f + 1
        return frames[i] if 0 <= i < len(frames) else None
    out = {"rebases": [], "commits": [], "base_frame_ms": base}
    i = 0
    while i < len(calls):
        c = calls[i]
        if c["name"] == "rebase.moveVehicles":
            parts = {"moveVehicles": c["t1"] - c["t0"]}
            j = i + 1
            while j < len(calls) and calls[j]["name"] != "send.commit_anchor":
                n = calls[j]["name"].split(".", 1)[1]
                parts[n] = parts.get(n, 0.0) + calls[j]["t1"] - calls[j]["t0"]
                j += 1
            if j < len(calls):
                prev = calls[j - 1]
                parts["reloadCollision"] = calls[j]["t0"] - prev["t1"]
                phase2 = calls[j]["frame"]
                fm = frame_ms(phase2)
                work = sum(v for k, v in parts.items() if k != "moveVehicles") + (
                    parts["moveVehicles"] if c["frame"] == phase2 else 0.0)
                out["rebases"].append({"parts": parts, "phase1_frame": c["frame"], "phase2_frame": phase2,
                                       "frame_ms": fm, "work_ms": work,
                                       "remainder_ms": None if fm is None else fm - work - base})
            i = j + 1
            continue
        if c["name"] == "terrain.load":
            j = i + 1
            if j < len(calls) and calls[j]["name"] == "farfield.onBlockCommit":
                fm = frame_ms(c["frame"])
                parts = {"load": c["t1"] - c["t0"], "reloadCollision": calls[j]["t0"] - c["t1"],
                         "onBlockCommit": calls[j]["t1"] - calls[j]["t0"]}
                out["commits"].append({"parts": parts, "frame": c["frame"], "frame_ms": fm,
                                       "remainder_ms": None if fm is None else fm - sum(parts.values()) - base})
                i = j + 1
                continue
        i += 1
    rem = [r["remainder_ms"] for r in out["rebases"] if r["remainder_ms"] is not None]
    out["rebase_remainder_ms"] = med(rem) if rem else None
    out["target_45ms_without_p7b"] = bool(rem) and med(rem) <= REMAINDER_MAX_MS
    return out


def evaluate_p7b(r, dz=DZ_P7B):
    pts = lambda rs: {(x["dx"], x["dy"]): x["h"] for x in rs}  # noqa: E731
    before = pts(r["before"])
    inside = [k for k in before if k != (700, 0)]

    def same(rs, d):
        p = pts(rs)
        return all(p.get(k) is not False and before[k] is not False and abs(p[k] - before[k] - d) <= 0.02
                   for k in inside if before[k] is not False)
    after_swap = pts(r.get("afterSwap", []))
    res = {
        "hidden_import_ms": r.get("hiddenImport", {}).get("importMs"),
        "b_silent_before_reload": same(r.get("withBNoReload", []), 0.0),
        "b_silent_after_reload": same(r.get("withBReload", []), 0.0),
        "b_collides_after_swap": bool(after_swap) and same(r["afterSwap"], dz) and after_swap.get((700, 0)) is False,
        "swap_ms": r.get("swapMs"), "error": r.get("error"),
        "restored": same(r["after"], 0.0),
    }
    res["pass"] = (res["b_silent_before_reload"] and res["b_silent_after_reload"] and res["b_collides_after_swap"]
                   and res["swap_ms"] is not None and res["swap_ms"] <= SWAP_MAX_MS and not res["error"])
    return res


def report(name, res):
    print(json.dumps({name: res}, indent=1, default=str))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=("check", "p2", "p11", "p7a-install", "p7a-read", "p7a-uninstall", "p7b", "clean"))
    ap.add_argument("--user-dir", default=os.path.expanduser("~/.local/share/BeamNG/BeamNG.drive/current"))
    ap.add_argument("--raw", action="store_true", help="also print the probes' raw answers")
    args = ap.parse_args(argv)
    import time
    if args.cmd == "clean":
        shutil.rmtree(os.path.join(args.user_dir, *PROBE_DIR), ignore_errors=True)
        print("removed", os.path.join(args.user_dir, *PROBE_DIR))
        return
    if args.cmd == "check":
        load_chunk()
        info = step("info")
        bad = check_info(info)
        report("check", {"go": not bad, "problems": bad, "info": info})
        return
    if args.cmd.startswith("p7a"):
        if args.cmd == "p7a-install":
            load_chunk()
            report("p7a-install", step("p7aInstall"))
        elif args.cmd == "p7a-read":
            raw = step("p7aRead")
            if args.raw:
                report("raw", raw)
            report("p7a", evaluate_p7a(raw))
        else:
            print(lua("return BNGE_M4 and BNGE_M4.p7aUninstall() or 'not loaded'"))
        return
    info, cfg = prepare(args.user_dir)
    try:
        run_terrain_probe(args, cfg, time)
    except BaseException:
        # a failed MCP call between the steps would leave physics paused and the live block on a
        # probe heightmap: put the original block back and resume physics before reporting the error
        try:
            step("restore", cfg)
            print("probe failed: the live block was restored and physics resumed", file=sys.stderr)
        except BaseException as e:      # noqa: BLE001 (the original error is the one to raise)
            print(f"probe failed and the restore failed too ({e!r}): run `restore` by hand "
                  "(BNGE_M4.restore) before the user plays", file=sys.stderr)
        raise


def run_terrain_probe(args, cfg, time):
    """p2 / p11 / p7b (main() restores the block and resumes physics if one of them fails)."""
    if args.cmd == "p2":
        a = step("p2a", cfg)
        time.sleep(0.5)                             # a few frames (physics paused)
        b = step("p2b", cfg)
        if args.raw:
            report("raw", {"p2a": a, "p2b": b})
        report("p2", evaluate_p2(a, b))
    elif args.cmd == "p11":
        cfg["dz"] = DZ_P11
        a = step("p11a", cfg)
        samples = []
        for t in (0.25, 0.25, 0.5, 1.0):            # ~0.25, 0.5, 1, 2 s after the import
            time.sleep(t)
            samples.append(step("p11sample"))
        b = step("p11b", cfg)
        b_samples = []
        for t in (0.25, 0.25, 0.5, 1.0):
            time.sleep(t)
            b_samples.append(step("p11bSample"))
        restored = step("restore", cfg)
        if args.raw:
            report("raw", {"p11a": a, "samples": samples, "p11b": b, "bSamples": b_samples, "restore": restored})
        res = evaluate_p11(a, samples, b, b_samples)
        res["restore"] = restored
        report("p11", res)
    elif args.cmd == "p7b":
        cfg["dz"] = DZ_P7B
        r = step("p7b", cfg)
        if args.raw:
            report("raw", r)
        report("p7b", evaluate_p7b(r))


if __name__ == "__main__":
    main()
