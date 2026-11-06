"""Opt-in explanations of road decoration policy, never a source of generation decisions.

BNGE_ROAD_DECOR_DEBUG is a comma-separated list of GERS ids or OSM way ids
(123, w123, or w123@4); '*' selects all roads. No sampling or formatting happens
unless a caller first checks ENABLED and selected(). The build_tile hook reports
newly generated content, not cache hits or objects already loaded by the game.
"""
import json
import logging
import math
import os

import numpy as np

log = logging.getLogger("earthd.roaddecor")
SELECTORS = frozenset(v.strip() for v in os.environ.get("BNGE_ROAD_DECOR_DEBUG", "").split(",") if v.strip())
ENABLED = bool(SELECTORS)
SCHEMA_VERSION = 1


def _value(feats, key, i, default=None):
    column = feats.attrs.get(key)
    if column is None:
        return default
    value = column[i]
    return value.item() if isinstance(value, np.generic) else value


def _safe(value):
    """Missing numeric OSM fields are NaN in feature columns, JSON null in logs."""
    if isinstance(value, np.ndarray):
        return _safe(value.tolist())
    if isinstance(value, np.generic):
        return _safe(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    return value


def identity(feats, i):
    import osmtags
    record = str(_value(feats, "src_record", i, ""))
    return {"feature_id": str(feats.ids[i]), "osm_way_id": osmtags.way_id(record),
            "source_record": record, "source_dataset": _value(feats, "src_dataset", i)}


def selected(feats, i):
    """Match a selected feature; absent ids never match a missing/empty source record."""
    if not ENABLED:
        return False
    if "*" in SELECTORS or str(feats.ids[i]) in SELECTORS:
        return True
    ident = identity(feats, i)
    way = ident["osm_way_id"]
    return (ident["source_record"] in SELECTORS or
            (way is not None and (str(way) in SELECTORS or "w" + str(way) in SELECTORS)))


def _decision(eligible, rule, policy_source="inferred", **extra):
    return {"eligible": bool(eligible), "rule": rule, "policy_source": policy_source,
            "object_source": "procedural", **extra}


def explain(feats, i, spec, urban_fn=None, *, region=None, town=False, buried=0.0,
            walks=(), tile=None, cache_status="generated", context=None, placement=None, left_hand=False):
    """JSON-safe evidence for the final spec of one ground-road feature.

    Eligibility is distinct from emitted objects: clipping, tile ownership,
    deterministic street selection, junction/deck/building clearances and later
    pipeline stages can reduce placement. Per-feature placement is unknown unless
    the caller supplies attributed placement data; rows must not be spatially
    guessed back onto parallel roads.
    """
    import furniture
    import roads
    import revisit

    fid = str(feats.ids[i])
    ident = identity(feats, i)
    tags = {key[4:]: _value(feats, key, i) for key in sorted(feats.attrs) if key.startswith("osm_")}
    tag_lit = str(tags.get("lit", "")).strip().lower()
    tag_walk = str(tags.get("sidewalk", "")).strip().lower()
    has_walk = roads.has_sidewalks(spec)
    walk_runs = sum(1 for wk in walks if wk.get("fid") == fid and not wk.get("median"))
    explicit_walk = getattr(spec, "sides", None) is not None
    sidewalk = _decision(has_walk,
                         "osm.sidewalk." + tag_walk if explicit_walk else "roads.has_sidewalks.class_and_urban",
                         "mapped_way_tag" if explicit_walk else "inferred",
                         sides=list(roads.walk_sides(spec, left_hand)) if has_walk else [], actual_run_count=walk_runs)

    highway = spec.cls in furniture.HIGHWAY_CLASSES
    # The same policy helper is used by placement; no duplicated lighting rules.
    light = {**furniture.lighting_eligibility(spec, tag_lit, region=region, has_walk=has_walk),
             "object_source": "procedural"}
    light.update({"osm_lit": tag_lit or None, "region": region,
                  "rural_ramp_partial": bool(highway and spec.link and not spec.urban and tag_lit != "yes"),
                  "mapped_point_features": "not consumed by this road furniture path"})

    tree_class = spec.cls in furniture.STREET_TREE_CLASSES
    tree_hash = furniture._h01(fid, "trees") < furniture.STREET_TREE_SHARE
    tree = _decision(has_walk and spec.urban and tree_class and tree_hash,
                     "furniture.place.urban_sidewalk_tree_class_and_hashed_street",
                     class_eligible=tree_class, street_selected=tree_hash,
                     mapped_vegetation="independent canopy/vegetation pipeline; unaffected by this rule")
    bench = _decision(has_walk and spec.urban and spec.cls in furniture.MAJOR,
                      "furniture.place.urban_major_sidewalk")

    if context is None and urban_fn is not None:
        helper = getattr(roads, "urban_context", None)
        if helper is not None:
            context = helper(feats, urban_fn)[i]
    if context is None:
        context = {"evidence": "not sampled", "urban_fn_present": urban_fn is not None}
    context = {**context, "final_urban": bool(spec.urban),
               "administrative_boundaries": "not used for urban eligibility",
               "land_use_polygons": "not used for urban eligibility",
               "building_density": "not used for near-road urban eligibility",
               "town": bool(town), "town_meaning": "tile is below dense-core building coverage; also true in countryside"}
    return _safe({"event": "road_decoration", "schema_version": SCHEMA_VERSION, **ident,
            "name": _value(feats, "name", i), "classification": spec.cls,
            "tags": tags, "spec": dict(spec._asdict()), "context": context,
            "eligibility": {"sidewalk": sidewalk, "streetlight": light,
                            "decorative_tree": tree, "bench_and_bin": bench},
            "placement": placement if placement is not None else {
                "status": "per-object attribution not supplied",
                "path": "roads.build_tile -> furniture.place -> roads.write_tile -> trees.lua",
                "filters": ["walk cuts", "tile ownership", "junction clearance", "road clearance",
                            "under-deck clearance", "later building clearance"]},
            "cache": {"status": cache_status, "facts_format": roads.FACTS_FORMAT,
                      "generation_code": revisit.code_key(),
                      "tile": tile, "source_cell_identity": feats.__dict__.get("_bnge_cell"),
                      "note": "generation hook does not run for a cached rendered tile"},
            "utility_poles": {"overhead_selected": furniture._h01(fid, "buried") >= buried,
                              "buried_share": buried, "mountain_excluded": bool(getattr(spec, "mountain", False))}})


def emit(feats, i, spec, urban_fn=None, **kwargs):
    """Log selected diagnostics without risking generation if development sampling fails."""
    if not selected(feats, i):
        return None
    try:
        record = explain(feats, i, spec, urban_fn, **kwargs)
        log.info("%s", json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False))
        return record
    except Exception as exc:
        log.warning("road_decoration diagnostic failed for %s: %s: %s", str(feats.ids[i]), type(exc).__name__, exc)
        return None
