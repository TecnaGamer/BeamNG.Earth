"""Opt-in local WKD/OSM context reads through the existing public data host.

Data access only: mast planning and context matching belong in earthfast. This
reader is not enabled by constructing it, and does not alter stock signals.
"""
from collections import OrderedDict
import math
import threading

import dmath
import railtags

FORMAT, ZOOM = 1, 14


def tile(lon, lat):
    return (math.floor((lon + 180.) / 360. * 2 ** ZOOM),
            math.floor((1. - dmath.log(dmath.tan(math.pi / 4 + lat * math.pi / 360.)) / math.pi) / 2 * 2 ** ZOOM))


def valid(row):
    if not isinstance(row, dict) or row.get('kind') not in ('road', 'signal', 'urban_section'):
        return False
    ident, geometry = row.get('id'), row.get('geometry')
    if not isinstance(ident, str) or not ident.isascii() or not ident.isdecimal() or len(ident) > 20:
        return False
    if not isinstance(geometry, list) or not 1 <= len(geometry) <= 65535:
        return False
    if row['kind'] == 'signal' and len(geometry) != 1 or row['kind'] != 'signal' and len(geometry) < 2:
        return False
    return all(isinstance(p, list) and len(p) == 2
               and all(type(v) in (int, float) and math.isfinite(v) for v in p)
               and 3 <= p[0] <= 8 and 50 <= p[1] <= 54 for p in geometry)


class Source(railtags.Shards):
    def __init__(self, cache_dir, fetcher, enabled=False):
        super().__init__(cache_dir, fetcher, 'nl-signal-context')
        self.enabled = bool(enabled)
        self.shards = OrderedDict()
        self.query_lock = threading.Lock()

    def query(self, bounds):
        """Return a bounded context neighbourhood; duplicate IDs never double up.

        Context records intentionally include a tile-edge halo. Do not clip a
        road at the query rectangle: matching needs its complete local segment.
        """
        if not self.enabled:
            return []
        west, south, east, north = map(float, bounds)
        if not all(math.isfinite(v) for v in (west, south, east, north)) or west > east or south > north:
            raise ValueError('signal context bounds')
        if east < 3 or west > 8 or north < 50 or south > 54:
            return []
        ax, ay = tile(max(west, 3), min(north, 54))
        bx, by = tile(min(east, 8), max(south, 50))
        if (bx - ax + 1) * (by - ay + 1) > 64:
            raise ValueError('signal context query exceeds tile neighbourhood')
        if self.version() is None:
            return []
        found, conflicts = {}, set()
        with self.query_lock:
            for x in range(ax, bx + 1):
                for y in range(ay, by + 1):
                    shard = self.shard(x, y)
                    if not isinstance(shard, dict) or shard.get('format') != FORMAT:
                        continue
                    rows = shard.get('records')
                    if not isinstance(rows, list) or len(rows) > 100000:
                        continue
                    for row in rows:
                        if not valid(row):
                            continue
                        key = row['kind'], row['id']
                        old = found.get(key)
                        if old is not None and old != row:
                            conflicts.add(key)
                        found[key] = row
                    with self.lock:
                        if (x, y) in self.shards:
                            self.shards.move_to_end((x, y))
                        while len(self.shards) > 16:
                            self.shards.popitem(last=False)
        return [found[key] for key in sorted(found) if key not in conflicts]
