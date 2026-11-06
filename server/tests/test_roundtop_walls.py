"""Roundtop Road regression using captured source geometry and production DEM/grade kernels.

This replay uses the actual built-world road plans, genuine ground-only road
parts and production terrain sampler. It does not claim a fresh independent
rebuild of the upstream regional planning or a complete in-game world replay.
"""
import unittest

import numpy as np

from roundtop_fixture import CapturedRoundtop, CapturedRoundtopPlans, ROUND_TOP
import grading
import mapgeom
import roadmesh
import roads


def single_feature(source, index):
    """Copy one genuine LineString and its genuine metadata; no fabricated OSM record."""
    part = source.parts(index)
    assert len(part) == 1 and len(part[0]) == 1
    line = part[0][0].copy()
    tables = {}
    for key, table in source.rules.items():
        keep = np.flatnonzero(np.asarray(table.feature) == index)
        tables[key] = mapgeom.RuleTable(np.zeros(len(keep), np.int32), table.value[keep].copy(),
                                       table.start[keep].copy(), table.end[keep].copy())
    return mapgeom.Features("roads", source.ids[index:index + 1].copy(), source.kind[index:index + 1].copy(),
                            np.array([0, 1]), np.array([0, 1]), np.array([0, len(line)]), line,
                            attrs={key: value[index:index + 1].copy() for key, value in source.attrs.items()},
                            rules=tables)


class RoundtopWallTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.capture = CapturedRoundtop()
        cls.frame = cls.capture.frame
        source = cls.capture.roads()
        cls.source = single_feature(source, source.index(ROUND_TOP))
        cls.lonlat = cls.source.lonlat.copy()
        cls.spec = roads.road_specs(cls.source)[0]
        cls.parts = roads.ground_parts(cls.source, 0)
        cls.context = CapturedRoundtopPlans(cls.capture)

    @classmethod
    def tearDownClass(cls):
        cls.capture.close()

    @classmethod
    def final(cls, latitude, longitude):
        return cls.context.final(latitude, longitude)

    @classmethod
    def road_lines(cls, final=None):
        final = cls.final if final is None else final
        out = []
        for ll in cls.parts:
            x, y, _ = cls.frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.)
            z = cls.frame.geodetic_to_enu(ll[:, 1], ll[:, 0], final(ll[:, 1], ll[:, 0]))[2]
            out.append((ROUND_TOP, cls.spec, np.c_[x, y, z], float(ll[:, 0].mean())))
        return out

    def walls(self, box=(-300., -300., 300., 300.), final=None):
        return roads.retaining_walls(self.frame, self.road_lines(final), final or self.final, box)

    def test_actual_source_and_sparse_chord_reproduce_the_bad_elevation_comparison(self):
        self.assertEqual(str(self.source.attrs["src_record"][0]), "w15739626@11")
        self.assertEqual(self.spec.cls, "tertiary")
        self.assertEqual(len(self.lonlat), 16)
        errors = []
        for _, _, points, _ in self.road_lines():
            xy, station = grading.resample(points[:, :2], roads.WALL_STEP)
            old_z = np.interp(station, np.r_[0., np.cumsum(np.linalg.norm(np.diff(points[:, :2], axis=0), axis=1))],
                              points[:, 2])
            latitude, longitude, _ = self.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.)
            actual_z = self.frame.geodetic_to_enu(latitude, longitude, self.final(latitude, longitude))[2]
            errors.append(old_z - actual_z)
        self.assertGreater(float(np.concatenate(errors).max()), roads.PARAPET_DROP_M,
                           "the actual sparse source chord falsely exceeds the parapet-drop threshold")
        self.assertEqual(self.walls(), [])

    def test_roundtop_has_no_false_walls_when_sharp_cut_generation_is_disabled(self):
        # A separate fresh native-kernel replay keeps the true bridge gap. It
        # deliberately does not pretend to rebuild every regional dependency.
        plans = grading.plan_roads(self.frame, [(ll, self.spec) for ll in self.parts], self.capture.raw, cuts=False)
        def final(latitude, longitude):
            e, n, _ = self.frame.geodetic_to_enu(latitude, longitude, 0.)
            return grading.grade_points(np.asarray(e), np.asarray(n), self.capture.raw(latitude, longitude),
                                        plans, grading.ROAD_LIFT)
        self.assertEqual(self.walls(final=final), [], "the fix concerns sampled support heights, not disabling cuts")

    def test_actual_tile_generation_drops_the_artificial_walls_without_changing_geometry(self):
        before = self.source.lonlat.copy()
        for tx in (-1, 0):
            for ty in (-2, -1, 0, 1):
                with self.subTest(tile=(tx, ty)):
                    walls = []
                    roads.build_tile(self.frame, tx, ty, [self.source], self.final,
                                     walls=walls, walks=[], props=[])
                    self.assertEqual(walls, [])
        np.testing.assert_array_equal(self.source.lonlat, before)

    def test_adjacent_tiles_repeated_reload_and_reverse_visit_have_the_same_result(self):
        boxes = [(-256., -256., 0., 0.), (-256., 0., 0., 256.), (0., -256., 256., 0.), (0., 0., 256., 256.)]
        forward = [self.walls(box) for box in boxes]
        backward = [self.walls(box) for box in reversed(boxes)]
        self.assertEqual(forward, list(reversed(backward)))
        self.assertEqual(forward, [[], [], [], []])
        fresh_context = CapturedRoundtopPlans(self.capture)
        np.testing.assert_array_equal(self.final(self.lonlat[:, 1], self.lonlat[:, 0]),
                                      fresh_context.final(self.lonlat[:, 1], self.lonlat[:, 0]))
        self.assertEqual(self.walls(final=fresh_context.final), [])

    def test_nearby_raw_and_final_cross_sections_have_no_real_two_metre_drop(self):
        # Within the genuine northern ground-road run; the southern dip is a
        # mapped bridge and must not be represented as a ground-road wall case.
        for latitude, longitude in ((41.1603, -104.87997), (41.1606, -104.879973)):
            with self.subTest(latitude=latitude):
                x, y, _ = self.frame.geodetic_to_enu(latitude, longitude, 0.)
                e = np.asarray([x - 5.6, x, x + 5.6])
                n = np.full(3, y)
                la, lo, _ = self.frame.enu_to_geodetic(e, n, 0.)
                raw = self.capture.raw(la, lo)
                final = self.final(la, lo)
                self.assertLess(float(np.ptp(raw)), 1.)
                self.assertLess(float(np.ptp(final)), .03)

    def test_actual_built_world_plan_context_also_has_no_false_walls(self):
        context = CapturedRoundtopPlans(self.capture)
        self.assertEqual(len(self.parts), 2, "mapped bridge range is excluded from ground-road consumers")
        bridge_drawn = False
        def drawn(x, y):
            # Same triangulated support lookup used by the live road pipeline;
            # q is an explicit test precision, not a fabricated DEM height.
            return roadmesh.drawn_ground(self.frame, context.final, np.c_[x, y], q=1 / 256)
        for tx in (-1, 0):
            for ty in (-2, -1, 0, 1):
                walls = []
                chunks = roads.build_tile(self.frame, tx, ty, [self.source], context.final,
                                          walls=walls, walks=[], props=[], ground=drawn)
                self.assertEqual(walls, [])
                bridge_drawn |= any(chunk.get("deck") for chunk in chunks)
        self.assertTrue(bridge_drawn, "the actual mapped bridge is still generated")
        self.assertEqual(len(context.keys), 4)
        self.assertTrue(any(context.planner.opens.values()), "actual neighboring infrastructure context is retained")
        # Source consumers must not mutate the cached plans or change the final terrain.
        sample_lat = np.array([41.1603, 41.1606])
        sample_lon = np.array([-104.87997, -104.879973])
        before = context.final(sample_lat, sample_lon)
        self.walls(final=context.final)
        np.testing.assert_array_equal(context.final(sample_lat, sample_lon), before)


if __name__ == "__main__":
    unittest.main()
