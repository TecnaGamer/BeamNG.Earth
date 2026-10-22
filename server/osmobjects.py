"""Mapped lamps, speed signs and exit destinations from the cached regional OSM extract.

An independent, atomic point index leaves the pinned lane/terrain snapshot untouched.
Missing indexes prepare in the background; road and terrain workers never wait for PBF
downloads/imports. No OSM editing API or per-tile Overpass requests.
"""
from contextlib import closing
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time

import numpy as np
import osmlanes
import exitsigns

log = logging.getLogger('earthd.osmobjects')
VERSION = 3                         # connected approaches now cover the one-mile advance sign
TAG_KEYS = ('highway', 'traffic_sign', 'traffic_sign:forward', 'traffic_sign:backward',
            'maxspeed', 'maxspeed:forward', 'maxspeed:backward', 'direction',
            'traffic_sign:direction', 'lamp_mount', 'light:mount', 'support', 'height',
            'light:direction', 'lamp_type', 'light:method', 'light:colour', 'layer', 'bridge') + exitsigns.EXIT_KEYS


def tile_objects(md, frame, tx, ty):
    source = getattr(md,'osm_objects',None)
    if source is None:
        return []
    import roads
    x0,y0,x1,y1 = roads.tile_bbox(tx,ty)
    objects = source.query(frame,(x0-80.,y0-80.,x1+80.,y1+80.))
    if hasattr(source, 'query_exits'):
        margin = exitsigns.QUERY_MARGIN
        objects += source.query_exits(frame, (x0-margin,y0-margin,x1+margin,y1+margin))
    return objects


def tile_exit_objects(md, frame, tx, ty):
    """Exit destinations whose advance-sign approaches overlap this road tile."""
    source = getattr(md, 'osm_objects', None)
    if source is None or not hasattr(source, 'query_exits'):
        return []
    import roads
    import exitsigns
    x0, y0, x1, y1 = roads.tile_bbox(tx, ty)
    margin = exitsigns.QUERY_MARGIN
    return source.query_exits(frame, (x0 - margin, y0 - margin, x1 + margin, y1 + margin))


def is_lamp(tags):
    return tags.get('highway') == 'street_lamp'


def is_speed_sign(tags):
    values = ';'.join(tags.get(k, '') for k in ('traffic_sign', 'traffic_sign:forward', 'traffic_sign:backward'))
    return bool(re.search(r'maxspeed|(?:US:)?R2-1|(?:DE:)?274(?:\[|$)|(?:GB:)?670', values, re.I))


def speed(value, default_mph=False):
    """Exact displayed integer + units. Conditional/variable/ambiguous limits stay unknown."""
    m = re.fullmatch(r'\s*(\d+(?:\.0+)?)\s*(mph|km/?h|kph)?\s*', str(value or ''), re.I)
    if not m:
        return None
    n = int(float(m[1]))
    unit = (m[2] or ('mph' if default_mph else 'kmh')).lower()
    return (n, 'mph' if unit == 'mph' else 'kmh') if 1 <= n <= 160 else None


def bearing(value):
    compass = {'N':0, 'NE':45, 'E':90, 'SE':135, 'S':180, 'SW':225, 'W':270, 'NW':315}
    if str(value).upper() in compass:
        return float(compass[str(value).upper()])
    try:
        n = float(value)
        return n % 360 if math.isfinite(n) else None
    except (ValueError, TypeError):
        return None


def height(value, default):
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(m|ft|')?\s*", str(value or ''), re.I)
    if not m:
        return default
    n = float(m[1]) * (.3048 if m[2] in ('ft', "'") else 1.)
    return float(np.clip(n, 2., 60.))


def import_pbf(path, target):
    """Three bounded streaming passes: objects, their road memberships, needed neighbours.

    Keep only tagged objects and adjacent way vertices in memory, never a full node cache.
    Directional sign tags refer to their actual OSM way, not a nearby opposite carriageway.
    """
    import osmium
    objects, memberships, needed = {}, {}, set()
    main_ways, links, junctions = [], [], {}
    for n in osmium.FileProcessor(str(path), osmium.osm.NODE).with_filter(osmium.filter.KeyFilter(*TAG_KEYS)):
        tags = {k:v for k,v in n.tags if k in TAG_KEYS}
        if n.location.valid() and (is_lamp(tags) or is_speed_sign(tags)):
            objects[n.id] = (n.location.lon, n.location.lat, tags)
        elif n.location.valid() and tags.get('highway') == 'motorway_junction':
            junctions[n.id] = tags
    for w in osmium.FileProcessor(str(path), osmium.osm.WAY).with_filter(osmium.filter.KeyFilter('highway')):
        refs = [n.ref for n in w.nodes]
        highway = w.tags.get('highway')
        if highway in exitsigns.MAIN_CLASSES or highway in exitsigns.LINK_CLASSES:
            wt = {k:v for k,v in w.tags if k in exitsigns.WAY_KEYS}
            (main_ways if highway in exitsigns.MAIN_CLASSES else links).append((w.id, refs, wt))
            needed.update(refs)
        for i, nid in enumerate(refs):
            if nid not in objects or len(refs) < 2:
                continue
            a, b = refs[max(0,i-1)], refs[min(len(refs)-1,i+1)]
            tags = {k:v for k,v in w.tags if k in ('highway','oneway','maxspeed','maxspeed:forward','maxspeed:backward','bridge','layer')}
            memberships.setdefault(nid, []).append((w.id, a, b, tags))
            needed.update((a,b))
    neighbours = {}
    if needed:
        for n in osmium.FileProcessor(str(path), osmium.osm.NODE).with_filter(osmium.filter.IdFilter(needed)):
            if n.location.valid():
                neighbours[n.id] = (n.location.lon, n.location.lat)
    exits = exitsigns.index_records(main_ways, links, neighbours, junctions)
    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    tmp = f'{target}.{os.getpid()}.{threading.get_ident()}.tmp'
    try:
        with closing(sqlite3.connect(tmp)) as con:
            con.execute("CREATE TABLE objects(id INTEGER PRIMARY KEY, lon REAL, lat REAL, tags TEXT, ways TEXT, kind TEXT DEFAULT 'point')")
            con.execute('CREATE VIRTUAL TABLE spatial USING rtree(id, minlon, maxlon, minlat, maxlat)')
            for nid, (lon,lat,tags) in objects.items():
                ways = []
                for wid,a,b,wt in memberships.get(nid, ()):
                    if a in neighbours and b in neighbours:
                        d = (np.array(neighbours[b])-neighbours[a])*[math.cos(math.radians(lat)),1.]
                        if np.linalg.norm(d) > 1e-12:
                            ways.append(dict(id=wid, bearing=math.degrees(math.atan2(d[0],d[1]))%360., **wt))
                con.execute('INSERT INTO objects(id,lon,lat,tags,ways) VALUES(?,?,?,?,?)', (nid,lon,lat,json.dumps(tags),json.dumps(ways)))
                con.execute('INSERT INTO spatial VALUES(?,?,?,?,?)', (nid,lon,lon,lat,lat))
            for obj in exits:
                nid, lon, lat = obj['id'], obj['lon'], obj['lat']
                con.execute('INSERT INTO objects VALUES(?,?,?,?,?,?)',
                            (nid,lon,lat,json.dumps(obj['tags']),json.dumps(obj['ways']),'exit'))
                con.execute('INSERT INTO spatial VALUES(?,?,?,?,?)', (nid,lon,lon,lat,lat))
            con.commit()
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    log.info('OSM roadside index ready: %d mapped lamps/speed signs, %d labelled highway exits from %s', len(objects), len(exits), path)
    return len(objects)+len(exits)


class ObjectSource:
    def __init__(self, lanes):
        self.lanes = lanes
        self._lock = threading.Lock()
        self._requested = {}
        self._stopped = False
        self._active = False
        self._bounds = None

    def close(self):
        self._stopped = True

    def _region(self, lat, lon):
        index = self.lanes.index
        if index is None:
            try:
                with open(os.path.join(self.lanes.root,'regions.json'), encoding='utf-8') as f:
                    index = json.load(f)
                self.lanes.index = index
            except (OSError,ValueError):
                return None
        if self._bounds is None:
            bounded=[]
            for feature in index.get('features',()):
                geom=feature['geometry']
                polys=geom['coordinates'] if geom['type']=='MultiPolygon' else [geom['coordinates']]
                p=np.concatenate([np.asarray(poly[0],float) for poly in polys])
                bounded.append((p[:,0].min(),p[:,1].min(),p[:,0].max(),p[:,1].max(),feature))
            self._bounds=bounded
        candidates=[f for x0,y0,x1,y1,f in self._bounds if x0<=lon<=x1 and y0<=lat<=y1]
        r = osmlanes.region_at({'features':candidates},lat,lon)
        return r[0].replace('/','_') if r and re.fullmatch(r'[\w/-]+',r[0]) else None

    def path(self, name):
        return os.path.join(self.lanes.root, name+f'.objects.v{VERSION}.sqlite')

    def _current(self, name):
        try:
            stamp = os.stat(self.path(name)).st_mtime_ns
            pbf = os.path.join(self.lanes.root, name+'.osm.pbf')
            return not os.path.exists(pbf) or os.stat(pbf).st_mtime_ns <= stamp
        except OSError:
            return False

    def request(self, lat, lon):
        if self._stopped or not self.lanes.background:
            return
        name = self._region(lat,lon)
        if name is not None and self._current(name):
            return
        key = (math.floor(lat),math.floor(lon))
        with self._lock:
            if self._active or time.monotonic()-self._requested.get(key,-math.inf) < 300.:
                return
            self._requested[key] = time.monotonic()
            self._active = True
        def prepare():
            try:
                # Reuse the downloader and its cross-process lock, including resumable PBFs.
                if self.lanes.prepare(lat,lon) is None or self._stopped:
                    return
                name = self._region(lat,lon)
                if name is None:
                    return
                with osmlanes._process_lock(os.path.join(self.lanes.root,'objects.lock')):
                    if not self._current(name):
                        pbf = os.path.join(self.lanes.root,name+'.osm.pbf')
                        # The cache cap may have evicted the raw PBF but retained its lane DB.
                        # prepare() can then return the DB without downloading the PBF again.
                        if not os.path.exists(pbf):
                            region = osmlanes.region_at(self.lanes.index,lat,lon)
                            with self.lanes.lock, osmlanes._process_lock(os.path.join(self.lanes.root,'prepare.lock')):
                                if not os.path.exists(pbf):
                                    self.lanes._download(region[1],pbf)
                        import_pbf(pbf,self.path(name))
            except Exception as e:
                if not self._stopped:
                    log.warning('OSM roadside preparation: %s; roads continue without mapped props',e)
            finally:
                with self._lock:
                    self._active = False
        threading.Thread(target=prepare,daemon=True,name='osm-roadside-cache').start()

    def query(self, frame, box):
        return self._query(frame, box, False)

    def query_exits(self, frame, box):
        return self._query(frame, box, True)

    def _readable(self, name, exits):
        # Keep the prior scenery visible while the longer-approach index builds atomically.
        for version in range(VERSION, 1 if exits else 0, -1):
            path = os.path.join(self.lanes.root, name+f'.objects.v{version}.sqlite')
            if os.path.exists(path):
                return path, version
        return None

    def _query(self, frame, box, exits):
        x0,y0,x1,y1 = box
        lat,lon,_ = frame.enu_to_geodetic(np.array([x0,x1,x0,x1]),np.array([y0,y0,y1,y1]),0.)
        names = set()
        for la,lo in zip(lat,lon):
            name = self._region(float(la),float(lo))
            if name is None or not self._current(name):
                self.request(float(la),float(lo))
            if name and self._readable(name, exits):
                names.add(name)
        out = {}
        for name in sorted(names):
            try:
                path, version = self._readable(name, exits)
                legacy = version == 1
                with closing(sqlite3.connect(f'file:{path.replace(os.sep,"/")}?mode=ro',uri=True)) as con:
                    kind = '' if legacy else " AND o.kind"+ ("='exit'" if exits else "!='exit'")
                    rows = con.execute('SELECT o.id,o.lon,o.lat,o.tags,o.ways FROM objects o JOIN spatial s ON o.id=s.id WHERE s.maxlon>=? AND s.minlon<=? AND s.maxlat>=? AND s.minlat<=?'+kind,
                                       (float(lon.min()),float(lon.max()),float(lat.min()),float(lat.max())))
                    for nid,lo,la,tags,ways in rows:
                        decoded = json.loads(ways)
                        out[nid] = dict(id=nid,lon=lo,lat=la,tags=json.loads(tags),ways=[] if exits else decoded)
                        if exits:
                            out[nid]['exit'] = decoded[0]
            except (OSError,sqlite3.Error,ValueError) as e:
                log.debug('OSM roadside query: %s',e)
        return [out[k] for k in sorted(out)]
