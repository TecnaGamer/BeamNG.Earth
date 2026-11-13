"""Assemble source-coincident underwater bore chains for the shared native constrained profile solver.

Dry tunnel rules remain in roads.py. This module packs the geometry/height-source inputs; all numerical
optimization runs in the exact Rust QP core also proposed for carriageways in PR #814.
"""
import logging
import math

import numpy as np

import carriageways
import fast
import dnp

log = logging.getLogger("earthd.roads")
GRADE = .08                # maximum modeled grade; an engineering bound, not a survey
K = 20.                     # model vertical-curve parameter, metres per percent change in grade
STEP = 4.
COVER = 8.


def node(p):
    return round(float(p[0]), 6), round(float(p[1]), 6)


def profiles(owner, comp, pool):
    try:
        return _profiles(owner, comp, pool)
    except (ValueError, RuntimeError, OSError) as e:
        log.warning("subsea inputs/solver unavailable: %s", e)
        return None


def _profiles(owner, comp, pool):
    """{part key: floor array} and actual wet-chain membership; None on an unsolved/missing-input bore."""
    if owner.water is None:
        return None
    keys = sorted(comp)
    parts = [(k, np.asarray(pool[k][1], np.float64)) for k in keys]
    sequences = carriageways.chains(parts, tol=1e-6)
    chains, locations, ends = [], {}, {}
    for seq in sequences:
        pieces, offset = [], 0.
        for index, reverse in seq:
            key, xy = parts[index]
            ss = np.r_[0., np.cumsum(dnp.hypot(*np.diff(xy, axis=0).T))]
            if len(xy) < 2 or ss[-1] <= 0:
                return None
            locations[key] = (len(chains), offset + (ss[-1] - ss if reverse else ss))
            q = xy[::-1] if reverse else xy
            pieces.append(q if not pieces else q[1:])
            offset += ss[-1]
        xy = np.concatenate(pieces)
        arc = np.r_[0., np.cumsum(dnp.hypot(*np.diff(xy, axis=0).T))]
        if arc[-1] <= 0 or node(xy[0]) == node(xy[-1]):
            log.warning("subsea chain has no identifiable portals")
            return None
        s = np.linspace(0., arc[-1], max(2, math.ceil(arc[-1] / STEP) + 1))
        q = np.column_stack([np.interp(s, arc, xy[:, i]) for i in range(2)])
        for at in (0, -1):
            ends.setdefault(node(q[at]), []).append((len(chains), at))
        ground = np.asarray((owner._raw_ground if owner.raw is not None else owner._ground)(q), np.float64)
        lat, lon, _ = owner.frame.enu_to_geodetic(q[:, 0], q[:, 1], 0.)
        depth, level = owner.water(lat, lon)[:2]
        depth, level = np.asarray(depth, np.float64), np.asarray(level, np.float64)
        if ((depth > .5) & ~np.isfinite(level)).any():
            log.warning("subsea profile has no water surface datum")
            return None
        level = np.asarray(owner.frame.geodetic_to_enu(lat, lon, np.where(np.isfinite(level), level, 0.))[2])
        # Pin to the common raw ground datum. Asking the grader for the endpoints here would recurse into
        # its own tunnel plans. Open approaches are graded to this very same solved floor afterwards.
        portal = ground[[0, -1]]
        if not np.isfinite(ground).all() or not np.isfinite(depth).all():
            log.warning("subsea profile has incomplete ground/water inputs")
            return None
        chains.append(dict(xy=q, s=s, ground=ground, depth=depth, level=level, portal=portal,
                           name=next((pool[parts[i][0]][5] for i, _ in seq if len(pool[parts[i][0]]) > 5 and pool[parts[i][0]][5]), ""),
                           width=max(pool[parts[i][0]][0].width for i, _ in seq)))
    # The resolver can gather nearby parallel/crossing bores. Only coincident source ends are topology here.
    groups = list(range(len(chains)))
    def root(i):
        while groups[i] != i:
            groups[i] = groups[groups[i]]; i = groups[i]
        return i
    for adjacent in ends.values():
        for a, _ in adjacent[1:]: groups[root(a)] = root(adjacent[0][0])
    free = {root(i): [] for i in range(len(chains))}
    for adjacent in ends.values():
        if len(adjacent) == 1:
            i, at = adjacent[0]; free[root(i)].append((i, at))
    eligible = set()
    for group in free:
        portals = free[group]
        if len(portals) < 2:
            if any((c["depth"] > .5).any() for i, c in enumerate(chains) if root(i) == group):
                log.warning("subsea component has fewer than two mapped portals")
                return None
            continue
        low = min(chains[i]["portal"][0 if at == 0 else 1] for i, at in portals)
        for i, c in enumerate(chains):
            if root(i) != group: continue
            c["wet"] = (c["depth"] > .5) & (c["ground"] - low <= 60.)
            if c["wet"].any(): eligible.add(group)
    active = [i for i in range(len(chains)) if root(i) in eligible]
    membership = {k: root(i) in eligible for k, (i, _) in locations.items()}
    if not active:
        return {}, membership
    local = {i: j for j, i in enumerate(active)}
    offsets = np.r_[0, np.cumsum([len(chains[i]["s"]) for i in active])].astype(np.int64)
    targets, weights, lowers, uppers, params = [], [], [], [], []
    for i in active:
        c = chains[i]; s = c["s"]; wet = c["wet"]
        t = np.interp(s, [0., s[-1]], c["portal"])
        t = np.where(wet, c["ground"] - COVER, t)
        w = np.where(wet, 1., .2)
        upper = np.where(wet, c["ground"] - COVER, c["ground"])
        # Surveyed road heights take precedence over an inferred bathymetric bed. Do not infer a survey from OSM.
        import roadz
        provider = roadz.PROVIDER
        if provider is not None and provider.enabled:
            try:
                observed = provider.observed(owner.frame, c["xy"], c["width"] / 2, -1, c["name"])
            except Exception as e:
                log.debug("subsea survey unavailable: %s: %s", type(e).__name__, e)
                observed = None
            if observed is not None:
                observed = np.asarray(observed, np.float64)
                ok = np.isfinite(observed)
                if ok.mean() >= .9:
                    observed = np.interp(s, s[ok], observed[ok])
                    la, lo, _ = owner.frame.enu_to_geodetic(c["xy"][:, 0], c["xy"][:, 1], 0.)
                    observed = np.asarray(owner.frame.geodetic_to_enu(la, lo, observed)[2])
                    t, w = observed, np.full(len(s), 100.)
                    upper = np.where(wet, np.minimum(c["level"] - COVER,
                                                    np.maximum(c["ground"] - COVER, observed + .05)), upper)
        lower = np.full(len(s), -np.inf)
        for at, p in [(0, c["portal"][0]), (-1, c["portal"][1])]:
            if len(ends[node(c["xy"][at])]) == 1:
                if c["depth"][at] > .5:
                    log.warning("subsea chain ends under water; awaiting its missing source continuation")
                    return None
                lower[at] = upper[at] = p
        targets.append(t); weights.append(w); lowers.append(lower); uppers.append(upper)
        params.append([GRADE, K, 2.e4, s[-1] / (len(s) - 1), 200.])
    rows = []
    for adjacent in ends.values():
        selected = [(local[i], at) for i, at in adjacent if i in local]
        for a in selected[1:]: rows.append([a[0], a[1], selected[0][0], selected[0][1], 0])
    pack = lambda a: np.ascontiguousarray(np.concatenate(a), np.float64)
    fast.require(156, "subsea profiles")
    z, info = fast._ef.carriageway_profiles(pack(targets), pack(weights), pack(lowers), pack(uppers), offsets,
        np.asarray(params, np.float64), np.asarray(rows, np.int64).reshape(-1, 5), np.zeros(len(rows)), 150, 30.)
    if not info["converged"] or not info["feasible"]:
        log.warning("subsea profile unsolved: %s; grade %.6g, curve %.6g, bounds %.3g/%.3g m", info["status"],
                    info["grade_excess"], info["curve_excess"], info["lower_shortfall_m"], info["upper_excess_m"])
        return None
    result = {}
    for k, (i, s) in locations.items():
        if i in local:
            j = local[i]
            result[k] = np.interp(s, chains[i]["s"], z[offsets[j]:offsets[j + 1]])
    return result, membership
