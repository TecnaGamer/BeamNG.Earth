"""Exercise per-wall photo application through the storefront exporter for every chain."""

import io
import json
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parents[1] / "server"))
from store_match import CHAIN_GROUPS, parking_frontage
from whole_store import CHAINS, build


def texture_and_geometry(path):
    data = path.read_bytes()
    size = struct.unpack_from("<I", data, 12)[0]
    model = json.loads(data[20 : 20 + size])
    view = model["bufferViews"][model["images"][0]["bufferView"]]
    start = 28 + size + view["byteOffset"]
    with Image.open(io.BytesIO(data[start : start + view["byteLength"]])) as image:
        atlas = np.array(image)
    return atlas, model["accessors"], model["meshes"]


def test_wall_observations():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        observations = folder / "walls"
        observations.mkdir()
        doc = ET.Element("osm")
        brands = [
            (key, min(types), name)
            for (key, types), names in CHAIN_GROUPS
            for name in names
        ]
        for i, (key, kind, name) in enumerate(brands, 1):
            refs = []
            points = []
            for k, (x, y) in enumerate([(0, 0), (0.5, 0), (1, 0), (1, 1), (0, 1)]):
                ident = str(10 * i + k)
                lon, lat = -86 + i * 0.001 + x * 0.0006, 36 + y * 0.00025
                node = ET.SubElement(doc, "node", id=ident, lon=str(lon), lat=str(lat))
                if k == 1:
                    ET.SubElement(node, "tag", k="entrance", v="main")
                refs.append(ident)
                points.append([lon, lat])
            way = ET.SubElement(doc, "way", id=str(i))
            for ident in refs + refs[:1]:
                ET.SubElement(way, "nd", ref=ident)
            for k, v in {"building": "retail", "name": name, key: kind}.items():
                ET.SubElement(way, "tag", k=k, v=v)
            record = {
                "way": str(i),
                "wall": points[2:4],
                "material": "brick",
                "color": "#854c37",
            }
            (observations / f"{i}.json").write_text(json.dumps(record))
        source = folder / "sample.osm"
        ET.ElementTree(doc).write(source)
        for label, args in [
            ("before", []),
            ("after", ["--wall-observation", str(observations)]),
        ]:
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "build_stores.py"),
                    str(source),
                    "--output",
                    str(folder / label),
                    *args,
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
        entries = json.loads((folder / "after/local_models.json").read_text())
        assert {entry["title"] for entry in entries} == {name for _, _, name in brands}
        for entry in entries:
            filename = entry["file"]
            before, *geometry = texture_and_geometry(folder / "before" / filename)
            after, *updated_geometry = texture_and_geometry(folder / "after" / filename)
            assert geometry == updated_geometry, filename
            changed = np.any(before != after, axis=2).mean()
            if entry["title"] in CHAINS:
                assert 0 < changed < 0.5, (filename, changed)
            else:
                assert changed == 0, filename


def test_only_observed_elevation_changes():
    ring = np.array([(0.0, 0.0), (40.0, 0.0), (40.0, 22.0), (0.0, 22.0)])
    for name in sorted(CHAINS):

        def paintings(name, records):
            images = []

            def texture(image):
                images.append(np.array(image))
                return len(images) + 2

            build(
                name,
                ring,
                np.array([0.0, -1.0]),
                6.0,
                [],
                lambda *args: None,
                texture,
                photos=records,
            )
            return images

        original = paintings(name, [])
        updated = paintings(name, [(ring[[2, 1]], "#854c37")])
        assert len(original) == len(updated)
        changed = [a for a, b in zip(original, updated) if not np.array_equal(a, b)]
        assert len(changed) == 1, name
        assert changed[0].shape[:2] == (192, 660), name


def test_street_sign_when_entrance_faces_parking():
    from unittest.mock import patch
    ring = np.array([(0., 0.), (24., 0.), (24., 18.), (0., 18.)])
    marker = object()
    for name in ("Taco Bell", "Wendy's"):
        signs = []
        def mesh(points, material, uv=None):
            if material == 99:
                signs.append(np.mean(points, axis=0))
        with patch("whole_store.sign_image", return_value=marker):
            build(name, ring, np.array([0., -1.]), 5., [(np.array([24., 9.]), False)],
                  mesh, lambda image: 99 if image is marker else 3)
        assert len(signs) == 2
        assert any(point[0] > 24 for point in signs)
        assert any(point[1] < 0 for point in signs)


def test_tenant_panel_requires_clear_frontage():
    ring = np.array([[0, 0], [.001, 0], [.001, .001], [0, .001]])
    tenant = np.array([.0005, .00008])
    parking = np.array([[.0005, -.0001]])
    panel = parking_frontage(ring, tenant, parking)
    assert panel is not None
    np.testing.assert_allclose(panel[:, 1], 0, atol=1e-10)
    np.testing.assert_allclose(parking_frontage(ring[::-1], tenant, parking), panel)
    assert parking_frontage(ring, tenant, np.array([[.0005, .0005]])) is None
    assert parking_frontage(ring, np.array([.00008, .00008]), parking) is None


if __name__ == "__main__":
    test_street_sign_when_entrance_faces_parking()
    test_tenant_panel_requires_clear_frontage()
    test_wall_observations()
    test_only_observed_elevation_changes()
    print(
        f"Passed photo-wall export checks for {len(CHAINS)} detailed chains and the legacy model."
    )
