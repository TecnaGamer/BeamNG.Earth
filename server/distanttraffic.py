"""Distant traffic's visual routes (#565), separate from the AI graph and its physics window.

Road and bridge parts split into stable pieces in a geotile's frame. A piece has the same id in every tile that sees it:
the mod keeps one route while any tile owns it, so crossing a tile edge does not restart its cars. No invented joins across junctions,
or tunnels; ends fade in Lua. Pure given the map cells, elev and solved bridge profiles.
"""
import json
import os

import numpy as np

import fast
import mapgeom
import navgraph
import roads
import staging

PROTO = 2
Z = 14
SEND_M = 5500.0
DROP_M = 6500.0
FAR_SEND_M = 11000.0
FAR_DROP_M = 13000.0
FAR_SKIP_M = 4500.0
fast.require(143, "distant traffic routes")
if not hasattr(fast._ef, "TrafficRoutes"):
    raise fast.NativeMissing("distant traffic routes needs this branch's native module with TrafficRoutes: "
                             "rebuild it with python3 tools/build_native.py, or use the matching test build")
_KERNEL = fast._ef.TrafficRoutes
MAX_PATHS, MAX_POINTS, MAX_TILE_POINTS, CHUNK_M, STEP_M, MIN_LENGTH_M = _KERNEL.limits()


def build(frame, feats_list, elev, box, urban_fn=None, widths=None, left_hand=False, mph=True, major_only=False,
          bridges=None):
    """Marshal the shared road rules and height sampler to the Rust route planner (native/earthfast/src/traffic.rs).

    The planner requests one admitted piece at a time: heights are sampled only for pieces fitting its payload,
    not for every road in a city. The road builder still owns lane shifts, driving widths and speed-limit rules.
    """
    inputs, sources, decks = [], [], {}
    if bridges is not None:
        for (fid, part), _spec, xy, z, _lon in bridges.parts_with_ids(feats_list, box):
            decks.setdefault(str(fid), []).append((part, np.ascontiguousarray(np.column_stack([xy, z]), np.float64)))
    for feats in feats_list:
        if feats is None or not len(feats):
            continue
        specs = roads.road_specs(feats, urban_fn, widths, left_hand)
        for i, (ground, bridge) in enumerate(zip(specs, roads.bridge_specs(feats))):
            sp = ground if ground is not None else bridge
            if sp is None:
                continue
            parts = [(part, np.ascontiguousarray(np.column_stack(mapgeom.enu_xy(feats, frame, ll)), np.float64))
                     for part, ll in enumerate(roads.ground_parts(feats, i))]
            parts.extend(decks.get(str(feats.ids[i]), ()))
            ow = roads._rule_value(feats.rules.get("oneway"), i)
            inputs.append((str(feats.ids[i]), sp.cls, sp.kind, bool(sp.oneway), float(ow or sp.dir or 0),
                           navgraph.driving_width(sp), navgraph.road_mph(feats, i, sp, mph),
                           roads._rule_value(feats.rules.get("subclass"), i) in navgraph.SKIP_SUBCLASSES, parts))
            sources.append((feats, i))
    planner = _KERNEL(inputs, box, left_hand, major_only)
    while (pending := planner.next()) is not None:
        source, xy = pending
        feats, i = sources[source]
        la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
        shifts = roads.shift_at(roads.lane_shifts(feats, urban_fn, widths, left_hand)[i], lo, la)
        import lanesections
        prof = lanesections.profile(feats, i)
        if prof and prof["extra"]:
            # Through traffic stays on its original lane grid as an auxiliary
            # lane opens outside it; a four-lane centre is between two lanes.
            shifts = -prof["shift"] * lanesections.growth(prof, lo, la)
        lane = planner.lane_points(np.ascontiguousarray(shifts, np.float64) if shifts is not None else None)
        heights = planner.deck_heights()
        if heights is None:
            la, lo, _ = frame.enu_to_geodetic(lane[:, 0], lane[:, 1], 0.0)
            heights = np.broadcast_to(np.asarray(elev(la, lo), np.float64), len(lane))
            _, _, curve = frame.geodetic_to_enu(la, lo, 0.0)
            heights = heights + curve
        planner.accept(np.ascontiguousarray(heights, np.float64))
    return planner.routes()


def tile(md, gx, gy, elev, urban_fn=None, widths=None, left_hand=False, mph=True, major_only=False, bridge_source=None):
    """A geotile's JSON, or None while its road cells are missing (prefetch, do not hold a worker on downloads)."""
    import geotiles
    t = geotiles.geobox(gx, gy, Z)
    keys = roads.cells_of_tile(t.frame, t, None, margin=0)
    missing = [k for k in keys if not md.has(k, "roads")]
    if missing:
        for k in missing:
            md.prefetch(k, layers=("roads",))
        return None
    feats = [md.cell(k, layers=("roads",), kind="background").layers.get("roads") for k in keys]
    paths = build(t.frame, feats, elev, t.box, urban_fn, widths, left_hand, mph, major_only,
                  bridges=bridge_source(t.frame) if bridge_source is not None else None)
    heights = [p[2] for path in paths for p in path["points"]]
    return {"v": PROTO, "key": t.key, "frame": [float(t.frame.lat0), float(t.frame.lon0)],
            "pivot": float(np.median(heights)) if heights else 0.0, "paths": paths}


def write(out_dir, vfs_dir, data, kind="traffic"):
    """Tracker result (path, count, frame, pivot); atomic, as the other streamed JSON files."""
    name = f"{kind}_{data['key']}.json"
    text = json.dumps(data, separators=(",", ":"), allow_nan=False)
    path = os.path.join(out_dir, name)
    tmp = staging.tmp_for(path)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    staging.replace(tmp, path)
    return f"{vfs_dir}/{name}", len(data["paths"]), data["frame"], data["pivot"]
