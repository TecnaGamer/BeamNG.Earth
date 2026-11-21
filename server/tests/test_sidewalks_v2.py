"""Sidewalks v2 (sidewalks.V2, BNE_SIDEWALK_V2; the road revamp's P1d): the walk's own profile, rolled kerbs, walks
across driveways, vanilla's curb return radii, corner slabs meeting the runs. The switch is read at import, so these
tests set sidewalks.V2 for their own run; off, every builder is v1's (test_sidewalk_collision and the tile tests)."""
import os
import sys
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fast  # noqa: E402
import geodesy  # noqa: E402
import junctions  # noqa: E402
import roads  # noqa: E402
import sidewalks  # noqa: E402


def spec(cls="residential", **kw):
    return roads.Spec(9.5, "paved", True, cls, **kw)


def straight(n=41, length=40.0, z=lambda s: 0.0 * s):
    s = np.linspace(0.0, length, n)
    return np.column_stack([s, np.full(n, 6.0), z(s)])


class KerbRuleTest(unittest.TestCase):
    def test_suburban_residential_rolls(self):
        self.assertEqual(roads.kerb_roll(spec(), "US"), 1.0)
        self.assertEqual(roads.kerb_roll(spec(), "ca"), 1.0)

    def test_vertical_elsewhere(self):
        self.assertEqual(roads.kerb_roll(spec(), "DE"), 0.0, "North America only")
        self.assertEqual(roads.kerb_roll(spec("tertiary"), "US"), 0.0, "collectors: vertical")
        self.assertEqual(roads.kerb_roll(spec(oneway=True), "US"), 0.0)
        self.assertEqual(roads.kerb_roll(spec(kmh=72.0), "US"), 0.0)
        self.assertEqual(roads.kerb_roll(spec(surf="sett"), "US"), 0.0, "a cobbled street: granite kerbs")
        self.assertEqual(roads.kerb_roll(spec(), "US", walled_row=18.0), 0.0, "walled downtown")
        self.assertEqual(roads.kerb_roll(spec(), "US", town_front=0.8), 0.0, "rowhouses at the walk")
        self.assertEqual(roads.kerb_roll(spec(), "US", walled_row=30.0, town_front=0.1), 1.0, "front yards")


@unittest.skipUnless(getattr(fast._ef, "VERSION", 0) >= sidewalks.V2_NATIVE, f"earthfast {sidewalks.V2_NATIVE}")
class MeshTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(sidewalks, "V2", True)
        p.start()
        self.addCleanup(p.stop)

    def build(self, walk):
        b = sidewalks.Builder((0.0, 0.0, 0.0), collision=True)
        b.add_walk(walk, 2.5)
        return b.arrays(), b.col.arrays()

    def walk(self, **kw):
        pts = straight()
        w = {"pts": pts, "side": 1, "caps": (True, True), "ramps": [], "w": 2.5, "roll": 0.0}
        w.update(kw)
        return w

    def curb_tops(self, P, T, M, x):
        """z of the drawn mesh's highest point within 0.12 m of the curb line (y = 6 - 1.25) at x."""
        near = (np.abs(P[:, 0] - x) < 0.3) & (P[:, 1] > 4.75 - 0.01) & (P[:, 1] < 4.75 + 0.35)
        return P[near, 2].max()

    def test_explicit_walk_surface_wins_over_inferred_brick(self):
        (_, _, _, inferred), _ = self.build(self.walk(brick=True))
        (_, _, _, tagged), _ = self.build(self.walk(brick=True, town_mat=1))
        self.assertIn(sidewalks.BRICK, inferred)
        self.assertNotIn(sidewalks.BRICK, tagged)

    def test_per_vertex_reveal(self):
        hv = np.linspace(0.08, 0.20, 41)
        (P, T, UV, M), _ = self.build(self.walk(hv=hv, drop=np.full(41, 0.2)))
        self.assertAlmostEqual(self.curb_tops(P, T, M, 0.0), 0.08, places=6)
        self.assertAlmostEqual(self.curb_tops(P, T, M, 40.0), 0.20, places=6)

    def test_rolled_kerb_shape(self):
        (P, T, UV, M), _ = self.build(self.walk(roll=1.0, hv=np.full(41, 0.09)))
        # the curb top band ends 0.28 m from the curb line (vertical: 0.15)
        tops = P[(np.abs(P[:, 0] - 20.0) < 1e-6) & (np.abs(P[:, 2] - 0.09) < 1e-9)]
        self.assertAlmostEqual(float(tops[:, 1].max() - 4.75), 0.28, places=6)

    def test_driveway_lowers_the_kerb_without_tactile(self):
        (P, T, UV, M), _ = self.build(self.walk(drives=[(18.0, 22.0)]))
        self.assertLess(self.curb_tops(P, T, M, 20.0), 0.031)
        self.assertGreater(self.curb_tops(P, T, M, 10.0), 0.14)
        self.assertNotIn(2, set(M.tolist()), "no tactile pads at a driveway")

    def test_rolled_kerb_keeps_its_height_at_a_driveway(self):
        (P, T, UV, M), _ = self.build(self.walk(roll=1.0, hv=np.full(41, 0.09), drives=[(18.0, 22.0)]))
        self.assertGreater(self.curb_tops(P, T, M, 20.0), 0.085, "mountable: crossed as it is")

    def test_back_ends_on_the_ground(self):
        (P, T, UV, M), _ = self.build(self.walk(hv=np.full(41, 0.15), drop=np.full(41, 0.5)))
        top = 0.15 + 2.5 * sidewalks.CROSS_SLOPE
        self.assertAlmostEqual(float(P[:, 2].min()), min(top - 0.5, -sidewalks.SINK), places=6)


@unittest.skipUnless(getattr(fast._ef, "VERSION", 0) >= sidewalks.V2_NATIVE, f"earthfast {sidewalks.V2_NATIVE}")
class ProfileTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(sidewalks, "V2", True)
        p.start()
        self.addCleanup(p.stop)

    def drape(self, ground, walks):
        roads.drape_walks(geodesy.EnuFrame(40.0, -100.0, 0.0), None, walks, ground)
        return walks

    def test_reveal_in_range_without_steps(self):
        # a bumpy road edge (+-6 cm every few metres) on a slope: the walk smooth, its reveal kept to 0.10-0.16
        def ground(x, y):
            return 0.02 * x + 0.06 * np.sin(x * 1.7) + 0.03 * (y - 4.75)
        w = {"pts": straight(201, 100.0), "side": 1, "caps": (True, True), "ramps": [], "w": 2.5, "roll": 0.0}
        self.drape(ground, [w])
        hv = w["hv"]
        self.assertEqual(len(hv), len(w["pts"]))
        self.assertTrue((hv >= sidewalks.REVEAL_MIN - 1e-12).all() and (hv <= sidewalks.REVEAL_MAX + 1e-12).all())
        self.assertGreater(float(np.ptp(hv)), (sidewalks.REVEAL_MAX - sidewalks.REVEAL_MIN) / 2,
                           "the reveal uses at least half the available range on this bumpy street")
        s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(w["pts"][:, :2], axis=0).T))]
        top = w["pts"][:, 2] + hv
        self.assertLess(float(np.max(np.abs(np.diff(top)) / np.maximum(np.diff(s), 1e-3))), 0.1, "no steps")

    def test_the_same_place_the_same_reveal(self):
        # two tiles' pieces of one run: their shared end gets one reveal (a function of the place)
        def ground(x, y):
            return 0.05 * np.sin(x / 3.0) + 0.01 * x
        whole = straight(161, 80.0)
        a = {"pts": whole[:81].copy(), "side": 1, "caps": (True, False), "ramps": [], "w": 2.5, "roll": 0.0}
        b = {"pts": whole[80:].copy(), "side": 1, "caps": (False, True), "ramps": [], "w": 2.5, "roll": 0.0}
        self.drape(ground, [a])
        self.drape(ground, [b])
        self.assertAlmostEqual(float(a["hv"][-1]), float(b["hv"][0]), places=12)
        self.assertAlmostEqual(float(a["pts"][-1, 2]), float(b["pts"][0, 2]), places=12)


class BentSeamTest(unittest.TestCase):
    @unittest.skipUnless(getattr(fast._ef, "VERSION", 0) >= sidewalks.V2_NATIVE, f"earthfast {sidewalks.V2_NATIVE}")
    def test_pieces_meeting_on_a_bend_share_their_curb_point_and_reveal(self):
        # a run bending 30 degrees at a tile border: each tile's piece ends on the bend; with the whole run's mitre
        # there (build_tile's end_n) both put the curb, and so the reveal's probes, at one point
        a = np.column_stack([np.linspace(0, 20, 11), np.zeros(11), np.zeros(11)])
        d = np.array([np.cos(np.radians(30)), np.sin(np.radians(30))])
        b = np.column_stack([20 + d[0] * np.linspace(0, 20, 11), d[1] * np.linspace(0, 20, 11), np.zeros(11)])
        whole = np.vstack([a, b[1:]])
        n = sidewalks.normals_2d(whole[:, :2])[10]
        ca, _ = sidewalks.run_geometry(a, 1, 2.5, (None, (a[-1, :2], n)))
        cb, _ = sidewalks.run_geometry(b, 1, 2.5, ((b[0, :2], n), None))
        self.assertLess(float(np.hypot(*(ca[-1, :2] - cb[0, :2]))), 1e-12)
        plain_a, _ = sidewalks.run_geometry(a, 1, 2.5)
        plain_b, _ = sidewalks.run_geometry(b, 1, 2.5)
        self.assertGreater(float(np.hypot(*(plain_a[-1, :2] - plain_b[0, :2]))), 0.3, "without: apart")
        with mock.patch.object(sidewalks, "V2", True):
            wa = {"pts": a.copy(), "side": 1, "caps": (True, False), "ramps": [], "w": 2.5, "roll": 0.0,
                  "end_n": (None, (a[-1, :2].copy(), n))}
            wb = {"pts": b.copy(), "side": 1, "caps": (False, True), "ramps": [], "w": 2.5, "roll": 0.0,
                  "end_n": ((b[0, :2].copy(), n), None)}

            def ground(x, y):
                return 0.05 * np.sin(x / 2.0) + 0.04 * np.cos(y / 1.5)
            for w in (wa, wb):
                roads.drape_walks(geodesy.EnuFrame(40.0, -100.0, 0.0), None, [w], ground)
        self.assertAlmostEqual(float(wa["hv"][-1]), float(wb["hv"][0]), places=12)


class DrivewayTest(unittest.TestCase):
    @unittest.skipUnless(getattr(fast._ef, "VERSION", 0) >= sidewalks.V2_NATIVE, f"earthfast {sidewalks.V2_NATIVE}")
    def test_crossings_along_a_run(self):
        run = straight()
        lines = [(np.array([[10.0, 0.0], [10.0, 20.0]]), 1.6), (np.array([[30.0, 0.0], [31.0, 3.0]]), 1.5)]
        got = roads.walk_drives(run, lines)
        self.assertEqual(len(got), 1, "the second ends before the walk")
        self.assertAlmostEqual(got[0][0], 8.4, places=9)
        self.assertAlmostEqual(got[0][1], 11.6, places=9)
        # a piece of the run (another tile's) keeps them where they are
        self.assertEqual(roads.drives_on(run[20:], run, got), [], "behind this piece, past its flare")
        self.assertEqual([(round(a, 9), round(b, 9)) for a, b in roads.drives_on(run[5:], run, got)], [(3.4, 6.6)])


@unittest.skipUnless(getattr(fast._ef, "VERSION", 0) >= sidewalks.V2_NATIVE, f"earthfast {sidewalks.V2_NATIVE}")
class CornerTest(unittest.TestCase):
    def corners(self, v2):
        a = np.column_stack([np.linspace(-60, 0, 31), np.zeros(31), np.zeros(31)])
        b = np.column_stack([np.zeros(31), np.linspace(0, 60, 31), np.zeros(31)])
        rows = [("a", spec(), a, -100.0), ("b", spec(), b, -100.0)]
        with mock.patch.object(sidewalks, "V2", v2):
            return junctions.find(rows, roads.has_sidewalks, None)[0]

    def test_crosswalks_on_a_short_block(self):
        def crossings(span):
            lines = [("west", [(-110, 0), (0, 0)]), ("block", [(0, 0), (span, 0)]),
                     ("east", [(span, 0), (110, 0)])]
            for i, x in enumerate((0, span)):
                for y in (-100, 100):
                    lines.append((f"side{i}_{y}", [(x, 0), (x, y)]))
            rows = [(fid, spec(), np.column_stack((xy, np.zeros(len(xy)))), -100.) for fid, xy in lines]
            with mock.patch.object(sidewalks, "V2", True):
                marks = junctions.find(rows, roads.has_sidewalks, None)[2]
            return [cw for cw in marks if not cw.get("stop") and abs(cw["a"][0] - cw["b"][0]) < .1
                    and 0 < cw["a"][0] < span]

        self.assertEqual(len(crossings(28)), 1)  # 3.5 m centre to centre, only 0.5 m between painted bands
        self.assertEqual(len(crossings(32)), 2)

    def test_vanilla_radius_and_steps(self):
        old, new = self.corners(False), self.corners(True)
        self.assertTrue(old and new)

        def radius(c):
            arc = np.asarray(c["arc"])[:, :2]
            seg = np.hypot(*np.diff(arc, axis=0).T)
            return seg.sum() / (np.pi / 2), seg.max()
        r_old, _ = radius(old[0])
        r_new, step = radius(new[0])
        self.assertAlmostEqual(r_old, junctions.RADII["residential"], delta=0.1)
        self.assertAlmostEqual(r_new, junctions.RADII_V2["residential"], delta=0.1)
        self.assertLess(step, 1.15 * sidewalks.ARC_STEP_M, "about ARC_STEP_M a segment (whole segments a turn)")


@unittest.skipUnless(getattr(fast._ef, "VERSION", 0) >= 561, "earthfast 561")
class DistrictTest(unittest.TestCase):
    def test_frontage_follows_wall_but_stops_at_drive(self):
        wall = np.array([[1., 4.], [39., 5.9], [39., 12.], [1., 12.]])
        curb = np.array([[0., 0., 0.], [40., 0., 0.]])
        outward = np.array([[0., 1.], [0., 1.]])
        profile = fast._ef.TownFrontage([(wall, 1, True)]).profile(
            curb, outward, 2.5, [(17., 20.)], (False, False))
        s, widths = np.asarray(profile).T
        for x in (5., 30.):
            self.assertAlmostEqual(np.interp(x / 40, s, widths), 4 + (x - 1) * .05)
        self.assertEqual(np.interp(18. / 40, s, widths), 2.5)
        with mock.patch.object(sidewalks, "V2", True):
            b = sidewalks.Builder((0., 0., 0.), collision=True)
            b.add_walk({"pts": curb + [0., 1.25, 0.], "w": 2.5, "caps": (True, True), "side": 1,
                        "front_widths": profile}, 2.5)
            for mesh in (b.arrays(), b.col.arrays()):
                self.assertGreater(mesh[0][:, 1].max(), 5.8)
                self.assertLessEqual(mesh[0][:, 1].max(), 5.901)

    def test_mapped_square_hole_and_road_exclusion(self):
        import frontage_geometry
        def box(x0, y0, x1, y1):
            return np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], np.float64)
        coverage = frontage_geometry.Coverage((0., 0., 20., 20.))
        coverage.reserve(box(0, 0, 2, 20))
        xy, t, m, edges = fast._ef.town_paving((0., 0., 20., 20.),
            [(box(0, 0, 20, 20), [box(4, 4, 8, 8)], sidewalks.BRICK)], coverage._c)
        a, b, c = xy[t].transpose(1, 0, 2)
        cross = (b-a)[:, 0] * (c-a)[:, 1] - (b-a)[:, 1] * (c-a)[:, 0]
        self.assertAlmostEqual(cross.sum() / 2, 344.)
        self.assertTrue((cross > 0).all())
        self.assertTrue(edges)
        pts = np.column_stack([xy, np.full(len(xy), 2.)])
        drawn = fast._ef.town_area_mesh(pts, t, m, edges, (0., 0., 0.))
        self.assertAlmostEqual(drawn[0][:, 2].max(), 2.15)
        self.assertAlmostEqual(drawn[0][:, 2].min(), 1.90)
        self.assertEqual(set(drawn[3]), {0, sidewalks.BRICK})

    def test_return_uses_the_branch_cross_section(self):
        for c in CornerTest().corners(True):
            for end, outer in ((0, -1), (-1, 0)):
                delta = np.asarray(c["outer"][outer])[:2] - c["arc"][end, :2]
                np.testing.assert_allclose(c["inward"][end], delta / np.linalg.norm(delta), atol=1e-12)

    def test_return_follows_the_actual_gutter(self):
        import townwalk
        run = {"fid": "r", "pts": np.array([[-12., 1.25, 0.], [0., 1.25, 0.]]),
               "w": 2.5, "side": 1, "j": (False, True)}
        corner = {"arc": np.array([[0., .8, 0.], [3., .5, 0.], [6., -4., 0.]]),
                  "outer": [[8., -4., 0.], [8., 3.3, 0.], [0., 3.3, 0.]],
                  "inward": np.array([[0., 1.], [.5, .85], [1., 0.]]),
                  "road_sides": (("r", 1), ("s", 1))}
        sections = townwalk.junction_sections([corner], [run])
        np.testing.assert_allclose(corner["arc"][0], [0., 0., 0.], atol=1e-12)
        np.testing.assert_allclose(corner["outer"][-1], [0., 2.5, 0.], atol=1e-12)
        np.testing.assert_allclose(sections["r", 1][0][1], [0., 1.], atol=1e-12)

    def test_island_in_a_plaza_hole(self):
        import frontage_geometry
        def box(lo, hi):
            return np.array([[lo, lo], [hi, lo], [hi, hi], [lo, hi]], np.float64)
        bounds = (0., 0., 20., 20.)
        rows = [(box(0, 20), [box(4, 16)], sidewalks.TOWN_PAVERS), (box(8, 12), [], sidewalks.BRICK)]
        xy, tri, m, _ = fast._ef.town_paving(bounds, rows, frontage_geometry.Coverage(bounds)._c)
        a, b, c = xy[tri].transpose(1, 0, 2)
        area = ((b - a)[:, 0] * (c - a)[:, 1] - (b - a)[:, 1] * (c - a)[:, 0]) / 2
        self.assertAlmostEqual(area[np.asarray(m) == sidewalks.BRICK].sum(), 16.)
        self.assertAlmostEqual(area.sum(), 272.)

    def test_wear_continues_in_next_tile(self):
        parent = np.array([[0., 0.], [2000., 0.]])
        full = fast._ef.town_marks(parent, parent, "high-street", 1)
        self.assertTrue(full)
        a, b, k, _, _ = full[0]
        cut = (a + b) * 1000.
        left = fast._ef.town_marks(parent, np.array([[0., 0.], [cut, 0.]]), "high-street", 1)
        right = fast._ef.town_marks(parent, np.array([[cut, 0.], [2000., 0.]]), "high-street", 1)
        before = next(r for r in left if r[2] == k and r[1] == 1.)
        after = next(r for r in right if r[2] == k and r[0] == 0.)
        self.assertAlmostEqual(before[4], after[3], places=10)


if __name__ == "__main__":
    unittest.main()
