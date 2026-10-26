"""shops: real repair shops and property near the player, with property prices."""
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mapgeom as mg  # noqa: E402
import shops  # noqa: E402
import jobfind  # noqa: E402
from test_gasstations import FakeMD, feats  # noqa: E402
from test_jobfind import LAT0, LON0, place, spot  # noqa: E402


class ShopsTest(unittest.TestCase):
    def setUp(self):
        self.md = FakeMD({"places": feats("places", [
            place("r1", 300.0, 0.0, name="Joe's Auto"), place("r2", 900.0, 0.0, cat="auto_body_shop", name="Body Pro"),
            place("s1", 500.0, 0.0, cat="self_storage", name="Public Storage"),
            place("p1", 700.0, 0.0, cat="parking_garage", name="Main St Garage"),
            place("c1", 100.0, 0.0, cat="cafe", name="Cafe"),
            place("far", 9000.0, 0.0, name="Too Far Auto")], mg.POINT)})

    def test_repair_shops(self):
        got = shops.near(self.md, LAT0, LON0, "repair", spot_fn=spot)
        self.assertEqual([g["name"] for g in got], ["Joe's Auto", "Body Pro"])
        self.assertLess(got[0]["dist"], got[1]["dist"])
        self.assertNotIn("buy", got[0])
        import jobfind
        for g in got:                       # the garage spot off the road, toward the business
            off = jobfind.dist_m(g["lat"], g["lon"], g["mlat"], g["mlon"])
            self.assertTrue(shops.MARK_MIN_M - 0.1 <= off <= shops.MARK_MAX_M + 0.1, off)

    def test_property_with_prices(self):
        got = shops.near(self.md, LAT0, LON0, "property", spot_fn=spot)
        self.assertEqual([g["name"] for g in got], ["Public Storage", "Main St Garage"])
        for g in got:
            self.assertTrue(18000 <= g["buy"] <= 60000 and 400 <= g["rent"] <= 1200)
        self.assertEqual(shops.prices("s1"), shops.prices("s1"))


if __name__ == "__main__":
    unittest.main()


class DeskOnWallTest(unittest.TestCase):
    """The bench against the property's wall facing the road, the ring in front of it."""
    BOX = [(0, 0), (20, 0), (20, 10), (0, 10), (0, 0)]       # counter-clockwise, 20 x 10 m

    def test_south_wall(self):
        (x, y), yaw, (rx, ry) = shops.desk_on_wall([self.BOX], (10, 5), (10, -15))
        self.assertAlmostEqual(x, 10, places=6)
        self.assertAlmostEqual(y, -(shops.BENCH_DEPTH / 2 + shops.WALL_GAP), places=6)
        self.assertAlmostEqual(yaw, 0.0, places=6)            # its back (+y) to the wall, north
        self.assertAlmostEqual(ry, y - shops.RING_OUT_M, places=6)

    def test_east_wall_clockwise_and_clamped(self):
        cw = self.BOX[::-1]
        (x, y), yaw, (rx, ry) = shops.desk_on_wall([cw], (10, 5), (40, 12))
        self.assertGreater(x, 20)
        self.assertLessEqual(y, 10 - (shops.BENCH_LEN / 2 + 0.3) + 1e-9)    # the bench kept on the wall
        self.assertAlmostEqual(math.cos(yaw), 0, places=6)   # back facing west
        self.assertAlmostEqual(-math.sin(yaw), -1, places=6)
        self.assertGreater(rx, x)

    def test_nearest_building_and_none(self):
        far = [(200, 200), (230, 200), (230, 220), (200, 220)]
        got = shops.desk_on_wall([far, self.BOX], (25, 5), (10, -15))
        self.assertIsNotNone(got)
        self.assertLess(got[0][0], 21)
        self.assertIsNone(shops.desk_on_wall([far], (0, 0), (0, -10)))
        shed = [(0, 0), (4, 0), (4, 4), (0, 4)]
        self.assertIsNone(shops.desk_on_wall([shed], (2, 2), (2, -10)))

    def test_wall_spot_from_map_data(self):
        from test_gasstations import FRAME
        la, lo, _ = FRAME.enu_to_geodetic(500.0, 0.0, 0.0)
        md = FakeMD({"buildings": feats("buildings", [("b1", [(490, -5), (510, -5), (510, 5), (490, 5), (490, -5)], {})],
                                         mg.POLYGON)})
        rla, rlo, _ = FRAME.enu_to_geodetic(500.0, -20.0, 0.0)
        w = shops.wall_spot(md, float(rla), float(rlo), float(la), float(lo))
        self.assertIsNotNone(w)
        d = jobfind.dist_m(w["dlat"], w["dlon"], float(la), float(lo))
        self.assertAlmostEqual(d, 5 + shops.BENCH_DEPTH / 2 + shops.WALL_GAP, delta=0.3)
        self.assertAlmostEqual(w["dyaw"], 0.0, places=2)
