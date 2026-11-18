"""gameinstall.py and the launcher's launch modes (the user's go 2026-11-15; ideas from PR #1309): every copy of the game
found (BeamNG's INI / registry, Steam's libraries, Epic's manifests) with its store, the player asked when there are
several, the launch mode kept in app.json only, the commands per mode and system, BeamMP beside every mode, and the
start through Steam unchanged when nothing is chosen. Fake folders, registry values and manifests only: never the
real app.json or game folders."""
import argparse
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import apppaths  # noqa: E402
import bmplaunch  # noqa: E402
import gameinstall  # noqa: E402
import launcher  # noqa: E402

GI = gameinstall


def make_game(root):
    """A fake copy of the game with both builds' programs (the checks differ per system)."""
    for rel in (GI.WIN_LAUNCHER, GI.WIN_GAME, GI.LINUX_GAME):
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").close()
    return root


class Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.dd = os.path.join(self.tmp, "data")          # the app's data folder (app.json): never the real one
        os.makedirs(self.dd)
        p = mock.patch.object(apppaths, "data_dir", return_value=self.dd)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)


class UserBaseTest(unittest.TestCase):
    def test_version_folder_gives_its_parent(self):
        self.assertEqual(GI.user_base(os.path.join("u", "BeamNG.drive", "current")), os.path.join("u", "BeamNG.drive"))
        self.assertEqual(GI.user_base(os.path.join("u", "BeamNG.drive", "0.31")), os.path.join("u", "BeamNG.drive"))
        self.assertEqual(GI.user_base(os.path.join("u", "mine")), os.path.join("u", "mine"))
        self.assertIsNone(GI.user_base(None))


class EpicManifestTest(Tmp):
    def write(self, name, data, raw=None):
        os.makedirs(self.path("manifests"), exist_ok=True)
        with open(self.path("manifests", name), "w", encoding="utf-8") as f:
            f.write(raw if raw is not None else json.dumps(data))

    def test_only_complete_beamng_copies_whose_files_are_there(self):
        epic = make_game(self.path("Epic Games", "BeamNGdrive"))
        other = make_game(self.path("Epic Games", "SomeGame"))
        self.write("A.item", {"DisplayName": "BeamNG.drive", "AppName": "x1", "InstallLocation": epic,
                              "LaunchExecutable": "BeamNG.drive.exe"})
        self.write("B.item", {"DisplayName": "Some Game", "AppName": "y", "InstallLocation": other})
        self.write("C.item", {"DisplayName": "BeamNG.drive", "InstallLocation": self.path("gone")})
        self.write("D.item", {"DisplayName": "BeamNG.drive", "InstallLocation": epic, "bIsIncompleteInstall": True})
        self.write("E.item", None, raw="{not json")
        self.write("F.item", ["a list"])
        self.write("G.txt", {"DisplayName": "BeamNG.drive", "InstallLocation": epic})
        self.assertEqual(GI.epic_installs(self.path("manifests")), [os.path.normpath(epic)])

    def test_deeply_nested_manifest_is_skipped(self):
        # (Codex review of ee11b37e: under MANIFEST_MAX, 100,000 nested arrays raised RecursionError out of json and
        # stopped every Windows start, a Steam copy's too)
        epic = make_game(self.path("Epic Games", "BeamNGdrive"))
        self.write("A.item", None, raw="[" * 100000 + "]" * 100000)
        self.write("B.item", {"DisplayName": "BeamNG.drive", "InstallLocation": epic})
        self.assertEqual(GI.epic_installs(self.path("manifests")), [os.path.normpath(epic)])

    def test_matched_by_names_not_by_folder(self):
        # (a folder called BeamNG holding another game is not BeamNG: the manifest's names decide)
        d = make_game(self.path("BeamNG.drive"))
        self.write("A.item", {"DisplayName": "Other", "AppName": "other", "InstallLocation": d})
        self.assertEqual(GI.epic_installs(self.path("manifests")), [])

    def test_no_folder(self):
        self.assertEqual(GI.epic_installs(None), [])
        self.assertEqual(GI.epic_installs(self.path("nope")), [])

    def test_manifest_folder_from_programdata(self):
        self.assertEqual(GI.epic_manifest_dir({"ProgramData": r"D:\PD"}),
                         os.path.join(r"D:\PD", "Epic", "EpicGamesLauncher", "Data", "Manifests"))
        self.assertIsNone(GI.epic_manifest_dir({}))


class FindTest(Tmp):
    def steam(self, *libs):
        """A Steam folder whose libraryfolders.vdf names libs."""
        sp = self.path("Steam")
        os.makedirs(os.path.join(sp, "steamapps"), exist_ok=True)
        with open(os.path.join(sp, "steamapps", "libraryfolders.vdf"), "w") as f:
            f.write('"libraryfolders"\n{\n' + "".join(
                f'\t"{i}"\n\t{{\n\t\t"path"\t\t"{lib}"\n\t}}\n' for i, lib in enumerate(libs)) + "}\n")
        return sp

    def epic(self, root):
        os.makedirs(self.path("manifests"), exist_ok=True)
        with open(self.path("manifests", "x.item"), "w") as f:
            json.dump({"DisplayName": "BeamNG.drive", "InstallLocation": root}, f)
        return self.path("manifests")

    def test_order_dedupe_and_store(self):
        lib = self.path("SteamLib")
        steam_copy = make_game(os.path.join(lib, "steamapps", "common", "BeamNG.drive"))
        epic_copy = make_game(self.path("EpicGames", "BeamNGdrive"))
        own = make_game(self.path("Games", "BeamNG"))
        found = GI.find_windows(saved=None, ini={"installPath": epic_copy},
                                registry=[("other", own), ("rootpath", steam_copy)],
                                steam_path=self.steam(lib), manifest_dir=self.epic(epic_copy))
        # the INI first (its folder is Epic's: the store is what the copy is, not where it was found), the registry's
        # rootpath before its other values, each folder once
        self.assertEqual([(i.root, i.store, i.source) for i in found],
                         [(epic_copy, "epic", "ini"), (steam_copy, "steam", "registry"), (own, "other", "registry")])

    def test_saved_first_and_revalidated(self):
        a = make_game(self.path("A"))
        b = make_game(self.path("B"))
        found = GI.find_windows(saved=b, ini={"installPath": a})
        self.assertEqual([(i.root, i.source) for i in found], [(b, "saved"), (a, "ini")])
        # a saved folder that no longer holds the game is skipped
        self.assertEqual([i.root for i in GI.find_windows(saved=self.path("gone"), ini={"installPath": a})], [a])

    def test_epic_only(self):
        e = make_game(self.path("EpicGames", "BeamNGdrive"))
        found = GI.find_windows(manifest_dir=self.epic(e))
        self.assertEqual([(i.root, i.store, i.source) for i in found], [(e, "epic", "epic")])

    def test_invalid_ini_falls_through(self):
        # (BeamMP's launcher stops at an INI that names a missing folder; here the next source is used)
        r = make_game(self.path("R"))
        found = GI.find_windows(ini={"installPath": self.path("missing")}, registry=[("rootpath", r)])
        self.assertEqual([i.root for i in found], [r])

    def test_linux_steam_libraries(self):
        a = make_game(os.path.join(self.path("lib1"), "steamapps", "common", "BeamNG.drive"))
        os.makedirs(os.path.join(self.path("lib2"), "steamapps", "common", "BeamNG.drive"))     # (no program)
        found = GI.find_linux(saved=None, steam_roots=[self.path("lib1"), self.path("lib2")])
        self.assertEqual([(i.root, i.store) for i in found], [(a, "steam")])
        own = make_game(self.path("own"))
        self.assertEqual([(i.root, i.store, i.source) for i in GI.find_linux(own, [self.path("lib1")])],
                         [(own, "other", "saved"), (a, "steam", "steam")])


class ModeTest(unittest.TestCase):
    steam = GI.Install("/s/steamapps/common/BeamNG.drive", "steam", "steam")
    epic = GI.Install("/e/BeamNG", "epic", "epic")
    own = GI.Install("/o/BeamNG", "other", "saved")

    def test_auto(self):
        self.assertEqual(GI.resolve_mode("auto", self.steam, "windows"), ("steam", ""))
        self.assertEqual(GI.resolve_mode("auto", self.epic, "windows"), ("direct", ""))
        self.assertEqual(GI.resolve_mode("auto", self.own, "native"), ("folder", ""))
        self.assertEqual(GI.resolve_mode("auto", None, "windows"), ("steam", ""))      # (not found: as before)

    def test_chosen(self):
        self.assertEqual(GI.resolve_mode("direct", self.steam, "windows")[0], "direct")
        self.assertEqual(GI.resolve_mode("direct", self.own, "native")[0], "direct")
        self.assertEqual(GI.resolve_mode("steam", self.steam, "native")[0], "steam")
        mode, note = GI.resolve_mode("steam", self.epic, "windows")       # (Steam cannot start Epic's copy)
        self.assertEqual(mode, "direct")
        self.assertIn("not Steam's", note)

    def test_direct_without_a_folder_stays_direct(self):
        # (Codex review of ee11b37e: it became "steam", so a removed copy with direct chosen started Steam or Steam's
        # copy; it stays direct, and direct_start stops with the missing-copy message)
        for build in ("windows", "native"):
            mode, note = GI.resolve_mode("direct", None, build)
            self.assertEqual(mode, "direct")
            self.assertTrue(note)
        self.assertIsNone(GI.launch_exe(None, "direct", True))

    def test_game_mode_and_proton_as_before(self):
        self.assertEqual(GI.resolve_mode("direct", self.steam, "native", gamescope=True)[0], "steam")
        self.assertEqual(GI.resolve_mode("direct", self.steam, "proton")[0], "steam")

    def test_programs(self):
        self.assertEqual(GI.launch_exe(self.steam, "direct", True), os.path.join(self.steam.root, GI.WIN_GAME))
        self.assertEqual(GI.launch_exe(self.own, "folder", True), os.path.join(self.own.root, "BeamNG.drive.exe"))
        self.assertEqual(GI.launch_exe(self.epic, "direct", False), os.path.join(self.epic.root, GI.LINUX_GAME))
        self.assertIsNone(GI.launch_exe(self.steam, "steam", True))
        self.assertEqual(GI.direct_args("/u/B", ["-gfx", "dx11"]), ["-nosteam", "-userpath", "/u/B", "-gfx", "dx11"])
        self.assertEqual(GI.direct_args(None), ["-nosteam"])


class SettingTest(Tmp):
    def test_default_auto_kept_and_forgotten(self):
        self.assertEqual(apppaths.saved_launch_mode(), "auto")
        apppaths.set_launch_mode("direct")
        self.assertEqual(apppaths.saved_launch_mode(), "direct")
        apppaths.set_launch_mode("auto")
        self.assertEqual(apppaths.saved_launch_mode(), "auto")
        self.assertNotIn("launchMode", apppaths.app_settings())

    def test_bad_values(self):
        with self.assertRaises(ValueError):
            apppaths.set_launch_mode("epic")
        apppaths.save_app_settings(launchMode=True)         # (a hand-edited file)
        self.assertEqual(apppaths.saved_launch_mode(), "auto")

    def test_other_writers_keep_it(self):
        # the server's writes to app.json (the game's user folder, the cache folder from the settings page) keep the
        # launcher's mode: nothing in the game writes it (PR #1309's page wrote an absent value back as false)
        apppaths.set_launch_mode("direct")
        apppaths.set_user_dir(self.path("u"))
        apppaths.set_cache_dir(self.path("cache"))
        apppaths.set_game_dir(make_game(self.path("g")))
        self.assertEqual(apppaths.saved_launch_mode(), "direct")


def args(**kw):
    base = dict(launch=None, choose_game=False, no_game=False, game_dir=None)
    base.update(kw)
    return argparse.Namespace(**base)


class ResolveTest(Tmp):
    def setUp(self):
        super().setUp()
        self.steam_copy = make_game(self.path("lib", "steamapps", "common", "BeamNG.drive"))
        self.epic_copy = make_game(self.path("Epic", "BeamNG"))
        self.st = ExitStack()
        self.addCleanup(self.st.close)
        for name in ("row", "say"):
            self.st.enter_context(mock.patch.object(launcher, name))
        self.st.enter_context(mock.patch.object(launcher, "under_gamescope", return_value=False))
        self.copies = []
        self.st.enter_context(mock.patch.object(launcher, "find_installs", side_effect=self.find))

    def find(self):
        saved = apppaths.saved_game_dir()
        out = [GI.Install(saved, "steam" if "steamapps" in saved else "epic", "saved")] if saved else []
        return out + [c for c in self.copies if not saved or c.root != saved]

    def no_ask(self, prompt):
        raise AssertionError(f"asked: {prompt}")

    def test_one_steam_copy_nothing_chosen_is_as_before(self):
        self.copies = [GI.Install(self.steam_copy, "steam", "steam")]
        la = launcher.resolve_launch(args(), "/u/BeamNG.drive/current", "native", ask=self.no_ask)
        self.assertEqual((la.mode, la.chosen, la.exe, la.user_base), ("steam", "auto", None, "/u/BeamNG.drive"))
        self.assertEqual(apppaths.app_settings(), {})        # (nothing written)

    def test_several_copies_the_player_picks(self):
        self.copies = [GI.Install(self.steam_copy, "steam", "ini"), GI.Install(self.epic_copy, "epic", "epic")]
        answers = iter(["2"])
        la = launcher.resolve_launch(args(), "/u/current", "native", ask=lambda p: next(answers))
        self.assertEqual(la.install.root, self.epic_copy)
        self.assertEqual(la.mode, "direct")                  # (an Epic copy: directly)
        self.assertEqual(apppaths.saved_game_dir(), apppaths.game_folder(self.epic_copy))

    def test_picking_a_steam_copy_asks_how(self):
        self.copies = [GI.Install(self.epic_copy, "epic", "ini"), GI.Install(self.steam_copy, "steam", "steam")]
        answers = iter(["2", "d"])
        la = launcher.resolve_launch(args(), "/u/current", "native", ask=lambda p: next(answers))
        self.assertEqual((la.install.root, la.mode), (self.steam_copy, "direct"))
        self.assertEqual(apppaths.saved_launch_mode(), "direct")

    def test_several_copies_without_a_console(self):
        self.copies = [GI.Install(self.steam_copy, "steam", "ini"), GI.Install(self.epic_copy, "epic", "epic")]

        def eof(prompt):
            raise EOFError
        la = launcher.resolve_launch(args(), "/u/current", "native", ask=eof)
        self.assertEqual(la.install.root, self.steam_copy)   # (this time only, and said)
        self.assertIsNone(apppaths.saved_game_dir())
        self.assertTrue(any("--choose-game" in str(c) for c in launcher.row.call_args_list))

    def test_saved_choice_is_not_asked_again(self):
        apppaths.set_game_dir(self.epic_copy)
        self.copies = [GI.Install(self.steam_copy, "steam", "steam")]
        la = launcher.resolve_launch(args(), "/u/current", "native", ask=self.no_ask)
        self.assertEqual(la.install.root, apppaths.game_folder(self.epic_copy))

    def test_cli_mode_is_kept(self):
        self.copies = [GI.Install(self.steam_copy, "steam", "steam")]
        with mock.patch.object(launcher, "IS_WIN", True):
            la = launcher.resolve_launch(args(launch="direct"), "/u/current", "windows", ask=self.no_ask)
        self.assertEqual((la.mode, la.exe), ("direct", os.path.join(self.steam_copy, GI.WIN_GAME)))
        self.assertEqual(apppaths.saved_launch_mode(), "direct")
        la = launcher.resolve_launch(args(launch="auto"), "/u/current", "native", ask=self.no_ask)
        self.assertEqual(la.mode, "steam")

    def test_none_found_asks_for_the_folder(self):
        with mock.patch.object(launcher, "ask_game_dir", return_value=None) as agd:
            la = launcher.resolve_launch(args(), "/u/current", "native", ask=self.no_ask)
        agd.assert_called_once()
        self.assertEqual((la.install, la.mode), (None, "steam"))
        with mock.patch.object(launcher, "ask_game_dir") as agd:      # (game running / --no-game: never asked)
            launcher.resolve_launch(args(), "/u/current", "native", can_ask=False)
        agd.assert_not_called()

    def test_direct_with_the_copy_gone_never_reaches_steam(self):
        # the saved copy removed, nothing else found, the folder question skipped: the start stops, Steam untouched
        apppaths.set_launch_mode("direct")
        with mock.patch.object(launcher, "ask_game_dir", return_value=None):
            la = launcher.resolve_launch(args(), "/u/current", "windows", ask=self.no_ask)
        self.assertEqual((la.install, la.mode), (None, "direct"))
        with ExitStack() as st:
            for name in ("IS_WIN",):
                st.enter_context(mock.patch.object(launcher, name, True))
            st.enter_context(mock.patch.object(launcher, "problem"))
            st.enter_context(mock.patch.object(launcher, "under_wine", return_value=False))
            steam = st.enter_context(mock.patch.object(launcher, "windows_steam_start"))
            sf = st.enter_context(mock.patch.object(launcher.os, "startfile", create=True))
            po = st.enter_context(mock.patch.object(launcher.subprocess, "Popen"))
            with self.assertRaises(SystemExit):
                launcher.start_game(False, launch=la)
        steam.assert_not_called()
        sf.assert_not_called()
        po.assert_not_called()

    def test_game_mode_keeps_its_start(self):
        apppaths.set_launch_mode("direct")
        self.copies = [GI.Install(self.steam_copy, "steam", "ini"), GI.Install(self.epic_copy, "epic", "epic")]
        with mock.patch.object(launcher, "under_gamescope", return_value=True):
            la = launcher.resolve_launch(args(), "/u/current", "native", ask=self.no_ask)
        self.assertEqual(la.mode, "steam")


def launch(root, store, mode, windows, user="/u/BeamNG.drive"):
    inst = GI.Install(root, store, "saved")
    return GI.Launch(inst, "windows" if windows else "native", user, mode, mode, GI.launch_exe(inst, mode, windows))


class StartTest(Tmp):
    def setUp(self):
        super().setUp()
        self.root = make_game(self.path("Games", "BeamNG.drive"))
        self.st = ExitStack()
        self.addCleanup(self.st.close)
        p = lambda name, **kw: self.st.enter_context(mock.patch.object(launcher, name, **kw))
        for name in ("row", "say", "problem"):
            p(name)
        p("DIRECT_FAIL_S", new=0.05)
        p("gcpatch_lib", return_value=None)
        p("steam_runtime", return_value=None)
        p("sandboxed_game", return_value=False)
        p("under_wine", return_value=False)
        self.steam_win = p("windows_steam_start", return_value=None)
        self.steam_lin = p("steam_launch")
        self.st.enter_context(mock.patch.dict(os.environ, {"BNGE_GAME_ARGS": "", "BNGE_GAME_WRAPPER": "",
                                                           "SteamAppId": "284160"}))

    def popen(self, alive=True):
        proc = mock.Mock(returncode=None if alive else 3)
        proc.poll.return_value = None if alive else 3
        return self.st.enter_context(mock.patch.object(launcher.subprocess, "Popen", return_value=proc))

    def test_windows_direct_command(self):
        self.st.enter_context(mock.patch.object(launcher, "IS_WIN", True))
        po = self.popen()
        launcher.start_game(False, gfx="dx11", user_dir="/u/BeamNG.drive/current",
                            launch=launch(self.root, "steam", "direct", True))
        argv, kw = po.call_args[0][0], po.call_args[1]
        self.assertEqual(argv, [os.path.join(self.root, "Bin64", "BeamNG.drive.x64.exe"), "-nosteam", "-userpath",
                                "/u/BeamNG.drive", "-gfx", "dx11"])
        self.assertEqual(kw["cwd"], self.root)
        self.steam_win.assert_not_called()

    def test_windows_direct_without_renderer(self):
        self.st.enter_context(mock.patch.object(launcher, "IS_WIN", True))
        po = self.popen()
        launcher.start_game(False, launch=launch(self.root, "epic", "direct", True))
        self.assertNotIn("-gfx", po.call_args[0][0])

    def test_missing_program_never_falls_back_to_steam(self):
        self.st.enter_context(mock.patch.object(launcher, "IS_WIN", True))
        os.remove(os.path.join(self.root, GI.WIN_GAME))
        po = self.popen()
        with self.assertRaises(SystemExit):
            launcher.start_game(False, launch=launch(self.root, "steam", "direct", True))
        po.assert_not_called()
        self.steam_win.assert_not_called()

    def test_early_exit_is_said_not_steam(self):
        for windows in (True, False):
            with self.subTest(windows=windows), mock.patch.object(launcher, "IS_WIN", windows):
                self.popen(alive=False)
                launcher.problem.reset_mock()
                with self.assertRaises(SystemExit):
                    launcher.start_game(False, launch=launch(self.root, "epic", "direct", windows))
                self.assertIn("Epic Games Launcher", launcher.problem.call_args[0][1])
                self.steam_win.assert_not_called()
                self.steam_lin.assert_not_called()

    def test_linux_direct_without_steam_running(self):
        self.st.enter_context(mock.patch.object(launcher, "IS_WIN", False))
        self.st.enter_context(mock.patch.object(launcher, "steam_running", return_value=False))
        po = self.popen()
        launcher.start_game(False, user_dir="/u/BeamNG.drive/current", steam_args=({"X": "1"}, ["gamemoderun"], ["-o"]),
                            launch=launch(self.root, "steam", "direct", False))
        argv, env = po.call_args[0][0], po.call_args[1]["env"]
        self.assertEqual(argv, ["gamemoderun", os.path.join(self.root, GI.LINUX_GAME), "-nosteam", "-userpath",
                                "/u/BeamNG.drive", "-o"])
        self.assertNotIn("SteamAppId", env)
        self.assertEqual(env["X"], "1")
        self.steam_lin.assert_not_called()

    def test_folder_mode_windows_as_before(self):
        self.st.enter_context(mock.patch.object(launcher, "IS_WIN", True))
        po = self.popen()
        launcher.start_game(False, launch=launch(self.root, "other", "folder", True))
        self.assertEqual(po.call_args[0][0], [os.path.join(self.root, "BeamNG.drive.exe")])
        self.steam_win.assert_not_called()

    def test_steam_mode_windows_unchanged(self):
        self.st.enter_context(mock.patch.object(launcher, "IS_WIN", True))
        po = self.popen()
        launcher.start_game(False, launch=launch(self.root, "steam", "steam", True))
        self.steam_win.assert_called_once()
        po.assert_not_called()

    def test_steam_mode_linux_unchanged(self):
        # (Steam not running: through Steam, as before; running: the native program with Steam's ids, as before)
        self.st.enter_context(mock.patch.object(launcher, "IS_WIN", False))
        with mock.patch.object(launcher, "steam_running", return_value=False):
            po = self.popen()
            launcher.start_game(False, launch=launch(self.root, "steam", "steam", False))
            po.assert_not_called()
            self.steam_lin.assert_called_once()
        with mock.patch.object(launcher, "steam_running", return_value=True):
            po = self.popen()
            launcher.start_game(False, user_dir="/u/BeamNG.drive/current",
                                launch=launch(self.root, "steam", "steam", False))
            argv, env = po.call_args[0][0], po.call_args[1]["env"]
            self.assertNotIn("-nosteam", argv)
            self.assertEqual(env["SteamAppId"], launcher.APP_ID)


class BeammpBesideEveryModeTest(Tmp):
    def run_up(self, windows, la):
        a = argparse.Namespace(no_beammp=False, beammp_wait=1)
        with ExitStack() as st:
            st.enter_context(mock.patch.object(launcher, "IS_WIN", windows))
            for owner, name, value in ((launcher, "under_wine", False), (launcher, "data_dir", self.dd),
                                       (bmplaunch, "running", False), (bmplaunch, "on_launch", True),
                                       (bmplaunch, "find", "/b/BeamMP-Launcher"), (bmplaunch, "rebuild_due", False),
                                       (bmplaunch, "system_too_old", None), (bmplaunch, "wait_ready", False)):
                st.enter_context(mock.patch.object(owner, name, return_value=value))
            st.enter_context(mock.patch.object(bmplaunch, "stop"))
            start = st.enter_context(mock.patch.object(bmplaunch, "start"))
            row = st.enter_context(mock.patch.object(launcher, "row"))
            launcher.beammp_up("/u/BeamNG.drive/current", "native", a, la)
        return start, row

    def test_linux_outside_steam_said_plainly(self):
        for store in ("other", "epic"):
            start, row = self.run_up(False, launch("/o", store, "direct", False))
            start.assert_not_called()
            text = " ".join(str(c) for c in row.call_args_list)
            self.assertIn("Steam library", text)
            self.assertIn("Single player", text)

    def test_linux_steam_copy_without_steam_keeps_multiplayer(self):
        start, _ = self.run_up(False, launch("/s/steamapps/common/BeamNG.drive", "steam", "direct", False))
        start.assert_called_once()
        self.assertFalse(start.call_args.kwargs["user_path"])        # (Linux names it anyway: bmplaunch.start)

    def test_windows_direct_names_the_user_folder(self):
        start, _ = self.run_up(True, launch("C:/G", "epic", "direct", True))
        self.assertTrue(start.call_args.kwargs["user_path"])
        start, _ = self.run_up(True, launch("C:/G", "steam", "steam", True))
        self.assertFalse(start.call_args.kwargs["user_path"])

    def test_same_user_base_for_beammp_and_the_game(self):
        # (Codex review of 904ac751: a folder of the player's own whose name starts with a digit went to the game as
        # it is but to BeamMP as its parent: BeamMP's mod in another folder)
        for user in ("/g/2026-profile", "/g/BeamNG.drive/current", "/g/BeamNG.drive/0.40", "/g/mine", "/g/7"):
            self.assertEqual(bmplaunch.game_user_path(user), GI.user_base(user), user)
        self.assertEqual(GI.user_base("/g/2026-profile"), os.path.normpath("/g/2026-profile"))

    def test_bmplaunch_user_path_on_windows(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(bmplaunch, "IS_WIN", True), \
                mock.patch.object(bmplaunch.subprocess, "Popen") as popen, \
                mock.patch.object(bmplaunch, "tie_to_app"), \
                mock.patch.object(bmplaunch.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True):
            user = os.path.join(d, "BeamNG.drive", "current")
            bmplaunch.start(os.path.join(d, "BeamMP-Launcher.exe"), user, os.path.join(d, "l.log"), user_path=True)
            self.assertEqual(popen.call_args[0][0][1:], ["--no-launch", "--user-path", os.path.join(d, "BeamNG.drive")])
            bmplaunch.start(os.path.join(d, "BeamMP-Launcher.exe"), user, os.path.join(d, "l.log"))
            self.assertEqual(popen.call_args[0][0][1:], ["--no-launch"])


class CliTest(unittest.TestCase):
    def test_launch_flag(self):
        src = (ROOT / "launcher.py").read_text(encoding="utf-8")
        self.assertIn('"--launch", choices=["auto", "steam", "direct"]', src)
        self.assertIn('"--choose-game"', src)


if __name__ == "__main__":
    unittest.main()
