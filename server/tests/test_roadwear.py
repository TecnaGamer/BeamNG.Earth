"""Road wear (roadwear.py, earthfast.wear_plan): deterministic pieces along a tile's paved roads, by tier, cut at the
other carriageways, the same pieces in two tiles that share a road, and the road mesh's fade for them."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import roads  # noqa: E402
import roadmesh  # noqa: E402
import roadwear  # noqa: E402
from test_roads import FRAME  # noqa: E402


def spec(cls="primary", width=10.0, surf=None, urban=False, lanes=None, oneway=False):
    return roads.Spec(width, "paved", urban, cls, oneway, 2.5, 0, None, None, None, lanes, surf)


def line(x0, y0, x1, y1, step=10.0):
    n = max(2, int(np.hypot(x1 - x0, y1 - y0) / step) + 1)
    return np.stack([np.linspace(x0, x1, n), np.linspace(y0, y1, n), np.zeros(n)], axis=1)


BOX = (0.0, 0.0, 256.0, 256.0)


def plan(rds, tier=2, box=BOX, **kw):
    return roadwear.tile_chunks(rds, {}, box, tier, **kw)


def quads(chunks, m):
    """The quads (4, 3) of the spot chunks of material m (roadmesh "quads": 4 corners each)."""
    out = []
    for c in chunks:
        if c["m"] == m:
            self_check = len(c["poly"]) % 4 == 0 and len(c["uvq"]) == len(c["poly"]) and c.get("quads")
            assert self_check, c
            out.extend(np.asarray(c["poly"], np.float64).reshape(-1, 4, 3))
    return out


def kinds(chunks):
    out = {}
    for c in chunks:
        out[c["m"]] = out.get(c["m"], 0) + 1
    return out


class RoadWearTest(unittest.TestCase):
    def setUp(self):              # (the strips' engine: drawn only with BNE_WEAR_STRIPS=on for now)
        self._strips = roadwear.STRIPS_ON
        roadwear.STRIPS_ON = True

    def tearDown(self):
        roadwear.STRIPS_ON = self._strips

    def test_tier_off_and_unpaved(self):
        rd = [("a", spec(), line(-500, 128, 800, 128), 0.0)]
        self.assertEqual(plan(rd, tier=0), [])
        dirt = [("a", spec()._replace(kind="dirt"), line(-500, 128, 800, 128), 0.0)]
        self.assertEqual(plan(dirt), [])

    def test_basic_tier_is_polish_and_joints_only(self):
        rd = [("a", spec(lanes=2), line(-500, 128, 800, 128), 0.0),
              ("c", spec(surf="concrete", lanes=4, cls="motorway", width=16.0), line(128, -500, 128, 800), 0.0)]
        got = kinds(plan(rd, tier=1))
        self.assertTrue(set(got) <= {"bne_wear_tread", "bne_crack_line"}, got)
        self.assertIn("bne_wear_tread", got)
        self.assertIn("bne_crack_line", got)

    def test_strips_off_by_default(self):
        """The full-lane strips (tyre polish, crack networks) are left out unless BNE_WEAR_STRIPS=on (bright bands in
        game, 2026-11-11); the crack lines stay."""
        roadwear.STRIPS_ON = False
        rd = [("c", spec(surf="concrete", lanes=4, cls="motorway", width=16.0), line(128, -500, 128, 800), 0.0)]
        got = kinds(plan(rd, tier=2))
        self.assertFalse({"bne_wear_tread", "bne_wear_sealed", "bne_wear_cracks"} & set(got), got)
        self.assertIn("bne_crack_line", got)

    def test_concrete_joints_across_every_few_metres(self):
        rd = [("c", spec(surf="concrete", lanes=2, width=8.0), line(-500, 128, 800, 128), 0.0)]
        across = [c for c in plan(rd, tier=1) if c["m"] == "bne_crack_line" and len(c["n"]) == 2
                  and abs(c["n"][0][0] - c["n"][1][0]) < 1e-6]
        xs = sorted(c["n"][0][0] for c in across)
        gaps = np.diff(xs)
        self.assertGreater(len(xs), 30)
        self.assertTrue(np.all((gaps > 4.4) & (gaps < 6.3)), gaps)
        for c in across:            # the whole carriageway across, less 0.2 m each side
            self.assertAlmostEqual(abs(c["n"][0][1] - c["n"][1][1]), 8.0 - 0.4, places=3)

    def test_same_pieces_in_two_tiles(self):
        """Two tiles sharing a road lay the same pieces where their boxes overlap: same seeds, same distances."""
        rd = [("x", spec(lanes=2, cls="secondary"), line(-1000, 300, 1500, 300), 0.0)]
        a = plan(rd, box=(0, 172, 256, 428))
        b = plan(rd, box=(200, 172, 456, 428))

        def near(chunks, x0, x1):
            out = set()
            for c in chunks:
                for pts in (np.asarray(c["poly"]).reshape(-1, 4, 3) if c.get("quads") else [np.asarray(c["n"])]):
                    if pts[:, 0].min() >= x0 and pts[:, 0].max() <= x1:
                        out.add((c["m"], tuple(np.round(pts[0, :2], 2)), round(float(c.get("slen", -1)), 2)))
            return out
        self.assertTrue(a and b)
        self.assertEqual(near(a, 210, 250), near(b, 210, 250))

    def test_strip_fade_fields(self):
        rd = [("a", spec(lanes=2), line(-500, 128, 800, 128), 0.0)]
        for c in plan(rd, tier=1):
            if c["m"] == "bne_wear_tread":
                self.assertEqual(c["fends"], [1, 1])
                self.assertEqual(c["feather"], 0)
                self.assertGreater(c["slen"], 0)
                self.assertLessEqual(c["s1"], c["slen"] + 1e-6)

    def test_cut_at_crossing_carriageway(self):
        rd = [("a", spec(lanes=2), line(-500, 128, 800, 128), 0.0),
              ("b", spec(width=12.0), line(128, -500, 128, 800), 0.0)]
        for c in plan(rd, tier=1):
            n = np.asarray(c["n"])
            if c["m"] == "bne_wear_tread" and abs(n[0, 1] - 128) < 6:     # road a's: none across b's carriageway
                self.assertFalse(n[:, 0].min() < 128 < n[:, 0].max(), n)
                self.assertFalse(np.any(np.abs(n[:, 0] - 128) < 6.0 + 0.5 - 1e-6), n)
        # excluded (a straight continuation): not cut
        ex = plan(rd, tier=1, exclude=lambda f, o: True)
        self.assertTrue(any(c["m"] == "bne_wear_tread" and abs(c["n"][0][1] - 128) < 6
                            and np.asarray(c["n"])[:, 0].min() < 120 < 136 < np.asarray(c["n"])[:, 0].max() for c in ex))

    def test_town_spots_and_lot_oil(self):
        town = [("t", spec(cls="residential", width=8.0, urban=True), line(-800, 128, 1200, 128), 0.0)]
        got = kinds(plan(town))
        self.assertIn("bne_spot_manhole", got)
        aisle = [("lot", spec(cls="service", width=7.0), line(-50, 128, 300, 128), 0.0)]
        oil = quads(plan(aisle, aisles={"lot"}), "bne_spot_oil")
        self.assertTrue(oil)
        for q in oil:               # in the stalls beside the aisle, not on it
            self.assertAlmostEqual(abs(q[:, 1].mean() - 128), 3.5 + 2.0, places=3)

    def test_poor_roads_get_more_damage(self):
        def damage(cond):
            class F:
                attrs = {"osm_smoothness": np.array([cond])}
            rd = [("p", spec(cls="tertiary", width=7.0, lanes=2), line(-1500, 128, 1800, 128), 0.0)]
            ch = roadwear.tile_chunks(rd, {"p": (F, 0, rd[0][1])}, (-2000, -2000, 2000, 2000), 2)
            return sum(c["m"] in ("bne_wear_cracks", "bne_spot_pothole") for c in ch)
        self.assertGreater(damage("bad"), damage("excellent"))
        self.assertEqual(damage("excellent"), 0)

    def test_mesh_draws_quads(self):
        town = [("t", spec(cls="residential", width=8.0, urban=True), line(-800, 128, 1200, 128), 0.0)]
        ch = [c for c in plan(town) if c.get("quads")]
        n = sum(len(c["poly"]) // 4 for c in ch)
        mesh = roadmesh.build(FRAME, ch, lambda la, lo: np.zeros(np.shape(la)), (0.0, 0.0, 0.0))
        self.assertEqual(len(mesh[1]), 2 * n)       # two triangles a quad, facing up
        P, T = mesh[0], mesh[1]
        nz = np.cross(P[T[:, 1]] - P[T[:, 0]], P[T[:, 2]] - P[T[:, 0]])[:, 2]
        self.assertTrue(np.all(nz > 0))

    def test_mesh_fades_wear_and_draws_it_first(self):
        rd = [("a", spec(lanes=2), line(0, 128, 256, 128), 0.0)]
        ch = [c for c in plan(rd, tier=1) if c["m"] == "bne_wear_tread"]
        ch.append({"m": "line_white", "w": 0.15, "tl": 6.4, "p": 2, "n": [[0, 128, 0], [256, 128, 0]]})
        mesh = roadmesh.build(FRAME, ch, lambda la, lo: np.zeros(np.shape(la)), (0.0, 0.0, 0.0), clip=BOX)
        self.assertEqual(len(mesh), 6)          # (the fade uv)
        self.assertEqual(mesh[3][0], "bne_wear_tread")
        fade = mesh[5]
        self.assertLess(fade[:, 1].min(), 0.05)     # (a run's open start)

    def test_grass_sides(self):
        """A rural lane through fields gets the grassy edge on its field side; a town street, a motorway and scrub
        keep the dirt edge."""
        def cover(lat, lon):
            e, n, _ = FRAME.geodetic_to_enu(np.asarray(lat), np.asarray(lon), 0.0)
            return np.where(n > 128, 40, 20).astype(np.uint8), True      # cropland north of y = 128, scrub south
        rd = [("lane", spec(cls="tertiary", width=6.0), line(0, 128, 256, 128), 0.0),
              ("town", spec(cls="tertiary", width=6.0, urban=True), line(0, 200, 256, 200), 0.0),
              ("mw", spec(cls="motorway", width=12.0, oneway=True), line(0, 230, 256, 230), 0.0)]
        got, leaves = roadwear.verges(FRAME, rd, cover, roads.has_sidewalks)
        self.assertEqual(got, {("lane", 1)})
        self.assertEqual(leaves, {})
        self.assertEqual(roadwear.verges(FRAME, rd, lambda la, lo: 1 / 0, roads.has_sidewalks), (set(), {}))

    def test_fallen_leaves_under_trees_in_the_fall(self):
        def cover(lat, lon):
            e, n, _ = FRAME.geodetic_to_enu(np.asarray(lat), np.asarray(lon), 0.0)
            return np.where(n > 128, 10, 30).astype(np.uint8), True      # woods north of y = 128, grass south
        rd = [("lane", spec(cls="tertiary", width=6.0), line(-400, 128, 600, 128), 0.0)]
        _, leaves = roadwear.verges(FRAME, rd, cover, roads.has_sidewalks, fall=1.0)
        self.assertAlmostEqual(leaves[("lane", 1)], 1.0)
        self.assertNotIn(("lane", -1), leaves)
        got = quads(roadwear.tile_chunks(rd, {}, BOX, 2, leaves=leaves), "bne_spot_leaves")
        self.assertTrue(got)
        self.assertTrue(all(q[:, 1].mean() > 128 for q in got))     # the wooded side only
        self.assertFalse([c for c in roadwear.tile_chunks(rd, {}, BOX, 1, leaves=leaves) if c["m"] == "bne_spot_leaves"])

    def test_fall_by_season_and_hemisphere(self):
        self.assertEqual(roadwear.fall_of({"tier": 2, "season": "off"}, 45.0), 0.0)
        self.assertEqual(roadwear.fall_of({"tier": 2, "season": "fall"}, -30.0), 1.0)
        self.assertEqual(roadwear.fall_of(2, 45.0), 0.0)
        import datetime
        import season
        a = season.amounts_for("auto", 48.0, datetime.date(2026, 10, 7))["fall"]
        b = season.amounts_for("auto", -48.0, datetime.date(2026, 10, 7))["fall"]
        self.assertGreater(a, 0.5)
        self.assertLess(b, 0.1)
        self.assertEqual(roadwear.wear_key({"tier": 1, "season": "fall"}, 45.0), (1, 0.0))

    def test_tier_of(self):
        self.assertEqual(roadwear.tier_of("off"), 0 if roadwear.ENV_TIER is None else roadwear.ENV_TIER)
        if roadwear.ENV_TIER is None:
            self.assertEqual(roadwear.tier_of("basic"), 1)
            self.assertEqual(roadwear.tier_of(None), roadwear.DEFAULT_TIER)
            self.assertEqual(roadwear.tier_of("nonsense"), roadwear.DEFAULT_TIER)
            self.assertEqual(roadwear.tier_of(7), 2)


V2_MATS = {"bne_wear_var_01", "bne_wear_var_02", "bne_wear_tread2", "bne_wear_patchstrip"}


class SurfaceV2Test(unittest.TestCase):
    """The road revamp v2's surface overlays (BNE_SURFACE_V2: roadwear.surface_chunks, earthfast.surface_plan; the
    road mesh's lifts, ground clearance and draw order: roadmesh.SURFACE_V2)."""

    def setUp(self):
        from unittest import mock
        self._p = [mock.patch.object(roadwear, "SURFACE_V2", True), mock.patch.object(roadmesh, "SURFACE_V2", True),
                   mock.patch.dict(roadmesh.LIFT, roadmesh.LIFT_V2)]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def test_off_draws_none(self):
        rd = [("a", spec(lanes=2), line(-500, 128, 800, 128), 0.0)]
        with __import__("unittest").mock.patch.object(roadwear, "SURFACE_V2", False):
            self.assertFalse(V2_MATS & set(kinds(plan(rd))))

    def test_pieces_and_their_numbers(self):
        L = 3000.0
        rd = [("gers-1", spec(lanes=2, width=7.4), line(0, 128, L, 128), 0.0)]
        ch = plan(rd, box=(-10, 0, L + 10, 256))
        got = kinds(ch)
        self.assertTrue({"bne_wear_tread2", "bne_wear_patchstrip"} <= set(got), got)
        self.assertNotIn("bne_wear_tread", got)          # (replaced by the lane-centre wear)
        var = [c for c in ch if c["m"].startswith("bne_wear_var_")]
        share = sum(c["slen"] for c in var) / L
        self.assertTrue(0.6 < share < 0.8, share)         # about 70 % of the length (one track on a 7.4 m road)
        for c in var:
            self.assertEqual((c["layer"], c["feather"], c["fend_m"]), ("var", 0, 10.0))
            self.assertTrue(5.0 <= c["w"] <= 7.4 and 80.0 <= c["tl"] <= 120.0, c)
            self.assertTrue(np.all(np.abs(np.asarray(c["n"])[:, 1] - 128) <= 3.7 - c["w"] / 2 + 1e-6))
        for c in ch:
            if c["m"] == "bne_wear_tread2":
                self.assertTrue(4.0 <= c["w"] <= 5.0 and 5.0 <= c["fend_m"] <= 20.0 and c["layer"] == "wear", c)
        patches = len([c for c in ch if c["m"] == "bne_wear_patchstrip"]) / (L / 1000)
        self.assertTrue(5 <= patches <= 11, patches)      # about 8 a km

    def test_variation_runs_through_junctions(self):
        """The variation strips run on across a crossing road's carriageway (the stock maps'); the wear stops."""
        a = ("a", spec(lanes=2, width=7.4), line(-500, 128, 800, 128), 0.0)
        b = ("b", spec(lanes=2, width=7.4), line(128, -500, 128, 800), 0.0)

        def of_a(chunks, m):
            return [c for c in chunks if c["m"].startswith(m) and abs(np.asarray(c["n"])[0, 1] - 128) < 4
                    and np.ptp(np.asarray(c["n"])[:, 0]) > 1.0]
        alone, crossed = plan([a]), plan([a, b])
        self.assertTrue(of_a(alone, "bne_wear_var_"))
        self.assertEqual(of_a(alone, "bne_wear_var_"), of_a(crossed, "bne_wear_var_"))   # (not cut)
        self.assertNotEqual(of_a(alone, "bne_wear_tread2"), of_a(crossed, "bne_wear_tread2"))
        for c in of_a(crossed, "bne_wear_tread2"):
            x = np.asarray(c["n"])[:, 0]
            self.assertFalse(x.min() < 124 and x.max() > 132, "the wear stops at the crossing carriageway")

    def test_same_pieces_in_two_tiles(self):
        rd = [("x", spec(lanes=2, cls="secondary"), line(-1000, 300, 1500, 300), 0.0)]
        a = [c for c in plan(rd, box=(0, 172, 256, 428)) if c["m"] in V2_MATS]
        b = [c for c in plan(rd, box=(200, 172, 456, 428)) if c["m"] in V2_MATS]
        key = lambda c: (c["m"], c["w"], c["tl"], c["slen"], c.get("fend_m"))     # noqa: E731
        common = {key(c) for c in a if np.asarray(c["n"])[:, 0].max() > 205} & {key(c) for c in b}
        self.assertTrue(common)
        self.assertEqual(plan(rd, box=(0, 172, 256, 428)), plan(rd, box=(0, 172, 256, 428)))

    def test_clear_keeps_a_corner_on_a_flat_cells_edge_beside_a_step(self):
        """follow_ground's clear: a corner on the edge a flat cell shares with a step cell (a wall the lattice draws as
        teeth) takes the flat cell's (no) cap, not the step's lowest corner + FOLLOW_MAX_UP under the flat ground."""
        grid = np.array([[0.0, 0.0, -0.5, -0.5]] * 3)          # (cells x 0-1 flat, 1-2 a 0.5 m step)
        P = np.array([[0.2, 0.2], [1.0, 0.5], [0.2, 0.8]])
        T = np.array([[0, 1, 2]])
        old = roadmesh.follow_ground(P, np.zeros((3, 2)), T, grid, 0, 0, tol=0.02, cell=1.0)[0]
        new = roadmesh.follow_ground(P, np.zeros((3, 2)), T, grid, 0, 0, tol=0.02, cell=1.0, clear=0.003)[0]
        self.assertLess(float(old[:, 2].min()), -0.3)           # (as before, without clear)
        self.assertGreaterEqual(float(new[:, 2].min()), -1e-9)

    def test_clear_keeps_the_cap_of_the_cell_holding_each_piece(self):
        """follow_ground's clear: a marking crossing from a flat plateau into a descending step cell (a wall drawn as
        teeth): its step-cell part stays held at the low side + FOLLOW_MAX_UP (a flat neighbour must not uncap a corner
        on their shared edge and lift a sliver up the wall); its plateau part keeps the plateau's height."""
        grid = np.array([[0.0, 0.0, -1.3, -1.3]] * 3)          # (x 0-1 a plateau, 1-2 a 1.3 m descending step)
        lo_cap = -1.3 + roadmesh.FOLLOW_MAX_UP
        for P in (np.array([[0.3, 0.4], [1.6, 0.4], [1.6, 0.6], [0.3, 0.6]]),       # (crossing the shared edge)
                  np.array([[1.0, 0.4], [1.6, 0.4], [1.6, 0.6], [1.0, 0.6]])):      # (a corner on the shared edge)
            T = np.array([[0, 1, 2], [0, 2, 3]])
            for clear in (None, 0.003):
                V, _uv, Tr, _s = roadmesh.follow_ground(P, np.zeros((4, 2)), T, grid, 0, 0, tol=0.02, cell=1.0,
                                                        clear=clear)
                for t in Tr:
                    c = V[t].mean(axis=0)
                    if c[0] > 1.0 + 1e-6:          # (a piece in the step cell: no corner up the wall)
                        self.assertLessEqual(float(V[t][:, 2].max()), lo_cap + 1e-9, (clear, V[t]))
                    elif clear is not None:        # (on the plateau: at its height; without clear, as before)
                        self.assertGreaterEqual(float(V[t][:, 2].min()), -1e-9, (clear, V[t]))

    def test_clear_step_cut_meets_its_whole_neighbour(self):
        """follow_ground's clear: a triangle within tol of its plane but over a step cell and others is cut for the
        cap only; its pieces keep its plane, so its edge meets a whole neighbour's exactly (on the terrain they opened
        a gap at a kink on the shared edge, Codex review of the fix)."""
        x = np.arange(5.0)
        grid = np.tile(0.4 * x, (5, 1))                        # (a 0.4 m / m slope: no cell holds a step)
        grid[1, 2] += 0.07                                     # (cells (1, 0) and (1, 1) now do: 0.47 > 0.45)
        grid[3, 2] += 0.03                                     # (a kink on the shared edge, in uncapped cells)
        P = np.array([[0.5, 3.0], [3.5, 3.0], [2.0, 0.5], [2.0, 3.9]])
        T = np.array([[0, 1, 2], [1, 0, 3]])
        V, _uv, Tr, src = roadmesh.follow_ground(P, np.zeros((4, 2)), T, grid, 0, 0, tol=0.1, cell=1.0, clear=0.003)
        self.assertGreater(int((src == 0).sum()), 1)           # (the lower one cut)
        self.assertEqual(int((src == 1).sum()), 1)             # (the upper one whole)
        up = V[Tr[src == 1][0]]
        a, b = up[np.argmin(up[:, 0] + 10 * np.abs(up[:, 1] - 3))], up[np.argmax(up[:, 0] - 10 * np.abs(up[:, 1] - 3))]
        on_edge = [v for v in V[np.unique(Tr[src == 0])] if abs(v[1] - 3.0) < 1e-9]
        self.assertGreater(len(on_edge), 2)
        for v in on_edge:
            f = (v[0] - a[0]) / (b[0] - a[0])
            self.assertAlmostEqual(float(v[2]), float(a[2] + f * (b[2] - a[2])), delta=1e-9)

    def test_refine_halves_instead_of_cutting(self):
        """follow_ground's refine (the v2 overlays): a triangle the tolerance would cut along the terrain is halved
        first; far fewer pieces, the same area, the ground still at most clear over it everywhere and its float no
        more than the tolerance past the cut's; the same output twice; refine 0 as without it."""
        n = 12
        gx, gy = np.meshgrid(np.arange(n + 1.0), np.arange(n + 1.0))
        grid = np.round((0.04 * np.sin(gx / 1.7) * np.cos(gy / 2.3) + 0.02 * gx) * 512) / 512   # (rolling, 1/512 m)
        P = np.array([[1.0, 1.0], [11.0, 1.4], [10.6, 6.0], [1.3, 5.2]])
        T = np.array([[0, 1, 2], [0, 2, 3]])
        UV = P / 10.0
        kw = dict(tol=0.008, cell=1.0, clear=0.003)
        cut = roadmesh.follow_ground(P, UV, T, grid, 0, 0, **kw)
        ref = roadmesh.follow_ground(P, UV, T, grid, 0, 0, refine=10, **kw)
        for a, b in zip(cut, roadmesh.follow_ground(P, UV, T, grid, 0, 0, refine=0, **kw)):
            np.testing.assert_array_equal(a, b)
        for a, b in zip(ref, roadmesh.follow_ground(P, UV, T, grid, 0, 0, refine=10, **kw)):
            np.testing.assert_array_equal(a, b)
        self.assertLess(len(ref[2]), len(cut[2]) / 2, (len(ref[2]), len(cut[2])))

        def area(V, Tr):
            t = V[Tr][:, :, :2]
            a, b = t[:, 1] - t[:, 0], t[:, 2] - t[:, 0]
            return float(np.abs(a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0]).sum() / 2)

        self.assertAlmostEqual(area(ref[0], ref[2]), area(cut[0], cut[2]), places=6)

        def ground(xy):
            i, j = np.floor(xy[:, 0]).astype(int), np.floor(xy[:, 1]).astype(int)
            fx, fy = xy[:, 0] - i, xy[:, 1] - j
            a, b, c, d = grid[j, i], grid[j, i + 1], grid[j + 1, i], grid[j + 1, i + 1]
            return np.where(fy > fx, a + fy * (c - a) + fx * (d - c), a + fx * (b - a) + fy * (d - b))

        w = np.random.default_rng(3).dirichlet((1, 1, 1), 400)

        def dz(V, Tr):
            p = np.einsum("kj,tjc->tkc", w, V[Tr]).reshape(-1, 3)
            return p[:, 2] - ground(p[:, :2])
        dc, dr = dz(cut[0], cut[2]), dz(ref[0], ref[2])
        self.assertGreaterEqual(float(dr.min()), -kw["clear"] - 1e-9)
        # (a whole triangle's float: the tolerance below its plane, and a raise of at most tol - clear: as before)
        self.assertLessEqual(float(dr.max()), float(dc.max()) + 2 * kw["tol"] - kw["clear"] + 1e-9)
        self.assertEqual(set(ref[3].tolist()), {0, 1})

    def test_refine_leaves_no_corner_on_a_neighbours_side(self):
        """follow_ground's refine: a side one triangle halved is split in its neighbour too (the input's two
        triangles share a side as copies of its points, as the tile clip leaves them), so every corner lying on
        another triangle's side is that side's own height there after the raises (halved next to whole, the two
        parted vertically by up to the tolerance: Codex review)."""
        n = 14
        gx, gy = np.meshgrid(np.arange(n + 1.0), np.arange(n + 1.0))
        grid = 0.05 * np.sin(gx / 2.1) * np.cos(gy / 1.9) + 0.01 * gy           # (smooth: no cut left)
        P = np.array([[1.0, 1.0], [12.0, 2.0], [6.0, 7.0], [6.0, 7.0], [12.0, 2.0], [13.0, 12.0]])
        T = np.array([[0, 1, 2], [3, 4, 5]])
        V, _uv, Tr, src = roadmesh.follow_ground(P, P / 10.0, T, grid, 0, 0, tol=0.004, cell=1.0, clear=0.002,
                                                 refine=10)
        self.assertGreater(len(Tr), 8)
        bad = 0
        for t in Tr:
            for e in range(3):
                a, b = V[t[e]], V[t[(e + 1) % 3]]
                ab = b[:2] - a[:2]
                L2 = float(ab @ ab)
                f = ((V[:, :2] - a[:2]) @ ab) / L2
                off = np.abs((V[:, 0] - a[0]) * ab[1] - (V[:, 1] - a[1]) * ab[0]) / np.sqrt(L2)
                on = (f > 1e-6) & (f < 1 - 1e-6) & (off < 1e-7)
                if on.any():
                    zl = a[2] + f[on] * (b[2] - a[2])
                    bad += int((np.abs(V[on, 2] - zl) > 1e-9).sum())
        self.assertEqual(bad, 0)

    def test_overlays_clear_the_bumpy_ground(self):
        """Every overlay keeps MARK_MIN_M over the drawn ground (the terrain's own triangles, where the asphalt is
        painted) at its points and inside its triangles; the asphalt mesh's draw order is v2_rank's."""
        from test_roads import LAT, LON, M_LAT, M_LON

        def elev(la, lo):           # (rolling, no cell steep enough for follow_ground's step cap)
            e, n = (np.asarray(lo) - LON) * M_LON, (np.asarray(la) - LAT) * M_LAT
            return 10.0 + 0.15 * np.sin(e / 7.0) * np.cos(n / 5.0) + 0.03 * e
        rd =[("a", spec(lanes=2, width=7.4), line(-50, 20, 120, 60), 0.0)]
        ch = plan(rd, box=(0, 0, 100, 100)) + [
            {"m": "line_white", "w": 0.15, "tl": 6.4, "p": 2, "n": [[0, 21.0, 0], [100, 44.6, 0]]},
            {**roads.EDGE_DECAL, "n": [[0, 25.0, 0], [100, 48.6, 0]]}]
        q = 1 / 512
        P, T, UV, mats, M, _fade = roadmesh.build(FRAME, ch, elev, (0.0, 0.0, 0.0), 0.0, clip=(0, 0, 100, 100),
                                                  follow=True, q=q)
        self.assertEqual([roadmesh.v2_rank(m) for m in mats], sorted(roadmesh.v2_rank(m) for m in mats))
        self.assertEqual(mats[-1], "line_white")
        tri = P[T]
        w = np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1], [1 / 3] * 3, [.6, .2, .2], [.2, .6, .2], [.2, .2, .6]])
        pts = np.einsum("kj,tjc->tkc", w, tri).reshape(-1, 3)
        dz = pts[:, 2] - roadmesh.drawn_ground(FRAME, elev, pts[:, :2], q)
        ok = np.isfinite(dz)
        self.assertGreater(ok.sum(), 1000)
        self.assertGreaterEqual(float(dz[ok].min()), 0.002 - 1e-6)


if __name__ == "__main__":
    unittest.main()
