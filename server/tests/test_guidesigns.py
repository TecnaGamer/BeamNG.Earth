"""guidesigns: green guide signs from a motorway's own data: exits (advance 1 MILE / 1/2 MILE, EXIT at the split),
route and mileage signs after a merge; the tile's mesh (one atlas, steel), deterministic across tiles."""
import json
import math
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cdae  # noqa: E402
import guidesigns as gs  # noqa: E402
import roads  # noqa: E402

BIG = (-1e5, -1e5, 1e5, 1e5)


def spec(cls, oneway=True, d=1, width=11.0, lanes=3):
    return roads.Spec(width, "paved", False, cls, oneway, dir=d, lanes=lanes)


def interstate():
    """I-76 eastbound (+x): a 3 km segment from a merge at x=0 to an exit at x=3000, its link leaving there; the link
    that merged at x=0; the next segment after the exit."""
    main = ("m1", spec("motorway"), np.array([(0.0, 0.0, 10.0), (3000.0, 0.0, 10.0)]), 0.0)
    exit_link = ("x1", spec("motorway_link", width=4.0, lanes=1),
                 np.array([(3000.0, 0.0, 10.0), (3150.0, -40.0, 10.0)]), 0.0)
    on_link = ("o1", spec("motorway_link", width=4.0, lanes=1),
               np.array([(-150.0, -40.0, 10.0), (0.0, 0.0, 10.0)]), 0.0)
    nxt = ("m2", spec("motorway"), np.array([(3000.0, 0.0, 10.0), (6000.0, 0.0, 10.0)]), 0.0)
    attrs = {
        "m1": {"routes": json.dumps([{"network": "US:I", "ref": "76"}]),
               "dests": json.dumps([{"to_segment_id": "x1", "labels": [{"value": "Oak Ridge", "type": "unknown"},
                                                                       {"value": "Fairview", "type": "unknown"},
                                                                       {"value": "PA 28", "type": "route_ref"}],
                                     "when": {"heading": "forward"}},
                                    {"to_segment_id": "m2", "labels": [{"value": "Harrisburg", "type": "unknown"}]}])},
        "x1": {}, "o1": {}, "m2": {},
    }
    return [main, exit_link, on_link, nxt], attrs


class DataTest(unittest.TestCase):
    def test_shields(self):
        self.assertEqual(gs.shield_of("US:I", "76"), ("interstate", "76"))
        self.assertEqual(gs.shield_of("US:US", "30"), ("us", "30"))
        self.assertEqual(gs.shield_of("US:PA", "28"), ("state", "28"))
        self.assertIsNone(gs.shield_of("e-road", "E 40"))
        self.assertEqual(gs._label_shield("I 376"), ("interstate", "376"))
        self.assertEqual(gs._label_shield("US-30"), ("us", "30"))
        self.assertEqual(gs.routes('[{"network":"US:I","ref":"76"},{"network":"US:I","ref":"76"}]'), [("interstate", "76")])
        self.assertEqual(gs.routes(None), [])
        self.assertEqual(gs.routes("not json"), [])

    def test_exits_by_heading(self):
        d = '[{"to_segment_id":"a","labels":[{"value":"X","type":"unknown"}],"when":{"heading":"backward"}}]'
        self.assertEqual(gs.exits(d, 1), [])
        self.assertEqual(gs.exits(d, -1), [("a", ["X"], [])])


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.roads, self.attrs = interstate()
        self.towns = [(60000.0, 2000.0, "Harrisburg"), (25000.0, -3000.0, "Carlisle"), (-50000.0, 0.0, "Behind"),
                      (90000.0, 60000.0, "Off To The Side")]

    def signs(self, box=BIG, **kw):
        return gs.plan(self.roads, self.attrs, box, towns_xy=self.towns, **kw)

    def test_exit_and_route_signs(self):
        got = {g["kind"]: g for g in self.signs() if g["id"].startswith("m1:")}
        adv = [g for g in self.signs() if g["kind"] == "advance" and g["id"].startswith("m1:")]
        self.assertEqual(sorted(round(g["x"]) for g in adv), [1391, 2195], "1 mile and 1/2 mile before the exit")
        self.assertTrue(all(g["overhead"] for g in adv), "3 lanes: over the road")
        rows = adv[0]["rows"]
        self.assertEqual(rows[0], [("shield", "state", "28")])
        self.assertIn([("text", "Oak Ridge", gs.TEXT_M)], rows)
        self.assertIn([("text", "Fairview", gs.TEXT_M)], rows)
        self.assertNotIn("Harrisburg", json.dumps(rows), "the through destination is not the exit's")
        self.assertEqual(round(got["exit"]["x"]), 3000 - gs.EXIT_BEFORE_M)
        self.assertFalse(got["exit"]["overhead"])
        self.assertEqual(got["route"]["rows"], [[("shield", "interstate", "76"), ("text", "EAST", gs.SMALL_M)]])
        self.assertEqual(round(got["route"]["x"]), gs.ROUTE_AT_M, "after the merge")
        self.assertEqual([r[0][1] for r in got["mileage"]["rows"]], ["Carlisle", "Harrisburg"])
        self.assertEqual(got["mileage"]["rows"][0][0][2], "15", "24.5 km from the sign: 15 mi")

    def test_km_countries(self):
        adv = [g for g in self.signs(mph=False) if g["kind"] == "advance" and g["id"].startswith("m1:")]
        self.assertEqual(sorted(round(g["x"]) for g in adv), [2000, 2500])
        self.assertIn([("text", "500 m", gs.SMALL_M)], [g["rows"][-1] for g in adv])

    def test_no_exit_without_a_link(self):
        rds = [r for r in self.roads if r[0] not in ("x1",)]
        att = dict(self.attrs)
        att["m1"] = {"routes": self.attrs["m1"]["routes"]}
        kinds = {g["kind"] for g in gs.plan(rds, att, BIG)}
        self.assertNotIn("advance", kinds)
        self.assertNotIn("exit", kinds)

    def test_geometric_exit_without_destinations(self):
        att = {k: {} for k in self.attrs}
        adv = [g for g in gs.plan(self.roads, att, BIG) if g["kind"] == "advance" and g["id"].startswith("m1:")]
        self.assertEqual(len(adv), 2)
        self.assertEqual(adv[0]["rows"][0], [("text", "Exit", gs.TEXT_M)])

    def test_two_lane_road_signs_at_the_shoulder(self):
        self.roads[0] = ("m1", spec("motorway", lanes=2), self.roads[0][2], 0.0)
        adv = [g for g in self.signs() if g["kind"] == "advance" and g["id"].startswith("m1:")]
        self.assertTrue(adv and not any(g["overhead"] for g in adv))

    def test_westbound_reads_the_other_way(self):
        rds = [("w1", spec("motorway", d=-1), self.roads[0][2], 0.0),
               ("x9", spec("motorway_link", width=4.0, lanes=1), np.array([(0.0, 0.0, 10.0), (-150.0, 40.0, 10.0)]), 0.0)]
        adv = [g for g in gs.plan(rds, {"w1": {}, "x9": {}}, BIG) if g["kind"] == "advance"]
        self.assertEqual(sorted(round(g["x"]) for g in adv), [805, 1609])
        self.assertTrue(all(g["d"][0] < -0.99 for g in adv))

    def test_same_signs_from_neighbouring_tiles(self):
        whole = sorted(g["id"] for g in self.signs())
        tiles = []
        for x0 in range(-512, 7000, 256):
            tiles += [g["id"] for g in self.signs(box=(x0, -256, x0 + 256, 256))]
        self.assertEqual(whole, sorted(tiles))


class MeshTest(unittest.TestCase):
    def test_mesh_encodes_and_faces_the_driver(self):
        rds, attrs = interstate()
        signs = gs.plan(rds, attrs, BIG, towns_xy=[(60000.0, 0.0, "Harrisburg"), (25000.0, 0.0, "Carlisle")])
        P, T, UV, M = gs.mesh(signs, (0.0, 0.0, 0.0))
        self.assertEqual(T.max() < len(P), True)
        self.assertEqual(set(np.unique(M)), {gs.ATLAS, gs.STEEL})
        self.assertTrue((UV >= -1e-6).all() and (UV <= 1 + 1e-6).all())
        data = cdae.encode(P, T, uvs=UV, material=gs.MATERIALS, tri_materials=M, object_name="guide")
        self.assertGreater(len(data), 1000)
        # the atlas quads (panel faces, text) face -x: toward the eastbound drivers coming from the west
        tri = T[M == gs.ATLAS]
        n = np.cross(P[tri[:, 1]] - P[tri[:, 0]], P[tri[:, 2]] - P[tri[:, 0]])
        n = n[np.linalg.norm(n, axis=1) > 1e-9]
        self.assertTrue((n[:, 0] < 0).all(), "every face toward the driver")
        # the overhead panel's bottom clears the road
        over = [g for g in signs if g["overhead"]][0]
        near = np.linalg.norm(P[:, :2] - np.array([over["x"], over["y"]]), axis=1) < 1.0
        ztop = P[T[M == gs.ATLAS].ravel()][:, 2]
        self.assertGreaterEqual(float(P[near & (np.abs(P[:, 0] - over["x"]) < 1.0)][:, 2].max()), 10.0 + gs.CLEAR_M)
        self.assertGreaterEqual(float(ztop.min()), 10.0 + gs.GROUND_M - 1e-3)

    def test_text_layout(self):
        self.assertGreater(gs.text_width("Oak Ridge", gs.TEXT_M), gs.text_width("Oak", gs.TEXT_M))
        w, h = gs.panel_size([[("text", "Oak Ridge", gs.TEXT_M)], [("text", "1 MILE", gs.SMALL_M)]])
        self.assertTrue(3.0 < w < 8.0 and 1.5 < h < 3.0, (w, h))

    def test_nothing(self):
        self.assertIsNone(gs.mesh([], (0, 0, 0)))


def geo(x, y):
    """A flat stand-in for the frame: 85 km a degree of longitude, 111 km of latitude, from 40 N 77 W."""
    return 40.0 + np.asarray(y, np.float64) / 111000.0, -77.0 + np.asarray(x, np.float64) / 85000.0


def square(name, region, xa, xb, ya=-1e5, yb=1e5):
    from polygon import Path
    return (name, region, [(Path(np.array([(xa, ya), (xb, ya), (xb, yb), (xa, yb), (xa, ya)])), [])])


class TollTest(unittest.TestCase):
    def test_named_toll_road_gets_far_apart_gantries(self):
        self.assertTrue(gs.is_toll("Pennsylvania Turnpike", None))
        self.assertTrue(gs.is_toll("", '[{"network":"US:PA:Turnpike","ref":"76"}]'))
        self.assertTrue(gs.is_toll("Jane Addams Memorial Tollway", None))
        self.assertFalse(gs.is_toll("Schuylkill Expressway", '[{"network":"US:I","ref":"76"}]'))
        # 40 km of turnpike cut in two at a bridge (x 20000): gantries only where it passes -76.8 / -76.6 (x 17000,
        # 34000), not one a piece
        rds = [("t1", spec("motorway"), np.array([(0.0, 0.0, 10.0), (20000.0, 0.0, 10.0)]), 0.0),
               ("t2", spec("motorway"), np.array([(20100.0, 0.0, 10.0), (40000.0, 0.0, 10.0)]), 0.0)]
        at = {"t1": {"name": "Pennsylvania Turnpike"}, "t2": {"name": "Pennsylvania Turnpike"}}
        got = gs.plan(rds, at, BIG, geo=geo)
        tolls = sorted(round(g["x"]) for g in got if g["kind"] == "toll")
        self.assertEqual(tolls, [17000, 34000])
        adv = sorted(round(g["x"]) for g in got if g["kind"] == "tolladv")
        self.assertEqual(adv, [round(17000 - gs.TOLL_ADV_M), round(34000 - gs.TOLL_ADV_M)])
        self.assertEqual([g for g in got if g["kind"] == "tolladv"][0]["rows"][0], [("text", "CASHLESS TOLL", gs.TEXT_M)])
        self.assertEqual(gs.plan(rds, {"t1": {"name": "Schuylkill Expressway"}}, BIG, geo=geo), [])
        self.assertEqual(gs.plan(rds, at, BIG), [], "no gantries without the frame's lat / lon")
        P, T, UV, M = gs.mesh([g for g in got if g["kind"] == "toll"][:1], (0, 0, 0))
        self.assertTrue(T.max() < len(P))
        low = P[(np.abs(P[:, 1]) < 5.5) & (np.abs(P[:, 0] - 17000) < 1.0)][:, 2]
        self.assertGreaterEqual(float(low.min()), 10.0 + gs.CLEAR_M - 0.2 - 1e-6, "nothing low over the lanes")

    def test_no_gantry_at_an_interchange(self):
        rds = [("t1", spec("motorway"), np.array([(0.0, 0.0, 10.0), (30000.0, 0.0, 10.0)]), 0.0),
               ("x1", spec("motorway_link", width=4.0, lanes=1),
                np.array([(17300.0, -6.0, 10.0), (17500.0, -50.0, 10.0)]), 0.0)]
        got = gs.plan(rds, {"t1": {"name": "Ohio Turnpike"}, "x1": {}}, BIG, geo=geo)
        self.assertEqual([g for g in got if g["kind"] == "toll"], [])
        self.assertTrue([g for g in got if g["kind"] == "exit"])


class MidSegmentTest(unittest.TestCase):
    def test_ramp_leaving_mid_segment_gets_its_signs(self):
        """Overture's segments run through interchanges: a ramp leaving partway along still gets 1 MILE, 1/2 MILE and
        EXIT; one joining partway gets the route sign after it."""
        rds = [("m1", spec("motorway"), np.array([(0.0, 0.0, 10.0), (3000.0, 0.0, 10.0), (6000.0, 0.0, 10.0)]), 0.0),
               ("x1", spec("motorway_link", width=4.0, lanes=1),
                np.array([(2500.0, -6.5, 10.0), (2700.0, -60.0, 10.0)]), 0.0),
               ("o1", spec("motorway_link", width=4.0, lanes=1),
                np.array([(3800.0, -60.0, 10.0), (4000.0, -6.5, 10.0)]), 0.0),
               ("w1", spec("motorway_link", width=4.0, lanes=1),            # (the other way: not an exit here)
                np.array([(5000.0, -6.5, 10.0), (4800.0, -60.0, 10.0)]), 0.0)]
        at = {"m1": {"routes": json.dumps([{"network": "US:I", "ref": "80"}])}, "x1": {}, "o1": {}, "w1": {}}
        got = {(g["kind"], round(g["x"])) for g in gs.plan(rds, at, BIG)}
        self.assertIn(("exit", round(2500 - gs.EXIT_BEFORE_M)), got)
        self.assertIn(("advance", round(2500 - 1609.344)), got)
        self.assertIn(("advance", round(2500 - 804.672)), got)
        self.assertIn(("route", round(4000 + gs.ROUTE_AT_M)), got)
        self.assertEqual(len([k for k in got if k[0] == "exit"]), 1)


class LocationTest(unittest.TestCase):
    def test_county_line_signs_both_ways(self):
        counties = [square("Allegheny County", "US-PA", -1e5, 5000.0), square("Washington County", "US-PA", 5000.0, 1e5)]
        rds = [("p1", spec("primary", oneway=False, width=12.0, lanes=2),
                np.array([(0.0, 0.0, 5.0), (10000.0, 0.0, 5.0)]), 0.0),
               ("s1", spec("secondary", oneway=False, width=7.0, lanes=2),
                np.array([(0.0, 900.0, 5.0), (10000.0, 900.0, 5.0)]), 0.0)]
        got = gs.plan(rds, {}, BIG, counties=counties)
        by = {(g["id"].split(":")[0], g["kind"], g["d"][0] > 0): g for g in got}
        east = by[("p1", "welcome", True)]
        self.assertAlmostEqual(east["x"], 5015.0, delta=gs.SAMPLE_M + 1)
        self.assertEqual(east["rows"][1], [("text", "Washington County", gs.TEXT_M)])
        west = by[("p1", "welcome", False)]
        self.assertAlmostEqual(west["x"], 4985.0, delta=gs.SAMPLE_M + 1)
        self.assertEqual(west["rows"][1][0][1], "Allegheny County")
        g = by[("s1", "county", True)]
        self.assertEqual([r[0][1] for r in g["rows"]], ["ENTERING", "WASHINGTON", "COUNTY"])
        self.assertEqual(len(got), 4)
        P, T, UV, M = gs.mesh(got, (0, 0, 0))
        self.assertTrue(T.max() < len(P))
        self.assertGreater(float(P[:, 1].max()), 900 + 3.5, "on the shoulder, off the road")

    def test_mileposts_on_an_interstate(self):
        counties = [square("Cumberland County", "US-PA", -1e5, 1e5)]
        rds = [("i1", spec("motorway"), np.array([(0.0, 0.0, 5.0), (0.0, 5000.0, 5.0)]), 0.0)]
        at = {"i1": {"routes": json.dumps([{"network": "US:I", "ref": "81"}])}}
        got = [g for g in gs.plan(rds, at, BIG, geo=geo, counties=counties) if g["kind"] == "mile"]
        miles = [(g["rows"][1][0][1], round(g["y"])) for g in got]
        # 40 N is 0.28 degrees north of PA's south edge: mile 19.3 at y 0, a mile every 1607.5 m
        m0 = (40.0 - gs.STATE_EDGES["PA"][0]) * 69.05
        want = [(str(k), round((k - m0) / 69.05 * 111000.0)) for k in range(int(m0) + 1, int(m0) + 4)]
        self.assertEqual([m for m, _ in miles], [m for m, _ in want])
        for (_, y), (_, wy) in zip(miles, want):
            self.assertAlmostEqual(y, wy, delta=2)
        self.assertEqual(gs.plan(rds, at, BIG, geo=geo, counties=[square("X", "US-ZZ", -1e5, 1e5)]), [])
        P, T, UV, M = gs.mesh(got, (0, 0, 0))
        self.assertLess(float(P[:, 2].max()) - 5.0, 2.2, "a small marker")


if __name__ == "__main__":
    unittest.main()
