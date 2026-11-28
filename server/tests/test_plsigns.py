"""Polish sign import, Forest placement and the shipped sign catalogue."""
import hashlib
import json
from pathlib import Path
import re
import sys
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "tools"))
import experimental
import geodesy
import guardrails
import import_pl_signs as importer
import plsigns


class PolishSigns(unittest.TestCase):
    def test_date_line_queries_skip_sign_data_and_road_cells(self):
        import plstock
        box = (-256., -256., 256., 256.)
        with mock.patch.object(plsigns, "ON", True), \
                mock.patch.object(plsigns.plmapillary, "token", return_value="test"), \
                mock.patch.object(plsigns, "index", side_effect=AssertionError("foreign sign lookup")), \
                mock.patch("mapcells.cells_for_bbox", side_effect=AssertionError("foreign road lookup")):
            for lon in (-180., -179.999, 179.999, 180.):
                frame = geodesy.EnuFrame(52., lon)
                self.assertFalse(plsigns.covers(frame, box))
                self.assertEqual(plsigns.tile_rows(frame, box, [], None), [])
                self.assertEqual(plstock.sign_stream(None, frame, box, None, object(), False, False).rows, [])

    def test_originals_and_models_agree(self):
        art = ROOT / "tools/sign_art/pl"
        assets = ROOT / "mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/pl_signs"
        sources = json.loads((art / "sources.json").read_text(encoding="utf8"))
        materials = json.loads((assets / "pl_signs.materials.json").read_text(encoding="utf8"))
        codes = json.loads((plsigns.DATA / "catalog.json").read_text())
        self.assertEqual(set(codes), set(sources))
        lua = (ROOT / "mod/beamng_earth/lua/ge/extensions/beamngEarth/trees.lua").read_text(encoding="utf8")
        groups = re.search(r"local PL_SIGNS = \{(.*?)\}", lua, re.S)[1]
        self.assertEqual(set(re.findall(r"'(pl_\w+)'", groups)), set(plsigns.catalog()))
        for code in codes:
            with self.subTest(code=code):
                raw = (art / (code + ".svg")).read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(), sources[code]["sha256"])
                g = plsigns.group(code)
                self.assertEqual(materials[g]["Stages"][0]["retroreflectivity"], 0.35)
                model = ET.parse(assets / (g + "_plate.dae"))
                triangles = model.findall(".//{*}triangles")
                self.assertEqual({t.get("material") for t in triangles}, {g+"-mat", "bne_cable_steel-mat"})
                texture = ROOT / "mod/beamng_earth" / materials[g]["Stages"][0]["baseColorMap"].lstrip("/")
                with Image.open(texture) as face:
                    self.assertEqual(face.format, "PNG")
                    self.assertEqual(face.mode, "RGBA")
                    self.assertEqual(face.size, (256, 256))
                    self.assertEqual(face.getchannel("A").getextrema(), (0, 255))
                    mesh = model.find(".//{*}mesh")
                    outline = mesh.find(f"{{*}}triangles[@material='{g}-mat']")
                    indices = np.fromstring(outline.find("{*}p").text, sep=" ", dtype=int).reshape(-1, 3)
                    uv_id = outline.find("{*}input[@semantic='TEXCOORD']").get("source")[1:]
                    uv = np.fromstring(mesh.find(f"{{*}}source[@id='{uv_id}']/{{*}}float_array").text, sep=" ").reshape(-1, 2)
                    pixels = uv * [face.width, -face.height] + [0, face.height]
                    mask = Image.new("L", face.size)
                    draw = ImageDraw.Draw(mask)
                    for tri in indices:
                        draw.polygon([tuple(p) for p in pixels[tri]], fill=255)
                    filled = np.asarray(mask) > 0
                    opaque = np.asarray(face.getchannel("A")) > 127
                    self.assertGreater((filled & opaque).sum() / (filled | opaque).sum(), 0.987)

    def test_exact_variants(self):
        self.assertEqual(importer.osm_code({"traffic_sign": "stop"}), "B-20")
        self.assertEqual(importer.osm_code({"traffic_sign": "give_way"}), "A-7")
        self.assertEqual(importer.osm_code({"traffic_sign": "PL:B-33[20]"}), "B-33-20")
        self.assertEqual(importer.osm_code({"traffic_sign": "PL:B-18[3.5]"}), "B-18-3.5t")
        self.assertNotIn(importer.osm_code({"traffic_sign": "PL:B-33[25]"}), ("B-33-20", "B-33-30"))
        self.assertIsNone(importer.mapillary_code("regulatory--maximum-speed-limit-20-mph--g1"))
        self.assertEqual(importer.mapillary_code("regulatory--maximum-speed-limit-20--g1"), "B-33-20")
        self.assertEqual(importer.bearing("WNW"), 292.5)
        self.assertIsNone(importer.bearing("backward"))

    def test_switch_and_tile_ownership(self):
        self.assertEqual(experimental.env_for({}), {})
        self.assertEqual(experimental.env_for({"version": 3, "roadSigns": True}), {"BNE_ROADSIGNS": "on"})
        self.assertEqual(guardrails.groups_for([[0]]), guardrails.STOCK_GROUPS)
        self.assertEqual(guardrails.groups_for([[len(guardrails.STOCK_GROUPS)]]), guardrails.ALL_GROUPS)
        frame = geodesy.EnuFrame(52., 20.)
        la, lo, _ = frame.enu_to_geodetic(2., 5., 0.)
        records = {plsigns.cell(lo, la): [(lo, la, "pl_b_20", 180., None)]}
        road = mock.Mock(width=6., oneway=False)
        roads = [("way", road, [[0., -50.], [0., 50.]])]
        ground = lambda lat, lon: np.full_like(lat, 112.)
        with mock.patch.object(plsigns.plmapillary, "token", return_value="test"), \
                mock.patch.object(plsigns, "index", return_value=records), mock.patch.object(plsigns, "ON", True):
            tiles = [plsigns.tile_rows(frame, box, roads, ground)
                     for box in ((-20., -20., 3., 20.), (3., -20., 20., 20.))]
            self.assertEqual([len(t) for t in tiles], [0, 1])
            row = tiles[1][0]
            self.assertEqual(guardrails.groups_for([row])[row[0]], "pl_b_20")
            self.assertAlmostEqual(row[1], 3.45, places=2)
            self.assertAlmostEqual(row[3], 112., places=2)
            np.testing.assert_allclose(row[8:14], [1., 0., 0., 0., 1., 0.], atol=1e-5)
        with mock.patch.object(plsigns, "ON", False), mock.patch.object(plsigns, "index", side_effect=AssertionError):
            self.assertEqual(plsigns.tile_rows(frame, (0., 0., 10., 10.), roads, ground), [])

    def test_export_is_wgs84_and_has_supported_faces(self):
        groups = set(plsigns.catalog())
        confirmed = set(json.loads((plsigns.DATA / "commons.json").read_bytes())) | set(plsigns.supported_codes())
        for source in plsigns.SOURCES:
            records = json.loads((plsigns.DATA / (source + ".json")).read_text())
            self.assertEqual(len(records), len({r[0] for r in records}))
            for _, lon, lat, code, direction in records:
                self.assertTrue(14. < lon < 25. and 48.9 < lat < 55.1)
                if source == "mapillary":
                    self.assertIn(code, confirmed)
                else:
                    self.assertIn(plsigns.group(code), groups)
                self.assertTrue(direction is None or 0 <= direction < 360)

    def test_road_shoulder_across_the_nysa_is_rejected(self):
        frame = geodesy.EnuFrame(51.1498896540787, 15.00094137)
        la, lo, _ = frame.enu_to_geodetic(2., 0., 0.)
        self.assertTrue(plsigns.inside_poland(lo, la))
        de, dx, _ = frame.enu_to_geodetic(-4., 0., 0.)
        self.assertFalse(plsigns.inside_poland(dx, de))
        records = {plsigns.cell(lo, la): [(lo, la, "pl_d_1", 180., None)]}
        road = mock.Mock(width=18., oneway=False)
        with mock.patch.object(plsigns.plmapillary, "token", return_value="test"), \
                mock.patch.object(plsigns, "ON", True), mock.patch.object(plsigns, "index", return_value=records):
            rows = plsigns.tile_rows(frame, (-25., -25., 25., 25.), [("way", road, [[5., -20.], [5., 20.]])],
                                     lambda *_: self.fail("A foreign shoulder must never sample terrain"))
            self.assertEqual(rows, [])
