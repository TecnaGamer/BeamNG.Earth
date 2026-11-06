"""Synthetic contrasting wall contexts; fabricated geography is never presented as OSM evidence."""
import json
import unittest

import numpy as np

from test_roads import FRAME
import grading
import roads


class RoadsideWallSurfaceTest(unittest.TestCase):
    spec = roads.Spec(6., "paved", False, "tertiary")

    def road(self, spec=None, y=0.):
        pts = np.array([[-100., y, 10.], [700., y, 10.]])
        return "synthetic-profile", spec or self.spec, pts, 0.

    def walls(self, ground, road=None, box=(-120., -30., 720., 30.), decks=()):
        def unused_raw(*args):
            raise AssertionError("wall support must use the provided final drawn surface")
        return roads.retaining_walls(FRAME, [road or self.road()], unused_raw, box, decks=decks, ground=ground)

    def test_flat_rural_road_has_no_walls(self):
        self.assertEqual(self.walls(lambda x, y: np.full(np.shape(x), 10.)), [])

    def test_rolling_rural_profile_is_sampled_instead_of_a_source_vertex_chord(self):
        ground = lambda x, y: 10. + 3. * np.sin(np.pi * (np.asarray(x) + 100.) / 200.)
        self.assertEqual(self.walls(ground), [])

    def test_mountain_retaining_cut_and_legitimate_drop_parapet_are_preserved(self):
        spec = self.spec._replace(mountain=True)
        def ground(x, y):
            y = np.asarray(y)
            return 10. + 6. * np.clip((y - 4.5) / .8, 0., 1.) - 3. * np.clip((-y - 3.5) / 1.5, 0., 1.)
        walls = self.walls(ground, self.road(spec))
        cuts = [w for w in walls if not w.get("parapet")]
        parapets = [w for w in walls if w.get("parapet")]
        self.assertTrue(cuts, "a supported steep uphill cut still receives a retaining wall")
        self.assertTrue(parapets, "a genuine downhill drop still receives a stone parapet")
        self.assertTrue(all(w["mountain"] for w in cuts))
        for wall in cuts:
            self.assertLessEqual(float(np.max(wall["top"] - wall["road_z"])), grading.WALL_MAX_H_M + roads.WALL_TOP_M + 1e-8)

    def cut_ground(self, x, y):
        core = max(self.spec.width, roads.carriageway(self.spec.cls, self.spec.kind, True)) / 2 + grading.shoulder(self.spec)
        return 10. + 3. * np.clip((np.asarray(y) - core - grading.WALL_FLAT_M) / grading.WALL_T_M, 0., 1.)

    def test_wall_profile_and_taper_do_not_restart_at_tile_boundaries(self):
        whole = [w for w in self.walls(self.cut_ground, box=(0., -30., 512., 30.)) if not w.get("parapet")]
        left = [w for w in self.walls(self.cut_ground, box=(0., -30., 256., 30.)) if not w.get("parapet")]
        right = [w for w in self.walls(self.cut_ground, box=(256., -30., 512., 30.)) if not w.get("parapet")]
        self.assertTrue(whole and left and right)
        def boundary(walls):
            return [(float(w["top"][i]), float(w["foot"][i])) for w in walls
                    for i in np.flatnonzero(abs(w["base"][:, 0] - 256.) < 1e-8)]
        self.assertEqual(boundary(left), boundary(right))
        self.assertEqual(boundary(left), boundary(whole))
        self.assertGreater(boundary(left)[0][0] - 10., 2.)

    def test_overpass_suppression_and_own_bridge_or_culvert_approach_remain_distinct(self):
        road = self.road()
        high = [{"n": [[-150., 0., 16.], [750., 0., 16.]], "w": 40.}]
        low = [{"n": [[-150., 0., 10.5], [750., 0., 10.5]], "w": 40.}]
        self.assertEqual(self.walls(self.cut_ground, road, decks=high), [])
        self.assertTrue(self.walls(self.cut_ground, road, decks=low),
                        "a near-grade own transition is not treated as an overhead deck")

    def test_reloads_are_deterministic_and_input_road_geometry_is_untouched(self):
        road = self.road()
        original = road[2].copy()
        def fingerprint():
            return json.dumps(self.walls(self.cut_ground, road), sort_keys=True,
                              default=lambda v: v.tolist() if isinstance(v, np.ndarray) else v)
        self.assertEqual(fingerprint(), fingerprint())
        np.testing.assert_array_equal(road[2], original)

    def test_flat_road_support_queries_remain_batched_across_roads(self):
        counts = []
        for n in (1, 8):
            calls = []
            def ground(x, y):
                calls.append(len(x))
                return np.full(np.shape(x), 10.)
            roads_ = [(f"synthetic-{i}", self.spec, self.road(y=40. * i)[2], 0.) for i in range(n)]
            self.assertEqual(roads.retaining_walls(FRAME, roads_, None, (-120., -50., 720., 400.), ground=ground), [])
            counts.append(len(calls))
        self.assertEqual(counts[0], counts[1])
        self.assertLessEqual(counts[1], 2)


if __name__ == "__main__":
    unittest.main()
