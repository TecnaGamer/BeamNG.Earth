"""The bus stations' bays for the buses that stand at them (plan 034, the user 2026-11-09: "spawn buses only when the traffic
is on, 2-3 of them"): a few parked-car-style rows [x, y, z, heading, 2] per station (the mod's busparked.py keeps them apart
from the parked cars, and puts at most three stock buses on the ones nearest the player while the traffic is on).

    rows(frame, md, box, elev) -> [[x, y, z, heading degrees, 2]] of the bays whose centre is in the tile box
"""
import hashlib
import math

import numpy as np

import busbays
import busstations as bs

PER_STATION = 1             # bays offered per station (the mod keeps ONE bus at most: the user 2026-11-09, three in a row closed a lane)
BUS_LEN_M, BUS_W_M = 12.4, 2.8          # the bus's footprint: it must stand clear of every drive lane and mouth
BAY_KIND = 2                # the row's fifth field: a bus bay (1 is a lot stall, none a curbside spot)
CLEAR_M = 1.0               # off every building, roof column and mouth by this much


def _heading(st, bay):
    """The bus's nose, in degrees: along the lane it stands by when that is one-way (the bay's own axis otherwise, the
    way its label reads)."""
    t = np.asarray(bay.t, float)
    best = None
    for ln in st.lanes:
        d = float(bs.path_dist(ln["xy"], bay.c[None])[0])
        if d < 6.0 and (best is None or d < best[0]):
            path = np.asarray(ln["xy"], float)
            seg = np.hypot(*np.diff(path, axis=0).T)
            k = int(np.argmin(np.hypot(*(0.5 * (path[:-1] + path[1:]) - bay.c).T))) if len(seg) else 0
            dd = path[k + 1] - path[k]
            if ln["oneway"] and float(np.hypot(*dd)) > 1e-6:
                best = (d, dd / float(np.hypot(*dd)))
    if best is not None:
        t = best[1]
    return float(math.degrees(math.atan2(t[1], t[0])) % 360.0)


def _on_pavement(st, pts, yard):
    """True when any point of the bus's footprint is on the station's raised walking ground or off the yard's asphalt."""
    import busstationmesh
    if bool(busstationmesh._walk_ok(st, pts).any()):
        return True
    return any(not any(bs.contains(r, q) for r in yard) for q in pts) if yard else False


def station_bays(st):
    """[(bay, heading)]: the station's PER_STATION bays a bus may stand at: labelled or synthesised, whole bay on the drive
    area, the bus clear of every lane and mouth (none when no bay is), away from buildings, picked by a hash of the bay (the same ones in every tile and process). Memoised."""
    got = st.__dict__.get("_bus_bays")
    if got is not None:
        return got
    import busyard
    yard = busyard.yard_polys(st)
    cand = []
    for b in busbays.layout_bays(st):
        if not (b.label or b.synth):
            continue
        c = np.asarray(b.c, float)
        if any(bs.contains(it["xy"], c) for it in st.buildings):
            continue
        corners = [np.asarray(q, float) for q in b.corners()]
        if any(bs.contains(it["xy"], q) for it in st.buildings for q in corners):
            continue
        pts = np.array([c + np.asarray(b.t, float) * sx * BUS_LEN_M / 2.0 + np.asarray(b.n, float) * sy * BUS_W_M / 2.0
                        for sx in (-1.0, -0.5, 0.0, 0.5, 1.0) for sy in (-1.0, 0.0, 1.0)])
        if (st.lanes or st.mouths) and bool(bs.lane_blocked(st, pts, pad=0.3, mouths=True).any()):
            continue                                    # (a bus here would close a lane)
        if _on_pavement(st, pts, yard):
            continue                                    # (the user 2026-11-09: "on the pavement": a bus stands on the asphalt)
        h = int(hashlib.sha1(("%s|%.1f|%.1f" % (st.fid, c[0], c[1])).encode()).hexdigest()[:8], 16)
        cand.append((h, b))
    cand.sort(key=lambda x: x[0])
    out = []
    for _h, b in cand:
        if len(out) >= PER_STATION:
            break
        if all(float(np.hypot(*(np.asarray(b.c) - np.asarray(o[0].c)))) >= 14.0 for o in out):
            out.append((b, _heading(st, b)))
    st._bus_bays = out
    return out


def rows(frame, md, box, elev):
    """[[x, y, z, heading, 2]] of the offered bays whose centre is in the tile box (x0, y0, x1, y1) of frame."""
    if md is None or not bs.ON:
        return []
    import busstationmesh
    x0, y0, x1, y1 = (float(v) for v in box)
    pend = []
    # (the stations as laid out once per group, the bays moved into frame: laid out again for each road tile they were
    # ~3 s of each in Lower Manhattan, 2026-11-11; and the bays a hash of their centre picks are the same in every tile)
    for st in bs.stations_near(md, frame, box, shared=True):
        if not st.confident():
            continue
        bays = station_bays(st)
        if not bays:
            continue
        gf = getattr(st, "group_frame", None) or frame
        c = np.array([np.asarray(b.c, float)[:2] for b, _h in bays])
        hr = np.radians([h for _b, h in bays])
        cc = bs.to_frame(gf, frame, c)
        tip = bs.to_frame(gf, frame, c + np.column_stack((np.cos(hr), np.sin(hr))))
        for (x, y), (tx_, ty_) in zip(cc.tolist(), tip.tolist()):
            if x0 <= x < x1 and y0 <= y < y1:
                pend.append((x, y, float(math.degrees(math.atan2(ty_ - y, tx_ - x)) % 360.0)))
    if not pend:
        return []
    z = busstationmesh.ground(frame, elev, np.array([[p[0], p[1]] for p in pend]))
    return [[round(x, 2), round(y, 2), round(float(zz), 2), round(h, 1), BAY_KIND] for (x, y, h), zz in zip(pend, z)]
