"""Stock props filling mapped places (areaprops.py): solar farms, container terminals, marinas, trailer parks,
construction sites."""
import json
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import fast
import geodesy
import mapgeom
import areaprops as places

FRAME = geodesy.EnuFrame(40.0, -100.0, 0.0)


def feats(layer, items):
    """Features of a layer: items [(id, kind, rings in frame xy (list of (k, 2)), class, tags)]."""
    ids, kinds, part_off, ring_off, coord_off, lonlat, cls, tags = [], [], [0], [0], [0], [], [], []
    for fid, kind, rings, c, t in items:
        ids.append(fid)
        kinds.append(kind)
        for ring in rings:
            ring = np.asarray(ring, np.float64)
            if kind == mapgeom.POLYGON:
                ring = np.vstack([ring, ring[:1]])
            la, lo, _ = FRAME.enu_to_geodetic(ring[:, 0], ring[:, 1], np.zeros(len(ring)))
            lonlat.append(np.column_stack([lo, la]))
            coord_off.append(coord_off[-1] + len(ring))
        ring_off.append(ring_off[-1] + len(rings))
        part_off.append(part_off[-1] + 1)
        cls.append(c)
        tags.append(json.dumps(t))
    n = len(ids)
    return mapgeom.Features(layer=layer, ids=np.array(ids), kind=np.array(kinds, np.uint8),
                            part_off=np.array(part_off, np.int64), ring_off=np.array(ring_off, np.int64),
                            coord_off=np.array(coord_off, np.int64),
                            lonlat=np.concatenate(lonlat) if lonlat else np.zeros((0, 2)),
                            attrs={"subtype": np.array(["x"] * n), "class": np.array(cls),
                                   "tags": np.array(tags), "src_record": np.array([f"w{i}" for i in range(n)])})


class FakeMD:
    def __init__(self, **layers):
        self.layers = layers

    def cell(self, key, layers=(), kind="demand"):
        return type("C", (), {"layers": {k: v for k, v in self.layers.items() if k in layers}})()


def rect(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def flat(la, lo):
    return np.zeros(np.shape(la))


BOX = (-128.0, -128.0, 128.0, 128.0)
KEYS = ["k"]


@unittest.skipUnless(getattr(fast._ef, "VERSION", 0) >= places.NATIVE, "earthfast with places.rs")
class PlacesTest(unittest.TestCase):
    def setUp(self):
        places._PLANT_ARRAYS.clear()
        places._PARK_BUILT.clear()

    def rows(self, md, box=BOX, roads=(), country="US"):
        return places.tile_rows(FRAME, box, md, flat, roads, country, keys=KEYS)

    def test_solar_array_rows_face_the_equator(self):
        md = FakeMD(infrastructure=feats("infrastructure", [
            ("g1", mapgeom.POLYGON, [rect(-100, -100, 100, 100)], "generator",
             {"power": "generator", "generator:source": "solar"})]))
        got = self.rows(md)
        self.assertGreater(len(got), 100)
        self.assertTrue(all(r[0] == places.G_SOLAR for r in got))
        # northern hemisphere: local +y (the face) south, so the yaw is 180 degrees
        self.assertTrue(all(abs(r[4] - 180.0) < 1e-6 for r in got))
        # every other row ranks behind (far tiles show half the rows)
        self.assertTrue(any(r[7] >= places.SOLAR_ODD_RANK for r in got))
        self.assertTrue(any(r[7] < 0.1 for r in got))

    def test_rooftop_solar_and_plant_round_arrays(self):
        md = FakeMD(infrastructure=feats("infrastructure", [
            ("roof", mapgeom.POLYGON, [rect(-100, -100, 100, 100)], "generator",
             {"power": "generator", "generator:source": "solar", "location": "roof"})]))
        self.assertEqual(self.rows(md), [])
        # a plant with a mapped array inside: only the array is filled
        md = FakeMD(infrastructure=feats("infrastructure", [
            ("plant", mapgeom.POLYGON, [rect(-120, -120, 120, 120)], "plant",
             {"power": "plant", "plant:source": "solar"}),
            ("g1", mapgeom.POLYGON, [rect(-40, -40, 40, 40)], "generator",
             {"power": "generator", "generator:source": "solar"})]))
        got = self.rows(md)
        self.assertTrue(got)
        self.assertTrue(all(abs(r[1]) < 45 and abs(r[2]) < 45 for r in got))
        # a plant alone is filled
        places._PLANT_ARRAYS.clear()
        md = FakeMD(infrastructure=feats("infrastructure", [
            ("plant2", mapgeom.POLYGON, [rect(-120, -120, 120, 120)], "plant",
             {"power": "plant", "plant:source": "solar"})]))
        self.assertGreater(len(self.rows(md)), 100)

    def test_tiles_split_a_place_without_doubling(self):
        md = FakeMD(infrastructure=feats("infrastructure", [
            ("g1", mapgeom.POLYGON, [rect(-100, -100, 100, 100)], "generator",
             {"power": "generator", "generator:source": "solar"})]))
        whole = self.rows(md)
        left = self.rows(md, box=(-128.0, -128.0, 0.0, 128.0))
        right = self.rows(md, box=(0.0, -128.0, 128.0, 128.0))
        self.assertEqual(len(whole), len(left) + len(right))

    def test_roads_keep_their_way_through(self):
        md = FakeMD(infrastructure=feats("infrastructure", [
            ("g1", mapgeom.POLYGON, [rect(-100, -100, 100, 100)], "generator",
             {"power": "generator", "generator:source": "solar"})]))
        spec = type("S", (), {"width": 6.0})()
        road = [("r", spec, np.array([[0.0, -150.0, 0.0], [0.0, 150.0, 0.0]]), 0.0)]
        got = self.rows(md, roads=road)
        for r in got:       # a table (16 m long along x) stays clear of the road's edge
            self.assertGreater(abs(r[1]) - 8.0, 3.0 - 1e-6 + places.SOLAR_ROAD_M - 1.6)

    def test_terminal_stacks_and_cranes(self):
        land = feats("land_use", [("t1", mapgeom.POLYGON, [rect(-150, -150, 150, 60)], "industrial",
                                   {"landuse": "industrial", "man_made": "container_terminal"})])
        water = feats("water", [("w1", mapgeom.POLYGON, [rect(-400, 60, 400, 400)], "harbour", {})])
        md = FakeMD(land_use=land, water=water)
        got = self.rows(md, box=(-200.0, -200.0, 200.0, 200.0))
        stacks = [r for r in got if r[0] == places.G_STACK]
        cranes = [r for r in got if r[0] == places.G_CRANE]
        self.assertGreater(len(stacks), 200)
        self.assertGreaterEqual(len(cranes), 2)
        for r in cranes:
            # on the quay, the boom's long reach (local -y) north over the water: local +x at 180 degrees
            self.assertLess(r[2], 60.0)
            self.assertGreater(r[2], 40.0)
            self.assertAlmostEqual(r[4] % 360.0, 180.0, delta=1.0)
        # the apron behind the quay is free of boxes
        self.assertTrue(all(r[2] < 60.0 - places.STACK_APRON_M + 1.0 for r in stacks))
        # stacked: some boxes over others
        self.assertTrue(any(r[3] > places.STACK_H - 0.01 for r in stacks))

    def test_marina_boats_in_the_water_by_the_piers(self):
        water = feats("water", [("w1", mapgeom.POLYGON, [rect(-200, -200, 200, 200)], "water",
                                 {"leisure": "marina"})])
        land = feats("land_use", [("m1", mapgeom.POLYGON, [rect(-150, -150, 150, 150)], "marina",
                                   {"leisure": "marina"})])
        infra = feats("infrastructure", [("p1", mapgeom.LINE, [[[-100, 0], [100, 0]]], "pier",
                                          {"man_made": "pier"})])
        md = FakeMD(water=water, land_use=land, infrastructure=infra)
        got = self.rows(md)
        self.assertGreater(len(got), 10)
        for r in got:
            self.assertTrue(r[0].startswith("boat_"))
            self.assertGreater(abs(r[2]), places.PIER_HW)          # off the pier
            self.assertLess(r[3], 0.0)                              # the hull a little under the water's surface
        # no boats without a marina round the pier (a fishing pier on the sea)
        sea = feats("water", [("w2", mapgeom.POLYGON, [rect(-200, -200, 200, 200)], "ocean", {})])
        md = FakeMD(water=sea, infrastructure=infra)
        self.assertEqual(self.rows(md), [])

    def test_trailer_park_filled_unless_its_homes_are_mapped(self):
        land = feats("land_use", [("tp", mapgeom.POLYGON, [rect(-100, -60, 100, 60)], "residential",
                                   {"landuse": "residential", "residential": "trailer_park"})])
        md = FakeMD(land_use=land, buildings=feats("buildings", []))
        got = self.rows(md)
        self.assertGreater(len(got), 20)
        self.assertTrue(all(r[0] == places.G_CARAVAN for r in got))
        self.assertTrue(all(abs(r[6] - places.CARAVAN_H * places.NA_HOME_SCALE) < 1e-3 for r in got))
        places._PARK_BUILT.clear()
        homes = [(f"b{i}", mapgeom.POLYGON, [rect(-95 + 12 * i, -10, -85 + 12 * i, 10)], "residential", {})
                 for i in range(15)]
        md = FakeMD(land_use=land, buildings=feats("buildings", homes))
        self.assertEqual(self.rows(md), [])

    def test_construction_site_hoarding_and_crane(self):
        land = feats("land_use", [("c1", mapgeom.POLYGON, [rect(-60, -40, 60, 40)], "construction",
                                   {"landuse": "construction"})])
        md = FakeMD(land_use=land)
        got = self.rows(md)
        fence = [r for r in got if r[0] == places.G_HOARD]
        self.assertGreater(len(fence), (2 * 120 + 2 * 80) / places.HOARD_LEN - 8)
        cranes = [r for r in got if r[0] == places.G_TC]
        self.assertEqual(len(cranes), 1)
        self.assertTrue(places.TC_H * places.TC_SCALE[0] - 1e-6 <= cranes[0][6] <= places.TC_H * places.TC_SCALE[1] + 1e-6)

    def test_mapped_street_furniture(self):
        pt = mapgeom.POINT
        infra = feats("infrastructure", [
            ("b1", pt, [[[2.0, 1.0]]], "bench", {"amenity": "bench"}),
            ("h1", pt, [[[20.0, 6.0]]], "fire_hydrant", {"emergency": "fire_hydrant", "fire_hydrant:type": "pillar"}),
            ("h2", pt, [[[40.0, 6.0]]], "fire_hydrant", {"emergency": "fire_hydrant",
                                                         "fire_hydrant:type": "underground"}),
            ("p1", pt, [[[60.0, 6.0]]], "post_box", {"amenity": "post_box"}),
            ("k1", pt, [[[80.0, 7.0]]], "bicycle_parking", {"amenity": "bicycle_parking", "capacity": "20"}),
            ("w1", pt, [[[90.0, 6.0]]], "waste_basket", {"amenity": "waste_basket", "level": "-1"})])
        md = FakeMD(infrastructure=infra)
        spec = type("S", (), {"width": 8.0})()
        road = [("r", spec, np.array([[-100.0, 0.0, 0.0], [200.0, 0.0, 0.0]]), 0.0)]
        mapped = {}
        us = places.street_rows(FRAME, BOX, md, flat, road, "US", keys=KEYS, mapped_out=mapped)
        groups = sorted(r[0] for r in us)
        self.assertEqual(groups, ["bench", "bikestand", "bikestand", "bikestand", "bikestand", "hydrant", "postbox_us"])
        bench = next(r for r in us if r[0] == "bench")
        self.assertGreaterEqual(bench[2], 4.0 + 0.8 - 1e-6)        # moved off the carriageway (half width 4)
        self.assertAlmostEqual(bench[3], places.CURB_H)            # on the sidewalk
        self.assertEqual(len(mapped["hydrant"]), 2)                # the underground one counts as mapped
        eu = places.street_rows(FRAME, BOX, md, flat, road, "DE", keys=KEYS)
        self.assertIn("hydrant_eu", [r[0] for r in eu])
        self.assertIn("postbox_eu", [r[0] for r in eu])

    def test_guessed_furniture_gives_way(self):
        guessed = [["hydrant", 0.0, 0.0, 0.0, 0.0, 1.0, 1.5, 0.0], ["hydrant", 200.0, 0.0, 0.0, 0.0, 1.0, 1.5, 0.0],
                   ["bench", 0.0, 0.0, 0.0, 0.0, 1.0, 0.9, 0.0], ["streetlight", 1.0, 0.0, 0.0, 0.0, 1.0, 9.0, 0.0]]
        out = places.drop_inferred(guessed, {"hydrant": [(10.0, 0.0)]})
        self.assertEqual([r[0] for r in out], ["hydrant", "bench", "streetlight"])
        self.assertEqual(out[0][1], 200.0)
        out = places.drop_inferred(guessed, {"hydrant": [(1e4, 0.0)] * places.INFERRED_TILE_MIN})
        self.assertNotIn("hydrant", [r[0] for r in out])

    def test_obstruction_lights_on_the_topmost_tall_part(self):
        def spec(ring, h, min_h=0.0):
            return type("Spec", (), {"ring": np.asarray(ring, np.float64), "height": h, "min_h": min_h,
                                     "portal": False})()
        podium = spec(rect(0, 0, 60, 60), 160.0)                    # tall, but under the tower
        tower = spec(rect(10, 10, 50, 50), 120.0, 160.0)            # its top at 280 m
        low = spec(rect(100, 0, 140, 40), 90.0)
        spire = spec(rect(200, 0, 205, 5), 200.0)
        got = places.warning_rows([(podium, 2.0, 3.0), (tower, 2.0, 3.0), (low, 2.0, 3.0), (spire, 2.0, 3.0)],
                                  1000.0, 2000.0)
        tops = sorted({r[3] for r in got})
        self.assertEqual(len([r for r in got if r[3] == tops[-1]]), 4)        # the tower's four corners
        self.assertAlmostEqual(tops[-1], 3.0 + 280.0 + places.WARN_LIFT, places=2)
        self.assertEqual(len(got), 5)                                         # + the spire's one
        self.assertTrue(all(r[1] >= 1000.0 and r[2] >= 2000.0 for r in got))

    def test_jet_fans_in_long_tubes(self):
        ch = {"tunnel": True, "tunnel_len": 2700.0, "w": 8.0, "n": [[-10.0, 5.0, 100.0], [370.0, 5.0, 100.0]]}
        got = places.tunnel_fan_rows([ch])
        fans = [r for r in got if r[0] == places.G_FAN]
        self.assertEqual(len(fans), 2 * 4)                    # x = 0, 120, 240, 360: a pair each
        self.assertTrue(all(abs(r[4] - 270.0) < 1e-6 for r in fans))   # the housing's axis (local y) along +x
        self.assertTrue(all(100.0 + 4.6 < r[3] < 100.0 + 6.8 for r in fans))
        self.assertEqual(places.tunnel_fan_rows([dict(ch, tunnel_len=200.0)]), [])
        self.assertEqual(places.tunnel_fan_rows([dict(ch, no_tube=True)]), [])
        self.assertEqual(places.tunnel_fan_rows([dict(ch, tunnel_h=4.0)]), [])
        # a tile's piece gives only its own marks
        a = places.tunnel_fan_rows([dict(ch, n=[[-10.0, 5.0, 100.0], [128.0, 5.0, 100.0]])])
        b = places.tunnel_fan_rows([dict(ch, n=[[128.0, 5.0, 100.0], [370.0, 5.0, 100.0]])])
        self.assertEqual(len(a) + len(b), len(got))

    def test_kinds(self):
        f = feats("land_use", [("a", mapgeom.POLYGON, [rect(0, 0, 10, 10)], "industrial",
                                {"industrial": "container_stacking"}),
                               ("b", mapgeom.POLYGON, [rect(0, 0, 10, 10)], "camp_site",
                                {"tourism": "caravan_site"}),
                               ("c", mapgeom.POLYGON, [rect(0, 0, 10, 10)], "industrial",
                                {"industrial": "port"}),
                               ("d", mapgeom.POLYGON, [rect(0, 0, 10, 10)], "camp_site",
                                {"tourism": "camp_site", "caravans": "yes"}),
                               ("e", mapgeom.POLYGON, [rect(0, 0, 10, 10)], "camp_site",
                                {"tourism": "camp_site", "tents": "yes"})])
        self.assertEqual(places.kind_of(f, 0, "land_use"), "terminal")
        self.assertEqual(places.kind_of(f, 1, "land_use"), "caravan_site")
        self.assertIsNone(places.kind_of(f, 2, "land_use"))
        self.assertEqual(places.kind_of(f, 3, "land_use"), "caravan_site")
        self.assertIsNone(places.kind_of(f, 4, "land_use"))


if __name__ == "__main__":
    unittest.main()
