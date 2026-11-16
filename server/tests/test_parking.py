"""parking: curbside spots along town streets, facing the traffic on their side, clear of corners,
hydrants and driveways; the same spots whichever tile draws them."""
import math
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import furniture  # noqa: E402
import parking  # noqa: E402
import roads  # noqa: E402


def street(fid="s", cls="residential", width=10.0, urban=True, oneway=False):
    pts = np.array([(-300.0, 50.0, 0.0), (600.0, 50.0, 0.0)])
    return (fid, roads.Spec(width, "paved", urban, cls, oneway), pts, 0.0)


def walks(r):
    fid, spec, pts, _ = r
    out = []
    for side in (1, -1):
        line = roads.offset_polyline(pts, side * (spec.width / 2 + 1.25))
        out.append({"fid": fid, "pts": line, "side": side, "w": 2.5, "caps": (False, False)})
    return out


def eligible(cls="residential", width=10.0):
    """A street id the STREET_SHARE hash keeps."""
    for k in range(50):
        if furniture._h01(f"s{k}", "park") < parking.STREET_SHARE:
            return street(f"s{k}", cls, width)
    raise AssertionError


class CurbTest(unittest.TestCase):
    def test_both_sides_of_a_wide_street_facing_the_traffic(self):
        r = eligible()
        spots = parking.curb_spots([r], walks(r), (0, 0, 256, 256))
        north = [s for s in spots if s[1] > 50]
        south = [s for s in spots if s[1] < 50]
        self.assertGreater(len(north), 20)
        self.assertGreater(len(south), 20)
        for s in north:     # 1.25 m in from the curb (y 55): westbound traffic on the north side
            self.assertAlmostEqual(s[1], 55.0 - parking.CURB_OFF_M, places=1)
            self.assertAlmostEqual(s[3], 180.0, places=0)
        for s in south:
            self.assertAlmostEqual(s[1], 45.0 + parking.CURB_OFF_M, places=1)
            self.assertAlmostEqual(min(s[3], 360 - s[3]), 0.0, places=0)
        xs = sorted(s[0] for s in north)
        self.assertTrue(np.allclose(np.diff(xs), parking.SPOT_LEN_M, atol=0.05))

    def test_left_hand_traffic_parks_the_other_way(self):
        r = eligible()
        spots = parking.curb_spots([r], walks(r), (0, 0, 256, 256), left_hand=True)
        for s in spots:
            if s[1] > 50:   # north side: eastbound traffic keeps left there
                self.assertAlmostEqual(min(s[3], 360 - s[3]), 0.0, places=0)
            else:
                self.assertAlmostEqual(s[3], 180.0, places=0)

    def test_one_way_against_its_geometry(self):
        for k in range(50):
            if furniture._h01(f"o{k}", "park") < parking.STREET_SHARE:
                break
        r = (f"o{k}", roads.Spec(9.5, "paved", True, "secondary", True), np.array([(-300.0, 50.0, 0.0), (600.0, 50.0, 0.0)]), 0.0)
        along = parking.curb_spots([r], walks(r), (0, 0, 256, 256))
        against = parking.curb_spots([r], walks(r), (0, 0, 256, 256), directions={f"o{k}": -1})
        self.assertTrue(along and all(min(s[3], 360 - s[3]) < 1 for s in along), "eastbound along the geometry")
        self.assertTrue(all(abs(s[3] - 180) < 1 for s in against), "westbound against it")

    def test_same_spots_from_neighbouring_tiles(self):
        r = eligible()
        a = parking.curb_spots([r], walks(r), (0, 0, 128, 256))
        b = parking.curb_spots([r], walks(r), (128, 0, 256, 256))
        whole = parking.curb_spots([r], walks(r), (0, 0, 256, 256))
        self.assertEqual(sorted(map(tuple, a + b)), sorted(map(tuple, whole)))

    def test_no_parking_on_highways_country_roads_or_narrow_lanes(self):
        for r in (street("t", "trunk"), street("c", urban=False), street("n", width=6.0)):
            self.assertEqual(parking.curb_spots([r], walks(r), (0, 0, 256, 256)), [])

    def test_narrow_street_parks_one_side(self):
        r = eligible(width=8.0)
        spots = parking.curb_spots([r], walks(r), (0, 0, 256, 256))
        self.assertTrue(spots)
        self.assertEqual(len({s[1] > 50 for s in spots}), 1)

    def test_clear_of_hydrants_and_driveways(self):
        r = eligible()
        spots = parking.curb_spots([r], walks(r), (0, 0, 256, 256))
        north = sorted((s for s in spots if s[1] > 50), key=lambda s: s[0])
        h = north[5]
        d = north[10]
        props = [[furniture.G_HYDRANT, h[0], 56.0, 0.0, 0.0, 1.0, 0.9, 0.0]]
        drives = [{"n": [[d[0], 70.0, 0.0], [d[0], 55.1, 0.0]]}]
        kept = parking.clear_of(spots, props, drives)
        self.assertNotIn(h, kept)
        self.assertNotIn(d, kept)
        self.assertGreater(len(kept), len(spots) - 6)
        for s in kept:
            self.assertGreaterEqual(math.hypot(s[0] - h[0], s[1] - 56.0), parking.HYDRANT_CLEAR_M)


class LotTest(unittest.TestCase):
    LOT = np.array([(0.0, 0.0), (60.0, 0.0), (60.0, 40.0), (0.0, 40.0)])

    def test_rows_of_stalls_inside_the_lot(self):
        spots = parking.lot_spots([self.LOT], (0, 0, 256, 256), lambda x, y: np.full(len(x), 3.0))
        self.assertGreater(len(spots), 60)
        for x, y, z, h, kind in spots:
            self.assertEqual(kind, 1)
            self.assertEqual(z, 3.0)
            self.assertTrue(1.0 < x < 59.0 and 2.5 < y < 37.5)
            self.assertIn(round(h) % 180, (90,), "stalls across the long side (nose north or south)")
        rows = sorted({round(s[1], 1) for s in spots})
        self.assertEqual(len(rows), len(parking._lot_rows(20.0)))
        xs = sorted(s[0] for s in spots if round(s[1], 1) == rows[0])
        self.assertTrue(np.allclose(np.diff(xs), parking.LOT_SPOT_W_M))

    def test_buildings_block_and_tiles_split(self):
        garage = np.array([(0.0, 0.0), (30.0, 0.0), (30.0, 40.0), (0.0, 40.0)])
        z = lambda x, y: np.zeros(len(x))
        spots = parking.lot_spots([self.LOT], (0, 0, 256, 256), z, [garage])
        self.assertTrue(spots and all(s[0] > 30 for s in spots))
        a = parking.lot_spots([self.LOT], (0, 0, 30, 256), z)
        b = parking.lot_spots([self.LOT], (30, 0, 256, 256), z)
        self.assertEqual(len(a) + len(b), len(parking.lot_spots([self.LOT], (0, 0, 256, 256), z)))


    def test_no_stall_on_a_road_through_the_lot(self):
        # a parking polygon drawn over a road (Montana's I-94 / I-90, 2026-11-05: parked cars across the lanes)
        z = lambda x, y: np.zeros(len(x))
        road = np.array([(-50.0, 20.0), (120.0, 20.0)])
        spots = parking.lot_spots([self.LOT], (0, 0, 256, 256), z, roads_xy=[road])
        self.assertTrue(spots, "the stalls away from it stay")
        self.assertTrue(all(abs(s[1] - 20.0) >= parking.LOT_ROAD_CLEAR_M for s in spots))
        self.assertLess(len(spots), len(parking.lot_spots([self.LOT], (0, 0, 256, 256), z)))


class AisleTest(unittest.TestCase):
    def test_stalls_both_sides_nose_out(self):
        aisle = ("a", roads.Spec(6.0, "paved", True, "service", False), np.array([(0.0, 50.0, 0.0), (80.0, 50.0, 0.0)]), 0.0)
        spots = parking.aisle_spots([aisle], {"a"}, (0, 0, 256, 256))
        north = [s for s in spots if s[1] > 50]
        south = [s for s in spots if s[1] < 50]
        self.assertTrue(len(north) > 20 and len(north) == len(south))
        self.assertTrue(all(abs(s[1] - (53.0 + parking.LOT_SPOT_D_M / 2)) < 0.01 and abs(s[3] - 90) < 0.5 for s in north))
        self.assertTrue(all(parking.AISLE_END_M - 0.01 <= s[0] <= 80 - parking.AISLE_END_M + 0.01 for s in spots))
        # a street crossing it clears its corridor; non-aisles get none
        street = ("s", roads.Spec(8.0, "paved", True, "residential", False), np.array([(40.0, 0.0, 0.0), (40.0, 100.0, 0.0)]), 0.0)
        crossed = parking.aisle_spots([aisle, street], {"a"}, (0, 0, 256, 256))
        self.assertTrue(all(abs(s[0] - 40.0) > 4.0 for s in crossed))
        self.assertEqual(parking.aisle_spots([street], {"a"}, (0, 0, 256, 256)), [])


def _z(*_a, **_k):
    return np.zeros(len(_a[0]))


class MarkTest(unittest.TestCase):
    LOT = np.array([(0.0, 0.0), (60.0, 0.0), (60.0, 40.0), (0.0, 40.0)])

    def _dividers(self, chunks):
        return [c for c in chunks if abs(c["n"][0][0] - c["n"][1][0]) < 0.05]

    def test_stall_lines_follow_the_rows(self):
        chunks = parking.lot_markings([self.LOT], (0, 0, 256, 256), _z)
        self.assertGreater(len(chunks), 40)
        self.assertTrue(all(c["m"] == "line_white" and c["w"] == 0.1 for c in chunks))
        divs = self._dividers(chunks)
        self.assertGreater(len(divs), 20)
        for c in divs:
            self.assertAlmostEqual(abs(c["n"][0][1] - c["n"][1][1]), parking.LOT_SPOT_D_M, delta=0.05)
            self.assertTrue(0.5 < c["n"][0][1] < 39.5 and 0.5 < c["n"][1][1] < 39.5)
        xs = sorted({round(c["n"][0][0], 2) for c in divs})
        self.assertTrue(np.all(np.abs(np.diff(xs) - parking.LOT_SPOT_W_M) < 0.05))
        keys = []
        for c in chunks:
            a = (round(c["n"][0][0], 2), round(c["n"][0][1], 2))
            b = (round(c["n"][1][0], 2), round(c["n"][1][1], 2))
            keys.append(tuple(sorted((a, b))))
        self.assertEqual(len(keys), len(set(keys)), "a shared head line drawn once")

    def test_a_building_takes_its_half(self):
        garage = np.array([(0.0, 0.0), (30.0, 0.0), (30.0, 40.0), (0.0, 40.0)])
        chunks = parking.lot_markings([self.LOT], (0, 0, 256, 256), _z, [garage])
        self.assertTrue(chunks)
        for c in chunks:
            mx = (c["n"][0][0] + c["n"][1][0]) / 2
            self.assertGreater(mx, 29.0)

    def test_neighbouring_tiles_share_no_divider(self):
        a = parking.lot_markings([self.LOT], (0, 0, 30, 256), _z)
        b = parking.lot_markings([self.LOT], (30, 0, 256, 256), _z)
        whole = parking.lot_markings([self.LOT], (0, 0, 256, 256), _z)

        def key(c):
            p, q = c["n"]
            return (round(p[0], 2), round(p[1], 2), round(q[0], 2), round(q[1], 2))

        da, db, dw = self._dividers(a), self._dividers(b), self._dividers(whole)
        self.assertFalse(set(map(key, da)) & set(map(key, db)))
        self.assertEqual(sorted(map(key, da + db)), sorted(map(key, dw)))

    def test_aisle_lines_both_sides_and_off_a_crossing_street(self):
        aisle = ("a", roads.Spec(6.0, "paved", True, "service", False),
                 np.array([(0.0, 50.0, 1.0), (80.0, 50.0, 1.0)]), 0.0)
        chunks = parking.aisle_markings([aisle], {"a"}, (0, 0, 256, 256))
        north = [c for c in self._dividers(chunks) if min(c["n"][0][1], c["n"][1][1]) > 50]
        south = [c for c in self._dividers(chunks) if max(c["n"][0][1], c["n"][1][1]) < 50]
        self.assertTrue(len(north) > 10 and len(south) > 10)
        for c in north:
            y0, y1 = sorted((c["n"][0][1], c["n"][1][1]))
            self.assertAlmostEqual(y0, 53.0, delta=0.05)
            self.assertAlmostEqual(y1, 53.0 + parking.LOT_SPOT_D_M, delta=0.05)
        street = ("s", roads.Spec(8.0, "paved", True, "residential", False),
                  np.array([(40.0, 0.0, 0.0), (40.0, 100.0, 0.0)]), 0.0)
        crossed = parking.aisle_markings([aisle, street], {"a"}, (0, 0, 256, 256))
        for c in crossed:
            for x, y, _z in c["n"]:
                self.assertGreater(abs(x - 40.0), 4.0)
        self.assertEqual(parking.aisle_markings([street], {"a"}, (0, 0, 256, 256)), [])

    def test_a_mapped_lot_hides_the_aisle_lines_on_it(self):
        line = {"m": "line_white", "w": 0.1, "tl": 6.4, "p": 2, "n": [[10.0, 10.0, 0.0], [12.0, 10.0, 0.0]]}
        out = {"m": "line_white", "w": 0.1, "tl": 6.4, "p": 2, "n": [[80.0, 80.0, 0.0], [82.0, 80.0, 0.0]]}
        kept = parking.drop_covered([line, out], [self.LOT])
        self.assertEqual(kept, [out])

    def test_tile_collects_aisle_lines_only_when_asked(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_roads import FRAME, features, flat, rules
        f = features([[(0, 50), (80, 50)]], ["service"], ids=["a"])
        f.rules["subclass"] = rules([(0, "parking_aisle")])
        marks = []
        chunks = roads.build_tile(FRAME, 0, 0, [f], flat, marks=marks)
        self.assertTrue(marks)
        self.assertTrue(all(c["m"] == "line_white" for c in marks))
        self.assertFalse([c for c in chunks if c.get("m") == "line_white" and c.get("w") == 0.1])
        bare = roads.build_tile(FRAME, 0, 0, [f], flat)
        self.assertEqual([c.get("m") for c in bare], [c.get("m") for c in chunks])

    def test_a_parking_aisle_has_no_gravel_edge(self):
        # (it runs through a paved lot: a dirt strip stood on the asphalt between the stall rows)
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_roads import FRAME, features, flat, rules
        f = features([[(0, 50), (80, 50)]], ["service"], ids=["a"])
        plain = roads.build_tile(FRAME, 0, 0, [f], flat)
        f.rules["subclass"] = rules([(0, "parking_aisle")])
        aisle = roads.build_tile(FRAME, 0, 0, [f], flat)
        edge = roads.EDGE_DECAL["m"]
        self.assertTrue([c for c in plain if c.get("m") == edge])
        self.assertFalse([c for c in aisle if c.get("m") == edge])



class HitsTest(unittest.TestCase):
    """_hits: per point, in any polygon, the same as testing each point against each polygon."""

    def test_matches_point_by_point(self):
        from polygon import Path
        rng = np.random.default_rng(3)
        rings = [np.array([[0, 0], [10, 0], [10, 10], [0, 10]], float),
                 np.array([[20, 5], [30, 5], [25, 15]], float),
                 np.array([[200, 200], [210, 200], [210, 210]], float)]     # (far: skipped by its box)
        paths = [Path(r) for r in rings]
        pts = rng.uniform(-5, 35, (400, 2))
        want = [any(bool(p.contains_points(q[None])[0]) for p in paths) for q in pts]
        self.assertEqual(parking._hits(paths, pts).tolist(), want)
        self.assertTrue(any(want) and not all(want))

    def test_empty(self):
        self.assertEqual(parking._hits([], np.zeros((3, 2))).tolist(), [False] * 3)
        self.assertEqual(len(parking._hits([], [])), 0)


if __name__ == "__main__":
    unittest.main()
