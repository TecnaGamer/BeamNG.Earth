"""French local direction signs tied to the roads and shoulders drawn in a tile."""

from dataclasses import dataclass
import math
import unicodedata

import numpy as np

import frroadmesh
import furniture
import geotiles
import signplacement
import townsigns

SIGNATURE = 'fr-local-directions-5'      # 5: PR #505's later route-estimate rules (checked routes, aligned distances)
# 4: the cells from the regional data (regionaldata fr_local / fr_priority)
CHECKED_ROUTING = 6     # the priority catalogue's routing version whose route faces compare competing turns
SOURCE_ORDER = {'relation': 0, 'road': 1, 'route': 2}
LOCAL = frozenset(
    ('primary', 'secondary', 'tertiary', 'unclassified', 'residential', 'living_street')
)
MATCH_M = 6.0
LOCAL_STATIONS = (8.0, 12.0, 5.0, 16.0, 21.0, 27.0, 34.0)
ADVANCE_STATIONS = (100.0, 90.0, 110.0, 80.0, 120.0, 70.0, 65.0)
ADVANCE_REACH_M = 128.0
CONTINUATION_M = 170.0
THROUGH = LOCAL - {'residential', 'living_street'}
PLACE_REACH = {'city': 3600.0, 'town': 2200.0, 'village': 1300.0, 'hamlet': 420.0}


def nearby(frame, box, margin=0.0):
    x0, y0, x1, y1 = box
    x0, y0, x1, y1 = x0 - margin, y0 - margin, x1 + margin, y1 + margin
    lat, lon, _ = frame.enu_to_geodetic(
        np.array([x0, x0, x1, x1]), np.array([y0, y1, y0, y1]), np.zeros(4)
    )
    lo, hi = np.floor(np.array([[min(lon), min(lat)], [max(lon), max(lat)]]) * 1.0e7).astype(int)
    if hi[0] < -55_000_000 or lo[0] > 100_000_000 or hi[1] < 410_000_000 or lo[1] > 515_000_000:
        return []
    import regionaldata
    result = []
    priority = regionaldata.dataset('fr_priority')
    # PR #505's rebuilt priority catalogue (routing 6) checks each route estimate against the junction's competing
    # turns (checked_route); with it, the older short-range estimates of both catalogues are left out. An older
    # catalogue keeps every face as before.
    checked = ((priority or {}).get('meta') or {}).get('routing', 0) >= CHECKED_ROUTING
    for ds in ('fr_priority', 'fr_local'):
        info = priority if ds == 'fr_priority' else regionaldata.dataset(ds)
        if info is None:
            continue
        cell_size = info['cell']
        for x in range(lo[0] // cell_size, hi[0] // cell_size + 1):
            for y in range(lo[1] // cell_size, hi[1] // cell_size + 1):
                for row in (regionaldata.cell(ds, x, y) or ()):
                    px, py = row['at']
                    if lo[0] <= px <= hi[0] + 1 and lo[1] <= py <= hi[1] + 1:
                        if not checked:
                            result.append(row)
                            continue
                        panels = [p for p in row['panels'] if p['source'] != 'route' or p.get('checked_route')]
                        if panels:
                            result.append(dict(row, panels=panels))
    return result


def _xy(frame, coordinate):
    lon, lat = coordinate
    return np.asarray(frame.geodetic_to_enu(lat * 1.0e-7, lon * 1.0e-7, 0.0)[:2])


def _right(travel):
    return np.array([travel[1], -travel[0]])


def _length(vector):
    return math.hypot(*vector)


@dataclass(frozen=True)
class Arm:
    part: int
    spec: object
    line: np.ndarray  # starts at the matched vertex and points away from the junction
    way: int | None
    sense: int
    ring: bool
    end: bool

    @property
    def vertex(self):
        return self.line[0]

    @property
    def key(self):
        return self.part, self.sense, tuple(self.vertex[:2])

    def allows(self, outbound):
        return not self.spec.oneway or (
            self.spec.dir != 0 and (self.spec.dir == self.sense) == outbound
        )


class RoadGraph:
    def __init__(self, roads, way_ids, roundabouts):
        eligible = [
            (fid, sp, line)
            for fid, sp, line, _ in roads
            if sp.kind == 'paved' and sp.cls in LOCAL and len(line) >= 2
        ]
        self.ids = [fid for fid, _, _ in eligible]
        self.parts = [
            (sp, np.asarray(line, dtype=float), way_ids.get(fid), fid in roundabouts)
            for fid, sp, line in eligible
        ]
        self.bounds = np.asarray(
            [(p[:, :2].min(0), p[:, :2].max(0)) for _, p, _, _ in self.parts]
        ).reshape(-1, 2, 2)

    def arms(self, point, reach):
        near = np.flatnonzero(
            np.all(point + reach >= self.bounds[:, 0], axis=1)
            & np.all(point - reach <= self.bounds[:, 1], axis=1)
        )
        for part in near:
            spec, line, way, ring = self.parts[part]
            distances = np.hypot(*(line[:, :2] - point).T)
            for i in np.flatnonzero(distances <= reach):
                for sense, outward in ((1, line[i:]), (-1, line[i::-1])):
                    if len(outward) > 1 and _length(outward[1, :2] - outward[0, :2]) >= 0.05:
                        yield Arm(part, spec, outward, way, sense, ring, i in (0, len(line) - 1))


def _match(arms, at, hint, way, outbound, ring=False):
    target = hint - at
    length = _length(target)
    if length < 1.0:
        return None
    aim = target / length
    ranked = []
    for arm in arms:
        if arm.ring != ring or not arm.allows(outbound):
            continue
        sample, _ = furniture._at(arm.line, np.array([min(length, 18.0)]))
        ray = sample[0, :2] - arm.vertex[:2]
        alignment = float(ray @ aim) / max(_length(ray), 0.01)
        if alignment < 0.81:
            continue
        same = way is not None and arm.way == way
        # A matching OSM id helps, but the live geometry is still required.
        score = _length(arm.vertex[:2] - at) + 12.0 * (1.0 - alignment) - (4.0 if same else 0.0)
        ranked.append((score, arm))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0])
    score, chosen = ranked[0]
    if any(
        other.part != chosen.part and other_score - score < 0.7 for other_score, other in ranked[1:]
    ):
        return None
    return chosen


def _connected(a, b):
    separation = _length(a.vertex[:2] - b.vertex[:2])
    return (
        separation <= (1.8 if a.end or b.end else 0.20) and abs(a.vertex[2] - b.vertex[2]) <= 1.15
    )


def _continuation(graph, arm, outbound, stop_at_junction=False):
    at = arm.line[-1]
    tail = at[:2] - arm.line[-2, :2]
    tail /= max(_length(tail), 0.01)
    matches = []
    for option in graph.arms(at[:2], 1.8):
        if option.ring != arm.ring or not option.end or abs(option.vertex[2] - at[2]) > 1.15:
            continue
        ray = option.line[1, :2] - option.vertex[:2]
        angle = float(ray @ tail) / _length(ray)
        if stop_at_junction and option.part != arm.part and -0.85 < angle <= 0.82:
            return None
        if option.allows(outbound) and angle > (-0.15 if arm.ring else 0.82):
            matches.append(option)
    if stop_at_junction:
        return matches[0] if len(matches) == 1 else None
    if len(matches) == 1:
        return matches[0]
    same = [option for option in matches if arm.way is not None and option.way == arm.way]
    return same[0] if len(same) == 1 else None


def _approach_line(graph, arm, reach=62.0, stop_at_junction=False):
    """Follow a split approach upstream across a clear continuation."""
    lines = [arm.line]
    used = {arm.part}
    for _ in range(7):
        if sum(furniture._arclen(line)[-1] for line in lines) >= reach:
            break
        arm = _continuation(graph, arm, False, stop_at_junction)
        if arm is None or arm.part in used:
            break
        used.add(arm.part)
        lines.append(
            arm.line[1:] if _length(lines[-1][-1, :2] - arm.vertex[:2]) < 0.05 else arm.line
        )
    return np.concatenate(lines)


def _ring_path(graph, entry):
    path = []
    used = set()
    for _ in range(32):
        if entry.key in used:
            return path, True
        path.append(entry)
        used.add(entry.key)
        entry = _continuation(graph, entry, True)
        if entry is None:
            break
    return path, False


def _ring_reaches(path, leaving):
    for entry in path:
        distances = np.hypot(*(entry.line[:, :2] - leaving.vertex[:2]).T)
        match = int(np.argmin(distances))
        if (
            distances[match] <= (1.8 if leaving.end or match == len(entry.line) - 1 else 0.20)
            and abs(entry.line[match, 2] - leaving.vertex[2]) <= 1.15
        ):
            return True
    return False


def _outlets(frame, graph, plan, at, arms, approach, ring_path):
    """Yield the catalogue labels whose outgoing branches exist in the live graph."""
    priority_ring = plan.get('junction') in ('roundabout', 'mini') and bool(ring_path)
    for panel in plan['panels']:
        hint = _xy(frame, panel['out'])
        if 'exit' in panel:
            fork = _xy(frame, panel['exit'])
            if not ring_path:
                continue
            if not priority_ring and _match(arms, at, _xy(frame, panel['ring']), None, True, ring=True) is None:
                continue
            options = [
                arm for arm in graph.arms(fork, 2 * MATCH_M if priority_ring else MATCH_M)
                if not arm.ring and _ring_reaches(ring_path, arm)
            ]
            leave = _match(options, fork, hint, panel.get('way'), True)
            if leave is not None:
                yield leave, panel
        elif priority_ring:
            options = [arm for arm in arms if not arm.ring and _ring_reaches(ring_path, arm)]
            leave = _match(options, at, hint, panel.get('way'), True)
            if leave is not None:
                lat, lon, _ = frame.enu_to_geodetic(*leave.vertex)
                yield leave, dict(panel, exit=[round(lon * 1e7), round(lat * 1e7)])
        else:
            leave = _match(arms, at, hint, panel.get('way'), True)
            if leave is not None and _connected(approach, leave):
                yield leave, panel


def _name(value):
    return ''.join(
        c for c in unicodedata.normalize('NFKD', value.casefold()) if c.isalnum()
    )


@dataclass
class Site:
    plan: dict
    approach: Arm
    outlets: list
    ring: bool


def _near_box(box, p, margin):
    x0, y0, x1, y1 = box
    return x0 - margin <= p[0] < x1 + margin and y0 - margin <= p[1] < y1 + margin


def _ring_legs(graph, path):
    legs = {}
    for segment in path:
        for i, point in enumerate(segment.line):
            end = segment.end if i == 0 else i == len(segment.line) - 1
            for arm in graph.arms(point[:2], 1.8):
                if arm.ring or abs(arm.vertex[2] - point[2]) > 1.15:
                    continue
                if _length(arm.vertex[:2] - point[:2]) <= (1.8 if end or arm.end else 0.20):
                    legs[arm.key] = arm
    return legs.values()


def _straight_labels(sites):
    labels = {}
    for site in sites:
        arm = site.approach
        if site.ring or arm.spec.cls not in THROUGH:
            continue
        pts, _ = furniture._at(arm.line, np.array([12.0]))
        incoming = arm.vertex[:2] - pts[0, :2]
        for leave, panel in site.outlets:
            if panel['source'] == 'route' or panel.get('local') or panel.get('centre') or leave.spec.cls not in THROUGH:
                continue
            pts, _ = furniture._at(leave.line, np.array([12.0]))
            ray = pts[0, :2] - leave.vertex[:2]
            if float(incoming @ ray) >= .88 * _length(incoming) * _length(ray):
                labels.setdefault((arm.part, -arm.sense), []).append((arm.vertex, panel))
    return labels


def _downstream_label(graph, branch, labels):
    """Borrow a straight-ahead destination only along the same driven road."""
    used = set()
    reach = CONTINUATION_M
    for _ in range(6):
        if branch.key in used:
            break
        used.add(branch.key)
        stations = furniture._arclen(branch.line)
        choices = []
        for point, panel in labels.get((branch.part, branch.sense), ()):
            distance = np.hypot(*(branch.line[:, :2] - point[:2]).T)
            at = int(np.argmin(distance))
            if distance[at] <= .20 and 1.8 < stations[at] <= reach:
                choices.append((stations[at], SOURCE_ORDER[panel['source']], panel))
        if choices:
            return min(choices, key=lambda item: item[:2])[2]
        reach -= stations[-1]
        if reach <= 0:
            break
        branch = _continuation(graph, branch, True)
        if branch is None:
            break
    return None


def _sites(frame, graph, data, box, french):
    """Keep surveyed movements first, then complete decisions at the same live junction."""
    sites = {}
    junctions = {}
    rings = {}
    margin = ADVANCE_REACH_M + CONTINUATION_M
    ordered = sorted(data, key=lambda p: (
        min(SOURCE_ORDER[d['source']] for d in p['panels']), 'junction' not in p, p['id'],
    ))
    for plan in ordered:
        at = _xy(frame, plan['at'])
        if not _near_box(box, at, margin) or not french(*at, 0.0):
            continue
        ring_plan = plan.get('junction') in ('roundabout', 'mini')
        arms = list(graph.arms(at, 2 * MATCH_M if ring_plan else MATCH_M))
        eligible = arms
        if ring_plan and any(arm.ring for arm in arms):
            eligible = [
                arm for arm in arms if not arm.ring and any(
                    entry.ring and entry.allows(True) and _connected(arm, entry) for entry in arms
                )
            ]
        approach = _match(eligible, at, _xy(frame, plan['in']), plan.get('way'), False)
        if approach is None:
            continue
        connected = [arm for arm in arms if _connected(arm, approach)]
        entries = [arm for arm in connected if arm.ring and arm.allows(True)]
        entry = min(entries, key=lambda arm: _length(arm.vertex[:2] - approach.vertex[:2]), default=None)
        path, closed = _ring_path(graph, entry) if entry is not None else ([], False)
        outlets = list(_outlets(frame, graph, plan, at, arms, approach, path))
        if not outlets:
            continue
        site = sites.setdefault(approach.key, Site(plan, approach, [], entry is not None))
        site.outlets.extend(outlets)
        if closed:
            key = frozenset(arm.part for arm in path)
            _, known = rings.setdefault(key, (path, []))
            known.extend((arm, panel) for arm, panel in outlets if 'exit' in panel)
        elif entry is None:
            legs = {arm.key: arm for arm in connected if not arm.ring}
            if 3 <= len(legs) <= 4:
                _, known = junctions.setdefault(frozenset(legs), (legs.values(), []))
                known.extend(outlets)

    def complete(approaches, known, ring):
        known = sorted(known, key=lambda row: SOURCE_ORDER[row[1]['source']])
        for arm in approaches:
            if arm.spec.cls not in THROUGH or not arm.allows(False):
                continue
            if not _near_box(box, arm.vertex, ADVANCE_REACH_M if ring else max(LOCAL_STATIONS) + 5.0):
                continue
            site = sites.get(arm.key)
            # Ordinary turns already covered by the catalogue retain its
            # checked movements, including any restrictions it found.
            checked = site and (not ring or site.plan.get('junction') == 'roundabout')
            verified = {leave.key for leave, _ in site.outlets} if checked else None
            inherited = []
            for leave, panel in known:
                if leave.key == arm.key or panel.get('local') or panel['source'] == 'route':
                    continue
                if not ring and not _connected(arm, leave):
                    continue
                if verified is not None and leave.key not in verified:
                    continue
                # A destination on another face does not inherit its survey
                # coordinates or exact mileage. Its road must still lead out.
                copy = {k: v for k, v in panel.items() if k not in {'distance', 'centre'}}
                copy['source'] = 'route'
                if ring:
                    copy.pop('route_m', None)
                inherited.append((leave, copy))
            if not inherited:
                continue
            if site is None:
                if not french(*arm.vertex):
                    continue
                lat, lon, _ = frame.enu_to_geodetic(*arm.vertex)
                ident = f'junction/{graph.ids[arm.part]}/{lon:.7f},{lat:.7f}/{arm.sense}'
                site = sites[arm.key] = Site({'id': ident}, arm, [], ring)
            site.outlets.extend(inherited)

    # Circles without their own catalogue entry can still use downstream
    # destinations, provided the real exit leads to the labelled continuation.
    labelled = _straight_labels(sites.values())
    seen_parts = {part for key in rings for part in key}
    for part, (spec, line, way, ring) in enumerate(graph.parts):
        if not ring or part in seen_parts or not spec.dir:
            continue
        start = line if spec.dir > 0 else line[::-1]
        if not any(_near_box(box, point, ADVANCE_REACH_M) for point in start):
            continue
        path, closed = _ring_path(graph, Arm(part, spec, start, way, spec.dir, True, True))
        parts = frozenset(arm.part for arm in path)
        seen_parts.update(parts)
        if closed:
            rings.setdefault(parts, (path, []))

    for path, known in rings.values():
        legs = _ring_legs(graph, path)
        named = {leave.key for leave, _ in known}
        for leave in legs:
            if leave.key in named or not leave.allows(True) or leave.spec.cls not in THROUGH:
                continue
            panel = _downstream_label(graph, leave, labelled)
            if panel is not None:
                lat, lon, _ = frame.enu_to_geodetic(*leave.vertex)
                known.append((leave, dict(panel, exit=[round(lon * 1.e7), round(lat * 1.e7)])))
        if not known:
            continue
        complete(legs, known, ring=True)
    for approaches, known in junctions.values():
        complete(approaches, known, ring=False)
    return sites.values(), rings.values()


def _settlements(frame, box):
    x0, y0, x1, y1 = box
    margin = max(PLACE_REACH.values()) + ADVANCE_REACH_M
    lat, lon, _ = frame.enu_to_geodetic(
        np.array([x0 - margin, x0 - margin, x1 + margin, x1 + margin]),
        np.array([y0 - margin, y1 + margin, y0 - margin, y1 + margin]), np.zeros(4)
    )
    bounds = min(lon), min(lat), max(lon), max(lat)
    return [
        (
            _name(place['name']), PLACE_REACH[place['place']],
            np.asarray(frame.geodetic_to_enu(place['lat'], place['lon'], 0.0)[:2]),
        )
        for place in townsigns.bundled().nearby(bounds, places=True)
        if place.get('place') in PLACE_REACH
    ]


def _settlement(at, urban, places):
    # The built-up area plus a mapped place identifies the locality. Outside
    # that area only the immediate core qualifies, so rural approaches keep
    # their legitimate destinations, including villages of the same commune.
    close = [
        (distance, name)
        for name, reach, point in places
        if (distance := _length(at - point)) <= reach * (1.0 if urban else 0.23)
    ]
    return min(close, default=(0.0, ''))[1]


def _display(panel, urban, locality):
    names = ['Centre-ville'] if panel.get('centre') and urban else panel['names']
    distance = [frroadmesh.distance_label(m) for m in panel.get('distance', ())]
    if not distance and not urban and panel.get('route_distances'):
        distance = [frroadmesh.distance_label(m, estimated=True) if m else ''
                    for m in panel['route_distances']]
    if not distance and not urban and panel.get('route_m'):
        distance = [frroadmesh.distance_label(panel['route_m'], estimated=True)]
    labels, distances = [], []
    for i, name in enumerate(names):
        if panel['source'] == 'route' and _name(name) in {locality, 'mairie', 'hoteldeville'}:
            continue
        labels.append(name)
        distances.append(distance[i] if i < len(distance) else '')
    return dict(panel, names=labels, display_distance=distances,
                local=panel.get('local', False) or names == ['Centre-ville'])


def _panels(at, approach, outlets, urban, locality):
    incoming, _ = furniture._at(approach.line, np.array([12.0]))
    travel = approach.vertex[:2] - incoming[0, :2]
    result = []
    seen = set()
    signed_names = {_name(name) for _, panel in outlets if panel['source'] != 'route' for name in panel['names']}
    inferred_names = set()
    for leave, panel in sorted(outlets, key=lambda row: SOURCE_ORDER[row[1]['source']]):
        if approach.key == leave.key:
            continue
        if 'exit' in panel:
            sample, _ = furniture._at(leave.line, np.array([25.0]))
            direction = sample[0, :2] - at
        else:
            sample, _ = furniture._at(leave.line, np.array([12.0]))
            direction = sample[0, :2] - leave.vertex[:2]
        angle = math.degrees(
            math.atan2(signplacement.cross(travel, direction), float(travel @ direction))
        )
        if abs(angle) > 143.0:
            continue
        turn = 'left' if angle > 33.0 else 'right' if angle < -33.0 else 'straight'
        panel = _display(panel, urban, locality)
        labels, distances = [], []
        for name, distance in zip(panel['names'], panel['display_distance']):
            normal = _name(name)
            key = leave.key, normal
            if key not in seen and (panel['source'] != 'route' or normal not in signed_names | inferred_names):
                seen.add(key)
                if panel['source'] == 'route':
                    inferred_names.add(normal)
                labels.append(name)
                distances.append(distance)
        if labels:
            result.append(dict(panel, names=labels, turn=turn, angle=angle, display_distance=distances))
    return result


def barrier_context(rows):
    """A small envelope around each rendered steel, concrete or cable module."""
    import euguardrails

    a, b = euguardrails._rail_segments(rows)
    rings = []
    for start, end in zip(a, b):
        axis = end - start
        length = _length(axis)
        if length > 0.05:
            normal = _right(axis) * (0.36 / length)
            rings.append([start + normal, end + normal, end - normal, start - normal])
    return signplacement.Context(rings)


@dataclass
class Clearance:
    box: tuple
    french: object
    pavement: signplacement.Context
    obstacles: signplacement.Context
    rails: signplacement.Context
    occupied: list

    def face(self, q, z, facing, width, eyes, share=None):
        if not geotiles.box_owns(self.box, *q) or not self.french(*q, z):
            return False
        if any((share is None or owner != share) and _length(q - p) < distance
               for p, distance, owner in self.occupied):
            return False
        a, _, b = signplacement.span(q, facing, width)
        return (self.pavement.visible(a, b, .38) and self.rails.visible(a, b, .42)
                and self.obstacles.post(q, facing, width, eyes))


def _position(
    frame,
    plan,
    approach,
    line,
    width,
    clearance,
    advance=False,
    mounts=(),
    across=False,
):
    length = furniture._arclen(line)[-1]
    if length < 15.0:
        if across and geotiles.box_owns(clearance.box, *approach.vertex[:2]):
            return _across(approach, line, width, clearance)
        return None
    half = approach.spec.width / 2
    candidates = []
    if not advance and 'site' in plan:
        site = _xy(frame, plan['site'])
        station = furniture._project(line, site[None])[0]
        if 2.0 <= station <= min(38.0, length - 9.0):
            points, outward = furniture._at(line, np.array([station]))
            offset = float((site - points[0, :2]) @ _right(-outward[0]))
            if half <= offset <= half + 9.0:
                candidates.append((station, site, None))
    stations = [s for s in (ADVANCE_STATIONS if advance else LOCAL_STATIONS) if s <= length - 9.0]
    if candidates:
        nominal = candidates[0][1]
    elif stations:
        p, outward = furniture._at(line, np.array(stations[:1]))
        nominal = p[0, :2] + _right(-outward[0]) * (half + width / 2 + 0.65)
    else:
        return None
    # The nominal position chooses one owner before obstacle avoidance, even
    # if the sign sits in the tile upstream of the junction.
    if not geotiles.box_owns(clearance.box, *nominal):
        return None
    if not candidates:
        for index, row in mounts:
            point = np.asarray(row[1:3])
            station = furniture._project(line, point[None])[0]
            if 3.0 <= station <= min(18.0, length - 9.0):
                candidates.append((station, point, (index, row)))
    candidates.extend((s, None, None) for s in stations)
    for station, fixed, mount in candidates:
        offsets = np.array([station, station + 10.0, min(length, station + 22.0)])
        points, tangents = furniture._at(line, offsets)
        into = -tangents[0]
        right = _right(into)
        eyes = [
            p[:2] + _right(-d) * min(1.35, half * 0.40) for p, d in zip(points[1:], tangents[1:])
        ]
        c = points[0, :2]
        index = None
        if mount is not None:
            index, row = mount
            yaw = math.radians(row[4])
            axis = np.array([-math.sin(yaw), math.cos(yaw)])
            offset = float((fixed - c) @ right)
            if not (half + .20 <= offset <= half + 3.8 and float(axis @ into) >= .91
                    and abs(row[3] - points[0, 2]) < 1.0):
                continue
            spots = [fixed + .016 * axis]
        elif fixed is not None:
            spots = [fixed]
        else:
            setbacks = (width / 2 + .65, .65, 1.05, width / 2 + 1.35, width / 2 + 2.3, width / 2 + 3.2)
            spots = [c + right * (half + setback) for setback in setbacks]
        for q in spots:
            if any(float((q - eye) @ into) < 0.62 * _length(q - eye) for eye in eyes):
                continue
            facing = q - (eyes[0] + eyes[1]) / 2
            facing /= _length(facing)
            if not clearance.face(q, points[0, 2], facing, width, eyes, share=index):
                continue
            z = clearance.obstacles.z(q, points[0, 2]) if mount is None else row[3]
            top = None if mount is None else row[6] * row[5]
            return np.r_[q, z], facing, fixed is not None and mount is None, station, top
    return _across(approach, line, width, clearance) if across else None


def _across(approach, line, width, clearance):
    """A stem of a T can face its drivers across the priority road."""
    length = furniture._arclen(line)[-1]
    if length < 8.0:
        return None
    points, outward = furniture._at(line, np.array([0., min(length * .55, 11.), min(length, 23.)]))
    into = -outward[0]
    right = _right(into)
    eyes = [p[:2] + _right(-d) * min(1.35, approach.spec.width * .20)
            for p, d in zip(points[1:], outward[1:])]
    for setback in (8., 11., 15., 19.):
        for lateral in (0., 2.5, -2.5, 4.5, -4.5):
            q = points[0, :2] + into * setback + right * lateral
            facing = q - (eyes[0] + eyes[1]) / 2
            facing /= _length(facing)
            if clearance.face(q, points[0, 2], facing, width, eyes):
                z = clearance.obstacles.z(q, points[0, 2])
                if abs(z - points[0, 2]) < 1.35:
                    return np.r_[q, z], facing, False, -setback, None
    return None


def _urban(frame, point, spec, urban_fn):
    if spec.urban:
        return True
    if urban_fn is None:
        return False
    lat, lon, _ = frame.enu_to_geodetic(*point)
    return bool(np.asarray(urban_fn(np.array([lat]), np.array([lon]))).ravel()[0])


def _direction_record(ident, kind, layout, position, surveyed=False, support=None):
    return {
        'id': ident, 'kind': kind, 'pos': position.tolist(), 'route': layout[1],
        'name': ' / '.join(', '.join(row[1]) for row in layout[0]),
        'source': layout[0][0][0]['source'], 'surveyed_position': surveyed,
        **({'support': support} if support else {}),
        'panels': [
            {
                'names': names, 'turn': panel['turn'], 'source': panel['source'],
                **({'distance_km': distances} if any(distances) else {}),
            }
            for panel, names, _, _, _, distances, _ in layout[0]
        ],
    }


def _exit_position(graph, leave, width, clearance):
    """Face cars approaching this exit along the actual circulating lane."""
    vertex = leave.vertex
    incoming = [
        arm for arm in graph.arms(vertex[:2], 1.8)
        if arm.ring and arm.allows(False) and _connected(leave, arm)
    ]
    if not incoming:
        return None
    arm = min(incoming, key=lambda a: _length(a.vertex[:2] - vertex[:2]))
    flow = _approach_line(graph, arm, reach=24.)
    length = furniture._arclen(flow)[-1]
    if length < 4.:
        return None
    points, tangents = furniture._at(flow, np.array([min(5., .42 * length), min(12., .85 * length)]))
    eyes = [p[:2] + _right(-d) * min(1.2, arm.spec.width * .20) for p, d in zip(points, tangents)]
    into = -tangents[0]
    end = furniture._arclen(leave.line)[-1]
    stations = list(dict.fromkeys(min(s, end - .4) for s in (6., 10., 3., 15., 21.)))
    if stations[0] < 1.7:
        return None
    points, tangents = furniture._at(leave.line, np.asarray(stations))
    half = leave.spec.width / 2
    nominal = points[0, :2] + _right(tangents[0]) * (half + width / 2 + .55)
    if not geotiles.box_owns(clearance.box, *nominal):
        return None
    for p, outward in zip(points, tangents):
        right = _right(outward)
        for side in (1., -1.):
            setbacks = (width / 2 + .55, .63, 1., width / 2 + 1.15, width / 2 + 2., width / 2 + 3.)
            for setback in setbacks:
                q = p[:2] + side * right * (half + setback)
                if float((q - eyes[0]) @ into) < 0:
                    continue
                facing = q - (eyes[0] + eyes[1]) / 2
                facing /= _length(facing)
                points_to = float(_right(facing) @ outward)
                if abs(points_to) < .23 or not clearance.face(q, p[2], facing, width, eyes):
                    continue
                z = clearance.obstacles.z(q, p[2])
                if abs(z - p[2]) <= 1.35:
                    return np.r_[q, z], facing, 'right' if points_to > 0 else 'left'
    return None


def _exit_boards(frame, graph, rings, places, urban_fn, mesh, clearance):
    exits = {}
    for _, known in rings:
        # Each inferred place belongs to its strongest documented exit. A
        # surveyed destination can legitimately occur on more than one face.
        votes = {}
        for leave, panel in known:
            for name in panel['names']:
                by_exit = votes.setdefault(_name(name), {})
                strength, count = by_exit.get(leave.key, (len(SOURCE_ORDER), 0))
                by_exit[leave.key] = (min(strength, SOURCE_ORDER[panel['source']]), count - 1)
        preferred = {
            name: min(by_exit, key=lambda key: (*by_exit[key], key[2], str(graph.ids[key[0]]), key[1]))
            for name, by_exit in votes.items()
        }
        for leave, panel in known:
            if not _near_box(clearance.box, leave.vertex, 30.):
                continue
            if panel['source'] == 'route':
                indices = [i for i, name in enumerate(panel['names']) if preferred[_name(name)] == leave.key]
                if not indices:
                    continue
                panel = dict(panel, names=[panel['names'][i] for i in indices])
                if 'distance' in panel:
                    panel['distance'] = [panel['distance'][i] for i in indices if i < len(panel['distance'])]
                if 'route_distances' in panel:
                    panel['route_distances'] = [panel['route_distances'][i] for i in indices]
                if 0 not in indices:
                    panel.pop('route_m', None)
            exits.setdefault(leave.key, (leave, []))[1].append(panel)
    result = []
    for leave, choices in exits.values():
        if len(result) >= 6:
            break
        urban = _urban(frame, leave.vertex, leave.spec, urban_fn)
        locality = _settlement(leave.vertex[:2], urban, places)
        seen = set()
        panels = []
        for panel in sorted(choices, key=lambda p: (SOURCE_ORDER[p['source']], p.get('local', False))):
            row = _display(panel, urban, locality)
            labels = [(name, distance) for name, distance in zip(row['names'], row['display_distance'])
                      if _name(name) not in seen]
            if labels:
                names, distances = zip(*labels)
                panels.append(dict(row, names=names, display_distance=distances, turn='right'))
                seen.update(map(_name, names))
            if len(panels) == 2:
                break
        layout = frroadmesh.local_layout(panels)
        if not layout[0]:
            continue
        hit = _exit_position(graph, leave, layout[2], clearance)
        if hit is None:
            continue
        p, facing, turn = hit
        layout = frroadmesh.local_layout([dict(panel, turn=turn) for panel in panels])
        frroadmesh.local_direction(mesh, p, facing, layout)
        clearance.occupied.append((p[:2], max(3.8, layout[2] / 2 + 1.8), None))
        lat, lon, _ = frame.enu_to_geodetic(*leave.vertex)
        ident = f'exit/{graph.ids[leave.part]}/{lon:.7f},{lat:.7f}/{leave.sense}'
        result.append(_direction_record(ident, 'direction-exit', layout, p))
    return result


def add(
    frame,
    roads,
    box,
    french,
    mesh,
    way_ids=None,
    ground=None,
    obstacles=None,
    decks=(),
    guardrails=(),
    roundabouts=(),
    props=(),
    records=(),
    urban_fn=None,
    data=None,
    context_roads=(),
    pavement_rings=(),
):
    if not roads or not any(sp.cls in LOCAL and sp.kind == 'paved' for _, sp, _, _ in roads):
        return []
    data = nearby(frame, box) if data is None else data
    if not data:
        return []
    graph = RoadGraph([*roads, *context_roads], way_ids or {}, roundabouts)
    pavement = signplacement.pavement(
        [(sp.width / 2, line) for _, sp, line, _ in roads] + [(half, line) for line, half in decks],
        rings=pavement_rings,
    )
    obstacles = obstacles if obstacles is not None else signplacement.Context(ground=ground)
    rails = barrier_context(guardrails)
    occupied = [(np.asarray(r[1:3]), 1.25, i) for i, r in enumerate(props) if isinstance(r[0], str)]
    occupied.extend((np.asarray(record['pos'][:2]), 2.5, None) for record in records if 'pos' in record)
    mounts = [(i, r) for i, r in enumerate(props) if r[0] == 'fr_giveway_m9c']
    clearance = Clearance(box, french, pavement, obstacles, rails, occupied)
    places = _settlements(frame, box)
    sites, rings = _sites(frame, graph, data, box, french)
    result = _exit_boards(frame, graph, rings, places, urban_fn, mesh, clearance)
    candidates = []
    for site in sites:
        at = site.approach.vertex
        urban = _urban(frame, at, site.approach.spec, urban_fn)
        panels = _panels(at[:2], site.approach, site.outlets, urban, _settlement(at[:2], urban, places))
        if not panels:
            continue
        turns = {panel['turn'] for panel in panels}
        if 'at' not in site.plan and not site.ring and turns == {'straight'}:
            continue
        priority = (not (site.ring or site.plan.get('junction') == 'mini'),
                    min(SOURCE_ORDER[panel['source']] for panel in panels), -len(turns),
                    'junction' not in site.plan, site.plan['id'])
        candidates.append((priority, site, panels))
    local_count = advances = 0
    for _, site, panels in sorted(candidates, key=lambda row: row[0]):
        if local_count >= 10 and advances >= 4:
            break
        plan, approach = site.plan, site.approach
        local = local_count < 10 and _near_box(box, approach.vertex, max(LOCAL_STATIONS) + 8)
        advance = advances < 4 and site.ring and approach.spec.cls in THROUGH
        layout = frroadmesh.local_layout(panels)
        hit = None
        if local and layout[0]:
            line = _approach_line(graph, approach)
            hit = _position(
                frame,
                plan,
                approach,
                line,
                layout[2],
                clearance,
                mounts=mounts if len(layout[0]) <= 3 and layout[2] <= 2.45 else (),
                across=not site.ring and {p['turn'] for p in panels} == {'left', 'right'},
            )
        if hit is not None:
            p, into, surveyed_position, _, yield_top = hit
            frroadmesh.local_direction(mesh, p, into, layout, yield_top=yield_top)
            occupied.append((p[:2], max(3.6, layout[2] / 2 + 1.8) if site.ring else 5.0, None))
            local_count += 1
            result.append(_direction_record(plan['id'], 'direction', layout, p, surveyed_position,
                                            'yield' if yield_top is not None else None))
        if advance:
            diagram = frroadmesh.roundabout_layout(panels)
            if diagram is None:
                continue
            line = _approach_line(graph, approach, reach=146.0, stop_at_junction=True)
            hit = _position(
                frame,
                plan,
                approach,
                line,
                diagram[1],
                clearance,
                advance=True,
            )
            if hit is None:
                continue
            p, into, _, station, _ = hit
            frroadmesh.roundabout(mesh, p, into, diagram)
            advances += 1
            occupied.append((p[:2], 18.0, None))
            result.append(
                {
                    'id': plan['id'] + '/roundabout',
                    'kind': 'roundabout-advance',
                    'pos': p.tolist(),
                    'name': ' / '.join(', '.join(panel['names']) for panel in diagram[0].values()),
                    'source': panels[0]['source'],
                    'distance_to_entry_m': round(station),
                    'panels': [
                        {'names': panel['names'], 'turn': turn, 'source': panel['source']}
                        for turn, panel in diagram[0].items()
                    ],
                }
            )
    return result
