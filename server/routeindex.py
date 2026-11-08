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
import highwayroutes

ROAD_CLASSES = ('motorway', 'trunk', 'primary', 'secondary', 'tertiary')
WAY_KEYS = ('highway', 'ref', 'name', 'oneway', 'junction', 'bridge', 'tunnel',
            'access', 'direction', 'network', 'modifier', 'destination:ref', 'destination:direction',
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

def route_refs(tags, relations, state=''):
    return highwayroutes.recognize(tags,relations,state)

def posted_direction(tags, relations, direction, route, axis):
    suffix = ':forward' if direction > 0 else ':backward'
    value = throughsigns.cardinal(tags.get('destination:direction'+suffix,
                                tags.get('destination:direction', tags.get('direction'))))
    choices = {throughsigns.cardinal(r.get('direction')) or throughsigns.cardinal(r.get('role')) for r in relations
               if (not r.get('network') or highwayroutes.network(r.get('network')) == route[0]) and
               (r.get('role') not in ('forward', 'backward') or
                r['role'] == ('forward' if direction > 0 else 'backward'))}
    choices.discard('')
    if value or len(choices) == 1:
        return value or next(iter(choices))
    # No posted direction: use the route's numbered axis, then local travel.
    # Directional relations take priority on roads like Michigan I-69 (E/W).
    # Federal numbering has a defined axis. State systems do not share it.
    numbered=route and re.fullmatch(r'\d+[A-Z]?',route[1]) and route[0] in ('US:I','US:US')
    ns = int(re.match(r'\d+',route[1])[0]) % 2 if numbered else abs(axis[1]) >= abs(axis[0])
    return ('NORTH' if axis[1] >= 0 else 'SOUTH') if ns else ('EAST' if axis[0] >= 0 else 'WEST')

def index_records(ways, coords, relations=None):
    relations = relations or {}
    edges, outgoing, incoming = [], defaultdict(list), defaultdict(list)
    mids = [(wid,coords[refs[len(refs)//2]]) for wid,refs,_ in ways
            if refs and refs[len(refs)//2] in coords]
    states = signstates.states(np.array([p[1] for _,p in mids]),np.array([p[0] for _,p in mids]))
    way_states = dict(zip((wid for wid,_ in mids),states))
    neighbours = defaultdict(set)
    for wid, refs, _ in ways:
        if not way_states.get(wid):
            continue
        for a,b in zip(refs,refs[1:]):
            neighbours[a].add(b); neighbours[b].add(a)
    for wid, refs, tags in sorted(ways, key=lambda r: r[0]):
        if tags.get('access') in ('private', 'no') or tags.get('junction') == 'roundabout':
            continue
        # Missing coordinates split a way; never draw across a missing extract.
        if len(refs) < 2 or any(n not in coords for n in refs):
            continue
        state = str(way_states.get(wid,''))
        if not state:
            continue
        routes = route_refs(tags, relations.get(wid, ()),state)
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
            signature = (tuple(routes), '' if routes else tags.get('ref', '') or tags.get('name', ''))
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

        # Starts, major intersections and divergences only. A connected route
        # never restarts an interval at a tile or OSM way boundary.
        events = [0.] + [float(arc[k]) for k,n in enumerate(refs) if len(neighbours[n])>2]
        stations=[]
        for event in sorted(set(events)):
            station=event+100.
            if station >= arc[-1]-45. or any(abs(station-other)<45. for other in events if other!=event):
                continue
            if stations and station-stations[-1][0]<250.:
                continue
            stations.append((station,'shield',{},round(event,3)))
        if not stations:
            continue
        points = np.array([at(s)[0] for s, *_ in stations])
        states = signstates.states(points[:, 1], points[:, 0])
        for (s, kind, content, source_id), point, state in zip(stations, points, states):
            if not state:
                continue
            _, k = at(s)
            wid, _, tags, direction, routes, _ = edges[owners[k]]
            # Ambiguous SR/SH fallback follows the station's actual state,
            # rather than a long way's midpoint across an administrative border.
            routes=route_refs(tags,relations.get(wid,()),str(state))
            if not ground(tags):
                continue
            axis = (path[k+1]-path[k])*scale
            axis /= max(np.linalg.norm(axis), 1e-9)
            if kind == 'shield':
                if not routes:
                    continue
                active = [r for r in routes if not any(
                    highwayroutes.network(rel.get('network')) == r.network and str(rel.get('ref')) == r.ref
                    for rel in relations.get(wid,())) or any(
                    highwayroutes.network(rel.get('network')) == r.network and str(rel.get('ref')) == r.ref and
                    rel.get('role','') not in (('backward',) if direction>0 else ('forward',))
                    for rel in relations.get(wid,()))]
                content = dict(routes=[highwayroutes.panel(r,
                    posted_direction(tags, relations.get(wid, ()), direction, r, axis)) for r in active])
                if not content['routes']:
                    continue
            start, end = max(0., s-60), min(float(arc[-1]), s+60)
            local = np.vstack([at(start)[0], path[(arc > start) & (arc < end)], at(end)[0]])
            extra = dict(type=kind, point=point.tolist(), approach=local.tolist(), main_id=wid,
                         axis=axis.tolist(), state=str(state), **content)
            out.append(record(('shield-v2', refs[0], refs[-1], source_id, edges[first][3],tuple(routes)), point, extra))
    return unique(out)


def unique(objects, separation=250.):
    """Deterministic local deduplication, preserving opposite carriageways and concurrency."""
    buckets=defaultdict(list)
    out=[]
    extra=lambda o:o.get('roadside') or o['ways'][0]
    order=lambda o:(not extra(o).get('mapped',False),o['id'])
    for obj in sorted({o['id']:o for o in objects}.values(),key=order):
        d=extra(obj)
        x,y=math.floor(obj['lon']/.02),math.floor(obj['lat']/.005)
        covered=set()
        for bx in range(x-1,x+2):
            for by in range(y-1,y+2):
                for old in buckets[bx,by]:
                    od=extra(old)
                    if np.dot(d['axis'],od['axis'])<.8:
                        continue
                    scale=math.cos(math.radians((old['lat']+obj['lat'])/2))
                    distance=111320.*math.hypot((old['lon']-obj['lon'])*scale,old['lat']-obj['lat'])
                    if distance<separation:
                        covered.update(highwayroutes.identity(r)[:3] for r in od['routes'])
        routes=[r for r in d['routes'] if highwayroutes.identity(r)[:3] not in covered]
        if routes:
            obj=dict(obj)
            if 'roadside' in obj: obj['roadside']=dict(d,routes=routes)
            else: obj['ways']=[dict(d,routes=routes)]
            out.append(obj); buckets[x,y].append(obj)
    return sorted(out,key=lambda o:o['id'])


def mapped_records(ways, coords, relations, nodes):
    """Mapped M1 route markers with a known travel direction, on or beside a way.

    A point on a road locates its station, not a post in the pavement. An
    independently mapped roadside point retains its actual position.
    """
    out=[]
    if not nodes:
        return out
    # Index eligible ways once; a planet's mapped markers must not each scan
    # its entire road network. Cache full geometry, not per-node copies.
    grid=defaultdict(list)
    for wid,refs,wt in ways:
        if not ground(wt) or len(refs)<2 or any(n not in coords for n in refs):
            continue
        path=np.asarray([coords[n] for n in refs])
        state=str(signstates.states(path[len(path)//2,1],path[len(path)//2,0]).item())
        route_refs_=route_refs(wt,relations.get(wid,()),state)
        if not route_refs_:
            continue
        lo=np.floor(path.min(0)/.05).astype(int);hi=np.floor(path.max(0)/.05).astype(int)
        for x in range(lo[0],hi[0]+1):
            for y in range(lo[1],hi[1]+1):
                grid[x,y].append((wid,path,wt,route_refs_))
    for nid,point,tags in nodes:
        code=';'.join(str(tags.get(k,'')) for k in ('traffic_sign','traffic_sign:forward','traffic_sign:backward'))
        if not re.search(r'M1-[145](?:\b|$)',code):
            continue
        point=np.asarray(point,float)
        scale=np.array([111320.*math.cos(math.radians(point[1])),111320.])
        choices=[]
        state=str(signstates.states(point[1],point[0]).item())
        own_refs=route_refs(tags,(),state)
        x,y=np.floor(point/.05).astype(int)
        nearby={row[0]:row for gx in range(x-1,x+2) for gy in range(y-1,y+2) for row in grid[gx,gy]}
        for wid,path,wt,rr in nearby.values():
            if own_refs and not set(own_refs)&set(rr):
                continue
            if (np.maximum(path.min(0)-point,point-path.max(0))*scale>40.).any():
                continue
            xy=(path-point)*scale
            delta=np.diff(xy,axis=0)
            t=np.clip(-(xy[:-1]*delta).sum(1)/np.maximum((delta*delta).sum(1),1e-9),0.,1.)
            distance=np.linalg.norm(xy[:-1]+delta*t[:,None],axis=1)
            k=int(distance.argmin())
            if distance[k]<=40.:
                choices.append((float(distance[k]),wid,k,path,wt))
        if not choices:
            continue
        distance,wid,k,path,wt=min(choices,key=lambda c:(c[0],c[1]))
        direction=tags.get('direction','')
        one=exitsigns._direction(wt)
        if not one and wt.get('highway')=='motorway' and wt.get('oneway') not in ('no','0','false'):
            one=1
        directions=([1] if 'traffic_sign:forward' in tags or direction=='forward' else [])+(
                    [-1] if 'traffic_sign:backward' in tags or direction=='backward' else [])
        if not directions and one:
            directions=[one]
        state=str(signstates.states(point[1],point[0]).item())
        refs=own_refs or route_refs(wt,relations.get(wid,()),state)
        kinds={kind for sign,kind in (('M1-1','interstate'),('M1-4','us'),('M1-5','state')) if sign in code}
        refs=[r for r in refs if highwayroutes.panel(r)['kind'] in kinds]
        for direction in directions:
            approach=path[max(0,k-1):min(len(path),k+3)]
            if direction<0: approach=approach[::-1]
            axis=(approach[-1]-approach[0])*scale
            axis/=max(np.linalg.norm(axis),1e-9)
            panels=[highwayroutes.panel(r,posted_direction(wt,relations.get(wid,()),direction,r,axis)) for r in refs]
            if not panels:
                continue
            extra=dict(type='shield',routes=panels,main_id=wid,point=point.tolist(),approach=approach.tolist(),
                       axis=axis.tolist(),state=state,mapped=True,mapped_position=distance>2.)
            out.append(record(('mapped-shield',nid,direction),point,extra))
    return out
