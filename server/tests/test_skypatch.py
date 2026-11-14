"""skypatch.py: the sky's haze from above the atmosphere, patched from the game's own shader into the installed mod."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import install  # noqa: E402
import skypatch  # noqa: E402

SHADER = """// before
float4 Other() { return float4(0.0, 0.0, 0.0, 1.0); }
float4 RenderCameraVolumePS(GeometryOutput Input)
{
\tif (viewHeight >= Atmosphere.TopRadius)
\t{
\t\tif (!MoveToTopAtmosphere(WorldPos, WorldDir, Atmosphere.TopRadius))
\t\t{
\t\t\treturn float4(0.0, 0.0, 0.0, 1.0);
\t\t}
\t\tif (tMaxMax < LengthToAtmosphere)
\t\t{
\t\t\treturn float4(0.0, 0.0, 0.0, 1.0);
\t\t}
\t}
\treturn float4(ss.L, 1.0 - Transmittance);
}
float4 After() { return float4(0.0, 0.0, 0.0, 1.0); }
"""


class CacheTest(unittest.TestCase):
    def test_patch_sets_the_shader_cache_aside(self):
        with tempfile.TemporaryDirectory() as d:
            game, mod, bak, user = (os.path.join(d, n) for n in ("game", "mod", "bak", "user"))
            src = os.path.join(game, *skypatch.SHADER.split("/"))
            os.makedirs(os.path.dirname(src))
            with open(src, "w") as fh:
                fh.write(SHADER)
            with open(os.path.join(game, *skypatch.MAIN.split("/")), "w") as fh:
                fh.write("x\n")
            db = os.path.join(user, "temp", "shaders", "shaders.vk.db")
            os.makedirs(os.path.dirname(db))
            with open(db, "w") as fh:
                fh.write("old")
            self.assertEqual(skypatch.apply(game, mod, bak, user), "patched")
            self.assertFalse(os.path.exists(db))
            self.assertTrue(os.path.exists(db + ".bnge-old"))
            with open(db, "w") as fh:
                fh.write("new")
            self.assertEqual(skypatch.apply(game, mod, bak, user), "current")
            self.assertTrue(os.path.exists(db))            # (patched already: the cache is the patched one)


class SkyColourTest(unittest.TestCase):
    """skyLutSkyP.hlsl: the sky's colour from the camera's own place on the planet (curved world far from the
    origin), patched, upgraded on an already patched game, backed up and restored."""

    def test_sky_camera_line(self):
        sky_src = "float4 main(Conn In) : SV_Target\n{\n    " + skypatch.SKY_OLD + "\n    return 0;\n}\n"
        with tempfile.TemporaryDirectory() as d:
            game, mod, bak, user = (os.path.join(d, n) for n in ("game", "mod", "bak", "user"))
            src = os.path.join(game, *skypatch.SHADER.split("/"))
            os.makedirs(os.path.dirname(src))
            with open(src, "w") as fh:
                fh.write(skypatch.patched(SHADER))          # (the camera volume patched already: an upgrade)
            with open(os.path.join(game, *skypatch.MAIN.split("/")), "w") as fh:
                fh.write("x\n")
            sky = os.path.join(game, *skypatch.SKY.split("/"))
            with open(sky, "w") as fh:
                fh.write(sky_src)
            self.assertEqual(skypatch.apply(game, mod, bak, user), "patched")
            with open(sky) as fh:
                text = fh.read()
            self.assertIn(skypatch.SKY_NEW, text)
            self.assertNotIn(skypatch.SKY_OLD, text)
            self.assertTrue(any(n.startswith("skyLutSkyP.") for n in os.listdir(bak)))
            self.assertEqual(skypatch.apply(game, mod, bak, user), "current")
            self.assertEqual(skypatch.restore(game, mod, user), "restored")
            with open(sky) as fh:
                self.assertEqual(fh.read(), sky_src)


class PatchTest(unittest.TestCase):
    def test_only_the_camera_volume_returns(self):
        out = skypatch.patched(SHADER)
        self.assertEqual(out.count(skypatch.MARK), 2)
        self.assertIn("float4 Other() { return float4(0.0, 0.0, 0.0, 1.0); }", out)
        self.assertIn("float4 After() { return float4(0.0, 0.0, 0.0, 1.0); }", out)
        self.assertIn("return float4(ss.L, 1.0 - Transmittance);", out)

    def test_unknown_code_left_alone(self):
        self.assertIsNone(skypatch.patched(SHADER.replace("RenderCameraVolumePS", "SomethingElse")))
        one = SHADER.replace("\t\t\treturn float4(0.0, 0.0, 0.0, 1.0);\n\t\t}\n\t\tif", "\t\t}\n\t\tif", 1)
        self.assertIsNone(skypatch.patched(one))

    def test_game_file_patched_in_place_backed_up_and_restored(self):
        with tempfile.TemporaryDirectory() as d:
            game, mod, bak = os.path.join(d, "game"), os.path.join(d, "mod"), os.path.join(d, "bak")
            src = os.path.join(game, *skypatch.SHADER.split("/"))
            os.makedirs(os.path.dirname(src))
            with open(src, "w") as fh:
                fh.write(SHADER)
            main = os.path.join(game, *skypatch.MAIN.split("/"))
            with open(main, "w") as fh:
                fh.write('#include "RenderSkyRayMarching.h.hlsl"\n')
            old_copy = os.path.join(mod, *skypatch.SHADER.split("/"))      # (an earlier version's mod copy)
            os.makedirs(os.path.dirname(old_copy))
            with open(old_copy, "w") as fh:
                fh.write("x")
            self.assertEqual(skypatch.apply(game, mod, bak), "patched")
            self.assertFalse(os.path.exists(old_copy))
            with open(src) as fh:
                self.assertEqual(fh.read().count(skypatch.MARK), 2)
            self.assertEqual(len(os.listdir(bak)), 1)
            with open(os.path.join(bak, os.listdir(bak)[0])) as fh:
                self.assertEqual(fh.read(), SHADER)
            self.assertTrue(os.path.isfile(os.path.join(mod, *skypatch.FLAG.split("/"))))
            with open(main) as fh:
                self.assertTrue(fh.read().startswith(skypatch.MAIN_MARK))
            self.assertEqual(skypatch.apply(game, mod, bak), "current")
            self.assertEqual(skypatch.restore(game, mod), "restored")
            with open(main) as fh:
                self.assertEqual(fh.read(), '#include "RenderSkyRayMarching.h.hlsl"\n')
            with open(src) as fh:
                self.assertEqual(fh.read(), SHADER)
            self.assertFalse(os.path.exists(os.path.join(mod, *skypatch.FLAG.split("/"))))
            self.assertEqual(skypatch.restore(game, mod), "original")

    def test_unknown_game_code_left_alone(self):
        with tempfile.TemporaryDirectory() as d:
            game, mod, bak = os.path.join(d, "game"), os.path.join(d, "mod"), os.path.join(d, "bak")
            src = os.path.join(game, *skypatch.SHADER.split("/"))
            os.makedirs(os.path.dirname(src))
            with open(src, "w") as fh:
                fh.write("// a newer game: other code\n")
            self.assertEqual(skypatch.apply(game, mod, bak), "skipped")
            with open(src) as fh:
                self.assertEqual(fh.read(), "// a newer game: other code\n")
            self.assertEqual(skypatch.apply(None, mod, bak), "skipped")

    def test_generated_file_is_not_part_of_the_mods_hash(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "a.lua"), "w") as fh:
                fh.write("x")
            before = install.tree_hash(d)
            p = os.path.join(d, *skypatch.FLAG.split("/"))
            os.makedirs(os.path.dirname(p))
            with open(p, "w") as fh:
                fh.write("patched")
            self.assertEqual(install.tree_hash(d), before)
            self.assertIn(skypatch.FLAG, install.GENERATED)


if __name__ == "__main__":
    unittest.main()
