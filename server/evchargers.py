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
