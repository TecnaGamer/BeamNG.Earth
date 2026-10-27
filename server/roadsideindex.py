"""OSM roadside inventory and corridor-distance sign stations, built off the road thread.

Stations are measured on connected source ways, not clipped map tiles. Posted
node signs take precedence over repeaters. US styling is restricted by the
bundled state polygons; lamps retain their mapped positions worldwide.
"""
from collections import defaultdict
import hashlib
import math
import re

import numpy as np

import exitsigns
import signstates
import throughsigns

MILE = 1609.344
SPEED_SPACING = 2*MILE
SHIELD_SPACING = 3*MILE
ADOPT_SPACING = 2*MILE
ROAD_CLASSES = ('motorway', 'trunk', 'primary', 'secondary', 'tertiary')
SPEED_KEYS = tuple(k+s for k in ('maxspeed', 'maxspeed:hgv')
                   for s in ('', ':forward', ':backward'))
WAY_KEYS = ('highway', 'ref', 'name', 'oneway', 'junction', 'bridge', 'tunnel',
            'access', 'direction', 'destination:direction') + SPEED_KEYS
NODE_KEYS = ('highway', 'traffic_sign', 'direction', 'traffic_sign:direction',
             'lamp_mount', 'lamp_type', 'support', 'height', 'light:height',
             'light:count', 'light:direction', 'man_made', 'tower:type',
             'light:colour', 'light:method', 'light:lit') + SPEED_KEYS


def uid(key):
    return -(1 << 60)-int.from_bytes(hashlib.blake2b(str(key).encode(), digest_size=7).digest(), 'little')


def record(key, point, extra):
    return dict(id=uid(key), lon=float(point[0]), lat=float(point[1]),
                tags={}, ways=[extra], kind='roadside')


def speed(value):
    """OSM's unqualified speeds are km/h. Reject variable/conditional/unknown text."""
    match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*(mph|km/h|kmh|kph)?\s*', str(value), re.I)
    if not match:
        return None
    mph = float(match[1])/(1. if (match[2] or '').lower() == 'mph' else 1.609344)
    return int(math.floor(mph+.5)) if 5 <= mph <= 100 else None


def limits(tags, direction):
    suffix = ':forward' if direction > 0 else ':backward'
    return tuple(speed(tags.get(k+suffix, tags.get(k, ''))) for k in ('maxspeed', 'maxspeed:hgv'))


def lamp(tags):
    return (tags.get('highway') == 'street_lamp' or
            (tags.get('man_made') in ('mast', 'tower') and tags.get('tower:type') == 'lighting'))


def speed_node(tags):
    return (any(k in tags for k in SPEED_KEYS) or
            bool(re.search(r'(?:US:)?R2-1\[|maxspeed\[', tags.get('traffic_sign', ''), re.I)))


def ground(tags):
    return (tags.get('bridge', 'no') in ('no', 'false', '0', '') and
            tags.get('tunnel', 'no') in ('no', 'false', '0', '') and
            tags.get('access') not in ('private', 'no') and tags.get('junction') != 'roundabout')


def route_refs(tags, relations):
    refs = []
    for raw in tags.get('ref', '').split(';'):
        match = re.fullmatch(r'\s*(I|US)[ -]?(\d{1,3})\s*', raw, re.I)
        if match:
            refs.append((match[1].upper(), match[2]))
    for r in relations:
        if r.get('network') in ('US:I', 'US:US') and re.fullmatch(r'\d{1,3}', r.get('ref', '')):
            refs.append(('I' if r['network'] == 'US:I' else 'US', r['ref']))
    return sorted(set(refs))


def posted_direction(tags, relations, direction, route, axis):
    suffix = ':forward' if direction > 0 else ':backward'
    value = throughsigns.cardinal(tags.get('destination:direction'+suffix,
                                tags.get('destination:direction', tags.get('direction'))))
    choices = {throughsigns.cardinal(r.get('direction')) for r in relations
               if r.get('role') not in ('forward', 'backward') or
               r['role'] == ('forward' if direction > 0 else 'backward')}
    choices.discard('')
    if value or len(choices) == 1:
        return value or next(iter(choices))
    # No posted direction: use the route's numbered axis, then local travel.
    # Directional relations take priority on roads like Michigan I-69 (E/W).
    ns = int(route[1]) % 2 if route else abs(axis[1]) >= abs(axis[0])
    return ('NORTH' if axis[1] >= 0 else 'SOUTH') if ns else ('EAST' if axis[0] >= 0 else 'WEST')


def index_records(ways, coords, nodes=(), relations=None):
    relations = relations or {}
    lamps, mapped = [], {}
    for nid, lon, lat, tags in nodes:
        if lamp(tags):
            # Wall/ceiling/suspended fixtures have no freestanding pole.
            if tags.get('support', '') not in ('wall', 'wall_mounted', 'ceiling', 'wire', 'ground') and tags.get('lamp_mount') not in ('wall', 'suspended'):
                lamps.append(record(('lamp', nid), (lon, lat), dict(type='lamp', tags=tags)))
        elif speed_node(tags):
            tags = dict(tags)
            if not tags.get('maxspeed'):
                m = re.search(r'(?:US:)?R2-1\[(\d+)\]|maxspeed\[(\d+)\]', tags.get('traffic_sign', ''), re.I)
                if m:
                    tags['maxspeed'] = (m[1]+' mph') if m[1] else m[2]
            mapped[nid] = (np.array([lon, lat]), tags)

    edges, outgoing, incoming = [], defaultdict(list), defaultdict(list)
    member = defaultdict(list)
    grid = defaultdict(set)
    for wid, refs, tags in sorted(ways, key=lambda r: r[0]):
        if tags.get('access') in ('private', 'no') or tags.get('junction') == 'roundabout':
            continue
        # Missing coordinates split a way; never draw across a missing extract.
        if len(refs) < 2 or any(n not in coords for n in refs):
            continue
        routes = route_refs(tags, relations.get(wid, ()))
        highway = tags.get('highway')
        if highway not in ROAD_CLASSES:
            continue
        one = exitsigns._direction(tags)
        if not one and highway == 'motorway' and tags.get('oneway') not in ('no', '0', 'false'):
            one = 1
        for direction in ((one,) if one else (1, -1)):
            rr = refs if direction > 0 else refs[::-1]
            i = len(edges)
            # Bridge and speed tags intentionally do not reset repeater spacing.
            signature = (highway, tuple(routes), '' if routes else tags.get('ref', '') or tags.get('name', ''))
            edges.append((wid, rr, tags, direction, routes, signature))
            outgoing[rr[0]].append(i)
            incoming[rr[-1]].append(i)
            for n in rr:
                if n in mapped:
                    member[n].append(i)
        if mapped:
            p = np.asarray([coords[n] for n in refs])
            # Grid only for detached sign nodes. 0.01 degrees is a bounded local search.
            for a, b in zip(p[:-1], p[1:]):
                low = np.floor(np.minimum(a, b)*100).astype(int)
                high = np.floor(np.maximum(a, b)*100).astype(int)
                for x in range(low[0], high[0]+1):
                    for y in range(low[1], high[1]+1):
                        grid[x, y].add(wid)

    by_way = defaultdict(list)
    for i, e in enumerate(edges):
        by_way[e[0]].append(i)
    # Attach roadside nodes to the nearest source road, never both divided roads.
    for nid, (p, tags) in mapped.items():
        if member[nid]:
            continue
        cell = np.floor(p*100).astype(int)
        candidates = set().union(*(grid.get((cell[0]+x, cell[1]+y), ()) for x in (-1, 0, 1) for y in (-1, 0, 1)))
        scale = np.array([111320.*math.cos(math.radians(p[1])), 111320.])
        best = None
        for wid in sorted(candidates):
            path = np.asarray([coords[n] for n in edges[by_way[wid][0]][1]])
            xy = (path-p)*scale
            d = np.diff(xy, axis=0)
            t = np.clip((-xy[:-1]*d).sum(1)/np.maximum((d*d).sum(1), 1e-9), 0, 1)
            distance = float(np.linalg.norm(xy[:-1]+t[:, None]*d, axis=1).min())
            if distance <= 45 and (best is None or distance < best[0]):
                best = (distance, wid)
        if best:
            member[nid] = by_way[best[1]]
    edge_nodes = defaultdict(list)
    for nid, indices in member.items():
        for i in indices:
            edge_nodes[i].append(nid)

    def next_edge(i):
        wid, rr, _, _, _, sig = edges[i]
        choices = [j for j in outgoing[rr[-1]] if edges[j][0] != wid and edges[j][5] == sig]
        if len(choices) != 1:
            return None
        j = choices[0]
        previous = [k for k in incoming[rr[-1]] if edges[k][0] != edges[j][0] and edges[k][5] == sig]
        return j if len(previous) == 1 else None

    following = [next_edge(i) for i in range(len(edges))]
    continued = {j for j in following if j is not None}
    starts = [i for i in range(len(edges)) if i not in continued]
    seen, out = set(), list(lamps)
    for first in starts + list(range(len(edges))):
        if first in seen:
            continue
        chain, i = [], first
        while i is not None and i not in seen:
            seen.add(i)
            chain.append(i)
            i = following[i]
        refs, owners = [], []
        for i in chain:
            rr = edges[i][1]
            refs.extend(rr if not refs else rr[1:])
            owners.extend([i]*(len(rr)-1))
        path = np.asarray([coords[n] for n in refs])
        scale = np.array([111320.*math.cos(math.radians(float(path[:, 1].mean()))), 111320.])
        xy = (path-path[0])*scale
        arc = np.r_[0., np.cumsum(np.linalg.norm(np.diff(xy, axis=0), axis=1))]
        if arc[-1] < 5:
            continue

        def at(s):
            k = min(len(owners)-1, max(0, int(np.searchsorted(arc, s, side='right')-1)))
            t = np.clip((s-arc[k])/max(arc[k+1]-arc[k], 1e-9), 0, 1)
            return path[k]*(1-t)+path[k+1]*t, k

        stations = []
        node_stations = []
        for nid in sorted({n for i in chain for n in edge_nodes[i]}):
            point, tags = mapped[nid]
            d = np.diff(xy, axis=0)
            q = (point-path[0])*scale
            t = np.clip(((q-xy[:-1])*d).sum(1)/np.maximum((d*d).sum(1), 1e-9), 0, 1)
            k = int(np.linalg.norm(xy[:-1]+t[:, None]*d-q, axis=1).argmin())
            e = edges[owners[k]]
            if owners[k] not in member[nid]:
                continue
            facing = tags.get('traffic_sign:direction', tags.get('direction', ''))
            if facing in ('forward', 'backward') and (facing == 'forward') != (e[3] > 0):
                continue
            # A numeric direction denotes the face normal toward approaching drivers.
            try:
                bearing = float(facing)
            except ValueError:
                bearing = None
            if bearing is not None:
                normal = np.array([math.sin(math.radians(bearing)), math.cos(math.radians(bearing))])
                if normal@d[k] >= 0:
                    continue
            value, truck = limits(tags, e[3])
            if value is None:
                continue
            if truck is None:
                truck = limits(e[2], e[3])[1]
            s = float(arc[k]+t[k]*(arc[k+1]-arc[k]))
            node_stations.append(s)
            stations.append((s, 'speed', dict(speed=value, truck=truck, source='osm:node'), nid))
        # At a changed posted way limit, show the new value near the boundary.
        for k in range(1, len(owners)):
            if owners[k] == owners[k-1]:
                continue
            prev, cur = edges[owners[k-1]], edges[owners[k]]
            before, after = limits(prev[2], prev[3]), limits(cur[2], cur[3])
            s = min(float(arc[k]+30), float(arc[-1]-1))
            if after[0] is not None and after != before and all(abs(s-v) > 150 for v in node_stations):
                stations.append((s, 'speed', dict(speed=after[0], truck=after[1], source='osm:change'), cur[0]))
                node_stations.append(s)
        for kind, spacing, phase in (('speed', SPEED_SPACING, 400.), ('shield', SHIELD_SPACING, 1000.), ('adopt', ADOPT_SPACING, 1800.)):
            for s in np.arange(phase, arc[-1]-10., spacing):
                if kind == 'speed' and any(abs(s-v) < 300 for v in node_stations):
                    continue
                stations.append((float(s), kind, {}, round(float(s), 3)))
        if not stations:
            continue
        points = np.array([at(s)[0] for s, *_ in stations])
        states = signstates.states(points[:, 1], points[:, 0])
        for (s, kind, content, source_id), point, state in zip(stations, points, states):
            if not state:
                continue
            _, k = at(s)
            wid, _, tags, direction, routes, _ = edges[owners[k]]
            if not ground(tags):
                continue
            axis = (path[k+1]-path[k])*scale
            axis /= max(np.linalg.norm(axis), 1e-9)
            if kind == 'speed' and not content:
                value, truck = limits(tags, direction)
                suffix = ':forward' if direction > 0 else ':backward'
                if value is None and tags.get('maxspeed'+suffix, tags.get('maxspeed')) not in (None, ''):
                    continue  # an explicit variable/unknown limit must not become a guessed number
                if tags['highway'] not in ('motorway', 'trunk') and not routes:
                    continue
                # Match the game's existing rural traffic defaults, not a legal speed inference.
                from navgraph import DRIVE
                content = dict(speed=value or DRIVE[tags['highway']][1], truck=truck,
                               source='osm:way' if value else 'class-default')
            if kind == 'shield':
                if not routes:
                    continue
                content = dict(routes=[dict(kind='interstate' if r[0] == 'I' else 'us', number=r[1],
                                      direction=posted_direction(tags, relations.get(wid, ()), direction, r, axis)) for r in routes[:3]])
            if kind == 'adopt':
                state_route = re.search(r'(?:^|;)\s*(?:M|MI)[ -]?\d+\s*(?:;|$)', tags.get('ref', ''), re.I)
                if state != 'MI' or not (routes or state_route or tags['highway'] in ('motorway', 'trunk')):
                    continue
            start, end = max(0., s-60), min(float(arc[-1]), s+60)
            local = np.vstack([at(start)[0], path[(arc > start) & (arc < end)], at(end)[0]])
            extra = dict(type=kind, point=point.tolist(), approach=local.tolist(), main_id=wid,
                         axis=axis.tolist(), state=str(state), **content)
            out.append(record((kind, refs[0], refs[-1], source_id, edges[first][3]), point, extra))
    return sorted({o['id']: o for o in out}.values(), key=lambda o: o['id'])
