"""Mapped ground paths: synthetic geometry, including the tags of the two Welsh examples."""
import tempfile
import unittest
import sys
from pathlib import Path
from unittest.mock import Mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import geodesy
import mapdata
import mapgeom
import osmtags
import roads
import surface
from test_roads import features, LAT, LON, M_LAT, M_LON, flat
from types import SimpleNamespace


def tagged(lines, classes, tags, **kwargs):
    f = features(lines, classes, **kwargs)
    f.attrs["src_record"] = np.array([f"w{i + 1}@1" for i in range(len(lines))])
    osmtags.join(f, tags)
    return f


def grid(n=65, square=0.25):
    x = (np.arange(n) - (n - 1) / 2) * square
    e, north = np.meshgrid(x, x)
    return LAT + north / M_LAT, LON + e / M_LON


class PathRegressionTest(unittest.TestCase):
    # These exercise existing production entrypoints and fail on the unchanged baseline.

    def test_unknown_footway_is_an_unsealed_fallback_without_invented_earthworks(self):
        import mappedpaths
        f = features([[(-8, 0), (8, 0)]], ["footway"])
        sp = mappedpaths.specs(f)[0]
        self.assertEqual(sp.kind, "dirt")
        self.assertFalse(mappedpaths.engineered(sp))
        for surf in ("asphalt", "concrete", "paving_stones"):
            tagged_f = features([[(-8, 0), (8, 0)]], ["footway"], surfaces=[(0, surf)])
            self.assertTrue(mappedpaths.engineered(mappedpaths.specs(tagged_f)[0]))

    def test_narrow_grass_guard_preserves_surface_width_and_distant_grass(self):
        import mappedpaths
        import surface as sf
        lat, lon = grid()
        initial = np.full(lat.shape, sf.GRASS, np.uint8)
        f = features([[(-8, 0), (8, 0)]], ["footway"], surfaces=[(0, "asphalt")])
        result = mappedpaths.paint_materials(initial, lat, lon, .25, [f])
        north = (lat - LAT) * M_LAT
        self.assertTrue((result[np.abs(north) < 1.] != sf.GRASS).all())
        edge = (np.abs(north) > 1.1) & (np.abs(north) < 1.4)
        self.assertTrue((result[edge] == sf.DIRT).all(), 'billboard roots have a narrow bare edge guard')
        self.assertTrue((result[np.abs(north) > 1.7] == sf.GRASS).all())
        self.assertTrue((result[np.abs(north) > 1.2] != sf.ROAD_MATERIAL['paved']).all())
        f = features([[(-8, 0), (8, 0)]], ["footway"], surfaces=[(0, "grass")])
        np.testing.assert_array_equal(mappedpaths.paint_materials(initial, lat, lon, .25, [f]), initial)

    def test_grass_gap_is_centred_in_root_space_without_shifting_the_physical_path(self):
        import mappedpaths
        lat, lon = grid(square=1.)
        initial = np.full(lat.shape, surface.GRASS, np.uint8)
        for vertical in (False, True):
            line = [[(.35, -25), (.35, 25)]] if vertical else [[(-25, .35), (25, .35)]]
            f = features(line, ["path"], surfaces=[(0, "asphalt")], widths=[(0, 1.2)])
            result = mappedpaths.paint_materials(initial, lat, lon, 1., [f])
            strip = result[32, :] if vertical else result[:, 32]
            cleared = np.flatnonzero(strip != surface.GRASS) - 32
            self.assertEqual(len(cleared), 2, 'registration changes, not a wider clearance band')
            # Nearest-sample grass cells extend half a metre either side of their vertex.
            # The earlier SW-square correction moved this gap by a whole sample here.
            self.assertAlmostEqual(float(cleared.mean()), .35, delta=.25)
            # Asphalt still belongs to its original physical square; only the grass guard moves.
            np.testing.assert_array_equal(np.flatnonzero(strip == surface.ASPHALT), [32])

    def test_pr653_promenade_surfaces_keep_mapped_widths_without_vehicle_plans(self):
        import mappedpaths
        # #653's Charzykowy promenade cases use the shared path pipeline, not a second road selector.
        for cls, width in (("pedestrian", 5.0), ("footway", 2.5), ("path", 2.5)):
            for surf in ("paving_stones", "interlocking_pavers", "flagstone", "flagstones", "tiles"):
                with self.subTest(cls=cls, surface=surf):
                    f = tagged([[(-8, 0), (8, 0)]], [cls],
                               {1: {"width": width, "osm_surface": surf, "osm_highway": cls}})
                    spec = mappedpaths.specs(f)[0]
                    self.assertEqual((spec.width, spec.kind), (width, "cobble"))
                    self.assertTrue(mappedpaths.engineered(spec))
                    self.assertIsNone(roads.road_specs(f)[0])
                    self.assertEqual(roads.extras(spec, np.array([[0., 0., 0.], [10., 0., 0.]]), LON), [])
    def test_join_keeps_exact_path_class(self):
        f = tagged([[(-8, 0), (8, 0)]], ["unknown"], {1: {"osm_highway": "bridleway"}})
        self.assertEqual(str(f.attrs["class"][0]), "bridleway")
        self.assertIsNone(roads.road_specs(f)[0], "not an unknown vehicle road")

    def test_paved_shared_path_material(self):
        # OSM way 55077740: cycleway, foot/bicycle=designated, segregated=no, surface=paved.
        f = tagged([[(-20, 0), (20, 0)]], ["cycleway"],
                   {1: {"osm_highway": "cycleway", "osm_surface": "paved"}})
        lat, lon = grid()
        idx = np.full(lat.shape, surface.GRASS, np.uint8)
        out = surface.paint_roads(idx, lat, lon, [f], 0.25)
        self.assertEqual(out[32, 32], surface.ASPHALT)
        self.assertEqual(out[50, 32], surface.GRASS, "one narrow shared corridor")

    def test_rock_bridleway_has_rock_physics(self):
        # OSM way 26819108: bridleway, surface=rock, sac_scale=hiking, foot/bicycle/horse=yes.
        f = tagged([[(-20, 0), (20, 0)]], ["path"],
                   {1: {"osm_highway": "bridleway", "osm_surface": "rock"}})
        lat, lon = grid()
        out = surface.paint_roads(np.full(lat.shape, surface.GRASS, np.uint8), lat, lon, [f], 0.25)
        self.assertEqual(out[32, 32], surface.ROCK)

    def test_built_path_has_no_hillside_crossfall(self):
        # Production Grader entrypoint: a paved north/south cycleway on a steep east/west bank.
        f = features([[(0, -100), (0, 100)]], ["cycleway"])
        grader, planner, lat, lon, base = path_grader(f)
        out = grader.apply(lat, lon, base)
        self.assertLess(abs(out[32, 34] - out[32, 30]), 0.03)
        self.assertAlmostEqual(out[40, 32] - out[24, 32], 0.2, delta=0.01)


def path_grader(feats, roads_plans=(), explicit=True):
    """Real production wrapper and native grading, deterministic elevation and in-memory map cells."""
    import grading
    import mapcells
    if explicit and ("surface" not in feats.rules or not len(feats.rules["surface"].feature)):
        from test_roads import rules
        feats.rules["surface"] = rules([(i, "asphalt") for i in range(len(feats))])
    frame = geodesy.EnuFrame(LAT, LON)
    key = mapcells.cell_of(LAT, LON)
    def elev(la, lo):
        e, n, _ = frame.geodetic_to_enu(la, lo, 0.0)
        return 100 + 0.8 * e + 0.05 * n
    planner = SimpleNamespace(frame=frame, md=SimpleNamespace(cell=lambda *a, **kw: SimpleNamespace(layers={"roads": feats})),
                              elev=Mock(side_effect=elev), _keys=lambda *a: [key],
                              plans=lambda *a, **kw: list(roads_plans), cells={key: []}, path_cells={key: True})
    planner.apply = lambda la, lo, base, *a, **kw: base
    grader = grading.Grader.__new__(grading.Grader)
    grader.store = SimpleNamespace(planners_of=lambda *a: [(planner, None)])
    lat, lon = grid()
    return grader, planner, lat, lon, elev(lat, lon)


class PathBenchTest(unittest.TestCase):
    def test_compacted_path_reuses_bench_without_changing_its_gravel_surface(self):
        import mappedpaths
        f = tagged([[(0, -100), (0, 100)]], ["footway"], {1: {"osm_surface": "compacted"}})
        grader, planner, lat, lon, _ = path_grader(f, explicit=False)
        def elev(la, lo):
            e, n, _ = planner.frame.geodetic_to_enu(la, lo, 0.)
            return 100 + .8 * e + .05 * n + .4 * np.sin(n * np.pi / 3)
        planner.elev = Mock(side_effect=elev)
        base = elev(lat, lon)
        out = grader.apply(lat, lon, base)
        self.assertEqual(mappedpaths.specs(f)[0].kind, "gravel")
        self.assertIsNone(roads.road_specs(f)[0])
        _, n, _ = planner.frame.geodetic_to_enu(lat[:, 32], lon[:, 32], 0.)
        np.testing.assert_allclose(out[:, 32], 100 + .05 * n, atol=.015)
        self.assertLess(np.abs(np.diff(out[:, 32], 2)).max(), .005)
        self.assertLess(np.abs(out[:, 34] - out[:, 30]).max(), .03)
        np.testing.assert_array_equal(out[:, :12], base[:, :12])
        calls = planner.elev.call_count
        np.testing.assert_array_equal(grader.apply(lat, lon, base), out)
        self.assertEqual(planner.elev.call_count, calls)

    def test_dem_crossfall_chatter_is_smoothed_before_limiting_the_bank(self):
        f = features([[(0, -100), (0, 100)]], ["cycleway"])
        grader, planner, lat, lon, _ = path_grader(f)
        def elev(la, lo):
            e, n, _ = planner.frame.geodetic_to_enu(la, lo, 0.)
            return 100 + (.01 + .2 * np.sin(n * np.pi / 3)) * e + .05 * n
        planner.elev = Mock(side_effect=elev)
        out = grader.apply(lat, lon, elev(lat, lon))
        # One metre across the path: retain gentle drainage, without alternating +/-2.5% banks.
        np.testing.assert_allclose(out[:, 34] - out[:, 30], .01, atol=.002)
        self.assertAlmostEqual(out[48, 32] - out[16, 32], .4, delta=.01)

    def test_paved_dem_chatter_is_smoothed_without_flattening_the_climb(self):
        for sf in ("asphalt", "rock"):
            with self.subTest(surface=sf):
                f = tagged([[(0, -100), (0, 100)]], ["cycleway"], {1: {"osm_surface": sf}})
                grader, planner, lat, lon, _ = path_grader(f)
                def elev(la, lo):
                    e, n, _ = planner.frame.geodetic_to_enu(la, lo, 0.)
                    return 100 + .8 * e + .05 * n + .4 * np.sin(n * np.pi / 3)
                planner.elev = Mock(side_effect=elev)
                base = elev(lat, lon)
                out = grader.apply(lat, lon, base)
                if sf == "rock":
                    np.testing.assert_array_equal(out, base)
                    planner.elev.assert_not_called()
                else:
                    _, n, _ = planner.frame.geodetic_to_enu(lat[:, 32], lon[:, 32], 0.)
                    np.testing.assert_allclose(out[:, 32], 100 + .05 * n, atol=.015)
                    self.assertLess(np.abs(np.diff(out[:, 32], 2)).max(), .005)

    def test_narrow_band_has_no_vehicle_wall_extension_or_query_size_artifact(self):
        import grading
        plan = grading.Plan(np.array([[0., 0.], [0., 10.]]), np.array([90., 90.]), 1.)
        x = np.array([1.25, 1.6, 2.5, 8.]); y = np.full(4, 5.); base = np.full(4, 100.)
        out = grading.grade_points(x, y, base, [plan], band=.5)
        self.assertLess(out[0], 100.)
        np.testing.assert_array_equal(out[1:], base[1:])
        for i in range(len(x)):
            np.testing.assert_array_equal(grading.grade_points(x[i:i+1], y[i:i+1], base[i:i+1],
                                                               [plan], band=.5), out[i:i+1])

    def test_smoothed_profile_is_consistent_across_source_cells(self):
        for surf in ("asphalt", "compacted"):
            with self.subTest(surface=surf):
                self._check_profile_across_source_cells(surf)

    def _check_profile_across_source_cells(self, surf):
        import grading
        import mapcells
        from test_grading import FakeMD
        from test_roads import FRAME
        edge = mapcells.cell_of(LAT, LON).bounds()[2]
        east, _, _ = FRAME.geodetic_to_enu(LAT, edge, 0.)
        f = features([[(east - 500, 0), (east + 500, 0)]], ["cycleway"], surfaces=[(0, surf)])
        def elev(la, lo):
            e, n, _ = FRAME.geodetic_to_enu(la, lo, 0.)
            return 100 + .02 * e + (.03 + .2 * np.sin(e * np.pi / 3)) * n + .4 * np.sin(e * np.pi / 3)
        x = np.arange(east - 150, east + 151)
        lat, lon, _ = FRAME.enu_to_geodetic(x, np.full(len(x), .5), 0.)
        def run(sl):
            g = grading.Grader(FRAME, FakeMD(f), elev)
            return g.apply(lat[sl], lon[sl], elev(lat[sl], lon[sl]))
        whole = run(np.s_[:])
        for sl in (np.s_[:50], np.s_[251:]):
            self.assertNotEqual(grading.Grader._keys(lat[sl], lon[sl]), grading.Grader._keys(lat, lon))
            np.testing.assert_allclose(run(sl), whole[sl], atol=1e-9)

    def test_natural_and_unsupported_paths_are_unchanged(self):
        for cls, sf in (("bridleway", "rock"), ("path", "ground"), ("cycleway", "grass"), ("footway", "wood")):
            with self.subTest(cls=cls, surface=sf):
                f = tagged([[(0, -100), (0, 100)]], [cls], {1: {"osm_surface": sf}})
                grader, planner, lat, lon, base = path_grader(f)
                np.testing.assert_array_equal(grader.apply(lat, lon, base), base)
                planner.elev.assert_not_called()
        f = tagged([[(0, -100), (0, 100)]], ["cycleway"], {1: {"osm_bridge": "yes"}})
        grader, planner, lat, lon, base = path_grader(f)
        np.testing.assert_array_equal(grader.apply(lat, lon, base), base)
        planner.elev.assert_not_called()

    def test_correction_is_local_and_profiles_are_reused(self):
        f = features([[(0, -100), (0, 100)]], ["cycleway"])
        grader, planner, lat, lon, base = path_grader(f)
        out = grader.apply(lat, lon, base)
        np.testing.assert_array_equal(out[:, :12], base[:, :12])
        calls = planner.elev.call_count
        np.testing.assert_array_equal(grader.apply(lat, lon, base), out)
        self.assertEqual(planner.elev.call_count, calls)
        self.assertLess(sum(np.size(c.args[0]) for c in planner.elev.call_args_list), 350)

    def test_repeated_queries_reuse_the_path_grader(self):
        import mappedpaths
        from unittest.mock import patch
        f = features([[(0, -100), (0, 100)]], ["cycleway"])
        grader, _planner, lat, lon, base = path_grader(f)
        with patch("mappedpaths.Grader", wraps=mappedpaths.Grader) as make:
            first = grader.apply(lat, lon, base)
            np.testing.assert_array_equal(grader.apply(lat, lon, base), first)
            self.assertEqual(make.call_count, 1)

    def test_vehicle_and_planned_only_queries_stay_vehicle_only(self):
        f = features([[(0, -100), (0, 100)]], ["cycleway"])
        grader, planner, lat, lon, base = path_grader(f)
        for kw in ({"roads_only": True}, {"planned_only": True}):
            np.testing.assert_array_equal(grader.apply(lat, lon, base, **kw), base)
        planner.elev.assert_not_called()

    def test_vehicle_core_and_sidewalk_are_preserved(self):
        import grading
        f = features([[(0, -100), (0, 100)]], ["cycleway"])
        p = grading.Plan(np.array([[-100., 0.], [100., 0.]]), np.array([100., 100.]), 3.0)
        grader, planner, lat, lon, base = path_grader(f, [p])
        out = grader.apply(lat, lon, base)
        np.testing.assert_array_equal(out[20:45], base[20:45])
        self.assertLess(abs(out[60, 34] - out[60, 30]), 0.03)

    def test_kept_raceway_surface_wins_without_a_vehicle_grading_plan(self):
        f = features([[(0, -100), (0, 100)]], ["cycleway"])
        grader, planner, lat, lon, base = path_grader(f)
        # Raceway banking/smoothing is applied by the planner without a vehicle Plan.
        planner._on_kept = lambda e, n: np.abs(n) <= 3.
        _, n, _ = planner.frame.geodetic_to_enu(lat, lon, 0.)
        kept = np.abs(n) <= 3.
        out = grader.apply(lat, lon, base)
        np.testing.assert_array_equal(out[kept], base[kept])
        self.assertGreater(np.abs(out[~kept] - base[~kept]).max(), .1)

    def test_profiles_clip_before_sampling_and_keep_overlap_alignment(self):
        import mappedpaths
        xy = np.array([[-100000., 0.], [0., 0.], [100000., 1000.]])
        left = np.concatenate(list(mappedpaths._profile_parts(xy, (-10, -10, 3, 10))))
        right = np.concatenate(list(mappedpaths._profile_parts(xy, (-3, -10, 10, 10))))
        self.assertLess(len(left), 15)
        shared_left = left[(left[:, 0] >= -2) & (left[:, 0] <= 2)]
        shared_right = right[(right[:, 0] >= -2) & (right[:, 0] <= 2)]
        np.testing.assert_array_equal(shared_left, shared_right)

    def test_arbitrary_tiles_and_query_order_return_identical_heights(self):
        f = features([[(-50, -50), (0, 0), (25, 100)]], ["cycleway"])
        grader, planner, lat, lon, base = path_grader(f)
        whole = grader.apply(lat, lon, base)
        for sl in (np.s_[:, :33], np.s_[:, 32:], np.s_[::2, ::3]):
            np.testing.assert_allclose(grader.apply(lat[sl], lon[sl], base[sl]), whole[sl], atol=1e-10)

    def test_close_unconnected_footway_is_not_height_matched(self):
        f = features([[(0, -100), (0, 100)], [(2.2, -100), (2.2, 100)]], ["cycleway", "footway"])
        grader, planner, lat, lon, base = path_grader(f)
        out = grader.apply(lat, lon, base)
        import mappedpaths
        self.assertLessEqual(np.abs(out - base).max(), mappedpaths.MAX_EARTHWORK_M)
        path = planner._path_grader
        ids = list(path.nodes)
        self.assertFalse(path.nodes[ids[0]] & path.nodes[ids[1]])
        import mappedpaths
        self.assertEqual(sum(sp is not None for sp in mappedpaths.specs(f)), 2)

    def test_separate_parallel_paths_retain_distinct_heights(self):
        f = features([[(-3, -100), (-3, 100)], [(3, -100), (3, 100)]], ["cycleway", "cycleway"])
        grader, planner, lat, lon, base = path_grader(f)
        out = grader.apply(lat, lon, base)
        self.assertAlmostEqual(out[32, 44] - out[32, 20], 4.8, delta=0.03)
        for centre in (20, 44):
            self.assertLess(abs(out[32, centre + 3] - out[32, centre - 3]), 0.05)

    def test_submetre_built_path_is_flat_across_and_keeps_its_width(self):
        f = tagged([[(0, -100), (0, 100)]], ["footway"], {1: {"width": 0.55}})
        grader, planner, lat, lon, base = path_grader(f)
        out = grader.apply(lat, lon, base)
        self.assertLess(abs(out[32, 33] - out[32, 31]), 0.015)
        import mappedpaths
        self.assertEqual(mappedpaths.specs(f)[0].width, 0.55)

    def test_failed_geometry_and_nonfinite_profiles_are_retried(self):
        f = features([[(0, -100), (0, 100)]], ["cycleway"])
        grader, planner, lat, lon, base = path_grader(f)
        good = planner.md.cell
        planner.md.cell = Mock(side_effect=OSError('offline'))
        np.testing.assert_array_equal(grader.apply(lat, lon, base), base)
        planner.md.cell = good
        planner.elev = Mock()
        # Preserve the input shape of whatever bounded profile is requested.
        planner.elev.side_effect = lambda la, lo: np.full(np.shape(la), np.nan)
        np.testing.assert_array_equal(grader.apply(lat, lon, base), base)
        planner.elev.side_effect = lambda la, lo: np.full(np.shape(la), 100.)
        self.assertFalse(np.array_equal(grader.apply(lat, lon, base), base))

    def test_path_presence_survives_road_plan_cache_and_old_cache_is_supported(self):
        import plancache
        import mapcells
        import worldbundle
        key = mapcells.cell_of(LAT, LON)
        with tempfile.TemporaryDirectory() as folder:
            cache = plancache.PlanCache(folder, 'w0s0')
            self.assertTrue(cache.store(key, [], [], [], [], has_paths=True))
            other = plancache.PlanCache(folder, 'w0s0')
            self.assertEqual(other.load(key), ([], [], [], [], []))
            self.assertIs(other.path_cells[key], True)
            self.assertEqual(worldbundle.validate_artifact(Path(cache._path(key)).read_bytes()), ([], [], [], [], []))
            self.assertTrue(cache.store(key, [], [], [], []))
            other.load(key)
            self.assertIsNone(other.path_cells[key])

    def test_loose_gravel_and_unmapped_surface_do_not_infer_earthworks(self):
        for sf in (None, "gravel", "fine_gravel"):
            with self.subTest(surface=sf):
                f = features([[(0, -100), (0, 100)]], ["cycleway"], surfaces=[] if sf is None else [(0, sf)])
                grader, planner, lat, lon, base = path_grader(f, explicit=False)
                np.testing.assert_array_equal(grader.apply(lat, lon, base), base)
                planner.elev.assert_not_called()

    def test_small_crossfall_is_retained_and_earthwork_is_capped(self):
        f = features([[(0, -100), (0, 100)]], ["cycleway"])
        grader, planner, lat, lon, base = path_grader(f)
        out = grader.apply(lat, lon, base)
        self.assertGreater(out[32, 34] - out[32, 30], 0.015)
        import mappedpaths
        extreme = base + 25
        altered = grader.apply(lat, lon, extreme)
        self.assertLessEqual(np.abs(altered - extreme).max(), mappedpaths.MAX_EARTHWORK_M)

    def test_shared_node_does_not_join_a_distant_returning_loop(self):
        f = features([[(0, 0), (0, 80)], [(0, 0), (20, -20), (20, 20), (1, 1)]],
                     ["cycleway", "footway"])
        grader, planner, lat, lon, base = path_grader(f)
        grader.apply(lat, lon, base)
        plans = planner._path_grader._plans(planner._keys(lat, lon), 0.0)
        narrow = next(p for p in plans if p.road_half < 1)
        la, lo, _ = planner.frame.enu_to_geodetic(narrow.xy[-1, 0], narrow.xy[-1, 1], 0.)
        self.assertAlmostEqual(narrow.z[-1], float(planner.elev(la, lo)), places=8)


class PathBridgeTest(unittest.TestCase):
    def test_short_approach_returns_to_its_mapped_far_endpoint(self):
        import grading
        f = features([[(0, -24), (0, 0)]], ["cycleway"])
        road = grading.Plan(np.array([[-50., -24.], [50., -24.]]), np.array([98.8, 98.8]), 3.)
        grader, planner, _, _, _ = path_grader(f, [road])
        _, _, deck_z = planner.frame.geodetic_to_enu(np.full(2, LAT), np.full(2, LON), np.full(2, 102.))
        bridge = SimpleNamespace(parts=lambda *a: [("deck", None, np.array([[0., 0.], [30., 0.]]), deck_z, LON)])
        planner.store = SimpleNamespace(path_bridge_view=lambda frame: bridge)
        lat, lon, _ = planner.frame.enu_to_geodetic(np.zeros(25), np.arange(-24., 1.), 0.)
        base = planner.elev(lat, lon)
        out = grader.apply(lat, lon, base)
        self.assertAlmostEqual(out[0], base[0], delta=.05)
        self.assertAlmostEqual(out[-1], 102., delta=.01)
        self.assertLess(np.abs(np.diff(out)).max(), .35)

    def test_explicit_cycle_bridge_uses_deck_pipeline_without_vehicle_extras(self):
        from test_roads import FRAME
        import mappedpaths
        f = tagged([[(-50, 0), (0, 0), (50, 20)], [(0, -50), (0, 50)]],
                   ["cycleway", "primary"], {1: {"osm_bridge": "yes", "osm_layer": 1,
                                                "osm_surface": "paved", "width": 2.5}})
        self.assertIsNone(mappedpaths.specs(f)[0])
        self.assertIsNone(roads.road_specs(f)[0])
        self.assertIsNone(roads.bridge_specs(f)[0])
        self.assertEqual(mappedpaths.specs(f, bridge=True)[0].width, 2.5)
        profile = roads.PathBridgeProfiles(FRAME, lambda key: f, flat)
        pieces = profile.parts([f], (-100, -100, 100, 100))
        self.assertTrue(pieces)
        xy, z = pieces[0][2:4]
        crossing = roads._crossings([xy], [np.array([[0., -50.], [0., 50.]])])[0]
        arc = np.r_[0., np.cumsum(np.linalg.norm(np.diff(xy, axis=0), axis=1))]
        self.assertGreaterEqual(float(np.interp(crossing[1], arc, z)) - 10.,
                                roads.deck_road_clear(profile.depth_of(str(f.ids[0]))))
        chunks = roads.build_tile(FRAME, 0, 0, [f], flat, path_bridges=profile)
        path_decks = [c for c in chunks if c.get('deck') and c.get('fid') == str(f.ids[0])]
        self.assertTrue(path_decks)
        self.assertTrue(all(c['w'] == 2.5 and c.get('fixed') for c in path_decks))
        self.assertEqual(roads.extras(mappedpaths.specs(f, bridge=True)[0], np.zeros((2, 3)), LON), [])

    def test_only_shared_bridge_endpoint_gets_a_bounded_flat_landing(self):
        for offset, height, connected in ((0., 103., True), (2., 103., False), (0., 110., False)):
            with self.subTest(offset=offset, height=height):
                f = features([[(0, -100), (0, 0)]], ["cycleway"])
                grader, planner, lat, lon, base = path_grader(f)
                xy = np.array([[offset, 0.], [30., 0.]])
                # Deck heights are ENU; ground profiles are geodetic heights.
                _, _, deck_z = planner.frame.geodetic_to_enu(
                    np.full(2, LAT), np.full(2, LON), np.full(2, height))
                bridge = SimpleNamespace(parts=lambda *a: [("deck", None, xy, deck_z, LON)])
                planner.store = SimpleNamespace(path_bridge_view=lambda frame: bridge)
                out = grader.apply(lat, lon, base)
                self.assertEqual(bool(planner._path_grader.approaches), connected)
                if connected:
                    self.assertAlmostEqual(out[32, 32], height, delta=.01)
                    self.assertLess(abs(out[30, 34] - out[30, 30]), .001)
                    self.assertLessEqual(np.abs(out - base).max(), 6.)
                else:
                    self.assertAlmostEqual(out[32, 32], base[32, 32], delta=.01)

    def test_unsafe_clearance_is_omitted_and_adjacent_tiles_share_deck_heights(self):
        from test_roads import FRAME
        f = tagged([[(-80, 0), (80, 0)], [(0, -50), (0, 50)]], ["cycleway", "primary"],
                   {1: {"osm_bridge": "yes", "osm_surface": "paved"}})
        profile = roads.PathBridgeProfiles(FRAME, lambda key: f, flat)
        left = profile.parts([f], (-100, -100, 0, 100))[0]
        right = profile.parts([f], (0, -100, 100, 100))[0]
        np.testing.assert_array_equal(left[2], right[2])
        np.testing.assert_array_equal(left[3], right[3])
        # A very short overpass cannot achieve normal road clearance with a bounded approach.
        short = tagged([[(-1, 0), (1, 0)], [(0, -50), (0, 50)]], ["cycleway", "primary"],
                       {1: {"osm_bridge": "yes", "osm_surface": "paved"}})
        unsafe = roads.PathBridgeProfiles(FRAME, lambda key: short, flat)
        chunks = roads.build_tile(FRAME, 0, 0, [short], flat, path_bridges=unsafe)
        self.assertFalse(any(c.get("deck") and c.get("fid") == str(short.ids[0]) for c in chunks))

    def test_layer_is_ordering_not_metres_and_ground_is_not_painted(self):
        import mappedpaths
        f = tagged([[(-10, 0), (10, 0)]], ["cycleway"], {1: {"osm_bridge": "yes", "osm_layer": 1,
                                                                       "osm_surface": "paved"}})
        lat, lon = grid()
        base = np.full(lat.shape, surface.GRASS, np.uint8)
        np.testing.assert_array_equal(surface.paint_roads(base, lat, lon, [f], .25), base)
        self.assertEqual(len(mappedpaths.bridge_parts(f, 0)), 1)


class PathSpecsTest(unittest.TestCase):
    def test_default_and_submetre_mapped_widths(self):
        import mappedpaths
        for cls, (width, _) in mappedpaths.STYLE.items():
            with self.subTest(cls=cls):
                f = features([[(-8, 0), (8, 0)]], [cls])
                self.assertEqual(mappedpaths.specs(f)[0].width, width)
                f = tagged([[(-8, 0), (8, 0)]], [cls], {1: {"width": 0.55}})
                sp = mappedpaths.specs(f)[0]
                self.assertEqual(sp.width, 0.55)
                self.assertEqual(sp.walk, 0)
                self.assertFalse(sp.parking)
                self.assertIsNone(sp.lanes)

    def test_invalid_widths_fall_back_and_osm_wins(self):
        import mappedpaths
        for value in (0, -1, float("nan"), float("inf"), 41):
            with self.subTest(width=value):
                f = tagged([[(-8, 0), (8, 0)]], ["path"], {1: {"width": value}})
                self.assertEqual(mappedpaths.specs(f)[0].width, 1.2)
        f = tagged([[(-8, 0), (8, 0)]], ["path"], {1: {"width": 0.55}}, widths=[(0, 3.0)])
        self.assertEqual(mappedpaths.specs(f)[0].width, 0.55)

    def test_surface_precedence_and_natural_defaults(self):
        import mappedpaths
        for cls in mappedpaths.STYLE:
            for sf, kind in (("asphalt", "paved"), ("concrete", "paved"), ("paving_stones", "cobble"),
                             ("compacted", "gravel"), ("ground", "dirt"), ("rock", "rock"), ("grass", "grass")):
                with self.subTest(cls=cls, surface=sf):
                    f = tagged([[(-8, 0), (8, 0)]], [cls], {1: {"osm_surface": sf}})
                    self.assertEqual(roads.surface_material_kind(mappedpaths.specs(f)[0]), kind)
        f = features([[(-8, 0), (8, 0)]], ["path"])
        self.assertEqual(mappedpaths.specs(f)[0].kind, "dirt")

    def test_no_same_name_class_width_or_structural_inheritance(self):
        import mappedpaths
        f = features([[(-8, 0), (8, 0)]], ["path"])
        f.attrs.update(src_record=np.array(["r9@1"]), name=np.array(["Main"]))
        osmtags.join(f, {9: {"name": "Main", "osm_highway": "cycleway", "width": 8,
                            "osm_bridge": "yes", "bx0": LON - 1, "by0": LAT - 1,
                            "bx1": LON + 1, "by1": LAT + 1}})
        self.assertEqual(str(f.attrs["class"][0]), "path")
        self.assertEqual(mappedpaths.specs(f)[0].width, 1.2)

    def test_off_ground_and_steps_are_not_painted(self):
        import mappedpaths
        for tag in ({"osm_bridge": "yes"}, {"osm_tunnel": "yes"}, {"osm_layer": 1}, {"osm_layer": -1}):
            with self.subTest(tag=tag):
                f = tagged([[(-8, 0), (8, 0)]], ["footway"], {1: tag})
                self.assertIsNone(mappedpaths.specs(f)[0])
        for kwargs in ({"flags": [(0, "is_bridge")]}, {"flags": [(0, "is_tunnel")]}, {"levels": [(0, 1.0)]}):
            f = features([[(-8, 0), (8, 0)]], ["path"], **kwargs)
            self.assertEqual(list(mappedpaths.parts([f])), [])
        f = tagged([[(-8, 0), (8, 0)]], ["unknown"], {1: {"osm_highway": "steps"}})
        self.assertEqual(list(mappedpaths.parts([f])), [])
        self.assertIsNone(roads.road_specs(f)[0])
        for sf in ("wood", "metal"):
            f = features([[(-8, 0), (8, 0)]], ["footway"], surfaces=[(0, sf)])
            self.assertIsNone(mappedpaths.specs(f)[0], 'boardwalks must not become asphalt')

    def test_no_vehicle_geometry_or_elevation_work(self):
        import mappedpaths
        f = features([[(-20, 0), (20, 0)]], ["bridleway"])
        elev = Mock(side_effect=AssertionError("ground paths must not acquire road profiles"))
        props, lines, park = [], [], []
        self.assertEqual(roads.build_tile(geodesy.EnuFrame(LAT, LON), 0, 0, [f], elev,
                                         props=props, walks=[], lines=lines, park=park), [])
        self.assertEqual((props, lines, park), ([], [], []))
        self.assertEqual(elev.call_count, 0)
        sp = mappedpaths.specs(f)[0]
        import navgraph
        self.assertNotIn(sp.cls, navgraph.DRIVE)

    def test_path_classes_stay_out_of_vehicle_selectors_when_styles_expand(self):
        # PR #653 adds these STYLE entries. STYLE membership must not imply vehicle-road behavior.
        import mappedpaths
        from unittest.mock import patch
        with patch.dict(roads.STYLE, mappedpaths.STYLE):
            for cls in mappedpaths.STYLE:
                f = features([[(-8, 0), (8, 0)]], [cls])
                self.assertIsNone(roads.road_specs(f)[0])
                self.assertIsNotNone(mappedpaths.specs(f)[0])
                deck = features([[(-8, 0), (8, 0)]], [cls], flags=[(0, "is_bridge")])
                self.assertIsNone(roads.bridge_specs(deck)[0], 'unsupported path structures stay excluded')

    def test_spatial_clipping_and_duplicate_cells(self):
        import mappedpaths
        f = features([[(-100, 0), (100, 0)], [(100, 100), (200, 200)]], ["path", "path"])
        bounds = (LON - 5 / M_LON, LAT - 5 / M_LAT, LON + 5 / M_LON, LAT + 5 / M_LAT)
        parts = list(mappedpaths.parts([f, f], bounds))
        self.assertEqual(len(parts), 1, "a crossing with both endpoints outside is retained once")
        self.assertLess(np.ptp(parts[0][2][:, 0]) * M_LON, 12)


class PathSurfaceTest(unittest.TestCase):
    def test_vehicle_crossing_material_wins(self):
        f = features([[(-20, 0), (20, 0)], [(0, -20), (0, 20)]], ["residential", "bridleway"],
                     surfaces=[(0, "asphalt"), (1, "rock")])
        lat, lon = grid()
        out = surface.paint_roads(np.full(lat.shape, surface.GRASS, np.uint8), lat, lon, [f], 0.25)
        self.assertEqual(out[32, 32], surface.ASPHALT)
        self.assertEqual(out[60, 32], surface.ROCK)

    def test_photo_width_parallel_paths_and_seams(self):
        f = features([[(-40, -3), (40, -3)], [(-40, 3), (40, 3)]], ["cycleway", "footway"],
                     surfaces=[(0, "paved"), (1, "paved")], widths=[(0, 0.6), (1, 0.6)])
        fr = geodesy.EnuFrame(LAT, LON)
        rgb = np.full((128, 128, 3), 100, np.uint8)
        whole = surface.photo_roads(rgb, fr, -16, -16, 32, [f], simple=True)
        self.assertFalse(np.array_equal(whole, rgb))
        np.testing.assert_array_equal(whole[64], rgb[64], "parallel paths have not become one road")
        # Square photos are the production contract; compare adjacent square tiles with a whole square.
        small = np.full((64, 64, 3), 100, np.uint8)
        a = surface.photo_roads(small, fr, -16, -8, 16, [f], simple=True)
        b = surface.photo_roads(small, fr, 0, -8, 16, [f], simple=True)
        np.testing.assert_array_equal(a[:, -1], b[:, 0])

    def test_rock_keeps_photo_and_natural_grade(self):
        f = features([[(-20, 0), (20, 0)]], ["bridleway"], surfaces=[(0, "rock")])
        rgb = np.full((64, 64, 3), 100, np.uint8)
        out = surface.photo_roads(rgb, geodesy.EnuFrame(LAT, LON), -8, -8, 16, [f], simple=True)
        np.testing.assert_array_equal(out, rgb)


class SupplementTest(unittest.TestCase):
    def test_paths_keep_busway_and_raceway_append_order_and_behavior(self):
        f = features([[(-8, 0), (8, 0)]], ["residential"], ids=["001"])
        coords = np.array([[LON, LAT], [LON + 8 / M_LON, LAT]])
        ways = [{"id": 8, "highway": "raceway", "coords": coords},
                {"id": 9, "highway": "busway", "oneway": "-1", "coords": coords}]
        tags = {2: {"osm_highway": "cycleway", "width": .55, "osm_surface": "concrete"}}
        fetch = Mock(return_value={2: coords})
        out = mapdata.MapData._osm(f, (tags, ways), path_geometry=fetch)
        self.assertEqual(out.ids.tolist(), ["001", "osm-busway-000000000009",
                                           "osm-path-000000000002", "osm-raceway-000000000008"])
        self.assertEqual(out.attrs["class"].tolist(), ["residential", "busway", "cycleway", "raceway"])
        self.assertAlmostEqual(roads.road_specs(out)[1].width, 7.)
        self.assertTrue(roads.road_specs(out)[1].oneway)
        self.assertEqual(out.rules["oneway"].for_feature(1)[0][0], -1.)
        import mappedpaths
        self.assertAlmostEqual(mappedpaths.specs(out)[2].width, .55)
        self.assertEqual(mappedpaths.specs(out)[2].surf, "concrete")

    def test_scoped_source_ids_prevent_a_duplicate_bridge(self):
        from test_roads import rules
        f = features([[(-30, 0), (30, 0)]], ['cycleway'], ids=['001'])
        f.attrs['src_record'] = np.array(['w1@1'])
        f.rules['src_ways'] = rules([(0, 'w1@1', 0, .4), (0, 'w2@1', .4, .6), (0, 'w3@1', .6, 1)])
        fetch = Mock(side_effect=AssertionError('bridge geometry already represented by Overture'))
        out = mapdata.MapData._osm(f, ({2: {'osm_highway':'cycleway','osm_bridge':'yes','osm_layer':1}}, []), path_geometry=fetch)
        fetch.assert_not_called()
        self.assertEqual(len(out), 1)

    def test_existing_geometry_is_kept_missing_ids_only_fetched(self):
        f = features([[(-8, 0), (8, 0)]], ["path"], ids=["001"])
        f.attrs["src_record"] = np.array(["w1@1"])
        tags = {1: {"osm_highway": "cycleway", "width": 0.8},
                2: {"osm_highway": "bridleway", "osm_surface": "rock", "width": 0.6}}
        coords = np.array([[LON - 8 / M_LON, LAT + 4 / M_LAT], [LON + 8 / M_LON, LAT + 4 / M_LAT]])
        fetch = Mock(return_value={2: coords})
        out = mapdata.MapData._osm(f, (tags, []), path_geometry=fetch)
        fetch.assert_called_once_with({2})
        self.assertEqual(len(out), 2)
        np.testing.assert_array_equal(out.lonlat[:2], f.lonlat)
        self.assertEqual(out.attrs["class"].tolist(), ["cycleway", "bridleway"])
        fetch.reset_mock()
        again = mapdata.MapData._osm(out, (tags, []), path_geometry=fetch)
        fetch.assert_not_called()
        self.assertEqual(len(again), 2)

    def test_empty_and_unavailable_geometry_keep_overture(self):
        f = features([[(-8, 0), (8, 0)]], ["path"], ids=["001"])
        f.attrs["src_record"] = np.array(["w1@1"])
        fetch = Mock(return_value={})
        self.assertIs(mapdata.MapData._osm(f, ({}, []), path_geometry=fetch), f)
        fetch.assert_not_called()
        out = mapdata.MapData._osm(f, ({2: {"osm_highway": "path"}}, []), path_geometry=fetch)
        self.assertEqual(len(out), 1)

    def test_bare_paths_are_selected_in_local_parquet(self):
        import duckdb
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "highways.parquet"
            con = duckdb.connect()
            raw = sorted(osmtags.USED - {"id", "bbox"})
            con.execute('CREATE TABLE hw (id BIGINT, highway VARCHAR, '
                        + ', '.join(f'"{c}" VARCHAR' for c in raw)
                        + ', bbox STRUCT(xmin FLOAT, ymin FLOAT, xmax FLOAT, ymax FLOAT))')
            con.execute("INSERT INTO hw (id,highway,bbox) VALUES (1,'bridleway',{'xmin':0,'ymin':0,'xmax':1,'ymax':1})")
            con.execute(f"COPY hw TO '{p.as_posix()}' (FORMAT PARQUET)")
            reader = osmtags.OsmTags.__new__(osmtags.OsmTags)
            reader._reader = Mock()
            groups = {0: {"first": 0, "n": 1, "cols": {c: [] for c in raw + ["id", "bbox", "highway"]}}}
            table = reader._decode(con, repr(p.as_posix()), groups, 0, Mock(), "demand")
            row = list(osmtags._rows(table, np.array([0])))[0]
            self.assertEqual(row[osmtags._ROW_VAL + list(osmtags.COLUMNS).index("osm_highway")], "bridleway")
            con.close()


    def test_cold_and_warm_range_reader_and_exact_missing_geometry(self):
        import duckdb
        import netfetch
        import rangeread
        import struct
        from test_rangeread import Fetcher

        class TaggedFetcher(Fetcher):
            def get(self, url, *, kind, headers):
                r = super().get(url, kind=kind, headers=headers)
                return netfetch.Response(r.status, r.body, dict(r.headers, etag='"paths-test-1"'))

        def wkb(y):
            return struct.pack('<BII4d', 1, 2, 2, 0.1, y, 0.9, y)

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "source.parquet"
            con = duckdb.connect()
            raw = sorted(osmtags.USED - {"id", "bbox"})
            con.execute('CREATE TABLE hw (id BIGINT, highway VARCHAR, '
                        + ', '.join(f'"{c}" VARCHAR' for c in raw)
                        + ', bbox STRUCT(xmin FLOAT,ymin FLOAT,xmax FLOAT,ymax FLOAT), geometry BLOB)')
            for wid, cls in ((1, 'cycleway'), (2, 'bridleway'), (3, 'residential')):
                y = wid * 0.1
                con.execute("INSERT INTO hw (id,highway,width,bbox,geometry) VALUES (?,?,'2 ft',"
                            "{'xmin':0.1,'ymin':?,'xmax':0.9,'ymax':?},?)", [wid, cls, y, y, wkb(y)])
            con.execute(f"COPY hw TO '{p.as_posix()}' (FORMAT PARQUET)")
            con.close()
            fetcher = TaggedFetcher(p.read_bytes())
            source = osmtags.OsmTags(str(Path(d) / 'cache'), fetcher, url='https://example.invalid/paths.parquet')
            tags, ways = source.cell((0, 0, 1, 1))
            self.assertFalse(ways)
            self.assertEqual(tags[2]['osm_highway'], 'bridleway')
            self.assertAlmostEqual(tags[1]['width'], 0.6096)
            geometry = source.highway_geometry((0, 0, 1, 1), way_ids={2})
            self.assertEqual(set(geometry), {2})
            np.testing.assert_array_equal(geometry[2], [[0.1, 0.2], [0.9, 0.2]])
            calls = len(fetcher.calls)
            source.cell((0, 0, 1, 1))
            source.highway_geometry((0, 0, 1, 1), way_ids={2})
            self.assertEqual(len(fetcher.calls), calls, 'warm data needs no new HTTP requests')
            source.highway_geometry((0, 0, 1, 1), way_ids=set())
            self.assertEqual(len(fetcher.calls), calls, 'no missing IDs: no request')
            rangeread.clear_memory()


class PathVegetationTest(unittest.TestCase):
    def test_path_clears_origins_but_keeps_overhang_and_rocks(self):
        import mappedpaths
        import vegetation as v
        f = features([[(-8, 0), (8, 0)]], ["path"], surfaces=[(0, "rock")])
        n = 4
        items = v.Items(np.full(n, LON), LAT + np.array([0, 0, 1.1, 0]) / M_LAT,
                        np.array([8., 1., 8., 0.4]), np.array([2., 0.4, 2., 0.2]),
                        np.array([v.KIND_TREE, v.KIND_BUSH, v.KIND_TREE, v.KIND_ROCK], np.uint8),
                        np.zeros(n, np.uint8), np.zeros(n), np.zeros(n), np.ones(n))
        out = mappedpaths.clear_obstructions(items, [f])
        self.assertEqual(out.kind.tolist(), [v.KIND_TREE, v.KIND_ROCK])
        f = features([[(-8, 0), (8, 0)]], ["footway"])
        out = mappedpaths.clear_obstructions(items, [f])
        self.assertEqual(out.kind.tolist(), [v.KIND_TREE, v.KIND_TREE], 'paved sidewalks also clear scattered rocks')


if __name__ == "__main__":
    unittest.main()
