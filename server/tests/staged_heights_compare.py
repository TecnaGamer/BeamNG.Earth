"""Stage 5 (staged_heights) against today's kernel on a place's staged systems: per system and in all, the time, the iterations and
residuals of ours, and what each answer is: the heights at the crossings (the clearance each gives), the grade excess, the curvature
excess, the objective (the shapes' own, measured on both), how far the kernel's answer breaks our constraints.

    python tests/staged_heights_compare.py [--out DIR] [--max-pieces N] [NAME ...]
NAME as tests/staged_compare.py (shanghai_13719_6695, popoli_ss17_underpass, zurich_brunau ...). Offline."""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import numpy as np  # noqa: E402

import gradeshapes as gs  # noqa: E402
import staged  # noqa: E402
import staged_compare as sc  # noqa: E402
import staged_feasible as s4  # noqa: E402
import staged_heights as h5  # noqa: E402


def clearance_at(cons, zs):
    """The clearance each crossing gets: (z upper - z lower) at the crossing's own samples less what it needs, per crossing of the
    system's record: the least over the crossings, and the mean of the margins."""
    margins = []
    for ci, c in enumerate(cons.sysm["crossings"]):
        need = gs.clearance_need(cons.net, c["u"], c["d"], c["q"])
        pu = gs._var(cons.pcs, cons.smp, c["u"], c["su"])
        pd = gs._var(cons.pcs, cons.smp, c["d"], c["sd"])
        if pu is None or pd is None:
            continue
        zu = zs[pu[0]][pu[1]] + (zs[pu[0]][pu[2]] - zs[pu[0]][pu[1]]) * pu[3]
        zd = zs[pd[0]][pd[1]] + (zs[pd[0]][pd[2]] - zs[pd[0]][pd[1]]) * pd[3]
        margins.append(float(zu - zd) - need)
    return margins


def one_system(S, n, with_kernel=True):
    net = S.net
    sysm = S.G.systems[n]
    pcs = gs.pieces(net, sysm)
    if not pcs:
        return None
    smp = gs.sample(net, pcs)
    gl = s4.ground_lists(S.ground, net, smp)
    cons = h5.constraints_of(net, sysm, pcs, smp, gl, S.ground.fixed, S.ground.at)
    t0 = time.perf_counter()
    H = h5.solve_constraints(cons)
    took = time.perf_counter() - t0
    row = dict(system=n, pieces=len(pcs), samples=int(cons.off[-1]), ours=H.stats, ours_s=took)
    if H.zs is None:
        return row
    P = h5.Problem(cons)
    zo = np.concatenate([np.asarray(z) for z in H.zs])
    row["clear_ours"] = clearance_at(cons, H.zs)
    if with_kernel:
        diag = {}
        t0 = time.perf_counter()
        zk = gs.solve_shape(net, sysm, pcs, smp, gl, S.ground.fixed, diag)
        row["kernel_s"] = time.perf_counter() - t0
        row["kernel_ok"] = zk is not None
        if zk is not None:
            zkc = np.concatenate([np.asarray(z) for z in zk])
            row["kernel_obj"] = P.objective(zkc)
            row["kernel_violation_m"], _ = P.violation(zkc)
            row["kernel_measure"] = h5.measure(cons, zk)
            row["max_dz"] = float(np.abs(zo - zkc).max())
            row["mean_dz"] = float(np.abs(zo - zkc).mean())
            row["clear_kernel"] = clearance_at(cons, zk)
    return row


def run(name, max_pieces=None, with_kernel=True):
    pl = sc.load(name)
    if pl is None:
        return None
    S = staged.run(pl.members, pl.meta, pl.ll, pl.edge, pl.ground, name=name, feasibility=True, endinfo=getattr(pl, "endinfo", None))
    rows = []
    for n, sysm in enumerate(S.G.systems):
        st = S.feasible.get(n)
        if st is None or not st["feasible"]:
            rows.append(dict(system=n, pieces=len(gs.pieces(S.net, sysm)), infeasible=True,
                             short_m=st and st.get("short_m")))
            continue
        if max_pieces and len(gs.pieces(S.net, sysm)) > max_pieces:
            rows.append(dict(system=n, pieces=len(gs.pieces(S.net, sysm)), skipped=True))
            continue
        rows.append(one_system(S, n, with_kernel))
    return S, [r for r in rows if r]


def summary(name, rows):
    ok = [r for r in rows if r.get("ours")]
    conv = [r for r in ok if r["ours"].get("converged")]
    lines = ["## %s: %d staged systems, %d solved by stage 5, %d converged, %d infeasible (stage 4), %d skipped" % (
        name, len(rows), len(ok), len(conv), sum(1 for r in rows if r.get("infeasible")), sum(1 for r in rows if r.get("skipped"))), ""]
    if not ok:
        return "\n".join(lines) + "\n"

    def agg(key, f=max, default=0.0):
        return f([r["ours"].get(key, default) for r in ok] + [default]) if ok else default
    lines.append("stage 5: time %.1f s in all (slowest %.1f s on %d samples), iterations %d at most (median %d), worst constraint violated by "
                 "%.2g m, worst grade excess %.2g, curve excess %.2g m, clearance shortfall %.2g m" % (
                     sum(r["ours_s"] for r in ok), max(r["ours_s"] for r in ok), max(r["samples"] for r in ok),
                     agg("iterations"), sorted(r["ours"].get("iterations", 0) for r in ok)[len(ok) // 2], agg("violation_m"),
                     agg("grade_excess"), agg("curve_excess_m"), agg("clearance_shortfall_m")))
    kern = [r for r in ok if "kernel_s" in r]
    if kern:
        kok = [r for r in kern if r.get("kernel_ok")]
        lines.append("kernel: %d of %d systems solved, %.1f s in all (slowest %.1f s)" % (
            len(kok), len(kern), sum(r["kernel_s"] for r in kern), max(r["kernel_s"] for r in kern)))
        if kok:
            worse = [r for r in kok if r["ours"]["objective"] > r["kernel_obj"] * (1 + 1e-3) + 1e-3]
            lines.append("against the kernel's heights (%d systems both solved): largest difference %.3f m (mean %.4f m), our objective "
                         "above the kernel's by more than 0.1%% on %d (the kernel's answer breaks our constraints by up to %.3g m), "
                         "the kernel's grade excess %.2g, curve excess %.2g m, clearance shortfall %.2g m; the least clearance margin "
                         "ours %.2f m, kernel's %.2f m" % (
                             len(kok), max(r["max_dz"] for r in kok), float(np.mean([r["mean_dz"] for r in kok])), len(worse),
                             max(r["kernel_violation_m"] for r in kok), max(r["kernel_measure"]["grade_excess"] for r in kok),
                             max(r["kernel_measure"]["curve_excess_m"] for r in kok),
                             max(r["kernel_measure"]["clearance_shortfall_m"] for r in kok),
                             min([min(r["clear_ours"]) for r in kok if r.get("clear_ours")] + [0.0]),
                             min([min(r["clear_kernel"]) for r in kok if r.get("clear_kernel")] + [0.0])))
    bad = [r for r in ok if not r["ours"].get("converged")]
    for r in bad[:6]:
        lines.append("- system %d (%d pieces) not converged: %d iterations, violation %.3g m, residuals %.2g / %.2g" % (
            r["system"], r["pieces"], r["ours"].get("iterations", 0), r["ours"].get("violation_m", 0.0),
            r["ours"].get("primal_residual_m", 0.0), r["ours"].get("dual_residual_m", 0.0)))
    return "\n".join(lines) + "\n"


def main(argv):
    out = None
    maxp = None
    kern = "--no-kernel" not in argv
    argv = [a for a in argv if a != "--no-kernel"]
    if "--out" in argv:
        k = argv.index("--out")
        out = argv[k + 1]
        argv = argv[:k] + argv[k + 2:]
    if "--max-pieces" in argv:
        k = argv.index("--max-pieces")
        maxp = int(argv[k + 1])
        argv = argv[:k] + argv[k + 2:]
    names = argv or list(sc.REGIONS + sc.REPLAYS + sc.TRUTH)
    text = []
    for name in names:
        got = run(name, maxp, kern)
        if got is None:
            print(name, ": no data here, skipped")
            continue
        S, rows = got
        s = summary(name, rows)
        print(s, flush=True)
        text.append(s)
    if out:
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with open(out, "w", encoding="utf-8") as fh:
            fh.write("\n".join(text))


if __name__ == "__main__":
    main(sys.argv[1:])
