"""ChainView with a preferred store (the AI graph's bridge view, blockworker._nav_bridges): the preferred store's
membership check (_has) takes its own lock, a plain threading.Lock as BridgeProfiles makes it; the view must not
hold that lock around it (Codex review 2026-11-13: a nested acquire deadlocked every non-empty lookup)."""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import roads  # noqa: E402


class _Store:
    """A chain store stand-in with BridgeProfiles' real _has and a production (non-reentrant) lock."""
    _has = roads.BridgeProfiles._has

    def __init__(self, done):
        self.lock = threading.Lock()
        self.done = dict(done)
        self.calls = []
        self.region = 0

    def parts_todo(self, todo, **kw):
        self.calls.append((list(todo), kw))
        return []


class _Prefer:
    def __init__(self, store):
        self.st = store

    def store(self, region):
        return self.st


class PreferLockTest(unittest.TestCase):
    def test_preferred_lookup_does_not_deadlock(self):
        pst = _Store({"a": object()})
        own = _Store({})
        view = roads.ChainView.__new__(roads.ChainView)     # (no real store/frame: only _parts runs)
        view.prefer = _Prefer(pst)
        view._todo = lambda cells_feats, box: {0: (own, [("a",), ("b",)])}
        view._map = lambda st, got: list(got)
        out = []
        t = threading.Thread(target=lambda: out.append(view._parts([], None)), daemon=True)
        t.start()
        t.join(5.0)
        self.assertFalse(t.is_alive(), "ChainView._parts deadlocked on the preferred store's lock")
        self.assertEqual(out, [[]])
        self.assertEqual(pst.calls[0][0], [("a",)])           # the solved part from the preferred store
        self.assertEqual(own.calls[0][0], [("b",)])           # the rest from the view's own store
        self.assertFalse(pst.lock.locked())


if __name__ == "__main__":
    unittest.main()
