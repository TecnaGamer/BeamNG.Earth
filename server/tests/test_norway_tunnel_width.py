import json
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import geodesy
import grading
import norway_tunnels
import roadmesh
import roads
import surface


def line(*stations):
    return np.c_[stations, np.zeros(len(stations))]


class Frame:
    def enu_to_geodetic(self, x, y, z):
        return y, x, z


def spec(width, lanes=2, oneway=False):
    return roads.Spec(width, "paved", False, "trunk", oneway, lanes=lanes)


def fit(parts, approaches):
    profiles = object.__new__(roads.TunnelProfiles)
    profiles.frame = Frame()
    pool = {(fid, 0): (road, xy) for fid, road, xy in parts}
    degree = {}
    joins = {}
    for key, (_, xy) in pool.items():
        joins[key] = (float(xy[0, 0]), float(xy[-1, 0]))
        for endpoint in joins[key]:
            degree[endpoint] = degree.get(endpoint, 0) + 1
    ground = {}
    for fid, _, xy in approaches:
        ground.setdefault((fid, "g"), []).append(xy)
    profiles._hw = {(fid, "g"): width / 2 for fid, width, _ in approaches}
    with mock.patch.object(norway_tunnels, "in_norway", return_value=True), \
            mock.patch.object(roads, "taper_widths", return_value=None):
        profiles._fit_norwegian_widths(list(pool), pool, ground, joins, degree)
    return [pool[(fid, 0)][0] for fid, *_ in parts]


class TunnelWidths(unittest.TestCase):
    def test_two_agreeing_approaches_keep_the_measured_width_through_the_bore(self):
        fitted, = fit([("bore", spec(5.5), line(0., 120.))],
                      [("bore", 2.5, line(-30., 0.)), ("bore", 2.5, line(120., 150.))])
        np.testing.assert_allclose(fitted.width_at([0., .25, .5, .75, 1.]), [2.5] * 5)

    def test_agreeing_approaches_keep_width_across_connected_parts(self):
        entrance, interior, exit_ = fit(
            [("a", spec(5.5), line(0., 40.)), ("b", spec(7.), line(80., 40.)), ("c", spec(5.5), line(80., 120.))],
            [("a", 2.5, line(-30., 0.)), ("c", 2.5, line(120., 150.))],
        )
        for part in (entrance, interior, exit_):
            np.testing.assert_allclose(part.width_at([0., .5, 1.]), [2.5] * 3)

    def test_the_cached_uniform_bore_keeps_its_shoulder_and_width(self):
        original = roads.NorwegianTunnelSpec(*spec(5.5)).with_widths([0., 1.], [2.5, 2.5])
        restored = roads.spec_from_json(json.loads(json.dumps(roads._spec_json(original))))
        self.assertIsInstance(restored, roads.NorwegianTunnelSpec)
        self.assertEqual(restored.width, 2.5)
        self.assertEqual(restored.tunnel_shoulder, 0.4)

    def test_unreliable_far_approach_does_not_cancel_valid_different_road_match(self):
        entry, exit_ = fit([("a", spec(8.), line(0., 140.)), ("b", spec(8.), line(280., 140.))],
                           [("road west", 6., line(-30., 0.)), ("unreliable", 16., line(280., 310.))])
        np.testing.assert_allclose(entry.width_at([0., .2, 1.]), [6., 6., 8.])
        np.testing.assert_allclose(exit_.width_at([0., .5, 1.]), [8., 8., 8.])

    def test_one_known_end_does_not_resize_the_other_end(self):
        fitted, = fit([("bore", spec(8.), line(0., 240.))], [("road", 10., line(-30., 0.))])
        np.testing.assert_allclose(fitted.width_at([0., .5, 1.]), [10., 8., 8.])

    def test_nominal_bore_width_does_not_override_a_connected_narrow_road(self):
        fitted, = fit([("bore", spec(8.), line(0., 240.))], [("bore", 4., line(-30., 0.))])
        np.testing.assert_allclose(fitted.width_at([0., .5, 1.]), [4., 8., 8.])

    def test_both_ends_fit_on_bores_with_different_lane_counts(self):
        entry, exit_ = fit([("a", spec(8.), line(0., 100.)), ("b", spec(11., lanes=3), line(250., 100.))],
                           [("west", 6.5, line(-30., 0.)), ("east", 12., line(250., 280.))])
        np.testing.assert_allclose(entry.width_at([0., 1.]), [6.5, 8.])
        np.testing.assert_allclose(exit_.width_at([0., 1.]), [12., 11.])

    def test_missing_lane_count_keeps_the_profile_continuous_between_parts(self):
        entry, exit_ = fit([("a", spec(8.), line(0., 100.)), ("b", spec(8., lanes=None), line(250., 100.))],
                           [("west", 6.5, line(-30., 0.)), ("east", 10., line(250., 280.))])
        np.testing.assert_allclose(entry.width_at([0., 1.]), [6.5, 6.5])
        np.testing.assert_allclose(exit_.width_at([0., 1.]), [10., 6.5])


class PortalTileEdges(unittest.TestCase):
    def test_only_the_chunk_with_the_actual_end_has_a_portal(self):
        class Bore:
            def parts(self, cells, box):
                xy = line(0., 2., 80.)
                return [("bore", spec(8., oneway=True), xy, np.zeros(len(xy)), 0.)]

            def portal_ups(self, fid):
                return 5., 6.

            def cap_m(self, fid):
                return 1., 2.

        def exported(start, end):
            return [c for c in roads._tunnel_chunks(Bore(), [], (start, -10., end, 10.)) if c.get("tunnel")]

        opening, = exported(-1., .35)
        neighboring, = exported(.35, 81.)
        self.assertEqual(opening["portal"], [True, False])
        self.assertEqual(opening["portal_up"], [5., None])
        self.assertEqual(opening["cap_m"], [1., None])
        self.assertTrue(opening["oneway"])
        self.assertEqual(neighboring["portal"], [False, True])
        self.assertEqual(neighboring["portal_up"], [None, 6.])
        self.assertEqual(neighboring["cap_m"], [None, 2.])
        self.assertEqual(exported(-1., 0.), [])


class OpenTunnelWidths(unittest.TestCase):
    def setUp(self):
        self.road = roads.NorwegianTunnelSpec(*spec(8.)).with_widths([0., .25, .75, 1.], [12., 12., 8., 8.])
        self.xy = line(*np.arange(0., 61., 3.))
        self.z = np.zeros(len(self.xy))

    def test_draped_ribbon_and_edge_markings_keep_width_at_clip(self):
        xy, z, road = self.xy, self.z, self.road

        class Bore:
            def open_parts(self, cells, box):
                return [("open", road, xy, z, 8.)]

            def parts(self, cells, box):
                return []

        chunks = roads._tunnel_chunks(Bore(), [], (-1., -20., 61., 20.))
        paved = [c for c in chunks if c["m"] == roads.look_of(road)["m"]]
        extended = [roadmesh._reach_past(c, (1., -10., 59., 10.)) for c in paved]
        widths = []
        for left, right, _, _, _, heights in roadmesh.ribbons(extended):
            self.assertIsNone(heights)
            widths.extend(np.linalg.norm(left - right, axis=1))
        self.assertAlmostEqual(widths[0], 12.)
        self.assertAlmostEqual(widths[-1], 8.)
        for station, expected in [(0., 6.), (60., 4.)]:
            outer_offsets = [abs(y) for c in chunks if c["m"] == "line_white"
                             for x, y, _ in c["n"] if x == station]
            self.assertAlmostEqual(max(outer_offsets), expected - roads.EDGE_LINE["inset"], places=3)

    def test_grade_widens_at_the_wide_open_section_and_mouth(self):
        open_plan = grading._open_tunnel_plan(self.road, self.xy, self.z)
        self.assertTrue(np.allclose(open_plan.widen[[0, -1]], [2., 0.]))
        plans = grading._Planner._mouth_plans([("test", self.road, self.xy, self.z, 2)])
        self.assertEqual(len(plans), 2)
        np.testing.assert_allclose(plans[0][1].widen, 2.)
        np.testing.assert_allclose(plans[1][1].widen, 0.)

    def test_terrain_paint_uses_the_cached_open_section_widths(self):
        frame = geodesy.EnuFrame(62., 8.)
        lat, lon, _ = frame.enu_to_geodetic(*self.xy.T, 0.)
        e, n = np.meshgrid(np.arange(-10., 72.), np.arange(-10., 11.))
        grid_lat, grid_lon, _ = frame.enu_to_geodetic(e, n, 0.)
        restored = roads.spec_from_json(json.loads(json.dumps(roads._spec_json(self.road))))
        base = np.full(e.shape, 255, dtype=np.uint8)
        painted = surface.paint_roads(base, grid_lat, grid_lon, [], 1., extra=[(lat, lon, restored)])
        is_paved = painted == surface.ROAD_MATERIAL["paved"]
        self.assertGreater(int(is_paved[:, 19].sum()), int(is_paved[:, 63].sum()) + 2)
