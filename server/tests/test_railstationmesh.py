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


class RoofsAndFurnitureTest(unittest.TestCase):
    def test_roofs_are_open_on_columns_standing_on_the_platform(self):
        import busstations as bs
        st = stations_of("hk_hln")[0]
        st.country = "CZ"
        self.assertTrue(st.roofs)
        self.assertEqual(st.claimed, {r[0] for r in st.roofs})
        m = rm.mesh(None, BIG, [st], FLAT, (0.0, 0.0, 0.0))
        V = tops(m, rm.STEEL[0])
        h = max(rm.platform_height(p, "CZ", len(st.platforms)) for p in st.platforms)
        floor = 100.0 + rm.RAIL_TOP_M + h
        self.assertGreater(V[:, 2].max(), floor + rm.ROOF_H_M - 0.5)       # (the roof's top over the platform)
        cols = V[V[:, 2] < floor + 1.0]
        self.assertGreater(len(cols), 8)
        edge = rs.norm("CZ")[0]
        for p in cols[::3]:
            self.assertGreater(min(bs.dist_to_path(xy, p[:2]) for _t, xy in st.tracks), edge + rm.COLUMN_TRACK_M - 0.4)

    def test_collider_has_the_columns_not_the_roof(self):
        st = stations_of("hk_hln")[0]
        st.country = "CZ"
        got, _boxes = rm.collision(None, BIG, [st], FLAT, (0.0, 0.0, 0.0))
        V = tops(got, rm.STEEL[0]) if rm.STEEL[0] in got[3] else np.zeros((0, 3))
        h = max(rm.platform_height(p, "CZ", len(st.platforms)) for p in st.platforms)
        self.assertLess(V[:, 2].max() if len(V) else 0.0, 100.0 + rm.RAIL_TOP_M + h + rm.ROOF_H_M + 0.1)   # (columns reach the roof's underside, not its top)

    def test_furniture_on_the_platform_facing_the_track(self):
        import busstations as bs
        import railstationprops as rp
        st = stations_of("trutnov")[0]                                     # (bench / bin / lit / covered tags)
        n = 0
        for pl in st.platforms:
            for group, x, y, fx, fy, kind in rp.furniture(pl):
                self.assertTrue(bs.contains(pl.ring, (x, y)))
                near = min((bs.dist_to_path(xy, np.array([x, y])), xy) for _t, xy in st.tracks)[0]
                self.assertGreater(near, rs.norm("CZ")[0] + 0.3)
                n += 1
        self.assertGreater(n, 4)

    def test_no_furniture_on_a_narrow_platform(self):
        import railstationprops as rp
        ring = np.array([[0.0, 0.0], [60.0, 0.0], [60.0, 2.0], [0.0, 2.0]])
        pl = rs.Platform("x", ring, {"bench": "yes"})
        self.assertEqual(rp.furniture(pl), [])

    def test_keepout_covers_the_platforms(self):
        import busstationkeep as keep
        st = stations_of("vinnytsia")[0]
        k = keep.of_rail_stations([st])
        self.assertTrue(k)
        c = np.asarray([bs_centroid(p.ring) for p in st.platforms])
        self.assertTrue(k.contains(c, 0.0).all())

    def test_tile_skip_is_the_claimed_roofs(self):
        st = stations_of("hk_hln")[0]
        self.assertEqual(rs_skip(st), {r[0] for r in st.roofs})


def bs_centroid(ring):
    import busstations as bs
    return bs.centroid(ring)


def rs_skip(st):
    return set(st.claimed)


class FootbridgeTest(unittest.TestCase):
    def over(self, name, country="UA"):
        st = stations_of(name)[0]
        st.country = country
        links = [l for l in st.links if l.kind == "over"]
        self.assertTrue(links, name)
        return st, links

    def test_deck_clears_every_rail_head_it_crosses(self):
        import railbridges as rb
        for name, c in (("kozyatyn", "UA"), ("hk_hln", "CZ"), ("pardubice", "CZ"), ("passau", "DE")):
            st, links = self.over(name, c)
            for ln in links:
                z = rb.deck_height(None, FLAT, st, ln)
                self.assertAlmostEqual(z, 100.0 + rb.RAIL_TOP_M + rb.CLEAR_M + rb.DECK_D, delta=0.01)

    def test_mesh_has_deck_rails_stairs_and_piers(self):
        import busstations as bs
        import railbridges as rb
        st, links = self.over("kozyatyn")
        m = bm._Mesh()
        rb.add(m, None, FLAT, st, links[0])
        got = m.result(BIG, (0.0, 0.0, 0.0))
        self.assertIsNotNone(got)
        V = got[0]
        zdeck = 100.0 + rb.RAIL_TOP_M + rb.CLEAR_M + rb.DECK_D
        self.assertAlmostEqual(V[:, 2].max(), zdeck + rb.HAND_H_M, delta=0.02)       # (the handrail's top)
        self.assertLess(V[:, 2].min(), 100.0)                                         # (stairs and piers reach the ground)
        self.assertGreater(len(m.M), 100)

    def test_piers_stand_clear_of_the_tracks(self):
        import busstations as bs
        import railbridges as rb
        st, links = self.over("hk_hln", "CZ")
        for ln in links:
            m = bm._Mesh()
            w = rb.width_of(ln)
            path = rb.resample(ln.xy)
            z = rb.deck_height(None, FLAT, st, ln)
            arcs = [a for _k, a in ln.crosses]
            rb._piers(m, None, FLAT, st, path, z, w, max(0.0, min(arcs) - 4.0), min(float(rb._arc(path)[-1]), max(arcs) + 4.0))
            got = m.result(BIG, (0.0, 0.0, 0.0))
            if got is None:
                continue
            P = got[0][got[0][:, 2] < 100.0 + 3.0]
            for p in P[::7]:
                d = min(bs.dist_to_path(xy, p[:2]) for _t, xy in st.tracks)
                self.assertGreater(d, rb.PIER_TRACK_M - 1.0)

    def test_stairs_rise_to_the_deck_in_small_steps(self):
        import railbridges as rb
        m = bm._Mesh()
        line = np.array([[0.0, 0.0], [12.0, 0.0]])
        got = rb._stair(m, None, FLAT, line[0], line, 106.0, 2.0)
        self.assertIsNotNone(got)
        r = m.result(BIG, (0.0, 0.0, 0.0))
        tops = np.unique(np.round(r[0][:, 2], 3))
        tops = tops[(tops > 100.2) & (tops <= 106.0)]
        self.assertTrue(np.all(np.diff(tops) < rb.RISE_M + 0.02))
        self.assertAlmostEqual(float(tops.max()), 106.0 - 6.0 / int(np.ceil(6.0 / rb.RISE_M)), delta=0.01)

    def test_a_short_mapped_flight_is_carried_on(self):
        import railbridges as rb
        m = bm._Mesh()
        line = np.array([[0.0, 0.0], [3.0, 0.0]])
        got = rb._stair(m, None, FLAT, line[0], line, 106.0, 2.0)
        self.assertGreater(float(np.hypot(*(got[-1] - got[0]))), 6.0 / rb.RISE_M * rb.RUN_M - 0.5)

    def test_low_deck_gets_no_stairs(self):
        import railbridges as rb
        m = bm._Mesh()
        line = np.array([[0.0, 0.0], [12.0, 0.0]])
        self.assertIsNone(rb._stair(m, None, FLAT, line[0], line, 100.2, 2.0))

    def test_big_stations_bridge_is_one_shape(self):
        st = stations_of("pardubice")[0]
        st.country = "CZ"
        m = rm.mesh(None, BIG, [st], FLAT, (0.0, 0.0, 0.0))
        self.assertLess(len(m[0]), 60000)
