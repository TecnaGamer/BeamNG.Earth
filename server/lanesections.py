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
import math
import os
import re

import numpy as np

import fast
import dmath
import dnp
import geodesy
import mapgeom
import railtags
import roadlanes

ON = os.environ.get("BNE_LANE_SECTIONS", "on").lower() not in ("off", "0", "false")
VERSION = 1                 # (mapdata.versions(): the derived caches are built again when it changes)
PROFILE = "lane_section_profile"
STEP_M = 4.0


def _arc(ll):
    return np.r_[0., np.cumsum(geodesy.segment_lengths(ll))]


def _sample(ll, a, b, dense=True):
    s = _arc(ll)
    u, v = a * s[-1], b * s[-1]
    # Source fractions are stored at lower precision than the vertices.
    # A split within millimetres of a vertex must not create an extra tangent.
    inner = s[(s > u + .01) & (s < v - .01)]
    if dense:
        stations = np.arange(u + STEP_M, v - .01, STEP_M)
        if len(inner):
            k = np.searchsorted(inner, stations)
            clear = np.minimum(abs(stations - inner[np.minimum(k, len(inner) - 1)]),
                               abs(stations - inner[np.maximum(k - 1, 0)])) > .01
            stations = stations[clear]
        inner = np.sort(np.r_[inner, stations])
    if len(inner):
        inner = inner[np.r_[True, np.diff(inner) > .01]]
    at = np.r_[u, inner, v]
    return np.column_stack([np.interp(at, s, ll[:, k]) for k in (0, 1)]), at - u


def _ease(s, length, reach, ends):
    g = np.ones_like(s)
    for end, span in enumerate(reach):
        if span > 0:
            t = np.clip((s if end == 0 else length - s) / span, 0., 1.)
            g = np.minimum(g, ends[end] + (1. - ends[end]) * t * t * (3. - 2. * t))
    return g


def _offset(ll, offsets):
    import roads
    fr = geodesy.EnuFrame(float(ll[0, 1]), float(ll[0, 0]), 0.)
    e, n, _ = fr.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.)
    p = roads.offset_polyline(np.c_[e, n, np.zeros(len(ll))], offsets)
    la, lo, _ = fr.enu_to_geodetic(p[:, 0], p[:, 1], 0.)
    return np.c_[lo, la]


def _placement(tags, n, lane_m, direction):
    v = str(tags.get("placement", ""))
    m = re.fullmatch(r"(middle_of|left_of|right_of):(\d+)", v)
    if not m or not 1 <= int(m[2]) <= n:
        return None
    # OSM lanes are numbered from travel-left, independent of driving side.
    k = int(m[2]) - {"left_of": 1., "middle_of": .5, "right_of": 0.}[m[1]]
    return (k - n / 2.) * lane_m * direction


def _outer_lanes(tags, n):
    """Travel side and count of a contiguous group of exit or merge lanes."""
    vals = [set(v.split(";")) for v in str(tags.get("turn:lanes", "")).split("|")]
    if len(vals) != n:
        return 0, 0
    for side, choices, ordered in ((-1, {"merge_to_left", "slight_right", "right"}, vals[::-1]),
                                   (1, {"merge_to_right", "slight_left", "left"}, vals)):
        count = 0
        for value in ordered:
            if not value & choices or "through" in value:
                break
            count += 1
        if 0 < count < n and all(v <= {"", "none", "through"} for v in ordered[count:]):
            return side, count
    return 0, 0


def _col(f, name, i):
    """f.attrs[name][i], None without the column."""
    col = f.attrs.get(name)
    return col[i] if col is not None else None


def _sections(f, i):
    # (the cheap tests first: every feature of a cell is asked, a layer at each decode; a single way's stretch holds
    # one count, roads.Placement's)
    parts = f.attrs.get(railtags.LANE_PARTS)
    if parts is None or not isinstance(parts[i], (list, tuple)) or len(parts[i]) < 2 or f.kind[i] != mapgeom.LINE:
        return None
    if _col(f, "class", i) not in roadlanes.HIGHWAYS:
        return None
    direction = roadlanes.rule(f, "oneway", i)
    if not direction or roadlanes.is_link(f, i):
        return None
    rings = f.parts(i)
    if len(rings) != 1 or len(rings[0]) != 1 or len(rings[0][0]) < 2:
        return None
    primary = roadlanes.count(_col(f, "osm_lanes", i))
    primary = primary or roadlanes.count(_col(f, railtags.lane_attr("lanes"), i))
    rows = sorted(parts[i], key=lambda r: r[0])
    explicit = [roadlanes.count((r[3] or {}).get("lanes")) for r in rows]
    if primary is None and all(n is not None for n in explicit):
        primary = explicit[max(range(len(rows)), key=lambda k: rows[k][1] - rows[k][0])]
    counts = [n or primary for n in explicit]
    if not primary or any(n is None for n in counts) or float(_arc(rings[0][0])[-1]) < 1.:
        return None
    if len(set(counts)) < 2:
        return None                 # (one count: roads.Placement moves a placed one, as for any road)
    # Reject overlapping/gapped references; do not guess across incomplete source coverage.
    if abs(rows[0][0]) > 1e-6 or abs(rows[-1][1] - 1.) > 1e-6 or any(
            b <= a or (j and abs(a - rows[j - 1][1]) > 1e-6) for j, (a, b, *_rest) in enumerate(rows)):
        return None
    out = []
    for (a, b, way, tags), n in zip(rows, counts):
        tags = dict(tags or {})
        sig = (n, tuple(sorted(tags.items())))
        if out and out[-1][-1] == sig:
            out[-1] = (out[-1][0], b, out[-1][2], tags, n, sig)
        else:
            out.append((a, b, way, tags, n, sig))
    base = min(counts)
    if len(set(counts)) == 1:
        base -= max(_outer_lanes(r[3] or {}, base)[1] for r in rows)
    return out, base, 1 if direction > 0 else -1


def _joint_specs(f):
    cached = f.__dict__.get("_lane_section_joints")
    if cached is not None:
        return cached
    joints = {}
    import roads
    for i in range(len(f)):
        got = _sections(f, i)
        if got is None:
            continue
        rows, base, direction = got
        raw = f.parts(i)[0][0]
        lane_m = roads.LANE_W.get(str(f.attrs["class"][i]), roads.LANE_W_DEFAULT)
        for a, b, _way, tags, n, _sig in rows:
            side, turning = _outer_lanes(tags, n)
            off = _placement(tags, n, lane_m, direction)
            if off is None or not side or not 0 < turning <= n - base:
                continue
            if abs(off - side * direction * (n - base) * lane_m / 2) > .05:
                continue
            for end, at in ((0, a), (-1, b)):
                if at not in (0., 1.):
                    continue
                e = raw[end]
                tangent = raw[1] - raw[0] if end == 0 else raw[-1] - raw[-2]
                tangent = tangent * np.array([dmath.cos(math.radians(e[1])), 1.])
                tangent = tangent / max(dnp.norm(tangent), 1e-12)
                # (the lanes stand off the pavement's centre by roads.driving_offset: a motorway's wider shoulder on
                # the right of travel)
                target = off + side * direction * (n - turning) * lane_m / 2. + _grid(str(f.attrs["class"][i]), n,
                                                                                       lane_m) * direction
                tail = (end == 0) == (direction > 0)
                key = tuple(np.round(e, 6))
                joints.setdefault(key, []).append((tangent, target, tail, str(f.attrs["class"][i]), turning))
    f.__dict__["_lane_section_joints"] = joints
    return joints


def _grid(cls, n, lane_m):
    """m the travel lanes of a one-way carriageway of class cls and n lanes stand left of travel off its pavement's
    centre (roads.driving_offset of its default width: its right shoulder 1.5 x, its left 0.5 x the mean shoulder)."""
    import roads
    if cls != "motorway":
        return 0.
    cap = roads.SHOULDER_WIDE_M if n >= roads.SHOULDER_WIDE_LANES else roads.SHOULDER_M
    return cap / 2.


def _ramp_mouths(f, jobs, context=None):
    """Resolve the physical slip-lane centre from explicit lane evidence.

    A merged feature can extend outside its data cell. Resolve each mouth from
    the data cell containing that mouth, so copies have identical geometry.
    """
    joints = dict(_joint_specs(f))
    if context is not None:
        for _fid, i, _a, _b, p, _tags, _n, _prof in jobs:
            if (p is not None or _col(f, "class", i) not in roadlanes.HIGHWAYS or f.kind[i] != mapgeom.LINE
                    or not roadlanes.is_link(f, i)):
                continue
            parts = f.parts(i)
            if len(parts) != 1 or len(parts[0]) != 1:
                continue
            for node in parts[0][0][[0, -1]]:
                key = tuple(np.round(node, 6))
                # Even a local candidate must use the mouth's owner when this
                # cell contains only some of the other incident roads.
                source = context(float(node[1]), float(node[0]))
                if source is not None:
                    joints[key] = _joint_specs(source).get(key, [])
    if not joints:
        return jobs
    result = []
    for job in jobs:
        fid, i, a, b, p, tags, n, prof = job
        if p is not None or _col(f, "class", i) not in roadlanes.HIGHWAYS or f.kind[i] != mapgeom.LINE:
            result.append(job)
            continue
        direction = roadlanes.rule(f, "oneway", i)
        parts = f.parts(i)
        if p is not None or not direction or not roadlanes.is_link(f, i) or len(parts) != 1 or len(parts[0]) != 1:
            result.append(job)
            continue
        raw = parts[0][0]
        if len(raw) < 2:
            result.append(job)
            continue
        moved = None
        for end in (0, -1):
            candidates = joints.get(tuple(np.round(raw[end], 6)), ())
            if len(candidates) != 1:
                continue
            tangent, target, main_tail, cls, turning = candidates[0]
            ramp_lanes = roadlanes.count(_col(f, "osm_lanes", i)) or \
                roadlanes.count(_col(f, railtags.lane_attr("lanes"), i)) or 1
            if (cls != str(f.attrs["class"][i]) or turning != ramp_lanes or
                    main_tail == ((end == 0) == (direction > 0))):
                continue
            line, stations = _sample(raw, 0., 1.)
            sc = np.array([111320. * dmath.cos(math.radians(raw[end, 1])), 110540.])
            xy = (line - raw[end]) * sc
            dr = (raw[1] - raw[0]) * sc if end == 0 else (raw[-1] - raw[-2]) * sc
            dr = dr / max(dnp.norm(dr), 1e-12)
            dot = float(dnp.dot(dr, tangent))
            if abs(dot) < .85:
                continue
            lateral = tangent[0] * xy[:, 1] - tangent[1] * xy[:, 0]
            from_end = stations if end == 0 else stations[-1] - stations
            reach = min(160., float(stations[-1]) * .45)
            t = np.clip(from_end / max(reach, 1e-9), 0., 1.)
            fade = 1. - t * t * (3. - 2. * t)
            shift = np.sign(target) * np.maximum(abs(target) - lateral * np.sign(target), 0.) * fade / dot
            # Two mouths of the same short link must share one geometry.
            if moved is None:
                moved = line
            moved = _offset(moved, shift)
        result.append((fid, i, a, b, moved, tags, n, prof) if moved is not None else job)
    return result


def split(f, frame=None, context=None):
    """New Features where merged highways' lane counts change; identity if none.

    Only explicit per-source counts override the primary count. Missing parts
    keep the primary evidence; two-way roads and ambiguous references stay intact.
    """
    if f is None or not len(f) or PROFILE in f.attrs:
        return f
    plans = {i: got for i in range(len(f)) if (got := _sections(f, i)) is not None}
    # A cell may contain only a distant portion of a link whose mouth is in
    # another cell. Its owner's evidence is still needed for stable geometry.
    if not plans and context is None:
        return f
    import roads
    jobs = []
    for i in range(len(f)):
        if i not in plans:
            jobs.append((str(f.ids[i]), i, 0., 1., None, None, None, None))
            continue
        rows, base, direction = plans[i]
        ll = f.parts(i)[0][0]
        lane_m = roads.LANE_W.get(str(f.attrs["class"][i]), roads.LANE_W_DEFAULT)
        offsets = [_placement(tags, n, lane_m, direction) or 0. for _, _, _, tags, n, _ in rows]
        base_offsets = [off for off, row in zip(offsets, rows) if row[4] == base] or [0.]
        reference = base_offsets[0]
        common_grid = max(base_offsets) - min(base_offsets) < .05
        for j, (a, b, _way, tags, n, _sig) in enumerate(rows):
            off = offsets[j]
            shift = off - reference
            extra = (n - base) * lane_m
            if not common_grid or abs(abs(shift) - extra / 2) > .05:
                extra = 0.
            p, s = _sample(ll, a, b, dense=bool(extra))
            length = float(s[-1])
            reach, ends = [0., 0.], [1., 1.]
            if extra:
                for end, nb in ((0, j - 1), (1, j + 1)):
                    if not 0 <= nb < len(rows):
                        continue
                    other = rows[nb][4]
                    change = (n - other) * lane_m
                    if change > 0. and abs(off - offsets[nb] - np.sign(shift) * change / 2) < .05:
                        reach[end] = min(90., change * 20., length * .45)
                        ends[end] = 1. - change / extra
                side, turning = _outer_lanes(tags, n)
                if side * direction != np.sign(shift):
                    turning = 0
                placed = reference + shift * _ease(s, length, reach, ends)
            else:
                placed, turning = off, 0
            moved = bool(np.any(np.abs(np.atleast_1d(placed)) > 1e-9))
            if moved:
                p = _offset(p, placed)
            ends_off = (float(np.atleast_1d(placed)[0]), float(np.atleast_1d(placed)[-1])) if moved else (0., 0.)
            prof = {"offset": off, "shift": shift, "extra": extra, "turning": turning, "end_off": ends_off,
                    "reach": reach, "ends": ends, "length": length, "phase": float(_arc(ll)[-1] * a),
                    "stations": s.tolist(), "line": p.tolist()}
            fid = f"{f.ids[i]}~lanes{round(a * 1e9):09d}-{round(b * 1e9):09d}"
            jobs.append((fid, i, a, b, p, tags, n, prof))
    jobs = _ramp_mouths(f, jobs, context)
    if not plans and not any(r[4] is not None for r in jobs):
        return f
    jobs.sort(key=lambda r: r[0])
    old = np.array([r[1] for r in jobs], np.int64)
    attrs = {k: np.asarray(v)[old].copy() for k, v in f.attrs.items()}
    attrs[PROFILE] = np.full(len(jobs), None, object)
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


def phase(f, i):
    prof = profile(f, i)
    return prof["phase"] if prof else 0.
