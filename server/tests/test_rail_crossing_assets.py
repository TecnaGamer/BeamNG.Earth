"""Lettering on the shipped crossing models must read from the driver's side."""
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

import numpy as np

ART = Path(__file__).resolve().parents[2] / "mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth"
NS = {"c": "http://www.collada.org/2005/11/COLLADASchema"}


class CrossingLetteringTest(unittest.TestCase):
    def test_text_runs_to_the_drivers_right_on_every_lod_and_gate_variant(self):
        for name in ("crossbuck", "flasher", "gate", "gate_l"):
            root = ET.parse(ART / f"bne_rr_{name}.dae")
            count = 0
            for mesh in root.findall(".//c:mesh", NS):
                sources = {s.attrib["id"]: np.fromstring(s.find("c:float_array", NS).text, sep=" ")
                           for s in mesh.findall("c:source", NS)}
                for triangles in mesh.findall("c:triangles", NS):
                    if triangles.attrib["material"] != "bne_rr_crossbuck-mat":
                        continue
                    inputs = triangles.findall("c:input", NS)
                    vertex = next(i for i in inputs if i.attrib["semantic"] == "VERTEX")
                    tex = next(i for i in inputs if i.attrib["semantic"] == "TEXCOORD")
                    positions = mesh.find("c:vertices/c:input", NS).attrib["source"][1:]
                    pts = sources[positions].reshape(-1, 3)
                    uv = sources[tex.attrib["source"][1:]].reshape(-1, 2)
                    stride = 1 + max(int(i.attrib["offset"]) for i in inputs)
                    indices = np.fromstring(triangles.find("c:p", NS).text, sep=" ", dtype=int).reshape(-1, stride)
                    for tri in indices.reshape(-1, 3, stride):
                        p = pts[tri[:, int(vertex.attrib["offset"])]]
                        t = uv[tri[:, int(tex.attrib["offset"])]]
                        # An observer in -Y sees +X to their right. Reconstruct
                        # the atlas's U axis from the shipped triangle itself.
                        axes = np.linalg.solve(np.column_stack((t, np.ones(3))), p)
                        with self.subTest(model=name, triangle=count):
                            self.assertGreater(axes[0, 0], 0)
                            self.assertGreater(axes[1, 2], 0)
                        count += 1
            self.assertEqual(count, 8, name)
