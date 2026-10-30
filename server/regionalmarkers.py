"""Opt-in country-specific markers, inferred from road geometry, not surveyed assets.

J1 on bends, J3 at rural intersections, J6 along eligible rural roads, J4 on
pronounced bends in France; other countries use representative local families.
Existing roads, bridges and tunnels remain the authority for
placement exclusions. Every object's country is tested at its actual position.
"""
import math
import numpy as np
from geotiles import box_owns
import europeinfra
import delineatorprofiles

CLASSES = {'primary', 'secondary', 'tertiary', 'unclassified'}


def place(roads, box, french_at, lighting=None):
    import furniture as f
    import islands
    # Keep every road for clearance, even those that receive no markers.
    lines=[(r[1].width/2,np.asarray(r[2])[:,:2]) for r in roads if len(r[2])>=2]
    clearance_roads=[(r[0],r[1].width/2,np.asarray(r[2])) for r in roads if len(r[2])>=2]
    ring = islands.ring_fids(roads)
    roads = [r for r in roads if r[0] not in ring and r[1].cls in CLASSES and not r[1].urban and not r[1].link and len(r[2]) >= 2]
    import delineators
    rows = delineators.place(roads,box,french_at,clearance_roads,lighting)
    x0,y0,x1,y1=box
    def put(kind,q,z,yaw=0,section=None):
        if not box_owns(box, q[0], q[1]):return
        country=french_at(float(q[0]),float(q[1]),float(z))
        if country is True:country='FR'  # legacy tests / fr-varied
        kit=europeinfra.KITS.get(country)
        if kit is None:return
        if section is not None and section>=delineatorprofiles.legacy_share(country):return
        heights={'j1':1.3,'j3':1.3,'j6':1.05,'j4_l':1.85,'j4_r':1.85}
        group='fr_'+kind;height=heights[kind]
        if country!='FR':
            if kind=='j3':return  # J3 is a French intersection marker.
            if kind.startswith('j4_'):group='eu_chevron_'+kit.chevron+'_'+kind[-1]
            elif kit.marker=='fr':group='fr_'+kind
            else:group='eu_marker_'+kit.marker;height=1.15
        rows.append([group,round(float(q[0]),2),round(float(q[1]),2),round(float(z),2),round(yaw%360,1),1.,height,0.])
    for fid,spec,line,_ in roads:
        line=np.asarray(line); length=f._arclen(line)[-1]
        if length<30:continue
        # Sparse, whole stretches rather than a roadside fence on every road.
        # 8% is an explicit scenery trial, not a survey of French road equipment.
        j6 = spec.cls in {'primary', 'secondary'} and length >= 250
        section=f._h01(fid, 'fr-j6-section-v2')
        phase=f._h01(fid,'fr-markers')*90
        distances=np.arange(25+phase,max(25,length-25),90) if j6 else np.array([])
        p,d=f._at(line,distances)
        for pos,tangent in zip(p,d):
            for side in (-1,1):
                n=np.array([-tangent[1],tangent[0]])*side
                q=pos[:2]+n*(spec.width/2+1.1)
                if all(f._seg_dist_min(q,l)>=h+.65 for h,l in lines):
                    # Front towards approaching traffic on each verge. The reverse
                    # reflector is visible to the opposite direction of travel.
                    country=french_at(float(q[0]),float(q[1]),float(pos[2]))
                    leading=side*(1 if country in europeinfra.LEFT_HAND else -1)
                    put('j6',q,pos[2],math.degrees(math.atan2(-tangent[0],tangent[1]))+(180 if leading<0 else 0),section)
        stations=np.arange(18,length-18,10.)
        if not len(stations):continue
        before,_=f._at(line,stations-15);at,_=f._at(line,stations);after,_=f._at(line,stations+15)
        last=-1000
        for s,a,p,b in zip(stations,before,at,after):
            u=p[:2]-a[:2];v=b[:2]-p[:2]
            if min(np.linalg.norm(u),np.linalg.norm(v))<2:continue
            u/=np.linalg.norm(u);v/=np.linalg.norm(v)
            turn=math.atan2(u[0]*v[1]-u[1]*v[0],float(u@v))
            if abs(turn)<math.radians(18) or s-last<55:continue
            last=s;t=u+v;t/=max(np.linalg.norm(t),1e-9)
            outside=np.array([t[1],-t[0]])*np.sign(turn)
            q=p[:2]+outside*(spec.width/2+1.5)
            if not all(f._seg_dist_min(q,l)>=h+.6 for h,l in lines):continue
            kind='j4_l' if turn>0 else 'j4_r'
            if abs(turn)>=math.radians(32):put(kind,q,p[2],math.degrees(math.atan2(-u[0],u[1])))
            for offset in (-10.,10.):
                pp,dd=f._at(line,np.array([s+offset]));pos,tan=pp[0],dd[0]
                n=np.array([tan[1],-tan[0]])*np.sign(turn)
                q2=pos[:2]+n*(spec.width/2+1.2)
                country=french_at(float(q2[0]),float(q2[1]),float(pos[2]))
                # Continuous rows already tighten through this bend. Keep J1
                # unchanged in France and local hazard posts on unequipped roads.
                if country is not True and delineatorprofiles.eligible(country,spec,(lighting or {}).get(fid)):
                    continue
                if country is not True and delineatorprofiles.policy(country).mode!='legacy' and spec.kind!='paved':
                    continue
                if delineatorprofiles.policy(country).unlit and str((lighting or {}).get(fid)).lower() in {'yes','true','1','24/7'}:
                    continue
                if all(f._seg_dist_min(q2,l)>=h+.6 for h,l in lines):
                    put('j1',q2,pos[2],math.degrees(math.atan2(-tan[0],tan[1])))
    for arms in f._arms(roads).values():
        if len(arms)<3:continue
        wide=max(a[1].width for a in arms)/2
        for fid,spec,u,c,*_ in arms:
            right=np.array([-u[1],u[0]])
            q=f.signal_spot(c,u,right,wide,spec.width/2,lines)
            if q is not None:put('j3',q,c[2])
    return rows
