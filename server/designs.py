"""The German junction signs: the switch (the settings page's "German junction signs", the mod's german_junctions
message, Server.dej_on, or BNE_DEJUNCTIONS=on / off to force it whatever the page says) and the glue to the Rust
kernel (native/earthfast dejunction.rs: de_stretch, de_junction): context() reads a tile's roads once (route ref
ranks, mapped priority roads and 30 zones, the Vorfahrtstraße stretches, the map's stop and give-way signs:
mapped_rows), controls() gives each arm of one junction its sign (a code of the kernel, EXIT, or a furniture group for
roundabouts and signals) and the 1002 plate (the map's signs each on one arm: mapped_arms),
junction_rows() stands them where VwV-StVO says (furniture.junction_props' rows), decide() makes that decision once
per junction of a road tile for its signs and its paint, junction_paint() paints the junctions as their signs say
(roads.build_tile: the Wartelinie, the Haltlinie, the map's zebras); and the signs' catalogue, designsigns.py's
(re-exported here): GROUPS, their sizes (size_suffix) and shapes, HEIGHTS."""
import os
import re
from collections import namedtuple

import numpy as np

import fast
import dnp
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


KERNEL = 171            # the earthfast version with de_stretch / de_junction (171: the batch merged over dev; every tertiary a stretch)
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


_YIELDS = re.compile(r"^DE:(205|206)(?=$|[\[\-:])")    # a 205 / 206 code of the lane shards' sign nodes (with "DE:"
                                                        # always: tools/build_lane_tags.sign_code), a variant kept


def mapped_rows(frame, priority, sign_nodes):
    """(n, 3) x, y, 1 stop / 0 give-way of the map's own signs, in frame: Overture's stop and give-way points (priority:
    secrossings.nodes' rows, frame xy already; None: none read) and the lane shards' sign nodes (sign_nodes:
    railtags.sign_nodes' [(lon, lat, tags)]) with a 206 (1) or else a 205 (0) among their traffic_sign codes, matched by
    number (DE:205,1002-12: the plate after it). A 301, 306 or 307 shows the road with the right of way, not the one
    that yields: no row. Their direction keys are not used (they say forward / backward along the OSM way, which
    the arms do not carry). A node in two cells, or with the same sign both ways (two records), is one row."""
    out = [np.asarray(priority, np.float64).reshape(-1, 3)] if priority is not None else []
    found = []
    for lon, lat, tags in sign_nodes or ():
        codes = {m.group(1) for m in (_YIELDS.match(c.strip()) for c in str(tags.get("traffic_sign", "")).split(","))
                 if m}
        if codes:
            found.append((float(lon), float(lat), 1.0 if "206" in codes else 0.0))
    if found:
        ll = np.array(sorted(set(found)), np.float64)
        e, n, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.0)
        out.append(np.column_stack([np.asarray(e, np.float64).ravel(), np.asarray(n, np.float64).ravel(), ll[:, 2]]))
    return np.vstack(out) if out else np.zeros((0, 3))


MAPPED_SIDE_M = 5.0     # m beside an arm's carriageway (past its half width) a mapped sign is still that arm's: a post
                        # at the kerb, or a node set beside the way (Sweden's furniture._mapped_priority: 1.0)
MAPPED_TIE_M = 1.0      # m within which two arms are as near a mapped sign (a node's own placing is no closer): the
                        # drivers' side decides


def _on_line(line, p):
    """(arc length from line[0] of each point's nearest point on the polyline line (k, 2), its distance from it, signed
    + toward the right of a driver coming in along the line toward line[0]) of points p (n, 2)."""
    d = np.diff(line, axis=0)
    keep = dnp.hypot(d[:, 0], d[:, 1]) > 1e-9                # (a repeated vertex is no segment)
    a, d = line[:-1][keep], d[keep]
    seg = dnp.hypot(d[:, 0], d[:, 1])
    t = np.clip(((p[:, None, :] - a[None]) * d[None]).sum(-1) / (seg * seg)[None], 0.0, 1.0)
    gap = dnp.hypot(*(a[None] + t[..., None] * d[None] - p[:, None, :]).transpose(2, 0, 1))
    k = np.argmin(gap, axis=1)
    i = np.arange(len(p))
    v = p - a[k]
    side = v[:, 1] * d[k, 0] - v[:, 0] * d[k, 1]           # (as the straight arm's: (-u.y, u.x))
    return np.concatenate([[0.0], np.cumsum(seg)])[k] + t[i, k] * seg[k], np.where(side < 0.0, -1.0, 1.0) * gap[i, k]


def mapped_arms(arms, rows, lines=None):
    """The arm (an index into arms) each of the map's rows (n, 3: mapped_rows') belongs to at this junction, -1 for
    none: the arm whose centreline the row is nearest, 0 < along it <= furniture.MAPPED_ARM_M and no farther than half
    the arm's road (furniture._mapped_priority's reach: a row on a road between two junctions is the nearer end's),
    within spec.width / 2 + MAPPED_SIDE_M beside it; of the arms within MAPPED_TIE_M as near, the one on whose drivers'
    side it stands (the right: Germany drives on the right). One arm per row, never every arm: OpenStreetMap's stop and
    give-way nodes stand on the way coming in (on its centreline) with no direction the arms could use. Every arm
    counts, a driveway's or a one-way's leaving too: a row nearest one of those is no other arm's. lines: each arm's
    road (its piece's centreline from the junction out, _arm_line's) or None: along it, exactly (the two ends of a
    bowed road between two junctions are never both within half of it); None, or an arm's None: the straight line
    from the junction along the arm's direction."""
    rows = np.asarray(rows, np.float64).reshape(-1, 3)
    out = np.full(len(rows), -1, np.int64)
    if not len(rows) or not arms:
        return out
    off = np.full((len(rows), len(arms)), np.inf)
    right = np.zeros((len(rows), len(arms)), bool)
    for j, a in enumerate(arms):
        line = None if lines is None or lines[j] is None else np.asarray(lines[j], np.float64)[:, :2]
        length = float(dnp.hypot(*np.diff(line, axis=0).T).sum()) if line is not None and len(line) >= 2 else 0.0
        if length > 1e-9:
            along, side = _on_line(line, rows[:, :2])
        else:
            u = np.asarray(a[2], np.float64)[:2]
            v = rows[:, :2] - np.asarray(a[3][:2], np.float64)
            along = dnp.matmul(v, u)
            side = v[:, 1] * u[0] - v[:, 0] * u[1]      # (toward the drivers' right coming in along -u: (-u.y, u.x))
            length = a[5] if len(a) > 5 else np.inf
        reach = min(furniture.MAPPED_ARM_M, length / 2)
        hit = (along > 0.0) & (along <= reach) & (np.abs(side) <= a[1].width / 2 + MAPPED_SIDE_M)
        off[hit, j] = np.abs(side[hit])
        right[:, j] = side > 0.0
    for i in np.flatnonzero(np.isfinite(off).any(axis=1)):
        tied = np.flatnonzero(off[i] <= off[i].min() + MAPPED_TIE_M)
        mine = tied[right[i, tied]]
        out[i] = min(mine if len(mine) else tied, key=lambda j: (off[i, j], j))
    return out


def reads_here(de_on, frame, box):
    """secrossings.wanted's de for a road tile or a road graph: the German junction signs on (de_on: the
    setting, enabled's) and the frame-local box (x0, y0, x1, y1) it decides touching Germany (a road tile's:
    tile_reach; a road graph's: its window): one of its four corners or its centre inside, or a point of a GRID_M grid
    over it (on countrybounds: Germany entering the box along an edge, a bulge or a river's bend, with no corner
    and not the centre inside; GRID_M says what it can miss). Then every German junction it decides has both of the
    map's sources, Overture's stop / give-way points and crossings and the lane shards' sign nodes, not only those of a
    tile whose centre is in Germany. Off, or the box away from germany.BOX (near), the border data is not asked at
    all; otherwise the corners and the centre are one lookup and the grid, where they are all outside, one more."""
    if not de_on or not near(frame, box):
        return False
    x0, y0, x1, y1 = (float(v) for v in box[:4])
    if _de(frame, [(x0, y0), (x1, y0), (x0, y1), (x1, y1), ((x0 + x1) / 2, (y0 + y1) / 2)]).any():
        return True
    return _grid_in(frame, (x0, y0, x1, y1))


GRID_M = 25.0       # m at most between reads_here's grid points, the box's edges among its rows and columns. Germany's
                    # part of the box is found where it runs GRID_M or more along one of the box's edges (an edge's
                    # points are no farther apart) or holds a disc of GRID_M * sqrt(2) = 35 m anywhere (a grid cell's
                    # diagonal). Missed can be a sliver narrower than that inside the box, or a bend shorter than
                    # GRID_M across an edge (the border data itself is ~10 m): what reads_here gates is left out
                    # there. A road tile decides its signs by each junction's point (inside), whatever reads_here
                    # says: it loses only the map's stop / give-way points and crossings there. A road graph gates its
                    # whole German context on reads_here (navgraph.build): a German junction in a missed sliver gets
                    # today's AI there while the tiles' props stand its German signs, the one decision no longer
                    # shared. That needs a strip of Germany under ~35 m wide reaching 200 m or more into the box (the
                    # window's margin round the junctions it decides), or such a piece inside it. A speed sign tile
                    # loses its zone signs there. Cost, measured on the published border data where the grid runs
                    # (the corners and the centre all outside Germany, the box meeting germany.BOX: Salzburg,
                    # Strasbourg): a road tile's tile_reach (336 m) 15 x 15 points, 0.4-0.6 ms; a speed sign tile's box
                    # (456 m) 0.6-1.0 ms; a road graph's window (navgraph.WINDOW_M + 200 m each way: 2600 m) 105 x
                    # 105, 16-26 ms. Inside Germany a corner answers (0.03 ms); away from the box nothing is asked
                    # (0.01 ms)


def _grid_in(frame, box):
    """Does a point of a GRID_M grid over the frame-local box (x0, y0, x1, y1), its edges and corners among them, lie in
    Germany? One germany.de_at: one codes_at for the grid's points inside germany.BOX."""
    x0, y0, x1, y1 = box
    nx = max(2, int(np.ceil((x1 - x0) / GRID_M)) + 1)
    ny = max(2, int(np.ceil((y1 - y0) / GRID_M)) + 1)
    gx, gy = np.meshgrid(np.linspace(x0, x1, nx), np.linspace(y0, y1, ny))
    return bool(_de(frame, np.column_stack([gx.ravel(), gy.ravel()])).any())


def cache_parts(de_on, frame, box):
    """What a cache key of output the German junction signs change adds for them over the frame-local box (x0, y0, x1,
    y1) of what it decides (a road tile's tile_reach), after the setting's own bool(de_on): () off or with the box away
    from germany.BOX (near: the key exactly as before, the border data not asked), else (germany.signature(),): a tile
    built without the border data (offline before its first download, the German behaviour off there) has another key
    than one built with it, and is built again once they are there."""
    if not de_on or not near(frame, box):
        return ()
    return (germany.signature(),)


def tile_reach(box, skin_margin=0.0):
    """The box (x0, y0, x1, y1) a road tile decides its German junctions in (roads.build_tile's reach before its clamp
    to the roads near): its own box (roads.tile_bbox) + roads.NEAR_M, + a geotile's skin_margin."""
    import roads
    m = roads.NEAR_M + float(skin_margin)
    return box[0] - m, box[1] - m, box[2] + m, box[3] + m


def context(frame, feats_list, routes_list=(), priority=None, signs=(), towns=None):
    """The tile's roads read once, as a DeContext: frame; info {fid: (ref rank, priority_road code, in a 30 zone)}
    of every road segment of feats_list (roads layers, joined with the lane shards: osm_priority_road,
    osm_zone_maxspeed); stretch {fid: on a Vorfahrtstraße stretch} from the kernel's de_stretch, one call over all;
    mapped (n, 3) x, y, 1 stop / 0 give-way of the map's own signs (mapped_rows of priority, Overture's points:
    secrossings.nodes' rows, and signs, the lane shards' sign nodes: railtags.sign_nodes'), each one arm's at a junction
    (controls, mapped_arms). routes_list: the road_routes layers of the cells (dirsigns' routes), joined by id for the
    ref ranks; missing ones leave rank 0 and the class decides. towns: {fid: in town by the land cover}
    (roads.road_towns), the town test's (in_town); None: every road's spec.urban."""
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
    return DeContext(frame, info, {fid: bool(v) for fid, v in zip(ids, st)}, mapped_rows(frame, priority, signs),
                     None if towns is None else dict(towns))


def in_town(arms, ctx=None):
    """The German signs' town test of a junction (the kernel's urban, and where junction_rows stands a 306 or a
    307): most of its street arms' roads in town by the land cover (ctx.town: taken before roads.build_tile draws a
    short ring approach, a divided primary built up on one side or a connector rural for its own reasons), its
    spec.urban where the context has none. Most, not every: a village's main road is mapped in segments from junction
    to junction, and one with fields on one side (Barbing's Regensburger Straße past its sports ground: 38 % of it
    built up) made both its junctions rural, their 306 after the junction as out of town, among side streets all in
    town (the user's review, 2026-11-07). Arms that are no street (a driveway, a service road) do not vote; where every
    arm is none, all of them do."""
    town = ctx.town if ctx is not None and ctx.town is not None else {}
    got = [bool(town.get(a[0], a[1].urban)) for a in arms]
    vote = [t for a, t in zip(arms, got) if CLS.get(_base(a[1].cls)) is not None] or got
    return 2 * sum(vote) > len(vote)


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
    return float(dnp.dot(np.asarray(leave[0][2], np.float64)[:2], np.asarray(come[0][2], np.float64)[:2])) > SPLIT_DOT


def inside(ctx, x, y):
    """Is the junction point (frame-local x, y: the junction's, not an arm's far end) in Germany."""
    return bool(np.asarray(germany.de_at(ctx.frame, x, y)).ravel()[0])


def controls(arms, k, link_ok=None, ring=None, signals=None, country=None, priority=None, ctx=None, lines=None):
    """[(arm, what, plate)] of a junction's arms (furniture's (fid, spec, unit vector away, point, at start, length)):
    - a roundabout's entries and signalled junctions: furniture.junction_controls' answer, what its group string
      (G_YIELD, G_SIGNAL), plate -1;
    - otherwise the kernel's (de_junction): what 0 none, Z205 .. RVL, plate -1 or an index into PLATES. Arms that are
      no street are not given to it but get EXIT: any class outside living_street .. motorway (links rank as their
      base class), and a living_street at a junction where another arm is none. With fewer than three street arms
      left the junction is no junction (a road split at a connector, a driveway on a road): what 0; so is a node of
      the Autobahn, a diverge and a carriageway split (_no_junction). A link that meets a road that is none gives way
      there (_yielding_links: the kernel's mapped give-way, no stretch). The map's own stop and give-way signs
      (ctx.mapped, each on one arm: mapped_arms) are the kernel's mapped 206 / 205, over a link's give-way; at a
      node not decided they place nothing. The kernel's urban is in_town's. A 301 where no arm gives way is none
      (VwV-StVO zu Zeichen 301 Rn. 2: the other road must have a 205 or 206).
    ctx: context()'s DeContext (the ref ranks, priorities, zones, stretches, towns, the map's signs); without it every
    road is plain by class and its spec.urban, and the map's signs are priority's (secrossings.nodes' rows). lines:
    each arm's road from the junction out (_arm_line's; decide passes them), for mapped_arms; without them a row is
    measured along the arm's first direction, which on a bowed road between two junctions can put it on both (a caller
    with the arms' roads, the road graph's too, navgraph._de_decide, passes them so its answer is decide's)."""
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
    rows = ctx.mapped if ctx is not None else priority
    if rows is not None and len(rows):
        rows = np.asarray(rows, np.float64).reshape(-1, 3)
        own = mapped_arms(arms, rows, lines)            # (over every arm: a driveway's row is no street's)
        for j, i in enumerate(idx):
            if (own == i).any():
                mapped[j] = 1 if rows[own == i, 2].any() else 0     # (the map's over a link's give-way)
    what, plate = fast._ef.de_junction(u, c8, r8, flags, mapped, in_town(arms, ctx))
    yields = any(int(w) in (Z205, Z206) for w in what)
    for j, i in enumerate(idx):
        out[i] = (arms[i], 0 if int(what[j]) == Z301 and not yields else int(what[j]), int(plate[j]))
    return out


def _de(frame, pts):
    """germany.de_at of points (n, 2) at once (one codes_at for them all, not one per point)."""
    p = np.asarray(pts, np.float64).reshape(-1, 2)
    return np.asarray(germany.de_at(frame, p[:, 0], p[:, 1]), bool).ravel() if len(p) else np.zeros(0, bool)


def decide(roads, ctx, box, signals=None, country=None, priority=None):
    """{junction key: (arms, controls' answer, or None outside Germany)} of the junctions of three or more arms among
    roads (build_tile's: furniture._arms' keys and arms) whose point is in box (x0, y0, x1, y1): the one decision of a
    junction for its signs (furniture.junction_props) and its paint (junction_paint), the junction's point deciding the
    country (inside's, for all of them at once)."""
    junctions = furniture._arms(roads)
    x0, y0, x1, y1 = box[:4]
    keys = [k for k, arms in junctions.items()
            if len(arms) >= 3 and x0 <= arms[0][3][0] <= x1 and y0 <= arms[0][3][1] <= y1]
    de = _de(ctx.frame, [junctions[k][0][3][:2] for k in keys])
    if not de.any():
        return {k: (junctions[k], None) for k in keys}
    import islands
    link_ok, ring = furniture._link_check(junctions, signals), islands.ring_fids(roads)
    pieces = _pieces(roads) if ctx.mapped is not None and len(ctx.mapped) else None

    def lines(arms):
        return [_arm_line(a, pieces) for a in arms] if pieces is not None else None
    return {k: (junctions[k], controls(junctions[k], k, link_ok, ring, signals, country, priority, ctx,
                                       lines(junctions[k])) if d else None)
            for k, d in zip(keys, de)}


def _pieces(roads):
    """{fid: [centreline (k, 3)]} of roads (build_tile's: a road once per ground part)."""
    out = {}
    for fid, _, pts, _ in roads:
        out.setdefault(fid, []).append(np.asarray(pts, np.float64))
    return out


def _arm_line(a, pieces):
    """The centreline (k, 3) of arm a's road from its junction out: the piece of its road (pieces: _pieces') that ends
    at the junction."""
    pts = min(pieces[a[0]], key=lambda q: float(dnp.hypot(*(q[0 if a[4] else -1, :2] - a[3][:2]))))
    return pts if a[4] else pts[::-1]


def line_arms(decided, roads):
    """(the Wartelinie's arms, the Haltlinie's arms) of decide's junctions, as furniture.give_way_arms gives Sweden's:
    [(fid, spec, centreline from the junction (k, 3), half the junction's widest arm's width)] of the paved 205 arms and
    a roundabout's entries (controls' G_YIELD), and those of the paved 206 arms with furniture.G_STOP fifth
    (markings.wait_lines, markings.stop_lines). None on right before left, an EXIT arm or a junction not decided. The
    centreline: the piece of its road that ends at the junction (build_tile has a road once per ground part)."""
    pieces = _pieces(roads)
    wait, stop = [], []
    for arms, ctl in decided.values():
        if ctl is None:
            continue
        wide = max(a[1].width for a in arms) / 2
        for a, what, _ in ctl:
            if a[1].kind != "paved" or what not in (Z205, Z206, furniture.G_YIELD):
                continue
            arm = (a[0], a[1], _arm_line(a, pieces), wide)
            if what == Z206:
                stop.append(arm + (furniture.G_STOP,))
            else:
                wait.append(arm)
    return wait, stop


JUNCTION_PAINT_M = 50.0     # m from its junction a crosswalk or stop line of junctions.find lies at most (a corner's
                            # tangent up to 40 m out, the crosswalk and its stop line past it): the nearest junction
                            # this near is its own


def _within(points, anchors, reach):
    """[bool] of points (n, 2): within reach m of any of anchors (m, 2)."""
    p = np.asarray(points, np.float64).reshape(-1, 2)
    q = np.asarray(anchors, np.float64).reshape(-1, 2)
    if not len(p) or not len(q):
        return np.zeros(len(p), bool)
    return (dnp.hypot(p[:, None, 0] - q[None, :, 0], p[:, None, 1] - q[None, :, 1]) <= reach).any(axis=1)


ZEBRA_KMH = 50.0        # km/h a road may have at most for a Fußgängerüberweg (VwV-StVO zu § 26 Rn. 1)


def _zebra_road(road, ctx):
    """May road (fid, spec) have a Fußgängerüberweg (Z 293): in town (in_town's, the signs' town test) and its limit
    (spec.kmh: the map's max_speed_kmh, as the speed signs read it) at most ZEBRA_KMH, or unknown (in town: 50)? Out of
    town never (VwV-StVO zu § 26 Rn. 1)."""
    kmh = getattr(road[1], "kmh", None)
    return in_town([road], ctx) and (kmh is None or float(kmh) <= ZEBRA_KMH)


def junction_paint(decided, roads, ctx, crosswalks, cuts, ramps, nodes=None, signals=None, left_hand=False):
    """Germany's paint at its junctions (roads.build_tile, de_on), in place of junctions.find's US crosswalks and stop
    lines: (crosswalks and stop lines [{"a", "b", "w"[, "stop"][, "look"]}], line cuts {fid: [(point, u, t)]}, kerb
    ramps {(fid, side): [...]}, the Wartelinie's dashes [(4, 3)], the mapped zebras the road's lines stop at [(ends
    (2, 2), half width)]). decided: decide's; crosswalks, cuts, ramps: junctions.find's (crosswalks with its stop lines,
    line_cuts, ramps); nodes: secrossings.nodes' (None: no crossing mapped, no zebra); signals: the mapped traffic
    signals (furniture.mapped_signals). Outside Germany all as it was.
    - A crosswalk or stop line of junctions.find is its nearest junction's of decided within JUNCTION_PAINT_M (none
      there: its midpoint's country, and a mapped signal within furniture.SIGNAL_NEAR_M): it stays where that junction
      is outside Germany or signalised (controls' G_SIGNAL: a mapped signal within SIGNAL_NEAR_M of its widest arm's
      edge, furniture._is_signalled, as its signal heads: today's paint there), else it goes, and its line cut with it.
      The kerb ramps all stay: German streets have lowered kerbs at junctions with or without paint.
    - The map's crossings inside Germany (secrossings.place of nodes' de_crossings: Germany's reading of their tags,
      secrossings.de_paint): a zebra (Fußgängerüberweg, Z 293: bars 0.50 m wide, 0.50 m apart, RMS 4.9:
      secrossings.M15_LOOK's) where the map has a marked crossing without signals, on a road in town (in_town's, as
      the signs') whose limit is at most ZEBRA_KMH or unknown (VwV-StVO zu § 26 Rn. 1: "Fußgängerüberwege dürfen nur
      innerhalb geschlossener Ortschaften und nicht auf Straßen angelegt werden, auf denen schneller als 50 km/h
      gefahren werden darf"; unknown: the town's 50), and no signal (a mapped one, or a signalised junction's point) is
      within SIGNAL_NEAR_M (VwV-StVO zu § 26 Rn. 5: none near traffic lights; today's paint stands there), and every
      one's kerb ramps added to the junctions' (out of town or on a faster road too: the kerb is lowered there all the
      same). Sweden's M16 squares are no German marking: none.
    - The Wartelinie (Z 341) on the 205 arms and a roundabout's entries, the Haltlinie (Z 294) on the 206 arms,
      at the map's stop line where it has one (line_arms; markings.wait_lines, markings.stop_lines): the road's lines
      end at them. Nothing on right before left, an EXIT arm or a junction not decided."""
    import junctions
    import markings
    import secrossings
    vals = list(decided.values())
    at = np.array([arms[0][3][:2] for arms, _ in vals], np.float64).reshape(-1, 2)
    german = np.array([ctl is not None for _, ctl in vals], bool)
    lit = np.array([ctl is not None and any(w == furniture.G_SIGNAL for _, w, _ in ctl) for _, ctl in vals], bool)
    mapped = np.asarray(signals, np.float64).reshape(-1, 2) if signals is not None else np.zeros((0, 2))
    mids = np.array([0.5 * (np.asarray(cw["a"][:2], np.float64) + cw["b"][:2]) for cw in crosswalks]).reshape(-1, 2)
    keep = np.ones(len(mids), bool)
    by_junction = np.ones(len(mids), bool)  # (a junction of decided near: it decides)
    for i, m in enumerate(mids):
        d = dnp.hypot(at[:, 0] - m[0], at[:, 1] - m[1]) if len(at) else np.zeros(0)
        j = int(np.argmin(d)) if len(d) else -1
        if j >= 0 and d[j] <= JUNCTION_PAINT_M:
            keep[i] = not german[j] or lit[j]
        else:
            by_junction[i] = False
    if not by_junction.all():
        alone = mids[~by_junction]
        keep[~by_junction] = ~_de(ctx.frame, alone) | _within(alone, mapped, furniture.SIGNAL_NEAR_M)
    gone = np.array([m for m, k, cw in zip(mids, keep, crosswalks) if not k and not cw.get("stop")]).reshape(-1, 2)
    half = junctions.CROSSWALK["w"] / 2     # (a line cut of junctions.find is at its crosswalk's far edge)
    out_cuts = {}
    for fid, cs in cuts.items():
        got = [c for c in cs if not _within([np.asarray(c[0][:2]) + np.asarray(c[1]) * (c[2] - half)], gone, 0.5)[0]]
        if got:
            out_cuts[fid] = got
    out = [cw for cw, k in zip(crosswalks, keep) if k]
    out_ramps = {key: list(r) for key, r in ramps.items()}
    zebras = []
    if nodes is not None:
        on = []                 # (each crosswalk's road)
        found, _, mapped_ramps = secrossings.place(roads, {"crossings": nodes["de_crossings"]}, on)  # (Germany's codes)
        painted = [cw for cw, r in zip(found, on) if cw.get("paint", True) and _zebra_road(r, ctx)]
        if painted:
            pm = np.array([0.5 * (cw["a"][:2] + cw["b"][:2]) for cw in painted])
            near = _within(pm, np.vstack([mapped, at[lit]]), furniture.SIGNAL_NEAR_M)
            zebras = [dict(cw, look=secrossings.M15_LOOK) for cw, de, s in zip(painted, _de(ctx.frame, pm), near)
                      if de and not s]
        rows = [(key, r) for key, rs in mapped_ramps.items() for r in rs]
        for (key, r), de in zip(rows, _de(ctx.frame, [r[0][:2] for _, r in rows])):
            if de:
                out_ramps.setdefault(key, []).append(r)
    wait, stop = line_arms(decided, roads)
    crossings = out + zebras
    halt, halt_cuts = markings.stop_lines(stop, crossings, nodes["stop_lines"] if nodes is not None else None,
                                          left_hand, markings.Z294_W)
    dashes, wait_cuts = markings.wait_lines(wait, crossings, left_hand)
    for got in (halt_cuts, wait_cuts):
        for fid, cs in got.items():
            out_cuts.setdefault(fid, []).extend(cs)
    return (out + zebras + halt, out_cuts, out_ramps, dashes,
            [(np.array([cw["a"], cw["b"]])[:, :2], cw["w"] / 2) for cw in zebras])


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
    border data): a road tile far from Germany builds no context, asks no junction where it is and keys its cache as
    before (the setting is on by default, the world over)."""
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


def _zone_junction(arms, ctl, ctx, out):
    """Is every street arm coming in (controls' answer ctl: no EXIT; out: furniture._outbound's) in a 30 zone, as the
    kernel's zone junction: its pair 301, never 306 (StVO § 45 Abs. 1c Satz 2: the zone extends over no
    Vorfahrtstraße), so no 306, 307 or stretch of the class there either?"""
    info = ctx.info if ctx is not None else {}
    what = {id(a): w for a, w, _ in ctl}
    coming = [a for a, o in zip(arms, out) if not o and what.get(id(a)) != EXIT]
    return bool(coming) and all(info.get(a[0], (0, 0, False))[2] for a in coming)


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
    - A roundabout's entries (controls' G_YIELD, furniture.junction_controls' ring rule): Zeichen 205 over Zeichen 215
      on one post, without a plate, at today's give-way spot (StVO § 8 Abs. 1a: the ring has the right of way where
      215 stands under 205). Signals (G_SIGNAL) are furniture's rows, with no 307 here."""
    rows = []

    def put(arm, face, plate=-1, after=False, back=0.0):
        u = np.asarray(arm[2], np.float64)[:2]
        side, yaw = furniture.approach_pose(-u if after else u, face, left_hand)
        group = f"de_{face}" + (f"_p{plate}" if plate >= 0 else "") + size_suffix(getattr(arm[1], "kmh", None))
        at = spot(arm, group, side, yaw, back)
        tries = RETRY_307
        while face == "z307" and at is not None and any(dnp.hypot(at[0] - r[1], at[1] - r[2]) < APART_307_M
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
        return float(dnp.dot(np.asarray(a[2], np.float64)[:2], np.asarray(b[2], np.float64)[:2]))
    if any(isinstance(w, str) for _, w, _ in ctl):
        at = {id(a): i for i, a in enumerate(arms)}
        for a, w, _ in ctl:
            if w != furniture.G_YIELD:
                continue
            put(a, "z205_z215")
            if urban or not st[at[id(a)]]:
                continue
            leave = [(x, y) for x, y in ring_arms if x[0] != a[0] and not _inbound(x)]
            going = stretch_flags([x for x, _ in leave], ctx)
            if not any(s and not y and dot(a, x) < BESIDE_DOT for (x, y), s in zip(leave, going)):
                put(a, "z307", back=BACK_307_M)
        return rows
    got = {id(a): (int(w), int(p)) for a, w, p in ctl}
    if _zone_junction(arms, ctl, ctx, out):
        st = [False] * n        # (a 30 zone's junction: no Vorfahrtstraße there, its pair the kernel's 301)
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
