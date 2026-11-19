"""Check 1, Paths: the members and how they meet and cross, recomputed from the lines alone."""
import numpy as np

import dnp
from ix import artifacts
from ixcheck import geom


def check(art, parents):
    if not isinstance(art, artifacts.Paths):
        return []
    ms = art.members
    lines = [np.asarray(m.line, np.float64) for m in ms]
    return (_keys(art, ms) + _broken(art, ms, lines) + _hangs(art, ms, lines) + _joins(art, ms, lines)
            + _crossings(art, ms, lines))


def _f(art, level, code, text, where=()):
    return artifacts.Finding(art.meta.step, level, code, text, where)


def _keys(art, ms):
    out, seen = [], {}
    for i, m in enumerate(ms):
        if m.key in seen:
            out.append(_f(art, "fail", "duplicate_member",
                          "member key %r on members %d and %d" % (m.key, seen[m.key], i)))
        else:
            seen[m.key] = i
    return out


def _broken(art, ms, lines):
    out = []
    for i, m in enumerate(ms):
        if len(lines[i]) < 2:
            out.append(_f(art, "fail", "broken_member", "member %s has %d point(s)" % (m.key, len(lines[i])),
                          ((i, 0.0),)))
            continue
        seg = np.diff(lines[i], axis=0)
        L = dnp.hypot(seg[:, 0], seg[:, 1])
        k = int(np.argmin(L))
        if float(L[k]) <= 1e-9:
            s = float(geom.arclen(lines[i])[k])
            out.append(_f(art, "fail", "broken_member",
                          "member %s has a zero-length segment at s=%.1f" % (m.key, s), ((i, s),)))
    return out


def _hangs(art, ms, lines):
    out = []
    for i, m in enumerate(ms):
        if m.kind not in artifacts.STRUCTURES or not len(lines[i]):
            continue
        s_all = geom.arclen(lines[i])
        for end, s in ((lines[i][0], 0.0), (lines[i][-1], float(s_all[-1]))):
            best, other = float("inf"), ""
            for j in range(len(lines)):
                if j == i or len(lines[j]) < 2:
                    continue
                d = float(np.min(geom.line_dist(end, lines[j])))
                if d < best:
                    best, other = d, str(ms[j].key)
            if best > geom.JOIN_M:
                out.append(_f(art, "fail", "hanging_end",
                              "member %s ends in the air at s=%.1f: %.1f m from %s, a join is %s m"
                              % (m.key, s, best, other, geom.JOIN_M), ((i, s),)))
    return out


def _joins(art, ms, lines):
    out = []
    for jn in art.joins:
        if not (0 <= jn.a < len(lines) and 0 <= jn.b < len(lines) and len(lines[jn.a]) and len(lines[jn.b])):
            out.append(_f(art, "fail", "bad_join", "a join names members %d and %d of %d"
                          % (jn.a, jn.b, len(lines))))
            continue
        d = geom.dist(geom.at_s(lines[jn.a], jn.sa), geom.at_s(lines[jn.b], jn.sb))
        if d > geom.JOIN_M:
            out.append(_f(art, "fail", "bad_join",
                          "join of %s at s=%.1f and %s at s=%.1f stands %.1f m apart, a join is %s m"
                          % (ms[jn.a].key, jn.sa, ms[jn.b].key, jn.sb, d, geom.JOIN_M),
                          ((jn.a, jn.sa), (jn.b, jn.sb))))
    return out


def _crossings(art, ms, lines):
    out = []
    for pc in art.crossings:
        if not (0 <= pc.a < len(lines) and 0 <= pc.b < len(lines) and len(lines[pc.a]) and len(lines[pc.b])):
            out.append(_f(art, "fail", "bad_crossing", "a crossing names members %d and %d of %d"
                          % (pc.a, pc.b, len(lines))))
            continue
        pa, pb = geom.at_s(lines[pc.a], pc.sa), geom.at_s(lines[pc.b], pc.sb)
        d, dq = geom.dist(pa, pb), geom.dist(pa, np.asarray(pc.q, np.float64))
        if max(d, dq) > geom.CROSS_M:
            out.append(_f(art, "fail", "bad_crossing",
                          "crossing of %s at s=%.1f and %s at s=%.1f: the points %.1f m apart, q %.1f m off, "
                          "the agreement is %s m" % (ms[pc.a].key, pc.sa, ms[pc.b].key, pc.sb, d, dq, geom.CROSS_M),
                          ((pc.a, pc.sa), (pc.b, pc.sb))))
    return out
