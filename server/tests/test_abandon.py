"""Queued work a teleport left behind, or the car drove past, is dropped (pools.result_running's abandon,
Server.left_behind / abandon_old; the user, 2026-11-11: "if the server is still building one city but initiates a
teleport, stop generating the old city" and "use this to improve loading during driving as well")."""
import concurrent.futures
import os
import sys
import threading
import threading as _t
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import earthd  # noqa: E402
import pools  # noqa: E402


class ResultRunningAbandonTest(unittest.TestCase):
    def test_a_queued_job_is_cancelled_when_abandoned(self):
        gate = threading.Event()
        with concurrent.futures.ThreadPoolExecutor(1) as ex:
            busy = ex.submit(gate.wait)                 # (the one worker busy: the next job stays queued)
            queued = ex.submit(lambda: 42)
            with self.assertRaises(pools.Abandoned):
                pools.result_running(queued, None, abandon=lambda: True)
            self.assertTrue(queued.cancelled())
            gate.set()
            busy.result()

    def test_a_running_or_wanted_job_is_waited_for(self):
        with concurrent.futures.ThreadPoolExecutor(1) as ex:
            self.assertEqual(pools.result_running(ex.submit(lambda: 7), None, abandon=lambda: False), 7)


def conn(aid, focus, vel=(0.0, 0.0), **kw):
    return SimpleNamespace(active=aid, focus=focus, vel=vel, view_at=None, map_open=False, map_view=False, foci=(), **kw)


class LeftBehindTest(unittest.TestCase):
    def server(self, *conns):
        return SimpleNamespace(connections=list(conns))

    def test_behind_a_moving_car_far_enough(self):
        s = self.server(conn(1, (0.0, 0.0), (30.0, 0.0)))
        lb = lambda x, y, m=0.0: earthd.Server.left_behind(s, 1, x, y, m)     # noqa: E731
        self.assertTrue(lb(-2500.0, 0.0), "2.5 km behind")
        self.assertFalse(lb(2500.0, 0.0), "2.5 km ahead")
        self.assertFalse(lb(-1500.0, 0.0), "close behind")
        self.assertTrue(lb(9000.0, 0.0), "past LEFT_FAR_M, ahead or not")
        self.assertFalse(lb(-2500.0, 0.0, 1024.0), "a block's half size of margin")

    def test_standing_car_only_far(self):
        s = self.server(conn(1, (0.0, 0.0), (0.5, 0.0)))
        self.assertFalse(earthd.Server.left_behind(s, 1, -3000.0, 0.0))
        self.assertTrue(earthd.Server.left_behind(s, 1, -9000.0, 0.0))

    def test_every_player_and_the_map_view_count(self):
        a, b = conn(1, (0.0, 0.0), (30.0, 0.0)), conn(1, (-2600.0, 0.0), (0.0, 0.0))
        s = self.server(a, b)
        self.assertFalse(earthd.Server.left_behind(s, 1, -2500.0, 0.0), "another player is there")
        s = self.server(conn(1, (0.0, 0.0), (30.0, 0.0)))
        s.connections[0].map_open = True
        self.assertFalse(earthd.Server.left_behind(s, 1, -2500.0, 0.0), "the 3D map open")
        s = self.server(conn(1, (0.0, 0.0), (30.0, 0.0)))
        s.connections[0].foci = ((-2500.0, 0.0, 50.0, False),)
        self.assertFalse(earthd.Server.left_behind(s, 1, -2500.0, 0.0), "a streamed car there")
        self.assertFalse(earthd.Server.left_behind(self.server(), 1, -9000.0, 0.0), "nobody on the anchor")


class AbandonOldTest(unittest.TestCase):
    def test_teleport_drops_then_restores(self):
        s = SimpleNamespace(abandoned=set(), connections=[], warm_cv=_t.Condition(), warm_want=[("g", "k")],
                            write_abandoned=lambda: None)
        c = SimpleNamespace(s=s, active=2)
        s.connections.append(c)
        earthd.Connection.abandon_old(c, 1)
        self.assertEqual(s.abandoned, {1})
        self.assertEqual(s.warm_want, [])
        self.assertTrue(earthd.Server.is_abandoned(s, 1))
        earthd.Connection.unabandon_old(c)
        self.assertEqual(s.abandoned, set())

    def test_another_player_still_there_keeps_it(self):
        s = SimpleNamespace(abandoned=set(), connections=[], warm_cv=_t.Condition(), warm_want=[],
                            write_abandoned=lambda: None)
        c, other = SimpleNamespace(s=s, active=2), SimpleNamespace(active=1)
        s.connections += [c, other]
        earthd.Connection.abandon_old(c, 1)
        self.assertEqual(s.abandoned, set())


class RunningJobGivesUpTest(unittest.TestCase):
    """A running block or road tile of an abandoned anchor gives up at its next road-plan wait or stage."""

    def test_the_server_file_reaches_the_workers(self):
        import tempfile
        import blockworker
        with tempfile.TemporaryDirectory() as d:
            s = SimpleNamespace(abandoned={3}, out_dir=d)
            self.assertFalse(blockworker.abandoned(d, 3), "no file yet")
            earthd.Server.write_abandoned(s)
            self.assertTrue(blockworker.abandoned(d, 3))
            self.assertFalse(blockworker.abandoned(d, 4))
            s.abandoned = set()
            import time as _time
            _time.sleep(0.01)
            earthd.Server.write_abandoned(s)
            os.utime(os.path.join(d, blockworker.ABANDONED_FILE), ns=(1, 1))     # (a new mtime for sure)
            self.assertFalse(blockworker.abandoned(d, 3))

    def test_stage_marks_and_plan_waits_give_up(self):
        import blockworker
        import xproc
        stages, mark = blockworker._stage_clock()
        xproc.set_cancel(lambda: True)
        try:
            with self.assertRaises(pools.Abandoned):
                mark("fuel")
        finally:
            xproc.set_cancel(None)
        mark("items")                       # (no check set: goes on)
        self.assertIn("items", stages)

    def test_a_plan_wait_gives_up(self):
        import tempfile
        import xproc
        with tempfile.TemporaryDirectory() as d:
            lock = os.path.join(d, "cell.lock")
            self.assertTrue(xproc.take(lock, "demand"))         # (another holder: this process, held)
            xproc.set_cancel(lambda: True)
            try:
                with self.assertRaises(pools.Abandoned):
                    xproc.once(lock, lambda: None, 5.0, kind="demand")
            finally:
                xproc.set_cancel(None)
                xproc.release(lock)


if __name__ == "__main__":
    unittest.main()
