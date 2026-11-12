"""Stock foliage may replace only fitting hedge intervals; fallback coverage is never optional."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path[:0] = [os.path.dirname(os.path.dirname(__file__)), os.path.dirname(__file__)]
import barriers
import roads
from test_barriers import ground

BOX = (0, 0, 256, 256)


def hedge(xy=((10, 30), (100, 30)), height=1.8, width=0.8, kind="hedge", gaps=()):
    xy = np.asarray(xy, float)
    return barriers.Run("synthetic", kind, xy, barriers._arc(xy), height, width, kind, barriers.HEDGE, gaps)


def meshes(run, ground_fn=ground, box=BOX, road_lines=()):
    props = []
    m = barriers.tile_meshes([run], box, ground_fn, road_lines, hedge_props=props)
    return m, props


class StockHedgesTest(unittest.TestCase):
    def test_inner_leaf_coverage_uses_cutout_instead_of_an_opaque_box(self):
        root = Path(__file__).resolve().parents[2]
        path = root / 'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/barriers.materials.json'
        materials = json.loads(path.read_text(encoding='utf-8'))
        leaf, core = materials[barriers.HEDGE], materials[barriers.HEDGE_CORE]
        self.assertTrue(core.get('alphaTest'))
        self.assertEqual(core['alphaRef'], leaf['alphaRef'])
        self.assertEqual(core['Stages'][0]['opacityMap'], leaf['Stages'][0]['opacityMap'])

    def test_unfitted_sections_have_leaf_cutouts_and_an_inset_core(self):
        material_file = Path(__file__).parents[2] / "mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/barriers.materials.json"
        material = json.loads(material_file.read_text())[barriers.HEDGE]
        self.assertTrue(material.get("alphaTest", False))
        self.assertEqual(material["alphaRef"], 75)
        self.assertTrue(material["doubleSided"])
        self.assertIn("t_cypress_leaves_distant_o.data.png", material["Stages"][0]["opacityMap"])
        self.assertEqual(material["Stages"][0]["baseColorFactor"], [1, 1, 1, 1])
        for r, gz in [(hedge(((10, 30), (11, 30))), ground),
                      (hedge(((30, 30), (31, 31), (30, 32))), ground),
                      (hedge(), lambda p: p[:, 0] * 0.3)]:
            with self.subTest(xy=r.xy.tolist()):
                m, rows = meshes(r, gz)
                self.assertFalse(rows)
                p, t, uv, mats, mi = m["hedge"][0]
                self.assertIn(barriers.HEDGE_CORE, mats)
                leaf_uv = uv[t[mi == mats.index(barriers.HEDGE)]]
                ab, ac = leaf_uv[:, 1] - leaf_uv[:, 0], leaf_uv[:, 2] - leaf_uv[:, 0]
                area = ab[:, 0] * ac[:, 1] - ab[:, 1] * ac[:, 0]
                self.assertTrue((np.abs(area) > 1e-9).all(), "Leaf caps must not stretch a single texture column")

    def test_leaf_strip_keeps_gate_opening_and_source_station_at_cuts(self):
        r = hedge(gaps=((40, 44, "gate"),))
        m = barriers.tile_meshes([r], BOX, ground)
        for mesh, _ in m.values():
            p = mesh[0]
            self.assertFalse(((p[:, 0] > 50.001) & (p[:, 0] < 53.999)).any())
        r = hedge(((10, 30), (14, 30)))
        left = barriers.tile_meshes([r], (0, 0, 12, 256), ground)["hedge"][0]
        right = barriers.tile_meshes([r], (12, 0, 256, 256), ground)["hedge"][0]
        def cut_vertices(mesh, origin):
            p, t, uv, mats, mi = mesh
            p = p + [origin, 0, 0]
            used = np.unique(t[mi == mats.index(barriers.HEDGE)])
            used = used[np.isclose(p[used, 0], 12) & (np.abs(p[used, 1] - 30) < 0.39)]
            return {tuple(row) for row in np.round(np.c_[p[used], uv[used]], 6)}
        self.assertGreaterEqual(len(cut_vertices(left, 0) & cut_vertices(right, 12)), 4)

    def test_dense_foliage_preserves_existing_mesh_budget(self):
        # The old eight-vertex strip fits four small parts; richer foliage must not make this tile fail.
        with patch.object(barriers, "MAX_VERTS", 160):
            m, rows = meshes(hedge(((10, 30), (150, 30))))
        self.assertFalse(rows)
        self.assertLessEqual(len(m), barriers.PARTS)
        p = np.concatenate([v[0][0] for v in m.values()])
        np.testing.assert_allclose(p.min(0)[:2], [10, 29.6])
        np.testing.assert_allclose(p.max(0)[:2], [150, 30.4])

    def assertGeometryEqual(self, a, b):
        self.assertEqual(a.keys(), b.keys())
        for key in a:
            for left, right in zip(a[key][0], b[key][0]):
                np.testing.assert_equal(left, right)
            np.testing.assert_equal(a[key][1], b[key][1])

    def test_fitting_hedge_has_stock_and_complete_mesh_fallback(self):
        r = hedge()
        m, rows = meshes(r)
        self.assertGreater(len(rows), 20)
        self.assertIn("hedgeStock", m)
        self.assertIn("hedge", m)       # ends remain exact mapped geometry
        for row in rows:
            self.assertEqual(row[0], 0)
            self.assertEqual(row[6:], [1.8, 0])
        # Splitting representation adds stations but preserves the full mapped extent.
        all_points = np.concatenate([part[0][0] for part in m.values()])
        np.testing.assert_allclose(all_points.min(0)[:2], [10, 29.6])
        np.testing.assert_allclose(all_points.max(0)[:2], [100, 30.4])

    def test_incompatible_dimensions_and_slopes_keep_identical_geometry(self):
        for r, gz in [(hedge(width=0.2), ground), (hedge(width=2), ground),
                      (hedge(height=7), ground), (hedge(height=0.55, width=0.15), ground),
                      (hedge(height=0.55, width=0.28), lambda p: p[:, 0] * 0.1),
                      (hedge(), lambda p: p[:, 0] * 0.3)]:
            with self.subTest(width=r.width, height=r.height):
                m, rows = meshes(r, gz)
                self.assertEqual(rows, [])
                self.assertGeometryEqual(m, barriers.tile_meshes([r], BOX, gz))

    def test_gate_opening_includes_foliage_end_and_wind_allowance(self):
        r = hedge(gaps=((40, 44, "gate"),))
        _, rows = meshes(r)
        self.assertTrue(rows)
        scale = r.height / barriers.HEDGE_HEIGHT
        for row in rows:
            station = row[1] - 10 - 0.030677 * scale
            self.assertTrue(station + barriers.HEDGE_REACH * scale < 40 or
                            station - barriers.HEDGE_REACH * scale > 44)

    def test_curves_short_runs_and_uneven_ground_use_fallback(self):
        for r in [hedge(((10, 30), (11, 30))), hedge(((30, 30), (31, 31), (30, 32), (29, 31), (30, 30)))]:
            m, rows = meshes(r)
            self.assertFalse(rows)
            self.assertGeometryEqual(m, barriers.tile_meshes([r], BOX, ground))
        m, rows = meshes(hedge(), lambda p: np.where(np.arange(len(p)) % 3 == 1, 11, 10))
        self.assertFalse(rows)

    def test_corner_and_closed_line_stay_filled(self):
        r = hedge(((20, 20), (80, 20), (80, 80), (20, 80), (20, 20)))
        m, rows = meshes(r)
        self.assertTrue(rows)
        self.assertIn("hedge", m)
        for row in rows:
            p = np.array(row[1:3])
            self.assertGreater(np.linalg.norm(r.xy - p, axis=1).min(), 1.2)

    def test_adjacent_negative_tiles_have_unique_bounded_stable_items(self):
        r = hedge(((-200, 30), (490, 30)))
        found = []
        for box in [(-256, 0, 0, 256), BOX, (256, 0, 512, 256)]:
            _, rows = meshes(r, box=box)
            _, again = meshes(r, box=box)
            self.assertEqual(rows, again)
            for row in rows:
                reach = barriers.HEDGE_REACH * row[6] / barriers.HEDGE_HEIGHT
                self.assertGreater(row[1] - reach, box[0] - 0.04)
                self.assertLess(row[1] + reach, box[2] + 0.04)
            found.extend(round(row[1], 6) for row in rows)
        self.assertEqual(len(found), len(set(found)))

    def test_dense_tile_falls_back_before_elevation_work(self):
        rr = [hedge(((10, y), (245, y))) for y in range(10, 250, 10)]
        def no_ground(p):
            self.fail("An over-budget tile must not sample elevations for stock items")
        rows, cover = barriers._hedge_items(rr, BOX, no_ground, ())
        self.assertEqual((rows, cover), ([], {}))

    def test_distant_geometry_is_clipped_before_stock_sampling(self):
        calls = []
        def bounded(p):
            calls.append(len(p))
            self.assertTrue(((p >= BOX[:2]) & (p <= BOX[2:])).all())
            return ground(p)
        _, rows = meshes(hedge(((-100000, 30), (100000, 30))), bounded)
        self.assertLessEqual(len(rows), barriers.HEDGE_MAX_ITEMS)
        self.assertLessEqual(max(calls), 3 * barriers.HEDGE_MAX_ITEMS)

    def test_carriageway_conflicts_do_not_gain_foliage(self):
        _, rows = meshes(hedge(), road_lines=[(np.array([[0, 30], [120, 30]]), 3)])
        self.assertFalse(rows)

    def test_other_boundaries_are_byte_identical(self):
        for kind in ["wall", "fence"]:
            r = hedge(kind=kind)
            r.style = "wall" if kind == "wall" else "rail"
            m, rows = meshes(r)
            self.assertFalse(rows)
            self.assertGeometryEqual(m, barriers.tile_meshes([r], BOX, ground))

    def test_road_tile_serializes_optional_stock_items_and_fallback_slots(self):
        m, rows = meshes(hedge())
        with tempfile.TemporaryDirectory() as folder:
            roads.write_tile(folder, "/temp/hedge-test", 1, 0, 0, [], boundary_meshes=m, hedge_props=rows)
            with open(os.path.join(folder, "roads_1_0_0.json")) as f:
                data = json.load(f)
            self.assertEqual(data["hedges"], {"groups": ["mapped_hedge"], "items": rows})
            self.assertIn("hedgeStock", data)
            self.assertNotIn("hedgeStock", data.get("colliders", {}))


if __name__ == "__main__":
    unittest.main()
