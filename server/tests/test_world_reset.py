"""world_reset (Connection.forget_world): the mod's level started again after another level in the same game, so it has
none of the tiles this connection sent. The geotile trackers keep what they sent across anchors and the far field
keeps its global tiles at a commit: after a stock map and back, the roads and buildings stayed missing until a teleport
(game perf report 2026-11-12, #10). Every tracker forgets, and the next commit starts with no previous frame."""
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import earthd  # noqa: E402
import vegetation  # noqa: E402


class WorldResetTest(unittest.TestCase):
    def test_every_tracker_forgets(self):
        veg = vegetation.Tracker(geo=True)
        veg.sent = {(1, 2), (3, 4)}
        veg.aid = 7
        far = vegetation.Tracker(kind="bldfar", geo=True)
        far.sent = {(5, 6)}
        roads = SimpleNamespace(closed=False, close=lambda: setattr(roads, "closed", True))
        conn = SimpleNamespace(road_geo=True, roads=roads, signs=earthd.signstream.Tracker(), veg=veg, bldfar=far,
                               bldsky=None, brfar=None, turbines=vegetation.Tracker(kind="turbines", geo=True),
                               distant_traffic=vegetation.Tracker(kind="traffic", geo=True),
                               distant_traffic_far=vegetation.Tracker(kind="traffic_far", geo=True),
                               nav=earthd.navgraphmod.Tracker(), active=7, shown={(earthd.GLOBAL_AID, "g"): None},
                               sent={"g": 1}, desired={"g": 1}, groups=[1], queue=[1], qi=1, refined={1},
                               node_changed={1: 0}, failed={"x"})
        conn.signs.sent = {(0, 0)}
        earthd.Connection.forget_world(conn)
        self.assertTrue(roads.closed)
        self.assertIsInstance(conn.roads, earthd.roadsmod.GeoTracker)      # (road_geo: a fresh geotile tracker)
        self.assertEqual(conn.roads.sent, set())
        self.assertEqual(conn.signs.sent, set())
        self.assertEqual((veg.sent, veg.aid, far.sent), (set(), None, set()))
        self.assertIsNone(conn.active)
        self.assertEqual((conn.shown, conn.sent, conn.desired, conn.groups, conn.queue, conn.qi), ({}, {}, {}, [], [], 0))
        self.assertEqual((conn.refined, conn.node_changed, conn.failed), (set(), {}, set()))


if __name__ == "__main__":
    unittest.main()
