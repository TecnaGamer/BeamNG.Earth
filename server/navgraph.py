"""The AI road graph ("navgraph") around the player, for BeamNG's own traffic and AI (docs/research/
2026-10-10-navgraph-traffic-signals.md): the mod hands it to `map.reset(customNodes)`.

Nodes are Overture connectors (named "c<connector id>": the same name in every window and anchor, and shared
by every segment meeting at a junction) and the simplified shape points between them ("s<segment id>_<k>").
Each edge carries what the custom map path does not compute (research §2.4): its half width (radius), lanes
(per direction), one-way direction, driving target (m/s, at most the posted limit) and drivability. Positions
are in the anchor frame, z the ground (elev); the mod clips the graph to the terrain block before using it
(the only collidable ground).

Pure given the map cells and elev; deterministic.
"""
import math

import numpy as np

import roads
import staging

# classes the AI drives on: drivability (path cost divisor) and a default speed limit (mph, US practice)
DRIVE = {"motorway": (1.0, 65), "trunk": (1.0, 55), "primary": (1.0, 45), "secondary": (1.0, 40),
         "tertiary": (0.9, 35), "residential": (0.8, 25), "unclassified": (0.8, 30), "living_street": (0.5, 15),
         "service": (0.4, 15), "unknown": (0.6, 25)}
SKIP_SUBCLASSES = {"driveway", "parking_aisle", "sidewalk", "crosswalk", "cycle_crossing"}
URBAN_MPH_CAP = {"primary": 35, "secondary": 35, "tertiary": 30}   # in town (spec.urban) unless mapped
# the km/h countries' rural defaults for the fast classes (Europe's 120-130 motorways: Bavaria's A9, unmapped, was
# driven at the US 65 mph, 2026-10-15)
KMH_DEFAULT = {"motorway": 120, "trunk": 100, "primary": 80, "secondary": 70}
MPH = 0.44704
LANE_W_M = 3.3
SIMPLIFY_M = 0.6            # a shape point is kept where the chord would be off by more than this
MAX_EDGE_M = 80.0           # long straight edges are split (the AI and the spawner like shorter ones)
WINDOW_M = 1100.0           # half size of the square around the player the graph covers
CURVE_ACCEL = 2.0           # m/s^2: comfortable lateral acceleration, a driving target rather than a posted limit
BRAKE_ACCEL = 2.5           # m/s^2: spread a tight bend's target back along its approaches
NARROW_M = 5.5              # narrower two-way streets need slower encounters with oncoming traffic
NARROW_MS = 30.0 / 3.6
RING_MS = 30.0 / 3.6
ENTRY_DRIVABILITY = 0.9     # stock AI yields to the ring's 1.0, even on a tangential entry


def road_mph(feats, i, spec, mph=True, at=0.5):
    """The speed limit (mph) of road feature i at fraction at (default: its middle), else the class default
    (capped in town for arterials), or None for a class the AI does not drive. Shared by the graph and the road tiles'
    speed signs (furniture.py), so a sign shows what the traffic drives."""
    if spec is None or spec.cls not in DRIVE:
        return None
    speed = feats.rules.get("max_speed_kmh")
    if speed is not None:
        for r in speed.rows(i):
            if speed.start[r] <= at <= speed.end[r]:
                value = float(speed.value[r])
                if math.isfinite(value) and value > 0:
                    return value / 1.609344
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


def _speed_profile(xy, width, oneway, limit=math.inf):
    """Advisory speeds at graph points. Both directions can brake before a bend; legal limits stay in road_mph.
    No curve is invented between separate roads, and straight wide roads keep their original target."""
    target = min(limit, NARROW_MS) if not oneway and width < NARROW_M else limit
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
    urban arterial is all lanes."""
    if spec.urban and not spec.oneway and spec.cls in roads.URBAN_ARTERIAL:
        return float(spec.width)
    w = roads.STYLE.get(spec.cls, roads.STYLE["unknown"])[0]
    if spec.oneway and spec.cls in roads.ONEWAY_STYLE:
        w = roads.ONEWAY_STYLE[spec.cls]
    if roads.rural_motorway(spec):      # two lanes between the shoulders (a ramp: one, roads.motorway_shoulder)
        w = min(roads.MOTORWAY_RURAL_M, spec.width - 2 * roads.motorway_shoulder(spec))
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
    """The deck height at (x, y) of feature fid's bridge parts (decks: fid -> [(xy (k, 2), z (k,))]), or None
    when the point is not on one (more than 1.5 m off its line)."""
    best = None
    for xy, z in decks.get(fid, ()):
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


def build(frame, feats_list, elev, centre, urban_fn=None, window=WINDOW_M, decks=None, widths=None, left_hand=False,
          mph=True):
    """{"nodes": {name: [x, y, z, radius]}, "edges": [[in, out, oneway, lanes_out, lanes_in, speed m/s,
    drivability, radius]]} of the drivable roads in the square of half size `window` around centre (x, y) of
    the anchor frame. feats_list: the "roads" mapgeom.Features of the cells around (duplicates across cells
    are taken once). elev(lat, lon) -> metres. decks: fid -> [(xy, z)] of the bridge parts (roads.BridgeProfiles):
    nodes on a bridge take its deck height (else traffic would spawn on the ground under it), and roads that are
    bridge all along are in the graph (road_specs leaves them to the bridge builder). widths: the road tiles'
    measured street widths (roadwidth.py), so the AI's lanes are on the carriageway the road mesh draws."""
    cx, cy = centre
    nodes, edges, edge_fids, seen = {}, [], [], set()
    carriage = []           # (half width, centreline (k, 2)) of every road and bridge here (the signal heads keep off them)
    prop_arms = {}          # connector name -> [(fid, spec, unit vector away, point, end, length)] (the props' view)
    ring_cands = []         # the roads the road tiles draw (fid, spec, pts, None): roundabout rings (islands.ring_fids)
    approaches = {}         # (connector, fid, end) -> (graph node next to it, traffic comes in)
    pending = {}            # name -> (x, y) of nodes whose z is still to sample
    node_fid = {}           # name -> the feature it was made on (its deck, if any)
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
            if (p[:, 0].max() < cx - window - 200 or p[:, 0].min() > cx + window + 200 or
                    p[:, 1].max() < cy - window - 200 or p[:, 1].min() > cy + window + 200):
                continue
            if rspecs[i] is not None:
                ring_cands.append((fid, rspecs[i], np.column_stack([p, np.zeros(len(p))]), None))
            seg = np.hypot(*np.diff(p, axis=0).T)
            s = np.concatenate([[0.0], np.cumsum(seg)])
            L = float(s[-1])
            if L < 1.0:
                continue
            carriage.append((spec.width / 2, p))     # (the signal heads keep off every carriageway, decks too)
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
            tunnels = [(a_, b_) for a_, b_, k_ in roads.level_ranges(feats, i) if k_ == "tunnel"]
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
            names = [marks[v] for v in full]
            # this road's edge attributes
            mid = 0.5
            driv = DRIVE[spec.cls][0]
            direction = 0
            if ow is not None:
                for r in ow.rows(i):
                    if ow.start[r] <= mid <= ow.end[r]:
                        direction = int(ow.value[r])
            if direction == 0 and spec.oneway and spec.dir:     # a divided road's carriageway (roads.dual_carriageways)
                direction = int(spec.dir)
            lo, li = _lanes(w, direction != 0, spec.lanes)
            profile = _speed_profile(xy, w, direction != 0)
            # this road's approaches at its two ends (the graph node next to the junction, whether traffic comes in)
            for end in (0, -1):
                j, nb = (0, 1) if end == 0 else (len(names) - 1, len(names) - 2)
                if not names[j].startswith("c") or names[j] == names[nb]:
                    continue
                incoming = direction == 0 or (direction > 0) == (end == -1)   # unless one-way out of it
                approaches[(names[j], fid, end)] = (names[nb], incoming)
            for j in range(len(names) - 1):
                a, b = names[j], names[j + 1]
                if a == b:
                    continue
                if tunnels and any(t0 <= (full[j] + full[j + 1]) / (2 * L) <= t1 for t0, t1 in tunnels):
                    continue                # no ground over a tunnel: its stretch is left out
                if direction < 0:
                    a, b = b, a
                pa, pb = xy[j] if direction >= 0 else xy[j + 1], xy[j + 1] if direction >= 0 else xy[j]
                for nm, q in ((a, pa), (b, pb)):
                    if nm not in nodes and nm not in pending:
                        pending[nm] = (float(q[0]), float(q[1]))
                        node_fid[nm] = fid
                legal = road_mph(feats, i, spec, mph, at=(full[j] + full[j + 1]) / (2 * L)) * MPH
                target = min(legal, float(profile[j]), float(profile[j + 1]))
                edges.append([a, b, 1 if direction else 0, lo, li, round(target, 2), driv, round(w / 2, 2)])
                edge_fids.append(fid)
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
        Z = np.asarray(elev(la, lo), np.float64).copy()
        if decks:
            for k, nm in enumerate(used):
                dz = _deck_z(decks, node_fid.get(nm), X[k], Y[k])
                if dz is not None:
                    Z[k] = dz
        radius = {}
        for e in edges:
            for nm in e[:2]:
                radius[nm] = max(radius.get(nm, 0.0), e[7])
        for nm, x, y, z in zip(used, X, Y, Z):
            nodes[nm] = [round(float(x), 2), round(float(y), 2), round(float(z), 2), radius[nm]]
    signals = _signals(prop_arms, approaches, nodes, left_hand, set(ring), carriage)
    # one edge per node pair (two segments joining the same connectors: keep the first)
    uniq, pairs = [], set()
    for e in edges:
        k = (min(e[0], e[1]), max(e[0], e[1]))
        if k not in pairs:
            pairs.add(k)
            uniq.append(e)
    return {"nodes": nodes, "edges": uniq, "signals": signals}


STOP_BACK_M = 2.0           # the stop point: this far before the junction's widest road edge
# An approach this short is a link inside one intersection (offset streets split it into connectors a few metres
# apart: Fells Point): no stop or signal on it. A car that had stopped at the first node met the next one's stop
# line behind it and waited forever (in game 2026-10-14, a queue of four at an all-way stop).
SHORT_APPROACH_M = 15.0


def _signals(arms, approaches, nodes, left_hand=False, ring=None, lines=()):
    """[{"j": junction node, "kind": "signal" | "stop", "app": [[approach node, x, y, z, dir x, dir y, phase, prop x,
    prop y, prop z, prop yaw]]}] of the urban junctions furniture.junction_controls controls, decided on the arms
    the road tiles' props see (arms) and built for the graph's approaches into them (approaches): per incoming
    approach its stop point on the kerb-side half of the lane (where the AI's closest-edge search finds that
    approach), the travel direction, the phase (0 / 1, by the approach's axis) and the head's prop transform."""
    import furniture
    out = []
    # furniture's arm tuples (fid, spec, u, point, at its start, length): the same decision as the road tiles' props
    farms = {name: [(x[0], x[1], x[2], x[3], x[4] == 0, x[5] if len(x) > 5 else math.inf) for x in a]
             for name, a in arms.items()}
    link_ok = furniture._link_check(farms)
    for name, a in arms.items():
        if name not in nodes or len(a) < 3 or not all(x[1].urban for x in a):
            continue
        put = [(arm, what) for arm, what in furniture.junction_controls(farms[name], None, link_ok, ring=ring)
               if what != furniture.G_YIELD]    # give-way uses edge priority; a stop controller would force a stop
        if not put:
            continue
        kind = "signal" if put[0][1] == furniture.G_SIGNAL else "stop"
        axis = put[0][0][2]
        wide = max(x[1].width for x in a) / 2
        jx, jy, jz = nodes[name][:3]
        app = []
        for arm, _ in put:
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
            # the head's prop where furniture.junction_props stands it (the switchable stock boom replaces it); none
            # where no spot is clear of the carriageways (no prop there either)
            pp = furniture.signal_spot(np.array([jx, jy]), u, right, wide, spec.width / 2, lines)
            head = [None] * 4 if pp is None else [round(float(pp[0]), 2), round(float(pp[1]), 2), round(jz + 0.15, 2),
                                                  round(furniture._yaw(right[0], right[1]), 1)]
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
             "BM", "MS", "PN"}
TURN_ON_RED = {"US", "CA"}
# speed limits posted in mph (the rest: km/h); the US territories, the UK and its crown dependencies, Liberia, Myanmar
MPH_COUNTRIES = {"US", "PR", "VI", "GU", "AS", "MP", "UM", "GB", "IM", "JE", "GG", "LR", "MM"}
RED_YELLOW = {"GB", "IE", "DE", "AT", "CH", "LI", "NL", "BE", "LU", "DK", "NO", "SE", "FI", "IS", "PL", "CZ", "SK",
              "HU", "SI", "HR", "BA", "RS", "ME", "MK", "AL", "RO", "BG", "LT", "LV", "EE", "UA", "BY", "RU", "IL"}


def road_rules(country):
    """{"rightHandDrive" (the game's name: true = driving on the LEFT), "turnOnRed", "redYellow"} of a country
    (US rules for an unknown one)."""
    c = (country or "US").upper()
    return {"rightHandDrive": c in LEFT_HAND, "turnOnRed": c in TURN_ON_RED, "redYellow": c in RED_YELLOW,
            "country": c, "mph": c in MPH_COUNTRIES}


def country_near(db, lat, lon):
    """The country of the airport nearest (lat, lon) in airports.AirportDB db, or None."""
    if db is not None and getattr(db, "runways", None):
        import airports
        d = airports._hav(lat, lon, db._mid[:, 0], db._mid[:, 1])
        rw = db.runways[int(np.argmin(d))]
        ap = db.airports.get(rw.airport_ident)
        c = getattr(ap, "country", None) or getattr(rw, "country", None) or None
        if c:
            return c
    if lat is not None and lon is not None:
        if 6.0 <= float(lat) <= 37.5 and 68.0 <= float(lon) <= 97.5:
            return "IN"
    return None


REBUILD_M = 400.0           # a new graph once the player is this far from the last one's centre


def write(out_dir, vfs_dir, aid, serial, graph):
    """The graph as navgraph_<aid>_<serial>.json in the session folder; returns its vfs path."""
    import json
    import os
    name = f"navgraph_{aid}_{serial}.json"
    path = os.path.join(out_dir, name)
    tmp = staging.tmp_for(path)
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
        self.pool = None        # one thread of its own (vegetation.Tracker: not behind other trackers' builds)

    def close(self):
        if self.pool is not None:
            self.pool.shutdown(wait=False, cancel_futures=True)

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
            from concurrent.futures import ThreadPoolExecutor
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
        self.aid, self.centre = aid, centre
        path, n, m = res
        conn.send({"type": "navgraph", "anchorId": aid, "path": path, "cx": round(centre[0], 1),
                   "cy": round(centre[1], 1), "nodes": n, "edges": m, "serial": serial})
