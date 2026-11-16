"""The tram poles' assets (plan 046, stage E): every group the kernel names has its shape (tools/blender/build_tram_poles.py),
its trees.lua catalog entry and materials; the poles have a collision box (poleprofiles.COLLISION_BOXES, plan 042's rule)
that holds the shape's own Colmesh; the lamp brackets have their lenses; a pole replaces the street lamps beside it."""
import os
import re
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import fast  # noqa: E402
import furniture  # noqa: E402
import poleprofiles  # noqa: E402
import tramcat  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
SHAPES = os.path.join(ROOT, "mod", "beamng_earth", "levels", "beamng_earth", "art", "shapes", "beamngEarth")
TREES = os.path.join(ROOT, "mod", "beamng_earth", "lua", "ge", "extensions", "beamngEarth", "trees.lua")
LAMPS = ("tram_lamp_g", "tram_lamp_k", "tram_lamp_t")
GROUPS = sorted(tramcat.POLE_GROUPS | set(LAMPS))
POLES = [g for g in GROUPS if g not in LAMPS and g != "tram_rosette"]


def dae(group):
    with open(os.path.join(SHAPES, "bne_" + group + ".dae"), encoding="utf-8") as f:
        return f.read()


def colmesh_bounds(text):
    m = re.search(r'<geometry id="Colmesh_[^"]*-mesh"[^>]*>.*?<float_array[^>]*>([^<]*)</float_array>', text, re.S)
    v = np.array(m.group(1).split(), np.float64).reshape(-1, 3)
    return v.min(0), v.max(0)


def materials_known():
    names = set()
    for fn in os.listdir(SHAPES):
        if fn.endswith(".materials.json"):
            with open(os.path.join(SHAPES, fn), encoding="utf-8") as f:
                names |= set(re.findall(r'"mapTo":\s*"([^"]+)"', f.read()))
    return names


class AssetsTest(unittest.TestCase):
    def test_every_group_has_a_shape_a_catalog_entry_and_known_materials(self):
        with open(TREES, encoding="utf-8") as f:
            lua = f.read()
        known = materials_known()
        for g in GROUPS:
            text = dae(g)
            self.assertIn(f"/bne_{g}'", lua, g)
            for m in re.findall(r'<material id="([^"]+)-material"', text):
                self.assertIn(m, known, f"{g}: material {m}")

    def test_a_pole_has_a_collider_and_its_box_holds_it(self):
        for g in POLES:
            h, boxes = poleprofiles.COLLISION_BOXES[g]
            self.assertEqual(h, 9.0)
            lo, hi = colmesh_bounds(dae(g))
            b = boxes[0]
            self.assertTrue(b[0] <= lo[0] and b[1] <= lo[1] and b[3] >= hi[0] and b[4] >= hi[1], f"{g}: {lo} {hi} in {b}")
            self.assertAlmostEqual(hi[2], 9.0, delta=0.05)
            self.assertIn(g, furniture.COLLISION_BOXES)

    def test_lamps_and_rosettes_have_no_collider(self):
        with open(TREES, encoding="utf-8") as f:
            lua = f.read()
        nc = re.search(r"M\.NO_COLLIDE = \{(.*?)\n", lua).group(1)
        for g in LAMPS + ("tram_rosette",):
            self.assertIn(f"{g} = true", nc)
            self.assertNotIn("Colmesh", dae(g))

    def test_the_height_of_each_pole_is_the_models(self):
        for g in POLES:
            text = dae(g)
            zs = [float(z) for z in re.findall(r'<float_array id="[^"]*64-mesh-positions-array"[^>]*>([^<]*)<', text)[0].split()[2::3]]
            self.assertAlmostEqual(max(zs), 9.0, delta=0.06, msg=g)


class LampTest(unittest.TestCase):
    def test_a_bracket_gets_its_lens_and_glow_a_t_head_two(self):
        g = ["tram_lamp_g", 10.0, 20.0, 5.0, 90.0, 1.0, 9.0, 0.0]
        t = ["tram_lamp_t", 10.0, 20.0, 5.0, 0.0, 1.0, 9.0, 0.0]
        self.assertEqual(len(furniture.lamp_lenses([g])), 2)
        lt = furniture.lamp_lenses([t])
        self.assertEqual(len(lt), 4)
        ys = sorted({r[2] for r in lt})
        self.assertAlmostEqual(ys[0], 20.0 - 0.85, places=2)
        self.assertAlmostEqual(ys[-1], 20.0 + 0.85, places=2)
        lens = furniture.lamp_lenses([g])[0]      # (yaw 90: the arm along +y)
        self.assertAlmostEqual(lens[1], 10.0, places=2)
        self.assertAlmostEqual(lens[2], 20.0 + 1.55, places=2)
        self.assertAlmostEqual(lens[3], 5.0 + 8.07 - furniture.LENS_DROP_M, places=2)


class WireSplitTest(unittest.TestCase):
    """A dense tile's wires go to a second mesh instead of being dropped at 60 000 vertices (plan 046)."""

    def lines(self, n, segs=40):
        return [(np.array([[i * 3.0, k * 1.0, 6.0] for k in range(segs + 1)]), 0.016) for i in range(n)]

    def test_nothing_is_lost_over_the_limit(self):
        n = 400                                      # 400 lines of 40 segments: 128 000 vertices, two meshes
        parts = furniture.wire_mesh_parts(self.lines(n), (0.0, 0.0, 0.0), None, None)
        self.assertEqual(len(parts), 3)
        self.assertEqual(sum(len(p[0]) for p in parts), 8 * 40 * n)
        for p in parts:
            self.assertLessEqual(len(p[0]), furniture.WIRE_VERTS_MAX)
        self.assertEqual(len(furniture.wire_mesh_parts(self.lines(10), (0.0, 0.0, 0.0), None, None)), 1)
        self.assertEqual(furniture.wire_mesh_parts([], (0.0, 0.0, 0.0), None, None), [])

    def test_materials_are_per_mesh_and_the_old_single_mesh_unchanged_below_the_limit(self):
        import catenary
        lines = self.lines(30) + [(np.array([[0.0, 0.0, 5.0], [10.0, 0.0, 5.0]]), catenary.INS_R)]
        one = furniture.wire_mesh_parts(lines, (0.0, 0.0, 0.0), catenary.wire_material, catenary.wire_solid)
        ref = furniture.wire_mesh(lines, (0.0, 0.0, 0.0), catenary.wire_material, catenary.wire_solid)
        self.assertEqual(len(one), 1)
        np.testing.assert_array_equal(one[0][0], ref[0])
        self.assertEqual(one[0][3], ref[3])

    def test_the_tile_names_a_shape_for_each_part_and_the_mod_loads_them(self):
        import roads
        files = roads.shape_files(0, 1, 2)
        self.assertEqual(files["wire"], "wire_0_1_2.cdae")
        for k in range(2, roads.WIRE_PARTS_MAX + 1):
            self.assertEqual(files[f"wire{k}"], f"wire_0_1_2_{k}.cdae")
        self.assertEqual(roads.WIRE_PARTS_MAX, furniture.WIRE_PARTS_MAX)
        with open(os.path.join(ROOT, "mod", "beamng_earth", "lua", "ge", "extensions", "beamngEarth", "roads.lua"), encoding="utf-8") as f:
            lua = f.read()
        self.assertIn("'wire4'", lua)
        self.assertEqual(lua.count("what == 'wire'"), 1, "only isWire's own definition")
        self.assertNotIn("q.what ~= 'wire'", lua)


@unittest.skipUnless(fast.AVAILABLE and getattr(fast._ef, "VERSION", 0) >= tramcat.KERNEL, "the kernel is older")
class DropLampsTest(unittest.TestCase):
    def test_lamps_within_the_clear_distance_of_a_bracket_pole_go_with_their_lights(self):
        poles = [["tram_lamp_g", 0.0, 0.0, 0.0, 0.0, 1.0, 9.0, 0.0], ["tram_tube_span", 0.0, 0.0, 0.0, 0.0, 1.0, 9.0, 0.0]]
        near, far = 3.0, 8.0
        props = [[furniture.G_LAMP, near, 0.0, 0.0, 0, 1, 9, 0], [furniture.G_LENS, near, 0.1, 7.0, 0, 1, 1, 0],
                 [furniture.G_GLOW, near, 0.1, 7.0, 0, 1, 1, 0], [furniture.G_LAMP, far, 0.0, 0.0, 0, 1, 9, 0],
                 ["bench", 1.0, 0.0, 0.0, 0, 1, 1, 0]]
        got = tramcat.drop_lamps(props, poles)
        self.assertEqual([r[0] for r in got], [furniture.G_LAMP, "bench"])
        self.assertEqual(got[0][1], far)

    def test_no_bracket_no_change(self):
        props = [[furniture.G_LAMP, 1.0, 0.0, 0.0, 0, 1, 9, 0]]
        self.assertIs(tramcat.drop_lamps(props, [["tram_rosette", 0.0, 0.0, 0.0, 0, 1, 1, 0]]), props)


if __name__ == "__main__":
    unittest.main()
