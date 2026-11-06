"""The German junction signs: the switch (the settings page's "German junction signs", the mod's german_junctions
message, Server.dej_on, or BNE_DEJUNCTIONS=on / off to force it whatever the page says) and the glue to the Rust
kernel (native/earthfast dejunction.rs: de_stretch, de_junction): context() reads a tile's roads once (route ref
ranks, mapped priority roads and 30 zones, the Vorfahrtstraße stretches), controls() gives each arm of one junction
its sign (a code of the kernel, EXIT, or a furniture group for roundabouts and signals) and the 1002 plate,
junction_rows() stands them where VwV-StVO says (furniture.junction_props' rows); and the signs' catalogue,
designsigns.py's (re-exported here): GROUPS, their sizes (size_suffix) and shapes, HEIGHTS."""
import os
import re
from collections import namedtuple

import numpy as np

import fast
import furniture
import germany
from designsigns import (  # noqa: F401 (the signs' catalogue, re-exported: designs.GROUPS, .PLATES ...)
    ASSEMBLIES, BOXES, DEPTH_M, FACES, GAP_M, GROUPS, HEIGHTS, LOW_M, PARTS, PLATED, PLATES, POST_R_M, SHAPES, SINK_M,
    SIZE, SIZES, WIDTHS, edge, extent, panels, rounded, size_suffix)

ENV = os.environ.get("BNE_DEJUNCTIONS", "")


def enabled(server_flag):
    """Whether the German junction signs are built: BNE_DEJUNCTIONS=on / off forces it, anything else follows
    server_flag (Server.dej_on)."""
    if ENV == "on":
        return True
    if ENV == "off":
        return False
    return bool(server_flag)


KERNEL = 159            # the earthfast version with de_stretch / de_junction
CLS = {"living_street": 0, "residential": 1, "unclassified": 2, "tertiary": 3, "secondary": 4, "primary": 5,
       "trunk": 6, "motorway": 7}                       # the kernel's class codes (anything else is no street)
PRIO = {"designated": 1, "end": 2, "yes_unposted": 3}   # osm_priority_road
ZONES = ("DE:30", "DE:20", "DE:10")                     # osm_zone_maxspeed: the Tempo 30 zone family
Z205, Z206, Z306, Z301, RVL = 1, 2, 3, 4, 5             # the kernel's what codes (0: no sign)
EXIT = 6                # glue only: an arm that is no street of the junction (a driveway, a service road, a living
                        # street leaving its zone): no sign, and the AI gives way there (StVO § 10: whoever comes from
                        # such a place onto a road must take care that no one else is endangered)
ZONE30, STRETCH, OUTBOUND, RING = 1, 2, 4, 8            # the kernel's arm flags

# route ref prefixes to the kernel's ref ranks: Bundesstraße 3, Staats- / Landesstraße 2; any other letters-only
# prefix of 1-3 letters is a Kreisstraße's district code (Bavaria: M 12, PAF 3, FFB 8, ND 15, and with an umlaut: MÜ 20,
# AÖ 5, TÖL 12), 1; E is the E-road
_REF_RANKS = {"B": 3, "L": 2, "S": 2, "ST": 2, "E": 0}
_REF = re.compile(r"^\s*([^\W\d_]{1,3})\s*\d")      # (letters: any script's, umlauts too)
_BELOW_TRUNK = ("primary", "secondary", "tertiary")     # a road_routes row class where "A n" is a district code

# frame; info {fid: (ref rank, priority_road code, in a 30 zone)}; stretch {fid: on a Vorfahrtstraße stretch}; mapped
# (n, 3) the map's own signs; town {fid: in town by the land cover} (roads.road_towns: before build_tile's drawing
# overrides) or None (spec.urban)
DeContext = namedtuple("DeContext", "frame info stretch mapped town", defaults=(None,))


def ref_rank(ref, cls=None):
    """The rank of a route ref: 3 Bundesstraße (B 300), 2 Staats- / Landesstraße (St 2049, L 1140), 1 Kreisstraße (K 12,
    or a district code and number: M 12, PAF 3, ND 15), 0 anything else (E 45, none). "A n" is the Autobahn (0) unless
    the road's class (cls: the road_routes row's) is primary .. tertiary, where it is the district code A (1). Refs
    joined by ";" or "/" rank as the highest."""
    if not isinstance(ref, str):
        return 0
    best = 0
    for part in re.split(r"[;/]", ref):
        m = _REF.match(part)
        if not m:
            continue
        pre = m.group(1).upper()
        r = (1 if cls in _BELOW_TRUNK else 0) if pre == "A" else _REF_RANKS.get(pre, 1)
        best = max(best, r)
    return best


def _routes_of(routes_list):
    """{fid: highest ref rank among the segment's route memberships} of the road_routes layers."""
    import dirsigns
    best = {}
    for layer in routes_list or ():
        if layer is None or not len(layer):
            continue
        txt = layer.attrs.get("routes_txt")
        if txt is None:
            continue
        cl = layer.attrs.get("class")
        for i, (fid, t) in enumerate(zip(layer.ids, txt)):
            if t:
                c = str(cl[i]) if cl is not None else None
                r = max((ref_rank(ref, c) for _, ref, _, _ in dirsigns.parse_routes(t)), default=0)
                f = str(fid)
                best[f] = max(best.get(f, 0), r)
    return best


def _base(cls):
    """A class without its _link suffix: a ramp or slip road ranks as the road it leads to."""
    c = str(cls)
    return c[:-5] if c.endswith("_link") else c


def context(frame, feats_list, routes_list=(), priority=None, signs=(), towns=None):
    """The tile's roads read once, as a DeContext: frame; info {fid: (ref rank, priority_road code, in a 30 zone)}
    of every road segment of feats_list (roads layers, joined with the lane shards: osm_priority_road,
    osm_zone_maxspeed); stretch {fid: on a Vorfahrtstraße stretch} from the kernel's de_stretch, one call over all;
    mapped (n, 3) x, y, 1 stop / 0 give-way of the map's own signs (empty: priority and signs are for Task 7).
    routes_list: the road_routes layers of the cells (dirsigns' routes), joined by id for the ref ranks; missing ones
    leave rank 0 and the class decides. towns: {fid: in town by the land cover} (roads.road_towns), the town test's
    (in_town); None: every road's spec.urban."""
    fast.require(KERNEL, "the German junction signs (de_stretch)")
    rank = _routes_of(routes_list)
    ids, cls, rr, pr, info = [], [], [], [], {}
    for f in feats_list:
        if f is None or not len(f):
            continue
        sub = f.attrs.get("subtype")
        kind = np.asarray(sub).astype(str) if sub is not None else np.full(len(f), "road")
        cl = f.attrs.get("class")
        prio, zone = f.attrs.get("osm_priority_road"), f.attrs.get("osm_zone_maxspeed")
        for i in np.flatnonzero(kind == "road"):
            fid = str(f.ids[i])
            p = PRIO.get(prio[i], 0) if prio is not None else 0
            z = bool(zone is not None and zone[i] in ZONES)
            r = rank.get(fid, 0)
            ids.append(fid)
            cls.append(CLS.get(_base(cl[i]), 0) if cl is not None else 0)
            rr.append(r)
            pr.append(p)
            info[fid] = (r, p, z)
    st = fast._ef.de_stretch(np.array(cls, np.uint8), np.array(rr, np.uint8), np.array(pr, np.uint8)) if ids else []
    return DeContext(frame, info, {fid: bool(v) for fid, v in zip(ids, st)}, np.zeros((0, 3)),
                     None if towns is None else dict(towns))


def in_town(arms, ctx=None):
    """The German signs' town test of a junction (the kernel's urban, and where junction_rows stands a 306 or a
    307): every arm's road in town by the land cover (ctx.town: taken before roads.build_tile draws a short ring
    approach, a divided primary built up on one side or a connector rural for its own reasons), its spec.urban where
    the context has none."""
    town = ctx.town if ctx is not None and ctx.town is not None else {}
    return bool(all(town.get(a[0], a[1].urban) for a in arms))


SPLIT_DOT = 0.5         # u·u over which two one-way carriageways leave a node together: within 60 deg of each other
                        # (a split's carriageways part at a few to some 30 deg; a one-way street through a node, its
                        # two pieces opposite, is -1)


def _is_link(a):
    """Is arm a a link (a ramp or slip road): a *_link class, or Spec.link (Overture's subclass "link", whose class is
    the road's it leads to: a motorway's ramp is class motorway)?"""
    return bool(getattr(a[1], "link", False)) or str(a[1].cls).endswith("_link")


def _yielding_links(arms):
    """[bool] per arm: a link where it meets a street that is no link: it ranks below every such street there and
    gives way (an Anschlussstelle's ramp ends at a 205), and is no Vorfahrtstrasse there. Among links alone a link
    ranks as its base class."""
    link = [_is_link(a) for a in arms]
    road = any(not k and CLS.get(_base(a[1].cls)) is not None for a, k in zip(arms, link))
    return [k and road for k in link]


def _no_junction(arms):
    """Is a node of these street arms (three or more) no junction, not decided (no German signs, none of the US's):
    - the Autobahn's own carriageway is one of them (a motorway arm that is no link: its through carriageway has the
      right of way by statute, StVO § 18 Abs. 3: "Der Verkehr auf der durchgehenden Fahrbahn hat die Vorfahrt"), or
      every arm is of the motorway's class (its ramps merging or parting among themselves);
    - only one arm comes in: a diverge, nobody there must yield;
    - a carriageway split: one two-way road and its two one-way carriageways, one leaving and one coming in,
      all of one base class, the two within SPLIT_DOT of each other (a road split is no junction)."""
    motorway = [_base(a[1].cls) == "motorway" for a in arms]
    if all(motorway) or any(m and not _is_link(a) for m, a in zip(motorway, arms)):
        return True
    if sum(1 for a in arms if not furniture._outbound(a)) <= 1:
        return True
    if len(arms) != 3 or len({_base(a[1].cls) for a in arms}) != 1:
        return False
    leave = [a for a in arms if furniture._outbound(a)]
    come = [a for a in arms if _inbound(a)]
    two = [a for a in arms if not (a[1].oneway and getattr(a[1], "dir", 0))]
    if not len(leave) == len(come) == len(two) == 1:
        return False
    return float(np.dot(np.asarray(leave[0][2], np.float64)[:2], np.asarray(come[0][2], np.float64)[:2])) > SPLIT_DOT


def inside(ctx, x, y):
    """Is the junction point (frame-local x, y: the junction's, not an arm's far end) in Germany."""
    return bool(np.asarray(germany.de_at(ctx.frame, x, y)).ravel()[0])


def controls(arms, k, link_ok=None, ring=None, signals=None, country=None, priority=None, ctx=None):
    """[(arm, what, plate)] of a junction's arms (furniture's (fid, spec, unit vector away, point, at start, length)):
    - a roundabout's entries and signalled junctions: furniture.junction_controls' answer, what its group string
      (G_YIELD, G_SIGNAL), plate -1;
    - otherwise the kernel's (de_junction): what 0 none, Z205 .. RVL, plate -1 or an index into PLATES. Arms that are
      no street are not given to it but get EXIT: any class outside living_street .. motorway (links rank as their
      base class), and a living_street at a junction where another arm is none. With fewer than three street arms
      left the junction is no junction (a road split at a connector, a driveway on a road): what 0; so is a node of
      the Autobahn, a diverge and a carriageway split (_no_junction). A link that meets a road that is none gives way
      there (_yielding_links: the kernel's mapped give-way, no stretch). The kernel's urban is in_town's. A 301 where
      no arm gives way is none (VwV-StVO zu Zeichen 301 Rn. 2: the other road must have a 205 or 206).
    ctx: context()'s DeContext (the ref ranks, priorities, zones, stretches, towns); without it every road is plain by
    class and its spec.urban."""
    if (ring and sum(1 for a in arms if a[0] in ring) >= 2) or furniture._is_signalled(arms, signals):
        return [(a, what, -1) for a, what in furniture.junction_controls(
            arms, k, link_ok, ring, signals, country, priority)]
    fast.require(KERNEL, "the German junction signs (de_junction)")
    cls = [CLS.get(_base(a[1].cls)) for a in arms]
    living = [c == CLS["living_street"] for c in cls]
    only = all(living[i] for i, c in enumerate(cls) if c is not None)     # (driveways do not count: they are EXIT)
    street = [c is not None and not (living[i] and not only) for i, c in enumerate(cls)]
    out = [(a, 0 if street[i] else EXIT, -1) for i, a in enumerate(arms)]
    idx = [i for i in range(len(arms)) if street[i]]
    if len(idx) < 3 or _no_junction([arms[i] for i in idx]):
        return out
    info = ctx.info if ctx is not None else {}
    got = [info.get(arms[i][0], (0, 0, False)) for i in idx]
    c8 = np.array([cls[i] for i in idx], np.uint8)
    r8 = np.array([g[0] for g in got], np.uint8)
    st = junction_stretch(arms, ctx)
    yl = _yielding_links([arms[i] for i in idx])        # (they give way: the kernel's mapped give-way)
    flags = np.zeros(len(idx), np.uint8)
    for j, i in enumerate(idx):
        fid = arms[i][0]
        leaving = furniture._outbound(arms[i])
        flags[j] = (ZONE30 if got[j][2] else 0) | (STRETCH if st[i] and not yl[j] else 0) \
            | (OUTBOUND if leaving else 0) | (RING if ring and fid in ring else 0)
        if yl[j] and leaving:
            # (a link leaving: the kernel's mapped give-way takes out only arms coming in, so it ranks lowest instead,
            # never the priority pair's; only the rank input: it gets no sign and is in no right-before-left test)
            c8[j] = r8[j] = 0
    u = np.array([np.asarray(arms[i][2], np.float64)[:2] for i in idx], np.float64).reshape(-1, 2)
    mapped = np.array([0 if y else -1 for y in yl], np.int8)
    what, plate = fast._ef.de_junction(u, c8, r8, flags, mapped, in_town(arms, ctx))
    yields = any(int(w) in (Z205, Z206) for w in what)
    for j, i in enumerate(idx):
        out[i] = (arms[i], 0 if int(what[j]) == Z301 and not yields else int(what[j]), int(plate[j]))
    return out


def stretch_flags(arms, ctx=None):
    """[bool] per arm (furniture's): on a Vorfahrtstraße stretch, the context's or, for a road it does not know,
    de_stretch's by its class, ref rank and priority; an arm of no street class never."""
    fast.require(KERNEL, "the German junction signs (de_stretch)")
    cls = [CLS.get(_base(a[1].cls)) for a in arms]
    info = ctx.info if ctx is not None else {}
    st = ctx.stretch if ctx is not None else {}
    got = [info.get(a[0], (0, 0, False)) for a in arms]
    plain = fast._ef.de_stretch(np.array([c or 0 for c in cls], np.uint8), np.array([g[0] for g in got], np.uint8),
                                np.array([g[1] for g in got], np.uint8)) if arms else ()
    return [c is not None and bool(st.get(a[0], plain[i])) for i, (a, c) in enumerate(zip(arms, cls))]


def junction_stretch(arms, ctx=None):
    """stretch_flags of one junction's arms, the map's own priority road first (the design's B1): where two or more
    arms are on roads mapped as a Vorfahrtstraße (priority_road=designated / yes_unposted: they can be the pair), an
    arm that is a stretch by its class and ref alone and ranks no higher than the best of them (class, then ref rank,
    as the kernel ranks) is none at this junction; else the kernel, one STRETCH bit for both, would pair a straight
    class stretch of the same rank over a mapped bend. A single mapped arm (a designated road ending at a main road)
    clears nothing, nor does a mapped road crossing a higher one (an untagged Bundesstraße over a designated
    tertiary): partial tagging is likelier there than the higher road giving way."""
    st = stretch_flags(arms, ctx)
    info = ctx.info if ctx is not None else {}
    got = [info.get(a[0], (0, 0, False)) for a in arms]
    mapped = [s and g[1] in (PRIO["designated"], PRIO["yes_unposted"]) for s, g in zip(st, got)]
    if sum(mapped) < 2:
        return st
    rank = [((CLS.get(_base(a[1].cls)) or 0) << 8) | int(g[0]) for a, g in zip(arms, got)]
    top = max(r for r, m in zip(rank, mapped) if m)
    return [s and (m or r > top) for s, m, r in zip(st, mapped, rank)]


def near(frame, box):
    """Whether the frame-local box (x0, y0, x1, y1) meets germany.BOX (its corners' lon / lat bounds; no lookup of the
    border data): a road tile far from Germany builds no context and asks no junction where it is (the setting is on by
    default, the world over)."""
    x0, y0, x1, y1 = box[:4]
    lat, lon, _ = frame.enu_to_geodetic(np.array([x0, x1, x0, x1], np.float64), np.array([y0, y0, y1, y1], np.float64),
                                        0.0)
    return germany.touches(lon, lat)


def ring_exits(junctions, ring):
    """{fid of a ring way: [(arm, gives way)] of the arms of its roundabout's junctions that are not on the ring}
    (junctions: furniture._arms'; ring: islands.ring_fids'): a ring's ways are one roundabout where two of them meet at
    a junction. gives way: a link that meets a road that is none at its own junction (_yielding_links over that
    junction's arms, the ring's ways among them): no stretch going on past the ring (it gives way at its end)."""
    up = {}

    def root(f):
        up.setdefault(f, f)
        while up[f] != f:
            up[f] = up[up[f]]
            f = up[f]
        return f
    on = [arms for arms in junctions.values() if sum(1 for a in arms if a[0] in ring) >= 2]
    for arms in on:
        r = [a[0] for a in arms if a[0] in ring]
        for f in r[1:]:
            up[root(f)] = root(r[0])
    out = {}
    for arms in on:
        out.setdefault(root(next(a[0] for a in arms if a[0] in ring)), []).extend(
            (a, y) for a, y in zip(arms, _yielding_links(arms)) if a[0] not in ring)
    return {f: out.get(root(f), []) for f in up}


def _inbound(a):
    """Does traffic on arm a only come into the junction (one-way toward it: furniture._outbound's other way)?"""
    if len(a) < 5 or not a[1].oneway or not getattr(a[1], "dir", 0):
        return False
    return (a[1].dir > 0) != bool(a[4])


def _end_ways(arms, ctx):
    """Indices of the arms whose way is priority_road=end and starts here, traffic leaving on it: the drivers entering
    it at its start pass the 307 (OpenStreetMap's Key:priority_road). At a dead end none (no one enters there), nor
    where an end way arrives too (its geometry ends here, traffic coming in on it): the same way going on, split at a
    connector, its 307 behind."""
    info = ctx.info if ctx is not None else {}
    end = [len(a) > 4 and info.get(a[0], (0, 0, False))[1] == PRIO["end"] for a in arms]
    if len(arms) < 2 or any(e and not a[4] and not furniture._outbound(a) for e, a in zip(end, arms)):
        return []
    return [i for i, a in enumerate(arms) if end[i] and a[4] and not _inbound(a)]


def end_way_starts(arms, ctx):
    """Does a priority_road=end way start at this node (of two ways too: the way split at its sign)? Its 307 is
    junction_rows'."""
    return bool(_end_ways(arms, ctx))


FACE = {Z205: "z205", Z206: "z206", Z306: "z306", Z301: "z301"}     # the kernel's codes' faces (designsigns.FACES)
BACK_307_M = 10.0       # m a 307 stands farther from the junction than its own sign, on a post of its own (an
                        # assumption: the VwV gives no distance)
APART_307_M, STEP_307_M, RETRY_307 = 1.0, 2.0, 3   # a 307 spot within APART_307_M of a sign placed: STEP_307_M farther
                                                    # out, at most RETRY_307 times
BESIDE_DOT = 0.7        # u·u over which another arm leaving a roundabout runs out beside an entry: its divided road's
                        # other carriageway, the same road, not where it goes on


def junction_rows(arms, ctl, ctx, urban, left_hand, spot, ring_arms=()):
    """The German signs of one junction (or of a node where a priority_road=end way starts), as furniture's rows
    [group, x, y, z, yaw, 1.0, HEIGHTS[group], 0.0]; group de_z<code>[_p<plate>]<size_suffix of the speed limit of the
    road it stands on>. arms: furniture's; ctl: controls' [(arm, what, plate)]; urban: in_town's (as controls);
    spot(arm, group, side, yaw, back=0.0) -> (x, y, z) or None: furniture.junction_props' spot on that arm at the
    junction's edge (signal_spot's, off every carriageway; back m farther out) and the planted foot's height;
    ring_arms: [(arm, gives way)] of the arms that meet this junction's roundabout (ring_exits'). Where each stands
    (VwV-StVO):
    - 205 / 206 immediately before the junction on their arm (zu den Zeichen 205 und 206 Rn. 1), with the kernel's
      1002 plate. No advance 205 with a distance plate (Rn. 2: only where the rule would not be seen in time).
    - 301 immediately before the junction (zu Zeichen 301 Rn. 1), in town and out of town. In town a stretch's arm
      shows its 306 there instead (its last junction, which the kernel ranks: 301 not on main roads, zu Zeichen 301
      Rn. 4).
    - 306 before the junction in town, after it out of town (zu den Zeichen 306 und 307 Rn. 2): on the arm the drivers
      leave on (the priority pair's other one, or the one-way leaving straight on), facing them, if that is a stretch
      (if not: before it). After it also for the drivers who start on a stretch there (a 301 whose other arm is a
      stretch). Where the priority bends (a 1002 plate on any arm) always before it (Rn. 4), and none after it.
    - 307 out of town only (in town "entbehrlich", Rn. 8), on a post of its own BACK_307_M farther out:
      - on a stretch's arm coming in that is no 306 here, with its own 205 / 206 / 301 as decided (Rn. 8), where the
        stretch goes on nowhere. Where it goes on past a 205 / 206 (another stretch arm gives way here too, or a stretch
        one-way leaves straight on), none (Rn. 7). A stretch meeting a road straight ahead that is none is such an end.
      - where a priority_road=end way starts, after the node on that way (the drivers entering it pass the sign); then
        none before the node.
      - before a roundabout's entry on a stretch, unless another stretch leaves the roundabout (not beside the entry:
        that is its divided road's other carriageway; not a link, which gives way at its end), where the stretch
        goes on past it (Rn. 7).
    - A roundabout's entries (controls' G_YIELD, furniture.junction_controls' ring rule): a 205 without a plate at
      today's give-way spot (spec B1; no Z 215). Signals (G_SIGNAL) are furniture's rows, with no 307 here."""
    rows = []

    def put(arm, face, plate=-1, after=False, back=0.0):
        u = np.asarray(arm[2], np.float64)[:2]
        side, yaw = furniture.approach_pose(-u if after else u, face, left_hand)
        group = f"de_{face}" + (f"_p{plate}" if plate >= 0 else "") + size_suffix(getattr(arm[1], "kmh", None))
        at = spot(arm, group, side, yaw, back)
        tries = RETRY_307
        while face == "z307" and at is not None and any(np.hypot(at[0] - r[1], at[1] - r[2]) < APART_307_M
                                                        for r in rows):
            # (spot does not know the rows just placed: the junction's own sign pushed out onto the 307's spot)
            if not tries:
                at = None       # (still on another sign's post: none rather than two on one)
                break
            tries -= 1
            back += STEP_307_M
            at = spot(arm, group, side, yaw, back)
        if at is not None:
            rows.append([group, *at, yaw, 1.0, HEIGHTS[group], 0.0])

    n = len(arms)
    yl = _yielding_links(arms)
    st = [s and not y for s, y in zip(junction_stretch(arms, ctx), yl)]   # (a link gives way: no stretch)
    out = [furniture._outbound(a) for a in arms]

    def dot(a, b):
        return float(np.dot(np.asarray(a[2], np.float64)[:2], np.asarray(b[2], np.float64)[:2]))
    if any(isinstance(w, str) for _, w, _ in ctl):
        at = {id(a): i for i, a in enumerate(arms)}
        for a, w, _ in ctl:
            if w != furniture.G_YIELD:
                continue
            put(a, "z205")
            if urban or not st[at[id(a)]]:
                continue
            leave = [(x, y) for x, y in ring_arms if x[0] != a[0] and not _inbound(x)]
            going = stretch_flags([x for x, _ in leave], ctx)
            if not any(s and not y and dot(a, x) < BESIDE_DOT for (x, y), s in zip(leave, going)):
                put(a, "z307", back=BACK_307_M)
        return rows
    got = {id(a): (int(w), int(p)) for a, w, p in ctl}
    what = [got.get(id(a), (0, -1))[0] for a in arms]
    plate = [got.get(id(a), (0, -1))[1] for a in arms]
    leaves = [not _inbound(a) for a in arms]
    pair = [i for i in range(n) if what[i] in (Z306, Z301)]
    bent = any(p >= 0 for p in plate)       # (a 1002 plate on any arm: every 306 before it, Rn. 4)
    ends = _end_ways(arms, ctx)

    def other(i):
        """The arm the drivers coming in on arm i leave on through the junction: the most nearly straight on of the
        priority pair's others and the street one-ways leaving (the kernel gives those no sign), not a link that gives
        way at its end (an on-ramp leaving straight on is not the road ahead)."""
        cand = [j for j in range(n) if j != i and leaves[j] and not yl[j] and (j in pair or (out[j] and what[j] == 0))]
        return min(cand, key=lambda j: dot(arms[i], arms[j]), default=None)

    def goes_on(i):
        """Does the stretch of arm i go on past this junction: another stretch arm gives way here too (both are the
        road the 205 / 206 interrupts, Rn. 7), or a stretch one-way leaves straight on?"""
        return any(st[j] and (what[j] in (Z205, Z206) or (out[j] and dot(arms[i], arms[j]) < -0.9))
                   for j in range(n) if j != i)
    for i, a in enumerate(arms):
        w, p = what[i], plate[i]
        if w in (Z205, Z206):
            put(a, FACE[w], p)
        elif w == Z301:
            put(a, "z306" if urban and st[i] else "z301")
        elif w == Z306:
            j = other(i)
            if urban or bent or j is None or not st[j]:
                put(a, "z306", p)
        if not urban and not ends and st[i] and not out[i] and w in (Z205, Z206, Z301) and not goes_on(i):
            put(a, "z307", back=BACK_307_M)
    if urban:
        return rows
    after = []
    if not bent:
        for i in range(n):
            j = other(i) if not out[i] and what[i] in (Z306, Z301) else None
            if j is not None and st[j] and j not in after:
                after.append(j)
    for j in after:
        put(arms[j], "z306", after=True)
    for j in ends:
        put(arms[j], "z307", after=True)
    return rows
