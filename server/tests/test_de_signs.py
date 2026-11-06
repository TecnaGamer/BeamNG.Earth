"""Germany's junction signs' artwork and meshes: designs.GROUPS / HEIGHTS / size_suffix, the face sheet
bne_de_signs.png (tools/build_de_signs.py), one bne_de_<group>.dae per group (tools/build_median_props.py), trees.lua's
DE_SIGNS catalog and furniture's collision boxes."""
import math
import os
import re
import subprocess
import sys
import unittest

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..", "..")
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import designs  # noqa: E402
import furniture  # noqa: E402
import build_de_signs  # noqa: E402

TREES = os.path.join(ROOT, "mod", "beamng_earth", "lua", "ge", "extensions", "beamngEarth", "trees.lua")
SHAPES = os.path.join(ROOT, "mod", "beamng_earth", "levels", "beamng_earth", "art", "shapes", "beamngEarth")
SHEET = os.path.join(SHAPES, "bne_de_signs.png")

# DIN 6171's sign colours (VwV-StVO zu den §§ 39 bis 43 Rn. 20) as the VzKat's pictures show them (vzkat.de's 205,
# 206, 301, 306, 307 and 1002-10, flat patches' medians, 2026-11-06), by their RAL names: 3020 Verkehrsrot, 9016
# Verkehrsweiß, 1023 Verkehrsgelb, 9017 Verkehrsschwarz
SRGB_3020, SRGB_9016 = (0xDE, 0x00, 0x00), (0xEF, 0xEF, 0xEF)
SRGB_1023, SRGB_9017 = (0xFF, 0xB5, 0x00), (0x00, 0x00, 0x00)
DELTA_E = 1.0       # CIE76: a flat area keeps its colour exactly (the palette is seeded with the four)


def lab(rgb):
    """sRGB (0-255) to CIE L*a*b* (D65)."""
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in (v / 255.0 for v in rgb[:3])]
    r, g, b = lin
    x = (0.4124564 * r + 0.3575761 * g + 0.1804375 * b) / 0.95047
    y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
    z = (0.0193339 * r + 0.1191920 * g + 0.9503041 * b) / 1.08883
    f = lambda t: t ** (1 / 3) if t > 216 / 24389 else (24389 / 27 * t + 16) / 116
    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


RAL_3020, RAL_9016, RAL_1023, RAL_9017 = (lab(c) for c in (SRGB_3020, SRGB_9016, SRGB_1023, SRGB_9017))


def sheet():
    return Image.open(SHEET).convert("RGB")


def at(im, name, x, z):
    """The sheet's colour at (x, z) m of picture name (Größe 2, from its middle: build_de_signs.pixel)."""
    px, py = build_de_signs.pixel(name, x, z)
    return im.getpixel((int(round(px)), int(round(py))))


def lum(rgb):
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]


def dae(group):
    """The near LOD of bne_<group>.dae: positions (n, 3), uvs (n, 2) and {material: triangles (k, 3)}."""
    with open(os.path.join(SHAPES, f"bne_{group}.dae"), encoding="utf-8") as fh:
        xml = fh.read()
    geom = re.search(r'<geometry id="bne_%s32-g">(.*?)</geometry>' % re.escape(group), xml, re.S).group(1)
    arr = lambda kind: np.array(re.search(r'<float_array id="bne_%s32-g-%s-a"[^>]*>([^<]*)<' % (re.escape(group), kind),
                                          geom).group(1).split(), float)
    tris = {m: np.array(p.split(), int).reshape(-1, 3)
            for m, p in re.findall(r'<triangles count="\d+" material="([\w]+)-mat">.*?<p>([^<]*)</p>', geom, re.S)}
    return arr("p").reshape(-1, 3), arr("uv").reshape(-1, 2), tris


class GroupsTest(unittest.TestCase):
    def test_35_assemblies_in_three_sizes(self):
        self.assertEqual(len(designs.ASSEMBLIES), 35)
        self.assertEqual(len(designs.GROUPS), 105)
        self.assertEqual(len(set(designs.GROUPS)), 105)
        want = {"de_z205", "de_z206", "de_z301", "de_z306", "de_z307"} | {
            f"de_{f}_p{k}" for f in ("z205", "z206", "z306") for k in range(len(designs.PLATES))}
        self.assertEqual(set(designs.ASSEMBLIES), want)
        self.assertEqual(set(designs.GROUPS), {a + s for a in want for s in ("_g1", "", "_g3")})
        self.assertFalse([g for g in designs.GROUPS if g.endswith("_r") or g.startswith("de_z301_p")],
                         "no out-of-town variant; no plate with 301 (VwV-StVO zu Zeichen 301 Rn. 3)")

    def test_size_by_the_speed_limit(self):
        """VwV-StVO zu den §§ 39 bis 43 Rn. 15 (triangles, squares and rectangles)."""
        for kmh, want in ((None, ""), (0, ""), (20, "_g1"), (30, "_g1"), (49.9, "_g1"), (50, ""), (70, ""),
                          (100, ""), (100.5, "_g3"), (120, "_g3")):
            self.assertEqual(designs.size_suffix(kmh), want, kmh)

    def test_heights_follow_the_parts(self):
        for a in designs.ASSEMBLIES:
            g1, g2, g3 = (designs.HEIGHTS[a + s] for s in designs.SIZES)
            if a.startswith("de_z206"):     # (the VzKat's octagon has no Größe 1: its 900 mm, and its plate Größe 2's)
                self.assertTrue(designs.LOW_M < g1 == g2 < g3, a)
            else:
                self.assertTrue(designs.LOW_M < g1 < g2 < g3, a)
        self.assertEqual(designs.SIZE["z206"], (0.90, 0.90, 1.05))
        for k in range(len(designs.PLATES)):
            self.assertEqual(designs.panels(f"de_z206_p{k}_g1"), designs.panels(f"de_z206_p{k}"), k)
        self.assertEqual(designs.panels("de_z205_p4_g1")[0][2], 0.42, "(a triangle's Größe 1 keeps its 420 mm plate)")
        self.assertAlmostEqual(designs.HEIGHTS["de_z205"], 2.0 + 0.9 * 3 ** 0.5 / 2, delta=0.05)
        self.assertGreater(designs.HEIGHTS["de_z205_p0"], designs.HEIGHTS["de_z205"] + 0.6)
        self.assertEqual(designs.PARTS["de_z306_p4_g3"], ("z306", 4, 2))
        self.assertEqual(designs.PARTS["de_z307"], ("z307", None, 1))


class CatalogTest(unittest.TestCase):
    def test_catalog_matches_lua(self):
        with open(TREES, encoding="utf-8") as f:
            lua = f.read()
        block = lua[lua.index("local DE_SIGNS"):lua.index("for g, h in pairs(DE_SIGNS)")]
        got = {k: float(v) for k, v in re.findall(r"(de_\w+) = ([\d.]+)", block)}
        self.assertEqual(got, designs.HEIGHTS)
        for g in designs.GROUPS:
            self.assertTrue(os.path.exists(os.path.join(SHAPES, f"bne_{g}.dae")), g)

    def test_meshes_at_true_size(self):
        """Rows carry HEIGHTS[group] at scale 1: each mesh is its sign as it stands, the lowest edge 2 m up (VwV-StVO
        zu den §§ 39 bis 43 Rn. 42), the face towards -Y."""
        for g in designs.GROUPS:
            p, uv, tris = dae(g)
            face = np.unique(tris["bne_de_signs"])
            self.assertAlmostEqual(p[:, 2].max(), designs.HEIGHTS[g], delta=0.002, msg=g)
            self.assertAlmostEqual(p[face, 2].min(), designs.LOW_M, delta=0.002, msg=g)
            self.assertAlmostEqual(2 * np.abs(p[face, 0]).max(), designs.WIDTHS[g], delta=0.002, msg=g)
            self.assertTrue((p[face, 1] < 0).all(), g)
            self.assertTrue(((uv[face] >= 0) & (uv[face] <= 1)).all(), g)
            self.assertIn("bne_cable_steel", tris, g)

    def test_the_sizes_are_the_vwv_tables(self):
        """VwV-StVO zu den §§ 39 bis 43 Rn. 13: triangle side 630 / 900 / 1260, square side 420 / 600 / 840 mm;
        the octagon VzKat Teil 3 (900 Standardgröße, 1050 Übergröße); the 1002 plate Höhe 3 (420 / 600 / 750)."""
        self.assertEqual(designs.SIZE["z205"], (0.63, 0.90, 1.26))
        self.assertEqual(designs.SIZE["z301"], (0.63, 0.90, 1.26))
        self.assertEqual(designs.SIZE["z306"], (0.42, 0.60, 0.84))
        self.assertEqual(designs.SIZE["z307"], (0.42, 0.60, 0.84))
        self.assertEqual(designs.SIZE["z206"][1:], (0.90, 1.05))
        self.assertEqual(designs.SIZE["plate"], (0.42, 0.60, 0.75))
        p, _, tris = dae("de_z306")      # (the square on its corner: its diagonal, less its rounded corners)
        face = np.unique(tris["bne_de_signs"])
        self.assertAlmostEqual(np.ptp(p[face, 2]), 0.6 * 2 ** 0.5, delta=0.04)


class SheetTest(unittest.TestCase):
    def test_sheet_colours(self):
        im = sheet()
        self.assertEqual(im.size, (1024, 1024))
        r_in = 0.9 / (2 * 3 ** 0.5)         # Z 205 / 301: the triangle's inradius
        checks = (("z205", 0.0, 0.0, RAL_9016), ("z205", 0.0, r_in - 0.05, RAL_3020),
                  ("z301", 0.0, -(r_in - 0.05), RAL_3020), ("z301", 0.2, -0.13, RAL_9016),
                  ("z206", 0.0, 0.30, RAL_3020), ("z306", 0.0, 0.0, RAL_1023), ("z306", 0.0, 0.30, RAL_9016),
                  ("z307", 0.078, -0.078, RAL_1023))
        for name, x, z, ref in checks:
            got = lab(at(im, name, x, z))
            self.assertLess(math.dist(got, ref), DELTA_E, (name, x, z, got, ref))
        # (307's middle bar, 301's symbol, a plate's road, the T's stem)
        for name, x, z, ref in (("z307", 0.0, 0.0, RAL_9017), ("z301", 0.0, 0.10, RAL_9017),
                                ("p0", -0.17, 0.0, RAL_9017), ("z206", -0.097, -0.05, RAL_9016)):
            got = lab(at(im, name, x, z))
            self.assertLess(math.dist(got, ref), DELTA_E, (name, x, z, got, ref))
        used = {rgb for _, rgb in im.getcolors(4096)}
        self.assertLessEqual(len(used), build_de_signs.COLOURS)
        self.assertLessEqual({SRGB_3020, SRGB_9016, SRGB_1023, SRGB_9017}, used, "RAL's four, exactly")

    def test_plates_bold_pair_from_the_drivers_view(self):
        """designs.PLATES k: the other arms' slots and the priority pair's, D the approach at the bottom, L left, A
        ahead, R right (native dejunction.rs: the driver's right is (inc.y, -inc.x)). The pair is drawn bold (100 mm
        at Größe 2), the other roads thin (50 mm), no road where there is none. E.g. 0 LR:DL: bold from the bottom to
        the left, thin to the right, nothing ahead (VzKat 1002-13)."""
        im = sheet()
        slots = {"D": (0.0, -1.0), "L": (-1.0, 0.0), "A": (0.0, 1.0), "R": (1.0, 0.0)}
        for k, pat in enumerate(designs.PLATES):
            others, pair = pat.split(":")
            roads = set(others) | {"D"}
            for s, (dx, dz) in slots.items():
                ox, oz = -dz * 0.04, dx * 0.04          # 40 mm across the arm
                mid = lum(at(im, f"p{k}", dx * 0.17, dz * 0.17))
                off = lum(at(im, f"p{k}", dx * 0.17 + ox, dz * 0.17 + oz))
                if s in pair:
                    self.assertLess(max(mid, off), 90, (k, pat, s, "bold"))
                elif s in roads:
                    self.assertLess(mid, 90, (k, pat, s, "thin"))
                    self.assertGreater(off, 180, (k, pat, s, "thin, not bold"))
                else:
                    self.assertGreater(min(mid, off), 180, (k, pat, s, "no road"))


class CollisionTest(unittest.TestCase):
    def test_collision_boxes_cover_de_groups(self):
        for g in designs.GROUPS:
            self.assertIn(g, furniture.COLLISION_BOXES)
            h, boxes = furniture.COLLISION_BOXES[g]
            self.assertEqual(h, designs.HEIGHTS[g], g)
            x0, y0, z0, x1, y1, z1 = np.asarray(boxes).min(0)[:3].tolist() + np.asarray(boxes).max(0)[3:].tolist()
            self.assertLessEqual(x0, -designs.WIDTHS[g] / 2, g)
            self.assertGreaterEqual(x1, designs.WIDTHS[g] / 2, g)
            self.assertGreaterEqual(z1, designs.HEIGHTS[g], g)

    def test_furniture_has_them_without_designs(self):
        """A fresh interpreter importing furniture alone (not designs) has every de_* box."""
        server = os.path.join(HERE, "..")
        code = ("import sys; sys.path.insert(0, %r); import furniture; assert 'designs' not in sys.modules; "
                "import designsigns; print(sum(g not in furniture.COLLISION_BOXES for g in designsigns.GROUPS), "
                "len(designsigns.GROUPS))" % server)
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=server)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.split(), ["0", "105"])


if __name__ == "__main__":
    unittest.main()
