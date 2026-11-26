"""gradeshapes.Net.near is the linear box scan's answer, for any pad; the line network's near pairs (the solve's
relations) are among the boxes' and leave out nothing that has a joint or a crossing."""
import os
import sys
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import gradeshapes as gs  # noqa: E402
from gradeshapes import Member  # noqa: E402


def linear_near(net, i, pad):
    """The scan Net.near was before the grid."""
    b = net.box[i]
    bx = net.box
    return [int(j) for j in np.flatnonzero((bx[:, 0] <= b[2] + pad) & (bx[:, 2] >= b[0] - pad) &
                                          (bx[:, 1] <= b[3] + pad) & (bx[:, 3] >= b[1] - pad)) if j != i]


def member(n, line):
    return Member(("m%d" % n, "ground", 0), np.asarray(line, np.float64), "ground", "secondary", 50, 0.0, 5.0, None)


def random_net(seed, n, span):
    """n members of mixed lengths (a few hundred m, a few km, a few points of a metre) over a span m square."""
    rng = np.random.RandomState(seed)
    out = []
    for k in range(n):
        p = rng.uniform(-span, span, 2)
        length = rng.choice([0.5, 40.0, 250.0, 1500.0, 6000.0])
        ang = rng.uniform(0, 2 * np.pi)
        d = np.array([np.cos(ang), np.sin(ang)]) * length
        t = np.linspace(0.0, 1.0, 5)[:, None]
        out.append(member(k, p + t * d))
    return gs.Net(out)


class NearIndexTest(unittest.TestCase):
    def test_near_gives_the_linear_scans_answer(self):
        for seed, n, span in ((1, 300, 3000.0), (2, 120, 300.0), (3, 700, 20000.0)):
            net = random_net(seed, n, span)
            for pad in (0.0, 3.0, 17.5, 400.0):
                for i in range(len(net.m)):
                    self.assertEqual(net.near(i, pad), linear_near(net, i, pad), (seed, pad, i))

    def test_the_network_pairs_are_near_without_the_members_off_the_line(self):
        net = random_net(4, 400, 4000.0)
        pad = gs.JOIN_M + 0.5
        for i in range(len(net.m)):
            at = {q: js for q, js in net.ln_at.items()}
            along = sorted(j for q in net.network.ln.near(net.ln_ids[i]) for j in at.get(q, ()))
            box = net.near(i, pad)
            self.assertTrue(set(along) <= set(box), i)
            # (what it leaves out has no joint or crossing with i: the relations are the same through either)
            for j in set(box) - set(along):
                self.assertEqual(gs._pair_relations(net, i, j), [], (i, j))

    def test_members_touching_only_at_the_pad_are_near(self):
        a = member(0, [(0.0, 0.0), (10.0, 0.0)])
        b = member(1, [(13.0, 0.0), (20.0, 0.0)])         # (3 m from a's end: near at pad 3, not below)
        c = member(2, [(10.0, 33.0), (20.0, 33.0)])
        net = gs.Net([a, b, c])
        self.assertEqual(net.near(0, 3.0), [1])
        self.assertEqual(net.near(0, 2.99), [])
        self.assertEqual(net.near(1, 3.0), [0])
        self.assertEqual(net.near(2, 40.0), [0, 1])

    def test_the_systems_found_are_the_same_with_the_linear_scan(self):
        import unittest.mock

        def deck(n, line):
            return Member(("d%d" % n, "deck", 0), np.asarray(line, np.float64), "deck", "secondary", 50, 1.0, 5.0, None)
        x = np.linspace(-300.0, 300.0, 40)
        ms = [member(0, np.stack([x, np.zeros(40)], 1)), member(1, np.stack([np.zeros(40), x], 1)),
              deck(2, np.stack([np.linspace(-60.0, 60.0, 20), np.full(20, 80.0)], 1)),
              deck(3, np.stack([np.full(20, 0.0), np.linspace(-60.0, 60.0, 20)], 1)),
              member(4, np.stack([x + 700.0, np.full(40, 400.0)], 1))]

        def canon(systems):
            return [(s["seeds"], s["joins"], [(c["u"], c["d"], c["moves"]) for c in s["crossings"]], s["ends"], s["dips"])
                    for s in systems]
        got = canon(gs.find_systems(ms)[1])
        with unittest.mock.patch.object(gs.Net, "near", linear_near):
            self.assertEqual(got, canon(gs.find_systems(ms)[1]))
        self.assertTrue(got)

    def test_one_member_and_none(self):
        self.assertEqual(gs.Net([member(0, [(0.0, 0.0), (5.0, 5.0)])]).near(0, 3.0), [])
        self.assertEqual(gs.Net([]).box.shape, (0, 4))


if __name__ == "__main__":
    unittest.main()
