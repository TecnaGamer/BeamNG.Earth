"""The interchange pipeline's orchestrator (SPEC.md): region(planner, home, hk) is what _Planner._shape_region_calc
returned, built by calling the s1..s6 steps in order for each interchange the region finds. This writes the artifacts
only -- it never reads them back to skip generation -- so with BNE_IX_CACHE unset nothing changes at all.

The steps of one interchange run in system(), one function, so whatever must hold across an interchange's steps (a
lock round them) has a single place to wrap: interchange() runs it for the owner (ix/owner.py), which holds the
interchange's lock round it, once for the run. Each step logs one line (step, key, size, seconds, ok or why it failed)
and region() one more per region, at LOG_LEVEL."""
import dataclasses
import logging
import os
import time

import numpy as np

import gradeshapes as _gsh

from . import cache, owner, s1_paths, s2_extent, s3_order, s4_terrain, s5_datum, s6_heights

log = logging.getLogger("earthd.grading")

LOG_LEVEL = logging.WARNING     # (the level the eval builds' view.log keeps: every eval shows each step's time)
STEPS = ("paths", "extent", "order", "terrain", "datum", "heights")


def _put(artifact, parents, inputs):
    """The artifact under its key (its meta filled in), written to the cache when it is on: (artifact, key)."""
    key = cache.key_of(artifact.meta.step, artifact.meta.format, artifact.meta.laws, parents, inputs)
    out = dataclasses.replace(artifact, meta=dataclasses.replace(artifact.meta, key=key, parents=tuple(parents)))
    if cache.ROOT:
        cache.put(out)
    return out, key


def _members_bytes(members):
    """The Paths' members' bytes: what the interchange's steps 1 and 2 were made from."""
    return [(repr(m.key) + repr((m.kind, m.cls, m.kmh, m.level, m.half, m.held,
                                 None if m.nodes is None else sorted(str(n) for n in m.nodes)))).encode() +
            np.asarray(m.line, np.float64).tobytes() for m in members]


def _samples_bytes(system_id, net, smp):
    """The samples' xy bytes (and the members under them): what an interchange's steps (4-6) were made from."""
    out = [system_id.encode()]
    for x in smp:
        out.append(np.round(np.asarray(x["xy"], np.float64), 3).tobytes())
        for i in np.unique(x["mem"]).tolist():
            out.append(repr(net.m[i].key).encode() + repr((net.m[i].kind, getattr(net.m[i], "level", None))).encode())
    return out


def _line(clock, step, key, pieces, crossings, seconds, why, where):
    """One step's line, its seconds added to the region's totals (clock): 'ix step: <step> key <key> pieces N crossings
    N <seconds> s ok' or '... failed: <why>', then where it ran (the region and the interchange)."""
    clock[step] = clock.get(step, 0.0) + seconds
    log.log(logging.WARNING if why is not None else LOG_LEVEL, "ix step: %s key %s pieces %d crossings %d %.2f s %s @ %s",
            step, key[:12], pieces, crossings, seconds, "ok" if why is None else "failed: " + why, where)


def _heights_why(zs, diag):
    """Why a system has no heights (None when it has): the kernel's own words, as the infeasible log line gives them."""
    if zs is not None:
        return None
    if diag.get("qp_error"):
        return str(diag["qp_error"])
    kind = "infeasible" if not diag.get("feasible", True) else "not converged"
    if diag.get("timed_out"):
        kind = "timed out"
    rest = ", ".join("%s %s" % (k, diag[k]) for k in sorted(diag) if k not in ("converged", "feasible"))
    return kind + (" (%s)" % rest if rest else "")


def system(planner, net, ix, fixed_z, hk, clock):
    """One interchange's steps, in order, each timed on its own: paths, extent, order, terrain (the pieces' samples and
    the ground there), datum, heights (the solve). ix: gradeshapes.Interchange, the whole one (owner.grow). Its key is
    the owner's (owner.key_of: its roads and its place, the same from every region), the scope of all its artifacts.
    Returns (pcs, smp, zs, diag, ground, heights key): zs None when there is no answer."""
    import xproc
    xproc.check_cancel()        # (a place a teleport left: not its next interchange either, a left city's 40 s solves
    #                              held the road tiles)
    sysm = ix.sysm
    system_id = owner.key_of(planner, net, sysm)
    scope = "system:%s" % system_id
    where = "%s system %s" % (hk, system_id)
    n_x = len(sysm["crossings"])

    t0 = time.perf_counter()
    pcs = _gsh.pieces(net, sysm)
    loc = s1_paths.local_of(net, sysm, pcs)
    members = _members_bytes([net.m[g] for g in sorted(loc, key=loc.get)])
    paths, paths_key = _put(s1_paths.run(net, loc, scope), (), members)
    _line(clock, "paths", paths_key, len(paths.members), len(paths.crossings), ix.paths_s + time.perf_counter() - t0,
          None, where)
    t0 = time.perf_counter()
    extent, extent_key = _put(s2_extent.run(net, sysm, loc, system_id, scope), (paths_key,), members)
    _line(clock, "extent", extent_key, len(sysm["seeds"]), n_x, ix.extent_s + time.perf_counter() - t0, None, where)

    t0 = time.perf_counter()
    order, order_key = _put(s3_order.run(planner, net, sysm, loc, system_id, scope), (extent_key,),
                            [system_id.encode()])
    bad = [f for f in order.findings if f.level == "fail"]
    _line(clock, "order", order_key, len(pcs), n_x, time.perf_counter() - t0,
          "; ".join("%s: %s" % (f.code, f.text) for f in bad) if bad else None, where)

    t0 = time.perf_counter()
    smp = _gsh.sample(net, pcs)
    inputs = _samples_bytes(system_id, net, smp)
    terrain, ground = s4_terrain.run(planner, smp, system_id, scope)
    terrain, terrain_key = _put(terrain, (order_key,), inputs)
    _line(clock, "terrain", terrain_key, len(pcs), n_x, time.perf_counter() - t0, None, where)

    t0 = time.perf_counter()
    datum, datum_key = _put(s5_datum.run(planner, net, sysm, loc, system_id, scope), (terrain_key,), inputs)
    _line(clock, "datum", datum_key, len(pcs), n_x, time.perf_counter() - t0, None, where)

    t0 = time.perf_counter()
    zs, diag = s6_heights.solve(net, sysm, pcs, smp, fixed_z, ground)
    _heights, heights_key = _put(s6_heights.run(planner, net, sysm, loc, pcs, smp, zs, diag, system_id, scope),
                                 (datum_key,), inputs)
    _line(clock, "heights", heights_key, len(pcs), n_x, time.perf_counter() - t0, _heights_why(zs, diag), where)
    return pcs, smp, zs, diag, ground, heights_key


def interchange(planner, net, ix, others, hk, clock):
    """One whole interchange's steps and what they give the roads (the owner's, owner.made): system(), then its
    embankments and profiles. ({road feature id: [(xy (m, 2), z (m,), mode)]}, None), or ({}, why) where there is no
    answer. others: the net's other interchanges' systems (the pieces its ramps stop at)."""
    sysm = ix.sysm
    fixed_z = s2_extent.make_fixed(planner, net)
    pcs, smp, zs, diag, ground, heights_key = system(planner, net, ix, fixed_z, hk, clock)
    if zs is None:              # (no answer: the interchange as it is, and why in the log; no stand-in)
        why = _heights_why(zs, diag)
        log.error("grading: SHAPES infeasible system in region %s: %d pieces, %d crossings, %d ends; %s; members %s",
                  hk, len(pcs), len(sysm["crossings"]), len(sysm["ends"]),
                  diag.get("qp_error") or ", ".join("%s %s" % (k, diag[k]) for k in sorted(diag)
                                                    if k not in ("converged", "feasible")),
                  ", ".join(sorted({str(net.m[i].key[0])[:8] + ":" + net.m[i].kind for i in sysm["seeds"]}))[:600])
        return {}, why
    if os.environ.get("BNE_SHAPES_DEBUG"):
        planner._shape_debug(net, sysm, pcs, smp, zs, ground, fixed_z, diag)
    prof, region = s6_heights.region_of(planner, net, [sysm], [(pcs, smp, zs)], "interchange:%s" % heights_key,
                                        [_gsh.pieces(net, o) for o in others])
    _put(region, (heights_key,), [heights_key.encode()])
    return prof, None


def region(planner, home, hk):
    """What _shape_region_calc(home, hk) returned: {road feature id: [(xy (m, 2), z (m,), mode)]}. The region finds the
    interchanges its box holds (s2_extent.find: pieces of them, a box cuts them anywhere); each is asked for as the
    interchange it is (owner.interchange): solved once for every region and process that reaches it, its steps written
    as artifacts (never read back), and its heights taken whole."""
    import grading      # (the planner's module: its laws; imported here, grading imports us first)
    t0 = time.perf_counter()
    clock = {}
    lon0, lat0, lon1, lat1 = (float(v) for v in home.bounds())
    ce, cn, _ = planner.frame.geodetic_to_enu(np.array([lat0, lat0, lat1, lat1]), np.array([lon0, lon1, lon0, lon1]),
                                              0.0)
    h_lo = np.array([np.min(ce), np.min(cn)]) - grading.SHAPES_PAD_M
    h_hi = np.array([np.max(ce), np.max(cn)]) + grading.SHAPES_PAD_M

    # (the region's own work, before any interchange's steps: its cells' members read, the interchanges found as its box
    # holds them. Each interchange's share of the finding is in its paths and extent lines; what is left is the region's:
    # the net, and the members that belong to no interchange)
    net, mine, loaded, members = s2_extent.find(planner, h_lo, h_hi, clock)
    clock["scan"] = clock.pop("find", 0.0) - sum(ix.paths_s + ix.extent_s for ix in mine)
    t1 = time.perf_counter()

    found = {}
    for ix in mine:
        got = owner.interchange(planner, net, ix, hk, clock)
        found[got["key"]] = got
    t_owner = time.perf_counter() - t1
    prof = {}
    for decks in (True, False):     # (a road's decks first, then the embankments and cuts: the order the consumers read)
        for ik in sorted(found):
            for fid, items in found[ik]["roads"].items():
                prof.setdefault(fid, []).extend(it for it in items if (it[2] == "deck") == decks)
    failed = sum(1 for g in found.values() if g["failed"])
    log.warning("grading: SHAPES pid %d region %s: %d cells, %d members, %d systems (%d interchanges, %d infeasible, "
                "left as they are), %d roads graded, find %.2f s, solve %.2f s", os.getpid(), hk, len(loaded),
                len(members), len(mine), len(found), failed, len(prof), t1 - t0, t_owner)
    log.log(LOG_LEVEL, "ix region: %s systems %d %.2f s, load %.2f s, scan %.2f s, steps %s, interchanges %.2f s", hk,
            len(mine), time.perf_counter() - t0, clock.get("load", 0.0), clock.get("scan", 0.0),
            ", ".join("%s %.2f s" % (k, clock.get(k, 0.0)) for k in STEPS), t_owner)
    return prof
