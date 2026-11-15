"""The settings page's "Restart world server" (server/srvrestart.py, 2026-11-15), without a game or a real server:
  - the start-time view of the settings read only at a start (the Experimental switches with their version rule, the
    ground grid with its preset rule, the cores, the priority, the 3dmr.eu models), the supervisor's part of it handed
    over in the environment (START_ENV) and taken over by the server for those keys only;
  - the supervisors' rate limit (never a restart loop) and that only EXIT_CODE is a restart;
  - the server's side: a request refused without a supervisor or before it serves, accepted with one (the answer goes
    out first, then main_async's stop), serve() returning with every connection closed, and the clean exit (finish: the
    worker processes stopped, the exit handlers run, exit code EXIT_CODE) in a child process;
  - the launcher's RestartingServer (started again on EXIT_CODE with the settings read again, never on a crash or while
    stopping, at most MAX_RESTARTS) and start.py's loop, with fake processes.
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent
ROOT = SERVER.parent
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import experimental  # noqa: E402
import srvrestart  # noqa: E402


def write_settings(folder, d):
    os.makedirs(os.path.join(folder, "settings"), exist_ok=True)
    with open(os.path.join(folder, "settings", "beamngEarth.json"), "w", encoding="utf-8") as f:
        json.dump(d, f)
    return folder


class ViewTest(unittest.TestCase):
    def test_defaults(self):
        v = srvrestart.view({})
        self.assertEqual(v["groundGrid"], "1")
        self.assertEqual(v["serverCores"], "auto")
        self.assertEqual(v["serverPriority"], "low")
        self.assertIs(v["models3dmr"], True)
        for sid in experimental.IDS:
            self.assertIs(v[sid], False, sid)
        self.assertEqual(srvrestart.view(None), v, "no file: the defaults")

    def test_values_as_the_server_takes_them(self):
        v = srvrestart.view({"version": 3, "groundGrid": "2", "serverCores": "3", "serverPriority": "normal",
                             "models3dmr": False, "expAiTunnels": True, "roadSigns": True})
        self.assertEqual((v["groundGrid"], v["serverCores"], v["serverPriority"], v["models3dmr"]),
                         ("2", "3", "normal", False))
        self.assertTrue(v["expAiTunnels"] and v["roadSigns"])
        self.assertEqual(srvrestart.view({"serverCores": "7"})["serverCores"], "auto", "not a choice: Auto, as cpupin")
        self.assertEqual(srvrestart.view({"quality": "low"})["groundGrid"], "2", "a file without it: its preset's")
        self.assertEqual(srvrestart.view({"quality": "low", "groundGrid": "1"})["groundGrid"], "1")
        # (road signs before settings version 3: the hidden switch's old default, not a choice: experimental.chosen)
        self.assertIs(srvrestart.view({"version": 2, "roadSigns": True})["roadSigns"], False)

    def applied(self, settings, base=None):
        """A supervisor's environment for settings (as launcher.start_server / start.server_env build it)."""
        base = dict(base or {})
        env = dict(base)
        for k, v in experimental.env_for(settings).items():     # (experimental.apply's rule: one set by hand stays)
            env.setdefault(k, v)
        srvrestart.apply_ground(env, settings)
        return srvrestart.supervisor_env(env, settings, base_env=base, times=[100.0, 200.5])

    def test_supervisor_part_and_start_view(self):
        with tempfile.TemporaryDirectory() as folder:
            d = {"version": 3, "expDropRails": True, "serverCores": "2"}
            write_settings(folder, d)
            env = self.applied(d)
            self.assertEqual(env[srvrestart.ENV], "1")
            self.assertEqual(env[srvrestart.TIMES_ENV], "100.000,200.500")
            part = json.loads(env[srvrestart.START_ENV])
            self.assertEqual(set(part["values"]), set(experimental.IDS), "only what the supervisor applies")
            self.assertTrue(part["values"]["expDropRails"])
            self.assertEqual(part["fixed"], [], "nothing set by hand")
            self.assertTrue(srvrestart.restartable(env))
            self.assertFalse(srvrestart.restartable({}))
            # the player changes the file while the server starts: the switches are what the supervisor applied, the
            # server's own settings what it read itself
            write_settings(folder, {"version": 3, "expDropRails": False, "serverCores": "4"})
            v, fixed = srvrestart.start_view(folder, env, square=1.0)
            self.assertTrue(v["expDropRails"])
            self.assertEqual((v["serverCores"], fixed), ("4", []))
            # a broken or odd hand-over changes nothing it should not
            for bad in ("{broken", json.dumps({"values": {"serverCores": "8", "expDropRails": "yes"}, "fixed": [3, "x"]})):
                v, fixed = srvrestart.start_view(folder, {srvrestart.START_ENV: bad})
                self.assertEqual((v["serverCores"], v["expDropRails"], fixed), ("4", False, []), bad)

    def test_ground_grid_is_what_is_in_force(self):
        """Codex 2026-11-15: start.py never applied Ground grid, yet the hello reported the file's 2 m as active (the
        button then went away with the server still on 1 m). The value is the server's own square now."""
        with tempfile.TemporaryDirectory() as folder:
            write_settings(folder, {"version": 3, "groundGrid": "2"})
            v, _ = srvrestart.start_view(folder, {}, square=1.0)
            self.assertEqual(v["groundGrid"], "1", "the square in force, not the file's wish")
            v, _ = srvrestart.start_view(folder, {}, square=2.0)
            self.assertEqual(v["groundGrid"], "2")
        self.assertEqual(srvrestart.apply_ground({}, {"groundGrid": "2"}), {srvrestart.GROUND_ENV: "2"})
        self.assertEqual(srvrestart.apply_ground({}, {"groundGrid": "1"}), {})
        self.assertEqual(srvrestart.apply_ground({srvrestart.GROUND_ENV: "1"}, {"groundGrid": "2"}),
                         {srvrestart.GROUND_ENV: "1"}, "set by hand: it wins")

    def test_set_by_hand_is_fixed(self):
        d = {"version": 3, "groundGrid": "2"}
        part = json.loads(self.applied(d, base={"BNE_AI_TUNNELS": "on", srvrestart.GROUND_ENV: "1"})[srvrestart.START_ENV])
        self.assertEqual(set(part["fixed"]), {"expAiTunnels", "groundGrid"}, "the page cannot change these")
        # a dev's BNGE_SERVER_ENV override after the switches: not what the file asks for, fixed too
        env = {"BNE_CATENARY": "1"}
        srvrestart.supervisor_env(env, {"version": 3}, base_env={})
        self.assertIn("expCatenary", json.loads(env[srvrestart.START_ENV])["fixed"])
        # (Codex round 2) a dev's BNGE_SERVER_ENV="BNGE_GROUND_SQUARE=2" over the page's 1 m: fixed, never offered;
        # the same square as the page's, or the page's own: not fixed
        for square, grid, want in (("2", "1", True), ("2.0", "1", True), ("1", "2", True), ("2", "2", False),
                                   ("", "1", False), ("1", "1", False)):
            env = {srvrestart.GROUND_ENV: square} if square else {}
            srvrestart.supervisor_env(env, {"version": 3, "groundGrid": grid}, base_env={})
            self.assertEqual("groundGrid" in json.loads(env[srvrestart.START_ENV])["fixed"], want, (square, grid))
        with tempfile.TemporaryDirectory() as folder:
            _v, fixed = srvrestart.start_view(folder, {srvrestart.START_ENV: json.dumps(part)}, hand=["serverCores"])
            self.assertEqual(fixed, ["expAiTunnels", "groundGrid", "serverCores"])

    def test_values_match_the_mod_schema(self):
        # (the mod compares them with its settings as they are: srvrestart.lua pending; settings.lua SCHEMA's values)
        lua = (ROOT / "mod" / "beamng_earth" / "lua" / "ge" / "extensions" / "beamngEarth" / "settings.lua").read_text()
        for k in srvrestart.view({}):
            self.assertIn(f"{k} = {{", lua, f"{k}: a setting of the mod's SCHEMA")


class LimiterTest(unittest.TestCase):
    def test_at_most_in_a_window(self):
        lim = srvrestart.Limiter(most=3, window_s=600)
        self.assertEqual([lim.allow(t) for t in (0, 10, 20, 30)], [True, True, True, False])
        self.assertFalse(lim.allow(599))
        self.assertTrue(lim.allow(601), "the first one is out of the window again")

    def test_server_side_budget(self):
        """Codex 2026-11-15: six requests in ten minutes used up the supervisor's five restarts and the sixth left the
        player with a stopped server. The server now knows the supervisor's restarts and refuses instead."""
        sup = srvrestart.Limiter()
        env = {}
        now = 1_000_000.0
        for i in range(srvrestart.MAX_RESTARTS):
            env[srvrestart.TIMES_ENV] = ",".join(f"{t:.3f}" for t in sup.times)
            self.assertIsNone(srvrestart.budget_refusal(env, now + i), f"restart {i + 1}: allowed")
            self.assertTrue(sup.allow(now + i + 0.5), "and the supervisor, a moment later, honours it")
        env[srvrestart.TIMES_ENV] = ",".join(f"{t:.3f}" for t in sup.times)
        why = srvrestart.budget_refusal(env, now + 10)
        self.assertIn("try again in 10 min", why, "the sixth: refused by the server, which keeps running")
        self.assertFalse(sup.would_allow(now + 10))
        self.assertIsNone(srvrestart.budget_refusal(env, now + srvrestart.WINDOW_S + 1), "later: allowed again")
        self.assertIsNone(srvrestart.budget_refusal({srvrestart.TIMES_ENV: "x,,nan"}, now), "garbage: no times")
        self.assertIsNone(srvrestart.budget_refusal({}, now))

    def test_only_the_restart_code(self):
        self.assertTrue(srvrestart.wants_restart(srvrestart.EXIT_CODE))
        for code in (0, 1, 2, 120, -9, -15, None, 3221225477):
            self.assertFalse(srvrestart.wants_restart(code), code)


class FinishTest(unittest.TestCase):
    def test_clean_exit_in_a_child(self):
        """finish(): the worker processes ended, the exit handlers run, the restart's exit code."""
        with tempfile.TemporaryDirectory() as tmp:
            script = os.path.join(tmp, "child.py")
            mark = os.path.join(tmp, "atexit.txt")
            pidf = os.path.join(tmp, "worker.pid")
            with open(script, "w", encoding="utf-8") as f:
                f.write(textwrap.dedent(f"""
                    import atexit, multiprocessing, sys, threading, time
                    sys.path.insert(0, {str(SERVER)!r})
                    import srvrestart

                    def worker():
                        time.sleep(60)

                    def hang():          # (a thread pool's job in flight: not waited for)
                        time.sleep(60)

                    if __name__ == "__main__":
                        p = multiprocessing.get_context("spawn").Process(target=worker)
                        p.start()
                        open({pidf!r}, "w").write(str(p.pid))
                        atexit.register(lambda: open({mark!r}, "w").write("ran"))
                        threading.Thread(target=hang).start()       # (not a daemon)
                        srvrestart.finish(None)
                """))
            t0 = time.monotonic()
            r = subprocess.run([sys.executable, "-I", script], capture_output=True, timeout=60)
            self.assertEqual(r.returncode, srvrestart.EXIT_CODE, r.stderr.decode(errors="replace"))
            self.assertLess(time.monotonic() - t0, 30, "the hanging thread was not waited for")
            with open(mark, encoding="utf-8") as f:
                self.assertEqual(f.read(), "ran", "the exit handlers ran")
            with open(pidf, encoding="utf-8") as f:
                pid = int(f.read())
            gone = False
            for _ in range(50):
                try:
                    os.kill(pid, 0)
                except OSError:
                    gone = True
                    break
                time.sleep(0.1)
            self.assertTrue(gone, "the worker process ended with it")


class ServerSideTest(unittest.TestCase):
    """earthd's request handling and serve() (no pools, no game)."""

    @classmethod
    def setUpClass(cls):
        import earthd
        cls.earthd = earthd

    def bare_server(self):
        s = self.earthd.Server.__new__(self.earthd.Server)
        s.restartable, s.stop_future, s.restart_wanted, s.restart_key = False, None, False, "ab" * 16
        return s

    def test_refusals(self):
        s = self.bare_server()
        game = types.SimpleNamespace()
        game.lease = types.SimpleNamespace(connection=game)
        ok = {"key": "ab" * 16}
        self.assertIn("not started by the BeamNG.Earth app", s.restart_refusal(game, ok))
        s.restartable = True
        self.assertIn("still starting", s.restart_refusal(game, ok), "not serving yet")
        s.stop_future = object()
        with mock.patch.dict(os.environ, {srvrestart.TIMES_ENV: ""}):
            self.assertIsNone(s.restart_refusal(game, ok))
            # (Codex 2026-11-15: any local client could stop the server) no hello on the connection, another
            # connection's lease, no key or a wrong one: refused before anything is answered yes
            self.assertIn("no hello", s.restart_refusal(types.SimpleNamespace(lease=None), ok))
            other = types.SimpleNamespace(lease=game.lease)
            self.assertIn("no hello", s.restart_refusal(other, ok))
            for bad in ({}, {"key": "cd" * 16}, {"key": 5}, {"key": ""}):
                self.assertIn("restart key", s.restart_refusal(game, bad), bad)
            s.restart_key = None
            self.assertIn("restart key", s.restart_refusal(game, ok), "no key written: nobody can ask")
            s.restart_key = "ab" * 16
        times = ",".join(str(time.time() - i) for i in range(srvrestart.MAX_RESTARTS))
        with mock.patch.dict(os.environ, {srvrestart.TIMES_ENV: times}):
            self.assertIn("try again in", s.restart_refusal(game, ok), "the supervisor's limit: refused, not stopped")

    def test_key_file(self):
        with tempfile.TemporaryDirectory() as root:
            k1 = srvrestart.write_key(os.path.join(root, "temp", "beamngEarth"))
            path = os.path.join(root, "temp", "beamngEarth", srvrestart.KEY_FILE)
            with open(path, encoding="ascii") as f:
                self.assertEqual(f.read(), k1)
            if os.name == "posix":
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o600, "private to the user")
            k2 = srvrestart.write_key(os.path.join(root, "temp", "beamngEarth"))
            self.assertNotEqual(k1, k2, "a fresh key every start")
            self.assertTrue(srvrestart.key_ok(k2, k2 + "\n") and not srvrestart.key_ok(k2, k1))
            blocker = os.path.join(root, "file")
            open(blocker, "w").close()
            self.assertIsNone(srvrestart.write_key(os.path.join(blocker, "sub")), "cannot write: None")

    def test_request_then_serve_returns_with_connections_closed(self):
        earthd = self.earthd
        s = self.bare_server()
        s.restartable = True
        closed = []
        s.connections = [types.SimpleNamespace(abort_stream=lambda why: closed.append(why))]
        restart = []

        async def run():
            srv = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
            task = asyncio.ensure_future(earthd.serve(srv, s, restart))
            await asyncio.sleep(0.05)
            self.assertIsNotNone(s.stop_future, "serving: a stop to wait on")
            s.request_restart(delay_s=0.05)
            s.request_restart(delay_s=0.05)         # (twice: the same restart)
            self.assertFalse(task.done(), "the answer goes out before the stop")
            await asyncio.wait_for(task, 5)
            return srv
        with mock.patch.object(earthd.srvrestartmod, "finish_soon") as guard:
            srv = asyncio.run(run())
        self.assertEqual(restart, [True])
        self.assertEqual(closed, ["the world server restarts"])
        self.assertFalse(srv.is_serving())
        guard.assert_called_once()

    def test_serve_without_a_restart_is_cancelled_as_before(self):
        earthd = self.earthd
        s = self.bare_server()
        s.connections = set()
        restart = []

        async def run():
            srv = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
            task = asyncio.ensure_future(earthd.serve(srv, s, restart))
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        with mock.patch.object(earthd.srvrestartmod, "finish_soon") as guard:
            asyncio.run(run())
        self.assertEqual(restart, [])
        guard.assert_not_called()


import test_server  # noqa: E402
from netclient import Client  # noqa: E402


class SessionTest(test_server.ServerHarness):
    """In a session: hello names the start-time values and the supervisor; the request's answers."""

    def test_hello_and_request(self):
        self.server.start_settings = srvrestart.view({"version": 3, "expDropRails": True})
        self.server.start_fixed = ["serverCores"]
        self.server.restartable = True
        key = self.server.restart_key = srvrestart.write_key(self.server.root)
        self.assertTrue(os.path.isfile(os.path.join(self.server.root, srvrestart.KEY_FILE)))
        self.c.send({"type": "hello", "protocol": 4, "format": self.MOD_FORMAT, "modVersion": "0.4.0"})
        m, _ = self.c.until(lambda m: m["type"] == "hello", 10)
        self.assertIs(m["restartable"], True)
        self.assertIs(m["startSettings"]["expDropRails"], True)
        self.assertEqual(m["startSettings"]["groundGrid"], "1")
        self.assertEqual(m["startFixed"], ["serverCores"])
        self.c.send({"type": "server_restart", "key": key})
        m, _ = self.c.until(lambda m: m["type"] == "server_restart", 10)
        self.assertEqual((m["ok"], "still starting" in m["error"]), (False, True), "not serving yet: refused")

        async def serving():
            self.server.stop_future = asyncio.get_running_loop().create_future()
        asyncio.run_coroutine_threadsafe(serving(), self.loop).result(5)
        # (Codex 2026-11-15) a stray local client: without a hello, or with one but without the key, refused and the
        # server keeps serving
        stray = Client(self.srv.sockets[0].getsockname()[1])
        try:
            stray.send({"type": "server_restart", "key": key})
            m, _ = stray.until(lambda m: m["type"] == "server_restart", 10)
            self.assertFalse(m["ok"])
            self.assertIn("no hello", m["error"])
            stray.send({"type": "hello", "protocol": 4, "format": self.MOD_FORMAT, "modVersion": "0.4.0"})
            stray.until(lambda m: m["type"] == "hello", 10)
            stray.send({"type": "server_restart", "key": "0" * 32})
            m, _ = stray.until(lambda m: m["type"] == "server_restart", 10)
            self.assertFalse(m["ok"])
            self.assertIn("restart key", m["error"])
        finally:
            stray.close()
        self.assertFalse(self.server.stop_future.done() or self.server.restart_wanted, "still serving")
        self.c.send({"type": "server_restart", "key": key})
        m, _ = self.c.until(lambda m: m["type"] == "server_restart", 10)
        self.assertEqual(m, {"type": "server_restart", "ok": True}, "the answer first ...")
        end = time.monotonic() + 5
        while not self.server.stop_future.done() and time.monotonic() < end:
            time.sleep(0.02)
        self.assertEqual(self.server.stop_future.result(), "restart", "... then the stop")


class FakeProc:
    """A process that exits with `code` after `polls` polls (None: runs until killed)."""
    n = 0

    def __init__(self, code, polls=1):
        FakeProc.n += 1
        self.pid, self.code, self.polls, self.returncode = 40000 + FakeProc.n, code, polls, None

    def poll(self):
        if self.returncode is None and self.code is not None:
            self.polls -= 1
            if self.polls < 0:
                self.returncode = self.code
        return self.returncode

    def wait(self, timeout=None):
        if self.code is None:
            raise subprocess.TimeoutExpired("server", timeout)
        self.returncode = self.code
        return self.code

    def kill(self):
        self.returncode = -9


class LauncherTest(unittest.TestCase):
    def setUp(self):
        import launcher
        self.launcher = launcher
        for name in ("row", "problem", "say"):
            p = mock.patch.object(launcher, name)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(launcher, "server_port_busy", return_value=False)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(launcher.os, "killpg", create=True)
        self.killpg = p.start()
        self.addCleanup(p.stop)

    def server(self, codes):
        procs = [FakeProc(c) for c in codes]
        made = []

        def start():
            made.append(procs[len(made)])
            return made[-1]
        return self.launcher.RestartingServer(start, 47613, None), made

    def test_restarts_on_the_code_only(self):
        rs, made = self.server([srvrestart.EXIT_CODE, None])
        self.assertIsNone(rs.poll())
        self.assertIsNone(rs.poll(), "exited asking: started again, still running")
        self.assertEqual(len(made), 2)
        self.assertIs(rs.proc, made[1])
        self.assertEqual(rs.pid, made[1].pid)
        if not self.launcher.IS_WIN:
            self.killpg.assert_called_once()        # (anything left of the old process group)
        rs, made = self.server([1])
        rs.poll()
        self.assertEqual(rs.poll(), 1, "a crash is the end, as before")
        self.assertEqual(len(made), 1)

    def test_never_while_stopping(self):
        rs, made = self.server([srvrestart.EXIT_CODE, None])
        with mock.patch.object(self.launcher.subprocess, "run"):
            self.launcher.stop_server(rs)
        self.assertTrue(rs.stopping)
        made[0].poll()
        self.assertEqual(rs.poll(), srvrestart.EXIT_CODE, "stopping: its exit is the end")
        self.assertEqual(len(made), 1)

    def test_wait_follows_restarts(self):
        rs, made = self.server([srvrestart.EXIT_CODE, srvrestart.EXIT_CODE, 0])
        self.assertEqual(rs.wait(), 0)
        self.assertEqual(len(made), 3)

    def test_rate_limited(self):
        rs, made = self.server([srvrestart.EXIT_CODE] * (srvrestart.MAX_RESTARTS + 2))
        self.assertEqual(rs.wait(), srvrestart.EXIT_CODE, "too many: it stays stopped")
        self.assertEqual(len(made), srvrestart.MAX_RESTARTS + 1)
        self.launcher.problem.assert_called_once()

    def test_a_restart_reads_the_settings_again(self):
        launcher = self.launcher
        with tempfile.TemporaryDirectory() as folder:
            write_settings(folder, {"version": 3})
            envs = []

            def popen(cmd, **kw):
                envs.append(kw["env"])
                return FakeProc(srvrestart.EXIT_CODE if len(envs) == 1 else None)
            with mock.patch.object(launcher.subprocess, "Popen", side_effect=popen), \
                    mock.patch.object(launcher.threading, "Thread"), \
                    mock.patch.dict(os.environ, {}, clear=False):
                for k in [k for k in os.environ if k.startswith("BNE_")]:
                    os.environ.pop(k)
                lim = srvrestart.Limiter()
                rs = launcher.RestartingServer(
                    lambda: launcher.start_server(folder, [], os.path.join(folder, "server.log"),
                                                  restart_times=lim.times), 47613, None, limiter=lim)
                write_settings(folder, {"version": 3, "expAiTunnels": True})     # (changed in the game meanwhile)
                rs.poll()
                rs.poll()
        self.assertEqual(len(envs), 2)
        self.assertNotIn("BNE_AI_TUNNELS", envs[0])
        self.assertEqual(envs[1].get("BNE_AI_TUNNELS"), "on", "the new switch in the restarted server")
        for env in envs:
            self.assertEqual(env[srvrestart.ENV], "1")
        self.assertFalse(json.loads(envs[0][srvrestart.START_ENV])["values"]["expAiTunnels"])
        self.assertTrue(json.loads(envs[1][srvrestart.START_ENV])["values"]["expAiTunnels"])
        self.assertEqual(envs[0][srvrestart.TIMES_ENV], "", "the first start: no restarts yet")
        self.assertEqual(len(envs[1][srvrestart.TIMES_ENV].split(",")), 1, "the restarted server knows of one")


class LauncherServerEnvTest(unittest.TestCase):
    def test_server_env_ground_override_is_fixed(self):
        """Codex round 2: the launcher's BNGE_SERVER_ENV square over the page's Ground grid is reported fixed."""
        import launcher
        with tempfile.TemporaryDirectory() as folder:
            write_settings(folder, {"version": 3, "groundGrid": "1"})
            for server_env, want in (("BNGE_GROUND_SQUARE=2", True), ("", False)):
                with mock.patch.object(launcher.subprocess, "Popen", return_value=FakeProc(None)) as popen, \
                        mock.patch.object(launcher.threading, "Thread"), mock.patch.object(launcher, "say"), \
                        mock.patch.dict(os.environ, {"BNGE_SERVER_ENV": server_env}, clear=False):
                    os.environ.pop(srvrestart.GROUND_ENV, None)
                    launcher.start_server(folder, [], os.path.join(folder, "server.log"))
                env = popen.call_args.kwargs["env"]
                self.assertEqual(env.get(srvrestart.GROUND_ENV), "2" if want else None)
                self.assertEqual("groundGrid" in json.loads(env[srvrestart.START_ENV])["fixed"], want, server_env)


class RestartExtraTest(unittest.TestCase):
    def test_cache_folder_read_again(self):
        import launcher
        first = ["--cache-dir", "/old", "--verbose"]
        with mock.patch.object(launcher, "cache_dir_arg", return_value="/new") as arg:
            self.assertEqual(launcher.restart_extra(["--verbose"], first), ["--cache-dir", "/new", "--verbose"],
                             "the folder picked on the settings page meanwhile")
            arg.assert_called_once_with(None)
            self.assertEqual(launcher.restart_extra(["--cache-dir", "/mine"], ["--cache-dir", "/mine"]),
                             ["--cache-dir", "/mine"], "the player's own --cache-dir stays")
        with mock.patch.object(launcher, "cache_dir_arg", return_value=None):
            self.assertEqual(launcher.restart_extra([], first), [], "back to the default folder")
        with mock.patch.object(launcher, "cache_dir_arg", side_effect=OSError("x")):
            self.assertEqual(launcher.restart_extra([], first), first, "unreadable: as the first start")


class StartPyTest(unittest.TestCase):
    def test_loop(self):
        import start
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(start.install, "say"), \
                mock.patch.object(start, "server_log_path", return_value=None):
            write_settings(folder, {"version": 3})
            base = {"PATH": "x"}
            runs = []

            def run(cmd, path, e):
                runs.append(dict(e))
                if len(runs) == 1:
                    write_settings(folder, {"version": 3, "expCatenary": True, "groundGrid": "2"})
                    return srvrestart.EXIT_CODE
                return 0
            self.assertEqual(start.run_restarting(["server"], folder, base, run=run), 0)
        self.assertEqual(len(runs), 2)
        self.assertNotIn("BNE_CATENARY", runs[0])
        self.assertEqual(runs[1].get("BNE_CATENARY"), "1", "the restarted server's environment made anew")
        self.assertEqual(runs[1]["PATH"], "x")
        self.assertTrue(all(r[srvrestart.ENV] == "1" for r in runs))
        self.assertEqual(base, {"PATH": "x"}, "the base environment untouched")
        # (Codex 2026-11-15: start.py never applied Ground grid) the restarted source server gets the 2 m ground
        self.assertNotIn(srvrestart.GROUND_ENV, runs[0])
        self.assertEqual(runs[1].get(srvrestart.GROUND_ENV), "2")
        self.assertEqual(len(runs[1][srvrestart.TIMES_ENV].split(",")), 1)

    def test_ground_grid_by_hand_wins_and_is_fixed(self):
        import start
        with tempfile.TemporaryDirectory() as folder:
            write_settings(folder, {"version": 3, "groundGrid": "2"})
            env, _ids = start.server_env(folder, {srvrestart.GROUND_ENV: "1"})
            self.assertEqual(env[srvrestart.GROUND_ENV], "1")
            self.assertIn("groundGrid", json.loads(env[srvrestart.START_ENV])["fixed"])
            env, _ids = start.server_env(folder, {})
            self.assertEqual(env[srvrestart.GROUND_ENV], "2")
            self.assertEqual(json.loads(env[srvrestart.START_ENV])["fixed"], [])

    def test_limit_and_crash(self):
        import start
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(start.install, "say"), \
                mock.patch.object(start, "server_log_path", return_value=None):
            calls = []
            code = start.run_restarting(["server"], folder, {},
                                        run=lambda c, p, e: calls.append(1) or srvrestart.EXIT_CODE,
                                        limiter=srvrestart.Limiter(most=2))
            self.assertEqual((code, len(calls)), (srvrestart.EXIT_CODE, 3), "two restarts, then it stays stopped")
            calls.clear()
            self.assertEqual(start.run_restarting(["server"], folder, {}, run=lambda c, p, e: calls.append(1) or 1), 1)
            self.assertEqual(len(calls), 1, "a crash is not restarted")


if __name__ == "__main__":
    unittest.main()
