"""railstationsign.py and railcrossings.py (task 037, stages F and G) on the eight real stations."""
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import busstationmesh as bm  # noqa: E402
import busstations as bs  # noqa: E402
import railcrossings as rc  # noqa: E402
import railstationsign as rsg  # noqa: E402
import railstations as rs  # noqa: E402
from test_railstations import stations_of  # noqa: E402

FLAT = bm.DrawnGround(lambda xy: np.full(len(xy), 100.0))
BIG = (-2000.0, -2000.0, 2000.0, 2000.0)


class SignSpotTest(unittest.TestCase):
    def test_name_boards_on_long_platforms_facing_the_track(self):
        st = stations_of("hk_hln")[0]
        spots = rsg.platform_spots(st, "CZ")
        names = [s for s in spots if s[4] == "name"]
        self.assertTrue(names)
        for x, y, fx, fy, kind, text, pl in names:
            self.assertTrue(bs.contains(pl.ring, (x, y)))
            self.assertEqual(text, st.name)
            d = min((bs.dist_to_path(xy, np.array([x, y])), xy) for _t, xy in st.tracks)[0]
            ahead = min(bs.dist_to_path(xy, np.array([x + fx * 3.0, y + fy * 3.0])) for _t, xy in st.tracks)
            self.assertLess(ahead, d)                 # (a step the way the face looks gets nearer to a track)

    def test_number_plates_carry_the_platform_ref(self):
        st = stations_of("pardubice")[0]
        plates = [s for s in rsg.platform_spots(st, "CZ") if s[4] == "plate"]
        self.assertTrue(plates)
        for s in plates:
            self.assertEqual(s[5], s[6].ref.replace(";", "/"))

    def test_short_platforms_have_no_board(self):
        ring = np.array([[0.0, 0.0], [20.0, 0.0], [20.0, 4.0], [0.0, 4.0]])
        track = [("t", np.array([[-10.0, -1.7], [40.0, -1.7]]))]
        ring2, sn, ti = rs.snap_edges(ring, track, 1.7)
        pl = rs.Platform("p", ring2, {})
        pl.snapped, pl.tidx, pl.tracks = sn, ti, track
        st = rs.RailStation()
        st.name, st.platforms = "X", [pl]
        self.assertEqual(rsg.platform_spots(st, "UA"), [])

    def test_facade_on_the_wall_toward_the_platforms(self):
        for name in ("vinnytsia", "kozyatyn", "hk_hln", "passau"):
            st = stations_of(name)[0]
            f = rsg.facade_spot(st)
            if f is None:
                continue
            x, y, ux, uy, ln = f
            hall = max(st.buildings, key=lambda b: bs.area(b[1]))
            self.assertAlmostEqual(bs.dist_to_path(hall[1], np.array([x, y]), True), 0.05, delta=0.2, msg=name)
            self.assertFalse(bs.contains(hall[1], (x + ux * 1.0, y + uy * 1.0)), name)    # (outward)
        self.assertTrue(any(rsg.facade_spot(stations_of(n)[0]) is not None for n in ("vinnytsia", "kozyatyn", "hk_hln", "passau")))

    def test_sign_tile_makes_a_mesh_and_pictures(self):
        import fast
        try:
            fast.require(rsg.KERNEL, "x")
        except Exception:
            self.skipTest("kernel too old")
        import geodesy
        import mapdata  # noqa: F401
        st = stations_of("hk_hln")[0]
        st.country = "CZ"
        frame = geodesy.EnuFrame(50.21454, 15.81012, 0.0)
        import railstations as r
        import roads
        old, old_box = r.stations_near, roads.tile_bbox
        r.stations_near = lambda md, fr, box: [st]
        roads.tile_bbox = lambda tx, ty: BIG
        try:
            with tempfile.TemporaryDirectory() as d:
                mesh, faces = rsg.sign_tile(object(), frame, 0, 0, lambda la, lo: np.full(np.shape(la), 100.0), d, "vfs/x")
                self.assertIsNotNone(mesh)
                self.assertTrue(faces)
                for f in faces:
                    self.assertTrue(os.path.exists(os.path.join(d, f + ".color.png")))
        finally:
            r.stations_near, roads.tile_bbox = old, old_box


class LevelCrossingTest(unittest.TestCase):
    def levels(self, name):
        st = stations_of(name)[0]
        return st, [l for l in st.links if l.kind == "level"]

    def test_slab_at_the_rail_head_with_ramps(self):
        st, links = self.levels("kozyatyn")
        self.assertTrue(links)
        done = 0
        for ln in links:
            m = bm._Mesh()
            rc.add(m, None, FLAT, st, ln)
            got = m.result(BIG, (0.0, 0.0, 0.0))
            if got is None:
                continue
            V = got[0]
            top = V[:, 2].max()
            self.assertLessEqual(top, 100.0 + rc.RAIL_TOP_M - rc.CROSS_PROUD_M + rc.STRIPE_LIFT_M + 1e-6)
            self.assertGreater(top, 100.0 + 0.3)
            self.assertGreaterEqual(V[:, 2].min(), 100.0 - 0.4)
            done += 1
        self.assertGreater(done, 3)

    def test_slab_is_flat_across_the_rails(self):
        st, links = self.levels("kalynivka")
        ln = max(links, key=lambda l: len(l.crosses))
        m = bm._Mesh()
        rc.add(m, None, FLAT, st, ln, collide=True)
        V = m.result(BIG, (0.0, 0.0, 0.0))[0]
        s = np.concatenate(([0.0], np.cumsum(np.hypot(*np.diff(np.asarray(ln.xy), axis=0).T))))
        for _k, arc in ln.crosses:
            p = np.array([np.interp(arc, s, np.asarray(ln.xy)[:, 0]), np.interp(arc, s, np.asarray(ln.xy)[:, 1])])
            near = V[np.hypot(V[:, 0] - p[0], V[:, 1] - p[1]) < rc.WIDTH_M]
            self.assertTrue(len(near))
            self.assertAlmostEqual(near[:, 2].max(), 100.0 + rc.RAIL_TOP_M - rc.CROSS_PROUD_M, delta=0.02)

    def test_collider_has_no_stripes(self):
        st, links = self.levels("kozyatyn")
        m = bm._Mesh()
        rc.add(m, None, FLAT, st, links[0], collide=True)
        got = m.result(BIG, (0.0, 0.0, 0.0))
        if got is not None:
            self.assertNotIn(rc.STRIPE[0], got[3])


if __name__ == "__main__":
    unittest.main()
