"""A destination picked on the world map is loaded before the teleport (earthd preload_place; the user, 2026-11-11:
"sub second world gen, even with clear cashe")."""
import os
import sys
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import earthd  # noqa: E402


class PreloadPlaceTest(unittest.TestCase):
    def _fake(self):
        calls = {"dem": [], "warm": []}

        class Dem:
            def keys_around(self, lat, lon, reach, zoom):
                return [(zoom, int(lat * 100), int(lon * 100))]

            def prefetch(self, keys):
                calls["dem"].append(keys)
                return len(keys), False

        def warm_place(spot, x, y):
            calls["warm"].append((round(spot.frame.lat0, 5), round(spot.frame.lon0, 5), x, y))
            return 7
        class Imagery:
            def render(self, frame, x0, y0, size, px, pending):
                calls.setdefault("photo", []).append(px)
        s = SimpleNamespace(dem=Dem(), mapdata=object(), warm_place=warm_place, imagery=Imagery(),
                            near_visual="detail", layout=SimpleNamespace(block=1024))
        return SimpleNamespace(s=s), calls

    def _wait(self, conn):
        t0 = time.time()
        while conn._preload["busy"] and time.time() - t0 < 5:
            time.sleep(0.01)

    def test_fetches_the_place_in_the_background(self):
        conn, calls = self._fake()
        preload = earthd.Connection.preload_place
        preload(conn, {"type": "preload_place", "lat": 40.7116, "lon": -74.0127})
        self._wait(conn)
        self.assertEqual(len(calls["dem"]), 1)
        self.assertEqual(calls["warm"], [(40.7116, -74.0127, 0.0, 0.0)])
        self.assertEqual(calls["photo"], [earthd.QUICK_BASE_PX, earthd.BASE_PX], "the stand-in's and the block's photo")
        preload(conn, {"lat": 40.71161, "lon": -74.01271})      # (the same spot again: nothing new)
        self._wait(conn)
        self.assertEqual(len(calls["warm"]), 1)
        preload(conn, {"lat": "x", "lon": 3})                   # (junk: ignored)
        preload(conn, {"lat": 95.0, "lon": 3})
        self._wait(conn)
        self.assertEqual(len(calls["warm"]), 1)
        preload(conn, {"lat": 48.86, "lon": 362.32})            # (a wrapped longitude)
        self._wait(conn)
        self.assertEqual(calls["warm"][-1][:2], (48.86, 2.32))


if __name__ == "__main__":
    unittest.main()
