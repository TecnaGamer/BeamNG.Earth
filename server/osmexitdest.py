"""Small cached OSM-ID reads for recreation exits missing from the weekly index.

The existing streamed roads discover ramp way IDs and provide geometry. No bbox
or Overpass API query, nearby-park routing guess, or offline sign inventory.
"""
import json
import os
from pathlib import Path
import tempfile
import time
from concurrent.futures import CancelledError

import numpy as np
import dnp
import exitsigns
import recreationrules
import routeindex
import xproc

WEEK = 7*86400


class Tags:
    def __init__(self, md):
        self.directory = Path(md.cache_dir)/'osm-exit-destinations-v1'
        self.fetcher = md.fetcher
        self.failed_until = 0.

    def read(self, kind, ids):
        ids = sorted(set(int(i) for i in ids))
        if not ids:
            return {}
        def load(fresh):
            result = {}
            for ident in ids:
                target = self.directory/kind/f'{ident}.json'
                try:
                    if fresh and int(target.stat().st_mtime)//WEEK != int(time.time())//WEEK:
                        continue
                    result[ident] = json.loads(target.read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    pass
            return result
        cached = load(True)
        if len(cached) == len(ids):
            return cached
        stale = load(False)
        if time.monotonic() < self.failed_until:
            if len(stale) == len(ids):return stale
            raise OSError('OSM destination source awaiting retry')
        lock = str(self.directory/kind/'fetch.lock')
        ready = lambda: data if len(data := load(True)) == len(ids) else None
        shared, mine = xproc.once(lock, ready, 10.)
        if shared is not None:return shared
        try:
            cached = load(True)
            missing = [i for i in ids if i not in cached]
            for start in range(0, len(missing), 64):
                batch = missing[start:start+64]
                url = f'https://api.openstreetmap.org/api/0.6/{kind}s.json?{kind}s='+','.join(map(str,batch))
                elements = json.loads(self.fetcher.get(url, kind='demand', lane='recreation-exits').body)['elements']
                found = {int(e['id']):e for e in elements if e.get('type') == kind}
                if set(found) != set(batch):raise ValueError('Incomplete OSM destination response')
                folder = self.directory/kind;folder.mkdir(parents=True, exist_ok=True)
                for ident, element in found.items():
                    # No contributor/account information is needed by the renderer.
                    value = {k:element[k] for k in ('id','type','nodes','tags') if k in element}
                    fd, temporary = tempfile.mkstemp(dir=folder, suffix='.tmp')
                    try:
                        with os.fdopen(fd,'w',encoding='utf-8') as stream:json.dump(value,stream)
                        os.replace(temporary,folder/f'{ident}.json')
                    finally:
                        if os.path.exists(temporary):os.unlink(temporary)
                    cached[ident] = value
            return cached
        except CancelledError:raise
        except Exception:
            self.failed_until = time.monotonic()+60.
            if len(stale) == len(ids):return stale
            raise
        finally:
            if mine:xproc.release(lock)


def records(links, mains, ways, nodes):
    """Directed ramp-to-mainline matches, with posted labels and actual exit numbers."""
    result = []
    for row in links:
        way = ways.get(row['wid'],{});tags = way.get('tags',{})
        path = np.asarray(row['path'],float)
        if len(path)<2 or tags.get('access') in ('private','no') or tags.get('service'):
            continue
        if exitsigns._direction(tags)<0:path=path[::-1]
        refs=way.get('nodes',());node=nodes.get(refs[-1] if exitsigns._direction(tags)<0 and refs else refs[0] if refs else None,{})
        label=exitsigns.labels(node.get('tags',{}),tags,backwards=exitsigns._direction(tags)<0)
        if not label or not label['ref'] or not any(recreationrules.destination(n) for n in label['names']):continue
        point=path[0];scale=np.array([111320.*dnp.cos(np.radians(point[1])),111320.])
        ramp_direction=(path[min(2,len(path)-1)]-point)*scale
        ramp_direction/=max(dnp.norm(ramp_direction),1e-9)
        choices=[]
        for main in mains:
            sp=main['spec']
            if sp.cls not in ('motorway','trunk') or not sp.oneway:continue
            p=np.asarray(main['path'] if (sp.dir or 1)>0 else main['path'][::-1]);xy=(p-point)*scale
            delta=np.diff(xy,axis=0);lengths=dnp.norm(delta,axis=1)
            t=np.clip(-dnp.vecdot(xy[:-1],delta)/np.maximum(lengths * lengths,1e-9),0,1)
            distance=dnp.norm(xy[:-1]+t[:,None]*delta,axis=1);k=int(distance.argmin())
            if distance[k]>6. or lengths[k]<1e-6:continue
            tangent=delta[k]/lengths[k]
            if dnp.matmul(tangent, ramp_direction)<.15:continue
            station=float(lengths[:k].sum()+t[k]*lengths[k])
            if station<250:continue
            projected=p[k]+t[k]*(p[k+1]-p[k]);arc=np.r_[0.,np.cumsum(lengths)]
            start=max(0.,station-1800.)
            approach=np.vstack([[np.interp(start,arc,p[:,j]) for j in (0,1)],p[(arc>start)&(arc<station)],projected])
            right=np.array([tangent[1],-tangent[0]])
            lateral=dnp.matmul((path-point)*scale, right)
            side=next((1 if v>0 else -1 for v in lateral if abs(v)>1.),None)
            if side is None:continue
            choices.append((float(distance[k]),main['fid'],dict(label=label,approach=approach.tolist(),
                ramp=path.tolist(),main_id=main['wid'],main_ids=[main['wid']] if main['wid'] else [],side=side)))
        if choices:
            extra=min(choices,key=lambda c:c[:2])[2]
            result.append(dict(id=routeindex.uid(('runtime-recreation-exit',row['wid'])),
                               lon=float(point[0]),lat=float(point[1]),tags={},ways=[],exit=extra))
    return result


class Source:
    def __init__(self, md):self.tags=Tags(md)

    def objects(self, links, mains):
        ways=self.tags.read('way',[r['wid'] for r in links if r['wid']])
        selected=[]
        for way in ways.values():
            label=exitsigns.labels({},way.get('tags',{}))
            if label and any(recreationrules.destination(n) for n in label['names']):selected.append(way)
        node_ids=[w['nodes'][-1] if exitsigns._direction(w.get('tags',{}))<0 else w['nodes'][0]
                  for w in selected if w.get('nodes') and not w.get('tags',{}).get('junction:ref')]
        return records(links,mains,ways,self.tags.read('node',node_ids))
