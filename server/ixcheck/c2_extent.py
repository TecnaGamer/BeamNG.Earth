"""Check 2, Extent: each system's seeds, boundary and box, against the Paths it was made from."""
import numpy as np

from ix import artifacts
from ixcheck import geom

MAX_SEEDS = 60    # a system with more seeds is two interchanges or more, not one solve


def check(art, parents):
    if not isinstance(art, artifacts.Extent):
        return []
    out = []
    paths = parents.get(1)
    ms = paths.members if paths is not None else []
    lines = [np.asarray(m.line, np.float64) for m in ms]
    for sysm in art.systems:
        out += _seeds(art, sysm, ms, lines)
        out += _boundary(art, sysm, ms, lines)
    return out


def _f(art, level, code, text, where=()):
    return artifacts.Finding(art.meta.step, level, code, text, where)


def _seeds(art, sysm, ms, lines):
    out = []
    for i in sysm.seeds:
        if not (0 <= i < len(ms)):
            out.append(_f(art, "fail", "bad_seed", "system %s seeds member %d of %d" % (sysm.key, i, len(ms))))
        elif ms[i].kind not in artifacts.STRUCTURES:
            out.append(_f(art, "fail", "bad_seed", "system %s seeds %s, a %s, not a structure"
                          % (sysm.key, ms[i].key, ms[i].kind), ((i, 0.0),)))
    if len(sysm.seeds) > MAX_SEEDS:
        keys = ", ".join(str(ms[i].key) for i in sysm.seeds if 0 <= i < len(ms))
        out.append(_f(art, "fail", "oversized_system", "system %s has %d seeds: %s"
                      % (sysm.key, len(sysm.seeds), keys[:400]), tuple((i, 0.0) for i in sysm.seeds)))
    if not len(sysm.boundary):
        out.append(_f(art, "note", "no_boundary", "system %s meets no road on the ground" % sysm.key))
    out += _box(art, sysm, ms, lines)
    return out


def _box(art, sysm, ms, lines):
    x0, y0, x1, y1 = (float(v) for v in sysm.box)
    out = []
    for i in sysm.seeds:
        if not (0 <= i < len(lines)) or not len(lines[i]):
            continue
        ln = lines[i]
        off = max(x0 - float(ln[:, 0].min()), float(ln[:, 0].max()) - x1,
                  y0 - float(ln[:, 1].min()), float(ln[:, 1].max()) - y1)
        if off > 1e-9:
            out.append(_f(art, "fail", "bad_box", "system %s's box (%.0f, %.0f, %.0f, %.0f) misses member %s by "
                          "%.1f m" % (sysm.key, x0, y0, x1, y1, ms[i].key, off), ((i, 0.0),)))
    return out


def _boundary(art, sysm, ms, lines):
    out = []
    for b in sysm.boundary:
        if not (0 <= b.i < len(ms) and 0 <= b.j < len(ms)):
            out.append(_f(art, "fail", "bad_boundary", "system %s's boundary names members %d and %d of %d"
                          % (sysm.key, b.i, b.j, len(ms))))
            continue
        if ms[b.j].kind not in ("ground", "rail"):
            out.append(_f(art, "fail", "bad_boundary", "system %s's boundary member %s is a %s, not a road on the "
                          "ground" % (sysm.key, ms[b.j].key, ms[b.j].kind), ((b.i, b.si), (b.j, b.sj))))
            continue
        d = geom.dist(geom.at_s(lines[b.i], b.si), geom.at_s(lines[b.j], b.sj))
        if d > geom.JOIN_M:
            out.append(_f(art, "fail", "bad_boundary", "system %s's boundary: %s at s=%.1f stands %.1f m from %s at "
                          "s=%.1f, a join is %s m" % (sysm.key, ms[b.i].key, b.si, d, ms[b.j].key, b.sj, geom.JOIN_M),
                          ((b.i, b.si), (b.j, b.sj))))
    return out
