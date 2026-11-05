"""netstate: the downloads in flight across the server's processes (the loading line's "downloading" part): a worker
process (spawned, as the server's are) marks its requests in its slot of the server's shared block, the server's
process sums them; the Meter's MB count from a burst's start and its rate."""
import multiprocessing
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import netstate  # noqa: E402


def _worker(q):
    import netstate as ns
    ns.begin()
    ns.begin()
    ns.end(1_500_000)
    q.put("marked")
    q.get()             # (stays alive with one request in flight until the parent has looked)


class NetStateTest(unittest.TestCase):
    def tearDown(self):
        netstate.close()
        os.environ.pop(netstate.ENV, None)
        netstate._slot, netstate._active, netstate._bytes = None, 0, 0

    def test_worker_counts_reach_the_server(self):
        if not netstate.init():
            self.skipTest("no shared memory here")
        ctx = multiprocessing.get_context("spawn")
        q, back = ctx.Queue(), ctx.Queue()
        p = ctx.Process(target=_worker, args=(q,))
        p.start()
        try:
            self.assertEqual(q.get(timeout=30), "marked")
            netstate.begin()                    # (this process too)
            act, got = netstate.snapshot()
            self.assertEqual(act, 2)            # the worker's one still in flight + ours
            self.assertEqual(got, 1_500_000)
            netstate.end(500_000)
            self.assertEqual(netstate.snapshot(), (1, 2_000_000))
        finally:
            q.put("done")
            p.join(timeout=30)

    def test_without_the_block(self):
        os.environ.pop(netstate.ENV, None)
        netstate.begin()
        netstate.end(10)                        # (no block: nothing, no error)
        self.assertEqual(netstate.snapshot(), (0, 0))

    def test_meter(self):
        t = [0.0]
        state = [(0, 0)]
        m = netstate.Meter(clock=lambda: t[0], snap=lambda: state[0])
        self.assertEqual(m.read(), (0, 0.0, 0.0))
        t[0], state[0] = 1.0, (2, 0)
        t[0], state[0] = 2.0, (2, 2_000_000)
        act, mb, rate = m.read()
        self.assertEqual((act, mb), (2, 2.0))
        t[0], state[0] = 4.0, (1, 6_000_000)
        act, mb, rate = m.read()
        self.assertEqual(mb, 6.0)
        self.assertAlmostEqual(rate, 1.5, places=3)     # 6 MB over the 4 s window (RATE_S)
        t[0], state[0] = 5.0, (0, 6_000_000)
        m.read()
        t[0], state[0] = 11.0, (1, 7_000_000)           # after IDLE_S without any: a new burst from 0
        act, mb, _ = m.read()
        self.assertEqual((act, mb), (1, 0.0))


if __name__ == "__main__":
    unittest.main()
