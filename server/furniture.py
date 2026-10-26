"""Street furniture (the user, 2026-10-13: "make the world look alive"): street lights along the sidewalks
of town streets and wooden utility poles along country roads, placed per road tile with the road tile's
geometry (roads.build_tile) and shown by the mod as Forest items (trees.lua groups "streetlight", "pole").

Spacing from the stock maps and US practice: West Coast USA's lamps stand ~17 m from their nearest
neighbour (often across the street; ~30-40 m along one side), residential streets in US towns light one
side every ~35-45 m, arterials both sides staggered every ~30 m; utility poles every ~40-50 m on one side.
Each road's lamps sit at fixed distances along the road from its first vertex with a phase hashed from the
road's id, so every tile (and every anchor) puts them in the same places (ROADMAP "deterministic
generation"). Pure.
"""
import hashlib
import math
import os

import numpy as np

# class: (spacing m, both sides staggered)
LAMP_SPACING = {"trunk": (30.0, True), "primary": (30.0, True), "secondary": (32.0, True), "tertiary": (36.0, True),
                "residential": (40.0, False), "unclassified": (42.0, False), "living_street": (36.0, False)}
LAMP_CURB_M = 0.5           # m from the curb onto the sidewalk
POLE_SPACING = 45.0
POLE_CLASSES = {"secondary", "tertiary", "residential", "unclassified"}
POLE_OFF_M = 2.4            # m beyond the carriageway's edge
END_CLEAR_M = 4.0           # m kept clear of a run's ends (corners, crosswalk ramps)
CLEAR_OTHER_M = 1.5         # m kept off another road's carriageway
G_LAMP, G_POLE = "streetlight", "pole"
# the sign set of 2026-10-25 (roadsigns.py: bend warnings, advisory plaques, chevrons). Off until the mod ships
# their models: trees.lua draws a group it has no shapes for as a broadleaf tree.
SIGNS2 = os.environ.get("BNE_SIGNS2", "on") == "on"     # (on since the models ship: us_signs.lua)


def _h01(*vals):
    h = hashlib.blake2b(repr(vals).encode(), digest_size=8).digest()
    return int.from_bytes(h, "little") / 2.0 ** 64


def _arclen(p):
    d = np.hypot(*np.diff(p[:, :2], axis=0).T)
    return np.concatenate([[0.0], np.cumsum(d)])


def _project(line, q):
    """Arc length along polyline line (k, 2+) of the nearest point to each q (m, 2)."""
    a, b = line[:-1, :2], line[1:, :2]
    d = b - a
    L2 = np.maximum((d * d).sum(1), 1e-12)
    t = np.clip(((q[:, None, :] - a[None]) * d[None]).sum(2) / L2[None], 0, 1)
    p = a[None] + t[..., None] * d[None]
    dist = np.hypot(*(q[:, None, :] - p).transpose(2, 0, 1))
    k = dist.argmin(1)
    s = _arclen(line)
    return s[k] + t[np.arange(len(q)), k] * np.sqrt(L2[k])


def _at(line, s):
    """Points (and unit directions) at arc lengths s along line (k, 3)."""
    ss = _arclen(line)
    s = np.clip(s, 0, ss[-1])
    k = np.clip(np.searchsorted(ss, s, side="right") - 1, 0, len(line) - 2)
    seg = line[k + 1] - line[k]
    L = np.maximum(ss[k + 1] - ss[k], 1e-9)
    t = (s - ss[k]) / L
    p = line[k] + seg * t[:, None]
    d = seg[:, :2] / L[:, None]
    return p, d


def _yaw(dx, dy):
    """Degrees of a Forest item's rotation that turns its local +x toward (dx, dy) (trees.lua: setFromEuler
    with -yaw about z, the engine's clockwise headings)."""
    return math.degrees(math.atan2(dy, dx)) % 360.0


def _clear(p, others, own):
    import fast
    if not isinstance(others, fast.Others):
        others = fast.Others(others)
    return bool(others.clear(p, own, CLEAR_OTHER_M))


WIRE_ARMS = (-0.8, 0.0, 0.8)    # m along the cross-arm (the pole's local x) of the three wires
WIRE_Z = 7.9                    # m above the pole's base (s_powerpole_3wire: 8.2 m tall above its origin)
WIRE_SAG = 0.012                # sag per metre of span
WIRE_SEG_M = 5.0
WIRE_R = 0.012                  # m half width of a wire's ribbons


def wire_lines(poles):
    """Polylines (k, 3) of the wires between consecutive poles [(x, y, z, yaw_deg)] of one road side."""
    out = []
    for (x0, y0, z0, w0), (x1, y1, z1, w1) in zip(poles[:-1], poles[1:]):
        span = math.hypot(x1 - x0, y1 - y0)
        if span < 5.0 or span > 80.0:
            continue
        k = max(2, int(math.ceil(span / WIRE_SEG_M)))
        t = np.linspace(0.0, 1.0, k + 1)
        for off in WIRE_ARMS:
            a = np.array([x0 + math.cos(math.radians(w0)) * off, y0 + math.sin(math.radians(w0)) * off, z0 + WIRE_Z])
            b = np.array([x1 + math.cos(math.radians(w1)) * off, y1 + math.sin(math.radians(w1)) * off, z1 + WIRE_Z])
            p = a[None] + (b - a)[None] * t[:, None]
            p[:, 2] -= 4.0 * WIRE_SAG * span * t * (1.0 - t)
            out.append(p)
    return out


def wire_mesh(lines, origin):
    """A mesh of the wires: two crossed ribbons per segment (seen from any side): (positions, tris, uvs)."""
    P, T = [], []
    ox, oy, oz = origin
    for line in lines:
        for a, b in zip(line[:-1], line[1:]):
            d = b - a
            L = float(np.linalg.norm(d))
            if L < 1e-6:
                continue
            d /= L
            side = np.cross(d, [0.0, 0.0, 1.0])
            side = side / (np.linalg.norm(side) or 1.0) * WIRE_R
            up = np.cross(side, d)
            up = up / (np.linalg.norm(up) or 1.0) * WIRE_R
            for v in (side, up):
                i = len(P)
                P += [a - v, b - v, b + v, a + v]
                T += [(i, i + 1, i + 2), (i, i + 2, i + 3), (i, i + 2, i + 1), (i, i + 3, i + 2)]
    if not P:
        return None
    P = np.array(P) - np.array([ox, oy, oz])
    return P, np.array(T, np.int64), np.zeros((len(P), 2))


MAJOR = {"primary", "secondary", "tertiary", "trunk"}
MINOR = {"residential", "unclassified", "living_street"}
G_SIGNAL, G_STOP, G_YIELD = "signal", "stop", "giveway"
JUNCTION_SNAP_M = 1.0       # road ends this close are one junction


def _arms(roads):
    """Junctions: {(x, y) rounded: [(fid, spec, unit vector away from the junction, point, at its start, road
    length), ...]} of road ends that meet (Overture splits segments at their connectors)."""
    ends = {}
    for fid, spec, pts, _ in roads:
        if len(pts) < 2:
            continue
        length = float(np.sum(np.hypot(*np.diff(pts[:, :2], axis=0).T)))
        for p, q, start in ((pts[0], pts[1], True), (pts[-1], pts[-2], False)):
            d = q[:2] - p[:2]
            L = float(np.hypot(*d))
            if L < 1e-3:
                continue
            k = (round(float(p[0]) / JUNCTION_SNAP_M), round(float(p[1]) / JUNCTION_SNAP_M))
            ends.setdefault(k, []).append((fid, spec, d / L, p, start, length))
    return ends


LINK_M = 30.0           # a road this short joining two signalled junctions is a link inside one intersection (the
                        # crossing of a divided road's median): no signal on it


def _outbound(a):
    """Does traffic on arm a (fid, spec, u, p[, at its start[, length]]) only leave the junction (one-way away)?"""
    if len(a) < 5 or not a[1].oneway or not getattr(a[1], "dir", 0):
        return False
    return (a[1].dir > 0) == bool(a[4])


SIGNAL_NEAR_M = 25.0        # m past the junction's widest arm a mapped traffic signal may stand (OSM maps them at the
                            # junction's node or at each approach's stop line)


def mapped_signals(frame, infra_list):
    """(n, 2) frame xy of OpenStreetMap's traffic signals (Overture infrastructure transportation/traffic_signals) in
    infra_list (infrastructure Features of map cells, duplicates taken once); None when no layer could be read (the
    junctions then fall back to _guess_signalled)."""
    if not infra_list or all(f is None for f in infra_list):
        return None
    seen, ll = set(), []
    for f in infra_list:
        if f is None or len(f) == 0:
            continue
        cls, sub = f.attrs.get("class"), f.attrs.get("subtype")
        if cls is None:
            continue
        # (whole arrays: a city cell has thousands of infrastructure features and a road tile reads ~9 cells)
        hit = np.asarray(cls).astype(str) == "traffic_signals"
        if sub is not None:
            hit &= np.asarray(sub).astype(str) == "transportation"
        for i in np.flatnonzero(hit):
            fid = str(f.ids[i])
            if fid in seen:
                continue
            seen.add(fid)
            a = f.coord_off[f.ring_off[f.part_off[i]]]
            b = f.coord_off[f.ring_off[f.part_off[i + 1]]]
            ll.append(f.lonlat[a:b].mean(axis=0))
    if not ll:
        return np.zeros((0, 2))
    ll = np.asarray(ll, np.float64)
    e, n, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.0)
    return np.column_stack([np.asarray(e), np.asarray(n)])


def signals_for(md, frame, keys):
    """mapped_signals of the map cells keys (md: mapdata.MapData), None when their infrastructure cannot be read."""
    try:
        return mapped_signals(frame, [md.cell(k, layers=("infrastructure",), kind="demand").layers.get("infrastructure")
                                      for k in keys])
    except Exception as e:      # guessed signals rather than no road tile
        import logging
        logging.getLogger("earthd.furniture").debug("traffic signals: %s: %s", type(e).__name__, e)
        return None


def _guess_signalled(arms):
    major = [a for a in arms if a[1].cls in MAJOR]
    return len(major) >= 3 or (len(major) >= 2 and len(arms) >= 4)


def _is_signalled(arms, signals=None):
    """Traffic lights at this junction: a mapped signal (signals: mapped_signals' points) within SIGNAL_NEAR_M of its
    widest arm's edge; with no map data (None), where two or more major roads meet (the user, 2026-10-25: lights where
    there are none)."""
    if signals is None:
        return _guess_signalled(arms)
    if not len(signals):
        return False
    c = np.array([sum(float(a[3][0]) for a in arms), sum(float(a[3][1]) for a in arms)]) / len(arms)
    reach = max(a[1].width for a in arms) / 2 + SIGNAL_NEAR_M
    return bool((np.hypot(signals[:, 0] - c[0], signals[:, 1] - c[1]) <= reach).any())


def junction_controls(arms, k, link_ok=None, ring=None, signals=None):
    """[(arm, G_SIGNAL | G_STOP | G_YIELD)] of a junction's arms (fid, spec, unit vector away, point): signals on
    every arm where two or more major roads meet, stop signs on the minor arms meeting a major road, on the
    stem of a minor T and on half the minor four-ways (hashed by the junction's 8 m cell; k is unused). Shared with
    navgraph.py, so the AI's signals and stop signs are where the props stand. ring: the fids of roundabout rings
    (islands.ring_fids): a junction on a ring (two of its arms) has give-way signs on incoming external arms.
    The AI graph handles their priority, without installing stop controllers on the ring. signals: the mapped
    traffic signals near (mapped_signals; None: guessed by the roads' classes)."""
    if ring and sum(1 for a in arms if a[0] in ring) >= 2:
        return [(a, G_YIELD) for a in arms if a[0] not in ring and not _outbound(a)]
    major = [a for a in arms if a[1].cls in MAJOR]
    minor = [a for a in arms if a[1].cls in MINOR]
    if _is_signalled(arms, signals):
        # one head per way in: none on a one-way arm leaving it, none on a short link to the next signalled
        # junction (a divided road crossing a street is two junctions 20 m apart: the user's report 2026-10-14,
        # Boston St, "way too many traffic lights"); link_ok(arm) says whether that link's far end is signalled
        return [(a, G_SIGNAL) for a in arms if a[1].cls in MAJOR | MINOR and not _outbound(a)
                and not (link_ok is not None and len(a) >= 6 and a[5] < LINK_M and link_ok(a))]
    if major:
        return [(a, G_STOP) for a in minor]
    if len(arms) == 3:
        # the stem: the arm least in line with another
        def straightest(a):
            return max(-float(a[2] @ b[2]) for b in arms if b is not a)
        return [(min(arms, key=straightest), G_STOP)] if all(a[1].cls in MINOR for a in arms) else []
    # the all-way coin flip: keyed on an 8 m cell of the junction, the same from the road tiles and the road graph
    # (whose points are at another height: a 1 m key could round differently)
    cx_ = sum(float(a[3][0]) for a in arms) / len(arms)          # (the arms' order differs between the two)
    cy_ = sum(float(a[3][1]) for a in arms) / len(arms)
    key = (int(math.floor(cx_ / 8.0)), int(math.floor(cy_ / 8.0)))
    return [(a, G_STOP) for a in minor] if _h01("allway", key) < 0.5 else []


def _link_check(junctions, signals=None):
    """link_ok(arm) for junction_controls: is the junction at the other end of arm's road a signalled urban one?"""
    where = {}                                  # (fid, at start) -> the junction's arms
    for arms in junctions.values():
        for a in arms:
            where[(a[0], a[4])] = arms
    def link_ok(a):
        other = where.get((a[0], not a[4]))
        return other is not None and len(other) >= 3 and _controlled_here(other, signals) and \
            _is_signalled(other, signals)
    return link_ok


def _controlled_here(arms, signals=None):
    """Does a junction get signals or signs at all: in town (every arm urban), or anywhere a signal is mapped at it."""
    return all(a[1].urban for a in arms) or (signals is not None and _is_signalled(arms, signals))


SIGN_CLEAR_M = 0.6          # m a junction sign stands clear of every carriageway's edge


def _off_carriageways(c, u, right, along, out, lines):
    """The point along m past the junction c along u and out m to the right, moved further along and out (up to
    10 m and 3 m) until it is SIGN_CLEAR_M clear of every carriageway in lines [(half width, centreline)]; None
    when nowhere is: stop signs and signals stood in the middle of a wide road beside the junction (Boston Street,
    Baltimore, the user's report 2026-10-17: 6 in one tile)."""
    for da in (0.0, 2.0, 4.0, 6.0, 8.0, 10.0):
        for do in (0.0, 1.0, 2.0, 3.0):
            q = c + u * (along + da) + right * (out + do)
            if all(_seg_dist_min(q, ln) >= h + SIGN_CLEAR_M for h, ln in lines):
                return q
    return None


def _seg_dist_min(p, line):
    a, b = line[:-1], line[1:]
    d = b - a
    L2 = np.maximum((d * d).sum(1), 1e-12)
    t = np.clip(((p - a) * d).sum(1) / L2, 0, 1)
    return float(np.hypot(*(a + t[:, None] * d - p).T).min())


def signal_spot(c, u, right, wide, half, lines):
    """Where an approach's signal or sign stands at junction point c (arm direction u away from it, right: the kerb
    side): past the junction's widest arm (wide: its half width) and outside the approach's carriageway (half), moved
    off every carriageway in lines [(half width, centreline (k, 2))], bridge decks included (a signal pole stood on a
    ramp's deck where it comes down to Fallsway, Baltimore, and a car hit it, 2026-10-21); None when nowhere is clear.
    junction_props' props and navgraph's switchable heads (which stand in for them) both stand here."""
    c = np.asarray(c, np.float64)[:2]
    near = [(h, ln) for h, ln in lines if len(ln) >= 2 and np.hypot(*(ln - c).T).min() < 80.0]
    return _off_carriageways(c, u, right, wide + 2.5, half + 1.0, near)


def junction_props(roads, box, curb_h=0.15, left_hand=False, decks=(), signals=None):
    """Signals at the junctions with a mapped traffic signal (signals: mapped_signals'; None: at urban junctions of
    two or more major roads), stop signs at urban junctions
    of minor roads (the minor arms meeting a major road; the stem of a T; half the four-way ones, hashed).
    Each on the approach's right (its left where traffic drives on the left), at the junction's edge, facing
    the drivers who come in. decks: [(centreline (k, 3), half width)] of the bridge decks near (signal_spot)."""
    x0, y0, x1, y1 = box
    rows = []
    junctions = _arms(roads)
    link_ok = _link_check(junctions, signals)
    import islands
    ring = islands.ring_fids(roads)
    lines = [(spec.width / 2, np.asarray(pts, np.float64)[:, :2]) for _, spec, pts, _ in roads if len(pts) >= 2]
    lines += [(float(h), np.asarray(pts, np.float64)[:, :2]) for pts, h in decks if len(pts) >= 2]
    for k, arms in junctions.items():
        on_ring = sum(1 for a in arms if a[0] in ring) >= 2
        if len(arms) < 3 or (not on_ring and not _controlled_here(arms, signals)):
            continue
        p = arms[0][3]
        if not (x0 <= p[0] < x1 and y0 <= p[1] < y1):
            continue
        put = junction_controls(arms, k, link_ok, ring, signals)
        if not put:
            continue
        wide = max(a[1].width for a in arms) / 2
        for (fid, spec, u, c, *_), what in put:
            # the approach comes in along -u; its right is (-u_y, u_x)... rotated: right of travel (-u)
            inc = -u
            right = np.array([inc[1], -inc[0]]) * (-1 if left_hand else 1)     # the kerb side of the approach
            q = signal_spot(c, u, right, wide, spec.width / 2, lines)
            if q is None or not (x0 - 20 <= q[0] < x1 + 20 and y0 - 20 <= q[1] < y1 + 20):
                continue
            rows.append([what, round(float(q[0]), 2), round(float(q[1]), 2), round(float(c[2] + curb_h), 2),
                         round(_yaw(right[0], right[1]), 1), 1.0, 9.4 if what == G_SIGNAL else 3.0, 0.0])
    return rows


SIGN_MPH = tuple(range(15, 90, 5))  # speed-limit sign models (us_signs.lua: tools/generate_us_signs.py)
# ... and the round km/h ones (trees.lua's speedk<n>, generated by tools/generate_kmh_speed_signs.py) where limits
# are posted in km/h: every 10 km/h from 30 to 150. A limit rounds to the nearest of them and one off the set
# (20, say) gets no sign; above 150 it gets the 150.
SIGN_KMH = (30, 40, 50, 60, 70, 80, 90, 100, 110, 120, 130, 140, 150)
SIGN_NONE = "speedknone"
SIGN_CLASSES = {"tertiary", "secondary", "primary", "trunk", "unclassified", "motorway"}
SIGN_MIN_M = 150.0          # a road segment at least this long gets a sign after each of its ends
# ... and again every SIGN_REPEAT_M along it (the user, 2026-10-25: "I hardly see any signs"); a one-way carriageway
# (motorways, divided roads) only for its traffic, the first SIGN_ONEWAY_IN_M in (its pieces end at every bridge)
SIGN_REPEAT_M = 3218.688    # 2 miles
SIGN_ONEWAY_IN_M = 400.0
SIGN_IN_M = 30.0            # m past the junction
SIGN_OFF_M = 1.5            # m beyond the carriageway's edge


def speed_signs(roads, speeds, box, left_hand=False, mph_signs=True):
    """mph: the country posts mph (else the km/h models). Prop rows of speed-limit signs: on roads of SIGN_CLASSES whose
    limit (speeds: fid -> mph, the road graph's) has a sign model, SIGN_IN_M after each end of a segment of at least
    SIGN_MIN_M and every SIGN_REPEAT_M after, on the kerb side of the traffic leaving that end, facing it; a one-way
    road for its own traffic only, from SIGN_ONEWAY_IN_M."""
    x0, y0, x1, y1 = box
    rows = []
    for fid, spec, line, _ in roads:
        mph_v = speeds.get(fid)
        if mph_v is None or spec.cls not in SIGN_CLASSES or len(line) < 2:
            continue
        if not math.isfinite(mph_v) or mph_v <= 0:
            group = SIGN_NONE if not mph_signs else None
        elif mph_signs:
            n = int(round(mph_v / 5.0)) * 5
            group = f"speed{n}" if n in SIGN_MPH else None
        else:
            n = int(round(mph_v * 1.609344, 6) / 10.0 + 0.5) * 10       # half up: round() takes 45 to 40
            n = min(n, SIGN_KMH[-1])
            group = f"speedk{n}" if n in SIGN_KMH else None
        if group is None:
            continue
        L = _arclen(line)[-1]
        if L < SIGN_MIN_M:
            continue
        if spec.oneway:
            fwd = (spec.dir if spec.dir in (1, -1) else 1) > 0
            first = SIGN_ONEWAY_IN_M
            if L < first + 100.0:
                continue
            ends = ((1.0,) if fwd else (-1.0,))
        else:
            first, ends = SIGN_IN_M, (1.0, -1.0)
        at = []
        for sgn in ends:
            s = first
            while s <= L - first or s == first:
                at.append((s if sgn > 0 else L - s, sgn))
                s += SIGN_REPEAT_M
        for s0, sgn in at:
            p, d = _at(line, np.array([s0]))
            f = d[0] * sgn                                  # the travel direction leaving that end
            side = np.array([f[1], -f[0]]) * (-1 if left_hand else 1)
            q = p[0][:2] + side * (spec.width / 2 + SIGN_OFF_M)
            if x0 <= q[0] < x1 and y0 <= q[1] < y1:
                rows.append([group, round(float(q[0]), 2), round(float(q[1]), 2), round(float(p[0][2]), 2),
                             round(_yaw(side[0], side[1]), 1), 1.0, 2.6, 0.0])
    return rows


G_HYDRANT, G_BENCH, G_BIN = "hydrant", "bench", "bin"
G_STREET_TREE = "deciduous"     # trees.lua's broadleaf group (the variant nearest each tree's height)
STREET_TREE_CLASSES = {"residential", "tertiary", "secondary", "living_street"}
STREET_TREE_SHARE = 0.5         # streets with a row of trees (hashed per street)
STREET_TREE_SPACING = 11.0
STREET_TREE_CURB_M = 0.9
G_POLELAMP = "polelamp"     # a cobra-head arm on a utility pole (East Coast USA's electrical_pole_lamp)
TOWN_POLE_CLASSES = {"residential", "tertiary", "unclassified", "secondary", "living_street", "primary", "trunk"}
TOWN_POLE_SPACING = 40.0
POLE_LAMP_Z = 7.0           # m above the pole's base
HYDRANT_SPACING = 120.0
BENCH_SPACING = 140.0
BIN_SPACING = 90.0
BENCH_YAW = 90.0            # the bench model's front is its local +y (the yaw puts +x toward the road)


def _stations(run, line, spacing, phase, clear, side):
    """(point (3,), away-from-the-road unit (2,)) on a sidewalk run at the road's stations phase + k spacing
    (arc length along the road line, so every tile and anchor gets the same ones), END_CLEAR_M from the run's
    ends."""
    s_run = _project(line, run[:, :2])
    lo, hi = min(s_run[0], s_run[-1]), max(s_run[0], s_run[-1])
    k0, k1 = math.ceil((lo - phase) / spacing), math.floor((hi - phase) / spacing)
    if k1 < k0:
        return []
    p_road, _ = _at(line, phase + spacing * np.arange(k0, k1 + 1))
    s_on_run = _project(run, p_road[:, :2])
    L = _arclen(run)[-1]
    out = []
    for s in s_on_run:
        if s < clear or s > L - clear:
            continue
        p, d = _at(run, np.array([s]))
        out.append((p[0], np.array([-d[0][1], d[0][0]]) * side))   # away from the road (sidewalks.run_geometry)
    return out


DECK_HEADROOM_M = 0.5      # m a prop's top stays below a bridge deck over it
DECK_SIDE_M = 1.0          # m beside a deck's edge that still counts as under it
DECK_WIRE_M = 1.5          # m a wire stays below a deck over it


def under_decks(rows, decks, wires=None):
    """rows without the props that would reach up into a bridge deck above them (a road's lamps and poles
    under a motorway bridge stood 9 m tall through its deck: a car hit one at 75 mph on I-80, 2026-10-15).
    decks: [(points (k, 3) of the deck's centreline, half width)]. wires: removed in place where one passes
    under a deck closer than the headroom."""
    if not decks:
        return rows
    segs = []
    for pts, half in decks:
        p = np.asarray(pts, np.float64)
        if len(p) >= 2:
            segs.append((p[:-1], p[1:], half + DECK_SIDE_M))

    def deck_z(x, y):
        """The lowest deck surface over (x, y), or None."""
        best = None
        for a, b, reach in segs:
            d = b[:, :2] - a[:, :2]
            L2 = np.maximum((d * d).sum(1), 1e-12)
            t = np.clip(((np.array([x, y]) - a[:, :2]) * d).sum(1) / L2, 0, 1)
            q = a[:, :2] + t[:, None] * d
            near = np.hypot(q[:, 0] - x, q[:, 1] - y) < reach
            if near.any():
                z = float((a[:, 2] + t * (b[:, 2] - a[:, 2]))[near].min())
                best = z if best is None else min(best, z)
        return best
    out = []
    for r in rows:
        z = deck_z(r[1], r[2])
        if z is not None and r[3] < z and r[3] + r[6] > z - DECK_HEADROOM_M:
            continue
        out.append(r)
    if wires is not None:
        keep = []
        for w in wires:
            w = np.asarray(w, np.float64)
            hit = False
            for x, y, wz in w[:, :3]:
                z = deck_z(x, y)
                if z is not None and wz > z - DECK_WIRE_M:     # through the deck, or just under it
                    hit = True
                    break
            if not hit:
                keep.append(w)
        wires[:] = keep
    return out


def place(roads, walks, box, curb_h=0.15, wires=None, town=False, left_hand=False, speeds=None, mph=True, decks=(),
          signals=None):
    """Rows [group, x, y, z, yaw, scale, height, rank] (trees.lua's tile rows) of the lamps and poles of a
    tile box (x0, y0, x1, y1). roads: build_tile's (fid, spec, centreline (k, 3), lon); walks: its sidewalk
    runs (dicts with fid, pts, side, w); decks: the bridge decks over the tile [(centreline (k, 3), half
    width)]: nothing tall under them (under_decks)."""
    x0, y0, x1, y1 = box
    by_fid = {fid: (spec, pts) for fid, spec, pts, _ in roads}
    import fast
    others = fast.Others([(fid, spec.width / 2, pts) for fid, spec, pts, _ in roads])
    rows = []
    lit = set()
    for wk in walks:
        fid = wk.get("fid")
        if fid is None or "pts" not in wk or fid not in by_fid:
            continue
        if wk.get("median"):        # a divided road's median: no lamps, poles, benches or bins on it (Boston
            continue                # Street's is a bare curb, Mapillary 2026-10-17)
        spec, line = by_fid[fid]
        run = np.asarray(wk["pts"], np.float64)
        if len(run) < 2:
            continue
        side = wk["side"]
        w = wk.get("w", 2.5)

        def put(group, spacing, phase, curb_off, height, yaw_fn, clear=END_CLEAR_M):
            for p, out in _stations(run, line, spacing, phase, clear, side):
                q = p[:2] - out * (w / 2 - curb_off)
                if x0 <= q[0] < x1 and y0 <= q[1] < y1:
                    rows.append([group, round(float(q[0]), 2), round(float(q[1]), 2), round(float(p[2] + curb_h), 2),
                                 round(yaw_fn(out), 1), 1.0, height, 0.0])
        rule = LAMP_SPACING.get(spec.cls)
        if town and spec.cls in TOWN_POLE_CLASSES:
            # towns and suburbs: wooden utility poles along one side, wired, a cobra-head lamp on every other
            pole_side = 1 if _h01(fid, "side") < 0.5 else -1
            if side == pole_side:
                lit.add(fid)
                run_poles = []
                ph = _h01(fid, "tphase") * TOWN_POLE_SPACING
                for p, out in _stations(run, line, TOWN_POLE_SPACING, ph, END_CLEAR_M, side):
                    q = p[:2] - out * (w / 2 - 0.6)
                    yaw = _yaw(out[0], out[1])
                    run_poles.append((float(q[0]), float(q[1]), float(p[2]), yaw))
                    if x0 <= q[0] < x1 and y0 <= q[1] < y1:
                        rows.append([G_POLE, round(float(q[0]), 2), round(float(q[1]), 2), round(float(p[2]), 2),
                                     round(yaw, 1), 1.0, 9.2, 0.0])
                        k = int(round((float(_project(line, q[None])[0]) - ph) / TOWN_POLE_SPACING))
                        if k % 2 == 0:
                            rows.append([G_POLELAMP, round(float(q[0]), 2), round(float(q[1]), 2),
                                         round(float(p[2]) + POLE_LAMP_Z, 2), round(yaw, 1), 1.0, 1.1, 0.0])
                if wires is not None and len(run_poles) > 1:
                    wires.extend(_own_spans(run_poles, box))
            rule = None
        if rule is not None:
            spacing, both = rule
            lamp_side = 1 if _h01(fid, "side") < 0.5 else -1
            if both or side == lamp_side:
                lit.add(fid)
                phase = _h01(fid, "phase") * spacing + (spacing / 2 if (both and side < 0) else 0.0)
                put(G_LAMP, spacing, phase, LAMP_CURB_M, 9.0, lambda out: _yaw(-out[0], -out[1]))
        if spec.urban and spec.cls in STREET_TREE_CLASSES and _h01(fid, "trees") < STREET_TREE_SHARE:
            # street trees in pits along the curb (the photos' dense streets have rows of them; the canopy
            # model has few there): small broadleaves, both sides, clear of the junctions
            for p, out in _stations(run, line, STREET_TREE_SPACING, _h01(fid, side, "tph") * STREET_TREE_SPACING,
                                    END_CLEAR_M + 4.0, side):
                q = p[:2] - out * (w / 2 - STREET_TREE_CURB_M)
                if x0 <= q[0] < x1 and y0 <= q[1] < y1:
                    h = 6.5 + 3.5 * _h01(fid, round(float(q[0]), 1), round(float(q[1]), 1))
                    rows.append([G_STREET_TREE, round(float(q[0]), 2), round(float(q[1]), 2), round(float(p[2] + curb_h), 2),
                                 round(_h01(q[0], q[1], "ty") * 360.0, 1), 1.0, round(h, 1),
                                 round(_h01(fid, q[0], "tr"), 3)])
        if spec.urban:
            # fire hydrants at the curb (US practice ~90-150 m apart), alternating sides
            put(G_HYDRANT, HYDRANT_SPACING, _h01(fid, "hyd") * HYDRANT_SPACING + (HYDRANT_SPACING / 2 if side < 0 else 0),
                0.45, 0.9, lambda out: _h01(fid, "hy") * 360.0)
        if spec.urban and spec.cls in MAJOR:
            # benches at the back of the sidewalk facing the street, bins at the curb
            put(G_BENCH, BENCH_SPACING, _h01(fid, side, "bench") * BENCH_SPACING, w - 0.45, 0.9,
                lambda out: _yaw(-out[0], -out[1]) + BENCH_YAW)
            put(G_BIN, BIN_SPACING, _h01(fid, side, "bin") * BIN_SPACING, 0.7, 1.1, lambda out: _yaw(-out[0], -out[1]))
    rows.extend(junction_props(roads, box, curb_h, left_hand, decks, signals))
    if speeds:
        rows.extend(speed_signs(roads, speeds, box, left_hand, mph))
        if SIGNS2 and mph:          # (the km/h countries' warning signs are still to make)
            import roadsigns
            rows.extend(roadsigns.curve_signs(
                roads, speeds, box, left_hand, mph,
                clear=lambda x, y, z, fid: _clear(np.array([x, y, z]), others, fid)))
    for fid, spec, line, _ in roads:
        if spec.urban or fid in lit or spec.cls not in POLE_CLASSES or len(line) < 2:
            continue
        side = 1 if _h01(fid, "pside") < 0.5 else -1
        phase = _h01(fid, "pphase") * POLE_SPACING
        L = _arclen(line)[-1]
        s = phase + POLE_SPACING * np.arange(0, int((L - phase) // POLE_SPACING) + 1)
        if not len(s):
            continue
        P, D = _at(line, s)
        run = []
        for p, d in zip(P, D):
            n = np.array([-d[1], d[0]]) * side
            q = p[:2] + n * (spec.width / 2 + POLE_OFF_M)
            inside = x0 <= q[0] < x1 and y0 <= q[1] < y1
            if not _clear(np.array([q[0], q[1], p[2]]), others, fid):
                if len(run) > 1 and wires is not None:
                    wires.extend(_own_spans(run, box))
                run = []
                continue
            # the cross-arm (local x) across the road, the wires (local y) along it
            yaw = _yaw(n[0], n[1])
            run.append((float(q[0]), float(q[1]), float(p[2]), yaw))
            if inside:
                rows.append([G_POLE, round(float(q[0]), 2), round(float(q[1]), 2), round(float(p[2]), 2),
                             round(yaw, 1), 1.0, 9.2, 0.0])
        if len(run) > 1 and wires is not None:
            wires.extend(_own_spans(run, box))
    return under_decks(rows, list(decks), wires)


def _own_spans(run, box):
    """The wires of the spans of a run of poles that start in the tile (each span drawn by one tile)."""
    x0, y0, x1, y1 = box
    out = []
    for a, b in zip(run[:-1], run[1:]):
        if x0 <= a[0] < x1 and y0 <= a[1] < y1:
            out.extend(wire_lines([a, b]))
    return out
