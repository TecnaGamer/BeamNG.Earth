"""Clearance-aware bridge deck profiles (roads.deck_curve / curve_profile): no kinks,
clearance held, grade capped, never under the ground, split chains solved as one line, legacy switch."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import bridgetypes  # noqa: E402
import roads  # noqa: E402
import roadmesh  # noqa: E402


def kink(z, step):
    """Largest change of grade over 2 m."""
    gr = np.diff(z) / step
    k = max(1, int(round(2.0 / step)))
    return float(np.max(np.abs(gr[k:] - gr[:-k]))) if len(gr) > k else 0.0


def resample(lines, zs, step=0.5):
    """The chain's heights on a uniform grid along it (lines in order, end to end)."""
    P = np.concatenate([ln if i == 0 else ln[1:] for i, ln in enumerate(lines)])
    Z = np.concatenate([z if i == 0 else z[1:] for i, z in enumerate(zs)])
    S = np.r_[0.0, np.cumsum(np.hypot(*np.diff(P, axis=0).T))]
    s = np.arange(0, S[-1], step)
    return s, np.interp(s, S, Z)


def line(x0, x1, step=4.0, y=0.0):
    x = np.arange(x0, x1 + 1e-9, step)
    if x[-1] < x1:
        x = np.r_[x, x1]
    return np.c_[x, np.full(len(x), y)]


def flat(h=10.0):
    return lambda xy: np.full(len(xy), h)


class CurveProfile(unittest.TestCase):

    def test_overpass_holds_clearance_and_is_smooth(self):
        # (320 m: upstream's road clearance, 6.15 m of headroom + the slab and girders, needs ~140 m at the 6 % cap)
        ln = line(0, 320)
        road_z = 10.0
        zs = roads.deck_curve([ln], flat(10.0), crossings=[(0, 160.0, road_z)])
        s, z = resample([ln], zs)
        mid = np.abs(s - 160) <= roads.DECK_ROAD_HALF
        self.assertGreaterEqual(z[mid].min(), road_z + roads.DECK_ROAD_CLEAR - 0.15)
        self.assertLess(kink(z, 0.5), 0.02)
        self.assertLessEqual(np.max(np.abs(np.diff(z) / 0.5)), roads.DECK_MAX_GRADE + 1e-3)
        self.assertAlmostEqual(zs[0][0], 10.0, places=6)
        self.assertAlmostEqual(zs[0][-1], 10.0, places=6)

    def test_not_two_flat_pieces(self):
        # the legacy deck: flat ground, one road under the middle: flat, then a 5 % tent. The curve deck
        # climbs from the abutments: no stretch of more than 30 m stays within 0.2 m of the abutment
        ln = line(0, 300)
        zs = roads.deck_curve([ln], flat(0.0), crossings=[(0, 150.0, 0.0)])
        s, z = resample([ln], zs)
        low = z < 0.2
        self.assertLess(low.sum() * 0.5, 30.0)

    def test_grade_cap_on_short_bridge(self):
        ln = line(0, 60)
        zs = roads.deck_curve([ln], flat(0.0), crossings=[(0, 30.0, 0.0)])
        s, z = resample([ln], zs)
        self.assertLessEqual(np.max(np.abs(np.diff(z) / 0.5)), roads.DECK_MAX_GRADE + 1e-3)
        self.assertLess(kink(z, 0.5), 0.02)

    def test_never_below_ground(self):
        ln = line(0, 200)

        def hill(xy):
            return 5.0 * np.exp(-((xy[:, 0] - 100.0) / 30.0) ** 2) + 0.01 * xy[:, 0]
        zs = roads.deck_curve([ln], hill)
        g = hill(ln)
        self.assertTrue(np.all(zs[0][1:-1] >= g[1:-1] + roads.DECK_CLEAR - 1e-9))
        self.assertTrue(np.all(zs[0] >= g - 1e-9))

    def test_easing_keeps_a_terrain_peak_between_working_grid_points(self):
        # A mapped vertex can lie between both points of the end-inclusive metre grid.
        # The ground sampled at that vertex must not disappear during the round trip.
        x = np.r_[np.arange(0.0, 201.0, 4.0), 200.5]
        line_ = np.c_[x, np.zeros_like(x)]
        for peak in (0.1, 0.009):
            for reverse in (False, True):
                with self.subTest(peak=peak, reverse=reverse):
                    xy = line_[::-1] if reverse else line_
                    ground = lambda q: np.where(np.abs(q[:, 0] - 180.0) < 1e-9,
                                                peak - roads.DECK_CLEAR, -1.05)
                    z, = roads._ease([xy], [np.zeros(len(xy))], ground)
                    at = int(np.flatnonzero(xy[:, 0] == 180.0)[0])
                    self.assertGreaterEqual(z[at], peak - 1e-9)
                    self.assertTrue(np.all(z[1:-1] >= ground(xy)[1:-1] - 1e-9))
                    np.testing.assert_allclose(z[[0, -1]], 0.0, atol=1e-9)
                    if peak > 0.01:
                        self.assertGreater(z[at - 1], 0.0, "the lift is eased before the peak")
                        self.assertGreater(z[at + 1], 0.0, "the lift is eased after the peak")

    def test_lift_preserves_existing_deck_clearance_at_mapped_stations(self):
        s = np.r_[np.arange(0.0, 201.0, 4.0), 200.5]
        # The first crest is already solved, e.g. for a road beneath it.
        z = np.maximum(1.0 - np.abs(s - 100.0) / 20.0, 0.0)
        floor = np.full(len(s), -1.0)
        floor[s == 180.0] = 0.1
        got = roads._lift_smooth(s, z, floor)
        self.assertGreaterEqual(got[s == 100.0][0], 1.0)
        self.assertTrue(np.all(got >= z - 1e-10))
        self.assertGreaterEqual(got[s == 180.0][0], 0.1 - 1e-10)
        np.testing.assert_allclose(got[[0, -1]], z[[0, -1]], atol=1e-10)

    def test_water_span_clearance(self):
        ln = line(0, 1000, 4.0)

        def river(xy):
            return np.where((xy[:, 0] > 100) & (xy[:, 0] < 900), 0.0, 8.0)
        zs = roads.deck_curve([ln], river, spans=[(0, 400.0, 600.0, 0.0 + 20.0)])
        s, z = resample([ln], zs)
        m = (s >= 400) & (s <= 600)
        self.assertGreaterEqual(z[m].min(), 20.0 - 0.05)
        self.assertLess(kink(z, 0.5), 0.02)

    def test_split_chain_one_profile(self):
        # one bridge in three parts, the middle one reversed: no kink at the joins, same as one line
        a, b, c = line(0, 80), line(80, 160)[::-1], line(160, 240)
        zs = roads.deck_curve([a, b, c], flat(0.0), crossings=[(1, 40.0, 0.0)])
        one = roads.deck_curve([line(0, 240)], flat(0.0), crossings=[(0, 120.0, 0.0)])
        _, z = resample([a, b[::-1], c], [zs[0], zs[1][::-1], zs[2]])
        _, z1 = resample([line(0, 240)], one)
        self.assertLess(kink(z, 0.5), 0.02)
        self.assertLess(np.max(np.abs(z - z1[:len(z)])), 0.05)

    def test_branched_chain(self):
        # a viaduct over a road with a ramp leaving it at x=200: the trunk is one smooth line, the ramp
        # starts at the trunk's height there and comes down to its own abutment without kinks
        # (lengths: the ramp long enough to land at 6 % from upstream's higher road clearance, shorter than either
        # trunk piece so the trunk stays the longest line)
        trunk_a, trunk_b = line(0, 200), line(200, 500)
        ramp = np.c_[np.full(41, 200.0), np.arange(0, 161, 4.0)]
        zs = roads.deck_curve([trunk_a, trunk_b, ramp], flat(0.0), crossings=[(1, 60.0, 0.0)])
        self.assertIsNotNone(zs)
        _, zt = resample([trunk_a, trunk_b], zs[:2])
        self.assertLess(kink(zt, 0.5), 0.02)
        self.assertAlmostEqual(zs[2][0], zs[0][-1], delta=0.05)     # the ramp leaves the deck at its height
        self.assertAlmostEqual(zs[2][-1], 0.0, delta=1e-6)          # and lands on the ground
        _, zr = resample([ramp], [zs[2]])
        self.assertLess(kink(zr, 0.5), 0.02)
        self.assertLessEqual(np.max(np.abs(np.diff(zs[2]) / 4.0)), roads.DECK_MAX_GRADE + 0.01)

    def test_loop_falls_back(self):
        sq = [np.array([[0, 0], [50, 0.0]]), np.array([[50, 0], [50, 50.0]]), np.array([[50, 50], [0, 50.0]]),
              np.array([[0, 50], [0, 0.0]])]
        self.assertIsNone(roads.deck_curve(sq, flat(0.0)))

    def test_legacy_switch(self):
        ln = line(0, 300)
        old = os.environ.get("BNE_BRIDGE_PROFILE")
        try:
            os.environ["BNE_BRIDGE_PROFILE"] = "legacy"
            leg = roads.deck_profile_once([ln], flat(0.0), [(0, 150.0, 0.0)])
            os.environ["BNE_BRIDGE_PROFILE"] = "curve"
            new = roads.deck_profile_once([ln], flat(0.0), [(0, 150.0, 0.0)])
        finally:
            if old is None:
                os.environ.pop("BNE_BRIDGE_PROFILE", None)
            else:
                os.environ["BNE_BRIDGE_PROFILE"] = old
        self.assertFalse(np.allclose(leg[0], new[0]))


class PinnedGeometry(unittest.TestCase):
    """A pinned junction beside a separate uphill deck, 0.7 m east.

    The former artificial pin split moved the second endpoint onto that uphill deck.
    It lost its ground pin and carried the uphill deck's height onto the approach.
    All coordinates here are synthetic metres, not a claim to replay any reported site.
    """

    @staticmethod
    def fixture():
        a = line(-400, 0)
        b = np.c_[np.zeros(101), np.linspace(0.0, 400.0, 101)]
        c = np.c_[np.full(101, 0.7), np.linspace(0.0, -400.0, 101)]
        ground = lambda xy: 10.0 + 0.05 * np.maximum(-xy[:, 1], 0.0)
        return [a, b, c], ground, [(1, 200.0, 10.0)]

    def test_ground_lift_preserves_a_legitimate_five_percent_deck(self):
        lines, ground, _ = self.fixture()
        xy = lines[2]
        z = roads.deck_curve([xy], ground)[0]
        np.testing.assert_allclose(z[[0, -1]], ground(xy[[0, -1]]), atol=1e-8)
        self.assertTrue(np.all(z >= ground(xy) - 1e-8))
        # The approach is level before this 5 % hillside: vertical-curve rounding may stand above the ground.
        # The invariant is a bounded grade with grounded ends, not an arbitrary maximum rounding height.
        self.assertLessEqual(roads._curve_grade([xy], [z]), roads.DECK_MAX_GRADE + 0.01)

    def test_nearby_unconnected_deck_cannot_steal_ground_pin(self):
        lines, ground, crossings = self.fixture()
        zs = roads.deck_profile(lines, ground, crossings, pins=[(0.0, 0.0)])
        for k, (xy, z) in enumerate(zip(lines, zs)):
            cr = [(0,) + tuple(c[1:]) for c in crossings if c[0] == k]
            independent = roads.deck_curve([xy], ground, cr)[0]
            np.testing.assert_allclose(z, independent, atol=1e-8)
            np.testing.assert_allclose(z[[0, -1]], ground(xy[[0, -1]]), atol=1e-8)
            self.assertLessEqual(roads._curve_grade([xy], [z]), roads.DECK_MAX_GRADE + 0.01)
        self.assertEqual(roads._clearance_deficits(lines, zs, crossings), [])
        nodes, edges, _ = roads._curve_graph(lines, [(0.0, 0.0)])
        self.assertNotEqual(edges[0][1], edges[1][0], "the ground junction is split")
        self.assertNotEqual(edges[1][0], edges[2][0], "the deck 0.7 m away is still separate")
        np.testing.assert_allclose(nodes[edges[1][0]], lines[1][0])

    def test_pin_is_invariant_to_order_direction_and_bearing(self):
        lines, ground, crossings = self.fixture()
        reference = roads.deck_profile(lines, ground, crossings, pins=[(0.0, 0.0)])
        for angle in (0.0, 0.7, np.pi / 2):
            rotation = np.array([[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]])
            transformed = [xy[::-1] @ rotation.T for xy in lines[::-1]]
            transformed_ground = lambda xy: ground(xy @ rotation)
            got = roads.deck_profile(transformed, transformed_ground, crossings, pins=[(0.0, 0.0)])
            # The crossing lies at the middle of the middle part, so its index and arc are unchanged.
            for a, b in zip(reference, got[::-1]):
                np.testing.assert_allclose(a, b[::-1], atol=0.01)

    def test_explicit_water_clearance_stays_on_its_mapped_span(self):
        lines, ground, _ = self.fixture()
        crossing = [(1, 200.0, 10.0, 8.0)]
        zs = roads.deck_profile(lines, ground, crossing, pins=[(0.0, 0.0)])
        self.assertGreaterEqual(float(zs[1][50]), 18.0 - 0.05)
        self.assertAlmostEqual(zs[1][0], 10.0, places=8)
        independent = roads.deck_curve([lines[2]], ground)[0]
        np.testing.assert_allclose(zs[2], independent, atol=1e-8)

    def test_surface_barriers_and_collision_keep_the_pinned_approach(self):
        from test_roads import FRAME
        lines, ground, crossings = self.fixture()
        zs = roads.deck_profile(lines, ground, crossings, pins=[(0.0, 0.0)])
        spec = roads.Spec(8.0, "paved", False, "secondary")
        chunks = roads._deck_chunks([("approach", spec, np.c_[lines[1], zs[1]], 0.0)],
                                    (-10.0, -1.0, 10.0, 401.0), ground)
        chunks = [c for c in chunks if c.get("deck") and not c.get("ctx")]
        self.assertTrue(chunks)
        for c in chunks:
            left, right, _, _, _, h = roadmesh.ribbons([c])[0]
            xy = (left + right) / 2
            expected = np.interp(xy[:, 1], lines[1][:, 1], zs[1])
            np.testing.assert_allclose(h, expected, atol=roads.SIMPLIFY_Z_M + 0.002)
            surface, *_ = roadmesh.build(FRAME, [c], None, (0.0, 0.0, 0.0))
            collision, *_ = roadmesh.deck([c], (0.0, 0.0, 0.0))
            np.testing.assert_allclose(surface[:len(h), 2], h + roadmesh.DECK_SKIN)
            for edge in (left, right):
                for point, height in zip(edge, h):
                    d = np.linalg.norm(collision[:, :2] - point, axis=1)
                    self.assertTrue(np.any(np.isclose(collision[d < 0.1, 2], height, atol=1e-5)))
            runs = roadmesh.parapet_runs([c])
            self.assertTrue(runs)
            for run in runs:
                expected = np.interp(run["mid"][:, 1], lines[1][:, 1], zs[1])
                np.testing.assert_allclose(run["z"], expected, atol=roads.SIMPLIFY_Z_M + 0.002)
        near = [c for c in chunks if abs(float(c["n"][0][1])) < 1e-6]
        self.assertTrue(near)
        self.assertAlmostEqual(near[0]["n"][0][2], 10.0, places=3)


class NavPlausible(unittest.TestCase):

    def test_rejects_and_keeps(self):
        self.assertFalse(bridgetypes.nav_plausible(4.3, 161.8, 15))     # Interstate Bridge, lift span down: no
        self.assertTrue(bridgetypes.nav_plausible(41.1, 60.0, 2))       # a high fixed span over a narrow channel
        self.assertFalse(bridgetypes.nav_plausible(33.5, 74.4, 15))     # Hawthorne Bridge, raised
        self.assertFalse(bridgetypes.nav_plausible(131.1, 109.7, 10))   # Smithfield Street Bridge (a coding error)
        self.assertTrue(bridgetypes.nav_plausible(67.1, 1280.0, 13))    # Golden Gate Bridge
        self.assertTrue(bridgetypes.nav_plausible(27.4, 92.7, 16))      # Broadway Bridge (bascule, closed)
        self.assertTrue(bridgetypes.nav_plausible(5.0, 20.0, 2))        # a short span: only 3-80 m


if __name__ == "__main__":
    unittest.main()
