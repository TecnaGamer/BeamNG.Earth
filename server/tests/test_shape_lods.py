"""The shipped COLLADA shapes' detail levels as the engine reads them: the size is a node name's trailing number
(Torque's String::GetTrailingNumber: a '_' or '-' before it is a minus sign, a negative size is never drawn). The
user's report 60 (Schleiden, 2026-11-17): "bne_de_z306" + "32" read as size 30632, so Germany's junction signs drew
only their far level, which has no post: priority diamonds in the air (tools/build_rail_crossing_props.lod_name)."""
from pathlib import Path
import re
import unittest
import xml.etree.ElementTree as ET

MOD = Path(__file__).resolve().parents[2] / "mod/beamng_earth"
NS = "{http://www.collada.org/2005/11/COLLADASchema}"
NOT_DRAWN = ("colmesh", "collmesh", "collision", "nulldetail", "los", "bounds")
MAX_SIZE = 1024         # px: a drawn detail level this large is a name read wrong


def trailing_number(name):
    """(base, size) as the engine splits a node name, size None without a trailing number."""
    m = re.search(r"(\d+)$", name)
    if m is None:
        return name, None
    base, digits = name[:m.start()], m.group(1)
    if base.endswith(("_", "-")):
        return base[:-1], -int(digits)
    return base, int(digits)


class TrailingNumberTest(unittest.TestCase):
    def test_the_engines_reading(self):
        self.assertEqual(trailing_number("bne_de_z30632"), ("bne_de_z", 30632))
        self.assertEqual(trailing_number("bne_de_z306_L32"), ("bne_de_z306_L", 32))
        self.assertEqual(trailing_number("Colmesh_x-1"), ("Colmesh_x", -1))
        self.assertEqual(trailing_number("s_powerpole_3wire_L40"), ("s_powerpole_3wire_L", 40))


class ShapeLodTest(unittest.TestCase):
    def test_drawn_levels_read_right(self):
        files = sorted(MOD.rglob("*.dae"))
        self.assertGreater(len(files), 100)
        bad = [f"{p.relative_to(MOD)}: {b}" for p in files for b in misread(p)]
        self.assertEqual(bad, [])


def misread(path):
    """The drawn detail levels of a shape file whose names the engine reads wrong: a size not in 1..MAX_SIZE, or a level
    named after the file whose name ends in a digit and lost that digit to the size ("bne_se_c1" + "32": 132)."""
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return []
    stem, bad = Path(path).stem, []
    for node in root.iter(NS + "node"):
        if node.find(NS + "instance_geometry") is None:
            continue
        name = node.attrib.get("name") or node.attrib.get("id") or ""
        if name.lower().startswith(NOT_DRAWN):
            continue
        base, size = trailing_number(name)
        if size is None:
            continue
        if not 0 < size <= MAX_SIZE or (name.startswith(stem) and len(base) < len(stem)):
            bad.append(f"{name} -> {base!r} {size}")
    return bad


if __name__ == "__main__":
    unittest.main()
