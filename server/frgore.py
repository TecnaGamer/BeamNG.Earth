"""French J14a beacons from connected motorway diverges, independent of labels.

The fork tile owns the beacon. Connected pieces of the slip road and main
carriageway can extend the search to the first usable gap downstream.
"""

import math
import numpy as np
import dmath
import dnp
import geotiles

MODELS = (("fr_j14a", 1.0, 1.70), ("fr_j14a_small", 0.5, 1.20))  # group, radius, height (m)
SEARCH_M = 150.0
EDGE_PAD_M = 1.35


def _mainline(spec):
    return (
        spec.oneway
        and spec.dir
        and not spec.link
        and (
            spec.cls == "motorway"
            or (spec.cls == "trunk" and not spec.urban and (spec.kmh or 0) >= 90)
        )
    )


def _interior_arms(mainlines, junction):
    """The exit connector may be a vertex inside an unsplit through road."""
    for key, spec, line, _ in mainlines:
        if len(line) < 3:
            continue
        offsets = line[1:-1] - junction
        close = (dnp.norm(offsets[:, :2], axis=1) <= 1.0) & (abs(offsets[:, 2]) <= 1.2)
        for index in np.flatnonzero(close) + 1:
            for neighbor, start in ((index - 1, False), (index + 1, True)):
                direction = line[neighbor, :2] - line[index, :2]
                length = float(dnp.norm(direction))
                if length > 1e-3:
                    yield (key, spec, direction / length, line[index], start, length)


def _near_ends(ends, point):
    x, y = map(round, point[:2])
    return [arm for xx in range(x - 2, x + 3) for yy in range(y - 2, y + 3)
            for arm in ends.get((xx, yy), ())
            if dnp.norm(arm[3][:2] - point[:2]) <= 1.45 and abs(arm[3][2] - point[2]) <= 1.2]


def _follow(arm, lines, ends, eligible):
    import furniture as f

    parts = []
    ids = []
    length = 0.
    for _ in range(12):
        key, _, _, start, forward, _ = arm
        line = lines[key] if forward else lines[key][::-1]
        # A motorway connector can be an interior vertex of its through road.
        index = int(np.argmin(dnp.norm(line - start, axis=1)))
        line = line[index:]
        if len(line) < 2:
            break
        ids.append(key)
        if parts and dnp.norm(parts[-1][-1] - line[0]) < .01:
            parts.append(line[1:])
        else:
            parts.append(line)
        length += f._arclen(line)[-1]
        if length >= SEARCH_M + 25.:
            break
        delta = line[-1, :2] - line[-2, :2]
        delta /= max(dnp.norm(delta), .001)
        next_arms = [a for a in _near_ends(ends, line[-1]) if a[0] not in ids
                     and eligible(a[1]) and f._outbound(a) and float(dnp.matmul(a[2], delta)) > .72]
        if len(next_arms) != 1:
            break
        arm = next_arms[0]
    return ids, np.concatenate(parts)


def place(frame, roads, box, country_at, ground=None, decks=(), guardrails=(), obstacles=None):
    if frame is None:
        return []
    import furniture as f
    import exitsigns
    import euguardrails
    import signroads
    from roads import NEAR_M

    if not any(sp.link and sp.cls in {"motorway", "trunk"} for _, sp, _, _ in roads):
        return []
    # Give each part its own key: a feature can have several disconnected lines.
    ordered = sorted(
        (r for r in roads if len(r[2]) >= 2 and r[1].kind == "paved"),
        key=lambda r: (str(r[0]), tuple(np.round(r[2][0][:2], 3))),
    )
    indexed = [(i, sp, np.asarray(line, dtype=float), lon) for i, (_, sp, line, lon) in enumerate(ordered, 1)]
    lines = {i: line for i, _, line, _ in indexed}
    mainlines = [row for row in indexed if _mainline(row[1])]
    ends = f._arms(indexed)
    barriers = euguardrails._rail_segments(guardrails)
    pavements = polygons = None
    result = []
    used = []
    x0, y0, x1, y1 = box
    for _, arms in sorted(ends.items()):
        for ramp in arms:
            if not ramp[1].link or ramp[1].cls not in {"motorway", "trunk"} or not f._outbound(ramp):
                continue
            c = ramp[3]
            if not geotiles.box_owns(box, *c[:2]) or country_at(*c) != "FR":
                continue
            mains = [a for a in _near_ends(ends, c) if _mainline(a[1])]
            if not any(f._outbound(a) for a in mains) or all(f._outbound(a) for a in mains):
                mains.extend(_interior_arms(mainlines, c))
            incoming = [a for a in mains if not f._outbound(a)]
            outgoing = [a for a in mains if f._outbound(a)]
            if not incoming or not outgoing:
                continue
            candidates = [
                a
                for a in outgoing
                if dnp.matmul(a[2], ramp[2]) > 0.4 and any(dnp.matmul(a[2], -b[2]) > 0.7 for b in incoming)
            ]
            if not candidates:
                continue
            if pavements is None:
                pavements = [
                    (f"osm:w{i}:0", signroads.spec(sp, link=sp.link), p, sp.width)
                    for i, sp, p, _ in indexed
                ]
                polygons = exitsigns._outlines(pavements)
            main = max(candidates, key=lambda a: max(dnp.matmul(a[2], -b[2]) for b in incoming))
            ramp_ids, line = _follow(ramp, lines, ends, lambda sp: sp.link and sp.cls == ramp[1].cls)
            main_ids, through = _follow(main, lines, ends, lambda sp: _mainline(sp) and sp.cls == main[1].cls)
            travel = main[2]
            ahead, _ = f._at(line, np.array([25., 45., 70., 95., 125.]))
            stations = f._project(through, ahead[:, :2])
            reference, vectors = f._at(through, stations)
            offsets = (ahead[:, :2] - reference[:, :2]) * np.c_[vectors[:, 1], -vectors[:, 0]]
            displacements = offsets.sum(1)
            departures = displacements[abs(displacements) > 1.2]
            if not len(departures):
                continue
            extra = {"ramp_id": ramp[0], "ramp_ids": ramp_ids, "main_ids": main_ids,
                     "side": 1 if departures[0] > 0 else -1}
            approach = np.array([c[:2] - travel * 30.0, c[:2]])
            for group, radius, height in MODELS:
                def clear(q):
                    # Use only the neighbourhood supplied by build_tile.
                    if not (x0 - NEAR_M + 2 <= q[0] <= x1 + NEAR_M - 2
                            and y0 - NEAR_M + 2 <= q[1] <= y1 + NEAR_M - 2):
                        return False
                    if not exitsigns._clear(q, radius + EDGE_PAD_M, polygons):
                        return False
                    if any(f._seg_dist_min(q, np.asarray(p)[:, :2]) < half + radius + .55 for p, half in decks):
                        return False
                    if euguardrails._near_barrier(q, barriers, radius + .55):
                        return False
                    if obstacles is not None and not obstacles.clear(q, radius + .30):
                        return False
                    return (country_at(float(q[0]), float(q[1]), float(c[2])) == "FR"
                            and all(dnp.norm(q - p) >= 8. for p in used))

                hit = exitsigns._compute_gore(
                    extra, approach, line[:, :2], pavements, {}, 2 * radius, max_distance=SEARCH_M,
                    clearance=EDGE_PAD_M, candidate_clear=clear,
                )
                if hit is None:
                    continue
                _, at, direction, q = hit
                z = (
                    at[2]
                    if ground is None
                    else np.asarray(ground(np.array([q[0]]), np.array([q[1]]))).ravel()[0]
                )
                yaw = math.degrees(dmath.atan2(-direction[0], direction[1]))
                result.append([group, float(q[0]), float(q[1]), float(z), yaw, 1.0, height, 0.0])
                used.append(q)
                break
    return result
