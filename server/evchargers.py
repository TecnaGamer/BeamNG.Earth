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
from dataclasses import dataclass

import numpy as np

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


def _charging(category, taxonomy):
    return "charging" in str(category or "") or "charging" in str(taxonomy or "")


def charger_pins(frame, layers):
    """[(id, x, y, name, brand)] in frame of the open EV chargers among places layers (mapgeom.Features, one per map
    cell; a place in two cells taken once): category or taxonomy ev_charging_station, or a gas listing named as a
    charger (Shell Recharge, BP Pulse: gasstations._is_ev)."""
    out, seen = [], set()
    for f in layers:
        if f is None or len(f) == 0:
            continue
        a = f.attrs

        def col(k, i):
            v = a[k][i] if k in a else None
            return "" if v is None else str(v)
        xy = None
        for i in range(len(f)):
            fid = str(f.ids[i])
            if fid in seen or col("operating_status", i) in gs.CLOSED:
                continue
            cat, tax, name, brand = col("basic_category", i), col("taxonomy", i), col("name", i), col("brand_name", i)
            gas = cat in gs.GAS_CATEGORIES or tax in gs.GAS_CATEGORIES
            if not (_charging(cat, tax) or (gas and (gs._is_ev(name) or gs._is_ev(brand)))):
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
AREA_W_M = 2.4              # a stall's charge area across (its depth along the stall: STALL_M)
ENERGY = ("electricEnergy",)


def count_of(ch):
    """Chargers wanted at a site: ROW_TESLA for Tesla's, 2 per record (ROW_BIG..ROW_MAX) for a big site, else
    ROW_SMALL."""
    if ch.tesla:
        return ROW_TESLA
    if ch.big:
        return min(ROW_MAX, max(ROW_BIG, 2 * ch.records))
    return ROW_SMALL


def reach_of(ch):
    """How far from its position a site's chargers and stalls can reach (the road tiles that lay it out)."""
    return AISLE_REACH_M + AISLE_HALF_M + STALL_M + CHARGER_BACK_M + ROW_MAX * CHARGER_GAP_M / 2


def _nearest(lines, p):
    """(point (2,), unit direction (2,), distance) of the nearest point on polylines lines to p, or None."""
    best = None
    for line in lines:
        a, b = line[:-1], line[1:]
        d = b - a
        L2 = np.maximum((d * d).sum(1), 1e-12)
        t = np.clip(((p - a) * d).sum(1) / L2, 0.0, 1.0)
        q = a + d * t[:, None]
        dist = np.hypot(*(q - p).T)
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
        if np.dot(p - q, nrm) < 0:
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
        if np.dot(p - q, nrm) < 0:
            nrm = -nrm
        kerb_tries.append(("kerb", (q, u, nrm, half)))
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


def _kerb_clear(pts, areas, blds, roads):
    """Kerbside chargers clear of buildings and off every public road's carriageway; their charge areas (at the road's
    edge) out of buildings."""
    for line, half in roads:
        if (gs._seg_dist(pts, line[:-1], line[1:]) < half + KERB_OFF_M - 0.05).any():
            return False
    return gs._clear(pts, blds, []) and gs._clear(areas, blds, [])


def layout(ch, blds, roads, service=(), style="default"):
    """A charger site (gasstations.Site): count_of(ch) chargers in a straight row CHARGER_GAP_M apart, each facing its
    stall STALL_M deep, along the nearest service line within AISLE_REACH_M (the stalls on the aisle's side) or at the
    site's position facing the nearest public road; or, by a public road, on its kerb KERB_GAP_M apart facing it, the
    cars parked at its edge (_kerb_row). The row is shortened to fit, every charger and stall clear of buildings and
    public roads (gasstations._clear; a kerbside charge area at the road's edge: _kerb_clear). One asphalt pad round a
    row (the user, 2026-10-29: "Ground paint minimum"), none at a kerb (road and sidewalk are paved). None when not
    even one charger fits (a charger in a parking garage)."""
    shapes = CHARGER_SHAPES.get(style, CHARGER_SHAPES["default"])
    shape = shapes[0] if ch.big else shapes[1]
    tries = _placements(ch, roads, service)
    for n in range(count_of(ch), 0, -1):
        for kind, data in tries:
            if kind == "kerb":
                q, u, nrm, half = data
                pts, stalls = _kerb_row(q, u, nrm, half, n)
                if not _kerb_clear(pts, stalls, blds, roads):
                    continue
                front, area_yaw, pad = -nrm, gs._yaw(u), []
            else:
                centre, u, f = data
                pts, stalls, ring = _row(centre, u, f, n)
                if not gs._clear(np.vstack([pts, stalls]), blds, roads):
                    continue
                front, area_yaw, pad = f, gs._yaw(f), [ring]
            yaw = gs._yaw(front)
            props = [dict(shape=shape, x=float(x), y=float(y), dz=0.0, yaw=(yaw + CHARGER_DYAW) % 360.0,
                          scale=[1.0, 1.0, 1.0], collide=True) for x, y in pts]
            areas = [dict(x=float(x), y=float(y), dz=gs.AREA_DZ, yaw=area_yaw % 360.0, half=[STALL_M, AREA_W_M, 2.75],
                          icon=gs.ICON_DZ) for x, y in stalls]
            st = gs.Station(ch.id, ch.x, ch.y, ch.name or "EV charger", ch.brand, (ch.id,), True, True)
            return gs.Site(st, props, areas, (), shop_id="ev:" + ch.id, pad=pad)
    return None
