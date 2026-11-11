import json
import math
import os
import unittest
from unittest import mock

import numpy as np

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import europewalks
import geodesy
import grading
import mapgeom
import roadwidth
import roads
import streetfront
import surface
import test_roads

LAT, LON = 53.93463, -1.11081
M_LAT = 111320.
M_LON = M_LAT * math.cos(math.radians(LAT))


def town(lat, lon):
    return np.ones(np.shape(lat), bool)


class EuropeanSidewalksTest(unittest.TestCase):
    def setUp(self):
        flags = mock.patch.dict(os.environ, {"BNE_EU_ROAD_DETAILS": "on"})
        flags.start()
        self.addCleanup(flags.stop)
        # (York: the country without the border data, which the tests' regional fixture has only round Alsace)
        country = mock.patch.object(europewalks, "country_for", lambda lon, lat: "GB")
        country.start()
        self.addCleanup(country.stop)

    def features(self, lines, classes, **kwargs):
        with mock.patch.multiple(test_roads, LAT=LAT, LON=LON, M_LAT=M_LAT, M_LON=M_LON):
            return test_roads.features(lines, classes, **kwargs)

    def build(self, feats):
        walks, park, carriageways = [], [], []
        roads.build_tile(geodesy.EnuFrame(LAT, LON), 0, 0, [feats], test_roads.flat, urban_fn=town,
                         walks=walks, park=park, roads_out=carriageways, left_hand=True)
        return walks, park, carriageways

    def test_narrow_street_has_grass_between_two_paths_and_no_parking_lanes(self):
        feats = self.features([[(20, 100), (175, 100)]], ["residential"], widths=[(0, 5.)])
        feats.attrs["osm_sidewalk"] = np.array(["both"])
        walks, park, carriageways = self.build(feats)
        spec = carriageways[0][1]
        self.assertEqual(spec.width, 5.)
        self.assertEqual(roads.lane_layout(spec)[0], 5.)
        self.assertFalse(park)
        self.assertEqual({w["side"] for w in walks}, {1, -1})
        self.assertTrue(all(w["detached"] and w["height"] < .05 for w in walks))

        square = .25
        xs, ys = np.meshgrid(60 + square * np.arange(16), 94 + square * np.arange(48))
        painted = surface.paint_roads(np.full(xs.shape, surface.LAWN), LAT + ys / M_LAT, LON + xs / M_LON,
                                      [feats], square, town)
        self.assertEqual(painted[24, 8], surface.ROAD_MATERIAL["paved"])
        self.assertEqual(painted[37, 8], surface.LAWN)
        self.assertEqual(painted[41, 8], surface.ROAD_MATERIAL["paved"])

    def test_mapped_walk_replaces_one_side_and_keeps_its_surface_and_width(self):
        feats = self.features([[(20, 100), (175, 100)], [(20, 105), (175, 105)], [(60, 88), (60, 112)]],
                              ["secondary", "footway", "footway"],
                              surfaces=[(1, "concrete:plates")], widths=[(0, 7.), (1, 1.2)])
        feats.attrs["subclass"] = np.array(["", "sidewalk", "sidewalk"])
        walks, _, _ = self.build(feats)
        beside = [w for w in walks if w["fid"] == "id001"]
        self.assertTrue(beside)
        self.assertTrue(all(w["surface"] == "slabs" and abs(w["w"] - 1.2) < .001 for w in beside))
        inferred = [w for w in walks if w["fid"] == "id000"]
        self.assertEqual({w["side"] for w in inferred}, {-1})
        crossing = [w for w in walks if w["fid"] == "id002"]
        self.assertTrue(crossing)
        self.assertTrue(all(np.all(np.abs(w["pts"][:, 1] - 100.) > 4.) for w in crossing))

    def test_village_layout_and_backward_compatible_spec_encoding(self):
        spec = roads.Spec(10.8, "paved", True, "tertiary", False, lanes=2)
        with mock.patch.object(europewalks, "_locality", return_value=("Șerbănești", "village")):
            open_verges = europewalks.plan(spec, "RO", 24.69767, 44.32470, "", "", None, (.5, .5), True)
            known_path = europewalks.plan(spec, "RO", 24.69767, 44.32470, "", "right", 5.6, (.5, .5), True)
        self.assertEqual(open_verges.width, 5.2)
        self.assertFalse(open_verges.street.walks())
        self.assertEqual(known_path.width, 5.6)
        self.assertIsNone(known_path.street.left)
        self.assertGreater(known_path.street.right.setback, 0.)
        xy = np.c_[np.arange(8.) * grading.PROFILE_STEP, np.zeros(8)]
        grade = grading._plan(xy, np.zeros(len(xy)), open_verges)
        self.assertEqual(grade.road_half, 2.6)
        self.assertEqual(grade.half, 2.6 + grading.SHOULDER_M)

        data = json.loads(json.dumps(roads._spec_json(known_path)))
        self.assertEqual(roads.spec_from_json(data), known_path)
        del data["street"]
        self.assertEqual(roads.spec_from_json(data), known_path._replace(street=None))

    def test_parking_uses_extra_width_only_when_the_road_already_has_it(self):
        feats = self.features([[(20, 100), (175, 100)], [(20, 150), (175, 150)]],
                              ["secondary", "secondary"], widths=[(1, 12.)])
        feats.attrs["osm_lanes"] = np.array([2., 2.])
        estimated, mapped = roads.road_specs(feats, urban_fn=town)
        self.assertAlmostEqual(estimated.width, 6.6)
        self.assertEqual(estimated.street.parking, 0.)
        self.assertEqual(mapped.width, 12.)
        self.assertEqual(mapped.street.parking, 2.5)
        self.assertEqual(roads.lane_layout(mapped), (7., 2))

    def test_village_facades_choose_sides_and_profiles_independently(self):
        spec = roads.Spec(5.8, "paved", True, "secondary", False, lanes=2)
        centre = np.array([[0., 0.], [60., 0.]])
        walls = roadwidth.Walls([[0., 5., 60., 5.], [0., -10., 60., -10.]])
        frontage = roadwidth.verge_frontage(centre, walls, spec.width)
        raised, flush = europewalks.village_walks(spec, "FR", "", buildings=frontage)
        self.assertEqual((raised.setback, flush.setback), (0., 0.))
        self.assertGreater(raised.height, flush.height)
        self.assertEqual(flush.height, europewalks.LOW_EDGE_HEIGHT)
        spec = spec._replace(street=europewalks.Street(raised, flush, 0., True))
        extras = roads.extras(spec, np.c_[centre, np.zeros(2)], 7.8, country="FR")
        self.assertEqual([look["side"] for look, _, _ in extras if look["m"] == roads.LOW_GUTTER["m"]], [-1])
        self.assertEqual(europewalks.village_walks(spec, "FR", "no", buildings=frontage), (None, None))
        left, right = europewalks.village_walks(spec, "FR", "right", buildings=frontage)
        self.assertIsNone(left)
        self.assertEqual(right, flush)

    def test_romanian_through_road_needs_more_than_developed_land_cover(self):
        spec = roads.Spec(5.8, "paved", True, "secondary", False, lanes=2)
        open_verges = ((.03, .23, .55), (.10, .35, .46))
        built_side = ((.10, .58, .68), (0., .26, .42))
        self.assertEqual(europewalks.village_walks(spec, "RO", "", (1., 1.), True, open_verges), (None, None))
        left, right = europewalks.village_walks(spec, "RO", "", buildings=built_side)
        self.assertGreater(left.setback, 0.)
        self.assertIsNone(right)

    def test_parallel_pieces_of_a_tertiary_street_need_oneway_evidence(self):
        feats = self.features([[(20, 100), (90, 100)], [(20, 111), (90, 111)]], ["tertiary"] * 2)
        feats.attrs["name"] = np.array(["Village road"] * 2)
        specs = [roads.Spec(5.2, "paved", True, "tertiary")] * 2
        self.assertEqual(roads.dual_carriageways(feats, specs), specs)
        paired = roads.dual_carriageways(feats, [spec._replace(oneway=True, dir=1) for spec in specs])
        self.assertTrue(all(sp.median is not None for sp in paired))

    def test_divided_street_keeps_its_curbs_and_receives_the_surface_palette(self):
        feats = self.features([[(20, 100), (180, 100)], [(180, 130), (20, 130)]], ["secondary"] * 2)
        feats.attrs["name"] = np.array(["Divided street"] * 2)
        with mock.patch.object(europewalks, "street_surface", return_value="pavers"):
            walks, _, carriageways = self.build(feats)
        self.assertTrue(all(sp.median is not None and sp.street is None for _, sp, _, _ in carriageways))
        ordinary = [w for w in walks if "pts" in w and not w.get("median")]
        self.assertTrue(ordinary)
        self.assertTrue(all(w["surface"] == "pavers" and w["europe"] for w in ordinary))

    def test_frontage_paves_setbacks_but_avoids_a_drive_and_a_planted_island(self):
        frame = geodesy.EnuFrame(49.02, 7.8)
        walk = {"pts": np.array([[0., 3.75, 0.], [36., 3.75, 0.]]),
                "side": 1, "w": 1.5, "surface": "pavers", "mapped": True}
        house = np.array([[0., 5.7], [12., 5.7], [12., 8.], [0., 8.]])
        set_back = house + [24., 4.]
        # The little flowerbed is fully inside one connector quad.
        coords = np.array([[15., 5.8], [24., 5.8], [24., 8.], [15., 8.], [15., 5.8],
                           [19.8, 4.95], [20.1, 4.95], [20.1, 5.10], [19.8, 5.10], [19.8, 4.95]])
        lat, lon, _ = frame.enu_to_geodetic(coords[:, 0], coords[:, 1], 0.)
        lots = mapgeom.Features("land_use", np.array(["a", "b"]), np.full(2, mapgeom.POLYGON, np.uint8),
                               np.arange(3), np.arange(3), np.array([0, 5, 10]), np.c_[lon, lat],
                               attrs={"class": np.array(["parking", "flowerbed"])})
        road = [("r", roads.Spec(6., "paved", True, "residential"), np.array([[0., 0., 0.], [40., 0., 0.]]), 7.8)]
        drive = {"n": [[4.5, 0., 0.], [4.5, 8., 0.]], "w": 1.}
        chunks = streetfront.chunks(frame, [walk], [house, set_back], [lots], road, [drive], (-10, -10, 50, 20))
        # The shallow frontage joins the parking lot and the deeper frontage on
        # the same sidewalk, while the drive, sidewalk and flowerbed stay clear.
        import polygon
        points = np.array([[2., 4.75], [14.1, 5.1], [19.6, 5.025], [34., 9.5],
                           [4.5, 4.8], [19.95, 5.025], [5., 4.2], [18., 6.5]])
        hits = [(c["m"], polygon.contains_points(np.asarray(c["poly"]), points)) for c in chunks]
        np.testing.assert_array_equal(np.sum([mask for _, mask in hits], axis=0), [1, 1, 1, 1, 0, 0, 0, 0])
        self.assertEqual({m for m, mask in hits if mask.any()}, {"bne_eu_walk_pavers"})

        service = ("a", roads.Spec(2.5, "paved", True, "service"), np.array([[22., 0., 0.], [22., 7., 0.]]), 7.8)
        with mock.patch.object(roads, "ROADS_IN_GROUND", True):
            chunks = streetfront.chunks(frame, [walk], [house, set_back], [lots], road + [service], [],
                                        (-10, -10, 50, 20))
        access, = [c for c in chunks if "poly" not in c]
        self.assertEqual(access["w"], service[1].width)
        np.testing.assert_array_equal(np.asarray(access["n"])[:, 0], 22.)
        self.assertGreaterEqual(access["n"][0][1], 3.)
        self.assertGreaterEqual(access["n"][-1][1], 5.8)
