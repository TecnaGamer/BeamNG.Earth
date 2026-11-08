"""Bounded, read-only local join of OSM way IDs to nationwide route relations.

The shipped index is unpacked atomically into the existing raw-data cache once.
No Overpass calls, geometry downloads, DB writes or per-frame queries are used.
"""
from collections import OrderedDict
from contextlib import closing
from functools import lru_cache
import gzip
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import threading
import tempfile

import staging

ROOT=Path(__file__).with_name('data')/'shields'
ARCHIVE=ROOT/'route_relations.sqlite.gz'
MANIFEST=ROOT/'route_relations.manifest.json'
log=logging.getLogger('earthd.route_relations')


@lru_cache(maxsize=1)
def manifest():
    return json.loads(MANIFEST.read_text(encoding='utf-8')) if MANIFEST.is_file() else {}


class Source:
    def __init__(self, raw_dir):
        self.root=Path(raw_dir).resolve()/'route-relations'
        self.cache=OrderedDict()
        self.lock=threading.Lock()
        self.warned=False
        self.ready=True

    def _database(self):
        spec=manifest()
        if not spec or not ARCHIVE.is_file():
            return None
        target=self.root/(spec['database_sha256'][:20]+'.sqlite')
        if not target.is_file() or target.stat().st_size!=spec['database_bytes']:
            self.root.mkdir(parents=True,exist_ok=True)
            with tempfile.NamedTemporaryFile(prefix='routes-',suffix='.tmp',dir=self.root,delete=False) as output:
                temp=output.name
            try:
                digest=hashlib.sha256()
                with gzip.open(ARCHIVE,'rb') as src,open(temp,'wb') as dst:
                    while chunk:=src.read(1024*1024):
                        digest.update(chunk);dst.write(chunk)
                if digest.hexdigest()!=spec['database_sha256']:
                    raise ValueError('Bundled route relation index checksum mismatch')
                staging.replace(temp,str(target))
            finally:
                Path(temp).unlink(missing_ok=True)
        return target

    def query(self, way_ids):
        ids=sorted(set(map(int,way_ids)))
        if not ids:
            return {}
        with self.lock:
            missing=[wid for wid in ids if wid not in self.cache]
            if missing:
                try:
                    database=self._database()
                    if database is None:
                        self.ready=False
                        return {}
                    with closing(sqlite3.connect(database.as_uri()+'?mode=ro&immutable=1',uri=True)) as con:
                        for start in range(0,len(missing),500):
                            batch=missing[start:start+500]
                            rows=con.execute('SELECT m.way_id,m.route_id,m.role,r.tags FROM members m '
                                'JOIN routes r ON r.id=m.route_id WHERE m.way_id IN ('+
                                ','.join('?' for _ in batch)+') ORDER BY m.way_id,m.route_id,m.role',batch)
                            for wid in batch:self.cache[wid]=[]
                            for wid,rid,role,tags in rows:
                                self.cache[wid].append(dict(json.loads(tags),role=role,source_relation_id=rid))
                except (OSError,ValueError,sqlite3.Error) as error:
                    self.ready=False
                    for wid in missing:self.cache.pop(wid,None)
                    if not self.warned:
                        self.warned=True
                        log.warning('Local route relation index unavailable; way refs remain: %s',error)
                    return {}
            self.ready=True
            result={wid:self.cache[wid] for wid in ids if self.cache[wid]}
            for wid in ids:self.cache.move_to_end(wid)
            while len(self.cache)>10000:self.cache.popitem(last=False)
            return result
