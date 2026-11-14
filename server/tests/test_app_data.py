"""Every data file the server ships in git goes into the frozen app (tools/build_app.py --add-data): 1.8.0's exe left
out server/data/billboards.npz (a "billboards.npz is missing" warning per worker, no mapped billboards)."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools import build_app

NOT_DATA = {"server/README.md", "server/requirements.txt", "server/earthfast.abi3.so", "server/earthfast.pyd"}


class AppDataTest(unittest.TestCase):
    def test_every_tracked_server_data_file_is_bundled(self):
        files = subprocess.run(["git", "ls-files", "server"], cwd=ROOT, capture_output=True, text=True,
                               check=True).stdout.split()
        data = [f for f in files if not f.endswith(".py") and not f.startswith("server/tests/")
                and "/__" not in f and f not in NOT_DATA]
        self.assertTrue(data)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(build_app.subprocess, "run") as run:
            build_app.pyinstaller(tmp, "icon.ico", onefile=True, ver="0.0.0")
        command = run.call_args.args[0]
        sources = [os.path.realpath(command[i + 1].split(build_app.SEP, 1)[0])
                   for i, a in enumerate(command[:-1]) if a == "--add-data"]
        for f in data:
            p = os.path.realpath(ROOT / f)
            covered = any(p == s or p.startswith(s + os.sep) for s in sources)
            self.assertTrue(covered, f"{f} is not in tools/build_app.py's --add-data list")


if __name__ == "__main__":
    unittest.main()
