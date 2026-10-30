"""OSM exit destinations on generated, traffic-facing highway guide signs (from cody196's PR #363).

The weekly sign shards (tools/build_sign_tags.py, read by railtags.SignTags) keep short connected approaches from
the planet. Sign workers project sign stations onto the generated carriageway and batch all panels, posts and atlas
lettering in one render-only mesh per road tile. Lettering: the Roadgeek 2014 Series EEM atlas
(tools/build_sign_glyphs.py, sign_glyphs.json).
"""
from collections import defaultdict
from functools import lru_cache
import json
import math
from pathlib import Path
import re
import unicodedata

import numpy as np
import routeshields

MAIN_CLASSES = ('motorway', 'trunk')
LINK_CLASSES = ('motorway_link', 'trunk_link')
APPROACH_M = 1800.
QUERY_MARGIN = 1900.
ADVANCE_MPH = 402.336
ADVANCE_METRIC = 500.
DECISION_M = 35.                 # destination/arrow panel immediately before the exit entrance
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
GUIDE_MATERIALS = MATERIALS + ['bne_guide_red', 'bne_guide_black', 'bne_guide_letters', 'bne_guide_letters_black']
STATE_CODES = set('AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC'.split())


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
                     main_id=mid, main_tags=mtags, ramp_tags=tags, side=side)
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


def _outline(line, width):
    import signroads
    return signroads.polygon(line, width)


def _outlines(pavements):
    """The pavement outlines of pavement rows: kept by a signroads.Pavements, else made."""
    got = getattr(pavements, 'outlines', None)
    if got is not None and len(got) == len(pavements):
        return got
    import signroads
    return signroads.Outlines([_outline(r[2], r[3]) for r in pavements])


def source_pavements(frame, obj, context, left_hand=False):
    """Use the 1.6 road widths and geometry in an exit's stable source neighbourhood."""
    extra = obj['exit']
    return context.pavements(frame, np.vstack([extra['approach'], extra['ramp']]), left_hand), {}


def _near_rows(pavements, q, limit):
    """Indices of the pavement rows whose outline's box lies within limit of q (the rest cannot be nearer)."""
    o = _outlines(pavements)
    if not len(o):
        return np.zeros(0, np.int64)
    q = np.asarray(q, float)[:2]
    return np.flatnonzero(np.all(q+limit >= o.lo, axis=1) & np.all(q-limit <= o.hi, axis=1))


def _clear(centre, radius, polygons):
    """A conservative disc keeps the entire panel and both posts clear of pavement."""
    lo = getattr(polygons, 'lo', None)
    if lo is not None:              # (signroads.Outlines: only the polygons whose box the disc touches)
        near = np.flatnonzero(np.all(centre+radius >= lo, axis=1) & np.all(centre-radius <= polygons.hi, axis=1))
        polygons = [polygons[i] for i in near]
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
    cache = getattr(pavements, 'gore_plans', None)
    if cache is None:
        return _compute_gore(extra, approach, ramp, pavements, ramp_mains, width_panel)
    # The source ribbons already identify anchor, map data, driving side and
    # width settings. Cache only the canonical horizontal search, never heights,
    # tile ownership or bridge clearance, which are still checked for each tile.
    key = (json.dumps(extra,sort_keys=True),json.dumps(ramp_mains,sort_keys=True),float(width_panel))
    with pavements.gore_lock:
        if key in cache:
            cache.move_to_end(key)
            return cache[key]
    result = _compute_gore(extra, approach, ramp, pavements, ramp_mains, width_panel)
    with pavements.gore_lock:
        cache[key] = result
        while len(cache)>32: cache.popitem(last=False)
    return result


def _compute_gore(extra, approach, ramp, pavements, ramp_mains, width_panel):
    import signroads as roadlanes
    ramp_rows = [r for r in pavements if r[1].link and _way_id(r[0]) == extra['ramp_id']]
    if not ramp_rows:
        # A ramp segment without its OSM way id (not from OSM, or the map data's OSM differs from the
        # sign shards' week): the spatial association, requiring both proximity and the ramp's direction.
        q, direction, _ = _station(ramp, min(45., _station(ramp, 1e12)[2]*.35))
        choices = []
        for fid, sp, line, _ in pavements:
            if not sp.link or sp.cls not in MAIN_CLASSES:
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
               distance_text='', distance_m=0., state=None):
    right = np.array([travel[1], -travel[0]])
    if state is None:
        la, lo, _ = frame.enu_to_geodetic(float(centre[0]), float(centre[1]), 0.)
        import signstates
        state = str(signstates.states(la, lo).item())
    offsets = (-.32, 0., .32) if portrait(state) and kind != 'ramp' and sign_style(obj['exit']['label']) == 'exit' else (-.32, .32)
    posts = np.array([centre + right * width * off for off in offsets])
    bases = np.array([_ground_z(frame, elev, p) for p in posts])
    z = _ground_z(frame, elev, centre)
    bottom = max(float(bases.max()) + 2.4, float(road_point[2]) + 2.2)
    label = obj['exit']['label']
    return dict(id=obj['id'], kind=kind, label=label, style=sign_style(label), state=state,
                centre=[float(centre[0]), float(centre[1]), z], travel=travel.tolist(), side=side,
                bottom=bottom, width=width, height=height, bases=bases.tolist(), post_offsets=list(offsets),
                distance=distance_text, advance_m=distance_m)


def place(frame, objects, roads, box, elev, chunks=(), left_hand=False, mph=True, link_ids=(),
          pavements=None, ramp_mains=None):
    """Place OSM destination signs along the generated mainline and in clear exit gores.

    Choose one clear gore on the full generated pavement, then test tile ownership.
    Searching farther along the ramp for each tile produces duplicate small panels.
    """
    import signroads as roadlanes
    x0, y0, x1, y1 = box
    links = set(link_ids)
    if pavements is None:
        pavements = []
        for fid, sp, line, _ in roads:
            sp = roadlanes.spec(sp, link=getattr(sp, 'link', False) or fid in links or sp.cls in LINK_CLASSES)
            p, width = roadlanes.pavement(sp, line, frame, left_hand)
            pavements.append((fid, sp, p, width))
    is_main = [sp.cls in MAIN_CLASSES and not sp.link for _, sp, _, _ in pavements]
    ramp_mains = ramp_mains or {}
    out, seen = [], set()

    def nearest(q, direction, limit):
        found = []
        for i in _near_rows(pavements, q, limit):
            if not is_main[i]:
                continue
            row = pavements[i]
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
        stations = (ADVANCE_MPH_STATIONS if mph else ((ADVANCE_METRIC, '500 m'),)) + ((DECISION_M, ''),)
        for distance, text in stations:
            if full[2] < distance + 20.:
                continue
            q, direction, _ = _station(approach, full[2] - distance)
            candidate = nearest(q, direction, 40.)
            if candidate is None:
                continue
            _, near, travel, paved_width = candidate
            _, _, spec, line, k, t, at_xy, _ = near
            at = line[k] * (1. - t) + line[k + 1] * t
            kind = 'advance' if text else 'decision'
            la, lo, _ = frame.enu_to_geodetic(float(at[0]), float(at[1]), 0.)
            import signstates
            state = str(signstates.states(la, lo).item())
            width, height = dimensions(label, kind, state)
            right = np.array([travel[1], -travel[0]])
            cross = roadlanes.cross(line)
            # The fourth row value is its generated ribbon width. Its cross-section can
            # vary through lane-count changes, rather than being the native Spec width.
            edge = abs((cross[k]*(1.-t)+cross[k+1]*t)@right)*paved_width/2.
            centre = at[:2] + right * side * (edge + 2. + width / 2.)
            # Never put a panel in a nearby road's pavement or on a bridge deck (a ramp still mapped as a bridge where it
            # comes down: not in the ground pavements; an EXIT 3 panel stood in the Fallsway's lane where the Jones
            # Falls Expressway's Guilford Avenue ramp lands, Baltimore 2026-10-29). Advance panels too.
            polygons = _outlines(pavements)
            for _ in range(12):
                if _clear(centre, width/2+.5, polygons) and not _on_deck(centre, chunks, width/2+1.):
                    break
                centre += right*side
            else:
                continue
            if not (x0 <= centre[0] < x1 and y0 <= centre[1] < y1) or _on_deck(at[:2], chunks, 2.):
                continue
            out.append(_make_sign(obj, kind, centre, travel, at, frame, elev, side, width, height, text, distance, state))

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


GLYPHS = 'sign_glyphs.json'      # tools/build_sign_glyphs.py


@lru_cache(maxsize=1)
def lettering():
    with open(Path(__file__).with_name(GLYPHS), encoding='utf-8') as f:
        return json.load(f)


def _text(value):
    glyphs = lettering()['glyphs']
    replacements = {'’': "'", '‘': "'", '–': '-', '—': '-', '…': '...'}
    return ''.join(c if c in glyphs else replacements.get(c, ''.join(
        p for p in unicodedata.normalize('NFKD', c) if p in glyphs) or '?') for c in str(value))


def text_width(value, height):
    data = lettering()
    return sum(data['glyphs'][c]['advance'] for c in _text(value))*height/data['capHeight']


def guide_lettering():
    """The guide panels' lettering: the same atlas (one font for all sign faces since PR #363's port)."""
    return lettering()


def guide_glyph(char):
    data = guide_lettering()
    return data, data['glyphs'][char]


def guide_width(value, height):
    return sum(glyph['advance']/data['capHeight'] for data, glyph in map(guide_glyph, _text(value)))*height


def route_symbols(routes, state=''):
    """Only explicit I/US prefixes select those networks; never use the exit number."""
    out, seen = [], set()
    for value in routes:
        for raw in re.split(r'\s*[/;|]\s*', str(value)):
            raw = ' '.join(raw.split())
            upper = raw.upper()
            match = re.fullmatch(r'(INTERSTATE|I|U\.?S\.?|US ROUTE|STATE(?: ROUTE| ROAD| HIGHWAY)?|SR|SH|ROUTE|RTE|[A-Z]{1,2})[ .-]*(\d{1,4}[A-Z]?)(?:\s+(NORTH|SOUTH|EAST|WEST|BUSINESS|BUS|ALT|BYPASS))?', upper)
            prefix, number, qualifier = match.groups() if match else ('', '', '')
            if prefix in ('I', 'INTERSTATE'):
                kind, region = 'interstate', ''
            elif prefix.replace('.', '') in ('US', 'US ROUTE'):
                kind, region = 'us', ''
            elif prefix in STATE_CODES or (prefix == 'M' and state == 'MI') or prefix in ('SR', 'SH', 'STATE', 'STATE ROUTE', 'STATE ROAD', 'STATE HIGHWAY', 'ROUTE', 'RTE'):
                kind, region = 'state', ('MI' if prefix == 'M' else prefix if prefix in STATE_CODES else state)
            elif re.fullmatch(r'\d{1,4}[A-Z]?', upper) and state in STATE_CODES:
                kind, region, number = 'state', state, upper
            else:
                kind, region, number = 'text', '', raw
            key = kind, region, number, qualifier or ''
            if number and key not in seen:
                seen.add(key)
                out.append(dict(kind=kind, state=region, number=number, qualifier=qualifier or ''))
    return out[:3]


def advance_arrow(sign):
    return sign['kind'] == 'decision'


def portrait(state):
    return state in STATE_CODES and state != 'MI'


def portrait_layout(sign):
    """Reference-photo arrangement: stacked shields, destinations, then distance/arrow."""
    label, w, h = sign['label'], sign['width'], sign['height']
    routes = route_symbols(label['routes'], sign.get('state', ''))
    names = label['names'] or ([] if routes else ['EXIT '+label['ref']])
    top = h-.40
    symbols, lines, directions = [], [], []
    for route in routes:
        sw = 1.08 if route['kind'] in ('interstate', 'us') and len(route['number']) >= 3 else .86
        compass = route['qualifier'] in ('NORTH', 'SOUTH', 'EAST', 'WEST')
        word = route['qualifier'].title() if compass else ''
        dw = guide_width(word, .46) if word else 0.
        group = sw + (.28+dw if word else 0.)
        y = -group/2+sw/2
        symbols.append(dict(route, qualifier='' if compass else route['qualifier'],
                            y=y, bottom=top-.90, width=sw, height=.90))
        if word:
            directions.append(dict(value=word,y=-group/2+sw+.28+dw/2,
                                   baseline=top-.65,height=.46,max_width=dw+.01))
        top -= 1.20
    top -= .10
    for name in names:
        lines.append(dict(value=name,y=0.,baseline=top-.58,height=.58,max_width=w-.65))
        top -= .85
    return dict(lines=lines,routes=symbols,directions=directions,arrow=advance_arrow(sign),
                arrow_y=0.,arrow_bottom=.28,arrow_size=1.35,
                distance=dict(value=sign.get('distance',''),y=0.,baseline=.30,height=.42,max_width=w-.8))


def advance_layout(sign):
    """One shared layout for sign geometry, placement tests and the authoring preview."""
    if portrait(sign.get('state', '')):
        return portrait_layout(sign)
    label, w, h = sign['label'], sign['width'], sign['height']
    routes = route_symbols(label['routes'], sign.get('state', ''))
    names = label['names'] or ([] if routes else ['EXIT '+label['ref']])
    arrow = advance_arrow(sign)
    left_arrow = right_arrow = 0.
    left = -w/2+.38+left_arrow+(1.45 if routes else 0.)
    right = w/2-.38-right_arrow
    cap, pitch = .64, .89
    block = cap+max(0, len(names)-1)*pitch
    centre_z = (.86+h-.36)/2
    top = centre_z+block/2
    lines = [dict(value=name,y=(left+right)/2,baseline=top-cap-i*pitch,
                  height=cap,max_width=right-left) for i,name in enumerate(names)]
    route_y = -w/2+1.0+left_arrow if names else (left_arrow-right_arrow)/2
    route_top = (lines[-1]['baseline']+cap/2+.48 if len(routes)==1 and len(lines)>1
                 else centre_z+(len(routes)*1.24-.28)/2)
    symbols = [dict(**route,y=route_y,bottom=route_top-.96-i*1.24,
                   width=1.20 if route['kind'] in ('interstate','us') and len(route['number'])>=3 else .96,
                   height=.96)
               for i,route in enumerate(routes)]
    return dict(lines=lines,routes=symbols,arrow=arrow,arrow_y=0.,
                arrow_bottom=.16,arrow_size=1.,distance=dict(value=sign.get('distance',''),y=0.,
                    baseline=.27,height=.38,max_width=w-.8))


def dimensions(label, kind, state=''):
    if sign_style(label) == 'rest_area':
        return (2.25, 3.05) if kind == 'ramp' else (3.0, 3.6)
    if kind == 'ramp':
        return max(2.3, min(4.3, text_width(label['ref'], .76)+1.1)), 2.15
    if portrait(state):
        routes = route_symbols(label['routes'], state)
        names = label['names'] or ([] if routes else ['EXIT '+label['ref']])
        groups = [(1.08 if r['kind'] in ('us','interstate') and len(r['number']) >= 3 else .86)
                  + (.28+guide_width(r['qualifier'].title(),.46)
                     if r['qualifier'] in ('NORTH','SOUTH','EAST','WEST') else 0.) for r in routes]
        width = min(6.0, max(3.5, max([guide_width(s,.58) for s in names]+groups+[0.])+.65))
        # (its content's height, a little taller for the portrait look: width * 1.22 alone made a long one-name panel
        # ~6 m tall and nearly empty, the Guilford Avenue exit, Baltimore 2026-10-29)
        content = .6+len(routes)*1.20+len(names)*.85+1.1
        return width, max(content, min(width*1.22, content*1.35))
    lines = label['names'] or ([] if label['routes'] else ['EXIT '+label['ref']])
    longest = max([guide_width(s, .64) for s in lines]+[2.7])
    width = min(8.5, max(5., longest+.76+.95+(1.45 if label['routes'] else 0.)))
    content = max(.64+max(0,len(lines)-1)*.89, len(route_symbols(label['routes']))*1.24-.28)
    return width, max(2.65, content+1.22)


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

    def guide_text(self, value, y, baseline, height, max_width, black=False):
        value = _text(value)
        fit = min(1., max_width/max(guide_width(value,height),1e-6))
        baseline += height*(1.-fit)/2
        cursor = y-guide_width(value,height)*fit/2
        for char in value:
            data, glyph = guide_glyph(char)
            scale = height*fit/data['capHeight']
            l,t,r,b = glyph['bbox'];u0,v0,u1,v1 = glyph['uv']
            if r>l and b>t:
                material = 8 if black else 7 if char in guide_lettering()['glyphs'] else 3
                self.quad([(.079,cursor+l*scale,baseline-b*scale),(.079,cursor+r*scale,baseline-b*scale),
                           (.079,cursor+r*scale,baseline-t*scale),(.079,cursor+l*scale,baseline-t*scale)],
                          material,[(u0,v0),(u1,v0),(u1,v1),(u0,v1)])
            cursor += glyph['advance']*scale

    def distance_text(self, row, bottom, stacked=False):
        value = row['value']
        if not value:
            return
        match = re.fullmatch(r'1/([24]) MILE', value) if stacked else None
        if match:
            h = row['height']
            word = guide_width('MILE', h)
            start = row['y']-(.92+word)/2
            baseline = bottom+row['baseline']
            self.guide_text('1',start+.12,baseline+.35,.28,.26)
            self.guide_text(match[1],start+.55,baseline-.02,.30,.32)
            self.quad([(.079,start+.19,baseline-.02),(.079,start+.23,baseline-.02),
                       (.079,start+.53,baseline+.65),(.079,start+.49,baseline+.65)],1)
            self.guide_text('MILE',start+.92+word/2,baseline+.06,h,word+.01)
        else:
            self.guide_text(value,row['y'],bottom+row['baseline'],row['height'],row['max_width'])

    def route_shield(self, route, bottom_offset=0.):
        y,bottom,w,h = route['y'],route['bottom']+bottom_offset,route['width'],route['height']
        kind = route['kind']
        if kind=='text':
            self.guide_text(route['number'],y,bottom+h*.35,.30,w+.1)
            return
        profile=routeshields.marker(route.get('state','')) if kind=='state' else None
        if profile is not None:
            # A single opaque white cutout. Concave outlines are triangulated
            # once per worker, with no layered faces to flicker at distance.
            points=profile.contour*[w,h]+[y,bottom]
            for tri in profile.triangles:
                self.triangle([(.067,*p) for p in points[tri]],1)
            x,z,cap,max_width=profile.number
            self.guide_text(route['number'],y+x*w,bottom+z*h,cap*h,max_width*w,black=True)
            if profile.legend:
                value,x,z,cap,max_width=profile.legend
                self.guide_text(value,y+x*w,bottom+z*h,cap*h,max_width*w,black=True)
            if route.get('qualifier'):
                self.guide_text(route['qualifier'],y,bottom+h+.04,.16,w+.12)
            return
        if kind=='interstate':
            contour=np.array([[-.43,1.],[-.23,.96],[0.,.97],[.23,.96],[.43,1.],
                [.49,.80],[.50,.64],[.45,.43],[.32,.20],[0.,0.],[-.32,.20],[-.45,.43],[-.50,.64],[-.49,.80]])[::-1]
        elif kind=='us':
            contour=np.array([[-.50,.84],[-.32,1.],[-.23,.96],[-.12,.94],[0.,1.],[.12,.94],[.23,.96],[.32,1.],[.50,.84],
                [.43,.72],[.42,.62],[.45,.49],[.48,.37],[.48,.27],[.44,.18],[.36,.12],[.23,.08],[.10,.05],
                [0.,0.],[-.10,.05],[-.23,.08],[-.36,.12],[-.44,.18],[-.48,.27],[-.48,.37],[-.45,.49],[-.42,.62],[-.43,.72]])[::-1]
        elif route.get('state')=='MI':
            contour=np.array([[0.,0.],[.5,.5],[0.,1.],[-.5,.5]])
        elif route.get('state')=='CA':
            # California spade, adapted to the requested white guide-sign silhouette.
            contour=np.array([[0.,0.],[.32,.02],[.46,.07],[.50,.14],[.50,.42],[.46,.62],[.37,.78],[.22,.91],
                              [0.,1.],[-.22,.91],[-.37,.78],[-.46,.62],[-.50,.42],[-.50,.14],[-.46,.07],[-.32,.02]])
        else:
            angle=np.linspace(0.,2*math.pi,49)[:-1]
            contour=np.c_[.49*np.cos(angle),.5+.49*np.sin(angle)]
        outer=contour*[w,h]+[y,bottom]
        inner=(contour-[0.,.5])*[w-.055,h-.055]+[y,bottom+h*.5]
        # Separate faces at one depth; no stacked white fill under a coloured face.
        for a,b,c,d in zip(outer,np.roll(outer,-1,axis=0),np.roll(inner,-1,axis=0),inner):
            self.quad([(.067,*a),(.067,*b),(.067,*c),(.067,*d)],1 if kind=='interstate' else 6)
        def clip(poly, level, above):
            out=[]
            for a,b in zip(poly,np.roll(poly,-1,axis=0)):
                ia,ib=(a[1]>=level,b[1]>=level) if above else (a[1]<=level,b[1]<=level)
                if ia:out.append(a)
                if ia!=ib:out.append(a+(b-a)*((level-a[1])/(b[1]-a[1])))
            return np.asarray(out)
        pieces=((clip(inner,bottom+h*.72,True),5),(clip(inner,bottom+h*.72,False),4)) if kind=='interstate' else ((inner,1),)
        for poly, material in pieces:
            # These contours are star-shaped; a centre fan preserves concave shield shoulders.
            centre=poly.mean(0)
            for a,b in zip(poly,np.roll(poly,-1,axis=0)):
                self.triangle([(.067,*centre),(.067,*a),(.067,*b)],material)
        if kind=='interstate':
            self.guide_text('INTERSTATE',y,bottom+h*.79,h*.10,w*.79)
            self.guide_text(route['number'],y,bottom+h*.24,h*.40,w*.78)
        elif kind=='state' and route.get('state')=='MI':
            # A diamond's upper corners narrow rapidly; keep the full digit
            # band within its central area, including wide two-digit numbers.
            self.guide_text(route['number'],y,bottom+h*.33,h*.34,w*.58,black=True)
        else:
            self.guide_text(route['number'],y,bottom+h*.30,h*.43,w*.72,black=True)
        if route.get('qualifier'):
            self.guide_text(route['qualifier'],y,bottom+h+.04,.16,w+.12)

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
        offsets = sign.get('post_offsets', (-.32, .32))
        for off, base in zip(offsets,sign.get('bases',[self.centre[2]]*len(offsets))):
            y = width*off
            self.box((-.12,y-.07,base-self.centre[2]-.2),(.02,y+.07,bottom+height-.1),2)
        side = 1 if sign['side'] > 0 else -1
        if sign.get('style',sign_style(label)) == 'rest_area':
            self.rounded_panel(width,bottom,height,4)
            welcome = all('welcome' in n.casefold() for n in label['names'])
            first,second = ('WELCOME','CENTER') if welcome else ('REST','AREA')
            self.text(first,0.,bottom+height-.83,.53,width-.4)
            self.text(second,0.,bottom+height-1.62,.53,width-.4)
            if sign['kind'] in ('ramp', 'decision'):
                self.arrow(0.,bottom+.34,side<0,1.25)
            else:
                self.text(sign['distance'],-.3*side,bottom+.36,.38,width-1.35)
                if advance_arrow(sign):
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
            tab_width = min(width, max(2.2, guide_width('EXIT '+label['ref'], .36)+.5))
            tab_y = (width-tab_width)/2*(1 if sign['side'] > 0 else -1)
            self.rounded_panel(tab_width,top,.66,0,tab_y)
            self.guide_text('EXIT '+label['ref'], tab_y, top+.16, .36, tab_width-.3)
        layout=advance_layout(sign)
        for line in layout['lines']:
            self.guide_text(line['value'],line['y'],bottom+line['baseline'],line['height'],line['max_width'])
        for line in layout.get('directions', ()):
            self.guide_text(line['value'],line['y'],bottom+line['baseline'],line['height'],line['max_width'])
        for route in layout['routes']:
            self.route_shield(route,bottom)
        distance=layout['distance']
        self.distance_text(distance,bottom,portrait(sign.get('state','')))
        if layout['arrow']:
            self.arrow(layout['arrow_y'],bottom+layout['arrow_bottom'],side<0,layout['arrow_size'])

    def result(self):
        if not self.t: return None
        materials = GUIDE_MATERIALS if max(self.mi)>=len(MATERIALS) else MATERIALS
        return np.asarray(self.p), np.asarray(self.t), np.asarray(self.uv), materials, np.asarray(self.mi)


def build(signs, origin):
    mesh = Mesh(origin)
    for sign in signs:
        mesh.panel(sign)
    return mesh.result()
