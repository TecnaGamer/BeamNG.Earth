"""Optional European steel profiles and estimated motorway outer-edge rails.

Mapped concrete/cable barriers remain authoritative. Country presets follow the
user's community reference map, not an engineering inventory of every road.
"""
import hashlib
import math
import os
import numpy as np
import geotiles

VERSION='eu-road-details-2026-11-v5-motorway-verges'
VERGE_CONTEXT_M = 32.0
PROFILES={c:('a','red') for c in
          'AD AL AM AT AX AZ BA BE BG BY CH CY CZ DE DK EE ES FI FO FR GB GE GG GI GR HR HU IE IM IS IT JE LI LT LU LV MC MD ME MK MT NL NO PL PT RO RS RU SE SI SK SM TR UA VA XK'.split()}
for c in 'DE IE ME TR'.split():PROFILES[c]=('mixed','red')
for c in 'DK PL HR RS'.split():PROFILES[c]=('b','red')
for c in 'CZ SK'.split():PROFILES[c]=('thin','red')
for c in 'FR IS SE LU MC'.split():PROFILES[c]=(PROFILES[c][0],'white')
for c in 'ES BE GI AD'.split():PROFILES[c]=(PROFILES[c][0],'yellow')
for c in 'IT SM VA'.split():PROFILES[c]=(PROFILES[c][0],'orange_red')
for c in 'GB IE IM JE GG DK NO FI AX FO NL DE CH LI PT EE'.split():PROFILES[c]=(PROFILES[c][0],'none')
PROFILES['LT']=('a','orange');PROFILES['LV']=('a','white_red')
FAMILIES=('a','b','thin');REFLECTORS=('white','red','yellow','orange','orange_red','white_red','none')
# Prefix keeps trees.lua's continuous barrier collision/thinning treatment.
GROUPS=tuple('guardrail_eu_'+f+'_'+r+'_'+part for f in FAMILIES for r in REFLECTORS
             for part in ('run','start','end'))


def enabled():
    return os.environ.get('BNE_EU_ROAD_DETAILS','off').lower() in {'1','on','true'}


def country_at(frame,point):
    import countrybounds
    lat,lon,_=frame.enu_to_geodetic(*point)
    return countrybounds.bundled().at(float(lat),float(lon))


def preset(country,key=''):
    family,colour=PROFILES.get(country,('a','white'))
    if family=='mixed':family=('a','b')[hashlib.blake2s(str(key).encode(),digest_size=1).digest()[0]%2]
    return family,colour


def group_index(country,key='',part='run'):
    import guardrails
    family,colour=preset(country,key)
    return len(guardrails.STOCK_GROUPS)+GROUPS.index('guardrail_eu_'+family+'_'+colour+'_'+part)


def localize(frame,rows):
    if not enabled():return rows
    result=[]
    for row in rows:
        r=list(row);g=int(r[0])
        if g<6:
            country=country_at(frame,r[1:4])
            if country in PROFILES:
                # Basis of reverse-facing rails already carries its rotation.
                part=('run','start','end','run','end','start')[g]
                lat,lon,_=frame.enu_to_geodetic(*r[1:4])
                key=f'{float(lat):.2f}/{float(lon):.2f}'
                r[0]=group_index(country,key,part)
        result.append(r)
    return result


def _rail_segments(rows):
    """Only barrier modules, including mapped concrete/cable, for suppression."""
    a=[];b=[]
    for r in rows:
        if len(r)<17:continue
        t=np.array(r[11:13] if int(r[0]) in (6,7) else r[8:10])
        a.append(np.array(r[1:3])-t*1.5);b.append(np.array(r[1:3])+t*1.5)
    return np.asarray(a).reshape(-1,2),np.asarray(b).reshape(-1,2)


def _near_barrier(q,segments,reach=2.0):
    a,b=segments
    if not len(a):return False
    d=b-a;t=np.clip(((q-a)*d).sum(1)/np.maximum((d*d).sum(1),1e-9),0,1)
    return bool((np.linalg.norm(q-a-t[:,None]*d,axis=1)<reach).any())


def is_motorway_mainline(spec):
    return spec.cls == 'motorway' and spec.oneway and not spec.link and spec.kind == 'paved'


def _verge_evidence(rows):
    """Mapped modules and short openings between facing mapped terminals."""
    a, b = _rail_segments(rows)
    xyz = np.asarray([r[1:4] for r in rows if len(r) >= 17], float).reshape(-1, 3)
    lines = np.column_stack((b - a, np.zeros(len(a))))
    terminals = [(np.asarray(r[1:4]), np.asarray(r[8:10]) * (-1 if r[0] in (1, 5) else 1))
                 for r in rows if len(r) >= 17 and r[0] in (1, 2, 4, 5)]
    if terminals:
        ends, outward = map(np.asarray, zip(*terminals))
        gaps, spans = [], []
        for i, (end, heading) in enumerate(terminals):
            d = ends[i+1:] - end
            along = d[:, :2] @ heading
            across = d[:, 0] * heading[1] - d[:, 1] * heading[0]
            match = ((along >= 4.) & (along <= 25.) & (np.abs(across) <= 1.5)
                     & (np.abs(d[:, 2]) <= 1.8) & (outward[i+1:] @ heading < -.94))
            gaps.extend(end + .5 * d[match])
            spans.extend(d[match])
        if gaps:
            xyz = np.vstack((xyz, gaps))
            lines = np.vstack((lines, spans))
    half = .5 * np.linalg.norm(lines[:, :2], axis=1)
    return xyz, lines / np.maximum(2. * half[:, None], 1e-9), half


def _verge_covered(q, tangent, outward, evidence):
    xyz, axes, half = evidence
    delta = q - xyz
    along = np.clip((delta[:, :2] * axes[:, :2]).sum(1), -half, half)
    offset = delta - along[:, None] * axes
    across = offset[:, :2] @ outward
    return bool(((np.abs(axes[:, :2] @ tangent) > .85)
                 & (np.abs(offset[:, :2] @ tangent) < 1.75)
                 & (np.abs(offset[:, 2]) < 2.)
                 & (across > -6.) & (across < 1.5)).any())


def _clear(q,segments,margin=.55):
    if segments is None:return True
    v=q[:2]-segments['a'];s=np.clip((v*segments['unit']).sum(1),0,segments['len'])
    return not (np.linalg.norm(v-s[:,None]*segments['unit'],axis=1)<segments['half']+margin).any()


def sos_access(rows,phones,road_segments=None):
    """Recess existing and estimated steel rails; two sloped ends leave access.

    The dimensions are scenery estimates. Concrete, cable, bridge-level rails
    and phones in the carriageway are untouched. The phone's source position
    remains fixed; only the local steel run adapts around it.
    """
    import guardrails as g
    rows=[list(r)for r in rows];removed=set();caps=[]
    for phone in phones:
        p=phone['point'];t=phone['travel'];n=phone['normal'];sides={}
        for i,r in enumerate(rows):
            if i in removed or len(r)<17 or int(r[0])<len(g.STOCK_GROUPS):continue
            local=int(r[0])-len(g.STOCK_GROUPS)
            if local%3:continue                         # an existing terminal
            q=np.asarray(r[1:4],float);delta=p[:2]-q[:2]
            along=float(delta@t);across=float(delta@n)
            if not 0.<across<5. or abs(along)>22 or abs(p[2]-q[2])>1.8:continue
            if abs(along)<6.:
                removed.add(i);continue
            amount=min(.85,max(0.,across-.6))*(1-abs(along)/22.)**2
            moved=q.copy();moved[:2]+=n*amount
            if not _clear(moved,road_segments):continue
            r[1:4]=list(map(float,moved))
            # Chords follow the shallow bend instead of keeping the old tangent.
            forward=np.array(r[8:11],float)
            slope=2*min(.85,max(0.,across-.6))*(1-abs(along)/22.)/22.*np.sign(along)
            forward[:2]+=n*slope*float(forward[:2]@t)
            forward/=max(np.linalg.norm(forward),1e-9)
            back=np.cross([0.,0.,1.],forward);back/=max(np.linalg.norm(back),1e-9)
            r[8:17]=list(forward)+list(back)+list(np.cross(forward,back))
            if abs(along)<8.79:
                side=1 if along>0 else -1
                if side not in sides or abs(along)<sides[side][0]:sides[side]=(abs(along),r)
        for _,r in sides.values():
            cap=list(r);forward=np.array(r[8:11]);direction=1 if (p[:2]-r[1:3])@forward[:2]>0 else -1
            pos=np.array(r[1:4])+forward*direction*1.45
            cap[1:4]=list(map(float,pos));cap[0]=int(r[0])+(2 if direction>0 else 1)
            caps.append(cap)
    return [r for i,r in enumerate(rows)if i not in removed]+caps


def extend(frame,roads,box,elev,mapped,decks=(),*,context=None):
    """Outer rail estimates: no crossing carriageways, ramps, bridges or mapped barriers.

    Uses full feature station origins then half-open tile ownership. This is
    intentionally a visual motorway fallback, not a claim rails exist everywhere.
    """
    if not enabled():return mapped
    from europeinfra import LEFT_HAND
    import furniture as f
    import guardrails as g
    from roadphones import on_highways
    phones=on_highways(frame,roads,box,50)
    rsegs=g._road_segments(roads)
    evidence=_verge_evidence(mapped if context is None else context)
    deck_segments=g._deck_segments(decks)
    result=list(mapped);seen=set()
    for fid,spec,line,*_ in roads:
        if not is_motorway_mainline(spec):continue
        line=np.asarray(line,float)
        if len(line)<2 or line.shape[1]<3:continue
        identity=(str(fid),tuple(np.round(line[0,:2],2)),tuple(np.round(line[-1,:2],2)))
        if identity in seen:continue
        seen.add(identity)
        length=f._arclen(line)[-1]
        if length<4:continue
        count=max(1,int(math.ceil(length/2.8)))
        stations=(np.arange(count)+.5)*(length/count)
        # Existing source direction is preserved; dir chooses the driving side.
        pts,tans=f._at(line,stations);before,_=f._at(line,stations-1.5);after,_=f._at(line,stations+1.5)
        side=(1 if country_at(frame,pts[len(pts)//2]) in LEFT_HAND else -1)*(spec.dir or 1)
        for p,t,a,b in zip(pts,tans,before,after):
            n=np.array([-t[1],t[0]])*side
            q=p.copy();q[:2]+=n*(spec.width/2+1.0)
            if not geotiles.box_owns(box,*q[:2]):continue
            country=country_at(frame,q)
            if country not in PROFILES:continue
            # All carriageways protect against longitudinal overlap and junction closure.
            if any(not _clear(q+np.r_[t*step,0.],rsegs)for step in (-1.5,0.,1.5)):continue
            if _verge_covered(q,t[:2],n,evidence):continue
            if deck_segments is not None and g._relate(q[:2],deck_segments,t[:2]) is not None:continue
            forward=np.r_[t[:2],(b[2]-a[2])/3.];forward/=max(np.linalg.norm(forward),1e-9)
            # The model's smooth side is -local Y; face it toward the road.
            if side<0:forward=-forward
            back=np.cross([0,0,1],forward);back/=max(np.linalg.norm(back),1e-9)
            up=np.cross(forward,back)
            lat,lon,_=frame.enu_to_geodetic(q[0],q[1],q[2])
            q[2]=float(frame.geodetic_to_enu(lat,lon,float(np.asarray(elev(lat,lon))))[2])
            row=[group_index(country,str(fid)),*map(float,q),math.degrees(math.atan2(forward[1],forward[0])),0.,.9,0.,*forward,*back,*up]
            result.append(row)
    return sos_access(localize(frame,result),phones,rsegs)
