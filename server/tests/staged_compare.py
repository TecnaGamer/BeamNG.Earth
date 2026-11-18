"""The staged solver against today's pipeline, per place: pass / fail of stages 1 to 4 with the top failures, the number and size of
systems today (gradeshapes.find_systems) and staged (staged_ground: cut where the level resets), what each says about levels, and
which systems today's solver (gradeshapes.solve_shape, the kernel) leaves as infeasible against what stage 4 says (exact).

    python tests/staged_compare.py [--solve] [--out DIR] [NAME ...]
NAME: shanghai_13719_6695 | shanghai_13716_6692 | shanghai_nanpu_spiral (the dumps in $STAGED_FIXTURES), a replay of tests/interchanges.py
(popoli_ss17_underpass, a61_928 ...), a survey place of tests/truth.py (zurich_brunau ...); none: every one that is there.
--solve runs today's kernel on each of today's systems (minutes for a Shanghai region; the kernel gives up on a system it cannot
satisfy and says so, as the server logs "SHAPES infeasible system"). Offline."""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import numpy as np  # noqa: E402

import gradeshapes as gs  # noqa: E402
import staged  # noqa: E402
import staged_places as places  # noqa: E402
import staged_feasible as s4  # noqa: E402

REGIONS = ("shanghai_13719_6695", "shanghai_13716_6692", "shanghai_nanpu_spiral")
REPLAYS = ("popoli_ss17_underpass", "a61_928", "a20_921", "bologna_929", "castlegar_939", "italy_967", "m1_ireland_960",
           "perth_915", "prague_924", "silistra_936")
TRUTH = ("zurich_brunau", "zurich_europabruecke", "zurich_duttweilerbruecke", "goeschenen_gotthard_portal", "kempttal_viaduct")


def load(name):
    if name in REGIONS:
        return places.region(name)
    if name in REPLAYS:
        return places.replay(name)
    if name in TRUTH:
        return places.truth(name)
    raise SystemExit("unknown place %s" % name)


def today_solver(pl):
    """{today's system index: (solved, status)}: gradeshapes.solve_shape on each system, as the shapes pipeline asks it (the
    ground along each piece's samples, fixed_z the ground's)."""
    out = {}
    net = pl.net
    for n, sysm in enumerate(pl.today):
        pcs = gs.pieces(net, sysm)
        smp = gs.sample(net, pcs)
        gl = s4.ground_lists(pl.ground, net, smp)
        diag = {}
        t0 = time.perf_counter()
        zs = gs.solve_shape(net, sysm, pcs, smp, gl, pl.ground.fixed, diag)
        out[n] = (zs is not None, diag.get("qp_error") or diag.get("status") or "", time.perf_counter() - t0)
    return out


def verdicts(S, pl, solved):
    """Today's solver's infeasible systems against stage 4: [(today index, seeds, solver said, staged systems it overlaps and what
    stage 4 says of each)]."""
    cl = S.P.cleaned
    staged_of = {}
    for n, sysm in enumerate(S.G.systems):
        for i in sysm["seeds"]:
            staged_of[cl.source[i]] = n
    rows = []
    for n, (ok, why, _t) in sorted(solved.items()):
        mine = sorted({staged_of[i] for i in pl.today[n]["seeds"] if i in staged_of})
        say = []
        for m in mine:
            st = S.feasible.get(m)
            say.append("%d: %s" % (m, "no data" if st is None else "feasible" if st["feasible"] else "NO HEIGHTS (%s)" % ", ".join(
                "%.2f m %s" % (c["short_m"], c["owner"]) for c in st["contradictions"][:3])))
        rows.append((n, len(pl.today[n]["seeds"]), ok, why, say))
    return rows


def one(name, solve=False, out=None):
    pl = load(name)
    if pl is None:
        return None
    t0 = time.perf_counter()
    S = staged.run(pl.members, pl.meta, pl.ll, pl.edge, pl.ground, name=name)
    took = time.perf_counter() - t0
    cmp_ = staged.compare(S, pl.today, pl.net)
    lines = [S.report(), "## Today's pipeline against the staged one", "",
             "staged run: %.1f s. Systems: today %d, staged %d." % (took, cmp_["today_systems"], cmp_["staged_systems"]), "",
             "| | systems (seeds, pieces), largest first |", "|---|---|",
             "| today | %s |" % cmp_["today_sizes"], "| staged | %s |" % cmp_["staged_sizes"], "",
             "today's systems split by the cut into: %s; staged systems that join several of today's: %s; seeds only today (a copy "
             "collapsed, or a bridge over nothing): %d; only staged: %d." % (cmp_["today_split_into"], cmp_["staged_joins_today"],
                                                                          cmp_["seeds_only_today"], cmp_["seeds_only_staged"]),
             "", "Who moves at the crossings both see: %d agree, %d differ, %d only staged." % (
                 cmp_["moves_agree"], cmp_["moves_differ"], cmp_["moves_unmatched"]), ""]
    lines += ["Levels the staged systems say (kind, level; most spans stacked): " + "; ".join(
        "system %d: %s, stack %s" % (f["index"], ", ".join("%s %g" % kl for kl in f["levels"][:6]), f["stack"])
        for f in sorted(S.facts, key=lambda f: -f["pieces"])[:5]), ""]
    if solve:
        solved = today_solver(pl)
        bad = [n for n, (ok, _w, _t) in solved.items() if not ok]
        lines += ["### Today's solver", "", "%d of %d systems left infeasible (the kernel's own verdict)." % (len(bad), len(solved)), ""]
        for n, k, ok, why, say in verdicts(S, pl, solved):
            if not ok:
                lines.append("- today's system %d (%d seeds): infeasible%s; stage 4 on the staged system: %s" % (
                    n, k, " (%s)" % why if why else "", "; ".join(say) or "not in a staged system"))
        lines.append("")
    text = "\n".join(lines) + "\n"
    if out:
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, name + ".md"), "w", encoding="utf-8") as fh:
            fh.write(text)
    summary = dict(name=name, stages={st: ("pass" if S.passed(st) else "FAIL %d" % len(S.fails(st))) for st in (1, 2, 3, 4)}, **cmp_)
    return S, text, summary


def main(argv):
    solve = "--solve" in argv
    argv = [a for a in argv if a != "--solve"]
    out = None
    if "--out" in argv:
        k = argv.index("--out")
        out = argv[k + 1]
        argv = argv[:k] + argv[k + 2:]
    names = argv or list(REGIONS + REPLAYS + TRUTH)
    for name in names:
        got = one(name, solve, out)
        if got is None:
            print(name, ": no data here, skipped")
            continue
        S, _text, summary = got
        print(name, summary["stages"], "systems today %d staged %d; largest %s -> %s" % (
            summary["today_systems"], summary["staged_systems"], summary["today_sizes"][:2], summary["staged_sizes"][:2]), flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
