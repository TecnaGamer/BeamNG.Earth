"""Utility poles never stand on a carriageway: every pole group a style can name has a collider box, and the mapped
poles pass the same road check as the guessed ones (the user, 2026-11-11: a Czech A-frame pole in the lane of Police nad
Metuji's roundabout). Synthetic data only."""
import os
os.environ.setdefault("BNE_REGIONAL_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "regional"))
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).parent)]
import europeinfra
import franceroads
import furniture
import geodesy
import mappedpoles
import poleprofiles
import roads
from test_mapped_poles import infrastructure, line, pole

HALF = 3.5
ROAD = [(HALF, np.array([[-200., 0., 0.], [200., 0., 0.]]))]


def row(group, x, y, height=8.5, yaw=0.0):
    return [group, x, y, 0.0, yaw, 1.0, height, 0.0]


class EveryPoleGroupHasABoxTest(unittest.TestCase):
    def groups(self):
        out = {poleprofiles.STOCK.group, poleprofiles.WOOD.group, poleprofiles.RECESSED.group,
               poleprofiles.EU_WOOD.group}
        for country in europeinfra.COUNTRIES:
            for fid in range(60):
                out.add(poleprofiles.choose(poleprofiles.EU_KIT + ":" + country, str(fid)).group)
        return sorted(out)

    def test_each_group_a_style_can_name_has_a_collider(self):
        groups = self.groups()
        self.assertGreater(len(groups), 8)
        self.assertEqual([g for g in groups if g not in furniture.COLLISION_BOXES], [])

    def test_each_blocks_the_lane_and_clears_a_normal_offset(self):
        clear = furniture.RoadClearance(ROAD)
        for g in self.groups():
            with self.subTest(group=g):
                self.assertFalse(clear.clear(row(g, 0., 0.)), "in the lane")
                self.assertFalse(clear.clear(row(g, 0., HALF + 0.05)), "on the carriageway's edge")
                self.assertTrue(clear.clear(row(g, 0., HALF + furniture.POLE_OFF_M)), "where the guessed poles stand")

    def test_the_a_frame_legs_count(self):
        clear = furniture.RoadClearance(ROAD)
        # a trunk 0.5 m off the edge clears; the A-frame's legs (0.75 m out), turned across the road (the guessed
        # poles' cross-arm is across it), do not
        self.assertTrue(clear.clear(row("eu_pole_concrete_round", 0., HALF + 0.5, yaw=90.)))
        self.assertFalse(clear.clear(row("eu_pole_wood_a", 0., HALF + 0.5, yaw=90.)))

    def test_boxes_follow_the_height_scale(self):
        clear = furniture.RoadClearance(ROAD)
        self.assertTrue(clear.clear(row("eu_pole_wood_a", 0., HALF + 1.5, 8.5, 90.)))
        self.assertFalse(clear.clear(row("eu_pole_wood_a", 0., HALF + 1.5, 17.0, 90.)))


def edge_gap(point, spec, line):
    """m from a point to a road's carriageway edge (negative: on it); plain geometry, not the collider table."""
    a, b = line[:-1, :2], line[1:, :2]
    d = b - a
    t = np.clip(((point - a) * d).sum(1) / np.maximum((d * d).sum(1), 1e-12), 0., 1.)
    return float(np.hypot(*(a + t[:, None] * d - point).T).min()) - spec.width / 2


class LanternTest(unittest.TestCase):
    def test_the_classic_lantern_blocks_the_lane_but_not_the_verge(self):
        clear = furniture.RoadClearance(ROAD)
        self.assertIn("fr_lantern", furniture.COLLISION_BOXES)
        self.assertFalse(clear.clear(row("fr_lantern", 0., 0., 5.4)))
        self.assertTrue(clear.clear(row("fr_lantern", 0., HALF + 1.0, 5.4)))


class GuessedPolesTest(unittest.TestCase):
    def test_czech_poles_do_not_stand_on_the_roads(self):
        """A rural road ending on another: its last guessed pole stood on the other (no box: "clear" everywhere).
        Before the fix 5 of these 30 layouts had one on a carriageway."""
        profile = poleprofiles.EU_KIT + ":CZ"
        main = roads.Spec(7.0, "paved", False, "secondary", False)
        spur = roads.Spec(6.0, "paved", False, "residential", False)
        seen = 0
        for fid in range(30):
            layout = [("m%d" % fid, main, np.array([[-300., 0., 0.], [300., 0., 0.]]), 0.),
                      ("s%d" % fid, spur, np.array([[0., 0., 0.], [0., 400., 0.]]), 0.)]
            for r in furniture.place(layout, [], (-400., -400., 400., 400.), pole_profile=profile):
                if r[0] in poleprofiles.COLLISION_BOXES:
                    seen += 1
                    for _, spec, line, _ in layout:
                        self.assertGreater(edge_gap(np.array(r[1:3]), spec, line), 0.0, (fid, r))
        self.assertGreater(seen, 100, "too few regional poles placed: the test checks little")


class MappedPolesTest(unittest.TestCase):
    def setUp(self):
        m = patch.object(franceroads, "ON", True)
        m.start()
        self.addCleanup(m.stop)
        self.frame = geodesy.EnuFrame(50.536, 16.231)
        self.box = (-100., -100., 100., 100.)
        self.clearance = furniture.RoadClearance(ROAD, bounds=self.box)

    def build(self, records, clearance="default"):
        props, wires = [], []
        layers = [infrastructure(self.frame, records)]
        mappedpoles.place(self.frame, self.box, layers, lambda la, lo: np.zeros(np.shape(la)), props, wires,
                          None, everywhere=True,
                          clearance=self.clearance if clearance == "default" else clearance)
        return props, wires

    def test_a_pole_mapped_on_the_carriageway_is_moved_off_it(self):
        props, _ = self.build([pole("n1", 0., 2.5)])     # (1 m inside the edge)
        self.assertEqual(len(props), 1)
        self.assertTrue(self.clearance.clear(props[0]))
        self.assertLessEqual(float(np.hypot(props[0][1], props[0][2] - 2.5)), 3.0 + 0.01)

    def test_a_pole_mapped_in_the_middle_of_the_road_is_left_out(self):
        self.assertEqual(self.build([pole("n1", 0., 0.5)])[0], [])

    def test_a_pole_already_clear_stays_exactly_where_it_is_mapped(self):
        props, _ = self.build([pole("n1", 10., 12.)])
        np.testing.assert_allclose(props[0][1:3], [10., 12.], atol=0.01)

    def test_a_pole_with_no_clear_spot_near_is_left_out_with_its_spans(self):
        wide = furniture.RoadClearance([(60.0, np.array([[-200., 0., 0.], [200., 0., 0.]]))], bounds=self.box)
        records = [pole("n1", -30., 0.), pole("n2", 30., 70.), pole("n3", 60., 70.),
                   line([[-30., 0.], [30., 70.], [60., 70.]])]
        props, wires = self.build(records, clearance=wide)
        _, whole = self.build(records, clearance=None)
        self.assertEqual(len(props), 2, "the one in the middle of the wide road is gone")
        self.assertEqual(len(whole), 6, "two spans of the stock pole's three wires")
        self.assertEqual(len(wires), 3, "only the span between the two that stand")

    def test_without_a_clearance_nothing_changes(self):
        props, _ = self.build([pole("n1", 0., 0.5)], clearance=None)
        np.testing.assert_allclose(props[0][1:3], [0., 0.5], atol=0.01)


if __name__ == "__main__":
    unittest.main()
