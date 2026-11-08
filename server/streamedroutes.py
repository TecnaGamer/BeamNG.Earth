"""Route shields from the roads already fetched by normal map streaming.

Uses OSM way membership for identity and actual road junctions for stations.
No regional sign inventory, extra road-data provider, or per-frame queries.
"""
from collections import OrderedDict, defaultdict
import threading
import numpy as np
import exitsigns
import highwayroutes
import mapcells
import railtags
import routeindex
import routerelations
import routerepeaters
import signroads


class Source:
    def __init__(self, md):
        self.md=md
        self.relations=routerelations.Source(md.raw_dir)
        self.cache=OrderedDict()
        self.lock=threading.Lock()

    def _cell(self,key):
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        rows={}
        # The source features are whole in every cell they cross. A fixed halo
        # sees the junction's other arms without depending on the requesting tile.
        for neighbour in [key, *key.neighbours()]:
            features=self.md.cell(neighbour,layers=('roads',),kind='demand').layers.get('roads')
            if features is None:
                return None  # not ready: never freeze an incomplete junction graph
            for i,fid in enumerate(features.ids):
                rows.setdefault(str(fid),(features,i))
        result=stations(list(rows.values()),self.relations)
        if result is None:return None
        x0,y0,x1,y1=key.bounds()
        result=[o for o in result if x0<=o['lon']<x1 and y0<o['lat']<=y1]
        self.cache[key]=result
        while len(self.cache)>32:self.cache.popitem(last=False)
        return result

    def query(self,lon0,lat0,lon1,lat1,existing=()):
        if lon1 < -180 or lon0 > -60 or lat1 < 18 or lat0 > 72:return []
        result={}
        with self.lock:
            for key in mapcells.cells_for_bbox(lat0,lon0,lat1,lon1):
                objects=self._cell(key)
                if objects is None:return None
                result.update((o['id'],o) for o in objects if lon0<=o['lon']<=lon1 and lat0<=o['lat']<=lat1)
        return [o for o in routerepeaters.thin(list(result.values()),(lon0,lat0,lon1,lat1))
                if not any(routerepeaters.keys(o)&routerepeaters.keys(old) and
                           routerepeaters.distance(o,old)<routerepeaters.SEPARATION_M
                           for old in existing if old.get('roadside',{}).get('type')=='shield')]


def stations(rows,relations):
    lines=[];neighbours=defaultdict(set);seen=set()
    for features,i in rows:
        specs=signroads.cell_specs(features)
        spec=specs[i]
        if spec is None or spec.cls not in routeindex.ROAD_CLASSES:continue
        records=features.attrs.get('src_record')
        wid=railtags.way_id(str(records[i])) if records is not None else None
        wid=int(wid) if wid is not None else None
        # All road arms contribute topology; only explicit OSM relation members
        # receive numbered shields. Road class never selects a network.
        for part in features.parts(i):
            line=np.asarray(part[0],float)
            if len(line)<2:continue
            nodes=[tuple(p) for p in np.round(line[:,:2],7)]
            for a,b in zip(nodes,nodes[1:]):
                if a!=b:neighbours[a].add(b);neighbours[b].add(a)
            if wid and not signroads.is_link(features,i):
                identity=(str(features.ids[i]),tuple(nodes))
                if identity not in seen:
                    seen.add(identity);lines.append((wid,line[:,:2],spec,nodes,features,i))
    membership=relations.query(wid for wid,*_ in lines)
    if getattr(relations,'ready',True) is False:return None
    out=[]
    for wid,line,spec,nodes,features,i in lines:
        routes=highwayroutes.recognize({},membership.get(wid,()))
        if not routes:continue
        flags=features.rules.get('flags')
        if flags is not None and any(str(flags.value[r]) in ('is_bridge','is_tunnel','is_under_construction') for r in flags.rows(i)):
            continue
        directions=(spec.dir or 1,) if spec.oneway else (1,-1)
        for direction in directions:
            path=line if direction>0 else line[::-1]
            vertex=nodes[0] if direction>0 else nodes[-1]
            if len(neighbours[vertex])<3:continue
            scale=np.array([111320.*np.cos(np.radians(path[:,1].mean())),111320.])
            arc=np.r_[0.,np.cumsum(np.linalg.norm(np.diff(path,axis=0)*scale,axis=1))]
            if arc[-1]<145.:continue
            def at(distance):
                distance=min(distance,arc[-1]);k=min(len(path)-2,max(0,int(np.searchsorted(arc,distance,side='right')-1)))
                t=(distance-arc[k])/max(arc[k+1]-arc[k],1e-9)
                return path[k]*(1-t)+path[k+1]*t,k
            point,k=at(100.)
            # Reject stations close to another mapped junction along the road.
            ordered=nodes if direction>0 else nodes[::-1]
            if any(abs(arc[j]-100.)<45. for j,n in enumerate(ordered[1:],1) if len(neighbours[n])>2):continue
            axis=(path[k+1]-path[k])*scale;axis/=max(np.linalg.norm(axis),1e-9)
            # Overture geometry may reverse the original OSM way; membership
            # identifies the road, while generated one-way rules control travel.
            active=routes
            approach=np.vstack([at(40.)[0],path[(arc>40.)&(arc<160.)],at(160.)[0]])
            panels=[highwayroutes.panel(r,routeindex.posted_direction({},membership[wid],direction,r,axis)) for r in active]
            extra=dict(type='shield',point=point.tolist(),approach=approach.tolist(),main_id=wid,
                       axis=axis.tolist(),routes=panels,source='streamed_roads')
            obj=routeindex.record(('streamed-route-v1',wid,vertex,tuple(active),direction),point,extra)
            obj['roadside']=obj.pop('ways')[0];out.append(obj)
    return routeindex.unique(out)
