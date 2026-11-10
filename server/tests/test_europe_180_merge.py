"""Regional scenery alongside 1.8's mountain roads and retained custom 3D models."""
import os
os.environ.setdefault("BNE_REGIONAL_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "regional"))  # (PR #505's data: the tests' slice)
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).parent)]
import blockworker as bw
import franceroads
import roads
import threedmr
from test_france_roads import french_features
import test_regional_geotiles as regional_geo
from test_roads import _zigzag
from test_threedmr import _binary_stl, _TETRAHEDRON


class MountainTest(unittest.TestCase):
    def test_french_pass_keeps_native_width_hairpins_and_markings(self):
        with patch.object(franceroads, "ON", True):
            f = french_features([_zigzag(4), [(0, 200), (300, 200)]], ["tertiary"] * 2)
            mountain, ordinary = roads.road_specs(f)
            self.assertTrue(mountain.france and mountain.mountain)
            self.assertEqual(mountain.width, roads.MOUNTAIN_ROAD_M)
            self.assertEqual(ordinary.width, 5.)
            self.assertTrue(ordinary.france and not ordinary.mountain)
            tapers = roads.road_tapers(f)[0]
            self.assertEqual(len(tapers), 4)
            self.assertTrue(all(t[2] == mountain.width + roads.HAIRPIN_WIDEN_M for t in tapers))
            line = np.array([[0., 0., 0.], [100., 0., 0.]])
            kinds = {look.get("kind") for look, _, _ in roads.extras(mountain, line, 7.8, country="FR")}
            self.assertNotIn("centre", kinds)
            self.assertIn("edge", kinds)
            self.assertTrue(franceroads.enabled(ordinary))

    def test_facts_round_trip_both_flags_and_stock_defaults(self):
        specs = [roads.Spec(6., "paved", False, "secondary", mountain=True, france=True),
                 roads.Spec(5., "paved", False, "tertiary", france=True),
                 roads.Spec(8., "paved", False, "secondary")]
        data = json.loads(json.dumps([roads._spec_json(sp) for sp in specs]))
        self.assertEqual(roads._specs_from(data, len(specs)), specs)
        self.assertFalse(specs[-1].mountain or specs[-1].france)


class CustomGeoTest(unittest.TestCase):
    def setUp(self):
        self.geo = regional_geo.RegionalGeoTest()
        self.geo.setUp()
        self.addCleanup(self.geo.doCleanups)

    def test_prebuilt_custom_model_and_regional_sign_survive_new_session(self):
        g = self.geo
        model_dir = g.root / "threedmr_cache"
        model_dir.mkdir()
        (model_dir / "tetra.stl").write_bytes(_binary_stl(_TETRAHEDRON))
        lat, lon, _ = g.tile.frame.enu_to_geodetic(100., 100., 0.)
        catalog = [{"file": "tetra.stl", "lat": float(lat), "lon": float(lon)}]
        catalog_path = model_dir / threedmr.LOCAL_MODELS_FILE
        catalog_path.write_text(json.dumps(catalog))
        scratch = g.root / "areas_scratch"
        # Model baking updates only the installed mod's material registry; keep this fixture self-contained.
        with patch.object(threedmr, "update_materials"):
            key = bw.prebuild_geo(g.gx, g.gy, 15, str(scratch), True, False, False,
                                  False, 0., None, "FR", "/temp/test/areas_scratch", pole_profile=g.profile)
        self.assertIsNotNone(key)
        out = g.root / "session"
        out.mkdir()
        with patch.object(bw, "_tile_parts", side_effect=AssertionError("must reuse prebuilt content")):
            bw._geo_content(g.gx, g.gy, 15, str(out), "/temp/test/session", True,
                            False, False, False, 0., None, "FR", g.profile)
        data = json.loads((out / bw.geo_names(g.tile.key)["json"]).read_text())
        self.assertIn("signs", data)
        self.assertEqual(len(data["b3dmr"]), 1)
        for entry in [data["signs"], *data["b3dmr"]]:
            self.assertTrue(entry["shape"].startswith("/temp/test/session/"))
            self.assertTrue((out / Path(entry["shape"]).name).is_file())
        model = out / Path(data["b3dmr"][0]["shape"]).name
        before = model.read_bytes()
        bw._geo_skin(2, 49.026, 8.02, 15, g.gx, g.gy, str(out), "/temp/test/session",
                     True, 0., 1 / 32, lambda: self.fail("content was already built"))
        self.assertEqual(model.read_bytes(), before)
        # Both the scenery profile and a changed local model must invalidate this content.
        args = (g.tile.key, 15, True, False, False, False, 0., None, "FR")
        self.assertNotEqual(key, bw._geo_content_key(*args, models_dir=str(g.root)))
        catalog[0]["lat"] += .001
        catalog_path.write_text(json.dumps(catalog))
        self.assertNotEqual(key, bw._geo_content_key(*args, models_dir=str(g.root), pole_profile=g.profile))


if __name__ == "__main__":
    unittest.main()
