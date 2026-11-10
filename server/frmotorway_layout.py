"""French mainline/exit boards sharing safe overhead supports.

These assemblies are scenery estimates from mapped road geometry and labels,
not a survey of the actual gantries. No through-city name is inferred from an exit.
"""
import re
from collections import OrderedDict
from concurrent.futures import CancelledError
import logging
import threading
import time

import numpy as np

import throughsigns

log = logging.getLogger('earthd.highwaysigns')


def read_main_tags(tags, remote, bounds, ids):
    """Read only the reference/class of exact OSM mainline IDs, no geometry column.

    Older weekly sign shards discard these fields. Use the same pinned, range-
    cached highway source as the road renderer, never a nearby road's reference.
    """
    import osmtags
    rd, files = tags._reader, {osmtags.PATH: remote}
    con, proto, errors = rd._connect(files, 'demand')
    try:
        uri = f"'{proto}://{osmtags.PATH}'"
        groups = tags._groups(con, uri, remote, 'demand')
        candidates = tags._candidates(groups, bounds)
        columns = ('id', 'bbox', 'highway', 'ref')
        if not candidates or not all(set(columns) <= set(groups[g]['cols']) for g in candidates):
            return {}
        available = set.intersection(*(set(groups[g]['cols']) for g in candidates))
        optional = [c for c in ('oneway', 'lanes', 'int_ref', 'destination', 'destination:forward',
                    'destination:backward', 'destination:lanes', 'destination:lanes:forward',
                    'destination:lanes:backward', 'destination:ref', 'destination:distance') if c in available]
        rd._fetch(files, {osmtags.PATH: [p for g in candidates for c in (*columns, *optional)
                                       for p in groups[g]['cols'][c]]}, 'demand')
        x0, y0, x1, y1 = map(float, bounds)
        numbers = ','.join(str(int(wid)) for wid in ids)
        if not numbers:
            return {}
        selected = ', '.join('"'+c+'"' for c in ('id', 'highway', 'ref', *optional))
        query = (f'SELECT {selected} FROM read_parquet({uri}) '
                 f'WHERE id IN ({numbers}) AND bbox.xmin <= {x1} AND bbox.xmax >= {x0} '
                 f'AND bbox.ymin <= {y1} AND bbox.ymax >= {y0}')
        return {int(row[0]): dict(highway=row[1], ref=row[2] or '',
                    **{k: str(v) for k, v in zip(optional, row[3:]) if v is not None})
                for row in con.execute(query).fetchall()}
    except Exception:
        for error in errors:
            if isinstance(error, osmtags.rangeread.Changed):
                raise error
        raise
    finally:
        con.close()


class MainlineTags:
    """Small optional metadata lookup, shared by neighbouring French sign tiles."""

    def __init__(self, raw_dir, fetcher):
        import osmtags
        self.tags = osmtags.OsmTags(raw_dir, fetcher)
        self.cache, self.lock = OrderedDict(), threading.Lock()
        self.retry = 0.

    def enrich(self, objects):
        import osmtags
        result = []
        with self.lock:
            for obj in objects:
                extra = obj.get('exit')
                known = through_content(extra) if extra else None
                if (not extra or (known and not known.get('route_only')) or time.monotonic() < self.retry
                        or extra.get('main_tags', {}).get('highway') not in (None, 'motorway')):
                    result.append(obj)
                    continue
                ids = tuple(sorted(set(extra['main_ids'])))
                try:
                    for attempt in (0, 1):
                        version, remote = self.tags.version()
                        key = version, ids
                        if key in self.cache:
                            self.cache.move_to_end(key)
                            break
                        path = np.asarray(extra['approach'], float)
                        lo, hi = path.min(axis=0)-.0001, path.max(axis=0)+.0001
                        try:
                            self.cache[key] = read_main_tags(self.tags, remote, (*lo, *hi), ids)
                        except osmtags.rangeread.Changed:
                            self.tags._forget()
                            if attempt:
                                raise
                            continue
                        break
                    rows = list(self.cache[key].values())
                    # A motorway number is only used when every available exact
                    # mainline match agrees; ambiguous transitions stay unlabelled.
                    refs = {row['ref'] for row in rows if row['ref']}
                    if rows and all(row['highway'] == 'motorway' for row in rows) and len(refs) == 1:
                        extra = dict(extra, main_tags=dict(extra.get('main_tags', {}),
                                                          highway='motorway', ref=refs.pop()))
                        posted = {}
                        for row in rows:
                            label = throughsigns.label(row)
                            if label:
                                posted[repr((label['names'], label['routes']))] = label
                        if len(posted) == 1:
                            extra['through_label'] = next(iter(posted.values()))
                        obj = dict(obj, exit=extra)
                    while len(self.cache) > 512:
                        self.cache.popitem(last=False)
                except CancelledError:
                    raise
                except Exception as error:
                    self.retry = time.monotonic()+60.
                    log.warning('French mainline references unavailable; existing signs retained: %s', error)
                result.append(obj)
        return result


def through_content(extra):
    content = extra.get('through_label') or throughsigns.label(extra.get('main_tags', {}))
    if content:
        return content
    # A mapped current-road reference is usable without inventing destinations.
    # Never borrow destination:ref or the ramp's reference for the mainline.
    refs = throughsigns.values(extra.get('main_tags', {}).get('ref'))
    if not any(re.fullmatch(r'A\s*\d+[A-Z]?', ref, re.I) for ref in refs):
        return None
    return dict(ref='', names=refs[:3], routes=refs[:3], direction='', route_only=True)


def roadside_size(label, kind, state=None):
    import frhighwaysigns
    if kind == 'ramp':
        return 1.55, .65
    plan = frhighwaysigns.compose(label, True, '500 m' if kind == 'advance' else '', roadside=True)
    return (plan['width'], plan['height']) if plan else (2.8, .65)


def add_overheads(frame, roadside, objects, contexts, area, elev, chunks, replaced):
    """One motorway decision assembly; try upstream ground if a deck blocks it.

    Planning is independent of whether a roadside decision panel could be placed.
    Advance boards remain on the shoulder. Ownership is resolved after grouping.
    """
    import exitsigns
    import frhighwaysigns
    assemblies = []
    for oid, obj in sorted(objects.items()):
        extra = obj['exit']
        if exitsigns.sign_style(extra['label']) != 'exit':
            continue
        if extra.get('main_tags', {}).get('highway') not in (None, 'motorway'):
            continue
        if not extra['label'].get('names') and not extra['label'].get('fr_blocks'):
            continue
        pavements = contexts[oid]
        approach = exitsigns._xy(frame, extra['approach'])
        end = exitsigns._station(approach, 1e12)
        if end is None:
            continue
        for distance in (35., 75., 130., 200.):
            if end[2] < distance+20.:
                continue
            q, travel, _ = exitsigns._station(approach, end[2]-distance)
            if not (area[0] <= q[0] < area[2] and area[1] <= q[1] < area[3]):
                continue
            candidate = throughsigns._candidate(pavements, q, travel, extra['main_ids'])
            if candidate is None or candidate[2][2].cls != 'motorway':
                continue
            lanes = frhighwaysigns.lane_count(pavements, q, travel, extra['main_ids'], extra.get('main_tags'))
            boards = frhighwaysigns.planned_panels(through_content(extra), extra['label'], lanes, '', extra['side'])
            if not boards:
                continue
            for panel in boards:
                panel['kind'] = 'decision'
                panel['fr_support'] = extra.get('fr_support', 'gantry')
            assembly = throughsigns._assembly(frame, obj, pavements, q, travel, area, elev, chunks,
                None, side=extra['side'], allow_outside=True, branch_panels=boards)
            if assembly is not None:
                assemblies.append(assembly)
                replaced.add((oid, 'decision', ''))
                break
    return assemblies


def place_through(frame, obj, pavements, area, elev, chunks=()):
    """Standalone mainline panels use the same French envelope as their mesh."""
    import exitsigns
    import frhighwaysigns
    extra = obj['through']
    content = frhighwaysigns.full_label(extra['label'])
    if content is not extra['label']:
        obj = dict(obj, through=dict(extra, label=content))
        extra = obj['through']
    q = exitsigns._xy(frame, [extra['point']])[0]
    approach = exitsigns._xy(frame, extra['approach'])
    near = exitsigns._nearest(q, [('', None, approach, 0.)])
    if near is None:
        return [], []
    travel = near[7]
    if content.get('distances') and len(content['distances']) == len(content['names']):
        sign = confirmation(frame, obj, pavements, q, travel, area, elev, chunks)
        return ([sign] if sign else []), []
    lanes = frhighwaysigns.lane_count(pavements, q, travel, extra['main_ids'], extra.get('main_tags'))
    boards = frhighwaysigns.planned_panels(content, None, lanes, '', 1)
    if not boards:
        return [], []
    assembly = throughsigns._assembly(frame, obj, pavements, q, travel, area, elev, chunks,
                                      None, branch_panels=boards)
    return [], [assembly] if assembly else []


def confirmation(frame, obj, pavements, q, travel, area, elev, chunks):
    import exitsigns
    import frhighwaysigns
    import signroads
    extra = obj['through']
    found = throughsigns._candidate(pavements, q, travel, extra['main_ids'])
    if found is None or exitsigns._on_deck(q, chunks, 3.):
        return None
    _, _, near, travel, width = found
    _, _, sp, line, k, t, at, _ = near
    plan = frhighwaysigns.compose(extra['label'], roadside=True, confirmation=True)
    if plan is None:
        return None
    right = np.array([travel[1], -travel[0]])
    cross = signroads.cross(line)
    edge = abs((cross[k]*(1.-t)+cross[k+1]*t)@right)*width/2.
    centre = at+right*(edge+2.+plan['width']/2)
    for _ in range(12):
        if (exitsigns._clear(centre, plan['width']/2+.5, exitsigns._outlines(pavements))
                and not exitsigns._on_deck(centre, chunks, plan['width']/2+1.)):
            break
        centre += right
    else:
        return None
    if not (area[0] <= centre[0] < area[2] and area[1] <= centre[1] < area[3]):
        return None
    fake = dict(obj, exit=dict(label=extra['label']))
    return exitsigns._make_sign(fake, 'confirmation', centre, travel,
        np.r_[at, exitsigns._ground_z(frame, elev, at)], frame, elev, 1, plan['width'], plan['height'])
