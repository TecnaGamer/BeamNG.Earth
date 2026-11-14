"""Pure calculations for geographically adaptive traffic populations (PR #728 / #773, ProxyFirewall202).

The population estimate is a smooth blend of nearest-settlement influence and
mapped local road density. It is an environmental heuristic, not a live traffic
measurement. Values are floating point until the game applies its configured cap.
navgraph.build attaches calculate()'s profile to every graph ("trafficProfile"); the mod moves the stock traffic's
active amount toward its target, one car at a time, with the Adaptive traffic amount setting on (navgraph.lua
updateAdaptiveTraffic). The drivers' speeds are speeding.lua's (by country), not a deck of this module's.
"""
from __future__ import annotations

import math
import dnp

SAMPLE_RADIUS_M = 750.0
SERVER_TARGET_MAX = 999  # Lua applies the actual user's native traffic limit after receiving the graph.
MAJOR_CLASSES = {"motorway", "trunk", "primary"}
LOCAL_CLASSES = {"residential", "unclassified", "living_street", "service", "tertiary", "secondary"}

# Approximate populations at distance from a populated place, ordered by rank
# (Natural Earth scalerank: 0 is the most prominent). The distance curve is
# interpolated with smoothstep, so its value and slope are continuous at every
# 50/200/400 km anchor and there are no threshold switches.
_DISTANCE_KM = (0.0, 4.0, 8.0, 15.0, 50.0, 200.0, 400.0)
_CURVES = {
    "metropolitan":       (15.0, 12.0, 7.0, 4.0, 4.0, 1.6, 0.0),
    "large-city":         (13.0, 10.0, 7.0, 4.0, 4.0, 1.5, 0.0),
    "regional-city":      (10.0, 8.0, 6.0, 4.0, 3.0, 1.0, 0.0),
    "regional-town":      (7.0, 6.0, 5.0, 4.0, 2.5, 0.7, 0.0),
    "small-town":         (5.0, 4.0, 3.0, 2.0, 1.5, 0.4, 0.0),
    "small-settlement":   (4.0, 3.5, 2.5, 1.5, 1.0, 0.2, 0.0),
}

# Road-line density (km of drivable centreline per km²) to a continuous local
# population contribution. Breakpoints are intentionally broad: this is a
# sampled network-density proxy, not an attempt to count actual road users.
_LOCAL_DENSITY = (0.0, 0.35, 0.7, 1.2, 1.8, 2.5, 3.5, 4.5, 6.0, 7.5, 9.0, 11.0)
_LOCAL_TARGET =  (0.0, 0.0, 0.25, 0.7, 1.6, 2.2, 3.2, 4.0, 5.0, 7.0, 10.0, 15.0)


def _clamp(value, low, high):
    return min(high, max(low, value))


def smoothstep(t):
    t = _clamp(float(t), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _interpolate(x, xs, ys):
    x = float(x)
    if x <= xs[0]:
        return float(ys[0])
    if x >= xs[-1]:
        return float(ys[-1])
    for i in range(1, len(xs)):
        if x <= xs[i]:
            t = smoothstep((x - xs[i - 1]) / (xs[i] - xs[i - 1]))
            return float(ys[i - 1] + (ys[i] - ys[i - 1]) * t)
    return float(ys[-1])


def settlement_target(distance_km, size_class="small-settlement"):
    """Uncapped settlement-distance target. Returns zero at and beyond 400 km."""
    curve = _CURVES.get(str(size_class), _CURVES["small-settlement"])
    return _interpolate(max(0.0, float(distance_km)), _DISTANCE_KM, curve)


def local_band(road_density, urban_share, local_density):
    """Diagnostic geography label retained from 1.7.1; never used as a step target."""
    density = max(0.0, float(road_density))
    urban = _clamp(float(urban_share), 0.0, 1.0)
    local = max(0.0, float(local_density))
    if density >= 9.0 or (density >= 7.5 and urban >= 0.20):
        return "city"
    if density >= 4.5 or (density >= 3.5 and urban >= 0.18):
        return "suburban"
    if density >= 1.5 and urban >= 0.18:
        return "town"
    if local >= 0.45 or (density >= 1.2 and urban < 0.70):
        return "rural"
    return "remote"


def local_target(road_density, urban_share=0.0, max_traffic=15.0):
    """Continuous population contribution from the nearby drivable-road network."""
    density = max(0.0, float(road_density))
    urban = _clamp(float(urban_share), 0.0, 1.0)
    base = _interpolate(density, _LOCAL_DENSITY, _LOCAL_TARGET)
    # Mapped built-up cover gives developed street grids a modest additional
    # weight, but when WorldCover is unavailable road density remains useful.
    cover_boost = 1.0 + 0.12 * smoothstep((urban - 0.08) / 0.38)
    return min(float(max_traffic), base * cover_boost)


def next_population_step(current, target):
    """Move a native active-vehicle ceiling by at most one vehicle."""
    current = max(0, int(current))
    target = max(0, int(target))
    return current + 1 if current < target else current - 1 if current > target else current


def is_divided_road(road_class, median_m=0.0, link=False, oneway=False):
    """True only for motorway mainlines or classified paired divided carriageways.

    `median_m` is populated by roads.dual_carriageways() only after matching
    named, parallel carriageways. A motorway ramp/link is deliberately excluded.
    """
    cls = str(road_class or "unknown")
    if bool(link):
        return False
    if cls == "motorway":
        return True
    return cls in {"trunk", "primary", "secondary", "tertiary"} and float(median_m or 0) > 0.0


def calculate(total_m, urban_m, major_m, local_m, settlement=None, road_class="unknown", divided=False,
              sample_radius_m=SAMPLE_RADIUS_M, max_traffic=15):
    """Build a JSON-safe diagnostic profile and floating desired vehicle population."""
    radius = max(1.0, float(sample_radius_m))
    total = max(0.0, float(total_m))
    urban = _clamp(float(urban_m), 0.0, total)
    major = _clamp(float(major_m), 0.0, total)
    local = max(0.0, float(local_m))
    area = math.pi * dnp.sq(radius / 1000.0)
    density = total / 1000.0 / area
    local_density = local / 1000.0 / area
    urban_share = urban / total if total > 1.0 else 0.0
    major_share = major / total if total > 1.0 else 0.0
    band = local_band(density, urban_share, local_density)
    cap = max(1, int(max_traffic))

    place = settlement if isinstance(settlement, dict) else None
    distance_km = None
    if place:
        try:
            distance_km = max(0.0, float(place.get("distanceM", 0.0)) / 1000.0)
        except (TypeError, ValueError):
            place = None
    size_class = str(place.get("sizeClass", "small-settlement")) if place else None
    proximity = settlement_target(distance_km, size_class) if distance_km is not None else 0.0
    nearby = local_target(density, urban_share, cap)

    # A major city centre can approach the configured limit, but the contribution
    # ramps with measured road density rather than switching at a band boundary.
    if place and size_class in ("metropolitan", "large-city") and distance_km is not None and distance_km < 30.0:
        city_core = cap * smoothstep((density - 7.5) / 3.5)
        nearby = max(nearby, city_core)
    target = max(proximity, nearby)
    multiplier = 2.0 if bool(divided) else 1.0
    target = min(float(cap), target * multiplier)

    if place:
        source = "Natural-Earth-populated-places+mapped-roads"
    else:
        source = "mapped-roads-fallback"
    return {
        "band": band,
        "distanceKm": round(distance_km, 2) if distance_km is not None else None,
        "settlement": str(place.get("name")) if place else None,
        "settlementRank": int(place.get("rank", 8)) if place else None,
        "settlementSize": size_class,
        "settlementLat": float(place["lat"]) if place and place.get("lat") is not None else None,
        "settlementLon": float(place["lon"]) if place and place.get("lon") is not None else None,
        "roadClass": str(road_class or "unknown"),
        "carriageway": "freeway" if divided and str(road_class) == "motorway" else
                       ("dual-carriageway" if divided else "single-carriageway"),
        "divided": bool(divided),
        "roadDensityKmKm2": round(density, 3),
        "localRoadDensityKmKm2": round(local_density, 3),
        "urbanRoadShare": round(urban_share, 4),
        "majorRoadShare": round(major_share, 4),
        "sampleRadiusM": int(round(radius)),
        "sampleAreaKm2": round(area, 3),
        "settlementTarget": round(proximity, 3),
        "localTarget": round(nearby, 3),
        "roadMultiplier": multiplier,
        "target": round(target, 3),
        "source": source,
    }
