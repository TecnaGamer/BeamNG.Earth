"""revisit's build scopes (revisit.scope / begin, task): a fault counts for the builds that read what failed, not for
every build of the process; a fault no scope covers still counts for all (a first visit kept almost nothing while
every fault was the process's, 2026-11-13). Both sides: a build that read something short is never kept."""
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor as StdPool

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import revisit  # noqa: E402
from runtimeprofiler import ThreadPoolExecutor  # noqa: E402


def in_thread(fn):
    t = threading.Thread(target=fn)
    t.start()
    t.join()


class ScopeTest(unittest.TestCase):
    def test_another_builds_fault_does_not_spoil_this_one(self):
        with revisit.scope() as mine:
            def other():
                with revisit.scope() as theirs:
                    revisit.fault()
                    revisit.fault(lidar=True)
                self.assertFalse(theirs.complete)
            in_thread(other)
        self.assertTrue(mine.complete, "another build's fault in its own thread")

    def test_own_faults_and_nested_scopes(self):
        with revisit.scope() as outer:
            with revisit.scope() as inner:
                revisit.fault(lidar=True)
            self.assertFalse(inner.complete)
        self.assertFalse(outer.complete, "what the inner build read, the outer one read too")
        with revisit.scope() as b:
            revisit.fault(here=True)
        self.assertFalse(b.complete)

    def test_a_fault_no_scope_covers_spoils_every_running_build(self):
        with revisit.scope() as b:
            in_thread(revisit.fault)            # (a background thread of its own: anyone may read what it made)
        self.assertFalse(b.complete)
        with revisit.scope() as b:
            in_thread(lambda: revisit.fault(lidar=True))     # (said where it is read: not the process's)
            in_thread(lambda: revisit.fault(here=True))
        self.assertTrue(b.complete)

    def test_a_pool_task_counts_for_its_submitter_and_whoever_takes_its_result(self):
        ex = ThreadPoolExecutor(2)
        try:
            with revisit.scope() as sub:
                ex.submit(revisit.fault).result()
            self.assertFalse(sub.complete, "its helper task read short")
            with revisit.scope() as quiet:
                ex.submit(lambda: None).result()
            self.assertTrue(quiet.complete)
            fut = ex.submit(revisit.fault)      # (submitted outside any build: a shared download)
            fut.result()
            with revisit.scope() as waiter:     # (another build waiting for the same download)
                fut.result()
            self.assertFalse(waiter.complete, "the shared task's failure reached the build that took its result")
            with revisit.scope() as bystander:
                done = threading.Event()
                in_thread(lambda: ex.submit(revisit.fault).add_done_callback(lambda f: done.set()))
                done.wait(5)                    # (a prefetch another thread asked for, nobody waits for)
            self.assertTrue(bystander.complete, "a task's fault with nobody taking its result")
        finally:
            ex.shutdown(wait=True)

    def test_exceptions_reach_takers_too(self):
        ex = ThreadPoolExecutor(1)
        try:
            def boom():
                revisit.fault()
                raise OSError("down")
            fut = ex.submit(boom)
            with revisit.scope() as b:
                self.assertIsInstance(fut.exception(), OSError)
            self.assertFalse(b.complete)
        finally:
            ex.shutdown(wait=True)

    def test_a_plain_pool_thread_is_no_scope(self):
        """A stdlib pool's thread carries nothing: its fault is the process's (conservative)."""
        with StdPool(1) as ex, revisit.scope() as b:
            ex.submit(revisit.fault).result()
        self.assertFalse(b.complete)

    def test_end_pops_its_own_scope(self):
        d = revisit.depth()
        a = revisit.begin()
        b = revisit.begin()
        revisit.end(a)
        revisit.fault()
        revisit.end(b)
        self.assertTrue(a.complete)
        self.assertFalse(b.complete)
        self.assertEqual(revisit.depth(), d)
        c = revisit.begin()                     # (a job that raised before it ended its scope: blockworker.build)
        revisit.unwind(d)
        self.assertEqual(revisit.depth(), d)
        self.assertNotIn(c, revisit._tl.stack)


class ExpiryTest(unittest.TestCase):
    """revisit.expires: a build read an answer that holds only for a time (the roofs' photo "nothing there", a miss
    with its TTL): kept only until then (Build.kept_until, store(until=), load: none after it); never forever."""

    def test_a_build_on_an_expiring_answer_is_kept_until_it_expires(self):
        import time
        with revisit.scope() as b:
            revisit.expires(time.time() + 600)
            revisit.expires(time.time() + 300)
        self.assertFalse(b.complete, "not kept for good")
        keep, until = b.kept_until()
        self.assertTrue(keep)
        self.assertAlmostEqual(until, time.time() + 300, delta=5)
        self.assertIn("expire", b.why_not())
        with revisit.scope() as b2:
            revisit.fault()
            revisit.expires(time.time() + 600)
        self.assertFalse(b2.kept_until()[0])

    def test_through_a_pool_task_and_without_a_scope(self):
        import time
        ex = ThreadPoolExecutor(2)
        try:
            with revisit.scope() as sub:
                ex.submit(revisit.expires, time.time() + 100).result()
            self.assertIsNotNone(sub.until, "its helper task read it")
        finally:
            ex.shutdown()
        with revisit.scope() as b:
            in_thread(lambda: revisit.expires(time.time() + 100))     # (no scope to carry the time: a fault)
        self.assertFalse(b.kept_until()[0])

    def test_an_item_kept_until_is_gone_after(self):
        import time
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            k = revisit.key("roadsg", "expiry-test")
            self.assertFalse(revisit.store(d, "roadsg", k, {"a": b"x"}, {"n": 1}, until=time.time() - 1),
                             "expired already: not kept")
            self.assertTrue(revisit.store(d, "roadsg", k, {"a": b"x"}, {"n": 1}, until=time.time() + 60))
            blobs, extra = revisit.load(d, "roadsg", k)
            self.assertEqual((blobs["a"], extra["n"]), (b"x", 1))
            later = time.time() + 120
            with mock.patch.object(revisit.time, "time", lambda: later):
                self.assertIsNone(revisit.load(d, "roadsg", k), "past its time: built again")
            k2 = revisit.key("roadsg", "no-expiry")
            self.assertTrue(revisit.store(d, "roadsg", k2, {"a": b"y"}, {"n": 2}))
            with mock.patch.object(revisit.time, "time", lambda: later + 1e9):
                self.assertIsNotNone(revisit.load(d, "roadsg", k2))


class ShortPlansTest(unittest.TestCase):
    def test_plans_made_short_are_marked_for_other_processes(self):
        import plancache
        with tempfile.TemporaryDirectory() as d:
            pc = plancache.PlanCache(d, "w1s1")
            key = (14, 4705, 6245)
            self.assertTrue(pc.store(key, [], [], [], [], persist=False))
            self.assertTrue(pc.is_short(key), "made short: the readers are short too")
            self.assertTrue(pc.store(key, [], [], [], [], persist=True))
            self.assertFalse(pc.is_short(key))

    def test_reading_a_short_plan_spoils_the_reader_alone(self):
        import grading
        g = grading._Planner.__new__(grading._Planner)
        g.lock = threading.Lock()
        g.short, g.stage_short = {(14, 1, 2)}, {(14, 3, 4)}
        with revisit.scope() as b:
            g._short_read([(14, 9, 9)])
        self.assertTrue(b.complete)
        for k in ((14, 1, 2), (14, 3, 4)):
            with revisit.scope() as b:
                g._short_read([(14, 9, 9), k])
            self.assertFalse(b.complete)
        with revisit.scope() as b:
            in_thread(lambda: g._short_read([(14, 1, 2)]))      # (another thread's read: not this build's)
        self.assertTrue(b.complete)


class LidarRuleMemoTest(unittest.TestCase):
    def test_a_cell_product_made_on_the_rule_stays_short(self):
        import roads
        from test_roads import features
        f = features([[(0, 0), (100, 0)]], ["residential"])

        def make():
            revisit.fault(lidar=True)
            return ["made"]
        with revisit.scope() as b:
            self.assertEqual(roads._cell_memo(f, "_x", make), ["made"])
        self.assertFalse(b.complete)
        with revisit.scope() as b:              # (the memo read again by a later build)
            self.assertEqual(roads._cell_memo(f, "_x", make), ["made"])
        self.assertFalse(b.complete, "a product made on the lidar rule read later")
        g = features([[(0, 0), (100, 0)]], ["residential"])
        with revisit.scope() as b:
            roads._cell_memo(g, "_x", lambda: ["clean"])
            roads._cell_memo(g, "_x", lambda: ["again"])
        self.assertTrue(b.complete)


if __name__ == "__main__":
    unittest.main()
