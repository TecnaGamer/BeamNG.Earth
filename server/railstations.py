"""Railway stations from the map data (task 037): the platforms with their edge on the standard distance from the track,
the station building, the roofs over the platforms and the pedestrian links across the tracks (a footbridge, an
underpass, a crossing at the level of the rails), found from what the map says and, where it says little, worked out
from the tracks.

Two layers, as busstations.py (it keeps the adapter: busstations.items_from_layers makes the items from the Overture
cells; tools/dump_railstation_fixture.py dumps them for the tests):

    find(items)                    -> [seed group]    (the station points / halts / hall buildings, clustered)
    assemble(items, seeds, country) -> RailStation | None   (pure: the station round those seeds, None when too little is
                                                             known: RailStation.confidence)

An item is a dict {"layer", "cls", "sub", "name", "tags", "kind", "xy", ...} (busstations). Everything is in the frame's
metres and pure: no randomness, the same on every tile, anchor and BeamMP peer. The heights over the rail head come from
the rail's own profile (railways._beds) when the mesh is made, not here: a platform keeps its mapped `height` tag and the
country's norm (norm()).
"""
import math
import os

import numpy as np

import busstations as bs

ON = os.environ.get("BNE_RAILSTATIONS", "on").lower() not in ("off", "0", "false")

RAIL_CLASSES = {"standard_gauge", "narrow_gauge"}       # (a tram or light rail line has no station of this kind)
SEED_CLASSES = {"railway_station", "railway_halt"}
HALL_CLASSES = {"train_station"}                        # buildings
BUILDING_CLASSES = {"train_station", "transportation"}
ROOF_CLASSES = {"roof", "shelter", "canopy"}
LINK_CLASSES = {"footway", "pedestrian", "corridor", "path", "steps", "cycleway"}
CLUSTER_M = 450.0           # seeds closer than this are one station (a big station's halls and its halt: Kyiv's 3 points)
REACH_M = 380.0             # a station takes the platforms, roofs and links within this of its hub
PLATFORM_TRACK_M = 12.0     # a platform farther than this from every track (level 0) is not a railway one
SNAP_REACH_M = 5.5          # a platform's boundary this near a track and parallel to it is moved to the standard edge
SNAP_ANGLE_DEG = 28.0       # ... parallel: within this
RESAMPLE_M = 2.0            # the boundary's points (a curved track's edge follows it)
LINE_WIDTH_M = 4.0          # a platform mapped as a line: the strip's width on the side without a track
ISLAND_MAX_M = 14.0         # an island platform between two tracks is at most this wide
SYNTH_LEN_M = {"station": 250.0, "halt": 150.0}
SYNTH_WIDTH_M = 4.0
BUILDING_NEAR_M = 160.0
ROOF_NEAR_M = 6.0           # a roof this near a platform (or overlapping it) covers it
LINK_JOIN_M = 2.5           # ways whose ends are this near are one link
LINK_PLATFORM_M = 6.0       # a link's end this near a platform ends on it
MIN_CONFIDENCE = 4
CANOPY_LEN_M = 110.0        # a canopy worked out for a platform without a mapped roof: this long at most ...
CANOPY_SHARE = 0.6          # ... and this share of the platform's edge at most
CANOPY_MIN_RUN_M = 60.0     # a platform's edge shorter than this gets none
CANOPY_IN_M = 1.2           # the canopy stands this far in from the track edge (the safety zone stays open)
CANOPY_BACK_M = 0.8         # ... and ends this far from the platform's other edge
CANOPY_MAX_W_M = 7.5        # the canopy reaches this far from its first line at most
CANOPY_MIN_W_M = 2.5
CANOPY_STEP_M = 2.0
MAIN_STATION_PLATFORMS = 3  # a station of this many platforms or more roofs the ones without a mapped roof

# Materials (tools/make_station_textures.py, railstation.materials.json)
PAVERS = "bne_platform_pavers"          # the platform's top
TACTILE = "bne_platform_tactile"        # the yellow band along the track edge
# the canopies' steel, tinted: the map's roof colour picks the nearest (the map rarely has one: then "grey")
CANOPY_PALETTE = {"grey": (0.71, 0.72, 0.72), "white": (0.90, 0.90, 0.88), "dark": (0.30, 0.31, 0.33),
                  "blue": (0.18, 0.36, 0.66), "green": (0.20, 0.45, 0.30), "red": (0.62, 0.16, 0.14),
                  "brown": (0.42, 0.28, 0.20), "yellow": (0.85, 0.68, 0.15)}
CANOPY_DEFAULT = "grey"
ROOF_MATERIAL_LOOK = {"glass": "white", "metal": "grey", "steel": "grey", "tin": "grey", "zinc": "grey",
                      "concrete": "white", "copper": "green", "plastic": "white", "polycarbonate": "white"}


def canopy_material(look):
    return "bne_canopy_" + look


def canopy_look(colour=None, material=None):
    """The palette name nearest to a mapped colour (hex or CSS name, as buildings.parse_color reads it), else the look of
    a mapped roof material, else CANOPY_DEFAULT."""
    rgb = None
    if colour:
        import buildings
        rgb = buildings.parse_color(str(colour))
    if rgb is not None:
        return min(CANOPY_PALETTE, key=lambda k: sum((a - b) ** 2 for a, b in zip(CANOPY_PALETTE[k], rgb)))
    m = str(material or "").strip().lower()
    return ROOF_MATERIAL_LOOK.get(m, CANOPY_DEFAULT)


def _item_look(it):
    t = it.get("tags") or {}
    colour = it.get("roof_color") or t.get("roof:colour") or t.get("colour") or t.get("building:colour")
    material = it.get("roof_material") or t.get("roof:material") or t.get("material")
    return canopy_look(colour, material), bool(colour or material)

# Standard distances (m) of a platform's edge from the track's axis and its height over the rail head, by country
# (typical values of the national rules: UA PTE, CZ SŽ, DE EBO / TSI PRM). "high" is a main station, "low" a halt.
EDGE_M = {"UA": 1.92, "CZ": 1.67, "SK": 1.67, "DE": 1.675, "AT": 1.725, "PL": 1.67, "default": 1.7}
HEIGHT_M = {"UA": {"high": 1.10, "low": 0.20}, "CZ": {"high": 0.55, "low": 0.30}, "SK": {"high": 0.55, "low": 0.30},
            "DE": {"high": 0.76, "low": 0.55}, "AT": {"high": 0.55, "low": 0.30}, "PL": {"high": 0.76, "low": 0.55},
            "default": {"high": 0.55, "low": 0.30}}
HEIGHT_RANGE_M = (0.05, 1.3)


def norm(country, platforms=3):
    """(edge distance from the track's axis, height over the rail head) of a station's platforms by country; a station
    of 3 platforms or more is "high", a smaller one a halt."""
    c = (country or "").upper()
    edge = EDGE_M.get(c, EDGE_M["default"])
    h = HEIGHT_M.get(c, HEIGHT_M["default"])
    return edge, h["high"] if platforms >= 3 else h["low"]


class Platform:
    """One platform: ring (k, 2) counter-clockwise (the standard edge towards each track), snapped (k,) the points moved
    to it, tidx (k,) the track each was moved from (-1 not moved), the map's tags."""

    def __init__(self, pid, ring, tags, mapped=True):
        self.id = pid
        self.ring = ring
        self.snapped = np.zeros(len(ring), bool)
        self.tidx = np.full(len(ring), -1, np.int64)       # into self.tracks
        self.tracks = []                                   # [(id, xy)] the tracks beside it that its edges were put by
        self.tags = tags
        self.mapped = mapped            # False: worked out from the track (no platform in the data)
        self.roofs = []                 # ids of the roofs over it
        self.island = False

    def tag(self, key):
        return self.tags.get(key, "")

    @property
    def ref(self):
        for k in ("local_ref", "ref"):
            v = self.tag(k)
            if v and len(v) <= 8:
                return v
        return ""

    @property
    def height_tag(self):
        try:
            v = float(str(self.tag("height")).replace(",", ".").split()[0])
        except (ValueError, IndexError):
            return None
        return v if HEIGHT_RANGE_M[0] <= v <= HEIGHT_RANGE_M[1] else None

    @property
    def covered(self):
        return self.tag("covered") == "yes" or self.tag("shelter") == "yes" or bool(self.roofs)


class Link:
    """A pedestrian link of the station: kind "over" (a footbridge), "under" (an underpass) or "level" (a crossing at the
    rails' level); xy (k, 2) its main way, steps [(k, 2)] the stairs ends, crosses [(track index, arc length m)]."""

    def __init__(self, kind, xy, steps, level, ids, covered=False, width=0.0):
        self.kind = kind
        self.xy = xy
        self.steps = steps
        self.level = level
        self.ids = ids
        self.covered = covered
        self.width = width
        self.crosses = []


class RailStation:
    def __init__(self):
        self.name = ""
        self.hub = np.zeros(2)
        self.kind = "halt"              # "station" or "halt" (the hub's class)
        self.tracks = []                # [(id, xy)] the level-0 tracks near the platforms
        self.platforms = []
        self.buildings = []             # [(id, ring, height)] the station's hall(s)
        self.roofs = []                 # [(id, ring)] over platforms
        self.links = []
        self.confidence = 0
        self.synth = False
        self.country = None
        self.claimed = set()            # ids of buildings-layer roofs the station draws itself (tile_skip)
        self.roof_looks = {}            # roof id -> CANOPY_PALETTE name


# --- geometry --------------------------------------------------------------------------------------------------

def _ccw(ring):
    ring = bs._open(ring)
    x, y = ring[:, 0], ring[:, 1]
    return ring if float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) >= 0 else ring[::-1]


def _resample_ring(ring, step):
    ring = bs._open(ring)
    pts = []
    for a, b in zip(ring, np.roll(ring, -1, axis=0)):
        n = max(1, int(math.ceil(float(np.hypot(*(b - a))) / step)))
        pts.extend(a + (b - a) * (k / n) for k in range(n))
    return np.asarray(pts, np.float64)


def _foot(path, p):
    """(distance, foot point, unit tangent) of p on the polyline path."""
    a, b = path[:-1], path[1:]
    ab = b - a
    ab2 = np.maximum((ab ** 2).sum(1), 1e-9)
    t = np.clip(((p[0] - a[:, 0]) * ab[:, 0] + (p[1] - a[:, 1]) * ab[:, 1]) / ab2, 0.0, 1.0)
    f = a + ab * t[:, None]
    d = np.hypot(f[:, 0] - p[0], f[:, 1] - p[1])
    k = int(np.argmin(d))
    tan = ab[k] / max(float(np.hypot(*ab[k])), 1e-9)
    return float(d[k]), f[k], tan


def _nearest_track(p, tracks):
    best = (1e9, None, None, -1)
    for i, (_tid, xy) in enumerate(tracks):
        d, f, tan = _foot(xy, p)
        if d < best[0]:
            best = (d, f, tan, i)
    return best


def tracks_beside(ring, tracks, margin=SNAP_REACH_M + 2.0):
    """The tracks (id, xy) whose bounding box reaches the ring's, grown by margin: the only ones a platform's edge can be by."""
    r = np.asarray(ring, np.float64)
    lo, hi = r.min(0) - margin, r.max(0) + margin
    return [t for t in tracks if not (t[1][:, 0].max() < lo[0] or t[1][:, 0].min() > hi[0]
                                      or t[1][:, 1].max() < lo[1] or t[1][:, 1].min() > hi[1])]


class _Segments:
    """The segments of the tracks within a box (the platform's, grown), for the nearest-track question of every boundary
    point in one numpy pass: nearest(p) -> (distance, foot point, unit tangent, track index)."""

    def __init__(self, tracks, lo, hi):
        a, b, k = [], [], []
        for n, (_tid, xy) in enumerate(tracks):
            p0, p1 = xy[:-1], xy[1:]
            ok = ~((np.maximum(p0[:, 0], p1[:, 0]) < lo[0]) | (np.minimum(p0[:, 0], p1[:, 0]) > hi[0])
                   | (np.maximum(p0[:, 1], p1[:, 1]) < lo[1]) | (np.minimum(p0[:, 1], p1[:, 1]) > hi[1]))
            a.append(p0[ok])
            b.append(p1[ok])
            k.append(np.full(int(ok.sum()), n))
        self.a = np.concatenate(a) if a else np.zeros((0, 2))
        self.b = np.concatenate(b) if b else np.zeros((0, 2))
        self.k = np.concatenate(k) if k else np.zeros(0, np.int64)
        self.ab = self.b - self.a
        self.ab2 = np.maximum((self.ab ** 2).sum(1), 1e-9)

    def nearest(self, p):
        if not len(self.a):
            return 1e9, None, None, -1
        t = np.clip(((p[0] - self.a[:, 0]) * self.ab[:, 0] + (p[1] - self.a[:, 1]) * self.ab[:, 1]) / self.ab2, 0.0, 1.0)
        f = self.a + self.ab * t[:, None]
        d = np.hypot(f[:, 0] - p[0], f[:, 1] - p[1])
        j = int(np.argmin(d))
        return float(d[j]), f[j], self.ab[j] / max(float(np.hypot(*self.ab[j])), 1e-9), int(self.k[j])


def snap_edges(ring, tracks, edge_m):
    """(ring', snapped, tidx): the boundary of a platform with every point that runs beside a track (within SNAP_REACH_M,
    parallel within SNAP_ANGLE_DEG) put exactly edge_m from that track's axis, on its own side. A platform mapped to the
    rail's axis, or a metre off it, ends on the standard distance, and a platform beside a curve follows the curve."""
    pts = _resample_ring(ring, RESAMPLE_M)
    n = len(pts)
    out, snapped, tidx = pts.copy(), np.zeros(n, bool), np.full(n, -1, np.int64)
    cos_min = math.cos(math.radians(SNAP_ANGLE_DEG))
    seg = _Segments(tracks, pts.min(0) - SNAP_REACH_M - 1.0, pts.max(0) + SNAP_REACH_M + 1.0)
    for i in range(n):
        d, f, tan, k = seg.nearest(pts[i])
        if k < 0 or d > SNAP_REACH_M:
            continue
        t = pts[(i + 1) % n] - pts[i - 1]
        tl = float(np.hypot(*t))
        if tl < 1e-6 or abs(float(np.dot(t / tl, tan))) < cos_min:
            continue
        v = pts[i] - f
        side = 1.0 if (tan[0] * v[1] - tan[1] * v[0]) >= 0 else -1.0     # (left of the track's direction: +1)
        nrm = np.array([-tan[1], tan[0]]) * side
        out[i] = f + nrm * edge_m
        snapped[i], tidx[i] = True, k
    for _ in range(2):          # the corner after a run of snapped points, beside the same track, goes to its edge too
        done = snapped.copy()
        for i in range(n):
            if done[i]:
                continue
            for j in ((i - 1) % n, (i + 1) % n):
                if not done[j]:
                    continue
                d, f, tan, k = _nearest_track(pts[i], [tracks[int(tidx[j])]])
                if d > SNAP_REACH_M:
                    continue
                v = pts[i] - f
                side = 1.0 if (tan[0] * v[1] - tan[1] * v[0]) >= 0 else -1.0
                out[i] = f + np.array([-tan[1], tan[0]]) * side * edge_m
                snapped[i], tidx[i] = True, tidx[j]
                break
    keep = np.ones(n, bool)
    for i in range(n):         # (points the snap brought together are one)
        j = (i + 1) % n
        if keep[i] and float(np.hypot(*(out[i] - out[j]))) < 0.25:
            keep[j if j != 0 else i] = False
    return out[keep], snapped[keep], tidx[keep]


def _strip(line, tracks, width_tag):
    """A platform mapped as a line: its strip (polygon) on the side without a track, or centred when a track is on each
    side (an island), with the width it has (tag) or LINE_WIDTH_M."""
    line = np.asarray(line, np.float64)
    if len(line) < 2:
        return None
    mid = line[len(line) // 2]
    seg = line[min(len(line) - 1, len(line) // 2 + 1)] - line[max(0, len(line) // 2 - 1)]
    sl = float(np.hypot(*seg))
    if sl < 1e-6:
        return None
    t = seg / sl
    nrm = np.array([-t[1], t[0]])
    gaps = {1: None, -1: None}
    for side in (1, -1):         # nearest track on each side of the line, across it
        for _tid, xy in tracks:
            d, f, tan = _foot(xy, mid + nrm * side * 0.01)
            v = f - mid
            if float(np.dot(v, nrm)) * side > 0 and abs(float(np.dot(tan, t))) > 0.8 and d < ISLAND_MAX_M:
                gaps[side] = d if gaps[side] is None else min(gaps[side], d)
    w = width_tag if width_tag else LINE_WIDTH_M
    if gaps[1] is not None and gaps[-1] is not None:
        left, right = gaps[1] * 0.5, gaps[-1] * 0.5            # an island: from the line to the middle of each gap
        left, right = min(left, w), min(right, w)
        if left + right < 1.5:
            left = right = 1.5
    elif gaps[1] is not None:
        left, right = 0.0, w                                  # (the track is on the left: the strip goes right)
    elif gaps[-1] is not None:
        left, right = w, 0.0
    else:
        left, right = 0.0, w
    tn = _normals(line)
    return np.vstack((line + tn * left, (line - tn * right)[::-1]))


def _normals(line):
    t = np.zeros_like(line)
    t[1:-1] = line[2:] - line[:-2]
    t[0] = line[1] - line[0]
    t[-1] = line[-1] - line[-2]
    t /= np.maximum(np.hypot(t[:, 0], t[:, 1]), 1e-9)[:, None]
    return np.c_[-t[:, 1], t[:, 0]]


# --- what is a railway station ---------------------------------------------------------------------------------

def _num(v):
    try:
        return float(str(v).replace(",", ".").split()[0])
    except (ValueError, IndexError):
        return None


def is_train_platform(it):
    """A platform of the railway (not a tram, bus or metro one that the map calls a platform too)."""
    if it["layer"] != "infrastructure" or it["cls"] != "platform" or it["kind"] == "point":
        return False
    t = it["tags"]
    if t.get("train") == "yes":
        return True
    if t.get("subway") == "yes" or t.get("light_rail") == "yes":
        return False
    if t.get("tram") == "yes" or t.get("trolleybus") == "yes":
        return False
    if t.get("bus") == "yes" or t.get("highway") == "bus_stop":
        return False
    return t.get("railway") == "platform"


def _seed(it):
    if it["layer"] == "infrastructure" and it["cls"] in SEED_CLASSES and it["kind"] == "point":
        return True
    return it["layer"] == "buildings" and it["cls"] in HALL_CLASSES


def _centre(it):
    return bs.centroid(it["xy"])


def find(items):
    """[[seed items]] clustered: the station points and halts (and a hall building when no point is near it)."""
    seeds = [it for it in items if _seed(it)]
    points = [it for it in seeds if it["layer"] == "infrastructure"]
    halls = [it for it in seeds if it["layer"] == "buildings"
             and not any(float(np.hypot(*(_centre(it) - _centre(p)))) < CLUSTER_M for p in points)]
    seeds = sorted(points + halls, key=lambda it: it["id"])
    groups = []
    for it in seeds:
        c = _centre(it)
        for g in groups:
            if any(float(np.hypot(*(c - _centre(m)))) < CLUSTER_M for m in g):
                g.append(it)
                break
        else:
            groups.append([it])
    return groups


def _tracks_of(items):
    """[(id, xy)] of the level-0 tracks (a bridge or a tunnel is not at the platform's level), in id order."""
    out = [(it["id"], np.asarray(it["xy"], np.float64)) for it in items
           if it["layer"] == "roads" and it["sub"] == "rail" and it["cls"] in RAIL_CLASSES
           and float(it.get("level") or 0.0) == 0.0 and len(it["xy"]) >= 2]
    return sorted(out, key=lambda e: e[0])


def _near_path(xy, path, d):
    return bs.dist_to_path(path, xy) <= d


def _ring_overlap(a, b):
    """Do the polygons a and b overlap (a vertex of one inside the other, or the nearest points under 0)?"""
    if any(bs.contains(b, p) for p in bs._open(a)[::max(1, len(bs._open(a)) // 12)]):
        return True
    return any(bs.contains(a, p) for p in bs._open(b)[::max(1, len(bs._open(b)) // 12)])


def _segments_cross(a0, a1, b0, b1):
    """(t along a, u along b) where the segments cross, or None."""
    r, s = a1 - a0, b1 - b0
    den = r[0] * s[1] - r[1] * s[0]
    if abs(den) < 1e-9:
        return None
    q = b0 - a0
    t = (q[0] * s[1] - q[1] * s[0]) / den
    u = (q[0] * r[1] - q[1] * r[0]) / den
    return (t, u) if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0 else None


def _crossings(path, tracks):
    """[(track index, arc length m along path)] where the path crosses a track, in order."""
    out = []
    s0 = 0.0
    for a, b in zip(path[:-1], path[1:]):
        seglen = float(np.hypot(*(b - a)))
        for k, (_tid, xy) in enumerate(tracks):
            for c, d in zip(xy[:-1], xy[1:]):
                hit = _segments_cross(a, b, c, d)
                if hit is not None:
                    out.append((k, s0 + hit[0] * seglen))
        s0 += seglen
    return sorted(out, key=lambda e: e[1])


def _links(items, tracks, platforms, hub):
    """The pedestrian links across the tracks: a footbridge (level > 0), an underpass (level < 0) with their stairs, and the
    footways that cross the tracks at the rails' level."""
    ways = [it for it in items if it["layer"] == "roads" and it["cls"] in LINK_CLASSES and len(it["xy"]) >= 2
            and it.get("subclass") not in SKIP_SUBCLASS
            and float(np.hypot(*(np.asarray(it["xy"]).mean(0) - hub))) < REACH_M + 60.0]
    ways.sort(key=lambda it: it["id"])
    lv = [float(w.get("level") or 0.0) for w in ways]
    marked = [i for i, w in enumerate(ways) if lv[i] != 0.0 and w["cls"] != "steps" or _bridge_or_tunnel(w)]
    group = {}                                  # way index -> group number (union by touching ends)
    for i in marked:
        group[i] = len(group)
    changed = True
    while changed:
        changed = False
        for j, w in enumerate(ways):
            if j in group:
                continue
            ends = np.asarray([w["xy"][0], w["xy"][-1]])
            for i in list(group):
                o = ways[i]
                oe = np.asarray([o["xy"][0], o["xy"][-1]])
                if w["cls"] in ("steps", "footway", "corridor", "pedestrian", "path") and \
                        float(np.min(np.hypot(ends[:, None, 0] - oe[None, :, 0], ends[:, None, 1] - oe[None, :, 1]))) <= LINK_JOIN_M \
                        and (w["cls"] == "steps" or lv[j] != 0.0):
                    group[j] = group[i]
                    changed = True
                    break
    # merge the groups of ways that touch each other
    root = {i: i for i in group}

    def find_root(i):
        while root[i] != i:
            root[i] = root[root[i]]
            i = root[i]
        return i
    idx = sorted(group)
    for a in range(len(idx)):
        for b in range(a + 1, len(idx)):
            ea = np.asarray([ways[idx[a]]["xy"][0], ways[idx[a]]["xy"][-1]])
            eb = np.asarray([ways[idx[b]]["xy"][0], ways[idx[b]]["xy"][-1]])
            if float(np.min(np.hypot(ea[:, None, 0] - eb[None, :, 0], ea[:, None, 1] - eb[None, :, 1]))) <= LINK_JOIN_M:
                root[find_root(idx[a])] = find_root(idx[b])
    chains = {}
    for i in idx:
        chains.setdefault(find_root(i), []).append(i)
    links = []
    for r in sorted(chains, key=lambda r: min(ways[i]["id"] for i in chains[r])):
        members = [ways[i] for i in chains[r]]
        mains = [w for w in members if w["cls"] != "steps"]
        if not mains:
            continue
        main = max(mains, key=lambda w: float(np.hypot(*np.diff(np.asarray(w["xy"]), axis=0).T).sum()))
        path = np.asarray(main["xy"], np.float64)
        level = max((float(w.get("level") or 0.0) for w in members), key=abs, default=0.0)
        sk = span_kind(main)
        kind = "over" if (sk == "bridge" or (sk is None and level > 0)) else "under"
        crosses = _crossings(path, tracks)
        if not crosses:                          # (a way along the tracks, or a part of the chain: the longest does the crossing)
            for w in mains:
                p2 = np.asarray(w["xy"], np.float64)
                c2 = _crossings(p2, tracks)
                if c2 and len(c2) > len(crosses):
                    main, path, crosses = w, p2, c2
        if not crosses:
            continue
        ln = Link(kind, path, [np.asarray(w["xy"], np.float64) for w in members if w["cls"] == "steps"], level,
                  sorted(w["id"] for w in members),
                  covered=_covered(main), width=_num(main["tags"].get("width")) or 0.0)
        ln.crosses = crosses
        links.append(ln)
    # footways at the rails' level that cross a track and are not part of a link
    used = {i for ln in links for i in ln.ids}
    for w in ways:
        if w["id"] in used or w["cls"] not in ("footway", "path", "pedestrian", "cycleway"):
            continue
        if float(w.get("level") or 0.0) != 0.0 or _bridge_or_tunnel(w):
            continue
        path = np.asarray(w["xy"], np.float64)
        crosses = _crossings(path, tracks)
        if crosses and not _inside_any(path, platforms, crosses):
            ln = Link("level", path, [], 0.0, [w["id"]], width=_num(w["tags"].get("width")) or 0.0)
            ln.crosses = crosses
            links.append(ln)
    return links


SPAN_MIN = 0.4              # a way is a bridge / a tunnel when that is this share of it or more
SKIP_SUBCLASS = ("sidewalk", "crosswalk")      # (a road bridge's pavement is not a link of the station)


def span_kind(w):
    """"bridge", "tunnel" or None: what the way is by its spans (the map cells' level ranges), else by its tags."""
    share = {"bridge": 0.0, "tunnel": 0.0}
    for a, b, k in w.get("spans") or ():
        if k in share:
            share[k] += float(b) - float(a)
    t = w["tags"]
    for k in share:
        if t.get(k) and t.get(k) != "no":
            share[k] = max(share[k], 1.0)
    k = max(share, key=share.get)
    return k if share[k] >= SPAN_MIN else None


def _bridge_or_tunnel(w):
    return span_kind(w) is not None


def _covered(w):
    t = w["tags"]
    return t.get("covered") == "yes" or t.get("tunnel") == "building_passage" or w["cls"] == "corridor"


def _inside_any(path, platforms, crosses):
    """Is every crossing of the path on a platform (a footway along a platform's end is not a crossing)?"""
    s = np.concatenate(([0.0], np.cumsum(np.hypot(*np.diff(path, axis=0).T))))
    for _k, arc in crosses:
        p = np.array([np.interp(arc, s, path[:, 0]), np.interp(arc, s, path[:, 1])])
        if not any(bs.contains(pl.ring, p) for pl in platforms):
            return False
    return True


def _ray_hit(ring, p, d, limit=60.0):
    """Distance along the unit direction d from p to the first crossing of the polygon ring's outline, or None."""
    a = bs._open(ring)
    b = np.roll(a, -1, axis=0)
    e = b - a
    den = d[0] * e[:, 1] - d[1] * e[:, 0]
    ok = np.abs(den) > 1e-9
    q = a - p
    t = np.where(ok, (q[:, 0] * e[:, 1] - q[:, 1] * e[:, 0]) / np.where(ok, den, 1.0), -1.0)
    u = np.where(ok, (q[:, 0] * d[1] - q[:, 1] * d[0]) / np.where(ok, den, 1.0), -1.0)
    hit = ok & (t > 1e-6) & (u >= 0.0) & (u <= 1.0) & (t < limit)
    return float(t[hit].min()) if hit.any() else None


def _canopy_ring(pl, run, centre_arc, length):
    """The ring (counter-clockwise) of a canopy along the platform's track-edge run: `length` m long about `centre_arc` (arc
    length along the run), from CANOPY_IN_M in from the edge to the platform's far edge (CANOPY_BACK_M short of it), at most
    CANOPY_MAX_W_M wide; None where the platform is narrower than CANOPY_MIN_W_M there."""
    pts = pl.ring[run]
    s = np.concatenate(([0.0], np.cumsum(np.hypot(*np.diff(pts, axis=0).T))))
    lo, hi = max(0.0, centre_arc - length / 2.0), min(float(s[-1]), centre_arc + length / 2.0)
    if hi - lo < 20.0:
        return None
    ss = np.arange(lo, hi + 1e-6, CANOPY_STEP_M)
    line = np.c_[np.interp(ss, s, pts[:, 0]), np.interp(ss, s, pts[:, 1])]
    t = np.zeros_like(line)
    t[1:-1] = line[2:] - line[:-2]
    t[0], t[-1] = line[1] - line[0], line[-1] - line[-2]
    t /= np.maximum(np.hypot(t[:, 0], t[:, 1]), 1e-9)[:, None]
    nrm = np.c_[-t[:, 1], t[:, 0]]                  # (the platform is to the left of its counter-clockwise ring)
    width = []
    for p, n in zip(line, nrm):
        w = _ray_hit(pl.ring, p + n * 0.05, n)
        width.append(w if w is not None else CANOPY_MAX_W_M + CANOPY_IN_M + CANOPY_BACK_M)
    width = np.asarray(width)
    k = 5
    padded = np.concatenate((np.full(k, width[0]), width, np.full(k, width[-1])))
    width = np.array([np.median(padded[i:i + 2 * k + 1]) for i in range(len(width))])      # (a median: a spike is a ray's slip)
    far = np.minimum(width - CANOPY_BACK_M, CANOPY_IN_M + CANOPY_MAX_W_M)
    if float(np.median(far - CANOPY_IN_M)) < CANOPY_MIN_W_M:
        return None
    far = np.maximum(far, CANOPY_IN_M + CANOPY_MIN_W_M * 0.5)
    ring = np.vstack((line + nrm * CANOPY_IN_M, (line + nrm * far[:, None])[::-1]))
    return _ccw(ring)


def synth_canopies(st):
    """Platforms with no mapped roof that the map says have one (shelter=yes, covered=yes) or that belong to a main station
    (CANOPY_... platforms or more): a canopy over the stretch of the platform nearest the hall (else its middle), worked out
    from the platform's own outline. The map has the roofs of a few platforms only; the platforms of a big station are roofed."""
    main = st.kind == "station" and len(st.platforms) >= MAIN_STATION_PLATFORMS
    hall = bs.centroid(max(st.buildings, key=lambda b: bs.area(b[1]))[1]) if st.buildings else None
    for pl in st.platforms:
        if pl.roofs or not pl.mapped:
            continue
        want = pl.tag("shelter") == "yes" or pl.tag("covered") == "yes" or main
        if not want or pl.tag("covered") == "no" or pl.tag("shelter") == "no":
            continue
        runs = []
        for run in _runs_of(pl):
            pts = pl.ring[run]
            ln = float(np.hypot(*np.diff(pts, axis=0).T).sum())
            if ln >= CANOPY_MIN_RUN_M:
                runs.append((ln, run))
        if not runs:
            continue
        ln, run = max(runs, key=lambda r: r[0])
        pts = pl.ring[run]
        s = np.concatenate(([0.0], np.cumsum(np.hypot(*np.diff(pts, axis=0).T))))
        arc = ln / 2.0
        if hall is not None:
            arc = float(s[int(np.argmin(np.hypot(*(pts - hall).T)))])
        length = min(CANOPY_LEN_M, CANOPY_SHARE * ln)
        arc = min(max(arc, length / 2.0), ln - length / 2.0)
        ring = _canopy_ring(pl, run, arc, length)
        if ring is None or bs.area(ring) < 60.0:
            continue
        rid = "synth:" + str(pl.id)
        st.roofs.append((rid, ring))
        pl.roofs.append(rid)
        known = st.__dict__.get("_known_looks") or []
        st.roof_looks[rid] = known[0] if known else CANOPY_DEFAULT     # (the station's mapped roofs' colour, if any)


def _runs_of(pl):
    """[index list] the runs of the platform's track edge (railstationmesh._runs, without importing it at module level)."""
    import railstationmesh
    return railstationmesh._runs(pl.snapped, pl.ring, pl.tidx)


def hubs_of(groups):
    """[(2,)] the hub of each seed group (find's): the mean of its seeds."""
    return [np.asarray([_centre(s) for s in g]).mean(0) for g in groups]


def stations(items, country=None):
    """[RailStation] of every station in items (a group of seeds that knows too little is left out); each platform, roof
    and link belongs to the station whose hub is nearest."""
    groups = find(items)
    hubs = hubs_of(groups)
    out = []
    for k, g in enumerate(groups):
        st = assemble(items, g, country, hubs=hubs, me=k)
        if st is not None:
            out.append(st)
    return out


def assemble(items, seeds, country=None, hubs=None, me=0):
    """The station round the seed group `seeds` (find's), or None when too little is known to build one. hubs / me: all
    the hubs near (stations()) and this one's index: what is nearer to another hub is that station's."""
    st = RailStation()
    pts = np.asarray([_centre(s) for s in seeds])
    st.hub = pts.mean(0)
    st.name = next((s["name"] for s in seeds if s["name"] and s["layer"] == "infrastructure"), "") \
        or next((s["name"] for s in seeds if s["name"]), "")
    st.kind = "station" if any(s["cls"] == "railway_station" for s in seeds) else "halt"
    others = [h for k, h in enumerate(hubs or []) if k != me]

    def mine(it):
        c = _centre(it)
        d = float(np.hypot(*(c - st.hub)))
        return all(float(np.hypot(*(c - h))) >= d for h in others)
    near = [it for it in items if float(np.hypot(*(_centre(it) - st.hub))) < REACH_M + 80.0
            and (not others or it["layer"] == "roads" and it["sub"] == "rail" or mine(it))]
    all_tracks = _tracks_of(near)
    edge_m, _h = norm(country)

    # platforms: railway ones, near a level-0 track, taken by the nearest station
    plats = []
    for it in sorted((i for i in near if is_train_platform(i)), key=lambda i: i["id"]):
        c = _centre(it)
        if float(np.hypot(*(c - st.hub))) > REACH_M:
            continue
        xy = np.asarray(it["xy"], np.float64)
        local = [t for t in all_tracks if bs.dist_to_path(t[1], c) < 90.0 or min(bs.dist_to_path(t[1], p) for p in xy) < 60.0]
        if not local or min(bs.dist_to_path(t[1], p) for t in local for p in xy) > PLATFORM_TRACK_M:
            continue
        if it["kind"] == "polygon":
            ring = _ccw(it["xy"])
        else:
            ring = _strip(it["xy"], local, _num(it["tags"].get("width")))
            if ring is None:
                continue
            ring = _ccw(ring)
        if bs.area(ring) < 8.0:
            continue
        beside = tracks_beside(ring, local)
        ring2, snapped, tidx = snap_edges(ring, beside, edge_m)
        if len(ring2) < 3 or bs.area(ring2) < 8.0:
            ring2, snapped, tidx = ring, np.zeros(len(ring), bool), np.full(len(ring), -1)
        pl = Platform(it["id"], ring2, it["tags"])
        pl.snapped, pl.tidx, pl.tracks = snapped, tidx, beside
        pl.island = len({int(k) for k in tidx[snapped]}) >= 2
        plats.append(pl)
    st.platforms = plats
    used = {pl.tracks[int(k)][0] for pl in plats for k in pl.tidx[pl.snapped]}
    st.tracks = [t for t in all_tracks if bs.dist_to_path(t[1], st.hub) < REACH_M]

    # the hall(s), and the roofs over the platforms
    st.buildings = [(it["id"], bs._open(it["xy"]), float(it.get("height") or 0.0)) for it in near
                    if it["layer"] == "buildings" and it["cls"] in BUILDING_CLASSES and it["kind"] == "polygon"
                    and float(np.hypot(*(_centre(it) - st.hub))) < BUILDING_NEAR_M and bs.area(it["xy"]) > 60.0]
    for it in sorted((i for i in near if i["layer"] == "buildings" and i["cls"] in ROOF_CLASSES and i["kind"] == "polygon"),
                     key=lambda i: i["id"]):
        ring = bs._open(it["xy"])
        for pl in plats:
            if _ring_overlap(ring, pl.ring) or min(bs.dist_to_path(pl.ring, p, closed=True) for p in ring) < ROOF_NEAR_M:
                pl.roofs.append(it["id"])
                if not any(r[0] == it["id"] for r in st.roofs):
                    st.roofs.append((it["id"], ring))
                    st.claimed.add(it["id"])
                    look, known = _item_look(it)
                    st.roof_looks[it["id"]] = look
                    if known:
                        st.__dict__.setdefault("_known_looks", []).append(look)

    st.links = _links(near, st.tracks, plats, st.hub)
    synth_canopies(st)

    # worked out from the track: no platform mapped
    if not plats:
        pl = _synth(st, near, all_tracks, edge_m)
        if pl is not None:
            st.platforms, st.synth = [pl], True
            used = {pl.tracks[int(k)][0] for k in pl.tidx[pl.snapped]}

    near_track = bool(all_tracks) and min(bs.dist_to_path(t[1], st.hub) for t in all_tracks) < 60.0
    st.confidence = (3 if any(p.mapped for p in st.platforms) else 0) + (2 if near_track else 0) \
        + (2 if st.kind == "station" or len(seeds) > 1 else 1) + (1 if st.buildings else 0) + (1 if used else 0)
    return st if st.confidence >= MIN_CONFIDENCE and st.platforms else None


def _synth(st, near, tracks, edge_m):
    """A platform where the map has none: along the longest track within 60 m of the hub, on the building's side."""
    cand = [t for t in tracks if bs.dist_to_path(t[1], st.hub) < 60.0]
    if not cand:
        return None
    tid, xy = max(cand, key=lambda t: float(np.hypot(*np.diff(t[1], axis=0).T).sum()))
    d, f, tan, _k = _nearest_track(st.hub, [(tid, xy)])
    length = SYNTH_LEN_M[st.kind]
    s = np.concatenate(([0.0], np.cumsum(np.hypot(*np.diff(xy, axis=0).T))))
    arc = _arc_of(xy, s, f)
    lo, hi = max(0.0, arc - length / 2), min(float(s[-1]), arc + length / 2)
    if hi - lo < 30.0:
        return None
    ss = np.arange(lo, hi + 1e-6, RESAMPLE_M)
    cx, cy = np.interp(ss, s, xy[:, 0]), np.interp(ss, s, xy[:, 1])
    line = np.c_[cx, cy]
    nrm = _normals(line)
    if st.buildings:
        bc = bs.centroid(min(st.buildings, key=lambda b: float(np.hypot(*(bs.centroid(b[1]) - st.hub))))[1])
        side = 1.0 if float(np.dot(bc - line[len(line) // 2], nrm[len(line) // 2])) >= 0 else -1.0
    else:
        side = 1.0
    inner, outer = line + nrm * side * edge_m, line + nrm * side * (edge_m + SYNTH_WIDTH_M)
    ring = _ccw(np.vstack((inner, outer[::-1])))
    pl = Platform("synth:" + str(tid), ring, {}, mapped=False)
    # the edge towards the track is the standard one by construction
    pl.snapped = np.zeros(len(ring), bool)
    pl.tidx = np.full(len(ring), -1, np.int64)
    pl.tracks = [(tid, xy)]
    for i, p in enumerate(ring):
        if abs(bs.dist_to_path(xy, p) - edge_m) < 0.05:
            pl.snapped[i], pl.tidx[i] = True, 0
    return pl


def _arc_of(xy, s, p):
    best, arc = 1e18, 0.0
    for k in range(len(xy) - 1):
        a, b = xy[k], xy[k + 1]
        ab = b - a
        t = float(np.clip(np.dot(p - a, ab) / max(float(np.dot(ab, ab)), 1e-9), 0.0, 1.0))
        d = float(np.hypot(*(a + ab * t - p)))
        if d < best:
            best, arc = d, float(s[k]) + t * float(np.hypot(*ab))
    return arc


# --- the stations of a road tile (the map cells through busstations' adapter), kept per process ---------------------

import threading
from collections import OrderedDict

_memo = OrderedDict()
_memo_lock = threading.Lock()
MEMO_N = 12
SEED_DEG = (0.006, 0.0045)      # (lon, lat) seeds this near each other (about 450 m) are read together
GROUP_PAD_DEG = (0.0045, 0.0035)
TILE_REACH_M = 420.0            # a station this close to a tile is made in it (its pieces that fall in the tile are kept)
PAD_DEG = 0.005


def _seed_points(feats):
    """(lon, lat) of the railway_station / railway_halt features of an infrastructure layer."""
    if feats is None or not len(feats):
        return []
    cls = np.asarray(feats.attrs.get("class"))
    out = []
    for i in np.flatnonzero((cls == "railway_station") | (cls == "railway_halt")):
        parts = feats.parts(int(i))
        if parts and parts[0] and len(parts[0][0]):
            ll = np.asarray(parts[0][0], float)
            out.append((float(ll[:, 0].mean()), float(ll[:, 1].mean())))
    return out


def _group(md, frame, group):
    """[RailStation] round one group of seed points [(lon, lat)], laid out in frame; kept per process."""
    key = (round(frame.lat0, 6), round(frame.lon0, 6), tuple(group))
    with _memo_lock:
        if key in _memo:
            _memo.move_to_end(key)
            return _memo[key]
    import countrybounds
    import mapcells
    lons = np.array([g[0] for g in group])
    lats = np.array([g[1] for g in group])
    cells = mapcells.cells_for_points(lats, lons, pad_lat=GROUP_PAD_DEG[1], pad_lon=GROUP_PAD_DEG[0], max_cells=64)
    layers = {name: [md.cell(k, layers=(name,), kind="demand").layers.get(name) for k in cells]
              for name in ("infrastructure", "buildings", "roads")}
    e, n, _ = frame.geodetic_to_enu(lats, lons, np.zeros(len(lats)))
    centre = (float(np.mean(e)), float(np.mean(n)))
    items = bs.items_from_layers(frame, layers, centre, REACH_M + 90.0)
    try:
        country = countrybounds.bundled().at(float(np.mean(lats)), float(np.mean(lons)))
    except Exception:
        country = None
    found = stations(items, country)
    for st in found:
        st.country = country
    with _memo_lock:
        _memo[key] = found
        while len(_memo) > MEMO_N:
            _memo.popitem(last=False)
    return found


def stations_near(md, frame, box, shared=False):
    """The RailStations within TILE_REACH_M of the tile box (x0, y0, x1, y1) of frame, [] almost always (one pass over the
    infrastructure cells for a railway station point). Errors cost the stations, not the tile. shared: each group laid
    out in its own frame (busstations.group_frame: the same for every tile, its geometry not in frame; tile_skip)."""
    if not ON or md is None:
        return []
    import logging
    x0, y0, x1, y1 = (float(v) for v in box)
    try:
        la, lo, _ = frame.enu_to_geodetic(np.array([x0, x0, x1, x1]), np.array([y0, y1, y0, y1]), np.zeros(4))
        import mapcells
        keys = mapcells.cells_for_points(la, lo, pad_lat=PAD_DEG, pad_lon=PAD_DEG, max_cells=64)
        seeds = sorted({(round(a, 5), round(b, 5)) for k in keys
                        for a, b in _seed_points(md.cell(k, layers=("infrastructure",), kind="demand")
                                                 .layers.get("infrastructure"))})
        if not seeds:
            return []
        groups = []
        for s in seeds:
            for g in groups:
                if abs(g[0][0] - s[0]) < SEED_DEG[0] and abs(g[0][1] - s[1]) < SEED_DEG[1]:
                    g.append(s)
                    break
            else:
                groups.append([s])
        found = []
        for g in groups:
            gf = bs.group_frame(g) if shared else frame
            found += [(st, gf) for st in _group(md, gf, tuple(g))]
    except Exception as ex:
        logging.getLogger("earthd.railstations").warning("rail stations of the tile at %s: %s: %s",
                                                         tuple(round(float(v)) for v in box[:2]), type(ex).__name__, ex)
        return []
    out, seen = [], set()
    for st, gf in found:
        hub = bs.to_frame(gf, frame, [st.hub])[0]
        fid = tuple(np.round(hub, 1))
        if fid in seen:
            continue
        seen.add(fid)
        pts = bs.to_frame(gf, frame, np.vstack([np.asarray(p.ring, np.float64)[:, :2] for p in st.platforms]
                                               + [np.asarray([st.hub], np.float64)]))
        lo_, hi_ = pts.min(0), pts.max(0)
        if lo_[0] - TILE_REACH_M <= x1 and hi_[0] + TILE_REACH_M >= x0 and lo_[1] - TILE_REACH_M <= y1 and hi_[1] + TILE_REACH_M >= y0:
            out.append(st)
    return out


def tile_skip(frame, tx, ty, md):
    """Ids of the buildings-layer roofs that the railway stations near road tile (tx, ty) draw themselves (buildings.
    tile_buildings' skip, as busstations.tile_skip)."""
    if not ON:
        return frozenset()
    import roads
    return frozenset().union(*(st.claimed for st in stations_near(md, frame, roads.tile_bbox(tx, ty), shared=True)))
