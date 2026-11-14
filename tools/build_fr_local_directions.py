"""Build the French local-direction tile archive, offline.

Run extract_fr_direction_network.py first. This step uses NumPy and SciPy.
Surveyed relations and explicitly tagged road destinations have priority over
estimated scenery. Inference follows a directed road graph towards named places
and public facilities. It is not a navigation service or a sign inventory.
"""

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import heapq
import json
import math
import multiprocessing
from pathlib import Path
import re
import sys
import zipfile

import numpy as np
from scipy.spatial import cKDTree

from extract_fr_direction_network import CLASSES, ROUNDABOUT, SEPARATED, PLACES, is_public

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import countrybounds

SCALE = np.array([0.00767, 0.01112])  # metres per degree E7 near the centre of France
CELL = 2_500_000  # 0.25 degree archive shards
MAJOR = CLASSES.index("unclassified")
LOCAL = CLASSES.index("living_street")
RADIUS = {"city": 7500, "town": 5200, "village": 3400, "hamlet": 2100, "suburb": 2500}
PRIORITY = {"relation": 0, "road": 1, "route": 2}
ROUTE = re.compile(r"[ADNME]\s*\d{1,4}[A-Z]?", re.I)
DISTANCE = re.compile(r"(\d{1,4}(?:[.,]\d{1,2})?)\s*(km|m)?", re.I)


def labels(value):
    return list(
        dict.fromkeys(
            s.strip()
            for s in str(value).split(";")
            if 1 < len(s.strip()) <= 90 and s.strip().lower() not in {"none", "yes", "no"}
        )
    )[:4]


def route_ref(value):
    value = str(value).split(";")[0].strip().upper()
    return value if ROUTE.fullmatch(value) else ""


def colour(value):
    return {"green": "green", "#008000": "green", "blue": "blue", "#0000ff": "blue"}.get(
        str(value).lower(), "white"
    )


def mapped_distances(value, names):
    parts = str(value).split(';')
    if len(parts) != len(names):
        return []
    result = []
    for part in parts:
        match = DISTANCE.fullmatch(part.strip())
        if match is None:
            return []
        number, unit = match.groups()
        distance = float(number.replace(',', '.')) * (
            1.0 if (unit or '').lower() == 'm' else 1000.0
        )
        if not 100.0 <= distance <= 450_000.0:
            return []
        result.append(round(distance))
    return result


def _destination(tags, forward, oneway):
    suffix = ":forward" if forward else ":backward"
    for key in ("destination", "destination:street"):
        value = tags.get(key + suffix) or (tags.get(key) if oneway else None)
        if value and (names := labels(value)):
            return (
                names,
                route_ref(tags.get("destination:ref" + suffix) or tags.get("destination:ref", "")),
                colour(
                    tags.get("destination:colour" + suffix) or tags.get("destination:colour", "")
                ),
                mapped_distances(
                    tags.get('destination:distance' + suffix)
                    or (tags.get('destination:distance') if oneway else ''),
                    names,
                ),
            )
    # A mixed lane list cannot reliably label one outgoing road. Identical
    # non-empty lanes still carry an unambiguous destination for this branch.
    value = tags.get("destination:lanes" + suffix) or (
        tags.get("destination:lanes") if oneway else None
    )
    if value:
        lanes = {tuple(labels(v)) for v in value.split("|") if labels(v)}
        if len(lanes) == 1:
            return list(lanes.pop()), route_ref(tags.get("destination:ref", "")), "white", []
    return None


@dataclass(frozen=True)
class Goal:
    kind: str
    name: str
    node: int
    xy: tuple
    ident: int


def _indices(ids, values):
    values = np.asarray(list(values), dtype=ids.dtype)
    if not len(ids):
        return np.empty(0, dtype=np.int64)
    ix = np.searchsorted(ids, values)
    valid = (ix < len(ids)) & (ids[np.minimum(ix, len(ids) - 1)] == values)
    return ix[valid]


def _csr(ends, size):
    counts = np.bincount(ends, minlength=size)
    offsets = np.r_[0, np.cumsum(counts, dtype=np.int32)].astype(np.int32)
    return memoryview(offsets), memoryview(np.argsort(ends, kind="stable").astype(np.int32))


class Graph:
    def __init__(self, arrays, notes, *, include_highways=False):
        """Contract shape points, keeping junctions, endpoints, and goal nodes."""
        ways = arrays["wayids"]
        kinds, flags = arrays["kinds"], arrays["flags"]
        xy = arrays["xy"]
        offsets = arrays["offsets"]
        ids, first, inverse, occurrences = np.unique(
            arrays["nodes"], return_index=True, return_inverse=True, return_counts=True
        )
        print(f'Indexed {len(ids):,} unique road nodes', flush=True)
        all_xy = xy[first]
        inverse = inverse.astype(np.int32)
        keep = occurrences > 1
        keep[inverse[offsets[:-1]]] = True
        keep[inverse[offsets[1:] - 1]] = True
        restricted_nodes = {
            ident
            for _, _, members in notes["restrictions"]
            for typ, ident, role in members
            if typ == "n" and role == "via"
        }
        relation_nodes = {int(key) for key in notes["relation_nodes"]}
        barriers = _indices(ids, notes["barriers"])
        keep[barriers] = True
        keep[_indices(ids, restricted_nodes | relation_nodes)] = True
        usable = np.ones(len(kinds), bool) if include_highways else kinds >= 2
        local_points = np.zeros(len(ids), bool)
        # Snap goals to public local roads, not to the motorway passing nearby.
        local_points[inverse[np.repeat((kinds >= 2) & ((flags & SEPARATED) == 0), np.diff(offsets))]] = (
            True
        )
        local_points[barriers] = False
        snap = np.flatnonzero(local_points)
        raw_targets = self._targets(notes["targets"], all_xy, snap)
        print(f'Connected {len(raw_targets):,} place and facility targets', flush=True)
        keep[[node for _, _, node, _, _ in raw_targets]] = True

        vertex = np.cumsum(keep, dtype=np.int32) - 1
        self.xy = all_xy[keep]
        self.ids = ids[keep]
        self.goals = [
            Goal(kind, name, int(vertex[node]), pos, ident)
            for kind, name, node, pos, ident in raw_targets
        ]
        self.barriers = set(map(int, vertex[barriers]))
        del all_xy, snap, local_points, ids, occurrences, first, raw_targets

        cuts = np.flatnonzero(keep[inverse])
        last = np.zeros(len(xy), bool)
        last[offsets[1:] - 1] = True
        take = ~last[cuts[:-1]]
        start, end = cuts[:-1][take], cuts[1:][take]
        way = np.searchsorted(offsets[1:], start, side="right").astype(np.int32)
        take = usable[way] & (inverse[start] != inverse[end])
        start, end, way = start[take], end[take], way[take]
        delta = np.diff(xy, axis=0).astype(np.float64)
        latitude = (xy[:-1, 1].astype(float) + xy[1:, 1]) * (math.pi / 360e7)
        steps = 0.0111195 * np.hypot(delta[:, 0] * np.cos(latitude), delta[:, 1])
        steps[last[:-1]] = 0
        del delta, latitude, last
        along = np.r_[0, np.cumsum(steps)]
        del steps
        length = along[end] - along[start]
        take = length >= 0.2
        start, end, way, length = start[take], end[take], way[take], length[take]
        src, dst = vertex[inverse[start]], vertex[inverse[end]]
        del inverse, vertex, keep, cuts
        degree = np.bincount(np.r_[src, dst], minlength=len(self.ids))
        major = kinds[way] <= MAJOR
        majors = np.bincount(np.r_[src[major], dst[major]], minlength=len(self.ids))
        self.candidate = (degree >= 3) & (majors >= 2)

        forward = arrays["dirs"][way] >= 0
        backward = arrays["dirs"][way] <= 0
        self.src = memoryview(np.r_[src[forward], dst[backward]])
        self.dst = memoryview(np.r_[dst[forward], src[backward]])
        self.edgeway = memoryview(np.r_[way[forward], way[backward]])
        self.forward = memoryview(
            np.r_[np.ones(forward.sum(), bool), np.zeros(backward.sum(), bool)]
        )
        self.wayids = memoryview(ways)
        self.edgeids = memoryview(ways[np.asarray(self.edgeway)])
        self.kind = memoryview(kinds)
        self.flag = memoryview(flags)
        self.dir = memoryview(arrays["dirs"])
        self.refs = memoryview(arrays["refs"])
        self.length = memoryview(np.r_[length[forward], length[backward]].astype(np.float32))
        # A 12 m chord is stable even when the first curve point is centimetres away.
        look_a = np.minimum(end, np.searchsorted(along, along[start] + 12.0))
        look_b = np.maximum(start, np.searchsorted(along, along[end] - 12.0, side="right") - 1)
        self.leave = xy[np.r_[look_a[forward], look_b[backward]]]
        self.arrive = xy[np.r_[look_b[forward], look_a[backward]]]
        self.at_way_start = memoryview(
            np.r_[
                start[forward] == offsets[way[forward]],
                end[backward] == offsets[way[backward] + 1] - 1,
            ]
        )
        weights = np.array([1.0, 1.0, 1.0, 1.0, 1.1, 1.3, 1.55, 1.75, 2.0])
        edge_kinds = kinds[np.asarray(self.edgeway)]
        self.cost = memoryview((np.asarray(self.length) * weights[edge_kinds]).astype(np.float32))
        self.in_offsets, self.in_edges = _csr(np.asarray(self.dst), len(self.ids))
        self.out_offsets, self.out_edges = _csr(np.asarray(self.src), len(self.ids))
        self.rules, self.closed_ways = self._restrictions(notes["restrictions"])
        self.planable = self._planable_edges(edge_kinds)
        self.junction_nodes = np.flatnonzero(degree >= 3)
        self.junctions = cKDTree(self.xy[self.junction_nodes] * SCALE)

    @staticmethod
    def _targets(rows, all_xy, snap):
        if not rows or not len(snap):
            return []
        tree = cKDTree(all_xy[snap] * SCALE)
        points = np.array([[r[2], r[3]] for r in rows])
        distances, indexes = tree.query(points * SCALE, workers=2)
        targets, used = [], set()
        for (kind, name, x, y, ident), distance, index in zip(rows, distances, indexes):
            if distance > (260 if kind in PLACES else 145):
                continue
            node = int(snap[index])
            # A mapped node and polygon for the same place often coincide.
            key = (kind, name.casefold(), x // 5000, y // 5000)
            if key in used:
                continue
            used.add(key)
            targets.append((kind, name, node, (x, y), ident))
        return targets

    def _restrictions(self, relations):
        rules = defaultdict(list)
        closed = set()
        for _, tags, members in relations:
            if {v.strip() for v in tags.get("except", "").split(";")} & {
                "motorcar",
                "motor_vehicle",
                "vehicle",
            }:
                continue
            restriction = next(
                (
                    tags[k]
                    for k in (
                        "restriction:motorcar",
                        "restriction:motorcar:conditional",
                        "restriction:motor_vehicle",
                        "restriction:motor_vehicle:conditional",
                        "restriction:vehicle",
                        "restriction:vehicle:conditional",
                        "restriction",
                        "restriction:conditional",
                    )
                    if k in tags
                ),
                "",
            )
            if not restriction.startswith(("no_", "only_")):
                continue
            incoming = {ident for typ, ident, role in members if typ == "w" and role == "from"}
            outgoing = {ident for typ, ident, role in members if typ == "w" and role == "to"}
            via = [ident for typ, ident, role in members if typ == "n" and role == "via"]
            via_ways = {ident for typ, ident, role in members if typ == "w" and role == "via"}
            # Exclude a via-way corridor from scenic routing rather than guessing
            # where a multi-step restriction starts to apply. For an only_* rule
            # the from-way is also excluded to avoid picking a forbidden detour.
            closed.update(via_ways)
            if via_ways and restriction.startswith("only_"):
                closed.update(incoming)
            if len(via) != 1 or not outgoing:
                continue
            index = self.node(via[0])
            if index is not None:
                for way in incoming:
                    rules[(index, way)].append((restriction.split(" @")[0], outgoing))
        return dict(rules), closed

    def node(self, ident):
        i = int(np.searchsorted(self.ids, self.ids.dtype.type(ident)))
        return i if i < len(self.ids) and self.ids[i] == ident else None

    def way(self, edge):
        return self.edgeids[edge]

    def incoming(self, node):
        return self.in_edges[self.in_offsets[node] : self.in_offsets[node + 1]]

    def outgoing(self, node):
        return self.out_edges[self.out_offsets[node] : self.out_offsets[node + 1]]

    def turn_allowed(self, inbound, outbound):
        node = self.dst[inbound]
        if node in self.barriers or self.src[inbound] == self.dst[outbound]:
            return False
        before, after = self.way(inbound), self.way(outbound)
        if before in self.closed_ways or after in self.closed_ways:
            return False
        for rule, ways in self.rules.get((node, before), ()):
            if (rule.startswith("only_") and after not in ways) or (
                rule.startswith("no_")
                and after in ways
                and (rule != "no_u_turn" or before != after)
            ):
                return False
        return True

    def _planable_edges(self, kinds):
        active = self.candidate[np.asarray(self.dst)] & (kinds <= MAJOR)
        flag = np.asarray(self.flag)[np.asarray(self.edgeway)]
        active &= (flag & (ROUNDABOUT | SEPARATED)) == 0
        return memoryview(active)

    def turn(self, inbound, outbound, via=None):
        node = self.dst[inbound]
        # The ring's exit bearing is measured across the whole ring.
        x, y = map(int, self.xy[node])
        ax, ay = self.arrive[inbound]
        bx, by = self.leave[outbound]
        vx, vy = (x, y) if via is not None else self.xy[self.src[outbound]]
        ux, uy = (x - int(ax)) * 0.00767, (y - int(ay)) * 0.01112
        dx, dy = (int(bx) - int(vx)) * 0.00767, (int(by) - int(vy)) * 0.01112
        angle = math.degrees(math.atan2(ux * dy - uy * dx, ux * dx + uy * dy))
        if abs(angle) > 143:
            return None
        return "left" if angle > 33 else "right" if angle < -33 else "straight"


def _panel(g, edge, names, ref, paint, source, ring=None):
    node = g.src[edge]
    return {
        "out": g.leave[edge].tolist(),
        "way": g.way(edge),
        "names": names[:3],
        "ref": ref,
        "colour": paint,
        "source": source,
        **(
            {"exit": g.xy[node].tolist(), "ring": g.leave[ring].tolist()}
            if ring is not None
            else {}
        ),
    }


def _score_approach(g, edge, site):
    node = g.dst[edge]
    u = (g.arrive[edge] - g.xy[node]) * SCALE
    u /= max(float(np.linalg.norm(u)), 1e-6)
    delta = (site - g.xy[node]) * SCALE
    along = float(delta @ u)
    offset = abs(float(delta[0] * u[1] - delta[1] * u[0]))
    return offset + 1.7 * max(0.0, -along) + 0.1 * max(0.0, along - 35.0)


def mapped_relations(g, notes):
    plans = defaultdict(list)
    actual = notes["relation_nodes"]
    mapped_by_id = {ident: tags for ident, _, _, tags in notes["mapped_nodes"]}
    for ident, tags, members in notes["relations"]:
        signs = [i for typ, i, role in members if typ == "n" and role == "sign"]
        names = labels(tags.get("destination") or tags.get("destination:street") or "")
        if not names:
            names = labels(
                next(
                    (mapped_by_id[i].get("destination", "") for i in signs if i in mapped_by_id), ""
                )
            )
        if not names or not is_public(tags):
            continue
        if any(
            tags.get(key) in {'yes', 'designated'} for key in ('foot', 'hiking', 'bicycle')
        ) and not any(
            tags.get(key) in {'yes', 'designated'} for key in ('motorcar', 'motor_vehicle')
        ):
            continue
        from_ways = {i for typ, i, role in members if typ == "w" and role == "from"}
        to_ways = {i for typ, i, role in members if typ == "w" and role == "to"}
        to_nodes = {i for typ, i, role in members if typ == "n" and role == "to"}
        via = [
            g.node(i) for typ, i, role in members if typ == "n" and role in {"via", "intersection"}
        ]
        via = [n for n in via if n is not None]
        sites = [actual[str(i)] for i in signs if str(i) in actual]
        if not via and sites and (from_ways or to_ways or to_nodes):
            hits = g.junctions.query_ball_point(np.asarray(sites[0]) * SCALE, 32.0)
            via = sorted(
                (int(g.junction_nodes[i]) for i in hits),
                key=lambda n: float(np.linalg.norm((g.xy[n] - sites[0]) * SCALE)),
            )[:5]
        for node in via:
            outgoing = [e for e in g.outgoing(node) if g.kind[g.edgeway[e]] <= LOCAL]
            if to_ways:
                outgoing = [e for e in outgoing if g.way(e) in to_ways]
            elif to_nodes:
                candidates = [actual[str(i)] for i in sorted(to_nodes) if str(i) in actual]
                ranks = []
                for e in outgoing:
                    v = (g.leave[e] - g.xy[node]) * SCALE
                    v /= max(float(np.linalg.norm(v)), 1e-6)
                    rank = max(
                        (
                            float((p - g.xy[node]) * SCALE @ v)
                            / max(float(np.linalg.norm((p - g.xy[node]) * SCALE)), 1e-6)
                            for p in candidates
                        ),
                        default=0.0,
                    )
                    ranks.append((rank, e))
                ranks.sort(reverse=True)
                outgoing = (
                    [ranks[0][1]]
                    if ranks
                    and ranks[0][0] > 0.81
                    and (len(ranks) == 1 or ranks[0][0] - ranks[1][0] > 0.10)
                    else []
                )
            else:
                continue
            if len(outgoing) != 1:
                continue
            out = outgoing[0]
            inbound = [
                e
                for e in g.incoming(node)
                if g.kind[g.edgeway[e]] <= LOCAL
                and (not from_ways or g.way(e) in from_ways)
                and g.turn_allowed(e, out)
                and g.turn(e, out) is not None
            ]
            if not inbound:
                continue
            site = (
                min(sites, key=lambda p: float(np.linalg.norm((p - g.xy[node]) * SCALE)))
                if sites
                else None
            )
            if site is not None and len(inbound) > 1:
                ranked = sorted((_score_approach(g, e, np.asarray(site)), e) for e in inbound)
                # An explicitly located board can identify its own approach.
                if ranked[0][0] > 15.0 or ranked[1][0] - ranked[0][0] < 1.0:
                    continue
                inbound = [ranked[0][1]]
            elif len(inbound) > 1 and len({g.way(e) for e in inbound} & from_ways) != len(inbound):
                continue
            for edge in inbound:
                panel = _panel(
                    g,
                    out,
                    names,
                    route_ref(tags.get("destination:ref", "")),
                    colour(tags.get("colour:back", "")),
                    "relation",
                )
                panel["relation"] = ident
                if distances := mapped_distances(
                    tags.get('destination:distance') or tags.get('distance', ''), names
                ):
                    panel['distance'] = distances
                plans[edge].append((out, panel, site))
            break
    return plans


def tagged_roads(g, notes):
    wayids = np.asarray(g.wayids)
    for way_id, tags in notes["tags"].items():
        ident = wayids.dtype.type(way_id)
        index = int(np.searchsorted(wayids, ident))
        if index >= len(wayids) or wayids[index] != ident or g.kind[index] > LOCAL:
            continue
        # Only consider a tag at the start of the signed segment in that direction.
        for forward in (True, False):
            if g.dir[index] and ((g.dir[index] > 0) != forward):
                continue
            tag = _destination(tags, forward, g.dir[index] != 0)
            if tag:
                yield index, forward, tag


def add_tagged(g, notes, plans):
    tagged = {(index, forward): tag for index, forward, tag in tagged_roads(g, notes)}
    if not tagged:
        return
    possible = np.flatnonzero(
        np.asarray(g.at_way_start)
        & np.isin(np.asarray(g.edgeway), np.array(sorted({key[0] for key in tagged})))
    )
    for out in map(int, possible):
        tag = tagged.get((g.edgeway[out], g.forward[out]))
        node = g.src[out]
        if not tag or len(g.incoming(node)) + len(g.outgoing(node)) < 3:
            continue
        for incoming in g.incoming(node):
            if (
                g.kind[g.edgeway[incoming]] > LOCAL
                or not g.turn_allowed(incoming, out)
                or g.turn(incoming, out) is None
            ):
                continue
            # At least one other traversable branch makes this a decision point.
            if not any(e != out and g.turn_allowed(incoming, e) for e in g.outgoing(node)):
                continue
            if any(item[0] == out for item in plans[incoming]):
                continue
            names, ref, paint, distance = tag
            ref = ref or route_ref(notes["refs"][g.refs[g.edgeway[out]]])
            panel = _panel(g, out, names, ref, paint, "road")
            if distance:
                panel['distance'] = distance
            plans[incoming].append((out, panel, None))


def _exit_from_ring(g, first, next_edge):
    edge = first
    for _ in range(24):
        if not g.flag[g.edgeway[edge]] & ROUNDABOUT:
            return edge
        edge = next_edge.get(edge)
        if edge is None:
            return None
    return None


def _goal_choices(g, plans, goals):
    choices = defaultdict(dict)
    for goal in goals:
        budget = RADIUS.get(goal.kind, 1500)
        is_place = goal.kind in PLACES
        preference = {"city": 0.76, "town": 0.82, "village": 0.92, "hamlet": 1.25}.get(
            goal.kind, 0.9
        )
        queue = [
            (g.cost[e], e, -1)
            for e in g.incoming(goal.node)
            if g.way(e) not in g.closed_ways and g.src[e] not in g.barriers
        ]
        heapq.heapify(queue)
        best = {e: cost for cost, e, _ in queue}
        next_edge = {}
        metres = {}
        steps = 0
        while queue and steps < 6000:
            cost, edge, after = heapq.heappop(queue)
            if cost > budget:
                break
            if cost != best.get(edge):
                continue
            steps += 1
            onward_metres = metres[after] if after >= 0 else 0.0
            metres[edge] = g.length[edge] + onward_metres
            if after >= 0:
                next_edge[edge] = after
            if (
                after >= 0
                and g.planable[edge]
                and edge not in plans
                and g.cost[after] <= budget
                and cost - g.cost[edge] >= 65.0
            ):
                via = g.dst[edge]
                ring = after if g.flag[g.edgeway[after]] & ROUNDABOUT else None
                out = _exit_from_ring(g, after, next_edge) if ring is not None else after
                if out is not None and g.kind[g.edgeway[out]] <= LOCAL:
                    # An unimportant residential turn is suitable for a nearby
                    # facility, but shouldn't advertise towns via a shortcut.
                    local_ok = g.kind[g.edgeway[out]] <= MAJOR or not is_place
                    if local_ok:
                        turn = g.turn(edge, out, ring)
                        other = any(
                            e != after and g.turn_allowed(edge, e) and g.kind[g.edgeway[e]] <= LOCAL
                            for e in g.outgoing(via)
                        )
                        if turn and (other or ring is not None):
                            dx, dy = g.xy[via]
                            air = math.hypot(
                                (int(dx) - goal.xy[0]) * SCALE[0], (int(dy) - goal.xy[1]) * SCALE[1]
                            )
                            onward = cost - g.cost[edge]
                            if onward <= max(220.0, air * 2.6) and air >= 30.0:
                                # Prefer reachable nearby places and explicit public destinations.
                                rank = onward * preference
                                key = (turn, is_place)
                                current = choices[edge].get(key)
                                if current is None or (rank, goal.ident) < (
                                    current[0],
                                    current[1].ident,
                                ):
                                    choices[edge][key] = (rank, goal, out, ring, air, onward_metres)
            node = g.src[edge]
            if node in g.barriers:
                continue
            for before in g.incoming(node):
                step = cost + g.cost[before]
                if (
                    step > budget
                    or step >= best.get(before, math.inf)
                    or not g.turn_allowed(before, edge)
                ):
                    continue
                best[before] = step
                heapq.heappush(queue, (step, before, edge))
    return choices


_BUILD_GRAPH = None


def _goal_batch(bounds):
    graph, plans, goals = _BUILD_GRAPH
    start, end = bounds
    return end - start, _goal_choices(graph, plans, goals[start:end])


def _infer_choices(g, plans, jobs):
    """Share the read-only national graph across offline workers when available."""
    global _BUILD_GRAPH
    if jobs < 2 or len(g.goals) < 4000 or 'fork' not in multiprocessing.get_all_start_methods():
        return _goal_choices(g, plans, g.goals)
    goals = sorted(g.goals, key=lambda s: (s.xy[0] // CELL, s.xy[1] // CELL, s.ident))
    batches = [(start, min(start + 1000, len(goals))) for start in range(0, len(goals), 1000)]
    _BUILD_GRAPH = (g, plans, goals)
    choices = defaultdict(dict)
    routed = 0
    try:
        with multiprocessing.get_context('fork').Pool(jobs) as pool:
            for n, batch in pool.imap_unordered(_goal_batch, batches):
                for edge, turns in batch.items():
                    current = choices[edge]
                    for turn, candidate in turns.items():
                        if turn not in current or (candidate[0], candidate[1].ident) < (
                            current[turn][0],
                            current[turn][1].ident,
                        ):
                            current[turn] = candidate
                routed += n
                if routed % 10_000 == 0 or routed == len(goals):
                    print(f'Routed {routed:,} of {len(goals):,} destinations', flush=True)
    finally:
        _BUILD_GRAPH = None
    return choices


def infer(g, plans, refs, jobs):
    choices = _infer_choices(g, plans, jobs)
    print(
        f'Chose destinations for {len(choices):,} approaches, thinning the national layer',
        flush=True,
    )
    selected = dict(_sample_inferred(g, choices, plans))
    del choices
    city_goals = [goal for goal in g.goals if goal.kind in {"city", "town"}]
    city_tree = cKDTree(np.asarray([s.xy for s in city_goals]).reshape(-1, 2) * SCALE)

    while selected:
        edge, turns = selected.popitem()
        records = []
        at = g.xy[g.dst[edge]]
        for _, goal, out, ring, air, metres in sorted(
            turns.values(), key=lambda v: (v[0], v[1].ident)
        )[:4]:
            name = goal.name
            # A local city-centre sign is an estimate only where the goal is
            # also the closest city/town centre. Runtime checks built-up land.
            centre = False
            if goal.kind in {"city", "town"} and air < 1450 and city_goals:
                _, i = city_tree.query(at * SCALE)
                centre = city_goals[int(i)] == goal
            ref = route_ref(refs[g.refs[g.edgeway[out]]])
            panel = _panel(g, out, [name], ref, "white", "route", ring)
            if goal.kind not in PLACES:
                panel['local'] = True
            if (
                metres >= 750
                and not centre
                and goal.kind in PLACES
                and int(g.ids[g.dst[edge]]) % 5 < 2
            ):
                panel['route_m'] = round(metres)
            if centre:
                panel["centre"] = True
            records.append((out, panel, None))
        yield edge, records


def _sample_inferred(g, inferred, surveyed):
    """Spread inferred signs across real junctions and cap support density."""
    explicit_nodes = {g.dst[e] for e, records in surveyed.items() if records}
    by_node = defaultdict(list)
    for edge, records in inferred.items():
        by_node[g.dst[edge]].append((edge, records))
    grid = defaultdict(list)

    def point(node):
        x, y = g.xy[node]
        return int(x) * SCALE[0], int(y) * SCALE[1]

    def add(node):
        x, y = point(node)
        grid[math.floor(x / 72.0), math.floor(y / 72.0)].append((x, y))

    for node in explicit_nodes:
        add(node)

    def weight(item):
        node, ways = item
        return (
            min(g.kind[g.edgeway[e]] for e, _ in ways),
            -max(min(4, len(p)) for _, p in ways),
            int(g.ids[node]),
        )

    for node, ways in sorted(by_node.items(), key=weight):
        px, py = point(node)
        x, y = math.floor(px / 72.0), math.floor(py / 72.0)
        if any(
            (px - ox) ** 2 + (py - oy) ** 2 < 72.0**2
            for xx in range(x - 1, x + 2)
            for yy in range(y - 1, y + 2)
            for ox, oy in grid.get((xx, yy), ())
        ):
            continue
        add(node)
        # Prefer major approaches and supports with useful choices. Two faces
        # per junction are enough for scenic density on the inferred layer.
        for edge, records in sorted(
            ways, key=lambda r: (g.kind[g.edgeway[r[0]]], -min(4, len(r[1])), r[0])
        )[:2]:
            yield edge, records


def _write(output, g, surveyed, inferred, notes):
    cells = defaultdict(list)
    index = countrybounds.bundled()
    france = {}
    counts = Counter()
    raw_counts = Counter()
    by_source = Counter()
    roundabout_plans = 0
    distance_panels = Counter()
    for source, mapping in (
        ("mapped", surveyed.items()),
        ("inferred", inferred),
    ):
        for edge, records in mapping:
            if not records:
                continue
            node = g.dst[edge]
            x, y = map(int, g.xy[node])
            if node not in france:
                france[node] = index.at(y * 1e-7, x * 1e-7) == "FR"
            if not france[node]:
                continue
            panel_rows, keys = [], set()
            site = None
            for _, panel, candidate in sorted(records, key=lambda v: PRIORITY[v[1]["source"]]):
                key = (panel["way"], tuple(panel["names"]))
                if key in keys:
                    continue
                keys.add(key)
                raw_counts[panel["source"]] += 1
                if panel.get('distance'):
                    distance_panels['mapped'] += 1
                if panel.get('route_m'):
                    distance_panels['route'] += 1
                if site is None and candidate is not None:
                    site = candidate
                panel_rows.append(panel)
                if len(panel_rows) == 4:
                    break
            if not panel_rows:
                continue
            counts[source] += 1
            if len({tuple(panel['exit']) for panel in panel_rows if 'exit' in panel}) >= 2:
                roundabout_plans += 1
            by_source[panel_rows[0]["source"]] += 1
            cells[x // CELL, y // CELL].append(
                {
                    "id": f"{g.ids[node]}/{g.way(edge)}/{g.ids[g.src[edge]]}",
                    "at": [x, y],
                    "in": g.arrive[edge].tolist(),
                    "way": g.way(edge),
                    "panels": panel_rows,
                    **({"site": site} if site is not None else {}),
                }
            )
    meta = {
        "format": 1,
        "cell": CELL,
        "timestamp": notes["timestamp"],
        "coverage": "Metropolitan France and Corsica",
        "source": "© OpenStreetMap contributors via Geofabrik France",
        "license": "ODbL-1.0",
        "source_url": "https://download.geofabrik.de/europe/france.html",
        "plans": dict(counts),
        "supports_by_source": dict(by_source),
        "directions_by_source": dict(raw_counts),
        "roundabout_approaches": roundabout_plans,
        "distance_panels": dict(distance_panels),
        "source_counts": notes["counts"],
        "cells": [list(c) for c in sorted(cells)],
    }
    temporary = output.with_suffix(".partial")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:

        def write(name, data):
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(
                info, json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            )

        write("index.json", meta)
        for (x, y), rows in sorted(cells.items()):
            write(f"{x}_{y}.json", sorted(rows, key=lambda p: p["id"]))
    if not counts["mapped"] or not counts["inferred"]:
        raise ValueError("Empty mapped or inferred extraction")
    temporary.replace(output)
    print(
        json.dumps(
            {
                k: meta[k]
                for k in (
                    "timestamp",
                    "plans",
                    "supports_by_source",
                    "directions_by_source",
                    "roundabout_approaches",
                    "distance_panels",
                )
            },
            indent=2,
        )
    )
    print(
        f"{output.stat().st_size:,} bytes, SHA256 {hashlib.sha256(output.read_bytes()).hexdigest()}",
        flush=True,
    )


def build(network, output, jobs):
    with np.load(network / "roads.npz") as source:
        arrays = {k: source[k] for k in source.files}
    notes = json.loads((network / "notes.json").read_text(encoding="utf-8"))
    g = Graph(arrays, notes)
    del arrays
    print(
        f"{len(g.ids):,} junction/endpoint vertices, {len(g.src):,} directed links, {len(g.goals):,} destinations",
        flush=True,
    )
    plans = mapped_relations(g, notes)
    add_tagged(g, notes, plans)
    print(f"{sum(bool(records) for records in plans.values()):,} mapped approaches", flush=True)
    estimates = infer(g, plans, notes['refs'], jobs)
    _write(output, g, plans, estimates, notes)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("network", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument('--jobs', type=int, default=4, choices=range(1, 9), metavar='1..8')
    args = parser.parse_args()
    build(args.network, args.output, args.jobs)
