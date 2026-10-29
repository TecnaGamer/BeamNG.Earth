"""French surveyed boards and European locality signs, in opt-in scenery modes.

Mapped names/positions take precedence. Unmapped approaches may get estimated
entries at a sustained built-up transition, never at an administrative border.
All dynamic faces share signmesh's alphabet; no file/material per settlement.
"""
from functools import lru_cache
import gzip
import hashlib
import json
import math
from pathlib import Path
import numpy as np
import poleprofiles
import signmesh
import europeinfra
import zipfile

DATA_PATH=Path(__file__).with_name('data')/'fr_town_signs.json.gz'
DATA_SHA256='3f3dde11f3f13b1226a8ce190ca57ff1f02b0cc241798604aafa52d84599495e'
VERSION='fr-town-signs-v2'
EU_DATA_PATH=Path(__file__).with_name('data')/'europe_places.zip'
EU_DATA_SHA256='de04ae47eac96d7172fc9091219af0d742f8ec015bda04aec486aea6493372e3'
CLASSES={'primary','secondary','tertiary','unclassified','residential','living_street'}
ESTIMATED_CLASSES={'primary','secondary','tertiary','unclassified'}

class SignData:
    def __init__(self,data):
        self.signs=data['signs'];self.places=data['places'];self.metadata={k:v for k,v in data.items() if k not in ('signs','places')}
        self.sign_grid=self._grid(self.signs);self.place_grid=self._grid(self.places)
        self.mapped_places={s['place_id'] for s in self.signs if 'place_id' in s}
    @staticmethod
    def _grid(rows):
        grid={}
        for r in rows:grid.setdefault((math.floor(r['lon']*10),math.floor(r['lat']*10)),[]).append(r)
        return grid
    def nearby(self,bounds,places=False):
        x0,y0,x1,y1=bounds;grid=self.place_grid if places else self.sign_grid
        return [r for x in range(math.floor(x0*10),math.floor(x1*10)+1)
                for y in range(math.floor(y0*10),math.floor(y1*10)+1) for r in grid.get((x,y),())
                if x0<=r['lon']<=x1 and y0<=r['lat']<=y1]

@lru_cache(maxsize=1)
def bundled():
    raw=DATA_PATH.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=DATA_SHA256:raise ValueError('Town-sign data checksum mismatch')
    return SignData(json.loads(gzip.decompress(raw)))


class EuropeanData:
    """Shared French survey plus lazily read European locality cells."""
    def __init__(self,path=EU_DATA_PATH):
        self.path=Path(path)
        if hashlib.sha256(self.path.read_bytes()).hexdigest()!=EU_DATA_SHA256:
            raise ValueError('European place data checksum mismatch')
        with zipfile.ZipFile(self.path) as z:self.metadata=json.loads(z.read('index.json'))
        self.cells={tuple(c) for c in self.metadata['cells']}
        self._load=lru_cache(maxsize=32)(self._load_cell)
    def _load_cell(self,x,y):
        with zipfile.ZipFile(self.path) as z:return json.loads(z.read(f'{x}_{y}.json'))
    def nearby(self,bounds,places=False):
        x0,y0,x1,y1=bounds
        # Avoid loading the entire French survey while driving in another region.
        rows=([dict(r,country='FR') for r in bundled().nearby(bounds,places)]
              if x1>=-5.6 and x0<=10 and y1>=41 and y0<=51.3 else [])
        if not places:return rows
        for x in range(math.floor(x0),math.floor(x1)+1):
            for y in range(math.floor(y0),math.floor(y1)+1):
                if (x,y) in self.cells:
                    rows.extend(r for r in self._load(x,y) if x0<=r['lon']<=x1 and y0<=r['lat']<=y1)
        return rows


@lru_cache(maxsize=1)
def bundled_europe():return EuropeanData()


def bearing_vector(bearing):
    a=math.radians(bearing);return np.array([math.sin(a),math.cos(a)])


def entry_direction(record,road_tangent,towards_centre=None):
    """Return travel INTO the named town, or None if neither tag nor centre helps.

    Cardinal/numeric direction denotes where the sign faces. forward/backward
    denotes traffic relative to the parent OSM way. Do not apply these tags to
    an arbitrary Overture way direction.
    """
    value=str(record.get('direction','')).strip();d=None
    if value in ('forward','backward') and 'bearing' in record:
        d=bearing_vector(record['bearing'])*(1 if value=='forward' else -1)
    else:
        cardinals=('N','NNE','NE','ENE','E','ESE','SE','SSE','S','SSW','SW','WSW','W','WNW','NW','NNW')
        # 16 compass directions, evenly spaced 22.5 degrees.
        try:
            b=list(cardinals).index(value.upper())*22.5 if value.upper() in cardinals else float(value)
            if math.isfinite(b):d=-bearing_vector(b%360)
        except (ValueError,TypeError):pass
    if d is not None:
        return -d if record.get('kind')=='end' else d
    if towards_centre is not None and np.linalg.norm(towards_centre)>30:
        dot=float(road_tangent@towards_centre)
        if abs(dot)/np.linalg.norm(towards_centre)>.15:return road_tangent*(1 if dot>0 else -1)
    return None


def _nearest(point,roads,way=None,way_ids=None):
    best=None
    for fid,spec,line,_ in roads:
        if spec.cls not in CLASSES or spec.link or len(line)<2:continue
        a,b=np.asarray(line[:-1]),np.asarray(line[1:]);d=b[:,:2]-a[:,:2];l2=np.sum(d*d,axis=1)
        t=np.clip(np.sum((point-a[:,:2])*d,axis=1)/np.maximum(l2,1e-9),0,1)
        proj=a+t[:,None]*(b-a);dist=np.linalg.norm(proj[:,:2]-point,axis=1);i=int(np.argmin(dist))
        if dist[i]>40 or l2[i]<1e-6:continue
        # Prefer a matching source way, then distance and stable road id.
        rank=(0 if way and (way_ids or {}).get(fid)==way else 1,float(dist[i]),str(fid))
        if best is None or rank<best[0]:best=(rank,spec,proj[i],d[i]/math.sqrt(l2[i]),fid)
    return best


def place(frame,roads,box,profile,ground=None,urban=None,data=None,way_ids=None,context_roads=None):
    """(mesh, Forest post rows, diagnostic records) for one road tile, half-open ownership."""
    if not poleprofiles.kit_enabled(profile):return None,[],[]
    import furniture as f
    europe=isinstance(profile,(tuple,list)) and profile[1]=='europe'
    data=data if data is not None else bundled_europe() if europe else bundled()
    french=poleprofiles.kit_selector(profile,frame)
    country_at=poleprofiles.country_selector(profile,frame)
    x0,y0,x1,y1=box;mesh=signmesh.Mesh((x0,y0,0));posts=[];records=[]
    la,lo,_=frame.enu_to_geodetic(np.array([x0-50,x1+50]),np.array([y0-50,y1+50]),np.zeros(2))
    bounds=(float(min(lo)),float(min(la)),float(max(lo)),float(max(la)))
    mapped=data.nearby(bounds)
    lines=[(r[1].width/2,np.asarray(r[2])[:,:2]) for r in roads if len(r[2])>=2]
    def emit(s,p,d,spec,source):
        p=np.array(p,float);right=np.array([d[1],-d[0]])
        country=country_at(*p)
        style=europeinfra.town_style(country,spec.cls,spec.kmh)
        if style is None or (s.get('country') and s['country']!=country):return
        if source!='estimated' and country!='FR':return  # bundled surveyed nodes are French
        if country=='FR':style=None  # keep EB10/EB20 and valid D-route cartouches
        if s.get('kind')=='end' and style is not None and style.exit=='none':return
        # Turkish dotted/dotless i require language-specific uppercasing.
        display_name=s['name'].replace('i','İ') if country=='TR' else s['name']
        width=signmesh.layout(display_name,True if style is None else style.uppercase)[1]
        shoulder=right*(-1 if s.get('kind')=='end' else 1)*(-1 if country in europeinfra.LEFT_HAND else 1)
        # Mapped on-road node: move to the driver's right shoulder. An off-road
        # sign retains its mapped position if it already clears all carriageways.
        if source=='estimated' or any(f._seg_dist_min(p[:2],ln)<hw+.6+width/2 for hw,ln in lines):
            nearest=_nearest(p[:2],roads)
            if nearest is None:return
            p[:2]=nearest[2][:2]+shoulder*(spec.width/2+.7+width/2)
        def clear(q):
            return all(f._seg_dist_min(q[:2],ln)>=hw+.55+width/2 for hw,ln in lines)
        if not clear(p):
            # A real on-road sign beside a farm access must not disappear merely
            # because its first shoulder projection lands in that access.
            hit=_nearest(p[:2],roads)
            if hit is None:return
            same=[r for r in roads if r[0]==hit[4]]
            for offset in (-8.,8.,-16.,16.):
                shifted=_nearest(hit[2][:2]+d*offset,same)
                if shifted is None:continue
                q=shifted[2].copy();q[:2]+=shoulder*(spec.width/2+.7+width/2)
                if clear(q):p=q;break
            else:return
        if not(x0<=p[0]<x1 and y0<=p[1]<y1) or country_at(*p)!=country:return
        if ground is not None:
            p[2]=float(np.asarray(ground(np.array([p[0]]),np.array([p[1]]))).ravel()[0])
        w=mesh.town(p,d,display_name,s.get('ref',''),s.get('kind','both'),style=style)
        # Shared collision-capable pole assets; the batched sign faces are render-only.
        for side in ((-1,1) if w>2.4 else (0,)):
            q=p.copy();q[:2]+=right*side*w*.30
            h=2.35 if side else (2.7 if signmesh.valid_route(s.get('ref','')) else 2.35)
            posts.append(['fr_signpost',round(q[0],3),round(q[1],3),round(q[2],3),
                          round(math.degrees(math.atan2(right[1],right[0])),2),1.,h,0.])
        records.append({'id':s['id'],'name':s['name'],'source':source,'pos':p.tolist(),
                        'route':signmesh.valid_route(s.get('ref','')) if country=='FR' else '',
                        'country':country,'board_type':'built-up' if style is None else style.meaning})
    for s in mapped:
        x,y,_=frame.geodetic_to_enu(s['lat'],s['lon'],0)
        hit=_nearest(np.array([float(x),float(y)]),roads,s.get('way'),way_ids)
        if hit is None:continue
        _,spec,p,tangent,fid=hit;p=p.copy();p[:2]=[x,y]
        centre=None
        if 'centre' in s:
            cx,cy,_=frame.geodetic_to_enu(s['centre'][1],s['centre'][0],0);centre=np.array([cx-x,cy-y])
        d=entry_direction(s,tangent,centre)
        if d is not None:
            source='mapped' if s.get('direction') else 'mapped-position/orientation-estimated'
            if s.get('name_estimated'):source+=' / name-estimated'
            emit(s,p,d,spec,source)
    if urban is not None:
        _estimated(frame,context_roads if context_roads is not None else roads,box,urban,data,emit,country_at)
    import directionsigns
    direction_posts, direction_records = directionsigns.add(frame,roads,box,french,mesh,ground)
    posts.extend(direction_posts); records.extend(direction_records)
    return (mesh if mesh.t else None),posts,records


def _estimated(frame,roads,box,urban,data,emit,country_at=None):
    """One candidate per built-up transition, including short connected features.

    Surveyed signs suppress only their own approach, not every street nearby.
    These are scenic estimates, never administrative or legal city boundaries.
    """
    import furniture as f
    from roadapproaches import Context
    x0,y0,x1,y1=box
    la,lo,_=frame.enu_to_geodetic(np.array([x0-3000,x1+3000]),np.array([y0-3000,y1+3000]),np.zeros(2))
    bounds=(float(min(lo)),float(min(la)),float(max(lo)),float(max(la)))
    places=[p for p in data.nearby(bounds,True) if p.get('place','village') in ('city','town','village')]
    if not places:return
    coords=np.array([frame.geodetic_to_enu(p['lat'],p['lon'],0)[:2] for p in places])
    mapped_xy=[]
    for s in data.nearby(bounds):
        q=np.array(frame.geodetic_to_enu(s['lat'],s['lon'],0)[:2])
        hit=_nearest(q,roads)
        mapped_xy.append((s,q,hit[4] if hit else None))
    eligible=[r for r in roads if r[1].cls in CLASSES and not r[1].link and len(r[2])>=2]
    context=Context(eligible);candidates=[]
    def built(points):
        if not points:return np.zeros(0,bool)
        pts=np.array([p for p,d in points]);tan=np.array([d for p,d in points]);n=np.c_[-tan[:,1],tan[:,0]]
        sample=np.concatenate([pts[:,:2]+n*18,pts[:,:2]-n*18])
        la,lo,_=frame.enu_to_geodetic(sample[:,0],sample[:,1],np.zeros(len(sample)))
        v=np.asarray(urban(la,lo),bool)
        return v[:len(pts)]|v[len(pts):]
    for index,(fid,spec,line,_) in enumerate(eligible):
        # Garden gaps and residential side streets are not village entrances.
        # Surveyed signs on those streets still render through the mapped path.
        if spec.cls not in ESTIMATED_CLASSES:continue
        length=context.lengths[index]
        if length<1:continue
        stations=np.unique(np.r_[np.arange(0,length,15.),length])
        pts,tan=f._at(np.asarray(line),stations)
        states=built(list(zip(pts,tan)))
        for j in np.flatnonzero(states[1:]!=states[:-1]):
            if not (x0-25<=pts[j,0]<=x1+25 and y0-25<=pts[j,1]<=y1+25):continue
            low,high=float(stations[j]),float(stations[j+1])
            # Root refinement has the feature's fixed station origin, not the tile's.
            for _ in range(6):
                mid=(low+high)/2;pm,dm=f._at(np.asarray(line),np.array([mid]))
                if bool(built([(pm[0],dm[0])])[0])==bool(states[j]):low=mid
                else:high=mid
            station=(low+high)/2;point,tangent=context.point(index,station)
            into=1 if states[j+1] else -1;direction=tangent*into
            samples=[context.point(index,station+offset*into) for offset in list(range(-120,-14,15))+list(range(15,121,15))]
            if any(v is None for v in samples):continue
            flags=built(samples)
            if np.mean(flags[:8])>.25 or np.mean(flags[8:])<.65:continue
            dist=np.linalg.norm(coords-point[:2],axis=1)
            if country_at is not None:
                code=country_at(*point)
                if code not in europeinfra.KITS:continue
                dist=np.where([p.get('country',code)==code for p in places],dist,np.inf)
            k=int(np.argmin(dist))
            if dist[k]>2200 or float((coords[k]-point[:2])@direction)<60:continue
            place=places[k]
            # A perpendicular street's mapped entrance must not hide this one.
            def same_approach(sign,q,mapped_fid):
                delta=q-point[:2]
                return ((sign.get('place_id')==place['id'] or signmesh.clean(sign['name'])==signmesh.clean(place['name']))
                        and ((mapped_fid==fid and np.linalg.norm(delta)<450)
                             or (np.linalg.norm(delta)<240 and abs(delta[0]*direction[1]-delta[1]*direction[0])<45)))
            if any(same_approach(sign,q,mapped_fid) for sign,q,mapped_fid in mapped_xy):continue
            candidate={'id':'estimated:'+str(fid)+':'+str(round(station,1)),'name':place['name'],'kind':'both',
                       'country':place.get('country')}
            candidates.append((str(fid),station,candidate,point,direction,spec))
    accepted=[]
    for fid,station,record,point,direction,spec in sorted(candidates,key=lambda v:(v[0],v[1])):
        if any(name==record['name'] and np.linalg.norm(point[:2]-q[:2])<100 and float(direction@d)>.7
               for name,q,d in accepted):continue
        accepted.append((record['name'],point,direction))
        emit(record,point,direction,spec,'estimated')
