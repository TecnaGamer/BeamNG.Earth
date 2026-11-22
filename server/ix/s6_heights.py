"""Step 6 (SPEC.md): the pieces' samples and the solve, and the embankments and profiles, as the Heights and Region
artifacts. An interchange is solved once for the run (ix/owner.py: its owner), so there is no solve cache here."""
import logging

import numpy as np

import dnp
import gradeshapes as _gsh

from . import artifacts, cache, s4_terrain

log = logging.getLogger("earthd.grading")

FORMAT = 3
LAWS = (("gradeshapes", ["STEP_M", "DECK_MAX_GRADE", "DECK_AIR_M", "SOFT_M", "DIP_MARGIN_M", "RAMP_MAX_M", "W_END",
                         "W_HOLD", "W_DIP", "W_SMOOTH", "W_STIFF", "W_JOIN"]),)
LAWS_DIGEST = cache.laws_digest(*LAWS)
STEP = 6


def solve(net, sysm, pcs, smp, fixed_z, ground):
    """(zs, diag) of one system: the heights of its pieces' samples (zs None: no answer) and the solver's account.
    ground: what the terrain step read at the samples."""
    diag = {}
    zs = _gsh.solve_shape(net, sysm, pcs, smp, ground, fixed_z, diag)
    return zs, diag


def _plain_value(v):
    """v as plain data (np scalars unwrapped, lists and dicts of plain values kept, anything else as its str)."""
    if isinstance(v, np.generic):
        v = v.item()
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (list, tuple)):
        return [_plain_value(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _plain_value(x) for k, x in v.items()}
    return str(v)


def _plain(diag):
    """The solver's diagnostics as plain values. A wall-clock time is not solver state: dropped, or no two runs would give
    the same bytes."""
    return {str(k): _plain_value(v) for k, v in diag.items() if str(k) != "elapsed_s"}


def relaxed_of(diag):
    """[dict(kind, amount, at, text)] of what the solve eased (gradeshapes.solve_shape: diag["relaxed"]), in the order it
    was found; the members in net's numbers."""
    return list(diag.get("relaxed") or ())


def relax_text(diag):
    """One line of what the solve eased, for the step's log line: '' where nothing was, else the count and each kind's
    total (clearance and floors in m, a grade law as the largest factor it was eased by)."""
    got = relaxed_of(diag)
    if not got:
        return ""
    parts = {}
    for r in got:
        parts.setdefault(r["kind"], []).append(r)
    out = []
    for kind in sorted(parts):
        rs = parts[kind]
        if kind == "grade":
            out.append("grade x%d (up to +%.2f%% of slope)" % (len(rs), 100.0 * max(r["amount"] for r in rs)))
        elif kind == "curve":
            out.append("curve x%d (radius divided by up to %.2f)" % (len(rs), max(r["amount"] for r in rs)))
        else:
            out.append("%s x%d (%.2f m at most, %.2f m in all)" % (kind, len(rs), max(r["amount"] for r in rs),
                                                                   sum(r["amount"] for r in rs)))
    return "%d eased: %s" % (len(got), "; ".join(out))


def run(planner, net, sysm, loc, pcs, smp, zs, diag, system, scope):
    """The Heights artifact of one system's solve (solved False: no answer, the system left as it is), the members
    numbered as the system's Paths' (loc)."""
    solved = zs is not None
    lut = np.full(len(net.m), -1, np.int64)
    for g, n in loc.items():
        lut[g] = n
    pieces = []
    for pc, x, z in zip(pcs, smp, zs if solved else [None] * len(pcs)):
        mem = lut[np.asarray(x["mem"], np.int64)]
        if (mem < 0).any():
            raise AssertionError("a sample of the system's pieces is on a member its Paths do not hold")
        pieces.append(artifacts.Piece(pc["kind"], int(loc[int(pc["main"])]), np.asarray(x["xy"], np.float64),
                                      np.asarray(x["d"], np.float64), mem,
                                      np.asarray(x["ms"], np.float64),
                                      np.full(len(x["xy"]), np.nan) if z is None else np.asarray(z, np.float64)))
    plain = _plain(diag)
    findings = []
    for r in relaxed_of(plain):
        at = tuple((int(loc[int(m)]), float(s_)) for m, s_ in r["at"] if int(m) in loc)
        findings.append(artifacts.Finding(STEP, "note", "relaxed_" + r["kind"], r["text"], at))
        r["at"] = [[int(loc[int(m)]), float(s_)] for m, s_ in r["at"] if int(m) in loc]
    return artifacts.Heights(artifacts.Meta(STEP, FORMAT, "", (), LAWS_DIGEST, scope), system, pieces, solved,
                             plain, findings)


def region_of(planner, net, solved, shapes, scope, beside=()):
    """(prof, Region): the embankments and the roads' profiles of the solved systems (their shapes: pieces, samples and
    heights each) -- what _shape_region returned, as data. beside: the pieces of the net's other systems, which the
    ramps of these stop at. scope: what the artifact is of (a region, an interchange)."""
    def ground_along(i, sv):
        xy = np.stack([dnp.interp(sv, net.s[i], net.lines[i][:, 0]), dnp.interp(sv, net.s[i], net.lines[i][:, 1])],
                      axis=1)
        return planner._ground_line(xy)

    emb = _gsh.embankments(net, solved, shapes, ground_along, beside)
    prof = {}
    for (pcs, smp, zs) in shapes:
        for pc, x, z in zip(pcs, smp, zs):
            if pc["kind"] == "deck":
                prof.setdefault(str(net.m[pc["main"]].key[0]), []).append((x["xy"], np.asarray(z, np.float64),
                                                                           "deck"))
    for j, lst in emb.items():
        m = net.m[j]
        if m.kind not in ("ground", "tunnel"):
            continue
        for sv, z, mode in lst:
            xy = np.stack([dnp.interp(sv, net.s[j], net.lines[j][:, 0]),
                           dnp.interp(sv, net.s[j], net.lines[j][:, 1])], axis=1)
            prof.setdefault(str(m.key[0]), []).append((xy, np.asarray(z, np.float64), mode))
    region = artifacts.Region(artifacts.Meta(STEP, FORMAT, "", (), LAWS_DIGEST, scope), str(scope),
                              [artifacts.Profile(str(fid), np.asarray(xy, np.float64), np.asarray(z, np.float64),
                                                 str(mode))
                               for fid, lst in prof.items() for (xy, z, mode) in lst])
    return prof, region
