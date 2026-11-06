"""Asset compatibility, source identity, ownership and final terrain placement."""
import json
import hashlib
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import antennas as a
import antenna_debug as debug
import geodesy
import geotiles
import mapgeom as mg
import roads
import worldprops

FRAME = geodesy.EnuFrame(41.1329323, -104.8398548)
BOX = (-100.0, -100.0, 256.0, 256.0)
LATTICE = {"man_made": "mast", "tower:construction": "lattice", "tower:type": "communication"}


def features(records, kind=mg.POINT):
    """Records are (source id, ENU ring/point, tags, optional class, extra attributes)."""
    records = sorted(records, key=lambda r: r[0])
    geoms = []
    for _, xy, *_ in records:
        xy = np.asarray(xy, np.float64).reshape(-1, 2)
        lat, lon, _ = FRAME.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
        geoms.append((kind, [[np.column_stack((lon, lat))]]))
    feats = mg.build("infrastructure", mg.text_column([r[0] for r in records]), geoms)
    feats.attrs["tags"] = mg.text_column([json.dumps(r[2]) for r in records])
    feats.attrs["class"] = mg.text_column([r[3] if len(r) > 3 else "communication_tower" for r in records])
    keys = {k for r in records if len(r) > 4 for k in r[4]}
    for key in keys:
        feats.attrs[key] = np.asarray([r[4].get(key) if len(r) > 4 else None for r in records], object)
    return feats


def elev(lat, lon):
    return np.full(np.shape(lat), 100.0)


def public_fixture():
    """OSM ground truth in the existing Overture reader's table/attribute form.

    This is a test-only conversion, not a runtime regional provider or extract.
    """
    path = Path(__file__).parent / "fixtures" / "antennas" / "cheyenne_towers.osm.json"
    elements = sorted(json.loads(path.read_text(encoding="utf-8"))["elements"], key=lambda e: e["id"])
    geoms = [(mg.POINT, [[np.array([[e["lon"], e["lat"]]], np.float64)]]) for e in elements]
    feats = mg.build("infrastructure", mg.text_column([f"fixture-{e['id']}" for e in elements]), geoms)
    feats.attrs = {"class": mg.text_column(["communication_tower"] * len(elements)),
                   "tags": mg.text_column([json.dumps(e["tags"]) for e in elements]),
                   "src_dataset": mg.text_column(["OpenStreetMap"] * len(elements)),
                   "src_record": mg.text_column([f"n{e['id']}@{e['version']}" for e in elements])}
    return feats


class MappedData:
    """The existing map-data interface; every cell shares the same small table."""
    def __init__(self, infrastructure):
        self.infrastructure = infrastructure
        self.requests = []

    def cell(self, key, layers=(), kind="demand"):
        self.requests.append((key, tuple(layers), kind))
        return SimpleNamespace(layers={name: self.infrastructure if name == "infrastructure" else mg.empty(name)
                                       for name in layers})


class ClassificationTest(unittest.TestCase):
    def test_verified_cheyenne_lattice_node_needs_no_radio_flag(self):
        # Actual OSM node 3787038673@2: 41.1329323, -104.8398548, retrieved 2026-11-05.
        self.assertEqual(a.classify(LATTICE), a.GROUP)

    def test_explicit_radio_or_tv_lattice_is_supported(self):
        for flag in ("radio", "television", "mobile_phone"):
            self.assertEqual(a.classify({"man_made": "tower", "tower:construction": "lattice",
                                         "communication:" + flag: "yes"}), a.GROUP)

    def test_mobile_ground_tower_class_retains_existing_model(self):
        self.assertEqual(a.classify({}, "mobile_phone_tower"), a.CELL_GROUP)
        self.assertEqual(a.classify({"man_made": "mast", "communication:mobile_phone": "yes",
                                    "tower:construction": "monopole"}), a.CELL_GROUP)

    def test_generic_communication_or_mast_does_not_prove_lattice(self):
        for tags, cls in [({}, "communication_tower"), ({"man_made": "tower"}, "tower"),
                          ({"man_made": "mast"}, ""),
                          ({"man_made": "tower", "communication:radio": "yes"}, "")]:
            self.assertIsNone(a.classify(tags, cls))

    def test_unrelated_and_inactive_structures_are_excluded(self):
        additions = [{"tower:type": "observation"}, {"man_made": "water_tower"},
                     {"power": "tower"}, {"historic": "yes"}, {"man_made": "windmill"},
                     {"power": "generator", "generator:source": "wind"}, {"man_made": "wind_turbine"},
                     {"disused": "yes"}, {"abandoned:man_made": "mast"}, {"radar": "yes"}]
        for tags in additions:
            self.assertIsNone(a.classify(dict(LATTICE, **tags)))
        self.assertIsNone(a.classify(LATTICE, "radar"))

    def test_guyed_cheyenne_mobile_mast_not_replaced_by_freestanding_model(self):
        tags = dict(LATTICE, **{"tower:construction": "guyed_lattice", "communication:mobile_phone": "yes"})
        self.assertIsNone(a.classify(tags, "mobile_phone_tower"))

    def test_rooftop_elevated_and_building_mounts_are_excluded(self):
        for tags in ({"location": "roof"}, {"support": "building"}, {"mounting": "wall"},
                     {"min_height": "8"}, {"building": "yes"}):
            self.assertIsNone(a.classify(dict(LATTICE, **tags)))

    def test_height_units_and_ambiguous_values(self):
        self.assertEqual(a.height_m("60 m"), 60.0)
        self.assertAlmostEqual(a.height_m("200 ft"), 60.96)
        self.assertAlmostEqual(a.height_m("100'"), 30.48)
        for value in (None, "unknown", "20;30", "30-40", "0", "-5", "nan", "inf", "30m above roof"):
            self.assertIsNone(a.height_m(value))


class PlacementTest(unittest.TestCase):
    def make(self, fid="osm-node-3787038673", xy=(0.0, 0.0), tags=None, cls="communication_tower"):
        return features([(fid, xy, dict(LATTICE) if tags is None else tags, cls)])

    def test_actual_cheyenne_coordinates_and_defaults(self):
        diagnostics = []
        rows = a.rows(FRAME, [self.make()], BOX, elev, ground=lambda x, y: np.full(len(x), 17.25),
                      diagnostics=diagnostics)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][:4], [a.GROUP, 0.0, 0.0, 17.25])
        self.assertEqual(rows[0][6], a.CATALOG_HEIGHT)
        got = [d for d in diagnostics if d["generated"]][0]
        self.assertEqual(got["feature_id"], "osm-node-3787038673")
        self.assertEqual(got["osm_ids"], ["node/3787038673"])
        self.assertAlmostEqual(got["latitude"], 41.1329323, places=7)
        self.assertAlmostEqual(got["longitude"], -104.8398548, places=7)
        self.assertEqual(got["asset"], a.ASSET_PATH)
        self.assertEqual(got["height_source"], "model_default")
        self.assertEqual(got["model_scale"], 1.0)

    def test_mapped_height_excludes_buried_foundation(self):
        rows = a.rows(FRAME, [self.make(tags=dict(LATTICE, height="80"))], BOX, elev, ground=lambda x, y: x * 0)
        scale = rows[0][6] / a.CATALOG_HEIGHT
        self.assertAlmostEqual(scale * a.ABOVE_GROUND_HEIGHT, 80.0, places=5)
        self.assertEqual(rows[0][3], 0.0)  # no +1.8 m minimum-vertex lift

    def test_short_rooftop_sized_lattice_and_bad_height_are_skipped(self):
        for height in ("8", "300", "unknown", "40;50"):
            self.assertEqual(a.rows(FRAME, [self.make(tags=dict(LATTICE, height=height))], BOX, elev), [])

    def test_ground_receives_exact_emitted_rounded_xy_once(self):
        f = features([("one", (1.234, 2.346), LATTICE), ("two", (12.342, 23.457), LATTICE)])
        calls = []

        def ground(x, y):
            calls.append(np.column_stack((x, y)))
            return x * 3.0 + y * 4.0

        def forbidden_elev(lat, lon):
            raise AssertionError("final ground callback must be used")

        rows = a.rows(FRAME, [(f, None)], BOX, forbidden_elev, ground=ground)
        self.assertEqual(len(calls), 1)
        np.testing.assert_array_equal(calls[0], np.asarray([[r[1], r[2]] for r in rows]))
        for r in rows:
            self.assertAlmostEqual(r[3], r[1] * 3.0 + r[2] * 4.0, places=3)

    def test_elevation_fallback_is_at_actual_placement_coordinates(self):
        calls = []

        def sample(lat, lon):
            calls.append((lat.copy(), lon.copy()))
            return np.full(len(lat), 123.0)

        rows = a.rows(FRAME, [self.make(xy=(80.234, 40.456))], BOX, sample)
        la, lo = calls[0]
        e, n, z = FRAME.geodetic_to_enu(la, lo, np.full(len(la), 123.0))
        self.assertAlmostEqual(rows[0][3], float(z[0]), places=3)
        expected_la, expected_lo, _ = FRAME.enu_to_geodetic(np.array([rows[0][1]]), np.array([rows[0][2]]), np.zeros(1))
        np.testing.assert_array_equal(la, expected_la)
        np.testing.assert_array_equal(lo, expected_lo)

    def test_same_source_in_distinct_cells_and_gers_alias_is_deduplicated(self):
        osm = self.make()
        gers = features([("gers-comm", (0.0, 0.0), LATTICE, "communication_tower",
                          {"sources": json.dumps([{"dataset": "OpenStreetMap", "record_id": "n3787038673@2"}])})])
        first = a.rows(FRAME, [osm, gers, osm], BOX, elev)
        second = a.rows(FRAME, [gers, osm], BOX, elev)
        self.assertEqual(len(first), 1)
        self.assertEqual(first, second)

    def test_real_reader_source_columns_and_richer_osm_height_take_precedence(self):
        osm = features([("osm-infrastructure-n003787038673", (0, 0), dict(LATTICE, height="75"))])
        gers = features([("781c0abc-c007", (0, 0), LATTICE, "communication_tower",
                          {"src_dataset": "OpenStreetMap", "src_record": "n3787038673@2"})])
        first = a.rows(FRAME, [gers, osm], BOX, elev)
        self.assertEqual(len(first), 1)
        self.assertAlmostEqual(first[0][6] / a.CATALOG_HEIGHT * a.ABOVE_GROUND_HEIGHT, 75, places=5)
        self.assertEqual(first, a.rows(FRAME, [osm, gers], BOX, elev))

    def test_identical_near_coincident_points_are_deduplicated_conservatively(self):
        f = features([("point-a", (2, 2), LATTICE), ("point-b", (2.04, 2.04), LATTICE)])
        self.assertEqual(len(a.rows(FRAME, [f], BOX, elev)), 1)

    def test_negative_direct_osm_record_suppresses_stale_eligible_source_copy(self):
        old = features([("gers-781c0abc", (0, 0), LATTICE, "communication_tower",
                         {"src_dataset": "OpenStreetMap", "src_record": "n3787038673@1"})])
        for negative in ({"disused": "yes"}, {"location": "roof"}, {"tower:construction": "guyed_lattice"},
                         {"height": "8"}):
            new = features([("osm-infrastructure-n003787038673", (0, 0), dict(LATTICE, **negative),
                             "mast", {"src_dataset": "OpenStreetMap", "src_record": "n3787038673@2"})])
            for layers in ([old, new], [new, old]):
                decisions = []
                self.assertEqual(a.rows(FRAME, layers, BOX, elev, diagnostics=decisions), [])
                chosen = [d for d in decisions if d["reason"] != "superseded_source_record"]
                self.assertEqual(len(chosen), 1)
                self.assertEqual(chosen[0]["source_record"], "n3787038673@2")
                self.assertEqual(chosen[0]["osm_ids"], ["node/3787038673"])
                self.assertFalse(chosen[0]["generated"])

    def test_newest_direct_osm_record_wins_even_when_no_longer_compatible(self):
        old = features([("osm-old-node-3787038673", (0, 0), LATTICE, "mast",
                         {"src_dataset": "OpenStreetMap", "src_record": "n3787038673@1"})])
        new = features([("osm-new-node-3787038673", (0, 0), dict(LATTICE, historic="yes"), "mast",
                         {"src_dataset": "OpenStreetMap", "src_record": "n3787038673@3"})])
        self.assertEqual(a.rows(FRAME, [old, new], BOX, elev), [])

    def test_point_and_closed_tower_footprint_use_point_position(self):
        point = self.make(xy=(3.0, 2.0))
        poly = features([("footprint", [(0, 0), (8, 0), (8, 8), (0, 8), (0, 0)], LATTICE)], mg.POLYGON)
        rows = a.rows(FRAME, [poly, point], BOX, elev)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1:3], [3.0, 2.0])

    def test_distinct_nearby_masts_are_preserved(self):
        f = features([("osm-node-101", (2.0, 2.0), LATTICE), ("osm-node-102", (3.0, 2.0), LATTICE)])
        self.assertEqual(len(a.rows(FRAME, [f], BOX, elev)), 2)

    def test_tile_boundary_and_duplicate_footprint_emit_only_once(self):
        point = self.make(xy=(256.001, 5.0))
        poly = features([("footprint", [(252, 1), (260, 1), (260, 9), (252, 9), (252, 1)], LATTICE)], mg.POLYGON)
        rows = []
        for box in ((0, 0, 256, 256), (256, 0, 512, 256)):
            rows.extend(a.rows(FRAME, [point, poly], box, elev))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], 256.0)

    def test_actual_adjacent_geoboxes_own_each_seam_point_exactly_once(self):
        gx, gy = geotiles.tile_of(41.1329323, -104.8398548)
        boxes = [geotiles.GeoBox(gx, gy), geotiles.GeoBox(gx + 1, gy)]
        _, _, south, _, north, seam = geotiles.info(gx, gy)
        lat = north - (north - south) * 0.005
        ll = [[seam - 1e-8, lat], [seam, lat], [seam + 1e-8, lat]]
        f = mg.build("infrastructure", np.array(["point-a", "point-b", "point-c"]),
                     [(mg.POINT, [[np.array([point])]]) for point in ll])
        # Different metadata keeps these test seam points independent of the 0.1m
        # duplicate rule; each is a distinct ownership probe.
        f.attrs = {"tags": np.array([json.dumps(dict(LATTICE, name=str(i))) for i in range(3)]),
                   "class": np.array(["communication_tower"] * 3)}
        generated = []
        for box in boxes:
            decisions = []
            a.rows(box.frame, [f], box.box, elev, ground=lambda x, y: np.zeros_like(x), diagnostics=decisions)
            generated.extend(d["feature_id"] for d in decisions if d["generated"])
        self.assertEqual(sorted(generated), ["point-a", "point-b", "point-c"])

    def test_rooftop_in_building_and_large_site_polygon_are_omitted(self):
        bld = features([("building", [(-5, -5), (5, -5), (5, 5), (-5, 5), (-5, -5)], {})], mg.POLYGON)
        self.assertEqual(a.rows(FRAME, [self.make()], BOX, elev, buildings=[bld]), [])
        huge = features([("site", [(0, 0), (80, 0), (80, 80), (0, 80), (0, 0)], LATTICE)], mg.POLYGON)
        self.assertEqual(a.rows(FRAME, [huge], BOX, elev), [])

    def test_empty_selected_rows_missing_data_and_nonfinite_ground(self):
        f = self.make()
        self.assertEqual(a.rows(FRAME, None, BOX, elev), [])
        self.assertEqual(a.rows(FRAME, [(f, np.array([], np.int64))], BOX, elev), [])
        self.assertEqual(a.rows(FRAME, [self.make(tags={})], BOX, elev), [])
        self.assertEqual(a.rows(FRAME, [f], BOX, elev, ground=lambda x, y: np.nan), [])

    def test_mobile_default_lift_and_mapped_height(self):
        f = self.make(tags={}, cls="mobile_phone_tower")
        rows = a.rows(FRAME, [f], BOX, elev, ground=lambda x, y: 8.0)
        self.assertEqual(rows[0][0], a.CELL_GROUP)
        self.assertEqual(rows[0][3], 8.02)
        self.assertEqual(rows[0][6], a.CELL_CATALOG_HEIGHT)
        f = self.make(tags={"height": "40"}, cls="mobile_phone_tower")
        rows = a.rows(FRAME, [f], BOX, elev, ground=lambda x, y: 8.0)
        scale = rows[0][6] / a.CELL_CATALOG_HEIGHT
        self.assertAlmostEqual(scale * a.CELL_ABOVE_GROUND_HEIGHT, 40.0, places=5)
        self.assertAlmostEqual(rows[0][3], 8.0 + a.CELL_BASE_LIFT * scale, places=3)


class WorldwideTileTest(unittest.TestCase):
    def test_public_fixture_keeps_source_identity_and_compatible_structure_only(self):
        path = Path(__file__).parent / "fixtures" / "antennas"
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(hashlib.sha256((path / manifest["fixture"]["file"]).read_bytes()).hexdigest(),
                         manifest["fixture"]["sha256"])
        decisions = []
        rows = a.rows(FRAME, public_fixture(), BOX, elev, ground=lambda x, y: 17.0,
                      diagnostics=decisions)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][:4], [a.GROUP, 0.0, 0.0, 17.0])
        generated = next(d for d in decisions if d["generated"])
        self.assertEqual(generated["source_record"], "n3787038673@2")
        self.assertEqual(generated["height_source"], "model_default")
        self.assertTrue(any(d["source_record"] == "n12752446639@1" and d["reason"] == "guyed_not_supported"
                            for d in decisions))

    def test_tile_wrapper_reads_existing_layers_and_uses_final_rounded_ground(self):
        frame = geodesy.EnuFrame(41.1324323, -104.8403548)
        md, sampled = MappedData(public_fixture()), []

        def final(x, y):
            sampled.append(np.c_[x, y])
            return 120 + 0.1*x + 0.2*y

        def forbidden(lat, lon):
            raise AssertionError("the wrapper must use the supplied final terrain")

        with patch.object(roads, "cells_of_tile", wraps=roads.cells_of_tile) as cells:
            got = a.tile_rows(frame, 0, 0, md, forbidden, final)
            cells.assert_called_once_with(frame, 0, 0, margin=20.0)
        self.assertEqual(len(got), 1)  # shared source in several cells is still one tower
        self.assertEqual(len(sampled), 1)
        row = got[0]
        np.testing.assert_array_equal(sampled[0], [row[1:3]])
        self.assertAlmostEqual(row[3], 120 + 0.1*row[1] + 0.2*row[2], delta=0.00051)
        self.assertEqual({layer for _, layers, _ in md.requests for layer in layers}, {"infrastructure", "buildings"})
        self.assertTrue(all(kind == "demand" for _, _, kind in md.requests))

    def test_missing_map_or_empty_existing_layer_emits_no_tower(self):
        self.assertEqual(a.tile_rows(FRAME, 0, 0, None, elev, None), [])
        self.assertEqual(a.tile_rows(FRAME, 0, 0, MappedData(mg.empty("infrastructure")), elev, None), [])

    def test_production_can_disable_legacy_communications_without_removing_water_towers(self):
        infra = features([("legacy-comm", (50, 50), {}, "communication_tower"),
                          ("water", (80, 50), {}, "water_tower")])
        md = MappedData(infra)
        legacy, _ = worldprops.tile_props(FRAME, 0, 0, md, elev)
        replacement, _ = worldprops.tile_props(FRAME, 0, 0, md, elev, include_communications=False)
        self.assertEqual([r[0] for r in legacy], ["celltower", "watertower"])
        self.assertEqual([r[0] for r in replacement], ["watertower"])


class DiagnosticsTest(unittest.TestCase):
    def test_disabled_diagnostics_do_not_serialize_or_log(self):
        with patch.object(debug, "ENABLED", False), patch.object(debug.log, "info") as log:
            debug.antenna({"feature_id": "anything", "model_scale": 1.0})
            log.assert_not_called()

    def test_original_osm_selector_emits_json_with_model_default_and_final_ground(self):
        with patch.object(debug, "ENABLED", True), patch.object(debug, "SELECTORS", {"3787038673"}), \
                self.assertLogs(debug.log, level="INFO") as logs:
            a.rows(FRAME, public_fixture(), BOX, elev, ground=lambda x, y: 17.0, diagnostics=debug.antenna)
        self.assertEqual(len(logs.output), 1)
        record = json.loads(logs.output[0].split(":", 2)[2])
        self.assertEqual(record["system"], "antenna")
        self.assertEqual(record["source_record"], "n3787038673@2")
        self.assertEqual(record["height_source"], "model_default")
        self.assertEqual(record["terrain_z"], 17.0)


if __name__ == "__main__":
    unittest.main()
