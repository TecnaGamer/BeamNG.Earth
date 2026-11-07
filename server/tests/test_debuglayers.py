"""Placement precedence, production-backed debug providers and bounded metadata."""
from pathlib import Path
import json
import sys
import tempfile
import unittest
from types import SimpleNamespace
from dataclasses import replace
from unittest.mock import patch
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import clifftypes as ct
import debuglayers as dl
import cliffrocks as cr
import geodesy

class CliffTypesTest(unittest.TestCase):
    def test_researched_regions_and_exclusions(self):
        for lat,lon in ((36.06,-112.14),(38.3,-109.9),(34.95,-101.65),(34.45,-101.1),(-27.72,114.44)):
            self.assertEqual(int(ct.select(lat,lon)),ct.CANYON,(lat,lon))
        for lat,lon in ((31.88,-106.49),(30.3,-97.8),(35.78,-106.27),(-22.4,118.3),(51.0,-115.0)):
            self.assertEqual(int(ct.select(lat,lon)),ct.NATURAL,(lat,lon))

    def test_explicit_priority_overrides_order_and_fallback(self):
        rules = (ct.TYPES[1],ct.TYPES[0])
        self.assertEqual(int(ct.select(36,-112,rules=rules)),ct.CANYON)
        override = replace(ct.TYPES[0],key="test_override",priority=200,regions=ct.REGIONS[:1])
        self.assertEqual(int(ct.select(36,-112,rules=rules+(override,))),ct.NATURAL)
        self.assertEqual(int(ct.select(36,-112,context="road_cut",biomes=1)),ct.ARID_CUT)

    def test_array_boundary_agrees_with_far_and_compact(self):
        lat=np.array([36,36,34.95]);lon=np.array([-112,-115,-101.65])
        np.testing.assert_array_equal(ct.select(lat,lon),[3,0,3])
        np.testing.assert_array_equal(ct.far_families(lat,lon,[60,60,60],(20,30,60)),[1,0,1])
        np.testing.assert_array_equal(ct.far_families(lat,lon,[10,0,0],(20,30,60)),[-1,-1,1])
        self.assertEqual(ct.compact([3,3]),3)
        self.assertIsInstance(ct.compact([0,3]),np.ndarray)

    def test_mixed_near_tile_keeps_both_families_and_finite_surface(self):
        x=np.arange(13)*8.;h=np.broadcast_to(x*2,(13,13)).copy()
        keep=np.ones((12,12),bool);biomes=np.zeros(keep.shape,int)
        family=np.zeros(keep.shape,int);family[:,6:]=ct.CANYON
        rock=cr.mesh(h,keep,x,x,biomes,(0,0,96,96),(0,0,0),natural_family=family)
        self.assertEqual(set(rock.tri_materials),{ct.NATURAL,ct.CANYON})
        self.assertTrue(np.isfinite(rock.positions).all())
        self.assertTrue(np.isfinite(rock.normals).all())

class DebugLayersTest(unittest.TestCase):
    def message(self,layer="cliff_regions"):
        return dict(layer=layer,s=30,w=-120,n=40,e=-90)

    def test_detector_runs_preserve_holes_and_materials(self):
        keep=np.array([[1,1,0,1],[1,1,1,1]],bool)
        families=np.array([[0,0,3,3],[1,1,2,2]])
        runs=dl.footprint_grid(keep,families,np.arange(5)*111320.,np.arange(3)*111320.)
        self.assertEqual(runs,[[0,0.,0.,2.,1.],[3,3.,0.,4.,1.],[1,0.,1.,2.,2.],[2,2.,1.,4.,2.]])

    def test_region_provider_needs_no_server_or_downloads(self):
        result=dl.query(None,self.message())
        self.assertEqual(len(result["features"]),len(ct.REGIONS))
        self.assertIn("not geology",result["note"])
        json.dumps(result,allow_nan=False)

    def test_invalid_and_antimeridian_bounds(self):
        for changes in ({"n":float("nan")},{"e":-121},{"w":-1000},{"n":29}):
            with self.assertRaises(ValueError): dl.query(None,{**self.message(),**changes})
        dl.query(None,dict(layer="cliff_regions",s=-10,w=170,n=10,e=190))
        with self.assertRaises(ValueError): dl.query(None,self.message("missing"))

    def test_other_providers_use_production_rules(self):
        import buildings
        import vegetation
        with patch.object(buildings,"region_style",return_value=buildings.STYLES["alpine"]) as f:
            result=dl.query(None,self.message("building_styles"))
            self.assertEqual(f.call_count,960)
            self.assertEqual([r["label"] for r in result["legend"]],["Alpine"])
        for layer in ("vegetation_trees","vegetation_shrubs","vegetation_rocks"):
            with patch.object(vegetation,"regional",return_value=np.full(960,vegetation.G_DESERT_ROCK)) as f:
                result=dl.query(None,self.message(layer))
                f.assert_called_once()
                self.assertEqual(len(result["features"]),24)
                self.assertIn("not vegetation density",result["note"])

    def test_loaded_footprints_empty_disabled_and_budgets(self):
        import roads
        frame=geodesy.EnuFrame(35,-110)
        with tempfile.TemporaryDirectory() as tmp:
            conn=SimpleNamespace(active=1,cliff_faces=True,roads=SimpleNamespace(sent={(0,0)},geo=False),
                s=SimpleNamespace(out_dir=tmp,anchors={1:SimpleNamespace(frame=frame)}))
            path=Path(tmp)/"roads_1_0_0.json"
            path.write_text(json.dumps({"debugCliffs":[[3,-110,35,-109.999,35.001],[2,-110,35,-109.999,35.001]]}))
            result=dl.query(conn,self.message("cliff_generated"))
            self.assertEqual({f["type"] for f in result["features"]},{2,3})
            with patch.object(dl,"MAX_QUERY_BYTES",1):
                limited=dl.query(conn,self.message("cliff_generated"))
                self.assertEqual(limited["features"],[])
                self.assertIn("Display limited",limited["note"])
            with patch.object(dl,"MAX_TILES",0):
                self.assertIn("Display limited",dl.query(conn,self.message("cliff_generated"))["note"])
            conn.cliff_faces=False
            self.assertEqual(dl.query(conn,self.message("cliff_generated"))["features"],[])
            conn.cliff_faces=True
            path.write_text("{}")
            self.assertIn("predate",dl.query(conn,self.message("cliff_generated"))["note"])
            conn.roads.sent=set()
            self.assertIn("No recorded",dl.query(conn,self.message("cliff_generated"))["note"])

if __name__ == "__main__": unittest.main()
