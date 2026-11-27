"""Step 3 of the 3D solve: every path sampled at an even step, and its joins and crossings located on the samples.

    sample(network, layers) -> Samples

A path of length L has n = max(1, round(L / STEP_M)) intervals of h = L / n (the kernel takes one step per chain), its
samples at s = 0, h, ..., L, each on the member the path is on there (its kind: a deck's samples are the deck's). A join
or a crossing is the nearest sample of each path (off by at most h / 2: STEP_M / 2 and a little); a crossing's samples
carry its Layer. The samples of every path are also one flat array (off[p]: path p's first), as the ground is read for
them in one call."""
from dataclasses import dataclass

import numpy as np

import gradeshapes as _gsh

STEP_M = _gsh.STEP_M        # m: the target step along a path (a deck's and a dip's, as the interchange solve)


@dataclass
class Samples:
    """xy (N, 2) of every path's samples, path after path; off (P + 1,): path p's are off[p]:off[p + 1]; s (N,) each one's
    arc length on its path; h (P,) each path's step; member (N,) the member each is on; joins [(path a, sample a, path b,
    sample b)] (the sample indices within their paths); crossings [(path a, sample a, path b, sample b, Layer)]."""
    xy: np.ndarray
    off: np.ndarray
    s: np.ndarray
    h: np.ndarray
    member: np.ndarray
    joins: list
    crossings: list

    def of(self, p):
        """slice of path p's samples in the flat arrays."""
        return slice(int(self.off[p]), int(self.off[p + 1]))

    def __len__(self):
        return len(self.s)


def intervals(length, step=STEP_M):
    """(n intervals, h) of a path of length m at the target step."""
    n = max(1, int(round(float(length) / step)))
    return n, float(length) / n


def _along(net, path, s):
    """(xy (k, 2), member (k,)) of the path at its arc lengths s (sorted)."""
    xy = np.empty((len(s), 2))
    mem = np.empty(len(s), np.int64)
    k = np.clip(np.searchsorted(path.start, s, side="right") - 1, 0, len(path.parts) - 1)
    for part in np.unique(k):
        sel = k == part
        i, forward = path.parts[part]
        line, cum = net.lines[i], np.asarray(net.s[i], np.float64)
        local = s[sel] - path.start[part]
        if not forward:
            local = float(net.L[i]) - local
        local = np.clip(local, 0.0, float(net.L[i]))
        if len(line) < 2:
            xy[sel] = line[0]
        else:
            xy[sel, 0] = np.interp(local, cum, line[:, 0])
            xy[sel, 1] = np.interp(local, cum, line[:, 1])
        mem[sel] = i
    return xy, mem


def locate(network, h, i, s):
    """(path, sample index) nearest member i's arc length s."""
    p, sp = network.path_s(i, s)
    n = int(round(network.paths[p].length / h[p])) if h[p] > 0 else 0
    return p, int(min(n, max(0, round(sp / h[p])))) if h[p] > 0 else 0


def sample(network, layers=()):
    """The Samples of the network's paths (step 1) and its crossings' Layers (step 2; their order is the network's
    crossings')."""
    net = network.net
    xys, ss, mems, hs, off = [], [], [], [], [0]
    for path in network.paths:
        n, h = intervals(path.length)
        s = np.linspace(0.0, path.length, n + 1) if path.length > 0 else np.zeros(1)
        xy, mem = _along(net, path, s)
        xys.append(xy)
        ss.append(s)
        mems.append(mem)
        hs.append(h if path.length > 0 else 0.0)
        off.append(off[-1] + len(s))
    h = np.asarray(hs, np.float64)
    joins = []
    for a, sa, b, sb in network.joins:
        joins.append(locate(network, h, a, sa) + locate(network, h, b, sb))
    crossings = []
    for x in layers:
        crossings.append(locate(network, h, x.a, x.sa) + locate(network, h, x.b, x.sb) + (x,))
    cat = (lambda v, w: np.concatenate(v) if v else np.zeros(w))
    return Samples(cat(xys, (0, 2)).reshape(-1, 2), np.asarray(off, np.int64), cat(ss, 0), h,
                   cat(mems, 0).astype(np.int64), joins, crossings)
