"""railways.pier_obstacles (earthfast rail_pier_obstacles): a motorway's or trunk road's piers keep off its shoulder
(grading.shoulder) as well as its carriageway (the Norfolk Southern bridges over I-75 / I-85 at 17th Street, Atlanta:
their piers stood on the shoulder the road is drawn over); a street's keep to its carriageway and sidewalks as before;
BNE_PIER_SHOULDER=off: the carriageway only."""
import os
import sys
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import fast  # noqa: E402
import grading  # noqa: E402
import railways  # noqa: E402
import roads  # noqa: E402
from test_roads import FRAME, features  # noqa: E402


def halves(cls):
    """{road index: the obstacle half widths pier_obstacles gives its segments} for a rail bridge over a road of cls."""
    f = features([[(-300.0, 100.0), (0.0, 100.0)], [(0.0, 100.0), (300.0, 100.0)],
                  [(100.0, -100.0), (100.0, 300.0)]],
                 ["standard_gauge", "standard_gauge", cls],
                 subtypes=["rail", "rail", "road"], flags=[(1, "is_bridge")])
    oa, ob, half = railways.pier_obstacles(FRAME, [f], roads.tile_bbox(0, 0))
    return f, np.unique(np.round(half, 6))


@unittest.skipUnless(fast.AVAILABLE and getattr(fast._ef, "VERSION", 0) >= 623,
                     "earthfast 623 (rail_pier_obstacles: the verge beside a motorway)")
class PierShoulderTest(unittest.TestCase):
    def test_a_motorways_piers_keep_off_its_shoulder(self):
        with mock.patch.object(railways, "PIER_SHOULDER_ON", True):
            f, h = halves("motorway")
        sp = roads.road_specs(f)[2]
        self.assertGreater(grading.shoulder(sp), 0.0)
        self.assertEqual(list(h), [round(sp.width / 2 + grading.shoulder(sp), 6)])

    def test_the_switch_keeps_the_carriageway_only(self):
        with mock.patch.object(railways, "PIER_SHOULDER_ON", False):
            f, h = halves("motorway")
        self.assertEqual(list(h), [round(roads.road_specs(f)[2].width / 2, 6)])

    def test_a_street_keeps_its_carriageway_and_sidewalks(self):
        with mock.patch.object(railways, "PIER_SHOULDER_ON", True):
            f, h = halves("secondary")
        sp = roads.road_specs(f)[2]
        side = float(sp.walk or 0.0) if roads.has_sidewalks(sp) else 0.0
        self.assertEqual(list(h), [round(sp.width / 2 + side, 6)])


if __name__ == "__main__":
    unittest.main()
