"""The line network (earthfast.LineNet): every line the interchange solve reads, roads on the ground, decks, tunnels and
tracks alike, in one index. Its segments are in one grid, and the lines near each other are paired once, as they are
added, so what touches a line is a lookup. The geometry of a pair (where two lines cross, where an end of one lies on the
other) is made natively to the bit of gradeshapes._pair_geometry; what each contact is (a joint, a crossing, which is
over) is gradeshapes._pair_from's, so relations(i) is gradeshapes._relations(net, i).

Lines of fewer than two points have no segments: they are near nothing (the solve's seeds and members it relates all
have two points or more)."""
import numpy as np

import fast
import gradeshapes as gsh

VERSION = 641                   # the earthfast that has LineNet
PAD_M = gsh.JOIN_M + 0.5        # m: lines whose segments come this close are a near pair (_relations' near_along pad)


class Lines:
    """A gradeshapes Net's members in an earthfast.LineNet, numbered as the net's."""

    def __init__(self, net):
        fast.require(VERSION, "LineNet")
        self.net = net
        self.ln = fast._ef.LineNet(PAD_M)
        self.ln.add(net.lines)

    def near(self, i):
        """The members whose lines come within PAD_M of member i's, ascending."""
        return self.ln.near(i)

    def geometry(self, i):
        """[(j, hits, ends)] of member i with each member it touches (gradeshapes._pair_geometry's tuples), j
        ascending."""
        out = []
        for j, hits, ends in self.ln.contacts(i, gsh.CROSS_END_M, gsh.JOIN_M):
            out.append((j, [(si, sj, np.array([x, y])) for si, sj, x, y in hits],
                        [(sw, se, so, np.array([x, y])) for sw, se, so, x, y in ends]))
        return out

    def relations(self, i):
        """gradeshapes._relations(net, i): [(j, "join"|"cross", s on i, s on j, q)]."""
        out = []
        for j, hits, ends in self.geometry(i):
            out.extend((j,) + r_ for r_ in gsh._pair_from(self.net, i, j, hits, ends))
        return out
