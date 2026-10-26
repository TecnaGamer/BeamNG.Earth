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
# Loads (the user, 2026-10-26: "make jobs load the trailers with the listed material ... and make sure it makes sense for
# the company asking you to do it. steelworks hauls steel, lumber yards haul lumber"): each load the stock trailer
# config that carries it (the game's content/vehicles), the pickup businesses it comes from and the ones it goes to
# (words in the Overture category / taxonomy), and its pay premium. A pickup's load is one whose pickup words its
# category has; its destination a business with one of the load's drop words (else DROP_FALLBACK's).
LOADS = (
    # id, trailer, trailer config, label, premium, pickup words, drop words
    ("coils", "flatbed", "vehicles/flatbed/48ft_coil.pc", "Steel coils", 1.2,
     ("steel", "metal", "foundry", "metal_fabricat"), ("manufactur", "factory", "fabricat", "auto_manufactur", "industrial")),
    ("pipes", "flatbed", "vehicles/flatbed/48ft_pipes.pc", "Steel pipes", 1.15,
     ("steel", "pipe", "metal_supplier", "plumbing_supply"), ("construction", "contractor", "plumbing", "oil", "industrial")),
    ("lumber", "flatbed", "vehicles/flatbed/48ft_wood.pc", "Lumber", 1.15,
     ("lumber", "sawmill", "wood", "timber"), ("building_supply", "hardware", "home_improvement", "construction",
                                               "contractor", "lumber")),
    ("logs", "log_trailer", "vehicles/log_trailer/40ft_12logs.pc", "Logs", 1.15,
     ("logging", "forestry", "timber", "tree_service"), ("sawmill", "lumber", "paper", "wood")),
    ("stone", "flatbed", "vehicles/flatbed/32ft_marble.pc", "Stone blocks", 1.1,
     ("quarry", "stone", "marble", "granite", "mining"), ("construction", "contractor", "landscap", "countertop",
                                                          "monument", "building_supply")),
    ("tires", "flatbed", "vehicles/flatbed/48ft_tires.pc", "Tires", 1.05,
     ("tire_wholesale", "tire_distribut", "tire_dealer", "tire"), ("tire", "auto_repair", "car_dealer", "automotive")),
    ("gasoline", "tanker", "vehicles/tanker/petroleum_3.pc", "Gasoline", 1.25,
     ("fuel", "petroleum", "oil_and_gas", "refiner", "energy_company", "oil_company"), ("gas_station", "fueling_station")),
    ("diesel", "tanker", "vehicles/tanker/diesel_3.pc", "Diesel", 1.25,
     ("fuel", "petroleum", "oil_and_gas", "refiner", "oil_company"), ("truck_stop", "gas_station", "fueling_station",
                                                                      "farm", "construction")),
    ("milk", "tanker", "vehicles/tanker/milk_3.pc", "Milk", 1.15,
     ("dairy", "farm", "creamery"), ("food_process", "dairy", "creamery", "grocery", "supermarket", "food_manufactur")),
    ("water", "tanker", "vehicles/tanker/water_3.pc", "Water", 1.0,
     ("water_utility", "water_supplier", "water_treatment", "bottled_water"), ("construction", "farm", "contractor")),
    ("freight", "dryvan", "vehicles/dryvan/53ft_22400kg.pc", "Dry goods", 1.0,
     ("warehouse", "distribution", "logistics", "wholesale", "manufactur", "factory"),
     ("supermarket", "grocery", "department_store", "superstore", "retail", "home_improvement", "shopping", "store")),
    ("groceries", "dryvan", "vehicles/dryvan/48ft_19200kg.pc", "Groceries", 1.05,
     ("food_distribut", "grocery_wholesale", "food_wholesale", "food_process", "bakery", "food_manufactur"),
     ("supermarket", "grocery", "convenience_store", "superstore")),
    ("container", "containerTrailer", "vehicles/containerTrailer/40ft_3axle_40ft.pc", "Shipping container", 1.1,
     ("seaport", "cargo", "freight", "intermodal", "container_terminal", "rail_yard"),
     ("warehouse", "distribution", "logistics")),
    ("chemicals", "containerTrailer", "vehicles/containerTrailer/40ft_3axle_tank.pc", "Chemicals", 1.3,
     ("chemical",), ("manufactur", "factory", "water_treatment", "industrial", "plastic")),
)
DROP_FALLBACK = ("warehouse", "distribution", "logistics")
DEPOT_WORDS = tuple(sorted({w for ld in LOADS for w in ld[5]}))
NOT_DEPOT = ("self_storage", "parking", "car_wash", "gas_station", "fueling_station")
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
CARGO = tuple((ld[1], ld[3], ld[4]) for ld in LOADS)        # (trailer, label, premium): the tests' view
# (midsize is the Gavril Grand Marshal's smaller sibling in some versions; the name shown is only a label)


def _h(*parts):
    return int(hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12], 16)


def _cat(category, taxonomy):
    return (str(category or "") + " " + str(taxonomy or "")).lower()


def loads_for(cat):
    """The loads a business of category text cat ships."""
    return [ld for ld in LOADS if any(w in cat for w in ld[5])]


def receives(ld, cat):
    """Does a business of category text cat take load ld?"""
    return any(w in cat for w in ld[6])


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
            out.append((fid, float(lat), float(lon), name, _cat(col("basic_category", i), col("taxonomy", i))))
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
        k = max(math.cos(math.radians(g[1])), 0.05)
        reach = TRUCK_SEARCH_M if truck else DROP_SEARCH_M
        if truck:
            mine = loads_for(g[4] if len(g) > 4 else "")
            r0 = (h >> 4) % len(mine) if mine else 0
            choices = mine[r0:] + mine[:r0]         # the business's loads, the hash's first: the next if nobody takes it
        else:
            choices = [None]
        load, far = None, []
        for cand in choices:
            if truck:
                def drop_test(category, taxonomy, status=None, ld=cand):
                    if status and str(status) in ("closed", "permanently_closed", "temporarily_closed"):
                        return False
                    return receives(ld, _cat(category, taxonomy))
            else:
                drop_test = test
            for tries in range(3 if truck else 1):      # (a load's customers are fewer: two more bearings)
                hh = _h(seed, g[0], tries)
                km = kms[hh % len(kms)]
                brg = math.radians((hh >> 8) % 360)
                tl = g[1] + km * 1000.0 * math.cos(brg) / M_LAT
                tn = g[2] + km * 1000.0 * math.sin(brg) / (M_LAT * k)
                cands = garages(_places(md, tl, tn, reach), drop_test)
                far = [d for d in cands if d[0] != g[0] and dist_m(tl, tn, d[1], d[2]) <= reach
                       and dist_m(g[1], g[2], d[1], d[2]) >= 2000.0]
                if far:
                    break
            if far:
                load = cand
                break
        if truck and not far and choices:               # nobody for any of its loads: a warehouse takes the first
            load = choices[0]
            for tries in range(3):
                hh = _h(seed, g[0], tries)
                km = kms[hh % len(kms)]
                brg = math.radians((hh >> 8) % 360)
                tl = g[1] + km * 1000.0 * math.cos(brg) / M_LAT
                tn = g[2] + km * 1000.0 * math.sin(brg) / (M_LAT * k)
                cands = garages(_places(md, tl, tn, reach),
                                lambda c, x, s=None: any(w in _cat(c, x) for w in DROP_FALLBACK))
                far = [d for d in cands if d[0] != g[0] and dist_m(tl, tn, d[1], d[2]) <= reach
                       and dist_m(g[1], g[2], d[1], d[2]) >= 2000.0]
                if far:
                    break
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
            _lid, trailer, config, cargo, value = load[:5]
            job.update({"kind": "truck", "car": TRUCK, "car_name": TRUCK_NAME, "trailer": trailer,
                        "trailer_config": config, "cargo": cargo, "rent": RENT})
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


# --- parcel delivery (the user, 2026-10-26: "add amazon jobs"; last night's list: "amazon delivery") ---------------------
# A van route: at a real parcel depot (PARCEL_WORDS) your car becomes a delivery van; PARCEL_STOPS drop-offs on the
# residential streets of one neighbourhood PARCEL_KM away, in nearest-next order; paid per stop and per mile.
PARCEL_WORDS = ("distribution", "warehouse", "courier", "post_office", "shipping_center", "logistics", "parcel",
                "delivery_service", "mail")
PARCEL_KM = (2.5, 3.5, 5.0)
PARCEL_STOPS = (6, 7, 8, 9)
PARCEL_SPREAD_M = 600.0
PARCEL_BASE, PARCEL_PER_STOP, PARCEL_PER_MILE = 60.0, 30.0, 6.0
VAN, VAN_NAME = "van", "Beamazon van"   # (the user: "call it beamazon")


def is_parcel_depot(category, taxonomy, status=None):
    if status and str(status) in ("closed", "permanently_closed", "temporarily_closed"):
        return False
    t = _cat(category, taxonomy)
    return any(w in t for w in PARCEL_WORDS) and "self_storage" not in t


def parcel_pay(dist, stops):
    """$ for a route of dist m with stops drop-offs (to the nearest $5)."""
    return float(5 * round((PARCEL_BASE + PARCEL_PER_STOP * stops + PARCEL_PER_MILE * dist / MI) / 5.0))


def parcel_offers(md, lat, lon, seed=0, spot_fn=None, house_fn=None):
    """Up to JOBS van routes from the parcel depots near (lat, lon): the delivery job shape plus "stops": [{lat, lon,
    name}...] (the last also the "dropoff"). house_fn(lat, lon) -> (lat, lon, ...) a spot on a residential street near
    the point, or None."""
    spot_fn = spot_fn or (lambda la, lo: randomplace.road_spot(md, la, lo, radius_m=SPOT_M))
    house_fn = house_fn or (lambda la, lo: randomplace.road_spot(md, la, lo, radius_m=250.0,
                                                                  classes=("residential", "living_street")))
    near = [g for g in garages(_places(md, lat, lon, PICK_M), is_parcel_depot) if dist_m(lat, lon, g[1], g[2]) <= PICK_M]
    near.sort(key=lambda g: (round(dist_m(lat, lon, g[1], g[2]), 1), g[0]))
    out = []
    for g in near[:JOBS]:
        h = _h(seed, g[0], "parcel")
        ps = spot_fn(g[1], g[2])
        if ps is None:
            continue
        km = PARCEL_KM[h % len(PARCEL_KM)]
        brg = math.radians((h >> 8) % 360)
        k = max(math.cos(math.radians(g[1])), 0.05)
        cl = g[1] + km * 1000.0 * math.cos(brg) / M_LAT
        cn = g[2] + km * 1000.0 * math.sin(brg) / (M_LAT * k)
        n = PARCEL_STOPS[(h >> 16) % len(PARCEL_STOPS)]
        pts = []
        for i in range(n * 3):                          # (some points find no street: three tries a stop)
            if len(pts) >= n:
                break
            hi = _h(seed, g[0], "stop", i)
            r = PARCEL_SPREAD_M * ((hi % 1000) / 1000.0) ** 0.5
            a = math.radians((hi >> 10) % 360)
            s = house_fn(cl + r * math.cos(a) / M_LAT, cn + r * math.sin(a) / (M_LAT * k))
            if s is not None and all(dist_m(s[0], s[1], q[0], q[1]) > 60.0 for q in pts):
                pts.append((float(s[0]), float(s[1])))
        if len(pts) < 3:
            continue
        route, here, dist = [], (ps[0], ps[1]), 0.0     # nearest-next order from the depot
        left = list(pts)
        while left:
            nxt = min(left, key=lambda q: dist_m(here[0], here[1], q[0], q[1]))
            dist += dist_m(here[0], here[1], nxt[0], nxt[1]) * DETOUR
            route.append(nxt)
            left.remove(nxt)
            here = nxt
        stops = [{"lat": round(a, 6), "lon": round(b, 6), "name": "Stop %d" % (i + 1)} for i, (a, b) in enumerate(route)]
        dur = dist / AVG_MS + 45.0 * len(stops)
        out.append({"id": "%016x" % _h(seed, g[0], "route"), "kind": "parcel", "car": VAN, "car_name": VAN_NAME,
                    "cargo": "Beamazon: %d parcels" % len(stops),
                    "pickup": {"lat": round(ps[0], 6), "lon": round(ps[1], 6), "name": g[3]},
                    "dropoff": stops[-1], "stops": stops, "dist": round(dist, 1), "dur": round(dur, 1),
                    "pay": parcel_pay(dist, len(stops)), "limit": time_limit(dur)})
    return out
