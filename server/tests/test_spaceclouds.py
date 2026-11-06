"""The clouds seen from space (spaceclouds.py, 2026-11-06): the shell's reach, the mosaic's zoom and tile budget, and a
whole build from a fake GIBS (white cloud tiles over a blue sea: the opacity map has its clouds, the shell its UVs on the
mosaic). No network."""
import io
import math
import os
import sys
import tempfile
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import cdae  # noqa: E402
import geodesy  # noqa: E402
import spaceclouds  # noqa: E402


class Resp:
    def __init__(self, body):
        self.body = body


class FakeGibs:
    """Tiles whose left half is white cloud and right half dark blue sea."""
    def __init__(self):
        self.asked = []

    def get(self, url, kind="demand", headers=None, lane=None):
        from PIL import Image
        self.asked.append(url)
        a = np.zeros((256, 256, 3), np.uint8)
        a[:, :128] = (240, 240, 242)
        a[:, 128:] = (20, 40, 90)
        b = io.BytesIO()
        Image.fromarray(a).save(b, "JPEG", quality=95)
        return Resp(b.getvalue())


class ReachTest(unittest.TestCase):
    def test_radius_follows_the_camera(self):
        self.assertAlmostEqual(spaceclouds.radius_for(100_000) / 1000, 1129 + 276, delta=5)
        self.assertEqual(spaceclouds.radius_for(5_000_000), spaceclouds.MAX_RADIUS_M)

    def test_zoom_keeps_the_tile_budget(self):
        for lat, alt in ((43.7, 20_000), (43.7, 400_000), (70.0, 400_000), (-0.5, 100_000)):
            z, x0, y0, x1, y1 = spaceclouds.pick_zoom(lat, 7.27, spaceclouds.radius_for(alt))
            n = (math.floor(x1 / 256) - math.floor(x0 / 256) + 1) * (math.floor(y1 / 256) - math.floor(y0 / 256) + 1)
            self.assertLessEqual(n, spaceclouds.MAX_TILES, (lat, alt, z))
            self.assertLessEqual(z, spaceclouds.MAX_ZOOM)
            self.assertLess(x0, x1)
            self.assertLess(y0, y1)


class BuildTest(unittest.TestCase):
    def test_a_whole_shell(self):
        frame = geodesy.EnuFrame(43.70, 7.27)
        net = FakeGibs()
        with tempfile.TemporaryDirectory() as d:
            res = spaceclouds.build(d, "/temp/beamngEarth/s1", frame, 43.70, 7.27, 100_000.0, net)
            self.assertIsNotNone(res)
            self.assertTrue(res["shape"].startswith("/temp/beamngEarth/s1/spaceclouds_"))
            self.assertTrue(res["material"].startswith("bne_spaceclouds_"))
            for k in ("shape", "color", "opacity"):
                self.assertTrue(os.path.exists(os.path.join(d, os.path.basename(res[k]))), k)
            self.assertTrue(all("gibs.earthdata.nasa.gov" in u and "/default/default/" in u for u in net.asked))
            shape = cdae.read(os.path.join(d, os.path.basename(res["shape"])))
            self.assertIsInstance(shape, dict)
            self.assertAlmostEqual(res["radius"], spaceclouds.radius_for(100_000.0))
            self.assertTrue(res["lightsShape"].endswith("_lights.cdae") and res["lightsMaterial"].endswith("_lights"))
            self.assertTrue(os.path.exists(os.path.join(d, os.path.basename(res["lights"]))))
            self.assertTrue(any("VIIRS_Black_Marble" in u for u in net.asked), "the night lights asked for too")

    def test_no_tile_no_shell(self):
        class Dead:
            def get(self, *a, **k):
                raise OSError("offline")
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(spaceclouds.build(d, "/temp/x", geodesy.EnuFrame(0, 0), 0.0, 0.0, 50_000.0, Dead()))


class SunTest(unittest.TestCase):
    def test_subsolar(self):
        import calendar
        lat, lon = spaceclouds.subsolar(calendar.timegm((2026, 6, 21, 12, 0, 0)))
        self.assertAlmostEqual(lat, 23.4, delta=0.3)
        self.assertAlmostEqual(lon, 0.0, delta=1.0)
        lat, lon = spaceclouds.subsolar(calendar.timegm((2026, 12, 21, 18, 0, 0)))
        self.assertAlmostEqual(lat, -23.4, delta=0.3)
        self.assertAlmostEqual(lon, -90.0, delta=1.5)


class GameSunTest(unittest.TestCase):
    def test_subsolar_from_the_sky(self):
        lat, lon = spaceclouds.subsolar_from(40.0, -80.0, 0.0, 90.0)
        self.assertAlmostEqual(lat, 40.0, places=6)          # overhead: right there
        lat, lon = spaceclouds.subsolar_from(0.0, 0.0, 90.0, 0.0)
        self.assertAlmostEqual(lat, 0.0, places=6)           # on the eastern horizon at the equator: 90 degrees east
        self.assertAlmostEqual(lon, 90.0, places=6)


class LayerTest(unittest.TestCase):
    def test_clouds_opaque_sea_clear(self):
        import fast
        rgb = np.zeros((4, 4, 3), np.uint8)
        rgb[:2] = (240, 240, 242)
        rgb[2:] = (20, 40, 90)
        col, op = fast._ef.cloud_layer(rgb)
        op = np.asarray(op)
        self.assertGreater(int(op[0, 0]), 240)
        self.assertEqual(int(op[3, 3]), 0)
        self.assertEqual(np.asarray(col).shape, (4, 4, 3))


if __name__ == "__main__":
    unittest.main()
