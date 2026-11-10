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
        rd._fetch(files, {osmtags.PATH: [p for g in candidates for c in columns
                                       for p in groups[g]['cols'][c]]}, 'demand')
        x0, y0, x1, y1 = map(float, bounds)
        numbers = ','.join(str(int(wid)) for wid in ids)
        if not numbers:
            return {}
        query = (f'SELECT id, highway, ref FROM read_parquet({uri}) '
                 f'WHERE id IN ({numbers}) AND bbox.xmin <= {x1} AND bbox.xmax >= {x0} '
                 f'AND bbox.ymin <= {y1} AND bbox.ymax >= {y0}')
        return {int(wid): dict(highway=hw, ref=ref or '') for wid, hw, ref in con.execute(query).fetchall()}
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
                if (not extra or through_content(extra) or time.monotonic() < self.retry
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


def add_overheads(frame, roadside, objects, contexts, area, elev, chunks, replaced):
    """Promote decision and nearest advance stations, retaining safe fallbacks.

    Reuse the common geometry/support planner: columns must clear pavement and
    bridge decks; no assembly means the existing roadside board is retained.
    """
    advances = {}
    for s in roadside:
        if s['kind'] == 'advance':
            advances[s['id']] = min(advances.get(s['id'], float('inf')), s.get('advance_m', float('inf')))
    assemblies = []
    for sign in roadside:
        key = sign['id'], sign['kind'], sign['distance']
        if key in replaced or sign.get('style') != 'exit':
            continue
        if sign['kind'] != 'decision' and not (sign['kind'] == 'advance' and
                sign.get('advance_m', float('inf')) == advances.get(sign['id'])):
            continue
        obj = objects[sign['id']]
        extra = obj['exit']
        pavements = contexts[sign['id']]
        highway = extra.get('main_tags', {}).get('highway')
        if highway not in (None, 'motorway'):
            continue
        candidate = throughsigns._candidate(pavements, sign['road_point'], sign['travel'], extra['main_ids'])
        if candidate is None or candidate[2][2].cls != 'motorway':
            continue
        content = through_content(extra)
        blue_exit = any(re.fullmatch(r'A\s*\d+[A-Z]?', ref, re.I)
                        for ref in sign['label'].get('routes', ()))
        if not content and not blue_exit:
            continue
        boards = None if content else [dict(label=sign['label'], width=sign['width'],
            height=sign['height'], exit=True, side=sign['side'], kind=sign['kind'])]
        assembly = throughsigns._assembly(frame, obj, pavements, sign['road_point'], sign['travel'],
                                         area, elev, chunks, content, sign['label'], sign['side'],
                                         sign['distance'], allow_outside=True, branch_panels=boards)
        if assembly is not None:
            for panel in assembly['panels']:
                panel.setdefault('kind', sign['kind'])
                if panel['exit']:
                    panel.setdefault('side', sign['side'])
            assemblies.append(assembly)
            replaced.add(key)
    return assemblies
