"""Preserve lane/placement changes hidden inside merged highway features.

Normalize once, before any geometry consumer sees the roads. Sections retain
linear-referenced rules and connector identities; painting, grading, structures
and traffic consequently consume the same physical carriageway centre lines.
No geographic or feature-ID overrides.
"""
import math
import re

import numpy as np

import geodesy
import mapgeom
import railtags
import roadlanes

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


def _sections(f, i):
    cls = f.attrs.get("class", [""] * len(f))[i]
    direction = roadlanes.rule(f, "oneway", i)
    parts = f.attrs.get(railtags.LANE_PARTS)
    if f.kind[i] != mapgeom.LINE or cls not in roadlanes.HIGHWAYS or not direction or roadlanes.is_link(f, i):
        return None
    rings = f.parts(i)
    if len(rings) != 1 or len(rings[0]) != 1 or len(rings[0][0]) < 2:
        return None
    primary = roadlanes.count(f.attrs.get("osm_lanes", [None] * len(f))[i])
    primary = primary or roadlanes.count(f.attrs.get(railtags.lane_attr("lanes"), [None] * len(f))[i])
    rows = sorted(parts[i], key=lambda r: r[0]) if parts is not None and parts[i] else [
        (0., 1., None, {key: f.attrs[railtags.lane_attr(key)][i] for key in railtags.LANE_COLUMNS
                       if railtags.lane_attr(key) in f.attrs and
                       f.attrs[railtags.lane_attr(key)][i] not in (None, "")})]
    explicit = [roadlanes.count((r[3] or {}).get("lanes")) for r in rows]
    if primary is None and all(n is not None for n in explicit):
        primary = explicit[max(range(len(rows)), key=lambda k: rows[k][1] - rows[k][0])]
    counts = [n or primary for n in explicit]
    if not primary or any(n is None for n in counts) or float(_arc(rings[0][0])[-1]) < 1.:
        return None
    if len(set(counts)) < 2 and not any(
            abs(_placement(r[3] or {}, n, 1., 1) or 0.) > .01 for r, n in zip(rows, counts)):
        return None
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
                tangent = tangent * np.array([math.cos(math.radians(e[1])), 1.])
                tangent = tangent / max(np.linalg.norm(tangent), 1e-12)
                target = off + side * direction * (n - turning) * lane_m / 2.
                tail = (end == 0) == (direction > 0)
                key = tuple(np.round(e, 6))
                joints.setdefault(key, []).append((tangent, target, tail, str(f.attrs["class"][i]), turning))
    f.__dict__["_lane_section_joints"] = joints
    return joints


def _ramp_mouths(f, jobs, context=None):
    """Resolve the physical slip-lane centre from explicit lane evidence.

    A merged feature can extend outside its data cell. Resolve each mouth from
    the data cell containing that mouth, so copies have identical geometry.
    """
    joints = dict(_joint_specs(f))
    if context is not None:
        for _fid, i, _a, _b, p, _tags, _n, _prof in jobs:
            if (p is not None or not roadlanes.is_link(f, i) or
                    f.attrs.get("class", [""] * len(f))[i] not in roadlanes.HIGHWAYS):
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
            ramp_lanes = roadlanes.count(f.attrs.get("osm_lanes", [None] * len(f))[i]) or \
                roadlanes.count(f.attrs.get(railtags.lane_attr("lanes"), [None] * len(f))[i]) or 1
            if (cls != str(f.attrs["class"][i]) or turning != ramp_lanes or
                    main_tail == ((end == 0) == (direction > 0))):
                continue
            line, stations = _sample(raw, 0., 1.)
            sc = np.array([111320. * math.cos(math.radians(raw[end, 1])), 110540.])
            xy = (line - raw[end]) * sc
            dr = (raw[1] - raw[0]) * sc if end == 0 else (raw[-1] - raw[-2]) * sc
            dr = dr / max(np.linalg.norm(dr), 1e-12)
            dot = float(np.dot(dr, tangent))
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
            if off:
                p = _offset(p, placed)
            prof = {"offset": off, "shift": shift, "extra": extra, "turning": turning,
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
    coords, po, ro, co = [], [0], [0], [0]
    rr = {k: [] for k in f.rules}
    rr.setdefault("connectors", [])
    for z, (_fid, i, a, b, p, tags, n, prof) in enumerate(jobs):
        split_ = p is not None
        for part in ([[p]] if split_ else f.parts(i)):
            for ring in part:
                coords.extend(ring)
                co.append(len(coords))
            ro.append(len(co) - 1)
        po.append(len(ro) - 1)
        for name, table in f.rules.items():
            for r in table.rows(i):
                u, v = float(table.start[r]), float(table.end[r])
                point = abs(u - v) < 1e-8
                if (point and a - 1e-7 <= u <= b + 1e-7) or (not point and v > a and u < b):
                    rr[name].append((z, table.value[r], np.clip((u - a) / (b - a), 0., 1.),
                                     np.clip((v - a) / (b - a), 0., 1.)))
        if prof is None:
            continue
        for at, value in ((a, 0.), (b, 1.)):
            if 0. < at < 1.:
                rr["connectors"].append((z, f"lane:{f.ids[i]}:{round(at * 1e9)}", value, value))
        attrs[PROFILE][z] = prof
        attrs["osm_lanes"][z] = n
        # Preserve the section's tags, rather than repeating the dominant way's turn arrows.
        for key in railtags.LANE_COLUMNS:
            attrs[railtags.lane_attr(key)][z] = tags.get(key)
        attrs[railtags.lane_attr("lanes")][z] = n
        attrs[railtags.LANE_PARTS][z] = None
    tables = {}
    for name, rows in rr.items():
        if not rows:
            tables[name] = f.rules[name] if name in f.rules and not len(f.rules[name]) else mapgeom.RuleTable(
                np.zeros(0, np.int32), np.zeros(0, str), np.zeros(0), np.zeros(0))
            continue
        tables[name] = mapgeom.RuleTable(np.array([r[0] for r in rows], np.int32),
                                        np.array([r[1] for r in rows], dtype=f.rules[name].value.dtype
                                                 if name in f.rules and name != "connectors" else str),
                                        np.array([r[2] for r in rows], np.float64),
                                        np.array([r[3] for r in rows], np.float64))
    out = mapgeom.Features(f.layer, np.array([r[0] for r in jobs]), f.kind[old], np.array(po, np.int64),
                           np.array(ro, np.int64), np.array(co, np.int64), np.array(coords, np.float64),
                           own=None if f.own is None else f.own[old],
                           clipped=None if f.clipped is None else f.clipped[old], attrs=attrs, rules=tables)
    # Join versions are retained; geometry/specification memos intentionally are not.
    for key in ("_lanes_built", "_rails_built"):
        if key in f.__dict__:
            out.__dict__[key] = f.__dict__[key]
    return out.localize(frame) if frame is not None else out


def profile(f, i):
    col = f.attrs.get(PROFILE)
    return col[i] if col is not None else None


def growth(prof, lon, lat):
    """Evaluate the same source-station profile on clipped/resampled geometry."""
    if not any(prof["reach"]):
        return np.ones(np.shape(lon), np.float64)
    ll = np.array(prof["line"], np.float64)
    fr = geodesy.EnuFrame(float(ll[0, 1]), float(ll[0, 0]), 0.)
    e, n, _ = fr.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.)
    pe, pn, _ = fr.geodetic_to_enu(np.asarray(lat), np.asarray(lon), 0.)
    a, d = np.c_[e, n][:-1], np.diff(np.c_[e, n], axis=0)
    p = np.c_[np.ravel(pe), np.ravel(pn)]
    # Profile sections are short; bound memory for long resampled sections.
    s = np.asarray(prof["stations"])
    at = []
    for batch in np.array_split(p, max(1, math.ceil(len(p) / 256))):
        t = np.clip(((batch[:, None] - a) * d).sum(2) / np.maximum((d * d).sum(1), 1e-12), 0., 1.)
        dist = ((batch[:, None] - a - t[:, :, None] * d) ** 2).sum(2)
        k = dist.argmin(1)
        at.extend(s[k] + t[np.arange(len(batch)), k] * np.diff(s)[k])
    return _ease(np.array(at), prof["length"], prof["reach"], prof["ends"]).reshape(np.shape(lon))


def phase(f, i):
    prof = profile(f, i)
    return prof["phase"] if prof else 0.
