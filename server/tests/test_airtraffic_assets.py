"""The live air traffic's shipped assets (tools/build_airtraffic_shapes.py, tools/build_airtraffic_sounds.py): the bodies'
detail levels, the tinted livery overlays, the materials every shape names, the sound loops and the palettes."""
import json
from pathlib import Path
import re
import unittest
import xml.etree.ElementTree as ET

MOD = Path(__file__).resolve().parents[2] / "mod/beamng_earth"
SHAPES = MOD / "levels/beamng_earth/art/shapes/airtraffic"
SOUNDS = MOD / "levels/beamng_earth/art/sound/airtraffic"
PALETTES = MOD / "lua/ge/extensions/beamngEarth/airtraffic/palettes.lua"
NS = "{http://www.collada.org/2005/11/COLLADASchema}"
PART = re.compile(r"_(liv|gear|cabin)$")


def levels(path):
    """[(size px, triangles, {materials})] of a shape's drawn detail levels, in file order (nearest first)."""
    root = ET.parse(path).getroot()
    geoms = {g.get("id"): g for g in root.iter(NS + "geometry")}
    mat_names = {m.get("id"): m.get("name") for m in root.iter(NS + "material")}
    out = []
    for node in root.iter(NS + "node"):
        ig = node.find(NS + "instance_geometry")
        if ig is None:
            continue
        size = int(re.search(r"(\d+)$", node.get("name")).group(1))
        tris = geoms[ig.get("url")[1:]].findall(f"{NS}mesh/{NS}triangles")
        out.append((size, sum(int(t.get("count")) for t in tris), {mat_names[t.get("material")] for t in tris}))
    return out


class AirTrafficAssetsTest(unittest.TestCase):
    def test_bodies_have_detail_levels(self):
        bodies = sorted(p for p in SHAPES.glob("lat_*.dae")
                        if not PART.search(p.stem) and not p.stem.startswith(("lat_glyph_", "lat_rotor_")))
        self.assertGreaterEqual(len(bodies), 20)
        for p in bodies:
            lv = levels(p)
            with self.subTest(shape=p.name):
                self.assertGreaterEqual(len(lv), 2)
                self.assertTrue(all(1 <= s <= 1024 for s, _, _ in lv))
                self.assertEqual([s for s, _, _ in lv], sorted((s for s, _, _ in lv), reverse=True))
                self.assertGreater(lv[0][1], lv[-1][1])

    def test_liveries_use_only_the_tinted_material(self):
        livs = sorted(SHAPES.glob("lat_*_liv.dae"))
        self.assertGreaterEqual(len(livs), 15)
        self.assertEqual(list(SHAPES.glob("lat_*_liv[0-9]*.dae")), [])
        for p in livs:
            with self.subTest(shape=p.name):
                self.assertEqual(set().union(*(m for _, _, m in levels(p))), {"lat_liv"})
        mats = json.loads((SHAPES / "main.materials.json").read_text(encoding="utf-8"))
        self.assertIs(mats["lat_liv"]["Stages"][0].get("instanceDiffuse"), True)

    def test_every_material_is_defined(self):
        mats = json.loads((SHAPES / "main.materials.json").read_text(encoding="utf-8"))
        used = set()
        for p in SHAPES.glob("*.dae"):
            root = ET.parse(p).getroot()
            used |= {m.get("name") for m in root.iter(NS + "material")}
        self.assertTrue(used)
        self.assertEqual(sorted(used - set(mats)), [])

    def test_sounds_exist(self):
        names = [f"lat_{k}_{layer}.ogg" for k in ("jet", "prop", "piston", "heli") for layer in ("near", "far")]
        names += ["lat_jet_distant.ogg", "lat_jet_close.ogg"]
        for n in names:
            with self.subTest(sound=n):
                self.assertTrue((SOUNDS / n).is_file())
                self.assertGreater((SOUNDS / n).stat().st_size, 1000)

    def test_palettes(self):
        src = PALETTES.read_text(encoding="utf-8")
        entries = re.findall(r"\[(\d+)\]\s*=\s*\{\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*\}", src)
        self.assertEqual(sorted(int(e[0]) for e in entries), list(range(16)))
        self.assertTrue(all(0.0 <= float(c) <= 1.0 for e in entries for c in e[1:]))


if __name__ == "__main__":
    unittest.main()
