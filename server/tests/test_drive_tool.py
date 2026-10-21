"""tools/drive.py and tools/drive_driver.lua offline (docs/V1_4_SPEC.md 3.0, M0): the profile rule
of both sides, the nominal speed, the summary (frame classes, exactMiss with the 2 s rebase window,
backlog slope, the 95 % speed rule), the driver's constants against the server's, and the driver
run under luajit with stub engine calls, its poll JSON fed through summarize(). Nothing here talks
to the game."""
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "tools"))
import drive  # noqa: E402
import farfield as ff  # noqa: E402

LUAJIT = shutil.which("luajit")
DRIVER = ROOT / "tools" / "drive_driver.lua"
STUBS = ROOT / "mod" / "tests" / "stubs.lua"


def driver_consts():
    out = {}
    for m in re.finditer(r"^D\.(\w+) = (-?[\d.]+)\b", DRIVER.read_text(), re.M):
        out[m.group(1)] = float(m.group(2))
    return out


def run_lua(script):
    res = subprocess.run([LUAJIT, "-"], input=script, capture_output=True, text=True, timeout=60,
                         cwd=str(ROOT / "mod" / "tests"))
    if res.returncode != 0:
        raise AssertionError(res.stderr)
    return res.stdout


# stub engine for the driver: flat ground at z = 50 (a miss beyond x = 4000), one vehicle
LUA_ENGINE = r"""
dofile('%(stubs)s')
function castRayStatic(pos, dir, len)
  if pos.x > 4000 then return len end
  return pos.z - 50
end
local veh = { pos = vec3(0, 0, 50.5), vel = vec3(0, 0, 0) }
function veh:getID() return 1 end
function veh:getPosition() return vec3(self.pos.x, self.pos.y, self.pos.z) end
function veh:getVelocity() return vec3(self.vel.x, self.vel.y, self.vel.z) end
function veh:getDirectionVector() return vec3(0, 1, 0) end
function veh:setClusterPosRelRot(n, x, y, z) self.pos = vec3(x, y, z) end
function veh:applyClusterVelocityScaleAdd(n, s, vx, vy, vz) self.vel = vec3(vx, vy, vz) end
function veh:queueLuaCommand() end
function getPlayerVehicle() return veh end
local sinks
function newExtensionProxy() return { submitEventSinks = function(_, s) sinks = s end, destroy = function() end } end
local heavyNext = false
local gov = { isHeavyFrame = function() return heavyNext end }
beamngEarth_core = { _governor = function() return gov end,
  status = function() return '{ anchorId = 1, far = { live = 800, queued = 3, done = true }, serverBacklog = 5 }' end }
package.loaded['beamngEarth/farfield'] = { exactState = function() return -2, false, false end }
local now = 0
assert(dofile('%(driver)s') == 'ok')
BNGE_DRIVE.clock = function() return now end
function FRAME(ms, dtSim, heavy)
  now = now + ms
  heavyNext = heavy or false
  for _, s in ipairs(sinks) do s:onPreRender(ms / 1000, dtSim or ms / 1000) end
end
"""


class ProfileTest(unittest.TestCase):
    def test_straight_nominal(self):
        d, t, v = drive.nominal(drive.straight(40.0, 37.0, 20.0))
        self.assertAlmostEqual(d, 20000.0, delta=1e-6)
        self.assertAlmostEqual(t, 500.0, delta=1e-6)
        self.assertAlmostEqual(v, 40.0, delta=1e-9)

    def test_profile_nominal(self):
        # 0 -> 40 m/s over 10 s (200 m), then 40 m/s: 1 km takes 10 + 800 / 40 = 30 s
        prof = {"points": [{"t": 0, "speed": 0, "heading": 0}, {"t": 10, "speed": 40, "heading": 0}], "km": 1}
        d, t, v = drive.nominal(prof, step=0.001)
        self.assertAlmostEqual(d, 1000.0, delta=0.05)
        self.assertAlmostEqual(t, 30.0, delta=0.01)
        # a time profile without km ends at its last point
        d, t, _ = drive.nominal({"points": prof["points"]}, step=0.001)
        self.assertAlmostEqual((d, t)[1], 10.0, delta=1e-6)
        self.assertAlmostEqual(d, 200.0, delta=0.1)
        with self.assertRaises(ValueError):
            drive.nominal({"points": [{"s": 0, "speed": 10}, {"s": 100, "speed": 0}], "km": 1})
        with self.assertRaises(ValueError):
            drive.nominal({"points": [{"t": 0, "speed": 10}, {"t": 10, "speed": 0}], "km": 1})

    def test_profile_at(self):
        pts = [{"s": 0, "speed": 10, "heading": 350}, {"s": 100, "speed": 30, "heading": 10}]
        self.assertEqual(drive.profile_at(pts, 50, "s"), (20.0, 0.0))
        self.assertEqual(drive.profile_at(pts, 500, "s"), (30, 10))
        self.assertEqual(drive.profile_at(pts, -1, "s"), (10, 350))

    @unittest.skipIf(LUAJIT is None, "luajit not installed")
    def test_profile_rule_matches_driver(self):
        pts = [{"t": 0, "speed": 5, "heading": 300}, {"t": 7, "speed": 50, "heading": 20},
               {"t": 9, "speed": 20, "heading": 190}, {"t": 30, "speed": 134, "heading": 37}]
        us = [-1, 0, 1.5, 6.99, 7, 8.2, 9, 15.5, 29.9, 30, 99]
        lua_pts = ", ".join(f"{{ t = {p['t']}, speed = {p['speed']}, heading = {p['heading']} }}" for p in pts)
        out = run_lua(f"""
assert(dofile('{DRIVER.as_posix()}') == 'ok')
local D = BNGE_DRIVE
local prof = D.makeProfile({{ {lua_pts} }}, 0)
for _, u in ipairs({{ {', '.join(str(u) for u in us)} }}) do
  local v, h = D.profileAt(prof, u)
  print(string.format('%.12f %.12f', v, h))
end
""")
        for u, line in zip(us, out.split("\n")):
            v, h = (float(x) for x in line.split())
            pv, ph = drive.profile_at(pts, u, "t")
            self.assertAlmostEqual(v, pv, delta=1e-9, msg=u)
            self.assertAlmostEqual(drive._angle_diff(h, ph), 0.0, delta=1e-9, msg=u)


class ConstantsTest(unittest.TestCase):
    def test_driver_constants(self):
        c = driver_consts()
        # the fallback for a mod without farfield.exactState: the v1.3.1 (32-cell) exact level; a mod
        # with it reports the active layout's (hello.cells)
        self.assertEqual(c["EXACT_LEVEL"], ff.MESH_MIN_LEVEL[32])
        self.assertEqual(c["MAX_LEVEL"], ff.MAX_LEVEL)
        self.assertEqual(c["L0"], ff.BASE)
        self.assertEqual(c["NO_TILE"], drive.NO_TILE)
        flags = {k[5:]: int(v) for k, v in c.items() if k.startswith("FLAG_")}
        self.assertEqual(flags, {"HEAVY": drive.F_HEAVY, "REBASE": drive.F_REBASE, "HARNESS": drive.F_HARNESS,
                                 "FALL": drive.F_FALL, "MISS": drive.F_MISS, "OLD": drive.F_OLD, "SKIP": drive.F_SKIP})

    def test_lua_long(self):
        self.assertEqual(drive.lua_long('{"a": [1]}'), '[[{"a": [1]}]]')
        self.assertEqual(drive.lua_long("x]]y"), "[=[x]]y]=]")
        self.assertEqual(drive.lua_long("]]]=]"), "[==[]]]=]]==]")

    def test_step_mode_mcp_args(self):
        # validated before anything runs (sim_drive.py would talk to the game)
        with self.assertRaises(SystemExit):
            drive.main(["--out", "/nonexistent", "--step-mode", "mcp", "--profile", "x.json"])
        with self.assertRaises(SystemExit):
            drive.main(["--out", "/nonexistent", "--step-mode", "mcp", "--speed", "30"])
        with self.assertRaises(SystemExit):
            drive.main(["--out", "/nonexistent"])
        with tempfile.TemporaryDirectory() as tmp:      # a one-point profile without km never ends
            path = Path(tmp) / "p.json"
            path.write_text(json.dumps([{"s": 0, "speed": 30, "heading": 0}]))
            with self.assertRaises(SystemExit) as cm:
                drive.main(["--out", tmp, "--profile", str(path)])
            self.assertIn("one-point", str(cm.exception))


def poll(t, ms, flags=None, under=None, s=0.0, wall=None, mod=None, done=False, reason=None, v=None):
    n = len(ms)
    rec = {"t": t, "wallMs": 1000.0 * (t if wall is None else wall),
           "frames": {"ms": ms, "flags": flags or [0] * n, "under": under or [-2] * n, "dropped": 0},
           "drive": {"s": s, "done": done, "reason": reason, "falls": 0, "resets": 0, "rebases": 0,
                     "vSum": (v or 0) * n, "vProfSum": 40.0 * n, "vN": n}}
    if mod is not None:
        rec["mod"] = mod
    return rec


class SummaryTest(unittest.TestCase):
    def test_frames_and_classes(self):
        prof = drive.straight(40.0, 0.0, 0.08)
        ms = [40.0] * 100              # 4 s: the rebase at ~1.3 s, frame 90 at ~3.8 s
        flags = [0] * 100
        ms[10], flags[10] = 60.0, drive.F_HARNESS                 # MCP frame: left out
        ms[20], flags[20] = 62.0, drive.F_HEAVY                   # terrain commit
        ms[30], flags[30] = 15.0, drive.F_REBASE | drive.F_SKIP   # rebase phase 1
        ms[31], flags[31] = 85.0, drive.F_HEAVY                   # phase 2: part of the rebase
        ms[50] = 55.0                                             # a steady hitch
        flags[32] |= drive.F_MISS                                 # within 2 s of the rebase
        flags[90] |= drive.F_MISS
        under = [-2] * 100
        under[90] = drive.NO_TILE
        polls = [poll(1.0, ms, flags, under, s=80.0, wall=2.0, done=True, reason="end", v=40.0)]
        sm = drive.summarize(polls, prof)
        self.assertEqual(sm["frames"], 100)
        self.assertEqual(sm["harnessFrames"], 1)
        self.assertEqual(sm["commitFrameMs"], [62.0])
        self.assertEqual(sm["rebaseFrameMs"], [15.0, 85.0])
        self.assertEqual(sm["over50"], 3)                         # 62, 85, 55 (not the 60 ms MCP frame)
        self.assertEqual(sm["over50Steady"], 1)
        self.assertEqual(sm["maxMs"], 85.0)
        self.assertEqual(sm["exactMissFrames"], 2)
        self.assertEqual(sm["exactMissFramesOutsideRebase"], 1)
        self.assertEqual(sm["underLevels"], {"-2": 99, "none": 1})
        self.assertEqual(sm["effectiveSpeed"], 40.0)
        self.assertEqual(sm["physicsSpeedRatio"], 1.0)
        self.assertTrue(sm["pass"], sm["fails"])
        self.assertEqual(sm["p0"], {"speedWithin2pct": True, "noResets": True})
        n = 99
        self.assertAlmostEqual(sm["fpsMean"], round(n / ((sum(ms) - 60.0) / 1000.0), 1))

    def test_speed_rule(self):
        prof = drive.straight(90.0, 0.0, 1.0)
        ok = drive.summarize([poll(11.6, [10.0], s=1000.0, wall=11.6, done=True, reason="end")], prof)
        self.assertTrue(ok["pass"], ok["fails"])                  # 86.2 m/s = 95.8 %
        slow = drive.summarize([poll(14.8, [10.0], s=1000.0, wall=14.8, done=True, reason="end")], prof)
        self.assertFalse(slow["pass"])                            # 67.6 m/s: the "200 mph" drive
        self.assertAlmostEqual(slow["effectiveRatio"], 0.751, delta=0.001)
        self.assertIn("nominal", slow["fails"][0])
        jump = drive.summarize([poll(10.0, [10.0], s=1000.0, wall=10.0, done=True, reason="jump")], prof)
        self.assertFalse(jump["pass"])
        unfinished = drive.summarize([poll(10.0, [10.0], s=900.0, wall=10.0)], prof)
        self.assertFalse(unfinished["pass"])

    def test_mod_series(self):
        prof = drive.straight(40.0, 0.0, 1.0)
        polls = []
        for i in range(12):
            t = float(i + 1)
            anchor = 1 if i < 5 else 2
            far = {"live": 800 + i, "old": 300 if 5 <= i < 9 else 0, "deleting": 2, "queued": 10 * i,
                   "groups": i, "waitingGroups": i, "done": not (5 <= i < 7), "pops": 0, "failed": 0,
                   "materials": 811}
            gov = {"estCdaeTex": 4.0 if i < 3 else (5.0 if i < 9 else 6.0), "target": 2.0}
            mod = {"anchorId": anchor, "far": far, "governor": gov, "serverBacklog": 3 * i,
                   "player": {"speed": 39.0}, "followCount": i // 4, "lastFollowMs": 45.0,
                   "lastRebaseMs": 71.0 if anchor == 2 else None}
            polls.append(poll(t, [10.0] * 5, s=40.0 * t, wall=t, mod=mod, done=i == 11,
                              reason="end" if i == 11 else None))
        sm = drive.summarize(polls, prof)
        self.assertEqual(sm["queuedMax"], 110)
        self.assertEqual(sm["waitingGroupsMax"], 11)
        self.assertEqual(sm["liveMax"], 811)
        self.assertEqual(sm["objectsMax"], 808 + 300 + 2)
        self.assertAlmostEqual(sm["backlogSlopePerMin"], 180.0, delta=1e-6)     # 3 tiles/s
        self.assertEqual((sm["estCdaeTexFirstQ"], sm["estCdaeTexLastQ"], sm["estCdaeTexRatio"]), (4.0, 6.0, 1.5))
        self.assertEqual((sm["cells"], sm["estCdaeTexKind"]), (None, "estCdaeTex"))
        self.assertEqual(sm["estTexByKind"], {"estCdaeTex": {"firstQ": 4.0, "lastQ": 6.0, "ratio": 1.5}})
        # M1 (64-cell tiles, the mod's status().cells): the 256 px kind is the main one; every kind apart
        for p in polls:
            p["mod"]["cells"] = 64
            g = p["mod"]["governor"]
            g["estCdaeTex256"], g["estCdaeTex512"], g["estCdaeTex"] = g["estCdaeTex"] * 2, 12.0, 3.0
        sm = drive.summarize(polls, prof)
        self.assertEqual((sm["cells"], sm["estCdaeTexKind"]), (64, "estCdaeTex256"))
        self.assertEqual((sm["estCdaeTexFirstQ"], sm["estCdaeTexLastQ"], sm["estCdaeTexRatio"]), (8.0, 12.0, 1.5))
        self.assertEqual(sm["estTexByKind"]["estCdaeTex512"], {"firstQ": 12.0, "lastQ": 12.0, "ratio": 1.0})
        self.assertEqual(sm["meanPoseSpeed"], 39.0)
        self.assertEqual(sm["followCommitMs"], [45.0, 45.0])
        self.assertEqual(sm["reloads"], [{"t": 6.0, "from": 1, "to": 2, "rebaseMs": 71.0, "farDoneS": 2.0,
                                          "oldGoneS": 4.0}])
        self.assertEqual((sm["pops"], sm["failed"], sm["materials"], sm["liveEnd"]), (0, 0, 811, 811))


@unittest.skipIf(LUAJIT is None, "luajit not installed")
class DriverJsonTest(unittest.TestCase):
    def test_driver_polls_feed_the_summary(self):
        """The driver under luajit (stub engine, fallback JSON encoder): 3 s at 40 m/s with a poll
        each second, a commit frame and a poll's harness frame; summarize() reads the polls."""
        script = LUA_ENGINE % {"stubs": STUBS.as_posix(), "driver": DRIVER.as_posix()} + r"""
local D = BNGE_DRIVE
local res = D.install({ points = { { s = 0, speed = 40, heading = 90 } }, km = 0.1 })
assert(res:match('"ok":true'), res)
for sec = 1, 3 do
  for f = 1, 60 do FRAME(1000 / 60, nil, sec == 2 and f == 30) end
  print(D.poll())
end
D.uninstall()
"""
        lines = run_lua(script).strip().split("\n")
        self.assertEqual(len(lines), 3)
        polls = [drive.poll_record(line, i + 1.0) for i, line in enumerate(lines)]
        self.assertEqual([len(p["frames"]["ms"]) for p in polls], [60, 60, 60])
        self.assertEqual(polls[1]["frames"]["flags"][0], drive.F_HARNESS)
        self.assertEqual(polls[1]["frames"]["flags"][29], drive.F_HEAVY)
        self.assertEqual(polls[0]["mod"]["serverBacklog"], 5)
        sm = drive.summarize(polls, drive.straight(40.0, 90.0, 0.1))
        self.assertEqual((sm["frames"], sm["harnessFrames"], sm["commitFrames"]), (180, 3, 1))   # install + 2 polls
        self.assertEqual(sm["reason"], "end")
        self.assertAlmostEqual(sm["km"], 0.1, delta=0.001)
        self.assertEqual(sm["exactMissFrames"], 0)
        self.assertTrue(math.isclose(sm["physicsSpeedRatio"], 1.0, abs_tol=1e-6))
        # the wall time ends at the frame that finished the drive (100 m at 40 m/s: 2.5 s), not at the
        # poll that saw it (3 s)
        self.assertAlmostEqual(sm["wallS"], 2.5, delta=0.02)
        self.assertAlmostEqual(sm["effectiveSpeed"], 40.0, delta=0.4)
        self.assertTrue(sm["pass"], sm["fails"])


class FakeClock:
    """time.monotonic / time.sleep for run_ge: sleeping advances the clock; each MCP call takes `cost` s."""

    def __init__(self):
        self.t = 100.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += max(0.0, s)


class RunGeTest(unittest.TestCase):
    """drive.run_ge with a fake MCP: every path uninstalls the driver, and polls are >= 1 s apart."""

    def args(self, tmp, **kw):
        a = drive.argparse.Namespace(out=tmp, poll=1.0, shots=0.0, max_s=3600.0, exact_level=None,
                                     use_player_vehicle=True)
        vars(a).update(kw)
        return a

    def fake(self, clock, fail_at=None, done_after=5, cost=0.0):
        calls = []

        def call(code):
            calls.append((clock.monotonic(), code[:80]))
            clock.t += cost
            if fail_at is not None and fail_at in code:
                raise RuntimeError("MCP timeout")
            if "BNGE_DRIVE.install" in code:
                return json.dumps({"ok": True})
            if "spawnNewVehicle" in code:
                return json.dumps({"carId": 1, "testId": 2, "lat": 39.3, "lon": -76.5})
            if "BNGE_DRIVE_CAR" in code and "restored" in code:
                return "user vehicle 1 restored, teleport true"
            if "BNGE_DRIVE.poll" in code:
                n = sum(1 for _, c in calls if "BNGE_DRIVE.poll" in c)
                return json.dumps({"frames": {"ms": [16.7], "flags": [0], "under": [-2], "dropped": 0},
                                   "drive": {"s": 10.0 * n, "done": n >= done_after, "reason": "end" if n >= done_after else None}})
            return "ok"
        return call, calls

    def uninstalled(self, calls):
        return any("uninstall" in c for _, c in calls)

    def restored(self, calls):
        return any("BNGE_DRIVE_CAR" in c and "no test vehicle" in c for _, c in calls)

    def test_start_discards_stale_hitches_and_keeps_first_drive_hitch(self):
        stale = {"ms": 120, "mod": {"net": 100}}
        fresh = {"ms": 60, "mod": {"roads": 50}}
        pending = [stale]
        perf = {"frames": 1000, "maxMs": 4000}
        clock = FakeClock()
        call, _ = self.fake(clock, done_after=1)

        def with_hitches(code):
            if "beamngEarth_core.hitches()" in code:
                self.assertIn("BNGE_DRIVE.install", code)
                self.assertLess(code.index("beamngEarth_core.hitches()"), code.index("BNGE_DRIVE.install"))
                pending.clear()
            if "beamngEarth_perfmon.snapshot()" in code:
                self.assertLess(code.index("beamngEarth_perfmon.snapshot()"), code.index("BNGE_DRIVE.install"))
                perf.clear()
            if "return BNGE_DRIVE.install" in code:
                pending.append(fresh)  # the first drive frame after activation
                perf.update({"frames": 1, "maxMs": 16.7})
            result = call(code)
            if "return BNGE_DRIVE.poll()" in code:
                row = json.loads(result)
                row["hitches"] = list(pending)
                row["perf"] = dict(perf)
                pending.clear()
                return json.dumps(row)
            return result

        with tempfile.TemporaryDirectory() as tmp:
            polls = drive.run_ge(self.args(tmp), drive.straight(10.0, 0.0, 0.01), with_hitches, clock)
        self.assertEqual(polls[0]["hitches"], [fresh])
        self.assertEqual(polls[0]["perf"], {"frames": 1, "maxMs": 16.7})
        summary = drive.summarize(polls, drive.straight(10.0, 0.0, 0.01))
        self.assertEqual(summary["hitch_causes"], {"mod.roads": [1, 60]})

    def test_test_vehicle_always_restored(self):
        """The drive uses its own stock test car; the user's vehicle comes back on every path,
        after the driver is uninstalled (in-game 2026-10-10: the user's own Hopper went unstable)."""
        for fail_at in (None, "BNGE_DRIVE.install", "BNGE_DRIVE.poll", "GE-side per-frame"):
            with self.subTest(fail_at=fail_at), tempfile.TemporaryDirectory() as tmp:
                clock = FakeClock()
                call, _ = self.fake(clock, fail_at=fail_at)
                codes = []

                def rec(code):
                    codes.append(code)
                    return call(code)
                try:
                    drive.run_ge(self.args(tmp, use_player_vehicle=False), drive.straight(40.0, 0.0, 1.0), rec, clock)
                except RuntimeError:
                    self.assertIsNotNone(fail_at)
                self.assertIn("spawnNewVehicle", codes[0])
                self.assertIn("setActive(0)", codes[0])     # the user's vehicle is frozen meanwhile
                ir = [i for i, c in enumerate(codes) if "BNGE_DRIVE_CAR" in c and "no test vehicle" in c]
                self.assertEqual(len(ir), 1, "restored exactly once")
                iu = max(i for i, c in enumerate(codes) if "uninstall()" in c)
                self.assertLess(iu, ir[0], "restore after the driver is gone")

    def test_poll_spacing_and_uninstall(self):
        for cost in (0.0, 0.3, 1.7):        # a slow MCP call must not make the next poll come early
            with self.subTest(cost=cost), tempfile.TemporaryDirectory() as tmp:
                clock = FakeClock()
                call, calls = self.fake(clock, cost=cost)
                polls = drive.run_ge(self.args(tmp), drive.straight(40.0, 0.0, 1.0), call, clock)
                self.assertEqual(len(polls), 5)
                starts = [t for t, c in calls if "BNGE_DRIVE.poll" in c]
                self.assertTrue(all(b - a >= drive.MIN_POLL_S - 1e-9 for a, b in zip(starts, starts[1:])), starts)
                self.assertTrue(self.uninstalled(calls))
                self.assertTrue((Path(tmp) / "samples.jsonl").exists())

    def test_every_failure_uninstalls(self):
        for fail_at in ("BNGE_DRIVE.install", "BNGE_DRIVE.poll", "extensions.load", "GE-side per-frame"):
            with self.subTest(fail_at=fail_at), tempfile.TemporaryDirectory() as tmp:
                clock = FakeClock()
                call, calls = self.fake(clock, fail_at=fail_at)
                with self.assertRaises(RuntimeError):
                    drive.run_ge(self.args(tmp), drive.straight(40.0, 0.0, 1.0), call, clock)
                self.assertTrue(self.uninstalled(calls), calls)
        with tempfile.TemporaryDirectory() as tmp:          # the driver refused the install
            clock = FakeClock()
            call, calls = self.fake(clock)

            def refuse(code):
                return json.dumps({"error": "no player vehicle"}) if "BNGE_DRIVE.install" in code else call(code)
            with self.assertRaises(SystemExit):
                drive.run_ge(self.args(tmp), drive.straight(40.0, 0.0, 1.0), refuse, clock)
            self.assertTrue(self.uninstalled(calls))


if __name__ == "__main__":
    unittest.main()
