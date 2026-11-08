"""Offline, conservative OSM exit/service routing. No road searches in gameplay.

Only explicit network access qualifies: a business node on a drivable way, or
a drivable vertex inside its mapped area. We never join a POI to the nearest
road across an unknown fence. Restrictions are handled conservatively by
blocking the via node (including via-way nodes) rather than guessing a turn.
"""
from collections import defaultdict
import heapq
import json
import math
from functools import lru_cache
from pathlib import Path

import exitsigns
import signstates

POLICY = Path(__file__).with_name('data') / 'service_sign_policy.json'
CATEGORIES = {'restaurant': 'FOOD', 'fast_food': 'FOOD', 'cafe': 'FOOD',
              'fuel': 'GAS', 'hotel': 'LODGING', 'motel': 'LODGING',
              'guest_house': 'LODGING', 'camp_site': 'CAMPING',
              'caravan_site': 'CAMPING', 'hospital': 'HOSPITAL'}
DRIVABLE = {'motorway', 'motorway_link', 'trunk', 'trunk_link', 'primary',
             'primary_link', 'secondary', 'secondary_link', 'tertiary',
             'tertiary_link', 'unclassified', 'residential', 'service', 'living_street'}


def policy(state, path=POLICY):
    path = Path(path)
    data = _policy_data(str(path), path.stat().st_mtime_ns)
    result = dict(data['default'])
    result.update(data.get('states', {}).get(state, {}))
    return result


@lru_cache(maxsize=8)
def _policy_data(path, stamp):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def allowed(tags):
    access = next((tags[k] for k in ('motorcar', 'motor_vehicle', 'vehicle', 'access') if k in tags), 'yes')
    return (access in ('yes', 'permissive', 'designated', 'destination', 'customers')
            and not any(':conditional' in k for k in tags)
            and tags.get('service') != 'emergency_access'
            and tags.get('area') != 'yes')


def controlled(tags):
    return tags.get('highway') == 'motorway' or (tags.get('highway') == 'trunk' and
           (tags.get('motorroad') == 'yes' or tags.get('expressway') == 'yes'))


def metres(a, b):
    return math.hypot((a[0]-b[0])*111320*math.cos(math.radians((a[1]+b[1])/2)),
                      (a[1]-b[1])*111320)


def inside(point, ring):
    if point in ring: return True
    x, y = point
    result = False
    for (ax, ay), (bx, by) in zip(ring, ring[1:]+ring[:1]):
        if (ay > y) != (by > y) and x < ax+(y-ay)*(bx-ax)/(by-ay):
            result = not result
    return result


def area_rings(element, coords, role='outer'):
    """Closed ways and stitched multipolygon outer members; holes omitted conservatively."""
    if element['type'] == 'way':
        if role == 'inner': return []
        refs = element.get('nodes', [])
        if refs and all(n in coords for n in refs):
            pieces = [[coords[n] for n in refs]]
        else:
            pieces = [[[p['lon'], p['lat']] for p in element.get('geometry', []) if p]]
    elif element['type'] == 'relation':
        pieces = [[[p['lon'],p['lat']] for p in m.get('geometry', []) if p]
                  for m in element.get('members', []) if (m.get('role') == role or (role == 'outer' and m.get('role') == '')) and m['type'] == 'way']
    else:
        return []
    pending, rings = [p for p in pieces if len(p) > 1], []
    while pending:
        line = pending.pop(0)
        while line[-1] != line[0]:
            match = next(((i, p if p[0] == line[-1] else p[::-1]) for i,p in enumerate(pending)
                          if p[0] == line[-1] or p[-1] == line[-1]), None)
            if match is None: break
            i, piece = match
            pending.pop(i)
            line.extend(piece[1:])
        if len(line) >= 4 and line[0] == line[-1]: rings.append(line)
    return rings


def build(data, policy_path=POLICY):
    elements = data['elements']
    coords, nodes, ways = {}, {}, {}
    for e in elements:
        if e['type'] == 'node':
            coords[e['id']] = [e['lon'], e['lat']]
            nodes[e['id']] = e.get('tags', {})
        elif e['type'] == 'way':
            ways[e['id']] = (e.get('nodes', []), e.get('tags', {}))
            for nid, p in zip(e.get('nodes', []), e.get('geometry', [])):
                if p:
                    coords[nid] = [p['lon'], p['lat']]
    blocked = {n for n, t in nodes.items() if t.get('barrier') and
               (t.get('barrier') not in ('gate', 'lift_gate', 'cattle_grid') or not allowed(t)
                or not any(t.get(k) in ('yes', 'permissive', 'customers')
                           for k in ('motorcar', 'motor_vehicle', 'vehicle', 'access')))}
    restricted = []
    barriers = []
    for e in elements:
        tags = e.get('tags', {})
        geometry = e.get('geometry', [])
        if tags.get('barrier') and e['type'] == 'way' and geometry:
            barriers.append([[p['lon'], p['lat']] for p in geometry if p])
        if tags.get('landuse') == 'military' or tags.get('military') in ('base', 'barracks', 'airfield'):
            rings = area_rings(e, coords)
            if not rings and e['type'] == 'relation':
                # Incomplete mapped base boundaries fail closed. Their enclosing
                # box can omit public POIs, but must never advertise base access.
                points = [p for m in e.get('members', []) if m.get('role') == 'outer'
                          for p in m.get('geometry', []) if p]
                if points:
                    x0,x1 = min(p['lon'] for p in points),max(p['lon'] for p in points)
                    y0,y1 = min(p['lat'] for p in points),max(p['lat'] for p in points)
                    rings = [[[x0,y0],[x1,y0],[x1,y1],[x0,y1],[x0,y0]]]
            restricted.extend(rings)
    for ring in restricted:
        lo = [min(p[i] for p in ring) for i in (0,1)]
        hi = [max(p[i] for p in ring) for i in (0,1)]
        blocked.update(n for n,p in coords.items() if all(lo[i] <= p[i] <= hi[i] for i in (0,1)) and inside(p,ring))
    for e in elements:
        if e['type'] == 'relation' and e.get('tags', {}).get('type') == 'restriction':
            for m in e.get('members', []):
                if m.get('role') == 'via':
                    blocked.update([m['ref']] if m['type'] == 'node' else ways.get(m['ref'], ([], {}))[0])
    graph, road_nodes = defaultdict(list), set()
    for wid, (refs, tags) in sorted(ways.items()):
        cls = tags.get('highway')
        if cls not in DRIVABLE or not allowed(tags):
            continue
        direction = exitsigns._direction(tags)
        if tags.get('junction') == 'roundabout' and 'oneway' not in tags:
            direction = 1
        for a, b in zip(refs, refs[1:]):
            if a not in coords or b not in coords or a in blocked or b in blocked:
                continue
            # Local access must never re-enter a motorway to reach another exit.
            if controlled(tags):
                continue
            if not cls.endswith('_link') and tags.get('bridge') not in ('yes', 'viaduct') and tags.get('tunnel') != 'yes' and tags.get('layer', '0') == '0':
                road_nodes.update((a, b))
            distance = metres(coords[a], coords[b])
            if direction >= 0: graph[a].append((b, distance, wid))
            if direction <= 0: graph[b].append((a, distance, wid))
    businesses = []
    by_node = defaultdict(list)
    node_grid = defaultdict(list)
    for n in sorted(road_nodes-blocked):
        node_grid[tuple(math.floor(v*200) for v in coords[n])].append(n)
    for e in sorted(elements, key=lambda e: (e['type'], e['id'])):
        tags = e.get('tags', {})
        category = CATEGORIES.get(tags.get('amenity')) or CATEGORIES.get(tags.get('tourism'))
        if not category or not allowed(tags) or tags.get('disused') == 'yes':
            continue
        access = []
        if e['type'] == 'node' and e['id'] in road_nodes and e['id'] not in blocked:
            access = [e['id']]
        elif e['type'] in ('way', 'relation'):
            refs = e.get('nodes', [])
            holes = area_rings(e, coords, 'inner')
            for ring in area_rings(e, coords):
                lo = [min(p[i] for p in ring) for i in (0, 1)]
                hi = [max(p[i] for p in ring) for i in (0, 1)]
                nearby = [n for x in range(math.floor(lo[0]*200), math.floor(hi[0]*200)+1)
                          for y in range(math.floor(lo[1]*200), math.floor(hi[1]*200)+1)
                          for n in node_grid.get((x,y), ())]
                access.extend(n for n in nearby if (n in refs or inside(coords[n], ring))
                              and not any(inside(coords[n], hole) for hole in holes))
        if not access:
            continue
        business = dict(id=f"osm:{e['type'][0]}{e['id']}", category=category,
                        name=tags.get('name') or tags.get('brand') or tags.get('operator') or '',
                        brand=tags.get('brand', ''), provenance='inferred',
                        source=f"https://www.openstreetmap.org/{e['type']}/{e['id']}")
        businesses.append(business)
        for n in access: by_node[n].append(len(businesses)-1)
    main, links = [], []
    for wid, (refs, tags) in sorted(ways.items()):
        if not allowed(tags): continue
        if controlled(tags): main.append((wid, refs, tags))
        if tags.get('highway') in ('motorway_link', 'trunk_link'): links.append((wid, refs, tags))
    records = []
    for record in exitsigns.index_records(main, links, coords, nodes):
        extra = record['ways'][0]
        state = str(signstates.states(record['lat'], record['lon']).item())
        if not state or not extra['label']['ref']:
            continue
        rules = policy(state, policy_path)
        rid = extra['ramp_id']
        refs, tags = ways[rid]
        refs = refs[::-1] if exitsigns._direction(tags) == -1 else refs
        # Start on this outgoing ramp, and retain every metre of its cost.
        start = refs[0]
        limit = max(rules['distance_m'].values())
        queue, distances, found = [(0., start)], {start: 0.}, {}
        previous = {}
        while queue:
            distance, at = heapq.heappop(queue)
            if distance != distances[at]: continue
            for index in by_node.get(at, ()):
                b = businesses[index]
                if distance <= rules['distance_m'].get(b['category'], -1):
                    if b['id'] not in found:
                        at_path, route = at, []
                        while at_path in previous:
                            before, way = previous[at_path]
                            route.append(way)
                            at_path = before
                        route.reverse()
                        route = [w for i, w in enumerate(route) if i == 0 or w != route[i-1]]
                        found[b['id']] = dict(b, distance_m=round(distance, 1), access_node=at, route_way_ids=route)
            for to, length, wid in graph.get(at, ()):
                if at == start and wid != rid: continue
                cost = distance+length
                if cost <= limit and cost < distances.get(to, math.inf):
                    distances[to] = cost
                    previous[to] = at, wid
                    heapq.heappush(queue, (cost, to))
        if not found: continue
        services = sorted(found.values(), key=lambda b: (b['category'], b['distance_m'], b['id']))
        extra['services'] = services
        extra['service_state'] = state
        approach = extra['approach']
        pad = .001
        lo = [min(p[i] for p in approach)-pad for i in (0,1)]
        hi = [max(p[i] for p in approach)+pad for i in (0,1)]
        extra['service_barriers'] = [line for line in barriers if line and
            all(max(p[i] for p in line) >= lo[i] and min(p[i] for p in line) <= hi[i] for i in (0,1))]
        records.append(dict(id=f"service:{rid}", lon=record['lon'], lat=record['lat'], tags=record['tags'],
                            exit=extra, provenance='inferred'))
    return dict(format=1, attribution='© OpenStreetMap contributors, ODbL 1.0',
                osm_timestamp=data.get('osm3s', {}).get('timestamp_osm_base'), objects=records,
                diagnostics=dict(elements=len(elements), connected_businesses=len(businesses), exits_with_services=len(records)))


def apply_verifications(result, verifications):
    """Apply audited participation evidence only to already reachable businesses.

    The sign location/layout remain inferred. No external asset or logo is
    implicitly licensed by a verification of business participation.
    """
    matched = 0
    for v in verifications:
        if not all(v.get(k) for k in ('ramp_id', 'business_id', 'source', 'checked_by', 'checked_at')):
            raise ValueError('Verification needs ramp_id, business_id, source, checked_by, checked_at')
        if not str(v['source']).startswith('https://'):
            raise ValueError('Verification requires a source URL')
        business = next((b for o in result['objects'] if o['exit']['ramp_id'] == int(v['ramp_id'])
                         for b in o['exit']['services'] if b['id'] == v['business_id']), None)
        if business is None:
            raise ValueError('Verified business is not reachable from the specified exit')
        business['provenance'] = 'verified'
        business['verification'] = {k: v[k] for k in ('source', 'checked_by', 'checked_at')}
        matched += 1
    result['diagnostics']['verified_business_associations'] = matched
    return result
