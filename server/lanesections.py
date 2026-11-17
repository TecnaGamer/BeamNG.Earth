"""Preserve lane/placement changes hidden inside merged highway features (from PR #880 by DerEifrige-f).

Overture merges the OpenStreetMap ways between two junctions into one segment; a motorway's ways there can hold 3 lanes
and then 4 (an auxiliary lane opening before an exit). The segment had one lane count: one width. split() cuts it
where its ways' lane counts change, into sections with their own counts (their own widths), before any geometry
consumer sees the roads and before the bends are rounded (mapdata: levelways, lane sections, roadsmooth). Sections
retain linear-referenced rules and connector identities; painting, grading, structures and traffic consequently consume
the same physical carriageway centre lines. Where the auxiliary lane's placement=* keeps the through lanes on one grid,
the section's line moves out to its placement, eased so the through edge runs straight and the added lane's edge
tapers (roads._section_tapers); its placement is then in its line (roads.Placement reads it at its ends as an end
shift: its neighbours meet it there). A segment whose ways all hold one count is left to roads.Placement.
No geographic or feature-ID overrides. BNE_LANE_SECTIONS=off: none (as before).
"""
import os
import re

import numpy as np

import fast
import dnp
import geodesy
import mapgeom
import railtags

ON = os.environ.get("BNE_LANE_SECTIONS", "on").lower() not in ("off", "0", "false")
VERSION = 3                 # (mapdata.versions(): the derived caches are built again when it changes; 2: a link's
                            # mouth on its bridge or tunnel stays on its joint, _structure_mouth; 3: every section
                            # carries its road's most lanes, LANE_MAX)
PROFILE = "lane_section_profile"
LANE_MAX = "lane_evidence_max"  # attrs: the most lanes the map's lane data gave the road before it was split
                                # (roads._lane_evidence of the unsplit layer): its mapped width is judged by them
                                # (roads.width_rejected), not by the one section's count


def _ease(s, length, reach, ends):
    """The eased share of a section's change at stations s (earthfast roadnet::lanesplit::lane_ease)."""
    fast.require(383, "lane_ease")
    return fast._ef.lane_ease(np.ascontiguousarray(s, np.float64).reshape(-1), float(length),
                              (float(reach[0]), float(reach[1])), (float(ends[0]), float(ends[1])))


def _offset(ll, offsets):
    """The lon / lat line ll moved offsets m (one, or one per point) to its left, in the ENU frame at its first point
    (earthfast roadnet::lanesplit::lane_offset)."""
    fast.require(383, "lane_offset")
    return fast._ef.lane_offset(np.ascontiguousarray(np.asarray(ll, np.float64)[:, :2]),
                                np.ascontiguousarray(np.atleast_1d(np.asarray(offsets, np.float64)).reshape(-1)))


def _cols(f):
    """Layer f's columns as roadnet::lanesplit reads them."""
    import roads
    a = f.attrs
    return ([str(x) for x in f.ids.tolist()], np.ascontiguousarray(f.kind, np.uint8),
            np.ascontiguousarray(f.part_off, np.int64), np.ascontiguousarray(f.ring_off, np.int64),
            np.ascontiguousarray(f.coord_off, np.int64), np.ascontiguousarray(f.lonlat, np.float64).reshape(-1, 2),
            a.get("class"), f.rules.get("oneway"), f.rules.get("subclass"), a.get("subclass"), a.get("osm_lanes"),
            a.get(railtags.lane_attr("lanes")), a.get(railtags.LANE_PARTS),
            {str(k): float(v) for k, v in roads.LANE_W.items()}, float(roads.LANE_W_DEFAULT),
            (float(roads.SHOULDER_M), float(roads.SHOULDER_WIDE_M), int(roads.SHOULDER_WIDE_LANES)))


def _joint_specs(f):
    """{(lon, lat) rounded to 6 places: [(tangent x, tangent y, target m, tail, class, turning)]}: every lane section's
    auxiliary lane at its own ends where its placement and turn lanes put the lane's edge (a slip road's mouth there
    moves out to it: split). Kept on f (earthfast roadnet::lanesplit::lane_joint_specs)."""
    cached = f.__dict__.get("_lane_section_joints")
    if cached is not None:
        return cached
    fast.require(383, "lane_joint_specs")
    joints = f.__dict__["_lane_section_joints"] = fast._ef.lane_joint_specs(*_cols(f))
    return joints


def _structure_mouth(f, i, end):
    """Whether link i's mouth (its end 0 / -1) is on a bridge or tunnel range the map gives it (roads.tag_level_ranges):
    a deck's (a tube's) end at the joint it leaves its road from. Moved out to its lane, the deck's end left the joint the
    chain solver and the grading join decks and roads at (roads.BridgeProfiles: END_JOIN_*, PIN_M; grading
    APPROACH_TOUCH_M): the exit ramp's deck at the A7's junction with the B 286 at Schweinfurt (2026-11-16) began 4 m off
    the A7's joint, its approach was raised 3.2 m for a deck taken to start on the ground, and the A7's deck beside it
    went up 2 m within 2 m of its abutment, a wall at 116 km/h. Such a mouth stays on its joint, as without the
    sections (BNE_LANE_SECTIONS=off); a mouth on the ground still goes out to its lane."""
    import roads
    at = 0. if end == 0 else 1.
    return any(a - 1e-6 <= at <= b + 1e-6 for a, b, _kind in roads.tag_level_ranges(f, i))


def split(f, frame=None, context=None):
    """New Features where merged highways' lane counts change; identity if none.

    Only explicit per-source counts override the primary count. Missing parts
    keep the primary evidence; two-way roads and ambiguous references stay intact.
    """
    if f is None or not len(f) or PROFILE in f.attrs:
        return f
    # (the sections, their lines and profiles and the slip roads' mouths: earthfast roadnet::lanesplit; None: the
    # layer as it is)
    fast.require(383, "lane_split_jobs")
    jobs = fast._ef.lane_split_jobs(*_cols(f), f, context, _joint_specs, lambda i, end: _structure_mouth(f, i, end))
    if jobs is None:
        return f
    import roads
    old = np.array([r[1] for r in jobs], np.int64)
    attrs = {k: np.asarray(v)[old].copy() for k, v in f.attrs.items()}
    attrs[PROFILE] = np.full(len(jobs), None, object)
    # (each section, and every road kept whole, with its road's most lanes before the split clears its lane parts)
    attrs[LANE_MAX] = np.ascontiguousarray(roads._lane_evidence(f)[old], np.int64)
    # Object columns permit new tag strings longer than the dominant source's string.
    for key in railtags.LANE_COLUMNS:
        attrs[railtags.lane_attr(key)] = np.asarray(attrs.get(railtags.lane_attr(key), [None] * len(jobs)), object)
    attrs["src_record"] = np.asarray(attrs.get("src_record", [""] * len(jobs)), object)
    attrs["osm_lanes"] = np.asarray(attrs.get("osm_lanes", [np.nan] * len(jobs)), np.float64)
    attrs[railtags.LANE_PARTS] = np.asarray(attrs.get(railtags.LANE_PARTS, [None] * len(jobs)), object)
    # the new layer's geometry and rule rows gathered natively (lanesections.rs, kernel 235): each job a copy of its
    # feature, or of its section's (or moved mouth's) line, and the rows over it
    fast.require(235, "lane_split_coords")
    moved = np.array([r[4] is not None for r in jobs], bool)
    lines = [np.asarray(r[4], np.float64).reshape(-1, 2) for r in jobs if r[4] is not None]
    p_off = np.r_[0, np.cumsum([len(r[4]) if r[4] is not None else 0 for r in jobs])].astype(np.int64)
    po, ro, co, coords = fast._ef.lane_split_coords(
        np.ascontiguousarray(f.part_off, np.int64), np.ascontiguousarray(f.ring_off, np.int64),
        np.ascontiguousarray(f.coord_off, np.int64), np.ascontiguousarray(f.lonlat, np.float64).reshape(-1, 2), old,
        moved, np.ascontiguousarray(np.concatenate(lines) if lines else np.zeros((0, 2)), np.float64), p_off)
    if not len(coords):
        coords = np.zeros(0, np.float64)
    ja = np.array([r[2] for r in jobs], np.float64)
    jb = np.array([r[3] for r in jobs], np.float64)
    extra = []                                      # (job, connector id, at) of the sections' cut points
    for z, (_fid, i, a, b, p, tags, n, prof) in enumerate(jobs):
        if prof is None:
            continue
        for at, value in ((a, 0.), (b, 1.)):
            if 0. < at < 1.:
                extra.append((z, f"lane:{f.ids[i]}:{round(at * 1e9)}", value))
        attrs[PROFILE][z] = prof
        attrs["osm_lanes"][z] = n
        # Preserve the section's tags, rather than repeating the dominant way's turn arrows.
        for key in railtags.LANE_COLUMNS:
            attrs[railtags.lane_attr(key)][z] = tags.get(key)
        attrs[railtags.lane_attr("lanes")][z] = n
        if railtags.lane_attr("placement") in attrs:
            attrs[railtags.lane_attr("placement")][z] = None     # (in its line: prof["end_off"])
        attrs[railtags.LANE_PARTS][z] = None
    tables = {}
    for name in list(f.rules) + ([] if "connectors" in f.rules else ["connectors"]):
        table = f.rules.get(name)
        if table is not None and len(table):
            rows, zs, us, vs = fast._ef.lane_split_rules(
                np.ascontiguousarray(table.feature, np.int64), np.ascontiguousarray(table.start, np.float64),
                np.ascontiguousarray(table.end, np.float64), old, ja, jb)
            values = np.asarray(table.value)[rows]
        else:
            rows = zs = np.zeros(0, np.int64)
            us = vs = np.zeros(0)
            values = None
        if name == "connectors":
            if extra:           # (after each job's own rows, in job order)
                ez = np.array([e[0] for e in extra], np.int64)
                order = np.lexsort((np.r_[np.arange(len(zs)), np.arange(len(ez))],
                                    np.r_[np.zeros(len(zs), np.int64), np.ones(len(ez), np.int64)], np.r_[zs, ez]))
                vals = np.empty(len(zs) + len(ez), object)
                if len(zs):
                    vals[:len(zs)] = list(values)
                vals[len(zs):] = [e[1] for e in extra]
                zs = np.r_[zs, ez][order]
                us = np.r_[us, [e[2] for e in extra]][order]
                vs = np.r_[vs, [e[2] for e in extra]][order]
                values = vals[order]
            if len(zs):
                values = np.array(list(values), dtype=str)
        if not len(zs):
            tables[name] = f.rules[name] if name in f.rules and not len(f.rules[name]) else mapgeom.RuleTable(
                np.zeros(0, np.int32), np.zeros(0, str), np.zeros(0), np.zeros(0))
            continue
        tables[name] = mapgeom.RuleTable(zs.astype(np.int32), values, np.asarray(us, np.float64),
                                        np.asarray(vs, np.float64))
    out = mapgeom.Features(f.layer, np.array([r[0] for r in jobs]), f.kind[old], po, ro, co, coords,
                           own=None if f.own is None else f.own[old],
                           clipped=None if f.clipped is None else f.clipped[old], attrs=attrs, rules=tables)
    # Join versions are retained; geometry/specification memos intentionally are not.
    for key in ("_lanes_built", "_rails_built"):
        if key in f.__dict__:
            out.__dict__[key] = f.__dict__[key]
    return out.localize(frame) if frame is not None else out


def profile(f, i):
    """Feature i's lane section profile (split), None for any other road."""
    col = f.attrs.get(PROFILE)
    return col[i] if col is not None else None


def replaced_roads(cells):
    """Unsplit IDs fully covered by intact lane sections in these cells.

    Neighbouring cells can have different lane-tag coverage. Keep the original if
    only part of its replacement is available; otherwise it duplicates the sections.
    """
    intervals = {}
    for f in cells:
        if f is None or PROFILE not in f.attrs:
            continue
        for i, fid in enumerate(f.ids):
            if not profile(f, i) or (f.clipped is not None and f.clipped[i]):
                continue
            match = re.fullmatch(r"(.+)~lanes(\d+)-(\d+)", str(fid))
            if match:
                parent, a, b = match.groups()
                intervals.setdefault(parent, set()).add((int(a), int(b)))
    covered = set()
    for parent, spans in intervals.items():
        end = 0
        for a, b in sorted(spans):
            if a > end:
                break
            end = max(end, b)
        if end == 1000000000:
            covered.add(parent)
    return covered


def end_shift(f, i, e):
    """m left of its geometry's raw line the lanes of section i stand at its end e (0 start, 1 end): its placement,
    in its line already (roads._end_shift); None for any other road."""
    prof = profile(f, i)
    if not prof:
        return None
    return float(prof.get("end_off", (0., 0.))[1 if e else 0])


def growth(prof, lon, lat):
    """Evaluate the same source-station profile on clipped/resampled geometry (each point's station: its nearest spot
    on the section's line, earthfast.polyline_project)."""
    if not any(prof["reach"]):
        return np.ones(np.shape(lon), np.float64)
    import fast
    ll = np.array(prof["line"], np.float64)
    fr = geodesy.EnuFrame(float(ll[0, 1]), float(ll[0, 0]), 0.)
    e, n, _ = fr.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.)
    pe, pn, _ = fr.geodetic_to_enu(np.ravel(lat), np.ravel(lon), 0.)
    line = np.ascontiguousarray(np.c_[e, n], np.float64)
    if len(line) < 2:
        return np.ones(np.shape(lon), np.float64)
    fast.require(164, "polyline_project")
    at = fast._ef.polyline_project(line, np.ascontiguousarray(np.c_[np.ravel(pe), np.ravel(pn)], np.float64))[0]
    # (the line's own metres to its source stations: geodesic, the same within millimetres)
    s = np.asarray(prof["stations"], np.float64)
    arc = np.r_[0., np.cumsum(dnp.hypot(*np.diff(line, axis=0).T))]
    at = np.interp(at, arc, s)
    return _ease(at, prof["length"], prof["reach"], prof["ends"]).reshape(np.shape(lon))


def unplaced(f, i, rings):
    """rings (lon / lat pieces of section i's line: its ground_parts) moved back onto the map's line it was cut from,
    off its placement (split moved a section's line out to where its lanes stand, by its offset eased along it: growth).
    The bridge chain solver's ground roads (roads.BridgeProfiles._cell_entries): its decks are the map's lines, and a
    section's end held its placement off the joint it meets them at. The A7's southbound section ending 2.0 m off its
    junction with the decks over the B 286 at Schweinfurt (2026-11-16) was no ground road going on from that joint
    (roads.PIN_M), so the joint was no abutment: the decks stood 2.8 m over the road at their start, a wall at 116 km/h.
    rings themselves for any other road and a section left in place."""
    prof = profile(f, i)
    if not prof or not (prof.get("extra") or abs(float(prof.get("offset") or 0.)) > 1e-9):
        return rings
    out = []
    for ring in rings:
        ll = np.asarray(ring, np.float64)[:, :2]
        if len(ll) < 2:
            out.append(ring)
            continue
        if prof["extra"]:       # (split's: its reference offset plus its shift eased along it)
            placed = prof["offset"] - prof["shift"] + prof["shift"] * growth(prof, ll[:, 0], ll[:, 1])
        else:
            placed = np.full(len(ll), float(prof["offset"]))
        out.append(_offset(ll, -placed) if np.any(np.abs(placed) > 1e-9) else ring)
    return out


def phase(f, i):
    prof = profile(f, i)
    return prof["phase"] if prof else 0.
