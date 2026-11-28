"""parkedaircraft: an airliner parks nose-in at each occupied terminal gate (its axis across the facade, the nose
NOSE_GAP_M off it, the centre off the building) by a hash of the gate's id; its type follows the aerodrome's class; a
jet bridge sets the nose and puts the aircraft beside it, its forward left door at the bridge's end; none overlap each other, a building, a runway, a taxiway or a
taxilane's centreline; general-aviation aprons get rows of light aircraft; Features without gates or aprons, or a gate
without a terminal, give none; each aircraft is in exactly one tile."""
import json
import math
import os
import sys
import types
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import geodesy  # noqa: E402
import mapgeom  # noqa: E402
import parkedaircraft as pa  # noqa: E402

FRAME = geodesy.EnuFrame(-27.39, 153.118)
TERMINAL = [(-200, -40), (200, -40), (200, 0), (-200, 0)]          # the facade is y = 0, outward +y
BOX = (-400.0, -300.0, 400.0, 300.0)
_IDS = iter(range(10 ** 6))


def lonlat(pts):
    la, lo, _ = FRAME.enu_to_geodetic(np.array([p[0] for p in pts], np.float64),
                                      np.array([p[1] for p in pts], np.float64), 0.0)
    return np.stack([lo, la], 1)


def poly(pts):
    return (mapgeom.POLYGON, [[lonlat(list(pts) + [pts[0]])]])


def line(*pts):
    return (mapgeom.LINE, [[lonlat(pts)]])


def point(x, y):
    return (mapgeom.POINT, [[lonlat([(x, y)])]])


def feats(*rows, occupied=None):
    """Infrastructure Features of (geometry, class, tags dict[, name]) rows, subtype airport; ids unique over the tests,
    in row order. occupied: the Features' ids are drawn until every row from index 1 on is (True) / is not (False) one
    the occupancy hash fills."""
    while True:
        base = next(_IDS)
        ids = np.array([f"{base:06d}{i:03d}" for i in range(len(rows))])
        if occupied is None or all((pa._crc(i) % 100 < pa.OCCUPIED_PCT) == occupied for i in ids[1:]):
            break
    f = mapgeom.build("infrastructure", ids, [r[0] for r in rows])
    f.attrs = {"class": mapgeom.text_column([r[1] for r in rows]), "subtype": mapgeom.text_column(["airport"] * len(rows)),
               "tags": mapgeom.text_column([json.dumps(r[2] if len(r) > 2 else {}) for r in rows]),
               "name": mapgeom.text_column([r[3] if len(r) > 3 else "" for r in rows]),
               "surface": mapgeom.text_column([None] * len(rows))}
    return f


def buildings(*polys, prefix="b"):
    return mapgeom.build("buildings", np.array([f"{prefix}{i:03d}" for i in range(len(polys))]), [poly(p) for p in polys])


def parts(*polys):
    """Building parts Features (canopies, covered walkways: the footprints of raised parts count the same)."""
    return mapgeom.build("building_parts", np.array([f"p{i:03d}" for i in range(len(polys))]), [poly(p) for p in polys])


def aerodrome(cls="international_airport"):
    return (poly([(-1500, -1500), (1500, -1500), (1500, 1500), (-1500, 1500)]), cls, {})


def gate(x, y=-1.5):
    return (point(x, y), "airport_gate", {"aeroway": "gate"})


def en(c):
    e, n, _ = FRAME.geodetic_to_enu(np.array([c.lat]), np.array([c.lon]), 0.0)
    return float(e[0]), float(n[0])


def ground(lat, lon):
    return np.full(np.shape(lat), 5.0)


def gates_row(n=6, gap=80.0, cls="international_airport", **kw):
    xs = [-160.0 + i * gap for i in range(n)]
    return feats(aerodrome(cls), *[gate(x) for x in xs], **kw), xs


class Gates(unittest.TestCase):
    def test_nose_in_toward_the_facade_off_the_building(self):
        f, xs = gates_row(5, 80.0)
        got = pa.stands(f, buildings(TERMINAL))
        self.assertGreaterEqual(len(got), 2)
        for c in got:
            x, y = en(c)
            half = pa.SPECS[c.kind][0] / 2.0
            self.assertAlmostEqual(c.ne, 0.0, 2)
            self.assertAlmostEqual(c.nn, -1.0, 2)                       # the nose toward the terminal
            self.assertAlmostEqual(y, pa.NOSE_GAP_M + half, delta=0.6)   # the nose NOSE_GAP_M off the facade
            self.assertTrue(any(abs(x - gx) < 0.6 for gx in xs))
            self.assertGreater(y - half, 0.0)                           # off the building

    def test_occupancy_is_a_hash_of_the_id_and_deterministic(self):
        f, xs = gates_row(6, 80.0)
        a = pa.stands(f, buildings(TERMINAL))
        f2, _ = gates_row(6, 80.0)
        f2.ids = f.ids                                                  # (the same ids: the same hashes)
        self.assertEqual(a, pa.stands(f2, buildings(TERMINAL)))
        want = [x for gid, x in zip(f.ids[1:], xs) if pa._crc(str(gid)) % 100 < pa.OCCUPIED_PCT and abs(x) < 190.0]
        self.assertEqual(sorted(round(en(c)[0]) for c in a if abs(en(c)[0]) < 190.0), sorted(round(x) for x in want))   # (inside the terminal's length)
        full, _ = gates_row(6, 80.0, occupied=True)
        self.assertEqual(len(pa.stands(full, buildings(TERMINAL))), 6)
        empty, _ = gates_row(6, 80.0, occupied=False)
        self.assertEqual(pa.stands(empty, buildings(TERMINAL)), [])

    def test_no_overlaps_among_dense_gates(self):
        f = feats(aerodrome(), *[gate(-100.0 + i * 8.0) for i in range(20)])
        got = pa.stands(f, buildings(TERMINAL))
        self.assertGreater(len(got), 1)
        placed = [(c.kind,) + en(c) + (c.ne, c.nn) for c in got]
        for i, (k, x, y, nx, ny) in enumerate(placed):
            for k2, x2, y2, nx2, ny2 in placed[i + 1:]:
                pts = pa._world(pa._local_points(k2), x2, y2, nx2, ny2)
                self.assertFalse(pa._in_rects(k, x, y, nx, ny, pts, 0.0).any())

    def test_closely_mapped_gates_still_get_alternate_aircraft(self):
        f = feats(aerodrome(), *[gate(-60.0 + i * 12.0) for i in range(10)], occupied=True)
        got = pa.stands(f, buildings(TERMINAL))
        xs = sorted(en(c)[0] for c in got)
        self.assertGreaterEqual(len(got), 3)                            # (not only the first of a cluster)
        self.assertGreater(xs[-1] - xs[0], 60.0)
        self.assertLess(len(got), 10)                                   # (a wing span is wider than 12 m)

    def test_a_gate_in_a_pier_drawn_into_the_terminal_parks_beyond_its_tip(self):
        block = [(-200, -40), (200, -40), (200, 0), (6, 0), (6, 60), (-6, 60), (-6, 0), (-200, 0)]     # a 12 m pier, 60 m out
        f = feats(aerodrome("regional_airport"), gate(0.0, 55.0), occupied=True)
        got = pa.stands(f, buildings(block))
        self.assertEqual(len(got), 1)
        x, y = en(got[0])
        half = pa.SPECS[got[0].kind][0] / 2.0
        self.assertAlmostEqual(got[0].nn, -1.0, 2)                      # the nose toward the pier's tip
        self.assertAlmostEqual(y - half, 60.0 + pa.NOSE_GAP_M, delta=0.6)

    def test_a_gate_by_a_cell_edge_uses_the_neighbours_buildings(self):
        f, _ = gates_row(3, 100.0, occupied=True)
        self.assertEqual(pa.stands(f, buildings([(500, 500), (520, 500), (520, 520), (500, 520)])), [])
        f2, _ = gates_row(3, 100.0, occupied=True)
        self.assertEqual(len(pa.stands(f2, [None, buildings(TERMINAL)])), 3)       # (the cell's own: none; a neighbour's)

    def test_pavement_and_taxilane_centreline_drop_an_aircraft(self):
        rows = [aerodrome()] + [gate(x) for x in (-100.0, 0.0, 100.0)]
        base = feats(*rows, occupied=True)
        self.assertEqual(len(pa.stands(base, buildings(TERMINAL))), 3)
        for cls, tags in (("taxiway", {}), ("taxilane", {}), ("runway", {"width": "45"})):
            f = feats(*rows, (line((-300, 22), (300, 22)), cls, tags), occupied=True)     # through the fuselages
            self.assertEqual(pa.stands(f, buildings(TERMINAL)), [], cls)
        far = feats(*rows, (line((-300, 90), (300, 90)), "taxiway", {}), occupied=True)  # clear of the tails
        self.assertEqual(len(pa.stands(far, buildings(TERMINAL))), 3)

    def test_no_terminal_no_aircraft(self):
        f, _ = gates_row(5, occupied=True)
        self.assertEqual(pa.stands(f, None), [])
        self.assertEqual(pa.stands(f, buildings([(500, 500), (520, 500), (520, 520), (500, 520)])), [])

    def test_jet_bridge_puts_the_forward_left_door_at_its_end(self):
        f = feats(aerodrome("regional_airport"), gate(0.0), occupied=True)
        got = pa.stands(f, buildings(TERMINAL, [(-2, -3), (2, -3), (2, 30), (-2, 30)]))     # a bridge 30 m out
        self.assertEqual(len(got), 1)
        c = got[0]
        self.assertAlmostEqual(c.ne, 0.0, 2)
        self.assertAlmostEqual(c.nn, -1.0, 2)                                       # nose-in, across the wall
        self.assertEqual(c.mode, "gate")
        self.assertLess(door(c)[0], 0.5)                                            # the fuselage right of the bridge (the nose toward -y: -x)
        self.assertLess(end_face(door(c), (0.0, 30.0), (0.0, 1.0), 2.0), pa.BRIDGE_CAB_M)           # the door at the bridge's end (its cab)


def door(c, side=1.0):
    """The aircraft's forward left door (side -1: right) in the plane: DOOR_K of its length back from the nose, half its
    fuselage's width left."""
    x, y = en(c)
    length, fus = pa.SPECS[c.kind][0], pa.SPECS[c.kind][3]
    return (x + c.ne * (length / 2.0 - pa.DOOR_K * length) - c.nn * side * fus / 2.0,
            y + c.nn * (length / 2.0 - pa.DOOR_K * length) + c.ne * side * fus / 2.0)


def end_face(p, end, d, half):
    """The distance from the point p to a jet bridge's end face: the segment across it (half each way, square to its direction d)."""
    perp = np.array([-d[1], d[0]])
    t = float(np.clip((np.array(p) - end) @ perp, -half, half))
    return float(np.hypot(*(np.array(p) - (np.array(end) + perp * t))))


def fuselage(c):
    """The sample points of an aircraft's fuselage in the plane."""
    return footprint(c)[:pa._fuselage_n(c.kind)]


def bridge(root, end, half=2.0):
    """A jet bridge footprint: the rectangle round the root -> end line."""
    d = np.array(end, float) - root
    d /= np.hypot(*d)
    p = np.array([-d[1], d[0]]) * half
    return [tuple(np.array(root) + p), tuple(np.array(end) + p), tuple(np.array(end) - p), tuple(np.array(root) - p)]


class Bridges(unittest.TestCase):
    """A gate with a mapped jet bridge (airportroles): the pose is solved, the forward left door nearest the bridge's outer end of those
    where the aircraft's plan keeps a skin gap off every building and bridge but the bridge's cab."""
    REACH = pa.BRIDGE_CAB_M + pa.SKIN_GAP_M

    def assertNotOverlapping(self, c, ring, end, d):
        """The fuselage is not on the bridge's ring, and clear of it (a skin gap) but for its last BRIDGE_CAB_M (the cab)."""
        cut = pa._cut(np.array(ring, np.float64), np.array(end, np.float64), np.array(d, np.float64))
        pts = fuselage(c)
        self.assertFalse(pa._pip(cut[0], pts).any())
        self.assertGreater(pa._seg_dist(pts, cut[0], np.roll(cut[0], -1, axis=0)).min(), pa.SKIN_GAP_M - 0.15)

    def test_a_skewed_bridge_turns_the_axis_along_it_nose_still_toward_the_building(self):
        d = np.array([math.sin(math.radians(55.0)), math.cos(math.radians(55.0))])         # 55 degrees off the wall's normal
        root = np.array([0.0, -3.0])
        end = root + 30.0 * d
        f = feats(aerodrome("regional_airport"), gate(0.0), occupied=True)
        got = pa.stands(f, buildings(TERMINAL, bridge(root, end)))
        self.assertEqual(len(got), 1)
        c = got[0]
        yaw = math.degrees(math.acos(max(-1.0, min(1.0, float(c.ne * -d[0] + c.nn * -d[1])))))
        self.assertLessEqual(yaw, max(pa.BRIDGE_YAWS_DEG) + 0.1)                    # the axis along the bridge (within the search's turn) ...
        self.assertLess(float(c.ne * d[0] + c.nn * d[1]), 0.0)                      # ... the nose toward the terminal
        self.assertLess(end_face(door(c), end, d, 2.0), self.REACH)                 # the door at the bridge's end
        self.assertNotOverlapping(c, bridge(root, end), end, d)                     # (a radial bridge: not on it)

    def test_a_slightly_skewed_bridge_keeps_the_walls_normal_within_the_search_turn(self):
        d = np.array([math.sin(math.radians(20.0)), math.cos(math.radians(20.0))])
        root = np.array([0.0, -3.0])
        end = root + 30.0 * d
        f = feats(aerodrome("regional_airport"), gate(0.0), occupied=True)
        c = pa.stands(f, buildings(TERMINAL, bridge(root, end)))[0]
        self.assertLessEqual(abs(math.degrees(math.atan2(c.ne, -c.nn))), max(pa.BRIDGE_YAWS_DEG) + 0.1)     # (the wall's normal, turned at most 5 degrees)
        self.assertLess(end_face(door(c), end, d, 2.0), self.REACH)
        self.assertNotOverlapping(c, bridge(root, end), end, d)

    def test_the_pose_minimises_the_door_to_end_distance_a_skin_gap_off_the_bridge(self):
        ring = [(-2, -3), (2, -3), (2, 30), (-2, 30)]
        f = feats(aerodrome("regional_airport"), gate(0.0), occupied=True)
        c = pa.stands(f, buildings(TERMINAL, ring))[0]
        self.assertLess(math.hypot(door(c)[0], door(c)[1] - 30.0), 3.0)             # (the audit's check, not a placement constant)
        cut = pa._cut(np.array(ring, np.float64), np.array([0.0, 30.0]), np.array([0.0, 1.0]))
        gap = pa._seg_dist(fuselage(c), cut[0], np.roll(cut[0], -1, axis=0)).min()
        self.assertGreater(gap, pa.SKIN_GAP_M - 0.15)
        self.assertLess(gap, pa.SKIN_GAP_M + 0.35)                                  # (as near as the skin gap lets it)

    @mock.patch.object(pa, "_kinds_for", lambda tier, gid: ("narrow",))
    def test_a_small_obstacle_at_the_nose_the_door_slides_tail_ward_to_clear_it(self):
        ring = [(-2, -3), (2, -3), (2, 30), (-2, 30)]
        shed = [(-16, 10), (-4.5, 10), (-4.5, 26.5), (-16, 26.5)]                   # on the fuselage's side, by the nose
        free = pa.stands(feats(aerodrome("regional_airport"), gate(0.0), occupied=True), buildings(TERMINAL, ring))[0]
        got = pa.stands(feats(aerodrome("regional_airport"), gate(0.0), occupied=True), buildings(TERMINAL, ring, shed))
        self.assertEqual(len(got), 1)
        c = got[0]
        self.assertLess(door(free)[1], 30.8)                                        # (level with the end without it)
        self.assertGreater(door(c)[1], 31.0)                                        # (stepped back: +y is tail-ward)
        self.assertLess(end_face(door(c), (0.0, 30.0), (0.0, 1.0), 2.0), self.REACH)
        sh = np.array(shed, np.float64)
        self.assertGreater(pa._seg_dist(fuselage(c), sh, np.roll(sh, -1, axis=0)).min(), pa.SKIN_GAP_M - 0.15)
        self.assertFalse(pa._pip(sh, fuselage(c)).any())

    def test_no_mirror_where_the_left_door_cannot_meet_the_bridge_the_stand_stays_empty(self):
        ring = [(-2, -3), (2, -3), (2, 30), (-2, 30)]
        deep = [(-16, 10), (-2.5, 10), (-2.5, 36), (-16, 36)]                       # the fuselage cannot be right of the bridge
        f = feats(aerodrome("regional_airport"), gate(0.0), occupied=True)
        self.assertEqual(pa.stands(f, buildings(TERMINAL, ring, deep)), [])
        beside = [(4.5, 10), (16, 10), (16, 26.5), (4.5, 26.5)]                     # a building on the bridge's other side changes nothing
        got = pa.stands(feats(aerodrome("regional_airport"), gate(0.0), occupied=True), buildings(TERMINAL, ring, beside))
        self.assertEqual(len(got), 1)
        self.assertLess(en(got[0])[0], 0.0)                                         # (the fuselage right of the bridge, never left)

    def test_every_bridged_gate_of_a_row_gets_its_own_aircraft(self):
        xs = [-60.0, 0.0, 60.0]
        bridges = [[(x - 2, -3), (x + 2, -3), (x + 2, 30), (x - 2, 30)] for x in xs]
        f = feats(aerodrome("regional_airport"), *[gate(x) for x in xs], occupied=True)
        got = pa.stands(f, buildings(TERMINAL, *bridges))
        self.assertEqual(len(got), 3)
        for c, x in zip(sorted(got, key=lambda c: en(c)[0]), xs):
            self.assertLess(end_face(door(c), (x, 30.0), (0.0, 1.0), 2.0), self.REACH)
            self.assertLess(door(c)[0], x)

    def test_a_neighbours_bridge_is_an_obstacle_all_along_only_the_gates_own_cab_is_exempt(self):
        own = [(-2, -3), (2, -3), (2, 30), (-2, 30)]
        near = [(-12, -3), (-8, -3), (-8, 30), (-12, 30)]                           # another gate's bridge, where the fuselage would stand
        f = feats(aerodrome("regional_airport"), gate(0.0), occupied=True)
        nr = np.array(near, np.float64)
        for k in pa.stands(f, buildings(TERMINAL, own, near)):
            self.assertFalse(pa._pip(nr, fuselage(k)).any())
            self.assertGreater(pa._seg_dist(fuselage(k), nr, np.roll(nr, -1, axis=0)).min(), pa.SKIN_GAP_M - 0.15)

    def test_only_the_cab_of_the_bridge_is_exempt_another_building_is_not(self):
        f = feats(aerodrome("regional_airport"), gate(0.0), occupied=True)
        bridge0 = [(-2, -3), (2, -3), (2, 30), (-2, 30)]
        self.assertEqual(len(pa.stands(f, buildings(TERMINAL, bridge0))), 1)
        shed = [(-6, 40), (6, 40), (6, 120), (-6, 120)]                                 # a 12 x 80 m building on the tail
        self.assertEqual(pa.stands(f, buildings(TERMINAL, bridge0, shed)), [])


class Centreline(unittest.TestCase):
    """A gate mapped on a pier's centreline: both sides are tried, the side whose stand is clear kept; where both are, the
    side away from the nearest stand kept."""
    PIER = [(-200, -40), (200, -40), (200, 0), (6, 0), (6, 100), (-6, 100), (-6, 0), (-200, 0)]    # 12 m wide, 100 m out

    def test_both_sides_clear_a_pair_of_gates_alternates_sides(self):
        f = feats(aerodrome("regional_airport"), gate(0.0, 40.0), gate(0.0, 70.0), occupied=True)
        got = pa.stands(f, buildings(self.PIER))
        self.assertEqual(len(got), 2)
        xs = sorted(en(c)[0] for c in got)
        self.assertLess(xs[0], -6.0)
        self.assertGreater(xs[1], 6.0)                                              # one each side
        for c in got:
            self.assertAlmostEqual(abs(c.ne), 1.0, 2)                               # across the pier's wall, nose-in

    def test_the_side_that_is_clear_is_the_one_kept(self):
        hangar = [(-60, 20), (-30, 20), (-30, 60), (-60, 60)]                       # on the west side
        for blocked, sign in ((hangar, 1.0), ([(-x, y) for x, y in hangar], -1.0)):
            f = feats(aerodrome("regional_airport"), gate(0.0, 40.0), occupied=True)
            got = pa.stands(f, buildings(self.PIER, blocked))
            self.assertEqual(len(got), 1)
            self.assertGreater(en(got[0])[0] * sign, 6.0)
            self.assertAlmostEqual(got[0].ne, -sign, 2)

    def test_a_gate_just_inside_a_wide_terminals_facade_stays_on_that_side(self):
        f = feats(aerodrome(), gate(-60.0), gate(60.0), occupied=True)
        got = pa.stands(f, buildings(TERMINAL))
        self.assertEqual(len(got), 2)
        for c in got:
            self.assertAlmostEqual(c.nn, -1.0, 2)
            self.assertGreater(en(c)[1], 0.0)


class Pitch(unittest.TestCase):
    """Gates closer than a wide-body's span: each stand's kind is the largest (wide, narrow, turboprop) that fits."""

    def test_pitch_ok_is_the_span_and_a_margin_beside_a_stand_kept(self):
        world = pa._World(types.SimpleNamespace(lines=[]), [])
        world.add("narrow", 0.0, 30.0, 0.0, -1.0)
        need = (pa.SPECS["narrow"][1] + pa.SPECS["wide"][1]) / 2.0 + pa.PITCH_GAP_M / 2.0 + 0.5
        self.assertFalse(world.pitch_ok("wide", 40.0, 30.0, 0.0, -1.0))
        self.assertTrue(world.pitch_ok("wide", need, 30.0, 0.0, -1.0))
        self.assertFalse(world.pitch_ok("narrow", 30.0, 30.0, 0.0, -1.0))
        self.assertTrue(world.pitch_ok("narrow", 40.0, 30.0, 0.0, -1.0))
        self.assertFalse(world.pitch_ok("turboprop", 32.0, 30.0, 0.0, -1.0))
        self.assertTrue(world.pitch_ok("turboprop", 34.0, 30.0, 0.0, -1.0))
        self.assertTrue(world.pitch_ok("wide", 40.0, 130.0, 0.0, -1.0))            # (a stand well behind: not beside)
        self.assertTrue(world.pitch_ok("wide", 0.0, 30.0, 1.0, 0.0))               # (an axis across: the clearance test's)

    def test_a_close_gate_takes_a_smaller_kind_instead_of_none(self):
        pitch = 45.0
        while True:
            f = feats(aerodrome(), gate(0.0), gate(pitch), occupied=True)
            k0, k1 = (pa._kinds_for("intl", str(i))[0] for i in f.ids[1:])
            if k0 != "wide" and k1 == "wide":                                       # (the second one's first choice: a wide)
                break
        got = sorted(pa.stands(f, buildings(TERMINAL)), key=lambda c: en(c)[0])
        self.assertEqual(len(got), 2)
        self.assertEqual(got[1].kind, "narrow")
        self.assertGreaterEqual(en(got[1])[0] - en(got[0])[0] - (pa.SPECS[got[0].kind][1] + pa.SPECS["narrow"][1]) / 2.0,
                                pa.PITCH_GAP_M / 2.0 - 0.1)

    def test_wide_stays_a_cap_by_the_aerodromes_class(self):
        f = feats(aerodrome("regional_airport"), gate(-200.0), gate(0.0), gate(200.0), occupied=True)
        self.assertNotIn("wide", {c.kind for c in pa.stands(f, buildings(TERMINAL))})


def footprint(c):
    """The sample points of an aircraft's rectangles in the plane."""
    x, y = en(c)
    return pa._world(pa._local_points(c.kind), x, y, c.ne, c.nn)


class Orientation(unittest.TestCase):
    def slanted(self, deg, tags=None):
        """The one aircraft at a gate just inside a terminal wall `deg` to the x axis, the wall's direction and normal."""
        a = math.radians(deg)
        rot = lambda x, y: (x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a))
        gx, gy = rot(0.0, -1.5)
        row = (point(gx, gy), "airport_gate", dict({"aeroway": "gate"}, **(tags or {})))
        f = feats(aerodrome("regional_airport"), row, occupied=True)
        got = pa.stands(f, buildings([rot(*p) for p in TERMINAL]))
        self.assertEqual(len(got), 1)
        return got[0], (math.cos(a), math.sin(a)), (-math.sin(a), math.cos(a))

    def test_the_axis_is_perpendicular_to_the_wall_and_the_nose_at_it(self):
        for deg in (0.0, 33.0, 100.0, -57.0):
            c, w, nrm = self.slanted(deg)
            self.assertAlmostEqual(c.ne * w[0] + c.nn * w[1], 0.0, 2, deg)               # across the wall
            self.assertAlmostEqual(c.ne * nrm[0] + c.nn * nrm[1], -1.0, 2, deg)          # the nose toward it, the tail away
            x, y = en(c)
            half = pa.SPECS[c.kind][0] / 2.0
            self.assertGreater((x - c.ne * half) * nrm[0] + (y - c.nn * half) * nrm[1], 0.0)

    def test_a_corner_does_not_turn_the_aircraft_sideways(self):
        ring = np.array([(-200.0, -60.0), (60.0, -60.0), (60.0, -20.0), (0.0, -20.0), (0.0, 0.0), (-200.0, 0.0)])
        _q, smooth = pa._facade(ring, np.array([6.0, -12.0]))           # (the long edges outweigh a short wall in a notch)
        self.assertLess(abs(smooth[0]), 0.1)
        term = pa._terminal([(ring, (-200.0, -60.0, 60.0, 0.0))], 6.0, -12.0)
        self.assertGreater(term[1][0][1][0], 0.99)                      # the nearest wall's (x = 0) normal comes first

    def test_a_tagged_heading_is_preferred_where_it_points_at_the_wall(self):
        c, _w, _n = self.slanted(0.0, {"heading": "200"})
        b = math.radians(200.0)
        self.assertAlmostEqual(c.ne, math.sin(b), 2)
        self.assertAlmostEqual(c.nn, math.cos(b), 2)
        away, _w, _n = self.slanted(0.0, {"heading": "0"})              # (nose away from the wall: not used)
        self.assertAlmostEqual(away.nn, -1.0, 2)

    def test_remote_stand_lines_up_with_the_nearest_taxilane(self):
        lane = (line((-200, 60), (200, 60)), "taxilane", {})
        got = pa.stands(feats(aerodrome("regional_airport"), gate(0.0, 100.0), lane, occupied=True), None)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].mode, "remote")
        self.assertAlmostEqual(got[0].ne, 0.0, 2)                       # across the lane, the nose away from it
        self.assertAlmostEqual(got[0].nn, 1.0, 2)
        below = pa.stands(feats(aerodrome("regional_airport"), gate(0.0, 20.0), lane, occupied=True), None)
        self.assertAlmostEqual(below[0].nn, -1.0, 2)
        stub = (line((0, 40), (0, 70)), "taxilane", {})                 # (a lane ending at the stand: along it, on)
        c = pa.stands(feats(aerodrome("regional_airport"), gate(0.0, 90.0), stub, occupied=True), None)[0]
        self.assertAlmostEqual(c.nn, 1.0, 2)


class Buildings(unittest.TestCase):
    CANOPY = [(-12, 6), (12, 6), (12, 12), (-12, 12)]

    def gate_with(self, *blds, f=None):
        f = f or feats(aerodrome("regional_airport"), gate(0.0), occupied=True)
        return pa.stands(f, [buildings(TERMINAL)] + list(blds))

    def test_a_canopy_or_walkway_part_pulls_the_aircraft_back(self):
        base = self.gate_with()
        self.assertEqual(len(base), 1)
        for blds in ([parts(self.CANOPY)], [buildings(self.CANOPY, prefix="c")]):
            got = self.gate_with(*blds)
            self.assertEqual(len(got), 1)
            c = got[0]
            x, y = en(c)
            half = pa.SPECS[c.kind][0] / 2.0
            self.assertGreaterEqual(y - half, 12.0 + pa.BUILDING_CLEAR_M - 0.05)        # the nose clear of the canopy
            self.assertLessEqual(y, en(base[0])[1] + pa.PULL_BACK_MAX_M + 0.05)
            self.assertAlmostEqual(c.nn, -1.0, 2)                                       # the same axis, moved tail-ward
            self.assertAlmostEqual(x, en(base[0])[0], 1)

    def test_no_aircraft_overlaps_any_building_or_part(self):
        sheds = [[(-30, 20), (-26, 20), (-26, 24), (-30, 24)], [(25, 30), (28, 30), (28, 33), (25, 33)]]   # (under 300 m2)
        f = feats(aerodrome("regional_airport"), *[gate(x) for x in (-60.0, 0.0, 60.0)], occupied=True)
        got = pa.stands(f, [buildings(TERMINAL, *sheds), parts(self.CANOPY)])
        self.assertTrue(got)
        for c in got:
            pts = footprint(c)
            for r in sheds + [self.CANOPY]:
                r = np.array(r, np.float64)
                self.assertFalse(pa._pip(r, pts).any())
                self.assertGreater(pa._seg_dist(pts, r, np.roll(r, -1, axis=0)).min(), pa.BUILDING_CLEAR_M - 0.05)

    def test_too_deep_to_pull_back_drops_it(self):
        self.assertEqual(self.gate_with(parts([(-12, 6), (12, 6), (12, 40), (-12, 40)])), [])

    def test_the_gates_own_bridge_is_no_obstacle_but_a_neighbours_is(self):
        f = feats(aerodrome("regional_airport"), gate(0.0), occupied=True)
        bridge = [(-2, -3), (2, -3), (2, 30), (-2, 30)]
        self.assertEqual(len(pa.stands(f, [buildings(TERMINAL), parts(bridge)])), 1)       # (beside it: the cab at the door)
        f2 = feats(aerodrome("regional_airport"), gate(0.0), occupied=True)       # (a long walkway by the wing tips: a wall)
        self.assertEqual(pa.stands(f2, buildings(TERMINAL, [(14, -3), (18, -3), (18, 80), (14, 80)])), [])

    def test_a_small_shed_on_the_apron_is_kept_off(self):
        apron = [(-150, 100), (150, 100), (150, 200), (-150, 200)]
        f = feats(aerodrome("regional_airport"), (poly(apron), "apron", {}))
        free = pa.stands(f, None)
        sheds = []
        for c in free[::2]:
            x, y = en(c)
            sheds.append([(x - 1.5, y - 1.5), (x + 1.5, y - 1.5), (x + 1.5, y + 1.5), (x - 1.5, y + 1.5)])
        got = pa.stands(f, buildings(*sheds))
        self.assertLess(len(got), len(free))
        for c in got:
            for r in sheds:
                self.assertFalse(pa._pip(np.array(r), footprint(c)).any())

    def test_a_narrow_bridge_by_the_gate_is_no_obstacle_to_its_aircraft(self):
        f = feats(aerodrome("regional_airport"), gate(0.0), occupied=True)         # (the ids fix the type: the same one)
        base = self.gate_with(f=f)
        bridge = [(8, 20), (12, 20), (12, 32), (8, 32)]                    # (4 x 12 m, off the gate: by the wing)
        shed = [(8, 20), (16, 20), (16, 26), (8, 26)]                      # (8 x 6 m: a building)
        got = self.gate_with(buildings(bridge, prefix="n"), f=f)
        self.assertEqual(len(got), 1)
        self.assertAlmostEqual(en(got[0])[1], en(base[0])[1], 1)           # (where it stood without it)
        moved = self.gate_with(buildings(shed, prefix="s"), f=f)
        self.assertEqual(len(moved), 1)
        self.assertGreater(en(moved[0])[1], en(base[0])[1] + 1.0)          # (pulled back from the shed)
        far = [(8 + 60, 20), (12 + 60, 20), (12 + 60, 32), (8 + 60, 32)]
        sc = pa._scene(feats(aerodrome("regional_airport"), gate(0.0)))
        obstacles, _small = pa._buildings(sc, buildings(bridge, shed, far))
        self.assertEqual(sorted(obstacles.narrow), [0, 2])                 # (the shed is not narrow)

    def test_a_raised_part_is_no_obstacle_to_the_wings_but_to_the_fuselage(self):
        f = feats(aerodrome("regional_airport"), gate(0.0), occupied=True)
        base = self.gate_with(f=f)
        c = en(base[0])
        wing = [(8, c[1] - 3), (16, c[1] - 3), (16, c[1] + 3), (8, c[1] + 3)]          # over a wing, beside the fuselage
        body = [(-3, c[1] - 3), (3, c[1] - 3), (3, c[1] + 3), (-3, c[1] + 3)]          # over the fuselage

        def raised(ring, height):
            p = parts(ring)
            p.attrs["min_height"] = np.array([height])
            return p
        self.assertAlmostEqual(en(self.gate_with(raised(wing, 4.0), f=f)[0])[1], c[1], 1)
        self.assertGreater(en(self.gate_with(raised(wing, 0.0), f=f)[0])[1], c[1] + 1.0)
        got = self.gate_with(raised(body, 4.0), f=f)
        self.assertTrue(not got or en(got[0])[1] > c[1] + 1.0)             # (the fuselage still meets it)

    def test_kept_stands_is_what_tile_rows_places(self):
        f, _ = gates_row(4, 80.0, occupied=True)
        got = pa.kept_stands([0], lambda k: f, lambda k: [buildings(TERMINAL), None])
        self.assertEqual(len(got), 4)
        rows = pa.tile_rows(FRAME, BOX, lambda k: f, lambda k: buildings(TERMINAL), ground, keys=[0])
        self.assertEqual(len([r for r in rows if r[0] in pa.GROUPS.values()]), 4)


class Types(unittest.TestCase):
    def kinds(self, cls, gap=9.0, n=40):
        f = feats(aerodrome(cls), *[gate(-190.0 + i * gap) for i in range(n)])
        return {c.kind for c in pa.stands(f, buildings(TERMINAL))}

    def test_by_aerodrome_class(self):
        intl = self.kinds("international_airport")
        self.assertIn("narrow", intl)                                           # (wide a quarter; a tight stand: smaller)
        self.assertLessEqual(intl, {"narrow", "wide", "turboprop"})
        self.assertEqual(self.kinds("regional_airport"), {"turboprop", "narrow"})
        self.assertEqual(self.kinds("private_airport"), {"light"})
        self.assertEqual(self.kinds("military_airport"), set())
        self.assertEqual(self.kinds("heliport"), set())

    def test_wide_a_quarter_by_hash_and_a_narrow_where_it_does_not_fit(self):
        found = set()
        for _ in range(6):
            f, _ = gates_row(4, 120.0)
            found |= {c.kind for c in pa.stands(f, buildings(TERMINAL))}
        self.assertLessEqual(found, {"narrow", "wide"})
        self.assertIn("wide", found)
        wide = [i for i in (f"{k:09d}" for k in range(2000)) if pa._kinds_for("intl", i)[0] == "wide"]
        self.assertAlmostEqual(len(wide) / 2000.0, 0.25, delta=0.04)
        tight = feats(aerodrome(), gate(0.0), occupied=True)
        tight.ids = np.array([tight.ids[0], wide[0]])                    # (a wide's gate: the terminal is 40 m deep)
        got = pa.stands(tight, buildings(TERMINAL, [(-45, 15), (45, 15), (45, 40), (-45, 40)]))
        self.assertTrue(all(c.kind != "wide" for c in got))


class Apron(unittest.TestCase):
    APRON = [(-150, 100), (150, 100), (150, 200), (-150, 200)]

    def test_light_aircraft_in_rows_on_a_non_international_apron(self):
        f = feats(aerodrome("regional_airport"), (poly(self.APRON), "apron", {}))
        got = pa.stands(f, None)
        self.assertTrue(0 < len(got) <= pa.GA_MAX)
        self.assertEqual({c.kind for c in got}, {"light"})
        for c in got:
            x, y = en(c)
            self.assertTrue(-150 < x < 150 and 100 < y < 200)
            d = min(abs(x - 150), abs(x + 150), abs(y - 100), abs(y - 200))
            self.assertAlmostEqual(d, pa.SPECS["light"][0] / 2.0 + pa.GA_INSET_M, delta=0.6)    # the nose to the edge
        for i, a in enumerate(got):
            for b in got[i + 1:]:
                self.assertGreater(math.hypot(en(a)[0] - en(b)[0], en(a)[1] - en(b)[1]), 7.0)

    def test_international_apron_has_none_unless_tagged_general_aviation(self):
        plain = feats(aerodrome(), (poly(self.APRON), "apron", {}, "Domestic Apron"))
        self.assertEqual(pa.stands(plain, None), [])
        ga = feats(aerodrome(), (poly(self.APRON), "apron", {}, "General Aviation Apron"))
        self.assertTrue(0 < len(pa.stands(ga, None)) <= pa.GA_MAX)

    def test_a_gates_aircraft_stands_on_the_paving(self):
        def run(apron):
            f = feats(aerodrome("regional_airport"), (poly(apron), "apron", {}), gate(0.0), occupied=True)
            return [c for c in pa.stands(f, buildings(TERMINAL)) if c.mode == "gate"]
        free = pa.stands(feats(aerodrome("regional_airport"), gate(0.0), occupied=True), buildings(TERMINAL))
        self.assertEqual(len(free), 1)                                  # (a cell that maps no apron: not asked)
        self.assertEqual(len(run([(-60, 0), (60, 0), (60, 80), (-60, 80)])), 1)         # (the whole stand on it)
        self.assertEqual(run([(-60, 0), (60, 0), (60, 14), (-60, 14)]), [])             # (the main gear on the grass)
        self.assertEqual(run([(-60, 40), (60, 40), (60, 90), (-60, 90)]), [])           # (the nose gear on the grass)

    def test_a_terminal_stands_gear_may_be_in_the_unmapped_strip_between_its_wall_and_the_apron_never_beyond_it(self):
        """Mappers stop an apron short of the wall: a leg in the strip (nearer the wall than the apron, within PAVING_GAP_M of it) is on
        paving; one beyond the apron, away from the wall, is on grass however near the apron it is."""
        def run(apron):
            f = feats(aerodrome("regional_airport"), (poly(apron), "apron", {}), gate(0.0), occupied=True)
            return [c for c in pa.stands(f, buildings(TERMINAL)) if c.mode == "gate"]
        strip = [(-60, 14), (60, 14), (60, 80), (-60, 80)]                  # (the apron starts 14 m off the wall: the nose gear, 9 m out, in the strip)
        self.assertEqual(len(run(strip)), 1)
        old, pa.PAVING_GAP_M = pa.PAVING_GAP_M, 0.0
        try:
            self.assertEqual(run(strip), [])                                # (no tolerance: that leg is on the grass)
        finally:
            pa.PAVING_GAP_M = old
        self.assertEqual(run([(-60, 0), (60, 0), (60, 14), (-60, 14)]), [])     # (the main gear 8 m past the apron's far edge: 8 m is within the gap)
        self.assertEqual(run([(-60, 70), (60, 70), (60, 140), (-60, 140)]), [])     # (further than the gap)

    def test_a_hangar_on_the_apron_is_kept_off(self):
        hangar = [(-150, 100), (-60, 100), (-60, 140), (-150, 140)]
        f = feats(aerodrome("regional_airport"), (poly(self.APRON), "apron", {}))
        free = pa.stands(f, None)
        got = pa.stands(f, buildings(hangar))
        self.assertLess(len(got), len(free))
        for c in got:
            x, y = en(c)
            self.assertFalse(-155 < x < -55 and 95 < y < 145)


class WallLines(unittest.TestCase):
    """A stepped, diagonal, curved or corner wall: the aircraft is nose-in to the wall's general line, not to the step it stands at."""

    @staticmethod
    def stair(n=10, step=8.0):
        """A building whose lower-right wall is a staircase up the diagonal y = x (treads and risers of `step` m)."""
        pts = [(0.0, 0.0)]
        for k in range(n):
            pts += [((k + 1) * step, k * step), ((k + 1) * step, (k + 1) * step)]
        return pts[:-1] + [(n * step, n * step), (n * step - 40.0, n * step + 40.0), (-40.0, 40.0)]

    def angle(self, a, b):
        return math.degrees(math.acos(max(-1.0, min(1.0, float(a[0] * b[0] + a[1] * b[1])))))

    def test_a_stepped_walls_normal_is_the_general_lines_not_the_treads(self):
        ring = np.array(self.stair(), np.float64)
        for q in ((44.0, 40.0), (46.0, 40.0), (48.0, 40.0), (48.0, 44.0)):        # (on a tread, on a riser)
            n = pa.wall_normal(ring, np.array(q))
            self.assertIsNotNone(n, q)
            self.assertLess(self.angle(n, (math.sqrt(0.5), -math.sqrt(0.5))), 8.0, q)

    def test_a_gate_on_a_stepped_wall_parks_across_the_general_line(self):
        f = feats(aerodrome(), (point(46.0, 36.0), "airport_gate", {"aeroway": "gate"}), occupied=True)
        got = [c for c in pa.stands(f, buildings(self.stair())) if c.mode == "gate"]
        self.assertEqual(len(got), 1)
        self.assertLess(self.angle((got[0].ne, got[0].nn), (-math.sqrt(0.5), math.sqrt(0.5))), 10.0)    # (the nose to the building)

    def test_a_curved_walls_normal_is_radial(self):
        ang = np.radians(np.arange(0.0, 360.0, 15.0))
        ring = np.stack([60.0 * np.cos(ang), 60.0 * np.sin(ang)], 1)
        for k in (0, 5, 11, 17):
            n = pa.wall_normal(ring, ring[k])
            self.assertIsNotNone(n)
            self.assertLess(self.angle(n, (math.cos(ang[k]), math.sin(ang[k]))), 10.0)

    def test_near_a_corner_a_long_edge_keeps_its_own_normal(self):
        ring = np.array([(0.0, 0.0), (22.0, 0.0), (22.0, 300.0), (0.0, 300.0)])
        for x in (4.0, 11.0):                                                      # (the pier's tip, 4 m from its corner)
            n = pa.wall_normal(ring, np.array([x, 300.0]))
            self.assertIsNotNone(n)
            self.assertLess(self.angle(n, (0.0, 1.0)), 5.0)
        self.assertLess(self.angle(pa.wall_normal(ring, np.array([22.0, 150.0])), (1.0, 0.0)), 5.0)


class Radial(unittest.TestCase):
    """A pier's tip or a corner of the wall (the outline turns convex by over 30 degrees within 40 m and goes on turning over 80 m): a gate
    there parks radially, its axis from the gate to the centre of the circle fitted to the outline, like a round satellite's."""
    PIER = [(-300, -40), (300, -40), (300, 0), (30, 0), (30, 150), (-30, 150), (-30, 0), (-300, 0)]     # a 60 m wide pier, 150 m out

    @staticmethod
    def round_tip(r=30.0, length=150.0):
        """A pier of half width r with a half-circle tip (chords of 10 degrees)."""
        a = np.radians(np.arange(0.0, 180.0 + 1e-9, 10.0))
        tip = [(r * math.cos(t), length + r * math.sin(t)) for t in a]
        return [(-300, -40), (300, -40), (300, 0), (r, 0)] + tip + [(-r, 0), (-300, 0)]

    def test_the_centre_of_a_round_tip_and_a_corner_none_on_a_straight_or_stepped_or_concave_wall(self):
        ring = np.array(self.round_tip(), np.float64)
        c = pa.radial_centre(ring, np.array([0.0, 180.0]))                        # (the tip's apex)
        self.assertIsNotNone(c)
        self.assertLess(math.hypot(c[0], c[1] - 150.0), 6.0)                       # (the arc's centre)
        corner = np.array(self.PIER, np.float64)
        c = pa.radial_centre(corner, np.array([30.0, 150.0]))                      # (a square tip's corner)
        self.assertIsNotNone(c)
        self.assertTrue(bool(pa._pip(corner, c[None])[0]))
        self.assertIsNone(pa.radial_centre(corner, np.array([30.0, 60.0])))        # (the pier's straight side)
        self.assertIsNone(pa.radial_centre(corner, np.array([100.0, 0.0])))        # (the terminal's wall, with the pier's root 70 m off)
        self.assertIsNone(pa.radial_centre(corner, np.array([30.0, 0.0])))         # (a concave corner: the root of the pier)
        steps = [(0.0, 0.0)]                                                       # (a wall with notches every 30 m: out and back)
        for k in range(10):
            steps += [(30.0 * k + 10.0, 0.0), (30.0 * k + 10.0, 6.0), (30.0 * k + 20.0, 6.0), (30.0 * k + 20.0, 0.0)]
        steps += [(300.0, 0.0), (300.0, -40.0), (0.0, -40.0)]
        notched = np.array(steps, np.float64)
        for x in (70.0, 80.0, 115.0, 175.0, 200.0):                                # (away from the building's own corners)
            self.assertIsNone(pa.radial_centre(notched, np.array([x, 0.0])), x)

    def test_a_gate_at_a_tips_corner_parks_toward_the_arcs_centre_not_across_the_wall(self):
        ring = np.array(self.PIER, np.float64)
        f = feats(aerodrome("regional_airport"), (point(33.0, 140.0), "airport_gate", {"aeroway": "gate"}), occupied=True)
        got = [c for c in pa.stands(f, buildings(self.PIER)) if c.mode == "gate"]
        self.assertEqual(len(got), 1)
        c = got[0]
        half = pa.SPECS[c.kind][0] / 2.0
        x, y = en(c)
        nose = np.array([x + c.ne * half, y + c.nn * half])
        centre = pa.radial_centre(ring, pa._nearest_on(ring, nose)[1], nose)
        self.assertIsNotNone(centre)
        want = (centre - nose) / np.hypot(*(centre - nose))
        self.assertGreater(c.ne * want[0] + c.nn * want[1], math.cos(math.radians(5.0)))    # (the axis through the centre)
        self.assertLess(abs(c.ne), math.cos(math.radians(8.0)))                             # (not across the east face (-x): rotated toward the tip)

    def test_the_gates_of_a_square_tip_fan_out_and_none_overlap(self):
        gates = [(34.0, 100.0), (33.0, 140.0), (15.0, 154.0), (-15.0, 154.0), (-33.0, 140.0), (-34.0, 100.0)]
        f = feats(aerodrome("regional_airport"), *[(point(x, y), "airport_gate", {"aeroway": "gate"}) for x, y in gates], occupied=True)
        got = [c for c in pa.stands(f, buildings(self.PIER)) if c.mode == "gate"]
        self.assertGreaterEqual(len(got), 3)
        placed = [(c.kind,) + en(c) + (c.ne, c.nn) for c in got]
        for i, (k, x, y, nx, ny) in enumerate(placed):
            for k2, x2, y2, nx2, ny2 in placed[i + 1:]:
                self.assertFalse(pa._in_rects(k, x, y, nx, ny, pa._world(pa._local_points(k2), x2, y2, nx2, ny2), 0.0).any())
        angles = sorted(math.degrees(math.atan2(c.nn, c.ne)) for c in got)
        self.assertGreater(angles[-1] - angles[0], 40.0)                                   # (a fan, not all one way)


class Occupancy(unittest.TestCase):
    def test_most_of_a_hubs_stands_are_full(self):
        share = sum(1 for i in range(2000) if pa._crc(f"gate{i}") % 100 < pa.OCCUPIED_PCT) / 2000.0
        self.assertGreaterEqual(pa.OCCUPIED_PCT, 80)
        self.assertAlmostEqual(share, pa.OCCUPIED_PCT / 100.0, delta=0.04)


class Rows(unittest.TestCase):
    def rows(self, f, bld, box=BOX, lights=False):
        """The aircraft's rows of the tile (lights: the navigation lights' instead)."""
        got = pa.tile_rows(FRAME, box, lambda k: f, lambda k: bld, ground, keys=[0])
        return [r for r in got if (r[0] in pa.GROUPS.values()) != lights]

    def test_a_gate_aircraft_has_three_navigation_lights(self):
        f = feats(aerodrome(), gate(0.0), occupied=True)
        bld = buildings(TERMINAL)
        craft = pa.stands(f, bld)
        self.assertEqual(len(craft), 1)
        rows, lights = self.rows(f, bld), self.rows(f, bld, lights=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(sorted(r[0] for r in lights), ["afl_green", "afl_red", "afl_white"])
        kind, (x, y) = craft[0].kind, en(craft[0])
        span, length = pa.SPECS[kind][1], pa.SPECS[kind][0]
        by = {r[0]: r for r in lights}
        # (the nose toward the terminal, -y: its left is +x: red there at the tip, green at the other, white at the tail)
        self.assertAlmostEqual(by["afl_red"][1] - rows[0][1], span / 2.0, delta=0.3)
        self.assertAlmostEqual(by["afl_green"][1] - rows[0][1], -span / 2.0, delta=0.3)
        self.assertAlmostEqual(by["afl_white"][2] - rows[0][2], length / 2.0 - pa.NAV_TAIL_IN_M, delta=0.3)
        self.assertAlmostEqual(by["afl_red"][3], 5.0 + pa.NAV[kind][1], 1)         # (the tip's height over the ground)
        for r in lights:
            self.assertEqual((r[4], r[5], r[6]), (0.0, 1.0, 1.0))
        self.assertEqual(self.rows(f, bld, (-400.0, 200.0, 400.0, 300.0), lights=True), [])      # (owned with its aircraft)

    def test_no_navigation_lights_at_remote_or_ga_stands(self):
        ga = feats(aerodrome("regional_airport"), (poly([(-150, 100), (150, 100), (150, 200), (-150, 200)]), "apron", {}))
        got = pa.tile_rows(FRAME, BOX, lambda k: ga, lambda k: None, ground, keys=[0])
        self.assertTrue(got and all(r[0] == "ac_light" for r in got))

    def test_none_without_airport_features(self):
        other = mapgeom.build("infrastructure", np.array(["a", "b"]), [point(0, 0), line((0, 0), (10, 0))])
        other.attrs = {"class": mapgeom.text_column(["bench", "fence"]),
                       "subtype": mapgeom.text_column(["pedestrian", "barrier"])}
        self.assertEqual(self.rows(other, buildings(TERMINAL)), [])
        self.assertEqual(self.rows(None, None), [])
        self.assertFalse(pa.has_stands(other))

    def test_row_format_yaw_and_ground(self):
        f, _ = gates_row(5)
        rows = self.rows(f, buildings(TERMINAL))
        self.assertGreater(len(rows), 0)
        for r in rows:
            self.assertIn(r[0], pa.GROUPS.values())
            kind = {v: k for k, v in pa.GROUPS.items()}[r[0]]
            self.assertEqual((r[5], r[6]), (1.0, pa.SPECS[kind][2]))
            self.assertIn(r[7], [pa.rank_for(i) for i in range(pa.LIVERIES)])       # (the rank picks a livery)
            a = math.radians(r[4] + 90.0)                               # the shape's +Y, pointing at the terminal (-y)
            self.assertAlmostEqual(math.cos(a), 0.0, 1)
            self.assertAlmostEqual(math.sin(a), -1.0, 1)
            self.assertAlmostEqual(r[3], 5.0, 1)

    def test_each_aircraft_is_in_one_tile(self):
        f, _ = gates_row(6, 70.0, occupied=True)
        bld = buildings(TERMINAL)
        whole = self.rows(f, bld)
        self.assertEqual(len(whole), 6)
        cut = sorted(r[1] for r in whole)[3] - 0.3
        west = self.rows(f, bld, (BOX[0], BOX[1], cut, BOX[3]))
        east = self.rows(f, bld, (cut, BOX[1], BOX[2], BOX[3]))
        self.assertEqual(sorted(map(tuple, west + east)), sorted(map(tuple, whole)))
        self.assertTrue(west and east)


class Liveries(unittest.TestCase):
    """Each stand wears one of its airport's two or three liveries, by a hash: neighbours vary, an airport is not a rainbow."""

    def pick(self, rank):
        """trees.lua's pick: the model number floor(rank * 7919) % LIVERIES."""
        return int(rank * 7919) % pa.LIVERIES

    def test_rank_for_picks_the_livery(self):
        for i in range(pa.LIVERIES):
            self.assertEqual(self.pick(pa.rank_for(i)), i)
            self.assertLess(pa.rank_for(i), 0.05)                                # (under every ring's threshold)

    def test_fleet_two_or_three_distinct_deterministic(self):
        sizes = set()
        for seed in range(300):
            fl = pa._fleet(seed)
            sizes.add(len(fl))
            self.assertEqual(len(set(fl)), len(fl))
            self.assertTrue(all(0 <= i < pa.LIVERIES for i in fl))
            self.assertEqual(fl, pa._fleet(seed))
        self.assertEqual(sizes, {pa.AIRLINES_MIN, pa.AIRLINES_MAX})
        self.assertEqual(len(pa._fleet(5, pa.GA_FLEET)), pa.GA_FLEET)

    def test_an_airports_stands_use_few_liveries_and_vary(self):
        f, _ = gates_row(12, 40.0, occupied=True)
        bld = buildings([(-200, -40), (500, -40), (500, 0), (-200, 0)])
        got = pa.stands(f, bld)
        self.assertGreaterEqual(len(got), 8)
        used = {c.livery for c in got}
        self.assertLessEqual(len(used), pa.AIRLINES_MAX)
        self.assertGreaterEqual(len(used), 2)
        self.assertLessEqual(used, set(range(pa.LIVERIES)))
        rows = [r for r in pa.tile_rows(FRAME, (-400.0, -300.0, 700.0, 300.0), lambda k: f, lambda k: bld, ground, keys=[0])
                if r[0] in pa.GROUPS.values()]
        self.assertEqual(sorted({self.pick(r[7]) for r in rows}), sorted(used))   # (the rows carry the stands' liveries)

    def test_deterministic_and_the_same_per_stand_whoever_asks(self):
        f, _ = gates_row(8, 70.0, occupied=True)
        bld = buildings(TERMINAL)
        a = [(c.lat, c.lon, c.livery) for c in pa.stands(f, bld)]
        f.__dict__.pop(pa._KEY)                                                 # (placed afresh)
        self.assertEqual(a, [(c.lat, c.lon, c.livery) for c in pa.stands(f, bld)])
        for c in pa.stands(f, bld):
            self.assertEqual(c.livery, pa.livery_for(pa._airport_seed(*self._scene(f, c)), c.lat, c.lon, c.mode))

    @staticmethod
    def _scene(f, c):
        sc = f.__dict__[pa._KEY + "_scene"]
        return sc, *sc.xy(np.array([[c.lon, c.lat]]))[0]

    def test_other_airports_have_other_fleets(self):
        fleets = {tuple(sorted(pa._fleet(seed))) for seed in range(200)}
        self.assertGreater(len(fleets), 20)

    def test_ga_aircraft_wear_the_wider_mix(self):
        seed = 12345
        got = {pa.livery_for(seed, -27.0 + i * 1e-4, 153.0, "ga") for i in range(200)}
        self.assertEqual(len(got), pa.GA_FLEET)
        self.assertEqual(got, set(pa._fleet(seed, pa.GA_FLEET)))


if __name__ == "__main__":
    unittest.main()
