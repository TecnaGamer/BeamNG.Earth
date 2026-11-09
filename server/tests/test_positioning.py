"""threedmr.model_position's new level order (override -> threedmr_position_fixes.json -> tagged OSM way ->
desc -> api point) and buildings._custom_tile_entries' own nearest-footprint level, run after model_position
when nothing better placed a model. probe/position_audit.json: 8 of 3dmr.eu's own models sit 10-109 m from
their real OSM way; Baghdad's Al-Shaheed Monument (id 24) is now a POSITION_OVERRIDES entry instead."""

import math
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import buildings as bld  # noqa: E402
import threedmr  # noqa: E402
from test_buildings import rect, Feats  # noqa: E402


def _osm_square_xml(model_id, half_diag_e, half_diag_n, lat0=0.0, lon0=0.0, tag=True):
    """A minimal OSM API 0.6 map response: one closed way, a square centred on (lat0, lon0), its corners
    half_diag_e/half_diag_n local metres out - optionally tagged 3dmr=<model_id>."""
    corners_en = [
        (half_diag_e, 0.0), (0.0, half_diag_n), (-half_diag_e, 0.0), (0.0, -half_diag_n),
    ]
    coslat = math.cos(math.radians(lat0))
    nodes_xml, refs_xml = [], []
    for i, (e, n) in enumerate(corners_en, start=1):
        lat = lat0 + n / 111132.0
        lon = lon0 + e / (111320.0 * coslat)
        nodes_xml.append(f'<node id="{i}" lat="{lat:.9f}" lon="{lon:.9f}"/>')
        refs_xml.append(f'<nd ref="{i}"/>')
    refs_xml.append('<nd ref="1"/>')
    tag_xml = f'<tag k="3dmr" v="{model_id}"/>' if tag else ""
    return (
        "<?xml version='1.0'?><osm version='0.6'>"
        + "".join(nodes_xml)
        + '<way id="100">' + "".join(refs_xml) + '<tag k="building" v="yes"/>' + tag_xml
        + "</way></osm>"
    ).encode()


class CreditedNearNeverFetchesTest(unittest.TestCase):
    """credits.feature_ids asks credited_near from the server's event loop every second: it must never reach the
    network, even for a cached model whose OSM ring isn't cached yet (PR #854 review)."""

    def test_no_request_from_credited_near(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            c = os.path.join(d, "threedmr_cache")
            os.makedirs(c)
            with open(os.path.join(c, "info_123456.json"), "w") as f:
                json.dump({"id": 123456, "lat": 48.0, "lon": 11.0, "title": "t", "tags": {}}, f)
            boom = mock.Mock(side_effect=AssertionError("a request from the event loop"))
            with mock.patch.object(threedmr, "enabled", return_value=True), \
                    mock.patch.object(threedmr, "_osm_building_ring", boom), \
                    mock.patch.object(threedmr, "_fetch_retry", boom), \
                    mock.patch.dict(threedmr._near_memo, {}, clear=True):
                self.assertTrue(threedmr.credited_near(48.0, 11.0, 100.0, d))
            boom.assert_not_called()


class ModelPositionOrderTest(unittest.TestCase):
    """model_position's own level order: override > threedmr_position_fixes.json > tagged OSM way > desc >
    api point - each level checked ahead of the next only kicking in when the one before it has nothing."""

    def test_override_wins_over_everything(self):
        with mock.patch.dict(threedmr.POSITION_OVERRIDES, {777: (1.0, 2.0, 0.0)}), \
             mock.patch("threedmr.position_fix", return_value=(9.0, 9.0)), \
             mock.patch("threedmr.model_info", side_effect=AssertionError("no network for an override")):
            lat, lon, corrected, source = threedmr.model_position(777, "/nonexistent")
        self.assertEqual((lat, lon, corrected, source), (1.0, 2.0, True, "override"))

    def test_fix_table_used_without_network_and_ahead_of_osm(self):
        # id 15 (Hafenkran Kocks): a real threedmr_position_fixes.json entry - model_info must never be
        # consulted (no override, but the fix table alone settles it, no network at all)
        with mock.patch("threedmr.model_info", side_effect=AssertionError("fix table needs no network")):
            lat, lon, corrected, source = threedmr.model_position(15, "/nonexistent")
        self.assertAlmostEqual(lat, 50.9793305)
        self.assertAlmostEqual(lon, 6.9761433)
        self.assertTrue(corrected)
        self.assertEqual(source, "fix")

    def test_osm_polygon_level_beats_desc_and_api(self):
        # no override, no fix (a plain id): a tagged OSM way close to the api point outranks both desc and
        # the raw api point
        info = {"lat": 10.0, "lon": 20.0, "desc": "no osmgo link here"}
        xml = _osm_square_xml(555, 10.0, 10.0, lat0=10.0, lon0=20.0)
        with tempfile.TemporaryDirectory() as d, \
             mock.patch("threedmr.model_info", return_value=info), \
             mock.patch("threedmr._urllib_fetch", return_value=xml):
            lat, lon, corrected, source = threedmr.model_position(555, d)
        self.assertEqual(source, "osm")
        self.assertTrue(corrected)
        self.assertAlmostEqual(lat, 10.0, delta=1e-6)
        self.assertAlmostEqual(lon, 20.0, delta=1e-6)

    def test_desc_used_when_no_osm_tag(self):
        info = {
            "lat": 15.99797, "lon": 107.99513,
            "desc": "View: http://osmgo.org/?km=1&lat=16.05716901&lon=108.2318281",
        }
        with tempfile.TemporaryDirectory() as d, \
             mock.patch("threedmr.model_info", return_value=info), \
             mock.patch("threedmr._urllib_fetch", side_effect=OSError("no OSM reachable")):
            lat, lon, corrected, source = threedmr.model_position(556, d)
        self.assertEqual(source, "desc")
        self.assertAlmostEqual(lat, 16.05716901)
        self.assertAlmostEqual(lon, 108.2318281)

    def test_api_point_when_nothing_else_matches(self):
        info = {"lat": 11.0, "lon": 22.0, "desc": "no coordinates in this writeup"}
        with tempfile.TemporaryDirectory() as d, \
             mock.patch("threedmr.model_info", return_value=info), \
             mock.patch("threedmr._urllib_fetch", side_effect=OSError("network down")):
            lat, lon, corrected, source = threedmr.model_position(557, d)
        self.assertEqual((lat, lon, corrected, source), (11.0, 22.0, False, "api"))

    def test_no_reposition_env_skips_osm_level(self):
        info = {"lat": 10.0, "lon": 20.0, "desc": "no osmgo link"}
        xml = _osm_square_xml(558, 10.0, 10.0, lat0=10.0, lon0=20.0)
        with tempfile.TemporaryDirectory() as d, \
             mock.patch("threedmr.model_info", return_value=info), \
             mock.patch("threedmr._urllib_fetch", return_value=xml) as fetch, \
             mock.patch.dict(os.environ, {threedmr.NO_REPOSITION_ENV: "1"}):
            lat, lon, corrected, source = threedmr.model_position(558, d)
        self.assertEqual(source, "api")
        self.assertEqual((lat, lon), (10.0, 20.0))
        fetch.assert_not_called()


class OsmPolygonPositionTest(unittest.TestCase):
    """threedmr.osm_polygon_position: model_position's level 2 on its own."""

    def test_gate_rejects_a_far_centroid(self):
        # the OSM /map fetch itself cannot turn up a tagged way farther than ~OSM_BBOX_DEG out, but the gate
        # is checked on its own merits here (ring_xy forced far via a mocked _osm_ring_cached)
        far_ring = np.array([[600.0, 0.0], [604.0, 0.0], [604.0, 4.0], [600.0, 4.0]])
        with tempfile.TemporaryDirectory() as d, \
             mock.patch("threedmr._osm_ring_cached", return_value=(far_ring, True)):
            with self.assertLogs(threedmr.log, level="WARNING") as cm:
                pos = threedmr.osm_polygon_position(0.0, 0.0, 9, d)
        self.assertIsNone(pos)
        self.assertTrue(any("not trusted" in m for m in cm.output))

    def test_untagged_ring_not_used_for_position(self):
        # _osm_building_ring's own "nearest building" fallback (tagged=False) is good enough for osm_yaw's
        # rotation but never for placing the model - the wrong neighbour would silently relocate it
        near_ring = np.array([[5.0, 0.0], [9.0, 0.0], [9.0, 4.0], [5.0, 4.0]])
        with tempfile.TemporaryDirectory() as d, \
             mock.patch("threedmr._osm_ring_cached", return_value=(near_ring, False)):
            pos = threedmr.osm_polygon_position(0.0, 0.0, 9, d)
        self.assertIsNone(pos)

    def test_ambiguous_tag_matches_are_not_used_for_position(self):
        # id 35's own real data: 5 different OSM ways all tagged 3dmr=35 (apparently copy-pasted across a
        # city block import), alongside the one real node. _osm_building_ring must not pick any one of them
        # for a position (it has no way to tell which, if any, is right) - osm_yaw's rotation can still fall
        # back to whichever of the lot is nearest, untagged.
        xml = (
            "<?xml version='1.0'?><osm version='0.6'>"
            '<node id="1" lat="0.0000000" lon="0.0000900"/>'
            '<node id="2" lat="0.0000900" lon="0.0000900"/>'
            '<node id="3" lat="0.0000900" lon="0.0000000"/>'
            '<node id="4" lat="0.0000000" lon="0.0000000"/>'
            '<node id="11" lat="0.0005000" lon="0.0005900"/>'
            '<node id="12" lat="0.0005900" lon="0.0005900"/>'
            '<node id="13" lat="0.0005900" lon="0.0005000"/>'
            '<node id="14" lat="0.0005000" lon="0.0005000"/>'
            '<way id="100"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>'
            '<tag k="building" v="yes"/><tag k="3dmr" v="35"/></way>'
            '<way id="200"><nd ref="11"/><nd ref="12"/><nd ref="13"/><nd ref="14"/><nd ref="11"/>'
            '<tag k="building" v="yes"/><tag k="3dmr" v="35"/></way>'
            "</osm>"
        ).encode()
        with tempfile.TemporaryDirectory() as d, \
             mock.patch("threedmr._urllib_fetch", return_value=xml):
            ring, tagged = threedmr._osm_building_ring(0.0, 0.0, 35, None, 30.0)
            pos = threedmr.osm_polygon_position(0.0, 0.0, 35, d)
        self.assertFalse(tagged)
        self.assertIsNotNone(ring, "still usable for yaw, as the nearest of the ambiguous pair")
        self.assertIsNone(pos, "but never for position")

    def test_no_reposition_env_short_circuits_before_any_fetch(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch("threedmr._osm_ring_cached") as ring, \
             mock.patch.dict(os.environ, {threedmr.NO_REPOSITION_ENV: "1"}):
            pos = threedmr.osm_polygon_position(0.0, 0.0, 9, d)
        self.assertIsNone(pos)
        ring.assert_not_called()

    def test_shares_its_fetch_with_a_later_osm_yaw_call(self):
        # osm_polygon_position (api point as anchor) then osm_yaw (the corrected centroid as anchor, the way
        # buildings._model_mesh actually calls it via custom["latlon"]) must cost exactly one OSM /map request
        # between them, not two - the seeded cache entry (_seed_ring_cache) is what makes that true even when
        # the centroid rounds to a DIFFERENT cache key than the api point it was fetched around (an off-
        # centre ring, not a symmetric one - a symmetric square's own centroid would coincide with the api
        # point's key already, which would not exercise the seeding at all)
        lat0, lon0 = 1.0, 2.0
        clat_real, clon_real = lat0 + 0.0002, lon0 + 0.0002   # ~22 m north-east: a different rounded key
        xml = _osm_square_xml(4, 5.0, 5.0, lat0=clat_real, lon0=clon_real)
        self.assertNotEqual(round(lat0, 4), round(clat_real, 4), "the test must exercise a real key change")
        square_pts = np.array([[-5.0, -5.0], [5.0, -5.0], [5.0, 5.0], [-5.0, 5.0], [0.0, -5.0], [0.0, 5.0]])
        with tempfile.TemporaryDirectory() as d, \
             mock.patch("threedmr._urllib_fetch", return_value=xml) as fetch:
            clat, clon = threedmr.osm_polygon_position(lat0, lon0, 4, d)
            self.assertAlmostEqual(clat, clat_real, delta=1e-6)
            self.assertAlmostEqual(clon, clon_real, delta=1e-6)
            yaw = threedmr.osm_yaw(clat, clon, 4, square_pts, d)
        self.assertEqual(fetch.call_count, 1, "one OSM request shared by position and yaw")
        self.assertIsInstance(yaw, float)


class NearestBuildingFootprintTest(unittest.TestCase):
    """buildings._nearest_building_footprint: threedmr model_position's level 3, run in _custom_tile_entries
    only for a model whose own source is "api" (no override/fix/tagged-way/desc already placed it)."""

    def test_within_gates_is_used(self):
        items = [(bld.Spec(ring=rect(45.0, 55.0, 10.0, 10.0), height=6.0, min_h=0.0, cls=""), 0.0, 6.0)]
        fp = bld._nearest_building_footprint(items, 0.0, 0.0, 50.0, 60.0, model_major_m=10.0)
        self.assertIsNotNone(fp)
        self.assertAlmostEqual(fp[0], 50.0, delta=0.01)
        self.assertAlmostEqual(fp[1], 60.0, delta=0.01)

    def test_too_far_is_not_used(self):
        items = [(bld.Spec(ring=rect(195.0, 195.0, 10.0, 10.0), height=6.0, min_h=0.0, cls=""), 0.0, 6.0)]
        fp = bld._nearest_building_footprint(items, 0.0, 0.0, 0.0, 0.0, model_major_m=10.0)
        self.assertIsNone(fp, "200 m away: past FOOTPRINT_GATE_M")

    def test_size_mismatch_is_not_used(self):
        # a model with a 10 m major side next to a 2 m garden shed (ratio 0.2, under FOOTPRINT_SIZE_MIN)
        items = [(bld.Spec(ring=rect(4.0, 4.0, 2.0, 2.0), height=3.0, min_h=0.0, cls=""), 0.0, 3.0)]
        fp = bld._nearest_building_footprint(items, 0.0, 0.0, 5.0, 5.0, model_major_m=10.0)
        self.assertIsNone(fp)

    def test_ambiguous_same_size_candidates_are_skipped(self):
        # two candidate footprints of about the same size, both plausible by distance/size alone - "сомневаешься
        # - не двигай": neither is picked
        a = bld.Spec(ring=rect(0.0, 0.0, 10.0, 10.0), height=6.0, min_h=0.0, cls="")
        b = bld.Spec(ring=rect(30.0, 0.0, 11.0, 10.0), height=6.0, min_h=0.0, cls="")
        items = [(a, 0.0, 6.0), (b, 0.0, 6.0)]
        fp = bld._nearest_building_footprint(items, 0.0, 0.0, 10.0, 5.0, model_major_m=10.0)
        self.assertIsNone(fp)

    def test_clearly_smaller_second_candidate_does_not_block_the_match(self):
        # one obviously close/right-sized candidate plus a second, much smaller one still inside the gates:
        # not ambiguous - the close one is used
        close = bld.Spec(ring=rect(45.0, 55.0, 10.0, 10.0), height=6.0, min_h=0.0, cls="")
        tiny = bld.Spec(ring=rect(100.0, 100.0, 4.0, 4.0), height=6.0, min_h=0.0, cls="")
        items = [(close, 0.0, 6.0), (tiny, 0.0, 6.0)]
        fp = bld._nearest_building_footprint(items, 0.0, 0.0, 50.0, 60.0, model_major_m=10.0)
        self.assertIsNotNone(fp)
        self.assertAlmostEqual(fp[0], 50.0, delta=0.01)
        self.assertAlmostEqual(fp[1], 60.0, delta=0.01)

    def test_no_candidates_is_none(self):
        self.assertIsNone(bld._nearest_building_footprint([], 0.0, 0.0, 50.0, 60.0, model_major_m=10.0))

    def test_deterministic_across_repeat_calls(self):
        a = bld.Spec(ring=rect(45.0, 55.0, 10.0, 10.0), height=6.0, min_h=0.0, cls="")
        b = bld.Spec(ring=rect(140.0, 150.0, 12.0, 9.0), height=6.0, min_h=0.0, cls="")
        items = [(a, 0.0, 6.0), (b, 0.0, 6.0)]
        results = {
            bld._nearest_building_footprint(items, 0.0, 0.0, 50.0, 60.0, model_major_m=10.0)
            for _ in range(5)
        }
        self.assertEqual(len(results), 1, "the same inputs always pick the same footprint")


class CustomTileEntriesLevel3Test(unittest.TestCase):
    """buildings._custom_tile_entries: the nearest-footprint level actually moves a baked model's xy (and so
    its suppression radius) - only for a "source": "api" custom entry, before the radius is computed."""

    def setUp(self):
        import geodesy
        self.f = geodesy.EnuFrame(39.28, -76.59)

    @staticmethod
    def _gltf_mesh(pos_bbox):
        dx, dy = pos_bbox
        pos = np.array([[0.0, 0.0, 0.0], [dx, 0.0, 0.0], [0.0, dy, 1.0]])
        return (pos, np.zeros((3, 2)), np.array([[0, 1, 2]]), None, [(None, None, None, False, None)],
               np.zeros(1, np.int64))

    def _bake(self, custom, items, origin):
        ground = lambda la, lo: np.zeros(np.shape(la))
        with mock.patch("threedmr.fetch_model", return_value=b"glb"), \
             mock.patch("threedmr.gltf_mesh", side_effect=lambda glb, model_id=None: self._gltf_mesh((8.0, 8.0))), \
             mock.patch("threedmr.write_model",
                       return_value={"shape": "threedmr_x.cdae", "texture": None, "material": "threedmr_x",
                                    "has_alpha": False, "alpha_cutoff": None}), \
             mock.patch("threedmr.update_materials", return_value=None):
            return bld._custom_tile_entries(
                self.f, custom, ground, "/tmp/s1", "/temp/beamngEarth/s1", items=items, origin=origin
            )

    def test_source_api_is_moved_to_the_footprint(self):
        footprint = bld.Spec(ring=rect(45.0, 55.0, 10.0, 10.0), height=6.0, min_h=0.0, cls="")
        items = [(footprint, 0.0, 6.0)]
        custom = [{"id": 1, "xy": (50.0, 60.0), "source": "api"}]
        _entries, radii, _b = self._bake(custom, items, origin=(0.0, 0.0))
        self.assertEqual(len(radii), 1)
        self.assertAlmostEqual(radii[0][1], 50.0, delta=0.01)
        self.assertAlmostEqual(radii[0][2], 60.0, delta=0.01)

    def test_source_desc_is_not_moved(self):
        # desc-correction already placed this model more precisely than its own (known-stale) api point -
        # searching the map's own footprints near that stale point would be meaningless
        footprint = bld.Spec(ring=rect(45.0, 55.0, 10.0, 10.0), height=6.0, min_h=0.0, cls="")
        items = [(footprint, 0.0, 6.0)]
        custom = [{"id": 1, "xy": (200.0, 200.0), "source": "desc"}]
        _entries, radii, _b = self._bake(custom, items, origin=(0.0, 0.0))
        self.assertAlmostEqual(radii[0][1], 200.0, delta=0.01)
        self.assertAlmostEqual(radii[0][2], 200.0, delta=0.01)

    def test_local_model_is_never_moved(self):
        footprint = bld.Spec(ring=rect(45.0, 55.0, 10.0, 10.0), height=6.0, min_h=0.0, cls="")
        items = [(footprint, 0.0, 6.0)]
        custom = [{"id": "local_x", "xy": (50.0, 60.0), "source": "local", "local": {"file": "x.stl"}}]
        with mock.patch("threedmr.load_local_model", return_value=self._gltf_mesh((8.0, 8.0))), \
             mock.patch("threedmr.write_model",
                       return_value={"shape": "threedmr_x.cdae", "texture": None, "material": "threedmr_x",
                                    "has_alpha": False, "alpha_cutoff": None}), \
             mock.patch("threedmr.update_materials", return_value=None):
            _entries, radii, _b = bld._custom_tile_entries(
                self.f, custom, lambda la, lo: np.zeros(np.shape(la)), "/tmp/s1", "/temp/beamngEarth/s1",
                items=items, origin=(0.0, 0.0),
            )
        self.assertAlmostEqual(radii[0][1], 50.0, delta=0.01)
        self.assertAlmostEqual(radii[0][2], 60.0, delta=0.01)

    def test_without_items_or_origin_level3_is_skipped(self):
        custom = [{"id": 1, "xy": (50.0, 60.0), "source": "api"}]
        _entries, radii, _b = self._bake(custom, items=None, origin=None)
        self.assertAlmostEqual(radii[0][1], 50.0, delta=0.01)
        self.assertAlmostEqual(radii[0][2], 60.0, delta=0.01)

    def test_no_reposition_env_skips_level3_too(self):
        footprint = bld.Spec(ring=rect(45.0, 55.0, 10.0, 10.0), height=6.0, min_h=0.0, cls="")
        items = [(footprint, 0.0, 6.0)]
        custom = [{"id": 1, "xy": (50.0, 60.0), "source": "api"}]
        with mock.patch.dict(os.environ, {threedmr.NO_REPOSITION_ENV: "1"}):
            _entries, radii, _b = self._bake(custom, items, origin=(0.0, 0.0))
        self.assertAlmostEqual(radii[0][1], 50.0, delta=0.01)
        self.assertAlmostEqual(radii[0][2], 60.0, delta=0.01, msg="stayed at the api point, env disables level 3")


class PositionFixesTableTest(unittest.TestCase):
    """threedmr.position_fix / threedmr_position_fixes.json: an offline backstop, no network at all."""

    def test_known_id_from_the_real_table(self):
        lat, lon = threedmr.position_fix(15)
        self.assertAlmostEqual(lat, 50.9793305)
        self.assertAlmostEqual(lon, 6.9761433)

    def test_unknown_id_is_none(self):
        self.assertIsNone(threedmr.position_fix(999999))


if __name__ == "__main__":
    unittest.main()
