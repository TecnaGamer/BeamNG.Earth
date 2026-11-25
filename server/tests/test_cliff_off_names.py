"""Cliffs off (BNE_CLIFFS unset, the default) adds no per-tile work (PR #1412 review R1): no cliff names in a road
tile's or geotile's shape files (the native staging stats every name it is given), none in an anchor's skin names, no
cliff mesh revisions in a far tile's files. With cliffs on, they are there."""
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import blockworker  # noqa: E402
import cliffsettings  # noqa: E402
import roads  # noqa: E402

OFF = cliffsettings.CliffSettings()
ON = cliffsettings.CliffSettings(True, True)


def _cliffy(names):
    return sorted(k for k in names if k.startswith("cliff") or "cliff" in str(names[k]))


class CliffOffNamesTest(unittest.TestCase):
    def test_road_tile_shape_files(self):
        with mock.patch.object(cliffsettings, "SETTINGS", OFF):
            self.assertEqual(_cliffy(roads.shape_files(3, 1, 2)), [])
        with mock.patch.object(cliffsettings, "SETTINGS", cliffsettings.CliffSettings(False, False, "high")):
            self.assertEqual(_cliffy(roads.shape_files(3, 1, 2)), [])
        with mock.patch.object(cliffsettings, "SETTINGS", ON):
            self.assertIn("cliffCol1", roads.shape_files(3, 1, 2))

    def test_geotile_names(self):
        with mock.patch.object(cliffsettings, "SETTINGS", OFF):
            self.assertEqual(_cliffy(blockworker.geo_names("g17_1_2")), [])
            self.assertEqual(sorted(blockworker.geo_names("g17_1_2", 5)), ["json", "road", "walk", "walkcol"])
        with mock.patch.object(cliffsettings, "SETTINGS", ON):
            self.assertIn("cliff", blockworker.geo_names("g17_1_2"))
            self.assertIn("cliffStrip1", blockworker.geo_names("g17_1_2", 5))

    def test_far_tile_files(self):
        import earthd
        s = types.SimpleNamespace(out_dir="/o", cdae_dir="/c")
        with mock.patch.object(cliffsettings, "SETTINGS", OFF):
            files = earthd.Server.tile_files(s, "far_1_k.cdae", "far_1_k_r2.dds", 2)
        self.assertEqual([f for f in files if f.endswith(".cdae") and "_r" in os.path.basename(f)], [])
        with mock.patch.object(cliffsettings, "SETTINGS", ON):
            files = earthd.Server.tile_files(s, "far_1_k.cdae", "far_1_k_r2.dds", 2)
        self.assertIn("/o/far_1_k_r2.cdae", files)


if __name__ == "__main__":
    unittest.main()
