"""BNE_ROADSYS_TUNNELS=on (with BNE_ROADSYS=surface, the "Revamped roads" toggle; tunnel plan T1, approved
2026-11-25): the tunnels from the road system's record (native/roadsys tunnel.rs), not from roads.TunnelProfiles' own
solve. Thin glue: every decision is the record's, made once per carriageway in the profile square that owns its
floor; TunnelProfiles hands it on to every consumer (the road tiles' tubes, the terrain blocks' holes, the grading's
mouths, the lane check), so none decides a tunnel of its own.

  solve(store, comp, pool)  per part of a dry tunnel network: the floor (the one profile through ground and tunnel,
                            blended from the squares as the decks' are), the sections (the record's kinds per vertex:
                            0 a daylight gap, 1 a lid, 2 a full tube; never "open" for want of cover), the portals and
                            each portal's hole reach (the record's mouths). None: the network stays on the legacy solve
                            (a square not at hand, a vertex on no stroke of its feature, under water: T3).
  holes(...)                the record's hole rule on a terrain block's lattice (earthfast tunnel_record_holes).

Off (the default) and Revamped without the switch: nothing here runs."""
import logging
import threading

import numpy as np

import dnp
import roadsys

log = logging.getLogger("earthd.tunnelrec")

KERNEL = 631                # earthfast roadsys_square(tunnels=), roadsys_tunnel_kinds / _mouths, tunnel_record_holes (631: mouths dig the approach)
JOIN_M = 2.0                # m: two parts' ends this close are one joint of the network (no portal there)
MOUTH_NEAR_M = 6.0          # m a record portal may lie from a section's end vertex to be that end's
HOLE_MARGIN_M = 0.5         # m round the tube's footprint the holes reach (tunnel plan 4.4)
HOLE_OVER_M = 1.0           # m: a square is kept where the vault top + this is under all its corners' ground
END_PAST_M = 0.5            # m past a portal end the approach's graded ground is read (the hand-over)
STATS = {"networks": 0, "parts": 0, "fallback": 0, "no_square": 0, "unmatched": 0, "wet": 0}
_lock = threading.Lock()


def _count(k, n=1):
    with _lock:
        STATS[k] = STATS.get(k, 0) + n


def portal_ground(plans, floor):
    """The height a portal end is brought onto (the hand-over, 4.5): the plans' first-stage approach ground there, never
    over the record's own floor. Where the plans lie over the profile's portal the profile owns the approach: its
    terrain targets dig it down to the floor (square.rs lows at the record's mouths, pins_near); a lift of the floor onto
    the plans took the cover the streets over the tube hold (Moscow's Garden Ring 2.4 m under Taganskaya Square,
    2026-11-25). NaN: no ground there (the end left as the record has it)."""
    if not np.isfinite(plans):
        return float("nan")
    return float(min(plans, floor)) if np.isfinite(floor) else float(plans)


def _runs(kinds):
    """[(i0, i1, kind)] of runs of one kind, neighbours sharing their end sample (as TunnelProfiles' sections)."""
    secs, i = [], 0
    while i < len(kinds):
        j = i
        while j + 1 < len(kinds) and kinds[j + 1] == kinds[i]:
            j += 1
        secs.append((i, j, int(kinds[i])))
        i = j + 1
    return [(a if n == 0 else a - 1, b, kd) for n, (a, b, kd) in enumerate(secs)]


def solve(store, comp, pool, ground=None):
    """{part key: (floor (k,) store frame, sections, subs, holes)} for the network comp (TunnelProfiles' pool), or None
    for the legacy solve. subs[n] = (kind, tube height, portal ups, mouths, caps) as TunnelProfiles stages them;
    holes[n] = (reach in from the start portal, from the end portal) (0: no portal there)."""
    import fast
    import roads
    import roadmesh
    fast.require(KERNEL, "roadsys_tunnel_kinds")
    keys = [k for k in sorted(comp, key=str) if len(pool[k][1]) >= 2]
    if not keys:
        return None
    _count("networks")
    sf = store.frame
    lls, regs = [], []
    for k in keys:
        xy = np.asarray(pool[k][1], np.float64)[:, :2]
        la, lo, _ = sf.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
        lls.append((np.asarray(la, np.float64), np.asarray(lo, np.float64)))
    fid, frame = roadsys.region(float(lls[0][0][0]), float(lls[0][1][0]))
    lines = []
    for la, lo in lls:
        e, n, _u = frame.geodetic_to_enu(la, lo, 0.0)
        lines.append(np.ascontiguousarray(np.column_stack((e, n)), np.float64))
    allp = np.concatenate(lines)
    box = [float(allp[:, 0].min()), float(allp[:, 1].min()), float(allp[:, 0].max()), float(allp[:, 1].max())]
    sqs = roadsys._squares_for(fid, frame, box, store.cell_fn, None)
    if any(q is None for q in sqs):
        _count("no_square")
        return None
    mouths = fast._ef.roadsys_tunnel_mouths(sqs)
    # the network's joints: part ends another part's end meets (no portal, no hole there)
    ends = [(lines[i][0], lines[i][-1]) for i in range(len(keys))]

    def joined(i, at_end):
        p = ends[i][1 if at_end else 0]
        for j, (a, b) in enumerate(ends):
            if j == i:
                continue
            for q in (a, b):
                if float(dnp.hypot(*(q - p))) < JOIN_M:
                    return True
        return False
    out = {}
    pre = []
    for i, k in enumerate(keys):
        d = lines[i]
        feat = roadsys.feature(k[0] if isinstance(k, tuple) else k)
        tg = roadsys._tangents(d)
        z = np.asarray(fast._ef.roadsys_blend(sqs, d, tg, [feat] * len(d), False), np.float64)
        kinds, cmin = fast._ef.roadsys_tunnel_kinds(sqs, d, tg, [feat] * len(d))
        kinds = np.asarray(kinds, np.int64)
        cmin = np.asarray(cmin, np.float64)
        if not np.isfinite(z).all() or (kinds == 254).any():
            _count("unmatched")
            return None
        # a vertex on no covered run (a part's end a hair past its run's): its neighbour's kind
        for j in range(len(kinds)):
            if kinds[j] == 255:
                kinds[j] = kinds[j - 1] if j > 0 and kinds[j - 1] != 255 else 2
        la, lo = lls[i]
        pre.append((feat, np.asarray(sf.geodetic_to_enu(la, lo, z)[2], np.float64), kinds, cmin))
    # the portal hand-over (4.5): each portal end brought onto the ground the roads' plans grade just past it (the
    # approach the road tiles draw), lifted or dropped, the change spread along the network as the decks' are onto
    # their abutments (native/roadsys endmatch.rs): the floor meets the approach, no step at the mouth
    plist, ends_g = [], []
    for i, k in enumerate(keys):
        xy = np.asarray(pool[k][1], np.float64)[:, :2]
        plist.append((np.ascontiguousarray(xy), np.ascontiguousarray(pre[i][1], np.float64)))
        pair = []
        for at_end, (a, b) in ((False, (xy[0], xy[1])), (True, (xy[-1], xy[-2]))):
            if joined(i, at_end):
                pair.append(float("nan"))
                continue
            dv = a - b
            nn = float(dnp.hypot(dv[0], dv[1]))
            q = a + (dv / nn if nn > 1e-9 else 0.0) * END_PAST_M
            gq = float(np.asarray(store._ends_ground(q[None, :]), np.float64).reshape(-1)[0])
            pair.append(portal_ground(gq, float(pre[i][1][-1 if at_end else 0])))
        ends_g.append(tuple(pair))
    lifts = fast._ef.roadsys_end_lifts(plist, ends_g, float("inf"), float("inf"))
    for i in range(len(keys)):
        pre[i] = (pre[i][0], pre[i][1] + np.round(np.asarray(lifts[i], np.float64), 4), pre[i][2], pre[i][3])
    for i, k in enumerate(keys):
        d = lines[i]
        feat, floor, kinds, cmin = pre[i]
        secs = _runs(kinds)
        xy = np.asarray(pool[k][1], np.float64)[:, :2]
        s = np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(xy, axis=0).T))]
        g = np.asarray(store._raw_ground(xy) if store.raw is not None else store._ground(xy), np.float64)
        cover = g - floor
        spec = pool[k][0]
        low_h = roads.MAJOR_LOW_H if spec is not None and spec.cls in roads.CONCRETE_WALL_CLASSES else roads.TUNNEL_LOW_H
        subs, holes = {}, {}
        for n, (a, b, kd) in enumerate(secs):
            if kd == 0:
                subs[n] = (0, None, None, None, None)
                continue
            th = roadmesh.TUNNEL_H
            if kd == 1:
                c = float(np.nanmin(cmin[a:b + 1])) if np.isfinite(cmin[a:b + 1]).any() else 0.0
                th = float(np.clip(c - roads.TUNNEL_SOIL_M, low_h, roadmesh.TUNNEL_H))
            # a portal: next to a daylight gap, or the part's end that no other part of the network goes on from
            m0 = (n > 0 and secs[n - 1][2] == 0) or (n == 0 and not joined(i, False))
            m1 = (n + 1 < len(secs) and secs[n + 1][2] == 0) or (n == len(secs) - 1 and not joined(i, True))
            # each portal's hole reach, the record's (its nearest portal of this feature)
            hr = []
            for on, at in ((m0, a), (m1, b)):
                h = 0.0
                if on:
                    best = None
                    for mx, my, _dx, _dy, reach, f in mouths:
                        dd = float(dnp.hypot(mx - d[at][0], my - d[at][1]))
                        if f == feat and dd < MOUTH_NEAR_M and (best is None or dd < best[0]):
                            best = (dd, reach)
                    # (no record portal there: a part end the record joins to a ground road off this square's
                    # strokes; the tube's whole cover decides, as the record's rule would)
                    h = float(best[1]) if best is not None else float(min(s[b] - s[a], 60.0))
                hr.append(h)
            ups = []
            for near in (s[a:b + 1] <= s[a] + roads.PORTAL_LOOK_M, s[a:b + 1] >= s[b] - roads.PORTAL_LOOK_M):
                cv = float(np.max(cover[a:b + 1][near])) if near.any() else th
                ups.append(float(np.clip(cv + roads.PORTAL_GROUND_M - th, roads.PORTAL_UP_MIN, roadmesh.PORTAL_FRAME)))
            caps = None
            if kd == 1:
                caps, ups = store._road_caps(xy[a:b + 1], floor[a:b + 1], th, ups, (m0, m1), spec.width, ground)
            subs[n] = (kd, th, ups, (m0, m1), caps)
            holes[n] = (hr[0], hr[1])
        out[k] = (floor, secs, subs, holes)
        _count("parts")
    return out


def holes(mask, u, x0, y0, square, parts, heights, holes_of):
    """mask with the record's holes of the parts ([(fid#n, spec, xy, z, lon)], those with holes_of entries) set: the
    plan's rule (4.4), earthfast tunnel_record_holes. Returns the parts left for the legacy rule (none from the record)."""
    import fast
    import roadmesh
    fast.require(KERNEL, "tunnel_record_holes")
    rest = []
    u = np.ascontiguousarray(u, np.float64)
    for p in parts:
        fid, spec, xy, z, _lon = p
        h = holes_of.get(fid)
        if h is None:
            rest.append(p)
            continue
        xy = np.asarray(xy, np.float64)
        z = np.asarray(z, np.float64)
        if len(xy) < 2 or not np.isfinite(z).all() or not (h[0] > 0 or h[1] > 0):
            continue
        hw = spec.width / 2 + roadmesh.TUNNEL_SHOULDER
        th = (heights or {}).get(fid, roadmesh.TUNNEL_H)
        fast._ef.tunnel_record_holes(mask, u, float(x0), float(y0), float(square), np.ascontiguousarray(xy[:, :2]),
                                     np.ascontiguousarray(z), float(hw), float(th), float(h[0]), float(h[1]),
                                     HOLE_MARGIN_M, HOLE_OVER_M)
    return rest
