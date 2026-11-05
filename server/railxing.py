"""Level crossings as one place (the user, 2026-11-05: on a four-track crossing in Kalynivka, Ukraine, "the signals
stand between the tracks, several together, and a big crossing has no gates"; GitHub #667).

railways.crossing_props used to take every track's crossing with a road alone: a kit before each track measured from
that track's axis, so N tracks gave N kits per approach and the inner ones stood between the tracks. Here the crossings
of one road with tracks lying within CLUSTER_M of each other along it are one crossing: a kit at each approach, before
the outermost track, at the distance the country's rules give, and the gate where the crossing is a big one.

Opt-in: BNE_RAIL_XING=1 (railways.crossing_props calls crossing_rows then). Pure given its inputs."""
import math
import os

import numpy as np

ENABLED = os.environ.get("BNE_RAIL_XING", "") not in ("", "0", "off", "false")

CLUSTER_M = 30.0        # m along a road between crossings of tracks that are one crossing (a four-track line: ~25 m)
MAX_SPAN_M = 50.0       # m a crossing spans at most (a yard's chain of tracks is cut here: every tile must agree)
MIN_SIN = 0.35          # a skew crossing's distance grows by 1 / sin, at most 1 / MIN_SIN
OSM_NODE_M = 15.0       # m between a crossing and OSM's level crossing node it takes the devices of

# ex-USSR (DSTU 4092 / Pravyla ulashtuvannia pereizdiv: a signal 6-8 m from the nearest rail), the EU (~6 m), the US and
# Canada (MUTCD: gates ~15 ft from the nearest rail); keyed by ISO alpha-2
SU = frozenset("AM AZ BY EE GE KG KZ LT LV MD RU TJ TM UA UZ".split())
AMERICAS = frozenset("US CA MX BR AR CL CO PE VE UY PY BO EC".split())
AMERICAS_LON = -30.0    # no country known: west of this the crossbuck

# m from the outermost track's axis to the kit's post, per style and kit (the nearest rail is ~0.75 m nearer)
BEFORE_M = {
    "su": {"gate": 9.0, "flash": 7.0, "cross": 4.5},
    "eu": {"gate": 7.0, "flash": 6.0, "cross": 4.5},
    "us": {"gate": 5.5, "flash": 5.5, "cross": 4.5},
}
# m from the carriageway's edge to the post
LATERAL_M = {"su": 1.5, "eu": 1.2, "us": 2.4}
# the group's suffix per style (the stock models of trees.lua: rrcross / rrflash / rrgate, "_eu" the St Andrew's cross).
# "su": the ex-USSR models of tools/blender/build_rail_xing.py once they are in the mod: "_su"
SUFFIX = {"us": "", "eu": "_eu", "su": "_eu"}
KIT_GROUP = {"cross": "rrcross", "flash": "rrflash", "gate": "rrgate"}
KIT_HEIGHT = {"cross": 3.263, "flash": 4.7, "gate": 8.25}

CLASS_KIT = {"motorway": "gate", "trunk": "gate", "primary": "gate", "secondary": "gate", "tertiary": "flash"}
CLASS_KIT_DEFAULT = "cross"
BIG_ROADS = frozenset(("motorway", "trunk", "primary", "secondary", "tertiary"))
NO_KIT = frozenset(("footway", "cycleway", "path", "pedestrian", "steps", "track", "bridleway", "corridor"))
BIG_TRACKS = 2          # a crossing of this many tracks or more has gates


def style_of(country, lon=0.0):
    """"su" / "eu" / "us" for an ISO alpha-2 country (None: by the longitude)."""
    c = str(country or "").upper()
    if c in SU:
        return "su"
    if c in AMERICAS:
        return "us"
    if not c:
        return "us" if float(lon) < AMERICAS_LON else "eu"
    return "eu"


def osm_devices(t):
    """(kit, barrier_known) of a level crossing node's OSM tags: kit "gate" / "flash" / "cross" / None (no word on its
    devices), barrier_known whether crossing:barrier says anything (yes or no)."""
    barrier, light = str(t.get("crossing:barrier") or ""), str(t.get("crossing:light") or "")
    known = bool(barrier)
    if barrier and barrier != "no":
        return "gate", True
    if light and light != "no":
        return "flash", known
    if barrier == "no" and light == "no":
        return "cross", True
    return None, known


def decide(road_cls, n_tracks, main, osm=None, fra=None):
    """The kit of one crossing: "gate" / "flash" / "cross". osm: (kit, barrier_known) of its OSM node, fra: the US
    inventory's kit (stronger than OSM), else the road class's. Mapped lights without a word on a barrier become gates
    where the crossing is a big one (BIG_TRACKS tracks, or a main line over a road of tertiary or more): the mapped
    barrier tag is missing on most of Ukraine's and the US's crossings (26 of 725 in the Vinnytsia shard)."""
    kit, known = fra, False
    if kit is None and osm is not None:
        kit, known = osm
    if kit is None:
        kit = CLASS_KIT.get(road_cls, CLASS_KIT_DEFAULT)
    if kit == "flash" and not known and (n_tracks >= BIG_TRACKS or (main and road_cls in BIG_ROADS)):
        kit = "gate"
    return kit


STRENGTH = {"cross": 0, "flash": 1, "gate": 2}


def _strongest(devs):
    """The (kit, barrier_known) of the strongest of a crossing's OSM nodes' devices (none: None). A node with no word
    (kit None) counts only for its barrier_known."""
    got = [d for d in devs if d[0] is not None]
    if not devs:
        return None
    known = any(d[1] for d in devs)
    if not got:
        return None
    return max(got, key=lambda d: STRENGTH[d[0]])[0], known


def _unit_at(line, pt):
    """The unit direction of polyline line at its segment nearest pt."""
    a, b = line[:-1], line[1:]
    d = b - a
    t = np.clip(((pt - a) * d).sum(1) / np.maximum((d * d).sum(1), 1e-12), 0.0, 1.0)
    j = int(np.argmin(np.hypot(*(a + t[:, None] * d - pt).T)))
    return d[j] / max(float(np.hypot(*d[j])), 1e-9)


def _arc_of(line, pt):
    """Arc length along polyline line to the point nearest pt."""
    a, b = line[:-1], line[1:]
    d = b - a
    seg = np.hypot(d[:, 0], d[:, 1])
    t = np.clip(((pt - a) * d).sum(1) / np.maximum(seg * seg, 1e-12), 0.0, 1.0)
    j = int(np.argmin(np.hypot(*(a + t[:, None] * d - pt).T)))
    return float(np.r_[0.0, np.cumsum(seg)][j] + t[j] * seg[j])


def clusters(line, hits):
    """Group hits (rail index, point xy) of one road's crossings with tracks into crossings: [[(arc, rail index, pt)]]
    sorted along the road, a new one where the gap passes CLUSTER_M or the span MAX_SPAN_M. Pure."""
    arcs = sorted((_arc_of(line, np.asarray(p, np.float64)), ri, np.asarray(p, np.float64)) for ri, p in hits)
    out = []
    for h in arcs:
        if out and h[0] - out[-1][-1][0] <= CLUSTER_M and h[0] - out[-1][0][0] <= MAX_SPAN_M:
            out[-1].append(h)
        else:
            out.append([h])
    return out


def _approach(line, rxy, pt, sgn, spec, kit, style, left_hand):
    """The post of the approach on the sgn side (+1: beyond pt along the road's geometry) of a crossing's end point."""
    r = _unit_at(line, pt)
    t = _unit_at(rxy, pt)
    sin = max(abs(float(r[0] * t[1] - r[1] * t[0])), MIN_SIN)
    a = -r * sgn                                        # the travel direction towards the crossing
    side = np.array([a[1], -a[0]])                      # its right
    kerb = -side if left_hand else side
    q = pt - a * (BEFORE_M[style][kit] / sin) + kerb * (spec.width / 2 + LATERAL_M[style])
    return q, side


def crossing_rows(box, rails, roads, ground, *, worn=None, usage=None, nodes=None, fra=None, country=None,
                  lon=0.0, left_hand=False, owns=None):
    """Prop rows ([group, x, y, z, yaw, scale, height, 0]) of the crossings of the roads with the tracks in a tile.
    rails: [(fid, xy, class)], roads: [(fid, spec, xy)] (both ground parts, in the tile's frame; spec: roads.Spec),
    ground(xy (n, 2)) -> z, worn: {fid: 1 disused / 2 abandoned}, usage: {fid: OSM's usage}, nodes: fn(pt) -> a level
    crossing node's OSM tags or None, fra: fn(pt, road class) -> kit or None, owns(x, y) whether the tile holds the
    point (the crossing's middle). Light rail has devices only where the data says so."""
    import roads as roadsmod
    worn, usage = worn or {}, usage or {}
    style = style_of(country, lon)
    fid_of = [f for f, _x, _c in rails]
    lines = [xy for _f, xy, _c in rails]
    rows, pending = [], []
    for (rfid, spec, line), crs in zip(roads, roadsmod._crossings_by(lines, [ln for _f, _s, ln in roads])):
        if not crs or spec.cls in NO_KIT:
            continue
        line = np.asarray(line, np.float64)
        for cl in clusters(line, [(ri, pt) for ri, _arc, pt in crs]):
            ris = {ri for _a, ri, _p in cl}
            ws = [worn.get(fid_of[ri], 0) for ri in ris]
            if min(ws) >= 2:
                continue                                # (abandoned: none)
            first, last = cl[0], cl[-1]
            mid = (first[2] + last[2]) / 2
            if owns is not None and not owns(float(mid[0]), float(mid[1])):
                continue
            main = any(usage.get(fid_of[ri]) == "main" for ri in ris)
            osm = _strongest([osm_devices(t) for t in (nodes(p) for _a, _r, p in cl) if t is not None])                 if nodes is not None else None
            kit = fra(mid, spec.cls) if fra is not None else None
            if kit is None and osm is None and all(rails[ri][2] == "light_rail" for ri in ris):
                continue                                # (a street's light rail: only where the data says it has devices)
            kit = decide(spec.cls, len(ris), main, osm, kit)
            if min(ws) >= 1:
                kit = "cross"                           # (a disused line keeps its crosses)
            group = KIT_GROUP[kit] + SUFFIX[style]
            if kit == "gate" and left_hand:
                group += "_l"
            ends = [(first, -1.0), (last, 1.0)]
            if spec.oneway:                             # (one kit: the approach the traffic comes from)
                ends = [e for e in ends if e[1] == -float(spec.dir or 1)]
            for (arc, ri, pt), sgn in ends:
                q, side = _approach(line, rails[ri][1], pt, sgn, spec, kit, style, left_hand)
                pending.append([group, q, side, KIT_HEIGHT[kit]])
    if not pending:
        return rows
    z = np.asarray(ground(np.array([p[1] for p in pending])), np.float64).ravel()
    for (group, q, side, h), zz in zip(pending, z):
        rows.append([group, round(float(q[0]), 2), round(float(q[1]), 2), round(float(zz), 2),
                     round(math.degrees(math.atan2(side[1], side[0])) % 360.0, 1), 1.0, h, 0.0])
    return rows


def node_lookup(frame, cells):
    """fn(pt (2,)) -> the tags of OSM's nearest level crossing node within OSM_NODE_M (railtags' nodes of the cells), or
    None."""
    pts = [(lo, la, t) for f in cells if f is not None for lo, la, t in f.__dict__.get("_rail_nodes", ())
           if t.get("railway") == "level_crossing"]
    if not pts:
        return lambda p: None
    a = np.array([[la, lo] for lo, la, _t in pts], np.float64)
    e, n, _ = frame.geodetic_to_enu(a[:, 0], a[:, 1], np.zeros(len(a)))
    xy = np.stack([np.asarray(e), np.asarray(n)], -1)

    def look(p):
        d = np.hypot(*(xy - np.asarray(p)[:2]).T)
        j = int(np.argmin(d))
        return pts[j][2] if d[j] <= OSM_NODE_M else None
    return look
