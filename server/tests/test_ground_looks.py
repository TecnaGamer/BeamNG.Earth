"""The ground layers' looks agree in both places that define them (2026-11-16: lawn had a 1 m detail repeat in terrain.lua
and 2 m in the level, the level's dirt no detail repeat and a 128 m macro): the level's main.materials.json (made by
tools/make_level_assets.py; our level uses it) and terrain.lua's M.LOOKS (other levels make the layers at runtime from
it), every texture, repeat (metres there, 1 m samples here), strength and distance."""
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[2]
LEVEL_JSON = ROOT / "mod" / "beamng_earth" / "levels" / "beamng_earth" / "art" / "terrains" / "main.materials.json"
LUAJIT = shutil.which("luajit")
LEVEL_SQUARE = 1.0          # terrain.lua M.LEVEL_SQUARE: the level's sizes are samples of 1 m


def _num_list(v):
    return [float(x) for x in str(v).split()]


@unittest.skipUnless(LUAJIT, "luajit")
class GroundLooksTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        out = subprocess.run([LUAJIT, str(ROOT / "mod" / "tests" / "dump_ground_looks.lua"), "all"],
                             capture_output=True, text=True, check=True, timeout=60).stdout
        cls.looks = {}
        for line in out.splitlines():
            table, name, field, value = line.split("\t")
            if table == "LOOKS":
                cls.looks.setdefault(name, {})[field] = value
        cls.level = {n: v for n, v in json.loads(LEVEL_JSON.read_text()).items()
                     if isinstance(v, dict) and v.get("class") == "TerrainMaterial"}

    def test_same_materials(self):
        self.assertEqual(set(self.looks), set(self.level))

    def test_every_field_agrees(self):
        for name, look in sorted(self.looks.items()):
            mat = self.level[name]
            for field, v in sorted(look.items()):
                with self.subTest(material=name, field=field):
                    self.assertIn(field, mat)
                    want = mat[field]
                    if field.endswith("TexSize"):
                        self.assertEqual(int(want), max(1, int(float(v) / LEVEL_SQUARE + 0.5)))
                    elif isinstance(want, list):
                        self.assertEqual([round(x, 6) for x in want], [round(x, 6) for x in _num_list(v)])
                    else:
                        self.assertEqual(str(want), v)

    def test_repeats_divide_the_block_step(self):
        """Every detail / macro repeat divides the 128 m block step (or the pattern jumps at every block move)."""
        for name, mat in self.level.items():
            for field, v in mat.items():
                if field.endswith(("DetailTexSize", "MacroTexSize")):
                    with self.subTest(material=name, field=field):
                        self.assertEqual(128 % (v * LEVEL_SQUARE), 0, v)

    def test_macro_gone_before_the_block_edge(self):
        """The macro fades to nothing by 200 m (macroDistAtten [., 1]): the far tiles past the block's edge (>= 229 m
        from the player) show the photo alone."""
        for name, mat in self.level.items():
            with self.subTest(material=name):
                self.assertLessEqual(mat["macroDistances"][3], 220)
                self.assertEqual(mat["macroDistAtten"][1], 1)


if __name__ == "__main__":
    unittest.main()
