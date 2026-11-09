"""Drag strips from OpenStreetMap raceways (dragstrips.py). Fixtures: Overpass snapshots of the raceways round
Bandimere Speedway (CO) and Island Dragway (NJ), 2026-11-03. (Keystone Raceway Park has no raceway in OSM.)"""
import json
import os
import unittest

import numpy as np

import dragstrips
import geodesy

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def lines_of(name, frame):
    with open(os.path.join(FIX, name)) as f:
        ways = json.load(f)["ways"]
    out = []
    for w in ways:
        ll = np.asarray(w["coords"], np.float64)
        e, n, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.0)
        t = w["tags"]
        out.append({"id": w["id"], "name": t.get("name", ""), "xy": np.stack([e, n], 1), "oneway": t.get("oneway", ""),
                    "width": t.get("width"), "tags": {}})       # (no sport tag: Layercake has none)
    return out


def heading(st):
    s, f = st["lanes"][0]
    d = np.asarray(f) - np.asarray(s)
    return float(np.degrees(np.arctan2(d[0], d[1]))) % 360      # compass


class Fixtures(unittest.TestCase):
    def test_bandimere_by_shape(self):
        fr = geodesy.EnuFrame(39.665, -105.19)
        got = dragstrips.strips_of(lines_of("dragstrip_bandimere.json", fr))
        self.assertEqual([s["id"] for s in got], ["earth_137280810"], "the strip only: not the kart track or dirt")
        st = got[0]
        self.assertEqual(st["dist"], dragstrips.QUARTER_M)
        self.assertEqual(st["conf"], "oneway")
        self.assertLess(abs(heading(st) - 354), 5, "north, the way's one-way sense")
        for s, f in st["lanes"]:
            self.assertAlmostEqual(float(np.hypot(*(np.asarray(f) - s))), dragstrips.QUARTER_M, 3)
        gap = float(np.hypot(*(st["lanes"][0][0] - st["lanes"][1][0])))
        self.assertAlmostEqual(gap, 2 * dragstrips.LANE_HALF_M, 3)

    def test_island_dragway_by_name(self):
        fr = geodesy.EnuFrame(40.878, -74.91)
        got = dragstrips.strips_of(lines_of("dragstrip_island_dragway.json", fr))
        self.assertEqual(len(got), 1)
        st = got[0]
        self.assertEqual(st["dist"], dragstrips.QUARTER_M)
        self.assertLess(abs(heading(st) - 110), 8, "east-southeast from the start toward the turnaround")
        # the right lane is south of the left on an eastbound strip
        self.assertLess(st["lanes"][0][0][1], st["lanes"][1][0][1])


class Keystone(unittest.TestCase):
    def test_keystone_by_shape_staged_at_the_road_end(self):
        """Keystone Raceway Park (New Alexandria PA): way 1568814841, unnamed, two nodes, not one-way. The north end
        is the one the roads come to (Stone Jug Road, US 22): staged there, running south."""
        fr = geodesy.EnuFrame(40.402, -79.3966)
        lines = lines_of("dragstrip_keystone.json", fr)
        race = [ln for ln in lines if ln["id"] == 1568814841]
        roads = [ln["xy"] for ln in lines if ln["id"] != 1568814841]
        got = dragstrips.strips_of(race, roads)
        self.assertEqual([s["id"] for s in got], ["earth_1568814841"])
        st = got[0]
        self.assertEqual(st["conf"], "access")
        self.assertEqual(st["dist"], dragstrips.QUARTER_M)
        self.assertLess(abs(heading(st) - 174), 6, "south from the north end")
        rev = dict(race[0], xy=race[0]["xy"][::-1])
        self.assertLess(abs(heading(dragstrips.strips_of([rev], roads)[0]) - 174), 6, "whatever the way's order")


class Rules(unittest.TestCase):
    def line(self, pts, **kw):
        d = {"id": 1, "name": "", "xy": np.asarray(pts, float), "oneway": "", "width": None, "tags": {}}
        d.update(kw)
        return d

    def test_names(self):
        for n in ("Island Dragway", "US 131 Drag Strip", "Keystone Raceway Park", "Maple Grove drag racing"):
            self.assertTrue(dragstrips.by_tags(n), n)
        for n in ("Daytona International Speedway", "Go Kart Track", ""):
            self.assertFalse(dragstrips.by_tags(n), n)
        self.assertTrue(dragstrips.by_tags("", {"sport": "motor;drag_racing"}))

    def test_eighth_mile(self):
        st = dragstrips.strip_of(self.line([[0, 0], [300, 0]], name="Tiny Dragway"))
        self.assertEqual(st["dist"], dragstrips.EIGHTH_M)

    def test_too_short(self):
        self.assertIsNone(dragstrips.strip_of(self.line([[0, 0], [150, 0]], name="Tiny Dragway")))

    def test_road_course_straight_not_by_shape(self):
        joined = [np.array([[0, 0], [-50, 80]], float), np.array([[900, 0], [950, 80]], float)]
        self.assertIsNone(dragstrips.strip_of(self.line([[0, 0], [900, 0]], oneway="yes"), joined))
        self.assertIsNone(dragstrips.strip_of(self.line([[0, 0], [900, 0]], oneway="yes", name="Pit Lane")))
        self.assertIsNotNone(dragstrips.strip_of(self.line([[0, 0], [900, 0]], oneway="yes")))

    def test_dead_end_gives_direction(self):
        ret = [np.array([[0, 0], [0, -60]], float)]     # the far end joins the return road: the start is x=900
        st = dragstrips.strip_of(self.line([[0, 0], [900, 0]], name="X Dragway"), ret)
        self.assertEqual(st["conf"], "deadend")
        self.assertLess(st["lanes"][0][1][0], st["lanes"][0][0][0], "runs toward x=0")

    def test_stage_pad_and_shutdown(self):
        st = dragstrips.strip_of(self.line([[0, 0], [1000, 0]], name="X Dragway", oneway="yes"))
        self.assertAlmostEqual(st["centre"][0], dragstrips.STAGE_PAD_M)
        st = dragstrips.strip_of(self.line([[0, 0], [410, 0]], name="X Dragway", oneway="yes"))
        self.assertAlmostEqual(st["centre"][0], 0.0, msg="no room: the stage at the way's start")

    def test_record_frame(self):
        fr = geodesy.EnuFrame(39.665, -105.19)
        st = dragstrips.strips_of(lines_of("dragstrip_bandimere.json", fr))[0]
        rec = dragstrips.record(st, fr, lambda la, lo: np.full(np.shape(la), 1800.0))
        self.assertEqual(len(rec["lanes"]), 2)
        self.assertEqual(len(rec["lanes"][0]["stage"]), 3)
        json.dumps(rec)


if __name__ == "__main__":
    unittest.main()
