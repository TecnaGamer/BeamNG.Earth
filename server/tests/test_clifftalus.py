import sys
from pathlib import Path
import unittest

import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import clifftalus as talus
import cliffrocks as cr
import geodesy
import fast


class TalusTest(unittest.TestCase):
    def test_companion_seating_keeps_parent_up_and_local_face_normal(self):
        support = np.array([[10., 20., 30.], [40., 50., 60.]])
        normal = np.array([[1., 0., 0.], [0., -.6, .8]])
        up = np.array([[0., 0., 1.], [0., .12, np.sqrt(1-.12**2)]])
        heights = np.array([.7, 2.4])
        got = fast._ef.cliff_seat_companions(support, normal, up, heights, .4, .3)
        np.testing.assert_allclose(got, support-normal*(heights*.4)[:, None]-up*(heights*.3)[:, None])
        np.testing.assert_allclose(fast._ef.cliff_seat_companions(support, normal, up, heights, 0., 0.),
                                   support)
        with self.assertRaises(ValueError):
            fast._ef.cliff_seat_companions(support, normal[:1], up, heights, .4, .3)
        self.assertEqual(fast._ef.cliff_seat_companions(support[:0], normal[:0], up[:0], heights[:0],
                                                       .4, .3).shape, (0, 3))

    def test_rubble_pose_falloff_tilt_and_ground_contact(self):
        gradients = np.array([[0., 0.], [.5, 0.], [0., -.9], [0., -.9]])
        steep = np.linalg.norm(gradients, axis=1)
        metrics = np.column_stack((steep, np.degrees(np.arctan(steep)), [0., 0., 0., 22.]))
        support = np.full((4, 3), 100.)
        ids = np.array([[3, 8]] * 4, np.int64)
        props, poses = fast._ef.cliff_rubble_poses(support, gradients, metrics, ids, 22., 28., .22)
        self.assertAlmostEqual(props[3, 0], props[2, 0]*.55)
        np.testing.assert_array_equal(props[:, 1:], np.tile(props[0, 1:], (4, 1)))
        np.testing.assert_allclose(poses[:, 1:] @ poses[:, 1:].transpose(0, 2, 1),
                                   np.tile(np.eye(3), (4, 1, 1)), atol=1e-12)
        self.assertTrue(np.all(poses[:, 3, 2] >= np.cos(np.radians(28))))
        normals = np.column_stack((-gradients, np.ones(4))) / np.sqrt(1+steep**2)[:, None]
        np.testing.assert_allclose(support-poses[:, 0], normals*(props[:, 0]*.22)[:, None], atol=1e-12)
        np.testing.assert_array_equal(poses[0, 3], [0., 0., 1.])

    def test_rubble_poses_handle_empty_input_and_validate_shapes(self):
        empty3, empty2 = np.empty((0, 3)), np.empty((0, 2))
        ids = np.empty((0, 2), np.int64)
        result = fast._ef.cliff_rubble_poses(empty3, empty2, empty3, ids, 22., 28., .22)
        self.assertEqual([a.shape for a in result], [(0, 3), (0, 4, 3)])
        with self.assertRaises(ValueError):
            fast._ef.cliff_rubble_poses(empty3, empty3, empty3, ids, 22., 28., .22)
        with self.assertRaises(ValueError):
            fast._ef.cliff_rubble_poses(empty3, empty2, empty3, ids, 0., 28., .22)

    def test_outcrop_poses_stay_upright_and_bury_mass_inside_face(self):
        support = np.array([[10., 20., 30.]] * 4)
        normals = np.array([[0., 0., 1.], [.6, 0., .8], [1., 0., 0.], [0., -1., 0.]])
        ids = np.array([[-10, 30], [0, 0], [50, -10], [50, -10]], np.int64)
        properties, poses = fast._ef.cliff_outcrop_poses(support, normals, ids, (4., 10.),
                                                       (.35, .45), 12., .3)
        height, yaw, rank, slope = properties.T
        self.assertTrue(np.all((height >= 4) & (height <= 10)))
        self.assertTrue(np.all(poses[:, 3, 2] >= np.cos(np.radians(12))))
        np.testing.assert_allclose(poses[:, 1:] @ poses[:, 1:].transpose(0, 2, 1),
                                   np.tile(np.eye(3), (4, 1, 1)), atol=1e-12)
        np.testing.assert_allclose(np.cross(poses[:, 1], poses[:, 2]), poses[:, 3], atol=1e-12)
        # Undo pivot correction: the remaining displacement is only into the face.
        offset = support - poses[:, 0] - poses[:, 3] * (height * .3)[:, None]
        depth = np.sum(offset * normals, axis=1)
        self.assertTrue(np.all((depth >= height*.35-1e-12) & (depth <= height*.45+1e-12)))
        np.testing.assert_allclose(offset, normals*depth[:, None], atol=1e-12)
        np.testing.assert_array_equal(properties[2, :3], properties[3, :3])
        np.testing.assert_allclose(slope, [0, np.degrees(np.arccos(.8)), 90, 90])

    def test_companions_follow_face_distance_and_keep_world_identity(self):
        ids = np.array([[-45, 17], [18, -39], [100, 100]], np.int64)
        for normal in ([0., 0., 1.], [.8, 0., .6]):
            parents = np.column_stack((np.array([[-100., 200.], [0., 0.], [200., -100.]]),
                                       np.tile(normal, (3, 1)), [4., 7., 10.]))
            for slot in range(talus.COMPANION_COUNT):
                got = fast._ef.cliff_companion_candidates(parents, ids, slot, 5, .7, .7, 2.4)
                delta = got[:, :2] - parents[:, :2]
                delta[:, 0] *= .7
                dz = -(delta @ np.array(normal[:2])) / normal[2]
                distance = np.sqrt(np.sum(delta**2, axis=1) + dz**2)
                self.assertTrue(np.all(distance >= parents[:, 5]*.35-1e-12))
                self.assertTrue(np.all(distance <= parents[:, 5]*.65+1e-12))
                self.assertTrue(np.all((got[:, 2] >= .7) & (got[:, 2] <= 2.4)))
                self.assertTrue(np.all((got[:, 3] >= 0) & (got[:, 3] < 1)))
                for row in range(3):
                    one = fast._ef.cliff_companion_candidates(parents[row:row+1], ids[row:row+1],
                                                              slot, 5, .7, .7, 2.4)
                    np.testing.assert_array_equal(one[0], got[row])

    def test_companion_candidates_handle_empty_input_and_reject_invalid_configuration(self):
        parents, ids = np.empty((0, 6)), np.empty((0, 2), np.int64)
        self.assertEqual(fast._ef.cliff_companion_candidates(parents, ids, 0, 5, 1., .7, 2.4).shape,
                         (0, 4))
        for slot, count, scale, low, high in ((0, 0, 1., .7, 2.4), (5, 5, 1., .7, 2.4),
                                            (0, 5, 0., .7, 2.4), (0, 5, 1., 2.4, .7)):
            with self.assertRaises(ValueError):
                fast._ef.cliff_companion_candidates(parents, ids, slot, count, scale, low, high)

    def test_native_placement_requires_all_corners_and_city_clearance(self):
        classes = np.full((9, 11), 60, np.uint8)
        classes[4, 5] = 50
        classes[1, 1] = 80
        blocked = np.zeros((8, 10), bool)
        blocked[6, 8] = True
        for radius in (0, 1, 3, 100):
            got = fast._ef.cliff_allowed_cells(classes, blocked, radius)
            # Independent small-grid oracle: each cell's four corners and their
            # distance to the one urban vertex, without using native dilation.
            expected = np.zeros(blocked.shape, bool)
            for y, x in np.ndindex(blocked.shape):
                expected[y, x] = not blocked[y, x] and all(
                    classes[j, i] in (10, 20, 30, 60, 100)
                    and max(abs(j-4), abs(i-5)) > radius
                    for j in (y, y+1) for i in (x, x+1))
            np.testing.assert_array_equal(got, expected)

    def test_native_placement_rejects_invalid_grid_shapes(self):
        with self.assertRaises(ValueError):
            fast._ef.cliff_allowed_cells(np.zeros((0, 2), np.uint8), np.zeros((0, 1), bool), 0)
        with self.assertRaises(ValueError):
            fast._ef.cliff_allowed_cells(np.zeros((3, 3), np.uint8), np.zeros((3, 3), bool), 0)
        self.assertEqual(fast._ef.cliff_allowed_cells(
            np.full((1, 4), 60, np.uint8), np.zeros((0, 3), bool), 1).shape, (0, 3))

    def test_rubble_candidates_keep_subcell_ownership_and_world_jitter(self):
        origins = np.array([[-4000004., -8.], [0., 0.], [4000000., 12.]])
        points, ids, parents = fast._ef.cliff_rubble_candidates(origins, 2.)
        np.testing.assert_array_equal(parents, np.repeat(np.arange(3), 4))
        self.assertTrue(np.all((points - ids*2 >= .4-1e-9) & (points - ids*2 <= 1.6+1e-9)))
        for row, origin in enumerate(origins):
            one, one_ids, one_parent = fast._ef.cliff_rubble_candidates(origin[None, :], 2.)
            np.testing.assert_array_equal(one, points[parents==row])
            np.testing.assert_array_equal(one_ids, ids[parents==row])
            np.testing.assert_array_equal(one_parent, np.zeros(4, dtype=int))
        empty = fast._ef.cliff_rubble_candidates(np.empty((0,2)), 2.)
        self.assertEqual([a.shape for a in empty], [(0,2), (0,2), (0,)])

    def test_native_rubble_fan_rejects_uphill_steep_distant_and_missing_ground(self):
        ids = np.column_stack((np.arange(-500, 500), np.zeros(1000))).astype(np.int64)
        samples = np.zeros((1000, 6))
        samples[:, 0] = 2.
        seeds = np.zeros((1000, 6))
        seeds[:, 2] = 1.
        def select(points):
            return fast._ef.cliff_rubble_selection(points, seeds, ids, 1., 22., 5., 28., 44., .65)[0]
        self.assertTrue(select(samples).any(), "stable ground receives rubble")
        # x/y/z/gx/gy/source-z; each exclusion acts independently of hash rank.
        for column, value in ((0, -2.), (0, 23.), (1, 6.), (2, .6), (3, 1.), (2, np.nan)):
            with self.subTest(column=column, value=value):
                changed = samples.copy()
                changed[:, column] = value
                self.assertFalse(select(changed).any())

    def test_mixed_rock_palettes_match_individual_families_with_and_without_imagery(self):
        families = np.array([3, 0, 2, 1], np.int16)
        biomes = np.array([1, 2, 0, 3], np.int32)
        samples = np.array([[90, 140, 80], [180, 170, 150], [0, 0, 0], [200, 165, 115]], float)
        for rgb in (None, samples):
            expected = np.vstack([talus.mineral_tints(biomes[i:i+1], None if rgb is None else rgb[i:i+1], int(f))
                                  for i, f in enumerate(families)])
            np.testing.assert_array_equal(talus.mineral_tints(biomes, rgb, families), expected)

    def test_native_surface_normals_and_half_open_ownership(self):
        def plane(x, y):
            return np.column_stack((x, y, 2*x+3*y))
        x, y = np.array([0., 10.]), np.array([1., -20.])
        expected = np.tile(np.array([-2., -3., 1.]) / np.sqrt(14), (2, 1))
        np.testing.assert_allclose(talus._surface_normal(plane, x, y, .7), expected, atol=1e-14)
        positions = np.array([[0., 0.], [10., 0.], [0., 10.], [10., 10.], [np.nan, 5.]])
        counts = sum(talus._owned(positions, (a, b, a+10, b+10)).astype(int)
                     for a in (0., 10.) for b in (0., 10.))
        np.testing.assert_array_equal(counts, [1, 1, 1, 1, 0])

    def test_outcrop_candidates_share_world_positions_across_overlapping_tiles(self):
        fast.require(__import__("cliffsettings").CLIFF_KERNEL, "cliff outcrop candidates")
        ys = np.arange(-48., 52., 4.)
        results = []
        for xs in (np.arange(-80., 52., 4.), np.arange(-40., 92., 4.)):
            keep = np.ones((len(ys)-1, len(xs)-1), bool)
            blocked = np.zeros_like(keep)
            rows = fast._ef.cliff_outcrop_candidates(xs, ys, keep, blocked, 9., .85)
            np.testing.assert_array_equal(rows, fast._ef.cliff_outcrop_candidates(xs, ys, keep, blocked, 9., .85))
            results.append({tuple(row[:2]): tuple(row[2:4]) for row in rows})
            self.assertEqual(len(results[-1]), len(rows), "one candidate per world bucket")
            self.assertEqual(fast._ef.cliff_outcrop_candidates(xs, ys, keep, ~blocked, 9., .85).shape, (0, 6))
            self.assertEqual(fast._ef.cliff_outcrop_candidates(xs, ys, keep, blocked, 9., 0.).shape, (0, 6))
        common = results[0].keys() & results[1].keys()
        self.assertGreater(len(common), 20)
        for bucket in common:
            self.assertEqual(results[0][bucket], results[1][bucket])

    def test_toe_seeds_ignore_uphill_rims_and_keep_stable_order(self):
        xs = np.arange(5, dtype=float) * 4
        keep = np.zeros((4, 4), bool)
        keep[1:3, 1:3] = True
        heights = np.tile(xs * 3, (5, 1))
        seeds = talus.base_seeds(xs, xs, keep, heights)
        # A plane rising east can shed rubble only on its western boundary.
        np.testing.assert_array_equal(seeds, [[4, 6, -1, 0, 1, 1], [4, 10, -1, 0, 2, 1]])
        self.assertEqual(talus.base_seeds(xs, xs, keep, np.zeros_like(heights)).shape, (0, 6))

    def test_rubble_fans_choose_one_seed_per_cell_with_stable_ties(self):
        xs = np.arange(11, dtype=float) * 4
        keep = np.zeros((10, 10), bool)
        seeds = np.array([[20, 20, -1, 0, 5, 5], [20, 20, -1, 0, 5, 5]], float)
        cells = talus.base_cells(xs, xs, keep, seeds, 1.0)
        self.assertGreater(len(cells), 0)
        self.assertTrue(np.all(cells[:, 2] == 0))
        flat = cells[:, 0] * keep.shape[1] + cells[:, 1]
        self.assertTrue(np.all(np.diff(flat) > 0))
        self.assertTrue(np.all((cells[:, :2] >= 0) & (cells[:, :2] < 10)))
        np.testing.assert_array_equal(cells, talus.base_cells(xs, xs, keep, seeds, 1.0))
        self.assertEqual(talus.base_cells(xs, xs, keep, np.empty((0, 6)), 1.0).shape, (0, 3))

    def test_debug_footprints_preserve_serialized_kind_and_geographic_precision(self):
        frame = geodesy.EnuFrame(36, -114)
        rows = [[kind, 12.3, -45.6, 789, 0, 0, height]
                for kind, height in [('talus_large_rock', 8), ('talus_face_rock', 2), ('talus_rock', 1)]]
        lat, lon, _ = frame.enu_to_geodetic(12.3, -45.6, 789)
        expected = [[kind, round(float(lon), 7), round(float(lat), 7), radius]
                    for kind, radius in [(1, 4), (2, 1), (0, .5)]]
        self.assertEqual(talus.debug_footprints(frame, rows), expected)
        self.assertEqual(talus.debug_footprints(frame, []), [])

    def fixture(self, box=(0,0,160,160), classes=None, blocked=None, keep=None, **kwargs):
        x = np.arange(41)*4.
        if keep is None:
            keep = np.zeros((40,40),bool)
            keep[5:35,20:25] = True
        if classes is None: classes = np.full((41,41),60)
        if blocked is None: blocked = np.zeros((40,40),bool)
        def elev(lat,lon): return 20+np.clip(np.asarray(lon)*cr.WORLD_M_PER_DEG-80,0,20)*3
        return talus.rows(geodesy.EnuFrame(0,0),box,x,x,keep,classes,blocked,
                          np.zeros((40,40),int),elev, **kwargs)

    def test_arid_outcrops_are_embedded_across_faces_not_toe_replacements(self):
        from unittest.mock import patch
        import cliffmaterial as cm
        skin=lambda x,y,z: np.column_stack((x,y,z+3*np.sin(x/2)*np.sin(y/2)))
        with patch.object(cm,'natural_family',return_value=cm.ARID_CUT):
            rows=self.fixture(skin_at=skin)
            large=[r for r in rows if r[0].startswith('talus_large_')]
            self.assertGreater(len(large),0)
            self.assertGreater(np.ptp([r[3] for r in large]),15)
            for r in large:
                self.assertTrue(4<=r[6]<=10)
                basis=np.array(r[8:]).reshape(3,3)
                np.testing.assert_allclose(basis@basis.T,np.eye(3),atol=2e-6)
                self.assertGreaterEqual(basis[2,2],np.cos(np.radians(talus.OUTCROP_MAX_TILT_DEG))-.000001)
            with patch.object(talus,'OUTCROP_BURIAL',(0.,0.)):
                unburied=[r for r in self.fixture(skin_at=skin) if r[0].startswith('talus_large_')]
            self.assertEqual(len(large),len(unburied))
            for r,base in zip(large,unburied):
                depth=np.linalg.norm(np.array(base[1:4])-r[1:4])/r[6]
                self.assertGreater(depth,.349)
                self.assertLess(depth,.451)
            without_skin=self.fixture()
            self.assertFalse(any(r[0].startswith('talus_large_') for r in without_skin))
            for family in (cm.CANYON,cm.NATURAL):
                with patch.object(cm,'natural_family',return_value=family):
                    self.assertFalse(any(r[0].startswith('talus_large_') for r in self.fixture(skin_at=skin)))

    def test_smooth_steep_faces_do_not_receive_outcrops(self):
        from unittest.mock import patch
        import cliffmaterial as cm
        plane=lambda x,y,z: np.column_stack((x,y,20+3*(x-80)))
        with patch.object(cm,'natural_family',return_value=cm.ARID_CUT):
            rows=self.fixture(skin_at=plane)
            self.assertTrue(rows)  # loose rubble still exists
            self.assertFalse(any(r[0].startswith('talus_large_') for r in rows))
        x=np.array([10.,20.]);y=np.array([10.,20.]);h=np.array([4.,6.])
        sample=lambda x,y: np.column_stack((x,y,3*x))
        n=np.tile(np.array([-3.,0,1])/np.sqrt(10),(2,1))
        self.assertFalse(talus.rough_face(sample,x,y,sample(x,y),n,h,1).any())
        jagged=lambda x,y: np.column_stack((x,y,3*x+3*np.sin(x/2)*np.sin(y/2)))
        x,y=np.meshgrid(np.arange(4.,60.,2),np.arange(4.,60.,2));x,y=x.ravel(),y.ravel()
        dx=jagged(x+.5,y)-jagged(x-.5,y);dy=jagged(x,y+.5)-jagged(x,y-.5)
        n=np.cross(dx,dy);n/=np.linalg.norm(n,axis=1,keepdims=True)
        selected=talus.rough_face(jagged,x,y,jagged(x,y),n,np.full_like(x,5),1)
        self.assertGreater(float(selected.mean()),.01)
        self.assertLess(float(selected.mean()),.95)

    def test_large_arid_rows_keep_ownership_and_road_clearance(self):
        from unittest.mock import patch
        import cliffmaterial as cm
        import roads
        skin=lambda x,y,z: np.column_stack((x,y,z+3*np.sin(x/2)*np.sin(y/2)))
        with patch.object(cm,'natural_family',return_value=cm.ARID_CUT):
            whole=self.fixture(skin_at=skin)
            left=self.fixture((0,0,90,160),skin_at=skin);right=self.fixture((90,0,160,160),skin_at=skin)
            self.assertEqual({tuple(r) for r in whole},{tuple(r) for r in left+right})
            corridor=roads._Segs({'road':(np.array([[85.,-100.],[85.,300.]]),3.)})
            rows=self.fixture(skin_at=skin,road_segments=corridor)
            self.assertLess(len(rows),len(whole))
            self.assertTrue(all(talus.road_clear(np.array([r[1:4]]),np.array([r[6]]),corridor)[0] for r in rows))

    def test_outcrop_companions_share_tint_and_have_varied_smaller_sizes(self):
        from unittest.mock import patch
        import cliffmaterial as cm
        skin=lambda x,y,z: np.column_stack((x,y,z+3*np.sin(x/2)*np.sin(y/2)))
        with patch.object(cm,'natural_family',return_value=cm.ARID_CUT):
            rows=self.fixture(skin_at=skin)
            self.assertEqual(rows,self.fixture(skin_at=skin))
            large=[r for r in rows if r[0].startswith('talus_large_')]
            small=[r for r in rows if r[0].startswith('talus_face_')]
            self.assertGreater(len(small),0)
            self.assertGreater(np.ptp([r[6] for r in large]),2)
            self.assertGreater(np.ptp([r[6] for r in small]),.5)
            for r in small:
                self.assertTrue(.7<=r[6]<=2.4)
                self.assertIn(r[0][-6:],{b[0][-6:] for b in large})
                distance=min(np.linalg.norm(np.array(r[1:4])-b[1:4]) for b in large)
                self.assertLess(distance,12)
                basis=np.array(r[8:]).reshape(3,3)
                np.testing.assert_allclose(basis@basis.T,np.eye(3),atol=2e-6)
                self.assertGreaterEqual(basis[2,2],np.cos(np.radians(12))-.000001)
            plane=lambda x,y,z: np.column_stack((x,y,20+3*(x-80)))
            self.assertFalse(any(r[0].startswith('talus_face_') for r in self.fixture(skin_at=plane)))

    def test_toe_rubble_uses_source_detector_material(self):
        from unittest.mock import patch
        import cliffmaterial as cm
        with patch.object(talus,'mineral_tints',wraps=talus.mineral_tints) as tint:
            self.fixture(families=np.full((40,40),cm.ARID_CUT))
            self.assertTrue(tint.called)
            np.testing.assert_array_equal(tint.call_args.args[2],cm.ARID_CUT)

    def test_base_rubble_is_buried_further_below_support(self):
        from unittest.mock import patch
        with patch.object(talus,'RUBBLE_BURIAL',.12): previous=self.fixture()
        current=self.fixture()
        self.assertEqual(len(previous),len(current))
        for old,new in zip(previous,current):
            self.assertLess(new[3],old[3])
            self.assertAlmostEqual(np.linalg.norm(np.array(new[1:4])-old[1:4])/new[6],.10,delta=.003)

    def test_density_favors_stable_ground_below_cliff(self):
        ids = np.column_stack((np.arange(-1000,1000), np.zeros(2000))).astype(np.int64)
        samples, seeds = np.zeros((2000,6)), np.zeros((2000,6))
        seeds[:,2] = 1
        counts = []
        for slope in (0,25,28,35,44,78,85):
            samples[:,3] = np.tan(np.radians(slope))
            selected, _ = fast._ef.cliff_rubble_selection(samples,seeds,ids,1.,22.,5.,28.,44.,.65)
            counts.append(selected.sum())
        self.assertGreater(counts[0], 0)
        self.assertEqual(counts[:3], [counts[0]]*3)
        self.assertGreater(counts[2], counts[3])
        self.assertEqual(counts[4:], [0,0,0])
        self.assertEqual(self.fixture(keep=np.zeros((40,40),bool)),[])

    def test_deterministic_settled_instances_only_below_cliff(self):
        rows = self.fixture()
        self.assertGreater(len(rows),20)
        self.assertEqual(rows,self.fixture())
        for r in rows:
            self.assertEqual(len(r),17)
            self.assertTrue(r[0].startswith('talus_'))
            self.assertGreater(r[1],80-talus.REACH_M)
            self.assertLess(r[1],80)  # no wall or high plateau/crest rocks
            self.assertGreater(r[2],20-talus.SCATTER_M)
            self.assertLess(r[2],140+talus.SCATTER_M)
            basis = np.array(r[8:]).reshape(3,3)
            np.testing.assert_allclose(basis@basis.T,np.eye(3),atol=2e-6)
            tilt = np.degrees(np.arccos(np.clip(basis[2,2],-1,1)))
            self.assertLessEqual(tilt,talus.MAX_TILT_DEG+.01)
        self.assertTrue(any(r[1]<70 for r in rows))  # downslope runout
        self.assertTrue(any(r[1]>76 for r in rows))  # scattered toe contact
        self.assertGreater(np.std([r[1] for r in rows]),3)

    def test_seating_samples_faded_skin_instead_of_full_displacement(self):
        xs = np.arange(9)*.5
        h = np.tile(xs*2,(9,1))
        keep = np.ones((8,8),bool)
        surface,_,_ = cr.displaced_surface(h,keep,xs,xs,np.zeros((8,8),int))
        sample = talus.surface_sampler(xs,xs,h,surface)
        # The buried boundary sits on terrain, never at the full interior offset.
        rim = sample(np.array([0.]),np.array([2.]),np.array([0.]))[0]
        np.testing.assert_allclose(rim,(0,2,0),atol=1e-10)
        center = sample(np.array([2.]),np.array([2.]),np.array([4.]))[0]
        np.testing.assert_allclose(center,surface[4,4],atol=1e-10)
        self.assertGreater(center[2],4)
        self.assertLess(center[0],2)
        # Triangle interpolation matches the rendered A-B-C diagonal exactly.
        offset = surface-np.stack((*np.meshgrid(xs,xs),h),axis=-1)
        offset[...,2] = np.maximum(offset[...,2],0)
        p = sample(np.array([1.4]),np.array([1.1]),np.array([2.8]))[0]
        expected = np.array([1.4,1.1,2.8])+.2*offset[2,2]+.6*offset[2,3]+.2*offset[3,3]
        np.testing.assert_allclose(p,expected,atol=1e-10)

    def test_tints_match_canyon_and_classic_cliff_albedo(self):
        import json
        import cliffcolors as cc
        import cliffmaterial as cm
        means = np.asarray(json.loads(Path(cr.__file__).with_name('cliff_transition_means.json').read_text()))
        rgb = np.array([[90,140,80],[110,75,110],[180,170,150],[200,165,115],[0,0,0]])
        biome = np.zeros(5,int)
        for family in (cm.NATURAL,cm.ARID_CUT,cm.CANYON):
            tint = (cc.canyon_tint(biome,rgb) if family==cm.CANYON else cr.rock_colors(biome,rgb,family))[:,:3]
            expected = cc.srgb(cc.linear(tint/255)*means[family])*255
            actual = talus.mineral_tints(biome,rgb,family)
            np.testing.assert_allclose(actual,expected,atol=talus.TINT_STEP/2)
        self.assertFalse(np.array_equal(talus.mineral_tints(biome,rgb,cm.NATURAL),
                                        talus.mineral_tints(biome,rgb,cm.CANYON)))

    def test_adjacent_tiles_own_each_instance_once(self):
        whole = self.fixture()
        left,right = self.fixture((0,0,70,160)),self.fixture((70,0,160,160))
        freeze = lambda rows: {tuple(r) for r in rows}
        self.assertFalse(freeze(left)&freeze(right))
        self.assertEqual(freeze(whole),freeze(left)|freeze(right))

    def test_full_footprint_clears_bends_and_endpoints(self):
        import roads
        corridor = roads._Segs({'bend': (np.array([[0.,0.],[20.,0.],[20.,20.]]), 3.)})
        points = np.array([[10,6,0],[10,9,0],[24,15,0],[24,24,0],[30,30,0]],float)
        # Centres are all off-road, but wide clusters overlap its sides/end cap.
        np.testing.assert_array_equal(talus.road_clear(points,np.full(5,2.),corridor),
                                      [False,True,False,False,True])
        self.assertTrue(talus.road_clear(points[2:3],np.array([.2]),corridor)[0])

    def test_road_check_uses_final_seated_position(self):
        import roads
        corridor = roads._Segs({'road': (np.array([[100.,-100.],[100.,300.]]), 3.)})
        self.assertEqual(self.fixture(),self.fixture(road_segments=corridor))
        def shifted(e,n,z):
            return np.column_stack((np.full_like(e,100.),n,z))
        self.assertGreater(len(self.fixture(skin_at=shifted)),20)
        self.assertEqual(self.fixture(skin_at=shifted,road_segments=corridor),[])

    def test_elevated_roads_and_tunnel_corridors_excluded(self):
        import surface
        from test_roads import FRAME, features
        feats = features([[(0,0),(200,0)]],['unclassified'],widths=[(0,8)],
                         flags=[(0,'is_bridge',.25,.5),(0,'is_tunnel',.65,.85)])
        ground = surface._roads_list_enu(FRAME,[feats],None,None)
        corridor = talus.road_exclusion(FRAME,[feats],None,None,ground)
        points = np.array([[20,0,0],[75,0,0],[150,0,0],[75,20,0]],float)
        np.testing.assert_array_equal(talus.road_clear(points,np.ones(4),corridor),
                                      [False,False,False,True])

    def test_open_road_inside_long_tunnel_tag_excludes_rubble_footprint(self):
        import surface
        from test_roads import FRAME, features
        for flag in ('is_tunnel', 'is_bridge'):
            feats = features([[(0,0),(400,0)]],['unclassified'],widths=[(0,8)],
                             flags=[(0,flag,0.,1.)])
            ground = surface._roads_list_enu(FRAME,[feats],None,None)
            corridor = talus.road_exclusion(FRAME,[feats],None,None,ground)
            points = np.array([[200,0,0],[200,7,0],[200,20,0]],float)
            # The large rock's centre clears the road, but its footprint overlaps.
            np.testing.assert_array_equal(talus.road_clear(points,np.array([1.,2.,2.]),corridor),
                                          [False,False,True])

    def test_city_road_and_water_exclusions_survive_rubble_growth(self):
        self.assertEqual(self.fixture(blocked=np.ones((40,40),bool)),[])
        for code in (50,80,0):
            self.assertEqual(self.fixture(classes=np.full((41,41),code)),[])
        c = np.full((41,41),60)
        c[20,22] = 50
        self.assertEqual(self.fixture(classes=c),[])


if __name__ == '__main__': unittest.main()
