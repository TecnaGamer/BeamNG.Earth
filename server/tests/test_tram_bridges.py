"""Mapped tram bridges retain an embedded slab; ground trams and metro never gain a rail bed."""
import json
import os
import sys
import unittest
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import geodesy
import gradesep
import grading
import mapgeom
import rails
import railways
import roads
from test_roads import FRAME, features


def captured_bridge():
    with open(os.path.join(os.path.dirname(__file__), "fixtures", "bucharest_tram_bridge.json"), encoding="utf-8") as fh:
        way = json.load(fh)["ways"][0]
    f = mapgeom.build("roads", [way["id"]], [(mapgeom.LINE, [[np.asarray(way["lonlat"], np.float64)]])])
    f.attrs = {k: np.array([v], object) for k, v in way["attrs"].items()}
    f.rules = {k: mapgeom.rule_table([(0, value, [a, b]) for value, a, b in rows],
                                    "float" if k in ("width", "level") else "text")
               for k, rows in way["rules"].items()}
    return f


class TramBridgeTest(unittest.TestCase):
    def test_actual_mapped_ranges_generate_visual_and_collision_slabs(self):
        # Overture 2026-09-23.1, Bucharest: w619112524@15 has four bridge ranges,
        # including short ones whose street approaches are not in railways' graph.
        f = captured_bridge()
        bridges = dict(railways._pieces(f, 0))["bridge"]
        self.assertEqual(len(bridges), 4)
        for ll in bridges:
            with self.subTest(start=np.asarray(ll)[0].tolist()):
                ll = np.asarray(ll)
                frame = geodesy.EnuFrame(float(ll[:, 1].mean()) - 0.0007,
                                        float(ll[:, 0].mean()) - 0.0009, 0.0)
                mesh = railways.tile_mesh(frame, 0, 0, [f], lambda xy: np.full(len(xy), 50.0))
                self.assertIsNotNone(mesh, "the mapped tram bridge must not disappear")
                p, t, _uv, mats, m = mesh
                used = {mats[int(i)] for i in np.unique(m)}
                self.assertEqual(used, {railways.CONCRETE, railways.STEEL}, "embedded rails, no mainline ballast/sleepers")
                steel = p[np.unique(t[m == mats.index(railways.STEEL)])]
                self.assertAlmostEqual(float(steel[:, 2].max()), 50.0 + railways.FLUSH_LIFT_M + 0.005, places=5)
                col = railways.collision_mesh(mesh)
                self.assertIsNotNone(col)
                self.assertEqual({col[3][int(i)] for i in np.unique(col[4])}, {railways.CONCRETE})
                np.testing.assert_array_equal(col[1], t[m == mats.index(railways.CONCRETE)])
                self.assertEqual(railways.grade_plans(frame, [f], roads.tile_bbox(0, 0),
                                                     lambda la, lo: np.full(np.shape(la), 50.0)), [])

    def test_only_mapped_tram_bridge_enters_the_3d_and_grading_consumers(self):
        f = features([[(-60, 80), (320, 80)], [(100, -60), (100, 320)]],
                     ["tram", "subway"], subtypes=["rail", "rail"],
                     flags=[(0, "is_bridge", 0.4, 0.6), (1, "is_tunnel")],
                     levels=[(0, 1, 0.4, 0.6), (1, -3)])
        parts = railways.parts(FRAME, [f], roads.tile_bbox(0, 0))
        self.assertEqual([(c, kind) for _fid, c, kind, _xy in parts], [("tram", "bridge")])
        self.assertIsNone(railways.drawn(f, 0), "a tram is not a ballasted mainline")
        self.assertIsNone(gradesep._kind_half(f, 0), "no inferred road underpasses from surface tram crossings")
        self.assertIsNone(gradesep._kind_half(f, 1), "the metro cannot infer a surface bridge")
        self.assertEqual(railways.crossing_props(FRAME, 0, 0, [f], lambda xy: np.full(len(xy), 10.0)), [])
        grader = SimpleNamespace(frame=FRAME, widths=None,
                                 md=SimpleNamespace(cell=lambda *args, **kw: SimpleNamespace(layers={"roads": f})))
        geometry = grading._Planner._cell_enu_calc(grader, None, True)
        self.assertEqual(len(geometry["rail_bridges"]), 1)
        self.assertEqual(geometry["rail_ground"], [], "neither ground tram nor metro becomes a road obstruction")
        np.testing.assert_allclose(geometry["rail_bridges"][0][[0, -1]], parts[0][3][[0, -1]], atol=1e-6)
        chunks = rails.tile_chunks(FRAME, 0, 0, [f])
        self.assertTrue(chunks, "street tram approaches still render")
        for chunk in chunks:
            x = np.asarray(chunk["n"])[:, 0]
            self.assertFalse(np.any((x > 99) & (x < 161)), "no draped duplicate through the mapped bridge interior")

    def test_untagged_ground_tram_over_metro_and_road_tunnel_stays_ground(self):
        f = features([[(-60, 80), (320, 80)], [(100, 40), (100, 120)], [(140, 40), (140, 120)]],
                     ["tram", "subway", "primary"], subtypes=["rail", "rail", "road"],
                     flags=[(1, "is_tunnel"), (2, "is_tunnel")], levels=[(1, -3), (2, -1)])
        self.assertEqual(railways.parts(FRAME, [f], roads.tile_bbox(0, 0)), [])
        self.assertIsNone(railways.tile_mesh(FRAME, 0, 0, [f], lambda xy: np.full(len(xy), 10.0)))
        self.assertEqual(railways.grade_plans(FRAME, [f], roads.tile_bbox(0, 0),
                                             lambda la, lo: np.full(np.shape(la), 10.0)), [])
        self.assertTrue(rails.tile_chunks(FRAME, 0, 0, [f]), "mapped surface tram remains street track")

    def test_mapped_subway_overrides_tram_class_even_for_bridges(self):
        # Source priority must remain intact when bridge consumers admit trams.
        f = features([[(0, 80), (250, 80)]], ["tram"], subtypes=["rail"], flags=[(0, "is_bridge")])
        f.attrs["osm_railway"] = np.array(["subway"], object)
        self.assertIsNone(railways.drawn(f, 0, bridges=True))
        self.assertIsNone(railways.tile_mesh(FRAME, 0, 0, [f], lambda xy: np.full(len(xy), 10.0)))


if __name__ == "__main__":
    unittest.main()
