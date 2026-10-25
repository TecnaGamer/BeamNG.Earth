"""The US sign models (tools/generate_us_signs.py) and their catalog (us_signs.lua) cover every group the server places
(furniture.speed_signs' mph signs, roadsigns.curve_signs): a group without a model is drawn as a tree (trees.lua)."""
import re
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import furniture  # noqa: E402
import roadsigns  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
ART = ROOT / "mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth"
LUA = (ROOT / "mod/beamng_earth/lua/ge/extensions/beamngEarth/us_signs.lua").read_text(encoding="utf-8")


def lua_list(name):
    return {int(v) for v in re.search(rf"^M\.{name} = \{{([^}}]*)\}}", LUA, re.M).group(1).split(",")}


class CatalogTest(unittest.TestCase):
    def test_every_placed_group_has_a_model(self):
        speeds, adv = lua_list("SPEEDS"), lua_list("ADVISORY")
        self.assertTrue(set(furniture.SIGN_MPH) <= speeds)
        # every advisory speed a bend under an 85 mph limit can get
        possible = {roadsigns.advisory_mph(r) for r in range(1, 2000)}
        possible = {a for a in possible if 85 - a >= roadsigns.ADVISE_MPH}
        self.assertTrue(possible <= adv, sorted(possible - adv))
        for g in ["warn_turn_l", "warn_turn_r", "warn_curve_l", "warn_curve_r", "chevron_l", "chevron_r"]:
            self.assertIn(f"add('warn_' .. kind", LUA) if g.startswith("warn") else None
        names = [f"us_speed{v}" for v in speeds] + [f"us_advisory{v}" for v in adv] + \
            [f"us_warn_{k}_{s}" for k in ("turn", "curve") for s in "lr"] + [f"us_chevron_{s}" for s in "lr"]
        for n in names:
            self.assertTrue((ART / f"{n}.dae").is_file() and (ART / f"{n}.png").is_file(), n)

    def test_models_parse_and_carry_face_metal_and_collision(self):
        ns = {"c": "http://www.collada.org/2005/11/COLLADASchema"}
        for dae in sorted(ART.glob("us_*.dae")):
            root = ET.parse(dae).getroot()
            nodes = [n.get("name") for n in root.iterfind(".//c:visual_scene/c:node", ns)]
            self.assertIn("Colmesh-1", nodes, dae.name)
            mats = {m.get("symbol") for m in root.iterfind(".//c:instance_material", ns)}
            self.assertIn(f"{dae.stem}_material", mats, dae.name)
            self.assertIn("sign_metal_material", mats, dae.name)

    def test_heights_agree(self):
        h = {k: float(v) for k, v in re.findall(r"(\w+) = ([\d.]+)", re.search(r"M\.HEIGHTS = \{([^}]*)\}", LUA).group(1))}
        self.assertEqual(h["warn"], roadsigns.SIGN_H)
        self.assertEqual(h["advisory"], roadsigns.PLAQUE_H)
        self.assertEqual(h["chevron"], roadsigns.CHEVRON_H)
        self.assertEqual(h["speed"], 2.6)       # furniture.speed_signs' rows
        import importlib.util
        spec = importlib.util.spec_from_file_location("gus", ROOT / "tools/generate_us_signs.py")
        self.assertIn("HEIGHTS = {\"speed\": 2.60, \"warn\": 3.05, \"advisory\": 1.90, \"chevron\": 2.20}",
                      (ROOT / "tools/generate_us_signs.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
