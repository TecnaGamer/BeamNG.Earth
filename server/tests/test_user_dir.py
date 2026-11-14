"""The game's own user folder (hello.userPath) against the server's (GitHub #770: the game used another BeamNG user
folder, never found the world's files: the car on a flat plane, "loading terrain" for good)."""
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
import apppaths  # noqa: E402
import earthd  # noqa: E402


class Conn:
    def __init__(self):
        self.sent = []

    def send(self, m):
        self.sent.append(m)


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ours = os.path.join(self.tmp.name, "ours", "current")
        self.theirs = os.path.join(self.tmp.name, "Mikołaj", "Documents", "BeamNG.drive", "current")
        for d in (self.ours, self.theirs):
            os.makedirs(os.path.join(d, "mods"))
        self.dd = os.path.join(self.tmp.name, "data")
        patch = mock.patch.object(apppaths, "data_dir", return_value=self.dd)
        patch.start()
        self.addCleanup(patch.stop)

    def check(self, game):
        srv = mock.Mock(user_dir=self.ours)
        conn = Conn()
        earthd.Server.check_user_dir(srv, conn, game)
        return conn.sent

    def test_the_same_folder_says_nothing(self):
        self.assertEqual(self.check(self.ours + os.sep), [])
        self.assertEqual(self.check(None), [])
        self.assertIsNone(apppaths.saved_user_dir())

    def test_another_folder_is_told_and_saved(self):
        sent = self.check(self.theirs.replace(os.sep, "/") + "/")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["type"], "user_dir_mismatch")
        self.assertEqual(os.path.normpath(sent[0]["game"]), os.path.normpath(self.theirs))
        self.assertEqual(os.path.normpath(apppaths.saved_user_dir()), os.path.normpath(self.theirs))

    def test_a_folder_we_cannot_see_is_not_judged(self):
        """Proton's C:/users/steamuser/..., a Flatpak sandbox's home: not the host's paths."""
        self.assertEqual(self.check("C:/users/steamuser/AppData/Local/BeamNG/BeamNG.drive/current/"), [])
        self.assertEqual(self.check(os.path.join(self.tmp.name, "nowhere")), [])
        self.assertIsNone(apppaths.saved_user_dir())

    def test_the_launcher_starts_with_the_game_s_folder(self):
        import launcher
        apppaths.set_user_dir(self.theirs)
        with mock.patch.object(launcher.apppaths, "data_dir", return_value=self.dd):
            self.assertEqual(os.path.normpath(launcher.user_folder()), os.path.normpath(self.theirs))


class SteamUserPathTest(unittest.TestCase):
    def test_the_launch_options_user_folder(self):
        """A -userpath in Steam's launch options wins in the game (Discord 2026-11-05: "the fix was removing the custom
        launch option from steam"): the launcher uses it too."""
        import launcher
        with tempfile.TemporaryDirectory() as d:
            base = os.path.join(d, "My Games", "BeamNG.drive")
            os.makedirs(os.path.join(base, "current"))
            self.assertEqual(launcher.steam_user_path(f'-userpath "{base}" -gfx dx11'), os.path.join(base, "current"))
            plain = os.path.join(d, "plain")
            os.makedirs(plain)
            self.assertEqual(launcher.steam_user_path(f"-gfx dx11 -userpath {plain}"), plain)
        for line in (None, "", "-gfx dx11", "-userpath /no/such/folder"):
            self.assertIsNone(launcher.steam_user_path(line))


if __name__ == "__main__":
    unittest.main()
