"""Roadside buildings share their corrected footprint with visible, LOD and collision consumers."""
import json
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import buildings as bld
import geodesy
import roads
from test_buildings import Feats, rect
from test_roads import FRAME, features


class RoadCorridorTest(unittest.TestCase):
    def test_widths_match_the_road_consumer_and_duplicates_are_taken_once(self):
        f = features([[(-100, 100), (400, 100)]], ["residential"])
        urban = lambda lat, lon: np.ones(np.shape(lat), bool)
        widths = lambda fid, ll: (3.2, 0.5)
        corridors = bld.road_corridors(FRAME, [f, f], 0, 0, lambda la, lo: np.full(np.shape(la), 10),
                                       urban_fn=urban, widths=widths, left_hand=True)
        self.assertEqual(len(corridors), 1)
        self.assertEqual(corridors[0][2], roads.road_specs(f, urban, widths, True)[0].width)
        self.assertEqual(corridors[0][2], 3.2, "a narrow historic street is not a fixed 8 m corridor")

    def test_bridge_tunnel_ranges_do_not_clear_ground_buildings(self):
        f = features([[(-100, 100), (400, 100)]] * 3, ["primary"] * 3,
                     flags=[(0, "is_bridge"), (1, "is_tunnel"), (2, "is_bridge", .4, .6)])
        corridors = bld.road_corridors(FRAME, [f], 0, 0, lambda la, lo: np.zeros(np.shape(la)))
        self.assertEqual(len(corridors), 2, "only the two ground pieces beside the partial bridge")
        for xy, _, _ in corridors:
            self.assertFalse(((xy[:, 0] > 105) & (xy[:, 0] < 195)).any())

    def test_duplicate_road_cells_do_not_invent_a_through_building(self):
        f = features([[(-100, 100), (400, 100)]], ["residential"])
        lines = bld.road_lines(FRAME, [f, f], 0, 0)
        self.assertEqual(len(lines), 1)
        ring = rect(20, 95, 8, 10)
        self.assertLess(bld.road_through(ring, lines), bld.ROAD_THROUGH_M)
        self.assertGreaterEqual(bld.road_through(ring, lines * 2), bld.ROAD_THROUGH_M)

    def test_taper_widths_match_painted_source_segments_without_a_roadwide_maximum(self):
        import surface
        f = features([[(-200, 100), (-100, 100), (-50, 100), (-20, 100), (0, 100)],
                      [(0, 100), (200, 100)]], ["residential", "residential"], widths=[(0, 3), (1, 9)])
        flat = lambda la, lo: np.zeros(np.shape(la))
        corridors = bld.road_corridors(FRAME, [f, f], -256, 0, flat)
        self.assertEqual(len(corridors), 2)
        xy, _, widths = corridors[0]
        self.assertEqual(np.shape(widths), (len(xy) - 1,))
        self.assertAlmostEqual(widths[0], 3, places=6, msg="the historic street beyond the taper stays narrow")
        self.assertGreater(widths[-1], widths[0] + 1)
        _, painted_half, _ = surface._road_segments(FRAME, -256, 0, 512, 512, 512, [f, f])
        np.testing.assert_allclose(painted_half[:len(widths)], widths / 2 + surface.PHOTO_ROAD_EDGE_M, atol=1e-9)
        flat_segments = bld._clearance_segments(corridors)
        np.testing.assert_allclose(flat_segments[3][:len(widths)], widths / 2)

        # Two otherwise identical houses beside the same source way: only the tapered end encroaches.
        half = float(widths[-1] / 2)
        offset = (1.5 + half) / 2
        self.assertGreater(half - offset, bld.ROAD_NOISE_M)
        rings = []
        for k in (0, len(xy) - 2):
            a, b = xy[k:k + 2]
            u = (b - a) / np.linalg.norm(b - a)
            normal = np.array([-u[1], u[0]])
            p = a + 2 * u + offset * normal
            ring = np.array([p, p + 8 * u, p + 8 * u + 10 * normal, p + 10 * normal, p])
            la, lo, _ = FRAME.enu_to_geodetic(ring[:, 0] - 256, ring[:, 1], 0)
            rings.append(np.column_stack([lo, la]))
        layer = Feats(["narrow", "taper"], rings, **{"class": ["house", "house"],
                      "height": [10., 10.], "roof_shape": ["flat", "flat"]})
        items = bld.tile_buildings(FRAME, -256, 0, 0, 256, [layer], flat,
                                  roads_xy=[xy], corridors=corridors)
        self.assertEqual(len(items), 2)
        narrow, tapered = sorted(items, key=lambda it: it[0].ring[:, 0].mean())
        self.assertFalse(narrow[0].road_trimmed, "a wide joint must not shave the rest of a narrow street")
        self.assertTrue(tapered[0].road_trimmed, "the old scalar spec width misses this tapered edge")
        a, b = xy[-2:]
        u = (b - a) / np.linalg.norm(b - a)
        normal = np.array([-u[1], u[0]])
        for mesh in (bld.build([tapered]), bld.build([tapered], lite=True), bld.collision_mesh([tapered])):
            p, _, t = mesh[:3]
            walls = p[t][p[t][:, :, 2].min(1) < 1]
            self.assertGreater(len(walls), 0)
            self.assertGreaterEqual(((walls[:, :, :2] - a) @ normal).min(), half - 1e-6)

    def test_narrowed_taper_does_not_shave_a_clear_roadside_wall(self):
        # The source way is 12 m wide away from its 4 m continuation; at their joint it tapers to 8 m.
        f = features([[(-200, 100), (-100, 100), (-20, 100), (0, 100)],
                      [(0, 100), (200, 100)]], ["primary", "primary"], widths=[(0, 12), (1, 4)])
        flat = lambda la, lo: np.zeros(np.shape(la))
        corridors = bld.road_corridors(FRAME, [f], -256, 0, flat)
        self.assertEqual(len(corridors), 2)
        xy, z, widths = corridors[0]
        self.assertEqual(roads.road_specs(f)[0].width, 12)
        self.assertGreater(widths[-1], 8)
        self.assertLess(widths[-1], 9)
        a, b = xy[-2:]
        u = (b - a) / np.linalg.norm(b - a)
        normal = np.array([-u[1], u[0]])
        p = a + 2 * u + 5 * normal
        ring = np.array([p, p + 12 * u, p + 12 * u + 10 * normal, p + 10 * normal, p])
        la, lo, _ = FRAME.enu_to_geodetic(ring[:, 0] - 256, ring[:, 1], 0)
        layer = Feats(["house"], [np.column_stack([lo, la])], **{"class": ["house"],
                      "height": [10.], "roof_shape": ["flat"]})
        args = (FRAME, -256, 0, 0, 256, [layer], flat)
        narrowed = bld.tile_buildings(*args, roads_xy=[xy], corridors=corridors)
        nominal = bld.tile_buildings(*args, roads_xy=[xy], corridors=[(xy, z, 12)])
        self.assertEqual(len(narrowed), 1)
        self.assertEqual(len(nominal), 1)
        self.assertFalse(narrowed[0][0].road_trimmed, "a wall 5 m from the centre clears an 8 m road")
        self.assertTrue(nominal[0][0].road_trimmed, "the nominal 12 m width would shave this wall to 6 m")
        self.assertAlmostEqual(float(((narrowed[0][0].ring - a) @ normal).min()), 5, delta=0.01)
        self.assertAlmostEqual(float(((nominal[0][0].ring - a) @ normal).min()), 6, delta=0.01)

    def test_development_ship_and_quay_meshes_keep_their_geometry(self):
        import ships
        ring = rect(20, 42, 14, 16)
        segments = bld._clearance_segments([(np.array([[-100., 40.], [600., 40.]]), np.zeros(2), 8)])
        for spec in (ships.spec(ring.copy(), {"height": 10}, .5), ships.pier_spec(ring.copy(), .5)):
            items = [(spec, 0., 0.)]
            meshes = (bld.build(items), bld.build(items, lite=True), bld.collision_mesh(items))
            bld._clear_road(spec, 0, 0, segments)
            self.assertFalse(spec.road_trimmed)
            np.testing.assert_array_equal(spec.ring, ring)
            for before, after in zip(meshes, (bld.build(items), bld.build(items, lite=True),
                                              bld.collision_mesh(items))):
                np.testing.assert_array_equal(before[0], after[0])
                np.testing.assert_array_equal(before[2], after[2])


class BuildingClearanceTest(unittest.TestCase):
    def setUp(self):
        self.frame = geodesy.EnuFrame(51.313550, 10.314557)
        self.flat = lambda la, lo: np.zeros(np.shape(la))
        self.road = np.array([[-100., 40.], [600., 40.]])

    def layer(self, ring, cls="house", min_h=0, source="OpenStreetMap", bid="house", roof="flat"):
        ring = np.vstack([ring, ring[:1]])
        la, lo, _ = self.frame.enu_to_geodetic(ring[:, 0], ring[:, 1], np.zeros(len(ring)))
        return Feats([bid], [np.column_stack([lo, la])], **{
            "class": [cls], "height": [10.], "min_height": [min_h],
            "roof_shape": [roof], "src_dataset": [source]})

    def items(self, ring, width=8, road_z=0, layers=None, box=(0, 0, 256, 256), **kw):
        layer = self.layer(ring, **kw)
        x0, y0, x1, y1 = box
        xy = self.road - [x0, y0]
        return bld.tile_buildings(self.frame, *box, layers or [layer], self.flat,
                                  roads_xy=[xy], corridors=[(xy, np.array([road_z, road_z]), width)])

    def test_edge_only_overlap_clears_visible_lod_and_collision_walls(self):
        # Centreline misses the footprint; centre is over 4 m away, so both old filters miss this wall.
        ring = rect(20, 42, 14, 16)
        self.assertEqual(bld.road_through(ring, [self.road]), 0)
        items = self.items(ring, roof="gabled")
        self.assertEqual(len(items), 1)
        spec = items[0][0]
        self.assertTrue(spec.road_trimmed)
        self.assertEqual(spec.min_h, 0, "a roadside house stays grounded, not a floating slab")
        self.assertGreaterEqual(bld.drawn_ring(spec)[:, 1].min(), 44 - 1e-6)
        self.assertEqual(len(spec.gring), len(spec.ring))
        # Check triangles at vehicle height, not just internal planning geometry (the pitched roof may overhang).
        for mesh in (bld.build(items), bld.build(items, lite=True), bld.collision_mesh(items)):
            p, _, t = mesh[:3]
            walls = p[t][p[t][:, :, 2].min(1) < 2]
            self.assertGreater(len(walls), 0)
            self.assertGreaterEqual(walls[:, :, 1].min(), 44 - 1e-6)

    def test_narrow_streets_and_tiny_mapping_noise_preserve_buildings(self):
        for y, width in ((42, 3), (43.9, 8)):
            with self.subTest(y=y, width=width):
                spec = self.items(rect(20, y, 14, 16), width=width)[0][0]
                self.assertFalse(spec.road_trimmed)
                self.assertAlmostEqual(bld._area2(spec.ring) / 2, 224, delta=.01)

    def test_no_block_deleted_for_large_or_uncertain_overlap(self):
        # Five metres of an eight-metre-deep house is not evidence that the whole house is bogus.
        spec = self.items(rect(20, 39, 14, 8))[0][0]
        self.assertFalse(spec.road_trimmed)
        self.assertAlmostEqual(bld._area2(spec.ring) / 2, 112, delta=.01)

    def test_existing_over_road_buildings_and_arches_are_preserved(self):
        # Existing long crossings retain their headroom; explicit elevated ML parts must not be discarded first.
        crossing = self.items(rect(20, 20, 50, 40))[0][0]
        self.assertEqual(crossing.min_h, bld.OVER_ROAD_CLEAR_M)
        elevated = self.items(rect(20, 35, 14, 10), min_h=7, source="Microsoft")[0][0]
        self.assertEqual(elevated.min_h, 7)
        self.assertFalse(elevated.road_trimmed)

    def test_grade_separated_road_and_building_stay_whole(self):
        for z in (11, 30, -30):
            spec = self.items(rect(20, 42, 14, 16), road_z=z)[0][0]
            self.assertFalse(spec.road_trimmed)

    def test_toll_canopies_bridge_supports_and_harbour_structures_are_not_shaved(self):
        for cls in ("toll_booth", "roof", "shelter", "bridge_structure", "ship", "pier"):
            spec = self.items(rect(20, 42, 14, 16), cls=cls)[0][0]
            self.assertFalse(spec.road_trimmed, cls)

    def test_short_segment_does_not_clip_its_infinite_extension(self):
        self.road = np.array([[25., 40.], [30., 40.]])
        spec = self.items(rect(20, 42, 14, 16))[0][0]
        self.assertFalse(spec.road_trimmed)

    def test_tile_boundary_and_source_duplicates_have_one_identical_building(self):
        ring = rect(250, 42, 20, 16)
        layer = self.layer(ring)
        left = self.items(ring, layers=[layer, layer])
        right = self.items(ring, layers=[layer, layer], box=(256, 0, 512, 256))
        self.assertEqual(left, [])
        self.assertEqual(len(right), 1)
        spec = right[0][0]
        self.assertTrue(spec.road_trimmed)
        self.assertAlmostEqual(bld._area2(spec.ring) / 2, 20 * 14, delta=.01)
        self.assertGreaterEqual(spec.ring[:, 1].min(), 44 - 1e-6)


class ReportedSourceClearanceTest(unittest.TestCase):
    def test_reported_source_footprints_clear_the_same_consumer_road(self):
        fixture = json.loads((Path(__file__).parent / "fixtures" / "building_road_clearance.json").read_text())
        for case in fixture["cases"]:
            with self.subTest(issue=case["issue"], building=case["building_id"]):
                import mapgeom
                frame = geodesy.EnuFrame(*case["location"])
                ll = np.asarray(case["road_lonlat"])
                road = mapgeom.Features(
                    layer="roads", ids=np.array([case["road_id"]]), kind=np.array([mapgeom.LINE], np.uint8),
                    part_off=np.array([0, 1]), ring_off=np.array([0, 1]), coord_off=np.array([0, len(ll)]),
                    lonlat=ll, attrs={k: np.array([v], object) for k, v in case["road_attrs"].items()})
                building = Feats([case["building_id"]], [case["building_lonlat"]],
                                 **{k: [v] for k, v in case["building_attrs"].items()})
                flat = lambda la, lo: np.zeros(np.shape(la))
                lines = bld.road_lines(frame, [road], -128, -128)
                corridors = bld.road_corridors(frame, [road, road], -128, -128, flat)
                self.assertEqual(len(corridors), 1)
                self.assertEqual(corridors[0][2], case["width_m"])
                args = (frame, -128, -128, 128, 128, [building, building], flat)
                before = bld.tile_buildings(*args, roads_xy=lines)
                after = bld.tile_buildings(*args, roads_xy=lines, corridors=corridors)
                self.assertEqual(len(before), 1)
                self.assertEqual(len(after), 1)
                original, corrected = before[0][0], after[0][0]
                self.assertEqual(corrected.min_h, original.min_h)
                self.assertTrue(corrected.road_trimmed)
                # Find the finite source segment responsible for the original wall intrusion.
                old = bld.drawn_ring(original)
                found = False
                for xy, _, width in corridors:
                    for a, b in zip(xy[:-1], xy[1:]):
                        d = b - a
                        length = np.linalg.norm(d)
                        u = d / length
                        along = (old - a) @ u
                        if along.min() < 0 or along.max() > length:
                            continue
                        normal = np.array([-u[1], u[0]])
                        normal *= 1 if (old.mean(0) - a) @ normal >= 0 else -1
                        if ((old - a) @ normal).min() >= width / 2 - bld.ROAD_NOISE_M:
                            continue
                        found = True
                        for mesh in (bld.build(after), bld.build(after, lite=True), bld.collision_mesh(after)):
                            p, _, t = mesh[:3]
                            walls = p[t][p[t][:, :, 2].min(1) < 1]
                            self.assertGreater(len(walls), 0)
                            distances = (walls[:, :, :2] - a) @ normal
                            self.assertGreaterEqual(distances.min(), width / 2 - 1e-5)
                self.assertTrue(found, "unchanged-main consumer geometry must reproduce the intrusion")


if __name__ == "__main__":
    unittest.main()
