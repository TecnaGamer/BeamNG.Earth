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

Opt-in: BNE_CATENARY=1 (railxing's BNE_RAIL_XING is the crossings'). Not done: masts on bridges."""
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
BUILDING_CLEAR_M = 1.5          # m a mast keeps off a building's outline (it stood in a hut's wall, Kalynivka)
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
MESSENGER_SAG_M = 1.12          # at mid-span: leaves 0.5 m between the messenger and the contact wire there (Z_MESSENGER - Z_CONTACT = 1.62)
DROPPER_EVERY_M = 6.0           # m between droppers (the user, 2026-11-10: a metre, then 3 m, then "6 m"; real lines: 4-6 m)
DROPPER_FIRST_M = 3.0           # m from a mast to its first dropper
DROPPER_MIN_GAP_M = 0.12        # a dropper shorter than this is not drawn
CONTACT_R, MESSENGER_R, DROPPER_R = 0.016, 0.013, 0.010
JOINT_M = 1.5                   # m: part ends this close are one line (a joint of two OSM ways, a switch's branch)
LINK_MAX_M = 150.0              # m: the longest span a joint's link may be
# the style of a country's masts: "su" the concrete spun mast (ex-USSR, and where concrete is the usual: Poland,
# Czechia, Slovakia, Hungary, Romania, the Baltics), "eu" the steel H-beam mast (Germany and the rest: DB uses both,
# Bane NOR steel; the others' norms were not found: a guess, agreed with the user 2026-11-05). STYLE_GROUPS: mast, post.
SU_COUNTRIES = frozenset("AM AZ BY EE GE KG KZ LT LV MD RU TJ TM UA UZ PL CZ SK HU RO".split())
STYLE_GROUPS = {"su": ("catmast", "catpost"), "eu": ("catmast_eu", "catpost_eu")}
PORTAL_MIN_TRACKS = 3           # a rigid portal (a truss girder on two posts) where this many tracks run side by side
PORTAL_BAND_M = 24.0            # m across: tracks this close and parallel are one corridor
PORTAL_COS = 0.9
PORTAL_MAX_W_M = 60.0           # a wider corridor (a yard's fan) gets none
PORTAL_POST_OFF_M = 3.4         # m from the outer tracks' axes to the posts
PORTAL_MERGE_M = 14.0          # m: a portal this close along the line to one already placed is merged into it (two overlapping girders, Vinnytsia)
PORTAL_POST_CLEAR_M = 2.3       # m a post keeps off every track's axis (a post stood in the track bed beside a siding, Vinnytsia)
PORTAL_Z = 8.0                  # m: the truss's lower chord over the tracks' bed (the contact wires hang from it)
PORTAL_H = 1.0                  # m the truss girder is deep
PORTAL_PANEL_M = 1.0            # m of a diagonal's panel
POST_H = 10.4                   # m (the post models' height)
CHORD_R, DIAG_R = 0.05, 0.025
HOLDER_INS_M = 0.4             # m a rod insulator is long (two of them with a gap hold a contact wire to the girder)
INS_R, CLAMP_R = 0.035, 0.03   # their half widths
HANGER_R = 0.022               # the holder's hanger rod (a 2 cm ribbon vanished at a distance and left the insulators floating)
PORTAL_VERT_M = 2.0            # m between the girder's verticals (no diagonals: the user, 2026-11-10, "remove the triangles")   # the girder's bars' half widths (drawn as the tile's wire ribbons)
ELECTRIC = {"contact_line", "yes"}     # (not "rail": a third rail has no overhead line)
MIN_SPAN_DRAWN_M = 0.3         # (a portal and a mast may stand close: the wire must still join them)
MAX_SPAN_DRAWN_M = 200.0


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


def near_ring(p, rings, m=BUILDING_CLEAR_M):
    """Whether point p (2,) is inside one of the footprints rings (k, 2) or within m of its outline."""
    for r in rings:
        if p[0] < r[:, 0].min() - m or p[0] > r[:, 0].max() + m or p[1] < r[:, 1].min() - m or p[1] > r[:, 1].max() + m:
            continue
        a, b = r, np.roll(r, -1, axis=0)
        d = b - a
        t = np.clip(((p - a) * d).sum(1) / np.maximum((d * d).sum(1), 1e-12), 0.0, 1.0)
        if float(np.hypot(*(a + t[:, None] * d - p).T).min()) <= m:
            return True
        # (inside: an even number of the outline's edges cross a ray to +x)
        y0, y1 = a[:, 1], b[:, 1]
        hit = ((y0 > p[1]) != (y1 > p[1])) & (p[0] < (b[:, 0] - a[:, 0]) * (p[1] - y0) / np.where(y1 == y0, 1e-12, y1 - y0) + a[:, 0])
        if int(hit.sum()) % 2 == 1:
            return True
    return False


def _clear_of_roads(p, roads):
    return all(_dist_to_lines(p, [ln]) > hw + ROAD_CLEAR_M for ln, hw in roads)


def style_of(country):
    """"su" or "eu" for an ISO alpha-2 country (None: "eu")."""
    return "su" if str(country or "").upper() in SU_COUNTRIES else "eu"


def _nearest(xy, arc, p):
    """(arc s, point, unit direction, distance) of polyline xy at its point nearest p."""
    a, b = xy[:-1], xy[1:]
    d = b - a
    seg = np.maximum((d * d).sum(1), 1e-12)
    t = np.clip(((p - a) * d).sum(1) / seg, 0.0, 1.0)
    q = a + t[:, None] * d
    dist = np.hypot(*(q - p).T)
    j = int(np.argmin(dist))
    return float(arc[j] + t[j] * math.sqrt(seg[j])), q[j], d[j] / math.sqrt(seg[j]), float(dist[j])


def corridor(parts, arcs, i, p, u):
    """[(index, arc, point, unit direction)] of the other parts running side by side with part i at point p (direction
    u): within PORTAL_BAND_M and parallel (PORTAL_COS). Pure."""
    out = []
    for j, (_f, xy) in enumerate(parts):
        if j == i:
            continue
        s, q, uj, d = _nearest(xy, arcs[j], p)
        if d <= PORTAL_BAND_M and abs(float(u @ uj)) >= PORTAL_COS and 0.0 < s < arcs[j][-1]:
            out.append((j, s, q, uj))
    return out


def _overlaps(new, placed):
    """Whether the portal entry `new` stands within PORTAL_MERGE_M of one of the placed portal entries (the midpoints of
    their posts): two girders over the same tracks a few metres apart are one. Pure."""
    c = (new["posts"][0][0] + new["posts"][1][0]) / 2.0
    for m in placed:
        c2 = (m["posts"][0][0] + m["posts"][1][0]) / 2.0
        if float(np.hypot(*(c - c2))) < PORTAL_MERGE_M:
            return True
    return False


def _portal(parts, arcs, pi, s0, p0, u0, mates, roads, rings, ground):
    """The entries of a rigid portal at arc s0 of part pi: a "portal" entry (its two posts) and a virtual mast of each
    track of the corridor (its wires start over its axis from the girder); [] where its posts cannot stand (roads,
    buildings) or the corridor is wider than PORTAL_MAX_W_M."""
    n = np.array([-u0[1], u0[0]])
    members = [(pi, s0, p0, u0)] + list(mates)
    lat = [float((q - p0) @ n) for _j, _s, q, _u in members]
    lo, hi = min(lat) - PORTAL_POST_OFF_M, max(lat) + PORTAL_POST_OFF_M
    if hi - lo > PORTAL_MAX_W_M + 2 * PORTAL_POST_OFF_M:
        return []
    got, fallback = None, None
    every = [xy for _f, xy in parts]
    for shift in SHIFT_M + (12.0, -12.0, 15.0, -15.0):
        c = p0 + u0 * shift
        a, b = c + n * lo, c + n * hi
        if all(_clear_of_roads(q, roads) and not (rings and near_ring(q, rings)) for q in (a, b)):
            if all(_dist_to_lines(q, every) >= PORTAL_POST_CLEAR_M for q in (a, b)):
                got = (a, b)
                break
            if fallback is None:
                fallback = (a, b)         # (clear of roads but on a track: used when no shift is clear of both)
    got = got or fallback
    if got is None:
        return []

    def z_at(q):
        return float(np.asarray(ground(np.array([q])), np.float64).ravel()[0])
    zs = [z_at(q) for _j, _s, q, _u in members]
    zm = float(np.mean(zs))
    posts = [(q, float(np.clip(z_at(q), zm - 0.5, zm + 0.2))) for q in got]
    out = [{"portal": True, "fid": "portal", "s": s0, "q": got[0], "axis": got[0], "posts": posts, "z": zm,
            "tracks": [(q, z) for (_j, _s, q, _u), z in zip(members, zs)]}]
    for (j, s, q, uj), z in zip(members, zs):
        out.append({"fid": parts[j][0], "part": j, "s": s, "q": q, "axis": q, "z": z, "zax": z, "dir": np.zeros(2),
                    "u": uj, "virtual": True})
    return out


def masts_of(frame, cells, box, ground, rings=()):
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
    arcs = [_arc(xy) for xy in all_xy]
    mapped = mapped_masts(frame, cells)
    out = []
    for pi, (fid, xy) in enumerate(parts):
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
            p0, u0 = _frame_at(xy, s0, arc)
            mates = corridor(parts, arcs, pi, p0, u0)
            if len(mates) + 1 >= PORTAL_MIN_TRACKS:          # side by side: a portal, from the corridor's first part
                if all(pi < j for j, *_ in mates):
                    ents = _portal(parts, arcs, pi, s0, p0, u0, mates, roads, rings, ground)
                    if ents and not _overlaps(ents[0], [m for m in out if m.get("portal")]):
                        out.extend(ents)
                continue                                      # (no cantilever masts between the tracks)
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
                    if not _clear_of_roads(q, roads) or (rings and near_ring(q, rings)):
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
            out.append({"fid": fid, "part": pi, "s": s, "q": q, "axis": p, "z": z, "zax": zax, "dir": toward,
                        "u": u, "pos": len(out)})
    out.extend(_joints(parts, arcs, out, ground))
    return out


def _tip(m):
    """The point of a mast's contact wire (its attach point over the track's axis, z the contact wire's height)."""
    return np.r_[m["q"] + m["dir"] * ARM_OUT_M, m["z"] + Z_CONTACT]


def _joints(parts, arcs, masts, ground):
    """Entries joining the wires where a line's pieces meet: {"link": (a, b)} (two mast dicts, drawn as a span) for
    two ways whose ends meet (JOINT_M), and for the end of a way that meets another way's side (a switch's branch) a
    virtual mast at the end (the branch's wire starts on the main track's) linked to the branch's last mast. Without
    these every joint of two OSM ways cut the wire (Vinnytsia, Kalynivka: the user's shots, 2026-11-10)."""
    by_part = {}
    for m in masts:
        if not m.get("portal") and "part" in m:
            by_part.setdefault(m["part"], []).append(m)
    for ms in by_part.values():
        ms.sort(key=lambda m: m["s"])
    ends = []        # (part, end point, which end 0/1, the mast nearest that end)
    for pi, (_f, xy) in enumerate(parts):
        ms = by_part.get(pi)
        if not ms:
            continue
        ends.append((pi, xy[0], 0, ms[0]))
        ends.append((pi, xy[-1], 1, ms[-1]))
    out, used = [], set()
    for i, (pa, ea, ka, ma) in enumerate(ends):
        for j in range(i + 1, len(ends)):
            pb, eb, kb, mb = ends[j]
            if pa != pb and float(np.hypot(*(ea - eb))) <= JOINT_M:
                out.append({"link": (ma, mb)})
                used.add(i)
                used.add(j)
    for i, (pa, ea, ka, ma) in enumerate(ends):
        if i in used:
            continue
        for pj, (_f, xyj) in enumerate(parts):
            if pj == pa:
                continue
            s, q, uj, d = _nearest(xyj, arcs[pj], ea)
            if d > JOINT_M * 2 or not 1.0 < s < arcs[pj][-1] - 1.0:
                continue
            z = float(np.asarray(ground(np.array([q])), np.float64).ravel()[0])
            # (the anchor stands on the other track's axis, where its contact wire runs: the branch's end may be up to
            # 2 * JOINT_M off it, and the wire then ended a metre short of the main wire, the user's shots 2026-11-10)
            end = {"fid": parts[pa][0], "part": pa, "s": 0.0 if ka == 0 else float(arcs[pa][-1]), "q": q.copy(), "axis": q.copy(),
                   "z": z, "zax": z, "dir": np.zeros(2), "u": uj, "virtual": True, "end": True}
            out.append({"link": (ma, end)})
            used.add(i)
            break
    return out


def portal_wires(pt):
    """The girder of a portal entry as wire-ribbon polylines: two chords, a zigzag of diagonals and a hanger down to
    each track's contact wire. Pure."""
    (a, za), (b, zb) = pt["posts"]
    zbot = pt["z"] + PORTAL_Z
    ztop = zbot + PORTAL_H
    L = float(np.hypot(*(b - a)))
    n = max(2, int(round(L / PORTAL_VERT_M)))
    ts = np.linspace(0.0, 1.0, n + 1)
    xy = np.array([a + (b - a) * t for t in ts])
    wires = [(np.c_[xy[[0, -1]], [zbot, zbot]], CHORD_R), (np.c_[xy[[0, -1]], [ztop, ztop]], CHORD_R)]
    for q in xy[1:-1]:           # (verticals flush with the chords: the bars meet at their ends and run into them)
        wires.append((np.array([[q[0], q[1], zbot - CHORD_R], [q[0], q[1], ztop + CHORD_R]]), DIAG_R))
    u = (b - a) / max(L, 1e-9)
    for q, z in pt["tracks"]:
        zc = z + Z_CONTACT
        # the holder of this track's contact wire: a rod insulator (two thick sticks with a gap) under the lower
        # chord, the hanger, and the registration arm (a short clamp along the girder at the contact wire)
        zi = zbot - HOLDER_INS_M
        wires.append((np.array([[q[0], q[1], zbot], [q[0], q[1], zi]]), INS_R))
        wires.append((np.array([[q[0], q[1], zi], [q[0], q[1], zi - HOLDER_INS_M * 0.6]]), INS_R))
        wires.append((np.array([[q[0], q[1], zi - HOLDER_INS_M * 0.6], [q[0], q[1], zc + 0.12]]), HANGER_R))
        for sgn in (-1.0, 1.0):          # (the clamp's two arms, under the girder's axis)
            e = q + u * (0.3 * sgn)
            wires.append((np.array([[q[0], q[1], zc + 0.12], [e[0], e[1], zc + 0.05]]), HANGER_R))
        wires.append((np.array([[q[0] - u[0] * 0.15, q[1] - u[1] * 0.15, zc], [q[0] + u[0] * 0.15, q[1] + u[1] * 0.15, zc]]), CLAMP_R))
    # a stub of the girder's end plate where each post meets the chords
    for (pq, _pz) in pt["posts"]:
        wires.append((np.array([[pq[0], pq[1], zbot - 0.4], [pq[0], pq[1], ztop + 0.3]]), CHORD_R * 1.6))
    return wires


MAT_COPPER, MAT_BRONZE, MAT_STEEL, MAT_INSULATOR = "bne_cat_wire_copper", "bne_cat_wire_bronze", "bne_cat_wire_steel", "bne_cat_wire_ins"


def wire_material(r, default="bne_wire"):
    """The material of a wire ribbon by its half width (the catenary's classes are told apart by it): the contact wire
    copper, the messenger and droppers bronze, the girder's bars galvanised steel, insulators dark porcelain; any other
    (a power line's) `default`."""
    if r == CONTACT_R:
        return MAT_COPPER
    if r in (MESSENGER_R, DROPPER_R):
        return MAT_BRONZE
    if r in (CHORD_R, DIAG_R, CHORD_R * 1.6, CLAMP_R, HANGER_R):
        return MAT_STEEL
    if r == INS_R:
        return MAT_INSULATOR
    return default


def wire_solid(r):
    """Whether a wire ribbon of half width r is drawn as a four-sided bar: the girder's bars, the insulators and clamps
    (the thin wires stay ribbons)."""
    return r in (CHORD_R, DIAG_R, CHORD_R * 1.6, CLAMP_R, INS_R, HANGER_R)


def span_wires(a, b):
    """The wires of one span between masts a and b: the contact wire, the messenger with its sag, the droppers every
    DROPPER_EVERY_M (the first DROPPER_FIRST_M from a mast, none shorter than DROPPER_MIN_GAP_M). Pure."""
    c0, c1 = _tip(a), _tip(b)
    m0, m1 = c0.copy(), c1.copy()
    m0[2], m1[2] = a["z"] + Z_MESSENGER, b["z"] + Z_MESSENGER
    if b.get("end"):            # (a branch's wire ends on the main track's contact wire: its messenger comes down to it
        m1[2] = c1[2]           # rather than ending in the air a metre and a half over it)
    if a.get("end"):
        m0[2] = c0[2]
    span = float(np.hypot(*(c1[:2] - c0[:2])))
    n = max(2, int(math.ceil(span / 10.0)))
    ts = np.linspace(0.0, 1.0, n + 1)
    contact = np.array([c0 + (c1 - c0) * t for t in ts])
    mess = np.array([m0 + (m1 - m0) * t for t in ts])
    mess[:, 2] -= MESSENGER_SAG_M * 4.0 * ts * (1.0 - ts)
    out = [(contact, CONTACT_R), (mess, MESSENGER_R)]
    first = min(DROPPER_FIRST_M, span / 2.0)
    k = max(1, int(round((span - 2.0 * first) / DROPPER_EVERY_M)))
    for t in ([first / span + (1.0 - 2.0 * first / span) * i / k for i in range(k + 1)] if span > 4.0 else [0.5]):
        top = m0 + (m1 - m0) * t
        top[2] -= MESSENGER_SAG_M * 4.0 * t * (1.0 - t)
        bot = c0 + (c1 - c0) * t
        if top[2] - bot[2] > DROPPER_MIN_GAP_M:
            out.append((np.array([bot, top]), DROPPER_R))
    return out


def rows_and_wires(masts, box_owns, style="su"):
    """(prop rows, wire polylines) of the masts: a mast row for each the tile holds, each span of a line whose first
    mast the tile holds: contact wire, messenger with its sag, droppers. A portal entry: its posts (rows) and its girder
    (wires), by the tile that holds its first post; a virtual mast (a portal's track) only anchors the wires."""
    rows, wires = [], []
    g_mast, g_post = STYLE_GROUPS[style]
    by_line, links = {}, []
    for m in masts:
        if "link" in m:
            links.append(m["link"])
            continue
        if m.get("portal"):
            if box_owns(float(m["q"][0]), float(m["q"][1])):
                wires.extend(portal_wires(m))
            d = m["posts"][1][0] - m["posts"][0][0]
            yaw = round(math.degrees(math.atan2(d[1], d[0])) % 360.0, 1)
            for q, z in m["posts"]:
                if box_owns(float(q[0]), float(q[1])):
                    rows.append([g_post, round(float(q[0]), 2), round(float(q[1]), 2), round(z, 2), yaw, 1.0, POST_H,
                                 0.0])
            continue
        # (by part: one feature's pieces each have their own arc lengths, mixed they joined masts of different pieces
        # with diagonal wires, Vinnytsia 2026-11-05)
        by_line.setdefault(m.get("part", m["fid"]), []).append(m)
    for fid, ms in by_line.items():
        ms.sort(key=lambda m: m["s"])
        for a, b in zip(ms[:-1], ms[1:]):
            L = b["s"] - a["s"]
            span = float(np.hypot(*(b["axis"] - a["axis"])))
            if not (MIN_SPAN_DRAWN_M <= span <= MAX_SPAN_DRAWN_M and L <= MAX_SPAN_DRAWN_M * 1.3):
                continue
            if not box_owns(float(a["q"][0]), float(a["q"][1])):
                continue
            wires.extend(span_wires(a, b))
    for lk in links:
        a, b = lk
        span = float(np.hypot(*(_tip(b)[:2] - _tip(a)[:2])))
        if span <= LINK_MAX_M and box_owns(float(a["q"][0]), float(a["q"][1])):
            wires.extend(span_wires(a, b))
    for m in masts:
        if m.get("portal") or m.get("virtual") or "link" in m:
            continue
        if box_owns(float(m["q"][0]), float(m["q"][1])):
            yaw = math.degrees(math.atan2(m["dir"][1], m["dir"][0])) % 360.0
            rows.append([g_mast, round(float(m["q"][0]), 2), round(float(m["q"][1]), 2), round(m["z"], 2),
                         round(yaw, 1), 1.0, MAST_H, 0.0])
    return rows, wires


def build(frame, tx, ty, cells, ground, rings=(), country=None):
    """(prop rows, wire lines) of road tile (tx, ty): the catenary of the electrified lines round it. rings: the
    buildings' footprints (anchor frame) the masts keep off."""
    import roads as roadsmod
    box = roadsmod.tile_bbox(tx, ty)
    masts = masts_of(frame, cells, box, ground, rings)
    return rows_and_wires(masts, lambda x, y: roadsmod.box_owns(box, x, y), style_of(country))
