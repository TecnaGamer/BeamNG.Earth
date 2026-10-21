#!/usr/bin/env python3
"""GE-side drive harness for speed-dependent in-game tests (docs/V1_4_SPEC.md 3.0, M0).

tools/sim_drive.py stepped the car with MCP set_position (spawn.safeTeleport: a reset per step,
velocity 0, slower than nominal, and its screenshots made > 50 ms frames by themselves). This one
installs a per-frame driver inside the game (tools/drive_driver.lua, sent through MCP run_lua; an
extension-proxy event sink, no mod file) that moves the player along a profile every frame without
resets and with the profile's velocity, follows the static-collision ground and leaves the inputs at
zero. MCP is only polled: one snapshot per second at most (the driver's frames since the last poll,
far.* / gov.* / perfmon / the server backlog). The frames in which that MCP work ran are tagged and
left out of the frame statistics. Timing runs take no screenshots; --shots S makes a separate visual
run (same profile) that takes one every S seconds and is marked visual in its summary.

  python3 tools/drive.py --out docs/research/d40 --speed 40 --heading 37 --km 20      (D40)
  python3 tools/drive.py --out docs/research/f300 --profile f300.json
  python3 tools/drive.py --out docs/research/sim --step-mode mcp --speed 30 --km 5     (old stepping)

Profile file: a JSON list of points, or {"points": [...], "km": D}. A point is {"t": s} or {"s": m}
(all points the same key), "speed" (m/s), "heading" (deg, 0 = north, 90 = east; omitted = the car's),
optional "alt" (m above the start ground; a flight profile) and "vz" (m/s). Speed and heading are
linear between points and held after the last one; the run ends at "km" or at the last point.

Run the server with --status-interval 1 so status.backlog is sampled every second.
Writes <out>/samples.jsonl (one poll per line), <out>/frames.jsonl (the raw frame arrays per poll)
and <out>/summary.json (see summarize()). Exit status 1 when the run fails (below 95 % of nominal
speed, or the driver stopped early).
"""
import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DRIVER = os.path.join(HERE, "drive_driver.lua")
MIN_POLL_S = 1.0            # MCP polls at most once per second (§3.0)
SPEED_PASS = 0.95           # a run below 95 % of nominal speed fails
REBASE_EXCLUDE_S = 2.0      # exactMiss is also reported without the 2 s after each rebase
REBASE_FRAMES = 3           # heavy frames up to this many frames after rebase phase 1 belong to the rebase
# driver frame flags (tools/drive_driver.lua FLAG_*)
F_HEAVY, F_REBASE, F_HARNESS, F_FALL, F_MISS, F_OLD, F_SKIP = 1, 2, 4, 8, 16, 32, 64
NO_TILE = 99


# --- MCP -------------------------------------------------------------------------------
def _mcp():
    sys.path.insert(0, HERE)
    import mcp      # tools/mcp.py
    return mcp


def lua(code, timeout=60):
    m = _mcp()
    try:
        res = m.rpc("tools/call", {"name": "run_lua", "arguments": {"code": code}}, timeout=timeout)
    except SystemExit as e:             # mcp.py reports RPC errors that way
        raise RuntimeError(str(e)) from None
    text = "\n".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
    if res.get("isError"):
        raise RuntimeError("lua: " + text[:400])
    return text


def lua_long(text):
    """A Lua long-bracket string literal holding `text` verbatim."""
    n = 0
    while ("]" + "=" * n + "]") in text:
        n += 1
    return "[" + "=" * n + "[" + text + "]" + "=" * n + "]"


def screenshot(path):
    out = _mcp().call("screenshot").splitlines()[-1]
    for _ in range(50):
        if os.path.exists(out) and os.path.getsize(out) > 0:
            break
        time.sleep(0.1)
    time.sleep(0.3)
    shutil.copy(out, path)


# --- profiles --------------------------------------------------------------------------
def straight(speed, heading, km):
    return {"points": [{"s": 0, "speed": speed, "heading": heading}], "km": km}


def load_profile(path):
    with open(path) as f:
        p = json.load(f)
    return p if isinstance(p, dict) else {"points": p}


def _angle_diff(a, b):
    d = (a - b) % 360.0
    return d - 360.0 if d > 180.0 else d


def profile_at(points, u, key):
    """Speed and heading at u (t or s): the same rule as drive_driver.lua profileAt."""
    if u <= points[0][key] or len(points) == 1:
        p = points[0]
        return p["speed"], p.get("heading")
    for a, b in zip(points, points[1:]):
        if u < b[key]:
            f = (u - a[key]) / (b[key] - a[key])
            ha, hb = a.get("heading"), b.get("heading")
            h = None if ha is None or hb is None else (ha + f * _angle_diff(hb, ha)) % 360.0
            return a["speed"] + f * (b["speed"] - a["speed"]), h
    p = points[-1]
    return p["speed"], p.get("heading")


def nominal(profile, step=0.05):
    """(distance m, duration s, mean speed m/s) the profile asks for: up to `km` if given, else to
    its last point. Integrated the way the driver advances (speed at the start of each step)."""
    pts = profile["points"]
    key = "t" if "t" in pts[0] else "s"
    limit = profile.get("km")
    limit = None if limit is None else 1000.0 * float(limit)
    end = pts[-1][key]
    # a profile that can stop for good never ends: by distance with a zero speed anywhere, by time
    # with a km beyond its last point and a zero final speed
    if key == "s" and any(p["speed"] <= 0 for p in pts):
        raise ValueError("a distance profile needs speed > 0 at every point")
    if key == "t" and limit is not None and pts[-1]["speed"] <= 0:
        raise ValueError("a time profile with km needs a final speed > 0")
    s = t = 0.0
    while True:
        u = t if key == "t" else s
        if (limit is not None and s >= limit) or (limit is None and u >= end):
            break
        v = profile_at(pts, u, key)[0]
        dt = step
        if limit is not None and v > 0:
            dt = min(dt, (limit - s) / v)
        if key == "t" and limit is None:
            dt = min(dt, end - t)
        s, t = s + v * dt, t + dt
    return s, t, (s / t if t > 0 else 0.0)


# --- summary ---------------------------------------------------------------------------
def _median(xs):
    xs = sorted(xs)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else 0.5 * (xs[n // 2 - 1] + xs[n // 2])


def _pct(xs, p):
    xs = sorted(xs)
    if not xs:
        return None
    return xs[min(len(xs) - 1, max(0, math.ceil(p * len(xs)) - 1))]


def slope_per_min(ts, ys):
    """Least-squares slope of ys over ts (s), per minute; None with fewer than 2 points."""
    pts = [(t, y) for t, y in zip(ts, ys) if y is not None]
    if len(pts) < 2:
        return None
    n = len(pts)
    mt = sum(t for t, _ in pts) / n
    my = sum(y for _, y in pts) / n
    var = sum((t - mt) ** 2 for t, _ in pts)
    if var == 0:
        return None
    return 60.0 * sum((t - mt) * (y - my) for t, y in pts) / var


def _r(x, nd=2):
    return None if x is None else round(x, nd)


def summarize(polls, profile, label=None, visual=False):
    """The run's summary from the poll records (BNGE_DRIVE.poll() each, plus "t": s since install).
    Frames: every frame the driver recorded, without the harness (MCP) frames; commit and rebase
    frames are reported on their own and left out of the steady-frame counts."""
    ms, flags, under, ftime, dropped = [], [], [], [], 0
    clock = 0.0
    for p in polls:
        fr = p.get("frames") or {}
        for a, b, c in zip(fr.get("ms") or [], fr.get("flags") or [], fr.get("under") or []):
            clock += a
            ms.append(a)
            flags.append(int(b))
            under.append(None if c == NO_TILE else c)
            ftime.append(clock / 1000.0)
        dropped += fr.get("dropped") or 0
    n = len(ms)
    harness = [bool(f & F_HARNESS) for f in flags]
    rebase_at = [i for i, f in enumerate(flags) if f & F_REBASE]
    in_rebase = [False] * n
    for i in rebase_at:
        for k in range(i, min(n, i + REBASE_FRAMES + 1)):
            in_rebase[k] = True
    rebase_ms = [ms[i] for i in range(n) if in_rebase[i] and (flags[i] & (F_HEAVY | F_REBASE)) and not harness[i]]
    commit_ms = [ms[i] for i in range(n) if flags[i] & F_HEAVY and not in_rebase[i] and not harness[i]]
    frames = [ms[i] for i in range(n) if not harness[i]]
    steady = [ms[i] for i in range(n) if not harness[i] and not in_rebase[i] and not flags[i] & F_HEAVY]
    rebase_t = [ftime[i] for i in rebase_at]
    after_rebase = [any(0 <= ftime[i] - rt <= REBASE_EXCLUDE_S for rt in rebase_t) for i in range(n)]
    miss = [bool(f & F_MISS) for f in flags]
    levels = {}
    for u in under:
        k = "none" if u is None else str(u)
        levels[k] = levels.get(k, 0) + 1

    last = polls[-1] if polls else {}
    drive = last.get("drive") or {}
    # wall time of the drive itself: install -> the frame that finished it (polls come up to 1 s later)
    wall = (drive.get("endMs") if drive.get("endMs") is not None else (last.get("wallMs") or 0)) / 1000.0
    dist = drive.get("s") or 0.0
    nom_dist, nom_time, nom_speed = nominal(profile)
    eff = dist / wall if wall > 0 else 0.0
    v_n = sum((p.get("drive") or {}).get("vN") or 0 for p in polls)
    v_sum = sum((p.get("drive") or {}).get("vSum") or 0 for p in polls)
    v_prof = sum((p.get("drive") or {}).get("vProfSum") or 0 for p in polls)

    mods = [(p["t"], p.get("mod") or {}) for p in polls if p.get("mod")]
    fars = [(t, m.get("far") or {}) for t, m in mods]
    govs = [(t, m.get("governor") or {}) for t, m in mods]
    pose_speeds = [(m.get("player") or {}).get("speed") for _, m in mods]
    pose_speeds = [v for v in pose_speeds if v is not None]
    backlog_t = [t for t, m in mods if m.get("serverBacklog") is not None]
    backlog = [m["serverBacklog"] for _, m in mods if m.get("serverBacklog") is not None]

    def fmax(key):
        vals = [f.get(key) for _, f in fars if f.get(key) is not None]
        return max(vals) if vals else None
    objects = [(f.get("live") or 0) + (f.get("old") or 0) + (f.get("deleting") or 0) for _, f in fars]
    # textured import estimate, first vs last quarter: the session's main kind (64-cell tiles, M1:
    # cdaeTex256 is the near levels' kind; FORMAT 3: cdaeTex), and every textured kind apart
    cells = next((m.get("cells") for _, m in reversed(mods) if m.get("cells") is not None), None)

    def first_last(key):
        vals = [g.get(key) for _, g in govs if g.get(key) is not None]
        q = max(1, len(vals) // 4)
        return _median(vals[:q]), _median(vals[-q:])
    tex_key = "estCdaeTex256" if cells == 64 else "estCdaeTex"
    tex_first, tex_last = first_last(tex_key)
    tex_kinds = {}
    for key in ("estCdaeTex", "estCdaeTex128", "estCdaeTex256", "estCdaeTex512"):
        a, b = first_last(key)
        if a is not None:
            tex_kinds[key] = {"firstQ": _r(a), "lastQ": _r(b), "ratio": _r(b / a, 3) if a and b else None}

    # rebases seen in the polls (anchor change) and the reload: until far_done of the new anchor,
    # and until the previous anchor's tiles are gone
    reloads = []
    prev_anchor = None
    for i, (t, m) in enumerate(mods):
        a = m.get("anchorId")
        if prev_anchor is not None and a is not None and a != prev_anchor:
            done_t = next((t2 for t2, m2 in mods[i:] if m2.get("anchorId") == a and (m2.get("far") or {}).get("done")), None)
            clear_t = next((t2 for t2, m2 in mods[i:] if m2.get("anchorId") == a and not (m2.get("far") or {}).get("old")), None)
            reloads.append({"t": _r(t, 1), "from": prev_anchor, "to": a, "rebaseMs": _r(m.get("lastRebaseMs"), 1),
                            "farDoneS": _r(done_t - t, 1) if done_t is not None else None,
                            "oldGoneS": _r(clear_t - t, 1) if clear_t is not None else None})
        if a is not None:
            prev_anchor = a
    follow_ms = []
    prev_count = None
    for _, m in mods:
        c = m.get("followCount")
        if prev_count is not None and c is not None and c > prev_count:
            follow_ms.append(_r(m.get("lastFollowMs"), 1))
        prev_count = c if c is not None else prev_count
    end_far = fars[-1][1] if fars else {}

    summary = {
        "label": label, "visual": visual,
        "polls": len(polls), "frames": n, "harnessFrames": sum(harness), "droppedFrames": dropped,
        "km": _r(dist / 1000.0, 3), "wallS": _r(wall, 1),
        "nominalSpeed": _r(nom_speed), "nominalKm": _r(nom_dist / 1000.0, 3), "nominalS": _r(nom_time, 1),
        "effectiveSpeed": _r(eff), "effectiveRatio": _r(eff / nom_speed, 3) if nom_speed > 0 else None,
        "meanPoseSpeed": _r(sum(pose_speeds) / len(pose_speeds)) if pose_speeds else None,
        "physicsSpeedRatio": _r(v_sum / v_prof, 4) if v_prof > 0 else None,      # P0: within 2 %
        "physicsSpeedMean": _r(v_sum / v_n) if v_n else None,
        "reason": drive.get("reason"), "error": drive.get("error"),
        "falls": drive.get("falls"), "resets": drive.get("resets"), "rebases": drive.get("rebases"),
        "headingResyncs": drive.get("resyncs"),
        "fpsMean": _r(len(frames) / (sum(frames) / 1000.0), 1) if frames and sum(frames) > 0 else None,
        "p50": _r(_pct(frames, 0.5)), "p95": _r(_pct(frames, 0.95)), "p99": _r(_pct(frames, 0.99)),
        "maxMs": _r(max(frames) if frames else None),
        "over50": sum(1 for x in frames if x > 50),
        "over50Steady": sum(1 for x in steady if x > 50),       # outside commit / rebase / harness frames
        "maxSteadyMs": _r(max(steady) if steady else None),
        "commitFrames": len(commit_ms), "commitFrameMs": [_r(x, 1) for x in commit_ms],
        "commitFrameMaxMs": _r(max(commit_ms) if commit_ms else None, 1),
        "rebaseFrameMs": [_r(x, 1) for x in rebase_ms], "rebaseFrameMaxMs": _r(max(rebase_ms) if rebase_ms else None, 1),
        "followCommitMs": follow_ms,
        "exactMissFrames": sum(miss),
        "exactMissPct": _r(100.0 * sum(miss) / n, 2) if n else None,
        "exactMissFramesOutsideRebase": sum(1 for m_, a_ in zip(miss, after_rebase) if m_ and not a_),
        "underLevels": levels,
        "queuedMax": fmax("queued"), "waitingGroupsMax": fmax("waitingGroups") or fmax("groups"),
        "liveMax": fmax("live"), "objectsMax": max(objects) if objects else None,
        "backlogMax": max(backlog) if backlog else None, "backlogSamples": len(backlog),
        "backlogSlopePerMin": _r(slope_per_min(backlog_t, backlog)),
        "cells": cells, "estCdaeTexKind": tex_key,
        "estCdaeTexFirstQ": _r(tex_first), "estCdaeTexLastQ": _r(tex_last),
        "estCdaeTexRatio": _r(tex_last / tex_first, 3) if tex_first and tex_last else None,
        "estTexByKind": tex_kinds,
        "govCapLast": govs[-1][1].get("cap") if govs else None,
        "govTargetMax": max((g.get("target") or 0) for _, g in govs) if govs else None,
        "govBaseELast": (govs[-1][1].get("base_e", govs[-1][1].get("base")) if govs else None),
        "reloads": reloads,
        "pops": end_far.get("pops"), "failed": end_far.get("failed"),
        "materials": end_far.get("materials"), "liveEnd": end_far.get("live"),
    }
    fails = []
    if nom_speed > 0 and eff < SPEED_PASS * nom_speed:
        fails.append(f"effective speed {eff:.1f} m/s < {SPEED_PASS:.0%} of nominal {nom_speed:.1f}")
    if drive.get("reason") not in ("end", None):
        fails.append(f"driver stopped: {drive.get('reason')} {drive.get('error') or ''}".strip())
    if drive.get("reason") is None:
        fails.append("driver did not finish")
    summary["fails"] = fails
    summary["pass"] = not fails
    try:                                            # what each frame over 50 ms went to (core.lua hitches)
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import autodrive_perf
        summary["hitch_causes"] = autodrive_perf._causes(polls)
    except Exception as e:                          # an older mod: no hitches in the polls
        summary["hitch_causes"] = f"unavailable: {e}"
    # probe P0 (§7): the physics speed within 2 % of the profile, no reset events
    ratio = summary["physicsSpeedRatio"]
    summary["p0"] = {"speedWithin2pct": ratio is not None and abs(ratio - 1.0) <= 0.02,
                     "noResets": drive.get("resets") == 0}
    return summary


def poll_record(raw, t):
    rec = json.loads(raw)
    rec["t"] = round(t, 3)
    fr = rec.get("frames") or {}
    for k in ("ms", "flags", "under"):     # an empty Lua table may encode as {}
        if isinstance(fr.get(k), dict):
            fr[k] = []
    return rec


# --- run -------------------------------------------------------------------------------
# The drive never uses the user's own vehicle (in-game 2026-10-10: the user's lifted Hopper went
# physically unstable under per-frame placement at 67 m/s and the engine removed it). A stock test
# car is spawned at the player, the user's vehicle is frozen meanwhile (setActive(0), as vehicle
# pooling does: it would fall once the physics block follows the test car away) and given back at
# the end: test car deleted, user's vehicle woken, re-entered and teleported to where it was.
TEST_VEHICLE = ("vivace", None)     # model, config (None = default); handled every run so far

SETUP_TEST_VEHICLE = r"""
local car = getPlayerVehicle(0)
if not car then return jsonEncode({ error = 'no player vehicle' }) end
local s = loadstring('return ' .. beamngEarth_core.status())()
local p, d = car:getPosition(), car:getDirectionVector()
local opts = { pos = vec3(p.x, p.y, p.z + 2), rot = quatFromDir(vec3(d.x, d.y, 0):normalized(), vec3(0, 0, 1)),
  autoEnterVehicle = true }
if %s then opts.config = %s end
local t = core_vehicles.spawnNewVehicle(%s, opts)
if not t then return jsonEncode({ error = 'test vehicle spawn failed' }) end
BNGE_DRIVE_CAR = { carId = car:getID(), testId = t:getID(), lat = s.player and s.player.lat or (s.lat or 0), lon = s.player and s.player.lon or (s.lon or 0) }
car:setActive(0)
t:setPositionRotation(p.x, p.y, p.z, opts.rot.x, opts.rot.y, opts.rot.z, opts.rot.w)
t:applyClusterVelocityScaleAdd(-1, 0, 0, 0, 0)
return jsonEncode(BNGE_DRIVE_CAR)"""

RESTORE_USER_VEHICLE = r"""
local st = BNGE_DRIVE_CAR
if not st then return 'no test vehicle' end
local t = st.testId and getObjectByID(st.testId)
if t then t:delete() end
local car = getObjectByID(st.carId)
if car then car:setActive(1); be:enterVehicle(0, car) end
commands.setGameCamera()
local ok = car and beamngEarth_core.teleport(st.lat, st.lon)
BNGE_DRIVE_CAR = nil
return 'user vehicle ' .. tostring(st.carId) .. ' restored, teleport ' .. tostring(ok)"""


def _lua_str(v):
    return "nil" if v is None else json.dumps(v)


def run_ge(a, profile, call=None, clock=time):
    """Install the driver, poll it until the drive ends, uninstall it. Everything after the first
    MCP call runs inside the try: whatever fails (an MCP timeout after the install went through, a
    bad answer, Ctrl-C), the finally block uninstalls the driver (proxy, rebase wrapper; the car
    parked, inputs zero). Polls start at least `interval` (>= MIN_POLL_S) apart.
    call / clock: the run_lua call and the time source (tests pass fakes; default lua() and time)."""
    lua_ = call or lua
    os.makedirs(a.out, exist_ok=True)
    with open(DRIVER) as f:
        code = f.read()
    cfg = dict(profile)
    if a.exact_level is not None:
        cfg["exactLevel"] = a.exact_level
    polls = []
    samples = frames_out = None
    interval = max(MIN_POLL_S, a.poll)
    test_car = not getattr(a, "use_player_vehicle", False)
    try:
        if test_car:
            model, config = TEST_VEHICLE
            res = json.loads(lua_(SETUP_TEST_VEHICLE % (_lua_str(config), _lua_str(config), _lua_str(model))))
            if "error" in res:
                raise SystemExit("test vehicle: " + res["error"])
            print("test vehicle", res, flush=True)
            clock.sleep(3.0)                    # spawned and settled on the ground
        print("driver:", lua_(code), flush=True)
        lua_("if not beamngEarth_perfmon then extensions.load('beamngEarth_perfmon') end return 1")
        res = json.loads(lua_(f"return BNGE_DRIVE.install({lua_long(json.dumps(cfg))})"))
        if "error" in res:
            raise SystemExit("install: " + res["error"])
        print("installed", res, flush=True)
        samples = open(os.path.join(a.out, "samples.jsonl"), "w")
        frames_out = open(os.path.join(a.out, "frames.jsonl"), "w")
        t0 = clock.monotonic()
        next_poll, next_shot, n_shot = t0 + interval, t0 + (a.shots or 0), 0
        while True:
            clock.sleep(max(0.0, next_poll - clock.monotonic()))
            now = clock.monotonic()
            next_poll = now + interval         # never two polls closer than the interval (§3.0)
            rec = poll_record(lua_("return BNGE_DRIVE.poll()"), now - t0)
            polls.append(rec)
            fr = rec.get("frames") or {}
            frames_out.write(json.dumps({"t": rec["t"], **fr}) + "\n")
            line = {k: v for k, v in rec.items() if k != "frames"}
            line["nFrames"] = len(fr.get("ms") or [])
            line["maxMs"] = max(fr.get("ms") or [0])
            samples.write(json.dumps(line) + "\n")
            samples.flush()
            frames_out.flush()
            d = rec.get("drive") or {}
            far = (rec.get("mod") or {}).get("far") or {}
            print(f"t {rec['t']:6.1f}  km {(d.get('s') or 0) / 1000:6.2f}  frames {line['nFrames']:4d} "
                  f"max {line['maxMs']:6.1f} ms  queued {far.get('queued')}  live {far.get('live')}  "
                  f"under {far.get('underLevel')}  backlog {(rec.get('mod') or {}).get('serverBacklog')}", flush=True)
            if d.get("done") or d.get("error"):
                break
            if now - t0 > a.max_s:
                print("timeout", flush=True)
                break
            if a.shots and now >= next_shot:
                screenshot(os.path.join(a.out, f"visual_{n_shot:03d}.png"))
                n_shot, next_shot = n_shot + 1, now + a.shots
    finally:
        try:
            print(lua_("return BNGE_DRIVE and BNGE_DRIVE.uninstall() or 'no driver'"), flush=True)
        except Exception as e:      # the game went away: the driver's watchdog parks the car
            print("uninstall failed:", e, flush=True)
        if test_car:
            try:
                print(lua_(RESTORE_USER_VEHICLE), flush=True)
            except Exception as e:
                print("restoring the user's vehicle failed (run BNGE_DRIVE_CAR restore by hand):", e, flush=True)
        for f in (samples, frames_out):
            if f is not None:
                f.close()
    return polls


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--profile", help="profile JSON (see the module doc)")
    ap.add_argument("--speed", type=float, help="m/s (straight profile)")
    ap.add_argument("--heading", type=float, default=None, help="deg, 0 = north (default: the car's heading)")
    ap.add_argument("--km", type=float, help="distance (straight profile; caps a --profile too)")
    ap.add_argument("--label", default=None)
    ap.add_argument("--poll", type=float, default=1.0, help=f"seconds between MCP polls (>= {MIN_POLL_S})")
    ap.add_argument("--shots", type=float, default=0.0,
                    help="visual run: a screenshot every S seconds (not a timing run; default 0 = none)")
    ap.add_argument("--max-s", type=float, default=3600.0, help="give up after this many seconds")
    ap.add_argument("--exact-level", type=int, default=None,
                    help="exact level for a mod without farfield.exactState (default -2)")
    ap.add_argument("--use-player-vehicle", action="store_true",
                    help="drive the player's own vehicle (default: spawn a stock test car, freeze the "
                         "player's vehicle and restore it at the end)")
    ap.add_argument("--step-mode", choices=("ge", "mcp"), default="ge",
                    help="ge: the per-frame driver (default); mcp: the old sim_drive.py set_position stepping")
    a = ap.parse_args(argv)
    # a fresh game shows our welcome page over the whole screen: closed and kept away for this game session (the
    # drive measures the world, not our UI)
    _mcp().quiet_ui()
    if a.step_mode == "mcp":
        if a.profile or a.speed is None or a.km is None:
            raise SystemExit("--step-mode mcp takes --speed, --km and --heading only (sim_drive.py)")
        cmd = [sys.executable, os.path.join(HERE, "sim_drive.py"), "--out", a.out, "--km", str(a.km),
               "--speed", str(a.speed), "--heading", str(90.0 if a.heading is None else a.heading)]
        return subprocess.call(cmd)
    if a.profile:
        profile = load_profile(a.profile)
        if a.km is not None:
            profile["km"] = a.km
    elif a.speed is not None and a.km is not None:
        profile = straight(a.speed, a.heading, a.km)
    else:
        raise SystemExit("need --profile, or --speed and --km")
    if a.heading is None:
        for p in profile["points"]:
            if p.get("heading") is None:
                p.pop("heading", None)          # the driver uses the car's heading
    if profile.get("km") is None and len(profile["points"]) < 2:
        raise SystemExit("a one-point profile needs --km (or \"km\" in the file)")
    nominal(profile)                            # rejects a profile that never ends
    polls = run_ge(a, profile)
    summary = summarize(polls, profile, a.label, visual=a.shots > 0)
    with open(os.path.join(a.out, "summary.json"), "w") as f:
        json.dump(summary, f, indent=1)
    print(json.dumps(summary, indent=1))
    return 0 if summary["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
