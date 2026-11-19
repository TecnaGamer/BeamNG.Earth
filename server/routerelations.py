"""Weekly route memberships, fetched through pagesdata and verified before use.

Only the local cache contains the manifest and database. Failed updates retain
an already verified database; missing memberships let callers use way refs.
"""
from collections import OrderedDict
from concurrent.futures import CancelledError
from contextlib import closing
import gzip
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time

import pagesdata

log = logging.getLogger('earthd.route_relations')
MAX_DATABASE_BYTES = 256 * 1024 * 1024


def validate(spec):
    if not isinstance(spec, dict) or spec.get('format') != 1:
        raise ValueError('Unsupported route membership manifest')
    digest = spec.get('database_sha256', '')
    if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
        raise ValueError('Invalid route membership checksum')
    if spec.get('file') != f'route_relations-{digest}.sqlite.gz':
        raise ValueError('Invalid route membership filename')
    size = spec.get('database_bytes')
    if type(size) is not int or not 0 < size <= MAX_DATABASE_BYTES:
        raise ValueError('Invalid route membership size')
    return digest, size


class Source:
    def __init__(self, raw_dir, fetcher=None):
        self.manifest = pagesdata.Manifest(str(raw_dir), fetcher, 'routes',
                                           key=lambda m: m.get('database_sha256'))
        self.root = Path(self.manifest.dir)
        self.cache = OrderedDict()
        self.lock = threading.Lock()
        self.ready = True
        self.active = None
        self.digest = None
        self.checked = set()
        self.failed = None
        self.retry_at = 0.
        self.warned = False

    def version(self):
        """Last verified build, without starting a network request."""
        if self.digest is not None:
            return self.digest
        try:
            spec = json.loads((self.root / 'verified.json').read_text(encoding='utf-8'))
            return validate(spec)[0]
        except (OSError, ValueError):
            return None

    def prepare(self):
        """Refresh/verify before consulting derived candidate caches."""
        with self.lock:
            database = self._database()
            self.ready = database is not None
            return self.digest if self.ready else None

    def _verify(self, path, digest, size):
        if path.stat().st_size != size:
            raise ValueError('Route membership database size mismatch')
        checksum = hashlib.sha256()
        with path.open('rb') as src:
            while chunk := src.read(1024 * 1024):
                checksum.update(chunk)
        if checksum.hexdigest() != digest:
            raise ValueError('Route membership database checksum mismatch')
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as con:
            if con.execute('PRAGMA quick_check').fetchone() != ('ok',):
                raise ValueError('Route membership database integrity check failed')
            row = con.execute('SELECT m.way_id,m.route_id,m.role,r.tags FROM members m '
                              'JOIN routes r ON r.id=m.route_id LIMIT 1').fetchone()
            if row is None:
                raise ValueError('Empty route membership database')

    def _database(self):
        spec = (self.manifest.current() if self.manifest.fetcher is not None else
                pagesdata.read_pin(str(self.root))[0])
        try:
            digest, size = validate(spec)
            if digest == self.failed and time.monotonic() < self.retry_at:
                return self.active
            target = (self.root / f'{digest}.sqlite').resolve()
            if digest not in self.checked:
                try:
                    self._verify(target, digest, size)
                except (OSError, ValueError, sqlite3.Error):
                    archive = pagesdata.fetch_data(self.manifest.fetcher, pagesdata.data_url(spec),
                                                   self.manifest.base, spec['file'])
                    with gzip.GzipFile(fileobj=io.BytesIO(archive)) as src:
                        body = src.read(size + 1)
                    if len(body) != size or hashlib.sha256(body).hexdigest() != digest:
                        raise ValueError('Downloaded route membership checksum/size mismatch')
                    self.root.mkdir(parents=True, exist_ok=True)
                    fd, name = tempfile.mkstemp(prefix='routes-', suffix='.part', dir=self.root)
                    temp = Path(name)
                    try:
                        with os.fdopen(fd, 'wb') as dst:
                            dst.write(body)
                        self._verify(temp.resolve(), digest, size)
                        # Concurrent processes publish only complete verified bytes.
                        pagesdata.write_atomic(str(target), body)
                    finally:
                        temp.unlink(missing_ok=True)
                self.checked.add(digest)
            if digest != self.digest:
                self.cache.clear()
                self.active, self.digest = target, digest
                pagesdata.write_atomic(str(self.root / 'verified.json'), json.dumps(spec).encode('utf-8'))
            self.failed = None
            self.warned = False
            return target
        except CancelledError:
            raise
        except Exception as error:
            self.failed = spec.get('database_sha256') if isinstance(spec, dict) else None
            self.retry_at = time.monotonic() + pagesdata.RETRY_S
            if not self.warned:
                self.warned = True
                log.warning('Weekly route memberships unavailable; cached data/way refs remain: %s', error)
            if self.active is None:
                try:
                    good = json.loads((self.root / 'verified.json').read_text(encoding='utf-8'))
                    digest, size = validate(good)
                    target = (self.root / f'{digest}.sqlite').resolve()
                    self._verify(target, digest, size)
                    self.active, self.digest = target, digest
                    self.checked.add(digest)
                except (OSError, ValueError, sqlite3.Error):
                    pass
            return self.active

    def query(self, way_ids):
        ids = sorted(set(map(int, way_ids)))
        if not ids:
            return {}
        with self.lock:
            database = self._database()
            self.ready = database is not None
            if not self.ready:
                return {}
            missing = [wid for wid in ids if wid not in self.cache]
            try:
                if missing:
                    with closing(sqlite3.connect(database.as_uri() + '?mode=ro&immutable=1', uri=True)) as con:
                        for start in range(0, len(missing), 500):
                            batch = missing[start:start + 500]
                            rows = con.execute('SELECT m.way_id,m.route_id,m.role,r.tags FROM members m '
                                               'JOIN routes r ON r.id=m.route_id WHERE m.way_id IN (' +
                                               ','.join('?' for _ in batch) +
                                               ') ORDER BY m.way_id,m.route_id,m.role', batch)
                            for wid in batch:
                                self.cache[wid] = []
                            for wid, rid, role, tags in rows:
                                self.cache[wid].append(dict(json.loads(tags), role=role, source_relation_id=rid))
                result = {wid: self.cache[wid] for wid in ids if self.cache[wid]}
                for wid in ids:
                    self.cache.move_to_end(wid)
                while len(self.cache) > 10000:
                    self.cache.popitem(last=False)
                return result
            except (OSError, ValueError, sqlite3.Error):
                self.ready = False
                self.checked.discard(self.digest)
                self.active = None
                for wid in missing:
                    self.cache.pop(wid, None)
                return {}
