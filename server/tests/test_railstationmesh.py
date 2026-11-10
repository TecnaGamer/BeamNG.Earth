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


def no_links(st):
    """The station without its footbridges / underpasses (the platforms alone)."""
    import copy
    st = copy.copy(st)
    st.links = []
    return st


def tops(m, mat_name):
    V, T, UV, mats, idx = m
    k = mats.index(mat_name)
    return V[np.unique(T[np.asarray(idx) == k])]


class PlatformMeshTest(unittest.TestCase):
    def test_top_stands_the_norm_over_the_rail_head(self):
        for name, country in (("vinnytsia", "UA"), ("hk_hln", "CZ"), ("passau", "DE")):
            st = no_links(stations_of(name)[0])
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
        keep = rm._ramps
        rm._ramps = lambda *a, **k: None                                   # (the slab alone: the ramps run down on purpose)
        try:
            m = rm.mesh(None, BIG, [st], slope, (0.0, 0.0, 0.0))
        finally:
            rm._ramps = keep
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
        st = no_links(stations_of("vinnytsia")[0])
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
        st = no_links(stations_of("hk_hln")[0])
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
        for x in range(-1000, 1000, 250):             # (a road tile's share: one shape's 16-bit indices)
            for y in range(-1000, 1000, 250):
                q = rm.mesh(None, (x, y, x + 250, y + 250), [st], FLAT, (0.0, 0.0, 0.0))
                self.assertLess(0 if q is None else len(q[0]), 60000)


def bs_dist(xy, p):
    import busstations as bs
    return bs.dist_to_path(xy, p)


if __name__ == "__main__":
    unittest.main()


class RoofsAndFurnitureTest(unittest.TestCase):
    def test_roofs_are_open_on_columns_standing_on_the_platform(self):
        import busstations as bs
        st = no_links(stations_of("hk_hln")[0])
        st.country = "CZ"
        self.assertTrue(st.roofs)
        self.assertEqual(st.claimed, {r[0] for r in st.roofs})
        m = rm.mesh(None, BIG, [st], FLAT, (0.0, 0.0, 0.0))
        V = np.vstack([tops(m, rs.canopy_material(k)) for k in rs.CANOPY_PALETTE if rs.canopy_material(k) in m[3]])
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
        for x in range(-1000, 1000, 250):             # (a road tile's share: one shape's 16-bit indices)
            for y in range(-1000, 1000, 250):
                q = rm.mesh(None, (x, y, x + 250, y + 250), [st], FLAT, (0.0, 0.0, 0.0))
                self.assertLess(0 if q is None else len(q[0]), 60000)


class UnderpassTest(unittest.TestCase):
    def under(self, name, country="UA"):
        st = stations_of(name)[0]
        st.country = country
        links = [l for l in st.links if l.kind == "under"]
        self.assertTrue(links, name)
        return st, links

    def test_levels_floor_under_the_lowest_bed(self):
        import railunderpass as ru
        for name, c in (("vinnytsia", "UA"), ("hk_hln", "CZ"), ("pardubice", "CZ"), ("kyiv_pas", "UA")):
            st, links = self.under(name, c)
            zf, lid = ru.levels(None, FLAT, st, links[0])
            self.assertAlmostEqual(lid, 100.0 - ru.SINK_M, delta=1e-6)
            self.assertAlmostEqual(lid - zf, ru.CEIL_D + ru.CLEAR_M, delta=1e-6)

    def test_passage_floor_walls_ceiling_lights_and_pits(self):
        import railunderpass as ru
        st, links = self.under("vinnytsia")
        m = bm._Mesh()
        ru.add(m, None, FLAT, st, links[0])
        got = m.result(BIG, (0.0, 0.0, 0.0))
        V = got[0]
        zf, lid = ru.levels(None, FLAT, st, links[0])
        self.assertAlmostEqual(V[:, 2].min(), zf - 0.3, delta=0.35)                  # (the pits' stairs are solid to the floor)
        self.assertGreater(V[:, 2].max(), 100.0 + 0.9)                               # (the handrail over the ground)
        self.assertIn(ru.LIGHT[0], got[3])
        zs = np.unique(np.round(V[:, 2], 2))
        self.assertIn(round(zf, 2), zs)
        self.assertIn(round(lid, 2), zs)

    def test_stairs_rise_from_the_floor_to_the_ground(self):
        import railbridges as rb
        import railunderpass as ru
        st, links = self.under("vinnytsia")
        zf, _lid = ru.levels(None, FLAT, st, links[0])
        m = bm._Mesh()
        fl = next(iter(ru.flights(links[0], rb.resample(links[0].xy)).values()))
        ru._pit(m, None, FLAT, fl, zf, ru.width_of(links[0]), False)
        V = m.result(BIG, (0.0, 0.0, 0.0))[0]
        tops = np.unique(np.round(V[:, 2], 3))
        steps = tops[(tops > zf + 0.05) & (tops < 100.0 - 0.01)]
        self.assertTrue(len(steps) > 5)
        self.assertTrue(np.all(np.diff(steps) < rb.RISE_M + 0.03))
        self.assertTrue(np.any(np.abs(V[:, 2] - (100.0 + ru.RIM_LIFT_M)) < 1e-3))     # (the paved rim at the ground)

    def test_faces_look_the_right_way(self):
        """The rim faces up, the pit's walls and the passage's walls face in, the floor up and the ceiling down."""
        import railbridges as rb
        import railunderpass as ru
        st, links = self.under("vinnytsia")
        ln = links[0]
        zf, lid = ru.levels(None, FLAT, st, ln)
        m = bm._Mesh()
        ru.add(m, None, FLAT, st, ln, collide=True)
        V, T, UV, mats, idx = m.result(BIG, (0.0, 0.0, 0.0))
        tri = V[T]
        nrm = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        nrm /= np.maximum(np.linalg.norm(nrm, axis=1), 1e-12)[:, None]
        zc = tri[:, :, 2].mean(1)
        flat = np.abs(nrm[:, 2]) > 0.99
        floor = flat & (np.abs(zc - zf) < 1e-3)
        ceiling = flat & (np.abs(zc - (lid - ru.CEIL_D)) < 1e-3)
        rim = flat & (np.abs(zc - (100.0 + ru.RIM_LIFT_M)) < 1e-3)
        self.assertTrue(floor.any() and ceiling.any() and rim.any())
        self.assertTrue((nrm[floor, 2] > 0).all())
        self.assertTrue((nrm[ceiling, 2] < 0).all())
        self.assertTrue((nrm[rim, 2] > 0).all())
        # the passage's walls (tall vertical faces between the floor and the ceiling) face the path's axis
        path = rb.resample(ln.xy)
        wall = (~flat) & (np.abs(tri[:, :, 2].min(1) - zf) < 0.01) & (np.abs(tri[:, :, 2].max(1) - (lid - ru.CEIL_D)) < 0.01)             & (np.abs(nrm[:, 2]) < 0.01)                   # (exactly floor to ceiling: the passage's, not a stair's side)
        self.assertTrue(wall.any())
        import busstations as bs
        axes = [path] + [rb.resample(fl, 1.0) for fl in ru.flights(ln, path).values()]
        for i in np.flatnonzero(wall)[::7]:
            c = tri[i].mean(0)[:2]
            d0 = min(bs.dist_to_path(a, c) for a in axes)
            d1 = min(bs.dist_to_path(a, c + nrm[i, :2] * 0.3) for a in axes)
            self.assertLess(d1, d0 + 1e-6)                # (a step along the normal gets nearer to an axis: the faces look in)

    def test_holes_cut_the_footprint_only(self):
        import railunderpass as ru
        st, links = self.under("vinnytsia")
        parts = ru.hole_parts(None, FLAT, st, links[0])
        self.assertGreaterEqual(len(parts), 2)
        n, sq, cx, cy = 600, 1.0, 0.0, 0.0
        u = np.full((n, n), 100.0)                       # (flat ground 3.2 m over the floor: between the floor and the vault)
        mask = ru.hole_mask(parts, u, cx, cy, sq)
        self.assertTrue(mask.any())
        x0 = cx - n * sq / 2
        iy, ix = np.nonzero(mask)
        pts = np.c_[x0 + ix * sq, x0 + iy * sq]
        import busstations as bs
        for p in pts[::25]:
            d = min(bs.dist_to_path(xy, p) for xy, _z, _hw in parts)
            self.assertLess(d, parts[0][2] + 3.0)
        # the passage's middle is a hole
        path = parts[0][0]
        mid = path[len(path) // 2]
        k = np.argmin(np.hypot(pts[:, 0] - mid[0], pts[:, 1] - mid[1]))
        self.assertLess(float(np.hypot(*(pts[k] - mid))), 1.5)

    def test_no_hole_where_the_ground_is_far_under_the_floor(self):
        import railunderpass as ru
        st, links = self.under("vinnytsia")
        parts = ru.hole_parts(None, FLAT, st, links[0])
        u = np.full((600, 600), 80.0)                    # (a valley under the passage: nothing to cut)
        self.assertFalse(ru.hole_mask(parts, u, 0.0, 0.0, 1.0).any())

    def test_no_underpass_no_holes(self):
        import railunderpass as ru
        self.assertIsNone(ru.holes(None, None, {}, 0, 0, 1.0, FLAT))
        self.assertEqual(ru.hole_parts(None, FLAT, stations_of("kalynivka")[0], rs.Link("under", np.zeros((1, 2)), [], -1.0, [])), [])

    def test_collider_has_no_lights_and_no_rails(self):
        import railunderpass as ru
        st, links = self.under("vinnytsia")
        m = bm._Mesh()
        ru.add(m, None, FLAT, st, links[0], collide=True)
        got = m.result(BIG, (0.0, 0.0, 0.0))
        self.assertNotIn(ru.LIGHT[0], got[3])
        self.assertNotIn(rb_steel(), got[3])


def rb_steel():
    import railbridges as rb
    return rb.STEEL[0]


class RampTest(unittest.TestCase):
    def test_ramps_run_down_to_the_ground_at_one_in_eight(self):
        st = no_links(stations_of("vinnytsia")[0])
        st.country = "UA"
        pl = max(st.platforms, key=lambda p: rm._runs(p.snapped, p.ring, p.tidx) and len(max(rm._runs(p.snapped, p.ring, p.tidx), key=len)))
        h = rm.platform_height(pl, "UA", len(st.platforms))
        ref = rm.reference(None, FLAT, pl)
        m = bm._Mesh()
        rm._ramps(m, None, FLAT, ref, pl, h)
        got = m.result(BIG, (0.0, 0.0, 0.0))
        self.assertIsNotNone(got)
        V = got[0]
        top = 100.0 + rm.RAIL_TOP_M + h
        self.assertAlmostEqual(V[:, 2].max(), top, delta=0.05)           # (starts at the platform's top)
        self.assertLessEqual(V[:, 2].min(), 100.0 + 0.03 - 0.3 + 0.01)    # (the slab's underside reaches under the ground)
        # the top of the ramp's surface drops by RAMP_SLOPE a metre
        zs = np.unique(np.round(V[:, 2], 3))
        steps = np.diff(zs[(zs > 100.2) & (zs <= top)])
        self.assertTrue((steps[steps > 0.02] <= rm.RAMP_SLOPE + 0.02).all())

    def test_low_platform_has_no_ramp(self):
        st = no_links(stations_of("hk_hln")[0])
        st.country = "CZ"
        pl = st.platforms[0]
        m = bm._Mesh()
        low = bm.DrawnGround(lambda xy: np.full(len(xy), 100.0))
        rm._ramps(m, None, low, rm.reference(None, low, pl), pl, 0.1)         # (0.58 + 0.1 over the ground: still a ramp)
        self.assertIsNotNone(m.result(BIG, (0.0, 0.0, 0.0)))
        m2 = bm._Mesh()
        rm._ramps(m2, None, low, rm.reference(None, low, pl), pl, -0.5)       # (below the ground: none)
        self.assertIsNone(m2.result(BIG, (0.0, 0.0, 0.0)))


class FlatAndYellowTest(unittest.TestCase):
    def test_a_platform_is_flat_across_and_ignores_the_ground_ripple(self):
        st = no_links(stations_of("kalynivka")[0])
        st.country = "UA"
        def noise(xy):                                  # (deterministic white noise of +-0.35 m: the DEM's own)
            v = np.sin(np.round(xy[:, 0]) * 12.9898 + np.round(xy[:, 1]) * 78.233) * 43758.5453
            return 100.0 + 0.35 * ((v - np.floor(v)) * 2.0 - 1.0)
        ripple = bm.DrawnGround(noise)
        keep = rm._ramps
        rm._ramps = lambda *a, **k: None
        try:
            m = rm.mesh(None, BIG, [st], ripple, (0.0, 0.0, 0.0))
        finally:
            rm._ramps = keep
        V = tops(m, rm.CONCRETE[0])
        top = V[V[:, 2] > 100.7]                        # (the slab's top: 0.58 + 0.2 over the ground, not its walls' feet)
        self.assertGreater(len(top), 30)
        self.assertLess(float(np.ptp(top[:, 2])), 0.12)  # (0.7 m of noise under it: the profile along the track leaves a hair)

    def test_the_edge_band_is_yellow_beside_the_kerb(self):
        import busstations as bs
        st = no_links(stations_of("vinnytsia")[0])
        st.country = "UA"
        m = rm.mesh(None, BIG, [st], FLAT, (0.0, 0.0, 0.0))
        self.assertIn(rm.BAND[0], m[3])
        V = tops(m, rm.BAND[0])
        edge = rs.norm("UA")[0]
        for p in V[::9]:
            d = min(bs.dist_to_path(xy, p[:2]) for _t, xy in st.tracks)
            self.assertGreater(d, edge + rm.STRIP_IN_M - 0.25)
            self.assertLess(d, edge + rm.STRIP_IN_M + rm.STRIP_W_M + 0.25)


class CanopyTest(unittest.TestCase):
    def test_a_main_station_roofs_its_platforms_near_the_hall(self):
        st = stations_of("vinnytsia")[0]
        syn = [r for r in st.roofs if str(r[0]).startswith("synth:")]
        self.assertGreaterEqual(len(syn), 2)
        edge = rs.norm("UA")[0]
        import busstations as bs
        for rid, ring in syn:
            pl = next(p for p in st.platforms if rid in p.roofs)
            inside = [bs.contains(pl.ring, q) for q in ring[::3]]
            self.assertGreater(sum(inside) / len(inside), 0.85)
            d = min(bs.dist_to_path(xy, q) for _t, xy in st.tracks for q in ring[::5])
            self.assertGreater(d, edge + 0.8)
            self.assertGreater(bs.area(ring), 200.0)

    def test_the_maps_no_shelter_is_kept(self):
        st = stations_of("trutnov")[0]                   # (shelter=no on every platform)
        self.assertFalse([r for r in st.roofs if str(r[0]).startswith("synth:")])

    def test_a_small_halt_gets_none(self):
        st = stations_of("kalynivka")[0]
        self.assertFalse(st.roofs)

    def test_canopies_stand_in_the_mesh_with_columns(self):
        st = no_links(stations_of("vinnytsia")[0])
        st.country = "UA"
        m = rm.mesh(None, BIG, [st], FLAT, (0.0, 0.0, 0.0))
        V = tops(m, rs.canopy_material(rs.CANOPY_DEFAULT))       # (no colour in the map for Vinnytsia: grey)
        h = max(rm.platform_height(p, "UA", len(st.platforms)) for p in st.platforms)
        self.assertGreater(V[:, 2].max(), 100.0 + rm.RAIL_TOP_M + h + rm.ROOF_H_M - 0.1)


class LookTest(unittest.TestCase):
    def test_canopy_colour_from_the_map(self):
        self.assertEqual(rs.canopy_look("#2F5DA8"), "blue")
        self.assertEqual(rs.canopy_look("red"), "red")
        self.assertEqual(rs.canopy_look(None, "glass"), "white")
        self.assertEqual(rs.canopy_look(None, None), rs.CANOPY_DEFAULT)
        self.assertEqual(rs.canopy_look("not a colour", "metal"), "grey")

    def test_top_is_pavers_and_band_is_tactile(self):
        st = no_links(stations_of("vinnytsia")[0])
        st.country = "UA"
        m = rm.mesh(None, BIG, [st], FLAT, (0.0, 0.0, 0.0))
        self.assertIn(rs.PAVERS, m[3])
        self.assertIn(rs.TACTILE, m[3])
        V = tops(m, rs.PAVERS)
        h = max(rm.platform_height(p, "UA", len(st.platforms)) for p in st.platforms)
        self.assertAlmostEqual(V[:, 2].max(), 100.0 + rm.RAIL_TOP_M + h, delta=0.02)

    def test_every_material_is_in_the_mods_file(self):
        import json
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "mod", "beamng_earth", "levels",
                            "beamng_earth", "art", "shapes", "beamngEarth", "railstation.materials.json")
        with open(path, encoding="utf8") as f:
            mats = json.load(f)
        for name in [rs.PAVERS, rs.TACTILE] + [rs.canopy_material(k) for k in rs.CANOPY_PALETTE]:
            self.assertIn(name, mats)
            for key in ("baseColorMap", "normalMap", "roughnessMap"):
                p = mats[name]["Stages"][0][key]
                self.assertTrue(os.path.exists(os.path.join(os.path.dirname(path), os.path.basename(p))), p)


class IslandFlatTest(unittest.TestCase):
    def test_an_island_between_two_beds_is_flat_across_with_no_ridge(self):
        t = np.arange(0.0, 200.0, 4.0)
        ref = [(np.c_[t, np.zeros_like(t)], np.full(len(t), 100.6)), (np.c_[t, np.full(len(t), 9.0)], np.full(len(t), 100.9))]
        across = np.c_[np.full(30, 100.0), np.linspace(1.7, 7.3, 30)]          # (edge to edge of a 5.6 m island)
        z = rm.top_z(ref, across, lambda xy: xy[:, 0] * 0)
        self.assertLess(float(np.ptp(z)), 0.02)                                # (no step where the nearer track changes)
        self.assertAlmostEqual(float(z.mean()), 100.75, delta=0.03)            # (the mean of the two heads' profiles)

    def test_another_platforms_far_track_counts_for_nothing(self):
        t = np.arange(0.0, 200.0, 4.0)
        ref = [(np.c_[t, np.zeros_like(t)], np.full(len(t), 100.6)), (np.c_[t, np.full(len(t), 30.0)], np.full(len(t), 105.0))]
        z = rm.top_z(ref, np.array([[100.0, 2.0]]), lambda xy: xy[:, 0] * 0)
        self.assertAlmostEqual(float(z[0]), 100.6, delta=0.01)

    def test_a_real_island_has_no_ridge(self):
        st = no_links(stations_of("kozyatyn")[0])
        st.country = "UA"
        pl = next(p for p in st.platforms if p.island)
        ref = rm.reference(None, FLAT, pl)
        self.assertGreaterEqual(len(ref), 2)
        d = bm._densify(pl.ring, 2.0)
        z = rm.top_z(ref, d, lambda xy: xy[:, 0] * 0)
        self.assertLess(float(np.ptp(z)), 0.05)                                # (a flat ground: a flat platform)


class BandTest(unittest.TestCase):
    def test_bands_cover_the_platform_with_small_pieces(self):
        import busstations as bs
        for name in ("vinnytsia", "kozyatyn", "kyiv_pas"):
            st = stations_of(name)[0]
            for pl in st.platforms:
                ps = rm.bands(bm._densify(pl.ring, 2.0))
                self.assertAlmostEqual(sum(bs.area(p) for p in ps), bs.area(pl.ring), delta=0.02 * bs.area(pl.ring) + 1.0)
                for p in ps:
                    T = bm._triangulate(p)
                    if len(T):
                        e = np.hypot(*(p[T[:, 0]] - p[T[:, 1]]).T).max()
                        self.assertLess(float(e), 60.0)        # (no sliver across the whole platform)

    def test_top_follows_a_grade_with_no_facets(self):
        st = no_links(stations_of("vinnytsia")[0])
        st.country = "UA"
        slope = bm.DrawnGround(lambda xy: 100.0 + 0.02 * xy[:, 1])
        pl = max(st.platforms, key=lambda p: len(p.ring))
        ref = rm.reference(None, slope, pl)
        m = bm._Mesh()
        rm._slab(m, None, slope, ref, pl.ring, 1.1)
        V, T, UV, mats, idx = m.result(BIG, (0.0, 0.0, 0.0))
        top = np.asarray(idx) == list(mats).index(rs.PAVERS)
        tri = V[T[top]]
        c = tri.mean(1)
        # the centre of every top triangle lies on the track's profile there (a facet would sag between far vertices)
        want = rm.top_z(ref, c[:, :2], lambda xy: xy[:, 0] * 0) + 1.1
        self.assertLess(float(np.abs(c[:, 2] - want).max()), 0.08)    # (a sliver over 200 m of a 2 % grade sagged metres)
