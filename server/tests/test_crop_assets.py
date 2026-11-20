"""The original crop meshes and their runtime material references ship together."""
import ast
import importlib.util
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[2]
MOD = ROOT / "mod/beamng_earth"
ASSETS = MOD / "art/shapes/crops"
NS = {"c": "http://www.collada.org/2005/11/COLLADASchema"}


class CropAssetIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.definitions = [json.loads(line) for line in (ASSETS / "items.level.json").read_text().splitlines() if line.strip()]
        cls.materials = json.loads((ASSETS / "main.materials.json").read_text())
        cls.cover = next(d for d in cls.definitions if d.get("class") == "GroundCover")
        cls.shapes = sorted(ASSETS.glob("*.dae"))
        cls.material_binding = cls.cover["material"]

    def test_groundcover_references_all_three_packaged_variants(self):
        self.assertEqual(len(self.shapes), 3)
        self.assertGreater(self.cover["radius"], self.cover["dissolveRadius"])
        self.assertGreater(self.cover["maxElements"], 0)
        self.assertFalse(self.cover["shapesCastShadows"])
        references = []
        for plant in self.cover["Types"]:
            with self.subTest(plant=plant):
                path = plant["shapeFilename"]
                self.assertTrue(path.startswith("/art/shapes/crops/"))
                resolved = (MOD / path.lstrip("/")).resolve()
                self.assertEqual(resolved.parent, ASSETS.resolve())
                self.assertTrue(resolved.is_file())
                self.assertTrue(plant["layer"])
                self.assertGreater(plant["probability"], 0)
                self.assertGreater(plant["sizeMin"], 0)
                self.assertGreater(plant["sizeMax"], plant["sizeMin"])
                references.append(resolved)
        self.assertCountEqual(references, [p.resolve() for p in self.shapes])

    def test_green_material_uses_unique_binding_without_texture_dependencies(self):
        material = self.materials[self.cover["material"]]
        self.assertEqual(material["mapTo"], self.material_binding)
        self.assertTrue(material["doubleSided"])
        self.assertEqual(material["class"], "Material")
        red, green, blue, alpha = material["Stages"][0]["baseColorFactor"]
        self.assertGreater(green, red)
        self.assertGreater(red, blue)
        self.assertEqual(alpha, 1)
        for stage in material["Stages"]:
            self.assertFalse(any(key.endswith("Map") and value for key, value in stage.items()))

    def test_collada_material_and_detail_nodes_resolve_to_existing_meshes(self):
        for path in self.shapes:
            with self.subTest(shape=path.name):
                tree = ET.parse(path)
                self.assertEqual(tree.find("c:asset/c:up_axis", NS).text, "Z_UP")
                self.assertEqual(float(tree.find("c:asset/c:unit", NS).get("meter")), 1)
                geometries = {g.get("id"): g for g in tree.findall("c:library_geometries/c:geometry", NS)}
                self.assertEqual(len(geometries), 2)
                nodes = {node.get("name"): node for node in tree.findall(".//c:visual_scene//c:node", NS)}
                self.assertTrue({"base00", "start01", "plant64", "plant8"} <= nodes.keys())
                near = nodes["plant64"].find("c:instance_geometry", NS).get("url")[1:]
                far = nodes["plant8"].find("c:instance_geometry", NS).get("url")[1:]
                self.assertNotEqual(near, far)
                self.assertIn(near, geometries)
                self.assertIn(far, geometries)
                counts = [int(geometries[key].find("c:mesh/c:triangles", NS).get("count")) for key in (near, far)]
                self.assertGreater(counts[0], counts[1])
                self.assertGreater(counts[1], 0)
                self.assertLess(counts[0], 151)
                materials = {m.get("id"): m for m in tree.findall("c:library_materials/c:material", NS)}
                for binding in tree.findall(".//c:instance_material", NS):
                    self.assertEqual(binding.get("symbol"), self.material_binding)
                    target = binding.get("target")[1:]
                    self.assertIn(target, materials)
                    self.assertEqual(materials[target].get("name"), self.material_binding)

    def test_mesh_indices_normals_and_crop_dimensions_are_valid(self):
        for path in self.shapes:
            for geometry in ET.parse(path).findall("c:library_geometries/c:geometry", NS):
                with self.subTest(shape=path.name, detail=geometry.get("id")):
                    mesh = geometry.find("c:mesh", NS)
                    sources = {}
                    for source in mesh.findall("c:source", NS):
                        array = source.find("c:float_array", NS)
                        values = [float(v) for v in array.text.split()]
                        self.assertEqual(len(values), int(array.get("count")))
                        self.assertTrue(all(math.isfinite(v) for v in values))
                        accessor = source.find("c:technique_common/c:accessor", NS)
                        stride = int(accessor.get("stride"))
                        self.assertEqual(stride, 3)
                        self.assertEqual(len(values), int(accessor.get("count")) * stride)
                        sources[source.get("id")] = [tuple(values[i:i + stride]) for i in range(0, len(values), stride)]
                    vertex_source = mesh.find("c:vertices/c:input", NS).get("source")[1:]
                    positions = sources[vertex_source]
                    self.assertGreater(max(p[2] for p in positions), 1.4)
                    self.assertLess(max(p[2] for p in positions), 2.1)
                    self.assertAlmostEqual(min(p[2] for p in positions), 0)
                    span = max(max(p[axis] for p in positions) - min(p[axis] for p in positions) for axis in (0, 1))
                    self.assertGreater(span, 0.5)
                    self.assertLess(span, 2.0)
                    triangles = mesh.find("c:triangles", NS)
                    self.assertEqual(triangles.get("material"), self.material_binding)
                    inputs = {i.get("semantic"): i for i in triangles.findall("c:input", NS)}
                    self.assertEqual(set(inputs), {"VERTEX", "NORMAL"})
                    stride = max(int(i.get("offset")) for i in inputs.values()) + 1
                    indices = [int(i) for i in triangles.find("c:p", NS).text.split()]
                    self.assertEqual(len(indices), int(triangles.get("count")) * 3 * stride)
                    normals = sources[inputs["NORMAL"].get("source")[1:]]
                    for n in normals:
                        self.assertAlmostEqual(sum(v * v for v in n), 1, delta=1e-6)
                    vertices = []
                    for off in range(0, len(indices), stride):
                        pi = indices[off + int(inputs["VERTEX"].get("offset"))]
                        ni = indices[off + int(inputs["NORMAL"].get("offset"))]
                        self.assertGreaterEqual(pi, 0)
                        self.assertLess(pi, len(positions))
                        self.assertGreaterEqual(ni, 0)
                        self.assertLess(ni, len(normals))
                        vertices.append((positions[pi], normals[ni]))
                    for off in range(0, len(vertices), 3):
                        (a, n), (b, _), (c, _) = vertices[off:off + 3]
                        u, v = [b[i] - a[i] for i in range(3)], [c[i] - a[i] for i in range(3)]
                        cross = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
                        magnitude = math.sqrt(sum(value * value for value in cross))
                        self.assertGreater(magnitude, 1e-10)
                        self.assertGreater(sum(cross[i] * n[i] for i in range(3)) / magnitude, 0.999)

    def test_stdlib_generator_recreates_shipped_artifacts_byte_for_byte(self):
        path = ROOT / "tools/make_crop_asset.py"
        syntax = ast.parse(path.read_text(encoding="utf-8"))
        # The optional development preview may use a plotting library; normal imports/build do not.
        for node in syntax.body:
            modules = [alias.name for alias in node.names] if isinstance(node, ast.Import) else \
                      [node.module] if isinstance(node, ast.ImportFrom) else []
            for module in modules:
                self.assertIn(module.split(".")[0], sys.stdlib_module_names)
        spec = importlib.util.spec_from_file_location("crop_asset_test_generator", path)
        generator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(generator)
        with tempfile.TemporaryDirectory() as temp:
            first, second = Path(temp) / "first", Path(temp) / "second"
            generator.build(first)
            generator.build(second)
            shipped = {p.name: p.read_bytes() for p in ASSETS.iterdir() if p.is_file()}
            self.assertEqual({p.name: p.read_bytes() for p in first.iterdir()}, shipped)
            self.assertEqual({p.name: p.read_bytes() for p in second.iterdir()}, shipped)
            self.assertEqual(len({p.read_bytes() for p in first.glob("*.dae")}), 3)

    def test_generic_crop_carrier_preserves_existing_crop_soil_fields(self):
        path = MOD / "levels/beamng_earth/art/terrains/main.materials.json"
        materials = json.loads(path.read_text(encoding="utf-8"))
        crop = materials["bngEarthGroundCrop"]
        generic = materials["bngEarthGroundGenericCrop"]
        identity = {"name", "internalName", "persistentId"}
        self.assertEqual(generic["name"], "bngEarthGroundGenericCrop")
        self.assertEqual(generic["internalName"], "bngEarthGroundGenericCrop")
        self.assertNotEqual(generic["persistentId"], crop["persistentId"])
        self.assertEqual({k: v for k, v in generic.items() if k not in identity},
                         {k: v for k, v in crop.items() if k not in identity})
        self.assertEqual(generic["groundmodelName"], "DIRT")
        self.assertIn("macro_holes", crop["baseColorMacroTex"])
        self.assertEqual(crop["baseColorMacroTexSize"], 16)
        self.assertIn("macro_clumpy", materials["beamngEarthDirt"]["baseColorMacroTex"])
        self.assertEqual(materials["beamngEarthDirt"]["baseColorMacroTexSize"], 32)


if __name__ == "__main__":
    unittest.main()
