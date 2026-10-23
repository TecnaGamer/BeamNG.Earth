"""OSM exit destinations on generated, traffic-facing highway guide signs.

The background point importer keeps short connected approaches from the PBF.
Road workers project sign stations onto the generated carriageway and batch all
panels, posts and atlas lettering in one render-only mesh per road tile.
"""
from collections import defaultdict
from functools import lru_cache
import json
import math
from pathlib import Path
import re
import unicodedata

import numpy as np

MAIN_CLASSES = ('motorway', 'trunk')
LINK_CLASSES = ('motorway_link', 'trunk_link')
APPROACH_M = 1800.
QUERY_MARGIN = 1900.
ADVANCE_MPH = 402.336
ADVANCE_METRIC = 500.
ADVANCE_MPH_STATIONS = ((1609.344, '1 MILE'), (804.672, '1/2 MILE'), (ADVANCE_MPH, '1/4 MILE'))
GORE_SEARCH_M = 300.
GORE_CLEAR_M = 1.
EXIT_KEYS = ('ref', 'name', 'noref', 'exit_to', 'junction:ref', 'destination',
             'destination:street', 'destination:ref', 'destination:ref:to',
             'destination:forward', 'destination:backward', 'destination:street:forward',
             'destination:street:backward', 'destination:ref:forward', 'destination:ref:backward',
             'destination:lanes', 'destination:ref:lanes')
WAY_KEYS = EXIT_KEYS + ('highway', 'oneway', 'lanes', 'lanes:forward', 'lanes:backward',
                       'width', 'bridge', 'layer', 'tunnel', 'access', 'service')
MATERIALS = ['bne_exit_green', 'bne_exit_white', 'bne_exit_metal', 'bne_exit_letters', 'bne_exit_blue']


def sign_style(label):
    """Rest-area destinations have service signs, not a guessed exit number."""
    names = label['names']
    rest = re.compile(r'(?:.*\s)?(?:rest[\s_-]+(?:area|stop)|welcome[\s_-]+cent(?:er|re))(?:\s+\d+)?', re.I)
    return 'rest_area' if names and not label['ref'] and all(rest.fullmatch(n) for n in names) else 'exit'


def labels(node, ramp, backwards=False):
    """Keep real posted labels; an interchange name is a last resort, never a city guess."""
    suffix = ':backward' if backwards else ':forward'
    def get(key):
        return ramp.get(key+suffix, ramp.get(key, ''))
    def values(text):
        out = []
        for part in re.split(r'[;|]', str(text or '')):
            part = ' '.join(''.join(c if c.isprintable() else ' ' for c in part).split())
            if part and part.casefold() not in {p.casefold() for p in out}:
                out.append(part[:96])
        return out
    ref = ' '.join(str(ramp.get('junction:ref', node.get('ref', node.get('junction:ref', '')))).split())[:24]
    if str(node.get('noref', '')).lower() == 'yes' and not ramp.get('junction:ref'):
        ref = ''
    names = values(get('destination')) or values(get('destination:street'))
    names = names or values(get('destination:lanes')) or values(node.get('exit_to')) or values(node.get('name'))
    routes = values(get('destination:ref')) or values(get('destination:ref:to')) or values(get('destination:ref:lanes'))
    return dict(ref=ref, names=names[:3], routes=routes[:3]) if ref or names or routes else None


def _direction(tags):
    value = tags.get('oneway', '')
    if value in ('-1', 'reverse'): return -1
    if value in ('no', '0', 'false'): return 0
    return 1 if value in ('yes', '1', 'true') or tags.get('highway') in ('motorway', 'motorway_link') else 0


def index_records(main_ways, links, coords, node_tags):
    """Join shared OSM nodes, excluding entries and private median/service connections.

    Coordinates are lon/lat. Only motorway/trunk vertices are retained by the
    caller, not the regional PBF's complete node population. Paths stop at an
    ambiguous upstream fork instead of jumping onto another carriageway.
    """
    incoming = defaultdict(list)
    for wid, refs, tags in main_ways:
        directions = (1, -1) if _direction(tags) == 0 else (_direction(tags),)
        for direction in directions:
            path = refs if direction == 1 else refs[::-1]
            for a, b in zip(path[:-1], path[1:]):
                if a in coords and b in coords and a != b:
                    incoming[b].append((a, wid, tags))
    def delta(a, b):
        p, q = np.asarray(coords[a]), np.asarray(coords[b])
        return (q-p)*[111320.*math.cos(math.radians(float((p[1]+q[1])/2))), 111320.]
    def unit(d): return d/max(float(np.linalg.norm(d)), 1e-9)
    out = []
    for rid, source, tags in links:
        if len(source) < 2 or tags.get('access') in ('private', 'no') or tags.get('service') == 'emergency_access':
            continue
        backwards = _direction(tags) == -1
        refs = source[::-1] if backwards else source
        nid = refs[0]
        if nid not in incoming or nid not in coords:
            continue                       # the other end is an on-ramp, not an exit
        label = labels(node_tags.get(nid, {}), tags, backwards)
        if label is None:
            continue
        ramp = [n for n in refs if n in coords]
        if len(ramp) < 2:
            continue
        rd = unit(delta(ramp[0], ramp[min(2, len(ramp)-1)]))
        candidates = [(float(unit(delta(a, nid))@rd), a, wid, wt) for a, wid, wt in incoming[nid]]
        candidates.sort(key=lambda c: (-c[0], c[2], c[1]))
        if not candidates or candidates[0][0] < .15:
            continue
        _, previous, mid, mtags = candidates[0]
        approach, visited, length = [nid, previous], {nid, previous}, float(np.linalg.norm(delta(previous, nid)))
        main_ids = {mid}
        while length < APPROACH_M:
            at, after = approach[-1], approach[-2]
            d = unit(delta(at, after))
            choices = [(float(unit(delta(a, at))@d), a, wid) for a, wid, _ in incoming[at]
                       if a not in visited]
            choices.sort(key=lambda c: (-c[0], c[2], c[1]))
            if not choices or choices[0][0] < .65 or (len(choices) > 1 and choices[0][0]-choices[1][0] < .12):
                break
            _, a, wid = choices[0]
            length += float(np.linalg.norm(delta(a, at)))
            approach.append(a); visited.add(a); main_ids.add(wid)
        # Retain enough of a split ramp to determine left/right even at a parallel tip.
        main_travel = unit(delta(previous, nid))
        right = np.array([main_travel[1], -main_travel[0]])
        offsets, run = [], 0.
        for a, b in zip(ramp[:-1], ramp[1:]):
            step = float(np.linalg.norm(delta(a, b)))
            if run+step > 150.:
                tip = delta(nid, a)+delta(a, b)*(150.-run)/max(step,1e-9)
                offsets.append(float(tip@right)); break
            offsets.append(float(delta(nid, b)@right)); run += step
        # Use the first clear separation, before a loop ramp can turn across the highway.
        offset = next((v for v in offsets if abs(v) > 1.), max(offsets,key=abs,default=1.))
        side = 1 if offset >= 0 else -1
        extra = dict(label=label, approach=[coords[n] for n in approach[::-1]],
                     ramp=[coords[n] for n in ramp], main_ids=sorted(main_ids), ramp_id=rid,
                     main_tags=mtags, ramp_tags=tags, side=side)
        # One row per outgoing ramp, including separately labelled branches at one junction.
        lo, la = coords[nid]
        out.append(dict(id=-rid, lon=lo, lat=la, tags=node_tags.get(nid, {}), ways=[extra], kind='exit'))
    return out


def _xy(frame, lonlat):
    p = np.asarray(lonlat, float)
    e, n, _ = frame.geodetic_to_enu(p[:, 1], p[:, 0], 0.)
    return np.c_[e, n]


def _station(path, distance):
    ab = np.diff(path[:, :2], axis=0)
    lengths = np.linalg.norm(ab, axis=1)
    arc = np.r_[0., np.cumsum(lengths)]
    if len(arc) < 2 or arc[-1] < 1e-3:
        return None
    s = float(np.clip(distance, 0., arc[-1]))
    k = min(len(ab)-1, int(np.searchsorted(arc, s, side='right')-1))
    t = (s-arc[k])/max(lengths[k], 1e-9)
    return path[k]+t*(path[k+1]-path[k]), ab[k]/max(lengths[k], 1e-9), float(arc[-1])


def _way_id(fid):
    match = re.search(r'^osm:w(\d+):', str(fid))
    return int(match[1]) if match else None


def gore_pavements(frame, objects, specs, sections, ramp_mains, auxiliary, left_hand=False):
    """Full source ribbons, before tile filtering, keep gore placement identical across tiles.

    Reuse the renderer's joined sections, lane-width profiles and trimmed auxiliary tails;
    a raw OSM centreline alone does not describe the pavement around the split.
    """
    import roadlanes
    import roads
    exits = [o['exit'] for o in objects if o.get('exit')]
    if not exits:
        return []
    ramp_ids = {e['ramp_id'] for e in exits}
    main_ids = {i for e in exits for i in e['main_ids']}
    ramps = {fid for fid, sp in specs.items() if sp.link and sp.cls in MAIN_CLASSES
             and (_way_id(fid) is None or _way_id(fid) in ramp_ids)}
    mains = {fid for fid, sp in specs.items() if not sp.link and sp.cls in MAIN_CLASSES
             and (_way_id(fid) is None or _way_id(fid) in main_ids)}
    mains.update(mid for fid in ramps for mid in ramp_mains.get(fid, ()) if mid in specs)
    ribbons = []
    for fid in sorted(mains | ramps):
        sp = specs[fid]
        for ref in sections.get('_uv', {}).get(fid, ()):
            line, width = roadlanes.pavement(sp, roads._with_cross(ref.points, sections), frame, left_hand)
            ribbons.append((fid, sp, line, width))
    def connected(fid):
        return [(line, width/2) for mid, sp, line, width in ribbons
                if not sp.link and mid in ramp_mains.get(fid, ())]
    out = [(fid, sp, roads._outside_mainline(line, width, connected(fid)) if sp.link else line, width)
           for fid, sp, line, width in ribbons]
    for band in auxiliary:
        fid = band['fid']
        if fid not in ramps:
            continue
        p = band['pts']
        width = float(max(band['widths']))
        cross = roadlanes.cross(p)*(band['widths']/width)[:, None]
        line = roads._outside_mainline(np.c_[p[:, :3], cross], width, connected(fid))
        out.append((fid, specs[fid], line, width))
    return out


def _outline(line, width):
    import roadlanes
    offset = roadlanes.cross(line)*width/2
    a, b = line[:, :2]+offset, line[:, :2]-offset
    return np.vstack([a, b[::-1], a[:1]])


def source_pavements(frame, obj, context, left_hand=False):
    """The same complete exit pavement in every rendering tile, including its through road.

    An exit's source path fixes the queried cells. Other exits returned by a tile's wider
    query cannot change this exit's first clear gore. Cache the flat ribbons per anchor;
    elevation is only sampled after a sign has been claimed by its owning tile.
    """
    import roads
    extra = obj['exit']
    feats = context.around(np.vstack([extra['approach'], extra['ramp']]))
    memo = feats.__dict__.setdefault('_exit_pavements', {})
    key = (frame.lat0, frame.lon0, frame.h0, bool(left_hand), obj['id'])
    if key not in memo:
        specs = {str(feats.ids[i]): sp for i, sp in enumerate(roads.road_specs(feats, left_hand=left_hand))
                 if sp is not None}
        sections = roads._join_sections(frame, [feats])
        _, _, mains, auxiliary = roads.projected_ramp_layout(frame, [feats], left_hand)
        ribbons = gore_pavements(frame, [obj], specs, sections, mains, auxiliary, left_hand)
        if len(memo) >= 16:
            memo.pop(next(iter(memo)))
        memo[key] = ribbons, mains
    return memo[key]


def _clear(centre, radius, polygons):
    """A conservative disc keeps the entire panel and both posts clear of pavement."""
    for poly in polygons:
        if np.any(centre+radius < poly.min(0)) or np.any(centre-radius > poly.max(0)):
            continue
        a, b = poly[:-1]-centre, poly[1:]-centre
        ab = b-a
        t = np.clip(-(a*ab).sum(1)/np.maximum((ab*ab).sum(1), 1e-12), 0., 1.)
        if np.min(np.linalg.norm(a+t[:, None]*ab, axis=1)) < radius:
            return False
        crosses = (a[:, 1] > 0.) != (b[:, 1] > 0.)
        hit = a[crosses, 0]-a[crosses, 1]*ab[crosses, 0]/ab[crosses, 1]
        if np.count_nonzero(hit > 0.) % 2:
            return False
    return True


def _spans(poly, at, travel, outward):
    """Paved intervals along a highway cross-section, including curved/looping ribbons."""
    a, b = poly[:-1]-at, poly[1:]-at
    da, db = a@travel, b@travel
    cuts = (da > 0.) != (db > 0.)
    hit = a[cuts]+(b-a)[cuts]*(-da[cuts]/(db-da)[cuts])[:, None]
    lateral = np.sort(hit@outward)
    return list(zip(lateral[::2], lateral[1::2]))


def _gore(extra, approach, ramp, pavements, ramp_mains, width_panel):
    import roadlanes
    ramp_rows = [r for r in pavements if r[1].link and _way_id(r[0]) == extra['ramp_id']]
    if not ramp_rows:
        # The optional Overture provider has no OSM way IDs. Retain the existing spatial
        # association, requiring both proximity and the outgoing ramp's direction.
        q, direction, _ = _station(ramp, min(45., _station(ramp, 1e12)[2]*.35))
        choices = []
        for fid, sp, line, _ in pavements:
            if not sp.link or sp.cls not in MAIN_CLASSES or _way_id(fid) is not None:
                continue
            near = _nearest(q, [(fid, sp, line, 0.)])
            if near[0] <= 35. and near[7]*(sp.dir or 1)@direction >= .5:
                choices.append((near[0], fid))
        if choices:
            fid = min(choices)[1]
            ramp_rows = [r for r in pavements if r[0] == fid]
    connected = {mid for r in ramp_rows for mid in ramp_mains.get(r[0], ())}
    main_rows = [r for r in pavements if not r[1].link and r[1].cls in MAIN_CLASSES
                 and (_way_id(r[0]) in extra['main_ids'] or r[0] in connected
                      or _way_id(r[0]) is None and not connected)]
    if not ramp_rows or not main_rows:
        return None
    ramp_polys = [_outline(r[2], r[3]) for r in ramp_rows]
    polygons = ramp_polys+[_outline(r[2], r[3]) for r in main_rows]
    direction = _station(approach, 1e12)[1]
    length = _station(ramp, 1e12)[2]
    radius = width_panel/2+GORE_CLEAR_M
    for distance in np.arange(8., min(GORE_SEARCH_M, length)+.01, 2.):
        q = _station(ramp, distance)[0]
        choices = []
        for fid, sp, line, width in main_rows:
            near = _nearest(q, [(fid, sp, line, 0.)])
            travel = near[7]*(sp.dir or (1 if near[7]@direction >= 0. else -1))
            if travel@direction < .5 or abs((q-near[6])@travel) > 2.:
                continue                 # no extrapolation beyond a missing through-road segment
            choices.append((near[0], fid, near, width, travel))
        if not choices:
            continue
        _, _, near, width, travel = min(choices, key=lambda c: (c[0], c[1]))
        _, _, sp, line, k, t, _, _ = near
        at = line[k]*(1.-t)+line[k+1]*t
        outward = np.array([travel[1], -travel[0]])*extra['side']
        cross = roadlanes.cross(line)
        edge = abs((cross[k]*(1.-t)+cross[k+1]*t)@outward)*width/2
        spans = [span for poly in ramp_polys for span in _spans(poly, at[:2], travel, outward)
                 if span[1] > edge]
        if not spans:
            continue
        inner = min(span[0] for span in spans)
        if inner-edge < 2.*radius:
            continue
        centre = at[:2]+outward*(edge+inner)/2
        if _clear(centre, radius, polygons):
            return sp, at, travel, centre
    return None                          # a narrow/unsuitable gore never falls back onto the ramp shoulder


def _nearest(q, roads):
    """Project q onto the closest generated road centreline."""
    best = None
    for fid, spec, line, lon in roads:
        line = np.asarray(line, float)
        if len(line) < 2:
            continue
        ab = np.diff(line[:, :2], axis=0)
        l2 = np.maximum((ab * ab).sum(1), 1e-12)
        t = np.clip(((q - line[:-1, :2]) * ab).sum(1) / l2, 0., 1.)
        at = line[:-1, :2] + t[:, None] * ab
        dist = np.linalg.norm(at - q, axis=1)
        k = int(dist.argmin())
        candidate = (float(dist[k]), fid, spec, line, k, float(t[k]), at[k], ab[k] / math.sqrt(l2[k]))
        if best is None or candidate[0] < best[0]:
            best = candidate
    return best


def _travel(near, direction):
    tangent = near[7]
    return tangent if float(tangent @ direction) >= 0. else -tangent


def _ground_z(frame, elev, point):
    la, lo, _ = frame.enu_to_geodetic(float(point[0]), float(point[1]), 0.)
    h = float(np.asarray(elev(np.array([la]), np.array([lo]))).ravel()[0])
    return float(frame.geodetic_to_enu(la, lo, h)[2])


def _on_deck(point, chunks, radius):
    for chunk in chunks:
        if not chunk.get('deck') or len(chunk.get('n', ())) < 2:
            continue
        line = np.asarray(chunk['n'], float)
        ab = np.diff(line[:, :2], axis=0)
        l2 = np.maximum((ab * ab).sum(1), 1e-12)
        t = np.clip(((point - line[:-1, :2]) * ab).sum(1) / l2, 0., 1.)
        d = np.linalg.norm(line[:-1, :2] + t[:, None] * ab - point, axis=1)
        if float(d.min()) < float(chunk.get('w', 0.)) / 2 + radius:
            return True
    return False


def _make_sign(obj, kind, centre, travel, road_point, frame, elev, side, width, height,
               distance_text='', distance_m=0.):
    right = np.array([travel[1], -travel[0]])
    posts = np.array([centre - right * width * .32, centre + right * width * .32])
    bases = np.array([_ground_z(frame, elev, p) for p in posts])
    z = _ground_z(frame, elev, centre)
    bottom = max(float(bases.max()) + 2.4, float(road_point[2]) + 2.2)
    label = obj['exit']['label']
    return dict(id=obj['id'], kind=kind, label=label, style=sign_style(label),
                centre=[float(centre[0]), float(centre[1]), z], travel=travel.tolist(), side=side,
                bottom=bottom, width=width, height=height, bases=bases.tolist(),
                distance=distance_text, advance_m=distance_m)


def place(frame, objects, roads, box, elev, chunks=(), left_hand=False, mph=True, link_ids=(),
          pavements=None, ramp_mains=None):
    """Place OSM destination signs along the generated mainline and in clear exit gores.

    Choose one clear gore on the full generated pavement, then test tile ownership.
    Searching farther along the ramp for each tile produces duplicate small panels.
    """
    import roadlanes
    x0, y0, x1, y1 = box
    links = set(link_ids)
    if pavements is None:
        import roads as roadsmod
        pavements = []
        for fid, sp, line, _ in roads:
            sp = sp._replace(link=sp.link or fid in links or sp.cls in LINK_CLASSES)
            p, width = roadlanes.pavement(sp, roadsmod._with_cross(line, None), frame, left_hand)
            pavements.append((fid, sp, p, width))
    main = [(fid, sp, line, width) for fid, sp, line, width in pavements
            if sp.cls in MAIN_CLASSES and not sp.link]
    ramp_mains = ramp_mains or {}
    out, seen = [], set()

    def nearest(rows, q, direction, limit):
        found = []
        for row in rows:
            near = _nearest(q, [row])
            if near is None or near[0] > limit:
                continue
            spec = near[2]
            travel = near[7] * (spec.dir or 1) if spec.oneway else _travel(near, direction)
            if float(travel @ direction) < .45:
                continue
            found.append((near[0], near, travel, row[3]))
        return min(found, key=lambda x: (x[0], x[1][1])) if found else None

    for obj in objects:
        extra = obj.get('exit')
        if not extra or obj['id'] in seen:
            continue
        seen.add(obj['id'])
        approach, ramp = _xy(frame, extra['approach']), _xy(frame, extra['ramp'])
        full, ramp_end = _station(approach, 1e12), _station(ramp, 1e12)
        if full is None or ramp_end is None:
            continue
        label, side = extra['label'], int(extra['side'])
        for distance, text in (ADVANCE_MPH_STATIONS if mph else ((ADVANCE_METRIC, '500 m'),)):
            if full[2] < distance + 20.:
                continue
            q, direction, _ = _station(approach, full[2] - distance)
            candidate = nearest(main, q, direction, 40.)
            if candidate is None:
                continue
            _, near, travel, paved_width = candidate
            _, _, spec, line, k, t, at_xy, _ = near
            at = line[k] * (1. - t) + line[k + 1] * t
            width, height = dimensions(label, 'advance')
            right = np.array([travel[1], -travel[0]])
            cross = roadlanes.cross(line)
            # The fourth row value is its generated ribbon width. Its cross-section can
            # vary through lane-count changes, rather than being the native Spec width.
            edge = abs((cross[k]*(1.-t)+cross[k+1]*t)@right)*paved_width/2.
            centre = at[:2] + right * side * (edge + 2. + width / 2.)
            if not (x0 <= centre[0] < x1 and y0 <= centre[1] < y1) or _on_deck(at[:2], chunks, 2.):
                continue
            out.append(_make_sign(obj, 'advance', centre, travel, at, frame, elev, side, width, height, text, distance))

        width, height = dimensions(label, 'ramp')
        candidate = _gore(extra, approach, ramp, pavements, ramp_mains, width)
        if candidate is None:
            continue
        _, at, travel, centre = candidate
        if not (x0 <= centre[0] < x1 and y0 <= centre[1] < y1):
            continue
        if not _on_deck(centre, chunks, width/2.+2.):
            out.append(_make_sign(obj, 'ramp', centre, travel, at, frame, elev, side, width, height))
    return out


@lru_cache(maxsize=1)
def lettering():
    with open(Path(__file__).with_name('exit_glyphs.json'), encoding='utf-8') as f:
        return json.load(f)


def _text(value):
    glyphs = lettering()['glyphs']
    replacements = {'’': "'", '‘': "'", '–': '-', '—': '-', '…': '...'}
    return ''.join(c if c in glyphs else replacements.get(c, ''.join(
        p for p in unicodedata.normalize('NFKD', c) if p in glyphs) or '?') for c in str(value))


def text_width(value, height):
    data = lettering()
    return sum(data['glyphs'][c]['advance'] for c in _text(value))*height/data['capHeight']


def dimensions(label, kind):
    if sign_style(label) == 'rest_area':
        return (2.25, 3.05) if kind == 'ramp' else (3.0, 3.6)
    if kind == 'ramp':
        return max(2.3, min(4.3, text_width(label['ref'], .76)+1.1)), 2.15
    lines = label['names'] or ([] if label['routes'] else ['EXIT '+label['ref']])
    longest = max([text_width(s, .55) for s in lines]+[text_width(' / '.join(label['routes']), .42), 3.4])
    return min(8., max(4.8, longest+1.0)), 1.65+.78*max(1, len(lines))


class Mesh:
    def __init__(self, origin):
        self.origin = np.asarray(origin)
        self.p, self.t, self.uv, self.mi = [], [], [], []
        self.centre = self.face = self.right = None

    def quad(self, points, material, uv=None):
        p = np.asarray(points, float)
        world = self.centre+p[:, :1]*self.face+p[:, 1:2]*self.right+np.c_[np.zeros((len(p), 2)), p[:, 2]]
        i = len(self.p)
        self.p.extend(world-self.origin)
        self.t.extend([(i, i+1, i+2), (i, i+2, i+3)])
        self.mi.extend([material]*2)
        self.uv.extend(uv if uv is not None else [(0., 0.), (1., 0.), (1., 1.), (0., 1.)])

    def box(self, lo, hi, material):
        x, y, z = lo; X, Y, Z = hi
        for p in ([(x,y,z),(x,y,Z),(x,Y,Z),(x,Y,z)], [(X,y,z),(X,Y,z),(X,Y,Z),(X,y,Z)],
                  [(x,y,z),(X,y,z),(X,y,Z),(x,y,Z)], [(x,Y,z),(x,Y,Z),(X,Y,Z),(X,Y,z)],
                  [(x,y,z),(x,Y,z),(X,Y,z),(X,y,z)], [(x,y,Z),(X,y,Z),(X,Y,Z),(x,Y,Z)]):
            self.quad(p, material)

    def rectangle(self, width, bottom, height, depth, material, offset=0.):
        self.quad([(depth, offset-width/2, bottom), (depth, offset+width/2, bottom),
                   (depth, offset+width/2, bottom+height), (depth, offset-width/2, bottom+height)], material)

    def triangle(self, points, material):
        p = np.asarray(points,float)
        index = len(self.p)
        self.p.extend(self.centre+p[:,:1]*self.face+p[:,1:2]*self.right+
                      np.c_[np.zeros((3,2)),p[:,2]]-self.origin)
        self.t.append((index,index+1,index+2));self.mi.append(material);self.uv.extend([(0.,0.)]*3)

    @staticmethod
    def outline(width, bottom, height, radius, offset=0.):
        outline = []
        for y,z,start in ((width/2-radius,bottom+radius,-90), (width/2-radius,bottom+height-radius,0),
                          (-width/2+radius,bottom+height-radius,90), (-width/2+radius,bottom+radius,180)):
            for angle in np.linspace(start,start+90.,7):
                a = math.radians(angle)
                outline.append((offset+y+radius*math.cos(a),z+radius*math.sin(a)))
        return np.asarray(outline)

    def rounded(self, width, bottom, height, radius, depth, material, back=False, front=True, offset=0.):
        """An actual rounded contour, including the metal backing and its thin sidewall."""
        ring = self.outline(width,bottom,height,radius,offset)
        if front:
            for i in range(1,len(ring)-1):
                p = ring[[0,i,i+1]]
                self.triangle(np.c_[np.full(3,depth),p],material)
        if back:
            # Double-sided backing with a real edge; no square box behind the rounded face.
            for a,b in zip(ring,np.roll(ring,-1,axis=0)):
                self.quad([(-.04,*a),(-.04,*b),(depth,*b),(depth,*a)],material)
            for i in range(1,len(ring)-1):
                a,b,c = ring[[0,i,i+1]]
                self.triangle([(-.04,*a),(-.04,*c),(-.04,*b)],material)

    def rounded_panel(self, width, bottom, height, material, offset=0.):
        # One tessellated face, not white/green sheets 5 mm apart. At road-view
        # distances their depth values coincided and the buried white sheet won,
        # making the complete sign appear white until the camera was close.
        self.rounded(width,bottom,height,.16,.05,2,back=True,front=False,offset=offset)
        rings = [self.outline(width-2*m,bottom+m,height-2*m,r,offset)
                 for m,r in ((0.,.16),(.06,.12),(.12,.08))]
        for outer,inner,colour in zip(rings,rings[1:],(material,1)):
            for i in range(len(outer)):
                j=(i+1)%len(outer)
                self.quad([(.05,*outer[i]),(.05,*outer[j]),(.05,*inner[j]),(.05,*inner[i])],colour)
        inner=rings[-1]
        for i in range(1,len(inner)-1):
            self.triangle(np.c_[np.full(3,.05),inner[[0,i,i+1]]],material)

    def text(self, value, y, baseline, height, max_width):
        value = _text(value)
        data = lettering()
        scale = min(height/data['capHeight'], max_width/max(text_width(value, data['capHeight']), 1e-6))
        cursor = y-sum(data['glyphs'][c]['advance'] for c in value)*scale/2
        for c in value:
            glyph = data['glyphs'][c]
            l, t, r, b = glyph['bbox']
            u0, v0, u1, v1 = glyph['uv']
            if r > l and b > t:
                self.quad([(.074,cursor+l*scale,baseline-b*scale), (.074,cursor+r*scale,baseline-b*scale),
                           (.074,cursor+r*scale,baseline-t*scale), (.074,cursor+l*scale,baseline-t*scale)],
                          3, [(u0,v0),(u1,v0),(u1,v1),(u0,v1)])
            cursor += glyph['advance']*scale

    def arrow(self, y, bottom, left, size=.75):
        # Diagonal shoulder arrow, built as opaque white geometry (no glyph dependency).
        direction = np.array([-1. if left else 1., 1.])/math.sqrt(2)
        cross = np.array([-direction[1], direction[0]])
        a, b = np.array([0., .28])-direction*.38, np.array([0., .28])+direction*.2
        shaft = np.array([a-cross*.11, a+cross*.11, b+cross*.11, b-cross*.11])*size
        def area(p):
            a, b = p[1]-p[0], p[2]-p[0]
            return a[0]*b[1]-a[1]*b[0]
        if area(shaft) < 0: shaft = shaft[::-1]
        self.quad([(.075,y+u,bottom+v) for u,v in shaft], 1)
        head = np.array([b-cross*.29, b+cross*.29, b+direction*.43])*size
        if area(head) < 0: head = head[::-1]
        i = len(self.p)
        p = np.array([(.075,y+u,bottom+v) for u,v in head])
        self.p.extend(self.centre+p[:, :1]*self.face+p[:, 1:2]*self.right+np.c_[np.zeros((3, 2)), p[:, 2]]-self.origin)
        self.t.append((i,i+1,i+2)); self.mi.append(1); self.uv.extend([(0.,0.),(1.,0.),(.5,1.)])

    def panel(self, sign):
        self.centre = np.asarray(sign['centre'], float)
        travel = np.asarray(sign['travel'], float)
        self.face = np.r_[-travel, 0.]
        self.right = np.array([travel[1], -travel[0], 0.])
        width, height = sign['width'], sign['height']
        bottom = sign['bottom']-self.centre[2]
        label = sign['label']
        for y, base in zip((-width*.32, width*.32),sign.get('bases',[self.centre[2]]*2)):
            self.box((-.12,y-.07,base-self.centre[2]-.2),(.02,y+.07,bottom+height-.1),2)
        side = 1 if sign['side'] > 0 else -1
        if sign.get('style',sign_style(label)) == 'rest_area':
            self.rounded_panel(width,bottom,height,4)
            welcome = all('welcome' in n.casefold() for n in label['names'])
            first,second = ('WELCOME','CENTER') if welcome else ('REST','AREA')
            self.text(first,0.,bottom+height-.83,.53,width-.4)
            self.text(second,0.,bottom+height-1.62,.53,width-.4)
            if sign['kind'] == 'ramp':
                self.arrow(0.,bottom+.34,side<0,1.25)
            else:
                self.text(sign['distance'],-.3*side,bottom+.36,.38,width-1.35)
                self.arrow((width/2-.62)*side,bottom+.52,side<0,.75)
            return
        if sign['kind'] == 'ramp':
            self.rounded_panel(width,bottom,height,0)
            self.text('EXIT',0.,bottom+1.47,.43,width-.4)
            if label['ref']:
                self.text(label['ref'],-.39*side,bottom+.35,.76,width-1.1)
                self.arrow((width/2-.64)*side,bottom+.43,side<0,.75)
            else:
                self.arrow(0.,bottom+.43,side<0,1.1)
            return
        self.rounded_panel(width,bottom,height,0)
        top = bottom+height
        if label['ref']:
            tab_width = min(width, max(2.0, text_width('EXIT '+label['ref'], .34)+.45))
            tab_y = (width-tab_width)/2*(1 if sign['side'] > 0 else -1)
            self.rounded_panel(tab_width,top,.66,0,tab_y)
            self.text('EXIT '+label['ref'], tab_y, top+.17, .34, tab_width-.3)
        cursor = top-.7
        if label['routes']:
            self.text(' / '.join(label['routes']), 0., cursor, .42, width-.6)
            cursor -= .7
        lines = label['names'] or ([] if label['routes'] else ['EXIT '+label['ref']])
        for line in lines:
            self.text(line, 0., cursor, .55, width-.6)
            cursor -= .78
        self.text(sign['distance'], 0., bottom+.32, .37, width-1.7)
        self.arrow(width/2-.7 if sign['side'] > 0 else -width/2+.7, bottom+.32, sign['side'] < 0)

    def result(self):
        if not self.t: return None
        return np.asarray(self.p), np.asarray(self.t), np.asarray(self.uv), MATERIALS, np.asarray(self.mi)


def build(signs, origin):
    mesh = Mesh(origin)
    for sign in signs:
        mesh.panel(sign)
    return mesh.result()
