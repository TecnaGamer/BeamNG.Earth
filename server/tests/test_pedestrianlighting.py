import json
import os
import unittest
from unittest.mock import patch

import numpy as np

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import furniture
import mapcells
import mapgeom
import pedestrianlighting as lighting
from test_roads import FRAME, features, flat
from test_worldprops import FakeMD, box, feats, FRAME as AREA_FRAME


class LightingTest(unittest.TestCase):
    def route(self, cls="cycleway", tag="yes"):
        f = features([[(-100, 20), (100, 20), (100, 100)]], [cls], widths=[(0, 4.)])
        f.attrs["osm_way_lit"] = np.array([tag])
        return f

    def rows(self, f, bounds=(-110, -10, 130, 120), **overrides):
        kwargs = dict(frame=FRAME, box=bounds, cells=[f], urban=None, widths=None, md=FakeMD({}),
                      keys=[], elev=flat, props=[], mapped=[], rings=[], clearance=None, road_rows=[],
                      near_lamps=[], country_at=lambda *args: None)
        kwargs.update(overrides)
        return lighting.tile_rows(**kwargs)

    def test_half_density_preserves_stations_and_tile_seams(self):
        for kind in ("cycleway", "footway", "pedestrian"):
            f = self.route(kind)
            full = self.rows(f)
            with patch.dict(os.environ, BNE_HALF_STREET_LAMPS="on"):
                half = self.rows(f)
                self.assertCountEqual(half, self.rows(f, (-110,-10,0,120)) + self.rows(f,(0,-10,130,120)))
            self.assertTrue(all(row in full for row in half))
            self.assertLessEqual(abs(len(half[::3]) * 2 - len(full[::3])), 1)

    def test_unknown_negative_and_conditional_tags(self):
        for value in ("no", "", "automatic", "yes;no"):
            f = self.route(tag=value)
            f.attrs["osm_lit"] = np.array(["yes"])
            self.assertEqual(self.rows(f), [])

    def test_merged_ways_use_their_own_light_tags_after_cell_round_trip(self):
        import osmtags
        f = features([[(0, 0), (100, 0), (100, 100)]], ["pedestrian"])
        f.attrs["src_record"] = np.array(["w337675417@5"])
        f.rules["src_ways"] = mapgeom.rule_table([
            (0, "w1@1", [0., .22289]), (0, "w276088773@11", [.22289, .59205]),
            (0, "w337675417@5", [.59205, 1.])])
        osmtags.join(f, {276088773: {"lit": "yes"}, 1: {"lit": "no"}})
        f = mapgeom.Features.from_arrays("roads", f.to_arrays(""), "")
        rows = self.rows(f, (-5, -5, 120, 110))
        self.assertGreaterEqual(len(rows[::3]), 3)
        for _, x, y, *_ in rows[::3]:
            station = x if y < 5 else 100 + y
            self.assertTrue(.22289 * 200 <= station < .59205 * 200)
        # Reversing the tags on the longest and middle ways must not flood the segment.
        osmtags.join(f, {337675417: {"lit": "yes"}, 276088773: {"lit": "no"}})
        rows = self.rows(f, (-5, -5, 120, 110))
        self.assertTrue(rows)
        self.assertTrue(all(r[2] > 18 for r in rows[::3]))

    def test_strict_carriageway_filters_posts_and_pole_arms_by_station(self):
        from lightingranges import LightingRanges
        from test_furniture import road, LampTest
        r = road("merged", "residential", True)
        fid, _, line, _ = r
        spans = LightingRanges([line[:, :2]], [("no", 0., .3), ("yes", .3, .7), ("", .7, 1.)])
        walks = LampTest().walks(r, 1) + LampTest().walks(r, -1)
        with patch.dict(os.environ, BNE_STRICT_LIGHTING="on"):
            for town, buried, group in [(False, 1., furniture.G_LAMP), (True, 0., furniture.G_POLELAMP)]:
                rows = furniture.place([r], walks, (0, 0, 512, 256), lighting={fid: ""},
                                       light_ranges={fid: spans}, town=town, buried=buried)
                lamps = [r for r in rows if r[0] == group]
                self.assertTrue(lamps)
                self.assertTrue(all(spans.at(np.array(r[1:3])) == "yes" for r in lamps))

    def test_shore_side_is_dry_and_faces_the_path_in_either_direction(self):
        points = box(0, -30, 300, 40)
        ring = np.array([[lon, lat] for x, y in points for lat, lon, _ in [FRAME.enu_to_geodetic(x, y, 0)]])
        lake = mapgeom.build("water", np.array(["lake"]), [(mapgeom.POLYGON, [[ring]])])
        lake.attrs["subtype"] = np.array(["lake"])
        md = FakeMD({"water": lake})
        bounds = (-110, -60, 110, 30)
        for start, end in [(-100, 100), (100, -100)]:
            f = features([[(start, 0), (end, 0)]], ["pedestrian"], widths=[(0, 4.)])
            f.attrs["osm_way_lit"] = np.array(["yes"])
            rows = self.rows(f, bounds, md=md)
            self.assertTrue(rows)
            for post, lens in zip(rows[::3], rows[1::3]):
                self.assertAlmostEqual(post[2], -2.75, delta=.05)
                self.assertGreater(lens[2], post[2])
            split = self.rows(f, (-110,-60,0,30), md=md) + self.rows(f,(0,-60,110,30),md=md)
            self.assertCountEqual(rows, split)

    def test_prom_routes_clearance_orientation_and_profiles(self):
        for kind in ("cycleway", "pedestrian", "footway", "path"):
            rows = self.rows(self.route(kind))
            self.assertGreater(len(rows), 12)
            for post, lens, glow in zip(rows[::3], rows[1::3], rows[2::3]):
                self.assertEqual(lens[-1], lighting.PROFILES[kind][2])
                self.assertEqual(glow[0], furniture.G_GLOW)
                if post[1] < 90:
                    self.assertAlmostEqual(post[2], 22.75, delta=.15)
                    self.assertAlmostEqual(post[4], 270, delta=.1)
                    self.assertLess(lens[2], post[2])
                self.assertAlmostEqual(post[3], 10., delta=.03)

    def test_seam_matches_single_tile_and_covered_sites_are_skipped(self):
        f = self.route()
        rows = self.rows(f)
        split = self.rows(f, (-110,-10,0,120)) + self.rows(f, (0,-10,130,120))
        self.assertCountEqual(rows, split)
        first, second, third = rows[0], rows[3], rows[6]
        x,y = third[1:3]
        filtered = self.rows(f, mapped=[first], near_lamps=[second[1:3]],
                             rings=[np.array(box(x,y,4,4))])
        self.assertGreater(len(filtered), 0)
        for post in [first,second,third]:
            self.assertNotIn(post, filtered)

    def test_strict_road_preserves_mapped_post_and_fills_confirmed_gap(self):
        from test_furniture import LampTest, road
        r = road("lit", "residential", True)
        walks = LampTest().walks(r, 1) + LampTest().walks(r, -1)
        node = [furniture.G_LAMP, 100., 56., 1., 270., 1., 9., 0.]
        for tag in ("yes", "no", "", "sunset-sunrise"):
            with patch.dict(os.environ, BNE_STRICT_LIGHTING="on"):
                rows = furniture.place([r], walks, (0,0,512,256), mapped_lamps=[node],
                                       lighting={"lit":tag})
            posts = [p for p in rows if p[0] == furniture.G_LAMP]
            self.assertIn(node, posts)
            self.assertEqual(len(posts) > 1, tag == "yes")
            self.assertTrue(all(np.linalg.norm(np.subtract(p[1:3],node[1:3])) >= 18
                                for p in posts if p != node))

    def test_negative_area_excludes_lit_path_stations(self):
        f = self.route()
        poly = mapgeom.build("land_use", np.array(["park"]), [(mapgeom.POLYGON, [[
            np.array([[lon,lat] for x,y in box(0,20,250,30) for lat,lon,_ in [FRAME.enu_to_geodetic(x,y,0)]])
        ]])])
        poly.attrs["tags"] = np.array(['{"leisure":"park","lit":"no"}'])
        rows = self.rows(f, md=FakeMD({"land_use":poly}), keys=[mapcells.CellKey(14,1,1)])
        self.assertTrue(rows)
        self.assertTrue(all(post[2] > 34.9 for post in rows[::3]))

    def test_switches_are_independent(self):
        import experimental
        with patch.dict(os.environ, BNE_STRICT_LIGHTING="off", BNE_PEDESTRIAN_LIGHTING="on"):
            self.assertTrue(lighting.enabled())
            self.assertFalse(furniture.strict_lighting())
        self.assertEqual(experimental.env_for({"strictOsmLighting":True}), {"BNE_STRICT_LIGHTING":"on"})
        self.assertEqual(experimental.env_for({}), {})

    def test_direct_evidence_for_strict_roads(self):
        f = self.route(tag="")
        f.attrs["osm_lit"] = np.array(["yes"])
        with patch.dict(os.environ, BNE_STRICT_LIGHTING="on"):
            self.assertEqual(furniture.way_lighting(f,0), "")
        with patch.dict(os.environ, BNE_STRICT_LIGHTING="off"):
            self.assertEqual(furniture.way_lighting(f,0), "yes")

    def test_perimeter_uses_tags_and_faces_inside(self):
        for tags, kind in [({"amenity":"parking"}, "parking"), ({"place":"square"},"square"),
                           ({"leisure":"garden"},"garden"), ({"leisure":"park"},"park")]:
            poly = feats("land_use", [("one", box(50,50,100,100), {"tags":json.dumps(dict(tags,lit="yes"))})],
                         mapgeom.POLYGON)
            md = FakeMD({"land_use":poly})
            args = AREA_FRAME, (-10,-10,120,120), md, [mapcells.CellKey(14,1,1)]
            sites = list(lighting.area_sites(*args))
            self.assertGreater(len(sites), 5)
            for _,x,y,yaw,profile in sites:
                self.assertEqual(profile,kind)
                self.assertGreater(np.dot([50-x,50-y], [np.cos(np.radians(yaw)),np.sin(np.radians(yaw))]), 0)
            poly.attrs["tags"][0] = json.dumps(dict(tags, lit="no"))
            self.assertFalse(list(lighting.area_sites(*args)))


if __name__ == "__main__":
    unittest.main()
