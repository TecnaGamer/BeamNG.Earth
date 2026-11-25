"""The degradation test's input filter (adaptive world-generation plan J1, ~/bnge-tests/adaptiveplan/PLAN.md section 6):
a well-mapped place's inputs with data taken away in steps, applied as the map data is read, before anything is built
from it. The output is compared with the same place's full-data build (D0, the same code path with nothing removed).

    D0  full data (the reference)
    D1  no width, lane, sidewalk or parking tags (OSM's width, lanes and lane-level tags, sidewalk; Overture's
        width rules). Trunk reads no parking:* tags: nothing to remove there.
    D2  class only: D1, and no speed, surface, smoothness, lit, maxheight, roundabout, priority or name tags.
        Kept: class and subclass, one-way, bridge / tunnel / layer / level (a divided road is always drawn
        one-way, and a crossing without its structure tags is not a poorly mapped road but a broken one).
    D3  a global 30 m DEM instead of lidar: the elevation capped at Mapterhorn's planet zoom (z12: Copernicus GLO-30
        where it has nothing finer; elsewhere its finer sources averaged to ~20-30 m), lidar road heights, deck
        heights and Canadian / Hong Kong lidar off.
    D4  no building heights or roof tags (height, levels, min height, roof shape / height / direction, on the
        buildings and their parts; BD TOPO's measured heights not joined).
    D5  footprints stripped of all tags: D4, and no class, name, colour, material or level; the building parts
        dropped. Kept: the outline, its id, its source (OSM or machine-learned) and whether it is underground.
    D6  no fine photo: the aerial's road-surface verdicts off (roadsurf) and the photo surface off. The corpus builds
        read no ground photo at all, so the road harness shows no change by construction (a check, not a finding).

Deterministic: each step is a pure function of the inputs (columns blanked, rule tables emptied, tag keys dropped);
no randomness, no ordering by hash. The road layer's OSM tags are removed before they are joined to the Overture
segments (MapData._osm_read, the decode-time join's read), so what is built from them at decode time (the lane sections, one-way joins, Layercake lane
counts) is built without them; the lane shards' columns and Overture's rules are blanked right after their join
(MapData._lanes), before the level ways, lane sections and rounding. The decoded roads must therefore be made again
(the runner removes mapdata/v2/*/roads.* from the build's private copy; the raw inputs re-decode offline) and the
on-disk decoded-roads cache (roadlayercache) is not used.

install(step) patches mapdata.MapData in this process; env(step) is the step's environment (switches read at import).
"""
import os

import numpy as np

STEPS = ("D0", "D1", "D2", "D3", "D4", "D5", "D6")
NAMES = {
    "D0": "full data",
    "D1": "no width, lane, sidewalk, parking tags",
    "D2": "class only",
    "D3": "global 30 m DEM, no lidar",
    "D4": "no building heights or roof tags",
    "D5": "footprints without tags",
    "D6": "no fine photo",
}

# OSM highway tags (osmtags.COLUMNS' names) removed before the join, and the same keys of the appended raceways /
# busways (osmtags.RACEWAY_COLUMNS)
_OSM_D1 = ("lanes", "width", "sidewalk")
_OSM_D2 = _OSM_D1 + ("maxspeed", "lit", "smoothness", "osm_surface", "name", "surface")
# the lane shards' columns (railtags.LANE_COLUMNS / RULE_COLUMNS) blanked after their join
_LANES_KEEP_D1 = ("junction",)
# Overture's rule tables emptied
_RULES_D1 = ("width",)
_RULES_D2 = _RULES_D1 + ("max_speed_kmh", "surface", "max_height_m")
# road attrs blanked in D2 (Overture's own)
_ATTRS_D2 = ("name",)
# building columns
_BLD_HEIGHT = ("height", "num_floors", "num_floors_underground", "min_height", "min_floor", "roof_shape",
               "roof_height", "roof_direction", "roof_orientation", "roof_levels", "height_src")
_BLD_ALL = _BLD_HEIGHT + ("class", "subtype", "name", "facade_color", "facade_material", "roof_color",
                          "roof_material", "has_parts", "level")

_ENV = {
    "D3": {"BNE_DEM_ZOOM": "12", "BNE_HRDEM": "0", "BNE_ROADZ": "off", "BNE_LIDAR_DECKS": "off",
           "BNE_ROADZ_HK": "off", "BNE_HKDTM": "off"},
    "D6": {"BNE_ROADSURF": "off", "BNE_PHOTO_SURFACE": "off"},
}


def env(step):
    """The step's process environment: BNE_DEGRADE (read by tools/widecorpus/build.py) and its switches."""
    if step not in STEPS:
        raise ValueError(f"unknown degradation step {step!r}")
    return {"BNE_DEGRADE": step, **_ENV.get(step, {})}


def osm_keys(step):
    return _OSM_D2 if step == "D2" else _OSM_D1 if step == "D1" else ()


def road_steps(step):
    return step in ("D1", "D2")


def building_columns(step):
    return _BLD_ALL if step == "D5" else _BLD_HEIGHT if step == "D4" else ()


def strip_tags(tags, keys):
    """osm_tags' {way id: {column: value}} without keys (new dicts; the input is not changed). Sorted by way id so the
    result does not depend on the input's order."""
    if not keys:
        return tags
    drop = frozenset(keys)
    return {w: {k: v for k, v in t.items() if k not in drop} for w, t in sorted(tags.items())}


def strip_ways(ways, keys):
    if not keys:
        return ways
    drop = frozenset(keys)
    return [{k: v for k, v in w.items() if k not in drop} for w in ways]


def _blank(col):
    """A column of the same length and kind with every value unknown (NaN, '', None, False, 0), or None when it is
    already so (the caller keeps the same object: memos made on it stay valid)."""
    a = np.asarray(col) if not isinstance(col, np.ndarray) else col
    k = a.dtype.kind
    if k == "f":
        return None if not np.isfinite(a).any() else np.full(a.shape, np.nan, a.dtype)
    if k in "US":
        return None if not (a != "").any() else np.zeros(a.shape, a.dtype)
    if k == "b":
        return None if not a.any() else np.zeros(a.shape, bool)
    if k in "iu":
        return None if not a.any() else np.zeros(a.shape, a.dtype)
    if k == "O":
        return None if all(v is None for v in a.tolist()) else np.full(a.shape, None, object)
    raise TypeError(f"column of dtype {a.dtype}")


def _blank_attrs(feats, names):
    n = 0
    for name in names:
        col = feats.attrs.get(name)
        if col is None:
            continue
        got = _blank(col)
        if got is not None:
            feats.attrs[name] = got
            n += 1
    return n


def _empty_rule(rule):
    import mapgeom
    return mapgeom.RuleTable(np.asarray(rule.feature)[:0], np.asarray(rule.value)[:0], np.asarray(rule.start)[:0],
                             np.asarray(rule.end)[:0])


def lane_attrs(step):
    """The lane shards' attrs blanked after their join."""
    import railtags
    if step not in ("D1", "D2"):
        return ()
    keep = () if step == "D2" else _LANES_KEEP_D1
    out = [railtags.lane_attr(k) for k in railtags.LANE_COLUMNS if k not in keep]
    out.append(railtags.LANE_PARTS)
    if step == "D2":
        out += [railtags.lane_attr(k) for k in railtags.RULE_COLUMNS]
    return tuple(out)


def strip_roads(feats, step):
    """A roads layer's lane-shard columns blanked and Overture's rules emptied, in place (idempotent)."""
    if feats is None or not road_steps(step):
        return feats
    _blank_attrs(feats, lane_attrs(step) + (_ATTRS_D2 if step == "D2" else ()))
    for name in (_RULES_D2 if step == "D2" else _RULES_D1):
        rule = feats.rules.get(name)
        if rule is not None and len(rule.feature):
            feats.rules[name] = _empty_rule(rule)
    return feats


def strip_buildings(feats, step):
    """A buildings / building parts layer's columns blanked, in place (idempotent)."""
    cols = building_columns(step)
    if feats is not None and cols:
        _blank_attrs(feats, cols)
    return feats


def install(step):
    """Patch mapdata.MapData (this process) so every map cell is read with step's data removed."""
    import mapdata
    if step not in STEPS:
        raise ValueError(f"unknown degradation step {step!r}")
    MD = mapdata.MapData
    if getattr(MD, "_j1_step", None) is not None:
        raise RuntimeError("degrade.install called twice")
    MD._j1_step = step
    keys = osm_keys(step)
    orig_osm_read = MD._osm_read
    orig_lanes = MD._lanes
    orig_decoded = MD._decoded_layer
    orig_bdtopo = MD._bdtopo
    orig_collect = MD._cell_collect

    def _osm_read(self, key, kind):
        # (the decode-time join reads a cell's OSM tags here since roads 10, osmtags joined at decode: stripped before
        # they reach the lane sections and one-way joins, as the bake-time join's input was before)
        tags, ways, geometry, src = orig_osm_read(self, key, kind)
        return strip_tags(tags, keys), strip_ways(ways, keys), geometry, src

    def _lanes(self, key, layer, feats, *args, **kwargs):
        out = orig_lanes(self, key, layer, feats, *args, **kwargs)
        if layer == "roads":
            strip_roads(feats, step)
        return out

    def _dec_key(self, key, layer, arrays):
        return None, {}             # (the decoded roads are made in this process, after the filter: never from disk)

    def _decoded_layer(self, key, layer, *args, **kwargs):
        entry, hit, dt = orig_decoded(self, key, layer, *args, **kwargs)
        if layer in ("buildings", "building_parts") and building_columns(step):
            strip_buildings(entry[0], step)     # (in place: the remembered copy too)
        return entry, hit, dt

    def _cell_collect(self, state):
        cd = orig_collect(self, state)
        if step == "D5" and cd.layers.get("building_parts") is not None:
            cd.layers["building_parts"] = None      # (the parts dropped: a footprint alone)
        return cd

    def _bdtopo(self, key, layer, feats):
        if building_columns(step) and feats is not None:
            feats.__dict__["_bdtopo"] = False       # (measured heights: not joined)
            return feats
        return orig_bdtopo(self, key, layer, feats)

    MD._osm_read = _osm_read
    MD._lanes = _lanes
    MD._dec_key = _dec_key
    MD._decoded_layer = _decoded_layer
    MD._bdtopo = _bdtopo
    MD._cell_collect = _cell_collect


def active():
    """The step this process builds (BNE_DEGRADE), or None."""
    v = os.environ.get("BNE_DEGRADE")
    return v or None
