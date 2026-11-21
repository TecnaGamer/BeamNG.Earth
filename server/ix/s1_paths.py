"""Step 1 (SPEC.md): the region's members after one_road, and how they meet and cross (gradeshapes' relations), as
the Paths artifact. The live objects stay gradeshapes' own: the later steps keep using them, so behaviour is
byte-identical."""
import numpy as np

import gradeshapes as _gsh

from . import artifacts, cache

FORMAT = 1
LAWS = (("gradeshapes", ["JOIN_M", "CROSS_END_M", "MERGE_DEG"]),)
LAWS_DIGEST = cache.laws_digest(*LAWS)
STEP = 1


def load(planner, box_lo, box_hi, byk, loaded):
    """One round of the region's growth: the map cells newly in the box, their members into byk (the longest line of a
    key kept) and loaded."""
    import mapcells
    cla, clo, _ = planner.frame.enu_to_geodetic(np.array([box_lo[0], box_hi[0], box_lo[0], box_hi[0]]),
                                                np.array([box_lo[1], box_lo[1], box_hi[1], box_hi[1]]), 0.0)
    keys = sorted(set(mapcells.cells_for_bbox(float(np.min(cla)), float(np.min(clo)), float(np.max(cla)),
                                              float(np.max(clo)))) - loaded, key=str)
    for k in keys:
        loaded.add(k)
        for m in planner._shape_members(k):
            old = byk.get(m.key)
            if old is None or len(m.line) > len(old.line):
                byk[m.key] = m


def members_of(byk, box_lo, box_hi):
    """one_road of the members in the box (a road also there as its lane sections' unsplit copy dropped)."""
    return _gsh.one_road([byk[q] for q in sorted(byk, key=str)
                          if ((byk[q].line >= box_lo) & (byk[q].line <= box_hi)).all(1).any()])


def member_of(m):
    """A gradeshapes member as data. Its signed height limit is not data (a callable): Crossing.need carries what it
    asks at each crossing."""
    return artifacts.Member(str(m.key), np.asarray(m.line, np.float64)[:, :2], str(m.kind), str(m.cls),
                            None if m.kmh is None else float(m.kmh), float(m.level), float(m.half), bool(m.held),
                            None if m.nodes is None else tuple(sorted(str(n) for n in m.nodes)))


def run(planner, net, scope):
    """The Paths artifact of net's members and their relations (each pair once, from its lower index's side)."""
    joins, crossings = [], []
    for i in range(len(net.m)):
        for j, kind, si, sj, q in _gsh._relations(net, i):
            if j < i:
                continue            # (a pair's relations are made from both members' sides; kept from the lower's)
            if kind == "join":
                joins.append(artifacts.Join(i, float(si), j, float(sj)))
            else:
                crossings.append(artifacts.PlanCrossing(i, float(si), j, float(sj), np.asarray(q, np.float64)))
    return artifacts.Paths(artifacts.Meta(STEP, FORMAT, "", (), LAWS_DIGEST, scope),
                           [member_of(m) for m in net.m], joins, crossings)
