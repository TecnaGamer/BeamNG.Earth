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
            by = {}
            for member, kind, arch in terraintex.TEXTURES.values():
                if all(m != member for m, _ in by.get(arch, [])):     # (one member can feed two files)
                    by.setdefault(arch, []).append((member, kind))
            for arch, items in by.items():
                path = os.path.join(game, *arch)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with zipfile.ZipFile(path, "w") as z:
                    for member, kind in items:
                        z.writestr(member, _png((128, 128, 0) if kind == "normal" else (90, 90, 90)))
            self.assertEqual(sorted(terraintex.ensure(user, game)), sorted(terraintex.TEXTURES))
            rc = np.asarray(Image.open(os.path.join(terraintex.out_dir(user),
                                                    terraintex.rc_name(terraintex.RECENTRED_STEMS[0]))))
            self.assertEqual(rc.shape, (terraintex.SIZE, terraintex.SIZE, 3))
            self.assertTrue(np.all(rc == 128), "a flat 90 grey: zero-mean (127.5 rounds up)")
            self.assertEqual(terraintex.ensure(user, game), [], "once")
            self.assertEqual(terraintex.ensure(user, None), [])
            self.assertEqual(terraintex.ensure(os.path.join(user, "x"), os.path.join(game, "none")), [],
                             "no game folder: nothing, no raise")

    def test_recentre(self):
        rng = np.random.default_rng(3)
        a = np.clip(rng.normal((80, 120, 60), 20, (64, 64, 3)), 0, 255).astype(np.uint8)
        r = terraintex.recentre(a)
        self.assertTrue(np.allclose(r.reshape(-1, 3).mean(0), 127.5, atol=0.6), r.reshape(-1, 3).mean(0))
        self.assertTrue(np.array_equal((r.astype(int) - a)[..., 0], np.full((64, 64), (r.astype(int) - a)[0, 0, 0])),
                        "one offset per channel: the grain kept")
        self.assertEqual(terraintex.rc_name("soil/t_gravel/t_gravel_b"), "bne_rc_soil__t_gravel__t_gravel_b.png")
        for s in terraintex.RECENTRED_STEMS:
            self.assertEqual(terraintex.stock_of(terraintex.rc_name(s)), "/assets/materials/terrain/" + s + ".png")
        self.assertIsNone(terraintex.stock_of("bne_pavers_b.png"))

    def test_recentred_lists_agree(self):
        """terraintex.RECENTRED_STEMS = terrain.lua M.RECENTRED; every stock colour map of a ground layer (the level's
        JSON, LOOKS' look() rows) is one of them."""
        lua = (ROOT / "mod/beamng_earth/lua/ge/extensions/beamngEarth/terrain.lua").read_text()
        block = re.search(r"^M\.RECENTRED = \{(.*?)^\}", lua, re.M | re.S).group(1)
        self.assertEqual(set(re.findall(r"\['([^']+)'\] = true", block)), set(terraintex.RECENTRED_STEMS))
        mats = json.loads((ROOT / "mod/beamng_earth/levels/beamng_earth/art/terrains/main.materials.json").read_text())
        for name, m in mats.items():
            for k in ("baseColorDetailTex", "baseColorMacroTex"):
                v = m.get(k)
                if v and v.startswith("/assets/materials/terrain/"):
                    self.fail(f"{name} {k}: the stock {v}, not its zero-mean copy")

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
