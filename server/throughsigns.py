"""Mapped mainline destinations on generated overhead highway sign gantries.

Destination names are posted OSM labels, including control cities far down a
route. No nearest-city selection or route-endpoint guesses are involved. The
assemblies share the exit-sign mesh, materials, streaming and visibility switch.
"""
from collections import defaultdict
import hashlib
import json
import math

import numpy as np

import exitsigns as signs

WAY_KEYS = tuple(dict.fromkeys(signs.WAY_KEYS + (
    'ref', 'direction', 'destination:direction',
    'destination:direction:forward', 'destination:direction:backward',
    'destination:lanes:forward', 'destination:lanes:backward',
    'destination:ref:lanes:forward', 'destination:ref:lanes:backward',
)))
SPACING_M = 5000.
QUERY_MARGIN = signs.QUERY_MARGIN
CARDINALS = dict(N='NORTH', S='SOUTH', E='EAST', W='WEST')


def cardinal(value):
    value = str(value or '').strip().upper()
    for short, full in CARDINALS.items():
        if value in (short, short+'B', full, full+'BOUND'):
            return full
    return ''


def values(value):
    out = []
    for part in str(value or '').split(';'):
        part = ' '.join(''.join(c if c.isprintable() else ' ' for c in part).split())
        if part and part.casefold() not in {p.casefold() for p in out}:
            out.append(part[:96])
    return out


def label(tags, relations=()):
    """One unambiguous destination block for a separated, directed mainline.

    Lane-dependent destinations cannot be collapsed into a through-city claim.
    A common value on all mapped lanes is usable; differing/unknown lane labels
    wait for a dedicated lane-assignment implementation.
    """
    direction = signs._direction(tags)
    if not direction or tags.get('highway') not in signs.MAIN_CLASSES:
        return None
    suffix = ':backward' if direction < 0 else ':forward'
    def get(key):
        return tags.get(key+suffix, tags.get(key, ''))
    def common(key, fallback):
        raw = get(key+':lanes')
        if not raw:
            return values(fallback)
        lanes = [values(v) or values(fallback) for v in str(raw).split('|')]
        return lanes[0] if lanes and all(v == lanes[0] for v in lanes) else None
    names = common('destination', get('destination'))
    refs = common('destination:ref', get('destination:ref'))
    if not names or refs is None or any('|' in n for n in names):
        return None
    # ref describes this road; destination:ref is a fallback when it is unmapped.
    routes = values(tags.get('ref')) or refs
    directions = {cardinal(r.get('direction')) for r in relations
                  if r.get('role', '') not in ('forward', 'backward') or
                  r['role'] == ('forward' if direction > 0 else 'backward')}
    directions.discard('')
    posted = cardinal(get('destination:direction')) or cardinal(tags.get('direction'))
    if not posted and len(directions) == 1:
        posted = next(iter(directions))
    return dict(ref='', names=names[:3], routes=routes[:3], direction=posted)


def annotate_exits(objects, relations=None):
    relations = relations or {}
    for obj in objects:
        extra = obj['ways'][0]
        # main_tags belongs to the way immediately upstream of this exit.
        extra['through_label'] = label(extra.get('main_tags', {}),
                                       relations.get(extra.get('main_id'), ()))


def index_records(main_ways, coords, exits=(), relations=None):
    """Join contiguous equally labelled ways before choosing sparse sign stations.

    Station IDs and placement depend on the source corridor, never tile bounds.
    Only short local paths are retained per sign, keeping road context bounded.
    """
    relations = relations or {}
    edges, incoming, outgoing = [], defaultdict(list), defaultdict(list)
    exit_points = defaultdict(list)
    for obj in exits:
        extra = obj['ways'][0]
        path = np.asarray(extra.get('approach', ()), float)
        scale = [111320.*math.cos(math.radians(float(obj['lat']))), 111320.]
        length = np.linalg.norm(np.diff(path, axis=0)*scale, axis=1).sum() if len(path) > 1 else 0.
        if (extra.get('through_label') and length >= 825. and
                signs.sign_style(extra['label']) == 'exit'):
            for wid in extra.get('main_ids', ()):
                exit_points[wid].append([obj['lon'], obj['lat']])
    for wid, refs, tags in sorted(main_ways, key=lambda row: row[0]):
        if (tags.get('bridge', 'no') not in ('no', 'false', '0', '') or
                tags.get('tunnel', 'no') not in ('no', 'false', '0', '') or
                tags.get('access') in ('private', 'no')):
            continue
        content = label(tags, relations.get(wid, ()))
        if content is None:
            continue
        path = refs[::-1] if signs._direction(tags) < 0 else refs
        signature = json.dumps(content, sort_keys=True)
        for a, b in zip(path[:-1], path[1:]):
            if a == b or a not in coords or b not in coords:
                continue
            i = len(edges)
            edges.append((a, b, wid, content, signature))
            outgoing[a].append(i)
            incoming[b].append(i)

    def next_edge(i):
        b = edges[i][1]
        choices = outgoing[b]
        return choices[0] if (len(choices) == 1 and len(incoming[b]) == 1 and
                              edges[choices[0]][4] == edges[i][4]) else None

    starts = [i for i, e in enumerate(edges)
              if len(incoming[e[0]]) != 1 or next_edge(incoming[e[0]][0]) != i]
    seen, out = set(), []
    # A labelled loop without a natural start still has one deterministic owner.
    for first in starts + list(range(len(edges))):
        if first in seen:
            continue
        chain, i = [], first
        while i is not None and i not in seen:
            seen.add(i)
            chain.append(i)
            i = next_edge(i)
        nodes = [edges[chain[0]][0]] + [edges[i][1] for i in chain]
        path = np.array([coords[n] for n in nodes], float)
        scale = np.array([111320.*math.cos(math.radians(float(path[:, 1].mean()))), 111320.])
        xy = (path-path[0])*scale
        arc = np.r_[0., np.cumsum(np.linalg.norm(np.diff(xy, axis=0), axis=1))]
        if arc[-1] < 60.:
            continue
        for station in np.arange(min(150., arc[-1]/2.), arc[-1]-10., SPACING_M):
            k = min(len(chain)-1, int(np.searchsorted(arc, station, side='right')-1))
            t = (station-arc[k])/max(arc[k+1]-arc[k], 1e-9)
            point = path[k]*(1.-t)+path[k+1]*t
            # Exit approaches receive their paired gantry at the half-mile station.
            # Suppress only the standalone sign on that same directed source way.
            wid = edges[chain[k]][2]
            if any(np.linalg.norm((np.asarray(p)-point)*scale) < 2000. for p in exit_points[wid]):
                continue
            # Keep only 80 m on each side even when a source way has long edges.
            start, end = max(0., station-80.), min(arc[-1], station+80.)
            def at(distance):
                j = min(len(chain)-1, int(np.searchsorted(arc, distance, side='right')-1))
                u = (distance-arc[j])/max(arc[j+1]-arc[j], 1e-9)
                return path[j]*(1.-u)+path[j+1]*u
            local = np.vstack([at(start), path[(arc > start) & (arc < end)], at(end)])
            lo = max(0, int(np.searchsorted(arc, start, side='right')-1))
            hi = min(len(chain), int(np.searchsorted(arc, end, side='left')))
            key = f'{nodes[0]}:{nodes[-1]}:{edges[first][4]}:{station:.3f}'
            nid = -(1 << 61)-int.from_bytes(hashlib.blake2b(key.encode(), digest_size=7).digest(), 'little')
            extra = dict(label=edges[first][3], approach=local.tolist(),
                         point=point.tolist(), main_ids=sorted({edges[j][2] for j in chain[lo:hi]}))
            out.append(dict(id=nid, lon=float(point[0]), lat=float(point[1]), tags={},
                            ways=[extra], kind='through'))
    return out


def source_pavements(frame, obj, context, left_hand=False):
    extra = obj.get('through', obj.get('exit'))
    return context.pavements(frame, np.asarray(extra['approach']), left_hand)


def _candidate(pavements, q, direction, ids):
    choices = []
    for row in pavements:
        fid, sp, line, width = row
        if sp.link or not sp.oneway or sp.cls not in signs.MAIN_CLASSES:
            continue
        wid = signs._way_id(fid)
        if wid is not None and wid not in ids:
            continue
        near = signs._nearest(q, [row])
        if near is None or near[0] > 30.:
            continue
        travel = near[7]*(sp.dir or 1)
        if travel@direction >= .8:
            choices.append((near[0], fid, near, travel, width))
    return min(choices, key=lambda v: (v[0], v[1])) if choices else None


def dimensions(content):
    longest = max(signs.guide_width(n, .70) for n in content['names'])
    header = len(signs.route_symbols(content['routes']))*1.35
    if content.get('direction'):
        header += signs.guide_width(content['direction'],.35)+.3
    return min(10., max(5.8, longest+.9, header+.8)), 3.7+max(0, len(content['names'])-1)*.88


def _assembly(frame, obj, pavements, q, direction, box, elev, chunks, content,
              exit_label=None, side=1, distance='', allow_outside=False):
    import signroads
    extra = obj.get('through', obj.get('exit'))
    found = _candidate(pavements, q, direction, extra['main_ids'])
    if found is None:
        return None
    _, _, near, travel, width = found
    _, _, sp, line, k, t, at_xy, _ = near
    x0, y0, x1, y1 = box
    if not allow_outside and not (x0 <= at_xy[0] < x1 and y0 <= at_xy[1] < y1):
        return None
    if signs._on_deck(at_xy, chunks, 3.):
        return None
    right = np.array([travel[1], -travel[0]])
    cross = signroads.cross(line)
    edge = abs((cross[k]*(1.-t)+cross[k+1]*t)@right)*width/2.
    lat, lon, _ = frame.enu_to_geodetic(float(at_xy[0]), float(at_xy[1]), 0.)
    import signstates
    state = str(signstates.states(lat, lon).item())
    w, h = dimensions(content)
    panels = [dict(label=content, width=w, height=h, offset=0., exit=False)]
    if exit_label:
        ew, eh = signs.dimensions(exit_label, 'advance', state)
        panels[0]['offset'] = -side*(ew+.25)/2.
        panels.append(dict(label=exit_label, width=ew, height=eh,
                           offset=side*(w+.25)/2., exit=True))
        for panel in panels:
            panel['height'] = max(h, eh)
    extent = max(abs(p['offset'])+p['width']/2 for p in panels)
    polygons = [signs._outline(r[2], r[3]) for r in pavements]
    poles = []
    for side_pole in (-1., 1.):
        offset = max(edge+2., extent+1.)*side_pole
        for _ in range(45):
            point = at_xy+right*offset
            if signs._clear(point, .65, polygons) and not signs._on_deck(point, chunks, 1.):
                poles.append((offset, point))
                break
            offset += side_pole
        else:
            return None
    if poles[1][0]-poles[0][0] > 60.:
        return None
    road_z = signs._ground_z(frame, elev, at_xy)
    bases = [signs._ground_z(frame, elev, point) for _, point in poles]
    # Entire underside clears the rendered carriageway; columns stand off pavement.
    bottom = max(road_z+6., max(bases)+.6)
    return dict(id=obj['id'], kind='through', centre=[*map(float, at_xy), road_z],
                travel=travel.tolist(), panels=panels, bottom=bottom, bases=bases,
                poles=[v[0] for v in poles], side=side, distance=distance,
                state=state)


def place(frame, objects, pavements, box, elev, chunks=(), mph=True, allow_outside=False):
    """Choose one station; allow an adjacent tile to check a roadside replacement.

    Unrelated tiles stop before support planning and elevation sampling.
    """
    out, seen = [], set()
    for obj in objects:
        if obj['id'] in seen:
            continue
        seen.add(obj['id'])
        extra = obj.get('through', obj.get('exit'))
        if extra is None:
            continue
        approach = signs._xy(frame, extra['approach'])
        end = signs._station(approach, 1e12)
        if end is None:
            continue
        content = extra.get('label') if 'through' in obj else extra.get('through_label')
        if 'exit' in obj and 'through_label' not in extra:
            content = label(extra.get('main_tags', {}))  # compatible legacy indexes
        if not content:
            continue
        if 'through' in obj:
            q = signs._xy(frame, [extra['point']])[0]
            direction = signs._nearest(q, [('', None, approach, 0.)])[7]
            result = _assembly(frame, obj, pavements, q, direction, box, elev, chunks, content)
        else:
            if signs.sign_style(extra['label']) != 'exit':
                continue
            distance, text = (804.672, '1/2 MILE') if mph else (500., '500 m')
            if end[2] < distance+20.:
                continue
            q, direction, _ = signs._station(approach, end[2]-distance)
            result = _assembly(frame, obj, pavements, q, direction, box, elev, chunks,
                               content, extra['label'], int(extra['side']), text, allow_outside)
        if result:
            out.append(result)
    return out


class Mesh(signs.Mesh):
    def bar(self, a, b, radius=.06):
        """A six-sided metal tube for diagonal lattice braces."""
        a, b = np.asarray(a, float), np.asarray(b, float)
        axis = b-a
        norm = np.linalg.norm(axis)
        if norm < 1e-6:
            return
        axis /= norm
        up = np.array([0., 0., 1.]) if abs(axis[2]) < .9 else np.array([1., 0., 0.])
        u = np.cross(axis, up)
        u /= np.linalg.norm(u)
        v = np.cross(axis, u)
        ring = [radius*(u*math.cos(t)+v*math.sin(t)) for t in np.linspace(0, 2*math.pi, 7)]
        for p, q in zip(ring[:-1], ring[1:]):
            self.quad([a+p, a+q, b+q, b+p], 2)

    def up_arrow(self, y, bottom):
        self.rectangle(.16, bottom, .65, .081, 1, y)
        self.triangle([(.081,y-.34,bottom+.60),(.081,y+.34,bottom+.60),
                       (.081,y,bottom+1.02)], 1)

    def overhead(self, sign):
        self.centre = np.asarray(sign['centre'], float)
        travel = np.asarray(sign['travel'], float)
        self.face = np.r_[-travel, 0.]
        self.right = np.array([travel[1], -travel[0], 0.])
        bottom = sign['bottom']-self.centre[2]
        top = bottom+max(p['height'] for p in sign['panels'])
        beam = top-.55
        left, right = sign['poles']
        for y, base in zip(sign['poles'], sign['bases']):
            self.box((-.85,y-.18,base-self.centre[2]-.15),(-.49,y+.18,beam+.4),2)
        for x in (-.55, -1.15):
            for z in (beam-.4, beam+.4):
                self.bar((x,left,z),(x,right,z),.09)
        stations = np.linspace(left, right, max(2, int(math.ceil((right-left)/1.8))+1))
        for a, b in zip(stations[:-1], stations[1:]):
            for x in (-.55, -1.15):
                self.bar((x,a,beam-.4),(x,b,beam+.4))
                self.bar((x,a,beam+.4),(x,b,beam-.4))
            self.bar((-.55,a,beam+.4),(-1.15,a,beam+.4))
        self.bar((-.55,right,beam+.4),(-1.15,right,beam+.4))

        anchor = self.centre.copy()
        for panel in sign['panels']:
            self.centre = anchor+self.right*panel['offset']
            w, h, content = panel['width'], panel['height'], panel['label']
            if panel['exit']:
                # Reuse the established exit panel and tab layout without its posts.
                self.rounded_panel(w,bottom,h,0)
                if content['ref']:
                    tw = min(w, max(2.2, signs.guide_width('EXIT '+content['ref'], .36)+.5))
                    ty = sign['side']*(w-tw)/2
                    self.rounded_panel(tw,bottom+h,.66,0,ty)
                    self.guide_text('EXIT '+content['ref'],ty,bottom+h+.16,.36,tw-.3)
                layout = signs.advance_layout(dict(label=content, width=w, height=h,
                    side=sign['side'], kind='advance', distance=sign['distance'], state=sign['state']))
                for row in layout['lines']:
                    self.guide_text(row['value'],row['y'],bottom+row['baseline'],row['height'],row['max_width'])
                for row in layout.get('directions', ()):
                    self.guide_text(row['value'],row['y'],bottom+row['baseline'],row['height'],row['max_width'])
                for route in layout['routes']:
                    self.route_shield(route,bottom)
                self.distance_text(layout['distance'],bottom,signs.portrait(sign['state']))
            else:
                self.rounded_panel(w,bottom,h,0)
                routes = signs.route_symbols(content['routes'], sign['state'])
                posted = content.get('direction', '')
                block_width = len(routes)*1.35+(signs.guide_width(posted,.35)+.3 if posted else 0.)
                cursor = -block_width/2.
                for route in routes:
                    self.route_shield(dict(**route, y=cursor+.6, bottom=bottom+h-1.1,
                                           width=1.2, height=.95))
                    cursor += 1.35
                if posted:
                    self.guide_text(posted,cursor+(block_width/2-cursor)/2,
                                    bottom+h-.76,.35,max(.1,block_width/2-cursor))
                for i, name in enumerate(content['names']):
                    self.guide_text(name,0.,bottom+h-2.-i*.88,.70,w-.8)
                self.up_arrow(0.,bottom+.25)
        self.centre = anchor


def build(exits, overheads, origin):
    mesh = Mesh(origin)
    for sign in exits:
        mesh.panel(sign)
    for sign in overheads:
        mesh.overhead(sign)
    return mesh.result()
