"""airfieldlights: a lit, open runway's white edge lights both sides along its length (the end zones yellow), green
threshold and red end bars (a displaced threshold moves the green); none for an unlit, closed or water runway. Blue
taxiway edge lights both sides of a taxiway at an airport whose nearest runway is lit, none on the runway, in an apron,
for an unlit airport, without airport Features or on an unpaved way; the tile's forest rows split between the tiles.
Approach lights (a 900 m row of white 5-light barrettes every 30 m before each end with crossbars at 150 m and 300 m, red
side rows over the last 270 m and touchdown zone barrettes on the CAT runways) on a runway over 1800 m or at a
large airport, green wing bars at every threshold, PAPIs (two red, two white, 9 m apart, 15 m outside the edge on the landing aircraft's left, 300 m past the
threshold) on every lit paved runway; the ILS aerials (a localizer past each far end, a glide-slope mast at each
threshold) on a paved open runway over 1800 m, as forest rows facing the approach. The same lights in the far and skyline
tiles (far_points: the tile's lamps' rows with a glow group number, no ILS aerials; blockworker's far geotiles carry them
over a lit runway, their revisit key too; the mod's roads.lua draws them as far as the street lamps)."""
import json
import os
import re
import sys
import types
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import aeroways  # noqa: E402
import airfieldlights as al  # noqa: E402
import airportfixtures as af  # noqa: E402
import airports  # noqa: E402
import geodesy  # noqa: E402
import geotiles  # noqa: E402
import mapgeom  # noqa: E402

FRAME = geodesy.EnuFrame(40.0, -76.0)       # the runway's low end is its origin, the runway runs east
LENGTH, WIDTH = 1500.0, 45.72       # (under APPROACH_MIN_RUNWAY_M: no approach lights nor ILS aerials)
LONG = 3000.0
BOX = (-200.0, -300.0, 2100.0, 300.0)


def runway(length=LENGTH, type="medium_airport", **kw):
    """A runway from (40, -76) due east, `length` m long (airports.Runway)."""
    ap = af.airport(1, "KTST", 40.0, -76.0, 100, type=type)
    row = af.runway(1, ap, 40.0, -76.0, 90.0, length, width_m=WIDTH, **kw)
    return airports.AirportDB.from_csv(af.to_csv([ap], af.AIRPORT_COLS), af.to_csv([row], af.RUNWAY_COLS)).runways[0]


def en(lights, kind=None, colour=None):
    """The lights' (east, north) in FRAME, optionally of a kind / colour."""
    e, n, _ = FRAME.geodetic_to_enu(lights.lat, lights.lon, 0.0)
    keep = np.ones(len(lights), bool)
    if kind is not None:
        keep &= lights.kind == al.KINDS.index(kind)
    if colour is not None:
        keep &= lights.colour == al.COLOURS.index(colour)
    return np.asarray(e)[keep], np.asarray(n)[keep]


def lonlat(pts):
    la, lo, _ = FRAME.enu_to_geodetic(np.array([p[0] for p in pts], np.float64),
                                      np.array([p[1] for p in pts], np.float64), 0.0)
    return np.stack([lo, la], 1)


def feats(*rows):
    """Infrastructure Features of (kind, points in FRAME, class, subtype, tags dict, surface) rows."""
    ids = np.array([f"f{i:03d}" for i in range(len(rows))])
    geoms = []
    for kind, pts, *_ in rows:
        pts = list(pts) + ([pts[0]] if kind == mapgeom.POLYGON else [])
        geoms.append((kind, [[lonlat(pts)]]))
    f = mapgeom.build("infrastructure", ids, geoms)
    f.attrs = {"class": mapgeom.text_column([r[2] for r in rows]), "subtype": mapgeom.text_column([r[3] for r in rows]),
               "tags": mapgeom.text_column([json.dumps(r[4]) for r in rows]),
               "surface": mapgeom.text_column([r[5] for r in rows])}
    return f


def way(pts, cls="taxiway", sub="airport", tags=None, surface=None):
    return (mapgeom.LINE, pts, cls, sub, tags or {}, surface)


def area(pts, cls="apron"):
    return (mapgeom.POLYGON, pts, cls, "airport", {}, None)


def half(intl):
    return (aeroways.TAXIWAY_INTL_M if intl else aeroways.TAXIWAY_M) / 2.0


PARALLEL = [(200.0, 150.0), (1600.0, 150.0)]
CROSSING = [(900.0, -120.0), (900.0, 120.0)]


class RunwayLights(unittest.TestCase):
    def setUp(self):
        self.lights = al.runway_lights(runway())

    def test_edge_lights_both_sides_the_runway_long(self):
        e, n = en(self.lights, al.EDGE)
        out = WIDTH / 2 + al.EDGE_OUT_M
        for side in (out, -out):
            east = np.sort(e[np.abs(n - side) < 0.5])
            self.assertAlmostEqual(east[0], 0.0, delta=1.0)
            self.assertAlmostEqual(east[-1], LENGTH, delta=1.0)
            self.assertLessEqual(float(np.diff(east).max()), al.EDGE_SPACING_M + 1.0)
        self.assertAlmostEqual(len(e), 2 * (LENGTH / al.EDGE_SPACING_M + 1), delta=2)      # (the length to a metre)

    def test_end_zones_yellow_the_rest_white(self):
        zone = min(al.CAUTION_M, LENGTH / 3.0)
        yellow, _ = en(self.lights, al.EDGE, al.YELLOW)
        white, _ = en(self.lights, al.EDGE, al.WHITE)
        self.assertTrue(len(yellow) and len(white))
        self.assertTrue(((yellow < zone + 1) | (yellow > LENGTH - zone - 1)).all())
        self.assertTrue(((white > zone - 1) & (white < LENGTH - zone + 1)).all())

    def test_green_threshold_and_red_end_bars_across_the_runway(self):
        for colour, kind, at in ((al.GREEN, al.THRESHOLD, (0.0, LENGTH)),
                                 (al.RED, al.END, (-al.BAR_OUT_M, LENGTH + al.BAR_OUT_M))):
            e, n = en(self.lights, kind, colour)
            for x in at:
                bar = n[np.abs(e - x) < 1.0]
                self.assertGreaterEqual(len(bar), int(WIDTH / al.BAR_SPACING_M))
                self.assertAlmostEqual(float(bar.min()), -WIDTH / 2, delta=1.0)
                self.assertAlmostEqual(float(bar.max()), WIDTH / 2, delta=1.0)
            self.assertEqual(len(e), 2 * len(bar))
        # each colour on its own kind only
        self.assertEqual(set(zip(self.lights.colour.tolist(), self.lights.kind.tolist())),
                         {(al.COLOURS.index(c), al.KINDS.index(k)) for c, k in
                          ((al.WHITE, al.EDGE), (al.YELLOW, al.EDGE), (al.GREEN, al.THRESHOLD), (al.RED, al.END),
                           (al.RED, al.PAPI), (al.WHITE, al.PAPI), (al.GREEN, al.WING))})

    def test_displaced_threshold_moves_the_green_bar(self):
        e, _ = en(al.runway_lights(runway(le_disp_ft=600)), al.THRESHOLD, al.GREEN)
        self.assertAlmostEqual(float(e.min()), 600 * airports.FT, delta=1.0)
        self.assertAlmostEqual(float(e.max()), LENGTH, delta=1.0)

    def test_unlit_closed_and_water_runways_have_none(self):
        self.assertEqual(len(al.runway_lights(runway(lighted=0))), 0)
        self.assertEqual(len(al.runway_lights(runway(closed=1))), 0)
        self.assertEqual(len(al.runway_lights(runway(surface="WATER"))), 0)

    def test_memoised(self):
        self.assertIs(al.runway_lights(runway()), al.runway_lights(runway()))
        self.assertIsNot(al.runway_lights(runway()), al.runway_lights(runway(lighted=0)))


class ApproachAndPapi(unittest.TestCase):
    def test_approach_row_900_m_of_barrettes_before_each_end_with_two_crossbars(self):
        lights = al.runway_lights(runway(LONG))
        e, n = en(lights, al.APPROACH, al.WHITE)
        centre = np.sort(e[np.abs(n) < 0.5])
        for row, edge in ((centre[centre < 0], 0.0), (centre[centre > LONG], LONG)):
            self.assertEqual(len(row), int(al.APPROACH_LEN_M / al.APPROACH_STEP_M))
            self.assertAlmostEqual(float(np.abs(row - edge).min()), al.APPROACH_STEP_M, delta=0.5)
            self.assertAlmostEqual(float(np.abs(row - edge).max()), al.APPROACH_LEN_M, delta=0.5)
            self.assertTrue(np.allclose(np.diff(row), al.APPROACH_STEP_M, atol=0.05))
        # a barrette: five lights a metre apart across the centreline at every row (the l1 / l2 photographs)
        for edge, sign in ((0.0, -1.0), (LONG, 1.0)):
            for k in range(1, 31):
                at = edge + sign * k * al.APPROACH_STEP_M
                across = np.sort(n[(np.abs(e - at) < 0.5) & (np.abs(n) < 2.5)])
                self.assertEqual(len(across), al.APPROACH_BARRETTE_N, k)
                self.assertTrue(np.allclose(across, np.arange(-2.0, 2.1, 1.0), atol=0.05), (k, across))
        # crossbars at 150 m and 300 m, 3 m apart out to 15 m each side of the barrette
        for edge, sign in ((0.0, -1.0), (LONG, 1.0)):
            for at in (al.APPROACH_BAR2_AT_M, al.APPROACH_BAR_AT_M):
                bar = np.sort(n[np.abs(e - (edge + sign * at)) < 1.0])
                self.assertAlmostEqual(float(bar.min()), -al.APPROACH_BAR_W_M / 2, delta=0.3)
                self.assertAlmostEqual(float(bar.max()), al.APPROACH_BAR_W_M / 2, delta=0.3)
                self.assertEqual(len(bar), 5 + 10)                       # (the barrette's five and five a side out to 15 m)
            other = np.sort(n[np.abs(e - (edge + sign * 120.0)) < 1.0])
            self.assertEqual(len(other), al.APPROACH_BARRETTE_N)           # (no crossbar between)
        self.assertEqual(len(e), 2 * (30 * 5 + 2 * 10))
        self.assertTrue(((e < 0) | (e > LONG)).all())                       # (none on the runway)

    def test_red_side_rows_over_the_last_270_m_on_a_cat_runway_only(self):
        lights = al.runway_lights(runway(LONG))
        e, n = en(lights, al.APPROACH, al.RED)
        for edge, sign in ((0.0, -1.0), (LONG, 1.0)):
            near = np.abs(e - edge) < al.SIDE_ROW_LEN_M + 1.0
            self.assertTrue(((np.abs(e - edge) <= al.SIDE_ROW_LEN_M + 0.5) | (np.sign(e - edge) != sign)).all())
            rows = sorted({round(abs(float(x - edge)) / al.APPROACH_STEP_M) for x in e[near & (np.sign(e - edge) == sign)]})
            self.assertEqual(rows, [1, 2, 3, 4, 6, 7, 8, 9], "every 30 m to 270 m but at the 150 m crossbar")
            at = edge + sign * 3 * al.APPROACH_STEP_M
            across = np.sort(n[np.abs(e - at) < 0.5])
            self.assertTrue(np.allclose(across, [-10, -9, -8, 8, 9, 10], atol=0.05), across)      # (two barrettes of three)
        self.assertEqual(len(e), 2 * 8 * 6)

        def red(rw):
            return len(en(al.runway_lights(rw), al.APPROACH, al.RED)[0])
        self.assertGreater(red(runway(1900.0, type="large_airport")), 0)
        self.assertEqual(red(runway(2000.0)), 0, "a 2 km runway at a medium airport: CAT I")
        self.assertEqual(red(runway(2300.0)), 0)
        self.assertGreater(red(runway(2500.0)), 0)
        self.assertEqual(red(runway(LONG, surface="GRS")), 0)
        self.assertEqual(red(runway(LONG, lighted=0)), 0)

    def test_approach_lights_by_length_or_large_airport_and_paving(self):
        def has(rw):
            return len(en(al.runway_lights(rw), al.APPROACH)[0]) > 0
        self.assertTrue(has(runway(1900.0)))
        self.assertEqual(len(en(al.runway_lights(runway(1900.0)), al.APPROACH)[0]), 2 * (30 * 5 + 2 * 10),
                         "(barrettes and crossbars: no side rows under 2400 m)")
        self.assertFalse(has(runway(1700.0)))
        self.assertFalse(has(runway(1700.0, surface="GRS")))
        self.assertTrue(has(runway(1500.0, type="large_airport")))
        self.assertFalse(has(runway(LONG, surface="GRS")))
        self.assertFalse(has(runway(LONG, lighted=0)))
        self.assertFalse(has(runway(LONG, closed=1)))

    def test_papi_four_units_left_of_the_landing_aircraft_two_red_two_white(self):
        lights = al.runway_lights(runway(LONG))
        out = WIDTH / 2 + al.PAPI_OFF_M
        for colour, want in ((al.RED, (0, 1)), (al.WHITE, (2, 3))):
            e, n = en(lights, al.PAPI, colour)
            self.assertEqual(len(e), 4)                                   # (two a landing end)
            for i in want:
                # landing toward the high end the left is north (+t) 300 m in; back the other way it is south
                for x, side in ((al.PAPI_AT_M, 1.0), (LONG - al.PAPI_AT_M, -1.0)):
                    hit = (np.abs(e - x) < 1.0) & (np.abs(n - side * (out + i * al.PAPI_STEP_M)) < 0.5)
                    self.assertEqual(int(hit.sum()), 1, (colour, i, x))
        self.assertEqual(len(en(lights, al.PAPI)[0]), 8)

    def test_papi_follows_the_displaced_threshold_and_needs_a_lit_paved_long_runway(self):
        e, _ = en(al.runway_lights(runway(LONG, le_disp_ft=600)), al.PAPI)
        self.assertAlmostEqual(float(e.min()), 600 * airports.FT + al.PAPI_AT_M, delta=1.0)

        def has(rw):
            return len(en(al.runway_lights(rw), al.PAPI)[0]) > 0
        self.assertTrue(has(runway(1000.0)))
        self.assertFalse(has(runway(800.0)))
        self.assertFalse(has(runway(1000.0, surface="GRS")))
        self.assertFalse(has(runway(1000.0, lighted=0)))


class IlsMasts(unittest.TestCase):
    def setUp(self):
        self.masts = al.ils_masts(runway(LONG))

    def at(self, group):
        out = []
        for m in self.masts:
            if m.group == group:
                e, n, _ = FRAME.geodetic_to_enu(np.array([m.lat]), np.array([m.lon]), 0.0)
                e2, n2, _ = FRAME.geodetic_to_enu(np.array([m.lat_to]), np.array([m.lon_to]), 0.0)
                out.append((float(e[0]), float(n[0]), float(e2[0] - e[0]), float(n2[0] - n[0])))
        return sorted(out)

    def test_a_localizer_past_each_far_end_facing_down_the_runway(self):
        (xa, ya, dxa, _), (xb, yb, dxb, _) = self.at("ils_loc")
        self.assertAlmostEqual(xa, -al.LOC_AT_M, delta=0.5)
        self.assertAlmostEqual(xb, LONG + al.LOC_AT_M, delta=0.5)
        self.assertAlmostEqual(ya, 0.0, delta=0.5)
        self.assertAlmostEqual(yb, 0.0, delta=0.5)
        self.assertGreater(dxa, 9.0)                    # (its front toward the runway)
        self.assertLess(dxb, -9.0)
        # it stands between the approach lights' rows of the end it is past (clear of the crossbar's)
        rows = np.arange(al.APPROACH_STEP_M, al.APPROACH_LEN_M + 1, al.APPROACH_STEP_M)
        self.assertGreater(float(np.abs(rows - al.LOC_AT_M).min()), 10.0)

    def test_a_glide_slope_mast_at_each_threshold_off_the_side_opposite_the_papi(self):
        (xa, ya, dxa, _), (xb, yb, dxb, _) = self.at("ils_gs")
        self.assertAlmostEqual(xa, al.GS_AT_M, delta=0.5)
        self.assertAlmostEqual(ya, -al.GS_OFF_M, delta=0.5)       # (landing toward the high end the PAPI is at +t)
        self.assertAlmostEqual(xb, LONG - al.GS_AT_M, delta=0.5)
        self.assertAlmostEqual(yb, al.GS_OFF_M, delta=0.5)
        self.assertLess(dxa, -9.0)                      # (each faces the approach it serves)
        self.assertGreater(dxb, 9.0)

    def test_only_over_1800_m_paved_and_open(self):
        for rw in (runway(1700.0), runway(LONG, surface="GRS"), runway(LONG, closed=1), runway(LONG, surface="WATER")):
            self.assertEqual(al.ils_masts(rw), [])
        self.assertEqual(len(al.ils_masts(runway(LONG, lighted=0))), 4)          # (an ILS runway need not be lit)
        self.assertEqual(len(self.masts), 4)

    def test_memoised(self):
        self.assertIs(al.ils_masts(runway(LONG)), al.ils_masts(runway(LONG)))

    def test_rows_in_their_groups_on_the_ground_facing_the_approach(self):
        ground = lambda lat, lon: np.full(np.shape(lat), 12.0)

        def rows(box):
            return al.tile_rows(FRAME, box, lambda k: None, lambda k: [runway(LONG)], ground, keys=[0])
        whole = rows((-1500.0, -400.0, 4500.0, 400.0))
        masts = [r for r in whole if r[0] in al.ILS_H]
        self.assertEqual(sorted(r[0] for r in masts), ["ils_gs", "ils_gs", "ils_loc", "ils_loc"])
        for r in masts:
            self.assertEqual((len(r), r[5], r[6], r[7]), (8, 1.0, al.ILS_H[r[0]], 0.0))
            self.assertAlmostEqual(r[3], 12.0, delta=1.5)                     # (no lift; the frame's up falls with the curvature)
        yaw = {(r[0], r[1] < LONG / 2): r[4] for r in masts}
        # (the shape's +Y at yaw 0 is north, the yaw counter-clockwise: toward +x is 270, toward -x is 90)
        self.assertAlmostEqual(yaw[("ils_loc", True)], 270.0, delta=0.5)
        self.assertAlmostEqual(yaw[("ils_loc", False)], 90.0, delta=0.5)
        self.assertAlmostEqual(yaw[("ils_gs", True)], 90.0, delta=0.5)
        self.assertAlmostEqual(yaw[("ils_gs", False)], 270.0, delta=0.5)
        a, b = rows((-1500.0, -400.0, 1500.0, 400.0)), rows((1500.0, -400.0, 4500.0, 400.0))
        self.assertEqual(len(a) + len(b), len(whole))                         # (each in one tile)
        self.assertEqual(sum(r[0] in al.ILS_H for r in a + b), 4)
        self.assertTrue({r[0] for r in whole} - set(al.ILS_H) <= set(al.GROUPS.values()))       # (the rest glow rows)
        self.assertEqual(len(whole) - 4, len(al.runway_lights(runway(LONG))))


def layer(name, kind, rows, cls, sub):
    """Features of (east, north) lines (kind LINE) or rings (POLYGON) in FRAME, one class and subtype."""
    ids = np.array([f"c{i:03d}" for i in range(len(rows))])
    geoms = [(kind, [[lonlat(list(r) + ([r[0]] if kind == mapgeom.POLYGON else []))]]) for r in rows]
    f = mapgeom.build(name, ids, geoms)
    f.attrs = {"class": mapgeom.text_column([cls] * len(rows)), "subtype": mapgeom.text_column([sub] * len(rows)),
               "tags": mapgeom.text_column(["{}"] * len(rows)), "surface": mapgeom.text_column([None] * len(rows))}
    return f


def box_at(x, y, w=20.0, d=20.0):
    return [(x - w / 2, y - d / 2), (x + w / 2, y - d / 2), (x + w / 2, y + d / 2), (x - w / 2, y + d / 2)]


class Clearance(unittest.TestCase):
    """Approach lights, PAPIs and ILS aerials are not put on the pavement, inside buildings or on roads."""
    BIG = (-1500.0, -400.0, 4500.0, 400.0)

    def rows(self, rw=None, box=BIG, apron=(), buildings=(), roads=(), runway_line=False):
        infra = [area(r) for r in apron]
        if runway_line:
            infra.append(way([(-500.0, 0.0), (LONG + 500.0, 0.0)], cls="runway"))
        f = feats(*infra) if infra else None
        b = layer("buildings", mapgeom.POLYGON, list(buildings), "", "") if buildings else None
        r = layer("roads", mapgeom.LINE, list(roads), "service", "road") if roads else None
        rw = rw or runway(LONG)
        return al.tile_rows(FRAME, box, lambda k: f, lambda k: [rw], lambda la, lo: np.full(np.shape(la), 12.0), keys=[0],
                            roads_fn=(lambda k: r) if r is not None else None,
                            buildings_fn=(lambda k: b) if b is not None else None)

    @staticmethod
    def of(rows, group):
        return sorted((r[1], r[2]) for r in rows if r[0] == group)

    def approach(self, rows):
        """The approach lights' (east, north): the white glow rows outside the runway between its edge lights' rows ..."""
        return [r for r in rows if r[0] == al.GROUPS[al.WHITE] and (r[1] < -10.0 or r[1] > LONG + 10.0)
                and abs(r[2]) < 16.0]

    def test_nothing_in_the_way_changes_nothing(self):
        base = self.rows()
        self.assertEqual(len(self.approach(base)), 2 * 170)
        self.assertEqual(len([r for r in base if r[0] in al.ILS_H]), 4)
        far = self.rows(apron=[box_at(1500.0, 300.0, 100.0, 60.0)], buildings=[box_at(1500.0, -300.0)],
                        roads=[[(1500.0, -400.0), (1500.0, 400.0)]])
        self.assertEqual(far, base)

    def test_a_localizer_on_an_apron_is_left_out_and_the_approach_row_stops_before_it(self):
        pad = box_at(LONG + 315.0, 0.0, 130.0, 80.0)            # (Brisbane's: the domestic apron past the end)
        got = self.rows(apron=[pad])
        locs = self.of(got, "ils_loc")
        self.assertEqual(len(locs), 1)
        self.assertLess(locs[0][0], 0.0)                         # (the west one stays)
        self.assertEqual(len(self.of(got, "ils_gs")), 2)
        east = [r for r in self.approach(got) if r[1] > LONG]
        self.assertEqual(len(east), 8 * 5 + 10, "the barrettes from 30 m to 240 m and the 150 m crossbar: it ends before the apron's edge (250 m)")
        self.assertLess(max(r[1] for r in east), LONG + 250.0)
        self.assertEqual(len([r for r in self.approach(got) if r[1] < 0.0]), 170, "the other end whole")

    def test_the_apron_alone_is_what_counts_not_the_runways_own_mapped_paving(self):
        self.assertEqual(self.rows(runway_line=True), self.rows())

    def test_a_papi_unit_in_a_building_is_left_out(self):
        out = WIDTH / 2 + al.PAPI_OFF_M
        base = self.rows()
        got = self.rows(buildings=[box_at(al.PAPI_AT_M, out + al.PAPI_STEP_M, 6.0, 6.0)])
        self.assertEqual(len(base) - len(got), 1)
        red = al.GROUPS[al.RED]
        self.assertEqual(len([r for r in base if r[0] == red]) - len([r for r in got if r[0] == red]), 1, "(the second unit)")

    def test_a_road_within_5_m_drops_only_the_light_on_it(self):
        base = self.rows()
        got = self.rows(roads=[[(-92.0, -200.0), (-92.0, 200.0)]])           # 2 m from the light 90 m out
        west = sorted(r[1] for r in self.approach(got) if r[1] < 0.0)
        self.assertEqual(len(west), len([r for r in self.approach(base) if r[1] < 0.0]) - 5, "the barrette's five: the row goes on over it")
        self.assertFalse(any(abs(x + 90.0) < 0.5 for x in west))
        self.assertEqual(len(self.approach(got)), len(self.approach(base)) - 5)
        near = self.rows(roads=[[(-96.5, -200.0), (-96.5, 200.0)]])          # 6.5 m off the light: clear
        self.assertEqual(len(self.approach(near)), len(self.approach(base)))
        foot = layer("roads", mapgeom.LINE, [[(-92.0, -200.0), (-92.0, 200.0)]], "footway", "road")
        rw = runway(LONG)
        got = al.tile_rows(FRAME, self.BIG, lambda k: None, lambda k: [rw], lambda la, lo: np.full(np.shape(la), 12.0),
                           keys=[0], roads_fn=lambda k: foot)
        self.assertEqual(len(self.approach(got)), 2 * 170, "a footpath is no road")

    def test_a_mast_with_its_half_width_clear_of_a_building(self):
        # (the localizer array is 30 m wide: a building 17-23 m off its centre clears it, one 11-17 m off is in its width)
        self.assertEqual(len(self.of(self.rows(buildings=[box_at(LONG + 315.0 + 20.0, 0.0, 6.0, 6.0)]), "ils_loc")), 2)
        self.assertEqual(len(self.of(self.rows(buildings=[box_at(LONG + 315.0 + 14.0, 0.0, 6.0, 6.0)]), "ils_loc")), 1)
        self.assertEqual(len(self.of(self.rows(buildings=[box_at(LONG + 315.0, 0.0, 6.0, 6.0)]), "ils_loc")), 1)

    def test_each_tile_decides_alike(self):
        pad = box_at(LONG + 315.0, 0.0, 130.0, 80.0)
        whole = self.rows(apron=[pad])
        a, b = self.rows(apron=[pad], box=(-1500.0, -400.0, 1500.0, 400.0)), \
            self.rows(apron=[pad], box=(1500.0, -400.0, 4500.0, 400.0))
        self.assertEqual(sorted(map(tuple, a + b)), sorted(map(tuple, whole)))

    def test_a_closed_runway_and_a_closed_airport_have_no_ils_or_approach_lights(self):
        for rw in (runway(LONG, closed=1), runway(LONG, type="closed")):
            self.assertEqual(al.ils_masts(rw), [])
            self.assertEqual(len([r for r in self.rows(rw=rw) if r[0] in al.ILS_H]), 0)
            self.assertEqual(len(self.approach(self.rows(rw=rw))), 0)


class Shapes(unittest.TestCase):
    """The generated shapes (tools/build_ils.py) are the ones the rows are made for."""
    SHAPES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "mod", "beamng_earth", "levels",
                          "beamng_earth", "art", "shapes", "beamngEarth")

    def read(self, kind):
        with open(os.path.join(self.SHAPES, f"bne_ils_{kind}.dae"), encoding="utf-8") as f:
            text = f.read()
        geoms = {m.group(1): np.array(m.group(2).split(), np.float64).reshape(-1, 3)
                 for m in re.finditer(r'<float_array id="([^"]*-p-a)"[^>]*>([^<]*)<', text)}
        return text, geoms

    def test_heights_widths_two_lods_the_front_toward_plus_y(self):
        for kind, group in (("loc", "ils_loc"), ("gs", "ils_gs")):
            text, geoms = self.read(kind)
            self.assertEqual(sorted(re.findall(rf'<node id="(bne_ils_{kind}\d+)"', text)),
                             sorted([f"bne_ils_{kind}96", f"bne_ils_{kind}12"]), "two levels of detail")
            self.assertNotIn("Colmesh", text)
            self.assertEqual(len(geoms), 2)
            for v in geoms.values():
                self.assertAlmostEqual(float(v[:, 2].max()), al.ILS_H[group], delta=0.01)       # (the catalogue height)
                self.assertGreaterEqual(float(v[:, 2].min()), -0.03)                             # (on the ground)
                self.assertGreater(float(v[:, 1].max()), float(-v[:, 1].min()))                  # (the front is +Y)
            tris = sum(int(c) for c in re.findall(r'<triangles count="(\d+)"', text))
            self.assertLess(tris, 1500)
        loc = max(self.read("loc")[1].values(), key=len)
        self.assertAlmostEqual(float(loc[:, 0].max() - loc[:, 0].min()), 30.0, delta=0.5)       # about 30 m wide, 3 m tall

    def test_the_materials_are_there(self):
        with open(os.path.join(self.SHAPES, "ils.materials.json"), encoding="utf-8") as f:
            mats = json.load(f)
        names = set()
        for kind in ("loc", "gs"):
            names |= set(re.findall(r'target="#([^"]+)-mat"', self.read(kind)[0]))
        self.assertTrue(names and names <= set(mats), names)


class TaxiwayLights(unittest.TestCase):
    def setUp(self):
        self.rw = runway()

    def lights(self, *rows, runways=None):
        return al.taxiway_lights(feats(*rows), [self.rw] if runways is None else runways)

    def test_blue_on_both_edges_of_a_taxiway(self):
        got = self.lights(way(PARALLEL))
        self.assertEqual(set(got.colour.tolist()), {al.COLOURS.index(al.BLUE)})
        e, n = en(got)
        out = half(False) + al.TAXI_OUT_M
        for side in (150.0 + out, 150.0 - out):
            east = np.sort(e[np.abs(n - side) < 0.5])
            self.assertAlmostEqual(east[0], 200.0, delta=1.0)
            self.assertAlmostEqual(east[-1], 1600.0, delta=1.0)
            self.assertLessEqual(float(np.diff(east).max()), al.TAXI_SPACING_M + 1.0)
        self.assertAlmostEqual(len(e), 2 * (1400.0 / al.TAXI_SPACING_M + 1), delta=2)

    def test_wider_at_an_international_airport_and_by_the_tag(self):
        intl = area([(-900, -900), (900, -900), (900, 900), (-900, 900)], "international_airport")
        _, n = en(self.lights(way(PARALLEL), intl))
        self.assertAlmostEqual(float(n.max()), 150.0 + half(True) + al.TAXI_OUT_M, delta=0.5)
        _, n = en(self.lights(way(PARALLEL, tags={"width": "30"})))
        self.assertAlmostEqual(float(n.max()), 150.0 + 15.0 + al.TAXI_OUT_M, delta=0.5)

    def test_none_on_the_runway(self):
        e, n = en(self.lights(way(CROSSING)))
        self.assertGreater(len(e), 0)
        self.assertFalse((np.abs(n) < WIDTH / 2 + al.RUNWAY_CLEAR_M - 0.01).any())

    def test_none_inside_an_apron_or_another_ways_pavement(self):
        ring = [(500.0, 100.0), (900.0, 100.0), (900.0, 200.0), (500.0, 200.0)]
        e, _ = en(self.lights(way(PARALLEL), area(ring)))
        self.assertFalse(((e > 500) & (e < 900)).any())
        self.assertTrue(((e < 500) | (e > 900)).any())
        # a spur ending on the parallel way: its lights inside that way's pavement are left out
        e, n = en(self.lights(way(PARALLEL), way([(900.0, 150.0), (900.0, 400.0)])))
        self.assertFalse(((np.abs(e - 900.0) < 9.0) & (np.abs(n - 150.0) < half(False) + al.PAVEMENT_CLEAR_M - 0.01)).any())
        self.assertTrue((n > 170.0).any())

    def test_only_where_the_nearest_runway_is_lit(self):
        self.assertEqual(len(self.lights(way(PARALLEL), runways=[runway(lighted=0)])), 0)
        self.assertEqual(len(self.lights(way(PARALLEL), runways=[runway(closed=1)])), 0)
        self.assertEqual(len(self.lights(way(PARALLEL), runways=[])), 0)
        self.assertEqual(len(self.lights(way([(20200.0, 150.0), (21600.0, 150.0)]))), 0)

    def test_not_unpaved_unlit_taxilane_or_other_features(self):
        self.assertEqual(len(self.lights(way(PARALLEL, surface="grass"))), 0)
        self.assertEqual(len(self.lights(way(PARALLEL, tags={"lit": "no"}))), 0)
        self.assertEqual(len(self.lights(way(PARALLEL, cls="taxilane"))), 0)
        self.assertEqual(len(self.lights(way(PARALLEL, sub="transit"))), 0)
        self.assertEqual(len(self.lights(way(PARALLEL, cls="road", sub="road"))), 0)
        self.assertEqual(len(al.taxiway_lights(None, [self.rw])), 0)

    def test_memoised_on_the_features(self):
        f = feats(way(PARALLEL))
        self.assertIs(al.taxiway_lights(f, [self.rw]), al.taxiway_lights(f, [self.rw]))
        self.assertEqual(len(al.taxiway_lights(f, [runway(lighted=0)])), 0)
        self.assertGreater(len(al.taxiway_lights(f, [self.rw])), 0)


class TileRows(unittest.TestCase):
    def setUp(self):
        self.rw = runway()
        self.f = feats(way(PARALLEL), way(CROSSING))
        self.ground = lambda lat, lon: np.full(np.shape(lat), 12.0)

    def rows(self, box):
        return al.tile_rows(FRAME, box, lambda k: self.f, lambda k: [self.rw], self.ground, keys=[0])

    def test_rows_in_the_colours_groups_on_the_ground(self):
        rows = self.rows(BOX)
        full = al.Lights.join([al.runway_lights(self.rw), al.taxiway_lights(self.f, [self.rw]),
                               al.taxiway_centre_lights([self.f], [self.rw])])
        self.assertEqual(len(rows), len(full))
        self.assertEqual({r[0] for r in rows}, set(al.GROUPS.values()))
        for r in rows:
            self.assertEqual(len(r), 8)
            self.assertAlmostEqual(r[3], 12.0 + al.LIFT_M, delta=0.6)      # (the frame's up falls with the curvature)
            self.assertEqual(r[6], al.GROUP_H)

    def test_each_light_in_one_tile(self):
        whole = self.rows(BOX)
        a, b = self.rows((-200.0, -300.0, 905.0, 300.0)), self.rows((905.0, -300.0, 2100.0, 300.0))
        self.assertEqual(len(a) + len(b), len(whole))
        self.assertTrue(all(r[1] < 905.0 for r in a) and all(r[1] >= 905.0 for r in b))
        self.assertEqual(self.rows((5000.0, 5000.0, 5500.0, 5500.0)), [])

    def test_the_centre_lights_read_the_cells_the_paint_reads(self):
        import taxiwaymarks
        self.assertEqual(al.CENTRE_REACH_M, taxiwaymarks.REACH_M, "the same list of Features: the paint's Scene is shared")
        got = al.tile_rows(FRAME, BOX, lambda k: self.f, lambda k: [self.rw], self.ground, reach_m=al.CENTRE_REACH_M)
        want = al.tile_rows(FRAME, BOX, lambda k: self.f, lambda k: [self.rw], self.ground)
        self.assertEqual(sorted(map(tuple, got)), sorted(map(tuple, want)), "one cell's Features, however many cells are read")
        cells = al._reach_keys(FRAME, BOX, al.CENTRE_REACH_M)
        import mapcells
        la, lo, _ = FRAME.enu_to_geodetic(np.array([BOX[0] - 280.0, BOX[2] + 280.0]), np.array([BOX[1] - 280.0, BOX[3] + 280.0]), 0.0)
        self.assertEqual(cells, mapcells.cells_for_bbox(float(min(la)), float(min(lo)), float(max(la)), float(max(lo))))

    def test_no_airport_no_rows(self):
        self.assertEqual(al.tile_rows(FRAME, BOX, lambda k: None, lambda k: [], self.ground, keys=[0]), [])
        plain = feats(way(PARALLEL, sub="transit"))
        self.assertEqual(al.tile_rows(FRAME, BOX, lambda k: plain, lambda k: [runway(lighted=0)], self.ground, keys=[0]), [])


ROADS_LUA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "mod", "beamng_earth", "lua", "ge",
                         "extensions", "beamngEarth", "roads.lua")


class FarPoints(unittest.TestCase):
    """The lights of the far and skyline tiles (far_points): tile_rows' without the ILS aerials, [x, y, z, c]."""

    def setUp(self):
        self.rw = runway(LONG)               # (approach lights and ILS aerials)
        self.f = feats(way(PARALLEL), way(CROSSING))
        self.ground = lambda lat, lon: np.full(np.shape(lat), 12.0)
        self.box = (-1200.0, -600.0, 4200.0, 600.0)

    def points(self, box):
        return al.far_points(FRAME, box, lambda k: self.f, lambda k: [self.rw], self.ground)

    def test_the_lights_of_the_road_tiles_without_the_aerials_each_in_a_group_number(self):
        rows = al.tile_rows(FRAME, self.box, lambda k: self.f, lambda k: [self.rw], self.ground)
        lights = [r for r in rows if r[0] in al.GROUPS.values()]
        self.assertGreater(len(rows), len(lights), "the road tiles' rows hold ILS aerials")
        ref = al.tile_rows(FRAME, self.box, lambda k: self.f, lambda k: [self.rw], self.ground, masts=False, skip=al.FAR_SKIP, far=True)
        got = self.points(self.box)
        self.assertEqual(len(got), len(ref))
        self.assertLess(len(got), len(lights), "one light a barrette, no guard or touchdown zone lights: fewer")
        self.assertTrue(all(len(p) == 4 for p in got))
        self.assertEqual({p[3] for p in got}, set(range(1, len(al.COLOURS) + 1)), "every colour")
        num = {g: i + 1 for i, g in enumerate(al.FAR_GROUPS)}
        self.assertEqual(sorted((r[1], r[2], r[3], num[r[0]]) for r in ref), sorted(tuple(p) for p in got))
        self.assertEqual(al.FAR_GROUPS, tuple("afl_" + c for c in al.COLOURS))

    def test_a_far_barrette_is_its_middle_light_and_the_kinds_left_out_are_none(self):
        full, far = al.runway_lights(self.rw), al.runway_lights(self.rw, far=True)
        self.assertIsNot(full, far)
        self.assertIs(far, al.runway_lights(self.rw, far=True))
        e, n = en(far, al.APPROACH, al.WHITE)
        self.assertEqual(len(e), 2 * (30 + 2 * 10), "a light a row and the crossbars' (a crossbar's lights are 3 m apart)")
        self.assertEqual(len(en(far, al.APPROACH, al.RED)[0]), 2 * 8 * 2, "a light each side of a side row")
        self.assertEqual(len(en(full, al.TDZ)[0]), 2 * 30 * 6)
        self.assertEqual(len(en(far, al.TDZ)[0]), 2 * 30 * 2)
        for kind in al.FAR_SKIP:
            plain = al.tile_rows(FRAME, self.box, lambda k: self.f, lambda k: [self.rw], self.ground, masts=False)
            rows = al.tile_rows(FRAME, self.box, lambda k: self.f, lambda k: [self.rw], self.ground, masts=False, skip=(kind,))
            self.assertEqual(len(plain) - len(rows), len(en(full, kind)[0]), kind)

    def test_on_the_ground_and_each_light_in_one_tile(self):
        whole = self.points(self.box)
        for p in whole:
            self.assertAlmostEqual(p[2], 12.0 + al.LIFT_M, delta=1.2)       # (the frame's up falls with the curvature)
        a = self.points((-1200.0, -600.0, 1000.0, 600.0))
        b = self.points((1000.0, -600.0, 4200.0, 600.0))
        self.assertEqual(len(a) + len(b), len(whole))
        self.assertTrue(all(p[0] < 1000.0 for p in a) and all(p[0] >= 1000.0 for p in b))
        self.assertEqual(self.points((9000.0, 9000.0, 9256.0, 9256.0)), [])

    def test_none_without_a_lit_runway(self):
        self.assertEqual(al.far_points(FRAME, self.box, lambda k: None, lambda k: [], self.ground), [])
        self.assertEqual(al.far_points(FRAME, self.box, lambda k: self.f, lambda k: [runway(lighted=0)], self.ground), [])

    def test_roads_lua_draws_the_numbers_in_these_groups(self):
        with open(ROADS_LUA, encoding="utf-8") as fh:
            m = re.search(r"^M\.FAR_LAMP_GROUPS = \{([^}]*)\}", fh.read(), re.M)
        self.assertIsNotNone(m)
        self.assertEqual(re.findall(r"'([^']+)'", m.group(1)), ["lampglow", *al.FAR_GROUPS, "apron_glow"],
                         "the apron masts' pool is the last (airsideprops.FAR_POOL)")


class FarGeotiles(unittest.TestCase):
    """blockworker's far geotiles over a lit runway: the airfield lights in their lamps (a tile with no buildings too),
    the infrastructure cells waited for, the revisit key changed there only."""

    def setUp(self):
        import blockworker as bw
        self.bw = bw
        ap = af.airport(1, "KTST", 40.0, -76.0, 100)
        row = af.runway(1, ap, 40.0, -76.0, 90.0, LONG, width_m=WIDTH)
        self.db = airports.AirportDB.from_csv(af.to_csv([ap], af.AIRPORT_COLS), af.to_csv([row], af.RUNWAY_COLS))
        self.f = feats(way(PARALLEL), way(CROSSING))
        self.have = True
        self.warmed = []
        md = types.SimpleNamespace(
            has=lambda k, layer: self.have,
            warm=lambda need, kind=None: self.warmed.append((list(need), kind)),
            cell=lambda k, layers=(), kind=None: types.SimpleNamespace(layers={"infrastructure": self.f}))
        flat = types.SimpleNamespace(sample=lambda la, lo, z: np.full(np.shape(la), 12.0))
        for patch in (mock.patch.object(bw, "_s", types.SimpleNamespace(airports=types.SimpleNamespace(db=self.db), md=md,
                                                                        dem=flat, widths=None)),
                      mock.patch.object(bw, "ground", lambda frame, la, lo, z: np.asarray(z, np.float64))):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(bw._far_cells_miss.clear)
        la, lo, _ = FRAME.enu_to_geodetic(1500.0, 0.0, 0.0)          # (a tile on the runway's middle)
        self.tile = geotiles.geobox(*geotiles.tile_of(float(la), float(lo), geotiles.FAR_Z), geotiles.FAR_Z)
        self.raw = lambda la_, lo_: np.full(np.shape(la_), 12.0)
        la, lo = 40.0, -76.0 + 80000.0 / 85000.0                    # (80 km east: no airport in reach)
        self.away = geotiles.geobox(*geotiles.tile_of(la, lo, geotiles.FAR_Z), geotiles.FAR_Z)

    def test_a_tile_over_the_runway_gets_its_lights_in_its_own_frame(self):
        got = self.bw._far_afl("bldfar", self.tile, self.raw)
        x0, y0, x1, y1 = self.tile.box
        self.assertGreater(len(got), 4)
        self.assertTrue(all(len(p) == 4 and 1 <= p[3] <= 5 for p in got))
        self.assertTrue(all(x0 - 0.01 <= p[0] <= x1 + 0.01 and y0 - 0.01 <= p[1] <= y1 + 0.01 for p in got))
        # (a skyline square holds its far tiles' lights, each once: each light in one tile)
        sx, sy = geotiles.parent(self.tile.gx, self.tile.gy, geotiles.FAR_Z, geotiles.SKY_Z)
        sq = geotiles.geobox(sx, sy, geotiles.SKY_Z)
        kids = [geotiles.geobox(a, b, geotiles.FAR_Z) for a, b in geotiles.children(sx, sy, geotiles.SKY_Z, geotiles.FAR_Z)]
        self.assertEqual(len(self.bw._far_afl("bldsky", sq, self.raw)),
                         sum(len(self.bw._far_afl("bldfar", k, self.raw)) for k in kids))

    def test_none_far_from_an_airport_and_the_key_changes_only_near_one(self):
        self.assertEqual(self.bw._far_afl("bldfar", self.away, self.raw), [])
        self.assertEqual(self.bw._far_afl_key(self.away), ())
        near = self.bw._far_afl_key(self.tile)
        self.assertEqual(near[0], f"afl={al.FAR_FORMAT}")
        self.assertIn(self.db.snapshot, near[1])

    def test_waits_for_the_infrastructure_cells_in_the_lane_asked(self):
        self.have = False
        self.assertIsNone(self.bw._far_afl("bldsky", self.tile, self.raw, "prefetch"))
        self.assertTrue(self.warmed and all(layer in ("infrastructure", "buildings", "building_parts")
                                            for need, _k in self.warmed for _c, layer in need))
        self.assertEqual({k for _n, k in self.warmed}, {"prefetch"})
        self.bw._far_cells_miss.clear()
        self.assertIsNone(self.bw._far_afl("bldfar", self.tile, self.raw))
        self.assertEqual(self.warmed[-1][1], "demand")
        self.have = True
        self.assertGreater(len(self.bw._far_afl("bldfar", self.tile, self.raw)), 0)

    def test_the_lamps_by_road_geotile_with_the_airfield_rows_beside_them(self):
        pts = self.bw._far_afl("bldfar", self.tile, self.raw)
        lamps = [[pts[0][0], pts[0][1], pts[0][2] + 8.0]]
        by = self.bw._lamps_by_geotile(self.tile.frame, lamps, pts)
        self.assertEqual(set(by), {self.tile.key})
        rows = by[self.tile.key]["rows"]
        self.assertEqual(len(rows), 1 + len(pts))
        self.assertEqual(len(rows[0]), 3, "a street lamp's row as before")
        self.assertTrue(all(len(r) == 4 for r in rows[1:]) and [r[3] for r in rows[1:]] == [p[3] for p in pts])
        self.assertEqual(self.bw._lamps_by_geotile(self.tile.frame, [], []), {})
        alone = self.bw._lamps_by_geotile(self.tile.frame, lamps)[self.tile.key]["rows"]
        self.assertEqual(len(alone), 1)
        self.assertEqual(len(alone[0]), 3, "the lamps alone: as before")

    def test_a_far_tile_with_no_buildings_carries_the_lights(self):
        import buildings

        def build(tile):
            with mock.patch.object(buildings, "far_needs", lambda *a: ([], ())), \
                    mock.patch.object(buildings, "far_items", lambda frame, t, *a, **k: ([], [], t.box)):
                return self.bw._far_geo_build("bldfar", tile, 15, "/tmp", "/v", None)
        data, shapes, n = build(self.tile)
        self.assertEqual((shapes, n), ({}, 0))
        self.assertNotIn("shape", data)
        rows = data["lamps"][self.tile.key]["rows"]
        self.assertGreater(len(rows), 4)
        self.assertTrue(all(len(r) == 4 for r in rows))
        data, _shapes, _n = build(self.away)             # (far from any runway: nothing, as before)
        self.assertNotIn("lamps", data)

    def test_the_skyline_tile_waits_for_the_cells_before_its_buildings(self):
        import buildings
        self.have = False
        with mock.patch.object(buildings, "sky_items", side_effect=AssertionError("not asked")):
            sx, sy = geotiles.parent(self.tile.gx, self.tile.gy, geotiles.FAR_Z, geotiles.SKY_Z)
            sq = geotiles.geobox(sx, sy, geotiles.SKY_Z)
            self.assertIsNone(self.bw._far_geo_build("bldsky", sq, 15, "/tmp", "/v", None))


class CentreLights(unittest.TestCase):
    """The runway centreline lights (the user, 2026-11-17: "no center runway line"): white, CENTRE_SPACING_M apart along
    the centreline from threshold to threshold, on the runways with approach lights only."""

    def test_a_long_runway_has_a_white_line_down_its_middle(self):
        lights = al.runway_lights(runway(LONG))
        e, n = en(lights, al.CENTRE)
        self.assertGreater(len(e), LONG / al.CENTRE_SPACING_M - 2)
        self.assertEqual(len(e), len(en(lights, al.CENTRE, al.WHITE)[0]))
        self.assertLess(float(np.abs(n).max()), 0.5)
        e = np.sort(e)
        self.assertTrue(np.allclose(np.diff(e), al.CENTRE_SPACING_M, atol=0.5))
        self.assertGreaterEqual(float(e[0]), -0.5)
        self.assertLessEqual(float(e[-1]), LONG + 0.5)

    def test_a_short_runway_has_none(self):
        self.assertEqual(len(en(al.runway_lights(runway(LENGTH)), al.CENTRE)[0]), 0)



def point(x, y, cls="holding_position"):
    return (mapgeom.POINT, [(x, y)], cls, "airport", {"aeroway": cls}, None)


RUNWAY_LINE = lambda: way([(-300.0, 0.0), (LENGTH + 300.0, 0.0)], cls="runway", tags={"width": str(WIDTH)})
HOLD = 60.0                                         # (taxiwaymarks.HOLD_DIST_M: the holding position from the centreline)
CROSS_X = 700.0                                     # a taxiway across the runway here (the holds 60 m out each side)
CROSSING_WAY = lambda **kw: way([(CROSS_X, -300.0), (CROSS_X, 300.0)], **kw)


class TaxiwayCentreLights(unittest.TestCase):
    """The green centre lights along the taxiways' centrelines as painted (the user, 2026-11-17: l5 photograph)."""

    def setUp(self):
        self.rw = runway()

    def lights(self, *rows, runways=None, runway_line=True):
        f = feats(*([RUNWAY_LINE()] if runway_line else []), *rows)
        return al.taxiway_centre_lights([f], [self.rw] if runways is None else runways)

    @staticmethod
    def of(lights, kind, colour=None):
        e, n = en(lights, kind, colour)
        o = np.argsort(e, kind="stable")
        return e[o], n[o]

    def test_green_on_the_centreline_30_m_apart_end_to_end(self):
        got = self.lights(way(PARALLEL))
        e, n = self.of(got, al.TAXICL)
        self.assertEqual(set(zip(got.colour.tolist(), got.kind.tolist())), {(al.COLOURS.index(al.GREEN), al.KINDS.index(al.TAXICL))})
        self.assertTrue(np.allclose(n, 150.0, atol=0.3), "on the painted line")
        self.assertAlmostEqual(float(e[0]), 200.0, delta=1.0)
        self.assertAlmostEqual(float(e[-1]), 1600.0, delta=1.0)
        d = np.diff(e)
        self.assertLessEqual(float(d.max()), al.TAXI_SPACING_M + 0.5)
        self.assertGreaterEqual(float(d.min()), al.TAXI_SPACING_M / 2.0 - 0.5)
        self.assertEqual(int((np.abs(d - al.TAXI_SPACING_M) < 0.5).sum()), len(d) - 1, "30 m but the last step")
        self.assertAlmostEqual(len(e), 1400.0 / al.TAXI_SPACING_M + 1, delta=1.5)

    def test_closer_where_the_painted_line_turns(self):
        # (a corner is painted as an arc of taxiwaymarks.MIN_RADIUS_M: lights TAXI_CURVE_STEP_M apart there, 30 m on the straights)
        got = self.lights(way([(100.0, 200.0), (500.0, 200.0), (500.0, 600.0)]))
        e, n = en(got, al.TAXICL)
        pts = np.stack([e, n], 1)
        pts = pts[np.argsort(np.where(n < 200.5, e, 1000.0 + n))]            # (along the way)
        steps = np.hypot(*np.diff(pts, axis=0).T)
        corner = np.hypot(pts[:, 0] - 500.0, pts[:, 1] - 200.0)
        around = (corner[:-1] < 30.0) & (corner[1:] < 30.0)
        self.assertGreaterEqual(int(around.sum()), 2, "lights on and about the arc")
        self.assertLessEqual(float(steps[around].max()), al.TAXI_CURVE_STEP_M + 0.5, "15 m apart where it turns")
        straight = (corner[:-1] > 60.0) & (corner[1:] > 60.0)
        self.assertGreater(int(straight.sum()), 10)
        self.assertTrue(np.allclose(steps[straight][1:-1], al.TAXI_SPACING_M, atol=0.5), "30 m apart on the straights")

    def test_only_a_taxiway_at_an_airport_whose_runway_is_lit(self):
        self.assertGreater(len(self.lights(way(PARALLEL))), 0)
        for runways in ([runway(lighted=0)], [runway(closed=1)], []):
            self.assertEqual(len(self.lights(way(PARALLEL), runways=runways)), 0)
        self.assertEqual(len(self.lights(way([(20200.0, 150.0), (21600.0, 150.0)]))), 0, "no runway near")
        self.assertEqual(len(self.lights(way(PARALLEL), runways=[runway(surface="WATER")])), 0)

    def test_not_a_taxilane_an_unpaved_or_unlit_way_or_another_feature(self):
        for kw in (dict(cls="taxilane"), dict(surface="grass"), dict(tags={"lit": "no"}), dict(sub="transit")):
            self.assertEqual(len(self.lights(way(PARALLEL, **kw))), 0, kw)
        self.assertEqual(len(self.lights(way(PARALLEL, cls="road", sub="road"))), 0)
        self.assertEqual(len(al.taxiway_centre_lights([None], [self.rw])), 0)
        self.assertEqual(len(al.taxiway_centre_lights([], [self.rw])), 0)
        # (the lit way keeps its lights beside an unlit one)
        both = self.lights(way(PARALLEL), way([(200.0, 300.0), (1600.0, 300.0)], tags={"lit": "no"}))
        self.assertTrue(np.allclose(en(both, al.TAXICL)[1], 150.0, atol=0.3))

    def test_none_on_a_stand_lead_in_that_is_not_a_mapped_way(self):
        # a taxilane (the aprons' own way) has none, and the lead-ins of the stands (standpaths) are not mapped ways at all
        got = self.lights(way(PARALLEL), way([(900.0, 150.0), (900.0, 400.0)], cls="taxilane"))
        self.assertFalse((en(got, al.TAXICL)[1] > 160.0).any())

    def test_none_on_the_runway_but_the_lead_on_and_lead_off_lights(self):
        got = self.lights(CROSSING_WAY())
        centre = (got.kind == al.KINDS.index(al.TAXICL)) | (got.kind == al.KINDS.index(al.LEAD))
        e, n = en(got.select(centre))
        half = WIDTH / 2.0
        self.assertGreater(len(e), 4)
        self.assertFalse((np.abs(n) < half).any(), "the way is painted up to the runway's edge, its lights too")
        self.assertTrue(np.allclose(e, CROSS_X, atol=0.3))

    def test_deterministic_and_memoised_on_the_features(self):
        f = feats(RUNWAY_LINE(), way(PARALLEL))
        a = al.taxiway_centre_lights([f], [self.rw])
        self.assertIs(a, al.taxiway_centre_lights([f], [self.rw]))
        self.assertEqual(len(al.taxiway_centre_lights([f], [runway(lighted=0)])), 0)
        self.assertIs(a, al.taxiway_centre_lights([f], [self.rw]))
        b = al.taxiway_centre_lights([feats(RUNWAY_LINE(), way(PARALLEL))], [self.rw])
        self.assertTrue(np.array_equal(a.lat, b.lat) and np.array_equal(a.lon, b.lon) and np.array_equal(a.colour, b.colour))

    def test_none_on_a_runway_the_data_does_not_draw(self):
        # (the mapped way runs over a runway of the airports' data that has no line of its own: no ordinary centre light on it)
        got = self.lights(CROSSING_WAY(), runway_line=False)
        e, n = en(got, al.TAXICL)
        self.assertGreater(len(e), 4)
        self.assertFalse((np.abs(n) < WIDTH / 2.0 + 0.5).any())


class LeadOnAndOff(unittest.TestCase):
    """Where a taxiway centreline enters a runway: alternately green and yellow from the runway to the holding position."""

    def setUp(self):
        self.rw = runway()

    def lights(self, *rows):
        return al.taxiway_centre_lights([feats(RUNWAY_LINE(), *rows)], [self.rw])

    def test_a_crossing_way_alternates_from_the_runway_edge_to_the_hold_both_sides(self):
        got = self.lights(CROSSING_WAY())
        e, n = en(got, al.LEAD)
        c = got.colour[got.kind == al.KINDS.index(al.LEAD)]
        for side in (1.0, -1.0):
            sel = np.flatnonzero(side * n > 0)
            o = sel[np.argsort(side * n[sel])]
            y = side * n[o]
            self.assertAlmostEqual(float(y[0]), WIDTH / 2.0 + 1.0, delta=0.3, msg="from the line's end at the runway")
            self.assertTrue(np.allclose(np.diff(y), al.LEAD_SPACING_M, atol=0.3))
            self.assertLess(float(y[-1]), HOLD + 0.1)
            self.assertGreater(float(y[-1]), HOLD - al.LEAD_SPACING_M)
            want = [al.COLOURS.index(al.GREEN if i % 2 == 0 else al.YELLOW) for i in range(len(y))]
            self.assertEqual(c[o].tolist(), want, "green first, then yellow, ...")
            self.assertGreaterEqual(len(y), 3)

    def test_green_after_the_hold(self):
        got = self.lights(CROSSING_WAY())
        _, n = en(got, al.TAXICL, al.GREEN)
        self.assertTrue((np.abs(n) > HOLD).all(), "the ordinary green beyond the holding position")
        self.assertTrue((np.abs(n) > 0).any())

    def test_a_way_ending_on_the_centreline_runs_on_to_it(self):
        got = self.lights(way([(300.0, 0.0), (300.0, 300.0)]))
        e, n = en(got, al.LEAD)
        o = np.argsort(n)
        self.assertTrue(np.allclose(n[o], [0.0, 15.0, 30.0, 45.0, 60.0], atol=0.3), n[o])
        colours = got.colour[got.kind == al.KINDS.index(al.LEAD)][o].tolist()
        self.assertEqual(colours, [al.COLOURS.index(c) for c in (al.GREEN, al.YELLOW, al.GREEN, al.YELLOW, al.GREEN)])
        self.assertTrue(np.allclose(e, 300.0, atol=0.3))

    def test_a_mapped_holding_position_ends_the_lead_there(self):
        got = self.lights(CROSSING_WAY(), point(CROSS_X, 85.0))
        _, n = en(got, al.LEAD)
        up = np.sort(n[n > 0])
        self.assertAlmostEqual(float(up[-1]), 23.86 + 15.0 * 4, delta=0.5)         # (the last lead light before 85 m)
        self.assertLess(float(up[-1]), 85.1)
        self.assertEqual(len(up), 5)

    def test_without_a_hold_the_lead_is_60_m(self):
        got = self.lights(way([(300.0, 20.0), (300.0, 400.0)]))
        _, n = en(got, al.LEAD)
        self.assertGreater(len(n), 0)
        self.assertLessEqual(float(n.max()) - float(n.min()), al.LEAD_DEFAULT_M + 0.5)

    def test_none_at_an_unlit_airport_and_none_where_the_way_stops_short(self):
        self.assertEqual(len(al.taxiway_centre_lights([feats(RUNWAY_LINE(), CROSSING_WAY())], [runway(lighted=0)])), 0)
        got = self.lights(way([(300.0, 100.0), (300.0, 400.0)]))
        self.assertEqual(len(en(got, al.LEAD)[0]), 0, "an end 100 m from the runway is no lead-on")
        self.assertEqual(len(en(got, al.STOPBAR)[0]), 0)


class StopBarsAndGuardLights(unittest.TestCase):
    """A red stop bar and the runway guard lights at each runway holding position (where the hold marking is painted)."""

    def setUp(self):
        self.rw = runway()

    def lights(self, *rows, runways=None):
        return al.taxiway_centre_lights([feats(RUNWAY_LINE(), *rows)], [self.rw] if runways is None else runways)

    def test_red_lights_3_m_apart_across_the_taxiway_at_each_hold(self):
        got = self.lights(CROSSING_WAY())
        e, n = en(got, al.STOPBAR, al.RED)
        self.assertEqual(len(e), len(en(got, al.STOPBAR)[0]), "all red")
        for side in (1.0, -1.0):
            bar = e[np.abs(n - side * 60.0) < 0.6]
            self.assertEqual(len(bar), int(np.ceil(15.0 / al.STOPBAR_STEP_M)) + 1)
            bar = np.sort(bar)
            self.assertAlmostEqual(float(bar[0]), CROSS_X - 7.5, delta=0.1)
            self.assertAlmostEqual(float(bar[-1]), CROSS_X + 7.5, delta=0.1)
            self.assertLessEqual(float(np.diff(bar).max()), al.STOPBAR_STEP_M + 0.01)
        self.assertEqual(len(e), 12)

    def test_the_bar_follows_the_width_and_a_mapped_holding_position(self):
        wide = self.lights(way([(CROSS_X, -300.0), (CROSS_X, 300.0)], tags={"width": "30"}))
        e, n = en(wide, al.STOPBAR)
        up = np.sort(e[n > 0])
        self.assertEqual(len(up), 11)
        self.assertAlmostEqual(float(up[-1] - up[0]), 30.0, delta=0.1)
        moved = self.lights(CROSSING_WAY(), point(CROSS_X, 85.0))
        _, n = en(moved, al.STOPBAR)
        self.assertAlmostEqual(float(n.max()), 85.0, delta=0.6, msg="a mapped holding position is where the bar stands")
        self.assertAlmostEqual(float(n.min()), -HOLD, delta=0.6, msg="the other side's is the default")
        self.assertEqual(len(n), 12)

    def test_a_yellow_pair_each_side_outside_the_edge_at_the_hold(self):
        got = self.lights(CROSSING_WAY())
        e, n = en(got, al.GUARD, al.YELLOW)
        self.assertEqual(len(e), len(en(got, al.GUARD)[0]), "all yellow")
        self.assertEqual(len(e), 8, "four at each of the two holds")
        out = 7.5 + al.GUARD_OUT_M
        for side in (1.0, -1.0):
            at = side * 60.0
            for sx in (-1.0, 1.0):
                pair = np.sort(n[(np.abs(n - at) < 2.0) & (np.abs(e - (CROSS_X + sx * out)) < 0.2)])
                self.assertEqual(len(pair), 2, (side, sx))
                self.assertAlmostEqual(float(pair[1] - pair[0]), al.GUARD_PAIR_M, delta=0.05)
                self.assertAlmostEqual(float((pair[0] + pair[1]) / 2.0), at, delta=0.5)

    def test_guard_lights_stand_higher_than_the_others(self):
        ground = lambda lat, lon: np.full(np.shape(lat), 12.0)
        f = feats(RUNWAY_LINE(), CROSSING_WAY())
        rows = al.tile_rows(FRAME, (-300.0, -400.0, 1800.0, 400.0), lambda k: f, lambda k: [self.rw], ground, keys=[0])
        yellow = [r for r in rows if r[0] == al.GROUPS[al.YELLOW] and abs(abs(r[1] - CROSS_X) - 10.5) < 0.3 and abs(abs(r[2]) - 60.0) < 2.0]
        self.assertEqual(len(yellow), 8)
        self.assertTrue(all(abs(r[3] - (12.0 + al.LIFT_M + al.GUARD_LIFT_M)) < 0.6 for r in yellow))
        red = [r for r in rows if r[0] == al.GROUPS[al.RED] and abs(abs(r[2]) - 60.0) < 0.6 and abs(r[1] - CROSS_X) < 8.0]
        self.assertEqual(len(red), 12)
        self.assertTrue(all(abs(r[3] - (12.0 + al.LIFT_M)) < 0.6 for r in red))

    def test_each_light_in_one_tile(self):
        ground = lambda lat, lon: np.full(np.shape(lat), 12.0)
        f = feats(RUNWAY_LINE(), CROSSING_WAY(), way(PARALLEL))

        def rows(box, **kw):
            return al.tile_rows(FRAME, box, lambda k: f, lambda k: [self.rw], ground, keys=[0], **kw)
        whole = rows((-300.0, -400.0, 1900.0, 400.0))
        a, b = rows((-300.0, -400.0, CROSS_X + 3.3, 400.0)), rows((CROSS_X + 3.3, -400.0, 1900.0, 400.0))
        self.assertEqual(len(a) + len(b), len(whole), "a stop bar cut by the tiles' edge: each light once")
        self.assertEqual(sorted(map(tuple, a + b)), sorted(map(tuple, whole)))
        self.assertGreater(len(whole), 100)

    def test_only_where_a_runway_is_met_and_the_airport_is_lit(self):
        self.assertEqual(len(self.lights(way(PARALLEL))), len(self.lights(way(PARALLEL))))
        self.assertEqual(len(en(self.lights(way(PARALLEL)), al.STOPBAR)[0]), 0, "a parallel taxiway meets no runway")
        self.assertEqual(len(self.lights(CROSSING_WAY(), runways=[runway(lighted=0)])), 0)
        self.assertEqual(len(en(self.lights(CROSSING_WAY(cls="taxilane")), al.STOPBAR)[0]), 0)
        self.assertEqual(len(en(self.lights(CROSSING_WAY(tags={"lit": "no"})), al.STOPBAR)[0]), 0)
        self.assertEqual(len(en(self.lights(CROSSING_WAY(surface="gravel")), al.STOPBAR)[0]), 0)

    def test_the_holds_are_the_markings_holds(self):
        # the sites of the Scene are where taxiwaymarks paints its hold marking: the bar stands on the first dashed line
        import taxiwaymarks as tm
        f = feats(RUNWAY_LINE(), CROSSING_WAY())
        marks = [m for m in tm.strokes([f]) if m[5] == "hold_dashed"]
        e, n, _ = FRAME.geodetic_to_enu(np.array([m[0] for m in marks]), np.array([m[1] for m in marks]), 0.0)
        got = al.taxiway_centre_lights([f], [self.rw])
        _, ny = en(got, al.STOPBAR)
        for side in (1.0, -1.0):
            self.assertAlmostEqual(float(np.min(np.abs(np.asarray(n)[np.sign(n) == side]))), float(np.abs(ny[np.sign(ny) == side]).min()), delta=0.2)


class TouchdownZoneLights(unittest.TestCase):
    """Two rows of white barrettes either side of the centreline every 30 m over the first 900 m of each end (CAT runways)."""

    def test_barrettes_of_three_9_m_off_the_centreline_every_30_m_from_each_threshold(self):
        lights = al.runway_lights(runway(LONG))
        e, n = en(lights, al.TDZ, al.WHITE)
        self.assertEqual(len(e), len(en(lights, al.TDZ)[0]), "all white")
        self.assertEqual(len(e), 2 * 30 * 6)
        for at_le in (True, False):
            for k in range(1, 31):
                x = k * al.TDZ_STEP_M if at_le else LONG - k * al.TDZ_STEP_M
                across = np.sort(n[np.abs(e - x) < 0.5])
                self.assertTrue(np.allclose(across, [-10, -9, -8, 8, 9, 10], atol=0.05), (at_le, k, across))
        self.assertAlmostEqual(float(e.min()), al.TDZ_STEP_M, delta=0.5)
        self.assertAlmostEqual(float(e[e < LONG / 2].max()), al.TDZ_LEN_M, delta=0.5)
        self.assertAlmostEqual(float(e[e > LONG / 2].min()), LONG - al.TDZ_LEN_M, delta=0.5)
        self.assertGreater(float(np.abs(n).min()), 7.0, "off the centreline's lights")

    def test_only_on_the_large_runways_paved_lit_and_open(self):
        def has(rw):
            return len(en(al.runway_lights(rw), al.TDZ)[0]) > 0
        self.assertTrue(has(runway(LONG)))
        self.assertTrue(has(runway(2500.0)))
        self.assertFalse(has(runway(2300.0)), "under CAT_MIN_RUNWAY_M at a medium airport")
        self.assertTrue(has(runway(1500.0, type="large_airport")))
        self.assertFalse(has(runway(LONG, surface="GRS")))
        self.assertFalse(has(runway(LONG, lighted=0)))
        self.assertFalse(has(runway(LONG, closed=1)))
        self.assertFalse(has(runway(LONG, type="closed")))

    def test_not_past_the_middle_of_a_short_runway_and_after_a_displaced_threshold(self):
        e, _ = en(al.runway_lights(runway(1500.0, type="large_airport")), al.TDZ)
        self.assertEqual(len(e), 2 * 25 * 6, "750 m each end: the two ends' rows do not meet")
        self.assertLess(float(e[e < 750.0].max()), 750.0 + 0.5)
        self.assertGreater(float(e[e > 750.0].min()), 750.0 - 0.5)
        e, _ = en(al.runway_lights(runway(LONG, le_disp_ft=600)), al.TDZ)
        d = 600 * airports.FT
        self.assertAlmostEqual(float(e.min()), d + al.TDZ_STEP_M, delta=1.0, msg="counted from the displaced threshold")

    def test_the_far_tiles_have_one_light_a_barrette_and_none_of_these(self):
        far = al.runway_lights(runway(LONG), far=True)
        e, n = en(far, al.TDZ)
        self.assertEqual(len(e), 2 * 30 * 2)
        self.assertTrue(np.allclose(np.abs(n), 9.0, atol=0.05))
        self.assertIn("tdz", al.FAR_SKIP)


class ThresholdWingBars(unittest.TestCase):
    """Green wing bars: five lights outward beyond each runway edge at the threshold."""

    def test_five_green_lights_each_side_each_threshold_out_from_the_edge_lights(self):
        lights = al.runway_lights(runway())
        e, n = en(lights, al.WING, al.GREEN)
        self.assertEqual(len(e), len(en(lights, al.WING)[0]), "all green")
        self.assertEqual(len(e), 2 * 2 * al.WING_N)
        out = WIDTH / 2.0 + al.EDGE_OUT_M + al.WING_IN_M
        for x in (0.0, LENGTH):
            for side in (1.0, -1.0):
                bar = np.sort(side * n[(np.abs(e - x) < 0.5) & (side * n > 0)])
                self.assertEqual(len(bar), al.WING_N)
                self.assertAlmostEqual(float(bar[0]), out, delta=0.05)
                self.assertTrue(np.allclose(np.diff(bar), al.WING_STEP_M, atol=0.05))
                self.assertAlmostEqual(float(bar[-1] - WIDTH / 2.0), al.WING_IN_M + al.EDGE_OUT_M + 4 * al.WING_STEP_M, delta=0.05)
        self.assertGreaterEqual(al.WING_N * al.WING_STEP_M, 10.0 - al.WING_STEP_M, "ICAO: about 10 m out")

    def test_the_wing_bar_is_at_the_displaced_threshold(self):
        e, _ = en(al.runway_lights(runway(le_disp_ft=600)), al.WING)
        self.assertAlmostEqual(float(e.min()), 600 * airports.FT, delta=0.5)
        self.assertAlmostEqual(float(e.max()), LENGTH, delta=0.5)

    def test_on_every_lit_open_runway_and_none_otherwise(self):
        def has(rw):
            return len(en(al.runway_lights(rw), al.WING)[0]) > 0
        self.assertTrue(has(runway(LONG)))
        self.assertTrue(has(runway(900.0, surface="GRS")))
        self.assertFalse(has(runway(lighted=0)))
        self.assertFalse(has(runway(closed=1)))
        self.assertFalse(has(runway(surface="WATER")))

    def test_not_on_an_edge_light_and_in_the_far_tiles_whole(self):
        lights = al.runway_lights(runway())
        e, n = en(lights)
        key = np.round(np.c_[e, n] * 2.0).astype(np.int64)
        self.assertEqual(len(np.unique(key, axis=0)), len(e), "no two lights within half a metre (tile_rows keeps one)")
        self.assertEqual(len(en(al.runway_lights(runway(), far=True), al.WING)[0]), 2 * 2 * al.WING_N)


class ApproachSideRowsAndFar(unittest.TestCase):
    """The approach barrettes: the clearing takes whole barrettes; the red side rows stand where the white do not."""

    def test_no_two_lights_within_half_a_metre_on_a_cat_runway(self):
        for far in (False, True):
            lights = al.runway_lights(runway(LONG), far=far)
            e, n = en(lights)
            key = np.round(np.c_[e, n] * 2.0).astype(np.int64)
            self.assertEqual(len(np.unique(key, axis=0)), len(e), far)

    def test_a_far_runway_has_fewer_lights_but_the_same_rows(self):
        full, far = al.runway_lights(runway(LONG)), al.runway_lights(runway(LONG), far=True)
        self.assertLess(len(far), len(full))
        for kind in (al.EDGE, al.THRESHOLD, al.END, al.PAPI, al.CENTRE, al.WING):
            self.assertEqual(len(en(far, kind)[0]), len(en(full, kind)[0]), kind)
        self.assertEqual(len(en(far, al.APPROACH, al.WHITE)[0]), 2 * (30 + 20))
        e, n = en(far, al.APPROACH, al.WHITE)
        centre = np.sort(e[np.abs(n) < 0.5])
        self.assertEqual(len(centre[centre < 0]), 30, "the middle light of each barrette is the old single row's")


class TaxiwayLightCounts(unittest.TestCase):
    """The counts the new lights add to a big airport (a 3 km runway and 10 km of taxiway): the budget the far tiles keep to."""

    def test_budget(self):
        rw = runway(LONG)
        full, far = al.runway_lights(rw), al.runway_lights(rw, far=True)
        self.assertLess(len(full), 1400)
        self.assertLess(len(far), 700)
        taxi = [way([(200.0 + 2000.0 * (i % 2), 150.0 + 150.0 * (i // 2)), (2200.0 + 2000.0 * (i % 2), 150.0 + 150.0 * (i // 2))])
                for i in range(0, 5, 2)]
        centre = al.taxiway_centre_lights([feats(RUNWAY_LINE(), *taxi)], [rw])
        blue = al.taxiway_lights(feats(*taxi), [rw])
        self.assertLess(len(centre), len(blue) / 1.5, "the centre lights are one row to the edge lights' two")


if __name__ == "__main__":
    unittest.main()
