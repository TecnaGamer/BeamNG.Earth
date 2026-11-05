"""Railway catenary: masts with cantilevers and the wires between them along electrified lines (the user, 2026-11-05:
"the line is electrified (25 kV) and there are no masts or wires").

OpenStreetMap says which lines are electrified (railtags: attrs osm_electrified, contact_line / yes) and, where it is
mapped (Germany, parts of Czechia), where the masts are (power=catenary_mast nodes, feats._rail_nodes). Elsewhere the
masts stand along the line at the spacing a catenary's span allows: 60 m on a straight, shorter on a curve (the
contact wire's pull-off from the track's axis must stay within its pantograph's reach), alternating nothing, from the
line's start so that every tile finds the same masts. A mast stands 3.45 m from the axis on the side with room (not
between two tracks: the line's outer side), off the roads; its cantilever (tools/blender/build_catenary.py,
trees.lua's catmast) holds the contact wire 5.75 m over the rail head and the messenger wire above it, and the droppers
hang between them. Every span is drawn by the tile that holds its mast.

Opt-in: BNE_CATENARY=1 (railxing's BNE_RAIL_XING is the crossings'). Not done: portals over three tracks or more (the
inner tracks of a yard stay bare), masts on bridges, other styles of mast (the shape is one)."""
import hashlib
import math
import os

import numpy as np

ENABLED = os.environ.get("BNE_CATENARY", "") not in ("", "0", "off", "false")

GROUP = "catmast"
MAST_H = 9.0                    # m (the model's top over its origin)
MAST_OFF_M = 3.45               # m from the track's axis to the mast's axis
SPAN_M = 60.0                   # m between masts on a straight
SPAN_MIN_M = 30.0
CURVE_K = 0.4                   # m: the pull-off allowed on a curve, a span's sagitta: span = sqrt(8 R k) ...
SPAN_MAX_M = 62.0               # ... at most this
ROAD_CLEAR_M = 2.0              # m a mast keeps off a carriageway's edge
BETWEEN_M = 3.0                 # m a mast keeps off another track's axis (not between two tracks)
SHIFT_M = (0.0, 3.0, -3.0, 6.0, -6.0, 9.0, -9.0)    # m along the track a mast moves off a road or a track
MAPPED_NEAR_M = 8.0             # m from a line a mapped mast node belongs to it
MAPPED_GAP_M = 100.0            # m: mapped masts this close along a line are its masts, else the rule's
REACH_M = 130.0                 # m round the tile the lines are read (the next mast of a span that ends outside it)
# the model's attach points over its origin (tools/blender/build_catenary.py): the registration arm's tip, the
# messenger's insulator, and how far out
ARM_OUT_M = 3.45
Z_CONTACT = 6.18
Z_MESSENGER = 7.80
RAIL_TOP_M = 0.58               # the rail head over the bed line (railways.RAIL_TOP_M's ballast + sleeper + rail)
MAST_Z_BAND = (-0.5, 0.2)       # the mast's base over the track's bed at most this far off it
MESSENGER_SAG_M = 1.3           # at mid-span
DROPPER_EVERY_M = 10.0
CONTACT_R, MESSENGER_R, DROPPER_R = 0.016, 0.013, 0.007
ELECTRIC = {"contact_line", "yes", "rail"}
MIN_SPAN_DRAWN_M = 8.0
MAX_SPAN_DRAWN_M = 110.0


def _h01(key, salt):
    return int.from_bytes(hashlib.blake2s(f"{key}/{salt}".encode(), digest_size=4).digest(), "little") / 2 ** 32


def electrified(cells):
    """{fid: True} of the lines OSM says have a contact line (railtags' osm_electrified)."""
    out = {}
    for f in cells:
        if f is None or not len(f):
            continue
        e = f.attrs.get("osm_electrified")
        if e is None:
            continue
        for i in range(len(f)):
            if e[i] and str(e[i]).split(";")[0].strip() in ELECTRIC:
                out[str(f.ids[i])] = True
    return out


def _arc(xy):
    return np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]


def _frame_at(xy, s, arc):
    """(point, unit direction) on polyline xy at arc length s (arc: its arc lengths)."""
    s = float(np.clip(s, 0.0, arc[-1]))
    j = int(np.clip(np.searchsorted(arc, s) - 1, 0, len(xy) - 2))
    d = xy[j + 1] - xy[j]
    n = max(float(np.hypot(*d)), 1e-9)
    t = (s - arc[j]) / n
    return xy[j] + d * t, d / n


def _radius(xy, arc, s, win=20.0):
    """Radius of the line round arc s from its heading change over +-win m (inf on a straight)."""
    _, a = _frame_at(xy, s - win, arc)
    _, b = _frame_at(xy, s + win, arc)
    ang = abs(math.atan2(a[0] * b[1] - a[1] * b[0], float(a @ b)))
    return float("inf") if ang < 1e-4 else 2.0 * win / ang


def span_at(xy, arc, s):
    """The span (m) starting round arc s: SPAN_M on a straight, sqrt(8 R k) on a curve (SPAN_MIN_M .. SPAN_MAX_M)."""
    r = _radius(xy, arc, s)
    return SPAN_MAX_M if not math.isfinite(r) else float(np.clip(math.sqrt(8.0 * r * CURVE_K), SPAN_MIN_M, SPAN_M))


def stations(xy, key):
    """Arc lengths of the procedural masts along xy: from a phase hashed from key, the span by the curve there.
    Deterministic (every tile of the same line finds the same masts)."""
    arc = _arc(xy)
    s = _h01(key, "cat") * SPAN_M
    out = []
    while s < arc[-1]:
        out.append(s)
        s += span_at(xy, arc, s)
    return out, arc


def _dist_to_lines(p, lines):
    """Distance from point p to the nearest of polylines (each (k, 2))."""
    best = float("inf")
    for ln in lines:
        a, b = ln[:-1], ln[1:]
        d = b - a
        t = np.clip(((p - a) * d).sum(1) / np.maximum((d * d).sum(1), 1e-12), 0.0, 1.0)
        best = min(best, float(np.hypot(*(a + t[:, None] * d - p).T).min()))
    return best


def mapped_masts(frame, cells):
    """[(x, y)] of OSM's catenary mast / portal nodes of the cells in the frame."""
    pts = [(lo, la) for f in cells if f is not None for lo, la, t in f.__dict__.get("_rail_nodes", ())
           if t.get("power") in ("catenary_mast", "catenary_portal")]
    if not pts:
        return np.zeros((0, 2))
    a = np.array(pts, np.float64)
    e, n, _ = frame.geodetic_to_enu(a[:, 1], a[:, 0], np.zeros(len(a)))
    return np.stack([np.asarray(e), np.asarray(n)], -1)


def _project(xy, arc, p):
    """(arc s, signed lateral m (left +), distance m) of point p on polyline xy."""
    a, b = xy[:-1], xy[1:]
    d = b - a
    seg = np.maximum((d * d).sum(1), 1e-12)
    t = np.clip(((p - a) * d).sum(1) / seg, 0.0, 1.0)
    q = a + t[:, None] * d
    dist = np.hypot(*(q - p).T)
    j = int(np.argmin(dist))
    u = d[j] / math.sqrt(seg[j])
    lat = float(u[0] * (p[1] - q[j, 1]) - u[1] * (p[0] - q[j, 0]))
    return float(arc[j] + t[j] * math.sqrt(seg[j])), lat, float(dist[j])


def _road_lines(frame, cells, box, reach):
    """[(centreline (k, 2), half width)] of the roads near box."""
    import mapgeom
    import roads as roadsmod
    near = (box[0] - reach, box[1] - reach, box[2] + reach, box[3] + reach)
    out, seen = [], set()
    for feats in cells:
        if feats is None or not len(feats):
            continue
        specs = roadsmod.road_specs(feats)
        fb = feats.boxes(frame)
        for i, sp in enumerate(specs):
            fid = str(feats.ids[i])
            if sp is None or fid in seen or not (fb[i][0] <= near[2] and fb[i][2] >= near[0] and
                                                  fb[i][1] <= near[3] and fb[i][3] >= near[1]):
                continue
            seen.add(fid)
            for ring in roadsmod.ground_parts(feats, i):
                e, n = mapgeom.enu_xy(feats, frame, ring)
                if len(e) >= 2:
                    out.append((np.c_[e, n], sp.width / 2))
    return out


def _clear_of_roads(p, roads):
    return all(_dist_to_lines(p, [ln]) > hw + ROAD_CLEAR_M for ln, hw in roads)


def masts_of(frame, cells, box, ground):
    """The masts of the electrified lines round box: [dict(fid, xy, s, side, dir, z, base)] one per mast of a line,
    with 'tip' the contact wire's attach point and 'axis' the track's axis point. Not clipped to the tile: the spans'
    ends are needed (rows_and_wires picks the tile's)."""
    import railways
    lines = electrified(cells)
    if not lines:
        return []
    parts = [(fid, xy) for fid, _c, kind, xy in railways.parts(frame, cells, box, reach=REACH_M)
             if kind == "ground" and lines.get(str(fid)) and len(xy) >= 2]
    if not parts:
        return []
    roads = _road_lines(frame, cells, box, REACH_M)
    all_xy = [xy for _f, xy in parts]
    mapped = mapped_masts(frame, cells)
    out = []
    for fid, xy in parts:
        others = [o for o in all_xy if o is not xy]
        stn, arc = stations(xy, fid)
        placed = {}
        if len(mapped):             # (mapped masts along it, where they are the line's: gaps within MAPPED_GAP_M)
            hits = sorted(((s, lat, d) for s, lat, d in (_project(xy, arc, p) for p in mapped) if d <= MAPPED_NEAR_M),
                          key=lambda h: h[0])
            if len(hits) >= 3 and max(np.diff([h[0] for h in hits])) <= MAPPED_GAP_M:
                stn = []
                for s, lat, d in hits:
                    if not stn or s - stn[-1] > 15.0:
                        stn.append(s)
                        placed[len(stn) - 1] = (1 if lat >= 0 else -1, float(np.clip(d, 2.5, 6.0)))
        for k, s0 in enumerate(stn):
            side0, off = placed.get(k, (1 if _h01(fid, "side") < 0.5 else -1, MAST_OFF_M))
            got = None
            for shift in SHIFT_M:
                s = s0 + shift
                if not 0.0 <= s <= arc[-1]:
                    continue
                p, u = _frame_at(xy, s, arc)
                n = np.array([-u[1], u[0]])
                for side in (side0, -side0):
                    q = p + n * side * off
                    if others and _dist_to_lines(q, others) < BETWEEN_M:
                        continue
                    if not _clear_of_roads(q, roads):
                        continue
                    got = (s, p, u, q, side)
                    break
                if got:
                    break
            if got is None:
                continue
            s, p, u, q, side = got
            zax = float(np.asarray(ground(np.array([p])), np.float64).ravel()[0])
            zq = float(np.asarray(ground(np.array([q])), np.float64).ravel()[0])
            z = float(np.clip(zq, zax + MAST_Z_BAND[0], zax + MAST_Z_BAND[1]))
            toward = (p - q) / max(float(np.hypot(*(p - q))), 1e-9)
            out.append({"fid": fid, "s": s, "q": q, "axis": p, "z": z, "zax": zax, "dir": toward, "u": u,
                        "pos": len(out)})
    return out


def rows_and_wires(masts, box_owns):
    """(prop rows, wire polylines) of the masts: a mast row for each the tile holds, each span of a line whose first
    mast the tile holds: contact wire, messenger with its sag, droppers."""
    rows, wires = [], []
    by_line = {}
    for m in masts:
        by_line.setdefault(m["fid"], []).append(m)
    for fid, ms in by_line.items():
        ms.sort(key=lambda m: m["s"])
        for a, b in zip(ms[:-1], ms[1:]):
            L = b["s"] - a["s"]
            span = float(np.hypot(*(b["axis"] - a["axis"])))
            if not (MIN_SPAN_DRAWN_M <= span <= MAX_SPAN_DRAWN_M and L <= MAX_SPAN_DRAWN_M * 1.3):
                continue
            if not box_owns(float(a["q"][0]), float(a["q"][1])):
                continue
            tip = [np.r_[m["q"] + m["dir"] * ARM_OUT_M, m["z"]] for m in (a, b)]
            c0, c1 = tip[0].copy(), tip[1].copy()
            c0[2], c1[2] = a["z"] + Z_CONTACT, b["z"] + Z_CONTACT
            m0, m1 = c0.copy(), c1.copy()
            m0[2], m1[2] = a["z"] + Z_MESSENGER, b["z"] + Z_MESSENGER
            n = max(2, int(math.ceil(span / 10.0)))
            ts = np.linspace(0.0, 1.0, n + 1)
            contact = np.array([c0 + (c1 - c0) * t for t in ts])
            mess = np.array([m0 + (m1 - m0) * t for t in ts])
            mess[:, 2] -= MESSENGER_SAG_M * 4.0 * ts * (1.0 - ts)
            wires.append((contact, CONTACT_R))
            wires.append((mess, MESSENGER_R))
            k = max(1, int(span // DROPPER_EVERY_M))
            for i in range(1, k):
                t = i / k
                top = m0 + (m1 - m0) * t
                top[2] -= MESSENGER_SAG_M * 4.0 * t * (1.0 - t)
                bot = c0 + (c1 - c0) * t
                if top[2] - bot[2] > 0.15:
                    wires.append((np.array([bot, top]), DROPPER_R))
    for m in masts:
        if box_owns(float(m["q"][0]), float(m["q"][1])):
            yaw = math.degrees(math.atan2(m["dir"][1], m["dir"][0])) % 360.0
            rows.append([GROUP, round(float(m["q"][0]), 2), round(float(m["q"][1]), 2), round(m["z"], 2),
                         round(yaw, 1), 1.0, MAST_H, 0.0])
    return rows, wires


def build(frame, tx, ty, cells, ground):
    """(prop rows, wire lines) of road tile (tx, ty): the catenary of the electrified lines round it."""
    import roads as roadsmod
    box = roadsmod.tile_bbox(tx, ty)
    masts = masts_of(frame, cells, box, ground)
    return rows_and_wires(masts, lambda x, y: roadsmod.box_owns(box, x, y))
