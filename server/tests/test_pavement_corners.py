"""Rounded junctions need real ground pavement even without any sidewalks."""
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import numpy as np
from test_roads import FRAME, features, flat, rules
import roads, roadcorners, surface, grading, vegetation


def crossing(shift=(0., 0.)):
    lines = [np.array([[-100., 0.], [0., 0.], [100., 0.]]) + shift,
             np.array([[0., -100.], [0., 0.], [0., 100.]]) + shift]
    return features(lines, ['secondary'] * 2)


class PavementCornerTest(unittest.TestCase):
    def test_four_rounded_corners_without_sidewalks_or_crosswalks(self):
        f = crossing((100., 100.))
        frame, rows, corners = roadcorners.source([f], frame=FRAME)
        self.assertEqual(len(corners), 4)
        self.assertTrue(all(len(c['arc']) > 15 for c in corners))
        self.assertFalse(any(roads.has_sidewalks(sp) for _, sp, *_ in rows))
        for in_ground in (False, True):
            walks = []
            with patch.object(roads, 'ROADS_IN_GROUND', in_ground):
                chunks = roads.build_tile(FRAME, 0, 0, [f], flat, walks=walks)
            self.assertFalse(walks)
            self.assertFalse(any(c['m'] == 'crossing_white' for c in chunks))
            self.assertEqual(sum(bool(c.get('poly')) for c in chunks), 0 if in_ground else 4)
            # Edge paint follows the curved corners, not just four straight strips.
            curved = [c for c in chunks if c['m'] == 'line_white' and len(c['n']) > 10]
            self.assertGreaterEqual(len(curved), 4)

    def test_material_photo_and_high_resolution_mask_all_fill_the_corner(self):
        f = crossing()
        e, n = np.meshgrid(np.arange(-32., 32.01, .25), np.arange(-32., 32.01, .25))
        la, lo, _ = FRAME.enu_to_geodetic(e, n, 0.)
        mat = surface.paint_roads(np.full(e.shape, surface.GRASS, np.uint8), la, lo, [f], .25)
        self.assertEqual(mat[160, 160], surface.ASPHALT, '(8,8) is between the old square edge and new arc')
        self.assertNotEqual(mat[180, 180], surface.ASPHALT, '(13,13) remains roadside')
        image = np.full((257, 257, 3), 230, np.uint8)
        photo = surface.photo_roads(image, FRAME, -32., -32., 64., [f])
        self.assertTrue(np.any(photo[96, 160] != image[96, 160]))
        np.testing.assert_array_equal(photo[76, 180], image[76, 180])
        fake = SimpleNamespace(urban=None, roads=lambda *_: ([f], None))
        mask = surface.road_mask(FRAME, -32., -32., 64., 257, fake)
        self.assertGreater(mask[96, 160], 240)
        self.assertEqual(mask[76, 180], 0)

    def test_corner_grading_supports_new_pavement_on_a_hillside(self):
        _, rows, _ = roadcorners.source([crossing()], frame=FRAME)
        plans = [(fid, grading.Plan(p[:, :2], np.full(len(p), 10.), sp.width / 2,
                                    road_half=sp.width / 2)) for fid, sp, p, _ in rows]
        extra = roadcorners.grade(plans, [sp for _, sp, *_ in rows])
        self.assertEqual(len(extra), 4)
        x = np.array([8., -8., 8., -8.])
        y = np.array([8., 8., -8., -8.])
        z = grading.grade_points(x, y, np.full(4, 16.), [p for _, p in plans + extra])
        np.testing.assert_allclose(z, 10., atol=.05)

    def test_edge_curves_join_straight_edge_lines_at_both_tangents(self):
        _, rows, corners = roadcorners.source([crossing()], frame=FRAME)
        chunks, cuts = roadcorners.tile(rows, (-50., -50., 50., 50.), False)
        self.assertEqual(len(chunks), 4)
        self.assertEqual(len(cuts), 4)
        for c in chunks:
            p = np.array(c['n'])
            for at in (0, -1):
                self.assertAlmostEqual(min(abs(p[at, 0]), abs(p[at, 1])), 4.25, delta=.03)
            self.assertGreater(np.linalg.norm(np.diff(p[:, :2], axis=0), axis=1).min(), .1)

    def test_t_junction_and_grade_separated_crossing(self):
        f = features([[(-100, 0), (0, 0), (100, 0)], [(0, 0), (0, 100)]], ['secondary'] * 2)
        self.assertEqual(len(roadcorners.source([f], frame=FRAME)[2]), 2)
        f = crossing()
        f.rules['flags'] = rules([(1, 'is_bridge')])
        self.assertEqual(roadcorners.source([f], frame=FRAME)[2], [], 'no corners under an overpass')

    def test_tile_boundaries_keep_the_same_pavement(self):
        f = crossing()
        left = surface.photo_roads(np.full((128, 128, 3), 230, np.uint8), FRAME, -32., -16., 32., [f])
        right = surface.photo_roads(np.full((128, 128, 3), 230, np.uint8), FRAME, 0., -16., 32., [f])
        full = surface.photo_roads(np.full((256, 256, 3), 230, np.uint8), FRAME, -32., -32., 64., [f])
        np.testing.assert_array_equal(left[40:88, 88:116], full[104:152, 88:116])
        np.testing.assert_array_equal(right[40:88, 12:40], full[104:152, 140:168])

    def test_tree_exclusion_includes_the_new_corner(self):
        f = crossing()
        lon, lat = FRAME.lon0, FRAME.lat0
        mx, my = vegetation._to3857(lon, lat)
        scale = 1 / np.cos(np.radians(lat))
        chm = SimpleNamespace(x0=mx - 32 * scale, y0=my + 32 * scale, dx=.5 * scale, dy=.5 * scale)
        mask = np.zeros((128, 128), bool)
        vegetation._mask_roads(f, mask, chm, clear=0.)
        self.assertTrue(mask[48, 80], 'tree at (8,8) is excluded from new pavement')

    def test_props_in_added_pavement_are_removed_but_roadside_and_elevated_props_remain(self):
        _, rows, _ = roadcorners.source([crossing()], frame=FRAME)
        props = [('post', 8., 8., 0.), ('post', 13., 13., 0.), ('post', 8., 8., 12.)]
        self.assertEqual(roadcorners.clear_props(props, rows), props[1:])


if __name__ == '__main__':
    unittest.main()
