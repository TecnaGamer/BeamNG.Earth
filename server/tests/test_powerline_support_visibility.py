"""Wire-owning tiles retain every required support, including unsafe and off-tile bases."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).parent)]
import geodesy
import mappedinfra
import powerlines as pl
import powersupports as ps
import roads
import test_power_support_types as support_cases


class VisibilityTests(unittest.TestCase):
    def setUp(self):
        self.frame = geodesy.EnuFrame(41.15, -104.91)
        self.flat = lambda la, lo: np.zeros(np.shape(la))

    def build(self, lines, *, box=(0., 0., 256., 256.), clearance=None, subs=(), frame=None):
        frame = frame or self.frame
        geoms = []
        for i, points in enumerate(lines):
            p = np.asarray(points, float)
            la, lo, _ = self.frame.enu_to_geodetic(p[:, 0], p[:, 1], np.zeros(len(p)))
            geoms.append((f"fixture:line:{i}", np.c_[lo, la]))
        props, wires, infrastructure = [], [], []
        with patch.object(pl, "_off_roads", side_effect=lambda f, md, e, n, ll, clear: (e, n)):
            pl._lay(frame, None, self.flat, subs, geoms, pl.G_PYLON, pl.PYLON_H, pl.ARMS,
                    pl.TOWER_ROAD_CLEAR_M, pl.WIRE_R, box, props, wires, clearance,
                    infrastructure=infrastructure)
        return props, wires, infrastructure

    def test_clearance_keeps_a_visible_support_with_collision_disabled(self):
        class Blocked:
            def clear(self, row): return False
        props, wires, supports = self.build([[(50., 100.), (200., 100.)]], clearance=Blocked())
        self.assertEqual(props, [], "unsafe stock colliders stay out of roads")
        self.assertEqual(len(wires), 6)
        self.assertEqual(len(supports), 2)
        self.assertTrue(all(r["asset"] == "pylon" and r["collide"] is False for r in supports))

    def test_native_off_tile_claim_cannot_hide_an_in_tile_wire_junction(self):
        # Reproduces the native claim-before-ownership failure: -1 m claims the
        # coarse dedup cell containing a separate, needed +5 m support.
        pts = np.array([[-1., 100., 0.], [-200., 100., 0.], [5., 100., 0.], [200., 100., 0.]])
        _, wires, supports = self.build([pts[:2, :2], pts[2:, :2]])
        self.assertEqual(len(wires), 6)
        self.assertEqual(len(supports), 2)
        np.testing.assert_allclose(sorted(r["x"] for r in supports), [5., 200.], atol=1e-5)

    def test_nearby_separate_lines_retain_all_their_distinct_supports(self):
        _, wires, supports = self.build([[(10., 100.), (200., 100.)], [(15., 110.), (200., 110.)]])
        self.assertEqual(len(supports), 4)
        self.assertEqual(len({r["id"] for r in supports}), 4)
        self.assertEqual(len(wires), 12)

    def test_exact_shared_junction_is_not_duplicated(self):
        _, wires, supports = self.build([[(10., 100.), (100., 100.)], [(100., 100.), (200., 100.)]])
        self.assertEqual(len(supports), 3)
        self.assertEqual(len(wires), 12)
        self.assertEqual(sum(abs(r["x"] - 100.) < .001 for r in supports), 1)

    def test_visible_span_retains_endpoints_outside_the_owning_tile(self):
        line = [[(-100., 100.), (350., 100.)]]
        props, wires, supports = self.build(line)
        self.assertEqual(props, [], "neither base belongs to this tile")
        self.assertEqual(len(wires), 6)
        self.assertEqual(len(supports), 2, "wire visibility must also deliver both supports")
        self.assertTrue(all(not r["collide"] for r in supports))
        _, _, left = self.build(line, box=(-256., 0., 0., 256.))
        _, _, right = self.build(line, box=(256., 0., 512., 256.))
        self.assertEqual({r["id"] for r in left + right}, {r["id"] for r in supports})
        self.assertTrue(all(r["collide"] for r in left + right))

    def test_station_endpoints_still_use_gantries_instead_of_extra_pylons(self):
        ring = np.array([[90., 90.], [250., 90.], [250., 110.], [90., 110.], [90., 90.]])
        _, wires, supports = self.build([[(10., 100.), (100., 100.), (200., 100.)]],
                                         subs=[("fixture:substation", [ring])])
        self.assertEqual(len(supports), 1)
        self.assertAlmostEqual(supports[0]["x"], 10., places=4)
        self.assertEqual(len(wires), 6)

    def test_wire_owned_wooden_endpoints_keep_the_pole_model(self):
        pts = np.array([[-100., 100., 0.], [350., 100., 0.]])
        la, lo, _ = self.frame.enu_to_geodetic(pts[:, 0], pts[:, 1], 0.)
        overrides = {i: mappedinfra.record("pole", f"fixture:pole:{i}", p[0], p[1], 0.) for i, p in enumerate(pts)}
        supports = ps.support_records(self.frame, np.c_[lo, la], pts, np.array([0, 2]), np.zeros(2, bool),
            overrides, {}, (0., 0., 256., 256.), "pylon", pl.PYLON_H, pl.PYLON_H, None,
            pl.MIN_SPAN_M, pl.MAX_SPAN_M)
        self.assertEqual(len(supports), 2)
        self.assertTrue(all(r["asset"] == "pole" for r in supports))
        self.assertEqual({r["id"] for r in supports}, {r["id"] for r in overrides.values()})

    def test_support_identity_survives_a_different_anchor_frame(self):
        lines = [[(50., 100.), (200., 100.)]]
        _, _, a = self.build(lines, box=(-5000., -5000., 5000., 5000.))
        _, _, b = self.build(lines, box=(-5000., -5000., 5000., 5000.), frame=geodesy.EnuFrame(41.151, -104.911))
        self.assertEqual({r["id"] for r in a}, {r["id"] for r in b})

    def test_support_identity_survives_neighboring_node_metadata_becoming_available(self):
        pts = np.array([[50., 100., 0.], [200., 100., 0.]])
        la, lo, _ = self.frame.enu_to_geodetic(pts[:, 0], pts[:, 1], 0.)
        args = (self.frame, np.c_[lo, la], pts, np.array([0, 2]), np.zeros(2, bool), {})
        rest = ((0., 0., 256., 256.), "pylon", pl.PYLON_H, pl.PYLON_H, None, pl.MIN_SPAN_M, pl.MAX_SPAN_M)
        a = ps.support_records(*args, {}, *rest)
        b = ps.support_records(*args, {0: "fixture:mapped-tower"}, *rest)
        self.assertEqual({r["id"] for r in a}, {r["id"] for r in b})
        self.assertIn(["fixture:mapped-tower"], [r.get("sourceIds") for r in b])

    def test_serialized_supports_replace_their_legacy_forest_duplicates(self):
        props, wires, supports = self.build([[(50., 100.), (200., 100.)]])
        with tempfile.TemporaryDirectory() as directory:
            path, _ = roads.write_tile(directory, "/test", 1, 0, 0, [], props=props, wires=wires,
                                       infrastructure=supports)
            data = json.loads((Path(directory) / Path(path).name).read_text())
        self.assertEqual(len(data["infrastructure"]), 2)
        self.assertNotIn("props", data, "new named supports must not also be drawn by Forest")
        self.assertIn("wire", data)

    def test_warren_wooden_nodes_remain_wood_in_the_new_visibility_payload(self):
        source = support_cases.WarrenSupportTests()
        source.setUp()
        supports = []
        source.place(infrastructure=supports)
        poles = {r["id"] for r in supports if r["asset"] == "pole"}
        expected = {f"osm:node:{e['id']}" for e in source.elements if e["type"] == "node" and e["tags"].get("power") == "pole"}
        self.assertEqual(poles, expected)
        self.assertEqual(len(poles), 156)
        tower_ids = {i for r in supports if r["asset"] == "pylon" for i in r.get("sourceIds", [])}
        self.assertEqual(tower_ids, {f"osm:node:{e['id']}" for e in source.elements
                                    if e["type"] == "node" and e["tags"].get("power") == "tower"})


if __name__ == "__main__":
    unittest.main()
