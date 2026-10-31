"""landmarks: world-famous structures drawn as their own models (GitHub #123), keyed by their OSM way."""
import json
import math
import os
import re
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import buildings as bld  # noqa: E402
import landmarks  # noqa: E402
from test_buildings import Feats  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def tower_ring(angle_deg=43.0, half=landmarks.EIFFEL_BASE_M / 2, centre=(0.0, 0.0)):
    a = math.radians(angle_deg)
    u = np.array([math.cos(a), math.sin(a)])
    v = np.array([-u[1], u[0]])
    return np.array([np.asarray(centre) + u * su * half + v * sv * half
                     for su, sv in ((-1, -1), (1, -1), (1, 1), (-1, 1))])


def mesh(spec, gmin=0.0, lite=False, collision=False):
    m = bld.Mesh()
    tok = (0, landmarks.colour(spec.facade))
    if collision:
        landmarks.collision(m, spec, gmin, tok)
    else:
        landmarks.build(m, spec, gmin, tok, lite)
    return m.arrays()


class WaysTest(unittest.TestCase):
    def test_of(self):
        self.assertEqual(landmarks.of("w5013364@184"), "eiffel")
        self.assertEqual(landmarks.of("w5013364"), "eiffel", "(any version)")
        self.assertEqual(landmarks.of("r1834818@68"), "colosseum", "(a relation: a footprint with a courtyard)")
        for rec in ("w50133640@1", "r5013364@1", "n5013364@1", "", None, "wabc@1"):
            with self.subTest(rec=rec):
                self.assertIsNone(landmarks.of(rec))


class EiffelTest(unittest.TestCase):
    def test_its_real_size_on_its_footprint(self):
        spec = bld.landmark_spec(tower_ring(centre=(30.0, -20.0)), "eiffel")
        P, _UV, T, _M, C = mesh(spec, gmin=33.0)
        self.assertGreater(len(T), 3000, "an open lattice, not a block")
        self.assertLess(len(T), bld.MAX_MESH_VERTS // 3)
        self.assertAlmostEqual(P[:, 2].min(), 33.0, delta=0.6, msg="on its ground")
        self.assertAlmostEqual(P[:, 2].max(), 33.0 + landmarks.EIFFEL_ANTENNA_Z, delta=1.0, msg="330 m with its antenna")
        c = P[:, :2].mean(0)
        self.assertLess(np.hypot(*(c - (30.0, -20.0))), 3.0, "on its footprint's centre")
        a = math.radians(43.0)
        u = np.array([math.cos(a), math.sin(a)])
        along = (P[:, :2] - (30.0, -20.0)) @ u
        self.assertAlmostEqual(along.max(), landmarks.EIFFEL_BASE_M / 2, delta=1.5, msg="the base as wide as the map's")
        self.assertTrue((C[:, :3] == landmarks.EIFFEL_COLOUR[:3]).all(), "its paint")

    def test_tapers_and_open_under_the_arches(self):
        spec = bld.landmark_spec(tower_ring(), "eiffel")
        P = mesh(spec)[0]
        a = math.radians(43.0)
        axes = np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])     # (columns: u, v)
        for z, at_most in ((5.0, 63.5), (60.0, 38.0), (200.0, 15.0), (300.0, 5.5)):
            near = P[np.abs(P[:, 2] - z) < 3.0]
            with self.subTest(z=z):
                self.assertLessEqual(float(np.abs(near[:, :2] @ axes).max()), at_most, "the outline narrows")
        # nothing under the middle of the tower below the arches (the square under the first platform is open)
        low = P[(P[:, 2] < 30.0) & (np.hypot(P[:, 0], P[:, 1]) < 20.0)]
        self.assertEqual(len(low), 0, "open under the tower")
        col = mesh(spec, collision=True)[0]
        self.assertEqual(len(col[(col[:, 2] < 50.0) & (np.hypot(col[:, 0], col[:, 1]) < 20.0)]), 0,
                         "driven under: no collision there")
        self.assertGreater(col[:, 2].max(), landmarks.EIFFEL_P1_Z, "its legs and first deck collide")

    def test_far_version_is_lighter(self):
        spec = bld.landmark_spec(tower_ring(), "eiffel")
        full, lite = mesh(spec)[2], mesh(spec, lite=True)[2]
        self.assertLess(len(lite), len(full) / 3)
        self.assertAlmostEqual(mesh(spec, lite=True)[0][:, 2].max(), landmarks.EIFFEL_ANTENNA_Z, delta=1.0)


class LampsTest(unittest.TestCase):
    def test_points_of_light_on_the_lattice(self):
        """The Eiffel Tower's lighting: small lamps up its girders and round its platforms (their own material, lit at
        night), not the whole iron glowing; fewer and bigger far away; none on the stone landmarks."""
        spec = bld.landmark_spec(tower_ring(), "eiffel")
        for lite, at_least in ((False, 1500), (True, 60)):
            m = bld.Mesh()
            iron, lampm = (0, landmarks.colour("eiffel")), (1, landmarks.LAMP_COLOUR)
            landmarks.build(m, spec, 0.0, iron, lite, {"lamp": lampm})
            P, _UV, T, M, _C = m.arrays()
            n_lamps = int((M == 1).sum()) // landmarks.LAMP_TRIS
            with self.subTest(lite=lite):
                self.assertGreater(n_lamps, at_least)
                lp = P[T[M == 1]].reshape(-1, 3)
                self.assertGreater(lp[:, 2].max(), landmarks.EIFFEL_P3_Z, "up to the top platform")
                size = np.ptp(P[T[M == 1][:landmarks.LAMP_TRIS]].reshape(-1, 3), axis=0).max()
                self.assertLess(size, 2 * (landmarks.LITE_LAMP_R if lite else landmarks.LAMP_R) + 1e-6, "small")
                self.assertGreater(int((M == 0).sum()), int((M == 1).sum()) / 3, "the iron still the tower")
        m = bld.Mesh()
        landmarks.build(m, bld.landmark_spec(tower_ring(65.0, 25.0), "arc"), 0.0, (0, (1, 1, 1, 255)), False,
                        {"lamp": (1, landmarks.LAMP_COLOUR)})
        self.assertEqual(int((m.arrays()[3] == 1).sum()), 0, "(the Arc de Triomphe is floodlit: no lamps)")

    def test_the_colosseum_lit_in_its_arches_and_big_bens_dials(self):
        t = np.linspace(0.0, 2 * math.pi, 50, endpoint=False)
        spec = bld.landmark_spec(np.stack([85.0 * np.cos(t), 74.0 * np.sin(t)], 1), "colosseum")
        m = bld.Mesh()
        landmarks.build(m, spec, 0.0, (0, landmarks.COL_COLOUR), False, {"lamp": (1, landmarks.LAMP_COLOUR)})
        P, _UV, T, M, _C = m.arrays()
        n = int((M == 1).sum()) // landmarks.LAMP_TRIS
        self.assertGreater(n, landmarks.COL_ARCHES, "a lamp in the arches all round, on every storey that stands")
        lp = P[T[M == 1]].reshape(-1, 3)
        self.assertLess(lp[:, 2].max(), sum(landmarks.COL_TIERS) + landmarks.COL_ATTIC_H, "up to the attic's top")
        self.assertEqual(landmarks.material("colosseum"), "stone", "its walls dark at night ...")
        edge = lp[np.abs(lp[:, 2] - sum(landmarks.COL_TIERS) + 0.6) < 1.0]
        self.assertGreater(len(edge), 0, "... its storeys' top edges drawn in lamps")
        m = bld.Mesh()
        landmarks.build(m, bld.landmark_spec(tower_ring(10.0, 7.0), "bigben"), 0.0, (0, landmarks.BB_COLOUR), False,
                        {"dial": (2, landmarks.BB_DIAL)})
        P, _UV, T, M, _C = m.arrays()
        dz = P[T[M == 2]].reshape(-1, 3)[:, 2]
        self.assertGreater(int((M == 2).sum()), 0, "its dials in their own material")
        self.assertTrue(((dz > landmarks.BB_CLOCK_Z) & (dz < landmarks.BB_CLOCK_Z + landmarks.BB_CLOCK_H)).all())


class StoneLandmarksTest(unittest.TestCase):
    def build(self, name, ring):
        spec = bld.landmark_spec(ring, name)
        return spec, mesh(spec)[0], mesh(spec, lite=True)[0], mesh(spec, collision=True)[0]

    def test_arc_de_triomphe(self):
        # its map footprint (~50 x 28 m, the long side its 45 m front): the great arch through its depth, open
        spec, P, lite, col = self.build("arc", tower_ring(65.0, 25.0)[[0, 1, 2, 3]] * [1.0, 1.0])
        self.assertAlmostEqual(P[:, 2].max(), landmarks.ARC_H, delta=0.5)
        a = math.radians(65.0)
        u = np.array([math.cos(a), math.sin(a)])
        along = P[:, :2] @ u
        under = P[(np.abs(along) < landmarks.ARC_MAIN_W / 2 - 0.5) & (P[:, 2] > 1.0) &
                  (P[:, 2] < landmarks.ARC_MAIN_H - landmarks.ARC_MAIN_W / 2 - 0.5)]
        self.assertEqual(len(under), 0, "the great arch open from the ground to its springing")
        self.assertEqual(len(col[(np.abs(col[:, :2] @ u) < 5.0) & (col[:, 2] < 15.0)]), 0, "driven through")

    def test_big_ben(self):
        spec, P, lite, col = self.build("bigben", tower_ring(10.0, 7.0))
        self.assertAlmostEqual(P[:, 2].max(), landmarks.BB_H, delta=0.5)
        self.assertLess(np.abs(P[:, :2]).max(), 12.0, "a 12 m square tower")
        self.assertGreater(col[:, 2].max(), 50.0)

    def test_colosseum(self):
        t = np.linspace(0.0, 2 * math.pi, 50, endpoint=False)
        ring = np.stack([85.0 * np.cos(t), 74.0 * np.sin(t)], 1)
        spec, P, lite, col = self.build("colosseum", ring)
        self.assertAlmostEqual(P[:, 2].max(), sum(landmarks.COL_TIERS) + landmarks.COL_ATTIC_H, delta=0.5)
        r = np.hypot(P[:, 0] / 85.0, P[:, 1] / 74.0)
        self.assertLess(r.max(), 1.03, "within its footprint")
        high = P[P[:, 2] > sum(landmarks.COL_TIERS[:2]) + 1.0]
        self.assertGreater(high[:, 1].mean(), 0.0, "the outer ring stands on the north side, fallen on the south")
        self.assertEqual(len(P[(r < 0.3) & (P[:, 2] > 1.0)]), 0, "the arena open")
        self.assertLess(len(mesh(spec, lite=True)[2]), len(mesh(spec)[2]))


class LibertyTest(unittest.TestCase):
    def test_on_its_star_facing_the_harbour(self):
        t = np.linspace(0, 2 * math.pi, 22, endpoint=False)
        rr = np.where(np.arange(22) % 2 == 0, 48.0, 36.0)
        ring = np.stack([rr * np.cos(t), rr * np.sin(t)], 1) + (10.0, -5.0)
        spec = bld.landmark_spec(ring, "liberty")
        self.assertEqual(landmarks.of("w32965412@109"), "liberty")
        m = bld.Mesh()
        landmarks.build(m, spec, 0.0, (0, landmarks.SOL_GRANITE), False,
                        {"lamp": (1, landmarks.LAMP_COLOUR), "floodlit": (2, (255, 255, 255, 255))})
        P, _UV, T, M, C = m.arrays()
        self.assertAlmostEqual(P[:, 2].max(), landmarks.SOL_TORCH_Z, delta=1.0, msg="the torch at 93 m")
        top = P[T[M == 1]].reshape(-1, 3)
        torch = top[top[:, 2] > landmarks.SOL_TORCH_Z - 4.0].mean(0)
        self.assertGreater(len(top[top[:, 2] > landmarks.SOL_TORCH_Z - 4.0]), 0, "its flame lit")
        statue = P[T[M == 2]].reshape(-1, 3)
        self.assertGreater(statue[:, 2].min(), landmarks.SOL_BASE_Z - 0.5, "the statue on the pedestal, floodlit")
        mid = statue[:, :2].mean(0)
        self.assertLess(np.hypot(*(mid - (10.0, -5.0))), 4.0, "in the middle of its star")
        fx = np.array([landmarks.SOL_FACING[1], -landmarks.SOL_FACING[0]])
        self.assertGreater(float((torch[:2] - mid) @ fx), 1.5, "the torch in her right hand")
        self.assertTrue(((P[:, 2] < landmarks.SOL_FORT_H + 0.1) | (np.hypot(P[:, 0] - 10.0, P[:, 1] + 5.0) < 25.0)).all(),
                        "over the fort's walls only the pedestal and the statue")
        col = bld.Mesh()
        landmarks.collision(col, spec, 0.0, (0, (1, 1, 1, 255)))
        self.assertGreater(col.arrays()[0][:, 2].max(), landmarks.SOL_BASE_Z)


class TileTest(unittest.TestCase):
    def test_drawn_instead_of_its_parts(self):
        """The map's Tour Eiffel (OSM way 5013364) and its parts: one landmark Spec, the parts left out; the building
        beside it as before. Its mesh, its collision, its skyline model."""
        import geodesy
        f = geodesy.EnuFrame(48.85826, 2.29450)

        def ll(ring):
            ring = np.vstack([ring, ring[:1]])
            la, lo, _ = f.enu_to_geodetic(ring[:, 0], ring[:, 1], 0.0)
            return np.stack([lo, la], 1)
        house = np.array([(100.0, 100.0), (120.0, 100.0), (120.0, 115.0), (100.0, 115.0)])
        b = Feats(["E", "H"], [ll(tower_ring()), ll(house)],
                  **{"class": ["", "house"], "height": [330.0, 9.0], "has_parts": [1.0, 0.0],
                     "min_height": [np.nan, np.nan], "src_record": ["w5013364@184", "w1@1"]})
        parts = Feats(["p1", "p2"], [ll(tower_ring(half=40.0)), ll(tower_ring(half=10.0))],
                      building_id=["E", "E"], height=[57.6, 276.0], min_height=[4.0, 115.7],
                      num_floors=[np.nan, np.nan], min_floor=[np.nan, np.nan])
        items = bld.tile_buildings(f, -128, -128, 256, 256, [b], lambda la, lo: np.full(np.shape(la), 33.0),
                                   part_layers=[parts])
        cls = sorted(s.cls or "" for s, _, _ in items)
        self.assertEqual(len(items), 2, "the tower and the house, not the tower's parts")
        lm = [s for s, _, _ in items if s.cls == landmarks.LANDMARK]
        self.assertEqual([s.facade for s in lm], ["eiffel"], cls)
        drawn = bld.build(items)
        self.assertIn(bld.LANDMARK_IRON, drawn[4])
        iron = drawn[4].index(bld.LANDMARK_IRON)
        self.assertGreater(int((drawn[3] == iron).sum()), 3000, "the tower in the iron material")
        self.assertIsNotNone(bld.collision_mesh(items))
        sky = bld._sky_mesh(items)
        self.assertLess(len(sky[2]), len(drawn[2]), "far away: its lite model, not a 330 m box")
        self.assertEqual(bld.collider_boxes([(s, g0, g1) for s, g0, g1 in items if s.cls == landmarks.LANDMARK],
                                            0, 0), [], "(no box round it: driven under)")
        self.assertEqual(bld.doors(items, [], 0, 0), [], "no door")
        # without the landmark: as before, its parts
        saved = dict(landmarks.WAYS)
        landmarks.WAYS.clear()
        try:
            before = bld.tile_buildings(f, -128, -128, 256, 256, [b], lambda la, lo: np.full(np.shape(la), 33.0),
                                        part_layers=[parts])
        finally:
            landmarks.WAYS.update(saved)
        self.assertEqual(len(before), 3, "(the parts and the house)")


class MaterialTest(unittest.TestCase):
    def test_its_material_is_shipped_and_lit_at_night(self):
        shapes = os.path.join(ROOT, "mod", "beamng_earth", "levels", "beamng_earth", "art", "shapes", "beamngEarth")
        with open(os.path.join(shapes, "buildings.materials.json"), encoding="utf-8") as fh:
            mats = json.load(fh)
        lua = os.path.join(ROOT, "mod", "beamng_earth", "lua", "ge", "extensions", "beamngEarth", "nightlights.lua")
        with open(lua, encoding="utf-8") as fh:
            night = fh.read()
        for name in (bld.LANDMARK_IRON, bld.LANDMARK_STONE, bld.LANDMARK_LAMP, bld.LANDMARK_FLOODLIT, bld.LANDMARK_DIAL):
            with self.subTest(name=name):
                self.assertIn(name, mats)
                self.assertIn(name, bld.MATERIALS)
                stage = mats[name]["Stages"][0]
                self.assertTrue(stage.get("vertColor"), "its paint by vertex colour")
                self.assertEqual(stage.get("emissiveFactor"), [0, 0, 0], "dark by day")
                m = re.search(name + r" = \{ '([0-9. ]+)', (\d+) \}", night)
                if name in (bld.LANDMARK_IRON, bld.LANDMARK_STONE):
                    self.assertIsNone(m, "the iron stays dark at night: its lamps light it (stone: unlit stone)")
                else:
                    self.assertIsNotNone(m, "lit at night (nightlights.lua)")
                    self.assertGreater(int(m.group(2)), 0)
        for name in landmarks.BUILDERS:
            self.assertIn(landmarks.material(name), bld.LANDMARK_MATERIALS)


if __name__ == "__main__":
    unittest.main()
