"""prewarm.py: the shape the mod loads at hello lists every material the server's shapes use."""
import glob
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import buildings  # noqa: E402
import cdae  # noqa: E402
import exitsigns
import mergesigns
import milemarkers
import prewarm  # noqa: E402
import roadmesh  # noqa: E402
import roads  # noqa: E402
import sidewalks  # noqa: E402
from test_blockworker import FakeDem, FakeMapData, tmp_cache  # noqa: E402
from test_roads import LAT, LON, FRAME, features  # noqa: E402


class PrewarmTest(unittest.TestCase):
    def test_lists_every_material(self):
        mats = prewarm.materials()
        self.assertEqual(mats, sorted(set(mats)))
        for m in (*buildings.MATERIALS, *sidewalks.MATERIALS, *exitsigns.GUIDE_MATERIALS,
                  *milemarkers.MATERIALS, *mergesigns.MATERIALS, "line_yellow", "line_white", "crossing_white",
                  "road_asphalt_2lane", roadmesh.TUNNEL_STONE, roadmesh.DECK_SIDE, "bne_wire", "line_runway_white"):
            self.assertIn(m, mats)
        with tempfile.TemporaryDirectory() as d:
            vfs = prewarm.write(d, "/temp/x")
            self.assertEqual(vfs, "/temp/x/prewarm.cdae")
            shape = cdae.decode(Path(d, "prewarm.cdae").read_bytes())
            self.assertEqual([m["name"] for m in shape["materials"]], mats, "a triangle each: none left out")

    def test_a_road_tiles_shapes_are_covered(self):
        import blockworker
        f = features([[(-300, 100), (300, 100)], [(120, -100), (120, 300)]], ["secondary", "residential"])
        keys = roads.cells_of_tile(FRAME, 0, 0)
        blockworker._s = SimpleNamespace(md=FakeMapData(f, keys[0]), dem=FakeDem(), widths=None, surface=None,
                                         airports=None, sea=None, lock=threading.Lock(), bridges={},
                                         anchor=lambda aid, lat, lon: (None, None), cache_dir=tmp_cache(self))
        mats = set(prewarm.materials())
        with tempfile.TemporaryDirectory() as d:
            blockworker.roads_tile(1, LAT, LON, 15, 0, 0, d, "/temp/x", True, 0.05)
            shapes = glob.glob(os.path.join(d, "*.cdae"))
            self.assertTrue(shapes)
            for p in shapes:
                used = {m["name"] for m in cdae.decode(Path(p).read_bytes())["materials"]}
                self.assertLessEqual(used, mats, os.path.basename(p))


if __name__ == "__main__":
    unittest.main()
