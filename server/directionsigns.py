"""Mapped French white direction arrows; no inferred destination text.

A source sign's physical arrow points towards the mapped outgoing node/way,
readable on both faces. A nearby drivable road must support the outgoing target;
this excludes hiking/cycle guideposts from the road scene. Incomplete mappings
are omitted. This is a subset of French directional signing, not a routing UI.
"""
from functools import lru_cache
import gzip,hashlib,json,math
from pathlib import Path
import numpy as np
from geotiles import box_owns

DATA_PATH=Path(__file__).with_name('data')/'fr_direction_signs.json.gz'
DATA_SHA256='be2a2a625ef55710c34c55f11a9ff05ac691d20775d6690da7c374b00702397f'

@lru_cache(maxsize=1)
def bundled():
    from townsigns import SignData
    raw=DATA_PATH.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=DATA_SHA256:raise ValueError('Direction-sign data checksum mismatch')
    doc=json.loads(gzip.decompress(raw));doc['places']=[]
    return SignData(doc)


def add(frame,roads,box,french,mesh,ground=None,data=None):
    import furniture as f
    from townsigns import _nearest
    data=bundled() if data is None else data;x0,y0,x1,y1=box;posts=[];records=[]
    la,lo,_=frame.enu_to_geodetic(np.array([x0-50,x1+50]),np.array([y0-50,y1+50]),np.zeros(2))
    near=data.nearby((float(min(lo)),float(min(la)),float(max(lo)),float(max(la))))
    lines=[(r[1].width/2,np.asarray(r[2])[:,:2]) for r in roads if len(r[2])>=2]
    occupied=set(); support_rows={}
    for s in near:
        x,y,_=frame.geodetic_to_enu(s['lat'],s['lon'],0);xy=np.array([x,y])
        hit=_nearest(xy,roads)
        if hit is None:continue
        _,spec,p,tangent,fid=hit
        if hit[0][1]>25:continue
        ox,oy,_=frame.geodetic_to_enu(s['origin'][1],s['origin'][0],0)
        tx,ty,_=frame.geodetic_to_enu(s['towards'][1],s['towards'][0],0)
        target_hit=_nearest(np.array([tx,ty]),roads)
        if target_hit is None or target_hit[0][1]>12:continue
        direction=np.array([tx-ox,ty-oy]);norm=np.linalg.norm(direction)
        if norm<2:continue
        direction/=norm;travel=np.array([-direction[1],direction[0]])
        p=p.copy();p[:2]=xy
        if any(f._seg_dist_min(xy,line)<half+.6 for half,line in lines):
            right=np.array([tangent[1],-tangent[0]])
            candidates=[hit[2][:2]+side*right*(spec.width/2+1.5)+tangent*along
                        for along in (0,3,-3,6,-6) for side in (1,-1)]
            chosen=next((q for q in candidates if all(f._seg_dist_min(q,line)>=half+.65 for half,line in lines)),None)
            if chosen is None:continue
            p[:2]=chosen
        if not box_owns(box, p[0], p[1]) or not french(*p):continue
        # Multiple relations on the same support: a stack, bounded to four rows.
        key=(round(float(p[0]),1),round(float(p[1]),1),tuple(s['labels']))
        if key in occupied:continue
        occupied.add(key)
        if ground is not None:p[2]=float(np.asarray(ground(np.array([p[0]]),np.array([p[1]]))).ravel()[0])
        support=(round(float(p[0]),1),round(float(p[1]),1))
        first=support_rows.get(support,0); labels=s['labels'][:max(0,4-first)]
        if not labels:continue
        support_rows[support]=first+len(labels)
        ref=s.get('ref','') if first==0 else ''
        mesh.direction(p,travel,labels,'right',ref,start_row=first)
        mesh.direction(p,-travel,labels,'left',ref,body=False,start_row=first)
        if first==0:
            posts.append(['fr_signpost',round(p[0],3),round(p[1],3),round(p[2],3),0.,1.,3. if ref else 2.7,0.])
        records.append({'id':s['id'],'name':'; '.join(labels),'kind':'direction',
                        'source':'mapped-direction' if s.get('mapped_position',True) else 'mapped-relation/position-estimated',
                        'pos':p.tolist(),'route':s.get('ref','')})
    return posts,records
