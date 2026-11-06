"""The German junction signs: the switch (the settings page's "German junction signs", the mod's german_junctions
message, Server.dej_on, or BNE_DEJUNCTIONS=on / off to force it whatever the page says) and the glue to the Rust
kernel (native/earthfast dejunction.rs: de_stretch, de_junction): context() reads a tile's roads once (route ref
ranks, mapped priority roads and 30 zones, the Vorfahrtstraße stretches), controls() gives each arm of one junction
its sign (a code of the kernel, EXIT, or a furniture group for roundabouts and signals) and the 1002 plate."""
import os
import re
from collections import namedtuple

import numpy as np

import fast
import furniture
import germany

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
# the kernel's 1002 pictures in its order (PATTERNS): the other arms' slots, then the priority pair's; D the approach
# (down on the plate), L left, A ahead, R right
PLATES = ("LR:DL", "LR:DR", "LA:DL", "LA:LA", "RA:DR", "RA:RA", "LAR:DL", "LAR:DR", "LAR:LA", "LAR:AR")
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

DeContext = namedtuple("DeContext", "frame info stretch mapped")


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


def context(frame, feats_list, routes_list=(), priority=None, signs=()):
    """The tile's roads read once, as a DeContext: frame; info {fid: (ref rank, priority_road code, in a 30 zone)}
    of every road segment of feats_list (roads layers, joined with the lane shards: osm_priority_road,
    osm_zone_maxspeed); stretch {fid: on a Vorfahrtstraße stretch} from the kernel's de_stretch, one call over all;
    mapped (n, 3) x, y, 1 stop / 0 give-way of the map's own signs (empty: priority and signs are for Task 7).
    routes_list: the road_routes layers of the cells (dirsigns' routes), joined by id for the ref ranks; missing ones
    leave rank 0 and the class decides."""
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
    return DeContext(frame, info, {fid: bool(v) for fid, v in zip(ids, st)}, np.zeros((0, 3)))


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
      left the junction is no junction (a road split at a connector, a driveway on a road): what 0.
    ctx: context()'s DeContext (the ref ranks, priorities, zones, stretches); without it every road is plain by class."""
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
    if len(idx) < 3:
        return out
    info = ctx.info if ctx is not None else {}
    st = ctx.stretch if ctx is not None else {}
    got = [info.get(arms[i][0], (0, 0, False)) for i in idx]
    c8 = np.array([cls[i] for i in idx], np.uint8)
    r8 = np.array([g[0] for g in got], np.uint8)
    p8 = np.array([g[1] for g in got], np.uint8)
    plain = fast._ef.de_stretch(c8, r8, p8)       # (a road the context does not know: by its class)
    flags = np.zeros(len(idx), np.uint8)
    for j, i in enumerate(idx):
        fid = arms[i][0]
        flags[j] = (ZONE30 if got[j][2] else 0) | (STRETCH if st.get(fid, plain[j]) else 0) \
            | (OUTBOUND if furniture._outbound(arms[i]) else 0) | (RING if ring and fid in ring else 0)
    u = np.array([np.asarray(arms[i][2], np.float64)[:2] for i in idx], np.float64).reshape(-1, 2)
    urban = bool(all(a[1].urban for a in arms))
    what, plate = fast._ef.de_junction(u, c8, r8, flags, np.full(len(idx), -1, np.int8), urban)
    for j, i in enumerate(idx):
        out[i] = (arms[i], int(what[j]), int(plate[j]))
    return out
