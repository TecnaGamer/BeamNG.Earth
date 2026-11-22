"""tools/m4_probes.py and probes/v14/m4_probes.lua offline (docs/V1_4_SPEC.md §3.5 M4, §7: P2, P7,
P11): the preconditions, the 512² crop of the live heightmap (the same lattice samples as the 1024²
block, so the same heights), maxHeight from the block's own samples, the Lua call quoting, and the
pass criteria applied to the probe chunk's answers under luajit with a fake engine
(mod/tests/test_m4_probes.lua --json). Nothing here talks to the game."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "tools"))
import heightmap  # noqa: E402
import m4_probes  # noqa: E402

LUAJIT = shutil.which("luajit")


def info(**kw):
    i = {"mod": True, "terrain": True, "blockSize": 1024, "anchorId": 3, "session": "s1",
         "block": {"cx": 512.0, "cy": -256.0}, "car": {"x": 600.0, "y": -200.0, "z": 1200.0, "speed": 0.1},
         "tb": {"x": 512.0 - 1024, "y": -256.0 - 1024, "z": 1088.0, "render": False, "heightScaleUser": 1024.0}}
    i.update(kw)
    return i


class PreparationTest(unittest.TestCase):
    def test_check_info(self):
        self.assertEqual(m4_probes.check_info(info()), [])
        self.assertTrue(m4_probes.check_info(info(mod=False)))
        self.assertTrue(m4_probes.check_info(info(blockSize=512)))
        self.assertIn("moves", " ".join(m4_probes.check_info(info(car={"x": 600, "y": -200, "z": 0, "speed": 3}))))
        far = m4_probes.check_info(info(car={"x": 900.0, "y": -256.0, "z": 0, "speed": 0}))
        self.assertIn("from the block centre", " ".join(far))

    def test_crop_is_the_same_lattice(self):
        """The 512² crop centred at the same (cx, cy) holds exactly the 1024² block's samples at the
        same world points (heightmap.sample_coords), in the server's PNG orientation (row 0 = north)."""
        rng = np.random.default_rng(5)
        q16 = rng.integers(0, 65535, (1024, 1024), dtype=np.uint16)          # PNG order
        with tempfile.TemporaryDirectory() as tmp:
            path, layer = m4_probes.write_crop(q16, tmp, 512.0, -256.0)
            crop = m4_probes.read_q16(path)
            with Image.open(layer) as im:
                self.assertEqual((im.size, im.mode), ((512, 512), "RGB"))
            with Image.open(path) as im:
                self.assertEqual((im.size, im.mode), ((512, 512), "I;16"))
        self.assertEqual(crop.dtype, np.uint16)
        big, small = q16[::-1], crop[::-1]                  # [gy, gx]
        xs1, ys1 = heightmap.sample_coords(512.0, -256.0)
        xs2, ys2 = heightmap.sample_coords(512.0, -256.0, 512)
        gx0, gy0 = int(np.searchsorted(xs1, xs2[0])), int(np.searchsorted(ys1, ys2[0]))
        self.assertTrue((xs1[gx0:gx0 + 512] == xs2).all() and (ys1[gy0:gy0 + 512] == ys2).all())
        self.assertTrue((big[gy0:gy0 + 512, gx0:gx0 + 512] == small).all())
        with self.assertRaises(SystemExit):
            m4_probes.write_crop(q16[:512, :512], tempfile.gettempdir(), 0, 0)

    def test_solve_max_height(self):
        q16 = np.zeros((1024, 1024), np.uint16)
        z, maxh = 1088.0, 1024.0                            # q = 1/64
        samples = []
        for gx, gy, q in ((1, 1, 1000), (512, 512, 30000), (1022, 7, 65000)):
            q16[1023 - gy, gx] = q
            samples.append({"gx": gx, "gy": gy, "h": z + q * maxh / 65536})
        i = info(samples=samples)
        self.assertEqual(m4_probes.solve_max_height(q16, i), (1024.0, 1024.0))
        samples[1]["h"] += 0.001                            # within the median: still 1024
        self.assertEqual(m4_probes.solve_max_height(q16, i)[0], 1024.0)
        bad = info(samples=[{"gx": 1, "gy": 1, "h": z + 1000 * 3000.0 / 65536}])
        with self.assertRaises(SystemExit):
            m4_probes.solve_max_height(q16, bad)            # 3000 is not on the ladder

    def test_heightmap_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp, "temp", "beamngEarth", "s1")
            d.mkdir(parents=True)
            (d / "hm_3_512_-256.png").write_bytes(b"x")
            local, vfs = m4_probes.heightmap_path(tmp, info())
            self.assertEqual((local, vfs), (d / "hm_3_512_-256.png", "/temp/beamngEarth/s1/hm_3_512_-256.png"))
            revision = d / "hm_3_512_-256_7.png"
            revision.write_bytes(b"y")
            live = "/temp/beamngEarth/s1/" + revision.name
            self.assertEqual(m4_probes.heightmap_path(tmp, info(heightmap=live)), (revision, live))
            with self.assertRaises(SystemExit):
                m4_probes.heightmap_path(tmp, info(anchorId=4))

    @unittest.skipIf(LUAJIT is None, "luajit not installed")
    def test_call_quoting(self):
        """step() passes the config as a Lua string literal holding JSON."""
        cfg = {"orig": {"path": "/temp/beamngEarth/20261010-1-ab/hm_3_512_-256.png", "maxH": 1024.0}, "cx": 512.0}
        lit = json.dumps(json.dumps(cfg))
        res = subprocess.run([LUAJIT, "-e", f"io.write({lit})"], capture_output=True, text=True, timeout=30)
        self.assertEqual(json.loads(res.stdout), cfg)


@unittest.skipIf(LUAJIT is None, "luajit not installed")
class CriteriaOnFakeEngineTest(unittest.TestCase):
    """The probe chunk's answers under the fake engine of mod/tests/test_m4_probes.lua (imports
    1 ms; collision only at reloadCollision; only the first registered block collides) through the
    tool's pass criteria."""

    @classmethod
    def setUpClass(cls):
        res = subprocess.run([LUAJIT, "test_m4_probes.lua", "--json"], capture_output=True, text=True, timeout=60,
                             cwd=str(ROOT / "mod" / "tests"))
        if res.returncode != 0:
            raise AssertionError(res.stdout + res.stderr)
        cls.out = json.loads(res.stdout)

    def test_p2(self):
        r = m4_probes.evaluate_p2(self.out["p2a"], self.out["p2b"])
        self.assertTrue(r["pass"], r)
        self.assertTrue(r["gate"] and r["extent512"] and r["extent512_same_frame"] and r["heights512"])
        self.assertTrue(r["extent1024_back"] and r["restored"] and r["render_survives"])
        # a 31 ms steady import fails the gate
        b = json.loads(json.dumps(self.out["p2b"]))
        for s in b["steady512Layer"]:
            s["importMs"] = 31.0
        self.assertFalse(m4_probes.evaluate_p2(self.out["p2a"], b)["gate"])
        # a collision extent that does not shrink fails
        a = json.loads(json.dumps(self.out["p2a"]))
        for ray in b["laterFrame"]:
            ray["h"] = 1200.0
        self.assertFalse(m4_probes.evaluate_p2(a, b)["pass"])

    def test_p11(self):
        o = self.out
        r = m4_probes.evaluate_p11(o["p11a"], o["p11samples"], o["p11b"], o["p11bSamples"])
        # the fake engine never follows without a reloadCollision
        self.assertEqual((r["lift_same_frame"], r["lift_after_ms"], r["shrink_same_frame"], r["shrink_after_ms"]),
                         (False, None, False, None))
        self.assertFalse(r["reload_can_go"])
        self.assertIn("keep reloadCollision", r["verdict"])
        # an engine that follows at once
        a = dict(o["p11a"], raySameFrame=o["p11a"]["rayBefore"] + 0.3)
        b = json.loads(json.dumps(o["p11b"]))
        for ray in b["sameFrame"]:
            if ray["dx"] or ray["dy"]:
                ray["h"] = False
        self.assertTrue(m4_probes.evaluate_p11(a, o["p11samples"], b, o["p11bSamples"])["reload_can_go"])

    def test_p7a(self):
        r = m4_probes.evaluate_p7a(self.out["p7aRead"], base_ms=0.0)
        self.assertEqual(len(r["rebases"]), 1)
        rb = r["rebases"][0]
        self.assertEqual((rb["phase1_frame"], rb["phase2_frame"]), (2, 3))
        self.assertEqual(set(rb["parts"]), {"moveVehicles", "load", "transform", "switchAnchor", "shift",
                                            "reloadCollision"})
        self.assertEqual(len(r["commits"]), 1)
        self.assertEqual(set(r["commits"][0]["parts"]), {"load", "reloadCollision", "onBlockCommit"})
        self.assertIsNotNone(rb["frame_ms"])

    def test_p7b(self):
        r = m4_probes.evaluate_p7b(self.out["p7b"])
        self.assertTrue(r["b_silent_before_reload"] and r["b_silent_after_reload"] and r["b_collides_after_swap"], r)
        self.assertTrue(r["restored"] and r["pass"], r)
        slow = dict(self.out["p7b"], swapMs=12.0)
        self.assertFalse(m4_probes.evaluate_p7b(slow)["pass"])


if __name__ == "__main__":
    unittest.main()
