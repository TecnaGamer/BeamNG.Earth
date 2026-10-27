"""Whole-mile Interstate reference signs, measured independently of world/road tiles.

Bundled DOT points work offline. Mapped OSM mileposts supplement them through the
regional roadside index. No worker invents mile zero or restarts a count at a tile.
The panel is opaque geometry with the existing tested opacity-atlas lettering.
"""
from collections import defaultdict
from functools import lru_cache
import hashlib
import json
import logging
import math
from pathlib import Path
import re

import numpy as np

import exitsigns

log = logging.getLogger('earthd.milemarkers')

DATA = Path(__file__).with_name('mile_markers_us.json')
MILE_M = 1609.344
WIDTH, HEIGHT, BOTTOM = .55, 2.10, 1.15
QUERY_MARGIN = 200.
MAX_SNAP_M = 150.
OPPOSITE = dict(N='S', S='N', E='W', W='E')
DIRECTIONS = dict(N='NORTH', S='SOUTH', E='EAST', W='WEST')
AXES = dict(N=(0., 1.), S=(0., -1.), E=(1., 0.), W=(-1., 0.))
MATERIALS = exitsigns.MATERIALS + ['bne_mile_red', 'bne_mile_blue', 'bne_mile_green']
TAG_KEYS = ('distance', 'carriageway_ref', 'milestone', 'ref', 'direction', 'highway')
WAY_KEYS = ('ref', 'direction', 'network')


def interstate(value):
    """Only explicitly identified Interstates; a bare number is not a route type."""
    found = re.search(r'\bI(?:NTERSTATE)?[ -]*(\d{1,3})(?!\d)', str(value or ''), re.I)
    return int(found[1]) if found and int(found[1]) > 0 else None


def cardinal(value):
    value = str(value or '').upper().strip()
    for short, full in DIRECTIONS.items():
        if re.search(r'\b(?:'+full+r'(?:BOUND)?|'+short+r'B?)\b', value):
            return short
    return ''


def miles(value):
    """OSM distance has km as its default; explicit mi stays in miles."""
    match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*(mi|miles?|km)?\s*', str(value or ''), re.I)
    if not match:
        return None
    distance = float(match[1])
    return distance if (match[2] or '').lower() in ('mi', 'mile', 'miles') else distance*1000./MILE_M


def marker_id(state, route, mile, direction):
    # A separate negative ID range from OSM nodes (positive) and exit way IDs.
    key = f'{state}:{route}:{float(mile):g}:{direction}'.encode()
    return -(1 << 62)-int.from_bytes(hashlib.blake2b(key, digest_size=7).digest(), 'little')


def _object(state, route, mile, direction, point, axis, source):
    lon, lat = map(float, point)
    return dict(id=marker_id(state, route, mile, direction), lon=lon, lat=lat, tags={},
                mile_marker=dict(state=state, route=int(route), mile=round(float(mile), 1) if float(mile) % 1 else int(mile), direction=direction,
                                 axis=list(map(float, axis)), source=source))


@lru_cache(maxsize=1)
def references():
    """One immutable reference set; normalize both carriageways before any tile query."""
    try:
        with DATA.open(encoding='utf-8') as stream:
            data = json.load(stream)
    except (OSError, ValueError) as exc:
        log.warning('Mile-marker references unavailable: %s; continuing with OSM references', exc)
        return (), np.empty((0,2))
    grouped = defaultdict(lambda: defaultdict(list))
    interpolated = {tuple(row) for row in data.get('interpolated',())}
    for state, route, mile, direction, lon, lat in data['markers']:
        grouped[state, int(route)][int(mile)].append((direction, np.array([lon, lat], float)))
    out = []
    for (state, route), stations in sorted(grouped.items()):
        ordered = sorted(stations)
        centres = {mile: np.mean([point for _, point in stations[mile]], axis=0) for mile in ordered}
        for i, mile in enumerate(ordered):
            before = ordered[max(0, i-1)]
            after = ordered[min(len(ordered)-1, i+1)]
            delta = centres[after]-centres[before]
            delta *= [math.cos(math.radians(float(centres[mile][1]))), 1.]
            norm = float(np.linalg.norm(delta))
            directions = {d for d, _ in stations[mile] if d}
            if not directions:
                # These two sources use statewide Interstate mileage. MI carries the
                # actual posted direction, including I-69's EAST/WEST section.
                directions = {'N' if route % 2 else 'E'}
            directions |= {OPPOSITE[d] for d in directions}
            for direction in sorted(directions):
                points = [p for d, p in stations[mile] if d == direction]
                point = np.mean(points, axis=0) if points else centres[mile]
                axis = delta/norm if norm > 1e-12 else np.array(AXES[direction])
                if direction in ('S', 'W'):
                    axis = -axis if norm > 1e-12 else axis
                source = 'dot:'+state.lower()
                if (state,route,mile,direction) in interpolated:
                    source += ':calibrated'
                out.append(_object(state, route, mile, direction, point, axis,source))
    import medianbarriers
    known = {(o['mile_marker']['state'], o['mile_marker']['route'], o['mile_marker']['mile']) for o in out}
    for state, stations in medianbarriers.station_sets():
        for route, mile, lon, lat, ax, ay in stations:
            if (state, int(route), mile) not in known:
                out.append(_object(state, int(route), mile, 'N', [lon, lat], [ax, ay],
                                   'dot:'+state.lower()+':osm-calibrated'))
    xy = np.array([[o['lon'], o['lat']] for o in out], float).reshape(-1, 2)
    return tuple(out), xy


def query(frame, box):
    """A vector bbox lookup, with no downloads or regional PBF work on the road thread."""
    x0, y0, x1, y1 = box
    lat, lon, _ = frame.enu_to_geodetic(np.array([x0,x1,x0,x1]), np.array([y0,y0,y1,y1]), 0.)
    objects, xy = references()
    keep = ((xy[:, 0] >= lon.min()) & (xy[:, 0] <= lon.max()) &
            (xy[:, 1] >= lat.min()) & (xy[:, 1] <= lat.max()))
    return [objects[i] for i in np.flatnonzero(keep)]


def _metric(points, at):
    return (np.asarray(points, float)-at)*[111320.*math.cos(math.radians(float(at[1]))), 111320.]


def unique_objects(objects):
    """DOT and OSM can describe the same reference; prefer the DOT inventory once."""
    groups, out = defaultdict(list), []
    ordered = sorted((o for o in objects if o.get('mile_marker')),
                     key=lambda o: (not o['mile_marker']['source'].startswith('dot:'),o['id']))
    for obj in ordered:
        info = obj['mile_marker']
        key = (info['route'],info['mile'],info['direction'])
        point = np.array([obj['lon'],obj['lat']])
        if any(np.linalg.norm(_metric([p],point)) < 750. for p in groups[key]):
            continue
        groups[key].append(point)
        out.append(obj)
    return out


def osm_records(main_ways, coords, markers, route_ways=(), mile_step=1., extend_miles=0., route_directions=None,
                calibration_range=(.90, 1.10)):
    """Mapped references plus calibrated whole miles between compatible OSM anchors.

    Directed, unbranched Interstate chains only. Ambiguous forks, resets and gaps
    inconsistent with the stated mileage are not extrapolated across.
    """
    metadata = defaultdict(set)
    for wid, route, direction, state in route_ways:
        metadata[wid].add((int(route), direction, state))
    grouped = defaultdict(list)
    for wid, refs, tags in main_ways:
        if tags.get('highway') != 'motorway' or tags.get('access') in ('private', 'no'):
            continue
        routes = metadata[wid]
        route = interstate(tags.get('ref'))
        if not routes and route:
            routes = {(route, cardinal(tags.get('direction')), '')}
        if not routes:
            continue
        direction = exitsigns._direction(tags)
        if not direction:
            continue
        path = refs if direction > 0 else refs[::-1]
        # Never join across missing extract nodes by deleting them from the path.
        if len(path) < 2 or any(n not in coords for n in path):
            continue
        for route, posted, state in sorted(routes):
            if not posted:
                d = _metric([coords[path[-1]]], np.asarray(coords[path[0]], float))[0]
                posted = ('N' if d[1] >= 0 else 'S') if abs(d[1]) >= abs(d[0]) else ('E' if d[0] >= 0 else 'W')
            grouped[state, route].append((wid, path, posted))
    anchors = []
    for nid, (lon, lat, tags) in sorted(markers.items()):
        mile = miles(tags.get('distance'))
        if mile is None or not math.isfinite(mile) or not 0 <= mile < 2000:
            continue
        ref = tags.get('carriageway_ref') or tags.get('ref', '')
        anchors.append((nid, np.array([lon,lat]), mile, interstate(ref), cardinal(ref) or cardinal(tags.get('direction'))))
    out = {}
    for (state, route), ways in sorted(grouped.items()):
        starts, ends = defaultdict(list), defaultdict(list)
        posted_by_way = {wid:posted for wid,_,posted in ways}
        ways = {wid:path for wid,path,_ in ways}
        for wid, path in ways.items():
            starts[path[0]].append(wid)
            ends[path[-1]].append(wid)
        visited = set()
        ordered = sorted(ways, key=lambda wid: (len(ends[ways[wid][0]]) == 1, wid))
        for seed in ordered:
            if seed in visited:
                continue
            chain, posteds, current = [], [], seed
            while current not in visited:
                visited.add(current)
                path = ways[current]
                chain += path if not chain else path[1:]
                posteds += [posted_by_way[current]]*(len(path)-1)
                following = starts[path[-1]]
                if len(following) != 1 or len(ends[path[-1]]) != 1:
                    break
                current = following[0]
            points = np.array([coords[n] for n in chain], float)
            if len(points) < 2:
                continue
            origin = points.mean(axis=0)
            xy = _metric(points, origin)
            ab = np.diff(xy, axis=0)
            lengths = np.linalg.norm(ab, axis=1)
            lengths2 = np.maximum(lengths**2, 1e-12)
            ss = np.r_[0., np.cumsum(lengths)]
            found = []
            low,high = points.min(axis=0)-.003,points.max(axis=0)+.003
            for nid, point, mile, marker_route, marker_direction in anchors:
                if marker_route not in (None, route):
                    continue
                if np.any(point < low) or np.any(point > high):
                    continue
                q = _metric([point], origin)[0]
                t = np.clip(((q-xy[:-1])*ab).sum(1)/lengths2, 0., 1.)
                dist = np.linalg.norm(xy[:-1]+t[:, None]*ab-q, axis=1)
                k = int(dist.argmin())
                if marker_direction and marker_direction != posteds[k]:
                    continue
                if dist[k] <= 40.:
                    found.append((float(ss[k]+t[k]*lengths[k]), mile, nid))
            found.sort()
            candidates = [(s, round(m/mile_step)*mile_step, 'osm') for s,m,_ in found
                          if abs(m/mile_step-round(m/mile_step)) < .001]
            for (s0,m0,_), (s1,m1,_) in zip(found[:-1], found[1:]):
                if abs(m1-m0) < .01 or abs(m1-m0) > 30.:
                    continue
                ratio = (s1-s0)/(abs(m1-m0)*MILE_M)
                if not calibration_range[0] <= ratio <= calibration_range[1]:
                    continue
                for tick in range(math.floor(min(m0,m1)/mile_step)+1, math.ceil(max(m0,m1)/mile_step)):
                    mile = round(tick*mile_step, 1)
                    candidates.append((s0+(mile-m0)/(m1-m0)*(s1-s0), mile, 'osm:interpolated'))
            if extend_miles and len(found) >= 2:
                # At state-line tails only, carry a nearby measured calibration
                # along the same connected path, at most the requested distance.
                for edge, neighbour, sign in ((found[0],found[1],-1), (found[-1],found[-2],1)):
                    s0,m0,_ = edge
                    s1,m1,_ = neighbour
                    if abs(m1-m0) < .01 or not .90 <= abs((s1-s0)/((m1-m0)*MILE_M)) <= 1.10:
                        continue
                    slope = (s1-s0)/(m1-m0)
                    for tick in range(1, round(extend_miles/mile_step)+1):
                        mile = round(m0+np.sign(slope)*sign*tick*mile_step, 1)
                        station = s0+(mile-m0)*slope
                        if mile >= 0 and 0 <= station <= ss[-1]:
                            candidates.append((station, mile, 'osm:tail-calibrated'))
            for station, mile, source in sorted(candidates, key=lambda row: (row[0], row[2] != 'osm')):
                k = min(len(points)-2, max(0, int(np.searchsorted(ss,station,side='right')-1)))
                fraction = np.clip((station-ss[k])/max(lengths[k],1e-9), 0., 1.)
                point = points[k]*(1.-fraction)+points[k+1]*fraction
                axis = ab[k]/max(lengths[k],1e-9)
                posted = posteds[k]
                if route_directions and route in route_directions and len(found) >= 2:
                    # Posted NORTH/SOUTH follows increasing/decreasing chainage,
                    # including bends where northbound temporarily travels south.
                    j = int(np.clip(np.searchsorted([a[0] for a in found], station)-1, 0, len(found)-2))
                    forward = route_directions[route]
                    if found[j+1][1] != found[j][1]:
                        posted = forward if found[j+1][1] > found[j][1] else OPPOSITE[forward]
                obj = _object(state, route, mile, posted, point, axis, source)
                key = obj['id']
                old = out.get(key)
                if old is None or old['mile_marker']['source'] != 'osm':
                    out[key] = obj
    return [out[k] for k in sorted(out)]


def source_pavements(frame, obj, context, left_hand=False):
    """One un-clipped road neighbourhood fixes the marker's owning tile."""
    return context.pavements(frame, [[obj['lon'], obj['lat']]], left_hand)





def place(frame, objects, pavements, box, elev, chunks=()):
    """Face traffic beyond the entire paved shoulder; exactly one owning tile per sign."""
    import signroads as roadlanes
    x0,y0,x1,y1 = box
    out, seen = [], set()
    polygons = [exitsigns._outline(p,w) for _,_,p,w in pavements]
    for obj in unique_objects(objects):
        info = obj.get('mile_marker')
        if not info:
            continue
        if ((info.get('state') == 'KY' and info['route'] in (65, 75)) or
                (info.get('state') == 'IN' and info['route'] == 65)):
            import medianbarriers
            mounting = medianbarriers.mount(frame, obj, pavements, elev)
            if mounting is not None:
                centre, travel = mounting
                # A single post carries separately readable opposite faces.
                if info['direction'] == 'N' and x0 <= centre[0] < x1 and y0 <= centre[1] < y1:
                    key = (info['state'], info['route'], info['mile'], 'N')
                    if key not in seen:
                        seen.add(key)
                        out.append(dict(id=obj['id'], kind='mile_marker', centre=centre.tolist(),
                                        travel=travel.tolist(), route=info['route'], direction='NORTH',
                                        mile=info['mile'], bottom=float(centre[2])+.55,
                                        width=.61, height=2.44, source=info['source'],
                                        style='ky_blue', double_sided=True))
                continue
            if float(info['mile']) % 1:
                continue           # fractional posts are mounted only on eligible barriers
        key = (info.get('state',''), info['route'], info['mile'], info['direction'])
        if key in seen:
            continue
        seen.add(key)
        q = exitsigns._xy(frame, [[obj['lon'],obj['lat']]])[0]
        axis = np.asarray(info['axis'], float)
        if not np.isfinite(axis).all() or np.linalg.norm(axis) < .1:
            continue
        axis /= np.linalg.norm(axis)
        candidates = []
        for fid, spec, line, width in pavements:
            if spec.cls != 'motorway' or spec.link or not spec.oneway:
                continue
            near = exitsigns._nearest(q, [(fid,spec,line,width)])
            if near is None or near[0] > MAX_SNAP_M:
                continue
            travel = near[7]*(spec.dir or 1)
            # Do not slide a post along the highway to the end of a clipped span,
            # across a tunnel gap, or to a distant nearby interchange leg.
            along = abs(float((near[6]-q)@travel))
            if float(travel@axis) > .35 and along <= 18.:
                candidates.append((near[0], str(fid), near, travel, width))
        if not candidates:
            continue
        _,_,near,travel,width = min(candidates, key=lambda c: c[:2])
        _,_,spec,line,k,t,at,_ = near
        if exitsigns._on_deck(at, chunks, 1.):
            continue
        right = np.array([travel[1],-travel[0]])
        cross = roadlanes.cross(line)
        edge = abs((cross[k]*(1.-t)+cross[k+1]*t)@right)*width/2.
        centre = None
        for gap in (1.2, 2., 3., 4.5, 6., 8., 10., 12.):
            trial = at+right*(edge+gap+WIDTH/2.)
            if exitsigns._clear(trial, WIDTH/2.+.35, polygons):
                centre = trial
                break
        if centre is None or not (x0 <= centre[0] < x1 and y0 <= centre[1] < y1):
            continue
        base = exitsigns._ground_z(frame,elev,centre)
        road_z = exitsigns._ground_z(frame,elev,at)
        out.append(dict(id=obj['id'], kind='mile_marker', centre=[float(centre[0]),float(centre[1]),base],
                        travel=travel.tolist(), route=info['route'], direction=DIRECTIONS[info['direction']],
                        mile=info['mile'], bottom=max(base,road_z)+BOTTOM,
                        width=WIDTH,height=HEIGHT,source=info['source']))
    return out


class Mesh(exitsigns.Mesh):
    def text(self, value, y, baseline, height, max_width):
        """Independently condense letter width, retaining the tall reference-sign lettering."""
        value = exitsigns._text(value)
        data = exitsigns.lettering()
        sy = height/data['capHeight']
        sx = min(sy,max_width/max(exitsigns.text_width(value,data['capHeight']),1e-6))
        cursor = y-sum(data['glyphs'][c]['advance'] for c in value)*sx/2.
        for c in value:
            glyph = data['glyphs'][c]
            l,t,r,b = glyph['bbox']
            u0,v0,u1,v1 = glyph['uv']
            if r > l and b > t:
                self.quad([(.074,cursor+l*sx,baseline-b*sy),(.074,cursor+r*sx,baseline-b*sy),
                           (.074,cursor+r*sx,baseline-t*sy),(.074,cursor+l*sx,baseline-t*sy)],
                          3,[(u0,v0),(u1,v0),(u1,v1),(u0,v1)])
            cursor += glyph['advance']*sx

    def panel(self, sign):
        if sign.get('style') == 'ky_blue':
            self.kentucky_panel(sign)
            return
        self.centre = np.asarray(sign['centre'],float)
        travel = np.asarray(sign['travel'],float)
        self.face = np.r_[-travel,0.]
        self.right = np.array([travel[1],-travel[0],0.])
        w,h = sign['width'],sign['height']
        bottom = sign['bottom']-self.centre[2]
        # One galvanized channel post, as in the supplied photograph.
        self.box((-.105,-.0325,-.22),(-.04,.0325,bottom+h-.14),2)
        self.rounded(w,bottom,h,.055,.05,2,back=True,front=False)
        rings = [self.outline(w-2*m,bottom+m,h-2*m,r)
                 for m,r in ((0.,.055),(.012,.044),(.026,.030))]
        for outer,inner,material in zip(rings,rings[1:],(7,1)):
            for i in range(len(outer)):
                j = (i+1)%len(outer)
                self.quad([(.05,*outer[i]),(.05,*outer[j]),(.05,*inner[j]),(.05,*inner[i])],material)
        for i in range(1,len(rings[-1])-1):
            self.triangle(np.c_[np.full(3,.05),rings[-1][[0,i,i+1]]],7)
        self.text(sign['direction'],0.,bottom+1.78,.235,w-.095)
        self.shield(str(sign['route']),bottom+1.11,.405,.51)
        self.text('MILE',0.,bottom+.72,.24,w-.095)
        self.text(str(sign['mile']),0.,bottom+.18,.38,w-.105)

    def kentucky_panel(self, sign):
        """Blue 24 x 96 inch reference-style board, white outline and separate tenths box."""
        self.centre = np.asarray(sign['centre'], float)
        travel = np.asarray(sign['travel'], float)
        self.face = np.r_[-travel, 0.]
        self.right = np.array([travel[1], -travel[0], 0.])
        bottom = sign['bottom']-self.centre[2]
        w, h = sign['width'], sign['height']
        # The centre is the wall's top, so the galvanized post seats into concrete.
        self.box((-.0325, -.0325, -.10), (.0325, .0325, bottom+h-.12), 2)
        self.rounded(w, bottom, h, .045, .05, 2, back=True, front=False)
        tenths = int(round(float(sign['mile'])*10))
        for face in range(2 if sign.get('double_sided') else 1):
            self.face = np.r_[(-travel if face == 0 else travel), 0.]
            self.right = np.array([travel[1], -travel[0], 0.])*(1 if face == 0 else -1)
            rings = [self.outline(w-2*m, bottom+m, h-2*m, r)
                     for m,r in ((0., .045), (.012, .034), (.026, .022))]
            for outer, inner, mat in zip(rings, rings[1:], (6, 1)):
                for i in range(len(outer)):
                    j = (i+1)%len(outer)
                    self.quad([(.05,*outer[i]),(.05,*outer[j]),(.05,*inner[j]),(.05,*inner[i])], mat)
            inner = rings[-1]
            # Split the blue interior around the divider; no coplanar overlapping panels.
            split = bottom+.49
            def clip(poly, level, above):
                result = []
                for a,b in zip(poly, np.roll(poly,-1,axis=0)):
                    ia,ib = (a[1]>=level,b[1]>=level) if above else (a[1]<=level,b[1]<=level)
                    if ia: result.append(a)
                    if ia != ib: result.append(a+(b-a)*((level-a[1])/(b[1]-a[1])))
                return np.asarray(result)
            for poly, mat in ((clip(inner, split+.008, True), 6),
                              (clip(clip(inner, split+.008, False), split-.008, True), 1),
                              (clip(inner, split-.008, False), 6)):
                for i in range(1, len(poly)-1):
                    self.triangle(np.c_[np.full(3,.05), poly[[0,i,i+1]]], mat)
            self.text('NORTH' if face == 0 else 'SOUTH', 0., bottom+2.10, .265, w-.075)
            self.shield(str(sign['route']), bottom+1.39, .45, .58)
            self.text('MILE', 0., bottom+.99, .265, w-.08)
            self.text(str(tenths//10), 0., bottom+.57, .36, w-.095)
            self.text('.'+str(tenths%10), 0., bottom+.08, .36, w-.13)

    def shield(self, route, bottom, width, height):
        # Interstate contour: broad red crown, curved blue bowl and a pointed base.
        contour = np.array([[-.42,1.],[-.22,.96],[0.,.97],[.22,.96],[.42,1.],
                            [.48,.83],[.50,.66],[.47,.48],[.39,.29],[.24,.13],[0.,0.],
                            [-.24,.13],[-.39,.29],[-.47,.48],[-.50,.66],[-.48,.83]])[::-1]
        outer = contour*[width,height]+[0.,bottom]
        inner = (contour-[0.,.53])*[width-.028,height-.028]+[0.,bottom+height*.53]
        # Border, red and blue are non-overlapping faces at the same depth. This
        # avoids the white-at-distance depth fight of stacked sign panels.
        for i in range(len(outer)):
            j = (i+1)%len(outer)
            self.quad([(.063,*outer[i]),(.063,*outer[j]),(.063,*inner[j]),(.063,*inner[i])],1)
        def clip(poly, level, above):
            out = []
            for a,b in zip(poly,np.roll(poly,-1,axis=0)):
                ia,ib = (a[1]>=level,b[1]>=level) if above else (a[1]<=level,b[1]<=level)
                if ia: out.append(a)
                if ia != ib:
                    out.append(a+(b-a)*((level-a[1])/(b[1]-a[1])))
            return np.asarray(out)
        split = bottom+height*.73
        for poly,material in ((clip(inner,split+.006,True),5),
                              (clip(clip(inner,split+.006,False),split-.006,True),1),
                              (clip(inner,split-.006,False),6)):
            centre = poly.mean(axis=0)
            for a,b in zip(poly,np.roll(poly,-1,axis=0)):
                self.triangle([(.063,*centre),(.063,*a),(.063,*b)],material)
        self.text('INTERSTATE',0.,bottom+height*.79,height*.075,width-.065)
        self.text(route,0.,bottom+height*.23,height*.38,width-.075)

    def result(self):
        if not self.t:
            return None
        return np.asarray(self.p),np.asarray(self.t),np.asarray(self.uv),MATERIALS,np.asarray(self.mi)


def build(signs, origin):
    mesh = Mesh(origin)
    for sign in signs:
        mesh.panel(sign)
    return mesh.result()
