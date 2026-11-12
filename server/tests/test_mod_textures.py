"""The mod ships its engine textures as DDS (tools/mod_textures_dds.py, 2026-11-11): a PNG a material names goes
through the engine's TextureCooker at run time, which has deadlocked the game under heavy loading and cooks every
texture again for each copy of the mod (a BeamMP server's)."""
import json
import re
import struct
import unittest
from pathlib import Path

MOD = Path(__file__).resolve().parents[2] / "mod" / "beamng_earth"
LEVEL = MOD / "levels" / "beamng_earth"
TERRAINS = LEVEL / "art" / "terrains"      # (heightmaps / layer maps / TerrainMaterial sources: not the cooker's)
FMT_BLOCK = {71: 8, 72: 8, 80: 8, 81: 8, 77: 16, 78: 16, 83: 16, 84: 16, 98: 16, 99: 16}


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


class ModTexturesTest(unittest.TestCase):
    def test_no_png_textures_left_in_the_level(self):
        left = [str(p.relative_to(MOD)) for p in LEVEL.rglob("*") if p.suffix.lower() in (".png", ".jpg", ".tga")
                and TERRAINS not in p.parents and p.name != "beamng_earth_preview.jpg"]
        self.assertEqual(left, [], "PNG textures in the level: run tools/mod_textures_dds.py (the engine cooks a PNG "
                                   "at run time)")

    def test_every_material_texture_exists(self):
        lower = {str(p).lower() for p in LEVEL.rglob("*")}
        missing = []
        for vfs, files in sorted(material_textures().items()):
            path = MOD / vfs.lstrip("/")
            if str(path).lower() not in lower:
                missing.append((vfs, files[0]))
            elif TERRAINS not in path.parents:
                self.assertTrue(vfs.endswith(".dds"), vfs)
        self.assertEqual(missing, [])

    def test_every_dds_is_pow2_with_its_whole_mip_chain(self):
        n = 0
        for p in LEVEL.rglob("*.dds"):
            if TERRAINS in p.parents:
                continue
            with self.subTest(p.name):
                levels = dds_levels(p.read_bytes())
                w, h = levels[0][:2]
                self.assertTrue(w & (w - 1) == 0 and h & (h - 1) == 0,
                                "power-of-two sides (the engine refuses a compressed texture of any other size)")
                if len(levels) > 1 or max(levels[0][:2]) > 64:
                    self.assertEqual(levels[-1][:2], (1, 1), "the mips down to 1 x 1")
                n += 1
        self.assertGreater(n, 150)

    def test_night_light_maps_exist(self):
        src = (MOD / "lua" / "ge" / "extensions" / "beamngEarth" / "nightlights.lua").read_text(encoding="utf-8")
        shapes = re.search(r"local SHAPES = '([^']+)'", src).group(1)
        steps = re.findall(r"steps\('([^']+)', '([^']+)'\)", src)
        self.assertGreater(len(steps), 3)
        for base, ext in steps:
            self.assertTrue(ext.endswith(".dds"), ext)
            for suffix in ("", "_late", "_dawn"):
                self.assertTrue((MOD / (shapes + base + suffix + ext).lstrip("/")).exists(), base + suffix + ext)


if __name__ == "__main__":
    unittest.main()
