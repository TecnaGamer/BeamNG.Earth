"""Roadside bases follow the final surface at their offset XY, before attached geometry.

The road and analytic terrain below are deliberately synthetic; actual Roundtop
road/DEM regressions are separate. Serialized bases are rounded to centimetres,
so the accepted base-datum error is 0.02 m, without a universal model offset.
"""
import sys
from pathlib import Path
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import furniture
import roads


TOLERANCE_M = 0.02
BOX = (0., 0., 256., 256.)


def synthetic_road(end=900., urban=False):
    return ("synthetic-grounding-road", roads.Spec(8., "paved", urban, "tertiary"),
            np.array([[-300., 50., 10.], [end, 50., 10.]]), 0.)


def poles(rows):
    return [r for r in rows if r[0] == furniture.G_POLE]


def sorted_rows(rows):
    return sorted(rows, key=lambda r: (r[0], *r[1:]))


class PoleGroundingTest(unittest.TestCase):
    def placed(self, ground=None, box=BOX, **kwargs):
        wire = []
        rows = furniture.place([synthetic_road()], [], box, wires=wire, ground=ground, **kwargs)
        return rows, wire

    def assert_grounded(self, rows, ground):
        got = poles(rows)
        self.assertTrue(got)
        for row in got:
            expected = float(np.asarray(ground(np.array([row[1]]), np.array([row[2]])))[0])
            self.assertLessEqual(abs(row[3] - expected), TOLERANCE_M, row)

    def test_actual_lateral_positions_on_both_slope_directions(self):
        old, _ = self.placed()
        for slope in (-.3, .3):
            with self.subTest(slope=slope):
                surface = lambda x, y: 10. + slope * (np.asarray(y) - 50.)
                original_error = [r[3] - float(surface(r[1], r[2])) for r in poles(old)]
                self.assertTrue(all(abs(abs(error) - 1.92) < .001 for error in original_error))
                corrected, _ = self.placed(surface)
                self.assert_grounded(corrected, surface)

    def test_rolling_profile_does_not_use_sparse_endpoint_chord(self):
        surface = lambda x, y: 10. - 2. * np.sin(np.pi * np.asarray(x) / 900.)
        old, _ = self.placed()
        self.assertGreater(max(r[3] - float(surface(r[1], r[2])) for r in poles(old)), 1.)
        corrected, _ = self.placed(surface)
        self.assert_grounded(corrected, surface)

    def test_xy_spacing_yaw_scale_and_road_geometry_are_preserved(self):
        road = synthetic_road()
        original = road[2].copy()
        old = furniture.place([road], [], BOX)
        new = furniture.place([road], [], BOX, ground=lambda x, y: 7. + .1 * x - .2 * y)
        self.assertEqual([[*r[:3], *r[4:]] for r in old], [[*r[:3], *r[4:]] for r in new])
        np.testing.assert_array_equal(road[2], original)

    def test_batched_queries_cover_only_owned_bases_and_next_wire_anchors(self):
        calls = []
        def surface(x, y):
            calls.append(np.column_stack([x, y]))
            return 9. + .1 * x
        road = synthetic_road(end=12000.)
        wires = []
        rows = furniture.place([road], [], BOX, ground=surface, wires=wires)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0]), len(poles(rows)) + 1)
        self.assertTrue(np.all(calls[0][:, 0] >= BOX[0]))
        self.assertLess(calls[0][:, 0].max(), BOX[2] + furniture.POLE_SPACING + .01)
        self.assertLess(len(calls[0]), 10, "do not grade the full 12 km source way for one tile")

    def test_wire_endpoints_and_sag_follow_corrected_bases(self):
        surface = lambda x, y: 10. - .03 * np.asarray(x) + .2 * np.asarray(y)
        _, wires = self.placed(surface)
        self.assertTrue(wires)
        self.assertEqual(len(wires) % len(furniture.WIRE_ARMS), 0)
        for start in range(0, len(wires), len(furniture.WIRE_ARMS)):
            span = wires[start:start + len(furniture.WIRE_ARMS)]
            for endpoint in (0, -1):
                centre = np.mean([line[endpoint, :2] for line in span], axis=0)
                query = np.round(centre, 2)
                # Current furniture shares the serialized centimetre-rounded
                # pole anchor with the conductor, so both attach to one datum.
                expected = round(float(surface(query[0], query[1])), 2) + furniture.WIRE_Z
                self.assertTrue(all(abs(line[endpoint, 2] - expected) < 1e-8 for line in span))
            line = span[0]
            t = np.linspace(0., 1., len(line))
            chord = line[0, 2] + (line[-1, 2] - line[0, 2]) * t
            length = np.linalg.norm(line[-1, :2] - line[0, :2])
            np.testing.assert_allclose(chord - line[:, 2], 4. * furniture.WIRE_SAG * length * t * (1. - t))

    def test_pole_arms_and_lenses_keep_their_relative_heights(self):
        surface = lambda x, y: 6. + .02 * np.asarray(x) + .03 * np.asarray(y)
        rows, _ = self.placed(surface, lighting={"synthetic-grounding-road": "yes"}, region="na:sparse")
        bases = {tuple(r[1:3]): r[3] for r in poles(rows)}
        arms = [r for r in rows if r[0] == furniture.G_POLELAMP]
        self.assertTrue(arms)
        for row in arms:
            self.assertLess(abs(row[3] - bases[tuple(row[1:3])] - furniture.POLE_LAMP_Z), TOLERANCE_M)
        actual_lenses = [r for r in rows if r[0] in (furniture.G_LENS, furniture.G_GLOW)]
        self.assertEqual(actual_lenses, furniture.lamp_lenses(arms))

    def test_standalone_roadside_lamps_use_the_same_final_support(self):
        surface = lambda x, y: 7. + .03 * np.asarray(x) - .15 * np.asarray(y)
        rows, _ = self.placed(surface, buried=1., lighting={"synthetic-grounding-road": "yes"})
        lamps = [r for r in rows if r[0] == furniture.G_LAMP]
        self.assertTrue(lamps)
        for row in lamps:
            self.assertLess(abs(row[3] - float(surface(row[1], row[2]))), TOLERANCE_M)

    def test_neighbor_tiles_and_repeated_generation_match_whole_area(self):
        surface = lambda x, y: 10. + .04 * np.asarray(x) - .1 * np.asarray(y)
        a, aw = self.placed(surface)
        b, bw = self.placed(surface, (256., 0., 512., 256.))
        whole, ww = self.placed(surface, (0., 0., 512., 256.))
        again, rw = self.placed(surface)
        self.assertEqual(sorted_rows(a + b), sorted_rows(whole))
        self.assertEqual(a, again)
        for left, right in zip(aw + bw, ww):
            np.testing.assert_array_equal(left, right)
        self.assertEqual(len(aw + bw), len(ww))
        for left, right in zip(aw, rw):
            np.testing.assert_array_equal(left, right)

    def test_under_bridge_filter_uses_grounded_base_and_conductors(self):
        old, _ = self.placed()
        y = poles(old)[0][2]
        deck = [(np.array([[-300., y, 23.], [900., y, 23.]]), 2.)]
        # Base10/top19.2 clears the current deck-underside envelope. Correct
        # support15/top24.2 intersects it and must suppress poles and wires.
        before, _ = self.placed(decks=deck)
        self.assertTrue(poles(before))
        after, wires = self.placed(lambda x, y: np.full(np.shape(x), 15.), decks=deck)
        self.assertFalse(poles(after))
        self.assertFalse(wires)

    def test_sidewalk_poles_keep_draped_slab_support(self):
        road = synthetic_road(urban=True)
        walks = [{"fid": road[0], "pts": roads.offset_polyline(road[2], side * 5.25),
                  "side": side, "w": 2.5, "caps": (False, False)} for side in (1, -1)]
        ground = mock.Mock(side_effect=AssertionError("draped sidewalk support must not snap to bare terrain"))
        rows = furniture.place([road], walks, BOX, town=True, ground=ground)
        self.assertTrue(poles(rows))
        self.assertTrue(all(row[3] == 10. for row in poles(rows)))
        ground.assert_not_called()

    def test_trace_reports_actual_base_ground_offset(self):
        surface = lambda x, y: 6.123 + .02 * np.asarray(x)
        trace = {"synthetic-grounding-road": []}
        self.placed(surface, trace=trace)
        objects = [obj for obj in trace["synthetic-grounding-road"]["objects"] if obj["group"] == furniture.G_POLE]
        self.assertTrue(objects)
        for obj in objects:
            self.assertEqual(obj["support"], "final_drawn_ground_at_object_xy")
            self.assertLessEqual(abs(obj["base_minus_ground_m"]), TOLERANCE_M)
            self.assertAlmostEqual(obj["ground_z"], float(surface(obj["position"][0], obj["position"][1])))

    def test_invalid_final_support_does_not_serialize_non_finite_bases(self):
        with self.assertRaisesRegex(ValueError, "non-finite"):
            self.placed(lambda x, y: np.full(np.shape(x), np.nan))


if __name__ == "__main__":
    unittest.main()
