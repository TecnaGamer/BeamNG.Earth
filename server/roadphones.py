"""Surveyed French SOS phones, bundled offline; never a network call in a tile."""
from functools import lru_cache
import gzip
import hashlib
import json
from pathlib import Path
import numpy as np
import dnp

DATA=Path(__file__).with_name('data')/'fr_sos_phones.json.gz'


@lru_cache(maxsize=1)
def signature():
    return hashlib.sha256(DATA.read_bytes()).hexdigest()


@lru_cache(maxsize=1)
def bundled():
    doc=json.loads(gzip.decompress(DATA.read_bytes()))
    grid={}
    for row in doc['phones']:
        grid.setdefault((int(np.floor(row['lon']*10)),int(np.floor(row['lat']*10))),[]).append(row)
    return doc,grid


def nearby(frame,box,pad=20.):
    x0,y0,x1,y1=box
    lat,lon,_=frame.enu_to_geodetic(np.array([x0-pad,x1+pad,x0-pad,x1+pad]),np.array([y0-pad,y0-pad,y1+pad,y1+pad]),0.)
    _,grid=bundled();out=[]
    for x in range(int(np.floor(min(lon)*10)),int(np.floor(max(lon)*10))+1):
        for y in range(int(np.floor(min(lat)*10)),int(np.floor(max(lat)*10))+1):
            for row in grid.get((x,y),()):
                if min(lon)<=row['lon']<=max(lon) and min(lat)<=row['lat']<=max(lat):
                    p=np.array(frame.geodetic_to_enu(row['lat'],row['lon'],0.),float)
                    out.append(dict(row,point=p))
    return out


def on_highways(frame,roads,box,pad=25.):
    """Surveyed phones outside ground carriageways, with an unambiguous road side.

    A town-sign nearest-road helper deliberately excludes motorways, so phones
    use their own projection. Access geometry and visible phones share this
    selection; an unsupported phone cannot cut a hole in a barrier elsewhere.
    """
    from furniture import _seg_dist_min
    import countrybounds
    all_lines=[(sp.width/2,np.asarray(line)[:,:2])for _,sp,line,*_ in roads if len(line)>1]
    highways=[r for r in roads if r[1].cls in {'motorway','trunk'} and r[1].kind=='paved'
              and not r[1].link and len(r[2])>1]
    out=[]
    for phone in nearby(frame,box,pad):
        if countrybounds.bundled().at(phone['lat'],phone['lon'])!='FR':continue
        p=phone['point'].copy();best=None
        for fid,sp,line,*_ in highways:
            line=np.asarray(line,float);d=np.diff(line,axis=0);l2=(d[:,:2] * d[:,:2]).sum(1)
            u=np.clip(((p[:2]-line[:-1,:2])*d[:,:2]).sum(1)/np.maximum(l2,1e-9),0,1)
            q=line[:-1]+u[:,None]*d;ds=dnp.norm(q[:,:2]-p[:2],axis=1);k=int(ds.argmin())
            if ds[k]>22 or ds[k]<sp.width/2+.5 or l2[k]<1e-9:continue
            key=(float(ds[k]),str(fid))
            if best is None or key<best[0]:best=(key,fid,q[k],d[k,:2]/np.sqrt(l2[k]))
        if best is None:continue
        _,fid,q,t=best
        if any(_seg_dist_min(p[:2],line)<half+.5 for half,line in all_lines):continue
        p[2]=q[2];n=p[:2]-q[:2];n/=max(dnp.norm(n),1e-9)
        out.append(dict(phone,point=p,roadpoint=q,travel=t,normal=n,road_id=fid))
    return out
