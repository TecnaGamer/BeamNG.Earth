"""Ground tram track (tramtrack.py through rails.tile_chunks): in the street, the gauge, no white sleepers."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import rails  # noqa: E402
import railways  # noqa: E402
import roads  # noqa: E402
import tramtrack  # noqa: E402
import railxing  # noqa: E402
from test_roads import FRAME, features  # noqa: E402


class PureTest(unittest.TestCase):
    def test_gauge_from_osm_inside_bounds_else_default(self):
        self.assertEqual(tramtrack.gauge(1.0), 1.0)
        self.assertEqual(tramtrack.gauge(1.524), 1.524)
        for bad in (None, 0.2, 3.0):
            self.assertEqual(tramtrack.gauge(bad), tramtrack.GAUGE_DEFAULT_M)

    def test_paving_width_follows_the_gauge(self):
        self.assertAlmostEqual(tramtrack.paving_width(1.0), 1.0 + tramtrack.PAVING_EXTRA_M, places=3)
        self.assertLess(tramtrack.paving_width(1.0), tramtrack.paving_width(1.435))

    def test_paving_is_slabs_in_the_ex_ussr_and_asphalt_elsewhere(self):
        self.assertEqual(tramtrack.paving("UA")["m"], tramtrack.SLAB["m"])
        self.assertEqual(tramtrack.paving("CZ")["m"], tramtrack.ASPHALT["m"])
        self.assertEqual(tramtrack.paving("DE")["m"], tramtrack.ASPHALT["m"])

    def test_in_street_margin(self):
        ways = {("a", 0): (np.array([[0.0, 0.0], [100.0, 0.0]]), 4.0)}
        pts = np.array([[50.0, 3.0], [50.0, 4.0 + tramtrack.STREET_MARGIN_M - 0.2], [50.0, 4.0 + tramtrack.STREET_MARGIN_M + 1.0]])
        got = tramtrack.in_street(pts, ways)
        self.assertEqual(got.tolist(), [True, True, False])
        self.assertFalse(tramtrack.in_street(pts, {}).any())


class StripTextureTest(unittest.TestCase):
    """The user's street strips (plan 046 stage B): the materials exist and the rule uses them."""

    def materials(self):
        import json
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "mod", "beamng_earth", "levels",
                            "beamng_earth", "art", "shapes", "beamngEarth", "tram.materials.json")
        with open(path, encoding="utf8") as f:
            return json.load(f)

    def test_every_material_the_kernel_draws_with_is_in_tram_materials_json(self):
        have = self.materials()
        for key in ("STREET_ASPHALT", "STREET_SETTS", "STREET_SLAB", "STREET_GRASS", "BALLAST_TEX", "RAIL", "ASPHALT", "SLAB"):
            name = getattr(tramtrack, key)["m"]
            if key == "ASPHALT":            # (the road's own stock material)
                continue
            self.assertIn(name, have, key)

    def test_a_street_strip_has_its_rails_baked_in_and_the_old_materials_are_the_fallback(self):
        f = features([[(-50, 128), (300, 128)], [(-50, 128 + 6.5), (300, 128 + 6.5)]], ["residential", "tram"],
                     ids=["road0", "tram0"], subtypes=["road", "rail"])
        f.attrs["osm_gauge"] = np.array(["", "1000"], dtype=object)
        saved = tramtrack.ENABLED, tramtrack.STREET_TEX
        self.addCleanup(lambda: (setattr(tramtrack, "ENABLED", saved[0]), setattr(tramtrack, "STREET_TEX", saved[1])))
        tramtrack.ENABLED = True
        tramtrack.STREET_TEX = True
        new = rails.tile_chunks(FRAME, 0, 0, [f], "CZ")
        mats = {c["m"] for c in new}
        self.assertIn(tramtrack.STREET_SETTS["m"], mats, "a residential street in Czechia: setts")
        self.assertNotIn(tramtrack.RAIL["m"], mats, "the rails are in the picture")
        strip = [c for c in new if c["m"] == tramtrack.STREET_SETTS["m"]][0]
        self.assertAlmostEqual(strip["tl"], strip["w"], places=3)
        tramtrack.STREET_TEX = False
        old = {c["m"] for c in rails.tile_chunks(FRAME, 0, 0, [f], "CZ")}
        self.assertIn(tramtrack.RAIL["m"], old)
        self.assertNotIn(tramtrack.STREET_SETTS["m"], old)


class TileTest(unittest.TestCase):
    """The old materials (BNE_TRAM_STREET_TEX=off): a paving and a separate rail ribbon."""

    def setUp(self):
        self._on = tramtrack.ENABLED
        tramtrack.ENABLED = True
        self.addCleanup(setattr, tramtrack, "ENABLED", self._on)
        self._tex = tramtrack.STREET_TEX, tramtrack.TRACK3D
        tramtrack.STREET_TEX = tramtrack.TRACK3D = False
        self.addCleanup(lambda: (setattr(tramtrack, "STREET_TEX", self._tex[0]), setattr(tramtrack, "TRACK3D", self._tex[1])))

    def build(self, tram_y, gauge="1000", country=None):
        # a primary street along y = 128, and a tram along it at tram_y (in tile (0, 0))
        cells = [features([[(-50, 128), (300, 128)], [(-50, tram_y), (300, tram_y)]], ["primary", "tram"],
                          ids=["road0", "tram0"], subtypes=["road", "rail"])]
        cells[0].attrs["osm_gauge"] = np.array(["", gauge], dtype=object)
        half = roads.road_specs(cells[0])[0].width / 2
        return half, rails.tile_chunks(FRAME, 0, 0, cells, country)

    @staticmethod
    def mats(chunks):
        return {c["m"] for c in chunks}

    def test_a_tram_beside_the_street_is_paved_with_two_rails_and_no_sleepers(self):
        half, _far = self.build(128 + 60.0)
        _half, ch = self.build(128 + half + 1.5)      # (outside the drawn width, inside the margin)
        m = self.mats(ch)
        self.assertIn(tramtrack.ASPHALT["m"], m)
        self.assertNotIn(rails.SLEEPER["m"], m, "no white sleepers")
        self.assertNotIn(rails.BALLAST["m"], m, "no gravel bed in the street")
        self.assertNotIn("road_tramline", m, "not the stock decal (three lines)")
        self.assertEqual(sum(1 for c in ch if c["m"] == tramtrack.RAIL["m"]), 2, "two rails")

    def test_a_street_tram_in_ukraine_lies_in_slabs(self):
        half, _far = self.build(128 + 60.0)
        _h, ch = self.build(128 + half + 1.5, country="UA")
        self.assertIn(tramtrack.SLAB["m"], self.mats(ch))
        self.assertNotIn(tramtrack.ASPHALT["m"], self.mats(ch))

    def test_open_ground_tram_has_a_bed_and_rails_at_the_osm_gauge_and_no_sleepers(self):
        _half, ch = self.build(128 + 60.0, gauge="1000")
        m = self.mats(ch)
        self.assertIn(rails.BALLAST["m"], m)
        self.assertNotIn(rails.SLEEPER["m"], m)
        xs = sorted(float(np.asarray(c["n"])[:, 1].mean()) for c in ch if c["m"] == tramtrack.RAIL["m"])
        self.assertEqual(len(xs), 2)
        self.assertAlmostEqual(xs[1] - xs[0], 1.0 + rails.HEAD_M, delta=0.02)

    def test_paving_width_follows_the_gauge(self):
        half, _far = self.build(128 + 60.0)
        _h, narrow = self.build(128 + half + 1.0, gauge="1000")
        _h, wide = self.build(128 + half + 1.0, gauge="1435")
        wn = {c["w"] for c in narrow if c["m"] == tramtrack.ASPHALT["m"]}
        ww = {c["w"] for c in wide if c["m"] == tramtrack.ASPHALT["m"]}
        self.assertTrue(wn and ww)
        self.assertLess(max(wn), max(ww))

    def test_off_by_default_keeps_the_old_drawing(self):
        tramtrack.ENABLED = False
        _half, ch = self.build(128 + 60.0)
        self.assertIn(rails.SLEEPER["m"], self.mats(ch))
        self.assertNotIn(tramtrack.RAIL["m"], self.mats(ch))


class BedTest(unittest.TestCase):
    """The ground tram's bed (tramtrack.beds -> tram/profile.rs) and the plan's lines (railways.tram_grade_plans' parts)."""

    @staticmethod
    def line(n, y):
        return np.c_[np.arange(n) * 2.0, np.full(n, float(y))]

    def test_a_track_beside_a_raised_road_lies_on_its_formation_not_on_the_dem(self):
        xy = self.line(100, 8.0)
        road = (self.line(100, 0.0), np.full(100, 10.0), 1.0)
        z = tramtrack.beds([(xy, np.full(100, 8.8))], [road])[0]
        band = tramtrack.BAND_BASE_M + tramtrack.ROAD_GRADE * 7.0
        self.assertGreater(float(z.min()), 10.0 - band - 1e-6)

    def test_open_track_has_no_step_over_a_trams_grade(self):
        xy = self.line(150, 0.0)
        zd = np.where(np.arange(150) > 80, 0.04 * np.arange(150) + 0.7, 0.04 * np.arange(150))
        z = tramtrack.beds([(xy, zd)], [])[0]
        self.assertLessEqual(float(np.abs(np.diff(z)).max()), 2.0 * tramtrack.TRAM_GRADE * 2.0 + 1e-6)

    def test_a_crossing_side_street_makes_the_bed_flush_with_it(self):
        xy = self.line(100, 8.0)
        main = (self.line(100, 0.0), np.full(100, 10.0), 1.0)
        side = np.c_[np.full(20, 100.0), -4.0 + 2.0 * np.arange(20)]
        side_z = np.where(side[:, 1] < 4.0, 10.0, 8.8)
        z = tramtrack.beds([(xy, np.full(100, 8.8))], [main, (side, side_z, 3.0)])[0]
        self.assertLessEqual(float(np.abs(np.diff(z)).max()), 2.0 * tramtrack.TRAM_GRADE * 2.0 + 1e-6)

    def test_ground_tram_parts_are_only_asked_for(self):
        f = features([[(-100.0, 100.0), (400.0, 100.0)], [(-100.0, 140.0), (400.0, 140.0)]], ["standard_gauge", "tram"],
                     ids=["rail0", "tram0"], subtypes=["rail", "rail"])
        both = railways.parts(FRAME, [f], (0.0, 0.0, 256.0, 256.0), reach=60.0, trams="only")
        self.assertEqual([p[0] for p in both], ["tram0"])
        self.assertEqual({p[1] for p in both}, {"tram"})
        plain = railways.parts(FRAME, [f], (0.0, 0.0, 256.0, 256.0), reach=60.0)
        self.assertNotIn("tram0", [p[0] for p in plain], "the street track stays rails.py's")


class FollowTest(unittest.TestCase):
    """A street crossing a tram at grade ramps to it (tramtrack.follow -> tram/profile.rs follow_roads)."""

    def test_a_side_street_has_no_crest_at_the_tram(self):
        bed = (np.c_[np.arange(-50.0, 51.0, 2.0), np.zeros(51)], np.full(51, 10.04))
        side = np.c_[np.zeros(41), np.arange(-40.0, 41.0, 2.0)]
        zs = np.where(side[:, 1] < 0.0, 10.0 + 0.2 * side[:, 1], 10.0)      # (a 20 % drop beyond a flat top: the crest)
        got = tramtrack.follow([(side, zs, "secondary")], [bed])[0]
        self.assertLessEqual(float(np.abs(np.diff(got)).max()) / 2.0, 0.04 + 1e-6)
        self.assertAlmostEqual(float(got[20]), 10.0, places=6)
        # a road across nothing stays as it is
        far = np.c_[np.full(41, 200.0), np.arange(-40.0, 41.0, 2.0)]
        np.testing.assert_array_equal(tramtrack.follow([(far, zs, "primary")], [bed])[0], zs)


class SwitchAndBorderTest(unittest.TestCase):
    def setUp(self):
        self._saved = tramtrack.ENABLED, tramtrack.STREET_TEX
        self.addCleanup(lambda: (setattr(tramtrack, "ENABLED", self._saved[0]),
                                 setattr(tramtrack, "STREET_TEX", self._saved[1])))
        tramtrack.ENABLED = True
        tramtrack.STREET_TEX = True

    def chunks(self, lines, country="DE"):
        cl = [[(-50, 128), (400, 128)]] + lines
        f = features(cl, ["primary"] + ["tram"] * len(lines), ids=["road0"] + ["tram%d" % i for i in range(len(lines))],
                     subtypes=["road"] + ["rail"] * len(lines))
        f.attrs["osm_gauge"] = np.array([""] + ["1435"] * len(lines), dtype=object)
        return rails.tile_chunks(FRAME, 0, 0, [f], country)

    def test_a_switch_is_one_strip_over_plain_paving_and_the_branch_has_its_own_rails(self):
        through = [(40.0, 130.0), (150.0, 130.0)]
        a, b = [(40.0, 130.0), (100.0, 130.0)], [(100.0, 130.0), (150.0, 130.0)]
        branch = [(100.0, 130.0), (160.0, 140.0)]
        ch = self.chunks([a, b, branch])
        mats = [c["m"] for c in ch]
        self.assertIn(tramtrack.ASPHALT["m"], mats, "plain paving where the branch leaves")
        self.assertIn(tramtrack.STREET_ASPHALT["m"], mats)
        self.assertLess(mats.index(tramtrack.ASPHALT["m"]), mats.index(tramtrack.STREET_ASPHALT["m"]),
                        "drawn first: the strip lies over it")
        self.assertGreaterEqual(sum(1 for c in ch if c["m"] == tramtrack.RAIL["m"]), 2, "the branch's rails")
        del through

    def test_a_strip_ends_with_a_thin_concrete_border(self):
        ch = self.chunks([[(40.0, 131.0), (200.0, 131.0)]])
        borders = [c for c in ch if abs(c["w"] - 0.12) < 1e-9]
        self.assertGreaterEqual(len(borders), 1)
        self.assertTrue(all(c["m"] != "m_gm_concrete_plain" for c in borders), "in the strip's own paving, not bright concrete")


class PhysicsTest(unittest.TestCase):
    """The street rails' physics strips (BNE_TRAM_RAIL_PHYS, tram/physmesh.rs) in the tile's railway mesh."""

    def setUp(self):
        self._saved = tramtrack.ENABLED, tramtrack.RAIL_PHYS
        self.addCleanup(lambda: (setattr(tramtrack, "ENABLED", self._saved[0]),
                                 setattr(tramtrack, "RAIL_PHYS", self._saved[1])))
        tramtrack.ENABLED = True
        tramtrack.RAIL_PHYS = True
        self.f = features([[(-50, 128), (300, 128)], [(-50, 131.5), (300, 131.5)]], ["primary", "tram"],
                          ids=["road0", "tram0"], subtypes=["road", "rail"])
        self.f.attrs["osm_gauge"] = np.array(["", "1435"], dtype=object)

    @staticmethod
    def ground(p):
        return np.full(len(np.asarray(p).reshape(-1, 2)), 10.0)

    def test_the_rails_in_the_street_stand_a_few_mm_over_the_surface_in_a_metal_never_drawn_material(self):
        mesh = railways.tile_mesh(FRAME, 0, 0, [self.f], self.ground)
        self.assertIsNotNone(mesh)
        P, T, _uv, mats, M = mesh
        k = mats.index(railways.TRAM_PHYS)
        z = np.asarray(P)[np.unique(np.asarray(T)[np.asarray(M) == k].ravel())][:, 2]
        self.assertAlmostEqual(float(z.max()), 10.0 + 0.007, places=3)
        self.assertGreater(len(z), 100)
        self.assertIsNotNone(railways.collision_mesh(mesh), "the physics keeps them")
        self.assertIsNone(railways.render_mesh(mesh), "the drawing never has them")

    def test_off_leaves_the_tile_without_them(self):
        tramtrack.RAIL_PHYS = False
        self.assertIsNone(railways.tile_mesh(FRAME, 0, 0, [self.f], self.ground))

    def test_the_budget_is_a_few_triangles_a_metre(self):
        mesh = railways.tile_mesh(FRAME, 0, 0, [self.f], self.ground)
        # 2 rails x (head + counter-rail) x 6 triangles per sample, a sample every 2 m: 12 triangles a metre of track
        self.assertLess(len(mesh[1]), 14 * 260)


class Track3DTest(unittest.TestCase):
    """A tram's open track as a 3D mesh (BNE_TRAM_TRACK3D, plan 046 stage D) and no decal for it."""

    def setUp(self):
        self._saved = tramtrack.ENABLED, tramtrack.TRACK3D
        self.addCleanup(lambda: (setattr(tramtrack, "ENABLED", self._saved[0]), setattr(tramtrack, "TRACK3D", self._saved[1])))
        tramtrack.ENABLED = True
        tramtrack.TRACK3D = True
        self.f = features([[(-50, 128), (300, 128)], [(-50, 180), (300, 180)]], ["primary", "tram"],
                          ids=["road0", "tram0"], subtypes=["road", "rail"])
        self.f.attrs["osm_gauge"] = np.array(["", "1000"], dtype=object)

    @staticmethod
    def ground(p):
        return np.full(len(np.asarray(p).reshape(-1, 2)), 10.0)

    def test_the_open_track_is_a_mesh_with_a_low_ballast_light_rails_and_sleepers_and_has_no_decal(self):
        mesh = railways.tile_mesh(FRAME, 0, 0, [self.f], self.ground, country="US")
        self.assertIsNotNone(mesh)
        P, T, _uv, mats, M = mesh
        used = {mats[int(k)] for k in set(np.asarray(M).tolist())}
        self.assertIn(railways.STEEL, used)
        self.assertTrue(used & set(railways.SLEEPER_WORN), used)
        z = np.asarray(P)[:, 2]
        self.assertLess(float(z.max()) - 10.0, 0.02 + 0.04 + 0.12 + 1e-6)
        self.assertEqual(rails.tile_chunks(FRAME, 0, 0, [self.f], "US"), [], "the decals leave it out")
        # no ballast mesh at all: nothing but sleepers, rails (and the physics boxes) over the ground
        self.assertFalse(used & {railways.BALLAST, "bne_rail_ballast_stone"} , used)
        tri = np.asarray(T)
        for k in set(np.asarray(M).tolist()):
            if mats[int(k)] in set(railways.SLEEPER_WORN) | set(railways.STEEL_WORN) | {railways.COLLIDER}:
                continue
            zz = np.asarray(P)[np.unique(tri[np.asarray(M) == k].ravel())][:, 2]
            self.assertLess(float(zz.max()) - 10.0, 0.06, mats[int(k)])

    def test_off_gives_the_decal_back_and_a_green_track_stays_a_decal(self):
        tramtrack.TRACK3D = False
        self.assertIsNone(railways.tile_mesh(FRAME, 0, 0, [self.f], self.ground, country="US"))
        self.assertTrue(rails.tile_chunks(FRAME, 0, 0, [self.f], "US"))
        tramtrack.TRACK3D = True
        self.assertIsNone(railways.tile_mesh(FRAME, 0, 0, [self.f], self.ground, country="DE"), "a green track: its strip")
        self.assertIn(tramtrack.STREET_GRASS["m"], {c["m"] for c in rails.tile_chunks(FRAME, 0, 0, [self.f], "DE")})


class TransitionTest(unittest.TestCase):
    """A strip going over to open track (plan 046, the user's report 2026-11-14): one gauge, one height at the junction, no wall."""

    def setUp(self):
        self._saved = tramtrack.ENABLED, tramtrack.TRACK3D, tramtrack.STREET_TEX, tramtrack.RAIL_PHYS
        self.addCleanup(lambda: [setattr(tramtrack, n, v) for n, v in zip(("ENABLED", "TRACK3D", "STREET_TEX", "RAIL_PHYS"),
                                                                             self._saved)])
        tramtrack.ENABLED = tramtrack.TRACK3D = tramtrack.STREET_TEX = tramtrack.RAIL_PHYS = True
        # a street along y = 128 that ends at x = 120, a tram beside it (in its margin) going on past its end
        self.street = features([[(-50, 128), (120, 128)], [(-50, 131.5), (300, 131.5)]], ["primary", "tram"],
                               ids=["road0", "tram0"], subtypes=["road", "rail"])
        self.street.attrs["osm_gauge"] = np.array(["", "1435"], dtype=object)

    @staticmethod
    def ground(p):
        return np.full(len(np.asarray(p).reshape(-1, 2)), 10.0)

    def junction_x(self):
        ch = rails.tile_chunks(FRAME, 0, 0, [self.street], "US")
        strips = [c for c in ch if c["m"] == tramtrack.STREET_ASPHALT["m"]]
        self.assertTrue(strips)
        return max(float(np.asarray(c["n"])[:, 0].max()) for c in strips), ch

    def test_the_strip_is_as_wide_as_puts_its_baked_rails_a_gauge_apart(self):
        _x, ch = self.junction_x()
        strip = [c for c in ch if c["m"] == tramtrack.STREET_ASPHALT["m"]][0]
        # the asphalt strip's rails stand at 9.25 % and 90.75 % of it: 81.5 % of its width apart
        self.assertAlmostEqual(strip["w"] * 0.815, 1.435 + 0.075, places=2)

    def test_the_3d_track_meets_the_strip_at_its_height_and_rises_to_a_track_on_the_ground(self):
        xj, _ch = self.junction_x()
        mesh = railways.tile_mesh(FRAME, 0, 0, [self.street], self.ground, country="US")
        self.assertIsNotNone(mesh)
        P = np.asarray(mesh[0])
        x = P[:, 0] + roads.tile_bbox(0, 0)[0]
        z = P[:, 2]
        near = (x > xj - 0.1) & (x < xj + 0.25)
        far = (x > xj + 12.0) & (x < xj + 40.0)
        self.assertTrue(near.any() and far.any())
        self.assertLessEqual(float(z[near].max()) - 10.0, 0.01, "no step at the junction: ballast, sleepers and rails flush")
        self.assertGreater(float(z[far].max()) - 10.0, 0.10, "the rails stand 12 cm over the ballast further on")
        self.assertLess(float(z[far].max()) - 10.0, 0.16, "no embankment: a track on the ground")
        # a ramp: no step over 8 cm between samples 1 m apart along the rail heads
        for a in np.arange(xj, xj + 8.0, 1.0):
            sel0 = (x > a - 0.3) & (x < a + 0.3)
            sel1 = (x > a + 0.7) & (x < a + 1.3)
            if sel0.any() and sel1.any():
                self.assertLess(abs(float(z[sel1].max()) - float(z[sel0].max())), 0.10)

    def test_the_physics_strips_taper_where_they_end_and_not_at_the_tiles_cut(self):
        xj, _ch = self.junction_x()
        mesh = railways.tile_mesh(FRAME, 0, 0, [self.street], self.ground, country="US")
        P, T, _uv, mats, M = mesh
        k = mats.index(railways.TRAM_PHYS)
        v = np.asarray(P)[np.unique(np.asarray(T)[np.asarray(M) == k].ravel())]
        x = v[:, 0] + roads.tile_bbox(0, 0)[0]
        at_end = (x > xj - 0.05) & (x <= xj + 0.2)
        mid = (x > 40.0) & (x < 60.0)
        self.assertLess(float(v[at_end, 2].max()) - 10.0, 0.004, "the strips come up over 0.3 m at their end")
        self.assertAlmostEqual(float(v[mid, 2].max()) - 10.0, 0.007, places=3)


class CorridorTest(unittest.TestCase):
    """Nothing stands on a ground tram's rails (BNE_TRAM_CORRIDOR, tram/corridor.rs)."""

    def setUp(self):
        self._saved = tramtrack.ENABLED, tramtrack.CORRIDOR
        self.addCleanup(lambda: [setattr(tramtrack, n, v) for n, v in zip(("ENABLED", "CORRIDOR"), self._saved)])
        tramtrack.ENABLED = tramtrack.CORRIDOR = True
        self.f = features([[(-50, 128), (300, 128)], [(-50, 140), (300, 140)]], ["primary", "tram"],
                          ids=["road0", "tram0"], subtypes=["road", "rail"])
        self.parts = [(np.c_[np.arange(-50.0, 301.0, 2.0), np.full(176, 140.0)], 1.435)]

    def test_the_corridor_lines_chain_the_ways_and_the_hits_are_the_rails(self):
        a = np.c_[np.arange(0.0, 101.0, 10.0), np.zeros(11)]
        b = np.c_[np.arange(100.0, 201.0, 10.0), np.zeros(11)]
        self.assertEqual(len(tramtrack.corridor_lines([(a, 1.435), (b, 1.435)])), 1)
        hit = tramtrack.corridor_hits([[50.0, 0.0], [50.0, 1.0], [50.0, 2.0], [50.0, -2.5]], [(a, 1.435)])
        self.assertEqual(hit.tolist(), [True, True, False, False])

    def test_poles_and_shelters_keep_off_it(self):
        import fast
        railxing_obs = railxing.rail_obstacles(FRAME, [self.f], (0.0, 0.0, 256.0, 256.0))
        tram = [o for o in railxing_obs if o[0].startswith("tram:")]
        self.assertTrue(tram)
        ay = float(np.asarray(tram[0][2])[:, 1].mean())          # (the axis: the frame's y)
        others = fast.Others(railxing_obs)
        margin = 1.5                                    # (furniture.CLEAR_OTHER_M)
        self.assertFalse(others.clear(np.array([100.0, ay]), None, margin), "a pole on the axis is moved or dropped")
        self.assertFalse(others.clear(np.array([100.0, ay + 1.2]), None, margin))
        self.assertTrue(others.clear(np.array([100.0, ay + 2.0]), None, margin), "one 2 m off the axis stays")
        tramtrack.CORRIDOR = False
        self.assertFalse(any(o[0].startswith("tram:") for o in railxing.rail_obstacles(FRAME, [self.f], (0.0, 0.0, 256.0, 256.0))))

    def test_buildings_keep_off_it(self):
        import buildings
        x0, y0 = roads.tile_bbox(0, 0)[0], roads.tile_bbox(0, 0)[1]
        lines = buildings.rail_lines(FRAME, [self.f], x0, y0)
        self.assertEqual(len(lines), 1)
        ay = float(np.asarray(lines[0])[:, 1].mean())
        box = lambda cy: np.array([[90.0, cy - 1.0], [110.0, cy - 1.0], [110.0, cy + 1.0], [90.0, cy + 1.0]])
        self.assertTrue(buildings.on_rail_bed(box(ay), lines), "a footprint over the rails")
        self.assertTrue(buildings.on_rail_bed(box(ay + 2.5), lines), "a wall 1.5 m off the axis")
        self.assertFalse(buildings.on_rail_bed(box(ay + 5.0), lines), "a building 4 m off stays")

    def test_sign_posts_keep_off_it(self):
        import signplacement
        parts = [(np.c_[np.arange(-50.0, 301.0, 2.0), np.full(176, 140.0)], 1.435)]
        ctx = signplacement.Context(corridor=parts)
        eyes = [np.array([100.0, 120.0])]
        self.assertFalse(ctx.post(np.array([100.0, 140.0]), np.array([0.0, 1.0]), 0.6, eyes))
        self.assertTrue(ctx.post(np.array([100.0, 143.0]), np.array([0.0, 1.0]), 0.6, eyes))
        self.assertFalse(ctx.clear(np.array([100.0, 140.5])))


class TurnoutTest(unittest.TestCase):
    """A turnout of the 3D track (plan 046, switches in 3D): the branch leaves tangent, one set of sleepers over the switch,
    its rails part and meet the stock rail at the frog and cross nothing else."""

    def setUp(self):
        self._saved = tramtrack.ENABLED, tramtrack.TRACK3D, tramtrack.SWITCH3D
        self.addCleanup(lambda: [setattr(tramtrack, n, v) for n, v in zip(("ENABLED", "TRACK3D", "SWITCH3D"), self._saved)])
        tramtrack.ENABLED = tramtrack.TRACK3D = tramtrack.SWITCH3D = True
        a, b = [(40.0, 200.0), (100.0, 200.0)], [(100.0, 200.0), (170.0, 200.0)]
        branch = [(100.0, 200.0), (130.0, 208.0), (170.0, 224.0)]
        self.f = features([[(-50, 128), (300, 128)], a, b, branch], ["primary", "tram", "tram", "tram"],
                          ids=["road0", "ta", "tb", "tc"], subtypes=["road", "rail", "rail", "rail"])
        self.f.attrs["osm_gauge"] = np.array(["", "1435", "1435", "1435"], dtype=object)

    @staticmethod
    def ground(p):
        return np.full(len(np.asarray(p).reshape(-1, 2)), 10.0)

    def runs(self):
        box = roads.tile_bbox(0, 0)
        return rails.tram_open_runs(FRAME, 0, 0, [self.f], "US", (box[0] - 10, box[1] - 10, box[2] + 10, box[3] + 10))

    def test_the_branch_knows_its_zone_and_the_through_track_its_frog(self):
        runs = self.runs()
        branches = [r for r in runs if r[4].get("branch")]
        thr = [r for r in runs if r[4].get("marks")]
        self.assertEqual(len(branches), 1)
        self.assertEqual(len(thr), 1)
        b = branches[0][4]["branch"]
        mk = thr[0][4]["marks"][0]
        self.assertGreater(b["zone"], 2.0)
        self.assertLess(b["div"], b["zone"], "the rails part before the sleepers may")
        self.assertIsNotNone(b["frog"])
        self.assertAlmostEqual(b["frog"][0], mk["frog"][0], places=6)
        self.assertAlmostEqual(b["frog"][1], mk["frog"][1], places=6)
        # the branch starts at the heel on the through track, tangent to it
        line = np.asarray(branches[0][0])
        self.assertLess(abs(float(line[0][1]) - float(np.asarray(thr[0][0])[:, 1].mean())), 0.05, "starts at the heel on the through axis")
        d = line[2] - line[0]
        self.assertLess(abs(d[1] / d[0]), 0.1, "leaves the through track tangent")

    def test_rails_cross_only_at_the_frog_and_it_is_within_5_mm_of_both(self):
        runs = self.runs()
        br = [r for r in runs if r[4].get("branch")][0]
        th = [r for r in runs if r[4].get("marks")][0]
        g = br[1]
        half = g / 2 + 0.0375
        off = railways.roads.offset_polyline
        bl, brail = np.asarray(br[0]), np.asarray(br[0])
        arc = railways._arc(bl)
        drawn = bl[arc >= br[4]["branch"]["div"]]           # (the branch's rails are drawn from where they have parted)
        branch_rails = [off(drawn, half), off(drawn, -half)]
        stock = [off(np.asarray(th[0]), half), off(np.asarray(th[0]), -half)]
        hits = []
        for i, r1 in enumerate(branch_rails):
            for j, r2 in enumerate(stock):
                for k in range(len(r1) - 1):
                    for m in range(len(r2) - 1):
                        p, e = r1[k], r1[k + 1] - r1[k]
                        q, f = r2[m], r2[m + 1] - r2[m]
                        den = e[0] * f[1] - e[1] * f[0]
                        if abs(den) < 1e-12:
                            continue
                        t = ((q - p)[0] * f[1] - (q - p)[1] * f[0]) / den
                        u = ((q - p)[0] * e[1] - (q - p)[1] * e[0]) / den
                        if 0 <= t <= 1 and 0 <= u <= 1:
                            hits.append(p + e * t)
        self.assertEqual(len(hits), 1, "one crossing: the frog")
        frog = np.array(br[4]["branch"]["frog"])
        self.assertLess(float(np.hypot(*(hits[0] - frog))), 0.005)

    def test_one_set_of_sleepers_over_the_switch(self):
        runs = self.runs()
        br = [r for r in runs if r[4].get("branch")][0]
        th = [r for r in runs if r[4].get("marks")][0]
        zone = br[4]["branch"]["zone"]

        def sleepers(run):
            xy = railways._resample(np.asarray(run[0]), railways.STEP_M)
            n = len(xy)
            f = np.zeros(n, bool)
            _r, boxes, _c = tramtrack.open_mesh(xy, run[1], np.full(n, 10.0), f, ~f, f, (run[2], run[3]), run[4])
            return xy, [b for b in boxes if b[7] == 2]
        bxy, bb = sleepers(br)
        arc = railways._arc(bxy)
        for b in bb:
            for c in np.asarray(b[0]):
                k = int(np.argmin(np.hypot(*(bxy - c).T)))
                self.assertGreaterEqual(float(arc[k]), zone - railways.STEP_M, "the branch's own sleepers start after the zone")
        _txy, tb = sleepers(th)
        self.assertGreater(max(float(np.max(b[6])) for b in tb), 1.2, "the through track's sleepers reach across to the branch")

    def test_off_draws_the_old_independent_tracks(self):
        tramtrack.SWITCH3D = False
        self.assertFalse(any(r[4].get("branch") for r in self.runs()))


class MedianTramTest(unittest.TestCase):
    """A ground tram in a divided street's median: no raised median band over its strip, in every country (plan 046, Prague's
    Dejvice median: the curb band ran beside the strip, which lay like a trench)."""

    def setUp(self):
        self._saved = tramtrack.ENABLED, tramtrack.CORRIDOR
        self.addCleanup(lambda: [setattr(tramtrack, n, v) for n, v in zip(("ENABLED", "CORRIDOR"), self._saved)])
        tramtrack.ENABLED = tramtrack.CORRIDOR = True

    def bands(self, with_tram, corridor=True):
        import geodesy
        tramtrack.CORRIDOR = corridor
        lines = [[(20, 100), (230, 100)], [(230, 124), (20, 124)]]
        classes = ["secondary", "secondary"]
        subtypes = ["road", "road"]
        if with_tram:
            lines.append([(20, 104.9), (230, 104.9)])
            classes.append("tram")
            subtypes.append("rail")
        f = features(lines, classes, ids=["rd0", "rd1", "tram0"][:len(lines)], subtypes=subtypes)
        f.attrs["name"] = np.array(["Divided street", "Divided street", ""][:len(lines)])
        walks = []
        roads.build_tile(FRAME, 0, 0, [f], lambda la, lo: np.full(np.shape(la), 10.0), urban_fn=lambda la, lo: np.ones(np.shape(la), bool),
                         walks=walks, country="CZ")
        return [w for w in walks if "pts" in w and w.get("median")]

    @staticmethod
    def over(ws, y=104.9, half=1.0):
        """Median-band points within half of the tram axis y = 104.9 (the frame's y of the test's metres)."""
        return sum(int((np.abs(np.asarray(w["pts"])[:, 1] - y) < half).sum()) for w in ws)

    def test_no_median_band_runs_over_the_strip(self):
        self.assertGreater(self.over(self.bands(False, corridor=False), half=1.3), 0, "the band's points lie near the tram's place")
        self.assertEqual(self.over(self.bands(True)), 0, "no band over the strip")

    def test_off_leaves_the_bands(self):
        self.assertGreater(self.over(self.bands(True, corridor=False), half=1.3), 0)


class BridgeTest(unittest.TestCase):
    """A tram bridge keeps the street's level: no railway embankment level lifts its deck over the street."""

    @staticmethod
    def deck(cls):
        xy = np.c_[np.arange(0.0, 41.0, 2.0), np.zeros(21)]

        def ground(p):
            p = np.asarray(p, np.float64).reshape(-1, 2)
            return np.where((p[:, 0] >= -0.5) & (p[:, 0] <= 40.5), 10.0, 11.0)      # (a plateau a metre up beyond its ends)
        zb, _zg = railways.profile([("f", cls, "bridge", xy)], ground)[0]
        return zb

    def setUp(self):
        self._on = tramtrack.ENABLED
        self.addCleanup(setattr, tramtrack, "ENABLED", self._on)

    def test_a_tram_bridge_is_not_lifted_over_the_street(self):
        tramtrack.ENABLED = True
        zb = self.deck("tram")
        self.assertLess(float(zb.max()) - 10.0, 0.05)

    def test_a_railway_bridge_still_holds_the_embankment_level(self):
        tramtrack.ENABLED = True
        self.assertGreater(float(self.deck("standard_gauge").max()) - 10.0, 0.9)

    def test_off_by_default_keeps_the_old_lift(self):
        tramtrack.ENABLED = False
        self.assertGreater(float(self.deck("tram").max()) - 10.0, 0.9)


class RoadDeckTest(unittest.TestCase):
    """A street tram's bridge on a road deck: that deck's height, rails and paving only, no slab and no piers."""

    def setUp(self):
        self._on = tramtrack.ENABLED
        tramtrack.ENABLED = True
        self.addCleanup(setattr, tramtrack, "ENABLED", self._on)
        self._t3 = tramtrack.TRACK3D
        tramtrack.TRACK3D = False       # (the ground stretches beside the bridge are not what these test)
        self.addCleanup(setattr, tramtrack, "TRACK3D", self._t3)
        self.xy = np.c_[np.arange(0.0, 41.0, 2.0), np.zeros(21)]
        # the road deck beside it (2 m off the tram), rising 10 -> 12 m over its 40 m
        self.deck = [(np.c_[np.arange(0.0, 41.0, 4.0), np.full(11, 2.0)], np.linspace(10.0, 12.0, 11))]

    def test_height_is_the_decks_where_it_is_near_and_nan_elsewhere(self):
        z, on = railways.road_deck_z(self.xy, self.deck)
        self.assertTrue(on.all())
        np.testing.assert_allclose(z, 10.0 + 2.0 * self.xy[:, 0] / 40.0, atol=0.01)
        far = [(self.deck[0][0] + [0.0, 30.0], self.deck[0][1])]
        z, on = railways.road_deck_z(self.xy, far)
        self.assertFalse(on.any())
        self.assertTrue(np.isnan(z).all())

    def test_profile_follows_the_road_deck_steeper_than_a_railways_grade(self):
        def ground(p):
            return np.full(len(np.asarray(p).reshape(-1, 2)), 10.0)
        zb, _zg = railways.profile([("f", "tram", "bridge", self.xy)], ground, (), self.deck)[0]
        np.testing.assert_allclose(zb, 10.0 + 2.0 * self.xy[:, 0] / 40.0, atol=0.01)
        zb, _zg = railways.profile([("f", "tram", "bridge", self.xy)], ground, ())[0]
        self.assertLess(float(zb.max()), 10.2, "without a road deck: its own profile")

    def test_mesh_has_the_rails_and_no_slab_or_piers_on_a_road_deck(self):
        # tile (0, 0): a tram bridge along y = 100 on the road deck (a primary road bridge beside it)
        f = features([[(-100.0, 100.0), (400.0, 100.0)]], ["tram"], ids=["tram0"], subtypes=["rail"])
        f.attrs["osm_railway"] = np.array(["tram"], object)
        ranges = {"ranges": [(0.2, 0.8, "bridge")]}
        deck = [(np.c_[np.arange(-100.0, 401.0, 5.0), np.full(101, 101.0)], np.full(101, 10.0))]

        def ground(p):
            return np.full(len(np.asarray(p).reshape(-1, 2)), 0.0)
        saved = roads.level_ranges
        roads.level_ranges = lambda feats, i, **kw: ranges["ranges"]
        try:
            with_deck = railways.tile_mesh(FRAME, 0, 0, [f], ground, road_decks=deck, country="UA")
            alone = railways.tile_mesh(FRAME, 0, 0, [f], ground)
        finally:
            roads.level_ranges = saved
        self.assertIsNotNone(with_deck)
        self.assertIsNotNone(alone)
        def used(mesh):
            return {mesh[3][int(k)] for k in set(np.asarray(mesh[4]).tolist())}
        self.assertIn(railways.CONCRETE, used(alone), "its own slab and piers")
        self.assertNotIn(railways.CONCRETE, used(with_deck), "no slab and no piers on the road's deck")
        self.assertIn(tramtrack.SLAB["m"], used(with_deck), "slabs for Ukraine")
        self.assertIn(railways.STEEL, used(with_deck), "the rails")
        z = np.asarray(with_deck[0])[:, 2]
        self.assertTrue(np.all(z > 9.8), "everything at the deck height (a rail foot 0.16 m under it), nothing down to the ground")


if __name__ == "__main__":
    unittest.main()
