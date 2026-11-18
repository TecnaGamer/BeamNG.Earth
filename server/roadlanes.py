"""Lane evidence shared by pavement, structures, markings and navigation.

Adapted from the 1.7.0 custom build. Highway counts come from the source way,
an unambiguous current geometry match, or agreeing connected approaches. A
nearby road's name alone is not evidence for a highway carriageway's count.
"""
import numpy as np
import dnp

import fast

HIGHWAYS = {"motorway", "trunk"}
MAX_LANES = 12
_ef_lanes = fast.native(456, "roadlanes")


def count(value, minimum=1):
    """A whole lane count, not a prefix of a list, fraction or conditional tag: float(value) (None where it raises
    TypeError / ValueError) finite, whole and minimum..MAX_LANES, as an int; else None (earthfast
    lanesjoin::lane_count; minimum an int)."""
    return _ef_lanes.lane_count(value, minimum)


def rule(feats, name, i):
    table = feats.rules.get(name)
    j = table.major(i) if table is not None else -1
    return table.value[j] if j >= 0 else None


def directions(total, osm=None):
    """(forward, backward, shared turn lanes) consistent with a two-way total.

    Incomplete directional tags can supply the missing side, but conflicting
    tags cannot invent lanes. An odd unqualified total keeps the extra lane
    along the geometry; a mapped shared turn lane is excluded from through AI.
    """
    osm = osm or {}
    both = count(osm.get("lanes:both_ways"), 0) or 0
    if total - both < 2:
        both = 0
    through = total - both
    fwd, bwd = count(osm.get("lanes:forward")), count(osm.get("lanes:backward"))
    if fwd is not None and bwd is None and fwd < through:
        bwd = through - fwd
    elif bwd is not None and fwd is None and bwd < through:
        fwd = through - bwd
    if fwd is None or bwd is None or fwd + bwd != through:
        fwd, bwd = max(1, (through + 1) // 2), max(1, through // 2)
    return fwd, bwd, both


def is_link(feats, i):
    subclasses = feats.attrs.get("subclass")
    return (rule(feats, "subclass", i) == "link"
            or (subclasses is not None and str(subclasses[i]) == "link"))


def invalidate(feats):
    """Enrichment can arrive after specs were requested; discard derived facts.

    Keep 1.8's cell identity and neighbour hook: they make lane-flow and deck
    joints consistent across cell boundaries. Evict this cell's in-memory
    facts too, so a reloaded feature cannot resurrect its previous widths.
    """
    import roads
    feats._road_revision = getattr(feats, "_road_revision", 0) + 1
    cid = feats.__dict__.get("_bnge_cell")
    if cid is not None:
        with roads._FACTS_LOCK:
            for key in list(roads._FACTS_MEM):
                if key[0] == cid:
                    roads._FACTS_MEM.pop(key, None)
    for key in ("_road_specs", "_sign_specs", "_sign_rows", "_bridge_specs", "_tunnel_specs", "_generated_lanes",
                "_raw_level_ranges", "_level_ranges", "_ranges_state", "_lane_flow_rows", "_lane_flow",
                "_lane_flow_home", "_ramp_rule_home", "_lane_shifts", "_road_tapers", "_deck_tapers", "_lane_overrides", "_ramp_junction",
                "_placement_rows", "_structure_w", "_structure_w_bridges", "_level_layer", "_spec_inputs",
                "_map_surface_values", "_lengths_all", "_raw_flat", "_raw_flat_kinds", "_lane_section_joints"):
        feats.__dict__.pop(key, None)


def _direction(feats, i):
    """The one-way sign of road i (0: two-way), as join's geometry match reads it."""
    d = rule(feats, "oneway", i)
    return 0 if d is None else int(np.sign(d))


def join(feats, tags, ids, geometry=None):
    """Keep highway evidence separate from osmtags' legacy named-street fallback. geometry() -> {way id: (k, 2)
    lon/lat} of the tagged highways (osmtags.OsmTags.highway_geometry), asked only when a motorway / trunk's own way
    id is not among the tags (replaced since the Overture release): its count from a way along the same line.
    geometry(need): need, the lon/lat boxes (lon0, lat0, lon1, lat1) of those highways. The attrs lane_count,
    lane_source and lane_osm_way (earthfast lanesjoin: lanes_need, then lanes_join's OSM counts and geometry match).
    Input contract (R2a): feats.attrs an exact dict of contract columns, tags osm_tags' exact dict of int way ids ->
    exact dicts ("_coords" a float64 (k, 2) array), ids an exact list of len(feats) ints / None, finite lon / lat;
    anything else TypeError."""
    n = len(feats)
    hw = sorted(HIGHWAYS)
    off = (feats.part_off, feats.ring_off, feats.coord_off, feats.lonlat)
    if geometry is not None:
        need = _ef_lanes.lanes_need(feats.attrs, tags, ids, n, *off, hw)
        if need:
            try:
                for wid, p in (geometry(need) or {}).items():
                    if wid in tags:
                        tags[wid]["_coords"] = p
            except Exception as e:      # (the counts from the ways' own ids only)
                import logging
                logging.getLogger("earthd.roadlanes").warning("highway geometry: %s: %s", type(e).__name__, e)
    _ef_lanes.lanes_join(feats.attrs, tags, ids, n, *off, hw, lambda i: _direction(feats, i),
                         lambda i: is_link(feats, i))
    invalidate(feats)


def ucs4(values):
    """str(v) of each value as a C-contiguous uint32 array (n, w) of code points, zero-padded (a NumPy str array's own
    buffer where it is one): the kernels compare these as the strings."""
    a = np.asarray(values)
    if a.dtype.kind != "U" or a.dtype.byteorder == ">":
        a = a.astype(str)
    a = np.ascontiguousarray(a.ravel())
    return a.view(np.uint32).reshape(len(a), a.dtype.itemsize // 4)


def _equals(col, n, values):
    """Per feature: attrs column col's value is one of values (False all along without the column)."""
    out = np.zeros(n, bool)
    if col is not None and n:
        c = np.asarray(col)
        for v in values:
            out |= c == v
    return out


def links(feats):
    """is_link of every feature, as one bool array."""
    n = len(feats)
    out = np.zeros(n, bool)
    table = feats.rules.get("subclass")
    if table is not None and len(table.feature) and n:
        maj = table.majors(n)
        hit = np.asarray(table.value).astype(str) == "link"
        out |= (maj >= 0) & hit[np.maximum(maj, 0)]
    sub = feats.attrs.get("subclass")
    if sub is not None and n:
        out |= np.asarray(sub).astype(str) == "link"
    return out


def generated_counts(feats):
    """Validated mapped counts and bounded highway gaps; defaults stay in roads. The map's count of each feature (its
    way's, a geometry match's, the lane tags' total or its direction's), then the gaps of a highway chain between two
    pieces whose counts agree (the chain's ends matched where they meet aligned, by flow and connectors): one
    earthfast.generated_lanes call (the Python was ~8 ms per Manhattan roads layer, 2026-11-08; its reference:
    tests/lanes_reference.py). Remembered on feats."""
    saved = feats.__dict__.get("_generated_lanes")
    if saved is not None:
        return saved
    import fast
    fast.require(169, "generated_lanes")
    n = len(feats)
    a = feats.attrs
    oneway = feats.rules.get("oneway")
    dirs, has = np.zeros(n), np.zeros(n, bool)
    if oneway is not None and len(oneway.feature) and n:
        maj = oneway.majors(n)
        has = maj >= 0
        dirs = np.where(has, np.asarray(oneway.value, np.float64)[np.maximum(maj, 0)], 0.0)
    hw = np.where(_equals(a.get("class"), n, ("motorway",)), 1, 0) + np.where(_equals(a.get("class"), n, ("trunk",)),
                                                                              2, 0)
    eligible = (hw > 0) & ~links(feats) & ~_equals(a.get("osm_junction"), n, ("roundabout", "circular"))
    con = feats.rules.get("connectors")
    if con is not None and len(con.feature):
        con_f = np.ascontiguousarray(con.feature, np.int64)
        con_s = np.ascontiguousarray(con.start, np.float64)
        con_v = ucs4(con.value)
    else:
        con_f, con_s, con_v = np.zeros(0, np.int64), np.zeros(0), np.zeros((0, 1), np.uint32)
    origin = feats.lonlat[0] if len(feats.lonlat) else np.zeros(2)
    scale = np.array([111320. * dnp.cos(np.radians(origin[1])), 110574.])
    got = fast._ef.generated_lanes(
        a.get("lane_count", a.get("osm_lanes")), a.get("osm_lane_total"), a.get("osm_lanes_forward"),
        a.get("osm_lanes_backward"), a.get("osm_lanes_both_ways"), np.ascontiguousarray(dirs, np.float64),
        np.ascontiguousarray(has, bool), np.ascontiguousarray(eligible, bool), np.ascontiguousarray(hw, np.int64),
        np.ascontiguousarray(feats.part_off, np.int64), np.ascontiguousarray(feats.ring_off, np.int64),
        np.ascontiguousarray(feats.coord_off, np.int64), np.ascontiguousarray(feats.lonlat, np.float64).reshape(-1, 2),
        (float(origin[0]), float(origin[1])), (float(scale[0]), float(scale[1])), con_f, con_s, con_v)
    counts = [None if c < 0 else c for c in got.tolist()]
    feats._generated_lanes = counts
    return counts
