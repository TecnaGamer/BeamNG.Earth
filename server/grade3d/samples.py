"""Step 3 of the 3D solve: every path sampled at an even step, and its joins and crossings located on the samples.

    sample(network, layers) -> Samples

A path of length L has n = max(1, round(L / STEP_M)) intervals of h = L / n, its samples at s = 0, h, ..., L, each on
the member the path is on there (its kind: a deck's samples are the deck's), with a sample exactly at each join and
crossing (grid: the nearest moved there, or one put in; the spacing then uneven, the kernel's ds). A join or a crossing
is that sample of each path; a crossing's samples carry its Layer. The samples of every path are also one flat array (off[p]: path p's first), as the ground is read for
them in one call."""
from dataclasses import dataclass

import numpy as np

import gradeshapes as _gsh

STEP_M = _gsh.STEP_M        # m: the target step along a path (a deck's and a dip's, as the interchange solve)
EVENT_MERGE_M = 0.05        # m: a join or crossing this near a sample (or another) is at it
MIN_GAP = 0.25              # of the step: a sample moved to an event keeps this far from its neighbours


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


def locate(network, path_s, i, s):
    """(path, sample index) nearest member i's arc length s (path_s: each path's samples' arc lengths)."""
    p, sp = network.path_s(i, s)
    sv = path_s[p]
    k = int(np.clip(np.searchsorted(sv, sp), 1, max(1, len(sv) - 1))) if len(sv) > 1 else 0
    if len(sv) > 1 and abs(sv[k - 1] - sp) <= abs(sv[k] - sp):
        k -= 1
    return p, k


def grid(length, events, step=STEP_M):
    """The arc lengths of a path's samples: an even step (intervals), with a sample exactly at each event (a join's or
    a crossing's place on it): the nearest even sample moved there where that keeps it more than MIN_GAP of the step
    from its neighbours, else one put in (events within EVENT_MERGE_M of each other are one)."""
    n, h = intervals(length, step)
    s = np.linspace(0.0, length, n + 1) if length > 0 else np.zeros(1)
    if length <= 0 or not len(events):
        return s
    ev = np.unique(np.clip(np.asarray(events, np.float64), 0.0, length))
    ev = ev[np.r_[True, np.diff(ev) > EVENT_MERGE_M]]
    moved = np.zeros(len(s), bool)
    extra = []
    for e in ev.tolist():
        k = int(round(e / h))
        if abs(s[k] - e) <= EVENT_MERGE_M:
            moved[k] = True
            continue
        lo = s[k - 1] if k > 0 else -np.inf
        hi = s[k + 1] if k < n else np.inf
        if 0 < k < n and not moved[k] and e - lo > MIN_GAP * h and hi - e > MIN_GAP * h:
            s[k] = e
            moved[k] = True
        else:
            extra.append(e)
    if extra:
        s = np.unique(np.r_[s, extra])
    return s


def sample(network, layers=()):
    """The Samples of the network's paths (step 1) and its crossings' Layers (step 2; their order is the network's
    crossings'), a sample exactly at each join and crossing."""
    net = network.net
    events = [[] for _ in network.paths]
    for a, sa, b, sb in network.joins:
        for i, si in ((a, sa), (b, sb)):
            p, sp = network.path_s(i, si)
            events[p].append(sp)
    for x in layers:
        for i, si in ((x.a, x.sa), (x.b, x.sb)):
            p, sp = network.path_s(i, si)
            events[p].append(sp)
    xys, ss, mems, hs, off = [], [], [], [], [0]
    for path, ev in zip(network.paths, events):
        n, h = intervals(path.length)
        s = grid(path.length, ev)
        xy, mem = _along(net, path, s)
        xys.append(xy)
        ss.append(s)
        mems.append(mem)
        hs.append(h if path.length > 0 else 0.0)
        off.append(off[-1] + len(s))
    h = np.asarray(hs, np.float64)
    joins = []
    for a, sa, b, sb in network.joins:
        joins.append(locate(network, ss, a, sa) + locate(network, ss, b, sb))
    crossings = []
    for x in layers:
        crossings.append(locate(network, ss, x.a, x.sa) + locate(network, ss, x.b, x.sb) + (x,))
    cat = (lambda v, w: np.concatenate(v) if v else np.zeros(w))
    return Samples(cat(xys, (0, 2)).reshape(-1, 2), np.asarray(off, np.int64), cat(ss, 0), h,
                   cat(mems, 0).astype(np.int64), joins, crossings)
