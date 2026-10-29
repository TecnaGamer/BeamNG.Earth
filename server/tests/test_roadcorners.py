"""Rounded corners where curbless paved roads meet (roadcorners.py; ported from PR #363's test_pavement_corners.py by
cody196): the fills, the gravel edge decal and edge lines along their arcs, the straight lines cut at the tangents, the
ground paint (material, photo, asphalt mask), the grading under them and no trees on them; curb returns unchanged."""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from test_roads import FRAME, features, flat, rules  # noqa: E402
import grading  # noqa: E402
import roadcorners  # noqa: E402
import roads  # noqa: E402
import surface  # noqa: E402
import vegetation  # noqa: E402

DECAL = roads.EDGE_DECAL["m"]


def crossing(shift=(0.0, 0.0), cls="secondary"):
    lines = [np.array([[-100.0, 0.0], [0.0, 0.0], [100.0, 0.0]]) + shift,
             np.array([[0.0, -100.0], [0.0, 0.0], [0.0, 100.0]]) + shift]
    return features(lines, [cls] * 2)


def tee(cls="tertiary"):
    return features([[(-100, 0), (0, 0), (100, 0)], [(0, 0), (0, 100)]], [cls] * 2)


def curved(chunks, m="line_white"):
    return [c for c in chunks if c["m"] == m and len(c["n"]) > 10]


class PavementCornerTest(unittest.TestCase):
    def test_oneway_highway_corner_keeps_yellow_left_edges(self):
        sp = roads.Spec(14.0, "paved", False, "motorway", True, dir=1, lanes=2)
        rows = [("in", sp, np.array([[-100.0, 0.0, 10.0], [0.0, 0.0, 10.0]]), -85.0),
                ("out", sp, np.array([[0.0, 0.0, 10.0], [0.0, 100.0, 10.0]]), -85.0)]
        chunks, cuts = roadcorners.tile(rows, (-100.0, -100.0, 100.0, 100.0), False, country="US")
        lines = [c for c in chunks if c["m"] != DECAL]
        self.assertTrue(lines)
        self.assertTrue(cuts)
        self.assertTrue(all(c["m"] == "line_yellow" for c in lines))

    def test_four_rounded_corners_without_sidewalks_or_crosswalks(self):
        f = crossing((100.0, 100.0))
        frame, rows, corners = roadcorners.source([f], frame=FRAME)
        self.assertEqual(len(corners), 4)
        self.assertTrue(all(len(c["arc"]) > 15 for c in corners))
        self.assertFalse(any(roads.has_sidewalks(sp) for _, sp, *_ in rows))
        for in_ground in (False, True):
            walks = []
            with patch.object(roads, "ROADS_IN_GROUND", in_ground):
                chunks = roads.build_tile(FRAME, 0, 0, [f], flat, walks=walks)
            self.assertFalse(walks)
            self.assertFalse(any(c["m"] == "crossing_white" for c in chunks))
            self.assertEqual(sum(bool(c.get("poly")) for c in chunks), 0 if in_ground else 4)
            self.assertEqual(len(curved(chunks)), 4)            # edge lines along the four arcs (US: secondary)
            self.assertEqual(len(curved(chunks, DECAL)), 4)     # and the gravel edge along them

    def test_material_photo_and_asphalt_mask_fill_the_corner(self):
        f = crossing()      # secondaries 8 m wide: each corner's arc of 12 m round (16, 16), the old corner (4, 4)
        e, n = np.meshgrid(np.arange(-32.0, 32.01, 0.25), np.arange(-32.0, 32.01, 0.25))
        la, lo, _ = FRAME.enu_to_geodetic(e, n, 0.0)
        mat = surface.paint_roads(np.full(e.shape, surface.GRASS, np.uint8), la, lo, [f], 0.25)
        self.assertEqual(mat[150, 150], surface.ASPHALT, "(5.5, 5.5) is between the old square corner and the arc")
        self.assertNotEqual(mat[168, 168], surface.ASPHALT, "(10, 10) stays roadside")
        self.assertEqual(mat[164, 164], surface.VERGE, "(9, 9): a bare verge round the corner, as along the roads")
        self.assertEqual(mat[168, 168], surface.GRASS)
        image = np.full((257, 257, 3), 230, np.uint8)
        photo = surface.photo_roads(image, FRAME, -32.0, -32.0, 64.0, [f])
        self.assertTrue(np.any(photo[106, 150] != image[106, 150]))
        np.testing.assert_array_equal(photo[88, 169], image[88, 169])
        fake = SimpleNamespace(urban=None, roads=lambda *_: ([f], None))
        mask = surface.road_mask(FRAME, -32.0, -32.0, 64.0, 257, fake)
        self.assertGreater(mask[106, 150], 240)
        self.assertEqual(mask[88, 169], 0)

    def test_corner_grading_holds_the_new_pavement_on_a_hillside(self):
        # a road with a narrow flat shoulder (grading.shoulder: a motorway's ramp terminal, an expressway) meets a
        # secondary: its 12 m corners' fans reach past the roads' graded ground
        # Keep this grading fixture's original 13 m carriageway explicit: lane defaults must not change the
        # geometry whose corner blending is measured here.
        f = features([[(-100, 0), (0, 0), (100, 0)], [(0, -100), (0, 0), (0, 100)]],
                     ["motorway", "secondary"], widths=[(0, 13.0)])
        _, rows, corners = roadcorners.source([f], frame=FRAME)
        rows = [(fid, sp, np.c_[p[:, :2], np.full(len(p), 10.0)], lon) for fid, sp, p, lon in rows]
        plans = [(fid, grading.Plan(p[:, :2], p[:, 2], sp.width / 2 + grading.shoulder(sp), road_half=sp.width / 2))
                 for fid, sp, p, _ in rows]
        extra = roadcorners.grade(rows)
        self.assertEqual(len({f.rsplit(":", 1)[0] for f, _ in extra}), 4)      # (lines along each corner)
        # in each fan: between its old square corner and its arc's middle
        q = np.array([(np.asarray(c["fill"])[0, :2] + np.asarray(c["arc"])[len(c["arc"]) // 2, :2]) / 2 for c in corners])
        base = np.full(len(q), 16.0)
        z = grading.grade_points(q[:, 0], q[:, 1], base, [p for _, p in plans + extra])
        np.testing.assert_allclose(z, 10.0, atol=0.05)
        # on a slope the roads keep their heights (the corners' lines stay off the carriageways: only grade_points'
        # blending over its transitions moves a centreline, by centimetres at the junction, as the roads move each other)
        rows = [(fid, sp, np.c_[p[:, :2], 10.0 + 0.06 * p[:, 0] + 0.03 * p[:, 1]], lon) for fid, sp, p, lon in rows]
        plans = [(fid, grading.Plan(p[:, :2], p[:, 2], sp.width / 2 + grading.shoulder(sp), road_half=sp.width / 2))
                 for fid, sp, p, _ in rows]
        extra = roadcorners.grade(rows)
        self.assertTrue(extra)
        t = np.linspace(-30.0, 30.0, 61)
        cx, cy = np.r_[t, np.zeros(61)], np.r_[np.zeros(61), t]
        base = np.full(len(cx), 16.0)
        np.testing.assert_allclose(grading.grade_points(cx, cy, base, [p for _, p in plans + extra]),
                                   grading.grade_points(cx, cy, base, [p for _, p in plans]), atol=0.07)
        # two streets (a sidewalk-wide flat shoulder each, in the country too): their corners are on it already
        _, rows, _ = roadcorners.source([crossing()], frame=FRAME)
        self.assertEqual(roadcorners.grade([(fid, sp, np.c_[p[:, :2], np.full(len(p), 10.0)], lon)
                                            for fid, sp, p, lon in rows]), [])

    def test_grade_rows_take_the_plans_heights(self):
        f = features([[(-100, 0), (0, 0), (100, 0)], [(0, -100), (0, 0), (0, 100)]], ["motorway", "secondary"])
        _, rows, _ = roadcorners.source([f], frame=FRAME)
        parts, plans = [], []
        for i in range(len(f)):
            for ring in roads.ground_parts(f, i):
                parts.append((str(f.ids[i]), roads.road_specs(f)[i], ring))
                e, n, _ = FRAME.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
                xy = np.c_[e, n]
                plans.append(grading.Plan(xy, 5.0 + 0.01 * xy[:, 0], 4.0))
        got, heights = roadcorners.grade_rows(parts, plans, FRAME)
        self.assertEqual([r[0] for r in got], [r[0] for r in rows])
        self.assertEqual(sorted(heights), sorted(r[0] for r in rows))
        extra = roadcorners.grade(got, heights=heights)
        self.assertEqual(len({f.rsplit(":", 1)[0] for f, _ in extra}), 4)
        for _f, p in extra:     # (the plans' heights by the tangents: 5 m + 1 %, not the rows' 0)
            self.assertTrue(np.all(np.abs(p.z - 5.0) < 0.35))

    def test_edge_curves_join_the_straight_edge_lines_at_both_tangents(self):
        _, rows, corners = roadcorners.source([crossing()], frame=FRAME)
        chunks, cuts = roadcorners.tile(rows, (-50.0, -50.0, 50.0, 50.0), False)
        lines = [c for c in chunks if c["m"] != DECAL]
        self.assertEqual(len(lines), 4)
        self.assertEqual(len(cuts), 4)
        off = 4.0 - roads.EDGE_LINE["inset"]
        for c in lines:
            p = np.array(c["n"])
            for at in (0, -1):
                self.assertAlmostEqual(min(abs(p[at, 0]), abs(p[at, 1])), off, delta=0.03)
            self.assertGreater(np.linalg.norm(np.diff(p[:, :2], axis=0), axis=1).min(), 0.1)

    def test_t_junction_and_grade_separated_crossing(self):
        f = features([[(-100, 0), (0, 0), (100, 0)], [(0, 0), (0, 100)]], ["secondary"] * 2)
        self.assertEqual(len(roadcorners.source([f], frame=FRAME)[2]), 2)
        f = crossing()
        f.rules["flags"] = rules([(1, "is_bridge")])
        self.assertEqual(roadcorners.source([f], frame=FRAME)[2], [], "no corners under an overpass")

    def test_tile_boundaries_keep_the_same_pavement(self):
        f = crossing()
        left = surface.photo_roads(np.full((128, 128, 3), 230, np.uint8), FRAME, -32.0, -16.0, 32.0, [f])
        right = surface.photo_roads(np.full((128, 128, 3), 230, np.uint8), FRAME, 0.0, -16.0, 32.0, [f])
        full = surface.photo_roads(np.full((256, 256, 3), 230, np.uint8), FRAME, -32.0, -32.0, 64.0, [f])
        np.testing.assert_array_equal(left[40:88, 88:116], full[104:152, 88:116])
        np.testing.assert_array_equal(right[40:88, 12:40], full[104:152, 140:168])

    def test_no_trees_on_the_new_corner(self):
        f = crossing()
        lon, lat = FRAME.lon0, FRAME.lat0
        mx, my = vegetation._to3857(lon, lat)
        scale = 1 / np.cos(np.radians(lat))
        chm = SimpleNamespace(x0=mx - 32 * scale, y0=my + 32 * scale, dx=0.5 * scale, dy=0.5 * scale)
        road = np.zeros((128, 128), bool)
        vegetation._mask_roads(f, road, chm, clear=0.0)
        self.assertFalse(road[53, 75], "(5.5, 5.5) is off both roads' corridors")
        vegetation._mask_corners([f], (road,), chm, (lon - 0.001, lat - 0.001, lon + 0.001, lat + 0.001))
        self.assertTrue(road[53, 75], "... and on the corner's")
        self.assertFalse(road[44, 84], "(10, 10) stays free")


class ApplyCutsTest(unittest.TestCase):
    def test_a_cut_left_behind_its_node_ends_the_line_there(self):
        # (the line leaves the cut where it starts behind the node, along -1 m, not across t: its end was extrapolated
        # 11 m past the line's own end, and a decal ran on 3 km past its tile)
        import junctions
        line = np.c_[np.linspace(-50.0, 50.0, 11), np.full(11, 5.0), np.zeros(11)]
        pieces = junctions.apply_cuts(line, [(np.array([0.0, 0.0]), np.array([1.0, 0.0]), 10.0)])
        ends = sorted(float(p[k, 0]) for p, _ in pieces for k in (0, -1))
        self.assertEqual(len(pieces), 2)
        self.assertAlmostEqual(ends[0], -50.0)
        self.assertAlmostEqual(ends[-1], 50.0)
        self.assertAlmostEqual(ends[1], -1.0, delta=0.5)
        self.assertAlmostEqual(ends[2], 10.0, delta=0.01)


class RuralTeeTest(unittest.TestCase):
    """A rural T of two tertiary roads (no sidewalks): one fill per corner, the gravel edge and (where the country's
    tertiary roads have edge lines) an edge line along each arc; the straight ones end at the tangents."""

    def build(self, country, in_ground=False):
        with patch.object(roads, "ROADS_IN_GROUND", in_ground):
            return roads.build_tile(FRAME, 0, 0, [tee()], flat, walks=[], country=country) + \
                roads.build_tile(FRAME, -1, 0, [tee()], flat, walks=[], country=country)

    def test_one_fill_per_corner_and_lines_along_each_arc(self):
        chunks = self.build("DE")
        self.assertEqual(sum(bool(c.get("poly")) for c in chunks), 2)
        self.assertEqual(len(curved(chunks)), 2)
        self.assertEqual(len(curved(chunks, DECAL)), 2)
        self.assertEqual(sum(bool(c.get("poly")) for c in self.build("DE", True)), 0)     # (painted in the ground)
        # the US paints no edge lines on tertiary roads (markings._US.edge_classes): the gravel edge only
        us = self.build("US")
        self.assertEqual(len(curved(us)), 0)
        self.assertEqual(len(curved(us, DECAL)), 2)

    def test_straight_edges_end_at_the_tangents(self):
        chunks = self.build("DE")
        t = 3.25 + junctions_pave()           # half the side road's width + the radius
        decal = 3.25 + (roads.EDGE_DECAL_AT - 0.5) * roads.EDGE_DECAL["w"]
        line = 3.25 - roads.EDGE_LINE["inset"]
        for m, off in ((DECAL, decal), ("line_white", line)):
            north = [np.array(c["n"]) for c in chunks if c["m"] == m and len(c["n"]) <= 10
                     and all(abs(q[1] - off) < 0.02 for q in c["n"])]
            self.assertTrue(north)
            xs = np.concatenate([p[:, 0] for p in north])
            self.assertFalse(np.any(np.abs(xs) < t - 0.02), m)        # nothing across the side road's mouth
            self.assertTrue(np.any(np.abs(np.abs(xs) - t) < 0.02), m)  # each ends at its tangent
            arcs = [np.array(c["n"]) for c in curved(chunks, m)]
            ends = np.vstack([p[[0, -1], :2] for p in arcs])
            for q in np.vstack([p[[0, -1], :2] for p in north]):
                if abs(abs(q[0]) - t) < 0.02:
                    self.assertLess(np.hypot(*(ends - q).T).min(), 0.02, m)     # the arc starts there

    def test_gravel_edge_runs_on_across_the_far_side_of_a_split_t(self):
        # the map splits the through road at the junction: its far side's gravel edge went 5 m short of the node on
        # each half (the side road's and the other half's EDGE_DECAL_GAP_M); now one edge, the corners' arcs whole
        f = features([[(-100, 0), (0, 0)], [(0, 0), (100, 0)], [(0, 0), (0, 100)]], ["tertiary"] * 3)
        chunks = [c for t in ((0, -1), (-1, -1), (0, 0), (-1, 0))
                  for c in roads.build_tile(FRAME, *t, [f], flat, walks=[], country="DE")]
        south = [np.array(c["n"]) for c in chunks if c["m"] == DECAL and np.all(np.array(c["n"])[:, 1] < 0)]
        xs = np.sort(np.concatenate([[p[0, 0], p[-1, 0]] for p in south]))
        self.assertTrue(np.any(np.abs(xs) < 0.01))                  # both halves reach the node
        self.assertEqual(len(curved(chunks, DECAL)), 2)
        # a ramp leaving a one-way carriageway keeps its gore clear (roads._going_on: no reduced gap there)
        sp = roads.Spec(7.0, "paved", False, "motorway", True, dir=1)
        rows = [("a", sp, np.array([[-100.0, 0.0, 0.0], [0.0, 0.0, 0.0]]), 0.0),
                ("b", sp, np.array([[0.0, 0.0, 0.0], [100.0, 0.0, 0.0]]), 0.0),
                ("r", sp, np.array([[0.0, 0.0, 0.0], [100.0, -30.0, 0.0]]), 0.0)]
        self.assertEqual(roads._going_on(rows), {("a", "b"), ("b", "a")})

    def test_corners_with_sidewalks_are_unchanged(self):
        f = tee("residential")
        urban = lambda la, lo: np.ones(np.shape(la), bool)      # noqa: E731
        self.assertTrue(all(roads.has_sidewalks(sp) for sp in roads.road_specs(f, urban)))
        self.assertEqual(roadcorners.source([f], urban, frame=FRAME)[2], [])
        for in_ground in (False, True):
            with patch.object(roads, "ROADS_IN_GROUND", in_ground):
                got = roads.build_tile(FRAME, 0, 0, [f], flat, urban, walks=[], country="DE")
                with patch.object(roadcorners, "tile", lambda *a, **k: ([], {})):
                    before = roads.build_tile(FRAME, 0, 0, [f], flat, urban, walks=[], country="DE")
            self.assertEqual(got, before)


def junctions_pave():
    import junctions
    return junctions.PAVE_RADIUS


if __name__ == "__main__":
    unittest.main()
