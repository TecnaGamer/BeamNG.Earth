"""Surveyed camera geometry and streaming rows."""
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fences
import geodesy
import radars
import roads
import signroads


class RadarTest(unittest.TestCase):
    def setUp(self):
        self.frame = geodesy.EnuFrame(48.8, 7.78)

    def site(self, x, y, kind="ETF", limit=130):
        lat, lon, _ = self.frame.enu_to_geodetic(x, y, 0.)
        return ["123", kind, limit, lat, lon]

    @staticmethod
    def road(fid, xy, cls="motorway", width=12., dir_=1, limit=130, link=False):
        return fid, signroads.Spec(width, cls, bool(dir_), dir_, link, limit), np.asarray(xy, float), width

    def place(self, box, sites, lines):
        return radars.place(self.frame, box, sites, lines, lambda la, lo: 100.)

    def test_motorway_sign_has_its_own_tile_and_uses_the_right_approach(self):
        site = self.site(0., -8.)
        lines = [self.road("west", [(-800, 0), (-160, 0)]),
                 self.road("east", [(-160, 0), (400, 0)]),
                 self.road("opposite", [(-800, -24), (400, -24)], dir_=-1),
                 self.road("ramp", [(-520, -17), (-160, -1)], width=4.2, link=True)]
        camera, gates = self.place((-128, -128, 128, 128), [site], lines)
        signs, other = self.place((-600, -128, -400, 128), [site], lines)
        self.assertEqual([p[0] for p in camera], ["fr_speed_camera_box"])
        self.assertEqual([p[0] for p in signs], ["fr_speed_warning_sr3a"])
        self.assertAlmostEqual(signs[0][1], -500., places=3)
        self.assertLess(signs[0][2], -6.)
        self.assertEqual(other, [])
        self.assertEqual(len(gates), 1)
        self.assertEqual(gates[0][0], "123")
        self.assertAlmostEqual(gates[0][2], 0.)
        self.assertEqual(gates[0][4:7], [0., 5.88, 130])
        self.assertAlmostEqual(gates[0][9] - camera[0][3], 1.22)
        with tempfile.TemporaryDirectory() as folder:
            path, _ = roads.write_tile(folder, "/world", 1, 0, 0, [], props=camera, speed_traps=gates)
            self.assertEqual(json.loads((Path(folder) / Path(path).name).read_text())["speedTraps"], gates)

    def test_two_way_gate_stays_in_the_surveyed_shoulder_lane(self):
        line = self.road("street", [(-300, 0), (300, 0)], cls="primary", width=8, dir_=0, limit=50)
        props, gates = self.place((-150, -25, 150, 25), [self.site(0, 6, "ETT", 50)], [line])
        self.assertEqual([p[0] for p in props], ["fr_speed_camera_tower", "fr_speed_warning_sr3b"])
        self.assertAlmostEqual(props[1][1], 100., places=3)
        self.assertEqual(gates[0][2], 2.)
        self.assertEqual(gates[0][4:7], [180., 1.88, 50])

    def test_survey_is_required_and_unsuitable_road_is_skipped(self):
        line = self.road("street", [(-300, 0), (300, 0)], cls="residential")
        box = (-200, -200, 200, 200)
        self.assertEqual(self.place(box, [], [line]), ([], []))
        self.assertEqual(self.place(box, [self.site(0., 5.)], [line]), ([], []))
        grid, signature = radars._catalog()
        by_id = {row[0]: row for group in grid.values() for row in group}
        self.assertGreater(len(by_id), 2500)
        self.assertEqual(by_id["51108"], ["51108", "ETT", 80, 48.863158, 7.700858])
        self.assertNotIn("20043", by_id)   # average-speed endpoint, not an instantaneous camera
        self.assertEqual(len(signature), 64)

    def test_hedge_rotation_is_stable_after_clearance(self):
        shrubs = [(fences.G_HEDGE, float(x), -6., 0., 1.8, 0.) for x in range(12)]
        cleared = fences._clear_hedges(shrubs, [(np.array([[-30., 0.], [30., 0.]]), 5.)])
        rows = fences._forest_rows(cleared, np.zeros(len(cleared)))
        self.assertEqual(rows, fences._forest_rows(cleared, np.zeros(len(cleared))))
        self.assertGreater(len({r[4] for r in rows}), 8)
        self.assertTrue(all(-r[2] >= 5. + r[6] * fences.HEDGE_CROWN_PER_M + fences.HEDGE_ROAD_MARGIN_M
                            for r in rows))


if __name__ == "__main__":
    unittest.main()
