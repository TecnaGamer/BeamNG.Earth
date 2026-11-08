"""Audited stock assets, real WWVB IDs, pole connectivity and tile serialization.

Non-OSM fixture names below are deliberately synthetic; no invented OSM IDs.
"""
import json
import math
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).parent)]
import blockworker
import geodesy
import mapgeom
import mappedinfra as mi
import mappedpoles as mp
import powerlines
import roads

FIXTURES = Path(__file__).parent / "fixtures"


def document():
    return json.loads((FIXTURES / "osm_infrastructure_wwvb.json").read_text(encoding="utf-8"))


def features(elements):
    geoms, attrs, ids = [], {"class": [], "tags": [], "src_record": []}, []
    for e in elements:
        tags = e["tags"]
        kind = 1 if e["type"] == "node" else 2
        ll = [[e["lon"], e["lat"]]] if kind == 1 else [[p["lon"], p["lat"]] for p in e["geometry"]]
        geoms.append((kind, [[np.array(ll, float)]]))
        ids.append(str(e["id"]))
        cls = {"pole": "power_pole", "tower": "power_tower", "line": "power_line", "minor_line": "minor_line"}.get(
            tags.get("power"), "communication_tower")
        attrs["class"].append(cls)
        attrs["tags"].append(json.dumps(tags))
        attrs["src_record"].append(("n" if kind == 1 else "w") + str(e["id"]) + "@" + str(e.get("version", 1)))
    f = mapgeom.build("infrastructure", np.array(ids), geoms)
    f.attrs.update({k: np.array(v) for k, v in attrs.items()})
    return f


class MD:
    def __init__(self, f): self.f = f
    def cell(self, key, layers=(), kind="demand"):
        return SimpleNamespace(layers={k: self.f if k == "infrastructure" else None for k in layers})


class StructureTests(unittest.TestCase):
    def test_model_dimensions_anchors_and_texture_references_match_installed_audit(self):
        audit = json.loads((FIXTURES / "infrastructure_assets.json").read_text())
        for asset, (height, bottom) in mi.MODEL.items():
            visual = [b for name, b in audit[asset]["nodes"].items() if not name.lower().startswith("colmesh")]
            low = min(b["min"][2] for b in visual)
            high = max(b["max"][2] for b in visual)
            self.assertAlmostEqual(height, high - low, places=4)
            self.assertAlmostEqual(bottom, low, places=4)
        anchors = audit["pole"]["anchors"]
        np.testing.assert_allclose(mi.POLE_ANCHORS, [anchors[k] for k in ("anchorPoint_11", "anchorPoint_6", "anchorPoint_10")])
        for r in audit.values():
            self.assertEqual(r["up_axis"], "Z_UP")
            self.assertTrue(all(status.startswith("installed") for status in r["texture_references"].values()))
        self.assertEqual({k for k, paths in audit["mast"]["material_files"].items() if not paths},
                         {"italy_bld_old_concrete", "italy_bld_electric_wire"})

    def test_real_mobile_phone_tag_selects_cell_tower_before_mast_mapping(self):
        e = json.loads((FIXTURES / "osm_infrastructure_mast.json").read_text())["elements"][0]
        self.assertEqual(mi.asset_for("mobile_phone_tower", e["tags"], f"osm:node:{e['id']}", e["lat"], e["lon"]), "celltower")
        frame = geodesy.EnuFrame(e["lat"], e["lon"])
        f = features([e])
        with patch("roads.tile_bbox", return_value=(-100., -100., 100., 100.)):
            out = mi.tile_structures(frame, 0, 0, MD(f), lambda la, lo: np.full(np.shape(la), 27.))
            import worldprops
            legacy, _ = worldprops.tile_props(frame, 0, 0, MD(f), lambda la, lo: np.full(np.shape(la), 27.))
        self.assertEqual(legacy, [], "mobile tower must not also be emitted by the old Forest-prop path")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["asset"], "celltower")
        self.assertEqual(out[0]["height"], mi.MODEL["celltower"][0], "absent height uses the measured model")
        self.assertAlmostEqual(out[0]["z"] + mi.MODEL["celltower"][1], 27., places=5)

    def test_mobile_phone_tag_applies_globally_without_changing_electrical_supports(self):
        for la, lo in ((39., -76.), (48., 2.), (35., 139.), (-33., 151.)):
            for cls in ("mast", "tower", "communication_tower", "communication_pole", "unknown"):
                self.assertEqual(mi.asset_for(cls, {"communication:mobile_phone": "yes"}, lat=la, lon=lo), "celltower")
        for tags in ({"power": "pole", "communication:mobile_phone": "yes"},
                     {"power": "tower", "communication:mobile_phone": "yes"}):
            self.assertIsNone(mi.asset_for("tower", tags))
        self.assertIsNone(mi.asset_for("mast", {"communication:mobile_phone": "yes", "disused": "yes"}))
        self.assertEqual(mi.asset_for("mast", {"man_made": "mast", "communication:mobile_phone": "no"}), "mast")
        self.assertFalse(mp._drawable_pole("communication_pole", {"communication:mobile_phone": "yes"}))
        self.assertTrue(mp._drawable_pole("power_pole", {"power": "pole", "communication:mobile_phone": "yes"}))

    def test_node_and_footprint_of_one_structure_are_not_doubled(self):
        frame = geodesy.EnuFrame(48., 10.)
        la, lo, _ = frame.enu_to_geodetic(np.array([-2., 2., 2., -2., -2., .5]),
                                         np.array([-2., -2., 2., 2., -2., .5]), np.zeros(6))
        f = mapgeom.build("infrastructure", np.array(["fixture:footprint", "fixture:node"]),
            [(3, [[np.c_[lo[:5], la[:5]]]]), (1, [[np.array([[lo[5], la[5]]])]])])
        f.attrs.update({"class": np.array(["tower", "tower"]), "tags": np.array(['{"man_made":"tower"}'] * 2)})
        with patch("roads.tile_bbox", return_value=(-100., -100., 100., 100.)):
            out = mi.tile_structures(frame, 0, 0, MD(f), lambda la, lo: np.zeros(np.shape(la)))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["id"], "overture:fixture:node")
        self.assertEqual(out[0]["sourceIds"], ["overture:fixture:node", "overture:fixture:footprint"])
        self.assertAlmostEqual(out[0]["x"], .5, places=5)

    def test_global_asset_selection_and_specialized_exclusions(self):
        for la, lo in ((40., -105.), (50., 10.), (-30., 140.)):
            self.assertEqual(mi.asset_for("mast", {"man_made": "mast"}, lat=la, lon=lo), "mast")
            self.assertEqual(mi.asset_for("tower", {"man_made": "tower"}, lat=la, lon=lo), "tower")
        for subtype in ("observation", "bell_tower", "cooling", "lighting", "watchtower", "water"):
            self.assertIsNone(mi.asset_for("tower", {"man_made": "tower", "tower:type": subtype}))
        for cls in ("power_tower", "water_tower", "radar"):
            self.assertIsNone(mi.asset_for(cls, {"man_made": "tower"}))
        self.assertIsNone(mi.asset_for("tower", {"man_made": "tower", "power": "tower"}))
        self.assertIsNone(mi.asset_for("mast", {"man_made": "mast", "disused": "yes"}))
        self.assertIsNone(mi.asset_for("tower", {"advertising": "billboard"}))

    def test_verified_wwvb_override_excludes_neighboring_wwv_towers(self):
        towers = [e for e in document()["elements"] if e["tags"].get("man_made") == "tower"]
        selected = []
        for e in towers:
            got = mi.asset_for("communication_tower", e["tags"], f"osm:node:{e['id']}", e["lat"], e["lon"])
            self.assertEqual(got, "mast" if e["tags"].get("name", "").startswith("WWVB") else "tower")
            if got == "mast": selected.append(e)
        self.assertEqual(len(selected), 8)
        e = selected[0]
        self.assertEqual(mi.asset_for("tower", e["tags"], "fixture:unlisted", e["lat"], e["lon"]), "tower")
        self.assertEqual(mi.asset_for("tower", e["tags"], f"osm:node:{e['id']}", e["lat"] + .1, e["lon"]), "tower")

    def test_wwvb_source_ids_coordinates_height_and_tile_ownership(self):
        f = features(document()["elements"])
        frame = geodesy.EnuFrame(40.678, -105.047)
        for e in [e for e in mi.wwvb() if e["type"] == "node"]:
            x, y, _ = frame.geodetic_to_enu(e["lat"], e["lon"], 0.)
            tx, ty = math.floor(x / roads.TILE), math.floor(y / roads.TILE)
            out = mi.tile_structures(frame, tx, ty, MD(f), lambda la, lo: np.full(np.shape(la), 1567.))
            r = next(r for r in out if r["id"] == f"osm:node:{e['id']}")
            self.assertEqual(r["asset"], "mast")
            self.assertAlmostEqual(r["lat"], e["lat"], places=7)
            self.assertAlmostEqual(r["lon"], e["lon"], places=7)
            self.assertEqual(r["height"], float(e["tags"]["height"]))
            _, _, grade = frame.geodetic_to_enu(e["lat"], e["lon"], 1567.)
            self.assertAlmostEqual(r["z"] + mi.MODEL["mast"][1] * r["scale"], grade, places=5)
            other = mi.tile_structures(frame, tx + 1, ty, MD(f), lambda la, lo: np.zeros(np.shape(la)))
            self.assertNotIn(r["id"], [p["id"] for p in other])

    def test_missing_height_units_and_invalid_height(self):
        for asset in mi.MODEL:
            self.assertEqual(mi.height({}, None, asset), mi.MODEL[asset][0])
            self.assertEqual(mi.height({"height": "nan"}, np.nan, asset), mi.MODEL[asset][0])
            self.assertAlmostEqual(mi.height({"height": "400 ft"}, None, asset), 121.92)
        r = mi.record("mast", "fixture:mast", 0., 0., 10., {"height": "122"})
        self.assertGreater(r["scale"], 4., "WWVB height is not limited by the vegetation scale cap")

    def test_source_identity_strips_revision_without_fabricating_osm_ids(self):
        e = mi.wwvb()[0]
        self.assertEqual(mi.source_id(f"n{e['id']}@7"), f"osm:node:{e['id']}")
        self.assertEqual(mi.source_id("", "fixture:mast"), "overture:fixture:mast")


class WireTests(unittest.TestCase):
    def setUp(self):
        self.frame = geodesy.EnuFrame(48.55, 11.50)
        self.elev = lambda la, lo: np.zeros(np.shape(la))

    def layer(self, span=40., line=True):
        # Distances are controlled synthetic geometry; real geographic fixture below exercises OSM ingestion.
        coords = [[0., 0.], [span, 0.]]
        la, lo, _ = self.frame.enu_to_geodetic(np.array(coords)[:, 0], np.array(coords)[:, 1], np.zeros(2))
        f = mapgeom.build("infrastructure", np.array(["fixture:left", "fixture:right", "fixture:line"][:3 if line else 2]),
            [(1, [[np.array([[lo[0], la[0]]])]]), (1, [[np.array([[lo[1], la[1]]])]])] +
            ([(2, [[np.c_[lo, la]]])] if line else []))
        f.attrs.update({"class": np.array(["power_pole", "power_pole"] + (["minor_line"] if line else [])),
                        "tags": np.array(["{}"] * (3 if line else 2))})
        return f

    def build(self, f, box=(-100., -100., 300., 100.), rings=()):
        props, wires = [], []
        mp.place(self.frame, box, [f], self.elev, props, wires, None, everywhere=True, building_rings=rings)
        return props, wires

    def test_30_40_and_longer_spans_fit_actual_attachment_points(self):
        for span, asset in ((30., "wire30"), (40., "wire40"), (63., "wire40"), (150., "wire40")):
            props, wires = self.build(self.layer(span))
            self.assertEqual(len(props), 2)
            self.assertTrue(all(r["asset"] == "pole" for r in props))
            self.assertEqual(len(wires), 3)
            self.assertTrue(all(w["asset"] == asset for w in wires))
            for w, anchor in zip(wires, mi.POLE_ANCHORS):
                self.assertAlmostEqual(w["z"] - props[0]["z"], anchor[2], places=5)
                self.assertAlmostEqual(w["end"][2] - props[1]["z"], anchor[2], places=5)
                self.assertAlmostEqual(math.dist([w["x"], w["y"]], w["end"][:2]), span, places=4)
        self.assertEqual(self.build(self.layer(270.))[1], [])

    def test_unlinked_neighbors_and_building_crossings_get_no_wire(self):
        self.assertEqual(self.build(self.layer(line=False))[1], [])
        ring = np.array([[10., -2.], [20., -2.], [20., 2.], [10., 2.]])
        self.assertEqual(self.build(self.layer(), rings=[ring])[1], [])

    def test_explicit_power_tower_never_becomes_a_small_wooden_pole(self):
        f = self.layer()
        f.attrs["tags"] = np.array(['{"power":"tower"}', '{}', '{}'])
        props, wires = self.build(f)
        self.assertEqual(len(props), 1)
        self.assertEqual(wires, [])

    def test_a_pole_near_a_different_line_is_not_assumed_to_be_on_its_way(self):
        f = self.layer()
        # Move only the way vertices a metre to the side. Poles are close, but are not its mapped vertices.
        la, lo, _ = self.frame.enu_to_geodetic(np.array([0., 40.]), np.array([1., 1.]), np.zeros(2))
        f.lonlat[-2:] = np.c_[lo, la]
        self.assertEqual(self.build(f)[1], [])

    def test_terrain_clearance_omits_wires_without_relocating_poles(self):
        def hill(la, lo):
            x, _, _ = self.frame.geodetic_to_enu(la, lo, 0.)
            return np.where((x > 10.) & (x < 30.), 8., 0.)
        self.elev = hill
        props, wires = self.build(self.layer())
        self.assertEqual(len(props), 2)
        self.assertEqual(wires, [])

    def test_duplicate_cells_reversed_ways_and_tiles_have_one_span_owner(self):
        f = self.layer()
        props, wires = [], []
        mp.place(self.frame, (-100., -100., 300., 100.), [f, f], self.elev, props, wires, None, everywhere=True)
        self.assertEqual((len(props), len(wires)), (2, 3))
        self.assertEqual(len({w["id"] for w in wires}), 3)
        left, lw = self.build(f, (-100., -100., 20., 100.))
        right, rw = self.build(f, (20., -100., 300., 100.))
        self.assertEqual(sorted(left + right, key=lambda r: r["id"]), sorted(props, key=lambda r: r["id"]))
        self.assertEqual(lw + rw, wires)
        reverse = f.lonlat.copy(); reverse[-2:] = reverse[-2:][::-1]
        f.lonlat[:] = reverse
        self.assertEqual(self.build(f), (props, wires))

    def test_real_osm_minor_line_only_connects_its_mapped_supports(self):
        elements = document()["elements"]
        selected = next(e for e in elements if e["tags"].get("power") == "minor_line")
        poles = [e for e in elements if e["type"] == "node" and e["id"] in selected["nodes"]]
        self.assertGreater(len(poles), 2)
        f = features(poles + [selected])
        first = poles[0]
        self.frame = geodesy.EnuFrame(first["lat"], first["lon"])
        props, wires = self.build(f, (-5000., -5000., 5000., 5000.))
        self.assertEqual(len(props), len(poles))
        self.assertGreater(len(wires), 0)
        self.assertTrue(all(set(w["supports"]) <= {r["id"] for r in props} for w in wires))


class PipelineTests(unittest.TestCase):
    def test_tile_serializes_asset_records_and_existing_transmission_mesh_separately(self):
        pole = mi.record("pole", "fixture:pole", 0., 0., 2.)
        wire = mi.wire_record("fixture:left", "fixture:right", (0., 0., 9.), (40., 0., 9.), 0)
        old_wire = np.array([[0., 10., 30.], [40., 10., 30.]])
        with tempfile.TemporaryDirectory() as directory:
            path, _ = roads.write_tile(directory, "/test", 1, 0, 0, [], props=[pole], wires=[wire, old_wire],
                                       infrastructure=[pole, pole])
            data = json.loads((Path(directory) / Path(path).name).read_text())
            self.assertEqual(len(data["infrastructure"]), 2)
            self.assertTrue((Path(directory) / Path(data["wire"]["shape"]).name).exists())

    def test_revisit_key_invalidates_only_road_content_without_cache_deletion(self):
        args = ("g17_1_1", 15, True, False, False, False, 0., None, None)
        with patch.object(blockworker, "_md_versions", return_value=None), patch("threedmr.local_signature", return_value=None):
            a = blockworker._geo_content_key(*args)
            with patch.object(mi, "VERSION", mi.VERSION + ":test"):
                self.assertNotEqual(a, blockworker._geo_content_key(*args))
            with patch.object(mp, "EVERYWHERE", not mp.EVERYWHERE):
                self.assertNotEqual(a, blockworker._geo_content_key(*args))

    def test_only_verified_wwvb_radio_ways_are_excluded_from_pylons(self):
        ways = [e for e in document()["elements"] if e["type"] == "way" and e["tags"].get("frequency") == "60000"]
        self.assertEqual(len(ways), 8)
        for e in ways: self.assertTrue(mi.wwvb_radio_line(f"osm:way:{e['id']}"))
        self.assertFalse(mi.wwvb_radio_line("fixture:electrical-line"))
        self.assertEqual(powerlines.G_PYLON, "pylon")
        f = features(ways)
        frame = geodesy.EnuFrame(40.678, -105.047)
        props, wires = [], []
        with patch("roads.cells_of_tile", return_value=["fixture:cell"]), patch.object(powerlines, "_substations", return_value=[]):
            powerlines.tile_lines(frame, 0, 0, MD(f), lambda la, lo: np.zeros(np.shape(la)), props, wires)
        self.assertEqual((props, wires), ([], []))


if __name__ == "__main__":
    unittest.main()
