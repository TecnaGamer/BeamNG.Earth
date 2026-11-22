"""Step 4 (SPEC.md): the ground the solve reads, as the Terrain artifact: at each of the system's samples (and its trust:
1 where the ground is finite, 0 where it is not), and the heights of the members the shape does not move, at the arcs the
solve asks them (a road on the ground under a deck, a track: gradeshapes.fixed_reads). Every read of the ground and of the
tracks' tops the heights need is made here, once; the heights step is made from what was read."""
import numpy as np

import gradeshapes as _gsh

from . import artifacts, cache

FORMAT = 4
LAWS = (("gradeshapes", ["STEP_M"]),)
LAWS_DIGEST = cache.laws_digest(*LAWS)
STEP = 4


def ground_of(planner, smp):
    """The ground the solve reads: _ground_line of each piece's samples."""
    import xproc
    out = []
    for x in smp:
        xproc.check_cancel()        # (a piece at a time: the step's time limit, ix/limits.py)
        out.append(planner._ground_line(x["xy"]))
    return out


def read_fixed(net, sysm, pcs, smp, fixed_z):
    """{(member of net, arc): height}: fixed_z (the planner's: the ground along the road, a track's top) asked at every arc
    the problem reads (gradeshapes.fixed_reads); NaN where it is not known."""
    table = {}
    reads = _gsh.fixed_reads(sysm, pcs, smp)
    prefetch = getattr(fixed_z, "prefetch", None)
    if prefetch is not None:        # (the railways read once for all the tracks' arcs)
        prefetch({i: sv.tolist() for i, sv in reads.items()})
    for i, sv in reads.items():
        zs = np.asarray(fixed_z(i, sv), np.float64)
        for s_, z_ in zip(sv.tolist(), zs.tolist()):
            table[(i, s_)] = z_
    return table


def lookup(table):
    """fixed_z(i, s) of the heights step: the heights terrain read (read_fixed), at the arcs it read. An arc it did not
    read is a bug of the problem's asking, not something to read again here."""
    def fixed_z(i, s_):
        sv = np.atleast_1d(np.asarray(s_, np.float64))
        z = np.array([table[(int(i), float(v))] for v in sv.tolist()])
        return z if np.ndim(s_) else float(z[0])
    return fixed_z


def run(planner, net, sysm, pcs, smp, loc, fixed_z, system, scope):
    """(Terrain, ground, table): the ground the solve will read at the samples, its trust, and the heights read of the members
    it does not move (the table: lookup's; the artifact's `fixed` numbers the members as the Paths'), with fixed_z the
    planner's fixed heights."""
    ground = ground_of(planner, smp)
    g = [np.asarray(z, np.float64) for z in ground]
    table = read_fixed(net, sysm, pcs, smp, fixed_z)
    by = {}
    for (i, s_), z_ in table.items():
        by.setdefault(i, []).append((s_, z_))
    fixed = [artifacts.Fixed(int(loc[i]), np.array([a for a, _b in sorted(v)], np.float64),
                             np.array([b for _a, b in sorted(v)], np.float64))
             for i, v in sorted(by.items(), key=lambda kv: loc[kv[0]])]
    return (artifacts.Terrain(artifacts.Meta(STEP, FORMAT, "", (), LAWS_DIGEST, scope), system, g,
                              [np.where(np.isfinite(z), 1.0, 0.0) for z in g], fixed=fixed), ground, table)
