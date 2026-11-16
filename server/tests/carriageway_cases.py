"""Deterministic numerical workloads, not surveyed geography or gameplay measurements."""
import numpy as np


def chain(n, base=20., ripple=0., phase=0., grade=.05, k=30.):
    s = np.arange(n, dtype=np.float64) * 4.
    t = base + ripple * np.sin(s / 60. + phase)
    w = np.full(n, .2)
    w[[0, -1]] = 5.
    return dict(t=t, w=w, grade=grade, K=k)


def cases():
    result = {}
    result["coarse_profile"] = ([chain(501, ripple=5.)], [])
    result["long_profile"] = ([chain(12001, ripple=.25)], [])
    # Branch points and parallel carriageways: many independent banded profiles, a shared start elevation.
    roads = [chain(81, base=20. + .002 * i) for i in range(150)]
    ties = [(i, 0, i - 1, 0, "eq", 0.) for i in range(1, len(roads))]
    result["junctions_150"] = (roads, ties)
    # Redundant junction equations must not require an unbounded dense Schur matrix.
    result["redundant_junctions_150"] = (roads, ties + [(i, 0, 0, 0, "eq", 0.) for i in range(2, len(roads))])
    # Five stacked levels, constrained over a corridor rather than at a single crossing sample.
    stack = [chain(301) for _ in range(5)]
    for i, road in enumerate(stack):
        road["lo"] = np.full(301, -np.inf)
        road["hi"] = np.full(301, np.inf)
        if i == 0:
            road["lo"].fill(20.); road["hi"].fill(20.)
    seps = [(i, j, i - 1, j, "ge", 7.6) for i in range(1, 5) for j in range(135, 166, 5)]
    result["stacked_corridor"] = (stack, seps)
    # Bound clearance along a shallow-angle overlap, rather than only at its centre.
    overlap = [chain(401), chain(401)]
    overlap[0]["lo"] = np.full(401, 20.)
    overlap[0]["hi"] = np.full(401, 20.)
    result["shallow_overlap"] = (overlap, [(1, j, 0, j, "ge", 7.6) for j in range(120, 281)])
    return result


def residuals(roads, couplings, profiles):
    """Independent measurement of the actual returned profiles, including exhausted/soft constraints."""
    out = dict(grade_excess=0., curve_excess=0., bound_error_m=0., junction_gap_m=0., clearance_shortfall_m=0.)
    for c, z in zip(roads, profiles):
        step = c.get("step", 4.)
        if len(z) > 1:
            out["grade_excess"] = max(out["grade_excess"], float(np.abs(np.diff(z)).max() / step - c["grade"]))
        if len(z) > 2:
            out["curve_excess"] = max(out["curve_excess"], float(np.abs(np.diff(z, 2)).max() / step ** 2 - 1 / (c["K"] * 100)))
        if c.get("lo") is not None:
            out["bound_error_m"] = max(out["bound_error_m"], float(np.max(np.asarray(c["lo"]) - z)))
        if c.get("hi") is not None:
            out["bound_error_m"] = max(out["bound_error_m"], float(np.max(z - np.asarray(c["hi"]))))
    for a, i, b, j, kind, value in couplings:
        gap = float(profiles[a][i] - profiles[b][j])
        if kind == "eq": out["junction_gap_m"] = max(out["junction_gap_m"], abs(gap))
        else: out["clearance_shortfall_m"] = max(out["clearance_shortfall_m"], value - gap)
    return out
