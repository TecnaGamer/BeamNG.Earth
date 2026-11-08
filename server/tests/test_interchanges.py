"""The interchange replays (tests/interchanges.py) run offline, and what they measure today holds.

The solver switch (BNE_INTERCHANGE, crossings.ON) is read at import, so each mode runs in its own process.
"""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import interchanges  # noqa: E402


def replay(name, interchange):
    env = dict(os.environ)
    env.pop("BNE_INTERCHANGE", None)
    if interchange:
        env["BNE_INTERCHANGE"] = "1"
    out = subprocess.run([sys.executable, str(HERE / "interchanges.py"), name], env=env, capture_output=True,
                         text=True, timeout=600, check=True).stdout
    return json.loads(out.strip().splitlines()[-1])


class FixturesTest(unittest.TestCase):
    def test_every_site_loads(self):
        for name in interchanges.SITES:
            with self.subTest(name):
                site = interchanges.load(name)
                self.assertTrue(site.cells)
                z = site.sample([site.origin[0]], [site.origin[1]])
                self.assertTrue(abs(float(z[0])) < 9000)


class SilistraTest(unittest.TestCase):
    """GitHub #936: the bridge at Aydemir pitches at ~43 % without the solver; with it, under 10 %."""

    def test_without_the_solver_the_deck_is_impassable(self):
        r = replay("silistra_936", interchange=False)
        self.assertFalse(r["interchange"])
        self.assertEqual(r["decks"], 1)
        self.assertGreater(r["max_grade"], 0.3)

    def test_the_solver_levels_it(self):
        r = replay("silistra_936", interchange=True)
        self.assertTrue(r["interchange"])
        self.assertEqual(r["decks"], 1)
        self.assertLess(r["max_grade"], 0.1)
        self.assertLess(r["max_grade_jump"], 0.05)


if __name__ == "__main__":
    unittest.main()
