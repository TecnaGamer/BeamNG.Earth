"""hkbuildings: Hong Kong's measured buildings (the Lands Department's Building layer, joined as France's BD TOPO is)
fill the height and storeys the map leaves out of the footprint a row's point stands in; a tower's height is measured
from its podium's top; the map's own values stay; nothing is asked for outside Hong Kong, the downloads run in the
background and a tile never waits past its bound; no road plan reads any of it."""
import json
import math
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bdtopo  # noqa: E402
import buildings as bld  # noqa: E402
import fast  # noqa: E402
import hkbuildings  # noqa: E402
import hkdtm  # noqa: E402
import mapcells  # noqa: E402
import mapgeom  # noqa: E402
import revisit  # noqa: E402

LOHAS = (22.2967, 114.2696)         # (lat, lon): a tower district
AIX = (43.5297, 5.4474)
LON, LAT = LOHAS[1], LOHAS[0]


def square(lon, lat, w=0.0001, d=0.0001):
    return [[lon, lat], [lon + w, lat], [lon + w, lat + d], [lon, lat + d], [lon, lat]]


def row(ring, block="Tower", base=None, top=None, storeys=None):
    return {"attributes": {"OBJECTID": 1, "BuildingBlockType": block, "BaseHeight": base, "TopHeight": top,
                           "Storeys": storeys}, "geometry": {"rings": [ring]}}


def layer(rings, **cols):
    """A buildings Features of rings (lon/lat lists) with attrs cols (lists). Other features may stand among the
    polygons: "line" and "point" (not polygons, though a line has a ring round a record), "empty" (a polygon whose part
    has no ring) and "bare" (a polygon without a part)."""
    n = len(rings)
    kind, part_off, ring_off, co, pts = [], [0], [0], [0], []
    odd = {"line": (square(LON + 0.004, LAT), mapgeom.LINE), "point": ([[LON + 0.004, LAT]], mapgeom.POINT)}
    for r in rings:
        if isinstance(r, str) and r in ("empty", "bare"):
            kind.append(mapgeom.POLYGON)
            part_off.append(part_off[-1] + (r == "empty"))
            if r == "empty":
                ring_off.append(ring_off[-1])
            continue
        ring, k = odd[r] if isinstance(r, str) else (r, mapgeom.POLYGON)
        kind.append(k)
        part_off.append(part_off[-1] + 1)
        ring_off.append(ring_off[-1] + 1)
        co.append(co[-1] + len(ring))
        pts.extend(ring)
    attrs = {"height": np.array(cols.pop("height", [np.nan] * n), np.float32),
             "num_floors": np.array(cols.pop("num_floors", [np.nan] * n), np.float32),
             "roof_height": np.full(n, np.nan, np.float32)}
    for k in ("class", "roof_shape", "roof_material", "facade_material", "height_src"):
        attrs[k] = mapgeom.text_column(cols.pop(k, [""] * n))
    if "has_parts" in cols:
        attrs["has_parts"] = np.array(cols.pop("has_parts"), bool)
    return mapgeom.Features(layer="buildings", ids=mapgeom.text_column([f"b{i}" for i in range(n)]),
                            kind=np.array(kind, np.uint8), part_off=np.array(part_off, np.int64),
                            ring_off=np.array(ring_off, np.int64), coord_off=np.array(co, np.int64),
                            lonlat=np.array(pts, np.float64).reshape(-1, 2), attrs=attrs)


def centre(ring, fx=0.5, fy=0.5):
    xs, ys = [p[0] for p in ring], [p[1] for p in ring]
    return min(xs) + fx * (max(xs) - min(xs)), min(ys) + fy * (max(ys) - min(ys))


def area_of(ring):
    """The ring's area in m2 as the join measures it."""
    _pts, area = fast._ef.bdtopo_points(np.array([0, len(ring)], np.int64), np.array(ring, np.float64))
    return float(area[0])


def rect(m2, lon=LON, lat=LAT):
    """A rectangle ring of about m2 square metres (area_of gives it exactly)."""
    side = m2 ** 0.5
    return square(lon, lat, side / (111320.0 * math.cos(math.radians(lat))), side / 111320.0)


def rect_at(m2, at):
    """A rectangle ring of about m2 square metres centred on at (lon, lat)."""
    side = m2 ** 0.5
    w, d = side / (111320.0 * math.cos(math.radians(at[1]))), side / 111320.0
    return square(at[0] - w / 2, at[1] - d / 2, w, d)


def rec(at, h, area=100.0, floors=0, tower=False):
    """One record: at is a ring (its middle) or a (lon, lat); area in m2."""
    lon, lat = centre(at) if isinstance(at[0], list) else at
    return {"lon": lon, "lat": lat, "area": area, "h": h, "floors": floors, "tower": tower}


def records(*rs):
    return hkbuildings.Records({k: [r[k] for r in rs] for k in hkbuildings.FIELDS_NPZ})


def float32_at_or_above(x):
    f = np.float32(x)
    return np.nextafter(f, np.float32(np.inf)) if float(f) < x else f


def float32_below(x):
    f = np.float32(x)
    return np.nextafter(f, np.float32(-np.inf)) if float(f) >= x else f


class CoversTest(unittest.TestCase):
    def test_hong_kong_and_not_its_neighbours(self):
        for la, lo in (LOHAS, (22.2815, 114.1585), (22.3880, 114.1950), (22.3080, 113.9185), (22.3800, 114.2700)):
            self.assertTrue(hkbuildings.covers(mapcells.cell_of(la, lo).bounds()), (la, lo))
        for la, lo in ((22.1900, 113.5400), (23.1291, 113.2644), (25.0330, 121.5654), (43.5297, 5.4474)):
            self.assertFalse(hkbuildings.covers(mapcells.cell_of(la, lo).bounds()), (la, lo))


class ParseTest(unittest.TestCase):
    def test_levels_become_heights_above_the_row_itself(self):
        r = hkbuildings.parse([row(square(LON, LAT), base=3.0, top=103.0, storeys=30),
                               row(square(LON + 0.001, LAT), "Podium", base=3.0, top=15.5)])
        self.assertEqual(r["h"].tolist(), [100.0, 12.5])
        self.assertEqual(r["floors"].tolist(), [30, 0])
        self.assertEqual(r["tower"].tolist(), [True, False])
        self.assertTrue(np.allclose(r["lon"][0], LON + 0.00005) and np.allclose(r["lat"][0], LAT + 0.00005))

    def test_a_tower_inside_its_podiums_outline_takes_the_podiums_base(self):
        r = hkbuildings.parse([row(square(LON, LAT, 0.0006, 0.0004), "Podium", base=5.0, top=40.0),
                               row(square(LON + 0.00045, LAT + 0.0001), base=40.0, top=200.0)])
        self.assertEqual(r["h"].tolist(), [35.0, 195.0])

    def test_a_tower_round_the_podiums_point_takes_the_podiums_base(self):
        r = hkbuildings.parse([row(square(LON, LAT, 0.0006, 0.0004), base=40.0, top=200.0),
                               row(square(LON + 0.00045, LAT + 0.0001), "Podium", base=5.0, top=40.0)])
        self.assertEqual(r["h"].tolist(), [195.0, 35.0])

    def test_the_ground_is_not_chained_through_the_rows(self):
        """A stands on B (base 20), B's point is in C (base 5), C and A do not meet: A's ground is B's."""
        rows = [row(square(LON, LAT, 0.0008, 0.0004), "Podium", base=20.0, top=60.0),
                row(square(LON + 0.00005, LAT + 0.0001), base=40.0, top=140.0),
                row(square(LON + 0.00035, LAT + 0.00015), "Podium", base=5.0, top=30.0)]
        self.assertEqual(hkbuildings.parse(rows)["h"].tolist(), [55.0, 120.0, 25.0])
        self.assertEqual(hkbuildings.parse(rows[::-1])["h"].tolist(), [25.0, 120.0, 55.0])      # (whatever the order)

    def test_a_row_without_a_top_gives_no_record_but_lends_its_base(self):
        r = hkbuildings.parse([row(square(LON, LAT, 0.0006, 0.0004), "Podium", base=5.0),
                               row(square(LON + 0.00045, LAT + 0.0001), base=40.0, top=100.0)])
        self.assertEqual(r["h"].tolist(), [95.0])

    def test_low_rows_and_rows_without_levels_are_dropped(self):
        r = hkbuildings.parse([row(square(LON, LAT), base=10.0, top=12.4),                    # 2.4 m
                               row(square(LON + 0.001, LAT), base=10.0, top=12.5),            # 2.5 m
                               row(square(LON + 0.002, LAT), top=50.0),                       # no ground
                               row(square(LON + 0.003, LAT), base=1.0),                       # no top
                               row(square(LON + 0.004, LAT), base=None, top=None, storeys=9)])
        self.assertEqual(r["h"].tolist(), [2.5])

    def test_broken_rows_are_skipped(self):
        r = hkbuildings.parse([row([[LON, LAT], [LON, LAT]], base=0.0, top=50.0),
                               row([[LON, LAT], [LON + 1e-4, float("nan")], [LON, LAT + 1e-4], [LON, LAT]],
                                   base=0.0, top=50.0),
                               {"attributes": {"BaseHeight": 0.0, "TopHeight": 50.0}, "geometry": None},
                               {"attributes": {}}, "junk", None,
                               row(square(LON, LAT), base=0.0, top=50.0, storeys=-3)])
        self.assertEqual((r["h"].tolist(), r["floors"].tolist()), ([50.0], [0]))
        self.assertEqual(len(hkbuildings.parse([])["lon"]), 0)

    def test_the_first_ring_is_the_outline(self):
        r = hkbuildings.parse([{"attributes": {"BaseHeight": 0.0, "TopHeight": 50.0},
                                "geometry": {"rings": [square(LON, LAT), square(LON + 0.01, LAT)]}}])
        self.assertTrue(np.allclose(r["lon"], LON + 0.00005))


class JoinTest(unittest.TestCase):
    def setUp(self):
        self.recs = hkbuildings.Records(hkbuildings.parse([
            # one footprint over a podium (8 storeys) and two towers on it: the highest tower wins
            row(square(LON, LAT, 0.0006, 0.0004), "Podium", base=5.0, top=45.0, storeys=8),
            row(square(LON + 0.00015, LAT + 0.0001), base=45.0, top=145.0, storeys=30),
            row(square(LON + 0.00045, LAT + 0.0001), base=45.0, top=205.0, storeys=50),
            # a low tower and a taller podium in one footprint: the tower
            row(square(LON + 0.002, LAT, 0.0004, 0.0004), base=0.0, top=30.0, storeys=9),
            row(square(LON + 0.0022, LAT + 0.0002, 0.0001, 0.0001), "Podium", base=0.0, top=90.0, storeys=2),
            # a podium alone, 6 m: no tower there
            row(square(LON + 0.004, LAT), "Podium", base=1.0, top=7.0),
            # a 14 m building
            row(square(LON + 0.006, LAT), "Podium", base=1.0, top=15.0),
        ]))
        # (the first footprint: the towers' end of the podium, under BIG_M2; podium_foot: the whole podium, over it)
        self.feet = [square(LON + 0.0001, LAT + 0.00005, 0.0005, 0.0003), square(LON + 0.002, LAT, 0.0004, 0.0004),
                     square(LON + 0.004, LAT), square(LON + 0.006, LAT), square(LON + 0.01, LAT)]
        self.podium_foot = square(LON, LAT, 0.0006, 0.0004)
        self.assertLess(area_of(self.feet[0]), hkbuildings.BIG_M2)
        self.assertGreater(area_of(self.podium_foot), hkbuildings.BIG_M2)

    def test_the_highest_tower_wins_then_any_row_where_there_is_no_tower(self):
        a = hkbuildings.join(layer(self.feet), self.recs)
        self.assertEqual(a["height"][:4].tolist(), [200.0, 30.0, 6.0, 14.0])
        np.testing.assert_array_equal(a["num_floors"][:4], [50.0, 9.0, np.nan, np.nan])
        self.assertEqual(list(a["height_src"][:4]), ["hk_landsd"] * 4)
        self.assertTrue(np.isnan(a["height"][4]) and str(a["height_src"][4]) == "")     # (no record: as it was)

    def test_a_footprint_over_the_whole_podium_takes_the_podium_not_a_tower(self):
        a = hkbuildings.join(layer([self.podium_foot]), self.recs)
        self.assertEqual((float(a["height"][0]), float(a["num_floors"][0])), (40.0, 8.0))
        self.assertEqual((str(a["height_src"][0]), str(a["roof_shape"][0])), ("hk_landsd", "flat"))

    def test_the_maps_storeys_stay_while_they_suit_the_height_and_the_records_fill_the_gaps(self):
        a = hkbuildings.join(layer(self.feet[:3], num_floors=[np.nan, 8, np.nan]), self.recs)
        self.assertEqual(a["num_floors"][0], 50.0)
        self.assertEqual(a["num_floors"][1], 8.0)           # (30 m over 8 storeys: 3.75 m a storey)
        self.assertTrue(np.isnan(a["num_floors"][2]))       # (the record has none)

    def test_the_maps_own_height_stays_ml_heights_give_way(self):
        def one(height, src):       # (a layer each: three copies of one footprint would contest its record)
            return hkbuildings.join(layer([self.feet[0]], height=[height], height_src=[src]), self.recs)
        self.assertIsNone(one(88.0, "OpenStreetMap"))       # (the map's own: nothing written, roof shape neither)
        for height, src in ((30.0, "Microsoft ML Buildings"), (np.nan, "")):
            a = one(height, src)
            self.assertEqual(float(a["height"][0]), 200.0)
            self.assertEqual(str(a["height_src"][0]), "hk_landsd")
            self.assertEqual(str(a["roof_shape"][0]), "flat")       # (only where it filled the height)

    def test_a_filled_in_tower_gets_a_flat_roof_unless_the_map_names_one(self):
        f = layer(self.feet[:4], roof_shape=["gabled", "", "", ""])
        a = hkbuildings.join(f, self.recs)
        # (height 200 with a mapped gabled roof: kept; 30 m: flat; 6 m: below the bound, left to the plan; 14 m: flat)
        self.assertEqual([str(s) for s in a["roof_shape"]], ["gabled", "flat", "", "flat"])

    def test_nothing_else_is_written_and_the_layer_is_not_changed(self):
        f = layer(self.feet)
        before = dict(f.attrs)
        a = hkbuildings.join(f, self.recs)
        self.assertIsNot(a, f.attrs)
        self.assertEqual(set(a), set(before))
        for k in ("roof_height", "class", "roof_material", "facade_material"):
            self.assertIs(a[k], before[k], k)
        self.assertTrue(np.isnan(f.attrs["height"]).all() and (f.attrs["height_src"] == "").all())

    def test_no_match_no_new_attrs(self):
        self.assertIsNone(hkbuildings.join(layer([square(LON + 0.5, LAT)]), self.recs))
        self.assertIsNone(hkbuildings.join(layer([self.feet[0]]), hkbuildings.EMPTY))


class JoinPickTest(unittest.TestCase):
    """The record a footprint takes: the highest tower, but a footprint over BIG_M2 only one that covers COVER of it."""
    BIGF = rect(3000)

    def heights(self, rings, *rs, **cols):
        a = hkbuildings.join(layer(rings, **cols), records(*rs))
        return None if a is None else a["height"]

    def big(self, *rs):
        """The height a 3 000 m2 footprint gets from records standing in it (NaN: none)."""
        return self.heights([self.BIGF, rect(400, LON + 0.01)], *rs, rec(rect(400, LON + 0.01), 20.0))[0]

    def test_up_to_big_m2_the_highest_tower_wins_whatever_its_size(self):
        f = rect(1990)
        self.assertLess(area_of(f), 2000.0)
        h = self.heights([f], rec(centre(f, 0.2), 30.0, area=30.0, tower=True), rec(centre(f, 0.8), 90.0, area=1900.0))
        self.assertEqual(h.tolist(), [30.0])

    def test_just_over_big_m2_a_tower_covering_little_gives_way_to_the_record_that_covers(self):
        f = rect(2010)
        self.assertGreater(area_of(f), 2000.0)
        h = self.heights([f], rec(centre(f, 0.2), 30.0, area=30.0, tower=True), rec(centre(f, 0.8), 90.0, area=1900.0))
        self.assertEqual(h.tolist(), [90.0])

    def test_a_footprint_of_exactly_big_m2_is_still_small(self):
        f = rect(1990)
        rs = (rec(centre(f, 0.2), 30.0, area=30.0, tower=True), rec(centre(f, 0.8), 90.0, area=1900.0))
        with mock.patch.object(hkbuildings, "BIG_M2", area_of(f)):
            self.assertEqual(self.heights([f], *rs).tolist(), [30.0])
        with mock.patch.object(hkbuildings, "BIG_M2", float(np.nextafter(area_of(f), 0.0))):
            self.assertEqual(self.heights([f], *rs).tolist(), [90.0])

    def test_a_tower_covering_just_under_half_of_a_big_footprint_gives_way(self):
        a = area_of(self.BIGF)
        h = self.big(rec(centre(self.BIGF, 0.2), 150.0, area=float32_below(0.5 * a), tower=True),
                     rec(centre(self.BIGF, 0.8), 40.0, area=0.9 * a))
        self.assertEqual(h, 40.0)

    def test_a_tower_covering_half_of_a_big_footprint_is_taken(self):
        a = area_of(self.BIGF)
        h = self.big(rec(centre(self.BIGF, 0.2), 150.0, area=float32_at_or_above(0.5 * a), tower=True),
                     rec(centre(self.BIGF, 0.8), 40.0, area=0.9 * a))
        self.assertEqual(h, 150.0)

    def exact_cover(self):
        """(a record area r, a cover): r is exactly that cover's share of the footprint."""
        a = area_of(self.BIGF)
        r = np.float32(1000.0)
        while (float(r) / a) * a != float(r):
            r = np.nextafter(r, np.float32(np.inf))
        return r, float(r) / a

    def test_the_towers_cover_is_compared_exactly(self):
        r, cover = self.exact_cover()
        with mock.patch.object(hkbuildings, "COVER", cover):
            rs = (rec(centre(self.BIGF, 0.2), 150.0, area=r, tower=True),
                  rec(centre(self.BIGF, 0.8), 40.0, area=2000.0))
            self.assertEqual(self.big(*rs), 150.0)
            rs = (rec(centre(self.BIGF, 0.2), 150.0, area=np.nextafter(r, np.float32(0.0)), tower=True), rs[1])
            self.assertEqual(self.big(*rs), 40.0)

    def test_the_fallbacks_cover_is_compared_exactly(self):
        r, cover = self.exact_cover()
        tower = rec(centre(self.BIGF, 0.2), 150.0, area=10.0, tower=True)
        with mock.patch.object(hkbuildings, "COVER", cover):
            self.assertEqual(self.big(tower, rec(centre(self.BIGF, 0.8), 40.0, area=r)), 40.0)
            self.assertTrue(np.isnan(self.big(tower, rec(centre(self.BIGF, 0.8), 40.0, area=np.nextafter(r,
                np.float32(0.0))))))

    def test_the_largest_record_stands_in_for_a_small_tower_not_the_tallest_or_a_tower(self):
        a = area_of(self.BIGF)
        h = self.big(rec(centre(self.BIGF, 0.1), 150.0, area=0.1 * a, tower=True),
                     rec(centre(self.BIGF, 0.5), 60.0, area=0.6 * a, tower=True),
                     rec(centre(self.BIGF, 0.9), 40.0, area=0.9 * a))
        self.assertEqual(h, 40.0)

    def test_when_the_largest_record_covers_too_little_nothing_is_written(self):
        a = area_of(self.BIGF)
        h = self.big(rec(centre(self.BIGF, 0.2), 150.0, area=0.1 * a, tower=True),
                     rec(centre(self.BIGF, 0.8), 40.0, area=float32_below(0.5 * a)))
        self.assertTrue(np.isnan(h))

    def test_a_tower_alone_covering_too_little_is_not_its_own_fallback(self):
        a = area_of(self.BIGF)
        self.assertTrue(np.isnan(self.big(rec(centre(self.BIGF), 150.0, area=0.1 * a, tower=True))))

    def test_a_big_footprint_without_a_record_standing_in_it_is_left_whatever_is_near(self):
        a = area_of(self.BIGF)
        out = (BIGF_X, BIGF_Y) = centre(self.BIGF, 1.2, 0.5)
        self.assertTrue(np.isnan(self.big(rec(out, 150.0, area=2 * a, tower=True))))
        self.assertTrue(np.isnan(self.big()))

    def test_a_record_without_area_never_covers_a_big_footprint_but_does_a_small_one(self):
        self.assertTrue(np.isnan(self.big(rec(centre(self.BIGF), 150.0, area=0.0, tower=True))))
        f = rect(400)
        self.assertEqual(self.heights([f], rec(centre(f), 50.0, area=0.0)).tolist(), [50.0])

    def test_the_footprint_written_and_the_one_left_do_not_disturb_each_other(self):
        a = area_of(self.BIGF)
        small = rect(400, LON + 0.01)
        h = self.heights([self.BIGF, small], rec(centre(self.BIGF), 150.0, area=0.1 * a, tower=True),
                         rec(centre(small), 20.0))
        np.testing.assert_array_equal(h, [np.nan, 20.0])


class JoinClaimTest(unittest.TestCase):
    """A record goes to one footprint: of those that pick it, the one closest in area to it (the map has a podium and
    the tower on it as two footprints, and the tower's record stands in both)."""
    P = rect(1500)                      # (a podium)
    T = rect_at(300, centre(P))         # (the tower on it)
    TOWER = rec(T, 150.0, area=300.0, tower=True)
    PODIUM = rec(centre(P, 0.1, 0.1), 20.0, area=1500.0)

    def heights(self, rings, *rs, **cols):
        return hkbuildings.join(layer(rings, **cols), records(*rs))["height"]

    def chain(self, n):
        """n concentric footprints (the biggest first) and a record in each, taller towards the middle, of the area of
        its own footprint: footprint k stands over the records k to n - 1 and its own is the closest in area."""
        mid = centre(self.P)
        rings = [rect_at(1800 * 0.7 ** k, mid) for k in range(n)]
        rs = [rec(centre(r, 0.5, 0.04) if k < n - 1 else mid, 20.0 + 10 * k, area=area_of(r), tower=True)
              for k, r in enumerate(rings)]
        return self.heights(rings, *rs)

    def test_the_tower_footprint_takes_the_tower_and_the_podium_its_own(self):
        self.assertLess(area_of(self.P), hkbuildings.BIG_M2)
        np.testing.assert_array_equal(self.heights([self.P, self.T], self.TOWER, self.PODIUM), [20.0, 150.0])
        np.testing.assert_array_equal(self.heights([self.T, self.P], self.TOWER, self.PODIUM), [150.0, 20.0])

    def test_a_podium_alone_keeps_the_tower_as_it_did(self):
        np.testing.assert_array_equal(self.heights([self.P], self.TOWER, self.PODIUM), [150.0])

    def test_the_closest_in_area_wins_not_the_smallest(self):
        shed = rect_at(50, centre(self.T))
        for rings, want in (([shed, self.T], [np.nan, 150.0]), ([self.T, shed], [150.0, np.nan])):
            np.testing.assert_array_equal(self.heights(rings, self.TOWER), want)

    def test_the_closest_in_area_is_the_closest_in_ratio(self):
        mid = centre(self.P)
        low, high = rect_at(320, mid), rect_at(1520, mid)      # (the record's 800: 2.5 times under, 1.9 times over)
        r = rec(mid, 100.0, area=800.0, tower=True)
        np.testing.assert_array_equal(self.heights([low, high], r), [np.nan, 100.0])
        np.testing.assert_array_equal(self.heights([high, low], r), [100.0, np.nan])

    def test_a_record_without_area_goes_to_the_smallest_footprint(self):
        r = rec(centre(self.T), 150.0, area=0.0, tower=True)
        np.testing.assert_array_equal(self.heights([self.P, self.T], r), [np.nan, 150.0])

    def test_a_loser_takes_the_other_record_in_it(self):
        shed = rect_at(50, centre(self.T))
        h = self.heights([self.T, shed], self.TOWER, rec(centre(shed, 0.2, 0.2), 8.0, area=50.0))
        np.testing.assert_array_equal(h, [150.0, 8.0])

    def test_a_loser_with_no_other_record_is_left_as_it_was(self):
        shed = rect_at(50, centre(self.T))
        a = hkbuildings.join(layer([self.T, shed], height=[np.nan, 30.0],
                                   height_src=["", "Microsoft ML Buildings"]), records(self.TOWER))
        self.assertEqual(a["height"].tolist(), [150.0, 30.0])
        self.assertEqual([str(s) for s in a["height_src"]], ["hk_landsd", "Microsoft ML Buildings"])
        self.assertEqual(str(a["roof_shape"][1]), "")

    def test_a_footprint_with_parts_claims_its_record_though_nothing_is_written_on_it(self):
        a = hkbuildings.join(layer([self.P, self.T], has_parts=[False, True]), records(self.TOWER, self.PODIUM))
        np.testing.assert_array_equal(a["height"], [20.0, np.nan])
        self.assertEqual([str(s) for s in a["height_src"]], ["hk_landsd", ""])

    def test_a_footprint_with_a_height_of_the_maps_own_claims_its_record_too(self):
        a = hkbuildings.join(layer([self.P, self.T], height=[np.nan, 88.0], height_src=["", "OpenStreetMap"]),
                             records(self.TOWER, self.PODIUM))
        np.testing.assert_array_equal(a["height"], [20.0, 88.0])
        self.assertEqual([str(s) for s in a["height_src"]], ["hk_landsd", "OpenStreetMap"])

    def test_equal_areas_go_to_the_lower_index_whatever_the_record_order(self):
        f = rect(400)
        tall, low = rec(centre(f), 100.0, area=400.0, tower=True), rec(centre(f), 50.0, area=400.0)
        np.testing.assert_array_equal(self.heights([f, f], tall, low), [100.0, 50.0])
        np.testing.assert_array_equal(self.heights([f, f], low, tall), [100.0, 50.0])

    def test_several_contested_records_in_one_layer_resolve_each(self):
        p2 = rect(1500, LON + 0.01)
        t2 = rect_at(300, centre(p2))
        rs = (self.TOWER, self.PODIUM, rec(t2, 150.0, area=300.0, tower=True),
              rec(centre(p2, 0.1, 0.1), 20.0, area=1500.0))
        np.testing.assert_array_equal(self.heights([self.P, p2, self.T, t2], *rs), [20.0, 20.0, 150.0, 150.0])

    def test_three_nested_footprints_resolve_over_rounds(self):
        np.testing.assert_array_equal(self.chain(3), [20.0, 30.0, 40.0])

    def test_up_to_claim_rounds_picks_resolve_and_past_them_the_picks_stand(self):
        self.assertEqual(hkbuildings.CLAIM_ROUNDS, 8)
        np.testing.assert_array_equal(self.chain(8), [20.0 + 10 * k for k in range(8)])
        np.testing.assert_array_equal(self.chain(9), [30.0, 30.0] + [20.0 + 10 * k for k in range(2, 9)])

    def test_with_one_pick_the_contested_picks_stand(self):
        with mock.patch.object(hkbuildings, "CLAIM_ROUNDS", 1):
            np.testing.assert_array_equal(self.heights([self.P, self.T], self.TOWER, self.PODIUM), [150.0, 150.0])
        with mock.patch.object(hkbuildings, "CLAIM_ROUNDS", 2):
            np.testing.assert_array_equal(self.chain(3), [30.0, 30.0, 40.0])

    def test_a_footprint_over_big_m2_takes_the_cover_test_again_when_it_picks_again(self):
        big = rect(3000)
        inner = rect_at(2700, centre(big))
        self.assertGreater(area_of(inner), hkbuildings.BIG_M2)
        whole = rec(centre(big), 200.0, area=2700.0, tower=True)    # (stands in both, covers both)
        tower = rec(centre(big, 0.5, 0.01), 150.0, area=100.0, tower=True)     # (covers little of the big one)
        podium = rec(centre(big, 0.5, 0.99), 40.0, area=2500.0)
        np.testing.assert_array_equal(self.heights([big, inner], whole, tower, podium), [40.0, 200.0])


class JoinPartsTest(unittest.TestCase):
    """A footprint the map marks has_parts is left to its parts."""
    F = [rect(400), rect(400, LON + 0.002)]
    R = [rec(F[0], 50.0), rec(F[1], 60.0)]

    def test_a_footprint_with_parts_is_never_written(self):
        a = hkbuildings.join(layer(self.F, has_parts=[True, False]), records(*self.R))
        np.testing.assert_array_equal(a["height"], [np.nan, 60.0])
        self.assertEqual(list(a["height_src"]), ["", "hk_landsd"])
        self.assertEqual(list(a["roof_shape"]), ["", "flat"])
        self.assertEqual(a["has_parts"].tolist(), [True, False])

    def test_the_other_way_round(self):
        a = hkbuildings.join(layer(self.F, has_parts=[False, True]), records(*self.R))
        np.testing.assert_array_equal(a["height"], [50.0, np.nan])

    def test_a_layer_where_every_footprint_has_parts_gets_nothing(self):
        self.assertIsNone(hkbuildings.join(layer(self.F, has_parts=[True, True]), records(*self.R)))

    def test_no_has_parts_column_is_no_footprint_with_parts(self):
        f = layer(self.F + [rect(400, LON + 0.004)])
        self.assertNotIn("has_parts", f.attrs)
        a = hkbuildings.join(f, records(*self.R))
        np.testing.assert_array_equal(a["height"], [50.0, 60.0, np.nan])
        self.assertNotIn("has_parts", a)

    def test_all_false_is_the_same_as_no_column(self):
        a = hkbuildings.join(layer(self.F, has_parts=[False, False]), records(*self.R))
        np.testing.assert_array_equal(a["height"], [50.0, 60.0])


class JoinStoreysTest(unittest.TestCase):
    """The record's height against the map's storeys, in buildings.ML_FLOOR_M's band (2.2 to 6.0 m a storey)."""
    F, G = rect(400), rect(400, LON + 0.002)
    CARRIER = rec(G, 20.0)          # (a second footprint that always gets its height: the join is not None)

    def go(self, h, rec_floors=0, map_floors=np.nan, **cols):
        """(height, storeys) the join writes on a footprint of one record, or None when it writes nothing there."""
        cols.setdefault("num_floors", [map_floors, np.nan])
        a = hkbuildings.join(layer([self.F, self.G], **cols), records(rec(self.F, h, floors=rec_floors), self.CARRIER))
        if str(a["height_src"][0]) != "hk_landsd":
            return None
        return float(a["height"][0]), float(a["num_floors"][0])

    def same(self, got, want):
        self.assertTrue(got is not None and np.allclose(got, want, equal_nan=True), (got, want))

    def test_a_record_under_the_maps_storeys_band_is_another_buildings_and_not_written(self):
        self.assertIsNone(self.go(20.0, 6, map_floors=10))
        self.assertIsNone(self.go(200.0, 30, map_floors=100))

    def test_a_record_in_the_band_is_written_and_the_maps_storeys_stay(self):
        self.same(self.go(30.0, 8, map_floors=10), (30.0, 10.0))
        self.same(self.go(30.0, 0, map_floors=10), (30.0, 10.0))

    def test_the_bands_edges_are_inside_it(self):
        with mock.patch.object(bld, "ML_FLOOR_M", (2.0, 4.0)):
            self.same(self.go(20.0, 0, map_floors=10), (20.0, 10.0))        # (2.0 m a storey)
            self.same(self.go(20.0, 0, map_floors=5), (20.0, 5.0))          # (4.0)
            self.assertIsNone(self.go(19.5, 0, map_floors=10))              # (1.95)
            # (4.1: the map's storeys go for the record's own, 2.05 a storey)
            self.same(self.go(20.5, 10, map_floors=5), (20.5, 10.0))
            self.same(self.go(20.0, 10, map_floors=1), (20.0, 10.0))        # (the record's own: 2.0)
            self.same(self.go(20.0, 5, map_floors=1), (20.0, 5.0))          # (4.0)
            self.same(self.go(20.0, 11, map_floors=1), (20.0, np.nan))      # (1.8)
            self.same(self.go(20.0, 4, map_floors=1), (20.0, np.nan))       # (5.0)

    def test_a_record_over_the_band_is_written_and_replaces_the_maps_storeys_with_its_own_when_they_suit(self):
        self.same(self.go(120.0, 30, map_floors=1), (120.0, 30.0))          # (a tower mapped with one storey)
        self.same(self.go(120.0, 30, map_floors=3), (120.0, 30.0))

    def test_a_record_over_the_band_whose_own_storeys_do_not_suit_leaves_no_storeys(self):
        self.same(self.go(120.0, 5, map_floors=1), (120.0, np.nan))         # (24 m a storey)
        self.same(self.go(120.0, 100, map_floors=1), (120.0, np.nan))       # (1.2)
        self.same(self.go(120.0, 0, map_floors=1), (120.0, np.nan))         # (none)

    def test_where_the_map_has_no_storeys_the_records_are_written_when_they_suit(self):
        self.same(self.go(60.0, 15), (60.0, 15.0))
        self.same(self.go(60.0, 5), (60.0, np.nan))                         # (12 m a storey)
        self.same(self.go(60.0, 60), (60.0, np.nan))                        # (1)
        self.same(self.go(60.0, 0), (60.0, np.nan))

    def test_storeys_are_only_written_on_footprints_whose_height_the_join_wrote(self):
        a = hkbuildings.join(layer([self.F, self.G, rect(400, LON + 0.004), rect(400, LON + 0.006)],
                                   height=[88.0, 88.0, 30.0, np.nan], num_floors=[1, np.nan, 10, np.nan],
                                   height_src=["OpenStreetMap", "OpenStreetMap", "Microsoft ML Buildings", ""]),
                             records(rec(self.F, 120.0, floors=30), rec(self.G, 120.0, floors=30),
                                     rec(rect(400, LON + 0.004), 20.0, floors=6), rec(rect(400, LON + 0.006), 60.0,
                                     floors=15)))
        np.testing.assert_array_equal(a["height"], [88.0, 88.0, 30.0, 60.0])
        np.testing.assert_array_equal(a["num_floors"], [1.0, np.nan, 10.0, 15.0])
        self.assertEqual([str(s) for s in a["height_src"]], ["OpenStreetMap", "OpenStreetMap",
            "Microsoft ML Buildings", "hk_landsd"])

    def test_a_machine_learned_height_gives_way_to_a_record_in_the_band_and_not_to_another_buildings(self):
        src = ["Microsoft ML Buildings"] * 2
        a = hkbuildings.join(layer([self.F, self.G], height=[30.0, 30.0], num_floors=[10, 6], height_src=src),
                             records(rec(self.F, 20.0), rec(self.G, 20.0)))
        self.assertEqual([str(s) for s in a["height_src"]], src[:1] + ["hk_landsd"])
        self.assertEqual(a["height"].tolist(), [30.0, 20.0])
        self.assertEqual(a["num_floors"].tolist(), [10.0, 6.0])


class JoinRoofTest(unittest.TestCase):
    """A flat roof only where the join wrote a height of FLAT_MIN_H_M or more and the map names no roof shape."""
    F = [rect(400), rect(400, LON + 0.002), rect(400, LON + 0.004)]

    def test_the_flat_roof_starts_at_flat_min_h_m(self):
        self.assertEqual(hkbuildings.FLAT_MIN_H_M, 12.0)
        a = hkbuildings.join(layer(self.F), records(rec(self.F[0], 12.0), rec(self.F[1], 11.9), rec(self.F[2], 12.1)))
        self.assertEqual(list(a["roof_shape"]), ["flat", "", "flat"])

    def test_no_roof_is_written_where_the_height_is_not(self):
        big = rect(3000, LON + 0.006)
        a = hkbuildings.join(layer(self.F + [big, rect(400, LON + 0.01)], has_parts=[True, False, False, False, False],
                                   num_floors=[np.nan, 40, np.nan, np.nan, np.nan], height=[np.nan] * 4 + [20.0],
                                   height_src=[""] * 4 + ["OpenStreetMap"]),
                             records(rec(self.F[0], 40.0), rec(self.F[1], 40.0), rec(self.F[2], 40.0),
                                     rec(big, 90.0, area=100.0, tower=True), rec(rect(400, LON + 0.01), 90.0)))
        self.assertEqual(a["height"].tolist()[2], 40.0)
        self.assertEqual(list(a["roof_shape"]), ["", "", "flat", "", ""])


class JoinNothingTest(unittest.TestCase):
    """None, not a copy of the attrs, when no footprint gets a height."""
    F = [rect(400), rect(400, LON + 0.002)]
    R = [rec(F[0], 50.0), rec(F[1], 60.0)]

    def test_the_cases_where_no_footprint_gets_a_height(self):
        cases = {
            "no records": (layer(self.F), hkbuildings.EMPTY),
            "no features": (layer([]), records(*self.R)),
            "records elsewhere": (layer(self.F), records(rec((LON + 0.5, LAT), 50.0))),
            "no polygons": (layer(["line", "point"]), records(*self.R, rec((LON + 0.004, LAT), 50.0))),
            "all with parts": (layer(self.F, has_parts=[True, True]), records(*self.R)),
            "the map's own heights": (layer(self.F, height=[10.0, 11.0], height_src=["OpenStreetMap", "Overture"]),
            records(*self.R)),
            "all other buildings'": (layer(self.F, num_floors=[40, 40]), records(*self.R)),
            "a big footprint nothing covers": (layer([rect(3000)]), records(rec(rect(3000), 50.0, area=100.0,
            tower=True))),
        }
        for name, (f, r) in cases.items():
            self.assertIsNone(hkbuildings.join(f, r), name)


class JoinLayerShapeTest(unittest.TestCase):
    """Features that are not polygons, or have no part, stand among the polygons: the heights land on the right ones."""

    def test_non_polygon_features_between_polygons_shift_nothing(self):
        s1, big, s2 = rect(400), rect(3000, LON + 0.002), rect(400, LON + 0.006)
        a = area_of(big)
        f = layer([s1, "line", big, "point", "bare", s2], height=[np.nan] * 6)
        r = records(rec(s1, 10.0), rec(big, 150.0, area=0.1 * a, tower=True), rec(centre(big, 0.8), 40.0, area=0.9 * a),
                    rec(s2, 20.0), rec(square(LON + 0.004, LAT), 77.0))      # (the last: inside the "line")
        h = hkbuildings.join(f, r)["height"]
        np.testing.assert_array_equal(h, [10.0, np.nan, 40.0, np.nan, np.nan, 20.0])

    def test_a_big_footprint_after_non_polygons_is_still_measured_as_itself(self):
        big = rect(3000, LON + 0.002)
        a = area_of(big)
        for before in ([], ["line"], ["point", "bare"], [rect(100, LON - 0.002), "line", rect(5000, LON - 0.004)]):
            f = layer(before + [big, rect(400, LON + 0.01)])
            r = records(rec(big, 150.0, area=float32_below(0.5 * a), tower=True), rec(centre(big, 0.8), 40.0,
                area=0.9 * a),
                        rec(rect(400, LON + 0.01), 20.0))
            h = hkbuildings.join(f, r)["height"][len(before):]
            np.testing.assert_array_equal(h, [40.0, 20.0], str(before))
            r = records(rec(big, 150.0, area=0.1 * a, tower=True), rec(rect(400, LON + 0.01), 20.0))
            h = hkbuildings.join(f, r)["height"][len(before):]
            np.testing.assert_array_equal(h, [np.nan, 20.0], str(before))


class OddInputTest(unittest.TestCase):
    """A layer or a row out of the ordinary spoils nothing round it."""

    def test_a_polygon_whose_part_has_no_ring_takes_no_neighbours_outline(self):
        b = rect(400, LON + 0.002)
        r = records(rec(b, 70.0))
        np.testing.assert_array_equal(hkbuildings.join(layer(["empty", b]), r)["height"], [np.nan, 70.0])
        a = rect(400)
        got = hkbuildings.join(layer([a, "empty"]), records(rec(a, 50.0)))["height"]
        np.testing.assert_array_equal(got, [50.0, np.nan])

    def test_a_row_with_a_storey_count_over_int16_does_not_break_the_cells_records(self):
        r = hkbuildings.parse([row(square(LON, LAT), base=0.0, top=50.0, storeys=70000),
                               row(square(LON + 0.001, LAT), base=0.0, top=60.0, storeys=20)])
        self.assertEqual(r["h"].tolist(), [50.0, 60.0])
        self.assertEqual(r["floors"].tolist()[1], 20)


class PlanTest(unittest.TestCase):
    """What the join writes is drawn as a measured tower: the flat roof keeps the wall to the measured height."""
    RING = np.array([[0, 0], [15, 0], [15, 10], [0, 10]], np.float64)

    def test_a_flat_roof_keeps_the_measured_height(self):
        def at(**kw):
            a = {"class": "", "height": 120.0, "num_floors": None, "roof_shape": "", "roof_height": None,
                 "height_src": "hk_landsd", "seed_id": "x"}
            a.update(kw)
            return a
        for i in range(40):
            s = bld.plan(self.RING, at(roof_shape="flat", seed_id=f"id{i}"), 0, *LOHAS)
            self.assertEqual((s.roof, s.height), ("flat", 120.0))


class Res:
    def __init__(self, body):
        self.body = body
        self.headers = {}


class FakeFetcher:
    """The layer's pages: answers[k] is the k-th request's features, exceededTransferLimit while more follow."""

    def __init__(self, features=(), delay=0.0, fail=False, pages=None):
        self.pages = pages if pages is not None else [list(features)]
        self.delay, self.fail, self.urls = delay, fail, []

    @property
    def calls(self):
        return len(self.urls)

    def get(self, url, kind="demand", lane=None, headers=None):
        self.urls.append(url)
        time.sleep(self.delay)
        if self.fail:
            raise IOError("offline")
        k = int(params(url).get("resultOffset", 0)) // hkbuildings.hklidar.PAGE
        return Res(json.dumps({"features": self.pages[k], "exceededTransferLimit": k + 1 < len(self.pages)}).encode())


def params(url):
    return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}


TOWER = row(square(LOHAS[1], LOHAS[0]), base=0.0, top=100.0, storeys=30)


class SourceTest(unittest.TestCase):
    key = mapcells.cell_of(*LOHAS)

    def test_outside_hong_kong_nothing_is_asked(self):
        with tempfile.TemporaryDirectory() as d:
            fx = FakeFetcher()
            s = hkbuildings.Source(d, fx)
            far = mapcells.cell_of(*AIX)
            self.assertTrue(s.wait([far]))
            self.assertIs(s.load(far), hkbuildings.EMPTY)
            self.assertEqual(s.prefetch([far]), [])
            self.assertFalse(s.wanted(far))
            self.assertEqual(fx.calls, 0)

    def test_downloaded_once_kept_on_disk_and_credited(self):
        with tempfile.TemporaryDirectory() as d:
            fx = FakeFetcher([TOWER])
            s = hkbuildings.Source(d, fx)
            self.assertTrue(s.wait([self.key]))
            self.assertEqual(fx.calls, 1)
            s2 = hkbuildings.Source(d, FakeFetcher(fail=True))
            self.assertEqual(s2.cached(self.key).n, 1)
            self.assertTrue(hkbuildings.credited_near(d, *LOHAS, 1500.0))
            self.assertFalse(hkbuildings.credited_near(d, 22.2, 114.0, 1500.0))
            with mock.patch.object(hkbuildings, "ON", False):
                self.assertFalse(hkbuildings.credited_near(d, *LOHAS, 1500.0))

    def test_a_cell_without_buildings_is_kept_empty(self):
        with tempfile.TemporaryDirectory() as d:
            fx = FakeFetcher([])
            s = hkbuildings.Source(d, fx)
            self.assertTrue(s.wait([self.key]))
            self.assertTrue(os.path.exists(s.path(self.key)))
            fresh = hkbuildings.Source(d, FakeFetcher(fail=True))
            self.assertEqual(fresh.cached(self.key).n, 0)
            self.assertTrue(fresh.wait([self.key]))
            self.assertFalse(hkbuildings.credited_near(d, *LOHAS, 1500.0))     # (nothing to credit)

    def test_a_failed_download_is_not_kept_and_asked_again_after_the_wait(self):
        with tempfile.TemporaryDirectory() as d:
            fx = FakeFetcher([TOWER], fail=True)
            s = hkbuildings.Source(d, fx)
            self.assertFalse(s.wait([self.key]))
            self.assertFalse(os.path.exists(s.path(self.key)))
            self.assertFalse(s.wait([self.key]))
            self.assertEqual(fx.calls, 1)           # (not asked again at once)
            fx.fail = False
            with mock.patch.object(hkbuildings, "RETRY_S", 0.0):
                self.assertTrue(s.wait([self.key]))
            self.assertEqual(fx.calls, 2)
            self.assertEqual(s.cached(self.key).n, 1)

    def test_the_query_asks_the_padded_box_in_hk1980_and_the_outlines_back_in_wgs84(self):
        with tempfile.TemporaryDirectory() as d:
            fx = FakeFetcher([TOWER])
            self.assertTrue(hkbuildings.Source(d, fx).wait([self.key]))
        q = params(fx.urls[0])
        self.assertTrue(fx.urls[0].startswith(hkbuildings.URL + "?"))
        self.assertEqual((q["inSR"], q["outSR"], q["geometryPrecision"], q["where"], q["orderByFields"], q["f"]),
                         ("2326", "4326", "7", "1=1", "OBJECTID", "json"))
        self.assertEqual(q["geometryType"], "esriGeometryEnvelope")
        for f in ("OBJECTID", "BuildingBlockType", "BaseHeight", "TopHeight", "Storeys"):
            self.assertIn(f, q["outFields"])
        lon0, lat0, lon1, lat1 = self.key.bounds()
        e, n = hkdtm.grid(np.array([lat0, lat0, lat1, lat1]), np.array([lon0, lon1, lon0, lon1]))
        got = [float(v) for v in q["geometry"].split(",")]
        want = [e.min() - 100.0, n.min() - 100.0, e.max() + 100.0, n.max() + 100.0]
        self.assertTrue(np.allclose(got, want, atol=0.2), (got, want))

    def test_every_page_is_read(self):
        with tempfile.TemporaryDirectory() as d:
            two = [row(square(LON + 0.001 * i, LAT), base=0.0, top=50.0 + i) for i in range(3)]
            fx = FakeFetcher(pages=[two[:2], two[2:]])
            s = hkbuildings.Source(d, fx)
            self.assertTrue(s.wait([self.key]))
            self.assertEqual(fx.calls, 2)
            self.assertEqual([params(u)["resultOffset"] for u in fx.urls], ["0", str(hkbuildings.hklidar.PAGE)])
            self.assertEqual(sorted(s.cached(self.key).h.tolist()), [50.0, 51.0, 52.0])

    def test_a_slow_answer_is_waited_for_at_most_the_bound(self):
        with tempfile.TemporaryDirectory() as d:
            s = hkbuildings.Source(d, FakeFetcher([TOWER], delay=1.5))
            with mock.patch.object(hkbuildings, "WAIT_S", 0.2):
                t0 = time.monotonic()
                self.assertFalse(s.wait([self.key]))
                self.assertLess(time.monotonic() - t0, 1.0)
            for t in threading.enumerate():
                if t.name.startswith("hkbuildings"):
                    t.join(5.0)
            self.assertIsNotNone(s.cached(self.key))     # (there for the next build)


class CreditTest(unittest.TestCase):
    def test_credited(self):
        import credits
        text = (Path(__file__).resolve().parents[2] / "CREDITS.md").read_text(encoding="utf-8")
        self.assertIn(f'"Buildings: {credits.full_label("bld:landsd-hk")}"', text)
        self.assertTrue(credits.SHORT["bld:landsd-hk"].isascii())
        self.assertEqual(credits.line({"bld:landsd-hk"}), "Buildings: HK Gov LandsD (CSDI)")


def mapdata_with(d, hk_fetcher, fr_fetcher):
    import mapdata
    md = mapdata.MapData.__new__(mapdata.MapData)
    md.bdtopo = bdtopo.Source(d, fr_fetcher)
    md.hkbuildings = hkbuildings.Source(d, hk_fetcher)
    md.side_sources = []
    return md


class MapDataTest(unittest.TestCase):
    hk, fr = mapcells.cell_of(*LOHAS), mapcells.cell_of(*AIX)

    def test_a_tile_without_its_records_is_a_revisit_fault(self):
        with tempfile.TemporaryDirectory() as d:
            md = mapdata_with(d, FakeFetcher(fail=True), FakeFetcher())
            before = revisit.thread_faults()
            self.assertFalse(md.hkbuildings_wait([self.hk]))
            self.assertEqual(revisit.thread_faults(), before + 1)
            self.assertTrue(md.hkbuildings_wait([self.fr]))
            self.assertEqual(revisit.thread_faults(), before + 1)

    def test_the_records_are_joined_once_and_the_layer_is_swapped(self):
        with tempfile.TemporaryDirectory() as d:
            md = mapdata_with(d, FakeFetcher([TOWER]), FakeFetcher())
            f = layer([square(LON, LAT)])
            old = f.attrs
            md._hkbuildings(self.hk, "buildings", f)            # (the records are not here yet: no marker)
            self.assertIs(f.attrs, old)
            self.assertNotIn("_hkbuildings", f.__dict__)
            self.assertTrue(md.hkbuildings_wait([self.hk]))
            with mock.patch.object(hkbuildings, "join", wraps=hkbuildings.join) as j:
                md._hkbuildings(self.hk, "buildings", f)
                md._hkbuildings(self.hk, "buildings", f)
            self.assertEqual(j.call_count, 1)
            self.assertIsNot(f.attrs, old)
            self.assertEqual((float(f.attrs["height"][0]), str(f.attrs["height_src"][0])), (100.0, "hk_landsd"))
            self.assertEqual(f.__dict__["_hkbuildings"], 1)

    def test_each_countrys_cells_are_its_own_sources_alone(self):
        with tempfile.TemporaryDirectory() as d:
            hk_fx, fr_fx = FakeFetcher([TOWER]), FakeFetcher([])
            md = mapdata_with(d, hk_fx, fr_fx)
            f, g = layer([square(LON, LAT)]), layer([square(LON, LAT)])
            old = f.attrs, g.attrs
            md._hkbuildings(self.fr, "buildings", f)
            md._bdtopo(self.hk, "buildings", g)
            self.assertTrue(f.attrs is old[0] and g.attrs is old[1])
            self.assertEqual((f.__dict__["_hkbuildings"], g.__dict__["_bdtopo"]), (False, False))
            self.assertTrue(md.hkbuildings_wait([self.fr]) and md.bdtopo_wait([self.hk]))
            self.assertEqual((hk_fx.calls, fr_fx.calls), (0, 0))
            self.assertFalse(md.hkbuildings.wanted(self.fr) or md.bdtopo.wanted(self.hk))
            md.side_prefetch([self.hk])
            self.assertTrue(md.hkbuildings.wait([self.hk]))
            self.assertEqual((hk_fx.calls, fr_fx.calls), (1, 0))
            md.side_prefetch([self.fr])
            self.assertTrue(md.bdtopo.wait([self.fr]))
            self.assertEqual((hk_fx.calls, fr_fx.calls), (1, 1))

    def test_no_road_plan_reads_the_records(self):
        """Only the buildings layer is joined, and a switch of its own keys everything it could change (it is not on
        revisit.PLAN_NEUTRAL: tests/test_plan_neutral.py reads MapData.cell as part of the planning, so the plans'
        keys hold it like BNE_BDTOPO)."""
        with tempfile.TemporaryDirectory() as d:
            md = mapdata_with(d, FakeFetcher([TOWER]), FakeFetcher())
            md.hkbuildings.cached = mock.Mock(side_effect=AssertionError("a road layer asked for the records"))
            for ly in ("roads", "connectors", "building_parts", "land_use", "water"):
                f = layer([square(LON, LAT)])
                self.assertIs(md._hkbuildings(self.hk, ly, f), f)
                self.assertNotIn("_hkbuildings", f.__dict__)
        self.assertNotIn("BNE_HK_BUILDINGS", revisit.PLAN_NEUTRAL)
        self.assertTrue(revisit.keyed_env("BNE_HK_BUILDINGS"))
        keys = []
        for v in (None, "off"):
            env = {k: x for k, x in os.environ.items() if k != "BNE_HK_BUILDINGS"}
            if v:
                env["BNE_HK_BUILDINGS"] = v
            with mock.patch.dict(os.environ, env, clear=True):
                revisit._switches.clear()
                keys.append((revisit.switches_key(), revisit.plans_switches_key()))
        revisit._switches.clear()
        self.assertNotEqual(keys[0][0], keys[1][0])
        self.assertNotEqual(keys[0][1], keys[1][1])

    def test_the_building_tiles_wait_for_both_countries(self):
        with tempfile.TemporaryDirectory() as d:
            md = mapdata_with(d, FakeFetcher([TOWER]), FakeFetcher([]))
            bld.measured_wait(md, [self.hk, self.fr])
            self.assertIsNotNone(md.hkbuildings.cached(self.hk))
            self.assertIsNotNone(md.bdtopo.cached(self.fr))


if __name__ == "__main__":
    unittest.main()
