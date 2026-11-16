"""The squares' layer textures as PNG (terraintex.py; the user 2026-11-11: "paint the plaza and apply a texture to it
like the roads")."""
import io
import json
import os
import re
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import terraintex  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def _png(rgb, size=8):
    b = io.BytesIO()
    Image.fromarray(np.full((size, size, 3), rgb, np.uint8), "RGB").save(b, "PNG")
    return b.getvalue()


class TerrainTexTest(unittest.TestCase):
    def test_normal_z_rebuilt(self):
        im = np.asarray(terraintex.convert(_png((128, 128, 0)), "normal"))
        self.assertEqual(im.shape, (terraintex.SIZE, terraintex.SIZE, 3))
        self.assertGreater(int(im[..., 2].min()), 250, "flat: z up")
        tilted = np.asarray(terraintex.convert(_png((200, 128, 0)), "normal"))
        self.assertLess(int(tilted[..., 2].mean()), 250)
        col = np.asarray(terraintex.convert(_png((180, 60, 40)), "color"))
        self.assertEqual(tuple(col[0, 0]), (180, 60, 40), "colour as it is")

    def test_ensure_from_the_game_archive(self):
        with tempfile.TemporaryDirectory() as game, tempfile.TemporaryDirectory() as user:
            arch = os.path.join(game, *terraintex.ARCHIVE)
            os.makedirs(os.path.dirname(arch))
            with zipfile.ZipFile(arch, "w") as z:
                for member, kind in terraintex.TEXTURES.values():
                    z.writestr(member, _png((128, 128, 0) if kind == "normal" else (90, 90, 90)))
            self.assertEqual(sorted(terraintex.ensure(user, game)), sorted(terraintex.TEXTURES))
            self.assertEqual(terraintex.ensure(user, game), [], "once")
            self.assertEqual(terraintex.ensure(user, None), [])
            self.assertEqual(terraintex.ensure(os.path.join(user, "x"), os.path.join(game, "none")), [],
                             "no game folder: nothing, no raise")

    def test_materials_name_the_written_textures(self):
        mats = json.loads((ROOT / "mod/beamng_earth/levels/beamng_earth/art/terrains/main.materials.json").read_text())
        used = {v for m in mats.values() for k, v in m.items() if isinstance(v, str)
                and v.startswith(terraintex.VFS_DIR)}
        self.assertTrue(used)
        self.assertEqual({u[len(terraintex.VFS_DIR):] for u in used}, set(terraintex.TEXTURES))
        lua = (ROOT / "mod/beamng_earth/lua/ge/extensions/beamngEarth/terrain.lua").read_text()
        self.assertIn(f"local TL = '{terraintex.VFS_DIR}'", lua)
        for name in re.findall(r"tileLook\('([^']+)', '([^']+)'", lua):
            self.assertTrue(set(name) <= set(terraintex.TEXTURES), name)


if __name__ == "__main__":
    unittest.main()
