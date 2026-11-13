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

        def warm_place(spot, x, y, radius=None, kind="demand"):
            calls["warm"].append((round(spot.frame.lat0, 5), round(spot.frame.lon0, 5), x, y) +
                                 ((radius, kind) if radius is not None else ()))
            return 7
        class Imagery:
            def render(self, frame, x0, y0, size, px, pending):
                calls.setdefault("photo", []).append(px)
        s = SimpleNamespace(dem=Dem(), mapdata=object(), warm_place=warm_place, imagery=Imagery(),
                            near_visual="detail", layout=SimpleNamespace(block=1024))
        return SimpleNamespace(s=s), calls

    def _wait(self, conn):
        t0 = time.time()
        while conn.s._preload["busy"] and time.time() - t0 < 5:
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

    def test_a_hover_loads_less_and_a_pick_then_the_rest(self):
        conn, calls = self._fake()
        preload = earthd.Connection.preload_place
        preload(conn, {"type": "preload_place", "lat": 40.7116, "lon": -74.0127, "hover": True})
        self._wait(conn)
        self.assertEqual(calls["warm"], [(40.7116, -74.0127, 0.0, 0.0, earthd.PRELOAD_HOVER_M, "prefetch")])
        self.assertNotIn("photo", calls, "a hover: no photos")
        preload(conn, {"lat": 40.7116, "lon": -74.0127, "hover": True})       # (the same hover again: nothing)
        self._wait(conn)
        self.assertEqual(len(calls["warm"]), 1)
        preload(conn, {"lat": 40.7116, "lon": -74.0127})                      # (then picked: the rest)
        self._wait(conn)
        self.assertEqual(calls["warm"][-1], (40.7116, -74.0127, 0.0, 0.0))
        self.assertEqual(calls["photo"], [earthd.QUICK_BASE_PX, earthd.BASE_PX])

    def test_the_level_starts_place_planned_and_dropped_when_the_player_goes_elsewhere(self):
        conn, calls = self._fake()
        s = conn.s
        s._next_anchor, s.abandoned, written = 7, set(), []
        s.write_abandoned = lambda: written.append(sorted(s.abandoned))
        handed = []
        orig = earthd.Server.plan_first
        earthd.Server.plan_first = lambda self, anchor, cx, cy, stop=None: \
            handed.append((anchor.id, anchor.lat, anchor.lon)) or 3
        try:
            earthd.Server.preload(s, 39.2833, -76.5935, "start")
            self._wait(conn)
        finally:
            earthd.Server.plan_first = orig
        self.assertEqual(handed, [(7, 39.2833, -76.5935)], "its first block's cells handed out under an id of its own")
        self.assertEqual(s._next_anchor, 8)
        self.assertEqual(calls["photo"], [earthd.QUICK_BASE_PX, earthd.BASE_PX])
        earthd.Server.preload_cancel(s, 39.2840, -76.5940)      # (the teleport goes there: kept)
        self.assertEqual(s.abandoned, set())
        earthd.Server.preload_cancel(s, 40.7116, -74.0127)      # (elsewhere: its plans dropped)
        self.assertEqual(s.abandoned, {7})
        self.assertEqual(written, [[7]])

    def _planner(self, s, during=None):
        """s set up for preplan; plan_first replaced by a hand-out of 4 jobs that calls during(i) before each and
        stops when its stop() says so. Returns the jobs handed out [(anchor id, i)]."""
        s._next_anchor, s.abandoned = 7, set()
        s.write_abandoned = lambda: None
        s._preload = {"next": None, "busy": True, "last": None, "plans": [], "cancel": False}
        jobs = []

        def plan_first(self, anchor, cx, cy, stop=None):
            for i in range(4):
                if during is not None:
                    during(i)
                if stop is not None and stop():
                    break
                jobs.append((anchor.id, i))
            return len(jobs)
        orig = earthd.Server.plan_first
        earthd.Server.plan_first = plan_first
        self.addCleanup(setattr, earthd.Server, "plan_first", orig)
        return jobs

    def test_a_teleport_elsewhere_in_the_middle_of_the_hand_out(self):
        conn, _calls = self._fake()
        s = conn.s

        def during(i):
            if i == 2:                  # (the event loop's teleport, between two jobs of the preload thread)
                earthd.Server.preload_cancel(s, 40.7116, -74.0127)
        jobs = self._planner(s, during)
        n = earthd.Server.preplan(s, 39.2833, -76.5935)
        self.assertEqual(jobs, [(7, 0), (7, 1)], "no job handed out after the teleport")
        self.assertEqual(n, 2)
        self.assertEqual(s.abandoned, {7}, "the ones handed out dropped: published before the hand-out")
        self.assertEqual(s._preload["plans"], [])

    def test_a_teleport_elsewhere_before_the_hand_out(self):
        conn, _calls = self._fake()
        s = conn.s
        jobs = self._planner(s)
        s._preload["cancel"] = True
        self.assertEqual(earthd.Server.preplan(s, 39.2833, -76.5935), 0)
        self.assertEqual(jobs, [])
        self.assertEqual(s._preload["plans"], [])

    def test_a_preload_of_another_place_in_the_middle_of_the_hand_out(self):
        conn, _calls = self._fake()
        s = conn.s

        def during(i):
            if i == 1:
                s._preload["next"] = (48.86, 2.32, "hover")
        jobs = self._planner(s, during)
        earthd.Server.preplan(s, 39.2833, -76.5935)
        self.assertEqual(jobs, [(7, 0)])
        self.assertEqual(s.abandoned, {7}, "superseded by another place: dropped")
        # ... and of the same place (a pick of it): handed out whole, kept
        conn, _calls = self._fake()
        s = conn.s

        def same(i):
            if i == 1:
                s._preload["next"] = (39.2834, -76.5936, "pick")
        jobs = self._planner(s, same)
        earthd.Server.preplan(s, 39.2833, -76.5935)
        self.assertEqual(len(jobs), 4)
        self.assertEqual(s.abandoned, set())


class AnchorIdTest(unittest.TestCase):
    def test_ids_are_never_shared(self):
        import threading
        s = SimpleNamespace(_next_anchor=1)
        got = []

        def take():
            mine = [earthd.Server.alloc_anchor_id(s) for _ in range(2000)]
            got.extend(mine)
        ts = [threading.Thread(target=take) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(len(got), 16000)
        self.assertEqual(len(set(got)), 16000, "every id once")
        self.assertEqual(s._next_anchor, 16001)


class StartPlaceTest(unittest.TestCase):
    def test_the_level_starts_place(self):
        import json
        import tempfile
        import startplace
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(startplace.start_point(d), "nothing saved yet")
            self.assertIsNone(startplace.start_point(None))
            os.makedirs(os.path.join(d, "settings", "cloud"))
            with open(os.path.join(d, "settings", "beamngearth_places.json"), "w") as f:     # (Linux: lower case)
                json.dump({"places": [{"id": 3, "lat": 39.28, "lon": -76.59, "name": "Fells"}],
                           "recent": [{"lat": 36.6, "lon": -118.17}]}, f)
            self.assertEqual(startplace.start_point(d)[:2], (36.6, -118.17), "the newest recent place")
            with open(os.path.join(d, "settings", "beamngEarth_lastpos.json"), "w") as f:
                json.dump({"lat": 40.7426, "lon": -73.993}, f)
            self.assertEqual(startplace.start_point(d)[:2], (40.7426, -73.993), "where the car was last")
            gs = os.path.join(d, "settings", "cloud", "game-state.json")
            with open(gs, "w") as f:
                json.dump({"levels": {"beamng_earth": {"defaultSpawnPointName": "bne_place_3"}}}, f)
            self.assertEqual(startplace.start_point(d)[:2], (39.28, -76.59), "the level select's place")
            with open(gs, "w") as f:
                json.dump({"levels": {"beamng_earth": {"defaultSpawnPointName": "bne_random"}}}, f)
            self.assertIsNone(startplace.start_point(d), "a random start: unknown here")
            with open(os.path.join(d, "settings", "beamngEarth_lastpos.json"), "w") as f:
                f.write("{broken")
            with open(gs, "w") as f:
                json.dump({"levels": {}}, f)
            self.assertEqual(startplace.start_point(d)[:2], (36.6, -118.17), "a broken file: the next one")
            with open(os.path.join(d, "settings", "beamngEarth_lastpos.json"), "w") as f:
                f.write('{"lat": 40.7426, "lon": -73.993, "pad": "' + "x" * startplace.MAX_BYTES + '"}')
            self.assertEqual(startplace.start_point(d)[:2], (36.6, -118.17), "an oversized file: not read")

    def test_the_start_preload_off_the_event_loop(self):
        import tempfile
        asked = []
        server = SimpleNamespace(preload=lambda la, lo, mode: asked.append((la, lo, mode)))
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(earthd.start_preload(server, d))
            os.makedirs(os.path.join(d, "settings"))
            with open(os.path.join(d, "settings", "beamngEarth_lastpos.json"), "w") as f:
                f.write('{"lat": 40.7426, "lon": -73.993}')
            self.assertEqual(earthd.start_preload(server, d)[:2], (40.7426, -73.993))
        self.assertEqual(asked, [(40.7426, -73.993, "start")])


if __name__ == "__main__":
    unittest.main()
