"""Read existing custom-build route shields without downloading regional extracts."""
from contextlib import closing
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time

log = logging.getLogger('earthd.shields')


class Source:
    def __init__(self, cache_dir):
        self.root = Path(cache_dir) / 'mapdata' / 'raw' / 'osm-lanes' if cache_dir is not None else None
        self.files = ()
        self.checked = -float('inf')
        self.lock = threading.Lock()

    def version(self):
        with self.lock:
            if time.monotonic() - self.checked >= 30.:
                files = []
                for path in sorted(self.root.glob('*.signs.v3.sqlite')) if self.root is not None else ():
                    try:
                        stat = path.stat()
                        files.append((path, stat.st_mtime_ns, stat.st_size))
                    except OSError:
                        pass
                self.files = tuple(files)
                self.checked = time.monotonic()
            return tuple((p.name, stamp, size) for p, stamp, size in self.files)

    def query(self, lon0, lat0, lon1, lat1):
        self.version()
        result = {}
        for path, _, _ in self.files:
            try:
                # mode=ro never creates or upgrades a user's existing index.
                with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=.2)) as con:
                    rows = con.execute('SELECT o.id,o.lon,o.lat,o.ways FROM objects o '
                                       'JOIN spatial s ON o.id=s.id WHERE o.kind=? '
                                       'AND s.maxlon>=? AND s.minlon<=? AND s.maxlat>=? AND s.minlat<=?',
                                       ('roadside', lon0, lon1, lat0, lat1)).fetchall()
                for oid, lon, lat, ways in rows:
                    if not (lon0 <= lon <= lon1 and lat0 <= lat <= lat1):
                        continue
                    extra = json.loads(ways)[0]
                    if extra.get('type') == 'shield':
                        result[oid] = dict(id=oid, lon=lon, lat=lat, tags={}, ways=[], roadside=extra)
            except (OSError, sqlite3.Error, ValueError, IndexError, KeyError, TypeError) as error:
                log.debug('Cached shields unavailable in %s: %s', path.name, error)
        return [result[k] for k in sorted(result)]
