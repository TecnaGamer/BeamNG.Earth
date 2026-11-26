"""blockworker._read_warm: the empty first item earthd.main puts (to start the queue's feeder thread before the event
loop runs: the first warm_roads stalled the loop 3.4 s on a loaded machine) is skipped; a real list is planned."""
import os
import queue
import sys
import threading
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import blockworker  # noqa: E402


class _Q:
    """queue.Queue's get / get_nowait, then EOFError (the queue closed: the reader ends)."""

    def __init__(self, items):
        self.items = list(items)

    def get(self):
        if not self.items:
            raise EOFError
        return self.items.pop(0)

    def get_nowait(self):
        raise queue.Empty


class ReadWarmTest(unittest.TestCase):
    def run_reader(self, items):
        asked = []
        st = types.SimpleNamespace(cv=threading.Condition(), want=None,
                                   anchor=lambda aid, la, lo: (asked.append(aid) or "grader", None))
        with mock.patch.object(blockworker, "_s", st):
            blockworker._read_warm(_Q(items))
        return asked, st.want

    def test_empty_first_item_skipped(self):
        asked, want = self.run_reader([(None, 0.0, 0.0, [])])
        self.assertEqual(asked, [])
        self.assertIsNone(want)

    def test_real_list_after_it(self):
        asked, want = self.run_reader([(None, 0.0, 0.0, []), (3, 40.0, -74.0, [(15, 1, 2)])])
        self.assertEqual(asked, [3])
        self.assertEqual(want, [("grader", (15, 1, 2))])

    def test_read_only_mark(self):
        st = types.SimpleNamespace(cv=threading.Condition(), want=None, anchor=lambda aid, la, lo: ("grader", None))
        with mock.patch.object(blockworker, "_s", st):
            blockworker._read_warm(_Q([(3, 40.0, -74.0, [(15, 1, 2)], True)]))
        self.assertTrue(st.warm_read_only)
        with mock.patch.object(blockworker, "_s", st):
            blockworker._read_warm(_Q([(3, 40.0, -74.0, [(15, 1, 2)])]))       # (a list without the mark: planned)
        self.assertFalse(st.warm_read_only)


class _Shared:
    def __init__(self, has=(), busy=()):
        self._has, self._busy = set(has), set(busy)

    def has(self, k):
        return k in self._has

    def busy(self, k):
        return k in self._busy


class _G:
    def __init__(self, shared, planned=()):
        self.shared, self._planned = shared, set(planned)

    def planned(self, k):
        return k in self._planned


class WarmNextTest(unittest.TestCase):
    """blockworker._warm_next: which cell a block process's warm thread takes next."""

    def test_skips_the_cells_another_process_is_planning(self):
        g = _G(_Shared(busy={"a"}), planned={"p"})
        self.assertEqual(blockworker._warm_next([(g, "p"), (g, "a"), (g, "b")], False), (g, "b"))

    def test_reads_what_is_planned_even_read_only(self):
        g = _G(_Shared(has={"b"}))
        self.assertEqual(blockworker._warm_next([(g, "a"), (g, "b")], True), (g, "b"))      # ("a" is not planned here)
        self.assertEqual(blockworker._warm_next([(g, "a"), (g, "b")], False), (g, "a"))

    def test_nothing_left(self):
        g = _G(_Shared(busy={"a"}))
        self.assertIsNone(blockworker._warm_next([(g, "a")], False))
        self.assertIsNone(blockworker._warm_next([(g, "a")], True))

    def test_without_a_plan_cache_as_before(self):
        g = _G(None)
        self.assertEqual(blockworker._warm_next([(g, "a")], True), (g, "a"))



class WarmOwnCancelTest(unittest.TestCase):
    """The plans-ahead thread's cancel is its own list's anchor's (xproc.set_thread_cancel), never the worker
    process's job-wide one: an old anchor's job in the process cancelled the current place's plans ('road plans ahead
    ... pools.Abandoned', 694 times at Popoli), and with no job in the process a left city's plans went on for minutes."""

    def run_loop(self, warm_of, process_check, left):
        import pools
        import xproc
        seen, done = [], threading.Event()

        class G:
            shared = None

            def __init__(self):
                self.ok = set()

            def planned(self, k):
                return k in self.ok

            def warm(self, k):
                try:
                    seen.append(xproc._cancelled())
                    xproc.check_cancel()
                    self.ok.add(k)
                finally:
                    done.set()
        g = G()
        st = types.SimpleNamespace(cv=threading.Condition(), want=[(g, "k")], warm_of=warm_of, warm_read_only=False)
        old = xproc._cancel_proc[0]
        xproc._cancel_proc[0] = process_check
        try:
            with mock.patch.object(blockworker, "_s", st),                     mock.patch.object(blockworker, "abandoned", lambda out_dir, aid: aid in left),                     mock.patch.object(blockworker.log, "exception") as logged:
                threading.Thread(target=blockworker._warm_loop, daemon=True).start()
                self.assertTrue(done.wait(10))
                for _ in range(100):            # (its list trimmed after the warm)
                    with st.cv:
                        if not st.want or g.planned("k"):
                            break
                    threading.Event().wait(0.01)
        finally:
            xproc._cancel_proc[0] = old
        return seen, st, g, logged, pools

    def test_another_anchors_job_does_not_cancel_it(self):
        seen, st, g, logged, _ = self.run_loop((5, "out"), lambda: True, left={4})
        self.assertEqual(seen, [False])
        self.assertTrue(g.planned("k"))
        logged.assert_not_called()

    def test_a_left_anchors_plans_stop_with_no_job_in_the_process(self):
        seen, st, g, logged, _ = self.run_loop((4, "out"), None, left={4})
        self.assertEqual(seen, [True])
        self.assertFalse(g.planned("k"))
        with st.cv:
            self.assertEqual(st.want, [], "its cell dropped from the list")
        logged.assert_not_called()          # (expected: no error logged)

    def test_the_list_carries_its_anchor(self):
        st = types.SimpleNamespace(cv=threading.Condition(), want=None, anchor=lambda aid, la, lo: ("grader", None))
        with mock.patch.object(blockworker, "_s", st):
            blockworker._read_warm(_Q([(5, 1.0, 2.0, ["k"], False, "out")]))
        self.assertEqual(st.warm_of, (5, "out"))


if __name__ == "__main__":
    unittest.main()
