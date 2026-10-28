"""The nearest gas station to a point (the user, 2026-10-25: "i need a gas station spawn so i dont search forever"): the
world map's "Nearest gas station" button teleports there.

Stations are gasstations.py's own candidates (Overture places: a gas_station / fueling_station category or a fuel
brand; a convenience brand alone is no station here, as gasstations.py needs more evidence for it), searched in
growing squares of map cells around the point (RINGS_M) so a town's nearest station costs one or two cells and a remote
road still finds one. The spot is on the nearest public road beside the station (randomplace.road_spot, SPOT_M), not on
its pin: pins stand on the shop, beside the pumps or on the road. Deterministic: ties go to the lower Overture id.
"""
import logging
import math

import geodesy
import gasstations
import mapcells
import randomplace

log = logging.getLogger("earthd.fuelfind")

RINGS_M = (2000.0, 6000.0, 15000.0)     # search squares' half sizes, nearest first
SPOT_M = 250.0                          # the road beside the station is at most this far from its pin
M_LAT = 111320.0


def _box(lat, lon, half_m):
    k = max(math.cos(math.radians(lat)), 0.05)
    dlat, dlon = half_m / M_LAT, half_m / (M_LAT * k)
    return lat - dlat, lon - dlon, lat + dlat, lon + dlon


def candidates(frame, layers):
    """[(distance m from the frame's origin, Pin)] of the real stations among places layers, nearest first."""
    out = []
    for p in gasstations.pins(frame, layers):
        if not (p.gas or p.fuel_brand):
            continue
        out.append((math.hypot(p.x, p.y), p))
    out.sort(key=lambda dp: (round(dp[0], 3), dp[1].id))
    return out


def nearest(md, lat, lon, rings=RINGS_M):
    """{"lat", "lon", "name", "dist"} of a spot on the road beside the gas station nearest (lat, lon) within the largest
    ring, or None. dist: straight-line metres to the station."""
    frame = geodesy.EnuFrame(lat, lon, 0.0)
    done = set()
    layers = []
    for half in rings:
        keys = [k for k in mapcells.cells_for_bbox(*_box(lat, lon, half)) if k not in done]
        for key in keys:
            done.add(key)
            try:
                f = md.cell(key, layers=("places",), kind="demand").layers.get("places")
            except Exception as e:      # a cell that could not be fetched: the others still count
                log.debug("nearest fuel: cell %s: %s: %s", key, type(e).__name__, e)
                continue
            if f is not None and len(f):
                layers.append(f)
        found = candidates(frame, layers)
        # a station inside this ring's circle is the nearest anywhere (cells outside it are farther); one in the
        # square's corners may not be: look one ring further before taking it
        for dist, p in found:
            if dist > half:
                break
            plat, plon, _h = frame.enu_to_geodetic(p.x, p.y, 0.0)
            plat, plon = float(plat), float(plon)
            spot = randomplace.road_spot(md, plat, plon, radius_m=SPOT_M)
            if spot is None:
                continue                # no public road beside it (a motorway service area's): the next one
            name = (p.name or p.brand or "Gas station").strip()
            return {"lat": spot[0], "lon": spot[1], "name": name, "dist": round(dist, 1)}
    return None
