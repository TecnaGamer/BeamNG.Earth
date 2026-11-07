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
from geotiles import box_owns
import poleprofiles
import signmesh
import europeinfra

DATA_PATH=Path(__file__).with_name('data')/'fr_town_signs.cells'      # (pack_cells of the survey's fr_town_signs.json.gz)
DATA_SHA256='c72a2a3690d00392973f2d02776abd6a8199bd4629323df1da2583ec3351205a'
VERSION='town-signs-measured-v3'
CLASSES={'primary','secondary','tertiary','unclassified','residential','living_street'}
ESTIMATED_CLASSES={'primary','secondary','tertiary','unclassified'}

CELLS_MAGIC=b'TSC1'
CELLS_KEEP=512          # decoded 0.1-degree cells kept per kind (a session's tiles touch a few dozen)


def _cell_key(r):
    return (math.floor(r['lon']*10),math.floor(r['lat']*10))


class SignData:
    """Signs and places on a 0.1-degree grid. From a document (dict: small direction-sign sets, tests) the rows stay
    as they are; from a cells file (pack_cells: the French survey) each cell stays compressed until a tile asks for
    it: the survey's 84,000 rows decoded were 51 MB in every server process (77 MB at load), 2026-11-07."""
    def __init__(self,data=None,cells=None):
        self._lock=__import__('threading').Lock()
        self._decoded={'s':{},'p':{}}
        if cells is not None:
            self.metadata,self._index,self._blob=cells
            return
        self.metadata={k:v for k,v in data.items() if k not in ('signs','places')}
        self._index=None
        self._grids={'s':self._grid(data['signs']),'p':self._grid(data['places'])}
        self._rows={'s':data['signs'],'p':data['places']}
    @staticmethod
    def _grid(rows):
        grid={}
        for r in rows:grid.setdefault(_cell_key(r),[]).append(r)
        return grid
    def _cell(self,kind,key):
        if self._index is None:return self._grids[kind].get(key,())
        got=self._decoded[kind].get(key)
        if got is not None:return got
        ref=self._index[kind].get(key)
        if ref is None:return ()
        import zlib
        got=json.loads(zlib.decompress(self._blob[ref[0]:ref[0]+ref[1]]))
        with self._lock:
            d=self._decoded[kind]
            d[key]=got
            while len(d)>CELLS_KEEP:d.pop(next(iter(d)))
        return got
    def _all(self,kind):
        if self._index is None:return self._rows[kind]
        return [r for key in sorted(self._index[kind]) for r in self._cell(kind,key)]
    @property
    def signs(self):return self._all('s')
    @property
    def places(self):return self._all('p')
    @property
    def mapped_places(self):return {s['place_id'] for s in self.signs if 'place_id' in s}
    def nearby(self,bounds,places=False):
        x0,y0,x1,y1=bounds;kind='p' if places else 's'
        return [r for x in range(math.floor(x0*10),math.floor(x1*10)+1)
                for y in range(math.floor(y0*10),math.floor(y1*10)+1) for r in self._cell(kind,(x,y))
                if x0<=r['lon']<=x1 and y0<=r['lat']<=y1]


def pack_cells(doc):
    """A survey document {"signs", "places", ...} as cells-file bytes: CELLS_MAGIC, a 4-byte header length, the header
    JSON (metadata; per kind "x,y" -> [offset, length]), then each cell's rows as zlib-compressed JSON."""
    import zlib
    blobs,index,off=[],{'s':{},'p':{}},0
    for kind,rows in (('s',doc['signs']),('p',doc['places'])):
        for key,cell in sorted(SignData._grid(rows).items()):
            b=zlib.compress(json.dumps(cell,separators=(',',':'),ensure_ascii=False).encode('utf-8'),9)
            index[kind][f'{key[0]},{key[1]}']=[off,len(b)];blobs.append(b);off+=len(b)
    header=json.dumps({'metadata':{k:v for k,v in doc.items() if k not in ('signs','places')},'cells':index},
                      separators=(',',':'),ensure_ascii=False).encode('utf-8')
    return CELLS_MAGIC+len(header).to_bytes(4,'little')+header+b''.join(blobs)


def read_cells(raw):
    """SignData's cells argument from cells-file bytes (pack_cells)."""
    if raw[:4]!=CELLS_MAGIC:raise ValueError('not a town-sign cells file')
    n=int.from_bytes(raw[4:8],'little');h=json.loads(raw[8:8+n])
    index={kind:{tuple(int(v) for v in k.split(',')):tuple(ref) for k,ref in cells.items()} for kind,cells in h['cells'].items()}
    return h['metadata'],index,memoryview(raw)[8+n:]


@lru_cache(maxsize=1)
def bundled():
    raw=DATA_PATH.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=DATA_SHA256:raise ValueError('Town-sign data checksum mismatch')
    return SignData(cells=read_cells(raw))


class EuropeanData:
    """Shared French survey plus lazily read European locality cells (regionaldata "places": Overture localities,
    one degree cell each, fetched where needed)."""
    def nearby(self,bounds,places=False):
        import regionaldata
        x0,y0,x1,y1=bounds
        # Avoid loading the entire French survey while driving in another region.
        rows=([dict(r,country='FR') for r in bundled().nearby(bounds,places)]
              if x1>=-5.6 and x0<=10 and y1>=41 and y0<=51.3 else [])
        if not places:return rows
        for x in range(math.floor(x0),math.floor(x1)+1):
            for y in range(math.floor(y0),math.floor(y1)+1):
                rows.extend(r for r in (regionaldata.cell('places',x,y) or ()) if x0<=r['lon']<=x1 and y0<=r['lat']<=y1)
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


def place(frame,roads,box,profile,ground=None,urban=None,data=None,way_ids=None,context_roads=None,street_names=None,sign_obstacles=None,decks=(),local_directions=False):
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
        style=europeinfra.town_style(country,spec.cls,spec.kmh,place_kind=s.get('place'),variant=s.get('sign_variant'))
        if style is None or (s.get('country') and s['country']!=country):return
        if source!='estimated' and country!='FR':return  # bundled surveyed nodes are French
        if country=='FR':style=None  # keep EB10/EB20 and valid D-route cartouches
        if s.get('kind')=='end' and style is not None and style.exit=='none':return
        # Turkish dotted/dotless i require language-specific uppercasing.
        display_name=s['name'].replace('i','İ') if country=='TR' else s['name']
        raw_details=s.get('admin_lines')
        details=tuple(signmesh.clean(x,False) for x in raw_details if isinstance(x,str))[:2] if isinstance(raw_details,(tuple,list)) else ()
        plan=signmesh.town_layout(display_name,style,details)
        width=plan.width+(.13 if country=='DE' else 0.)
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
        if not box_owns(box, p[0], p[1]) or country_at(*p)!=country:return
        if ground is not None:
            p[2]=float(np.asarray(ground(np.array([p[0]]),np.array([p[1]]))).ravel()[0])
        w=mesh.town(p,d,display_name,s.get('ref',''),s.get('kind','both'),style=style,details=details)
        # Shared collision-capable pole assets; the batched sign faces are render-only.
        for side in ((-1,1) if w>2.4 else (0,)):
            q=p.copy();q[:2]+=right*side*w*.30
            h=plan.top-.03
            if country=='DE':h=plan.top+.06
            if country=='FR' and signmesh.valid_route(s.get('ref','')):h=plan.top+.28
            posts.append(['fr_signpost',round(q[0],3),round(q[1],3),round(q[2],3),
                          round(math.degrees(math.atan2(right[1],right[0])),2),1.,h,0.])
        records.append({'id':s['id'],'name':s['name'],'source':source,'pos':p.tolist(),
                        'route':signmesh.valid_route(s.get('ref','')) if country=='FR' else '',
                        'country':country,'board_type':'built-up' if style is None else style.meaning,
                        'size_m':[plan.width,plan.height],'lettering':plan.profile.font,
                        'layout':plan.profile.family,'admin_lines':list(details)})
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
    if not local_directions:
        import directionsigns
        direction_posts, direction_records = directionsigns.add(frame,roads,box,french,mesh,ground)
        posts.extend(direction_posts); records.extend(direction_records)
    import euguardrails
    if euguardrails.enabled():
        import frstreets
        new_posts,new_records=frstreets.place(frame,roads,box,country_at,mesh,street_names,ground,sign_obstacles,decks)
        posts.extend(new_posts);records.extend(new_records)
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
                       'country':place.get('country'),'place':place.get('place'),
                       'admin_lines':place.get('admin_lines',[])}
            candidates.append((str(fid),station,candidate,point,direction,spec))
    accepted=[]
    for fid,station,record,point,direction,spec in sorted(candidates,key=lambda v:(v[0],v[1])):
        if any(name==record['name'] and np.linalg.norm(point[:2]-q[:2])<100 and float(direction@d)>.7
               for name,q,d in accepted):continue
        accepted.append((record['name'],point,direction))
        emit(record,point,direction,spec,'estimated')
