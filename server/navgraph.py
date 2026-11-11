"""The AI road graph ("navgraph") around the player, for BeamNG's own traffic and AI (docs/research/
2026-10-10-navgraph-traffic-signals.md): the mod hands it to `map.reset(customNodes)`.

Nodes are Overture connectors (named "c<connector id>": the same name in every window and anchor, and shared
by every segment meeting at a junction) and the simplified shape points between them ("s<segment id>_<k>").
Each edge carries what the custom map path does not compute (research §2.4): its half width (radius), lanes
(per direction), one-way direction, driving target (m/s, at most the posted limit) and drivability. Positions
are in the anchor frame, z the ground (elev); optional edge fields carry a portal radius, posted-speed metadata and a
country/road-class rumble-strip candidate flag. The mod clips the graph to the terrain block before using it (the only
collidable ground).

Pure given the map cells and elev; deterministic.
"""
import math
import os
import re

import numpy as np

import roads
import staging

# classes the AI drives on: drivability (path cost divisor) and a default speed limit (mph, US practice). A busway (PR
# #836) is buses only: in the graph so a car on it can drive out, at a low drivability so routes keep off it
DRIVE = {"motorway": (1.0, 65), "trunk": (1.0, 55), "primary": (1.0, 45), "secondary": (1.0, 40),
         "tertiary": (0.9, 35), "residential": (0.8, 25), "unclassified": (0.8, 30), "living_street": (0.5, 15),
         "service": (0.4, 15), "busway": (0.2, 35), "unknown": (0.6, 25),
         # tracks too (GitHub #756: an AI car following the player down one drove into the trees beside it): below the
         # stock traffic's spawn floor (trafficUtils minDrivability 0.5) and its cars' own minimum (>= 0.25), so random
         # traffic stays off them while a follow / chase / route AI can use them. Rural Europe: the box's nodes about
         # double (Czech farmland 136 -> 235, Black Forest 141 -> 288; a city block has ~700-1000)
         "track": (0.2, 15)}
# (none of them, nor ENTRY_DRIVABILITY, lies in 0.85 .. 0.9: the minimap's road colour starts at 0.85 (minimaproads.lua
# M.class), so Germany's give-way steps (DE_YIELD_STEP: a tertiary's 205 at 0.88) keep it; below 0.5 they spawn no
# random traffic, as DE_YIELD_STEP says)
SKIP_SUBCLASSES = {"driveway", "parking_aisle", "sidewalk", "crosswalk", "cycle_crossing"}
URBAN_MPH_CAP = {"primary": 35, "secondary": 35, "tertiary": 30}   # in town (spec.urban) unless mapped
# the km/h countries' rural defaults for the fast classes (Europe's 120-130 motorways: Bavaria's A9, unmapped, was
# driven at the US 65 mph, 2026-10-15)
KMH_DEFAULT = {"motorway": 120, "trunk": 100, "primary": 80, "secondary": 70}
# Sweden's limits by law where no sign says otherwise, whatever the class: 50 km/h in a built-up area, 70 outside it
# (bashastighet, Trafikförordningen 1998:1276 3 kap. 17 §), 110 on a motorway (9 kap. 1 §). Slower classes as above.
SE_ROADS = {"trunk", "primary", "secondary", "tertiary", "unclassified", "residential"}
MPH = 0.44704
LANE_W_M = 3.3
SIMPLIFY_M = 0.6            # a shape point is kept where the chord would be off by more than this
MAX_EDGE_M = 80.0           # long straight edges are split (the AI and the spawner like shorter ones)
WINDOW_M = 1100.0           # half size of the square around the player the graph covers
CURVE_ACCEL = 2.0           # m/s^2: comfortable lateral acceleration, a driving target rather than a posted limit
BRAKE_ACCEL = 2.5           # m/s^2: spread a tight bend's target back along its approaches
NARROW_M = 5.5              # narrower two-way town streets need slower encounters with oncoming traffic (parked cars,
NARROW_MS = 30.0 / 3.6      # doors); a narrow rural road keeps its limit (the Netherlands' 60 km/h country roads, ~5 m
                            # wide: the AI held 30 km/h on all of them, GitHub #451)
RING_MS = 30.0 / 3.6
# m a mountain pass road's AI half width keeps inside its edge lines (roads._mountain_roads: a wall or a parapet right at
# the edge; the AI ran its outer wheels onto the verge on the Stelvio's hairpins and hit a parapet's end, 2026-11-01)
MOUNTAIN_EDGE_M = 0.4
ENTRY_DRIVABILITY = 0.9     # stock AI yields to the ring's 1.0, even on a tangential entry
DE_YIELD_STEP = 0.02        # Germany (the German junction signs on): a 205's approach (and an exit's, StVO § 10)
                            # this much below the lowest of the arms it gives way to (research §4.4: the stock AI gives
                            # way on an incoming edge of lower drivability than another at the node; §5's 1.0 / 0.98).
                            # No floor: an approach stepped below the stock traffic's spawn floor (0.5: an exit at a
                            # living street, 0.6 -> 0.48, or a mapped 205 among living streets) spawns no random
                            # traffic but is still driven, by it and by a route / chase AI, as a track is; a floor
                            # would tie it with the road it gives way to there, undoing the give-way
DE_YIELD_PASSES = 4         # times _give_way's pass is repeated (a side road that is the next junction's priority arm:
                            # one step per junction of such a chain; a cycle of them stops here)
# A median crossover: a short service road whose two ends are both on a motorway's main carriageways (the paved U-turns
# for police and road crews across an Interstate's median, mapped as service roads, often access=no). As a service
# road (0.4) random traffic could take it: cars on I-94 near Huntley MT turned across the median and into the ditch
# (Tann2019, 2026-11-05). It takes a track's drivability instead: below the stock traffic's own minimum, still there
# for a chase or route AI. (Ramps' connectors do not count: a rest area's roads between two ramps stay service roads.)
CROSSOVER_M = 150.0


# A US motorway with no mapped limit: its state's maximum posted limit on interstates, (rural, urban) mph, cars (IIHS,
# "Maximum posted speed limits", November 2026: each state's general limit, not its "80 on specified segments", which
# OSM maps where they are). The flat 65 had Montana's I-94 driven at 65 where it is 80 (Tann2019, 2026-11-05). The state
# from the bundled Census outlines (signstates.py) at the road's middle; DC has no rural interstate (its urban 55).
US_MOTORWAY_MPH = {
    "AL": (70, 70), "AK": (65, 55), "AZ": (75, 65), "AR": (75, 65), "CA": (70, 65), "CO": (75, 65), "CT": (65, 55),
    "DE": (65, 55), "DC": (55, 55), "FL": (70, 65), "GA": (70, 70), "HI": (60, 60), "ID": (75, 75), "IL": (70, 55),
    "IN": (70, 55), "IA": (70, 65), "KS": (75, 75), "KY": (65, 65), "LA": (75, 70), "ME": (75, 75), "MD": (70, 70),
    "MA": (65, 65), "MI": (70, 70), "MN": (70, 65), "MS": (70, 70), "MO": (75, 60), "MT": (80, 65), "NE": (75, 70),
    "NV": (80, 65), "NH": (65, 65), "NJ": (65, 55), "NM": (75, 75), "NY": (65, 65), "NC": (70, 70), "ND": (80, 75),
    "OH": (70, 65), "OK": (75, 70), "OR": (65, 55), "PA": (70, 70), "RI": (65, 55), "SC": (70, 70), "SD": (80, 80),
    "TN": (70, 70), "TX": (75, 75), "UT": (75, 70), "VT": (65, 55), "VA": (70, 70), "WA": (70, 60), "WV": (70, 55),
    "WI": (70, 70), "WY": (75, 75),
}
# ... the urban one only near a city (a Natural Earth populated place of scalerank <= CITY_RANK, the world map's place
# names: maplayers.js) within CITY_KM: the road specs' `urban` (built up along the road: sidewalks, parking) took I-94
# past Huntley MT (a few hundred people) for a city's interstate, 65 where the state's limit is 80 outside its
# urbanized areas (Billings, rank 4).
# ... and the other fast classes outside towns, where no limit is mapped: the rural limit most states set for a two-lane
# highway by statute (55 mph; Texas 70 on its numbered highways, FM roads among them, 60 on the others), not the town
# speeds of DRIVE: a farm-to-market road in Texas was driven at 30-35 where it is 55-70 (GitHub #888). Not unclassified
# and below (county and gravel roads vary too much); a road built up along it (spec.urban) keeps URBAN_MPH_CAP.
US_RURAL_MPH = {"trunk": 65, "primary": 55, "secondary": 55, "tertiary": 45}
CITY_RANK = 6
CITY_KM = 12.0
_state_memo = {}
_cities = None


def _city_points():
    """(lat (k,), lon (k,)) of the populated places of scalerank <= CITY_RANK, read once (empty when missing)."""
    global _cities
    if _cities is None:
        import json
        import os
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mod", "beamng_earth", "ui",
                            "ui-vue", "mods", "beamngEarth", "maplayers.js")
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            places = json.loads(text[text.index("export default ") + len("export default "):].rstrip().rstrip(";"))
            rows = [(float(p[2]), float(p[1])) for p in places.get("places", []) if int(p[3]) <= CITY_RANK]
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            rows = []
        _cities = (np.array([r[0] for r in rows], np.float64), np.array([r[1] for r in rows], np.float64))
    return _cities


def _near_city(lat, lon):
    la, lo = _city_points()
    if not len(la):
        return False
    d = np.hypot((la - lat) * 111.32, (lo - lon) * 111.32 * math.cos(math.radians(lat)))
    return bool(d.min() <= CITY_KM)


def _us_place(feats, i):
    """(US state postal code ('' outside the states), near a city (CITY_*)) at road feature i's middle (its cx, cy).
    Memoized by place."""
    cx, cy = feats.attrs.get("cx"), feats.attrs.get("cy")
    if cx is None or cy is None:
        return "", False
    try:
        lon, lat = float(cx[i]), float(cy[i])
    except (TypeError, ValueError):
        return "", False
    if not (math.isfinite(lon) and math.isfinite(lat)):
        return "", False
    key = (round(lon, 3), round(lat, 3))
    got = _state_memo.get(key)
    if got is None:
        import signstates
        got = (str(signstates.states(lat, lon).item()), _near_city(lat, lon))
        if len(_state_memo) > 50000:
            _state_memo.clear()
        _state_memo[key] = got
    return got


def _us_state(feats, i):
    """The US state (postal code) at road feature i's middle, '' outside the states."""
    return _us_place(feats, i)[0]


def mapped_limit(feats, i, at=0.5):
    """Is road feature i's speed limit at fraction at the map's own (max_speed_kmh), not its class's default?"""
    speed = feats.rules.get("max_speed_kmh")
    if speed is None:
        return False
    for r in speed.rows(i):
        if speed.start[r] <= at <= speed.end[r]:
            v = float(speed.value[r])
            if math.isfinite(v) and v > 0:
                return True
    return False


def posted_field(legal, target, mapped):
    """An edge's posted limit field (e[9], the modding API's speedLimit: GitHub #1061): None where the edge's speed
    (e[5], the AI's target: min(legal, the curve's)) is the posted limit (most edges: the graph files stay as they
    were; always writing whether it is mapped grew every unmapped road's edges); else the limit (m/s, 0.1), negative
    when it is the road class's default, not mapped. Pure."""
    if legal is None or not math.isfinite(legal) or legal <= 0:
        return None
    if abs(legal - target) < 0.05:
        return None
    v = round(float(legal), 1)
    return v if mapped else -v


# OSM smoothness is vehicle passability, not a speed limit. Carry a rough-road flag so the traffic driver profile
# avoids its normal overspeed on these roads, while retaining the posted limit and any lower curve target.
ROUGH_SMOOTHNESS = {"bad", "very_bad", "horrible", "very_horrible", "impassable"}


def rough_road(feats, i):
    """Whether OSM marks road i rough enough that traffic should not overspeed its legal/curve target."""
    values = getattr(feats, "attrs", {}).get("osm_smoothness")
    if values is None or i < 0 or i >= len(values):
        return False
    value = values[i]
    if value is None:
        return False
    return str(value).strip().lower() in ROUGH_SMOOTHNESS


# Corridor-specific longitudinal rumble strip inventories are not available in OSM. This deliberately narrow
# country/road heuristic uses the road agency guidance documented in docs/RUMBLE_STRIPS.md.
RUMBLE_COUNTRIES = {"AU", "NZ", "US"}
RUMBLE_RURAL_CLASSES = {"trunk", "primary", "secondary"}


def rumble_eligible(country, spec):
    """Whether this carriageway gets the lane-departure rumble heuristic, not which painted lines exist."""
    if str(country or "").upper() not in RUMBLE_COUNTRIES or getattr(spec, "kind", None) != "paved":
        return False
    if getattr(spec, "link", False):
        return False
    if getattr(spec, "cls", None) == "motorway":
        return True
    return getattr(spec, "cls", None) in RUMBLE_RURAL_CLASSES and not getattr(spec, "urban", True)


def road_mph(feats, i, spec, mph=True, at=0.5, country=None, de_on=False):
    """The speed limit (mph) of road feature i at fraction at (default: its middle), else the class default
    (capped in town for arterials), or None for a class the AI does not drive. Shared by the graph and the speed-limit
    signs (speedsigns.py), so a sign shows what the traffic drives. de_on (the German junction signs, the graph's):
    a road of a German zone without a mapped limit (osm_zone_maxspeed) drives the zone's, as its 274.1 posts it."""
    if spec is None or spec.cls not in DRIVE:
        return None
    speed = feats.rules.get("max_speed_kmh")
    if speed is not None:
        for r in speed.rows(i):
            if speed.start[r] <= at <= speed.end[r]:
                value = float(speed.value[r])
                if math.isfinite(value) and value > 0:
                    return value / 1.609344
    if de_on:
        zone = feats.attrs.get("osm_zone_maxspeed")
        if zone is not None and isinstance(zone[i], str):
            from designs import ZONES
            if zone[i] in ZONES:
                return int(zone[i][3:]) / 1.609344
    if country == "US" and spec.cls == "motorway" and not getattr(spec, "link", False):     # (not a ramp)
        state, city = _us_place(feats, i)
        if state in US_MOTORWAY_MPH:
            return float(US_MOTORWAY_MPH[state][1 if spec.urban and city else 0])
    if country == "US" and not spec.urban and spec.cls in US_RURAL_MPH and not getattr(spec, "link", False):
        return float(US_RURAL_MPH[spec.cls])
    if country == "SE" and spec.cls == "motorway":
        return 110.0 / 1.609344
    if country == "SE" and spec.cls in SE_ROADS:
        return (50.0 if spec.urban else 70.0) / 1.609344
    v = DRIVE[spec.cls][1]
    if not mph and spec.cls in KMH_DEFAULT:
        v = KMH_DEFAULT[spec.cls] / 1.609344
    if spec.urban and spec.cls in URBAN_MPH_CAP:
        v = min(v, URBAN_MPH_CAP[spec.cls])
    return float(v)


def _rdp(p, tol):
    """Indices kept by Ramer-Douglas-Peucker on polyline p (k, 2)."""
    keep = np.zeros(len(p), bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(p) - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        d = p[b] - p[a]
        L = math.hypot(*d)
        seg = p[a + 1:b] - p[a]
        # Distance to the finite chord: an infinite line discarded collinear reversals in hairpins.
        t = np.clip((seg @ d) / (L * L), 0, 1) if L > 1e-9 else np.zeros(len(seg))
        dist = np.hypot(*(seg - t[:, None] * d).T)
        k = int(dist.argmax())
        if dist[k] > tol:
            keep[a + 1 + k] = True
            stack += [(a, a + 1 + k), (a + 1 + k, b)]
    return np.flatnonzero(keep)


def _speed_profile(xy, width, oneway, limit=math.inf, urban=True):
    """Advisory speeds at graph points. Both directions can brake before a bend; legal limits stay in road_mph.
    No curve is invented between separate roads, and straight wide roads keep their original target."""
    target = min(limit, NARROW_MS) if urban and not oneway and width < NARROW_M else limit
    v2 = np.full(len(xy), target * target, np.float64)
    lengths = np.hypot(*np.diff(xy, axis=0).T)
    if len(xy) > 2:
        a, b = np.diff(xy, axis=0)[:-1], np.diff(xy, axis=0)[1:]
        la, lb = lengths[:-1], lengths[1:]
        chord = np.hypot(*(a + b).T)
        cross = np.abs(a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0])
        radius = np.full(len(a), np.inf)
        bend = cross > 1e-6
        radius[bend] = la[bend] * lb[bend] * chord[bend] / (2 * cross[bend])
        reversal = (~bend) & ((a * b).sum(1) < 0)
        radius[reversal] = 0.0
        v2[1:-1] = np.minimum(v2[1:-1], np.maximum((10 / 3.6) ** 2, CURVE_ACCEL * radius))
    for j in range(1, len(xy)):
        v2[j] = min(v2[j], v2[j - 1] + 2 * BRAKE_ACCEL * lengths[j - 1])
    for j in range(len(xy) - 2, -1, -1):
        v2[j] = min(v2[j], v2[j + 1] + 2 * BRAKE_ACCEL * lengths[j])
    return np.sqrt(v2)


def driving_width(spec):
    """The width of a road's driving lanes: the drawn carriageway (spec.width) less its parking lanes and edge
    strips (roads.carriageway adds them), i.e. the class's driving width capped by the drawn one; a four-lane
    urban arterial is all lanes. Where lane lines are painted (roads.LANE_CLASSES, paved) the painted lanes'
    (roads.lane_layout): the AI in the lanes it sees (a measured 12.8 m tertiary: 8.2 m painted, 6.5 m driven; the
    user, 2026-10-28: "make sure all elements of the road are properly aligned (markers, materials, ai paths)")."""
    if spec.kind == "paved" and spec.cls in roads.LANE_CLASSES:
        return float(roads.lane_layout(spec)[0])
    if spec.urban and not spec.oneway and spec.cls in roads.URBAN_ARTERIAL:
        return float(spec.width)
    w = roads.STYLE.get(spec.cls, roads.STYLE["unknown"])[0]
    if spec.oneway and spec.cls in roads.ONEWAY_STYLE:
        w = roads.ONEWAY_STYLE[spec.cls]
    if roads.rural_motorway(spec):      # two lanes between the shoulders (a ramp: one, roads.motorway_shoulder)
        w = min(roads.MOTORWAY_RURAL_M, spec.width - 2 * roads.motorway_shoulder(spec))
    if spec.lanes:                      # OpenStreetMap's lane count ahead of the class's width
        w = spec.lanes * roads.LANE_W.get(spec.cls, roads.LANE_W_DEFAULT)
    return float(min(spec.width, w))


def _lanes(width, oneway, known=None):
    """Lanes (with the direction, against it) of a road whose driving lanes are `width` wide (known: OpenStreetMap's
    lane count, both directions, osmtags.py): the same number each way on a two-way road (a lopsided split put the AI's
    centre line off the road's)."""
    n = int(known) if known else max(1, int(round(width / LANE_W_M)))
    if oneway:
        return n, 0
    k = max(1, n // 2)
    return k, k


def _deck_z(decks, fid, x, y):
    """The height at (x, y) of feature fid's bridge parts (decks: fid -> [(xy, z, depth)]), or None when the
    point is not on one (more than 1.5 m off its line). Existing (xy, z) parts are accepted when depth is unknown."""
    best = None
    for part in decks.get(fid, ()):
        xy, z = part[:2]
        a, b = xy[:-1], xy[1:]
        d = b - a
        L2 = np.maximum((d * d).sum(1), 1e-12)
        t = np.clip(((np.array([x, y]) - a) * d).sum(1) / L2, 0, 1)
        q = a + t[:, None] * d
        dist = np.hypot(q[:, 0] - x, q[:, 1] - y)
        k = int(dist.argmin())
        if dist[k] < 1.5 and (best is None or dist[k] < best[0]):
            best = (float(dist[k]), float(z[k] + t[k] * (z[k + 1] - z[k])))
    return None if best is None else best[1]


def build(frame, feats_list, elev, centre, urban_fn=None, window=WINDOW_M, decks=None, tunnels=None, widths=None, left_hand=False,
          mph=True, signals=None, country=None, priority=None, de_on=False, de_routes=()):
    """{"nodes": {name: [x, y, z, radius]}, "edges": [[in, out, oneway, lanes_out, lanes_in, speed m/s,
    drivability, radius, (optional portal radius, posted speed, rough-road flag, rumble candidate)]], `lines` for tunnel-only minimap
    stretches as [x1, y1, x2, y2, drivability, posted speed m/s]
    (the last field was added in 1.9 for add-ons that read legal limits). Of the roads in the square of half size
    `window` around centre (x, y) of
    the anchor frame. feats_list: the "roads" mapgeom.Features of the cells around (duplicates across cells
    are taken once). elev(lat, lon) -> metres. decks: fid -> [(xy, z, structural depth)] of the bridge parts
    (roads.BridgeProfiles; existing (xy, z) entries leave the depth unknown). Nodes on a bridge take its deck height
    (else traffic would spawn on the ground under it), and roads that are bridge all along are in the graph. widths:
    measured street widths (roadwidth.py), so the AI's lanes are on the carriageway the road mesh draws. signals:
    the mapped traffic signals (furniture.mapped_signals; None: guessed), as the road tiles' props have them.
    de_on: the German junction signs (designs.enabled): where the window touches Germany (designs.reads_here) its
    junctions inside it as the road tiles decide them (_de_context: their context of these cells, the cells'
    road_routes de_routes, priority: the map's stop and give-way points, secrossings.nodes'; _signals, _give_way).
    The graph's "trafficProfile": the adaptive traffic amount's profile of its centre (_traffic_profile)."""
    cx, cy = centre
    nodes, edges, edge_fids, seen = {}, [], [], set()
    posted = {}                 # id(edge) -> its posted limit where the AI's target is not it (posted_field)
    rough_edges = set()          # id(edge) -> OSM smoothness class says traffic should not overspeed
    rumble_edges = set()         # id(edge) -> likely ATLM/rural rumble strip corridor
    carriage, carriage_ll = [], []     # sampled ground carriageways, with heights batched below
    signal_parts, signal_walks, signal_depths = [], {}, {}
    copies = roads.copies_of(feats_list)
    placement = roads.placement_context(feats_list, urban_fn, widths, left_hand)    # (as the road tiles draw them)
    prop_arms = {}          # connector name -> [(fid, spec, unit vector away, point, end, length)] (the props' view)
    pieces = {}             # fid -> [ground part (k, 2)]: the road tiles' roads (designs._arm_line: an arm's own road)
    ring_cands = []         # the roads the road tiles draw (fid, spec, pts, None): roundabout rings (islands.ring_fids)
    approaches = {}         # (connector, fid, end) -> (graph node next to it, traffic comes in)
    pending = {}            # name -> (x, y) of nodes whose z is still to sample
    node_fid = {}           # name -> the feature it was made on (its deck, if any)
    deck_centres = {}       # original centreline point for deck/tunnel height lookup
    pending_by = {}         # name -> (-width, fid) of the road whose point it is (the widest's, then the lower id)
    # the stretches through tunnels, left out of the graph (no ground there for the AI), for the minimap only
    # (minimaproads.lua: its lines broke at every tunnel and every underpass mapped as one; the user, 2026-11-04)
    lines = []              # [x1, y1, x2, y2, drivability]
    # tunnels: fid -> [(xy (k, 2), floor z (k,))] of the tunnels the road tiles build (roads.TunnelProfiles): a stretch
    # through one is the AI's too, its nodes at the tunnel's floor; a tunnel not built (under water, none at hand) stays
    # the minimap's only (the AI's graph broke at every tunnel since they were built, 1.4.20; the user, 2026-11-05)
    tunnel_node = {}        # name -> fid of the tunnel its height is read from
    tube_edges = set()      # id() of the edges through a tunnel (portal_taper)
    mainline = _mainline_connectors(feats_list)     # (CROSSOVER_M)
    density_lines = []      # [(centreline xy, spec)] of the drivable roads near, for the traffic profile (_traffic_profile)
    import traffic
    density_r = traffic.SAMPLE_RADIUS_M
    for feats in feats_list:
        if feats is None or len(feats) == 0:
            continue
        rspecs = roads.road_specs(feats, urban_fn, widths, left_hand)      # the roads the road tiles (and their props) see
        bspecs = roads.bridge_specs(feats)
        specs = [sp if sp is not None else bs for sp, bs in zip(rspecs, bspecs)]
        rules = feats.rules
        sub = rules.get("subclass")
        con = rules.get("connectors")
        speed = rules.get("max_speed_kmh")
        ow = rules.get("oneway")
        for i, spec in enumerate(specs):
            fid = str(feats.ids[i])
            if spec is None or fid in seen:
                continue
            seen.add(fid)
            parts = feats.parts(i)
            if not parts or not parts[0]:
                continue
            ll = np.asarray(parts[0][0], np.float64)
            if len(ll) < 2:
                continue
            e, n, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.0)
            p = np.stack([e, n], 1)
            pmin, pmax = p.min(0), p.max(0)
            if (pmax[0] < cx - window - 200 or pmin[0] > cx + window + 200 or
                    pmax[1] < cy - window - 200 or pmin[1] > cy + window + 200):
                continue
            if rspecs[i] is not None:
                ring_cands.append((fid, rspecs[i], np.column_stack([p, np.zeros(len(p))]), None))
            seg = np.hypot(*np.diff(p, axis=0).T)
            s = np.concatenate([[0.0], np.cumsum(seg)])
            L = float(s[-1])
            if L < 1.0:
                continue
            if rspecs[i] is not None:
                tp = roads.joint_desc(copies.get(fid, [(feats, i)]),
                                      lambda f: roads.road_tapers(f, urban_fn, widths, left_hand))
                for ring_ll in roads.ground_parts(feats, i):
                    ring_ll = placement.ring(frame, feats, i, np.asarray(ring_ll, np.float64), rspecs[i], fid)
                    if len(ring_ll) < 2:
                        continue
                    ge, gn, _ = frame.geodetic_to_enu(ring_ll[:, 1], ring_ll[:, 0], 0.0)
                    ws = roads.taper_widths(rspecs[i].width, tp, ring_ll[:, 0], ring_ll[:, 1])
                    half = rspecs[i].width / 2 if ws is None else (ws[:-1] + ws[1:]) / 4
                    carriage.append((half, np.column_stack([ge, gn])))
                    carriage_ll.append(ring_ll)
                    pieces.setdefault(fid, []).append(carriage[-1][1])
            if bspecs[i] is not None and decks and fid in decks:
                for part in decks[fid]:
                    dxy, dz = part[:2]
                    depth = float(part[2]) if len(part) > 2 else roads.DECK_FULL_DEPTH
                    signal_depths[fid] = max(signal_depths.get(fid, 0.0), depth)
                    signal_parts.append((fid, bspecs[i], np.column_stack([dxy, dz]), float(np.mean(ll[:, 0]))))
                walk = roads.deck_walks(feats, i, rspecs, left_hand)
                if walk:
                    signal_walks[fid] = walk
            # the junction arms as the road tiles' props see them (every road they draw: service roads, driveways
            # and tracks too, no bridge-only roads), so furniture.junction_controls decides the same there and here
            if rspecs[i] is not None and con is not None:
                for r in con.rows(i):
                    at = float(con.start[r])
                    if at > 0.001 and at < 0.999:
                        continue
                    end = 0 if at <= 0.001 else -1
                    j0, j1 = (0, 1) if end == 0 else (len(p) - 1, len(p) - 2)
                    dd = p[j1] - p[j0]
                    dl = float(np.hypot(*dd))
                    if dl > 1e-6:
                        prop_arms.setdefault("c" + str(con.value[r]), []).append(
                            (fid, rspecs[i], dd / dl, np.array([p[j0][0], p[j0][1], 0.0]), end, L))
            if spec.cls not in DRIVE or roads._rule_value(sub, i) in SKIP_SUBCLASSES:
                continue
            if spec.cls not in DENSITY_SKIP and pmax[0] >= cx - density_r and pmin[0] <= cx + density_r and \
                    pmax[1] >= cy - density_r and pmin[1] <= cy + density_r:
                density_lines.append((p, spec))      # (adaptive traffic: _traffic_profile)
            tunnel_ranges = [(a_, b_) for a_, b_, k_ in roads.level_ranges(feats, i) if k_ == "tunnel"]
            # the nodes along it: connectors at their arc length, shape points that bend it, long gaps split
            marks = {}
            if con is not None:
                for r in con.rows(i):
                    marks[round(float(con.start[r]) * L, 2)] = "c" + str(con.value[r])
            w = driving_width(spec)
            # A narrow street gets the stock game's ~20 cm precision instead of a 60 cm corner cut.
            tolerance = min(SIMPLIFY_M, max(0.1, w * 0.035))
            for k in _rdp(p, tolerance):
                sk = round(float(s[k]), 2)
                if not any(abs(sk - m) < 0.05 for m in marks):
                    marks[sk] = f"s{fid}_{k}"
            if 0.0 not in marks and not any(m < 1.0 for m in marks):
                marks[0.0] = f"s{fid}_0"
            if not any(m > L - 1.0 for m in marks):
                marks[round(L, 2)] = f"s{fid}_{len(p) - 1}"
            # A speed limit may change inside an Overture segment. Keep its boundaries in the graph.
            if speed is not None:
                for r in speed.rows(i):
                    for at in (float(speed.start[r]), float(speed.end[r])):
                        sk = round(at * L, 2)
                        if 0 < at < 1 and not any(abs(sk - m) < 0.01 for m in marks):
                            marks[sk] = f"s{fid}_v{int(round(at * 1e6))}"
            order = sorted(marks)
            full = []
            for a, b in zip(order[:-1], order[1:]):
                full.append(a)
                gap = b - a
                max_edge = min(MAX_EDGE_M, max(12.0, 8 * w))
                if gap > max_edge:
                    m = int(math.ceil(gap / max_edge))
                    for j in range(1, m):
                        sj = round(a + gap * j / m, 2)
                        marks[sj] = f"s{fid}_{int(round(sj * 10))}d"
                        full.append(sj)
            full.append(order[-1])
            xy = np.stack([np.interp(full, s, p[:, 0]), np.interp(full, s, p[:, 1])], 1)
            deck_xy = xy.copy()
            if placement.on:            # (the AI's lanes where the road tiles draw a placed carriageway's)
                la_, lo_, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
                psh = placement.shift(feats, i, lo_, la_, spec, fid)
                if psh is not None:
                    xy = roads.offset_polyline(np.c_[xy, np.zeros(len(xy))], psh)[:, :2]
            offset = roads.driving_offset(spec)
            if abs(offset) > 1e-6:
                xy = roads.offset_polyline(np.c_[xy, np.zeros(len(xy))], offset)[:, :2]
            names = [marks[v] for v in full]
            # this road's edge attributes
            mid = 0.5
            driv = DRIVE[spec.cls][0]
            if spec.cls == "service" and L <= CROSSOVER_M and _crossover(con, i, mainline):
                driv = DRIVE["track"][0]
            direction = 0
            if ow is not None:
                for r in ow.rows(i):
                    if ow.start[r] <= mid <= ow.end[r]:
                        direction = int(ow.value[r])
            if direction == 0 and spec.oneway and spec.dir:     # a divided road's carriageway (roads.dual_carriageways)
                direction = int(spec.dir)
            if spec.kind == "paved" and spec.cls in roads.LANE_CLASSES:     # (the painted lanes: roads.lane_layout)
                n = roads.lane_layout(spec)[1]
                tags = roads.lane_tags(feats, i) or {}
                import roadlanes
                lo, li = ((n, 0) if direction else roadlanes.directions(n, tags)[:2]
                          if spec.lanes or tags else _lanes(w, False, n))
            else:
                lo, li = _lanes(w, direction != 0, spec.lanes)
            profile = _speed_profile(xy, w, direction != 0, urban=bool(getattr(spec, "urban", True)))
            # (a pass road's widened hairpins: its half width there, roads.hairpin_extra on its whole line)
            hx = roads.hairpin_extra(frame, spec, np.asarray(p, np.float64)[:, :2])
            hxm = np.zeros(len(full)) if hx is None else np.interp(full, s, hx)
            # this road's approaches at its two ends (the graph node next to the junction, whether traffic comes in)
            for end in (0, -1):
                j, nb = (0, 1) if end == 0 else (len(names) - 1, len(names) - 2)
                if not names[j].startswith("c") or names[j] == names[nb]:
                    continue
                incoming = direction == 0 or (direction > 0) == (end == -1)   # unless one-way out of it
                approaches[(names[j], fid, end)] = (names[nb], incoming)
            rough = rough_road(feats, i)
            for j in range(len(names) - 1):
                a, b = names[j], names[j + 1]
                if a == b:
                    continue
                at = (full[j] + full[j + 1]) / (2 * L)
                legal = road_mph(feats, i, spec, mph, at=at, country=country, de_on=de_on) * MPH
                in_tunnel = False
                if tunnel_ranges and any(t0 <= at <= t1 for t0, t1 in tunnel_ranges):
                    # each end inside the tunnel on its built floor (an end past a portal is on the ground)
                    ends_in = [(nm_, q_) for nm_, q_, at_ in ((a, deck_xy[j], full[j]), (b, deck_xy[j + 1], full[j + 1]))
                               if any(t0 * L < at_ < t1 * L for t0, t1 in tunnel_ranges)]
                    if AI_TUNNELS and tunnels and fid in tunnels and w >= TUNNEL_AI_MIN_W and all(
                            _deck_z(tunnels, fid, float(q_[0]), float(q_[1])) is not None for _, q_ in ends_in):
                        for nm_, _q in ends_in:
                            tunnel_node[nm_] = fid      # (the AI's too: through the built tunnel)
                        in_tunnel = True
                    else:
                        lines.append([round(float(xy[j][0]), 1), round(float(xy[j][1]), 1),
                                      round(float(xy[j + 1][0]), 1), round(float(xy[j + 1][1]), 1), driv,
                                      round(legal, 2)])
                        continue            # no tunnel built there: the minimap's line only
                if direction < 0:
                    a, b = b, a
                pa, pb = xy[j] if direction >= 0 else xy[j + 1], xy[j + 1] if direction >= 0 else xy[j]
                for nm, q, dk in ((a, pa, j if direction >= 0 else j + 1),
                                  (b, pb, j + 1 if direction >= 0 else j)):
                    # a connector the map's lines reach at different points (a ramp's end 3.3 m off the motorway's
                    # joint, I-83 in Baltimore: the AI's lanes stood 3 m east of the drawn ones, their edge on the
                    # parapet, 2026-10-29) stands where the widest road's line reaches it (then the lower id): not
                    # wherever the first road read put it
                    old = pending_by.get(nm)
                    if nm not in nodes and (old is None or (-w, fid) < old):
                        pending[nm] = (float(q[0]), float(q[1]))
                        node_fid[nm] = fid
                        deck_centres[nm] = deck_xy[dk]
                        pending_by[nm] = (-w, fid)
                target = min(legal, float(profile[j]), float(profile[j + 1]))
                half = w / 2 - (MOUNTAIN_EDGE_M if getattr(spec, "mountain", False) else 0.0) + float(max(hxm[j], hxm[j + 1]))
                if in_tunnel:
                    # (the AI keeps TUNNEL_AI_WALL_M off the tube's walls, roadmesh.tunnel's road + shoulders: with the
                    # road's half width it ran 5.3 m off its line at 56 mph into the Fort Pitt Tunnel's portal frame)
                    import roadmesh
                    half = max(1.5, min(half, spec.width / 2 + roadmesh.TUNNEL_SHOULDER - TUNNEL_AI_WALL_M))
                edges.append([a, b, 1 if direction else 0, lo, li, round(target, 2), driv, round(half, 2)])
                edge_fids.append(fid)
                pf = posted_field(legal, target, mapped_limit(feats, i, (full[j] + full[j + 1]) / (2 * L)))
                if pf is not None:
                    posted[id(edges[-1])] = pf
                if rough:
                    rough_edges.add(id(edges[-1]))
                if rumble_eligible(country, spec):
                    rumble_edges.add(id(edges[-1]))
                if in_tunnel:
                    tube_edges.add(id(edges[-1]))
    # The static props use 3D solved roads/decks. A 2D graph clearance line would falsely block every level below
    # a viaduct, suppressing the switchable head while leaving the static signal behind.
    if carriage_ll:
        ll = np.concatenate(carriage_ll)
        heights = np.asarray(frame.geodetic_to_enu(
            ll[:, 1], ll[:, 0], np.asarray(elev(ll[:, 1], ll[:, 0]), np.float64))[2], np.float64)
        at = 0
        for i, (half, xy) in enumerate(carriage):
            carriage[i] = (half, np.column_stack([xy, heights[at:at + len(xy)]]))
            at += len(xy)
    signal_decks = []
    if signal_parts:
        chunks = roads._deck_chunks(
            signal_parts, (cx - window - 200, cy - window - 200, cx + window + 200, cy + window + 200),
            depths=signal_depths, walks=signal_walks,
            shift_of=roads.deck_shift_fn(frame, feats_list, urban_fn, widths, left_hand),
            width_of=roads.deck_width_fn(frame, feats_list))
        for ch in chunks:
            if ch.get("deck"):
                signal_decks.append(roads._deck_clearance(ch))

    def signal_ground(x, y):
        la, lo, _ = frame.enu_to_geodetic(np.asarray([x]), np.asarray([y]), 0.0)
        return frame.geodetic_to_enu(la, lo, np.asarray(elev(la, lo), np.float64))[2]

    import islands
    ring = islands.ring_directions(ring_cands, left_hand)
    ring_nodes = {nm for fid, e in zip(edge_fids, edges) if fid in ring for nm in e[:2]}
    for fid, e in zip(edge_fids, edges):
        if fid in ring:
            if not e[2]:             # a mapped one-way takes precedence over inferred circulation
                if ring[fid] < 0:
                    e[0], e[1] = e[1], e[0]
                e[2] = 1
                e[3], e[4] = _lanes(e[7] * 2, True)
            e[5], e[6] = min(e[5], round(RING_MS, 2)), 1.0
        elif any(nm in ring_nodes for nm in e[:2]):
            # Yield through the stock AI's priority mechanism, not action 4 (which it ignores).
            e[6] = min(e[6], ENTRY_DRIVABILITY)
            e[5] = min(e[5], round(RING_MS, 2))
    # keep the edges with both ends in the window; z of their nodes from the ground
    inside = {nm for nm, (x, y) in pending.items() if abs(x - cx) <= window and abs(y - cy) <= window}
    edges = [e for e in edges if e[0] in inside and e[1] in inside]
    used = sorted({e[0] for e in edges} | {e[1] for e in edges})
    if used:
        X = np.array([pending[nm][0] for nm in used])
        Y = np.array([pending[nm][1] for nm in used])
        la, lo, _ = frame.enu_to_geodetic(X, Y, 0.0)
        # the anchor frame's up, as the ground drawn: raw heights put the AI's nodes and the traffic lights over the
        # ground by the Earth's curve, ~6 m 10 km from the anchor, ~18 m at the 15 km rebase (PR #112's finding,
        # measured in game 2026-10-24); the decks' heights (_deck_z) are already in this frame (roads.BridgeProfiles)
        Z = np.asarray(frame.geodetic_to_enu(la, lo, np.asarray(elev(la, lo), np.float64))[2], np.float64).copy()
        if decks:
            for k, nm in enumerate(used):
                dz = _deck_z(decks, node_fid.get(nm), *deck_centres.get(nm, (X[k], Y[k])))
                if dz is not None:
                    Z[k] = dz
        if tunnel_node:
            for k, nm in enumerate(used):
                if nm in tunnel_node:
                    tz = _deck_z(tunnels, tunnel_node[nm], *deck_centres.get(nm, (X[k], Y[k])))
                    if tz is not None:
                        Z[k] = tz
        radius = {}
        for e in edges:
            for nm in e[:2]:
                radius[nm] = max(radius.get(nm, 0.0), e[7])
        for nm, x, y, z in zip(used, X, Y, Z):
            nodes[nm] = [round(float(x), 2), round(float(y), 2), round(float(z), 2), radius[nm]]
    de_ctx = None               # Germany's junctions (de_on, the window touching Germany): the road tiles' decision
    if de_on:
        import designs
        if designs.reads_here(True, frame, (cx - window - 200, cy - window - 200, cx + window + 200,
                                            cy + window + 200)):
            de_ctx = _de_context(frame, feats_list, urban_fn, left_hand, de_routes, priority)
    yields, priorities = [], []
    signals = _signals(prop_arms, approaches, nodes, left_hand, set(ring), carriage, signals, country, priority,
                       decks=signal_decks, ground=signal_ground, de_ctx=de_ctx, pieces=pieces, yields=yields,
                       priorities=priorities)
    if yields:
        _give_way(edges, approaches, yields)
    if priorities:
        _priority_way(edges, approaches, priorities)
    # one edge per node pair (two segments joining the same connectors: keep the first)
    uniq, pairs = [], set()
    for e in edges:
        k = (min(e[0], e[1]), max(e[0], e[1]))
        if k not in pairs:
            pairs.add(k)
            uniq.append(e)
    if tube_edges:
        portal_taper(nodes, uniq, tube_edges, set(tunnel_node))
    for e in uniq:              # (posted limit e[10], rough flag e[11], rumble candidate e[12], after optional portal radius e[9])
        pf = posted.get(id(e))
        rough = id(e) in rough_edges
        rumble = id(e) in rumble_edges
        if pf is not None or rough or rumble:
            if len(e) == 8:
                e.append(None)
            e.append(pf)
            if rough or rumble:
                e.append(bool(rough))      # (false, not null: no hole before the rumble flag in the mod's array)
            if rumble:
                e.append(True)
    lines = [ln for ln in lines if max(abs(ln[0] - cx), abs(ln[1] - cy), abs(ln[2] - cx), abs(ln[3] - cy)) <= window]
    return {"nodes": nodes, "edges": uniq, "signals": signals, "lines": lines,
            "trafficProfile": _traffic_profile(frame, density_lines, cx, cy), "mph": bool(mph)}


# Adaptive traffic (PR #728 / #773, ProxyFirewall202; traffic.py, docs/ADAPTIVE_TRAFFIC.md): the graph carries a
# population profile of its centre, from the drivable road length within traffic.SAMPLE_RADIUS_M (tracks and busways
# left out: farm tracks are not traffic roads), the road nearest the centre (a divided one doubles it) and the nearest
# populated place (settlements.py). The mod uses it only with its Adaptive traffic amount setting on.
DENSITY_SKIP = {"track", "busway"}
NEAR_ROAD_M = 100.0         # the nearest road counts for the divided-road multiplier this close to the centre
_profile_warned = []


def _traffic_profile(frame, lines, cx, cy):
    """traffic.calculate's profile at the graph's centre (cx, cy) from lines [(xy (k, 2), spec)]: one vectorised pass
    over all their segments (the length inside the sample circle, the nearest segment); never raises."""
    import logging
    import settlements
    import traffic
    radius = traffic.SAMPLE_RADIUS_M
    try:
        try:
            la, lo, _ = frame.enu_to_geodetic(float(cx), float(cy), 0.0)
            place = settlements.nearest(float(np.asarray(la).reshape(-1)[0]), float(np.asarray(lo).reshape(-1)[0]))
        except Exception as e:      # (no place: the mapped roads alone)
            place = None
            if not _profile_warned:
                _profile_warned.append(1)
                logging.getLogger("earthd.navgraph").warning("adaptive traffic: no settlement lookup (%s: %s)",
                                                             type(e).__name__, e)
        total = urban = major = local = 0.0
        near = (math.inf, None)
        if lines:
            # every line's points end to end; the segments between two lines (owner changes) left out
            pts = np.concatenate([np.asarray(xy, np.float64)[:, :2] for xy, _ in lines])
            owner = np.repeat(np.arange(len(lines)), [len(xy) for xy, _ in lines])
            same = owner[1:] == owner[:-1]
            a, d, owner = pts[:-1][same] - (cx, cy), np.diff(pts, axis=0)[same], owner[:-1][same]
            aa = np.einsum("ij,ij->i", d, d)
            ok = aa > 1e-12
            aa_ = np.where(ok, aa, 1.0)
            # the length of each segment inside the circle (the chord's parameter range [lo, hi] clipped to [0, 1])
            bb = 2.0 * np.einsum("ij,ij->i", a, d)
            cc = np.einsum("ij,ij->i", a, a) - radius * radius
            disc = bb * bb - 4.0 * aa_ * cc
            root = np.sqrt(np.maximum(disc, 0.0))
            lo_ = np.maximum(0.0, (-bb - root) / (2.0 * aa_))
            hi_ = np.minimum(1.0, (-bb + root) / (2.0 * aa_))
            inside = np.where(ok & (disc >= 0.0), np.sqrt(aa_) * np.maximum(0.0, hi_ - lo_), 0.0)
            per = np.bincount(owner, inside, minlength=len(lines))
            flags = np.array([(bool(getattr(sp, "urban", False)), sp.cls in traffic.MAJOR_CLASSES,
                               sp.cls in traffic.LOCAL_CLASSES) for _, sp in lines], np.float64).reshape(-1, 3)
            total = float(per.sum())
            urban, major, local = (float(v) for v in per @ flags)
            # the segment nearest the centre (its closest point)
            t = np.clip(-np.einsum("ij,ij->i", a, d) / aa_, 0.0, 1.0)
            q = a + t[:, None] * d
            dist = np.hypot(q[:, 0], q[:, 1])
            if len(dist):
                j = int(np.argmin(dist))
                near = (float(dist[j]), lines[int(owner[j])][1])
        dist, sp = near
        cls = sp.cls if sp is not None and dist <= NEAR_ROAD_M else "unknown"
        divided = sp is not None and dist <= NEAR_ROAD_M and traffic.is_divided_road(
            sp.cls, getattr(sp, "median", 0.0), getattr(sp, "link", False), getattr(sp, "oneway", False))
        prof = traffic.calculate(total, urban, major, local, settlement=place, road_class=cls, divided=divided,
                                 sample_radius_m=radius, max_traffic=traffic.SERVER_TARGET_MAX)
        prof["roadDistanceM"] = round(dist, 1) if dist < math.inf else None
        return prof
    except Exception as e:          # (a graph without a profile rather than none: the mod keeps the player's amount)
        logging.getLogger("earthd.navgraph").warning("adaptive traffic profile: %s: %s", type(e).__name__, e)
        return None


PORTAL_LEAD_M = 60.0        # m before a tunnel's portal over which the AI's road narrows to the tube's (portal_taper)


def _mainline_connectors(feats_list):
    """The connector ids on the motorways' main carriageways (class motorway, not a link) of the roads layers."""
    out = set()
    for feats in feats_list:
        if feats is None or len(feats) == 0:
            continue
        cls = feats.attrs.get("class")
        con = feats.rules.get("connectors")
        if cls is None or con is None:
            continue
        sub = feats.rules.get("subclass")
        for i in np.flatnonzero(np.asarray(cls).astype(str) == "motorway").tolist():
            if roads._rule_value(sub, i) == "link":
                continue
            out.update(str(con.value[r]) for r in con.rows(i))
    return out


def _crossover(con, i, mainline):
    """Do road i's two ends (its connectors at its start and end) both lie on motorway main carriageways?"""
    if con is None or not mainline:
        return False
    ends = {}
    for r in con.rows(i):
        at = float(con.start[r])
        if at <= 0.001:
            ends[0] = str(con.value[r])
        elif at >= 0.999:
            ends[1] = str(con.value[r])
    return len(ends) == 2 and ends[0] != ends[1] and ends[0] in mainline and ends[1] in mainline


def portal_taper(nodes, edges, tube_edges, inside):
    """In place: the roads leading to a tunnel narrow to its tube's AI half width at the portal, over PORTAL_LEAD_M.
    A portal node (an end of a tube edge outside the tunnel) took the widest of its roads' half widths, so the AI
    spread its lanes over the approach's (Fort Pitt Tunnel's west portal: carriageways 11.6 m wide and shoulders into
    openings of ~9 m, an AI car 5 m off its line into the pier between the tubes three times, 2026-11-05). An edge
    near a portal gets its two ends' radii (edge[7] at edge[0], an added edge[8] at edge[1]: the stock AI's
    inRadius / outRadius, map.getEdgeRadii); the portal node's own radius is the tube's."""
    portal = {}
    for e in edges:
        if id(e) in tube_edges:
            for nm in e[:2]:
                if nm not in inside and nm in nodes:
                    portal[nm] = min(portal.get(nm, math.inf), e[7])
    if not portal:
        return
    # how far each node near a portal is from it along the roads outside (Dijkstra, PORTAL_LEAD_M at most), with the
    # nearest portal's half width
    adj = {}
    for e in edges:
        if id(e) not in tube_edges and e[0] in nodes and e[1] in nodes:
            pa, pb = nodes[e[0]], nodes[e[1]]
            d = math.hypot(pa[0] - pb[0], pa[1] - pb[1])
            adj.setdefault(e[0], []).append((e[1], d))
            adj.setdefault(e[1], []).append((e[0], d))
    import heapq
    best = {nm: (0.0, r) for nm, r in portal.items()}
    heap = [(0.0, nm) for nm in portal]
    while heap:
        d, nm = heapq.heappop(heap)
        if d > best[nm][0]:
            continue
        for ot, dd in adj.get(nm, ()):
            nd = d + dd
            if nd < PORTAL_LEAD_M and (ot not in best or nd < best[ot][0]):
                best[ot] = (nd, best[nm][1])
                heapq.heappush(heap, (nd, ot))

    def at(nm, half):
        b = best.get(nm)
        if b is None or b[1] >= half:
            return half
        return round(b[1] + (half - b[1]) * b[0] / PORTAL_LEAD_M, 2)
    for e in edges:
        if id(e) in tube_edges:
            continue
        r0, r1 = at(e[0], e[7]), at(e[1], e[7])
        if r0 != e[7] or r1 != e[7]:
            e[7] = r0
            e.append(r1)
    for nm, r in portal.items():
        nodes[nm][3] = min(nodes[nm][3], r)


STOP_BACK_M = 2.0           # the stop point: this far before the junction's widest road edge
# An approach this short is a link inside one intersection (offset streets split it into connectors a few metres
# apart: Fells Point): no stop or signal on it. A car that had stopped at the first node met the next one's stop
# line behind it and waited forever (in game 2026-10-14, a queue of four at an all-way stop).
SHORT_APPROACH_M = 15.0


def _de_context(frame, feats_list, urban_fn, left_hand, routes, priority):
    """The road tiles' designs.context for the graph's roads (roads.build_tile's): these cells' roads (joined with the
    lane shards: their priority roads and 30 zones) and their lane shard sign nodes (railtags.sign_nodes), the cells'
    road_routes (routes: the ref ranks), the map's stop and give-way points (priority) and the land cover's town before
    the tiles' drawing overrides (roads.road_towns: the signs' town test, designs.in_town)."""
    import designs
    import railtags
    towns, signs = {}, []
    for f in feats_list:
        if f is None or len(f) == 0:
            continue
        got = roads.road_towns(f, urban_fn, left_hand)
        if got is not None:
            for fid, t in zip(f.ids, got):
                towns.setdefault(str(fid), bool(t))
        signs.extend(railtags.sign_nodes(f))
    return designs.context(frame, feats_list, routes, priority, signs, towns=towns)


def _de_decide(farms, nodes, ctx, pieces, link_ok, ring, signals, country, priority):
    """{junction node: designs.controls' answer [(arm, what, plate)]} of the graph's junctions of three or more arms
    whose point is inside Germany and which the kernel decides, as the road tiles' designs.decide does: their arms in
    the tiles' order (furniture._arms of the roads by id: the lowest id's arm gives the junction's point), each arm's
    own road (designs._arm_line of its ground part, pieces) for the map's signs. A roundabout's or a signalised
    junction's answer (furniture.junction_controls': groups) is left out: today's graph there."""
    import designs
    names = [n for n, a in farms.items() if n in nodes and len(a) >= 3]
    if not names:
        return {}
    own = {n: sorted(farms[n], key=lambda x: (str(x[0]), not x[4])) for n in names}
    rows = ctx.mapped is not None and len(ctx.mapped) > 0
    out = {}
    for n, de in zip(names, designs._de(ctx.frame, [own[n][0][3][:2] for n in names])):
        if not de:
            continue
        lines = [designs._arm_line(x, pieces) if x[0] in pieces else None for x in own[n]] if rows else None
        ctl = designs.controls(own[n], n, link_ok, ring, signals, country, priority, ctx, lines)
        if ctl and not any(isinstance(w, str) for _, w, _ in ctl):      # (the kernel's: one per arm, never empty)
            out[n] = ctl
    return out


def _give_way(edges, approaches, yields):
    """In place: the approach edge of each arm that gives way at a German junction (yields: _signals' [(junction node,
    controls' answer)]), a 205's or an exit's (designs.EXIT: StVO § 10), coming in, takes drivability min(its own, the
    lowest of the approaches it gives way to - DE_YIELD_STEP): the stock AI gives way on an incoming edge of lower
    drivability than another at the node (the roundabout entries' method), so its traffic lets theirs go first. It
    gives way to the arms of the junction's priority pair (306 / 301), or where it has none to its right-before-left
    arms (a mapped 205 among them). An approach of any length (the stop's SHORT_APPROACH_M is the stop point's: a short
    approach is stepped all the same); none where the edge or the arms it gives way to are not in the graph.
    One edge can be a junction's give-way approach and the next junction's priority arm (a side road 30 m long that
    goes on through the next junction): stepped for the first, it ties the second's own 205. So the pass is
    repeated on the values the last one left, at most DE_YIELD_PASSES times, until no edge changes: the second's 205
    a step below that (1.0, 0.98, 0.96 ...). Each pass takes its steps from the edges as they were at its start and
    only lowers them (min), so the outcome is the same in any order of the junctions; a chain of more than
    DE_YIELD_PASSES such junctions keeps its last tie, and so does a cycle of them (each one's 205 the next one's
    priority arm), which would otherwise go on down."""
    import designs
    by = {}
    for e in edges:
        by.setdefault((e[0], e[1]) if e[0] <= e[1] else (e[1], e[0]), []).append(e)

    def approach(name, arm):
        nb, incoming = approaches.get((name, arm[0], 0 if arm[4] else -1), (None, False))
        return (by.get((nb, name) if nb <= name else (name, nb)) if nb is not None else None), incoming
    jobs = []                   # per junction: (the approaches it gives way to, its giving-way approaches)
    for name, ctl in yields:
        got = [(w,) + approach(name, a) for a, w, _ in ctl]
        ref = [es for w, es, _ in got if es and w in (designs.Z306, designs.Z301)] or \
            [es for w, es, _ in got if es and w == designs.RVL]
        give = [es for w, es, incoming in got if es and incoming and w in (designs.Z205, designs.EXIT)]
        if ref and give:
            jobs.append((ref, give))
    for _ in range(DE_YIELD_PASSES):
        steps = []
        for ref, give in jobs:
            to = round(min(es[0][6] for es in ref) - DE_YIELD_STEP, 4)
            steps += [(es, to) for es in give if any(e[6] > to for e in es)]
        if not steps:
            break
        for es, to in steps:
            for e in es:
                e[6] = min(e[6], to)


PRIORITY_SLOW_MS = 2.0      # the road through a right-of-way junction (_priority_way): its approach this much slower,
PRIORITY_SLOW_FLOOR_MS = 6.0    # not under this (nor raised to it)


def _priority_way(edges, approaches, priorities):
    """In place (furniture.PRIORITY_STOPS, PR #728 / #773): the approach edges coming into each junction road-class
    right of way decides (priorities: _signals' [(junction node, arms)]) take drivability min(their own, their road's
    furniture.PRIORITY_DRIVABILITY): the stock AI gives way on an incoming edge of lower drivability than another at
    the node (_give_way's method), so a side road's traffic, stopped at its sign, lets the road through go first;
    the road through's own approaches come in PRIORITY_SLOW_MS slower (at most down to PRIORITY_SLOW_FLOOR_MS)."""
    import furniture
    by = {}
    for e in edges:
        by.setdefault((e[0], e[1]) if e[0] <= e[1] else (e[1], e[0]), []).append(e)
    for name, arms in priorities:
        ranked = [(a, furniture.priority_rank(a[1])) for a in arms]
        ranked = [(a, r) for a, r in ranked if r is not None]
        incoming = []
        for a, r in ranked:
            nb, inc = approaches.get((name, a[0], 0 if a[4] else -1), (None, False))
            es = by.get((nb, name) if nb is not None and nb <= name else (name, nb)) if nb is not None else None
            if es and inc:
                incoming.append((r, es))
        if not incoming:
            continue
        top = max(r for r, _ in incoming)
        for r, es in incoming:
            for e in es:
                e[6] = min(e[6], furniture.PRIORITY_DRIVABILITY[r])
                if r == top:
                    e[5] = round(min(e[5], max(PRIORITY_SLOW_FLOOR_MS, e[5] - PRIORITY_SLOW_MS)), 2)


def _signals(arms, approaches, nodes, left_hand=False, ring=None, lines=(), signals=None, country=None, priority=None,
             decks=(), ground=None, de_ctx=None, pieces=None, yields=None, priorities=None):
    """[{"j": junction node, "kind": "signal" | "stop", "app": [[approach node, x, y, z, dir x, dir y, phase, prop x,
    prop y, prop z, prop yaw]]}] of the urban junctions furniture.junction_controls controls, decided on the arms
    the road tiles' props see (arms) and built for the graph's approaches into them (approaches): per incoming
    approach its stop point on the kerb-side half of the lane (where the AI's closest-edge search finds that
    approach), the travel direction, the phase (0 / 1, by the approach's axis) and the head's prop transform.
    Germany (de_ctx: the German junction signs' context, _de_context; pieces: the roads' ground parts {fid: [(k, 2)]}):
    a junction inside it the kernel decides (_de_decide, the road tiles' decision) in town and out of town, a "stop"
    on its 206 and right-before-left arms (the spec's B4: an invisible stop for the AI at right before left), no head
    (no stop sign stands there: the 206 is the road tiles' designs sign, and the mod raises heads for signals only);
    its 205 and exit arms appended with the junction to yields for _give_way (their drivability step); nothing else
    there (a 306 / 301; a node the kernel does not decide: no US stop either, as the road tiles').
    Its roundabouts and signalised junctions as everywhere. priorities: the junctions road-class right of way decides
    (furniture.priority_controls) appended as (junction node, its arms), for _priority_way."""
    import furniture
    out = []
    # furniture's arm tuples (fid, spec, u, point, at its start, length): the same decision as the road tiles' props
    farms = {name: [(x[0], x[1], x[2], x[3], x[4] == 0, x[5] if len(x) > 5 else math.inf) for x in a]
             for name, a in arms.items()}
    link_ok = furniture._link_check(farms, signals)
    # Restrict index construction to this graph's nodes (some source ways extend many kilometres beyond it).
    xy = np.asarray([v[:2] for v in nodes.values()], np.float64).reshape(-1, 2)
    bounds = (*xy.min(0), *xy.max(0)) if len(xy) else (0.0, 0.0, 0.0, 0.0)
    clearance = furniture.RoadClearance(lines, decks, bounds=bounds)
    german = {}
    if de_ctx is not None:
        import designs
        german = _de_decide(farms, nodes, de_ctx, pieces or {}, link_ok, ring, signals, country, priority)
    for name, a in arms.items():
        if name not in nodes or len(a) < 3:
            continue
        prop = True             # (the head's prop transform: today's signals and stops)
        if name in german:
            ctl = german[name]
            if yields is not None and any(w in (designs.Z205, designs.EXIT) for _, w, _ in ctl):
                yields.append((name, ctl))
            put = [(arm, furniture.G_STOP) for arm, w, _ in ctl if w in (designs.Z206, designs.RVL)]
            prop = False
        else:
            # (road-class right of way, furniture.PRIORITY_STOPS: in the country too, as the road tiles' props)
            prio = furniture.priority_controls(farms[name], ring, signals, country)
            if not prio and not furniture._controlled_here(farms[name], signals):
                continue
            put = [(arm, what) for arm, what in furniture.junction_controls(farms[name], None, link_ok, ring=ring,
                                                                             signals=signals, country=country,
                                                                             priority=priority)
                   if what != furniture.G_YIELD]    # give-way uses edge priority; a stop controller would force a stop
            if prio and priorities is not None:
                priorities.append((name, farms[name]))
        if not put:
            continue
        kind = "signal" if put[0][1] == furniture.G_SIGNAL else "stop"
        axis = put[0][0][2]
        wide = max(x[1].width for x in a) / 2
        jx, jy, jz = nodes[name][:3]
        app = []
        for arm, what in put:
            full = next(x for x in a if x[0] == arm[0] and x[2] is arm[2])
            fid, spec, u, _, end, *_ = full
            nb, incoming = approaches.get((name, fid, end), (None, False))
            if not incoming or nb not in nodes:
                continue
            if str(nb).startswith("c") and math.hypot(nodes[nb][0] - nodes[name][0],
                                                      nodes[nb][1] - nodes[name][1]) < SHORT_APPROACH_M:
                continue            # from another connector a few metres away (a road vertex there is no reason)
            inc = -u
            right = np.array([inc[1], -inc[0]]) * (-1 if left_hand else 1)     # the side traffic keeps to
            q = np.array([jx, jy]) + u * (wide + STOP_BACK_M) + right * (spec.width / 4)
            phase = 0 if abs(float(u @ axis)) > 0.7 else 1
            def support(x, y):
                return float(np.asarray(ground(x, y)).reshape(-1)[0]) + 0.15 if ground is not None else jz + 0.15
            # the head's prop where furniture.junction_props stands it (the switchable stock boom replaces it); none
            # where no spot is clear of the carriageways (no prop there either)
            pp = None
            if prop:
                side, yaw = furniture.approach_pose(u, what, left_hand)
                pp = furniture.signal_spot(np.array([jx, jy]), u, side, wide, spec.width / 2, lines, group=what,
                                           base_z=jz + 0.15, ground=support, clearance=clearance, yaw=yaw)
            head = [None] * 4 if pp is None else [round(float(pp[0]), 2), round(float(pp[1]), 2), round(support(*pp), 2),
                                                  yaw]
            app.append([nb, round(float(q[0]), 2), round(float(q[1]), 2), round(jz, 2),
                        round(float(inc[0]), 4), round(float(inc[1]), 4), phase] + head)
        if app:
            out.append({"j": name, "kind": kind, "app": app})
    return out


# Road rules by country (ISO 3166-1 alpha-2; the nearest airport's, AirportDB: a stand-in until a border map)
LEFT_HAND = {"GB", "IE", "IM", "JE", "GG", "MT", "CY", "IN", "PK", "BD", "LK", "NP", "BT", "MV", "JP", "ID", "MY",
             "SG", "BN", "TH", "TL", "HK", "MO", "AU", "NZ", "PG", "FJ", "WS", "TO", "KI", "SB", "TV", "NR", "NU",
             "CK", "ZA", "NA", "BW", "ZW", "ZM", "MZ", "MW", "KE", "TZ", "UG", "LS", "SZ", "MU", "SC", "SH", "FK",
             "JM", "BS", "BB", "TT", "GY", "SR", "AG", "DM", "GD", "KN", "LC", "VC", "VG", "VI", "AI", "KY", "TC",
             "BM", "MS", "PN", "NF", "CX", "CC"}     # (the last three: Australia's external territories)
TURN_ON_RED = {"US", "CA"}
# speed limits posted in mph (the rest: km/h); the US territories, the UK and its crown dependencies, Liberia, Myanmar;
# the Caribbean's mph countries and the US's former Pacific trust territories (Wikipedia "Miles per hour": Antigua and
# Barbuda, the Bahamas, Belize, Dominica, Grenada, Saint Kitts and Nevis, Saint Lucia, Saint Vincent, the Marshall
# Islands, Micronesia, Palau: their limits were drawn in km/h)
MPH_COUNTRIES = {"US", "PR", "VI", "GU", "AS", "MP", "UM", "GB", "IM", "JE", "GG", "LR", "MM",
                 "AG", "BS", "BZ", "DM", "GD", "KN", "LC", "VC", "MH", "FM", "PW"}
RED_YELLOW = {"GB", "IE", "DE", "AT", "CH", "LI", "NL", "BE", "LU", "DK", "NO", "SE", "FI", "IS", "PL", "CZ", "SK",
              "HU", "SI", "HR", "BA", "RS", "ME", "MK", "AL", "RO", "BG", "LT", "LV", "EE", "UA", "BY", "RU", "IL"}


def road_rules(country):
    """{"rightHandDrive" (the game's name: true = driving on the LEFT), "turnOnRed", "redYellow"} of a country
    (US rules for an unknown one)."""
    c = (country or "US").upper()
    return {"rightHandDrive": c in LEFT_HAND, "turnOnRed": c in TURN_ON_RED, "redYellow": c in RED_YELLOW,
            "country": c, "mph": c in MPH_COUNTRIES}


_NEAREST = {}       # (id(db), lat, lon) -> (db, runway index): geo_rules asks country_near then region_near there


def _nearest_runway(db, lat, lon):
    """The index of the runway nearest (lat, lon) in db (airports._hav's first minimum), the last few remembered."""
    k = (id(db), float(lat), float(lon))
    hit = _NEAREST.get(k)
    if hit is not None and hit[0] is db:
        return hit[1]
    import airports
    i = int(np.argmin(airports._hav(lat, lon, db._mid[:, 0], db._mid[:, 1])))
    if len(_NEAREST) > 64:
        _NEAREST.clear()
    _NEAREST[k] = (db, i)
    return i


def country_near(db, lat, lon):
    """The country of the airport nearest (lat, lon) in airports.AirportDB db, or None."""
    if db is None or not getattr(db, "runways", None):
        return None
    rw = db.runways[_nearest_runway(db, lat, lon)]
    ap = db.airports.get(rw.airport_ident)
    return getattr(ap, "country", None) or getattr(rw, "country", None) or None


def region_near(db, lat, lon):
    """The ISO 3166-2 region (e.g. "US-CA") of the airport nearest (lat, lon) in airports.AirportDB db, or None."""
    if db is None or not getattr(db, "runways", None):
        return None
    ap = db.airports.get(db.runways[_nearest_runway(db, lat, lon)].airport_ident)
    return getattr(ap, "region", None) or None


def border_country(lat, lon):
    """The territory at (lat, lon) by real borders (countrybounds: Overture's divisions, ~10 m), upper case, or None:
    no single territory there (the sea, a gap, two overlapping), no border data (countrybounds.Absent), or a lookup
    that raised (germany's back-off: one retry clock for every border lookup of the process). Never raises."""
    import germany
    idx = germany._index()
    if idx is None:
        return None
    try:
        c = idx.at(float(lat), float(lon))
    except Exception as e:
        germany._fail(e)
        return None
    return str(c).upper() if c else None


def country_at(db, lat, lon, borders=False):
    """The country of (lat, lon): with borders (the server fetched the border data anyway: its regional scenery is
    on), the real border's territory where there is one, else the nearest airport's (country_near). The airport alone
    is wrong at border towns: in British Columbia's Lower Mainland the nearest airport can be in Washington, so the
    speed signs read mph on a Vienna-style disc (GitHub #1046); Kehl gets Strasbourg's, Windsor Detroit's."""
    if borders:
        c = border_country(lat, lon)
        if c:
            return c
    return country_near(db, lat, lon)


def region_at(db, lat, lon, borders=False):
    """region_near, None where its airport is in another country than country_at's (a US state's rules don't reach a
    Canadian town whose nearest airport is in that state)."""
    r = region_near(db, lat, lon)
    if r and borders:
        c = border_country(lat, lon)
        if c and not str(r).upper().startswith(c + "-"):
            return None
    return r


def part_fid(pid):
    """The road feature's id of a chain part's id (roads.ChainView: "<fid>.<part>#<section>")."""
    return re.sub(r"(\.\d+)?(#\d+)?$", "", str(pid))


# The AI through built tunnels: off until their portals fit the roads leading in (Fort Pitt Tunnel, 2026-11-05: the
# approach carriageways, shoulders and all, are wider than the tube openings and lead into the pier between the twin
# tubes; an AI car hit it at 38-56 mph three times). Off, every tunnel stretch is the minimap's line only, as before.
AI_TUNNELS = os.environ.get("BNE_AI_TUNNELS", "off") == "on"     # (off by default; on for the Fort Pitt drive tests)
TUNNEL_AI_WALL_M = 2.5     # m the AI's road half width in a tunnel stays inside the tube's walls (a car's half width + room)
TUNNEL_AI_MIN_W = 5.0       # m of driving width a tunnel needs to be the AI's: a one-lane one (3.4 m, the Park Avenue
                            # Tunnel's south portal in Manhattan) was entered at an angle off the wider approach and the
                            # car hit the portal's pier (2026-11-05); narrower ones stay the minimap's only
REBUILD_M = 400.0           # a new graph once the player is this far from the last one's centre


def short_ids(graph):
    """The graph with every node name (an Overture id and its suffix, 38-46 characters, e.g.
    "s3d0fc61b-e9c6-487c-9938-418f4c654cb6_7555d") replaced by "n" + 12 hex characters of its hash: the same node
    keeps the same short name in every graph (gps.lua and the traffic follow node names from one graph to the next).
    The names were most of the file (a Times Square graph: 577 KB, 3,163 nodes named again in 3,353 edges and the
    signals), and the game decodes the file at every rebuild (every REBUILD_M): its Lua garbage, 2026-11-09. In the
    nodes' keys, the edges' first two fields, the signals' junction "j" and each approach's first field."""
    import hashlib
    memo = {}

    def sid(name):
        v = memo.get(name)
        if v is None:
            v = memo[name] = "n" + hashlib.blake2b(str(name).encode("utf-8"), digest_size=6).hexdigest()
        return v
    out = dict(graph)
    out["nodes"] = {sid(k): v for k, v in graph.get("nodes", {}).items()}
    out["edges"] = [[sid(e[0]), sid(e[1])] + list(e[2:]) for e in graph.get("edges", ())]
    if graph.get("signals"):
        out["signals"] = [dict(sg, j=sid(sg["j"]), app=[[sid(a[0])] + list(a[1:]) for a in sg.get("app", ())])
                          for sg in graph["signals"]]
    return out


SHORT_IDS = True


def write(out_dir, vfs_dir, aid, serial, graph):
    """The graph as navgraph_<aid>_<serial>.json in the session folder (short_ids); returns its vfs path."""
    import json
    import os
    name = f"navgraph_{aid}_{serial}.json"
    path = os.path.join(out_dir, name)
    tmp = staging.tmp_for(path)
    if SHORT_IDS:
        graph = short_ids(graph)
    with open(tmp, "w") as f:
        f.write(json.dumps(graph, separators=(",", ":")))    # the C encoder (json.dump streams in Python)
    staging.replace(tmp, path)
    return f"{vfs_dir}/{name}"


class Tracker:
    """Per connection: builds a graph around the player when the anchor changes or the player moved
    REBUILD_M from the last one's centre, one at a time, and announces it ("navgraph")."""

    def __init__(self, rebuild_m=REBUILD_M):
        self.rebuild_m = rebuild_m
        self.aid = None
        self.centre = None
        self.building = False
        self.serial = 0
        self.stale = 0          # builds up to this serial are of an old setting (refresh): not sent, not kept
        self.pool = None        # one thread of its own (vegetation.Tracker: not behind other trackers' builds)

    def close(self):
        if self.pool is not None:
            self.pool.shutdown(wait=False, cancel_futures=True)

    def refresh(self):
        """A setting the graph reads changed (the German junction signs): built again at the next pose, wherever the
        player is; a build already running lands unsent."""
        self.centre = None
        self.stale = self.serial

    def on_pose(self, conn, aid, x, y, loop, build):
        """build(aid, x, y, serial) runs in a worker and returns (vfs path, nodes, edges) or None."""
        if self.building:
            return
        if aid == self.aid and self.centre is not None and \
                max(abs(x - self.centre[0]), abs(y - self.centre[1])) < self.rebuild_m:
            return
        self.building = True
        self.serial += 1
        serial = self.serial
        if self.pool is None:
            from runtimeprofiler import ThreadPoolExecutor
            self.pool = ThreadPoolExecutor(1, thread_name_prefix="navgraph")
        fut = loop.run_in_executor(self.pool, build, aid, x, y, serial)
        fut.add_done_callback(lambda f: self._built(conn, aid, (x, y), serial, f))

    def _built(self, conn, aid, centre, serial, fut):
        self.building = False
        try:
            res = fut.result()
        except Exception as e:      # map data not at hand: the next pose asks again
            import logging
            logging.getLogger("earthd.navgraph").debug("navgraph %s: %s: %s", centre, type(e).__name__, e)
            return
        if res is None:
            return
        if serial <= self.stale:    # (the old setting's: the next pose builds again)
            self.aid = aid
            return
        self.aid, self.centre = aid, centre
        path, n, m = res
        conn.send({"type": "navgraph", "anchorId": aid, "path": path, "cx": round(centre[0], 1),
                   "cy": round(centre[1], 1), "nodes": n, "edges": m, "serial": serial})
