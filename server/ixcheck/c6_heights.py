"""Check 6, Heights: the solved pieces against the order, the datum and the terrain, and the joints and boundaries the
earlier steps promised (the checks carried forward)."""
import numpy as np

from ix import artifacts

GRADE_ROAD = 0.08   # a road's samples never climb steeper between neighbours (fixed here, as the spec has it)
GRADE_RAIL = 0.05   # a track's
JOINT_M = 0.05      # m: the two heights at a joint of the pieces
CLEAR_SLACK = 0.05  # m: the clearance may fall this short of the need without a failure


def check(art, parents):
    if not isinstance(art, artifacts.Heights):
        return []
    out = []
    paths, ext = parents.get(1), parents.get(2)
    order = _same_system(parents, 3, art.system)
    terrain = _same_system(parents, 4, art.system)
    ms = paths.members if paths is not None else []
    sysm = next((s for s in ext.systems if s.key == art.system), None) if ext is not None else None
    key = _keyer(ms)
    zs = [np.asarray(pc.z, np.float64) for pc in art.pieces]

    if not art.solved:
        seeds = sysm.seeds if sysm is not None else sorted({int(pc.main) for pc in art.pieces})
        out.append(_f(art, "note", "no_heights", "system %s left as it is, no heights: %s"
                      % (art.system, ", ".join(key(i) for i in seeds))))
        return out

    for p, pc in enumerate(art.pieces):
        n = int((~np.isfinite(zs[p])).sum())
        if n:
            out.append(_f(art, "fail", "heights_nan", "piece %d (%s) has %d non-finite heights"
                          % (p, key(int(pc.main)), n), ((int(pc.main), float(pc.ms[0]) if len(pc.ms) else 0.0),)))

    eased = _eased(art, key)
    out += eased[0]

    if order is not None:
        for c in order.crossings:
            zu = _at(zs, art, c.u, c.su)
            if zu is None:
                continue
            zl = _at(zs, art, c.d, c.sd)
            if zl is None and terrain is not None:
                zl = _at(terrain.ground, art, c.d, c.sd)   # the lower member's own ground, where a piece holds it
            if zl is None and terrain is not None:
                zl = _at(terrain.ground, art, c.u, c.su)   # else the ground at the crossing, read on the upper's own
                #                                               piece: the ground the lower road sits on there
            if zl is None or not np.isfinite(zu) or not np.isfinite(zl):
                continue
            gap = zu - zl
            allowed = eased[1].get(frozenset((c.u, c.d)), 0.0) + eased[2].get(c.u, 0.0) + eased[2].get(c.d, 0.0)
            if gap < c.need - CLEAR_SLACK - allowed:
                out.append(_f(art, "fail", "clearance", "crossing of %s over %s: %.2f m over %.2f, %.2f m short of "
                              "the %.2f m asked" % (key(c.u), key(c.d), zu, zl, (c.need - CLEAR_SLACK) - gap, c.need),
                              ((c.u, c.su), (c.d, c.sd))))

    if sysm is not None:
        for jn in sysm.joins:
            za, zb = _at(zs, art, jn.a, jn.sa), _at(zs, art, jn.b, jn.sb)
            if za is None or zb is None:
                continue
            if abs(za - zb) > JOINT_M:
                out.append(_f(art, "fail", "joint_step", "joint of %s at s=%.1f and %s at s=%.1f differ %.2f m"
                              % (key(jn.a), jn.sa, key(jn.b), jn.sb, abs(za - zb)),
                              ((jn.a, jn.sa), (jn.b, jn.sb))))
        for b in sysm.boundary:
            z, g = _at(zs, art, b.i, b.si), None
            if terrain is not None:
                g = _at(terrain.ground, art, b.i, b.si)
            if z is None or g is None:
                continue
            out.append(_f(art, "note", "boundary_off", "boundary of %s at s=%.1f: %.2f m over its ground there"
                          % (key(b.i), b.si, z - g), ((b.i, b.si), (b.j, b.sj))))

    out += _grades(art, zs, ms, key, eased[3])
    return out


def _eased(art, key):
    """(notes, {frozenset((member, member)): m a clearance between the two was eased by}, {member: m a clearance bound on one
    member (a road the shape does not move) was eased by}, {members whose grade law was eased}): what the solve relaxed
    (diag["relaxed"]: kind, amount, at, text), counted as relaxations, never as failures, in notes of their own."""
    notes, pair, single, grade = [], {}, {}, set()
    for r in art.diag.get("relaxed") or ():
        at = tuple((int(m), float(s)) for m, s in r.get("at", ()))
        notes.append(_f(art, "note", "relaxed_" + str(r.get("kind")), "%s (%.3f)" % (r.get("text", ""), float(r.get("amount", 0.0))),
                        at))
        if r.get("kind") == "clearance":
            members = sorted({m for m, _s in at})
            if len(members) == 2:
                k = frozenset(members)
                pair[k] = max(pair.get(k, 0.0), float(r["amount"]))
            for m in members if len(members) == 1 else ():
                single[m] = max(single.get(m, 0.0), float(r["amount"]))
        elif r.get("kind") == "grade":
            grade |= {m for m, _s in at}
    return notes, pair, single, grade


def _f(art, level, code, text, where=()):
    return artifacts.Finding(art.meta.step, level, code, text, where)


def _same_system(parents, step, system):
    a = parents.get(step)
    return a if a is not None and getattr(a, "system", None) == system else None


def _keyer(ms):
    def key(i):
        return str(ms[i].key) if 0 <= i < len(ms) else str(i)
    return key


def _cover(art, mem, s):
    """(piece, its samples on mem, their arcs) of the first piece whose samples on mem span s, by arc: where a member's
    height is read."""
    for p, pc in enumerate(art.pieces):
        k = np.flatnonzero(np.asarray(pc.mem, np.int64) == mem)
        if not len(k):
            continue
        ms_ = np.asarray(pc.ms, np.float64)[k]
        if float(ms_.min()) - 1e-6 <= s <= float(ms_.max()) + 1e-6:
            o = np.argsort(ms_, kind="stable")
            return p, k[o], ms_[o]
    return None


def _at(vals, art, mem, s):
    """One value per sample of the pieces (a solved z, or a ground) at (mem, s), or None where no piece covers it.
    Between two samples the piece's d runs with its ms, so the share along the one is the share along the other."""
    got = _cover(art, mem, s)
    if got is None or got[0] >= len(vals):
        return None
    p, k, ms_ = got
    v = np.asarray(vals[p], np.float64)
    if len(v) != len(art.pieces[p].ms):
        return None
    if len(k) < 2 or s <= float(ms_[0]):
        return float(v[int(k[0])])
    if s >= float(ms_[-1]):
        j = len(k) - 2
    else:
        j = min(max(int(np.searchsorted(ms_, s, side="right")) - 1, 0), len(k) - 2)
    den = float(ms_[j + 1] - ms_[j])
    if den <= 0.0:
        return float(v[int(k[j])])
    a, b = int(k[j]), int(k[j + 1])
    return float(v[a] + (v[b] - v[a]) * ((s - float(ms_[j])) / den))


def _grades(art, zs, ms, key, eased=frozenset()):
    """A note per piece whose heights climb steeper between neighbour samples than its member's law allows (not one whose
    law the solve eased: that is a relaxation, noted as such)."""
    out = []
    gm = [GRADE_RAIL if m.kind in ("rail", "rail_deck") else GRADE_ROAD for m in ms]
    for p, pc in enumerate(art.pieces):
        z = zs[p]
        if len(pc.d) < 2 or not np.isfinite(z).all() or int(pc.main) in eased:
            continue
        dd = np.diff(np.asarray(pc.d, np.float64))
        dz = np.abs(np.diff(z))
        mem = np.asarray(pc.mem, np.int64)[:-1]
        for k in range(len(dd)):
            if dd[k] <= 1e-9:
                continue
            g = gm[int(mem[k])] if 0 <= int(mem[k]) < len(gm) else GRADE_ROAD
            if dz[k] / dd[k] > g:
                out.append(_f(art, "note", "grade", "piece %d (%s) climbs %.3f between s=%.1f and s=%.1f, above its "
                              "%.2f" % (p, key(int(pc.main)), dz[k] / dd[k], float(pc.ms[k]), float(pc.ms[k + 1]), g),
                              ((int(pc.main), float(pc.ms[k])),)))
                break                                   # one note a piece: the first step over
    return out
