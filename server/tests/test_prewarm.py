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
        for m in (*buildings.MATERIALS, *sidewalks.MATERIALS, "line_yellow", "line_white", "crossing_white",
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

    def test_the_strips_vertex_format_has_its_own_shape(self):
        """A road mesh with a dirt or gravel strip has the second uv (roadmesh.write): prewarm_x.cdae sets up its
        materials in that vertex format; with BNE_DIRT_STRIPS off it is not written."""
        for m in roads.DIRT_MATERIALS:
            self.assertIn(m, prewarm.materials())
        rm = prewarm.road_materials()
        for m in (*roads.DIRT_MATERIALS, "line_white", "line_yellow", "road_asphalt_2lane"):
            self.assertIn(m, rm)
        was = roads.DIRT_STRIPS
        try:
            with tempfile.TemporaryDirectory() as d:
                roads.DIRT_STRIPS = True
                prewarm.write(d, "/temp/x")
                shape = cdae.decode(Path(d, prewarm.NAME_X).read_bytes())
                self.assertEqual([m["name"] for m in shape["materials"]], rm)
                a = cdae.mesh_arrays(shape["meshes"][0])
                self.assertEqual(len(a["tverts2"]), len(a["verts"]))
                roads.DIRT_STRIPS = False
                prewarm.write(d, "/temp/x")
                self.assertFalse(Path(d, prewarm.NAME_X).exists())
        finally:
            roads.DIRT_STRIPS = was

    def test_a_dirt_road_tiles_materials_are_covered(self):
        import blockworker
        f = features([[(-300, 100), (300, 100)], [(120, -100), (120, 300)]], ["track", "residential"],
                     surfaces=[(1, "gravel")])
        keys = roads.cells_of_tile(FRAME, 0, 0)
        blockworker._s = SimpleNamespace(md=FakeMapData(f, keys[0]), dem=FakeDem(), widths=None, surface=None,
                                         airports=None, sea=None, lock=threading.Lock(), bridges={},
                                         anchor=lambda aid, lat, lon: (None, None), cache_dir=tmp_cache(self))
        rm = set(prewarm.road_materials())
        seen = False
        with tempfile.TemporaryDirectory() as d:
            blockworker.roads_tile(1, LAT, LON, 15, 0, 0, d, "/temp/x", True, 0.05)
            for p in glob.glob(os.path.join(d, "*.cdae")):
                shape = cdae.decode(Path(p).read_bytes())
                if not len(cdae.mesh_arrays(shape["meshes"][0])["tverts2"]):
                    continue
                seen = True
                self.assertLessEqual({m["name"] for m in shape["materials"]}, rm, os.path.basename(p))
        self.assertTrue(seen, "the road mesh has the strips' second uv")



class RailMaterialsTest(unittest.TestCase):
    """The 3D tracks' materials (railways.py names them as plain strings) are prewarmed: the first rail tile of a
    session instanced them mid-drive (a 104 ms import at 120 mph, 2026-11-04)."""

    def test_rail_materials_prewarmed(self):
        import prewarm
        import railways
        got = set(prewarm.materials())
        for m in railways.BALLAST_WORN + railways.STEEL_WORN + railways.SLEEPER_WORN + (railways.CONCRETE,):
            self.assertIn(m, got)

    def test_no_looks(self):
        import prewarm
        got = set(prewarm.materials())
        self.assertNotIn("bne_bld_fac_blue", got)       # (a look: FACADE with a tint, no material of its own)
        self.assertNotIn("bne_precast", got)


if __name__ == "__main__":
    unittest.main()
