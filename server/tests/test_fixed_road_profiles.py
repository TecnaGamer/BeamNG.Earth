"""Road-level separation and collision profiles: measured A3 underpass regression."""
import json
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import roadmesh
import roads


class UnderpassTopology(unittest.TestCase):
    def test_measured_a3_tunnel_keeps_both_mapped_portals(self):
        record = json.loads((Path(__file__).parent / "fixtures/a3_grasser_weg.json").read_text())
        xy = np.array(record["tunnel_xy"])
        s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
        ground = np.array(record["tunnel_ground"])
        surface = [(np.array(r["xy"]), r["width"]) for r in record["crossing_roads"]]
        _floor, old_kind = roads.tunnel_sections(s, ground)
        self.assertFalse(old_kind.any(), "the cached DEM alone misclassifies this underpass as open")
        _floor, kind = roads.tunnel_sections(s, ground, covered=roads.tunnel_crossing_mask(xy, surface))
        self.assertTrue((kind == 1).all(), "both A3 carriageways must pass above Graßer Weg")

    def test_parallel_road_and_portal_junction_do_not_imply_cover(self):
        xy = np.c_[np.arange(0.0, 41.0, 4.0), np.zeros(11)]
        neighbors = [(xy + [0.0, 5.0], 13.0), (np.array([[0.0, -20.0], [0.0, 20.0]]), 13.0)]
        self.assertFalse(roads.tunnel_crossing_mask(xy, neighbors).any())

    def test_missing_headroom_does_not_create_a_tube_above_the_surface_road(self):
        xy = np.c_[np.arange(0.0, 41.0, 4.0), np.zeros(11)]
        surface = [(np.array([[20.0, -50.0], [20.0, 50.0]]), 13.0)]
        mask = roads.tunnel_crossing_mask(xy, surface)
        self.assertTrue(mask.any())
        floor, kinds = roads.tunnel_sections(xy[:, 0], np.full(len(xy), 100.0), covered=mask)
        self.assertFalse(kinds.any(), "topology alone cannot invent clearance missing from the DEM")
        self.assertTrue((floor == 100.0).all())

    def test_distant_hill_does_not_supply_headroom_at_a_flat_crossing(self):
        s = np.arange(0.0, 401.0, 4.0)
        ground = 100.0 + np.maximum(0.0, 12.0 - np.abs(s - 300.0) * 0.3)
        forced = np.abs(s - 60.0) <= 8.0
        _floor, kinds = roads.tunnel_sections(s, ground, covered=forced)
        self.assertFalse(kinds[forced].any(), "each crossing needs its own measured headroom")
        self.assertTrue(kinds[s == 300.0].any(), "the separate hill still contains a tunnel")

    def test_raw_peak_cannot_override_missing_final_graded_clearance(self):
        s = np.arange(0.0, 121.0, 4.0)
        ground = 100.0 + 6.0 * np.exp(-((s - 60.0) ** 2) / 16.0)
        forced = np.abs(s - 60.0) <= 8.0
        upper = np.where(forced, 101.35, np.nan)
        _floor, kinds = roads.tunnel_sections(s, ground, covered=forced, upper=upper)
        self.assertFalse(kinds[forced].any(), "the smoothed upper road cannot fit the minimum tunnel vault")


class CollisionProfile(unittest.TestCase):
    def test_surface_crossing_preserves_portal_cap_limit(self):
        from test_roads import features, FRAME
        f = features([[(-20, 0), (20, 0)], [(-16, -100), (-16, 100)]],
                     ["residential", "motorway"], widths=[(1, 7.0)], flags=[(0, "is_tunnel")])

        def elev(lat, lon):
            e, _n, _u = FRAME.geodetic_to_enu(lat, lon, 0.0)
            return 100.0 + np.where(np.abs(e) < 18.0, 6.0, 0.0)

        tunnel = roads.TunnelProfiles(FRAME, lambda key: f, elev)
        chunks = [c for c in roads._tunnel_chunks(tunnel, [f], (-128.0, -128.0, 128.0, 128.0))
                  if c.get("tunnel")]
        self.assertTrue(chunks)
        self.assertFalse(chunks[0]["arch_cap"], "the upper road supplies this mapped crossing's cover")
        self.assertLess(chunks[0]["cap_m"][0], roadmesh.LOW_CAP_M,
                        "the portal's stone top still stops before the upper carriageway")

    def test_tube_height_fits_the_final_upper_road(self):
        from test_roads import features, FRAME
        f = features([[(0, -140), (0, 140)], [(-60, 0), (60, 0)]],
                     ["motorway", "tertiary"], widths=[(0, 13.0), (1, 7.0)], flags=[(1, "is_tunnel")])

        def raw(lat, lon):
            e, _n, _u = FRAME.geodetic_to_enu(lat, lon, 0.0)
            return 100.0 + 20.0 * np.exp(-(e * e) / 16.0)

        upper = lambda la, lo: np.full(np.shape(la), 105.0)
        tunnel = roads.TunnelProfiles(FRAME, lambda key: f, upper)
        tunnel.raw = raw
        tunnel.ends = lambda la, lo: np.full(np.shape(la), 100.0)
        parts = tunnel.parts([f], (-100.0, -100.0, 100.0, 100.0))
        self.assertTrue(parts)
        self.assertTrue(tunnel.heights, "the raw full-size tube must become a lower vault")
        for fid, _spec, _xy, floor, _lon in parts:
            self.assertLessEqual(float(np.max(floor)) + tunnel.height(fid) + roads.ARCH_SOIL_MIN, 105.001)
        raised = roads.arch_raise(parts, tunnel.heights, np.zeros(1), np.zeros(1), np.array([105.0]), tunnel.mouths)
        self.assertLessEqual(float(raised[0]), 105.001)

    def test_straight_bridge_retains_its_solved_vertical_curve(self):
        # A straight plan view must not reduce a bridge's vertical curve to one straight chord.
        x = np.arange(0.0, 100.1, 4.0)
        z = 10.0 + 0.85 * np.sin(np.pi * x / x[-1])
        pts = np.c_[x, np.zeros(len(x)), z]
        spec = roads.Spec(8.0, "paved", False, "primary")
        chunks = [c for c in roads._deck_chunks([("curve", spec, pts, 12.0)], (-1.0, -10.0, 101.0, 10.0))
                  if c.get("deck") and not c.get("ctx")]
        mesh = roadmesh.deck(chunks, (0.0, 0.0, 0.0))
        self.assertIsNotNone(mesh)
        emitted = np.asarray(chunks[0]["n"])
        actual = np.interp(x, emitted[:, 0], emitted[:, 2])
        self.assertLess(np.max(np.abs(actual - z)), roads.FIXED_Z_TOL + 0.001)


if __name__ == "__main__":
    unittest.main()
