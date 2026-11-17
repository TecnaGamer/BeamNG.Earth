"""The game's packed terrain textures made by the server (terrainprefill.py; 2026-11-14: 12 s and 5 s freezes at
128 mph, the game packing bngEarthGroundRock's textures on its main thread)."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bc7  # noqa: E402
import terraincache  # noqa: E402
import terrainprefill as tp  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
LEVEL_JSON = ROOT / "mod" / "beamng_earth" / "levels" / "beamng_earth" / "art" / "terrains" / "main.materials.json"
TERRAIN_LUA = ROOT / "mod" / "beamng_earth" / tp.TERRAIN_LUA
LUAJIT = shutil.which("luajit")
# the first 148 bytes of the game's own cache file e6fcfabef688536b.dds (bngEarthGroundRock's detail colour, written
# by BeamNG.drive 0.40 on 2026-11-14: 1024², 11 mips, DXGI_BC7_UNORM)
GAME_HEADER = bytes.fromhex(
    "444453207c00000007100a00000400000004000000001000010000000b000000" + "00" * 44
    + "2000000004000000445831300000000000000000000000000000000000000000081040000000000000000000000000000000000062000000"
    + "03000000000000000100000000000000")


def _png(a):
    b = io.BytesIO()
    Image.fromarray(np.asarray(a, np.uint8)).save(b, "PNG")
    return b.getvalue()


class KeysTest(unittest.TestCase):
    def test_rock_keys(self):
        """The four keys the game missed for bngEarthGroundRock (its log, 2026-11-14), from the path rules."""
        # (the stock colour maps: the layer's look when the server's zero-mean copies are missing, still prefilled)
        f = tp.with_stock_colour(tp.look_fields("rock/t_dirt_rocky/t_dirt_rocky", "rock/macro_rocky/t_macro_rocky"))
        keys ={p.what.split()[-2] + " " + p.kind: p.key for s in ("Detail", "Macro") for p in tp.slot_packs(f, s, 1024)}
        self.assertEqual(keys, {"detail color": "e6fcfabef688536b", "macro color": "fd2b60672cc54aae",
                                "detail pack": "67b10910fce45348", "macro pack": "9a3c37a62b7067cb"})
        level = tp.wanted(json.loads(LEVEL_JSON.read_text()), base_sizes=())
        for k in keys.values():
            self.assertIn(k, level, "the level's rock material")
        d = "/assets/materials/terrain/rock/t_dirt_rocky/t_dirt_rocky"
        self.assertEqual(tp.key((d + "_b.png",) * 3 + (d + "_ao.png",)), "e6fcfabef688536b")
        self.assertEqual(terraincache.base_key(d + "_b.png", d + "_ao.png"), "e6fcfabef688536b", "terraincache's rule")

    def test_base_slot_at_each_photo_size(self):
        """The level's flat base maps (terrain.lua baseMapFor): 2048 the plain names, sRGB colour, linear pack."""
        w = tp.wanted(json.loads(LEVEL_JSON.read_text()), base_sizes=(1024, 2048, 4096))
        base = {(p.px, p.kind): p for p in w.values() if " base " in p.what}
        self.assertEqual(sorted(base), [(n, k) for n in (1024, 2048, 4096) for k in ("color", "pack")])
        # (the game's own two 2048 files, 2026-11-14)
        self.assertEqual(base[2048, "color"].key, "32704c6398ec8cb6")
        self.assertEqual(base[2048, "pack"].key, "6c8c4a0c4f73bb93")
        self.assertEqual(base[2048, "color"].dxgi, bc7.DXGI_BC7_UNORM_SRGB)
        self.assertEqual(base[2048, "pack"].dxgi, bc7.DXGI_BC7_UNORM)
        self.assertTrue(base[4096, "pack"].sources[0].endswith("bngEarth_base_r_4096.png"))
        self.assertTrue(all(p.dxgi == bc7.DXGI_BC7_UNORM for p in w.values() if " base " not in p.what),
                        "detail / macro colour is linear in the game's cache")

    def test_header_is_the_games(self):
        self.assertEqual(bc7.header(1024, 1024, 11, bc7.DXGI_BC7_UNORM), GAME_HEADER)
        self.assertEqual(tp.expected_size(1024), 1398276, "the game's file size")


class CoverageTest(unittest.TestCase):
    """Every look terrain.lua can put on a TerrainMaterial has its packed textures in wanted()."""

    @staticmethod
    def lua_rows():
        out = subprocess.run([LUAJIT, str(ROOT / "mod" / "tests" / "dump_ground_looks.lua")], capture_output=True,
                             text=True, check=True, timeout=60).stdout
        rows = {}
        for line in out.splitlines():
            table, name, field, value = line.split("\t")
            rows.setdefault(table, {}).setdefault(name, {})[field] = value
        return rows

    @staticmethod
    def uncovered(rows, materials, want):
        """[(table, material, pack)] of the looks whose packs are not in want: a LOOKS entry stands for itself when the
        level does not define that material (made at runtime), a TILE_FALLBACK one over the level's material."""
        level = {n: v for n, v in materials.items() if isinstance(v, dict) and v.get("class") == "TerrainMaterial"}
        px = tp.level_slot_px(materials)
        miss = []
        for table, looks in rows.items():
            for name, f in looks.items():
                if table == "LOOKS" and name in level:
                    f = level[name]
                elif table == "TILE_FALLBACK":
                    f = {**level.get(name, {}), **f}
                for slot in ("Detail", "Macro"):
                    for p in tp.slot_packs(f, slot, px[slot]):
                        if p.key not in want:
                            miss.append((table, name, p.what.strip() or slot, p.kind))
        return miss

    @unittest.skipUnless(LUAJIT, "luajit")
    def test_every_look_covered(self):
        rows = self.lua_rows()
        self.assertIn("bngEarthGroundRock", rows["LOOKS"])
        self.assertIn("bngEarthGroundCobble", rows["TILE_FALLBACK"])
        materials = json.loads(LEVEL_JSON.read_text())
        want = tp.wanted(materials, TERRAIN_LUA.read_text(), base_sizes=())
        self.assertEqual(self.uncovered(rows, materials, want), [],
                         "a ground look of terrain.lua is not prefilled: define its material in the level "
                         "(tools/make_level_assets.py), or make terrainprefill.lua_looks read it")
        # a look terrain.lua would make at runtime, not in the level: caught
        extra = {"LOOKS": {"bngEarthGroundNew": tp.look_fields("x/new/t_new", "x/macro/t_macro")}}
        self.assertEqual(len(self.uncovered(extra, materials, want)), 4)

    @unittest.skipUnless(LUAJIT, "luajit")
    def test_parse_matches_lua(self):
        """lua_looks' fields of the look() / golfLook() entries are what luajit evaluates (the path rules agree)."""
        rows = self.lua_rows()
        parsed = tp.lua_looks(TERRAIN_LUA.read_text())
        self.assertEqual(set(parsed["TILE_FALLBACK"]), set(rows["TILE_FALLBACK"]))
        self.assertGreaterEqual(len(parsed["LOOKS"]), 10)
        for table in ("LOOKS", "TILE_FALLBACK"):
            for name, f in parsed[table].items():
                self.assertEqual(f, rows[table][name], f"{table}.{name}")


class PrefillTest(unittest.TestCase):
    PX = 16

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.user, self.game, self.mod = t / "user", t / "game", t / "mod"
        self.user.mkdir()
        n = self.PX
        rng = np.random.default_rng(7)

        def blocks(ch):     # (each 4 x 4 block one random value per channel: BC7 keeps a flat block nearly exactly)
            a = rng.integers(0, 256, (n // 4, n // 4, ch), np.uint8).repeat(4, 0).repeat(4, 1)
            return a[..., 0] if ch == 1 else a
        self.src = {"_b": blocks(3), "_ao": blocks(1), "_r": blocks(1), "_nm": blocks(3)}
        zdir = self.game / "content" / "assets" / "materials"
        zdir.mkdir(parents=True)
        with zipfile.ZipFile(zdir / "terrain.zip", "w") as z:
            for stem in ("d/t_d", "m/t_m"):
                for suf, a in self.src.items():
                    z.writestr(f"assets/materials/terrain/{stem}{suf}.png", _png(a))
        self.zip = zdir / "terrain.zip"
        self.height = np.full((n, n), 128, np.uint8)
        hdir = self.mod / "levels" / "beamng_earth" / "art" / "terrains"
        hdir.mkdir(parents=True)
        (hdir / "bngEarth_flat_h_1024.png").write_bytes(_png(self.height))
        mat = {"class": "TerrainMaterial", **tp.look_fields("d/t_d", "m/t_m")}
        self.materials = {"mat": mat, "set": {"class": "TerrainMaterialTextureSet", "detailTexSize": [n, n],
                                              "macroTexSize": [n, n]}}
        self.cache = Path(terraincache.cache_dir(str(self.user)))
        old = time.time() - 86400
        os.utime(self.zip, (old, old))

    def tearDown(self):
        self.tmp.cleanup()

    def run_prefill(self, **kw):
        args = dict(materials=self.materials, lua_text="", mod_src=str(self.mod), duty=1.0, base_sizes=())
        args.update(kw)
        return tp.prefill(str(self.user), str(self.game), **args)

    def levels(self, path):
        data = path.read_bytes()
        self.assertEqual(data[:148], bc7.header(self.PX, self.PX, 5, bc7.DXGI_BC7_UNORM))
        out, off, n = [], 148, self.PX
        while n:
            sz = max(1, n // 4) ** 2 * 16
            out.append(bc7.decode(data[off:off + sz], n, n).astype(np.int32))
            off, n = off + sz, n // 2
        self.assertEqual(off, len(data))
        return out

    def test_channels_and_mips(self):
        r = self.run_prefill()
        want = {p.key: p for p in tp.wanted(self.materials, base_sizes=()).values()}
        self.assertEqual(sorted(r["written"]), sorted(want))
        self.assertEqual(len(want), 4)
        s = {k: v.astype(np.int32) for k, v in self.src.items()}
        for k, p in want.items():
            top = self.levels(self.cache / (k + ".dds"))
            if p.kind == "color":
                exp = np.dstack([s["_b"], s["_ao"]])
            else:
                exp = np.dstack([s["_r"], self.height, s["_nm"][..., 0], s["_nm"][..., 1]])
            err = np.abs(top[0] - exp).mean((0, 1))
            self.assertTrue((err < 3).all(), (p, err))
            for c in range(4):                                 # each channel closest to its own source
                others = [np.abs(top[0][..., c] - exp[..., o]).mean() for o in range(4) if o != c]
                if exp[..., c].std() > 1:
                    self.assertLess(err[c], min(others), (p, c))
            mean = exp.reshape(-1, 4).mean(0)
            self.assertTrue((np.abs(top[-1][0, 0] - mean) < 6).all(), "1x1 mip = the mean")

    def test_box_mips(self):
        a = np.zeros((4, 4, 4), np.uint8)
        a[:2, :2] = 10
        a[0, 0] = 13
        chain = tp.box_mips(a)
        self.assertEqual([m.shape[0] for m in chain], [4, 2, 1])
        self.assertEqual(int(chain[1][0, 0, 0]), 11)       # (13 + 10 + 10 + 10 + 2) >> 2
        self.assertEqual(int(chain[2][0, 0, 0]), 3)

    def test_encode_paced_same_bytes(self):
        img = np.random.default_rng(1).integers(0, 256, (64, 2048, 4), np.uint8)
        self.assertEqual(tp.encode_paced(img), bc7.encode(img))

    def test_fresh_not_rewritten_outdated_is(self):
        self.assertEqual(len(self.run_prefill()["written"]), 4)
        again = self.run_prefill()
        self.assertEqual((again["written"], again["fresh"]), ([], 4), "up to date: nothing written")
        k = sorted(p.key for p in tp.wanted(self.materials, base_sizes=()).values())
        f0 = self.cache / (k[0] + ".dds")
        st = f0.stat()
        os.utime(f0, (self.zip.stat().st_mtime - 10,) * 2)  # older than the game's archive: outdated
        f1 = self.cache / (k[1] + ".dds")
        f1.write_bytes(f1.read_bytes()[:-16])                 # the wrong size
        r = self.run_prefill()
        self.assertEqual(sorted(r["written"]), sorted(k[:2]))
        self.assertGreater(f0.stat().st_mtime, self.zip.stat().st_mtime)
        self.assertEqual(f0.stat().st_size, st.st_size)

    def test_future_source(self):
        """A source dated ahead of the clock: the cache file is dated after it (else the game would pack again)."""
        ahead = time.time() + 3600
        os.utime(self.zip, (ahead, ahead))
        self.run_prefill()
        for f in self.cache.iterdir():
            self.assertGreater(f.stat().st_mtime, ahead)
        self.assertEqual(self.run_prefill()["written"], [])

    def test_off(self):
        self.assertEqual(tp.prefill(None, str(self.game), materials=self.materials)["written"], [])
        self.assertEqual(tp.prefill(str(self.user), None, materials=self.materials)["written"], [])
        with mock.patch.object(terraincache, "NO_PACK", True):
            self.assertEqual(self.run_prefill()["written"], [])
        with mock.patch.object(tp, "ENABLED", False):
            self.assertEqual(self.run_prefill()["written"], [])
        self.assertFalse(self.cache.exists())

    def test_wrong_size_or_missing_source_left_to_the_game(self):
        self.materials["set"]["detailTexSize"] = [32, 32]     # the sources are 16: the game resizes, not us
        self.materials["mat"]["baseColorMacroTex"] = "/assets/materials/terrain/m/gone_b.png"
        r = self.run_prefill()
        self.assertEqual(len(r["written"]), 1, r)             # the macro pack only
        self.assertEqual(sorted(w for _, w in r["skipped"]), ["source missing", "source not 32 px 8-bit",
                                                               "source not 32 px 8-bit"])

    def test_sources_replaced_by_a_mod_or_the_user_folder_left_to_the_game(self):
        """A stock texture a mounted mod (zip or unpacked) or a loose user-folder file replaces in the game's VFS: its
        packs are not written (the key is the path alone: stock pixels would pass for the replacement's). Our own mod's
        files do not count (Codex review 2026-11-14)."""
        mods = self.user / "mods"
        (mods / "repo").mkdir(parents=True)
        with zipfile.ZipFile(mods / "repo" / "texturepack.zip", "w") as z:
            z.writestr("assets/materials/terrain/d/t_d_b.png", _png(self.src["_b"]))
        own = mods / "unpacked" / tp.MOD_NAME / "assets" / "materials" / "terrain" / "m"
        own.mkdir(parents=True)
        (own / "t_m_b.png").write_bytes(_png(self.src["_b"]))
        (mods / "multiplayer").mkdir()
        with zipfile.ZipFile(mods / "multiplayer" / "earth_mp.zip", "w") as z:    # (a BeamMP server's copy of ours)
            z.writestr("lua/ge/extensions/beamngEarth/core.lua", "")
            z.writestr("assets/materials/terrain/m/t_m_nm.png", _png(self.src["_nm"]))
        r = self.run_prefill()
        replaced = [k for k, w in r["skipped"] if "replaced" in w]
        self.assertEqual(len(replaced), 1, r)                 # the detail colour pack (t_d_b); our mod's t_m_b counts not
        self.assertEqual(len(r["written"]), 3, r)
        loose = self.user / "assets" / "materials" / "terrain" / "m"
        loose.mkdir(parents=True)
        (loose / "t_m_r.png").write_bytes(_png(self.src["_r"]))
        for f in self.cache.glob("*.dds"):
            f.unlink()
        r = self.run_prefill()
        self.assertEqual(len([k for k, w in r["skipped"] if "replaced" in w]), 2, r)   # + the macro pack (t_m_r)
        self.assertEqual(len(r["written"]), 2, r)
        lvl = self.user / "levels" / "beamng_earth" / "art" / "terrains"      # a loose level height replacement:
        lvl.mkdir(parents=True)                                               # both normal packs read it
        (lvl / "bngEarth_flat_h_1024.png").write_bytes(_png(self.height))
        for f in self.cache.glob("*.dds"):
            f.unlink()
        r = self.run_prefill()
        self.assertEqual(len([k for k, w in r["skipped"] if "replaced" in w]), 3, r)   # detail colour + both packs
        self.assertEqual(len(r["written"]), 1, r)                                       # the macro colour only


if __name__ == "__main__":
    unittest.main()
