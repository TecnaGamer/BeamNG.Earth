"""Step 1 (SPEC.md): one interchange's members and how they meet and cross (gradeshapes' relations), as the Paths
artifact. Its members are its structures, the roads and tracks on the ground they reach, and what its pieces are
followed through: the same from every region that finds it, numbered by their keys, so the member numbers of every
later artifact of the interchange are these. The live objects stay gradeshapes' own: the later steps keep using
them, so behaviour is byte-identical. Also here: reading the map cells' members into the region (load, members_of)."""
import numpy as np

import gradeshapes as _gsh

from . import artifacts, cache

FORMAT = 8     # 8: a link's ground part a link, a piece of a road its own stretch's map nodes (#133); 7: two roads joined where the map joins them, a way's decks at two levels two decks, a tunnel's height limit (#129); 6: a track's map nodes, as a road's (#117); 5: a tunnel member's half the carriageway's and the shoulder's, as a ground road's (#119)
LAWS = (("gradeshapes", ["JOIN_M", "CROSS_END_M", "MERGE_DEG", "NODE_M", "NODE_EPS_M"]),)
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


def local_of(net, sysm, pcs):
    """{member of net: its number in the interchange's Paths}: every member the interchange names (its seeds, crossings,
    joins, ends and dips), what its structures are near (their relations: the ground roads they reach) and what its
    pieces are followed through; numbered by key, then by place in net."""
    names = set(int(i) for i in sysm["seeds"])
    for c in sysm["crossings"]:
        names |= {int(c["u"]), int(c["d"])}
    for a, _sa, b, _sb in list(sysm["joins"]) + list(sysm["ends"]):
        names |= {int(a), int(b)}
    names |= {int(d) for d, _a, _b in sysm["dips"]}
    names |= {int(t) for t, _a, _b in sysm.get("tracks", ())}
    for pc in pcs:
        names |= {int(j) for j, _s0, _s1 in pc["segs"]}
    for i in sorted(names):
        if net.m[i].kind in _gsh.STRUCTURES:
            names |= {int(j) for j, *_r in _gsh._relations(net, i)}
    return {g: n for n, g in enumerate(sorted(names, key=lambda i: (str(net.m[i].key), i)))}


def run(net, loc, scope):
    """The Paths artifact of one interchange: its members (loc, from local_of) and the relations of its structures with
    them. Never a ground road's with another's: those are a ground road's own meeting, not what an interchange is made
    of, and most of the pairs of a city. Each pair once, the lower member first; the lists ordered by member and
    position."""
    members = sorted(loc, key=loc.get)
    joins, crossings = [], []
    for i in members:
        if net.m[i].kind not in _gsh.STRUCTURES:
            continue
        for j, kind, si, sj, q in _gsh._relations(net, i):
            if j not in loc or (loc[j] < loc[i] and net.m[j].kind in _gsh.STRUCTURES):
                continue            # (two structures' pair is kept from the lower's side)
            a, sa, b, sb = (loc[i], si, loc[j], sj) if loc[i] < loc[j] else (loc[j], sj, loc[i], si)
            if kind == "join":
                joins.append(artifacts.Join(a, float(sa), b, float(sb)))
            else:
                crossings.append(artifacts.PlanCrossing(a, float(sa), b, float(sb), np.asarray(q, np.float64)))
    joins.sort(key=lambda r_: (r_.a, r_.b, r_.sa, r_.sb))
    crossings.sort(key=lambda r_: (r_.a, r_.b, r_.sa, r_.sb))
    return artifacts.Paths(artifacts.Meta(STEP, FORMAT, "", (), LAWS_DIGEST, scope),
                           [member_of(net.m[i]) for i in members], joins, crossings)
