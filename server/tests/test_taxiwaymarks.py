"""taxiwaymarks: the yellow centreline along the paved taxiways and the runway holding position marking where one
meets a runway. The centreline follows the mapped line and stops short of a runway; a taxiway entering a runway has
ICAO pattern A across its width on the taxiway side (a mapped holding_position point moves it); Features of anywhere
else and unpaved ways draw nothing; the tile's chunks are the strokes, each in exactly one tile."""
import json
import math
import os
import sys
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import aeroways  # noqa: E402
import airportgrade  # noqa: E402
import geodesy  # noqa: E402
import mapgeom  # noqa: E402
import parkedaircraft as pa  # noqa: E402
import standmarks  # noqa: E402
import standpaths  # noqa: E402
import taxiwaymarks as tm  # noqa: E402

FRAME = geodesy.EnuFrame(-27.39, 153.118)
RUNWAY_W = 45.0
HALF = RUNWAY_W / 2


def lonlat(e, n):
    la, lo, _ = FRAME.enu_to_geodetic(np.asarray(e, np.float64), np.asarray(n, np.float64), 0.0)
    return np.stack([lo, la], 1)


def line(*pts):
    return (mapgeom.LINE, [[lonlat([p[0] for p in pts], [p[1] for p in pts])]])


_IDS = iter(range(10 ** 6))


def feats(*rows):
    """Infrastructure Features of (geometry, class, subtype, tags dict, surface) rows (ids unique over the tests: two
    cells' Features share an id only for one feature)."""
    ids = np.array([f"f{next(_IDS):06d}" for _ in rows])
    f = mapgeom.build("infrastructure", ids, [r[0] for r in rows])
    f.attrs = {"class": mapgeom.text_column([r[1] for r in rows]),
               "subtype": mapgeom.text_column([r[2] for r in rows]),
               "tags": mapgeom.text_column([json.dumps(r[3]) for r in rows]),
               "surface": mapgeom.text_column([r[4] for r in rows])}
    return f


def runway(*pts):
    return (line(*pts), "runway", "airport", {"width": str(RUNWAY_W)}, None)


def taxiway(*pts, tags=None, surface=None, cls="taxiway"):
    return (line(*pts), cls, "airport", tags or {}, surface)


def hold_point(e, n):
    return ((mapgeom.POINT, [[lonlat([e], [n])]]), "holding_position", "airport", {"aeroway": "holding_position"}, None)


def metres(stroke):
    """A stroke's (x0, y0, x1, y1) east / north of the frame's origin."""
    e, n, _ = FRAME.geodetic_to_enu(np.array([stroke[0], stroke[2]]), np.array([stroke[1], stroke[3]]), np.zeros(2))
    return float(e[0]), float(n[0]), float(e[1]), float(n[1])


def filled(f, stands, site=None):
    """[(polygon (k, 2) m east / north of the frame's origin, colour, layer)] of the stands' filled marks."""
    return [x[:3] for x in filled_uv(f, stands, site)]


def filled_uv(f, stands, site=None):
    """[(polygon, colour, layer, UVs (k, 2) or None)]: a character of the lettering is a quad with its UVs into the font sheet."""
    out = []
    for la, lo, colour, layer, *uv in tm.polygons([f], list(stands), site):
        e, n, _ = FRAME.geodetic_to_enu(la, lo, np.zeros(len(la)))
        out.append((np.stack([e, n], 1), colour, layer, uv[0] if uv else None))
    return out


def of(got, kind):
    return [s for s in got if s[5] == kind]


class Centreline(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(tm, "_edge_lines", lambda sc, feats_list: [])         # (the edge lines: TaxiEdges)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_follows_the_mapped_line_in_yellow(self):
        with mock.patch.object(tm, "SMOOTH_MAPPED_M", 0.0):                 # (as mapped: the vertices exactly)
            got = tm.strokes([feats(runway((-1000, 0), (1000, 0)), taxiway((0, 100), (0, 400), (200, 400)))])
        centre = of(got, tm.CENTRE_KIND)
        self.assertEqual(len(centre), 2, "one stroke a segment of the line")
        for s in centre:
            self.assertEqual((s[4], s[5], s[6]), (tm.CENTRE_W_M, tm.CENTRE_KIND, "yellow"))
        want = [(0, 100, 0, 400), (0, 400, 200, 400)]
        for s, w in zip(sorted(centre, key=lambda s: metres(s)[2]), want):
            np.testing.assert_allclose(metres(s), w, atol=0.05)

    def test_a_mapped_corner_is_painted_as_an_arc_of_the_radius(self):
        got = tm.strokes([feats(runway((-1000, 0), (1000, 0)), taxiway((0, 100), (0, 400), (200, 400)))])
        chain = [metres(s) for s in sorted(of(got, tm.CENTRE_KIND), key=lambda s: metres(s)[1] + metres(s)[0])]
        P = np.array([chain[0][:2]] + [m[2:] for m in chain])
        np.testing.assert_allclose(P[0], (0, 100), atol=0.05)                # (the ends as mapped ...)
        np.testing.assert_allclose(P[-1], (200, 400), atol=0.05)
        self.assertGreater(len(P), 10)                                       # (... the corner an arc, no vertex turning over 6 degrees)
        self.assertGreaterEqual(tm._min_radius(P), tm.MIN_RADIUS_M - 0.5)
        self.assertLess(float(np.min(np.hypot(*(P - [0, 400]).T))), tm.MIN_RADIUS_M * 0.5)       # (cut inside the mapped corner)

    def test_a_t_junction_of_mapped_ways_is_tangent_and_an_end_in_an_apron_reaches_its_edge(self):
        main = taxiway((-200, 0), (200, 0))
        branch = taxiway((0, 150), (0, 0))
        got = tm.network([feats(main, branch)])
        self.assertEqual(got.audit().counts(), {"gaps": 0, "opens": 0, "kinks": 0, "sbends": 0})
        stub = taxiway((300, 200), (300, 300), cls="taxilane")              # a lane that ends 10 m short of an apron's edge
        apron = (mapgeom.POLYGON, [[lonlat([250, 350, 350, 250, 250], [50, 50, 310, 310, 50])]])
        f = feats(stub, (apron, "apron", "airport", {"aeroway": "apron"}, None))
        with mock.patch.object(tm, "_pavement_rings", lambda sc, fl: (tm._apron_rings(sc, fl), [])):       # (the apron's edge alone)
            nw = tm.network([f])
        la, lo, _ = nw.frame.enu_to_geodetic(*np.vstack([x[0] for x in nw.lines]).T, np.zeros(sum(len(x[0]) for x in nw.lines)))
        north = FRAME.geodetic_to_enu(la, lo, np.zeros(len(la)))[1]
        self.assertAlmostEqual(float(north.max()), 310.0, delta=0.3)         # (the lane's end carried on to the apron's edge)
        self.assertEqual(nw.audit().counts(), {"gaps": 0, "opens": 1, "kinks": 0, "sbends": 0})      # (its other end: in the apron, on nothing)

    def test_taxilane_has_one_too(self):
        got = tm.strokes([feats(taxiway((0, 0), (80, 0), cls="taxilane"))])
        self.assertEqual(len(of(got, tm.CENTRE_KIND)), 1)

    def test_a_way_ending_on_a_runway_runs_on_to_its_centreline_one_crossing_it_stops_short_of_its_edge(self):
        got = tm.strokes([feats(runway((-1000, 0), (1000, 0)), taxiway((0, 300), (0, 0)),
                                taxiway((300, -300), (300, 300)))])
        centre = of(got, tm.CENTRE_KIND)
        self.assertTrue(centre)
        ending = [abs(v) for s in centre if abs(metres(s)[0]) < 30.0 and abs(metres(s)[2]) < 30.0 for v in metres(s)[1::2]]
        self.assertLess(min(ending), 0.3, "the line runs on to the runway's centreline")
        crossing = [metres(s) for s in centre if abs(metres(s)[0] - 300) < 0.1]
        near = [abs(v) for m in crossing for v in m[1::2]]
        self.assertGreaterEqual(min(near), HALF + tm.RUNWAY_GAP_M - 0.2, "the crossing line stops short of the runway's edge")
        self.assertLess(min(near), HALF + tm.RUNWAY_GAP_M + 0.5, "... and only just")
        self.assertTrue(any(min(m[1], m[3]) > 0 for m in crossing))
        self.assertTrue(any(max(m[1], m[3]) < 0 for m in crossing), "both sides of the crossing")
        self.assertTrue([s for s in got if s[5] in ("hold_solid", "hold_dashed")], "the hold short marking stays where it is")

    def run_metres(self, *rows):
        """[(xy (k, 2) m east / north of the frame's origin, first, last)] of the painted mapped centrelines."""
        sc = tm.scene([feats(*rows)])
        out = []
        for run, _cls, _wi, first, last in tm._mapped_runs(sc):
            la, lo, _ = sc.frame.enu_to_geodetic(run[:, 0], run[:, 1], np.zeros(len(run)))
            e, n, _ = FRAME.geodetic_to_enu(la, lo, np.zeros(len(la)))
            out.append((np.stack([e, n], 1), first, last))
        return out

    def test_a_way_meeting_a_runway_at_a_shallow_angle_finishes_with_a_tangent_arc_into_the_centreline(self):
        a = math.radians(30.0)                                      # the way arrives at 30 degrees to the runway (+x)
        start = (-400.0 * math.cos(a), 400.0 * math.sin(a))
        runs = self.run_metres(runway((-1000, 0), (1000, 0)), taxiway(start, (0, 0)))
        ends = [r for r, _f, last in runs if last]
        self.assertEqual(len(ends), 1)
        P = ends[0]
        self.assertLess(abs(P[-1][1]), 0.05)                        # (the end on the centreline)
        d = P[-1] - P[-2]
        self.assertLess(abs(math.degrees(math.atan2(d[1], d[0]))), 4.0, "tangent to the centreline (the last chord of an arc)")
        self.assertGreater(P[-1][0], 3.0)                           # (it turns onto the runway: the end is along it, past the meeting point)
        d0 = P[1] - P[0]
        self.assertAlmostEqual(math.degrees(math.atan2(d0[1], d0[0])), -30.0, delta=1.0)       # (and keeps its own line before the arc)
        tn = [math.degrees(abs(math.atan2(float((b - a_)[0] * (c - b)[1] - (b - a_)[1] * (c - b)[0]), float(np.dot(b - a_, c - b)))))
              for a_, b, c in zip(P[:-2], P[1:-1], P[2:])]
        self.assertLess(max(tn), 25.0, "no kink")

    def test_a_way_meeting_a_runway_at_a_steep_angle_goes_straight_to_the_centreline(self):
        runs = self.run_metres(runway((-1000, 0), (1000, 0)), taxiway((50, 300), (0, 0)))
        P = [r for r, _f, last in runs if last][0]
        self.assertLess(abs(P[-1][1]), 0.05)
        self.assertLess(abs(P[-1][0]), 0.5)                         # (no arc: it arrives where it was mapped)
        d = P[-1] - P[-2]
        self.assertLess(math.degrees(abs(math.atan2(d[0], -d[1]))), 12.0)

    def test_unpaved_and_other_features_draw_nothing(self):
        self.assertEqual(tm.strokes([feats(taxiway((0, 0), (100, 0), surface="grass"))]), [])
        self.assertEqual(tm.strokes([feats(taxiway((0, 0), (100, 0), tags={"surface": "dirt"}))]), [])
        self.assertEqual(tm.strokes([feats((line((0, 0), (100, 0)), "taxiway", "transit", {}, None))]), [])
        self.assertEqual(tm.strokes([feats((line((0, 0), (100, 0)), "footway", "airport", {}, None))]), [])
        self.assertEqual(tm.strokes([None]), [])
        self.assertEqual(tm.strokes([]), [])
        self.assertEqual(tm.tile_chunks([feats((line((0, 0), (100, 0)), "taxiway", "transit", {}, None))], FRAME, 0, 0,
                                        lambda la, lo: np.zeros(len(la)), lambda tx, ty: (0, 0, 256, 256)), [])

    def test_the_same_feature_in_two_cells_is_painted_once(self):
        a = feats(runway((-1000, 0), (1000, 0)), taxiway((0, 300), (0, 0)))
        b = feats(runway((-1000, 0), (1000, 0)), taxiway((0, 300), (0, 0)))
        b.ids = a.ids.copy()
        self.assertEqual(len(tm.strokes([a, b])), len(tm.strokes([a])))

    def test_a_runway_in_another_cell_still_cuts_a_crossing_way_and_holds_the_way_that_ends_on_it(self):
        taxi, rw = feats(taxiway((300, -300), (300, 300))), feats(runway((-1000, 0), (1000, 0)))
        self.assertEqual(len(tm._mapped_runs(tm.scene([taxi]))), 1)
        self.assertEqual(len(tm._mapped_runs(tm.scene([taxi, rw]))), 2)           # (cut at the runway, from the other cell)
        end = feats(taxiway((0, 300), (0, 0)))
        got = tm.strokes([end, rw])
        self.assertTrue([s for s in got if s[5] in ("hold_solid", "hold_dashed")])


class Holds(unittest.TestCase):
    def marking(self, rows):
        got = tm.strokes([feats(*rows)])
        return [s for s in got if s[5] in ("hold_solid", "hold_dashed")]

    def test_taxiway_entering_a_runway_has_pattern_a_across_its_width(self):
        hold = self.marking([runway((-1000, 0), (1000, 0)), taxiway((0, 300), (0, 0))])
        solid, dashed = of(hold, "hold_solid"), of(hold, "hold_dashed")
        self.assertEqual(len(solid), 2)
        self.assertGreaterEqual(len(dashed), 2 * 4)
        for s in hold:
            self.assertEqual((s[4], s[6]), (tm.HOLD_LINE_W_M, "yellow"))
        w = aeroways.TAXIWAY_M
        for s in solid:                                     # across the way's whole width
            x0, y0, x1, y1 = metres(s)
            self.assertAlmostEqual(abs(y1 - y0), 0.0, places=2)
            self.assertAlmostEqual(abs(x1 - x0), w, delta=0.05)
            self.assertAlmostEqual((x0 + x1) / 2, 0.0, delta=0.05)
        for s in dashed:
            x0, _, x1, _ = metres(s)
            self.assertLessEqual(max(abs(x0), abs(x1)), w / 2 + 0.05)

        def ys(ss):
            return [metres(s)[1] for s in ss]
        self.assertAlmostEqual(min(ys(dashed)), tm.HOLD_DIST_M, delta=0.1, msg="the nearest line is HOLD_DIST_M out")
        self.assertGreater(min(ys(solid)), max(ys(dashed)), "the solid lines are on the taxiway side")
        self.assertEqual(len(set(round(y, 2) for y in ys(dashed))), 2)
        self.assertEqual(len(set(round(y, 2) for y in ys(solid))), 2)

    def test_a_taxiway_across_a_runway_has_one_each_side(self):
        hold = self.marking([runway((-1000, 0), (1000, 0)), taxiway((0, -300), (0, 300))])
        solid = of(hold, "hold_solid")
        self.assertEqual(len(solid), 4)
        up = [s for s in solid if metres(s)[1] > 0]
        down = [s for s in solid if metres(s)[1] < 0]
        self.assertEqual((len(up), len(down)), (2, 2))
        dashed = of(hold, "hold_dashed")
        self.assertAlmostEqual(min(abs(metres(s)[1]) for s in dashed), tm.HOLD_DIST_M, delta=0.1)
        self.assertLess(max(metres(s)[1] for s in down), -tm.HOLD_DIST_M, "the solid lines are further out, below too")

    def test_the_width_tag_sets_the_length(self):
        hold = self.marking([runway((-1000, 0), (1000, 0)), taxiway((0, 300), (0, 0), tags={"width": "30"})])
        for s in of(hold, "hold_solid"):
            x0, _, x1, _ = metres(s)
            self.assertAlmostEqual(abs(x1 - x0), 30.0, delta=0.05)

    def test_a_mapped_holding_position_is_used(self):
        hold = self.marking([runway((-1000, 0), (1000, 0)), taxiway((0, 300), (0, 0)), hold_point(1.0, 85.0)])
        self.assertAlmostEqual(min(metres(s)[1] for s in of(hold, "hold_dashed")), 85.0, delta=0.1)
        far = self.marking([runway((-1000, 0), (1000, 0)), taxiway((0, 300), (0, 0)), hold_point(40.0, 85.0)])
        self.assertAlmostEqual(min(metres(s)[1] for s in of(far, "hold_dashed")), tm.HOLD_DIST_M, delta=0.1,
                               msg="a point off the taxiway is not its holding position")

    def test_none_without_a_runway_or_off_one(self):
        self.assertEqual(self.marking([taxiway((0, 300), (0, 0))]), [])
        self.assertEqual(self.marking([runway((-1000, 0), (1000, 0)), taxiway((0, 300), (0, 100))]), [], "ends short of it")
        self.assertEqual(self.marking([runway((-1000, 0), (1000, 0)), taxiway((0, 40), (0, 0))]), [],
                         "no room for it before the way ends")
        self.assertEqual(self.marking([runway((-1000, 0), (1000, 0)), taxiway((0, 300), (0, 0), surface="gravel")]), [])

    def test_a_non_airport_feature_set_draws_nothing(self):
        rows = [(line((-1000, 0), (1000, 0)), "runway", "transit", {}, None),
                (line((0, 300), (0, 0)), "taxiway", "transit", {}, None)]
        self.assertEqual(tm.strokes([feats(*rows)]), [])


class TileChunks(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(tm, "_edge_lines", lambda sc, feats_list: [])         # (the edge lines: TaxiEdges)
        patcher.start()
        self.addCleanup(patcher.stop)
        smooth = mock.patch.object(tm, "SMOOTH_MAPPED_M", 0.0)                             # (the tiles cut the lines as mapped)
        smooth.start()
        self.addCleanup(smooth.stop)

    SIDE = 256.0

    def bbox(self, tx, ty):
        return (tx * self.SIDE, ty * self.SIDE, (tx + 1) * self.SIDE, (ty + 1) * self.SIDE)

    def chunks(self, f, tx, ty):
        return tm.tile_chunks([f], FRAME, tx, ty, lambda la, lo: np.full(len(la), 7.0), self.bbox)

    def test_chunks_are_yellow_decals_each_in_one_tile(self):
        f = feats(runway((-600, 0), (600, 0)), taxiway((50, 600), (50, 0), (300, 0)))
        per = {(tx, ty): self.chunks(f, tx, ty) for tx in range(-4, 4) for ty in range(-4, 4)}
        allc = [c for v in per.values() for c in v]
        self.assertTrue(allc)
        look = airportgrade.MARK_LOOK["yellow"]
        for c in allc:
            self.assertEqual((c["m"], c["tl"], c["p"]), (look["m"], look["tl"], look["p"]))
            self.assertIn(c["w"], (round(tm.CENTRE_W_M, 2), round(tm.HOLD_LINE_W_M, 2)))
            self.assertGreaterEqual(len(c["n"]), 2)
            self.assertAlmostEqual(c["n"][0][2], 7.0, places=1)
        # the pieces tile the strokes: the length painted is the strokes' whole length, once
        want = sum(float(np.hypot(metres(s)[2] - metres(s)[0], metres(s)[3] - metres(s)[1])) for s in tm.strokes([f]))
        got = sum(float(np.hypot(*np.diff(np.array(c["n"])[:, :2], axis=0).T).sum()) for c in allc)
        self.assertAlmostEqual(got, want, delta=want * 0.01)

    def test_pieces_are_short_and_far_tiles_empty(self):
        f = feats(taxiway((0, 100), (900, 100)))
        v = sum((self.chunks(f, tx, 0) for tx in range(4)), [])
        self.assertGreater(len(v), 3)
        for c in v:
            p = np.array(c["n"])
            self.assertLessEqual(float(np.hypot(*np.diff(p[:, :2], axis=0).T).sum()), airportgrade.MARK_PIECE_M + 1e-6)
        self.assertEqual(self.chunks(f, 0, 40), [])

    def border_points(self, f, stands=None, tx=0, edge=SIDE):
        """(the nodes of tile (tx, 0)'s chunks, the nodes of tile (tx + 1, 0)'s) lying on their shared edge x = edge."""
        run = lambda t: [c for c in tm.tile_chunks([f], FRAME, t, 0, lambda la, lo: np.full(len(la), 7.0), self.bbox, stands)]
        a, b = run(tx), run(tx + 1)
        for chunks, box in ((a, self.bbox(tx, 0)), (b, self.bbox(tx + 1, 0))):         # (nothing outside its own tile)
            for c in chunks:
                for x, y, _z in c["n"]:
                    self.assertTrue(box[0] - 1e-3 <= x <= box[2] + 1e-3 and box[1] - 1e-3 <= y <= box[3] + 1e-3, (x, y))
        on = lambda chunks: sorted((round(x, 2), round(y, 2)) for c in chunks for x, y, _z in c["n"] if abs(x - edge) < 1e-3)
        return on(a), on(b), a, b

    def test_a_line_across_two_tiles_is_painted_in_both_and_the_pieces_join(self):
        f = feats(taxiway((150, 100), (400, 100)))                  # across the edge x = 256
        left, right, a, b = self.border_points(f)
        self.assertTrue(a and b)
        self.assertEqual(left, right)
        self.assertEqual(left, [(256.0, 100.0)])                    # (the two pieces end and begin at the same point)
        length = lambda cs: sum(float(np.hypot(*np.diff(np.array(c["n"])[:, :2], axis=0).T).sum()) for c in cs)
        self.assertAlmostEqual(length(a), 256.0 - 150.0, delta=0.01)
        self.assertAlmostEqual(length(b), 400.0 - 256.0, delta=0.01)

    def test_a_line_that_leaves_a_tile_and_comes_back_is_painted_in_its_parts(self):
        f = feats(taxiway((150, 100), (300, 100), (300, 130), (150, 130)))      # out across x = 256 and back
        left, right, a, b = self.border_points(f)
        self.assertEqual(left, right)
        self.assertEqual(left, [(256.0, 100.0), (256.0, 130.0)])
        length = lambda cs: sum(float(np.hypot(*np.diff(np.array(c["n"])[:, :2], axis=0).T).sum()) for c in cs)
        self.assertAlmostEqual(length(a), 2 * (256.0 - 150.0), delta=0.01)      # (both parts in the first tile)
        self.assertAlmostEqual(length(b), 2 * (300.0 - 256.0) + 30.0, delta=0.01)       # (the bend, one part, in the next)

    def test_a_bend_on_the_edge_and_a_line_along_it_are_not_lost(self):
        f = feats(taxiway((256, 20), (256, 200)))                   # along the edge: painted (by both, at worst)
        _l, _r, a, b = self.border_points(f)
        self.assertTrue(a or b)

    def test_bends_are_one_polyline(self):
        f = feats(taxiway((10, 10), (10, 30), (30, 30)))
        v = self.chunks(f, 0, 0)
        self.assertEqual(len(v), 1)
        pts = np.array(v[0]["n"])[:, :2]
        self.assertTrue(np.any(np.hypot(*(pts - [10, 30]).T) < 0.1), "it passes the mapped vertex")


def craft(kind, x, y, ne, nn, mode="gate"):
    """A parked aircraft (parkedaircraft.Craft) centred at (x, y) east / north of the frame's origin."""
    la, lo, _ = FRAME.enu_to_geodetic(np.array([x], np.float64), np.array([y], np.float64), 0.0)
    return pa.Craft(kind, float(la[0]), float(lo[0]), ne, nn, mode)


class Unpaved(unittest.TestCase):
    """The lead-in tests lay lanes and stands on bare ground with no apron mapped: with no paving known a lens is held to none
    (standpaths: paved), so these are about the geometry alone."""
    def setUp(self):
        patcher = mock.patch.object(tm, "_pavement_rings", lambda sc, fl: ([], []))
        patcher.start()
        self.addCleanup(patcher.stop)


def leads(f, stands):
    """[polyline (k, 2) m east / north of the frame's origin] of every yellow lead-in line the stands paint on the Features f: a half
    lens, a final run to the nose, a spine or a connector, each as one polyline."""
    out = []
    for la, lo, w, kind, _col in tm._lead_lines(tm.scene([f]), list(stands), None)[0]:
        if kind == tm.CENTRE_KIND and abs(w - tm.LEAD_W_M) < 1e-9:
            e, n, _ = FRAME.geodetic_to_enu(la, lo, np.zeros(len(la)))
            out.append(np.stack([e, n], 1))
    return out


class LeadIns(Unpaved):
    """A stand's yellow lead-in: from the taxilane its axis meets, an arc into a straight run along the axis to the nose,
    and a stop bar across it at the nose."""
    LANE = taxiway((-120, 0), (100, 0), cls="taxilane")             # (more lane behind the axis than before it)
    NARROW = pa.SPECS["narrow"]

    def polys(self, f, *stands):
        """[(polygon (k, 2) m, colour, layer)] of the stands' filled marks."""
        return filled(f, stands)

    def painted(self, f, *stands):
        base = tm.strokes([f])
        got = tm.strokes([f], list(stands))
        self.assertEqual(got[:len(base)], base)                         # (the mapped lines unchanged)
        got = [s for s in got[len(base):] if s[5] == tm.CENTRE_KIND]    # (the lead-in and the stop bar, not the stand details)
        return [metres(s) for s in got], got

    def halves(self, f, stand):
        """([the half lenses (k, 2)], the final run (2, 2)) the stand paints: from the lane to the axis, and from there to the nose."""
        lines = leads(f, [stand])
        final = [P for P in lines if len(P) == 2]
        self.assertEqual(len(final), 1)
        return [P for P in lines if len(P) > 2], final[0]

    def test_lane_arc_straight_run_to_the_nose_and_a_stop_bar(self):
        f = feats(self.LANE)
        L, span = self.NARROW[0], self.NARROW[1]
        stand = craft("narrow", 0.0, 40.0, 0.0, 1.0)                                         # nose 40 + L/2 north, tail to the lane
        nose = 40.0 + L / 2.0
        for s in self.painted(f, stand)[1]:
            self.assertEqual((s[4], s[5], s[6]), (tm.LEAD_W_M, tm.CENTRE_KIND, "yellow"))      # the lead-in's paint
        halves, final = self.halves(f, stand)
        self.assertEqual(len(halves), 2)                                                     # a half lens each way along the lane
        tl = standpaths.LENS_R_K * span
        self.assertEqual(sorted(round(float(P[0][0]) / tl) for P in halves), [-1, 1])        # ... each leaving it a radius before the axis
        for P in halves:
            self.assertAlmostEqual(P[0][1], 0.0, 1)                                          # it starts on the lane ...
            self.assertAlmostEqual(abs(P[0][0]), tl, delta=0.5)
            d0, d1 = P[1] - P[0], P[-1] - P[-2]
            self.assertGreater(abs(d0[0]) / np.hypot(*d0), 0.97)                             # ... leaves along the lane (tangent) ...
            self.assertGreater(d1[1] / np.hypot(*d1), 0.97)                                  # ... and arrives along the axis, northward
            self.assertAlmostEqual(P[-1][0], 0.0, 2)
            self.assertAlmostEqual(P[-1][1], final[0][1], 1)                                 # (the run to the nose starts where it arrives)
        self.assertAlmostEqual(final[1][0], 0.0, 2)
        self.assertAlmostEqual(final[1][1], nose, 2)                                         # ... and ends at the nose
        self.assertGreaterEqual(final[1][1] - final[0][1], tm.lead_straight(L) - 0.05)
        bars = [p for p, colour, layer in self.polys(f, stand) if colour == "yellow" and layer == "paint"
                and abs(p[:, 1].mean() - nose) < 0.01 and abs(p[:, 0].mean()) < 0.01]
        self.assertEqual(len(bars), 1)                                                       # the stop bar across the axis at the nose
        self.assertAlmostEqual(np.ptp(bars[0][:, 0]), tm.STOP_BAR_M, 2)
        self.assertAlmostEqual(np.ptp(bars[0][:, 1]), tm.STOP_BAR_W_M, 2)

    def test_a_curve_not_a_corner_between_lane_and_axis(self):
        halves, final = self.halves(feats(self.LANE), craft("turboprop", 30.0, 40.0, 0.0, 1.0))
        self.assertEqual(len(halves), 2)
        for P in halves:
            line = np.vstack([P, final[1:]])
            turn = turns(line)
            self.assertLess(max(turn), 25.0)                            # (no vertex turns sharply: a 90 degree turn in steps)
            self.assertAlmostEqual(sum(turn), 90.0, delta=4.0)          # (a quarter turn in all, less the chords' ends)

    def test_a_lane_that_ends_short_is_carried_on(self):
        f = feats(taxiway((-120, 0), (-30, 0), cls="taxilane"))
        halves, final = self.halves(f, craft("narrow", 0.0, 20.0, 0.0, 1.0))
        lens = [P for P in halves if np.hypot(*(P[-1] - final[0])) < 0.05]           # (the curve onto the final run: one, from the lane's end)
        self.assertEqual(len(lens), 1)
        P = np.vstack([lens[0], final[1:]])
        self.assertAlmostEqual(P[0][0], -30.0, 1)                       # from the lane's end ...
        self.assertAlmostEqual(P[0][1], 0.0, 1)
        self.assertGreaterEqual(tm.final_straight(P), tm.lead_straight(self.NARROW[0]) - 0.05)       # ... the straight run to the nose
        self.assertGreaterEqual(tm._min_radius(P), tm.MIN_RADIUS_M - 0.5)                          # (the nose is 38.8 m on: no room for the
        self.assertAlmostEqual(P[-1][1], 20.0 + self.NARROW[0] / 2.0, 2)                            # straight and a 15 m arc beside the lane's end)

    def test_a_stand_on_the_far_side_is_reached_from_its_own_side(self):
        segs, _ = self.painted(feats(self.LANE), craft("narrow", 0.0, -40.0, 0.0, -1.0))
        self.assertAlmostEqual(segs[0][1], 0.0, 1)
        self.assertAlmostEqual(segs[-1][3], -40.0 - self.NARROW[0] / 2.0, 2)

    def test_no_lead_in_for_a_ga_row_and_a_straight_stub_for_a_stand_without_a_lane(self):
        f = feats(self.LANE)
        self.assertEqual(self.painted(f, craft("light", 0.0, 30.0, 0.0, 1.0, "ga"))[0], [])
        segs = self.painted(f, craft("narrow", 600.0, 900.0, 0.0, 1.0))[0]                         # (a lane 1 km off)
        self.assertEqual(len(segs), 1)                                  # the stub along the axis
        nose = 900.0 + self.NARROW[0] / 2.0
        self.assertAlmostEqual(segs[0][3], nose, 2)
        self.assertAlmostEqual(segs[0][3] - segs[0][1], self.NARROW[0] + tm.LEAD_STUB_M, 2)
        self.assertAlmostEqual(segs[0][0], 600.0, 2)

    def test_a_lane_ending_at_the_nose_along_the_axis_is_carried_on_to_it(self):
        f = feats(taxiway((0, 0), (0, 40), cls="taxilane"))
        segs, _ = self.painted(f, craft("narrow", 0.0, 60.0, 0.0, 1.0))      # nose at 78.8: the lane's end 38.8 short of it
        self.assertTrue(all(abs(s[0]) < 0.01 and abs(s[2]) < 0.01 for s in segs))     # (a straight line along the axis, however many pieces)
        self.assertAlmostEqual(min(s[1] for s in segs), 40.0, 1)
        self.assertAlmostEqual(max(s[3] for s in segs), 60.0 + self.NARROW[0] / 2.0, 2)

    def test_a_lane_across_a_taxiways_pad_is_the_last_resort_and_a_runways_is_never_crossed(self):
        stand = craft("narrow", 0.0, 40.0, 0.0, 1.0)
        lane = taxiway((-120, 0), (100, 0), cls="taxilane")
        across = feats(lane, taxiway((-4, 20), (-4, 45)))                   # a taxiway's pad (15 m wide) under the run
        got = self.painted(across, stand)[0]
        self.assertAlmostEqual(got[0][1], 0.0, 1)                           # (no other lane: the lane's line, across the pad)
        beside = feats(self.LANE, taxiway((-40, 20), (-40, 45)))
        self.assertAlmostEqual(self.painted(beside, stand)[0][0][1], 0.0, 1)
        stub = self.painted(feats(self.LANE, runway((-100, 40), (100, 40))), stand)[0]        # (a runway's is not to be crossed)
        self.assertEqual(len(stub), 1)
        self.assertAlmostEqual(stub[0][3] - stub[0][1], self.NARROW[0] + tm.LEAD_STUB_M, 2)   # (the stub, not a lane's line)

    def test_the_lane_it_leaves_may_be_a_taxiway_and_another_lane_is_tried_when_the_nearest_crosses_a_pad(self):
        stand = craft("narrow", 0.0, 40.0, 0.0, 1.0)
        own = feats(taxiway((-120, 0), (100, 0)))                           # a taxiway: its own lane
        segs = self.painted(own, stand)[0]
        self.assertAlmostEqual(segs[0][1], 0.0, 1)
        both = feats(self.LANE, taxiway((-120, 30), (100, 30)))             # the taxiway nearer: no room for the straight run and the arc
        self.assertAlmostEqual(self.painted(both, stand)[0][0][1], 0.0, 1)       # (the nose is 58.8 m on, 28.8 from it): the farther lane

    def test_a_stand_listed_twice_or_overlapped_paints_one_lead_in(self):
        f = feats(self.LANE)
        one = craft("narrow", 0.0, 60.0, 0.0, 1.0)
        twin = craft("narrow", 0.0, 60.0, 0.0, 1.0)
        got = tm.strokes([f], [one])
        self.assertEqual(tm.strokes([f], [one, twin]), got)
        self.assertEqual(tm.strokes([f], [one, craft("light", 0.0, 62.0, 0.0, 1.0)]), got)    # (a shorter kind on top: not kept)
        self.assertEqual(tm.strokes([f], [one, craft("light", 0.0, 62.0, 0.0, 1.0, "ga")]), got)

    def test_a_lead_in_and_its_stop_bar_across_two_tiles_are_painted_by_both_and_join(self):
        lane = taxiway((100, 0), (420, 0), cls="taxilane")
        f = feats(lane)
        st = [craft("narrow", 255.0, 40.0, 0.0, 1.0)]               # its stop bar and the lane's run cross x = 256
        tc = TileChunks()
        run = lambda t: tm.tile_chunks([f], FRAME, t, 0, lambda la, lo: np.full(len(la), 7.0), tc.bbox, st)
        base = lambda t: tm.tile_chunks([f], FRAME, t, 0, lambda la, lo: np.full(len(la), 7.0), tc.bbox)
        for t in (0, 1):
            self.assertGreater(len(run(t)), len(base(t)))           # (the lead-in is in both tiles)
        on = lambda cs: sorted((round(x, 2), round(y, 2)) for c in cs for x, y, _z in c["n"] if abs(x - tc.SIDE) < 1e-3)
        want = on(run(0))
        self.assertTrue(want)
        self.assertEqual(want, on(run(1)))                          # (every piece's end on the edge has its partner)
        self.assertGreater(len(want), len(on(base(0))))             # (more than the lane's: the stop bar's)

    def test_chunks_paint_a_listed_twice_stand_once_across_tiles(self):
        f = feats(self.LANE)
        tc = TileChunks()
        run = lambda stands: [c for tx in range(-2, 2) for ty in range(-2, 2) for c in
                              tm.tile_chunks([f], FRAME, tx, ty, lambda la, lo: np.full(len(la), 7.0), tc.bbox, stands)]
        length = lambda cs: sum(float(np.hypot(*np.diff(np.array(c["n"])[:, :2], axis=0).T).sum()) for c in cs if c["n"])
        st = craft("narrow", 0.0, 40.0, 0.0, 1.0)
        self.assertAlmostEqual(length(run([st, st])), length(run([st])), places=3)
        quads = lambda cs: sum(len(c["poly"]) for c in cs if c.get("poly"))
        self.assertEqual(quads(run([st, st])) // 4 > 0, True)                   # (the boxes and lettering are in the tiles too)

    def test_chunks_carry_the_lead_in_each_in_one_tile(self):
        f = feats(self.LANE)
        st = [craft("narrow", 0.0, 40.0, 0.0, 1.0)]
        tc = TileChunks()
        run = lambda stands: [c for tx in range(-2, 2) for ty in range(-2, 2) for c in
                              tm.tile_chunks([f], FRAME, tx, ty, lambda la, lo: np.full(len(la), 7.0), tc.bbox, stands)]
        plain, with_lead = run(None), run(st)
        length = lambda cs: sum(float(np.hypot(*np.diff(np.array(c["n"])[:, :2], axis=0).T).sum()) for c in cs
                                if c["n"] and c["w"] == tm.LEAD_W_M and c.get("layer") != "edge")
        wings = [metres(s) for s in tm.strokes([f], st) if s[5] == tm.STAND_KIND]       # (the wing-tip lines are as wide now)
        want = sum(float(np.hypot(m[2] - m[0], m[3] - m[1])) for m in self.painted(f, *st)[0] + wings)
        self.assertAlmostEqual(length(with_lead) - length(plain), want, delta=want * 0.02)

    def test_wing_tip_lines_and_a_stand_number(self):
        f = feats(self.LANE)
        L = self.NARROW[0]
        span = self.NARROW[1]
        nose = 60.0 + L / 2.0
        c = craft("narrow", 0.0, 60.0, 0.0, 1.0)
        base = tm.strokes([f], [c])
        got = [s for s in base if s[5] == tm.STAND_KIND]
        self.assertEqual(len(got), 2, "no ref: the wing-tip lines only")
        for s in got:
            x0, y0, x1, y1 = metres(s)
            self.assertAlmostEqual(abs(x0), span / 2.0 + tm.WING_LINE_OFF_M, 1)
            self.assertAlmostEqual(x0, x1, 2)                                   # parallel to the axis ...
            self.assertAlmostEqual(y1 - y0, tm.WING_LINE_LEN_M, 2)              # ... 20 m long, ending level with the bar
            self.assertAlmostEqual(y1, nose, 2)
        number = lambda ref: [(p, uv) for p, colour, layer, uv in filled_uv(f, [c._replace(ref=ref)])
                              if layer == "glyph" and p[:, 1].min() > nose + 1.0 and abs(p[:, 0].mean()) < 4.0]
        glyph = number("12")
        self.assertEqual(len(glyph), 2)                                             # (a 1 and a 2: a quad each, with its UVs)
        pts = np.concatenate([p for p, _uv in glyph])
        h = tm.NUM_TEXT
        mid = nose + tm.STOP_BAR_W_M / 2.0 + tm.NUM_AFTER_M + tm.NUM_MARGIN[1] + h / 2.0
        self.assertAlmostEqual((pts[:, 1].min() + pts[:, 1].max()) / 2.0, mid, delta=0.05)   # past the stop bar, the capitals centred on the box ...
        self.assertTrue(h < pts[:, 1].max() - pts[:, 1].min() < 1.2 * h)                            # ... 1.5 m tall (a quad's margin a few % of that),
        self.assertAlmostEqual(float(pts[:, 0].max() + pts[:, 0].min()), 0.0, 2)                    # ... centred on the axis
        self.assertAlmostEqual(float(np.ptp(pts[:, 0])), standmarks.text_width("12", h), 2)
        boxes = [p for p, colour, layer in filled(f, [c._replace(ref="12")]) if colour == "yellow" and layer == "paint"
                 and p[:, 1].min() > nose + 1.0 and abs(p[:, 0].mean()) < 0.01]
        self.assertEqual(len(boxes), 1)                                             # on one yellow box, the text's size and a margin
        self.assertAlmostEqual(np.ptp(boxes[0][:, 0]), standmarks.text_width("12", h) + 2 * tm.NUM_MARGIN[0], 2)
        self.assertAlmostEqual(np.ptp(boxes[0][:, 1]), h + 2 * tm.NUM_MARGIN[1], 2)
        black = [p for p, colour, layer in filled(f, [c._replace(ref="12")]) if layer == "under" and p[:, 1].min() > nose + 1.0
                 and abs(p[:, 0].mean()) < 0.01]
        self.assertEqual(len(black), 1)                                             # ... over a black one BORDER_M larger each side
        self.assertAlmostEqual(np.ptp(black[0][:, 0]) - np.ptp(boxes[0][:, 0]), 2 * tm.BORDER_M, 2)
        self.assertEqual(number("12@"), [])                                         # (no font for an @: none)
        for p, uv in glyph:                                                         # (black letters: the font sheet over the black paint)
            self.assertEqual(uv.shape, (4, 2))
            self.assertTrue(((uv >= 0.0) & (uv <= 1.0)).all())
    def test_stop_bars_per_kind_with_generic_labels_and_a_border(self):
        f = feats(self.LANE)
        for kind, want in (("wide", ["WIDE", "A320", "AT72"]), ("narrow", ["A320", "AT72", "C172"]), ("turboprop", ["AT72", "C172"])):
            at = self.polys(f, craft(kind, 0.0, 60.0, 0.0, 1.0))
            nose = 60.0 + pa.SPECS[kind][0] / 2.0
            bars = sorted([p for p, colour, layer in at if colour == "yellow" and layer == "paint"
                           and abs(np.ptp(p[:, 0]) - tm.STOP_BAR_M) < 0.01 and abs(np.ptp(p[:, 1]) - tm.STOP_BAR_W_M) < 0.01],
                          key=lambda p: -p[:, 1].mean())
            self.assertEqual(len(bars), len(want), kind)                            # a bar for each kind that fits
            self.assertAlmostEqual(bars[0][:, 1].mean(), nose, 2)                   # (the stand's own at the nose, the others back)
            gaps = [bars[i][:, 1].mean() - bars[i + 1][:, 1].mean() for i in range(len(bars) - 1)]
            self.assertTrue(all(g > 1.9 for g in gaps), gaps)
            labels = [p for p, colour, layer in at if colour == "yellow" and layer == "paint"
                      and abs(np.ptp(p[:, 0]) - tm.LABEL_BOX_M[0]) < 0.01 and abs(np.ptp(p[:, 1]) - tm.LABEL_BOX_M[1]) < 0.01]
            self.assertEqual(len(labels), len(want))                                # a 2.2 x 0.9 box beside each, with its text
            for lab in labels:
                inner = [p for p, colour, layer in at if layer == "glyph" and abs(p[:, 0].mean() - lab[:, 0].mean()) < 1.0
                         and abs(p[:, 1].mean() - lab[:, 1].mean()) < 0.45]
                self.assertEqual(len(inner), 4)                                     # (a quad a character of a four-letter code)
                pts = np.concatenate(inner)
                self.assertLessEqual(pts[:, 1].max() - pts[:, 1].min(), tm.LABEL_TEXT * 1.25)
                self.assertGreaterEqual(pts[:, 0].min(), lab[:, 0].min())           # (the text is inside its box)
                self.assertLessEqual(pts[:, 0].max(), lab[:, 0].max())
            for p in bars:                                                          # every bar has its black border under it
                self.assertTrue(any(layer == "under" and abs(p[:, 1].mean() - q[:, 1].mean()) < 1e-3
                                    and abs(np.ptp(q[:, 0]) - np.ptp(p[:, 0]) - 2 * tm.BORDER_M) < 1e-3 for q, c_, layer in at))
        raw = tm.strokes([f], [craft("narrow", 0.0, 60.0, 0.0, 1.0)])
        under = [s for s in raw if s[5] == tm.UNDER_KIND]
        self.assertTrue(under)                                                      # the lead-in has a border under it too: black,
        for s in under:                                                             # (the wing-tip lines have theirs: the same 0.3 m)
            self.assertEqual((round(s[4], 3), s[6]), (round(tm.LEAD_W_M + 2 * tm.BORDER_M, 3), "black"))   # 0.15 m wider each side
        self.assertEqual(tm.CENTRE_W_M, tm.LEAD_W_M)
        self.assertEqual(len(under), len([s for s in raw if s[5] == tm.CENTRE_KIND and s[4] == tm.LEAD_W_M])
                         + len([s for s in raw if s[5] == tm.STAND_KIND]))

    def test_the_equipment_restraint_legs_are_red_and_stay_inside_the_wingspan(self):
        f = feats(self.LANE)
        c = craft("narrow", 0.0, 60.0, 0.0, 1.0)
        raw = [s for s in tm.strokes([f], [c]) if s[5] == tm.RESTRAINT_KIND]
        self.assertEqual(len(raw), 2)
        self.assertTrue(all(s[6] == "red" for s in raw))
        red = [metres(s) for s in raw]
        xs = np.array([x for m in red for x in (m[0], m[2])])
        self.assertLess(xs.max(), self.NARROW[1] / 2.0)                  # (inside the wings: never into the next stand)
        self.assertGreater(xs.max(), 0.0)
        self.assertLess(xs.min(), 0.0)

    def test_an_empty_valid_stand_gets_a_lead_in(self):
        import test_parkedaircraft as tpa
        f = tpa.feats(tpa.aerodrome(), tpa.gate(0.0), (tpa.line((-200, 60), (200, 60)), "taxilane", {}), occupied=False)
        bld = tpa.buildings(tpa.TERMINAL)
        self.assertEqual(pa.stands(f, bld), [])                         # (the occupancy hash leaves it empty: no aircraft)
        got = pa.lead_stands([0], lambda k: f, lambda k: bld)
        self.assertEqual([c.mode for c in got], ["gate"])               # ... but the stand is valid
        c = got[0]
        e, n, _ = FRAME.geodetic_to_enu(np.array([c.lat]), np.array([c.lon]), 0.0)
        nose = np.array([float(e[0]), float(n[0])]) + np.array([c.ne, c.nn]) * pa.SPECS[c.kind][0] / 2.0
        base = tm.strokes([f])
        lead = [metres(s) for s in tm.strokes([f], got)[len(base):] if s[5] == tm.CENTRE_KIND]
        self.assertGreater(len(lead), 3)                                # (a curve, a run and the stop bar)
        self.assertTrue(any(np.hypot(m[2] - nose[0], m[3] - nose[1]) < 0.1 for m in lead), "the line reaches the nose")
        self.assertTrue(any(abs(m[1] - 60.0) < 0.1 for m in lead), "... from the lane")


class LeadSpine(Unpaved):
    """A row of stands along a wall whose axes reach no lane: one taxilane-like spine behind the tails joined to the network at one
    end, each stand's lens off it. Where a lane is within reach of the axes the stands use it and no spine is made."""
    NARROW = pa.SPECS["narrow"]
    XS = (-88.0, -44.0, 0.0, 44.0, 88.0)
    NOSE_Y = 5.0

    def stands(self, xs=XS, kind="narrow"):
        # (the noses at y = 5 facing south, a facade at y = 0: the centres half a length north)
        return [craft(kind, x, self.NOSE_Y + pa.SPECS[kind][0] / 2.0, 0.0, -1.0) for x in xs]

    def painted(self, f, stands):
        base = tm.strokes([f])
        got = [metres(s) for s in tm.strokes([f], stands)[len(base):] if s[5] == tm.CENTRE_KIND]
        return got

    @staticmethod
    def back(kinds=("narrow",)):
        """How far behind the tails the spine runs: the largest half wingspan of the row and SPINE_CLEAR_M (ICAO's taxilane clearance)."""
        return max(pa.SPECS[k][1] for k in kinds) / 2.0 + tm.SPINE_CLEAR_M

    def spine_y(self):
        return self.NOSE_Y + self.NARROW[0] + self.back()                      # half a span and 7.5 m behind the tails

    def test_a_row_with_no_lane_in_reach_has_one_spine_behind_its_tails_joined_to_a_lane_at_one_end(self):
        lane = taxiway((-250, 150), (250, 150), cls="taxilane")                # (past the reach: 2.5 spans behind a tail)
        got = self.painted(feats(lane), self.stands())
        spine = [g for g in got if abs(g[1] - self.spine_y()) < 0.05 and abs(g[3] - self.spine_y()) < 0.05]
        self.assertTrue(spine)
        xs = [x for g in spine for x in (g[0], g[2])]
        self.assertGreater(max(xs) - min(xs), 100.0)                            # one line along the row ...
        touching = [g for g in got if abs(g[1] - 150.0) < 0.05 or abs(g[3] - 150.0) < 0.05]
        self.assertEqual(len(touching), 1, "... joined to the mapped lane at ONE point (an end), not at both")

    def test_a_lane_within_reach_of_the_axes_is_used_and_no_spine_is_made(self):
        got = self.painted(feats(taxiway((-250, 100), (250, 100), cls="taxilane")), self.stands())      # (58 m behind the tails)
        self.assertFalse([g for g in got if abs(g[1] - self.spine_y()) < 0.3 and abs(g[3] - self.spine_y()) < 0.3 and abs(g[0] - g[2]) > 1.0])
        self.assertTrue(any(abs(g[1] - 100.0) < 0.05 for g in got), "the lenses leave the lane")

    def test_the_spine_joins_the_lane_tangentially_by_a_curve_and_not_when_it_is_over_300_m_away(self):
        got = self.painted(feats(taxiway((-250, 150), (250, 150), cls="taxilane")), self.stands())
        near = [g for g in got if abs(g[1] - 150.0) < 0.05 or abs(g[3] - 150.0) < 0.05][0]
        d = np.array([near[2] - near[0], near[3] - near[1]])
        self.assertGreater(abs(d[0]) / np.hypot(*d), 0.9)                       # leaves the lane along it (the lane runs east - west)
        curve = [g for g in got if max(g[1], g[3]) > self.spine_y() + 3.0 and abs(g[1] - g[3]) > 1e-3]
        self.assertGreater(len(curve), 4)                                       # a curve of several chords, not one straight stub
        far = self.painted(feats(taxiway((-250, 450), (250, 450), cls="taxilane")), self.stands())
        self.assertFalse([g for g in far if max(g[1], g[3]) > self.spine_y() + 0.1], "a lane 380 m off: no connector (CONN_MAX_M)")
        self.assertTrue([g for g in far if abs(g[1] - self.spine_y()) < 0.05], "... the spine and its lenses stand alone")

    def test_a_mapped_lane_along_the_tails_is_the_spine(self):
        lane = taxiway((-250, 52.0), (250, 52.0), cls="taxilane")              # 0.6 m off the spine's line
        got = self.painted(feats(lane), self.stands())
        spine_y = self.NOSE_Y + self.NARROW[0] + self.back()
        self.assertFalse([g for g in got if abs(g[1] - spine_y) < 3.0 and abs(g[3] - spine_y) < 3.0 and abs(g[0] - g[2]) > 10.0],
                         "no spine of its own is painted")
        bars = [p for p, colour, layer in filled(feats(lane), self.stands()) if colour == "yellow" and layer == "paint"
                and abs(np.ptp(p[:, 0]) - tm.STOP_BAR_M) < 0.01 and abs(p[:, 1].mean() - self.NOSE_Y) < 0.01]
        self.assertEqual(len(bars), len(self.XS))                               # (the stop bars at the noses)
        for sx in self.XS:
            self.assertTrue(any(abs(g[2] - sx) < 0.01 and abs(g[3] - self.NOSE_Y) < 0.01 and abs(g[0] - sx) < 0.01 for g in got), sx)


class StandFont(unittest.TestCase):
    """The lettering (standmarks): DejaVu Sans Bold from a texture sheet, a quad with UVs for each character."""
    H = tm.NUM_TEXT

    def quads(self, text, up=(0.0, 1.0)):
        return standmarks.text_quads(text, np.zeros(2), np.array(up), self.H)

    def test_every_character_is_one_undistorted_quad_with_its_cell_of_the_sheet(self):
        table = standmarks.font()
        for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-/.":
            got = self.quads(ch)
            self.assertEqual(len(got), 1, ch)
            corners, uv = got[0]
            self.assertEqual((corners.shape, uv.shape), ((4, 2), (4, 2)))
            self.assertTrue(((uv >= 0.0) & (uv <= 1.0)).all(), ch)
            w, h = float(np.ptp(corners[:, 0])), float(np.ptp(corners[:, 1]))
            self.assertAlmostEqual(w / h, np.ptp(uv[:, 0]) / np.ptp(uv[:, 1]), 3, ch)            # (the sheet is square: no stretch)
            self.assertAlmostEqual(h, (table[ch]["box"][3] - table[ch]["box"][1]) * self.H, 5, ch)
        self.assertTrue(standmarks.supported("A\u2197"))
        self.assertFalse(standmarks.supported("A@"))
        self.assertEqual(self.quads("a?"), [])

    def test_the_corners_and_uvs_run_top_left_top_right_bottom_right_bottom_left_and_the_text_is_upright(self):
        corners, uv = self.quads("H")[0]
        self.assertTrue(corners[0, 1] > corners[3, 1] and corners[1, 1] > corners[2, 1])         # (the top corners over the bottom ones)
        self.assertTrue(corners[0, 0] < corners[1, 0] and corners[3, 0] < corners[2, 0])         # (left to right)
        self.assertTrue(uv[0, 1] > uv[3, 1] and uv[0, 0] < uv[1, 0])                             # (the sheet's V runs up: the top has the larger V)
        np.testing.assert_allclose(uv[0], [uv[3, 0], uv[1, 1]])

    def test_text_is_centred_and_rotates_with_up(self):
        h = self.H
        pts = np.concatenate([q for q, _uv in self.quads("H13")])
        self.assertAlmostEqual(float(pts[:, 0].max() + pts[:, 0].min()), 0.0, 6)                  # (centred on the point)
        self.assertAlmostEqual(float(pts[:, 0].max() - pts[:, 0].min()), standmarks.text_width("H13", h), 6)
        self.assertAlmostEqual(float(pts[:, 1].max() + pts[:, 1].min()), 0.0, 3)                  # (the capitals' middle on it)
        up = np.array([-0.6, 0.8])
        right = np.array([up[1], -up[0]])
        q = np.concatenate([q for q, _uv in self.quads("H13", up=up)])
        self.assertAlmostEqual(float((q @ right).max()), standmarks.text_width("H13", h) / 2.0, 6)   # (left to right across up)
        self.assertAlmostEqual(float((q @ up).max() + (q @ up).min()), 0.0, 3)
        xs = [float(c.mean(axis=0) @ right) for c, _uv in self.quads("H13", up=up)]
        self.assertEqual(xs, sorted(xs))                                                         # (the characters in reading order)

    def test_the_stand_number_box_is_about_5_by_2_m(self):
        w, hh = standmarks.text_width("B13", self.H) + 2 * tm.NUM_MARGIN[0], self.H + 2 * tm.NUM_MARGIN[1]
        self.assertTrue(4.5 < w < 6.0, w)
        self.assertAlmostEqual(hh, 2.0, 2)


def site_of_metres(walls, bridges=(), blocks=()):
    """A taxiwaymarks.Site from rings (k, 2) and (point, unit direction) in metres of the test frame."""
    def ll(xy):
        la, lo, _ = FRAME.enu_to_geodetic(np.asarray(xy[:, 0], np.float64), np.asarray(xy[:, 1], np.float64), 0.0)
        return np.asarray(la), np.asarray(lo)
    br = []
    for pt, d in bridges:
        la, lo = ll(np.array([pt]))
        br.append((float(la[0]), float(lo[0]), float(d[0]), float(d[1])))
    return tm.Site([ll(np.array(w)) for w in walls], br, [ll(np.array(b)) for b in blocks])


class ServiceRoad(unittest.TestCase):
    """The service road along the terminal's wall, between the noses and the building, and what is on it."""
    NARROW = pa.SPECS["narrow"]
    XS = (-88.0, -44.0, 0.0, 44.0, 88.0)

    def stands(self, nose_y=5.0):
        return [craft("narrow", x, nose_y + self.NARROW[0] / 2.0, 0.0, -1.0) for x in self.XS]

    @staticmethod
    def wall(y):
        return [(-300.0, y), (300.0, y), (300.0, y - 25.0), (-300.0, y - 25.0)]

    def paint(self, wall_y, nose_y=5.0):
        f = feats(taxiway((-250, 130), (250, 130), cls="taxilane"))
        site = site_of_metres([self.wall(wall_y)])
        stands = self.stands(nose_y)
        raw = tm.strokes([f], stands, site)
        return [metres(s) for s in raw if s[5] == tm.ROAD_KIND], filled(f, stands, site), raw

    def test_two_lanes_white_edges_and_a_dashed_centre_7_m_wide_6_to_9_5_m_from_the_wall(self):
        edges, filled_, raw = self.paint(-15.0)                         # (the wall 15 m south of the facade: the nose 20 m from it)
        self.assertTrue(all(s[6] == "white" and s[4] == tm.ROAD_LINE_W_M for s in raw if s[5] == tm.ROAD_KIND))
        self.assertGreaterEqual(len(edges), 2)                       # (both edges: straight, so one stroke each)
        ys = sorted({round(float(np.mean([m[1], m[3]])), 1) for m in edges if abs(m[0] - m[2]) > 3.0})
        self.assertAlmostEqual(max(ys) - min(ys), tm.ROAD_W_M, 1)     # two solid edges 7 m apart
        self.assertAlmostEqual((max(ys) + min(ys)) / 2.0, -15.0 + tm.ROAD_WALL_M, 1)   # the centre 9.5 m from the wall
        dashes = [p for p, colour, layer in filled_ if colour == "white" and layer == "paint" and 0.05 < np.ptp(p[:, 1]) < 0.3
                  and 2.0 < np.ptp(p[:, 0]) < 3.5]
        self.assertGreater(len(dashes), 10)                           # a dashed centre
        for p in dashes:
            self.assertAlmostEqual(p[:, 1].mean(), -15.0 + tm.ROAD_WALL_M, 1)

    def test_none_where_the_nose_is_under_9_m_from_the_wall(self):
        edges, filled_, raw = self.paint(-3.0)                          # (nose 8 m from the wall)
        self.assertEqual(edges, [])
        self.assertFalse([p for p, colour, layer in filled_ if colour == "white"])

    def test_give_way_triangles_each_side_of_a_lead_in_that_crosses(self):
        items = [(np.array([x, 5.0]), np.array([0.0, -1.0]), self.NARROW[0], self.NARROW[1]) for x in self.XS]
        walls = [np.array(self.wall(-15.0), np.float64)]
        lead = np.array([[10.0, 30.0], [10.0, -20.0]])                # a line across the road at x = 10
        ribbons, polys = tm._service_roads(items, [(list(range(5)), "row")], walls, [lead])
        tri = [x[0] for x in polys if len(x[0]) == 3]
        self.assertEqual(len(tri), 2 * tm.GIVEWAY_N)                  # a row each side of the crossing
        c = np.array([p.mean(axis=0) for p in tri])
        self.assertEqual(int((c[:, 0] > 10.0).sum()), tm.GIVEWAY_N)
        self.assertEqual(int((c[:, 0] < 10.0).sum()), tm.GIVEWAY_N)
        self.assertTrue(all(abs(abs(x - 10.0) - (tm.LEAD_W_M / 2 + tm.GIVEWAY_AT_M + tm.GIVEWAY_H_M / 3)) < 0.2 for x in c[:, 0]))
        self.assertLess(np.ptp(c[:, 1]), tm.ROAD_W_M)                 # across the road's width
        none = tm._service_roads(items, [(list(range(5)), "row")], walls, [np.array([[10.0, 30.0], [10.0, 20.0]])])[1]
        self.assertFalse([x for x in none if len(x[0]) == 3])        # (a line that does not reach it: none)

    def test_equipment_boxes_between_stands_on_the_wall_side(self):
        items = [(np.array([x, 5.0]), np.array([0.0, -1.0]), self.NARROW[0], self.NARROW[1]) for x in self.XS]
        walls = [np.array(self.wall(-15.0), np.float64)]
        ribbons, polys = tm._service_roads(items, [(list(range(5)), "row")], walls, [])
        thin = [p for p, colour, layer, *_ in polys if colour == "white" and 0.1 < min(np.ptp(p[:, 0]), np.ptp(p[:, 1])) < 0.2
                and max(np.ptp(p[:, 0]), np.ptp(p[:, 1])) > 2.5]       # (the long outline bars of a box, not the dashes)
        self.assertGreater(len(thin), 8)
        pts = np.concatenate([p for p in thin if np.ptp(p[:, 1]) > 0.25 or np.ptp(p[:, 0]) > 5.0])
        centre = -15.0 + tm.ROAD_WALL_M
        self.assertGreater(pts[:, 1].min(), centre - tm.ROAD_W_M / 2.0 - 4.0)    # (south of the road: the wall side)
        self.assertLess(pts[:, 1].max(), centre - tm.ROAD_W_M / 2.0 + 1e-6)
        self.assertGreaterEqual(pts[:, 1].min(), -15.0 + 0.3)                     # (clear of the wall)
        xs = pts[:, 0]
        for ax in self.XS:
            self.assertFalse(((xs > ax - tm.EQUIP_CLEAR_M + 0.4) & (xs < ax + tm.EQUIP_CLEAR_M - 0.4)).any(), ax)   # (between stands)
        none = tm._service_roads(items, [(list(range(5)), "row")], [np.array(self.wall(0.0), np.float64)], [])[1]
        self.assertFalse([p for p in none if np.ptp(p[0][:, 0]) > 5.0 and 0.1 < np.ptp(p[0][:, 1]) < 0.2 and p[1] == "white"
                          and p[0][:, 1].max() < -4.0])               # (a wall too close: no room for a box beside the road)


class BridgeBox(unittest.TestCase):
    def test_a_red_hatched_box_under_each_mapped_jet_bridge_at_a_stand(self):
        f = feats(taxiway((-120, 0), (100, 0), cls="taxilane"))
        st = [craft("narrow", 0.0, 40.0, 0.0, 1.0)]
        nose = 40.0 + pa.SPECS["narrow"][0] / 2.0
        bridge = (np.array([0.0, nose + 8.0]), np.array([0.0, 1.0]))
        site = site_of_metres([], [bridge])
        red = [p for p, colour, layer in filled(f, st, site) if colour == "red" and layer == "red"]
        self.assertGreater(len(red), 8)                                       # the hatching and the outline
        pts = np.concatenate(red)
        w, h = tm.BRIDGE_BOX_M
        self.assertAlmostEqual(np.ptp(pts[:, 0]), w, 1)
        self.assertAlmostEqual(np.ptp(pts[:, 1]), h, 1)
        self.assertAlmostEqual(pts[:, 1].mean(), bridge[0][1] + tm.BRIDGE_BOX_AT_M, delta=0.6)
        diag = [p for p in red if np.ptp(p[:, 0]) > 0.5 and np.ptp(p[:, 1]) > 0.5]
        self.assertGreater(len(diag), 5)                                      # (diagonal strips: not boxes along the axes)
        far = site_of_metres([], [(np.array([0.0, nose + 300.0]), np.array([0.0, 1.0]))])
        self.assertFalse([p for p, colour, layer in filled(f, st, far) if colour == "red"])   # (a bridge with no stand by it: none)
        self.assertFalse([p for p, colour, layer in filled(f, st) if colour == "red"])


class TaxiEdges(unittest.TestCase):
    """Double yellow edge lines along the taxiways' pads, outside the aprons."""
    HALF = aeroways.TAXIWAY_M / 2.0

    def edges(self, *rows):
        return [metres(s) for s in tm.strokes([feats(*rows)]) if s[5] == tm.EDGE_KIND]

    def test_two_015_m_lines_015_m_apart_inside_each_edge(self):
        got = tm.strokes([feats(taxiway((0, 0), (300, 0), tags={"width": "15"}))])
        edge = [s for s in got if s[5] == tm.EDGE_KIND]
        self.assertTrue(edge)
        self.assertTrue(all(s[4] == tm.EDGE_W_M and s[6] == "yellow" for s in edge))
        m = [metres(s) for s in edge]
        ys = sorted({round(abs(float(np.mean([a[1], a[3]]))), 3) for a in m if abs(a[0] - a[2]) > 100.0 and abs(a[1] - a[3]) < 0.01 and abs(a[1]) > 5.0})
        want = sorted(round(self.HALF - tm.EDGE_IN_M - k * (tm.EDGE_W_M + tm.EDGE_GAP_M), 3) for k in (0, 1))
        self.assertEqual(len(ys), 2)                                          # (two lines each side)
        self.assertAlmostEqual(ys[0], want[0], delta=0.05)
        self.assertAlmostEqual(ys[1], want[1], delta=0.05)
        self.assertAlmostEqual(abs(ys[1] - ys[0]) - tm.EDGE_W_M, tm.EDGE_GAP_M, 2)    # 0.15 m lines 0.15 m apart
        for y in (-1.0, 1.0):
            self.assertTrue(any(abs(a[1] * y - want[1]) < 0.05 and abs(a[0] - a[2]) > 0.5 for a in m))

    def test_none_inside_an_apron_a_runway_or_another_pad(self):
        apron = (poly_rows((100, -50), (200, -50), (200, 50), (100, 50)), "apron", "airport", {}, None)
        lane = taxiway((0, 0), (300, 0), tags={"width": "15"})
        m = self.edges(lane, apron)
        mid = [a for a in m if 105 < a[0] < 195 and 105 < a[2] < 195 and abs(a[1] - a[3]) < 0.1 and abs(a[1]) < 8]
        self.assertEqual(mid, [])                                             # (the apron's paving: no edge lines on it)
        self.assertTrue([a for a in m if a[0] < 90 and a[2] < 95])            # ... and they are still there outside it
        rw = self.edges(lane, runway((150, -200), (150, 200)))
        self.assertEqual([a for a in rw if abs(a[0] - 150) < 20 and abs(a[2] - 150) < 20 and abs(a[1]) < 9 and abs(a[3]) < 9], [])
        cross = self.edges(lane, taxiway((150, -200), (150, 200), tags={"width": "15"}))
        self.assertEqual([a for a in cross if 143 < a[0] < 157 and 143 < a[2] < 157 and abs(a[1]) < 6 and abs(a[3]) < 6], [])


class TaxiEdgesOuter(unittest.TestCase):
    """The edge lines run along the OUTER boundary of the paving only: not where one pad's edge lies in another, a break under
    EDGE_JOIN_M run across, a piece under EDGE_MIN_M none."""
    HALF = aeroways.TAXIWAY_M / 2.0

    def edges(self, *rows):
        return [metres(s) for s in tm.strokes([feats(*rows)]) if s[5] == tm.EDGE_KIND]

    def covered(self, m, y, lo, hi, step=0.25):
        """Is every point of x in [lo, hi] on a horizontal stroke at height y (within 0.2)?"""
        xs = [(min(a[0], a[2]), max(a[0], a[2])) for a in m if abs(a[1] - y) < 0.2 and abs(a[3] - y) < 0.2]
        return all(any(a - 0.3 <= x <= b + 0.3 for a, b in xs) for x in np.arange(lo, hi, step))

    def test_a_break_under_5_m_is_run_across_the_line_is_continuous(self):
        main = taxiway((0, 0), (300, 0), tags={"width": "15"})
        y = self.HALF - tm.EDGE_IN_M
        narrow = self.edges(main, taxiway((150, -40), (150, 40), tags={"width": "3"}))      # a 3 m way across: ~4 m of the edge lies in its pad
        self.assertTrue(self.covered(narrow, y, 20.0, 280.0))
        wide = self.edges(main, taxiway((150, -40), (150, 40), tags={"width": "8"}))        # an 8 m one: a real break
        self.assertFalse(self.covered(wide, y, 20.0, 280.0))
        self.assertTrue(self.covered(wide, y, 20.0, 140.0))

    def test_a_piece_under_15_m_is_none(self):
        self.assertGreaterEqual(tm.EDGE_MIN_M, 15.0)
        self.assertEqual(self.edges(taxiway((0, 0), (4, 0), tags={"width": "3"})), [])           # (a pad whose whole outline is 14 m)
        self.assertTrue(self.edges(taxiway((0, 0), (40, 0), tags={"width": "3"})))               # (a longer one is painted)
        main = taxiway((0, 0), (300, 0), tags={"width": "15"})
        m = self.edges(main, taxiway((150, 0), (150, 14), tags={"width": "15"}))                  # (the line round a stub is part of the long one)
        self.assertTrue([a for a in m if 140 < a[0] < 160 and 140 < a[2] < 160 and (abs(a[1]) > 9 or abs(a[3]) > 9)])

    def test_nothing_in_the_middle_of_a_crossing(self):
        m = self.edges(taxiway((0, 0), (300, 0), tags={"width": "15"}), taxiway((150, -150), (150, 150), tags={"width": "15"}),
                       taxiway((0, 90), (300, 90), tags={"width": "15"}))
        inside = [a for a in m if (abs(a[0] - 150) < self.HALF - 0.6 and abs(a[2] - 150) < self.HALF - 0.6 and abs(a[1]) < self.HALF - 0.6
                                   and abs(a[3]) < self.HALF - 0.6) or (abs(a[0] - 150) < self.HALF - 0.6 and abs(a[2] - 150) < self.HALF - 0.6
                                                                          and abs(a[1] - 90) < self.HALF - 0.6 and abs(a[3] - 90) < self.HALF - 0.6)]
        self.assertEqual(inside, [])                                          # (no chevron, no stray piece inside an intersection)
        self.assertTrue([a for a in m if a[0] < 100 and a[2] < 100])          # ... the edges outside it are there
        self.assertTrue([a for a in m if abs(a[1] - self.HALF) < 1.0 or abs(a[3] - self.HALF) < 1.0 or abs(a[1] + self.HALF) < 1.0])


class NoseMarksClearOfBuildings(unittest.TestCase):
    """No stop bar, type label or stand number lies under a building or a jet bridge: the number and the labels move to the side that is
    clear (or behind the ladder), and what cannot be moved clear is clipped away."""
    NARROW = pa.SPECS["narrow"]

    def setUp(self):
        patcher = mock.patch.object(tm, "_edge_lines", lambda sc, feats_list: [])
        patcher.start()
        self.addCleanup(patcher.stop)

    def paint(self, blocks):
        f = feats(taxiway((-150, 60), (150, 60), cls="taxilane"))
        st = craft("narrow", 0.0, 5.0 + self.NARROW[0] / 2.0, 0.0, -1.0)._replace(ref="B13")     # (the nose at y = 5, toward the terminal: -y)
        site = site_of_metres([], blocks=blocks)
        return [(p, c, l) for p, c, l in filled(f, [st], site) if c in ("yellow", "font_black", "black")]

    @staticmethod
    def overlaps(poly, rect):
        x0, y0, x1, y1 = rect
        return poly[:, 0].max() > x0 and poly[:, 0].min() < x1 and poly[:, 1].max() > y0 and poly[:, 1].min() < y1

    def test_nothing_changes_where_nothing_is_in_the_way(self):
        got = self.paint([[(40.0, 100.0), (60.0, 100.0), (60.0, 120.0), (40.0, 120.0)]])
        free = self.paint([])
        self.assertEqual(len(got), len(free))

    def test_the_number_moves_to_the_clear_side_a_bridge_by_the_nose(self):
        rect = (1.0, -8.0, 12.0, 3.6)                                          # a bridge along the nose's left, over the number's box
        blocks = [[(rect[0], rect[1]), (rect[2], rect[1]), (rect[2], rect[3]), (rect[0], rect[3])]]
        free = self.paint([])
        got = self.paint(blocks)
        self.assertTrue([p for p, _c, _l in free if self.overlaps(p, rect)], "(it was under the bridge)")
        self.assertEqual([p for p, _c, _l in got if self.overlaps(p, rect)], [])
        self.assertTrue([p for p, c, l in got if l == "glyph"], "the number is still painted")
        self.assertGreaterEqual(len([1 for p, c, l in got if l == "glyph"]), len([1 for p, c, l in free if l == "glyph"]))

    def test_the_number_goes_behind_the_ladder_where_both_sides_are_in_the_way_and_is_clipped_where_nothing_is_clear(self):
        rect = (-14.0, -9.0, 14.0, 3.8)
        got = self.paint([[(rect[0], rect[1]), (rect[2], rect[1]), (rect[2], rect[3]), (rect[0], rect[3])]])
        self.assertEqual([p for p, _c, _l in got if self.overlaps(p, rect)], [])
        glyph = [p for p, c, l in got if l == "glyph"]
        self.assertTrue(glyph)
        self.assertGreater(max(float(p[:, 1].max()) for p in glyph), 10.0)    # (behind the ladder: tail-ward, +y)
        all_ = (-60.0, -60.0, 60.0, 60.0)
        blocked = self.paint([[(all_[0], all_[1]), (all_[2], all_[1]), (all_[2], all_[3]), (all_[0], all_[3])]])
        self.assertEqual([p for p, _c, _l in blocked if self.overlaps(p, all_)], [])


class GlyphOverBox(unittest.TestCase):
    """The black lettering of the yellow boxes (the stand number, the type labels) is drawn over its box: its own material, listed
    after the yellow's, in a layer above the paint (in game the boxes were plain yellow: 0.6 mm over them, in the black border's
    material, drawn before the yellow)."""

    def setUp(self):
        patcher = mock.patch.object(tm, "_edge_lines", lambda sc, feats_list: [])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_layers_and_materials_put_the_glyphs_over_the_boxes(self):
        import roadmesh
        self.assertEqual(tm.LAYER_OF["glyph"], "glyph")
        lift = roadmesh.LIFT
        self.assertGreaterEqual(lift["glyph"] - lift["mark"], 0.001)             # (at least a millimetre over the paint)
        self.assertGreater(lift["mark"], lift["gutter"])
        self.assertGreater(lift["gutter"], lift["edge"])                          # (the borders under all)
        look = airportgrade.MARK_LOOK
        fonts = [look[k]["m"] for k in ("font_black", "font_yellow", "font_white")]
        self.assertEqual(len(set(fonts)), 3)
        for k in fonts:
            self.assertNotIn(k, (look["yellow"]["m"], look["black"]["m"], look["text"]["m"]))
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "mod", "beamng_earth", "levels", "beamng_earth",
                            "art", "runway", "main.materials.json")
        with open(path, encoding="utf-8") as fh:
            mats = json.load(fh)
        for k, colour in (("font_black", "black"), ("font_yellow", "yellow"), ("font_white", "white")):
            stage = mats[look[k]["m"]]["Stages"][0]                                # (the mod has them: the paint's colour under the sheet's opacity)
            self.assertTrue(stage["baseColorMap"].endswith(f"t_paint_{colour}_b.color.dds"), stage)
            self.assertTrue(stage["opacityMap"].endswith("bne_paint_font_o.data.dds"), stage)
            self.assertFalse(mats[look[k]["m"]]["translucentZWrite"])
        self.assertIn(look["text"]["m"], mats)

    def test_the_sheet_is_a_power_of_two_in_the_texture_manifest_and_the_table_is_inside_it(self):
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "mod")
        with open(os.path.join(root, "texture_sources", "textures.json"), encoding="utf-8") as fh:
            entry = json.load(fh)["textures"]["levels/beamng_earth/art/runway/bne_paint_font_o.data.dds"]
        self.assertEqual(entry["format"], "BC4")                                   # (one channel: the opacity)
        from PIL import Image
        with Image.open(os.path.join(root, "texture_sources", *entry["source"].split("/"))) as im:
            side = im.size[0]
            self.assertEqual(im.size, (side, side))
            self.assertEqual(side & (side - 1), 0)
            solid = standmarks._table()["solid"]
            self.assertEqual(im.convert("L").getpixel((int(solid[0] * side), int((1.0 - solid[1]) * side))), 255)    # (the solid cell is solid)
        for ch, g in standmarks.font().items():
            u0, v0, u1, v1 = g["uv"]
            self.assertTrue(0.0 <= u0 < u1 <= 1.0 and 0.0 <= v0 < v1 <= 1.0, ch)

    def test_polygons_glyphs_lie_inside_a_yellow_box(self):
        f = feats(taxiway((-120, 0), (100, 0), cls="taxilane"))
        c = craft("narrow", 0.0, 60.0, 0.0, 1.0)._replace(ref="B13")
        got = filled(f, [c])
        glyphs = [p for p, colour, layer in got if layer == "glyph"]
        self.assertTrue(glyphs)
        self.assertEqual({colour for _p, colour, layer in got if layer == "glyph"}, {"font_black"})
        boxes = [p for p, colour, layer in got if colour == "yellow" and layer == "paint"]
        for g in glyphs:
            self.assertTrue(any(g[:, 0].min() >= b[:, 0].min() - 1e-6 and g[:, 0].max() <= b[:, 0].max() + 1e-6
                                and g[:, 1].min() >= b[:, 1].min() - 1e-6 and g[:, 1].max() <= b[:, 1].max() + 1e-6 for b in boxes))
    def test_a_tiles_chunks_list_the_lettering_after_the_yellow_and_higher(self):
        import roadmesh
        f = feats(taxiway((-120, 0), (300, 0), cls="taxilane"))
        c = craft("narrow", 100.0, 60.0, 0.0, 1.0)._replace(ref="B13")
        side = 256.0
        bbox = lambda tx, ty: (tx * side, ty * side, (tx + 1) * side, (ty + 1) * side)
        chunks = tm.tile_chunks([f], FRAME, 0, 0, lambda la, lo: np.full(len(la), 7.0), bbox, [c])
        text, yellow = airportgrade.MARK_LOOK["font_black"]["m"], airportgrade.MARK_LOOK["yellow"]["m"]
        order = []
        for ch in chunks:
            if ch["m"] not in order:
                order.append(ch["m"])
        self.assertIn(text, order)
        self.assertIn(yellow, order)
        self.assertGreater(order.index(text), order.index(yellow))                 # (the mesh draws the materials in this order)
        glyph = [ch for ch in chunks if ch["m"] == text]
        box = [ch for ch in chunks if ch["m"] == yellow and ch.get("poly")]
        self.assertTrue(glyph and box)
        self.assertEqual({roadmesh.layer_of(ch) for ch in glyph}, {"glyph"})
        self.assertEqual({roadmesh.layer_of(ch) for ch in box}, {"apaint"})
        self.assertGreater(roadmesh.LIFT["glyph"], roadmesh.LIFT["apaint"])
        # (each layer 1.5 mm or more over the one under it: at 1 mm or less the black won the depth test in game)
        self.assertGreaterEqual(roadmesh.LIFT["apaint"] - roadmesh.LIFT["edge"], 0.0015 - 1e-9)
        self.assertGreaterEqual(roadmesh.LIFT["glyph"] - roadmesh.LIFT["apaint"], 0.0015 - 1e-9)
        # (the chunks of the polygons come in the layers' order: the border under, then the paint, then the lettering)
        layers = [ch["layer"] for ch in chunks if ch.get("poly")]
        self.assertEqual(layers, sorted(layers, key=lambda x: ["edge", "gutter", "apaint", "glyph"].index(x)))
        # (the paint writes no depth: the mesh's material order decides what is on top, whatever order the chunks of a tile
        # came in; the black borders, the red, the yellow and white, the lettering)
        po = roadmesh.PAINT_ORDER
        rank = [po[airportgrade.MARK_LOOK[k]["m"]] for k in ("black", "red", "yellow", "text")]
        self.assertEqual(rank, sorted(set(rank)))
        self.assertEqual(po[airportgrade.MARK_LOOK["white"]["m"]], po[yellow])


def circumradii(P, min_side=0.2):
    """The radius of the circle through each three consecutive points of the polyline P (k, 2) (inf for a straight run)."""
    out = []
    for a, b, c in zip(P[:-2], P[1:-1], P[2:]):
        ab, bc, ca = np.hypot(*(b - a)), np.hypot(*(c - b)), np.hypot(*(a - c))
        cr = abs((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]))
        if min(ab, bc) < min_side or cr < 1e-9:
            out.append(np.inf)
        else:
            out.append(ab * bc * ca / (2.0 * cr))
    return out


def turns(P):
    """The turn (degrees) at each interior point of the polyline P."""
    out = []
    for a, b, c in zip(P[:-2], P[1:-1], P[2:]):
        d1, d2 = b - a, c - b
        if np.hypot(*d1) < 1e-9 or np.hypot(*d2) < 1e-9:
            continue
        out.append(float(np.degrees(np.arccos(np.clip(d1 @ d2 / np.hypot(*d1) / np.hypot(*d2), -1, 1)))))
    return out


class Routing(Unpaved):
    """The painted yellow lead-ins, spines and connectors: no branch that leads nowhere, every curve a circular arc of at least 15 m
    (8 m for a light aircraft); no kink over 25 degrees."""

    def lines(self, lane, stands):
        """([polyline (k, 2) m of every painted lead-in, spine and connector], [nose (2,)], mapped lane segments)."""
        f = feats(*lane) if isinstance(lane, (list, tuple)) else feats(lane)
        sc = tm.scene([f])

        def to_frame(P):                                                            # (the scene's own frame: ours)
            la, lo, _ = sc.frame.enu_to_geodetic(P[:, 0], P[:, 1], np.zeros(len(P)))
            e, n, _ = FRAME.geodetic_to_enu(la, lo, np.zeros(len(P)))
            return np.stack([e, n], 1)
        out = []
        for la, lo, w, kind, _col in tm._lead_lines(sc, stands, None)[0]:
            if kind == tm.CENTRE_KIND and abs(w - tm.LEAD_W_M) < 1e-9:
                e, n, _ = FRAME.geodetic_to_enu(la, lo, np.zeros(len(la)))
                out.append(np.stack([e, n], 1))
        e0, n0, _ = FRAME.geodetic_to_enu(np.array([c.lat for c in stands]), np.array([c.lon for c in stands]), np.zeros(len(stands)))
        noses = [np.array([x, y]) + np.array([c.ne, c.nn]) / np.hypot(c.ne, c.nn) * pa.SPECS[c.kind][0] / 2.0
                 for c, x, y in zip(stands, e0, n0)]
        lanes = tm._lanes(sc)
        return out, noses, [(a, b) for a, b in zip(to_frame(lanes[0]), to_frame(lanes[0] + lanes[1] * lanes[2][:, None]))]

    @staticmethod
    def on(p, segs, tol=0.06):
        for a, b in segs:
            d = b - a
            t = min(max(float((p - a) @ d) / max(float(d @ d), 1e-12), 0.0), 1.0)
            if np.hypot(*(a + d * t - p)) <= tol:
                return True
        return False

    def dead_ends(self, lines, noses, lane_segs):
        """The ends of the painted polylines that are no stand's nose, on no other painted line and on no mapped lane."""
        segs = [(P[i], P[i + 1]) for P in lines for i in range(len(P) - 1)]
        out = []
        for k, P in enumerate(lines):
            own = [(Q[i], Q[i + 1]) for j, Q in enumerate(lines) if j != k for i in range(len(Q) - 1)]
            for p in (() if np.hypot(*(P[0] - P[-1])) < 0.01 else (P[0], P[-1])):      # (a ring closes on itself: no ends)
                if any(np.hypot(*(p - n)) < 0.6 for n in noses) or self.on(p, own) or self.on(p, lane_segs):
                    continue
                out.append(p)
        del segs
        return out

    ROW = tuple(float(x) for x in (-88, -44, 0, 44, 88))

    def row(self, kinds=("narrow",) * 5, xs=ROW, y=5.0):
        return [craft(k, x, y + pa.SPECS[k][0] / 2.0, 0.0, -1.0) for k, x in zip(kinds, xs)]

    def scenarios(self):
        """[(lane tuple, stands)]: rows with a lane far, near and mapped along them; rows of mixed kinds; a satellite ring; light stands."""
        lane = lambda y: taxiway((-250, y), (250, y), cls="taxilane")
        out = [((lane(100.0),), self.row()), ((lane(130.0),), self.row()), ((lane(70.0),), self.row(xs=(-22.0, 22.0, 66.0, 110.0, 154.0))),
               ((lane(150.0),), self.row()), ((lane(450.0),), self.row()), ((lane(150.0),), self.row(xs=(-22.0, 22.0))),
               ((lane(100.0),), self.row(("narrow", "wide", "narrow", "turboprop", "narrow"))),
               ((lane(52.0),), self.row()), ((lane(100.0),), self.row(xs=(-22.0, 22.0))),
               ((lane(100.0),), self.row(("light",) * 4, xs=(-40.0, -20.0, 0.0, 20.0)))]
        ring = []
        for k in range(6):
            a = np.radians(60.0 * k)
            ring.append(craft("narrow", float(np.cos(a) * 75.0), float(np.sin(a) * 75.0), float(-np.cos(a)), float(-np.sin(a))))
        out.append(((taxiway((-250, -250), (-200, -250), cls="taxilane"),), ring))
        out.append(((taxiway((-120, 0), (100, 0), cls="taxilane"),), [craft("narrow", 0.0, 40.0, 0.0, 1.0)]))
        out.append(((taxiway((-120, 0), (100, 0), cls="taxilane"),), [craft("turboprop", 30.0, 40.0, 0.0, 1.0)]))
        out.append(((taxiway((-120, 0), (100, 0), cls="taxilane"),), [craft("light", 30.0, 25.0, 0.0, 1.0)]))
        return out

    def test_no_branch_leads_nowhere(self):
        for lane, stands in self.scenarios():
            lines, noses, lane_segs = self.lines(lane, stands)
            self.assertEqual([p.round(1).tolist() for p in self.dead_ends(lines, noses, lane_segs)], [], (lane, len(stands)))

    def spine_to(self, lane_y, xs=ROW):
        """([the spine and its connector from its free end to the lane (k, 2)], [the starts of the lenses leaving it (2,)], the
        spine's y) of a row on a lane lane_y m north, out of the axes' reach."""
        lines, _noses, _segs = self.lines((taxiway((-250, lane_y), (250, lane_y), cls="taxilane"),), self.row(xs=xs))
        joined = [P for P in lines if abs(P[0][1] - lane_y) < 0.05 or abs(P[-1][1] - lane_y) < 0.05]
        self.assertEqual(len(joined), 1)
        P = joined[0] if abs(joined[0][-1][1] - lane_y) < 0.05 else joined[0][::-1]
        spine_y = 5.0 + pa.SPECS["narrow"][0] + LeadSpine.back()
        starts = [Q[0] for Q in lines if Q is not joined[0] and abs(Q[0][1] - spine_y) < 0.6 and Q[-1][1] < spine_y - 20.0]
        return P, starts, spine_y

    def test_a_spine_ends_at_its_first_lens_and_runs_on_to_its_lane(self):
        for lane_y, xs in ((150.0, self.ROW), (140.0, self.ROW), (150.0, (-22.0, 22.0))):
            P, starts, spine_y = self.spine_to(lane_y, xs)
            self.assertGreaterEqual(len(starts), len(xs), (lane_y, xs))         # (a lens off the spine for every stand)
            self.assertAlmostEqual(float(P[0][1]), spine_y, delta=0.05)         # (the free end on the spine ...)
            x0 = [float(s[0]) for s in starts]
            self.assertLess(min(abs(float(P[0][0]) - min(x0)), abs(float(P[0][0]) - max(x0))), 0.1, (lane_y, xs))    # ... at the first lens: no line past it

    def test_the_connector_leaves_the_lane_and_meets_the_spine_tangentially_in_arcs(self):
        P, _starts, _spine_y = self.spine_to(150.0)
        d = P[-1] - P[-2]
        self.assertGreater(abs(d[0]) / np.hypot(*d), 0.97)                          # along the lane (it runs east - west)
        self.assertLessEqual(max(turns(P)), 6.0)                                    # (arcs in small chords, none at the spine's end)
        self.assertGreater(min(circumradii(P)), tm.MIN_RADIUS_M * 0.97)

    def test_every_stand_has_its_lead_in_to_the_nose(self):
        for lane, stands in self.scenarios():
            lines, noses, _segs = self.lines(lane, stands)
            for n in noses:
                self.assertTrue(any(np.hypot(*(P[-1] - n)) < 0.05 or np.hypot(*(P[0] - n)) < 0.05 for P in lines), (lane, n))

    def test_curves_are_arcs_of_at_least_the_minimum_radius_and_no_corner_over_25_degrees(self):
        for lane, stands in self.scenarios():
            lines, _noses, _segs = self.lines(lane, stands)
            light = all(c.kind == "light" for c in stands)
            floor = (tm.MIN_RADIUS_LIGHT_M if light else tm.MIN_RADIUS_M) * 0.97
            for P in lines:
                for r in circumradii(P):
                    self.assertGreaterEqual(r, floor, (lane, P[0].round(1), P[-1].round(1)))
                for t in turns(P):
                    self.assertLess(t, 25.0, (lane, P[0].round(1)))

    def test_dubins_path_is_arcs_and_a_straight_between_two_poses(self):
        got = tm._dubins(np.array([0.0, 0.0]), np.array([1.0, 0.0]), np.array([50.0, 30.0]), np.array([1.0, 0.0]), 15.0)
        self.assertIsNotNone(got)
        P = tm._dubins_pts(got)
        self.assertAlmostEqual(float(P[0][0]), 0.0, 6)
        self.assertAlmostEqual(float(P[-1][0]), 50.0, 6)
        self.assertAlmostEqual(float(P[-1][1]), 30.0, 6)
        self.assertAlmostEqual(tm._length(P), got[0], delta=0.1)
        self.assertGreaterEqual(min(circumradii(P)), 14.5)
        self.assertLess(max(turns(P)), 6.0)
        self.assertIsNone(tm._dubins(np.array([0.0, 0.0]), np.array([1.0, 0.0]), np.array([-2.0, 0.5]), np.array([1.0, 0.0]), 15.0))   # (a loop: none)


class WingTipConflicts(unittest.TestCase):
    """Where stands are close the wing-tip lines would overlap or cross: clipped, or dropped."""
    N = pa.SPECS["narrow"]

    def stands(self, *xy):
        return [craft("narrow", x, y, 0.0, 1.0) for x, y in xy]

    def wings(self, stands):
        f = feats(taxiway((-250, -60), (250, -60), cls="taxilane"))
        got = [metres(s) for s in tm.strokes([f], stands) if s[5] == tm.STAND_KIND]
        return [(np.array(g[:2]), np.array(g[2:])) for g in got]

    @staticmethod
    def seg_dist(a, b, c, d):
        return tm._seg_dist(a, b, c, d)

    def test_apart_four_lines_and_close_the_shared_line_once(self):
        self.assertEqual(len(self.wings(self.stands((0.0, 40.0), (80.0, 40.0)))), 4)
        close = self.wings(self.stands((0.0, 40.0), (44.0, 40.0)))              # (their lines 2.2 m apart)
        self.assertEqual(len(close), 3)
        for i, (a, b) in enumerate(close):
            for c, d in close[i + 1:]:
                self.assertGreaterEqual(self.seg_dist(a, b, c, d), tm.WING_CLEAR_M - 1e-6)

    def test_a_line_is_clipped_where_it_would_run_under_an_adjacent_aircraft(self):
        s = self.stands((0.0, 40.0), (36.0, 80.0))                              # (the second's nose end 2.4 m beyond the first's line's end)
        nose0 = 40.0 + self.N[0] / 2.0
        got = self.wings(s)
        side = [(a, b) for a, b in got if abs(a[0] - (self.N[1] / 2.0 + tm.WING_LINE_OFF_M)) < 0.01]
        self.assertEqual(len(side), 1)
        top = max(side[0][0][1], side[0][1][1])
        env_low = 80.0 - self.N[0] / 2.0                                         # (the second's tail)
        self.assertAlmostEqual(top, env_low - tm.WING_CLEAR_M, delta=0.3)
        self.assertLess(top, nose0 - 0.3)                                        # (shorter than the 20 m line)
        for a, b in got:                                                         # (no line within 3 m of the other's aircraft)
            for k, (cx, cy) in enumerate(((0.0, 40.0), (36.0, 80.0))):
                for p in (a + (b - a) * q for q in np.linspace(0.0, 1.0, 40)):
                    dx = max(abs(p[0] - cx) - self.N[1] / 2.0, 0.0)
                    dy = max(abs(p[1] - cy) - self.N[0] / 2.0, 0.0)
                    own = abs(abs(p[0] - cx) - (self.N[1] / 2.0 + tm.WING_LINE_OFF_M)) < 0.01
                    if not own:
                        self.assertGreaterEqual(float(np.hypot(dx, dy)), tm.WING_CLEAR_M - 0.01)

    def test_a_line_under_its_neighbours_aircraft_is_none(self):
        got = self.wings(self.stands((0.0, 40.0), (36.0, 60.0)))                # (their envelopes overlap in length: the line is all under it)
        self.assertEqual([g for g in got if abs(g[0][0] - (self.N[1] / 2.0 + tm.WING_LINE_OFF_M)) < 0.01], [])

    def test_a_line_within_3_m_of_another_stands_lead_in_is_dropped(self):
        stands = [(craft("narrow", 0.0, 40.0, 0.0, 1.0), np.array([0.0, 1.0]), self.N[0], self.N[1], np.array([0.0, 40.0 + self.N[0] / 2.0])),
                  (craft("narrow", 80.0, 40.0, 0.0, 1.0), np.array([0.0, 1.0]), self.N[0], self.N[1], np.array([80.0, 40.0 + self.N[0] / 2.0]))]
        x = self.N[1] / 2.0 + tm.WING_LINE_OFF_M
        near = np.array([[x + 2.0, 30.0], [x + 2.0, 60.0]])                      # (2 m from the first's line)
        far = np.array([[x + 4.0, 30.0], [x + 4.0, 60.0]])
        for lead, count in ((None, 2), (near, 1), (far, 2), (np.array([[x - 5.0, 40.0], [x + 5.0, 40.0]]), 1)):    # (the last crosses it)
            got = tm._wing_lines(stands, [None, lead])
            self.assertEqual(len(got[0]), count, lead)


def poly_rows(*pts):
    return (mapgeom.POLYGON, [[lonlat([p[0] for p in pts], [p[1] for p in pts])]])


def polyline_samples(segs, step=0.5):
    """Points every `step` m along the segments (x0, y0, x1, y1): (n, 2)."""
    out = []
    for g in segs:
        a, b = np.array(g[:2]), np.array(g[2:])
        n = max(1, int(np.hypot(*(b - a)) / step))
        out += [a + (b - a) * k / n for k in range(n + 1)]
    return np.array(out).reshape(-1, 2)


def dist_to_segments(p, segs):
    best = 1e9
    for g in segs:
        a, b = np.array(g[:2]), np.array(g[2:])
        t = np.clip((p - a) @ (b - a) / max((b - a) @ (b - a), 1e-12), 0.0, 1.0)
        best = min(best, float(np.hypot(*(a + t * (b - a) - p))))
    return best


class Connectors(Unpaved):
    """Every painted yellow line ends on a mapped centreline (within 0.1 m), on the line it branches off, or at a stand's nose:
    none is cut short on the open apron."""
    NARROW = pa.SPECS["narrow"]
    XS = (-88.0, -44.0, 0.0, 44.0, 88.0)

    def check(self, f, stands, mapped, label):
        base = tm.strokes([f])
        got = [metres(s) for s in tm.strokes([f], stands)[len(base):] if s[5] == tm.CENTRE_KIND]
        noses = []
        for c in stands:
            e, n, _ = FRAME.geodetic_to_enu(np.array([c.lat]), np.array([c.lon]), np.zeros(1))
            u = np.array([c.ne, c.nn]) / np.hypot(c.ne, c.nn)
            noses.append(np.array([float(e[0]), float(n[0])]) + u * pa.SPECS[c.kind][0] / 2.0)
        deg = {}
        for g in got:
            for p in (g[:2], g[2:]):
                deg.setdefault((round(p[0] / 0.05), round(p[1] / 0.05)), [np.array(p), 0])[1] += 1
        for p, d in deg.values():
            if d != 1:
                continue
            ok = (any(float(np.hypot(*(p - n))) < 0.1 for n in noses) or dist_to_segments(p, mapped) < 0.1
                  or dist_to_segments(p, [g for g in got if not (np.allclose(g[:2], p) or np.allclose(g[2:], p))]) < 0.1)
            self.assertTrue(ok, (label, p.round(1).tolist()))
        return got

    def test_a_stand_far_from_its_lane_still_runs_from_the_lane_to_the_nose(self):
        lane = taxiway((-120, 0), (100, 0), cls="taxilane")
        got = self.check(feats(lane), [craft("narrow", 0.0, 75.0, 0.0, 1.0)], [(-120.0, 0.0, 100.0, 0.0)], "far stand")
        self.assertTrue(any(abs(g[1]) < 0.1 or abs(g[3]) < 0.1 for g in got), "it starts on the lane")

    def test_rows_and_a_ring_join_the_lanes_and_end_nowhere_else(self):
        for lane_y in (100.0, 130.0, 70.0):
            lane = taxiway((-250, lane_y), (250, lane_y), cls="taxilane")
            stands = [craft("narrow", x, 5.0 + self.NARROW[0] / 2.0, 0.0, -1.0) for x in self.XS]
            self.check(feats(lane), stands, [(-250.0, lane_y, 250.0, lane_y)], ("row", lane_y))
        ring = []
        for k in range(6):
            a = np.radians(60.0 * k)
            c = np.array([np.cos(a), np.sin(a)]) * 75.0
            ring.append(craft("narrow", float(c[0]), float(c[1]), float(-np.cos(a)), float(-np.sin(a))))
        self.check(feats(taxiway((-300, -150), (300, -150), cls="taxilane")), ring, [(-300.0, -150.0, 300.0, -150.0)], "ring")


class SpineClearance(unittest.TestCase):
    """The spine (standpaths) runs half the largest wingspan and SPINE_CLEAR_M behind the tails, in arcs of at least the radius."""
    NONE = (np.zeros((0, 2)), np.zeros((0, 2)), np.zeros(0), np.zeros(0, np.int64))

    def items(self, kinds, xs, y=5.0):
        return [(np.array([x, y]), np.array([0.0, -1.0]), pa.SPECS[k][0], pa.SPECS[k][1]) for k, x in zip(kinds, xs)]

    @staticmethod
    def envelope_distance(P, item):
        nose, u, length, span = item
        smp = np.concatenate([P[i] + (P[i + 1] - P[i]) * (np.arange(20) / 20.0)[:, None] for i in range(len(P) - 1)])
        v = smp - (nose - u * length / 2.0)
        along = np.abs(v[:, 0] * u[0] + v[:, 1] * u[1]) - length / 2.0
        across = np.abs(v[:, 0] * -u[1] + v[:, 1] * u[0]) - span / 2.0
        return float(np.hypot(np.maximum(along, 0.0), np.maximum(across, 0.0)).min())

    def spines(self, items, kinds):
        paint, _found, rows = standpaths.build(items, list(kinds), [], self.NONE)
        return [xy for xy, role, _rm in paint if role == "spine"], rows

    def test_a_row_of_wide_stands_has_its_spine_half_a_wide_span_and_7_5_m_behind_the_tails(self):
        kinds = ("narrow", "wide", "narrow", "wide")
        items = self.items(kinds, (-90.0, -30.0, 30.0, 90.0))
        spines, rows = self.spines(items, kinds)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(spines), 1)
        spine = spines[0]
        back = pa.SPECS["wide"][1] / 2.0 + tm.SPINE_CLEAR_M
        for it in items:
            self.assertGreaterEqual(self.envelope_distance(spine, it), tm.SPINE_CLEAR_M)
        wide_tail = max(it[0][1] + it[2] for it in items)
        self.assertGreaterEqual(float(spine[:, 1].max()), wide_tail + back - 5.0)       # (behind the wide tails by their half span + 7.5 m,
        #                                                                                  its corners rounded: a few metres off the vertex)
        self.assertGreaterEqual(tm._min_radius(spine), tm.MIN_RADIUS_M - 0.5)

    def test_a_ring_clears_every_aircraft_and_keeps_the_radius(self):
        items = []
        for k in range(8):
            a = np.radians(45.0 * k)
            c = np.array([np.cos(a), np.sin(a)])
            items.append((c * 40.0, -c, pa.SPECS["narrow"][0], pa.SPECS["narrow"][1]))
        spines, rows = self.spines(items, ["narrow"] * 8)
        self.assertEqual([k for _m, k in rows], ["ring"])
        self.assertEqual(len(spines), 1)
        for it in items:
            self.assertGreaterEqual(self.envelope_distance(spines[0], it), tm.SPINE_CLEAR_M)
        self.assertGreaterEqual(tm._min_radius(spines[0], True), tm.MIN_RADIUS_M - 0.5)

    def test_the_radius_of_a_polyline(self):
        pts = np.array([[0.0, 0.0], [10.0, 0.0], [10.0, 10.0]])
        self.assertLess(tm._min_radius(tm._round_arcs(pts, 15.0)), tm.MIN_RADIUS_M)      # (a 90 degree turn on 10 m sides: tight)
        self.assertEqual(tm._min_radius(np.array([[0.0, 0.0], [5.0, 0.0], [9.0, 0.0]])), np.inf)


class Lenses(unittest.TestCase):
    """standpaths.build on synthetic stands and lanes: the half lenses off the lane a stand's axis meets, the slit, the radius, the room
    between neighbours and the spine of a row no lane reaches. The stands are narrow-bodies, noses at (x, y), the lanes flat polylines."""
    NONE = (np.zeros((0, 2)), np.zeros((0, 2)), np.zeros(0), np.zeros(0, np.int64))
    L, SPAN = pa.SPECS["narrow"][0], pa.SPECS["narrow"][1]
    TL = standpaths.LENS_R_K * SPAN                         # (a quarter turn's tangent length at the preferred radius: 25 m)

    def stand(self, x, y, up=1.0):
        return (np.array([x, y]), np.array([0.0, up]), self.L, self.SPAN)

    def build(self, stands, *lanes, pads=None):
        """([lens halves (k, 2)], [spines], [final runs (2, 2)]) of the stands on the lanes (each a list of points)."""
        paint, found, _rows = standpaths.build(stands, ["narrow"] * len(stands), [(np.array(p, np.float64), 0) for p in lanes],
                                               self.NONE if pads is None else pads)
        return ([xy for xy, role, _r in paint if role == "lead"], [xy for xy, role, _r in paint if role == "spine"], found)

    def test_a_stand_square_behind_a_long_lane_gets_two_halves_and_its_final_straight(self):
        halves, spines, found = self.build([self.stand(0.0, 60.0)], [(-150, 0), (150, 0)])
        self.assertEqual((len(halves), spines), (2, []))
        self.assertEqual(sorted(round(float(P[0][0]) / self.TL) for P in halves), [-1, 1])       # one each way along the lane
        for P in halves:
            self.assertAlmostEqual(float(P[0][1]), 0.0, 3)                      # leaving the lane a tangent length either side of the axis
            self.assertAlmostEqual(abs(float(P[0][0])), self.TL, delta=0.1)
            self.assertAlmostEqual(float(P[-1][0]), 0.0, 3)                     # ... arriving on the axis, along it
            d = P[-1] - P[-2]
            self.assertGreater(d[1] / np.hypot(*d), 0.97)
            self.assertAlmostEqual(float(P[-1][1]), float(found[0][0][1]), 3)   # (where the final straight starts)
        self.assertAlmostEqual(float(found[0][1][1]), 60.0, 3)                  # (it ends at the nose)
        self.assertGreaterEqual(float(found[0][1][1] - found[0][0][1]), tm.lead_straight(self.L) - 0.05)

    def test_a_lane_ending_just_past_the_axis_gives_one_half_only(self):
        halves, _spines, found = self.build([self.stand(0.0, 60.0)], [(-150, 0), (20, 0)])        # (no room for an arc to leave it east)
        self.assertEqual(len(halves), 1)
        self.assertAlmostEqual(float(halves[0][0][0]), -self.TL, delta=0.1)     # from the west, along the lane
        self.assertAlmostEqual(float(halves[0][-1][1]), float(found[0][0][1]), 3)

    def test_a_lane_running_head_on_into_the_stand_gives_the_slit_the_final_straight_starts_on_the_lane(self):
        across = [(-100, 0), (100, 0)]
        _h, _s, plain = self.build([self.stand(0.0, 60.0)], across)
        self.assertAlmostEqual(float(plain[0][0][1]), self.TL, delta=0.1)       # (a lane across alone: the run starts at the arcs' end)
        _h, _s, slit = self.build([self.stand(0.0, 60.0)], across, [(0, -100), (0, 0)])         # (a lane along the axis, up to the crossing)
        self.assertAlmostEqual(float(slit[0][0][0]), 0.0, 3)
        self.assertAlmostEqual(float(slit[0][0][1]), 0.0, 3)                    # the run starts at X, on the lane
        self.assertAlmostEqual(float(slit[0][1][1]), 60.0, 3)

    def test_a_lane_far_behind_gives_a_larger_radius_than_a_near_one(self):
        lane = [(-300, 0), (300, 0)]
        near = abs(float(self.build([self.stand(0.0, 60.0)], lane)[0][0][0][0]))
        far = abs(float(self.build([self.stand(0.0, 120.0)], lane)[0][0][0][0]))     # (a quarter turn: the tangent length is the radius)
        self.assertAlmostEqual(near, self.TL, delta=0.1)
        self.assertGreater(far, near + 10.0)
        self.assertLessEqual(far, standpaths.LENS_R_MAX_K * self.TL + 0.1)      # (stretched, but not without end)

    def test_stands_side_by_side_on_a_lane_keep_their_halves_within_half_the_way_to_the_next(self):
        for gap in (40.0, 80.0):
            halves, _spines, _found = self.build([self.stand(-gap / 2.0, 60.0), self.stand(gap / 2.0, 60.0)], [(-300, 0), (300, 0)])
            self.assertEqual(len(halves), 4)
            inner = [P for P in halves if float((P[0][0] - P[-1][0]) * -P[-1][0]) > 0.0]       # (leaving toward the other stand)
            self.assertEqual(len(inner), 2)
            for P in inner:                                                     # (a half ends on its own stand's axis, at that stand's X)
                self.assertAlmostEqual(abs(float(P[0][0] - P[-1][0])), min(self.TL, gap / 2.0), delta=0.05, msg=gap)
            for P in halves:
                self.assertLessEqual(abs(float(P[0][0] - P[-1][0])), self.TL + 0.05)

    def test_a_row_of_three_with_no_lane_in_reach_gets_a_spine_joined_to_the_lane_150_m_off(self):
        stands = [self.stand(x, 5.0, -1.0) for x in (-44.0, 0.0, 44.0)]         # (their tails face a lane 145 m behind: past 2.5 spans)
        halves, spines, found = self.build(stands, [(-300, 150), (300, 150)])
        self.assertEqual(len(spines), 1)
        spine = spines[0]
        flip = abs(float(spine[-1][1]) - 150.0) > 0.05                          # (the end that is on the lane last)
        spine = spine[::-1] if flip else spine
        self.assertAlmostEqual(float(spine[-1][1]), 150.0, 3)                   # joined to the lane ...
        d = spine[-1] - spine[-2]
        self.assertGreater(abs(float(d[0])) / np.hypot(*d), 0.97)               # ... leaving it along it
        back = self.SPAN / 2.0 + tm.SPINE_CLEAR_M
        spine_y = 5.0 + self.L + back                                           # (half a span and 7.5 m behind the tails)
        self.assertAlmostEqual(float(spine[0][1]), spine_y, delta=0.05)
        starts = np.array([P[0][0] for P in halves if abs(float(P[0][1]) - spine_y) < 0.6])
        self.assertGreaterEqual(len(starts), 3)                                 # (the lenses leave the spine)
        free = float(spine[0][0])
        self.assertLess(min(abs(free - starts.min()), abs(free - starts.max())), 0.1)    # the free end stops at the first lens: not past it
        self.assertTrue(starts.min() - 0.1 <= free <= starts.max() + 0.1)
        self.assertTrue(all(f is not None for f in found))


class MarkWidths(unittest.TestCase):
    def test_yellow_centrelines_are_0_3_m_with_a_0_15_m_black_border_each_side(self):
        self.assertEqual((tm.CENTRE_W_M, tm.LEAD_W_M, tm.BORDER_M), (0.3, 0.3, 0.15))
        f = feats(taxiway((-100, 0), (100, 0), cls="taxilane"))
        raw = tm.strokes([f])
        yellow = [s for s in raw if s[5] == tm.CENTRE_KIND]
        under = [s for s in raw if s[5] == tm.UNDER_KIND]
        self.assertTrue(yellow and under, "a mapped taxilane has its border")
        self.assertTrue(all(abs(s[4] - 0.3) < 1e-9 for s in yellow))
        self.assertTrue(all(abs(s[4] - 0.6) < 1e-9 and s[6] == "black" for s in under))
        self.assertAlmostEqual(sum(float(np.hypot(*np.subtract(metres(s)[2:], metres(s)[:2]))) for s in under), 200.0, 0)


class ServiceRoadFrontage(unittest.TestCase):
    """One road along a building's frontage: a ring round a satellite, an arc along a terminal, cut only by a bridge's footprint."""
    NARROW = pa.SPECS["narrow"]

    @staticmethod
    def circle(r, n=24):
        return [(r * np.cos(2 * np.pi * k / n), r * np.sin(2 * np.pi * k / n)) for k in range(n)]

    def road_points(self, stands, site):
        f = feats(taxiway((-400, -400), (-300, -400), cls="taxilane"))
        raw = [metres(s) for s in tm.strokes([f], stands, site) if s[5] == tm.ROAD_KIND]
        return polyline_samples(raw, 0.5)

    def satellite(self, bridges=()):
        stands = []
        for k in range(8):
            a = np.radians(45.0 * k + 22.5)
            c = np.array([np.cos(a), np.sin(a)]) * 75.0
            stands.append(craft("narrow", float(c[0]), float(c[1]), float(-np.cos(a)), float(-np.sin(a))))
        return stands, site_of_metres([self.circle(30.0)], bridges)

    def test_a_ring_all_the_way_round_a_satellite_where_nothing_is_in_the_way(self):
        stands, site = self.satellite()
        pts = self.road_points(stands, site)
        r = np.hypot(*pts.T)
        self.assertTrue(len(pts) > 100)
        self.assertTrue(((r > 30.0 + 9.5 - 4.2) & (r < 30.0 + 9.5 + 4.2)).all(), "offset from the outline: two edges 7 m apart")
        ang = np.degrees(np.arctan2(pts[:, 1], pts[:, 0])) % 360.0
        for a in range(0, 360, 4):
            self.assertTrue((np.abs(((ang - a + 180.0) % 360.0) - 180.0) < 3.0).any(), a)        # no part of the ring is missing

    def test_a_bridge_cuts_the_ring_only_where_its_footprint_is_and_the_road_resumes(self):
        d = np.array([np.cos(np.radians(20.0)), np.sin(np.radians(20.0))])
        stands, site = self.satellite([(d * 60.0, d)])                          # (a bridge from the wall out to 60 m at 20 degrees)
        pts = self.road_points(stands, site)
        ang = np.degrees(np.arctan2(pts[:, 1], pts[:, 0])) % 360.0
        near = lambda a: (np.abs(((ang - a + 180.0) % 360.0) - 180.0) < 2.0).any()
        self.assertFalse(near(20.0), "the road stops at the bridge")
        for a in (3.0, 8.0, 32.0, 40.0, 90.0, 200.0, 340.0):
            self.assertTrue(near(a), a)                                         # (and goes on, both sides)

    def test_a_row_along_a_terminal_is_one_road_across_a_stand_that_is_too_near(self):
        xs = (-88.0, -44.0, 0.0, 44.0, 88.0)
        ys = (5.0, 5.0, -8.0, 5.0, 5.0)                                         # (the middle nose 7 m from the wall: no road of its own)
        wall = [(-300.0, -15.0), (300.0, -15.0), (300.0, -40.0), (-300.0, -40.0)]
        stands = [craft("narrow", x, y + self.NARROW[0] / 2.0, 0.0, -1.0) for x, y in zip(xs, ys)]
        pts = self.road_points(stands, site_of_metres([wall]))
        for x in range(-90, 91, 6):
            self.assertTrue((np.abs(pts[:, 0] - x) < 3.0).any(), x)             # (not broken at the stand)
        self.assertGreater(pts[:, 0].max(), 88.0)


class LeadStraight(Unpaved):
    """An aircraft is lined up straight before its stop position: the lead-in's last straight along the stand's axis, ending at the stop
    bar, is at least the nose-to-main-gear distance plus a quarter of the length, and at least 20 m; the arcs that join the lane or spine
    (the radius of its kind or more) start behind it. Where the lane is too close the straight is never shortened."""
    LANE = taxiway((-300, 0), (300, 0), cls="taxilane")
    EXPECT = {"light": 20.0, "turboprop": 20.0, "narrow": 26.696, "wide": 45.227}      # (0.46 + 0.25) of the length, at least 20 m

    def test_the_least_straight_run_of_each_kind(self):
        for kind, want in self.EXPECT.items():
            length = pa.SPECS[kind][0]
            self.assertAlmostEqual(tm.lead_straight(length), want, 3, kind)
            self.assertGreaterEqual(tm.lead_straight(length), tm.LEAD_STRAIGHT_MIN_M)
            self.assertAlmostEqual(tm.lead_straight(length), max(20.0, (pa.MAIN_GEAR_K - pa.NOSE_GEAR_K + 0.25) * length), 6)

    def lead_of(self, lane, stand):
        nw = tm.network([feats(lane)], [stand])
        nose = nw.stops[0][0]
        got = [x for x in nw.lines if x[1] == "lead" and float(np.hypot(*(x[0][-1] - nose))) < 0.05]
        self.assertEqual(len(got), 1, "a stand has its lead-in")
        return nw, got[0]

    def test_every_kind_lines_up_straight_before_the_stop_bar_with_an_arc_of_its_radius(self):
        for kind in self.EXPECT:
            for gap in (45.0, 30.0, 14.0):                  # (the stand's centre this far from the lane: room, little, none)
                nw, (xy, _role, _rm) = self.lead_of(self.LANE, craft(kind, 0.0, gap, 0.0, 1.0))
                self.assertGreaterEqual(tm.final_straight(xy), tm.lead_straight(pa.SPECS[kind][0]) - 0.05, (kind, gap))
                for arc, role, _rm in nw.lines:             # (the curves in front of it: arcs of the kind's radius or more)
                    if role == "lead" and len(arc) > 2:
                        self.assertGreaterEqual(tm._min_radius(arc), tm._rmin(kind) - 0.5, (kind, gap))
                self.assertEqual(nw.short, [])

    def test_a_close_lane_does_not_shorten_the_straight(self):
        near = craft("narrow", 0.0, 30.0, 0.0, 1.0)         # (nose 48.8 m from the lane: 26.7 m straight and a 15 m arc do not fit beside it)
        _nw, (xy, _r, _m) = self.lead_of(self.LANE, near)
        self.assertGreaterEqual(tm.final_straight(xy), tm.lead_straight(pa.SPECS["narrow"][0]) - 0.05)


class NetworkAudit(unittest.TestCase):
    """tm.audit: dangling ends (gaps under 8 m, ends in an open apron), kinks (along a line, and at a join that is neither tangent
    nor straight through) and S-bends tighter than the radius; the painted network of a small airport reports none."""
    NONE = {"gaps": 0, "opens": 0, "kinks": 0, "sbends": 0}

    def test_a_small_airport_reports_none(self):
        lane = taxiway((-250, 100), (250, 100), cls="taxilane")
        main = taxiway((-250, 400), (250, 400))
        link = taxiway((120, 400), (120, 100))                              # (a taxiway across the corner: a T at each end)
        row = [craft("narrow", x, 5.0 + pa.SPECS["narrow"][0] / 2.0, 0.0, -1.0) for x in (-90.0, -45.0, 0.0, 45.0)]
        row += [craft("turboprop", -10.0, 700.0, 0.0, 1.0)]                 # (a stand on the far side)
        nw = tm.network([feats(lane, main, link)], row)
        self.assertEqual(nw.audit().counts(), self.NONE)
        self.assertEqual(nw.short, [])
        self.assertEqual(len(nw.stops), len(row))

    def test_a_planted_gap_of_3_m_is_found_and_a_join_is_not(self):
        a = np.array([[0.0, 0.0], [100.0, 0.0]])
        b = np.array([[50.0, 3.0], [50.0, 60.0]])                           # ends 3 m short of a
        got = tm.audit([a, b])
        self.assertEqual(len(got.gaps), 1)
        self.assertAlmostEqual(got.gaps[0][2], 3.0, 3)
        self.assertEqual((got.gaps[0][0], got.gaps[0][1]), (50.0, 3.0))
        smooth = np.array([[100.0, 0.0], [160.0, 0.0]])                     # (a line end to end, straight on: nothing)
        self.assertEqual(len(tm.audit([a, smooth]).gaps), 0)
        far = tm.audit([a, np.array([[50.0, 12.0], [50.0, 60.0]])])
        self.assertEqual(len(far.gaps), 0)                                  # (12 m off: not a gap, an end in the open)
        self.assertIn((50.0, 12.0), [(o[0], o[1]) for o in far.opens])

    def test_ends_that_are_meant_to_stop_are_not_reported(self):
        a = np.array([[0.0, 0.0], [100.0, 0.0]])
        edge = np.array([[-5.0, -20.0], [-5.0, 20.0]])
        self.assertEqual(len(tm.audit([a]).opens), 2)                       # (a line alone: both ends in the open)
        self.assertEqual(len(tm.audit([a], stops=[(100.0, 0.0)], edges=[edge]).opens), 1)
        self.assertEqual(len(tm.audit([a], runways=[(np.array([[106.0, -50.0], [106.0, 50.0]]), 5.0)], stops=[(0.0, 0.0)]).opens), 0)
        self.assertEqual(len(tm.audit([a], aprons=[]).opens), 0)            # (not in an apron: the end of a taxiway)

    def test_a_planted_40_degree_kink_is_found_and_a_crossing_straight_through_is_not(self):
        c = np.radians(40.0)
        bent = np.array([[0.0, 0.0], [50.0, 0.0], [50.0 + 50.0 * np.cos(c), 50.0 * np.sin(c)]])
        got = tm.audit([bent])
        self.assertEqual(len(got.kinks), 1)
        self.assertAlmostEqual(got.kinks[0][2], 40.0, delta=0.5)
        self.assertEqual((round(got.kinks[0][0]), round(got.kinks[0][1])), (50, 0))
        self.assertEqual(len(tm.audit([tm._round_arcs(bent, tm.MIN_RADIUS_M)]).kinks), 0)       # (the same corner as an arc of the radius)
        across = [np.array([[0.0, 0.0], [100.0, 0.0]]), np.array([[50.0, -40.0], [50.0, 0.0]]), np.array([[50.0, 0.0], [50.0, 40.0]])]
        self.assertEqual(len(tm.audit(across).kinks), 0)                    # (one line straight across another, the other split there)
        self.assertEqual(len(tm.audit([across[0], across[2]]).kinks), 1)    # (a T that is neither tangent nor across)
        fork = [across[0], np.array([[50.0, 0.0], [90.0, 8.0]])]            # (a branch leaving 11 degrees off the line: tangent)
        self.assertEqual(len(tm.audit(fork).kinks), 0)

    def test_an_s_bend_tighter_than_the_radius_is_found(self):
        def s_curve(r, turn):                                               # a circular arc of radius r to the left, then one to the right
            t = np.linspace(0.0, turn, 40)
            left = np.stack([r * np.sin(t), r * (1.0 - np.cos(t))], 1)
            end, th = left[-1], turn
            right = np.stack([end[0] + r * (np.sin(th) - np.sin(th - t)), end[1] + r * (np.cos(th - t) - np.cos(th))], 1)
            return np.vstack([left, right[1:]])
        got = tm.audit([s_curve(8.0, 0.8)], rmin=15.0)
        self.assertEqual(len(got.sbends), 1)
        self.assertLess(got.sbends[0][2], 15.0)
        self.assertEqual(len(tm.audit([s_curve(40.0, 0.6)], rmin=15.0).sbends), 0)
        self.assertEqual(len(tm.audit([s_curve(8.0, 0.8)], rmin=8.0).sbends), 0)       # (the radius of a light aircraft: not tight)


class TaxiwayNames(unittest.TestCase):
    """The taxiways' names (ICAO location markings, as in the photographs): a black box with a thin yellow ring and the name in yellow
    letters, centred on the way's yellow centreline (the box is drawn over it), upright for the pilot looking along the way, 20 to 30 m
    before each junction with a way of another name and before a runway's holding position; none on an unnamed way, off the paving or
    over other paint."""
    def setUp(self):
        patcher = mock.patch.object(tm, "_edge_lines", lambda sc, feats_list: [])
        patcher.start()
        self.addCleanup(patcher.stop)

    def named(self, ref, *pts, **kw):
        tags = dict(kw.pop("tags", {}))
        if ref:
            tags["ref"] = ref
        return taxiway(*pts, tags=tags, **kw)

    def marks(self, f):
        """[(box (4, 2), ring [(4, 2)], letters [(quad (4, 2), UVs (4, 2))])] of the names painted on the Features f, in metres east /
        north of the frame's origin: the black fill ("text" material), the yellow ring and the letters (both in the yellow font
        material; the ring's quads sit on the sheet's solid cell), over it."""
        got = filled_uv(f, [])
        boxes = [p for p, colour, layer, _uv in got if (colour, layer) == ("text", "glyph")]
        rest = [x for x in got if (x[1], x[2]) == ("font_yellow", "glyph")]
        self.assertEqual(len(got), len(boxes) + len(rest), "a name is a black box, a ring and letters, nothing else")
        solid = np.array(standmarks.solid())
        out = []
        for b in boxes:
            lo, hi = b.min(axis=0) - 1e-3, b.max(axis=0) + 1e-3
            mine = [x for x in rest if (x[0].min(axis=0) >= lo).all() and (x[0].max(axis=0) <= hi).all()]
            ring = [p for p, _c, _l, uv in mine if np.allclose(uv, solid)]
            letters = [(p, uv) for p, _c, _l, uv in mine if not np.allclose(uv, solid)]
            self.assertEqual(len(ring), 4, "a ring of four bars")
            self.assertTrue(letters, "letters in every box")
            out.append((b, ring, letters))
        self.assertEqual(sum(len(r) + len(m) for _b, r, m in out), len(rest), "every ring and letter is in a box")
        return out

    def cross(self, *extra, ref_a="A", ref_b="B"):
        return feats(self.named(ref_a, (-300, 0), (300, 0)), self.named(ref_b, (0, -300), (0, 300)), *extra)

    def test_two_named_ways_crossing_have_a_name_on_each_approach_centred_on_the_line_and_upright(self):
        got = self.marks(self.cross())
        self.assertEqual(len(got), 4, "a name on each of the four approaches")
        h = tm.NAME_TEXT
        for name, up in (("A", (1, 0)), ("A", (-1, 0)), ("B", (0, 1)), ("B", (0, -1))):
            up = np.array(up, np.float64)
            left = np.array([-up[1], up[0]])
            near = [x for x in got if float(np.hypot(*(x[0].mean(axis=0) - (-up * 25.0)))) < 6.0]
            self.assertEqual(len(near), 1, f"{name} travelling {tuple(up)}")
            box, ring, letters = near[0]
            centre = box.mean(axis=0)
            self.assertTrue(20.0 <= -float(centre @ up) <= 30.0, centre)           # (20 to 30 m before the crossing)
            self.assertAlmostEqual(float(centre @ left), 0.0, 2)                    # (centred on the centreline)
            # the box: the name's width and the capitals' height with a margin all round, the ring inside it
            bw, bh = standmarks.text_width(name, h) + 2.0 * tm.NAME_MARGIN_M, h + 2.0 * tm.NAME_MARGIN_M
            self.assertAlmostEqual(float(np.ptp(box @ left)), bw, 2)
            self.assertAlmostEqual(float(np.ptp(box @ up)), bh, 2)
            inset, width = tm.NAME_RING_M
            outer = np.concatenate(ring)
            self.assertAlmostEqual(float(np.ptp(outer @ left)), bw - 2.0 * inset, 2)
            self.assertAlmostEqual(float(np.ptp(outer @ up)), bh - 2.0 * inset, 2)
            self.assertTrue(all(abs(float(np.ptp(r @ left)) - width) < 1e-2 or abs(float(np.ptp(r @ up)) - width) < 1e-2 for r in ring))
            # upright for the pilot: the font's quads read looking along `up`, with the UVs of the sheet
            want = standmarks.text_quads(name, centre, up, h)
            self.assertEqual(len(letters), len(want))
            for (p, uv), (q, quv) in zip(letters, want):
                np.testing.assert_allclose(p, q, atol=0.02)
                np.testing.assert_allclose(uv, quv, atol=1e-6)

    def test_the_box_is_over_the_centreline_and_clear_of_every_other_paint_and_the_paving_edge(self):
        f = self.cross()
        got = self.marks(f)
        strokes = [np.array(metres(s)).reshape(2, 2) for s in tm.strokes([f])]
        for box, _ring, _letters in got:
            centre = box.mean(axis=0)
            on_a = abs(float(centre[1])) < 1e-2
            other = [seg for seg in strokes if (float(np.abs(seg[:, 0]).max()) < 1e-2) == on_a]      # (the other way's strokes)
            self.assertTrue(other)
            for seg in other:
                near = min(float(tm._pt_segs(q, seg[:1], seg[1:]).min()) for q in box)
                self.assertGreaterEqual(near, 0.2)
            across = np.minimum(np.abs(box[:, 0]), np.abs(box[:, 1]))             # (inside the 15 m wide pad of the way it is on)
            self.assertLessEqual(float(across.max()), aeroways.TAXIWAY_M / 2.0 - tm.NAME_PAVE_M + 1e-3)

    def test_an_unnamed_way_gets_none_and_meets_no_name(self):
        self.assertEqual(self.marks(self.cross(ref_b="")), [])                  # (A across an unnamed way: nothing to tell apart)
        self.assertEqual(self.marks(self.cross(ref_b="A")), [])                 # (two ways of one name)
        for unpaintable in ("TAXIWAY", "A;B"):                                  # (a name over NAME_MAX characters, one the font cannot draw: still
            got = self.marks(self.cross(ref_b=unpaintable))                     # a name, so A has its two approaches' and B, painted nothing, none)
            self.assertEqual(len(got), 2)
            self.assertTrue(all(abs(float(b.mean(axis=0)[1])) < 1e-2 for b, _r, _m in got))
        got = self.marks(self.cross(self.named("", (-300, 80), (300, 80))))     # (a third way across both, unnamed: no name on it, none for it)
        self.assertEqual(len(got), 4)
        for box, _ring, _letters in got:
            self.assertGreater(abs(float(box.mean(axis=0)[1] - 80.0)), 8.0)

    def test_a_name_is_none_where_it_would_touch_other_paint_or_leave_the_paving(self):
        lane = self.named("", (-300, 2.3), (300, 2.3), cls="taxilane")          # (an unnamed lane's yellow line 2.3 m off A: across A's box
        with mock.patch.object(tm, "NAME_TEXT", 3.0), mock.patch.object(tm, "NAME_MARGIN_M", 0.6):   # at 3 m capitals: nearer, the
            got = self.marks(self.cross(lane))                                  # lane is smoothed into A's own line)
        self.assertEqual(len(got), 2, "A's two names have the lane's line across them; B's keep theirs")
        self.assertTrue(all(abs(float(b.mean(axis=0)[0])) < 1e-2 and abs(float(b.mean(axis=0)[1])) > 15.0 for b, _r, _m in got))
        narrow = feats(self.named("A", (-300, 0), (300, 0), tags={"width": "5"}), self.named("B", (0, -300), (0, 300)))
        got = self.marks(narrow)
        self.assertEqual(len(got), 2, "none on A where its pad is 5 m wide (the box is wider): B's two")
        self.assertTrue(all(abs(float(b.mean(axis=0)[0])) < 1e-2 and abs(float(b.mean(axis=0)[1])) > 15.0 for b, _r, _m in got))

    def test_the_same_name_is_not_repeated_within_60_m_for_one_direction(self):
        f = feats(self.named("A", (-300, 0), (300, 0)), self.named("B", (0, -300), (0, 300)), self.named("C", (40, -300), (40, 300)))
        got = sorted(float(b.mean(axis=0)[0]) for b, _r, _m in self.marks(f) if abs(float(b.mean(axis=0)[1])) < 8.0)      # (the names on A)
        self.assertEqual(len(got), 2, "one A for each direction, though B and C are 40 m apart")
        self.assertAlmostEqual(got[0], -25.0, delta=5.0)                          # (before B going east, before B going west: the farther C's dropped)
        self.assertAlmostEqual(got[1], 25.0, delta=5.0)

    def test_a_name_before_a_runway_holding_position_toward_the_runway(self):
        f = feats(runway((-1000, 0), (1000, 0)), self.named("A", (0, 400), (0, 0)))
        got = self.marks(f)
        self.assertEqual(len(got), 1)
        centre = got[0][0].mean(axis=0)
        self.assertGreaterEqual(float(centre[1]), tm.HOLD_DIST_M + 20.0)       # (before the hold line, coming from the far end)
        self.assertLessEqual(float(centre[1]), tm.HOLD_DIST_M + 30.0)
        self.assertAlmostEqual(float(centre[0]), 0.0, 2)                       # (on the centreline)
        up = np.array([0.0, -1.0])                                             # (heading south, toward the runway)
        for (p, uv), (q, quv) in zip(got[0][2], standmarks.text_quads("A", centre, up, tm.NAME_TEXT)):
            np.testing.assert_allclose(p, q, atol=0.02)

    def test_the_names_are_in_the_tiles_chunks_a_black_fill_then_the_yellow_ring_and_letters_with_their_uvs(self):
        f = self.cross()
        bbox = lambda tx, ty: (tx * 256.0 - 128.0, ty * 256.0 - 128.0, tx * 256.0 + 128.0, ty * 256.0 + 128.0)      # (the crossing's tile: no box on its edge)
        chunks = tm.tile_chunks([f], FRAME, 0, 0, lambda la, lo: np.full(len(la), 7.0), bbox)
        look = airportgrade.MARK_LOOK
        fill = [c for c in chunks if c["m"] == look["text"]["m"] and c.get("poly")]
        font = [c for c in chunks if c["m"] == look["font_yellow"]["m"] and c.get("poly")]
        self.assertTrue(fill and font)
        self.assertEqual({c["layer"] for c in fill + font}, {"glyph"})
        self.assertEqual(sum(len(c["poly"]) for c in fill), 4 * 4, "four boxes of four corners")
        self.assertEqual(sum(len(c["poly"]) for c in font), 4 * (4 + 1) * 4, "a ring of four bars and a letter in each")
        for c in font:
            self.assertEqual(len(c["uvq"]), len(c["poly"]))
            self.assertTrue(all(0.0 <= u <= 1.0 and 0.0 <= v <= 1.0 for u, v in c["uvq"]))
        self.assertTrue(any(c["uvq"] != [[0.5, 0.5]] * len(c["uvq"]) for c in font))
        import roadmesh
        po = roadmesh.PAINT_ORDER
        self.assertGreater(po[look["font_yellow"]["m"]], po[look["text"]["m"]])      # (the letters and ring drawn over the fill, which hides the centreline)
        self.assertGreater(po[look["text"]["m"]], po[look["yellow"]["m"]])

    def ribbons(self, f):
        """[(polyline (k, 2) m east / north, kind)] of the marks' lines of the Features f (the centrelines and their black borders)."""
        out = []
        for la, lo, _w, kind, _c in tm._all_marks([f], [], None)[0]:
            e, n, _ = FRAME.geodetic_to_enu(la, lo, np.zeros(len(la)))
            out.append((np.stack([e, n], 1), kind))
        return out

    @staticmethod
    def samples(P, step=0.02):
        """Points along the polyline P every `step` m (its vertices too)."""
        out = [P[:1]]
        for a, b in zip(P[:-1], P[1:]):
            k = max(1, int(np.ceil(float(np.hypot(*(b - a))) / step)))
            out.append(a + (b - a) * (np.arange(1, k + 1) / k)[:, None])
        return np.concatenate(out)

    def test_the_centreline_and_its_border_are_cut_out_of_a_name_box_and_resume_on_both_sides(self):
        """The user, 2026-11-17: the yellow centreline showed through the black box in game (the draw order of the paint was no help):
        no line of the way runs inside the box grown by NAME_CUT_M, and it goes on either side of it."""
        f = self.cross()
        boxes = [b for b, _r, _m in self.marks(f)]
        self.assertEqual(len(boxes), 4)
        ribbons = self.ribbons(f)
        kinds = {k for _p, k in ribbons}
        self.assertIn(tm.CENTRE_KIND, kinds)
        for box in boxes:
            centre = box.mean(axis=0)
            up = (np.array([1.0, 0.0]) if abs(float(centre[1])) < 1e-2 else np.array([0.0, 1.0]))      # (the way's axis: A east-west, B north-south)
            hx = float(np.ptp(box @ np.array([-up[1], up[0]]))) / 2.0 + tm.NAME_CUT_M
            hy = float(np.ptp(box @ up)) / 2.0 + tm.NAME_CUT_M
            left = np.array([-up[1], up[0]])
            for P, kind in ribbons:
                q = self.samples(P)
                inside = (np.abs((q - centre) @ left) < hx - 1e-6) & (np.abs((q - centre) @ up) < hy - 1e-6)
                self.assertFalse(inside.any(), f"{kind} runs through the box at {centre}")
            for kind in (tm.CENTRE_KIND, tm.UNDER_KIND):            # (a line on each side of it: just past the cut, 1 m on)
                mine = [P for P, k in ribbons if k == kind and float(np.abs((P - centre) @ left).max()) < 1e-2]      # (this way's own)
                if kind == tm.UNDER_KIND and not mine:
                    continue                                        # (the border is on aprons only)
                for side in (-1.0, 1.0):
                    for off in (hy + 0.01, hy + 1.0):
                        at = centre + up * side * off
                        near = min(float(tm._pt_segs(at, P[:-1], P[1:]).min()) for P in mine)
                        self.assertLess(near, 1e-3, f"{kind} {side:+} {off}")

    def test_without_the_cut_the_line_runs_through_the_box(self):
        """(the check above bites: the same Features with the cut switched off have the line in the box)"""
        f = self.cross()
        box = self.marks(f)[0][0]
        centre = box.mean(axis=0)
        with mock.patch.object(tm, "_cut_lines", lambda sc, lines, boxes, cuts: (lines, boxes)):
            uncut = self.ribbons(self.cross())
        up = np.array([1.0, 0.0]) if abs(float(centre[1])) < 1e-2 else np.array([0.0, 1.0])
        left = np.array([-up[1], up[0]])
        self.assertTrue(any(bool(((np.abs((self.samples(P) - centre) @ left) < 1e-2) & (np.abs((self.samples(P) - centre) @ up) < 1.0)).any())
                            for P, _k in uncut))

    def test_the_cut_pieces_are_the_same_every_time_and_in_the_tiles_chunks_none_runs_through_a_box(self):
        one, two = self.ribbons(self.cross()), self.ribbons(self.cross())
        self.assertEqual(len(one), len(two))
        for (p, k), (q, l) in zip(one, two):
            self.assertEqual(k, l)
            np.testing.assert_array_equal(p, q)
        for P, _k in one:
            self.assertGreaterEqual(len(P), 2)
            self.assertGreaterEqual(float(np.hypot(*np.diff(P, axis=0).T).sum()), tm.PIECE_MIN_M)       # (no zero-length piece)
        f = self.cross()
        boxes = [b for b, _r, _m in self.marks(f)]
        bbox = lambda tx, ty: (tx * 256.0 - 128.0, ty * 256.0 - 128.0, tx * 256.0 + 128.0, ty * 256.0 + 128.0)
        chunks = [c for c in tm.tile_chunks([f], FRAME, 0, 0, lambda la, lo: np.full(len(la), 7.0), bbox) if not c.get("poly")]
        self.assertTrue(chunks)
        for c in chunks:
            self.assertGreaterEqual(len(c["n"]), 2)
            nodes = np.array(c["n"])[:, :2]
            ends = float(np.hypot(*np.diff(nodes, axis=0).T).sum())
            self.assertGreater(ends, 0.0)
            for box in boxes:
                centre = box.mean(axis=0)
                up = np.array([1.0, 0.0]) if abs(float(centre[1])) < 1e-2 else np.array([0.0, 1.0])
                left = np.array([-up[1], up[0]])
                q = self.samples(nodes)
                inside = ((np.abs((q - centre) @ left) < float(np.ptp(box @ left)) / 2.0 - 1e-3) &
                          (np.abs((q - centre) @ up) < float(np.ptp(box @ up)) / 2.0 + tm.NAME_CUT_M - 1e-3))
                self.assertFalse(inside.any())

    def test_the_same_names_whatever_the_order_of_the_ways(self):
        a, b = self.named("A", (-300, 0), (300, 0)), self.named("B", (0, -300), (0, 300))
        one = sorted(tuple(np.round(p.mean(axis=0), 2)) for p, _c, _l in filled(feats(a, b), []))
        two = sorted(tuple(np.round(p.mean(axis=0), 2)) for p, _c, _l in filled(feats(b, a), []))
        self.assertTrue(one)
        self.assertEqual(one, two)


if __name__ == "__main__":
    unittest.main()
