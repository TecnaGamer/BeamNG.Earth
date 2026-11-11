"""The container's entrypoint (docker/entrypoint.sh), run against a stand-in of the image's folders: the config and the
plugin's data in /data, the plugin's settings from BNGE_*, a bad value stopping the start before anything changes."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
ENTRY = ROOT / "docker/entrypoint.sh"
MAIN_LUA = ROOT / "beammp/Server/BeamNGEarth/main.lua"


@unittest.skipUnless(os.name == "posix" and shutil.which("sh"), "the entrypoint is a POSIX shell script")
class EntrypointTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root, self.data = Path(tmp.name, "beammp"), Path(tmp.name, "data")
        plugin = self.root / "Resources/Server/BeamNGEarth"
        plugin.mkdir(parents=True)
        self.data.mkdir()
        shutil.copy(MAIN_LUA, plugin / "main.lua")
        self.main_lua = plugin / "main.lua"
        (self.root / "ServerConfig.default.toml").write_text('[General]\nAuthKey = ""\n')
        server = self.root / "BeamMP-Server"     # (stands in for BeamMP: says where it runs and with what)
        server.write_text('#!/bin/sh\necho "started in $(pwd) $*"\n')
        server.chmod(0o755)

    def run_entry(self, **env):
        return subprocess.run(["sh", str(ENTRY), "--flag"], capture_output=True, text=True, timeout=30,
                              env={"PATH": os.environ["PATH"], "BNGE_ROOT": str(self.root),
                                   "BNGE_DATA": str(self.data), **env})

    def setting(self, name):
        return re.search(rf"^G\.{name} = (.*)$", self.main_lua.read_text(), re.M).group(1)

    def test_first_start_seeds_the_config_and_data_in_data(self):
        r = self.run_entry()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), f"started in {self.root} --flag")
        self.assertEqual((self.data / "ServerConfig.toml").read_text(), '[General]\nAuthKey = ""\n')
        self.assertEqual(os.readlink(self.root / "ServerConfig.toml"), str(self.data / "ServerConfig.toml"))
        for d in ("store", "bake", "spool"):
            self.assertTrue((self.data / d).is_dir(), d)
            self.assertEqual(os.readlink(self.root / "Resources/Server/BeamNGEarth" / d), str(self.data / d))
        self.assertEqual(self.setting("TIME"), "'real'")     # (no BNGE_*: main.lua as shipped)

    def test_a_config_in_data_is_kept_and_a_removed_one_comes_back(self):
        (self.data / "ServerConfig.toml").write_text("mine\n")
        self.assertEqual(self.run_entry().returncode, 0)
        self.assertEqual((self.data / "ServerConfig.toml").read_text(), "mine\n")
        (self.data / "ServerConfig.toml").unlink()           # (a restart then finds the template again)
        self.assertEqual(self.run_entry().returncode, 0)
        self.assertEqual((self.data / "ServerConfig.toml").read_text(), '[General]\nAuthKey = ""\n')

    def test_bnge_settings_go_into_main_lua(self):
        r = self.run_entry(BNGE_TIME="20:30", BNGE_TIME_SPEED="0.5", BNGE_WEATHER="rain", BNGE_LISTED="false")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([self.setting(n) for n in ("TIME", "TIME_SPEED", "WEATHER", "LISTED")],
                         ["'20:30'", "0.5", "'rain'", "false"])

    def test_a_bad_value_stops_the_start_before_anything_changes(self):
        shipped = self.main_lua.read_text()
        for name, value in (("BNGE_TIME", "29:59"), ("BNGE_TIME_SPEED", "fast"), ("BNGE_WEATHER", "hail"),
                            ("BNGE_LISTED", "maybe"), ("BNGE_WEATHER", "rain'; os.exit() --"),
                            ("BNGE_WEATHER", "rain\nx")):
            r = self.run_entry(**{name: value})
            self.assertEqual(r.returncode, 2, (name, value))
            self.assertIn(name, r.stderr)
            self.assertNotIn("started", r.stdout)
            self.assertEqual(self.main_lua.read_text(), shipped)
            self.assertEqual(list(self.data.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
