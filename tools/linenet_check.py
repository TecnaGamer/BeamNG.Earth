#!/usr/bin/env python3
"""The line network (server/linenet.py, earthfast.LineNet) against the Python geometry on a real place: the members of a block of
map cells, as an interchange's growth reads them, and every seed's relations (the decks' and short tunnels', what the
find asks) from both, compared to the bit; with the seconds of each.

    python tools/linenet_check.py --anchor 35.46,139.68 --cells 14/14542/6459:14/14552/6468     (Yokohama's viaducts)
    python tools/linenet_check.py ... --all 20      (also every 20th member that is no seed: roads, tracks)

The map data comes from Earth's cache (offline: nothing is fetched; --cache). Exit 1 on a difference."""
import argparse
import os
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(HERE), "server")


def same(a, b):
    import numpy as np
    if isinstance(a, np.ndarray) or isinstance(b, np.ndarray):
        a, b = np.asarray(a), np.asarray(b)
        return a.dtype == b.dtype and a.shape == b.shape and a.tobytes() == b.tobytes()
    if isinstance(a, float) or isinstance(b, float):
        return isinstance(a, float) and isinstance(b, float) and struct.pack("<d", a) == struct.pack("<d", b)
    if isinstance(a, (tuple, list)):
        return isinstance(b, (tuple, list)) and len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    return a == b


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--anchor", required=True, help="lat,lon of the frame")
    ap.add_argument("--aid", type=int, default=9)
    ap.add_argument("--cells", required=True, help="z/x0/y0:z/x1/y1, the block of cells read")
    ap.add_argument("--all", type=int, default=0, help="also every Nth member that is no seed")
    ap.add_argument("--cache", default=os.path.join(os.path.expanduser("~"), ".cache", "beamng-earth"))
    a = ap.parse_args()
    os.environ.update(BNE_SHAPES="1", BNGE_PLANCACHE="off", BNGE_BLOCKCACHE="off")
    sys.path.insert(0, SERVER)
    import numpy as np
    import blockworker
    import gradeshapes as gsh
    import linenet
    import mapcells
    from ix import s1_paths

    la, lo = (float(v) for v in a.anchor.split(","))
    (z, x0, y0), (_z, x1, y1) = ([int(v) for v in c.split("/")] for c in a.cells.split(":"))
    state = blockworker._State(a.cache, True, True, True, True, True, airports=False, sea=False)
    blockworker._s = state
    state.anchor(a.aid, la, lo)
    planner = state.plan_store.planner_of_cell(mapcells.CellKey(z, (x0 + x1) // 2, (y0 + y1) // 2))
    planner._over_mapped_underpass = lambda b: False        # (Member.held: no part of a pair's relations)
    keys = [mapcells.CellKey(z, x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]
    t = time.perf_counter()
    byk, loaded = {}, set()
    for k in keys:
        loaded.add(k)
        for m in planner._shape_members(k):
            old = byk.get(m.key)
            if old is None or len(m.line) > len(old.line):
                byk[m.key] = m
    members = gsh.one_road([byk[q] for q in sorted(byk, key=str)])
    t_read = time.perf_counter() - t

    t = time.perf_counter()
    net = gsh.Net(members, linenet.Network())
    t_net = time.perf_counter() - t
    seeds = [i for i in range(len(members)) if len(net.lines[i]) >= 2 and gsh._is_seed(net, i)]
    ask = list(seeds)
    if a.all:
        ask += [i for i in range(0, len(members), a.all) if len(net.lines[i]) >= 2 and not gsh._is_seed(net, i)]

    # (the Python geometry, no network: each member against those whose boxes come within the pad of its own)
    t = time.perf_counter()
    bx, pad = net.box, gsh.JOIN_M + 0.5
    want = {}
    for i in ask:
        b = bx[i]
        near = np.flatnonzero((bx[:, 0] <= b[2] + pad) & (bx[:, 2] >= b[0] - pad) & (bx[:, 1] <= b[3] + pad)
                              & (bx[:, 3] >= b[1] - pad))
        want[i] = [(j,) + r_ for j in near.tolist() if j != i and len(net.lines[j]) >= 2
                   for r_ in gsh._pair_relations(net, i, j)]
    t_py = time.perf_counter() - t

    t = time.perf_counter()
    got = {i: gsh._relations(net, i) for i in ask}
    t_nat = time.perf_counter() - t
    t = time.perf_counter()
    again = gsh.Net(members, net.network)
    t_again = time.perf_counter() - t
    t = time.perf_counter()
    n_geo = sum(len(net.network.ln.contacts(q, gsh.CROSS_END_M, gsh.JOIN_M)) for q in range(len(net.network.ln)))
    t_every = time.perf_counter() - t

    bad = [i for i in ask if not same(got[i], want[i])]
    n_rel = sum(len(v) for v in want.values())
    print("cells %d, members %d (%d points), seeds %d; read %.1f s" % (len(keys), len(members),
          sum(len(l_) for l_ in net.lines), len(seeds), t_read))
    print("relations asked of %d members, %d relations: %s" % (len(ask), n_rel,
          "the same to the bit" if not bad else "%d DIFFER: %s" % (len(bad), bad[:10])))
    print("Python geometry (box candidates, _pair_relations): %.2f s" % t_py)
    print("line network: a Net of every member, its lines added and paired %.2f s (%d near pairs), the same Net again "
          "%.2f s; relations %.2f s" % (t_net, net.network.ln.pairs, t_again, t_nat))
    print("line network: the geometry of every line with everything it touches: %.2f s (%d touching pairs)"
          % (t_every, n_geo))
    for i in bad[:3]:
        print("  member %d %r" % (i, members[i].key))
        print("    python  %r" % (want[i][:4],))
        print("    network %r" % (got[i][:4],))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
