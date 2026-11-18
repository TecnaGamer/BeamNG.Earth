"""The lighter ground of the Low and Medium quality presets (GitHub #943; the user, 2026-11-11: "yes use lighter detail
on the lower presets"): BNGE_GROUND_SQUARE=2 makes the server's terrain block the same kilometre in 512² samples 2 m
apart, every rule that follows the block's square with it (heightmap.ground_square). The square is read once at import,
so the 2 m checks run in a child process with the variable set; the 1 m defaults in this one."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.dirname(HERE)
sys.path.insert(0, SERVER)
import heightmap  # noqa: E402

CHILD = r"""
import json, os, sys, tempfile
sys.path.insert(0, sys.argv[1])
import numpy as np
import earthd, farfield, grading, heightmap, revisit, roadmesh, roads, surface
out = {"square": heightmap.SQUARE, "size": heightmap.SIZE, "half": heightmap.HALF, "grid": heightmap.GRID,
       "chunk": heightmap.CHUNK, "valid": heightmap.VALID, "block_default": earthd.BLOCK_DEFAULT,
       "cli_block": earthd.CLI_BLOCK_MESH, "block_sizes": list(earthd.BLOCK_SIZES),
       "terrain_block": farfield.TERRAIN_BLOCK, "layout_block": farfield.Layout(64, None, True).block,
       "follow": earthd.FOLLOW_RULES[farfield.Layout(64, None, True).block * heightmap.SQUARE].grid,
       "grid_m": roadmesh.GRID_M, "follow_step": roadmesh.FOLLOW_STEP_M, "low_hole": roads.LOW_HOLE_STEP,
       "wall_diag": roads.WALL_DIAG_M, "wall_flat": grading.WALL_FLAT_M, "road_edge": surface.ROAD_EDGE_M,
       "verge": surface.VERGE_M, "base_shift": earthd.BASE_SHIFT_M, "code_key": revisit.code_key()}
# a block built on the 2 m lattice: 512² samples over the kilometre, its .ter as big
frame = __import__("geodesy").EnuFrame(39.283, -76.594)
keep = {}
with tempfile.TemporaryDirectory() as d:
    info = heightmap.generate(frame, lambda la, lo: 10.0 + 0 * np.asarray(la), os.path.join(d, "b.png"), 128.0, 0.0,
                              keep=keep)
    idx = np.full(keep["qgrid"].shape, surface.GRASS, np.uint8)
    name, used = surface.write_ter(d, "b.ter", keep["qgrid"], idx)
    out["qgrid"] = list(keep["qgrid"].shape)
    out["ter_bytes"] = os.path.getsize(os.path.join(d, "b.ter"))
print(json.dumps(out))
"""


def child(env_square):
    env = dict(os.environ)
    env.pop("BNGE_GROUND_SQUARE", None)
    if env_square is not None:
        env["BNGE_GROUND_SQUARE"] = env_square
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(CHILD)
    try:
        got = subprocess.run([sys.executable, f.name, SERVER], env=env, capture_output=True, text=True, timeout=300)
    finally:
        os.remove(f.name)
    if got.returncode:
        raise AssertionError(got.stderr[-3000:])
    return json.loads(got.stdout.strip().splitlines()[-1])


class GroundSquareTest(unittest.TestCase):
    def test_setting(self):
        self.assertEqual(heightmap.ground_square({}), 1.0)
        self.assertEqual(heightmap.ground_square({"BNGE_GROUND_SQUARE": "2"}), 2.0)
        for v in ("3", "0.5", "x", " "):
            with self.subTest(v=v):
                self.assertEqual(heightmap.ground_square({"BNGE_GROUND_SQUARE": v}), 1.0, "only 1 or 2")

    def test_presets_agree_with_the_mod(self):
        # cpupin.LIGHT_GROUND_PRESETS (a settings file from before groundGrid existed) = the mod's presets with '2'
        import re
        import cpupin
        lua = open(os.path.join(SERVER, "..", "mod", "beamng_earth", "lua", "ge", "extensions", "beamngEarth",
                                "settings.lua"), encoding="utf-8").read()
        block = lua[lua.index("M.PRESETS = {"):lua.index("M.PRESET_ORDER")]
        grids = {}
        for name, body in re.findall(r"^  (\w+) = \{(.*?)\},$", block, re.S | re.M):
            m = re.search(r"groundGrid = '(\d)'", body)
            grids[name] = m.group(1) if m else None
        self.assertEqual(set(grids), {"low", "medium", "high", "ultra", "extreme"})
        self.assertEqual({k for k, v in grids.items() if v == "2"}, set(cpupin.LIGHT_GROUND_PRESETS))
        self.assertTrue(all(v in ("1", "2") for v in grids.values()))

    def test_two_metre_block(self):
        one, two = child(None), child("2")
        self.assertEqual((one["square"], one["size"], one["chunk"], one["block_default"]), (1.0, 1024, 128, 1024))
        self.assertEqual((two["square"], two["size"], two["chunk"], two["block_default"], two["cli_block"]),
                         (2.0, 512, 64, 512, 512))
        for k in ("half", "grid", "follow"):
            self.assertEqual(one[k], two[k], f"{k}: the same kilometre on the same 128 m grid")
        self.assertEqual(two["valid"], 1022.0)
        self.assertEqual(two["block_sizes"], [512, 1024])
        self.assertEqual((two["terrain_block"], two["layout_block"]), (1024, 512))
        # the rules that follow the lattice's square (the 2 m values they had before 2026-11-01)
        self.assertEqual((two["grid_m"], two["low_hole"], two["wall_flat"], two["road_edge"], two["verge"]),
                         (2.0, 2.5, 2.0, 1.0, 2.0))
        self.assertAlmostEqual(two["follow_step"], 0.9)
        self.assertAlmostEqual(two["wall_diag"], 2.9)
        self.assertEqual(two["base_shift"], 1.0)
        self.assertEqual((one["grid_m"], one["low_hole"], one["wall_flat"], one["road_edge"], one["verge"]),
                         (1.0, 1.5, 1.0, 0.5, 1.5))
        # the road tiles drape on the lattice: their cached copies are kept apart (revisit.code_key)
        self.assertNotEqual(one["code_key"], two["code_key"])
        self.assertEqual(two["qgrid"], [512, 512])
        self.assertEqual(one["qgrid"], [1024, 1024])
        self.assertLess(two["ter_bytes"], one["ter_bytes"] / 3.9, "a quarter of the .ter to parse at each swap")


if __name__ == "__main__":
    unittest.main()
