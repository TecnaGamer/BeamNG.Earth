"""subsea: an underwater tunnel network that cannot be solved is "awaiting" (None: the caller faults, nothing built on
it is kept) only while the map may still hold more of it: a map cell at one of its ends not read. Once every cell at
its ends is read (and the resolve's limit of cells not reached) the map has no continuation, and the answer is final:
left out as before, WITHOUT a fault. Waiting for good made Times Square's and Lower Manhattan's map cells short on
every visit (a 26 m and a 59 m covered ramp onto Hudson piers, one end over the water), so no terrain block or road
tile round them was ever kept (2026-11-13)."""
import os
import sys
import threading
import types
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import geodesy  # noqa: E402
import mapcells  # noqa: E402
import revisit  # noqa: E402
import roads  # noqa: E402
import subsea  # noqa: E402

LAT, LON = 40.7627, -74.0013


def _owner(read, wet_from=20.0):
    """A tunnel store's stand-in: flat ground, water (2 m deep) east of x = wet_from; read: the cells its resolve
    read (None: no resolve)."""
    frame = geodesy.EnuFrame(LAT, LON)

    def water(la, lo):
        x = np.asarray(frame.geodetic_to_enu(np.asarray(la), np.asarray(lo), np.zeros(np.size(la)))[0])
        return np.where(x > wet_from, 2.0, 0.0), np.zeros(np.size(la))
    o = types.SimpleNamespace(frame=frame, water=water, raw=None, _ground=lambda q: np.full(len(q), 3.0),
                              _tls=threading.local())
    if read is not None:
        o._tls.read = frozenset(read)
    return o


def _pool(x1=30.0):
    """One part, 30 m, from dry land (x = 0) out over the water."""
    spec = types.SimpleNamespace(width=7.0)
    return {("f", 1): (spec, np.array([[0.0, 0.0], [x1, 0.0]]), None, 0, 2)}


class SubseaFinalTest(unittest.TestCase):
    def setUp(self):
        self.cell = str(mapcells.cell_of(LAT, LON))

    def solve(self, owner, pool=None):
        pool = pool or _pool()
        with revisit.scope() as b:
            got = subsea.profiles(owner, sorted(pool), pool)
        return got, b

    def test_awaiting_while_an_end_cell_is_not_read(self):
        for read in (None, set(), {"14/1/1"}):
            got, b = self.solve(_owner(read))
            self.assertIsNone(got, read)

    def test_awaiting_when_the_resolve_reached_its_limit(self):
        read = {self.cell} | {f"14/0/{i}" for i in range(roads.CHAIN_CELLS - 1)}
        self.assertEqual(len(read), roads.CHAIN_CELLS)
        got, _ = self.solve(_owner(read))
        self.assertIsNone(got)

    def test_final_once_every_end_cell_is_read(self):
        got, b = self.solve(_owner({self.cell}))
        self.assertIs(got, subsea.LEFT_OUT)
        self.assertEqual(got, ({}, {}))
        self.assertTrue(b.complete, "no fault from subsea itself")

    def test_the_caller_faults_only_while_awaiting(self):
        # (roads.TunnelProfiles._solve: `answer is None and water` is the fault; LEFT_OUT is an answer)
        import inspect
        src = inspect.getsource(roads.TunnelProfiles._solve)
        self.assertIn("if answer is not None:", src)
        self.assertIn("revisit.fault()", src)
        self.assertIn("self._tls.read = frozenset(loaded)", inspect.getsource(roads.TunnelProfiles._resolve))

    def test_a_loop_is_final_too(self):
        spec = types.SimpleNamespace(width=7.0)
        ring = np.array([[0.0, 0.0], [30.0, 0.0], [30.0, 30.0], [0.0, 0.0]])
        pool = {("f", 1): (spec, ring, None, 0, 4)}
        got, _ = self.solve(_owner(None), pool)
        self.assertIsNone(got)
        got, b = self.solve(_owner({self.cell}), pool)
        self.assertIs(got, subsea.LEFT_OUT)
        self.assertTrue(b.complete)

    def test_a_dry_network_is_solved_as_before(self):
        got, _ = self.solve(_owner({self.cell}, wet_from=1e9))
        self.assertEqual(got[0], {})
        self.assertEqual(got[1], {("f", 1): False})


if __name__ == "__main__":
    unittest.main()
