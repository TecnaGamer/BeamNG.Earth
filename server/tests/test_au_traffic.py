"""fork: Australia's Give Way sign and traffic signals (furniture.au_group, junction_controls' AU rules, tools/build_au_traffic.py).
Every other country must come out exactly as before: the same controls and the stock groups."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import furniture  # noqa: E402
import roads  # noqa: E402

S = roads.Spec
BOX = (-80.0, -80.0, 80.0, 80.0)


def spec(cls="residential", width=7.0, urban=True):
    return S(width, "paved", urban, cls, False)


def tee(c_a="residential", c_b="residential", c_c="residential", **kw):
    """Three arms of a junction at the origin: east, north and west."""
    ways = [("e", c_a, [(0.0, 0, 0), (60, 0, 0)]), ("n", c_b, [(0.0, 0, 0), (0, 60, 0)]), ("w", c_c, [(0.0, 0, 0), (-60, 0, 0)])]
    return [(fid, spec(cls, **kw), np.array(pts, np.float64), 153.0) for fid, cls, pts in ways]


def controls(rs, **kw):
    (arms,) = [a for a in furniture._arms(rs).values() if len(a) >= 3]
    return [(a[0], w) for a, w in furniture.junction_controls(arms, None, **kw)]


class ControlsTest(unittest.TestCase):
    def test_untagged_minor_t_is_give_way_in_australia_not_a_stop(self):
        rs = tee()
        self.assertEqual(controls(rs), [("n", furniture.G_STOP)], "the stem of a T, as everywhere")
        self.assertEqual(controls(rs, country="AU"), [("n", furniture.G_YIELD)])
        self.assertEqual(controls(rs, country="US"), [("n", furniture.G_STOP)])

    def test_minor_arms_meeting_a_major_road_give_way(self):
        rs = tee("secondary", "residential", "secondary")
        self.assertEqual(controls(rs), [("n", furniture.G_STOP)])
        self.assertEqual(controls(rs, country="AU"), [("n", furniture.G_YIELD)])

    def test_the_maps_signs_are_used_as_mapped(self):
        rs = tee()
        prio = np.array([[0.0, 9.0, 1.0], [14.0, 0.0, 0.0]])        # a stop on the north arm, a give way on the east
        got = dict(controls(rs, country="AU", priority=prio))
        self.assertEqual(got, {"n": furniture.G_STOP, "e": furniture.G_YIELD})
        self.assertEqual(controls(rs, priority=prio), [("n", furniture.G_STOP)], "elsewhere the map's signs are not read")

    def test_rural_junctions_keep_only_the_mapped_signs(self):
        rs = tee(urban=False)
        self.assertEqual(controls(rs, country="AU"), [])
        self.assertEqual(controls(rs, country="AU", priority=np.array([[14.0, 0.0, 0.0]])), [("e", furniture.G_YIELD)])

    def test_signals_are_unchanged(self):
        rs = tee("primary", "primary", "primary")
        self.assertEqual({w for _, w in controls(rs, country="AU")}, {furniture.G_SIGNAL})
        self.assertEqual({w for _, w in controls(rs)}, {furniture.G_SIGNAL})

    def test_au_group(self):
        wide, narrow = spec("primary", 14.0), spec("primary", 7.0)
        self.assertEqual(furniture.au_group(furniture.G_YIELD, narrow), furniture.G_GIVEWAY_AU)
        self.assertEqual(furniture.au_group(furniture.G_STOP, narrow), furniture.G_STOP, "stop signs stay")
        self.assertEqual(furniture.au_group(furniture.G_SIGNAL, narrow), furniture.G_SIGNAL_AU)
        self.assertEqual(furniture.au_group(furniture.G_SIGNAL, wide), furniture.G_SIGNAL_AU_MAST)
        for g in (furniture.G_SIGNAL_AU, furniture.G_SIGNAL_AU_MAST, furniture.G_GIVEWAY_AU):
            self.assertIn(g, furniture.COLLISION_BOXES)
            self.assertEqual(furniture.COLLISION_BOXES[g][0], furniture.PROP_H[g], "the row's height is its model's")


class PropsTest(unittest.TestCase):
    def props(self, rs, country, left_hand, **kw):
        return furniture.junction_props(rs, BOX, left_hand=left_hand, country=country, **kw)

    def test_give_way_signs_stand_on_the_left_facing_the_drivers(self):
        rows = self.props(tee(), "AU", True)
        self.assertEqual([r[0] for r in rows], [furniture.G_GIVEWAY_AU])
        r = rows[0]
        self.assertEqual(r[6], 2.7)
        self.assertGreater(r[1], 0.0, "north arm: drivers heading south, their left is east of the road")
        self.assertAlmostEqual(r[4], 180.0, delta=0.5, msg="yaw: local +x toward the drivers' right, the west")
        self.assertEqual([x[0] for x in self.props(tee(), "US", False)], [furniture.G_STOP])
        self.assertEqual([x[0] for x in self.props(tee(), None, False)], [furniture.G_STOP])

    def test_signals_are_poles_or_masts_on_the_left_kerb(self):
        for width, group in ((7.0, furniture.G_SIGNAL_AU), (14.0, furniture.G_SIGNAL_AU_MAST)):
            rows = self.props(tee("primary", "primary", "primary", width=width), "AU", True)
            self.assertEqual({r[0] for r in rows}, {group}, width)
            self.assertEqual({r[6] for r in rows}, {furniture.PROP_H[group]})
            north = [r for r in rows if abs(r[4] - 180.0) < 1.0]       # (yaw: the north arm's drivers head south)
            self.assertEqual(len(north), 1)
            self.assertGreater(north[0][1], width / 2, "the north arm's left kerb: east of the carriageway")
        rows = self.props(tee("primary", "primary", "primary"), "US", False)
        self.assertEqual({r[0] for r in rows}, {furniture.G_SIGNAL})
        self.assertEqual({r[6] for r in rows}, {9.4}, "the stock boom's height")
        north = [r for r in rows if abs(r[4] - 180.0) < 1.0]       # (yaw: the north arm's drivers head south)
        self.assertLess(north[0][1], 0.0, "a stock boom's pole stands on the drivers' right")

    def test_mapped_stop_signs_stay_stop_signs(self):
        rows = self.props(tee(), "AU", True, priority=np.array([[0.0, 9.0, 1.0]]))
        self.assertEqual([r[0] for r in rows], [furniture.G_STOP])


if __name__ == "__main__":
    unittest.main()
