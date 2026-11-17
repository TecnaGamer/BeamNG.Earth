"""lanesections.split's plan (earthfast 383, roadnet::lanesplit: the road revamp's R2a) against the Python it replaced:
the whole new layer (ids, kinds, geometry bit for bit, every attrs column with the sections' profiles value for value and
type for type, the rule tables) over random merged motorways (2-4 lane parts with lanes / placement / turn:lanes tags,
gaps and overlaps, equal neighbours merged, missing counts, reversed one-ways, 1- and 2-point lines, several parts) and
slip roads at their ends (one-way links of 1-2 lanes, at either end, on a structure or not, a context layer or none);
also _ease, _offset (lane_ease, lane_offset) and roads.lane_tag_parts (lane_tag_parts).

The Python is kept here only as the reference (the server has no Python twin: fast.require): lanesections.py's (with R1j's LANE_MAX)
_arc .. split before earthfast 383, verbatim, its joints remembered under their own name."""
import math
import os
import random
import re
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from test_roads import rules  # noqa: E402  (first: it puts the server on the path)
import dmath  # noqa: E402,F401
import dnp  # noqa: E402,F401
import fast  # noqa: E402
import geodesy  # noqa: E402,F401
import lanesections  # noqa: E402
import mapgeom  # noqa: E402
import railtags  # noqa: E402
import roadlanes  # noqa: E402
import roads  # noqa: E402
from lanesections import LANE_MAX, PROFILE  # noqa: E402,F401
from test_roadlanes import highway  # noqa: E402

_MOUTH = {}                         # (feature, end) -> on a structure: both sides' _structure_mouth


def _structure_mouth(f, i, end):
    return _MOUTH.get((id(f), i, end), False)


# ---- the Python version (before earthfast 383), verbatim ----

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
    cached = f.__dict__.get("_ref_lane_section_joints")
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
    f.__dict__["_ref_lane_section_joints"] = joints
    return joints


def _grid(cls, n, lane_m):
    """m the travel lanes of a one-way carriageway of class cls and n lanes stand left of travel off its pavement's
    centre (roads.driving_offset of its default width: its right shoulder 1.5 x, its left 0.5 x the mean shoulder)."""
    import roads
    if cls != "motorway":
        return 0.
    cap = roads.SHOULDER_WIDE_M if n >= roads.SHOULDER_WIDE_LANES else roads.SHOULDER_M
    return cap / 2.


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
            if _structure_mouth(f, i, end):
                continue
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


# ---- random layers ----

WAYS = ["10", "11", "12", "13"]


def _tags(rng, n):
    t = {}
    if rng.random() < 0.85:
        t["lanes"] = rng.choice([str(n), str(n), n, "x", "2;3"])
    if rng.random() < 0.5:
        side = rng.choice(["left_of", "middle_of", "right_of", "lft_of"])
        t["placement"] = f"{side}:{rng.choice([1, 2, n, n + 1, 0])}" if rng.random() < 0.9 else "middle_of:٢"
    if rng.random() < 0.5:
        k = rng.choice([n, n, n - 1])
        lanes = ["none"] * max(k, 1)
        exit_right = rng.random() < 0.5
        for j in range(rng.choice([0, 1, 1, 2])):
            if j < len(lanes):
                lanes[-1 - j if exit_right else j] = rng.choice(["slight_right", "right", "merge_to_left"]) \
                    if exit_right else rng.choice(["slight_left", "left", "merge_to_right"])
        if rng.random() < 0.2:
            lanes[0] = "through;slight_left"
        t["turn:lanes"] = "|".join(lanes)
    return t


def random_layer(rng):
    """A few merged motorways along x (lane parts with other counts) and slip roads joined at their ends."""
    lines, parts, cls, direction, link, lanes = [], [], [], [], [], []
    for m in range(rng.randint(1, 3)):
        y = m * 60.0
        k = rng.choice([2, 3, 4, 6])
        xs = sorted(rng.sample(range(10, 990), k - 2)) if k > 2 else []
        pts = [(0.0, y)] + [(float(x), y + rng.uniform(-3, 3)) for x in xs] + [(1000.0, y)]
        if rng.random() < 0.4:              # (vertices under 1 cm apart: _sample's dedupe chain, a zero-length step)
            x0 = rng.uniform(100, 900)
            near = [(x0 + d, y) for d in (0.0, 0.006, 0.012, 0.012, 0.03)]
            pts = sorted([p for p in pts if abs(p[0] - x0) > 1] + near)
        rev = rng.random() < 0.3
        lines.append(pts)
        cls.append(rng.choice(["motorway", "motorway", "trunk", "primary"]))
        direction.append(rng.choice([-1.0, 1.0, 1.0]) if rng.random() < 0.95 else None)
        link.append(False)
        lanes.append(rng.choice([None, 2, 3]))
        cuts = sorted(rng.sample([0.2, 0.35, 0.5, 0.7, 0.85], rng.randint(1, 3)))
        edges = [0.0] + cuts + [1.0]
        rows = []
        for j in range(len(edges) - 1):
            a, b = edges[j], edges[j + 1]
            if rng.random() < 0.05:
                b = b - 0.01                    # (a gap: rejected)
            n = rng.choice([2, 3, 3, 4, 5])
            rows.append((a, b, rng.choice(WAYS), _tags(rng, n) if rng.random() < 0.9 else None))
        if rng.random() < 0.5:
            # an auxiliary lane at an end the slip road takes: its placement and turn lanes agree (the joint specs)
            n0, extra_lanes = rng.choice([2, 3]), rng.choice([1, 1, 2])
            n1 = n0 + extra_lanes
            right = rng.random() < 0.6
            k = n1 / 2 - extra_lanes / 2 if right else n1 / 2 + extra_lanes / 2
            place = f"middle_of:{int(k + .5)}" if (k + .5) == int(k + .5) else f"right_of:{int(k)}"
            turn = ["none"] * n1
            for j in range(extra_lanes):
                turn[-1 - j if right else j] = "slight_right" if right else "slight_left"
            aux = {"lanes": str(n1), "placement": place, "turn:lanes": "|".join(turn)}
            mid = {"lanes": str(n0)}
            at_end = rng.random() < 0.5
            rows = [(0.0, 0.6, "20", mid), (0.6, 1.0, "21", aux)] if at_end else \
                [(0.0, 0.4, "20", aux), (0.4, 1.0, "21", mid)]
        if rng.random() < 0.3:
            rows.reverse()                      # (sorted by start)
        parts.append(rows if rng.random() < 0.9 else None)
        # slip roads at the motorway's ends (either way, some two-way, on a structure or not)
        for end_x in (0.0, 1000.0):
            if rng.random() < 0.6:
                side = rng.choice([-1, 1])
                ahead = (1.0 if end_x > 0 else -1.0) if rng.random() < 0.8 else rng.choice([-1.0, 1.0])
                ramp = [(end_x, y), (end_x + ahead * 120.0, y + side * rng.uniform(5, 30)),
                        (end_x + ahead * rng.choice([260.0, 700.0]), y + side * rng.uniform(20, 60))]
                if rng.random() < 0.5:
                    ramp.reverse()
                lines.append(ramp)
                cls.append(cls[-1] if rng.random() < 0.8 else "trunk")
                direction.append(rng.choice([1.0, -1.0, 1.0]) if rng.random() < 0.9 else None)
                link.append(True)
                lanes.append(rng.choice([1, 1, 2, None]))
                parts.append(None)
    f = highway(lines)
    n = len(lines)
    f.attrs["class"] = np.array(cls, object if rng.random() < 0.5 else str)
    f.rules["oneway"] = rules([(i, d) for i, d in enumerate(direction) if d is not None], "float")
    f.rules["subclass"] = rules([(i, "link") for i in range(n) if link[i]])
    f.attrs["osm_lanes"] = np.array([np.nan if v is None else float(v) for v in lanes])
    pa = np.empty(n, object)
    for i, p in enumerate(parts):
        pa[i] = p
    f.attrs[railtags.LANE_PARTS] = pa
    f.rules["connectors"] = rules([(i, f"c{i}a", 0, 0) for i in range(n)] + [(i, f"c{i}b", 1, 1) for i in range(n)])
    for i in range(n):
        for end in (0, -1):
            if rng.random() < 0.15:
                _MOUTH[(id(f), i, end)] = True
    return f


def same(a, b, what):
    if isinstance(a, dict) or isinstance(b, dict):
        assert type(a) is type(b) and list(a) == list(b), (what, a, b)
        for k in a:
            same(a[k], b[k], f"{what}.{k}")
        return
    if isinstance(a, (list, tuple)) or isinstance(b, (list, tuple)):
        assert type(a) is type(b) and len(a) == len(b), (what, a, b)
        for k, (x, y) in enumerate(zip(a, b)):
            same(x, y, f"{what}[{k}]")
        return
    if isinstance(a, np.ndarray) or isinstance(b, np.ndarray):
        assert isinstance(a, np.ndarray) and isinstance(b, np.ndarray), (what, a, b)
        assert a.dtype == b.dtype and a.shape == b.shape, (what, a.dtype, b.dtype, a.shape, b.shape)
        if a.dtype.kind == "O":
            for k, (x, y) in enumerate(zip(a.ravel(), b.ravel())):
                same(x, y, f"{what}[{k}]")
        else:
            assert a.tobytes() == b.tobytes(), (what, a, b)
        return
    assert type(a) is type(b), (what, type(a), type(b), a, b)
    if isinstance(a, float):
        assert np.float64(a).tobytes() == np.float64(b).tobytes(), (what, a, b)
    else:
        assert a == b or (a != a and b != b), (what, a, b)


def same_layer(g, w, what):
    if w is None or g is None:
        assert g is w, what
        return
    for k in ("layer", "ids", "kind", "part_off", "ring_off", "coord_off", "lonlat", "own", "clipped"):
        same(getattr(g, k), getattr(w, k), f"{what}.{k}")
    assert list(g.attrs) == list(w.attrs), (what, list(g.attrs), list(w.attrs))
    for k in w.attrs:
        same(g.attrs[k], w.attrs[k], f"{what}.attrs[{k}]")
    assert list(g.rules) == list(w.rules), what
    for k in w.rules:
        for col in ("feature", "value", "start", "end"):
            same(getattr(g.rules[k], col), getattr(w.rules[k], col), f"{what}.rules[{k}].{col}")


class LaneSplitParityTest(unittest.TestCase):
    def setUp(self):
        self._mouth = lanesections._structure_mouth
        lanesections._structure_mouth = _structure_mouth

    def tearDown(self):
        lanesections._structure_mouth = self._mouth

    def test_random_layers(self):
        rng = random.Random(int(os.environ.get("LANESPLIT_SEED", "383")))
        sections = 0
        for k in range(int(os.environ.get("LANESPLIT_CASES", "400"))):
            f = random_layer(rng)
            other = random_layer(rng) if rng.random() < 0.5 else None
            pick = rng.random()

            def context(lat, lon):
                return None if pick < 0.2 else f if pick < 0.6 or other is None else other
            ctx = context if rng.random() < 0.7 else None
            want = split(f, None, ctx)
            got = lanesections.split(f, None, ctx)
            same_layer(got, want, f"case {k}")
            if got is not f and PROFILE in got.attrs:
                sections += sum(p is not None for p in got.attrs[PROFILE])
        self.assertGreater(sections, int(os.environ.get("LANESPLIT_CASES", "400")) // 2)

    def outcome(self, fn, *args):
        """("ok", the result) or ("raised", the exception's class); a Rust panic (PanicException, a BaseException
        that `except Exception` does not catch) fails the test."""
        try:
            return "ok", fn(*args)
        except Exception as e:      # noqa: BLE001 (the class is the point)
            return "raised", type(e)
        except BaseException as e:  # noqa: BLE001
            self.fail(f"{type(e).__name__}: {e}")

    def test_non_finite_map_data_raises_as_python_did(self):
        """Lane fractions and coordinates that are NaN / infinite / huge (railtags copies the source rules' starts and
        ends into the lane parts unchecked): the same exception class as the Python, or the same layer; never a
        panic (Codex review of d0888ef3: a NaN fraction reached np.interp's unwrap)."""
        nan, inf = float("nan"), float("inf")
        fractions = [
            [(0.0, nan, "1", {"lanes": "2"}), (nan, 1.0, "2", {"lanes": "3"})],
            [(nan, 0.5, "1", {"lanes": "2"}), (0.5, 1.0, "2", {"lanes": "3"})],
            [(0.0, 0.5, "1", {"lanes": "2"}), (0.5, nan, "2", {"lanes": "3"})],
            [(0.0, inf, "1", {"lanes": "2"}), (inf, 1.0, "2", {"lanes": "3"})],
            [(-inf, 0.5, "1", {"lanes": "2"}), (0.5, 1.0, "2", {"lanes": "3"})],
            [(0.0, 1e300, "1", {"lanes": "2"}), (1e300, 1.0, "2", {"lanes": "3"})],
            [(0.0, nan, "1", {"lanes": "3"}),
             (nan, 1.0, "2", {"lanes": "4", "placement": "middle_of:2", "turn:lanes": "none|none|none|slight_right"})],
            [(0.0, 0.6, "1", {"lanes": "3"}),
             (0.6, 1.0, "2", {"lanes": "4", "placement": "middle_of:2", "turn:lanes": "none|none|none|slight_right"})],
        ]
        lines = [[(0, 0), (1000, 0)], [(0, 0), (500, 0), (500, 0), (1000, 0)]]
        cases = 0
        for parts in fractions:
            for pts in lines:
                for bad_point in (None, 0, 1, -1):
                    f = highway([pts, [(1000, 0), (1120, -8), (1300, -40)]])
                    f.rules["subclass"] = rules([(1, "link")])
                    f.attrs["osm_lanes"] = np.array([np.nan, 1.0])
                    if bad_point is not None:
                        k = bad_point if bad_point >= 0 else len(pts) - 1
                        f.lonlat = f.lonlat.copy()
                        f.lonlat[k] = (np.nan, f.lonlat[k][1]) if k % 2 else (f.lonlat[k][0], np.inf)
                    pa = np.empty(2, object)
                    pa[0] = parts
                    f.attrs[railtags.LANE_PARTS] = pa
                    for ctx in (None, lambda la, lo: f):
                        want = self.outcome(split, f, None, ctx)
                        got = self.outcome(lanesections.split, f, None, ctx)
                        self.assertEqual(got[0], want[0], (parts, bad_point))
                        if want[0] == "raised":
                            self.assertIs(got[1], want[1], (parts, bad_point))
                        else:
                            same_layer(got[1], want[1], f"{parts} {bad_point}")
                        cases += 1
        self.assertEqual(cases, len(fractions) * len(lines) * 4 * 2)

    def test_malformed_offsets_raise_not_panic(self):
        """A layer whose offsets point past its arrays: an IndexError from every roadnet kernel that reads offsets,
        never a panic."""
        f = random_layer(random.Random(7))
        f.coord_off = f.coord_off.copy()
        f.coord_off[-1] += 5
        for fn in (lanesections.split, lambda f: roads._lengths(f), lambda f: roads._lane_flow_rows(f)):
            f.__dict__.pop("_lane_flow_rows", None)
            got = self.outcome(fn, f)
            self.assertEqual(got, ("raised", IndexError))

    def test_ease_offset_and_tag_parts(self):
        rng = random.Random(3831)
        for k in range(300):
            s = np.sort(np.array([rng.uniform(0, 300) for _ in range(rng.randint(1, 30))]))
            length = float(s[-1]) if rng.random() < 0.8 else rng.uniform(1, 400)
            reach = [rng.choice([0.0, 30.0, 90.0, -1.0]), rng.choice([0.0, 45.0, 120.0])]
            ends = [rng.choice([1.0, 0.25, 0.0]), rng.choice([1.0, 0.5])]
            self.assertEqual(lanesections._ease(s, length, reach, ends).tobytes(),
                             _ease(s, length, reach, ends).tobytes(), k)
            m = rng.randint(1, 9)
            pts = np.c_[np.cumsum([rng.uniform(-1e-3, 1e-3) for _ in range(m)]) - 76.6,
                        np.cumsum([rng.uniform(-1e-3, 1e-3) for _ in range(m)]) + 39.28]
            d = rng.choice([3.5, -2.0, 0.0]) if rng.random() < 0.5 else \
                np.array([rng.uniform(-5, 5) for _ in range(len(pts))])
            self.assertEqual(lanesections._offset(pts, d).tobytes(), _offset(pts, d).tobytes(), k)
        f = random_layer(random.Random(5))
        for i in range(len(f)):
            pa = f.attrs[railtags.LANE_PARTS][i]
            want = [(a, b, w, t or None) for a, b, w, t in pa] if pa else None
            got = fast._ef.lane_tag_parts(pa)
            self.assertEqual(got, want)


if __name__ == "__main__":
    unittest.main()
