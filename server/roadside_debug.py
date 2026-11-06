"""Opt-in roadside elevation evidence. This module never decides eligibility.

BNGE_ROADSIDE_DEBUG selects comma-separated GERS ids, OSM way ids, or '*'.
Only emit() after ENABLED/selected checks; normal generation does no sampling.
All compared geometry heights are frame ENU z; geodetic heights are named h_m.
"""
import json
import logging
import os

import numpy as np

from roaddecor_debug import identity, _safe

log = logging.getLogger("earthd.roadside")
SELECTORS = frozenset(v.strip() for v in os.environ.get("BNGE_ROADSIDE_DEBUG", "").split(",") if v.strip())
ENABLED = bool(SELECTORS)
PROFILE_RADIUS_M = 300.
PROFILE_STEP_M = 2.
SCHEMA_VERSION = 1


def selected(feats, i):
    if not ENABLED:
        return False
    if "*" in SELECTORS or str(feats.ids[i]) in SELECTORS:
        return True
    ident = identity(feats, i)
    way = ident["osm_way_id"]
    return ident["source_record"] in SELECTORS or (way is not None and
        (str(way) in SELECTORS or "w" + str(way) in SELECTORS))


def _sample(frame, xy, elev):
    lat, lon, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
    if elev is None:
        return np.asarray(lat), np.asarray(lon), None, None
    height = np.asarray(elev(lat, lon), np.float64)
    z = np.asarray(frame.geodetic_to_enu(lat, lon, height)[2], np.float64)
    return np.asarray(lat), np.asarray(lon), height, z


def explain(feats, i, spec, frame, *, elev, raw_elev=None, ground=None, walls=(), props=(),
            tile=None, dem_source=None, cache_status="generated"):
    """Compare computed terrain/objects; this does not observe the live engine.

    elev/raw_elev return geodetic height at lat/lon. ground, if supplied, returns
    actual generated terrain-grid ENU z at x/y. Nearby walls/poles are described
    without guessing ownership from nearest-road matching.
    """
    import grading
    import roads
    import revisit

    profiles = []
    for part in roads.ground_parts(feats, i):
        source = np.asarray(part, np.float64)
        if len(source) < 2:
            continue
        east, north, _ = frame.geodetic_to_enu(source[:, 1], source[:, 0], np.zeros(len(source)))
        source_xy = np.column_stack([east, north])
        xy, station = grading.resample(source_xy, PROFILE_STEP_M)
        keep = np.hypot(xy[:, 0], xy[:, 1]) <= PROFILE_RADIUS_M
        if not keep.any():
            nearest = int(np.argmin(np.hypot(xy[:, 0], xy[:, 1])))
            keep = np.abs(station - station[nearest]) <= PROFILE_RADIUS_M
        derivative = np.gradient(xy, axis=0)
        derivative /= np.maximum(np.linalg.norm(derivative, axis=1), 1e-9)[:, None]
        normal = np.column_stack([-derivative[:, 1], derivative[:, 0]])
        source_arc = np.r_[0., np.cumsum(np.linalg.norm(np.diff(source_xy, axis=0), axis=1))]
        first = max(0, int(np.searchsorted(source_arc, station[keep].min())) - 1)
        last = min(len(source), int(np.searchsorted(source_arc, station[keep].max(), side="right")) + 1)
        chord_source = source[first:last]
        source_h = np.asarray(elev(chord_source[:, 1], chord_source[:, 0]), np.float64)
        source_z = np.asarray(frame.geodetic_to_enu(chord_source[:, 1], chord_source[:, 0], source_h)[2], np.float64)
        chord = np.interp(station[keep], source_arc[first:last], source_z)
        xy, station, normal = xy[keep], station[keep], normal[keep]
        lat, lon, final_h, final_z = _sample(frame, xy, elev)
        _, _, raw_h, raw_z = _sample(frame, xy, raw_elev)
        drawn = np.asarray(ground(xy[:, 0], xy[:, 1]), np.float64) if ground is not None else None
        width = spec.width if spec.mountain else max(spec.width,
            roads.carriageway(spec.cls, spec.kind, True, None, spec.oneway))
        core = width / 2 + grading.shoulder(spec)
        side_samples = {}
        for name, offset in (("edge", spec.width / 2), ("shoulder", core)):
            for side, sign in (("left", 1), ("right", -1)):
                points = xy + normal * offset * sign
                _, _, h, z = _sample(frame, points, elev)
                side_samples[name + "_" + side] = {"offset_m": offset, "graded_h_m": h, "graded_z_m": z,
                    "drawn_z_m": np.asarray(ground(points[:, 0], points[:, 1]), np.float64) if ground is not None else None}
        profiles.append({"selected_range": {"radius_m": PROFILE_RADIUS_M, "station_start_m": station[0],
            "station_end_m": station[-1], "samples": len(station)},
            "station_m": station, "east_m": xy[:, 0], "north_m": xy[:, 1],
            "latitude": lat, "longitude": lon, "raw_h_m": raw_h, "raw_z_m": raw_z,
            "graded_h_m": final_h, "graded_z_m": final_z, "source_chord_z_m": chord,
            "source_chord_minus_graded_m": chord - final_z, "drawn_z_m": drawn,
            "cross_sections": side_samples})

    nearby_walls = []
    for wall in walls:
        base = np.asarray(wall.get("base", []), np.float64)
        if len(base) < 2 or np.min(np.hypot(base[:, 0], base[:, 1])) > PROFILE_RADIUS_M:
            continue
        top, foot = np.asarray(wall["top"]), np.asarray(wall["foot"])
        lat, lon, _, graded = _sample(frame, base[:, :2], elev)
        drawn = np.asarray(ground(base[:, 0], base[:, 1]), np.float64) if ground is not None else None
        parapet = bool(wall.get("parapet"))
        nearby_walls.append({"geometry_type": "procedural stone parapet" if parapet else "procedural retaining wall",
            "eligible": True, "rule": "roads._parapets.edge_drop" if parapet else "roads._wall_runs.cut_and_cut_behind.steep",
            "attribution": "supplied nearby wall list; no nearest-road ownership inferred",
            "feature_id": wall.get("fid", wall.get("feature_id")),
            "start_latlon": [lat[0], lon[0]], "end_latlon": [lat[-1], lon[-1]],
            "east_m": base[:, 0], "north_m": base[:, 1], "top_z_m": top, "foot_z_m": foot,
            "height_m": top - foot, "road_z_m": wall.get("road_z"), "graded_ground_z_m": graded,
            "drawn_ground_z_m": drawn})

    poles = []
    for row in props:
        if row[0] not in ("pole", "pylon") or np.hypot(row[1], row[2]) > PROFILE_RADIUS_M:
            continue
        xy = np.asarray([row[1:3]], np.float64)
        lat, lon, _, graded = _sample(frame, xy, elev)
        expected = float(ground(xy[:, 0], xy[:, 1])[0]) if ground is not None else float(graded[0])
        poles.append({"group": row[0], "latitude": lat[0], "longitude": lon[0],
            "east_m": row[1], "north_m": row[2], "object_origin_z_m": row[3],
            "ground_z_m": expected, "origin_to_ground_m": float(row[3]) - expected,
            "comparison_surface": "generated terrain grid" if ground is not None else "graded elevation callback",
            "physical_base_note": "object origin is a base proxy; model-specific origin offsets are not assumed"})

    return _safe({"event": "roadside_elevation", "schema_version": SCHEMA_VERSION, **identity(feats, i),
        "classification": spec.cls, "name": str(feats.attrs.get("name", [""] * len(feats))[i]),
        "spec": dict(spec._asdict()), "frame": {"latitude": frame.lat0, "longitude": frame.lon0, "height_m": frame.h0},
        "height_convention": "all *_z_m values are ENU in frame; *_h_m values are geodetic DEM/elevation heights",
        "profile_scope": "ground road parts only; bridge/tunnel spans excluded, no road-deck height inferred",
        "off_ground_ranges": roads.raw_level_ranges(feats, i),
        "dem_source": dem_source if dem_source is not None else "not supplied",
        "profiles": profiles, "walls": nearby_walls, "wall_eligibility": {
            "generated_nearby_count": len(nearby_walls), "scope": "input wall list, not a fresh wall eligibility run",
            "retaining_cut_threshold_m": grading.WALL_CUT_M, "retaining_steep_threshold": roads.WALL_STEEP,
            "parapet_drop_threshold_m": roads.PARAPET_DROP_MOUNTAIN_M if spec.mountain else roads.PARAPET_DROP_M,
            "parapet_probe_m": roads.PARAPET_PROBE_MOUNTAIN_M if spec.mountain else roads.PARAPET_PROBE_M},
        "poles": poles, "collision_ground": "computed terrain only; live BeamNG collision was not observed",
        "cache": {"status": cache_status, "tile": tile, "facts_format": roads.FACTS_FORMAT,
                  "generation_code": revisit.code_key(), "source_cell": feats.__dict__.get("_bnge_cell")},
        "limitations": ["generation hook does not execute for cached rendered tiles",
                        "nearby walls and poles may belong to other source features",
                        "live terrain LOD/collision transforms require in-game verification"]})


def emit(feats, i, spec, frame, **kwargs):
    if not selected(feats, i):
        return None
    try:
        record = explain(feats, i, spec, frame, **kwargs)
        log.info("%s", json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False))
        return record
    except Exception as exc:
        log.warning("roadside diagnostic failed for %s: %s: %s", str(feats.ids[i]), type(exc).__name__, exc)
        return None
