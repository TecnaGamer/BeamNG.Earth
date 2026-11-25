"""The degradation test's input filter (degrade.py) and measurements (degrade_metrics.py). Run with unittest in this
directory."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

import numpy as np

from common import HERE, TREE
import degrade
import degrade_metrics as dm

sys.path[:0] = [str(TREE / "server")]
import mapgeom  # noqa: E402
import railtags  # noqa: E402


class Layer:
    """The parts of mapgeom.Features the filter touches: attrs and rules."""

    def __init__(self, n, attrs, rules):
        self.n = n
        self.attrs = attrs
        self.rules = rules

    def __len__(self):
        return self.n


def rule(feature, value):
    n = len(feature)
    return mapgeom.RuleTable(np.asarray(feature, np.int32), np.asarray(value), np.zeros(n, np.float32),
                             np.ones(n, np.float32))


def roads_layer():
    n = 3
    attrs = {"class": np.array(["primary", "residential", "service"]), "name": np.array(["Main", "", "Side"]),
             "osm_junction": np.array(["roundabout", None, None], object),
             railtags.lane_attr("lanes"): np.array([4, None, None], object),
             railtags.lane_attr("turn:lanes"): np.array(["left|through", None, None], object),
             railtags.LANE_PARTS: np.array([[(0.0, 1.0, "1", {"lanes": "4"})], None, None], object),
             "lane_osm_way": np.array([5, 0, 0], np.int64), "osm_oneway": np.array(["yes", "", ""])}
    rules = {"width": rule([0], np.array([7.5], np.float32)), "max_speed_kmh": rule([0, 1], np.array([50., 30.])),
             "surface": rule([2], np.array(["gravel"])), "oneway": rule([0], np.array([1.], np.float32)),
             "flags": rule([1], np.array(["is_bridge"])), "max_height_m": rule([1], np.array([4.], np.float32))}
    return Layer(n, attrs, rules)


class Filter(unittest.TestCase):
    def test_osm_tags_removed_before_the_join(self):
        tags = {7: {"lanes": 4.0, "width": 9.0, "sidewalk": "both", "maxspeed": 50.0, "osm_oneway": "yes",
                    "name": "Main", "osm_surface": "asphalt"}, 3: {"lanes": 2.0, "_coords": "c"}}
        frozen = json.dumps(tags, sort_keys=True)
        d1 = degrade.strip_tags(tags, degrade.osm_keys("D1"))
        self.assertEqual(json.dumps(tags, sort_keys=True), frozen)          # (the input is not changed)
        self.assertEqual(list(d1), [3, 7])                                  # (sorted: no input-order dependence)
        self.assertEqual(d1[7], {"maxspeed": 50.0, "osm_oneway": "yes", "name": "Main", "osm_surface": "asphalt"})
        self.assertEqual(d1[3], {"_coords": "c"})
        d2 = degrade.strip_tags(tags, degrade.osm_keys("D2"))
        self.assertEqual(d2[7], {"osm_oneway": "yes"})                      # (class only: one-way kept)
        self.assertIs(degrade.strip_tags(tags, degrade.osm_keys("D3")), tags)
        ways = [{"id": 1, "width": 12, "lanes": 3, "highway": "raceway", "surface": "asphalt"}]
        self.assertEqual(degrade.strip_ways(ways, degrade.osm_keys("D2")), [{"id": 1, "highway": "raceway"}])

    def test_d1_blanks_lane_data_and_widths_only(self):
        f = roads_layer()
        degrade.strip_roads(f, "D1")
        self.assertEqual(len(f.rules["width"].feature), 0)
        self.assertEqual(len(f.rules["max_speed_kmh"].feature), 2)
        self.assertTrue(all(v is None for v in f.attrs[railtags.lane_attr("lanes")]))
        self.assertTrue(all(v is None for v in f.attrs[railtags.LANE_PARTS]))
        self.assertEqual(f.attrs["osm_junction"][0], "roundabout")         # (D1 keeps the roundabout)
        self.assertEqual(f.attrs["name"][0], "Main")

    def test_d2_is_class_only(self):
        f = roads_layer()
        degrade.strip_roads(f, "D2")
        for name in ("width", "max_speed_kmh", "surface", "max_height_m"):
            self.assertEqual(len(f.rules[name].feature), 0, name)
        for name in ("oneway", "flags"):                                    # (kept: one-way, structures)
            self.assertEqual(len(f.rules[name].feature), 1, name)
        self.assertTrue(all(v is None for v in f.attrs["osm_junction"]))
        self.assertEqual(f.attrs["name"].tolist(), ["", "", ""])
        self.assertEqual(f.attrs["class"].tolist(), ["primary", "residential", "service"])
        self.assertEqual(f.attrs["osm_oneway"][0], "yes")

    def test_idempotent_and_deterministic(self):
        a, b = roads_layer(), roads_layer()
        degrade.strip_roads(a, "D2")
        cols = {k: id(v) for k, v in a.attrs.items()}
        degrade.strip_roads(a, "D2")                                        # (again: nothing replaced)
        self.assertEqual(cols, {k: id(v) for k, v in a.attrs.items()})
        degrade.strip_roads(b, "D2")
        for k in a.attrs:
            self.assertEqual(np.asarray(a.attrs[k]).tolist(), np.asarray(b.attrs[k]).tolist(), k)
        for k in a.rules:
            self.assertEqual(len(a.rules[k].feature), len(b.rules[k].feature), k)

    def test_steps_that_keep_roads(self):
        for step in ("D0", "D3", "D4", "D5", "D6"):
            f = roads_layer()
            degrade.strip_roads(f, step)
            self.assertEqual(len(f.rules["width"].feature), 1, step)
            self.assertEqual(f.attrs[railtags.lane_attr("lanes")][0], 4, step)

    def test_buildings(self):
        def layer():
            return Layer(2, {"height": np.array([12., np.nan], np.float32), "num_floors": np.array([4., 2.], np.float32),
                             "roof_shape": np.array(["gabled", ""]), "class": np.array(["house", "apartments"]),
                             "has_parts": np.array([True, False]), "level": np.array([np.nan, -1.], np.float32),
                             "src_dataset": np.array(["OpenStreetMap", "Microsoft ML Buildings"])}, {})
        d4, d5 = layer(), layer()
        degrade.strip_buildings(d4, "D4")
        self.assertFalse(np.isfinite(d4.attrs["height"]).any())
        self.assertFalse(np.isfinite(d4.attrs["num_floors"]).any())
        self.assertEqual(d4.attrs["roof_shape"].tolist(), ["", ""])
        self.assertEqual(d4.attrs["class"].tolist(), ["house", "apartments"])
        self.assertEqual(float(d4.attrs["level"][1]), -1.0)                 # (an underground level is not a height)
        degrade.strip_buildings(d5, "D5")
        self.assertEqual(d5.attrs["class"].tolist(), ["", ""])
        self.assertFalse(d5.attrs["has_parts"].any())
        self.assertEqual(d5.attrs["src_dataset"].tolist(), ["OpenStreetMap", "Microsoft ML Buildings"])
        for step in ("D0", "D1", "D2", "D3", "D6"):
            f = layer()
            degrade.strip_buildings(f, step)
            self.assertEqual(float(f.attrs["height"][0]), 12.0, step)

    def test_env(self):
        self.assertEqual(degrade.env("D0"), {"BNE_DEGRADE": "D0"})
        self.assertEqual(degrade.env("D3")["BNE_DEM_ZOOM"], "12")
        self.assertEqual(degrade.env("D6")["BNE_ROADSURF"], "off")
        with self.assertRaises(ValueError):
            degrade.env("D9")

    def test_install_patches_map_reads(self):
        # (in its own process: the patch is global)
        code = ("import sys; sys.path[:0]=[%r,%r]; import degrade, mapdata; m=mapdata.MapData; o=m._osm_read;"
                "degrade.install('D1'); assert m._osm_read is not o and m._j1_step=='D1';"
                "assert m._dec_key(None,None,'roads',{})==(None,{});"
                "print('ok')") % (str(TREE / "server"), str(HERE))
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertIn("ok", out.stdout, out.stderr[-2000:])


class Metrics(unittest.TestCase):
    def test_stations_and_edges_on_a_straight_road(self):
        quad = np.array([[0., 0.], [100., 0.], [100., 100.], [0., 100.]])
        lines = [dict(cls="primary", level="ground", width=7.0, lanes=2, xy=[[-10., 50.], [110., 50.]])]
        st = dm.stations(lines, quad)
        self.assertEqual(len(st["pt"]), 50)
        self.assertTrue(np.allclose(st["pt"][:, 1], 50.0))
        # (a 7 m wide road, 3 m left / 4 m right of the centre line)
        tris = np.array([[[-10, 47, 0], [110, 47, 0], [110, 53, 0]], [[-10, 47, 0], [110, 53, 0], [-10, 53, 0]]],
                        float)
        tris[:, :, 1] = np.where(tris[:, :, 1] > 50, 53.0, 46.0)
        grid = dm.raster(tris, quad)
        L, R, c = dm.edges(grid, st["pt"], st["tan"])
        self.assertTrue(c.all())
        self.assertTrue(np.allclose(L, 3.0, atol=dm.RES_M))
        self.assertTrue(np.allclose(R, 4.0, atol=dm.RES_M))
        far = dm.edges(dm.raster(np.zeros((0, 3, 3)), quad), st["pt"], st["tan"])
        self.assertTrue(np.isnan(far[0]).all() and not far[2].any())

    def test_matching_follows_position_and_direction(self):
        quad = np.array([[0., 0.], [100., 0.], [100., 100.], [0., 100.]])
        lines0 = [dict(cls="primary", level="ground", xy=[[0., 50.], [100., 50.]])]
        st = dm.stations(lines0, quad)
        lines1 = [dict(cls="primary", level="ground", xy=[[0., 51.], [40., 51.]]),          # near, parallel
                  dict(cls="primary", level="ground", xy=[[40., 50.5], [100., 50.5]]),      # nearer for x > 40
                  dict(cls="service", level="ground", xy=[[60., 0.], [60., 100.]])]          # crossing
        m = dm.match_lines(st, lines1)
        self.assertTrue((m[st["pt"][:, 0] < 40] == 0).all())
        self.assertTrue((m[st["pt"][:, 0] > 40] == 1).all())

    def test_grade_and_ride(self):
        s = np.arange(1.0, 101.0, dm.STATION_M)
        z = 0.05 * s
        grade, ride = dm.profiles(z, np.zeros(len(s), int), s)
        self.assertTrue(np.allclose(grade[np.isfinite(grade)], 5.0))
        self.assertTrue(np.allclose(ride[np.isfinite(ride)], 0.0))
        z2 = np.where(s > 50, 0.05 * s + 0.2, 0.05 * s)           # (a 20 cm step)
        g2, r2 = dm.profiles(z2, np.zeros(len(s), int), s)
        self.assertGreater(np.nanmax(np.abs(r2)), 0.5)

    def test_buildings_by_footprint(self):
        sq = lambda x, y, w: [[x, y], [x + w, y], [x + w, y + w], [x, y + w], [x, y]]   # noqa: E731
        full = dict(footprints=[["a", sq(0, 0, 10)], ["b", sq(20, 0, 10)]],
                    pieces=[[sq(0, 0, 10), 0, 9.0, 3, "flat"], [sq(20, 0, 4), 0, 6.0, 2, "gabled"],
                            [sq(24, 0, 6), 0, 15.0, 5, "flat"]])
        deg = dict(footprints=full["footprints"],
                   pieces=[[sq(0, 0, 10), 0, 6.0, 2, "flat"], [sq(20, 0, 10), 0, 6.0, 2, "gabled"]])
        rows = dm.building_rows(full, deg, {"b"})
        self.assertEqual(rows["err_m"].tolist(), [3.0, 9.0])
        self.assertEqual(rows["mapped"].tolist(), [False, True])
        self.assertEqual(rows["roof_eq"].tolist(), [True, False])            # (b's largest piece: flat, 6 x 6 m)


if __name__ == "__main__":
    unittest.main()
