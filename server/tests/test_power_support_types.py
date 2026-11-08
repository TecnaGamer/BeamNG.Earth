"""Real mixed 115 kV ways west of F. E. Warren: node tags, not way voltage, select the support."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).parent)]
import geodesy
import mapgeom
import mappedpoles
import powerlines
import powersupports
from test_mapped_infrastructure import features, MD

FIXTURE = Path(__file__).parent / "fixtures" / "fe_warren_power_supports.json"


class GlobalSupportTests(unittest.TestCase):
    def test_pole_override_is_worldwide_and_preserves_towers(self):
        # Synthetic geometry/identifiers exercise global routing without relocating
        # real OSM records or introducing a location-specific runtime exception.
        locations = ((39., -76.), (51., -114.), (48., 2.), (60., 10.),
                     (35., 139.), (-33., 151.), (84., 20.), (0., 179.9999))
        for lat, lon in locations:
            with self.subTest(latitude=lat, longitude=lon):
                frame = geodesy.EnuFrame(lat, lon)
                la, lo, _ = frame.enu_to_geodetic(np.array([10., 90., 170., 230.]), np.full(4, 100.), np.zeros(4))
                f = mapgeom.build("infrastructure",
                    np.array(["fixture:pole-a", "fixture:pole-b", "fixture:tower-a", "fixture:tower-b", "fixture:line"]),
                    [(1, [[np.array([[x, y]])]]) for x, y in zip(lo, la)] + [(2, [[np.c_[lo, la]]])])
                f.attrs.update({"class": np.array(["power_pole", "power_pole", "power_tower", "power_tower", "power_line"]),
                    "tags": np.array(['{"power":"pole"}'] * 2 + ['{"power":"tower"}'] * 2 +
                                     ['{"power":"line","voltage":"230000","cables":"3"}'])})
                def render(enabled):
                    props, wires = [], []
                    with patch("roads.cells_of_tile", return_value=["fixture:cell"]), \
                            patch("roads.tile_bbox", return_value=(0., 0., 256., 256.)), \
                            patch.object(powerlines, "_substations", return_value=[]), \
                            patch.object(mappedpoles, "EVERYWHERE", enabled):
                        powerlines.tile_lines(frame, 0, 0, MD(f), lambda la, lo: np.zeros(np.shape(la)), props, wires)
                    return props, wires
                before, old_wires = render(False)
                after, wires = render(True)
                wood = [r for r in after if isinstance(r, dict)]
                towers = [r for r in after if not isinstance(r, dict)]
                self.assertEqual({r["id"] for r in wood}, {"overture:fixture:pole-a", "overture:fixture:pole-b"})
                self.assertTrue(all(r["asset"] == "pole" for r in wood))
                self.assertEqual(towers, before[2:], "global pole selection changed a mapped tower")
                self.assertEqual(len(wires), 12)
                for (a, ar), (b, br) in zip(wires[-6:], old_wires[-6:]):
                    np.testing.assert_array_equal(a, b)
                    self.assertEqual(ar, br)


class WarrenSupportTests(unittest.TestCase):
    def setUp(self):
        self.elements = json.loads(FIXTURE.read_text())["elements"]
        self.frame = geodesy.EnuFrame(41.155, -104.907)
        self.ground = lambda la, lo: np.zeros(np.shape(la))

    def source(self, way=None):
        elements = self.elements
        if way is not None:
            line = next(e for e in elements if e["type"] == "way" and e["id"] == way)
            elements = [e for e in elements if e["type"] == "node" and e["id"] in line["nodes"]] + [line]
        selected = [e for e in elements if e["type"] == "way" or e["tags"].get("power") in {"pole", "tower"}]
        return selected, features(selected)

    def place(self, way=None, *, explicit=True, moved=False, box=None, final=None, infrastructure=None):
        elements, f = self.source(way)
        ll = np.array([[e["lon"], e["lat"]] for e in elements if e["type"] == "node"])
        x, y, _ = self.frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.)
        if box is None:
            box = (float(x.min()) - 10., float(y.min()) - 10., float(x.max()) + 10., float(y.max()) + 10.)
        props, wires = [], []
        def move(frame, md, e, n, ll, clear):
            return e + (20. if moved else 0.), n + (20. if moved else 0.)
        with patch("roads.cells_of_tile", return_value=["fixture:source"]), \
                patch("roads.tile_bbox", return_value=box), \
                patch.object(powerlines, "_substations", return_value=[]), \
                patch.object(powerlines, "_off_roads", side_effect=move), \
                patch.object(mappedpoles, "EVERYWHERE", explicit):
            powerlines.tile_lines(self.frame, 0, 0, MD(f), self.ground, props, wires, pole_elev=final,
                                  infrastructure=infrastructure)
        return props, wires, box

    def point(self, element):
        x, y, _ = self.frame.geodetic_to_enu(element["lat"], element["lon"], 0.)
        return np.array([x, y])

    def test_all_156_real_power_pole_nodes_use_wood_despite_115kv_way_voltage(self):
        props, wires, _ = self.place()
        poles = [e for e in self.elements if e["type"] == "node" and e["tags"].get("power") == "pole"]
        wood = {r["id"]: r for r in props if isinstance(r, dict)}
        towers = np.array([r[1:3] for r in props if not isinstance(r, dict) and r[0] == "pylon"])
        self.assertEqual(len(poles), 156)
        self.assertEqual(set(wood), {f"osm:node:{e['id']}" for e in poles})
        for e in poles:
            r = wood[f"osm:node:{e['id']}"]
            self.assertEqual(r["asset"], "pole")
            np.testing.assert_allclose([r["x"], r["y"]], self.point(e), atol=1e-5)
            self.assertGreater(np.linalg.norm(towers - self.point(e), axis=1).min(), 1.)
        self.assertTrue(wires, "changing the support must not remove its mapped electrical circuit")

    def test_real_power_tower_nodes_keep_their_existing_pylon_rows(self):
        before, _, _ = self.place(explicit=False)
        after, _, _ = self.place()
        before = [r for r in before if not isinstance(r, dict)]
        after = [r for r in after if not isinstance(r, dict)]
        checked = 0
        for e in self.elements:
            if e["type"] != "node" or e["tags"].get("power") != "tower":
                continue
            xy = self.point(e)
            old = [r for r in before if np.linalg.norm(np.array(r[1:3]) - xy) < .02]
            for r in old:
                self.assertIn(r, after, f"power=tower {e['id']} changed")
                self.assertEqual(r[0], "pylon")
                checked += 1
        self.assertEqual(checked, 54)

    def test_explicit_pole_tag_wins_at_low_high_and_missing_way_voltage(self):
        line = next(e for e in self.elements if e["type"] == "way" and e["id"] == 158100246)
        expected = None
        for voltage in ("66000", "115000", "230000", None):
            if voltage is None:
                line["tags"].pop("voltage", None)
            else:
                line["tags"]["voltage"] = voltage
            props, _, _ = self.place(158100246)
            wood = sorted([r for r in props if isinstance(r, dict)], key=lambda r: r["id"])
            self.assertTrue(wood)
            self.assertTrue(all(r["asset"] == "pole" for r in wood))
            if expected is None:
                expected = wood
            else:
                self.assertEqual(wood, expected, f"way voltage {voltage} changed a mapped pole's model or placement")

    def test_entire_tower_only_way_has_identical_models_and_wire_geometry(self):
        before, bw, _ = self.place(157467934, explicit=False)
        after, aw, _ = self.place(157467934)
        self.assertEqual(before, after)
        self.assertEqual(len(bw), len(aw))
        for (a, ar), (b, br) in zip(bw, aw):
            np.testing.assert_array_equal(a, b)
            self.assertEqual(ar, br)

    def test_poles_keep_mapped_coordinates_when_pylon_road_clearance_moves_towers(self):
        props, _, _ = self.place(158028370, moved=True)
        for r in props:
            if not isinstance(r, dict):
                continue
            e = next(e for e in self.elements if e["type"] == "node" and r["id"] == f"osm:node:{e['id']}")
            np.testing.assert_allclose([r["x"], r["y"]], self.point(e), atol=1e-5)
        old, _, _ = self.place(158028370, explicit=False, moved=True)
        expected_towers = {e["id"]: self.point(e) + 20. for e in self.elements
                           if e["type"] == "node" and e["tags"].get("power") == "tower"}
        for row in old:
            if any(np.linalg.norm(np.array(row[1:3]) - xy) < .02 for xy in expected_towers.values()):
                current = [r for r in props if not isinstance(r, dict) and r[1:4] == row[1:4]]
                self.assertTrue(current)
                self.assertEqual(current[0], row, "mapped pole replacement changed a neighboring tower's pose")

    def test_mixed_way_wires_attach_to_the_real_wood_mesh_anchors(self):
        props, wires, _ = self.place(158028370)
        wood = [r for r in props if isinstance(r, dict)]
        checked = 0
        for polyline, _ in wires:
            for endpoint in (polyline[0], polyline[-1]):
                near = [r for r in wood if np.linalg.norm(endpoint[:2] - [r["x"], r["y"]]) < 2.]
                if near:
                    expected = [powersupports.attachment(r, c, 3) for r in near for c in range(3)]
                    self.assertLess(np.linalg.norm(np.array(expected) - endpoint, axis=1).min(), 1e-6)
                    checked += 1
        self.assertGreater(checked, 100)

    def test_power_line_poles_use_final_terrain_and_have_single_streamed_identity(self):
        final = lambda la, lo: np.full(np.shape(la), 14.)
        props, _, box = self.place(158100246, final=final)
        wood = [r for r in props if isinstance(r, dict)]
        self.assertTrue(wood)
        for r in wood:
            _, _, grade = self.frame.geodetic_to_enu(r["lat"], r["lon"], 14.)
            self.assertAlmostEqual(r["z"] - .08358 * r["scale"], grade, places=5)
        mid = (box[0] + box[2]) / 2
        left, _, _ = self.place(158100246, box=(box[0], box[1], mid, box[3]), final=final)
        right, _, _ = self.place(158100246, box=(mid, box[1], box[2], box[3]), final=final)
        split = [r for r in left + right if isinstance(r, dict)]
        self.assertEqual(sorted(r["id"] for r in split), sorted(r["id"] for r in wood))
        self.assertEqual(len({r["id"] for r in split}), len(split))


if __name__ == "__main__":
    unittest.main()
