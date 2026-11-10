"""Final PR #505 additions alongside the 1.9.1 tile and bridge pipelines."""
import json
import gzip
import tempfile
import zipfile
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).parent)]
import fences
import radars
import test_fences
import test_regional_geotiles as regional_geo


class IntegratedTileTest(unittest.TestCase):
    def test_frontages_and_radar_rows_survive_geotile_retention_and_skin(self):
        geo = regional_geo.RegionalGeoTest()
        geo.setUp()
        self.addCleanup(geo.doCleanups)
        gate = ["test-camera", 0., 0., 0., 0., 3., 50., 0., 0., 2.]
        with patch.dict(os.environ, {"BNE_EU_ROAD_DETAILS": "on"}), \
             patch.object(radars, "tile_rows", return_value=([], [gate])):
            out = geo.root / "combined"
            import poleprofiles
            geo.content(out, poleprofiles.geographic_profile("europe"))
            import blockworker as bw
            data = json.loads((out / bw.geo_names(geo.tile.key)["json"]).read_text())
            self.assertEqual(data["speedTraps"], [gate])
            bw._geo_skin(2, 49.026, 8.02, 15, geo.gx, geo.gy, str(out), "/test",
                         True, 0., 1 / 32, lambda: self.fail("must use retained content"))
            self.assertEqual(json.loads((out / bw.geo_names(geo.tile.key)["json"]).read_text())["speedTraps"], [gate])

    def test_french_fence_keeps_official_bridge_deck_height(self):
        case = test_fences.PlaceTest()
        case.setUp()
        feats = case.line("fence", {"material": "wood", "layer": "1"}, [[10., 20.], [90., 20.]])
        with patch("house_regions.profile", return_value="central"):
            rows = fences.rows(case.frame, lambda key: feats, (0., 0., 100., 100.),
                               lambda la, lo: np.full(np.shape(la), 5.), decks=case.DECK, french=True)
        self.assertTrue(rows)
        self.assertTrue(all(row[0] == "fr_fence_wood" and abs(row[3] - 13.) < .02 for row in rows))


class RegionalConversionTest(unittest.TestCase):
    def test_split_cells_keep_archive_order_and_match_existing_gzip_format(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
        import build_regional_data as build
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archives = []
            for i, cells in enumerate(({(1, 2): [{"id": 1}], (3, 4): [{"id": 2}]},
                                       {(1, 2): [{"id": 3}]})):
                name = f"part{i}.zip"
                archives.append(name)
                with zipfile.ZipFile(root / name, "w") as z:
                    z.writestr("index.json", json.dumps({"cell": 4, "cells": list(cells), "format": 1}))
                    for (x, y), rows in cells.items():
                        z.writestr(f"{x}_{y}.json", json.dumps(rows))
            info = build.stream_cells(str(root), archives, str(root / "out"), "fr_priority")
            self.assertEqual(info, {"cell": 4, "cells": [[1, 2], [3, 4]], "meta": {"format": 1}})
            payload = (root / "out/fr_priority/1_2.json.gz").read_bytes()
            self.assertEqual(payload, build._gz([{"id": 1}, {"id": 3}]))
            self.assertEqual(json.loads(gzip.decompress(payload)), [{"id": 1}, {"id": 3}])
