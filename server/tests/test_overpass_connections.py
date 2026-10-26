"""Level short decks and continuous visible approaches through terrain LOD changes."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

import numpy as np
from test_roads import FRAME, features, flat
from test_grading import FakeMD
import blockworker, farfield, grading, heightmap, roads


class DeckConnectionTest(TestCase):
    def test_flat_and_steady_grade_spans_do_not_acquire_a_crest(self):
        for slope in (0., .025, -.025):
            x = np.arange(0., 121., 4.)
            xy = np.c_[x, np.zeros(len(x))]
            z, = roads.deck_profile([xy], lambda p: 20 + p[:, 0] * slope)
            np.testing.assert_allclose(z, 20 + x * slope, atol=1e-6)

    def test_raised_approaches_allow_a_flat_deck_without_losing_clearance(self):
        x = np.arange(0., 121., 4.)
        xy = np.c_[x, np.zeros(len(x))]
        ground = lambda p: np.where((p[:, 0] < 16) | (p[:, 0] > 104), 20., 10.)
        z, = roads.deck_profile([xy], ground, [(0, 60., 10.)])
        np.testing.assert_allclose(z, 20., atol=1e-6)
        self.assertFalse(roads._clearance_deficits([xy], [z], [(0, 60., 10.)]))

    def test_real_exit3_profiles_keep_their_approach_heights_without_added_arch(self):
        data = json.loads((Path(__file__).parent/'fixtures/i165_exit3_profiles.json').read_text())
        for row in data['profiles']:
            xy, s, ground = (np.array(row[k]) for k in ('xy', 's', 'ground'))
            def elev(p):
                at = [roads.roadmarkings.phase(xy, q) for q in p]
                return np.interp(at, s, ground)
            z, = roads.deck_profile([xy], elev)
            baseline = np.interp(s, [0, s[-1]], ground[[0, -1]])
            np.testing.assert_allclose(z[[0, -1]], ground[[0, -1]], atol=1e-6)
            self.assertLess(np.max(z - baseline), .04)
            self.assertTrue(np.all(z >= ground - 1e-6))
            self.assertLess(np.max(z - baseline), np.max(np.array(row['deck']) - baseline))

    def test_real_terrain_obstructions_are_not_flattened_into_the_slab(self):
        x = np.arange(0., 121., 4.)
        xy = np.c_[x, np.zeros(len(x))]
        ground = lambda p: 20 + 2 * np.exp(-((p[:, 0] - 60) / 10)**2)
        z, = roads.deck_profile([xy], ground)
        self.assertTrue(np.all(z >= ground(xy) - 1e-6))


class ApproachTerrainTest(TestCase):
    def test_only_tiles_containing_bridge_ends_request_finer_ground(self):
        f = features([[(40, 50), (100, 50)]], ['motorway'], flags=[(0, 'is_bridge')])
        g = grading.Grader(FRAME, FakeMD(f), flat)
        d = grading.GradedDem(None, g)
        self.assertTrue(d.bridge_approaches(FRAME, (0, 0, 256, 256)))
        self.assertFalse(d.bridge_approaches(FRAME, (512, 512, 768, 768)))

    def test_missing_bridge_metadata_does_not_block_terrain_loading(self):
        g = SimpleNamespace(_cell_enu=mock.Mock(side_effect=OSError('offline')))
        self.assertFalse(grading.GradedDem(None, g).bridge_approaches(FRAME, (0, 0, 256, 256)))

    def test_level1_worker_keeps_approach_grading_but_distant_terrain_does_not(self):
        base = SimpleNamespace(sample=lambda la, lo, zoom: np.full(np.shape(la), 10.))
        g = SimpleNamespace(apply=lambda la, lo, z: z + 8.)
        state = SimpleNamespace(anchor=lambda *a: (g, None), dem=base, airports=None, sea=None)
        def build(frame, dem, *args, **kwargs):
            return dem.sample(np.array([FRAME.lat0]), np.array([FRAME.lon0]), 16)[0]
        with mock.patch.object(blockworker, '_s', state), mock.patch.object(farfield, 'build_tile', side_effect=build):
            for level, expected in ((0, 18.), (1, 18.), (2, 10.)):
                result = blockworker._far_geometry(1, FRAME.lat0, FRAME.lon0, farfield.Tile(level, 0, 0),
                                                   16, None, None, False, farfield.V131)
                self.assertEqual(result, expected)

    def test_fine_bridge_approach_matches_physics_lattice_without_coarse_gap(self):
        class Embankment:
            def sample(self, la, lo, zoom):
                e, n, _ = FRAME.geodetic_to_enu(la, lo, 0.)
                distance = np.abs(n - .35 * e - 64.)
                return 10. + 8. * np.clip((12. - distance) / 4., 0, 1)
        dem = Embankment()
        tile = farfield.Tile(1, 0, 0)
        coarse = farfield.build_tile(FRAME, dem, tile, 16, with_sink=False)
        dem.bridge_approaches = lambda frame, box: True
        fine = farfield.build_tile(FRAME, dem, tile, 16, with_sink=False)
        self.assertEqual(fine.grid.shape, (257, 257))
        self.assertLessEqual(len(fine.positions), farfield.VERTS_MAX)
        xs = np.arange(257) * heightmap.SQUARE
        truth = heightmap.surface_u(FRAME, lambda la, lo: dem.sample(la, lo, 16), xs, xs)
        x = np.linspace(20, 480, 200)
        y = .35 * x + 64. + 6.
        at = lambda b: farfield.surface_at(b.grid, tile.size/(b.grid.shape[0]-1), x, y)
        expected = farfield.surface_at(truth, 2., x, y)
        self.assertGreater(np.max(np.abs(at(coarse) - expected)), .5)
        self.assertLessEqual(np.max(np.abs(at(fine) - expected)), .041)

    def test_bridge_detail_also_applies_in_detail_and_terrain_modes(self):
        class Ground:
            def sample(self, la, lo, zoom):
                e, n, _ = FRAME.geodetic_to_enu(la, lo, 0.)
                return 20. + .01 * e + .02 * n
            def bridge_approaches(self, frame, box):
                return True
        for level, samples in ((0, 129), (1, 257)):
            tile = farfield.Tile(level, 0, 0)
            result = farfield.build_tile(FRAME, Ground(), tile, 16, q=.01, with_sink=True)
            self.assertEqual(result.grid.shape, (samples, samples))
            self.assertLessEqual(len(result.positions), farfield.VERTS_MAX)
            if level == 0:
                # The refined mesh still sits below the drawn terrain block.
                self.assertIsNotNone(result.sink)
                xs = np.arange(samples) * heightmap.SQUARE
                truth = heightmap.surface_u(FRAME, lambda la, lo: Ground().sample(la, lo, 16), xs, xs)
                terrain = heightmap.quantise(truth, .01) * .01
                self.assertTrue(np.all(result.grid - result.sink <= terrain + 1e-5))
            else:
                self.assertIsNone(result.sink)
