"""A chain solve made short is short for everyone who builds on it (roads.BridgeProfiles._chain_of / parts_todo): the
solves are kept for the session in `done`; one that read something short (an underwater bore unsolved, a cell not
read, the lidar rule) faulted only the build that made it, and a later build, in another revisit scope, took the same
fallback from `done` without a fault and was kept as complete (the review of 2026-11-13). Now the fault is noted with
the chain's parts and said again at every reuse, also of parts adopted from the store that owns the chain."""
import os
import sys
import threading
import types
import unittest

from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import revisit  # noqa: E402
import roads  # noqa: E402


class _Store(roads.BridgeProfiles):
    """A chain store's stand-in: one part, its solve faulting as told."""

    def __init__(self, fault=None):
        self.lock = threading.RLock()
        self.done, self.trusses, self.piers = {}, {}, {}
        self.fault = fault          # None, "plain" or "lidar"
        self.solves = 0
        self.owner_of = None

    def _load(self, key, pool, ground):
        pool[("f", 0)] = (types.SimpleNamespace(width=7.0), np.array([[0.0, 0.0], [10.0, 0.0]]), 0.0, 0, 2)

    def _resolve(self, start, pool, ground, loaded):
        return [("f", 0)]

    def _solve(self, comp, pool, ground):
        self.solves += 1
        if self.fault == "plain":
            revisit.fault()
        elif self.fault == "lidar":
            revisit.fault(lidar=True)
        return [np.zeros(2) for _ in comp]

    def _commit(self, k, ground=None):
        pass

    def _structure(self, chain):
        return [], []


ENDS = [(40.0, -74.0)]


class _Same:
    """ChainView.mapper's stand-in between two stores of one frame."""

    def pts(self, xyz):
        return np.asarray(xyz, np.float64)

    def pier(self, q):
        return q

    def beam(self, b):
        return b


class ChainShortTest(unittest.TestCase):
    def test_a_short_solve_is_short_again_for_a_later_scope(self):
        for kind, field in (("plain", "n"), ("lidar", "lidar")):
            st = _Store(kind)
            with revisit.scope() as first:
                self.assertIsNotNone(st._chain_of(("f", 0), ENDS))
            self.assertFalse(first.complete)
            self.assertEqual(getattr(first, field), 1, "counted once for the build that made it")
            with revisit.scope() as second:             # (another build, later: the solve comes from `done`)
                self.assertIsNotNone(st._chain_of(("f", 0), ENDS))
            self.assertEqual(st.solves, 1)
            self.assertFalse(second.complete, f"{kind}: a build on a short solve must be short")
            self.assertGreaterEqual(getattr(second, field), 1)
            with revisit.scope() as third:              # (and through parts_todo's own look into `done`)
                st.keys_of = st.parts_of = None
                got = st.parts_todo([(("f", 0), None, 0)], solved_only=True)
            self.assertEqual(len(got), 1)
            self.assertFalse(third.complete)

    def test_a_complete_solve_stays_complete(self):
        st = _Store(None)
        with revisit.scope() as first:
            st._chain_of(("f", 0), ENDS)
        with revisit.scope() as second:
            st._chain_of(("f", 0), ENDS)
        self.assertTrue(first.complete and second.complete)
        self.assertEqual(st.__dict__.get("short", {}), {})

    def test_a_short_solve_adopted_from_its_owner_is_short_here_too(self):
        owner = _Store("plain")
        owner.frame = types.SimpleNamespace()
        with revisit.scope():
            owner._chain_of(("f", 0), ENDS)             # (the owner solved it before, short)
        mine = _Store(None)
        mine.owner_of = lambda me, comp, pool: owner
        mine._ends_ll = lambda xy: ENDS

        mine.frame = owner.frame
        with mock.patch.object(roads.ChainView, "mapper", staticmethod(lambda a, b: _Same())):
            with revisit.scope() as a:
                self.assertIsNotNone(mine._chain_of(("f", 0), ENDS))    # (the real BridgeProfiles._adopt)
        self.assertEqual(mine.solves, 0, "adopted, not solved here")
        self.assertFalse(a.complete, "the owner's short solve replayed into the adopting build")
        self.assertIn(("f", 0), mine.short, "noted with the adopted part")
        with revisit.scope() as b:
            mine._chain_of(("f", 0), ENDS)
        self.assertFalse(b.complete, "short again at its reuse")

    def test_a_reader_in_another_thread_never_takes_a_short_solve_as_complete(self):
        """The part and its note are published under one hold of the lock: a reader that finds the part in `done`
        while the solving thread is still at work (paused here in _structure, right after the parts are published)
        is short too. It read the part as complete before: the note came only after the solve's scope had ended."""
        st = _Store("plain")
        in_structure, go_on = threading.Event(), threading.Event()

        def structure(chain):
            in_structure.set()
            self.assertTrue(go_on.wait(10))
            return [], []
        st._structure = structure
        got = {}

        def writer():
            with revisit.scope() as w:
                st._chain_of(("f", 0), ENDS)
            got["writer"] = w.complete
        th = threading.Thread(target=writer)
        th.start()
        try:
            self.assertTrue(in_structure.wait(10))
            self.assertIn(("f", 0), st.done, "published, the solving thread still in its structure")
            with revisit.scope() as reader:
                self.assertIsNotNone(st._chain_of(("f", 0), ENDS))
            self.assertFalse(reader.complete, "the reader took a short solve: it must be short")
            with revisit.scope() as reader2:
                st.keys_of = st.parts_of = None
                self.assertEqual(len(st.parts_todo([(("f", 0), None, 0)], solved_only=True)), 1)
            self.assertFalse(reader2.complete)
        finally:
            go_on.set()
            th.join(10)
        self.assertIs(got["writer"], False)

    def test_adopted_parts_are_published_with_their_note(self):
        """As above for a chain adopted from its owner: checked from inside the adopting store's lock, the moment
        each part appears in `done`."""
        owner = _Store("lidar")
        owner.frame = types.SimpleNamespace()
        with revisit.scope():
            owner._chain_of(("f", 0), ENDS)
        mine = _Store(None)
        mine.owner_of = lambda me, comp, pool: owner
        mine._ends_ll = lambda xy: ENDS
        mine.frame = owner.frame
        seen = []

        class Watch(dict):
            def __setitem__(w, k, v):
                seen.append((k, k in mine.__dict__.get("short", {})))
                dict.__setitem__(w, k, v)
        mine.done = Watch()
        with mock.patch.object(roads.ChainView, "mapper", staticmethod(lambda a, b: _Same())):
            mine._chain_of(("f", 0), ENDS)
        self.assertEqual(seen, [(("f", 0), True)], "its note was there when the part was published")
        # and the solving store's own publication
        st = _Store("plain")
        seen2 = []

        class Watch2(dict):
            def __setitem__(w, k, v):
                seen2.append((k, k in st.__dict__.get("short", {})))
                dict.__setitem__(w, k, v)
        st.done = Watch2()
        st._chain_of(("f", 0), ENDS)
        self.assertEqual(seen2, [(("f", 0), True)])


if __name__ == "__main__":
    unittest.main()
