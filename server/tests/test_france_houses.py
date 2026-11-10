"""French house geography, conservative classification, real meshes, LOD and assets."""
import os
os.environ.setdefault("BNE_REGIONAL_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "regional"))  # (PR #505's data: the tests' slice)
import copy
from collections import Counter
import json
import math
from pathlib import Path
import pickle
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "server"), str(ROOT / "tools"), str(Path(__file__).parent)]
import buildings as b
import cdae
import francehouses as fr
import geodesy
import house_regions
import mapgeom
import roads
import build_french_houses as assets
import test_regional_geotiles as regional_geo
import blockworker as bw


def rect(x=0., y=0., w=12., d=9.):
    return np.array([[x, y], [x + w, y], [x + w, y + d], [x, y + d]], float)


def features(frame, layer, records):
    coords, off = [], [0]
    columns = set().union(*(r[2] for r in records))
    for _, xy, _ in records:
        xy = np.asarray(xy)
        if not np.array_equal(xy[0], xy[-1]):
            xy = np.vstack([xy, xy[0]])
        lat, lon, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.)
        coords.extend(zip(lon, lat));off.append(len(coords))
    n = len(records)
    return mapgeom.Features(layer, np.array([r[0] for r in records]), np.full(n, 3, np.uint8),
                           np.arange(n + 1), np.arange(n + 1), np.array(off), np.array(coords),
                           attrs={k: np.array([r[2].get(k, np.nan if k in {"height", "num_floors"} else "") for r in records]) for k in columns})


class HouseTest(unittest.TestCase):
    def setUp(self):
        for mock in (patch.object(fr, "ON", True), patch.object(fr, "INFER", True)):
            mock.start();self.addCleanup(mock.stop)

    def plan(self, **attrs):
        return b.plan(rect(), {"class": "house", "num_floors": 2, "seed_id": "h-1", **attrs}, lat=48.77, lon=7.85)

    def test_regional_selection_uses_actual_polygons_and_altitude(self):
        cases = [(48.77, 7.85, 150, "alsace"), (47.75, 7.34, 230, "alsace"),
                 (49.00, 7.52, 220, "east"), (47.64, 6.86, 350, "east"),
                 (48.11, -1.68, 30, "brittany"), (50.63, 3.06, 30, "north"),
                 (43.48, -1.55, 30, "basque"), (43.30, -.37, 200, "pyrenees"),
                 (43.66, -1.43, 10, "southwest"), (43.60, 1.44, 140, "toulouse"),
                 (45.92, 6.87, 1030, "alpine"), (45.90, 6.13, 450, "east"),
                 (48.85, 2.35, 40, "paris_basin"), (43.61, 3.88, 60, "mediterranean")]
        for lat, lon, alt, expected in cases:
            with self.subTest(expected=expected, location=(lat, lon)):
                self.assertEqual(house_regions.profile(lat, lon, alt), expected)
        for lat, lon in ((49., 8.2), (47.56, 7.59), (43.32, -1.98), (45., -73.), (float("nan"), 3.)):
            self.assertIsNone(house_regions.profile(lat, lon, 1000.))

    def test_alsace_has_more_timber_houses_and_varied_colours(self):
        specs = [self.plan(seed_id=str(i)) for i in range(500)]
        counts = Counter(s.fr_house["family"] for s in specs)
        self.assertTrue(150 < counts["render"] < 250, counts)
        self.assertTrue(150 < counts["timber"] < 250, counts)
        self.assertTrue(50 < counts["halfhip"] < 170, counts)
        self.assertGreater(len({s.facade for s in specs}), 8)
        self.assertTrue(any(s.fr_house["roller"] for s in specs))
        self.assertTrue(any(s.roof == "half-hipped" for s in specs))

    def test_alsace_rosy_facades_are_rare_but_mapped_pink_is_kept(self):
        colours = [tuple(b.LOOKS[self.plan(seed_id=str(i)).facade][1]) for i in range(2000)]
        rosy = sum(c in fr.ALSACE_ROSY for c in colours)
        self.assertGreater(rosy, 10)
        self.assertLess(rosy, 80, "Rosy facades must stay below 4% of this sample")
        self.assertGreater(sum(c in fr.NEUTRALS for c in colours), 1000)
        mapped = self.plan(facade_color="#f7d4d9")
        np.testing.assert_allclose(b.LOOKS[mapped.facade][1], np.array([247, 212, 217]) / 255.)

    def test_known_nonhouses_retain_exact_specs_and_meshes(self):
        kinds = {"apartments", "office", "retail", "commercial", "school", "civic", "hotel", "hospital",
                 "warehouse", "industrial", "garage", "garages", "shed", "church", "farm", "barn", "roof"}
        for kind in sorted(kinds):
            with self.subTest(kind=kind):
                attrs = {"class": kind, "height": 8., "num_floors": 2, "seed_id": "same", "_fr_residential": True}
                with patch.object(fr, "ON", False):
                    old = b.plan(rect(), attrs, lat=48.77, lon=7.85)
                new = b.plan(rect(), attrs, lat=48.77, lon=7.85)
                self.assertIsNone(new.fr_house)
                for field in b.Spec.__slots__:
                    a, c = getattr(old, field), getattr(new, field)
                    if isinstance(a, np.ndarray):
                        np.testing.assert_array_equal(a, c)
                    else:
                        self.assertEqual(a, c, field)
                for a, c in zip(b.build([(old, 0, 0)]), b.build([(new, 0, 0)])):
                    np.testing.assert_array_equal(a, c)

    def test_unknown_requires_residential_land_and_small_house_geometry(self):
        self.assertIsNone(self.plan(**{"class": "", "_fr_residential": False}).fr_house)
        self.assertTrue(self.plan(**{"class": "", "_fr_residential": True}).fr_house["inferred"])
        with patch.object(fr, "INFER", False):
            self.assertIsNone(self.plan(**{"class": "", "_fr_residential": True}).fr_house)
            self.assertIsNotNone(self.plan().fr_house)
        for attrs in ({"num_floors": 3}, {"height": 12.}, {"name": "École primaire"}, {"_fr_part": True},
                      {"roof_shape": "flat"}, {"height": .2}):
            self.assertIsNone(self.plan(**{"class": "", "_fr_residential": True, **attrs}).fr_house)
        s = b.plan(rect(w=30, d=20), {"class": "", "_fr_residential": True}, lat=48.77, lon=7.85)
        self.assertIsNone(s.fr_house)

    def test_residential_land_institutional_overlap_excludes_guess(self):
        frame = geodesy.EnuFrame(48.77, 7.85)
        f = features(frame, "land_use", [("res", rect(-100, -100, 200, 200), {"class": "residential"}),
                                         ("school", rect(-10, -10, 20, 20), {"class": "school"})])
        land = fr.ResidentialLand([f])
        lat, lon, _ = frame.enu_to_geodetic(50., 0., 0.)
        self.assertTrue(land.at(float(lat), float(lon)))
        self.assertFalse(land.at(48.77, 7.85))
        self.assertFalse(land.at(48., 7.))

    def test_mapped_height_roof_material_and_colour_win(self):
        s = self.plan(height=9., roof_height=2., roof_shape="gabled", roof_material="slate",
                      facade_material="brick", facade_color="#c0b0a0", roof_color="#203040")
        self.assertEqual(s.roof, "gabled")
        self.assertEqual(s.fr_house["family"], "brick")
        self.assertAlmostEqual(s.height + 4.5 * math.tan(math.radians(s.pitch)), 9.)
        self.assertTrue(s.roof_mat.startswith("bne_fr_house_slate|c_0.125_0.188_0.251"))
        np.testing.assert_allclose(b.LOOKS[s.facade][1], np.array([192, 176, 160]) / 255.)

    def test_unsupported_mansards_and_building_parts_keep_upstream(self):
        for attrs in ({"roof_shape": "mansard"}, {"building_height": 12.}, {"min_height": 3.},
                      {"facade_material": "glass"}):
            self.assertIsNone(self.plan(**attrs).fr_house)

    def test_other_country_houses_and_off_keep_original(self):
        for lat, lon in ((49., 8.2), (39.2, -76.6), (47.56, 7.59)):
            self.assertIsNone(b.plan(rect(), {"class": "house"}, lat=lat, lon=lon).fr_house)
        with patch.object(fr, "ON", False):
            self.assertIsNone(self.plan().fr_house)

    def test_all_families_export_real_opaque_geometry_and_reduced_lods(self):
        found = {}
        for region, lat, lon, alt in (("alsace", 48.77, 7.85, 150), ("brittany", 48.39, -4.49, 30),
                                      ("north", 50.63, 3.06, 30), ("paris_basin", 48.84, 2.10, 100),
                                      ("basque", 43.48, -1.55, 30), ("alpine", 45.92, 6.87, 1030),
                                      ("loire", 47.39, .69, 50), ("pyrenees", 43.30, -.37, 200)):
            for i in range(40):
                s = b.plan(rect(), {"class": "house", "num_floors": 2, "seed_id": str(i), "_fr_altitude": alt}, lat=lat, lon=lon)
                found.setdefault(s.fr_house["family"], s)
        found["wood"] = self.plan(facade_material="wood")
        self.assertGreaterEqual(len(found), 10)
        for family, spec in found.items():
            with self.subTest(family=family):
                high = b.build([(spec, -1., 0.)]); low = b.build([(spec, -1., 0.)], lite=True)
                self.assertGreater(len(high[2]), len(low[2]) * 2)
                self.assertTrue(np.isfinite(high[0]).all())
                cross = np.cross(high[0][high[2][:, 1]] - high[0][high[2][:, 0]], high[0][high[2][:, 2]] - high[0][high[2][:, 0]])
                self.assertTrue((np.linalg.norm(cross, axis=1) > 1e-9).all())
                self.assertLess(high[3].max(), len(high[4]))
                self.assertGreater(high[0][:, 2].max(), spec.height)

    def test_square_hipped_roof_and_collision_winding(self):
        for shape in ("gabled", "hipped", "half-hipped", "flat"):
            s = b.plan(rect(w=10, d=10), {"class": "house", "num_floors": 2, "roof_shape": shape}, lat=48.77, lon=7.85)
            out = b.collision_mesh([(s, 0., 0.)])
            p, t = out[0], out[2]
            normal = np.cross(p[t[:, 1]] - p[t[:, 0]], p[t[:, 2]] - p[t[:, 0]])
            self.assertTrue((np.linalg.norm(normal, axis=1) > 1e-9).all())
            vertical = np.abs(normal[:, 2]) < 1e-9
            centre = p[t].mean(axis=1)[:, :2] - [5., 5.]
            self.assertTrue((np.sum(normal[vertical, :2] * centre[vertical], axis=1) >= -1e-8).all())
            self.assertTrue((normal[~vertical, 2] > 0).all())

    def test_no_flicker_in_window_states_when_ground_or_anchor_moves(self):
        s = self.plan(seed_id="0")
        a = b.build([(s, 0., 0.)])
        shifted = copy.deepcopy(s)
        shifted.ring += [1000., 2000.]
        c = b.build([(shifted, 327., 327.)])
        np.testing.assert_allclose(a[0] + [1000., 2000., 327.], c[0], atol=1e-6)
        np.testing.assert_array_equal(a[2], c[2])
        np.testing.assert_array_equal(a[3], c[3])
        np.testing.assert_array_equal(a[5], c[5])

    def test_pickled_looks_keep_their_colours_without_process_dictionary(self):
        s = pickle.loads(pickle.dumps(self.plan()))
        expected = b.build([(s, 0., 0.)])[5]
        dynamic = {k: v for k, v in b.LOOKS.items() if k.startswith("bne_fr_house_")}
        for k in dynamic:
            del b.LOOKS[k]
        try:
            np.testing.assert_array_equal(b.build([(s, 0., 0.)])[5], expected)
        finally:
            b.LOOKS.update(dynamic)

    def test_dense_house_tile_keeps_all_buildings_within_native_mesh_slots(self):
        items = [(self.plan(seed_id=str(i)), 0., 0.) for i in range(150)]
        for i, (spec, _, _) in enumerate(items):
            spec.ring += [i * 20., 0.]
        apartment = b.plan(rect(x=4000), {"class": "apartments", "height": 30., "seed_id": "apartment"}, lat=48.77, lon=7.85)
        items.append((apartment, 0., 0.))
        parts = b.build_parts(items, with_lod=True)
        self.assertLessEqual(len(parts), roads.BLD_PARTS_MAX)
        self.assertTrue(all(len(p[0]) <= b.MAX_MESH_VERTS for p in parts))
        points = np.concatenate([p[0] for p in parts])
        for i in range(150):
            self.assertTrue(np.any((np.abs(points[:, 0] - i * 20.) < .01) & (points[:, 2] > 1.)))
        self.assertTrue(np.any(points[:, 0] >= 4000.))

    def test_native_cdae_contains_model_and_coarse_lod(self):
        parts = b.build_parts([(self.plan(), 0., 0.)], with_lod=True)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "house.cdae"
            b.write(str(path), parts[0])
            shape = cdae.read(path)
            self.assertGreaterEqual(len(shape["meshes"]), 2)
            self.assertTrue(any(m["name"].startswith("bne_fr_house_") for m in shape["materials"]))

    def test_actual_house_location_is_used_instead_of_tile_centre(self):
        frame = geodesy.EnuFrame(48.96, 7.62)
        records = []
        for name, lat, lon in (("lorraine", 49.00, 7.52), ("alsace", 48.96, 7.64)):
            x, y, _ = frame.geodetic_to_enu(lat, lon, 0.)
            records.append((name, rect(float(x), float(y)), {"class": "house", "num_floors": 2}))
        f = features(frame, "buildings", records)
        items = b.tile_buildings(frame, -20000., -20000., 20000., 20000., [f], lambda la, lo: np.full(np.shape(la), 200.))
        self.assertEqual({s.fr_house["region"] for s, _, _ in items}, {"east", "alsace"})

    def test_original_textures_and_material_registration_are_reproducible(self):
        with tempfile.TemporaryDirectory() as td:
            assets.build(td)
            names = {p.name for p in assets.OUT.iterdir()}
            self.assertEqual(names, {p.name for p in Path(td).iterdir()})
            for name in names:
                with self.subTest(asset=name):
                    if name.endswith(".png"):
                        with Image.open(assets.OUT / name) as a, Image.open(Path(td) / name) as c:
                            np.testing.assert_array_equal(np.asarray(a), np.asarray(c))
                    else:
                        self.assertEqual(json.loads((assets.OUT / name).read_text()), json.loads((Path(td) / name).read_text()))
        material = json.loads((assets.OUT / "main.materials.json").read_text())
        self.assertEqual(set(material), set(fr.MATERIALS))
        self.assertTrue(all(not v["translucent"] for v in material.values()))
        lua = ROOT / "mod/beamng_earth/lua/ge/extensions/beamngEarth"
        self.assertIn("fr_houses/main.materials.json", (lua / "roads.lua").read_text())
        self.assertIn("bne_fr_house_glass_lit", (lua / "nightlights.lua").read_text())

    def test_startup_switches_have_separate_persistent_caches(self):
        import os
        values = []
        for enabled, infer in (("off", "on"), ("on", "on"), ("on", "off")):
            values.append(subprocess.check_output([sys.executable, "-c", "import revisit;print(revisit.code_key())"],
                          cwd=ROOT / "server", env=dict(os.environ, BNE_FR_HOUSES=enabled, BNE_FR_HOUSES_INFER=infer), text=True).strip())
        self.assertEqual(len(set(values)), 3)

    def test_real_haguenau_outlines_only_infer_untyped_with_residential_context(self):
        data = json.loads((Path(__file__).parent / "fixtures/haguenau_house_candidates.json").read_text())
        for item in data["features"]:
            ll = np.asarray(item["lonlat"])
            lon, lat = ll[:-1].mean(axis=0)
            frame = geodesy.EnuFrame(float(lat), float(lon))
            x, y, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.)
            ring = b.clean_ring(np.c_[x, y])
            cls = item["tags"]["building"]
            attrs = {"class": "" if cls == "yes" else cls, "seed_id": item["id"]}
            s = b.plan(ring, attrs, lat=float(lat), lon=float(lon))
            self.assertIsNone(s.fr_house)
            s = b.plan(ring, dict(attrs, _fr_residential=True), lat=float(lat), lon=float(lon))
            self.assertEqual(s.fr_house is not None, cls == "yes")

    def test_non_french_tiles_do_not_request_residential_land(self):
        calls = []
        class MD:
            def cell(self, key, layers=(), kind="demand"):
                calls.append(layers)
                return SimpleNamespace(layers={})
        self.assertEqual(b.road_tile_items(geodesy.EnuFrame(39., -76.), 0, 0, MD(), lambda la, lo: np.zeros(np.shape(la))), ([], []))
        self.assertFalse(any("land_use" in ls for ls in calls))

    def test_mesh_builder_accepts_an_iterator_without_losing_buildings(self):
        item = (self.plan(), 0., 0.)
        a, c = b.build_parts([item]), b.build_parts(iter([item]))
        self.assertEqual(len(a), len(c))
        for av, cv in zip(a[0], c[0]):
            np.testing.assert_array_equal(av, cv)


class PipelineTest(unittest.TestCase):
    def setUp(self):
        regional_geo.RegionalGeoTest.setUp(self)
        for mock in (patch.object(fr, "ON", True), patch.object(fr, "INFER", True)):
            mock.start();self.addCleanup(mock.stop)
        self.frame = self.tile.frame
        self.buildings = features(self.frame, "buildings", [("house", rect(-30, 30), {"class": "", "num_floors": 2}),
                                                            ("apartment", rect(30, 30), {"class": "apartments", "num_floors": 3})])
        self.land = features(self.frame, "land_use", [("res", rect(-100, -100, 200, 200), {"class": "residential"})])
        old = bw._s.md.cell
        def cell(key, layers=(), kind="demand"):
            result = old(key, layers, kind)
            if key == bw._s.md.first:
                result.layers.update(buildings=self.buildings, land_use=self.land)
            return result
        bw._s.md.cell = cell

    def test_live_prebuilt_reuse_and_rebase_keep_house_models(self):
        scratch = self.root / "prebuilt"
        key = bw.prebuild_geo(self.gx, self.gy, 15, str(scratch), True, False, False, False, 0., None, "FR", pole_profile=self.profile)
        self.assertIsNotNone(key)
        out = self.root / "live";out.mkdir()
        with patch.object(bw, "_tile_parts", side_effect=AssertionError("must reuse prebuild")):
            bw._geo_content(self.gx, self.gy, 15, str(out), "/test", True, False, False, False, 0., None, "FR", self.profile)
        names = bw.geo_names(self.tile.key)
        data = json.loads((out / names["json"]).read_text())
        shape = out / Path(data["bld"]["shape"]).name
        mats = {m["name"] for m in cdae.read(shape)["materials"]}
        self.assertTrue(any(m.startswith("bne_fr_house_") for m in mats), mats)
        self.assertTrue(any(m.startswith("bne_bld_") for m in mats), "the apartment keeps its upstream materials")
        original = shape.read_bytes()
        for aid, lon in ((1, 7.816), (2, 8.02)):
            bw._geo_skin(aid, 49.026, lon, 15, self.gx, self.gy, str(out), "/test", True, 0., 1 / 32,
                         lambda: self.fail("already built"))
        self.assertEqual(shape.read_bytes(), original)
