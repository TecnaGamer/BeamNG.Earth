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



class OverpassHeadroomTest(unittest.TestCase):
    """GitHub #960: the M1's overpass near Dundalk ends on the carriageways' 3 m fork stubs; the motorway under it now
    dips as under a single span (grading.UNDER_STUB_M): 3.3 m of road over road before, over 6 m now."""

    def test_the_m1_clears_its_overpass(self):
        r = replay("m1_ireland_960", interchange=False)
        self.assertGreater(r["min_gap"], 6.0)
        self.assertEqual(r["low"], [])



class JunctionGapTest(unittest.TestCase):
    """GitHub #915: at Tonkin Highway x Morley Drive a trunk road ends 1.7 m short of its junction; at 0.5 m it was
    left out of the underpass's dip and stood as 40-90 % steps where it met the lowered roads (grading.UNDER_NODE_M)."""

    def test_no_steps_round_the_interchange(self):
        r = replay("perth_915", interchange=False)
        self.assertLess(r["road_max_grade"], 0.25)
        self.assertEqual(r["low"], [])


if __name__ == "__main__":
    unittest.main()
