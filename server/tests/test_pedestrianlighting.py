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

    def test_unknown_negative_and_conditional_tags(self):
        for value in ("no", "", "automatic", "yes;no"):
            f = self.route(tag=value)
            f.attrs["osm_lit"] = np.array(["yes"])
            self.assertEqual(self.rows(f), [])

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
