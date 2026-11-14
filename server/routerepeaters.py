"""Sparse OSM route shields when no regional/weekly shield inventory is installed.

Full-way stations and local deterministic thinning make placement independent of
streaming tiles. This never guesses a route number or a mile reference.
"""
from collections import OrderedDict
import math
import threading
import numpy as np
import dmath
import dnp
import osmtags
import routeindex
import signstates

CELL_DEG = .1
CANDIDATE_M = 700.
SEPARATION_M = 2400.       # local minima: typically about three miles apart
HALO_M = SEPARATION_M + 200.


def read_routes(tags, remote, bounds, kind='demand'):
    """Read route refs separately so ordinary road-tag decoding stays unchanged."""
    rd, files = tags._reader, {osmtags.PATH: remote}
    con, proto, errors = rd._connect(files, kind)
    try:
        uri = f"'{proto}://{osmtags.PATH}'"
        groups = tags._groups(con, uri, remote, kind)
        candidates = tags._candidates(groups, bounds)
        if not candidates or not all('ref' in groups[g]['cols'] for g in candidates):
            return []
        cols = ('id', 'bbox', 'highway', 'ref')
        rd._fetch(files, {osmtags.PATH: [r for g in candidates for c in cols
                                       for r in groups[g]['cols'].get(c, ())]}, kind)
        x0, y0, x1, y1 = map(float, bounds)
        predicate = (f"bbox.xmin <= {x1} AND bbox.xmax >= {x0} AND bbox.ymin <= {y1} AND bbox.ymax >= {y0} "
                     "AND highway IN ('motorway','trunk','primary','secondary','tertiary') "
                     "AND regexp_matches(ref, '(?i)(^|;)\\s*(I|US)[ -]?[0-9]{1,3}\\s*(;|$)')")
        rows = con.execute(f'SELECT file_row_number FROM read_parquet({uri}, file_row_number=true) WHERE {predicate}').fetchall()
        if not rows:
            return []
        hit = [g for g in candidates if any(groups[g]['first'] <= r[0] < groups[g]['first']+groups[g]['n'] for r in rows)]
        wanted = ('oneway', 'bridge', 'tunnel', 'access', 'junction', 'direction')
        available = set.intersection(*(set(groups[g]['cols']) for g in hit))
        rd._fetch(files, {osmtags.PATH: [r for g in hit for c in (*wanted, 'geometry')
                                       for r in groups[g]['cols'].get(c, ())]}, kind)
        try:
            con.execute('SET enable_geoparquet_conversion = false')
        except Exception:
            pass
        columns = ', '.join(f'"{c}"' if c in available else f'NULL AS "{c}"' for c in wanted)
        numbers = ','.join(str(int(r[0])) for r in rows)
        result = []
        for wid, highway, ref, *rest in con.execute(
                f'SELECT id, highway, ref, {columns}, geometry FROM read_parquet({uri}, file_row_number=true) '
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


def candidates(ways):
    result = []
    for wid, line, tags in sorted(ways, key=lambda w: w[0]):
        routes = routeindex.route_refs(tags, ())
        if not routes or tags.get('highway') not in routeindex.ROAD_CLASSES or not routeindex.ground(tags):
            continue
        path = np.asarray(line, float)
        scale = np.array([111320.*dmath.cos(math.radians(float(path[0, 1]))), 111320.])
        arc = np.r_[0., np.cumsum(dnp.norm(np.diff(path, axis=0)*scale, axis=1))]
        length = float(arc[-1])
        if length < 10.:
            continue
        at = lambda s: np.array([np.interp(s, arc, path[:, j]) for j in (0, 1)])
        one = routeindex.exitsigns._direction(tags)
        if not one and tags.get('highway') == 'motorway' and tags.get('oneway') not in ('no', 'false', '0'):
            one = 1
        stations = np.linspace(0., length, max(1, int(math.ceil(length/CANDIDATE_M)))+1)
        for k, station in enumerate((stations[:-1]+stations[1:])/2):
            point = at(station)
            a, b = max(0., station-60.), min(length, station+60.)
            local = np.vstack([at(a), path[(arc > a) & (arc < b)], at(b)])
            for direction in ((one,) if one else (1, -1)):
                approach = local if direction > 0 else local[::-1]
                axis = (approach[-1]-approach[0])*scale
                axis /= max(dnp.norm(axis), 1e-9)
                panels = [dict(kind='interstate' if r[0]=='I' else 'us', number=r[1],
                               direction=routeindex.posted_direction(tags, (), direction, r, axis)) for r in routes[:3]]
                extra = dict(type='shield', routes=panels, main_id=wid, point=point.tolist(),
                             approach=approach.tolist(), axis=axis.tolist())
                obj = routeindex.record(('fallback-shield-v1', wid, k, direction), point, extra)
                obj.pop('ways'); obj['roadside'] = extra
                result.append(obj)
    return result


def keys(obj):
    return {(r['kind'], r['number'], r.get('direction', '')) for r in obj.get('roadside', {}).get('routes', ())}


def distance(a, b):
    scale = dmath.cos(math.radians((a['lat']+b['lat'])/2))
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
        self.cache = OrderedDict()
        self.lock = threading.Lock()

    def _cell(self, x, y):
        for attempt in (0, 1):
            version, remote = self.tags.version()
            key = version, x, y
            if key not in self.cache:
                box = (x*CELL_DEG, y*CELL_DEG, (x+1)*CELL_DEG, (y+1)*CELL_DEG)
                dy = HALO_M/110000.
                dx = dy/max(.25, dmath.cos(math.radians(max(abs(box[1]), abs(box[3]))+dy)))
                area = (box[0]-dx, box[1]-dy, box[2]+dx, box[3]+dy)
                try:
                    self.cache[key] = thin(candidates(read_routes(self.tags, remote, area)), box)
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
        result = []
        with self.lock:
            for x in range(math.floor(lon0/CELL_DEG), math.floor(lon1/CELL_DEG)+1):
                for y in range(math.floor(lat0/CELL_DEG), math.floor(lat1/CELL_DEG)+1):
                    result.extend(o for o in self._cell(x, y) if lon0 <= o['lon'] <= lon1 and lat0 <= o['lat'] <= lat1)
        shields = [(o, keys(o)) for o in existing if o.get('roadside', {}).get('type') == 'shield']
        return [o for o in result if not any(keys(o) & k and distance(o, old) < SEPARATION_M for old, k in shields)]
