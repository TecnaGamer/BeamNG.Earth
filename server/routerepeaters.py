"""Sparse OSM route shields when no regional/weekly shield inventory is installed.

Full-way stations and local deterministic thinning make placement independent of
streaming tiles. This never guesses a route number or a mile reference.
"""
from collections import OrderedDict
import math
import threading
import json
import logging
import os
from pathlib import Path
import tempfile
import time
from concurrent.futures import CancelledError
import numpy as np
import osmtags
import routeindex
import signstates
import highwayroutes
import routerelations

log = logging.getLogger("earthd.route_repeaters")
CANDIDATE_CACHE_VERSION = 1

CELL_DEG = .1
SEPARATION_M = 250.        # duplicate suppression only; stations come from junctions
HALO_M = SEPARATION_M + 200.


def read_routes(tags, remote, bounds, kind='demand'):
    """Read route refs separately so ordinary road-tag decoding stays unchanged."""
    rd, files = tags._reader, {osmtags.PATH: remote}
    con, proto, errors = rd._connect(files, kind)
    try:
        uri = f"'{proto}://{osmtags.PATH}'"
        groups = tags._groups(con, uri, remote, kind)
        candidates = tags._candidates(groups, bounds)
        if not candidates:
            return []
        cols = ('id', 'bbox', 'highway', 'ref')
        rd._fetch(files, {osmtags.PATH: [r for g in candidates for c in cols
                                       for r in groups[g]['cols'].get(c, ())]}, kind)
        x0, y0, x1, y1 = map(float, bounds)
        predicate = (f"bbox.xmin <= {x1} AND bbox.xmax >= {x0} AND bbox.ymin <= {y1} AND bbox.ymax >= {y0} "
                     "AND highway IN ('motorway','trunk','primary','secondary','tertiary')")
        rows = con.execute(f'SELECT file_row_number FROM read_parquet({uri}, file_row_number=true) WHERE {predicate}').fetchall()
        if not rows:
            return []
        hit = [g for g in candidates if any(groups[g]['first'] <= r[0] < groups[g]['first']+groups[g]['n'] for r in rows)]
        wanted = ('oneway', 'bridge', 'tunnel', 'access', 'junction', 'direction', 'network', 'modifier')
        available = set.intersection(*(set(groups[g]['cols']) for g in hit))
        rd._fetch(files, {osmtags.PATH: [r for g in hit for c in (*wanted, 'geometry')
                                       for r in groups[g]['cols'].get(c, ())]}, kind)
        try:
            con.execute('SET enable_geoparquet_conversion = false')
        except Exception:
            pass
        columns = ', '.join(f'"{c}"' if c in available else f'NULL AS "{c}"' for c in wanted)
        numbers = ','.join(str(int(r[0])) for r in rows)
        ref_column='ref' if 'ref' in available else 'NULL AS ref'
        result = []
        for wid, highway, ref, *rest in con.execute(
                f'SELECT id, highway, {ref_column}, {columns}, geometry FROM read_parquet({uri}, file_row_number=true) '
                f'WHERE file_row_number IN ({numbers})').fetchall():
            line = osmtags._linestring(bytes(rest[-1])) if rest[-1] is not None else None
            if line is not None and len(line) >= 2:
                attrs = dict(highway=highway, ref=ref, **{c: v for c, v in zip(wanted, rest) if v is not None})
                result.append((int(wid), line, attrs))
        return result
    except Exception:
        for err in errors:
            if isinstance(err, osmtags.rangeread.Changed):
                raise err
        raise
    finally:
        con.close()


def candidates(ways, relations=None):
    # Full geometry from the existing parquet reader; shared coordinate IDs join
    # adjacent OSM ways without restarting stations at way or streaming seams.
    coords, rows = {}, []
    import hashlib
    def node(point):
        key = tuple(round(float(v),7) for v in point)
        nid = int.from_bytes(hashlib.blake2b(repr(key).encode(),digest_size=8).digest(),'little')
        coords[nid] = key
        return nid
    for wid,line,tags in ways:
        rows.append((wid,[node(p) for p in line],tags))
    out = routeindex.index_records(rows,coords,relations)
    for obj in out:
        obj['roadside'] = obj.pop('ways')[0]
    return out


def keys(obj):
    return {highwayroutes.identity(r) for r in obj.get('roadside', {}).get('routes', ())}


def distance(a, b):
    scale = math.cos(math.radians((a['lat']+b['lat'])/2))
    return 111320.*math.hypot((a['lon']-b['lon'])*scale, a['lat']-b['lat'])


def thin(objects, bounds):
    """Keep local priority minima; all competitors within HALO_M must be supplied."""
    x0, y0, x1, y1 = bounds
    out = []
    keyed = [(o, keys(o)) for o in objects]
    for obj, routes in keyed:
        if not (x0 <= obj['lon'] < x1 and y0 <= obj['lat'] < y1):
            continue
        if any(other['id'] < obj['id'] and routes & other_routes and distance(obj, other) < SEPARATION_M
               for other, other_routes in keyed):
            continue
        out.append(obj)
    if out:
        states = signstates.states(np.array([o['lat'] for o in out]), np.array([o['lon'] for o in out]))
        out = [o for o, state in zip(out, states) if state]
    return out


class Source:
    def __init__(self, raw_dir, fetcher):
        self.tags = osmtags.OsmTags(raw_dir, fetcher)
        self.relations = routerelations.Source(raw_dir)
        self.cache = OrderedDict()
        self.lock = threading.Lock()
        self.saved = Path(raw_dir)/'route-candidates-v1'
        self.retry_at = OrderedDict()
        self.persisted = OrderedDict()
        self.warned = -float("inf")

    def _saved_cell(self, path):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if data['format'] != CANDIDATE_CACHE_VERSION:
                return None
            objects = data['objects']
            if not isinstance(objects, list):
                return None
            for obj in objects:
                float(obj['lon']); float(obj['lat']); int(obj['id'])
                if obj['roadside']['type'] != 'shield' or not obj['roadside']['routes']:
                    return None
            return objects
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _cell(self, x, y):
        path = self.saved/f'{x}_{y}.json'
        if time.monotonic() < self.retry_at.get((x,y), 0):
            saved = self._saved_cell(path)
            if saved is not None:
                return saved
        try:
            result = self._online_cell(x, y)
        except CancelledError:
            raise
        except Exception:
            saved = self._saved_cell(path)
            if saved is None:
                raise
            self.retry_at[(x,y)] = time.monotonic()+osmtags.RETRY_S
            while len(self.retry_at)>32: self.retry_at.popitem(last=False)
            log.warning('Road source unavailable for cell %s,%s; using saved shield candidates', x, y)
            return saved
        # Persist semantic candidates, never mixed Parquet blocks from two releases.
        self.retry_at.pop((x,y), None)
        try:
            if self.persisted.get((x,y)) is not result:
                path.parent.mkdir(parents=True, exist_ok=True)
                fd, temp = tempfile.mkstemp(prefix=path.stem+'-', suffix='.tmp', dir=path.parent)
                try:
                    with os.fdopen(fd, 'w', encoding='utf-8') as f:
                        json.dump(dict(format=CANDIDATE_CACHE_VERSION, saved_at=time.time(), objects=result), f)
                    os.replace(temp, path)
                    self.persisted[(x,y)] = result
                    self.persisted.move_to_end((x,y))
                    while len(self.persisted)>32: self.persisted.popitem(last=False)
                finally:
                    if os.path.exists(temp): os.remove(temp)
        except OSError as error:
            log.debug('Cannot save shield candidates: %s', error)
        return result

    def _online_cell(self, x, y):
        for attempt in (0, 1):
            version, remote = self.tags.version()
            key = version, x, y
            if key not in self.cache:
                box = (x*CELL_DEG, y*CELL_DEG, (x+1)*CELL_DEG, (y+1)*CELL_DEG)
                dy = HALO_M/110000.
                dx = dy/max(.25, math.cos(math.radians(max(abs(box[1]), abs(box[3]))+dy)))
                area = (box[0]-dx, box[1]-dy, box[2]+dx, box[3]+dy)
                try:
                    ways=read_routes(self.tags,remote,area)
                    relations=self.relations.query(wid for wid,_,_ in ways)
                    self.cache[key] = thin(candidates(ways,relations), box)
                except osmtags.rangeread.Changed:
                    self.tags._forget()
                    if attempt:
                        raise
                    continue
            self.cache.move_to_end(key)
            result = self.cache[key]
            while len(self.cache) > 32:
                self.cache.popitem(last=False)
            return result

    def query(self, lon0, lat0, lon1, lat1, existing=()):
        # Explicit US refs only. Avoid an optional download outside US latitudes.
        if lon1 < -180 or lon0 > -60 or lat1 < 18 or lat0 > 72:
            return []
        result, answered, failure = [], 0, None
        with self.lock:
            for x in range(math.floor(lon0/CELL_DEG), math.floor(lon1/CELL_DEG)+1):
                for y in range(math.floor(lat0/CELL_DEG), math.floor(lat1/CELL_DEG)+1):
                    try:
                        objects = self._cell(x, y)
                    except CancelledError:
                        raise
                    except Exception as error:
                        failure = error
                        continue
                    answered += 1
                    result.extend(o for o in objects if lon0 <= o['lon'] <= lon1 and lat0 <= o['lat'] <= lat1)
            if failure is not None:
                if not answered:
                    raise failure
                if time.monotonic()-self.warned >= 60.:
                    self.warned = time.monotonic()
                    log.warning('Partial shield coverage: unavailable uncached cells; keeping available candidates: %s', failure)
        shields = [(o, keys(o)) for o in existing if o.get('roadside', {}).get('type') == 'shield']
        return [o for o in result if not any(keys(o) & k and distance(o, old) < SEPARATION_M for old, k in shields)]
