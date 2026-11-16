"""bgpaving: Sofia's yellow paving by OpenStreetMap way, drawn in the yellow cobble look."""
import json
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bgpaving  # noqa: E402
import roads  # noqa: E402

TSAR_OSVOBODITEL = "157756008"      # бул. Цар Освободител, colour=yellow
TOTLEBEN = "34603907"               # бул. Ген. Тотлебен: grey granite setts, no colour


class Feats:
    def __init__(self, records):
        self.attrs = {"src_record": np.array(records, object)}

    def __len__(self):
        return len(self.attrs["src_record"])


def spec(kind="cobble"):
    return roads.Spec(9.0, kind, True, "secondary", False, surf="paving_stones")


class YellowPavingTest(unittest.TestCase):
    def test_the_shipped_list(self):
        listed = bgpaving.ways()
        self.assertIn(TSAR_OSVOBODITEL, listed)
        self.assertNotIn(TOTLEBEN, listed)
        with open(bgpaving.PATH, encoding="utf-8") as f:
            self.assertIn("ODbL", json.load(f)["source"])

    def test_only_the_listed_ways_cobbles(self):
        feats = Feats([f"w{TSAR_OSVOBODITEL}@12", f"w{TOTLEBEN}@3", f"w{TSAR_OSVOBODITEL}@12", None])
        got = bgpaving.apply(feats, [spec(), spec(), spec("paved"), None])
        self.assertEqual([None if s is None else s.surf for s in got],
                         [bgpaving.SURF, "paving_stones", "paving_stones", None], "a paved one stays asphalt")

    def test_look(self):
        yellow = roads.look_of(spec()._replace(surf=bgpaving.SURF))
        self.assertEqual(yellow["m"], bgpaving.MATERIAL)
        grey = roads.look_of(spec())
        self.assertEqual({k: v for k, v in yellow.items() if k != "m"}, {k: v for k, v in grey.items() if k != "m"},
                         "the cobble look's strips and tiling")
        self.assertEqual(roads.YELLOW_SURF, bgpaving.SURF)

    def test_painted_yellow_in_the_ground(self):
        # with the carriageways in the ground (roads.ROADS_IN_GROUND, the default) no road mesh is drawn: the street's
        # colour is the ground paint's (in game 2026-11-05: grey cobbles that sounded right)
        import surface
        yellow = spec()._replace(surf=bgpaving.SURF)
        self.assertEqual(surface.photo_kind(yellow), "yellow_paving")
        self.assertEqual(surface.photo_kind(spec()), "cobble")
        self.assertEqual(surface.photo_kind(spec("paved")._replace(surf=bgpaving.SURF)), "paved")
        r, g, b, op = surface.PHOTO_ROAD_RGBA["yellow_paving"]
        self.assertGreater(min(r, g) - b, 50, "ochre, not grey")
        kinds = list(surface.PHOTO_ROAD_RGBA)
        self.assertEqual(kinds.index("paved"), 4, "the native paint (paintlist.rs PAVED_KIND) needs paved fifth")
        # (the last kind over a pixel wins: yellow paving after paved, so the yellow boulevards' bricks run on through
        # their junctions with asphalt streets; the native paint leaves no slot before paved for a sixth kind)
        self.assertEqual(kinds[-1], "yellow_paving")
        self.assertIn("yellow_paving", surface.ROAD_MASK_KINDS)

    def test_the_material_is_in_the_mod(self):
        root = Path(__file__).resolve().parents[2] / "mod" / "beamng_earth"
        mats = root / "levels" / "beamng_earth" / "art" / "shapes" / "beamngEarth" / "paving.materials.json"
        with open(mats, encoding="utf-8") as f:
            m = json.load(f)[bgpaving.MATERIAL]
        self.assertEqual(m["groundType"], "COBBLESTONE", "it drives as cobbles")
        lua = (root / "lua" / "ge" / "extensions" / "beamngEarth" / "roads.lua").read_text(encoding="utf-8")
        self.assertIn("/levels/beamng_earth/art/shapes/beamngEarth/paving.materials.json", lua)


if __name__ == "__main__":
    unittest.main()
