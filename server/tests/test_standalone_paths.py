import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import buildings
import experimental
import furniture
import paintlist
import roads
import standalonepaths as paths
import surface
import vegetation
from test_buildings import rect
from test_roads import FRAME, LAT, LON, M_LAT, M_LON, features, flat


class StandalonePathsTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(paintlist.clear)
        self.setting = patch.dict(os.environ, BNE_STANDALONE_PATHS="on")
        self.setting.start()
        self.addCleanup(self.setting.stop)
        paintlist.clear()

    def test_setting_is_read_on_startup_and_respects_an_explicit_override(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory, "settings", "beamngEarth.json")
            settings.parent.mkdir()
            for value in (False, True):
                settings.write_text(json.dumps({"standalonePaths": value}), encoding="utf-8")
                env = {}
                experimental.apply(env, directory)
                self.assertEqual(env.get("BNE_STANDALONE_PATHS"), "on" if value else None)
            env = {"BNE_STANDALONE_PATHS": "off"}
            experimental.apply(env, directory)
            self.assertEqual(env["BNE_STANDALONE_PATHS"], "off")

    def parts(self, *cells):
        return paths.parts(FRAME, (-10, -10, 110, 110), cells)

    def test_only_ground_and_no_sidewalk_or_second_road(self):
        classes = ["residential", "footway", "footway", "cycleway", "steps", "path", "footway", "pedestrian", "cycleway"]
        lines = [[(0, y), (100, y)] for y in (0, 5, 22, 30, 40, 50, 60, 70, 80)]
        f = features(lines, classes, subclasses=[(2, "sidewalk"), (3, "crossing")],
                     flags=[(5, "is_bridge"), (6, "is_tunnel")], widths=[(7, 4.)], surfaces=[(7, "paving_stones")])
        f.attrs["osm_sidewalk"] = np.array([""] * 8 + ["both"])
        parts = self.parts(f, f)
        self.assertEqual([(fid, w, k) for fid, _, w, k in parts], [("id007", 4., "cobble")])
        self.assertIsNone(roads.road_specs(f)[7])
        with patch.dict(os.environ, BNE_STANDALONE_PATHS="off"):
            self.assertFalse(self.parts(f))
        f.attrs["osm_highway"] = np.array([""] * 7 + ["construction", ""])
        f.__dict__.pop("_unbuilt", None)
        self.assertIsNone(paths.profile(f, 7))

    def test_unlabelled_sidewalk_turns_into_an_independent_path_across_cells(self):
        street = features([[(0, 0), (100, 0)]], ["residential"])
        path = features([[(90, 5), (30, 5), (30, 40)]], ["path"], ids=["spur"])
        result = self.parts(path, street)
        self.assertEqual(len(result), 1)
        _, line, width, kind = result[0]
        self.assertLess(np.ptp(line[:, 0]), .1)
        self.assertGreater(np.ptp(line[:, 1]), 34.)
        self.assertEqual((width, kind), (1.2, "dirt"))

    def test_roadside_lines_leave_road_and_sidewalk_outputs_identical(self):
        lines = [[(10, y), (115, y)] for y in (62, 67, 57, 77)]
        f = features(lines, ["residential", "footway", "cycleway", "footway"],
                     widths=[(0, 7.), (1, 5.), (2, 2.), (3, 2.)], subclasses=[(3, "sidewalk")])
        f.attrs["osm_sidewalk"] = np.array(["both", "", "", ""])
        x, y = np.meshgrid(np.arange(128.), np.arange(128.))
        lawn = np.full(x.shape, surface.LAWN, np.uint8)
        rgb = np.full((128, 128, 3), [25, 110, 30], np.uint8)

        def outputs(setting):
            with patch.dict(os.environ, BNE_STANDALONE_PATHS=setting):
                paintlist.clear()
                walks, carriageways = [], []
                meshes = roads.build_tile(FRAME, 0, 0, [f], flat, walks=walks, roads_out=carriageways)
                self.assertTrue(walks)
                self.assertTrue(carriageways)
                geometry = json.dumps([meshes, walks, carriageways], default=lambda a: a.tolist(), sort_keys=True)
                material = surface.paint_roads(lawn, LAT + y / M_LAT, LON + x / M_LON, [f], 1.)
                return geometry, material, surface.photo_roads(rgb, FRAME, 0, 0, 128, [f])

        before, after = outputs("off"), outputs("on")
        self.assertEqual(before[0], after[0])
        np.testing.assert_array_equal(before[1], after[1])
        np.testing.assert_array_equal(before[2], after[2])

    def test_explicit_crossings_never_receive_a_standalone_strip(self):
        f = features([[(0, y), (50, y)] for y in (5, 15, 25)], ["cycleway", "footway", "footway"],
                     subclasses=[(0, "cycle_crossing"), (1, "crosswalk")])
        f.attrs["osm_footway"] = np.array(["", "", "sidewalk"])
        self.assertFalse(self.parts(f))

    def test_road_end_does_not_leave_a_stub_of_parallel_sidewalk(self):
        f = features([[(10, 20), (50, 20)], [(10, 25), (53, 25)]], ["residential", "footway"])
        self.assertFalse(self.parts(f))

    def test_material_photo_and_final_plant_positions_use_the_same_width(self):
        f = features([[(0, 16), (32, 16)]], ["footway"], surfaces=[(0, "paving_stones")])
        f.attrs["osm_width"] = np.array([2.])
        x, y = np.meshgrid(np.arange(32.), np.arange(32.))
        lawn = np.full(x.shape, surface.LAWN, np.uint8)
        rgb = np.full((32, 32, 3), [25, 110, 30], np.uint8)
        material = surface.paint_roads(lawn, LAT + y / M_LAT, LON + x / M_LON, [f], 1.)
        self.assertEqual(np.flatnonzero(material[:, 16] == surface.COBBLE).tolist(), [15, 16])
        photo = surface.photo_roads(rgb, FRAME, 0, 0, 32, [f])
        self.assertFalse(np.array_equal(photo[16, 16], rgb[16, 16]))
        np.testing.assert_array_equal(photo[11, 16], rgb[11, 16])
        items = SimpleNamespace(lat=np.array([LAT + 16 / M_LAT, LAT + 18 / M_LAT]),
                                lon=np.full(2, LON + 16 / M_LON), radius=np.full(2, .5))
        blockers = [(xy, w / 2) for _, xy, w, _ in self.parts(f)]
        self.assertEqual(vegetation._off_paths(items, FRAME, blockers).tolist(), [False, True])
        on = vegetation.Vegetation(None, "cache").dir
        with patch.dict(os.environ, BNE_STANDALONE_PATHS="off"):
            paintlist.clear()
            np.testing.assert_array_equal(surface.photo_roads(rgb, FRAME, 0, 0, 32, [f]), rgb)
            np.testing.assert_array_equal(surface.paint_roads(lawn, LAT + y / M_LAT, LON + x / M_LON, [f], 1.), lawn)
            self.assertNotEqual(vegetation.Vegetation(None, "cache").dir, on)

    def test_tree_on_a_promenade_removed_by_cell_builder(self):
        from test_vegetation import FakeMapData, KEY
        md = FakeMapData()
        path = features([[(-10, 0), (10, 0)]], ["pedestrian"], widths=[(0, 6.)])
        path.lonlat = np.asarray([md.lonlat(100, c) for c in (80, 120)])
        md.cd.layers["roads"] = path

        def crowns():
            items = vegetation.Vegetation(md).cell(KEY)
            return items.take(items.kind == vegetation.KIND_TREE)

        with patch.dict(os.environ, BNE_STANDALONE_PATHS="off"):
            before = crowns()
        after = crowns()
        self.assertEqual(len(before), len(after) + 1)
        lon, lat = md.lonlat(100, 100)
        self.assertTrue((np.hypot((before.lon - lon) * M_LON, (before.lat - lat) * M_LAT) < 3.).any())
        self.assertFalse((np.hypot((after.lon - lon) * M_LON, (after.lat - lat) * M_LAT) < 3.).any())

    def test_plant_footprints_clear_edges_bends_and_endpoints(self):
        blockers = [(np.array([[0., 0.], [20., 0.], [20., 20.]]), 2.)]
        points = np.array([[10., 3.], [10., 4.1], [-3., 0.], [20., 23.],
                           [22., -2.], [24., -4.], [10., 0.]])
        # All but the last centre are off the paving; shrubs/crowns still overlap it.
        self.assertEqual(paths.keep_outside(points, blockers).tolist(), [True] * 6 + [False])
        self.assertEqual(paths.keep_outside(points, blockers, np.full(7, 2.)).tolist(),
                         [False, True, False, False, False, True, False])
        self.assertEqual(paths.keep_outside(points, [], np.full(7, 2.)).tolist(), [True] * 7)
        self.assertEqual(paths.keep_outside([], blockers, []).size, 0)

    def test_final_plant_filter_uses_each_footprint(self):
        # A small shrub fits beside the path; a larger bush, crown or far clump at
        # the same position does not. Negative near-only scale is not a radius.
        items = vegetation.Items(np.full(4, LON + 10 / M_LON), np.full(4, LAT + 3 / M_LAT),
                                 np.array([1., 3., 12., 20.]), np.array([.5, 2., 2.5, 8.]),
                                 np.array([vegetation.KIND_BUSH] * 2 + [vegetation.KIND_TREE] * 2),
                                 np.zeros(4, np.uint8), np.zeros(4), np.zeros(4), np.array([1., 1., -1., 1.]))
        blockers = [(np.array([[0., 0.], [20., 0.]]), 2.)]
        self.assertEqual(vegetation._off_paths(items, FRAME, blockers).tolist(), [True, False, False, False])

    def test_furniture_footprints_clear_path_and_keep_nearby_items(self):
        blockers = [(np.array([[0., 0.], [20., 0.]]), 2.)]
        def row(group, x, y, yaw=0., height=1.):
            return [group, x, y, 0., yaw, 1., height, .5]
        rows = [row(furniture.G_HYDRANT, 10., 0., height=.9),
                row(furniture.G_HYDRANT, 10., 2.2, height=.9),
                row(furniture.G_BENCH, 10., 2.8, yaw=90., height=.9),
                row(furniture.G_BIN, -2.1, 0., height=1.1),
                row(furniture.G_STREET_TREE, 10., 3., height=10.),
                row(furniture.G_HYDRANT, 10., 4., height=.9),
                row(furniture.G_BENCH, 10., 4., height=.9),
                row(furniture.G_STREET_TREE, 10., 6., height=10.),
                row(furniture.G_LAMP, 10., 0., height=9.)]
        self.assertEqual(paths.clear_props(rows, blockers), rows[5:])
        self.assertEqual(paths.clear_props(rows, []), rows)

    def test_road_builder_removes_hydrant_on_crossing_path_only_when_enabled(self):
        f = features([[(0, 64), (128, 64)], [(64, 0), (64, 128)]],
                     ["residential", "pedestrian"], widths=[(1, 4.)])
        on_path = [furniture.G_HYDRANT, 64., 70., 0., 0., 1., .9, .5]
        off_path = [furniture.G_HYDRANT, 80., 70., 0., 0., 1., .9, .5]
        for setting, expected in (("off", [on_path, off_path]), ("on", [off_path])):
            with patch.dict(os.environ, BNE_STANDALONE_PATHS=setting), \
                    patch.object(furniture, "place", return_value=[on_path, off_path]):
                paintlist.clear()
                props = []
                roads.build_tile(FRAME, 0, 0, [f], flat, walks=[], props=props)
                self.assertEqual(props, expected)

    def test_old_centre_only_vegetation_cache_is_not_reused(self):
        from test_vegetation import FakeMapData, KEY
        with tempfile.TemporaryDirectory() as directory:
            md = FakeMapData()
            v = vegetation.Vegetation(md, directory)
            old = Path(directory, "vegetation", f"v{vegetation.VERSION}", "standalone-paths-v1",
                       md.release, str(KEY.x), f"{KEY.y}.npz")
            old.parent.mkdir(parents=True)
            empty = vegetation._empty()
            np.savez(old, **vars(empty))
            with patch.object(v, "_build", return_value=empty) as build:
                v.cell(KEY)
            build.assert_called_once_with(KEY.x, KEY.y)
            self.assertTrue(old.exists())
            self.assertTrue(Path(v._path(KEY.x, KEY.y)).exists())

    def test_infer_house_access_only_on_a_local_street_without_existing_access(self):
        street = np.array([[-50., 6.], [100., 6.]])

        def drive(cls, extra=()):
            house = buildings.plan(rect(0, 20, 12, 9), {"class": "house"}, 0, 53.7, 17.5)
            house.wall = None
            drives = []
            buildings.doors([(house, 0., 0.)], [(street, 4., cls, True), *extra], 0., 0., drives)
            return drives

        self.assertEqual(len(drive("residential")), 1)
        self.assertFalse(drive("trunk"))
        self.assertFalse(drive("residential", [(np.array([[14., 6.], [14., 22.]]), 1.5, "service", True)]))
        with patch.dict(os.environ, BNE_STANDALONE_PATHS="off"):
            self.assertFalse(drive("residential"))
