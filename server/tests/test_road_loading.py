"""Clearance equivalence and bounded, concurrent corner reuse."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest import TestCase, mock
import numpy as np
from test_pavement_corners import crossing
from test_roads import FRAME, features
import geodesy, junctions, roadcorners, roundabouts
import surface


class BoundedPavementTest(TestCase):
    def test_bounded_photo_material_and_mask_match_full_cell_geometry(self):
        # Nearby, boundary and remote junctions, with different angles/widths and one merged connector.
        lines, classes = [], []
        for j, (x, y) in enumerate([(0, 0), (60, 30), (-75, 12), (400, 400), (-800, 600)]):
            a = j * .19
            rotation = np.array([[np.cos(a), -np.sin(a)], [np.sin(a), np.cos(a)]])
            for p in ([[-100, 0], [0, 0], [100, 0]], [[0, -100], [0, 0], [0, 100]]):
                lines.append(np.asarray(p) @ rotation + [x, y])
                classes.append('secondary' if j % 2 else 'residential')
        lines += [[(200, -100), (200, 0), (200, 100)], [(210, -100), (210, 0), (210, 100)], [(200, 0), (210, 0)]]
        classes += ['residential'] * 3
        f = features(lines, classes)
        pads = roadcorners.pads
        def full(cells, urban=None, widths=None, frame=None, **kwargs):
            return pads(cells, urban, widths, frame)
        for x, y in [(-32., -32.), (48., 0.), (175., -20.), (370., 370.)]:
            with self.subTest(origin=(x, y)):
                e, n = np.meshgrid(np.linspace(x, x+64, 65), np.linspace(y, y+64, 65))
                la, lo, _ = FRAME.enu_to_geodetic(e, n, 0.)
                rgb = np.full((128, 128, 3), 220, np.uint8)
                mats = np.full((65, 65), surface.GRASS, np.uint8)
                cover = type('Cover', (), {'urban': None, 'roads': lambda *a: ([f], True)})()
                actual = (surface.photo_roads(rgb, FRAME, x, y, 64., [f]),
                          surface.paint_roads(mats, la, lo, [f], 1.),
                          surface.road_mask(FRAME, x, y, 64., 128, cover))
                with mock.patch.object(roadcorners, 'pads', full):
                    expected = (surface.photo_roads(rgb, FRAME, x, y, 64., [f]),
                                surface.paint_roads(mats, la, lo, [f], 1.),
                                surface.road_mask(FRAME, x, y, 64., 128, cover))
                for a, b in zip(actual, expected):
                    np.testing.assert_array_equal(a, b)

    def test_remote_simple_junction_skips_corner_work_but_keeps_merged_connectors(self):
        _, rows, _ = roadcorners.source([crossing((1000., 1000.))], frame=FRAME)
        with mock.patch.object(junctions, '_on_carriageway', side_effect=AssertionError('remote corner work')):
            self.assertEqual(roadcorners.find(rows, (-32., -32., 32., 32.)), [])
        f = features([[(1000, -100), (1000, 0), (1000, 100)],
                      [(1010, -100), (1010, 0), (1010, 100)], [(1000, 0), (1010, 0)]], ['residential'] * 3)
        _, rows, full = roadcorners.source([f], frame=FRAME)
        with mock.patch.object(junctions, '_branch_dir', wraps=junctions._branch_dir) as branches:
            clipped = roadcorners.find(rows, (-32., -32., 32., 32.))
            self.assertGreater(branches.call_count, 0, 'merged connector is still evaluated')
        self.assertEqual(len(full), len(clipped), 'merged connector context retained conservatively')
        for a, b in zip(full, clipped):
            np.testing.assert_array_equal(a['arc'], b['arc'])

    def test_bounds_are_part_of_corner_cache_identity(self):
        f = crossing()
        near = roadcorners.pads([f], frame=FRAME, bounds=(-32., -32., 32., 32.))
        far = roadcorners.pads([f], frame=FRAME, bounds=(1000., 1000., 1100., 1100.))
        self.assertTrue(near)
        self.assertEqual(far, [])
        self.assertIs(near, roadcorners.pads([f], frame=FRAME, bounds=(-32., -32., 32., 32.)))


class JunctionSearchTest(TestCase):
    def test_spatial_pairs_match_all_pairs_including_order_and_boundaries(self):
        import math
        rng = np.random.default_rng(125)
        cases = [rng.uniform(-100, 100, (700, 2)), rng.normal(size=(100, 2)),
                 np.array([[-4., 0.], [-.01, 0.], [0., 0.], [4., 0.], [4., .01], [0., 0.]])]
        for pos in cases:
            for radius in (0., .1, 4., 10.):
                expected = [(a, b) for a in range(len(pos)) for b in range(a + 1, len(pos))
                            if math.hypot(*(pos[a] - pos[b])) < radius]
                self.assertEqual(list(junctions._near_node_pairs(pos, radius)), expected)

    def test_separated_nodes_do_not_require_quadratic_distance_checks(self):
        pos = np.c_[np.arange(4000) * 100., np.zeros(4000)]
        with mock.patch.object(junctions.math, 'hypot', side_effect=AssertionError('remote pair')):
            self.assertEqual(list(junctions._near_node_pairs(pos, 4.)), [])


def original_overlap(points, rings, margin=0., vertical=None):
    for p, half in rings:
        a, d = p[:-1], np.diff(p, axis=0)
        length2 = np.maximum(np.sum(d[:, :2] ** 2, axis=1), 1e-12)
        for point in points:
            t = np.clip(np.sum((point[:2] - a[:, :2]) * d[:, :2], axis=1) / length2, 0., 1.)
            nearest = a + t[:, None] * d
            hit = np.linalg.norm(nearest[:, :2] - point[:2], axis=1) < half + margin
            if vertical is not None:
                hit &= np.abs(nearest[:, 2] - point[2]) < vertical
            if hit.any():
                return True
    return False


class ClearanceTest(TestCase):
    def test_matches_original_for_curves_heights_degenerate_segments_and_margins(self):
        rng = np.random.default_rng(731)
        for _ in range(45):
            rings = [(rng.normal(size=(12, 3)).cumsum(0) * 6, 2.),
                     (np.zeros((2, 3)), .5)]
            prepared = roundabouts._Corridors(rings)
            for points in (rng.normal(size=(25, 3)) * 20, rng.normal(size=(1, 3)) * 4, np.empty((0, 3))):
                for margin in (-3., -.2, 0., 4.):
                    for vertical in (None, 2., 10.):
                        self.assertEqual(roundabouts.overlaps(points, prepared, margin, vertical),
                                         original_overlap(points, rings, margin, vertical))

    def test_distant_corners_do_not_project_any_points(self):
        ring = roundabouts._Corridors([(np.array([[0., 0., 0.], [10., 10., 1.]]), 5.)])
        with mock.patch.object(np.linalg, 'norm', side_effect=AssertionError('unnecessary projection')):
            self.assertFalse(roundabouts.overlaps(np.full((1000, 3), 10000.), ring))

    def test_strict_edges_and_vertical_clearance_are_preserved(self):
        rings = [(np.array([[0., 0., 0.], [10., 0., 10.]]), 2.)]
        for point in ([5., 2., 5.], [5., 1.999999, 5.], [-2., 0., 0.],
                      [12., 0., 10.], [5., 0., 8.], [5., 0., 7.999999]):
            self.assertEqual(roundabouts.overlaps([point], rings, vertical=3.),
                             original_overlap([point], rings, vertical=3.))
        self.assertFalse(roundabouts.overlaps([[0., 0.]], [(np.empty((0, 2)), 2.)]))


class CornerCacheTest(TestCase):
    def setUp(self):
        roadcorners._CACHE.clear()

    def test_simultaneous_requests_share_geometry_and_geographic_pads(self):
        f = crossing()
        entered, release, waiting = Event(), Event(), Event()
        original = roadcorners._build
        def build(*args):
            entered.set()
            self.assertTrue(release.wait(5))
            return original(*args)
        with mock.patch.object(roadcorners, '_build', side_effect=build) as work, ThreadPoolExecutor(2) as pool:
            first = pool.submit(roadcorners.pads, [f], frame=FRAME)
            self.assertTrue(entered.wait(5))
            pending = next(iter(roadcorners._PENDING.values()))
            original_result = pending.result
            def wait_for_result():
                waiting.set()
                return original_result()
            with mock.patch.object(pending, 'result', side_effect=wait_for_result):
                second = pool.submit(roadcorners.pads, [f], frame=FRAME)
                try:
                    self.assertTrue(waiting.wait(5))
                finally:
                    release.set()
                a, b = first.result(5), second.result(5)
            self.assertIs(a, b)
            self.assertEqual(work.call_count, 1)
            self.assertEqual(len(a), 4)

    def test_failed_build_can_retry_and_changed_specs_invalidate(self):
        f = crossing()
        with mock.patch.object(roadcorners, '_build', side_effect=ValueError('temporary')):
            with self.assertRaises(ValueError):
                roadcorners.source([f], frame=FRAME)
        self.assertFalse(roadcorners._PENDING)
        first = roadcorners.source([f], frame=FRAME)
        del f.__dict__['_road_specs']
        with mock.patch.object(roadcorners, '_build', wraps=roadcorners._build) as work:
            second = roadcorners.source([f], frame=FRAME)
            self.assertEqual(work.call_count, 1)
            self.assertIsNot(first[2], second[2])

    def test_recent_entries_survive_bounded_cache_eviction(self):
        f = crossing()
        frames = [geodesy.EnuFrame(FRAME.lat0 + k * .001, FRAME.lon0, 0.) for k in range(17)]
        with mock.patch.object(roadcorners, '_build', return_value=([], [])) as work:
            for frame in frames[:16]:
                roadcorners.source([f], frame=frame)
            roadcorners.source([f], frame=frames[0])
            roadcorners.source([f], frame=frames[16])
            roadcorners.source([f], frame=frames[0])
            self.assertEqual(work.call_count, 17)
            self.assertEqual(len(roadcorners._CACHE), 16)
