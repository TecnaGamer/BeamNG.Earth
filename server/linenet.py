"""The line network (earthfast.LineNet): every line the interchange solve reads, roads on the ground, decks, tunnels and
tracks alike, in one index for the process. Its segments are in one grid, and the lines near each other are paired once,
as they are added, so what touches a line is a lookup. A line is added once, whichever net, round or growth meets it
first (gradeshapes._line_id: its key, nodes and bytes), and every net after reads it there: gradeshapes.Net takes its
members' arc lengths and boxes from it and gradeshapes._relations its members' joints and crossings.

The geometry of a pair (where two lines cross, where an end of one lies on the other) is made natively, to the bit of
gradeshapes._pair_geometry; what each contact is (a joint, a crossing, which is over) is gradeshapes._pair_from's, read
on the asking net's members."""
import threading

import numpy as np

import fast

VERSION = 642                   # the earthfast whose LineNet is shared by threads (arclens, boxes, points)
MAX_POINTS = 8_000_000          # points the process's network holds before a new one is started (~40 bytes a point
                                # with its grid: ~320 MB at most; Yokohama's 110 cells are ~1 M)


class Network:
    """One earthfast.LineNet and the numbers of the lines in it, by line id."""

    def __init__(self):
        import gradeshapes as gsh
        fast.require(VERSION, "LineNet")
        self.ln = fast._ef.LineNet(gsh.JOIN_M + 0.5)
        self.ids = {}
        self.lock = threading.Lock()

    def full(self):
        return self.ln.points >= MAX_POINTS

    def bind(self, net):
        """(numbers, at): the numbers in the network of net's members' lines, those it lacks added, and {number:
        [member]} (two members of one line, kept apart, have one number)."""
        import gradeshapes as gsh
        keys = [gsh._line_id(net, i) for i in range(len(net.m))]
        with self.lock:
            new, seen = [], set()
            for i, k in enumerate(keys):
                if k not in self.ids and k not in seen:
                    seen.add(k)
                    new.append(i)
            if new:
                first = self.ln.add([net.lines[i] for i in new])
                for n, i in enumerate(new):
                    self.ids[keys[i]] = first + n
            ids = [self.ids[k] for k in keys]
        at = {}
        for i, q in enumerate(ids):
            at.setdefault(q, []).append(i)
        return ids, at


_CURRENT = [None]
_LOCK = threading.Lock()


def current():
    """The process's network: a new one when there is none, or the last is full (a net bound to the old one goes on
    with it)."""
    with _LOCK:
        nw = _CURRENT[0]
        if nw is None or nw.full():
            nw = _CURRENT[0] = Network()
        return nw


def reset():
    """A new network for the process (the tests: one test's lines are not the next one's)."""
    with _LOCK:
        _CURRENT[0] = None


def geometry(net, i):
    """[(j, hits, ends)] of member i of net with each member it touches (gradeshapes._pair_geometry's tuples), j
    ascending."""
    import gradeshapes as gsh
    nw, at = net.network, net.ln_at
    me = net.ln_ids[i]
    got = []
    for q, hits, ends in nw.ln.contacts(me, gsh.CROSS_END_M, gsh.JOIN_M):
        for j in at.get(q, ()):
            got.append((j, hits, ends))
    for j in at[me]:
        if j != i:                      # (another member of the same line)
            hits, ends = nw.ln.pair(me, me, gsh.CROSS_END_M, gsh.JOIN_M)
            if hits or ends:
                got.append((j, hits, ends))
    got.sort(key=lambda g: g[0])
    return [(j, [(si, sj, np.array([x, y])) for si, sj, x, y in hits],
             [(sw, se, so, np.array([x, y])) for sw, se, so, x, y in ends]) for j, hits, ends in got]
