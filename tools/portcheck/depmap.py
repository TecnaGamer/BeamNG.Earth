#!/usr/bin/env python3
"""Inventory phase-5 callable results and their statically resolvable production dependencies."""
import argparse
import ast
from collections import defaultdict
from pathlib import Path

# Phase 5, group G of the port plan, plus its split modules and the worker dispatch.
PHASES = {
    '5a': '''roadmesh markings catenary roadcorners sidewalks islands bumps plazas shells rails
        railbridges railxing railunderpass railcrossings footbridges roundabouts roadwear
        roadmarkings frroadmesh europewalks frontage_geometry paintlist roadshape roads cdae portals railways''',
    '5b': '''furniture worldprops areaprops powerlines mappedpoles guardrails fences droprails medians
        cablebarriers delineators delineatorprofiles regionalmarkers citylamps industry golf oilwells
        windturbines ships picnic frgore kantstolpar euguardrails mappedlamps''',
    '5c': '''townentry exitsigns frlocaldirections travelboards designs motesfri signs sesigns
        deautobahn milemarkers roadsigns throughsigns dezones dirsigns speedsigns townsigns signmesh
        mergesigns radars secrossings highwaysigns townlayout routerepeaters signlayout designsigns
        frmotorwaymarkers signroads routeindex citysigns osmwelcomes routemarkers forestboards routeshields
        directionsigns signcontext signcodes signstates legacyshields recreationrules deautobahnfont signstream''',
    '5d': '''buildings landmarks bdtopo threedbag lod2roofs nycmassing nyctanks vegetation threedmr
        francehouse_mesh francehouses development streetfront''',
    '5e': '''gasstations busstationmesh busstations railstations parking busfurniture railstationmesh
        shelters busbays evchargers streetparking busyard busstationsign railstationsign busstationkeep
        busstationpark railstationprops''',
    '5f': '''blockworker earthd superlatives_data franceroads poleprofiles traffic frstreets europeinfra
        regionaltraffic roadphones regionalstops''',
}
PHASE = {mod: phase for phase, mods in PHASES.items() for mod in mods.split()}
ROOTS = ['earthd.Server.' + name for name in
         ('build_roads_geo', 'build_roads_tile', 'build_signs_tile', 'build_veg_geo', 'build_veg_tile')]
# Reviewed boundaries for job packets. Each entry names one observable return or file set.
# Other statically reachable helpers and callers remain in the inventory below.
RISKS = {
    'delineators.place': 'Country queries, spacing integral, stable side ties, OwnedBox and rounding',
    'delineators.motorway_verges': 'Travel direction and deck/rail clearance on Italian motorway verges',
    'regionalmarkers.place': 'Section hashes and ordering of regular, bend and legacy rows',
    'furniture.place': 'Station phase and clearance; ordered prop rows and wire side effects',
    'guardrails.forest_rows': 'Split-span joins and ordered rail model variants',
    'mappedpoles.tile_poles': 'Deduplication of mapped poles and order of per-road hits',
    'powerlines.tile_lines': 'Shared supports, clipped wire segments and vertex order',
    'worldprops.tile_props': 'Feature ownership and returned barn IDs alongside placement rows',
    'roadcorners.tile': 'Near-coincident endpoints, join winding and tile-edge ownership',
    'roads.drape_walks': 'In-place sidewalk heights and support sampling',
    'roadmesh.build': 'Ground callback order, vertex dtype and material/index ordering',
    'roadmesh.deck': 'Profile interpolation, parapet winding and collision vertices',
    'roadmesh.tunnel': 'Profile joins and portal cuts across neighbouring tiles',
    'railways.tile_mesh': 'Track elevations and index/material order',
    'catenary.build': 'Track station origin and shared supports on split spans',
    'roads.build_tile': 'Mutated output lists, clipping and sorted feature ownership',
    'cdae.write': 'Encoder sections, lengths and exact emitted bytes',
    'roads.write_tile': 'Atomic file set, JSON bytes, model references and CDAE bytes',
    'townentry.town_signs': 'Boundary crossings, commune precedence and mapped entry signs',
    'townsigns.place': 'Country callback, ordered posts and face mesh together',
    'deautobahn.build_tile': 'Glyph atlas, sign materials and placement coordinates',
    'dirsigns.build_tile': 'Generated face bytes and side-of-road placement',
    'roadsigns.build_tile': 'Imported signs and inferred candidates sharing an approach',
    'signstream.build_tile': 'Sign JSON and every referenced model/face file',
    'buildings.plan': 'Feature IDs, seeded choices and ordered footprint geometry',
    'buildings.tile_buildings': 'Footprints, parts and adjoining heights in feature order',
    'buildings.road_tile_items': 'Source model precedence, exclusions and custom asset writes',
    'buildings.road_tile': 'Merged mesh and entries after custom-model placement',
    'vegetation.tile_items': 'Seeded placements, masks and ordered packed item rows',
    'gasstations.tile_sites': 'Returned sites, cuts, skip IDs and mutated pad lists',
    'streetparking.tile_layout': 'Street-side orientation, stall clipping and exclusions',
    'railstations.stations_near': 'Station/platform grouping across the map-cell boundary',
    'busfurniture.rows': 'Bay orientation, shared platforms and ordered furniture rows',
    'evchargers.layout': 'Stall orientation and pad/prop correspondence',
    'streetfront.finish': 'Draped frontages and shared cuts after station/parking passes',
    'blockworker._tile_parts': 'Mutable worker stores, optional data and accumulated side products',
    'blockworker.roads_geo': 'Worker process, cold/revisit paths, anchor skin and file set',
}
CONTRACT = {
    '5a': 'Capture vertices, indices, winding and written mesh bytes',
    '5b': 'Capture ordered placements, clipping and clearance decisions',
    '5c': 'Capture glyph/texture bytes, ordered sign records and placements',
    '5d': 'Capture feature order, footprint decisions and mesh/item bytes',
    '5e': 'Capture station layout, pads, skips and collision geometry',
    '5f': 'Capture worker switches, cache state, callback answers and written files',
}


def imports(nodes):
    names = {}
    for node in nodes:
        if isinstance(node, ast.Import):
            names.update({a.asname or a.name: a.name for a in node.names})
        elif isinstance(node, ast.ImportFrom):
            names.update({a.asname or a.name: f'{node.module}.{a.name}' for a in node.names})
    return names


def inventory(server):
    nodes, scope, qual = {}, {}, {}
    for mod in sorted(PHASE):
        path = server / (mod + '.py')
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'))
        scope[mod] = imports(tree.body)
        for member in tree.body:
            cls = member.name if isinstance(member, ast.ClassDef) else ''
            for fn in member.body if cls else [member]:
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    name = '.'.join(filter(None, (mod, cls, fn.name)))
                    nodes[name], qual[name] = fn, (mod, cls)

    calls, external = defaultdict(set), defaultdict(set)
    for name, fn in nodes.items():
        mod, cls = qual[name]
        bindings = scope[mod] | imports(ast.walk(fn))
        # A nested closure's calls belong to the result that creates it. Include named
        # functions passed as arguments (worker.submit, sorted keys, cache builders).
        for expr in ast.walk(fn):
            if not isinstance(expr, (ast.Name, ast.Attribute)) or not isinstance(expr.ctx, ast.Load):
                continue
            parts = []
            base = expr
            while isinstance(base, ast.Attribute):
                parts.append(base.attr)
                base = base.value
            if not isinstance(base, ast.Name):
                continue
            target = bindings.get(base.id, f'{mod}.{base.id}')
            if cls and base.id in ('self', 'cls'):
                target = f'{mod}.{cls}'
            target = '.'.join([target, *reversed(parts)])
            if target in nodes and target != name:
                calls[name].add(target)
            elif parts and base.id in bindings:
                external[name].add(target)
    return nodes, calls, external


def walk(roots, edges):
    seen = set()
    todo = list(roots)
    while todo:
        name = todo.pop()
        if name not in seen:
            seen.add(name)
            todo.extend(edges[name] - seen)
    return seen


def report(server):
    nodes, edges, external = inventory(server)
    missing = set(ROOTS) - nodes.keys()
    if missing:
        raise ValueError(f'production entries missing: {sorted(missing)}')
    # Stop at the host/worker interface, and leave the plan/facts APIs for phase 7.
    host_dispatch = {'earthd.Server._signs_in_pool', 'earthd.Server._signs_here'}
    for name in nodes:
        if name.startswith('earthd.'):
            edges[name] = {n for n in edges[name] if not n.startswith('earthd.') or n in host_dispatch}
    for name in nodes:
        if name.startswith(('roads.road_specs', 'roads.level_ranges', 'roads.bridge_specs')):
            edges[name].clear()

    # The worker stores this concrete Towns instance on _s. Its maker returns the
    # closure passed as roads.build_tile(towns=...). Attribute spelling alone loses it.
    for name in ('blockworker.town_signs', 'earthd.Server.build_roads_tile'):
        if 'townentry.Towns.maker' in nodes:
            edges[name].add('townentry.Towns.maker')
    reached = walk(ROOTS, edges)
    parents = defaultdict(set)
    for caller in reached:
        for target in edges[caller]:
            parents[target].add(caller)
    # A crossing or a shared result gets its own witness. Fold local leaves with one
    # owner into the same replay, even if the leaf calls another local helper.
    cuts = set(ROOTS) | (set(RISKS) & reached)
    for name in reached:
        if len(parents[name]) != 1 or any(p.split('.')[0] != name.split('.')[0] for p in parents[name]):
            cuts.add(name)
    groups, deps, owner = defaultdict(set), defaultdict(set), {}
    for name in sorted(cuts):
        todo = [name]
        while todo:
            helper = todo.pop()
            if helper in groups[name]:
                continue
            groups[name].add(helper)
            owner[helper] = name
            todo.extend(edges[helper] - cuts)
    for name, members in groups.items():
        deps[name] = {owner[t] for f in members for t in edges[f]} - {name}

    ordered, seen = [], set()
    def visit(name):
        if name in seen:
            return
        seen.add(name)
        for dep in sorted(deps[name]):
            visit(dep)
        ordered.append(name)
    for name in sorted(cuts, key=lambda n: (n in ROOTS, PHASE[n.split('.')[0]], n)):
        visit(name)

    lines = ['# Phase 5 road-tile slices', '',
             'Worker dispatch starts at ' + ', '.join(ROOTS) + '. Both anchor and geotile paths are included.', '',
             '## Job packets', '',
             'Ordered by callable dependencies with the leaf pilot first. Each packet replays one named result '
             'including its observable side effects. LOC includes the result and its locally reachable helpers; '
             'the traversal stops at another packet or module. Shared helpers can appear in two estimates. '
             'Check the graph and capture a real callback and exceptional path before assigning ownership.', '',
             '| Order | Phase | Replay point | LOC | Callers | Riskiest dependency |',
             '|---:|---|---|---:|---|---|']
    packets = [n for n in ordered if n in RISKS]
    pilot = 'delineators.place'
    if pilot in packets:
        packets.remove(pilot)
        packets.insert(0, pilot)
    for i, name in enumerate(packets, 1):
        module = name.split('.')[0]
        members = {name}
        todo = [name]
        while todo:
            new = {t for t in edges[todo.pop()] if t.startswith(module + '.') and t not in RISKS} - members
            members.update(new)
            todo.extend(new)
        loc = sum(nodes[n].end_lineno - nodes[n].lineno + 1 for n in members)
        lines.append(f"| {i} | {PHASE[module]} | {name} | {loc} | {', '.join(sorted(parents[name]))} | {RISKS[name]} |")
    lines.extend(['', '## Helper inventory', '',
                  'Physical function spans include nested closures once. A shared or cross-module callable is '
                  'listed separately here to expose file ownership. Native wrappers and upstream input providers '
                  'are verification boundaries. Inventory LOC is not a deletion forecast.', '',
                  '| Order | Phase | Callable | LOC | Callers | Owned helpers | Check |',
                  '|---:|---|---|---:|---|---|---|'])
    for i, name in enumerate(ordered, 1):
        members = groups[name]
        phase = PHASE[name.split('.')[0]]
        loc = sum(nodes[n].end_lineno - nodes[n].lineno + 1 for n in members)
        # Prefer a called external codec/kernel or a concrete shared result to a generic risk.
        refs = sorted({t for n in members for t in external[n]
                       if t.startswith(('fast.', 'cdae.', 'dnp.', 'mapgeom.', 'revisit.', 'netfetch.'))})
        targets = refs or sorted(deps[name])
        risk = RISKS.get(name, CONTRACT[phase] + (f' at {targets[0]}' if targets else ' at this return'))
        join = lambda ns: ', '.join(sorted(ns)) or '—'
        lines.append(f'| {i} | {phase} | {name} | {loc} | {join(parents[name])} | '
                     f'{join(members - {name})} | {risk} |')
    lines.extend(['', '## Production call graph', '',
                  'Static function and callback references within the phase inventory. Nested closure edges are '
                  'attributed to the enclosing callable. Dynamic method dispatch, native calls, callable arguments '
                  'and out-of-phase modules need a production trace. Unresolved module coverage is listed below.', ''])
    for name in sorted(reached):
        if edges[name]:
            lines.append(f'- `{name}` ({name.split(".")[0]}.py line {nodes[name].lineno}) → ' + ', '.join(sorted(edges[name])))
    unseen = {m for m in PHASE if (server / (m + '.py')).is_file()} - {n.split('.')[0] for n in reached}
    lines.extend(['', 'Modules without a resolved entry edge: ' + ', '.join(sorted(unseen)) + '.', ''])
    return '\n'.join(lines)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server-dir', type=Path, default=Path(__file__).resolve().parents[2] / 'server')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    args.out.write_text(report(args.server_dir), encoding='utf-8')
