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
from whole_store import CHAINS, build, sign_image
from brand_signs import LETTERING


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
        assert len(signs) == 4
        # Both faces of the blade share a projecting centre, with a small lateral separation.
        blades = sorted((pt for pt in signs if pt[1] < -.5), key=lambda pt: pt[0])
        assert len(blades) == 2 and np.isclose(blades[1][0] - blades[0][0], .17)
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


def test_signature_shapes_stay_on_standalone_buildings():
    from build_stores import export_store
    import threedmr
    footprint = np.array([[0., 0.], [.0003, 0.], [.0003, .00018], [0., .00018]])
    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp) / "threedmr_cache"
        cache.mkdir()
        for name, low, high in (("Waffle House", 3.5, 3.7), ("Cracker Barrel", 7., 8.6),
                                ("IKEA", 12., 16.), ("Bass Pro Shops", 12., 14.)):
            for overlay in (False, True):
                outline = footprint[:2] if overlay else footprint
                direction = None if overlay else np.array([0., -1.])
                ident = f"{name}-{overlay}"
                entry = export_store(Path("shape.osm"), (name, ident, outline, direction, [], None), {}, cache)
                vertices = threedmr.load_local_model(tmp, entry)[0]
                size = np.ptp(vertices, axis=0)
                if overlay:
                    assert entry["storefront_overlay"] and size[1] <= 1.1 and size[2] < 6, name
                else:
                    assert low <= size[2] <= high, (name, size)


def test_fitted_entrances_and_shared_wall_exports():
    import entrance_designs
    import threedmr
    from build_stores import export_store
    from unittest.mock import patch

    wall = np.array([[0., 0.], [.00034, 0.]])
    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp) / "threedmr_cache"
        cache.mkdir()
        for name, design in entrance_designs.DESIGNS.items():
            assert name in CHAINS
            fit = entrance_designs.fitted_span
            assert fit(name, 40, 20, [20]) == design.span
            assert fit(name, 40, 1, [1]) == 0
            assert fit(name, 40, 20, [20, 21]) == 0
            assert fit(name, 40, 10, [10, 20]) <= 10
            # Shared hosts must retain their roof and mass even for a flagship brand.
            with patch.object(entrance_designs, "draw", side_effect=AssertionError(name)):
                entry = export_store(Path("tenant.osm"), (name, "tenant", wall, None, [], None), {}, cache)
            vertices = threedmr.load_local_model(tmp, entry)[0]
            assert entry["storefront_overlay"] and np.ptp(vertices[:,1]) <= 1.1, name


def test_storefront_signs():
    def sign(aspect, height, surface):
        return np.asarray(sign_image("Domino's", aspect, height, surface))

    # A horizontal lockup puts the red tile left of center. A stacked one centers it.
    for aspect, expected_left in ((4.5, True), (1., False)):
        panel = sign(aspect, 1.2, "facade")
        red = (panel[:, :, 0] > 140) & (panel[:, :, 1] < 65)
        assert (np.where(red)[1].mean() < panel.shape[1] * .4) == expected_left
        assert not np.array_equal(panel, sign(aspect, 1.2, "projecting"))
    compact = sign(2.4, .1, "roadside")
    assert np.array_equal(compact, sign(2.4, 1.2, "projecting"))
    assert not np.array_equal(compact, sign(2.4, 1.2, "roadside"))
    assert set(LETTERING).issubset(CHAINS)
    assert LETTERING["Freddy's Frozen Custard & Steakburgers"] == "Freddy\u2019s"
    for name in ("Apple Store", "Best Buy", "Domino's", "Freddy's Frozen Custard & Steakburgers"):
        for aspect in (1, 4.5):
            pixels = np.asarray(sign_image(name, aspect))
            assert pixels.shape == (200, int(200 * aspect), 3)
            ys, xs = np.where(np.any(pixels != pixels[0, 0], axis=2))
            assert xs.size and ys.size
            if name == "Apple Store":
                assert .7 < np.ptp(xs) / np.ptp(ys) < 1
                assert abs(xs.mean() - pixels.shape[1] / 2) < 12


if __name__ == "__main__":
    test_storefront_signs()
    test_fitted_entrances_and_shared_wall_exports()
    test_signature_shapes_stay_on_standalone_buildings()
    test_street_sign_when_entrance_faces_parking()
    test_tenant_panel_requires_clear_frontage()
    test_wall_observations()
    test_only_observed_elevation_changes()
    print(
        f"Passed photo-wall export checks for {len(CHAINS)} detailed chains and the legacy model."
    )
