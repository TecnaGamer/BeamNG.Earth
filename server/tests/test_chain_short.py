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

        def adopt(own, comp, pool):                     # (BridgeProfiles._adopt's first step and its copy)
            if own._chain_of(min(comp), ENDS, adopt=False) is None:
                return False
            with mine.lock:
                for k in comp:
                    mine.done[k] = own.done[k]
            return True
        mine._adopt = adopt
        with revisit.scope() as a:
            self.assertIsNotNone(mine._chain_of(("f", 0), ENDS))
        self.assertEqual(mine.solves, 0, "adopted, not solved here")
        self.assertFalse(a.complete, "the owner's short solve replayed into the adopting build")
        with revisit.scope() as b:
            mine._chain_of(("f", 0), ENDS)
        self.assertFalse(b.complete, "and noted with the adopted parts: short again at their reuse")

    def test_the_real_adopt_goes_through_the_owners_chain_of(self):
        import inspect
        self.assertIn("owner._chain_of(first", inspect.getsource(roads.BridgeProfiles._adopt))


if __name__ == "__main__":
    unittest.main()
