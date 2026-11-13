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

    def test_c31_face_is_the_artwork_the_right_way_round(self):
        table = np.array([[0.0, 0.0, 10.0, 10.5, 0.0, 1.0, 70.0, 1.0, 0.0]])       # driving north
        p, t, uv, mats, mi = speedsigns.sweden_mesh(table, (0.0, 0.0, 0.0))
        face = t[mi == 0]
        a, b, c = p[face[:, 0]], p[face[:, 1]], p[face[:, 2]]
        self.assertTrue((np.cross(b - a, c - a)[:, 1] < 0).all(), "the face towards the driver")
        used = np.unique(face)
        for axis, along in ((0, 0), (2, 1)):                                        # east is the driver's right, up is up
            self.assertGreater(np.corrcoef(p[used, axis], uv[used, along])[0, 1], 0.999)
        low = p[used, 2].min()
        self.assertAlmostEqual(low, 10.5 + 1.52, places=2)
        self.assertGreater(p[:, 2].max() - p[:, 2].min(), 2.0, "with a pole to the ground and below it")
        self.assertLess(p[:, 2].min(), 10.0)

    def test_c31_cells_hold_their_signs(self):
        from PIL import Image
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "mod", "beamng_earth", "levels",
                            "beamng_earth", "art", "shapes", "beamngEarth", "bne_se_signs.dds")
        __import__('sys').path.insert(0, __import__('os').path.join(__import__('os').path.dirname(__import__('os').path.abspath(__file__)), '..', '..', 'tools')); import mod_textures_dds as _mt
        sheet = np.asarray(_mt.open_texture(path).convert("RGB")).astype(int)
        self.assertEqual(sheet.shape[:2], (int(speedsigns.C31_SHEET),) * 2)
        at = lambda value, c, s: sheet[(lambda uv: (int((1 - uv[1]) * 1024), int(uv[0] * 1024)))(   # noqa: E731
            speedsigns.c31_uv(value, c, s))]
        faces = set()
        for value in range(30, 130, 10):
            r, g, b = at(value, 0.92, 0.0)
            self.assertTrue(r > 200 and g < 100 and b < 100, f"{value}: the red ring")
            r, g, b = at(value, 0.0, 0.72)
            self.assertTrue(r > 200 and g > 170 and b < 60, f"{value}: yellow inside it")
            k = value // 10 - 3
            cell = sheet[(k // 4) * 256: (k // 4) * 256 + 256, (k % 4) * 256: (k % 4) * 256 + 256]
            faces.add(cell.tobytes())
            self.assertLess(cell.sum(2).min(), 90, f"{value}: black digits")
        self.assertEqual(len(faces), 10)

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



class ClassTest(unittest.TestCase):
    def test_tracks_are_never_signed(self):
        """Tracks are in the AI's graph (navgraph.DRIVE, GitHub #756) but get no speed signs."""
        from test_roads import features, FRAME
        import navgraph
        self.assertIn("track", navgraph.DRIVE)
        f = features([[(0, 0), (400, 0)], [(0, 50), (400, 50)]], ["primary", "track"])
        got = speedsigns._cell_rows(f, FRAME, None, None, False, True)
        self.assertEqual(sorted(got), [str(f.ids[0])])




class GermanGeneralLimitsTest(unittest.TestCase):
    """speedsigns.general (earthfast speed_sign_general): with the town signs on, Germany's general limits are not
    posted (StVO § 3 Abs. 3, § 39 Abs. 1): no 50 at a town's Ortstafel or in it, no 100 out of town but the end of a
    lower limit; outside Germany and on motorways every sign stays."""

    def rows(self, *signs):
        # [x, y, travel x, travel y, value, size, road x, road y, urban, kind, before, rank]
        return np.array([[x, 0.0, 1.0, 0.0, v, 0, x, 0.0, 0, k, b, r] for x, v, k, b, r in signs], np.float64)

    def towns(self, xy):
        # a German town east of x = 1000 m, Germany west of x = 5000 m
        xy = np.asarray(xy, np.float64)
        return np.c_[(xy[:, 0] >= 1000.0) & (xy[:, 0] < 5000.0), xy[:, 0] < 5000.0]

    def test_general_limits(self):
        signs = self.rows((850.0, 50, 0, 100, 4),        # 150 m before the town: its Ortstafel's
                          (2000.0, 50, 3, 0, 4),         # a reminder in town
                          (2000.0, 50, 0, 30, 4),        # the end of a 30
                          (500.0, 100, 0, 50, 4),        # a town's end
                          (300.0, 100, 2, 0, 4),         # out of town past a junction
                          (300.0, 100, 0, 70, 4),        # the end of a 70
                          (300.0, 100, 2, 0, 7),         # a motorway's
                          (300.0, 50, 2, 0, 4),          # a 50 of its own out of town
                          (6000.0, 100, 2, 0, 4))        # outside Germany
        self.assertEqual(speedsigns.general(signs, self.towns).tolist(),
                         [False, False, True, False, False, True, True, True, True])

if __name__ == "__main__":
    unittest.main()
