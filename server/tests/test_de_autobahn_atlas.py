"""The German Autobahn signs' own lettering atlas (tools/build_de_autobahn_atlas.py -> server/data/de_sign_font.json,
de_autobahn/de_letters_o.data.png) and its reader server/deautobahnfont.py: DINish and the RWB/RWBA pictograms in one
opacity texture with materials of their own; the shared sign atlas untouched."""
import hashlib
import json
import sys
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "server"))

import deautobahnfont  # noqa: E402
from tools import package  # noqa: E402

SCENERY = "levels/beamng_earth/art/shapes/beamngEarth/de_autobahn/"
MOD = ROOT / "mod/beamng_earth" / SCENERY
SHARED_ATLAS_SHA = "1c79aa7a8c1c4db4802f69fa444a39e6e813721fdc2c5175ba2fdd8845a4cbdc"   # sign_font.json at d5941c7c


class AtlasTest(unittest.TestCase):
    def test_atlas_size_and_uvs(self):
        im = Image.open(MOD / "de_letters_o.data.dds")      # (shipped as DDS, BC4)
        self.assertEqual((im.mode, im.size), ("L", (2048, 1024)))
        cps, glyphs, faces, params, kern = deautobahnfont.tables()
        inked = (glyphs[:, 3] > glyphs[:, 1]) & (glyphs[:, 4] > glyphs[:, 2])
        uv = glyphs[inked, 5:9]
        self.assertTrue(((uv > 0) & (uv <= 1)).all() or ((uv >= 0) & (uv <= 1)).all())
        self.assertTrue((uv[:, 2] > uv[:, 0]).all() and (uv[:, 3] > uv[:, 1]).all())

    def test_tables_shapes(self):
        cps, glyphs, faces, params, kern = deautobahnfont.tables()
        self.assertEqual((cps.dtype, glyphs.dtype, faces.dtype, params.dtype, kern.dtype),
                         (np.dtype(np.int64), np.dtype(np.float64), np.dtype(np.int64), np.dtype(np.float64),
                          np.dtype(np.float64)))
        self.assertEqual(glyphs.shape, (len(cps), 9))
        self.assertEqual(faces.shape, (len(cps),))
        self.assertEqual(params.shape, (3, 2))
        self.assertEqual(kern.shape[1], 4)
        self.assertTrue(set(np.unique(faces)) <= {0, 1, 2})
        self.assertTrue(glyphs.flags.c_contiguous and cps.flags.c_contiguous)

    def test_coverage(self):
        text = ("Neutraubling Regensburg-Universität Ingolstadt-Süd Großmehring Straße STRASSE ẞ Plzeň Wrocław "
                "Liège (A) (CZ) 1000 m – Kösching Würzburg Ærø Øresund Šumava Košice")
        self.assertEqual(deautobahnfont.coverage(text), set())
        self.assertEqual(deautobahnfont.coverage("Z中"), {"中"})

    def test_condensed_face_present(self):
        cps, glyphs, faces, params, kern = deautobahnfont.tables()
        for face in (0, 1):
            have = set(cps[faces == face].tolist())
            self.assertTrue({ord(c) for c in "Regensburg-Ost 0123456789"} <= have, face)
        a0 = glyphs[(faces == 0) & (cps == ord("m")), 0][0]
        a1 = glyphs[(faces == 1) & (cps == ord("m")), 0][0]
        self.assertLess(a1, a0)                 # the condensed face is narrower

    def test_kerning_pairs(self):
        cps, glyphs, faces, params, kern = deautobahnfont.tables()
        pairs = {(int(f), int(a), int(b)): v for f, a, b, v in kern}
        self.assertLess(pairs[(0, ord("T"), ord("o"))], -0.05)     # DINish kerns To by about -0.11 cap

    def test_pictograms_present(self):
        cps, glyphs, faces, params, kern = deautobahnfont.tables()
        have = set(cps[faces == 2].tolist())
        self.assertTrue({"motorway_junction", "interchange", "airport", "harbour", "ferry", "industrial",
                         "train_station", "railroad_freight_terminal", "stadium", "park_and_ride"}
                        <= set(deautobahnfont.PICTOGRAMS))
        self.assertEqual(deautobahnfont.PICTOGRAMS["ferry"], deautobahnfont.PICTOGRAMS["harbour"])
        for cp in deautobahnfont.PICTOGRAMS.values():
            self.assertIn(cp, have)
            self.assertTrue(0xE000 <= cp <= 0xE01F)
        # white ink on the blue panel below U+E010, black ink on a white square from U+E010
        self.assertLess(deautobahnfont.PICTOGRAMS["interchange"], 0xE010)
        self.assertGreaterEqual(deautobahnfont.PICTOGRAMS["airport"], 0xE010)


class MaterialsTest(unittest.TestCase):
    def test_materials_file(self):
        mats = json.loads((MOD / "de_autobahn.materials.json").read_text())
        self.assertEqual(set(mats), set(deautobahnfont.MATERIALS[:-1]))
        self.assertEqual(deautobahnfont.MATERIALS[-1], "bne_exit_metal")
        for name in ("bne_de_letters_white", "bne_de_letters_black"):
            stage = mats[name]["Stages"][0]
            self.assertTrue(mats[name]["alphaTest"])
            self.assertTrue((ROOT / "mod/beamng_earth" / stage["opacityMap"].lstrip("/")).exists())
        self.assertFalse(any(n.startswith(("bne_sign_", "bne_exit_")) for n in mats))

    def test_blue_as_on_the_photos(self):
        """The panel blue against the white legend (linear light, Rec. 709 luminance). The 2023 A93 gantry photos at
        Kreuz Regensburg (overcast): r:g:b 0.02-0.05 : 0.41-0.45 : 1, 0.24-0.35. Tuned live in the game at the A3
        westbound Kreuz Regensburg 2000 m gantry at 08:00 (sunlit face), 12:00 (shaded) and 17:00 (backlit),
        2026-11-09: a shaded face gets about a quarter of a sunlit one's light and the game shifts the blue towards
        violet, so the blue sits at the photos' low end with a little more green. 0.07 (the drawings' #154889) and
        0.15 rendered near black in the shade, 0.33 washed out to sky blue in the sun."""
        mats = json.loads((MOD / "de_autobahn.materials.json").read_text())
        y = lambda c: 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]       # noqa: E731
        blue = mats["bne_de_blue"]["Stages"][0]["baseColorFactor"][:3]
        white = mats["bne_de_white"]["Stages"][0]["baseColorFactor"][:3]
        self.assertTrue(0.2 <= y(blue) / y(white) <= 0.27, y(blue) / y(white))
        self.assertLessEqual(blue[0] / blue[2], 0.06)
        self.assertTrue(0.44 <= blue[1] / blue[2] <= 0.52, blue[1] / blue[2])

    def test_materials_registered(self):
        lua = (ROOT / "mod/beamng_earth/lua/ge/extensions/beamngEarth/roads.lua").read_text()
        self.assertIn("/levels/beamng_earth/art/shapes/beamngEarth/de_autobahn/de_autobahn.materials.json", lua)

    def test_packaging(self):
        client = set(package.tree_files("mod/beamng_earth"))
        for name in ("de_autobahn.materials.json", "de_letters_o.data.dds", "OFL.txt", "NOTICE.md"):
            self.assertIn(SCENERY + name, client)

    def test_shared_atlas_untouched(self):
        self.assertEqual(hashlib.sha256((ROOT / "server/data/sign_font.json").read_bytes()).hexdigest(),
                         SHARED_ATLAS_SHA)


if __name__ == "__main__":
    unittest.main()
