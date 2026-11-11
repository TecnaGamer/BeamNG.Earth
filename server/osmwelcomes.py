"""Mapped welcome boards: Layercake spatial discovery, exact OSM node tags/positions.

Layercake POIs retain tourism but not board_type or inscription. Hydrate only the
near-border information-node IDs in small API batches, never a map/Overpass query.
Successful cells (including empty ones) are shared on disk for a week; stale data
remains usable offline. All requests use the normal fetcher and its budgets.
"""
import json
import os
from pathlib import Path
import tempfile
import time
from concurrent.futures import CancelledError

import osmfuel
import rangeread
import xproc

TTL = 7 * 86400
RETRY = 60.
BATCH = 64


class Information(osmfuel.OsmFuel):
    def cell(self, bounds, kind='demand'):
        for attempt in (0, 1):
            _, remote = self.version(kind)
            try:
                return self._information(remote, bounds, kind)
            except rangeread.Changed:
                if attempt:
                    raise
                with self._lock:
                    self._remote = None

    def _information(self, remote, bounds, kind):
        path = osmfuel.PATH
        rd, files = self._reader, {path: remote}
        con, proto, errors = rd._connect(files, kind)
        try:
            uri = f"'{proto}://{path}'"
            groups = self._groups(con, uri, remote, kind)
            candidates = self._candidates(groups, bounds)
            if not candidates:
                return []
            rd._fetch(files, {path: [r for g in candidates for c in ('bbox', 'tourism')
                                    for r in groups[g]['cols'].get(c, ())]}, kind)
            only = ' OR '.join(f"file_row_number BETWEEN {groups[g]['first']} AND "
                              f"{groups[g]['first'] + groups[g]['n'] - 1}" for g in candidates)
            x0, y0, x1, y1 = map(float, bounds)
            rows = con.execute(f'SELECT file_row_number, tourism FROM read_parquet({uri}, file_row_number=true) '
                               f'WHERE ({only}) AND bbox.xmin <= {x1} AND bbox.xmax >= {x0} '
                               f'AND bbox.ymin <= {y1} AND bbox.ymax >= {y0}').fetchall()
            hits = [int(r) for r, tourism in rows if tourism == 'information']
            if not hits:
                return []
            selected = [g for g in candidates if any(groups[g]['first'] <= r < groups[g]['first'] + groups[g]['n']
                                                     for r in hits)]
            rd._fetch(files, {path: [r for g in selected for c in ('id', 'type')
                                    for r in groups[g]['cols'].get(c, ())]}, kind)
            return sorted({int(fid) for fid, typ in con.execute(
                f'SELECT id, type FROM read_parquet({uri}, file_row_number=true) '
                f"WHERE file_row_number IN ({','.join(map(str, hits))})").fetchall() if typ == 'node'})
        except Exception:
            for error in errors:
                if isinstance(error, rangeread.Changed):
                    raise error
            raise
        finally:
            con.close()


class Source:
    def __init__(self, md):
        self.fetcher = md.fetcher
        self.info = Information(md.raw_dir, md.fetcher)
        self.directory = Path(md.cache_dir) / 'osm-welcomes-v1'
        self.failed = {}

    def cell(self, key):
        target = self.directory / str(key) / 'nodes.json'

        def read(fresh=False):
            try:
                if fresh and int(target.stat().st_mtime) // TTL != int(time.time()) // TTL:
                    return None
                return json.loads(target.read_text(encoding='utf-8'))['nodes']
            except (OSError, ValueError, KeyError):
                return None

        cached = read(True)
        if cached is not None:
            return cached
        if time.monotonic() - self.failed.get(key, -float('inf')) < RETRY:
            stale = read()
            if stale is not None:
                return stale
            raise OSError('Welcome-node source awaiting retry')
        lock = str(target) + '.lock'
        cached, mine = xproc.once(lock, lambda: read(True), 10., kind='demand')
        if cached is not None:
            return cached
        try:
            ids = self.info.cell(key.bounds())
            if len(ids) > 256:
                raise ValueError('Too many information nodes in one welcome-sign cell')
            nodes = []
            for start in range(0, len(ids), BATCH):
                batch = ids[start:start + BATCH]
                response = self.fetcher.get('https://api.openstreetmap.org/api/0.6/nodes.json?nodes=' +
                                            ','.join(map(str, batch)), kind='demand', lane='welcome-signs')
                elements = json.loads(response.body)['elements']
                if {e['id'] for e in elements if e.get('type') == 'node'} != set(batch):
                    raise ValueError('Incomplete welcome-node response')
                nodes.extend(dict(id=e['id'], lon=float(e['lon']), lat=float(e['lat']), tags=e.get('tags', {}))
                             for e in elements if e.get('tags', {}).get('board_type') == 'welcome_sign')
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(dir=target.parent, suffix='.tmp')
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    json.dump({'nodes': nodes, 'license': 'ODbL 1.0; OpenStreetMap contributors'}, stream)
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return nodes
        except CancelledError:
            raise
        except Exception:
            self.failed[key] = time.monotonic()
            stale = read()
            if stale is not None:
                return stale
            raise
        finally:
            if mine:
                xproc.release(lock)
