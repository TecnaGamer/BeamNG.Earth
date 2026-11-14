"""The global index must travel with both source releases and frozen applications."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools import build_app, package


class TurbinePackagingTest(unittest.TestCase):
    def test_source_and_frozen_packages_include_index_provenance_and_client(self):
        source = set(package.collect())
        for path in ("server/wind_turbines.npz", "server/wind_turbines.provenance.json", "server/windturbines.py",
                     "mod/beamng_earth/lua/ge/extensions/beamngEarth/windturbines.lua",
                     "mod/beamng_earth/lua/ge/extensions/beamngEarth/turbinestream.lua"):
            self.assertIn(path, source)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(build_app.subprocess, "run") as run:
            build_app.pyinstaller(tmp, "icon.ico", onefile=True, ver="0.0.0")
        command = run.call_args.args[0]
        data = [command[i + 1].split(build_app.SEP, 1) for i, arg in enumerate(command[:-1]) if arg == "--add-data"]
        files = {Path(path).name: destination for path, destination in data}
        self.assertEqual(files["wind_turbines.npz"], ".")
        self.assertEqual(files["wind_turbines.provenance.json"], ".")
        self.assertEqual(files["wind_turbine_phase1.json"], ".")


if __name__ == "__main__":
    unittest.main()
