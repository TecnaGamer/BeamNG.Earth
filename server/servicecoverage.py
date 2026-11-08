"""Bounded, local-only reader for progressively published national service cells."""
from collections import OrderedDict
import gzip
import hashlib
import json
import math
from pathlib import Path
import threading

CELL = .5
MAX_CACHE_BYTES = 128*1024*1024


def cell(lon, lat):
    return math.floor(float(lon)/CELL), math.floor(float(lat)/CELL)


def key(x, y):
    return f'{x}_{y}'


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix+'.part')
    temporary.write_text(json.dumps(value, sort_keys=True, separators=(',', ':')), encoding='utf-8')
    temporary.replace(path)


class Source:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.manifest = {}
        self.stamp = None
        self.wanted = OrderedDict()
        self.cache = OrderedDict()
        self.lock = threading.RLock()

    def version(self):
        with self.lock:
            path = self.directory/'manifest.json'
            try:
                stat = path.stat()
                stamp = stat.st_mtime_ns, stat.st_size
                if stamp != self.stamp:
                    data = json.loads(path.read_text(encoding='utf-8'))
                    if data.get('format') != 1 or data.get('cell_degrees') != CELL or not isinstance(data.get('cells'), dict):
                        raise ValueError('Unsupported national service coverage manifest')
                    self.manifest, self.stamp = data, stamp
                selected = {k:self.manifest['cells'].get(k, {}).get('sha256') for k in sorted(self.wanted)}
                return hashlib.sha256(json.dumps([self.manifest.get('built'), selected], sort_keys=True).encode()).hexdigest()
            except FileNotFoundError:
                self.manifest, self.stamp = {}, None
                return None

    def query(self, lon0, lat0, lon1, lat1):
        with self.lock:
            self.version()
            cells = self.manifest.get('cells', {})
            x0,y0 = cell(lon0,lat0)
            x1,y1 = cell(lon1,lat1)
            requested = [key(x,y) for x in range(x0,x1+1) for y in range(y0,y1+1)]
            records, covered = [], set()
            for k in requested:
                self.wanted[k] = None
                self.wanted.move_to_end(k)
                while len(self.wanted) > 64: self.wanted.popitem(last=False)
                entry = cells.get(k)
                if entry is None: continue
                digest = entry['sha256']
                cached = self.cache.get(k)
                if cached is None or cached[0] != digest:
                    path = (self.directory/entry['path']).resolve()
                    if not path.is_relative_to(self.directory): raise ValueError('Coverage path leaves dataset')
                    raw = path.read_bytes()
                    if hashlib.sha256(raw).hexdigest() != digest: raise ValueError('Service coverage checksum mismatch')
                    with gzip.GzipFile(fileobj=__import__('io').BytesIO(raw)) as f:
                        body = f.read(32*1024*1024+1)
                    if len(body) > 32*1024*1024: raise ValueError('Service coverage cell is too large')
                    data = json.loads(body)
                    if data.get('format') != 1: raise ValueError('Unsupported service coverage cell')
                    # Reserve for decoded Python dictionaries/lists as well as
                    # source text; large cells can be used without being retained.
                    cached = digest, data['objects'], 6*len(body)
                    self.cache[k] = cached
                self.cache.move_to_end(k)
                while len(self.cache) > 16 or sum(v[2] for v in self.cache.values()) > MAX_CACHE_BYTES:
                    self.cache.popitem(last=False)
                covered.add(k)
                records.extend(o for o in cached[1] if lon0 <= o['lon'] <= lon1 and lat0 <= o['lat'] <= lat1)
            return records, covered
