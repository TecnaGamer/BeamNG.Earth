"""Connected OSM route reassurance stations from the 1.7.0 customization.

Built with the weekly shards; spacing never restarts at a map tile boundary.
"""
from collections import defaultdict
import hashlib
import math
import re
import numpy as np
import exitsigns
import signstates
import throughsigns

SHIELD_SPACING = 3 * 1609.344
ROAD_CLASSES = ('motorway', 'trunk', 'primary', 'secondary', 'tertiary')
WAY_KEYS = ('highway', 'ref', 'name', 'oneway', 'junction', 'bridge', 'tunnel',
            'access', 'direction', 'destination:direction',
            'destination:direction:forward', 'destination:direction:backward')

def uid(key):
    return -(1 << 60)-int.from_bytes(hashlib.blake2b(str(key).encode(), digest_size=7).digest(), 'little')

def record(key, point, extra):
    return dict(id=uid(key), lon=float(point[0]), lat=float(point[1]),
                tags={}, ways=[extra], kind='roadside')

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

def index_records(ways, coords, relations=None):
    relations = relations or {}
    edges, outgoing, incoming = [], defaultdict(list), defaultdict(list)
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
    seen, out = set(), []
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

        stations = [(float(s), 'shield', {}, round(float(s), 3))
                    for s in np.arange(1000., arc[-1]-10., SHIELD_SPACING)]
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
            if kind == 'shield':
                if not routes:
                    continue
                content = dict(routes=[dict(kind='interstate' if r[0] == 'I' else 'us', number=r[1],
                                      direction=posted_direction(tags, relations.get(wid, ()), direction, r, axis)) for r in routes[:3]])
            start, end = max(0., s-60), min(float(arc[-1]), s+60)
            local = np.vstack([at(start)[0], path[(arc > start) & (arc < end)], at(end)[0]])
            extra = dict(type=kind, point=point.tolist(), approach=local.tolist(), main_id=wid,
                         axis=axis.tolist(), state=str(state), **content)
            out.append(record((kind, refs[0], refs[-1], source_id, edges[first][3]), point, extra))
    return sorted({o['id']: o for o in out}.values(), key=lambda o: o['id'])
