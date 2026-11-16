import json
import os
import sys
import tempfile
import threading
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
import pathfootprints
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

    def test_verge_batches_preserve_long_path_gaps(self):
        # Cross both batch limits: 40 street segments and 2,500 one-metre path steps.
        x = 25. + np.arange(40) * 60.
        starts = np.c_[x, np.zeros(40)]
        ends = starts + [10., 0.]
        radii = np.ones(40)
        bounds = np.c_[starts - radii[:, None], ends + radii[:, None]]
        runs = paths._outside_verges(np.array([[0., 0.], [2500., 0.]]), 2.,
                                     starts, ends - starts, np.full(40, 10.), radii, bounds)
        expected = np.c_[np.r_[0., x + 12.], np.r_[x - 2., 2500.]]
        np.testing.assert_allclose([[run[0, 0], run[-1, 0]] for run in runs], expected, rtol=0., atol=1e-9)

    def test_charzykowy_width_is_scoped_to_osm_ways(self):
        ways = sorted(paths.CHARZYKOWY_PROMENADE)
        n = len(ways)
        f = features([[(0, 20 + i * 10), (100, 20 + i * 10)] for i in range(n + 3)],
                     ["path"] * (n + 3), widths=[(n + 2, 2.5)])
        f.attrs["name"] = np.full(n + 3, "Promenada")
        f.attrs["src_dataset"] = np.array(["OpenStreetMap"] * n + ["Other", "OpenStreetMap", "OpenStreetMap"])
        f.attrs["src_record"] = np.array([f"w{way}@5" for way in ways] + [f"w{ways[0]}@5", "w9999999999@1", "w9999999998@1"])
        self.assertEqual([paths.profile(f, i)[0] for i in range(n + 3)], [4.] * n + [1.2, 1.2, 2.5])
        with patch.dict(os.environ, BNE_STANDALONE_PATHS="off"):
            self.assertEqual(self.parts(f), [])

    def test_charzykowy_cache_rebuild_is_local_and_opt_in(self):
        from test_vegetation import FakeMapData
        with tempfile.TemporaryDirectory() as directory:
            v = vegetation.Vegetation(FakeMapData(), directory)
            self.assertEqual(Path(v._path(8988, 5281)).name, "5281-charzykowy-promenada-4m.npz")
            self.assertEqual(Path(v._path(8987, 5281)).name, "5281.npz")
            with patch.dict(os.environ, BNE_STANDALONE_PATHS="off"):
                self.assertEqual(Path(v._path(8988, 5281)).name, "5281.npz")

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
                                lon=np.full(2, LON + 16 / M_LON), radius=np.full(2, .5),
                                height=np.full(2, .3), kind=np.full(2, vegetation.KIND_BUSH),
                                group=np.full(2, vegetation.G_SHRUB))
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
        # Shrubs need their full footprint clear, but a tree's branches may
        # overhang. A far clump contains several trunks across its footprint.
        items = vegetation.Items(np.full(4, LON + 10 / M_LON), np.full(4, LAT + 3.5 / M_LAT),
                                 np.array([.3, 3., 12., 20.]), np.array([.5, 2., 2.5, 8.]),
                                 np.array([vegetation.KIND_BUSH] * 2 + [vegetation.KIND_TREE] * 2),
                                 np.array([vegetation.G_SHRUB] * 2 + [vegetation.G_CONIFER,
                                          vegetation.G_CONIFER_CLUMP]),
                                 np.zeros(4), np.zeros(4), np.array([1., 1., -1., 1.]))
        blockers = [(np.array([[0., 0.], [20., 0.]]), 2.)]
        self.assertEqual(vegetation._off_paths(items, FRAME, blockers).tolist(), [True, False, True, False])

    def test_tree_trunks_stay_off_path_but_crowns_can_overhang(self):
        items = SimpleNamespace(lon=np.full(3, LON + 10 / M_LON),
                                lat=LAT + np.array([0., 1.95, 2.05]) / M_LAT,
                                radius=np.full(3, 5.), kind=np.full(3, vegetation.KIND_TREE),
                                group=np.full(3, vegetation.G_DECIDUOUS))
        blockers = [(np.array([[0., 0.], [20., 0.]]), 2.)]
        self.assertEqual(vegetation._off_paths(items, FRAME, blockers).tolist(), [False, False, True])

    def test_furniture_footprints_clear_path_and_keep_nearby_items(self):
        blockers = [(np.array([[0., 0.], [20., 0.]]), 2.)]
        def row(group, x, y, yaw=0., height=1.):
            return [group, x, y, 0., yaw, 1., height, .5]
        rows = [row(furniture.G_HYDRANT, 10., 0., height=.9),
                row(furniture.G_HYDRANT, 10., 2.2, height=.9),
                row(furniture.G_BENCH, 10., 2.8, yaw=90., height=.9),
                row(furniture.G_BIN, -2.1, 0., height=1.1),
                row(furniture.G_STREET_TREE, 10., 2., height=10.),
                row(furniture.G_BIN, 10., 2.30, height=1.1),  # collider clears the path by 3.6 cm
                row(furniture.G_HYDRANT, 10., 4., height=.9),
                row(furniture.G_BENCH, 10., 4., height=.9),
                row(furniture.G_STREET_TREE, 10., 2.05, height=10.),
                row(furniture.G_LAMP, 10., 0., height=9.)]
        self.assertEqual(paths.clear_props(rows, blockers), rows[5:])
        self.assertEqual(paths.clear_props(rows, []), rows)

    def test_both_tile_builders_reject_late_props_on_every_path_class(self):
        import blockworker
        import earthd
        import radars
        from test_blockworker import FakeMapData, FakeDem, GermanRoutesTest
        late = [["hydrant", 64., 70., 10., 0., 1., .9, .5],
                ["hydrant_eu", 64., 90., 10., 0., 1., .79, .5],
                ["shrub", 68., 80., 10., 0., 1., 1.8, .5],
                ["bin", 80., 70., 10., 0., 1., 1.1, .5]]
        for cls in paths.WIDTH:
            f = features([[(0, 64), (128, 64)], [(64, 0), (64, 128)]],
                         ["residential", cls], widths=[(1, 4.)])
            md = FakeMapData(f, roads.cells_of_tile(FRAME, 0, 0)[0])
            with tempfile.TemporaryDirectory() as directory:
                state = SimpleNamespace(md=md, dem=FakeDem(), widths=None, surface=None, airports=None, sea=None,
                                        lock=threading.Lock(), cache_dir=directory, bridges={},
                                        anchor=lambda *a: (None, None))
                ns = GermanRoutesTest().server((LAT, LON), directory, False, "off")
                ns.mapdata = md
                for setting, expected in (("off", late), ("on", late[3:])):
                    with self.subTest(cls=cls, setting=setting), \
                            patch.dict(os.environ, BNE_STANDALONE_PATHS=setting), \
                            patch.object(blockworker, "_s", state), \
                            patch.object(radars, "tile_rows", return_value=(late, [])):
                        result = blockworker._tile_parts(lambda *a: None, FRAME, (0, 0), 15, None,
                            buildings=False, left_hand=False, mph=False, gas_stations=False, buried=0.,
                            lamp_region=None, country="PL", q=None, rkey=("test",), centre=(LAT, LON))
                        self.assertEqual(result.props, expected)
                        with patch.object(roads, "write_tile", return_value=(None, 0)) as write:
                            earthd.Server.build_roads_tile(ns, 1, 0, 0)
                        self.assertEqual(write.call_args.args[10], expected)

    def test_measured_bush_model_extends_beyond_canopy_radius(self):
        # The stock generibush is 1.8 m high but reaches 3.31 m from its origin.
        # Its old canopy radius (1.216 m) misses the overlap at this point.
        points = np.array([[10., 4.], [10., 6.]])
        blockers = [(np.array([[0., 0.], [20., 0.]]), 2.)]
        self.assertEqual(paths.keep_outside(points, blockers, 1.216).tolist(), [True, True])
        self.assertAlmostEqual(pathfootprints.radii('shrub', np.array([1.8]))[0], 3.31)
        items = SimpleNamespace(lon=LON + points[:, 0] / M_LON, lat=LAT + points[:, 1] / M_LAT,
                                radius=np.full(2, 1.216), height=np.full(2, 1.8),
                                kind=np.full(2, vegetation.KIND_BUSH), group=np.full(2, vegetation.G_SHRUB))
        self.assertEqual(vegetation._off_paths(items, FRAME, blockers).tolist(), [False, True])

    def test_mapped_street_furniture_is_filtered_after_placement(self):
        import areaprops
        import test_areaprops as fixtures
        with patch.object(fixtures, 'FRAME', FRAME):
            infra = fixtures.feats('infrastructure', [
                ('hydrant', 1, [[(10., 0.)]], 'fire_hydrant', {}),
                ('stand', 1, [[(15., 3.)]], 'bicycle_parking', {}),
                ('clear', 1, [[(20., 8.)]], 'fire_hydrant', {})])
        rows = areaprops.street_rows(FRAME, (-10, -10, 110, 110), fixtures.FakeMD(infrastructure=infra),
                                    fixtures.flat, country='PL', keys=['cell'])
        f = features([[(0, 0), (100, 0)]], ['pedestrian'], widths=[(0, 4.)])
        self.assertEqual(len(rows), 3)
        kept = paths.finish_props(rows, FRAME, (-10, -10, 110, 110), [f])
        self.assertEqual(len(kept), 1)
        self.assertGreater(kept[0][2], 7.)

    def test_structural_and_overhead_objects_are_preserved(self):
        f = features([[(0, 0), (100, 0)]], ['cycleway'], widths=[(0, 4.)])
        rows = [['pole', 10., 0., 0., 0., 1., 9.2, .5], ['door', 20., 0., 10., 0., 1., 2., .5]]
        self.assertEqual(paths.finish_props(rows, FRAME, (-10, -10, 110, 110), [f]), rows)

    def test_final_gate_removes_hydrant_only_when_enabled(self):
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
                props = paths.finish_props(props, FRAME, roads.tile_bbox(0, 0), [f])
                self.assertEqual(props, expected)

    def test_old_crown_clearance_vegetation_cache_is_not_reused(self):
        from test_vegetation import FakeMapData, KEY
        with tempfile.TemporaryDirectory() as directory:
            md = FakeMapData()
            v = vegetation.Vegetation(md, directory)
            old = Path(directory, "vegetation", f"v{vegetation.VERSION}", "standalone-paths-v3",
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
