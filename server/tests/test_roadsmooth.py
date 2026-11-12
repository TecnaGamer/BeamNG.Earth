"""Rounded bends of the road centrelines (roadsmooth.py)."""
import math
import os
import sys
import unittest
import unittest.mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import geodesy
import mapgeom
import roadsmooth


def _turn_deg(xy):
    d = np.diff(xy, axis=0)
    a = np.degrees(np.arctan2(d[:, 1], d[:, 0]))
    return np.abs((np.diff(a) + 180) % 360 - 180)


def _length(xy):
    return float(np.hypot(*np.diff(xy, axis=0).T).sum())


def _features(frame, lines, classes, rules=None):
    """A roads layer of the lines (lists of (e, n)) in frame, localized, with the given classes and rules."""
    geoms = []
    for ln in lines:
        e, n = np.array(ln, np.float64).T
        lat, lon, _ = frame.enu_to_geodetic(e, n, 0.0)
        geoms.append((mapgeom.LINE, [[np.stack([lon, lat], axis=1)]]))
    ids = np.array([f"id{i:03d}" for i in range(len(lines))])
    f = mapgeom.build("roads", ids, geoms)
    f.attrs["class"] = np.array(classes)
    f.attrs["subtype"] = np.array(["road"] * len(lines))
    f.rules = {"flags": mapgeom.RuleTable(*(np.array(c) for c in rules["flags"]))} if rules else {}
    return f.localize(frame)



# The NumPy original of the kernel (native/earthfast/src/roadsmooth.rs), kept as the oracle.
def _oracle_arc(vx, vy, ux, uy, wx, wy, delta, r, t):
    """The points of the arc replacing vertex (vx, vy) where the unit direction (ux, uy) turns to (wx, wy) by
    delta (signed radians) with radius r, tangent points t from the vertex: [(x, y)], first and last the tangents."""
    ax, ay = vx - ux * t, vy - uy * t                           # where the arc starts
    sgn = 1.0 if delta > 0 else -1.0
    nx, ny = -uy * sgn, ux * sgn                                # towards the centre (left of the line for a left turn)
    cx, cy = ax + nx * r, ay + ny * r
    a0 = math.atan2(ay - cy, ax - cx)
    mag = abs(delta)
    n = max(1, int(math.ceil(max(r * mag / roadsmooth.ARC_STEP_M, mag / math.radians(roadsmooth.ARC_STEP_DEG)))))
    pts = [(ax, ay)]
    for k in range(1, n):
        a = a0 + sgn * mag * k / n
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    pts.append((vx + wx * t, vy + wy * t))
    return pts


def oracle_smooth_line(xy, radius):
    """(new points (m, 2), knots (old distance, new distance) at the line's vertices and tangent points) of the line
    xy (k, 2) with its bends rounded; None when nothing changes. Ends and unrounded vertices keep their coordinates."""
    k = len(xy)
    if k < 3:
        return None
    d = np.diff(xy, axis=0)
    L = np.hypot(d[:, 0], d[:, 1])
    if L.min() < roadsmooth.MIN_PIECE_M or math.hypot(*(xy[0] - xy[-1])) < roadsmooth.CLOSED_M:
        return None
    u = d / L[:, None]
    cross = u[:-1, 0] * u[1:, 1] - u[:-1, 1] * u[1:, 0]
    delta = np.arctan2(cross, (u[:-1] * u[1:]).sum(axis=1))      # signed turn at vertices 1 .. k-2
    mag = np.abs(delta)
    rounded = (mag >= math.radians(roadsmooth.MIN_DEFLECT_DEG)) & (mag <= math.radians(roadsmooth.MAX_DEFLECT_DEG))
    if not rounded.any():
        return None
    isr = np.r_[False, rounded, False]                           # per vertex 0 .. k-1
    # the tangent length at vertex j (index j - 1 here): a piece between two rounded vertices gives each half, a rounded
    # vertex beside one that is not may take most of it (not all: its arc would end on that vertex, a duplicate point)
    share = np.minimum(np.where(isr[:-2], 0.5, roadsmooth.FREE_SHARE) * L[:-1], np.where(isr[2:], 0.5, roadsmooth.FREE_SHARE) * L[1:])
    half = np.tan(mag / 2.0)
    # the class's radius, or a larger one when it fills the pieces (a gentle bend of short pieces: the arcs then meet and
    # the curve is round all along, no straight stretch between them) and moves the line less than roadsmooth.MAX_SHIFT_M
    # (a corner's distance to its arc is r * (1 / cos(delta / 2) - 1))
    with np.errstate(divide="ignore", invalid="ignore"):
        r_fill = np.minimum(share / half, roadsmooth.MAX_SHIFT_M / (1.0 / np.cos(mag / 2.0) - 1.0))
    t = np.minimum(np.maximum(radius, np.nan_to_num(r_fill, nan=0.0, posinf=0.0)) * half, share)
    out = [(float(xy[0, 0]), float(xy[0, 1]))]
    old_s, new_s = [0.0], [0.0]
    s_old = np.r_[0.0, np.cumsum(L)]
    s_new = 0.0
    last = out[0]
    for j in range(1, k - 1):
        v = xy[j]
        if not rounded[j - 1] or t[j - 1] <= 1e-6:
            p = (float(v[0]), float(v[1]))
            s_new += math.hypot(p[0] - last[0], p[1] - last[1])
            out.append(p)
            last = p
            old_s.append(float(s_old[j]))
            new_s.append(s_new)
            continue
        tj = float(t[j - 1])
        dj = float(delta[j - 1])
        r = tj / math.tan(abs(dj) / 2.0)
        pts = _oracle_arc(float(v[0]), float(v[1]), float(u[j - 1, 0]), float(u[j - 1, 1]), float(u[j, 0]), float(u[j, 1]),
                   dj, r, tj)
        s_new += math.hypot(pts[0][0] - last[0], pts[0][1] - last[1])
        old_s.append(float(s_old[j]) - tj)
        new_s.append(s_new)
        for a, b in zip(pts[:-1], pts[1:]):
            s_new += math.hypot(b[0] - a[0], b[1] - a[1])
        if math.hypot(pts[0][0] - last[0], pts[0][1] - last[1]) < roadsmooth.MIN_PIECE_M:
            pts = pts[1:]                   # (the arc before took half of the same piece: this one starts where it ended)
        out.extend(pts)
        last = pts[-1]
        old_s.append(float(s_old[j]) + tj)
        new_s.append(s_new)
    p = (float(xy[-1, 0]), float(xy[-1, 1]))
    s_new += math.hypot(p[0] - last[0], p[1] - last[1])
    out.append(p)
    old_s.append(float(s_old[-1]))
    new_s.append(s_new)
    return np.asarray(out, np.float64), np.asarray(old_s), np.asarray(new_s)


class SmoothLineTest(unittest.TestCase):
    def test_right_angle_becomes_a_tangent_arc(self):
        xy = np.array([[0.0, 0.0], [100.0, 0.0], [100.0, 100.0]])
        pts, old_s, new_s = roadsmooth.smooth_line(xy, 25.0)
        np.testing.assert_allclose(pts[0], xy[0])
        np.testing.assert_allclose(pts[-1], xy[-1])
        self.assertTrue((_turn_deg(pts) <= roadsmooth.ARC_STEP_DEG + 1e-6).all())      # no corner left
        self.assertAlmostEqual(pts[:, 0].max(), 100.0, places=6)    # the pieces stay on their lines
        r = 25.0                                                   # the arc is a circle of that radius, centre (75, 25)
        arc = pts[(pts[:, 0] > 75.0 + 1e-6) & (pts[:, 1] < 25.0 - 1e-6)]
        np.testing.assert_allclose(np.hypot(arc[:, 0] - 75.0, arc[:, 1] - 25.0), r, atol=1e-9)
        # the arc cuts the corner: shorter than the corner's two pieces by 2 * r * (1 - pi / 4)
        self.assertAlmostEqual(_length(xy) - _length(pts), 2 * r - math.pi * r / 2, places=1)
        self.assertAlmostEqual(new_s[-1], _length(pts), places=9)

    def test_ends_and_straight_vertices_are_exact(self):
        xy = np.array([[0.0, 0.0], [50.0, 0.0], [100.0, 0.0], [150.0, 30.0], [200.0, 30.0]])
        pts, _, _ = roadsmooth.smooth_line(xy, 20.0)
        np.testing.assert_array_equal(pts[0], xy[0])
        np.testing.assert_array_equal(pts[-1], xy[-1])
        self.assertTrue(any((p == xy[1]).all() for p in pts))      # the vertex with no turn is kept as it was

    def test_short_pieces_limit_the_radius(self):
        # a 16 m piece turning 7 degrees at each vertex (the Vinnytsia road): the tangent points stay within the pieces
        a = np.radians(np.arange(0, 8) * 7.0)
        xy = np.cumsum(np.c_[np.r_[0.0, 16 * np.cos(a)], np.r_[0.0, 16 * np.sin(a)]], axis=0)
        pts, _, _ = roadsmooth.smooth_line(xy, 100.0)
        self.assertLess(_turn_deg(pts).max(), roadsmooth.ARC_STEP_DEG + 1e-6)
        # the arcs of the short pieces meet: no straight stretch between them (the pieces of the new line are its
        # arcs' steps, no longer than about ARC_STEP_M)
        self.assertLess(np.hypot(*np.diff(pts[1:-1], axis=0).T).max(), roadsmooth.ARC_STEP_M * 1.5)
        # the curve stays within a vertex's sagitta of the old line (no big shift)
        for p in pts:
            self.assertLess(np.min(np.hypot(*(xy - p).T)), 16.0)

    def test_a_larger_radius_only_where_the_line_hardly_moves(self):
        # 10 degrees between 100 m pieces: the arc fills them only as far as MAX_SHIFT_M allows
        xy = np.array([[0.0, 0.0], [100.0, 0.0], [100.0 + 100 * math.cos(math.radians(10)), 100 * math.sin(math.radians(10))]])
        pts, _, _ = roadsmooth.smooth_line(xy, 25.0)
        shift = np.hypot(*(pts - xy[1]).T).min()
        self.assertLessEqual(shift, roadsmooth.MAX_SHIFT_M + 1e-6)
        self.assertGreater(shift, 0.5)

    def test_the_road_mesh_keeps_the_arcs(self):
        # roads.simplify (RDP) before the mesh: at 0.25 m it put a vertex back every ~20 m of the Vinnytsia road's arc,
        # turning 5-6 degrees (the user, 2026-11-08: "still corners"); while on, its tolerance keeps the curve
        import roads
        a = np.radians(np.arange(0, 12) * 7.0)
        xy = np.cumsum(np.c_[np.r_[0.0, 16 * np.cos(a)], np.r_[0.0, 16 * np.sin(a)]], axis=0)
        pts, _, _ = roadsmooth.smooth_line(xy, 100.0)
        p3 = np.c_[pts, np.zeros(len(pts))]

        def corners(q):
            d = np.diff(q[:, :2], axis=0)
            s = np.hypot(d[:, 0], d[:, 1])
            return int(((_turn_deg(q[:, :2]) > 3.0) & (s[:-1] >= 5.0) & (s[1:] >= 5.0)).sum())
        was = roadsmooth.ON
        self.addCleanup(setattr, roadsmooth, "ON", was)
        roadsmooth.ON = False
        self.assertGreater(corners(roads.densify(roads.simplify(p3))), 0, "0.25 m: the corners come back")
        roadsmooth.ON = True
        self.assertEqual(corners(roads.densify(roads.simplify(p3))), 0)
        self.assertEqual(len(roads.simplify(p3, tol=roads.SIMPLIFY_M)), len(roads.simplify(p3, tol=0.25)))

    def test_neighbouring_arcs_do_not_overlap(self):
        xy = np.array([[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [20.0, 10.0], [20.0, 20.0]])      # a staircase of 10 m
        pts, _, _ = roadsmooth.smooth_line(xy, 100.0)
        d = np.hypot(*np.diff(pts, axis=0).T)
        self.assertGreater(d.min(), 1e-6)
        self.assertTrue((pts[:, 0] >= -1e-9).all() and (pts[:, 0] <= 20.0 + 1e-9).all())
        # (monotone: a doubling back would put the curve behind its own start)
        self.assertTrue((np.diff(pts[:, 0]) >= -1e-9).all() and (np.diff(pts[:, 1]) >= -1e-9).all())

    def test_nothing_to_do(self):
        self.assertIsNone(roadsmooth.smooth_line(np.array([[0.0, 0.0], [10.0, 0.0]]), 20.0))
        self.assertIsNone(roadsmooth.smooth_line(np.array([[0.0, 0.0], [10.0, 0.0], [20.0, 0.0]]), 20.0))
        ring = np.array([[0.0, 0.0], [50.0, 0.0], [50.0, 50.0], [0.0, 50.0], [0.0, 0.0]])
        self.assertIsNone(roadsmooth.smooth_line(ring, 20.0))                       # closed: a roundabout / a ring
        self.assertIsNone(roadsmooth.smooth_line(np.array([[0.0, 0.0], [10.0, 0.0], [10.0, 0.0], [10.0, 9.0]]), 20.0))
        hairpin = np.array([[0.0, 0.0], [50.0, 0.0], [0.0, 2.0]])                   # doubling back: left alone
        self.assertIsNone(roadsmooth.smooth_line(hairpin, 20.0))

    def test_deterministic(self):
        xy = np.array([[0.0, 0.0], [60.0, 5.0], [90.0, 40.0], [150.0, 45.0]])
        a, b = roadsmooth.smooth_line(xy, 40.0), roadsmooth.smooth_line(xy, 40.0)
        for x, y in zip(a, b):
            np.testing.assert_array_equal(x, y)


class DefaultsTest(unittest.TestCase):
    def test_on_by_default(self):
        import importlib
        keep = {k: os.environ.pop(k) for k in ("BNE_ROAD_SMOOTH", "BNE_ROAD_G2") if k in os.environ}
        try:
            m = importlib.reload(roadsmooth)
            self.assertTrue(m.ON and m.G2_ON)
            os.environ["BNE_ROAD_SMOOTH"] = "off"
            self.assertFalse(importlib.reload(roadsmooth).ON)
        finally:
            os.environ.pop("BNE_ROAD_SMOOTH", None)
            os.environ.update(keep)
            importlib.reload(roadsmooth)


class SmoothFeaturesTest(unittest.TestCase):
    def setUp(self):
        self.frame = geodesy.EnuFrame(49.04, 28.04, 0.0)
        was, roadsmooth.ON = roadsmooth.ON, True
        self.addCleanup(setattr, roadsmooth, "ON", was)
        rail, roadsmooth.RAIL_ON = roadsmooth.RAIL_ON, True       # (the rails' arcs: BNE_RAIL_CURVES=on)
        self.addCleanup(setattr, roadsmooth, "RAIL_ON", rail)

    def test_off_changes_nothing(self):
        roadsmooth.ON = False
        f = _features(self.frame, [[(0.0, 0.0), (100.0, 0.0), (100.0, 100.0)]], ["secondary"])
        before = f.lonlat.copy()
        roadsmooth.smooth(f, self.frame)
        np.testing.assert_array_equal(f.lonlat, before)

    def test_only_drivable_roads_and_the_layers_structure_holds(self):
        bend = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0)]
        f = _features(self.frame, [bend, bend, bend], ["secondary", "footway", "residential"])
        before = f.lonlat.copy()
        roadsmooth.smooth(f, self.frame)
        co = f.coord_off
        self.assertEqual(len(f.lonlat), len(f.en))
        self.assertEqual(co[-1], len(f.en))
        n = np.diff(co)
        self.assertGreater(n[0], 3)
        self.assertEqual(n[1], 3)                                  # a footway is not drawn: untouched
        self.assertGreater(n[2], 3)
        np.testing.assert_array_equal(f.lonlat[co[1]:co[2]], before[3:6])
        self.assertLess(n[0], 60)
        # lon / lat and en agree (the same arc in both)
        e, nn, _ = self.frame.geodetic_to_enu(f.lonlat[:, 1], f.lonlat[:, 0], 0.0)
        np.testing.assert_allclose(np.c_[e, nn], f.en, atol=1e-5)
        # the ends are bit for bit the map's
        np.testing.assert_array_equal(f.lonlat[co[0]], before[0])
        np.testing.assert_array_equal(f.lonlat[co[1] - 1], before[2])

    def test_a_railway_bend_is_rounded_with_its_class_radius(self):
        bend = [(0.0, 0.0), (400.0, 0.0), (400.0, 400.0)]
        f = _features(self.frame, [bend, bend, bend], ["standard_gauge", "subway", "tram"])
        f.attrs["subtype"] = np.array(["rail"] * 3)
        before = f.lonlat.copy()
        roadsmooth.smooth(f, self.frame)
        co = f.coord_off
        n = np.diff(co)
        self.assertGreater(n[0], 6, "a standard-gauge line's corner is an arc")
        self.assertEqual(n[1], 3, "a subway is left")
        self.assertEqual(n[2], 3, "a tram stays draped in rails.py as mapped")
        arc = f.en[co[0]:co[1]]
        self.assertLess(float(_turn_deg(arc).max()), 8.0, "no corner left in the line")
        np.testing.assert_array_equal(f.lonlat[co[0]], before[0])
        np.testing.assert_array_equal(f.lonlat[co[1] - 1], before[2])
        was = roadsmooth.RAIL_ON
        self.addCleanup(setattr, roadsmooth, "RAIL_ON", was)
        roadsmooth.RAIL_ON = False
        g = _features(self.frame, [bend], ["standard_gauge"])
        g.attrs["subtype"] = np.array(["rail"])
        roadsmooth.smooth(g, self.frame)
        self.assertEqual(len(g.en), 3, "BNE_RAIL_CURVES=off")

    def test_a_switch_keeps_the_vertices_where_a_branch_ends_on_the_line(self):
        main = [(0.0, 0.0), (300.0, 0.0), (330.0, 30.0), (600.0, 30.0)]
        branch = [(150.0, 0.4), (170.0, 60.0)]          # its end 0.4 m off the main line's first piece
        f = _features(self.frame, [main, branch], ["standard_gauge", "standard_gauge"])
        f.attrs["subtype"] = np.array(["rail"] * 2)
        roadsmooth.smooth(f, self.frame)
        co = f.coord_off
        line = f.en[co[0]:co[1]]
        mapped = np.array(main[1])
        self.assertLess(float(np.min(np.hypot(*(line - mapped).T))), 1e-6, "the switch's vertex stays")
        self.assertGreater(co[1] - co[0], 4, "the other corner is still rounded")
        g = _features(self.frame, [main], ["standard_gauge"])
        g.attrs["subtype"] = np.array(["rail"])
        roadsmooth.smooth(g, self.frame)
        self.assertGreater(float(np.min(np.hypot(*(g.en - mapped).T))), 0.05, "alone it is rounded there")
        with unittest.mock.patch.object(roadsmooth, "SWITCH_PIN_ON", False):
            h = _features(self.frame, [main, branch], ["standard_gauge", "standard_gauge"])
            h.attrs["subtype"] = np.array(["rail"] * 2)
            roadsmooth.smooth(h, self.frame)
            self.assertGreater(float(np.min(np.hypot(*(h.en[:h.coord_off[1]] - mapped).T))), 0.05, "BNE_RAIL_SWITCH_FIX=off")

    def test_rule_fractions_follow_the_line(self):
        self.addCleanup(setattr, roadsmooth, "G2_ON", roadsmooth.G2_ON)
        roadsmooth.G2_ON = False                # (the arcs' exact tangent point: G2 below)
        bend = [(0.0, 0.0), (200.0, 0.0), (200.0, 200.0)]
        # a bridge from 0.9 of the line (160 m up the second piece, past the arc's end at 150 m) to its end
        f = _features(self.frame, [bend], ["primary"], {"flags": ([0], ["is_bridge"], [0.9], [1.0])})
        roadsmooth.smooth(f, self.frame)
        rt = f.rules["flags"]
        pts = f.en
        s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(pts, axis=0).T))]
        self.assertAlmostEqual(float(rt.end[0]), 1.0)
        i = np.argmin(np.hypot(pts[:, 0] - 200.0, pts[:, 1] - 150.0))     # the arc's end, 10 m before the bridge's start
        self.assertLess(np.hypot(pts[i, 0] - 200.0, pts[i, 1] - 150.0), 1e-6)
        self.assertAlmostEqual(float(rt.start[0]) * s[-1], s[i] + 10.0, places=3)
        self.assertLess(float(rt.start[0]), 0.9)                          # the line got shorter, the bridge's share grew

    def test_g2_rule_fractions_follow_the_line(self):
        bend = [(0.0, 0.0), (200.0, 0.0), (200.0, 200.0)]
        # a bridge from 0.9 of the line (160 m up the second piece) to its end: it starts there on the new line too
        f = _features(self.frame, [bend], ["primary"], {"flags": ([0], ["is_bridge"], [0.9], [1.0])})
        roadsmooth.smooth(f, self.frame)
        rt = f.rules["flags"]
        pts = f.en
        s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(pts, axis=0).T))]
        at = float(rt.start[0]) * s[-1]
        q = np.array([np.interp(at, s, pts[:, 0]), np.interp(at, s, pts[:, 1])])
        self.assertLess(float(np.hypot(*(q - (200.0, 160.0)))), 0.3)
        self.assertAlmostEqual(float(rt.end[0]), 1.0)

    def test_g2_is_curvature_continuous_and_keeps_connectors(self):
        # a primary's mapped curve, chords turning unevenly, a side road joining at vertex 3 (an interior connector)
        a = np.radians(np.cumsum([0, 4, 7, 3, 6, 2, 5, 4]))
        xy = np.cumsum(np.c_[np.r_[0.0, 30 * np.cos(a)], np.r_[0.0, 30 * np.sin(a)]], axis=0)
        line = [tuple(p) for p in xy]
        s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
        f = _features(self.frame, [line], ["primary"])
        con = mapgeom.rule_table([(0, "c0", [0.0, 0.0]), (0, "c1", [s[3] / s[-1]] * 2), (0, "c2", [1.0, 1.0])], "text")
        f.rules["connectors"] = con
        before = f.lonlat.copy()
        roadsmooth.smooth(f, self.frame)
        out = f.en
        self.assertTrue(any((f.lonlat == before[3]).all(1)), "the connector's vertex stays bit for bit")
        np.testing.assert_array_equal(f.lonlat[0], before[0])
        np.testing.assert_array_equal(f.lonlat[-1], before[-1])

        def jumps(p):
            d = np.diff(p, axis=0)
            L = np.hypot(*d.T)
            h = np.unwrap(np.arctan2(d[:, 1], d[:, 0]))
            k = np.diff(h) / (0.5 * (L[:-1] + L[1:]))
            return float(np.abs(np.diff(k)).max())
        with unittest.mock.patch.object(roadsmooth, "G2_ON", False):
            g = _features(self.frame, [line], ["primary"])
            g.rules["connectors"] = mapgeom.rule_table([(0, "c0", [0.0, 0.0]), (0, "c1", [s[3] / s[-1]] * 2),
                                                        (0, "c2", [1.0, 1.0])], "text")
            roadsmooth.smooth(g, self.frame)
        self.assertLess(jumps(out), 0.5 * jumps(g.en))
        # the connector's fraction still points at its vertex
        c = f.rules["connectors"]
        s2 = np.r_[0.0, np.cumsum(np.hypot(*np.diff(out, axis=0).T))]
        at = float(c.start[1]) * s2[-1]
        q = np.array([np.interp(at, s2, out[:, 0]), np.interp(at, s2, out[:, 1])])
        e, n, _ = self.frame.geodetic_to_enu(before[3:4, 1], before[3:4, 0], 0.0)
        self.assertLess(float(np.hypot(q[0] - e[0], q[1] - n[0])), 0.05)

    def test_raceways_are_smoothed_and_rings_too(self):
        ring = [(60 * math.cos(t), 60 * math.sin(t)) for t in np.linspace(0, 2 * math.pi, 25)]
        ring[-1] = ring[0]
        f = _features(self.frame, [ring, [(0.0, 0.0), (100.0, 0.0), (190.0, 40.0)]], ["residential", "raceway"])
        before = f.lonlat.copy()
        roadsmooth.smooth(f, self.frame)
        n = np.diff(f.coord_off)
        self.assertGreater(n[0], 25, "a ring is rounded")
        self.assertGreater(n[1], 3, "a raceway is rounded")
        np.testing.assert_array_equal(f.lonlat[f.coord_off[1] - 1], before[24])

    def test_two_cells_agree_on_a_shared_road(self):
        line = [(0.0, 0.0), (80.0, 10.0), (120.0, 60.0), (200.0, 70.0)]
        out = []
        for fr in (geodesy.EnuFrame(49.04, 28.04, 0.0), geodesy.EnuFrame(49.043, 28.045, 0.0)):
            f = _features(self.frame, [line], ["tertiary"])
            f.localize(fr)
            roadsmooth.smooth(f, fr)
            out.append(f.lonlat.copy())
        self.assertEqual(out[0].shape, out[1].shape)
        np.testing.assert_allclose(out[0], out[1], atol=1e-8)


class KernelParityTest(unittest.TestCase):
    """The Rust kernel gives what the NumPy original gave, on random lines of every kind of bend."""

    def test_random_lines(self):
        rng = np.random.default_rng(7)
        for n in range(300):
            k = int(rng.integers(3, 40))
            step = rng.choice([2.0, 6.0, 16.0, 60.0])
            ang = np.cumsum(rng.normal(0.0, rng.choice([0.02, 0.1, 0.5, 1.2]), k))
            xy = np.cumsum(np.c_[np.cos(ang), np.sin(ang)] * step * rng.uniform(0.3, 1.5, (k, 1)), axis=0)
            if n % 7 == 0:
                xy[k // 2] = xy[k // 2 - 1]                  # a duplicate vertex
            radius = float(rng.choice([12.0, 25.0, 100.0, 300.0]))
            want, got = oracle_smooth_line(xy, radius), roadsmooth.smooth_line(xy, radius)
            if want is None:
                self.assertIsNone(got, n)
                continue
            self.assertIsNotNone(got, n)
            for a, b in zip(want, got):
                self.assertEqual(a.shape, b.shape, n)
                np.testing.assert_allclose(b, a, rtol=0, atol=1e-7, err_msg=str(n))


if __name__ == "__main__":
    unittest.main()
