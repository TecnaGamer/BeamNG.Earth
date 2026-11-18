"""Places the player is likely to go to, loaded ahead (preload.py; earthd preload_place; the user, 2026-11-11: "sub second
world gen, even with clear cashe"): hover / pick / start, all of the preloader's state on the event loop; the scenarios of
the reviews of bc332f34 driven step by step (a manual executor: each blocking part ends when the test says so)."""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import earthd  # noqa: E402
import preload  # noqa: E402


def dev_fake():
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


class DevPreloadTest(unittest.TestCase):
    """BNE_PRELOAD=off: a pick's preload as before the redesign, a hover nothing."""

    def setUp(self):
        self._was = earthd.PRELOAD
        earthd.PRELOAD = False
        self.addCleanup(setattr, earthd, "PRELOAD", self._was)

    def test_a_pick_as_before_a_hover_nothing(self):
        conn, calls = dev_fake()
        preload_place = earthd.Connection.preload_place
        preload_place(conn, {"type": "preload_place", "lat": 40.7116, "lon": -74.0127, "hover": True})
        self.assertFalse(hasattr(conn, "_preload"), "a hover: nothing")
        preload_place(conn, {"type": "preload_place", "lat": 40.7116, "lon": -74.0127})
        t0 = time.time()
        while conn._preload["busy"] and time.time() - t0 < 5:
            time.sleep(0.01)
        self.assertEqual(len(calls["dem"]), 1)
        self.assertEqual(calls["warm"], [(40.7116, -74.0127, 0.0, 0.0)])
        self.assertEqual(calls["photo"], [earthd.QUICK_BASE_PX, earthd.BASE_PX])
        preload_place(conn, {"lat": 40.71161, "lon": -74.01271})      # (the same spot again: nothing new)
        while conn._preload["busy"] and time.time() - t0 < 5:
            time.sleep(0.01)
        self.assertEqual(len(calls["warm"]), 1)


class ManualExecutor:
    """An executor whose jobs run only when the test says (step): the interleavings are the test's."""

    def __init__(self):
        self.jobs = []          # (fn name, fn, args, concurrent future)

    def submit(self, fn, *args):
        import concurrent.futures
        f = concurrent.futures.Future()
        self.jobs.append((getattr(fn, "__name__", str(fn)), fn, args, f))
        return f

    def step(self):
        """Runs the oldest waiting job; returns its name."""
        name, fn, args, f = self.jobs.pop(0)
        try:
            f.set_result(fn(*args))
        except Exception as e:
            f.set_exception(e)
        return name

    def shutdown(self, wait=True):
        pass


class FakeServer:
    def __init__(self):
        self.calls = []
        self.next_aid = 7
        self.abandoned = set()
        self.dem = SimpleNamespace(keys_around=lambda la, lo, r, z: [(z, la, lo)], prefetch=self._dem)
        self.mapdata = object()
        self.imagery = SimpleNamespace(render=self._render)
        self.near_visual = "detail"

    def _dem(self, keys):
        self.calls.append(("dem", keys[0][1]))
        return len(keys), False

    def _render(self, frame, x0, y0, size, px, pending):
        self.calls.append(("photo", px))

    def decisions_prefetch(self, lat, lon):
        self.calls.append(("decisions", lat))

    def warm_place(self, spot, x, y, radius=None, kind="demand"):
        self.calls.append(("cells", round(spot.frame.lat0, 4), radius, kind))
        return 5

    def alloc_anchor_id(self):
        self.next_aid += 1
        return self.next_aid - 1

    def plan_first(self, anchor, cx, cy):
        self.calls.append(("plan", anchor.id, anchor.lat))
        return 3

    def set_abandoned(self, add=(), remove=()):
        self.abandoned.update(add)
        self.abandoned.difference_update(remove)


FELLS = (39.2833, -76.5935)
NYC = (40.7116, -74.0127)


class PreloaderTest(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.close_loop)
        self.ex = ManualExecutor()
        self.s = FakeServer()
        self.pl = self.make(self.s)

    def close_loop(self):
        pending = [t for t in asyncio.all_tasks(self.loop) if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self.loop.close()

    def make(self, s, ex=None):
        return preload.Preloader(s, executor=ex or self.ex, sharing=lambda: True, render_px=(512, 2048),
                                 dem_zoom=lambda la: 15, dem_reach=600.0)

    def spin(self, n=6):
        """The loop runs its ready callbacks (a job's coroutine up to its next part)."""
        async def go():
            for _ in range(n):
                await asyncio.sleep(0)
        self.loop.run_until_complete(go())

    def on_loop(self, fn, *a):
        async def go():
            return fn(*a)
        return self.loop.run_until_complete(go())

    def run_all(self, ex=None):
        ex = ex or self.ex
        while True:
            self.spin()
            if not ex.jobs:
                break
            ex.step()
        self.spin()

    def test_hover_then_pick_then_the_same_again(self):
        self.on_loop(self.pl.request, *NYC, "hover")
        self.run_all()
        self.assertEqual([c[0] for c in self.s.calls], ["decisions", "dem", "cells"])
        self.assertEqual(self.s.calls[-1][2:], (preload.HOVER_M, "prefetch"), "a hover: the near cells, prefetch")
        self.on_loop(self.pl.request, *NYC, "hover")
        self.run_all()
        self.assertEqual(len(self.s.calls), 3, "the same hover again: nothing")
        self.on_loop(self.pl.request, *NYC, "pick")
        self.run_all()
        self.assertEqual([c[0] for c in self.s.calls[3:]], ["decisions", "dem", "cells", "photo", "photo"])
        self.assertEqual(self.s.calls[5][2:], (None, "demand"))

    def test_start_plans_recorded_and_handed_out_on_the_loop(self):
        self.on_loop(self.pl.request, *FELLS, "start")
        self.spin()
        self.assertEqual(self.ex.step(), "decisions_prefetch")
        self.spin()
        self.assertIn(("plan", 7, FELLS[0]), self.s.calls, "handed out on the loop, after the decisions")
        self.assertEqual(self.pl.plans, [(7, FELLS)])
        self.run_all()
        self.on_loop(self.pl.teleport, FELLS[0] + 0.001, FELLS[1])      # (the teleport goes there: kept)
        self.assertEqual(self.s.abandoned, set())
        self.on_loop(self.pl.teleport, *NYC)                             # (elsewhere later: dropped)
        self.assertEqual(self.s.abandoned, {7})
        self.assertEqual(self.pl.plans, [])

    # --- the first scenario: a teleport between the job taking its place and recording what it does -----------
    def test_a_teleport_before_the_job_starts_or_between_its_parts(self):
        def both():                                     # (the teleport before the job's coroutine ran at all)
            self.pl.request(*FELLS, "start")
            self.pl.teleport(*NYC)
        self.on_loop(both)
        self.run_all()
        self.assertEqual(self.s.calls, [], "nothing done, nothing planned")
        self.on_loop(self.pl.request, *FELLS, "start")
        self.spin()
        self.assertEqual(self.ex.jobs[0][0], "decisions_prefetch")
        self.on_loop(self.pl.teleport, *NYC)            # (while its first part runs on the thread)
        self.run_all()
        self.assertEqual([c[0] for c in self.s.calls], ["decisions"], "the part that ran, then nothing")
        self.assertEqual(self.pl.plans, [], "no plans handed out after the teleport")
        self.assertEqual(self.s.abandoned, set())

    def test_a_teleport_after_the_hand_out_abandons_the_plans(self):
        self.on_loop(self.pl.request, *FELLS, "start")
        self.spin()
        self.ex.step()                                   # (decisions; then the plans, on the loop)
        self.spin()
        self.assertEqual(self.pl.plans, [(7, FELLS)])
        self.on_loop(self.pl.teleport, *NYC)            # (while the elevation part runs)
        self.run_all()
        self.assertEqual(self.s.abandoned, {7})
        self.assertNotIn("cells", [c[0] for c in self.s.calls], "stopped after the part that was running")

    def test_a_preload_of_another_place_supersedes_and_drops_the_plans(self):
        self.on_loop(self.pl.request, *FELLS, "start")
        self.spin()
        self.ex.step()
        self.spin()
        self.on_loop(self.pl.request, *NYC, "hover")
        self.assertEqual(self.s.abandoned, {7}, "the plans of a place 300 km away dropped at once")
        self.run_all()
        self.assertEqual(self.s.calls[-1][:2], ("cells", round(NYC[0], 4)), "then the new place's")
        # a preload of the same place (a pick of it) keeps them
        ex2, s2 = ManualExecutor(), FakeServer()
        pl2 = self.make(s2, ex2)
        self.on_loop(pl2.request, *FELLS, "start")
        self.spin()
        ex2.step()
        self.spin()
        self.on_loop(pl2.request, FELLS[0] + 0.0005, FELLS[1], "pick")
        self.run_all(ex2)
        self.assertEqual(s2.abandoned, set())
        self.assertEqual(pl2.plans, [(7, FELLS)])

    # --- the third: the start place read after a teleport or a shutdown ----------------------------------------
    def test_the_start_place_dropped_when_a_teleport_or_close_comes_first(self):
        def read(_d):
            return FELLS[0], FELLS[1], "test"
        for late in ("teleport", "close", None):
            ex, s = ManualExecutor(), FakeServer()
            pl = self.make(s, ex)
            task = self.loop.create_task(pl.start_place("/user", read))
            self.spin()
            self.assertEqual(ex.jobs[0][0], "read")
            if late == "teleport":
                self.on_loop(pl.teleport, *NYC)
            elif late == "close":
                pl.close()
            ex.step()                                    # (the files read, after it)
            got = self.loop.run_until_complete(task)
            self.spin()
            if late is None:
                self.assertEqual(got[:2], FELLS)
                self.assertEqual(ex.jobs[0][0], "decisions_prefetch", "the start place preloaded")
            else:
                self.assertIsNone(got, late)
                self.assertEqual(ex.jobs, [], late)
                self.assertEqual(s.calls, [], late)

    def test_closed_takes_nothing(self):
        self.pl.close()
        self.assertIsNone(self.on_loop(self.pl.request, *NYC, "pick"))


class AbandonedWriterTest(unittest.TestCase):
    """The second scenario: the teleports' and the preloads' abandonments at once through the one writer, a temporary
    file of each write's own: the file is always whole and ends with every anchor."""

    def test_concurrent_abandonments_keep_the_file_whole(self):
        with tempfile.TemporaryDirectory() as d:
            s = SimpleNamespace(out_dir=d, abandoned=set())
            s.write_abandoned = lambda: earthd.Server.write_abandoned(s)
            path = os.path.join(d, earthd.blockworker.ABANDONED_FILE)
            bad = []
            stop = threading.Event()

            def reader():
                while not stop.is_set():
                    try:
                        with open(path, encoding="utf-8") as f:
                            json.load(f)
                    except FileNotFoundError:
                        pass
                    except ValueError as e:
                        bad.append(e)

            def writer(base):
                for i in range(200):
                    earthd.Server.set_abandoned(s, add=[base + i])
            r = threading.Thread(target=reader)
            r.start()
            ws = [threading.Thread(target=writer, args=(k * 1000,)) for k in range(6)]
            for w in ws:
                w.start()
            for w in ws:
                w.join()
            stop.set()
            r.join()
            self.assertEqual(bad, [], "never a part of the file")
            with open(path, encoding="utf-8") as f:
                self.assertEqual(len(json.load(f)), 1200, "every anchor in the end")
            self.assertEqual([n for n in os.listdir(d) if n.endswith(".tmp")], [], "no temporary file left")
            earthd.Server.set_abandoned(s, remove=[0, 1000])
            with open(path, encoding="utf-8") as f:
                self.assertEqual(len(json.load(f)), 1198)


class AbandonedPublicationTest(unittest.TestCase):
    """Windows: a worker reading the file holds the server's rename off (a sharing violation). The list stays dirty
    and its latest version lands once the reader lets go, every anchor in it; the readers try again too."""

    def server(self, d):
        s = SimpleNamespace(out_dir=d, abandoned=set())
        s.write_abandoned = lambda: earthd.Server.write_abandoned(s)
        return s

    def test_a_held_reader_delays_the_list_never_loses_it(self):
        from unittest import mock
        real = os.replace
        left = {"n": 6}

        def held(src, dst):
            if left["n"] > 0:
                left["n"] -= 1
                raise PermissionError(13, "the file is in use by another process")
            return real(src, dst)
        with tempfile.TemporaryDirectory() as d, mock.patch("earthd.os.replace", side_effect=held):
            s = self.server(d)
            path = os.path.join(d, earthd.blockworker.ABANDONED_FILE)
            self.assertFalse(earthd.Server.set_abandoned(s, add=[1]), "not landed yet")
            self.assertFalse(earthd.Server.set_abandoned(s, add=[2]))     # (meanwhile: the latest list lands)
            t0 = time.time()
            while time.time() - t0 < 5:
                try:
                    with open(path, encoding="utf-8") as f:
                        if json.load(f) == [1, 2]:
                            break
                except (OSError, ValueError):
                    pass
                time.sleep(0.01)
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), [1, 2], "every anchor, once the reader let go")
            self.assertEqual(left["n"], 0)
            while s.__dict__.get("_abandon_retrying") and time.time() - t0 < 5:
                time.sleep(0.01)
            self.assertFalse(s.__dict__.get("_abandon_dirty"))
            self.assertEqual([n for n in os.listdir(d) if n.endswith(".tmp")], [], "no temporary file left")
            self.assertTrue(earthd.Server.set_abandoned(s, add=[3]), "a free file: at once")

    def test_the_workers_read_again_and_keep_the_last_list(self):
        from unittest import mock
        bw = earthd.blockworker
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, bw.ABANDONED_FILE)
            with open(path, "w") as f:
                json.dump([5], f)
            real = open
            left = {"n": 2}

            def flaky(p, *a, **k):
                if p == path and left["n"] > 0:
                    left["n"] -= 1
                    raise PermissionError(13, "in use")
                return real(p, *a, **k)
            with mock.patch("builtins.open", side_effect=flaky):
                self.assertTrue(bw.abandoned(d, 5), "read again after two sharing violations")
            os.utime(path, ns=(time.time_ns(), time.time_ns() + 10 ** 9))   # (changed: read again ...)
            with mock.patch("builtins.open", side_effect=PermissionError(13, "in use")):
                self.assertTrue(bw.abandoned(d, 5), "... unreadable now: the list last read")


class PreloaderPublicationTest(unittest.TestCase):
    def test_plans_tracked_until_their_abandonment_landed(self):
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        s = FakeServer()
        landed = {"ok": False}

        def set_abandoned(add=(), remove=()):
            s.abandoned.update(add)
            return landed["ok"]
        s.set_abandoned = set_abandoned
        pl = preload.Preloader(s, executor=ManualExecutor())
        pl.plans = [(7, FELLS)]
        pl.teleport(*NYC)
        self.assertEqual(pl.plans, [(7, FELLS)], "not landed: still tracked")
        landed["ok"] = True
        pl.teleport(*NYC)
        self.assertEqual(pl.plans, [])


class ShutdownTest(unittest.TestCase):
    def test_shutdown_while_a_part_is_blocked(self):
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        s = FakeServer()
        gate = threading.Event()
        started = threading.Event()

        def stuck(keys):                    # (an elevation download that does not come back)
            started.set()
            gate.wait(10)
            return 1, False
        s.dem = SimpleNamespace(keys_around=lambda la, lo, r, z: [(z, la, lo)], prefetch=stuck)
        pl = preload.Preloader(s, sharing=lambda: True, dem_zoom=lambda la: 15)     # (its real daemon executor)

        async def go():
            pl.request(*FELLS, "start")
            for _ in range(200):
                if started.is_set():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(started.is_set())
            t0 = time.monotonic()
            box["task"] = pl.task
            await pl.shutdown(timeout=0.3)
            return time.monotonic() - t0
        box = {}
        took = loop.run_until_complete(go())
        self.assertLess(took, 2.0, "bounded: the blocked part left to its thread")
        self.assertEqual(s.abandoned, {7}, "its plans abandoned")
        self.assertTrue(pl.ex.thread.daemon, "a daemon thread: never holds the exit")
        self.assertIsNone(pl.request(*NYC, "pick"), "closed")
        gate.set()
        loop.run_until_complete(asyncio.wait([box["task"]], timeout=2))     # (the job sees it is closed and ends)
        self.assertTrue(box["task"].done())
        pl.ex.thread.join(2)
        self.assertFalse(pl.ex.thread.is_alive(), "its thread ends once the part does")
        self.assertNotIn("cells", [c[0] for c in s.calls], "nothing after the blocked part")

    def test_shutdown_waits_for_a_short_part(self):
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        s = FakeServer()
        pl = preload.Preloader(s, dem_zoom=lambda la: 15)

        async def go():
            pl.request(*NYC, "hover")
            await asyncio.sleep(0)
            await pl.shutdown(timeout=5.0)
        loop.run_until_complete(go())
        self.assertIsNone(pl.task)
        self.assertLessEqual(len(s.calls), 1, "stopped after the part that was running")


class ConnectionTest(unittest.TestCase):
    def test_messages_reach_the_preloader(self):
        was = earthd.PRELOAD
        earthd.PRELOAD = True
        self.addCleanup(setattr, earthd, "PRELOAD", was)
        got = []
        pl = SimpleNamespace(request=lambda la, lo, mode: got.append((la, lo, mode)))
        conn = SimpleNamespace(s=SimpleNamespace(preloader=lambda: pl))
        earthd.Connection.preload_place(conn, {"lat": 40.7, "lon": 362.0, "hover": True})
        earthd.Connection.preload_place(conn, {"lat": 40.7, "lon": 2.0})
        earthd.Connection.preload_place(conn, {"lat": "x", "lon": 2.0})
        earthd.Connection.preload_place(conn, {"lat": 95.0, "lon": 2.0})
        self.assertEqual([(round(a, 3), round(b, 3), m) for a, b, m in got], [(40.7, 2.0, "hover"), (40.7, 2.0, "pick")])


class AnchorIdTest(unittest.TestCase):
    def test_ids_are_never_shared(self):
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


if __name__ == "__main__":
    unittest.main()
