"""Repair shops and property near the player (the user, 2026-10-26: "repair shops/garages to buy new parts/fix your
car"; "purchasable & rentable property for storing and working on vehicles"): real places from Overture.
Repair shops: jobfind.is_garage (repair and body shops, dealers, tyre shops). Property: self storage, storage and
parking buildings (PROPERTY_WORDS). Each with the spot on the road beside it, its distance, and for property a buy
price and a weekly rent (deterministic: from its id). Nearest first, at most MAX within REACH_M.
"""
import hashlib

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
        item = {"id": g[0], "name": g[3], "lat": round(s[0], 6), "lon": round(s[1], 6),
                "dist": round(jobfind.dist_m(lat, lon, s[0], s[1]), 1)}
        if kind == "property":
            item["buy"], item["rent"] = prices(g[0])
        out.append(item)
    return out
