"""Winter's bare trees draw the same crown at every detail level (the user, 2026-11-27: "the tree LODs still pop a lot
when rendering leafless trees"; tools/twig_lodcheck.py, texdds.rs coverage_levels, baretrees.lua).

1. The twig cards' opacity maps keep their drawn share at every mip (the manifest's "coverage" derive) at the alphaRef
   their materials use: a mismatch draws the haze the derive was meant to keep (or drops it) again.
2. With the game installed: the stock oak and aspen rendered offline at each switch between their detail levels as the
   GPU draws them; the crown's drawn area across a switch within 25 % (before the derive: 2.5x the oak's, 3x the
   aspen's: thin near crowns that popped to dense dark far ones).
"""
import io
import json
import os
import re
import sys
import tempfile
import unittest
import zipfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import twig_lodcheck  # noqa: E402

BARE = "levels/beamng_earth/art/shapes/beamngEarth/bare/"


class TestTwigCoverage(unittest.TestCase):
    def test_every_twig_opacity_keeps_its_share_at_its_alpha_ref(self):
        rename, _, near, far = twig_lodcheck.bare_table()
        man = json.load(open(os.path.join(ROOT, "mod", "texture_sources", "textures.json"), encoding="utf-8"))["textures"]
        lua = open(twig_lodcheck.BARETREES, encoding="utf-8").read()
        young = re.findall(r"'bne_twig_\w+', '(\w+)'", lua.split("M.YOUNG = ", 1)[1].split("\n", 1)[0])
        texs = set(rename.values()) | set(young)
        self.assertIn("oak_distant", texs)
        self.assertIn("beech_young", texs)
        for tex in sorted(texs):
            with self.subTest(tex=tex):
                e = man.get(BARE + "bne_twig_%s_o.data.dds" % tex)
                self.assertIsNotNone(e, "no opacity map")
                m = re.fullmatch(r"coverage:(\d+)(?::([\d.]+))?", e.get("derive", ""))
                self.assertIsNotNone(m, "no coverage derive: %r" % e.get("derive"))
                self.assertEqual(int(m.group(1)), far if tex.endswith("_distant") else near)


@unittest.skipUnless(twig_lodcheck.game_dir(), "BeamNG.drive not installed (the stock tree shapes)")
class TestTwigLods(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        zpath = os.path.join(twig_lodcheck.game_dir(), "content", "levels", "east_coast_usa.zip")
        base = "levels/east_coast_usa/art/shapes/trees/"
        with zipfile.ZipFile(zpath) as z:
            for f in ("trees_oak/tree_oak_large_a.dae", "trees_oak/main.materials.json",
                      "trees_aspen/tree_aspen_small_a.dae", "trees_aspen/main.materials.json"):
                out = os.path.join(cls.tmp.name, f)
                os.makedirs(os.path.dirname(out), exist_ok=True)
                with open(out, "wb") as fh:
                    fh.write(z.read(base + f))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def ratios(self, shape, plain):
        rows = twig_lodcheck.check(os.path.join(self.tmp.name, shape), plain=plain, azims=(0.0,))
        return {(sz, nxt): st[1][0] / max(st[0][0], 1) for sz, nxt, _, st in rows if nxt}

    def test_crowns_match_across_switches(self):
        for shape in ("trees_oak/tree_oak_large_a.dae", "trees_aspen/tree_aspen_small_a.dae"):
            with self.subTest(shape=shape):
                for sw, r in self.ratios(shape, False).items():
                    self.assertTrue(0.8 <= r <= 1.25, "%s %s: drawn area x%.2f across the switch" % (shape, sw, r))

    def test_plain_mips_popped(self):
        # (the check sees the old pop: the near cards' twigs faded under alphaRef 100, the far cards dense at 50)
        r = self.ratios("trees_oak/tree_oak_large_a.dae", True)
        self.assertGreater(r[(600, 200)], 1.8)


if __name__ == "__main__":
    unittest.main()
