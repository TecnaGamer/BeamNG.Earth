"""launcher.py on Linux: the user folder and start path with a Flatpak Steam, the game-closed grace period."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import install  # noqa: E402
import launcher  # noqa: E402

FP = (".var", "app", "com.valvesoftware.Steam")


def touch(path, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").close()
    if mtime is not None:
        os.utime(path, (mtime, mtime))


@unittest.skipIf(os.name == "nt", "Linux: the launcher's Windows paths (LOCALAPPDATA, USERPROFILE) ignore HOME")
class FlatpakSteam(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.env = mock.patch.dict(os.environ, {"HOME": self.home})
        self.env.start()
        # the player's report (2026-10-17): Debian, Steam as a Flatpak; an earlier launcher made ~/.local/share's
        # folder (the mod in it), the game itself writes inside the Flatpak's home
        self.host = os.path.join(self.home, ".local", "share", "BeamNG", "BeamNG.drive", "current")
        os.makedirs(os.path.join(self.host, "mods"))
        self.flat = os.path.join(self.home, *FP, ".local", "share", "BeamNG", "BeamNG.drive", "current")
        self.exe = os.path.join(self.home, *FP, ".local", "share", "Steam", "steamapps", "common", "BeamNG.drive",
                                "BinLinux", "BeamNG.drive.x64")
        touch(self.exe)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_folder_the_game_wrote_in(self):
        touch(os.path.join(self.flat, "beamng.log"))
        with mock.patch.object(launcher, "steam_compat_tool", return_value=""):
            self.assertEqual(launcher.linux_build(), ("native", self.flat))

    def test_steam_linux_runtime_is_the_native_build(self):
        # GitHub #56: Steam set to steamlinuxruntime_4 (a container for the native build), a stale Proton prefix
        # from 2024 with a log in it: the native folder, not the prefix
        pfx = os.path.join(self.home, *FP, ".local", "share", "Steam", "steamapps", "compatdata", str(launcher.APP_ID),
                           "pfx", "drive_c", "users", "steamuser", "AppData", "Local", "BeamNG.drive", "0.32")
        touch(os.path.join(pfx, "beamng.log"), time.time() - 3e7)
        touch(os.path.join(self.flat, "beamng.log"))
        for tool in ("steamlinuxruntime_4", "steamlinuxruntime_sniper"):
            with mock.patch.object(launcher, "steam_compat_tool", return_value=tool):
                self.assertEqual(launcher.linux_build(), ("native", self.flat), tool)
        self.assertTrue(launcher.is_proton("proton_9") and launcher.is_proton("GE-Proton9-20")
                        and launcher.is_proton("proton_experimental"))
        self.assertFalse(launcher.is_proton("steamlinuxruntime_4") or launcher.is_proton("") or launcher.is_proton(None))

    def test_game_mode_overlay(self):
        # a Steam Deck's Game Mode shows only windows Steam's overlay tags as the game: found in the Steam root
        lib = os.path.join(self.home, *FP, ".local", "share", "Steam", "ubuntu12_64", "gameoverlayrenderer.so")
        touch(lib)
        self.assertEqual(launcher.steam_overlay_lib(), lib)
        with mock.patch.dict(os.environ, {"GAMESCOPE_WAYLAND_DISPLAY": "gamescope-0"}):
            self.assertTrue(launcher.under_gamescope())
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "KDE"}):
            os.environ.pop("GAMESCOPE_WAYLAND_DISPLAY", None)
            self.assertFalse(launcher.under_gamescope())

    def test_flatpak_folder_before_the_first_start(self):
        import shutil
        shutil.rmtree(os.path.join(self.home, ".local"))
        self.assertEqual(launcher.native_folder(), self.flat)

    def test_host_steam_keeps_local_share(self):
        os.makedirs(os.path.join(self.home, ".local", "share", "Steam"))
        self.assertEqual(launcher.native_folder(), self.host)

    def test_newest_log_wins(self):
        touch(os.path.join(self.flat, "beamng.log"), time.time() - 1000)
        touch(os.path.join(self.host, "beamng.log"))
        self.assertEqual(launcher.native_folder(), self.host)

    def test_flatpak_game_through_steam(self):
        self.assertEqual(install._steam_roots()[0], os.path.join(self.home, *FP, ".local", "share", "Steam"))
        self.assertEqual(launcher.linux_game_exe(), self.exe)
        self.assertTrue(launcher.sandboxed_game(self.exe))
        self.assertFalse(launcher.sandboxed_game("/usr/games/steam/BeamNG.drive.x64"))

    def test_flatpak_steam_pid(self):
        touch(os.path.join(self.home, *FP, ".steam", "steam.pid"))
        with open(os.path.join(self.home, *FP, ".steam", "steam.pid"), "w") as f:
            f.write(str(os.getpid()))
        self.assertTrue(launcher.steam_running())


class GameClosed(unittest.TestCase):
    def test_a_restart_during_the_start_is_not_a_close(self):
        seen = iter([[1], [], [], [2], [2], []] + [[]] * 100)
        clock = [0.0]

        def sleep(s):
            clock[0] += s
        with mock.patch.object(launcher, "game_pids", lambda: next(seen)), \
                mock.patch.object(launcher, "game_connected", lambda: False), \
                mock.patch.object(launcher.time, "sleep", sleep), \
                mock.patch.object(launcher.time, "time", lambda: clock[0]):
            launcher.wait_for_game(None)
        # [1] found; gone twice (6 s), back ([2] x2, 6 s), then gone for good: closed after GAME_GONE_S of absence
        self.assertGreaterEqual(clock[0], 12 + launcher.GAME_GONE_S)


@unittest.skipIf(os.name == "nt", "Linux: ensure_ca_certs leaves Windows alone")
class CaCerts(unittest.TestCase):
    """The packaged app's OpenSSL looks in /usr/lib/ssl (Ubuntu's): elsewhere SSL_CERT_FILE points at a bundle."""
    NONE = mock.Mock(cafile="/usr/lib/ssl/cert.pem", capath="/nonexistent/certs")

    def run_it(self, bundles, env=None):
        with mock.patch.dict(os.environ, env or {}, clear=False), \
                mock.patch("ssl.get_default_verify_paths", return_value=self.NONE), \
                mock.patch.object(launcher, "CA_BUNDLES", bundles):
            if not env:
                os.environ.pop("SSL_CERT_FILE", None)
            launcher.ensure_ca_certs()
            return os.environ.get("SSL_CERT_FILE")

    def test_system_bundle(self):
        with tempfile.NamedTemporaryFile() as f:
            self.assertEqual(self.run_it(("/nonexistent/a.crt", f.name)), f.name)

    def test_certifi_fallback(self):
        import certifi
        self.assertEqual(self.run_it(("/nonexistent/a.crt",)), certifi.where())

    def test_kept_when_set(self):
        self.assertEqual(self.run_it((), {"SSL_CERT_FILE": "/mine.pem"}), "/mine.pem")



class CacheDirTest(unittest.TestCase):
    """--cache-dir: the download cache on another drive, remembered (GitHub #254)."""
    def test_remembered_and_forgotten(self):
        with tempfile.TemporaryDirectory() as dd:
            self.assertIsNone(launcher.cache_dir_arg(None, dd), "the server's default")
            where = os.path.join(dd, "other drive", "cache")
            self.assertEqual(launcher.cache_dir_arg(f' "{where}" ', dd), where)
            self.assertTrue(os.path.isdir(where), "made")
            self.assertEqual(launcher.cache_dir_arg(None, dd), where, "kept for the next start")
            self.assertIsNone(launcher.cache_dir_arg("default", dd))
            self.assertIsNone(launcher.cache_dir_arg(None, dd), "forgotten")


class GpuPlan(unittest.TestCase):
    """gpu_plan: the hybrid-graphics steering (the user's report, 2026-10-26: the game started directly rendered on
    the integrated GPU where Steam's start used the dedicated one)."""

    def test_nvidia_offload(self):
        env, note = launcher.gpu_plan("auto", scan=[("intel", True, 0), ("nvidia", False, 0, "nvidia")])
        self.assertEqual(env, {"__NV_PRIME_RENDER_OFFLOAD": "1", "__GLX_VENDOR_LIBRARY_NAME": "nvidia"})
        self.assertIn("NVIDIA", note)

    def test_nvidia_is_the_default_already(self):
        env, note = launcher.gpu_plan("auto", scan=[("nvidia", True, 0, "nvidia"), ("intel", False, 0, "")])
        self.assertIsNone(env)
        self.assertIn("already", note)

    def test_driverless_nvidia_goes_by_vram(self):
        # a nouveau-bound NVIDIA card: Mesa's offload only, and only while it reports the more VRAM
        env, note = launcher.gpu_plan("auto", scan=[("intel", True, 0), ("nvidia", False, 8 << 30, "nouveau")])
        self.assertEqual(env, {"DRI_PRIME": "1"})
        self.assertIn("nvidia", note)

    def test_mesa_pair(self):
        env, note = launcher.gpu_plan("auto", scan=[("amd", True, 512 << 20), ("amd", False, 8 << 30, "amdgpu")])
        self.assertEqual(env, {"DRI_PRIME": "1"})
        self.assertIn("DRI_PRIME=1", note)

    def test_mesa_pair_default_is_the_bigger_one(self):
        env, note = launcher.gpu_plan("auto", scan=[("amd", True, 12 << 30, "amdgpu"), ("amd", False, 512 << 20, "amdgpu")])
        self.assertIsNone(env)
        self.assertIn("most VRAM", note)

    def test_vram_unknown_gives_up(self):
        env, note = launcher.gpu_plan("auto", scan=[("intel", True, 0, "i915"), ("amd", False, 0, "amdgpu")])
        self.assertIsNone(env)
        self.assertIn("cannot tell", note)

    def test_discrete_guesses_when_unknown(self):
        env, note = launcher.gpu_plan("discrete", scan=[("intel", True, 0, "i915"), ("amd", False, 0, "amdgpu")])
        self.assertEqual(env, {"DRI_PRIME": "1"})

    def test_one_gpu_off_and_no_gpu(self):
        self.assertIsNone(launcher.gpu_plan("auto", scan=[("amd", True, 4 << 30, "amdgpu")])[0])
        self.assertIsNone(launcher.gpu_plan("off", scan=[("intel", True, 0, ""), ("nvidia", False, 0, "nvidia")])[0])
        self.assertIsNone(launcher.gpu_plan("off", scan=[])[0])
        self.assertIsNone(launcher.gpu_plan("auto", scan=[])[0])

    def test_no_boot_flag_takes_the_first_card(self):
        env, note = launcher.gpu_plan("auto", scan=[("other", False, 0, ""), ("other", False, 0, "")])
        self.assertIsNone(env)

    def test_player_vars_win(self):
        scan = [("intel", True, 0, "i915"), ("nvidia", False, 0, "nvidia")]
        for var, value in (("DRI_PRIME", "1"), ("__NV_PRIME_RENDER_OFFLOAD", "0"),
                           ("MESA_VK_DEVICE_SELECT", "1002:744c"), ("VK_DRIVER_FILES", "/mine.json")):
            env, note = launcher.gpu_plan("auto", environ={var: value}, scan=scan)
            self.assertIsNone(env)
            self.assertIn(var, note)


class VdfTest(unittest.TestCase):
    def test_tree(self):
        self.assertEqual(launcher._vdf('"a" "1" "b" { "c" { "d" "2" } "e" "3" } // a comment'),
                         {"a": "1", "b": {"c": {"d": "2"}, "e": "3"}})

    def test_escapes(self):
        self.assertEqual(launcher._vdf('"a" "x\\"y\\\\z"'), {"a": 'x"y\\z'})


class SteamLaunchOptionsTest(unittest.TestCase):
    """steam_launch_options: the game's line in Steam's config.vdf (this client's per-game settings) and
    registry.vdf (an older one's)."""
    APPS = ('"SteamPath" "/home/u/.local/share/Steam"\n'
            f'\t"apps"\n\t{{\n\t\t"{launcher.APP_ID}"\n\t\t{{\n'
            '\t\t\t"LaunchOptions" "DRI_PRIME=1 gamemoderun %command% -gfx dx11"\n'
            '\t\t}\n\t}\n')
    CONFIG = ('"InstallConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n\t\t\t"Steam"\n\t\t\t{\n\t\t\t\t'
              + APPS + '\t\t\t}\n\t\t}\n\t}\n}\n')
    REGISTRY = ('"registry.vdf"\n{\n\t"HKCU"\n\t{\n\t\t"Software"\n\t\t{\n\t\t\t"Valve"\n\t\t\t{\n'
                '\t\t\t\t"Steam"\n\t\t\t\t{\n\t\t\t\t\t' + APPS + '\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n')

    def test_found(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(launcher.steam_launch_options([d]), "no Steam config here")
            with open(os.path.join(d, "registry.vdf"), "w") as f:
                f.write(self.REGISTRY)
            self.assertEqual(launcher.steam_launch_options([d]), "DRI_PRIME=1 gamemoderun %command% -gfx dx11")

    def test_config_vdf_first(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "config"))
            with open(os.path.join(d, "config", "config.vdf"), "w") as f:
                f.write(self.CONFIG)
            self.assertEqual(launcher.steam_launch_options([d]), "DRI_PRIME=1 gamemoderun %command% -gfx dx11")

    def test_this_machines_partial_registry(self):
        # a registry.vdf without the game's apps at all (a ~/.steam one): config read, no options
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "registry.vdf"), "w") as f:
                f.write('"Registry"\n{\n\t"HKCU"\n\t{\n\t\t"Software"\n\t\t{\n\t\t\t"Valve"\n\t\t\t{\n'
                        '\t\t\t\t"Steam"\n\t\t\t\t{\n\t\t\t\t\t"SteamPID" "0"\n\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n')
            self.assertEqual(launcher.steam_launch_options([d]), "")
            self.assertIsNone(launcher.steam_launch_options([os.path.join(d, "config")]))


class SplitLaunchOptionsTest(unittest.TestCase):
    def test_env_wrapper_options(self):
        self.assertEqual(launcher.split_launch_options("DRI_PRIME=1 gamemoderun %command% -gfx dx11"),
                         ({"DRI_PRIME": "1"}, ["gamemoderun"], ["-gfx", "dx11"]))

    def test_env_only(self):
        self.assertEqual(
            launcher.split_launch_options("__NV_PRIME_RENDER_OFFLOAD=1 __GLX_VENDOR_LIBRARY_NAME=nvidia %command%"),
            ({"__NV_PRIME_RENDER_OFFLOAD": "1", "__GLX_VENDOR_LIBRARY_NAME": "nvidia"}, [], []))

    def test_wrappers_only(self):
        self.assertEqual(launcher.split_launch_options("gamemoderun mangohud %command%"),
                         ({}, ["gamemoderun", "mangohud"], []))

    def test_no_command_means_game_options(self):
        self.assertEqual(launcher.split_launch_options("-gfx dx11"), ({}, [], ["-gfx", "dx11"]))

    def test_empty(self):
        self.assertEqual(launcher.split_launch_options(""), ({}, [], []))


class SteamRunningTest(unittest.TestCase):
    """steam_running sees a Flatpak Steam although its steam.pid names a pid inside its own PID namespace (the
    user's report, 2026-10-26: the app then started the game through Steam, where the GPU choice is lost)."""

    def fake_proc(self, comms):
        """comms: {pid: comm text}"""
        real_open, real_listdir = open, os.listdir

        def listdir(p):
            return [str(k) for k in comms] if p == "/proc" else real_listdir(p)

        def fopen(p, *a, **k):
            parts = str(p).split("/")
            if len(parts) == 4 and parts[1] == "proc" and parts[3] == "comm" and int(parts[2]) in comms:
                import io
                return io.StringIO(comms[int(parts[2])])
            return real_open(p, *a, **k)

        return mock.patch.multiple(launcher.os, listdir=listdir), mock.patch("builtins.open", fopen)

    def run_it(self, home_files, comms):
        with tempfile.TemporaryDirectory() as d:
            for name, text in home_files.items():
                p = os.path.join(d, name)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w") as f:
                    f.write(text)
            p1, p2 = self.fake_proc(comms)
            with mock.patch.dict(os.environ, {"HOME": d}), p1, p2:
                return launcher.steam_running()

    def test_flatpak_pid_namespace(self):
        self.assertTrue(self.run_it({".var/app/com.valvesoftware.Steam/.steam/steam.pid": "1252\n"}, {500: "steam"}))

    def test_pidfile_pid_alive(self):
        self.assertTrue(self.run_it({".steam/steam.pid": "500\n"}, {500: "steam"}))

    def test_pidfile_pid_is_not_steam(self):
        self.assertFalse(self.run_it({".steam/steam.pid": "500\n"}, {500: "bash"}))

    def test_nothing_running(self):
        self.assertFalse(self.run_it({}, {500: "steamwebhelper", 501: "kwin"}))


class DirectStartAdoptsOptionsTest(unittest.TestCase):
    """The direct start applies the launch options' variables to the game's environment and appends their options
    (through Steam, Steam applies them itself: nothing passed on)."""

    def run_start(self, steam_args, user_dir=None):
        calls = []

        def popen(argv, **kw):
            if not calls:
                calls.append((argv, kw["env"]))   # the game's spawn; a later one is the through-Steam fallback
            return mock.Mock(poll=lambda: 0)      # "ended at once": the launcher then goes through Steam instead

        with mock.patch.object(launcher, "linux_game_exe", return_value="/g/BeamNG/BinLinux/BeamNG.drive.x64"), \
                mock.patch.object(launcher, "sandboxed_game", return_value=False), \
                mock.patch.object(launcher, "steam_running", return_value=True), \
                mock.patch.object(launcher, "gcpatch_lib", return_value=None), \
                mock.patch.object(launcher, "steam_runtime", return_value=None), \
                mock.patch.object(launcher.subprocess, "Popen", popen), \
                mock.patch.dict(os.environ, {"BNGE_GAME_ARGS": ""}):
            launcher.start_game(False, user_dir=user_dir, steam_args=steam_args)
        argv, env = calls[0]
        return argv, env

    def test_env_and_options_reach_the_game(self):
        argv, env = self.run_start(({"DRI_PRIME": "1"}, [], ["-forcecloud"]))
        self.assertEqual(argv, ["/g/BeamNG/BinLinux/BeamNG.drive.x64", "-forcecloud"])
        self.assertEqual(env["DRI_PRIME"], "1")
        self.assertEqual(env["SteamAppId"], launcher.APP_ID)

    def test_wrapper_wraps_the_game(self):
        argv, env = self.run_start(({}, ["gamemoderun"], []))
        self.assertEqual(argv, ["gamemoderun", "/g/BeamNG/BinLinux/BeamNG.drive.x64"])

    def test_userpath_reaches_the_game(self):
        # the mod and the settings live in the user folder the launcher chose: the game is told the folder its
        # version folder sits in (a Flatpak Steam's folder, the user's report 2026-10-26)
        argv, env = self.run_start(({}, [], []), user_dir="/u/flatpak/BeamNG.drive/current")
        self.assertEqual(argv[:3], ["/g/BeamNG/BinLinux/BeamNG.drive.x64", "-userpath",
                                    "/u/flatpak/BeamNG.drive"])
        argv, env = self.run_start(({}, [], []), user_dir="/u/own/folder")
        self.assertEqual(argv[1:3], ["-userpath", "/u/own/folder"])


if __name__ == "__main__":
    unittest.main()


class WindowsTasklist(unittest.TestCase):
    """game_pids on Windows: tasklist's output in the console's code page (a German Windows, 2026-10-17: byte 0x81
    stopped the launcher with UnicodeDecodeError)."""

    def run_with(self, stdout):
        res = mock.Mock(stdout=stdout, returncode=0)
        with mock.patch.object(launcher, "IS_WIN", True), mock.patch.object(launcher.subprocess, "run", return_value=res):
            return launcher.game_pids()

    def test_localised_no_tasks_message(self):
        msg = "INFORMATION: Es werden keine Aufgaben mit den angegebenen Kriterien ausgeführt.\r\n".encode("cp850")
        self.assertIn(b"\x81", msg)
        self.assertEqual(self.run_with(msg), [])

    def test_game_found(self):
        exe = launcher.GAME_EXE_WIN
        out = f'"{exe}","4242","Console","1","1.234.567 K"\r\n'.encode("cp850")
        self.assertEqual(self.run_with(out), [4242])

    def test_no_output(self):
        self.assertEqual(self.run_with(None), [])


class FlatpakGameProcess(unittest.TestCase):
    """game_pids / game_alive on Linux when argv[0] does not name the game (a Flatpak Steam's container, a player's
    video 2026-10-17: the server stopped ~30 s after the game started)."""

    def fake_proc(self, procs):
        """procs: {pid: (argv list of bytes, exe path or None)}"""
        real_open, real_listdir, real_readlink = open, os.listdir, os.readlink

        def listdir(p):
            return [str(k) for k in procs] + ["self"] if p == "/proc" else real_listdir(p)

        def fopen(p, *a, **k):
            parts = str(p).split("/")
            if len(parts) == 4 and parts[1] == "proc" and parts[3] == "cmdline":
                import io
                return io.BytesIO(b"\0".join(procs[int(parts[2])][0]))
            return real_open(p, *a, **k)

        def readlink(p):
            parts = str(p).split("/")
            if len(parts) == 4 and parts[1] == "proc" and parts[3] == "exe":
                exe = procs[int(parts[2])][1]
                if exe is None:
                    raise PermissionError(p)
                return exe
            return real_readlink(p)
        return mock.patch.multiple(launcher.os, listdir=listdir, readlink=readlink), \
            mock.patch("builtins.open", fopen)

    def test_game_found_by_its_executable(self):
        game = "/home/u/.var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps/common/BeamNG.drive/BinLinux/" + \
            os.path.basename(launcher.GAME_EXE_LINUX)
        procs = {
            10: ([b"/usr/lib/pressure-vessel/from-host/lib/ld-linux-x86-64.so.2", game.encode(), b"-forcecloud"], game),
            11: ([game.encode() + b" --type=renderer --enable-crash-reporter"], game),       # a CEF helper
            12: ([b"/usr/bin/bash", b"-c", b"x"], "/usr/bin/bash"),
            13: ([b"/usr/bin/other"], None),                                                     # not ours
        }
        p1, p2 = self.fake_proc(procs)
        with mock.patch.object(launcher, "IS_WIN", False), p1, p2:
            self.assertEqual(launcher.game_pids(), [10])

    def test_alive_while_the_mod_is_connected(self):
        port = format(launcher.SERVER_PORT, "04X")
        tcp = ("  sl  local_address rem_address   st\n"
               f"   0: 0100007F:{port} 0100007F:8A3C 01 00000000:00000000\n")
        with mock.patch.object(launcher, "IS_WIN", False), mock.patch.object(launcher, "game_pids", lambda: []), \
                mock.patch("builtins.open", mock.mock_open(read_data=tcp)):
            self.assertTrue(launcher.game_alive())
        listen = tcp.replace(" 01 ", " 0A ")
        with mock.patch.object(launcher, "IS_WIN", False), mock.patch.object(launcher, "game_pids", lambda: []), \
                mock.patch("builtins.open", mock.mock_open(read_data=listen)):
            self.assertFalse(launcher.game_alive())


class ConsoleEcho(unittest.TestCase):
    def test_only_warnings_errors_tracebacks_and_connected(self):
        log = ["2026-10-17 07:04:50,488 INFO earthd: session 1 kept\n",
               "earthd listening on 127.0.0.1:47613 session x\n",
               "2026-10-17 07:04:55,466 INFO earthd: client connected ('127.0.0.1', 34278)\n",
               "2026-10-17 07:05:01,000 WARNING earthd.net: net: host: circuit open for 60 s\n",
               "2026-10-17 07:05:02,000 ERROR earthd: tile failed\n",
               "Traceback (most recent call last):\n", '  File "dem.py", line 236\n', "dem.DemUnavailable: x\n",
               "2026-10-17 07:05:03,000 INFO earthd: anchor 1: land cover wait 3.11 s\n",
               "2026-10-17 07:05:04,000 INFO earthd: client connected ('127.0.0.1', 1)\n"]
        state = {}
        out = launcher.echo_lines(log[:4], state) + launcher.echo_lines(log[4:], state)
        self.assertEqual(out[0], "  Game          connected to the world server")
        self.assertTrue(out[1].startswith("  server: ") and "WARNING" in out[1])
        self.assertIn("ERROR", out[2])
        self.assertEqual([o.strip() for o in out[3:6]], ["server: Traceback (most recent call last):",
                                                         'server:   File "dem.py", line 236', "server: dem.DemUnavailable: x"])
        self.assertEqual(len(out), 6, "no INFO lines, 'connected' once")


class ModsOffAfterUpdateTest(unittest.TestCase):
    """A game update turns every mod off until the player turns them back on (BeamNG 0.40, 2026-10-19): the
    launcher reads the game's log and says what to do."""

    def test_the_line_is_reported(self):
        got = []
        old = launcher.problem
        launcher.problem = lambda msg, hint=None: got.append((msg, hint))
        self.addCleanup(setattr, launcher, "problem", old)
        with tempfile.TemporaryDirectory() as d:
            since = time.time() - 1
            launcher.watch_game_log(d, since)
            with open(os.path.join(d, "beamng.log"), "w") as f:
                f.write(" 1.2|I|getModsDisabledAfterUpdate| Mods will be disabled after updating from version: '0.39.4'\n")
            for _ in range(20):
                if got:
                    break
                time.sleep(0.25)
        self.assertEqual(len(got), 1)
        self.assertIn("turned all mods off", got[0][0])

    def test_last_runs_log_is_ignored(self):
        got = []
        old = launcher.problem
        launcher.problem = lambda msg, hint=None: got.append(msg)
        self.addCleanup(setattr, launcher, "problem", old)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "beamng.log")
            with open(p, "w") as f:
                f.write("Mods will be disabled after updating from version: '0.39.4'\n")
            os.utime(p, (time.time() - 3600, time.time() - 3600))      # an old run's log
            launcher.watch_game_log(d, time.time())
            time.sleep(3)
        self.assertEqual(got, [])


class GfxArgTest(unittest.TestCase):
    def test_dx12_is_d3d12(self):
        """The game knows -gfx dx11 / vk but not dx12 ("no valid rendering device"): DirectX 12 is d3d12."""
        self.assertEqual(launcher.GFX_ARG, {"dx11": "dx11", "dx12": "d3d12", "vk": "vk"})


class WindowsStartTest(unittest.TestCase):
    """On Windows the game starts through Steam (Steam Input, GitHub #218) unless start options are needed or the app
    runs under Wine; then BeamNG.drive.exe directly."""

    def test_through_steam_without_options(self):
        with mock.patch.object(launcher, "IS_WIN", True), mock.patch.object(launcher, "under_wine", return_value=False), \
                mock.patch.object(launcher.os, "startfile", create=True) as sf, \
                mock.patch.object(launcher.subprocess, "Popen") as po, mock.patch.dict(os.environ, {"BNGE_GAME_ARGS": ""}):
            self.assertIsNone(launcher.start_game(False))
            sf.assert_called_once_with("steam://rungameid/284160")
            po.assert_not_called()

    def test_direct_with_options_or_wine(self):
        for gfx, wine in (("dx12", False), ("", True)):
            with mock.patch.object(launcher, "IS_WIN", True), mock.patch.object(launcher, "under_wine", return_value=wine), \
                    mock.patch.object(launcher, "windows_game_dir", return_value="C:/BeamNG"), \
                    mock.patch.object(launcher.os, "startfile", create=True) as sf, \
                    mock.patch.object(launcher.subprocess, "Popen") as po, \
                    mock.patch.dict(os.environ, {"BNGE_GAME_ARGS": ""}):
                launcher.start_game(False, gfx=gfx)
                sf.assert_not_called()
                args = po.call_args[0][0]
                self.assertTrue(args[0].endswith("BeamNG.drive.exe"))
                self.assertEqual(args[1:], ["-gfx", "d3d12"] if gfx else [])


class WaitForGameTest(unittest.TestCase):
    def test_a_late_game_is_still_followed(self):
        """Started through Steam: no game for longer than GAME_START_S (the launch options window open), then the game
        runs and closes: wait_for_game returns once it is gone (it used to wait for good)."""
        clock = [0.0]
        seen = iter([False] * 5 + [True] * 3)        # game_pids: none for a while, then the game

        def fake_sleep(s):
            clock[0] += max(s, 100)
        alive = iter([True, True, False, False, False, False, False, False, False, False, False, False])
        with mock.patch.object(launcher.time, "time", side_effect=lambda: clock[0]), \
                mock.patch.object(launcher.time, "sleep", side_effect=fake_sleep), \
                mock.patch.object(launcher, "game_pids", side_effect=lambda: [1] if next(seen, True) else []), \
                mock.patch.object(launcher, "game_alive", side_effect=lambda: next(alive, False)), \
                mock.patch.object(launcher, "game_launcher_running", return_value=False), \
                mock.patch.object(launcher, "say") as say:
            launcher.wait_for_game(None)
            self.assertEqual(say.call_count, 1, "told once that the game has not started yet")
