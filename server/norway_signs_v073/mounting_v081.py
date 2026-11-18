"""Support/face/column layout for NVDB records. No destination-name rules."""
import math
import numpy as np

def unit(v):
    v=np.asarray(v,float);n=float(np.linalg.norm(v))
    if not math.isfinite(n) or n<1e-8:raise ValueError('Invalid sign normal')
    return v/n

def slot(row):
    """NVDB 5128: tens = vertical level, units = column; missing is unknown."""
    value=row.get('position_code')
    try:
        if isinstance(value,bool):return None,0
        n=int(value)
        if float(value)!=n or not 10<=n<=99:return None,0
        return n//10,n%10
    except (TypeError,ValueError,OverflowError):return None,0

def metering_side(obj):
    refs=(obj.get('lokasjon') or {}).get('vegsystemreferanser') or []
    sides={(r.get('metrertLokasjon') or {}).get('sideposisjon') for r in refs}
    sides.discard(None)
    return next(iter(sides)) if len(sides)==1 and sides<={'H','V'} else None

def normal(row,physical,reference,fallback):
    """Use recorded metered-road side and compass to disambiguate traffic faces.

    The road-offset perpendicular remains a local tangent approximation.
    """
    mode=row['facing'];v=fallback
    if mode in ('trafikk i metreringsretning','trafikk mot metreringsretning'):
        off=unit(np.asarray(physical)-np.asarray(reference));v=np.array([off[1],-off[0]])
        if row.get('metering_side')=='V':v=-v
        if mode=='trafikk mot metreringsretning':v=-v
        hint={'nord':(0,1),'sør':(0,-1),'øst':(1,0),'vest':(-1,0)}.get(row.get('compass'))
        if hint is not None and float(np.dot(v,hint))<0:v=-v
    return None if v is None else unit(v)

def supports(records):
    groups=[]
    # Sorting makes grouping and tie handling independent of API page order.
    for rec in sorted(records,key=lambda r:r[0]['id']):
        pid=rec[0].get('pole_id')
        hit=next((g for g in groups if
            ((pid and pid==g[0][0].get('pole_id') and all(np.linalg.norm(rec[1]-x[1])<=1.0 for x in g)) or
             (not pid and not g[0][0].get('pole_id') and all(np.linalg.norm(rec[1]-x[1])<.35 for x in g)))
            and all(abs(rec[6]-x[6])<=.3 for x in g)),None)
        if hit is None:groups.append([rec])
        else:hit.append(rec)
    return groups

def face_groups(records):
    groups=[]
    for rec in records:
        mode=rec[0].get('facing')
        hit=next((g for g in groups if mode==g[0][0].get('facing') and
                  all(float(np.dot(unit(rec[2]),unit(r[2])))>=math.cos(math.radians(12)) for r in g)),None)
        if hit is None:groups.append([rec])
        else:hit.append(rec)
    return groups

def face_grid(records,gap):
    # A diamond and a single route badge are a common two-plate assembly.
    # Honour distinct surveyed slots. Resolve missing or duplicate slots only
    # for this identifiable pair, after support and face grouping.
    if len(records) == 2:
        first, second = sorted(records, key=lambda r:r[0]['kind'])
        if (first[0]['kind'] in ('206','208') and second[0]['code'] in
                ('723.11','723.12','723.13','723.14','723.15','723.16')):
            first_slot, second_slot = slot(first[0]), slot(second[0])
            if first_slot[0] is None or second_slot[0] is None or first_slot == second_slot:
                def with_slot(rec, position):
                    return (dict(rec[0], position_code=position), *rec[1:])
                records = [with_slot(first,10), with_slot(second,20)]
    cells={};unknown=[];highest=max((slot(r[0])[0] or 0 for r in records),default=0)
    for r in records:
        level,col=slot(r[0])
        if level is None:unknown.append(r)
        else:cells.setdefault((level,col),[]).append(r)
    # Missing/colliding metadata must never discard content or overlap faces.
    for j,r in enumerate(unknown,1):cells[(highest+j,0)]=[r]
    steps=[]
    for level in sorted({key[0] for key in cells}):
        cols={c:sorted(rs,key=lambda r:r[0]['id']) for (lv,c),rs in cells.items() if lv==level}
        for k in range(max(len(rs) for rs in cols.values())):
            steps.append({col:rs[k] for col,rs in cols.items() if k<len(rs)})
    widths={c:max(r[4] for step in steps for col,r in step.items() if col==c) for c in {c for step in steps for c in step}}
    total=sum(widths.values())+.12*max(0,len(widths)-1);cursor=total/2;centers={}
    for col in sorted(widths):centers[col]=cursor-widths[col]/2;cursor-=widths[col]+.12
    heights=[max(r[5] for r in step.values()) for step in steps]
    depth=sum(heights)+sum(gap(a,b) for a,b in zip(heights,heights[1:]))
    return steps,centers,heights,depth,total

def plan(records,gap,priority_positions=()):
    plans=[]
    for group in supports(records):
        q=np.mean([r[1] for r in group],axis=0);ground=float(np.mean([r[6] for r in group]))
        faces=face_groups(group);axes=[]
        for face in faces:
            n=unit(np.mean([r[2] for r in face],axis=0));grid=face_grid(face,gap)
            entry=(face,n,grid)
            axis=next((a for a in axes if len(a)==1 and
                       face[0][0].get('facing')!='tosidig' and a[0][0][0][0].get('facing')!='tosidig' and
                       float(np.dot(n,a[0][1]))<=-math.cos(math.radians(12))),None)
            if axis is None:axes.append([entry])
            else:axis.append(entry)
        base=2.55 if any(np.linalg.norm(q-p)<1.5 for p in priority_positions) else 1.78
        batches=[];max_width=0
        # Perpendicular arrangements lack bracket geometry: use separate height
        # bands rather than make large boards intersect at the support center.
        for axis in axes:
            axis_height=max(e[2][3] for e in axis);top=base+axis_height
            for face,n,(steps,centers,heights,_,total) in axis:
                right=np.array([-n[1],n[0]])
                # Placement columns are defined relative to metering direction.
                flip=-1 if face[0][0].get('facing')=='trafikk mot metreringsretning' else 1
                plane=q+n*(.18 if len(axis)>1 else 0.)
                cursor=top;cols={};bases={}
                for i,step in enumerate(steps):
                    cursor-=heights[i]
                    for col,r in step.items():
                        row,_,_,mat,w,h,_=r
                        # Top-align boards occupying the same mounting level.
                        bases[row['id']]=cursor+heights[i]-h
                        cols.setdefault(col,[]).append((row,plane+right*centers[col]*flip,n,mat,w,h,ground))
                    if i+1<len(heights):cursor-=gap(heights[i],heights[i+1])
                for col,rs in cols.items():batches.append({'records':rs,'bases':{r[0]['id']:bases[r[0]['id']] for r in rs}})
                max_width=max(max_width,total)
            base=top+.12
        plans.append({'records':group,'center':q,'ground':ground,'normal':unit(group[0][2]),
                      'top':base-.12,'width':max_width,'batches':batches})
    return plans
