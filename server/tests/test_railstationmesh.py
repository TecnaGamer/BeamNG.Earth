"""railstationmesh.py (task 037): the platforms' slabs stand the station's height over the rail head, kerb walls and strips."""
import os
import sys
import time
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import busstationmesh as bm  # noqa: E402
import railstationmesh as rm  # noqa: E402
import railstations as rs  # noqa: E402
from test_railstations import stations_of  # noqa: E402

BIG = (-2000.0, -2000.0, 2000.0, 2000.0)
FLAT = bm.DrawnGround(lambda xy: np.full(len(xy), 100.0))


def tops(m, mat_name):
    V, T, UV, mats, idx = m
    k = mats.index(mat_name)
    return V[np.unique(T[np.asarray(idx) == k])]


class PlatformMeshTest(unittest.TestCase):
    def test_top_stands_the_norm_over_the_rail_head(self):
        for name, country in (("vinnytsia", "UA"), ("hk_hln", "CZ"), ("passau", "DE")):
            st = stations_of(name)[0]
            st.country = country
            m = rm.mesh(None, BIG, [st], FLAT, (0.0, 0.0, 0.0))
            self.assertIsNotNone(m, name)
            V = tops(m, rm.CONCRETE[0])
            top = V[:, 2].max()
            want = 100.0 + rm.RAIL_TOP_M + max(rm.platform_height(p, country, len(st.platforms)) for p in st.platforms)
            self.assertAlmostEqual(top, want, delta=0.02, msg=name)

    def test_mapped_height_wins(self):
        st = stations_of("kyiv_pas")[0]
        st.country = "UA"
        tagged = [p for p in st.platforms if p.height_tag is not None]
        self.assertTrue(tagged)
        self.assertAlmostEqual(rm.platform_height(tagged[0], "UA", 9), tagged[0].height_tag)
        self.assertAlmostEqual(rm.platform_height(next(p for p in st.platforms if p.height_tag is None), "UA", 9), 1.10)

    def test_slope_is_followed_along_the_edge_not_across(self):
        st = stations_of("kalynivka")[0]
        st.country = "UA"
        slope = bm.DrawnGround(lambda xy: 100.0 + 0.01 * xy[:, 0])        # (a 1 % grade along x)
        m = rm.mesh(None, BIG, [st], slope, (0.0, 0.0, 0.0))
        V = tops(m, rm.CONCRETE[0])
        top = V[V[:, 2] > 100.5]                                           # (the slab's top, not its foot)
        self.assertGreater(len(top), 20)
        resid = top[:, 2] - (100.0 + 0.01 * top[:, 0] + rm.RAIL_TOP_M + rs.norm("UA", len(st.platforms))[1])
        self.assertLess(float(np.abs(resid).max()), 0.35)

    def test_kerb_walls_reach_below_the_ground(self):
        st = stations_of("vinnytsia")[0]
        st.country = "UA"
        m = rm.mesh(None, BIG, [st], FLAT, (0.0, 0.0, 0.0))
        V = tops(m, rm.KERB[0])
        self.assertLess(V[:, 2].min(), 100.0 - bm.SLAB_FOOT_M + 0.01)
        self.assertGreater(V[:, 2].max(), 100.0 + rm.RAIL_TOP_M)

    def test_strips_only_on_the_track_edge(self):
        st = stations_of("vinnytsia")[0]
        st.country = "UA"
        m = rm.mesh(None, BIG, [st], FLAT, (0.0, 0.0, 0.0))
        V = tops(m, rm.LINE[0])
        edge = rs.norm("UA")[0]
        self.assertGreater(len(V), 50)
        for p in V[::5]:
            d = min(bs_dist(xy, p[:2]) for _t, xy in st.tracks)
            self.assertLess(d, edge + rm.LINE_IN_M + rm.LINE_W_M + 0.2)
            self.assertGreater(d, edge + rm.STRIP_IN_M - 0.2)

    def test_tiles_split_the_station(self):
        st = stations_of("kozyatyn")[0]
        st.country = "UA"
        whole = rm.mesh(None, BIG, [st], FLAT, (0.0, 0.0, 0.0))
        parts = [rm.mesh(None, b, [st], FLAT, (0.0, 0.0, 0.0))
                 for b in ((-2000, -2000, 0, 2000), (0, -2000, 2000, 2000))]
        n = sum(0 if p is None else len(p[1]) for p in parts)
        self.assertEqual(n, len(whole[1]))

    def test_collision_boxes_cover_the_slabs(self):
        st = stations_of("hk_hln")[0]
        st.country = "CZ"
        got, boxes = rm.collision(None, BIG, [st], FLAT, (0.0, 0.0, 0.0))
        self.assertIsNotNone(got)
        self.assertTrue(boxes)
        V = np.asarray(got[0])
        for b in boxes:
            self.assertLess(b[2], 100.0)
            self.assertGreater(b[5], 100.0 + rm.RAIL_TOP_M)
        self.assertNotIn(rm.LINE[0], got[3])           # (no strips in the collider)

    def test_a_big_station_is_fast(self):
        st = stations_of("kyiv_pas")[0]
        st.country = "UA"
        t = time.time()
        m = rm.mesh(None, BIG, [st], FLAT, (0.0, 0.0, 0.0))
        self.assertLess(time.time() - t, 25.0)
        self.assertLess(len(m[0]), 60000)             # (one shape's indices)


def bs_dist(xy, p):
    import busstations as bs
    return bs.dist_to_path(xy, p)


if __name__ == "__main__":
    unittest.main()
