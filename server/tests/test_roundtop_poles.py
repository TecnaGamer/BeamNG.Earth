"""Pole planting-datum replay using genuine Roundtop Road and captured DEM.

The replay uses the world's captured built plans, including its neighboring
road, junction and bridge context. Only genuine ground_parts emit these poles;
the off-ground bridge interval is excluded. This establishes an offline placement
error, not proof of visible floating poles in the screenshot or a fresh world build.
"""
import unittest

import numpy as np

from roundtop_fixture import CapturedRoundtop, CapturedRoundtopPlans, ROUND_TOP
from test_roundtop_walls import single_feature
import furniture
import fast
import roadmesh
import roads


class RoundtopPoleTest(unittest.TestCase):
    BOX = (-300., -300., 300., 300.)
    QUANTUM = 1. / 64.
    TOLERANCE_M = .02

    @classmethod
    def setUpClass(cls):
        cls.capture = CapturedRoundtop()
        cls.frame = cls.capture.frame
        all_roads = cls.capture.roads()
        cls.source = single_feature(all_roads, all_roads.index(ROUND_TOP))
        cls.spec = roads.road_specs(cls.source)[0]
        cls.context = CapturedRoundtopPlans(cls.capture)
        cls.roads = []
        for ll in roads.ground_parts(cls.source, 0):
            x, y, _ = cls.frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.)
            z = cls.frame.geodetic_to_enu(ll[:, 1], ll[:, 0], cls.final(ll[:, 1], ll[:, 0]))[2]
            cls.roads.append((ROUND_TOP, cls.spec, np.c_[x, y, z], float(ll[:, 0].mean())))

    @classmethod
    def tearDownClass(cls):
        cls.capture.close()

    @classmethod
    def final(cls, latitude, longitude):
        return cls.context.final(latitude, longitude)

    @classmethod
    def ground(cls, x, y):
        return roadmesh.drawn_ground(cls.frame, cls.final, np.c_[x, y], cls.QUANTUM)

    @classmethod
    def replay(cls, grounded):
        wires = []
        rows = furniture.place(cls.roads, [], cls.BOX, wires=wires,
                               ground=cls.ground if grounded else None)
        return [r for r in rows if r[0] == furniture.G_POLE], wires

    @classmethod
    def measured(cls):
        old, _ = cls.replay(False)
        new, _ = cls.replay(True)
        xy = np.array([row[1:3] for row in new])
        ground_z = cls.ground(xy[:, 0], xy[:, 1])
        lat, lon, _ = cls.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.)
        return [{"latitude": float(la), "longitude": float(lo), "xy": row[1:3],
                 "legacy_base_up_m": before[3], "corrected_base_up_m": row[3], "final_ground_up_m": float(zz),
                 "legacy_base_minus_ground_m": float(before[3] - zz),
                 "corrected_base_minus_ground_m": float(row[3] - zz)}
                for before, row, zz, la, lo in zip(old, new, ground_z, lat, lon)]

    def test_actual_sparse_profile_error_is_corrected_without_moving_poles(self):
        before, _ = self.replay(False)
        after, _ = self.replay(True)
        self.assertEqual(str(self.source.attrs["src_record"][0]), "w15739626@11")
        self.assertEqual([[*r[:3], *r[4:]] for r in before], [[*r[:3], *r[4:]] for r in after])
        measurements = self.measured()
        self.assertEqual(len(self.roads), 2, "the source bridge interval separates two ground runs")
        self.assertTrue(measurements)
        full_ll = self.source.lonlat
        x, y, _ = self.frame.geodetic_to_enu(full_ll[:, 1], full_ll[:, 0], 0.)
        full_xy = np.c_[x, y]
        distance = furniture._arclen(full_xy)[-1]
        fractions = furniture._project(full_xy, np.array([row[1:3] for row in after])) / distance
        for start, end, kind in roads.level_ranges(self.source, 0):
            if kind == "bridge":
                self.assertFalse(((fractions > start + .001) & (fractions < end - .001)).any(),
                                 "roadside poles must not be invented inside the source bridge interval")
        self.assertGreater(max(row["legacy_base_minus_ground_m"] for row in measurements), 1.)
        self.assertLessEqual(max(abs(row["corrected_base_minus_ground_m"]) for row in measurements),
                             self.TOLERANCE_M)

    def test_generated_tile_bases_use_final_quantized_support(self):
        rows = []
        for tx in (-2, -1, 0, 1):
            for ty in (-2, -1, 0, 1):
                roads.build_tile(self.frame, tx, ty, [self.source], self.final,
                                 walks=[], props=rows, ground=self.ground)
        nearby = [row for row in rows if row[0] == furniture.G_POLE and np.hypot(row[1], row[2]) <= 300.]
        self.assertTrue(nearby)
        positions = [tuple(row[1:3]) for row in nearby]
        self.assertEqual(len(positions), len(set(positions)), "source/tile boundaries must not duplicate poles")
        xy = np.array(positions)
        support = self.ground(xy[:, 0], xy[:, 1])
        self.assertLessEqual(float(np.max(np.abs(np.array([row[3] for row in nearby]) - support))), self.TOLERANCE_M)

    def test_captured_neighbor_carriageway_clearance_preserves_the_measured_poles(self):
        # This is a 2-D query only: no fabricated terrain/elevation is assigned to neighboring roads.
        lines, seen = [], set()
        for x in (3417, 3418, 3419):
            for y in (6132, 6133, 6134):
                source = self.capture.roads(x, y)
                for index, spec in enumerate(roads.road_specs(source)):
                    fid = str(source.ids[index])
                    if spec is None or fid in seen:
                        continue
                    seen.add(fid)
                    for ll in roads.ground_parts(source, index):
                        e, n, _ = self.frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.)
                        lines.append((fid, spec.width / 2, np.c_[e, n]))
        other_carriageways = fast.Others(lines)
        measured, _ = self.replay(False)
        self.assertGreater(len(lines), 100)
        self.assertTrue(all(furniture._clear(np.asarray(row[1:3]), other_carriageways, ROUND_TOP)
                            for row in measured), "a nearby road must not invalidate the measured station candidates")

    def test_actual_conductors_share_grounded_pole_bases_and_reload_is_identical(self):
        first_rows, first_wires = self.replay(True)
        again_rows, again_wires = self.replay(True)
        self.assertEqual(first_rows, again_rows)
        self.assertEqual(len(first_wires), len(again_wires))
        for before, after in zip(first_wires, again_wires):
            np.testing.assert_array_equal(before, after)
        for start in range(0, len(first_wires), len(furniture.WIRE_ARMS)):
            span = first_wires[start:start + len(furniture.WIRE_ARMS)]
            for endpoint in (0, -1):
                pole_xy = np.round(np.mean([line[endpoint, :2] for line in span], axis=0), 2)
                expected = round(float(self.ground(pole_xy[0:1], pole_xy[1:2])[0]), 2) + furniture.WIRE_Z
                self.assertTrue(all(abs(line[endpoint, 2] - expected) < 1e-8 for line in span))


if __name__ == "__main__":
    unittest.main()
