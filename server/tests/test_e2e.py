"""End-to-end: real server process, real DEM downloads (needs network).

The DEM cache lives in <system temp>/beamng-earth-test-cache so reruns are warm;
everything else goes to a fresh temp dir passed as --user-dir.
"""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

SERVER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import cdae  # noqa: E402
import farfield as ff  # noqa: E402
import heightmap  # noqa: E402
from netclient import Client, FarState, stop_server  # noqa: E402

LAT, LON = 36.5785, -118.2923       # Mt Whitney summit, 4421 m
CACHE = os.path.join(tempfile.gettempdir(), "beamng-earth-test-cache")


class EndToEndTest(unittest.TestCase):
    """The v1.2 behaviour: terrain near-visual mode, imagery off."""
    args = ["--near-visual", "terrain", "--imagery", "off"]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.user = self.tmp.name
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER_DIR / "earthd.py"), "--port", "0",
             "--user-dir", self.user, "--cache-dir", CACHE, "--weather", "off", "--gravity", "off",
             "--update-check", "off"] + self.args,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.addCleanup(stop_server, self.proc)     # a failed start too: no server or workers left behind
        line = self.proc.stdout.readline()
        self.assertIn("listening on 127.0.0.1:", line)
        self.port = int(line.split("127.0.0.1:")[1].split()[0])
        self.c = Client(self.port)

    def tearDown(self):
        self.c.close()
        stop_server(self.proc)
        self.proc.stdout.close()
        self.tmp.cleanup()

    def fs(self, vfs):
        self.assertTrue(vfs.startswith("/temp/beamngEarth/"))
        return os.path.join(self.user, vfs.lstrip("/"))

    def test_session(self):
        c, far = self.c, FarState()
        c.send({"type": "hello", "protocol": 4, "format": 3, "modVersion": "0.4.0"})
        hello, _ = c.until(lambda m: m["type"] == "hello", timeout=10)
        self.assertEqual((hello["protocol"], hello["farFormat"], hello["nearVisual"]), (4, "cdae", "terrain"))
        sid = hello["sessionId"]

        # --- anchor + its first terrain ---
        t0 = time.monotonic()
        c.send({"type": "prepare_anchor", "requestId": 1, "lat": LAT, "lon": LON})
        a1, _ = c.until(lambda m: m["type"] in ("anchor_ready", "anchor_error"), timeout=180)
        self.assertEqual(a1["type"], "anchor_ready", a1)
        t = a1["terrain"]
        aid = a1["anchorId"]
        print(f"\n  anchor_ready in {time.monotonic() - t0:.2f} s: zBase={t['zBase']} "
              f"maxHeight={t['maxHeight']} q=1/{round(1 / t['q'])} groundZ={a1['groundZ']}", file=sys.stderr)
        self.assertEqual(t["heightmap"], f"/temp/beamngEarth/{sid}/hm_{aid}_0_0.png")
        self.assertEqual((t["cx"], t["cy"], t["size"], t["squareSize"]), (0.0, 0.0, 2048, heightmap.SQUARE))   # terrain mode: 2 km
        self.assertEqual(t["maxHeight"], 65536 * t["q"])
        with Image.open(self.fs(t["heightmap"])) as im:
            self.assertEqual(im.size, (2048, 2048))     # terrain mode: its 2 km block at 1 m
            q = np.asarray(im).astype(np.int64)[::-1]       # [gy, gx]
        h = heightmap.heights(q, t["zBase"], t["maxHeight"])
        self.assertGreater(q.min(), 0)
        self.assertLess(q.max(), 65535)
        # Whitney summit ~4420 m; the 1 km box reaches some hundreds of metres down.
        self.assertTrue(4380 < a1["groundZ"] < 4440, a1["groundZ"])
        self.assertTrue(4380 < h.max() < 4440 and 3000 < h.min() < 4200, (h.min(), h.max()))
        self.assertAlmostEqual(h[1024, 1024], a1["groundZ"], delta=0.001)

        # --- commit, pose: the far field streams nearest-first until far_done ---
        c.send({"type": "commit_anchor", "anchorId": aid})
        c.send({"type": "commit_terrain", "anchorId": aid, "cx": 0, "cy": 0})
        c.send({"type": "pose", "anchorId": aid, "x": 0, "y": 0, "z": a1["groundZ"],
                "lat": LAT, "lon": LON, "speed": 0, "vx": 0, "vy": 0})
        t0 = time.monotonic()
        first, _ = c.until(lambda m: m["type"] == "far_tile", timeout=60)
        t_first = time.monotonic() - t0
        done, seen = c.until(lambda m: m["type"] == "far_done", timeout=300)
        far.apply([first] + seen)
        tiles = [first] + [m for m in seen if m["type"] == "far_tile"]
        sizes = [os.path.getsize(self.fs(m["shape"])) for m in tiles]
        l0 = [m for m in tiles if m["level"] == 0]
        print(f"  far field: first tile after {t_first:.2f} s, {done['count']} tiles in "
              f"{time.monotonic() - t0:.2f} s; .cdae {min(sizes) / 1024:.0f}-{max(sizes) / 1024:.0f} KB; "
              f"err median {sorted(m['err'] for m in tiles)[len(tiles) // 2]:.2f} m, "
              f"L0 sink median/max {sorted(m['sink'] for m in l0)[len(l0) // 2]:.2f}/"
              f"{max(m['sink'] for m in l0):.2f} m", file=sys.stderr)
        want, refined = ff.select(0.0, 0.0, [ff.Block(0.0, 0.0).bbox])
        self.assertEqual(far.keys(aid), set(want))
        self.assertEqual(first["level"], 0)
        self.assertLessEqual(max(sizes), 90 * 1024)
        for m in tiles[:20]:
            self.assertEqual(m["shape"], f"/temp/beamngEarth/{sid}/far_{aid}_{m['key']}.cdae")
            self.assertEqual(cdae.read(self.fs(m["shape"]))["materials"][0]["name"], ff.MATERIAL)

        # --- 600 m east: removals and new tiles ---
        t0 = time.monotonic()
        c.send({"type": "pose", "anchorId": aid, "x": 600, "y": 0, "z": 4300, "lat": LAT, "lon": LON,
                "speed": 20, "vx": 20, "vy": 0})
        done, seen = c.until(lambda m: m["type"] == "far_done", timeout=120)
        far.apply(seen)
        adds = [m for m in seen if m["type"] == "far_tile"]
        swaps = [m for m in seen if m["type"] == "far_swap"]
        print(f"  600 m move: {len(adds)} tiles, {len(swaps)} swap groups (replacing "
              f"{sum(len(m['remove']) for m in swaps)} tiles) in {time.monotonic() - t0:.2f} s", file=sys.stderr)
        self.assertTrue(adds and [m for m in seen if m["type"] == "far_swap" and m["remove"]])
        forced = [ff.Block(0.0, 0.0, 2048).bbox, ff.Block(512.0, 0.0, 2048).bbox]      # committed + predicted
        self.assertEqual(far.keys(aid), set(ff.select(600.0, 0.0, forced, refined)[0]))

        # --- terrain-follow ---
        t0 = time.monotonic()
        c.send({"type": "prepare_terrain", "requestId": 2, "anchorId": aid, "cx": 512, "cy": 0})
        tr, _ = c.until(lambda m: m["type"] in ("terrain_ready", "terrain_error"), timeout=120)
        self.assertEqual(tr["type"], "terrain_ready", tr)
        print(f"  terrain_ready in {time.monotonic() - t0:.2f} s", file=sys.stderr)
        self.assertEqual((tr["requestId"], tr["anchorId"], tr["cx"], tr["cy"], tr["size"]), (2, aid, 512.0, 0.0, 2048))
        self.assertEqual((tr["q"], tr["squareSize"]), (t["q"], heightmap.SQUARE))
        self.assertEqual(tr["heightmap"], f"/temp/beamngEarth/{sid}/hm_{aid}_512_0.png")
        with Image.open(self.fs(tr["heightmap"])) as im:
            self.assertEqual(im.size, (2048, 2048))     # terrain mode: its 2 km block at 1 m
            q2 = np.asarray(im).astype(np.int64)[::-1]
        # The overlap with the first block is bit-identical on real data (§1.8).
        h2 = heightmap.heights(q2, tr["zBase"], tr["maxHeight"])
        k = int(512 / heightmap.SQUARE)                 # the 512 m move in samples
        self.assertTrue((h2[:, :-k] == h[:, k:]).all())

    def test_bad_prepare(self):
        self.c.send({"type": "prepare_anchor", "requestId": 7, "lat": 95, "lon": 0})
        m, _ = self.c.until(lambda m: m["type"] == "anchor_error", timeout=10)
        self.assertEqual(m["requestId"], 7)
        # Malformed fields are logged and skipped; the connection stays up.
        self.c.send({"type": "commit_anchor", "anchorId": [1]})
        self.c.send({"type": "pose", "anchorId": {"a": 1}})
        self.c.send({"type": "prepare_terrain", "requestId": 8, "anchorId": 1, "cx": 0, "cy": 0})
        m, _ = self.c.until(lambda m: m["type"] == "terrain_error", timeout=10)
        self.assertEqual(m["requestId"], 8)
        self.c.send({"type": "hello", "protocol": 4, "format": 3, "modVersion": "0.4.0"})
        self.c.until(lambda m: m["type"] == "hello", timeout=10)


if __name__ == "__main__":
    unittest.main()
