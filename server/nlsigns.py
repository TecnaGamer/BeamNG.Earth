"""Optional precompiled George tiles over the existing companion connection.

No NDW/photo requests and no extraction of the individual JSON files. A store is
immutable for a server process; replace it and restart to publish a new revision.
"""
import asyncio
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
from pathlib import Path
import re
import threading
import zipfile

PREFIX = 'levels/beamng_earth/data/george_nederland/'
MAX_BYTES = 2 * 1024 * 1024
MAX_RECORDS = 5000
KEY = re.compile(r'g17_([0-9]+)_([0-9]+)\Z')


def integer(value):
    return type(value) is int


def tile_of(lat, lon):
    a = math.radians(lat)
    return (int((lon + 180) / 360 * 2**17),
            int((1 - math.asinh(math.tan(a)) / math.pi) / 2 * 2**17))


def request_keys(msg):
    """Only one NL-centered 3x3 neighborhood; never a path supplied by a client."""
    x, y, keys, request_id = (msg.get(k) for k in ('x', 'y', 'keys', 'id'))
    if not integer(request_id) or not 0 < request_id < 2**53:
        raise ValueError('invalid_request_id')
    if not integer(x) or not integer(y):
        raise ValueError('invalid_center')
    west, north = tile_of(54, 3)
    east, south = tile_of(50, 8)
    if not (west <= x <= east and north <= y <= south):
        raise ValueError('outside_netherlands')
    if not isinstance(keys, list) or not 1 <= len(keys) <= 9 or len(set(map(str, keys))) != len(keys):
        raise ValueError('invalid_keys')
    for key in keys:
        m = KEY.fullmatch(key) if isinstance(key, str) else None
        if not m or abs(int(m[1]) - x) > 1 or abs(int(m[2]) - y) > 1:
            raise ValueError('invalid_neighborhood')
    return keys


class Store:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.archives = []
        self.lock = threading.Lock()
        self.cache = OrderedDict()
        self.cache_bytes = 0
        self.jobs = 0
        self.executor = None
        try:
            manifest_file = self.directory / 'manifest.json'
            if manifest_file.is_file():
                raw = manifest_file.read_bytes()
                self.loose = True
            else:
                self.loose = False
                paths = sorted(self.directory.glob('BeamNG_Earth_Nederland_v9_data_[0-9][0-9].zip'))
                if not paths:
                    raise ValueError('No completed tile store or v9 data ZIP parts found; extract only the outer bundle')
                # Index only once, shared by every connection. Never extract tile files.
                for p in paths:
                    self.archives.append(zipfile.ZipFile(p))
                manifests = [z for z in self.archives if PREFIX + 'manifest.json' in z.namelist()]
                if len(manifests) != 1:
                    raise ValueError('Require exactly one dataset manifest')
                raw = manifests[0].read(PREFIX + 'manifest.json')
            manifest = json.loads(raw)
            if (manifest.get('kind') != 'george_v5_tile_store' or manifest.get('zoom') != 17 or
                    manifest.get('runtime_file_pattern') != 'render/g17_{x}_{y}.json'):
                raise ValueError('Unsupported George tile store')
            if not self.loose:
                self.owners = {}
                for z in self.archives:
                    for name in z.namelist():
                        if not name.startswith(PREFIX + 'render/') or not name.endswith('.json'):
                            continue
                        key = name[len(PREFIX + 'render/'):-5]
                        if not KEY.fullmatch(key) or key in self.owners:
                            raise ValueError('Invalid or duplicate tile in ZIP parts')
                        self.owners[key] = z
                if len(self.owners) != manifest['totals']['render_tiles']:
                    raise ValueError('Missing data ZIP part or mismatched tile count')
            self.revision = hashlib.sha256(raw).hexdigest()
            self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='nl-signs')
        except Exception:
            self.close()
            raise

    def close(self):
        if self.executor is not None:
            self.executor.shutdown(wait=True, cancel_futures=True)
        for z in self.archives:
            z.close()

    def _tile(self, key):
        # Called only on the one dedicated worker (including cache/ZipFile access).
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key][0]
        if self.loose:
            file = self.directory / 'render' / (key + '.json')
            if not file.is_file():
                raw = b''
            elif file.stat().st_size > MAX_BYTES:
                raise ValueError('tile_too_large')
            else:
                raw = file.read_bytes()
        else:
            z = self.owners.get(key)
            name = PREFIX + 'render/' + key + '.json'
            if z is None:
                raw = b''
            elif z.getinfo(name).file_size > MAX_BYTES:
                raise ValueError('tile_too_large')
            else:
                raw = z.read(name)
        records = []
        if raw:
            tile = json.loads(raw)
            if (not isinstance(tile, dict) or tile.get('schema_version') != 1 or tile.get('kind') != 'george_v5_render_tile' or
                    tile.get('key') != key or not isinstance(tile.get('records'), list) or
                    len(tile['records']) > MAX_RECORDS):
                raise ValueError('invalid_tile')
            seen = set()
            for r in tile['records']:
                if not isinstance(r, dict):
                    raise ValueError('invalid_source_record')
                ident, lat, lon, asset, bearing = (r.get(k) for k in ('id', 'lat', 'lon', 'asset', 'bearing_deg'))
                subs = r.get('supplementary_assets', [])
                if (not isinstance(ident, str) or not 0 < len(ident) <= 256 or ident in seen or
                        type(lat) not in (float, int) or type(lon) not in (float, int) or
                        not (50 <= lat <= 54 and 3 <= lon <= 8) or
                        tile_of(lat, lon) != tuple(map(int, KEY.fullmatch(key).groups())) or
                        type(bearing) not in (float, int) or not 0 <= bearing < 360 or
                        not isinstance(asset, str) or not 0 < len(asset) <= 128 or
                        not isinstance(subs, list) or len(subs) > 2 or
                        any(not isinstance(a, str) or not 0 < len(a) <= 128 for a in subs)):
                    raise ValueError('invalid_source_record')
                seen.add(ident)
                records.append((ident, lat, lon, asset, bearing, subs))
        self.cache[key] = (records, len(raw))
        self.cache_bytes += len(raw)
        while len(self.cache) > 128 or self.cache_bytes > 16 * 1024 * 1024:
            _, (_, size) = self.cache.popitem(last=False)
            self.cache_bytes -= size
        return records

    def response(self, msg):
        assets, indices, tiles, ids = [], {}, [], set()
        def asset_id(a):
            if a not in indices:
                assets.append(a)
                indices[a] = len(assets)   # Lua's 1-based arrays
            return indices[a]
        for key in request_keys(msg):
            rows = []
            for ident, lat, lon, asset, bearing, subs in self._tile(key):
                if ident in ids:
                    raise ValueError('duplicate_source_id')
                ids.add(ident)
                if len(ids) > MAX_RECORDS:
                    raise ValueError('neighborhood_too_dense')
                row = [ident, lat, lon, asset_id(asset), bearing]
                if subs:
                    row.append([asset_id(a) for a in subs])
                rows.append(row)
            tiles.append([key, rows])
        out = dict(type='nl_sign_tiles', v=1, id=msg['id'], revision=self.revision, assets=assets, tiles=tiles)
        if len(json.dumps(out, separators=(',', ':')).encode()) > MAX_BYTES:
            raise ValueError('response_too_large')
        return out

    async def query(self, msg):
        request_keys(msg)  # reject malformed requests before queuing any disk work
        with self.lock:
            if self.jobs >= 8:
                raise ValueError('server_busy')
            self.jobs += 1
        # Cancellation of a connection must not release capacity while its worker
        # is still running; release when the actual concurrent future completes.
        def done(_):
            with self.lock:
                self.jobs -= 1
        try:
            future = self.executor.submit(self.response, msg)
        except Exception:
            done(None)
            raise
        future.add_done_callback(done)
        return await asyncio.wrap_future(future)
