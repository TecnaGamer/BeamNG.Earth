"""Bounded launcher lifecycle tests; no game launch or live process termination."""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
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

    def test_slow_steam_start_keeps_the_server_waiting(self):
        # (no game yet after GAME_START_S: said once, and the server keeps waiting for it, 2026-10-24)
        for windows in (True, False):
            with self.subTest(windows=windows):
                result, elapsed = self.follow(windows=windows, alive=lambda t: 400 <= t < 405)
                self.assertTrue(result)
                self.assertGreaterEqual(elapsed, 405)

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
    def test_os_selected_port_accepts_only_a_valid_bound_port(self):
        import tempfile
        proc = mock.Mock()
        proc.poll.return_value = None
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, "server.log")
            path.write_text("earthd listening on 127.0.0.1:49123 session fixture\n")
            self.assertTrue(launcher.wait_server_ready(proc, path, port=0, timeout=1))

    def test_waits_for_listener_and_fails_when_child_exits(self):
        for dies in (False, True):
            clock = Clock()
            proc = mock.Mock()
            proc.poll.side_effect = lambda: 2 if dies and clock.now >= 0.5 else None
            with tempfile.TemporaryDirectory() as folder:
                log = Path(folder) / "server.log"
                log.write_text("starting\n")
                def advance(seconds):
                    clock.sleep(seconds)
                    if clock.now >= 1:
                        with log.open("a") as f:
                            f.write("earthd listening on 127.0.0.1:47699 session test -> cache\n")
                with mock.patch.object(launcher.time, "monotonic", side_effect=lambda: clock.now), \
                        mock.patch.object(launcher.time, "sleep", side_effect=advance), \
                        mock.patch.object(launcher, "server_port_busy") as probe:
                    self.assertEqual(launcher.wait_server_ready(proc, log, 47699, timeout=2), not dies)
                    probe.assert_not_called()
            self.assertLessEqual(clock.now, 1)

    def test_missing_or_wrong_port_marker_times_out(self):
        for contents in (None, "starting\n", "earthd listening on 127.0.0.1:476130 session wrong\n"):
            clock = Clock()
            proc = mock.Mock()
            proc.poll.return_value = None
            with tempfile.TemporaryDirectory() as folder:
                log = Path(folder) / "server.log"
                if contents is not None:
                    log.write_text(contents)
                with mock.patch.object(launcher.time, "monotonic", side_effect=lambda: clock.now), \
                        mock.patch.object(launcher.time, "sleep", side_effect=clock.sleep), \
                        mock.patch.object(launcher, "server_port_busy") as probe:
                    self.assertFalse(launcher.wait_server_ready(proc, log, timeout=1))
                    probe.assert_not_called()
            self.assertEqual(clock.now, 1)

    def test_marker_at_end_of_large_log_and_dead_child(self):
        proc = mock.Mock()
        proc.poll.return_value = None
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "server.log"
            log.write_bytes(b"startup data\n" * 200000 + b"earthd listening on 127.0.0.1:47613 session ready\n")
            real_open = open
            reads = []
            def tracked_open(*args, **kwargs):
                stream = real_open(*args, **kwargs)
                original_read = stream.read
                def read(size=-1):
                    reads.append(size)
                    return original_read(size)
                stream.read = read
                return stream
            with mock.patch("builtins.open", side_effect=tracked_open):
                self.assertTrue(launcher.wait_server_ready(proc, log, timeout=1))
            self.assertEqual(reads, [65536])
            proc.poll.return_value = 1
            self.assertFalse(launcher.wait_server_ready(proc, log, timeout=1))

    def test_new_child_truncates_old_marker_before_waiting(self):
        clock = Clock()
        proc = mock.Mock()
        proc.poll.return_value = None
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "server.log"
            log.write_text("earthd listening on 127.0.0.1:47613 session old\n")
            with mock.patch.object(launcher.subprocess, "Popen", return_value=proc), \
                    mock.patch.object(launcher.threading, "Thread"):
                launcher.start_server("game-user", [], str(log))
            self.assertEqual(log.read_bytes(), b"")
            with mock.patch.object(launcher.time, "monotonic", side_effect=lambda: clock.now), \
                    mock.patch.object(launcher.time, "sleep", side_effect=clock.sleep):
                self.assertFalse(launcher.wait_server_ready(proc, log, timeout=1))
        self.assertEqual(clock.now, 1)

    def test_ground_grid_reaches_the_server(self):
        # the settings page's ground grid (the Low / Medium presets: 2 m, GitHub #943) as BNGE_GROUND_SQUARE
        for grid, want in (("2", "2"), ("1", None), (None, None)):
            with self.subTest(grid=grid), tempfile.TemporaryDirectory() as folder:
                os.makedirs(os.path.join(folder, "settings"))
                with open(os.path.join(folder, "settings", "beamngEarth.json"), "w") as f:
                    json.dump({} if grid is None else {"groundGrid": grid}, f)
                proc = mock.Mock()
                with mock.patch.object(launcher.subprocess, "Popen", return_value=proc) as popen, \
                        mock.patch.object(launcher.threading, "Thread"), \
                        mock.patch.dict(launcher.os.environ, {}, clear=False) as env:
                    env.pop("BNGE_GROUND_SQUARE", None)
                    launcher.start_server(folder, [], os.path.join(folder, "server.log"))
                self.assertEqual(popen.call_args.kwargs["env"].get("BNGE_GROUND_SQUARE"), want)


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



class KeepPreviousLogTest(unittest.TestCase):
    """GitHub #687: the next start overwrote a crashed session's server.log."""

    def test_rotates_two(self):
        import importlib
        import os
        import tempfile
        launcher = importlib.import_module("launcher")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "server.log")
            for run in ("a", "b", "c"):
                launcher.keep_previous_log(p)
                with open(p, "w") as f:
                    f.write(run)
            got = {n: open(os.path.join(d, n)).read() for n in os.listdir(d)}
            self.assertEqual(got, {"server.log": "c", "server.log.1": "b", "server.log.2": "a"})

    def test_empty_log_not_kept(self):
        import importlib
        import os
        import tempfile
        launcher = importlib.import_module("launcher")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "server.log")
            open(p, "w").close()
            launcher.keep_previous_log(p)
            self.assertEqual(os.listdir(d), ["server.log"])


    def test_stale_level_flag_cleared(self):
        # (a game killed on our map leaves the engine patches' flag behind; a fresh start removes it)
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "temp"))
            flag = os.path.join(d, "temp", "beamngearth_gcpatch_on")
            open(flag, "w").write("1")
            launcher.clear_level_flag(d)
            self.assertFalse(os.path.exists(flag))
            launcher.clear_level_flag(d)      # (none there: quiet)
            launcher.clear_level_flag(None)


class MemoryNoticeTest(unittest.TestCase):
    """The start's low-memory notice (8 GB / 4 GB machines: server/budget.py memory_notice): set apart, wrapped,
    never stopping the start; nothing on a machine with enough."""

    def lines(self, notice=None, gb=None):
        out = []
        env = {"BNGE_TOTAL_GB": str(gb)} if gb is not None else {}
        with mock.patch.object(launcher, "_out", side_effect=out.append), mock.patch.dict(os.environ, env):
            shown = launcher.memory_notice(notice)
        return shown, out

    def test_emergency(self):
        shown, out = self.lines(gb=4.1)
        self.assertTrue(shown)
        self.assertIn("Not enough memory:", "\n".join(out))
        self.assertTrue(all(len(line) <= 80 for line in out), out)

    def test_lean(self):
        shown, out = self.lines(gb=8.3)
        self.assertTrue(shown)
        self.assertIn("Low memory:", "\n".join(out))

    def test_enough(self):
        self.assertEqual(self.lines(gb=32.0), (False, []))

    def test_given(self):
        shown, out = self.lines(("Head", "text " * 40))
        self.assertTrue(shown and out[2] == "  Head:")


if __name__ == "__main__":
    unittest.main()


class FakeSteamLog:
    """SteamLog stand-in: lines that appear at given times (clock.now)."""

    def __init__(self, clock, timed):
        self.clock, self.timed = clock, sorted(timed)

    def read(self):
        out = [line for t, line in self.timed if t <= self.clock.now]
        self.timed = [(t, line) for t, line in self.timed if t > self.clock.now]
        return out


def steam_line(text):
    return "[2026-11-12 16:34:25] GameAction [AppID 284160, ActionID 3] : " + text


class SteamHandoffTest(unittest.TestCase):
    """The Windows start through Steam is watched (a player's dev 401 start stopped at "starting through Steam" with no
    game, four times, Discord 2026-11-12): Steam silent -> asked again -> the game started directly; Steam working on the
    start -> never overtaken."""

    def handoff(self, alive_at=None, lines=(), steam_up=True, direct_proc="proc"):
        clock = Clock()
        calls, said = [], []
        result = launcher.steam_handoff(
            ask=lambda: calls.append(("ask", clock.now)), retry=lambda: calls.append(("retry", clock.now)),
            direct=lambda: calls.append(("direct", clock.now)) or direct_proc,
            alive=lambda: alive_at is not None and clock.now >= alive_at,
            steam_log=FakeSteamLog(clock, lines), steam_up=lambda: steam_up,
            clock=lambda: clock.now, sleep=clock.sleep, report=said.append)
        return result, calls, said, clock.now

    def test_game_appears(self):
        (how, proc), calls, said, _ = self.handoff(alive_at=4)
        self.assertEqual((how, proc), ("started", None))
        self.assertEqual([c[0] for c in calls], ["ask"])
        self.assertEqual(said, [])

    def test_silent_steam_is_asked_again_then_the_game_starts_directly(self):
        with mock.patch.object(launcher, "say"):
            (how, proc), calls, said, now = self.handoff()
        self.assertEqual((how, proc), ("direct", "proc"))
        self.assertEqual([c[0] for c in calls], ["ask", "retry", "direct"])
        self.assertAlmostEqual(calls[1][1], launcher.STEAM_RETRY_S, delta=0.6)
        self.assertAlmostEqual(calls[2][1], launcher.STEAM_DIRECT_S, delta=0.6)
        self.assertTrue(any("directly" in s for s in said))

    def test_the_retry_works(self):
        (how, _), calls, _, _ = self.handoff(alive_at=launcher.STEAM_RETRY_S + 3)
        self.assertEqual(how, "started")
        self.assertEqual([c[0] for c in calls], ["ask", "retry"])

    def test_steam_starting_itself_gets_longer(self):
        with mock.patch.object(launcher, "say"):
            (how, _), calls, _, _ = self.handoff(steam_up=False)
        self.assertEqual(how, "direct")
        self.assertGreaterEqual(calls[1][1], launcher.STEAM_RETRY_S + launcher.STEAM_COLD_S - 0.6)

    def test_steam_working_on_it_is_not_overtaken(self):
        lines = [(1, steam_line('LaunchApp changed task to SynchronizingCloud with ""')),
                 (2, steam_line('LaunchApp waiting for user response to ShowEula ""'))]
        (how, proc), calls, said, now = self.handoff(lines=lines)
        self.assertEqual((how, proc), ("steam", None))
        self.assertEqual([c[0] for c in calls], ["ask"])
        self.assertTrue(any("Steam's window (ShowEula)" in s for s in said))
        self.assertLess(now, launcher.STEAM_RETRY_S)

    def test_quick_answers_are_not_said(self):
        lines = [(1, steam_line('LaunchApp waiting for user response to CreatingProcess ""')),
                 (1, steam_line('LaunchApp continues with user response "CreatingProcess"')),
                 (2, "[2026-11-12 16:34:26] Game process added : AppID 284160 \"C:\\BeamNG.drive.exe\", ProcID 1")]
        (how, _), calls, said, _ = self.handoff(lines=lines)
        self.assertEqual(how, "started")
        self.assertEqual(said, [])

    def test_no_game_folder(self):
        with mock.patch.object(launcher, "say"):
            (how, proc), _, _, _ = self.handoff(direct_proc=None)
        self.assertEqual((how, proc), ("none", None))

    def test_news_and_words(self):
        news = launcher.steam_news([
            '[2026-11-12 00:22:00] ExecuteSteamURL: "steam://rungameid/284160"',
            steam_line('LaunchApp changed task to UpdatingApp with ""'),
            '[2026-11-12 00:22:01] Game process added : AppID 284160 "x", ProcID 8228, IP 0.0.0.0:0',
            '[2026-11-12 00:22:01] GameAction [AppID 999, ActionID 1] : LaunchApp changed task to Completed with ""'])
        self.assertEqual([k for k, _ in news], ["asked", "step", "started"])
        self.assertIn("updating", launcher.steam_step_text(news[1][1]))
        self.assertIsNone(launcher.steam_step_text('LaunchApp changed task to Completed with ""'))

    def test_steam_log_reads_only_new_complete_lines(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "console_log.txt")
            Path(path).write_bytes(b"old line\n")
            log = launcher.SteamLog(path)
            with open(path, "ab") as f:
                f.write(b"new one\nhalf")
            self.assertEqual(log.read(), ["new one"])
            with open(path, "ab") as f:
                f.write(b" done\n")
            self.assertEqual(log.read(), ["half done"])
            self.assertEqual(launcher.SteamLog(None).read(), [])


class WindowsListeningTest(unittest.TestCase):
    def table(self, ports):
        import socket
        import struct
        rows = b"".join(struct.pack("<6I", 2, 0x0100007F, socket.htons(p), 0, 0, 1234) for p in ports)
        return struct.pack("<I", len(ports)) + rows

    def test_port_in_the_table(self):
        self.assertTrue(bmplaunch.windows_listening(47613, table=self.table([135, 47613])))
        self.assertFalse(bmplaunch.windows_listening(4444, table=self.table([135, 47613])))
        self.assertFalse(bmplaunch.windows_listening(4444, table=self.table([])))
        self.assertIsNone(bmplaunch.windows_listening(4444, table=b"\x05\x00\x00\x00"))

    def test_not_windows_asks_by_connecting(self):
        self.assertIsNone(bmplaunch.windows_listening(4444) if not bmplaunch.IS_WIN else None)
        with mock.patch.object(bmplaunch.socket, "create_connection", side_effect=OSError):
            self.assertFalse(bmplaunch.listening(4444))


class StartOrderTest(unittest.TestCase):
    """The game starts before the world server is waited for (a ten-second Windows start, Discord 2026-11-12); BeamMP
    before the game; a game already running waits for the server first."""

    def run_launcher(self, running=False, server_ok=True, game_first=True, watched=False):
        order = []
        args = argparse.Namespace(user_dir="/u", build="native", keep_mod=True, game_dir=None, no_game=False, extra=[],
                                  cache_dir=None, clear_cache=False, no_gcpatch=True, gfx="", gpu="off",
                                  no_update_check=True, no_beammp=False)
        server = mock.Mock()
        server.poll.return_value = None
        with ExitStack() as st:
            p = lambda name, **kw: st.enter_context(mock.patch.object(launcher, name, **kw))
            p("IS_WIN", new=False)
            p("GAME_FIRST", new=game_first)
            for name in ("title", "support_notice", "memory_notice", "row", "say", "watch_game_log",
                         "clear_level_flag", "problem"):
                p(name)
            p("update_at_start", return_value=False)
            p("under_wine", return_value=False)
            p("steam_compat_tool", return_value=None)
            p("steam_launch_options", return_value="")
            p("gpu_plan", return_value=({}, None))
            p("game_still_running", return_value=running)
            p("linux_game_exe", return_value="/g/BinLinux/BeamNG.drive.x64")
            p("cache_dir_arg", return_value=None)
            p("server_port_busy", return_value=False)
            p("data_dir", return_value="/d")
            p("start_server", side_effect=lambda *a, **k: order.append("server spawned") or server)
            p("wait_server_ready", side_effect=lambda *a, **k: order.append("server waited") or server_ok)
            p("beammp_up", side_effect=lambda *a, **k: order.append("beammp") or None)
            def start_game(*a, meanwhile=None, **k):
                order.append("game")
                if watched and meanwhile:       # (the start watched: the server followed meanwhile, once)
                    meanwhile()
                    order.append("watching")
            p("start_game", side_effect=start_game)
            p("wait_for_game", side_effect=lambda *a, **k: order.append("follow") or True)
            p("stop_server")
            st.enter_context(mock.patch("install.clear_reload_files"))
            st.enter_context(mock.patch("install.reload_running_game", return_value=True))
            if server_ok:
                launcher.run(args)
            else:
                with self.assertRaises(SystemExit):
                    launcher.run(args)
        return order

    def test_game_first(self):
        self.assertEqual(self.run_launcher(), ["server spawned", "beammp", "game", "server waited", "follow"])

    def test_server_followed_while_the_start_is_watched(self):
        self.assertEqual(self.run_launcher(watched=True),
                         ["server spawned", "beammp", "game", "server waited", "watching", "follow"])

    def test_off_waits_for_the_server_first(self):
        self.assertEqual(self.run_launcher(game_first=False),
                         ["server spawned", "server waited", "beammp", "game", "follow"])

    def test_game_already_running(self):
        self.assertEqual(self.run_launcher(running=True), ["server spawned", "server waited", "beammp", "follow"])

    def test_server_failing_after_the_game_started(self):
        self.assertEqual(self.run_launcher(server_ok=False), ["server spawned", "beammp", "game", "server waited"])
