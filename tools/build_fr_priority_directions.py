"""Compile extra French direction signs at roundabouts and important junctions.

Uses the public road graph from extract_fr_direction_network.py --priority.
Several reverse searches label each directed exit with reachable places. Road
geometry, access and OSM turn restrictions are checked before compiling a face.
"""

import argparse
from collections import Counter, OrderedDict
from functools import lru_cache
import hashlib
import json
import multiprocessing
from pathlib import Path
import tempfile
import zipfile

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

from build_fr_local_directions import (
    CELL, MAJOR, LOCAL, SCALE, Graph, _destination, _panel, route_ref,
)
from extract_fr_direction_network import CLASSES, ROUNDABOUT, SEPARATED
import countrybounds

LOCAL_PARTITIONS = 6
CITY_PARTITIONS = 2
TOWN_PARTITIONS = 6
MAIN_ROAD = CLASSES.index("tertiary")
REGIONAL_MIN_M = 5_000
REGIONAL_LIMIT = {"town": 32_000, "city": 55_000}
PLACE_COST = {"city": 0., "town": 100., "village": 250., "suburb": 320., "hamlet": 590.}


def label_exits(graph):
    """Search legal edge-to-edge movements in reverse, from disjoint groups of places."""
    src, dst = np.asarray(graph.src), np.asarray(graph.dst)
    count = len(src)
    cost = np.asarray(graph.cost)
    out_offsets = np.asarray(graph.out_offsets)
    open_edges = ~np.isin(np.asarray(graph.edgeids), list(graph.closed_ways))
    n = np.diff(out_offsets)[dst]
    after = np.asarray(graph.out_edges)[
        np.repeat(out_offsets[dst] - (np.cumsum(n) - n), n) + np.arange(n.sum())
    ]
    before = np.repeat(np.arange(count, dtype=np.int32), n)
    allowed = open_edges[before] & open_edges[after] & (src[before] != dst[after])
    restricted = {node for node, _ in graph.rules}
    guarded = np.isin(dst, list(restricted | graph.barriers))
    for i in np.flatnonzero(allowed & guarded[before]):
        allowed[i] = graph.turn_allowed(int(before[i]), int(after[i]))
    before, after = before[allowed], after[allowed]
    del allowed, guarded, n

    goals = [g for g in graph.goals if g.kind in PLACE_COST and g.node not in graph.barriers]
    seed_rows, seed_cols, seed_cost = [], [], []
    for i, goal in enumerate(goals):
        for edge in graph.incoming(goal.node):
            if open_edges[edge]:
                seed_rows.append(count + i)
                seed_cols.append(edge)
                seed_cost.append(float(cost[edge]) + PLACE_COST[goal.kind])
    size = count + len(goals)
    matrix = csr_matrix(
        (np.r_[cost[before], np.asarray(seed_cost, np.float32)],
         (np.r_[after, np.asarray(seed_rows, np.int32)], np.r_[before, np.asarray(seed_cols, np.int32)])),
        shape=(size, size),
    )
    del before, after, seed_rows, seed_cols, seed_cost
    groups = (
        (LOCAL_PARTITIONS, 15_000., {"village", "hamlet", "suburb"}),
        (CITY_PARTITIONS, 65_000., {"city"}),
        (TOWN_PARTITIONS, 42_000., {"town"}),
    )
    costs, next_edges, sources, regional = [], [], [], []
    for partitions, limit, kinds in groups:
        for partition in range(partitions):
            starts = [count + i for i, goal in enumerate(goals)
                      if goal.kind in kinds and goal.ident % partitions == partition]
            if not starts:
                continue
            distance, previous, source = dijkstra(
                matrix, directed=True, indices=starts, min_only=True,
                return_predecessors=True, limit=limit,
            )
            costs.append(distance[:count].astype(np.float32))
            next_edges.append(previous[:count])
            sources.append(source[:count] - count)
            regional.append(kinds <= REGIONAL_LIMIT.keys())
            print(f"Routed {'regional' if regional[-1] else 'local'} group {partition + 1}/{partitions}",
                  flush=True)
    return goals, (np.stack(costs), next_edges, np.stack(sources), np.array(regional))


def roundabout_exits(graph, inbound, entry, rings):
    """Follow one circulating lane, without crossing an unverified junction or taking a U-turn."""
    visited = set()
    edge = entry
    origin = graph.dst[inbound]
    exits = {}
    ring_cost = ring_m = 0.
    for _ in range(40):
        if edge in visited:
            break
        visited.add(edge)
        node = graph.dst[edge]
        ring_cost += graph.cost[edge]
        ring_m += graph.length[edge]
        options = [e for e in graph.outgoing(node) if graph.turn_allowed(edge, e)]
        if node != origin:
            for outlet in options:
                if not rings[outlet]:
                    exits.setdefault(outlet, (ring_cost, ring_m))
        onward = [e for e in options if rings[e]]
        if len(onward) != 1:
            break
        if onward[0] == entry:
            return exits
        edge = onward[0]
    return {}


def compile_sites(graph, notes, goals, searches, shard=0, shards=1):
    way = np.asarray(graph.edgeway)
    kind = np.asarray(graph.kind)[way]
    flags = np.asarray(graph.flag)[way]
    rings = (flags & ROUNDABOUT) != 0
    ring_nodes = set(np.asarray(graph.src)[rings]) | set(np.asarray(graph.dst)[rings])
    controls = {}
    for label, x, y in notes.get("junction_points", ()):
        dist, index = graph.junctions.query(np.array([x, y]) * SCALE)
        if dist <= (12. if label == "mini_roundabout" else 16.):
            node = int(graph.junction_nodes[index])
            if label == "mini_roundabout" or node not in controls:
                controls[node] = label

    costs, next_edges, sources, regional = searches
    edge_count = len(graph.src)

    @lru_cache(maxsize=65_536)
    def measure(edge, junction, group):
        # Report physical road length, including curved portions, and reject
        # paths that come back through the very junction they start from.
        previous = next_edges[group]
        metres = 0.
        for _ in range(1536):
            if graph.dst[edge] == junction:
                break
            metres += graph.length[edge]
            edge = previous[edge]
            if edge >= edge_count:
                return metres
            if edge < 0:
                break
        return None

    tags = notes["tags"]
    refs = notes["refs"]
    places = [g for g in goals if g.kind in ("city", "town", "village", "hamlet")]
    from scipy.spatial import cKDTree

    tree = cKDTree(np.asarray([goal.xy for goal in places]).reshape(-1, 2) * SCALE)
    index = countrybounds.bundled()
    nodes = sorted(set(map(int, np.flatnonzero(graph.candidate))) | ring_nodes | set(controls))
    nodes = nodes[shard * len(nodes) // shards:(shard + 1) * len(nodes) // shards]
    for n, junction in enumerate(nodes, 1):
        if n % 100_000 == 0:
            print(f"Group {shard + 1}/{shards}: checked {n:,}/{len(nodes):,} junctions", flush=True)
        at = graph.xy[junction]
        lat, lon = at[1] * 1e-7, at[0] * 1e-7
        inbound = [e for e in graph.incoming(junction) if not rings[e] and 2 <= kind[e] <= LOCAL]
        outgoing = list(graph.outgoing(junction))
        if not inbound or not outgoing:
            continue
        on_ring = junction in ring_nodes
        mini = controls.get(junction) == "mini_roundabout"
        if not on_ring:
            neighbours = {graph.src[e] for e in graph.incoming(junction)} | {graph.dst[e] for e in outgoing}
            if len(neighbours) < 3 or len(neighbours) > 6:
                continue
            if not (mini or junction in controls or any(2 <= kind[e] <= MAIN_ROAD for e in outgoing)):
                continue
        if index.at(lat, lon) != "FR":
            continue
        distance, i = tree.query(at * SCALE)
        same_place = places[i] if distance <= 1250. else None
        for approach in inbound:
            if flags[approach] & SEPARATED or (kind[approach] > MAJOR and not (on_ring or mini or junction in controls)):
                continue
            legal = {e: (0., 0.) for e in outgoing if graph.turn_allowed(approach, e)}
            circulating = [e for e in legal if rings[e]]
            entry = None
            if on_ring:
                if len(circulating) != 1:
                    continue
                entry = circulating[0]
                legal = roundabout_exits(graph, approach, entry, rings)
            if len(legal) < 2:
                continue
            edges = list(legal)
            # Each partition gives a lower bound on the cost of reaching
            # any specific place in it. If another turn's bound is notably
            # better, there isn't enough evidence to put this name here.
            scores = costs[:, edges] + np.asarray([c for c, _ in legal.values()], dtype=np.float32)
            best = scores.min(axis=1)
            viable = np.isfinite(scores) & (scores <= best[:, None] * 1.06 + 120.)
            panels = []
            estimated = {}
            signed = set()
            for column, edge in enumerate(edges):
                turn = graph.turn(approach, edge, entry)
                if turn is None or not 2 <= kind[edge] <= LOCAL or flags[edge] & SEPARATED:
                    continue
                ref = route_ref(refs[graph.refs[way[edge]]])
                tagged = _destination(tags.get(str(graph.way(edge)), {}), graph.forward[edge], graph.dir[way[edge]] != 0)
                if tagged:
                    names, signed_ref, paint, distance = tagged
                    panel = _panel(graph, edge, names, signed_ref or ref, paint, "road", entry)
                    if distance:
                        panel["distance"] = distance
                    panels.append(panel)
                    signed.update(name.casefold() for name in names)

                for group in np.flatnonzero(viable[:, column]):
                    goal = goals[sources[group, edge]]
                    air = float(np.linalg.norm((np.asarray(goal.xy) - at) * SCALE))
                    score = float(scores[group, column])
                    if air < 120. or score > max(480., air * 2.25 + PLACE_COST[goal.kind]):
                        continue
                    own = same_place is not None and goal.name.casefold() == same_place.name.casefold()
                    if own and goal.kind not in REGIONAL_LIMIT:
                        continue
                    distant = regional[group] and air >= REGIONAL_MIN_M * .75
                    if distant and (kind[edge] > MAIN_ROAD or kind[approach] > MAJOR):
                        continue
                    if not distant and score > (1500. if kind[edge] > MAJOR else 15_000.):
                        continue
                    length = measure(edge, junction, group)
                    if length is None:
                        continue
                    length = round(length + legal[edge][1])
                    if distant and not REGIONAL_MIN_M <= length <= REGIONAL_LIMIT[goal.kind]:
                        continue
                    name = goal.name.casefold()
                    rank = (score, length, edge)
                    if name not in estimated or rank < estimated[name][0]:
                        estimated[name] = (rank, goal, ref, own, distant)

            # Keep one local name and, on useful through routes, one more
            # distant town or city. Explicitly mapped labels take precedence.
            branches = {}
            for name, (rank, goal, ref, own, distant) in sorted(estimated.items(), key=lambda row: row[1][0]):
                if name not in signed:
                    branches.setdefault(rank[2], {}).setdefault(distant, (rank, goal, ref, own))
            for edge, choices in branches.items():
                ranked = [choices[key] for key in (True, False) if key in choices]
                # Centre-ville is a separate local face; don't convert a
                # regional name on the same branch to a centre label.
                if len(ranked) == 2 and ranked[1][3]:
                    ranked.pop()
                names = [goal.name for _, goal, _, _ in ranked]
                panel = _panel(graph, edge, names, ranked[0][2], "white", "route", entry)
                panel["checked_route"] = True
                if ranked[0][3]:
                    panel["centre"] = True
                elif int(graph.ids[junction]) % 5 < 2:
                    panel["route_distances"] = [rank[1] if rank[1] >= 900 else 0 for rank, *_ in ranked]
                panels.append(panel)
            if panels:
                yield {
                    "id": f"priority/{int(graph.ids[junction])}/{int(graph.way(approach))}/{int(graph.ids[graph.src[approach]])}",
                    "at": at.tolist(), "in": graph.arrive[approach].tolist(), "way": int(graph.way(approach)),
                    "junction": "roundabout" if on_ring else "mini" if mini else "intersection",
                    "panels": panels[:4],
                }


def _spool(shard, shards, directory):
    """Keep the build bounded while streaming rows for each geographic cell."""
    graph, notes, goals, searches = _BUILD
    opened = OrderedDict()
    counts = Counter()
    cells = set()
    try:
        for plan in compile_sites(graph, notes, goals, searches, shard, shards):
            x, y = plan["at"]
            cell = x // CELL, y // CELL
            file = opened.get(cell)
            if file is None:
                file = (directory / f"{shard}_{cell[0]}_{cell[1]}.jsonl").open("a", encoding="utf-8")
                opened[cell] = file
            opened.move_to_end(cell)
            if len(opened) > 64:
                opened.popitem(last=False)[1].close()
            file.write(json.dumps(plan, ensure_ascii=False, separators=(",", ":")) + "\n")
            cells.add(cell)
            counts[plan["junction"]] += 1
    finally:
        for file in opened.values():
            file.close()
    return cells, counts


def build(network, output, jobs=1):
    global _BUILD
    with np.load(network / "roads.npz") as raw:
        arrays = {name: raw[name] for name in raw.files}
    notes = json.loads((network / "notes.json").read_text(encoding="utf-8"))
    graph = Graph(arrays, notes, include_highways=True)
    del arrays
    goals, searches = label_exits(graph)
    _BUILD = graph, notes, goals, searches
    if "fork" not in multiprocessing.get_all_start_methods():
        jobs = 1
    with tempfile.TemporaryDirectory(dir=output.parent) as scratch:
        directory = Path(scratch)
        if jobs == 1:
            stats = [_spool(0, 1, directory)]
        else:
            with multiprocessing.get_context("fork").Pool(jobs) as pool:
                stats = pool.starmap(_spool, [(shard, jobs, directory) for shard in range(jobs)])
        counts = Counter()
        cells = set()
        for shard_cells, shard_counts in stats:
            cells.update(shard_cells)
            counts.update(shard_counts)
        metadata = {
            "format": 1, "routing": 6, "cell": CELL, "cells": sorted(cells), "counts": counts,
            "source": "© OpenStreetMap contributors via Geofabrik, ODbL 1.0",
            "timestamp": notes["timestamp"],
        }
        temp = output.with_suffix(".partial")
        with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.writestr("index.json", json.dumps(metadata, separators=(",", ":")))
            for x, y in sorted(cells):
                rows = []
                for shard in range(jobs):
                    path = directory / f"{shard}_{x}_{y}.jsonl"
                    if path.exists():
                        rows.extend(path.read_text(encoding="utf-8").splitlines())
                archive.writestr(f"{x}_{y}.json", "[" + ",".join(rows) + "]")
        temp.replace(output)
    del _BUILD
    with output.open("rb") as f:
        print(dict(counts), f"({output.stat().st_size:,} bytes)", hashlib.file_digest(f, "sha256").hexdigest())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("network", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--jobs", type=int, default=1, choices=range(1, 9))
    args = parser.parse_args()
    build(args.network, args.output, args.jobs)
