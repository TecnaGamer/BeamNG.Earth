"""Bounded, readiness-gated lookahead for the Norwegian sign stream."""
import math
import os


def _corridor(x, y, velocity, width, radius):
    """Cells on the motion ray, in encounter order, including the current cell."""
    tx, ty = math.floor(x / width), math.floor(y / width)
    result = [(tx, ty)]
    vx, vy = velocity
    speed = math.hypot(vx, vy)
    if not math.isfinite(speed) or speed < 2:
        return result
    dx, dy = vx / speed, vy / speed
    limit = min(speed * 15, 480, radius)
    sx, sy = (1 if dx > 0 else -1), (1 if dy > 0 else -1)
    while len(result) < 3:
        ex = ((tx + (sx > 0)) * width - x) / dx if dx else math.inf
        ey = ((ty + (sy > 0)) * width - y) / dy if dy else math.inf
        distance = min(ex, ey)
        if distance > limit:
            break
        if ex <= ey:
            tx += sx
        if ey <= ex:
            ty += sy
        result.append((tx, ty))
    return result


def tracker_class(base, roads):
    class Tracker(base):
        def __init__(self):
            super().__init__()
            self._held = {}
            self._ready = set()
            self._route = []
            self._early = set()

        def close(self):
            self._held.clear()
            self._early.clear()
            super().close()

        def refresh(self):
            super().refresh()
            self._held.clear()

        def on_pose(self, conn, aid, x, y, loop, build, ready, version=None):
            if self.closed:
                return
            if version != self.version:
                self.refresh()
                self.version = version
            if aid != self.aid:
                self._held.clear()

            self._ready = set(ready)
            self._route = []
            server = conn.s
            anchor = getattr(server, 'anchors', {}).get(aid)
            if (anchor is not None and not getattr(conn, 'map_view', False)
                    and not getattr(conn, 'map_open', False)
                    and os.environ.get('BNE_NVDB_DESTINATIONS_V07', 'on').lower() not in ('0','off','false','no')
                    and os.environ.get('BNE_NORWAY_SIGNS_V06', 'on').lower() not in ('0','off','false')
                    and str(server.country(anchor)).upper() == 'NO' and math.isfinite(x) and math.isfinite(y)):
                self._route = _corridor(x, y, getattr(conn, 'vel', (0, 0)), roads.TILE, roads.SEND_M)

            wanted = self._ready.union(self._route)
            for key in tuple(self._held):
                if key not in wanted or not self._route:
                    del self._held[key]
            # Failed or empty speculative builds get one normal attempt when ready.
            for key in self._ready.intersection(self._held):
                if self._held[key] is None:
                    del self._held[key]

            super().on_pose(conn, aid, x, y, loop, build, wanted, version)
            # Publishing uses the original inline JSON, shape pool and drop protocol.
            for key in self._ready.intersection(self._held):
                super()._built(conn, aid, key, self.revision, self._held.pop(key))

        def _job_started(self, key, future):
            if key not in self._ready and key in self._route:
                self._early.add(future)

        def _order_tiles(self, want, x, y):
            if not self._route:
                want.sort(key=lambda k: roads.tile_distance(*k, x, y))
                return
            want[:] = [k for k in want if k not in self._held]
            def rank(key):
                distance = roads.tile_distance(*key, x, y)
                return (key != self._route[0], distance * (0.35 if key in self._route else 1),
                        key not in self._route, key)
            want.sort(key=rank)

        def _built(self, conn, aid, key, revision, future):
            early = future in self._early
            self._early.discard(future)
            if not early and not (key in self._route and key not in self._ready and key not in self.sent):
                return super()._built(conn, aid, key, revision, future)
            self.jobs.pop((aid, key), None)
            if future.cancelled():
                return
            error = future.exception()
            if self.closed or aid != self.aid or revision != self.revision or key not in self.wanted:
                return
            result = None if error is not None else future.result()
            if key not in self._ready:
                if key in self._route:
                    self._held[key] = future if result and result[1] else None
                return
            if result and result[1]:
                super()._built(conn, aid, key, revision, future)
            # Empty/failed early attempt finishing after readiness: retry on next pose.

    return Tracker
