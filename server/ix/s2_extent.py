"""Step 2 (SPEC.md): the region's systems (gradeshapes.find_systems) and the region growth loop, as the Extent
artifact. make_fixed is the planner's fixed_z maker (it needs the planner: the rails' tops, the ground lines)."""
import time

import numpy as np

import dnp
import gradeshapes as _gsh

from . import artifacts, cache, s1_paths

FORMAT = 1
LAWS = (("gradeshapes", ["TUNNEL_MAX_M", "CONNECT_M"]),)
LAWS_DIGEST = cache.laws_digest(*LAWS)
STEP = 2


def make_fixed(planner, net):
    """fixed_z(i, s): member i's height where the shape does not move it, at arc length s (None: not known), or at an
    array of them (an array, NaN: not known)."""
    rail_tops = {}

    def fixed_z(i, s_):
        if np.ndim(s_):                 # (many: the ground read once for all, _ground_lines)
            sv_ = np.asarray(s_, np.float64)
            if net.m[i].kind in ("rail", "rail_deck"):
                return np.array([np.nan if z is None else z
                                 for z in planner._rail_tops(net, i, sv_.tolist(), rail_tops)])
            sv = np.clip(sv_[:, None] + np.linspace(-24.0, 24.0, 13)[None], 0.0, net.L[i])
            xy = np.stack([dnp.interp(sv, net.s[i], net.lines[i][:, 0]),
                           dnp.interp(sv, net.s[i], net.lines[i][:, 1])], axis=2)
            return planner._ground_lines(xy)[:, 6]
        m = net.m[i]
        if m.kind in ("rail", "rail_deck"):     # (a track, on its deck or on the ground: its rails' top over the
            # bed the railways lay and draw it on, never a height of the shapes' own: its deck's mapped ends' ground,
            # down the slopes of the street's cutting under it, stood Compiegne's 7 m rail bridge 2.8 m under its
            # track, the street was dug that much too deep and the track's fill beside it stood 10.5 m tall)
            return planner._rail_tops(net, i, [float(s_)], rail_tops)[0]   # (None: the railways lay none of it, no
            #                                                               track there to clear)
        q = _gsh.at_s(net.lines[i], net.s[i], s_)
        sv = np.clip(np.linspace(s_ - 24.0, s_ + 24.0, 13), 0.0, net.L[i])
        xy = np.stack([dnp.interp(sv, net.s[i], net.lines[i][:, 0]),
                       dnp.interp(sv, net.s[i], net.lines[i][:, 1])], axis=1)
        return float(planner._ground_line(xy)[6]) if len(xy) else None

    return fixed_z


def system_of(net, sysm):
    """One system as data: its key, seeds, joins, boundary (its ends on the roads on the ground) and box."""
    pts = np.concatenate([net.lines[i] for i in sysm["seeds"]])
    return artifacts.System(_gsh.system_key(net, sysm), tuple(int(i) for i in sysm["seeds"]),
                            tuple(artifacts.Join(int(a), float(sa), int(b), float(sb)) for a, sa, b, sb in sysm["joins"]),
                            tuple(artifacts.Boundary(int(a), float(sa), int(b), float(sb))
                                  for a, sa, b, sb in sysm["ends"]),
                            tuple(float(v) for v in np.r_[pts.min(0), pts.max(0)]))


def run(planner, hk, h_lo, h_hi, box_lo, box_hi, cap_lo, cap_hi, clock=None):
    """find_systems and the region growth loop, as _shape_region_calc ran them: (Extent, net, mine, loaded, members).
    mine is the systems this region solves (those reaching its home box), in solve order. clock (a dict): the seconds
    of the members' reading (step 1) and of find_systems (step 2) are added to its "paths" and "extent"."""
    import xproc
    clock = {} if clock is None else clock
    byk, loaded = {}, set()
    while True:
        xproc.check_cancel()        # (a place a teleport left: its systems not found and solved for it)
        t0 = time.perf_counter()
        s1_paths.load(planner, box_lo, box_hi, byk, loaded)
        members = s1_paths.members_of(byk, box_lo, box_hi)
        t1 = time.perf_counter()
        net, systems = _gsh.find_systems(members, lambda net_: (planner._ground_line, make_fixed(planner, net_)))
        clock["paths"] = clock.get("paths", 0.0) + t1 - t0
        clock["extent"] = clock.get("extent", 0.0) + time.perf_counter() - t1
        mine = []
        n_lo, n_hi = box_lo.copy(), box_hi.copy()
        for sysm in systems:
            pts = np.concatenate([net.lines[i] for i in sysm["seeds"]])
            b_lo, b_hi = pts.min(0), pts.max(0)
            if (b_hi < h_lo).any() or (b_lo > h_hi).any():
                continue
            mine.append(sysm)
            if (b_lo < box_lo + 100.0).any() or (b_hi > box_hi - 100.0).any():     # (open: follow it)
                n_lo = np.minimum(n_lo, b_lo - 300.0)
                n_hi = np.maximum(n_hi, b_hi + 300.0)
        n_lo, n_hi = np.maximum(n_lo, cap_lo), np.minimum(n_hi, cap_hi)
        if (n_lo >= box_lo).all() and (n_hi <= box_hi).all():
            break
        box_lo, box_hi = n_lo, n_hi
    extent = artifacts.Extent(artifacts.Meta(STEP, FORMAT, "", (), LAWS_DIGEST, "region:%s" % hk), str(hk),
                              [system_of(net, sysm) for sysm in mine])
    return extent, net, mine, loaded, members
