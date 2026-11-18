"""The planned GPS route's upcoming corridor (routing.py's road route), fetched into the disk caches well
before the player drives it (the user, 2026-10-22: picking a route should start loading its road, bridges
etc. right away). download_ahead only looks a minute or so along the player's current heading; a 20 km
road route can wander off that heading entirely (a bridge, a one-way detour), and a player can cover it
in a few minutes at speed. Reuses netfetch "prefetch" priority exactly like download_ahead: only a host's
spare tokens, never ahead of a build's own demand fetch. Pure point/key generation only; earthd.py owns
the actual fetch loop and its pool.
"""
import math

import numpy as np

import fast
import dmath
import mapcells

ROUTE_SAMPLE_M = 250.0      # spacing of corridor sample points along the route's cumulative arc length


def arc_length_points(points, max_m, step_m=ROUTE_SAMPLE_M):
    """points [[lat, lon], ...] (routing.py's simplified geometry, starting at the player) -> [(lat, lon), ...]
    sampled every step_m along the route's cumulative length, nearest first, stopping at min(max_m, the
    route's own length). Rust: earthfast route_samples."""
    if not points:
        return []
    fast.require(72, "route_samples")
    p = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    la, lo = fast._ef.route_samples(np.ascontiguousarray(p[:, 0]), np.ascontiguousarray(p[:, 1]), float(max_m),
                                    float(step_m))
    return list(zip(la.tolist(), lo.tolist()))


def need(dem, mapdata, pts, reach_m, zoom, layers):
    """No network: the deduped (nearest-first) DEM tile keys and (cell key, layer) pairs the corridor's
    sample points need. dem_keys: dict.fromkeys so order survives the dedupe, as keys_around itself does
    for its ground-parent tiles."""
    dem_keys = {}
    for la, lo in pts:
        for k in dem.keys_around(la, lo, reach_m, zoom):
            dem_keys.setdefault(k, None)
    cell_layer_pairs = []
    if layers and pts:
        lats = np.array([p[0] for p in pts])
        lons = np.array([p[1] for p in pts])
        dlat = reach_m / 111320.0
        dlon = reach_m / (111320.0 * max(0.05, dmath.cos(math.radians(float(lats[0])))))
        for key in mapcells.cells_near_points(lats, lons, dlat, dlon):
            for layer in layers:
                cell_layer_pairs.append((key, layer))
    return list(dem_keys.keys()), cell_layer_pairs


def done_count(dem, mapdata, dem_keys, cell_layer_pairs):
    """No network: how many of need()'s keys are already on disk (dem.has / mapdata.has)."""
    return (sum(1 for k in dem_keys if dem.has(*k)) +
            sum(1 for key, layer in cell_layer_pairs if mapdata.has(key, layer)))
