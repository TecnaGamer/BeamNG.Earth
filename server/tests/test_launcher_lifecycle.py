"""Bounded launcher lifecycle tests; no game launch or live process termination."""
import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import launcher
import bmplaunch


class Clock:
    now = 0.0

    def sleep(self, seconds):
        self.now += seconds
        if self.now > 1000:
            raise AssertionError("unbounded launcher wait")


class GameLifecycleTest(unittest.TestCase):
    def follow(self, windows=True, alive=None, dialog=None, server_exit=None, proc=None):
        clock = Clock()
        server = mock.Mock()
        server.poll.side_effect = lambda: 1 if server_exit is not None and clock.now >= server_exit else None
        with mock.patch.object(launcher, "IS_WIN", windows), \
                mock.patch.object(launcher.time, "monotonic", side_effect=lambda: clock.now), \
                mock.patch.object(launcher.time, "sleep", side_effect=clock.sleep), \
                mock.patch.object(launcher, "game_alive", side_effect=lambda: bool(alive and alive(clock.now))), \
                mock.patch.object(launcher, "game_launcher_running", side_effect=lambda: bool(dialog and dialog(clock.now))), \
                mock.patch.object(launcher, "say"):
            result = launcher.wait_for_game(proc, server)
        return result, clock.now

    def test_windows_closes_promptly_after_game_exit(self):
        result, elapsed = self.follow(alive=lambda t: t < 1)
        self.assertTrue(result)
        self.assertEqual(elapsed, 1 + launcher.WINDOWS_GAME_GONE_S)

    def test_linux_preserves_restart_grace(self):
        result, elapsed = self.follow(windows=False, alive=lambda t: t < 3 or 9 <= t < 12)
        self.assertTrue(result)
        self.assertGreaterEqual(elapsed, 12 + launcher.GAME_GONE_S)

    def test_start_without_game_or_dialog_is_bounded(self):
        result, elapsed = self.follow()
        self.assertFalse(result)
        self.assertEqual(elapsed, launcher.GAME_START_S)

    def test_cancelled_launch_dialog_stops_services(self):
        result, elapsed = self.follow(dialog=lambda t: t < 5)
        self.assertFalse(result)
        self.assertEqual(elapsed, 5 + launcher.WINDOWS_GAME_GONE_S)

    def test_open_dialog_may_outlive_startup_budget(self):
        result, elapsed = self.follow(dialog=lambda t: t < 210, alive=lambda t: 210 <= t < 215)
        self.assertTrue(result)
        self.assertEqual(elapsed, 215 + launcher.WINDOWS_GAME_GONE_S)

    def test_server_exit_interrupts_startup_and_active_game_wait(self):
        for alive in (None, lambda t: True):
            with self.subTest(alive=bool(alive)):
                result, elapsed = self.follow(alive=alive, server_exit=2)
                self.assertFalse(result)
                self.assertEqual(elapsed, 2)

    def test_lingering_windows_bootstrap_does_not_extend_actual_game_lifetime(self):
        proc = mock.Mock()
        proc.poll.return_value = None
        result, elapsed = self.follow(alive=lambda t: t < 1, proc=proc)
        self.assertTrue(result)
        self.assertEqual(elapsed, 1 + launcher.WINDOWS_GAME_GONE_S)
        proc.wait.assert_not_called()


class WindowsGameHelpersTest(unittest.TestCase):
    def query(self, entries, pids=(10, 11), exception=None):
        result = mock.Mock(returncode=0, stdout=json.dumps(entries).encode())
        with mock.patch.object(launcher.subprocess, "run", return_value=result, side_effect=exception) as run:
            got = launcher.windows_game_main_pids(list(pids))
        self.assertLessEqual(run.call_args.kwargs["timeout"], 3)
        return got

    def test_filters_only_explicit_cef_helpers(self):
        rows = [{"ProcessId": 10, "CommandLine": '"C:\\Game\\BeamNG.drive.x64.exe" -gfx d3d12'},
                {"ProcessId": 11, "CommandLine": 'BeamNG.drive.x64.exe --type=renderer'}]
        self.assertEqual(self.query(rows), [10])
        self.assertEqual(self.query(rows[1], pids=(11,)), [])

    def test_missing_command_line_or_failed_query_retains_candidates(self):
        self.assertEqual(self.query([{"ProcessId": 10, "CommandLine": None}]), [10, 11])
        self.assertEqual(self.query(None), [10, 11])
        self.assertEqual(self.query(None, exception=subprocess.TimeoutExpired("powershell", 3)), [10, 11])


class ServerReadinessTest(unittest.TestCase):
    def test_waits_for_listener_and_fails_when_child_exits(self):
        for dies in (False, True):
            clock = Clock()
            proc = mock.Mock()
            proc.poll.side_effect = lambda: 2 if dies and clock.now >= 0.5 else None
            with mock.patch.object(launcher.time, "monotonic", side_effect=lambda: clock.now), \
                    mock.patch.object(launcher.time, "sleep", side_effect=clock.sleep), \
                    mock.patch.object(launcher, "server_port_busy", side_effect=lambda port: clock.now >= 1) as probe:
                self.assertEqual(launcher.wait_server_ready(proc, 47699, timeout=2), not dies)
            self.assertLessEqual(clock.now, 1)
            probe.assert_called_with(47699)

    def test_missing_listener_times_out(self):
        clock = Clock()
        proc = mock.Mock()
        proc.poll.return_value = None
        with mock.patch.object(launcher.time, "monotonic", side_effect=lambda: clock.now), \
                mock.patch.object(launcher.time, "sleep", side_effect=clock.sleep), \
                mock.patch.object(launcher, "server_port_busy", return_value=False):
            self.assertFalse(launcher.wait_server_ready(proc, timeout=1))
        self.assertEqual(clock.now, 1)


class BeammpStartupTest(unittest.TestCase):
    def run_startup(self, ready=True, failure=None, timeout=30):
        proc = mock.Mock()
        args = argparse.Namespace(no_beammp=False, beammp_wait=timeout)
        with ExitStack() as stack:
            for owner, name, value in ((launcher, "IS_WIN", True),):
                stack.enter_context(mock.patch.object(owner, name, value))
            for owner, name, value in ((launcher, "under_wine", False), (launcher, "data_dir", "test-data"),
                                       (launcher.apppaths, "saved_game_dir", None), (bmplaunch, "running", False),
                                       (bmplaunch, "on_launch", True), (bmplaunch, "find", "test/BeamMP-Launcher.exe"),
                                       (bmplaunch, "start", proc)):
                stack.enter_context(mock.patch.object(owner, name, return_value=value))
            wait = stack.enter_context(mock.patch.object(bmplaunch, "wait_ready", return_value=ready, side_effect=failure))
            stop = stack.enter_context(mock.patch.object(bmplaunch, "stop"))
            stack.enter_context(mock.patch.object(launcher, "row"))
            if failure:
                with self.assertRaises(type(failure)):
                    launcher.beammp_up("game-user", "native", args)
            else:
                self.assertIsNone(launcher.beammp_up("game-user", "native", args))
            wait.assert_called_once_with(proc, timeout=timeout)
            stop.assert_called_once_with(proc)

    def test_timeout_falls_back_and_stops_only_owned_process(self):
        self.run_startup(ready=False, timeout=7)

    def test_interrupted_or_failed_wait_cleans_up_owned_process(self):
        for failure in (KeyboardInterrupt(), OSError("readiness failed")):
            with self.subTest(failure=type(failure).__name__):
                self.run_startup(failure=failure)

    def test_wait_setting_is_finite_and_bounded(self):
        for bad in ("0", "-1", "nan", "inf", "601", "bad"):
            with self.assertRaises(argparse.ArgumentTypeError):
                launcher.beammp_wait_seconds(bad)
        self.assertEqual(launcher.beammp_wait_seconds("12.5"), 12.5)


if __name__ == "__main__":
    unittest.main()
