"""Tunnels (2026-10-17, the user: "tunnels through terrain need to be added"): the floor profile, the tube mesh and the
terrain block's holes at the portals."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import grading  # noqa: E402
import roadmesh  # noqa: E402
import roads  # noqa: E402

Spec = roads.Spec


def line(x0, x1, step=4.0, y=0.0):
    xs = np.arange(x0, x1 + 1e-9, step)
    return np.c_[xs, np.full(len(xs), y)]


def hill(xy):
    """A mountain over a 1 km tunnel: steep portal faces (1:1) up to 80 m over the line, the portals at 100 m and
    110 m."""
    x = np.asarray(xy, np.float64)[:, 0]
    return 100.0 + 0.01 * x + np.minimum(80.0, np.minimum(x, 1000.0 - x))


class Profile(unittest.TestCase):
    def test_mountain_tunnel_is_straight_between_the_portals(self):
        ln = line(0, 1000)
        z = roads.tunnel_profile([ln], hill)[0]
        self.assertAlmostEqual(z[0], 100.0, places=3)
        self.assertAlmostEqual(z[-1], 110.0, places=3)
        mid = len(z) // 2
        self.assertLess(abs(z[mid] - 105.0), 0.5)               # the straight line, not the ridge
        self.assertTrue((np.diff(z) > -1e-6).all())             # rising steadily

    def test_split_tunnel_is_one_chain(self):
        a, b = line(0, 500), line(500, 1000)
        za, zb = roads.tunnel_profile([a, b], hill)
        self.assertAlmostEqual(za[-1], zb[0], places=6)        # the joint is not a portal at the ridge's height
        self.assertLess(abs(za[-1] - 105.0), 0.5)

    def test_under_a_hollow_it_dips_no_steeper_than_the_grade(self):
        def bay(xy):            # flat land at 5 m, a 1.5 km bay at -10 m in the middle
            x = np.asarray(xy, np.float64)[:, 0]
            return np.where((x > 300) & (x < 1700), -10.0, 5.0)
        ln = line(0, 2000)
        z = roads.tunnel_profile([ln], bay)[0]
        self.assertAlmostEqual(z[0], 5.0, places=3)
        self.assertLess(z[len(z) // 2], -10.0 - roads.TUNNEL_COVER + 1.0)     # under the bed with cover
        s = np.r_[0, np.cumsum(np.hypot(*np.diff(ln, axis=0).T))]
        self.assertTrue((z >= 5.0 - roads.TUNNEL_GRADE * np.minimum(s, s[-1] - s) - 0.5).all())


class Holes(unittest.TestCase):
    def test_a_cut_into_a_cliff_opens(self):
        # GitHub #403 (Malibu Canyon Road): the road at the cliff's foot, the next sample far over the vault: no sample
        # between floor and vault, yet the square from the road up the cliff stood across the opening. The squares
        # crossing the opening are cut, the road in front stays, the rock over the vault stays.
        n, sq = 64, 2.0
        x0 = -n * sq / 2
        xs = x0 + np.arange(n) * sq
        X, Y = np.meshgrid(xs, xs)
        u = np.where(X <= 0, 10.0, 40.0)
        part = ("t", Spec(7.0, "paved", False, "primary", False), line(0, 40), np.full(11, 10.0), 0.0)
        m = roads.tunnel_hole_mask([part], u, 0.0, 0.0, sq)
        ix0 = int(np.flatnonzero(xs == 0.0)[0])
        on = np.abs(xs + sq / 2) <= 3.5          # squares across the carriageway
        self.assertTrue(m[on, ix0].all())         # the square from x 0 (the road) to 2 (the cliff): cut
        self.assertFalse(m[:, :ix0 - 1].any())    # the road in front: kept
        self.assertFalse(m[:, ix0 + 1:].any())    # the rock over the vault: kept

    def test_portal_opening_only(self):
        n, sq = 256, 2.0
        cx = cy = 0.0
        x0 = -n * sq / 2
        xs = x0 + np.arange(n) * sq
        X, Y = np.meshgrid(xs, xs)
        u = np.where(X < 0, 10.0, 10.0 + (X / 2.0))             # flat, then a hillside rising 1 m per 2 m
        part = ("t", Spec(7.0, "paved", False, "primary", False), line(0, 200), np.full(51, 10.0), 0.0)
        m = roads.tunnel_hole_mask([part], u, cx, cy, sq)
        hit = np.argwhere(m)
        self.assertTrue(len(hit) > 0)
        hx = xs[hit[:, 1]] + sq / 2         # (a hole sample cuts the engine square it is the south-west corner of)
        hy = xs[hit[:, 0]] + sq / 2
        self.assertTrue((hx >= -2.0).all() and (hx <= 2 * (roadmesh.TUNNEL_H + 1.0)).all())   # the mouth only
        self.assertTrue((np.abs(hy) <= 7.0 / 2 + roadmesh.TUNNEL_SHOULDER + 1.0).all())        # the tube's width
        self.assertFalse(m[:, xs > 40].any())                    # rock over the vault stays


class Mesh(unittest.TestCase):
    def test_tube_and_portal(self):
        pts = np.c_[line(0, 40), np.full(11, 5.0)]
        ch = roads._chunk({**roads.TUNNEL, "deck": True, "tunnel": True}, 7.0, pts)
        ch.update(fixed=True, deck=True, tunnel=True, portal=[True, False])
        P, T, UV, mats, M = roadmesh.tunnel([ch], (0.0, 0.0, 0.0))
        self.assertEqual(mats, [roadmesh.TUNNEL_SIDE, roadmesh.TUNNEL_LIGHT, roadmesh.TUNNEL_INNER])   # (the inside darker)
        lamp = T[M == 1]
        self.assertGreater(len(lamp), 0)                          # the lamps: on both walls, faced into the tube
        self.assertTrue((P[lamp][:, :, 2] < 5.0 + roadmesh.TUNNEL_WALL_H).all())
        ln = np.cross(P[lamp[:, 1]] - P[lamp[:, 0]], P[lamp[:, 2]] - P[lamp[:, 0]])
        cy = P[lamp].mean(1)[:, 1]
        self.assertTrue((ln[:, 1] * cy < 0).all())               # towards the axis (y = 0)
        self.assertTrue((cy > 0).any() and (cy < 0).any())
        self.assertAlmostEqual(P[:, 2].max(), 5.0 + roadmesh.TUNNEL_H + roadmesh.PORTAL_FRAME, places=3)
        tube = P[P[:, 0] > 1.0]                                   # past the portal face
        self.assertAlmostEqual(tube[:, 2].max(), 5.0 + roadmesh.TUNNEL_H, places=3)
        self.assertAlmostEqual(np.abs(tube[:, 1]).max(), 3.5 + roadmesh.TUNNEL_SHOULDER, places=3)
        # tube faces look inward: towards the axis
        tri = T[(P[T[:, 0], 0] > 1.0) & (P[T[:, 1], 0] > 1.0) & (P[T[:, 2], 0] > 1.0)]
        nrm = np.cross(P[tri[:, 1]] - P[tri[:, 0]], P[tri[:, 2]] - P[tri[:, 0]])
        cen = P[tri].mean(axis=1)
        to_axis = np.c_[np.zeros(len(cen)), -cen[:, 1], 5.0 + roadmesh.TUNNEL_H / 2 - cen[:, 2]]
        self.assertTrue(((nrm * to_axis).sum(1) > -1e-9).all())

    def test_no_tunnel_no_mesh(self):
        ch = roads._chunk(roads.DECK, 7.0, np.c_[line(0, 40), np.full(11, 5.0)])
        self.assertIsNone(roadmesh.tunnel([ch], (0.0, 0.0, 0.0)))


class OpenUnderpass(unittest.TestCase):
    def chunks(self, elev):
        from test_roads import features, FRAME, M_LAT
        f = features([[(-100, 0), (100, 0)]], ["residential"], flags=[(0, "is_tunnel")])
        tp = roads.TunnelProfiles(FRAME, lambda key: f, elev)
        return roads._tunnel_chunks(tp, [f], (-128.0, -128.0, 128.0, 128.0))

    def test_flat_ground_is_a_road_in_the_open(self):
        # a 'tunnel' under a park bridge in a trench: the ground over it never rises TUNNEL_OPEN_COVER over the road
        ch = self.chunks(lambda la, lo: np.full(np.shape(la), 10.0))
        self.assertTrue(ch)
        self.assertFalse(any(c.get("tunnel") for c in ch), "no tube or portals")
        self.assertTrue(any(c["m"] == roads.LOOK["paved"]["m"] for c in ch), "the road on the ground")

    def test_under_a_hill_keeps_its_tube(self):
        from test_roads import LON, M_LON

        def hill(la, lo):
            e = (np.asarray(lo) - LON) * M_LON
            return 10.0 + np.maximum(0.0, 40.0 - np.abs(e) * 0.4)
        ch = self.chunks(hill)
        self.assertTrue(any(c.get("tunnel") for c in ch))


class LowArch(unittest.TestCase):
    def test_a_park_bridge_is_a_low_stone_arch(self):
        # 5 m of ground over a short 'tunnel' (Central Park's West Drive over the 86th Street Transverse): a tube that
        # fits under it (ground - TUNNEL_SOIL_M), of stone, straight between its portals; no lamps
        from test_roads import features, FRAME, LON, M_LON
        f = features([[(-20, 0), (20, 0)]], ["residential"], flags=[(0, "is_tunnel")])

        def arch(la, lo):
            e = (np.asarray(lo) - LON) * M_LON
            return 10.0 + np.where(np.abs(e) < 15.0, 5.0, 0.0)
        tp = roads.TunnelProfiles(FRAME, lambda key: f, arch)
        ch = roads._tunnel_chunks(tp, [f], (-128.0, -128.0, 128.0, 128.0))
        tube = [c for c in ch if c.get("tunnel")]
        self.assertTrue(tube)
        h = tube[0]["tunnel_h"]
        self.assertAlmostEqual(h, 5.0 - roads.TUNNEL_SOIL_M, delta=0.3)
        zs = np.concatenate([np.asarray(c["n"])[:, 2] for c in tube])
        self.assertLess(np.ptp(zs), 0.1, "straight, no dip")
        for c in tube:
            c["portal"] = [True, True]
        P, T, UV, mats, M = roadmesh.tunnel(tube, (0.0, 0.0, 0.0))
        self.assertIn(roadmesh.TUNNEL_STONE, mats)
        self.assertNotIn(1, set(M.tolist()), "no lamps under an arch")
        # its face to just over the ground over the portal (15 m here), its top stone under that
        self.assertLess(P[:, 2].max(), 15.0 + roads.PORTAL_GROUND_M + 0.01)


    def test_a_motorway_s_low_arch_is_concrete(self):
        # the Gotthard road tunnel's portal at Goeschenen (a cut-and-cover box under a meadow) stood as a park's stone arch
        from test_roads import features, FRAME, LON, M_LON
        f = features([[(-20, 0), (20, 0)]], ["motorway"], flags=[(0, "is_tunnel")])

        def arch(la, lo):
            e = (np.asarray(lo) - LON) * M_LON
            return 10.0 + np.where(np.abs(e) < 15.0, 5.0, 0.0)
        tp = roads.TunnelProfiles(FRAME, lambda key: f, arch)
        tube = [c for c in roads._tunnel_chunks(tp, [f], (-128.0, -128.0, 128.0, 128.0)) if c.get("tunnel")]
        self.assertTrue(tube and all(c.get("concrete") for c in tube))
        for c in tube:
            c["portal"] = [True, True]
        _P, _T, _UV, mats, M = roadmesh.tunnel(tube, (0.0, 0.0, 0.0))
        used = {mats[i] for i in set(M.tolist())}
        self.assertFalse(used & {roadmesh.TUNNEL_STONE, roadmesh.TUNNEL_STONE_IN}, used)


class PortalCover(unittest.TestCase):
    def test_a_major_road_s_shallow_open_end_is_covered_a_park_road_s_is_not(self):
        # the Gotthard road tunnel's north portal: a cut-and-cover box under ~3 m of meadow, then the mountain
        kinds = np.array([0, 0, 0, 0, 2, 2, 2, 2, 0])
        cover = np.array([3.0, 3.0, 2.5, 3.5, 50.0, 80.0, 60.0, 9.0, 0.2])
        trunk = Spec(9.0, "paved", False, "trunk")
        got = roads.portal_cover(kinds, cover, trunk)
        self.assertEqual(got[:4].tolist(), [1, 1, 1, 1])
        self.assertEqual(got[-1], 0, "no ground over it: an open approach stays open")
        park = Spec(7.0, "paved", False, "secondary")
        self.assertEqual(roads.portal_cover(kinds, cover, park).tolist(), kinds.tolist())
        self.assertEqual(roads.portal_cover(np.zeros(5, np.int64), np.full(5, 3.0), trunk).tolist(), [0] * 5)


class ArchRaise(unittest.TestCase):
    def test_ground_over_a_low_arch_stays_over_its_vault(self):
        xy = np.array([[0.0, 0.0], [30.0, 0.0]])
        part = ("a", Spec(7.0, "paved", False, "residential"), xy, np.array([10.0, 10.0]), 0.0)
        px = np.array([1.0, 15.0, 15.0, 15.0, 40.0])
        py = np.array([0.0, 0.0, 4.0, 20.0, 0.0])
        z = np.array([10.0, 12.0, 12.0, 12.0, 12.0])
        out = roads.arch_raise([part], {"a": 4.0}, px, py, z)
        self.assertAlmostEqual(out[0], 9.98, msg="the mouth: flat at the floor (cut away)")
        self.assertAlmostEqual(out[1], 10.0 + 4.0 + roads.ARCH_SOIL_MIN)
        self.assertAlmostEqual(out[2], 10.0 + 4.0 + roads.ARCH_SOIL_MIN, msg="beside the vault too")
        self.assertEqual(out[3], 12.0, "away from it as it was")
        self.assertEqual(out[4], 12.0, "past its portal as it was")
        self.assertTrue(np.array_equal(roads.arch_raise([part], {}, px, py, z), z), "a full tunnel: nothing")


class Sections(unittest.TestCase):
    """A long 'tunnel' cut by its cover (Central Park's 79th Street Transverse, 683 m in the map: a sunken road with
    arches and a short rock tunnel, drawn as one concrete tube, 2026-10-17)."""

    def ground(self):
        s = np.arange(0.0, 400.0 + 1e-9, 4.0)
        g = np.full(len(s), 10.0)
        g[(s > 60) & (s < 100)] = 15.5           # a park bridge: a low arch
        g[(s > 200) & (s < 300)] = 25.0          # a hill: a full tunnel
        return s, g

    def test_kinds(self):
        s, g = self.ground()
        floor, k = roads.tunnel_sections(s, g)
        self.assertTrue(np.allclose(floor, 10.0))
        self.assertTrue((k[(s > 64) & (s < 96)] == 1).all())
        self.assertTrue((k[(s > 204) & (s < 296)] == 2).all())
        self.assertTrue((k[(s < 56) | ((s > 104) & (s < 196)) | (s > 304)] == 0).all())

    def test_short_gaps_merge(self):
        s, g = self.ground()
        g[(s > 240) & (s < 246)] = 11.0          # a dip under a full tunnel's cover: one tunnel still
        _f, k = roads.tunnel_sections(s, g)
        self.assertTrue((k[(s > 204) & (s < 296)] == 2).all())

    def test_one_line_three_parts(self):
        from test_roads import features, FRAME, LON, M_LON
        f = features([[(-200, 0), (200, 0)]], ["residential"], flags=[(0, "is_tunnel")])
        s0, g0 = self.ground()

        def elev(la, lo):
            e = (np.asarray(lo) - LON) * M_LON + 200.0
            return np.interp(e, s0, g0)
        tp = roads.TunnelProfiles(FRAME, lambda key: f, elev)
        box = (-256.0, -256.0, 256.0, 256.0)
        ch = roads._tunnel_chunks(tp, [f], box)
        tube = [c for c in ch if c.get("tunnel")]
        hs = sorted({round(c["tunnel_h"], 2) for c in tube})
        self.assertEqual(len(hs), 2, "a low arch and a full tube")
        self.assertAlmostEqual(hs[0], 5.5 - roads.TUNNEL_SOIL_M, delta=0.3)
        self.assertEqual(hs[1], roadmesh.TUNNEL_H)
        self.assertTrue(any(c["m"] == roads.LOOK["paved"]["m"] for c in ch), "open road between them")
        parts = tp.parts([f], box)
        self.assertEqual(len(parts), 2)
        self.assertTrue(all(tp.mouth(p[0]) == (True, True) for p in parts))

    def test_no_hole_where_an_arch_meets_a_tunnel(self):
        # a low arch running straight on into a full tunnel: no mouth inside the hill
        xy = np.array([[0.0, 0.0], [30.0, 0.0]])
        part = ("a", Spec(7.0, "paved", False, "residential"), xy, np.array([10.0, 10.0]), 0.0)
        sq, n = 1.0, 64
        u = np.full((n, n), 15.0)
        u[:, :] = 10.0 + 0.0 * u                 # ground at the floor: a mouth would be cut
        m_both = roads.tunnel_hole_mask([part], u, 15.0, 0.0, sq, {"a": 4.0})
        m_one = roads.tunnel_hole_mask([part], u, 15.0, 0.0, sq, {"a": 4.0}, {"a": (True, False)})
        xs = 15.0 - n * sq / 2 + np.arange(n) * sq
        # (the squares from the flat mouth band, LOW_HOLE_M, up to the soil: about LOW_HOLE_M in from each portal)
        self.assertTrue(m_both[:, xs > 25].any())
        self.assertFalse(m_one[:, xs > 25].any())
        self.assertTrue(m_one[:, xs < 4].any())

    def test_a_low_arch_s_mouth_cuts_no_square_past_its_portal(self):
        # GitHub #450 (Malaga): a portal facing +x had the squares of its last samples cut, reaching a grid step out
        # past the face into the open road in front (a hole cars fell through)
        for sq in (1.0, 2.0):
            xy = np.array([[0.0, 0.0], [30.0, 0.0]])
            part = ("a", Spec(7.0, "paved", False, "residential"), xy, np.array([10.0, 10.0]), 0.0)
            n = 64
            u = np.full((n, n), 10.0)
            m = roads.tunnel_hole_mask([part], u, 15.0, 0.0, sq, {"a": 4.0})
            xs = 15.0 - n * sq / 2 + np.arange(n) * sq
            iy, ix = np.nonzero(m)
            self.assertTrue(len(ix))
            self.assertLessEqual((xs[ix] + sq).max(), 30.0 + 1e-6, sq)      # (a square spans [x, x + sq])
            self.assertGreaterEqual(xs[ix].min(), 0.0 - 1e-6, sq)


class TwinTunnels(unittest.TestCase):
    """GitHub #84: two carriageways 9.5 m apart, each an 11 m road in its own tube, the tubes through each other."""

    def parts(self, gap=9.5, length=220.0, dz=0.0):
        a = ("a", Spec(11.0, "paved", False, "trunk"), np.array([[0.0, 0.0], [length, 0.0]]), np.array([10.0, 11.0]), 0.0)
        b = ("b", Spec(11.0, "paved", False, "trunk"), np.array([[length, gap], [0.0, gap]]),
             np.array([11.0 + dz, 10.0 + dz]), 0.0)
        return [a, b]

    def test_found_with_a_midline_and_a_width_round_both(self):
        tw = roads.twin_tunnels(self.parts())
        self.assertEqual(len(tw), 1)
        i, j, mid, width = tw[0]
        self.assertEqual((i, j), (0, 1))
        self.assertTrue(np.allclose(mid[:, 1], 4.75))
        self.assertAlmostEqual(width, 9.5 + 11.0)
        self.assertAlmostEqual(float(mid[0, 2]), 10.0, places=6)
        self.assertEqual(roads.twin_tunnels(self.parts(gap=40.0)), [], "apart: two tubes")
        self.assertEqual(roads.twin_tunnels(self.parts(dz=5.0)), [], "one over the other: two tubes")

    def test_chunks_one_tube_round_both(self):
        class T:
            def __init__(self, parts):
                self._p = parts

            def parts(self, cells, exact):
                return self._p
        ch = roads._tunnel_chunks(T(self.parts()), [], (-50.0, -50.0, 300.0, 300.0))
        tubes = [c for c in ch if c.get("tunnel") and not c.get("no_tube")]
        self.assertTrue(tubes and all(c.get("tube_only") and not c.get("deck") for c in tubes))
        floors = [c for c in ch if c.get("no_tube")]
        self.assertEqual(len({round(c["n"][0][1], 1) for c in floors}), 2, "both carriageways' floors")
        self.assertTrue(all(c.get("deck") for c in floors))
        mesh = roadmesh.tunnel(ch, (0.0, 0.0, 0.0))
        self.assertIsNotNone(mesh)
        P = mesh[0]
        self.assertLess(float(P[:, 1].min()), -5.5)     # the walls outside both carriageways
        self.assertGreater(float(P[:, 1].max()), 9.5 + 5.5)

    def test_holes_round_both_and_their_mouths(self):
        parts = self.parts()
        n, sq = 128, 2.0
        u = np.full((n, n), 30.0)                       # a hill over them: the tubes' insides are not cut ...
        xs = 110.0 - n * sq / 2 + np.arange(n) * sq
        m = roads.tunnel_hole_mask(parts, u, 110.0, 0.0, sq)
        ys = 0.0 - n * sq / 2 + np.arange(n) * sq
        mid_row = int(np.argmin(np.abs(ys - 4.75)))
        self.assertTrue(m[mid_row, (xs > 0) & (xs < 4)].any(), "... but the shared mouth is, between them too")
        self.assertFalse(m[mid_row, (xs > 20) & (xs < 200)].any())


class FloorAt(unittest.TestCase):
    def test_in_the_tube_and_beside_it(self):
        # a teleport into a park arch: the car on its floor, not the soil over it
        part = ("a", Spec(7.0, "paved", False, "residential"), np.array([[-20.0, 0.0], [20.0, 0.0]]),
                np.array([10.0, 12.0]), 0.0)
        self.assertAlmostEqual(roads.tunnel_floor_at([part], 0.0, 1.0), 11.0)
        self.assertIsNone(roads.tunnel_floor_at([part], 0.0, 3.5 + roadmesh.TUNNEL_SHOULDER + 0.5))
        self.assertIsNone(roads.tunnel_floor_at([part], 25.0, 0.0), "past its portal")


class SegmentalArch(unittest.TestCase):
    def test_low_vault_is_a_shallow_arc(self):
        sec = roadmesh._tunnel_section(6.0, 5.0, low=True)
        top = sec[:, 1].max()
        self.assertAlmostEqual(top, 5.0)
        vault = sec[2:-2]
        self.assertAlmostEqual(vault[0, 1], 5.0 * (1 - roadmesh.LOW_RISE))
        self.assertAlmostEqual(abs(vault[0, 0]), 6.0)
        # a circular arc: one radius from one centre on the axis
        r = (36 + (5.0 * roadmesh.LOW_RISE) ** 2) / (2 * 5.0 * roadmesh.LOW_RISE)
        d = np.hypot(vault[:, 0], vault[:, 1] - (5.0 - r))
        self.assertTrue(np.allclose(d, r))

class PortalClearTest(unittest.TestCase):
    """GitHub #2: an underpass under the E6 in Norway mapped to end 0.7 m off the carriageway: its floor rose to the
    road's level at that end and its portal stood across the lanes."""

    def ends(self, lines, classes, flags):
        from test_roads import features, LAT, LON, M_LAT, M_LON
        f = features(lines, classes, flags=flags)
        out = []
        for ring in roads.tunnel_parts(f, 0):
            out.append([((p[0] - LON) * M_LON, (p[1] - LAT) * M_LAT) for p in (ring[0], ring[-1])])
        return out

    def test_end_under_a_road_carried_past_it(self):
        (a, b), = self.ends([[(0, -40), (0, 7)], [(-200, 0), (200, 0)]], ["service", "trunk"], [(0, "is_tunnel")])
        hw = roads.carriageway("trunk", "paved", False) / 2
        self.assertAlmostEqual(a[1], -40.0, delta=0.1, msg="the far end stays")
        self.assertGreaterEqual(b[1], hw + grading.T_MAX - 0.1, "past the road's graded ground")
        self.assertLessEqual(b[1], 7.0 + roads.PORTAL_EXTEND_MAX_M + 0.1)
        self.assertAlmostEqual(b[0], 0.0, delta=0.1, msg="straight on")

    def test_road_beside_it_not_over_it(self):
        (a, b), = self.ends([[(0, -60), (0, -10)], [(8, -100), (8, 100)]], ["service", "trunk"], [(0, "is_tunnel")])
        self.assertAlmostEqual(a[1], -60.0, delta=0.1)
        self.assertAlmostEqual(b[1], -10.0, delta=0.1)


class ShallowFullTest(unittest.TestCase):
    def kinds(self, length, cover):
        s = np.arange(0.0, length + 40.0 + 1e-9, 2.0)
        g = np.full(len(s), 10.0)
        g[(s > 20) & (s < 20 + length)] = 10.0 + cover
        return roads.tunnel_sections(s, g)[1][(s > 24) & (s < 16 + length)]

    def test_short_embankment_low_arch(self):
        self.assertTrue((self.kinds(30.0, 9.0) == 1).all(), "under an embankment: a low arch")
        self.assertTrue((self.kinds(80.0, 9.0) == 2).all(), "long: a full tunnel")
        self.assertTrue((self.kinds(30.0, 20.0) == 2).all(), "deep: a full tunnel")


class Underwater(unittest.TestCase):
    """GitHub #128 (the Burnley Tunnel, Melbourne): an underwater tunnel split into a dry piece and a wet one is
    left out whole; the dry piece alone was built as a trench ending at the river."""

    def setup(self, wet_from_m):
        from test_roads import features, FRAME, LON, M_LON
        f = features([[(-200, 0), (0, 0)], [(0, 0), (200, 0)]], ["primary", "primary"],
                     flags=[(0, "is_tunnel"), (1, "is_tunnel")])
        tp = roads.TunnelProfiles(FRAME, lambda key: f, lambda la, lo: np.full(np.shape(la), 30.0))
        tp.wet = lambda la, lo: np.where((np.asarray(lo) - LON) * M_LON > wet_from_m, 8.0, 0.0)
        return tp, f

    def fresh(self, tp, f):
        t = roads.TunnelProfiles(tp.frame, lambda key: f, tp.elev)
        t.wet = tp.wet
        return t

    def test_the_dry_piece_of_an_underwater_tunnel_goes_too(self):
        tp, f = self.setup(20.0)                # the second piece is under water from 20 m on
        box = (-256.0, -64.0, 0.0, 64.0)
        self.assertEqual(tp.parts([f], box), [], "no piece of it on land")
        self.assertEqual(tp.open_parts([f], box), [], "no open trench either")
        self.assertTrue(self.fresh(tp, f).drowned((str(f.ids[0]), 0), roads.tunnel_parts(f, 0)[0]),
                        "the grader's question gets the same answer")

    def test_a_dry_tunnel_stays(self):
        tp, f = self.setup(1000.0)              # no water near it
        box = (-256.0, -64.0, 256.0, 64.0)
        self.assertTrue(tp.parts([f], box) or tp.open_parts([f], box))
        self.assertFalse(self.fresh(tp, f).drowned((str(f.ids[0]), 0), roads.tunnel_parts(f, 0)[0]))

    def one_piece(self, wet_fn):
        from test_roads import features, FRAME
        f = features([[(-300, 0), (300, 0)]], ["motorway"], flags=[(0, "is_tunnel")])
        tp = roads.TunnelProfiles(FRAME, lambda key: f, lambda la, lo: np.full(np.shape(la), 30.0))
        tp.wet = wet_fn
        return tp, f

    def test_a_long_tunnel_under_a_river_goes(self):
        # the Burnley Tunnel: 70 m under the Yarra in 3.2 km (2 %): under the share, but under water in its middle
        from test_roads import LON, M_LON
        tp, f = self.one_piece(lambda la, lo: np.where(np.abs((np.asarray(lo) - LON) * M_LON) < 20, 6.0, 0.0))
        box = (-256.0, -64.0, 256.0, 64.0)
        self.assertEqual(tp.parts([f], box) + tp.open_parts([f], box), [])

    def test_a_mountain_lake_far_over_a_deep_tunnel_is_no_river(self):
        # the Gotthard road tunnel: one pond ~900 m over it left all 17 km out (2026-10-28)
        from test_roads import features, FRAME, LON, M_LON
        f = features([[(-300, 0), (300, 0)]], ["motorway"], flags=[(0, "is_tunnel")])
        mountain = lambda la, lo: 30.0 + np.maximum(0.0, 600.0 - 2.0 * np.abs((np.asarray(lo) - LON) * M_LON))
        tp = roads.TunnelProfiles(FRAME, lambda key: f, mountain)
        tp.wet = lambda la, lo: np.where(np.abs((np.asarray(lo) - LON) * M_LON) < 20, 3.0, 0.0)
        box = (-256.0, -64.0, 256.0, 64.0)
        self.assertTrue(tp.parts([f], box) + tp.open_parts([f], box))

    def test_a_portal_on_a_bank_stays(self):
        # Fort Pitt: one end a little "in" the river, the tunnel itself under the hill
        from test_roads import LON, M_LON
        tp, f = self.one_piece(lambda la, lo: np.where((np.asarray(lo) - LON) * M_LON > 290, 6.0, 0.0))
        box = (-256.0, -64.0, 256.0, 64.0)
        self.assertTrue(tp.parts([f], box) + tp.open_parts([f], box))

    def test_wet_run_inside(self):
        xy = np.c_[np.arange(0, 100, 2.0), np.zeros(50)]
        w = np.zeros(50, bool)
        w[20:30] = True
        self.assertAlmostEqual(roads.wet_run_inside(xy, w), 22.0)       # between the dry points either side
        w = np.zeros(50, bool)
        w[40:] = True
        self.assertEqual(roads.wet_run_inside(xy, w), 0.0)              # at an end: a portal on a bank


class SplitRanges(unittest.TestCase):
    """roads.tag_level_ranges joins a segment's tunnel ranges only where one source way runs on
    through the gap (2026-11-08, Potaarde under the R0 at Strombeek-Bever: three underpasses of a track, 24 m and 8 m
    of open track between them, merged into one 157 m tunnel and graded as an open cut through the R0's lanes)."""

    def feats(self, src_ways, tunnels=((0.0, 0.15), (0.25, 0.5), (0.55, 0.8))):
        from test_roads import features, rules
        # 200 m east-west; by default tunnel ranges 0-0.15, 0.25-0.5 and 0.55-0.8 (gaps of 20 m and 10 m)
        f = features([[(-100, 0), (100, 0)]], ["track"], flags=[(0, "is_tunnel", a, b) for a, b in tunnels])
        if src_ways is not None:
            f.rules["src_ways"] = rules([(0, v, a, b) for v, a, b in src_ways])
        return f

    def ranges(self, f):
        return [(round(a, 3), round(b, 3), k) for a, b, k in roads.tag_level_ranges(f, 0)]

    def test_gaps_that_are_ways_of_their_own_stay_open(self):
        f = self.feats([("w1@9", 0.0, 0.15), ("w2@8", 0.15, 0.25), ("w3@9", 0.25, 0.5), ("w4@8", 0.5, 0.55),
                        ("w5@10", 0.55, 0.8), ("w6@10", 0.8, 1.0)])
        self.assertEqual(self.ranges(f), [(0.0, 0.15, "tunnel"), (0.25, 0.5, "tunnel"), (0.55, 0.8, "tunnel")])

    def test_a_split_inside_one_way_is_one_tunnel(self):
        # (the Fort McHenry Tunnel: one OSM way per bore, its Overture range split inside it)
        f = self.feats([("w7@3", 0.0, 0.05), ("w8@12", 0.05, 0.9), ("w9@3", 0.9, 1.0)])
        self.assertEqual(self.ranges(f), [(0.0, 0.8, "tunnel")])

    def test_only_the_gap_inside_a_way_is_joined(self):
        f = self.feats([("w1@1", 0.0, 0.15), ("w2@1", 0.15, 0.25), ("w3@1", 0.25, 0.9), ("w4@1", 0.9, 1.0)])
        self.assertEqual(self.ranges(f), [(0.0, 0.15, "tunnel"), (0.25, 0.8, "tunnel")])

    def test_a_segment_of_one_way_joins_all_its_gaps(self):
        self.assertEqual(self.ranges(self.feats(None)), [(0.0, 0.8, "tunnel")])

    def test_a_long_gap_inside_one_way_is_joined_too(self):
        f = self.feats([("w7@3", 0.0, 0.05), ("w8@12", 0.05, 0.6), ("w9@3", 0.6, 1.0)], tunnels=((0.0, 0.1), (0.3, 0.5)))
        self.assertEqual(self.ranges(f), [(0.0, 0.5, "tunnel")])         # (40 m: the way's tag covers it all)
