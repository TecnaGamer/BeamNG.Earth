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
        mapillarypoles._tile.cache_clear()
        self.addCleanup(mapillarypoles._tile.cache_clear)
        with patch.object(mapillarypoles, "token", return_value="dummy"), patch.object(
                mapillarypoles.netfetch, "default_fetcher", return_value=fetcher):
            expected = ((mapcells._lon_of(cell.x + .5, 16384), mapcells._lat_of(cell.y + .25, 16384)),)
            self.assertEqual(mapillarypoles._tile(cell), expected)
            self.assertEqual(mapillarypoles._tile(cell), expected)
            fetcher.get.assert_called_once()
            self.assertNotIn("dummy", fetcher.get.call_args.args[0])
            self.assertEqual(fetcher.get.call_args.kwargs["headers"]["Authorization"], "OAuth dummy")
