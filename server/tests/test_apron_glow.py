"""tools/build_apron_glow.py: the pool of light on the ground under each apron mast (shape bne_apron_pool.dae, its opacity map, its
material), as the mod ships them: a flat soft-edged quad in the engine's detail levels (a speck near, the quad from NEAR_M out),
a translucent night-lit material, the opacity map made at install (textures.json)."""
import json
import os
import re
import sys
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..", "..")
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "server"))
import build_apron_glow as bag  # noqa: E402
import build_lamp_glow as lg  # noqa: E402

LEVEL = os.path.join(ROOT, "mod", "beamng_earth", "levels", "beamng_earth", "art", "shapes", "beamngEarth")


class OpacityMap(unittest.TestCase):
    def test_a_circular_gradient_clear_at_the_edge_and_in_the_corners(self):
        a = bag.opacity_map().astype(int)
        n = a.shape[0]
        self.assertEqual(a.shape, (n, n))
        self.assertEqual(a[n // 2 - 1:n // 2 + 1, n // 2 - 1:n // 2 + 1].min(), 255)    # (opaque at the middle)
        self.assertEqual(a[0, :].max(), 0)                                               # (clear along the edges: the pools
        self.assertEqual(a[:, -1].max(), 0)                                              # overlap without a seam)
        self.assertEqual(a[0, 0] + a[-1, -1] + a[0, -1] + a[-1, 0], 0)
        row = a[n // 2, n // 2:]
        self.assertTrue((np.diff(row) <= 0).all())                                       # (falls outward, never rises)
        self.assertGreater(a[n // 2, n // 2 + n // 8], 200)                              # (flat over the middle, then soft)
        self.assertTrue(0 < a[n // 2, n // 2 + n // 4] < 200)
        self.assertTrue((a == a[::-1]).all() and (a == a[:, ::-1]).all() and (a == a.T).all())      # (round)

    def test_the_manifest_makes_it_at_install_from_a_source_that_is_there(self):
        with open(os.path.join(ROOT, "mod", "texture_sources", "textures.json")) as f:
            tex = json.load(f)["textures"]
        dds = "levels/beamng_earth/art/shapes/beamngEarth/" + bag.TEXTURE + ".dds"
        self.assertEqual(tex[dds]["format"], "BC4")                                      # (an opacity map: one channel)
        self.assertTrue(os.path.exists(os.path.join(ROOT, "mod", "texture_sources", tex[dds]["source"])))
        self.assertFalse(os.path.exists(os.path.join(LEVEL, bag.TEXTURE + ".png")))     # (no PNG left in the level)
        self.assertTrue(bag.TEXTURE_VFS.endswith(dds))


class Shape(unittest.TestCase):
    def test_the_shipped_shape_is_what_the_builder_makes(self):
        with open(os.path.join(LEVEL, bag.SHAPE), newline="") as f:
            self.assertEqual(f.read(), bag.shape_xml())

    def test_a_speck_near_then_the_flat_quad(self):
        xml = bag.shape_xml()
        sizes = [int(m) for m in re.findall(r'<node id="pool(\d+)"', xml)]
        self.assertEqual(len(sizes), 2)
        self.assertGreater(sizes[0], sizes[1])                                           # (the engine's detail sizes, falling)
        self.assertEqual(sizes[1], int(lg.LAST_PX))
        # the speck is shown out to NEAR_M: the quad's shape radius over that distance, the engine's size there
        self.assertAlmostEqual(sizes[0], bag.HALF_M * 2 ** 0.5 * lg.ENGINE_PX / bag.NEAR_M, delta=1)
        quad = [float(v) for v in re.findall(r'id="g1-pos-a"[^>]*>([^<]*)<', xml)[0].split()]
        pts = np.array(quad).reshape(-1, 3)
        self.assertEqual(len(pts), 4)
        self.assertTrue((pts[:, 2] == 0).all())                                           # (flat, at the origin's height)
        self.assertEqual(sorted(set(np.abs(pts[:, :2]).ravel().tolist())), [bag.HALF_M])
        self.assertEqual(xml.count('target="#bne_apron_pool"'), 2)                       # (one material)

    def test_pools_of_masts_90_m_apart_overlap(self):
        import airsideprops
        self.assertGreater(2 * bag.HALF_M, airsideprops.MAST_SPACING_M * 1.5)


class Material(unittest.TestCase):
    def test_translucent_lit_by_its_emissive_only(self):
        m = bag.material()
        self.assertEqual(m["name"], bag.MATERIAL)
        self.assertTrue(m["translucent"])
        self.assertFalse(m["translucentZWrite"])
        self.assertEqual(m["Stages"][0]["opacityMap"], bag.TEXTURE_VFS)
        self.assertEqual(m["Stages"][0]["baseColorFactor"][:3], [0.0, 0.0, 0.0])         # (the lights and the sky add nothing)
        self.assertEqual(m["Stages"][0]["emissiveFactor"], [0, 0, 0])                    # (dark by day: nightlights.lua lights it)
        self.assertLessEqual(bag.OPACITY, 0.7)                                           # (a wash, not a floor lamp)

    def test_the_shipped_materials_file_has_it(self):
        with open(os.path.join(LEVEL, "lamps.materials.json")) as f:
            mats = json.load(f)
        self.assertEqual(mats[bag.MATERIAL], bag.material())
        self.assertIn("bne_lamp_lens", mats)                                              # (the others kept)


if __name__ == "__main__":
    unittest.main()
