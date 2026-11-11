"""OpenStreetMap's placement=* on one-way carriageways (docs/research/2026-11-09-osm-placement-design.md): the full
shift a tag gives a carriageway's lanes (roads.placement_full) and its run along a feature (roads.placement_shift),
through its width tapers and eased between parts. The A93 before Kreuz Regensburg (OSM ways 308799828 and others:
lanes=3, placement=right_of:1) stood 2 m into the median, overlapped the other carriageway and lost its median
barrier."""
import math
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import railtags  # noqa: E402
import roads  # noqa: E402
from test_roads import FRAME, LAT, LON, M_LAT, M_LON, features, flat, rules  # noqa: E402


def spec(cls="motorway", width=18.0, d=1, oneway=True, link=False, lanes=3):
    return roads.Spec(width, "paved", False, cls, oneway, 2.5, d, None, None, None, lanes, None, link)


def tags(placement=None, lanes=None, oneway="yes"):
    t = {"oneway": oneway}
    if placement is not None:
        t["placement"] = placement
    if lanes is not None:
        t["lanes"] = str(lanes)
    return t


def feature(length=200.0, parts=None):
    """One eastbound feature along y = 0 from x = 0 to length, its lane-tag parts [(a, b, tags)]."""
    xs = np.arange(0.0, length + 0.1, 5.0)
    f = features([[(x, 0.0) for x in xs]], ["motorway"])
    f.attrs[railtags.LANE_PARTS] = [[(a, b, "w1", t) for a, b, t in parts]] if parts is not None else [None]
    lon = LON + xs / M_LON
    lat = np.full(len(xs), LAT)
    return f, xs, lon, lat


class RuleTest(unittest.TestCase):
    def test_rules(self):
        for value, lanes, want in (("right_of:1", 3, 2.0), ("right_of:1", 2, 0.0), ("right_of:1", 4, 4.0),
                                   ("middle_of:2", 3, 0.0), ("middle_of:1", 2, 2.0), ("left_of:1", 2, 4.0),
                                   ("right_of:2", 3, -2.0), ("right_of:9", 3, -6.0)):
            with self.subTest(value=value, lanes=lanes):
                self.assertAlmostEqual(roads.placement_full(tags(value, lanes), spec(lanes=lanes)), want)

    def test_no_shift(self):
        self.assertEqual(roads.placement_full(tags("right_of:1", 3), spec(link=True)), 0.0)
        self.assertEqual(roads.placement_full(tags("right_of:1", 3), spec(oneway=False)), 0.0)
        self.assertEqual(roads.placement_full(tags("right_of:1"), spec(lanes=None)), 0.0)
        self.assertEqual(roads.placement_full(tags("transition", 3), spec()), 0.0)
        self.assertEqual(roads.placement_full(tags("right_of:x", 3), spec()), 0.0)
        self.assertEqual(roads.placement_full({"placement:forward": "right_of:1", "lanes": "3"}, spec()), 0.0)
        self.assertEqual(roads.placement_full(None, spec()), 0.0)

    def test_lanes_from_the_spec(self):
        """No lanes on the part: the spec's count."""
        self.assertAlmostEqual(roads.placement_full(tags("right_of:1"), spec(lanes=3)), 2.0)

    def test_trunk_lane_width(self):
        self.assertAlmostEqual(roads.placement_full(tags("right_of:1", 3), spec(cls="trunk", width=17.4)), 1.9)

    def test_shift_capped_inside_the_pavement(self):
        self.assertAlmostEqual(roads.placement_full(tags("right_of:1", 3), spec(width=10.0)), 2.0)
        self.assertAlmostEqual(roads.placement_full(tags("right_of:9", 3), spec(width=18.0)), -6.0)
        self.assertAlmostEqual(roads.placement_full(tags("right_of:9", 3), spec(width=12.0)), -4.0)


class ShiftTest(unittest.TestCase):
    def test_none_without_placement(self):
        f, xs, lon, lat = feature(parts=[(0.0, 1.0, tags(None, 3))])
        self.assertIsNone(roads.placement_shift(f, 0, lon, lat, spec()))
        f, xs, lon, lat = feature(parts=None)
        self.assertIsNone(roads.placement_shift(f, 0, lon, lat, spec()))

    def test_whole_feature(self):
        """right_of:1 on 3 lanes, eastbound along its geometry: 2 m to the right, -2 in offset_polyline's sign."""
        f, xs, lon, lat = feature(parts=[(0.0, 1.0, tags("right_of:1", 3))])
        sh = roads.placement_shift(f, 0, lon, lat, spec())
        np.testing.assert_allclose(sh, -2.0, atol=1e-9)

    def test_reversed_geometry(self):
        f, xs, lon, lat = feature(parts=[(0.0, 1.0, tags("right_of:1", 3))])
        along = roads.placement_shift(f, 0, lon, lat, spec(d=1))
        against = roads.placement_shift(f, 0, lon, lat, spec(d=-1))
        np.testing.assert_allclose(against, -along)
        f2, *_ = feature(parts=[(0.0, 1.0, tags("right_of:1", 3, oneway="-1"))])
        np.testing.assert_allclose(roads.placement_shift(f2, 0, lon, lat, spec(d=1)), -along)
        np.testing.assert_allclose(roads.placement_shift(f2, 0, lon, lat, spec(d=-1)), along)

    def test_joint_delta(self):
        """A joint at x = 0 whose delta is +1 over 48 m (the mean of -2 and a 2-lane neighbour's 0, less -2): -1 at the
        joint, the full -2 past 48 m, and with the width's taper to 16 m there the left (median) edge W(s)/2 + shift
        where it was all along."""
        f, xs, lon, lat = feature(parts=[(0.0, 1.0, tags("right_of:1", 3))])
        sh = roads.placement_shift(f, 0, lon, lat, spec(), ((LON, LAT, 48.0, 1.0),))
        ws = roads.taper_widths(18.0, ((LON, LAT, 16.0, 48.0),), lon, lat)
        self.assertAlmostEqual(sh[0], -1.0, places=6)
        np.testing.assert_allclose(sh[xs >= 50.0], -2.0, atol=1e-9)
        np.testing.assert_allclose(ws / 2 + sh, 7.0, atol=1e-6)

    def test_joint_moves_an_unplaced_feature(self):
        """The 2-lane neighbour: no shift of its own, -1 at the joint while it widens to 16 m, its left edge at 7."""
        sp = spec(width=14.0, lanes=2)
        f, xs, lon, lat = feature(parts=[(0.0, 1.0, tags(None, 2))])
        self.assertIsNone(roads.placement_shift(f, 0, lon, lat, sp))
        sh = roads.placement_shift(f, 0, lon, lat, sp, ((LON, LAT, 48.0, -1.0),))
        ws = roads.taper_widths(14.0, ((LON, LAT, 16.0, 48.0),), lon, lat)
        self.assertAlmostEqual(sh[0], -1.0, places=6)
        np.testing.assert_allclose(sh[xs >= 50.0], 0.0, atol=1e-9)
        np.testing.assert_allclose(ws / 2 + sh, 7.0, atol=1e-6)

    def test_point_outside_the_parts_takes_the_nearest(self):
        """Lane-tag parts covering 0.1..1.0 (unplaced to 0.5, then right_of:1): the first 40 m, outside every part, keep
        the nearest part's shift (none), not the last part's -2 (a 2 m step at 0.1)."""
        f, xs, lon, lat = feature(400.0, parts=[(0.1, 0.5, tags(None, 3)), (0.5, 1.0, tags("right_of:1", 3))])
        sh = roads.placement_shift(f, 0, lon, lat, spec())
        np.testing.assert_allclose(sh[xs <= 40.0], 0.0, atol=1e-9)
        self.assertLess(float(np.abs(np.diff(sh)).max()), 0.5)

    def test_easing_between_parts(self):
        """Unplaced first half, right_of:1 second half, one width: 0 at the start, -2 at the end, monotonic, the change
        eased over clip(40 x 2, 40, 160) = 80 m round the boundary at 200 m."""
        f, xs, lon, lat = feature(400.0, parts=[(0.0, 0.5, tags(None, 3)), (0.5, 1.0, tags("right_of:1", 3))])
        sh = roads.placement_shift(f, 0, lon, lat, spec())
        self.assertAlmostEqual(sh[0], 0.0)
        self.assertAlmostEqual(sh[-1], -2.0)
        self.assertTrue((np.diff(sh) <= 1e-9).all())
        moving = xs[(sh < -1e-6) & (sh > -2.0 + 1e-6)]
        self.assertGreaterEqual(moving.min(), 200.0 - 40.0 - 1e-6)
        self.assertLessEqual(moving.max(), 200.0 + 40.0 + 1e-6)


PLACED = {"placement": "right_of:1", "lanes": "3", "oneway": "yes"}


def carriageways(placed=True, cells=1, ramp=False, outer=2):
    """A dual carriageway along x, centrelines at y = -6.7 (eastbound) and +6.7 (westbound), 13.4 m apart as the A93's
    before Kreuz Regensburg: 2 lanes (14 m, their edges 0.6 m over each other there too) from -500 to -150 and from 150
    to 500, 3 lanes (18 m: 4.6 m over each other drawn centred, no median barrier) between, tagged right_of:1 (the line
    on the through lanes, the added lane outside) when placed. cells 2: the eastbound pieces in two cells, the joint at x = 150
    only in the second (copies of the middle piece in both). ramp: an exit ramp leaving the eastbound 3-lane piece's end
    to the right (a split: lane_shifts shift the wider piece's lines toward it). outer: the outer pieces' lanes (3: one
    width all along, no taper at the joints)."""
    def piece(x0, x1, y):
        xs = np.linspace(x0, x1, int(abs(x1 - x0) / 10) + 1)
        return [(x, y) for x in xs]
    east = [("eA", piece(-500, -150, -6.7), outer), ("eB", piece(-150, 150, -6.7), 3),
            ("eC", piece(150, 500, -6.7), outer)]
    west = [("wC", piece(500, 150, 6.7), outer), ("wB", piece(150, -150, 6.7), 3), ("wA", piece(-150, -500, 6.7), outer)]

    if ramp:
        east = east + [("eR", [(150.0, -6.7), (200.0, -16.0), (260.0, -40.0)], 1)]

    def cell(items):
        f = features([ln for _, ln, _ in items], ["motorway"] * len(items), ids=[fid for fid, _, _ in items])
        f.rules["oneway"] = rules([(k, 1.0) for k in range(len(items))], "float")
        f.rules["subclass"] = rules([(k, "link") for k, (fid, _, _) in enumerate(items) if fid == "eR"])
        f.attrs["osm_lanes"] = np.array([float(n) for _, _, n in items])
        f.attrs[railtags.LANE_PARTS] = [[(0.0, 1.0, fid, dict(PLACED) if (placed and n == 3)
                                          else {"lanes": str(n), "oneway": "yes"})] for fid, _, n in items]
        if outer == 3:
            f.attrs[railtags.LANE_PARTS] = [[(0.0, 1.0, fid, dict(PLACED) if (placed and fid[1] == "B")
                                              else {"lanes": "3", "oneway": "yes"})] for fid, _, _ in items]
        return f
    if cells == 1:
        return [cell(east + west)]
    return [cell(east[:2] + west), cell(east[1:])]


def roads_out(cells):
    out, seen = [], set()
    for tx in (-2, -1, 0, 1):
        got = []
        roads.build_tile(FRAME, tx, 0, cells, flat, terrain_paint=False, roads_out=got)
        for fid, sp, pts, lon in got:
            key = (fid, round(float(pts[0, 0]), 3), round(float(pts[0, 1]), 3))
            if key not in seen:
                seen.add(key)
                out.append((fid, sp, pts, lon))
    return out


def at_x(rows, fid, x):
    """(centreline y, spec) of piece fid at x (its pieces in rows; 0.5 m past their ends: the frame's metres run 0.13 %
    over these)."""
    for f, sp, pts, _ in rows:
        if f == fid and min(pts[0, 0], pts[-1, 0]) - 0.5 <= x <= max(pts[0, 0], pts[-1, 0]) + 0.5:
            order = np.argsort(pts[:, 0])
            return float(np.interp(x, pts[order, 0], pts[order, 1])), sp
    return None


def edge(cells, rows, fid, xs):
    """The median-side (north) edge y of eastbound piece fid at xs: centreline + the drawn width / 2 (its tapers)."""
    out = []
    for x in xs:
        y, sp = at_x(rows, fid, x)
        tp = roads.joint_desc(roads.copies_of(cells).get(fid, []), lambda f: roads.road_tapers(f))
        la, lo, _ = FRAME.enu_to_geodetic(np.array([x]), np.array([y]), 0.0)
        ws = roads.taper_widths(sp.width, tp, lo, la)
        out.append(y + (float(ws[0]) if ws is not None else sp.width) / 2)
    return np.array(out)


class TileTest(unittest.TestCase):
    def test_carriageways_do_not_overlap(self):
        """The 3-lane stretch's gap edge to edge no worse than its 2-lane neighbours' (-0.6 m, as mapped)."""
        rows = roads_out(carriageways())

        def gap(e, w, x):
            (ye, se), (yw, sw) = at_x(rows, e, x), at_x(rows, w, x)
            return (yw - sw.width / 2) - (ye + se.width / 2)
        ref = gap("eA", "wA", -300.0)
        for x in np.arange(-100.0, 101.0, 10.0):
            self.assertGreaterEqual(gap("eB", "wB", x), ref - 0.01, x)

    def test_median_barrier_continuous(self):
        import medians
        rows = roads_out(carriageways())
        mods = medians.modules(rows, (-256.0, -256.0, 256.0, 256.0), False, "DE", (), ())
        self.assertTrue(len(mods))
        spans = sorted((min(m[2], m[4]), max(m[2], m[4])) for m in mods)
        reach, gaps = -150.0, []
        for a, b in spans:
            if b < -150.0 or a > 150.0:
                continue
            if a - reach > 6.0:
                gaps.append((reach, a))
            reach = max(reach, b)
        self.assertEqual(gaps, [])
        self.assertGreaterEqual(reach, 150.0 - 6.0)

    def test_placed_edge_continuous(self):
        cells = carriageways()
        rows = roads_out(cells)
        e = np.concatenate([edge(cells, rows, "eA", np.arange(-300.0, -150.0, 5.0)),
                            edge(cells, rows, "eB", np.arange(-150.0, 150.1, 5.0)),
                            edge(cells, rows, "eC", np.arange(155.0, 300.0, 5.0))])
        self.assertLess(float(np.abs(e - e[0]).max()), 0.1, e)

    def test_joints_meet(self):
        """Both pieces at each joint on one centre, so both edges continuous (both run to 16 m there): the mean of
        theirs, -7.7 (eA's -6.7, eB's -8.7); each where it was past its taper."""
        rows = roads_out(carriageways())
        for a, b, x in (("eA", "eB", -150.0), ("eB", "eC", 150.0)):
            ya, yb = at_x(rows, a, x)[0], at_x(rows, b, x)[0]
            self.assertAlmostEqual(ya, yb, delta=0.05, msg=(a, b))
            self.assertAlmostEqual(ya, -7.7, delta=0.05, msg=(a, b))
        self.assertAlmostEqual(at_x(rows, "eA", -250.0)[0], -6.7, delta=0.05)
        self.assertAlmostEqual(at_x(rows, "eB", 0.0)[0], -8.7, delta=0.05)
        self.assertAlmostEqual(at_x(rows, "eC", 250.0)[0], -6.7, delta=0.05)

    def test_one_width_joint_eased(self):
        """3 lanes all along, the middle piece placed: no taper; both centres run to the mean at the joints, eased
        (40 m each side), no step."""
        rows = roads_out(carriageways(outer=3))
        for a, b, x in (("eA", "eB", -150.0), ("eB", "eC", 150.0)):
            self.assertAlmostEqual(at_x(rows, a, x)[0], at_x(rows, b, x)[0], delta=0.05, msg=(a, b))
        ys = np.array([at_x(rows, "eA", x)[0] for x in np.arange(-260.0, -150.0, 2.0)]
                      + [at_x(rows, "eB", x)[0] for x in np.arange(-150.0, -40.0, 2.0)])
        self.assertLess(float(np.abs(np.diff(ys)).max()), 0.15, ys)
        self.assertAlmostEqual(ys[0], -6.7, delta=0.05)
        self.assertAlmostEqual(ys[-1], -8.7, delta=0.05)

    def test_shift_from_the_joints_own_cell(self):
        cells = carriageways(cells=2)
        rows = roads_out(cells)
        e = np.concatenate([edge(cells, rows, "eB", np.arange(100.0, 150.1, 2.0)),
                            edge(cells, rows, "eC", np.arange(152.0, 200.0, 2.0))])
        self.assertLess(float(np.abs(np.diff(e)).max()), 0.1, e)

    def test_no_lane_shift_on_a_placed_feature(self):
        """An exit ramp at the 3-lane piece's end: unplaced, lane_shifts shift its lines toward the ramp; placed, none
        (the placement says where the lanes are)."""
        for placed, want_rows in ((False, True), (True, False)):
            f = carriageways(placed=placed, ramp=True)[0]
            ids = [str(v) for v in f.ids]
            self.assertEqual(bool(roads.lane_shifts(f)[ids.index("eB")]), want_rows, placed)

    def test_unplaced_unchanged(self):
        cells = carriageways(placed=False)
        rows = roads_out(cells)
        for fid, sp, pts, _ in rows:
            self.assertTrue(np.allclose(np.abs(pts[:, 1]), 6.7, atol=0.05), fid)     # (the frame's own metres)


def east_with_deck(deck_placed):
    """The eastbound placed 3-lane piece (eB, -150 to 150) going on onto a 60 m bridge (eD, to 210), the bridge's way
    tagged as the piece's (deck_placed) or without a placement."""
    lines = [[(x, -6.7) for x in np.linspace(-150, 150, 31)], [(x, -6.7) for x in np.linspace(150, 210, 7)]]
    f = features(lines, ["motorway", "motorway"], ids=["eB", "eD"], flags=[(1, "is_bridge", 0.0, 1.0)])
    f.rules["oneway"] = rules([(0, 1.0), (1, 1.0)], "float")
    f.attrs["osm_lanes"] = np.array([3.0, 3.0])
    f.attrs[railtags.LANE_PARTS] = [[(0.0, 1.0, "eB", dict(PLACED))],
                                    [(0.0, 1.0, "eD", dict(PLACED) if deck_placed else {"lanes": "3", "oneway": "yes"})]]
    return f


def y_of(rows, fid, x):
    return at_x(rows, fid, x)[0]


class ConsumersTest(unittest.TestCase):
    """The roads' other readers follow a placed carriageway as the road tiles draw it (placement_context)."""

    def test_grading_follows(self):
        """The graded corridor's outer (south) side over the placed piece 2.0 m further south than unplaced."""
        import grading
        from test_grading import FakeMD

        def south_side(placed):
            g = grading.Grader(FRAME, FakeMD(carriageways(placed=placed)[0]),
                               lambda la, lo: np.full(np.shape(la), 10.0))
            ys = np.arange(-40.0, 0.0, 0.02)
            la, lo, _ = FRAME.enu_to_geodetic(np.zeros(len(ys)), ys, 0.0)
            d = np.abs(np.asarray(g.apply(la, lo, np.full(len(ys), 30.0)), np.float64) - 30.0)
            return float(ys[np.argmax(d > 0.5 * float(d.max()))])      # (the southmost point graded half way)
        self.assertAlmostEqual(south_side(False) - south_side(True), 2.0, delta=0.1)

    def test_traffic_lanes_follow(self):
        """navgraph's nodes on the placed piece 2.0 m south of where they are unplaced (its other pieces' as they
        were, away from the joints)."""
        import navgraph

        def nodes(placed):
            g = navgraph.build(FRAME, carriageways(placed=placed), flat, (0.0, 0.0))
            return {k: v for k, v in g["nodes"].items() if -100.0 <= v[0] <= 100.0 or v[0] <= -250.0}
        a, b = nodes(False), nodes(True)
        moved = [b[k][1] - a[k][1] for k in a if k.startswith("seB_") and k in b]
        kept = [b[k][1] - a[k][1] for k in a if k.startswith("seA_") and k in b]
        self.assertTrue(moved and kept)
        np.testing.assert_allclose(moved, -2.0, atol=0.1)
        np.testing.assert_allclose(kept, 0.0, atol=1e-6)

    def test_sign_pavements_follow(self):
        """signroads' pavement rows (the signs' road context): the placed piece's line 2.0 m south."""
        import signroads

        def line_y(placed):
            f = carriageways(placed=placed)[0]
            rows = signroads.Context(lambda key: f).pavements(FRAME, [[LON, LAT]])
            got = [(fid, sp, line, None) for fid, sp, line, _w in rows if fid == "eB"]
            self.assertTrue(got)
            return y_of(got, "eB", 0.0)
        self.assertAlmostEqual(line_y(False) - line_y(True), 2.0, delta=0.1)

    def test_deck_moves_with_its_road(self):
        """A deck tagged as its road: shifted as the road is at the abutment (-2 m, both); one without a placement
        stands, and the placed road runs to it (0 at the abutment)."""
        for deck_placed, want in ((True, -2.0), (False, 0.0)):
            f = east_with_deck(deck_placed)
            place = roads.placement_context([f])
            end = f.parts(0)[0][0][-1]
            ground = place.shift(f, 0, [end[0]], [end[1]], roads.road_specs(f)[0], "eB")
            self.assertAlmostEqual(float(ground[0]) if ground is not None else 0.0, want, delta=0.05, msg=deck_placed)
            deck = np.asarray(f.parts(1)[0][0], np.float64)
            e, n, _ = FRAME.geodetic_to_enu(deck[:, 1], deck[:, 0], 0.0)
            sh = roads.deck_shift_fn(FRAME, [f])("eD", np.c_[e, n])
            np.testing.assert_allclose(sh if sh is not None else np.zeros(len(e)), want, atol=0.05,
                                       err_msg=str(deck_placed))

    def test_deck_inside_the_joints_taper(self):
        """The placed piece's own bridge range starting 15 m past its joint with the 2-lane piece (inside the joint's
        48 m taper): the deck is shifted as the ground is at the abutment (the joint's easing reaches both)."""
        def piece(x0, x1):
            return [(x, -6.7) for x in np.linspace(x0, x1, int(abs(x1 - x0) / 5) + 1)]
        f = features([piece(-500, -150), piece(-150, 150)], ["motorway"] * 2, ids=["eA", "eB"],
                     flags=[(1, "is_bridge", 0.05, 0.6)])
        f.rules["oneway"] = rules([(0, 1.0), (1, 1.0)], "float")
        f.attrs["osm_lanes"] = np.array([2.0, 3.0])
        f.attrs[railtags.LANE_PARTS] = [[(0.0, 1.0, "eA", {"lanes": "2", "oneway": "yes"})],
                                        [(0.0, 1.0, "eB", dict(PLACED))]]
        place = roads.placement_context([f])
        g_end = np.asarray(roads.ground_parts(f, 1)[0])[-1]
        d_start = np.asarray(roads.bridge_parts(f, 1)[0])[0]
        ground = place.shift(f, 1, [g_end[0]], [g_end[1]], roads.road_specs(f)[1], "eB")
        e, n, _ = FRAME.geodetic_to_enu(np.array([d_start[1]]), np.array([d_start[0]]), 0.0)
        deck = roads.deck_shift_fn(FRAME, [f])("eB", np.c_[e, n])
        self.assertLess(float(ground[0]), -1.0)         # (inside the taper: on its way from -1 to -2)
        self.assertAlmostEqual(float(deck[0]), float(ground[0]), delta=0.05)

    def test_road_photo_follows(self):
        """surface._road_segments (the asphalt painted into the ground photo, roads.ROADS_IN_GROUND, and its road mask):
        the placed eastbound carriageway's segments 2 m south (pixel rows 1 m each, row 0 north)."""
        import surface

        def rows_at_0(placed):
            got = surface._road_segments(FRAME, -128.0, -128.0, 256.0, 256, 256, carriageways(placed=placed))
            segs = got[0]
            cross = segs[(np.minimum(segs[:, 0], segs[:, 2]) <= 128.0) & (np.maximum(segs[:, 0], segs[:, 2]) > 128.0)]
            return np.sort((cross[:, 1] + cross[:, 3]) / 2)
        a, b = rows_at_0(False), rows_at_0(True)
        self.assertEqual(len(a), 2)
        np.testing.assert_allclose(b - a, [-2.0, 2.0], atol=0.1)    # (westbound north, eastbound south)

    def test_ground_material_follows(self):
        """surface.paint_roads (the terrain's material under the wheels): the placed carriageway's asphalt reaches 2 m
        further south, its outer edge 17.7 m south of the middle (unplaced 15.7)."""
        import surface as sf
        n = 80
        rows_, cols = np.mgrid[0:n, 0:n]
        lat = LAT + (n / 2 - rows_) / M_LAT
        lon = LON + (cols - n / 2) / M_LON

        def south_edge(placed):
            out = sf.paint_roads(np.full(lat.shape, sf.GRASS, np.uint8), lat, lon, carriageways(placed=placed), 1.0)
            col = out[:, n // 2]
            paved = np.nonzero(col == sf.ASPHALT)[0]
            return float(n / 2 - paved.max())           # (m north of the middle; rows run south)
        self.assertAlmostEqual(south_edge(False) - south_edge(True), 2.0, delta=0.6)

    def test_ramp_paint_follows(self):
        """rampmerges.ground (the US exit-lane paint) at the placed piece's exit: its lane goes on into the ramp as
        unplaced (eB a main, lane_continuation), not cut off with a diagonal line."""
        import rampmerges

        def layout(placed):
            plans = rampmerges.ground(FRAME, carriageways(placed=placed, ramp=True))
            return [(sorted(p["mains"]), bool(p.get("lane_continuation"))) for p in plans]
        self.assertEqual(layout(True), layout(False))
        self.assertIn((["eB", "eC"], True), layout(False))

    def test_plans_format_bumped(self):
        import plancache
        self.assertGreaterEqual(plancache.PLANS_FORMAT, 26)


def line_y(ring, x):
    """y (frame metres) of a lon / lat ring at x."""
    e, n, _ = FRAME.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
    order = np.argsort(e)
    return float(np.interp(x, np.asarray(e)[order], np.asarray(n)[order]))


def east(pieces, flags=()):
    """Eastbound features [(id, x0, x1, lane-tag parts [(a, b, tags)], vertices)] along y = -6.7."""
    lines = [[(x, -6.7) for x in np.linspace(x0, x1, k)] for _fid, x0, x1, _parts, k in pieces]
    f = features(lines, ["motorway"] * len(pieces), ids=[p[0] for p in pieces], flags=list(flags))
    f.rules["oneway"] = rules([(k, 1.0) for k in range(len(pieces))], "float")
    f.attrs["osm_lanes"] = np.array([float(max(int((t or {}).get("lanes", 2)) for _a, _b, t in parts))
                                     for _f, _x0, _x1, parts, _k in pieces])
    f.attrs[railtags.LANE_PARTS] = [[(a, b, fid, t) for a, b, t in parts] for fid, _x0, _x1, parts, _k in pieces]
    return f


TWO = {"lanes": "2", "oneway": "yes"}
THREE = {"lanes": "3", "oneway": "yes"}


class ReviewTest(unittest.TestCase):
    """Codex's review of #1014 (2026-11-09)."""

    def test_sparse_vertices_sampled(self):
        """A 2-lane piece of two vertices 350 m apart meeting the placed piece: moved only within its taper (its
        middle where it was, not on a diagonal), and a placed stretch inside a 2-vertex feature is not lost."""
        f = east([("eA", -500.0, -150.0, [(0.0, 1.0, TWO)], 2), ("eB", -150.0, 150.0, [(0.0, 1.0, PLACED)], 31)])
        place = roads.placement_context([f])
        ring = place.ring(FRAME, f, 0, roads.ground_parts(f, 0)[0], roads.road_specs(f)[0], "eA")
        self.assertAlmostEqual(line_y(ring, -325.0), -6.7, delta=0.05)
        self.assertAlmostEqual(line_y(ring, -150.0), -7.7, delta=0.05)
        g = east([("eM", 0.0, 400.0, [(0.0, 0.3, THREE), (0.3, 0.7, PLACED), (0.7, 1.0, THREE)], 2)])
        ring = roads.placement_context([g]).ring(FRAME, g, 0, roads.ground_parts(g, 0)[0], roads.road_specs(g)[0])
        self.assertAlmostEqual(line_y(ring, 200.0), -8.7, delta=0.05)
        self.assertAlmostEqual(line_y(ring, 20.0), -6.7, delta=0.05)

    def test_placement_rows_follow_lane_enrichment(self):
        """A cell read before its lane shard joined, then enriched (railtags' join calls roadlanes.invalidate): the
        placement is seen."""
        import roadlanes
        f = east([("eB", -150.0, 150.0, [(0.0, 1.0, THREE)], 31)])
        self.assertFalse(roads.placement_context([f]).on)
        f.attrs[railtags.LANE_PARTS] = [[(0.0, 1.0, "eB", dict(PLACED))]]
        roadlanes.invalidate(f)
        self.assertTrue(roads.placement_context([f]).on)

    def test_oneway_minus_one_from_the_way_without_lane_parts(self):
        """A single-way feature (no LANE_PARTS: lane_tags' columns, which carry no oneway) whose way is oneway=-1, the
        Overture geometry along the way, travel against it: the shift goes to the right of the way (= of the
        geometry), as with LANE_PARTS."""
        f, xs, lon, lat = feature(parts=None)
        f.attrs[railtags.lane_attr("placement")] = np.array(["right_of:1"], object)
        f.attrs["osm_oneway"] = np.array(["-1"], object)
        sh = roads.placement_shift(f, 0, lon, lat, spec(d=-1))
        np.testing.assert_allclose(sh, -2.0, atol=1e-9)

    def test_lane_shifts_kept_where_the_part_is_unplaced(self):
        """The exit at the 3-lane piece's end, only its first 30 % placed: the joint's part is unplaced, so its lane
        shifts stay (the placement does not reach there)."""
        f = carriageways(placed=False, ramp=True)[0]
        ids = [str(v) for v in f.ids]
        k = ids.index("eB")
        f.attrs[railtags.LANE_PARTS][k] = [(0.0, 0.3, "eB", dict(PLACED)), (0.3, 1.0, "eB", dict(THREE))]
        self.assertTrue(roads.lane_shifts(f)[k])

    def test_tunnel_stands(self):
        """A placed piece with a tunnel inside (0.4..0.6) and one going on into a tunnel feature: the tunnel stays on
        its line, the ground runs to it at each portal; past the easing the full shift."""
        f = east([("eB", -150.0, 150.0, [(0.0, 1.0, PLACED)], 61), ("eT", 150.0, 250.0, [(0.0, 1.0, PLACED)], 21)],
                 flags=[(0, "is_tunnel", 0.4, 0.6), (1, "is_tunnel", 0.0, 1.0)])
        place = roads.placement_context([f])
        sp = roads.road_specs(f)[0]
        rings = [np.asarray(r) for r in roads.ground_parts(f, 0)]
        self.assertEqual(len(rings), 2)
        for ring, end in ((rings[0], -1), (rings[1], 0), (rings[1], -1)):
            sh = place.shift(f, 0, [ring[end, 0]], [ring[end, 1]], sp, "eB")
            self.assertAlmostEqual(float(sh[0]), 0.0, delta=0.05, msg=(end, ring[end]))
        lon0, lat0 = rings[0][0]
        self.assertAlmostEqual(float(place.shift(f, 0, [lon0], [lat0], sp, "eB")[0]), -2.0, delta=0.05)
        mid = f.parts(0)[0][0][30]                      # (x = 0, inside the tunnel: the AI's line there too)
        self.assertAlmostEqual(float(place.shift(f, 0, [mid[0]], [mid[1]], sp, "eB")[0]), 0.0, delta=0.05)


if __name__ == "__main__":
    unittest.main()
