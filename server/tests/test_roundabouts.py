"""roundabouts (plan 038): the island's layers, the ring's outside curb between its entries, the entries' give-way lines."""
import math
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import islands  # noqa: E402
import roads  # noqa: E402
import roundabouts  # noqa: E402


def arc(r, a0, a1, n=14, cx=0.0, cy=0.0):
    t = np.linspace(a0, a1, n)
    return np.stack([cx + r * np.cos(t), cy + r * np.sin(t), np.zeros(n)], 1)


def ring_with_entries(r=11.7, ring_w=6.3, town=True, entries=(0.0, math.pi / 2, math.pi)):
    ow = roads.Spec(ring_w, "paved", False, "primary", True, dir=1)
    two = roads.Spec(9.0, "paved", town, "primary", False)
    cuts = sorted(entries) + [sorted(entries)[0] + 2 * math.pi]     # the ring is split at every entry, as the map has it
    rs = [(f"r{k}", ow, arc(r, a, b), 0.0) for k, (a, b) in enumerate(zip(cuts[:-1], cuts[1:]))]
    for k, a in enumerate(entries):
        d = np.array([math.cos(a), math.sin(a)])
        rs.append((f"e{k}", two, np.array([(*(d * r), 0.0), (*(d * (r + 70)), 0.0)]), 0.0))
    return rs


def place_with(slot):
    """A (lat, lon) whose island's planting is slot."""
    for k in range(400):
        at = (50.0 + k * 0.0137, 16.0 + k * 0.0071)
        if roundabouts.planting(*at) == slot:
            return at
    raise AssertionError("no place for the planting")


class IslandLayersTest(unittest.TestCase):
    def found(self, **kw):
        f = islands.find(ring_with_entries(**kw))
        self.assertEqual([x["round"] for x in f], [True])
        return f[0]

    def test_a_town_roundabout_island_has_an_apron_and_a_planter(self):
        f = self.found()
        layers = roundabouts.island_layers(f, (0.6, 1.2), place_with(9))
        self.assertEqual(len(layers), 2)
        (o1, w1, h1, m1), (o2, w2, h2, m2) = layers
        self.assertEqual(m1, 7)
        self.assertEqual(m2, 0, "the planting's curb band: the fan inside it is the planting")
        self.assertLess(h1, h2, "the apron is the low one, the planter's curb the high one")
        self.assertAlmostEqual(o2 - w2 / 2, o1 + w1 / 2, places=6, msg="the curb band starts where the apron ends")
        r = roundabouts.inner_radius(f["ring"])
        ring, apex, mat, lift, mound = roundabouts.planting_fill(f, (0.6, 1.2), place_with(9), roads.offset_polyline)
        self.assertEqual(mat, 9)
        rr = np.hypot(ring[:, 0], ring[:, 1])
        self.assertAlmostEqual(float(rr.mean()), r - w1 - w2, delta=0.25, msg="the fan starts at the curb band's inner edge")
        self.assertLess(float(np.hypot(apex[0], apex[1])), 0.2 * r, "and ends near the island's middle")
        self.assertGreater(lift, roundabouts.PLANTER_H)
        self.assertTrue(0 < mound <= roundabouts.MOUND_MAX_M)
        self.assertTrue(roundabouts.APRON_MIN_M <= w1 <= roundabouts.APRON_MAX_M)

    def test_a_lawn_island_is_a_fan_in_the_lawn_texture(self):
        f = self.found()
        got = roundabouts.planting_fill(f, (0.6, 1.2), place_with(roundabouts.LAWN), roads.offset_polyline)
        self.assertEqual(got[2], roundabouts.LAWN)

    def test_a_tiny_island_has_no_fan(self):
        f = self.found(r=6.0, ring_w=5.0)
        self.assertIsNone(roundabouts.planting_fill(f, (0.6, 1.2), place_with(9), roads.offset_polyline))

    def test_a_tiny_island_keeps_the_single_band(self):
        f = self.found(r=6.0, ring_w=5.0)
        self.assertEqual(roundabouts.island_layers(f, (0.6, 1.2)), [(0.6, 1.2, 0.15, 0)])

    def test_a_big_roundabout_keeps_the_plain_curb(self):
        f = self.found(r=45.0, ring_w=8.0)
        self.assertEqual(roundabouts.island_layers(f, (0.6, 1.2)), [(0.6, 1.2, 0.15, 0)])

    def test_a_turnaround_loop_is_no_roundabout(self):
        ow = roads.Spec(5.8, "paved", True, "residential", True, dir=1)
        two = roads.Spec(10.6, "paved", True, "residential", False)
        f = islands.find([("loop", ow, arc(12, math.pi, 2 * math.pi), 0.0),
                          ("link", two, np.array([(12.0, 0.0, 0.0), (-12.0, 0.0, 0.0)]), 0.0)])
        self.assertEqual(len(f), 1)
        self.assertEqual(roundabouts.island_layers(f[0], (0.6, 1.2)), [(0.6, 1.2, 0.15, 0)])

    def test_switched_off(self):
        f = self.found()
        with mock.patch.dict(os.environ, {"BNE_ROUNDABOUTS": "off"}):
            self.assertEqual(roundabouts.island_layers(f, (0.6, 1.2)), [(0.6, 1.2, 0.15, 0)])
            self.assertEqual(roundabouts.outer_curbs(f, ring_with_entries(), roads.offset_polyline, roads.densify), [])
            self.assertEqual(roundabouts.entry_marks(ring_with_entries(), None, "CZ", None, []), ([], {}))

    def test_apron_grows_with_the_island(self):
        self.assertLessEqual(roundabouts.apron_width(5.0), roundabouts.apron_width(9.0))
        self.assertEqual(roundabouts.apron_width(100.0), roundabouts.APRON_MAX_M)


class OuterCurbTest(unittest.TestCase):
    def runs(self, town=True, entries=(0.0, math.pi / 2, math.pi)):
        rs = ring_with_entries(town=town, entries=entries)
        f = islands.find(rs)[0]
        return rs, f, roundabouts.outer_curbs(f, rs, roads.offset_polyline, roads.densify)

    def test_curb_runs_lie_outside_the_ring_between_the_entries(self):
        rs, f, runs = self.runs()
        self.assertEqual(len(runs), 3, "three entries leave three arcs (the fourth quarter is one arc of the loop)")
        for run in runs:
            r = np.hypot(run[:, 0], run[:, 1])
            self.assertGreater(float(np.mean(np.abs(r - (11.7 + 3.15 + 0.05)) < 0.35)), 0.4, "much of it on the ring's outer edge")
        for _fid, sp, pts, _l in rs[3:]:
            for run in runs:
                d = np.abs(run[:, 1] * pts[1, 0] - run[:, 0] * pts[1, 1]) / np.hypot(pts[1, 0], pts[1, 1])
                ahead = run[:, 0] * pts[1, 0] + run[:, 1] * pts[1, 1] > 0
                self.assertGreater(float(d[ahead].min()) if ahead.any() else 99.0, sp.width / 2 - 0.1,
                                   "no curb across an entry's carriageway (its corner meets the road's edge)")


    def test_a_run_ends_in_a_corner_on_the_entry_roads_edge(self):
        rs, f, runs = self.runs()
        # the entry along +x (half width 4.5): some run has a point on its edge line y = +-4.5 beyond the ring's edge
        edge_x = 11.7 + 3.15 + roundabouts.OUTER_JOIN_M
        on_edge = [r for r in runs if np.any((np.abs(np.abs(r[:, 1]) - 4.5) < 0.2) & (np.abs(r[:, 0] - edge_x) < 0.9))]
        self.assertTrue(on_edge, "a curb run reaches the road's edge where its sidewalks start")
        for r in runs:
            step = np.hypot(*np.diff(r[:, :2], axis=0).T)
            self.assertLess(float(step.max()), 2.6, "no gap or jump inside a run")
            d = np.diff(r[:, :2], axis=0)
            cosang = (d[1:] * d[:-1]).sum(1) / np.maximum(np.hypot(*d[1:].T) * np.hypot(*d[:-1].T), 1e-9)
            self.assertGreater(float(cosang.min()), 0.0, "no sharp turn: a rounded corner, not a stub")

    def test_no_curb_without_a_town(self):
        _rs, _f, runs = self.runs(town=False)
        self.assertEqual(runs, [])

    def test_no_curb_for_a_plain_loop(self):
        ow = roads.Spec(5.8, "paved", True, "residential", True, dir=1)
        two = roads.Spec(10.6, "paved", True, "residential", False)
        rs = [("loop", ow, arc(12, math.pi, 2 * math.pi), 0.0),
              ("link", two, np.array([(12.0, 0.0, 0.0), (-12.0, 0.0, 0.0)]), 0.0)]
        f = islands.find(rs)[0]
        self.assertEqual(roundabouts.outer_curbs(f, rs, roads.offset_polyline, roads.densify), [])


class EntryMarkTest(unittest.TestCase):
    def test_dashes_across_every_entry_in_czechia(self):
        rs = ring_with_entries()
        marks, cuts = roundabouts.entry_marks(rs, None, "CZ", None, [])
        self.assertGreaterEqual(len(marks), 3, "dashes on each of the three entries")
        self.assertEqual(len({round(float(np.asarray(m)[:, :2].mean(0)[0]), 0) for m in marks}
                             | {round(float(np.asarray(m)[:, :2].mean(0)[1]), 0) for m in marks}) >= 3, True)
        self.assertTrue(all(np.asarray(m).shape == (4, 3) for m in marks))
        self.assertTrue(set(cuts) <= {"e0", "e1", "e2"}, "the lines end at the entries' dashes only")

    def test_two_lines_in_the_uk(self):
        one, _c = roundabouts.entry_marks(ring_with_entries(), None, "CZ", None, [])
        two, _c = roundabouts.entry_marks(ring_with_entries(), None, "GB", None, [], left_hand=True)
        self.assertEqual(len(two), 2 * len(one), "each entry's dashes twice, a gap out")

    def test_triangles_in_the_us(self):
        marks, _cuts = roundabouts.entry_marks(ring_with_entries(), None, "US", None, [])
        self.assertTrue(marks)
        self.assertTrue(all(np.asarray(m).shape == (3, 3) for m in marks))

    def test_nothing_on_a_plain_junction(self):
        two = roads.Spec(9.0, "paved", True, "primary", False)
        rs = [("a", two, np.array([(-50.0, 0.0, 0.0), (0.0, 0.0, 0.0)]), 0.0),
              ("b", two, np.array([(0.0, 0.0, 0.0), (50.0, 0.0, 0.0)]), 0.0),
              ("c", two, np.array([(0.0, 0.0, 0.0), (0.0, 50.0, 0.0)]), 0.0)]
        self.assertEqual(roundabouts.entry_marks(rs, None, "CZ", None, []), ([], {}))


class SettMaterialTest(unittest.TestCase):
    def circle_run(self, r, mat, brick=True):
        import sidewalks
        pts = arc(r, 0, 2 * math.pi, n=40)
        out = pts[:, :2] / np.hypot(pts[:, 0], pts[:, 1])[:, None] * -1.0     # inwards
        b = sidewalks.Builder((0.0, 0.0, 0.0), collision=True)
        b.add_run(pts, out, 2.0, h=0.04, caps=(False, False), slab=sidewalks.BRICK if brick else 1, mat=mat)
        return sidewalks, b

    def test_the_apron_has_its_own_material(self):
        sidewalks, b = self.circle_run(8.0, 7)
        _P, _T, _UV, M = b.arrays()
        self.assertIn(7, set(M.tolist()))
        self.assertEqual(sidewalks.MATERIALS[7], "bne_sidewalk_sett")
        self.assertNotIn(sidewalks.BRICK, set(M.tolist()), "its slab is the sett, not the brick")

    def test_a_planting_has_planar_uvs_and_its_material(self):
        sidewalks, b = self.circle_run(8.0, 8)
        P, T, UV, M = b.arrays()
        self.assertEqual(sidewalks.MATERIALS[8], "bne_island_lawn")
        slab = np.unique(T[M == 8])
        self.assertTrue(len(slab))
        self.assertTrue(np.allclose(UV[slab], P[slab, :2] / sidewalks.PLANT_REPEAT_M[0]), "metres over the repeat")

    def test_a_fan_is_a_mound_in_the_planting_material(self):
        import sidewalks
        t = np.linspace(0, 2 * math.pi, 25)
        ring = np.column_stack([5 * np.cos(t), 5 * np.sin(t), np.zeros(len(t))])
        b = sidewalks.Builder((0.0, 0.0, 0.0), collision=True)
        b.add_disc(ring, (0.0, 0.0, 0.0), 10, 0.18, 0.1)
        self.assertFalse(b.empty())
        P, T, UV, M = b.arrays()
        self.assertEqual(len(T), 24)
        self.assertEqual(set(M.tolist()), {10})
        self.assertAlmostEqual(float(P[:, 2].max()), 0.28, places=6)
        self.assertAlmostEqual(float(P[:, 2].min()), 0.18, places=6)
        n = np.cross(P[T[:, 1]] - P[T[:, 0]], P[T[:, 2]] - P[T[:, 0]])
        self.assertTrue((n[:, 2] > 0).all(), "facing up")
        self.assertEqual(len(b.col.arrays()[1]), 24, "and in the collision mesh")

    def test_a_plain_brick_run_is_unchanged(self):
        sidewalks, b = self.circle_run(8.0, 0)
        _P, _T, _UV, M = b.arrays()
        self.assertIn(sidewalks.BRICK, set(M.tolist()))
        self.assertFalse(set(M.tolist()) & {7, 8, 9, 10, 11})

    def test_the_planting_is_the_same_wherever_it_is_asked(self):
        for at in ((50.4173, 16.1690), (0.0, 0.0), (-33.9, 151.2)):
            self.assertEqual(roundabouts.planting(*at), roundabouts.planting(*at))
        slots = {roundabouts.planting(50.0 + k * 0.0137, 16.0 + k * 0.0071) for k in range(60)}
        self.assertEqual(slots, {8, 9, 10, 11}, "all four turn up")


class SignsOnIslandsTest(unittest.TestCase):
    def rows(self):
        """speedsigns._cell_rows' values of a ring split in two one-way halves and an approach."""
        out = []
        for fid, sp, pts, _l in ring_with_entries(entries=(0.0,)):
            out.append((pts[:, :2], pts[:, :2], (sp.width / 2, (int(sp.dir) or 1) if sp.oneway else 0, 2, 1.0, 0.0), [], [], [], (0, "primary")))
        return out

    def test_a_sign_inside_the_loop_goes_and_one_outside_stays(self):
        signs = np.array([[0.0, 0.0, 1.0, 0.0], [5.0, 3.0, 1.0, 0.0], [20.0, 2.0, 1.0, 0.0], [-30.0, -30.0, 1.0, 0.0]])
        got = roundabouts.drop_on_islands(signs, self.rows())
        self.assertEqual(got[:, 0].tolist(), [20.0, -30.0])

    def test_nothing_changes_without_a_ring_or_when_off(self):
        signs = np.array([[0.0, 0.0, 1.0, 0.0]])
        straight = [(np.array([(-50.0, 0.0), (50.0, 0.0)]),) * 2 + ((3.0, 0, 2, 1.0, 0.0), [], [], [], (0, "primary"))]
        self.assertEqual(len(roundabouts.drop_on_islands(signs, straight)), 1)
        with mock.patch.dict(os.environ, {"BNE_ROUNDABOUTS": "off"}):
            self.assertEqual(len(roundabouts.drop_on_islands(signs, self.rows())), 1)


class PlantsTest(unittest.TestCase):
    def fill(self, slot):
        f = islands.find(ring_with_entries())[0]
        return roundabouts.planting_fill(f, (0.6, 1.2), place_with(slot), roads.offset_polyline)

    def test_a_shrub_bed_is_denser_than_a_gravel_bed_and_all_stay_inside(self):
        ground = lambda xy: np.full(len(xy), 100.0)     # noqa: E731
        everywhere = lambda x, y: True                   # noqa: E731
        rows = {slot: roundabouts.plant_rows(self.fill(slot), ground, everywhere) for slot in (8, 9, 10, 11)}
        self.assertTrue(all(rows[s] for s in rows), "every planting has plants")
        self.assertGreater(len(rows[9]), 2 * len(rows[10]))
        edge = self.fill(9)[0]
        r_edge = float(np.hypot(edge[:, 0], edge[:, 1]).min())
        self.assertEqual({r[0] for r in rows[9]}, {"shrub", "fern"})
        self.assertEqual({r[0] for r in rows[8]}, {"tuft", "shrub"}, "clover patches in the lawn")
        for r in rows[9]:
            self.assertLess(float(np.hypot(r[1], r[2])), r_edge, "inside the planting")
            self.assertTrue(0.5 <= r[6] <= 1.5)
            self.assertGreater(r[3], 100.0, "on the mound")
        for r in rows[8]:
            if r[0] == "tuft":
                self.assertTrue(0.09 <= r[6] <= 0.15, "a patch of a hand's height")

    def test_deterministic_and_only_the_tiles_own(self):
        ground = lambda xy: np.zeros(len(xy))           # noqa: E731
        a = roundabouts.plant_rows(self.fill(9), ground, lambda x, y: True)
        self.assertEqual(a, roundabouts.plant_rows(self.fill(9), ground, lambda x, y: True))
        half = roundabouts.plant_rows(self.fill(9), ground, lambda x, y: x < 0)
        self.assertTrue(0 < len(half) < len(a) and all(r[1] < 0 for r in half))


class RingWidthTest(unittest.TestCase):
    class Stub:
        def __init__(self, ll):
            self.ll = ll

        def parts(self, i):
            return [[self.ll]]

    def circle(self, r, a0=0.0, a1=2 * math.pi, n=19, lat=50.4):
        t = np.linspace(a0, a1, n)
        k = math.cos(math.radians(lat)) * 111320.0
        return np.column_stack([16.0 + r * np.cos(t) / k, lat + r * np.sin(t) / 110540.0])

    def test_the_fitted_radius(self):
        for r in (8.0, 11.7, 30.0):
            self.assertAlmostEqual(roads.ring_radius(self.Stub(self.circle(r, 0.3, 3.9)), 0), r, delta=0.05 * r)

    def test_a_straight_or_wild_line_is_no_circle(self):
        straight = np.column_stack([np.linspace(16.0, 16.001, 12), np.full(12, 50.4)])
        self.assertIsNone(roads.ring_radius(self.Stub(straight), 0))
        self.assertIsNone(roads.ring_radius(self.Stub(self.circle(8.0)[:4]), 0), "too few points")

    def test_small_rings_are_wider(self):
        self.assertEqual(roads.ring_width(1), roads.RING_LANE_M, "unknown radius: as before")
        self.assertEqual(roads.ring_width(1, 25.0), roads.RING_LANE_M)
        self.assertAlmostEqual(roads.ring_width(1, 11.7), 6.33, delta=0.05)
        self.assertEqual(roads.ring_width(1, 3.5), roads.RING_LANE_M + roads.RING_SMALL_MAX_M)
        self.assertGreater(roads.ring_width(2, 12.0), roads.ring_width(2, 18.0))

    def test_switched_off(self):
        with mock.patch.dict(os.environ, {"BNE_ROUNDABOUTS": "off"}):
            self.assertIsNone(roads.ring_radius(self.Stub(self.circle(8.0)), 0))


class SplitterTest(unittest.TestCase):
    def setUp(self):
        self.rs = ring_with_entries(entries=(0.0, math.pi / 2, math.pi))      # entries along +x, +y, -x from r = 11.7

    def test_an_island_where_the_map_has_a_refuge(self):
        out = roundabouts.splitters(self.rs, [(11.7 + 14.0, 0.6)])
        self.assertEqual(len(out), 1)
        o = out[0]["outline"]
        self.assertEqual(out[0]["fid"], "e0")
        area = 0.5 * float(np.dot(o[:, 0], np.roll(o[:, 1], -1)) - np.dot(o[:, 1], np.roll(o[:, 0], -1)))
        self.assertGreater(area, 3.0, "counter-clockwise, a few m2")
        self.assertTrue(np.all(np.abs(o[:, 1]) < roundabouts.SPLIT_HALF_M + 1e-6), "on the road's middle")
        self.assertGreaterEqual(float(o[:, 0].min()), 11.7 + roundabouts.SPLIT_RING_CLEAR_M - 1e-6)
        self.assertAlmostEqual(float(o[:, 0].max()), 11.7 + 14.0 + roundabouts.SPLIT_FAR_M, delta=0.3)

    def test_no_island_without_a_refuge_off_the_road_or_off_the_ring(self):
        self.assertEqual(roundabouts.splitters(self.rs, []), [])
        self.assertEqual(roundabouts.splitters(self.rs, [(11.7 + 14.0, 30.0)]), [], "a node off every approach")
        self.assertEqual(roundabouts.splitters(self.rs, [(11.7 + 60.0, 0.0)]), [], "past the reach")

    def test_a_narrow_approach_keeps_its_width(self):
        narrow = [(f, sp._replace(width=5.0) if f.startswith("e") else sp, p, l) for f, sp, p, l in self.rs]
        self.assertEqual(roundabouts.splitters(narrow, [(11.7 + 14.0, 0.0)]), [])

    def test_one_island_a_road_and_off(self):
        out = roundabouts.splitters(self.rs, [(11.7 + 14.0, 0.0), (11.7 + 16.0, 0.5)])
        self.assertEqual(len(out), 1)
        with mock.patch.dict(os.environ, {"BNE_ROUNDABOUTS": "off"}):
            self.assertEqual(roundabouts.splitters(self.rs, [(11.7 + 14.0, 0.0)]), [])


class ClosedLoopTest(unittest.TestCase):
    def test_a_closed_loops_seam_has_one_normal(self):
        import sidewalks
        t = np.linspace(0, 2 * math.pi, 9)               # a coarse circle: 45 degrees a segment
        loop = np.column_stack([8 * np.cos(t), 8 * np.sin(t), np.zeros(9)])
        curb, out = sidewalks.run_geometry(loop, 1, 1.8)
        self.assertTrue(np.allclose(out[0], out[-1]), "the same normal at both ends of the seam")
        self.assertTrue(np.allclose(curb[0], curb[-1]))
        # ... pointing as a circle's would: along the radius at the seam (the point on +x)
        self.assertAlmostEqual(abs(float(out[0][1])), 0.0, places=6)
        open_ = sidewalks.run_geometry(loop[:-1], 1, 1.8)[1]
        self.assertFalse(np.allclose(open_[0], open_[-1]), "an open run keeps its one-sided ends")


if __name__ == "__main__":
    unittest.main()
