"""Step 8 of the 3D solve: a road on the ground over a tube, a tunnel or a deck (#119, the user 2026-11-27).

    find(network, samples, problem, z, lo, hi) -> [(fid, xy_a (2,), xy_b (2,))]

Where a road the map has on the ground crosses over a road or track the map puts in a tunnel, the tube is a tunnel by
default: the ground over it is the upper road's lid, the tube's hole cut through it. Where the terrain has no room to
model that, the upper road is a deck over the tube instead, the ground under it cut to the tube's floor:
- the tube's opening within MOUTH_M of the upper road's edge (along the lower road, across the angle they cross at):
  the terrain cannot hold the opening between them. The A23's residential street crosses Erzherzog-Karl-Strasse 5.9 m
  inside its tunnel: the mouth graded the ground to the floor 4 m in from the portal, under the street's half, and the
  street stood 0.5-2 m over its own ground, a 3.7 m step.
- the cover over the tube's vault thinner than SOIL_M: the solve's upper less its lower less the clearance it needs.
One decision per crossing of an upper road over a tube, taken on the least cover over the clearance's reach. The deck
spans the lower road's half width and CLEAR_PAD_M either side, across the angle; one road's decks within MOUTH_M of
each other are one (a strip of ground that short between two decks is no more modelled than the opening). A crossing
belongs to the block whose core holds it (lo..hi), as its heights do."""
import numpy as np

from . import problem as _problem

MOUTH_M = 4.0               # m: grading.MOUTH_PLAN_M, the mouth's own grading into the tube
SOIL_M = 0.7                # m: roads.BORE_FIT_M, the soil kept between a tube's vault and a road over it
UPPER = ("ground",)         # the upper member's kinds: a road on the ground (a track's beds are the railways')


def _in_tube(m):
    return m.kind == "tunnel" or (m.kind == "rail" and float(m.level) < 0)


def _to_portal(net, sm, p, i):
    """m along path p from its sample i to where the tube it is in opens (its member no longer in a tunnel), the
    nearer way; inf where the path ends inside the tube both ways (it goes on in another path: no opening here)."""
    sl = sm.of(p)
    mem, s = sm.member[sl], sm.s[sl]
    best = np.inf
    for step in (-1, 1):
        j = i
        while 0 <= j + step < len(mem) and _in_tube(net.members[int(mem[j + step])]):
            j += step
        if 0 <= j + step < len(mem):
            best = min(best, abs(float(s[j + step] + s[j]) / 2 - float(s[i])))
    return best


def _at(sm, p, s0):
    """The point of path p at arc s0 (clipped to the path)."""
    sl = sm.of(p)
    s, xy = sm.s[sl], sm.xy[sl]
    s0 = float(np.clip(s0, s[0], s[-1]))
    return np.array([np.interp(s0, s, xy[:, 0]), np.interp(s0, s, xy[:, 1])])


def find(net, sm, pr, z, lo=None, hi=None):
    """[(fid, xy_a, xy_b)] of the decks: the upper road's span over each tube it has no room to be a tunnel over."""
    from . import outputs
    spans = {}                  # (path, fid) -> [(s_a, s_b)]
    for pa, ia, pb, ib, lay in sm.crossings:
        if lay.upper is None:
            continue
        (pu, iu, mu), (pl, il, ml) = ((pa, ia, lay.a), (pb, ib, lay.b)) if lay.upper == "a" else \
            ((pb, ib, lay.b), (pa, ia, lay.a))
        mU, mL = net.members[mu], net.members[ml]
        if mU.kind not in UPPER or not _in_tube(mL):
            continue
        q = sm.xy[sm.of(pu)][iu]
        if lo is not None and (np.any(q < lo) or np.any(q >= hi)):
            continue
        t1, t2 = _problem.tangent(sm, pu, iu), _problem.tangent(sm, pl, il)
        sin = max(_problem.MIN_SIN, abs(float(t1[0] * t2[1] - t1[1] * t2[0])))
        ou, ol = sm.off[pu], sm.off[pl]
        # (the least cover over the clearance's reach, the pairs the solve held apart there)
        ru = _problem._reach(sm, pu, iu, pl, il, float(mL.half) + _problem.CLEAR_PAD_M)
        rl = _problem._reach(sm, pl, il, pu, iu, float(mU.half) + _problem.CLEAR_PAD_M)
        cover = min(float(z[ou + iu] - z[ol + j]) for j in rl)
        cover = min(cover, min(float(z[ou + j] - z[ol + il]) for j in ru)) - float(lay.need)
        gap = _to_portal(net, sm, pl, il) - float(mU.half) / sin
        if gap >= MOUTH_M and cover >= SOIL_M:
            continue
        reach = (float(mL.half) + _problem.CLEAR_PAD_M) / sin
        s0 = float(sm.s[ou + iu])
        spans.setdefault((pu, outputs.fid_of(mU)), []).append((s0 - reach, s0 + reach))
    out = []
    for (p, fid), ss in sorted(spans.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        ss.sort()
        merged = [list(ss[0])]
        for a, b in ss[1:]:
            if a - merged[-1][1] < MOUTH_M:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        for a, b in merged:
            out.append((fid, _at(sm, p, a), _at(sm, p, b)))
    return out
