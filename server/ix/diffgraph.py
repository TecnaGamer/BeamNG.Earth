"""Difference constraints z_v - z_u <= w, and what to do where they contradict (the order step's and the heights step's
common ground).

A set of such constraints has heights iff its graph (an edge u -> v of weight w) has no negative cycle, and then the
shortest-path distances are heights that satisfy every one of them. Where there is a negative cycle the constraints
contradict: Graph.settle() finds each one and asks the caller which of its edges to give way and by how much, then goes
on from the labels it has (the heights it had reached stay: only what changed is looked at again), until the graph has
heights. It never gives a constraint up on its own: the caller's `yield_` names what changes.

Pure and deterministic: plain floats, the same edges in the same order give the same cycles in the same order."""
import math
from collections import deque

EPS_M = 1e-7            # m: a label must fall by this much to count (float noise is no cycle)
CONTRADICTION_M = 1e-6  # m: a cycle this short of closing is a contradiction


class Graph:
    """Nodes 0..n-1, edges added with add(); w may be raised (eased) or made math.inf (the constraint given up) later."""

    def __init__(self, n):
        self.n = n
        self.eu, self.ev, self.ew = [], [], []
        self.adj = [[] for _ in range(n)]
        self.dist = [0.0] * n

    def add(self, u, v, w):
        """The constraint z_v - z_u <= w; the edge's index."""
        k = len(self.eu)
        self.eu.append(u)
        self.ev.append(v)
        self.ew.append(float(w))
        self.adj[u].append(k)
        return k

    def _cycle(self, pred):
        """The edges (in order) of a cycle among the predecessor edges, or None."""
        n, eu = self.n, self.eu
        stamp = [0] * n
        for s in range(n):
            if stamp[s]:
                continue
            v = s
            while v != -1 and stamp[v] == 0:
                stamp[v] = s + 1
                k = pred[v]
                v = eu[k] if k >= 0 else -1
            if v != -1 and stamp[v] == s + 1:
                cyc, u = [], v
                while True:
                    k = pred[u]
                    cyc.append(k)
                    u = eu[k]
                    if u == v:
                        break
                cyc.reverse()
                return cyc
        return None

    def settle(self, yield_):
        """Labels (heights) satisfying every edge, after yield_(cycle) has been asked about each contradiction on the way.
        yield_(cycle: [edge index]) -> [edge index whose weight it changed] (it must raise at least one weight, or the
        cycle comes back). A cycle whose weight is no more than CONTRADICTION_M short of zero is no contradiction. The
        queue empty, every edge holds (a cycle among the labels' sources could not be short then)."""
        n, adj, ev, ew = self.n, self.adj, self.ev, self.ew
        dist = self.dist
        pred = [-1] * n
        inq = [True] * n
        queue = deque(range(n))
        since, every = 0, max(n, 2000)
        while queue:
            u = queue.popleft()
            inq[u] = False
            du = dist[u]
            for k in adj[u]:
                w = ew[k]
                if w == math.inf:
                    continue
                v = ev[k]
                nd = du + w
                if nd < dist[v] - EPS_M:
                    dist[v] = nd
                    pred[v] = k
                    since += 1
                    if not inq[v]:
                        inq[v] = True
                        queue.append(v)
            if since >= every:
                since = 0
                self._take(pred, yield_, queue, inq)
        return dist

    def _take(self, pred, yield_, queue, inq):
        """A cycle among the predecessors, if there is one: a contradiction goes to yield_ and the tails of the edges it
        changed are looked at again; the cycle's predecessors are forgotten (the labels stay)."""
        import xproc
        xproc.check_cancel()        # (the step's time limit, ix/limits.py: a relaxation that goes on and on stops here)
        cyc = self._cycle(pred)
        if cyc is None:
            return
        changed = yield_(cyc) if math.fsum(self.ew[k] for k in cyc) < -CONTRADICTION_M else ()
        for k in cyc:
            pred[self.ev[k]] = -1
        for k in changed:
            u = self.eu[k]
            if not inq[u]:
                inq[u] = True
                queue.append(u)

    def check(self):
        """The most any edge is violated by the labels (0 where they satisfy every one): a certificate, checked edge by edge."""
        d = self.dist
        return max([d[v] - d[u] - w for u, v, w in zip(self.eu, self.ev, self.ew) if w != math.inf] + [0.0])
