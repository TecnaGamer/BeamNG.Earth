"""Global rural clearance, shared by pavement, paint, grading and traffic."""
import unittest
from unittest.mock import patch

import numpy as np

from test_roads import FRAME, LAT, LON, features, flat, rules
import grading
import navgraph
import roads
import surface


def rural(lat, lon):
    return np.zeros(np.shape(lat), bool)


def urban(lat, lon):
    return np.ones(np.shape(lat), bool)


def road(cls='secondary', lanes=2, width=None, **kwargs):
    f = features([[(-100., 20.), (200., 20.)]], [cls],
                 widths=[] if width is None else [(0, width)], **kwargs)
    if lanes is not None:
        f.attrs['lane_count'] = np.array([lanes], float)
    return f


class RuralRoadTest(unittest.TestCase):
    def test_global_minimum_for_mapped_and_unmapped_two_lane_classes(self):
        for lat, lon in ((41.63514, -85.01852), (52., 0.), (-33., 151.), (36., 138.)):
            for cls in sorted(roads.RURAL_LANE_CLASSES):
                for lanes in (None, 2):
                    with self.subTest(location=(lat, lon), cls=cls, lanes=lanes):
                        f = road(cls, lanes)
                        f.lonlat += [lon - LON, lat - LAT]
                        for left_hand in (False, True):
                            sp = roads.road_specs(f, rural, left_hand=left_hand)[0]
                            self.assertGreaterEqual(sp.width, 11.)
                            self.assertEqual((sp.oneway, sp.lanes), (False, 2))
                            self.assertEqual(roads.edge_margin(sp), 1.25)
                            self.assertGreaterEqual(navgraph.driving_width(sp) / 2, 4.25)
                            self.assertFalse(roads.has_sidewalks(sp))

    def test_mapped_and_measured_totals_cannot_squeeze_the_travel_lanes(self):
        for source in ('rule', 'osm', 'buildings'):
            with self.subTest(source=source):
                f = road(width=7. if source == 'rule' else None)
                if source == 'osm':
                    f.attrs['osm_width'] = np.array([7.])
                sp = roads.road_specs(f, rural, widths=lambda *_: (7., 1.))[0]
                self.assertEqual(sp.width, 11.)
                self.assertEqual(navgraph.driving_width(sp), 8.5)
        sp = roads.road_specs(road(width=15.), rural)[0]
        self.assertEqual((sp.width, sp.lanes), (15., 2), 'retain wider mapped lanes')
        sp = roads.road_specs(road(lanes=None), rural, widths=lambda *_: (7., 1.))[0]
        self.assertEqual((sp.width, sp.lanes), (11., 2), 'measurements cannot undo the inferred two lanes')

    def test_urban_and_other_lane_counts_keep_their_dimensions(self):
        for cls in sorted(roads.RURAL_LANE_CLASSES):
            for lanes in (1, 3, 4):
                f = road(cls, lanes)
                self.assertEqual(roads.road_specs(f, rural)[0].width,
                                 roads.carriageway(cls, 'paved', False, lanes=lanes))
            for width in (None, 7.):
                f = road(cls, width=width)
                self.assertEqual(roads.road_specs(f, urban)[0].width,
                                 roads.carriageway(cls, 'paved', True, width, lanes=2))
            f = road(cls)
            f.attrs['osm_width'] = np.array([7.])
            self.assertEqual(roads.road_specs(f, urban)[0].width, 7.)
        for width in (4., 16.):
            sp = roads.road_specs(road(lanes=None, width=width), rural)[0]
            self.assertEqual(sp.width, width)
            self.assertIsNone(sp.lanes, 'do not relabel shared single-lane or wide unknown-count roads')

    def test_ramps_oneways_and_non_target_surfaces_are_preserved(self):
        for cls in ('motorway', 'service', 'track', 'living_street', 'raceway'):
            f = road(cls)
            sp = roads.road_specs(f, rural)[0]
            self.assertEqual(sp.width, roads.carriageway(cls, sp.kind, False, lanes=2))
        for surf in ('gravel', 'dirt', 'cobblestone'):
            sp = roads.road_specs(road(surfaces=[(0, surf)]), rural)[0]
            self.assertEqual(sp.width, roads.carriageway('secondary', sp.kind, False, lanes=2))
        for link in (False, True):
            for direction in (-1, 1):
                f = road(lanes=1)
                f.rules['oneway'] = rules([(0, direction)], 'float')
                if link:
                    f.rules['subclass'] = rules([(0, 'link')])
                sp = roads.road_specs(f, rural)[0]
                self.assertEqual(sp.width, roads.carriageway('secondary', 'paved', False,
                                                            oneway=True, lanes=1, link=link))

    def test_ground_bridges_and_tunnels_share_clearance(self):
        for flag, get_specs in ((None, roads.road_specs), ('is_bridge', roads.bridge_specs),
                                ('is_tunnel', roads.tunnel_specs)):
            with self.subTest(flag=flag):
                f = road(width=7., flags=[] if flag is None else [(0, flag)])
                sp = get_specs(f)[0]
                self.assertEqual((sp.width, sp.lanes), (11., 2))
                self.assertEqual(navgraph.driving_width(sp), 8.5)
                p = np.array([[-100., 20., 10.], [200., 20., 10.]])
                paint = roads.extras(sp, p, -85.)
                edges = [pts for look, _, pts in paint if look['p'] == roads.EDGE_LINE['p']]
                self.assertEqual(len(edges), 2)
                for edge in edges:
                    np.testing.assert_allclose(np.abs(edge[:, 1] - 20.), 4.25)
                self.assertFalse([look for look, _, _ in paint if 'dash' in look])

    def test_mesh_ground_material_markings_ai_and_grading_cover_widened_road(self):
        f = road()
        sp = roads.road_specs(f, rural)[0]
        with patch.object(roads, 'ROADS_IN_GROUND', False):
            chunks = roads.build_tile(FRAME, 0, 0, [f], flat, urban_fn=rural)
        pavement = [c for c in chunks if c['m'] == roads.LOOK['paved']['m']]
        self.assertTrue(pavement)
        self.assertTrue(all(c['w'] == 11. for c in pavement))
        graph = navgraph.build(FRAME, [f], flat, (50., 20.), urban_fn=rural)
        self.assertTrue(graph['edges'])
        for e in graph['edges']:
            self.assertEqual((e[2], e[3], e[4], e[7]), (0, 1, 1, 4.25))
        points = np.array([[-100., 20.], [200., 20.]])
        plan = grading._plan(points, np.full(2, 10.), sp)
        self.assertGreaterEqual(plan.road_half, 5.5, 'grading must support the entire widened pavement')
        # At 5 m from centre, the original 9.5 m pavement had already ended.
        east, north = np.meshgrid(30. + np.arange(129) * .25, 4. + np.arange(129) * .25)
        lat, lon, _ = FRAME.enu_to_geodetic(east, north, np.zeros(east.shape))
        painted = surface.paint_roads(np.full(east.shape, surface.GRASS, np.uint8),
                                      lat, lon, [f], .25, urban_fn=rural)
        self.assertEqual(int(painted[84, 80]), surface.ASPHALT)
        self.assertNotEqual(int(painted[92, 80]), surface.ASPHALT)
        # The current in-ground renderer uses the native photo/mask corridor too.
        original = np.full((129, 129, 3), 240, np.uint8)
        photo = surface.photo_roads(original, FRAME, 4., 4., 32., [f], urban_fn=rural)
        self.assertTrue(np.any(photo[44, 80] != original[44, 80]), '5 m from centre is paved')
        np.testing.assert_array_equal(photo[36, 80], original[36, 80], '7 m from centre is off road')


if __name__ == '__main__':
    unittest.main()
