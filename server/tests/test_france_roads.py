"""French scenery: real boundary gates, directional paint, models and tile export."""
import os
os.environ.setdefault("BNE_REGIONAL_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "regional"))  # (PR #505's data: the tests' slice)
import json
import math
import os
from pathlib import Path
import pickle
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "server"), str(ROOT / "tools"), str(Path(__file__).parent)]
import franceroads as fr
import furniture
import geodesy
import mapgeom
import markings
import poleprofiles
import regionaltraffic
import roadmesh
import roads
import build_french_traffic as assets
from test_roads import features, rules


def spec(width=6., cls="secondary", **kw):
    return roads.Spec(width, "paved", False, cls, france=True, **kw)


def french_features(lines, classes, **kw):
    f = features(lines, classes, **kw)
    frame = geodesy.EnuFrame(49.02127, 7.80967)
    xy = np.concatenate([np.asarray(line)[:, :2] for line in lines])
    la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.)
    f.lonlat[:] = np.c_[lo, la]
    return f


class FrenchTest(unittest.TestCase):
    def setUp(self):
        on = patch.object(fr, "ON", True)
        on.start()
        self.addCleanup(on.stop)


class WidthTest(FrenchTest):
    def test_rural_defaults_reach_real_spec_and_ai_lanes(self):
        f = french_features([[(0, 0), (200, 0)]] * 3, ["secondary", "tertiary", "unclassified"])
        ss = roads.road_specs(f)
        self.assertEqual([s.width for s in ss], [6., 5., 4.5])
        self.assertTrue(all(s.france for s in ss))
        self.assertTrue(all(roads.lane_layout(s)[1] == 2 for s in ss))
        self.assertTrue(fr.narrow(ss[1]))

    def test_width_and_lanes_from_map_win(self):
        f = french_features([[(0, 0), (200, 0)]] * 3, ["tertiary"] * 3, widths=[(0, 7.2)])
        f.attrs["osm_lanes"] = np.array([None, 3, 2], object)
        ss = roads.road_specs(f)
        self.assertAlmostEqual(ss[0].width, 7.2, places=4)
        self.assertEqual(ss[1].width, 9.)
        self.assertEqual(roads.lane_layout(ss[1])[1], 3)
        self.assertEqual(ss[2].width, 5.)
        self.assertEqual(roads.lane_layout(ss[2])[1], 2)

    def test_measured_width_is_not_replaced_by_fallback(self):
        f = french_features([[(0, 0), (200, 0)]], ["tertiary"])
        got = roads.road_specs(f, widths=lambda fid, ll: (4.2, 1.5))[0]
        self.assertEqual(got.width, 4.2)
        self.assertEqual(got.walk, 1.5)
        self.assertTrue(got.france)

    def test_real_border_and_ambiguous_crossing_keep_upstream(self):
        f = features([[(0, 0), (200, 0)]], ["tertiary"])
        f.lonlat[:] = [[7.79547, 49.06593], [7.80, 49.067]]  # Germany
        self.assertFalse(roads.road_specs(f)[0].france)
        f = french_features([[(0, 0), (200, 0)]], ["tertiary"])
        f.lonlat[-1] = [7.79547, 49.06593]
        self.assertFalse(roads.road_specs(f)[0].france)

    def test_off_and_on_have_distinct_in_memory_spec_caches(self):
        f = french_features([[(0, 0), (200, 0)]], ["tertiary"])
        on = roads.road_specs(f)[0]
        with patch.object(fr, "ON", False):
            off = roads.road_specs(f)[0]
        self.assertEqual(on.width, 5.)
        self.assertFalse(off.france)
        self.assertNotEqual(on.width, off.width)

    def test_ring_and_divided_roads_not_narrowed(self):
        f = french_features([[(0, 0), (50, 30), (0, 0)]], ["secondary"])
        f.attrs["osm_junction"] = np.array(["roundabout"])
        got = roads.road_specs(f)[0]
        self.assertEqual(got.width, roads.ring_width(None))


class PaintTest(FrenchTest):
    line = np.array([[0., 0., 0.], [300., 0., 3.]])

    def test_mre_shape_dimensions_spacing_and_no_edge_or_centre(self):
        out = roads.extras(spec(4.8, "tertiary"), self.line, 7.8, country="FR")
        paint = [(lk, q) for lk, w, q in out if lk.get("kind")]
        self.assertEqual({lk["kind"] for lk, q in paint}, {"mre"})
        self.assertEqual(len(paint), 40)
        # Each half is 10 cm wide, with 25 cm longitudinal sides and 8 cm step.
        a, b = paint[0][1], paint[1][1]
        self.assertAlmostEqual(np.ptp(a[:, 1]), .1)
        self.assertAlmostEqual(np.linalg.norm(a[3, :2] - a[0, :2]), .25)
        self.assertAlmostEqual(b[0, 0] - a[1, 0], .08)
        self.assertAlmostEqual(paint[2][1][:, 0].mean() - a[:, 0].mean(), 15.)
        self.assertTrue(all(lk["polygon"] for lk, q in paint))

    def test_mre_threshold_urban_oneway_and_unpainted_exclusions(self):
        for sp in (spec(5.2), spec(4.8, oneway=True), spec(4.8)._replace(urban=True)):
            self.assertFalse(fr.narrow(sp))
        guided = fr.paint(spec(4.8), self.line, {"lane_markings": "no"})
        self.assertTrue(guided)
        self.assertEqual({lk["kind"] for lk, _, _ in guided}, {"mre"})
        self.assertEqual(fr.paint(spec(6.), self.line, {"lane_markings": "no"}), [])

    def test_no_invented_edges_on_small_departmental_roads(self):
        out = fr.paint(spec(6.), self.line)
        self.assertEqual({lk["kind"] for lk, _, _ in out}, {"centre"})
        self.assertTrue(all(abs(w - .10) < 1e-6 for _, w, _ in out))

    def test_primary_edge_t2_and_motorway_t4(self):
        for sp, dash in ((spec(7., "primary"), 3.), (spec(10., "motorway", oneway=True, dir=1), 39.)):
            out = fr.paint(sp, np.array([[0., 0, 0], [200., 0, 0]]))
            edges = [(lk, w, p) for lk, w, p in out if lk["kind"] == "edge"]
            self.assertTrue(edges)
            self.assertEqual({lk["side"] for lk, w, p in edges}, {-1, 1})
            self.assertAlmostEqual(edges[0][2][-1, 0] - edges[0][2][0, 0], dash)

    def test_mixed_line_is_solid_nearest_forbidden_direction(self):
        for tag, sign in (("forward", -1), ("backward", 1)):
            out = fr.paint(spec(), self.line, {"overtaking:" + tag: "no"})
            solid = [p for lk, w, p in out if lk["kind"] == "centre" and len(p) == 2 and np.ptp(p[:, 0]) > 299]
            self.assertEqual(len(solid), 1)
            self.assertEqual(np.sign(solid[0][:, 1].mean()), sign)
            self.assertGreater(len(out), 10)

    def test_mapped_no_overtaking_is_one_continuous_white_line(self):
        out = fr.paint(spec(), self.line, {"overtaking": "no"})
        self.assertEqual(len(out), 1)
        np.testing.assert_array_equal(out[0][2], self.line)

    def test_osm_directional_value_and_divider_are_honoured(self):
        a = fr.paint(spec(), self.line, {"overtaking": "forward"})
        b = fr.paint(spec(), self.line, {"overtaking:backward": "no"})
        self.assertEqual(len(a), len(b))
        for (_, wa, pa), (_, wb, pb) in zip(a, b):
            self.assertEqual(wa, wb)
            np.testing.assert_array_equal(pa, pb)
        solid = fr.paint(spec(), self.line, {"divider": "solid_line"})
        self.assertEqual(len(solid), 1)

    def test_no_guessed_mre_on_farm_service_roads(self):
        self.assertEqual(fr.paint(spec(3.5, "service"), self.line), [])

    def test_bend_fallback_and_recall_arrows_and_mapped_override(self):
        p = np.array([[0., 0, 0], [300., 0, 0], [300., 300, 0], [600., 300, 0]])
        self.assertEqual(len(fr.bends(p)), 2)
        out = fr.paint(spec(), p)
        self.assertTrue(any(lk["kind"] == "recall" for lk, _, _ in out))
        mapped = fr.paint(spec(), p, {"overtaking": "yes"})
        self.assertFalse(any(lk["kind"] == "recall" for lk, _, _ in mapped))

    def test_paint_phases_and_polygons_translate_with_the_world(self):
        sp = spec(4.8)
        shift = np.array([12340., -8000., 0.])
        a, b = fr.paint(sp, self.line), fr.paint(sp, self.line + shift)
        self.assertEqual(len(a), len(b))
        for (la, wa, pa), (lb, wb, pb) in zip(a, b):
            self.assertEqual((la, wa), (lb, wb))
            np.testing.assert_allclose(pa + shift, pb, atol=1e-8)

    def test_real_tile_build_and_native_mesh_keep_mre(self):
        frame = geodesy.EnuFrame(49.02127, 7.80967)
        f = french_features([[(-300, 100), (600, 100)]], ["tertiary"])
        elev = lambda lat, lon: np.zeros(np.shape(lat))
        chunks = roads.build_tile(frame, 0, 0, [f], elev, country="FR")
        poly = [c for c in chunks if c.get("poly") and c["m"] == fr.PAINT_MATERIAL]
        self.assertTrue(poly)
        self.assertTrue(all(max(p[0] for p in c["poly"]) <= 258.01 for c in poly))
        # Exercise the actual native road-marking mesh builder, not just helpers.
        mesh = roadmesh.build(frame, chunks, elev, (0., 0., 0.))
        self.assertTrue(len(mesh[0]))
        self.assertTrue(np.isfinite(np.asarray(mesh[0])).all())


class TrafficTest(FrenchTest):
    def test_giveway_plate_changes_only_france_and_avoids_signals(self):
        row = ["giveway", 10., 5., 0., 90., 1., 3., 0.]
        out = regionaltraffic.localize([row], lambda *p: "FR")
        self.assertEqual(out[0][0], "fr_giveway_m9c")
        self.assertEqual(row[0], "giveway")
        self.assertEqual(regionaltraffic.localize([row], lambda *p: "DE"), [row])
        signal = ["signal", 12., 5., 0., 0., 1., 9.4, 0.]
        self.assertEqual(regionaltraffic.localize([row, signal], lambda *p: "FR"), [row, signal])

    def junction(self, reverse=False):
        p = np.array([[0., -200, 0], [0., 0, 0]])
        if reverse:
            p = p[::-1]
        return [("one", spec(4., oneway=True, dir=-1 if reverse else 1), p, 7.8),
                ("cross", spec(), np.array([[-100., 0, 0], [0., 0, 0]]), 7.8),
                ("cross2", spec(), np.array([[0., 0, 0], [100., 0, 0]]), 7.8)]

    def test_noentry_placement_is_withdrawn_everywhere(self):
        box = (-300, -300, 300, 300)
        for code in ("FR", "DE", "US"):
            for reverse in (False, True):
                self.assertEqual(regionaltraffic.place(self.junction(reverse), box, lambda *p: code), [])

    def test_degree_two_oneway_continuation_does_not_get_noentry(self):
        rr = self.junction()[:1]
        rr += [("continue", spec(4., oneway=True, dir=1), np.array([[0., 0, 0], [0., 100, 0]]), 7.8)]
        self.assertEqual(regionaltraffic.place(rr, (-300, -300, 300, 300), lambda *p: "FR"), [])

    def test_dangerous_and_successive_bends_have_correct_hand(self):
        box = (-1000, -1000, 1000, 1000)
        for pts, want in (([[0, 0, 0], [300, 0, 0], [300, 300, 0]], {"fr_a1a", "fr_a1b"}),
                          ([[0, 0, 0], [300, 0, 0], [300, 200, 0], [600, 200, 0]], {"fr_a1d"}),
                          ([[0, 0, 0], [300, 0, 0], [300, -200, 0], [600, -200, 0]], {"fr_a1c"})):
            rr = [("curve", spec(), np.asarray(pts, float), 7.8)]
            rows = regionaltraffic.place(rr, box, lambda *p: "FR")
            self.assertEqual({r[0] for r in rows}, want)
            self.assertEqual(regionaltraffic.place(rr, box, lambda *p: "DE"), [])

    def test_sign_tile_ownership_is_unique_and_order_independent(self):
        rr = self.junction()
        country = lambda *p: "FR"
        whole = regionaltraffic.place(rr, (-300, -300, 300, 300), country)
        parts = regionaltraffic.place(rr, (-300, -300, 0, 300), country)
        parts += regionaltraffic.place(rr, (0, -300, 300, 300), country)
        self.assertEqual(sorted(whole), sorted(parts))
        self.assertEqual(sorted(whole), sorted(regionaltraffic.place(rr[::-1], (-300, -300, 300, 300), country)))

    def test_no_inferred_rural_poles_but_explicit_lighting_remains(self):
        rr = [("country-road", spec(), np.array([[0., 100, 0], [500., 100, 0]]), 7.8)]
        rows = furniture.place(rr, [], (0, 0, 512, 256), pole_profile=poleprofiles.FR_KIT)
        self.assertFalse(any("pole" in r[0] for r in rows))
        lit = furniture.place(rr, [], (0, 0, 512, 256), lighting={"country-road": "yes"})
        self.assertTrue(any(r[0] == "streetlight" for r in lit))

    def test_urban_utility_poles_are_preserved_with_france_mode_on(self):
        rr = [("town-road", spec()._replace(urban=True),
               np.array([[0., 100, 0], [500., 100, 0]]), 7.8)]
        args = (rr, [], (0, 0, 512, 256))
        with patch.object(fr, "ON", False):
            before = furniture.place(*args, pole_profile=poleprofiles.FR_KIT)
        after = furniture.place(*args, pole_profile=poleprofiles.FR_KIT)
        self.assertTrue(any(r[0] in {"fr_wood_pole", "pole_concrete_recessed"} for r in after))
        self.assertEqual(after, before)

    def test_off_has_no_new_signs_or_models(self):
        with patch.object(fr, "ON", False):
            self.assertEqual(regionaltraffic.place(self.junction(), (-300, -300, 300, 300), lambda *p: "FR"), [])


class AssetTest(unittest.TestCase):
    def assert_collada_equal(self, expected, actual):
        """Strict XML/indices/materials and numeric arrays within 1e-12.

        Scalar text is not a portable representation of floating point zero:
        Windows produced -1.1565e-16 where Linux produced -2.313e-16 for a normal.
        Keep this allowance below the exporter's meaningful coordinate precision;
        topology, UVs, normals and positions are still compared, not discarded.
        """
        a, b = ET.fromstring(expected), ET.fromstring(actual)
        tag = "{http://www.collada.org/2005/11/COLLADASchema}float_array"
        aa, bb = list(a.iter(tag)), list(b.iter(tag))
        self.assertEqual(len(aa), len(bb))
        for x, y in zip(aa, bb):
            self.assertEqual(x.attrib, y.attrib)
            av, bv = np.fromstring(x.text or "", sep=" "), np.fromstring(y.text or "", sep=" ")
            self.assertEqual(len(av), int(x.attrib["count"]))
            self.assertEqual(len(bv), int(y.attrib["count"]))
            self.assertTrue(np.isfinite(av).all() and np.isfinite(bv).all())
            np.testing.assert_allclose(av, bv, rtol=0., atol=1e-12, err_msg=x.attrib.get("id"))
            x.text = y.text = ""
        self.assertEqual(ET.tostring(a), ET.tostring(b))

    def test_portable_comparison_accepts_roundoff_and_rejects_asset_changes(self):
        original = ('<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema">'
                    '<geometry id="sign"><float_array id="normal" count="3">'
                    '-2.313e-16 -1 0</float_array><triangles material="white">'
                    '<p>0 1 2</p></triangles></geometry></COLLADA>')
        self.assert_collada_equal(original, original.replace('-2.313e-16', '-1.1565e-16'))
        for changed in (original.replace('-2.313e-16', '0.001'),
                        original.replace('-1 0</float_array>', '-0.99 0</float_array>'),
                        original.replace('0 1 2</p>', '0 2 1</p>'),
                        original.replace('material="white"', 'material="red"'),
                        original.replace('count="3"', 'count="4"')):
            with self.subTest(changed=changed), self.assertRaises(AssertionError):
                self.assert_collada_equal(original, changed)

    def test_models_front_faces_uv_height_materials_and_lods(self):
        materials = json.loads((assets.OUT / "main.materials.json").read_text())
        ns = {"c": "http://www.collada.org/2005/11/COLLADASchema"}
        for kind in assets.KINDS:
            mesh = assets.model(kind)
            p = np.asarray(mesh.p)
            self.assertTrue(np.isfinite(p).all())
            self.assertAlmostEqual(p[:, 2].max(), assets.HEIGHTS[kind], places=3)
            self.assertTrue(set(mesh.tri) <= set(materials))
            face_tri = mesh.tri["bne_frt_" + kind]
            self.assertTrue(all(np.cross(p[b] - p[a], p[c] - p[a])[1] < 0 for a, b, c in face_tri))
            used = {i for tri in face_tri for i in tri}
            self.assertTrue(all(-1e-12 <= mesh.uv[i][0] <= 1 + 1e-12 and
                                -1e-12 <= mesh.uv[i][1] <= 1 + 1e-12 for i in used))
            xml = ET.parse(assets.OUT / ("bne_" + kind + ".dae"))
            self.assertEqual(len(xml.findall(".//c:geometry", ns)), 4)
            for material in set(mesh.tri):
                stage = materials[material]["Stages"][0]
                if "baseColorMap" in stage:
                    self.assertTrue((assets.OUT / Path(stage["baseColorMap"]).name).is_file())

    def test_mirrored_symbols_and_text_accent_are_shipped(self):
        from PIL import Image, ImageOps
        for a, b in (("fr_a1a", "fr_a1b"), ("fr_a1c", "fr_a1d")):
            np.testing.assert_array_equal(np.asarray(assets.texture(a)), np.asarray(ImageOps.mirror(assets.texture(b))))
        panel = Image.open(assets.OUT / "m9c.color.png")
        self.assertEqual(panel.size, (512, 512))
        self.assertGreater(np.count_nonzero(np.asarray(panel)[60:200, :, 0] < 100), 1000)

    def test_m9c_text_uses_fixed_glyphs_without_a_system_font_renderer(self):
        from PIL import ImageFont
        self.assertEqual(assets.M9C_ROWS, (("CÉDEZ LE", .28), ("PASSAGE", .70)))
        glyphs, _ = assets.font_atlas()
        self.assertGreater(glyphs['É']['bounds'][3], glyphs['E']['bounds'][3])
        expected = np.asarray(assets.texture("m9c"))
        with patch.object(ImageFont, "truetype", side_effect=OSError("font renderer unavailable")):
            actual = np.asarray(assets.texture("m9c"))
        np.testing.assert_array_equal(expected, actual)

    def test_generator_reproduces_shipped_assets_ignoring_newlines(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            assets.build(Path(tmp))
            generated = sorted(Path(tmp).iterdir())
            self.assertEqual(len(generated), 16)  # 7 models, 8 faces and their material file
            for got in generated:
                src = assets.OUT / got.name
                with self.subTest(asset=got.name):
                    self.assertTrue(src.is_file())
                    if src.suffix == ".dae":
                        self.assert_collada_equal(src.read_text(encoding="utf-8"), got.read_text(encoding="utf-8"))
                    elif src.suffix == ".json":
                        self.assertEqual(json.loads(src.read_text(encoding="utf-8")),
                                         json.loads(got.read_text(encoding="utf-8")))
                    elif src.suffix == ".png":
                        # PNG compression/metadata can vary without changing a pixel.
                        with Image.open(src) as a, Image.open(got) as b:
                            np.testing.assert_array_equal(np.asarray(a.convert("RGBA")), np.asarray(b.convert("RGBA")))
                    else:
                        self.fail("Unexpected generated asset: " + got.name)


class RetainedTest(FrenchTest):
    def test_prebuild_reuse_and_skin_rebase_keep_small_paint_polygons(self):
        # Reuse the production worker fixture, with a rural tertiary rather than
        # the earlier kit's urban residential test road.
        from test_regional_geotiles import RegionalGeoTest
        import blockworker as bw
        import cdae
        RegionalGeoTest.setUp(self)
        bw._s.md.feats.attrs["class"] = np.array(["tertiary"])
        bw._s.surface.urban = lambda lat, lon: np.zeros(np.shape(lat), bool)
        pre = self.root / "pre"
        pre.mkdir()
        key = bw.prebuild_geo(self.gx, self.gy, 15, str(pre), True, False, False,
                             False, 0., None, "FR", pole_profile=self.profile)
        self.assertIsNotNone(key)
        out = self.root / "live"
        with patch.object(bw, "_tile_parts", side_effect=AssertionError("must reuse prebuilt content")):
            RegionalGeoTest.content(self, out, self.profile)
        names = bw.geo_names(self.tile.key)
        source = (out / names["inputs"]).read_bytes()
        inputs = pickle.loads(source)
        paint = [c for c in inputs["chunks"] if c.get("poly") and c["m"] == fr.PAINT_MATERIAL]
        self.assertTrue(paint)
        self.assertTrue(all(len(c["poly"]) <= 6 for c in paint))
        with patch.object(bw, "_geo_inputs", {}):
            for aid, lon in ((1, 7.816), (2, 8.02)):
                bw._geo_skin(aid, 49.026, lon, 15, self.gx, self.gy, str(out), "/test", True, 0., 1 / 32,
                             lambda: self.fail("disk inputs should suffice"))
                data = json.loads((out / bw.geo_names(self.tile.key, aid)["json"]).read_text())
                mesh = cdae.read(out / Path(data["road"]["shape"]).name)
                self.assertIn(fr.PAINT_MATERIAL, {m["name"] for m in mesh["materials"]})
        self.assertEqual(source, (out / names["inputs"]).read_bytes())

    def test_startup_flag_separates_persistent_cache_keys(self):
        code = "import sys;sys.path.insert(0,'server');import revisit;print(revisit.code_key())"
        keys = []
        for value in ("off", "on"):
            env = dict(os.environ, BNE_FR_ROADS=value)
            keys.append(subprocess.check_output([sys.executable, "-c", code], cwd=ROOT, env=env, text=True).strip())
        self.assertNotEqual(*keys)


def osm_example(way_id):
    """Real OSM geometry/tags; converted to the same joined feature schema."""
    import railtags
    data = json.loads((Path(__file__).parent / "fixtures/france_road_examples.json").read_text(encoding="utf-8"))
    row = data["ways"][str(way_id)]
    ll = np.asarray(row["lonlat"], float)
    tags = row["tags"]
    f = features([np.zeros((len(ll), 2))], [tags["highway"]], surfaces=[(0, tags.get("surface", "asphalt"))])
    f.lonlat[:] = ll
    f.attrs["src_record"] = np.array(["w" + str(way_id) + "@0"])
    if "lanes" in tags:
        f.attrs["osm_lanes"] = np.array([int(tags["lanes"])])
    for key in railtags.LANE_COLUMNS:
        if key in tags:
            f.attrs[railtags.lane_attr(key)] = np.array([tags[key]])
    return f, tags


class RenderingRegressionTest(FrenchTest):
    def test_d503_real_lane_markings_no_still_gets_guidance_blocks(self):
        for way in (24651289, 24651290, 754913915, 754913916):
            f, raw = osm_example(way)
            self.assertEqual(raw["lane_markings"], "no")
            sp = roads.road_specs(f)[0]
            frame = geodesy.EnuFrame(*f.lonlat[0, ::-1])
            e, n, _ = frame.geodetic_to_enu(f.lonlat[:, 1], f.lonlat[:, 0], 0.)
            out = fr.paint(sp, np.c_[e, n, np.zeros(len(e))], roads.lane_tags(f, 0))
            self.assertTrue(out)
            self.assertEqual({lk["kind"] for lk, _, _ in out}, {"mre"})

    def test_boulevard_real_three_lane_tags_keep_two_short_dash_rows(self):
        f, raw = osm_example(101290249)
        sp = roads.road_specs(f, urban_fn=lambda la, lo: np.ones(np.shape(la), bool))[0]
        self.assertTrue(sp.urban)
        self.assertEqual(sp.lanes, 3)
        pts = np.array([[0., 100, 0], [150., 100, 0]])
        out = fr.paint(sp, pts, roads.lane_tags(f, 0))
        self.assertEqual({lk["kind"] for lk, _, _ in out}, {"centre", "lane"})
        for lk, width, run in out:
            self.assertLessEqual(fr.stations(run)[-1], 1.50001)
            kept = roads.cut_outside(run, [(np.array([[0., 250.], [150., 250.]]), 4.)], min_piece=.02)
            self.assertEqual(len(kept), 1)

    def test_short_urban_dashes_survive_the_complete_multi_road_tile(self):
        frame = geodesy.EnuFrame(49.02127, 7.80967)
        f = french_features([[(-200, 100), (600, 100)], [(-200, 210), (600, 210)]], ["primary", "primary"])
        chunks = roads.build_tile(frame, 0, 0, [f], lambda la, lo: np.zeros(np.shape(la)),
                                  urban_fn=lambda la, lo: np.ones(np.shape(la), bool), country="FR")
        short = [c for c in chunks if c["m"] == fr.PAINT_MATERIAL and len(c["n"]) >= 2
                 and .5 < fr.stations(np.asarray(c["n"]))[-1] < 1.6]
        self.assertGreater(len(short), 50)

    def test_straight_map_splits_do_not_make_holes_in_a_continuous_line(self):
        frame = geodesy.EnuFrame(49.02127, 7.80967)
        f = french_features([[(-200, 100), (128, 100)], [(128, 100), (600, 100)]], ["secondary"] * 2)
        f.attrs["osm_overtaking"] = np.array(["no", "no"])
        chunks = roads.build_tile(frame, 0, 0, [f], lambda la, lo: np.zeros(np.shape(la)), country="FR")
        intervals = sorted((min(q[0] for q in c["n"]), max(q[0] for q in c["n"]))
                           for c in chunks if c["m"] == fr.PAINT_MATERIAL and c["n"])
        self.assertTrue(intervals)
        self.assertTrue(any(a <= 128.001 and b >= 127.999 for a, b in intervals))
        for (_, end), (start, _) in zip(intervals, intervals[1:]):
            self.assertLessEqual(start - end, .005)

    def test_continuations_do_not_remove_crossing_road_blockers(self):
        sp = spec()
        rr = [("a", sp, np.array([[-100., 0, 0], [0., 0, 0]]), 7.8),
              ("b", sp, np.array([[0., 0, 0], [100., 0, 0]]), 7.8),
              ("cross", sp, np.array([[0., 0, 0], [0., 100, 0]]), 7.8)]
        self.assertEqual(fr.continuations(rr), {("a", "b"), ("b", "a")})

    def test_d27_reference_is_mixed_and_handles_reversed_feature_geometry(self):
        f, raw = osm_example(15954261)
        self.assertEqual(raw["old_ref"], "D 27")
        sp = roads.road_specs(f)[0]
        tags = roads.lane_tags(f, 0)
        self.assertEqual(tags["overtaking:forward"], "no")
        frame = geodesy.EnuFrame(*f.lonlat[0, ::-1])
        x, y, _ = frame.geodetic_to_enu(f.lonlat[:, 1], f.lonlat[:, 0], 0.)
        pts = np.c_[x, y, np.zeros(len(x))]
        out = fr.paint(sp, pts, tags)
        self.assertFalse(any(lk["kind"] == "edge" for lk, _, _ in out))
        solid = max(out, key=lambda r: fr.stations(r[2])[-1])[2]
        self.assertGreater(fr.stations(solid)[-1], 3000)
        f.lonlat[:] = f.lonlat[::-1].copy()
        reverse_tags = roads.lane_tags(f, 0)
        self.assertEqual(reverse_tags["overtaking:backward"], "no")
        reverse = fr.paint(sp, pts[::-1], reverse_tags)
        reverse_solid = max(reverse, key=lambda r: fr.stations(r[2])[-1])[2]
        np.testing.assert_allclose(solid[::-1], reverse_solid, atol=1e-8)
        f.attrs["osm_overtaking"] = np.array(["yes"])
        self.assertEqual(roads.lane_tags(f, 0)["overtaking"], "yes")

    @staticmethod
    def heights_at(points, tris, xy):
        a, b, c = points[tris[:, 0]], points[tris[:, 1]], points[tris[:, 2]]
        v, w = b[:, :2] - a[:, :2], c[:, :2] - a[:, :2]
        det = v[:, 0] * w[:, 1] - v[:, 1] * w[:, 0]
        heights = []
        for q in xy:
            d = q - a[:, :2]
            s = (d[:, 0] * w[:, 1] - d[:, 1] * w[:, 0]) / det
            t = (v[:, 0] * d[:, 1] - v[:, 1] * d[:, 0]) / det
            inside = (s >= -1e-8) & (t >= -1e-8) & (s + t <= 1 + 1e-8)
            z = a[:, 2] + s * (b[:, 2] - a[:, 2]) + t * (c[:, 2] - a[:, 2])
            heights.append(np.max(z[inside]) if inside.any() else np.nan)
        return np.array(heights)

    def test_paint_is_above_actual_asphalt_triangles_on_quantized_crossfall(self):
        frame = geodesy.EnuFrame(49.01471, 7.79754)
        def elev(la, lo):
            _, north, _ = frame.geodetic_to_enu(la, lo, 0.)
            return np.maximum(north, 0) * .0105
        chunks = [{"m": material, "w": width, "p": 2, "tl": 1., "n": [[0., 0, 0], [40., 0, 0]]}
                  for material, width in (("road_asphalt_2lane", 8.), (fr.PAINT_MATERIAL, .1))]
        p, t, _, mats, tm = roadmesh.build(frame, chunks, elev, (0., 0., 0.), lift=.025, follow=True, q=1 / 32)
        xy = np.c_[np.linspace(.2, 39.8, 100), np.zeros(100)]
        base = self.heights_at(p, t[tm == mats.index("road_asphalt_2lane")], xy)
        paint = self.heights_at(p, t[tm == mats.index(fr.PAINT_MATERIAL)], xy)
        self.assertTrue(np.isfinite(base).all() and np.isfinite(paint).all())
        # (over the asphalt everywhere, by millimetres: the user, 2026-11-11, "i want to make them virtualy the same
        # height of the road": roadmesh.LIFT mark 2 mm)
        self.assertGreater(float(np.min(paint - base)), .001)

    def test_geometric_paint_is_opaque_and_registered_for_road_tiles(self):
        materials = json.loads((assets.OUT / "main.materials.json").read_text())
        paint = materials[fr.PAINT_MATERIAL]
        self.assertFalse(paint["translucent"])
        self.assertNotIn("opacityMap", paint["Stages"][0])
        lua = (ROOT / "mod/beamng_earth/lua/ge/extensions/beamngEarth/roads.lua").read_text()
        self.assertIn("/fr_traffic/main.materials.json", lua)


if __name__ == "__main__":
    unittest.main()
