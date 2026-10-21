"""tools/autodrive_perf.py: the crash / stop / off-route checks of a recorded drive."""
import os
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import autodrive_perf as ad  # noqa: E402


def poll(t, v=10.0, damage=0.0, off=1.0, rem=2000.0, planning=False):
    return {"t": t, "x": t * v, "y": 0.0, "v": v, "damage": damage, "lat": 39.0, "lon": -76.0,
            "gps": {"remaining": rem, "off": off, "arrows": "r3", "planning": planning}}


class ChecksTest(unittest.TestCase):
    def run_polls(self, polls):
        shots = []
        c = ad.Checks(lambda n, ev: shots.append(ev["kind"]) or f"event_{n}.jpg")
        for p in polls:
            c.feed(p)
        return c.events, shots

    def test_a_crash_is_a_damage_jump(self):
        ev, shots = self.run_polls([poll(0), poll(1, damage=200), poll(2, v=0.1, damage=9000)])
        self.assertEqual([e["kind"] for e in ev], ["crash"])
        self.assertEqual(ev[0]["mph_before"], round(10 * 2.23694, 1))
        self.assertEqual(shots, ["crash"])

    def test_a_scrape_is_not_a_crash(self):
        ev, _ = self.run_polls([poll(0), poll(1, damage=300), poll(2, damage=700)])
        self.assertEqual(ev, [])

    def test_a_stop_after_standing_long_once(self):
        polls = [poll(0)] + [poll(t, v=0.0) for t in range(1, 120)]
        ev, _ = self.run_polls(polls)
        self.assertEqual([e["kind"] for e in ev], ["stop"], "once, not every poll after")
        self.assertGreaterEqual(ev[0]["seconds"], ad.STOP_S)

    def test_a_red_light_planning_or_arrival_is_no_stop(self):
        ev, _ = self.run_polls([poll(t, v=0.0) for t in range(30)] + [poll(31)])
        self.assertEqual(ev, [], "30 s at a light")
        ev, _ = self.run_polls([poll(t, v=0.0, planning=True) for t in range(100)])
        self.assertEqual(ev, [], "waiting for the route")
        ev, _ = self.run_polls([poll(t, v=0.0, rem=20.0) for t in range(100)])
        self.assertEqual(ev, [], "arrived")

    def test_leaving_the_route_once_per_excursion(self):
        ev, _ = self.run_polls([poll(0), poll(1, off=40), poll(2, off=45), poll(3, off=2), poll(4, off=30)])
        self.assertEqual([e["kind"] for e in ev], ["off_route", "off_route"])


class HitchTest(unittest.TestCase):
    def test_activation_discards_setup_hitches_before_first_poll(self):
        pending = [{"ms": 120, "mod": {"net": 100}}]
        fresh = {"ms": 60, "mod": {"roads": 50}}
        clock, polls = [0.0], [0]
        perf_frames = [1000]

        def lua(code):
            if "uiGpsAutodrive(true)" in code:
                self.assertIn("beamngEarth_core.hitches()", code)
                self.assertLess(code.index("beamngEarth_core.hitches()"), code.index("uiGpsAutodrive(true)"))
                self.assertIn("beamngEarth_perfmon.snapshot()", code)
                self.assertLess(code.index("beamngEarth_perfmon.snapshot()"), code.index("uiGpsAutodrive(true)"))
                pending.clear()
                perf_frames[0] = 0
            if code == ad.LUA_POLL:
                polls[0] += 1
                row = poll(float(polls[0]))
                perf_frames[0] += 60
                row["perf"] = {"frames": perf_frames[0], "avgMs": 16.7, "maxMs": 16.7}
                perf_frames[0] = 0
                if polls[0] == 2:
                    pending.append(fresh)
                row["hitches"] = list(pending)
                pending.clear()
                return json.dumps(row)
            return "ok"

        def sleep(seconds):
            clock[0] += seconds

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(sys, "argv", ["autodrive_perf.py", "--to", "49,12", "--out", tmp,
                                            "--minutes", "0.05", "--target-kmh", "200"]), \
                    patch.object(ad, "lua", side_effect=lua), patch.object(ad.mcp, "quiet_ui"), \
                    patch.object(ad, "ProcSampler", return_value=Mock(sample=Mock(return_value={}))), \
                    patch.object(ad.time, "time", side_effect=lambda: clock[0]), \
                    patch.object(ad.time, "sleep", side_effect=sleep), patch("sys.stdout", new_callable=io.StringIO):
                ad.main()
            with open(os.path.join(tmp, "samples.jsonl")) as f:
                rows = [json.loads(line) for line in f]
            with open(os.path.join(tmp, "summary.json")) as f:
                summary = json.load(f)
        self.assertEqual(rows[0]["hitches"], [])
        self.assertEqual(rows[0]["perf"]["frames"], 60)
        self.assertEqual(rows[1]["hitches"], [fresh])
        self.assertEqual(summary["hitch_causes"], {"mod.roads": [1, 60]})
        self.assertEqual(summary["targetKmh"], 200.0)

    def test_causes(self):
        self.assertEqual(ad.hitch_cause({"ms": 80, "mod": {"roads": 60, "far": 3}, "engine": {"render": 10}}), "mod.roads")
        self.assertEqual(ad.hitch_cause({"ms": 80, "mod": {"roads": 5}, "engine": {"render": 60, "ui": 2}}),
                         "engine.render")
        rows = [{"hitches": [{"ms": 80, "mod": {"roads": 60}}, {"ms": 120, "mod": {"roads": 90}}]},
                {"hitches": {}}, {"hitches": [{"ms": 70, "engine": {"gfx": 50}}]}]
        self.assertEqual(ad._causes(rows), {"mod.roads": [2, 120], "engine.gfx": [1, 70]})


class TargetSpeedTest(unittest.TestCase):
    def test_speed_options_cannot_override_each_other(self):
        with patch.object(sys, "argv", ["autodrive_perf.py", "--to", "49,12", "--out", "unused",
                                        "--target-kmh", "200", "--ai-mph", "100"]), \
                patch.object(ad, "lua") as lua, patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as error:
                ad.main()
        self.assertEqual(error.exception.code, 2)
        lua.assert_not_called()

    def test_delayed_mph_switch_retains_telemetry_and_cleanup(self):
        clock, calls, polls = [0.0], [], [0]

        def lua(code):
            calls.append(code)
            if code == ad.ROAD_LIMIT_LUA:
                return str(100 / 3.6)
            if code == ad.LUA_POLL:
                polls[0] += 1
                row = poll(float(polls[0]))
                row["perf"] = {"frames": 60, "avgMs": 16.7, "maxMs": 16.7}
                row["gps"]["autodrive"] = polls[0] < 3
                row["lead"] = {"road": 150, "arrived": 500}
                return json.dumps(row)
            return "ok"

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(sys, "argv", ["autodrive_perf.py", "--to", "49,12", "--out", tmp,
                                            "--minutes", "0.1", "--ai-mph", "100", "--fast-after", "1"]), \
                    patch.object(ad, "lua", side_effect=lua), patch.object(ad.mcp, "quiet_ui"), \
                    patch.object(ad, "ProcSampler", return_value=Mock(sample=Mock(return_value={}))), \
                    patch.object(ad.time, "time", side_effect=lambda: clock[0]), \
                    patch.object(ad.time, "sleep", side_effect=lambda s: clock.__setitem__(0, clock[0] + s)), \
                    patch("sys.stdout", new_callable=io.StringIO):
                ad.main()
            with open(os.path.join(tmp, "summary.json")) as f:
                summary = json.load(f)
        self.assertIn("speedOverride = nil", calls[0])
        switches = [code for code in calls if 'ai.setSpeedMode' in code]
        self.assertEqual(len(switches), 1)
        self.assertIn(repr(100 * 0.44704), switches[0])
        self.assertIn("speedOverride = nil", calls[-1])
        self.assertIn("uiGpsAutodrive(false)", calls[-1])
        self.assertEqual(summary["endReason"], "autodrive_ended")
        self.assertEqual(summary["lead"]["road_median_m"], 150)
        self.assertEqual(summary["lead"]["road_under_200m_s"], 2)

    def test_invalid_target_is_rejected_before_game_calls(self):
        for value in ("0", "-1", "nan", "inf", "-inf", "bad"):
            with self.subTest(value=value), \
                    patch.object(sys, "argv", ["autodrive_perf.py", "--to", "49,12", "--out", "unused",
                                                "--target-kmh=" + value]), \
                    patch.object(ad, "lua") as lua, patch.object(ad.mcp, "quiet_ui") as quiet, \
                    patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as error:
                    ad.main()
                self.assertEqual(error.exception.code, 2)
                lua.assert_not_called()
                quiet.assert_not_called()

    def test_legal_default_does_not_install_an_override(self):
        with patch.object(ad, "lua") as lua:
            with ad.speed_override(None):
                pass
        lua.assert_not_called()

    def test_override_restored_if_activation_raises(self):
        active, calls = [False], []

        def lua(code):
            calls.append(code)
            if "speed override installed" in code:
                self.assertIn(repr(200 / 3.6), code)
                active[0] = True
            elif code == ad.RESTORE_SPEED_LUA:
                active[0] = False
            elif "uiGpsAutodrive(true)" in code:
                self.assertTrue(active[0])
                raise RuntimeError("activation failed")
            return "ok"

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(sys, "argv", ["autodrive_perf.py", "--to", "49,12", "--out", tmp,
                                            "--target-kmh", "200"]), patch.object(ad, "lua", side_effect=lua), \
                    patch.object(ad.mcp, "quiet_ui"), patch.object(ad, "ProcSampler"):
                with self.assertRaisesRegex(RuntimeError, "activation failed"):
                    ad.main()
        self.assertFalse(active[0])
        self.assertEqual(calls[-1], ad.RESTORE_SPEED_LUA)

    def test_restore_attempted_if_install_reply_is_lost(self):
        with patch.object(ad, "lua", side_effect=[SystemExit("lost reply"), "restored"]) as lua:
            with self.assertRaisesRegex(SystemExit, "lost reply"):
                with ad.speed_override(200):
                    self.fail("a failed installation must not start the run")
        self.assertEqual(lua.call_args_list[-1].args[0], ad.RESTORE_SPEED_LUA)

    @unittest.skipUnless(shutil.which("luajit"), "LuaJIT not on PATH")
    def test_lua_override_changes_every_route_and_restores_original(self):
        script = r'''
local gps = {}
gps.aiCommand = function(ids)
  if not ids then return 'ai.setMode("stop")' end
  return 'ai.driveUsingPath({routeSpeedMode = "legal", path = {' .. ids .. '}})'
end
local original = gps.aiCommand
package.loaded['beamngEarth/gps'] = gps
local install = assert(loadstring(__INSTALL__))
assert(install() == 'speed override installed')
for _, ids in ipairs({'one', 'replanned'}) do
  local command, extra = gps.aiCommand(ids)
  assert(extra == nil, 'gsub replacement count leaked into caller')
  assert(command:find('routeSpeedMode = "set"', 1, true))
  assert(command:find('routeSpeed = 55.555', 1, true))
  assert(command:find(ids, 1, true))
end
assert(gps.aiCommand(nil) == 'ai.setMode("stop")')
assert(install() == 'speed override installed') -- repeat run must not stack wrappers
assert(loadstring(__RESTORE__))()
assert(gps.aiCommand == original and BNGE_AUTODRIVE_SPEED == nil)
'''
        install = ad.TARGET_SPEED_LUA.replace("__TARGET_MPS__", repr(200 / 3.6))
        script = script.replace("__INSTALL__", "[==[" + install + "]==]")
        script = script.replace("__RESTORE__", "[==[" + ad.RESTORE_SPEED_LUA + "]==]")
        result = subprocess.run([shutil.which("luajit"), "-"], input=script, text=True,
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


class RunTerminationTest(unittest.TestCase):
    def test_cleared_or_disabled_gps_ends_capture_before_later_coasting(self):
        for ended in (None, False):
            with self.subTest(ended=ended), tempfile.TemporaryDirectory() as tmp:
                clock, polls = [0.0], [0]

                def lua(code):
                    if code != ad.LUA_POLL:
                        return "ok"
                    polls[0] += 1
                    row = poll(float(polls[0]), v=55.55, damage=9000 if polls[0] > 2 else 0)
                    row["perf"] = {"frames": 60, "avgMs": 16.7, "maxMs": 16.7}
                    row["gps"]["autodrive"] = True if polls[0] == 1 else ended
                    if ended is None and polls[0] > 1:
                        row["gps"] = {}  # GPS arrival removes the route and its fields.
                    return json.dumps(row)

                def sleep(seconds):
                    clock[0] += seconds

                with patch.object(sys, "argv", ["autodrive_perf.py", "--to", "49,12", "--out", tmp,
                                                "--minutes", "0.05"]), patch.object(ad, "lua", side_effect=lua), \
                        patch.object(ad.mcp, "quiet_ui"), patch.object(ad, "ProcSampler"), \
                        patch.object(ad.time, "time", side_effect=lambda: clock[0]), \
                        patch.object(ad.time, "sleep", side_effect=sleep), patch("sys.stdout", new_callable=io.StringIO):
                    ad.ProcSampler.return_value.sample.return_value = {}
                    ad.main()
                with open(os.path.join(tmp, "summary.json")) as f:
                    summary = json.load(f)
                self.assertEqual(polls[0], 2)
                self.assertEqual(summary["endReason"], "autodrive_ended")
                self.assertEqual(summary["events"], [])


class LifecycleTest(unittest.TestCase):
    def test_ai_stops_and_event_file_closes_on_setup_recording_and_file_failures(self):
        import builtins
        from types import SimpleNamespace

        for stage in (None, "speed_setup", "activation", "recorder_start", "events.jsonl", "samples.jsonl",
                      "recorder_stop", "disable"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                calls, event_files = [], []

                def lua(code):
                    calls.append(code)
                    if stage == "speed_setup" and "speedOverride" in code and "uiGpsAutodrive(false)" not in code:
                        raise RuntimeError(stage)
                    if stage == "activation" and "uiGpsAutodrive(true)" in code:
                        raise RuntimeError(stage)
                    if stage == "disable" and "uiGpsAutodrive(false)" in code:
                        raise RuntimeError(stage)
                    return "ok"

                def open_file(path, *args, **kwargs):
                    if stage in ("events.jsonl", "samples.jsonl") and os.path.basename(path) == stage:
                        raise RuntimeError(stage)
                    f = builtins.open(path, *args, **kwargs)
                    if os.path.basename(path) == "events.jsonl":
                        event_files.append(f)
                    return f

                recorder = Mock()
                recorder.stop.return_value = None
                argv = ["autodrive_perf.py", "--to", "49,12", "--out", tmp, "--minutes", "0"]
                if stage in ("recorder_start", "recorder_stop"):
                    argv += ["--obs", "--rec"]
                with patch.object(sys, "argv", argv), patch.object(ad, "lua", side_effect=lua), \
                        patch.object(ad.mcp, "quiet_ui"), patch.object(ad, "ProcSampler"), \
                        patch.object(ad, "open", side_effect=open_file, create=True), \
                        patch.object(ad, "obs_start", side_effect=RuntimeError(stage) if stage == "recorder_start" else None,
                                     return_value=True), \
                        patch.object(ad, "obs_stop", side_effect=RuntimeError(stage) if stage == "recorder_stop" else None), \
                        patch.dict(sys.modules, {"gamerec": SimpleNamespace(start=Mock(return_value=recorder))}), \
                        patch("sys.stdout", new_callable=io.StringIO):
                    if stage is not None:
                        with self.assertRaisesRegex(RuntimeError, stage):
                            ad.main()
                    else:
                        ad.main()
                self.assertTrue(any("uiGpsAutodrive(false)" in code for code in calls))
                self.assertTrue(all(f.closed for f in event_files))
                if stage == "recorder_stop":
                    recorder.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
