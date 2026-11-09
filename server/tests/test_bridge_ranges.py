"""A bridge on a long road has one extent in every cell that contains the road."""
import sys
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mapcells
import roads
import netfetch
import grading
from test_roads import FRAME, LAT, LON, features, rules


class BridgeContextTest(unittest.TestCase):
    def copies(self, centers=(0.0,), boundary=False, skew=False):
        # A 10 km feature, with 16 m raw spans. Its first point is several cells away.
        flags = [(0, "is_bridge", (x + 4992) / 10000, (x + 5008) / 10000) for x in centers]
        full = features([[(-5000, 0), (5000, 0)]] +
                        [[(x - (100 if skew else 0), -100), (x + (100 if skew else 0), 100)] for x in centers],
                        ["motorway"] + ["secondary"] * len(centers), widths=[(0, 14)] +
                        [(i + 1, 8) for i in range(len(centers))], flags=flags,
                        ids=["long"] + [f"cross{i}" for i in range(len(centers))])
        sparse = features([[(-5000, 0), (5000, 0)]], ["motorway"], widths=[(0, 14)],
                          flags=flags, ids=["long"])
        if boundary:
            # Put the bridge across a real map-cell boundary, without changing its geometry.
            delta = mapcells.cell_of(LAT, LON).bounds()[2] - LON
            full.lonlat[:, 0] += delta
            sparse.lonlat[:, 0] += delta
        rich_keys = set()
        for x in centers:
            p = roads.sub_polyline(full.parts(0)[0][0], (x + 5000) / 10000, (x + 5000) / 10000)[0]
            rich_keys.add(mapcells.cell_of(p[1], p[0]))
        calls = []

        def cells(keys):
            calls.extend(keys)
            return [full if k in rich_keys else sparse for k in keys]

        for f in (full, sparse):
            f.__dict__["_bnge_road_cells"] = cells
        return full, sparse, calls

    def test_long_road_copies_share_deck_and_ground_extents(self):
        full, sparse, calls = self.copies()
        a, b = roads.level_ranges(full, 0), roads.level_ranges(sparse, 0)
        np.testing.assert_allclose(np.asarray(a)[:, :2].astype(float), np.asarray(b)[:, :2].astype(float), atol=1e-12)
        self.assertGreater((b[0][1] - b[0][0]) * 10000, 22.0)
        # The ground complements the same deck, rather than overlapping another copy's deck.
        for f in (full, sparse):
            ground = roads.ground_parts(f, 0)
            deck = roads.bridge_parts(f, 0)[0]
            np.testing.assert_allclose(ground[0][-1], deck[0], atol=1e-12)
            np.testing.assert_allclose(ground[1][0], deck[-1], atol=1e-12)
        self.assertLessEqual(len(set(calls)), 9, "bridge-local cells, not the whole 10 km road")

    def test_boundary_bridge_is_independent_of_copy_and_load_order(self):
        for reverse in (False, True):
            full, sparse, calls = self.copies(boundary=True)
            pair = (sparse, full) if reverse else (full, sparse)
            ranges = [roads.level_ranges(f, 0) for f in pair]
            np.testing.assert_allclose(np.asarray(ranges[0])[:, :2].astype(float),
                                       np.asarray(ranges[1])[:, :2].astype(float), atol=1e-12)
            self.assertGreater((ranges[0][0][1] - ranges[0][0][0]) * 10000, 22.0)
            self.assertLessEqual(len(set(calls)), 9)

    def test_several_bridges_use_their_own_neighborhoods(self):
        full, sparse, calls = self.copies(centers=(-4000.0, 4000.0))
        spans = roads.level_ranges(sparse, 0)
        self.assertEqual(len(spans), 2)
        for a, b, _ in spans:
            self.assertGreater((b - a) * 10000, 22.0)
        self.assertGreater(len(set(calls)), 1, "the bridges do not share the road's first-point cell")
        self.assertLessEqual(len(set(calls)), 18)

    def test_context_supplies_the_upper_roads_locally_inferred_width(self):
        full, sparse, _ = self.copies(skew=True)
        for f in (full, sparse):
            f.rules["oneway"] = rules([(0, 1.0)], "float")
        full.attrs["osm_lanes"] = np.array(["3", ""])
        sparse.attrs["osm_lanes"] = np.array(["2"])
        # A11's bridge-local copy infers three lanes; distant copies infer two.
        a, b = roads.level_ranges(full, 0), roads.level_ranges(sparse, 0)
        np.testing.assert_allclose(np.asarray(a)[:, :2].astype(float), np.asarray(b)[:, :2].astype(float), atol=1e-12)

    def test_unavailable_context_does_not_leave_a_completed_short_bridge(self):
        full, sparse, calls = self.copies()
        read = sparse.__dict__["_bnge_road_cells"]
        def offline(keys):
            raise netfetch.Offline("bridge context unavailable")
        sparse.__dict__["_bnge_road_cells"] = offline
        with self.assertRaises(netfetch.Offline):
            roads.level_ranges(sparse, 0)
        sparse.__dict__["_bnge_road_cells"] = read
        spans = roads.level_ranges(sparse, 0)
        self.assertGreater((spans[0][1] - spans[0][0]) * 10000, 22.0)


class UnderpassSourceTest(unittest.TestCase):
    def lower(self, source=("road", 0, 0.0, 0.8), chained=False):
        # A stale cell copy has ground overlapping the extended bridge. Its curved source intersects the deck chord.
        ground = np.array([[-300., 0.], [-5., 1.], [0., 0.], [5., -1.], [300., 0.]])
        bridges = [np.array([[-20., 0.], [20., 0.]])]
        origins = [("road", 0, .35, .45)]
        if chained:
            bridges = [np.array([[-20., 0.], [0., 0.]]), np.array([[0., 0.], [20., 0.]])]
            origins = [("road", 0, .35, .40), ("road", 0, .40, .45)]
        data = dict(bridges=bridges, bridge_cls=["motorway"] * len(bridges), bridge_ids=["road"] * len(bridges),
                    bridge_sources=origins, bridge_levels=[1.] * len(bridges), bridge_half=[7.] * len(bridges),
                    ground=[(ground, 7., "motorway")], ground_ids=[(source[0], 0, 6., 14.)],
                    ground_sources=[source], rail_bridges=[], rail_ground=[], span_bridges=[],
                    ends=np.concatenate([[b[0], b[-1]] for b in bridges]),
                    end_of=[j for j in range(len(bridges)) for _ in range(2)])
        planner = grading._Planner(FRAME, None, lambda la, lo: np.full(np.shape(la), 50.))
        plans = [(source[0], grading.Plan(ground, np.full(len(ground), 50.), 7.))]
        with patch.object(planner, "_cell_enu", return_value=data):
            planner._lower_underpasses(mapcells.cell_of(LAT, LON), plans)
        return plans[0][1].z

    def test_overlapping_copy_cannot_dig_its_own_bridge_approach(self):
        np.testing.assert_allclose(self.lower(), 50., atol=1e-9)

    def test_chained_span_keeps_its_source_intervals(self):
        np.testing.assert_allclose(self.lower(chained=True), 50., atol=1e-9)

    def test_same_feature_at_a_different_source_position_still_passes_under(self):
        self.assertLess(float(self.lower(source=("road", 0, 0.0, 0.3)).min()), 45.)

    def test_a_different_road_still_passes_under(self):
        self.assertLess(float(self.lower(source=("lower", 0, 0.0, 0.8)).min()), 45.)

    def test_legacy_cell_provider_cannot_lower_its_own_abutment(self):
        # Exercise provenance construction too, with a provider lacking MapData's new shared-context hook.
        # The central copy sees the crossing road, neighboring copies retain the shorter raw bridge.
        line = [(-5000, 0), (-8, 1), (0, 0), (8, -1), (5000, 0)]
        flags = [(0, "is_bridge", .4992, .5008)]
        rich = features([line, [(0, -100), (0, 100)]], ["motorway", "secondary"],
                        widths=[(0, 14), (1, 8)], flags=flags, ids=["road", "under"])
        sparse = features([line], ["motorway"], widths=[(0, 14)], flags=flags, ids=["road"])
        key = mapcells.cell_of(LAT, LON)
        md = SimpleNamespace(cell=lambda k, **kw: SimpleNamespace(layers={"roads": rich if k == key else sparse}))
        planner = grading._Planner(FRAME, md, lambda la, lo: np.full(np.shape(la), 50.))
        cell = planner._cell_enu(key)
        plans = [(fid, grading.Plan(xy, np.full(len(xy), 50.), half))
                 for (xy, half, _), (fid, *_rest) in zip(cell['ground'], cell['ground_ids']) if fid == 'road']
        planner._lower_underpasses(key, plans)
        for _fid, plan in plans:
            np.testing.assert_allclose(plan.z, 50., atol=1e-9)


if __name__ == "__main__":
    unittest.main()
