"""Opt-in, connection-owned native signal jobs. One worker, bounded snapshots.

Nothing imports this module, reads context data or creates a worker in stock
mode. Existing navgraphs and fully sent road geotiles supply the inputs.
"""
import asyncio
import copy
from collections import deque
from functools import lru_cache
import json
import logging
from pathlib import Path
import time

import geotiles
import nlsignalbuild
from nlsignaldata import Source
from nlsignalsnapshot import Snapshot
from nlsignalassembly import SurfacePending
import staging

log = logging.getLogger('earthd.nl-signals')

@lru_cache(maxsize=1)
def catalog():
    return json.loads((Path(__file__).with_name('data') / 'groenpol_components.json').read_text())


class Tracker:
    def __init__(self):
        self.state = Snapshot()
        self.source = self.pool = self.future = None
        self.signature = self.retry_at = None
        self.closed = False
        self.graph_signature = None
        self.groups = {}
        self.offered_groups = ()
        self.files = deque()
        self.stats = dict(builds=0, pending=0, failures=0, accepted=0)

    def waiting(self, reason):
        if self.stats.get('waiting') != reason:
            self.stats['waiting'] = reason
            log.info('native signals waiting: %s', reason)

    def close(self):
        self.closed = True
        self.state.reset()
        if self.pool is not None:
            self.pool.shutdown(wait=False, cancel_futures=True)

    def acknowledge(self, msg):
        owners=msg.get('owners')
        if isinstance(owners,dict) and not owners:owners=[]  # Lua's empty table may encode as {}
        if owners is not None:
            if not isinstance(owners,list) or any(not isinstance(o,str) for o in owners):return None
            chosen=set(owners)
            if any(chosen.intersection(group) and not group.issubset(chosen) for group in self.offered_groups):
                return None
        accepted = self.state.acknowledge(msg.get('epoch'), msg.get('anchorId'), msg.get('revision'),owners)
        if accepted is not None:
            self.stats['accepted'] += 1
        return accepted

    def on_pose(self, conn, msg):
        aid = conn.active
        on = msg.get('nlSignals') == 1 and msg.get('nlSignals') is not True
        if not on or aid is None:
            if self.state.enabled:
                self.state.reset(); self.signature = None; self.groups = {}
                nav = getattr(conn, 'nav', None)
                if nav is not None and getattr(nav, 'path', None) and nav.aid == aid:
                    conn.send(dict(type='navgraph', anchorId=aid,
                                   serial=getattr(nav,'ready_serial',nav.serial),
                                   path=nav.path,nodes=0,edges=0))
                self.graph_signature = None
                self.source = None
                if self.pool is not None:
                    self.pool.shutdown(wait=False,cancel_futures=True)
                    self.pool = None
            return
        if not self.state.enabled or self.state.anchor != aid:
            self.state.reset(True, aid)
            self.signature = self.graph_signature = None
            self.groups = {}
        # A prepared next block does not invalidate the committed block. Its
        # exact client terrain token below determines which geometry is usable.
        if self.closed or not getattr(conn.roads, 'geo', False):
            self.waiting('closed' if self.closed else 'road_mode')
            return
        nav = conn.nav
        path = getattr(nav, 'path', None)
        if nav.aid != aid or not path or conn.block is None:
            self.waiting('graph_or_block')
            return
        centre = (conn.block.cx, conn.block.cy)
        info = conn.s.hm.get((aid, *centre))
        if not info or not info.get('ter') or msg.get('nlGround') != info['ter']:
            self.waiting('terrain_version: server=%s client=%s' % ((info or {}).get('ter'),msg.get('nlGround')))
            return
        self.waiting('planning')
        frame = conn.s.anchors[aid].frame
        # Copy only fully sent content + this anchor's skin. Retry markers are
        # not proof of coverage. The worker never traverses a mutable tracker.
        ready = (set(conn.roads.sent) & set(conn.roads.skins)) - set(conn.roads.building)
        nearby = geotiles.tiles_near(frame, *centre, 650., exact=True)
        tiles = [(gx, gy, copy.deepcopy(conn.roads.info[(gx, gy)]),
                  dict(conn.roads.shown.get((gx, gy), {})))
                 for gx, gy, _ in nearby if (gx, gy) in ready and (gx, gy) in conn.roads.info]
        if len(tiles) > 64:
            tiles = tiles[:64]
        if self.source is None:
            self.source = Source(conn.s.cache_dir, conn.s.net, enabled=True, disk=getattr(conn.s,'cache',None))
        serial = getattr(nav, 'ready_serial', nav.serial)
        signature = (aid, serial, path, info['ter'], tuple((x,y,r.get('h'),conn.roads.skin_h.get((x,y)),tuple(sorted(e.items())))
                     for x,y,r,e in tiles), self.source.known())
        now = time.monotonic()
        if signature != self.signature:
            self.signature = signature
            self.state.begin()  # invalidate a still-running older snapshot
            self.retry_at = None
        elif self.retry_at is None or now < self.retry_at:
            return
        if self.future is not None:
            return
        ticket = self.state.begin()
        self.retry_at = None
        if self.pool is None:
            from runtimeprofiler import ThreadPoolExecutor
            self.pool = ThreadPoolExecutor(1, thread_name_prefix='nl-signals')
        args = (conn.s.out_dir, conn.s.vfs_dir, aid, frame, dict(info), centre, tiles, path, self.source)
        previous = copy.deepcopy(self.groups)
        def work():
            return nlsignalbuild.build(*args, catalog(), previous=previous,
                                      name_key=f'{id(self):x}_{ticket.epoch}_{ticket.generation}')
        self.stats['builds'] += 1
        self.future = asyncio.get_running_loop().run_in_executor(self.pool, work)
        surfaces = [dict(key=geotiles.key(x,y), h=r.get('h'), skin=conn.roads.skin_h.get((x,y)), exact=e,
                         bounds=geotiles.quad(frame,x,y).tolist())
                    for x,y,r,e in tiles]
        self.future.add_done_callback(lambda f: self._built(conn, ticket, serial, info['ter'], f, surfaces))

    def _built(self, conn, ticket, serial, ground, future, surfaces=()):
        self.future = None
        if not self.state.current(ticket) or self.closed:
            # A newer surface/anchor arrived during the build. Schedule that
            # generation at the next pose instead of starving it permanently.
            self.retry_at = 0.
            try:
                result = future.result()
                name = Path(result.get('graphPath','')).name
                if name.startswith(f'nl_{id(self):x}_'):
                    (Path(conn.s.out_dir)/name).unlink(missing_ok=True)
            except Exception: pass
            return
        try:
            result = future.result()
            if result.get('pending'):
                self.waiting(result.get('reason','context_pending'))
                self.stats['pending'] += 1
                self.retry_at = time.monotonic() + 5.
                return
            self.groups = result['groups']
            reasons = {}
            for status in result['statuses']:
                for reason in status.get('reasons', ()):
                    reasons[reason] = reasons.get(reason, 0) + 1
            summary = (len(result['owners']), tuple(sorted(reasons.items())))
            if self.stats.get('placement') != summary:
                self.stats['placement'] = summary
                log.info('native signals placement: %s approaches; rejected groups: %s',
                         summary[0], reasons)
            packet = self.state.offer(ticket, result['parts'], result['owners'])
            if packet is None:
                return
            # Distant parked decks must not hold a nearby junction hostage.
            # Require only tiles within a maximum span of accepted components.
            def dependencies(parts):
                out=[]
                for row in surfaces:
                    q=row['bounds'];xs=[v[0] for v in q];ys=[v[1] for v in q]
                    if any(min(xs)-45<=p['pose'][0]<=max(xs)+45 and min(ys)-45<=p['pose'][1]<=max(ys)+45 for p in parts):
                        out.append({k:v for k,v in row.items() if k!='bounds'})
                return out
            packet.update(ground=ground, graphSerial=serial, owners=result['owners'], bundle=True,
                          surfaces=dependencies(result['parts']))
            groups=[];first=1
            for junction,group in sorted(result['groups'].items()):
                groups.append(dict(key=junction,first=first,count=len(group['parts']),owners=group['owners'],
                                   surfaces=dependencies(group['parts'])))
                first+=len(group['parts'])
            packet['groups']=groups
            self.offered_groups=tuple(frozenset(g['owners']) for g in groups)
            name = f'nl_signals_{id(self):x}_{packet["revision"]}.json'
            path = Path(conn.s.out_dir) / name
            tmp = staging.tmp_for(str(path))
            with open(tmp, 'w') as f:
                f.write(nlsignalbuild.dumps(packet))
            staging.replace(tmp, str(path))
            self.files.extend((path, Path(conn.s.out_dir) / Path(result['graphPath']).name))
            while len(self.files) > 8:
                try:self.files.popleft().unlink(missing_ok=True)
                except OSError:pass
            graph_signature = (serial, result['signalSignature'])
            if graph_signature != self.graph_signature:
                conn.send(dict(type='navgraph', anchorId=ticket.anchor, serial=serial,
                               path=result['graphPath'], nodes=0, edges=0))
                self.graph_signature = graph_signature
            conn.send(dict(type='nl_signals', anchorId=ticket.anchor, epoch=ticket.epoch,
                           revision=packet['revision'], path=f'{conn.s.vfs_dir}/{name}'))
            # New road/terrain versions already change on_pose's signature.
            # Re-solving the same incomplete edge coverage every two seconds
            # cannot load another tile and needlessly keeps a parked city busy.
            # Public shard arrivals are asynchronous under the same manifest,
            # so only incomplete context needs polling without a version change.
            if not result.get('contextComplete',True):
                self.retry_at = time.monotonic() + 5.
        except (SurfacePending, FileNotFoundError):
            self.stats['pending'] += 1
            self.retry_at = time.monotonic() + 2.
        except Exception as exc:
            self.stats['failures'] += 1
            self.retry_at = time.monotonic() + 10.
            log.warning('native signals: %s: %s', type(exc).__name__, exc)
