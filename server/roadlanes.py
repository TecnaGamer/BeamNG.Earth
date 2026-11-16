"""Lane evidence shared by pavement, structures, markings and navigation.

Adapted from the 1.7.0 custom build. Highway counts come from the source way,
an unambiguous current geometry match, or agreeing connected approaches. A
nearby road's name alone is not evidence for a highway carriageway's count.
"""
import numpy as np
import dnp

HIGHWAYS = {"motorway", "trunk"}
MAX_LANES = 12


def count(value, minimum=1):
    """A whole lane count, not a prefix of a list, fraction or conditional tag."""
    try:
        n = float(value)
        return int(n) if np.isfinite(n) and n.is_integer() and minimum <= n <= MAX_LANES else None
    except (TypeError, ValueError):
        return None


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
                "_lane_flow_home", "_lane_shifts", "_road_tapers", "_deck_tapers", "_lane_overrides", "_ramp_junction",
                "_placement_rows", "_structure_w", "_structure_w_bridges", "_level_layer", "_spec_inputs",
                "_map_surface_values", "_lengths_all", "_raw_flat", "_raw_flat_kinds", "_lane_section_joints"):
        feats.__dict__.pop(key, None)


def _project(line, points):
    delta = np.diff(line, axis=0)
    length = np.maximum(dnp.norm(delta, axis=1), 1e-9)
    t = np.clip(((points[:, None] - line[None, :-1]) * delta).sum(2) / (length * length), 0., 1.)
    q = line[None, :-1] + t[:, :, None] * delta
    dist = dnp.norm(points[:, None] - q, axis=2)
    k = dist.argmin(1)
    return dist[np.arange(len(points)), k], delta[k] / length[k, None]


def join(feats, tags, ids, geometry=None):
    """Keep highway evidence separate from osmtags' legacy named-street fallback. geometry() -> {way id: (k, 2)
    lon/lat} of the tagged highways (osmtags.OsmTags.highway_geometry), asked only when a motorway / trunk's own way
    id is not among the tags (replaced since the Overture release): its count from a way along the same line.
    geometry(need): need, the lon/lat boxes (lon0, lat0, lon1, lat1) of those highways."""
    n = len(feats)
    if geometry is not None:
        classes_ = feats.attrs.get("class", [""] * n)
        need = []           # (lon0, lat0, lon1, lat1) of each highway whose own way is not among the tags
        for i in range(n):
            if classes_[i] in HIGHWAYS and ids[i] not in tags:
                parts = feats.parts(i)
                if len(parts) == 1 and len(parts[0]) == 1 and len(parts[0][0]) >= 2:
                    ll = np.asarray(parts[0][0], np.float64)
                    need.append((float(ll[:, 0].min()), float(ll[:, 1].min()), float(ll[:, 0].max()),
                                 float(ll[:, 1].max())))
        if need:
            try:
                for wid, p in (geometry(need) or {}).items():
                    if wid in tags:
                        tags[wid]["_coords"] = p
            except Exception as e:      # (the counts from the ways' own ids only)
                import logging
                logging.getLogger("earthd.roadlanes").warning("highway geometry: %s: %s", type(e).__name__, e)
    counts = np.array([count(v) or np.nan for v in feats.attrs.get("osm_lanes", [None] * n)])
    sources = ["osm-way" if np.isfinite(v) else "" for v in counts]
    matched = np.array([w or 0 for w in ids], dtype=np.int64)
    classes = feats.attrs.get("class", [""] * n)
    origin = feats.lonlat[0] if len(feats.lonlat) else np.zeros(2)
    scale = np.array([111320. * dnp.cos(np.radians(origin[1])), 110574.])
    candidates = []
    for wid, tag in tags.items():
        p, lanes, cls = tag.get("_coords"), count(tag.get("lanes")), (tag.get("lane_hw") or tag.get("_highway") or "")
        if lanes is None or p is None or len(p) < 2 or cls.removesuffix("_link") not in HIGHWAYS:
            continue
        p = (np.asarray(p) - origin) * scale
        p = p[np.r_[True, dnp.norm(np.diff(p, axis=0), axis=1) > .01]]
        if len(p) < 2:
            continue
        ow = str(tag.get("osm_oneway", ""))
        direction = -1 if ow == "-1" else 1 if ow in ("yes", "1", "true") else 0
        candidates.append((wid, lanes, cls.removesuffix("_link"), cls.endswith("_link"), direction,
                           p, p.min(0) - 6., p.max(0) + 6.))
    for i in range(n):
        if classes[i] not in HIGHWAYS:
            continue
        counts[i] = count(tags.get(ids[i], {}).get("lanes")) or np.nan
        sources[i] = "osm-way" if np.isfinite(counts[i]) else ""
        if sources[i] or not candidates:
            continue
        parts = feats.parts(i)
        if len(parts) != 1 or len(parts[0]) != 1:
            continue
        line = (parts[0][0] - origin) * scale
        if len(line) < 2:
            continue
        line = line[np.r_[True, dnp.norm(np.diff(line, axis=0), axis=1) > .01]]
        if len(line) < 2:
            continue
        s = np.r_[0., np.cumsum(dnp.norm(np.diff(line, axis=0), axis=1))]
        samples = np.column_stack([np.interp(np.linspace(0., s[-1], 9), s, line[:, d]) for d in (0, 1)])
        _, tangent = _project(line, samples)
        direction = rule(feats, "oneway", i)
        direction = 0 if direction is None else int(np.sign(direction))
        matches = []
        for wid, lanes, cls, link, od, p, lo, hi in candidates:
            if (cls != classes[i] or link != is_link(feats, i) or bool(od) != bool(direction)
                    or np.any(samples < lo) or np.any(samples > hi)):
                continue
            distance, heading = _project(p, samples)
            alignment = (tangent * heading).sum(1)
            alignment = alignment * direction * od if direction else np.abs(alignment)
            if distance.max() <= 6. and distance.mean() <= 3. and alignment.min() >= .9:
                matches.append((float(distance.mean()), wid, lanes))
        matches.sort()
        if matches and not any(m[2] != matches[0][2] and m[0] < matches[0][0] + 1. for m in matches[1:]):
            _, matched[i], counts[i] = matches[0]
            sources[i] = "osm-geometry"
    feats.attrs["lane_count"] = counts
    feats.attrs["lane_source"] = np.array(sources, dtype="<U16")
    feats.attrs["lane_osm_way"] = matched
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
    import rampoverrides
    for i, lanes in rampoverrides.lane_counts(feats).items():
        counts[i] = lanes
    feats._generated_lanes = counts
    return counts
