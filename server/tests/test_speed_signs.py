"""Speed-limit signs (speedsigns.py, earthfast speed_sign_place / speed_sign_mesh): where they stand and how they look."""
import json
import os
import sys
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import fast  # noqa: E402
import signroads  # noqa: E402
import speedsigns  # noqa: E402

M_LAT = 111320.0
LAT0 = 39.3
M_LON = M_LAT * np.cos(np.radians(LAT0))


def row(xy, value, rank=3, hw=4.0, way=0, urban=False, link=False, ends=None, lims=None, skips=()):
    """A speedsigns road row: xy (k, 2) metres; ends: its junctions' ids at the start and end."""
    xy = np.asarray(xy, np.float64)
    ll = np.column_stack([xy[:, 0] / M_LON - 76.5, xy[:, 1] / M_LAT + LAT0])
    a, b = ends or (f"{xy[0][0]:.1f},{xy[0][1]:.1f}", f"{xy[-1][0]:.1f},{xy[-1][1]:.1f}")
    return (ll, xy, (hw, way, rank, 1.0 if urban else 0.0, 1.0 if link else 0.0), [(0.0, a), (1.0, b)],
            lims or [(0.0, float(value))], list(skips))


def place(rows, box=(-5000, -5000, 5000, 5000), polys=(), left_hand=False, face=0):
    arrays = speedsigns._arrays(rows, box)
    polys = [np.asarray(p, np.float64) for p in polys]
    pts = np.concatenate(polys) if polys else np.zeros((0, 2))
    off = np.r_[0, np.cumsum([len(p) for p in polys])].astype(np.int64)
    return fast._ef.speed_sign_place(*arrays, np.ascontiguousarray(pts), off, [float(v) for v in box], face,
                                     left_hand, [float(v) for v in speedsigns.REPEAT_M])


def near(signs, x, y, r):
    return [s for s in signs if np.hypot(s[0] - x, s[1] - y) < r]


@unittest.skipUnless(getattr(fast._ef, "VERSION", 0) >= speedsigns.KERNEL, "earthfast with the speed-sign kernels")
class PlaceTest(unittest.TestCase):
    def test_a_sign_where_the_limit_changes_each_way_on_the_driving_side(self):
        rows = [row([(-600, 0), (0, 0)], 35), row([(0, 0), (600, 0)], 55)]
        signs = place(rows)
        east = [s for s in signs if s[2] > 0.9]
        west = [s for s in signs if s[2] < -0.9]
        self.assertTrue(any(s[4] == 55 and 0 < s[0] < 30 for s in east), "55 just past the change, eastbound")
        self.assertTrue(any(s[4] == 35 and -30 < s[0] < 0 for s in west), "35 just past it, westbound")
        for s in east:
            self.assertLess(s[1], -4.0, "eastbound: right of the road (south), off its 8 m pavement")
        for s in west:
            self.assertGreater(s[1], 4.0)
        lh = place(rows, left_hand=True)
        self.assertTrue(all(s[1] > 4.0 for s in lh if s[2] > 0.9), "driving on the left: the left side")

    def test_sizes_by_road(self):
        town = [row([(-600, 0), (0, 0)], 35, rank=3, hw=6.0, urban=True),
                row([(0, 0), (600, 0)], 25, rank=1, hw=6.0, urban=True)]
        self.assertEqual({s[5] for s in place(town)}, {0}, "a 12 m town street (parking lanes): the 24x30 in panel")
        arterial = [row([(-600, 0), (0, 0)], 35, rank=5, hw=7.0, urban=True),
                    row([(0, 0), (600, 0)], 45, rank=5, hw=7.0, urban=True)]
        self.assertEqual({s[5] for s in place(arterial)}, {1}, "an arterial: the multi-lane 30x36 in")

    def test_no_sign_where_the_limit_goes_on(self):
        rows = [row([(-600, 0), (0, 0)], 35), row([(0, 0), (600, 0)], 35)]
        self.assertEqual(len(near(place(rows), 0, 0, 100)), 0)

    def test_past_a_major_junction_not_a_minor_one(self):
        cross = [row([(-500, 0), (0, 0)], 40, rank=5), row([(0, 0), (500, 0)], 40, rank=5),
                 row([(0, -500), (0, 0)], 40, rank=5), row([(0, 0), (0, 500)], 40, rank=5)]
        signs = near(place(cross), 0, 0, 120)
        self.assertEqual(len(signs), 4, "two primaries: a sign past the crossing in each direction")
        minor = cross[:2] + [row([(0, -500), (0, 0)], 25, rank=1), row([(0, 0), (0, 500)], 25, rank=1)]
        self.assertEqual(len(near(place(minor), 0, 0, 120)), 0, "a residential side street: none on either road")

    def test_past_a_motorway_entrance(self):
        main = [row([(0, 0), (0, 2000)], 65, rank=7, hw=7.0, way=1), row([(0, 2000), (0, 5000)], 65, rank=7, hw=7.0, way=1)]
        ramp = row([(-150, 1700), (0, 2000)], 65, rank=7, hw=3.0, way=1, link=True)
        signs = place(main + [ramp])
        merge = [s for s in signs if 2200 < s[1] < 2400]
        self.assertEqual(len(merge), 1, "MERGE_M past the join")
        self.assertEqual(merge[0][5], 3, "the freeway size")
        self.assertTrue(merge[0][0] > 7.0, "beside the carriageway")

    def test_reminders_on_a_long_road(self):
        signs = place([row([(0, 0), (20000, 0)], 55)], box=(-100, -100, 20100, 100))
        east = sorted(s[0] for s in signs if s[2] > 0)
        self.assertGreaterEqual(len(east), 3)
        self.assertLessEqual(len(east), 9)
        self.assertGreater(min(np.diff(east)), speedsigns.REPEAT_M[1] * 0.55, "no two close together")

    def test_never_on_a_pavement(self):
        rows = [row([(-600, 0), (0, 0)], 35), row([(0, 0), (600, 0)], 55)]
        default = [s for s in place(rows) if s[2] > 0.9][0]
        block = [(default[0] - 3, default[1] - 3), (default[0] + 3, default[1] - 3), (default[0] + 3, default[1] + 3),
                 (default[0] - 3, default[1] + 3), (default[0] - 3, default[1] - 3)]
        moved = [s for s in place(rows, polys=[block]) if s[2] > 0.9][0]
        self.assertFalse(abs(moved[0] - default[0]) < 3.5 and abs(moved[1] - default[1]) < 3.5)

    def test_not_on_a_bridge(self):
        rows = [row([(-600, 0), (0, 0)], 35), row([(0, 0), (600, 0)], 55, skips=[(0.0, 0.05)])]
        east = [s for s in place(rows) if s[2] > 0.9 and s[4] == 55]
        self.assertTrue(east and east[0][0] > 30.0, "past the bridge's 30 m")

    def test_a_change_inside_a_road(self):
        signs = place([row([(0, 0), (1000, 0)], 0, lims=[(0.0, 30.0), (0.5, 50.0)])])
        self.assertTrue(near([s for s in signs if s[2] > 0.9 and s[4] == 50], 505, -6, 10))
        self.assertTrue(near([s for s in signs if s[2] < -0.9 and s[4] == 30], 495, 6, 10))

    def test_one_panel_a_spot(self):
        """Two map lines over one road (different limits) give one panel, not two in each other."""
        rows = [row([(-600, 0), (0, 0)], 35, rank=5), row([(0, 0), (600, 0)], 50, rank=5),
                row([(0, 0), (600, 0)], 70, rank=5, ends=("0.0,0.0", "dup"))]
        east = [s for s in place(rows) if s[2] > 0.9 and s[0] < 100]
        self.assertEqual(len(east), 1)

    def test_only_in_the_box(self):
        rows = [row([(-600, 0), (0, 0)], 35), row([(0, 0), (600, 0)], 55)]
        self.assertEqual(len(place(rows, box=(-1000, -1000, -500, -500))), 0)


@unittest.skipUnless(getattr(fast._ef, "VERSION", 0) >= speedsigns.KERNEL, "earthfast with the speed-sign kernels")
class MeshTest(unittest.TestCase):
    def mesh(self, face, value, size=0, urban=1):
        cp, glyph, cap = speedsigns._glyphs()
        table = np.array([[10.0, 20.0, 5.0, 5.0, 0.0, 1.0, value, size, urban]])
        return fast._ef.speed_sign_mesh(table, face, cp, glyph, cap, [0.0, 0.0, 0.0])

    def test_faces_turn_to_the_driver(self):
        for face in (speedsigns.STYLE_MUTCD, speedsigns.STYLE_VIENNA, speedsigns.STYLE_CANADA):
            p, t, uv, mi = self.mesh(face, 50)
            n = np.cross(p[t[:, 1]] - p[t[:, 0]], p[t[:, 2]] - p[t[:, 0]])
            sign_face = np.isin(mi, [0, 1, 3, 4])
            self.assertTrue((n[sign_face, 1] < 0).all(), "travel +y: the face looks to -y")
            self.assertLess(mi.max(), len(speedsigns.MATERIALS))

    def test_lettering(self):
        p, t, uv, mi = self.mesh(speedsigns.STYLE_MUTCD, 55)
        self.assertEqual(np.count_nonzero(mi == 3), 2 * len("SPEEDLIMIT55"))
        p, t, uv, mi = self.mesh(speedsigns.STYLE_VIENNA, 120)
        self.assertEqual(np.count_nonzero(mi == 3), 2 * 3, "the number only")
        self.assertTrue(np.count_nonzero(mi == 1) > 0, "the red ring")

    def test_mounting_height(self):
        p, _, _, _ = self.mesh(speedsigns.STYLE_MUTCD, 25, urban=1)
        self.assertAlmostEqual(p[:, 2].max() - 5.0, 2.13 + 0.76, places=2, msg="7 ft to the bottom in town")
        p, _, _, _ = self.mesh(speedsigns.STYLE_VIENNA, 100, size=1, urban=0)
        self.assertAlmostEqual(p[:, 2].max() - 5.0, 1.52 + 0.75, places=2)


class GlueTest(unittest.TestCase):
    def test_posted_values(self):
        self.assertEqual(speedsigns.posted(34.2), 35.0)
        self.assertEqual(speedsigns.posted(50 / 1.609344, mph=False), 50.0)
        self.assertEqual(speedsigns.posted(120 / 1.609344, mph=False), 120.0)
        self.assertEqual(speedsigns.posted(None), 0.0)
        self.assertEqual(speedsigns.posted(float("inf")), 0.0)

    def test_styles(self):
        self.assertEqual(speedsigns.style("US", True), speedsigns.STYLE_MUTCD)
        self.assertEqual(speedsigns.style("CA", False), speedsigns.STYLE_CANADA)
        self.assertEqual(speedsigns.style("GB", False), speedsigns.STYLE_VIENNA)
        self.assertEqual(speedsigns.style(None, False), speedsigns.STYLE_VIENNA)
        self.assertEqual(speedsigns.style("se", False), speedsigns.STYLE_SWEDEN)

    def test_swedish_values_are_the_legal_steps(self):
        posted = lambda kmh: speedsigns.posted(kmh / 1.609344, False, speedsigns.STYLE_SWEDEN)  # noqa: E731
        self.assertEqual([posted(v) for v in (30, 35, 44, 50, 65, 74, 80, 95, 110, 120)],
                         [30, 40, 40, 50, 70, 70, 80, 100, 110, 120])
        self.assertEqual(posted(130), 120)
        self.assertEqual(posted(20), 0, "slower than any step: no sign rather than a number above the limit")

    def test_c31_materials_are_in_the_mod(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "mod", "beamng_earth", "levels",
                            "beamng_earth", "art", "shapes", "beamngEarth", "roadside", "exit.materials.json")
        with open(path, encoding="utf-8") as f:
            self.assertLessEqual(set(speedsigns.MATERIALS_C31), set(json.load(f)))

    @unittest.skipUnless(getattr(fast._ef, "VERSION", 0) >= speedsigns.KERNEL, "earthfast with the speed-sign kernels")
    def test_c31_mesh_has_swedish_sizes_and_colours(self):
        class Frame:
            enu_to_geodetic = staticmethod(lambda x, y, z: (y, x, x * 0))
            geodetic_to_enu = staticmethod(lambda la, lo, h: (lo, la, h))
        widths = {}
        for size, want in ((1, 0.60), (2, 0.90), (3, 0.90)):
            sign = np.array([[0.0, 0.0, 0.0, 1.0, 70.0, size, 5.0, 0.0, 0.0]])
            with mock.patch.object(speedsigns, "place", return_value=sign):
                p, _, _, mats, mi = speedsigns.build_tile(Frame, 0, 0, lambda la, lo: la * 0, None,
                                                          face=speedsigns.STYLE_SWEDEN)
            self.assertEqual(mats, speedsigns.MATERIALS_C31)
            widths[size] = p[:, 0].max() - p[:, 0].min()
            self.assertAlmostEqual(widths[size], want, places=2)
            self.assertIn(1, set(mi.tolist()))

    def test_cell_specs_kept(self):
        class Feats:
            calls = 0

            def __len__(self):
                return 0
        f = Feats()
        import roads
        real = roads.road_specs
        try:
            roads.road_specs = lambda *a, **k: (setattr(Feats, "calls", Feats.calls + 1), [])[1]
            signroads.cell_specs(f)
            signroads.cell_specs(f)
        finally:
            roads.road_specs = real
        self.assertEqual(Feats.calls, 1)


if __name__ == "__main__":
    unittest.main()
