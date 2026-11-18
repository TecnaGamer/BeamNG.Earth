"""v0.7 Lua-only installation and complete rollback checks."""
from pathlib import Path
import hashlib
import shutil
import subprocess
import sys
import tempfile
import unittest


PACK = Path(__file__).resolve().parents[1]
WORK = PACK.parent
SCRIPT = PACK / "apply.py"
TREES = Path("mod/beamng_earth/lua/ge/extensions/beamngEarth/trees.lua")
VEG = Path("server/vegetation.py")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestReversibleNorwayBackdrop(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for rel in (TREES, VEG):
            dest = self.root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(WORK / "norway_foliage_v06" / rel, dest)
        self.roads = self.root / "server/roads.py"
        self.roads.write_text("USER NVDB ROAD EDIT", encoding="utf-8")

    def cli(self, mode, success=True):
        run = subprocess.run(
            [sys.executable, str(SCRIPT), mode, "--root", str(self.root)],
            capture_output=True, text=True,
        )
        if success:
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        else:
            self.assertNotEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("STOPPED safely", run.stderr)
        return run.stdout + run.stderr

    def test_install_then_undo_is_byte_exact(self):
        before_tree, before_server = digest(self.root / TREES), digest(self.root / VEG)
        self.assertIn("ready", self.cli("--check"))
        self.cli("--install")
        self.assertEqual(digest(self.root / TREES), digest(PACK / TREES))
        self.assertEqual(digest(self.root / VEG), before_server)
        self.assertEqual(self.roads.read_text(encoding="utf-8"), "USER NVDB ROAD EDIT")
        self.cli("--install")  # idempotent
        self.cli("--undo")
        self.assertEqual(digest(self.root / TREES), before_tree)
        self.assertEqual(digest(self.root / VEG), before_server)
        self.cli("--install")
        self.cli("--undo")
        self.assertEqual(digest(self.root / TREES), before_tree)
        self.assertEqual(self.roads.read_text(encoding="utf-8"), "USER NVDB ROAD EDIT")

    def test_refuses_unknown_trees_and_server(self):
        (self.root / TREES).write_text("-- user edited trees", encoding="utf-8")
        self.cli("--install", success=False)
        self.assertFalse((self.root / "norway_foliage_v07_backup").exists())
        shutil.copy2(WORK / "norway_foliage_v06" / TREES, self.root / TREES)
        (self.root / VEG).write_text("# another version", encoding="utf-8")
        self.cli("--install", success=False)
        self.assertFalse((self.root / "norway_foliage_v07_backup").exists())

    def test_refuses_to_erase_subsequent_user_edits(self):
        self.cli("--install")
        edited = "-- user changed trees after v0.7"
        (self.root / TREES).write_text(edited, encoding="utf-8")
        self.cli("--undo", success=False)
        self.cli("--install", success=False)
        self.assertEqual((self.root / TREES).read_text(encoding="utf-8"), edited)


if __name__ == "__main__":
    unittest.main()
