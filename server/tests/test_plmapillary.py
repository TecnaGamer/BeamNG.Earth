"""Local coverage, caching, and the connection between an original SVG and a streamed sign."""
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "server"), str(ROOT / "tools")]
import geodesy
import mapcells
import plmapillary as api
import plsignart
import plsigns


def feature(ident, lon, lat, kind):
    return {"id": ident, "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "object_type": "trafficsign", "object_value": kind}


class PolishMapillary(unittest.TestCase):
    def test_game_profile_token_and_explicit_rules_only_override(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, BNGE_USER_DIR=tmp,
                MAPILLARY_ACCESS_TOKEN="legacy-test"):
            path = Path(tmp, "settings", "beamngearth-mapillary.json")
            self.addCleanup(api.token.cache_clear)
            api.token.cache_clear()
            self.assertEqual(api.token(), "legacy-test")
            for data, expected in [({"token":"game-test"}, "game-test"), ({"token":""}, ""), ({}, "")]:
                api.atomic_write(path, json.dumps(data).encode())
                api.token.cache_clear()
                self.assertEqual(api.token(), expected)
    def test_current_tile_only_and_offline_revisit(self):
        cell = mapcells.cell_of(53.69, 17.56)
        lat, lon = cell.center()
        known = feature("1", lon, lat, "regulatory--priority-road--g1")
        ambiguous = feature("2", lon, lat, "regulatory--weight-limit--g4")
        east = feature("3", lon+2, lat, "regulatory--priority-road--g1")
        calls = []
        def get(url, headers, max_bytes):
            calls.append(url)
            self.assertNotIn("private-test-token", url)
            self.assertEqual(headers["Authorization"], "OAuth private-test-token")
            params = parse_qs(urlparse(url).query)
            np.testing.assert_allclose([float(v) for v in params["bbox"][0].split(",")], cell.bounds(), atol=1e-7)
            page = ({"data": [known, east], "paging": {"next": "https://unused.example/", "cursors": {"after": "page2"}}}
                    if "after" not in params else {"data": [known, ambiguous, None]})
            return SimpleNamespace(body=json.dumps(page).encode())
        fetcher = SimpleNamespace(get=get, offline_flag=False)
        box = (-20., -20., 20., 20.)
        def render(la, lo):
            return plsigns.tile_rows(geodesy.EnuFrame(la, lo), box, [], lambda la, lo: np.zeros_like(la), tmp, fetcher)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(api, "token", return_value="private-test-token"), \
                mock.patch.object(plsigns, "index", return_value={}), mock.patch.object(plsigns, "ON", True):
            rows = render(lat, lon)
            self.assertEqual(len(rows), 1)
            self.assertEqual(len(calls), 2)
            self.assertEqual(rows, render(lat, lon))
            self.assertEqual(render(52.52, 13.4), [])
            self.assertEqual(render(52., 180.), [])
            fetcher.offline_flag = True
            with mock.patch.object(api, "epoch", return_value="next-month"):
                self.assertEqual(rows, render(lat, lon))
            with mock.patch.object(plsigns, "ON", False):
                self.assertEqual(render(lat, lon), [])
            self.assertEqual(len(calls), 2)

    def test_verified_extra_model_is_available_after_preparation(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            user = Path(tmp)
            data = user / "data"
            data.mkdir()
            real = json.loads((api.DATA / "commons.json").read_bytes())["D-1"]
            (data / "commons.json").write_text(json.dumps({"D-1": real}))
            (data / "catalog.json").write_text("[]")
            (data / "mapillary.json").write_text("[]")
            originals = user / "originals"
            originals.mkdir()
            raw = (ROOT / "tools/sign_art/pl/D-1.svg").read_bytes()
            source = originals / "PL_road_sign_D-1.svg"
            source.write_bytes(raw)
            with mock.patch.object(api, "DATA", data), mock.patch.object(plsigns, "DATA", data), \
                    mock.patch.object(api, "classes", return_value={}), mock.patch.dict(os.environ, BNGE_USER_DIR=tmp), \
                    mock.patch.object(plsignart, "download", side_effect=AssertionError):
                self.addCleanup(plsigns.supported_codes.cache_clear)
                plsigns.supported_codes.cache_clear()
                self.assertNotIn("D-1", plsigns.supported_codes())
                plsignart.prepare(user, originals)
                root = plsignart.folder(user)
                self.assertEqual(raw, (root / "D-1.svg").read_bytes())
                self.assertTrue((root / "pl_d_1_plate.dae").is_file())
                with Image.open(root / "pl_d_1_face_256.png") as face:
                    self.assertEqual(face.size, (256, 256))
                self.assertIn("pl_d_1", json.loads((root / "extra.materials.json").read_bytes()))
                (root / "extra.materials.json").write_text("{}")
                plsignart.prepare(user, originals)
                stages = json.loads((root / "extra.materials.json").read_bytes())["pl_d_1"]["Stages"]
                self.assertEqual(stages[0]["retroreflectivity"], 0.35)
                plsigns.supported_codes.cache_clear()
                self.assertIn("D-1", plsigns.supported_codes())
