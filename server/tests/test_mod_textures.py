"""The game loads only DDS textures of the mod (2026-11-11): a PNG a material names goes through the engine's
TextureCooker at run time, which has deadlocked the game under heavy loading and cooks every texture again for each
copy of the mod (a BeamMP server's). Since 2026-11-13 most of them are made at install time from lossless sources
(mod/texture_sources, modtextures.py, native texdds.rs; tools/mod_textures_dds.py makes the sources)."""
import hashlib
import json
import os
import re
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MOD = ROOT / "mod" / "beamng_earth"
LEVEL = MOD / "levels" / "beamng_earth"
TERRAINS = LEVEL / "art" / "terrains"      # (heightmaps / layer maps / TerrainMaterial sources: not the cooker's)
FMT_BLOCK = {71: 8, 72: 8, 80: 8, 81: 8, 77: 16, 78: 16, 83: 16, 84: 16, 98: 16, 99: 16}
FMT_NAME = {"BC4": 80, "BC5": 83, "BC7": 98, "BC7_SRGB": 99}
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import modtextures  # noqa: E402


def dds_levels(data):
    """[(w, h, bytes)] of a block-compressed DDS's mips (DX10 or DXT1/DXT5/ATI1/ATI2 header); asserts the size."""
    assert data[:4] == b"DDS ", "not a DDS"
    h, w = struct.unpack_from("<II", data, 12)
    mips = max(1, struct.unpack_from("<I", data, 28)[0])
    four = data[84:88]
    if four == b"DX10":
        block, off = FMT_BLOCK[struct.unpack_from("<I", data, 128)[0]], 148
    else:
        block, off = {b"DXT1": 8, b"ATI1": 8, b"BC4U": 8, b"DXT5": 16, b"ATI2": 16, b"BC5U": 16}[four], 128
    out = []
    for k in range(mips):
        lw, lh = max(1, w >> k), max(1, h >> k)
        n = ((lw + 3) // 4) * ((lh + 3) // 4) * block
        out.append((lw, lh, n))
        off += n
    assert off == len(data), (off, len(data))
    return out


def material_textures():
    """{vfs path: [materials.json files naming it]} for every /levels/beamng_earth/ texture a material names."""
    out = {}
    for p in LEVEL.rglob("*.materials.json"):
        for m in re.finditer(r'"(/levels/beamng_earth/[^"]+\.(?:png|dds|jpg))"', p.read_text(encoding="utf-8")):
            out.setdefault(m.group(1), []).append(p.name)
    return out


def made():
    """{path relative to the mod, lower case: entry} of the textures made at install."""
    return {k.lower(): v for k, v in modtextures.manifest().items()}


def check_pow2_mips(test, data, what):
    levels = dds_levels(data)
    w, h = levels[0][:2]
    test.assertTrue(w & (w - 1) == 0 and h & (h - 1) == 0,
                    f"{what}: power-of-two sides (the engine refuses a compressed texture of any other size)")
    if len(levels) > 1 or max(levels[0][:2]) > 64:
        test.assertEqual(levels[-1][:2], (1, 1), f"{what}: the mips down to 1 x 1")


class ModTexturesTest(unittest.TestCase):
    def test_no_png_textures_left_in_the_level(self):
        left = [str(p.relative_to(MOD)) for p in LEVEL.rglob("*") if p.suffix.lower() in (".png", ".jpg", ".tga")
                and TERRAINS not in p.parents and p.name != "beamng_earth_preview.jpg"]
        self.assertEqual(left, [], "PNG textures in the level: run tools/mod_textures_dds.py (the engine cooks a PNG "
                                   "at run time)")

    def test_sources_never_inside_the_mod(self):
        """The sources (WebP) and the manifest live in mod/texture_sources, outside the folder copied into the game."""
        self.assertEqual([str(p) for p in MOD.rglob("*.webp")], [])
        for rel, e in modtextures.manifest().items():
            self.assertTrue(rel.endswith(".dds"), rel)
            self.assertIn(e["format"], FMT_NAME, rel)
            self.assertIn(e["role"], ("color", "normal", "data"), rel)
            self.assertTrue((ROOT / "mod" / "texture_sources" / e["source"]).is_file(), e["source"])
            self.assertFalse((MOD / rel).exists(), f"{rel} is made at install: not in the tree")

    def test_every_material_texture_exists(self):
        lower = {str(p).lower() for p in LEVEL.rglob("*")}
        at_install = made()
        missing = []
        for vfs, files in sorted(material_textures().items()):
            path = MOD / vfs.lstrip("/")
            if str(path).lower() not in lower and vfs.lstrip("/").lower() not in at_install:
                missing.append((vfs, files[0]))
            elif TERRAINS not in path.parents:
                self.assertTrue(vfs.endswith(".dds"), vfs)
        self.assertEqual(missing, [])

    def test_every_dds_in_the_tree_is_pow2_with_its_whole_mip_chain(self):
        for p in LEVEL.rglob("*.dds"):
            if TERRAINS in p.parents:
                continue
            with self.subTest(p.name):
                check_pow2_mips(self, p.read_bytes(), p.name)

    def test_install_makes_every_texture(self):
        """modtextures.build (install.py's step) makes every DDS of the manifest: its format, pow2 sides, whole mip
        chain; a second build keeps them all (the stamp); the bytes are the same with one thread."""
        todo = modtextures.plan()
        self.assertGreater(len(todo), 140)
        with tempfile.TemporaryDirectory() as d:
            n, kept, _s = modtextures.build(d, todo)
            self.assertEqual((n, kept), (len(todo), 0))
            for rel, e in todo.items():
                with self.subTest(rel):
                    data = (Path(d) / rel).read_bytes()
                    self.assertEqual(struct.unpack_from("<I", data, 128)[0], FMT_NAME[e["format"]])
                    check_pow2_mips(self, data, rel)
            self.assertTrue(modtextures.current(d, todo))
            self.assertEqual(modtextures.reusable(d, todo), set(todo))
            # one texture of each kind again on one thread: the same bytes (deterministic)
            kinds = {}
            for rel, e in sorted(todo.items()):
                kinds.setdefault((e["format"], e["role"], e.get("derive", "").split(":")[0]), rel)
            sub = {rel: todo[rel] for rel in kinds.values()}
            with tempfile.TemporaryDirectory() as d2:
                modtextures.build(d2, sub, threads=1)
                for rel in sub:
                    self.assertEqual(hashlib.sha256((Path(d2) / rel).read_bytes()).hexdigest(),
                                     hashlib.sha256((Path(d) / rel).read_bytes()).hexdigest(), rel)
            # a changed source: only its texture is made again
            stamp = json.loads((Path(d) / modtextures.STAMP).read_text())
            some = sorted(todo)[0]
            stamp[some]["key"] = "0" * 64
            (Path(d) / modtextures.STAMP).write_text(json.dumps(stamp))
            self.assertEqual(modtextures.reusable(d, todo), set(todo) - {some})

    def test_derived_maps_are_the_tools_own(self):
        """A texture set's normal / roughness maps made from its colour map are the build tools' (bit for bit)."""
        import numpy as np
        from PIL import Image, ImageFilter
        import ingest_rail_textures as irt
        ef = modtextures.native()
        entries = modtextures.manifest()
        seen = set()
        for rel, e in sorted(entries.items()):
            kind = e.get("derive", "").split(":")[0]
            if not kind or kind in seen:
                continue
            seen.add(kind)
            src = (ROOT / "mod" / "texture_sources" / e["source"]).read_bytes()
            w, h, c, px = ef.texture_pixels(src)
            rgb = np.frombuffer(px, np.uint8).reshape(h, w, c)[..., :3].astype(np.float64)
            w, h, c, px = ef.texture_pixels(src, e["derive"])
            got = np.frombuffer(px, np.uint8).reshape(h, w, c)
            args = [float(v) for v in e["derive"].split(":")[1:]]
            if kind == "normal_fft":
                want = irt.normal_map(rgb, *args)
            elif kind == "rough_fft":
                want = irt.roughness_map(rgb, *args)[..., None]
            elif kind == "normal_box":       # (make_sett_texture.normal_map, NORMAL_STRENGTH = args[0], SIZE = w)
                lum = rgb.mean(2)
                hh = np.asarray(Image.fromarray(np.clip(lum, 0, 255).astype(np.uint8)).filter(
                    ImageFilter.GaussianBlur(1.4)), float) / 255
                dx = (np.roll(hh, -1, 1) - np.roll(hh, 1, 1)) * 0.5 * w / 64
                dy = (np.roll(hh, -1, 0) - np.roll(hh, 1, 0)) * 0.5 * w / 64
                n = np.stack([-dx * args[0], dy * args[0], np.ones_like(hh)], 2)
                n /= np.linalg.norm(n, axis=2, keepdims=True)
                want = ((n * 0.5 + 0.5) * 255).astype(np.uint8)
            else:                           # rough_box (make_sett_texture.roughness)
                lum = rgb.mean(2)
                lo, hi = np.percentile(lum, 5), np.percentile(lum, 95)
                t = np.clip((lum - lo) / max(hi - lo, 1.0), 0, 1)
                want = np.repeat(((0.97 - 0.17 * t) * 255)[:, :, None], 3, 2).astype(np.uint8)
            self.assertTrue(np.array_equal(got, want), rel)
        self.assertEqual(seen, {"normal_fft", "rough_fft", "normal_box", "rough_box"})

    def test_night_light_maps_exist(self):
        src = (MOD / "lua" / "ge" / "extensions" / "beamngEarth" / "nightlights.lua").read_text(encoding="utf-8")
        shapes = re.search(r"local SHAPES = '([^']+)'", src).group(1)
        steps = re.findall(r"steps\('([^']+)', '([^']+)'\)", src)
        self.assertGreater(len(steps), 3)
        at_install = made()
        for base, ext in steps:
            self.assertTrue(ext.endswith(".dds"), ext)
            for suffix in ("", "_late", "_dawn"):
                rel = (shapes + base + suffix + ext).lstrip("/")
                self.assertTrue((MOD / rel).exists() or rel.lower() in at_install, base + suffix + ext)


if __name__ == "__main__":
    unittest.main()
