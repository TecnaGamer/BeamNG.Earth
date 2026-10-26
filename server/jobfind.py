"""Garage-to-garage delivery jobs (the user, 2026-10-25: "we need jobs around the map. Garage to garage delivery ...";
the first job, the base the others reuse): a customer's car to take from a real garage near the player to another
real garage farther away.

Garages are Overture places whose category says cars are worked on or sold there (GARAGE_WORDS: repair shops, body
shops, dealers, tyre shops; not fuel, parking or washes). Pickups: the garages within PICK_M of the player, nearest
first. Drop-offs: for each job a target point DROP_KM away on a bearing (both from the job's seed), the garage nearest
it (the cells around the target only: no 30 km square of cells). Each garage's spot is on the public road beside it
(randomplace.road_spot), where the car is handed over.

Pay (the user: "fair but real ... damage cuts pay, time limits are very fair and early gets bonus, ... tolls and gas
... the company will pay you"): BASE_PAY + PER_MILE for the road distance (OSRM, else the straight line times DETOUR),
times the car's value factor, to the nearest $5. The time limit is the drive's duration times LIMIT_X plus LIMIT_ADD_S.
The game (jobs.lua) adds the early bonus, the damage and lateness cuts, and pays back the tolls and fuel of the job.
Deterministic for a seed: the same offers for the same place and seed.
"""
import hashlib
import logging
import math

import mapcells
import randomplace

log = logging.getLogger("earthd.jobfind")

GARAGE_WORDS = ("automotive", "auto_repair", "car_repair", "auto_body", "body_shop", "car_dealer", "auto_dealer",
                "used_car", "tire", "tyre", "oil_change", "auto_glass", "transmission", "mechanic", "garage",
                "auto_detailing", "auto_service", "car_service", "motor_vehicle_dealer", "truck_dealer")
NOT_GARAGE = ("gas", "fuel", "parking", "car_wash", "charging", "rental", "insurance", "school", "parts_store",
              "parts_and_supply")
PICK_M = 4000.0             # pickups: garages this close to the player
DROP_KM = (5.0, 8.0, 12.0, 16.0, 22.0)     # drop-off targets this far from the pickup
DROP_SEARCH_M = 3000.0      # the garage nearest the target within this
TRUCK_SEARCH_M = 1500.0     # (trucks: far targets, each search a first download of its cells: kept small)
SPOT_M = 250.0
JOBS = 5
DETOUR = 1.35               # road distance / straight line (no route)
AVG_MS = 15.6               # 35 mph: the duration without a route
BASE_PAY = 60.0
PER_MILE = 7.0
LIMIT_X, LIMIT_ADD_S = 1.6, 240.0
MI = 1609.344
M_LAT = 111320.0
# customer cars (stock vehicles; every one is in the game's content/vehicles), with a value factor for the pay
CARS = (("covet", "Ibishu Covet", 0.85), ("pessima", "Ibishu Pessima", 0.9), ("vivace", "Cherrier Vivace", 1.15),
        ("etk800", "ETK 800", 1.3), ("etki", "ETK I-Series", 1.1), ("etkc", "ETK K-Series", 1.45),
        ("bastion", "Bruckell Bastion", 1.35), ("legran", "Bruckell LeGran", 0.9), ("moonhawk", "Bruckell Moonhawk", 1.2),
        ("sunburst2", "Hirochi Sunburst", 1.05), ("bx", "Ibishu BX-Series", 1.1), ("midsize", "Gavril Grand Marshal",
        0.9), ("fullsize", "Gavril Grand Marshal", 0.9), ("lansdale", "Soliad Lansdale", 0.95),
        ("wendover", "Soliad Wendover", 1.0), ("barstow", "Gavril Barstow", 1.15), ("burnside", "Burnside Special",
        1.1), ("roamer", "Gavril Roamer", 1.0), ("pickup", "Gavril D-Series", 1.05), ("van", "Gavril H-Series", 1.0),
        ("bluebuck", "Bruckell Bluebuck", 1.1), ("scintilla", "Civetta Scintilla", 2.0), ("bolide", "Civetta Bolide",
        1.8), ("nine", "Autobello Nine", 1.25), ("miramar", "Ibishu Miramar", 0.95))
# Trucking (the user, 2026-10-25: "truck driving (different kinds for different trucks)"; "just spawn me in the truck
# and the trailer nearby"; "you can rent a truck and pay them a small fee each job"): freight between real warehouses,
# factories, distribution centres and big stores, farther apart; a rented semi (RENT a job, off the pay) and the
# trailer for the cargo.
DEPOT_WORDS = ("warehouse", "distribution", "logistics", "freight", "factory", "manufactur", "wholesale", "industrial",
               "lumber", "building_supply", "hardware_store", "home_improvement", "farm_supply", "agricultur", "grain",
               "quarry", "mining", "concrete", "steel", "storage_facility", "trucking", "shipping", "supermarket",
               "grocery", "department_store", "superstore")
NOT_DEPOT = ("self_storage", "parking", "gas", "fuel")
TRUCK_KM = (24.0, 35.0, 48.0, 65.0, 90.0)
TRUCK = "us_semi"
TRUCK_NAME = "Gavril T-Series semi"
# pay (the user, 2026-10-26: "make it realistic", then "a 1.3x on that"): a load's market rate, $2.50 a mile (DAT dry
# van contract, July 2026), at least TRUCK_MIN (short hauls pay a flat minimum: loading and unloading take hours),
# times the cargo's premium, times TRUCK_BOOST; fuel and tolls are paid back on top (jobs.lua)
TRUCK_PER_MILE = 2.50
TRUCK_MIN = 350.0
TRUCK_BOOST = 1.3
RENT = 45.0                 # $ the rental company takes per job
# cargo -> (trailer model, what the panel says, pay factor)
CARGO = (("dryvan", "Dry van: general freight", 1.0), ("tanker", "Tanker: fuel", 1.25),
         ("flatbed", "Flatbed: building materials", 1.15), ("log_trailer", "Log trailer: lumber", 1.15),
         ("hopper", "Hopper: gravel", 1.05), ("containerTrailer", "Container: imports", 1.1))
# (midsize is the Gavril Grand Marshal's smaller sibling in some versions; the name shown is only a label)


def _h(*parts):
    return int(hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12], 16)


def is_depot(category, taxonomy, status=None):
    if status and str(status) in ("closed", "permanently_closed", "temporarily_closed"):
        return False
    t = (str(category or "") + " " + str(taxonomy or "")).lower()
    if not t.strip() or any(w in t for w in NOT_DEPOT):
        return False
    return any(w in t for w in DEPOT_WORDS)


def is_garage(category, taxonomy, status=None):
    if status and str(status) in ("closed", "permanently_closed", "temporarily_closed"):
        return False
    t = (str(category or "") + " " + str(taxonomy or "")).lower()
    if not t.strip() or any(w in t for w in NOT_GARAGE):
        return False
    return any(w in t for w in GARAGE_WORDS)


def _box(lat, lon, half_m):
    k = max(math.cos(math.radians(lat)), 0.05)
    return lat - half_m / M_LAT, lon - half_m / (M_LAT * k), lat + half_m / M_LAT, lon + half_m / (M_LAT * k)


def dist_m(lat0, lon0, lat1, lon1):
    k = math.cos(math.radians((lat0 + lat1) / 2))
    return math.hypot((lat1 - lat0) * M_LAT, (lon1 - lon0) * M_LAT * k)


def garages(layers, test=None):
    """[(id, lat, lon, name)] of the garages (test: is_garage, or is_depot) among places layers (one per map cell),
    each once, by id."""
    test = test or is_garage
    out, seen = [], set()
    for f in layers:
        if f is None or not len(f):
            continue
        a = f.attrs

        def col(k, i):
            v = a[k][i] if k in a else None
            return "" if v is None else str(v)
        for i in range(len(f)):
            fid = str(f.ids[i])
            if fid in seen or not test(col("basic_category", i), col("taxonomy", i), col("operating_status", i)):
                continue
            seen.add(fid)
            k = f.coord_off[f.ring_off[f.part_off[i]]]
            lon, lat = f.lonlat[k]
            name = col("name", i).strip() or col("brand_name", i).strip() or "Garage"
            out.append((fid, float(lat), float(lon), name))
    out.sort(key=lambda g: g[0])
    return out


def _places(md, lat, lon, half):
    layers = []
    for key in mapcells.cells_for_bbox(*_box(lat, lon, half)):
        try:
            layers.append(md.cell(key, layers=("places",), kind="demand").layers.get("places"))
        except Exception as e:      # a cell that could not be fetched: the others still count
            log.debug("jobs: cell %s: %s: %s", key, type(e).__name__, e)
    return layers


# mileage bonus (the user, 2026-10-26: "if it has more miles it needs to pay more, base pay plus a nice mileage bonus.
# the lowest ones can stay how they are"): the shortest jobs pay as before, every mile past TRUCK_FREE_MI / CAR_FREE_MI
# adds TRUCK_MILE_BONUS / CAR_MILE_BONUS (times the cargo / car factor)
TRUCK_FREE_MI, TRUCK_MILE_BONUS = 15.0, 9.0
TRUCK_PAY_X = 2.0           # all of it doubled (the user: "we need trucking to be a cash cow")
TRUCK_FLOOR = 750.0         # no haul pays less (the user, 2026-10-26: "there should actually be no trip below 750")
CAR_FREE_MI, CAR_MILE_BONUS = 4.0, 6.0


def truck_pay(dist, value):
    """$ for a load hauled dist m with cargo premium value (to the nearest $5)."""
    mi = dist / MI
    base = max(TRUCK_FLOOR, TRUCK_MIN * TRUCK_BOOST, TRUCK_PER_MILE * mi * TRUCK_BOOST)
    bonus = TRUCK_MILE_BONUS * max(0.0, mi - TRUCK_FREE_MI)
    return float(5 * round((base + bonus) * value * TRUCK_PAY_X / 5.0))


def pay_for(dist, value):
    """$ for a delivery of dist m in a car of value factor value (to the nearest $5)."""
    mi = dist / MI
    return float(5 * round((BASE_PAY + PER_MILE * mi + CAR_MILE_BONUS * max(0.0, mi - CAR_FREE_MI)) * value / 5.0))


def time_limit(dur):
    return float(round(dur * LIMIT_X + LIMIT_ADD_S))


def offers(md, lat, lon, seed=0, route_fn=None, spot_fn=None, kind="car"):
    """Up to JOBS delivery offers from (lat, lon): [{"id", "car", "car_name", "pickup": {lat, lon, name}, "dropoff":
    {...}, "dist", "dur", "pay", "limit"}], nearest pickup first. route_fn(lat0, lon0, lat1, lon1) -> {dist, dur}
    (raises: the straight-line estimate); spot_fn(lat, lon) -> (lat, lon, ...) on the road beside, or None."""
    spot_fn = spot_fn or (lambda la, lo: randomplace.road_spot(md, la, lo, radius_m=SPOT_M))
    truck = kind == "truck"
    test, kms = (is_depot, TRUCK_KM) if truck else (is_garage, DROP_KM)
    near = [g for g in garages(_places(md, lat, lon, PICK_M), test) if dist_m(lat, lon, g[1], g[2]) <= PICK_M]
    near.sort(key=lambda g: (round(dist_m(lat, lon, g[1], g[2]), 1), g[0]))
    out, used = [], set()
    for n, g in enumerate(near[:JOBS * 2]):
        if len(out) >= JOBS:
            break
        h = _h(seed, g[0])
        km = kms[h % len(kms)]
        brg = math.radians((h >> 8) % 360)
        k = max(math.cos(math.radians(g[1])), 0.05)
        tl = g[1] + km * 1000.0 * math.cos(brg) / M_LAT
        tn = g[2] + km * 1000.0 * math.sin(brg) / (M_LAT * k)
        reach = TRUCK_SEARCH_M if truck else DROP_SEARCH_M
        far = [d for d in garages(_places(md, tl, tn, reach), test)
               if d[0] != g[0] and dist_m(tl, tn, d[1], d[2]) <= reach
               and dist_m(g[1], g[2], d[1], d[2]) >= 2000.0]
        if not far:
            continue
        d = min(far, key=lambda x: (round(dist_m(tl, tn, x[1], x[2]), 1), x[0]))
        if (g[0], d[0]) in used:
            continue
        ps, ds = spot_fn(g[1], g[2]), spot_fn(d[1], d[2])
        if ps is None or ds is None:
            continue
        used.add((g[0], d[0]))
        straight = dist_m(ps[0], ps[1], ds[0], ds[1])
        dist, dur = straight * DETOUR, straight * DETOUR / AVG_MS
        if route_fn is not None:
            try:
                r = route_fn(ps[0], ps[1], ds[0], ds[1])
                if float(r.get("dist") or 0) > 0:
                    dist, dur = float(r["dist"]), float(r.get("dur") or dist / AVG_MS)
            except Exception as e:  # offline, rate limited: the estimate
                log.debug("jobs: route: %s: %s", type(e).__name__, e)
        job = {"id": "%016x" % _h(seed, g[0], d[0], kind)}
        if truck:
            trailer, cargo, value = CARGO[(h >> 20) % len(CARGO)]
            job.update({"kind": "truck", "car": TRUCK, "car_name": TRUCK_NAME, "trailer": trailer, "cargo": cargo,
                        "rent": RENT})
            pay = truck_pay(dist, value)
        else:
            model, car_name, value = CARS[(h >> 20) % len(CARS)]
            job.update({"kind": "car", "car": model, "car_name": car_name})
            pay = pay_for(dist, value)
        out.append(dict(job, **{
                    "pickup": {"lat": round(ps[0], 6), "lon": round(ps[1], 6), "name": g[3]},
                    "dropoff": {"lat": round(ds[0], 6), "lon": round(ds[1], 6), "name": d[3]},
                    "dist": round(dist, 1), "dur": round(dur, 1), "pay": pay,
                    "limit": time_limit(dur)}))
    return out
