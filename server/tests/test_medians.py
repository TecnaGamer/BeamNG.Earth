"""medians.py (native medians.rs): median barriers between a divided highway's two carriageways, by the median's width
and the country's style, with openings at other roads, never in a carriageway, the same across tile seams."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import guardrails  # noqa: E402
import medians  # noqa: E402
import roads  # noqa: E402

BOX = (-128.0, -128.0, 128.0, 128.0)


def spec(cls="motorway", width=11.8, d=1, urban=False, oneway=True, link=False, lanes=None):
    return roads.Spec(width, "paved", urban, cls, oneway, 2.5, d, None, None, None, lanes, None, link)


def line(x, y0=-400.0, y1=400.0, n=40):
    y = np.linspace(y0, y1, n)
    return np.c_[np.full(n, x), y, np.zeros(n)]


def pair(gap, width=11.8, cls="motorway", urban=False, lanes=None):
    """Two carriageways along y, gap m of median between their edges: northbound on the east (driving on the right the
    median is on its left), southbound on the west, drawn northwards (travel against it)."""
    h = width / 2
    return [("nb", spec(cls, width, 1, urban, lanes=lanes), line(h + gap / 2), 0.0),
            ("sb", spec(cls, width, -1, urban, lanes=lanes), line(-h - gap / 2), 0.0)]


def mods(roads_near, country="US", box=BOX, left_hand=False, mapped=()):
    return medians.modules(roads_near, box, left_hand, country, mapped)


class MedianTest(unittest.TestCase):
    def test_narrow_median_concrete_in_the_middle(self):
        m = mods(pair(2.0))
        self.assertGreater(len(m), 70)                     # ~256 m of 3 m modules
        self.assertTrue((m[:, 0] == medians.JERSEY).all())
        self.assertLess(np.abs(m[:, [2, 4]]).max(), 0.01)  # on the median's middle line
        mids = (m[:, 3] + m[:, 5]) / 2
        self.assertEqual(len(np.unique(np.round(mids, 1))), len(m))   # one carriageway carries it: no doubles

    def test_width_and_country_pick_the_type(self):
        self.assertTrue((mods(pair(8.0))[:, 0] == medians.CABLE).all())                    # US rural
        self.assertTrue((mods(pair(8.0, urban=True))[:, 0] == medians.JERSEY).all())       # US urban
        self.assertTrue((mods(pair(16.0, lanes=3))[:, 0] == medians.JERSEY).all())         # six lanes: concrete
        self.assertEqual(len(mods(pair(16.0))), 0)                                         # 50 ft and more: none
        self.assertTrue((mods(pair(8.0), "DE")[:, 0] == medians.STEEL).all())
        self.assertTrue((mods(pair(8.0), "ES")[:, 0] == medians.RAILS).all())
        self.assertEqual(len(mods(pair(8.0, cls="trunk"))), 0)                             # US rural trunk
        self.assertEqual(len(mods(pair(30.0), "DE")), 0)

    def test_two_edge_rails_spread_to_the_edges(self):
        m = mods(pair(8.0), "ES")
        self.assertAlmostEqual(float(np.median(m[:, 8])), 8.0 / 2 - 0.75, places=3)
        rows = medians.fast._ef.median_rows(np.ascontiguousarray(m[:3]), np.zeros(3), np.zeros(3))
        xs = sorted(np.round(np.asarray(rows)[:, 1], 2))
        self.assertAlmostEqual(xs[0], -3.25)
        self.assertAlmostEqual(xs[-1], 3.25)

    def test_a_bend_keeps_the_wall_whole(self):
        # wide carriageways (16.6 m half widths, Phoenix's I-10) with a 4 degree kink in one: no gap, no ends at it
        k = np.radians(4.0)
        a = np.array([[19.1, -400.0], [19.1, 0.0], [19.1 + 400 * np.sin(k), 400 * np.cos(k)]])
        b = np.array([[-19.1, -400.0], [-19.1, 0.0], [-19.1 + 400 * np.sin(k), 400 * np.cos(k)]])
        near = [("nb", spec(width=33.2, urban=True), np.c_[a, np.zeros(3)], 0.0),
                ("sb", spec(width=33.2, d=-1, urban=True), np.c_[b, np.zeros(3)], 0.0)]
        m = mods(near)
        self.assertGreater(len(m), 80)
        self.assertTrue((m[:, 1] == 0).all())
        m = m[np.argsort(m[:, 3])]
        self.assertLess(np.hypot(*(m[1:, 2:4] - m[:-1, 4:6]).T).max(), 0.05)    # each module goes on from the last

    def test_no_stub_of_another_type_where_the_median_narrows_briefly(self):
        # an 8 m Spanish median narrowing to 2 m over 6 m: the edge rails go on, no 9 m concrete stub
        y = np.linspace(-400.0, 400.0, 801)
        gap = np.where(np.abs(y - 10.0) < 3.0, 2.0, 8.0)
        h = 11.8 / 2
        near = [("nb", spec(), np.c_[h + gap / 2, y, 0 * y], 0.0),
                ("sb", spec(d=-1), np.c_[-h - 8.0 / 2 + 0 * y, y, 0 * y], 0.0)]
        m = mods(near, "ES")
        self.assertTrue((m[:, 0] == medians.RAILS).all())

    def test_bridges(self):
        def decks(gap, z0=10.0, z1=10.0):
            return [(f, s, p[:, :2], np.full(len(p), z), 0.0)
                    for (f, s, p, _), z in zip(pair(gap), (z0, z1))]
        # one slab (roadmesh.MEDIAN_JOIN_M): the wall goes over at the decks' height
        m = medians.modules([], BOX, False, "US", (), decks(2.0))
        self.assertGreater(len(m), 70)
        self.assertTrue(np.allclose(m[:, 9:11], 10.0))
        # decks apart (their own parapets) or stacked: none
        self.assertEqual(len(medians.modules([], BOX, False, "US", (), decks(8.0))), 0)
        self.assertEqual(len(medians.modules([], BOX, False, "US", (), decks(2.0, 10.0, 16.0))), 0)
        # on the ground: NaN heights (the caller's ground), and a bridge over the median opens nothing
        over = [("o", spec("primary", 10.0, 0, oneway=False), np.array([[-40.0, 0.0], [40.0, 0.0]]),
                 np.full(2, 8.0), 0.0)]
        g = medians.modules(pair(2.0), BOX, False, "US", (), over)
        self.assertTrue(np.isnan(g[:, 9:11]).all())
        self.assertEqual(len(g), len(mods(pair(2.0))))

    def test_tile_seams_agree(self):
        near = pair(2.0)
        whole = mods(near)
        halves = np.concatenate([mods(near, box=(-128.0, -128.0, 128.0, 0.0)),
                                 mods(near, box=(-128.0, 0.0, 128.0, 128.0))])
        key = lambda a: sorted(map(tuple, np.round(a[:, 2:6], 6)))
        self.assertEqual(key(whole), key(halves))

    def test_opening_at_a_crossover_and_a_crossing_road(self):
        cross = ("x", spec("service", 4.0, 0, oneway=False), np.array([[-30.0, 0.0, 0.0], [30.0, 0.0, 0.0]]), 0.0)
        m = mods(pair(2.0) + [cross])
        ys = np.r_[m[:, 3], m[:, 5]]
        self.assertGreater(len(m), 50)
        self.assertGreaterEqual(np.abs(ys).min(), 2.0 + medians.OPENING_M - 0.01)
        ends = m[m[:, 1] > 0]                       # the runs end at the opening with the sloped end
        self.assertEqual(len(ends), 2)              # the two runs' sloped ends at the opening (the far ends: other tiles)
        self.assertEqual(sorted(ends[:, 1]), [1.0, 2.0])
        # two crossings 22 m apart leave ~10 m between their openings: no stub there
        cross2 = ("x2", cross[1], cross[2] + [0.0, 22.0, 0.0], 0.0)
        m = mods(pair(2.0) + [cross, cross2])
        mids = (m[:, 3] + m[:, 5]) / 2
        self.assertFalse(((mids > 0) & (mids < 22)).any())

    def test_never_in_a_carriageway(self):
        # a ramp leaving into the median and a carriageway overlapping the other: nothing on their pavement
        ramp = ("r", spec("motorway", 6.0, 1, link=True), np.array([[2.0, -50.0, 0.0], [2.0, 50.0, 0.0]]), 0.0)
        near = pair(8.0) + [ramp]
        m = mods(near)
        for x0, y0, x1, y1 in m[:, 2:6]:
            for y in (y0, y1):
                if -50 <= y <= 50:
                    self.assertGreater(abs(x0 - 2.0), 3.0 + medians.OPENING_M - 0.01)
        self.assertEqual(len(mods(pair(-4.0))), 0)  # carriageways overlapping past their shoulders: no room
        self.assertEqual(len(mods(pair(-1.0, urban=True, width=10.0))), 0)    # (no shoulders in town)

    def test_mapped_barrier_stands_for_it(self):
        mapped = [[0, 0.0, y, 0.0, 90.0, 0, 0.9, 0.5, 0.0, 1.0, 0.0, -1.0, 0.0, 0.0, 0.0, 0.0, 1.0]
                  for y in np.arange(-150.0, 150.0, 2.8)]
        self.assertEqual(len(mods(pair(4.0), mapped=mapped)), 0)

    def test_left_hand_traffic(self):
        # mirrored: the median on the driver's right
        near = [("nb", spec(d=1), line(-5.9 - 1.0), 0.0), ("sb", spec(d=-1), line(5.9 + 1.0), 0.0)]
        self.assertEqual(len(mods(near, left_hand=False)), 0)
        self.assertGreater(len(mods(near, "GB", left_hand=True)), 70)

    def test_streets_with_sidewalks_have_none(self):
        near = [(f, s._replace(cls="trunk", urban=True, sides=(1,)), p, z) for f, s, p, z in pair(2.0)]
        self.assertEqual(len(mods(near)), 0)

    def test_rows_basis(self):
        m = mods(pair(2.0))
        rows = np.asarray(medians.fast._ef.median_rows(np.ascontiguousarray(m), np.zeros(len(m)), np.zeros(len(m))))
        self.assertEqual(rows.shape[1], 17)
        for r in rows:
            c0, c1, c2 = r[8:11], r[11:14], r[14:17]
            self.assertAlmostEqual(float(np.dot(np.cross(c0, c1), c2)), 1.0, places=6)    # right-handed, no mirror
            self.assertAlmostEqual(abs(c1[1]), 1.0, places=6)          # the jersey runs along its y axis
        self.assertTrue(set(rows[:, 0].astype(int)) <= {guardrails.G_JERSEY, guardrails.G_JERSEY_END})
        self.assertEqual(guardrails.GROUPS[guardrails.G_CABLE], "cable")

    def test_style_regions(self):
        self.assertIs(medians.style("us"), medians.STYLES["na"])
        self.assertIs(medians.style(None), medians.STYLES["default"])

    def test_sweden_has_cable_along_a_2_plus_1_road_and_steel_on_a_motorway(self):
        rural_trunk, rural_motorway = 4, 1
        for row in medians.style("SE"):
            self.assertEqual(row[rural_trunk], medians.CABLE)
            self.assertEqual(row[rural_motorway], medians.STEEL)

    def test_a_fast_rural_primary_is_a_candidate_in_sweden_alone(self):
        sp = roads.Spec(4.5, "paved", False, "primary", True, 2.5, 1, 100.0)
        self.assertEqual(medians.flags_of(sp) & 1, 0)
        self.assertEqual(medians.flags_of(sp, False, "SE") & 1, 1)
        self.assertEqual(medians.flags_of(sp._replace(kmh=50.0), False, "SE") & 1, 0)
        self.assertEqual(medians.flags_of(sp._replace(urban=True, kmh=70.0), False, "SE") & 1, 0)
        fast_past_houses = medians.flags_of(sp._replace(urban=True), False, "SE")
        self.assertEqual(fast_past_houses & 1, 1)
        self.assertEqual(fast_past_houses & 8, 0, "the rural cable, not a town's kerb")
        for rows in medians.STYLES.values():
            widths = [r[0] for r in rows]
            self.assertEqual(widths, sorted(widths))
            self.assertLessEqual(widths[-1], 20.0)


if __name__ == "__main__":
    unittest.main()
