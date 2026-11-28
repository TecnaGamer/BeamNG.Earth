import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mapcells
import mapillarypoles
from test_pmtiles import mvt


class MapillaryPolesTest(unittest.TestCase):
    def test_saved_token_survives_restart_and_can_be_removed(self):
        self.addCleanup(mapillarypoles.token.cache_clear)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, BNGE_USER_DIR=directory,
                                                                     MAPILLARY_ACCESS_TOKEN="env-token"):
            folder = Path(directory) / "settings"
            folder.mkdir()
            path = folder / "beamngearth-mapillary.json"
            for value, expected in [(None, "env-token"), ("test-client", "test-client"), ("", "")]:
                if value is not None:
                    path.write_text(json.dumps({"token": value}))
                mapillarypoles.token.cache_clear()
                self.assertEqual(mapillarypoles.token(), expected)

    def test_feature_filter_coordinates_and_cache(self):
        cell = mapcells.cell_of(52.23, 21.01)
        body = mvt({"point": [(1, [[(2048, 1024)]], {"value": "object--support--utility-pole"}),
                              (1, [[(3000, 2000)]], {"value": "object--street-light"})]})
        fetcher = Mock()
        fetcher.get.return_value.body = body
        with patch.object(mapillarypoles, "token", return_value="dummy"), patch.object(
                mapillarypoles.netfetch, "default_fetcher", return_value=fetcher):
            expected = ((mapcells._lon_of(cell.x + .5, 16384), mapcells._lat_of(cell.y + .25, 16384)),)
            self.assertEqual(mapillarypoles._tile(cell), expected)
            fetcher.get.assert_called_once()
            self.assertNotIn("dummy", fetcher.get.call_args.args[0])
            self.assertEqual(fetcher.get.call_args.kwargs["headers"]["Authorization"], "OAuth dummy")

    def test_point_content_is_pinned_for_both_key_and_build(self):
        extent = (52.2, 21.0, 52.2001, 21.0001)
        a, b = (21.00002, 52.20002), (21.00003, 52.20003)
        self.assertEqual(len(mapcells.cells_for_bbox(*extent, z=14)), 1)
        with patch.object(mapillarypoles, "_cell_points", side_effect=[(a,), (b,), None]) as read:
            with mapillarypoles.build_inputs():
                first = mapillarypoles.signature(extent)
                with mapillarypoles.build_inputs():
                    self.assertEqual(mapillarypoles.points(extent), [a])
            with mapillarypoles.build_inputs():
                second = mapillarypoles.signature(extent)
                self.assertNotEqual(first, second)
                self.assertEqual(mapillarypoles.points(extent), [b])
            missing = mapillarypoles.signature(extent)
            self.assertFalse(missing[1])
            self.assertFalse(read.call_args.args[1])  # Main-process probe is disk-only.
            self.assertEqual(read.call_count, 3)

    def test_shared_disk_probe_and_expiry(self):
        extent = (52.2, 21.0, 52.2001, 21.0001)
        cell = mapcells.cells_for_bbox(*extent, z=14)[0]
        with tempfile.TemporaryDirectory() as folder, patch.object(mapillarypoles.netfetch, "SHARED_DIR", folder), patch.object(
                mapillarypoles, "_tile", side_effect=[((21., 52.2),), ()]) as download:
            self.assertFalse(mapillarypoles.signature(extent)[1])
            download.assert_not_called()
            with mapillarypoles.build_inputs():
                key = mapillarypoles.signature(extent)
            self.assertTrue(key[1])
            self.assertEqual(key, mapillarypoles.signature(extent))
            path = Path(folder) / "mapillary-poles-v1" / str(cell.x) / f"{cell.y}.bin"
            os.utime(path, (1, 1))
            with mapillarypoles.build_inputs():
                changed = mapillarypoles.signature(extent)
                self.assertNotEqual(key, changed)
                self.assertTrue(changed[1])
            self.assertEqual(download.call_count, 2)
