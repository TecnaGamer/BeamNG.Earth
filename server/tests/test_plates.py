"""The Pennsylvania plate design (tools/generate_pa_plate.py): BeamNG's plate system files, consistent."""
import json
import unittest
from pathlib import Path

from PIL import Image

D = Path(__file__).resolve().parents[2] / "mod" / "beamng_earth" / "vehicles" / "common" / "licenseplates" / "bne_pennsylvania"


class PlateTest(unittest.TestCase):
    def test_files_agree(self):
        part = json.loads((D / "bne_pennsylvania-2-1.jbeam").read_text(encoding="utf-8"))["bne_pennsylvania_2_1"]
        self.assertEqual(part["slotType"], "licenseplate_design_2_1")
        tpl_path = Path(__file__).resolve().parents[2] / "mod" / "beamng_earth" / part["licenseplate_path"]
        tpl = json.loads(tpl_path.read_text(encoding="utf-8"))
        self.assertEqual((tpl["version"], tpl["type"]), (3, "licenseplate"))
        fmt = tpl["format"]["30-15"]
        bg = Path(__file__).resolve().parents[2] / "mod" / "beamng_earth" / fmt["background"]
        self.assertEqual(Image.open(bg).size, tuple(fmt["size"]))
        self.assertEqual(Image.open(bg).size, (512, 256))
        self.assertIn("{plate}", json.dumps(fmt["root"]))


if __name__ == "__main__":
    unittest.main()
