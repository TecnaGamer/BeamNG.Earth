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

    def _precommit(self, k, part, ground=None):
        if self.fault == "commit":      # (a part's piers: the ground under them read short)
            revisit.fault()
        return []

    def _commit(self, k, ground=None, pre=None):
        pass

    def _structure(self, chain):
        if self.fault == "structure":   # (the chain's structure: a cell round it read short)
            revisit.fault()
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
            self.assertGreaterEqual(getattr(first, field), 1, "counted for the build that made it")
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

    def test_an_unfinished_chain_is_not_there_for_a_reader_or_an_adopter(self):
        """A chain is published only when everything it depends on is read: its parts' piers (_precommit) and its
        structure (_structure) can still read something short after the solve. While the solving thread is at either
        of them nothing of the chain is in `done` (a reader does not find it, an adopter cannot take it), and once it
        is published its note has those late faults: a later build that takes it is short. Before, the part was in
        `done` with a note taken before them: a reader in that window took the fallback as complete."""
        for where in ("commit", "structure"):
            st = _Store(where)                      # (the solve itself reads nothing short: only the late step does)
            reached, go_on = threading.Event(), threading.Event()
            real = getattr(st, "_precommit" if where == "commit" else "_structure")

            def paused(*a, _real=real, **kw):
                out = _real(*a, **kw)               # (the fault first, then the pause: the note must still get it)
                reached.set()
                self.assertTrue(go_on.wait(10))
                return out
            setattr(st, "_precommit" if where == "commit" else "_structure", paused)
            got = {}

            def writer():
                with revisit.scope() as w:
                    st._chain_of(("f", 0), ENDS)
                got["writer"] = w.complete
            th = threading.Thread(target=writer)
            th.start()
            try:
                self.assertTrue(reached.wait(10))
                # the writer is still at work: a reader finds nothing to take
                self.assertNotIn(("f", 0), st.done, f"{where}: nothing published before the chain is finished")
                st.keys_of = st.parts_of = None
                with revisit.scope() as reader:
                    self.assertEqual(st.parts_todo([(("f", 0), None, 0)], solved_only=True), [])
                self.assertTrue(reader.complete, "it took nothing, so it is not short for it")
                # ... and an adopter cannot take it either (the owner's parts are not there to copy)
                with st.lock:
                    self.assertEqual([k for k in [("f", 0)] if k in st.done], [])
            finally:
                go_on.set()
                th.join(10)
            self.assertIs(got["writer"], False, f"{where}: the build that made it is short")
            self.assertIn(("f", 0), st.done)
            self.assertIn(("f", 0), st.short, f"{where}: the late fault is in the part's note")
            with revisit.scope() as later:
                self.assertIsNotNone(st._chain_of(("f", 0), ENDS))
            self.assertFalse(later.complete, f"{where}: a build that takes the chain after is short")
            # and an adopter after the writer finished is short too
            mine = _Store(None)
            mine.owner_of = lambda me, comp, pool, _o=st: _o
            mine._ends_ll = lambda xy: ENDS
            st.frame = mine.frame = types.SimpleNamespace()
            with mock.patch.object(roads.ChainView, "mapper", staticmethod(lambda a, b: _Same())):
                with revisit.scope() as adopter:
                    self.assertIsNotNone(mine._chain_of(("f", 0), ENDS))
            self.assertFalse(adopter.complete, f"{where}: adopted with its note")
            self.assertIn(("f", 0), mine.short)

    def test_a_clean_solve_that_loses_to_a_short_publisher_is_short_too(self):
        """Two threads solve the same chain. The clean one is paused in its staging (its structure); meanwhile the
        other, whose solve read something short, publishes the part with its note. The clean solve then finds the part
        published, does not publish its own, and returns the winner's: the fallback. Its build must be short too (it
        returned the winner's part without its note before, and stayed complete)."""
        st = _Store(None)
        real_solve = st._solve

        def solve(comp, pool, ground):
            if threading.current_thread().name == "short":
                revisit.fault()
            return real_solve(comp, pool, ground)
        st._solve = solve
        reached, go_on = threading.Event(), threading.Event()

        def structure(chain):
            if threading.current_thread().name == "clean":
                reached.set()
                self.assertTrue(go_on.wait(10))
            return [], []
        st._structure = structure
        got = {}

        def build(name):
            with revisit.scope() as b:
                got[name + " part"] = st._chain_of(("f", 0), ENDS)
            got[name] = b.complete
        clean = threading.Thread(target=build, args=("clean",), name="clean")
        clean.start()
        try:
            self.assertTrue(reached.wait(10))
            self.assertNotIn(("f", 0), st.done)
            short = threading.Thread(target=build, args=("short",), name="short")
            short.start()
            short.join(10)
            self.assertIn(("f", 0), st.done, "the short solve published while the clean one was staged")
            self.assertIn(("f", 0), st.short)
        finally:
            go_on.set()
            clean.join(10)
        self.assertIs(got["short"], False)
        self.assertIs(got["clean part"], got["short part"], "the clean solve returned the winner's part")
        self.assertIs(got["clean"], False, "... so its build is short too")
        self.assertEqual(st.solves, 2)

    def test_a_clean_adoption_that_loses_to_a_short_local_publisher_is_short_too(self):
        """An adopting thread waits for the chain's owner (a clean solve there). Meanwhile another thread publishes
        the same part LOCALLY from a solve made short. The adoption keeps the local part, and returns it: the
        fallback. Its build must be short (it skipped the existing part without its note before)."""
        owner = _Store(None)
        owner.frame = types.SimpleNamespace()
        mine = _Store(None)
        mine.frame = owner.frame
        mine._ends_ll = lambda xy: ENDS
        waiting, go_on = threading.Event(), threading.Event()
        real_owner_chain = owner._chain_of

        def owner_chain(key, ends, adopt=True):         # (the adopter waits here for its owner)
            waiting.set()
            self.assertTrue(go_on.wait(10))
            return real_owner_chain(key, ends, adopt)
        owner._chain_of = owner_chain
        # the adopter's thread has an owner; the local publisher's solves here
        mine.owner_of = lambda me, comp, pool: owner if threading.current_thread().name == "adopter" else None
        real_solve = mine._solve

        def solve(comp, pool, ground):
            revisit.fault()                             # (the local solve reads something short)
            return real_solve(comp, pool, ground)
        mine._solve = solve
        got = {}

        def build(name):
            with mock.patch.object(roads.ChainView, "mapper", staticmethod(lambda a, b: _Same())):
                with revisit.scope() as b:
                    got[name + " part"] = mine._chain_of(("f", 0), ENDS)
                got[name] = b.complete
        adopter = threading.Thread(target=build, args=("adopter",), name="adopter")
        adopter.start()
        try:
            self.assertTrue(waiting.wait(10))
            local = threading.Thread(target=build, args=("local",), name="local")
            local.start()
            local.join(10)
            self.assertIn(("f", 0), mine.done, "the local solve published while the adopter waited")
        finally:
            go_on.set()
            adopter.join(10)
        self.assertIs(got["local"], False)
        self.assertEqual(owner.solves, 1, "the owner solved it, clean")
        self.assertNotIn(("f", 0), owner.__dict__.get("short", {}))
        self.assertIs(got["adopter part"], got["local part"], "the adoption returned the local part")
        self.assertIs(got["adopter"], False, "... so its build is short too")

    def test_no_part_leaves_done_but_through_take(self):
        """`done` is read for use in one place only (_take_locked); the rest of roads.py writes it or asks whether a
        key is in it (_has)."""
        import inspect
        import re
        src = inspect.getsource(roads)
        reads = [ln.strip() for ln in src.splitlines()
                 if re.search(r"\b\w+\.done(\.get\(|\[[^\]]+\](?!\s*=))", ln) and "fut.done" not in ln]
        self.assertEqual(reads, ["got = self.done.get(key)"], "every use of a part goes through _take")
        self.assertEqual(len(re.findall(r"in \w+\.done\b", src)), 1, "one membership test: _has")

    def test_a_part_kept_from_before_brings_its_note(self):
        """A solve whose chain holds a part published before (kept: this solve saw no more of it) builds on that part:
        its note is this build's."""
        st = _Store("plain")
        with revisit.scope():
            st._chain_of(("f", 0), ENDS)                # (published, short)
        st.fault = None

        def load(key, pool, ground):                    # (the chain now has a second part)
            pool[("f", 0)] = (types.SimpleNamespace(width=7.0), np.array([[0.0, 0.0], [10.0, 0.0]]), 0.0, 0, 2)
            pool[("f", 1)] = (types.SimpleNamespace(width=7.0), np.array([[10.0, 0.0], [20.0, 0.0]]), 0.0, 0, 2)
        st._load = load
        st._resolve = lambda start, pool, ground, loaded: [("f", 0), ("f", 1)]
        with revisit.scope() as b:
            self.assertIsNotNone(st._chain_of(("f", 1), ENDS))
        self.assertFalse(b.complete, "built beside a part made short")
        self.assertIn(("f", 1), st.short, "and the part it published says so")

    def test_the_note_is_final_when_the_part_appears(self):
        """Watched from inside the lock: the moment a part is put into `done`, its note already holds what its piers
        and its structure read short (nothing of the chain is read after its publication)."""
        for where in ("plain", "commit", "structure"):
            st = _Store(where)
            seen = []

            class Watch(dict):
                def __setitem__(w, k, v, _st=st, _seen=seen):
                    _seen.append((k, _st.__dict__.get("short", {}).get(k)))
                    dict.__setitem__(w, k, v)
            st.done = Watch()
            with revisit.scope() as b:
                st._chain_of(("f", 0), ENDS)
            self.assertEqual(seen, [(("f", 0), (1, 0))], where)
            self.assertEqual(st.short[("f", 0)], (1, 0), f"{where}: nothing was added after")
            self.assertFalse(b.complete)

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
