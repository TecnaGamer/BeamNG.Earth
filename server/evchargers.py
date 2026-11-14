"""EV chargers with charging (the user, 2026-10-29: "real life ev stations mapped like the gas stations are but using
beam assets to have real recharging in the game"; design: .claude/superpowers/specs/2026-10-29-ev-chargers-design.md).

Chargers come from Overture's places (basic_category / taxonomy ev_charging_station, and the chargers it files as gas
stations: gasstations._is_ev) and from OpenStreetMap's amenity=charging_station (osmfuel.py, the same pass as its fuel
stations). Neither source says how fast a charger is or how many stalls it has, so a site's size comes from what is
known: a known fast-charging network, or two or more records together, is a big site (a row, as a Tesla Supercharger:
the user, 2026-10-29, "Tesla Supercharge"); anything else is small. Every charger is placed (the user, 2026-10-29:
"i would like as many chanrgers as possible"). Pure and deterministic, as gasstations.py: every tile, anchor and BeamMP
peer places the same site.
"""
import logging
import math
import re
import dataclasses
from dataclasses import dataclass

import numpy as np
import dnp

import gasstations as gs

log = logging.getLogger("earthd.evchargers")

# fast-charging networks: one of their records is a big site
NETWORK_WORDS = re.compile(r"\b(tesla|supercharger|electrify america|ionity|evgo|fastned|shell recharge|bp pulse|"
                           r"allego|enbw|aral pulse|ewe go|electra|zunder|atlante|tesla destination|"
                           r"ultra-?rapid|hpc|schnelllade)\b", re.IGNORECASE)
TESLA_WORDS = re.compile(r"\b(tesla|supercharger)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Charger:
    id: str
    x: float            # anchor frame, m
    y: float
    name: str
    brand: str
    records: int        # Overture's and OSM's records merged into it
    big: bool           # a known fast network, or 2+ records: a row of chargers
    tesla: bool         # Tesla's own (a Supercharger's 8)
    cap: int = None     # OSM's capacity=* of its charging station (the weekly shards, with_shard), when tagged
    points: tuple = ()  # (x, y) of its mapped man_made=charge_point nodes (with_shard)


def _charging(category, taxonomy):
    return "charging" in str(category or "") or "charging" in str(taxonomy or "")


def _ev_candidates(f):
    """[(feature index, name, brand)] of a places layer's open EV chargers, made once per layer (kept on it, and in this
    process for another copy of the layer, as gasstations._candidates: a city cell has ~47k places, 2026-11-02)."""
    got = f.__dict__.get("_ev_candidates")
    if got is not None:
        return got
    cid = f.__dict__.get("_bnge_cell")
    import roads
    mk = (cid, "ev_candidates") if cid is not None else None
    if mk is not None:
        with roads._FACTS_LOCK:
            got = roads._FACTS_MEM.get(mk)
        if got is not None:
            f.__dict__["_ev_candidates"] = got
            return got
    a = f.attrs

    def col(k, i):
        v = a[k][i] if k in a else None
        return "" if v is None else str(v)
    out = []
    for i in range(len(f)):
        if col("operating_status", i) in gs.CLOSED:
            continue
        cat, tax, name, brand = col("basic_category", i), col("taxonomy", i), col("name", i), col("brand_name", i)
        gas = cat in gs.GAS_CATEGORIES or tax in gs.GAS_CATEGORIES
        if _charging(cat, tax) or (gas and (gs._is_ev(name) or gs._is_ev(brand))):
            out.append((i, name, brand))
    f.__dict__["_ev_candidates"] = out
    if mk is not None:
        with roads._FACTS_LOCK:
            roads._FACTS_MEM[mk] = out
            while len(roads._FACTS_MEM) > roads.FACTS_MEM_KEPT:
                roads._FACTS_MEM.popitem(last=False)
    return out


def charger_pins(frame, layers):
    """[(id, x, y, name, brand)] in frame of the open EV chargers among places layers (mapgeom.Features, one per map
    cell; a place in two cells taken once): category or taxonomy ev_charging_station, or a gas listing named as a
    charger (Shell Recharge, BP Pulse: gasstations._is_ev). The candidates found once per layer (_ev_candidates)."""
    out, seen = [], set()
    for f in layers:
        if f is None or len(f) == 0:
            continue
        xy = None
        for i, name, brand in _ev_candidates(f):
            fid = str(f.ids[i])
            if fid in seen:
                continue
            seen.add(fid)
            if xy is None:
                xy = f.xy_in(frame)
            k = f.coord_off[f.ring_off[f.part_off[i]]]
            out.append((fid, float(xy[k, 0]), float(xy[k, 1]), name, brand))
    return out


def merge(pins, osm, merge_m=gs.MERGE_M):
    """The charger sites of Overture's pins [(id, x, y, name, brand)] and OSM's points [{"id", "x", "y", "name",
    "brand"}] (gasstations._osm_points, amenity charging_station): each record joins the first site (in id order,
    Overture's first) whose first record is within merge_m, so a street of single chargers does not chain into one
    site. A site is at its first record, named by its first named one."""
    recs = sorted(((str(p[0]), 0, p[1], p[2], p[3] or "", p[4] or "") for p in pins), key=lambda r: r[0])
    recs += sorted((("osm%x" % int(o["id"]), 1, o["x"], o["y"], o.get("name") or "", o.get("brand") or "")
                    for o in osm), key=lambda r: r[0])
    sites = []
    for r in recs:
        for s in sites:
            if math.hypot(r[2] - s[0][2], r[3] - s[0][3]) <= merge_m:
                s.append(r)
                break
        else:
            sites.append([r])
    out = []
    for s in sites:
        name = next((r[4] for r in s if r[4]), "")
        brand = next((r[5] for r in s if r[5]), "")
        words = " ".join(f"{r[4]} {r[5]}" for r in s)
        out.append(Charger("ev" + s[0][0], float(s[0][2]), float(s[0][3]), name, brand, len(s),
                           len(s) >= 2 or bool(NETWORK_WORDS.search(words)), bool(TESLA_WORDS.search(words))))
    return out


# --- a site's layout ------------------------------------------------------------------------------------------------
# the stock models (docs/research/2026-10-29-ev-chargers.md): (a big site's, a small site's) per gasstations.style_of
_WCA = "/levels/west_coast_usa/art/shapes/buildings/"
CHARGER_SHAPES = {
    "default": (_WCA + "s_fuel_charger_car_2020.dae", _WCA + "s_fuel_charger_car_2022.dae"),
    "us_east": ("/levels/east_coast_usa/art/shapes/buildings/eca_charging_station.DAE",) * 2,
    "europe": ("/levels/italy/art/shapes/buildings/charging_station_a.DAE",
               "/levels/italy/art/shapes/buildings/charging_station_b.DAE"),
}
# the mod's yaw (gasstations.lua yawQuat) turns a charger's front to _yaw(front) + this (in game, 2026-10-29: yaw 0
# faced -y, yaw 90 faced +x)
CHARGER_DYAW = 90.0
CHARGER_GAP_M = 3.0         # chargers along a row (a stall's width)
STALL_M = 5.5               # a stall's depth, in front of its charger
CHARGER_BACK_M = 0.5        # the charger behind its stall
AISLE_HALF_M = 3.0          # a parking aisle's half width: its stalls start there
AISLE_REACH_M = 30.0        # a big site's row along an aisle this near its position
ROW_BIG = 4
ROW_TESLA = 8               # (a Supercharger's usual 8)
ROW_MAX = 8
ROW_SMALL = 2
AREA_W_M = 2.4              # a stall's charge area across (its depth along the stall: STALL_M); the area's half
                            # extents are what it gets (setScale: gasStationMarker.lua passes the scale to
                            # overlapsOBB_OBB as half-axes; PR #716 review, 2026-11-03)
ENERGY = ("electricEnergy",)


def count_of(ch):
    """Chargers wanted at a site: OSM's capacity (at most CAP_MAX) when its station has one; else ROW_TESLA for
    Tesla's, 2 per record (ROW_BIG..ROW_MAX) for a big site, else ROW_SMALL."""
    if ch.cap is not None and ch.cap >= 1:
        return min(int(ch.cap), CAP_MAX)
    if ch.tesla:
        return ROW_TESLA
    if ch.big:
        return min(ROW_MAX, max(ROW_BIG, 2 * ch.records))
    return ROW_SMALL


def reach_of(ch):
    """How far from its position a site's chargers and stalls can reach (the road tiles that lay it out)."""
    r = AISLE_REACH_M + AISLE_HALF_M + STALL_M + CHARGER_BACK_M + max(ROW_MAX, count_of(ch)) * CHARGER_GAP_M / 2
    far = max((math.hypot(px - ch.x, py - ch.y) for px, py in ch.points), default=0.0)
    return max(r, far + STALL_M + CHARGER_BACK_M)


# --- the weekly OpenStreetMap station shards (railtags.StationTags; Bauer33333 on #716, 2026-11-04: "Almost 3/4 of
# `amenity=charging_station` on OSM got a `capacity=*` tag, same for `man_made=charge_point`")
# (the user in game, 2026-11-09, Barstow's Supercharger as a row of 12: "some of them are sliding off a hill ... it
# look s silly"; "Do that or just shift them") a row stays on level ground: it slides along its aisle by whole chargers
# (ROW_SLIDE) to where every charger and stall is within FLAT_M of the row's middle height, else is shortened; a mapped
# charge point on a bank is dropped
FLAT_M = 0.5
ROW_SLIDE = (0, -1, 1, -2, 2, -3, 3, -4, 4)
CAP_MAX = 12                # chargers a site gets from its capacity at most (a big Supercharger has 12-40 stalls)
POINTS_MAX = 16             # mapped charge points a site stands chargers at, at most


def with_shard(chargers, stations, points, frame):
    """The sites with OSM's capacity of the nearest shard charging station within gasstations.MERGE_M, and the charge
    points (lat, lon) each nearest to it (to its matched station's position, else its own) within MERGE_M: a
    neighbour's points stay the neighbour's. stations: railtags.StationTags.stations_in's; frame: the sites'."""
    if not chargers:
        return chargers
    evs = [s for s in stations or () if s.get("kind") == "ev"]
    sxy = np.array([frame.geodetic_to_enu(s["lat"], s["lon"], 0.0)[:2] for s in evs], np.float64).reshape(-1, 2)
    caps, anchors = [], []
    for ch in chargers:
        cap, at = None, (ch.x, ch.y)
        if len(sxy):
            d = dnp.hypot(sxy[:, 0] - ch.x, sxy[:, 1] - ch.y)
            k = int(np.argmin(d))
            if d[k] <= gs.MERGE_M:
                cap, at = evs[k].get("cap"), (float(sxy[k, 0]), float(sxy[k, 1]))
        caps.append(cap)
        anchors.append(at)
    own = [[] for _ in chargers]
    A = np.array(anchors, np.float64)
    for la, lo in points or ():
        x, y, _ = frame.geodetic_to_enu(la, lo, 0.0)
        d = dnp.hypot(A[:, 0] - float(x), A[:, 1] - float(y))
        k = int(np.argmin(d))
        if d[k] <= gs.MERGE_M:
            own[k].append((float(x), float(y)))
    return [dataclasses.replace(ch, cap=cap, points=tuple(sorted(pts))[:POINTS_MAX])
            for ch, cap, pts in zip(chargers, caps, own)]


def _nearest(lines, p):
    """(point (2,), unit direction (2,), distance) of the nearest point on polylines lines to p, or None."""
    best = None
    for line in lines:
        a, b = line[:-1], line[1:]
        d = b - a
        L2 = np.maximum((d * d).sum(1), 1e-12)
        t = np.clip(((p - a) * d).sum(1) / L2, 0.0, 1.0)
        q = a + d * t[:, None]
        dist = dnp.hypot(*(q - p).T)
        k = int(np.argmin(dist))
        if best is None or dist[k] < best[2]:
            best = (q[k], d[k] / np.sqrt(L2[k]), float(dist[k]))
    return best


def _row(centre, u, f, n):
    """n chargers centred on centre along u, their fronts to f: (charger points, stall centres, pad ring)."""
    offs = (np.arange(n) - (n - 1) / 2.0) * CHARGER_GAP_M
    ch = centre[None] + offs[:, None] * u[None]
    stalls = ch + f[None] * (CHARGER_BACK_M + STALL_M / 2.0)
    half = n * CHARGER_GAP_M / 2.0
    back, front = centre - f * CHARGER_BACK_M * 2, centre + f * (CHARGER_BACK_M + STALL_M)
    pad = np.array([back - u * half, back + u * half, front + u * half, front - u * half, back - u * half])
    return ch, stalls, pad


# (the 8 sample places, 2026-10-29: 191 of 267 sites skipped, most of them street chargers 0-3 m from the road's
# edge, median 1-1.6 m in Utrecht, Berlin and Oslo) a charger by a public road stands on its kerb facing it, its car
# parked at the road's edge, as a street charger is
KERB_REACH_M = 10.0         # a charger this near a public road's edge is a kerbside one
KERB_OFF_M = 0.8            # its charger this far behind the carriageway's edge (on the kerb)
KERB_LANE_M = 1.2           # its charge area's centre this far inside the edge (a car parked along it)
KERB_GAP_M = 6.0            # kerbside chargers along the road (a parallel-parked car's length)
# (the user, 2026-10-30: "do the driveway fix now") a driveway's mouth stays clear: no kerbside charger within
# DRIVE_CLEAR_M of a service road, no car charging across one (its area's half length + DRIVE_HALF_M); the row slides
# along the kerb by whole spaces (KERB_SLIDE) to find room
DRIVE_HALF_M = 3.0          # a driveway's half width
DRIVE_CLEAR_M = 3.5
KERB_SLIDE = (0, 1, -1, 2, -2)


def _placements(ch, roads, service):
    """[(kind, data)] to try, in order. "row" (centre, u, f): a row along the nearest aisle (the stalls toward it) or at
    the position facing the nearest public road, then away from it. "kerb" (q, u, n, half): on the nearest public
    road's kerb when its edge is within KERB_REACH_M. A big site tries the aisle, then the kerb; a small one the kerb
    first."""
    p = np.array([ch.x, ch.y], np.float64)
    aisle_tries = []
    aisle = _nearest(service, p)
    if aisle is not None and aisle[2] <= AISLE_REACH_M:
        q, u, dist = aisle
        nrm = np.array([-u[1], u[0]])
        if dnp.dot(p - q, nrm) < 0:
            nrm = -nrm
        centre = q + nrm * (AISLE_HALF_M + STALL_M + CHARGER_BACK_M)
        aisle_tries.append(("row", (centre, u, -nrm)))     # (the stalls toward the aisle, the chargers behind them)
    kerb_tries = []
    best = None
    for line, half in roads:
        got = _nearest([line], p)
        if got is not None and (best is None or got[2] - half < best[0][2] - best[1]):
            best = (got, half)
    if best is not None and best[0][2] - best[1] <= KERB_REACH_M:
        (q, u, dist), half = best
        nrm = np.array([-u[1], u[0]])
        if dnp.dot(p - q, nrm) < 0:
            nrm = -nrm
        kerb_tries += [("kerb", (q + u * (k * KERB_GAP_M), u, nrm, half)) for k in KERB_SLIDE]
    road = _nearest([r[0] for r in roads], p)
    f = (road[0] - p) / max(road[2], 1e-9) if road is not None and road[2] > 1e-6 else np.array([0.0, 1.0])
    u = np.array([-f[1], f[0]])
    at_pin = [("row", (p, u, f)), ("row", (p, u, -f))]
    if ch.big:
        return aisle_tries + kerb_tries + at_pin
    return kerb_tries + at_pin + aisle_tries


def _kerb_row(q, u, nrm, half, n):
    """n chargers on a road's kerb centred on q along u (nrm: from the road to the kerb), facing the road: (charger
    points, charge area centres)."""
    offs = (np.arange(n) - (n - 1) / 2.0) * KERB_GAP_M
    along = q[None] + offs[:, None] * u[None]
    return along + nrm[None] * (half + KERB_OFF_M), along + nrm[None] * (half - KERB_LANE_M)


def _kerb_clear(pts, areas, blds, roads, service=()):
    """Kerbside chargers clear of buildings and off every public road's carriageway; their charge areas (at the road's
    edge) out of buildings; both clear of every driveway's mouth (service lines: DRIVE_CLEAR_M, DRIVE_HALF_M)."""
    for line, half in roads:
        if (gs._seg_dist(pts, line[:-1], line[1:]) < half + KERB_OFF_M - 0.05).any():
            return False
    for line in service:
        if ((gs._seg_dist(pts, line[:-1], line[1:]) < DRIVE_CLEAR_M).any() or
                (gs._seg_dist(areas, line[:-1], line[1:]) < STALL_M / 2 + DRIVE_HALF_M).any()):
            return False
    return gs._clear(pts, blds, []) and gs._clear(areas, blds, [])


def _level(pts, height):
    """Whether every point's ground (height(xs, ys)) is within FLAT_M of their middle (median) height."""
    h = np.asarray(height(pts[:, 0], pts[:, 1]), np.float64).ravel()
    return bool(np.all(np.abs(h - np.median(h)) <= FLAT_M))


def _at_points(ch, blds, roads, service, shape, height=None):
    """A site with a charger at each mapped charge point, facing the nearest drive line within AISLE_REACH_M (else the
    nearest public road), its stall in front; a point whose charger or stall is in a building or on a road dropped.
    None when fewer than 2 stand (today's placements then). No pad: a mapped site's ground is there already."""
    props, areas, keep = [], [], []
    for px, py in ch.points:
        p = np.array([px, py], np.float64)
        near = _nearest(service, p)
        if near is None or near[2] > AISLE_REACH_M:
            near = _nearest([r[0] for r in roads], p)
        f = (near[0] - p) / near[2] if near is not None and near[2] > 1e-6 else np.array([0.0, 1.0])
        stall = p + f * (CHARGER_BACK_M + STALL_M / 2.0)
        if not gs._clear(np.array([p, stall]), blds, roads):
            continue
        keep.append((p, stall))
        yaw = gs._yaw(f)
        props.append(dict(shape=shape, x=float(px), y=float(py), dz=0.0, yaw=(yaw + CHARGER_DYAW) % 360.0,
                          scale=[1.0, 1.0, 1.0], collide=True))
        areas.append(dict(x=float(stall[0]), y=float(stall[1]), dz=gs.AREA_DZ, yaw=yaw % 360.0,
                          half=[STALL_M / 2, AREA_W_M / 2, 2.75], icon=gs.ICON_DZ))
    if height is not None and keep:             # (a point on a bank: off the middle height of the site's points)
        hp = np.asarray(height(np.array([k[0][0] for k in keep]), np.array([k[0][1] for k in keep])), np.float64)
        hs = np.asarray(height(np.array([k[1][0] for k in keep]), np.array([k[1][1] for k in keep])), np.float64)
        med = float(np.median(np.concatenate([hp.ravel(), hs.ravel()])))
        ok = (np.abs(hp.ravel() - med) <= FLAT_M) & (np.abs(hs.ravel() - med) <= FLAT_M)
        props = [p for p, k in zip(props, ok) if k]
        areas = [a for a, k in zip(areas, ok) if k]
    if len(props) < 2:
        return None
    st = gs.Station(ch.id, ch.x, ch.y, ch.name or "EV charger", ch.brand, (ch.id,), True, True)
    return gs.Site(st, props, areas, (), shop_id="ev:" + ch.id, pad=[])


def layout(ch, blds, roads, service=(), style="default", height=None):
    """A charger site (gasstations.Site): count_of(ch) chargers in a straight row CHARGER_GAP_M apart, each facing its
    stall STALL_M deep, along the nearest service line within AISLE_REACH_M (the stalls on the aisle's side) or at the
    site's position facing the nearest public road; or, by a public road, on its kerb KERB_GAP_M apart facing it, the
    cars parked at its edge (_kerb_row). The row is shortened to fit, every charger and stall clear of buildings and
    public roads (gasstations._clear; a kerbside charge area at the road's edge: _kerb_clear). One asphalt pad round a
    row (the user, 2026-10-29: "Ground paint minimum"), none at a kerb (road and sidewalk are paved). None when not
    even one charger fits (a charger in a parking garage)."""
    shapes = CHARGER_SHAPES.get(style, CHARGER_SHAPES["default"])
    shape = shapes[0] if ch.big else shapes[1]
    if len(ch.points) >= 2:
        site = _at_points(ch, blds, roads, service, shape, height)
        if site is not None:
            return site
    tries = _placements(ch, roads, service)
    for n in range(count_of(ch), 0, -1):
        for kind, data in tries:
            if kind == "kerb":
                q, u, nrm, half = data
                pts, stalls = _kerb_row(q, u, nrm, half, n)
                if not _kerb_clear(pts, stalls, blds, roads, service):
                    continue
                front, area_yaw, pad = -nrm, gs._yaw(u), []
            else:
                centre, u, f = data
                for s in (ROW_SLIDE if height is not None else (0,)):     # (height(xs, ys): the ground, level rows)
                    pts, stalls, ring = _row(centre + u * (s * CHARGER_GAP_M), u, f, n)
                    both = np.vstack([pts, stalls])
                    if gs._clear(both, blds, roads) and (height is None or _level(both, height)):
                        break
                else:
                    continue
                front, area_yaw, pad = f, gs._yaw(f), [ring]
            yaw = gs._yaw(front)
            props = [dict(shape=shape, x=float(x), y=float(y), dz=0.0, yaw=(yaw + CHARGER_DYAW) % 360.0,
                          scale=[1.0, 1.0, 1.0], collide=True) for x, y in pts]
            areas = [dict(x=float(x), y=float(y), dz=gs.AREA_DZ, yaw=area_yaw % 360.0, half=[STALL_M / 2, AREA_W_M / 2, 2.75],
                          icon=gs.ICON_DZ) for x, y in stalls]
            st = gs.Station(ch.id, ch.x, ch.y, ch.name or "EV charger", ch.brand, (ch.id,), True, True)
            return gs.Site(st, props, areas, (), shop_id="ev:" + ch.id, pad=pad)
    return None
