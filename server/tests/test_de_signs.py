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
import designsigns  # noqa: E402
import furniture  # noqa: E402
import build_de_signs  # noqa: E402
import mod_textures_dds  # noqa: E402

TREES = os.path.join(ROOT, "mod", "beamng_earth", "lua", "ge", "extensions", "beamngEarth", "trees.lua")
SHAPES = os.path.join(ROOT, "mod", "beamng_earth", "levels", "beamng_earth", "art", "shapes", "beamngEarth")
# The mod ships the sheets as DDS (tools/mod_textures_dds.py: lossy BC7, no palette); the sheets' own checks run on
# the tool's fresh output (made once here, as the tool makes them now), and the shipped DDS must match it.
SHEET_DDS = os.path.join(SHAPES, "bne_de_signs.dds")
ZONE_SHEET_DDS = os.path.join(SHAPES, "bne_de_zones.dds")
_FRESH = []


def fresh_sheets():
    """(the junction signs' sheet, the zone sheet) as tools/build_de_signs.py makes them, built once a run."""
    if not _FRESH:
        import atexit
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp()
        atexit.register(shutil.rmtree, tmp, True)
        _FRESH.extend(build_sheets(tmp, "fresh"))
    return tuple(_FRESH)


class _Fresh(os.PathLike):
    """A path that is the fresh build's (os.fspath: resolved at use, built on first use)."""
    def __init__(self, k):
        self.k = k

    def __fspath__(self):
        return fresh_sheets()[self.k]


SHEET = _Fresh(0)

# DIN 6171's sign colours (VwV-StVO zu den §§ 39 bis 43 Rn. 20) as the VzKat's pictures show them (vzkat.de's 205,
# 206, 301, 306, 307 and 1002-10, flat patches' medians, 2026-11-06), by their RAL names: 3020 Verkehrsrot, 9016
# Verkehrsweiß, 1023 Verkehrsgelb, 9017 Verkehrsschwarz
SRGB_3020, SRGB_9016 = (0xDE, 0x00, 0x00), (0xEF, 0xEF, 0xEF)
SRGB_1023, SRGB_9017 = (0xFF, 0xB5, 0x00), (0x00, 0x00, 0x00)
SRGB_5017 = (0x00, 0x3A, 0xBD)      # Verkehrsblau: vzkat.de's 215 (Teil 3), its flat blue, 2026-11-07
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


RAL_3020, RAL_9016, RAL_1023, RAL_9017, RAL_5017 = (lab(c) for c in (SRGB_3020, SRGB_9016, SRGB_1023, SRGB_9017,
                                                                    SRGB_5017))


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
    def test_36_assemblies_in_three_sizes(self):
        self.assertEqual(len(designs.ASSEMBLIES), 36)
        self.assertEqual(len(designs.GROUPS), 108)
        self.assertEqual(len(set(designs.GROUPS)), 108)
        want = {"de_z205", "de_z206", "de_z301", "de_z306", "de_z307", "de_z205_z215"} | {
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
        self.assertEqual(designs.PARTS["de_z205_z215_g1"], ("z205", "z215", 0))

    def test_215_under_205(self):
        """A roundabout's entry: Zeichen 215 (a 600 mm disc; 750 above 100 km/h: VwV-StVO zu den §§ 39 bis 43 Rn. 13
        and 15, round signs over 20 km/h Größe 2) its lower edge 2 m up, the 205 GAP_M over it (StVO § 8 Abs. 1a:
        "Zeichen 215 (Kreisverkehr) unter dem Zeichen 205")."""
        self.assertEqual(designs.SIZE["z215"], (0.60, 0.60, 0.75))
        for suffix, d215, d205 in (("_g1", 0.60, 0.63), ("", 0.60, 0.90), ("_g3", 0.75, 1.26)):
            (p0, s0, m0, c0), (p1, s1, m1, c1) = designs.panels("de_z205_z215" + suffix)
            self.assertEqual((p0, s0, m0, p1, s1, m1), ("z215", "z215", d215, "z205", "z205", d205), suffix)
            self.assertAlmostEqual(c0, designs.LOW_M + d215 / 2, places=6)
            self.assertAlmostEqual(c1 + designs.extent("z205", d205)[1], designs.LOW_M + d215 + designs.GAP_M,
                                   places=6)
        self.assertEqual(designs.WIDTHS["de_z205_z215"], designs.WIDTHS["de_z205"])
        x0, z0, x1, z1 = designs.extent("z215", 0.6)
        self.assertEqual((round(x0, 6), round(z0, 6), round(x1, 6), round(z1, 6)), (-0.3, -0.3, 0.3, 0.3))
        edge = designs.edge("z215", 0.6)
        self.assertEqual(len(edge), len(set(edge)), "a circle: no point twice")
        self.assertTrue(all(abs(math.hypot(x, z) - 0.3) < 1e-9 for x, z in edge))


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
                  ("z307", 0.078, -0.078, RAL_1023), ("z215", 0.0, 0.0, RAL_5017), ("z215", 0.0, 0.293, RAL_9016),
                  ("z215", 0.0, -0.19, RAL_9016), ("z215", 0.0, -0.25, RAL_5017))
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
        self.assertLessEqual({SRGB_3020, SRGB_9016, SRGB_1023, SRGB_9017, SRGB_5017}, used, "RAL's five, exactly")

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
        self.assertEqual(out.stdout.split(), ["0", "108"])


ZONE_SHEET = _Fresh(1)
SRGB_GREY = (0x7B, 0x7B, 0x7B)      # 274.2's struck-through ring, digits and ZONE (vzkat.de 274-2-50.gif, flat patches)


def zone_at(im, kind, kmh, x, z):
    """The zone sheet's colour at (x, z) m of the face of Zeichen 274.<kind> for kmh (Größe 2, designsigns.zone_uv)."""
    u, v = designsigns.zone_uv(kind, kmh, x, z)
    w, h = im.size
    return im.getpixel((int(u * w), int((1 - v) * h)))


def sheet_cases():
    """[(the committed sheet, its seeded colours, spots(im) -> [(rgb, want)])]: the junction signs', then the
    zones'."""
    r_in = 0.9 / (2 * 3 ** 0.5)
    ring = build_de_signs.ZONE_RING[0]
    fresh_sheets()          # (built now, before any test patches the tool)
    return [(SHEET, (SRGB_9016, SRGB_3020, SRGB_1023, SRGB_9017),
             lambda im: [(at(im, "z205", 0.0, 0.0), SRGB_9016), (at(im, "z205", 0.0, r_in - 0.05), SRGB_3020),
                         (at(im, "z306", 0.0, 0.0), SRGB_1023), (at(im, "z307", 0.0, 0.0), SRGB_9017)]),
            (ZONE_SHEET, (SRGB_9016, SRGB_3020, SRGB_9017, SRGB_GREY),
             lambda im: [(zone_at(im, 1, 30, 0.0, -0.148), SRGB_9016), (zone_at(im, 1, 30, 0.165, ring), SRGB_3020),
                         (zone_at(im, 1, 30, 0.2825, 0.0), SRGB_9017), (zone_at(im, 2, 30, -0.165, ring), SRGB_GREY)])]


def build_sheets(tmp, tag=""):
    """(the junction signs' sheet, the zone sheet) as the tool makes them now, in tmp."""
    import contextlib
    import io
    a, b = os.path.join(tmp, f"de{tag}.png"), os.path.join(tmp, f"zones{tag}.png")
    with contextlib.redirect_stdout(io.StringIO()):
        build_de_signs.main(a, b)
    return a, b


class ZoneSheetTest(unittest.TestCase):
    """bne_de_zones.png (tools/build_de_signs.py's second sheet): Zeichen 274.1 and 274.2 for the zones of
    designsigns.ZONE_KMH, as the VzKat's pictures (Teil 3, 274-1-50.gif and 274-2-50.gif) show them."""

    def at(self, im, kind, kmh, x, z):
        return zone_at(im, kind, kmh, x, z)

    def test_zone_faces(self):
        im = Image.open(ZONE_SHEET).convert("RGB")
        self.assertEqual(im.size, designsigns.ZONE_SHEET)
        ring = build_de_signs.ZONE_RING[0]
        for kmh in designsigns.ZONE_KMH:
            for x, z, want in ((0.165, ring, SRGB_3020), (0.0, ring + 0.12, SRGB_9016), (0.0, -0.148, SRGB_9016),
                               (0.2825, 0.0, SRGB_9017), (0.0935, -0.205, SRGB_9017)):
                self.assertEqual(self.at(im, 1, kmh, x, z), want, (274.1, kmh, x, z))
            for x, z, want in ((-0.165, ring, SRGB_GREY), (0.2825, 0.0, SRGB_9017), (0.0, 0.0, SRGB_9017),
                               (-0.2, -0.05, SRGB_9016)):
                self.assertEqual(self.at(im, 2, kmh, x, z), want, (274.2, kmh, x, z))
            (_, x0, x1), z1 = build_de_signs.digit_boxes(kmh)[-1], build_de_signs.DIGIT_Z[1]
            self.assertLess(lum(self.at(im, 1, kmh, (x0 + x1) / 2, z1 - 0.012)), 60, (kmh, "a black digit: its top"))
            self.assertGreater(lum(self.at(im, 1, kmh, (x0 + x1) / 2, ring)), 200, (kmh, "the 0's white middle"))
            h = 2 ** -0.5                   # (half a pitch across the bars, 0.2 m down along them: the panel's white)
            self.assertGreater(lum(self.at(im, 2, kmh, (0.01055 - 0.2) * h, (-0.01055 - 0.2) * h)), 200,
                               (kmh, "white between the bars"))
            self.assertLess(lum(self.at(im, 2, kmh, (0.0211 - 0.2) * h, (-0.0211 - 0.2) * h)), 60, (kmh, "a bar"))
        used = {rgb for _, rgb in im.getcolors(4096)}
        self.assertLessEqual(len(used), build_de_signs.COLOURS)
        self.assertLessEqual({SRGB_3020, SRGB_9016, SRGB_9017, SRGB_GREY}, used, "the VzKat's four, exactly")
        cells = set()
        for kind in (1, 2):
            for kmh in designsigns.ZONE_KMH:
                u0, v0, u1, v1 = designsigns.zone_cell(kind, kmh)
                w, h = im.size
                cells.add(im.crop((round(u0 * w), round((1 - v1) * h), round(u1 * w), round((1 - v0) * h))).tobytes())
        self.assertEqual(len(cells), 6, "six faces")

    def test_the_tool_makes_the_sheets(self):
        """The committed sheets are the tool's output (decoded, not byte for byte, since FreeType's
        anti-aliasing and zlib differ between Pillow builds): a fresh build has their size and mode, the seeded VzKat
        colours exactly in its palette and at their spots, and differs from them by edges at most; two builds here are
        byte for byte the same (the tool is deterministic)."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            made = [build_sheets(tmp, n) for n in "ab"]
            for k, (kept, base, spots) in enumerate(sheet_cases()):
                with open(made[0][k], "rb") as f, open(made[1][k], "rb") as g:
                    self.assertEqual(f.read(), g.read(), "a rebuild is byte for byte the same")
                self.assertEqual(sheet_differences(made[0][k], kept, base, spots), [], kept)
            # the shipped DDS are these sheets (BC7: within its error)
            for k, dds in enumerate((SHEET_DDS, ZONE_SHEET_DDS)):
                self.assertGreater(mod_textures_dds.texture_psnr(Image.open(made[0][k]).convert("RGB"), dds), 40.0, dds)

    def test_the_comparison_sees_a_wrong_sheet(self):
        """sheet_differences flags a sheet one glyph off on one face (the 30's 3 drawn as an 8 on both zone faces;
        STOP's O as a Q on the junction sheet's 206), a seeded colour moved, and a block of pixels painted over."""
        import tempfile
        from unittest import mock
        (de_kept, de_base, de_spots), (z_kept, z_base, z_spots) = sheet_cases()
        with tempfile.TemporaryDirectory() as tmp:
            eight = {**build_de_signs.DIGITS, 30: (("8",) + build_de_signs.DIGITS[30][0][1:],
                                                     build_de_signs.DIGITS[30][1])}
            with mock.patch.object(build_de_signs, "DIGITS", eight):
                _, z = build_sheets(tmp, "8")
            self.assertTrue(sheet_differences(z, z_kept, z_base, z_spots), "a 3 drawn as an 8")
            q = tuple(("Q",) + box[1:] if box[0] == "O" else box for box in build_de_signs.STOP)
            with mock.patch.object(build_de_signs, "STOP", q):
                de, _ = build_sheets(tmp, "q")
            self.assertTrue(sheet_differences(de, de_kept, de_base, de_spots), "STOP's O as a Q")
            im = Image.open(ZONE_SHEET)
            pal = im.getpalette()
            k = [tuple(pal[i:i + 3]) for i in range(0, len(pal), 3)].index(SRGB_GREY)
            pal[3 * k:3 * k + 3] = [0x80, 0x80, 0x80]
            moved = os.path.join(tmp, "moved.png")
            im.putpalette(pal)
            im.save(moved)
            self.assertTrue(sheet_differences(moved, z_kept, z_base, z_spots), "a seeded colour moved")
            im = Image.open(ZONE_SHEET).convert("RGB")
            im.paste((0, 0, 0), (0, 0, 64, 64))
            painted = os.path.join(tmp, "painted.png")
            im.quantize(48).save(painted)
            self.assertTrue(sheet_differences(painted, z_kept, z_base, z_spots), "a block painted over")


def sheet_differences(made, kept, base, spots):
    """[what differs] between the sheet made and the one kept: size, mode, base colours not exactly in the palette or
    not at their spots (spots(im) -> [(rgb, want)]), more than FreeType's anti-aliasing elsewhere, in any 256 px cell
    (one face: a mean difference over 1 of 255 a channel, or over 0.25 % of its pixels, 164, 64 or more off; a wrong
    glyph on one face is some 500 to 900 of them)."""
    a, b = Image.open(made), Image.open(kept)
    out = []
    if (a.size, a.mode) != (b.size, b.mode):
        return [f"size / mode {a.size} {a.mode} != {b.size} {b.mode}"]
    pal = a.getpalette() or []
    colours = {tuple(pal[i:i + 3]) for i in range(0, len(pal), 3)}
    out += [f"{c} not in the palette" for c in base if c not in colours]
    rgb_a, rgb_b = a.convert("RGB"), b.convert("RGB")
    out += [f"{got} at a spot of {want}" for got, want in spots(rgb_a) if got != want]
    d = np.abs(np.asarray(rgb_a, np.int64) - np.asarray(rgb_b, np.int64))
    cell = build_de_signs.CELL
    for y in range(0, d.shape[0], cell):
        for x in range(0, d.shape[1], cell):
            c = d[y:y + cell, x:x + cell]
            off = int((c.max(axis=2) >= 64).sum())
            if c.mean() > 1.0 or off > 0.0025 * c.shape[0] * c.shape[1]:
                out.append(f"the cell at ({x}, {y}): mean {c.mean():.3f}, {off} px 64 or more off")
    return out


if __name__ == "__main__":
    unittest.main()
