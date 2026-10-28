"""Repair shops and property near the player (the user, 2026-10-26: "repair shops/garages to buy new parts/fix your
car"; "purchasable & rentable property for storing and working on vehicles"): real places from Overture.
Repair shops: jobfind.is_garage (repair and body shops, dealers, tyre shops). Property: self storage, storage and
parking buildings (PROPERTY_WORDS). Each with the spot on the road beside it, its distance, and for property a buy
price and a weekly rent (deterministic: from its id). Nearest first, at most MAX within REACH_M.
"""
import hashlib
import math

import mapcells
import jobfind
import randomplace

PROPERTY_WORDS = ("self_storage", "storage", "parking", "garage_rental", "warehouse_rental")
REACH_M = 6000.0
MAX = 8


def is_property(category, taxonomy, status=None):
    if status and str(status) in ("closed", "permanently_closed", "temporarily_closed"):
        return False
    t = (str(category or "") + " " + str(taxonomy or "")).lower()
    return any(w in t for w in PROPERTY_WORDS)


MARK_MIN_M, MARK_MAX_M = 9.0, 25.0


def marker(rlat, rlon, plat, plon):
    """The garage spot (the blue ring, the workbench and laptop; the user, 2026-10-26: "some properties locations are off
    slightly and would likely place the desk and computer in the road"): from the road spot toward the business's pin,
    MARK_MIN_M to MARK_MAX_M off the road, so it stands beside the building, not on the street."""
    k = math.cos(math.radians(rlat))
    dn, de = (plat - rlat) * jobfind.M_LAT, (plon - rlon) * jobfind.M_LAT * k
    d = math.hypot(dn, de)
    if d < 1.0:
        dn, de, d = 0.0, 1.0, 1.0          # (a pin on the road: a side of it)
    r = min(max(d * 0.6, MARK_MIN_M), MARK_MAX_M)
    return rlat + dn / d * r / jobfind.M_LAT, rlon + de / d * r / (jobfind.M_LAT * k)


# The bench against the building (the user, 2026-10-26, with screenshots: the desk stood out on the grass by the road;
# it belongs against the property's wall, where the car is parked): the building at the pin (or the nearest one), its
# wall facing the road, the stock workbench (4.84 m long, 0.5 m deep) along it with its back to the wall, the blue ring
# RING_OUT_M in front of it, where the car stands.
BENCH_LEN, BENCH_DEPTH, WALL_GAP = 4.84, 0.5, 0.1
RING_OUT_M = 5.5
WALL_REACH_M = 40.0
MIN_WALL_M = BENCH_LEN + 0.6


def _seg_dist(p, a, b):
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    l2 = dx * dx + dy * dy
    t = 0.0 if l2 <= 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / l2))
    return math.hypot(p[0] - ax - dx * t, p[1] - ay - dy * t)


def _inside(p, ring):
    x, y, c = p[0], p[1], False
    for i in range(len(ring)):
        x0, y0 = ring[i - 1]
        x1, y1 = ring[i]
        if (y0 > y) != (y1 > y) and x < (x1 - x0) * (y - y0) / ((y1 - y0) or 1e-12) + x0:
            c = not c
    return c


def desk_on_wall(rings, pin, road):
    """rings: building outlines [[(x, y)...]...] (m, east / north); pin the business, road the road spot. The bench:
    ((x, y) its middle, yaw (radians, counter-clockwise: its back, local +y, turned toward the wall), (x, y) the ring),
    or None (no building near, no wall long enough). Pure."""
    best = None
    for ring in rings:
        ring = [tuple(map(float, q)) for q in ring]
        if len(ring) > 1 and ring[0] == ring[-1]:
            ring = ring[:-1]
        if len(ring) < 3:
            continue
        d = 0.0 if _inside(pin, ring) else min(_seg_dist(pin, ring[i - 1], ring[i]) for i in range(len(ring)))
        if d <= WALL_REACH_M and (best is None or d < best[0]):
            best = (d, ring)
    if best is None:
        return None
    ring = best[1]
    area = sum(ring[i - 1][0] * ring[i][1] - ring[i][0] * ring[i - 1][1] for i in range(len(ring))) / 2.0
    pick = None
    for i in range(len(ring)):
        a, b = ring[i - 1], ring[i]
        dx, dy = b[0] - a[0], b[1] - a[1]
        ln = math.hypot(dx, dy)
        if ln < MIN_WALL_M:
            continue
        ux, uy = dx / ln, dy / ln
        nx, ny = (uy, -ux) if area > 0 else (-uy, ux)          # outward
        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        tx, ty = road[0] - mx, road[1] - my
        facing = (tx * nx + ty * ny) / max(math.hypot(tx, ty), 1e-9)      # 1: the wall looks straight at the road
        score = (0 if facing > 0 else 1, -round(facing, 3), _seg_dist(road, a, b))
        if pick is None or score < pick[0]:
            pick = (score, a, ux, uy, nx, ny, ln)
    if pick is None:
        return None
    _, a, ux, uy, nx, ny, ln = pick
    half = BENCH_LEN / 2 + 0.3
    t = (road[0] - a[0]) * ux + (road[1] - a[1]) * uy
    t = min(max(t, half), ln - half)
    off = BENCH_DEPTH / 2 + WALL_GAP
    cx, cy = a[0] + ux * t + nx * off, a[1] + uy * t + ny * off
    yaw = math.atan2(nx, -ny)                                  # local +y (the bench's back) -> -n, into the wall
    return (cx, cy), yaw, (cx + nx * RING_OUT_M, cy + ny * RING_OUT_M)


def buildings_near(md, lat, lon, radius_m=WALL_REACH_M + 30):
    """Building outlines near (lat, lon), [[(x, y)...]...] m east / north of it (each once)."""
    k = math.cos(math.radians(lat))
    dlat, dlon = radius_m / jobfind.M_LAT, radius_m / (jobfind.M_LAT * max(k, 0.05))
    out, seen = [], set()
    for key in mapcells.cells_for_bbox(lat - dlat, lon - dlon, lat + dlat, lon + dlon):
        f = md.cell(key, layers=("buildings",), kind="demand").layers.get("buildings")
        if f is None or not len(f):
            continue
        for i in range(len(f)):
            fid = str(f.ids[i])
            if fid in seen:
                continue
            seen.add(fid)
            for part in f.parts(i):
                if part:
                    r = part[0]
                    out.append([((float(lo) - lon) * jobfind.M_LAT * k, (float(la) - lat) * jobfind.M_LAT)
                                for lo, la in r[:, :2]])
    return out


def wall_spot(md, rlat, rlon, plat, plon):
    """{mlat, mlon (the ring), dlat, dlon, dyaw (the bench)} against the business's building, or None."""
    try:
        rings = buildings_near(md, plat, plon)
    except Exception:
        return None
    k = math.cos(math.radians(plat))
    road = ((rlon - plon) * jobfind.M_LAT * k, (rlat - plat) * jobfind.M_LAT)
    got = desk_on_wall(rings, (0.0, 0.0), road)
    if got is None:
        return None
    (dx, dy), yaw, (rx, ry) = got

    def ll(x, y):
        return plat + y / jobfind.M_LAT, plon + x / (jobfind.M_LAT * k)
    dlat, dlon = ll(dx, dy)
    mlat, mlon = ll(rx, ry)
    return {"mlat": round(mlat, 7), "mlon": round(mlon, 7), "dlat": round(dlat, 7), "dlon": round(dlon, 7),
            "dyaw": round(yaw, 4)}


def prices(pid):
    """(buy $, rent $ a week) of a property, from its id."""
    h = int(hashlib.sha1(str(pid).encode()).hexdigest()[:8], 16)
    return float(18000 + (h % 43) * 1000), float(400 + ((h >> 8) % 17) * 50)


def near(md, lat, lon, kind="repair", spot_fn=None):
    spot_fn = spot_fn or (lambda la, lo: randomplace.road_spot(md, la, lo, radius_m=jobfind.SPOT_M))
    test = is_property if kind == "property" else jobfind.is_garage
    found = [g for g in jobfind.garages(jobfind._places(md, lat, lon, REACH_M), test)
             if jobfind.dist_m(lat, lon, g[1], g[2]) <= REACH_M]
    found.sort(key=lambda g: (round(jobfind.dist_m(lat, lon, g[1], g[2]), 1), g[0]))
    out = []
    for g in found:
        if len(out) >= MAX:
            break
        s = spot_fn(g[1], g[2])
        if s is None:
            continue
        mlat, mlon = marker(s[0], s[1], g[1], g[2])
        item = {"id": g[0], "name": g[3], "lat": round(s[0], 6), "lon": round(s[1], 6),
                "mlat": round(mlat, 7), "mlon": round(mlon, 7),
                "dist": round(jobfind.dist_m(lat, lon, s[0], s[1]), 1)}
        w = wall_spot(md, s[0], s[1], g[1], g[2]) if md is not None else None
        if w:
            item.update(w)
        if kind == "property":
            item["buy"], item["rent"] = prices(g[0])
        out.append(item)
    return out
