"""The interchange pipeline's orchestrator (SPEC.md): region(planner, home, hk) is what _Planner._shape_region_calc
returned, built by calling the s1..s6 steps in order. This PR writes the artifacts only -- it never reads them back to
skip generation -- so with BNE_IX_CACHE unset nothing changes at all.

Each step logs one INFO line (step, key, size, seconds, ok or why it failed) and region() one more per region. The steps
of one system run in system(), one function, so whatever must hold across a system's steps (a lock round them) has a
single place to wrap."""
import dataclasses
import logging
import os
import time

import numpy as np

import gradeshapes as _gsh

from . import cache, s1_paths, s2_extent, s3_order, s4_terrain, s5_datum, s6_heights

log = logging.getLogger("earthd.grading")

STEPS = ("paths", "extent", "order", "terrain", "datum", "heights")


def _put(artifact, parents, inputs):
    """The artifact under its key (its meta filled in), written to the cache when it is on: (artifact, key)."""
    key = cache.key_of(artifact.meta.step, artifact.meta.format, artifact.meta.laws, parents, inputs)
    out = dataclasses.replace(artifact, meta=dataclasses.replace(artifact.meta, key=key, parents=tuple(parents)))
    if cache.ROOT:
        cache.put(out)
    return out, key


def _members_bytes(members):
    """The members' bytes: what the region steps (1, 2) were made from."""
    return [(repr(m.key) + repr((m.kind, m.cls, m.kmh, m.level, m.half, m.held))).encode() +
            np.asarray(m.line, np.float64).tobytes() for m in members]


def _samples_bytes(net, sysm, smp):
    """The samples' xy bytes, as _shape_sys_key digests them: what a system's steps (3-6) were made from."""
    out = [_gsh.system_key(net, sysm).encode()]
    for x in smp:
        out.append(np.round(np.asarray(x["xy"], np.float64), 3).tobytes())
        for i in np.unique(x["mem"]).tolist():
            out.append(repr(net.m[i].key).encode() + repr((net.m[i].kind, getattr(net.m[i], "level", None))).encode())
    return out


def _line(clock, step, key, pieces, crossings, seconds, why, where):
    """One step's INFO line, its seconds added to the region's totals (clock): 'ix step: <step> key <key> pieces N
    crossings N <seconds> s ok' or '... failed: <why>', then where it ran (the region, and the system for steps 3-6)."""
    clock[step] = clock.get(step, 0.0) + seconds
    log.info("ix step: %s key %s pieces %d crossings %d %.2f s %s @ %s", step, key[:12], pieces, crossings, seconds,
             "ok" if why is None else "failed: " + why, where)


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


def system(planner, net, sysm, fixed_z, extent_key, hk, clock):
    """One system's steps, in the order they depend on each other: its pieces and samples and the solve (the heights'
    inputs; the terrain step keeps the ground the solve read), then order, terrain, datum and heights. Returns
    (pcs, smp, zs, diag, ground, heights key): zs None when there is no answer."""
    import xproc
    xproc.check_cancel()        # (a place a teleport left: not its next system either, a left city's 40 s solves held
    #                              the road tiles)
    t0 = time.perf_counter()
    pcs, smp, ck = s6_heights.problem(planner, net, sysm)
    system_id = _gsh.system_key(net, sysm)
    scope = "system:%s" % system_id
    where = "%s system %s" % (hk, system_id)
    inputs = _samples_bytes(net, sysm, smp)
    ground, zs, diag = s6_heights.solve(planner, net, sysm, pcs, smp, ck, fixed_z)
    t_solve = time.perf_counter() - t0
    n_x = len(sysm["crossings"])

    t0 = time.perf_counter()
    order, order_key = _put(s3_order.run(planner, net, sysm, system_id, scope), (extent_key,), inputs)
    bad = [f for f in order.findings if f.level == "fail"]
    _line(clock, "order", order_key, len(pcs), n_x, time.perf_counter() - t0,
          "; ".join("%s: %s" % (f.code, f.text) for f in bad) if bad else None, where)
    t0 = time.perf_counter()
    terrain, _ground = s4_terrain.run(planner, smp, ground, system_id, scope)
    terrain, terrain_key = _put(terrain, (order_key,), inputs)
    _line(clock, "terrain", terrain_key, len(pcs), n_x, time.perf_counter() - t0, None, where)
    t0 = time.perf_counter()
    datum, datum_key = _put(s5_datum.run(planner, net, sysm, system_id, scope), (terrain_key,), inputs)
    _line(clock, "datum", datum_key, len(pcs), n_x, time.perf_counter() - t0, None, where)
    t0 = time.perf_counter()
    _heights, heights_key = _put(s6_heights.run(planner, net, sysm, pcs, smp, zs, diag, system_id, scope),
                                 (datum_key,), inputs)
    _line(clock, "heights", heights_key, len(pcs), n_x, t_solve + time.perf_counter() - t0, _heights_why(zs, diag),
          where)
    return pcs, smp, zs, diag, ground, heights_key


def region(planner, home, hk):
    """What _shape_region_calc(home, hk) returned: {road feature id: [(xy (m, 2), z (m,), mode)]}, built by the s1..s6
    steps (the artifacts written, never read back)."""
    import grading      # (the planner's module: its laws and its solve lock; imported here, grading imports us first)
    t0 = time.perf_counter()
    clock = {}
    lon0, lat0, lon1, lat1 = (float(v) for v in home.bounds())
    ce, cn, _ = planner.frame.geodetic_to_enu(np.array([lat0, lat0, lat1, lat1]), np.array([lon0, lon1, lon0, lon1]),
                                              0.0)
    h_lo = np.array([np.min(ce), np.min(cn)]) - grading.SHAPES_PAD_M
    h_hi = np.array([np.max(ce), np.max(cn)]) + grading.SHAPES_PAD_M
    box_lo, box_hi = h_lo - 200.0, h_hi + 200.0
    cap_lo, cap_hi = h_lo - grading.SHAPES_CAP_M, h_hi + grading.SHAPES_CAP_M

    # (steps 1 and 2 run once for the region: its members are read and its systems found together, the loop's time is
    # split in clock as "paths" (the cells' members and one_road) and "extent" (find_systems))
    extent, net, mine, loaded, members = s2_extent.run(planner, hk, h_lo, h_hi, box_lo, box_hi, cap_lo, cap_hi, clock)
    region_scope = "region:%s" % hk
    inputs = _members_bytes(members)
    t_paths = time.perf_counter()
    paths, paths_key = _put(s1_paths.run(planner, net, region_scope), (), inputs)
    _line(clock, "paths", paths_key, len(paths.members), len(paths.crossings),
          clock.pop("paths", 0.0) + time.perf_counter() - t_paths, None, hk)
    t_extent = time.perf_counter()
    _extent, extent_key = _put(extent, (paths_key,), inputs)
    _line(clock, "extent", extent_key, len(extent.systems), sum(len(sysm["crossings"]) for sysm in mine),
          clock.pop("extent", 0.0) + time.perf_counter() - t_extent, None, hk)
    t1 = time.perf_counter()

    fixed_z = s2_extent.make_fixed(planner, net)

    shapes, failed, solved, keys = [], 0, [], []
    for sysm in mine:
        pcs, smp, zs, diag, ground, heights_key = system(planner, net, sysm, fixed_z, extent_key, hk, clock)
        if zs is None:          # (no answer: the system as it is, and why in the log; no stand-in)
            failed += 1
            log.error("grading: SHAPES infeasible system in region %s: %d pieces, %d crossings, %d ends; %s; "
                      "members %s", home, len(pcs), len(sysm["crossings"]), len(sysm["ends"]),
                      diag.get("qp_error") or ", ".join("%s %s" % (k, diag[k]) for k in sorted(diag)
                                                        if k not in ("converged", "feasible")),
                      ", ".join(sorted({str(net.m[i].key[0])[:8] + ":" + net.m[i].kind for i in sysm["seeds"]}))[:600])
            continue
        solved.append(sysm)
        shapes.append((pcs, smp, zs))
        keys.append(heights_key)
        if os.environ.get("BNE_SHAPES_DEBUG"):
            planner._shape_debug(net, sysm, pcs, smp, zs, ground, fixed_z, diag)

    prof, region = s6_heights.region_of(planner, net, solved, shapes, hk)
    _put(region, (extent_key,) + tuple(keys), _members_bytes(members) + [k.encode() for k in keys])
    log.warning("grading: SHAPES pid %d region %s: %d cells, %d members, %d systems (%d infeasible, left as they are), "
                "%d roads graded, find %.2f s, solve %.2f s", os.getpid(), hk, len(loaded), len(members), len(mine),
                failed, len(prof), t1 - t0, time.perf_counter() - t1)
    log.info("ix region: %s systems %d %.2f s, steps %s", hk, len(mine), time.perf_counter() - t0,
             ", ".join("%s %.2f s" % (k, clock.get(k, 0.0)) for k in STEPS))
    return prof
