"""Mapped rural poles: geography, real positions, known aerial connectivity and retained content."""
import os
os.environ.setdefault("BNE_REGIONAL_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "regional"))  # (PR #505's data: the tests' slice)
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).parent)]
import blockworker as bw
import cdae
import franceroads
import furniture
import geodesy
import mapgeom
import mappedpoles
import poleprofiles
import test_regional_geotiles as regional_geo_tests


def infrastructure(frame, records):
    """Overture Features, including its point/line kinds and optional source tags."""
    coords, off = [], [0]
    for _, _, xy, _ in records:
        xy = np.asarray(xy, float).reshape(-1, 2)
        lat, lon, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
        coords.extend(zip(lon, lat))
        off.append(len(coords))
    n = len(records)
    return mapgeom.Features("infrastructure", np.array([r[0] for r in records]),
            np.array([1 if r[1] in mappedpoles.POLES else 2 for r in records], np.uint8),
            np.arange(n + 1), np.arange(n + 1), np.array(off), np.asarray(coords).reshape(-1, 2),
            attrs={"class": np.array([r[1] for r in records]),
                   "src_record": np.array([r[0] + "@1" for r in records]),
                   "tags": np.array([json.dumps(r[3]) for r in records])})


def pole(key, x, y=10., **tags):
    return key, "power_pole", [[x, y]], {"material": "wood", **tags}


def line(points, cls="minor_line", **tags):
    return "w1", cls, points, tags


def serial(wires):
    return [(w[0].tolist(), w[1]) for w in wires]


class MappedPoleTest(unittest.TestCase):
    def setUp(self):
        m = patch.object(franceroads, "ON", True)
        m.start()
        self.addCleanup(m.stop)
        self.frame = geodesy.EnuFrame(48.91623, 7.78231)
        self.box = (-100., -100., 100., 100.)
        self.profile = poleprofiles.geographic_profile("europe")

    def build(self, records, *, frame=None, box=None, urban=None, layers=None):
        frame = frame or self.frame
        props, wires = [], []
        layers = layers if layers is not None else [infrastructure(frame, records)]
        mappedpoles.place(frame, box or self.box, layers, lambda la, lo: np.zeros(np.shape(la)),
                          props, wires, self.profile, urban)
        return props, wires

    def test_positions_side_material_height_and_wire_attachments(self):
        records = [pole("n1", -20., material="concrete", height="9"), pole("n2", 20., height="10"),
                   line([[-20., 10.], [20., 10.]])]
        props, wires = self.build(records)
        self.assertEqual([r[0] for r in props], ["pole_concrete_recessed", "fr_wood_pole"])
        np.testing.assert_allclose([r[1:3] for r in props], [[-20, 10], [20, 10]], atol=.01)
        self.assertEqual([r[6] for r in props], [9., 10.])
        self.assertEqual(len(wires), 1)
        self.assertAlmostEqual(wires[0][0][0, 2] - props[0][3], 7.85 * 9 / 8.5, places=2)
        self.assertAlmostEqual(wires[0][0][-1, 2] - props[1][3], 7.85 * 10 / 8.5, places=2)
        self.assertIsNotNone(furniture.wire_mesh(wires, (0., 0., 0.)))

    def test_a_line_without_mapped_poles_does_not_invent_supports(self):
        self.assertEqual(self.build([line([[-60., 10], [60., 10]])]), ([], []))

    def test_isolated_poles_are_kept_without_guessed_nearest_neighbour_wires(self):
        props, wires = self.build([pole("n1", -20), pole("n2", 20)])
        self.assertEqual(len(props), 2)
        self.assertEqual(wires, [])

    def test_cables_require_explicit_overhead_location(self):
        for cls in ("cable", "communication_line"):
            for location, expected in (("overhead", 1), ("underground", 0), ("underwater", 0), ("", 0)):
                with self.subTest(cls=cls, location=location):
                    _, wires = self.build([pole("n1", -20), pole("n2", 20),
                                           line([[-20., 10], [20., 10]], cls, location=location)])
                    self.assertEqual(len(wires), expected)

    def test_telecom_supports_are_used_but_lighting_only_supports_are_not(self):
        records = [("n1", "utility_pole", [[-20., 10]], {"utility": "telecom", "material": "wood"}),
                   ("n2", "communication_pole", [[20., 10]], {"material": "wood"}),
                   ("n3", "utility_pole", [[0., 40]], {"utility": "street_lighting"}),
                   line([[-20., 10], [20., 10]], "communication_line", location="overhead")]
        props, wires = self.build(records)
        self.assertEqual(len(props), 2)
        self.assertEqual(len(wires), 1)

    def test_excluded_middle_support_does_not_get_a_wire_jumped_over_it(self):
        for tags in ({"location": "underground"}, {"disused": "yes"}, {"construction": "pole"}):
            props, wires = self.build([pole("n1", -30), pole("n2", 0, **tags), pole("n3", 30),
                                       line([[-30., 10], [0., 10], [30., 10]])])
            self.assertEqual(len(props), 2)
            self.assertEqual(wires, [])

    def test_urban_poles_are_unchanged_and_do_not_get_mapped_duplicates(self):
        props, wires = self.build([pole("n1", -20), pole("n2", 20), line([[-20., 10], [20., 10]])],
                                  urban=lambda la, lo: np.ones(np.shape(la), bool))
        self.assertEqual((props, wires), ([], []))
        def middle_is_urban(la, lo):
            x, _, _ = self.frame.geodetic_to_enu(la, lo, 0.)
            return np.abs(x) < 5.
        props, wires = self.build([pole("n1", -30), pole("n2", 0), pole("n3", 30),
                                  line([[-30., 10], [0., 10], [30., 10]])], urban=middle_is_urban)
        self.assertEqual(len(props), 2)
        self.assertEqual(wires, [])

    def test_other_countries_are_untouched_even_with_europe_profile(self):
        for la, lo in ((49., 8.2), (39., -76.), (45., -73.)):
            self.assertEqual(self.build([pole("n1", 0)], frame=geodesy.EnuFrame(la, lo)), ([], []))

    def test_off_and_no_kit_do_not_even_read_map_data(self):
        class Unreadable:
            def cell(self, *a, **kw):
                raise AssertionError("must not read")
        for on, profile in ((False, self.profile), (True, None)):
            with patch.object(franceroads, "ON", on), patch.object(mappedpoles, "EVERYWHERE", False):
                mappedpoles.tile_poles(self.frame, 0, 0, Unreadable(), None, [], [], profile)
                self.assertIsNone(mappedpoles.road_points(Unreadable(), self.frame, 0, 0, profile))

    def test_high_voltage_vertices_do_not_receive_duplicate_small_poles(self):
        self.assertEqual(self.build([pole("n1", -20), pole("n2", 20),
                                     line([[-20., 10], [20., 10]], "power_line")]), ([], []))

    def test_repeated_cells_and_reversed_lines_have_identical_output(self):
        records = [pole("n1", -20), pole("n2", 20), line([[-20., 10], [20., 10]])]
        f = infrastructure(self.frame, records)
        a, aw = self.build([], layers=[f, f])
        reverse = infrastructure(self.frame, records[:2] + [line([[20., 10], [-20., 10]])])
        b, bwires = self.build([], layers=[reverse])
        self.assertEqual(a, b)
        self.assertEqual(serial(aw), serial(bwires))

    def test_neighbouring_tiles_own_each_pole_and_span_exactly_once(self):
        records = [pole("n1", -20), pole("n2", 20), line([[-20., 10], [20., 10]])]
        whole, all_wires = self.build(records)
        left, lw = self.build(records, box=(-100., -100., 0., 100.))
        right, rw = self.build(records, box=(0., -100., 100., 100.))
        self.assertEqual(left + right, whole)
        self.assertEqual(serial(lw + rw), serial(all_wires))

    def test_long_or_bending_runs_do_not_invent_connections(self):
        for points in ([[-60., 10], [60., 10]], [[-20., 10], [0., 40], [20., 10]]):
            records = [pole("n1", *points[0]), pole("n2", *points[-1]), line(points)]
            props, wires = self.build(records)
            self.assertEqual(len(props), 2)
            self.assertEqual(wires, [])

    def test_ground_queries_remain_local_even_with_a_distant_point_in_the_layer(self):
        records = [pole("n1", 0), pole("n2", 15000)]
        f = infrastructure(self.frame, records)
        seen = []
        def elev(la, lo):
            seen.extend(zip(la, lo))
            return np.full(np.shape(la), 14.)
        props = []
        mappedpoles.place(self.frame, self.box, [f], elev, props, [], self.profile)
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(props), 1)
        self.assertAlmostEqual(props[0][3], 14., places=2)

    def test_positions_survive_a_change_of_anchor(self):
        f = infrastructure(self.frame, [pole("n1", -20), pole("n2", 20)])
        for frame in (self.frame, geodesy.EnuFrame(48.917, 7.780)):
            props, _ = self.build([], frame=frame, box=(-2000., -2000., 2000., 2000.), layers=[f])
            la, lo, _ = frame.enu_to_geodetic(np.array([r[1] for r in props]),
                                             np.array([r[2] for r in props]), np.array([r[3] for r in props]))
            np.testing.assert_allclose(np.c_[lo, la], f.lonlat, atol=1e-7)

    def test_real_gunstett_mapping_does_not_invent_the_unmapped_streetview_row(self):
        data = json.loads((Path(__file__).parent / "fixtures/gunstett_mapped_power.json").read_text())
        layers = []
        for r in data["records"]:
            ll = np.asarray(r["lonlat"])
            n = len(ll)
            layers.append(mapgeom.Features("infrastructure", np.array([r["id"]]),
                          np.array([1 if n == 1 else 2], np.uint8), np.array([0, 1]),
                          np.array([0, 1]), np.array([0, n]), ll,
                          attrs={"class": np.array([r["class"]]),
                                 "tags": np.array([json.dumps(r["tags"])])}))
        self.assertTrue(any(f.attrs["class"][0] == "power_pole" for f in layers))
        props, wires = self.build([], layers=layers)
        self.assertEqual((props, wires), ([], []))
        # This real mapped support is used when read without the already-rendered
        # high-voltage way, rather than treating every French rural pole as absent.
        f = next(f for f in layers if f.attrs["class"][0] == "power_pole")
        props, wires = self.build([], layers=[f], box=(-2000., -2000., 2000., 2000.))
        self.assertEqual(len(props), 1)
        self.assertEqual(wires, [])


class PipelineTest(unittest.TestCase):
    def setUp(self):
        regional_geo_tests.RegionalGeoTest.setUp(self)
        m = patch.object(franceroads, "ON", True)
        m.start()
        self.addCleanup(m.stop)
        bw._s.surface.urban = lambda la, lo: np.zeros(np.shape(la), bool)
        self.infra = infrastructure(self.tile.frame, [pole("n1", -20, 20), pole("n2", 20, 20),
                                                     line([[-20., 20], [20., 20]])])
        old_cell = bw._s.md.cell
        def cell(key, layers=(), kind="demand"):
            result = old_cell(key, layers, kind)
            result.layers["infrastructure"] = self.infra if key == bw._s.md.first else None
            return result
        bw._s.md.cell = cell

    def test_live_prebuilt_and_reused_content_keep_poles_and_native_wires(self):
        scratch = self.root / "prebuilt"
        key = bw.prebuild_geo(self.gx, self.gy, 15, str(scratch), True, False, False,
                              False, 0., None, "FR", pole_profile=self.profile)
        self.assertIsNotNone(key)
        out = self.root / "live"
        out.mkdir()
        with patch.object(bw, "_tile_parts", side_effect=AssertionError("must reuse complete prebuild")):
            bw._geo_content(self.gx, self.gy, 15, str(out), "/test", True,
                            False, False, False, 0., None, "FR", self.profile)
        names = bw.geo_names(self.tile.key)
        data = json.loads((out / names["json"]).read_text())
        groups = data["props"]["groups"]
        poles = [r for r in data["props"]["items"] if groups[r[0]] == "fr_wood_pole"]
        self.assertEqual(len(poles), 2)
        shape = out / Path(data["wire"]["shape"]).name
        self.assertTrue(cdae.read(shape)["meshes"])
        original_props = data["props"]
        for aid, lon in ((1, 7.816), (2, 8.02)):
            bw._geo_skin(aid, 49.026, lon, 15, self.gx, self.gy, str(out), "/test",
                          True, 0., 1 / 32, lambda: self.fail("content already present"))
            self.assertEqual(json.loads((out / names["json"]).read_text())["props"], original_props)


class EverywhereTest(unittest.TestCase):
    """Mapped poles outside the French scenery (BNE_MAPPED_POLES, on by default): every country, in town too, the
    stock pole and its stock wires; France's left to its own scenery while that places them."""

    def setUp(self):
        m = patch.object(franceroads, "ON", False)
        m.start()
        self.addCleanup(m.stop)
        self.frame = geodesy.EnuFrame(48.55, 11.50)      # Bavaria
        self.box = (-100., -100., 100., 100.)

    def build(self, records, frame=None, profile=None):
        frame = frame or self.frame
        props, wires = [], []
        mappedpoles.place(frame, self.box, [infrastructure(frame, records)], lambda la, lo: np.zeros(np.shape(la)),
                          props, wires, profile, everywhere=True)
        return props, wires

    def test_stock_poles_at_their_mapped_places_with_stock_wires(self):
        props, wires = self.build([pole("n1", -20., height="12"), pole("n2", 20.), line([[-20., 10.], [20., 10.]])])
        self.assertEqual([r[0] for r in props], [poleprofiles.STOCK.group] * 2)
        self.assertEqual([r[6] for r in props], [poleprofiles.STOCK.height] * 2, "the stock pole's own height")
        np.testing.assert_allclose([r[1:3] for r in props], [[-20, 10], [20, 10]], atol=.01)
        self.assertEqual(len(wires), len(furniture.WIRE_ARMS), "the stock pole's wires, as between guessed ones")
        self.assertTrue(all(isinstance(w, np.ndarray) for w in wires))
        self.assertIsNotNone(furniture.wire_mesh(wires, (0., 0., 0.)))

    def test_other_countries_too_and_nothing_invented(self):
        for la, lo in ((39., -76.), (45., -73.), (59.9, 10.7)):
            props, wires = self.build([pole("n1", 0)], frame=geodesy.EnuFrame(la, lo))
            self.assertEqual((len(props), wires), (1, []))
        self.assertEqual(self.build([line([[-60., 10], [60., 10]])]), ([], []))
        self.assertEqual(len(self.build([pole("n1", 0, disused="yes")])[0]), 0)

    def test_urban_poles_are_drawn(self):
        props, _ = [], []
        mappedpoles.place(self.frame, self.box, [infrastructure(self.frame, [pole("n1", 0)])],
                          lambda la, lo: np.zeros(np.shape(la)), props, [], None,
                          lambda la, lo: np.ones(np.shape(la), bool), everywhere=True)
        self.assertEqual(len(props), 1)

    def test_france_is_left_to_its_scenery_while_that_places_its_poles(self):
        fr = geodesy.EnuFrame(48.91623, 7.78231)
        profile = poleprofiles.geographic_profile("europe")
        self.assertEqual(len(self.build([pole("n1", 0)], frame=fr, profile=profile)[0]), 1, "scenery off: drawn")
        with patch.object(franceroads, "ON", True):
            self.assertEqual(self.build([pole("n1", 0)], frame=fr, profile=profile), ([], []))
            self.assertEqual(len(self.build([pole("n1", 0)], profile=profile)[0]), 1, "Germany: still drawn")

    def test_road_points_are_the_drawn_poles(self):
        records = [pole("n1", -20.), pole("n2", 20.), pole("n3", 0., location="underground"),
                   ("n4", "utility_pole", [[0., 40.]], {"utility": "street_lighting"}), line([[-20., 10], [20., 10]])]
        f = infrastructure(self.frame, records)

        class Md:
            def cell(self, key, layers=(), kind="demand"):
                return SimpleNamespace(layers={"infrastructure": f})
        with patch("roads.cells_of_tile", return_value=["a"]):
            pts = mappedpoles.road_points(Md(), self.frame, 0, 0)
        np.testing.assert_allclose(sorted(map(tuple, pts)), [(-20., 10.), (20., 10.)], atol=.01)

    def test_poles_join_a_line_only_at_its_vertices(self):
        """(PR #919 by wyo-backroads) A pole beside a line, or a crossing line's, is not on it; a telecom pole is not
        on a power line; a power=tower point or a mast is not a pole; a pole mapped twice counts once."""
        on = [pole("n1", -20.), pole("n2", 20.)]
        self.assertEqual(len(self.build(on + [line([[-20., 10.], [20., 10.]])])[1]), len(furniture.WIRE_ARMS))
        self.assertEqual(self.build(on + [line([[-20., 11.], [20., 11.]])])[1], [], "a metre beside: not its supports")
        self.assertEqual(self.build(on + [line([[-40., 10.], [40., 10.]])])[1], [], "along a segment: not a vertex")
        tel = [("n1", "communication_pole", [[-20., 10.]], {}), ("n2", "communication_pole", [[20., 10.]], {})]
        self.assertEqual(self.build(tel + [line([[-20., 10.], [20., 10.]])])[1], [])
        self.assertTrue(self.build(tel + [line([[-20., 10.], [20., 10.]], "communication_line",
                                               location="overhead")])[1])
        props, _ = self.build([pole("n1", -20., power="tower"), pole("n2", 20., man_made="mast"),
                               pole("n3", 0., man_made="mast", power="pole")])
        self.assertEqual(len(props), 1, "only the mast that is also tagged a power pole")
        f = infrastructure(self.frame, [pole("n1", -20.), pole("n9", -20.), pole("n2", 20.)])

        class Md:
            def cell(self, key, layers=(), kind="demand"):
                return SimpleNamespace(layers={"infrastructure": f})
        with patch("roads.cells_of_tile", return_value=["a"]):
            self.assertEqual(len(mappedpoles.road_points(Md(), self.frame, 0, 0)), 2)

    def test_field_lines_get_their_long_spans(self):
        """OpenStreetMap's minor_line is mostly the 10-30 kV line across the fields, 90-160 m between mapped poles:
        wired here (up to EVERYWHERE_MAX_SPAN_M), never past it."""
        props, wires = self.build([pole("n1", -75.), pole("n2", 75.), line([[-75., 10.], [75., 10.]])])
        self.assertEqual((len(props), len(wires)), (2, len(furniture.WIRE_ARMS)))
        self.assertGreater(min(float(w[:, 2].min()) for w in wires), 4.5, "sagging, not on the ground")
        far = mappedpoles.EVERYWHERE_MAX_SPAN_M / 2 + 10.
        self.assertEqual(self.build([pole("n1", -far), pole("n2", far), line([[-far, 10.], [far, 10.]])])[1], [])
