import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np
from photo_match import match_wall
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent


class PhotoMatchTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.extract = self.folder / "sample.osm"
        self.doc = ET.Element("osm")
        self.add_rectangle("1", 0.00015, 0.00035)

    def add_rectangle(self, ident, south, north):
        way = ET.SubElement(self.doc, "way", id=ident)
        for i, (lon, lat) in enumerate(
            [(-0.0002, south), (0.0002, south), (0.0002, north), (-0.0002, north)]
        ):
            node_id = f"{ident}{i}"
            ET.SubElement(self.doc, "node", id=node_id, lon=str(lon), lat=str(lat))
            ET.SubElement(way, "nd", ref=node_id)
        ET.SubElement(way, "nd", ref=f"{ident}0")
        ET.SubElement(way, "tag", k="building", v="retail")
        ET.ElementTree(self.doc).write(self.extract)

    def test_photo_cli_uses_mapped_wall(self):
        image = Image.new("RGB", (240, 144), "#975841")
        draw = ImageDraw.Draw(image)
        for row in range(12):
            y = row * 12
            draw.line((0, y, 239, y), fill="white", width=2)
            for x in range(24 * (row % 2), 240, 48):
                draw.line((x, y, x, y + 11), fill="white", width=2)
        image.save(self.folder / "sample.png")
        output = self.folder / "wall.json"
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "detect_wall_material.py"),
                str(self.folder / "sample.png"),
                "--crop",
                "0",
                "0",
                "240",
                "144",
                "--osm",
                str(self.extract),
                "--camera",
                "0",
                "0",
                "--bearing",
                "0",
                "--source",
                "synthetic",
                "--output",
                str(output),
            ],
            check=True,
        )
        record = json.loads(output.read_text())
        self.assertEqual((record["material"], record["way"]), ("brick", "1"))
        np.testing.assert_allclose(
            record["wall"], [[-0.0002, 0.00015], [0.0002, 0.00015]]
        )

    def test_nearer_building_occludes_store(self):
        self.add_rectangle("2", 0.00005, 0.00010)
        self.assertEqual(match_wall(self.extract, (0, 0), 0)[0], "2")

    def test_ambiguous_or_missing_sight_line(self):
        for camera, bearing in [((0.000198, 0), 0), ((0, 0.0002), 0), ((0, 0), 180)]:
            with (
                self.subTest(camera=camera, bearing=bearing),
                self.assertRaises(ValueError),
            ):
                match_wall(self.extract, camera, bearing)
