"""The ground photos' tone paint on several threads (earthfast 652 paintpar.rs): the same bytes as the one-thread
kernel it replaces (roadtone.rs, still behind earthfast.paint_lines_toned), as the per-square Python reference, and
on any number of threads, at a photo large enough for every step to split (test_paintlist's squares are small)."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from test_roads import FRAME  # noqa: E402
from test_paintlist import FakeSurface, photo, scene  # noqa: E402
import fast  # noqa: E402
import paint_reference as ref  # noqa: E402
import paintlist  # noqa: E402
import surface as sf  # noqa: E402

BIG = (-300.0, -300.0, 600.0, 1536)
OFF = (-140.0, -260.0, 420.0, 1200)         # (another crop: the steps' row ranges and windows fall elsewhere)


@unittest.skipUnless(fast._ef is not None and getattr(fast._ef, "VERSION", 0) >= 652, "earthfast 652")
class PaintThreadsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.roads, cls.lots, cls.pads = scene()
        cls.surf = FakeSurface([cls.roads], cls.lots, cls.pads)

    def setUp(self):
        paintlist.clear()

    def tearDown(self):
        fast._ef.set_native_threads(0)

    def test_same_bytes_on_any_number_of_threads(self):
        for k, (x0, y0, size, px) in enumerate((BIG, OFF)):
            rgb = photo(px, 20 + k)
            for simple in (False, True):
                fast._ef.set_native_threads(1)
                want = sf.paint_square(rgb, FRAME, x0, y0, size, self.surf, None, simple=simple)
                self.assertGreater(int(np.any(want != rgb, axis=2).sum()), 10000)
                for t in (2, 3, 8):
                    fast._ef.set_native_threads(t)
                    got = sf.paint_square(rgb, FRAME, x0, y0, size, self.surf, None, simple=simple)
                    np.testing.assert_array_equal(got, want, f"square {k} simple {simple} threads {t}")

    def test_large_square_against_the_python_reference(self):
        x0, y0, size, px = OFF
        rgb = photo(px, 31)
        fast._ef.set_native_threads(5)
        want = ref.paint_square(rgb, FRAME, x0, y0, size, self.surf, None)
        got = sf.paint_square(rgb, FRAME, x0, y0, size, self.surf, None)
        np.testing.assert_array_equal(got, want)

    def test_roads_against_the_one_thread_kernel(self):
        """The carriageways alone (a stand-in's paint) are earthfast.paint_lines_toned's, roadtone.rs's kernel."""
        for k, (x0, y0, size, px) in enumerate((BIG, OFF)):
            rgb = photo(px, 40 + k)
            segs, hws, kind = sf._road_segments(FRAME, x0, y0, size, px, px, [self.roads], self.surf.urban)
            colors = np.array([sf.PHOTO_ROAD_RGBA[n] for n in sf.PHOTO_ROAD_RGBA], np.float32)
            tone = np.array([sf.PHOTO_ROAD_TONE[n] for n in sf.PHOTO_ROAD_RGBA], np.float32)
            radius = max(1, int(round(sf.PHOTO_TONE_RADIUS_M * px / size)))
            want = fast._ef.paint_lines_toned(rgb, np.ascontiguousarray(segs, np.float64),
                                              np.ascontiguousarray(hws, np.float64),
                                              np.ascontiguousarray(kind, np.uint8), colors, tone, radius,
                                              sf.PHOTO_TONE_SAMPLE_LUMA, sf.PHOTO_TONE_SAMPLE_SAT, sf.PHOTO_TONE_GREEN)
            roads_only = FakeSurface([self.roads], [], [])
            for t in (1, 4):
                fast._ef.set_native_threads(t)
                got = sf.paint_square(rgb, FRAME, x0, y0, size, roads_only, None, simple=True)
                np.testing.assert_array_equal(got, want, f"square {k} threads {t}")

    def test_step_times_are_counted(self):
        fast._ef.paint_step_ms(True)
        x0, y0, size, px = BIG
        sf.paint_square(photo(px, 5), FRAME, x0, y0, size, self.surf, None)
        got = {name: (n, ms) for name, n, ms in fast._ef.paint_step_ms(False)}
        self.assertEqual(set(got), {"fields", "golf", "lots", "pads", "islands", "roads", "corners"})
        self.assertGreater(got["roads"][0], 0)
        self.assertGreater(got["roads"][1], 0.0)


if __name__ == "__main__":
    unittest.main()
