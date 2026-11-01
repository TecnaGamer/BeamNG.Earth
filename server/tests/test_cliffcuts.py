"""Road-cut evidence, exclusions and displaced pavement clearance."""
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cliffcuts as cuts
import roads


class RoadCutTests(unittest.TestCase):
    def setUp(self):
        self.x = np.arange(-80, 125, 4.0)
        self.y = np.arange(-80, 125, 4.0)
        self.xx, self.yy = np.meshgrid(self.x, self.y)
        cx, cy = np.meshgrid(self.x[:-1]+2, self.y[:-1]+2)
        self.centers = np.stack((cx, cy), axis=-1)
        self.segs = roads._Segs({'highway': (np.array([[0., -200], [0, 200]]), 5.)})
        self.classes = np.full(self.xx.shape, 30)
        self.labels = np.abs(self.xx) <= 7
        self.blocked = np.abs(cx) < 11
        # 22 m high, 52-degree cut rising from a bench into a 20-degree hill.
        self.h = (np.clip(self.xx-10, 0, 18)*np.tan(np.radians(52))
                  + np.maximum(self.xx-28, 0)*np.tan(np.radians(20)))

    def select(self, h=None, classes=None, segments=None):
        return cuts.select(self.h if h is None else h, self.classes if classes is None else classes,
                           self.x, self.y, 1, self.centers, self.segs if segments is None else segments,
                           self.blocked, self.labels)

    def test_road_cut_with_built_up_carriageway_is_detected(self):
        self.classes[self.labels] = 50
        keep = self.select()
        self.assertTrue(keep.any())
        self.assertFalse(keep[self.centers[..., 0] <= 10].any())
        self.assertFalse(keep[self.centers[..., 0] >= 30].any(), 'gentle hill stays terrain')

    def urban_cut(self, source, road_class='trunk'):
        spec = SimpleNamespace(urban=True, cls=road_class, width=10.)
        road = [('urban-highway', spec, np.array([[0., -200, 0], [0, 200, 0]]), 0)]
        groups = cuts.road_groups(road, source is not None)
        backed = None if source is None else cuts.terrain_backed(source, self.h, 4., 4.)
        return cuts.select_roads(self.h, self.classes, self.x, self.y, 1, self.centers,
            np.c_[self.xx.ravel(), self.yy.ravel()], groups, self.blocked, backed)

    def test_north_capital_urban_highway_cut_with_buildings_above(self):
        # North Capital of Texas Hwy, 30.30705, -97.82551: the road's urban
        # classification previously vetoed a sustained bank before slope tests.
        # Reproduce the geometry/land-cover distinction without online DEM data.
        self.classes[self.labels] = 50
        self.classes[self.xx >= 68] = 50
        selected, domain = self.urban_cut(self.h)
        face = ((self.centers[..., 0] >= 14) & (self.centers[..., 0] <= 26)
                & (np.abs(self.centers[..., 1]) < 40))
        self.assertTrue(selected[face].all(), 'continuous steep bank must be covered')
        self.assertFalse(selected[self.blocked].any(), 'pavement stays clear')
        backed = cuts.terrain_backed(self.h, self.h, 4., 4.)
        self.assertFalse(domain[~backed].any(), 'toe fitting cannot authorize urban fill')
        self.assertFalse(selected[self.centers[..., 0] >= 48].any(), 'building plateau stays protected')

    def test_urban_highway_requires_original_relief_not_graded_fill(self):
        for source in (None, np.zeros_like(self.h), self.h+20, -self.h,
                       np.full_like(self.h, np.nan)):
            with self.subTest(source='missing' if source is None else float(source[0, 0])):
                selected, domain = self.urban_cut(source)
                self.assertFalse(selected.any())
                self.assertFalse(domain.any())

    def test_urban_edge_domain_requires_fine_source_check_and_retains_land_exclusions(self):
        spec = SimpleNamespace(urban=True, cls='trunk', width=10.)
        road = [('highway', spec, np.array([[0., -200, 0], [0, 200, 0]]), 0)]
        self.classes[self.yy >= 80] = 40
        backed = cuts.terrain_backed(self.h, self.h, 4., 4.)
        args = (self.h, self.classes, self.x, self.y, 1, self.centers,
                np.c_[self.xx.ravel(), self.yy.ravel()], cuts.road_groups(road, True),
                self.blocked, backed)
        selected, domain = cuts.select_roads(*args)
        refined, rural, urban = cuts.select_roads(*args, refine_source=True)
        np.testing.assert_array_equal(refined, selected)
        self.assertFalse(rural.any())
        self.assertTrue((urban & ~backed).any(), 'fine evidence can recover rounded edges')
        np.testing.assert_array_equal(domain, urban & backed)
        self.assertFalse(urban[self.centers[..., 1] >= 78].any(), 'cultivated land remains excluded')
        empty = cuts.select_roads(*args[:-1], np.zeros_like(backed), refine_source=True)
        self.assertFalse(any(mask.any() for mask in empty), 'no confirmed cut grants no edge domain')

    def test_urban_minor_roads_and_built_up_crest_stay_excluded(self):
        for cls in ('secondary', 'tertiary', 'residential', 'service', 'motorway_link'):
            with self.subTest(road_class=cls):
                self.assertFalse(self.urban_cut(self.h, cls)[0].any())
        self.classes[self.xx >= 32] = 50
        self.assertFalse(self.urban_cut(self.h)[0].any(), 'source relief cannot override a built-up crest')

    def test_urban_exception_does_not_change_rural_selection(self):
        spec = SimpleNamespace(urban=False, cls='trunk', width=10.)
        road = [('rural-highway', spec, np.array([[0., -200, 0], [0, 200, 0]]), 0)]
        vertices = np.c_[self.xx.ravel(), self.yy.ravel()]
        expected = cuts.select(self.h, self.classes, self.x, self.y, 1, self.centers,
            self.segs, self.blocked,
            self.segs.near(vertices, cuts.ROAD_LABEL_MARGIN_M).reshape(self.h.shape),
            self.segs.near(vertices, cuts.REACH_M).reshape(self.h.shape), with_domain=True)
        for source_available in (False, True):
            actual = cuts.select_roads(self.h, self.classes, self.x, self.y, 1, self.centers,
                vertices, cuts.road_groups(road, source_available), self.blocked,
                np.zeros_like(self.blocked) if source_available else None)
            np.testing.assert_array_equal(actual, expected)

    def test_toe_domain_defers_road_clearance_but_retains_land_exclusions(self):
        self.classes[self.yy >= 80] = 40
        selected, domain = cuts.select(self.h, self.classes, self.x, self.y, 1,
            self.centers, self.segs, self.blocked, self.labels, with_domain=True)
        np.testing.assert_array_equal(selected, self.select())
        self.assertTrue(selected.any())
        self.assertFalse(selected[self.blocked].any())
        self.assertTrue(domain[self.blocked & (self.centers[..., 1] < 60)].any(),
                        'fine fitting rechecks pavement instead of inheriting a coarse stair')
        self.assertFalse(domain[self.centers[..., 1] >= 78].any(), 'cultivation is not a toe domain')
        self.assertTrue(domain[selected].all())
        empty, domain = cuts.select(self.h, self.classes, self.x, self.y, 1,
            self.centers, roads._Segs({}), self.blocked, self.labels, with_domain=True)
        self.assertFalse(empty.any())
        self.assertFalse(domain.any(), 'no road cannot grant a fitting domain')

    def test_nearby_city_and_unsafe_land_remain_excluded(self):
        for cls in (0, 40, 50, 80):
            with self.subTest(cls=cls):
                self.assertFalse(self.select(classes=np.full(self.classes.shape, cls)).any())
        self.classes[:, (self.x >= 32) & (self.x <= 40)] = 50
        self.assertFalse(self.select().any(), 'urban land beyond carriageway still buffers the cut')

    def test_no_road_downhill_fill_low_bank_and_open_hill_fail(self):
        self.assertFalse(self.select(segments=roads._Segs({})).any())
        self.assertFalse(self.select(h=-self.h).any(), 'downhill fill is not an uphill cutting')
        self.assertFalse(self.select(h=np.clip(self.xx-10, 0, 4)*1.5).any())
        self.assertFalse(self.select(h=np.maximum(self.xx-10, 0)*1.3).any(), 'no crest slope break')
        self.assertFalse(self.select(h=self.xx*1.3).any(), 'no road bench')
        self.assertFalse(self.select(h=np.maximum(self.xx-10, 0)*.6).any(), 'ordinary roadside slope')
        gentle = (np.clip(self.xx-10, 0, 24)*np.tan(np.radians(38))
                  + np.maximum(self.xx-34, 0)*.2)
        self.assertFalse(self.select(h=gentle).any(), 'bounded but no steep core')

    def test_highway_cut_near_city_requires_local_clearance_and_full_profile(self):
        # Built-up land 40 m above the crest used to suppress an entire valid
        # rural highway cut via the natural-cliff 64 m city buffer (Austin).
        self.classes[:, self.x >= 68] = 50
        self.assertFalse(self.select().any())
        def select(h):
            return cuts.select(h, self.classes, self.x, self.y, 1, self.centers,
                               self.segs, self.blocked, self.labels, np.abs(self.xx) <= 64)
        keep = select(self.h)
        self.assertTrue(keep.any())
        self.assertFalse(keep[self.centers[..., 0] >= 48].any())
        self.assertFalse(select(-self.h).any(), 'no fill slopes beside a highway')
        self.assertFalse(select(np.maximum(self.xx-10,0)*1.3).any(), 'no open hillside')
        self.classes[:, self.x >= 32] = 50
        self.assertFalse(select(self.h).any(), 'built-up land immediately at crest stays protected')

    def test_highway_road_label_can_cover_cut_but_not_its_crest(self):
        self.labels = np.abs(self.xx) <= 24
        self.classes[self.labels] = 50
        self.assertFalse(self.select().any(), 'ordinary roads cannot recover built-up faces')
        def select():
            return cuts.select(self.h, self.classes, self.x, self.y, 1, self.centers,
                               self.segs,self.blocked,self.labels,np.abs(self.xx)<=64)
        self.assertTrue(select().any(), 'road-labelled face with natural crest is a cut')
        self.classes[:] = 50
        self.labels[:] = True
        self.assertFalse(select().any(), 'a roof or wholly built-up scarp has no natural crest')

    def test_short_cut_and_missing_profile_fail_closed(self):
        self.classes[np.abs(self.yy) > 8] = 40
        self.assertFalse(self.select().any(), 'short bank fails area and length')
        self.classes[:] = 30
        h = self.h.copy()
        h[:, self.x > 28] = np.nan
        self.assertFalse(self.select(h=h).any())

    def test_highway_terraces_recover_road_labels_without_covering_benches(self):
        h = sum(np.clip(self.xx-start, 0, 8)*2 for start in (10, 26, 42))
        self.classes[:, self.x <= 32] = 50
        self.labels = np.abs(self.xx) <= 21
        keep = cuts.select(h, self.classes, self.x, self.y, 1, self.centers,
                           self.segs, self.blocked, self.labels, np.abs(self.xx) <= 64)
        for x in (14, 30, 46):
            self.assertTrue(keep[:, self.centers[0, :, 0] == x].any(), f'lift at {x} m')
        for x in (22, 38, 58):
            self.assertFalse(keep[:, self.centers[0, :, 0] == x].any(), f'flat bench at {x} m')
        self.assertFalse(self.select(h=h).any(), 'recovery is limited to major rural highways')

    def test_built_up_roof_cannot_borrow_a_higher_hillside_crest(self):
        h = (np.clip(self.xx-10, 0, 8)*2 - np.clip(self.xx-30, 0, 8)*2
             + np.clip(self.xx-42, 0, 8)*3)
        self.classes[:, self.x <= 38] = 50
        keep = cuts.select(h, self.classes, self.x, self.y, 1, self.centers,
                           self.segs, self.blocked, self.labels, np.abs(self.xx) <= 64)
        self.assertFalse(keep.any(), 'the road-to-crest path must not cross a roof and drop behind it')

    def test_recovery_does_not_erase_neighboring_urban_plateau(self):
        self.classes[self.yy >= 20] = 50
        keep = cuts.select(self.h, self.classes, self.x, self.y, 1, self.centers,
                           self.segs, self.blocked, self.labels, np.abs(self.xx) <= 64)
        self.assertTrue(keep[self.centers[..., 1] < -20].any())
        self.assertFalse(keep[self.centers[..., 1] >= 4].any(), 'nearby city land still has its local buffer')

    def test_closer_road_above_cut_does_not_hide_downhill_highway(self):
        h = sum(np.clip(self.xx-start, 0, 8)*2 for start in (10, 26, 42))
        segments = roads._Segs({'highway': (np.array([[0., -200], [0, 200]]), 5.),
                               'upper': (np.array([[64., -200], [64, 200]]), 3.)})
        keep = self.select(h=h, segments=segments)
        self.assertTrue(keep[:, self.centers[0, :, 0] == 46].any(), 'upper lift faces the lower highway')
        # Reordering roads must not alter which side supplies the cut evidence.
        reverse = roads._Segs({'upper': (np.array([[64., -200], [64, 200]]), 3.),
                              'highway': (np.array([[0., -200], [0, 200]]), 5.)})
        np.testing.assert_array_equal(keep, self.select(h=h, segments=reverse))

    def test_rotated_road_cut_and_asymmetric_spacing(self):
        self.segs = roads._Segs({'highway': (np.array([[-200., 0], [200, 0]]), 5.)})
        self.centers[..., 0] *= .8
        keep = cuts.select(self.h.T, self.classes, self.x, self.y, .8, self.centers, self.segs,
                           self.blocked.T, self.labels.T)
        self.assertTrue(keep.any())
        self.assertFalse(keep[self.centers[..., 1] >= 30].any())

    def test_smoothed_cut_retains_its_face_without_flooding_the_upper_hill(self):
        h = (np.clip(self.xx-10, 0, 24)*np.tan(np.radians(42))
             + np.maximum(self.xx-34, 0)*np.tan(np.radians(20)))
        keep = self.select(h=h)
        self.assertTrue(keep.any())
        self.assertFalse(keep[self.centers[..., 0] >= 38].any())

    def test_blocked_corridor_and_grid_boundary_do_not_bridge_components(self):
        self.blocked[20:30] = True
        keep = self.select()
        self.assertTrue(keep.any())
        self.assertFalse(keep[20:30].any())
        # The same global-lattice section must agree with a shifted tile's halo.
        trimmed = cuts.select(self.h[4:-4], self.classes[4:-4], self.x, self.y[4:-4], 1,
                              self.centers[4:-4], self.segs, self.blocked[4:-4], self.labels[4:-4])
        np.testing.assert_array_equal(keep[10:40], trimmed[6:36])

    def test_displaced_triangle_clearance_includes_edges_and_interiors(self):
        p = np.array([[7., 0, 20], [12, -2, 20], [12, 2, 20],
                      [12, 0, 20], [13, 0, 20], [12, 1, 20],
                      [-20, -20, 20], [20, -20, 20], [20, 20, 20]])
        tris = np.arange(9).reshape(-1, 3)
        kept = cuts.clear_triangles(p, tris, self.segs)
        np.testing.assert_array_equal(kept, [[3, 4, 5]])
        # Last triangle's vertices are all clear; its interior crosses the road.
        np.testing.assert_array_equal(cuts.clear_triangles(p, tris, roads._Segs({})), tris)

    def test_upper_road_clearance_checks_height_and_triangle_interiors(self):
        clearance = cuts.RoadClearance(self.segs, lambda xy: np.full(len(xy), 30.))
        p = np.array([[-20., -20, 10], [20, -20, 10], [20, 20, 10],
                      [-20, -20, 30], [20, -20, 30], [20, 20, 30],
                      [-20, -20, 10], [20, -20, 40], [20, 20, 10]])
        tris = np.arange(9).reshape(-1, 3)
        np.testing.assert_array_equal(cuts.clear_triangles(p, tris, self.segs, clearance), [[0, 1, 2]])
        unknown = cuts.RoadClearance(self.segs, lambda xy: np.full(len(xy), np.nan))
        self.assertFalse(len(cuts.clear_triangles(p, tris, self.segs, unknown)))

    def test_relaxed_buffers_require_pregrading_terrain_relief(self):
        supported = cuts.terrain_backed(self.h, self.h, 4, 4)
        face = (self.centers[..., 0] >= 14) & (self.centers[..., 0] <= 26)
        self.assertTrue(supported[face].all())
        self.assertFalse(cuts.terrain_backed(np.zeros_like(self.h), self.h, 4, 4).any(),
                         'a graded artificial embankment cannot waive road clearance')
        self.assertFalse(cuts.terrain_backed(self.h+20, self.h, 4, 4).any(),
                         'unrelated relief at another elevation cannot authorize the exception')
        self.assertFalse(cuts.terrain_backed(self.h*np.nan, self.h, 4, 4).any())

    def test_fit_clearance_recovers_wall_below_upper_road(self):
        clearance = cuts.RoadClearance(self.segs, lambda xy: np.full(len(xy), 30.))
        p = np.array([[0., 0, 10], [.5, 0, 30], [0, .5, 10]])
        tris = np.array([[0, 1, 2]])
        attrs = p[:, 2:3]/20
        self.assertFalse(len(cuts.clear_triangles(p, tris, self.segs, clearance)))
        pp, tt, aa = cuts.fit_clearance(p, tris, attrs, clearance)
        self.assertTrue(len(tt))
        self.assertGreater(pp[:, 2].max(), 25)
        self.assertLess(pp[:, 2].max(), 28)
        np.testing.assert_allclose(aa[:, 0], pp[:, 2]/20)
        np.testing.assert_array_equal(cuts.clear_triangles(pp, tt, self.segs, clearance), tt)
        # Fully clear triangles stay at their original density; road-level
        # surfaces and unknown elevations do not gain an unsafe exception.
        pp, tt, _ = cuts.fit_clearance(p-40*np.array([0, 0, 1]), tris, attrs, clearance)
        self.assertEqual(len(tt), 1)
        self.assertEqual(len(pp), 3)
        unknown = cuts.RoadClearance(self.segs, lambda xy: np.full(len(xy), np.nan))
        self.assertFalse(len(cuts.fit_clearance(p, tris, attrs, unknown)[1]))

    def test_source_match_tolerates_one_graded_corner_but_not_fill_or_reversal(self):
        source = np.array([[0., 20.], [0., 20.]])
        drawn = source.copy(); drawn[0, 1] += 9
        self.assertTrue(cuts.terrain_backed(source, drawn, 4, 4)[0, 0])
        drawn[1, 1] += 9
        self.assertFalse(cuts.terrain_backed(source, drawn, 4, 4)[0, 0])
        self.assertFalse(cuts.terrain_backed(source, 20-source, 4, 4)[0, 0])
        drawn = source.copy(); drawn[0, 0] = np.nan
        self.assertFalse(cuts.terrain_backed(source, drawn, 4, 4)[0, 0])

    def test_sloping_upper_road_keeps_full_segment_height_envelope(self):
        clearance = cuts.RoadClearance(self.segs, lambda xy: xy[:, 1]*2)
        xy = np.array([[0., 0], [0, 0], [20, 0]])
        np.testing.assert_array_equal(clearance.blocked(xy, np.array([0., -40, 0]),
            np.array([0., -40, 0]), 3.), [True, False, False])

    def test_coarse_cell_can_share_upper_pavement_but_fine_triangles_cannot(self):
        clearance = cuts.RoadClearance(self.segs, lambda xy: np.full(len(xy), 30.))
        xy = np.array([[0., 0], [0, 0], [0, 0]])
        np.testing.assert_array_equal(clearance.cell_blocked(xy,
            np.array([10., 29., 30.]), np.array([30., 31., 30.]), 3), [False, True, True])
        unknown = cuts.RoadClearance(self.segs, lambda xy: np.full(len(xy), np.nan))
        self.assertTrue(unknown.cell_blocked(xy, np.full(3, 10.), np.full(3, 30.), 3).all())
        # Spatial pruning must not change clearance within the tile.
        bounded = cuts.RoadClearance(self.segs, lambda xy: np.full(len(xy), 30.),
                                    (np.array([-40., -40]), np.array([40., 40])))
        np.testing.assert_array_equal(bounded.cell_blocked(xy, np.full(3, 10.), np.full(3, 30.), 3),
                                      clearance.cell_blocked(xy, np.full(3, 10.), np.full(3, 30.), 3))

    def test_confirmed_cut_follows_shrinking_end_but_does_not_seed_small_banks(self):
        rise = np.clip((104-self.yy)*.2, 0, 24)
        h = np.minimum(np.maximum(self.xx-10, 0)*1.5, rise)
        keep = self.select(h=h)
        end = (self.centers[..., 1] >= 70) & (self.centers[..., 1] < 90)
        self.assertTrue(keep[end].any(), 'continue beyond the old 8 m minimum rise')
        self.assertFalse(keep[self.centers[..., 1] >= 106].any(), 'stop where hill meets road')
        self.assertFalse(keep[self.centers[..., 0] >= 30].any(), 'do not cover plateau')
        self.assertFalse(self.select(h=np.minimum(h, 6)).any(), 'small banks cannot seed a cut')

    def test_end_continuation_cannot_cross_missing_land_or_road(self):
        rise = np.clip((104-self.yy)*.2, 0, 24)
        h = np.minimum(np.maximum(self.xx-10, 0)*1.5, rise)
        for cls in (0, 40, 50, 80):
            classes = self.classes.copy()
            classes[(self.yy >= 40) & (self.yy <= 52)] = cls
            keep = self.select(h=h, classes=classes)
            self.assertFalse(keep[self.centers[..., 1] >= 54].any(), f'cannot jump class {cls}')
        self.blocked[(self.centers[..., 1] >= 40) & (self.centers[..., 1] <= 52)] = True
        keep = self.select(h=h)
        self.assertFalse(keep[self.centers[..., 1] >= 54].any(), 'cannot jump protected road')

    def test_end_travel_is_bounded_in_metres_and_cannot_wrap_grid(self):
        seed = np.zeros((3, 100), bool)
        seed[1, 0] = True
        profiles = np.zeros_like(seed)
        profiles[1] = True
        keep = cuts.continue_ends(seed, profiles, 4., 3.)
        self.assertTrue(keep[1, 40])
        self.assertFalse(keep[1, 41:].any())
        self.assertFalse(keep[0].any())
        diagonal = np.eye(12, dtype=bool)
        seed = np.zeros_like(diagonal)
        seed[0, 0] = True
        self.assertTrue(cuts.continue_ends(seed, diagonal, 4., 3.)[-1, -1])
        diagonal[6, 6] = False
        self.assertFalse(cuts.continue_ends(seed, diagonal, 4., 3.)[-1, -1],
                         'diagonal traversal still cannot jump a missing profile')

    def test_short_road_labelled_end_needs_existing_natural_crested_cut(self):
        rise = np.clip((104-self.yy)*.2, 0, 24)
        h = np.minimum(np.maximum(self.xx-10, 0)*1.5, rise)
        classes = self.classes.copy()
        classes[(self.yy >= 48) & (self.xx <= 16)] = 50
        labels = np.abs(self.xx) <= 20
        def select(heights):
            return cuts.select(heights, classes, self.x, self.y, 1, self.centers,
                self.segs, self.blocked, labels, np.abs(self.xx) <= 64)
        keep = select(h)
        self.assertTrue(keep[self.centers[..., 1] >= 74].any())
        self.assertFalse(select(np.minimum(h, 6)).any(), 'road labels cannot seed small banks')


if __name__ == '__main__':
    unittest.main()
