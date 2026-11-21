"""A track over a road on the ground is drawn as a span past its carriageway and the bed's feet, wherever the map's
bridge ends (railways._spans_over_roads), its beds as made. Vienna's primary road under the five tracks by the A23:
the mapped bridges end 2.8 m from its centre line on a 4.3 m half width, the ground tracks' beds stood on the
carriageway and their walls rose 7 m out of it."""
import os
import sys
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import railways  # noqa: E402

REPLAY = os.path.join(HERE, "fixtures", "drive_vienna_a23_1083.npz")
ROAD = "07fd728e"               # (the primary road under the tracks)
AT = (48.229468, 16.453193)     # (where they cross it)


def _seg_dist(q, line):
    a, b = line[:-1], line[1:]
    ab = b - a
    t = np.clip(((q[:, None, :] - a) * ab).sum(2) / np.maximum((ab * ab).sum(1), 1e-12), 0.0, 1.0)
    p = a + t[..., None] * ab
    return np.sqrt(((p - q[:, None, :]) ** 2).sum(2)).min(1)


@unittest.skipUnless(os.path.exists(REPLAY), "the Vienna A23 replay")
class TrackOverRoadSpansTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import interchanges
        rep = interchanges.load("vienna_a23_1083", prefix="drive")
        cls.frame, cls.cells = rep.frame, list(rep.cells.values())
        e, n, _ = cls.frame.geodetic_to_enu(np.array([AT[0]]), np.array([AT[1]]), 0.0)
        cls.at = np.array([float(e[0]), float(n[0])])
        cls.road = None
        for f in cls.cells:
            ids = [str(i) for i in f.ids]
            hit = [k for k, i in enumerate(ids) if i.startswith(ROAD)]
            if hit:
                cls.road = np.asarray(f.parts(hit[0], f.xy_in(cls.frame))[0][0], np.float64)
                break
        box = (cls.at[0] - 150.0, cls.at[1] - 150.0, cls.at[0] + 150.0, cls.at[1] + 150.0)
        rails = railways.parts(cls.frame, cls.cells, box)
        cls.mapped = rails
        prof = [(np.full(len(xy), 1.0 + n), np.zeros(len(xy))) for n, (*_r, xy) in enumerate(rails)]
        ways = railways._roads_under_bridges(cls.frame, cls.cells, box, railways.REACH_M, rails)
        cls.parts, cls.prof = railways._spans_over_roads(rails, prof, ways)

    def test_no_ground_track_on_the_road_or_its_feet(self):
        self.assertIsNotNone(self.road)
        reach = 4.3 + railways.FOOT_MAX_M - 0.5     # (the road's half width, the bed's feet; a sample's slack)
        near = 0
        for fid, _cls, kind, xy in self.parts:
            xy = np.asarray(xy, np.float64)[:, :2]
            if float(np.sqrt(((xy - self.at) ** 2).sum(1)).min()) > 40.0:
                continue
            d = _seg_dist(xy, self.road)
            if kind == "ground":
                self.assertGreater(float(d.min()), reach, "%s: a ground track %.1f m from the road's centre line"
                                   % (str(fid)[:8], float(d.min())))
            elif kind == "bridge" and float(d.min()) < 1.0:
                near += 1
        self.assertGreaterEqual(near, 4, "the tracks' spans over the road")

    def test_the_beds_are_the_parts_own(self):
        # (drawn only: each piece keeps its part's beds, point for point; the ground runs off the road stay ground)
        self.assertEqual(len(self.parts), len(self.prof))
        for (_f, _c, _k, xy), (zb, zg) in zip(self.parts, self.prof):
            self.assertEqual(len(xy), len(zb))
            self.assertEqual(len(xy), len(zg))
        self.assertEqual(sum(len(xy) for *_r, xy in self.parts) - sum(len(xy) for *_r, xy in self.mapped),
                         sum(1 for (_f, _c, k, _x) in self.parts if k == "bridge")
                         - sum(1 for (_f, _c, k, _x) in self.mapped if k == "bridge"),
                         "each new span shares its end point with the ground run it was cut from")
        self.assertTrue(any(k == "ground" for (_f, _c, k, _x) in self.parts))


if __name__ == "__main__":
    unittest.main()
