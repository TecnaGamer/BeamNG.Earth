"""Background exit destinations and milestone indexes; cached legacy sign indexes remain readable."""
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
import osmregions
import exitsigns
import milemarkers
import throughsigns

log = logging.getLogger('earthd.osmsigns')
VERSION = 4                         # compatible with the legacy whole-mile reference schema
INDEX_SUFFIX = '.signs.v2.sqlite'    # v2 adds mainline destinations; retain prior indexes while rebuilding
TAG_KEYS = ('highway',) + exitsigns.EXIT_KEYS + milemarkers.TAG_KEYS


def tile_exit_objects(md, frame, tx, ty):
    """Guide-sign and mile-reference sources overlapping this tile (including shoulders)."""
    source = getattr(md, 'objects', None)
    import roads
    x0, y0, x1, y1 = roads.tile_bbox(tx, ty)
    pad = milemarkers.QUERY_MARGIN
    marker_box = (x0-pad,y0-pad,x1+pad,y1+pad)
    out = milemarkers.query(frame, marker_box)
    if source is None:
        return out
    if hasattr(source, 'query_mile_markers'):
        out += source.query_mile_markers(frame, marker_box)
    margin = exitsigns.QUERY_MARGIN
    if hasattr(source, 'query_exits'):
        out += source.query_exits(frame, (x0 - margin, y0 - margin, x1 + margin, y1 + margin))
    if hasattr(source, 'query_through'):
        out += source.query_through(frame, (x0 - margin, y0 - margin, x1 + margin, y1 + margin))
    return out


def import_pbf(path, target):
    """Bounded streaming passes: objects, road memberships, neighbours, route metadata.

    Keep only tagged objects and adjacent way vertices in memory, never a full node cache.
    Directional sign tags refer to their actual OSM way, not a nearby opposite carriageway.
    """
    import osmium
    needed = set()
    main_ways, links, junctions, milestones = [], [], {}, {}
    for n in osmium.FileProcessor(str(path), osmium.osm.NODE).with_filter(osmium.filter.KeyFilter(*TAG_KEYS)):
        tags = {k:v for k,v in n.tags if k in TAG_KEYS}
        if n.location.valid() and tags.get('highway') == 'motorway_junction':
            junctions[n.id] = tags
        elif n.location.valid() and tags.get('highway') == 'milestone':
            milestones[n.id] = (n.location.lon,n.location.lat,tags)
    for w in osmium.FileProcessor(str(path), osmium.osm.WAY).with_filter(osmium.filter.KeyFilter('highway')):
        refs = [n.ref for n in w.nodes]
        highway = w.tags.get('highway')
        if highway in exitsigns.MAIN_CLASSES or highway in exitsigns.LINK_CLASSES:
            wt = {k:v for k,v in w.tags if k in throughsigns.WAY_KEYS + milemarkers.WAY_KEYS}
            (main_ways if highway in exitsigns.MAIN_CLASSES else links).append((w.id, refs, wt))
            needed.update(refs)
    neighbours = {}
    if needed:
        for n in osmium.FileProcessor(str(path), osmium.osm.NODE).with_filter(osmium.filter.IdFilter(needed)):
            if n.location.valid():
                neighbours[n.id] = (n.location.lon, n.location.lat)
    exits = exitsigns.index_records(main_ways, links, neighbours, junctions)
    route_ways, relations = [], {}
    main_ids = {wid for wid, _, _ in main_ways}
    for r in osmium.FileProcessor(str(path), osmium.osm.RELATION).with_filter(osmium.filter.KeyFilter('route')):
        if r.tags.get('route') != 'road':
            continue
        ref = str(r.tags.get('ref',''))
        direction = milemarkers.cardinal(r.tags.get('direction'))
        state = r.tags.get('is_in:state','')
        for m in r.members:
            if m.type != 'w' or m.ref not in main_ids:
                continue
            posted = throughsigns.cardinal(m.role) or throughsigns.cardinal(r.tags.get('direction'))
            if posted:
                relations.setdefault(m.ref, []).append(dict(direction=posted, role=m.role))
            if milestones and r.tags.get('network') == 'US:I' and re.fullmatch(r'\d{1,3}',ref):
                route_ways.append((m.ref,int(ref),direction,state))
    throughsigns.annotate_exits(exits, relations)
    through = throughsigns.index_records(main_ways, neighbours, exits, relations)
    mileposts = milemarkers.osm_records(main_ways, neighbours, milestones, route_ways)
    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    tmp = f'{target}.{os.getpid()}.{threading.get_ident()}.tmp'
    try:
        with closing(sqlite3.connect(tmp)) as con:
            con.execute("CREATE TABLE objects(id INTEGER PRIMARY KEY, lon REAL, lat REAL, tags TEXT, ways TEXT, kind TEXT DEFAULT 'point')")
            con.execute('CREATE VIRTUAL TABLE spatial USING rtree(id, minlon, maxlon, minlat, maxlat)')
            for obj in exits + through:
                nid, lon, lat = obj['id'], obj['lon'], obj['lat']
                con.execute('INSERT INTO objects VALUES(?,?,?,?,?,?)',
                            (nid,lon,lat,json.dumps(obj['tags']),json.dumps(obj['ways']),obj['kind']))
                con.execute('INSERT INTO spatial VALUES(?,?,?,?,?)', (nid,lon,lon,lat,lat))
            for obj in mileposts:
                nid,lon,lat = obj['id'],obj['lon'],obj['lat']
                con.execute('INSERT INTO objects VALUES(?,?,?,?,?,?)',
                            (nid,lon,lat,'{}',json.dumps([obj['mile_marker']]),'mile_marker'))
                con.execute('INSERT INTO spatial VALUES(?,?,?,?,?)',(nid,lon,lon,lat,lat))
            con.commit()
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    log.info('OSM sign index ready: %d exits, %d overhead destinations, %d whole-mile references from %s',
             len(exits), len(through), len(mileposts), path)
    return len(exits)+len(through)+len(mileposts)


class ObjectSource:
    def __init__(self, regions):
        self.regions = regions
        self._lock = threading.Lock()
        self._requested = {}
        self._stopped = False
        self._active = False
        self._bounds = None

    def close(self):
        self._stopped = True
        self.regions.close()

    def _region(self, lat, lon):
        index = self.regions.index
        if index is None:
            try:
                with open(os.path.join(self.regions.root,'regions.json'), encoding='utf-8') as f:
                    index = json.load(f)
                self.regions.index = index
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
        r = osmregions.region_at({'features':candidates},lat,lon)
        return r[0].replace('/','_') if r and re.fullmatch(r'[\w/-]+',r[0]) else None

    def path(self, name):
        return os.path.join(self.regions.root, name+INDEX_SUFFIX)

    def _current(self, name):
        try:
            path = self.path(name)
            if not os.path.exists(path):
                # An older index stays visible, but needs a background upgrade for
                # destination records. Offline play can continue using that index.
                found = self._readable(name, True)
                if self.regions.background or not found:
                    return False
                path = found[0]
            stamp = os.stat(path).st_mtime_ns
            pbf = os.path.join(self.regions.root, name+'.osm.pbf')
            return not os.path.exists(pbf) or os.stat(pbf).st_mtime_ns <= stamp
        except OSError:
            return False

    def request(self, lat, lon):
        if self._stopped or not self.regions.background:
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
                if self.regions.prepare(lat,lon) is None or self._stopped:
                    return
                name = self._region(lat,lon)
                if name is None:
                    return
                with osmregions._process_lock(os.path.join(self.regions.root,'objects.lock'), self.regions._stop):
                    if not self._current(name):
                        pbf = os.path.join(self.regions.root,name+'.osm.pbf')
                        import_pbf(pbf,self.path(name))
            except Exception as e:
                if not self._stopped:
                    log.warning('OSM sign preparation: %s; roads continue without OSM signs',e)
            finally:
                with self._lock:
                    self._active = False
        threading.Thread(target=prepare,daemon=True,name='osm-sign-cache').start()

    def query_exits(self, frame, box):
        return self._query(frame, box, True)

    def query_mile_markers(self, frame, box):
        return self._query(frame, box, 'mile_marker')

    def query_through(self, frame, box):
        return self._query(frame, box, 'through')

    def _readable(self, name, exits):
        # Keep the prior scenery visible while the longer-approach index builds atomically.
        path = self.path(name)
        if os.path.exists(path):
            return path, VERSION
        path = os.path.join(self.regions.root, name+'.signs.v1.sqlite')
        if os.path.exists(path):
            return path, VERSION
        oldest = 3 if exits == 'mile_marker' else 1 if exits else 0
        for version in range(VERSION, oldest, -1):
            path = os.path.join(self.regions.root, name+f'.objects.v{version}.sqlite')
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
                    if legacy and exits == 'through':
                        continue
                    record_kind = exits if isinstance(exits, str) else 'exit' if exits else 'point'
                    kind = '' if legacy else " AND o.kind='"+record_kind+"'"
                    rows = con.execute('SELECT o.id,o.lon,o.lat,o.tags,o.ways FROM objects o JOIN spatial s ON o.id=s.id WHERE s.maxlon>=? AND s.minlon<=? AND s.maxlat>=? AND s.minlat<=?'+kind,
                                       (float(lon.min()),float(lon.max()),float(lat.min()),float(lat.max())))
                    for nid,lo,la,tags,ways in rows:
                        decoded = json.loads(ways)
                        out[nid] = dict(id=nid,lon=lo,lat=la,tags=json.loads(tags),ways=[] if exits else decoded)
                        if exits == 'mile_marker':
                            out[nid]['mile_marker'] = decoded[0]
                        elif exits == 'through':
                            out[nid]['through'] = decoded[0]
                        elif exits:
                            out[nid]['exit'] = decoded[0]
            except (OSError,sqlite3.Error,ValueError) as e:
                log.debug('OSM sign query: %s',e)
        return [out[k] for k in sorted(out)]
