"""Building geometry contracts: mapped outlines, terrain contact and visual/physics agreement."""
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import buildings as bld
import cdae
import mapgeom
import polygon
from test_roads import FRAME


def rect(x, y, w, h):
    return np.array([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], float)


def features(parts, **attrs):
    """One mapped building with several polygon components and optional holes."""
    coords, ro, co = [], [0], [0]
    for rings in parts:
        for ring in rings:
            r = np.vstack([ring, ring[0]])
            la, lo, _ = FRAME.enu_to_geodetic(r[:, 0], r[:, 1], 0.0)
            coords.extend(zip(lo, la))
            co.append(len(coords))
        ro.append(len(co) - 1)
    return mapgeom.Features("buildings", np.array(["stable-building-id"]), np.array([mapgeom.POLYGON]),
                            np.array([0, len(parts)]), np.array(ro), np.array(co), np.array(coords),
                            attrs={k: np.array([v]) for k, v in attrs.items()})


def flat(la, lo):
    return np.full(np.shape(la), 10.0)




class CourtyardTest(unittest.TestCase):
    def test_building_parts_keep_courtyards_and_binary_collision(self):
        parent = features([[rect(20, 20, 40, 40)]], height=12.0)
        part = features([[rect(20, 20, 40, 40), rect(30, 30, 20, 20)]],
                        height=12.0, building_id="stable-building-id")
        items = bld.tile_buildings(FRAME, 0, 0, 256, 256, [parent], flat, part_layers=[part])
        self.assertEqual(len(items), 1)
        self.assertEqual(len(items[0][0].holes), 1)
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "collision.cdae")
            bld.write(path, bld.collision_mesh(items), collision_only=True)
            self.assertIsNotNone(cdae.read(path))

    def test_sweep_preserves_area_holes_and_winding(self):
        outer = rect(0, 0, 40, 30)
        holes = [rect(5, 5, 8, 10), rect(22, 7, 10, 15)]
        for angle in (0, 0.3, 1.1):
            rot = np.array([[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]])
            ring, hh = outer @ rot, [h[::-1] @ rot for h in holes]
            cells = polygon.roof_cells(ring, hh)
            self.assertAlmostEqual(sum(bld._area2(c) / 2 for c in cells), 970.0, places=6)
            mid = np.array([c.mean(0) for c in cells])
            self.assertTrue(polygon.contains_points(ring, mid).all())
            self.assertFalse(any(polygon.contains_points(h, mid).any() for h in hh))

    def test_visible_lod_collision_and_spawn_guard_leave_courtyard_open(self):
        outer, hole = rect(0, 0, 40, 40), rect(10, 10, 20, 20)
        spec = bld.plan(outer, {"height": 12, "roof_shape": "gabled", "_holes": [hole]})
        self.assertEqual(spec.roof, "flat")
        items = [(spec, 0, 0)]
        for mesh in (bld.build(items), bld.build(items, lite=True), bld.collision_mesh(items)):
            p, _, t = mesh[:3]
            tri = p[t]
            horizontal = np.ptp(tri[:, :, 2], axis=1) < 1e-7
            top = horizontal & (tri[:, :, 2].mean(1) > 11.9)
            self.assertTrue(top.any())
            self.assertFalse(polygon.contains_points(hole, tri[top].mean(1)[:, :2]).any())
            norm = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
            walls = (np.abs(norm[:, 2]) < 1e-9) & (np.linalg.norm(norm, axis=1) > 0)
            inner = walls & (np.linalg.norm(tri.mean(1)[:, :2] - [20, 20], axis=1) < 15)
            self.assertTrue(inner.any())
            toward_void = np.array([20, 20]) - tri[inner].mean(1)[:, :2]
            self.assertTrue((np.sum(norm[inner, :2] * toward_void, axis=1) > 0).all())
        self.assertFalse(any(x0 < 20 < x1 and y0 < 20 < y1
                             for x0, y0, _, x1, y1, _ in bld.collider_boxes(items, 0, 0)))

    def test_import_keeps_holes_and_all_components(self):
        f = features([[rect(20, 20, 40, 40), rect(30, 30, 20, 20)], [rect(300, 20, 20, 20)]],
                     height=12.0, **{"class": "apartments"})
        a = bld.tile_buildings(FRAME, 0, 0, 256, 256, [f, f], flat)
        b = bld.tile_buildings(FRAME, 256, 0, 512, 256, [f, f], flat)
        self.assertEqual(len(a), 1)
        self.assertEqual(len(b), 1)
        self.assertEqual(len(a[0][0].holes), 1)
        self.assertEqual(b[0][0].holes, [])


class BuildingFidelityTest(unittest.TestCase):

    def test_mapped_roof_height_and_ridge_direction_match_collision(self):
        for direction, axis in ((0, 1), (90, 0)):
            spec = bld.plan(rect(0, 0, 20, 10), {"height": 12.0, "roof_shape": "gabled",
                            "roof_height": 3.0, "roof_direction": direction})
            self.assertAlmostEqual(spec.height, 9.0)
            for mesh in (bld.build([(spec, 0, 0)]), bld.collision_mesh([(spec, 0, 0)])):
                p = mesh[0]
                self.assertAlmostEqual(p[:, 2].max(), 12.0)
                ridge = p[np.isclose(p[:, 2], 12.0)]
                self.assertGreater(np.ptp(ridge[:, axis]), 5.0)
                self.assertLess(np.ptp(ridge[:, 1 - axis]), 1e-8)

    def test_across_orientation_and_invalid_height_fallback(self):
        spec = bld.plan(rect(0, 0, 20, 10), {"height": 12.0, "roof_shape": "hipped",
                        "roof_height": -3.0, "roof_orientation": "across"})
        self.assertGreater(spec.roof_rise, 0)
        self.assertAlmostEqual(abs(bld.roof_box(spec)[1][1]), 1.0)
        p = bld.build([(spec, 0, 0)])[0]
        self.assertAlmostEqual(p[:, 2].max(), 12.0)

    def test_stable_building_look_across_tile_origins(self):
        f = features([[rect(50, 50, 12, 10)]], **{"class": "house"})
        a = bld.tile_buildings(FRAME, 0, 0, 256, 256, [f], flat)[0][0]
        b = bld.tile_buildings(FRAME, -10, -10, 246, 246, [f], flat)[0][0]
        self.assertEqual((a.seed, a.roof, a.floors, a.facade), (b.seed, b.roof, b.floors, b.facade))

    def test_ground_boundary_detects_dip_and_batches_sampling(self):
        f = features([[rect(20, 20, 40, 20)], [rect(100, 20, 40, 20)]], height=8.0)
        calls = []
        def ground(la, lo):
            calls.append(len(la))
            e, n, _ = FRAME.geodetic_to_enu(la, lo, 0.0)
            return 10 - 3 * np.exp(-((e - 40) / 3) ** 2)
        items = bld.tile_buildings(FRAME, 0, 0, 256, 256, [f], ground)
        self.assertEqual(len(items), 2)
        self.assertLess(items[0][1], 7.2)
        self.assertGreater(items[0][0].gring.min(), 9.9)
        self.assertLessEqual(len(calls), 2, "one boundary batch, plus optional alpine probe")


if __name__ == "__main__":
    unittest.main()
