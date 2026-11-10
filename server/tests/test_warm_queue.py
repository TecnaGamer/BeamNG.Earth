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


if __name__ == "__main__":
    unittest.main()
