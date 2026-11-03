"""Highway signs (from cody196's PR #363): optional scenery built per road tile after the tile's roads were sent,
never part of their collision or readiness. A sign tile is sent as a `roads_tile` with key "s<tx>_<ty>" and the
fields exits / mileposts / merges / speeds (render-only shapes, pooled slots); the mod's highwaySigns setting hides them.

Every guide sign face is drawn to the US MUTCD (green guide panels, Interstate / US / state shields, the yellow merge
diamond, Interstate mile markers): those are built where signstates finds a US state (BNE_SIGNS_WORLD=1: everywhere,
for tests), elsewhere no sign data is asked for. The speed-limit signs (speedsigns.py, field speeds: the AI's limit, in
each region's style) are built everywhere."""
from runtimeprofiler import ThreadPoolExecutor
import json
import logging
import os
import time

import numpy as np

import highwaysigns
import mergesigns
import roadmesh
import roads
import signstates
import speedsigns
import staging

log = logging.getLogger('earthd.signs')
KINDS = ('exits', 'mileposts', 'merges', 'speeds')
WORKERS = 1                     # sign tiles in flight per connection (Tracker): a road tile waits behind one at most
WORLD = os.environ.get("BNE_SIGNS_WORLD") == "1"


def shape_files(aid, tx, ty):
    return {kind: f'{kind}_{aid}_{tx}_{ty}.cdae' for kind in KINDS}


def write_tile(out_dir, vfs_dir, aid, tx, ty, meshes):
    x, y, _, _ = roads.tile_bbox(tx, ty)
    data = {'v': roads.VERSION, 'chunks': []}
    for kind, mesh in zip(KINDS, meshes):
        if mesh is None:
            continue
        name = shape_files(aid, tx, ty)[kind]
        roadmesh.write(os.path.join(out_dir, name), mesh)
        data[kind] = {'shape': f'{vfs_dir}/{name}', 'pos': [x, y, 0.]}
    # An empty replacement is intentional: it retires signs from an older index.
    name = f'signs_{aid}_{tx}_{ty}.json'
    path = os.path.join(out_dir, name)
    tmp = staging.tmp_for(path)
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(json.dumps(data, separators=(',', ':')))
    staging.replace(tmp, path)
    return f'{vfs_dir}/{name}', len(data) - 2


def build_tile(md, frame, aid, tx, ty, elev, out_dir, vfs_dir, urban=None, widths=None,
               left_hand=False, mph=True, country=None):
    source = getattr(md, 'highway_signs', None)
    if source is None:
        return None
    x0, y0, x1, y1 = roads.tile_bbox(tx, ty)
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x1]), np.array([y0, y1]), 0.)
    us = bool((signstates.states(la, lo) != '').any())
    context = source.context(urban, widths)
    feats = context.around(np.c_[lo, la])
    # Conservative deck footprints exclude ground signs on/under overpasses without
    # rebuilding road meshes or solving bridge heights in this optional queue.
    decks = []
    for i, spec in enumerate(roads.bridge_specs(feats)):
        if spec is None:
            continue
        for ring in roads.bridge_parts(feats, i):
            if len(ring) >= 2:
                pts = np.column_stack(frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.))
                decks.append({'deck': True, 'n': pts, 'w': spec.width})
    speeds = _speeds(frame, tx, ty, elev, context, decks, left_hand, mph, speedsigns.style(country, us))
    if not (WORLD or us):
        return write_tile(out_dir, vfs_dir, aid, tx, ty, (None, None, None, speeds))     # (not the US: no guide signs)
    exits, miles = highwaysigns.build_tile(md, frame, tx, ty, elev, decks, left_hand, mph, urban, widths)
    merges = mergesigns.build_tile(frame, tx, ty, [feats], elev, decks, urban, widths, left_hand, context)
    return write_tile(out_dir, vfs_dir, aid, tx, ty, (exits, miles, merges, speeds))


_warned = -float('inf')


def _speeds(frame, tx, ty, elev, context, decks, left_hand, mph, face):
    """speedsigns.build_tile, or None: optional scenery never stops the other signs."""
    global _warned
    try:
        return speedsigns.build_tile(frame, tx, ty, elev, context, decks, left_hand, mph, face)
    except Exception as error:
        now = time.monotonic()
        if now - _warned >= 60.:
            _warned = now
            log.warning('Speed-limit signs unavailable; roads continue: %s: %s', type(error).__name__, error)
        return None


class Tracker:
    """Bounded sign jobs for ready road tiles; old results cannot survive reset/rebase."""
    def __init__(self):
        self.aid = None
        self.sent, self.stale = set(), set()
        self.jobs, self.retry = {}, {}
        self.pool = None
        self.revision = 0
        self.closed = False
        self.wanted = set()
        self.version = None

    def close(self):
        self.closed = True
        for future in self.jobs.values():
            future.cancel()
        self.jobs.clear()
        if self.pool is not None:
            self.pool.shutdown(wait=False, cancel_futures=True)

    def refresh(self):
        self.revision += 1
        self.stale.update(self.sent)
        self.retry.clear()

    def on_pose(self, conn, aid, x, y, loop, build, ready, version=None):
        """ready: the road tiles the mod has (roads.Tracker.sent); version: the sign data's build known now
        (SignTags.known(): no network): a new one rebuilds the tiles sent."""
        if self.closed:
            return
        if version != self.version:         # (None -> a build too: the tiles sent before had no sign data)
            self.refresh()
            self.version = version
        if aid != self.aid:
            # Keep running old jobs in the bounded queue until they finish; callbacks
            # discard their results. Repeated teleports cannot grow an executor backlog.
            self.aid = aid
            self.sent.clear()
            self.stale.clear()
            self.retry.clear()
        self.wanted = set(ready)
        for key in self.sent - self.wanted:
            conn.send({'type': 'roads_drop', 'anchorId': aid, 'key': f's{key[0]}_{key[1]}'})
        self.sent.intersection_update(self.wanted)
        self.stale.intersection_update(self.wanted)
        self.retry = {k: t for k, t in self.retry.items() if k in self.wanted}
        now = time.monotonic()
        want = [k for k in self.wanted if (k not in self.sent or k in self.stale)
                and (aid, k) not in self.jobs and self.retry.get(k, 0.) <= now]
        want.sort(key=lambda k: roads.tile_distance(*k, x, y))
        if want and self.pool is None:
            self.pool = ThreadPoolExecutor(WORKERS, thread_name_prefix='highway-signs')
        for key in want[:max(0, WORKERS - len(self.jobs))]:
            future = loop.run_in_executor(self.pool, build, aid, *key)
            self.jobs[aid, key] = future
            future.add_done_callback(lambda f, a=aid, k=key, r=self.revision: self._built(conn, a, k, r, f))

    def _built(self, conn, aid, key, revision, future):
        self.jobs.pop((aid, key), None)
        if future.cancelled():
            return
        try:
            result = future.result()
        except Exception:
            if not self.closed and aid == self.aid and key in self.wanted:
                self.retry[key] = time.monotonic() + 10.
                log.warning('Highway signs %s unavailable; road tile retained', key, exc_info=True)
            return
        if self.closed or aid != self.aid or key not in self.wanted:
            return
        if revision != self.revision:
            self.stale.add(key)
            return
        if result is None:
            self.retry[key] = time.monotonic() + 10.
            return
        path, n = result
        if n == 0 and key not in self.stale:    # (nothing to show, none shown before: no message)
            self.sent.add(key)
            return
        data = staging.inline_json(conn.s.out_dir, path)
        message = {'type': 'roads_tile', 'anchorId': aid, 'key': f's{key[0]}_{key[1]}',
                   'path': path, 'n': n}
        if data is not None:
            message['data'] = data
        pool = getattr(conn.s, 'pool_shapes', None)
        if pool is not None and data is not None:
            # Old optional mesh files may remain on disk after an empty replacement.
            # Pool only the fields in this tile, never those stale files.
            files = {k: v for k, v in shape_files(aid, *key).items() if k in data}
            slots = pool(conn, files)
            if slots:
                message['slots'] = slots
        conn.send(message)
        self.sent.add(key)
        self.stale.discard(key)
