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
from whole_store import build, sign_image
from storeidentity import SOURCES, STYLES, identity_for, variant_for


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
        brands = sorted({name: (key, min(types), name)
                         for (key, types), names in CHAIN_GROUPS for name in names}.values())
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
        assert len(entries) == len(brands)
        assert {entry["title"] for entry in entries} == {identity.label for identity in SOURCES.values()}
        for entry in entries:
            filename = entry["file"]
            before, *geometry = texture_and_geometry(folder / "before" / filename)
            after, *updated_geometry = texture_and_geometry(folder / "after" / filename)
            assert geometry == updated_geometry, filename
            changed = np.any(before != after, axis=2).mean()
            assert 0 < changed < 0.5, (filename, changed)


def test_only_observed_elevation_changes():
    ring = np.array([(0.0, 0.0), (40.0, 0.0), (40.0, 22.0), (0.0, 22.0)])
    for name in sorted(SOURCES):

        def paintings(name, records):
            images = []

            def texture(image):
                images.append(np.array(image))
                return len(images) + 2

            build(
                identity_for(name), variant_for(identity_for(name), (0,0)),
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
            build(identity_for(name), "wing", ring, np.array([0., -1.]), 5., [(np.array([24., 9.]), False)],
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


def test_mapped_unit_door_on_split_exterior():
    import whole_store
    from build_stores import export_store
    from store_match import matches
    from unittest.mock import patch
    root = ET.Element("osm")
    for j,(x,y) in enumerate([(0,0),(8,0),(30,0),(48,0),(48,32),(0,32),(30,12),(0,12)]):
        node = ET.SubElement(root, "node", id=str(j), lon=str(x/111320), lat=str(y/111132))
        if j == 1:
            ET.SubElement(node, "tag", k="entrance", v="main")
    for ident,refs,tags in (("host",(0,1,2,3,4,5,0),{"building":"retail"}),
                            ("unit",(0,1,2,6,7,0),{"shop":"clothes","name":"Uniqlo","level":"0","indoor":"room"})):
        way = ET.SubElement(root, "way", id=ident)
        for j in refs:
            ET.SubElement(way, "nd", ref=str(j))
        for k,v in tags.items():
            ET.SubElement(way, "tag", k=k, v=v)
    with tempfile.TemporaryDirectory() as temp:
        folder = Path(temp)
        source = folder/"units.osm"
        ET.ElementTree(root).write(source)
        match, = matches(source)
        assert match[3] is None and len(match[4]) == 1
        with patch("whole_store.elevation", wraps=whole_store.elevation) as paint:
            assert export_store(source, match, {}, folder)["storefront_overlay"]
            _, width, _, doors, *_ = paint.call_args.args
            np.testing.assert_allclose([width,*doors], [30,8], atol=.02)
        door = root.find("node[@id='1']")
        door.remove(door.find("tag[@k='entrance']"))
        ET.ElementTree(root).write(source)
        assert list(matches(source)) == []


def test_sign_font_reuse_keeps_exact_glb_bytes():
    import whole_store
    from build_stores import export_store
    from unittest.mock import patch

    wall = np.array([[0., 0.], [.00015, 0.]])
    match = ("Neighborhood Mall", "mall", wall, None, [], None)
    rows = tuple((f"A{i}", name) for i, name in enumerate(("Bookshop", "Cafe", "Books", "Market", "Gallery", "Goods")))
    panel = ("mall_entrance", 4.5, rows)
    source = Path("synthetic.osm")
    original = whole_store.ImageFont.truetype

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "a").mkdir()
        (root / "b").mkdir()
        with patch.object(whole_store, "_sign_font", side_effect=lambda size: original(whole_store.SIGN_FONT, size)) as uncached:
            first = export_store(source, match, {}, root / "a", local_panel=panel)
        with patch.object(whole_store.ImageFont, "truetype", wraps=original) as loaded:
            second = export_store(source, match, {}, root / "b", local_panel=panel)
        assert 0 < loaded.call_count < uncached.call_count
        assert (root / "a" / first["file"]).read_bytes() == (root / "b" / second["file"]).read_bytes()
        assert whole_store._SIGN_FONTS.get() is None


def test_community_catalog_edit():
    from copy import deepcopy
    from unittest import TestCase
    from storeidentity import parse_catalog

    catalog = json.loads((ROOT / "fictional_catalog.json").read_bytes())
    data = deepcopy(catalog)
    example = {"label": "Sample Fictional Grocer", "style": "market", "paint": "#59483c",
               "ink": "#fffdf0", "variants": ["glazed_shed"],
               "matches": {"supermarket": ["Example Source Grocer"]}}
    data["identities"]["sample_grocer"] = example
    _, sources, groups = parse_catalog(data)
    identity = sources["example source grocer"]
    assert identity.label == "Sample Fictional Grocer" and identity.variants == ("glazed_shed",)
    assert any(key == "shop" and "supermarket" in kinds and "Example Source Grocer" in names
               for (key, kinds), names in groups)
    with TestCase().assertRaisesRegex(ValueError, "Duplicate"):
        example["matches"]["supermarket"].append("Kroger")
        parse_catalog(data)
    example["matches"]["supermarket"].pop()
    with TestCase().assertRaisesRegex(ValueError, "variants"):
        example["variants"] = ["drive_in"]
        parse_catalog(data)


def test_individual_store_styles_and_retired_aliases():
    from dataclasses import replace
    from store_match import chain
    identities = [identity_for(n) for n in ("Walmart", "Target", "Macy's", "Burlington")]
    assert len({identity.label for identity in identities}) == 4
    assert len({frozenset(identity.variants) for identity in identities}) == 4
    assert len({sign_image(replace(identity, label="Sample")).tobytes() for identity in identities}) == 4
    cvs, walgreens = (identity_for(n) for n in ("CVS", "Walgreens"))
    assert cvs.label != walgreens.label and cvs.layout == walgreens.layout and cvs.variants == walgreens.variants
    assert chain("Freddy's") is None
    assert chain("Walmart Supercenter") == "Walmart"


def test_catalog_variants_and_identity_across_surfaces():
    from build_stores import directory_rows, export_store
    import entrance_designs
    from unittest.mock import patch
    from whole_store import directory_image
    assert {n.casefold() for _, names in CHAIN_GROUPS for n in names} == SOURCES.keys()
    identity = identity_for("Burger King")
    assert identity.label == "Patty Regent"
    rows = [("101", "Burger King"), ("202", "West Books")]
    for enabled, bookshop in [(True, "West Books"), (False, "Bookshop")]:
        with patch("storeidentity.MAPPED_NAMES", enabled):
            assert directory_rows(rows) == [("101", "Patty Regent"), ("202", bookshop)]
            assert identity_for("West Books", mapped_name=True).label == bookshop
            assert identity_for("West Books").label == "Bookshop"
            assert identity_for("West Books", "mall_banner", mapped_name=True).label == "Bookshop"
            assert identity_for("City Bank ATM", "atm", mapped_name=True).label == "ATM"
    shell = np.array([[0.,0.],[.00034,0.],[.00034,.00021],[0.,.00021]])
    axes = np.array([0.,-1.])
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        sites = {}
        for k in range(200):
            footprint = shell + [k*.001, 0.]
            form = variant_for(identity, footprint.mean(0))
            sites.setdefault(form, footprint)
        assert set(sites) == set(STYLES[identity.style]["variants"])
        geometry = []
        for form, footprint in sites.items():
            args = (Path("sample.osm"), ("Burger King", form, footprint, axes, [], None), {}, out)
            entry = export_store(*args)
            model = out / entry["file"]
            assert (entry["fictional_identity"], entry["storefront_variant"]) == (identity.key, form)
            original = model.read_bytes()
            export_store(*args)
            assert model.read_bytes() == original
            geometry.append(texture_and_geometry(model)[1:])
        assert geometry[0] != geometry[1]
        with patch.object(entrance_designs, "draw", side_effect=AssertionError("shared host")):
            for panel, size in [(None, []), (None, [6.]), (("mall_banner", 5.), [])]:
                args = ("Burger King", "sign", shell[:2], None, [], None, *size)
                with patch("whole_store.sign_image", wraps=sign_image) as render:
                    entry = export_store(Path("test.osm"), args, {}, out, local_panel=panel)
                    assert entry["title"] == identity.label and entry["storefront_overlay"]
                    assert all(call.args[0] == identity for call in render.call_args_list) and render.called
            with patch("whole_store.directory_image", wraps=directory_image) as board:
                args = ("Real Mall Name", "mall", shell[:2], None, [], None)
                panel = ("mall_entrance", 5., ("Burger King", "West Books"))
                entry = export_store(Path("test.osm"), args, {}, out, local_panel=panel)
                assert entry["title"] == "Shopping Center"
                assert board.call_args.args[0] == directory_rows(["Burger King", "West Books"])


def test_families_have_distinct_frontage_at_the_same_site():
    from dataclasses import replace
    from whole_store import elevation
    sample = identity_for("Walmart")
    assert {identity.style for identity in SOURCES.values()} == set(STYLES)
    fronts = {elevation(replace(sample, style=style), 24., 6., [12.], [], [4., 20.]).tobytes()
              for style in STYLES}
    assert len(fronts) == len(STYLES)


def test_offline_category_store_and_shared_host():
    from build_stores import export_store
    from store_match import matches
    from unittest.mock import patch
    doc = ET.Element("osm")
    for i, (x, y) in enumerate([(0, 0), (.0003, 0), (.0003, .0002), (0, .0002)], 1):
        node = ET.SubElement(doc, "node", id=str(i), lon=str(x), lat=str(y))
        if i == 2:
            ET.SubElement(node, "tag", k="entrance", v="main")
    way = ET.SubElement(doc, "way", id="88")
    for ref in (1, 2, 3, 4, 1):
        ET.SubElement(way, "nd", ref=str(ref))
    for k, v in {"building":"retail", "amenity":"restaurant", "cuisine":"pizza", "name":"Oak Pie"}.items():
        ET.SubElement(way, "tag", k=k, v=v)
    with tempfile.TemporaryDirectory() as temp:
        out = Path(temp)
        source = out / "test.osm"
        ET.ElementTree(doc).write(source)
        match, = matches(source)
        variants = set()
        for mapped, label in ((True, "Oak Pie"), (False, "Pizzeria")):
            with patch("storeidentity.MAPPED_NAMES", mapped):
                entry = export_store(source, match, {}, out)
            assert entry["title"] == label and entry["storefront_style"] == "pizzeria"
            variants.add(entry["storefront_variant"])
        assert len(variants) == 1
        tenant = ET.SubElement(doc, "node", id="90", lon=".00015", lat=".0001")
        for k, v in {"amenity":"cafe", "name":"Upstairs Coffee", "indoor":"yes"}.items():
            ET.SubElement(tenant, "tag", k=k, v=v)
        ET.ElementTree(doc).write(source)
        assert list(matches(source)) == []


def test_shed_sign_clears_the_projected_roof():
    import entrance_designs

    for form in ("shed_lobby", "glazed_shed"):
        for width, height in ((6., 4.25), (12., 5.7), (23., 9.)):
            faces = []
            entrance = entrance_designs.Entrance(form, "#32484b", "#fff4ec", width)
            entrance_designs.draw(entrance, width, height, lambda *p: p,
                                  lambda points, mat, uv: faces.append(np.array(points)),
                                  lambda image: 3, lambda aspect, h: None)
            sign = faces[-1]
            roof_front = min(2.6, width * .13)
            # Project both upper sign corners toward the lip, from either approach.
            posts = [face for face in faces[:-1] if np.allclose(face[:, 1], roof_front)]
            roof = next(face for face in posts if np.ptp(face[:, 0]) >= width - .01
                        and np.min(face[:, 2]) > height)
            left, right = (np.min(roof[roof[:, 0] == x, 2]) for x in (-width / 2, width / 2))
            for x, depth, top in sign[sign[:, 2] == sign[:, 2].max()]:
                gap = roof_front - depth
                for dx in (-gap, gap):
                    roof_z = np.interp(x + dx, [-width / 2, width / 2], [left, right])
                    assert top + .45 * gap < roof_z, (form, width, x)
            assert sign[:, 2].min() > 3.1 and np.ptp(sign[:, 2]) > .5


def test_sign_bearing():
    from store_match import sign_bearing
    assert sign_bearing("SSE;NNW") == 157.5
    assert sign_bearing("310;130") == 310.
    assert sign_bearing("0") == sign_bearing(0) == 0.
    assert sign_bearing("forward; sw") == 225.
    assert sign_bearing("backward") is None
    assert sign_bearing("NaN") is None
    assert sign_bearing(None) is None


if __name__ == "__main__":
    test_community_catalog_edit()
    test_individual_store_styles_and_retired_aliases()
    test_catalog_variants_and_identity_across_surfaces()
    test_sign_font_reuse_keeps_exact_glb_bytes()
    test_sign_bearing()
    test_shed_sign_clears_the_projected_roof()
    test_offline_category_store_and_shared_host()
    test_families_have_distinct_frontage_at_the_same_site()
    test_street_sign_when_entrance_faces_parking()
    test_tenant_panel_requires_clear_frontage()
    test_mapped_unit_door_on_split_exterior()
    test_wall_observations()
    test_only_observed_elevation_changes()
    print(f"Passed storefront export and wall observation checks for {len(SOURCES)} mapped sources.")
